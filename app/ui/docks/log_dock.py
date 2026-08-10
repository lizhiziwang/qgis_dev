"""日志面板 Dock。

迁自 :class:`component.LogDock.LogDock`，去掉了对 :class:`MainWindow.log_dock`
的动态属性依赖，统一由 :class:`MainWindow._docks["log"]` 管理。
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QDockWidget,
)

from app.infra.fs.io import write_text
from app.infra.fs.paths import Paths
from app.infra.logging import (
    LogConfig,
    _RootSink,
    get_logger,
    render_log_html,
)


logger = get_logger(__name__)

_LOG_CONFIG = LogConfig.from_env()
_ALL_LOG_PATH = str(_LOG_CONFIG.log_dir / _LOG_CONFIG.all_log_filename)

_LEVEL_TAG_RE = re.compile(r">([A-Z]+)</")


def _line_has_level(line: str, level: str) -> bool:
    if level == "ALL":
        return True
    return any(m.group(1) == level for m in _LEVEL_TAG_RE.finditer(line))


class LogDock(QDockWidget):
    """日志面板。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("日志信息", parent)
        self.param = {"size": 500, "level": "ALL", "filter": ""}
        self.txt: Optional[QTextBrowser] = None
        self.auto_scroll = True
        self.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self._init_ui()
        self._load_log_file()

    def _init_ui(self) -> None:
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_button_bar())
        layout.addWidget(self._build_text(), 1)
        self.setWidget(container)

    def _build_button_bar(self) -> QWidget:
        bar = QWidget()
        h = QHBoxLayout(bar)
        self.search_line = QLineEdit()
        self.search_line.setPlaceholderText("查询文本")
        h.addWidget(self.search_line)

        self.level_combo = QComboBox()
        self.level_combo.addItems(["ALL", "INFO", "DEBUG", "ERROR", "WARNING"])
        self.level_combo.setCurrentIndex(0)
        h.addWidget(self.level_combo)

        self.size_combo = QComboBox()
        self.size_combo.addItems(["50", "100", "500", "1000", "无限制"])
        self.size_combo.setCurrentIndex(2)
        h.addWidget(self.size_combo)

        query = QPushButton("查询")
        query.clicked.connect(self._on_search)
        h.addWidget(query)

        h.addSpacerItem(QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))

        clear = QPushButton("清空日志")
        clear.clicked.connect(self._on_clear)
        h.addWidget(clear)
        refresh = QPushButton("刷新日志")
        refresh.clicked.connect(self._on_refresh)
        h.addWidget(refresh)
        save = QPushButton("保存日志")
        save.clicked.connect(self._on_save)
        h.addWidget(save)
        h.addSpacing(10)
        self.auto_btn = QPushButton("自动滚动")
        self.auto_btn.setCheckable(True)
        self.auto_btn.setChecked(True)
        self.auto_btn.clicked.connect(lambda: setattr(self, "auto_scroll", self.auto_btn.isChecked()))
        h.addWidget(self.auto_btn)
        h.setSpacing(8)
        h.setContentsMargins(5, 5, 5, 5)
        return bar

    def _build_text(self) -> QWidget:
        wrapper = QWidget()
        qh = QHBoxLayout(wrapper)
        qh.setContentsMargins(0, 0, 0, 0)
        qh.setSpacing(0)
        self.txt = QTextBrowser()  # 配色由主窗体 window_qss / app_qss 统一负责
        qh.addWidget(self.txt)
        return wrapper

    # ---------------------------------------------------------- 行为

    def _load_log_file(self, param: Optional[dict] = None) -> None:
        p = param or self.param
        if not os.path.exists(_ALL_LOG_PATH):
            self.txt.setHtml("")
            return
        with open(_ALL_LOG_PATH, "r", encoding="utf-8") as f:
            raw = [line.strip() for line in f.readlines() if _line_has_level(line, p["level"])]
        rendered = [render_log_html(line) for line in raw]
        if p["filter"]:
            rendered = [line for line in rendered if p["filter"] in line]
        if p["size"] != -1 and len(rendered) > p["size"]:
            rendered = rendered[-p["size"]:]
        html = "\n".join(
            f'<p style="line-height:1.5;margin:2px 0;">{line}</p>' for line in rendered
        )
        self.txt.setHtml(html)
        if self.auto_scroll:
            self.txt.moveCursor(QTextCursor.End)

    def _on_search(self) -> None:
        self.param["level"] = self.level_combo.currentText()
        self.param["filter"] = self.search_line.text()
        size_txt = self.size_combo.currentText()
        self.param["size"] = -1 if size_txt == "无限制" else int(size_txt)
        self._on_refresh()

    def _on_clear(self) -> None:
        mb = QMessageBox(self)
        mb.setWindowTitle("日志")
        mb.setText("是否清除当前日志信息")
        mb.setIcon(QMessageBox.Icon.Warning)
        mb.addButton(QMessageBox.StandardButton.Ok)
        mb.addButton(QMessageBox.StandardButton.Cancel)
        if mb.exec() == QMessageBox.StandardButton.Ok:
            write_text(_ALL_LOG_PATH, "")
            self._on_refresh()

    def _on_refresh(self) -> None:
        self._load_log_file(self.param)

    def _on_save(self) -> None:
        from app.ui.common.widgets import open_file_dir

        path = open_file_dir(self, is_dir=True, title="选择保存目录")
        if not path:
            return
        file_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}.log"
        full = os.path.join(path, file_name)
        write_text(full, self.txt.toPlainText())
        QMessageBox.information(self, "保存成功", f"日志已保存到\n{full}")


def add_log_dock(parent) -> LogDock:
    """兼容旧 ``add_log_dock`` API：幂等地挂日志面板到父窗口。"""
    existing = getattr(parent, "_docks", {}).get("log")
    if existing is not None:
        existing.show()
        existing.raise_()
        return existing
    log = LogDock(parent)
    if not hasattr(parent, "_docks"):
        parent._docks = {}
    parent._docks["log"] = log
    parent.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, log)
    return log
