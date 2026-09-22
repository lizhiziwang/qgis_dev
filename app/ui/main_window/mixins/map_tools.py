"""地图工具与选择。

地图工具的切换（点选/框选/平移/缩放）与清除选择、要素信息查看、刷新画布。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QMessageBox

from app.ui.docks.feature_info_dock import FeatureInfoDock
from app.ui.tools.find_feature import FindFeatureTool
from app.ui.tools.select_feature import (
    PointSelectTool,
    RectSelectTool,
)


class MapToolsMixin:
    """MapToolsMixin：见模块 docstring。"""

    # ===================================================== 工具回调

    def _tool_key(self, tool) -> Optional[str]:
        """地图工具 → 工具条按钮 key（None 表示非工具条地图工具，不高亮）。"""
        if tool is None:
            return None
        if tool is self._pan_tool:
            return "pan"
        if tool is self._zoom_in_tool:
            return "zoom_in"
        if tool is self._zoom_out_tool:
            return "zoom_out"
        return {
            "PointSelectTool": "select_point",
            "RectSelectTool": "select_box",
            "FindFeatureTool": "identify",
            "SwipeTool": "swipe",
        }.get(type(tool).__name__)

    def _on_map_tool_set(self, tool) -> None:
        """画布激活的地图工具变化 → 同步顶部工具条与左侧竖排工具条单选高亮。"""
        key = self._tool_key(tool)
        self.tool_bar_builder.set_active(key)
        self.side_toolbar.set_active(key)

    def _on_pan(self) -> None:
        self.canvas.setMapTool(self._pan_tool)
        # setMapTool(同一工具) 不触发 mapToolSet：重复点击已激活按钮时
        # 勾选态被 QAction 自动翻转，这里显式恢复
        self.tool_bar_builder.set_active("pan")

    def _on_select_point(self) -> None:

        self._select_point_tool = PointSelectTool(self.canvas)
        self.canvas.setMapTool(self._select_point_tool)
        self.tool_bar_builder.set_active("select_point")

    def _on_select_box(self) -> None:

        self._select_box_tool = RectSelectTool(self.canvas)
        self.canvas.setMapTool(self._select_box_tool)
        self.tool_bar_builder.set_active("select_box")

    def _on_clear_selection(self) -> None:
        """取消选择：清空**所有矢量图层**的当前选择（画布重绘由 QGIS 自己驱动）。

        ``removeSelection()`` 会发 ``selectionChanged`` → 图层 ``triggerRepaint()``
        → ``repaintRequested``，画布据此重画这一层（其它层复用缓存），不需要再显式
        ``canvas.refresh()`` 刷第二遍整幅图。
        """
        from qgis.core import QgsVectorLayer

        for layer in self.project.mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and layer.selectedFeatureCount() > 0:
                layer.removeSelection()

    def _on_identify(self) -> None:

        self._find_feature_tool = FindFeatureTool(self.canvas, parent=self)
        self.canvas.setMapTool(self._find_feature_tool)
        self.tool_bar_builder.set_active("identify")

    def show_feature_info(self, result, highlight=None) -> None:
        """点击识别后把要素详情显示到右侧 Dock（懒创建，内容就地刷新）。"""
        if not hasattr(self, "_feature_info_dock") or self._feature_info_dock is None:
            self._feature_info_dock = FeatureInfoDock(self)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._feature_info_dock)
        self._feature_info_dock.show_feature(result, highlight=highlight)

    def _on_zoom_in(self) -> None:
        self.canvas.setMapTool(self._zoom_in_tool)
        self.tool_bar_builder.set_active("zoom_in")

    def _on_zoom_out(self) -> None:
        self.canvas.setMapTool(self._zoom_out_tool)
        self.tool_bar_builder.set_active("zoom_out")

    def _on_zoom_full(self) -> None:
        layers = [l for l in self.project.mapLayers().values() if l.isValid()]
        if not layers:
            QMessageBox.information(self, "提示", "当前无有效图层")
            return
        combined = layers[0].extent()
        for l in layers[1:]:
            combined.combineExtentWith(l.extent())
        self.canvas.setExtent(combined)
        self.canvas.refresh()

    def _on_refresh(self) -> None:
        self.canvas.refresh()
