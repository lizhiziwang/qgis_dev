"""悬浮消息条：自己精确跟踪“当前有没有消息在显示”。

``QgsMessageBar`` 的消息过期/关闭移除都在 C++ 内部完成、没有对外信号，靠
遍历子控件判空在版本间不可靠（实测空条也会被误判为有内容、空卡片常驻）。
本模块用子类接管 Python 侧全部 push 入口，把“有没有消息”变成确定性状态，
供宿主窗口（如主窗口把消息条悬浮盖在画布顶部）按回调显示/隐藏。

用法::

    bar = FloatingMessageBar(canvas)
    bar.set_on_state(lambda active: bar.show() if active else bar.hide())
"""

from __future__ import annotations

from qgis.core import Qgis
from qgis.gui import QgsMessageBar
from qgis.PyQt.QtCore import QTimer


class FloatingMessageBar(QgsMessageBar):
    """悬浮消息条：每次 push 记一条“展示租约”，到期自动通知宿主收起。

    - 每次 push 立即显示，``duration`` 秒后自动收起（由自带单次 QTimer
      触发，不依赖 C++ 内部移除机制）；``duration<=0`` 表示常驻，直到
      下一条消息顶掉或窗口关闭；
    - 是否显示通过 :meth:`set_on_state` 回调通知宿主——启动时从未 push 过
      → 回调不触发 → 消息条保持隐藏，绝不会出现空卡片。

    传给 C++ 的消息一律 ``duration=0``（禁用其内部过期移除，避免与租约
    计时打架），过期与否完全由租约计时器说了算。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lease = QTimer(self)
        self._lease.setSingleShot(True)
        self._lease.timeout.connect(self._lease_expired)
        self._on_state = None

    def set_on_state(self, cb) -> None:
        """注册显隐回调：``cb(active: bool)``。"""
        self._on_state = cb

    # ---- 展示租约：push 即显示，到期即收起 ----

    def _kick(self, duration: int) -> None:
        """（重新）记一条展示租约：立即显示，duration 秒后自动收起。"""
        self._lease.stop()
        if self._on_state:
            self._on_state(True)
        if duration and duration > 0:
            self._lease.start(int(duration) * 1000)

    def _lease_expired(self) -> None:
        if self._on_state:
            self._on_state(False)

    # ---- 接管 push 入口（把 duration 转为租约，C++ 一律传 0）----

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
        if title:
            item = super().pushMessage(title, text or "", level, 0)
        else:
            item = super().pushMessage(text or "", level, 0)
        self._kick(duration)
        return item

    def pushSuccess(self, title, text="", duration=0):
        super().pushSuccess(title, text, 0)
        self._kick(duration)

    def pushWarning(self, title, text="", duration=0):
        super().pushWarning(title, text, 0)
        self._kick(duration)

    def pushCritical(self, title, text="", duration=0):
        super().pushCritical(title, text, 0)
        self._kick(duration)

    def pushError(self, title, text="", duration=0):
        super().pushError(title, text, 0)
        self._kick(duration)

    def pushInfo(self, title, text="", duration=0):
        super().pushInfo(title, text, 0)
        self._kick(duration)

    def pushWidget(self, widget, level=Qgis.MessageLevel.Info, duration=0):
        super().pushWidget(widget, level, 0)
        self._kick(duration)

    def pushItem(self, item):
        super().pushItem(item)
        self._kick(0)
