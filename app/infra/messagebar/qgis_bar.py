"""消息条基础设施。

QGIS 桌面端用 ``QgsMessageBar`` 显示一次性消息（success / warning / critical）。
旧代码 ``component/util/QGISUtil.py`` 用 ``QgsApplication.instance().setProperty("messageBar", bar)``
把 bar 放到全局属性，再用 ``sendMessageToTopParent(2, "...", 5)`` 调用。

新代码保留同样的全局属性协议（兼容性），但把消息 API 改成更友好的
字符串级别 + 友好名关键字：

    from app.infra.messagebar import send
    send("warning", "操作失败", duration=5)
"""

from __future__ import annotations

from typing import Optional

from qgis._core import Qgis
from qgis.core import QgsApplication

_LEVEL_MAP = {
    "info":     Qgis.Info,
    "success":  Qgis.Success,
    "warning":  Qgis.Warning,
    "critical": Qgis.Critical,
}


def send(level: str, message: str, duration: int = 3) -> bool:
    """往主窗口 ``messageBar`` 推一条消息。

    Parameters
    ----------
    level:
        ``"info"`` / ``"success"`` / ``"warning"`` / ``"critical"`` 之一。
    message:
        消息文本。
    duration:
        持续显示秒数。

    Returns
    -------
    bool
        是否成功推送。未找到 ``messageBar`` 属性时返回 ``False``。
    """
    qgis_level = _LEVEL_MAP.get(level.lower())
    if qgis_level is None:
        raise ValueError(f"unknown level: {level!r}")

    bar = _get_bar()
    if bar is None:
        return False
    bar.pushMessage(message, level=qgis_level, duration=duration)
    return True


def _get_bar():
    """从 ``QgsApplication`` 全局属性拿 ``messageBar``。"""
    app = QgsApplication.instance()
    if app is None:
        return None
    bar = app.property("messageBar")
    if bar is None:
        return None
    # type-narrow：运行时是 ``QgsMessageBar``，IDE 看到是 ``object`` 也没关系
    return bar  # type: ignore[return-value]


# 向后兼容旧 API
def sendMessageToTopParent(mes_type: int, mes_str: str, duration: int = 3) -> None:
    """``component.util.QGISUtil.sendMessageToTopParent`` 旧版别名。

    ``mes_type`` 数字映射::

        1 → success, 2 → warning, 3 → critical
    """
    reverse = {1: "success", 2: "warning", 3: "critical"}
    level = reverse.get(mes_type, "info")
    send(level, mes_str, duration=duration)
