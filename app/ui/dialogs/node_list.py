"""「节点列表」弹窗：把图形的每个节点摊成表，逐点查看 / 插 / 删并定位。

编辑工具条「节点列表」按钮 → 在地图上点一个图形 → 弹出本弹窗。几何的每个**环**
（部件 × 外环 / 内环）是表外的一个页签（`第1个外环` / `第1个内环` / 多部件时前缀
`第N个部件 · `），点哪个就把那一环的节点**完整**列出来：每行一个节点，四列
`勾选` / `ID`（环内从 0 数起，不含闭合重复点）/ `X` / `Y`（**图层 CRS** 的真实坐标）。
按钮行照原型：前插 / 后插 / 删除 / 闪烁 / 平移 / 缩放。

**用平表而不是树**（虽然"按环分组"用树更直观）：树的缩进会把窄的勾选列挤掉，
勾选框画不全；而且环切换本来就更适合放在表外当页签。

**两种行状态并存**（照原型）：点行 = 当前行，给前插 / 后插 / 平移用；勾选 = 集合，
给删除 / 闪烁用。原型区分「点行」与「点勾选框」，Qt 里没有干净的区分方式——点勾选框
也会顺手把当前行挪过去，接受这点差异（它只会让操作更容易成功，而"什么都没选"时的
提示仍然成立）。

**非模态**：闪烁 / 平移 / 缩放这三件事都要在地图上看得见，模态挡住主界面就没有意义。
因此本弹窗**不**改图层选中集（那是 `SelectionOverlay` 与各选择工具的地盘），被检查的
图形由自己的一条红色描边带标出。

**只在编辑态可用**（用户明确约束）：地图工具侧拦住"没开编辑 / 点到别的图层"，本弹窗
再挂一次图层 `editingStopped` 兜底——图层一停止编辑就自己关掉。几何改动一律走
:func:`app.core.domain.editing.edit_command`（撤销栈上一条命令），改完由用户 Ctrl+Z
撤销；**平移与缩放不改几何，也就不开编辑命令**（开了会白白截断 redo 历史）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from qgis.core import (
    QgsCoordinateTransform,
    QgsCsException,
    QgsGeometry,
    QgsPoint,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsVertexId,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core.domain.check.space_index import clear_spatial_index
from app.core.domain.editing import edit_command
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.coord_format import format_coord
from app.ui.tools.feature_attrs import feature_label

logger = get_logger(__name__)

# ---- 列布局：勾选框一列在最前、ID 一列，再是 X / Y ----
_COL_CHECK = 0
_COL_ID = 1
_COL_X = 2
_COL_Y = 3

#: 勾选框列宽（比勾选框本身略宽一点，别浪费地方）
_WIDTH_CHECK = 34
#: ID 列（内容就一两个字符，宽了白占地方）
_WIDTH_ID = 56
#: 行高在字体高度之外的额外留白：X / Y 格要**直接编辑**，编辑器里的文字不能被切
_ROW_EXTRA = 12

#: 每个环至少保留的节点数（原型规则「面要素至少保留 3 个节点」）
MIN_RING_NODES = 3

#: 顶点闪烁：间隔（毫秒）/ 总时长（毫秒）。8 × 325 ≈ 2600，与原型 setTimeout 一致。
_BLINK_INTERVAL_MS = 325
_BLINK_TOTAL_MS = 2600
_BLINK_COLOR = (229, 48, 48)

#: 描边带颜色（被检查的图形）
_OUTLINE_COLOR = QColor(229, 48, 48)

#: 「缩放」时留的视野余量（取要素自身尺寸的比例，约等于让要素占视口 60%）
_ZOOM_PADDING_RATIO = 0.3

_ROW_ROLE = Qt.ItemDataRole.UserRole
#: 待提交的空白行的位置 ``(part, ring, 插入下标)``；真实节点行没有这个值
_PENDING_ROLE = Qt.ItemDataRole.UserRole + 1


# ============================================================ 几何遍历（纯函数）


@dataclass(frozen=True)
class _RingInfo:
    """一个环：``count`` 是**显示的**节点数（已扣掉闭合重复点）。"""

    part: int
    ring: int
    count: int


def _ring_closed(absg, part: int, ring: int) -> bool:
    """环是否闭合（首末顶点 2D 坐标一致，容差 1e-9）。与要素编辑工具同一判据。"""
    n = absg.vertexCount(part, ring)
    if n < 2:
        return False
    a = absg.vertexAt(QgsVertexId(part, ring, 0))
    b = absg.vertexAt(QgsVertexId(part, ring, n - 1))
    return abs(a.x() - b.x()) < 1e-9 and abs(a.y() - b.y()) < 1e-9


def _iter_rings(geom: QgsGeometry) -> list[_RingInfo]:
    """几何的部件 × 环清单。``partCount()`` 而非已移除的 ``numGeometries()``。"""
    out: list[_RingInfo] = []
    if geom is None or geom.isNull() or geom.isEmpty():
        return out
    absg = geom.constGet()
    if absg is None:
        return out
    for part in range(absg.partCount()):
        for ring in range(absg.ringCount(part)):
            n = absg.vertexCount(part, ring)
            if n <= 0:
                continue
            count = n - 1 if _ring_closed(absg, part, ring) else n
            out.append(_RingInfo(part, ring, count))
    return out


def _ring_label(part: int, ring: int, part_count: int) -> str:
    """环的分组标题：`第1个外环` / `第1个内环`；多部件时前缀 `第N个部件 · `。"""
    ring_name = f"第{ring + 1}个外环" if ring == 0 else f"第{ring}个内环"
    if part_count <= 1:
        return ring_name
    return f"第{part + 1}个部件 · {ring_name}"


# ============================================================ 弹窗


class _NoFocusDelegate(QStyledItemDelegate):
    """去掉单元格的**焦点框**。

    主题的 QSS 里没有 ``::item:focus`` 规则，Qt 就按原生样式在「当前单元格」上画一圈
    方框 —— 点一下勾选框旁边的空白就冒出一圈黑边，很难看。这里把 State_HasFocus
    从样式选项里摘掉，别的绘制照旧。
    """

    def initStyleOption(self, option, index) -> None:  # noqa: N802 —— Qt 命名
        super().initStyleOption(option, index)
        option.state &= ~QStyle.StateFlag.State_HasFocus


class NodeListDialog(BaseDialog):
    """一个图形的节点列表；点行闪烁、按钮行做插 / 删 / 定位。"""

    def __init__(self, canvas, toast=None, parent=None) -> None:
        # 小弹窗不需要最小化 / 最大化，标题栏只留关闭
        super().__init__(
            parent, title="节点列表", show_min_button=False, show_max_button=False
        )
        # 必须压掉 BaseDialog 的 800×600 默认最小尺寸
        self.setMinimumSize(460, 380)
        self.resize(620, 540)
        self.setModal(False)

        self._canvas = canvas
        self._project = None
        if canvas is not None:
            try:
                self._project = canvas.project()
            except RuntimeError:
                self._project = None
        self._project = self._project or QgsProject.instance()
        self._toast = toast or (lambda *_a, **_k: None)

        self._layer: Optional[QgsVectorLayer] = None
        self._fid: Optional[int] = None
        self._rings: list[_RingInfo] = []
        #: 当前环在 ``_rings`` 里的下标（由表外的环切换控件决定）
        self._ring_index = 0
        #: 当前待提交的空白行位置 ``(part, ring, 插入下标)``；没有则为 None
        self._pending: Optional[tuple[int, int, int]] = None
        #: 正在提交自己的几何改动（挡住 geometryChanged 引发的自刷新）
        self._applying = False

        #: 收尾状态：初值 True —— 构造期若收到 hideEvent（无边框装配可能触发）直接跳过
        self._torn_down = True
        self._band: Optional[QgsRubberBand] = None
        #: 顶点手柄（常显）。QgsVertexMarker 不是 QObject，必须显式拆（见 _drop_markers）
        self._markers: list[QgsVertexMarker] = []
        #: 闪烁用的临时标记 + 计时器
        self._blink_markers: list[QgsVertexMarker] = []
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(_BLINK_INTERVAL_MS)
        self._blink_timer.timeout.connect(self._on_blink_tick)
        self._blink_left = 0

        self._build_ui()

    # ------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        # 裸构造再 setLayout（BaseDialog 的约定；QVBoxLayout(self) 内容会静默消失）
        layout = QVBoxLayout()
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        for text, slot in (
            ("前插", lambda: self._insert(-1)),
            ("后插", lambda: self._insert(1)),
            ("删除", self._delete),
            ("闪烁", self._blink),
            ("平移", self._pan),
            ("缩放", self._zoom),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            # 等宽铺满一行（原型 .node-toolbar button{flex:1}）
            actions.addWidget(button, 1)
        layout.addLayout(actions)

        # 环切换控件放在**表格外面**：点哪个环，下面就把那个环的节点完整列出来。
        # 用平表（不搞树）是刻意的——树的缩进会挤掉窄的勾选列，勾选框会画不全。
        self.ring_bar = QTabBar(self)
        self.ring_bar.setObjectName("NodeListRingBar")
        self.ring_bar.setDrawBase(False)
        self.ring_bar.setExpanding(False)
        self.ring_bar.currentChanged.connect(self._on_ring_changed)
        layout.addWidget(self.ring_bar)

        self.table = QTableWidget(self)
        self.table.setObjectName("NodeListTable")   # 供 QSS 定位（编辑器等，见 theme.app_qss）
        self.table.setColumnCount(4)
        # 空表头的第 0 列放勾选框、第 1 列放 ID：两列内容都只有一两个字符，给窄一点
        self.table.setHorizontalHeaderLabels(["", "ID", "X", "Y"])
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # 空白行的 X/Y 要能直接双击填写（真实节点行不给编辑，见 _on_item_changed）
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        # 不加黑白间隔：这张表本来就短，斑马纹只是噪声
        self.table.setAlternatingRowColors(False)
        # 行高 = 字体高 + 留白（X / Y 要就地编辑，行太矮时编辑器里的字会被切）
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.verticalHeader().setDefaultSectionSize(
            self.table.fontMetrics().height() + _ROW_EXTRA
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(_COL_CHECK, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_ID, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_X, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COL_Y, QHeaderView.ResizeMode.Stretch)
        header.resizeSection(_COL_CHECK, _WIDTH_CHECK)
        header.resizeSection(_COL_ID, _WIDTH_ID)
        # 点一下当前单元格不该留一圈原生焦点框（见 _NoFocusDelegate）
        self.table.setItemDelegate(_NoFocusDelegate(self.table))
        self.table.currentCellChanged.connect(lambda *_a: self._sync_markers())
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, 1)

        hint = QLabel(
            "点行选中节点（供前插 / 后插 / 平移）；勾选行（供删除 / 闪烁）。"
            "前插 / 后插会插一个空白行，在 X / Y 格填完坐标才会落到图形上。改动可用 Ctrl+Z 撤销。"
        )
        hint.setObjectName("PropHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.close_btn = QPushButton("关闭")
        self.close_btn.setObjectName("DialogPrimary")
        self.close_btn.clicked.connect(self.close)
        bottom.addWidget(self.close_btn)
        layout.addLayout(bottom)

        self.setLayout(layout)

    # ------------------------------------------------------------ 载入

    def load_feature(self, layer: QgsVectorLayer, fid: int) -> None:
        """切到「查看 ``layer`` 的 ``fid``」；重复打开同一要素也重载最新几何。"""
        if self._layer is not None:
            self._disconnect_layer(self._layer)
        self._layer = layer
        self._fid = int(fid)
        self._ring_index = 0   # 换了要素就回到第 1 个环
        self._connect_layer(layer)
        try:
            feature = layer.getFeature(self._fid)
            label = feature_label(feature, layer) if feature.isValid() else str(self._fid)
        except RuntimeError:
            label = str(self._fid)
        self.setWindowTitle(f"节点列表 · {label}")
        self._rebuild(clear_rows=True)

    def _connect_layer(self, layer: QgsVectorLayer) -> None:
        """图层一停止编辑就关掉本弹窗；几何被外部改了就同步刷新列表。"""
        try:
            layer.editingStopped.connect(self._on_editing_stopped)
            layer.geometryChanged.connect(self._on_layer_geometry_changed)
        except Exception:  # noqa: BLE001 —— 连接失败不该挡住弹窗
            logger.warning("节点列表：挂接图层信号失败", exc_info=True)

    def _disconnect_layer(self, layer: QgsVectorLayer) -> None:
        for signal, slot in (
            (layer.editingStopped, self._on_editing_stopped),
            (layer.geometryChanged, self._on_layer_geometry_changed),
        ):
            try:
                signal.disconnect(slot)
            except Exception:  # noqa: BLE001 —— 收尾场景属正常
                pass

    def _on_layer_geometry_changed(self, *_args) -> None:
        """图层几何被**外部**改了（撤销 / 重做 / 别的工具）→ 推迟一拍刷新列表。

        自己的提交不走这里（``_applying`` 挡着，且 ``_commit`` 自己会重建），
        否则重建会插进编辑命令窗口里。不刷新的话，用户 Ctrl+Z 之后列表还停在
        「多一个节点」的状态，跟图形对不上。
        """
        if self._applying or self._torn_down:
            return
        QTimer.singleShot(0, self._rebuild_from_layer)

    def _rebuild_from_layer(self) -> None:
        if self._torn_down:
            return
        self._pending = None
        self._rebuild()

    def _on_editing_stopped(self, *_args) -> None:
        self.close()

    # ------------------------------------------------------------ 刷新

    def _geom(self) -> Optional[QgsGeometry]:
        """图层里该要素的**当前**几何（读操作，不复制）。"""
        if self._layer is None or self._fid is None:
            return None
        try:
            feature = self._layer.getFeature(self._fid)
        except RuntimeError:
            return None
        if not feature.isValid():
            return None
        geom = feature.geometry()
        if geom is None or geom.isNull() or geom.isEmpty():
            return None
        return geom

    def _rebuild(self, *, clear_rows: bool = False) -> None:
        """按图层当前几何重建环页签 + 当前环的节点表。

        状态都以 ``(part, ring, vertex)`` 为键——索引会因插删而移动，键不会。
        **未提交的空白行不跨重建存活**：几何一变（或换了要素/换了环）它就该失效。
        """
        checked = set() if clear_rows else self._checked_keys()
        current = None if clear_rows else self._current_key()
        self._pending = None

        geom = self._geom()
        self._rings = _iter_rings(geom) if geom is not None else []
        if not self._rings:
            self._ring_index = 0
        else:
            self._ring_index = max(0, min(self._ring_index, len(self._rings) - 1))

        part_count = max((r.part for r in self._rings), default=-1) + 1
        self.ring_bar.blockSignals(True)
        while self.ring_bar.count() > len(self._rings):
            self.ring_bar.removeTab(self.ring_bar.count() - 1)
        for index, info in enumerate(self._rings):
            label = _ring_label(info.part, info.ring, part_count)
            if index < self.ring_bar.count():
                self.ring_bar.setTabText(index, label)
            else:
                self.ring_bar.addTab(label)
        self.ring_bar.setCurrentIndex(self._ring_index)
        # **始终显示**：只有一个环时它写着「第1个外环」，是用户确认"我在看哪一环"的依据
        self.ring_bar.setVisible(bool(self._rings))
        self.ring_bar.blockSignals(False)

        self._fill_table(checked, current)
        self._sync_markers()
        self._refresh_canvas()

    def _fill_table(self, checked: set, current) -> None:
        """把**当前环**的节点铺成平表（一行一个节点）。"""
        geom = self._geom()
        absg = geom.constGet() if geom is not None else None
        info = self._current_ring()
        crs = self._layer.crs() if self._layer is not None else None

        self.table.blockSignals(True)
        self.table.clearContents()
        if info is None or absg is None:
            self.table.setRowCount(0)
            self.table.blockSignals(False)
            return
        self.table.setRowCount(info.count)
        for vertex in range(info.count):
            point = absg.vertexAt(QgsVertexId(info.part, info.ring, vertex))
            self._set_row(vertex, vertex, point.x(), point.y(), crs)
            self.table.item(vertex, _COL_CHECK).setCheckState(
                Qt.CheckState.Checked if (info.part, info.ring, vertex) in checked
                else Qt.CheckState.Unchecked
            )
        self.table.blockSignals(False)
        if current is not None:
            self._set_current_key(current)

    def _set_row(self, row: int, vertex: int, x, y, crs) -> None:
        """写一行（勾选格 + ID / X / Y，内容居中）。"""
        check_item = QTableWidgetItem()
        check_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        check_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        check_item.setData(_ROW_ROLE, vertex)
        self.table.setItem(row, _COL_CHECK, check_item)
        for col, text in ((_COL_ID, str(vertex)),
                          (_COL_X, format_coord(x, crs)),
                          (_COL_Y, format_coord(y, crs))):
            item = QTableWidgetItem(text)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
            )
            self.table.setItem(row, col, item)

    # ------------------------------------------------------------ 行状态

    def _current_ring(self) -> Optional[_RingInfo]:
        """当前页签选中的环。"""
        if not self._rings:
            return None
        return self._rings[max(0, min(self._ring_index, len(self._rings) - 1))]

    def _key_at(self, row: int):
        """表格第 ``row`` 行对应的 ``(part, ring, vertex)`` 键；无效/空白行返回 None。"""
        info = self._current_ring()
        if info is None or row < 0 or row >= info.count:
            return None
        item = self.table.item(row, _COL_CHECK)
        if item is not None and item.data(_PENDING_ROLE) is not None:
            return None   # 没填坐标的空白行还不是一个节点
        return (info.part, info.ring, row)

    def _checked_keys(self) -> set[tuple[int, int, int]]:
        """当前环里勾选的行（键带 part/ring，供下游统一处理）。"""
        out = set()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, _COL_CHECK)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                key = self._key_at(row)
                if key is not None:
                    out.add(key)
        return out

    def _checked_rows(self) -> list[int]:
        """勾选的行号（含空白的待填行——它也能被勾上来删掉）。"""
        out = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, _COL_CHECK)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                out.append(row)
        return out

    def _renumber_from(self, row: int) -> None:
        """把 ``row`` 起往下的 ID 与节点下标重排（插/删一行之后调用）。"""
        for r in range(max(0, row), self.table.rowCount()):
            id_item = self.table.item(r, _COL_ID)
            if id_item is not None:
                id_item.setText(str(r))
            check = self.table.item(r, _COL_CHECK)
            if check is not None and check.data(_PENDING_ROLE) is None:
                check.setData(_ROW_ROLE, r)

    def _current_key(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        return self._key_at(row)

    def _item_for(self, key):
        """键对应的行内控件（勾选格）；键不在当前环时返回 None。"""
        info = self._current_ring()
        if info is None or key is None:
            return None
        if (key[0], key[1]) != (info.part, info.ring):
            return None
        row = key[2]
        if row < 0 or row >= self.table.rowCount():
            return None
        return self.table.item(row, _COL_CHECK)

    def _set_current_key(self, key) -> None:
        item = self._item_for(key)
        if item is None:
            return
        self.table.blockSignals(True)
        self.table.setCurrentCell(item.row(), _COL_ID)
        self.table.blockSignals(False)

    def _ring_info(self, part: int, ring: int) -> Optional[_RingInfo]:
        for info in self._rings:
            if info.part == part and info.ring == ring:
                return info
        return None

    def _on_ring_changed(self, index: int) -> None:
        """换环：把该环的节点完整列出来（未提交的空白行随之失效）。"""
        if self._torn_down or index < 0:
            return
        self._ring_index = int(index)
        self._pending = None
        self._fill_table(set(), None)
        self._sync_markers()
        self._refresh_canvas()

    # ------------------------------------------------------------ 六个动作

    def _guard_loaded(self) -> bool:
        """弹窗可用性 + 编辑态兜底（用户约束：只有编辑中才可用）。"""
        if self._layer is None or self._fid is None:
            self._warn("请先在地图上点击要编辑的面要素")
            return False
        try:
            editable = self._layer.isEditable()
        except RuntimeError:
            editable = False
        if not editable:
            self._warn("图层已停止编辑，节点列表不可用")
            self.close()
            return False
        return True

    def _warn(self, text: str) -> None:
        QMessageBox.warning(self, "提示", text)

    def _insert(self, direction: int) -> None:
        """前插（``direction=-1``）/ 后插：在当前节点的前 / 后**插一个空白行**。

        **不自动填坐标**（原型取两节点中点只是为了演示）：新行的 X / Y 留空，用户在
        单元格里自己填，**两格都填成合法数字**才写进几何（见 :meth:`_on_item_changed`）。
        没填完的空白行不进几何、也不占用撤销栈。
        """
        if not self._guard_loaded():
            return
        if self._pending is not None:
            self._warn("上一行坐标还没填完，请先填好或按 Esc 取消该行")
            return
        key = self._current_key()
        if key is None:
            self._warn("请先在表格中点击行选择当前节点")
            return
        part, ring, vertex = key
        info = self._ring_info(part, ring)
        if info is None:
            return
        if direction < 0 and vertex == 0:
            self._warn("已是第一个节点，前插位置越界")
            return
        if direction > 0 and vertex >= info.count - 1:
            self._warn("已是最后一个节点，后插位置越界")
            return
        # 两个越界守卫保证 insert_at 落在环内部，不会插到闭合重复点上
        insert_at = vertex if direction < 0 else vertex + 1
        self.table.blockSignals(True)
        self.table.insertRow(insert_at)
        check_item = QTableWidgetItem()
        # 空白行**也给勾选框**：填到一半不想插了，勾上它点删除就能撤掉这一行
        check_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        check_item.setData(_PENDING_ROLE, (part, ring, insert_at))
        # **必须显式给个勾选态**：只设 ItemIsUserCheckable 而不设 CheckStateRole 的话，
        # 委托拿到的 checkState 是无效值，压根不画勾选框（用户就删不掉这张空白行了）
        check_item.setCheckState(Qt.CheckState.Unchecked)
        self.table.setItem(insert_at, _COL_CHECK, check_item)
        for col, text in ((_COL_ID, str(insert_at)), (_COL_X, ""), (_COL_Y, "")):
            item = QTableWidgetItem(text)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
            )
            if col != _COL_ID:
                # 只有坐标格可编辑；ID 是自动编号，不给改
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsEditable
                )
            self.table.setItem(insert_at, col, item)
        # 插进来之后，它下面那些行的序号要跟着顺延（否则 ID 列会跟几何对不上）
        self._renumber_from(insert_at + 1)
        self.table.blockSignals(False)
        self._pending = (part, ring, insert_at)
        self.table.setCurrentCell(insert_at, _COL_ID)
        # 直接把光标放进 X 格，用户接着敲数字就行
        self.table.editItem(self.table.item(insert_at, _COL_X))
        self._toast(
            f"已在节点 {vertex} {'前' if direction < 0 else '后'}插入空白行：填好 X / Y 后自动落到图形上",
            "info",
        )

    def _on_item_changed(self, item) -> None:
        """行内容变了。

        普通节点行：勾选态变化 → 重画地图标记（勾选的顶点画红）。
        空白行：X / Y 被编辑，两格都填成合法数字才提交到几何。
        """
        row = item.row()
        check_item = self.table.item(row, _COL_CHECK)
        pending = check_item.data(_PENDING_ROLE) if check_item is not None else None
        if pending is None:
            # 普通节点行：改 X / Y 就是移动该节点；勾选则顺便让它成为当前行、并重画标记
            if item.column() in (_COL_X, _COL_Y):
                self._move_vertex_from_row(row)
                return
            if item.column() == _COL_CHECK and item.checkState() == Qt.CheckState.Checked:
                # 勾选即当前行：省得用户"闪烁还要先勾、前插还要先点行"两套操作
                self.table.blockSignals(True)
                self.table.setCurrentCell(row, _COL_ID)
                self.table.blockSignals(False)
            self._sync_markers()
            return
        if item.column() not in (_COL_X, _COL_Y):
            return
        coords: dict[str, float] = {}
        for col, name, label in ((_COL_X, "x", "X"), (_COL_Y, "y", "Y")):
            cell = self.table.item(row, col)
            text = cell.text().strip() if cell is not None else ""
            if not text:
                return  # 还没填完，继续等
            try:
                coords[name] = float(text)
            except ValueError:
                self._warn(f"{label} 坐标必须是数字：{text}")
                self.table.blockSignals(True)
                if cell is not None:
                    cell.setText("")
                self.table.blockSignals(False)
                return
        self._commit_pending(self._pending, coords["x"], coords["y"])

    def _commit_pending(self, key: tuple[int, int, int], x: float, y: float) -> None:
        """把空白行填的坐标真正插进几何（一个编辑命令）。"""
        if not self._guard_loaded():
            self._pending = None
            return
        geom = self._copy_geom()
        if geom is None:
            self._pending = None
            return
        part, ring, insert_at = key
        if not geom.get().insertVertex(QgsVertexId(part, ring, insert_at), QgsPoint(x, y)):
            self._warn("插入节点失败（几何不接受该位置）")
            return
        # 先清掉待提交态：_commit 内部会 _rebuild，而空白行不该跨重建存活
        self._pending = None
        if not self._commit(geom, "插入节点"):
            return
        self._toast(f"已插入节点（{x:g}, {y:g}）", "success")

    def _move_vertex_from_row(self, row: int) -> None:
        """把某行 X / Y 的改动落到几何上（一个编辑命令）——改完地图立刻跟着变。

        与空白行那套一样：**两个格都填成合法数字**才提交，只填一格先等着。
        """
        key = self._key_at(row)
        if key is None:
            return
        coords: dict[str, float] = {}
        for col, name, label in ((_COL_X, "x", "X"), (_COL_Y, "y", "Y")):
            cell = self.table.item(row, col)
            text = cell.text().strip() if cell is not None else ""
            if not text:
                return
            try:
                coords[name] = float(text)
            except ValueError:
                self._warn(f"{label} 坐标必须是数字：{text}")
                return
        geom = self._copy_geom()
        if geom is None:
            return
        part, ring, vertex = key
        was_closed = _ring_closed(geom.constGet(), part, ring)
        if not geom.get().moveVertex(
            QgsVertexId(part, ring, vertex), QgsPoint(coords["x"], coords["y"])
        ):
            self._warn("修改节点坐标失败（几何不接受该位置）")
            return
        if was_closed:
            self._sync_ring_closure(geom, part, ring)
        self._commit(geom, "移动节点")

    @staticmethod
    def _sync_ring_closure(geom: QgsGeometry, part: int, ring: int) -> None:
        """闭合环的首末节点必须一致：动了首点就把末点跟过去。

        不补这一步，图上会多出一条「从末点连回首点」的边——正是用户看到的
        「多余、不正确的边线」，而且图形会跟节点列表的坐标对不上。
        """
        absg = geom.get()
        count = absg.vertexCount(part, ring)
        if count < 2:
            return
        first = absg.vertexAt(QgsVertexId(part, ring, 0))
        absg.moveVertex(QgsVertexId(part, ring, count - 1), first)

    def _delete(self) -> None:
        """删除全部勾选节点；**每个环**至少保留 :data:`MIN_RING_NODES` 个。"""
        if not self._guard_loaded():
            return
        rows = self._checked_rows()
        if not rows:
            self._warn("请先勾选要删除的节点")
            return
        # 先按行拿键：空白行没有键（它还不是节点），真实行有
        keys = [k for k in (self._key_at(r) for r in rows) if k is not None]
        blank_rows = [r for r in rows if self._key_at(r) is None]
        if not keys:
            # 只勾了空白行 → 就是把这张没填完的空白行撤掉，不碰几何、也不开编辑命令
            self.table.blockSignals(True)
            for r in sorted(blank_rows, reverse=True):
                self.table.removeRow(r)
            self._renumber_from(min(blank_rows))
            self.table.blockSignals(False)
            self._pending = None
            self._sync_markers()
            self._toast("已取消未填写的空白行", "success")
            return
        per_ring: dict[tuple[int, int], list[int]] = {}
        for part, ring, vertex in keys:
            per_ring.setdefault((part, ring), []).append(vertex)
        for (part, ring), vertices in per_ring.items():
            info = self._ring_info(part, ring)
            if info is None:
                continue
            if info.count - len(set(vertices)) < MIN_RING_NODES:
                # 任一受影响环会低于下限 → 整次拒绝，不做部分删除
                self._warn(f"面要素至少保留 {MIN_RING_NODES} 个节点，删除被拒绝")
                return

        geom = self._copy_geom()
        if geom is None:
            return
        absg = geom.get()
        # 删之前先记住哪些环是闭合的：删掉首点会破闭合，之后要补回来
        closed_before = {
            key: _ring_closed(geom.constGet(), key[0], key[1]) for key in per_ring
        }
        for (part, ring), vertices in per_ring.items():
            for vertex in sorted(set(vertices), reverse=True):
                absg.deleteVertex(QgsVertexId(part, ring, vertex))
        for (part, ring), was_closed in closed_before.items():
            if was_closed:
                self._sync_ring_closure(geom, part, ring)
        if not self._commit(geom, "删除节点"):
            return
        self._rebuild(clear_rows=True)
        self._toast(f"已删除 {len(keys)} 个节点", "success")

    def _blink(self) -> None:
        """**只闪勾选的节点**；:data:`_BLINK_TOTAL_MS` 后自动收。

        不闪整个要素：要素本来就有红色描边带与顶点手柄标着，再整体闪一下只是噪声，
        用户要看的是「这几个节点具体在哪」。
        """
        if not self._guard_loaded():
            return
        checked = self._checked_keys()
        if not checked:
            self._warn("请先勾选要闪烁的节点")
            return
        self._start_vertex_blink(sorted(checked))
        self._toast("已闪烁勾选的节点", "success")

    def _pan(self) -> None:
        """**只挪地图视图**：把目标节点平移到视图中心（不改几何，照原型）。"""
        if not self._guard_loaded():
            return
        key = self._current_key()
        if key is None:
            checked = sorted(self._checked_keys())
            if not checked:
                self._warn("请先勾选或点击选择要平移的节点")
                return
            key = checked[len(checked) // 2]  # 照原型取勾选行的中间那个
        geom = self._geom()
        if geom is None:
            return
        part, ring, vertex = key
        point = geom.constGet().vertexAt(QgsVertexId(part, ring, vertex))
        center = self._canvas_xy(QgsPointXY(point.x(), point.y()))
        if center is None:
            return
        try:
            current = self._canvas.extent()
            if current is None or current.isNull() or current.isEmpty():
                current = QgsRectangle(center.x() - 1, center.y() - 1, center.x() + 1, center.y() + 1)
            half_w, half_h = current.width() / 2.0, current.height() / 2.0
            # 顺序不能倒：没有 extent 时 setCenter 是空操作、center 还会变 nan
            self._canvas.setExtent(
                QgsRectangle(
                    center.x() - half_w, center.y() - half_h,
                    center.x() + half_w, center.y() + half_h,
                )
            )
            self._canvas.setCenter(center)
        except RuntimeError:
            return
        self._refresh_canvas()
        self._toast(f"节点 {vertex} 已移动至地图视图中心点", "success")

    def _zoom(self) -> None:
        """把**整个要素**缩放到画布（留余量）；忽略行选择，照原型。"""
        if not self._guard_loaded():
            return
        geom = self._geom()
        if geom is None:
            return
        extent = geom.boundingBox()
        try:
            dest_crs = self._canvas.mapSettings().destinationCrs()
            source_crs = self._layer.crs()
            if source_crs.isValid() and source_crs != dest_crs:
                transform = QgsCoordinateTransform(source_crs, dest_crs, self._project)
                extent = transform.transformBoundingBox(extent)
        except QgsCsException:
            # 脏几何（坐标与声明的 CRS 不符）反变换越界：退回原始范围直接缩，不崩
            logger.warning("节点列表：缩放越界，改用原始范围 layer=%s", self._layer.name())
            extent = geom.boundingBox()
        except RuntimeError:
            return
        if extent.isNull():
            return
        pad = max(extent.width(), extent.height()) * _ZOOM_PADDING_RATIO
        if pad <= 0.0:
            current = self._canvas.extent()
            pad = max(current.width(), current.height()) * 0.1
        extent = QgsRectangle(extent)
        extent.grow(pad)
        try:
            self._canvas.setExtent(extent)
        except RuntimeError:
            return
        self._refresh_canvas()
        self._toast(f"图斑 {self._feature_label()} 已缩放定位到地图画布中心", "success")

    def _feature_label(self) -> str:
        if self._layer is None or self._fid is None:
            return ""
        try:
            return feature_label(self._layer.getFeature(self._fid), self._layer)
        except RuntimeError:
            return ""

    # ------------------------------------------------------------ 提交与收尾

    def _copy_geom(self) -> Optional[QgsGeometry]:
        """几何**副本**：改动在副本上做，再整体 ``changeGeometry`` 写回。

        与 ``FeatureEditTool._commit`` 同一模型——QGIS 在命令创建时自行快照；
        绕开 ``constGet()`` 就地改动对撤销语义的破坏。
        """
        geom = self._geom()
        return None if geom is None else QgsGeometry(geom)

    def _commit(self, geom: QgsGeometry, command_text: str) -> bool:
        """一个编辑命令里写回几何；成功才算数（失败则丢弃本次改动）。"""
        # 提交期间挂标志：changeGeometry 会同步发 geometryChanged，不挡住的话
        # 外部刷新会插进编辑命令窗口里
        self._applying = True
        try:
            with edit_command(self._layer, command_text) as cmd:
                if not self._layer.changeGeometry(self._fid, geom):
                    cmd.abort()
        except Exception:  # noqa: BLE001 —— 命令已由 edit_command 收尾，这里只提示
            logger.exception("节点列表：写回几何失败")
            self._warn("修改失败：图层编辑 API 异常")
            return False
        finally:
            self._applying = False
        # 收尾都在编辑命令**之外**（放进 with 里一旦抛异常会连成功改动一起被丢弃）
        clear_spatial_index(self._layer)
        # 写完**读回来核对**：编辑缓冲里要是没真的变（图层只读 / 驱动拒绝 / 命令被丢弃），
        # 列表就会与画布各说各话 —— 用户看到的正是「只有高亮在变、原始图形不动」。
        # 与其装作写成功，不如明说，并让列表回到图层的真实现状。
        actual = self._geom()
        if actual is None or actual.asWkb() != geom.asWkb():
            logger.warning("节点列表：几何没能写进图层，按图层现状刷新")
            self._warn("几何没有写进图层（图层可能不可编辑），已按图层现状刷新列表")
            self._rebuild(clear_rows=True)
            return False
        # 明着让图层重绘一次：改的是编辑缓冲里的几何，画布上那份渲染缓存不会自己失效，
        # 不刷的话用户看到的还是改之前的原始图形
        try:
            self._layer.triggerRepaint()
        except RuntimeError:
            pass
        self._rebuild()
        return True

    def _refresh_canvas(self) -> None:
        if self._canvas is None:
            return
        # 仓库约束：事件栈内不直接 refresh
        QTimer.singleShot(0, self._safe_refresh)

    def _safe_refresh(self) -> None:
        try:
            self._canvas.refresh()
        except RuntimeError:
            pass

    # ------------------------------------------------------------ 地图上的标记

    def _canvas_xy(self, point_xy) -> Optional[QgsPointXY]:
        """图层 CRS 的点 → 画布目标 CRS 的点（同 CRS 时原样返回）。"""
        try:
            source = self._layer.crs()
            dest = self._canvas.mapSettings().destinationCrs()
        except RuntimeError:
            return None
        if not source.isValid() or source == dest:
            return QgsPointXY(point_xy)
        try:
            return QgsCoordinateTransform(source, dest, self._project).transform(
                QgsPointXY(point_xy)
            )
        except (QgsCsException, RuntimeError):
            return None

    def _sync_markers(self) -> None:
        """描边带圈出被检查的要素 + 每个节点一个手柄（勾选的画红）。"""
        if self._torn_down or self._canvas is None:
            return
        geom = self._geom()
        if geom is None:
            self._drop_band()
            self._drop_markers(self._markers)
            return
        if self._band is None:
            try:
                self._band = QgsRubberBand(self._canvas, QgsWkbTypes.PolygonGeometry)
            except RuntimeError:
                return
            self._band.setColor(_OUTLINE_COLOR)
            self._band.setWidth(2)
            self._band.setFillColor(QColor(229, 48, 48, 40))
        try:
            # 先 reset 再设：只调 setToGeometry 时旧几何会留在带子上，图上就出现
            # 「多余的、跟节点列表对不上的边线」
            self._band.reset(QgsWkbTypes.PolygonGeometry)
            # 传 layer：QGIS 自己把几何从图层 CRS 换算到画布 CRS
            self._band.setToGeometry(geom, self._layer)
            self._band.show()
        except RuntimeError:
            return

        self._drop_markers(self._markers)
        checked = self._checked_keys()
        info = self._current_ring()
        if info is None:
            return
        absg = geom.constGet()
        # 只画**当前环**的节点手柄：表里也只看得到这一环，图上对得上
        for vertex in range(info.count):
            point = absg.vertexAt(QgsVertexId(info.part, info.ring, vertex))
            center = self._canvas_xy(QgsPointXY(point.x(), point.y()))
            if center is None:
                continue
            is_checked = (info.part, info.ring, vertex) in checked
            marker = self._make_marker(center, checked=is_checked)
            if marker is None:
                continue
            self._markers.append(marker)

    def _make_marker(self, center: QgsPointXY, checked: bool = False
                     ) -> Optional[QgsVertexMarker]:
        """节点手柄：**实心圆点**，勾选的画成红色实心。"""
        try:
            marker = QgsVertexMarker(self._canvas)
        except RuntimeError:
            return None
        marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        marker.setIconSize(11)
        marker.setPenWidth(2)
        color = QColor(*_BLINK_COLOR) if checked else QColor(255, 255, 255)
        marker.setColor(color)
        marker.setFillColor(color)     # 实心
        marker.setCenter(center)
        marker.show()
        return marker

    def _drop_markers(self, markers: list) -> None:
        """销毁顶点手柄。

        ``QgsVertexMarker`` 继承链是 ``QgsMapCanvasItem -> QGraphicsItem``（**非
        QObject**），没有 ``deleteLater()``：先移出画布场景再 ``sip.delete``。
        与 ``FeatureEditTool._clear_markers`` 同一套——不走这一步，关掉弹窗后
        画布上会留一排红方块。
        """
        for marker in markers:
            try:
                marker.hide()
            except RuntimeError:
                pass
            try:
                scene = marker.scene()
                if scene is not None:
                    scene.removeItem(marker)
            except RuntimeError:
                pass
            try:
                sip.delete(marker)
            except (RuntimeError, TypeError):
                pass
        markers.clear()

    def _drop_band(self) -> None:
        band, self._band = self._band, None
        if band is None:
            return
        try:
            band.reset(QgsWkbTypes.PolygonGeometry)
        except RuntimeError:
            pass

    # ------------------------------------------------------------ 顶点闪烁

    def _start_vertex_blink(self, keys: list) -> None:
        self._stop_vertex_blink()
        geom = self._geom()
        if geom is None:
            return
        absg = geom.constGet()
        for part, ring, vertex in keys:
            point = absg.vertexAt(QgsVertexId(part, ring, vertex))
            center = self._canvas_xy(QgsPointXY(point.x(), point.y()))
            if center is None:
                continue
            marker = self._make_marker(center, checked=True)
            if marker is None:
                continue
            marker.setIconSize(14)
            self._blink_markers.append(marker)
        if not self._blink_markers:
            return
        self._blink_left = max(1, _BLINK_TOTAL_MS // _BLINK_INTERVAL_MS)
        self._blink_timer.start()
        QTimer.singleShot(_BLINK_TOTAL_MS, self._stop_vertex_blink)

    def _on_blink_tick(self) -> None:
        self._blink_left -= 1
        visible = self._blink_left % 2 == 0
        for marker in self._blink_markers:
            try:
                marker.setVisible(visible)
            except RuntimeError:
                pass
        if self._blink_left <= 0:
            self._stop_vertex_blink()

    def _stop_vertex_blink(self) -> None:
        if self._blink_timer.isActive():
            self._blink_timer.stop()
        self._blink_left = 0
        self._drop_markers(self._blink_markers)

    # ------------------------------------------------------------ 生命周期

    def showEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        super().showEvent(event)
        self._torn_down = False
        self._rebuild()

    def hideEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        super().hideEvent(event)
        self._teardown()

    def done(self, result: int) -> None:  # noqa: N802 —— Qt 命名
        # × / Esc / reject 的兜底（hideEvent 一般已收过，_teardown 幂等）
        self._teardown()
        super().done(result)

    def _teardown(self) -> None:
        """幂等收尾：停闪烁、收标记与描边带、断图层信号。"""
        if self._torn_down:
            return
        self._torn_down = True
        timer = getattr(self, "_blink_timer", None)
        if timer is not None:
            timer.stop()
        self._blink_left = 0
        self._drop_markers(self._blink_markers)
        self._drop_markers(self._markers)
        self._drop_band()
        self._pending = None
        if self._layer is not None:
            self._disconnect_layer(self._layer)


__all__ = ["NodeListDialog"]
