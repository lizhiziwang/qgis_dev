"""悬浮消息条：自己精确跟踪“当前有没有消息在显示”。

``QgsMessageBar`` 的消息过期/关闭移除都在 C++ 内部完成、没有对外信号，靠
遍历子控件判空在版本间不可靠（实测空条也会被误判为有内容、空卡片常驻）。
本模块用子类接管 Python 侧全部 push 入口：

- 每条 push **顶掉上一条**（条内最多一条消息，见
  :meth:`FloatingMessageBar._drop_previous`）——C++ 侧一律 ``duration=0``
  （永不过期），不清的话旧消息会一直堆在条里，下次任何工具 push 时被一起
  重新显示（QgsMessageBar 折叠成「N more」），看上去像“别的面板刚推的旧提示
  又冒出来”；
- 把“有没有消息”变成确定性状态，供宿主窗口（如主窗口把消息条悬浮盖在画布
  顶部）按回调显示/隐藏。

**鼠标停留不收起**：提示文字长时，用户常来不及看完就被 duration 收掉。租约
到期时若鼠标正停在提示条上，则不收起、改为持续复检；鼠标移开后再**重新开始**
算一个完整的展示时长，而不是当场消失（见
:meth:`FloatingMessageBar._lease_expired` / :meth:`FloatingMessageBar.leaveEvent`）。

**只显示最新一条**：本消息条是浮动 toast，不是通知中心。每次 push 前先
``clearWidgets()`` 清掉旧条目——不清的话条目会无限堆积，右侧冒出 QGIS 自带的
「3 more」计数标签和下拉菜单，而那个菜单里只有「Close All」、**列不出消息内容**
（实测），用户看到一个点不开的入口，两个按钮还挤在一起。见
:meth:`FloatingMessageBar._replace_items`。

用法::

    bar = FloatingMessageBar(canvas)
    bar.set_on_state(lambda active: bar.show() if active else bar.hide())
"""

from __future__ import annotations

from qgis.core import Qgis
from qgis.gui import QgsMessageBar
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QCursor

#: 租约到期但鼠标仍停在提示条上时，以此间隔复检鼠标是否已移开（毫秒）
_HOVER_POLL_MS = 200


class FloatingMessageBar(QgsMessageBar):
    """悬浮消息条：每次 push 记一条“展示租约”，到期自动通知宿主收起。

    - 每次 push 立即显示，``duration`` 秒后自动收起（由自带单次 QTimer
      触发，不依赖 C++ 内部移除机制）；``duration<=0`` 表示常驻，直到
      下一条消息顶掉或窗口关闭；
    - **到期时鼠标若正停在提示条上，则不收起**：改为每隔
      :data:`_HOVER_POLL_MS` 复检一次；鼠标移开后再重新计满一个展示时长才收起
      ——文字太长时用户可以把鼠标停在上面慢慢看，看完移开也有足够时间收尾；
    - 是否显示通过 :meth:`set_on_state` 回调通知宿主——启动时从未 push 过
      → 回调不触发 → 消息条保持隐藏，绝不会出现空卡片。

    传给 C++ 的消息一律 ``duration=0``（禁用其内部过期移除，避免与租约
    计时打架），过期与否完全由租约计时器说了算；也因此每条 push 都要
    先摘掉上一条，否则旧消息会攒在条里被一起重新显示。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lease = QTimer(self)
        self._lease.setSingleShot(True)
        self._lease.timeout.connect(self._lease_expired)
        self._on_state = None
        #: 本次消息的完整展示时长（毫秒）；None = 常驻（duration<=0）或已收起
        self._duration_ms: int | None = None
        #: 到期后因鼠标停留而处于「等待移开」态：leaveEvent 据此重新开始计时
        self._holding = False
        #: 宿主注入的外观 QSS（见 :meth:`set_appearance`）
        self._appearance_qss = ""

    def set_on_state(self, cb) -> None:
        """注册显隐回调：``cb(active: bool)``。"""
        self._on_state = cb

    def set_appearance(self, qss: str) -> None:
        """设置宿主的外观 QSS（圆角卡片 / 文字色 / 去文字框边框等）。

        **必须由本类在每次 push 之后重新贴一遍**：``QgsMessageBar`` 每推一条消息都会
        用它自带的那份（``QgsMessageBar { background-color: #e7f5fe; border: 1px …}``）
        覆盖掉宿主的样式表 —— 只设一次的话，推第一条消息起宿主的样式就全没了（实测
        条底色一直是 QGIS 的浅蓝、里面文字框的边框也去不掉）。
        """
        self._appearance_qss = qss
        self._apply_appearance()

    def _apply_appearance(self) -> None:
        if not self._appearance_qss:
            return
        try:
            self.setStyleSheet(self._appearance_qss)
        except RuntimeError:
            pass  # 窗口关闭流程中 C++ 对象已销毁

    def dismiss(self) -> None:
        """立刻收起（公共入口）：清掉租约并通知宿主隐藏。

        宿主/工具条在「用户按 Esc 取消」这类语义上主动收起时调它——提示框属于
        本次操作，操作取消了提示也该跟着消失。
        """
        self._dismiss()

    # ---- 展示租约：push 即显示，到期即收起 ----

    # def _replace_items(self) -> None:
    #     """清掉条里已有的条目——本消息条只显示**最新一条**。
    #
    #     QGIS 的 ``QgsMessageBar`` 会把每条 push 都留成一条持久条目（我们给 C++
    #     传的 duration 恒为 0，好让租约计时器独占过期控制），不主动清就会无限
    #     堆积：右侧冒出内置的「N more」计数标签 + 一个下拉菜单，而那个菜单里
    #     只有「Close All」、列不出消息内容，看起来就是个点不开的入口，两个按钮
    #     还挤在一起。清掉后计数标签与下拉自然消失，条高也恒定。
    #     """
    #     try:
    #         self.clearWidgets()
    #     except RuntimeError:
    #         pass  # 窗口关闭流程中 C++ 对象已销毁

    def _drop_previous(self) -> None:
        """清掉条里已有的消息：整条同时只保留一条（新消息顶掉旧的）。

        C++ 侧一律 ``duration=0``（永不过期），不主动清就不会自己消失；旧消息
        留在条里，下次任何工具 push 时会被一起重新显示（QgsMessageBar 折叠成
        「N more」），表现为“别的面板刚推的旧提示又冒出来”。
        """
        try:
            if self.items():
                self.clearWidgets()
        except RuntimeError:
            pass  # 窗口关闭流程中 C++ 对象已销毁

    def _kick(self, duration: int) -> None:
        """（重新）记一条展示租约：立即显示，duration 秒后自动收起。"""
        self._apply_appearance()   # push 会把 QGIS 自带的样式表盖回来，这里贴回宿主的
        self._lease.stop()
        self._holding = False
        if self._on_state:
            self._on_state(True)
        seconds = int(duration or 0)
        # duration<=0 → 常驻：不设自动收起
        self._duration_ms = seconds * 1000 if seconds > 0 else None
        if not self._duration_ms:
            return
        if self._cursor_on_bar():
            # 新消息顶上来时鼠标可能正停在条上：先挂住不启动计时，等移开再算
            self._holding = True
            self._lease.start(_HOVER_POLL_MS)
        else:
            self._lease.start(self._duration_ms)

    def _lease_expired(self) -> None:
        """租约到期：鼠标还停在提示条上就继续留着，等移开再重新计时。

        到期那一刻鼠标仍在上面的，转到「等待移开」态并按 :data:`_HOVER_POLL_MS`
        周期复检——光标判定不依赖 ``enterEvent`` / ``leaveEvent``（鼠标移进条内
        的子控件时父控件收不到可靠的离开事件），所以轮询是必要的兜底。
        """
        if self._cursor_on_bar():
            self._holding = True
            self._lease.start(_HOVER_POLL_MS)
            return
        self._dismiss()

    def _cursor_on_bar(self) -> bool:
        """鼠标当前是否落在提示条上（按全局光标位置判定）。"""
        try:
            return self.isVisible() and self.rect().contains(
                self.mapFromGlobal(QCursor.pos())
            )
        except RuntimeError:
            return False  # 窗口关闭流程中 C++ 对象已销毁

    def _dismiss(self) -> None:
        """收起提示：清掉租约状态并通知宿主隐藏。"""
        self._lease.stop()
        self._duration_ms = None
        self._holding = False
        if self._on_state:
            self._on_state(False)

    def leaveEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        """鼠标移开：**重新开始**算消失时间，而不是当场收起。

        用户看完才把鼠标移开，此时再留足一个完整展示时长才合理——不然「停得
        越久、一移开就没」正好与「让人看清提示」的初衷相反。
        """
        if self._holding and not self._cursor_on_bar():
            self._holding = False
            self._lease.start(self._duration_ms)
        super().leaveEvent(event)

    # ---- 接管 push 入口（清掉旧条目 + 把 duration 转为租约，C++ 一律传 0）----

    def pushMessage(self, *args, **kwargs):
        title = kwargs.get("title")
        text = kwargs.get("text")
        level = kwargs.get("level", Qgis.MessageLevel.Info)
        duration = int(kwargs.get("duration", 0) or 0)
        if title is None:
            if len(args) >= 2 and isinstance(args[1], str):
                # (title, text[, level[, duration]]) 全位置风格
                title, text = args[0], args[1]
                if len(args) >= 3:
                    level = args[2]
                if len(args) >= 4:
                    duration = int(args[3] or 0)
            elif args:
                # (text[, level[, duration]]) 风格
                text = args[0] if text is None else text
                if len(args) >= 2:
                    level = args[1]
                if len(args) >= 3:
                    duration = int(args[2] or 0)
        # self._replace_items()
        if title:
            self._drop_previous()
            item = super().pushMessage(title, text or "", level, 0)
        else:
            self._drop_previous()
            item = super().pushMessage(text or "", level, 0)
        self._kick(duration)
        return item

    # 带标题的各入口统一走 pushMessage(title, text, level, 0)：
    # ``QgsMessageBar.pushSuccess/Warning/Critical/Info`` 只有 ``(title, text)`` 两个参数
    # （没有 duration，C++ 侧用自带默认过期），传第三个参数会 TypeError；而本类要求
    # C++ 永不过期、由自家租约统一管显隐，所以改用 ``pushMessage`` 显式传 duration=0。

    def pushSuccess(self, title, text="", duration=0):
        self._drop_previous()
        super().pushMessage(title, text or "", Qgis.MessageLevel.Success, 0)
        self._kick(duration)

    def pushWarning(self, title, text="", duration=0):
        self._drop_previous()
        super().pushMessage(title, text or "", Qgis.MessageLevel.Warning, 0)
        self._kick(duration)

    def pushCritical(self, title, text="", duration=0):
        self._drop_previous()
        super().pushMessage(title, text or "", Qgis.MessageLevel.Critical, 0)
        self._kick(duration)

    def pushError(self, title, text="", duration=0):
        # ``QgsMessageBar`` 没有 pushError，错误一律按 critical 呈现
        self._drop_previous()
        super().pushMessage(title, text or "", Qgis.MessageLevel.Critical, 0)
        self._kick(duration)

    def pushInfo(self, title, text="", duration=0):
        self._drop_previous()
        super().pushMessage(title, text or "", Qgis.MessageLevel.Info, 0)
        self._kick(duration)

    def pushWidget(self, widget, level=Qgis.MessageLevel.Info, duration=0):
        self._drop_previous()
        super().pushWidget(widget, level, 0)
        self._kick(duration)

    def pushItem(self, item):
        self._drop_previous()
        super().pushItem(item)
        self._kick(0)
