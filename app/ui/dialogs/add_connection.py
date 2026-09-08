"""「新建连接」Dialog：顶部 Tab 区分 **本地数据库 / 服务器数据库**。

- 本地数据库：本轮只需选一个本地库文件（.sqlite/.sqlite3/.db/.gpkg），
  选中后立即只读读取其 ``user_relation`` 注册目录（经
  :meth:`app.core.util.local_db_util.describe_local_db`，即
  ``local_db_util.query_all_table_name()`` 的三类表名 + 逐表行数），
  校验通过才允许确定。
- 服务器数据库：**本轮搁置**——页面只放说明 + 置灰的预留表单，暂不可提交。

确定后 :meth:`connection` 返回可直接入数据管理树的连接 dict::

    {"kind": "local", "name": "<文件名>", "path": "<绝对路径>",
     "categories": {"spatial": [...], "dict": [...], "image": [...]},
     "counts": {表名: 行数|None},
     "meta": {表名: 元信息 dict}}  # 几何类型/坐标系/字段列表, 见 describe_local_db

服务器连接待后续版本实现，不在本对话框收集字段。
"""

from __future__ import annotations

from pathlib import Path

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.util.local_db_util import (
    _CATEGORY_LABEL,
    _CATEGORY_ORDER,
    describe_local_db,
)
from app.ui.common.base_dialog import BaseDialog

_FILE_FILTER = (
    "SQLite / GeoPackage (*.sqlite *.sqlite3 *.db *.gpkg);;所有文件 (*)"
)
_OK_GREEN = "#2e7d32"
_ERR_RED = "#c62828"


def _hint_label(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet("color: #666;")
    return lab


class AddConnectionDialog(BaseDialog):
    """新建数据库连接（本地 sqlite 文件选择；服务器预留）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, title="新建连接")
        self.setModal(True)
        self.setMinimumSize(0, 0)  # 表单紧凑布局，不用 BaseDialog 默认 800×600
        self.resize(560, 440)
        self._path = ""
        self._catalog: dict | None = None
        self._build_ui()

    # ============================================================== UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        # 顶部 Tab：本地数据库 / 服务器数据库
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_local_page(self.tabs), "本地数据库")
        self.tabs.addTab(self._build_server_page(self.tabs), "服务器数据库")
        layout.addWidget(self.tabs, 1)

        # 底部按钮（确定只在本地文件校验通过后可用）
        self.bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        ok_btn = self.bb.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setDefault(True)
        ok_btn.setEnabled(False)
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        layout.addWidget(self.bb)

        self.setLayout(layout)

    def _build_local_page(self, parent: QWidget) -> QWidget:
        page = QWidget(parent)
        v = QVBoxLayout(page)
        v.setContentsMargins(12, 14, 12, 8)
        v.setSpacing(8)

        v.addWidget(_hint_label("选择要连接的本地数据库文件（SQLite / "
                                "GeoPackage / SpatiaLite），确定后加入左侧数据库树。"))

        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("未选择文件")
        row.addWidget(self.path_edit, 1)
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self._on_browse)
        row.addWidget(browse_btn)
        v.addLayout(row)

        self.feedback = QLabel("")
        self.feedback.setWordWrap(True)
        v.addWidget(self.feedback)
        v.addStretch(1)
        return page

    def _build_server_page(self, parent: QWidget) -> QWidget:
        page = QWidget(parent)
        v = QVBoxLayout(page)
        v.setContentsMargins(12, 14, 12, 8)
        v.setSpacing(8)

        notice = QLabel("服务器数据库连接暂未开放（本轮先搁置），后续版本接入，"
                        "此处仅预览预留字段，不可编辑、不可提交。")
        notice.setWordWrap(True)
        v.addWidget(notice)

        # 置灰的预留表单：只展示未来结构，不收集（加完行再整体禁用）
        form_wrap = QWidget()
        form = QFormLayout(form_wrap)
        fields = [
            ("主机", "localhost"),
            ("端口", "5432"),
            ("数据库名", "postgres"),
            ("用户名", "postgres"),
            ("密码", "••••••••"),
        ]
        for label, placeholder in fields:
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            form.addRow(label, edit)
        form_wrap.setEnabled(False)
        v.addWidget(form_wrap)
        v.addStretch(1)
        return page

    # ============================================================== 交互

    def _on_browse(self) -> None:
        from app.ui.common.widgets import open_file_dir

        path = open_file_dir(self, title="选择 SQLite 数据库文件", filter_=_FILE_FILTER)
        if path:
            self.set_sqlite_file(path)

    def set_sqlite_file(self, path: str) -> bool:
        """选择/校验本地库文件；读取注册目录成功则启用确定并返回 True。"""
        if not path:
            return False
        try:
            catalog = describe_local_db(path)
        except ValueError as exc:
            self._path = ""
            self._catalog = None
            self.path_edit.setText(path)
            self._set_feedback(str(exc), error=True)
            self.bb.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            return False
        self._path = str(Path(path))
        self._catalog = catalog
        self.path_edit.setText(self._path)
        cats = catalog["categories"]
        total = sum(len(cats.get(k) or []) for k in _CATEGORY_ORDER)
        brief = " · ".join(
            f"{_CATEGORY_LABEL[k]} {len(cats.get(k) or [])}"
            for k in _CATEGORY_ORDER
        )
        text = (
            f"识别到 {total} 个已注册图层（{brief}），可连接。"
            if total
            else "该文件暂无已注册图层，可连接后再创建/导入数据。"
        )
        self._set_feedback(text, error=False)
        self.bb.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        return True

    def _set_feedback(self, text: str, *, error: bool) -> None:
        self.feedback.setText(text)
        self.feedback.setStyleSheet(
            f"color: {_ERR_RED if error else _OK_GREEN};"
        )

    # ============================================================== 取值

    def connection(self) -> dict | None:
        """校验通过的连接 dict；未选文件/未通过校验时为 None。"""
        if not self._path or not self._catalog:
            return None
        return {
            "kind": "local",
            "name": Path(self._path).stem,
            "path": self._path,
            "categories": self._catalog["categories"],
            "counts": self._catalog["counts"],
            "meta": self._catalog["meta"],
        }


__all__ = ["AddConnectionDialog"]
