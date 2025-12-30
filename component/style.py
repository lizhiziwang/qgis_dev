import os

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton

from .check_dia import BaseDialog
from .file import *


class StyleSaveDia(BaseDialog):
    def __init__(self, layer, parent):
        super().__init__(parent, "保存样式")
        self.layer = layer
        self.set_ui()

    def set_ui(self):
        self.setMinimumSize(400, 300)

        # 主布局
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(20, 20, 20, 20)  # 外边距
        main_layout.setSpacing(15)  # 布局间距

        # === 文件名 ===
        file_layout = QHBoxLayout()
        file_layout.setSpacing(10)
        label1 = QLabel("文件名：")
        label1.setMinimumWidth(60)
        file_name_input = QLineEdit()
        file_name_input.setPlaceholderText("请输入文件名")
        file_layout.addWidget(label1)
        file_layout.addWidget(file_name_input)

        # === 文件路径 ===
        file_path_layout = QHBoxLayout()
        file_path_layout.setSpacing(10)
        label2 = QLabel("文件路径：")
        label2.setMinimumWidth(60)
        file_path_input = QLineEdit()
        file_path_input.setPlaceholderText("点击“选择文件”或手动输入路径")
        select_file_button = QPushButton("选择文件")
        select_file_button.clicked.connect(lambda: self.open_dir(file_path_input))
        select_file_button.setFixedWidth(80)
        file_path_layout.addWidget(label2)
        file_path_layout.addWidget(file_path_input)
        file_path_layout.addWidget(select_file_button)

        # 添加到主布局
        main_layout.addLayout(file_layout)
        main_layout.addLayout(file_path_layout)
        main_layout.addStretch()  # 上方控件靠上

        # === 底部按钮 ===
        button_layout = QHBoxLayout()
        confirm_button = QPushButton("保存")

        confirm_button.clicked.connect(lambda: self.save(file_name_input, file_path_input))

        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(lambda: self.close())

        # 统一按钮大小（可选）
        button_height = 32
        confirm_button.setFixedHeight(button_height)
        cancel_button.setFixedHeight(button_height)
        confirm_button.setMinimumWidth(80)
        cancel_button.setMinimumWidth(80)

        button_layout.addStretch()
        button_layout.addWidget(confirm_button)
        button_layout.addWidget(cancel_button)
        button_layout.setSpacing(10)

        main_layout.addLayout(button_layout)

        self.setLayout(main_layout)

        # === 应用统一的 QSS 样式表 ===
        self.setStyleSheet("""
            QDialog {
                font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 10pt;
            }
            QLabel {
                font-weight: bold;
                color: #333;
            }
            QLineEdit {
                padding: 6px;
                border: 1px solid #ccc;
                border-radius: 4px;
                background: #fff;
            }
            QLineEdit:focus {
                border: 1px solid #4A90E2;
                outline: none;
            }
            QPushButton {
                padding: 6px 12px;
                border: 1px solid #ccc;
                border-radius: 4px;
                background-color: #f5f5f5;
                color: #333;
            }
            QPushButton:hover {
                background-color: #e5e5e5;
                border-color: #999;
            }
            QPushButton:pressed {
                background-color: #ddd;
            }
            QPushButton#confirm_button {
                background-color: #4A90E2;
                color: white;
                border: 1px solid #357ABD;
            }
            QPushButton#confirm_button:hover {
                background-color: #357ABD;
            }
            QPushButton#confirm_button:pressed {
                background-color: #2A65A0;
            }
        """)

        # 给确认按钮设置 objectName，以便单独样式
        confirm_button.setObjectName("confirm_button")

    def save(self, name, path):
        name = name.text()
        path = path.text()

        self.layer.styleManager().addStyleFromLayer(name)
        style_obj = self.layer.styleManager().style(name)
        xml = style_obj.xmlData()
        write_data(path + os.sep + name + ".qml", xml)
        self.close()

    def open_dir(self, file_path_input):
        path = open_file_dir(self, True)
        print(path)
        file_path_input.setText(path)


class StyleOpenDia(BaseDialog):
    def __init__(self, layer, parent=None):
        super().__init__(parent, "打开样式文件")
        self.style_path = None
        self.layer = layer
        self.set_ui()

    def set_ui(self):
        self.setMinimumSize(500, 150)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        # 文件选择行
        file_layout = QHBoxLayout()
        label = QLabel("选择 QML 文件：")
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        file_path_input = QLineEdit()
        file_path_input.setPlaceholderText("点击“选择文件”或手动输入 .qml 文件路径")
        # self.file_path_input.textChanged.connect(self.on_path_changed)

        select_file_button = QPushButton("选择文件…")
        select_file_button.clicked.connect(lambda: self.open_style(file_path_input))

        # 设置 QLineEdit 占比更大
        file_layout.addWidget(label, 0)  # 固定宽度标签
        file_layout.addWidget(file_path_input, 1)  # 伸缩
        file_layout.addWidget(select_file_button, 0)  # 固定宽度按钮

        main_layout.addLayout(file_layout)

        # 按钮行
        button_layout = QHBoxLayout()
        confirm_button = QPushButton("加载")
        confirm_button.setDefault(True)
        # confirm_button.setEnabled(False)  # 初始禁用
        confirm_parent = QPushButton("取消")
        confirm_parent.clicked.connect(lambda: self.close())  # 使用 QDialog 标准方式

        button_layout.addStretch()
        button_layout.addWidget(confirm_button)
        button_layout.addWidget(confirm_parent)

        main_layout.addLayout(button_layout)
        self.setLayout(main_layout)

        # 连接确认按钮（路径有效时才应能加载）
        confirm_button.clicked.connect(self.load_style)

    def open_style(self, var1: QLineEdit) -> None:
        self.style_path = open_file_dir(self, False, filter="*.qml")
        var1.setText(self.style_path)

    def load_style(self) -> None:
        if self.style_path is None:
            return
        self.layer.loadNamedStyle(self.style_path)
        self.close()
