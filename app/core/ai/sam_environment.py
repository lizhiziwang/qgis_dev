"""SAM 外部运行环境的依赖探测与一键 pip 安装。

依赖清单（外部 Python）：

- ``torch`` / ``torchvision``：PyTorch；
- ``numpy`` / ``opencv-python``（import ``cv2``）；
- ``segment_anything``：SAM1，可来自 pip 源码包或 ``sam_src`` 目录；
- ``sam2``：SAM2 / SAM2.1；
- ``sam3``：SAM3 / SAM3.1（官方 PyPI 包名为 sam3，从 GitHub 源码安装）。

探测脚本只用 ``importlib.util.find_spec`` 与 ``importlib.metadata``，不真正 import
torch（否则打开设置页要等好几秒）。模块本身不依赖 Qt；``DependencyInstaller`` 是最
后的 Qt 封装。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from qgis.PyQt.QtCore import QObject, QProcess, QProcessEnvironment, pyqtSignal

from app.infra.logging import get_logger

logger = get_logger(__name__)

SEGMENT_ANYTHING_URL = (
    "https://github.com/facebookresearch/segment-anything/archive/refs/heads/main.zip"
)
SAM2_URL = "https://github.com/facebookresearch/sam2/archive/refs/heads/main.zip"
SAM3_URL = "https://github.com/facebookresearch/sam3/archive/refs/heads/main.zip"

#: SAM3 的运行时依赖：--no-deps 安装时手动补齐（见 build_install_commands）
SAM3_EXTRA_REQUIREMENTS: tuple[str, ...] = (
    "timm",
    "ftfy==6.1.1",
    "regex",
    "iopath",
    "huggingface_hub",
    "tqdm",
    "typing_extensions",
    "einops",  # sam3/sam/rope.py
    "pycocotools",  # sam3/train/data/coco_json_loaders.py
    # sam3/model_builder.py 仍 import pkg_resources；setuptools 81 起已移除该模块
    "setuptools<81",
)
if sys.platform == "win32":
    # Windows 的 torch 轮子不带 triton；sam3/model/edt.py 顶层 import triton
    SAM3_EXTRA_REQUIREMENTS += ("triton-windows",)

TORCH_INDEX_URLS: dict[str, str] = {
    "cpu": "https://download.pytorch.org/whl/cpu",
    "cuda124": "https://download.pytorch.org/whl/cu124",
}

#: 子进程环境里需要剔除的变量（QGIS 运行时会污染外部 Python）
STRIP_ENV: tuple[str, ...] = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "QT_PLUGIN_PATH",
    "QGIS_PREFIX_PATH",
    "GDAL_DATA",
    "PROJ_LIB",
    "PROJ_DATA",
    "QT_QPA_PLATFORM",
)

MAX_LOG_LINES = 2000


@dataclass(frozen=True)
class DependencySpec:
    key: str
    title: str
    module: str
    distributions: tuple[str, ...]
    requirement: str
    installable: bool = True


DEPENDENCIES: tuple[DependencySpec, ...] = (
    DependencySpec("torch", "PyTorch", "torch", ("torch",), "torch"),
    DependencySpec("torchvision", "TorchVision", "torchvision", ("torchvision",), "torchvision"),
    DependencySpec("numpy", "NumPy", "numpy", ("numpy",), "numpy"),
    DependencySpec(
        "opencv",
        "OpenCV",
        "cv2",
        ("opencv-python", "opencv-contrib-python", "opencv-python-headless"),
        "opencv-python",
    ),
    DependencySpec(
        "segment_anything",
        "Segment Anything (SAM1)",
        "segment_anything",
        ("segment-anything", "segment_anything"),
        SEGMENT_ANYTHING_URL,
    ),
    DependencySpec("sam2", "SAM2 / SAM2.1", "sam2", ("SAM-2", "sam2"), SAM2_URL),
    DependencySpec("sam3", "SAM3 / SAM3.1", "sam3", ("SAM-3", "sam3"), SAM3_URL),
)

_DEPENDENCIES_BY_KEY = {spec.key: spec for spec in DEPENDENCIES}


@dataclass(frozen=True)
class DependencyStatus:
    spec: DependencySpec
    found: bool
    version: str = ""
    origin: str = ""


@dataclass(frozen=True)
class EnvironmentReport:
    python: str = ""
    executable: str = ""
    sam_src_ok: bool = False
    statuses: tuple[DependencyStatus, ...] = ()
    error: str = ""
    raw: dict = field(default_factory=dict, compare=False)

    @property
    def missing(self) -> tuple[DependencyStatus, ...]:
        return tuple(item for item in self.statuses if not item.found)

    @property
    def missing_keys(self) -> tuple[str, ...]:
        return tuple(item.spec.key for item in self.missing)

    def status(self, key: str) -> DependencyStatus | None:
        for item in self.statuses:
            if item.spec.key == key:
                return item
        return None


# ---------------------------------------------------------------------- 环境清洗
def clean_process_environment(env: QProcessEnvironment) -> QProcessEnvironment:
    """剔除 QGIS 注入的 Python/Qt/GDAL 变量并固定 UTF-8 输出。"""
    for key in STRIP_ENV:
        env.remove(key)
    env.insert("PYTHONUNBUFFERED", "1")
    env.insert("PYTHONIOENCODING", "utf-8")
    return env


def stripped_environ_dict(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """``clean_process_environment`` 的纯 dict 版本（便于单测/非 Qt 调用）。"""
    result = {key: value for key, value in dict(base or os.environ).items() if key not in STRIP_ENV}
    result["PYTHONUNBUFFERED"] = "1"
    result["PYTHONIOENCODING"] = "utf-8"
    return result


# ---------------------------------------------------------------------- 探测
_SPECS_LITERAL = repr(
    [(spec.key, spec.module, spec.distributions) for spec in DEPENDENCIES]
)

_PROBE_TEMPLATE = r'''
import json
import os
import sys
from importlib import metadata, util

specs = __SPECS__


def _version(dist_names):
    for name in dist_names:
        try:
            return metadata.version(name)
        except Exception:
            continue
    return ""


sam_src = sys.argv[1] if len(sys.argv) > 1 else ""
sam_src_ok = bool(sam_src) and os.path.isdir(sam_src)
if sam_src_ok:
    sys.path.insert(0, sam_src)
sam_module_origin = os.path.join(sam_src, "segment_anything") if sam_src_ok else ""

modules = {}
for key, module, dist_names in specs:
    try:
        found = util.find_spec(module) is not None
    except Exception:
        found = False
    origin = "外部 Python"
    if key == "segment_anything" and sam_module_origin and os.path.isdir(sam_module_origin):
        origin = "SAM 源码目录"
    modules[key] = {
        "found": bool(found),
        "version": _version(dist_names) if found else "",
        "origin": origin,
    }

payload = {
    "python": ".".join(str(v) for v in sys.version_info[:3]),
    "executable": sys.executable,
    "sam_src_ok": sam_src_ok,
    "modules": modules,
}
sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
sys.stdout.flush()
'''


def probe_script() -> str:
    return _PROBE_TEMPLATE.replace("__SPECS__", _SPECS_LITERAL)


def build_probe_command(python: str, sam_src: str = "") -> list[str]:
    return [str(python), "-c", probe_script(), str(sam_src or "")]


def python_problem(python: str) -> str | None:
    """本地快速校验解释器路径；真正可用性由探测脚本回答。"""
    if not python:
        return "尚未配置外部 Python"
    if not os.path.isfile(python):
        return f"外部 Python 不存在：{python}"
    return None


def parse_probe_output(text: str, *, error: str = "") -> EnvironmentReport:
    """解析探测脚本的 JSON 输出；失败时返回带 ``error`` 的空报告。"""
    raw_text = (text or "").strip()
    if not raw_text:
        return EnvironmentReport(error=error or "依赖探测没有输出")
    line = raw_text.splitlines()[-1]
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return EnvironmentReport(error=error or f"依赖探测输出无法解析：{line[:200]}")
    if not isinstance(payload, dict):
        return EnvironmentReport(error=error or "依赖探测输出格式错误")
    modules = payload.get("modules")
    if not isinstance(modules, dict):
        modules = {}
    statuses = []
    for spec in DEPENDENCIES:
        item = modules.get(spec.key)
        if not isinstance(item, dict):
            item = {}
        statuses.append(
            DependencyStatus(
                spec=spec,
                found=bool(item.get("found")),
                version=str(item.get("version") or ""),
                origin=str(item.get("origin") or ""),
            )
        )
    return EnvironmentReport(
        python=str(payload.get("python") or ""),
        executable=str(payload.get("executable") or ""),
        sam_src_ok=bool(payload.get("sam_src_ok")),
        statuses=tuple(statuses),
        error=error,
        raw=payload,
    )


# ---------------------------------------------------------------------- 安装
def build_install_commands(
    python: str,
    missing_keys: Iterable[str],
    *,
    torch_variant: str = "cpu",
) -> list[list[str]]:
    """把缺失依赖拼成 pip 命令列表（torch 与其它包分开，避免 index 互相影响）。"""
    missing = {str(key) for key in missing_keys}
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
        _DEPENDENCIES_BY_KEY[key].requirement
        for key in ("numpy", "opencv", "segment_anything", "sam2")
        if key in missing
    ]
    if others:
        commands.append([*base, *others])

    if "sam3" in missing:
        # sam3 的 pyproject 钉死 numpy<2，而 Python 3.13 只有 numpy>=2 的轮子；
        # --no-deps 安装源码包，再手动补齐其运行时依赖，避免把现有 numpy 降级。
        commands.append([*base, "--no-deps", SAM3_URL])
        commands.append([*base, *SAM3_EXTRA_REQUIREMENTS])
    return commands


def missing_specs(missing_keys: Iterable[str]) -> list[DependencySpec]:
    return [_DEPENDENCIES_BY_KEY[key] for key in missing_keys if key in _DEPENDENCIES_BY_KEY]


class DependencyInstaller(QObject):
    """在 GUI 线程用 QProcess 顺序执行 pip 命令，流式转发日志。"""

    log = pyqtSignal(str)
    finished = pyqtSignal(bool, int)  # ok, exit code（取消为 -2）
    started = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process: QProcess | None = None
        self._commands: list[list[str]] = []
        self._index = 0
        self._canceled = False
        self._ok = True

    @property
    def is_running(self) -> bool:
        return self._process is not None

    def start(self, python: str, missing_keys: Iterable[str], *, torch_variant: str = "cpu") -> None:
        if self.is_running:
            self.log.emit("已有安装任务在运行")
            return
        commands = build_install_commands(python, missing_keys, torch_variant=torch_variant)
        self.start_commands(commands)

    def start_commands(self, commands: Iterable[Iterable[str]]) -> None:
        """直接执行一组 pip 命令（训练依赖等其它安装流程复用）。"""
        if self.is_running:
            self.log.emit("已有安装任务在运行")
            return
        prepared = [list(command) for command in commands if command]
        if not prepared:
            self.finished.emit(True, 0)
            return
        self._commands = prepared
        self._index = 0
        self._canceled = False
        self._ok = True
        self.started.emit()
        self._run_next()

    def cancel(self) -> None:
        self._canceled = True
        process = self._process
        if process is not None and process.state() != QProcess.ProcessState.NotRunning:
            self.log.emit("正在取消安装…")
            process.kill()

    def _run_next(self) -> None:
        if self._canceled:
            self._finish(False, -2)
            return
        if self._index >= len(self._commands):
            self._finish(True, 0)
            return
        args = self._commands[self._index]
        self._index += 1
        self.log.emit("> " + " ".join(args))
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.setProgram(args[0])
        process.setArguments(args[1:])
        env = clean_process_environment(QProcessEnvironment.systemEnvironment())
        process.setProcessEnvironment(env)
        process.readyReadStandardOutput.connect(lambda p=process: self._read_output(p))
        process.finished.connect(lambda code, status, p=process: self._on_finished(p, code, status))
        process.errorOccurred.connect(lambda error, p=process: self._on_process_error(p, error))
        self._process = process
        process.start()

    def _read_output(self, process: QProcess) -> None:
        text = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if text:
            self.log.emit(text.rstrip("\r\n"))

    def _on_finished(self, process: QProcess, code: int, status: QProcess.ExitStatus) -> None:
        if self._process is not process:
            return
        self._read_output(process)
        self._process = None
        process.deleteLater()
        if self._canceled:
            self._finish(False, -2)
            return
        if status != QProcess.ExitStatus.NormalExit or code != 0:
            self._finish(False, code)
            return
        self._run_next()

    def _on_process_error(self, process: QProcess, error: QProcess.ProcessError) -> None:
        if self._process is not process:
            return
        self.log.emit(f"pip 进程启动/运行失败：{process.errorString()}")
        self._process = None
        process.deleteLater()
        self._finish(False, -1)

    def _finish(self, ok: bool, code: int) -> None:
        if self._process is not None:
            self._process.deleteLater()
            self._process = None
        self.finished.emit(bool(ok and not self._canceled), int(code))


__all__ = [
    "DEPENDENCIES",
    "MAX_LOG_LINES",
    "SAM3_EXTRA_REQUIREMENTS",
    "SAM3_URL",
    "STRIP_ENV",
    "DependencyInstaller",
    "DependencySpec",
    "DependencyStatus",
    "EnvironmentReport",
    "build_install_commands",
    "build_probe_command",
    "clean_process_environment",
    "missing_specs",
    "parse_probe_output",
    "probe_script",
    "python_problem",
    "stripped_environ_dict",
]
