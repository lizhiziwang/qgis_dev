"""主菜单栏。

只负责组装菜单项，业务逻辑（打开 dialog、切换工具）由 :class:`MainWindow`
通过 callback 注入。
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtGui import QKeySequence
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMainWindow, QMenu, QMenuBar


class MenuBar:
    """主菜单栏构建器。"""

    def __init__(
        self,
        main_window: QMainWindow,
        menubar: Optional[QMenuBar] = None,
    ) -> None:
        self.main_window = main_window
        # 无边框标题栏（方案 A）由 MainWindow 手动创建菜单栏并传进来；
        # 不传时退回 ``main_window.menuBar()``（旧调用方）。
        self.menubar = menubar if menubar is not None else main_window.menuBar()

    def build(self, callbacks: dict[str, Callable[[], None]]) -> QMenu:
        """构建菜单。

        :param callbacks: 由调用方注入。文件菜单用到 ``new_project`` /
            ``open_project`` / ``open_file``，查看菜单用到 ``show_log``。
        """
        file_menu = self._build_file_menu(callbacks)
        check_menu = self._build_check_menu(callbacks)
        return file_menu

    # ---------------------------------------------------------- 各菜单

    def _build_file_menu(self, callbacks: dict[str, Callable[[], None]]) -> QMenu:
        menu = self.menubar.addMenu("文件(&F)")

        act_new = QAction("新建工程(&N)", self.main_window)
        act_new.setShortcut("Ctrl+N")
        act_new.triggered.connect(callbacks.get("new_project", lambda: None))
        menu.addAction(act_new)

        act_open_proj = QAction("打开工程(&P)", self.main_window)
        act_open_proj.setShortcut("Ctrl+Shift+O")
        act_open_proj.triggered.connect(callbacks.get("open_project", lambda: None))
        menu.addAction(act_open_proj)

        menu.addSeparator()

        act_open = QAction("打开文件(&O)", self.main_window)
        act_open.setShortcut(QKeySequence.StandardKey.Open)
        act_open.triggered.connect(callbacks.get("open_file", lambda: None))
        menu.addAction(act_open)

        menu.addSeparator()

        act_exit = QAction("退出(&X)", self.main_window)
        act_exit.setShortcut("Ctrl+Q")
        act_exit.triggered.connect(self.main_window.close)
        menu.addAction(act_exit)

        return menu

    def _build_check_menu(self, callbacks: dict[str, Callable[[], None]]) -> QMenu:
        from app.ui.common.icons import Icons

        menu = self.menubar.addMenu("查看(&C)")

        act_log = QAction("日志(&L)", self.main_window)
        act_log.setShortcut("Ctrl+L")
        act_log.setStatusTip("打开 / 唤起日志面板")
        act_log.setIcon(Icons.app())  # 不强制 icon
        act_log.triggered.connect(callbacks.get("show_log", lambda: None))
        menu.addAction(act_log)

        return menu
