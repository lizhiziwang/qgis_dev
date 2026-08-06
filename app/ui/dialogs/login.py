"""启动登录对话框。

启动 :func:`app.bootstrap.main` 时，**在** :func:`QgsApplication.initQgis`
**之前**弹出本对话框，校验通过后才真正初始化 QGIS / 进入主窗口。

设计：

- 只依赖 PyQt5，不引入 ``qgis.*``，避免登录阶段拖 QGIS 启动开销。
- 默认账号 ``admin / admin``（生产环境请接真实 auth 服务 / 配置文件）。
- 取消 → :meth:`reject`，外层 :func:`bootstrap.main` 据此 ``return 0`` 退出。
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from app.ui.common.icons import Icons
from app.ui.common.styles import DIALOG_BASE_QSS

# 默认账号（dev / 占位用；生产请替换为读配置 / 调 auth 服务）
_DEFAULT_USER = "admin"
_DEFAULT_PASSWORD = "admin"


class LoginDialog(QDialog):
    """启动登录对话框。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("登录 - qgis_dev")
        self.setWindowIcon(Icons.app())
        self.setModal(True)
        self.setMinimumWidth(400)
        # 不显示问号按钮
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        self._username: str = ""

        self._build_ui()

    # ============================================================== UI

    def _build_ui(self) -> None:
        # 与主系统对话框一致的字体 / 输入框 / 按钮；二级文字、错误提示走 object-name 规则，
        # 这样它们仍继承 DIALOG_BASE_QSS 的字体，不会退回系统默认。
        self.setStyleSheet(
            DIALOG_BASE_QSS
            + """
              QLabel#loginSubtitle { color: #666; }
              QLabel#loginError { color: #d33; font-weight: bold; }
              """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        # 标题
        title = QLabel("qgis_dev")
        title_font = QFont()
        title_font.setPointSize(18)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("矢量数据处理工具")
        subtitle.setObjectName("loginSubtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle)

        layout.addSpacing(8)

        # 表单
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(8)

        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("请输入用户名")
        self.username_edit.setText(_DEFAULT_USER)  # dev 模式默认填上
        form.addRow("用户名：", self.username_edit)

        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("请输入密码")
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setText(_DEFAULT_PASSWORD)  # dev 模式默认填上
        form.addRow("密  码：", self.password_edit)

        layout.addLayout(form)

        # 错误标签（初始隐藏）
        self.error_label = QLabel("")
        self.error_label.setObjectName("loginError")
        self.error_label.setAlignment(Qt.AlignCenter)
        self.error_label.setMinimumHeight(20)
        layout.addWidget(self.error_label)

        layout.addStretch(1)

        # 按钮
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("登录")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        # 默认焦点在"登录"按钮上，回车直接登录
        buttons.button(QDialogButtonBox.Ok).setDefault(True)
        buttons.button(QDialogButtonBox.Ok).setAutoDefault(True)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # 让密码框回车直接触发 accept
        self.password_edit.returnPressed.connect(self._on_accept)

    # ============================================================== 行为

    def _on_accept(self) -> None:
        username = self.username_edit.text().strip()
        password = self.password_edit.text()
        if not username or not password:
            self._show_error("用户名和密码不能为空")
            return
        if username != _DEFAULT_USER or password != _DEFAULT_PASSWORD:
            self._show_error("用户名或密码错误")
            return
        self._username = username
        self.accept()

    def _show_error(self, msg: str) -> None:
        self.error_label.setText(msg)

    # ============================================================== 公共 API

    def username(self) -> str:
        """登录成功后的用户名。"""
        return self._username
