"""ArcMap 风格「I 查询」结果面板。

布局参考 ArcMap 的 Identify 窗口：

- 顶部「识别来源」下拉（最顶层图层 / 所有可见图层 / 所有图层 / 指定图层）；
- 中部结果树：按图层分组，列出一次点击命中的全部要素；
- 下部「位置」+ 属性表：两列（字段 / 值）展示当前选中要素的属性；
- 底部「复制信息」「关闭」。

面板只负责展示与选择；点击地图、重新识别、地图高亮由
:class:`app.ui.tools.i_query.IQueryTool` 负责。
"""

from __future__ import annotations

from qgis.core import QgsMapLayer, QgsProject
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.common.value_format import format_value
from app.ui.tools.feature_attrs import field_alias

#: 「识别来源」下拉里内置选项的 data 值（具体图层用 ``layer:<id>``）
_SOURCE_TOP_MOST = "topmost"
_SOURCE_VISIBLE = "visible"
_SOURCE_ALL = "all"
_SOURCE_LAYER = "layer"
_LAYER_PREFIX = "layer:"


class IQueryDock(QDockWidget):
    """I 查询结果 Dock（ArcMap Identify 布局）。"""

    #: 结果树切换选中项 → ``IdentifyResult``（``None`` = 没有选中）
    result_selected = pyqtSignal(object)
    #: 双击结果树里的要素 → ``IdentifyResult``（地图定位 + 闪烁）
    feature_activated = pyqtSignal(object)
    #: 「识别来源」变化（工具据此在最后点击位置重查）
    source_changed = pyqtSignal()
    #: 面板关闭（工具据此清掉地图高亮）
    closed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__("I查询", parent)
        self.setObjectName("IQueryDock")
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.setMinimumSize(360, 300)
        #: 最近一次点击命中的全部结果
        self._results: list = []
        #: 当前选中并显示在属性表里的结果
        self.result = None
        QgsProject.instance().layersAdded.connect(self._on_layers_changed)
        QgsProject.instance().layersRemoved.connect(self._on_layers_changed)
        QgsProject.instance().layersWillBeRemoved.connect(self._on_layers_will_be_removed)
        self._init_ui()
        self._refresh_sources()
        # 放在初始填充之后接：避免建面板时误触发一次「重新识别」
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)

    # ------------------------------------------------------------ UI

    def _init_ui(self) -> None:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        source_row = QHBoxLayout()
        source_row.setContentsMargins(0, 0, 0, 0)
        source_row.setSpacing(6)
        source_row.addWidget(QLabel("识别来源:"))
        self.source_combo = QComboBox()
        self.source_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.source_combo.setMinimumContentsLength(12)
        source_row.addWidget(self.source_combo, 1)
        layout.addLayout(source_row)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # 结果树：图层（父）→ 要素（子）
        results_box = QWidget()
        results_layout = QVBoxLayout(results_box)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(4)
        self.results_label = QLabel("识别结果（0）")
        results_layout.addWidget(self.results_label)
        self.results_tree = QTreeWidget()
        self.results_tree.setHeaderHidden(True)
        self.results_tree.setAlternatingRowColors(True)
        self.results_tree.currentItemChanged.connect(self._on_current_item_changed)
        self.results_tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        results_layout.addWidget(self.results_tree)
        splitter.addWidget(results_box)

        # 属性：位置 + 字段/值表
        attrs_box = QWidget()
        attrs_layout = QVBoxLayout(attrs_box)
        attrs_layout.setContentsMargins(0, 0, 0, 0)
        attrs_layout.setSpacing(4)
        self.location_label = QLabel("位置: -")
        attrs_layout.addWidget(self.location_label)
        self.attributes_table = QTableWidget(0, 2)
        self.attributes_table.setHorizontalHeaderLabels(["字段", "值"])
        self.attributes_table.verticalHeader().setVisible(False)
        self.attributes_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.attributes_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.attributes_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.attributes_table.customContextMenuRequested.connect(self._on_attributes_context_menu)
        header = self.attributes_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        attrs_layout.addWidget(self.attributes_table)
        splitter.addWidget(attrs_box)

        # 结果列表 : 详情（位置 + 属性表）高度固定 1:4，窗口缩放时保持比例
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([100, 400])
        layout.addWidget(splitter, 1)

        buttons = QHBoxLayout()
        self.copy_btn = QPushButton(QIcon.fromTheme("edit-copy"), "复制信息")
        self.copy_btn.clicked.connect(self._copy)
        self.close_btn = QPushButton(QIcon.fromTheme("window-close"), "关闭")
        self.close_btn.clicked.connect(self.close)
        buttons.addWidget(self.copy_btn)
        buttons.addWidget(self.close_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.setWidget(widget)

    # ------------------------------------------------------------ 识别来源

    def _query_layers(self) -> list:
        """可参与识别的图层，按图层面板从上到下的顺序。"""
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        try:
            layers = list(root.layerOrder()) if root is not None else list(project.mapLayers().values())
        except RuntimeError:
            layers = list(project.mapLayers().values())
        return [layer for layer in layers if layer is not None and layer.flags() & QgsMapLayer.LayerFlag.Identifiable]

    def _refresh_sources(self) -> None:
        """重建「识别来源」下拉，尽量保留当前选择。"""
        current = self.source_combo.currentData()
        self.source_combo.blockSignals(True)
        try:
            self.source_combo.clear()
            self.source_combo.addItem("<<最顶层图层>>", _SOURCE_TOP_MOST)
            self.source_combo.addItem("<<所有可见图层>>", _SOURCE_VISIBLE)
            self.source_combo.addItem("<<所有图层>>", _SOURCE_ALL)
            self.source_combo.insertSeparator(self.source_combo.count())
            for layer in self._query_layers():
                self.source_combo.addItem(layer.name(), f"{_LAYER_PREFIX}{layer.id()}")
            index = self.source_combo.findData(current) if current else 0
            self.source_combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self.source_combo.blockSignals(False)

    def _on_source_changed(self, *_args) -> None:
        self.source_changed.emit()

    def identify_source(self):
        """返回 ``(来源类型, 图层列表)``。

        来源类型：``topmost`` / ``visible`` / ``all`` / ``layer``。
        ``topmost`` / ``visible`` 由 QGIS 按可见图层自行筛选，图层列表为 ``None``；
        ``all`` 返回全部可识别图层；``layer`` 返回选中的单个图层。
        """
        data = self.source_combo.currentData()
        if data == _SOURCE_TOP_MOST:
            return _SOURCE_TOP_MOST, None
        if data == _SOURCE_ALL:
            return _SOURCE_ALL, self._query_layers()
        if isinstance(data, str) and data.startswith(_LAYER_PREFIX):
            layer = QgsProject.instance().mapLayer(data[len(_LAYER_PREFIX) :])
            if layer is not None:
                return _SOURCE_LAYER, [layer]
        return _SOURCE_VISIBLE, None

    def _on_layers_changed(self, *_args) -> None:
        self._refresh_sources()

    # ------------------------------------------------------------ 结果展示

    def show_results(self, results, location: str = "") -> None:
        """展示一次识别命中的全部结果，默认选中第一条。"""
        self._results = list(results or [])
        self.location_label.setText(f"位置: {location}" if location else "位置: -")
        first_item = None
        total = 0
        layer_counts: dict[str, int] = {}
        self.results_tree.blockSignals(True)
        try:
            self.results_tree.clear()
            layer_items: dict[str, QTreeWidgetItem] = {}
            for result in self._results:
                try:
                    layer_name = result.layer.name()
                    layer_id = result.layer.id()
                except RuntimeError:  # 图层已销毁，跳过
                    continue
                parent = layer_items.get(layer_id)
                if parent is None:
                    parent = QTreeWidgetItem([layer_name])
                    # 图层节点只做分组，不允许选中（避免把整层当成一条结果）
                    parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                    self.results_tree.addTopLevelItem(parent)
                    layer_items[layer_id] = parent
                layer_counts[layer_id] = layer_counts.get(layer_id, 0) + 1
                parent.setText(0, f"{layer_name}（{layer_counts[layer_id]}）")
                child = QTreeWidgetItem([self._result_label(result)])
                child.setData(0, Qt.ItemDataRole.UserRole, result)
                child.setToolTip(0, self._result_tooltip(result))
                parent.addChild(child)
                total += 1
                if first_item is None:
                    first_item = child
            self.results_tree.expandAll()
            if first_item is not None:
                self.results_tree.setCurrentItem(first_item)
        finally:
            self.results_tree.blockSignals(False)
        self.results_label.setText(f"识别结果（{total}）")

        if first_item is not None:
            self._show_result(first_item.data(0, Qt.ItemDataRole.UserRole))
            # 第一次真的点到要素才弹出；只是激活工具不打扰界面
            self.show()
            self.raise_()
        else:
            self._clear_result()

    def _on_current_item_changed(self, current, _previous) -> None:
        if current is not None and current.data(0, Qt.ItemDataRole.UserRole) is None:
            # 点到了图层分组节点：保持当前要素不变（分组节点只是分类）
            return
        result = current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None
        if result is None:
            self._clear_result()
            self.result_selected.emit(None)
            return
        self._show_result(result)
        self.result_selected.emit(result)

    def _on_item_double_clicked(self, item, _column) -> None:
        """双击结果树里的要素 → 请求地图定位并闪烁。"""
        result = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if result is not None:
            self.feature_activated.emit(result)

    def _show_result(self, result) -> None:
        self.result = result
        self._fill_attributes(result)

    def _clear_result(self) -> None:
        self.result = None
        self.attributes_table.setRowCount(0)

    def _fill_attributes(self, result) -> None:
        rows = self._attribute_rows(result)
        self.attributes_table.setRowCount(len(rows))
        for row, (name, real_name, value, tip) in enumerate(rows):
            name_item = QTableWidgetItem(name)
            # 表格显示别名，但复制时要用真实字段名（见 _copy）
            name_item.setData(Qt.ItemDataRole.UserRole, real_name)
            name_item.setToolTip(tip)
            value_item = QTableWidgetItem(value)
            value_item.setToolTip(value)
            self.attributes_table.setItem(row, 0, name_item)
            self.attributes_table.setItem(row, 1, value_item)

    def _attribute_rows(self, result) -> list[tuple[str, str, str, str]]:
        """当前结果的属性行：``(显示名, 真实字段名, 值, 悬浮提示)``。"""
        if result.is_raster and not result.attributes:
            rows = []
            for band, value in result.raster_values.items():
                rows.append((str(band), str(band), format_value(value), str(band)))
            return rows

        field_info = self._field_info(result.layer)
        if result.attributes:
            items = list(result.attributes.items())
        elif result.feature is not None:
            try:
                items = [(field.name(), result.feature.attribute(field.name())) for field in result.layer.fields()]
            except RuntimeError:
                items = []
        else:
            items = []

        rows: list[tuple[str, str, str, str]] = []
        for name, value in items:
            real_name = str(name)
            display, tip = self._display_and_tip(real_name, field_info)
            rows.append((display, real_name, format_value(value) if value is not None else "", tip))
        return rows

    @staticmethod
    def _field_info(layer) -> dict:
        """字段名 → ``(别名, 类型名, 长度)``；别名缺失时用真实字段名。"""
        mapping: dict = {}
        try:
            fields = layer.fields()
        except RuntimeError:
            return mapping
        for i in range(fields.count()):
            field = fields.at(i)
            try:
                type_name = field.typeName() or ""
                length = int(field.length())
            except Exception:  # noqa: BLE001
                type_name, length = "", 0
            mapping[field.name()] = (field_alias(field) or field.name(), type_name, length)
        return mapping

    def _display_and_tip(self, name: str, field_info: dict) -> tuple[str, str]:
        alias, type_name, length = field_info.get(name, (name, "", 0))
        real = name
        display = alias or real
        tip = f"{alias}（{real}）" if alias and alias != real else real
        if type_name:
            tip += f"\n{type_name}"
            if length > 0:
                tip += f"({length})"
        return display, tip

    def _result_label(self, result) -> str:
        """结果树子节点的文案：优先常见名称字段，其次是要素 ID。"""
        if result.is_raster and not result.attributes:
            return "栅格像元"
        attrs = result.attributes or {}
        for key in ("名称", "name", "Name", "NAME", "title", "TITLE"):
            value = attrs.get(key)
            if value not in (None, ""):
                return str(value)
        return f"要素 ID: {result.fid}"

    @staticmethod
    def _result_tooltip(result) -> str:
        try:
            layer_name = result.layer.name()
        except RuntimeError:
            layer_name = "（图层已删除）"
        if result.is_raster and not result.attributes:
            return f"{layer_name}\n栅格像元"
        return f"{layer_name}\n要素 ID: {result.fid}"

    # ------------------------------------------------------------ 复制 / 清理

    def _on_attributes_context_menu(self, pos) -> None:
        """详情表右键：复制光标所在的单个单元格。"""
        item = self.attributes_table.itemAt(pos)
        if item is None:
            return
        # 右键先选中该单元格，菜单里的「复制」与看到的选中项一致
        self.attributes_table.clearSelection()
        item.setSelected(True)
        self.attributes_table.setCurrentItem(item)

        menu = QMenu(self)
        copy_action = menu.addAction("复制")
        chosen = menu.exec(self.attributes_table.viewport().mapToGlobal(pos))
        if chosen is copy_action:
            QApplication.clipboard().setText(item.text())

    def _copy(self) -> None:
        """把当前选中结果（图层、ID、位置、属性）复制为纯文本。"""
        result = self.result
        if result is None:
            return
        try:
            layer_name = result.layer.name()
        except RuntimeError:
            layer_name = ""
        lines = [f"图层: {layer_name}"]
        if not (result.is_raster and not result.attributes):
            lines.append(f"要素 ID: {result.fid}")
        location = self.location_label.text()
        if location and location != "位置: -":
            lines.append(location)
        for row in range(self.attributes_table.rowCount()):
            name_item = self.attributes_table.item(row, 0)
            value_item = self.attributes_table.item(row, 1)
            if name_item is not None and value_item is not None:
                # 表格显示的是字段别名；复制必须用真实字段名
                real_name = name_item.data(Qt.ItemDataRole.UserRole) or name_item.text()
                lines.append(f"  {real_name}: {value_item.text()}")
        QApplication.clipboard().setText("\n".join(lines))

    def _on_layers_will_be_removed(self, layer_ids) -> None:
        """结果里任一图层即将被删 → 清空结果，避免留下悬空引用。"""
        if not self._results or not layer_ids:
            return
        removed = set(layer_ids)
        for result in self._results:
            try:
                if result.layer.id() in removed:
                    self.clear()
                    self.result_selected.emit(None)
                    return
            except RuntimeError:
                self.clear()
                self.result_selected.emit(None)
                return

    def clear(self) -> None:
        """清空结果树与属性表。"""
        self._results = []
        self.results_tree.blockSignals(True)
        try:
            self.results_tree.clear()
        finally:
            self.results_tree.blockSignals(False)
        self.results_label.setText("识别结果（0）")
        self._clear_result()

    def closeEvent(self, event) -> None:
        self.closed.emit()
        super().closeEvent(event)
