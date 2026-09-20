"""启动登录对话框（账号校验走授权服务器）。

启动 :func:`app.bootstrap.main` 时，**在** :func:`QgsApplication.initQgis`
**之前**弹出本对话框；登录成功后才真正初始化 QGIS / 进入主窗口。

鉴权方式：

- 用户名 / 密码提交到**授权服务器**换取 token。接口地址与请求/响应契约集中在
  :mod:`app.core.auth.config`（部署只改那一个文件），HTTP 客户端是
  :class:`app.core.auth.client.AuthClient`（仅标准库 ``urllib``，无第三方依赖）。
- 网络请求在**后台线程**里跑（普通 :class:`threading.Thread`，与
  ``attribute_dock.FeatureStreamWorker`` 同一套路，不用 QThread / QgsTask），
  界面只切「登录中…」忙碌态、不冻结；结果经 Qt 信号回到主线程再更新 UI。
- 任何失败（网络不通 / 超时 / 账号密码错）都把 :class:`AuthError` 的中文文案
  显示在错误行上，**不做本地兜底账号**。

"记住账号"：登录成功后把本次用户名/密码交给
:mod:`app.core.auth.saved_logins` 保存（Windows 上密码用 DPAPI 加密）；下次
点击用户名输入框时弹 :class:`app.ui.dialogs.saved_login_popup.SavedLoginPopup`
气泡列出已保存账号，选中即同时回填用户名与密码。气泡是独立的 ``Qt.Popup``
窗口，**不影响本对话框任何像素**（空输入框、无气泡时的外观与之前完全一致）。

取消 / 关闭 → :meth:`reject`，外层 :func:`bootstrap.main` 据此 ``return 0`` 退出。
"""

from __future__ import annotations

import threading
from typing import cast

from qgis.PyQt.QtCore import QEvent, QObject, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.auth.client import AuthClient, AuthError, AuthResult
from app.core.auth.saved_logins import (
    clear_saved_logins,
    load_saved_logins,
    password_protection,
    remove_login,
    save_login,
)
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.icons import Icons
from app.ui.common.theme import ThemeManager, login_qss
from app.ui.dialogs.saved_login_popup import SavedLoginPopup, login_of

logger = get_logger(__name__)


class LoginDialog(BaseDialog):
    """启动登录对话框：向授权服务器校验账号密码，成功后 :meth:`accept`。"""

    #: 登录成功（后台线程 → 主线程）：``(尝试序号, AuthResult)``
    _login_succeeded = pyqtSignal(int, object)
    #: 登录失败（后台线程 → 主线程）：``(尝试序号, 可直接显示的文案)``
    _login_failed = pyqtSignal(int, str)

    def __init__(
        self,
        parent: QWidget | None = None,
        app_name: str = "qgis_dev",
        auth_client: AuthClient | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"登录 - {app_name}")
        self.setModal(True)
        self.setMinimumWidth(400)
        # 不显示问号按钮（无边框标题栏本来就没有原生问号，保留该行无害且保险）
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        self._app_name = app_name
        #: 授权客户端（可注入便于测试；默认按 app/core/auth/config.py 的地址）
        self._client = auth_client if auth_client is not None else AuthClient()

        self._username: str = ""
        self._auth_result: AuthResult | None = None
        #: 本次输入的密码（登录成功后交给 saved_logins 保存，随后清掉）
        self._pending_password: str = ""
        #: 本机保存过的账号（点击用户名输入框时气泡列出）
        self._saved_logins = load_saved_logins()
        #: 账号气泡（懒创建；仅在用户点击用户名输入框时出现）
        self._popup: SavedLoginPopup | None = None
        #: 是否正在请求授权服务器（忙碌态）
        self._busy = False
        #: 尝试序号：取消 / 重新发起后自增，用来丢弃在途的后台结果
        self._attempt = 0
        self._thread: threading.Thread | None = None

        self._login_succeeded.connect(self._on_login_succeeded)
        self._login_failed.connect(self._on_login_failed)

        self._build_ui()
        self.username_edit.setFocus()

    # ============================================================== UI

    def _build_ui(self) -> None:
        """现代简洁卡片式登录界面（与启动 splash 视觉统一）。

        结构：品牌区（图标 + 应用名 + 副标题 + 强调线）→ 卡片表单（字段标签
        在输入框上方、圆角输入框）→ 右下角操作（取消 + 主色登录按钮）。
        配色全部来自当前主题（:func:`app.ui.common.theme.login_qss`），内容走
        无边框标题栏（:class:`app.ui.common.base_dialog.BaseDialog`）。
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
        # 点击用户名输入框 → 弹「已保存账号」气泡（由 eventFilter 捕获）
        self.username_edit.installEventFilter(self)
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

        self._cancel_btn = QPushButton("取消", self)
        self._cancel_btn.setObjectName("LoginCancel")
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self._login_btn = QPushButton("登  录", self)
        self._login_btn.setObjectName("LoginPrimary")
        self._login_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._login_btn.setDefault(True)
        self._login_btn.setAutoDefault(True)

        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._login_btn)
        layout.addLayout(btn_row)

        self._cancel_btn.clicked.connect(self.reject)
        self._login_btn.clicked.connect(self._on_accept)
        # 密码框回车直接提交；用户名框回车先跳到密码框
        self.password_edit.returnPressed.connect(self._on_accept)
        self.username_edit.returnPressed.connect(self._focus_password)
        self.setLayout(layout)
        self._sync_username_hint()

    # ============================================================== 行为

    def _on_accept(self) -> None:
        """校验输入 → 切忙碌态 → 后台线程请求授权服务器。"""
        if self._busy:
            return
        username = self.username_edit.text().strip()
        password = self.password_edit.text()
        if not username or not password:
            self._show_error("用户名和密码不能为空")
            return

        self._attempt += 1
        self._show_error("")
        self._pending_password = password
        self._set_busy(True)
        self._thread = threading.Thread(
            target=self._login_in_background,
            args=(self._attempt, username, password),
            name="qgis-dev-auth-login",
            daemon=True,
        )
        self._thread.start()

    def _login_in_background(self, attempt: int, username: str, password: str) -> None:
        """后台线程体：只发请求 + emit 信号，不碰任何控件（Qt 控件非线程安全）。"""
        try:
            result = self._client.login(username, password)
        except AuthError as exc:
            self._login_failed.emit(attempt, str(exc))
        except Exception as exc:  # noqa: BLE001 - 线程里不能把异常丢掉，兜底转成提示
            self._login_failed.emit(attempt, f"登录失败：{exc}")
        else:
            self._login_succeeded.emit(attempt, result)

    def _on_login_succeeded(self, attempt: int, result: object) -> None:
        """主线程槽：登录成功 → 记录结果并关闭对话框。"""
        if attempt != self._attempt:
            return  # 用户已取消 / 重新发起，丢弃过期结果
        res = cast(AuthResult, result)
        self._auth_result = res
        self._username = res.username or self.username_edit.text().strip()
        self._remember_credentials()
        self._set_busy(False)
        self.accept()

    def _on_login_failed(self, attempt: int, message: str) -> None:
        """主线程槽：登录失败 → 恢复可编辑并显示服务端文案。"""
        if attempt != self._attempt:
            return
        self._set_busy(False)
        self._show_error(message)
        self.password_edit.setFocus()
        self.password_edit.selectAll()

    def _set_busy(self, busy: bool) -> None:
        """切换「登录中…」状态：禁输入框与主按钮、换按钮文案、忙碌光标。

        取消按钮**保持可用**——用户可随时放弃等待，在途结果由 :meth:`reject`
        自增 ``_attempt`` 丢弃（后台线程是 daemon，超时后自然结束）。
        """
        self._busy = busy
        self.username_edit.setEnabled(not busy)
        self.password_edit.setEnabled(not busy)
        self._login_btn.setEnabled(not busy)
        self._login_btn.setText("登录中…" if busy else "登  录")
        if busy:
            self.setCursor(Qt.CursorShape.BusyCursor)
        else:
            self.unsetCursor()

    def _focus_password(self) -> None:
        """用户名框回车 → 跳到密码框（而不是直接提交空密码）。"""
        self.password_edit.setFocus()

    # ============================================================== 记住账号

    def _remember_credentials(self) -> None:
        """登录成功后记住本次输入的账号密码（失败只记日志，绝不能挡住登录）。"""
        username = self.username_edit.text().strip()
        password = self._pending_password
        self._pending_password = ""
        if not username or not password:
            return
        try:
            entry = save_login(username, password)
        except Exception:  # 记不住密码不能影响登录
            logger.warning("保存登录账号失败", exc_info=True)
            return
        if entry is not None:
            logger.info("已记住账号 %s（密码保护方式：%s）", entry.username, password_protection())

    def _sync_username_hint(self) -> None:
        """按“有没有保存过账号”维护用户名框的悬浮提示。"""
        self.username_edit.setToolTip("点击可选择已保存的账号" if self._saved_logins else "")

    def _show_saved_logins(self) -> None:
        """弹出「已保存账号」气泡（无保存记录 / 忙碌时不弹）。"""
        if self._busy or not self._saved_logins:
            return
        if self._popup is None:
            self._popup = SavedLoginPopup(self)
            self._popup.picked.connect(self._on_saved_login_picked)
            self._popup.removed.connect(self._on_saved_login_removed)
            self._popup.cleared.connect(self._on_saved_logins_cleared)
        self._popup.set_logins(self._saved_logins)
        self._popup.show_for(self.username_edit, ThemeManager.instance().current())

    def _on_saved_login_picked(self, login: object) -> None:
        """气泡里选中一条 → 同时回填用户名与密码。"""
        entry = login_of(login)
        if entry is None:
            return
        self.username_edit.setText(entry.username)
        self.password_edit.setText(entry.password)
        self._show_error("")
        self.password_edit.setFocus()
        self.password_edit.setCursorPosition(len(entry.password))

    def _on_saved_login_removed(self, username: str) -> None:
        """删掉一条（气泡不关，方便连续清理）。"""
        if remove_login(username):
            logger.info("已删除保存的账号 %s", username)
        self._reload_saved_logins()

    def _on_saved_logins_cleared(self) -> None:
        """清空全部已保存账号。"""
        if clear_saved_logins():
            logger.info("已清空保存的账号")
        self._reload_saved_logins()

    def _reload_saved_logins(self) -> None:
        """重新读取保存列表；没剩下就关掉气泡。"""
        self._saved_logins = load_saved_logins()
        self._sync_username_hint()
        if self._popup is None:
            return
        if self._saved_logins:
            self._popup.set_logins(self._saved_logins)
            self._popup.show_for(self.username_edit, ThemeManager.instance().current())
        else:
            self._popup.close()

    def _show_error(self, msg: str) -> None:
        self.error_label.setText(msg)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """用户名输入框被点击 → 弹「已保存账号」气泡。

        返回 ``False`` 让点击继续走输入框自己的处理（光标定位 / 选中）；气泡是
        ``Qt.Popup``，弹出后鼠标按下会先被它接管，不会误触到下面的输入框。
        """
        if (
            obj is self.username_edit
            and event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._show_saved_logins()
        return super().eventFilter(obj, event)

    def reject(self) -> None:
        """取消 / 关闭（标题栏 ✕）→ 作废在途登录结果后关闭。"""
        self._attempt += 1
        self._busy = False
        self._pending_password = ""
        super().reject()

    # ============================================================== 公共 API

    def username(self) -> str:
        """登录成功后的用户名（失败 / 取消时为空串）。"""
        return self._username

    def token(self) -> str:
        """授权服务器签发的 token（失败 / 取消时为空串）。"""
        return self._auth_result.token if self._auth_result else ""

    def auth_result(self) -> AuthResult | None:
        """完整登录结果（含 ``expires_in`` / 原始响应）；未登录成功时为 ``None``。"""
        return self._auth_result
