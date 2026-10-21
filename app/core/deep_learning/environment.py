"""标注 / 训练 / 推理共用的第三方依赖目录与 pip 安装命令。

三个工作区运行在同一个外部 Python 里（训练 / 推理可在 YAML/QSettings 里覆盖
解释器，见 :mod:`app.infra.deep_learning`）。本模块把三张旧依赖表合并成一张
统一目录，供「深度学习 → 设置」使用；旧表与页面级安装按钮保持原样，由一致性
测试锁住，避免漂移。
"""

from __future__ import annotations

from collections.abc import Iterable

from app.core.ai.sam_environment import (
    DEPENDENCIES as SAM_DEPENDENCIES,
)
from app.core.ai.sam_environment import (
    SAM3_EXTRA_REQUIREMENTS,
    TORCH_INDEX_URLS,
)
from app.core.inference.environment import RASTERIO_DEPENDENCY
from app.core.training import environment as training_environment
from app.core.training.environment import (
    TRAINING_DEPENDENCIES,
    TrainingEnvironmentReport,
)

#: 依赖归属页面的 id 与显示名
ALL_USAGES: tuple[str, ...] = ("annotation", "training", "inference")
USAGE_TITLES: dict[str, str] = {
    "annotation": "标注",
    "training": "训练",
    "inference": "推理",
}

_SAM_BY_KEY = {spec.key: spec for spec in SAM_DEPENDENCIES}
_TRAINING_BY_KEY = {spec["key"]: spec for spec in TRAINING_DEPENDENCIES}


def _sam_spec(key: str, usages: tuple[str, ...]) -> dict:
    spec = _SAM_BY_KEY[key]
    return {
        "key": spec.key,
        "title": spec.title,
        "module": spec.module,
        "distributions": tuple(spec.distributions),
        "requirement": spec.requirement,
        "usages": tuple(usages),
    }


def _training_spec(key: str, usages: tuple[str, ...]) -> dict:
    spec = dict(_TRAINING_BY_KEY[key])
    spec["distributions"] = tuple(spec["distributions"])
    spec["usages"] = tuple(usages)
    return spec


#: 统一依赖表（顺序即设置页表格顺序）
DEEP_LEARNING_DEPENDENCIES: tuple[dict, ...] = (
    # 标注 / 训练 / 推理共用
    _sam_spec("torch", ALL_USAGES),
    _sam_spec("torchvision", ALL_USAGES),
    _sam_spec("numpy", ALL_USAGES),
    _sam_spec("opencv", ALL_USAGES),
    # 仅标注（Meta SAM）
    _sam_spec("segment_anything", ("annotation",)),
    _sam_spec("sam2", ("annotation",)),
    _sam_spec("sam3", ("annotation",)),
    # 训练 / 推理
    _training_spec("pillow", ("training", "inference")),
    _training_spec("smp", ("training", "inference")),
    # 仅推理
    {
        **RASTERIO_DEPENDENCY,
        "distributions": tuple(RASTERIO_DEPENDENCY["distributions"]),
        "usages": ("inference",),
    },
)

_SPECS_BY_KEY = {spec["key"]: spec for spec in DEEP_LEARNING_DEPENDENCIES}


def spec_by_key(key: str) -> dict | None:
    return _SPECS_BY_KEY.get(str(key))


def specs_for_usage(usage: str) -> tuple[dict, ...]:
    return tuple(spec for spec in DEEP_LEARNING_DEPENDENCIES if usage in spec["usages"])


def probe_script() -> str:
    return training_environment.probe_script(DEEP_LEARNING_DEPENDENCIES)


def build_probe_command(python: str, sam_src: str = "") -> list[str]:
    """探测命令；``sam_src`` 作为脚本第 1 个 argv 注入外部 Python 的 sys.path。"""
    return training_environment.build_probe_command(
        python,
        dependencies=DEEP_LEARNING_DEPENDENCIES,
        extra_sys_path=sam_src,
    )


def parse_probe_output(text: str, *, error: str = "") -> TrainingEnvironmentReport:
    return training_environment.parse_probe_output(
        text,
        error=error,
        dependencies=DEEP_LEARNING_DEPENDENCIES,
        label="深度学习",
    )


def build_install_commands(
    python: str,
    missing_keys: Iterable[str],
    *,
    torch_variant: str = "cpu",
) -> list[list[str]]:
    """缺失依赖 → pip 命令列表。

    - torch / torchvision 单独一组，带 PyTorch index；
    - 其余普通包（含 SAM1/SAM2 源码包与 rasterio）一次装完；
    - SAM3 先 ``--no-deps`` 安装，再补齐其运行时依赖，避免 numpy&lt;2 降级。
    """
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

    if {"torch", "torchvision"} & missing:
        args = [*base, "torch", "torchvision"]
        index = TORCH_INDEX_URLS.get(str(torch_variant or "cpu").lower())
        if index:
            args.extend(["--index-url", index])
        commands.append(args)

    others = [
        spec["requirement"]
        for key, spec in _SPECS_BY_KEY.items()
        if key in missing and key not in ("torch", "torchvision", "sam3")
    ]
    if others:
        commands.append([*base, *others])

    if "sam3" in missing:
        commands.append([*base, "--no-deps", _SPECS_BY_KEY["sam3"]["requirement"]])
        commands.append([*base, *SAM3_EXTRA_REQUIREMENTS])
    return commands


__all__ = [
    "ALL_USAGES",
    "DEEP_LEARNING_DEPENDENCIES",
    "USAGE_TITLES",
    "build_install_commands",
    "build_probe_command",
    "parse_probe_output",
    "probe_script",
    "spec_by_key",
    "specs_for_usage",
]
