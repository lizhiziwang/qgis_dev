import time
import uuid
from qgis._core import QgsVectorLayer
from typing import List, Any

from osgeo import ogr

from component.check_dia import BaseDialog
from .Logger import setup_logger
logger = setup_logger(__name__)


from PyQt5.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLineEdit, QPushButton, QLabel,
    QFrame, QTextEdit, QTableView
)
from PyQt5.QtCore import Qt, QAbstractTableModel, QModelIndex
from PyQt5.QtGui import QFont


class ExecuteSQlDialog(BaseDialog):
    def __init__(self, query_layer: QgsVectorLayer, parent, title=None):
        super().__init__(parent, title or "执行SQL")
        self.layer = query_layer
        self.load_layer_btn = None
        self.layer_name_input = None
        self.subset_filter_btn = None
        self.result_table = None
        self.subset_filter_input = None
        self.status_label = None
        self.stop_btn = None
        self.execute_btn = None
        self.sql_input = None
        # self.mem_layer = self.layer.materialize(QgsFeatureRequest())
        # self.mem_layer.setName("temp_mem_layer_for_sql")

        self.parent = parent
        self.setup_ui()

    def setup_ui(self):
        # 主布局
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(8)

        # ========== SQL 输入与控制区 ==========
        # SQL 输入框容器（带边框）
        sql_frame = QFrame(self.parent)
        sql_frame.setFrameShape(QFrame.StyledPanel)
        sql_frame.setFrameShadow(QFrame.Sunken)
        sql_layout = QVBoxLayout(sql_frame)
        sql_layout.setContentsMargins(5, 5, 5, 5)

        self.sql_input = QTextEdit()
        self.sql_input.setText(f"""select * from {self.layer.name().split('.')[0]} limit 1000""")
        self.sql_input.setFont(QFont("Courier New", 10))
        self.sql_input.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.sql_input.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.sql_input.setMinimumHeight(40)  # 最小高度（约2行）
        self.sql_input.setMaximumHeight(150)
        sql_layout.addWidget(self.sql_input)

        # 控制按钮区
        control_layout = QHBoxLayout()
        self.execute_btn = QPushButton("执行查询")
        self.stop_btn = QPushButton("停止")
        self.status_label = QLabel("")

        control_layout.addWidget(self.execute_btn)
        control_layout.addWidget(self.stop_btn)
        control_layout.addWidget(self.status_label)
        control_layout.addStretch()

        # ========== 结果表格 ==========
        self.result_table = QTableView()
        # self.result_table.setColumnCount(7)
        # self.result_table.setHorizontalHeaderLabels([
        #     "fid", "EDGE_ID", "START_NODE", "END_NODE", "OBJECTID", "PRJID", "SUBTYPE"
        # ])
        self.result_table.setAlternatingRowColors(True)
        self.result_table.setEditTriggers(self.result_table.NoEditTriggers)
        self.result_table.setSelectionBehavior(self.result_table.SelectRows)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        # 可选：设置行高或字体
        # self.result_table.setFont(QFont("Courier New", 9))

        # ========== Load as new layer 区域 ==========
        load_group = QGroupBox("加载为图层")
        load_layout = QVBoxLayout(load_group)
        load_layout.setContentsMargins(8, 8, 8, 8)

        form_layout = QFormLayout()
        form_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form_layout.setLabelAlignment(Qt.AlignLeft)

        # Subset filter
        self.subset_filter_input = QLineEdit()
        self.subset_filter_input.setPlaceholderText("Enter the optional SQL filter or click on the button to open the query builder tool")
        self.subset_filter_btn = QPushButton("...")
        self.subset_filter_btn.setFixedWidth(30)

        # 将按钮与输入框放在同一行（模拟原图布局）
        subset_hbox = QHBoxLayout()
        subset_hbox.addWidget(self.subset_filter_input)
        subset_hbox.addWidget(self.subset_filter_btn)
        form_layout.addRow("Subset filter", subset_hbox)

        # Layer name
        self.layer_name_input = QLineEdit()
        form_layout.addRow("Layer name", self.layer_name_input)

        # Load Layer 按钮（右对齐）
        btn_hbox = QHBoxLayout()
        btn_hbox.addStretch()
        self.load_layer_btn = QPushButton("加载图层")
        btn_hbox.addWidget(self.load_layer_btn)

        load_layout.addLayout(form_layout)
        load_layout.addLayout(btn_hbox)

        # ========== 添加到主布局 ==========
        main_layout.addWidget(sql_frame)
        main_layout.addLayout(control_layout)
        main_layout.addWidget(self.result_table)
        main_layout.addWidget(load_group)

        # ========== 连接信号槽 ==========
        self.execute_btn.clicked.connect(self.on_execute)
        self.stop_btn.clicked.connect(self.on_stop)
        self.load_layer_btn.clicked.connect(self.on_load_layer)
        self.subset_filter_btn.clicked.connect(self.on_open_query_builder)

        # 可选：初始化表格数据（示例）
        # self._populate_sample_data()

    def _populate_sample_data(self, data: List[List[Any]], columns):
        """使用 QTableView 高效显示数据"""
        if not data:
            header = [] if not data else [f"Field_{i}" for i in range(len(data[0]))]
            data = []
        # else:
        #     # 假设你已从 QgsVectorLayer 获取 field_names
        #     # 如果没有，可传入 header 作为参数
        #     header = getattr(self, '_current_field_names', [f"Col{i}" for i in range(len(data[0]))])

        # 首次创建 model，或更新现有 model
        if not hasattr(self, '_table_model') or self._table_model is None:
            self._table_model = VectorLayerTableModel(data, columns)
            self.result_table.setModel(self._table_model)
            # 可选：自动调整列宽（谨慎用于大数据）
            # self.result_table.resizeColumnsToContents()
        else:
            self._table_model.update_data(data, columns)

    # 执行sql
    def on_execute(self):
        """ 使用gdal的ogr模块执行 SQL 查询"""
        try:
            sql = self.sql_input.toPlainText().strip()
            path = self.layer.path_c
            ds = ogr.Open(path, 0)
            start = time.time()
            v_layer = ds.ExecuteSQL(sql)
            end = time.time()
            self.status_label.setText("执行成功" if v_layer else "执行失败")
            print(f"执行sql的时间{end - start}")
            # SQL执行失败退出
            if v_layer is None:
                return -1

            defn = v_layer.GetLayerDefn()
            field_count = defn.GetFieldCount()
            # 字段信息
            columns = []
            for i in range(field_count):
                f = defn.GetFieldDefn(i)
                if f.GetName().upper() in ('SHAPE', 'GEOMETRY', 'WKB_GEOMETRY', 'WKT_GEOMETRY'):
                    continue
                columns.append(f.GetName())
            # 数据
            data = []
            v_layer.ResetReading()
            for feat in v_layer:
                row = []
                for i in columns:
                    # fdefn = defn.GetFieldDefn(i)
                    # fname = fdefn.GetName()
                    if i.upper() in ('SHAPE', 'GEOMETRY', 'WKB_GEOMETRY', 'WKT_GEOMETRY'):
                        continue
                    val = feat.GetFieldAsString(i)
                    # 处理 None / NULL
                    row.append(val if val is not None else '')
                data.append(row)
            self._populate_sample_data(data, columns)
        except Exception as e:
            logger.error(e)

        """
            转换到qgis的图层数据类型
        """
        # v_l = self.save_data(v_layer)
        #
        # if v_l.isValid():
        #     columns = v_l.fields()
        #     field_names = []
        #     for field in columns:
        #         field_names.append(field.name())
        #     # self.result_table.setColumnCount(len(field_names))
        #     # self.result_table.setHorizontalHeaderLabels(field_names)
        #     data = []
        #     for feature in v_l.getFeatures():
        #         data.append([feature.attribute(name) for name in field_names])
        #     self._populate_sample_data(data, field_names)

    # 持久化临时文件
    def save_data(self, ogr_layer):
        temp_gpkg = "d:/temp/"+uuid.uuid4().hex+".gpkg"
        # 创建输出数据源
        drv = ogr.GetDriverByName('GPKG')
        out_ds = drv.CreateDataSource(temp_gpkg)
        out_layer = out_ds.CreateLayer(ogr_layer.GetName(), geom_type=ogr_layer.GetGeomType())

        # 复制字段定义
        layer_defn = ogr_layer.GetLayerDefn()
        for i in range(layer_defn.GetFieldCount()):
            field_defn = layer_defn.GetFieldDefn(i)
            field_name = field_defn.GetName()
            # 跳过常见的几何字段名（尤其来自 FileGDB/Shapefile）
            if field_name.upper() in ('SHAPE', 'GEOMETRY', 'WKB_GEOMETRY', 'WKT_GEOMETRY'):
                continue
            out_layer.CreateField(field_defn)

        # 复制要素
        ogr_layer.ResetReading()
        for feat in ogr_layer:
            # 创建新要素（自动处理几何）
            new_feat = ogr.Feature(out_layer.GetLayerDefn())
            # 复制属性（跳过被过滤的字段）
            for i in range(layer_defn.GetFieldCount()):
                field_defn = layer_defn.GetFieldDefn(i)
                field_name = field_defn.GetName()
                if field_name.upper() in ('SHAPE', 'GEOMETRY', 'WKB_GEOMETRY', 'WKT_GEOMETRY'):
                    continue
                new_feat.SetField(field_name, feat.GetField(i))
            # 复制几何
            geom = feat.GetGeometryRef()
            if geom:
                new_feat.SetGeometry(geom.Clone())
            out_layer.CreateFeature(new_feat)
            new_feat = None

        # 清理
        out_ds = None  # 关闭并写入
        return QgsVectorLayer(f"{temp_gpkg}|layername={ogr_layer.GetName()}", ogr_layer.GetName(), "ogr")


    def on_stop(self):
        """停止执行"""
        print("[Stop] Stopping query execution...")
        # TODO: 实现停止逻辑

    def on_load_layer(self):
        """加载为新图层"""
        layer_name = self.layer_name_input.text().strip()
        subset_filter = self.subset_filter_input.text().strip()
        print(f"[Load Layer] Name: '{layer_name}', Filter: '{subset_filter}'")
        # TODO: 实现加载逻辑

    def on_open_query_builder(self):
        """打开查询构建器工具"""
        print("[Query Builder] Opening...")
        # TODO: 打开查询构建器对话框

    # 可选：调整对话框大小
    def sizeHint(self):
        return self.minimumSizeHint()

    # def minimumSizeHint(self):
    #     return super().minimumSizeHint().expandedTo(
    #         type(self).minimumSizeHint(self)
    #     )


class VectorLayerTableModel(QAbstractTableModel):
    def __init__(self, data: List[List[Any]], header: List[str], parent=None):
        super().__init__(parent)
        self._data = data          # 二维列表：[[row1], [row2], ...]
        self._header = header      # 字段名列表

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._data)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self._header) if self._data else 0

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        if role == Qt.DisplayRole:
            value = self._data[index.row()][index.column()]
            return str(value) if value is not None else ''
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> Any:
        if role == Qt.DisplayRole:
            if orientation == Qt.Horizontal:
                return self._header[section]
            else:
                return str(section + 1)  # 行号从 1 开始
        return None

    def update_data(self, new_data: List[List[Any]], new_header: List[str]):
        """刷新整个表格数据"""
        self.beginResetModel()
        self._data = new_data
        self._header = new_header
        self.endResetModel()