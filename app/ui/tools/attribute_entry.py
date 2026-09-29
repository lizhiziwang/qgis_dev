"""属性录入交互工具。

编辑工具条「属性录入」按钮激活的交互工具：**点击矢量要素 → 在右侧 Dock
（:class:`app.ui.docks.attribute_entry_dock.AttributeEntryDock`）中编辑该
要素的属性**，点「确认修改」把改动写回图层编辑缓冲：

- **左键单击要素** → 命中后**把图层选中集换成该要素**（QGIS 自身的选中高亮，
  一眼看出正在录入的是哪一块），再回调 ``on_feature(layer, fid)``（由工具条接
  入 Dock 的 :meth:`AttributeEntryDock.load_feature`）；
- **点击空白** → 提示未命中；
- **Esc** → 退出工具。

只作用于**当前编辑目标图层**（:class:`~app.ui.main_window.draw_toolbar.DigitizingToolbar`
的编辑目标）。该工具激活时（:meth:`DigitizingToolbar.enable_attribute_entry`）
目标图层**自动进入编辑态**，无需用户先点「开始编辑」——点击载入 / Dock
确认的写回都走编辑缓冲，因此本工具只保留防御性校验（万一激活后图层被
外部停止编辑则只提示、不载入）。命中测试在画布像素空间做（容差 12px，
与 :class:`~app.ui.tools.feature_edit.FeatureEditTool` 一致），多要素命中取
**最上层**（后添加的要素通常绘制在上层）。
"""

from __future__ import annotations

from collections.abc import Callable

from qgis.core import (
    Qgis,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.gui import QgsMapTool
from qgis.PyQt.QtCore import QPoint, QPointF, Qt, QTimer

from app.core.domain.check.query import intersect_features
from app.infra.logging import get_logger

logger = get_logger(__name__)


class AttributeEntryTool(QgsMapTool):
    """点击要素 → 唤起右侧属性录入 Dock 的工具。"""

    #: 命中容差（像素），与要素几何编辑工具一致
    TOLERANCE_PX = 12

    def __init__(
        self,
        canvas,
        editing_layer_getter: Callable[[], QgsVectorLayer | None] = lambda: None,
        on_feature: Callable[[QgsVectorLayer, int], None] | None = None,
        hint: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self._layer_getter = editing_layer_getter
        #: 命中要素回调 (layer, fid)
        self._on_feature = on_feature or (lambda layer, fid: None)
        self._hint = hint or (lambda text: None)
        #: 已载入的 (layer, fid)：重复点击同一要素不重载（避免丢 Dock 里的编辑）
        self._loaded: tuple[QgsVectorLayer, int] | None = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    # -------------------------------------------------- 工具生命周期

    def activate(self) -> None:
        super().activate()

    def deactivate(self) -> None:
        # 工具退出：下次激活时可重新点同一要素载入最新属性
        self._loaded = None
        super().deactivate()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            event.ignore()
            # 不在键盘事件栈内 unsetMapTool，推迟出栈（仓库约束，见 swipe.py）
            QTimer.singleShot(0, self._quit)
        else:
            super().keyPressEvent(event)

    def _quit(self) -> None:
        try:
            self.canvas.unsetMapTool(self)
        except RuntimeError:
            pass

    # -------------------------------------------------- 交互

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        """左键单击：命中要素则载入其属性到 Dock。"""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        layer = self._current_layer()
        if layer is None or layer.type() != layer.VectorLayer:
            self._hint("没有编辑图层：请先点「开始编辑」选择图层")
            return
        if not layer.isEditable():
            self._hint(f"图层「{layer.name()}」未处于编辑态，无法录入属性")
            return
        fid = self._pick_fid(event, layer)
        if fid is None:
            self._hint("未命中要素：请点击当前编辑图层上的要素")
            return
        # 点击即把图层选中集换成这一个：地图上用 QGIS 自身的选中高亮标出正在
        # 录入的是哪一块。**替换**而非追加（SetSelection）——属性录入一次只针对
        # 一个要素，累积选中没有意义。
        self._select_only(layer, fid)
        if self._loaded == (layer, fid):
            return  # 已在编辑该要素，忽略重复点击（Dock 里的输入不清空）
        self._loaded = (layer, fid)
        self._on_feature(layer, fid)

    def _select_only(self, layer: QgsVectorLayer, fid: int) -> None:
        """把图层选中集换成 ``fid`` 这一个（失败不影响录入流程）。"""
        try:
            layer.selectByIds([fid], Qgis.SelectBehavior.SetSelection)
        except (RuntimeError, AttributeError):
            # 图层销毁 / 构建不支持 selectByIds：高亮缺失，但属性录入照常
            logger.warning("属性录入选中要素失败（不影响录入）", exc_info=True)

    # -------------------------------------------------- 拾取

    def _pick_fid(self, event, layer: QgsVectorLayer) -> int | None:
        """点击像素 → 命中要素 fid（12px 容差矩形求交；多要素命中取最上层）。"""
        px = self._pixel_of_event(event)
        if px is None:
            return None
        map_pt = self._map_point_of_px(px)
        if map_pt is None:
            return None
        tol = self.TOLERANCE_PX * self.canvas.mapSettings().mapUnitsPerPixel()
        rect = QgsRectangle(
            map_pt.x() - tol, map_pt.y() - tol, map_pt.x() + tol, map_pt.y() + tol
        )
        project = self.canvas.project() or QgsProject.instance()
        fids = intersect_features(
            layer,
            QgsGeometry.fromRect(rect),
            self.canvas.mapSettings().destinationCrs(),
            project,
        )
        # 过滤已删除/失效的要素（空间索引缓存可能含已删 fid）
        fids = [fid for fid in fids if layer.getFeature(fid).isValid()]
        return fids[-1] if fids else None  # 后添加的要素通常绘制在上层

    # -------------------------------------------------- 图层 / 坐标

    def _current_layer(self) -> QgsVectorLayer | None:
        return self._layer_getter()

    def _pixel_of_event(self, event) -> QPoint | None:
        """事件像素坐标（QPointF 测试桩 / 真实 QPoint 都兼容）。"""
        try:
            raw = event.pixelPoint()
        except AttributeError:
            raw = event.pos()
        if isinstance(raw, QPointF):
            return raw.toPoint()
        return QPoint(raw.x(), raw.y())

    def _map_point_of_px(self, px: QPoint):
        """画布像素 → 画布地图坐标（canvas 目标 CRS）。"""
        try:
            return self.canvas.getCoordinateTransform().toMapCoordinates(px)
        except RuntimeError:
            return None


__all__ = ["AttributeEntryTool"]
