"""图层树操作。

图层树头部工具（样式面板/删除/展开折叠）、新建分组、图层或分组上下移动、
设为编辑图层与绘制完成后的处理。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

from contextlib import suppress
from typing import Optional

from qgis.core import (
    Qgis,
    QgsLayerTreeGroup,
    QgsRasterLayer,
    QgsVectorDataProvider,
    QgsVectorLayer,
)
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QVBoxLayout,
)

from app.infra.logging import get_logger
from app.ui.common.message_box import MessageBox

logger = get_logger(__name__)


class LayerTreeOpsMixin:
    """LayerTreeOpsMixin：见模块 docstring。"""

    @property
    def edit_layer(self) -> "Optional[QgsVectorLayer]":
        """当前编辑目标图层（**唯一真源**）。

        工具条按钮、捕捉当前层、撤销栈钩子、下拉框都从这里派生。赋值统一走
        setter：除了记录引用，还把停编钩子 / 捕捉目标 / 下拉框一次性同步过去，
        避免某个入口只改了 ``edit_layer`` 却漏掉联动（数据源重设时就是这种情况）。
        """
        return getattr(self, "_edit_layer", None)

    @edit_layer.setter
    def edit_layer(self, layer: "Optional[QgsVectorLayer]") -> None:
        if getattr(self, "_edit_layer", None) is layer:
            return
        self._edit_layer = layer
        toolbar = getattr(self, "digitizing_toolbar", None)
        if toolbar is not None:
            try:
                toolbar.set_edit_target(layer)
                # 下拉框只显示不持有状态：每次目标变化都从 edit_layer 重新渲染
                syncer = getattr(toolbar, "sync_edit_layer_combo", None)
                if callable(syncer):
                    syncer()
            except Exception:  # noqa: BLE001 —— 窗口收尾阶段工具条可能已销毁
                try:
                    layer_name = layer.name() if layer is not None else None
                except RuntimeError:
                    layer_name = "<已销毁>"
                logger.exception("同步编辑工具条失败: layer=%s", layer_name)
        snap_tb = getattr(self, "snapping_toolbar", None)
        if snap_tb is not None:
            try:
                snap_tb.set_current_layer(layer)
            except Exception:  # noqa: BLE001 —— 同上
                logger.exception("同步捕捉工具条失败")

    def _editable_layers(self) -> "list[QgsVectorLayer]":
        """工程中**当前可见**且可添加要素的矢量图层，按图层树显示顺序返回。

        下拉框 / 选层弹窗都只列可见图层：隐藏图层在画布上既看不到要素，
        点选工具也只查可见图层，选中它作为编辑目标后会出现「节点编辑拖了
        没反应、选中不了、分割按钮不亮」——直接不让它进列表最省事。
        可见性变化时由工具条的下拉框刷新（见 ``DigitizingToolbar`` 的
        ``visibilityChanged`` 钩子）。

        不能直接遍历 ``project.mapLayers()``：它底层是 ``QMap<QString, layer>``，
        按图层 id（UUID）排序，看起来就是乱序（用户报过「下拉框图层顺序是乱的」）。
        ``layerTreeRoot().findLayers()`` 返回的才是图层面板里从上到下的顺序。
        """
        editable = {
            layer.id(): layer
            for layer in self.project.mapLayers().values()
            if isinstance(layer, QgsVectorLayer)
            and not layer.readOnly()
            and bool(
                layer.dataProvider()
                and layer.dataProvider().capabilities()
                & QgsVectorDataProvider.Capability.AddFeatures
            )
        }
        ordered: list[QgsVectorLayer] = []
        root = self.project.layerTreeRoot()
        if root is not None:
            try:
                for node in root.findLayers():
                    layer = node.layer()
                    if layer is None or layer.id() not in editable:
                        continue
                    # isVisible() 是**有效**可见性（含祖先组），组被关掉也算隐藏
                    if not node.isVisible():
                        continue
                    ordered.append(editable.pop(layer.id()))
            except Exception:  # noqa: BLE001 —— 图层树异常时退回工程顺序
                logger.exception("按图层树顺序整理可编辑图层失败")
        # 不在图层树里 / 隐藏的图层不进编辑列表（"编辑列表只显示可见图层"）
        return ordered

    def _choose_edit_layer(self) -> "Optional[QgsVectorLayer]":
        """「开始编辑」的选层弹窗：列出工程中可添加要素的矢量图层供选择。

        只负责**选择并返回**（取消返回 None）——把选中层落地为编辑目标
        （edit_layer / 停旧层 / 工具条 / 磁性捕捉联动）由工具条的
        :meth:`DigitizingToolbar.start_edit_fun` 统一调用
        :meth:`_apply_edit_target` 完成，避免职责散落。

        编辑图层入口已收敛到这里——图层树右键「设为编辑图层」与点击自动预设
        均已移除。
        """
        candidates = self._editable_layers()
        if not candidates:
            MessageBox.information(
                self, "提示", "工程中没有可见且可编辑的矢量图层，请先勾选显示图层"
            )
            return None
        # 默认高亮：当前编辑图层 → 图层树选中项 → 第一个
        if self.edit_layer in candidates:
            default = self.edit_layer
        elif self._selected_layer in candidates:
            default = self._selected_layer
        else:
            default = candidates[0]

        dlg = QDialog(self)
        dlg.setWindowTitle("选择编辑图层")
        dlg.resize(380, 420)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("请选择需要编辑的图层："))
        list_widget = QListWidget(dlg)
        for layer in candidates:
            list_widget.addItem(layer.name())
        list_widget.setCurrentRow(candidates.index(default))
        layout.addWidget(list_widget)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        layout.addWidget(buttons)
        picked = None

        def on_ok() -> None:
            nonlocal picked
            row = list_widget.currentRow()
            if 0 <= row < len(candidates):
                picked = candidates[row]
            dlg.accept()

        buttons.accepted.connect(on_ok)
        buttons.rejected.connect(dlg.reject)
        list_widget.itemDoubleClicked.connect(lambda *_: on_ok())
        dlg.exec()
        return picked

    def _apply_edit_target(
        self, layer: "Optional[QgsVectorLayer]", *, announce: bool = True
    ) -> bool:
        """把 ``layer`` 设为编辑目标图层并联动工具条 / 磁性捕捉。

        仅指定目标（记录 ``self.edit_layer`` + 刷新按钮），不进入编辑态；真正的
        ``layer.startEditing()`` 由工具条的「开始编辑」执行。若之前有别的图层在
        编辑，先停掉（避免遗留脏 buffer）。

        **旧图层有未提交修改时会先问「保存 / 放弃 / 取消」**（见
        :meth:`_confirm_switch_with_changes`）—— 直接 ``stop(save=False)`` 会把用户
        刚改的东西静悄悄丢掉，那是数据损失，不能默默做。

        :param announce: 是否提示「已选 X 为编辑图层」。下拉框在**未编辑**状态下
            只切目标时传 ``False``（用户还要再点「开始编辑」，提示留到那时）；
            编辑中切层会自动进入编辑，照常提示。

        :returns: 是否已切到 ``layer``。用户在提示里选了「取消」→ ``False``，
            调用方**不要**接着进编辑态（否则等于绕过了用户的选择）。
        """
        if layer is None or not isinstance(layer, QgsVectorLayer):
            return False
        # 如果之前有别的图层在编辑，先停掉（避免遗留脏 buffer）
        prev = self.edit_layer
        if prev is not None and prev is not layer and prev.isEditable():
            if prev.isModified():
                if not self._confirm_switch_with_changes(prev):
                    return False
            self.editing_service.stop(prev, save=False)

        # 赋值即完成联动：setter 会同步工具条（含下拉框）、停编钩子和捕捉目标
        self.edit_layer = layer
        # 下拉框 / 选层框列的是"可编辑"图层，不区分显隐。选到隐藏图层时画布上
        # 看不到要素，点选工具也只查可见图层（canvas.layers()）→ 表现为
        # "节点编辑拖了没反应、选中不了、分割按钮不亮"。选中目标时顺手显示它。
        shown = self._ensure_layer_visible(layer)
        if announce:
            self._announce_edit_layer(layer, shown=shown)
        return True

    def _ensure_layer_visible(self, layer: "QgsVectorLayer") -> bool:
        """让编辑目标在画布上可见（连同祖先组），返回是否做了改动。"""
        if layer is None:
            return False
        try:
            root = self.project.layerTreeRoot()
            node = root.findLayer(layer.id())
        except (AttributeError, RuntimeError):
            return False
        if node is None:
            return False
        try:
            if node.isVisible():
                return False
            parent = node.parent()
            while isinstance(parent, QgsLayerTreeGroup) and parent is not root:
                if not parent.itemVisibilityChecked():
                    parent.setItemVisibilityChecked(True)
                parent = parent.parent()
            if not node.itemVisibilityChecked():
                node.setItemVisibilityChecked(True)
        except RuntimeError:
            return False
        return True

    def _announce_edit_layer(
        self, layer: "QgsVectorLayer", *, shown: bool = False
    ) -> None:
        """「已选 X 为编辑图层」提示（切层 / 开始编辑共用同一句文案）。"""
        text = f"已选 {layer.name()} 为编辑图层"
        if shown:
            text += "（该图层原为隐藏，已自动显示）"
        self.messageBar.pushMessage(
            text,
            level=Qgis.MessageLevel.Info,
            duration=3,
        )

    def _confirm_switch_with_changes(self, layer: "QgsVectorLayer") -> bool:
        """旧编辑图层有未提交改动 → 问「保存 / 放弃 / 取消」。

        :returns: ``True`` = 可以继续切换（已保存，或用户选择放弃）；
            ``False`` = 用户取消，流程要中止。
        """
        box = MessageBox(self)
        box.setWindowTitle("切换编辑图层")
        box.setIcon(MessageBox.Icon.Warning)
        box.setText(f"图层「{layer.name()}」有未保存的修改。")
        box.setInformativeText("切换到其它图层前，请先处理这些修改：")
        save_btn = box.addButton("保存", MessageBox.ButtonRole.AcceptRole)
        drop_btn = box.addButton("放弃", MessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", MessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_btn:
            return self.editing_service.stop(layer, save=True)
        if clicked is drop_btn:
            self.editing_service.stop(layer, save=False)
            return True
        return False

    def _toggle_first_imagery(self) -> None:
        """显隐**第一个**影像（栅格）图层 —— 「编辑设置」里那条快捷键的动作。

        只切可见性复选框，不动图层顺序 / 选中状态；项目里一个栅格都没有时静默返回
        （快捷键按下去没反应总比弹个错误框好）。
        """
        root = self.project.layerTreeRoot()
        if root is None:
            return
        for layer in self.project.mapLayers().values():
            if not isinstance(layer, QgsRasterLayer):
                continue
            node = root.findLayer(layer.id())
            if node is not None:
                node.setItemVisibilityChecked(not node.isVisible())
            return

    def _set_all_layers_visible(self, visible: bool) -> None:
        """打开 / 关闭所有图层（图层树勾选状态与画布同步变化）。

        与 QGIS 桌面 ``showAllLayers / hideAllLayers`` 一致：显示时把根组及
        全部子节点置为勾选；关闭时逐个取消根组子节点（根节点本身不参与显示）。
        """
        root = self.project.layerTreeRoot()
        if root is None:
            return
        if visible:
            root.setItemVisibilityCheckedRecursive(True)
        else:
            for child in root.children():
                child.setItemVisibilityCheckedRecursive(False)
        self.messageBar.pushMessage(
            "已打开所有图层" if visible else "已关闭所有图层",
            level=Qgis.MessageLevel.Info,
            duration=2,
        )

    def _on_layer_clicked_in_dock(self, index) -> None:
        """用户在图层树点击一层：仅记录为 ``_selected_layer``。

        不再自动设为编辑图层——编辑图层统一在点「开始编辑」时弹窗选择
        （见 :meth:`_choose_edit_layer`），避免"点了哪层就编辑哪层"的隐式切换。
        """
        # 注意：本构建没有 ``indexToNode``，只有 ``index2node``（两者都有
        # 的版本里行为一致）；index 直接来自 view 的模型，无需再 map。
        try:
            node = self.layer_dock.view.index2node(index)
        except Exception:  # noqa: BLE001
            return
        if node is None:
            return
        # node 可能是 group / layer / legend node；只要带 ``layer()`` 方法的就拿
        layer_getter = getattr(node, "layer", None)
        if not callable(layer_getter):
            return
        layer = layer_getter()
        if layer is None:
            return
        self._selected_layer = layer

    # ---------------------------------------------------- 图层树头部工具

    @staticmethod
    def _normalize_dock_node(node):
        """把图例项等子项上溯到所在图层节点（组 / 根节点原样返回）。"""
        from qgis.core import QgsLayerTreeGroup

        try:
            while (
                node is not None
                and node.parent() is not None
                and not isinstance(node.parent(), QgsLayerTreeGroup)
            ):
                node = node.parent()
        except RuntimeError:  # 节点已被别的入口删除
            return None
        return node

    def _current_dock_node(self):
        """图层树当前选中节点；图例项自动上溯到所在图层节点。

        **以真实选中集为准**：``currentNode()`` 只是"当前行"，没选中任何行时
        也可能指着第一项 —— 拿它当"用户选了什么"会让上移 / 新建组 / 删除
        默认作用到第一个图层（用户报过删除的问题）。没有选中项返回 ``None``。
        多选时优先用 ``currentNode()``（若它也在选中集里），否则取第一个选中项。
        """
        selected = self._selected_dock_nodes()
        if not selected:
            return None
        view = self.layer_dock.view
        try:
            current = self.layer_dock.project_node(
                self._normalize_dock_node(view.currentNode())
            )
        except Exception:  # noqa: BLE001
            current = None
        for node in selected:
            if node is current:
                return node
        return selected[0]

    def _on_header_render_tab(self) -> None:
        """头部「打开图层渲染设置」：弹自建图层属性对话框并跳到「渲染」tab。

        走 :class:`app.ui.dialogs.layer_properties.dialog.LayerPropertiesDialog`
        （继承本应用主题/无边框标题栏） + 跳到"渲染"tab，不再复用 QGIS 原生
        :class:`QgsRendererPropertiesDialog`——后者样式与本应用 chrome 不一致。
        """
        try:
            layer = self.layer_dock.view.currentLayer()
        except Exception:  # noqa: BLE001
            layer = None
        if layer is None:
            layer = getattr(self, "_selected_layer", None)
        if layer is None:
            self.messageBar.pushMessage(
                "请先在图层列表中选择要设置渲染的图层",
                level=Qgis.MessageLevel.Warning,
                duration=3,
            )
            return
        self._show_render_tab(layer)

    def _selected_dock_nodes(self) -> list:
        """图层树里**真正被选中**的节点（Shift / Ctrl 多选的都算）。

        不用 ``currentNode()``：它只是"当前行"，并不代表用户选中了什么 ——
        树里刚插入图层时当前行可能自动落在第一项上，于是"什么都没选"点删除
        也会删掉第一个图层（用户报的）。``selectedNodes()`` 为空就是真的没选。

        图例项等子项往上归到所在图层节点；结果去重并保持选择顺序。
        """
        view = self.layer_dock.view
        try:
            nodes = list(view.selectedNodes())
        except Exception:  # noqa: BLE001 —— 极端版本 / 视图已销毁
            nodes = []
        out: list = []
        seen: set[int] = set()
        for node in nodes:
            # 「按数据源展示」里选中的是独立树里的副本节点：先映射回工程节点，
            # 否则删除 / 上移下移 / 分组只作用于视图，工程里的图层纹丝不动
            node = self.layer_dock.project_node(node)
            node = self._normalize_dock_node(node)
            if node is None or id(node) in seen:
                continue
            seen.add(id(node))
            out.append(node)
        return out

    def _on_header_remove(self) -> None:
        """头部「删除图层/组」：删除**所有选中节点**（支持 Shift / Ctrl 多选）。

        - 只选一个图层 → 直接走 :meth:`_remove_layer`
        - 只选一个组 → 确认后递归移除组里全部图层，再摘掉组节点
        - 多选（图层 / 组混合）→ **一次确认**后全部删除；选中的组已经包含了
          里面的图层，所以组内图层 / 嵌套子组不再重复处理
        - 什么都没选 → 提示，**不再**默认拿第一个图层开刀
        """
        from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer

        nodes = []
        for node in self._selected_dock_nodes():
            try:
                if node.parent() is None:  # 树根（正常选不中，兜底别把整棵树删了）
                    continue
            except RuntimeError:  # 节点已被别的入口删除
                continue
            if isinstance(node, (QgsLayerTreeLayer, QgsLayerTreeGroup)):
                nodes.append(node)
        if not nodes:
            self.messageBar.pushMessage(
                "请先在图层列表中选择要删除的图层或组",
                level=Qgis.MessageLevel.Warning,
                duration=3,
            )
            return

        if len(nodes) == 1:
            node = nodes[0]
            if isinstance(node, QgsLayerTreeLayer):
                layer = node.layer()
                if layer is not None:
                    self._remove_layer(layer, confirm=True)
                return
            if isinstance(node, QgsLayerTreeGroup):
                self._remove_layer_group(node)
                return

        # 多选：选中的组里已经包含的节点不再单列（否则会被删两遍 / 组删完节点悬空）
        selected_groups = {
            id(node) for node in nodes if isinstance(node, QgsLayerTreeGroup)
        }

        def _inside_selected_group(node) -> bool:
            parent = node.parent()
            while parent is not None:
                if id(parent) in selected_groups:
                    return True
                parent = parent.parent()
            return False

        nodes = [node for node in nodes if not _inside_selected_group(node)]
        layer_nodes = [
            node
            for node in nodes
            if isinstance(node, QgsLayerTreeLayer) and node.layer() is not None
        ]
        group_nodes = [
            node for node in nodes if isinstance(node, QgsLayerTreeGroup)
        ]
        if not layer_nodes and not group_nodes:
            return

        group_layer_count = sum(len(group.findLayers()) for group in group_nodes)
        parts: list[str] = []
        if layer_nodes:
            parts.append(f"{len(layer_nodes)} 个图层")
        if group_nodes:
            parts.append(f"{len(group_nodes)} 个图层组（共 {group_layer_count} 个图层）")
        ret = MessageBox.question(
            self,
            "删除图层",
            f"确定删除选中的{'和'.join(parts)}吗？\n\n"
            "图层组会连同组内全部图层一起删除；此操作不会删除磁盘上的数据文件。",
            MessageBox.StandardButton.Yes | MessageBox.StandardButton.No,
            MessageBox.StandardButton.No,
        )
        if ret != MessageBox.StandardButton.Yes:
            return

        for node in layer_nodes:
            layer = node.layer()
            if layer is not None:
                self._remove_layer(layer, notify=False)
        for group in group_nodes:
            self._remove_layer_group(group, confirm=False, notify=False)
        done: list[str] = []
        if layer_nodes:
            done.append(f"{len(layer_nodes)} 个图层")
        if group_nodes:
            done.append(f"{len(group_nodes)} 个图层组")
        self.messageBar.pushMessage(
            f"已删除 {'和'.join(done)}",
            level=Qgis.MessageLevel.Info,
            duration=3,
        )

    # ---------------------------------------------------- 组节点删除/移动的引用维护

    def _remove_layer_group(self, group, *, confirm: bool = True, notify: bool = True) -> None:
        """确认后递归删除组里**全部图层**，再摘掉组节点本身（含嵌套空子组）。

        供头部「删除图层/组」与组右键菜单「移除组」共用。

        ``confirm=False`` / ``notify=False``：多选批量删除时由调用方**统一确认、
        统一提示**，避免一组弹一个确认框、一串消息条。
        """
        if group is None or group.parent() is None:
            return  # 树根（正常选不中，兜底别把整棵树删了）
        parent = group.parent()
        # findLayers 默认递归收集所有子图层（嵌套子组下的也算）
        layers = [c.layer() for c in group.findLayers() if c.layer() is not None]
        name = group.name()
        if confirm:
            text = f"确定删除「{name}」及其 {len(layers)} 个图层吗？" if layers else f"确定删除空图层组「{name}」吗？"
            ret = MessageBox.question(
                self,
                "删除图层组",
                text,
                MessageBox.StandardButton.Yes | MessageBox.StandardButton.No,
                MessageBox.StandardButton.No,
            )
            if ret != MessageBox.StandardButton.Yes:
                return
        # 1) 先逐个走 _remove_layer：拿到完整清理（退出编辑态/清选中引用/
        #    gpkg 别名/延迟刷新）。若只摘组节点，bridge 会直接 delete 里面的
        #    图层，正在编辑的图层会留下脏 buffer 与悬空 edit_layer。
        for layer in layers:
            self._remove_layer(layer, notify=notify)
        # 2) 再摘掉组节点本身（空组、嵌套空子组）。原实现只删子图层就返回，
        #    空组永远删不掉、非空组删完还留个空壳——看起来就是「移除失效」。
        #    删节点前先清视图当前项，避免 Qt 持有即将析构节点的持久索引。
        self._clear_view_current()
        # 组被删后 _data_group/_ref_group 会悬空，先清引用再摘节点
        self._forget_group_refs(group)
        parent.removeChildNode(group)
        if notify:
            self.messageBar.pushMessage(f"已删除图层组：{name}", level=Qgis.MessageLevel.Info, duration=3)

    def _clear_view_current(self) -> None:
        """清掉图层树视图的当前项与选中项。

        删除（或克隆替换）节点前调用：QgsLayerTreeView 会为当前节点持有持久
        索引，节点析构后再有事件处理就可能堆损坏（见 :meth:`_on_add_layer_group`
        注释里的 0xc0000374）。
        """
        from qgis.PyQt.QtCore import QModelIndex

        try:
            view = self.layer_dock.view
            view.selectionModel().clear()
            view.setCurrentIndex(QModelIndex())
        except Exception:  # noqa: BLE001
            pass

    def _group_subtree_groups(self, node) -> list:
        """``node`` 自身及递归子组（不含非组节点）；``node`` 非组时返回空表。"""
        from qgis.core import QgsLayerTreeGroup

        if not isinstance(node, QgsLayerTreeGroup):
            return []
        return [node, *node.findGroups()]

    def _forget_group_refs(self, removed) -> None:
        """``removed`` 子树即将删除：清掉指向其内部组的工程分组引用。

        ``_data_group`` / ``_ref_group`` 是裸 C++ 指针，组被删后继续用会抛
        ``RuntimeError: wrapped C/C++ object ... has been deleted``。清掉并把
        工程 Dock 复位，避免删掉「数据图层 / 参考图层」后下一次导图层就崩。
        """
        stale = self._group_subtree_groups(removed)
        changed = False
        for attr in ("_data_group", "_ref_group"):
            grp = getattr(self, attr, None)
            if grp is not None and any(grp is g for g in stale):
                setattr(self, attr, None)
                changed = True
        if changed:
            dock = getattr(self, "project_dock", None)
            if dock is not None:
                with suppress(Exception):
                    dock.reset()

    def _remap_group_refs(self, old_node, new_node) -> None:
        """组节点被克隆替换后，把指向 ``old_node`` 的工程分组引用改指新节点。

        上下移动用的是 clone + 删原节点，节点身份会变；若移的正是「数据图层 /
        参考图层」，裸指针与工程 Dock 的 rootIndex 必须跟着改指克隆，否则原节点
        销毁后悬空。
        """
        stale = self._group_subtree_groups(old_node)
        if not stale:
            return
        changed = False
        for attr in ("_data_group", "_ref_group"):
            grp = getattr(self, attr, None)
            if grp is None:
                continue
            if grp is old_node:
                setattr(self, attr, new_node)
                changed = True
            elif any(grp is g for g in stale):
                setattr(self, attr, None)
                changed = True
        if not changed:
            return
        dock = getattr(self, "project_dock", None)
        if dock is None:
            return
        data = getattr(self, "_data_group", None)
        ref = getattr(self, "_ref_group", None)
        try:
            if data is not None and ref is not None:
                dock.set_project(self.project.title(), data, ref)
            else:
                dock.reset()
        except Exception:  # noqa: BLE001
            pass

    def _on_header_expand_all(self) -> None:
        """头部「全部展开」：展开整个图层树。"""
        self.layer_dock.view.expandAll()

    def _on_header_collapse_all(self) -> None:
        """头部「全部折叠」：折叠整个图层树（保留根节点）。"""
        self.layer_dock.view.collapseAll()

    def _on_add_layer_group(self) -> None:
        """新建图层组（与 QGIS 一致）：选中分组 → 建在该组最顶；

        选中图层 → 建在所在分组最顶；未选中 → 建在根最顶。新建后选中并尝试
        就地重命名（失败则保留默认名）。
        """
        from qgis.core import QgsLayerTreeGroup

        node = self._current_dock_node()
        if isinstance(node, QgsLayerTreeGroup):
            parent = node
        elif node is not None and isinstance(node.parent(), QgsLayerTreeGroup):
            parent = node.parent()
        else:
            parent = self.project.layerTreeRoot()
        self._add_layer_group_under(parent)

    def _add_layer_group_under(self, parent) -> None:
        """在 ``parent`` 下新建图层组（默认名去重 → 插入 → 尝试就地重命名）。

        供头部「添加图层组」与组右键菜单「添加组」共用。
        """
        from qgis.core import QgsLayerTreeGroup

        parent = parent if isinstance(parent, QgsLayerTreeGroup) else self.project.layerTreeRoot()
        # 默认名去重（父分组内查重）
        base = "新建图层组"
        name = base
        i = 2
        while parent.findGroup(name) is not None:
            name = f"{base} {i}"
            i += 1

        group = QgsLayerTreeGroup(name)
        parent.insertChildNode(0, group)
        view = self.layer_dock.view
        # QGIS 行为：新建组后选中并直接进入重命名。两者都只在视图已聚焦时做
        # （未显示/未聚焦的视图上强设"当前节点"或开编辑器，会在该节点随后被
        # 移除时令 Qt 持有失效持久索引，触发堆损坏 0xc0000374）。
        if not view.hasFocus():
            self.messageBar.pushMessage(f"已新建图层组：{name}", level=Qgis.MessageLevel.Info, duration=2)
            return
        try:
            index = view.node2index(group)
            if index.isValid():
                view.setCurrentIndex(index)
        except Exception:  # noqa: BLE001
            pass
        try:
            index = view.node2index(group)
            if index.isValid():
                view.edit(index)
        except Exception:  # noqa: BLE001
            pass
        self.messageBar.pushMessage(f"已新建图层组：{name}", level=Qgis.MessageLevel.Info, duration=2)

    def _on_move_layer_node(self, delta: int) -> None:
        """上移 / 下移当前选中的图层或组（仅在同分组内重排）。"""
        node = self._current_dock_node()
        if node is None or node.parent() is None:
            self.messageBar.pushMessage(
                "请先在图层列表中选择要移动的图层或组",
                level=Qgis.MessageLevel.Warning,
                duration=3,
            )
            return
        parent = node.parent()
        siblings = parent.children()
        try:
            pos = siblings.index(node)
        except ValueError:
            return
        target_pos = pos + delta
        if not 0 <= target_pos < len(siblings):
            return  # 已在顶/底
        self._move_dock_node(node, target_pos, "上移" if delta < 0 else "下移")

    def _move_dock_node_to(self, node, where: str) -> None:
        """把节点移到同组顶部 / 上一位 / 下一位 / 底部（右键菜单用）。"""
        from qgis.core import QgsLayerTreeGroup

        if node is None:
            return
        try:
            parent = node.parent()
        except RuntimeError:  # 节点已被别的入口删除
            return
        if not isinstance(parent, QgsLayerTreeGroup):
            return
        siblings = parent.children()
        try:
            pos = siblings.index(node)
        except ValueError:
            return
        if where == "top":
            target_pos = 0
        elif where == "up":
            target_pos = pos - 1
        elif where == "down":
            target_pos = pos + 1
        elif where == "bottom":
            target_pos = len(siblings) - 1
        else:
            return
        if not 0 <= target_pos < len(siblings) or target_pos == pos:
            return
        verb = {"top": "置顶", "up": "上移", "down": "下移", "bottom": "置底"}[where]
        self._move_dock_node(node, target_pos, verb)

    def _move_dock_node(self, node, target_pos: int, verb: str) -> None:
        """按 clone + insert + remove 三步把节点移动到同组 ``target_pos``。

        严格按 QGIS 桌面 ``qgslayertreeviewdefaultactions.cpp`` 里
        ``moveToTop / moveToBottom`` 的写法，**不是** ``takeChild +
        insertChildNode``。

        为何不能直接 takeChild：``takeChild`` 不会发 ``willRemoveChildren`` 信号，
        ``QgsLayerTreeRegistryBridge`` 收不到"将要删节点"的钩子，于是直接
        ``project.removeMapLayer(layer)`` 把 layer 从工程中抹掉——肉眼看上去
        就是"图层消失"。

        正确顺序：先 ``insertChildNode`` 把克隆挂到新位置（layer 立刻在新位置
        有 node），再 ``removeChildNode`` 删旧 node（bridge 收到
        willRemoveChildren 时检查到 layer 还有 cloned 那个 node 挂着，**不**删
        layer，只删旧 node）。**insert 位置要按目标方向偏移一格**：

        - 目标在当前位置之前：插到 ``target_pos`` 即落在目标位置之前，删原节点
          后自然到位。
        - 目标在当前位置之后：原节点还占着位，插到 ``target_pos + 1``，删原节点
          后才落回 ``target_pos``（否则下移等于没动）。

        clone 是新对象，``view.setCurrentNode`` / messageBar 都用 cloned。
        """
        from qgis.core import QgsLayerTreeGroup

        if node is None:
            return
        try:
            parent = node.parent()
        except RuntimeError:  # 节点已被别的入口删除
            return
        if not isinstance(parent, QgsLayerTreeGroup):
            return
        siblings = parent.children()
        try:
            pos = siblings.index(node)
        except ValueError:
            return
        if not 0 <= target_pos < len(siblings) or target_pos == pos:
            return

        cloned = node.clone()
        # 1) 先把克隆挂到新位置（layer 立即在树里有 node，bridge 不会干 layer）。
        insert_pos = target_pos + (1 if target_pos > pos else 0)
        parent.insertChildNode(insert_pos, cloned)
        # 2) 若移的是「数据图层 / 参考图层」这类工程分组，克隆替换了节点身份，
        #    先把裸指针 / Dock 的 rootIndex 改指克隆，再删原节点。
        self._remap_group_refs(node, cloned)
        # 3) 删旧节点前清视图当前项（旧节点马上析构，别留持久索引），
        #    之后再把当前项设回克隆。
        self._clear_view_current()
        parent.removeChildNode(node)

        view = self.layer_dock.view
        with suppress(Exception):
            view.setCurrentNode(cloned)
        self.messageBar.pushMessage(
            f"已{verb}：{getattr(cloned, 'name', lambda: '')()}",
            level=Qgis.MessageLevel.Info,
            duration=2,
        )

    # ---------------------------------------------------- 绘图工具回调（保留兼容）

    def _on_draw_finished(self, geometry) -> None:
        """DrawManager.finished 信号：跳到 DigitizingToolbar.finish_draw。"""
        if hasattr(self, "digitizing_toolbar"):
            self.digitizing_toolbar.finish_draw(geometry)
