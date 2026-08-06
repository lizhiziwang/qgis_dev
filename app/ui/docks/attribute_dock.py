"""分页属性表 Dock。

迁自 :class:`component.map_tool.PagedAttributeDockWidget` 等 5 个类。"""

from __future__ import annotations

import math
from typing import Any, List, Optional

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt, QThread, QVariant, pyqtSignal
from PyQt5.QtWidgets import (
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
from PyQt5.QtGui import QIcon

from app.core.domain.editing import LayerEditService
from app.core.domain.identify import FeatureIdentifyService
from app.ui.common.icons import Icons


class FeatureLoaderThread(QThread):
    """后台加载要素 ID（用于大量数据时的进度条）。"""

    progress_updated = pyqtSignal(int)
    features_loaded = pyqtSignal(list)
    finished = pyqtSignal()

    def __init__(self, layer, filter_str: str, show_ids: Optional[list] = None, parent=None) -> None:
        super().__init__(parent)
        self.layer = layer
        self.filter_str = filter_str
        self.show_ids = show_ids

    def run(self) -> None:
        try:
            total = self.layer.featureCount()
            if total == 0:
                self.features_loaded.emit([])
                return
            step = max(1, total // 100)
            ids: List[int] = []
            request = QgsFeatureRequest().setFilterExpression(self.filter_str)
            for i, feature in enumerate(self.layer.getFeatures(request), 1):
                if self.show_ids is not None and len(self.show_ids) > 0:
                    if feature.id() in self.show_ids:
                        ids.append(feature.id())
                else:
                    ids.append(feature.id())
                if i % step == 0 or i == total:
                    self.progress_updated.emit(int(i / total * 100))
            ids.sort()
            self.features_loaded.emit(ids)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.finished.emit()


class PagedAttributeTableModel(QAbstractTableModel):
    """分页属性表 model。"""

    def __init__(self, layer: QgsVectorLayer, page_size: int = 50, filter_str: str = "1=1", show_ids: Optional[list] = None) -> None:
        super().__init__()
        self.layer = layer
        self.page_size = page_size
        self.filter_str = filter_str
        self.show_ids = list(show_ids) if show_ids else []
        self.field_names = [f.name() for f in layer.fields()]
        self.features: List[QgsFeature] = []
        self.is_loading = True
        where = (
            f"{self.filter_str} and $id in ({','.join(str(i) for i in self.show_ids)})"
            if self.show_ids else self.filter_str
        )
        self.where = where
        request = QgsFeatureRequest().setFilterExpression(where)
        try:
            self.total_features = len(list(layer.getFeatures(request)))
        except Exception:  # noqa: BLE001
            self.total_features = 0
        effective_size = min(self.page_size, max(self.total_features, 1)) if self.total_features else self.page_size
        self._effective_size = effective_size
        self.total_pages = max(1, math.ceil(self.total_features / max(self.page_size, 1))) if self.total_features else 1
        self._load_page()

    def _load_page(self) -> None:
        self.beginResetModel()
        self.features = []
        try:
            tmp = max(1, int(1.5 * self.page_size))
            size = min(self.total_features, tmp) if self.total_features else 0
            ob = QgsFeatureRequest.OrderByClause("$id", ascending=True)
            request = (
                QgsFeatureRequest()
                .setLimit(size)
                .setOrderBy(QgsFeatureRequest.OrderBy([ob]))
                .setFilterExpression(self.where)
            )
            self.features = list(self.layer.getFeatures(request))
        except Exception:  # noqa: BLE001
            self.features = []
        self.endResetModel()
        self.is_loading = False

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self.features)

    def columnCount(self, parent=QModelIndex()) -> int:
        if self.is_loading:
            return 0
        return len(self.field_names) + 1

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or self.is_loading or index.row() >= len(self.features):
            return None
        if role in (Qt.DisplayRole, Qt.EditRole):
            feature = self.features[index.row()]
            if index.column() == 0:
                return feature.id()
            field_idx = index.column() - 1
            if field_idx < len(feature.fields()):
                return feature.attribute(field_idx)
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            if section == 0:
                return "ID"
            if section - 1 < len(self.field_names):
                return self.field_names[section - 1]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemIsEnabled
        if index.column() == 0:
            return Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if self.layer.isEditable():
            return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid() or role != Qt.EditRole:
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
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setEditTriggers(QTableView.DoubleClicked)
        self.table_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        header = self.table_view.horizontalHeader()
        header.setMinimumSectionSize(20)
        header.setMaximumSectionSize(400)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(False)
        self.table_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self._show_context_menu)
        main_layout.addWidget(self.table_view)

        self.setLayout(main_layout)

        self.model = PagedAttributeTableModel(self.layer, self.page_size, filter_str=self.filter_str, show_ids=self.show_ids)
        self.table_view.setModel(self.model)

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
        dlg.sqlConfirmed.connect(lambda: self._reload_model())
        dlg.exec_()

    def _reload_model(self) -> None:
        if self.model:
            self.model._load_page()

    def _show_context_menu(self, position) -> None:
        from PyQt5.QtWidgets import QMenu, QAction
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
        sel.triggered.connect(lambda: self.layer.selectByIds([feature.id()]))
        menu.addAction(sel)
        if self.layer.isEditable():
            delete = QAction("删除", self)
            delete.triggered.connect(lambda: self.layer.deleteFeature(fid=feature.id()))
            menu.addAction(delete)
        menu.exec_(self.table_view.viewport().mapToGlobal(position))


class AttributeDock(QDockWidget):
    """分页属性表 dock。"""

    def __init__(self, layer, page_size: int = 50, parent=None, filter_str: str = "1=1", show_ids: Optional[list] = None) -> None:
        title = f"属性表 - {layer.name()}"
        super().__init__(title, parent)
        self.setFeatures(QDockWidget.AllDockWidgetFeatures)
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)
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
        elif action_type == "zoom_to_features":
            self.topParent.canvas.setExtent(data["extent"])
            self.layer.selectByIds(data["ids"])
            self.topParent.canvas.refresh()
