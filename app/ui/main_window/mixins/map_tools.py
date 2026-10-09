"""地图工具与选择。

地图工具的切换（点选/框选/平移/缩放）与清除选择、要素信息查看、刷新画布。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

from qgis.core import Qgis, QgsCoordinateTransform
from qgis.PyQt import sip
from qgis.PyQt.QtWidgets import QMessageBox

from app.infra.logging import get_logger

logger = get_logger(__name__)

from app.ui.dialogs.selection_set import SelectionSetDialog
from app.ui.docks.feature_info_dock import FeatureInfoDock
from app.ui.tools.find_feature import FindFeatureTool
from app.ui.tools.measure import MeasureAreaTool, MeasureLengthTool
from app.ui.tools.select_feature import (
    PointSelectTool,
    RectSelectTool,
)


#: 「操作指引」类提示的展示时长（秒）。这类提示是让用户**看完再动手**（"按住左键拉框"
#: "请先选中要素"），3 秒常来不及；结果类提示保持 :data:`_NOTIFY_SECONDS`。
_HINT_NOTIFY_SECONDS = 6

#: 结果类提示的展示时长（秒）：与其它工具一致（重命名 2 / 删除 3 / 建数据 4）
_NOTIFY_SECONDS = 3


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

        self._select_point_tool = PointSelectTool(
            self.canvas, visible_filter_getter=self.display_filter_for
        )
        self.canvas.setMapTool(self._select_point_tool)
        self.tool_bar_builder.set_active("select_point")

    def _on_select_box(self) -> None:

        self._select_box_tool = RectSelectTool(
            self.canvas,
            visible_filter_getter=self.display_filter_for,
            on_select_complete=self._on_box_select_complete,
        )
        self.canvas.setMapTool(self._select_box_tool)
        self.tool_bar_builder.set_active("select_box")

    # ===================================================== 选择集

    def _on_box_select_complete(self) -> None:
        """框选结束：弹出或刷新「选择集」面板。

        已开着的就刷新 + 置前，不另开一个（面板是常驻观察窗，见
        :class:`~app.ui.dialogs.selection_set.SelectionSetDialog`）。

        **不调 ``activateWindow()``**：面板的承诺是「可继续用选择工具增减」，抢焦点会让
        用户没法接着拖下一个框、也没法直接按 Delete 删选中——这一点与
        ``_on_offline_download`` 的做法有意不同。
        """
        dlg = getattr(self, "_selection_set_dialog", None)
        if dlg is None or sip.isdeleted(dlg):
            dlg = SelectionSetDialog(self.canvas, parent=self)
            self._selection_set_dialog = dlg
        dlg.refresh()
        if dlg.isMinimized():
            dlg.showNormal()
        else:
            dlg.show()
        dlg.raise_()

    def _on_clear_selection(self) -> None:
        """清除：清空**所有图层**的选中集，并抹掉临时绘制的辅助图形。

        「清除」不只是清选择——用户画到一半的测量图形、测量读数条这些**临时
        辅助图形**同样归它管（与 QGIS 桌面「清除测量」的直觉一致）。目前临时
        图形来自「线测量 / 面测量」，各自 :meth:`clear` 会同时收掉图形与读数条。

        工具**保持激活**（清完可以接着量），与 QGIS 的测量行为一致。
        """
        from qgis.core import QgsVectorLayer

        cleared = 0
        for layer in self.project.mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and layer.selectedFeatureCount() > 0:
                layer.removeSelection()
                cleared += 1
        cleared += self._clear_temp_graphics()
        logger.debug("清除：清了 %d 个图层的选中 + %d 个临时图形", cleared, cleared)

    def _clear_temp_graphics(self) -> int:
        """抹掉临时绘制的辅助图形（线 / 面测量的图形与读数条），返回处理了几个。"""
        n = 0
        for attr in ("_measure_line_tool", "_measure_area_tool"):
            tool = getattr(self, attr, None)
            if tool is None or sip.isdeleted(tool):
                continue
            try:
                tool.clear()
                n += 1
            except Exception:  # noqa: BLE001 —— 收尾失败不该影响清除本身
                logger.exception("清除测量临时图形失败: %s", attr)
        return n

    # ===================================================== 选择屏蔽

    def _mask_state(self) -> dict:
        """屏蔽前的现场 ``{layer_id: {"renderer": <克隆>}}``（懒建）。"""
        if not hasattr(self, "_select_mask_saved"):
            self._select_mask_saved: dict = {}
        return self._select_mask_saved

    @staticmethod
    def _masked_renderer(layer, fids):
        """按当前渲染器造一个「只渲染 ``fids``」的等价规则渲染器；造不出返回 None。

        过滤条件用**固定的 ``$id IN (…)``，不能用 ``is_selected()``**：后者是渲染
        时实时求值的——用户一按「清除」清空选中，整层就什么都不画了；点选别的图形，
        显示还会跟着变。那不是屏蔽，是个跟着选中集走的显示开关。屏蔽的语义是
        「屏蔽那一刻选中的这些要素留下，其余藏起来」，与之后改不改选中集无关。
        """
        condition = "$id IN ({})".format(",".join(str(int(f)) for f in sorted(fids)))
        return MapToolsMixin._renderer_filtered_by(layer, condition)

    def _on_select_mask(self) -> None:
        """选择屏蔽：各图层只**显示**屏蔽那一刻选中的要素，其余隐藏。

        为什么不用图层过滤表达式（``subsetString``）：实测只要图层处于编辑态，
        ``setSubsetString`` 就恒返回 False（与有没有未提交改动无关），而用户恰恰
        经常在编辑中屏蔽。渲染级方案不动数据、不动查询、不碰编辑缓冲，编辑态照样
        生效；过滤条件是屏蔽时固定下来的 ``$id IN (…)``，之后清除选择 / 改选别的
        都不影响已屏蔽的结果。取消屏蔽时把**屏蔽前克隆的渲染器**原样交还。

        注意 ``layer.setRenderer()`` 会**接管所有权并删掉旧渲染器**，所以现场必须
        先 ``clone()`` 存下来，不能存裸引用（存了就是悬垂指针，还原时直接崩）。
        """
        from qgis.core import QgsVectorLayer

        saved = self._mask_state()
        masked = 0
        for layer in self.project.mapLayers().values():
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                continue
            fids = layer.selectedFeatureIds()
            if not fids:
                continue
            if layer.id() in saved:
                continue  # 已在屏蔽态，不重复套
            current = layer.renderer()
            if current is None:
                continue
            renderer = self._masked_renderer(layer, fids)
            if renderer is None:
                continue
            # fids 一起存下来：点选 / 框选要靠它把命中结果收窄到"可见的那些"
            saved[layer.id()] = {"renderer": current.clone(), "fids": {int(f) for f in fids}}
            layer.setRenderer(renderer)
            layer.triggerRepaint()
            masked += 1

        if masked == 0:
            self._notify("请先用点选 / 框选选中要显示的图形，再点「选择屏蔽」",
                         level="info", duration=_HINT_NOTIFY_SECONDS)
            return
        self._notify(f"已屏蔽 {masked} 个图层中未选中的图形（清除/改选不影响已屏蔽结果）")

    def _on_unmask(self) -> None:
        """取消屏蔽：各图层还原到屏蔽前的渲染器。"""
        from qgis.core import QgsVectorLayer

        saved = self._mask_state()
        if not saved:
            self._notify("当前没有开启选择屏蔽", level="info")
            return
        restored = 0
        for layer_id, info in list(saved.items()):
            layer = self.project.mapLayer(layer_id)
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                continue
            renderer = info.get("renderer")
            if renderer is None:
                continue
            try:
                layer.setRenderer(renderer)
                layer.triggerRepaint()
                restored += 1
            except Exception:  # noqa: BLE001 —— 单层还原失败不影响其它层
                logger.exception("取消屏蔽：还原渲染器失败: %s", layer.name())
        saved.clear()
        self._notify(f"已取消屏蔽，还原 {restored} 个图层")

    def display_filter_for(self, layer):
        """该图层当前的「可见性」判定（选择屏蔽 / 范围裁剪）；无过滤时返回 None。

        **点选 / 框选必须用它**：屏蔽与裁剪都是**渲染级**过滤（只决定画不画），
        要素本身还在图层里，``intersect_features`` 查的是数据、不看渲染器——不过滤
        的话就会出现"屏蔽/裁剪后还能选中看不见的图形"。

        :return: ``callable(fid) -> bool``（True = 当前可见 / 可选中），或 ``None``。
        """
        if layer is None:
            return None
        layer_id = layer.id()
        info = self._mask_state().get(layer_id)
        if info and "fids" in info:
            allowed = info["fids"]
            return lambda fid: int(fid) in allowed
        info = self._clip_state().get(layer_id)
        if info and "box" in info:
            box = info["box"]
            return lambda fid: self._feature_hits_box(layer, fid, box)
        return None

    @staticmethod
    def _feature_hits_box(layer, fid, box) -> bool:
        """要素是否与 ``box``（已换算到该图层 CRS）相交；取不到几何按不可见处理。"""
        try:
            feature = layer.getFeature(fid)
            if not feature.isValid():
                return False
            geom = feature.geometry()
            if geom is None or geom.isNull() or geom.isEmpty():
                return False
            return bool(geom.intersects(box))
        except RuntimeError:
            return False

    def _notify(self, message: str, level: str = "success", duration: int = _NOTIFY_SECONDS) -> None:
        """侧栏工具的结果提示：走浮动消息条，**过几秒自动收起**（默认 3 秒）。

        以前这里不带 ``duration``（= 常驻，只能点 × 收起）：屏蔽 / 取消屏蔽 / 范围
        裁剪这些操作会做很多次，条子老挂着挡视线，而且常驻提示被 × 关掉之后宿主那份
        「有消息」状态还会残留（见 ``_relayout_floating_message_bar`` 的对账）。现在
        与其它工具（重命名 2 秒、删除 3 秒、建数据 4 秒）一致走定时。

        文案长也不用担心看不完：``FloatingMessageBar`` 的展示租约在**鼠标停在条上**
        时会一直留着，移开后再重新计满一个时长才收。

        **操作指引类**（让人看完再动手的）传 :data:`_HINT_NOTIFY_SECONDS`，3 秒常来不及。
        """
        bar = getattr(self, "messageBar", None)
        if bar is None:
            return
        bar.pushMessage(message, level=Qgis.MessageLevel.Success if level == "success"
                        else Qgis.MessageLevel.Info, duration=duration)

    # ===================================================== 范围裁剪

    def _clip_state(self) -> dict:
        """裁剪前的现场 ``{layer_id: {"renderer": <克隆>}}``（懒建）。"""
        if not hasattr(self, "_extent_clip_saved"):
            self._extent_clip_saved: dict = {}
        return self._extent_clip_saved

    def _on_extent_clip(self) -> None:
        """范围裁剪：在地图上拉一个矩形，只显示框内的图形（影像图层除外）。

        与「选择屏蔽」同一套做法 —— **渲染级**过滤，不动数据、不碰编辑缓冲，
        编辑态照样生效（``setSubsetString`` 在编辑态一律被拒，实测）。区别在
        过滤条件：这里是「与矩形框相交」而不是「属于某几个 fid」。

        栅格 / 影像图层不动：用户明确要求排除，而且渲染级过滤对栅格本来也不适用。
        再点一次本按钮（取消勾选）即还原。
        """
        from app.ui.tools.extent_clip import ExtentClipTool

        # 已经是裁剪态 → 本次是「取消」：还原现场
        if self._clip_state():
            self._clear_extent_clip()
            return

        self._extent_clip_tool = ExtentClipTool(self.canvas, on_clip=self._apply_extent_clip)
        previous = self.canvas.mapTool()
        if previous is not None and previous is not self._extent_clip_tool:
            self._extent_clip_previous_tool = previous
        self.canvas.setMapTool(self._extent_clip_tool)
        self.side_toolbar.set_session_active("extent_clip", True)
        self._notify("范围裁剪：在地图上按住左键拉一个矩形框",
                     level="info", duration=_HINT_NOTIFY_SECONDS)

    def _apply_extent_clip(self, rect) -> None:
        """拉框完成：把各矢量图层过滤到框内（``rect`` 为画布 CRS 的矩形）。"""
        from qgis.core import QgsCoordinateTransform, QgsGeometry, QgsVectorLayer

        saved = self._clip_state()
        clipped = 0
        for layer in self.project.mapLayers().values():
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                continue
            if layer.id() in saved:
                continue
            current = layer.renderer()
            if current is None:
                continue
            # 把画布 CRS 的矩形换算到该图层 CRS，再拼成固定的 WKT 条件
            box = QgsGeometry.fromRect(rect)
            try:
                if layer.crs() != self.canvas.mapSettings().destinationCrs():
                    box.transform(QgsCoordinateTransform(
                        self.canvas.mapSettings().destinationCrs(), layer.crs(), self.project
                    ))
            except Exception:  # noqa: BLE001
                logger.warning("范围裁剪：坐标转换失败: %s", layer.name(), exc_info=True)
                continue
            renderer = self._renderer_filtered_by(layer, f"intersects($geometry, geom_from_wkt('{box.asWkt()}'))")
            if renderer is None:
                continue
            # box 一起存下来（已是该图层 CRS）：点选 / 框选要靠它收窄命中结果
            saved[layer.id()] = {"renderer": current.clone(), "box": box}
            layer.setRenderer(renderer)
            layer.triggerRepaint()
            clipped += 1

        if clipped == 0:
            self._notify("范围内没有可裁剪的矢量图层", level="info")
        else:
            self._notify(f"已按范围裁剪 {clipped} 个矢量图层（影像图层不受影响）")
        # 拉完框即退出拉框模式（按钮弹起），要改范围就再点一次
        self._finish_extent_clip_tool()

    def _finish_extent_clip_tool(self) -> None:
        """收掉拉框工具并恢复激活前的画布工具。"""
        tool = getattr(self, "_extent_clip_tool", None)
        self._extent_clip_tool = None
        previous = getattr(self, "_extent_clip_previous_tool", None)
        self._extent_clip_previous_tool = None
        try:
            if tool is not None and self.canvas.mapTool() is tool:
                self.canvas.unsetMapTool(tool)
        except Exception:  # noqa: BLE001 —— 收尾失败不该影响裁剪结果
            logger.exception("卸载范围裁剪拉框工具失败")
        if previous is not None and not sip.isdeleted(previous):
            self.canvas.setMapTool(previous)
        self.side_toolbar.set_session_active(
            "extent_clip", bool(self._clip_state())
        )

    def _clear_extent_clip(self) -> None:
        """取消范围裁剪：各图层还原到裁剪前的渲染器。"""
        from qgis.core import QgsVectorLayer

        saved = self._clip_state()
        if not saved:
            self._notify("当前没有开启范围裁剪", level="info")
            return
        restored = 0
        for layer_id, info in list(saved.items()):
            layer = self.project.mapLayer(layer_id)
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                continue
            renderer = info.get("renderer")
            if renderer is None:
                continue
            try:
                layer.setRenderer(renderer)
                layer.triggerRepaint()
                restored += 1
            except Exception:  # noqa: BLE001
                logger.exception("取消范围裁剪：还原渲染器失败: %s", layer.name())
        saved.clear()
        self._finish_extent_clip_tool()
        self._notify(f"已取消范围裁剪，还原 {restored} 个图层")

    @staticmethod
    def _renderer_filtered_by(layer, condition: str):
        """按 ``condition`` 造一个「只渲染满足该条件的要素」的等价规则渲染器。

        与 :meth:`_masked_renderer` 同构，只是过滤条件由调用方给（屏蔽用
        ``$id IN (…)``，范围裁剪用 ``intersects($geometry, …)``）。
        """
        from qgis.core import QgsRuleBasedRenderer

        try:
            renderer = QgsRuleBasedRenderer.convertFromRenderer(layer.renderer())
        except Exception:  # noqa: BLE001
            logger.warning("渲染器转换失败: %s", layer.name(), exc_info=True)
            return None
        if renderer is None:
            return None
        root = renderer.rootRule()
        for rule in root.children():
            old = (rule.filterExpression() or "").strip()
            rule.setFilterExpression(f"({old}) AND ({condition})" if old else condition)
        hidden = QgsRuleBasedRenderer.Rule(None)
        hidden.setLabel("范围外（不显示）")
        root.appendChild(hidden)
        root.setActive(True)
        return renderer

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
        """全图：合并所有有效图层的范围。

        ``combineExtentWith`` 只做数值并集、不理解坐标系，因此每个图层的
        ``extent()`` 必须先换算到画布 CRS 再合并，否则经纬度（度）与投影（米）
        图层混用时全图会偏。
        """
        dest_crs = self.canvas.mapSettings().destinationCrs()
        combined = None
        for layer in self.project.mapLayers().values():
            if not layer.isValid():
                continue
            extent = layer.extent()
            if extent.isNull():
                continue
            if layer.crs() != dest_crs:
                try:
                    transform = QgsCoordinateTransform(layer.crs(), dest_crs, self.project)
                    extent = transform.transformBoundingBox(extent)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "全图：图层 %s 范围坐标转换失败，已跳过", layer.name(), exc_info=True
                    )
                    continue
            if combined is None:
                combined = extent
            else:
                combined.combineExtentWith(extent)
        if combined is None or combined.isNull():
            QMessageBox.information(self, "提示", "当前无有效图层")
            return
        self.canvas.setExtent(combined)
        self.canvas.refresh()

    def _on_refresh(self) -> None:
        self.canvas.refresh()
