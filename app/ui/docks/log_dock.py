"""日志面板 Dock。

迁自 :class:`component.LogDock.LogDock`，去掉了对 :class:`MainWindow.log_dock`
的动态属性依赖，统一由 :class:`MainWindow._docks["log"]` 管理。

除查看 / 查询 / 保存之外，这里还是**导出诊断包**的入口（见
:meth:`LogDock._on_export_diagnostics`）：用户报错之后该发什么给我们，就在这一排
按钮里，不必再去 logs/ 目录里猜哪个文件有用。

固定显示文本日志（``app.log``，跨运行累积）。默认勾选「当前会话」，按本次运行
的开始时间自动过滤，时间控件隐藏；取消勾选后显示全部累积日志，时间范围恢复为
手动选择。级别 / 文本 / 条数条件两种状态下通用。
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
    QCheckBox,
    QComboBox,
    QDockWidget,
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
)

from app.infra.fs.io import write_text
from app.infra.logging import (
    active_log_config,
    build_diagnostics_package,
    current_session,
    get_logger,
    render_log_html,
)
from app.ui.common.date_field import DatePickerField
from app.ui.common.project_report import snapshot_text
from app.ui.common.widgets import open_file_dir

logger = get_logger(__name__)

_LEVEL_TAG_RE = re.compile(r">([A-Z]+)</")
#: 日志行行首时间戳（与 ``LogConfig.date_format`` 一致），固定 19 字符，可直接按字符串比较。
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _line_has_level(line: str, level: str) -> bool:
    if level == "ALL":
        return True
    return any(m.group(1) == level for m in _LEVEL_TAG_RE.finditer(line))


def _line_in_time_range(line: str, start: str, end: str) -> bool:
    """日志行时间戳是否落在 ``[start, end]``（含端点）内。

    时间戳格式固定为 ``YYYY-MM-DD HH:MM:SS``，定长且高位在前，按字符串比较
    与按时间比较等价。``start`` / ``end`` 为空表示该端不限。没有时间戳的行
    （旧格式 / 外部写入）在启用时间过滤时视为不匹配。
    """
    if not _TIMESTAMP_RE.match(line):
        return False
    ts = line[:19]
    if start and ts < start:
        return False
    if end and ts > end:
        return False
    return True


def _filter_lines_by_time(lines: list[str], start: str, end: str) -> list[str]:
    """按时间过滤，并把**没有时间戳的续行归到上一条记录**。

    异常堆栈由 ``logging.Formatter`` 追加在记录后面，每行都没有时间戳（也不带
    级别标签）。若逐行判断，时间过滤会把堆栈整段丢掉——恰恰是排查最需要的部分。
    所以只在遇到新的时间戳行时重新决定是否保留，续行跟随上一条记录。
    """
    kept: list[str] = []
    in_range = False
    for line in lines:
        if _TIMESTAMP_RE.match(line):
            in_range = _line_in_time_range(line, start, end)
        if in_range:
            kept.append(line)
    return kept


class LogDock(QDockWidget):
    """日志面板。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("日志信息", parent)
        self.param = {
            "size": 500,
            "level": "ALL",
            "filter": "",
            #: 时间过滤开关与闭区间端点（``YYYY-MM-DD HH:MM:SS``）；关闭时忽略端点
            "time_enabled": False,
            "time_start": "",
            "time_end": "",
        }
        self.txt: Optional[QTextBrowser] = None
        self.auto_scroll = True
        self.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self._init_ui()
        # _init_ui 已按默认勾选应用了「当前会话」时间条件，首次加载就用它
        self._load_log_file()
        # 自动刷新：dock 可见且数据源有新写入时才重载（用 mtime 去抖）。
        # 手动「刷新日志」也会更新 mtime，避免下一次定时 tick 重复重载。
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
        layout.addWidget(self._build_content(), 1)
        self.setWidget(container)
        # 控件都建好后才能应用初始状态（默认勾选「当前会话」→ 隐藏时间控件）
        self.current_session_check.toggled.connect(self._on_current_session_toggled)
        self._set_time_controls_visible(False)
        self._apply_session_time_param()

    def _build_button_bar(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(5, 5, 5, 5)
        row.setSpacing(6)

        # 查询文本输入不占满整行，把宽度留给后面的时间范围与操作按钮
        self.search_line = QLineEdit()
        self.search_line.setPlaceholderText("查询文本")
        self.search_line.setMaximumWidth(130)
        row.addWidget(self.search_line)

        # 日期范围：沿用属性录入的日期控件（可点日历 /「当前时间」），
        # 留空表示该端不限；两端都不填时不做日期过滤。
        # 勾选「当前会话」时这两组控件隐藏，时间范围由会话自动决定。
        self.time_label = QLabel("时间")
        row.addWidget(self.time_label)
        self.start_time = DatePickerField(mode="date")
        self.start_time.setFixedWidth(120)
        self.start_time.setToolTip("起始日期（含），留空表示不限")
        row.addWidget(self.start_time)
        self.time_dash = QLabel("-")
        row.addWidget(self.time_dash)
        self.end_time = DatePickerField(mode="date")
        self.end_time.setFixedWidth(120)
        self.end_time.setToolTip("结束日期（含），留空表示不限")
        row.addWidget(self.end_time)
        # 选完时间立即按当前条件重查，不必再点一次「查询」
        self.start_time.valueChanged.connect(self._on_time_changed)
        self.end_time.valueChanged.connect(self._on_time_changed)

        self.level_combo = QComboBox()
        self.level_combo.addItems(["ALL", "INFO", "DEBUG", "ERROR", "WARNING", "CRITICAL"])
        self.level_combo.setCurrentIndex(0)
        row.addWidget(self.level_combo)

        self.size_combo = QComboBox()
        self.size_combo.addItems(["50", "100", "500", "1000", "无限制"])
        self.size_combo.setCurrentIndex(2)
        row.addWidget(self.size_combo)

        query = QPushButton("查询")
        query.clicked.connect(self._on_search)
        row.addWidget(query)

        # 默认只看本次运行：按会话开始时间过滤，时间控件随勾选状态显隐
        self.current_session_check = QCheckBox("当前会话")
        self.current_session_check.setChecked(True)
        self.current_session_check.setToolTip(
            "只显示本次运行开始之后的日志，时间范围自动确定；\n"
            "取消勾选可查看全部累积日志，并手动选择时间范围"
        )
        row.addWidget(self.current_session_check)

        row.addSpacerItem(
            QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        )

        self.clear_btn = QPushButton("清空日志")
        self.clear_btn.setToolTip("清空累积日志 app.log；会话日志与诊断包不受影响")
        self.clear_btn.clicked.connect(self._on_clear)
        row.addWidget(self.clear_btn)
        refresh = QPushButton("刷新日志")
        refresh.clicked.connect(self._on_refresh)
        row.addWidget(refresh)
        save = QPushButton("保存日志")
        save.setToolTip("把当前筛选后的日志另存为 .log")
        save.clicked.connect(self._on_save)
        row.addWidget(save)
        export = QPushButton("导出诊断包")
        export.setToolTip(
            "把本次运行的日志、崩溃记录、环境 / 工程 / 配置快照打成一个 zip（凭据已打码），\n"
            "用于反馈问题。"
        )
        export.clicked.connect(self._on_export_diagnostics)
        row.addWidget(export)
        row.addSpacing(10)
        self.auto_btn = QPushButton("自动滚动")
        self.auto_btn.setCheckable(True)
        self.auto_btn.setChecked(True)
        self.auto_btn.clicked.connect(
            lambda: setattr(self, "auto_scroll", self.auto_btn.isChecked())
        )
        row.addWidget(self.auto_btn)
        return bar

    def _build_content(self) -> QWidget:
        """文本日志视图。"""
        self.txt = QTextBrowser()  # 配色由主窗体 window_qss / app_qss 统一负责
        return self.txt

    # ---------------------------------------------------------- 行为

    def _log_path(self) -> str:
        """当前生效的 ``app.log`` 路径。

        必须**运行时**解析：本模块在 ``app.bootstrap`` 导入阶段就会被连带导入，
        早于 ``main()`` 里的 ``_ensure_portable_env`` / ``_configure_logging``。
        打包后那时 ``__file__`` 推出的默认目录是 ``_internal/logs``，与真正写入的
        ``<便携包>/logs`` 不同——缓存成模块常量会让日志面板永远读那个空文件。
        """
        cfg = active_log_config()
        return str(cfg.log_dir / cfg.all_log_filename)

    def _load_log_file(self, param: Optional[dict] = None) -> None:
        p = param or self.param
        path = self._log_path()
        if not os.path.exists(path):
            self.txt.setHtml("")
            return
        with open(path, encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        # 时间过滤先做（原始行行首就是时间戳），再按级别 / 文本筛选，最后截取条数。
        if p.get("time_enabled"):
            lines = _filter_lines_by_time(
                lines, p.get("time_start", ""), p.get("time_end", "")
            )
        raw = [line for line in lines if _line_has_level(line, p["level"])]
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
            return os.path.getmtime(self._log_path())
        except OSError:
            return 0.0

    def _on_current_session_toggled(self, checked: bool) -> None:
        """勾选当前会话：时间范围由会话决定，日期控件隐藏；取消则恢复手动选择。"""
        self._set_time_controls_visible(not checked)
        self._on_search()

    def _set_time_controls_visible(self, visible: bool) -> None:
        for widget in (self.time_label, self.start_time, self.time_dash, self.end_time):
            widget.setVisible(visible)

    def _current_session_start(self) -> str:
        """本次运行的起始时间（``YYYY-MM-DD HH:MM:SS``）。

        会话 id 前 15 位就是启动时间（``20261022-133554-p6208``）。取不到会话
        时返回空串，调用方据此不做时间过滤——总比拿一个猜的时间把日志筛没了强。
        """
        session = current_session()
        if session is None:
            return ""
        try:
            started = datetime.strptime(session.session_id[:15], "%Y%m%d-%H%M%S")
        except ValueError:
            return ""
        return started.strftime("%Y-%m-%d %H:%M:%S")

    def _apply_session_time_param(self) -> None:
        """把「当前会话」的时间条件写进查询参数。

        只设开始时间、不设结束时间：会话还在进行，之后写入的日志也应继续显示
        （自动刷新直接用这份参数，固定 ``end=now`` 会把新日志挡在外面）。
        """
        start = self._current_session_start()
        self.param["time_enabled"] = bool(start)
        self.param["time_start"] = start
        self.param["time_end"] = ""

    def _auto_reload(self) -> None:
        """定时 tick：dock 不可见或数据源未变化时跳过，避免无谓重绘。"""
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
        if self.current_session_check.isChecked():
            # 时间范围由本次运行决定，隐藏的日期控件不参与查询
            self._apply_session_time_param()
            self._on_refresh()
            return
        start_date = self.start_time.value()
        end_date = self.end_time.value()
        # 界面只选到年月日；查询时开始取当天 00:00:00、结束取当天 23:59:59，
        # 保证所选日期当天的日志都包含在内。
        start = f"{start_date.toString('yyyy-MM-dd')} 00:00:00" if start_date is not None else ""
        end = f"{end_date.toString('yyyy-MM-dd')} 23:59:59" if end_date is not None else ""
        if start and end and start > end:
            QMessageBox.warning(self, "时间查询", "开始日期不能晚于结束日期，请重新选择。")
            return
        self.param["time_enabled"] = bool(start or end)
        self.param["time_start"] = start
        self.param["time_end"] = end
        self._on_refresh()

    def _on_time_changed(self) -> None:
        """日期控件值变化后即时刷新；区间暂时颠倒时先不查（等用户改好）。"""
        start_date = self.start_time.value()
        end_date = self.end_time.value()
        if start_date is not None and end_date is not None and start_date > end_date:
            return
        self._on_search()

    def _on_clear(self) -> None:
        mb = QMessageBox(self)
        mb.setWindowTitle("日志")
        mb.setText("是否清除当前日志信息？")
        mb.setIcon(QMessageBox.Icon.Warning)
        mb.addButton(QMessageBox.StandardButton.Ok)
        mb.addButton(QMessageBox.StandardButton.Cancel)
        if mb.exec() == QMessageBox.StandardButton.Ok:
            # 只清累积的 app.log。会话日志（session/*.log、*.jsonl）是诊断包的
            # 核心证据，清空日志不应连带清掉；会话文件自己的清理由 ``session_keep``
            # 在每次启动时负责。
            write_text(self._log_path(), "")
            self._load_log_file(self.param)
            self._last_mtime = self._file_mtime()

    def _on_refresh(self) -> None:
        self._load_log_file(self.param)
        self._last_mtime = self._file_mtime()

    def _on_save(self) -> None:
        path = open_file_dir(self, is_dir=True, title="选择保存目录")
        if not path:
            return
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        file_name = f"{stamp}.log"
        full = os.path.join(path, file_name)
        ok = write_text(full, self.txt.toPlainText())
        if not ok:
            QMessageBox.warning(self, "保存失败", f"日志保存失败：\n{full}")
            return
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
