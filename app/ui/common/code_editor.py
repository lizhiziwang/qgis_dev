"""通用代码编辑器控件。

在 ``QPlainTextEdit`` 基础上补齐"像个代码编辑器"的基本要素，供表达式 /
SQL 等自由文本输入使用：

- 等宽字体、Tab 转 4 空格、不自动换行、当前行高亮；
- 左侧行号槽（随行数 / 滚动自动更新）；
- 基于词表的自动补全：输入 2 个字符后弹出候选，或 ``Ctrl+Space`` 手动唤出。

语法高亮不在这里实现——调用方按需把 ``QSyntaxHighlighter`` 挂到
``self.document()`` 上（例如 ``expression_builder`` 的表达式高亮）。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QRect, QSize, Qt, QStringListModel
from qgis.PyQt.QtGui import (
    QColor,
    QFontDatabase,
    QFontMetricsF,
    QPainter,
    QTextCursor,
    QTextFormat,
)
from qgis.PyQt.QtWidgets import (
    QCompleter,
    QPlainTextEdit,
    QTextEdit,
    QWidget,
)

#: 行号槽 / 当前行用半透明灰，浅色深色主题下都能看清
_GUTTER_TEXT = QColor("#8a8f98")
_GUTTER_BG = QColor(127, 127, 127, 24)
_CURRENT_LINE = QColor(74, 144, 226, 24)


class _LineNumberArea(QWidget):
    """行号槽（真正的绘制委托给编辑器）。"""

    def __init__(self, editor: "CodeEditor") -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._editor.paint_line_numbers(event)


class CodeEditor(QPlainTextEdit):
    """带行号、当前行高亮与自动补全的代码编辑器。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        if font.pointSize() <= 0:
            font.setPointSize(10)
        self.setFont(font)
        self.setTabStopDistance(QFontMetricsF(font).horizontalAdvance(" ") * 4)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        self._line_number_area = _LineNumberArea(self)
        self._completer: QCompleter | None = None

        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._on_update_request)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_line_number_area_width(0)
        self._highlight_current_line()

    # ------------------------------------------------------------- 行号槽

    def line_number_area_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return int(QFontMetricsF(self.font()).horizontalAdvance("9") * digits) + 14

    def _update_line_number_area_width(self, _block_count: int) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _on_update_request(self, rect: QRect, dy: int) -> None:
        if dy:
            self._line_number_area.scroll(0, dy)
        else:
            self._line_number_area.update(
                0, rect.y(), self._line_number_area.width(), rect.height()
            )
        if rect.contains(self.viewport().rect()):
            self._update_line_number_area_width(0)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_number_area.setGeometry(
            QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height())
        )

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self._line_number_area)
        painter.fillRect(event.rect(), _GUTTER_BG)
        painter.setPen(_GUTTER_TEXT)

        block = self.firstVisibleBlock()
        number = block.blockNumber()
        offset = self.contentOffset()
        top = self.blockBoundingGeometry(block).translated(offset).top()
        bottom = top + self.blockBoundingRect(block).height()
        height = self.fontMetrics().height()
        width = self._line_number_area.width() - 6

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(
                    0,
                    int(top),
                    width,
                    height,
                    Qt.AlignmentFlag.AlignRight,
                    str(number + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            number += 1

    # ----------------------------------------------------------- 当前行

    def _highlight_current_line(self) -> None:
        selection = QTextEdit.ExtraSelection()
        selection.format.setBackground(_CURRENT_LINE)
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.cursor = self.textCursor()
        selection.cursor.clearSelection()
        self.setExtraSelections([selection])

    # ----------------------------------------------------------- 自动补全

    def set_completions(self, words) -> None:
        """设置补全词表（字段名 / 函数名 / 变量名等）。"""
        items = sorted({w for w in words if w}, key=str.lower)
        if not items:
            return
        if self._completer is None:
            completer = QCompleter(self)
            completer.setWidget(self)
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.activated.connect(self._insert_completion)
            self._completer = completer
        self._completer.setModel(QStringListModel(items, self._completer))

    def _current_word(self) -> str:
        cursor = self.textCursor()
        cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        return cursor.selectedText()

    def _insert_completion(self, completion: str) -> None:
        cursor = self.textCursor()
        cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        cursor.insertText(completion)
        self.setTextCursor(cursor)

    def _show_completions(self) -> None:
        completer = self._completer
        if completer is None:
            return
        popup = completer.popup()
        prefix = self._current_word()
        if len(prefix) < 2:
            if popup is not None:
                popup.hide()
            return
        if prefix != completer.completionPrefix():
            completer.setCompletionPrefix(prefix)
            index = completer.completionModel().index(0, 0)
            if not index.isValid():
                if popup is not None:
                    popup.hide()
                return
            popup.setCurrentIndex(index)
        rect = self.cursorRect()
        rect.setWidth(
            popup.sizeHintForColumn(0) + popup.verticalScrollBar().sizeHint().width()
        )
        completer.complete(rect)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        completer = self._completer
        popup = completer.popup() if completer is not None else None
        if popup is not None and popup.isVisible():
            # 弹窗可见时把确认 / 取消 / 切换键让给补全器处理
            if event.key() in (
                Qt.Key.Key_Enter,
                Qt.Key.Key_Return,
                Qt.Key.Key_Escape,
                Qt.Key.Key_Tab,
                Qt.Key.Key_Backtab,
            ):
                event.ignore()
                return
        if (
            completer is not None
            and event.key() == Qt.Key.Key_Space
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self._show_completions()
            return
        super().keyPressEvent(event)
        text = event.text()
        if completer is not None and text and (text.isalnum() or text == "_"):
            self._show_completions()
