from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QToolBar, QToolButton, QMenu, QAction, QMessageBox
from qgis._core import QgsFeature, QgsGeometry, QgsWkbTypes, QgsPointXY, QgsEditorWidgetSetup, QgsVectorLayerUtils
from qgis._gui import QgsAttributeDialog, QgsMapTool, QgsMapToolEmitPoint, QgsRubberBand, QgsAttributeEditorContext

from qgis.utils import iface

from .CustomAttrDialog import CustomAttrDialog


# from CustomAttrDialog import CustomAttrDialog


class Draw:
    def __init__(self, parent):
        self.temp_points = []
        self.rubber_band = None
        self.active_tool = None
        self.parent = parent
        self.digitizing_toolbar = QToolBar("编辑工具")
        self.digitizing_toolbar.setObjectName("DigitizingToolbar")

        self.digitize_button = QToolButton()
        self.digitize_button.setText("编辑器")
        self.digitize_button.setPopupMode(QToolButton.InstantPopup)

        # 创建下拉菜单
        self.digitize_menu = QMenu()

        # 添加子操作（带图标）
        self.start_edit = QAction("开始编辑", self.digitize_menu)
        self.start_edit.triggered.connect(lambda: self.start_edit_fun())
        self.stop_edit = QAction("停止编辑", self.digitize_menu)
        self.stop_edit.triggered.connect(lambda: self.stop_edit_fun())
        self.save_edit = QAction("保存所选图层编辑", self.digitize_menu)
        self.stop_edit.triggered.connect(lambda: self.parent.edit_layer.commitChanges())

        self.start_edit.setEnabled(self.parent.edit_layer is not None)
        self.stop_edit.setEnabled(self.parent.edit_layer is not None)
        self.save_edit.setEnabled(self.parent.edit_layer is not None)

        # 可选：设置图标（需有图标路径）
        # add_point_action.setIcon(QIcon("icons/mActionAddPoint.svg"))

        self.digitize_menu.addAction(self.start_edit)
        self.digitize_menu.addAction(self.stop_edit)
        self.digitize_menu.addAction(self.save_edit)

        # 关联菜单
        self.digitize_button.setMenu(self.digitize_menu)

        # 将按钮添加到主工具栏
        self.digitizing_toolbar.addWidget(self.digitize_button)

        # 3. 添加要素下拉工具（点/线/面）
        self.add_feature_button = QToolButton()
        self.add_feature_button.setText("添加要素")
        self.add_feature_button.setPopupMode(QToolButton.InstantPopup)
        self.add_feature_menu = QMenu()
        # --- 添加其他独立按钮 ---
        # 点要素工具
        self.add_point_action = QAction("添加点", self.add_feature_menu)
        self.add_point_action.triggered.connect(lambda: self.enable_draw_tool("point"))
        # 线要素工具
        self.add_line_action = QAction("添加线", self.add_feature_menu)
        self.add_line_action.triggered.connect(lambda: self.enable_draw_tool("line"))
        # 面要素工具
        self.add_polygon_action = QAction("添加面", self.add_feature_menu)
        self.add_polygon_action.triggered.connect(lambda: self.enable_draw_tool("polygon"))

        # 初始状态：未启动编辑时，添加要素工具不可用
        self.add_point_action.setEnabled(False)
        self.add_line_action.setEnabled(False)
        self.add_polygon_action.setEnabled(False)

        self.add_feature_menu.addAction(self.add_point_action)
        self.add_feature_menu.addAction(self.add_line_action)
        self.add_feature_menu.addAction(self.add_polygon_action)
        self.add_feature_button.setMenu(self.add_feature_menu)
        self.digitizing_toolbar.addWidget(self.add_feature_button)

        self.parent.addToolBar(self.digitizing_toolbar)

    def start_edit_f(self):
        self.start_edit.setEnabled(True)
        # self.stop_edit.setEnabled(True)

    ''' 图层开启编辑 '''
    def start_edit_fun(self):
        self.parent.edit_layer.startEditing()
        self.start_edit.setEnabled(False)
        self.stop_edit.setEnabled(True)
        self.save_edit.setEnabled(True)

        g_type = self.parent.edit_layer.geometryType()
        if g_type == 0:
            self.add_point_action.setEnabled(True)
        if g_type == 1:
            self.add_line_action.setEnabled(True)
        if g_type == 2:
            self.add_polygon_action.setEnabled(True)

    ''' 图层开启编辑 '''

    def stop_edit_fun(self):
        # self.parent.edit_layer.rollBack()
        layer = self.parent.edit_layer
        buffer = layer.editBuffer()
        if buffer is not None:

            reply = QMessageBox.warning(
                self.parent,  # 父窗口
                "警告",  # 弹窗标题
                "是否保存编辑的内容！",  # 弹窗内容
                QMessageBox.Yes | QMessageBox.No,  # 显示的按钮（是 + 否）
                QMessageBox.Yes  # 默认选中的按钮
            )
            if reply == QMessageBox.Yes:
                self.parent.edit_layer.commitChanges(stopEditing=True)
            else:
                self.parent.edit_layer.rollBack()
                self.parent.edit_layer.commitChanges(stopEditing=True)
        self.start_edit.setEnabled(True)
        self.stop_edit.setEnabled(False)
        self.save_edit.setEnabled(False)
        self.clear_draw_tool()

    def clear_draw_tool(self):
        """清除绘制工具、橡皮筋和临时点"""
        if self.active_tool:
            self.parent.canvas.unsetMapTool(self.active_tool)
            self.active_tool = None
        if self.rubber_band:
            self.rubber_band.reset()
            self.rubber_band = None
        if self.temp_points:
            self.temp_points.clear()
        else:
            self.temp_points = []
        
    def enable_draw_tool(self, geom_type: str):
        """激活指定类型的绘制工具（点/线/面）"""
        # 先清除之前的工具和临时图形
        self.clear_draw_tool()

        # 初始化橡皮筋（临时绘制轨迹）
        rubber_band_type = {
            "point": QgsWkbTypes.PointGeometry,
            "line": QgsWkbTypes.LineGeometry,
            "polygon": QgsWkbTypes.PolygonGeometry
        }[geom_type]
        self.rubber_band = QgsRubberBand(self.parent.canvas, rubber_band_type)
        self.rubber_band.setColor(Qt.red)  # 橡皮筋颜色
        self.rubber_band.setWidth(2)  # 线宽（点/线）
        self.rubber_band.setFillColor(QColor(255, 0, 0, 50))  # 面填充色（半透明）

        # 根据几何类型创建对应绘制工具
        if geom_type == "point":
            self.active_tool = PointDrawTool(self.parent.canvas, self)
        elif geom_type == "line":
            self.active_tool = LineDrawTool(self.parent.canvas, self)
        elif geom_type == "polygon":
            self.active_tool = PolygonDrawTool(self.parent.canvas, self)

        # 激活工具
        self.parent.canvas.setMapTool(self.active_tool)
        QMessageBox.information(self.parent, "提示", f"已激活「添加{geom_type}」工具，点击画布绘制！")

    def finish_draw(self, geometry: QgsGeometry):
        """绘制完成处理（QGIS 3.40 LTS 稳定版：解决控件创建失败+版本兼容）"""
        # 1. 基础校验：几何有效性
        print(self.parent.edit_layer.isEditable())
        if geometry is None:
            QMessageBox.warning(self.parent, "警告", "绘制的几何图形无效！")
            self.clear_draw_tool()
            return
        self.temp_points = []

        # 2. 几何类型与图层匹配校验
        layer = self.parent.edit_layer
        layer_geom_type = layer.geometryType()
        draw_geom_type = geometry.type()
        geom_type_map = {
            QgsWkbTypes.PointGeometry: "点",
            QgsWkbTypes.LineGeometry: "线",
            QgsWkbTypes.PolygonGeometry: "面"
        }
        layer_geom_name = geom_type_map.get(layer_geom_type, "未知")
        draw_geom_name = geom_type_map.get(draw_geom_type, "未知")

        if layer_geom_type != draw_geom_type:
            QMessageBox.warning(
                self.parent,
                "警告",
                f"绘制类型与图层不匹配！\n图层类型：{layer_geom_name}，绘制类型：{draw_geom_name}"
            )
            self.clear_draw_tool()
            return

        # 4. 创建要素并绑定几何
        feature = QgsVectorLayerUtils.createFeature(layer, geometry)
        feature.setGeometry(geometry)

        dialog = CustomAttrDialog(layer, feature=feature, parent=self.parent)
        dialog.setWindowTitle(f"编辑{draw_geom_name}要素属性")

        dialog.show()



class PointDrawTool(QgsMapToolEmitPoint):
    """点要素绘制工具：点击画布直接创建点"""
    def __init__(self, canvas, draw_manager):
        super().__init__(canvas)
        self.canvas = canvas
        self.draw_manager = draw_manager

        self.rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        self.rubber_band.setColor(Qt.red)  # 橡皮筋颜色
        self.rubber_band.setWidth(2)  # 线宽（点/线）

    def canvasReleaseEvent(self, event):
        """鼠标释放事件（点击画布创建点）"""
        # 获取点击位置的地图坐标
        point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())
        # 创建点几何
        geometry = QgsGeometry.fromPointXY(QgsPointXY(point.x(), point.y()))
        # 通知管理器完成绘制
        self.draw_manager.finish_draw(geometry)


class LineDrawTool(QgsMapTool):
    """线要素绘制工具：点击添加节点，右键结束绘制"""
    def __init__(self, canvas, draw_manager):
        super().__init__(canvas)
        self.canvas = canvas
        self.draw_manager = draw_manager

        self.line_rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self.line_rubber_band.setColor(Qt.red)  # 橡皮筋颜色
        self.line_rubber_band.setWidth(2)  # 线宽（点/线）
        self.line_rubber_band.setLineStyle(Qt.DashLine)
        self.is_over = False

    def canvasMoveEvent(self, event):
        if self.is_over is False:
            l = len(self.draw_manager.temp_points)
            if l == 0:
                return
            point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())
            self.line_rubber_band.reset(QgsWkbTypes.LineGeometry)
            self.line_rubber_band.addPoint(self.draw_manager.temp_points[l-1])
            self.line_rubber_band.addPoint(point)
        self.line_rubber_band.show()

    def canvasPressEvent(self, event):
        """鼠标按压事件（添加节点）"""
        if event.button() == Qt.LeftButton:  # 左键添加节点
            self.is_over = False
            point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())
            self.draw_manager.temp_points.append(QgsPointXY(point.x(), point.y()))
            # 更新橡皮筋显示
            self.update_rubber_band()
        elif event.button() == Qt.RightButton:  # 右键结束绘制
            self.is_over = True
            self.line_rubber_band.reset(QgsWkbTypes.LineGeometry)
            if len(self.draw_manager.temp_points) >= 2:  # 线至少需要2个点
                geometry = QgsGeometry.fromPolylineXY(self.draw_manager.temp_points)
                self.draw_manager.finish_draw(geometry)
            else:
                QMessageBox.warning(self.parent.canvas, "警告", "线要素至少需要2个节点！")
                self.draw_manager.clear_draw_tool()

    def update_rubber_band(self):
        """更新橡皮筋（显示已绘制的线）"""
        if len(self.draw_manager.temp_points) < 1:
            return
        self.draw_manager.rubber_band.reset(QgsWkbTypes.LineGeometry)
        for point in self.draw_manager.temp_points:
            self.draw_manager.rubber_band.addPoint(point)
        self.draw_manager.rubber_band.show()


class PolygonDrawTool(QgsMapTool):
    """面要素绘制工具：点击添加节点，右键结束绘制"""
    def __init__(self, canvas, draw_manager):
        super().__init__(canvas)
        self.canvas = canvas
        self.draw_manager = draw_manager

        self.line_rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self.line_rubber_band.setColor(Qt.red)  # 橡皮筋颜色
        self.line_rubber_band.setWidth(2)  # 线宽（点/线）
        self.line_rubber_band.setLineStyle(Qt.DashLine)
        self.is_over = False
        # self.rubber_band.setFillColor(QColor(255, 0, 0, 50))  # 面填充色（半透明）

    def canvasMoveEvent(self, event):
        if self.is_over is False:
            l = len(self.draw_manager.temp_points)
            if l == 0:
                return
            point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())
            self.line_rubber_band.reset(QgsWkbTypes.LineGeometry)
            self.line_rubber_band.addPoint(self.draw_manager.temp_points[0])
            self.line_rubber_band.addPoint(point)
            self.line_rubber_band.addPoint(self.draw_manager.temp_points[l-1])
        self.line_rubber_band.show()

    def canvasPressEvent(self, event):
        """鼠标按压事件（添加节点）"""
        if event.button() == Qt.LeftButton:  # 左键添加节点
            self.is_over = False
            point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())
            self.draw_manager.temp_points.append(QgsPointXY(point.x(), point.y()))
            # 更新橡皮筋显示
            self.update_rubber_band()
        elif event.button() == Qt.RightButton:  # 右键结束绘制
            self.is_over = True
            self.line_rubber_band.reset(QgsWkbTypes.LineGeometry)
            if len(self.draw_manager.temp_points) >= 3:  # 面至少需要3个点
                # 闭合面（第一个点和最后一个点重合）
                if self.draw_manager.temp_points[0] != self.draw_manager.temp_points[-1]:
                    self.draw_manager.temp_points.append(self.draw_manager.temp_points[0])
                geometry = QgsGeometry.fromPolygonXY([self.draw_manager.temp_points])
                self.draw_manager.finish_draw(geometry)
            else:
                QMessageBox.warning(self.canvas, "警告", "面要素至少需要3个节点！")
                self.draw_manager.clear_draw_tool()

    def update_rubber_band(self):
        """更新橡皮筋（显示已绘制的面）"""
        if len(self.draw_manager.temp_points) < 1:
            return
        self.draw_manager.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        for point in self.draw_manager.temp_points:
            self.draw_manager.rubber_band.addPoint(point)
        self.draw_manager.rubber_band.show()