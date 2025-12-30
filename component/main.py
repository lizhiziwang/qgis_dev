import os
import sys
# sys.path.append(r"D:\pf\miniconda3\envs\qgis_env\Library\python")

from PyQt5.QtWidgets import (
    QMainWindow, QDockWidget, QAction, QApplication, QFileDialog
)
from PyQt5.QtCore import Qt
from qgis.core import (
    QgsApplication, QgsVectorLayer, QgsProject,
    QgsLayerTreeModel, QgsLayerTreeLayer  # 直接使用图层树模型
)
from qgis.gui import (
    QgsMapCanvas, QgsLayerTreeMapCanvasBridge,
    QgsMapToolPan, QgsMapToolZoom, QgsLayerTreeView,Qgs3DSymbolDialog
)

# 设置QGIS路径
# if os.name == 'nt':
#     os.add_dll_directory(os.path.join(os.environ['CONDA_PREFIX'], 'Library', 'bin'))

# QgsApplication.setPrefixPath(os.environ['CONDA_PREFIX'], True)
app = QgsApplication([], True)
app.initQgis()


class QGISMapWindow(QMainWindow):
    def __init__(self, shapefile_path=None):
        super().__init__()
        self.setWindowTitle("GIS")
        self.setGeometry(100, 100, 1200, 800)

        # 地图画布
        self.canvas = QgsMapCanvas()
        self.canvas.setCanvasColor(Qt.white)
        self.canvas.enableAntiAliasing(True)
        self.setCentralWidget(self.canvas)

        # 图层树与桥接器（仅用于画布同步，不依赖其模型）
        self.layer_tree_root = QgsProject.instance().layerTreeRoot()
        self.bridge = QgsLayerTreeMapCanvasBridge(self.layer_tree_root, self.canvas)
        self.bridge.setAutoSetupOnFirstLayer(True)

        # 创建面板和工具
        self.create_layer_dock()
        self.create_toolbar()
        self.create_menu()

        # 加载SHP文件
        if shapefile_path and os.path.exists(shapefile_path):
            self.load_file(shapefile_path)

    def create_layer_dock(self):
        """完全独立创建图层树视图（适配QGIS 3.44+）"""
        dock = QDockWidget("图层列表", self)
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        # 1. 获取项目的图层树模型（直接从QgsProject获取，不依赖桥接器）
        print(self.layer_tree_root)
        self.layer_model = QgsLayerTreeModel(self.layer_tree_root)
        # 启用图层树的交互功能
        self.layer_model.setFlags(
            QgsLayerTreeModel.AllowNodeRename |
            QgsLayerTreeModel.AllowNodeReorder |
            QgsLayerTreeModel.AllowNodeChangeVisibility |
            QgsLayerTreeModel.ShowLegend |
            QgsLayerTreeModel.ShowLegendAsTree
        )

        # 2. 创建图层树视图并绑定模型
        self.layer_tree_view = QgsLayerTreeView()
        self.layer_tree_view.setModel(self.layer_model)
        # 绑定双击事件
        self.layer_tree_view.doubleClicked.connect(self.handle_layer_double_click)
        # 不需要设置rootIndex，默认使用根节点

        # 3. 关键：将图层树视图的选择与画布联动（手动同步，替代桥接器模型）
        # self.layer_tree_view.selectionModel().selectionChanged.connect(
        #     self.sync_layer_selection
        # )

        dock.setWidget(self.layer_tree_view)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
    # 图例双击事件
    def handle_layer_double_click(self, index):
        """处理图层双击事件 - 使用安全的方法"""
        try:
            # 使用图层名称来查找图层，避免使用index2node
            layer_name = self.layer_model.data(index, Qt.DisplayRole)
            if not layer_name:
                return

            # 通过图层名称查找对应的图层
            project = QgsProject.instance()
            layers = project.mapLayersByName(layer_name)
            if not layers:
                print(f"未找到图层: {layer_name}")
                return

            layer = layers[0]
            if not layer or not layer.isValid():
                print(f"图层无效: {layer_name}")
                return

            # 这里你可以根据需要执行相应的操作
            # 例如：显示图层属性、缩放到图层等
            print(f"双击了图层: {layer_name}")

            # 如果你想缩放到该图层
            extent = layer.extent()
            if not extent.isEmpty():
                self.canvas.setExtent(extent)
                self.canvas.refresh()

        except Exception as e:
            print(f"处理双击事件错误: {str(e)}")
    def sync_layer_selection(self, selected, deselected):
        """彻底重构：不使用index2node，通过图层ID映射实现安全同步"""
        try:
            # 1. 获取选中项的文本（图层名）
            selection_model = self.layer_tree_view.selectionModel()
            if not selection_model or not selection_model.hasSelection():
                return

            # 获取第一个选中项的文本（通常是图层名）
            index = selection_model.selectedIndexes()[0]
            if not index.isValid():
                return

            layer_name = self.layer_model.data(index, Qt.DisplayRole)
            if not layer_name:
                return

            # 2. 通过图层名查找对应的图层（容错处理）
            project = QgsProject.instance()
            layers = project.mapLayersByName(layer_name)
            if not layers:
                print(f"未找到图层: {layer_name}")
                return

            # 3. 验证图层有效性并缩放
            target_layer = layers[0]
            if target_layer.isValid():
                extent = target_layer.extent()
                if not extent.isEmpty():
                    self.canvas.setExtent(extent)
                    self.canvas.refresh()
                else:
                    print(f"图层 {layer_name} 范围为空")
            else:
                print(f"图层 {layer_name} 无效")

        except Exception as e:
            print(f"图层选择同步错误: {str(e)}")

    def create_toolbar(self):
        """地图工具条（修复工具无法使用的问题）"""
        self.toolbar = self.addToolBar("地图工具")
        self.toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)

        # 1. 平移工具（保存实例引用）
        self.pan_tool = QgsMapToolPan(self.canvas)  # 保存为实例属性，避免被回收
        self.pan_action = QAction("平移", self)
        self.pan_action.triggered.connect(lambda: self.canvas.setMapTool(self.pan_tool))
        self.toolbar.addAction(self.pan_action)

        # 2. 放大工具（保存实例引用）
        self.zoom_in_tool = QgsMapToolZoom(self.canvas, False)  # False=放大
        self.zoom_in_action = QAction("放大", self)
        self.zoom_in_action.triggered.connect(lambda: self.canvas.setMapTool(self.zoom_in_tool))
        self.toolbar.addAction(self.zoom_in_action)

        # 3. 缩小工具（保存实例引用）
        self.zoom_out_tool = QgsMapToolZoom(self.canvas, True)  # True=缩小
        self.zoom_out_action = QAction("缩小", self)
        self.zoom_out_action.triggered.connect(lambda: self.canvas.setMapTool(self.zoom_out_tool))
        self.toolbar.addAction(self.zoom_out_action)

        # 4. 全图工具（确保画布范围正确计算）
        self.zoom_full_action = QAction("全图", self)
        self.zoom_full_action.triggered.connect(self.zoom_to_full_extent)
        self.toolbar.addAction(self.zoom_full_action)

        # 5. 添加"刷新"工具（解决图层显示延迟问题）
        self.refresh_action = QAction("刷新", self)
        self.refresh_action.triggered.connect(self.canvas.refresh)
        self.toolbar.addAction(self.refresh_action)

        # 默认激活平移工具
        self.canvas.setMapTool(self.pan_tool)

    # 打开文件弹窗
    def open_file_dialog(self):
        """打开文件对话框"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            '选择矢量文件',
            '',
            '矢量文件 (*.shp *.geojson *.gpkg *.gdb);;所有文件 (*)'
        )
        if file_path:
            self.load_file(file_path)
    def create_menu(self):
        menubar = self.menuBar()

        # 文件菜单
        file_menu = menubar.addMenu('文件(&F)')

        # 打开文件
        open_action = QAction('打开文件(&O)', self)
        open_action.setShortcut('Ctrl+O')
        open_action.setStatusTip('打开矢量文件')
        open_action.triggered.connect(self.open_file_dialog)
        file_menu.addAction(open_action)

        # 添加分隔符
        file_menu.addSeparator()

        # 退出
        exit_action = QAction('退出(&X)', self)
        exit_action.setShortcut('Ctrl+Q')
        exit_action.setStatusTip('退出应用')
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # # 视图菜单
        # view_menu = menubar.addMenu('视图(&V)')
        #
        # # 图层列表
        # layer_dock_action = QAction('图层列表(&L)', self)
        # layer_dock_action.setCheckable(True)
        # layer_dock_action.setChecked(True)
        # layer_dock_action.triggered.connect(self.toggle_layer_dock)
        # view_menu.addAction(layer_dock_action)
        #
        # # 工具栏
        # toolbar_action = QAction('工具栏(&T)', self)
        # toolbar_action.setCheckable(True)
        # toolbar_action.setChecked(True)
        # toolbar_action.triggered.connect(self.toggle_toolbar)
        # view_menu.addAction(toolbar_action)
        #
        # # 添加分隔符
        # view_menu.addSeparator()
        #
        # # 缩放到全图
        # zoom_full_action = QAction('缩放到全图(&F)', self)
        # zoom_full_action.setShortcut('Ctrl+Home')
        # zoom_full_action.triggered.connect(self.zoom_to_full_extent)
        # view_menu.addAction(zoom_full_action)
        #
        # # 刷新
        # refresh_action = QAction('刷新(&R)', self)
        # refresh_action.setShortcut('F5')
        # refresh_action.triggered.connect(self.canvas.refresh)
        # view_menu.addAction(refresh_action)
        #
        # # 图层菜单
        # layer_menu = menubar.addMenu('图层(&L)')
        #
        # # 添加图层
        # add_layer_action = QAction('添加图层(&A)', self)
        # add_layer_action.triggered.connect(self.open_file_dialog)
        # layer_menu.addAction(add_layer_action)
        #
        # # 清空图层
        # clear_layers_action = QAction('清空图层(&C)', self)
        # clear_layers_action.triggered.connect(self.clear_all_layers)
        # layer_menu.addAction(clear_layers_action)
        #
        # # 帮助菜单
        # help_menu = menubar.addMenu('帮助(&H)')
        #
        # # 关于
        # about_action = QAction('关于(&A)', self)
        # about_action.triggered.connect(self.show_about_dialog)
        # help_menu.addAction(about_action)

    def zoom_to_full_extent(self):
        """修复全图工具：正确计算所有图层的范围"""
        # 获取所有已加载的图层
        layers = QgsProject.instance().mapLayers().values()
        if not layers:
            return

        # 计算所有图层的合并范围
        combined_extent = None
        for layer in layers:
            if combined_extent is None:
                combined_extent = layer.extent()
            else:
                combined_extent.combineExtentWith(layer.extent())

        # 应用合并后的范围到画布
        if combined_extent:
            self.canvas.setExtent(combined_extent)
            self.canvas.refresh()

    # def zoom_to_full_extent(self):
    #     """缩放到全图范围"""
    #     self.canvas.setExtent(self.canvas.fullExtent())
    #     self.canvas.refresh()

    def load_file(self, shapefile_path):
        """加载SHP文件"""
        if not os.path.exists(shapefile_path):
            print(f"错误：文件不存在 - {shapefile_path}")
            return

        layer = QgsVectorLayer(shapefile_path, os.path.basename(shapefile_path), "ogr")
        if not layer.isValid():
            print(f"错误：图层无效 - {shapefile_path}")
            return

        QgsProject.instance().addMapLayer(layer)
        self.canvas.setExtent(layer.extent())
        self.canvas.refresh()


if __name__ == "__main__":
    # 替换为实际SHP文件路径
    shp_file_path = r"D:\work\zwps\林业项目\文件\867aee55-5456-4fb9-9b53-3deb51c4ffb1.gdb"  # 例如：r"D:\data\boundary.shp"

    window = QGISMapWindow(shp_file_path)
    window.show()
    sys.exit(app.exec_())
