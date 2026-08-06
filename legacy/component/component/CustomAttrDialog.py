from PyQt5.QtCore import QVariant, Qt
from PyQt5.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QDialogButtonBox,
    QWidget, QVBoxLayout, QLabel, QScrollArea, QSizePolicy,
    QHBoxLayout  # 新增：用于按钮居右
)
from qgis.core import QgsVectorLayer, QgsField


class CustomAttrDialog(QDialog):
    """自定义属性对话框（修复setAlignment报错+匹配目标样式）"""
    def __init__(self, layer: QgsVectorLayer, feature=None, parent=None):
        super().__init__(parent)
        self.layer = layer
        self.feature = feature
        self.attr_values = {}  # 存储输入框引用
        self._load_qgis_style()  # 加载样式
        self.init_ui()

    def _load_qgis_style(self):
        """加载与目标截图一致的样式"""
        target_style = """
            /* 对话框全局：浅灰背景+紧凑布局 */
            QDialog {
                background-color: #f0f0f0;
                font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 12px;
            }
            /* 标签：左对齐+固定宽度 */
            QLabel {
                color: #222222;
                padding: 3px 0;
                min-width: 80px;  /* 标签固定宽度，对齐更整齐 */
            }
            /* 输入框：默认显示NULL+边框样式 */
            QLineEdit {
                padding: 2px 4px;
                border: 1px solid #b9b9b9;
                border-radius: 1px;
                background-color: #ffffff;
                font-size: 12px;
                color: #333333;
            }
            QLineEdit:focus {
                border-color: #4d90fe;
                outline: none;
            }
            /* 按钮组容器：底部边框 */
            QWidget#button_container {
                padding-top: 10px;
                margin-top: 5px;
            }
            /* OK/Cancel按钮样式 */
            QPushButton {
                padding: 3px 12px;
                border-radius: 2px;
                font-size: 12px;
            }
            QPushButton:default {
                background-color: #4d90fe;
                color: white;
                border: none;
            }
            QPushButton:default:hover {
                background-color: #3079ed;
            }
            QPushButton:not(:default) {
                background-color: #f5f5f5;
                color: #333333;
                border: 1px solid #b9b9b9;
            }
            QPushButton:not(:default):hover {
                background-color: #e8e8e8;
            }
            /* 滚动条样式（匹配目标截图） */
            QScrollBar:vertical {
                width: 12px;
                background: #f0f0f0;
                margin: 0;
            }


        """
        self.setStyleSheet(target_style)

    def init_ui(self):
        # ========== 窗口设置：固定宽400高600+标题匹配截图 ==========
        self.setWindowTitle(f"{self.layer.name()} - 要素属性")  # 标题格式：图层名-要素属性
        self.setModal(True)
        self.setFixedSize(500, 550)  # 固定宽高

        # ========== 主布局+滚动区域 ==========
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)  # 紧凑边距
        main_layout.setSpacing(5)

        # 滚动区域（承载表单）
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.NoFrame)  # 隐藏滚动区域边框
        main_layout.addWidget(scroll_area)

        # 表单容器
        form_widget = QWidget()
        scroll_area.setWidget(form_widget)

        # ========== 表单布局：标签左对齐+输入框占满右侧 ==========
        form_layout = QFormLayout(form_widget)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)  # 标签左对齐
        form_layout.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form_layout.setRowWrapPolicy(QFormLayout.DontWrapRows)

        # ========== 遍历字段创建控件：输入框默认显示NULL ==========
        for field in self.layer.fields():
            field_name = field.name()

            # 标签：显示字段名（左对齐）
            label = QLabel(field_name)
            label.setMinimumWidth(80)  # 标签固定最小宽度，对齐更整齐

            # 输入框：默认显示"NULL"
            line_edit = QLineEdit()
            line_edit.setText("NULL")  # 默认值NULL
            # 优化：焦点时清空NULL
            line_edit.focusInEvent = lambda e, le=line_edit: self._on_edit_focus(le, e)

            # 存储输入框引用（后续获取值时处理NULL）
            self.attr_values[field_name] = line_edit
            form_layout.addRow(label, line_edit)

        # ========== 按钮组：通过HBoxLayout实现居右（修复setAlignment报错） ==========
        # 创建按钮容器（用于加边框）
        button_container = QWidget()
        button_container.setObjectName("button_container")  # 绑定样式
        button_layout = QHBoxLayout(button_container)
        button_layout.setContentsMargins(5, 0, 0, 0)
        button_layout.setAlignment(Qt.AlignRight)  # 按钮居右

        # 创建按钮组
        button_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
            Qt.Horizontal
        )
        # 设置OK为默认按钮
        ok_btn = button_box.button(QDialogButtonBox.Ok)
        ok_btn.setDefault(True)

        # 按钮信号连接
        button_box.accepted.connect(self.save_edit)
        button_box.rejected.connect(self.reject)

        # 将按钮组添加到居右布局
        button_layout.addWidget(button_box)
        # 将按钮容器添加到主布局
        main_layout.addWidget(button_container)

    def save_edit(self):
        for key, value in self.attr_values.items():
            # 处理key和value
            self.feature.setAttribute(key, value.text())
        success = self.layer.addFeature(self.feature)
        if not success:
            print("要素添加失败！")
        self.close()
        # raise f"添加成功，要素{self.attr_values.get('OBJECTID')}"


    def _on_edit_focus(self, line_edit, event):
        """输入框获取焦点时清空NULL"""
        if line_edit.text() == "NULL":
            line_edit.clear()
        # 执行原生焦点事件
        QLineEdit.focusInEvent(line_edit, event)

    def get_attrs(self):
        """获取属性值：将"NULL"转为实际NULL值"""
        attrs = {}
        for field_name, line_edit in self.attr_values.items():
            value = line_edit.text().strip()
            # 若用户未修改（显示NULL），则设为None；否则取用户输入值
            attrs[field_name] = None if value == "NULL" else value
        return attrs

    def _get_layer_geom_type(self):
        geom_type = self.layer.geometryType()
        return {0: "点", 1: "线", 2: "面"}.get(geom_type, "要素")