"""工具条线性图标（SVG，随主题变色）。

图标取自设计原型的统一线性图标集（24×24 viewBox、1.8 描边、圆头圆角），
放在 ``resource/icons/<key>.svg``，用 ``currentColor`` 占位描边色，渲染时
替换成当前主题色，浅色 / 深色 / 蓝色 / 绿色主题下自动适配。

- 路径经 :class:`Paths` 解析——开发态落到仓库 ``resource/``，打包态由
  bootstrap ``QGIS_DEV_PATHS_RESOURCE`` 指到便携包 ``resource/``；
  PyInstaller spec 已把 ``resource/`` 整目录打进包；
- 图标按 ``(key, color, size)`` 缓存，按目标逻辑尺寸 ×2 渲染（高 DPI 下发虚），
  主题切换时用新颜色重新生成（同 :mod:`app.ui.common.tool_icons`）；
- :func:`available` 用来判断某 key 是否有 SVG；没有的交给
  :func:`app.ui.common.tool_icons.icon` 回退到 Font Awesome。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from qgis.PyQt.QtCore import QByteArray, QRectF, Qt
from qgis.PyQt.QtGui import QIcon, QPainter, QPixmap
from qgis.PyQt.QtSvg import QSvgRenderer

from app.infra.fs.paths import Paths

_DEFAULT_SIZE = 20  # 默认逻辑像素边长（与工具条 iconSize 一致）
_DPR = 2.0          # 2x 渲染，高 DPI 下不发虚

_CACHE: dict[tuple[str, str, int], QIcon] = {}
_AVAIL: dict[str, bool] = {}


@lru_cache(maxsize=1)
def _icons_dir() -> Path:
    """``resource/icons`` 目录（只解析一次）。"""
    return Paths.from_env(Paths.default()).resource("icons")


#: app 图标 key → ``resource/icons`` 下实际文件名（同一原型图标被多个 app key 复用）
_ALIASES: dict[str, str] = {
    "condition_query": "query_dialog",
    "select_mask": "mask_selection",
    "unmask": "unmask_all",
    "coord_locate": "locate",
    "measure_line": "measure_distance",
    "extent_clip": "clip",
    # 「新增数据」复用「添加图层」的图层叠 + 加号图标（同为「往工程/盘上添一份数据」）
    "create_data": "add_layer",
}


def _path(key: str) -> Path:
    return _icons_dir() / f"{_ALIASES.get(key, key)}.svg"


def available(key: str) -> bool:
    """是否存在该 key 的 SVG 图标（结果缓存，避免反复读盘）。"""
    cached = _AVAIL.get(key)
    if cached is None:
        cached = _path(key).is_file()
        _AVAIL[key] = cached
    return cached


def _render(key: str, color: str, size: int) -> QIcon:
    text = _path(key).read_text(encoding="utf-8").replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(text.encode("utf-8")))
    icon = QIcon()
    # 同时提供 1x 与 2x 位图，交给 QIcon 按目标尺寸/DPR 选最合适的一张。
    # 注意：不能只塞一张 ``setDevicePixelRatio(2)`` 的高清位图——QIcon 在
    # 请求较小尺寸时会把它**裁切**成左上角一块（图标只显示部分内容）。
    for px in (int(size), int(size * _DPR)):
        pm = QPixmap(px, px)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        renderer.render(painter, QRectF(0, 0, px, px))
        painter.end()
        icon.addPixmap(pm)
    return icon


def icon(key: str, color: str, size: int = _DEFAULT_SIZE) -> QIcon:
    """按 key 渲染 SVG 图标，颜色取主题色，逻辑尺寸为 ``size``。

    同一 ``(key, color, size)`` 缓存；``size`` 直接决定渲染位图大小，
    调用方应传入与工具条 ``iconSize()`` 一致的值（避免二次缩放发虚）。

    无对应 SVG 时返回空 :class:`QIcon`（调用方按需回退）。
    """
    if not available(key):
        return QIcon()
    cache_key = (key, color, int(size))
    if cache_key not in _CACHE:
        _CACHE[cache_key] = _render(key, color, int(size))
    return _CACHE[cache_key]


__all__ = ["available", "icon"]
