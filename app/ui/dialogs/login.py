"""启动登录对话框。

启动 :func:`app.bootstrap.main` 时，**在** :func:`QgsApplication.initQgis`
**之前**弹出本对话框，校验通过后才真正初始化 QGIS / 进入主窗口。

设计：

- 只依赖 qgis.PyQt（QGIS 自带绑定），不引入其他 qgis 模块，避免登录阶段拖 QGIS 启动开销。
- 默认账号来自 ``config/app.yaml`` 的 ``auth`` 段（bootstrap 传入）；未传时兜底
  ``admin / admin``（生产环境请接真实 auth 服务 / 配置文件）。
- 取消 → :meth:`reject`，外层 :func:`bootstrap.main` 据此 ``return 0`` 退出。
"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.common.base_dialog import BaseDialog
from app.ui.common.icons import Icons
from app.ui.common.theme import ThemeManager, login_qss

# 兜底账号（无 config / 未传入时的默认值；实际值由 bootstrap 从 app.yaml 传入）
_DEFAULT_USER = "admin"
_DEFAULT_PASSWORD = "admin"


class LoginDialog(BaseDialog):
    """启动登录对话框。"""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        default_user: str = _DEFAULT_USER,
        default_password: str = _DEFAULT_PASSWORD,
        app_name: str = "qgis_dev",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"登录 - {app_name}")
        self.setModal(True)
        self.setMinimumWidth(400)
        # 不显示问号按钮（无边框标题栏本来就没有原生问号，保留该行无害且保险）
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        # 本地校验账号（来自 config/app.yaml → bootstrap 传入）
        self._default_user = default_user
        self._default_password = default_password
        self._app_name = app_name

        self._username: str = ""

        self._build_ui()

    # ============================================================== UI

    def _build_ui(self) -> None:
        """现代简洁卡片式登录界面（与启动 splash 视觉统一）。

        结构：品牌区（图标 + 应用名 + 副标题 + 强调线）→ 卡片表单（字段标签
        在输入框上方、圆角输入框）→ 右下角操作（取消 + 主色登录按钮）。
        配色全部来自当前主题（:func:`app.ui.common.theme.login_qss`），保留
        系统原生窗口边框 → 既是系统程序对话框、内容又是现代化简洁风格。
        """

        self.setObjectName("LoginDialog")
        self.setFixedWidth(400)
        self.setStyleSheet(login_qss(ThemeManager.instance().current()))

        layout = QVBoxLayout()
        layout.setContentsMargins(32, 28, 32, 24)
        layout.setSpacing(10)

        # ---- 品牌区 ----
        icon = QLabel(self)
        icon.setPixmap(Icons.app().pixmap(56, 56))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)

        title = QLabel(self._app_name, self)
        title.setObjectName("LoginTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("登录您的账号以继续", self)
        subtitle.setObjectName("LoginSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        accent = QLabel(self)
        accent.setObjectName("LoginAccent")
        accent.setFixedSize(48, 2)
        layout.addWidget(accent, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout.addSpacing(16)

        # ---- 卡片表单 ----
        card = QFrame(self)
        card.setObjectName("LoginCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(6)

        user_label = QLabel("用户名", card)
        user_label.setObjectName("LoginFieldLabel")
        card_layout.addWidget(user_label)

        self.username_edit = QLineEdit(card)
        self.username_edit.setObjectName("LoginField")
        self.username_edit.setPlaceholderText("请输入用户名")
        card_layout.addWidget(self.username_edit)

        card_layout.addSpacing(10)

        pwd_label = QLabel("密码", card)
        pwd_label.setObjectName("LoginFieldLabel")
        card_layout.addWidget(pwd_label)

        self.password_edit = QLineEdit(card)
        self.password_edit.setObjectName("LoginField")
        self.password_edit.setPlaceholderText("请输入密码")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        card_layout.addWidget(self.password_edit)

        # 错误提示（占位预留高度，出现时布局不跳动）
        self.error_label = QLabel("", card)
        self.error_label.setObjectName("loginError")
        self.error_label.setMinimumHeight(18)
        card_layout.addWidget(self.error_label)

        layout.addWidget(card)
        layout.addStretch(1)

        # ---- 操作区（右下角，符合系统对话框习惯）----
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch(1)

        cancel_btn = QPushButton("取消", self)
        cancel_btn.setObjectName("LoginCancel")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        login_btn = QPushButton("登  录", self)
        login_btn.setObjectName("LoginPrimary")
        login_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        login_btn.setDefault(True)
        login_btn.setAutoDefault(True)

        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(login_btn)
        layout.addLayout(btn_row)

        cancel_btn.clicked.connect(self.reject)
        login_btn.clicked.connect(self._on_accept)
        # 密码框回车直接触发登录
        self.password_edit.returnPressed.connect(self._on_accept)
        self.setLayout(layout)

    # ============================================================== 行为

    def _on_accept(self) -> None:
        username = self.username_edit.text().strip()
        password = self.password_edit.text()
        if not username or not password:
            self._show_error("用户名和密码不能为空")
            return
        if username != self._default_user or password != self._default_password:
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
