"""图层动作与右键菜单。

图层树 / 工程管理 Dock 共用的 QGIS 式右键菜单构建，以及重命名、复制粘贴样式、
移动、删除、缩放至图层等动作。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsCsException,
    QgsMapLayerStyle,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QPoint
from qgis.PyQt.QtGui import QAction
from qgis.PyQt.QtWidgets import (
    QMenu,
    QMessageBox,
)

from app.infra.logging import get_logger
from app.ui.common.icons import Icons


def _snapshot_tree_node(node):
    """图层树节点 → 可反复粘贴的快照；不支持的节点返回 ``None``。

    **不在这里 ``clone()``**：实测 clone 会给图层**新建一个 provider 并读元数据**
    （1 个要素的 shp 就要 35ms，图层越重越慢），用户在 QGIS 里觉得复制粘贴很快，
    正是因为复制那步只记**数据源引用**、粘贴时才建图层。这里照做：

    - 普通图层 → ``("layer", 数据源, 名字, provider, 样式 xml)``（都是轻量值）；
    - **内存图层**例外 → 直接存 ``clone()``：内存图层的数据活在 provider 实例里，
      按数据源重建会**丢掉全部要素**；
    - 组 → ``("group", 组名, [子快照…])``，递归。
    """
    from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer

    if isinstance(node, QgsLayerTreeLayer):
        layer = node.layer()
        if layer is None:
            return None
        if layer.providerType() == "memory":
            return ("clone", layer.clone())
        style_xml = None
        try:
            sm = layer.styleManager()
            style_xml = sm.style(sm.currentStyle()).xmlData()
        except Exception:  # noqa: BLE001 —— 取不到样式就只记数据源
            logger.debug("复制图层时读取样式失败：%s", layer.name(), exc_info=True)
        return ("layer", layer.source(), layer.name(), layer.providerType(), style_xml)
    if isinstance(node, QgsLayerTreeGroup):
        children = [
            snap for snap in (_snapshot_tree_node(c) for c in node.children())
            if snap is not None
        ]
        return ("group", node.name(), children)
    return None


def _restore_tree_snapshot(win, snapshot, parent_group, at: int) -> int:
    """把一份快照还原到 ``parent_group`` 的 ``at`` 位，返回新建的**图层**数。

    ``at`` 由调用方**逐项递增**传入：全都插到 0 会把顺序倒过来（用户报「粘贴后
    里面的图层顺序反掉了」）。
    """
    from qgis.core import QgsLayerTreeGroup, QgsRasterLayer, QgsVectorLayer

    kind = snapshot[0]
    if kind == "clone":
        layer = snapshot[1].clone()   # 内存图层：再克隆一份，剪贴板可反复粘贴
        win.project.addMapLayer(layer, False)
        parent_group.insertLayer(at, layer)
        return 1
    if kind == "layer":
        _, source, name, provider, style_xml = snapshot
        layer = QgsVectorLayer(source, name, provider)
        if not layer.isValid():
            layer = QgsRasterLayer(source, name, provider)
        if not layer.isValid():
            win.messageBar.pushMessage(
                f"粘贴失败：{name}", level=Qgis.MessageLevel.Warning, duration=3
            )
            return 0
        if style_xml:
            try:
                layer.importNamedStyle(QgsMapLayerStyle(style_xml))
            except Exception:  # noqa: BLE001 —— 样式没套上不影响图层可用
                logger.debug("粘贴图层时套用样式失败：%s", name, exc_info=True)
        win.project.addMapLayer(layer, False)
        parent_group.insertLayer(at, layer)
        return 1
    if kind == "group":
        _, name, children = snapshot
        sub = QgsLayerTreeGroup(name)
        parent_group.insertChildNode(at, sub)
        # 新组是空组，子节点从 0 开始按原顺序逐个追加
        return sum(
            _restore_tree_snapshot(win, c, sub, i) for i, c in enumerate(children)
        )
    return 0

logger = get_logger(__name__)


class LayerActionsMixin:
    """LayerActionsMixin：见模块 docstring。"""

    # ===================================================== 图层右键菜单

    def _on_layer_tree_blank_context_menu(self, global_pos: QPoint) -> None:
        """图层树**空白处**右键：添加组 / 全部展开 / 全部折叠 / 粘贴图层或组。

        这几项都不针对某个节点（粘贴默认落到工程根），所以单独一套菜单。
        """
        menu = QMenu(self)
        act_add = menu.addAction("添加组")
        act_add.triggered.connect(self._on_add_layer_group)
        menu.addSeparator()
        act_expand = menu.addAction("全部展开")
        act_expand.triggered.connect(self._on_header_expand_all)
        act_collapse = menu.addAction("全部折叠")
        act_collapse.triggered.connect(self._on_header_collapse_all)
        menu.addSeparator()
        act_paste = menu.addAction("粘贴图层 / 组")
        act_paste.setEnabled(bool(getattr(self, "_layer_clipboard", None)))
        act_paste.triggered.connect(lambda: self._paste_tree_nodes(self.project.layerTreeRoot()))
        menu.exec(global_pos)

    def _on_layer_group_context_menu(self, group, global_pos: QPoint) -> None:
        """图层组右键菜单。

        图层面板的组节点在走"图层菜单"时被 ``layer is None`` 挡掉了，于是右键组
        什么也没有（用户报过）。这里给组一套自己的菜单：缩放到组 / 复制组 /
        粘贴图层或组 / 重命名组 / 添加组 / 移除组。

        各动作直接**闭包捕获**右键的那个组，不依赖"当前选中节点"——右键的未必是
        已选中的那个（与图层菜单同样的取舍）。
        """
        from qgis.core import QgsLayerTreeGroup

        if not isinstance(group, QgsLayerTreeGroup):
            return
        menu = QMenu(self)
        act_zoom = menu.addAction("缩放到组")
        act_zoom.triggered.connect(lambda: self._zoom_to_layer_group(group))
        menu.addSeparator()
        act_copy = menu.addAction("复制组")
        act_copy.triggered.connect(lambda: self._copy_tree_nodes([group]))
        act_paste = menu.addAction("粘贴图层 / 组")
        act_paste.setEnabled(bool(getattr(self, "_layer_clipboard", None)))
        act_paste.triggered.connect(lambda: self._paste_tree_nodes(group))
        menu.addSeparator()
        act_rename = menu.addAction("重命名组")
        act_rename.triggered.connect(lambda: self._rename_layer_group(group))
        act_add = menu.addAction("添加组")
        act_add.triggered.connect(lambda: self._add_layer_group_under(group))
        menu.addSeparator()
        act_remove = menu.addAction("移除组")
        act_remove.triggered.connect(lambda: self._remove_layer_group(group))
        menu.exec(global_pos)

    # ---------------------------------------------------------- 组菜单动作

    def _zoom_to_layer_group(self, group) -> None:
        """缩放到组：合并组内**全部子图层**（含嵌套子组）的范围。

        与「全图」同一套口径：每个图层的 extent 先换算到画布 CRS 再合并，否则
        经纬度（度）与投影（米）混在一起会偏。
        """
        from qgis.core import QgsLayerTreeGroup

        if not isinstance(group, QgsLayerTreeGroup):
            return
        dest_crs = self.canvas.mapSettings().destinationCrs()
        combined = None
        for node in group.findLayers():
            layer = node.layer()
            if layer is None or not layer.isValid():
                continue
            extent = layer.extent()
            if extent.isNull():
                continue
            if layer.crs() != dest_crs:
                try:
                    transform = QgsCoordinateTransform(layer.crs(), dest_crs, self.project)
                    extent = transform.transformBoundingBox(extent)
                except Exception:  # noqa: BLE001 —— 脏数据的越界范围，跳过该图层
                    logger.warning("缩放到组：图层 %s 范围转换失败，已跳过", layer.name(), exc_info=True)
                    continue
            if combined is None:
                combined = QgsRectangle(extent)
            else:
                combined.combineExtentWith(extent)
        if combined is None or combined.isNull():
            self.messageBar.pushMessage(
                "该图层组里没有可定位的图层", level=Qgis.MessageLevel.Info, duration=3
            )
            return
        # 组里全是点（或共线）时范围是**零尺寸**的：注意 ``isEmpty()`` 对零尺寸范围
        # 返回 True，所以这里**不能**拿它当"空的"判据（会把点图层组误判成没内容），
        # 只按宽高是否 > 0 补一圈 —— 与「放大至选中要素」同一套做法。
        if combined.width() <= 0 or combined.height() <= 0:
            center = combined.center()
            half = 0.001
            combined = QgsRectangle(
                center.x() - half, center.y() - half, center.x() + half, center.y() + half
            )
        self.canvas.setExtent(combined)
        self.canvas.refresh()

    def _rename_layer_group(self, group) -> None:
        """重命名图层组（空名 / 取消则不改）。"""
        from qgis.PyQt.QtWidgets import QInputDialog

        if group is None:
            return
        name, ok = QInputDialog.getText(self, "重命名图层组", "组名：", text=group.name())
        name = (name or "").strip()
        if not ok or not name or name == group.name():
            return
        group.setName(name)
        self.messageBar.pushMessage(f"已重命名为：{name}", level=Qgis.MessageLevel.Info, duration=2)

    def _copy_tree_nodes(self, nodes) -> None:
        """把图层 / 组快照进剪贴板（组连同子节点一起，可反复粘贴）。"""
        snapshots = [
            snap for snap in (_snapshot_tree_node(n) for n in nodes if n is not None)
            if snap is not None
        ]
        if not snapshots:
            return
        self._layer_clipboard = snapshots
        self.messageBar.pushMessage(
            f"已复制 {len(snapshots)} 项（右键图层组可粘贴）",
            level=Qgis.MessageLevel.Info,
            duration=3,
        )

    def _paste_tree_nodes(self, target_group) -> None:
        """把剪贴板里的图层 / 组粘到 ``target_group`` **末尾**（每次粘贴都是新的克隆）。

        粘到末尾而不是 0：图层树里 **0 = 最上层**，粘在最前面会盖住原有图层，
        用户还要手动往下挪（用户报「空白处粘贴后跑到最顶部了」）。顺序由调用方
        逐项递增的 ``at`` 保证，不再倒序。
        """
        from qgis.core import QgsLayerTreeGroup

        items = getattr(self, "_layer_clipboard", None) or []
        if not items:
            return
        if not isinstance(target_group, QgsLayerTreeGroup):
            target_group = self.project.layerTreeRoot()
        at = len(target_group.children())
        added = 0
        for snap in items:
            added += _restore_tree_snapshot(self, snap, target_group, at)
            at += 1
        # **不调 refreshAllLayers**：那是"整幅重画"，图层多 / 有栅格时很贵；
        # 图层树桥接本来就会把新图层同步进画布并触发重绘（实测粘贴后图层正常上屏）。
        self.messageBar.pushMessage(
            f"已粘贴 {added} 个图层", level=Qgis.MessageLevel.Info, duration=3
        )

    def _on_layer_context_menu(self, layer, global_pos: QPoint) -> None:
        """图层树 / 工程管理 Dock 右键 → 同一套 QGIS 式图层菜单。

        图层由发信号的 Dock 用 ``index2node`` 自己解析后带出来（两个 Dock 都
        只发信号），所以这里不再拿显示名去 ``mapLayersByName`` 回查——那样
        同名图层会拿到错的那个。
        """
        self._build_layer_context_menu(layer, global_pos)

    def _build_layer_context_menu(self, layer, global_pos: QPoint) -> None:
        """QGIS 式图层右键菜单（图层树与工程 Dock 共用）。

        菜单项统一作用在 ``self._selected_layer`` 上；各动作方法（缩放/移分组/
        属性表/样式/移除/导出/属性）读的都是这个字段，所以进入时先把它指向
        当前右键的图层。

        「设为编辑图层」入口已移除：编辑图层统一由点「开始编辑」时的弹窗选择
        （见 :meth:`_choose_edit_layer`）。

        布局贴近 QGIS 原生图层面板：缩放至图层 / 放大至选中要素 → 打开属性表 /
        执行SQL（仅矢量）→ 重命名 / 复制图层 → 样式（含复制/粘贴样式）→ 移到
        分组 → 移除 / 导出 / 属性。
        """
        if layer is None:
            return
        self._selected_layer = layer
        is_vector = isinstance(layer, QgsVectorLayer)
        menu = QMenu(self)
        # 缩放至图层
        act_zoom = QAction("缩放至图层", self)
        act_zoom.triggered.connect(lambda: self._zoom_to_layer(self._selected_layer))
        menu.addAction(act_zoom)
        # 放大至选中要素（有选中才显示，与 QGIS 一致）
        if is_vector and layer.selectedFeatureCount() > 0:
            act_sel = QAction("放大至选中要素", self)
            act_sel.triggered.connect(lambda: self._zoom_to_selection(self._selected_layer))
            menu.addAction(act_sel)
        # 属性表 / SQL（栅格没有要素/表，不显示）
        if is_vector:
            menu.addSeparator()
            act_attr = QAction("打开属性表", self)
            act_attr.triggered.connect(self._open_attribute_table)
            menu.addAction(act_attr)
            act_sql = QAction("执行SQL", self)
            act_sql.triggered.connect(self._open_execute_sql)
            menu.addAction(act_sql)
        # 重命名 / 复制图层
        menu.addSeparator()
        act_rename = QAction("重命名", self)
        act_rename.triggered.connect(lambda: self._rename_layer(self._selected_layer))
        menu.addAction(act_rename)
        act_dup = QAction("复制图层", self)
        act_dup.triggered.connect(lambda: self._duplicate_layer(self._selected_layer))
        menu.addAction(act_dup)
        # 样式
        sub_style = menu.addMenu("样式")
        if is_vector:
            edit_style = QAction("编辑样式", self)
            edit_style.triggered.connect(lambda: self._show_style_editor(self._selected_layer))
            sub_style.addAction(edit_style)
        open_style = QAction("打开样式配置", self)
        open_style.triggered.connect(lambda: self._open_style_file(self._selected_layer))
        sub_style.addAction(open_style)
        save_style = QAction("保存样式", self)
        save_style.triggered.connect(lambda: self._save_style(self._selected_layer))
        sub_style.addAction(save_style)
        sub_style.addSeparator()
        copy_style = QAction("复制样式", self)
        copy_style.triggered.connect(lambda: self._copy_layer_style(self._selected_layer))
        sub_style.addAction(copy_style)
        paste_style = QAction("粘贴样式", self)
        paste_style.setEnabled(bool(getattr(self, "_style_clipboard", None)))
        paste_style.triggered.connect(lambda: self._paste_layer_style(self._selected_layer))
        sub_style.addAction(paste_style)
        # 工程分组：移动图层归属（创建工程后才显示）
        if getattr(self, "_data_group", None) is not None and getattr(self, "_ref_group", None) is not None:
            menu.addSeparator()
            act_to_ref = QAction("移到参考图层", self)
            act_to_ref.triggered.connect(lambda: self._move_layer_to_group(self._selected_layer, self._ref_group))
            menu.addAction(act_to_ref)
            act_to_data = QAction("移到数据图层", self)
            act_to_data.triggered.connect(lambda: self._move_layer_to_group(self._selected_layer, self._data_group))
            menu.addAction(act_to_data)
        # 移除 / 导出 / 属性
        menu.addSeparator()
        act_remove = QAction("移除", self)
        act_remove.setIcon(Icons.remove())
        act_remove.triggered.connect(lambda: self._remove_layer(self._selected_layer))
        menu.addAction(act_remove)
        if is_vector:
            # 导出走 QgsVectorLayerSaveAsDialog，仅矢量
            act_export = QAction("导出", self)
            act_export.triggered.connect(lambda: self._export_layer(self._selected_layer))
            menu.addAction(act_export)
        act_props = QAction("属性", self)
        act_props.triggered.connect(lambda: self._open_layer_properties(self._selected_layer))
        menu.addAction(act_props)
        menu.exec(global_pos)

    def _zoom_to_selection(self, layer) -> None:
        """放大至选中要素（与 QGIS 图层面板一致）。"""
        if layer is None or layer.selectedFeatureCount() <= 0:
            return
        ext = QgsRectangle()
        for feat in layer.selectedFeatures():
            ext.combineExtentWith(feat.geometry().boundingBox())
        if ext.isNull():
            return
        # 单点/零面积（点的 bbox isEmpty() 为真，但几何本身有效）：先扩一圈
        # 再设置，避免 setExtent 拿到空范围
        if ext.width() <= 0 or ext.height() <= 0:
            center = ext.center()
            half = 0.001
            ext = QgsRectangle(
                center.x() - half,
                center.y() - half,
                center.x() + half,
                center.y() + half,
            )
        dest_crs = self.canvas.mapSettings().destinationCrs()
        if layer.crs() != dest_crs:
            xform = QgsCoordinateTransform(layer.crs(), dest_crs, self.project)
            ext = xform.transformBoundingBox(ext)
        self.canvas.setExtent(ext)
        self.canvas.refresh()

    def _rename_layer(self, layer) -> None:
        """重命名图层：layer.setName 会同步树节点名（已实测）。"""
        if layer is None:
            return
        from qgis.PyQt.QtWidgets import QInputDialog

        new_name, ok = QInputDialog.getText(self, "重命名图层", "图层名称：", text=layer.name())
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == layer.name():
            return
        layer.setName(new_name)
        self.canvas.refresh()
        self.messageBar.pushMessage(f"已重命名为：{new_name}", level=Qgis.MessageLevel.Info, duration=2)

    def _duplicate_layer(self, layer) -> None:
        """复制图层（与 QGIS 图层菜单一致）。

        - 矢量：内存副本（同一 provider 几何/字段/要素），挂到原图层所在分组
        - 栅格：直接指向同一数据源的新图层
        副本默认带上原图层的当前样式。
        """
        if layer is None:
            return
        if isinstance(layer, QgsVectorLayer):
            geom = QgsWkbTypes.displayString(layer.wkbType())
            copy = QgsVectorLayer(f"{geom}?crs={layer.crs().authid()}", f"{layer.name()}_副本", "memory")
            dp = copy.dataProvider()
            dp.addAttributes(layer.fields().toList())
            copy.updateFields()
            dp.addFeatures(list(layer.getFeatures()))
            copy.updateExtents()
        elif isinstance(layer, QgsRasterLayer):
            copy = QgsRasterLayer(layer.source(), f"{layer.name()}_副本")
            if not copy.isValid():
                QMessageBox.warning(self, "复制图层", "无法复制该栅格图层")
                return
        else:
            return
        # 复制当前样式
        try:
            sm = layer.styleManager()
            copy_sm = copy.styleManager()
            copy_sm.addStyle("copied", sm.style(sm.currentStyle()))
            copy_sm.setCurrentStyle("copied")
        except Exception:  # noqa: BLE001
            pass
        # 加到原图层所在分组（找不到就挂到树根，走 _add_layer_to_project）
        node = self.project.layerTreeRoot().findLayer(layer)
        group = node.parent() if node is not None else None
        self.project.addMapLayer(copy, False)
        if group is not None:
            group.addLayer(copy)
        else:
            self._add_layer_to_project(copy)
        self.canvas.refresh()
        self.messageBar.pushMessage(f"已复制图层：{copy.name()}", level=Qgis.MessageLevel.Info, duration=2)

    def _copy_layer_style(self, layer) -> None:
        """复制当前样式（xml 存到剪贴板，粘贴样式时用）。"""
        if layer is None:
            return
        sm = layer.styleManager()
        self._style_clipboard = sm.style(sm.currentStyle()).xmlData()
        self.messageBar.pushMessage(f"已复制 {layer.name()} 的样式", level=Qgis.MessageLevel.Info, duration=2)

    def _paste_layer_style(self, layer) -> None:
        """把复制的样式贴到目标图层。"""
        if layer is None:
            return
        xml = getattr(self, "_style_clipboard", None)
        if not xml:
            QMessageBox.information(self, "粘贴样式", "请先「复制样式」")
            return
        sm = layer.styleManager()
        sm.addStyle("pasted", QgsMapLayerStyle(xml))
        sm.setCurrentStyle("pasted")
        layer.triggerRepaint()
        self.canvas.refresh()
        self.messageBar.pushMessage(f"已粘贴样式到 {layer.name()}", level=Qgis.MessageLevel.Info, duration=2)

    def _zoom_to_layer(self, layer) -> None:
        if layer is None:
            return
        dest_crs = self.canvas.mapSettings().destinationCrs()
        if layer.crs() != dest_crs:
            xform = QgsCoordinateTransform(layer.crs(), dest_crs, self.project)
            try:
                ext = xform.transformBoundingBox(layer.extent())
            except QgsCsException:
                # 脏数据(几何坐标范围与声明 CRS 不符)越界时回退到原始范围
                logger.warning(
                    "缩放到图层 %s 越界(%s→%s), 使用原始范围",
                    layer.name(), layer.crs().authid(), dest_crs.authid(),
                )
                ext = layer.extent()
        else:
            ext = layer.extent()
        self.canvas.setExtent(ext)
        self.canvas.refresh()

    def zoom_to_layer_extent_transform(self, extent, source_crs) -> None:
        """缩放到指定范围(属性表右键"缩放到要素"用), 做 CRS 变换到画布坐标系。

        脏几何(坐标范围与声明 CRS 不符, 如把经纬度度值误存进投影图层)在反变换
        时越界抛 ``QgsCsException`` —— 此时回退到原始范围直接缩放, 不崩溃。
        """
        if extent is None or extent.isNull():
            return
        dest_crs = self.canvas.mapSettings().destinationCrs()
        if source_crs is not None and source_crs.isValid() and source_crs != dest_crs:
            xform = QgsCoordinateTransform(source_crs, dest_crs, self.project)
            try:
                extent = xform.transformBoundingBox(extent)
            except QgsCsException:
                logger.warning(
                    "缩放到要素越界(%s→%s), 使用原始范围",
                    source_crs.authid(), dest_crs.authid(),
                )
        self.canvas.setExtent(extent)
        self.canvas.refresh()

    def _move_layer_to_group(self, layer, group) -> None:
        """把图层节点移动到指定分组。

        不能只 ``takeChild`` 再 ``addLayer``：`QgsLayerTreeRegistryBridge`（由
        ``QgsProject`` 自动创建、负责树与图层注册表同步）会在图层**在树里没有任何
        节点**时把图层从工程删掉——事件循环一跑图层就没了，"移动"看起来没生效。
        所以先 `group.addLayer` 在目标组挂新节点（图层始终在树里），再摘原节点。
        """
        if layer is None or group is None:
            return
        node = self.project.layerTreeRoot().findLayer(layer)
        if node is not None and node.parent() is not group:
            group.addLayer(layer)  # 先挂新节点，保证图层仍在树里
            node.parent().takeChild(node)  # 再摘原节点（bridge 不会删图层）
            self.messageBar.pushMessage(
                f"已把 {layer.name()} 移到「{group.name()}」", level=Qgis.MessageLevel.Info, duration=2
            )

    def _remove_layer(self, layer, *, notify: bool = True) -> None:
        """移除图层：清引用 + 刷新画布。

        ``project.removeMapLayer`` 只会把图层从 project 树摘掉，
        画布仍保留最后一次 render 缓存 → 必须显式 ``refresh()`` 否则看到
        残留图形。

        关键时序：``canvas.refresh()`` 不能在 ``removeMapLayer`` 返回后立刻
        调用——我们还在 ``QAction.triggered`` 信号栈里，QGIS 这时尝试
        render 会撞上半移除状态的 layer，导致 0xC0000409。
        用 :class:`QTimer.singleShot` 推到下一个事件循环 tick，等
        ``layersRemoved`` 信号链彻底走完再画。

        关键时序二：需要从 ``layer`` 读的东西必须在 ``removeMapLayer`` **之前**
        读完。``QgsProject`` 对图层持有所有权，``removeMapLayer(layer_id)`` 会
        连 C++ 对象一起 ``delete``——之后再碰 ``layer``（例如为了判断 gpkg 别名
        去读 ``layer.source()``）会抛 ``RuntimeError: wrapped C/C++ object ...
        has been deleted``，把下面排刷新的那段整段跳过，症状就是「图层从面板里
        没了，画布上图形还留在原地」。
        """
        from qgis.PyQt.QtCore import QTimer

        if layer is None:
            return
        layer_id = layer.id()
        layer_name = layer.name()
        # 必须在 removeMapLayer 之前读：工程对图层持有所有权，removeMapLayer
        # 会连 C++ 对象一起 delete（见 docstring「关键时序二」）。
        src = str(getattr(layer, "source", lambda: "")() or "")
        # 如果该图层正在编辑，先退出（避免脏 buffer 滞留）
        was_editing = layer.isEditable()
        if self.edit_layer is layer and was_editing:
            self.editing_service.stop(layer, save=False)
        # 清空本地引用（在 removeMapLayer 之前，避免后续回调引用悬空 layer）
        if self._selected_layer is layer:
            self._selected_layer = None
        if self.edit_layer is layer:
            self.edit_layer = None
            # 工具条也回滚到"无编辑图层"态
            if hasattr(self, "digitizing_toolbar"):
                try:
                    self.digitizing_toolbar.set_edit_target(None)
                except Exception:  # noqa: BLE001
                    pass
            snap_tb = getattr(self, "snapping_toolbar", None)
            if snap_tb is not None:
                try:
                    snap_tb.set_current_layer(None)
                except Exception:  # noqa: BLE001
                    pass

        # 真正从 project 摘掉
        self.project.removeMapLayer(layer_id)
        # 若该图层是经 .gpkg 别名打开的老 .db gpkg 内容库(源为 “x.gpkg” /
        # “x.gpkg|layername=t”), 移除后别名已无活引用 → 延迟删除它。
        # 新规 .gpkg 本体库也以 .gpkg 结尾, 但 release_gpkg_alias 内部有
        # _is_our_alias 闸门(需同 stem .db 存在且为 gpkg 内容) → 不会误删。
        alias_candidate = src.split("|", 1)[0].strip()
        if alias_candidate.lower().endswith(".gpkg"):
            QTimer.singleShot(0, lambda: self._release_gpkg_alias_retry(alias_candidate, 0))
        # 延迟刷新画布到下一个事件循环 tick，让 layersRemoved 信号链走完
        QTimer.singleShot(0, self._deferred_refresh_after_remove)
        if notify:
            self.messageBar.pushMessage(f"已移除图层：{layer_name}", level=Qgis.MessageLevel.Info, duration=3)

    def _deferred_refresh_after_remove(self) -> None:
        """延迟到事件循环下一拍才刷新画布（避免半移除状态 native 崩溃）。"""
        try:
            self.canvas.refresh()
        except Exception:  # noqa: BLE001
            pass
