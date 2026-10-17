"""日志面板 Dock。

迁自 :class:`component.LogDock.LogDock`，去掉了对 :class:`MainWindow.log_dock`
的动态属性依赖，统一由 :class:`MainWindow._docks["log"]` 管理。

除查看 / 查询 / 保存之外，这里还是**导出诊断包**的入口（见
:meth:`LogDock._on_export_diagnostics`）：用户报错之后该发什么给我们，就在这一排
按钮里，不必再去 logs/ 目录里猜哪个文件有用。
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QTextCursor
from qgis.PyQt.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.infra.fs.io import write_text
from app.infra.logging import (
    LogConfig,
    build_diagnostics_package,
    get_logger,
    render_log_html,
)
from app.ui.common.project_report import snapshot_text
from app.ui.common.widgets import open_file_dir

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
        # 自动刷新：dock 可见且日志文件有新写入时才重载（用 mtime 去抖）。
        # 手动「刷新日志」也会更新 _last_mtime，避免下一次定时 tick 重复重载。
        self._last_mtime = self._file_mtime()
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._auto_reload)
        self._timer.start()

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
        export = QPushButton("导出诊断包")
        export.setToolTip(
            "把本次运行的日志、崩溃记录、环境 / 工程 / 配置快照打成一个 zip（凭据已打码），\n"
            "用于反馈问题。"
        )
        export.clicked.connect(self._on_export_diagnostics)
        h.addWidget(export)
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
        # setHtml 会重置滚动位置；自动滚动开启时跳到底部（追最新日志），
        # 关闭时恢复到原位置，避免自动刷新把用户正在看的行拽走。
        scroll_pos = self.txt.verticalScrollBar().value()
        self.txt.setHtml(html)
        if self.auto_scroll:
            self.txt.moveCursor(QTextCursor.MoveOperation.End)
        else:
            self.txt.verticalScrollBar().setValue(scroll_pos)

    def _file_mtime(self) -> float:
        """日志文件最后修改时间；文件不存在返回 0（触发一次重载兜底）。"""
        try:
            return os.path.getmtime(_ALL_LOG_PATH)
        except OSError:
            return 0.0

    def _auto_reload(self) -> None:
        """定时 tick：dock 不可见或日志未变化时跳过，避免无谓重绘。"""
        if not self.isVisible():
            return
        mtime = self._file_mtime()
        if mtime == self._last_mtime:
            return
        self._last_mtime = mtime
        self._load_log_file(self.param)

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
        self._last_mtime = self._file_mtime()

    def _on_save(self) -> None:

        path = open_file_dir(self, is_dir=True, title="选择保存目录")
        if not path:
            return
        file_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}.log"
        full = os.path.join(path, file_name)
        write_text(full, self.txt.toPlainText())
        QMessageBox.information(self, "保存成功", f"日志已保存到\n{full}")

    def _on_export_diagnostics(self) -> None:
        """导出诊断包：一次运行的全部证据，打成一个 zip。

        这是"出问题之后该发什么给我们"的**唯一入口**（第四步）。现场同事不需要知道
        ``logs/`` 下哪个文件有用、哪个是上次跑剩下的，也不必担心把配置里的 key 发出去
        ——包里已经按同一套规则打过码（:mod:`app.infra.logging.redaction`）。

        同步执行：只读几 MB 文本再压缩，是一瞬间的事，为它起线程反而要处理
        "导出中用户又把窗口关了"。失败必须弹窗（静默失败等于用户白点一次），
        同时记一条日志——现场说"导不出来"时，我们能从日志里看出卡在哪。
        """
        path = open_file_dir(self, is_dir=True, title="选择诊断包保存目录")
        if not path:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            package = build_diagnostics_package(
                Path(path), extra={"project.json": snapshot_text()}
            )
        except Exception as exc:  # 导出失败要说清楚，不能静默
            logger.exception("导出诊断包失败 dest=%s", path)
            QMessageBox.critical(self, "导出失败", f"诊断包导出失败：\n{exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        size_kb = package.size_bytes / 1024
        QMessageBox.information(
            self,
            "导出完成",
            f"诊断包已导出到\n{package.path}\n\n"
            f"共 {len(package.entries)} 个文件、{size_kb:.0f} KB，凭据已打码。"
            f"\n请把它发给技术支持。",
        )


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
