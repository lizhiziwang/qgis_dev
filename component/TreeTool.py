from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QVBoxLayout, QTreeWidget, QTreeWidgetItem, QWidget, QHBoxLayout, QPushButton, \
    QGroupBox, QLabel, QSpinBox, QLineEdit, QScrollArea, QCheckBox, QListWidget, QListWidgetItem, QComboBox, \
    QProgressBar, QSizePolicy
from qgis._core import Qgis, QgsProject, QgsTask, QgsApplication
from qgis.core import QgsProcessingFeedback
from .file import *
from .check_dia import BaseDialog


import processing

class CreateTileTask(QgsTask):
    progress_updated = pyqtSignal(int)

    def __init__(self, description, param):
        super().__init__(description, QgsTask.CanCancel)
        self.param = param
        self.exception = None
        self.feedback = QgsProcessingFeedback()
    def fun(self, value):
        self.progress_updated.emit(int(value))

    def run(self):
        try:
            # 在后台线程中运行处理算法
            # feedback = QgsProcessingFeedback()
            # 注意：不能在后台线程中调用 runAndLoadResults（它会尝试加载图层到主界面）
            # 所以我们改用 processing.run()，然后在 finished() 中手动加载结果
            print("开始执行")
            self.feedback.progressChanged.connect(self.fun)
            result = processing.run("native:writevectortiles_xyz", self.param, feedback=self.feedback)
            self.result_output = result
            print(f"结果:{result}")
            return True
        except Exception as e:
            self.exception = e
            return False


    def finished(self, result):
        # if result:
        #     # 成功完成，在主线程中加载结果（例如添加图层）
        #     output_path = self.result_output['OUTPUT']
        #     # 假设输出是 MBTiles 文件或目录，根据实际情况加载
        #     # 示例：如果是矢量切片，可能需要使用 QgsVectorTileLayer
        #     from qgis.core import QgsVectorTileLayer, QgsProject
        #     layer = QgsVectorTileLayer(output_path, "Vector Tiles")
        #     if layer.isValid():
        #         QgsProject.instance().addMapLayer(layer)
        #     else:
        #         print("Failed to load vector tile layer")
        # else:
        #     if self.exception:
        #         print(f"Task failed: {self.exception}")
        #     else:
        #         print("Task was cancelled or failed without exception")
        pass


class PGISToolsDialog(BaseDialog):
    """
    从矢量图层创建矢量瓦片数据
    """
    def __init__(self, parent=None, title="", dia_type=None):
        super().__init__(parent, title=title)
        self.parent = parent
        self.dia_type = dia_type
        if self.dia_type is None:
            QgsApplication.instance().property("messageBar").pushMessage("功能待开发", level=Qgis.Warning, duration=3)
            self.close()
            return

        self.project = QgsProject.instance()
        self.select_layers = []
        self.out_extent = None
        self.task = None

        self.__init_ui__()




    def __init_ui__(self):
        layout = QVBoxLayout()
        # if self.dia_type == "native:writevectortiles_xyz":
        parameters_panel = QWidget()
        self.init_parameters_panel(parameters_panel)

        # 2. 右侧说明面板
        desc_group = QGroupBox("矢量瓦片(XYZ)生成")
        desc_label = QLabel(
            "该算法导出一个或多个"
            "矢量图层到矢量瓦片"
            "为快速地图渲染和"
            "小数据量而优化的数据格式"
        )
        print(desc_label.text())
        desc_label.setWordWrap(True)
        desc_layout = QVBoxLayout(desc_group)
        desc_layout.addWidget(desc_label)
        desc_layout.addStretch()

        # 3. 底部按钮栏
        b_l = QVBoxLayout()
        b_l.setContentsMargins(0, 0, 0, 0)
        self.loading_ = QProgressBar()
        self.loading_.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.loading_.setRange(0, 100)
        b_l.addWidget(self.loading_, stretch=2)
        bottom_layout = QHBoxLayout()
        bottom_layout.addWidget(QPushButton("Advanced"))
        bottom_layout.addWidget(QPushButton("Run as Batch Process..."))
        bottom_layout.addStretch()
        # bottom_layout.addWidget(QPushButton("0%"))  # 进度显示
        bottom_layout.addWidget(QPushButton("取消"))
        execute_but = QPushButton("执行")
        execute_but.clicked.connect(self.execute_create_tile)

        bottom_layout.addWidget(execute_but)
        c_b = QPushButton("关闭")
        c_b.clicked.connect(self.close)
        bottom_layout.addWidget(c_b)
        # bottom_layout.addWidget(QPushButton("帮助"))
        b_l.addLayout(bottom_layout)

        # 4. 主布局（左侧参数面板+右侧说明）
        main_layout = QHBoxLayout()
        main_layout.addWidget(parameters_panel, stretch=3)
        main_layout.addWidget(desc_group, stretch=1)

        # 5. 整体布局（主布局+底部按钮）
        overall_layout = QVBoxLayout(self)
        overall_layout.addLayout(main_layout)
        overall_layout.addLayout(b_l)
        self.setLayout(overall_layout)

    def execute_create_tile(self):
        try:
            self.on_progress_changed(0)
            param = self.build_param()
            # feedback = QgsProcessingFeedback()
            # feedback.progressChanged.connect(self.on_progress_changed)
            self.task = CreateTileTask("", param)

            task_manager = QgsApplication.taskManager()
            self.task.progress_updated.connect(self.on_progress_changed)
            print("Current task count before:", task_manager.count())

            task_manager.addTask(self.task)
            print("Current task count after:", task_manager.count())


            # processing.runAndLoadResults("native:writevectortiles_xyz", param, feedback=feedback)
        except Exception as e:
            print(e)
    def on_progress_changed(self, progress):
        try:
            print(f"当前进度：{progress} %")
            self.loading_.setValue(int(progress))
        except Exception as e:
            print(e)



    def init_parameters_panel(self, parent):
        """初始化参数面板（拆分参数项内部/外部间距，不受全局spacing影响）"""
        layout = QVBoxLayout(parent)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)

        # 关键1：取消scroll_layout的全局统一间距（注释或删除该行，不再影响参数项之间的间隙）
        # scroll_layout.setSpacing(20)  # 禁用全局统一间距，改为手动控制参数项之间的间隙

        # ==================== 1. File template（自主控制内部间距） ====================
        file_item_layout = QVBoxLayout()
        # 关键2：参数项内部间距（标签与控件之间），仅作用于当前参数项，与外部无关
        file_item_layout.setSpacing(8)  # 标签和输入框之间的间距为8px（自主控制）
        # 标签独占一行
        file_label = QLabel("文件模版")
        file_item_layout.addWidget(file_label)
        # 控件水平排列
        file_ctrl_layout = QHBoxLayout()
        self.xyz_temp = QLineEdit("{z}/{x}/{y}.pbf")
        file_ctrl_layout.addWidget(self.xyz_temp)
        file_item_layout.addLayout(file_ctrl_layout)
        # 将参数项添加到滚动布局
        scroll_layout.addLayout(file_item_layout)

        # 关键3：手动添加参数项之间的间隙（方式1：addSpacing()，简单高效，推荐）
        scroll_layout.addSpacing(15)  # 该间距仅作用于「File template」和「Input layers」之间

        # ==================== 2. Input layers（自主控制内部间距） ====================
        input_item_layout = QVBoxLayout()
        input_item_layout.setSpacing(8)  # 内部间距独立设置，不受其他参数项影响
        input_label = QLabel("输入图层")
        input_item_layout.addWidget(input_label)
        input_ctrl_layout = QHBoxLayout()

        # input_edit = QLineEdit()
        # input_edit.setPlaceholderText(f"已选择{0}个图层")
        # input_edit.setReadOnly(True)

        # layer_select = QCheckBox()
        # layer_select.setChecked(False)
        # layer_select.setToolTip("选择生成矢量瓦片的图层")

        self.list_widget = QListWidget()
        self.list_widget.setFixedHeight(130)  # 固定高度，避免占用过多空间
        layers = self.project.mapLayers().values()
        batch_options = [item.name() for item in layers]
        self.list_widget.itemClicked.connect(self.modify_select_layers)


        # 批量创建可勾选列表项
        for option_text in batch_options:
            # 创建列表项
            item = QListWidgetItem(option_text, self.list_widget)
            # 开启列表项的勾选功能
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            # 设置默认勾选状态（未选中）
            item.setCheckState(Qt.Unchecked)

        input_ctrl_layout.addWidget(self.list_widget)
        # input_ctrl_layout.addWidget(input_edit)
        # input_ctrl_layout.addWidget(QPushButton("..."))
        input_item_layout.addLayout(input_ctrl_layout)
        scroll_layout.addLayout(input_item_layout)

        # 手动添加参数项之间的间隙（保持20px，与上一个参数项间距一致）
        scroll_layout.addSpacing(15)

        # ==================== 3. Minimum zoom level（自主控制内部间距） ====================
        min_zoom_item_layout = QVBoxLayout()
        min_zoom_item_layout.setSpacing(8)  # 内部间距统一为8px，也可自定义不同值
        min_zoom_label = QLabel("最小等级")
        min_zoom_item_layout.addWidget(min_zoom_label)
        min_zoom_ctrl_layout = QHBoxLayout()
        self.min_zoom_spin = QSpinBox()
        self.min_zoom_spin.setRange(0, 20)
        self.min_zoom_spin.setValue(0)
        min_zoom_ctrl_layout.addWidget(self.min_zoom_spin)
        min_zoom_item_layout.addLayout(min_zoom_ctrl_layout)
        scroll_layout.addLayout(min_zoom_item_layout)

        # 手动添加参数项之间的间隙
        scroll_layout.addSpacing(15)

        # ==================== 4. Maximum zoom level（自主控制内部间距） ====================
        max_zoom_item_layout = QVBoxLayout()
        max_zoom_item_layout.setSpacing(8)
        max_zoom_label = QLabel("最大等级")
        max_zoom_item_layout.addWidget(max_zoom_label)
        max_zoom_ctrl_layout = QHBoxLayout()
        self.max_zoom_spin = QSpinBox()
        self.max_zoom_spin.setRange(0, 20)
        self.max_zoom_spin.setValue(3)
        max_zoom_ctrl_layout.addWidget(self.max_zoom_spin)
        max_zoom_item_layout.addLayout(max_zoom_ctrl_layout)
        scroll_layout.addLayout(max_zoom_item_layout)

        # 手动添加参数项之间的间隙
        scroll_layout.addSpacing(15)

        # ==================== 5. Extent [optional]（自主控制内部间距） ====================
        extent_item_layout = QVBoxLayout()
        extent_item_layout.setSpacing(8)
        extent_label = QLabel("输出范围")
        extent_item_layout.addWidget(extent_label)
        extent_ctrl_layout = QHBoxLayout()
        self.extent_text = QLineEdit()
        self.extent_text.setReadOnly(True)
        extent_ctrl_layout.addWidget(self.extent_text, stretch=8)
        self.extent_edit = QComboBox()
        datas = ["图层范围", "当前画布范围"]


        self.extent_edit.addItems(datas)
        self.extent_edit.setCurrentIndex(-1)

        self.extent_edit.setPlaceholderText("未设置")

        self.extent_edit.currentTextChanged.connect(self.extent_changed)

        # extent_edit.setReadOnly(True)
        extent_ctrl_layout.addWidget(self.extent_edit, stretch=1)
        # extent_ctrl_layout.addWidget(QPushButton("⌂"))
        # extent_ctrl_layout.addWidget(QPushButton("↔"))

        extent_item_layout.addLayout(extent_ctrl_layout)
        scroll_layout.addLayout(extent_item_layout)

        # 手动添加参数项之间的间隙
        scroll_layout.addSpacing(15)

        # ==================== 6. Output directory（自主控制内部间距） ====================
        output_item_layout = QVBoxLayout()
        output_item_layout.setSpacing(8)
        output_label = QLabel("输出目录")
        output_item_layout.addWidget(output_label)
        output_ctrl_layout = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("保存为临时目录")
        self.output_edit.setReadOnly(True)
        output_ctrl_layout.addWidget(self.output_edit)
        f_c = QPushButton("选择")
        f_c.clicked.connect(lambda : self.output_edit.setText(open_file_dir(self, is_dir=True)))
        output_ctrl_layout.addWidget(f_c)

        # output_ctrl_layout.addStretch()
        output_item_layout.addLayout(output_ctrl_layout)
        scroll_layout.addLayout(output_item_layout)

        # 底部伸缩空间（参数项顶部对齐）
        scroll_layout.addStretch()

        # 绑定滚动区域内容
        scroll_area.setWidget(scroll_content)
        layout.addWidget(scroll_area)

    def extent_changed(self, value):
        try:

            if "图层范围" == value:
                for item in self.select_layers:
                    self.out_extent = item.extent() \
                        if self.out_extent is None else self.out_extent.combineExtentWith(item.extent())
            elif "当前画布范围" == value:
                self.out_extent = self.parent.canvas.extent()
            print(self.out_extent.toString())
            self.extent_text.setText(self.out_extent.toString())
        except Exception as e:
            print(e)
    def modify_select_layers(self, item):
        if item.checkState():
            self.select_layers.append(self.project.mapLayersByName(item.text())[0])
        elif item.checkState():
            self.select_layers.remove(self.project.mapLayersByName(item.text())[0])

    def build_param(self):
        return {
            "OUTPUT_DIRECTORY": self.output_edit.text(),
            "XYZ_TEMPLATE": self.xyz_temp.text(),
            "LAYERS": [{"layer":item.source()} for item in self.select_layers],
            "MIN_ZOOM": self.min_zoom_spin.value(),
            "MAX_ZOOM": self.max_zoom_spin.value(),
            "EXTENT": f"{self.out_extent.xMinimum()},{self.out_extent.xMaximum()},{self.out_extent.yMinimum()},{self.out_extent.yMaximum()} [{self.select_layers[0].crs().authid()}]",
        }



class PGISTools(QWidget):
    def __init__(self, parent=None, topParent=None, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.parent = parent
        self.topParent = topParent

        self.__init_ui__()
    def __init_ui__(self):
        layout = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setStyleSheet("""
            QTreeWidget::item {
                padding: 3px;           /* 上下左右内边距 */
            }
        """)

        root = QTreeWidgetItem(self.tree)
        root.setText(0, "矢量常规")
        # 可选：root.setIcon(0, QIcon("computer.png"))
        # root.setFlags(root.flags() | Qt.ItemIsTristate)
        # root.setCheckState(0, Qt.Unchecked)

        item1 = QTreeWidgetItem(root)
        item1.setText(0, "生成矢量瓦片（XYZ）")
        item1._code_ = "native:writevectortiles_xyz"
        # 可选：root.setIcon(0, QIcon("computer.png"))
        # item1.setFlags(root.flags() | Qt.ItemIsTristate)
        # item1.setCheckState(0, Qt.Unchecked)

        item2 = QTreeWidgetItem(root)
        item2.setText(0, "最短路径搜索")
        item2._code_ = "native:writevectortiles_xyz"


        root2 = QTreeWidgetItem(self.tree)
        root2.setText(0, "测试2")

        # 可选：root.setIcon(0, QIcon("computer.png"))
        # root2.setFlags(root.flags() | Qt.ItemIsTristate)
        # root2.setCheckState(0, Qt.Unchecked) processing.run("native:writevectortiles_xyz", {'OUTPUT_DIRECTORY':'D:\\zsh\\temp\\xyz3','XYZ_TEMPLATE':'{z}/{x}/{y}.pbf','LAYERS':[{ 'layer': 'D:/Temp/2469ef2b-3423-4a22-8bce-dc808a144ace.json|layername=lcstzhjc_2023' }],'MIN_ZOOM':8,'MAX_ZOOM':13,'EXTENT':'39277867.069399998,39304429.969099998,2899285.205500000,2914619.243800000 [EPSG:4527]'})
        # self.tree.itemClicked.connect(lambda : processing.run(processing.run("native:writevectortiles_xyz", {'OUTPUT_DIRECTORY':'D:\\zsh\\temp\\xyz','XYZ_TEMPLATE':'{z}/{x}/{y}.pbf','LAYERS':[{ 'layer': 'D:/Temp/林草湿测试数据.gdb|layername=lcstzhjc_2023' }],'MIN_ZOOM':5,'MAX_ZOOM':12,'EXTENT':'39252010.440300003,39309491.049900003,2894446.860400000,2960439.598900000 [EPSG:4527]'})))
        self.tree.itemClicked.connect(self.item_clicked)
        layout.addWidget(self.tree)

    def item_clicked(self, item, column):
        try:
            text = item.text(column)
            # data = item.data((0, 0),Qt.UserRole)
            if item._code_ == "native:writevectortiles_xyz":

                PGISToolsDialog(parent=self.topParent, title="创建矢量瓦片", dia_type="native:writevectortiles_xyz").show()
            # dia = processing.createAlgorithmDialog("native:writevectortiles_xyz")
            # dia.show()

            print(f"点击了: {text}")
            print(f"数据: {item._code_}")
        except Exception as e:
            print(e)

    def on_progress_changed(self, progress: float):
        print(f"进度: {progress:.1f}%")



