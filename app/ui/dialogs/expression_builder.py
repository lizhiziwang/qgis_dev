"""自建表达式构建器。

替代 QGIS 原生 ``QgsExpressionBuilderDialog`` / ``QgsExpressionBuilderWidget``
（qgis.gui 组件）：界面全部用 ``QtWidgets`` 手搭，逻辑只依赖 ``qgis.core``，
不再引入 qgis.gui 带来的初始化 / 崩溃风险。

拆成可复用的控件 + 对话框两层，和 QGIS 一样：

- :class:`ExpressionBuilderWidget`：表达式构建器本体（可内嵌，字段计算器的
  表达式区直接用它）；
- :class:`ExpressionBuilderDialog`：把控件包成一个对话框（原调用方用）。

控件布局与 QGIS 表达式构建器一致——一行三栏 + 底部预览：

- 左栏：表达式编辑框（自建 :class:`CodeEditor`，等宽 + 语法高亮 + 行号 + 补全）；
- 中栏：``QTreeWidget``（窄）：字段与值 / 运算符 / 函数（按 ``group`` 归类）/
  特殊变量，**双击叶子节点**把对应文本插入编辑框（函数插入 ``name()`` 且光标
  停在括号内）；
- 右栏：``QTextBrowser``：显示当前选中项的帮助，函数帮助直接渲染
  ``QgsExpressionFunction.helpText()`` 返回的 HTML；
- 底部：实时预览，用图层首个要素求值，解析 / 求值出错就地标红。

支持用户在 QGIS 函数编辑器里写的 ``@qgsfunction`` 自定义函数——它们在
``QgsExpression.Functions()`` 里，已由 ``app.env.user_expressions`` 加载。
"""

from __future__ import annotations

import html
import re

from qgis.core import QgsExpression
from qgis.PyQt.QtCore import QRegularExpression, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat, QTextCursor
from qgis.PyQt.QtWidgets import (
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.code_editor import CodeEditor
from app.ui.common.theme import ThemeManager
from app.ui.tools.feature_attrs import field_alias

logger = get_logger(__name__)

#: 合法的裸字段名（不需要加双引号）
_BARE_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TAG_RE = re.compile(r"<[^>]+>")

#: 运算符（插入时两边留空格，便于继续拼）
_OPERATORS = (
    " + ",
    " - ",
    " * ",
    " / ",
    " % ",
    " ^ ",
    " = ",
    " != ",
    " <> ",
    " < ",
    " <= ",
    " > ",
    " >= ",
    " AND ",
    " OR ",
    " NOT ",
    " || ",
    " LIKE ",
    " ILIKE ",
    " IN ",
    " IS NULL ",
    " IS NOT NULL ",
    " BETWEEN ",
)

#: 常用特殊变量 / 常量
_SPECIALS = (
    ("NULL", "空值"),
    ("TRUE", "真"),
    ("FALSE", "假"),
    ("$id", "要素 id"),
    ("$geometry", "要素几何（用于 geometry 函数）"),
    ("$area", "面积（当前单位）"),
    ("$length", "长度（当前单位）"),
    ("$perimeter", "周长（当前单位）"),
    ("$x", "点几何 x 坐标"),
    ("$y", "点几何 y 坐标"),
    ("$feature", "当前要素（用于 get_feature 等）"),
    ("@layer_name", "当前图层名"),
    ("@layer_id", "当前图层 id"),
    ("@project_title", "工程标题"),
    ("@project_crs", "工程坐标系"),
)

_DEFAULT_HELP = (
    "<p>双击左侧条目插入到表达式。</p>"
    "<ul>"
    "<li><b>字段与值</b>：插入图层字段名</li>"
    "<li><b>运算符</b>：插入数学 / 比较 / 逻辑运算符</li>"
    "<li><b>函数</b>：插入函数调用，光标停在括号内</li>"
    "<li><b>特殊变量</b>：$id、$area、@layer_name 等</li>"
    "</ul>"
)


#: 表达式关键字（高亮 + 补全共用）
_KEYWORDS = "AND|OR|NOT|IN|LIKE|ILIKE|IS|NULL|BETWEEN|TRUE|FALSE"

#: 语法配色（VS Code Light+ / Dark+），按当前主题选一套
_SYNTAX_COLORS = {
    "light": {
        "string": "#a31515",
        "number": "#098658",
        "keyword": "#0000ff",
        "function": "#795e26",
        "identifier": "#001080",
        "quoted_field": "#0451a5",
        "variable": "#001080",
    },
    "dark": {
        "string": "#ce9178",
        "number": "#b5cea8",
        "keyword": "#569cd6",
        "function": "#dcdcaa",
        "identifier": "#9cdcfe",
        "quoted_field": "#9cdcfe",
        "variable": "#9cdcfe",
    },
}


def _syntax_colors() -> dict:
    try:
        key = ThemeManager.instance().current().key
    except Exception:  # noqa: BLE001 - 主题不可用时退回浅色
        key = "light"
    return _SYNTAX_COLORS.get(key, _SYNTAX_COLORS["light"])


def _char_format(color: str, bold: bool = False) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(color))
    if bold:
        fmt.setFontWeight(700)
    return fmt


class _ExpressionHighlighter(QSyntaxHighlighter):
    """QGIS 表达式语法高亮。

    规则顺序有讲究：字符串 / 双引号字段名放在最后应用，这样引号内部的
    关键字 / 标识符匹配会被整体覆盖掉（否则 ``'AND'`` 里的 ``AND`` 会被
    当成关键字上色）。
    """

    def __init__(self, document) -> None:
        super().__init__(document)
        c = _syntax_colors()
        ci = QRegularExpression.PatternOption.CaseInsensitiveOption
        self._rules = [
            (QRegularExpression(r"\b\d+(?:\.\d+)?\b"), _char_format(c["number"])),
            (QRegularExpression(r"\b[A-Za-z_]\w*\b"), _char_format(c["identifier"])),
            (
                QRegularExpression(rf"\b(?:{_KEYWORDS})\b", ci),
                _char_format(c["keyword"], bold=True),
            ),
            (
                QRegularExpression(r"\b[A-Za-z_]\w*(?=\s*\()"),
                _char_format(c["function"]),
            ),
            (QRegularExpression(r"[$@][A-Za-z_]\w*"), _char_format(c["variable"])),
            (QRegularExpression(r"'([^']|'')*'"), _char_format(c["string"])),
            (QRegularExpression(r'"([^"]|"")*"'), _char_format(c["quoted_field"])),
        ]

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt 命名
        for regex, fmt in self._rules:
            it = regex.globalMatch(text)
            while it.hasNext():
                match = it.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)


def _plain_text(markup: str) -> str:
    """把 QGIS 函数的 HTML 帮助压成纯文本（用于 tooltip）。"""
    text = _TAG_RE.sub("", markup or "")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
    text = text.replace("&gt;", ">").replace("&quot;", '"')
    return " ".join(text.split())


def _quote_field(name: str) -> str:
    """字段名需要时加双引号（与 QGIS 表达式一致）。"""
    if _BARE_FIELD_RE.match(name):
        return name
    return '"' + name.replace('"', '""') + '"'


class ExpressionBuilderWidget(QWidget):
    """表达式构建器本体（可内嵌，字段计算器的表达式区就用它）。"""

    #: 表达式有效性变化（True = 当前表达式可求值）。宿主据此启用/禁用「确定」。
    validation_changed = pyqtSignal(bool)

    def __init__(self, layer=None, parent=None) -> None:
        super().__init__(parent)
        self._layer = layer
        #: 当前表达式是否有效（空 / 语法错误 / 求值错误均为 False）
        self._valid = False
        #: 预览用的首个要素（惰性取一次）
        self._first_feature = None
        #: 编辑停顿多久后刷新预览
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(250)
        self._preview_timer.timeout.connect(self._update_preview)

        self._build()
        self._populate()
        self._update_preview()

    # ------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 与 QGIS 表达式构建器一致：顶部 tab 切换「表达式」/「函数编辑器」
        self.tabs = QTabWidget(self)
        root.addWidget(self.tabs)

        self.tabs.addTab(self._build_expression_page(), "表达式")

        from app.ui.dialogs.function_editor import FunctionEditorWidget

        self.function_editor = FunctionEditorWidget(self)
        self.tabs.addTab(self.function_editor, "函数编辑器")
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _build_expression_page(self) -> QWidget:
        """「表达式」tab 的内容：三栏 + 底部预览。"""
        page = QWidget(self)
        root = QVBoxLayout(page)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        def make_pane(title: str, widget: QWidget) -> QWidget:
            """给控件配一个顶部标题，作为一个 splitter 面板。"""
            pane = QWidget(page)
            lay = QVBoxLayout(pane)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(4)
            lay.addWidget(QLabel(title, pane))
            lay.addWidget(widget, 1)
            return pane

        splitter = QSplitter(Qt.Orientation.Horizontal, page)

        self.expr_edit = CodeEditor(page)
        self.expr_edit.setPlaceholderText("例如 upper(name)")
        self.expr_edit.setMinimumHeight(120)
        self._highlighter = _ExpressionHighlighter(self.expr_edit.document())
        self.expr_edit.textChanged.connect(self._preview_timer.start)
        splitter.addWidget(make_pane("表达式", self.expr_edit))

        self.tree = QTreeWidget(page)
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(120)
        self.tree.itemDoubleClicked.connect(self._on_double_clicked)
        self.tree.currentItemChanged.connect(self._on_current_changed)
        splitter.addWidget(make_pane("字段与函数", self.tree))

        self.help_view = QTextBrowser(page)
        self.help_view.setOpenExternalLinks(False)
        self.help_view.setHtml(_DEFAULT_HELP)
        splitter.addWidget(make_pane("函数描述", self.help_view))

        # 中栏（树）保持窄宽度，左右两栏分摊剩余空间
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 1)
        splitter.setSizes([360, 190, 240])
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        self.preview_label = QLabel("", page)
        self.preview_label.setObjectName("PropHint")
        self.preview_label.setWordWrap(True)
        self.preview_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        root.addWidget(self.preview_label)
        return page

    def _on_tab_changed(self, index: int) -> None:
        # 切回「表达式」时刷新函数树 / 补全（函数编辑器里可能刚加了函数）
        if index == 0:
            self._populate()

    def _populate(self) -> None:
        """填左侧树：字段 / 运算符 / 函数（按组）/ 特殊变量。"""
        self.tree.clear()
        words: list[str] = []

        fields_root = QTreeWidgetItem(["字段与值"])
        fields = self._layer.fields() if self._layer is not None else None
        if fields is not None:
            for i in range(fields.size()):
                fld = fields.at(i)
                name = fld.name()
                words.append(name)
                item = QTreeWidgetItem([name])
                item.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    {
                        "kind": "field",
                        "insert": _quote_field(name),
                        "help": (
                            f"<p><b>{html.escape(name)}</b></p>"
                            f"<p>类型：{html.escape(fld.typeName())}"
                            + (
                                f"<br>别名：{html.escape(field_alias(fld))}"
                                if field_alias(fld)
                                else ""
                            )
                            + "</p>"
                        ),
                    },
                )
                item.setToolTip(0, f"{name} ({fld.typeName()})")
                fields_root.addChild(item)
        self.tree.addTopLevelItem(fields_root)
        fields_root.setExpanded(True)

        ops_root = QTreeWidgetItem(["运算符"])
        for op in _OPERATORS:
            item = QTreeWidgetItem([op.strip()])
            item.setData(
                0,
                Qt.ItemDataRole.UserRole,
                {"kind": "operator", "insert": op, "help": f"<p>运算符 <b>{op.strip()}</b></p>"},
            )
            ops_root.addChild(item)
        self.tree.addTopLevelItem(ops_root)

        self._functions_root = QTreeWidgetItem(["函数"])
        groups: dict[str, list] = {}
        for fn in QgsExpression.Functions():
            groups.setdefault(fn.group() or "其他", []).append(fn)
        # Custom（用户自定义）排最前，其余按名称排
        for gname in sorted(groups, key=lambda g: (g != "Custom", g)):
            gitem = QTreeWidgetItem([gname])
            for fn in sorted(groups[gname], key=lambda f: f.name()):
                words.append(fn.name())
                help_html = fn.helpText() or ""
                item = QTreeWidgetItem([fn.name()])
                item.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    {
                        "kind": "function",
                        "name": fn.name(),
                        "insert": f"{fn.name()}()",
                        "help": help_html,
                    },
                )
                item.setToolTip(0, _plain_text(help_html)[:300])
                gitem.addChild(item)
            self._functions_root.addChild(gitem)
        self.tree.addTopLevelItem(self._functions_root)

        special_root = QTreeWidgetItem(["特殊变量"])
        for name, tip in _SPECIALS:
            words.append(name)
            item = QTreeWidgetItem([name])
            item.setData(
                0,
                Qt.ItemDataRole.UserRole,
                {
                    "kind": "special",
                    "insert": name,
                    "help": f"<p><b>{html.escape(name)}</b></p><p>{html.escape(tip)}</p>",
                },
            )
            item.setToolTip(0, tip)
            special_root.addChild(item)
        self.tree.addTopLevelItem(special_root)

        words.extend(_KEYWORDS.split("|"))
        self.expr_edit.set_completions(words)

    # ------------------------------------------------------------- 交互

    @staticmethod
    def _info(item) -> dict | None:
        return item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None

    def _on_double_clicked(self, item, _column: int) -> None:
        info = self._info(item)
        if not info:
            return
        kind = info.get("kind")
        text = info.get("insert") or ""
        self.expr_edit.insertPlainText(text)
        if kind == "function":
            # 光标停在括号内
            cursor = self.expr_edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Left)
            self.expr_edit.setTextCursor(cursor)
        self.expr_edit.setFocus()
        self._preview_timer.start()

    def _on_current_changed(self, current, _previous) -> None:
        info = self._info(current)
        if info and info.get("help"):
            self.help_view.setHtml(info["help"])
        else:
            self.help_view.setHtml(_DEFAULT_HELP)

    # ------------------------------------------------------------- 预览

    def expression_text(self) -> str:
        """当前表达式（去首尾空白）。"""
        return self.expr_edit.toPlainText().strip()

    def set_expression_text(self, expression: str) -> None:
        """设置表达式内容并把焦点放到编辑器。"""
        self.expr_edit.setPlainText(expression or "")
        self.expr_edit.setFocus()
        self._update_preview()

    def _context(self):
        layer = self._layer
        if layer is None or not hasattr(layer, "createExpressionContext"):
            return None
        ctx = layer.createExpressionContext()
        if self._first_feature is None:
            try:
                self._first_feature = next(layer.getFeatures(), None)
            except Exception:  # noqa: BLE001
                self._first_feature = None
        if self._first_feature is not None:
            ctx.setFeature(self._first_feature)
        return ctx

    def _update_preview(self) -> None:
        text = self.expression_text()
        if not text:
            self._show_preview("", error=False)
            self._set_valid(False)
            return
        expr = QgsExpression(text)
        if expr.hasParserError():
            self._show_preview(f"语法错误：{expr.parserErrorString()}", error=True)
            self._set_valid(False)
            return
        ctx = self._context()
        if ctx is None:
            # 无图层：只能判到语法层，视为可接受（预览不可用）
            self._show_preview("（无图层，无法预览）", error=False)
            self._set_valid(True)
            return
        try:
            expr.prepare(ctx)
            if expr.hasEvalError():
                self._show_preview(f"无法求值：{expr.evalErrorString()}", error=True)
                self._set_valid(False)
                return
            value = expr.evaluate(ctx)
        except Exception as exc:  # noqa: BLE001
            self._show_preview(f"无法求值：{exc}", error=True)
            self._set_valid(False)
            return
        if expr.hasEvalError():
            self._show_preview(f"无法求值：{expr.evalErrorString()}", error=True)
            self._set_valid(False)
            return
        self._show_preview(f"= {_fmt_value(value)}", error=False)
        self._set_valid(True)

    def _set_valid(self, valid: bool) -> None:
        """记录有效性并广播（值没变就不发信号）。"""
        if valid != self._valid:
            self._valid = valid
            self.validation_changed.emit(valid)

    def is_expression_valid(self) -> bool:
        """当前表达式是否有效（空 / 语法错误 / 求值错误均为 ``False``）。"""
        return self._valid

    def _show_preview(self, text: str, error: bool) -> None:
        self.preview_label.setText(text)
        self.preview_label.setStyleSheet("color: #c0392b;" if error else "")


class ExpressionBuilderDialog(BaseDialog):
    """把 :class:`ExpressionBuilderWidget` 包成一个对话框（兼容原调用方）。"""

    def __init__(self, layer=None, expression: str = "", parent=None) -> None:
        super().__init__(parent, "表达式构建器")
        self.setMinimumSize(0, 0)
        self.resize(760, 520)

        root = QVBoxLayout()
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        self._widget = ExpressionBuilderWidget(layer, self)
        root.addWidget(self._widget, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.setLayout(root)
        self._widget.set_expression_text(expression)

    def expression_text(self) -> str:
        """当前表达式（去首尾空白）。"""
        return self._widget.expression_text()

    def _on_accept(self) -> None:
        text = self.expression_text()
        if not text:
            QMessageBox.warning(self, "表达式构建器", "表达式不能为空")
            return
        expr = QgsExpression(text)
        if expr.hasParserError():
            QMessageBox.warning(
                self, "表达式构建器", f"表达式有语法错误：\n{expr.parserErrorString()}"
            )
            return
        self.accept()


def _fmt_value(value) -> str:
    """预览值的可读表示（几何 / 长文本做截断）。"""
    if value is None:
        return "NULL"
    if isinstance(value, str):
        text = value
        return f'"{text}"' if len(text) <= 120 else f'"{text[:117]}…"'
    text = str(value)
    return text if len(text) <= 200 else text[:197] + "…"
