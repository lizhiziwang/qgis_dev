"""构建元数据：运行期版本、构建戳与兼容指纹的唯一读取入口。

有两份数据来源：

- ``app/_build.py``：打包开始时由 ``tools/stamp_source.py`` 生成，随源码一起
  进入 PYZ（或编译成原生扩展），运行期**不依赖外部文件**；
- ``build_info.json``：PyInstaller 产物根下由 ``tools/stamp_build.py`` 写入，
  方便更新器 / 运维脚本在**不启动 Qt** 的情况下直接读。

本模块把两者合并成同一份字典。版本号本身以 ``config/app.yaml`` 的
``app.version`` 为准（经 ``app.__version__``；``build_info.json`` 只在安装器
保留了旧配置时用来补正版本，见 ``app/__init__.py``）。

Qt-free：``scripts/entry.py --version`` 会在导入 ``app.bootstrap`` 之前调用，
不能把 QGIS / PyQt 拖进来。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from app import APP_ID, CHANNEL, VERSION_CODE, __version__

BUILD_INFO_FILE = "build_info.json"

#: 没有构建戳时（源码直接运行）的占位值。
DEV_BUILD_ID = "dev"

_STAMP_FIELDS = {
    "BUILD_ID": "buildId",
    "BUILD_TIME": "buildTime",
    "GIT_REVISION": "git",
    "GIT_DIRTY": "dirty",
    "CHANNEL": "channel",
    "RUNTIME_ID": "runtimeId",
    "PYTHON_ABI": "pythonAbi",
    "QGIS_VERSION": "qgisVersion",
}

_TEXT_FIELDS = (
    "appId",
    "version",
    "versionCode",
    "buildId",
    "buildTime",
    "git",
    "dirty",
    "channel",
    "runtimeId",
    "pythonAbi",
    "qgisVersion",
)


def read_build_info(project_root: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """返回运行期构建元数据（所有值都是字符串，缺失项退化为 ``"?"``）。

    :param project_root: 便携包根 / 仓库根；``None`` 时按 :func:`build_info_path`
        的候选顺序自动定位 ``build_info.json``。
    """
    info: dict[str, str] = {
        "appId": APP_ID,
        "version": __version__,
        "versionCode": str(VERSION_CODE),
        "buildId": DEV_BUILD_ID,
        "buildTime": "?",
        "git": "?",
        "dirty": "False",
        "channel": CHANNEL,
        "runtimeId": "?",
        "pythonAbi": "?",
        "qgisVersion": "?",
    }
    # 源码直跑时不用 app/_build.py：它可能是上一次打包留下的，git 哈希会误导
    # 排查（traceback 的文件:行号对的是当前源码）。冻结态则以包内戳为准。
    if getattr(sys, "frozen", False):
        info.update(_stamp_values())
    info.update(_json_values(build_info_path(project_root)))
    # 版本事实源固定在 config/app.yaml（app.__version__）；build_info 只贡献构建戳。
    info["appId"] = APP_ID
    info["version"] = __version__
    info["versionCode"] = str(VERSION_CODE)
    return info


def build_info_path(project_root: str | os.PathLike[str] | None = None) -> Path | None:
    """定位 ``build_info.json``；找不到返回 ``None``。

    顺序：显式 ``QGIS_DEV_BUILD_INFO`` → ``project_root`` → 冻结态可执行文件
    目录 → 源码树根。后两者覆盖“源码直跑”和“便携包直接双击”两种常见布局。
    """
    candidates: list[Path] = []
    env = os.environ.get("QGIS_DEV_BUILD_INFO")
    if env:
        candidates.append(Path(env))
    if project_root is not None:
        candidates.append(Path(project_root) / BUILD_INFO_FILE)
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / BUILD_INFO_FILE)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / BUILD_INFO_FILE)
    candidates.append(Path(__file__).resolve().parents[1] / BUILD_INFO_FILE)

    for path in candidates:
        if path.is_file():
            return path
    return None


def as_json(info: dict[str, str], *, indent: int | None = None) -> str:
    """元数据 → JSON 文本（键序固定，中文不转义）。"""
    payload: dict[str, Any] = {key: info.get(key, "?") for key in _TEXT_FIELDS}
    try:
        payload["versionCode"] = int(info.get("versionCode", "0"))
    except (TypeError, ValueError):
        payload["versionCode"] = 0
    payload["dirty"] = str(info.get("dirty", "")).strip().lower() in {"true", "1", "yes"}
    payload["frozen"] = bool(getattr(sys, "frozen", False))
    return json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=False)


def _stamp_values() -> dict[str, str]:
    """读 ``app/_build.py``（由 tools/stamp_source.py 生成）；缺失返回空。"""
    try:
        from app import _build
    except Exception:  # noqa: BLE001 —— 源码直跑没有该模块，属于正常情况
        return {}
    values: dict[str, str] = {}
    for attr, key in _STAMP_FIELDS.items():
        value = getattr(_build, attr, None)
        if value is not None:
            values[key] = _text(value)
    return values


def _json_values(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    values: dict[str, str] = {}
    for key in _TEXT_FIELDS:
        if key in raw and raw[key] is not None:
            values[key] = _text(raw[key])
    return values


def _text(value: Any) -> str:
    text = str(value).strip()
    return text or "?"


__all__ = [
    "BUILD_INFO_FILE",
    "DEV_BUILD_ID",
    "as_json",
    "build_info_path",
    "read_build_info",
]
