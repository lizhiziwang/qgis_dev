"""自定义表达式函数编辑器。

本应用不走 QGIS 原生启动器，``QgsPythonRunner`` 未初始化，QGIS 自带的
「函数编辑器」（表达式构建器 → Function Editor）在本程序里实际不可用：
它靠 ``QgsPythonRunner`` 取保存目录并执行代码，runner 无效时会写到
``<当前盘符>:\\<名>.py`` 且不会即时注册——这正是"保存之后识别不到"的原因。

所以这里自建一个函数编辑器，绕开原生 Function Editor：

- :class:`FunctionEditorWidget`：函数编辑器本体，作为表达式构建器的
  「函数编辑器」tab 内嵌。

功能：

- 左侧列出 :func:`app.env.user_expressions.expressions_dir` 下的 ``*.py``；
- 右侧用 :class:`CodeEditor` 编辑文件内容；
- 保存后立即执行文件（``@qgsfunction`` 自注册），本进程立即可用；
- 文件写入标准 QGIS profile 的 ``python/expressions``，独立 QGIS 也能看到。
"""

from __future__ import annotations

import os
import re

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.env.user_expressions import (
    FUNCTION_TEMPLATE,
    delete_function_file,
    expressions_dir,
    list_function_files,
    save_function_file,
)
from app.infra.logging import get_logger
from app.ui.common.code_editor import CodeEditor
from app.ui.common.message_box import MessageBox

logger = get_logger(__name__)

#: 合法的函数文件名（不带 .py）
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_HELP = (
    "每个文件可定义多个函数，用 <code>@qgsfunction</code> 装饰；"
    "保存后会立即注册，表达式里马上就能用。"
    "函数建议放在 <code>group='Custom'</code>，表达式构建器里会排在前面。"
)

_TEMPLATE_HINT = (
    "@qgsfunction(group='Custom', referenced_columns=[])\n"
    "def my_sum(a, b):\n"
    "    \"\"\"两数相加。\"\"\"\n"
    "    return a + b"
)


class FunctionEditorWidget(QWidget):
    """函数文件增删改 + 保存即注册（可内嵌为表达式构建器的 tab）。"""

    def __init__(self, parent=None, select: str = "") -> None:
        super().__init__(parent)
        self._current_path: str | None = None
        self._build()
        self._reload_list(select)

    # ------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        hint = QLabel(_HELP, self)
        hint.setObjectName("PropHint")
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(hint)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # 左：文件列表 + 新建 / 删除
        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)
        left_layout.addWidget(QLabel("函数文件", left))
        self.file_list = QListWidget(left)
        self.file_list.currentItemChanged.connect(self._on_file_changed)
        left_layout.addWidget(self.file_list, 1)
        buttons_row = QHBoxLayout()
        self.new_btn = QPushButton("新建", left)
        self.del_btn = QPushButton("删除", left)
        self.new_btn.clicked.connect(self._new_file)
        self.del_btn.clicked.connect(self._delete_file)
        buttons_row.addWidget(self.new_btn)
        buttons_row.addWidget(self.del_btn)
        left_layout.addLayout(buttons_row)
        splitter.addWidget(left)

        # 右：代码编辑器 + 保存
        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)
        right_layout.addWidget(QLabel("函数代码（Python）", right))
        self.editor = CodeEditor(right)
        self.editor.setPlaceholderText(_TEMPLATE_HINT)
        right_layout.addWidget(self.editor, 1)
        action_row = QHBoxLayout()
        self.save_btn = QPushButton("保存并加载", right)
        self.save_btn.setToolTip("保存当前文件并立即注册其中的 @qgsfunction 函数")
        self.save_btn.clicked.connect(self._save_current)
        self.status = QLabel("", right)
        self.status.setObjectName("PropHint")
        self.status.setWordWrap(True)
        action_row.addWidget(self.save_btn)
        action_row.addWidget(self.status, 1)
        right_layout.addLayout(action_row)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 680])
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

    # ------------------------------------------------------------- 文件列表

    def _reload_list(self, select: str = "") -> None:
        """重新填充文件列表；``select`` 为要选中的文件名（不含 .py）。"""
        self.file_list.blockSignals(True)
        self.file_list.clear()
        target_row = -1
        for index, path in enumerate(list_function_files()):
            name = os.path.splitext(os.path.basename(path))[0]
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            self.file_list.addItem(item)
            if select and name == select:
                target_row = index
        self.file_list.blockSignals(False)

        count = self.file_list.count()
        self.del_btn.setEnabled(count > 0)
        self.save_btn.setEnabled(count > 0)
        if count == 0:
            self._current_path = None
            self.editor.clear()
            self.editor.document().setModified(False)
            self.status.setText(f"函数目录：{expressions_dir()}")
            return
        if target_row < 0:
            target_row = 0
        self.file_list.setCurrentRow(target_row)

    def refresh(self) -> None:
        """重新扫描函数文件目录（外部新增文件后调用）。"""
        self._reload_list()

    def _on_file_changed(self, current, _previous) -> None:
        # 切换文件前先把上一份改动静默保存，避免丢失（与 QGIS 行为一致）
        self.save_if_modified()
        if current is None:
            self._current_path = None
            self.editor.clear()
            self.editor.document().setModified(False)
            return
        self._load_path(current.data(Qt.ItemDataRole.UserRole))

    def _load_path(self, path: str) -> None:
        try:
            with open(path, encoding="utf-8") as fh:
                source = fh.read()
        except OSError as exc:
            source = ""
            logger.warning("读取函数文件失败：%s（%s）", path, exc)
        self._current_path = path
        self.editor.setPlainText(source)
        self.editor.document().setModified(False)
        self.status.setText(f"函数目录：{expressions_dir()}")

    # ------------------------------------------------------------- 动作

    def _new_file(self) -> None:
        name, ok = QInputDialog.getText(self, "新建函数文件", "文件名（不含 .py）：")
        name = (name or "").strip()
        if not ok or not name:
            return
        if not _NAME_RE.match(name):
            MessageBox.warning(self, "新建函数文件", "文件名只能是字母、数字、下划线，且不能以数字开头。")
            return
        self.save_if_modified()
        path = os.path.join(expressions_dir(), f"{name}.py")
        if os.path.exists(path):
            MessageBox.warning(self, "新建函数文件", f"文件已存在：{name}.py")
            self._reload_list(name)
            return
        _count, error = save_function_file(path, FUNCTION_TEMPLATE)
        if error is not None:
            MessageBox.warning(self, "新建函数文件", f"创建失败：\n{error}")
        self._reload_list(name)

    def _delete_file(self) -> None:
        current = self.file_list.currentItem()
        if current is None:
            return
        name = current.text()
        path = current.data(Qt.ItemDataRole.UserRole)
        if MessageBox.question(
            self,
            "删除函数文件",
            f"确定删除 {name}.py 吗？该文件注册的函数会被注销。",
            MessageBox.StandardButton.Yes | MessageBox.StandardButton.No,
            MessageBox.StandardButton.No,
        ) != MessageBox.StandardButton.Yes:
            return
        delete_function_file(path)
        self._current_path = None
        self._reload_list()

    def _save_current(self) -> bool:
        if not self._current_path:
            current = self.file_list.currentItem()
            if current is None:
                MessageBox.information(self, "自定义函数", "请先新建一个函数文件。")
                return False
            self._current_path = current.data(Qt.ItemDataRole.UserRole)
        return self._save_path(self._current_path, silent=False)

    def save_if_modified(self) -> None:
        """有未保存改动时静默落盘（切文件 / 关闭时用）。"""
        if self._current_path and self.editor.document().isModified():
            self._save_path(self._current_path, silent=True)

    def _save_path(self, path: str, silent: bool) -> bool:
        source = self.editor.toPlainText()
        count, error = save_function_file(path, source)
        name = os.path.splitext(os.path.basename(path))[0]
        if error is not None:
            self.status.setStyleSheet("color: #c0392b;")
            self.status.setText(f"保存失败（{name}.py），已写入文件但未注册：\n{error.strip()}")
            if not silent:
                MessageBox.warning(self, "自定义函数", f"函数有错误，未能注册：\n\n{error}")
            return False
        self.editor.document().setModified(False)
        self.status.setStyleSheet("color: #2e7d32;")
        self.status.setText(f"已保存并注册 {name}.py（{count} 个函数）")
        return True
