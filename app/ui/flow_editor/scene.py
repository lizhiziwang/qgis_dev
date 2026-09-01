"""流程编排：画布场景。

:class:`FlowScene` 是「图模型 ↔ 图形项」的协调者——节点/连线的增删、
端口拖拽连线、依赖链高亮、状态回写都由这里发起。图模型
（:class:`FlowGraph`）是**单一事实来源**，场景负责把它的变化渲染成
图形项，并把图形交互（移动 / 选中 / 连线）写回模型。

对外信号供主窗口联动：选中节点刷新参数面板、结构变化标记脏、操作提示写日志。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush
from qgis.PyQt.QtWidgets import QGraphicsItem, QGraphicsScene

from app.core.domain.flow.algorithm_library import (
    BUILTIN_TEMPLATES,
    describe_processing_algorithm,
    instantiate_template,
)
from app.core.domain.flow.model import FlowGraph, NodeSpec, PortRef
from app.core.domain.flow.topo import ancestors, dependencies, dependents, descendants

from .edge_item import EdgeItem
from .flow_theme import apply_theme as apply_flow_theme
from .flow_theme import current as current_palette
from .node_item import NodeItem, PortItem


class FlowScene(QGraphicsScene):
    """流程图场景。"""

    node_selected = pyqtSignal(str)  # node_id（'' 表示取消选中）
    node_double_clicked = pyqtSignal(str)  # node_id（双击重命名）
    graph_changed = pyqtSignal()  # 结构 / 参数变化（标记脏、刷新面板）
    status_message = pyqtSignal(str)  # 操作提示（写日志 / 状态栏）
    edge_created = pyqtSignal(str, str)  # src_node, dst_node

    def __init__(self, graph: FlowGraph | None = None, parent=None) -> None:
        super().__init__(parent)
        self._graph = graph or FlowGraph()
        self._node_items: dict[str, NodeItem] = {}
        self._edge_items: dict[str, EdgeItem] = {}
        self._temp_edge: EdgeItem | None = None
        self._drag_port: PortItem | None = None
        #: 待销毁的图形项。removeItem 之后场景/视图仍可能挂着未处理的移除重绘，
        #: 此刻释放图形项的 C++ 对象会在下次重绘访问悬空内存 → 崩溃。且删除后
        #: 「何时重绘完成」不可靠（真实窗口里定时释放会踩中正在进行的重绘）。
        #: 因此这里一直保留这些项，只在下一次 rebuild() 的 clear()（Qt 批量清理，
        #: 保证无未处理更新）之后再清空释放。scene.clear() 走 Qt 内部清理不会踩坑。
        self._deleted_items: list[QGraphicsItem] = []
        self.setBackgroundBrush(QBrush(current_palette().canvas_bg))
        self.rebuild()

    # ------------------------------------------------------------ 属性 / 重建

    @property
    def graph(self) -> FlowGraph:
        return self._graph

    def set_graph(self, graph: FlowGraph) -> None:
        """整体换图（加载 JSON 后调用），全量重建。"""
        self._graph = graph
        self.rebuild()

    def rebuild(self) -> None:
        """按图模型全量重建图形项。"""
        self.clear()
        # 场景已全量清空，之前删除时收进墓碑的图形项此刻可安全释放
        self._deleted_items.clear()
        self._node_items.clear()
        self._edge_items.clear()
        for node in self._graph.nodes.values():
            self._create_node_item(node)
        self._rebuild_edges()

    def apply_theme(self, theme) -> None:
        """画布跟随全局主题：切换调色板 + 背景刷 + 现有图形项重绘。"""
        apply_flow_theme(theme)
        pal = current_palette()
        self.setBackgroundBrush(QBrush(pal.canvas_bg))
        for item in self._node_items.values():
            item.apply_theme()
        for item in self._edge_items.values():
            item.apply_theme()

    def _create_node_item(self, node: NodeSpec) -> NodeItem:
        item = NodeItem(node)
        self.addItem(item)
        self._node_items[node.id] = item
        if not node.enabled:
            item.setOpacity(0.55)
        return item

    def _rebuild_edges(self) -> None:
        for item in list(self._edge_items.values()):
            self.removeItem(item)
        self._edge_items.clear()
        for edge in self._graph.edges.values():
            src = self._node_items.get(edge.src_node)
            dst = self._node_items.get(edge.dst_node)
            if src is None or dst is None:
                continue
            src_item = src.port_items.get(edge.src_port)
            dst_item = dst.port_items.get(edge.dst_port)
            if src_item is None or dst_item is None:
                continue
            item = EdgeItem(src_item, dst_item)
            item.edge_id = edge.id
            self.addItem(item)
            self._edge_items[edge.id] = item

    def node_item(self, node_id: str) -> NodeItem | None:
        return self._node_items.get(node_id)

    def current_selected_node(self) -> str | None:
        for item in self.selectedItems():
            if isinstance(item, NodeItem):
                return item.node_id
        return None

    # ------------------------------------------------------------ 节点操作

    def add_template(self, key: str, x: float, y: float) -> str | None:
        """按模板 key 建节点（内置模板或处理算法 id）。返回新节点 id。"""
        template = BUILTIN_TEMPLATES.get(key)
        if template is None:
            template = describe_processing_algorithm(key)
        if template is None:
            self.status_message.emit(f"未找到模板：{key}")
            return None
        node = instantiate_template(self._graph, template, x, y)
        self._create_node_item(node)
        self.graph_changed.emit()
        return node.id

    def remove_node(self, node_id: str) -> None:
        if node_id not in self._graph.nodes:
            return
        item = self._node_items.pop(node_id, None)
        if item is not None:
            # 先隐藏再移除：直接 removeItem 时场景 BSP 索引可能残留该节点所在区域，
            # 导致删除后画布上仍绘制出这个节点；hide() 会立即停止绘制并清理索引。
            # 延迟释放（墓碑）逻辑不变，见 __init__ 里 _deleted_items 的注释。
            item.hide()
            self.removeItem(item)
            self._deleted_items.append(item)
        # 正在拖线的端口 / 临时线挂在被删节点上 → 一并取消，避免悬空引用
        if self._drag_port is not None and self._drag_port.node_id == node_id:
            self._cancel_temp()
        self._graph.remove_node(node_id)
        self._rebuild_edges()
        self.graph_changed.emit()

    def sync_node(self, node_id: str) -> None:
        """端口增删 / 参数变更后，重建该节点的端口与相关连线。"""
        node = self._graph.nodes.get(node_id)
        if node is None:
            return
        item = self._node_items.get(node_id)
        if item is None:
            self._create_node_item(node)
        else:
            item.rebuild_ports()
            item.setOpacity(0.55 if not node.enabled else 1.0)
            item.update()
        self._rebuild_edges()
        self.graph_changed.emit()

    def rename_node(self, node_id: str, new_name: str) -> None:
        node = self._graph.nodes.get(node_id)
        if node is None or not new_name:
            return
        node.name = new_name
        item = self._node_items.get(node_id)
        if item is not None:
            item.update()
        self.graph_changed.emit()

    def copy_node(self, node_id: str) -> str | None:
        """复制节点（连同输入/输出端口与代码，偏移 40px）。"""
        src = self._graph.nodes.get(node_id)
        if src is None:
            return None
        node = self._graph.add_node(
            name=src.name + " 副本",
            x=src.x + 40,
            y=src.y + 40,
            code=src.code,
            algorithm_id=src.algorithm_id,
        )
        for ins in src.inputs:
            self._graph.add_input(node.id, ins.name, ins.type, default=ins.default)
        for outs in src.outputs:
            out = self._graph.add_output(node.id, outs.name, outs.type)
            out.save_path = outs.save_path
            out.write_temp = outs.write_temp
            out.load_to_canvas = outs.load_to_canvas
        self._create_node_item(node)
        self.graph_changed.emit()
        return node.id

    def replace_node_from_template(self, node_id: str, template) -> None:
        """把已有节点整体换成某个模板（输入/输出/代码/名称全替换）。

        旧的端口先删（联动清理引用它们的边），再按模板重建。
        """
        node = self._graph.nodes.get(node_id)
        if node is None or template is None:
            return
        for ins in list(node.inputs):
            self._graph.remove_input(node_id, ins.id)
        for outs in list(node.outputs):
            self._graph.remove_output(node_id, outs.id)
        node.name = template.name
        node.code = template.code
        node.algorithm_id = template.key
        for ti in template.inputs:
            self._graph.add_input(node_id, ti.name, ti.type, default=ti.default)
        for to in template.outputs:
            out = self._graph.add_output(node_id, to.name, to.type)
            out.write_temp = to.write_temp
            out.load_to_canvas = to.load_to_canvas
        self.sync_node(node_id)

    def delete_selected(self) -> None:
        """删除当前选中的节点与连线（画布 Delete 键 / 工具条）。"""
        node_ids: list[str] = []
        edge_ids: list[str] = []
        for item in self.selectedItems():
            if isinstance(item, NodeItem):
                node_ids.append(item.node_id)
            elif isinstance(item, EdgeItem) and item.edge_id:
                edge_ids.append(item.edge_id)
        changed = False
        for eid in edge_ids:
            if eid in self._graph.edges:
                self._graph.remove_edge(eid)
                changed = True
        for nid in node_ids:
            if nid in self._graph.nodes:
                self.remove_node(nid)
                changed = True
        if changed:
            self._rebuild_edges()
            self.graph_changed.emit()

    # ------------------------------------------------------------ 端口连线

    def on_port_pressed(self, port_item: PortItem) -> None:
        """端口按下：若已在拖线则尝试收线，否则开一条临时线。"""
        if self._drag_port is not None:
            if self._drag_port is not port_item:
                self._try_connect(self._drag_port, port_item)
            self._cancel_temp()
        self._drag_port = port_item
        self._temp_edge = EdgeItem(port_item, port_item)
        self.addItem(self._temp_edge)
        self._temp_edge.update_path()

    def on_port_moved(self, port_item: PortItem, scene_pos: QPointF) -> None:
        if self._drag_port is None or self._temp_edge is None:
            return
        self._temp_edge.set_drag_pos(scene_pos)

    def on_port_released(self, port_item: PortItem, scene_pos: QPointF) -> None:
        if self._drag_port is None:
            return
        target = self._port_at(scene_pos, exclude=self._drag_port)
        if target is not None:
            self._try_connect(self._drag_port, target)
        self._cancel_temp()

    def _port_at(self, scene_pos: QPointF, exclude: PortItem | None) -> PortItem | None:
        rect = QRectF(scene_pos.x() - 6, scene_pos.y() - 6, 12, 12)
        for item in self.items(rect, Qt.ItemSelectionMode.IntersectsItemShape):
            if isinstance(item, PortItem) and item is not exclude:
                return item
        return None

    def _try_connect(self, a: PortItem, b: PortItem) -> None:
        """把两个端口连起来（输出→输入，方向自动判定）。"""
        if a.kind == "output" and b.kind == "input":
            src, dst = a, b
        elif a.kind == "input" and b.kind == "output":
            src, dst = b, a
        else:
            self.status_message.emit("只能从输出端口连到输入端口")
            return
        try:
            self._graph.bind_input(dst.node_id, dst.port_id, PortRef(src.node_id, src.port_id))
        except ValueError as exc:  # noqa: BLE001
            self.status_message.emit(f"无法连线：{exc}")
            return
        self._rebuild_edges()
        self.edge_created.emit(src.node_id, dst.node_id)
        self.graph_changed.emit()

    def _cancel_temp(self) -> None:
        if self._temp_edge is not None:
            self.removeItem(self._temp_edge)
            self._temp_edge = None
        self._drag_port = None

    # ------------------------------------------------------------ 上下文菜单

    def on_node_context_action(self, node_id: str, verb: str) -> None:
        """节点右键菜单动作分发（来自 :class:`NodeItem`）。"""
        if verb == "rename":
            self.node_double_clicked.emit(node_id)
        elif verb == "copy":
            self.copy_node(node_id)
        elif verb == "delete":
            self.remove_node(node_id)
        elif verb == "toggle_enabled":
            node = self._graph.nodes.get(node_id)
            if node is not None:
                node.enabled = not node.enabled
                item = self._node_items.get(node_id)
                if item is not None:
                    item.setOpacity(0.55 if not node.enabled else 1.0)
                    item.update()
                self.graph_changed.emit()
        elif verb == "toggle_skip":
            node = self._graph.nodes.get(node_id)
            if node is not None:
                node.skip = not node.skip
                item = self._node_items.get(node_id)
                if item is not None:
                    item.update()
                self.graph_changed.emit()

    def on_edge_context_action(self, edge_id: str) -> None:
        if edge_id in self._graph.edges:
            self._graph.remove_edge(edge_id)
            self._rebuild_edges()
            self.graph_changed.emit()

    def on_node_double_clicked(self, node_id: str) -> None:
        self.node_double_clicked.emit(node_id)

    # ------------------------------------------------------------ 移动 / 选中

    def on_node_moved(self, node_id: str, pos: QPointF) -> None:
        node = self._graph.nodes.get(node_id)
        if node is None:
            return
        node.x, node.y = pos.x(), pos.y()
        for item in self._edge_items.values():
            item.update_path()

    def on_node_selected(self, node_id: str, selected: bool) -> None:
        if selected:
            self.highlight_chain(node_id)
            self.node_selected.emit(node_id)
        else:
            current = self.current_selected_node()
            if current is not None:
                self.highlight_chain(current)
                self.node_selected.emit(current)
            else:
                self.clear_highlight()
                self.node_selected.emit("")

    # ------------------------------------------------------------ 高亮依赖链

    def highlight_chain(self, node_id: str) -> None:
        """高亮 node 及其祖先 / 子孙整条依赖链，其余节点与连线置灰。"""
        chain = {node_id} | ancestors(self._graph, node_id) | descendants(self._graph, node_id)
        for nid, item in self._node_items.items():
            item.setOpacity(1.0 if nid in chain else 0.35)
        for eid, item in self._edge_items.items():
            edge = self._graph.edges[eid]
            inside = edge.src_node in chain and edge.dst_node in chain
            touching = edge.src_node in chain or edge.dst_node in chain
            item.set_highlighted(inside)
            item.setOpacity(1.0 if touching else 0.25)

    def clear_highlight(self) -> None:
        for item in self._node_items.values():
            item.setOpacity(1.0 if item.node.enabled else 0.55)
        for item in self._edge_items.values():
            item.setOpacity(1.0)
            item.set_highlighted(False)

    # ------------------------------------------------------------ 依赖跳转

    def select_ancestor(self) -> bool:
        """选中直接上游（无上游返回 False 并提示）。"""
        nid = self.current_selected_node()
        if nid is None:
            self.status_message.emit("请先选中一个节点")
            return False
        deps = sorted(dependencies(self._graph, nid))
        if not deps:
            self.status_message.emit("该节点没有上游")
            return False
        self._select_node(deps[0])
        return True

    def select_descendant(self) -> bool:
        """选中直接下游（无下游返回 False 并提示）。"""
        nid = self.current_selected_node()
        if nid is None:
            self.status_message.emit("请先选中一个节点")
            return False
        depd = sorted(dependents(self._graph, nid))
        if not depd:
            self.status_message.emit("该节点没有下游")
            return False
        self._select_node(depd[0])
        return True

    def _select_node(self, node_id: str) -> None:
        self.clearSelection()
        item = self._node_items.get(node_id)
        if item is not None:
            item.setSelected(True)
            self.highlight_chain(node_id)
            self.node_selected.emit(node_id)

    # ------------------------------------------------------------ 运行状态

    def reset_statuses(self) -> None:
        for item in self._node_items.values():
            item.set_status("idle")

    def apply_statuses(self, statuses: dict[str, str]) -> None:
        """运行后把各节点状态刷到节点上（成功绿 / 失败红 / 禁用灰等）。"""
        for nid, status in statuses.items():
            item = self._node_items.get(nid)
            if item is not None:
                item.set_status(status)
