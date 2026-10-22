"""日志面板「列表」视图的模型：把会话 JSONL 映射成多列表格。

**为什么读 JSONL 而不是 ``app.log``**：``app.log`` 是预渲染 HTML（带 ``<font>``
和 message marker），一条记录可能跨多行；按行切再扒标签既脆又慢。会话 JSONL 一条
记录就是一行 JSON，字段天然分好，正好每个字段一列——文本日志行里有的时间、模块、
行号、进程线程、op、消息、异常，这里都能直接看到，不必再靠详情面板展开。

数据源是**一次运行**的会话 JSONL（见 :mod:`app.infra.logging.session`），包括
轮转出来的 ``.jsonl.1`` 备份；日志面板还可以切到历史会话文件。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from qgis.PyQt.QtCore import QAbstractTableModel, QModelIndex, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QIcon, QPalette, QPen
from qgis.PyQt.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from app.infra.logging.formatter import HtmlLogFormatter

#: 列顺序。前几列是排查时最常看的，堆栈放最后（很长，按需横向滚动）。
COLUMNS = (
    "时间",
    "级别",
    "用户",
    "模块",
    "内容",
    "异常",
    "账号",
    "主机",
    "系统型号",
    "文件行号",
    "进程",
    "线程",
    "op",
    "堆栈",
)

#: 级别 → (颜色, 加粗)。复用文本日志的配色，两个视图看起来是同一套严重度语义
_LEVEL_STYLES = {
    logging.getLevelName(level): style
    for level, style in HtmlLogFormatter.LEVEL_STYLES.items()
}

_EMPTY = "-"

#: 堆栈在表格里压成一行显示；完整多行内容仍可从悬停提示 / JSONL 里拿到
_STACK_JOINER = " | "


@dataclass(frozen=True)
class LogRecord:
    """表格里的一行（已格式化，不含 HTML）。"""

    time: str
    level: str
    user: str
    module: str
    message: str
    exception: str
    user_id: str
    host: str
    system: str
    location: str
    pid: str
    thread: str
    op: str
    stack: str


def parse_jsonl(path: Path | str) -> list[LogRecord]:
    """读取会话 JSONL；坏行（截断 / 外部写入）跳过，不影响其余记录。"""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    records: list[LogRecord] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(raw, dict):
            records.append(_to_record(raw))
    return records


def _to_record(raw: dict[str, Any]) -> LogRecord:
    user_id = str(raw.get("user_id") or "")
    user_name = str(raw.get("user") or "")
    # 老会话没有 user 字段；登录失败时 user 也是空，用账号兜底，再不行显示 -
    user = user_name or user_id or _EMPTY
    host = str(raw.get("host") or _EMPTY)
    system = str(raw.get("os") or _EMPTY)
    display_time = _display_time(raw.get("ts"))
    level = str(raw.get("level") or _EMPTY)

    # 模块列沿用文本日志的写法 ``name.func``，文件行号单独成列：定位问题最常用的
    # 两组信息都直接看得见。
    logger_name = str(raw.get("logger") or "")
    func = str(raw.get("func") or "")
    module = f"{logger_name}.{func}" if logger_name and func else logger_name or _EMPTY
    file_name = str(raw.get("file") or "")
    line = str(raw.get("line") or "")
    location = f"{file_name}:{line}" if file_name else _EMPTY

    message = str(raw.get("msg") or "") or _EMPTY
    exception = _EMPTY
    stack = ""
    exc = raw.get("exc") if isinstance(raw.get("exc"), dict) else None
    if exc:
        exc_type = str(exc.get("type") or "?")
        exc_msg = str(exc.get("msg") or "")
        exception = f"{exc_type}: {exc_msg}" if exc_msg else exc_type
        stack = str(exc.get("stack") or "").rstrip()

    return LogRecord(
        time=display_time,
        level=level,
        user=user,
        module=module,
        message=message,
        exception=exception,
        user_id=user_id or _EMPTY,
        host=host,
        system=system,
        location=location,
        pid=str(raw.get("pid") if raw.get("pid") is not None else _EMPTY),
        thread=str(raw.get("thread") if raw.get("thread") is not None else _EMPTY),
        op=str(raw.get("op") or _EMPTY),
        stack=stack,
    )


def _display_time(ts: Any) -> str:
    """``2026-10-17T09:15:33.123+08:00`` → ``2026-10-17 09:15:33``（本地时间不变）。

    只截显示串，不做时区换算：日志文件里的时间本来就是本机本地时间。
    """
    text = str(ts or "")
    if len(text) >= 19 and text[10] == "T":
        return f"{text[:10]} {text[11:19]}"
    return text or _EMPTY


class LogTableModel(QAbstractTableModel):
    """只读表格模型；``records`` 已按筛选 / 截取后的顺序排好。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[LogRecord] = []

    # ------------------------------------------------------------ 对外

    def set_records(self, records: Iterable[LogRecord]) -> None:
        self.beginResetModel()
        self._rows = list(records)
        self.endResetModel()

    def record(self, row: int) -> Optional[LogRecord]:
        """第 ``row`` 行（源模型顺序）；越界返回 ``None``。"""
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    # ------------------------------------------------------------ Qt 接口

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(  # noqa: N802
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            if 0 <= section < len(COLUMNS):
                return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        record = self._rows[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return (
                record.time,
                record.level,
                record.user,
                record.module,
                record.message,
                record.exception,
                record.user_id,
                record.host,
                record.system,
                record.location,
                record.pid,
                record.thread,
                record.op,
                record.stack.replace("\n", _STACK_JOINER) if record.stack else _EMPTY,
            )[column]
        if role == Qt.ItemDataRole.ToolTipRole:
            return _tooltip(record)
        if role == Qt.ItemDataRole.TextAlignmentRole and column == 1:
            return int(Qt.AlignmentFlag.AlignCenter)
        if column == 1:
            color, bold = _LEVEL_STYLES.get(record.level, ("", False))
            if role == Qt.ItemDataRole.ForegroundRole and color:
                return QColor(color)
            if role == Qt.ItemDataRole.FontRole and bold:
                font = QFont()
                font.setBold(True)
                return font
        return None


class LevelColumnDelegate(QStyledItemDelegate):
    """让「级别」列在选中行里也保留级别色。

    Qt 默认 delegate 只在**未选中**时使用模型的 ``ForegroundRole``；选中行的字色
    由样式表 ``::item:selected { color: ... }`` 统一覆盖，于是最新一条（默认选中）
    CRITICAL 显示成普通文字色，和文本视图里的深红对不上。这里先让样式画好底色，
    再按级别色自己画文字，选中与否都不影响级别配色。
    """

    def initStyleOption(self, option, index) -> None:  # noqa: N802 —— Qt 接口
        super().initStyleOption(option, index)
        color = index.data(Qt.ItemDataRole.ForegroundRole)
        if color is not None:
            # 未选中时也统一走 palette，保证两条绘制路径颜色一致
            option.palette.setBrush(
                QPalette.ColorRole.HighlightedText, QBrush(color)
            )

    def paint(self, painter, option, index) -> None:  # noqa: D401 —— Qt 接口
        color = index.data(Qt.ItemDataRole.ForegroundRole)
        if color is None:
            super().paint(painter, option, index)
            return

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget is not None else QApplication.style()

        text = opt.text
        # 1) 先按样式画底色 / 选中态 / 交替行，不画文字
        opt.text = ""
        opt.icon = QIcon()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)

        # 2) 文字自己画：绕开样式表对选中行字色的覆盖
        opt.text = text
        text_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, opt, opt.widget
        )
        painter.save()
        painter.setPen(QPen(QColor(color)))
        painter.setFont(opt.font)
        elided = painter.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideRight, text_rect.width()
        )
        painter.drawText(text_rect, int(opt.displayAlignment), elided)
        painter.restore()


def _tooltip(record: LogRecord) -> str:
    """单元格悬停：多行堆栈保持原样，比表格里压成一行好读。"""
    lines = [f"{record.time}  {record.level}  {record.user}", "", record.message]
    if record.exception != _EMPTY:
        lines += ["", record.exception]
    if record.stack:
        lines += ["", record.stack]
    lines += [
        "",
        f"模块：{record.module}    文件：{record.location}",
        f"主机：{record.host}    系统：{record.system}",
        f"账号：{record.user_id}    进程：{record.pid}    线程：{record.thread}    "
        f"op：{record.op}",
    ]
    return "\n".join(lines)
