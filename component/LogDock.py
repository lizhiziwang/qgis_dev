from PyQt5.QtCore import Qt
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import QDockWidget, QHBoxLayout, QTextBrowser, QWidget, QVBoxLayout, QAction, QToolBar, \
    QPushButton, QSpacerItem, QSizePolicy, QLineEdit, QComboBox, QMessageBox
from component.Logger import  *
from component.file import open_file_dir, write_data
from datetime import datetime

logger = setup_logger(__name__)


class LogDock(QDockWidget):
    def __init__(self, parent=None):
        super(LogDock, self).__init__("日志信息", parent)
        self.txt = None
        self.button_bar_widget = None  # 按钮栏容器
        self.auto_scroll_flag = True  # 自动滚动标记
        self.setAllowedAreas(Qt.BottomDockWidgetArea)
        self.param = {
                "size": 500,
                "level": "ALL", # ALL INFO DEBUG ERROR WARING
                "filter":""
            }
        self.init_ui()
        self.__load_log_file__()


    def init_ui(self):
        main_container_widget = QWidget(self)
        main_layout = QVBoxLayout(main_container_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)  # 按钮栏与文本框无间距

        self.create_button_bar()
        # 将按钮栏添加到垂直布局上方
        main_layout.addWidget(self.button_bar_widget)

        # 3. 原有QTextBrowser配置（保留不变）
        self.txt = QTextBrowser(self)
        qss_style = """
               QTextBrowser {
                   font-family: "微软雅黑", "宋体", sans-serif;
                   font-size: 14px;
                   color: #2c3e50;
                   padding: 10px;
                   border: 1px solid #dee2e6; /* 与按钮栏分隔 */
                   selection-background-color: #3498db;
                   selection-color: white;
               }
               QTextBrowser:hover {
                   border-color: #95a5a6;
               }
           """
        self.txt.setStyleSheet(qss_style)

        # 文本框容器（保留原有水平布局）
        text_container_widget = QWidget(self)
        qh = QHBoxLayout(text_container_widget)
        qh.setContentsMargins(0, 0, 0, 0)
        qh.setSpacing(0)
        qh.addWidget(self.txt)

        # 将文本框容器添加到垂直布局下方（占剩余空间）
        main_layout.addWidget(text_container_widget, stretch=1)

        # 设置主容器为DockWidget内容
        self.setWidget(main_container_widget)

    def create_button_bar(self):
        """创建普通按钮栏（替换ToolBar，使用QPushButton）"""
        # 按钮栏容器
        self.button_bar_widget = QWidget(self)
        # 水平布局：承载所有普通按钮（横向排列）
        button_layout = QHBoxLayout(self.button_bar_widget)
        self.search_line = QLineEdit(self)
        self.search_line.setPlaceholderText("查询文本")
        button_layout.addWidget(self.search_line)
        self.level = QComboBox(self)
        self.level.addItems(["ALL", "INFO", "DEBUG", "ERROR", "WARNING"])
        button_layout.addWidget(self.level)
        self.level.setCurrentIndex(0)

        self.size_line = QComboBox(self)
        self.size_line.addItems(["50", "100", "500", "1000", "无限制"])
        button_layout.addWidget(self.size_line)
        self.size_line.setCurrentIndex(2)

        query_but = QPushButton("查询" ,self)
        query_but.clicked.connect(self.search)
        button_layout.addWidget(query_but)

        # 弹性占位符：将后续按钮推到右侧（可选优化）
        spacer = QSpacerItem(40, 20, QSizePolicy.Expanding, QSizePolicy.Minimum)
        button_layout.addSpacerItem(spacer)

        # 1. 清空日志按钮
        clear_btn = QPushButton("清空日志", self)
        clear_btn.setToolTip("清空当前所有日志内容")
        clear_btn.clicked.connect(self.clear_log)
        button_layout.addWidget(clear_btn)

        # 2. 刷新日志按钮
        refresh_btn = QPushButton("刷新日志", self)
        refresh_btn.setToolTip("重新加载日志文件")
        refresh_btn.clicked.connect(self.refresh_log)
        button_layout.addWidget(refresh_btn)

        # 3. 保存日志按钮
        save_btn = QPushButton("保存日志", self)
        save_btn.setToolTip("将当前日志保存到本地文件")
        save_btn.clicked.connect(self.save_log)
        button_layout.addWidget(save_btn)

        # 添加水平分隔符（可选：将按钮分组，右侧放自动滚动开关）
        button_layout.addSpacing(10)  # 固定间距

        # 4. 自动滚动开关按钮（可勾选，替代原Action）
        self.auto_scroll_btn = QPushButton("自动滚动", self)
        self.auto_scroll_btn.setCheckable(True)  # 设置为可勾选状态
        self.auto_scroll_btn.setChecked(True)  # 默认勾选
        self.auto_scroll_btn.setToolTip("是否自动滚动到最新日志")
        self.auto_scroll_btn.clicked.connect(self.toggle_auto_scroll)
        button_layout.addWidget(self.auto_scroll_btn)

        # 设置按钮之间的间距
        button_layout.setSpacing(8)
        button_layout.setContentsMargins(5, 5, 5, 5)

    def __load_log_file__(self, param=None):
        if not param:
            param = {
                "size": 500,
                "level": "ALL", # ALL INFO DEBUG ERROR WARING
                "filter":""
            }
        path = "logs/app.log"
        all_lines = []
        with open(path, "r", encoding="utf-8") as f:
            all_lines = [log_to_html(line.strip()) for line in f.readlines()
                         if param["level"] == "ALL" or f"__{param["level"]}__" in line]
        # 根据文本过滤
        if param["filter"]:
            all_lines = [line for line in all_lines if param["filter"] in line ]
        if param["size"] != -1:
            all_lines = all_lines[-param["size"] if len(all_lines)>param["size"]  else -len(all_lines):]
        self.txt.setText("\n".join(all_lines))
        self.txt.moveCursor(QTextCursor.End)

    def clear_log(self):
        m_box = QMessageBox(self)
        m_box.setWindowTitle("日志")
        m_box.setText("是否清除当前日志信息")
        m_box.setIcon(QMessageBox.Warning)
        m_box.addButton(QMessageBox.Ok)
        m_box.addButton(QMessageBox.Cancel)

        ok = m_box.exec_()
        if ok == QMessageBox.Ok:
            write_data("logs/app.log", "")
            self.refresh_log()

    def refresh_log(self):
        self.__load_log_file__(self.param)

    def save_log(self):
        path = open_file_dir(self, is_dir=True)
        file_name = f"{datetime.now().strftime("%Y%m%d%H%M%S")}.log"
        path = os.path.join(path, file_name)
        flag = write_data(path, self.txt.toPlainText())
        m_box = QMessageBox(self)
        m_box.setWindowTitle("保存结果")
        if flag:
            # 保存成功
            m_box.setIcon(QMessageBox.Information)  # 信息图标（蓝色i）
            success_text = f"日志保存成功！\n文件路径：\n{path}"
            m_box.setText(success_text)
        else:
            # 保存失败
            m_box.setIcon(QMessageBox.Critical)  # 错误图标（红色叉）
            fail_text = f"日志保存失败！\n请检查目录写入权限或文件路径是否有效。\n目标路径：\n{path}"
            m_box.setText(fail_text)

            # 6. 添加确认按钮并显示弹窗（阻塞式，用户点击后关闭）
        m_box.addButton(QMessageBox.Ok)
        m_box.exec_()


    def search(self):
        var1 = self.search_line.text()
        var2 = self.size_line.currentText()
        var3 = self.level.currentText()
        self.param["level"] = var3
        self.param["filter"] = var1
        self.param["size"] = int(var2) if var2 is not "无限制" else -1
        self.refresh_log()

    def toggle_auto_scroll(self):
        pass
def add_log_dock(parent=None):
    try:
        log = LogDock(parent)
        parent.addDockWidget(Qt.BottomDockWidgetArea, log)
    except Exception as e:
        logger.error(e)


