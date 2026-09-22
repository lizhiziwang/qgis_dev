"""质检面板 Dock（独立模块）。

主窗口顶部「质检」页签里的「质检」工具按钮打开本 dock
（见 :meth:`app.ui.main_window.mixins.app_actions.AppActionsMixin._on_quality_dock`）。
树 UI 复用 :class:`app.ui.docks.tools_dock.ToolsDock`，本模块只声明质检自己的
分组树 / 一次性动作 / 对话框工厂 / tooltip。

当前挂「狭长面」「细碎面」「面几何角检查」三个专项质检节点（分别打开
:class:`app.ui.dialogs.sliver_face.SliverFaceDialog`、
:class:`app.ui.dialogs.small_face.SmallFaceDialog` 与
:class:`app.ui.dialogs.polygon_angle.PolygonAngleDialog`)；「矢量质检」入口
:func:`_make_open_check_viewer` 仍保留（复用主窗口既有入口，不写第二套逻辑），
按需再挂到分组树里。后续各专项质检继续往 :data:`_QUALITY_GROUPS` 里加。
"""

from __future__ import annotations

from app.ui.docks.tools_dock import ToolsDock

#: 「质检」面板的分组树：(分组名, [(工具名, code), ...])
_QUALITY_GROUPS: list = [
    (
        "质检",
        [
            # ("矢量质检", "check:vector"),
            ("狭长面", "check:sliver"),
            ("细碎面", "check:small_area"),
            ("面几何角检查", "check:angle"),
        ],
    ),
]

#: 质检工具节点 code → 树节点 tooltip
_QUALITY_TIPS: dict = {
    "check:vector": "矢量图层质检：空值 / 值域 / 逻辑 / 空间拓扑检查",
    "check:sliver": "狭长面质检：按长宽比阈值检查源图层中的狭长面要素",
    "check:small_area": "细碎面质检：按最小面积 / 周长面积比 / 多部件检查源图层中的细碎面要素",
    "check:angle": "面几何角度检查：按面内夹角阈值检查源图层中的尖角面要素",
}


def _make_open_check_viewer(parent) -> None:
    """打开数据质检对话框（复用主窗口「通用 → 质检」入口，不写第二套逻辑）。"""
    opener = getattr(parent, "_on_quality", None)
    if opener is not None:
        opener()


def _make_sliver_face_dialog(parent):
    """狭长面处理对话框（懒 import，避免 dock 构造时拉进质检模块）。"""
    from app.ui.dialogs.sliver_face import SliverFaceDialog  # noqa: PLC0415 - 懒 import

    return SliverFaceDialog(parent)


def _make_small_face_dialog(parent):
    """细碎面处理对话框（懒 import，避免 dock 构造时拉进质检模块）。"""
    from app.ui.dialogs.small_face import SmallFaceDialog  # noqa: PLC0415 - 懒 import

    return SmallFaceDialog(parent)


def _make_polygon_angle_dialog(parent):
    """面几何角度处理对话框（懒 import，避免 dock 构造时拉进质检模块）。"""
    from app.ui.dialogs.polygon_angle import PolygonAngleDialog  # noqa: PLC0415 - 懒 import

    return PolygonAngleDialog(parent)


#: 质检工具节点 code → 一次性动作
_QUALITY_ACTIONS: dict = {
    "check:vector": _make_open_check_viewer,
}

#: 质检工具节点 code → 对话框工厂
_QUALITY_DIALOG_FACTORIES: dict = {
    "check:sliver": _make_sliver_face_dialog,
    "check:small_area": _make_small_face_dialog,
    "check:angle": _make_polygon_angle_dialog,
}


class QualityDock(ToolsDock):
    """质检面板：一个「质检」分组，下挂「狭长面」「细碎面」等专项质检节点。"""

    def __init__(self, parent=None) -> None:
        super().__init__(
            parent,
            title="质检",
            groups=_QUALITY_GROUPS,
            simple_actions=_QUALITY_ACTIONS,
            tips=_QUALITY_TIPS,
            dialog_factories=_QUALITY_DIALOG_FACTORIES,
        )


__all__ = ["QualityDock"]
