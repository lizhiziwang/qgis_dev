"""工具条。

按 :class:`MainWindow` 注入的 callback 列表组装工具按钮。
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QAction, QMainWindow, QToolBar, QToolButton

from app.ui.common.icons import Icons
from app.ui.common.styles import BUTTON_TOOLBAR_QSS


class ToolBar:
    """主工具条构建器。"""

    def __init__(self, main_window: QMainWindow) -> None:
        self.main_window = main_window
        self.toolbar: Optional[QToolBar] = None
        self.draw_manager = None  # 由 MainWindow 注入

    def build(self, callbacks: dict[str, Callable[[], None]]) -> QToolBar:
        """构建工具条。

        :param callbacks: 回调字典，包括
            - pan / zoom_in / zoom_out
            - identify
            - quality (质检 dialog)
            - analyse (分析)
            - refresh
            - test
        """
        tb = QMainWindow.addToolBar(self.main_window, "地图工具")
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toolbar = tb

        # 1. 平移
        pan = QAction("平移", self.main_window)
        pan.triggered.connect(callbacks.get("pan", lambda: None))
        tb.addAction(pan)

        # 2. 识别（点击查询要素）
        identify = QAction("属性", self.main_window)
        identify.triggered.connect(callbacks.get("identify", lambda: None))
        tb.addAction(identify)

        # 3. 放大 / 缩小
        zoom_in = QAction("放大", self.main_window)
        zoom_in.triggered.connect(callbacks.get("zoom_in", lambda: None))
        tb.addAction(zoom_in)

        zoom_out = QAction("缩小", self.main_window)
        zoom_out.triggered.connect(callbacks.get("zoom_out", lambda: None))
        tb.addAction(zoom_out)

        # 4. 质检
        qc = QAction("质检", self.main_window)
        qc.triggered.connect(callbacks.get("quality", lambda: None))
        tb.addAction(qc)

        # 5. 分析
        an = QAction("分析", self.main_window)
        an.triggered.connect(callbacks.get("analyse", lambda: None))
        tb.addAction(an)

        # 6. 全图
        full = QAction("全图", self.main_window)
        full.triggered.connect(callbacks.get("zoom_full", lambda: None))
        tb.addAction(full)

        # 7. 刷新
        refresh = QAction("刷新", self.main_window)
        refresh.triggered.connect(callbacks.get("refresh", lambda: None))
        tb.addAction(refresh)

        # 8. 测试
        test = QAction("测试", self.main_window)
        test.triggered.connect(callbacks.get("test", lambda: None))
        tb.addAction(test)

        tb.setStyleSheet(BUTTON_TOOLBAR_QSS)
        return tb

    def add_space_query_buttons(self, callbacks: dict[str, Callable[[], None]]) -> None:
        """追加空间查询工具条（点 / 线 / 面 / 圆 / 框）。"""
        if self.toolbar is None:
            return
        for q_type, label in [
            ("point", "点查询"),
            ("line", "线查询"),
            ("polygon", "面查询"),
        ]:
            btn = QToolButton()
            btn.setText(label)
            btn.setPopupMode(QToolButton.InstantPopup)
            btn.clicked.connect(callbacks.get(f"space_query_{q_type}", lambda: None))
            self.toolbar.addWidget(btn)
