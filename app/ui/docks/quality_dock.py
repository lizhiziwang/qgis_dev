"""质检面板 Dock（独立模块）。

主窗口顶部「质检」页签里的「质检」工具按钮打开本 dock
（见 :meth:`app.ui.main_window.mixins.app_actions.AppActionsMixin._on_quality_dock`）。
树 UI 复用 :class:`app.ui.docks.tools_dock.ToolsDock`，本模块只声明质检自己的
分组树 / 一次性动作 / tooltip。

当前只挂一个「矢量质检」节点，点击后打开数据质检对话框（复用主窗口既有入口，
不写第二套逻辑）；后续各专项质检继续往 :data:`_QUALITY_GROUPS` 里加。
"""

from __future__ import annotations

from app.ui.docks.tools_dock import ToolsDock

#: 「质检」面板的分组树：(分组名, [(工具名, code), ...])
_QUALITY_GROUPS: list = [
    (
        "质检",
        [
            ("矢量质检", "check:vector"),
        ],
    ),
]

#: 质检工具节点 code → 树节点 tooltip
_QUALITY_TIPS: dict = {
    "check:vector": "矢量图层质检：空值 / 值域 / 逻辑 / 空间拓扑检查",
}


def _make_open_check_viewer(parent) -> None:
    """打开数据质检对话框（复用主窗口「通用 → 质检」入口，不写第二套逻辑）。"""
    opener = getattr(parent, "_on_quality", None)
    if opener is not None:
        opener()


#: 质检工具节点 code → 一次性动作
_QUALITY_ACTIONS: dict = {
    "check:vector": _make_open_check_viewer,
}


class QualityDock(ToolsDock):
    """质检面板：一个「质检」分组，下挂「矢量质检」节点。"""

    def __init__(self, parent=None) -> None:
        super().__init__(
            parent,
            title="质检",
            groups=_QUALITY_GROUPS,
            simple_actions=_QUALITY_ACTIONS,
            tips=_QUALITY_TIPS,
            dialog_factories={},
        )


__all__ = ["QualityDock"]
