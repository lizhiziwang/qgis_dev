"""流程编排：画布节点与端口图形项。

- :class:`NodeItem`：圆角矩形节点，含标题栏（名称 + 禁用/跳过徽标）、
  左侧输入端口 / 右侧输出端口圆点、状态色（运行中蓝 / 成功绿 / 失败红）。
  支持拖拽移动（回写 ``FlowGraph`` 坐标并刷新连线）、双击重命名、右键菜单。
- :class:`PortItem`：端口小圆点，按端口类型着色；负责发起 / 接收连线拖拽，
  把鼠标事件转发给 ``FlowScene`` 统一协调（避免与场景循环 import）。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QPointF, QRectF, Qt, QVariant
from qgis.PyQt.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPen
from qgis.PyQt.QtWidgets import (
    QGraphicsDropShadowEffect,
    QGraphicsItem,
    QGraphicsObject,
    QMenu,
)

from app.core.domain.flow.executor import (
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
)
from app.core.domain.flow.types import PortType

from .flow_theme import current as current_palette

#: 端口类型 → 端口圆点颜色
PORT_COLORS: dict[PortType, QColor] = {
    PortType.VECTOR: QColor("#7cb342"),
    PortType.RASTER: QColor("#ef6c00"),
    PortType.TABLE: QColor("#8d6e63"),
    PortType.NUMBER: QColor("#2196f3"),
    PortType.BOOLEAN: QColor("#9c27b0"),
    PortType.STRING: QColor("#607d8b"),
    PortType.FILE: QColor("#00bcd4"),
    PortType.FOLDER: QColor("#009688"),
    PortType.FIELDS: QColor("#ff7043"),
}

#: 节点状态 → 标题栏颜色
_STATUS_TITLE_COLORS: dict[str, QColor] = {
    STATUS_SUCCESS: QColor("#2e7d32"),
    STATUS_FAILED: QColor("#c62828"),
    STATUS_RUNNING: QColor("#1565c0"),
    STATUS_SKIPPED: QColor("#757575"),
}

#: 几何常量
WIDTH = 200          #: 节点宽度
TITLE_H = 32         #: 标题栏高度
PORT_H = 28          #: 每行端口高度
PAD = 10             #: 端口区上下留白
RADIUS = 8           #: 主体 / 标题圆角半径
PORT_OVER = 7        #: 端口圆点超出主体左右边缘的宽度（配合阴影取景）
SHADOW_MARGIN = 16   #: 投影阴影在右下扩展的空间（模糊 12 + 偏移 3 = 15）

# 主体/边框/标题底色等中性色随全局主题走 flow_theme（见 paint()）


class PortItem(QGraphicsObject):
    """端口圆点。``kind``：``'input'``（左，白底）| ``'output'``（右，实心）。"""

    def __init__(
        self,
        node_id: str,
        port_id: str,
        port_type: PortType,
        kind: str,
        label: str,
        parent: QGraphicsItem | None = None,
    ) -> None:
        super().__init__(parent)
        self.node_id = node_id
        self.port_id = port_id
        self.port_type = port_type
        self.kind = kind
        self.label = label
        self._hovered = False
        self.setAcceptHoverEvents(True)
        self.setToolTip(f"{label}（{port_type.value}）")

    def boundingRect(self) -> QRectF:  # noqa: N802 —— Qt 命名
        return QRectF(-7, -7, 14, 14)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = PORT_COLORS.get(self.port_type, QColor("#607d8b"))
        # 悬停：柔和光晕
        if self._hovered:
            glow = QColor(color)
            glow.setAlpha(64)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(QPointF(0, 0), 9.0, 9.0)
        if self.kind == "input":
            # 输入：空心插口（主题底 + 彩色环），语义「接受连线」
            painter.setPen(QPen(color, 2.0 if self._hovered else 1.6))
            painter.setBrush(current_palette().node_fill)
            painter.drawEllipse(QPointF(0, 0), 5.5, 5.5)
        else:
            # 输出：实心圆点 + 白色高光芯，语义「提供数据」
            painter.setPen(QPen(color, 1.4))
            painter.setBrush(color)
            painter.drawEllipse(QPointF(0, 0), 5.5, 5.5)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 210))
            painter.drawEllipse(QPointF(0, 0), 1.8, 1.8)

    def hoverEnterEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    # 连线拖拽由 FlowScene 协调（on_port_* 方法），这里只转发事件
    def mousePressEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        scene = self.scene()
        if scene is not None and hasattr(scene, "on_port_pressed"):
            scene.on_port_pressed(self)
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        scene = self.scene()
        if scene is not None and hasattr(scene, "on_port_moved"):
            scene.on_port_moved(self, event.scenePos())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        scene = self.scene()
        if scene is not None and hasattr(scene, "on_port_released"):
            scene.on_port_released(self, event.scenePos())
        event.accept()


class NodeItem(QGraphicsObject):
    """流程节点。标题栏 + 左输入 / 右输出端口 + 状态色 + 移动/选中/右键。"""

    def __init__(self, node, parent: QGraphicsItem | None = None) -> None:
        super().__init__(parent)
        self.node_id = node.id
        self.node = node
        self.port_items: dict[str, PortItem] = {}
        self._status = "idle"
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setPos(node.x, node.y)
        self.setOpacity(0.55 if not node.enabled else 1.0)
        # 柔和投影（现代化浮层感）；颜色随主题亮暗切换
        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(12)
        self._shadow.setOffset(0, 3)
        self.setGraphicsEffect(self._shadow)
        self.rebuild_ports()

    # ------------------------------------------------------------ 几何

    @property
    def width(self) -> float:
        return WIDTH

    @property
    def height(self) -> float:
        return TITLE_H + self._body_height()

    def _body_height(self) -> float:
        rows = max(len(self.node.inputs), len(self.node.outputs), 1)
        return rows * PORT_H + 2 * PAD  # 上下各留 PAD，端口行居中

    def boundingRect(self) -> QRectF:  # noqa: N802 —— Qt 命名
        # 四周把外探的端口圆点 / 选中光晕收进取景区，右下给投影阴影留空间
        return QRectF(
            -PORT_OVER,
            -4,
            self.width + 2 * PORT_OVER + SHADOW_MARGIN,
            self.height + SHADOW_MARGIN + 4,
        )

    def port_pos(self, kind: str, index: int) -> QPointF:
        """端口圆心在节点局部坐标里的位置。"""
        y = TITLE_H + PAD + index * PORT_H + PORT_H / 2
        if kind == "input":
            return QPointF(0, y)
        return QPointF(self.width, y)

    def rebuild_ports(self) -> None:
        """按节点当前的输入/输出重建端口圆点（增删端口后调用）。"""
        for child in list(self.childItems()):
            sc = child.scene()
            if sc is not None:
                sc.removeItem(child)
        self.port_items.clear()
        for i, ins in enumerate(self.node.inputs):
            item = PortItem(self.node_id, ins.id, ins.type, "input", ins.name, parent=self)
            item.setPos(self.port_pos("input", i))
            self.port_items[ins.id] = item
        for i, outs in enumerate(self.node.outputs):
            item = PortItem(self.node_id, outs.id, outs.type, "output", outs.name, parent=self)
            item.setPos(self.port_pos("output", i))
            self.port_items[outs.id] = item
        self.update()

    # ------------------------------------------------------------ 绘制

    def paint(self, painter: QPainter, option, widget=None) -> None:
        pal = current_palette()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width, self.height

        # 主体：圆角面板（底色 = 面板色，标题栏下方与上方同底）
        painter.setPen(QPen(pal.node_border, 1))
        painter.setBrush(pal.node_fill)
        painter.drawRoundedRect(QRectF(0, 0, w, h), RADIUS, RADIUS)

        # 标题栏：状态色纵向渐变 + 底部压平（标题栏下缘成直线，与主体干净衔接）
        title_color = (
            pal.title_disabled
            if not self.node.enabled
            else _STATUS_TITLE_COLORS.get(self._status, pal.title_bg)
        )
        grad = QLinearGradient(0, 0, 0, TITLE_H)
        grad.setColorAt(0.0, title_color.lighter(116))
        grad.setColorAt(1.0, title_color)
        painter.setPen(QPen(title_color.darker(110), 1))
        painter.setBrush(QBrush(grad))
        painter.drawRoundedRect(QRectF(0, 0, w, TITLE_H), RADIUS, RADIUS)
        # 把圆角矩形下半截垫平，标题栏底部成直线
        painter.fillRect(QRectF(0, TITLE_H // 2, w, TITLE_H // 2), QBrush(grad))

        # 标题文字（10px 半粗、白色、超长省略号截断）
        painter.setPen(QPen(pal.title_text, 1))
        font = painter.font()
        font.setBold(True)
        font.setPointSize(10)
        painter.setFont(font)
        name = self.node.name or "节点"
        name = painter.fontMetrics().elidedText(
            name, Qt.TextElideMode.ElideRight, int(w - 24)
        )
        painter.drawText(
            QRectF(10, 0, w - 24, TITLE_H),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            name,
        )

        # 禁用/跳过徽标（右上角）
        badge = []
        if not self.node.enabled:
            badge.append("禁用")
        if self.node.skip:
            badge.append("跳过")
        if badge:
            painter.setPen(QPen(pal.badge_text, 1))
            painter.drawText(
                QRectF(0, 0, w - 8, TITLE_H),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                " · ".join(badge),
            )

        # 节点体内输入 / 输出名称
        self._draw_port_labels(painter, pal, w)

        # 选中：半透明光晕 + 2px 强调描边
        if self.isSelected():
            halo = QColor(pal.selected)
            halo.setAlpha(46)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(halo)
            painter.drawRoundedRect(
                QRectF(-3, -3, w + 6, h + 6), RADIUS + 3, RADIUS + 3
            )
            painter.setPen(QPen(pal.selected, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), RADIUS, RADIUS)

    def _draw_port_labels(self, painter: QPainter, pal, w: float) -> None:
        """在节点体内绘制端口名称：左列输入（左对齐）、右列输出（右对齐）。

        名称太长时按列宽省略号截断；端口圆点本身是子项 :class:`PortItem`，
        紧贴左右边缘，名称在其内侧各留 14px 不重叠。
        """
        font = painter.font()
        font.setBold(False)
        font.setPointSize(8)
        painter.setFont(font)
        fm = painter.fontMetrics()
        text_pen = QPen(pal.node_text, 1)
        half = (w - 28) / 2  # 每列可用宽度（两侧各让 14px 给端口圆点）

        # 输入列：圆点内侧 → 名称左对齐
        for i, ins in enumerate(self.node.inputs):
            y = TITLE_H + PAD + i * PORT_H + PORT_H / 2
            name = fm.elidedText(ins.name or "?", Qt.TextElideMode.ElideRight, int(half))
            painter.setPen(text_pen)
            painter.drawText(
                QRectF(14, y - 9, half, 18),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                name,
            )

        # 输出列：名称右对齐 → 圆点
        for i, outs in enumerate(self.node.outputs):
            y = TITLE_H + PAD + i * PORT_H + PORT_H / 2
            name = fm.elidedText(outs.name or "?", Qt.TextElideMode.ElideRight, int(half))
            painter.setPen(text_pen)
            painter.drawText(
                QRectF(w - 14 - half, y - 9, half, 18),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                name,
            )

    # ------------------------------------------------------------ 交互

    def set_status(self, status: str) -> None:
        self._status = status
        self.update()

    def apply_theme(self) -> None:
        """主题切换后重绘（配色由 flow_theme 全局调色板提供）。"""
        self._shadow.setColor(current_palette().node_shadow)
        self.update()

    def itemChange(self, change, value):  # noqa: N802 —— Qt 命名
        # PyQt5（qgis.PyQt 映射）下 item 脱离场景时 ItemSceneHasChanged 的
        # value 是 PyQt_PyObject 型 QVariant（内部 Python 对象已被消费），
        # 原样透传 super() 会因无法转回 Python 对象而 TypeError；而
        # .value() 也会失败（PyQt6 下 value 本就是普通对象，不会进来）。
        if isinstance(value, QVariant):
            try:
                value = value.value()
            except TypeError:
                # 解包失败的 change（如脱离场景）业务上用不到该 value，置 None 安全透传
                value = None
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.scene() is not None:
            scene = self.scene()
            if hasattr(scene, "on_node_moved"):
                scene.on_node_moved(self.node_id, value)
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged and self.scene() is not None:
            scene = self.scene()
            if hasattr(scene, "on_node_selected"):
                scene.on_node_selected(self.node_id, bool(value))
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        scene = self.scene()
        if scene is not None and hasattr(scene, "on_node_double_clicked"):
            scene.on_node_double_clicked(self.node_id)
        event.accept()

    def contextMenuEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        menu = QMenu()

        def add(text: str, verb: str, enabled: bool = True):
            act = menu.addAction(text)
            act.setData(verb)
            act.setEnabled(enabled)
            return act

        add("重命名", "rename")
        add("复制节点", "copy")
        add("删除节点", "delete")
        menu.addSeparator()
        add("禁用节点" if self.node.enabled else "启用节点", "toggle_enabled")
        add("取消跳过" if self.node.skip else "标记跳过", "toggle_skip")

        chosen = menu.exec(event.screenPos())
        if chosen is not None:
            scene = self.scene()
            handler = getattr(scene, "on_node_context_action", None)
            if handler is not None:
                handler(self.node_id, chosen.data())
        event.accept()
