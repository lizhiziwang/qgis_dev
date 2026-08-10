"""Execute SQL Dialog（ogr SQL 查询 + 结果表）。"""

from __future__ import annotations

import time
from typing import Any, List, Optional

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt5.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QTextEdit,
    QVBoxLayout,
)

from osgeo import ogr

from qgis.core import QgsVectorLayer

from app.infra.logging import get_logger
from app.ui.common.icons import Icons


logger = get_logger(__name__)


class VectorLayerTableModel(QAbstractTableModel):
    def __init__(self, data: List[List[Any]], header: List[str], parent=None) -> None:
        super().__init__(parent)
        self._data = data
        self._header = header

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._data)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self._header) if self._data else 0

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.DisplayRole:
            v = self._data[index.row()][index.column()]
            return str(v) if v is not None else ""
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self._header[section]
        return None

    def update_data(self, new_data: List[List[Any]], new_header: List[str]) -> None:
        self.beginResetModel()
        self._data = new_data
        self._header = new_header
        self.endResetModel()


class ExecuteSqlDialog(QDialog):
    """执行 ogr SQL 并展示结果。"""

    def __init__(self, layer: QgsVectorLayer, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("执行SQL")
        self.layer = layer
        self._table_model: Optional[VectorLayerTableModel] = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        sql_frame = QFrame()
        sql_layout = QVBoxLayout(sql_frame)
        self.sql_input = QTextEdit()
        self.sql_input.setText(f"select * from {self.layer.name().split('.')[0]} limit 1000")
        self.sql_input.setMinimumHeight(40)
        self.sql_input.setMaximumHeight(150)
        sql_layout.addWidget(self.sql_input)
        layout.addWidget(sql_frame)

        ctrl = QHBoxLayout()
        self.execute_btn = QPushButton("执行查询")
        self.stop_btn = QPushButton("停止")
        self.status_label = QLabel("")
        ctrl.addWidget(self.execute_btn)
        ctrl.addWidget(self.stop_btn)
        ctrl.addWidget(self.status_label)
        ctrl.addStretch()
        layout.addLayout(ctrl)

        self.result_table = QTableView()
        self.result_table.setAlternatingRowColors(True)
        self.result_table.setEditTriggers(QTableView.NoEditTriggers)
        self.result_table.setSelectionBehavior(QTableView.SelectRows)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.result_table)

        load_group = QGroupBox("加载为图层")
        load_layout = QVBoxLayout(load_group)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignLeft)
        self.subset_filter_input = QLineEdit()
        self.subset_filter_input.setPlaceholderText("Enter the optional SQL filter or click on the button to open the query builder tool")
        self.subset_filter_btn = QPushButton("...")
        self.subset_filter_btn.setFixedWidth(30)
        sub = QHBoxLayout()
        sub.addWidget(self.subset_filter_input)
        sub.addWidget(self.subset_filter_btn)
        form.addRow("Subset filter", sub)
        self.layer_name_input = QLineEdit()
        form.addRow("Layer name", self.layer_name_input)
        load_layout.addLayout(form)
        load_btn = QPushButton("加载图层")
        load_btn.clicked.connect(self._on_load_layer)
        btn_h = QHBoxLayout()
        btn_h.addStretch()
        btn_h.addWidget(load_btn)
        load_layout.addLayout(btn_h)
        layout.addWidget(load_group)

        self.execute_btn.clicked.connect(self._on_execute)
        self.stop_btn.clicked.connect(self._on_stop)
        self.subset_filter_btn.clicked.connect(self._on_open_query_builder)

    def _on_execute(self) -> None:
        try:
            sql = self.sql_input.toPlainText().strip()
            path = self.layer.path_c
            ds = ogr.Open(path, 0)
            t0 = time.time()
            v_layer = ds.ExecuteSQL(sql)
            t1 = time.time()
            self.status_label.setText("执行成功" if v_layer else "执行失败")
            if v_layer is None:
                return
            defn = v_layer.GetLayerDefn()
            columns = []
            for i in range(defn.GetFieldCount()):
                f = defn.GetFieldDefn(i)
                if f.GetName().upper() in ("SHAPE", "GEOMETRY", "WKB_GEOMETRY", "WKT_GEOMETRY"):
                    continue
                columns.append(f.GetName())
            data = []
            v_layer.ResetReading()
            for feat in v_layer:
                row = [feat.GetFieldAsString(c) if feat.GetFieldAsString(c) is not None else "" for c in columns]
                data.append(row)
            if self._table_model is None:
                self._table_model = VectorLayerTableModel(data, columns, self)
                self.result_table.setModel(self._table_model)
            else:
                self._table_model.update_data(data, columns)
        except Exception as e:  # noqa: BLE001
            logger.error(e)

    def _on_stop(self) -> None:
        pass

    def _on_load_layer(self) -> None:
        pass

    def _on_open_query_builder(self) -> None:
        from app.ui.dialogs.query_builder import QueryBuilderDialog
        dlg = QueryBuilderDialog(self.layer, parent=self)
        dlg.exec_()

    def sizeHint(self):  # noqa: D401
        return self.minimumSizeHint()
