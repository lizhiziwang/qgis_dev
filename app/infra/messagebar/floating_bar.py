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
    计时打架），过期与否完全由租约计时器说了算；也因此每条 push 都要
    先摘掉上一条，否则旧消息会攒在条里被一起重新显示。
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
        # 先摘掉上一条（条内只留一条），再按两种调用风格推
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
