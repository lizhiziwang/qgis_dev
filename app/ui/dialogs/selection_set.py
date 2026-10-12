"""「选择集」面板：框选结束后用**属性表**列出选中的要素，并跟随选中集实时刷新。

框选一次常跨几层、几十个图斑——画布上只有一片黄色高亮，想核对「到底选进了哪几个」
就只能去属性表里翻。这里把选中集摊成一张属性表：**第一行是字段表头，下面是属性值**，
一页只渲染一页的行（选得多时不会一次建出成百上千行卡住界面）。点某一行就把该图斑在
画布上闪烁几下定位。

跨图层：不同图层的字段不同，没法共用一行表头，所以上方有个**图层下拉框**一次看一层；
只有一层有选中时下拉框藏起来（那时它只有一个选项，白占一行）。

**只做展示与定位**：本面板唯一的写操作是「清除选择」，且**只清自己列出的那些图层**
（不走 :meth:`MainWindow._on_clear_selection`——那一个会清工程里所有图层，还会顺手
抹掉测量的临时图形）。点行**只闪烁、不缩放**（缩放会把视野挪走）；数据挤成一片、
光闪看不见时，按「定位」显式放大到当前行——见 :meth:`SelectionSetDialog._on_locate`。

**实时刷新**：面板开着时跟随选中集变化（可继续用选择工具增减）。``QgsProject`` 没有
全局选择信号，只能逐层接 ``selectionChanged`` + 工程图层增删信号，写法与
:class:`~app.ui.common.selection_overlay.SelectionOverlay` 一致。一次框选跨 N 层会在
工具的循环里连发 N 次信号，所以重建走 80ms 防抖。

**不画常驻选择带**：那是 ``SelectionOverlay`` 的事；本面板点行只用
:class:`~app.ui.common.feature_highlight.FeatureHighlighter` 闪几下（洋红，与选中集的
黄色、框选的青色都区分得开）。

主题由全局 QSS 下发（见 ``app/ui/common/theme.py``），本类不写 ``setStyleSheet``，
只用 objectName（``#SectionLabel`` / ``#PropHint`` / ``#DialogPrimary``）。
"""

from __future__ import annotations

from typing import Optional

from qgis.core import (
    QgsCoordinateTransform,
    QgsCsException,
    QgsFeature,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
)

from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.feature_highlight import FeatureHighlighter
from app.ui.common.value_format import format_value
from app.ui.tools.feature_attrs import field_alias

logger = get_logger(__name__)

#: 每页条数可选值（默认取第 2 个）
_PAGE_SIZE_CHOICES = (20, 50, 100, 200)
_DEFAULT_PAGE_SIZE = 50

#: 选中集变化后的重建防抖（毫秒）。一次框选跨 N 层会连发 N 次 selectionChanged。
_RELOAD_DEBOUNCE_MS = 80

#: 「定位」时的视野余量：取要素自身尺寸的这个比例（留一圈周围，看得出位置关系）
_ZOOM_PADDING_RATIO = 0.3

_HINT = "点击行高亮闪烁图斑，挤在一起看不清时按「定位」放大；选择集与“选择屏蔽”联动，可继续用选择工具增减。"
_EMPTY_TEXT = "当前没有选中要素（用框选工具在地图上选择）"


# ============================================================ 模型


class _MessageModel(QAbstractTableModel):
    """一句话占位模型：零选中时让空表看起来是**有意为之**，而不是坏了。"""

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self._text = text

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 —— Qt 命名
        return 1

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802 —— Qt 命名
        return 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role == Qt.ItemDataRole.DisplayRole:
            return self._text
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        return None


class _SelectionPageModel(QAbstractTableModel):
    """选中集的**分页**属性表模型：一页只渲染 ``page_size`` 行，列头是字段名。

    与属性表（:class:`~app.ui.docks.attribute_dock.PagedAttributeTableModel`）的区别：
    那个是**流式增量加载**（按需从 provider 续取，为的是扛几十万条的完整浏览）；这里
    的数据源本来就是一个有限的 fid 列表，做**真分页**更直白——页码 / 上一页下一页 /
    每页条数，一次只把当前页的要素取回来。

    列布局与属性表一致：第 0 列是 ``ID``（要素 fid），其后每列一个字段（表头用别名）。
    因此本模型不带任何"挑哪些字段显示"的判断——**跟属性表一样全列出来**。
    """

    def __init__(
        self,
        layer: QgsVectorLayer,
        fids: list[int],
        page_size: int = _DEFAULT_PAGE_SIZE,
        page: int = 0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._layer = layer
        self._fids = [int(fid) for fid in fids]
        self._page_size = max(1, int(page_size))
        self._page = 0
        #: 当前页的要素（fid → QgsFeature）。一页最多几百个，一次取回即可——
        #: 否则 ``data()`` 会被每个单元格各调一次 ``getFeature``，一屏就是几百次。
        self._page_features: dict[int, QgsFeature] = {}
        self.column_names = ["ID"] + [
            field_alias(layer.fields().at(i)) or field.name()
            for i, field in enumerate(layer.fields())
        ]
        self._page = max(0, min(int(page), self.page_count - 1))
        self._load_page()

    # ------------------------------------------------------------ 分页

    @property
    def page(self) -> int:
        return self._page

    @property
    def page_size(self) -> int:
        return self._page_size

    @property
    def total(self) -> int:
        return len(self._fids)

    @property
    def page_count(self) -> int:
        return max(1, (len(self._fids) + self._page_size - 1) // self._page_size)

    def page_fids(self) -> list[int]:
        start = self._page * self._page_size
        return self._fids[start : start + self._page_size]

    def set_page(self, page: int) -> None:
        page = max(0, min(int(page), self.page_count - 1))
        if page == self._page:
            return
        self.beginResetModel()
        self._page = page
        self._load_page()
        self.endResetModel()

    def set_page_size(self, size: int) -> None:
        """换每页条数，尽量让当前看的第一行留在视野里。"""
        size = max(1, int(size))
        if size == self._page_size:
            return
        first_row = self._page * self._page_size
        self.beginResetModel()
        self._page_size = size
        self._page = min(first_row // size, self.page_count - 1)
        self._load_page()
        self.endResetModel()

    def fid_at(self, row: int) -> Optional[int]:
        fids = self.page_fids()
        return fids[row] if 0 <= row < len(fids) else None

    def _load_page(self) -> None:
        self._page_features = {}
        for fid in self.page_fids():
            try:
                feature = self._layer.getFeature(fid)
            except RuntimeError:
                continue  # 图层销毁流程中
            if feature is not None and feature.isValid():
                self._page_features[fid] = feature

    # ------------------------------------------------------------ QAbstractTableModel

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 —— Qt 命名
        return len(self.page_fids())

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802 —— Qt 命名
        return len(self.column_names)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if 0 <= section < len(self.column_names):
            return self.column_names[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return None
        fids = self.page_fids()
        if index.row() >= len(fids):
            return None
        fid = fids[index.row()]
        if index.column() == 0:
            return fid
        feature = self._page_features.get(fid)
        if feature is None:
            return None
        attributes = feature.attributes()
        field_idx = index.column() - 1
        if field_idx >= len(attributes):
            return None
        value = attributes[field_idx]
        # 与属性表同口径：NULL 显示成空单元格（不是字面量 "NULL"）
        return "" if value is None else format_value(value)

    def flags(self, index):
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable


# ============================================================ 面板


class SelectionSetDialog(BaseDialog):
    """用属性表列出当前选中集，点行闪烁定位；跟随选中集实时刷新。"""

    def __init__(self, canvas, parent=None) -> None:
        super().__init__(parent, title="选择集")
        # BaseDialog 默认最小尺寸 800×600，本面板小得多，这里整体压掉
        self.setMinimumSize(420, 320)
        self.resize(680, 520)
        self.setModal(False)

        self._canvas = canvas
        self._project = None
        if canvas is not None:
            try:
                self._project = canvas.project()
            except RuntimeError:
                self._project = None
        self._project = self._project or QgsProject.instance()

        self._hooked: set[str] = set()
        self._attached = False
        #: 收尾状态：默认 True —— 构造期若收到 hideEvent（无边框装配可能触发）直接跳过
        self._torn_down = True
        self._highlighter: Optional[FeatureHighlighter] = None
        self._highlighter_layer_id: Optional[str] = None

        #: 当前分组 ``[(图层, 升序 fid)]``
        self._groups: list[tuple[QgsVectorLayer, list[int]]] = []
        self._current_layer: Optional[QgsVectorLayer] = None
        self._model: Optional[_SelectionPageModel] = None
        self._page_size = _DEFAULT_PAGE_SIZE

        # 选中集重建的防抖计时器（见模块 docstring：一次框选会连发多次信号）
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(_RELOAD_DEBOUNCE_MS)
        self._reload_timer.timeout.connect(self.refresh)

        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        # 必须是裸构造的 QVBoxLayout 再 setLayout（见 BaseDialog docstring）：
        # QVBoxLayout(self) 会被 Qt 静默忽略，内容不显示。
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)

        self.header = QLabel("当前选择集 · 0 个要素")
        self.header.setObjectName("SectionLabel")
        layout.addWidget(self.header)

        # 图层下拉：不同图层字段不同，没法共用一行表头，所以一次看一层。
        # 只有一层有选中时藏起来（那时它只有一个选项，白占一行）。
        self.layer_combo = QComboBox(self)
        self.layer_combo.currentIndexChanged.connect(self._on_layer_changed)
        self.layer_combo.setVisible(False)
        layout.addWidget(self.layer_combo)

        self.table = QTableView(self)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.table.clicked.connect(self._on_cell_clicked)
        layout.addWidget(self.table, 1)

        pager = QHBoxLayout()
        pager.setSpacing(6)
        self.page_label = QLabel("")
        self.page_label.setObjectName("PropHint")
        pager.addWidget(self.page_label)
        pager.addStretch(1)
        pager.addWidget(QLabel("每页"))
        self.page_size_combo = QComboBox(self)
        for size in _PAGE_SIZE_CHOICES:
            self.page_size_combo.addItem(str(size), size)
        self.page_size_combo.setCurrentIndex(_PAGE_SIZE_CHOICES.index(_DEFAULT_PAGE_SIZE))
        self.page_size_combo.currentIndexChanged.connect(self._on_page_size_changed)
        pager.addWidget(self.page_size_combo)
        self.prev_btn = QPushButton("上一页")
        self.prev_btn.clicked.connect(lambda: self._set_page(
            (self._model.page - 1) if self._model is not None else 0))
        pager.addWidget(self.prev_btn)
        self.next_btn = QPushButton("下一页")
        self.next_btn.clicked.connect(lambda: self._set_page(
            (self._model.page + 1) if self._model is not None else 0))
        pager.addWidget(self.next_btn)
        layout.addLayout(pager)

        hint = QLabel(_HINT)
        hint.setObjectName("PropHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.locate_btn = QPushButton("定位")
        self.locate_btn.setEnabled(False)   # 没有当前行时不可用，见 _sync_locate_btn
        self.locate_btn.clicked.connect(self._on_locate)
        bottom.addWidget(self.locate_btn)
        self.clear_btn = QPushButton("清除选择")
        self.clear_btn.clicked.connect(self._on_clear)
        bottom.addWidget(self.clear_btn)
        self.close_btn = QPushButton("关闭")
        self.close_btn.setObjectName("DialogPrimary")
        self.close_btn.clicked.connect(self.close)
        bottom.addWidget(self.close_btn)
        layout.addLayout(bottom)

        self.setLayout(layout)

    # ------------------------------------------------------------ 数据

    def _project_layers(self) -> list[QgsVectorLayer]:
        """工程里的全部矢量图层（挂选中信号的依据）。"""
        try:
            return [
                item
                for item in self._project.mapLayers().values()
                if isinstance(item, QgsVectorLayer) and item.isValid()
            ]
        except RuntimeError:
            return []

    def _collect(self) -> list[tuple[QgsVectorLayer, list[int]]]:
        """当前**有选中要素**的图层 → ``(图层, 升序 fid 列表)``。

        顺序取 ``canvas.layers()``（索引 0 画在最上，与框选工具的命中顺序一致），
        不在画布但有选中的图层按工程顺序补在后面。fid 一律升序：``selectedFeatureIds()``
        的顺序没有保证，不排序的话用户 Ctrl 加选一个，整个表就会重排。
        """
        ordered: list[QgsVectorLayer] = []
        if self._canvas is not None:
            try:
                ordered.extend(
                    layer
                    for layer in self._canvas.layers()
                    if isinstance(layer, QgsVectorLayer)
                )
            except RuntimeError:
                pass
        seen = {layer.id() for layer in ordered}
        ordered.extend(layer for layer in self._project_layers() if layer.id() not in seen)

        groups: list[tuple[QgsVectorLayer, list[int]]] = []
        for layer in ordered:
            try:
                if sip.isdeleted(layer) or not layer.isValid():
                    continue
                fids = layer.selectedFeatureIds()
            except RuntimeError:
                continue  # 图层销毁流程中
            if fids:
                groups.append((layer, sorted(int(fid) for fid in fids)))
        return groups

    # ------------------------------------------------------------ 刷新

    def refresh(self) -> None:
        """按当前选中集重建表格（防抖到期与外部入口都走这里）。"""
        self._reload_timer.stop()
        try:
            self._groups = self._collect()
        except RuntimeError:
            return  # 窗口销毁流程中

        total = sum(len(fids) for _layer, fids in self._groups)
        self.header.setText(f"当前选择集 · {total} 个要素")
        self.clear_btn.setEnabled(total > 0)

        keep_id = self._current_layer.id() if self._current_layer is not None else None
        ids = [layer.id() for layer, _fids in self._groups]
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        for layer, fids in self._groups:
            self.layer_combo.addItem(f"{layer.name()}（{len(fids)}）", layer.id())
        if keep_id in ids:
            self.layer_combo.setCurrentIndex(ids.index(keep_id))
        self.layer_combo.blockSignals(False)
        self.layer_combo.setVisible(len(self._groups) > 1)

        self._rebuild_table()

    def _rebuild_table(self) -> None:
        """按下拉框选中的图层重建模型（尽量停在上次的页码上）。"""
        layer_id = self.layer_combo.currentData() if self.layer_combo.count() else None
        group = next((g for g in self._groups if g[0].id() == layer_id), None)
        page = self._model.page if self._model is not None else 0
        if group is None:
            self._current_layer = None
            self._model = None
            self.table.setModel(_MessageModel(_EMPTY_TEXT, parent=self))
        else:
            self._current_layer, fids = group
            self._model = _SelectionPageModel(
                self._current_layer, fids, self._page_size, page=page, parent=self
            )
            self.table.setModel(self._model)
            self.table.resizeColumnToContents(0)  # ID 列定宽，其余留给用户拖
            selection = self.table.selectionModel()
            if selection is not None:
                # 换模型会换掉选中模型，所以每次重建都要重连（旧的一起销毁，不会漏连）
                selection.currentChanged.connect(self._on_current_row_changed)
        self._sync_pager()

    def _sync_pager(self) -> None:
        model = self._model
        if model is None:
            self.page_label.setText("共 0 条")
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            self._sync_locate_btn()
            return
        self.page_label.setText(f"共 {model.total} 条 · 第 {model.page + 1}/{model.page_count} 页")
        self.prev_btn.setEnabled(model.page > 0)
        self.next_btn.setEnabled(model.page < model.page_count - 1)
        self._sync_locate_btn()

    # ------------------------------------------------------------ 分页交互

    def _set_page(self, page: int) -> None:
        if self._model is None:
            return
        self._model.set_page(page)
        self._sync_pager()

    def _on_page_size_changed(self, index: int) -> None:
        size = self.page_size_combo.itemData(index)
        if not size:
            return
        self._page_size = int(size)
        if self._model is not None:
            self._model.set_page_size(self._page_size)
        self._sync_pager()

    def _on_layer_changed(self, _index: int) -> None:
        if self._torn_down:
            return
        self._rebuild_table()

    def _sync_locate_btn(self) -> None:
        """没有当前行（或没有数据）时「定位」不可点。"""
        _layer, fid = self._current_fid()
        self.locate_btn.setEnabled(fid is not None)

    def _on_current_row_changed(self, *_args) -> None:
        self._sync_locate_btn()

    def _current_fid(self) -> tuple[Optional[QgsVectorLayer], Optional[int]]:
        """表格当前行对应的 ``(图层, fid)``；没有则 ``(None, None)``。"""
        if self._model is None or self._current_layer is None:
            return None, None
        index = self.table.currentIndex()
        if not index.isValid():
            return None, None
        fid = self._model.fid_at(index.row())
        if fid is None:
            return None, None
        return self._current_layer, int(fid)

    # ------------------------------------------------------------ 点行闪烁

    def _on_cell_clicked(self, index) -> None:
        """点某一行 → 在画布上闪烁该要素。

        **不顺便缩放到它**：缩放会把用户的视野挪走，而"这是哪一个图斑"大多数时候闪一下
        就够看清了。真的挤成一片看不见时，用户按「定位」显式来一下（见 :meth:`_on_locate`）。
        """
        if self._model is None or self._current_layer is None:
            return
        fid = self._model.fid_at(index.row())
        if fid is None:
            return
        self._flash(self._current_layer, int(fid))

    def _on_locate(self) -> None:
        """定位到当前行：缩放到该要素并闪一下。

        与点行的区别就在这个"缩放"：数据多的时候几个图斑挤在一起，只闪一下根本看不出
        是哪一个；放大到它（还留一圈余量，免得只剩它自己、看不出位置关系）就一目了然。
        """
        layer, fid = self._current_fid()
        if layer is None:
            return
        self._zoom_to_feature(layer, fid)
        self._flash(layer, fid)

    def _zoom_to_feature(self, layer: QgsVectorLayer, fid: int) -> None:
        """把画布缩放到该要素（含余量）。

        骨架与 ``layer_actions.zoom_to_layer_extent_transform`` / ``_zoom_to_selection``
        一致（取 bbox → 换算到画布 CRS → ``setExtent`` + ``refresh``，脏几何越界时退回
        原始范围）；差别只在这里**在画布 CRS 里加了一圈余量**，让用户看得见它周围——
        属性表那个「缩放至要素」是不留余量的，单独看一个图斑时容易被撑满整屏。
        """
        canvas = self._canvas
        if canvas is None:
            return
        raw = None
        ext = None
        try:
            feature = layer.getFeature(int(fid))
            if not feature.isValid():
                return
            geom = feature.geometry()
            if geom is None or geom.isNull() or geom.isEmpty():
                return
            raw = geom.boundingBox()
            dest_crs = canvas.mapSettings().destinationCrs()
            if layer.crs() != dest_crs:
                xform = QgsCoordinateTransform(layer.crs(), dest_crs, self._project)
                ext = xform.transformBoundingBox(raw)
            else:
                ext = QgsRectangle(raw)
        except QgsCsException:
            # 脏几何（坐标与声明的 CRS 不符）反变换越界：退回原始范围直接缩，不崩
            logger.warning("选择集：定位要素越界，改用原始范围 layer=%s fid=%s", layer.name(), fid)
            ext = raw
        except RuntimeError:
            return  # 图层 / 画布已销毁
        if ext is None or ext.isNull():
            return

        pad = max(ext.width(), ext.height()) * _ZOOM_PADDING_RATIO
        if pad <= 0.0:
            # 点 / 零尺寸要素：按当前视野给一圈余量，避免 setExtent 拿到空范围
            cur = canvas.mapSettings().extent()
            pad = max(cur.width(), cur.height()) * 0.1
        ext = QgsRectangle(ext)
        ext.grow(pad)
        canvas.setExtent(ext)
        canvas.refresh()

    def _flash(self, layer: QgsVectorLayer, fid: int) -> None:
        """在目标图层上闪烁该要素，**闪完就收掉高亮带**。

        `FeatureHighlighter` 默认闪完保持可见（那是给「补缝 / 合并」对照挑面用的）；
        本面板只要闪一下说明「是哪一个图斑」，一直亮着反而挡视线，所以接它的
        ``finished`` 在闪完后自己 ``clear()``。

        高亮带在构造函数里吃 layer，一个实例服务不了多层；这里只保留一个实例，
        目标层变了就收掉重建（比每层缓存一个更省，也不会漏收）。在别的层上闪会指错。
        """
        if self._highlighter is None or self._highlighter_layer_id != layer.id():
            self._release_highlighter()
            try:
                self._highlighter = FeatureHighlighter(self._canvas, layer, parent=self)
            except RuntimeError:
                self._highlighter = None
                return
            self._highlighter.finished.connect(self._on_flash_finished)
            self._highlighter_layer_id = layer.id()
        if self._highlighter is not None:
            self._highlighter.flash(fid)

    def _on_flash_finished(self) -> None:
        """闪完收掉高亮带（实例留着，下次点行继续用）。"""
        if self._highlighter is not None:
            self._highlighter.clear()

    def _release_highlighter(self) -> None:
        """收掉高亮带。**必须显式收**：``QgsRubberBand`` 活在画布 scene 里，
        不在本对话框的 QObject 树里，不 ``close()`` 就残留在图上。"""
        highlighter, self._highlighter = self._highlighter, None
        self._highlighter_layer_id = None
        if highlighter is None:
            return
        try:
            highlighter.close()
        except Exception:  # noqa: BLE001 —— 收尾失败不该影响关窗
            logger.exception("选择集：释放要素高亮带失败")

    # ------------------------------------------------------------ 清除 / 关闭

    def _on_clear(self) -> None:
        """只清**本面板列出的那些图层**的选中集。

        不能走 ``MainWindow._on_clear_selection``：那一个会遍历工程里**所有**图层，
        还会调 ``_clear_temp_graphics()`` 抹掉测量临时图形——而用户在这里只是想清掉
        刚框出来的这一批。清完面板不关，刷新成 0（否则与「关闭」重复）。
        选择屏蔽是屏蔽那一刻固定的 ``$id IN (…)`` 渲染条件，与选中集无关，不用管。
        """
        for layer, _fids in self._collect():
            try:
                layer.removeSelection()
            except RuntimeError:
                continue  # 图层销毁流程中
        self.refresh()

    # ------------------------------------------------------------ 生命周期

    def showEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        super().showEvent(event)
        self._torn_down = False
        self._attach()
        self.refresh()  # 每次重新打开都对齐当前选中集

    def hideEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        super().hideEvent(event)
        self._teardown()

    def done(self, result: int) -> None:  # noqa: N802 —— Qt 命名
        # × / Esc / reject 的收尾兜底（hideEvent 一般已经收过，_teardown 幂等）
        self._teardown()
        super().done(result)

    def _attach(self) -> None:
        """挂上选中集监听（幂等）。"""
        if self._attached:
            return
        self._attached = True
        try:
            self._project.layersAdded.connect(self._on_layers_changed)
            self._project.layersRemoved.connect(self._on_layers_changed)
        except Exception:  # noqa: BLE001
            logger.exception("选择集：连接工程图层增删信号失败")
        self._sync_listeners()

    def _detach(self) -> None:
        """断开全部监听（幂等）。"""
        if not self._attached:
            return
        self._attached = False
        for signal in (self._project.layersAdded, self._project.layersRemoved):
            try:
                signal.disconnect(self._on_layers_changed)
            except Exception:  # noqa: BLE001 —— 收尾场景属正常
                pass
        for layer in self._project_layers():
            self._unhook(layer)
        self._hooked.clear()

    def _on_layers_changed(self, *_args) -> None:
        if self._torn_down:
            return
        self._sync_listeners()
        self._reload_timer.start()

    def _sync_listeners(self, *_args) -> None:
        """让已连图层的集合与工程矢量图层集合一致。

        按**工程**图层挂而不是画布图层：``layersAdded`` 触发时画布的图层列表还没
        同步（QgsLayerTreeMapCanvasBridge 是异步的），按画布挂会漏掉刚加进来的那层
        ——它之后就没被监听，选中永远刷不出来。与 ``SelectionOverlay.sync`` 同款。
        """
        targets = self._project_layers()
        alive = {layer.id() for layer in targets}
        for layer_id in list(self._hooked - alive):
            stale = self._project.mapLayer(layer_id)
            if stale is not None:
                self._unhook(stale)
            self._hooked.discard(layer_id)
        for layer in targets:
            if layer.id() not in self._hooked:
                self._hook(layer)

    def _hook(self, layer: QgsVectorLayer) -> None:
        try:
            layer.selectionChanged.connect(self._on_selection_changed)
            self._hooked.add(layer.id())
        except Exception:  # noqa: BLE001 —— 图层销毁等收尾场景属正常
            logger.warning("选择集：连接 %s 的选中信号失败", layer.name(), exc_info=True)

    def _unhook(self, layer: QgsVectorLayer) -> None:
        try:
            layer.selectionChanged.disconnect(self._on_selection_changed)
        except Exception:  # noqa: BLE001 —— 图层销毁等收尾场景属正常
            pass

    def _on_selection_changed(self, *_args) -> None:
        # 防抖：一次框选跨 N 层会连发 N 次，不防抖会重建 N 次、表格闪
        self._reload_timer.start()

    def _teardown(self) -> None:
        """幂等收尾：停防抖、断监听、收高亮带。"""
        if self._torn_down:
            return
        self._torn_down = True
        timer = getattr(self, "_reload_timer", None)
        if timer is not None:
            timer.stop()
        self._detach()
        self._release_highlighter()


__all__ = ["SelectionSetDialog"]
