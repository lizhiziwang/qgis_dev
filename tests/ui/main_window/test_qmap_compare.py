"""地图对比组件（qmap_compare）回归测试。

覆盖：
1. tool_icons 注册了 "qmap_compare" 图标（防主题切换 KeyError；纯单测）
2. 通用工具条包含「地图对比」按钮
3. 对比参数对话框默认垂直分割；lens 模式才显示放大镜设置；空选择弹警告
4. CompareMask 垂直分割会话：组结构正确、对比图层移入组、事件循环后不丢
5. CompareMask stop：对比图层还原原位、临时图层清理干净
6. mask 离屏渲染：垂直左红右蓝 / 水平上红下蓝（修正后的表达式）
7. Lens 会话：mask 层 ReloadData 自动刷新 + 200ms 间隔
8. MirrorCompare 镜像分屏：dock 第二画布、双向中心同步、停止还原可见性

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


@pytest.fixture
def project(qgis_app):
    """独立 QgsProject（隔离共享单例，测试结束彻底清理）。"""
    from PyQt6.QtWidgets import QApplication
    from qgis.core import QgsProject

    p = QgsProject()
    yield p
    # 清空地图 + 树（先删节点再删图层：节点析构时图层还活着才安全），
    # 再 sip.delete 显式销毁工程 C++ 对象：pytest 里若等 fixture 局部
    # 自然释放（teardown 收尾阶段）project 析构会触发 C++ 堆损坏
    # （0xc0000374 / exit 139，与 CompareMask 无关的最小复现即崩）。
    p.layerTreeRoot().removeAllChildren()
    for layer in list(p.mapLayers().values()):
        p.removeMapLayer(layer.id())
    QApplication.processEvents()
    QApplication.processEvents()
    from PyQt6 import sip  # noqa: PLC0415

    try:
        sip.delete(p)
    except RuntimeError:
        pass


def _make_poly_layer(project, name, color, wkt="POLYGON((99 91, 109 91, 109 101, 99 101, 99 91))"):
    """造一个实心多边形矢量图层（无需 GDAL）。"""
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsFeature,
        QgsFillSymbol,
        QgsGeometry,
        QgsSingleSymbolRenderer,
        QgsVectorLayer,
    )

    vl = QgsVectorLayer("Polygon?crs=EPSG:4490", name, "memory")
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromWkt(wkt))
    vl.dataProvider().addFeatures([f])
    vl.updateExtents()
    vl.setRenderer(
        QgsSingleSymbolRenderer(
            QgsFillSymbol.createSimple({"color": color, "outline_style": "no"})
        )
    )
    project.setCrs(QgsCoordinateReferenceSystem("EPSG:4490"))
    project.addMapLayer(vl, False)
    return vl


def _node_count(root, layer_id):
    """统计图层在树中的节点数（判断移动 vs 复制）。"""
    out = []

    def walk(node):
        for c in node.children():
            if hasattr(c, "layerId") and c.layerId() == layer_id:
                out.append(c)
            walk(c)

    walk(root)
    return out


# ---------------------------------------------------------------------------
# 1. 图标注册 / 按钮（纯单测 + 轻 GUI）
# ---------------------------------------------------------------------------
def test_qmap_compare_glyph_registered():
    """tool_icons 必须注册 "qmap_compare" 图标。"""
    from app.ui.common import tool_icons

    assert "qmap_compare" in tool_icons._GLYPHS


@pytest.mark.gui
def test_qmap_compare_button_present(win):
    """通用工具条应包含「地图对比」按钮。"""
    acts = [a for a in win.tool_bar.actions() if not a.isSeparator()]
    assert any(a.text() == "地图对比" for a in acts)


# ---------------------------------------------------------------------------
# 2. 对话框
# ---------------------------------------------------------------------------
@pytest.mark.gui
def test_dialog_defaults_and_lens_visibility(qgis_app, project):
    """默认垂直分割；lens 模式才显示放大镜设置；切换模式控制可见性。"""
    from app.ui.dialogs.qmap_compare import QMapCompareDialog

    base = _make_poly_layer(project, "base", "red")
    _make_poly_layer(project, "compare", "blue")
    project.layerTreeRoot().addLayer(base)

    dlg = QMapCompareDialog(None)
    try:
        dlg.project = project
        dlg._populate_tree()
        # 非模态显示，让可见性断言生效
        dlg.show()
        from PyQt6.QtWidgets import QApplication

        QApplication.processEvents()
        assert dlg._mode() == "vertical"
        assert dlg._mode_radios["vertical"].isChecked()
        assert not dlg._mode_radios["lens"].isChecked()
        # 非 lens：放大镜设置隐藏
        assert not dlg._circle_radio.isVisible()
        # 切到 lens：放大镜设置可见
        dlg._mode_radios["lens"].setChecked(True)
        QApplication.processEvents()
        assert dlg._circle_radio.isVisible()
        assert dlg._size_slider.value() == 15
        dlg.hide()

        # 勾选 compare 后 selected() 返回图层 + 模式 + 形状 + 比例
        items = [dlg.layer_tree.topLevelItem(i) for i in range(dlg.layer_tree.topLevelItemCount())]
        target = next(i for i in items if i.data(0, 0x0100) == base.id())
        from PyQt6.QtCore import Qt

        target.setCheckState(0, Qt.CheckState.Checked)
        dlg.checked_layers.append(base.id())
        layers, mode, shape, rate = dlg.selected()
        assert [l.id() for l in layers] == [base.id()]
        assert mode == "lens"
        assert shape == "circle"
        assert rate == 0.15
    finally:
        dlg.deleteLater()


@pytest.mark.gui
def test_dialog_empty_selection_warns(qgis_app, project, monkeypatch):
    """空选择点「开始」弹警告、不 accept。"""
    from app.ui.dialogs.qmap_compare import QMapCompareDialog

    _make_poly_layer(project, "base", "red")
    dlg = QMapCompareDialog(None)
    try:
        dlg.project = project
        dlg._populate_tree()
        warned = []
        monkeypatch.setattr(
            "app.ui.dialogs.qmap_compare.QMessageBox.warning",
            lambda *a, **k: warned.append(a),
        )
        accepted = []
        dlg.accept = lambda: accepted.append(1)
        dlg._on_start()
        assert warned, "空选择应弹警告"
        assert not accepted, "空选择不应 accept"
    finally:
        dlg.deleteLater()


# ---------------------------------------------------------------------------
# 3. CompareMask 会话
# ---------------------------------------------------------------------------
@pytest.mark.gui
def test_mask_session_structure_and_restore(qgis_app, project):
    """垂直分割会话：结构正确、事件循环后不丢图层、stop 还原干净。"""
    from qgis.core import QgsInvertedPolygonRenderer, QgsLayerTreeGroup
    from PyQt6.QtGui import QPainter

    from app.ui.tools.qmap_compare import (
        BACKGROUND_LAYER_NAME,
        COMPARE_GROUP_NAME,
        MASK_LAYER_NAME,
        CompareMask,
    )

    root = project.layerTreeRoot()
    base = _make_poly_layer(project, "base", "red")
    comp = _make_poly_layer(project, "compare", "blue")
    data_group = root.addGroup("数据图层")
    data_group.addLayer(base)
    data_group.addLayer(comp)

    s = CompareMask(project)
    assert not s.is_active
    assert s.start([comp], "vertical")
    assert s.is_active

    # 组结构：对比组在顶部，含 mask + comp + bg
    group_node = root.findGroup(COMPARE_GROUP_NAME)
    assert group_node is not None
    assert root.children().index(group_node) == 0
    names = [c.name() for c in group_node.children()]
    assert MASK_LAYER_NAME in names and BACKGROUND_LAYER_NAME in names
    assert "compare" in names
    # 对比图层从数据组移走（仅 1 个节点，未复制）
    assert [c.name() for c in data_group.children()] == ["base"]
    assert len(_node_count(root, comp.id())) == 1

    # mask 渲染器：InvertedPolygonRenderer + DestinationIn
    mask = project.mapLayersByName(MASK_LAYER_NAME)[0]
    assert isinstance(mask.renderer(), QgsInvertedPolygonRenderer)
    assert mask.blendMode() == QPainter.CompositionMode.CompositionMode_DestinationIn

    # 事件循环跑一遍（桥的 queued 删除若误判会在此删图层）
    from PyQt6.QtWidgets import QApplication

    QApplication.processEvents()
    assert comp.id() in project.mapLayers()
    assert len(_node_count(root, comp.id())) == 1

    # stop：还原 + 清理
    s.stop()
    assert not s.is_active
    assert [c.name() for c in data_group.children()] == ["base", "compare"]
    assert len(_node_count(root, comp.id())) == 1
    assert comp.id() in project.mapLayers()
    # 组节点移除后，组内 mask/背景图层由桥在事件循环里从工程删除
    # （stop 不手动删，避免与桥的 queued 删除双删）→ 先跑事件循环再断言
    QApplication.processEvents()
    QApplication.processEvents()
    leftovers = [l.name() for l in project.mapLayers().values() if "QMapCompare" in l.name()]
    assert not leftovers, f"临时图层未清理干净: {leftovers}"
    # 清理：先删图层/组并跑事件循环（桥的 queued 删除），再断引用 +
    # 显式 GC（QApplication 存活时）。project 由 fixture teardown 的
    # sip.delete 显式销毁（见 fixture 注释）。
    project.removeMapLayers([base.id(), comp.id()])
    root.removeChildNode(data_group)
    QApplication.processEvents()
    QApplication.processEvents()
    import gc

    del s, group_node, mask, base, comp, data_group, root
    gc.collect()

@pytest.mark.gui
def test_mask_offscreen_render_split_colors(qgis_app, project):
    """离屏渲染验证分割效果：垂直左红右蓝、水平上红下蓝。"""
    from PyQt6.QtWidgets import QApplication
    from qgis.core import QgsMapSettings

    from app.ui.tools.qmap_compare import CompareMask, offscreen_render

    base = _make_poly_layer(project, "base", "red")
    comp = _make_poly_layer(project, "compare", "blue")
    root = project.layerTreeRoot()
    data_group = root.addGroup("数据图层")
    data_group.addLayer(base)
    data_group.addLayer(comp)

    s = CompareMask(project)
    s.start([comp], "vertical")
    ms = QgsMapSettings()
    ms.setDestinationCrs(base.crs())
    ms.setExtent(base.extent())
    ms.setLayers([s._group_layer, base, comp])
    img = offscreen_render(ms, project)
    assert img.pixelColor(100, 200).getRgb()[:3] == (255, 0, 0), "左半应为 base 红"
    assert img.pixelColor(300, 200).getRgb()[:3] == (0, 0, 255), "右半应为 compare 蓝"
    s.stop()

    s.start([comp], "horizontal")
    ms2 = QgsMapSettings()
    ms2.setDestinationCrs(base.crs())
    ms2.setExtent(base.extent())
    ms2.setLayers([s._group_layer, base, comp])
    img2 = offscreen_render(ms2, project)
    assert img2.pixelColor(200, 100).getRgb()[:3] == (255, 0, 0), "上半应为 base 红"
    assert img2.pixelColor(200, 300).getRgb()[:3] == (0, 0, 255), "下半应为 compare 蓝"
    s.stop()
    QApplication.processEvents()
    QApplication.processEvents()
    project.removeMapLayers([base.id(), comp.id()])
    root.removeChildNode(data_group)
    QApplication.processEvents()
    QApplication.processEvents()
    import gc

    del s, base, comp, data_group, root
    gc.collect()


@pytest.mark.gui
def test_lens_session_auto_refresh(qgis_app, project):
    """Lens 会话：mask 层 ReloadData 自动刷新 + 200ms 间隔 + 表达式。"""
    from PyQt6.QtWidgets import QApplication
    from qgis.core import Qgis, QgsGeometryGeneratorSymbolLayer

    from app.ui.tools.qmap_compare import (
        LENS_AUTO_REFRESH_INTERVAL_MS,
        CompareMask,
    )

    base = _make_poly_layer(project, "base", "red")
    comp = _make_poly_layer(project, "compare", "blue")
    root = project.layerTreeRoot()
    data_group = root.addGroup("数据图层")
    data_group.addLayer(base)
    data_group.addLayer(comp)

    s = CompareMask(project)
    assert s.start([comp], "lens", "circle", 0.2)
    mask = project.mapLayersByName("QMapCompareMask")[0]
    assert mask.autoRefreshMode() == Qgis.AutoRefreshMode.ReloadData
    assert mask.autoRefreshInterval() == LENS_AUTO_REFRESH_INTERVAL_MS
    # mask 渲染器表达式应包含 canvas_cursor_point（放大镜跟随鼠标）
    inv = mask.renderer()
    sym = inv.embeddedRenderer().symbol()
    expr = ""
    for i in range(sym.symbolLayerCount()):
        layer = sym.symbolLayer(i)
        if isinstance(layer, QgsGeometryGeneratorSymbolLayer):
            expr = layer.geometryExpression()
    assert "canvas_cursor_point" in expr
    assert "0.2" in expr
    s.stop()
    assert not s.is_active
    QApplication.processEvents()
    QApplication.processEvents()
    project.removeMapLayers([base.id(), comp.id()])
    root.removeChildNode(data_group)
    QApplication.processEvents()
    QApplication.processEvents()
    import gc

    del s, base, comp, data_group, root
    gc.collect()


# ---------------------------------------------------------------------------
# 4. MirrorCompare 镜像分屏
# ---------------------------------------------------------------------------
@pytest.mark.gui
def test_mirror_session_sync_and_cleanup(qgis_app):
    """镜像分屏：dock 第二画布、初始同步、双向同步、停止还原可见性。

    不用真实 MainWindow：真实窗口的图层树桥接器（挂在共享单例上）在
    teardown 移除图层时堆损坏（test_swipe 已验证）。这里用独立
    QgsProject + 裸 QMainWindow + 裸主画布。
    """
    from PyQt6.QtWidgets import QApplication, QDockWidget, QMainWindow

    from qgis.core import QgsPointXY, QgsProject
    from qgis.gui import QgsMapCanvas as GuiQgsMapCanvas

    from app.ui.tools.qmap_compare import MIRROR_DOCK_TITLE, MirrorCompare

    project = QgsProject()
    root = project.layerTreeRoot()
    base = _make_poly_layer(project, "base", "red")
    comp = _make_poly_layer(project, "compare", "blue")
    data_group = root.addGroup("数据图层")
    data_group.addLayer(base)
    data_group.addLayer(comp)

    qmw = QMainWindow()
    main_canvas = GuiQgsMapCanvas(qmw)
    main_canvas.setProject(project)
    main_canvas.setLayers([base])
    main_canvas.setExtent(base.extent())  # 必须先 setExtent（中心非 nan）
    QApplication.processEvents()

    m = MirrorCompare(qmw, main_canvas)
    assert m.start([comp])
    assert m.is_active
    QApplication.processEvents()  # 触发 _initial_sync（singleShot(0)）

    # dock 存在且带第二画布
    docks = [d for d in qmw.findChildren(QDockWidget) if d.windowTitle() == MIRROR_DOCK_TITLE]
    assert docks, "应出现分屏 dock"
    assert m.mirror_canvas is not None
    assert [l.name() for l in m.mirror_canvas.layers()] == ["compare"]
    # 主画布隐藏对比图层（互补显示）
    assert [l.name() for l in main_canvas.layers()] == ["base"]

    # 初始同步：mirror center == main center（且非 nan）
    mc = m.mirror_canvas.center()
    assert mc.toString() != "nan, nan"
    assert mc == main_canvas.center()

    # 主画布 → mirror 同步
    main_canvas.setCenter(QgsPointXY(104.0, 96.0))
    QApplication.processEvents()
    assert m.mirror_canvas.center().toString() == "104, 96"

    # mirror → 主画布 同步
    m.mirror_canvas.setCenter(QgsPointXY(105.0, 97.0))
    QApplication.processEvents()
    assert main_canvas.center().toString() == "105, 97"

    # 停止：dock 移除、可见性还原
    m.stop()
    assert not m.is_active
    QApplication.processEvents()
    assert not [d for d in qmw.findChildren(QDockWidget) if d.windowTitle() == MIRROR_DOCK_TITLE]
    assert root.findLayer(comp.id()).isVisible()

    # 独立工程清理（无共享单例桥接器，安全）
    project.removeMapLayers([base.id(), comp.id()])
    root.removeChildNode(data_group)
    qmw.close()
    # 显式销毁画布 / 窗口，断引用 + 测试内 GC：
    # 残留 C++ 包装留到 pytest unconfigure 的 shutdown GC（QgsApplication
    # 已半毁）会段错误（test_swipe 已验证同款问题）。
    main_canvas.deleteLater()
    qmw.deleteLater()
    QApplication.processEvents()
    QApplication.processEvents()
    del m, main_canvas, qmw, docks
    import gc

    gc.collect()


@pytest.mark.gui
def test_main_window_on_qmap_compare_toggle(qgis_app, monkeypatch):
    """_on_qmap_compare：无会话弹对话框；接受后启动 mask 会话；再点停止。

    不用真实 MainWindow（桥接器堆损坏风险同 mirror 测试）；用桩窗口绑
    真实方法实现。
    """
    from PyQt6.QtWidgets import QApplication, QDialog, QMainWindow

    from qgis.core import QgsProject
    from qgis.gui import QgsMapCanvas

    from app.ui.main_window.main_window import MainWindow
    from app.ui.tools.qmap_compare import CompareMask

    project = QgsProject()
    root = project.layerTreeRoot()
    base = _make_poly_layer(project, "base", "red")
    comp = _make_poly_layer(project, "compare", "blue")
    data_group = root.addGroup("数据图层")
    data_group.addLayer(base)
    data_group.addLayer(comp)

    class _MsgBar:
        def pushMessage(self, *a, **k):
            pass

    class _FakeToolBar:
        """工具条高亮桩：真实 _on_qmap_compare 会调 set_session_active。"""

        def set_active(self, key=None):
            pass

        def set_session_active(self, key, active):
            pass

    class _FakeWin(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.project = project
            self.canvas = QgsMapCanvas(self)
            self.canvas.setProject(project)
            self.canvas.setLayers([base])
            self.messageBar = _MsgBar()
            self.tool_bar_builder = _FakeToolBar()

    win = _FakeWin()

    # 假对话框：返回 (图层, 模式, 形状, 比例)
    class FakeDialog:
        def __init__(self, *a):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selected(self):
            return [comp], "vertical", "circle", 0.15

    monkeypatch.setattr(
        "app.ui.dialogs.qmap_compare.QMapCompareDialog", FakeDialog
    )
    win._on_qmap_compare = MainWindow._on_qmap_compare.__get__(win)
    win._on_qmap_compare()
    assert isinstance(getattr(win, "_qmap_compare_session", None), CompareMask)
    assert win._qmap_compare_session.is_active
    # 再点：停止
    win._on_qmap_compare()
    assert not win._qmap_compare_session.is_active
    # 会话停止后图层还原
    assert [c.name() for c in data_group.children()] == ["base", "compare"]
    QApplication.processEvents()
    assert comp.id() in project.mapLayers()

    project.removeMapLayers([base.id(), comp.id()])
    root.removeChildNode(data_group)
    # 断循环引用（FakeWin ↔ 会话 ↔ project/canvas）后显式销毁 + 测试内 GC，
    # 避免 shutdown GC 段错误（同 mirror 测试）。
    win._qmap_compare_session = None
    win.canvas.deleteLater()
    win.deleteLater()
    QApplication.processEvents()
    QApplication.processEvents()
    del win
    import gc

    gc.collect()
    # CompareMask 会话动过树（桥内部状态），独立 QgsProject 的 C++ 析构
    # 在函数返回阶段会崩（与 fixture teardown 同因）；QApplication 存活
    # 时 sip.delete 显式销毁
    from PyQt6 import sip  # noqa: PLC0415

    sip.delete(project)
    gc.collect()
