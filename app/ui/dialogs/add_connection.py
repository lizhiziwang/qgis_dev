"""「新建连接」Dialog：顶部 Tab 区分 **本地数据库 / 服务器数据库**。

- 本地数据库：本轮只需选一个本地库文件（.sqlite/.sqlite3/.db/.gpkg），
  选中后立即只读读取其 ``user_relation`` 注册目录（经
  :meth:`app.core.util.local_db_util.describe_local_db`，即
  ``local_db_util.query_all_table_name()`` 的三类表名 + 逐表行数），
  校验通过才允许确定。
- 服务器数据库：填写连接信息（主机 / 端口 / 数据库名 / 用户名 / 密码），
  顶部选**数据库类型**：PostgreSQL / Oracle / SQL Server —— 选类型自动带出
  默认端口 (5432 / 1521 / 1433)，必填项齐全且端口合法才可确定；
  服务器表目录浏览待后续版本接入。

确定后 :meth:`connection` 返回**当前激活页**（该校验通过才返回）的连接 dict::

    # 本地页
    {"kind": "local", "name": "<文件名>", "path": "<绝对路径>",
     "categories": {"spatial": [...], "dict": [...], "image": [...]},
     "counts": {表名: 行数|None},
     "meta": {表名: 元信息 dict},  # 几何类型/坐标系/字段列表, 见 describe_local_db
     "register_unregistered": True}  # 可选: 连接时把未登记矢量图层登记进数据管理

    # 服务器页
    {"kind": "server", "db_type": "postgresql|oracle|sqlserver",
     "db_type_label": "PostgreSQL|Oracle|SQL Server",
     "name": "<host>:<port>/<database>",
     "host": ..., "port": ..., "database": ...,
     "username": ..., "password": ...}
"""

from __future__ import annotations

from pathlib import Path

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
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

from app.core.domain.local_layer_register import (
    UnregisteredLayer,
    find_unregistered_spatial_layers,
)
from app.core.util.local_db_util import (
    _CATEGORY_LABEL,
    _CATEGORY_ORDER,
    describe_local_db,
)
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import open_file_dir

_FILE_FILTER = (
    "SQLite / GeoPackage (*.sqlite *.sqlite3 *.db *.gpkg);;所有文件 (*)"
)
_OK_GREEN = "#2e7d32"
_ERR_RED = "#c62828"
_GREY = "#888"

#: (db_type key, 显示名, 默认端口)
_SERVER_TYPES = [
    ("postgresql", "PostgreSQL", "5432"),
    ("oracle", "Oracle", "1521"),
    ("sqlserver", "SQL Server", "1433"),
]

#: **服务器数据库页签先隐藏**（用户要求：服务器版还没接入）。代码保留，
#: 把这个开关打开、并让数据管理树的 ``_SHOW_SERVER_DB`` 一起打开即可恢复。
_SHOW_SERVER_TAB = False


def _hint_label(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet("color: #666;")
    return lab


class AddConnectionDialog(BaseDialog):
    """新建数据库连接：本地 sqlite 文件 / 服务器(PostgreSQL·Oracle·SQL Server)。"""

    def __init__(self, parent: QWidget | None = None, *, default_kind: str = "local") -> None:
        """``default_kind`` 决定打开时停在哪个页签（``local`` / ``server``）。

        数据管理里在「服务器数据库」根节点上右键「添加数据库连接」，弹窗就该默认
        停在服务器页 —— 否则用户还得自己再点一下页签。
        """
        super().__init__(parent, title="新建连接")
        self.setModal(True)
        self.setMinimumSize(0, 0)  # 表单紧凑布局，不用 BaseDialog 默认 800×600
        self.resize(560, 440)
        self._default_kind = default_kind
        self._path = ""
        self._catalog: dict | None = None
        #: 选中库里未登记到数据管理的矢量表（文件校验时扫描）
        self._unregistered: list[UnregisteredLayer] = []
        self._build_ui()
        self.tabs.setCurrentIndex(1 if default_kind == "server" else 0)
        self._refresh_ok_state()

    # ============================================================== UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        # 顶部 Tab：本地数据库 / 服务器数据库（服务器页先隐藏，见 _SHOW_SERVER_TAB）
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_local_page(self.tabs), "本地数据库")
        self.tabs.addTab(self._build_server_page(self.tabs), "服务器数据库")
        self.tabs.setTabVisible(1, _SHOW_SERVER_TAB)
        self.tabs.currentChanged.connect(self._refresh_ok_state)
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
        # 手上还没有库的时候，在这儿就能建一个（不用先关掉弹窗去别处建）
        new_btn = QPushButton("新建数据库…")
        new_btn.setToolTip("建一个新的空 GeoPackage / SQLite 库，建完直接作为连接")
        new_btn.clicked.connect(self._on_create_database)
        row.addWidget(new_btn)
        v.addLayout(row)

        self.feedback = QLabel("")
        self.feedback.setWordWrap(True)
        v.addWidget(self.feedback)
        self.register_unregistered_check = QCheckBox("")
        self.register_unregistered_check.setToolTip(
            "只写数据管理使用的目录表 user_relation，不改动图层数据"
        )
        self.register_unregistered_check.setVisible(False)
        v.addWidget(self.register_unregistered_check)
        v.addStretch(1)
        return page

    def _build_server_page(self, parent: QWidget) -> QWidget:
        """服务器页：数据库类型(PostgreSQL/Oracle/SQL Server) + 连接字段。

        类型切换自动带出默认端口；主机/端口/数据库名/用户名为必填，端口需为
        1-65535 数字，全部通过后该页才允许确定（``connection`` 返回服务器 dict）。
        """
        page = QWidget(parent)
        v = QVBoxLayout(page)
        v.setContentsMargins(12, 14, 12, 8)
        v.setSpacing(8)

        v.addWidget(_hint_label(
            "填写服务器连接信息（支持 PostgreSQL / Oracle / SQL Server）。\n"
            "连接保存后在数据管理树「服务器数据库」下管理；表目录浏览待后续版本接入。"))

        form_wrap = QWidget()
        form = QFormLayout(form_wrap)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )

        # 数据库类型
        self.type_combo = QComboBox()
        self._type_specs: dict[str, tuple[str, str]] = {}
        for key, label, port in _SERVER_TYPES:
            self.type_combo.addItem(label, key)
            self._type_specs[key] = (label, port)
        form.addRow("数据库类型", self.type_combo)

        # 连接字段
        self.host_edit = QLineEdit("localhost")
        form.addRow("主机", self.host_edit)
        self.port_edit = QLineEdit("5432")
        form.addRow("端口", self.port_edit)
        self.db_edit = QLineEdit()
        self.db_edit.setPlaceholderText("数据库名 / Oracle 服务名")
        form.addRow("数据库名", self.db_edit)
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("登录用户名")
        form.addRow("用户名", self.user_edit)
        self.pwd_edit = QLineEdit()
        self.pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pwd_edit.setPlaceholderText("密码（可选）")
        form.addRow("密码", self.pwd_edit)

        # 信号接线：类型切换带默认端口；任一必填/端口变更刷新校验
        self.type_combo.currentIndexChanged.connect(self._on_server_type_changed)
        for edit in (self.host_edit, self.port_edit, self.db_edit, self.user_edit):
            edit.textChanged.connect(self._refresh_ok_state)
        self.pwd_edit.textChanged.connect(self._refresh_ok_state)

        v.addWidget(form_wrap)
        self.server_feedback = QLabel("")
        self.server_feedback.setWordWrap(True)
        v.addWidget(self.server_feedback)
        v.addStretch(1)
        return page

    # ============================================================== 交互

    def _on_browse(self) -> None:

        path = open_file_dir(self, title="选择 SQLite 数据库文件", filter_=_FILE_FILTER)
        if path:
            self.set_sqlite_file(path)

    def _on_create_database(self) -> None:
        """「新建数据库…」：开「创建数据」对话框的「本地数据库」页，建完直接选为连接。

        建库仍走 :class:`~app.core.domain.create_data.CreateContainerService`（对话框里
        点确定才是真正的落盘）；这里只把它建出来的文件路径填进本弹窗，
        注册到数据管理由本弹窗的「确定」统一完成（**不重复登记**）。
        """
        from qgis.PyQt.QtWidgets import QDialog

        from app.ui.dialogs.create_data_dialog import CreateDataDialog

        dialog = CreateDataDialog(self, start_on_container=True)
        accepted = dialog.exec()
        outcome = dialog.outcome() if accepted else None
        dialog.deleteLater()
        if outcome is None or not outcome.container_path:
            return
        self.set_sqlite_file(outcome.container_path)

    def set_sqlite_file(self, path: str) -> bool:
        """选择/校验本地库文件；读取注册目录成功则启用确定并返回 True。"""
        if not path:
            return False
        try:
            catalog = describe_local_db(path)
        except ValueError as exc:
            self._path = ""
            self._catalog = None
            self._unregistered = []
            self._update_unregistered_check()
            self.path_edit.setText(path)
            self._set_feedback(str(exc), error=True)
            self._refresh_ok_state()
            return False
        self._path = str(Path(path))
        self._catalog = catalog
        # 库里可能有 OGR / 其它工具写出的图层，但没有本工程的注册行 ——
        # 扫描出来给用户勾选，连接时一并登记（数据管理只认 user_relation）。
        try:
            self._unregistered = find_unregistered_spatial_layers(path)
        except Exception:  # noqa: BLE001 —— 扫描失败只影响提示，不影响连接
            self._unregistered = []
        self.path_edit.setText(self._path)
        cats = catalog["categories"]
        total = sum(len(cats.get(k) or []) for k in _CATEGORY_ORDER)
        brief = " · ".join(
            f"{_CATEGORY_LABEL[k]} {len(cats.get(k) or [])}"
            for k in _CATEGORY_ORDER
        )
        extras = (
            f"另有 {len(self._unregistered)} 个未登记矢量图层。" if self._unregistered else ""
        )
        if total:
            text = f"识别到 {total} 个已注册图层（{brief}），可连接。{extras}"
        elif self._unregistered:
            text = f"文件里有 {len(self._unregistered)} 个未登记矢量图层，可连接并登记到数据管理。"
        else:
            text = "该文件暂无已注册图层，可连接后再创建/导入数据。"
        self._set_feedback(text, error=False)
        self._update_unregistered_check()
        self._refresh_ok_state()
        return True

    def _update_unregistered_check(self) -> None:
        """按扫描结果更新「同时登记未登记图层」勾选框。"""
        count = len(self._unregistered)
        check = getattr(self, "register_unregistered_check", None)
        if check is None:
            return
        check.setVisible(count > 0)
        check.setChecked(count > 0)
        check.setText(f"把 {count} 个未登记矢量图层登记到数据管理")

    def _set_feedback(self, text: str, *, error: bool) -> None:
        self.feedback.setText(text)
        self.feedback.setStyleSheet(
            f"color: {_ERR_RED if error else _OK_GREEN};"
        )

    # ------------------------------------------------ 服务器表单校验

    def _on_server_type_changed(self, *_args) -> None:
        """切换数据库类型 → 端口带默认值并刷新校验。"""
        if getattr(self, "port_edit", None) is None:
            return
        key = self.type_combo.currentData()
        spec = self._type_specs.get(key)
        if spec:
            self.port_edit.setText(spec[1])
        self._refresh_ok_state()

    def _server_state(self) -> tuple[bool, str, bool]:
        """服务器页校验: (通过?, 提示文本, 提示是否红色错误)。"""
        host = self.host_edit.text().strip()
        db = self.db_edit.text().strip()
        user = self.user_edit.text().strip()
        port = self.port_edit.text().strip()
        if not (host and db and user and port):
            return False, "必填：主机 / 端口 / 数据库名 / 用户名", False
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            return False, "端口需为 1-65535 的数字", True
        return True, "连接信息完整。", False

    def _refresh_ok_state(self, *_args) -> None:
        """按激活页刷新「确定」：本地=文件校验通过; 服务器=必填齐全且端口合法。"""
        if not hasattr(self, "bb"):
            return
        ok_btn = self.bb.button(QDialogButtonBox.StandardButton.Ok)
        if self.tabs.currentIndex() == 1:
            ok, msg, red = self._server_state()
            self.server_feedback.setText(msg)
            self.server_feedback.setStyleSheet(
                f"color: {_ERR_RED if red else (_OK_GREEN if ok else _GREY)};"
            )
            ok_btn.setEnabled(ok)
        else:
            self.server_feedback.clear()
            ok_btn.setEnabled(bool(self._path and self._catalog))

    # ============================================================== 取值

    def connection(self) -> dict | None:
        """当前激活页的连接 dict；该页未通过校验时为 None。"""
        if self.tabs.currentIndex() == 1:
            ok, _, _ = self._server_state()
            if not ok:
                return None
            return self._server_connection()
        if not self._path or not self._catalog:
            return None
        return self._local_connection()

    def _local_connection(self) -> dict:
        """本地页连接 dict（须已通过 set_sqlite_file 校验）。"""
        conn = {
            "kind": "local",
            "name": Path(self._path).stem,
            "path": self._path,
            "categories": self._catalog["categories"],
            "counts": self._catalog["counts"],
            "meta": self._catalog["meta"],
        }
        # 只有扫描到未登记图层、且用户保留勾选时才带这个标记；
        # DataManageDock.add_connection 登记后会把它从连接 dict 里摘掉。
        if self._unregistered and self.register_unregistered_check.isChecked():
            conn["register_unregistered"] = True
        return conn

    def _server_connection(self) -> dict:
        """服务器页连接 dict（须已通过 _server_state 校验）。"""
        host = self.host_edit.text().strip()
        port = self.port_edit.text().strip()
        database = self.db_edit.text().strip()
        user = self.user_edit.text().strip()
        key = self.type_combo.currentData() or _SERVER_TYPES[0][0]
        label, _ = self._type_specs.get(key, (_SERVER_TYPES[0][1], ""))
        return {
            "kind": "server",
            "db_type": key,
            "db_type_label": label,
            "name": f"{host}:{port}/{database}",
            "host": host,
            "port": port,
            "database": database,
            "username": user,
            "password": self.pwd_edit.text(),
        }


__all__ = ["AddConnectionDialog"]
