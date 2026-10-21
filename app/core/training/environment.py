"""训练外部运行环境的依赖探测与一键安装。

与 SAM 环境共用同一个外部 Python 和 ``clean_process_environment``；
探测只做 ``importlib.util.find_spec``，不真正 import torch（打开训练页不卡顿）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.core.ai.sam_environment import (
    TORCH_INDEX_URLS,
    clean_process_environment,
    python_problem,
)

#: 训练所需模块；``requirement`` 为 pip 包名
TRAINING_DEPENDENCIES: tuple[dict, ...] = (
    {"key": "torch", "title": "PyTorch", "module": "torch", "distributions": ("torch",), "requirement": "torch"},
    {"key": "torchvision", "title": "TorchVision", "module": "torchvision", "distributions": ("torchvision",), "requirement": "torchvision"},
    {"key": "numpy", "title": "NumPy", "module": "numpy", "distributions": ("numpy",), "requirement": "numpy"},
    {
        "key": "opencv",
        "title": "OpenCV",
        "module": "cv2",
        "distributions": ("opencv-python", "opencv-contrib-python", "opencv-python-headless"),
        "requirement": "opencv-python",
    },
    {
        "key": "pillow",
        "title": "Pillow",
        "module": "PIL",
        "distributions": ("pillow", "Pillow"),
        "requirement": "pillow",
    },
    {
        "key": "smp",
        "title": "Segmentation Models PyTorch",
        "module": "segmentation_models_pytorch",
        "distributions": ("segmentation-models-pytorch",),
        "requirement": "segmentation-models-pytorch",
    },
)

def _specs_by_key(dependencies: Iterable[dict]) -> dict[str, dict]:
    """按 key 索引依赖表（训练 / 推理各传各的）。"""
    return {spec["key"]: spec for spec in dependencies}


@dataclass(frozen=True)
class TrainingDependencyStatus:
    key: str
    title: str
    found: bool
    version: str = ""


@dataclass(frozen=True)
class TrainingEnvironmentReport:
    python: str = ""
    executable: str = ""
    statuses: tuple[TrainingDependencyStatus, ...] = ()
    error: str = ""
    raw: dict = field(default_factory=dict, compare=False)

    @property
    def missing(self) -> tuple[TrainingDependencyStatus, ...]:
        return tuple(item for item in self.statuses if not item.found)

    @property
    def missing_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.missing)

    @property
    def ok(self) -> bool:
        return not self.error and not self.missing

    def status(self, key: str) -> TrainingDependencyStatus | None:
        for item in self.statuses:
            if item.key == key:
                return item
        return None


_PROBE_TEMPLATE = r'''
import json
import sys
from importlib import metadata, util

# 可选的额外 sys.path（例如 SAM 源码目录）；由调用方作为第 1 个参数传入
extra_sys_path = sys.argv[1] if len(sys.argv) > 1 else ""
if extra_sys_path:
    sys.path.insert(0, extra_sys_path)

specs = __SPECS__


def _version(dist_names):
    for name in dist_names:
        try:
            return metadata.version(name)
        except Exception:
            continue
    return ""


modules = {}
for spec in specs:
    try:
        found = util.find_spec(spec["module"]) is not None
    except Exception:
        found = False
    modules[spec["key"]] = {
        "found": bool(found),
        "version": _version(tuple(spec["distributions"])) if found else "",
    }

payload = {
    "python": ".".join(str(v) for v in sys.version_info[:3]),
    "executable": sys.executable,
    "modules": modules,
}
sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
sys.stdout.flush()
'''


def probe_script(dependencies: Iterable[dict] = TRAINING_DEPENDENCIES) -> str:
    specs = [
        {
            "key": spec["key"],
            "module": spec["module"],
            "distributions": list(spec["distributions"]),
        }
        for spec in dependencies
    ]
    return _PROBE_TEMPLATE.replace("__SPECS__", repr(specs))


def build_probe_command(
    python: str,
    *,
    dependencies: Iterable[dict] = TRAINING_DEPENDENCIES,
    extra_sys_path: str = "",
) -> list[str]:
    command = [str(python), "-c", probe_script(dependencies)]
    if extra_sys_path:
        command.append(str(extra_sys_path))
    return command


def parse_probe_output(
    text: str,
    *,
    error: str = "",
    dependencies: Iterable[dict] = TRAINING_DEPENDENCIES,
    label: str = "训练",
) -> TrainingEnvironmentReport:
    raw_text = (text or "").strip()
    if not raw_text:
        return TrainingEnvironmentReport(error=error or f"{label}依赖探测没有输出")
    line = raw_text.splitlines()[-1]
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return TrainingEnvironmentReport(
            error=error or f"{label}依赖探测输出无法解析：{line[:200]}"
        )
    if not isinstance(payload, dict):
        return TrainingEnvironmentReport(error=error or f"{label}依赖探测输出格式错误")
    modules = payload.get("modules")
    if not isinstance(modules, dict):
        modules = {}
    statuses = []
    for spec in dependencies:
        item = modules.get(spec["key"])
        if not isinstance(item, dict):
            item = {}
        statuses.append(
            TrainingDependencyStatus(
                key=spec["key"],
                title=spec["title"],
                found=bool(item.get("found")),
                version=str(item.get("version") or ""),
            )
        )
    return TrainingEnvironmentReport(
        python=str(payload.get("python") or ""),
        executable=str(payload.get("executable") or ""),
        statuses=tuple(statuses),
        error=error,
        raw=payload,
    )


def build_install_commands(
    python: str,
    missing_keys: Iterable[str],
    *,
    torch_variant: str = "cpu",
    dependencies: Iterable[dict] = TRAINING_DEPENDENCIES,
) -> list[list[str]]:
    """缺失依赖 → pip 命令；torch 系列单独指定 index，其余一次装完。"""
    missing = {str(key) for key in missing_keys}
    specs_by_key = _specs_by_key(dependencies)
    commands: list[list[str]] = []
    base = [
        str(python),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--no-input",
        "--disable-pip-version-check",
    ]
    torch_keys = {"torch", "torchvision"} & missing
    if torch_keys:
        args = [*base, "torch", "torchvision"]
        index = TORCH_INDEX_URLS.get(str(torch_variant or "cpu").lower())
        if index:
            args.extend(["--index-url", index])
        commands.append(args)
    others = [
        spec["requirement"]
        for key, spec in specs_by_key.items()
        if key in missing and key not in ("torch", "torchvision")
    ]
    if others:
        commands.append([*base, *others])
    return commands


def default_torch_variant() -> str:
    return "cpu"


def missing_titles(report: TrainingEnvironmentReport) -> str:
    return "、".join(item.title for item in report.missing)


__all__ = [
    "TRAINING_DEPENDENCIES",
    "TrainingDependencyStatus",
    "TrainingEnvironmentReport",
    "build_install_commands",
    "build_probe_command",
    "clean_process_environment",
    "default_torch_variant",
    "missing_titles",
    "parse_probe_output",
    "probe_script",
    "python_problem",
]
