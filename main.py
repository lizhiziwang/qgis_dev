import os
import sys

current_file = os.path.abspath(__file__)
current_dir = os.path.dirname(current_file)
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

from PyQt5.QtCore import QTranslator
from qgis._core import QgsCoordinateTransform, QgsCoordinateReferenceSystem
from qgis._gui import QgsMapLayerStyleManagerWidget, QgsBrowserGuiModel, \
    QgsBrowserTreeView, QgsDataItemGuiContext, QgsMessageBar, QgsVectorLayerProperties
from osgeo import ogr

from component.excute_sql import ExecuteSQlDialog
from component.map_tool import *

from PyQt5.QtWidgets import (
    QMainWindow, QDockWidget, QMessageBox, QFileDialog, QStyle,
)
from qgis.core import (
    QgsApplication, QgsProject, QgsStyle,
    QgsLayerTreeModel,
)
from qgis.gui import (
    QgsMapCanvas, QgsLayerTreeMapCanvasBridge, QgsScaleWidget, QgsProjectionSelectionWidget,
    QgsMapToolPan, QgsMapToolZoom, QgsLayerTreeView, QgsRendererPropertiesDialog
)
from component.analyse import AnalysePanel
from component.style import StyleSaveDia, StyleOpenDia
from component.draw_tool import Draw
from component.add_vec_data import *
from component.check_dia import JsonTreeView

''' 打包时需要使用以下4行 '''
# QGIS_PREFIX = os.path.dirname(os.path.abspath(__file__))  # 即 dist\app
# os.environ['QGIS_PREFIX_PATH'] = QGIS_PREFIX
# os.environ['PROJ_LIB'] = QGIS_PREFIX

app = QgsApplication([], True)
# app.setPrefixPath(QGIS_PREFIX, True)
logger.warning(f"srs路径{app.i18nPath()}")
app.initQgis()

translator = QTranslator()
# 加载 QGIS 中文翻译文件（路径因系统而异 ）
translator.load("qgis_zh-Hans.qm", app.i18nPath())
QgsApplication.installTranslator(translator)


class QGISMapWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.browser_widget = None
        self.edit_layer = None
        self.current_attribute_dock = None
        self.attribute_table_dialog = None
        self.browser_dock = None
        self.project = QgsProject.instance()
        self.browser = None
        self.setWindowTitle("default")
        self.setGeometry(100, 100, 1200, 800)
        self.setWindowIcon(QIcon("resource/png/app_icon.png"))
        # 地图画布初始化
        # 地图画布
        self.canvas = QgsMapCanvas()
        self.canvas.setAcceptDrops(True)
        self.canvas.setCanvasColor(Qt.white)
        self.canvas.enableAntiAliasing(True)
        self.canvas.setProject(self.project)

        crs = QgsCoordinateReferenceSystem("EPSG:4490")
        self.project.setCrs(crs)
        self.canvas.setDestinationCrs(crs)

        self.setCentralWidget(self.canvas)
        # 当前编辑图层初始化
        self.current_edit_layer = None

        # 图层树核心组件（QGIS 3.44+ 兼容）
        self.layer_tree_root = self.project.layerTreeRoot()
        self.bridge = QgsLayerTreeMapCanvasBridge(self.layer_tree_root, self.canvas)
        self.bridge.setAutoSetupOnFirstLayer(True)
        self.draw_tool = Draw(self)
        # 坐标系注册
        self.coord_register = app.coordinateReferenceSystemRegistry()

        # 创建UI组件
        self.create_layer_dock()
        self.create_browser()
        # 编辑组件
        self.create_toolbar()
        self.create_menu()

        self.statusBar().show()

        from qgis.gui import QgsScaleWidget, QgsProjectionSelectionWidget

        status_bar = self.statusBar()

        # 添加比例尺
        scale_widget = QgsScaleWidget(self.canvas)
        status_bar.addPermanentWidget(scale_widget)
        # # 添加投影选择器（EPSG）
        proj_selector = QgsProjectionSelectionWidget(self)
        proj_selector.setCrs(crs)
        proj_selector.setOptionVisible(option=QgsProjectionSelectionWidget().CrsOption.ProjectCrs, visible=False)
        status_bar.addPermanentWidget(proj_selector)

        self.messageBar = QgsMessageBar()
        # qgis_default_style = self.style()

        self.load_file(f"D:\work\共青城市_共青城市金湖乡公益性公墓_林地审核审批数据库_20251123\共青城市_共青城市金湖乡公益性公墓_林地审核审批数据库_20251123.gdb")

    ''' 创建图层对象 '''

    def create_layer_dock(self):
        """创建独立图层树面板（修复索引与节点匹配问题）"""
        dock = QDockWidget("图层列表", self)
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        # 初始化图层树模型（绑定项目根节点）
        self.layer_model = QgsLayerTreeModel(self.layer_tree_root)
        self.layer_model.setFlags(
            QgsLayerTreeModel.AllowNodeRename |
            QgsLayerTreeModel.AllowNodeReorder |
            QgsLayerTreeModel.AllowNodeChangeVisibility |
            QgsLayerTreeModel.ShowLegend |
            QgsLayerTreeModel.ShowLegendAsTree
        )

        # 图层树视图
        self.layer_tree_view = QgsLayerTreeView()
        self.layer_tree_view.setModel(self.layer_model)
        self.layer_tree_view.clicked.connect(self.handle_layer_click)
        self.layer_tree_view.setContextMenuPolicy(Qt.CustomContextMenu)

        self.layer_tree_view.customContextMenuRequested.connect(self.show_layer_context_menu)

        dock.setWidget(self.layer_tree_view)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

    def show_layer_context_menu(self, pos):
        """修复节点获取逻辑，确保能识别图层节点"""
        index = self.layer_tree_view.indexAt(pos)
        if not index.isValid():
            logger.warning("点击空白处，不显示菜单")
            return

        # 修复1：QGIS 3.44 兼容的节点获取方式（替代 index2node）
        model = index.model()

        source_index = model.mapToSource(index)
        source_model = source_index.model()
        layer_name = source_model.data(source_index, Qt.DisplayRole)
        if not layer_name:
            return

        # 通过图层名称查找对应的图层
        layers = self.project.mapLayersByName(layer_name)
        if not layers:
            logger.info(f"未找到图层: {layer_name}")
            return

        # layer = layers[0]
        # 修复2：确保图层有效
        self.selected_layer = layers[0]
        if self.selected_layer is None:
            logger.info("选中的图层无效")
            return

        # 后续菜单创建逻辑不变
        context_menu = QMenu(self.layer_tree_view)

        set_edit_action = QAction(QIcon.fromTheme("zoom to layer"), "缩放至图层", self)
        set_edit_action.triggered.connect(lambda: self.zoom_to_layer_extent_transform(layer=self.selected_layer))
        context_menu.addAction(set_edit_action)

        set_edit_action = QAction(QIcon.fromTheme("edit"), "设为编辑图层", self)
        set_edit_action.triggered.connect(self.set_current_edit_layer)
        context_menu.addAction(set_edit_action)

        attribute_action = QAction(QIcon.fromTheme("attribute"), "打开属性表", self)
        attribute_action.triggered.connect(self.open_attribute_table)
        context_menu.addAction(attribute_action)

        execute_sql_action = QAction(QIcon.fromTheme("sql"), "执行SQL", self)
        execute_sql_action.triggered.connect(self.execute_sql)
        context_menu.addAction(execute_sql_action)
        # 样式子菜单
        submenu = QMenu("样式", context_menu)
        style_edit_action = QAction("编辑样式", self)
        style_edit_action.triggered.connect(lambda: self.show_style_edit_dia(self.selected_layer))
        submenu.addAction(style_edit_action)

        open_style_action = QAction("打开样式配置（.qml/.sld）", self)
        open_style_action.triggered.connect(lambda: StyleOpenDia(self.selected_layer, self).show())
        # 打开样式事件
        submenu.addAction(open_style_action)

        save_style_action = QAction("保存图层样式", self)
        # 打开样式事件
        save_style_action.triggered.connect(lambda: self.save_layer_style(self.selected_layer))
        submenu.addAction(save_style_action)
        context_menu.addMenu(submenu)
        # 移除图层
        remove_action = QAction(QIcon("png/remove.png"), "移除", self)
        remove_action.triggered.connect(lambda: self.project.removeMapLayer(self.selected_layer.id()))
        context_menu.addAction(remove_action)

        export_action = QAction("图层导出（另存为）", self)
        export_action.triggered.connect(lambda: save_as_export(self.selected_layer, self.project))
        context_menu.addAction(export_action)

        open_source_ = QAction("属性", self)

        open_source_.triggered.connect(lambda: self._open_attribute_dia())
        context_menu.addAction(open_source_)

        context_menu.exec_(self.layer_tree_view.mapToGlobal(pos))
    def _open_attribute_dia(self):
        try:
            dia = QgsVectorLayerProperties(
                canvas=self.canvas,
                lyr=self.selected_layer,
                messageBar=self.messageBar,
                parent=self
            )
            dia.setWindowIcon(QIcon("resource/png/app_icon.png"))
            dia.exec_()
        except Exception as e:
            logger.error(e)
    def execute_sql(self):
        # 170326
        dia = ExecuteSQlDialog(self.selected_layer, self, "执行SQL")
        dia.show()
        # filter_expression = 'objectid = 170326'
        # self.selected_layer.setSubsetString(filter_expression)
        # self.selected_layer.triggerRepaint()

    def create_browser(self):
        """创建数据浏览器（复用QGIS原生组件和事件，3.44完美兼容）"""
        try:
            # 1. 创建Dock容器（与图层树风格一致）
            self.browser_dock = QDockWidget("数据浏览器", self)
            self.browser_dock.setAllowedAreas(Qt.RightDockWidgetArea)
            # 设置Dock特性（原生风格：可关闭、可移动、可浮动）
            self.browser_dock.setFeatures(
                QDockWidget.DockWidgetClosable |
                QDockWidget.DockWidgetMovable |
                QDockWidget.DockWidgetFloatable
            )

            self.browser_gui_model = QgsBrowserGuiModel(self)
            self.browser_gui_model.initialize()
            self.browser_gui_model.refresh()

            self.browser_view = QgsBrowserTreeView(self)
            self.browser_view.setModel(self.browser_gui_model)
            self.browser_view.setHeaderHidden(True)

            self.browser_view.setContextMenuPolicy(Qt.CustomContextMenu)
            self.browser_view.customContextMenuRequested.connect(self.show_browser_context_menu)

            self.browser_view.doubleClicked.connect(self.__add_layer_all__)

            # self.browser_view.setContextMenuPolicy(Qt.DefaultContextMenu)

            # ✅ 启用拖拽源（允许拖出）
            self.browser_view.setDragEnabled(True)
            self.browser_dock.setWidget(self.browser_view)
            self.addDockWidget(Qt.RightDockWidgetArea, self.browser_dock)


        except Exception as e:
            logger.info(f"创建数据浏览器失败：{str(e)}")

    def __add_layer_all__(self, index_or_pos):
        """
        从浏览器模型的 index 或 pos 添加图层
        支持双击或右键调用
        """
        # 如果传入的是 QPoint（来自右键），先转为 index
        if hasattr(index_or_pos, 'x') and hasattr(index_or_pos, 'y'):  # 是 QPoint
            pos = index_or_pos
            index = self.browser_view.indexAt(pos)
        else:
            index = index_or_pos  # 已是 QModelIndex

        if not index.isValid():
            return

        item = self.browser_gui_model.dataItem(index)
        if not item:
            return

        from qgis.core import QgsLayerItem, QgsVectorLayer, QgsRasterLayer

        # 只处理图层项
        if not isinstance(item, QgsLayerItem):
            return

        uri = item.uri()
        name = item.name()
        provider_key = item.providerKey()  # 例如 'ogr', 'gdal', 'postgres' 等

        # 尝试加载矢量图层
        vlayer = QgsVectorLayer(uri, name, provider_key)
        if vlayer.isValid():
            vlayer.path_c = item.path()
            self.project.addMapLayer(vlayer)
            return

        # 尝试加载栅格图层
        rlayer = QgsRasterLayer(uri, name, provider_key)
        if rlayer.isValid():
            rlayer.path_c = item.path()
            self.project.addMapLayer(rlayer)
            return

    def show_browser_context_menu(self, pos):
        """显示 QGIS 原生右键菜单（QGIS 3.40 兼容）"""
        index = self.browser_view.indexAt(pos)
        if not index.isValid():
            return

        item = self.browser_gui_model.dataItem(index)
        if not item:
            return

        from qgis.PyQt.QtWidgets import QMenu
        from qgis.gui import QgsGui

        menu = QMenu(self)

        # ✅ QGIS 3.40 使用 populateContextMenu（不是 actions()）
        registry = QgsGui.instance().dataItemGuiProviderRegistry()
        for provider in registry.providers():
            try:
                provider.populateContextMenu(item, menu, selectedItems=[item], context=QgsDataItemGuiContext())
            except Exception as e:
                logger.info(e)
                continue

        # 如果菜单为空，至少加个“刷新”
        # if menu.isEmpty():
        refresh_action = menu.addAction("刷新")
        refresh_action.triggered.connect(lambda: item.refresh())

        # 显示菜单
        global_pos = self.browser_view.viewport().mapToGlobal(pos)
        menu.exec_(global_pos)

    def on_browser_double_click(self, index):
        """处理数据浏览器双击事件"""
        if not index.isValid():
            return

        # 从模型获取数据项
        item = self.browser_gui_model.dataItem(index)
        if not item:
            return

        # 只处理图层项（避免双击文件夹/连接等无效操作）
        from qgis.core import QgsLayerItem
        if isinstance(item, QgsLayerItem):
            uri = item.uri()
            name = item.name()

            # 判断图层类型（矢量/栅格）
            from qgis.core import QgsVectorLayer, QgsRasterLayer, QgsProject

            # 尝试作为矢量图层加载
            vlayer = QgsVectorLayer(uri, name, "ogr")
            if vlayer.isValid():
                vlayer.path_c = item.path()
                QgsProject.instance().addMapLayer(vlayer)
                return

            # 尝试作为栅格图层加载
            rlayer = QgsRasterLayer(uri, name)
            if rlayer.isValid():
                rlayer.path_c = item.path()
                QgsProject.instance().addMapLayer(rlayer)
                return

            # 可选：提示加载失败
            from qgis.PyQt.QtWidgets import QMessageBox
            QMessageBox.warning(self, "加载失败", f"无法加载图层：{name}")

    '''
        创建样式编辑
    '''

    def create_layer_style(self):
        self.style_dock = QDockWidget("图层样式编辑", self)
        self.style_dock.setAllowedAreas(Qt.RightDockWidgetArea)
        a = QgsMapLayerStyleManagerWidget(self.selected_layer, self.canvas, self.style_dock)
        self.style_dock.setWidget(a)
        self.addDockWidget(Qt.RightDockWidgetArea, self.style_dock)

    '''
        保存样式
    '''

    def save_layer_style(self, layer):

        dia = StyleSaveDia(self.selected_layer, self)
        dia.exec_()

    def handle_browser_double_click(self, index):
        """浏览器双击加载图层（与图层树逻辑统一，3.44 兼容）"""
        try:
            # 从模型获取双击项的路径（QGIS 3.44 正确获取方式）
            item = self.browser_model.dataItem(index)
            # if not item:
            #     return  # 仅处理文件类型

            file_path = item.path()
            layer_name = os.path.splitext(item.name())[0]

            # 加载矢量图层（与图层树加载逻辑一致）
            # layer = QgsVectorLayer(file_path, layer_name, "ogr")
            # if layer.isValid():
            self.load_file(file_path)
            # else:
            #     error_detail = layer.error().message()
            #     QMessageBox.warning(self, "失败", f"图层加载失败\n文件：{item.name()}\n错误：{error_detail}")

        except Exception as e:
            QMessageBox.warning(self, "错误", f"双击事件处理失败：{str(e)}")

    ''' 图例双击打开样式编辑 '''

    def handle_layer_double_click(self, index):
        """简化版：仅处理顶层图层（非组、非图例项）"""
        index_model = index.model()
        source_index = index_model.mapToSource(index)
        source_model = source_index.model()
        layer_name = source_model.data(source_index, Qt.DisplayRole)
        if not layer_name:
            return

        # 通过图层名称查找对应的图层
        layers = self.project.mapLayersByName(layer_name)
        if not layers:
            logger.info(f"未找到图层: {layer_name}")
            return

        layer = layers[0]
        if not layer or not layer.isValid():
            logger.info(f"图层无效: {layer_name}")
            return

        # 这里你可以根据需要执行相应的操作
        # 例如：显示图层属性、缩放到图层等
        logger.info(f"双击了图层: {layer_name}")
        self.show_style_edit_dia(layer)

    def handle_layer_click(self, index):
        """简化版：仅处理顶层图层（非组、非图例项）"""
        index_model = index.model()
        source_index = index_model.mapToSource(index)
        source_model = source_index.model()
        layer_name = source_model.data(source_index, Qt.DisplayRole)
        if not layer_name:
            return

        # 通过图层名称查找对应的图层
        layers = self.project.mapLayersByName(layer_name)
        if not layers:
            logger.info(f"未找到图层: {layer_name}")
            return
        if not isinstance(layers[0], QgsVectorLayer):
            return

        self.edit_layer = layers[0]

        if not self.edit_layer or not self.edit_layer.isValid():
            logger.info(f"图层无效: {layer_name}")
            return

        # 这里你可以根据需要执行相应的操作
        # 例如：显示图层属性、缩放到图层等
        logger.info(f"双击了图层: {layer_name}")
        self.draw_tool.start_edit_f()
        # Draw(self)
        # self.show_style_edit_dia(layer)

    def show_style_edit_dia(self, layer):
        global_style = QgsStyle.defaultStyle()
        dlg = QgsRendererPropertiesDialog(
            layer=layer,  # 目标矢量图层
            style=global_style,  # 全局样式管理器（必填参数）
            embedded=False,  # 不嵌入，作为独立窗口
            parent=self  # 父窗口为当前主窗口
        )
        dlg.setWindowTitle(f"图层样式 - {layer.name()}")
        # 显示对话框并等待用户操作
        if dlg.exec_():
            # 用户点击"确定"后刷新图层和画布
            layer.triggerRepaint()
            self.canvas.refresh()

    ''' 创建地图工具 '''

    def create_toolbar(self):
        """地图工具条（确保工具实例不被垃圾回收）"""
        self.toolbar = self.addToolBar("地图工具")
        self.toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)

        # 平移工具
        self.pan_tool = QgsMapToolPan(self.canvas)
        self.pan_action = QAction("平移", self)
        self.pan_action.triggered.connect(lambda: self.canvas.setMapTool(self.pan_tool))
        self.toolbar.addAction(self.pan_action)
        # 查看属性信息
        self.sx_tool = FindFeatureTool(self.canvas)
        self.sx_action = QAction("属性", self)
        self.sx_action.triggered.connect(lambda: self.canvas.setMapTool(self.sx_tool))
        self.toolbar.addAction(self.sx_action)

        # 放大工具
        self.zoom_in_tool = QgsMapToolZoom(self.canvas, False)
        self.zoom_in_action = QAction("放大", self)
        self.zoom_in_action.triggered.connect(lambda: self.canvas.setMapTool(self.zoom_in_tool))
        self.toolbar.addAction(self.zoom_in_action)

        # 缩小工具
        self.zoom_out_tool = QgsMapToolZoom(self.canvas, True)
        self.zoom_out_action = QAction("缩小", self)
        self.zoom_out_action.triggered.connect(lambda: self.canvas.setMapTool(self.zoom_out_tool))
        self.toolbar.addAction(self.zoom_out_action)
        # 质检工具
        # self.zoom_out_tool = QgsMapToolZoom(self.canvas, True)
        self.zoom_out_action = QAction("质检", self)
        self.zoom_out_action.triggered.connect(lambda: JsonTreeView(self, "").show())
        self.toolbar.addAction(self.zoom_out_action)

        self.analyse = QAction("分析", self)
        self.analyse.triggered.connect(lambda: self.fun2())
        self.toolbar.addAction(self.analyse)

        # # 编辑工具
        # self.edit_toolbar = self.addToolBar("编辑工具")
        # self.edit_toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        #
        # # 点绘制工具
        # self.point_draw_tool = PointDrawTool(self.canvas, None)
        # self.point_draw_action = QAction("绘制点", self)
        # self.point_draw_action.triggered.connect(self.activate_point_draw_tool)
        # self.edit_toolbar.addAction(self.point_draw_action)
        #
        # # 编辑工具
        # self.edit_tool = EditTool(self.canvas, self)  # 传递主窗口引用
        # self.edit_tool_action = QAction("编辑工具", self)
        # self.edit_tool_action.triggered.connect(lambda: self.canvas.setMapTool(self.edit_tool))
        # self.edit_toolbar.addAction(self.edit_tool_action)
        #
        # # 保存编辑工具
        # self.save_edit_action = QAction("保存编辑", self)
        # self.save_edit_action.triggered.connect(self.save_edits)
        # self.edit_toolbar.addAction(self.save_edit_action)
        #
        # # 取消编辑工具
        # self.cancel_edit_action = QAction("取消编辑", self)
        # self.cancel_edit_action.triggered.connect(self.cancel_edits)
        # self.edit_toolbar.addAction(self.cancel_edit_action)
        #
        # self.toolbar.addAction(self.edit_tool_action)
        # # 全图工具
        self.zoom_full_action = QAction("全图", self)
        self.zoom_full_action.triggered.connect(self.zoom_to_full_extent)
        self.toolbar.addAction(self.zoom_full_action)

        # 刷新工具
        self.refresh_action = QAction("刷新", self)
        self.refresh_action.triggered.connect(self.canvas.refresh)
        self.toolbar.addAction(self.refresh_action)

        # 默认激活平移工具
        self.canvas.setMapTool(self.pan_tool)

    ''' 创建菜单 '''

    def create_menu(self):
        """创建菜单（保留核心功能，简化冗余代码）"""
        menubar = self.menuBar()

        # 文件菜单
        file_menu = menubar.addMenu('文件(&F)')
        # 打开文件
        open_action = QAction('打开文件(&O)', self)
        open_action.setShortcut('Ctrl+O')
        open_action.triggered.connect(self.open_file_dialog)
        file_menu.addAction(open_action)
        # 退出
        exit_action = QAction('退出(&X)', self)
        exit_action.setShortcut('Ctrl+Q')
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

    '''缩放全屏'''

    def zoom_to_full_extent(self):
        """缩放到所有图层的合并范围（修复空图层问题）"""
        layers = [l for l in QgsProject.instance().mapLayers().values() if l.isValid()]
        if not layers:
            QMessageBox.information(self, "提示", "当前无有效图层")
            return

        # 计算所有图层的合并范围
        combined_extent = layers[0].extent()
        for layer in layers[1:]:
            combined_extent.combineExtentWith(layer.extent())

        self.canvas.setExtent(combined_extent)
        self.canvas.refresh()

    def zoom_to_layer_extent_transform(self, layer=None, extent=None, source_crs=None):
        transformed_extent = None
        dest_crs = self.canvas.mapSettings().destinationCrs()  # canvas 的目标坐标系
        if layer is not None:
            source_crs = layer.crs()  # 图层自身的坐标系
            if source_crs != dest_crs:
                # 创建坐标转换器（注意：QGIS 3.x 需要传入 QgsProject 实例）
                transform = QgsCoordinateTransform(source_crs, dest_crs, QgsProject.instance())

                # 获取原始 extent
                original_extent = self.selected_layer.extent()

                # 转换为目标 CRS 下的 extent
                transformed_extent = transform.transformBoundingBox(original_extent)
            else:
                # CRS 相同，直接使用
                transformed_extent = self.selected_layer.extent()

        else:
            if source_crs != dest_crs:
                # 创建坐标转换器（注意：QGIS 3.x 需要传入 QgsProject 实例）
                transform = QgsCoordinateTransform(source_crs, dest_crs, self.project)
                # 转换为目标 CRS 下的 extent
                transformed_extent = transform.transformBoundingBox(extent)
            else:
                # CRS 相同，直接使用
                transformed_extent = extent
        self.canvas.setExtent(transformed_extent)
        self.canvas.refresh()

    ''' 设置当前编辑图层 '''

    def set_current_edit_layer(self):
        tool = LayerEditTool(self.selected_layer)
        tool.start_editing()

    def fun2(self):
        dock = AnalysePanel(self)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        self.current_analyse_dock = dock
        dock.show()

    def open_attribute_table(self, layer=None, filter_str: string = None):
        c_layer = self.selected_layer if not layer else layer

        if self.current_attribute_dock is None:
            # 创建新的停靠窗口
            dock = PagedAttributeDockWidget(c_layer,
                                            f"属性表 - {c_layer.name()}", 50, parent=self,
                                            filter_str=filter_str if filter_str else '1=1')

            # 添加到主窗口
            self.addDockWidget(Qt.RightDockWidgetArea, dock)

            # 保存引用
            self.current_attribute_dock = dock

            # 显示窗口
            dock.show()
        else:
            # 如果对话框已存在，只是显示它
            self.current_attribute_dock.add_new_layer(c_layer, self, 50,
                                                      f"属性表 - {c_layer.name()}")

        # self.attribute_table_dialog.show()

        # 连接关闭信号，清理引用

    '''打开文件对话框'''

    def open_file_dialog(self):
        """打开文件对话框（支持多种矢量格式）"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            '选择矢量文件',
            '',
            '矢量文件 (*.shp *.geojson *.gpkg *.gdb);;所有文件 (*)'
        )
        if file_path:
            self.load_file(file_path)

    '''加载文件对象'''

    def load_file(self, file_path):
        """修复文件加载逻辑（支持GDB数据库、单个图层）"""
        file_ext = os.path.splitext(file_path)[1].lower()

        try:
            # 处理GDB数据库文件（需指定图层名）
            if file_ext == '.gdb':
                ogr.RegisterAll()
                gdb_dataset = ogr.Open(file_path)
                if not gdb_dataset:
                    logger.info(f"无法打开 GDB 文件：{file_path}")
                    return []

                # 2. 遍历 GDB 中的所有要素类（图层）
                for i in range(gdb_dataset.GetLayerCount()):
                    feature_class = gdb_dataset.GetLayerByIndex(i)
                    fc_name = feature_class.GetName()  # 获取要素类名称

                    # 3. 构建数据源路径，创建 QGIS 图层
                    source = f"{file_path}|layername={fc_name}"
                    layer = QgsVectorLayer(source, fc_name, "ogr")
                    layer.path_c = file_path

                    if layer.isValid():
                        self.project.addMapLayer(layer)
                        self.canvas.setExtent(layer.extent())
                gdb_dataset.Destroy()
            # 处理普通单个图层（SHP/GeoJSON/GPKG）
            else:
                layer_name = os.path.basename(file_path)
                layer = QgsVectorLayer(file_path, layer_name, "ogr")
                self.project.addMapLayer(layer)
                self.canvas.setExtent(layer.extent())
                layer.path_c = file_path

            self.canvas.refreshAllLayers()

        except Exception as e:
            QMessageBox.critical(self, "加载错误", f"文件加载失败：{str(e)}")
            logger.info(f"加载文件详细错误：{repr(e)}")

    """关闭窗口时释放QGIS资源"""

    def closeEvent(self, event):
        """关闭窗口时释放QGIS资源"""
        QgsProject.instance().clear()
        app.exitQgis()
        event.accept()


if __name__ == "__main__":
    try:
        icon = QIcon("resource/png/app_icon.png")
        app.setWindowIcon(icon)

        window = QGISMapWindow()
        window.show()
        sys.exit(app.exec_())
    except Exception as e:
        logger.info(e)
