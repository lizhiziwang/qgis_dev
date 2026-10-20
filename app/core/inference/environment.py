"""推理外部运行环境的依赖探测与一键安装。

推理与训练共用同一个外部 Python；额外需要 rasterio（自带 GDAL/PROJ 的
wheel），用于按窗口读写大图 GeoTIFF。探测/安装逻辑复用训练模块，只换
依赖表与提示文案。
"""

from __future__ import annotations

from collections.abc import Iterable

from app.core.training.environment import (
    TRAINING_DEPENDENCIES,
    TrainingDependencyStatus,
    TrainingEnvironmentReport,
)
from app.core.training.environment import (
    build_install_commands as _build_install_commands,
)
from app.core.training.environment import (
    build_probe_command as _build_probe_command,
)
from app.core.training.environment import (
    parse_probe_output as _parse_probe_output,
)
from app.core.training.environment import (
    probe_script as _probe_script,
)

#: 推理额外依赖：rasterio（``import rasterio``，发行名 ``rasterio``）
RASTERIO_DEPENDENCY: dict = {
    "key": "rasterio",
    "title": "Rasterio",
    "module": "rasterio",
    "distributions": ("rasterio",),
    "requirement": "rasterio>=1.4",
}

INFERENCE_DEPENDENCIES: tuple[dict, ...] = (
    *TRAINING_DEPENDENCIES,
    RASTERIO_DEPENDENCY,
)


def probe_script() -> str:
    return _probe_script(INFERENCE_DEPENDENCIES)


def build_probe_command(python: str) -> list[str]:
    return _build_probe_command(python, dependencies=INFERENCE_DEPENDENCIES)


def parse_probe_output(text: str, *, error: str = "") -> TrainingEnvironmentReport:
    return _parse_probe_output(
        text,
        error=error,
        dependencies=INFERENCE_DEPENDENCIES,
        label="推理",
    )


def build_install_commands(
    python: str,
    missing_keys: Iterable[str],
    *,
    torch_variant: str = "cpu",
) -> list[list[str]]:
    return _build_install_commands(
        python,
        missing_keys,
        torch_variant=torch_variant,
        dependencies=INFERENCE_DEPENDENCIES,
    )


__all__ = [
    "INFERENCE_DEPENDENCIES",
    "RASTERIO_DEPENDENCY",
    "TrainingDependencyStatus",
    "TrainingEnvironmentReport",
    "build_install_commands",
    "build_probe_command",
    "parse_probe_output",
    "probe_script",
]
