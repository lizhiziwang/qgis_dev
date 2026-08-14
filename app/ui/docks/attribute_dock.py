"""动态加载属性表 Dock。

迁自 :class:`component.map_tool.PagedAttributeDockWidget` 等 5 个类。"""

from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, QVariant, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsVectorLayer,
)

from app.core.domain.editing import LayerEditService
from app.ui.common.icons import Icons

#: 超过该数量才启用分批动态加载；≤ 阈值时一次性全量加载（避免小图层分页闪烁）
DYNAMIC_LOAD_THRESHOLD = 1000
#: 大图层每批固定加载条数（首屏即 2000，滚动/「加载更多」后续批同量）。
#: 用固定值而不是总量的百分比：500w 级图层按 20% 算首屏就是 100w 条，一样卡死。
STREAM_BATCH = 2000


class PagedAttributeTableModel(QAbstractTableModel):
    """动态加载属性表 model（流式迭代器实现）。

    总量 **大于** ``DYNAMIC_LOAD_THRESHOLD``（默认 1000）时分批动态加载：
    持有 ``QgsFeatureIterator``，每次从迭代器断点续取 ``STREAM_BATCH`` 条
    （首屏即 2000）。总量 ≤ 阈值时一次性全量加载（``batch_size`` = 总量）。

    关键点：**不再为分页预收集全部要素 ID**。旧实现把全部匹配 ID 一次性塞进
    ``all_ids``（5w 级图层同步遍历几百毫秒，500w 级直接卡死 UI），这里改为
    持有一个流式迭代器，``load_more()`` 按需续取，内存只保留已加载的行。
    """

    def __init__(self, layer: QgsVectorLayer, page_size: int = 50, filter_str: str = "1=1", show_ids: Optional[list] = None) -> None:
        super().__init__()
        self.layer = layer
        # page_size 仅保留兼容旧调用（main_window 传 50），动态加载不再按页
        self.page_size = max(1, int(page_size))
        self.filter_str = filter_str or "1=1"
        self.show_ids = list(show_ids) if show_ids else []
        self.field_names = [f.name() for f in layer.fields()]
        self.features: List[QgsFeature] = []
        #: 每批加载条数：≤ 阈值时 = 总量（一次全量）；否则固定 STREAM_BATCH
        self.batch_size = 0
        #: 已加载条数（= features 长度；为 0 表示尚未加载）
        self.loaded_count = 0
        #: 加载中标志，防止滚动信号重入导致重复加载
        self.is_loading = False
        #: 流式迭代器；None 表示未打开/已耗尽
        self._iterator = None
        #: 恰好拉满一整批时探测出来的下一条（属于下一批），避免边界漏判耗尽
        self._lookahead: Optional[QgsFeature] = None
        self._exhausted = False
        #: 总数是否已确知。无过滤（1=1）时 featureCount() 直接给出；
        #: 带过滤/show_ids 时无法免遍历获知，流式结束（迭代器耗尽）后补齐
        self.total_known = False
        self.total_features = 0
        self.where = self._build_where()
        self._refresh_totals()
        self.load_more()

    def _build_where(self) -> str:
        """组合筛选表达式：show_ids 子集时叠加 ``$id in (...)`。"""
        if self.show_ids:
            return f"{self.filter_str} and $id in ({','.join(str(i) for i in self.show_ids)})"
        return self.filter_str

    @property
    def has_more(self) -> bool:
        """是否还有未加载的要素（迭代器未耗尽即视为有）。"""
        return not self._exhausted

    def _fast_total(self):
        """总数快路径：无过滤表达式时 ``featureCount()``（O(1)）；否则返回未知。

        :return: ``(total, known)``。带过滤/show_ids 时总量必须流式结束后才知道。
        """
        if self.where.strip() in ("", "1=1"):
            try:
                return int(self.layer.featureCount()), True
            except Exception:  # noqa: BLE001
                pass
        return 0, False

    def _refresh_totals(self) -> None:
        """重置流式状态（数据/筛选变化后调用）：快路径总数 + 重新打开迭代器。"""
        self.total_features, self.total_known = self._fast_total()
        self.loaded_count = 0
        self.features = []
        # 小图层一次全量，避免分页闪烁；大图层/未知总数用固定批次
        self.batch_size = (
            self.total_features
            if self.total_known and self.total_features <= DYNAMIC_LOAD_THRESHOLD
            else STREAM_BATCH
        )
        self._start_iteration()

    def _start_iteration(self) -> None:
        """打开匹配 ``where`` 的流式迭代器。已知空图层直接标记耗尽。"""
        self._exhausted = False
        self._iterator = None
        self._lookahead = None
        if self.total_known and self.total_features == 0:
            self._exhausted = True
            return
        try:
            request = QgsFeatureRequest().setFilterExpression(self.where)
            self._iterator = self.layer.getFeatures(request)
        except Exception:  # noqa: BLE001
            self._exhausted = True

    def load_more(self) -> None:
        """从流式迭代器续取下一批（每批 ``batch_size`` 条）；已加载完或加载中则不做。

        总量恰好是 ``batch_size`` 整数倍时，迭代器恰好在批次边界耗尽——若只在拉满
        ``batch_size`` 后就停止，永远探测不到末尾。因此批次拉满后再 ``next()`` 一次
        判断是否还有下一条（探测出的要素暂存到 ``_lookahead``，属于下一批）。
        """
        if self._exhausted or self.is_loading:
            return
        self.is_loading = True
        try:
            chunk = []
            if self._lookahead is not None:
                chunk.append(self._lookahead)
                self._lookahead = None
            while self._iterator is not None and len(chunk) < self.batch_size:
                try:
                    chunk.append(next(self._iterator))
                except StopIteration:
                    self._iterator = None
                    self._exhausted = True
                    break
            # 恰好拉满一整批：探测下一条判断是否已到末尾（StopIteration → 耗尽）
            if self._iterator is not None and not self._exhausted:
                try:
                    self._lookahead = next(self._iterator)
                except StopIteration:
                    self._iterator = None
                    self._exhausted = True
            if chunk:
                self.beginInsertRows(QModelIndex(), self.loaded_count, self.loaded_count + len(chunk) - 1)
                self.features.extend(chunk)
                self.endInsertRows()
                self.loaded_count += len(chunk)
            # 流式结束：补齐真实总数（过滤/show_ids 路径此前 unknown，或 provider
            # 返回数与 featureCount() 不一致时校正）
            if self._exhausted and (not self.total_known or self.loaded_count != self.total_features):
                self.total_features = self.loaded_count
                self.total_known = True
        except Exception:  # noqa: BLE001
            # 迭代中异常（如图层被外部改动使游标失效）：放弃本批并标记耗尽，避免死循环
            self._iterator = None
            self._exhausted = True
        finally:
            self.is_loading = False

    def set_filter(self, filter_str: str) -> None:
        """更换筛选表达式并刷新（重开迭代器，回到首屏首批）。"""
        self.filter_str = filter_str or "1=1"
        self.where = self._build_where()
        self.beginResetModel()
        self._refresh_totals()
        self.endResetModel()
        self.load_more()

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self.features)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.field_names) + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.features):
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            feature = self.features[index.row()]
            if index.column() == 0:
                return feature.id()
            field_idx = index.column() - 1
            if field_idx < len(feature.fields()):
                return feature.attribute(field_idx)
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            if section == 0:
                return "ID"
            if section - 1 < len(self.field_names):
                return self.field_names[section - 1]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.ItemIsEnabled
        if index.column() == 0:
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if self.layer.isEditable():
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        if index.column() == 0:
            return False
        if index.row() >= len(self.features):
            return False
        feature = self.features[index.row()]
        col = index.column() - 1
        if col >= len(feature.fields()):
            return False
        field: QgsField = self.layer.fields().at(col)
        if field.type() == QVariant.Int:
            try:
                value = int(value)
            except (ValueError, TypeError):
                return False
        elif field.type() == QVariant.Double:
            try:
                value = float(value)
            except (ValueError, TypeError):
                return False
        success = self.layer.changeAttributeValue(feature.id(), col, value)
        if success:
            self.features[index.row()].setAttribute(col, value)
            self.dataChanged.emit(index, index, [role])
            return True
        return False


class PagedAttributeTableWidget(QWidget):
    """可停靠的属性表 widget。"""

    zoom_to_map = pyqtSignal(str, object)

    def __init__(self, layer, page_size: int = 50, parent=None, filter_str: str = "1=1", show_ids: Optional[list] = None) -> None:
        super().__init__(parent)
        self.parent = parent
        self.layer = layer
        self.page_size = page_size
        self.filter_str = filter_str
        self.show_ids = show_ids
        self.model: Optional[PagedAttributeTableModel] = None
        self.editing_service = LayerEditService()
        self._setup_ui()

    def _setup_ui(self) -> None:
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(2)

        # 工具条
        tb = QHBoxLayout()
        self.start_btn = QPushButton()
        self.start_btn.setIcon(Icons.edit())
        self.start_btn.setToolTip("开始编辑")
        self.start_btn.setIconSize(self.start_btn.iconSize())
        self.start_btn.setFixedSize(26, 26)
        self.save_btn = QPushButton()
        self.save_btn.setEnabled(False)
        self.save_btn.setIcon(Icons.save_edit())
        self.save_btn.setToolTip("保存编辑")
        self.save_btn.setFixedSize(26, 26)
        self.stop_btn = QPushButton()
        self.stop_btn.setEnabled(False)
        self.stop_btn.setIcon(Icons.stop_edit())
        self.stop_btn.setToolTip("停止编辑")
        self.stop_btn.setFixedSize(26, 26)
        self.filter_btn = QPushButton()
        self.filter_btn.setIcon(Icons.filter())
        self.filter_btn.setToolTip("属性筛选")
        self.filter_btn.setFixedSize(26, 26)
        self.start_btn.clicked.connect(self._start_edit)
        self.save_btn.clicked.connect(self._save_edit)
        self.stop_btn.clicked.connect(self._stop_edit)
        self.filter_btn.clicked.connect(self._filter)
        for b in (self.start_btn, self.save_btn, self.stop_btn, self.filter_btn):
            tb.addWidget(b)
        tb.addStretch(0)
        main_layout.addLayout(tb)

        # 表格
        self.table_view = QTableView()
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.table_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        header = self.table_view.horizontalHeader()
        header.setMinimumSectionSize(20)
        header.setMaximumSectionSize(400)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        self.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self._show_context_menu)
        main_layout.addWidget(self.table_view)

        # 底部动态加载条（加载状态 + 加载更多按钮）
        self._setup_load_bar(main_layout)

        self.setLayout(main_layout)

        self.model = PagedAttributeTableModel(self.layer, self.page_size, filter_str=self.filter_str, show_ids=self.show_ids)
        self.table_view.setModel(self.model)
        # 筛选重置后刷新加载状态；滚动到底自动加载下一批
        self.model.modelReset.connect(self._refresh_load_state)
        self.table_view.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self._refresh_load_state()

    def _setup_load_bar(self, main_layout) -> None:
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(4)

        self.load_status_label = QLabel("加载中…")
        bar.addWidget(self.load_status_label)

        self.load_more_btn = QPushButton("加载更多")
        self.load_more_btn.clicked.connect(self._do_load_batch)
        bar.addWidget(self.load_more_btn)

        bar.addStretch()
        main_layout.addLayout(bar)

    def _refresh_load_state(self) -> None:
        """按模型加载进度刷新状态文本与"加载更多"按钮可用性。"""
        if self.model is None:
            return
        loaded = len(self.model.features)
        total = self.model.total_features
        if not self.model.total_known:
            # 带过滤/show_ids：总数流式结束前未知，先显示已加载条数
            if self.model.has_more:
                self.load_status_label.setText(f"已加载 {loaded} 条，滚动到底或点击加载更多")
            else:
                self.load_status_label.setText(f"共 {loaded} 条（已全部加载）")
        elif total == 0:
            self.load_status_label.setText("0 条")
        elif self.model.has_more:
            self.load_status_label.setText(f"已加载 {loaded} / {total} 条，滚动到底或点击加载更多")
        else:
            self.load_status_label.setText(f"共 {total} 条（已全部加载）")
        self.load_more_btn.setEnabled(self.model.has_more)

    def _on_scroll(self, value: int) -> None:
        """滚动到接近底部时追加加载下一批。"""
        if self.model is None or not self.model.has_more:
            return
        bar = self.table_view.verticalScrollBar()
        if value >= bar.maximum() - 10:
            self._load_batch()

    def _load_batch(self) -> None:
        # 延迟到下一事件循环再加载：endInsertRows 会同步触发滚动 valueChanged，
        # 直接调 load_more 会重入（见 buffer 对话框的 Qt 信号重入踩坑）
        QTimer.singleShot(0, self._do_load_batch)

    def _do_load_batch(self) -> None:
        if self.model is None:
            return
        self.model.load_more()
        self._refresh_load_state()

    def _start_edit(self) -> None:
        ok = self.editing_service.start(self.layer)
        if ok:
            self.save_btn.setEnabled(True)
            self.stop_btn.setEnabled(True)

    def _save_edit(self) -> None:
        if self.layer.isEditable():
            self.editing_service.commit(self.layer)

    def _stop_edit(self) -> None:
        self.editing_service.stop(self.layer, save=True)
        self.save_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)

    def _filter(self) -> None:
        from app.ui.dialogs.query_builder import QueryBuilderDialog
        dlg = QueryBuilderDialog(self.layer, parent=self)
        dlg.sqlConfirmed.connect(self._apply_filter)
        dlg.exec()

    def _apply_filter(self, cond: str) -> None:
        """筛选确认后更新模型筛选并刷新（重新统计，回到首屏首批）。"""
        if self.model:
            self.model.set_filter(cond)
            self.table_view.scrollToTop()
            self._refresh_load_state()

    def _show_context_menu(self, position) -> None:
        from PyQt6.QtGui import QAction
        from PyQt6.QtWidgets import QMenu
        if not self.model:
            return
        idx = self.table_view.indexAt(position)
        if not idx.isValid():
            return
        feature = self.model.features[idx.row()]
        menu = QMenu(self)
        zoom = QAction("缩放到要素", self)
        zoom.triggered.connect(lambda: self.zoom_to_map.emit("zoomTo", {"extent": feature.geometry().boundingBox(), "id": [feature.id()]}))
        menu.addAction(zoom)
        sel = QAction("选中要素", self)
        sel.triggered.connect(lambda: self.zoom_to_map.emit("selectFeature", {"id": [feature.id()]}))
        menu.addAction(sel)
        if self.layer.isEditable():
            delete = QAction("删除", self)
            delete.triggered.connect(lambda: self.layer.deleteFeature(fid=feature.id()))
            menu.addAction(delete)
        menu.exec(self.table_view.viewport().mapToGlobal(position))


class AttributeDock(QDockWidget):
    """动态加载属性表 dock。"""

    def __init__(self, layer, page_size: int = 50, parent=None, filter_str: str = "1=1", show_ids: Optional[list] = None) -> None:
        title = f"属性表 - {layer.name()}"
        super().__init__(title, parent)
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.BottomDockWidgetArea)
        self.topParent = parent
        self.layer = layer
        self.show_ids = list(show_ids) if show_ids else []
        self.content_widget = PagedAttributeTableWidget(layer, page_size, self, filter_str=filter_str, show_ids=show_ids)
        self.setWidget(self.content_widget)
        self.content_widget.zoom_to_map.connect(self.zoom_to_map)
        self.setMinimumSize(200, 100)

    def add_new_layer(self, layer, parent, filter_str: str, show_ids, page_size: int = 50, title: str = "属性表") -> None:
        self.layer = layer
        self.show_ids = list(show_ids) if show_ids else []
        self.content_widget = PagedAttributeTableWidget(layer, page_size, self, filter_str=filter_str, show_ids=show_ids)
        self.setWidget(self.content_widget)
        self.setWindowTitle(title)

    def closeEvent(self, event) -> None:
        try:
            if self.layer.subsetString():
                self.layer.setSubsetString("")
            if self.layer.isEditable():
                self.content_widget._stop_edit()
            if self.topParent is not None:
                self.topParent.current_attribute_dock = None
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)

    def zoom_to_map(self, action_type: str, data: dict) -> None:
        if self.topParent is None:
            return
        if action_type == "zoomTo":
            self.layer.selectByIds(data["id"])
            if hasattr(self.topParent, "zoom_to_layer_extent_transform"):
                self.topParent.zoom_to_layer_extent_transform(extent=data["extent"], source_crs=self.layer.crs())
        elif action_type == "selectFeature":
            self.layer.selectByIds(data["id"])
            self._refresh_canvas()
        elif action_type == "zoom_to_features":
            self.topParent.canvas.setExtent(data["extent"])
            self.layer.selectByIds(data["ids"])
            self.topParent.canvas.refresh()

    def _refresh_canvas(self) -> None:
        """选中要素后刷新画布，让高亮立即显示（selectByIds 不会自动刷新）。"""
        canvas = getattr(self.topParent, "canvas", None)
        if canvas is not None:
            canvas.refresh()
