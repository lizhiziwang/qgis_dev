"""最近打开的工程列表（``QSettings`` 持久化）。

启动时「打开地图文档」对话框右侧列的“最近打开”数据源；保存 / 打开工程成功
后由 :class:`~app.ui.main_window.mixins.project_ops.ProjectOpsMixin` 写入。
与 :mod:`app.ui.common.theme` 相同，用 ``QSettings("qgis_dev", "qgis_dev")``
落盘，Windows 下即注册表 ``HKCU\\Software\\qgis_dev\\qgis_dev``，避免额外
维护配置文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from qgis.PyQt.QtCore import QSettings

#: 与 ThemeManager 共用同一组 QSettings 标识
_ORG = "qgis_dev"
_APP = "qgis_dev"
#: 存储键：工程绝对路径列表（新→旧）
_KEY = "projects/recent"
#: 最多保留条数
MAX_ITEMS = 10


def _settings() -> QSettings:
    return QSettings(_ORG, _APP)


def load_recent() -> list[str]:
    """读取最近工程列表（新→旧）。

    去重保序，并剔除已不存在的文件（临时目录里的工程被删后不再出现）；没有任何
    有效项时返回空列表。``QSettings`` 在只有一个元素时会退化成 ``str``，这里
    统一成列表。
    """
    raw: object = _settings().value(_KEY, [])
    if raw is None:
        items: list = []
    elif isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, Iterable):
        items = list(raw)
    else:  # 理论上不会走到
        items = []
    return _clean(items)


def _clean(items: Iterable[object]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        path = str(item or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            if Path(path).is_file():
                out.append(path)
        except OSError:
            continue
    return out


def _write(items: list[str]) -> None:
    settings = _settings()
    settings.setValue(_KEY, items[:MAX_ITEMS])
    settings.sync()


def add_recent(path: str | Path) -> None:
    """把 ``path`` 置顶写入最近列表（已存在则先移除再插到最前）。"""
    if not path:
        return
    target = str(Path(path).resolve())
    items = [p for p in load_recent() if p != target]
    items.insert(0, target)
    _write(items)


def remove_recent(path: str | Path) -> None:
    """从最近列表中移除 ``path``（不存在时静默）。"""
    if not path:
        return
    target = str(Path(path).resolve())
    _write([p for p in load_recent() if p != target])


def clear_recent() -> None:
    """清空最近列表。"""
    _settings().remove(_KEY)
    _settings().sync()


__all__ = [
    "MAX_ITEMS",
    "add_recent",
    "clear_recent",
    "load_recent",
    "remove_recent",
]
