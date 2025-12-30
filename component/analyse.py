"""
分析工具
"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import  QDockWidget


class AnalysePanel(QDockWidget):
    def __init__(self, parent):
        super().__init__("分析工具", parent)

        self.topParent = parent

        # 设置停靠窗口的属性
        self.setFeatures(QDockWidget.AllDockWidgetFeatures)
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)

    def set_ui(self):
        pass

