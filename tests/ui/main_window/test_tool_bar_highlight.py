"""工具条按钮状态高亮回归测试。

需求：所有地图工具上添加状态显示 —— 当前使用的工具按钮高亮。

- 地图工具（平移/缩放/点选/框选/属性/卷帘）：单选高亮，跟随画布激活的
  QgsMapTool（``mapToolSet`` 信号 + 各 ``_on_*`` 回调里显式恢复勾选）；
- 会话型工具（地图对比）：独立开关，会话存活期间按钮常亮，不受地图工具切换影响；
- 一次性动作（全图/刷新/测试/质检/分析）：不可勾选、不高亮。

需要真实 QGIS（PyQt6 + QgsApplication）；CI 默认 ``-m 'not gui'`` 跳过。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")


@pytest.fixture
def win(qgis_app):
    """构造主窗口（不 show）。"""
    from app.ui.main_window.main_window import MainWindow

    w = MainWindow()
    yield w
    w.close()


def _tool_action(win, key):
    return win.tool_bar_builder._tool_actions[key]


def _checkable_texts(win):
    """工具条里所有可勾选按钮的文本。"""
    return {a.text() for a in win.tool_bar.actions() if a.isCheckable()}


@pytest.mark.gui
@pytest.mark.gui
def test_only_map_tools_are_checkable(win):
    """只有地图工具与会话型工具可勾选；一次性动作不可勾选。"""
    checkable = _checkable_texts(win)
    assert "平移" in checkable
    assert "点选" in checkable
    assert "框选" in checkable
    assert "属性" in checkable
    assert "放大" in checkable
    assert "缩小" in checkable
    assert "卷帘对比" in checkable
    assert "地图对比" in checkable
    assert len(checkable) == 8
    for text in ("全图", "刷新", "测试", "质检", "分析", "取消选择"):
        acts = [a for a in win.tool_bar.actions() if a.text() == text]
        assert acts, f"{text} 按钮应存在"
        assert not acts[0].isCheckable(), f"{text} 不应可勾选"


@pytest.mark.gui
def test_default_active_is_pan(win):
    """启动时默认激活平移 → 「平移」按钮高亮。"""
    assert win.tool_bar_builder.active_key() == "pan"
    assert _tool_action(win, "pan").isChecked()
    assert not _tool_action(win, "zoom_in").isChecked()


@pytest.mark.gui
def test_switch_tool_moves_highlight(win):
    """切到放大 → 放大高亮、平移取消；再切回平移恢复（含重复点击同一工具）。"""
    win._on_zoom_in()
    assert win.tool_bar_builder.active_key() == "zoom_in"
    assert _tool_action(win, "zoom_in").isChecked()
    assert not _tool_action(win, "pan").isChecked()

    win._on_zoom_out()
    assert win.tool_bar_builder.active_key() == "zoom_out"
    assert not _tool_action(win, "zoom_in").isChecked()

    # 重复点击已激活的平移：setMapTool 不触发 mapToolSet，回调须显式恢复勾选
    win._on_pan()
    assert _tool_action(win, "pan").isChecked()
    win._on_pan()
    assert _tool_action(win, "pan").isChecked()
    assert not _tool_action(win, "zoom_out").isChecked()


@pytest.mark.gui
def test_select_and_identify_highlight(win):
    """点选 / 框选 / 属性工具激活时各自按钮高亮。"""
    win._on_select_point()
    assert _tool_action(win, "select_point").isChecked()
    assert not _tool_action(win, "pan").isChecked()

    win._on_select_box()
    assert _tool_action(win, "select_box").isChecked()
    assert not _tool_action(win, "select_point").isChecked()

    win._on_identify()
    assert _tool_action(win, "identify").isChecked()
    assert not _tool_action(win, "select_box").isChecked()


@pytest.mark.gui
def test_clear_selection_clears_all_vector_layers(win, monkeypatch):
    """「取消选择」清空所有矢量图层的选择，且不影响当前地图工具高亮。"""
    from qgis.core import QgsProject, QgsVectorLayer

    project = QgsProject()  # 独立工程，隔离共享单例
    vl = QgsVectorLayer("Point?crs=EPSG:4326", "a", "memory")
    assert vl.isValid()
    project.addMapLayer(vl)
    vl.startEditing()
    from qgis.core import QgsFeature

    feats = [QgsFeature(i + 1) for i in range(3)]
    vl.dataProvider().addFeatures(feats)
    vl.select([1, 2])
    assert vl.selectedFeatureCount() == 2
    monkeypatch.setattr(win, "project", project)

    # 当前激活框选工具（高亮保持），点「取消选择」不清除工具状态
    win._on_select_box()
    assert _tool_action(win, "select_box").isChecked()

    win._on_clear_selection()
    assert vl.selectedFeatureCount() == 0
    assert _tool_action(win, "select_box").isChecked()
    assert win.tool_bar_builder.active_key() == "select_box"


@pytest.mark.gui
def test_map_tool_set_signal_drives_highlight(win):
    """mapToolSet 信号路径：画布工具被外部切换时高亮同步（select/swipe 类工具）。"""
    from qgis.core import QgsVectorLayer

    from app.ui.tools.select_feature import PointSelectTool
    from app.ui.tools.swipe import SwipeTool

    win._on_pan()
    assert _tool_action(win, "pan").isChecked()

    # 外部（非 _on_* 回调）setMapTool → mapToolSet → _tool_key 解析高亮
    sel = PointSelectTool(win.canvas)
    win.canvas.setMapTool(sel)
    assert _tool_action(win, "select_point").isChecked()

    base = QgsVectorLayer("Point?crs=EPSG:4326", "b", "memory")
    cmp = QgsVectorLayer("Point?crs=EPSG:4326", "c", "memory")
    swipe = SwipeTool(win.canvas, base, cmp)
    win.canvas.setMapTool(swipe)
    assert _tool_action(win, "swipe").isChecked()
    assert not _tool_action(win, "select_point").isChecked()

    # 退出会话 / unsetMapTool → 无激活工具 → 全不亮
    swipe._exit_session()
    win.canvas.unsetMapTool(swipe)
    assert win.tool_bar_builder.active_key() is None
    assert not any(
        a.isChecked()
        for a in win.tool_bar.actions()
        if a.isCheckable() and a.text() != "地图对比"
    )


@pytest.mark.gui
def test_qmap_compare_highlights_while_session_active(win, monkeypatch):
    """地图对比会话启动 → 按钮常亮（不受地图工具切换影响）；停止 → 取消高亮。"""
    from PyQt6.QtWidgets import QDialog
    from qgis.core import QgsProject, QgsVectorLayer

    project = QgsProject()  # 独立工程，隔离共享单例
    layer = QgsVectorLayer("Point?crs=EPSG:4326", "a", "memory")
    assert layer.isValid()
    project.addMapLayer(layer)
    monkeypatch.setattr(win, "project", project)

    class _Bar:
        def pushMessage(self, *a, **k):
            pass

    monkeypatch.setattr(win, "messageBar", _Bar())

    from app.ui.dialogs import qmap_compare as qmc_mod
    from app.ui.tools import qmap_compare as qmc_tools_mod

    monkeypatch.setattr(
        qmc_mod.QMapCompareDialog, "exec", lambda self: QDialog.DialogCode.Accepted
    )
    monkeypatch.setattr(
        qmc_mod.QMapCompareDialog, "selected",
        lambda self: ([layer], "vertical", "circle", 0.3),
    )

    class FakeSession:
        is_active = True

        def start(self, *a, **k):
            pass

        def stop(self):
            self.is_active = False

    fake = FakeSession()
    monkeypatch.setattr(qmc_tools_mod, "CompareMask", lambda *a, **k: fake)

    # 启动 → 高亮；会话存活期间切地图工具 → 保持高亮（独立开关）
    win._on_qmap_compare()
    assert win._qmap_compare_session is fake
    assert _tool_action(win, "qmap_compare").isChecked()

    win._on_pan()
    assert _tool_action(win, "pan").isChecked()
    assert _tool_action(win, "qmap_compare").isChecked()

    # 再点「地图对比」停止 → 取消高亮
    win._on_qmap_compare()
    assert not fake.is_active
    assert not _tool_action(win, "qmap_compare").isChecked()
