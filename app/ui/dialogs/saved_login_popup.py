"""用户名输入框下方的"已保存账号"气泡。

从 :mod:`app.core.auth.saved_logins` 读出本机保存过的账号（用户名 + 密码），
点击用户名输入框时在它下面弹一个小气泡：

- 点某一行 → 发出 :attr:`SavedLoginPopup.picked`，登录框**同时回填用户名和密码**；
- 行尾 ``✕`` → 发出 :attr:`SavedLoginPopup.removed`，删掉这一条；
- 底部"清除全部" → 发出 :attr:`SavedLoginPopup.cleared`。

实现约定：

- 是 ``Qt.Popup`` 顶层窗口（点外面 / ``Esc`` 自动关），所以**不会改变登录框
  本身的任何像素**——登录框只在界面之外多一个临时窗口。
- 行用 ``QListWidget`` + ``setItemWidget``（用户名 + 掩码密码 + ✕）；行高走
  ``item.setSizeHint``，**不用 QSS ``::item { padding }``**（那会让绘制区比行距高，
  相邻行互相覆盖）。
- 配色由 :func:`app.ui.common.theme.saved_login_popup_qss` 提供，跟随当前主题。
"""

from __future__ import annotations

from collections.abc import Sequence

from qgis.PyQt.QtCore import QPoint, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QKeyEvent, QPainter, QPaintEvent
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStyle,
    QStyleOption,
    QVBoxLayout,
    QWidget,
)

from app.core.auth.saved_logins import SavedLogin
from app.ui.common.theme import Theme, saved_login_popup_qss

#: 单行高度（像素）
ROW_HEIGHT = 30

#: 气泡最小宽度（输入框更宽时跟随输入框）
MIN_WIDTH = 260

#: 最多直接显示几行，超出滚动
MAX_VISIBLE_ROWS = 6

_ROLE_LOGIN = Qt.ItemDataRole.UserRole


class SavedLoginPopup(QWidget):
    """已保存账号气泡（``Qt.Popup``，无边框，主题配色）。"""

    #: 选中一条（参数是 :class:`app.core.auth.saved_logins.SavedLogin`，用 ``object`` 过信号）
    picked = pyqtSignal(object)
    #: 删除一条（参数是用户名）
    removed = pyqtSignal(str)
    #: 清除全部
    cleared = pyqtSignal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("SavedLoginPopup")
        # WA_StyledBackground：QWidget 子类默认不画 QSS 背景
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # 圆角靠 QSS border-radius，必须半透明底才能露出圆角
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        header = QLabel("最近登录的账号", self)
        header.setObjectName("SavedLoginHeader")
        layout.addWidget(header)

        self._list = QListWidget(self)
        self._list.setObjectName("SavedLoginList")
        self._list.setSpacing(2)
        self._list.setUniformItemSizes(True)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._list.itemClicked.connect(self._on_item)
        self._list.itemActivated.connect(self._on_item)
        layout.addWidget(self._list)

        self._clear_btn = QPushButton("清除全部", self)
        self._clear_btn.setObjectName("SavedLoginClear")
        self._clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_btn.clicked.connect(self._on_clear)
        layout.addWidget(self._clear_btn)

    # ============================================================== 填充 / 弹出

    def set_logins(self, logins: Sequence[SavedLogin]) -> None:
        """重建列表（每次弹出前调用，保证内容是最新的）。"""
        self._list.clear()
        for login in logins:
            item = QListWidgetItem(self._list)
            item.setData(_ROLE_LOGIN, login)
            item.setSizeHint(QSize(0, ROW_HEIGHT))
            self._list.addItem(item)
            self._list.setItemWidget(item, self._build_row(login))
        rows = min(max(len(logins), 1), MAX_VISIBLE_ROWS)
        self._list.setFixedHeight(rows * ROW_HEIGHT + (rows - 1) * self._list.spacing())

    def show_for(self, anchor: QWidget, theme: Theme) -> None:
        """在 ``anchor`` 正下方弹出（宽度跟随 anchor，最小 :data:`MIN_WIDTH`）。"""
        self.setStyleSheet(saved_login_popup_qss(theme))
        self.setFixedWidth(max(anchor.width(), MIN_WIDTH))
        self.adjustSize()
        self.move(anchor.mapToGlobal(QPoint(0, anchor.height() + 4)))
        self.show()
        self.raise_()
        if self._list.count():
            self._list.setCurrentRow(0)
        self._list.setFocus()

    # ============================================================== 内部

    def _build_row(self, login: SavedLogin) -> QWidget:
        """一行：用户名 + 掩码密码 + 删除按钮。"""
        row = QWidget(self._list)
        row.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        box = QHBoxLayout(row)
        box.setContentsMargins(8, 0, 4, 0)
        box.setSpacing(8)

        name = QLabel(login.username, row)
        name.setObjectName("SavedLoginUser")
        box.addWidget(name)

        mask = QLabel(login.masked_password(), row)
        mask.setObjectName("SavedLoginMask")
        box.addWidget(mask)
        box.addStretch(1)

        remove = QPushButton("✕", row)
        remove.setObjectName("SavedLoginRemove")
        remove.setFixedSize(18, 18)
        remove.setCursor(Qt.CursorShape.PointingHandCursor)
        remove.setToolTip(f"删除账号 {login.username}")
        remove.clicked.connect(lambda _=False, u=login.username: self._on_remove(u))
        box.addWidget(remove)
        return row

    def _on_item(self, item: QListWidgetItem) -> None:
        """点行 / 回车 → 回填。"""
        login = item.data(_ROLE_LOGIN)
        if isinstance(login, SavedLogin):
            self.picked.emit(login)
            self.close()

    def _on_remove(self, username: str) -> None:
        self.removed.emit(username)

    def _on_clear(self) -> None:
        self.cleared.emit()
        self.close()

    # ============================================================== 键盘

    def paintEvent(self, event: QPaintEvent) -> None:
        """自己画 QSS 背景。

        ``WA_TranslucentBackground`` 的窗口**不会**自动填充 QSS 背景（背景会是全透明），
        必须手动画 ``PE_Widget``——这也是 QWidget 子类用 QSS 背景的官方写法。
        画完之后圆角外的区域保持透明，圆角才看得出来。
        """
        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, option, painter, self)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """``Esc`` 关闭；上下键选行、``Enter`` 取当前行（``itemActivated`` 已覆盖）。"""
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)


def login_of(signal_arg: object) -> SavedLogin | None:
    """把信号里的 ``object`` 参数安全转回 :class:`SavedLogin`。"""
    return signal_arg if isinstance(signal_arg, SavedLogin) else None


__all__ = ["MAX_VISIBLE_ROWS", "MIN_WIDTH", "ROW_HEIGHT", "SavedLoginPopup", "login_of"]
