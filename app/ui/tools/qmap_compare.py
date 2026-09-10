"""地图对比组件（参照 QGIS 插件 qmapcompare 的实现思路）。

与 :mod:`app.ui.tools.swipe`（卷帘，双影像合成）互补，提供三种对比模式
（垂直 / 水平分割支持鼠标拖动分隔条调整比例）：

1. 垂直 / 水平分割（mask 方案）
   图层树顶层插入对比组 ``QMapCompare_Group``（QgsGroupLayer），组内包含：

   - 遮罩层（QMapCompareMask）：geometry generator 生成半边矩形，
     InvertedPolygonRenderer 渲染矩形外部，blend 模式 DestinationIn
     → 分割线一侧露出主画布、另一侧显示组内对比图层；
   - 白色背景层（QMapCompareBackground）：填充被遮罩挖掉的区域；
   - 用户勾选的对比图层（从原位移入组内，停止时还原）。
   分割线默认在画布中点，可鼠标拖动调整比例（:class:`SplitDividerOverlay`）：
   拖动条带 → 重建遮罩 geometry generator 表达式 → 动态调整分割比例。
   分隔条不画通栏线，只在分割线中点画一枚主题配色的「胶囊抓手」
   （``app.ui.common.theme.divider_colors``），切主题自动跟随。

2. 放大镜（Lens，mask 方案变体）
   遮罩几何为跟随 ``@canvas_cursor_point`` 的圆 / 方形，mask 层
   ``ReloadData`` 自动刷新（200ms）跟随鼠标。

3. 镜像分屏（Mirror）
   在右侧 dock 新建第二个 QgsMapCanvas 显示对比图层集合，与主画布
   双向同步中心点与缩放（递归保护标志）。对比期间主画布临时隐藏对比
   图层（停止时还原可见性），两画布内容互补。

实现要点（已在 QGIS 4.2 逐项验证）：
- mask 方案的合成依赖 QgsGroupLayer 组节点 + DestinationIn blend；
  GUI 画布渲染会自动提供 ``@map_extent_*`` / ``@canvas_cursor_point``
  表达式变量（离屏渲染需手动注入，见 ``offscreen_render`` 测试辅助）。
- ``make_rectangle_3points`` 的"高"取第三点到第二点的欧氏距离，
  因此水平分割的第三点必须放在 p2 正上方（qmapcompare 原实现此处
  有 bug：第三点放在 p1 正上方，矩形高度变为 sqrt(w^2+(h/2)^2)
  超出视口导致分割失效，本实现已修正）。
- 遵守 qgis_dev 约束：不在 Qt 事件栈内调用 canvas.refresh()；
  镜像初始渲染用 QTimer.singleShot(0, ...) 推迟到事件栈外。
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Optional

from qgis.PyQt.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer
from qgis.PyQt.QtGui import QPainter, QPen
from qgis.PyQt.QtWidgets import QDockWidget, QWidget
from qgis.core import (
    Qgis,
    QgsCoordinateTransformContext,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFillSymbol,
    QgsGeometryGeneratorSymbolLayer,
    QgsGroupLayer,
    QgsInvertedPolygonRenderer,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsMapLayer,
    QgsProject,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas, QgsMapToolPan

from app.ui.common.theme import ThemeManager, divider_colors

if TYPE_CHECKING:  # pragma: no cover
    from qgis.PyQt.QtGui import QColor
    from qgis.PyQt.QtWidgets import QMainWindow

# ---------------------------------------------------------------------------
# 常量（与 qmapcompare 保持一致，便于对照源码）
# ---------------------------------------------------------------------------
COMPARE_GROUP_NAME = "QMapCompare_Group"
MASK_LAYER_NAME = "QMapCompareMask"
BACKGROUND_LAYER_NAME = "QMapCompareBackground"
MIRROR_DOCK_TITLE = "分屏对比"

LENS_DEFAULT_SIZE_RATE = 0.15
LENS_MIN_SIZE_RATE = 0.05
LENS_MAX_SIZE_RATE = 0.40
LENS_AUTO_REFRESH_INTERVAL_MS = 200

#: 垂直 / 水平分割比例（0~1：垂直从左算、水平从上算，画布占比）
SPLIT_DEFAULT_RATIO = 0.5
#: 拖动分隔条的比例上下限（拖到贴边会抓不住，留一点余量）
SPLIT_MIN_RATIO = 0.02
SPLIT_MAX_RATIO = 0.98
#: 分隔条绘制 / 命中参数（逻辑像素）。方案 D：**不画通栏分割线**，
#: 只在分割线中点画一枚居中「胶囊抓手」（圆角矩形 + 双向箭头）。
SPLIT_STRIP_SIZE = 24  # 条带（鼠标命中区）宽度：抓手居中于条带
SPLIT_GRIP_W = 22  # 抓手横宽（垂直分割时）
SPLIT_GRIP_H = 40  # 抓手纵长（垂直分割时；水平分割与之互换）
SPLIT_GRIP_RADIUS = 9  # 抓手圆角半径
SPLIT_GRIP_BORDER = 2  # 抓手描边宽
SPLIT_GRIP_CHEVRON = 6  # 抓手内单枚箭头的大小
SPLIT_GRIP_CHEVRON_GAP = 4  # 两枚箭头到中心的距离


def get_split_geometry(
    method: str = "vertical", ratio: float = SPLIT_DEFAULT_RATIO
) -> str:
    """生成垂直 / 水平分割遮罩的 geometry generator 表达式。

    :param method: ``"vertical"``（掩掉分割线右侧）/ ``"horizontal"``
        （掩掉分割线下方）
    :param ratio: 分割线位置（0~1：垂直从左算、水平从上算）

    ``ratio`` 直接以字面量写进表达式，拖动时重建——不依赖额外的画布
    表达式变量，离屏渲染（``offscreen_render``）也无需注入新变量。
    """
    ratio = max(SPLIT_MIN_RATIO, min(SPLIT_MAX_RATIO, ratio))
    if method == "horizontal":
        # 水平：屏幕上边 = ymax，ratio 从上算 → split_y = ymax - h * ratio
        split_y = (
            "y(@map_extent_center) + (@map_extent_height / 2)"
            f" - (@map_extent_height * {ratio!r})"
        )
        return f"""make_rectangle_3points(
        make_point(x(@map_extent_center) - (@map_extent_width / 2), {split_y}),
        make_point(x(@map_extent_center) + (@map_extent_width / 2), {split_y}),
        make_point(x(@map_extent_center) + (@map_extent_width / 2), y(@map_extent_center) - (@map_extent_height / 2)),
        0)"""
    # 垂直：分割线左侧保留主画布、右侧掩掉显示对比图层
    split_x = (
        "x(@map_extent_center) - (@map_extent_width / 2)"
        f" + (@map_extent_width * {ratio!r})"
    )
    return f"""make_rectangle_3points(
        make_point({split_x}, y(@map_extent_center) - (@map_extent_height / 2)),
        make_point({split_x}, y(@map_extent_center) + (@map_extent_height / 2)),
        make_point(x(@map_extent_center) + (@map_extent_width / 2), y(@map_extent_center) + (@map_extent_height / 2)),
        0)"""


# 默认（0.5 居中）分割表达式，供外部 / 测试引用
VERTICAL_SPLIT_GEOMETRY = get_split_geometry("vertical", SPLIT_DEFAULT_RATIO)
# 水平分割修正 qmapcompare 原实现：第三点必须放 p2 正上方，否则高
# = sqrt(w^2+(h/2)^2) 超出视口、分割失效。
HORIZONTAL_SPLIT_GEOMETRY = get_split_geometry("horizontal", SPLIT_DEFAULT_RATIO)

# 背景层：整个视口范围（InvertedPolygonRenderer 填白）
BACKGROUND_GEOMETRY = "@map_extent"


def get_lens_geometry(
    shape: str = "circle", rate: float = LENS_DEFAULT_SIZE_RATE
) -> str:
    """生成放大镜遮罩的 geometry generator 表达式。"""
    rate = max(LENS_MIN_SIZE_RATE, min(LENS_MAX_SIZE_RATE, rate))
    if shape == "square":
        cx = "x(@canvas_cursor_point)"
        cy = "y(@canvas_cursor_point)"
        offset = f"@map_extent_width * {rate}"
        bottom_left = f"make_point({cx} - {offset}, {cy} - {offset})"
        top_left = f"make_point({cx} - {offset}, {cy} + {offset})"
        top_right = f"make_point({cx} + {offset}, {cy} + {offset})"
        return f"make_rectangle_3points({bottom_left}, {top_left}, {top_right}, 0)"
    return f"buffer(@canvas_cursor_point, @map_extent_width * {rate})"


class SplitDividerOverlay(QWidget):
    """画布上的可拖动分隔条（垂直 / 水平分割模式）。

    仓库既定覆盖层模式（见 :class:`app.ui.tools.swipe.SwipeOverlay`）：作为
    canvas 子 QWidget 浮在画布上。与卷帘覆盖层不同，这里是**窄条带**且
    自身接收鼠标事件——只有条带（``SPLIT_STRIP_SIZE`` px）拦截鼠标，其余
    区域仍可正常平移 / 缩放。按住条带拖动 → 回调 ``on_ratio(ratio)``
    重建遮罩几何，动态调整分割比例。

    绘制（方案 D）：**不画通栏分割线**，只在分割线中点画一枚居中的
    「胶囊抓手」（圆角矩形 + 两枚指示拖拽方向的箭头），其余露出画布渲染
    结果。抓手配色取自全局主题
    （:func:`app.ui.common.theme.divider_colors`），不写死黑白，切主题自动重绘。
    """

    def __init__(self, canvas, orientation: str, on_ratio, parent=None) -> None:
        super().__init__(canvas)
        self._canvas = canvas
        self._orientation = orientation
        self._on_ratio = on_ratio
        self._ratio = SPLIT_DEFAULT_RATIO
        self._dragging = False
        if orientation == "vertical":
            self.setCursor(Qt.CursorShape.SplitHCursor)
        else:
            self.setCursor(Qt.CursorShape.SplitVCursor)
        # 透明背景：只画分割线 + 手柄，其余露出画布渲染结果
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._reposition()
        canvas.installEventFilter(self)
        with contextlib.suppress(RuntimeError):
            canvas.viewport().installEventFilter(self)
        self._canvas_signal = False
        try:
            canvas.mapCanvasRefreshed.connect(self._on_canvas_refreshed)
        except (AttributeError, RuntimeError):
            pass
        else:
            self._canvas_signal = True
        # 配色走全局主题（QPainter 直绘不吃 QSS），切主题自动重绘
        self._theme_signal = False
        try:
            ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        except (AttributeError, RuntimeError):
            pass
        else:
            self._theme_signal = True

    # ------------------------------------------------------------ 状态 / 几何

    def set_ratio(self, ratio: float) -> None:
        """设置分割比例（0~1）并重定位条带。"""
        self._ratio = max(SPLIT_MIN_RATIO, min(SPLIT_MAX_RATIO, ratio))
        self._reposition()
        self.update()

    def ratio(self) -> float:
        return self._ratio

    def divider_px(self) -> int:
        """分割线在当前画布尺寸下的像素位置（垂直：x；水平：y）。"""
        parent = self.parentWidget()
        if parent is None:
            return 0
        if self._orientation == "vertical":
            return int(self._ratio * parent.width())
        return int(self._ratio * parent.height())

    def _reposition(self) -> None:
        """按比例把条带摆到分割线处（条带居中于分割线，越界时夹回画布内）。"""
        parent = self.parentWidget()
        if parent is None:
            return
        w, h = parent.width(), parent.height()
        if w <= 0 or h <= 0:
            return
        if self._orientation == "vertical":
            strip = max(1, min(SPLIT_STRIP_SIZE, w))
            x = max(0, min(self.divider_px() - strip // 2, w - strip))
            self.setGeometry(QRect(x, 0, strip, h))
        else:
            strip = max(1, min(SPLIT_STRIP_SIZE, h))
            y = max(0, min(self.divider_px() - strip // 2, h - strip))
            self.setGeometry(QRect(0, y, w, strip))
        self.raise_()

    # ------------------------------------------------------------ 事件

    def eventFilter(self, watched, event) -> bool:
        """画布 / viewport 尺寸变化时重定位条带。"""
        if event.type() == QEvent.Type.Resize:
            self._reposition()
        return super().eventFilter(watched, event)

    def _on_canvas_refreshed(self) -> None:
        """画布重绘后重新置顶（底层渲染可能盖住覆盖层）。"""
        self._reposition()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            self._apply_cursor(event.position().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _apply_cursor(self, local_pos: QPoint) -> None:
        """条带内坐标 → 画布坐标 → 归一化比例，回调更新遮罩。"""
        parent = self.parentWidget()
        if parent is None:
            return
        canvas_pt = self.mapTo(parent, local_pos)
        if self._orientation == "vertical":
            ratio = canvas_pt.x() / max(1, parent.width())
        else:
            ratio = canvas_pt.y() / max(1, parent.height())
        self._on_ratio(ratio)

    # ------------------------------------------------------------ 绘制

    def paintEvent(self, event) -> None:
        grip_fill, grip_border = divider_colors(ThemeManager.instance().current())
        painter = QPainter(self)
        try:
            w, h = self.width(), self.height()
            if w <= 0 or h <= 0:
                return
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            if self._orientation == "vertical":
                cx = self.divider_px() - self.x()
                cy = h // 2
                grip = QRect(
                    cx - SPLIT_GRIP_W // 2,
                    cy - SPLIT_GRIP_H // 2,
                    SPLIT_GRIP_W,
                    SPLIT_GRIP_H,
                )
                chevrons_horizontal = True
            else:
                cx = w // 2
                cy = self.divider_px() - self.y()
                grip = QRect(
                    cx - SPLIT_GRIP_H // 2,
                    cy - SPLIT_GRIP_W // 2,
                    SPLIT_GRIP_H,
                    SPLIT_GRIP_W,
                )
                chevrons_horizontal = False
            pen = QPen(grip_border)
            pen.setWidth(SPLIT_GRIP_BORDER)
            painter.setPen(pen)
            painter.setBrush(grip_fill)
            painter.drawRoundedRect(grip, SPLIT_GRIP_RADIUS, SPLIT_GRIP_RADIUS)
            self._draw_chevrons(painter, cx, cy, chevrons_horizontal, grip_border)
        finally:
            painter.end()

    @staticmethod
    def _draw_chevrons(painter: QPainter, cx: int, cy: int, horizontal: bool, color: QColor) -> None:
        """在抓手中心画两枚箭头，指向可拖拽的左右（或上下）方向。"""
        pen = QPen(color)
        pen.setWidth(SPLIT_GRIP_BORDER)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        gap = SPLIT_GRIP_CHEVRON_GAP
        size = SPLIT_GRIP_CHEVRON
        if horizontal:
            painter.drawPolyline(
                [QPoint(cx - gap, cy - size), QPoint(cx - gap - size, cy), QPoint(cx - gap, cy + size)]
            )
            painter.drawPolyline(
                [QPoint(cx + gap, cy - size), QPoint(cx + gap + size, cy), QPoint(cx + gap, cy + size)]
            )
        else:
            painter.drawPolyline(
                [QPoint(cx - size, cy - gap), QPoint(cx, cy - gap - size), QPoint(cx + size, cy - gap)]
            )
            painter.drawPolyline(
                [QPoint(cx - size, cy + gap), QPoint(cx, cy + gap + size), QPoint(cx + size, cy + gap)]
            )

    def _on_theme_changed(self, _theme=None) -> None:
        """主题切换 → 重绘分隔条抓手（配色取自全局主题）。"""
        self.update()

    # ------------------------------------------------------------ 清理

    def teardown(self) -> None:
        """断开画布信号 / 事件过滤器（stop 时调用）。"""
        with contextlib.suppress(RuntimeError):
            self._canvas.removeEventFilter(self)
            self._canvas.viewport().removeEventFilter(self)
        if getattr(self, "_canvas_signal", False):
            with contextlib.suppress(TypeError, RuntimeError):
                self._canvas.mapCanvasRefreshed.disconnect(self._on_canvas_refreshed)
        if getattr(self, "_theme_signal", False):
            with contextlib.suppress(TypeError, RuntimeError):
                ThemeManager.instance().theme_changed.disconnect(self._on_theme_changed)


# ---------------------------------------------------------------------------
# mask 方案：垂直/水平分割 + 放大镜
# ---------------------------------------------------------------------------
class CompareMask(QObject):
    """mask 方案地图对比会话（垂直/水平分割 + 放大镜）。

    用法::

        session = CompareMask(project)
        session.start(layers, "vertical")   # / "horizontal" / "lens"
        session.stop()
        session.is_active
    """

    def __init__(
        self,
        project: Optional[QgsProject] = None,
        parent: QObject | None = None,
        canvas: QgsMapCanvas | None = None,
    ) -> None:
        super().__init__(parent)
        self._project = project or QgsProject.instance()
        self._canvas = canvas
        self._group_node: Optional[QgsLayerTreeGroup] = None
        self._group_layer: Optional[QgsGroupLayer] = None
        self._mask_layer: Optional[QgsVectorLayer] = None
        self._background_layer: Optional[QgsVectorLayer] = None
        #: 遮罩 geometry generator（分割模式拖动时改表达式）
        self._generator: QgsGeometryGeneratorSymbolLayer | None = None
        #: 可拖动分隔条覆盖层（仅垂直/水平分割 + 有画布时创建）
        self._overlay: SplitDividerOverlay | None = None
        self._split_ratio = SPLIT_DEFAULT_RATIO
        # 对比图层移动记录：(原父节点, 原索引, 图层)
        self._moved: list[tuple[QgsLayerTreeGroup, int, QgsMapLayer]] = []
        self._method: Optional[str] = None

    # ------------------------------------------------------------------
    @property
    def is_active(self) -> bool:
        return self._group_node is not None

    @property
    def method(self) -> Optional[str]:
        return self._method

    @property
    def split_ratio(self) -> float:
        """当前垂直 / 水平分割比例（0~1）；非分割模式恒为默认值。"""
        return self._split_ratio

    # ------------------------------------------------------------------
    def start(
        self,
        compare_layers: list[QgsMapLayer],
        method: str,
        lens_shape: str = "circle",
        lens_size_rate: float = LENS_DEFAULT_SIZE_RATE,
    ) -> bool:
        """开启 mask 对比会话。

        :param compare_layers: 对比图层（移入对比组，停止时还原原位）
        :param method: ``"vertical"`` / ``"horizontal"`` / ``"lens"``
        """
        self.stop()  # 幂等：先清理残留会话
        if not compare_layers:
            return False
        project = self._project
        root = project.layerTreeRoot()

        # 1) 遮罩层 + 组图层（组内渲染遮罩）
        mask_layer = QgsVectorLayer(
            f"Polygon?crs={project.crs().authid()}", MASK_LAYER_NAME, "memory"
        )
        if not mask_layer.isValid():
            return False
        project.addMapLayer(mask_layer, False)
        options = QgsGroupLayer.LayerOptions(QgsCoordinateTransformContext())
        group_layer = QgsGroupLayer("group", options)
        group_layer.setChildLayers([mask_layer])
        project.addMapLayer(group_layer, False)

        group_node = QgsLayerTreeGroup(COMPARE_GROUP_NAME)
        group_node.setGroupLayer(group_layer)
        group_node.addLayer(mask_layer)
        root.insertChildNode(0, group_node)

        # 2) 对比图层移入组（记录原位，停止时还原）
        for layer in compare_layers:
            self._move_into_group(layer, group_node)

        # 3) 白色背景层：填充遮罩外区域
        background_layer = QgsVectorLayer(
            "Polygon?crs=EPSG:3857", BACKGROUND_LAYER_NAME, "memory"
        )
        project.addMapLayer(background_layer, False)
        group_node.addLayer(background_layer)
        bg_generator = QgsGeometryGeneratorSymbolLayer.create(
            {
                "geometryModifier": BACKGROUND_GEOMETRY,
                "geometry_type": 2,  # Polygon
                "extent": "",
                "color": "white",
                "outline_style": "no",
            }
        )
        bg_symbol = QgsFillSymbol.createSimple({})
        bg_symbol.changeSymbolLayer(0, bg_generator)
        background_layer.setRenderer(
            QgsInvertedPolygonRenderer(QgsSingleSymbolRenderer(bg_symbol))
        )

        # 4) 遮罩渲染器：半边矩形 / 放大镜几何 + 反转 + DestinationIn
        self._split_ratio = SPLIT_DEFAULT_RATIO
        geometry_formula = get_split_geometry("vertical", self._split_ratio)
        if method == "horizontal":
            geometry_formula = get_split_geometry("horizontal", self._split_ratio)
        elif method == "lens":
            geometry_formula = get_lens_geometry(lens_shape, lens_size_rate)
        generator = QgsGeometryGeneratorSymbolLayer.create(
            {
                "geometryModifier": geometry_formula,
                "geometry_type": 2,  # Polygon
                "extent": "",
                "color": "white",
                "outline_style": "no",
            }
        )
        symbol = QgsFillSymbol.createSimple({})
        symbol.changeSymbolLayer(0, generator)
        mask_layer.setRenderer(
            QgsInvertedPolygonRenderer(QgsSingleSymbolRenderer(symbol))
        )
        mask_layer.setBlendMode(QPainter.CompositionMode.CompositionMode_DestinationIn)

        # 5) 放大镜模式：mask 层 ReloadData 自动刷新跟随鼠标
        if method == "lens":
            mask_layer.setAutoRefreshMode(Qgis.AutoRefreshMode.ReloadData)
            mask_layer.setAutoRefreshInterval(LENS_AUTO_REFRESH_INTERVAL_MS)
        else:
            mask_layer.setAutoRefreshMode(Qgis.AutoRefreshMode.Disabled)

        self._generator = generator if method in ("vertical", "horizontal") else None
        self._group_node = group_node
        self._group_layer = group_layer
        self._mask_layer = mask_layer
        self._background_layer = background_layer
        self._method = method

        # 6) 垂直/水平分割：画布上加可拖动分隔条
        #    （无画布时跳过——如离屏渲染测试，CompareMask(project) 仍可用）
        if self._generator is not None and self._canvas is not None:
            try:
                self._overlay = SplitDividerOverlay(self._canvas, method, self.set_split_ratio)
                self._overlay.show()
            except RuntimeError:  # 画布正在销毁，退化为不可拖
                self._overlay = None
        return True

    # ------------------------------------------------------------------
    def set_split_ratio(self, ratio: float) -> None:
        """设置垂直 / 水平分割比例（0~1，拖动分隔条 / 外部调用）。

        重建遮罩 geometry generator 表达式并触发 mask 层重绘。仅
        ``triggerRepaint``（异步），不在 Qt 事件栈内调 ``canvas.refresh()``。
        """
        if self._method not in ("vertical", "horizontal"):
            return
        ratio = max(SPLIT_MIN_RATIO, min(SPLIT_MAX_RATIO, ratio))
        self._split_ratio = ratio
        generator = self._generator
        if generator is not None:
            try:
                generator.setGeometryExpression(get_split_geometry(self._method, ratio))
            except RuntimeError:
                return
        if self._overlay is not None:
            self._overlay.set_ratio(ratio)
        if self._mask_layer is not None:
            with contextlib.suppress(RuntimeError):
                self._mask_layer.triggerRepaint()

    def _teardown_overlay(self) -> None:
        """移除分隔条覆盖层（幂等）。"""
        overlay, self._overlay = self._overlay, None
        if overlay is None:
            return
        with contextlib.suppress(RuntimeError):
            overlay.teardown()
        overlay.hide()
        overlay.deleteLater()

    # ------------------------------------------------------------------
    def stop(self) -> None:
        """停止对比：还原对比图层原位、移除对比组、清理临时图层。"""
        self._teardown_overlay()
        self._generator = None
        if self._group_node is None:
            # 仍可能残留未清理的临时图层（防御）
            self._cleanup_temp_layers()
            return
        root = self._project.layerTreeRoot()

        # 1) 还原对比图层（先 addLayer 复制新节点、再摘旧节点：
        #    takeChild 时图层始终有节点，QgsLayerTreeRegistryBridge 不会删图层；
        #    按记录顺序还原保持原顺序）
        for parent, index, layer in self._moved:
            node = find_layer_node(root, layer)
            try:
                if node is not None:
                    if parent is None:
                        root.addLayer(layer)
                    else:
                        parent.addLayer(layer)
                    node.parent().takeChild(node)
            except Exception:  # noqa: BLE001 —— 原分组可能已被用户删除
                try:
                    root.addLayer(layer)
                except Exception:  # noqa: BLE001
                    pass
        self._moved.clear()

        # 2) mask 层停自动刷新
        if self._mask_layer is not None:
            try:
                self._mask_layer.setAutoRefreshMode(Qgis.AutoRefreshMode.Disabled)
            except Exception:  # noqa: BLE001
                pass

        # 3) 移除对比组。顺序关键：
        #    a) 先摘空组内节点（mask/背景节点析构时图层还活着，安全）；
        #       摘空后这两个图层无树节点，由桥在事件循环里从工程删除
        #       （不再手动 removeMapLayers，避免与桥的 queued 删除双删）；
        #    b) 再摘组节点本身（空组，无悬垂子节点）；
        #    c) 组图层（QgsGroupLayer）与组节点解绑后再手动删：
        #       - setChildLayers([])：析构不碰 mask；
        #       - setGroupLayer(None)：桥处理组节点移除时不再访问它。
        group_node = self._group_node
        self._group_node = None
        try:
            group_node.setGroupLayer(None)
        except Exception:  # noqa: BLE001
            pass
        if group_node.parent() is not None:
            for child in list(group_node.children()):
                group_node.removeChildNode(child)
            root.removeChildNode(group_node)

        if self._group_layer is not None:
            try:
                self._group_layer.setChildLayers([])
            except Exception:  # noqa: BLE001
                pass

        self._method = None
        self._cleanup_temp_layers()

    # ------------------------------------------------------------------
    def _move_into_group(self, layer: QgsMapLayer, group_node: QgsLayerTreeGroup) -> None:
        """把图层节点移入对比组，记录 (原父节点, 原索引, 图层)。

        QGIS 4.2 树节点移动的正确姿势（与 main_window._move_layer_to_group
        一致）：先 ``addLayer`` 在目标组挂新节点（图层始终在树里），再
        ``takeChild`` 摘原节点。反之若先把图层摘成无节点，QgsLayerTree-
        RegistryBridge 会在事件循环把图层从工程删掉。
        """
        node = find_layer_node(self._project.layerTreeRoot(), layer)
        if node is None:
            # 图层不在树中（罕见）：直接挂进组
            group_node.addLayer(layer)
            self._moved.append((None, 0, layer))
            return
        parent = node.parent()
        index = parent.children().index(node)
        self._moved.append((parent, index, layer))
        group_node.addLayer(layer)          # 先挂新节点（复制，图层 2 节点）
        node.parent().takeChild(node)       # 再摘原节点（安全：图层还有新节点）

    def _cleanup_temp_layers(self) -> None:
        """清理对比组临时图层（幂等）。

        只删 group_layer：mask/背景层挂在组节点下，``removeChildNode``
        后它们无树节点，由桥在事件循环里从工程移除——这里再
        ``removeMapLayers`` 会与桥的 queued 删除双删（悬垂访问崩溃）。
        组图层与组节点已解绑（``setGroupLayer(None)``），桥不再访问它。
        """
        if (
            self._group_layer is not None
            and self._group_layer.id() in self._project.mapLayers()
        ):
            self._project.removeMapLayers([self._group_layer.id()])
        self._group_layer = None
        self._mask_layer = None
        self._background_layer = None


def find_layer_node(root: QgsLayerTreeGroup, layer: QgsMapLayer) -> Optional[QgsLayerTreeLayer]:
    """在图层树中查找图层的节点（返回第一个匹配）。"""

    def walk(node) -> Optional[QgsLayerTreeLayer]:
        if isinstance(node, QgsLayerTreeGroup):
            for child in node.children():
                found = walk(child)
                if found is not None:
                    return found
        elif isinstance(node, QgsLayerTreeLayer) and node.layerId() == layer.id():
            return node
        return None

    return walk(root)


# ---------------------------------------------------------------------------
# Mirror：双画布镜像分屏
# ---------------------------------------------------------------------------
class MirrorCompare(QObject):
    """镜像分屏会话：右侧 dock 第二个画布显示对比图层，与主画布双向同步。

    用法::

        session = MirrorCompare(main_window, main_canvas)
        session.start(compare_layers)
        session.stop()
        session.is_active
    """

    def __init__(
        self,
        main_window: QMainWindow,
        canvas: QgsMapCanvas,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._main_window = main_window
        self._canvas = canvas
        self._dock: Optional[QDockWidget] = None
        self._mirror_canvas: Optional[QgsMapCanvas] = None
        # 递归同步保护标志（双向 setCenter/zoomScale 互触信号）
        self._synchronizing = False
        # 对比期间被隐藏的主画布图层节点：[(节点, 原可见性)]
        self._hidden_nodes: list[tuple[QgsLayerTreeLayer, bool]] = []

    # ------------------------------------------------------------------
    @property
    def is_active(self) -> bool:
        return self._dock is not None

    @property
    def mirror_canvas(self) -> Optional[QgsMapCanvas]:
        return self._mirror_canvas

    # ------------------------------------------------------------------
    def start(self, compare_layers: list[QgsMapLayer]) -> bool:
        """开启镜像分屏。"""
        self.stop()
        if not compare_layers:
            return False
        project = self._canvas.project()

        # 1) 右侧 dock + 第二画布（独立图层列表，不走树可见性）
        dock = QDockWidget(MIRROR_DOCK_TITLE, self._main_window)
        mirror_canvas = QgsMapCanvas(dock)
        mirror_canvas.setProject(project)
        mirror_canvas.setDestinationCrs(self._canvas.mapSettings().destinationCrs())
        mirror_canvas.setLayers(compare_layers)
        mirror_canvas.setMapTool(QgsMapToolPan(mirror_canvas))
        dock.setWidget(mirror_canvas)
        # 禁止关闭按钮（避免半开状态；统一由「地图对比」按钮退出）
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self._main_window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

        # 2) 主画布临时隐藏对比图层（互补显示），记录可见性
        for layer in compare_layers:
            node = find_layer_node(project.layerTreeRoot(), layer)
            if node is not None:
                self._hidden_nodes.append((node, node.isVisible()))
                node.setItemVisibilityChecked(False)

        # 3) 初始 extent / scale 同步（推迟：dock 刚加入未布局，画布尺寸 0
        #    时 setCenter 会产生 nan extent，且遵守「事件栈内不 refresh」约束）
        QTimer.singleShot(0, self._initial_sync)

        # 4) 双向同步（信号槽内只 setCenter/zoomScale，不 refresh ——
        #    遵守 qgis_dev「Qt 事件栈内不 refresh」约束）
        self._canvas.extentsChanged.connect(self._sync_to_mirror)
        self._canvas.scaleChanged.connect(self._sync_to_mirror)
        mirror_canvas.extentsChanged.connect(self._sync_from_mirror)
        mirror_canvas.scaleChanged.connect(self._sync_from_mirror)

        self._dock = dock
        self._mirror_canvas = mirror_canvas
        return True

    def _initial_sync(self) -> None:
        """初始同步（事件循环内执行，画布已有尺寸）。

        必须先 setExtent：QgsMapCanvas 从未 setExtent 时 setCenter/zoomScale
        是 no-op（extent 保持 Null，center 变 nan）。
        """
        if self._mirror_canvas is None:
            return
        self._mirror_canvas.setExtent(self._canvas.extent())
        self._mirror_canvas.setCenter(self._canvas.center())
        self._mirror_canvas.zoomScale(self._canvas.scale())
        self._mirror_canvas.refresh()

    # ------------------------------------------------------------------
    def stop(self) -> None:
        """停止镜像分屏：断信号、还原可见性、销毁 dock。"""
        if self._dock is None:
            return
        # 1) 断双向同步
        for signal in (self._canvas.extentsChanged, self._canvas.scaleChanged):
            try:
                signal.disconnect(self._sync_to_mirror)
            except TypeError:
                pass
        for signal in (
            self._mirror_canvas.extentsChanged,
            self._mirror_canvas.scaleChanged,
        ):
            try:
                signal.disconnect(self._sync_from_mirror)
            except TypeError:
                pass

        # 2) 还原主画布对比图层可见性
        for node, visible in self._hidden_nodes:
            try:
                node.setItemVisibilityChecked(visible)
            except RuntimeError:  # 节点已随图层删除
                pass
        self._hidden_nodes.clear()

        # 3) 销毁 dock：先解绑父窗口（findChildren 立即不再返回），再排期删除
        self._dock.close()
        self._dock.setParent(None)
        self._dock.deleteLater()
        self._dock = None
        self._mirror_canvas = None

    # ------------------------------------------------------------------
    def _sync_to_mirror(self) -> None:
        if self._synchronizing or self._mirror_canvas is None:
            return
        if self._mirror_canvas.size().width() <= 0:  # dock 收起时跳过，防 nan 传染
            return
        self._synchronizing = True
        try:
            self._mirror_canvas.setCenter(self._canvas.center())
            self._mirror_canvas.zoomScale(self._canvas.scale())
        finally:
            self._synchronizing = False

    def _sync_from_mirror(self) -> None:
        if self._synchronizing or self._canvas is None:
            return
        if self._mirror_canvas is None or self._mirror_canvas.size().width() <= 0:
            return
        self._synchronizing = True
        try:
            self._canvas.setCenter(self._mirror_canvas.center())
            self._canvas.zoomScale(self._mirror_canvas.scale())
        finally:
            self._synchronizing = False


# ---------------------------------------------------------------------------
# 离屏渲染辅助（测试用；GUI 画布渲染自动带表达式变量，无需注入）
# ---------------------------------------------------------------------------
def offscreen_render(settings, project, size=(400, 400), background="white"):
    """用 QgsMapRendererCustomPainterJob 离屏渲染。

    QGIS 4.2 的 ``QgsRenderContext.fromMapSettings`` 不再自动填充
    ``@map_extent_*`` 等变量，需要手动注入 global + project +
    mapSettings scope（GUI 画布渲染无需此步骤）。
    """
    from qgis.PyQt.QtCore import QSize
    from qgis.PyQt.QtGui import QColor, QImage, QPainter

    from qgis.core import QgsMapRendererCustomPainterJob

    settings.setOutputSize(QSize(*size))
    settings.setBackgroundColor(QColor(background))
    ctx = QgsExpressionContext()
    ctx.appendScope(QgsExpressionContextUtils.globalScope())
    ctx.appendScope(QgsExpressionContextUtils.projectScope(project))
    ctx.appendScope(QgsExpressionContextUtils.mapSettingsScope(settings))
    settings.setExpressionContext(ctx)

    img = QImage(*size, QImage.Format.Format_RGB32)
    img.fill(QColor(background))
    painter = QPainter(img)
    job = QgsMapRendererCustomPainterJob(settings, painter)
    job.start()
    job.waitForFinished()
    painter.end()
    return img
