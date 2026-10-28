"""无人机航测向导的 QSettings 记忆（键前缀 ``uav/stitch``）。"""

from __future__ import annotations

from collections.abc import Mapping

_SETTINGS_ORG = "qgis_dev"
_SETTINGS_APP = "qgis_dev"

SETTING_PREFIX = "uav/stitch"
ENGINE_PREFIX = "uav/engine"

#: 需要跨会话记忆的字段
FIELDS: tuple[str, ...] = (
    "image_dir",
    "position_file",
    "gcp_file",
    "output_dir",
    "output_format",
    "crs_epsg",
    "preset",
    "gsd_mode",
    "gsd_cm",
)

#: 引擎设置字段
ENGINE_FIELDS: tuple[str, ...] = (
    "python",
    "root",
    "manifest_url",
    "bundled_root",
)


def uav_setting_key(field: str) -> str:
    return f"{SETTING_PREFIX}/{field}"


def uav_engine_key(field: str) -> str:
    return f"{ENGINE_PREFIX}/{field}"


def load_settings() -> dict[str, object]:
    """读取上次使用的参数；没有 Qt（纯配置单测）时返回空字典。"""
    try:
        from qgis.PyQt.QtCore import QSettings
    except Exception:  # noqa: BLE001 —— 无 Qt 的测试环境
        return {}
    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    return {field: settings.value(uav_setting_key(field), "") for field in FIELDS}


def save_settings(values: Mapping[str, object]) -> None:
    try:
        from qgis.PyQt.QtCore import QSettings
    except Exception:  # noqa: BLE001
        return
    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    for field in FIELDS:
        if field in values:
            settings.setValue(uav_setting_key(field), values[field])
    settings.sync()


def load_engine_settings() -> dict[str, str]:
    """读取拼接引擎设置；无 Qt 时返回空字典。"""
    try:
        from qgis.PyQt.QtCore import QSettings
    except Exception:  # noqa: BLE001
        return {}
    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    return {
        field: str(settings.value(uav_engine_key(field), "") or "")
        for field in ENGINE_FIELDS
    }


def save_engine_settings(values: Mapping[str, object]) -> None:
    try:
        from qgis.PyQt.QtCore import QSettings
    except Exception:  # noqa: BLE001
        return
    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    for field in ENGINE_FIELDS:
        if field in values:
            settings.setValue(uav_engine_key(field), values[field])
    settings.sync()


def uav_engine_state_dir():
    """组件安装状态目录（延迟导入 Paths，避免纯测试依赖）。"""
    from app.infra.fs.paths import Paths  # noqa: PLC0415

    paths = Paths.default()
    paths.ensure()
    return paths.config / "state" / "uav_engine"


def uav_bundled_engine_dir():
    """随软件附带的引擎目录（打包时可放 ``runtime/uav_engine``）。"""
    from app.infra.fs.paths import Paths  # noqa: PLC0415

    return Paths.default().project_root / "runtime" / "uav_engine"


def resolve_engine(*, probe: bool = False):
    """按 QSettings / 环境变量 / 已安装组件 / 附带目录查找引擎。"""
    from app.core.uav.engine import discover_engine  # noqa: PLC0415

    settings = load_engine_settings()
    state_dir = uav_engine_state_dir()
    return discover_engine(
        config_dir=state_dir.parent.parent,
        settings_python=settings.get("python", ""),
        settings_root=settings.get("root", ""),
        bundled_root=settings.get("bundled_root", "") or uav_bundled_engine_dir(),
        probe=probe,
    )


__all__ = [
    "ENGINE_FIELDS",
    "FIELDS",
    "SETTING_PREFIX",
    "load_engine_settings",
    "load_settings",
    "resolve_engine",
    "save_engine_settings",
    "save_settings",
    "uav_bundled_engine_dir",
    "uav_engine_key",
    "uav_engine_state_dir",
    "uav_setting_key",
]
