"""空间查询 Dialog：按位置在多个图层中选择要素（QGIS「按位置选择」语义）。

界面参照旧版「空间查询」面板（``在以下图层中查询并选取`` + ``过滤条件``）：

- **目标图层**：勾选「在以下图层中查询并选取」里的图层（可多选）；
- **源图层**：「源图层」下拉选择作为查询参照的图层；
- **使用选中要素**：源图层当前有选中要素时自动勾选，否则不可勾选、使用全部要素；
  复选框文案实时显示源图层当前已选数量；
- **位置关系**：目标要素与源要素的空间关系（相交 / 包含 / 被包含 / 重叠 / 穿越 /
  接触 / 相等 / 不相交 / 距离范围内），与 QGIS「按位置选择」一致；
- **选择方式**：新建 / 增加 / 移除选择集，只写选择集，不改图层的定义查询。

与 :class:`~app.ui.dialogs.space_query.SpaceQueryDialog`（在地图上画几何再查询）
不同，本面板不需要绘制，直接拿源图层要素做参照。计算见
:mod:`app.core.domain.query.select_by_location`。

面板**非模态**：查询后可直接回主窗口图层列表 / 画布继续操作。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from qgis.core import QgsFeatureRequest, QgsProject, QgsVectorLayer
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.query.select_by_location import (
    DISTANCE_PREDICATE,
    match_feature_ids,
)
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.qgis_selection import SELECT_METHODS, selection_behavior

if TYPE_CHECKING:
    from qgis.core import QgsGeometry

logger = get_logger(__name__)

#: 空间关系：(界面文案, ``select_by_location`` 里的关系名)
_SPATIAL_METHODS: tuple[tuple[str, str], ...] = (
    ("与源图层要素相交", "intersects"),
    ("包含源图层要素", "contains"),
    ("被源图层要素包含", "within"),
    ("与源图层要素重叠", "overlaps"),
    ("被源图层要素穿越", "crosses"),
    ("与源图层要素接触", "touches"),
    ("等于源图层要素", "equals"),
    ("与源图层要素不相交", "disjoint"),
    ("在距离范围内", DISTANCE_PREDICATE),
)


class SpatialQueryDialog(BaseDialog):
    """按位置选择：目标图层要素与源图层要素做空间关系查询。"""

    def __init__(
        self,
        layers: list["QgsVectorLayer"],
        current_layer: "QgsVectorLayer | None" = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("空间查询")
        # 非模态：查询后用户仍要回主窗口点图层列表 / 画布
        self.setModal(False)
        self.setMinimumSize(560, 400)
        self.resize(660, 440)
        self._layers = list(layers)
        self._current_layer = self._find_layer(current_layer)
        self._hooked_layer: "QgsVectorLayer | None" = None
        self._init_ui()
        self._init_signals()
        self._populate_layers()

    # ------------------------------------------------------------ 装配

    def _init_ui(self) -> None:
        main = QVBoxLayout()
        main.setContentsMargins(10, 8, 10, 8)
        main.setSpacing(8)

        # 目标图层
        target_group = QGroupBox("在以下图层中查询并选取")
        target_layout = QVBoxLayout(target_group)
        self.target_list = QListWidget()
        self.target_list.setMinimumHeight(100)
        self.target_list.setMaximumHeight(160)
        target_layout.addWidget(self.target_list)
        main.addWidget(target_group)

        # 过滤条件
        filter_group = QGroupBox("过滤条件")
        filter_layout = QVBoxLayout(filter_group)

        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("源图层"))
        self.source_combo = QComboBox()
        self.source_combo.setToolTip("作为空间查询参照的源图层")
        source_row.addWidget(self.source_combo, 1)
        filter_layout.addLayout(source_row)

        self.use_selected_check = QCheckBox("使用选中要素（已选中 0 个要素）")
        self.use_selected_check.setEnabled(False)
        self.use_selected_check.setToolTip(
            "勾选后只用源图层当前选中的要素作为查询范围；否则使用源图层全部要素"
        )
        filter_layout.addWidget(self.use_selected_check)

        method_row = QHBoxLayout()
        method_row.addWidget(QLabel("位置关系"))
        self.method_combo = QComboBox()
        for label, key in _SPATIAL_METHODS:
            self.method_combo.addItem(label, key)
        method_row.addWidget(self.method_combo, 1)
        filter_layout.addLayout(method_row)

        # 距离行：只在「在距离范围内」时显示
        distance_row = QHBoxLayout()
        self.distance_label = QLabel("距离")
        distance_row.addWidget(self.distance_label)
        self.distance_spin = QDoubleSpinBox()
        self.distance_spin.setDecimals(6)
        self.distance_spin.setRange(0.0, 1_000_000_000_000.0)
        self.distance_spin.setValue(0.001)
        self.distance_spin.setMinimumWidth(130)
        distance_row.addWidget(self.distance_spin)
        self.distance_hint = QLabel("（目标图层坐标系单位）")
        self.distance_hint.setObjectName("PropHint")
        distance_row.addWidget(self.distance_hint)
        distance_row.addStretch(1)
        self.distance_widgets: list[QWidget] = [
            self.distance_label,
            self.distance_spin,
            self.distance_hint,
        ]
        filter_layout.addLayout(distance_row)
        for widget in self.distance_widgets:
            widget.hide()

        result_row = QHBoxLayout()
        result_row.addWidget(QLabel("选择方式"))
        self.result_combo = QComboBox()
        for label, action in SELECT_METHODS:
            self.result_combo.addItem(label, action)
        result_row.addWidget(self.result_combo, 1)
        self.query_btn = QPushButton("查询")
        self.query_btn.setObjectName("DialogPrimary")
        self.query_btn.setMinimumWidth(88)
        result_row.addWidget(self.query_btn)
        filter_layout.addLayout(result_row)

        main.addWidget(filter_group)

        self.result_log = QPlainTextEdit()
        self.result_log.setReadOnly(True)
        self.result_log.setPlaceholderText("点击「查询」后在此显示各图层的命中结果。")
        self.result_log.setMinimumHeight(90)
        self.result_log.setMaximumHeight(150)
        # 查询前不展示结果栏，把空间留给目标图层列表
        self.result_log.hide()
        main.addWidget(self.result_log)

        self.setLayout(main)

    def _init_signals(self) -> None:
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        self.method_combo.currentIndexChanged.connect(self._update_distance_visibility)
        self.query_btn.clicked.connect(self._on_query)
        self._update_distance_visibility()

    # ------------------------------------------------------------ 图层列表

    def _find_layer(self, layer: "QgsVectorLayer | None") -> "QgsVectorLayer | None":
        if layer is None or sip.isdeleted(layer):
            return None
        for candidate in self._layers:
            if candidate is layer or candidate.id() == layer.id():
                return candidate
        return None

    def source_layer(self) -> "QgsVectorLayer | None":
        """当前源图层（面板常驻期间图层可能已被移除）。"""
        layer = self.source_combo.currentData()
        if isinstance(layer, QgsVectorLayer) and not sip.isdeleted(layer):
            return layer
        return None

    def _populate_layers(self) -> None:
        """重建目标图层列表 / 源图层下拉；保留用户已勾选的目标图层。"""
        previous_checked: set[str] = set()
        for index in range(self.target_list.count()):
            item = self.target_list.item(index)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            layer = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(layer, QgsVectorLayer) and not sip.isdeleted(layer):
                previous_checked.add(layer.id())
        previous_source = self.source_layer()

        self.target_list.blockSignals(True)
        self.target_list.clear()
        for layer in self._layers:
            item = QListWidgetItem(layer.name())
            item.setData(Qt.ItemDataRole.UserRole, layer)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if layer.id() in previous_checked
                else Qt.CheckState.Unchecked
            )
            item.setToolTip(layer.source())
            self.target_list.addItem(item)
        self.target_list.blockSignals(False)

        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for layer in self._layers:
            self.source_combo.addItem(layer.name(), layer)
        target = (
            self._current_layer
            or self._find_layer(previous_source)
            or (self._layers[0] if self._layers else None)
        )
        if target is not None:
            for index in range(self.source_combo.count()):
                if self.source_combo.itemData(index) is target:
                    self.source_combo.setCurrentIndex(index)
                    break
        self.source_combo.blockSignals(False)
        self._hook_source_selection()

    def set_layers(
        self,
        layers: list["QgsVectorLayer"],
        current_layer: "QgsVectorLayer | None" = None,
    ) -> None:
        """刷新可查询图层（非模态面板复用同一实例时由主窗口调用）。"""
        source = self.source_layer()
        self._layers = list(layers)
        target = self._find_layer(current_layer) or self._find_layer(source)
        self._current_layer = target or (self._layers[0] if self._layers else None)
        self._populate_layers()

    # ------------------------------------------------------------ 源图层选中集

    def _hook_source_selection(self) -> None:
        """把「已选中 N 个要素」挂到当前源图层的选择集信号上。"""
        if self._hooked_layer is not None:
            try:
                self._hooked_layer.selectionChanged.disconnect(self._update_selected_label)
            except (TypeError, RuntimeError):
                pass
        layer = self.source_layer()
        if layer is not None:
            layer.selectionChanged.connect(self._update_selected_label)
        self._hooked_layer = layer
        self._update_selected_label()

    def _on_source_changed(self, _index: int = -1) -> None:
        self._hook_source_selection()

    def _update_selected_label(self, *_args) -> None:
        layer = self.source_layer()
        count = layer.selectedFeatureCount() if layer is not None else 0
        self.use_selected_check.setText(f"使用选中要素（已选中 {count} 个要素）")
        self.use_selected_check.setEnabled(count > 0)
        # 源图层存在选中要素时默认勾选，没有选中则取消
        self.use_selected_check.setChecked(count > 0)

    def checked_layers(self) -> list["QgsVectorLayer"]:
        """目标图层列表里勾选的图层。"""
        layers: list[QgsVectorLayer] = []
        for index in range(self.target_list.count()):
            item = self.target_list.item(index)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            layer = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(layer, QgsVectorLayer) and not sip.isdeleted(layer):
                layers.append(layer)
        return layers

    # ------------------------------------------------------------ 查询

    def _update_distance_visibility(self, _index: int = -1) -> None:
        is_distance = self.method_combo.currentData() == DISTANCE_PREDICATE
        for widget in self.distance_widgets:
            widget.setVisible(is_distance)

    def _source_geometries(self, layer: "QgsVectorLayer") -> list["QgsGeometry"]:
        """源图层参与查询的几何：选中要素或全部要素。"""
        use_selected = self.use_selected_check.isChecked()
        features = (
            layer.selectedFeatures()
            if use_selected
            else layer.getFeatures(QgsFeatureRequest().setNoAttributes())
        )
        geometries: list[QgsGeometry] = []
        for feature in features:
            geometry = feature.geometry()
            if geometry is not None and not geometry.isEmpty():
                geometries.append(geometry)
        return geometries

    def _append_log(self, text: str) -> None:
        self.result_log.appendPlainText(text)

    def _on_query(self) -> None:
        """按选择方式把各目标图层的命中要素写入选择集。"""
        source = self.source_layer()
        if source is None:
            QMessageBox.warning(self, "无法查询", "请选择源图层。")
            return
        targets = self.checked_layers()
        if not targets:
            QMessageBox.warning(self, "无法查询", "请至少勾选一个目标图层。")
            return

        geometries = self._source_geometries(source)
        if not geometries:
            message = (
                "源图层当前没有选中要素，请先选择要素或取消勾选「使用选中要素」。"
                if self.use_selected_check.isChecked()
                else "源图层中没有可用的要素几何。"
            )
            QMessageBox.warning(self, "无法查询", message)
            return

        predicate = str(self.method_combo.currentData() or "intersects")
        method_label = self.method_combo.currentText()
        distance = self.distance_spin.value() if predicate == DISTANCE_PREDICATE else 0.0
        behavior = selection_behavior(str(self.result_combo.currentData() or "SetSelection"))

        self.result_log.clear()
        if self.result_log.isHidden():
            # 查询前不显示结果栏；第一次查询时把面板拉高一点放下结果
            self.result_log.show()
            self.resize(self.width(), self.height() + 150)
        header = f"源图层「{source.name()}」：参与要素 {len(geometries)} 个；空间关系：{method_label}"
        if predicate == DISTANCE_PREDICATE:
            header += f"（{distance:g}）"
        self._append_log(header)

        total = 0
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for target in targets:
                try:
                    fids = match_feature_ids(
                        target, geometries, source.crs(), predicate, distance
                    )
                    target.selectByIds(fids, behavior)
                except Exception as e:  # noqa: BLE001 —— 单层失败不影响其它层
                    logger.exception("空间查询失败：%s", target.name())
                    self._append_log(f"图层「{target.name()}」：查询失败（{e}）")
                    continue
                total += len(fids)
                self._append_log(
                    f"图层「{target.name()}」：命中 {len(fids)} 个要素，"
                    f"当前已选 {target.selectedFeatureCount()} 个。"
                )
                QApplication.processEvents()
        finally:
            QApplication.restoreOverrideCursor()

        self._append_log(f"查询完成：共命中 {total} 个要素。")
