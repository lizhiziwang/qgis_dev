"""
    空间查询工具
"""
from PyQt5.QtCore import QSize, Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QComboBox, QPushButton, QVBoxLayout, QLabel, QTextEdit, \
    QDialogButtonBox, QToolBar, QToolButton, QMenu, QAction, QLineEdit
from qgis._core import QgsVectorLayer, Qgis

from .check_dia import BaseDialog

from .check.SpaceDataCheck import search_intersect_features_geom
from .draw_tool import PointDrawTool, PolygonDrawTool, LineDrawTool, RectangleDrawTool, CircleDrawTool

from .Logger import setup_logger

logger = setup_logger(__name__)


# 封装当前qgis环境下 矢量图层选择器

class SpaceQueryTool:
    def __init__(self, parent):
        self.parent = parent
        self.toolbar = QToolBar("编辑工具")

        self.p_button = QToolButton()
        self.p_button.setText("点查询")
        self.p_button.setPopupMode(QToolButton.InstantPopup)

        # 创建下拉菜单
        self.p_menu = QMenu()

        # 添加子操作（带图标）
        self.p_query = QAction("点击查询", self.p_menu)
        self.p_b_query = QAction("点缓冲查询", self.p_menu)
        self.p_query.triggered.connect(lambda : SpaceQuery(self.parent, q_type="point").show())
        self.p_b_query.triggered.connect(lambda : SpaceQuery(self.parent, q_type="point", is_buffer=True).show())

        self.p_menu.addAction(self.p_query)
        self.p_menu.addAction(self.p_b_query)
        # self.p_query.triggered.connect(lambda: pass)

        self.l_but  = QToolButton()
        self.l_but.setText("线查询")
        self.l_but.setPopupMode(QToolButton.InstantPopup)
        self.l_menu = QMenu()

        self.l_query = QAction("线查询", self.l_menu)
        self.l_online_query = QAction("沿线查询", self.l_menu)
        self.l_online_query.setStatusTip("针对线图层，沿线查询最短路径")
        self.l_b_query = QAction("线缓冲查询", self.l_menu)

        self.l_query.triggered.connect(lambda : SpaceQuery(self.parent, q_type="line", is_buffer=False).show())
        self.l_b_query.triggered.connect(lambda : SpaceQuery(self.parent, q_type="line", is_buffer=True).show())

        self.l_menu.addAction(self.l_query)
        self.l_menu.addAction(self.l_online_query)
        self.l_menu.addAction(self.l_b_query)


        self.p_but = QToolButton()
        self.p_but.setText("面查询")

        self.p_but.setPopupMode(QToolButton.InstantPopup)
        self.po_menu = QMenu()

        self.p_query2 = QAction("面查询", self.po_menu)
        self.p_b_query = QAction("面缓冲查询", self.po_menu)
        self.k_query = QAction("框选查询", self.po_menu)
        self.c_query = QAction("圆查询", self.po_menu)

        self.p_query2.triggered.connect(lambda : SpaceQuery(self.parent, q_type="polygon").show())
        self.k_query.triggered.connect(lambda : SpaceQuery(self.parent, q_type="rectangle").show())
        self.c_query.triggered.connect(lambda : SpaceQuery(self.parent, q_type="circle").show())

        self.p_b_query.triggered.connect(lambda : SpaceQuery(self.parent, q_type="polygon", is_buffer=True).show())


        self.po_menu.addAction(self.p_query2)
        self.po_menu.addAction(self.p_b_query)
        self.po_menu.addAction(self.k_query)
        self.po_menu.addAction(self.c_query)

        self.p_but.setMenu(self.po_menu)
        self.l_but.setMenu(self.l_menu)
        self.p_button.setMenu(self.p_menu)

        self.toolbar.addWidget(self.p_button)
        self.toolbar.addWidget(self.l_but)
        self.toolbar.addWidget(self.p_but)

        self.parent.addToolBar(self.toolbar)

    def build(self, q_type, is_buf:bool = False):
        pass







class VecLayerSelector(QWidget):
    def __init__(self, vec_layers, parent):
        super().__init__(parent)
        self.vec_layers = vec_layers
        self.data_source_input = None
        self.layer_names = None
        self.reflash_but = None
        self.label = None
        self.__init_ui__()

    def __init_ui__(self):
        main_layout = QVBoxLayout()
        # main_layout.setContentsMargins(20, 20, 20, 20)
        # main_layout.setSpacing(8)

        # === 第一行：Label 单独在上 ===
        self.label = QLabel("矢量数据源：")
        # self.label.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.label.setAlignment(Qt.AlignLeft)  # 左对齐（也可居中）
        main_layout.addWidget(self.label)

        # === 第二行：下拉框 + 刷新按钮（水平布局）===
        input_layout = QHBoxLayout()
        # input_layout.setSpacing(8)

        # 下拉框
        self.data_source_input = QComboBox(self)
        self.data_source_input.setMinimumWidth(180)

        self.data_source_input.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        input_layout.addWidget(self.data_source_input)

        # 刷新按钮
        self.reflash_but = QPushButton()
        self.reflash_but.setIcon(QIcon("resource/png/刷新.png"))
        self.reflash_but.setIconSize(QSize(16, 16))
        self.reflash_but.setFixedSize(26, 26)
        self.reflash_but.setToolTip("刷新数据源")
        self.reflash_but.setStyleSheet("""
                    QPushButton {
                        border: none;
                        background-color: transparent;
                        border-radius: 3px;
                    }
                    QPushButton:hover {
                        background-color: #e8e8e8;
                    }
                    QPushButton:pressed {
                        background-color: #dcdcdc;
                    }
                """)
        self.reflash_but.clicked.connect(self._reflash_data_source)
        input_layout.addWidget(self.reflash_but)

        # 将输入行加入主布局
        main_layout.addLayout(input_layout)

        self.setLayout(main_layout)

        # 初始化数据
        self._reflash_data_source()


    def _reflash_data_source(self):
        # 筛选矢量图层
        self.layer_names = [item.name() for item in self.vec_layers if isinstance(item, QgsVectorLayer)]
        self.data_source_input.addItems(self.layer_names)

query_type = {
    "point": "点查询",
    "line": "线查询",
    "polygon": "区域查询",
    "circle": "圆形查询",  # 以上四个支持任意类型 相交查询
    "along_the_line": "沿线查询", # 沿线查询
    "buffer":"缓冲区查询",
    "rectangle": "框线查询"
}

class SpaceQuery(BaseDialog):
    def __init__(self, parent, q_type: str = "point", is_buffer: bool = False ):
        super().__init__(parent, query_type[q_type])
        self.parent = parent
        self.layers = parent.project.mapLayers().values()

        self.layer_input = None
        self.q_type = q_type
        self.search_geom = None
        self.pt = None
        self.geom_txt = None
        self.is_buffer = is_buffer
        self.buff_value = None

        self.tool_classes = {
            "point": PointDrawTool,
            "line": LineDrawTool,
            "polygon": PolygonDrawTool,
            "circle": CircleDrawTool,
            "rectangle": RectangleDrawTool,
        }
        self.__init_ui__()

    def __init_ui__(self):
        main_layout = QVBoxLayout(self)

        try:
            self.layer_input = VecLayerSelector(self.layers, self)
            main_layout.addWidget(self.layer_input)
            self.setMinimumSize(400, 300)

            # if self.q_type == "point":
            var = QHBoxLayout()
            var1 = QLabel("操作")
            var2 = QPushButton("绘制")
            var2.clicked.connect(self.get_geom)

            self.geom_txt = QTextEdit()
            self.geom_txt.setPlaceholderText("绘制几何的位置")
            self.geom_txt.setReadOnly(True)

            var.addWidget(var1)
            var.addWidget(var2)

            if self.is_buffer:
                var3 = QLabel("缓冲距离：")
                self.buff_value = QLineEdit("0.0")
                var5 = QLabel("米(m)")
                var.addWidget(var3)
                var.addWidget(self.buff_value)
                var.addWidget(var5)
            var.addStretch()

            main_layout.addLayout(var)
            main_layout.addWidget(self.geom_txt)


        except Exception as e:
            logger.error(e)
        button_box = QDialogButtonBox()
        self.confirm_btn = button_box.addButton("确认", QDialogButtonBox.AcceptRole)
        self.cancel_btn = button_box.addButton("取消", QDialogButtonBox.RejectRole)

        button_box.accepted.connect(self.query)  # 点击“确认”触发 accept()
        button_box.rejected.connect(lambda : self.close())  # 点击“取消”触发 reject()
        main_layout.addWidget(button_box)

        self.setLayout(main_layout)

    def query(self):
        try:
            if not self.layer_input.data_source_input.currentText():
                self.parent.messageBar.pushMessage("请选择图层进行查询", level=Qgis.Warning, duration=3)
                return
            if self.search_geom is None:
                self.hide()
                msg_widget = self.parent.messageBar.createMessage("警告", "请绘制几何图形用于查询")

                item = self.parent.messageBar.pushWidget(msg_widget, Qgis.Warning, duration=3)
                item.destroyed.connect(lambda: self.show())
                return
                # if self.q_type == "point":
            layer = self.parent.project.mapLayersByName(self.layer_input.data_source_input.currentText())[0]
            fids = search_intersect_features_geom(layer, self.search_geom,
                                                   param={"crs": self.parent.canvas.mapSettings().destinationCrs()})
            self.parent.open_attribute_table(layer=layer,show_ids=fids)
            self.parent.current_attribute_dock.zoom_to_map("selectFeature", {"id":fids})
        except Exception as e:
            logger.error(e)
        finally:
            self.close()


    def get_geom(self):
        self.hide()
        try:
            self.pt = self.tool_classes[self.q_type](self.parent.canvas, None, is_buffer=self.is_buffer,
                                                     distance=float(self.buff_value.text()) if self.buff_value is not None else 0.0)
            # pt = PolygonDrawTool(self.parent.canvas, None)
            # self.pt = LineDrawTool(self.parent.canvas, None)
            self.parent.canvas.setMapTool(self.pt)
            self.pt.finish.connect(lambda g: self._finish_draw(g))
        except Exception as e:
            logger.error(e)
    def _finish_draw(self, geom):
        self.search_geom = geom
        self.geom_txt.setPlainText(geom.asWkt())
        self.show()

    # def close(self):
    #     if self.pt is not None:
    #         self.pt.clear()
    #     super().close()

    def close(self):
        # 关键：关闭对话框时，彻底销毁工具实例，释放底层资源
        if self.pt is not None:
            self.pt.clear()
            # 无论是否激活，都销毁工具实例，解除底层引用
            self.pt.deleteLater()  # 销毁C++底层资源
            self.pt = None  # 清空Python层引用
        super().close()

