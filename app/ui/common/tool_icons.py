"""工具条图标（Font Awesome Free 5 Solid 渲染，随主题变色）。

图标用随应用分发的 Font Awesome Free 5 Solid 字体（OFL-1.1，见
``resource/fonts/README.txt``）绘制，现代扁平实心风格，颜色取当前主题的
``toolbar_text``，浅色 / 深色 / 蓝色 / 绿色主题下自动适配。

- 字体文件：``resource/fonts/fa-solid-900.ttf``。路径经 :class:`Paths`
  解析——开发态落到仓库 ``resource/``，打包态由 bootstrap
  ``QGIS_DEV_PATHS_RESOURCE`` 指到便携包 ``resource/``（见
  :func:`app.bootstrap._ensure_portable_env`），PyInstaller spec 已把
  ``resource/`` 整目录打进包；
- 图标按 ``(key, color)`` 缓存；主题切换时用新颜色重新生成即可（见
  :meth:`ToolBar.apply_theme`）。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QRect, Qt
from qgis.PyQt.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap

_LOGICAL = 36        # 逻辑像素边长（与工具条 iconSize 一致）
_DPR = 2.0           # 2x 渲染，高 DPI 下图标不发虚
_GLYPH_PX = int(_LOGICAL * 0.8)  # 字形像素尺寸（约留 10% 边距，图标不顶满）

_FONT_FAMILY = "Font Awesome 5 Free Solid"

# key -> Font Awesome 5 Solid 码位（PUA，U+F000 区段）
_GLYPHS: dict[str, str] = {
    "pan": "",                 # arrows-alt 四向移动
    "select_point": "",       # mouse-pointer 点选
    "select_box": "",         # vector-square 框选
    "clear_selection": "",    # times-circle 取消选择（清空全部选择）
    "identify": "",            # search 放大镜
    "zoom_in": "",             # search-plus 放大镜+加号
    "zoom_out": "",            # search-minus 放大镜+减号
    "quality": "",             # shield-alt 盾牌
    "analyse": "",             # chart-bar 柱状图
    "zoom_full": "",           # globe 地球（全图）
    "refresh": "",             # sync 环形刷新
    "test": "",                # flask 烧瓶
    "swipe": "",              # columns 竖向分割（卷帘对比）
    "qmap_compare": "",       # object-group 图层组（地图对比：分割/放大镜/镜像）
    "online_basemap": "",     # map 地图轮廓（在线底图，避免与 zoom_full 的 globe 撞）
    "data_manage": "",       # database 数据库（数据管理）
    "offline_download": "",   # download 下载箭头（离线下载）
    "space_query_point": "",   # map-marker 定位大头针
    "space_query_line": "",    # route 路线（线查询）
    "space_query_polygon": "", # draw-polygon 多边形（面查询）
    # ---- 流程节点编排工具（app/ui/flow_editor）----
    "play": "",           # play 运行全部
    "step": "",           # step-forward 单步执行
    "undo": "",           # undo 重置参数
    "sitemap": "",        # sitemap 拓扑校验（依赖关系图）
    "link": "",           # link 高亮依赖链
    "chevron_up": "",     # chevron-up 上一级
    "chevron_down": "",   # chevron-down 下一级
    "trash": "",          # trash 删除选中
    "file": "",           # file 新建流程
    "folder_open": "",    # folder-open 打开流程
    "save": "",           # save 保存
    "archive": "",        # archive 另存为
    "expand": "",         # expand-arrows-alt 适应窗口
}

_CACHE: dict[tuple[str, str], QIcon] = {}

_font_loaded = False
_font_ok = False


def _ensure_font() -> bool:
    """注册 FA 字体（只做一次），返回是否可用。"""
    global _font_loaded, _font_ok
    if _font_loaded:
        return _font_ok
    _font_loaded = True

    from app.infra.fs.paths import Paths

    font_path = Paths.from_env(Paths.default()).resource("fonts", "fa-solid-900.ttf")
    if not font_path.is_file():
        return False
    fid = QFontDatabase.addApplicationFont(str(font_path))
    _font_ok = fid >= 0
    return _font_ok


def _glyph_font() -> QFont:
    f = QFont(_FONT_FAMILY, 100)  # pixelSize 精确定位，字号 100 只是占位
    f.setPixelSize(_GLYPH_PX)
    f.setWeight(QFont.Weight.Bold)       # FA Solid 是 weight 900
    return f


def _render(glyph: str, color: QColor) -> QIcon:
    pm = QPixmap(int(_LOGICAL * _DPR), int(_LOGICAL * _DPR))
    pm.setDevicePixelRatio(_DPR)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if _ensure_font():
        painter.setPen(color)
        painter.setFont(_glyph_font())
        painter.drawText(QRect(0, 0, _LOGICAL, _LOGICAL), Qt.AlignmentFlag.AlignCenter, glyph)
    else:
        # 兜底：字体缺失时画一个实心圆点，避免空白按钮
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRect(int(_LOGICAL * 0.3), int(_LOGICAL * 0.3),
                                  int(_LOGICAL * 0.4), int(_LOGICAL * 0.4)))
    painter.end()
    return QIcon(pm)


def icon(key: str, color: str) -> QIcon:
    """按 key 画图标，颜色取主题 toolbar_text；同一 (key, color) 缓存。"""
    if key not in _GLYPHS:
        raise KeyError(f"未知工具图标: {key}")
    cache_key = (key, color)
    if cache_key not in _CACHE:
        _CACHE[cache_key] = _render(_GLYPHS[key], QColor(color))
    return _CACHE[cache_key]


__all__ = ["icon"]
