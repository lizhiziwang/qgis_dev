import math

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QToolBar, QToolButton, QMenu, QAction, QMessageBox
from qgis._core import QgsFeature, QgsGeometry, QgsWkbTypes, QgsPointXY, QgsEditorWidgetSetup, QgsVectorLayerUtils, \
    QgsRectangle, QgsCircle, QgsPoint
from qgis._gui import QgsAttributeDialog, QgsMapTool, QgsMapToolEmitPoint, QgsRubberBand, QgsAttributeEditorContext, \
    QgsMapToolPan

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
        self.temp_points.clear()

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

        # 3. 若当前图层恰好单选了一个要素 → 视为“重绘替换原要素”：只把新几何
        #    写回该要素（changeGeometry 保留 fid、属性原样，**不新增、不弹属性
        #    框**）——与线分割/融合“编辑原要素”约束一致，GPKG 等强 fid 数据源
        #    上也能干净提交；无选中 / 多选则维持下方“新增要素”流程。
        sel = layer.selectedFeatureIds()
        if len(sel) == 1:
            self._edit_selected_geometry(layer, next(iter(sel)), geometry, draw_geom_name)
            return

        # 4. 新增流程：创建要素并绑定几何
        feature = QgsVectorLayerUtils.createFeature(layer, geometry)
        feature.setGeometry(geometry)

        dialog = CustomAttrDialog(layer, feature=feature, parent=self.parent)
        dialog.setWindowTitle(f"编辑{draw_geom_name}要素属性")

        dialog.show()

    def _edit_selected_geometry(self, layer, fid, geometry: QgsGeometry, geom_name: str):
        """重绘替换：把新几何写回选中的原要素（保留 fid 与属性，不新增）。"""
        if not layer.isEditable():
            QMessageBox.warning(self.parent, "警告", "请先点击「开始编辑」再修改要素几何！")
            return
        feat = layer.getFeature(fid)
        if not feat.isValid():
            QMessageBox.warning(self.parent, "警告", "选中的要素已不存在，请重新选择！")
            layer.removeSelection()
            return
        # 一个撤销步骤：只改几何（属性原样保留），提交到强 fid 数据源也干净
        layer.beginEditCommand("修改要素几何")
        try:
            if not layer.changeGeometry(fid, geometry):
                raise RuntimeError("changeGeometry 失败")
        except RuntimeError as e:
            layer.cancelEditCommand()
            QMessageBox.warning(self.parent, "警告", f"几何写入失败：{e}")
            return
        layer.endEditCommand()
        # 画布外刷新（事件栈内不直接 refresh，推迟到出栈后）
        canvas = getattr(self.parent, "canvas", None)
        if canvas is not None:
            QTimer.singleShot(0, canvas.refresh)
        QMessageBox.information(
            self.parent, "提示", f"已更新选中的{geom_name}要素几何（Ctrl+Z 可撤销）。"
        )

class BaseDrawTool(QgsMapTool):
    finish = pyqtSignal(QgsGeometry)

    def __init__(self, canvas, parent=None, is_buffer=False, distance=0.0):
        super().__init__(canvas)
        self.canvas = canvas

        self.rubber_band_buffer = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band_buffer.setColor(Qt.red)
        self.rubber_band_buffer.setWidth(2)
        self.rubber_band_buffer.setLineStyle(Qt.DashLine)

        self.is_buffer = is_buffer
        self.distance = distance
        self.geom = None

    def finish_draw(self):
        try:
            # pan_tool = QgsMapToolPan(self.canvas)
            # self.canvas.setMapTool(pan_tool)
            self.canvas.unsetMapTool(self)
            if self.is_buffer:
                buffer = self.geom.buffer(self.distance, 16)
                self.rubber_band_buffer.addGeometry(buffer)

            self.finish.emit(self.geom)
        except Exception as e:
            print(e)

    def clear(self):
        self.rubber_band_buffer.hide()
        # pass


class PointDrawTool(BaseDrawTool):
    """点要素绘制工具：点击画布直接创建点"""
    def __init__(self, canvas, draw_manager, is_buffer=False, distance=0.0):
        super().__init__(canvas,is_buffer=is_buffer, distance=distance)
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
        # self.rubber_band.reset(QgsWkbTypes.PointGeometry)
        geometry = QgsGeometry.fromPointXY(QgsPointXY(point.x(), point.y()))
        self.geom = geometry

        self.rubber_band.addPoint(point)
        self.rubber_band.show()
        # 通知管理器完成绘制
        if self.draw_manager is not None:
            self.draw_manager.finish_draw(geometry)
        else:
            self.finish_draw()
    def clear(self):
        super().clear()
        self.rubber_band.hide()


class LineDrawTool(BaseDrawTool):
    """线要素绘制工具：点击添加节点，右键结束绘制"""
    def __init__(self, canvas, draw_manager, is_buffer=False, distance=0.0):
        super().__init__(canvas, is_buffer=is_buffer, distance=distance)
        self.canvas = canvas
        self.draw_manager = draw_manager

        self.line_rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self.line_rubber_band.setColor(Qt.red)  # 橡皮筋颜色
        self.line_rubber_band.setWidth(2)  # 线宽（点/线）
        self.line_rubber_band.setLineStyle(Qt.SolidLine)
        self.is_over = False
        self.temp_points = draw_manager.temp_points if draw_manager is not None else []

        self.line_rubber_band2 = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self.line_rubber_band2.setColor(Qt.red)  # 橡皮筋颜色
        self.line_rubber_band2.setWidth(2)  # 线宽（点/线）
        self.line_rubber_band2.setLineStyle(Qt.DashLine)

        self.geom = None

    def canvasMoveEvent(self, event):
        if self.is_over is False:
            l = len(self.temp_points)
            if l == 0:
                return
            point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())
            self.line_rubber_band2.reset(QgsWkbTypes.LineGeometry)
            self.line_rubber_band2.addPoint(self.temp_points[l-1])
            self.line_rubber_band2.addPoint(point)
            self.line_rubber_band2.show()


    def canvasPressEvent(self, event):
        """鼠标按压事件（添加节点）"""
        try:
            if event.button() == Qt.LeftButton:  # 左键添加节点
                self.is_over = False
                point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())
                self.temp_points.append(QgsPointXY(point.x(), point.y()))
                # 更新橡皮筋显示
                self.update_rubber_band()
            elif event.button() == Qt.RightButton:  # 右键结束绘制
                self.is_over = True
                self.line_rubber_band2.reset(QgsWkbTypes.LineGeometry)
                if len(self.temp_points) >= 2:  # 线至少需要2个点
                    geometry = QgsGeometry.fromPolylineXY(self.temp_points)
                    self.geom = geometry

                    if self.draw_manager is not None:
                        self.draw_manager.finish_draw(geometry)
                    else:
                        self.temp_points.clear()
                        self.finish_draw()
                else:
                    QMessageBox.warning(self.parent.canvas, "警告", "线要素至少需要2个节点！")
                    if self.draw_manager is not None:
                        self.draw_manager.clear_draw_tool()
        except Exception as e:
            print(e)

    def update_rubber_band(self):
        """更新橡皮筋（显示已绘制的线）"""
        try:
            if len(self.temp_points) < 2:
                return
            self.line_rubber_band.reset(QgsWkbTypes.LineGeometry)
            for point in self.temp_points:
                self.line_rubber_band.addPoint(point)
            self.line_rubber_band.show()
        except Exception as e:
            print(e)
    def clear(self):
        super().clear()
        self.line_rubber_band.hide()


class PolygonDrawTool(BaseDrawTool):
    """面要素绘制工具：点击添加节点，右键结束绘制"""
    def __init__(self, canvas, draw_manager, is_buffer=False, distance=0.0):
        super().__init__(canvas, is_buffer=is_buffer, distance=distance)
        self.canvas = canvas
        self.draw_manager = draw_manager

        self.line_rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self.line_rubber_band.setColor(Qt.red)  # 橡皮筋颜色
        self.line_rubber_band.setWidth(2)  # 线宽（点/线）
        self.line_rubber_band.setLineStyle(Qt.DashLine)
        self.is_over = False

        self.temp_points = draw_manager.temp_points if draw_manager is not None else []
        self.rubber_band = draw_manager.rubber_band if draw_manager is not None else None
        if self.rubber_band is None:
            self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
            self.rubber_band.setColor(Qt.red)
            self.rubber_band.setWidth(2)
            self.rubber_band.setFillColor(QColor(255, 0, 0, 50))
        # self.rubber_band.setFillColor(QColor(255, 0, 0, 50))  # 面填充色（半透明）
        self.geom = None

    def canvasMoveEvent(self, event):
        if not self.is_over:
            l = len(self.temp_points)
            if l == 0:
                return
            point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())
            self.line_rubber_band.reset(QgsWkbTypes.LineGeometry)
            self.line_rubber_band.addPoint(self.temp_points[0])
            self.line_rubber_band.addPoint(point)
            self.line_rubber_band.addPoint(self.temp_points[l-1])
        self.line_rubber_band.show()

    def canvasPressEvent(self, event):
        """鼠标按压事件（添加节点）"""
        if event.button() == Qt.LeftButton:  # 左键添加节点
            self.is_over = False
            point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())
            self.temp_points.append(QgsPointXY(point.x(), point.y()))
            # 更新橡皮筋显示
            self.update_rubber_band()
        elif event.button() == Qt.RightButton:  # 右键结束绘制
            self.is_over = True
            self.line_rubber_band.reset(QgsWkbTypes.LineGeometry)
            if len(self.temp_points) >= 3:  # 面至少需要3个点
                # 闭合面（第一个点和最后一个点重合）
                if self.temp_points[0] != self.temp_points[-1]:
                    self.temp_points.append(self.temp_points[0])
                geometry = QgsGeometry.fromPolygonXY([self.temp_points])
                self.geom = geometry

                if self.draw_manager is not None:
                    self.draw_manager.finish_draw(geometry)
                else:
                    self.temp_points.clear()
                    self.finish_draw()

            else:
                # self.messageBar.pushMessage("操作成功", level=Qgis.Success, duration=3)
                QMessageBox.warning(self.canvas, "警告", "面要素至少需要3个节点！")
                if self.draw_manager is not None:
                    self.draw_manager.clear_draw_tool()

    def update_rubber_band(self):
        """更新橡皮筋（显示已绘制的面）"""
        if len(self.temp_points) < 1:
            return
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        for point in self.temp_points:
            self.rubber_band.addPoint(point)
        self.rubber_band.show()

    def clear(self):
        super().clear()
        self.rubber_band.hide()

class RectangleDrawTool(BaseDrawTool):
    """
        绘制矩形，左键绘制
    """
    def __init__(self, canvas, draw_manager=None, is_buffer=False, distance=0.0):
        super().__init__(canvas, is_buffer=is_buffer, distance=distance)
        self.canvas = canvas
        self.draw_manager = draw_manager
        self.rubber_band = draw_manager.rubber_band if draw_manager is not None else None
        self.temp_points = draw_manager.temp_points if draw_manager is not None else []

        self.is_over = False
        if self.rubber_band is None:
            self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
            self.rubber_band.setColor(Qt.red)
            self.rubber_band.setWidth(2)
            self.rubber_band.setFillColor(QColor(255, 0, 0, 50))


    def canvasMoveEvent(self, event):
        try:
            if not self.is_over:
                l = len(self.temp_points)
                if l == 0:
                    return
                point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())
                if l > 1:
                    self.temp_points = [self.temp_points[0]]
                self.temp_points.append(QgsPointXY(point.x(), point.y()))
                self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)

                rectangle = QgsRectangle(self.temp_points[0], self.temp_points[1])
                geometry = QgsGeometry.fromRect(rectangle)
                self.geom = geometry

                self.update_rubber_band()
        except Exception as e:
            print(e)
    def canvasPressEvent(self, event):
        if not self.is_over:
            if event.button() == Qt.LeftButton:  # 左键添加节点
                self.is_over = False
                point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())
                self.temp_points.append(QgsPointXY(point.x(), point.y()))
            else:
                self.is_over = True
                if self.draw_manager is not None:
                    self.draw_manager.finish_draw(self.geom)
                else:
                    self.temp_points.clear()
                    self.finish_draw()



    def update_rubber_band(self):
        if len(self.temp_points) < 2:
            return
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        p1 = self.temp_points[0]
        p2 = self.temp_points[1]
        self.rubber_band.addPoint(p1)
        self.rubber_band.addPoint(QgsPointXY(p2.x(), p1.y()))
        self.rubber_band.addPoint(p2)
        self.rubber_band.addPoint(QgsPointXY(p1.x(), p2.y()))
        self.rubber_band.addPoint(QgsPointXY(p1.x(), p2.y()))
        self.rubber_band.addPoint(p1)
        self.rubber_band.show()

    def clear(self):
        super().clear()
        self.rubber_band.hide()

"""
    圆形绘制工具
"""

class CircleDrawTool(BaseDrawTool):
    def __init__(self, canvas, draw_manager=None, is_buffer=False, distance=0.0):
        super().__init__(canvas, is_buffer=is_buffer, distance=distance)
        self.canvas = canvas
        self.draw_manager = draw_manager
        self.temp_points = draw_manager.temp_points if draw_manager is not None else []
        self.rubber_band = draw_manager.rubber_band if draw_manager is not None else None
        self.radius = 0
        if self.rubber_band is None:
            self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
            self.rubber_band.setColor(Qt.red)
            self.rubber_band.setWidth(2)
            self.rubber_band.setFillColor(QColor(255, 0, 0, 50))

    def canvasMoveEvent(self, event):
        try:
            if len(self.temp_points) < 1:
                return
            point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())
            self.radius = math.sqrt(
                (point.x() - self.temp_points[0].x()) ** 2 + (point.y() - self.temp_points[0].y()) ** 2)
            circle = QgsCircle.fromCenterDiameter(QgsPoint(self.temp_points[0].x(),self.temp_points[0].y()), self.radius*2)
            self.geom = QgsGeometry(circle.toPolygon(segments=72))
            self.update_rubber_band()
        except Exception as e:
            print(e)
    def canvasPressEvent(self, event):
        if event.button() == Qt.LeftButton:
            point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())

            if len(self.temp_points) == 0:
                self.temp_points.append(QgsPointXY(point.x(), point.y()))
            else:
                self.temp_points[0] = QgsPointXY(point.x(), point.y())
        elif event.button() == Qt.RightButton:
            if self.draw_manager is not None:
                self.draw_manager.finish_draw(self.geom)
            else:
                self.temp_points.clear()
                self.finish_draw()

            # self.clear()
    def update_rubber_band(self):
        self.rubber_band.setToGeometry(QgsGeometry(self.geom), None)


    def clear(self):
        super().clear()
        self.rubber_band.hide()
