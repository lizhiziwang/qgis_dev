"""日期字段的输入控件：文本框（内嵌 ×）+ 右端 ▼ 弹日历。

**为什么不用 ``QDateEdit``**：它看上去最省事（``setCalendarPopup(True)`` 一条），
但在本应用里**点不开日历** —— 主题给 ``QDateEdit`` 套了边框/背景样式，Qt 就此
接管它的绘制，下拉按钮的行为跟着变得不可靠（补 ``::drop-down`` 也没修好）。与其
继续跟 ``QStyleSheetStyle`` 较劲，不如照 QGIS 的样子自己拼一个：

- 文本框：日期以 ``yyyy-MM-dd`` 显示，**× 挂在文本框里面**（``QLineEdit`` 自带的
  清除按钮，有内容才出现），不占额外的地方；
- **没有上下微调箭头**（不是 ``QDateEdit``，自然就没有）；
- 右端一个 ▼ 按钮，点开一个 ``QCalendarWidget`` 弹窗选日期。

空值约定与 :mod:`app.ui.dialogs.attribute_entry` 一致：空文本 = NULL。
"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import QDate, QDateTime, QPoint, QTime, Qt
from qgis.PyQt.QtGui import QIcon, QPixmap
from qgis.PyQt.QtWidgets import (
    QCalendarWidget,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QLabel,
    QPushButton,
    QTimeEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

#: 三种模式各自的显示 / 解析格式
_FORMATS = {
    "date": "yyyy-MM-dd",
    "datetime": "yyyy-MM-dd HH:mm:ss",
    "time": "HH:mm:ss",
}

#: 早于该日期按"空值"处理（1900-01-01 是"没有日期"的哨兵，沿用成果数据的占位约定）
NULL_DATE = QDate(1900, 1, 1)


class DatePickerField(QWidget):
    """日期 / 日期时间 / 时间的输入：文本框（内嵌 ×）+ ▼ 弹选择器。

    :param mode: ``"date"`` / ``"datetime"`` / ``"time"``。**日期时间与时间也必须
        走这里** —— 它们原来用的 ``QDateTimeEdit`` / ``QTimeEdit`` 同样带着那两个
        上下微调箭头、且日历点不开（用户实报：「修改时间」字段还是老样子）。
    """

    def __init__(self, value: Optional[QDate] = None, parent: Optional[QWidget] = None,
                 *, mode: str = "date") -> None:
        super().__init__(parent)
        self._mode = mode if mode in _FORMATS else "date"
        self._fmt = _FORMATS[self._mode]
        self._with_time = self._mode != "date"
        self._date: Optional[QDate] = None
        self._time: Optional[QTime] = None
        self._popup: Optional[QFrame] = None
        self._calendar: Optional[QCalendarWidget] = None
        #: 正在程序化写文本（见 :meth:`_sync_text`）：用来区分"用户键入/点 ×"
        #: 与"我们自己刷新显示"，**不能用 blockSignals**（会连 QLineEdit 内部
        #: 刷新清除按钮的信号一起挡掉，× 就再也不出现了）
        self._syncing = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._edit = QLineEdit(self)
        # × 在文本框**内部**（与 QGIS 一致：清空日期而不是另挂一个按钮）
        self._edit.setClearButtonEnabled(True)
        self._edit.setPlaceholderText("（空）")
        self._edit.editingFinished.connect(self._on_text_committed)
        layout.addWidget(self._edit, 1)

        self._button = QToolButton(self)
        self._button.setObjectName("DatePickerButton")
        self._button.setCursor(Qt.CursorShape.ArrowCursor)
        self._button.setToolTip("选择日期")
        self._button.setIcon(self._arrow_icon())
        self._button.clicked.connect(self._toggle_popup)
        layout.addWidget(self._button)

        # × 清空 → 记成空值（不能只清文本，否则 _date 里还留着旧值）
        self._edit.textChanged.connect(self._on_text_changed)
        self.set_date(value)

    # ------------------------------------------------------------ 对外

    def value(self):
        """当前值：date 模式返回 ``QDate``、datetime 返回 ``QDateTime``、time 返回
        ``QTime``；``None`` = 空。"""
        if self._mode == "time":
            return QTime(self._time) if self._time is not None else None
        if self._date is None:
            return None
        if self._mode == "datetime":
            return QDateTime(self._date, self._time or QTime(0, 0, 0))
        return QDate(self._date)

    def date(self):
        """兼容旧调用：返回日期部分（datetime 模式下也返回 ``QDate``）。"""
        return self._date

    def set_value(self, value) -> None:
        """写入值（``None`` / 无效 = 空）。取日期与时间两部分，其余忽略。"""
        if value is None:
            self._date = None
            self._time = None
        else:
            date = value.date() if hasattr(value, "date") else value
            if isinstance(date, QDate) and date.isValid() and date > NULL_DATE:
                self._date = QDate(date)
            else:
                self._date = None
            # 时间部分：QDateTime 走 ``.time()``；**裸 QTime 直接就是时间**。
            # ``QTime`` 没有 ``.time()`` 方法，只按 ``hasattr(value, "time")`` 取会把
            # 时间整段丢成空 —— 时间字段永远显示空、读回 NULL（静默清值）
            if isinstance(value, QTime):
                time = value
            else:
                time = value.time() if hasattr(value, "time") else None
            self._time = QTime(time) if isinstance(time, QTime) and time.isValid() else None
        self._sync_text()

    def set_date(self, value) -> None:
        """兼容旧调用：整值写一遍（时间部分一并按 ``value`` 重置）。"""
        self.set_value(value)

    # ------------------------------------------------------------ 内部

    def _text_of(self) -> str:
        if self._mode == "time":
            return self._time.toString(self._fmt) if self._time is not None else ""
        if self._date is None:
            return ""
        if self._mode == "datetime":
            return QDateTime(self._date, self._time or QTime(0, 0, 0)).toString(self._fmt)
        return self._date.toString(self._fmt)

    def _sync_text(self) -> None:
        text = self._text_of()
        if self._edit.text() == text:
            return
        self._syncing = True
        try:
            self._edit.setText(text)
        finally:
            self._syncing = False

    def _on_text_changed(self, text: str) -> None:
        # 程序化刷新（_sync_text）跳过：那次改的只是显示，值已经是最新的
        if self._syncing:
            return
        # × 清空时 textChanged 会发空串：此刻把值也清成 NULL
        if not text.strip():
            self._date = None
            self._time = None

    def _on_text_committed(self) -> None:
        """手输的文本在编辑结束时解析；解析不了就退回原值。"""
        text = self._edit.text().strip()
        if not text:
            self._date = None
            self._time = None
            self._sync_text()
            return
        if self._mode == "time":
            parsed = QTime.fromString(text, self._fmt)
            if parsed.isValid():
                self._time = parsed
        elif self._mode == "datetime":
            parsed = QDateTime.fromString(text, self._fmt)
            if parsed.isValid() and parsed.date() > NULL_DATE:
                self._date, self._time = parsed.date(), parsed.time()
        else:
            parsed = QDate.fromString(text, self._fmt)
            if parsed.isValid() and parsed > NULL_DATE:
                self._date = parsed
        self._sync_text()   # 非法输入 → 恢复成上一次的合法值

    def _arrow_icon(self) -> QIcon:
        """复用主题生成的下三角 PNG（与组合框同一个箭头，风格一致）。"""
        try:
            from app.ui.common.theme import ThemeManager, _arrow_url

            path = _arrow_url(ThemeManager.instance().current().menu_text)
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                return QIcon(pixmap)
        except Exception:  # noqa: BLE001 —— 取不到图就用系统默认箭头，不影响功能
            pass
        return self.style().standardIcon(self.style().StandardPixmap.SP_ArrowDown)

    def _toggle_popup(self) -> None:
        if self._popup is not None and self._popup.isVisible():
            self._popup.hide()
            return
        popup = self._ensure_popup()
        # 对齐**文本框左边缘**、贴在文本框正下方（与 QGIS 的日历一致）。
        # 不能对齐 ▼ 按钮：那样弹窗会往右偏半个控件。
        popup.move(self.mapToGlobal(QPoint(0, self.height())))
        popup.show()

    def _ensure_popup(self) -> QFrame:
        """选择器弹窗：日期只给日历（点一天就落地）；日期时间/时间再加一个时间
        输入框和「确定」——时间不是日历能表达的，得让用户补完再关。

        造型上两处硬要求（都来自实报）：

        - **日历表头必须贴到弹窗顶边**：容器上边距留 4px 就是顶部那条白线；
        - **表头高度写死**（见 :meth:`_popup_qss`）：交给平台样式时表头会被撑高，
          压住下面的周一~周日行。
        """
        if self._popup is not None:
            return self._popup
        popup = QFrame(self, Qt.WindowType.Popup)
        popup.setObjectName("DatePickerPopup")
        popup.setFrameShape(QFrame.Shape.StyledPanel)
        popup.setStyleSheet(self._popup_qss())
        box = QVBoxLayout(popup)
        # 0 边距：日历（表头）直接贴住弹窗边框，不然表头上方露出一条白
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        if self._mode != "time":
            calendar = QCalendarWidget(popup)
            calendar.setObjectName("DatePickerCalendar")
            calendar.setGridVisible(True)
            calendar.setVerticalHeaderFormat(
                QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
            )
            # 表头高度被 QSS 写死了，sizeHint 不含这部分增量 —— 补一点最小高度，
            # 免得表头增高后反过来挤压日期网格（就是"表头压住星期行"的成因）
            calendar.setMinimumHeight(calendar.sizeHint().height() + 8)
            if self._date is not None:
                calendar.setSelectedDate(self._date)
            if self._mode == "date":
                calendar.clicked.connect(self._on_calendar_picked)
            else:
                calendar.clicked.connect(self._on_calendar_date_only)
            box.addWidget(calendar)
            self._calendar = calendar

        # 下半部分（时间输入 + 按钮）单独包一层，好给回内边距
        body = QWidget(popup)
        body_box = QVBoxLayout(body)
        body_box.setContentsMargins(6, 6, 6, 6)
        body_box.setSpacing(6)
        if self._with_time:
            self._time_edit = QTimeEdit(body)
            self._time_edit.setDisplayFormat(self._fmt.split(" ")[-1])
            self._time_edit.setTime(self._time or QTime(0, 0, 0))
            row = QHBoxLayout()
            row.setSpacing(6)
            row.addWidget(QLabel("时间", body))
            row.addWidget(self._time_edit, 1)
            body_box.addLayout(row)
        # 「当前时间」：#7 —— 一键填当前日期，含时分秒的字段连时分秒一起填
        now = QPushButton("当前时间", body)
        now.clicked.connect(self._on_now)
        if self._with_time:
            btns = QHBoxLayout()
            btns.setSpacing(6)
            btns.addWidget(now)
            btns.addStretch(1)
            ok = QPushButton("确定", body)
            ok.clicked.connect(self._on_popup_confirmed)
            btns.addWidget(ok)
            body_box.addLayout(btns)
        else:
            body_box.addWidget(now)
        box.addWidget(body)

        self._popup = popup
        return popup

    def _popup_qss(self) -> str:
        """弹窗 + 日历的样式：主题配色，且把表头高度钉死。"""
        try:
            from app.ui.common.theme import ThemeManager

            t = ThemeManager.instance().current()
        except Exception:  # noqa: BLE001 —— 取不到主题就退回无样式，不影响功能
            return ""
        return f"""
        QFrame#DatePickerPopup {{
            background-color: {t.input_bg};
            border: 1px solid {t.input_border};
            border-radius: 4px;
        }}
        QCalendarWidget#DatePickerCalendar {{ background-color: {t.input_bg}; }}
        /* 表头：底色用强调色，高度钉死 —— 交给平台样式会被撑高压住星期行 */
        QCalendarWidget QWidget#qt_calendar_navigationbar {{
            background-color: {t.input_focus};
            min-height: 26px;
            max-height: 26px;
        }}
        QCalendarWidget QToolButton {{
            color: #ffffff;
            background-color: transparent;
            border: none;
            padding: 2px 8px;
            margin: 0;
        }}
        QCalendarWidget QToolButton:hover {{ background-color: rgba(255, 255, 255, 0.22); }}
        QCalendarWidget QToolButton::menu-indicator {{ image: none; }}
        QCalendarWidget QMenu {{
            background-color: {t.menu_bg};
            color: {t.menu_text};
        }}
        QCalendarWidget QSpinBox {{
            background-color: {t.input_bg};
            color: {t.menu_text};
        }}
        /* 年份编辑框（点年份后出现）：**必须压窄**。全局 QSS 给 QSpinBox 留了
           22px 右内边距 + 20px 宽的微调按钮，撑出来的宽度会盖住右侧的"下一月"
           箭头；年份只有 4 位，够用就行 */
        QCalendarWidget QSpinBox#qt_calendar_yearedit {{
            min-width: 0px;
            max-width: 60px;
            padding: 0px 2px;
        }}
        QCalendarWidget QSpinBox#qt_calendar_yearedit::up-button,
        QCalendarWidget QSpinBox#qt_calendar_yearedit::down-button {{
            width: 14px;
        }}
        /* 日期网格：只动底与选中色，**不覆盖文字色**（周末红是模型的前景色，
           被 QSS 的 color 压掉就没了） */
        QCalendarWidget QAbstractItemView:enabled {{
            background-color: {t.input_bg};
            selection-background-color: {t.input_focus};
            selection-color: #ffffff;
            outline: 0;
        }}
        """

    def _hide_popup(self) -> None:
        if self._popup is not None:
            self._popup.hide()
        self._edit.setFocus()

    def _on_now(self) -> None:
        """「当前时间」：日期填今天；含时分秒的字段再把当前时分秒填进去。"""
        now = QDateTime.currentDateTime()
        if self._mode != "time":
            self._date = now.date()
        if self._with_time:
            self._time = now.time()
        self._sync_text()
        self._hide_popup()

    def _on_calendar_date_only(self, picked: QDate) -> None:
        """日期时间/时间模式：点日历只改日期，等用户按「确定」再关。"""
        if picked.isValid() and picked > NULL_DATE:
            self._date = picked
            self._sync_text()

    def _on_calendar_picked(self, picked: QDate) -> None:
        if picked.isValid() and picked > NULL_DATE:
            self._date = picked
            self._sync_text()
        self._hide_popup()

    def _on_popup_confirmed(self) -> None:
        if getattr(self, "_time_edit", None) is not None:
            self._time = self._time_edit.time()
        if self._date is not None or self._time is not None:
            self._sync_text()
        self._hide_popup()


def make_date_field(value, to_value, mode: str = "date"):
    """建一个 :class:`DatePickerField`。

    :param value: 字段原值（``None`` = 空）
    :param to_value: 把非空的原始值转成 ``QDate`` / ``QDateTime`` / ``QTime``
    :param mode: ``"date"`` / ``"datetime"`` / ``"time"``
    :returns: ``(控件, 读值回调)``；读值回调返回对应类型或 ``None``（= NULL）。
    """
    field = DatePickerField(
        None if value is None else _date_part(to_value(value)), mode=mode
    )
    if value is not None:
        field.set_value(to_value(value))
    return field, field.value


def _date_part(value):
    """取一个 QDateTime / QTime 的日期部分（QDate 直接返回）。"""
    if isinstance(value, QDateTime):
        return value.date()
    if isinstance(value, QTime):
        return None
    return value
