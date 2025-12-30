import string

from qgis._core import QgsWkbTypes, QgsMapLayer, QgsRectangle
from qgis._gui import QgsHighlight
from qgis.gui import (
    QgsMapToolIdentify,
)

from PyQt5.QtCore import QSize, QVariant
from PyQt5.QtGui import QFont, QColor
from PyQt5.QtWidgets import QMessageBox, QDialog, QTextEdit, \
    QAction, QMenu, QInputDialog

'''
    点击查询要素
'''


class FindFeatureTool(QgsMapToolIdentify):
    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.dialog = None
        self.highlight = None

    def _draw_highlight(self, layer, feature):
        """绘制高亮要素（无重绘，仅局部渲染）"""
        # 移除旧的高亮（避免叠加）
        if self.highlight:
            self.canvas.scene().removeItem(self.highlight)

        # 创建新的高亮对象
        self.highlight = QgsHighlight(self.canvas, feature, layer)

        # 自定义高亮样式（红色粗边框+半透明填充，可按需调整）
        self.highlight.setColor(QColor(255, 0, 0))  # 高亮颜色：红色
        self.highlight.setFillColor(QColor(255, 0, 0, 50))  # 填充色：半透明红
        self.highlight.setWidth(1)  # 边框宽度
        # self.highlight.setBuffer(1)  # 缓冲（点要素放大）

        # 显示高亮（直接添加到画布场景，无画布重绘）
        self.highlight.show()

    def deactivate(self):
        """工具停用后移除高亮"""
        super().deactivate()
        if self.highlight:
            self.canvas.scene().removeItem(self.highlight)
            self.highlight = None
        # 关闭弹窗（可选）
        if self.dialog:
            self.dialog.close()
            self.dialog = None

    def canvasReleaseEvent(self, event):
        # 使用内置的识别功能
        results = self.identify(event.x(), event.y(),
                                self.IdentifyMode.TopDownAll)

        if results:
            result = results[0]
            layer = result.mLayer
            feature = result.mFeature
            if feature:
                # self._update_highlight_layer(layer, feature=feature)
                self._draw_highlight(layer, feature)

            # 展示弹窗
            if self.dialog is None:
                self.dialog = ModernFeatureInfoDialog(self.canvas.parent(), layer, feature)
                self.dialog.setWindowModality(Qt.NonModal)
                # 不可交互
                # self.dialog.exec_()
                # 可交互
                self.dialog.show()
            else:
                self.dialog.show_feature(feature)
                self.dialog.show()


class ModernFeatureInfoDialog(QDialog):
    """现代化要素信息弹窗（支持富文本、复制功能、美化样式）"""

    def __init__(self, parent, layer, feature):
        super().__init__(parent)
        self.setWindowIcon(QIcon("resource/png/app_icon.png"))
        self.layer = layer
        self.feature = feature
        self.init_ui()  # 初始化界面
        self.set_style()  # 设置现代化样式

    def init_ui(self):
        """初始化弹窗布局和组件"""
        self.setWindowTitle("要素详情")
        self.setMinimumSize(500, 400)  # 最小尺寸
        self.setModal(True)  # 模态弹窗（阻止背景操作）
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # 1. 顶部标题栏（显示图层名和要素ID）
        title_layout = QHBoxLayout()
        # 图标
        icon_label = QLabel()
        icon_label.setPixmap(QIcon.fromTheme("layer-vector-point").pixmap(QSize(32, 32)))
        title_layout.addWidget(icon_label)
        # 标题文本
        title_text = QLabel(f"<h3 style='margin:0'>{self.layer.name()}</h3>"
                            f"<p style='margin:0; color:#666'>要素 ID: {self.feature.id()}</p>")
        title_layout.addWidget(title_text)
        title_layout.addStretch()  # 右对齐

        # 2. 要素信息展示区域（富文本 + 滚动）
        self.info_text = QTextEdit()
        self.info_text.setReadOnly(True)  # 只读
        self.info_text.setFont(QFont("Segoe UI", 10))  # 现代字体
        self.load_feature_info()  # 加载要素信息

        # 3. 底部按钮区域
        button_layout = QHBoxLayout()
        # 复制按钮
        copy_btn = QPushButton(QIcon.fromTheme("edit-copy"), "复制信息")
        copy_btn.clicked.connect(self.copy_info)
        button_layout.addWidget(copy_btn)
        # 关闭按钮
        close_btn = QPushButton(QIcon.fromTheme("window-close"), "关闭")
        close_btn.clicked.connect(self.close)
        button_layout.addWidget(close_btn)

        # 主布局
        main_layout = QVBoxLayout()
        main_layout.addLayout(title_layout)
        main_layout.addWidget(self.info_text, stretch=1)  # 占满剩余空间
        main_layout.addLayout(button_layout)
        self.setLayout(main_layout)

    def show_feature(self, feature):
        self.feature = feature
        self.load_feature_info()

    def load_feature_info(self):
        """加载要素信息（富文本格式，表格自适应对话框尺寸）"""
        info = "<div style='line-height:1.6; width:100%; box-sizing:border-box;'>"  # 外层容器100%宽度
        # 2. 属性信息
        info += f"<p><strong>属性字段（共 {len(self.layer.fields())} 个）：</strong></p>"
        # 关键优化：表格添加 width:100% + table-layout:fixed + word-wrap:break-word
        info += """
        <table style='width:100%; border-collapse:collapse; table-layout:auto;'>
            <colgroup>
                <col style='width: 35%;' />  <!-- 字段名列固定35%宽度 -->
                <col style='width: 65%;' />  <!-- 属性值列占剩余65%宽度 -->
            </colgroup>
        """
        info += f"""
            <tr style='background-color:"#f8f9fa";width=100%'>
                <td style='padding:8px; border-bottom:1px solid #eee; word-wrap:break-word; white-space:normal;'>
                    几何文本
                </td>
                <td style='padding:8px; border-bottom:1px solid #eee; word-wrap:break-word; white-space:normal;'>
                    {self.feature.geometry().asWkt()}
                </td>
            </tr>
        """
        for idx, (field, value) in enumerate(zip(self.layer.fields(), self.feature.attributes())):
            # 交替行颜色（增强可读性）
            bg_color = "#f8f9fa" if idx % 2 == 0 else "#ffffff"
            # 处理空值
            value_str = str(value) if value is not None else "<span style='color:#999'>无</span>"
            # 关键优化：td添加 word-wrap:break-word（自动换行）和 white-space:normal（允许换行）
            info += f"""
            <tr style='background-color:{bg_color};width=100%'>
                <td style='padding:8px; border-bottom:1px solid #eee; word-wrap:break-word; white-space:normal;'>
                    {field.name()}
                </td>
                <td style='padding:8px; border-bottom:1px solid #eee; word-wrap:break-word; white-space:normal;'>
                    {value_str}
                </td>
            </tr>
            """
        info += "</table></div>"
        self.info_text.setHtml(info)

        # 关键配置：允许QTextEdit水平滚动（字段过多时），同时禁用固定宽度
        self.info_text.setLineWrapMode(QTextEdit.NoWrap)  # 禁用文本自动换行（表格自适应）
        self.info_text.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)  # 必要时显示水平滚动条
        self.info_text.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)  # 必要时显示垂直滚动条

    def set_style(self):
        """设置现代化样式（圆角、阴影、配色）"""
        # 1. 弹窗样式（圆角 + 阴影）
        self.setStyleSheet("""
            QDialog {
                background-color: #ffffff;
                border-radius: 8px;
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.15);
                padding: 15px;
            }
            QPushButton {
                background-color: #2196F3;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 10pt;
                margin-left: 8px;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
            QPushButton#close_btn {
                background-color: #f5f5f5;
                color: #333;
            }
            QPushButton#close_btn:hover {
                background-color: #e0e0e0;
            }
            QTextEdit {
                border: 1px solid #e0e0e0;
                border-radius: 4px;
                padding: 10px;
                background-color: #fafafa;
            }
            QScrollArea {
                border: none;
            }
        """)

    def copy_info(self):
        """复制要素信息到剪贴板"""
        # 提取纯文本信息
        plain_info = f"图层: {self.layer.name()}\n要素ID: {self.feature.id()}\n"
        plain_info += f"坐标（EPSG:{self.layer.crs().authid()}）:\n"
        geom = self.feature.geometry()
        if geom.type() == QgsWkbTypes.PointGeometry:
            point = geom.asPointXY()
            plain_info += f"  X: {point.x():.6f}\n  Y: {point.y():.6f}\n"
        plain_info += "\n属性信息:\n"
        for field, value in zip(self.layer.fields(), self.feature.attributes()):
            value_str = str(value) if value is not None else "无"
            plain_info += f"  {field.name()}: {value_str}\n"

        # 复制到剪贴板
        clipboard = QApplication.clipboard()
        clipboard.setText(plain_info)

        # 显示复制成功提示（临时弹窗）
        success_dialog = QDialog(self)
        success_dialog.setWindowTitle("成功")
        success_dialog.setStyleSheet("""
            QDialog { border-radius: 4px; padding: 10px; background-color: #e8f5e9; }
            QLabel { color: #2e7d32; font-size: 10pt; }
        """)
        success_layout = QVBoxLayout()
        success_layout.addWidget(QLabel("✅ 要素信息已复制到剪贴板！"))
        success_dialog.setLayout(success_layout)
        success_dialog.setModal(True)
        success_dialog.show()


''' 图层编辑 '''


class LayerEditTool:
    def __init__(self, current_edit_layer):
        self.current_edit_layer = current_edit_layer
        self.dialog = None

    def start_editing(self) -> bool:
        layer = self.current_edit_layer
        if layer and layer.type() == QgsMapLayer.VectorLayer:
            return self.start_editing_layer(layer)
        else:
            print(f"找不到ID为 {layer.getId()} 的矢量图层")
            return False

    def start_editing_layer(self, layer):
        """开始编辑图层"""
        if layer.isEditable():
            print("图层已经在编辑模式")
            return True

        # 开始编辑
        if layer.startEditing():
            print(f"成功开始编辑图层: {layer.name()}")
            return True
        else:
            print(f"无法开始编辑图层: {layer.name()}")
            return False

    def stop_editing_layer(self, layer, save_changes=True):
        """停止编辑图层"""
        if not layer.isEditable():
            print("图层不在编辑模式")
            return True

        if save_changes:
            # 保存更改
            if layer.commitChanges():
                print(f"成功保存并停止编辑: {layer.name()}")
                return True
            else:
                print(f"保存失败: {layer.name()}")
                # 回滚更改
                layer.rollBack()
                return False
        else:
            # 不保存，回滚
            layer.rollBack()
            print(f"已回滚并停止编辑: {layer.name()}")
            return True


'''
  打开属性表  
'''
from PyQt5.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QTableView,
    QHeaderView, QLabel, QPushButton, QSpinBox, QComboBox,
    QAbstractItemView, QApplication, QSizePolicy, QProgressDialog
)
from PyQt5.QtCore import Qt, QAbstractTableModel, QModelIndex, QThread, pyqtSignal
from qgis.core import QgsFeatureRequest
import math
from PyQt5.QtGui import QIcon  # 添加缺失的导入


class FeatureLoaderThread(QThread):
    """加载要素的后台线程"""
    progress_updated = pyqtSignal(int)
    features_loaded = pyqtSignal(list)
    finished = pyqtSignal()

    def __init__(self, layer, filter_str, parent=None):
        super().__init__(parent)
        self.layer = layer
        self.all_ids = []
        self.filter_str = filter_str

    def run(self):
        """在后台线程中加载要素ID"""
        try:
            total_features = self.layer.featureCount()
            if total_features == 0:
                self.features_loaded.emit([])
                return

            step = max(1, total_features // 100)  # 每1%更新一次进度

            all_ids = []
            count = 0
            request = QgsFeatureRequest().setFilterExpression(self.filter_str)

            for feature in self.layer.getFeatures(request):
                all_ids.append(feature.id())
                count += 1

                # 更新进度
                if count % step == 0 or count == total_features:
                    progress = int((count / total_features) * 100)
                    self.progress_updated.emit(progress)

            self.all_ids = sorted(all_ids)  # 排序ID
            self.features_loaded.emit(self.all_ids)

        except Exception as e:
            print(f"加载要素时出错: {e}")
        finally:
            self.finished.emit()


class PagedAttributeTableModel(QAbstractTableModel):
    """分页属性表模型 - 带进度提示"""

    def __init__(self, layer, page_size=50, parent=None, filter_str='1=1'):
        super().__init__(parent)
        self.layer = layer
        self.page_size = page_size
        self.current_page = 0
        self.order_field = "id"
        self.filter_str = filter_str

        # 预先获取所有要素ID
        self.all_ids = None
        self.features = []
        self.field_names = [field.name() for field in layer.fields()]

        # 初始化加载状态
        self.is_loading = True
        self.total_features = 0
        self.total_pages = 0

        # 创建进度对话框（非模态）
        self.loading_progress = QProgressDialog("正在加载图层数据...", "取消", 0, 100)
        self.loading_progress.setWindowModality(Qt.NonModal)
        self.loading_progress.setWindowTitle("加载中...")
        self.loading_progress.setMinimumDuration(0)
        self.loading_progress.setValue(0)
        self.loading_progress.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.Dialog)
        self.loading_progress.setWindowIcon(QIcon("resource/png/app_icon.png"))  # 注释掉可能缺失的图标

        # 连接取消信号
        self.loading_progress.canceled.connect(self.on_cancel_loading)

        # 启动后台线程加载ID
        self.loading_thread = FeatureLoaderThread(layer, self.filter_str)
        self.loading_thread.progress_updated.connect(self.on_progress_updated)
        self.loading_thread.features_loaded.connect(self.on_features_loaded)
        self.loading_thread.finished.connect(self.on_loading_finished)

        # 显示进度对话框
        self.loading_progress.show()

        # 启动线程
        self.loading_thread.start()

    def on_progress_updated(self, progress):
        """进度更新"""
        if self.loading_progress:
            self.loading_progress.setValue(progress)
            QApplication.processEvents()

    def on_features_loaded(self, all_ids):
        """要素加载完成"""
        if not self.loading_progress.wasCanceled():
            self.all_ids = all_ids
            self.total_features = len(self.all_ids)
            self.total_pages = math.ceil(self.total_features / self.page_size) if self.total_features > 0 else 1

            # 加载第一页数据
            self.load_page(0)

    def on_cancel_loading(self):
        """取消加载"""
        self.is_loading = False

    def on_loading_finished(self):
        """加载完成"""
        self.close_progress_dialog()
        self.load_page(self.current_page)

    def close_progress_dialog(self):
        """关闭进度对话框"""
        self.is_loading = False
        if self.loading_progress:
            self.loading_progress.close()
            self.loading_progress = None

    def load_page(self, page):
        """加载指定页面的数据"""
        if self.is_loading or self.all_ids is None:
            self.beginResetModel()
            self.features = []
            self.endResetModel()
            return

        if page < 0 or page >= self.total_pages:
            return

        self.current_page = page
        start_idx = page * self.page_size
        end_idx = min(start_idx + self.page_size, self.total_features)

        # 获取当前页的要素ID
        page_ids = self.all_ids[start_idx:end_idx]

        # 清空现有数据
        self.beginResetModel()
        self.features = []

        if page_ids:
            request = QgsFeatureRequest()
            request.setFilterFids(page_ids)
            request.setFilterExpression(self.filter_str)

            for feature in self.layer.getFeatures(request):
                self.features.append(feature)

        self.endResetModel()

    def set_page_size(self, new_page_size):
        """设置新的页大小"""
        if self.is_loading:
            return

        if self.page_size != new_page_size and new_page_size > 0:
            old_page = self.current_page
            self.page_size = new_page_size
            self.total_pages = math.ceil(self.total_features / new_page_size) if self.total_features > 0 else 1

            new_page = min(old_page, self.total_pages - 1) if self.total_pages > 0 else 0
            self.current_page = new_page
            self.load_page(new_page)

    def get_total_pages(self):
        if self.is_loading:
            return 0
        return self.total_pages

    def get_current_page(self):
        if self.is_loading:
            return 0
        return self.current_page

    def get_total_features(self):
        if self.is_loading:
            return 0
        return self.total_features

    def get_feature_by_row(self, row):
        """根据行号获取要素"""
        if 0 <= row < len(self.features):
            return self.features[row]
        return None

    def get_features_by_rows(self, rows):
        """根据行号列表获取要素列表"""
        features = []
        for row in rows:
            if 0 <= row < len(self.features):
                features.append(self.features[row])
        return features

    def rowCount(self, parent=QModelIndex()):
        return len(self.features)

    def columnCount(self, parent=QModelIndex()):
        if self.is_loading:
            return 0
        return len(self.field_names) + 1

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or self.is_loading:
            return None

        row = index.row()
        col = index.column()

        if row >= len(self.features) or row < 0:
            return None

        feature = self.features[row]

        if role == Qt.DisplayRole or role == Qt.EditRole:
            if col == 0:
                return feature.id()
            else:
                field_idx = col - 1
                if field_idx < len(feature.fields()):
                    return feature.attribute(field_idx)

        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if self.is_loading or orientation == Qt.Vertical:
            return None

        if role == Qt.DisplayRole:
            if orientation == Qt.Horizontal:
                if section == 0:
                    return "ID"
                elif section - 1 < len(self.field_names):
                    return self.field_names[section - 1]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemIsEnabled

        # 第0列是ID，不可编辑；其他列如果图层可编辑，则可编辑
        if index.column() == 0:
            return Qt.ItemIsEnabled | Qt.ItemIsSelectable
        else:
            if self.layer.isEditable():
                return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable
            else:
                return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid() or role != Qt.EditRole:
            return False

        if index.column() == 0:
            return False  # ID 列不可编辑

        row = index.row()
        col = index.column() - 1  # 跳过ID列

        if row >= len(self.features):
            return False

        feature = self.features[row]
        field = self.layer.fields().at(col)

        # 类型转换（可选，但建议）
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
        # 其他类型可按需处理

        # 修改属性值
        fid = feature.id()
        field_name = field.name()

        # 注意：必须使用 layer.changeAttributeValue，不能直接改 feature
        success = self.layer.changeAttributeValue(fid, col, value)
        if success:
            # 同步更新本地缓存（可选，但推荐以保持视图一致）
            self.features[row].setAttribute(col, value)
            self.dataChanged.emit(index, index, [role])
            return True
        return False


class PagedAttributeTableWidget(QWidget):
    """可停靠的属性表小部件"""

    zoom_to_map = pyqtSignal(str, object)

    def __init__(self, layer, page_size=50, parent=None, filter_str: string = "1=1"):
        super().__init__(parent)
        self.layer = layer
        self.page_size = page_size
        self.model = None
        self.table_view = None
        self.filter_str = filter_str

        # 设置小部件的大小策略
        size_policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.setSizePolicy(size_policy)

        self.setup_ui()

    def setup_ui(self):
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)  # 移除边距
        main_layout.setSpacing(2)  # 减小间距

        # 创建表格视图
        self.table_view = QTableView()
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setEditTriggers(QTableView.DoubleClicked)

        # 设置表格大小策略
        table_size_policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.table_view.setSizePolicy(table_size_policy)

        # 设置表头
        header = self.table_view.horizontalHeader()
        header.setMinimumSectionSize(20)  # 设置最小列宽为20像素
        header.setMaximumSectionSize(400)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(False)

        self.table_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table_view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        # 连接右键菜单事件
        self.table_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self.show_context_menu)

        # 创建分页控件
        pagination_widget = self.create_pagination_controls()

        main_layout.addWidget(self.table_view)
        main_layout.addWidget(pagination_widget)
        self.setLayout(main_layout)

        # 初始化模型
        self.model = PagedAttributeTableModel(self.layer, self.page_size, filter_str=self.filter_str)
        self.table_view.setModel(self.model)

        # 连接信号
        self.model.loading_thread.finished.connect(self.update_pagination_info)

    # 表格右键菜单
    def show_context_menu(self, position):
        """显示右键菜单"""
        if not self.model or self.model.is_loading:
            return

        # 获取被点击的行
        index = self.table_view.indexAt(position)
        if not index.isValid():
            return

        # 获取选中的行
        selected_rows = [index.row() for index in self.table_view.selectionModel().selectedRows()]
        if not selected_rows:
            selected_rows = [index.row()]

        # 获取选中的要素
        selected_features = self.model.get_features_by_rows(selected_rows)

        if not selected_features:
            return

        # 创建右键菜单
        menu = QMenu(self)

        # 单选操作
        if len(selected_features) == 1:
            feature = selected_features[0]

            # 查看要素信息
            view_info_action = QAction(f"查看要素信息 (ID: {feature.id()})", self)
            view_info_action.triggered.connect(lambda: self.view_feature_info(feature))
            menu.addAction(view_info_action)

            # 缩放到要素
            zoom_to_action = QAction("缩放到要素", self)
            zoom_to_action.triggered.connect(lambda: self.zoom_to_feature(feature))
            menu.addAction(zoom_to_action)

            # 选中要素
            select_action = QAction("选中要素", self)
            select_action.triggered.connect(lambda: self.select_feature(feature))
            menu.addAction(select_action)

            # 判断要素所在的图层是否可编辑，编辑状态下可删除数据
            delete_action = QAction("删除", self)
            delete_action.setEnabled(self.layer.isEditable())
            delete_action.triggered.connect(lambda: self.layer.deleteFeature(fid=feature.id()))
            menu.addAction(delete_action)

            menu.addSeparator()

        # 多选操作
        # 缩放到选中要素
        zoom_to_selected_action = QAction(f"缩放到选中要素 ({len(selected_features)}个)", self)
        zoom_to_selected_action.triggered.connect(lambda: self.zoom_to_selected_features(selected_features))
        menu.addAction(zoom_to_selected_action)

        # 选中多个要素
        select_multiple_action = QAction(f"选中这 {len(selected_features)} 个要素", self)
        select_multiple_action.triggered.connect(lambda: self.select_multiple_features(selected_features))
        menu.addAction(select_multiple_action)

        # 导出选中要素
        export_action = QAction(f"导出选中要素 ({len(selected_features)}个)", self)
        export_action.triggered.connect(lambda: self.export_selected_features(selected_features))
        menu.addAction(export_action)

        # 统计信息
        stats_action = QAction(f"统计信息 ({len(selected_features)}个)", self)
        stats_action.triggered.connect(lambda: self.show_statistics(selected_features))
        menu.addAction(stats_action)

        # 如果有多个要素选中，添加分隔符和批量操作
        if len(selected_features) > 1:
            menu.addSeparator()

            # 批量编辑（示例）
            batch_edit_action = QAction("批量编辑属性", self)
            batch_edit_action.triggered.connect(lambda: self.batch_edit_attributes(selected_features))
            menu.addAction(batch_edit_action)

        menu.exec_(self.table_view.viewport().mapToGlobal(position))

    def view_feature_info(self, feature):
        """查看要素信息"""
        info = f"要素ID: {feature.id()}\n"
        info += f"几何类型: {feature.geometry().type() if feature.hasGeometry() else '无几何'}\n"
        info += f"属性字段数: {feature.fields().count()}\n\n"

        info += "属性值:\n"
        for field in feature.fields():
            field_name = field.name()
            field_value = feature[field_name]
            info += f"{field_name}: {field_value}\n"

        QMessageBox.information(self, "要素信息", info)

    def zoom_to_feature(self, feature):
        """缩放到要素"""
        if feature.hasGeometry():

            geom = feature.geometry()
            if geom and not geom.isEmpty():
                # 这里需要访问QGIS主窗口的map canvas
                # 由于我们没有直接访问canvas，这里只是示例
                # 实际使用时需要传递canvas引用
                print(f"缩放到要素ID: {feature.id()}")
                data = {
                    "extent": geom.boundingBox(),
                    "id": [feature.id()]
                }
                self.zoom_to_map.emit("zoomTo", data)
                # canvas = iface.mapCanvas()  # 如果在QGIS插件中
                # canvas.setExtent(geom.boundingBox())
                # canvas.refresh()

    def select_feature(self, feature):
        """选中要素"""
        # 在QGIS中选中要素
        self.layer.selectByIds([feature.id()])
        print(f"已选中要素ID: {feature.id()}")

    def zoom_to_selected_features(self, features):
        """缩放到选中的多个要素"""
        feature_ids = [f.id() for f in features if f.hasGeometry()]
        if feature_ids:
            print(f"缩放到选中的 {len(feature_ids)} 个要素")
            # canvas = iface.mapCanvas()  # 如果在QGIS插件中
            # request = QgsFeatureRequest().setFilterFids(feature_ids)
            extent = QgsRectangle()
            # extent.setMinimal()
            for feat in features:
                if feat.hasGeometry():
                    extent.combineExtentWith(feat.geometry().boundingBox())

            data = {
                "extent": extent,
                "ids": feature_ids
            }

            self.zoom_to_map.emit("zoom_to_features", data)

    def select_multiple_features(self, features):
        """选中多个要素"""
        feature_ids = [f.id() for f in features]
        self.layer.selectByIds(feature_ids)
        print(f"已选中 {len(feature_ids)} 个要素")

    def export_selected_features(self, features):
        """导出选中要素"""
        if not features:
            return

        # 获取导出路径
        from PyQt5.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出要素",
            "",
            "GeoPackage (*.gpkg);;Shapefile (*.shp);;CSV (*.csv)"
        )

        if not file_path:
            return

        try:
            # 创建新图层
            crs = self.layer.crs()
            geom_type = self.layer.geometryType()

            # 根据文件扩展名确定导出格式
            if file_path.lower().endswith('.shp'):
                driver_name = 'ESRI Shapefile'
            elif file_path.lower().endswith('.gpkg'):
                driver_name = 'GPKG'
            else:
                driver_name = 'ESRI Shapefile'  # 默认

            # 获取选中的要素ID
            feature_ids = [f.id() for f in features]

            # 创建要素请求，只获取选中的要素
            request = QgsFeatureRequest().setFilterFids(feature_ids)

            # 这里是简化版本，实际需要使用QGIS的导出功能
            print(f"导出 {len(features)} 个要素到: {file_path}")

        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"导出过程中出现错误:\n{str(e)}")

    def show_statistics(self, features):
        """显示统计信息"""
        if not features:
            return

        stats_info = f"选中要素数量: {len(features)}\n"

        # 统计几何信息
        has_geom_count = sum(1 for f in features if f.hasGeometry())
        stats_info += f"有几何的要素: {has_geom_count}\n"
        stats_info += f"无几何的要素: {len(features) - has_geom_count}\n\n"

        # 如果有字段，统计字段信息
        if features and features[0].fields().count() > 0:
            stats_info += "字段统计:\n"
            first_feature = features[0]
            for i, field in enumerate(first_feature.fields()):
                field_name = field.name()
                # 统计该字段的值类型分布
                values = [f[field_name] for f in features]
                non_null_values = [v for v in values if v is not None and v != '']
                stats_info += f"{field_name}: {len(non_null_values)}/{len(values)} 有值\n"

        QMessageBox.information(self, "统计信息", stats_info)

    def batch_edit_attributes(self, features):
        """批量编辑属性"""
        if not features:
            return

        # 获取所有字段名
        field_names = [f.name() for f in features[0].fields()] if features else []

        if not field_names:
            QMessageBox.information(self, "批量编辑", "该图层没有属性字段")
            return

        # 让用户选择要编辑的字段
        field, ok = QInputDialog.getItem(
            self,
            "批量编辑",
            "选择要编辑的字段:",
            field_names,
            0,
            False
        )

        if not ok or not field:
            return

        # 获取新值
        new_value, ok = QInputDialog.getText(
            self,
            "批量编辑",
            f"输入 '{field}' 字段的新值:"
        )

        if not ok:
            return

        # 批量更新
        try:
            # 创建要素编辑器
            self.layer.startEditing()

            for feature in features:
                self.layer.changeAttributeValue(feature.id(), feature.fieldNameIndex(field), new_value)

            self.layer.commitChanges()
            QMessageBox.information(self, "批量编辑", f"成功更新 {len(features)} 个要素的 '{field}' 字段")

        except Exception as e:
            self.layer.rollBack()
            QMessageBox.critical(self, "批量编辑", f"更新失败:\n{str(e)}")

    def create_pagination_controls(self):
        """创建分页控件"""
        widget = QWidget()

        # 设置分页控件的大小策略
        pagination_size_policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        widget.setSizePolicy(pagination_size_policy)

        layout = QHBoxLayout()
        layout.setContentsMargins(5, 5, 5, 5)  # 减小边距
        layout.setSpacing(5)  # 减小组件间距

        # 页大小选择
        page_label = QLabel("每页显示:")
        page_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        layout.addWidget(page_label)

        self.page_size_combo = QComboBox()
        self.page_size_combo.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        page_sizes = [10, 25, 50, 100, 200]
        for size in page_sizes:
            self.page_size_combo.addItem(str(size), size)

        current_index = page_sizes.index(self.page_size) if self.page_size in page_sizes else 2
        self.page_size_combo.setCurrentIndex(current_index)
        self.page_size_combo.currentIndexChanged.connect(self.on_page_size_changed)

        layout.addWidget(self.page_size_combo)

        unit_label = QLabel("条")
        unit_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        layout.addWidget(unit_label)

        # 分页信息
        self.page_info_label = QLabel("正在加载...")
        self.page_info_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        layout.addWidget(self.page_info_label)

        # 导航按钮
        self.first_btn = QPushButton("首页")
        self.prev_btn = QPushButton("上一页")
        self.next_btn = QPushButton("下一页")
        self.last_btn = QPushButton("末页")

        # 设置按钮大小策略
        for btn in [self.first_btn, self.prev_btn, self.next_btn, self.last_btn]:
            btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            btn.setFixedWidth(50)  # 设置固定宽度

        self.first_btn.clicked.connect(self.go_to_first_page)
        self.prev_btn.clicked.connect(self.go_to_prev_page)
        self.next_btn.clicked.connect(self.go_to_next_page)
        self.last_btn.clicked.connect(self.go_to_last_page)

        # 初始禁用按钮
        self.first_btn.setEnabled(False)
        self.prev_btn.setEnabled(False)
        self.next_btn.setEnabled(False)
        self.last_btn.setEnabled(False)

        layout.addWidget(self.first_btn)
        layout.addWidget(self.prev_btn)
        layout.addWidget(self.next_btn)
        layout.addWidget(self.last_btn)

        # 当前页输入
        jump_label = QLabel("跳转到:")
        jump_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        layout.addWidget(jump_label)

        self.page_spinbox = QSpinBox()
        self.page_spinbox.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.page_spinbox.setMinimum(1)
        self.page_spinbox.setMaximum(1)
        self.page_spinbox.setValue(1)
        self.page_spinbox.setEnabled(False)
        self.page_spinbox.setFixedWidth(60)  # 设置固定宽度

        layout.addWidget(self.page_spinbox)

        page_unit_label = QLabel("页")
        page_unit_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        layout.addWidget(page_unit_label)

        # 刷新按钮
        refresh_btn = QPushButton("刷新")
        refresh_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        refresh_btn.setFixedWidth(50)  # 设置固定宽度
        refresh_btn.clicked.connect(self.refresh_data)
        layout.addWidget(refresh_btn)

        # 在末尾添加弹性空间，使控件紧凑排列
        layout.addStretch()

        widget.setLayout(layout)
        return widget

    def update_pagination_info(self):
        """更新分页信息"""
        if self.model and not self.model.is_loading:
            current_page = self.model.get_current_page() + 1
            total_pages = self.model.get_total_pages()
            total_features = self.model.get_total_features()

            start_idx = current_page * self.page_size - self.page_size + 1
            end_idx = min(current_page * self.page_size, total_features)

            info_text = f"第 {current_page}/{total_pages} 页，共 {total_features} 条记录"
            self.page_info_label.setText(info_text)

            # 更新页码输入框范围
            self.page_spinbox.setMaximum(total_pages)
            self.page_spinbox.setValue(current_page)
            self.page_spinbox.setEnabled(True)

            # 更新按钮状态
            self.first_btn.setEnabled(current_page > 1)
            self.prev_btn.setEnabled(current_page > 1)
            self.next_btn.setEnabled(current_page < total_pages)
            self.last_btn.setEnabled(current_page < total_pages)

    def on_page_size_changed(self):
        """页大小改变事件"""
        if self.model and not self.model.is_loading:
            new_page_size = self.page_size_combo.currentData()
            if new_page_size != self.page_size:
                self.page_size = new_page_size
                self.model.set_page_size(new_page_size)
                self.update_pagination_info()

    def on_page_changed(self, page):
        """页码改变事件"""
        if (self.model and not self.model.is_loading and
                1 <= page <= self.model.get_total_pages()):
            self.model.load_page(page - 1)
            self.update_pagination_info()

    def go_to_first_page(self):
        """跳转到首页"""
        if self.model and not self.model.is_loading:
            self.model.load_page(0)
            self.update_pagination_info()

    def go_to_prev_page(self):
        """跳转到上一页"""
        if self.model and not self.model.is_loading:
            current_page = self.model.get_current_page()
            if current_page > 0:
                self.model.load_page(current_page - 1)
                self.update_pagination_info()

    def go_to_next_page(self):
        """跳转到下一页"""
        if self.model and not self.model.is_loading:
            current_page = self.model.get_current_page()
            if current_page < self.model.get_total_pages() - 1:
                self.model.load_page(current_page + 1)
                self.update_pagination_info()

    def go_to_last_page(self):
        """跳转到末页"""
        if self.model and not self.model.is_loading:
            last_page = self.model.get_total_pages() - 1
            self.model.load_page(last_page)
            self.update_pagination_info()

    def refresh_data(self):
        """刷新数据"""
        if self.model and not self.model.is_loading:
            current_page = self.model.get_current_page()
            self.model.load_page(current_page)
            self.update_pagination_info()


class PagedAttributeDockWidget(QDockWidget):
    """可停靠的属性表停靠窗口"""

    def __init__(self, layer, title="属性表", page_size=50, parent=None, filter_str: string = "1=1"):
        super().__init__(title, parent)
        self.topParent = parent
        self.layer = layer

        # 设置停靠窗口的属性
        self.setFeatures(QDockWidget.AllDockWidgetFeatures)
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)

        # 创建内容小部件
        self.content_widget = PagedAttributeTableWidget(layer, page_size, self, filter_str=filter_str)
        self.setWidget(self.content_widget)
        # 连接地图事件
        self.content_widget.zoom_to_map.connect(self.zoom_to_map)

        # 设置最小尺寸为较小的值
        self.setMinimumSize(200, 100)  # 设置较小的最小尺寸

        # 设置窗口标题
        self.setWindowTitle(title)

    def closeEvent(self, event):
        """
        当组件关闭时，如果图层处于编辑状态，且还又编辑的内容未保存，则进行提示
        :return:
        """
        if self.layer.isEditable():
            self.topParent.draw_tool.stop_edit_fun()
        # 用户点击了 × 关闭 dock
        self.topParent.current_attribute_dock = None
        # self._user_closed = True
        self.setParent(None)  # 从父窗口解绑
        self.deleteLater()  # 稍后自动销毁（安全）
        event.accept()
        # 可以在这里安全清理（如果需要）
        super().closeEvent(event)

        # 连接关闭事件

    #     self.visibilityChanged.connect(self.on_visibility_changed)
    #
    # def on_visibility_changed(self, visible):
    #     """停靠窗口可见性变化"""
    #     if not visible:
    #         # 当隐藏时，可以做一些清理工作
    #         self.content_widget.model.all_ids = None
    #         self.content_widget.model.features = None
    #         self.topParent.current_attribute_dock = None
    #         self.destroy()
    #
    #         pass

    def add_new_layer(self, layer, parent, page_size=50, title="属性表"):
        self.content_widget = PagedAttributeTableWidget(layer, page_size, self)
        self.setWidget(self.content_widget)
        self.setWindowTitle(title)

    def zoom_to_map(self, action_type, data):
        if action_type == "zoomTo":
            self.topParent.selected_layer.selectByIds(data["id"])
            self.topParent.zoom_to_layer_extent_transform(extent=data["extent"]
                                                          , source_crs=self.topParent.selected_layer.crs())
        if action_type == "selectFeature":
            self.topParent.selected_layer.selectByIds(data["id"])
        if action_type == "zoom_to_features":
            self.topParent.canvas.setExtent(data["extent"])
            self.topParent.selected_layer.selectByIds(data["ids"])
            self.topParent.canvas.refresh()
