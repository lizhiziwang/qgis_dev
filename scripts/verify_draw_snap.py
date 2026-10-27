"""绘制「磁性捕捉（顶点/边）」指示器 + 停止编辑生命周期的回归自测。

用途
----
验证两类回归：
  V1 点「停止编辑」→ 弹窗选「否」后，已激活的点/线/面绘制工具被真正取消
     （``DigitizingToolbar.active_tool`` 置空、画布当前地图工具不再是该工具）
  V2 线/面绘制工具：**激活后、点下第一个点之前**移动鼠标靠近已有顶点/边，
     应出现吸附标记（不同图标）
  V3 画完一笔后标记隐藏；**继续画下一笔、首点落下前**移动靠近顶点，标记应
     再次出现（修复：move 不再被 ``is_over`` 提前拦掉）

环境
----
需要 QGIS 运行时（qgis.core / qgis.gui / qgis.PyQt）。Windows + QGIS 自带
python 示例：:

    "E:\\app\\QGIS421\\bin\\python-qgis.bat" scripts/verify_draw_snap.py

Linux / conda 里用对应 python 直接跑同一文件即可。脚本以 offscreen 跑，无需
显示器，会自建 QgsMapCanvas / 真实 DigitizingToolbar / QgisSnappingToolbar
与内存图层，不碰真实工程与磁盘数据。

退出码
------
0=全部通过；1=有断言未过；2=QGIS 初始化/装配失败。
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QPoint, Qt, QVariant  # noqa: E402
from qgis.PyQt.QtGui import QColor  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

from app.ui.common.message_box import MessageBox as QMessageBox  # noqa: E402

# 无头跑：所有消息框一律自动点掉（information=确定，warning 默认=否，
# 让「停止编辑是否保存」走到 No 分支；需要 Yes 分支时用 _patch_warning 临时切）。
def _auto_ok(*_a, **_k):
    return QMessageBox.StandardButton.Ok


def _auto_no(*_a, **_k):
    return QMessageBox.StandardButton.No


QMessageBox.information = staticmethod(_auto_ok)
QMessageBox.warning = staticmethod(_auto_no)


def _patch_warning(reply) -> None:
    """切换「停止编辑是否保存」弹窗的返回值（Yes / No 分支都覆盖）。"""
    QMessageBox.warning = staticmethod(_auto(reply))


def _auto(reply):
    def _f(*_a, **_k):
        return reply

    return _f

_FAILURES: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    flag = "PASS" if ok else "FAIL"
    line = f"[{flag}] {name}"
    if detail:
        line += f"  | {detail}"
    print(line)
    if not ok:
        _FAILURES.append(name)


def _line_layer(name: str, wkt: str | None = None) -> QgsVectorLayer:
    lyr = QgsVectorLayer("LineString", name, "memory")
    pr = lyr.dataProvider()
    pr.addAttributes([QgsField("id", QVariant.Int)])
    lyr.updateFields()
    if wkt:
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        f.setAttributes([1])
        pr.addFeatures([f])
    return lyr


class _Evt:
    """最小化鼠标事件替身：只暴露绘制工具用到的方法。"""

    def __init__(self, xy, button=Qt.MouseButton.LeftButton):
        self._p = QPoint(int(round(xy[0])), int(round(xy[1])))
        self._b = button

    def pixelPoint(self):
        return self._p

    def button(self):
        return self._b


def _indicator_visible(tool) -> bool:
    w = tool._snap_indicator_widget()
    return bool(w and w.isVisible())


def _make_env():
    """装配 QgsApplication + 画布 + 真实 DigitizingToolbar/SnappingToolbar。"""
    app = QgsApplication([], True)
    prefix = os.environ.get("QGIS_PREFIX_PATH")
    if prefix:
        QgsApplication.setPrefixPath(prefix, True)
    QgsApplication.initQgis()

    target = _line_layer("snap_target", "LINESTRING(0 0, 100 0)")
    prj = QgsProject.instance()
    prj.addMapLayer(target)

    win = QMainWindow()
    canvas = QgsMapCanvas(win)
    canvas.resize(800, 600)
    canvas.setCanvasColor(QColor(255, 255, 255))
    canvas.setLayers([target])
    canvas.setExtent(QgsRectangle(-20, -20, 120, 20))
    win.setCentralWidget(canvas)
    win.show()
    canvas.show()
    for _ in range(5):
        app.processEvents()
        time.sleep(0.02)

    state = {"layer": None}

    def getter():
        return state["layer"]

    from app.ui.main_window.draw_toolbar import DigitizingToolbar
    from app.ui.main_window.snapping_toolbar import QgisSnappingToolbar

    tb = DigitizingToolbar(win, canvas, editing_layer_getter=getter)
    snap = QgisSnappingToolbar(win, canvas, prj, editing_layer_getter=getter)
    canvas.refresh()
    for _ in range(4):
        app.processEvents()
        time.sleep(0.02)
    return app, win, canvas, prj, tb, snap, state


def _px(canvas, map_xy):
    tr = canvas.getCoordinateTransform()
    q = tr.transform(QgsPointXY(*map_xy))
    return (q.x(), q.y())


def _enable(tb, tool_name: str):
    """真实走工具条激活路径，并拆掉 finished 连接避免弹出属性框/类型校验。"""
    tb.enable_draw_tool(tool_name)
    # enable_draw_tool 首次会懒建 draw_manager 并 connect finished；此后把它拆掉，
    # 让「画完一笔」停在工具自留的提交后状态（is_over=True），便于断言指示器。
    try:
        if tb.draw_manager is not None:
            tb.draw_manager.finished.disconnect()
    except Exception:  # noqa: BLE001
        pass
    return tb.active_tool


def _finish_shape(tool, canvas, shape_points):
    """点完若干个节点后右键结束 → 等价于“画完一笔但工具保留（is_over=True）”。"""
    for m in shape_points:
        tool.canvasPressEvent(_Evt(_px(canvas, m)))
    tool.canvasPressEvent(_Evt(_px(canvas, (40, 40)), Qt.MouseButton.RightButton))


def _assert_stopped_reset(tb, layer, tag):
    """断言「停止编辑」后整条状态机已复位（无论选是/否）。

    这正是用户报告过的回归点：早期实现把工具/按钮复位放在弹窗与提交之后，
    选「否」路径一旦抛异常，后面的复位全部被跳过 → 画布仍挂激活的添加面工具、
    停止编辑按钮仍可用。这里把每个可见状态都钉死。
    """
    active = type(tb.active_tool).__name__ if tb.active_tool is not None else None
    ok = all(
        [
            tb.active_tool is None,
            tb._editing is False,
            not tb.stop_edit.isEnabled(),
            not tb.save_edit.isEnabled(),
            tb.start_edit.isEnabled(),
            not layer.isEditable(),
        ]
    )
    _check(
        f"{tag} 停止编辑后状态复位(工具空/非编辑/停止保存禁用)",
        ok,
        detail=(f"active={active} _editing={tb._editing} "
                f"stop={tb.stop_edit.isEnabled()} save={tb.save_edit.isEnabled()} "
                f"start={tb.start_edit.isEnabled()} editable={layer.isEditable()}"),
    )


def _run_stop_case(tb, canvas, layer, tool_name, reply, tag):
    """真实触发一次「停止编辑」（弹窗选 Yes/No），并断言工具 + 状态机复位。"""
    _patch_warning(reply)
    tb._ensure_editing(layer)
    _enable(tb, tool_name)
    # 让 editBuffer 非空，真实走到「是否保存」弹窗分支
    f = QgsFeature(layer.fields())
    f.setGeometry(QgsGeometry.fromWkt("LINESTRING(200 0, 250 0)"))
    layer.addFeature(f)
    before = canvas.mapTool()
    tb.stop_edit.trigger()
    choice = "是" if reply == QMessageBox.StandardButton.Yes else "否"
    _check(
        f"{tag} 停止编辑选「{choice}」后激活工具取消且画布不再挂工具",
        tb.active_tool is None and canvas.mapTool() is not before,
        detail="before=" + type(before).__name__,
    )
    _assert_stopped_reset(tb, layer, f"{tag} 选「{choice}」")


def main() -> int:
    app, win, canvas, prj, tb, snap, state = _make_env()
    FAR = (130, 0)  # 远离吸附目标线的落点区
    VX = _px(canvas, (0, 0))  # 吸附目标线起点顶点（像素坐标）

    # ------------------------------------------------------- V1
    print("\n== V1 停止编辑（选“是/否”）→ 激活工具取消 + 工具条/编辑态复位 ==")
    edit_v1 = _line_layer("edit_v1", None)
    state["layer"] = edit_v1
    for tool_name in ("point", "line", "polygon"):
        # 「否」分支 + 「是」分支各跑一遍，两组最终状态必须一致
        _run_stop_case(tb, canvas, edit_v1, tool_name, QMessageBox.StandardButton.No, f"V1[{tool_name}]")
        _run_stop_case(tb, canvas, edit_v1, tool_name, QMessageBox.StandardButton.Yes, f"V1[{tool_name}]")
    # 还原默认「否」，让后续 V1b 仍走「选否」分支
    _patch_warning(QMessageBox.StandardButton.No)

    # ------------------------------------------------------- V2 / V3
    print("\n== V2/V3 线/面：首点前移动显示标记；画完后隐藏；续画首点前再显示 ==")
    edit_v2 = _line_layer("edit_v2", None)
    state["layer"] = edit_v2
    tb._ensure_editing(edit_v2)
    for tool_name, shape_pts in (
        ("line", [FAR, (160, 0)]),
        ("polygon", [FAR, (160, 0), (145, 15)]),
    ):
        tool = _enable(tb, tool_name)
        # 预热吸附索引 + V2：激活后、未点任何点就移动靠近顶点
        tool.canvasMoveEvent(_Evt(_px(canvas, (50, 0))))
        fresh = _indicator_visible(tool)
        _check(
            f"V2 [{tool_name}] 激活后首点前移动靠近顶点出现标记",
            fresh,
            detail="is_over=" + str(getattr(tool, "is_over", False)),
        )
        # V3：画完一笔（提交后状态），标记应隐藏
        _finish_shape(tool, canvas, shape_pts)
        if tb.draw_manager is not None:
            tb.draw_manager._temp_points.clear()
        tool.hide_snap_indicator()
        after_finish = not _indicator_visible(tool)
        # 续画下一笔：首点落下前再移动靠近顶点，标记应重新出现
        tool.canvasMoveEvent(_Evt(VX))
        cont = _indicator_visible(tool)
        _check(
            f"V3 [{tool_name}] 画完隐藏；续画首点前移动靠近顶点标记重现",
            after_finish and cont,
            detail=f"hiddenAfterFinish={after_finish} is_over="
            + str(getattr(tool, "is_over", False))
            + f" showAgain={cont}",
        )
        # 顺带 V1-after-finish：提交后仍激活的工具，停止编辑选否也要能取消
        tb.stop_edit.trigger()
        _check(
            f"V1b [{tool_name}] 画完保留的工具停止编辑选否后取消",
            tb.active_tool is None,
        )
        # 重新进入编辑态供下一轮
        tb._ensure_editing(edit_v2)

    print()
    if _FAILURES:
        print(f"RESULT: FAILED ({len(_FAILURES)} failed)")
        for n in _FAILURES:
            print("   -", n)
    else:
        print("RESULT: ALL PASS")
    QgsApplication.exitQgis()
    return 1 if _FAILURES else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print("RESULT: ENV FAILURE", exc)
        sys.exit(2)
