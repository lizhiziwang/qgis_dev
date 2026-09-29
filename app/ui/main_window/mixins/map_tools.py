"""地图工具与选择。

地图工具的切换（点选/框选/平移/缩放）与清除选择、要素信息查看、刷新画布。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

from qgis.PyQt import sip
from qgis.PyQt.QtWidgets import QMessageBox

from app.ui.docks.feature_info_dock import FeatureInfoDock
from app.ui.tools.find_feature import FindFeatureTool
from app.ui.tools.measure import MeasureAreaTool, MeasureLengthTool
from app.ui.tools.select_feature import (
    PointSelectTool,
    RectSelectTool,
)


class MapToolsMixin:
    """MapToolsMixin：见模块 docstring。"""

    # ===================================================== 工具回调

    def _tool_key(self, tool) -> str | None:
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
            "MeasureLengthTool": "measure_line",
            "MeasureAreaTool": "measure_area",
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
        # 侧栏也有「属性」按钮（同一个工具）：setMapTool 到新工具时 mapToolSet
        # 会同步，但同工具重复激活不发信号，这里显式校正一次
        self.side_toolbar.set_active("identify")

    def show_feature_info(self, result, highlight=None) -> None:
        """点击识别后把要素详情显示到右侧 Dock（懒创建，内容就地刷新）。"""
        if not hasattr(self, "_feature_info_dock") or self._feature_info_dock is None:
            self._feature_info_dock = FeatureInfoDock(self)
            self.place_right_dock(self._feature_info_dock)
        self._feature_info_dock.show_feature(result, highlight=highlight)

    def _on_zoom_in(self) -> None:
        self.canvas.setMapTool(self._zoom_in_tool)
        self.tool_bar_builder.set_active("zoom_in")

    def _on_zoom_out(self) -> None:
        self.canvas.setMapTool(self._zoom_out_tool)
        self.tool_bar_builder.set_active("zoom_out")

    def _measure_tool(self, kind: str):
        """懒建并缓存测量工具。

        必须缓存：每次点击都 new 一个会在画布上堆积读数条 QLabel（工具实例
        不退场，widget 也不会被回收）。
        """
        attr = f"_measure_{kind}_tool"
        tool = getattr(self, attr, None)
        if tool is None:
            tool = (MeasureLengthTool if kind == "line" else MeasureAreaTool)(self.canvas)
            setattr(self, attr, tool)
        return tool

    def _is_measure_tool(self, tool) -> bool:
        """``tool`` 是否为本工程的测量工具（线 / 面）。"""
        return isinstance(tool, (MeasureLengthTool, MeasureAreaTool))

    def _on_measure_line(self) -> None:
        """侧栏「线测量」：**开关型**——未启用则启用，已启用则取消。

        启用后连续左键落点实时累计折线长度（右键结束保留图形、Esc 清除）。
        """
        self._toggle_measure_tool("line")

    def _on_measure_area(self) -> None:
        """侧栏「面测量」：**开关型**——未启用则启用，已启用则取消。

        启用后连续左键落点实时累计多边形面积与周长（右键结束、Esc 清除）。
        """
        self._toggle_measure_tool("area")

    def _toggle_measure_tool(self, kind: str) -> None:
        """测量工具开关：未启用 → 启用；已启用 → 取消并还原画布工具。

        侧栏按钮是 checkable 的（Qt 点击时已自行翻转勾选态），所以这里按
        **画布当前工具是不是该测量工具**判断用户意图，再由 :meth:`set_active`
        统一校正勾选态——不依赖 ``clicked(bool)`` 传参，也避免重复点击被当成
        “再激活一次”。

        :param kind: ``"line"`` / ``"area"``
        """
        tool = self._measure_tool(kind)
        if self.canvas.mapTool() is tool:
            self._stop_measure_tool(tool)
            return
        # 记住进入测量前的常规工具（线 → 面互切不覆盖它，取消时能回到原点）
        previous = self.canvas.mapTool()
        if previous is not None and not self._is_measure_tool(previous):
            self._measure_previous_tool = previous
        self.canvas.setMapTool(tool)
        # setMapTool(同一工具) 不触发 mapToolSet，不同工具时也显式同步一次更稳
        key = f"measure_{kind}"
        self.tool_bar_builder.set_active(key)
        self.side_toolbar.set_active(key)

    def _stop_measure_tool(self, tool) -> None:
        """取消测量：清掉图形与读数条，并还原到进入测量前的画布工具。

        与 QGIS 桌面一致——退出测量即抹掉量算图形；想留住结果请用**右键结束**
        （结束态停在图上、读数条保留）。
        """
        tool.clear()
        previous = self._measure_previous_tool
        self._measure_previous_tool = None
        # 上一个工具可能已随图层/画布销毁（sip 引用还在、C++ 对象已死）：退回平移
        if previous is None or self._is_measure_tool(previous) or sip.isdeleted(previous):
            previous = self._pan_tool
        self.canvas.setMapTool(previous)
        # 还原到不同工具时 mapToolSet 会同步高亮；兜底再校正一次（同工具时不发信号）
        key = self._tool_key(self.canvas.mapTool())
        self.tool_bar_builder.set_active(key)
        self.side_toolbar.set_active(key)

    def _on_zoom_full(self) -> None:
        layers = [layer for layer in self.project.mapLayers().values() if layer.isValid()]
        if not layers:
            QMessageBox.information(self, "提示", "当前无有效图层")
            return
        combined = layers[0].extent()
        for layer in layers[1:]:
            combined.combineExtentWith(layer.extent())
        self.canvas.setExtent(combined)
        self.canvas.refresh()

    def _on_refresh(self) -> None:
        self.canvas.refresh()
