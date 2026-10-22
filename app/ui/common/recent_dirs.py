"""「最近打开目录」列表（``QSettings`` 持久化）。

「添加数据」弹窗左栏的“最近打开目录”数据源。与
:mod:`app.ui.common.recent_projects` 同一套落盘方式（``QSettings("qgis_dev",
"qgis_dev")``），但存的是**目录**：

- 用户在弹窗里成功添加数据后，由弹窗把当前目录置顶写入；
- 读取时剔除已不存在的目录（移动硬盘拔掉、临时目录被删）；
- 去重按 ``os.path.normcase`` 比较，Windows 下 ``D:/data`` 与 ``d:\\data`` 视为同一项，
  但**展示 / 回填保留用户原来的写法**。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from qgis.PyQt.QtCore import QSettings

_ORG = "qgis_dev"
_APP = "qgis_dev"
#: 存储键：目录绝对路径列表（新→旧）
_KEY = "data/recent_dirs"
#: 最多保留条数（左栏高度有限，太多反而难找）
MAX_ITEMS = 12


def _settings() -> QSettings:
    return QSettings(_ORG, _APP)


def _norm(path: str) -> str:
    """比较用的规范形式：绝对路径 + 平台大小写规则（不解析符号链接）。"""
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def load_recent_dirs() -> list[str]:
    """读取最近目录（新→旧），剔除已不存在 / 重复项。"""
    raw: object = _settings().value(_KEY, [])
    if raw is None:
        items: list = []
    elif isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, Iterable):
        items = list(raw)
    else:
        items = []
    return _clean(items)


def _clean(items: Iterable[object]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        path = str(item or "").strip()
        if not path:
            continue
        key = _norm(path)
        if key in seen:
            continue
        try:
            if not Path(path).is_dir():
                continue
        except OSError:
            continue
        seen.add(key)
        out.append(path)
    return out


def _write(items: list[str]) -> None:
    settings = _settings()
    settings.setValue(_KEY, items[:MAX_ITEMS])
    settings.sync()


def add_recent_dir(path: str | Path) -> None:
    """把 ``path`` 置顶写入最近目录（已存在则先移除再插到最前）。"""
    path = str(path or "").strip()
    if not path:
        return
    try:
        if not Path(path).is_dir():
            return
    except OSError:
        return
    target = _norm(path)
    items = [p for p in load_recent_dirs() if _norm(p) != target]
    items.insert(0, os.path.abspath(os.path.expanduser(path)))
    _write(items)


def remove_recent_dir(path: str | Path) -> None:
    """从最近目录中移除 ``path``（不存在时静默）。"""
    path = str(path or "").strip()
    if not path:
        return
    target = _norm(path)
    _write([p for p in load_recent_dirs() if _norm(p) != target])


def clear_recent_dirs() -> None:
    """清空最近目录。"""
    _settings().remove(_KEY)
    _settings().sync()


__all__ = [
    "MAX_ITEMS",
    "add_recent_dir",
    "clear_recent_dirs",
    "load_recent_dirs",
    "remove_recent_dir",
]
