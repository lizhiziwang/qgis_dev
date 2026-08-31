"""融合几何工具回归测试。

覆盖：
1. 编辑工具条包含「融合几何」按钮，默认禁用
2. 仅编辑态（开始编辑后）启用；停止编辑后禁用
3. 未编辑时点「融合几何」弹警告、不激活工具
4. 右键点选 / 再点取消 / 空白不改变
5. 左键双击融合：相邻面合并（unaryUnion 保形：边界节点保留、无简化）、
   保留首个选中要素属性、结果要素选中、可整体撤销
6. 分离要素融合 → 多边形凹包（concaveHullOfPolygons，JTS 系）融成单 Polygon（不丢面积）
7. 角对角要素 → 微膨胀缝合融成单面（面积几乎不变）
8. 微缝隙要素 → 微膨胀缝合填缝成单面、面积几乎不变
9. 重叠要素 → 正常 union 融合成单面（不走缝合/凸包）
10. 少于 2 个要素不融合
11. 未处于编辑态双击 → 提示、不修改数据
12. Esc 退出工具（延迟 unsetMapTool）

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
    """独立 QgsProject；teardown 彻底清理（防 C++ 堆损坏 0xc0000374 / exit 139）。

    与 test_qmap_compare.py 相同模式：先删树节点、再删图层、processEvents
    两次让 deleteLater 的画布真正销毁，最后 sip.delete 显式析构工程对象。
    """
    from PyQt6 import sip
    from PyQt6.QtWidgets import QApplication
    from qgis.core import QgsProject

    p = QgsProject()
    yield p
    p.layerTreeRoot().removeAllChildren()
    for layer in list(p.mapLayers().values()):
        p.removeMapLayer(layer.id())
    QApplication.processEvents()
    QApplication.processEvents()
    try:
        sip.delete(p)
    except RuntimeError:
        pass


def _make_polygon_layer():

    """内存面图层：a/b 相邻（共享边 10），c 与两者分离。"""
    from qgis.core import QgsFeature, QgsField, QgsFields, QgsGeometry, QgsVectorLayer
    from qgis.PyQt.QtCore import QVariant

    fields = QgsFields()
    fields.append(QgsField("name", QVariant.String))
    vl = QgsVectorLayer("Polygon?crs=EPSG:4326", "merge_t", "memory")
    pr = vl.dataProvider()
    pr.addAttributes(fields)
    vl.updateFields()

    def add(name, wkt):
        f = QgsFeature(fields)
        f.setAttribute("name", name)
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        pr.addFeatures([f])

    add("a", "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))")
    add("b", "POLYGON((10 0, 20 0, 20 10, 10 10, 10 0))")
    add("c", "POLYGON((30 0, 40 0, 40 10, 30 10, 30 0))")
    return vl


def _make_canvas(vl, project):
    """裸画布：定好范围并触发一次渲染，让坐标变换可用（工具依赖像素→地图）。"""
    from PyQt6.QtWidgets import QApplication
    from qgis.core import QgsRectangle
    from qgis.gui import QgsMapCanvas

    canvas = QgsMapCanvas()
    canvas.setProject(project)
    canvas.setLayers([vl])
    canvas.setExtent(QgsRectangle(0, 0, 50, 50))
    canvas.resize(400, 300)
    canvas.show()
    app = QApplication.instance()
    assert app is not None
    app.processEvents()
    app.processEvents()
    # 兜底：确保画布 CRS 与图层一致（命中测试不触发多余坐标变换）
    ms = canvas.mapSettings()
    if not ms.destinationCrs().isValid():
        ms.setDestinationCrs(vl.crs())
    return canvas


class _FakeMouseEvent:
    """最小事件桩：暴露 button()/pixelPoint()，驱动 MergeGeometryTool 交互。"""

    def __init__(self, pos, button=None) -> None:
        self._pos = pos
        self._button = button

    def button(self):
        from PyQt6.QtCore import Qt

        return self._button or Qt.MouseButton.LeftButton

    def pixelPoint(self):  # noqa: N802 —— Qt 事件方法命名
        return self._pos


class _FakeKeyEvent:
    """Esc 键事件桩。"""

    def key(self):
        from PyQt6.QtCore import Qt

        return Qt.Key.Key_Escape

    def ignore(self):
        pass


@pytest.mark.gui
def test_merge_button_present_and_disabled_by_default(win):
    """编辑工具条包含「融合几何」按钮，初始禁用。"""
    acts = [
        a
        for a in win.digitizing_toolbar.digitizing_toolbar.actions()
        if not a.isSeparator()
    ]
    texts = [a.text() for a in acts]
    assert "融合几何" in texts
    idx = texts.index("融合几何")
    assert acts[idx].isEnabled() is False


@pytest.mark.gui
def test_merge_button_enabled_only_while_editing(win, monkeypatch):
    """融合几何仅在编辑态可用：开始编辑启用、停止编辑禁用。"""
    from PyQt6.QtWidgets import QMessageBox
    from qgis.core import QgsVectorLayer

    vl = QgsVectorLayer("Polygon?crs=EPSG:4326", "t", "memory")
    win.edit_layer = vl
    dt = win.digitizing_toolbar

    dt.set_edit_target(vl)
    assert dt.merge_geometry_action.isEnabled() is False  # 未编辑

    monkeypatch.setattr(
        "app.ui.main_window.draw_toolbar.QMessageBox.warning",
        lambda *a, **k: QMessageBox.StandardButton.No,
    )
    dt.start_edit_fun()
    assert vl.isEditable()
    assert dt.merge_geometry_action.isEnabled() is True

    dt.stop_edit_fun()
    assert not vl.isEditable()
    assert dt.merge_geometry_action.isEnabled() is False


@pytest.mark.gui
def test_enable_merge_tool_warns_when_not_editing(win, monkeypatch):
    """未开始编辑时点「融合几何」弹警告、不激活工具。"""
    from qgis.core import QgsVectorLayer

    vl = QgsVectorLayer("Polygon?crs=EPSG:4326", "t", "memory")
    win.edit_layer = vl
    dt = win.digitizing_toolbar
    dt.set_edit_target(vl)

    warns = []
    monkeypatch.setattr(
        "app.ui.main_window.draw_toolbar.QMessageBox.warning",
        lambda *a, **k: warns.append(a),
    )
    dt.enable_merge_tool()
    assert warns, "未编辑时应弹「请先开始编辑」警告"
    assert dt.merge_tool is None
    # 画布工具不应变成融合工具（保持原工具/无工具）
    from app.ui.tools.merge_geometry import MergeGeometryTool

    assert not isinstance(win.canvas.mapTool(), MergeGeometryTool)


@pytest.mark.gui
def test_enable_merge_tool_activates_tool(win, monkeypatch):
    """编辑态点「融合几何」→ 画布切到 MergeGeometryTool。"""
    from qgis.core import QgsVectorLayer

    from app.ui.tools.merge_geometry import MergeGeometryTool

    vl = QgsVectorLayer("Polygon?crs=EPSG:4326", "t", "memory")
    assert vl.startEditing()
    win.edit_layer = vl
    dt = win.digitizing_toolbar
    dt.set_edit_target(vl)
    monkeypatch.setattr(
        "app.ui.main_window.draw_toolbar.QMessageBox.information",
        lambda *a, **k: None,
    )
    dt.enable_merge_tool()
    try:
        assert isinstance(win.canvas.mapTool(), MergeGeometryTool)
        assert dt.merge_tool is not None
    finally:
        if dt.merge_tool is not None:
            win.canvas.unsetMapTool(dt.merge_tool)
            dt.merge_tool = None


@pytest.mark.gui
def test_right_click_selects_toggles_and_empty_noop(qgis_app, project):
    """右键点选加入集合、再点取消、点空白不改变。"""
    from PyQt6.QtCore import QPointF, Qt

    from app.ui.tools.merge_geometry import MergeGeometryTool

    vl = _make_polygon_layer()
    canvas = _make_canvas(vl, project)
    hints = []
    tool = MergeGeometryTool(canvas, editing_layer_getter=lambda: vl, hint=hints.append)
    try:
        canvas.setMapTool(tool)
        fids = [f.id() for f in vl.getFeatures()]
        a, b = fids[0], fids[1]
        t = canvas.getCoordinateTransform()
        px_a = QPointF(t.transform(5, 5).x(), t.transform(5, 5).y())
        px_b = QPointF(t.transform(15, 5).x(), t.transform(15, 5).y())
        px_empty = QPointF(t.transform(25, 25).x(), t.transform(25, 25).y())

        def right(px):
            tool.canvasReleaseEvent(_FakeMouseEvent(px, Qt.MouseButton.RightButton))

        right(px_a)
        assert tool._fids == [a]
        right(px_b)
        assert tool._fids == [a, b]
        right(px_a)  # 再右键同一要素 → 移出
        assert tool._fids == [b]
        right(px_empty)  # 空白 → 不改变
        assert tool._fids == [b]
        assert any("已选择" in h for h in hints)
        assert any("已移出" in h for h in hints)
    finally:
        canvas.unsetMapTool(tool)
        tool.deleteLater()
        canvas.deleteLater()


@pytest.mark.gui
def test_double_click_merges_adjacent_and_undo(qgis_app, project):
    """双击融合相邻面：合并公共边、顶点保留（无简化）、保留首个要素属性、可撤销。"""
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtWidgets import QApplication

    from app.ui.tools.merge_geometry import MergeGeometryTool

    vl = _make_polygon_layer()
    canvas = _make_canvas(vl, project)
    hints = []
    tool = MergeGeometryTool(canvas, editing_layer_getter=lambda: vl, hint=hints.append)
    try:
        vl.startEditing()
        canvas.setMapTool(tool)
        fids = [f.id() for f in vl.getFeatures()]
        a, b = fids[0], fids[1]
        t = canvas.getCoordinateTransform()
        px_a = QPointF(t.transform(5, 5).x(), t.transform(5, 5).y())
        px_b = QPointF(t.transform(15, 5).x(), t.transform(15, 5).y())

        tool.canvasReleaseEvent(_FakeMouseEvent(px_a, Qt.MouseButton.RightButton))
        tool.canvasReleaseEvent(_FakeMouseEvent(px_b, Qt.MouseButton.RightButton))
        assert tool._fids == [a, b]

        tool.canvasDoubleClickEvent(
            _FakeMouseEvent(px_a, Qt.MouseButton.LeftButton)
        )
        app = QApplication.instance()
        assert app is not None
        app.processEvents()

        # 3 个要素 → 2 个（a+b 融合，c 保留）
        assert vl.featureCount() == 2
        kept = vl.getFeature(a)
        assert kept.isValid()
        assert kept.attribute("name") == "a"  # 保留首个选中要素属性
        g = kept.geometry()
        assert g.area() == pytest.approx(200.0)  # 两 10×10 面合并
        # 公共边移除后外环 7 个顶点（含闭合点），全部保留（无简化）
        assert sum(1 for _ in g.vertices()) == 7
        assert vl.selectedFeatureIds() == [a]  # 结果要素选中

        # 整体撤销：一个编辑命令
        vl.undoStack().undo()
        assert vl.featureCount() == 3
        assert any("已融合" in h for h in hints)
    finally:
        canvas.unsetMapTool(tool)
        tool.deleteLater()
        canvas.deleteLater()


@pytest.mark.gui
def test_merge_disjoint_polygon_concave_hull_single_polygon(qgis_app, project):
    """真分离要素 → 多边形凹包（JTS ConcaveHull 系）融成**一个 Polygon**。

    两 10×10 块同 y 范围（0..10）相距 20：凹包保持两块边界、只填充中间
    间隙成走廊 → 400（missing=0，不丢面积）。
    """
    from PyQt6.QtCore import QPointF, Qt

    from app.ui.tools.merge_geometry import MergeGeometryTool

    vl = _make_polygon_layer()
    canvas = _make_canvas(vl, project)
    tool = MergeGeometryTool(canvas, editing_layer_getter=lambda: vl)
    try:
        vl.startEditing()
        canvas.setMapTool(tool)
        fids = [f.id() for f in vl.getFeatures()]
        a = fids[0]  # 融合后保留要素
        t = canvas.getCoordinateTransform()
        px_a = QPointF(t.transform(5, 5).x(), t.transform(5, 5).y())
        px_c = QPointF(t.transform(35, 5).x(), t.transform(35, 5).y())

        tool.canvasReleaseEvent(_FakeMouseEvent(px_a, Qt.MouseButton.RightButton))
        tool.canvasReleaseEvent(_FakeMouseEvent(px_c, Qt.MouseButton.RightButton))
        tool.canvasDoubleClickEvent(
            _FakeMouseEvent(px_a, Qt.MouseButton.LeftButton)
        )

        assert vl.featureCount() == 2
        kept = vl.getFeature(a)
        g = kept.geometry()
        assert not g.isMultipart(), "分离要素凹包后应是单个 Polygon"
        assert g.area() == pytest.approx(400.0)  # 两块 + 中间走廊
        assert sum(1 for _ in g.vertices()) == 9  # 凹包顶点（两块 10 点-重叠边+走廊）
    finally:
        canvas.unsetMapTool(tool)
        tool.deleteLater()
        canvas.deleteLater()


@pytest.mark.gui
def test_merge_corner_touching_stitched_single_polygon(qgis_app, project):
    """角对角要素（只共享一个点）→ 微膨胀缝合融成**一个 Polygon**，

    只在角点处补上极窄连接带，面积几乎不变（200 + 0.001）。
    """
    from PyQt6.QtCore import QPointF, Qt

    from app.ui.tools.merge_geometry import MergeGeometryTool

    vl = _make_polygon_layer()
    # 角对角：右下角 (10,10) 处另造一个方块（与 a 只共享一个角点）
    from qgis.core import QgsFeature, QgsGeometry

    corner = QgsGeometry.fromWkt("POLYGON((10 10, 20 10, 20 20, 10 20, 10 10))")
    f = QgsFeature()
    f.setGeometry(corner)
    vl.dataProvider().addFeatures([f])
    canvas = _make_canvas(vl, project)
    tool = MergeGeometryTool(canvas, editing_layer_getter=lambda: vl)
    try:
        vl.startEditing()
        canvas.setMapTool(tool)
        fids = [f.id() for f in vl.getFeatures()]
        a = fids[0]
        t = canvas.getCoordinateTransform()
        px_a = QPointF(t.transform(5, 5).x(), t.transform(5, 5).y())
        px_corner = QPointF(t.transform(15, 15).x(), t.transform(15, 15).y())

        tool.canvasReleaseEvent(_FakeMouseEvent(px_a, Qt.MouseButton.RightButton))
        tool.canvasReleaseEvent(_FakeMouseEvent(px_corner, Qt.MouseButton.RightButton))
        tool.canvasDoubleClickEvent(
            _FakeMouseEvent(px_a, Qt.MouseButton.LeftButton)
        )

        assert vl.featureCount() == 3  # a+角对角融合，b/c 保留
        kept = vl.getFeature(a)
        g = kept.geometry()
        assert not g.isMultipart(), "角对角缝合后应是单个 Polygon"
        assert g.area() == pytest.approx(200.0, abs=0.5)  # 只补极窄连接带，面积几乎不变
    finally:
        canvas.unsetMapTool(tool)
        tool.deleteLater()
        canvas.deleteLater()


@pytest.mark.gui
def test_merge_small_gap_filled_single_polygon(qgis_app, project):
    """微缝隙要素 → 微膨胀缝合填掉缝隙成单面，面积基本不变（保形）。"""
    from PyQt6.QtCore import QPointF, Qt

    from app.ui.tools.merge_geometry import MergeGeometryTool

    vl = _make_polygon_layer()
    # 与 a 共享边但留 0.001 缝隙（手工矢量化边界不严实的典型）
    from qgis.core import QgsFeature, QgsGeometry

    gap = QgsGeometry.fromWkt(
        "POLYGON((10.001 0, 20 0, 20 10, 10.001 10, 10.001 0))"
    )
    f = QgsFeature()
    f.setGeometry(gap)
    vl.dataProvider().addFeatures([f])
    canvas = _make_canvas(vl, project)
    tool = MergeGeometryTool(canvas, editing_layer_getter=lambda: vl)
    try:
        vl.startEditing()
        canvas.setMapTool(tool)
        fids = [f.id() for f in vl.getFeatures()]
        a = fids[0]
        t = canvas.getCoordinateTransform()
        px_a = QPointF(t.transform(5, 5).x(), t.transform(5, 5).y())
        px_gap = QPointF(t.transform(15, 5).x(), t.transform(15, 5).y())

        tool.canvasReleaseEvent(_FakeMouseEvent(px_a, Qt.MouseButton.RightButton))
        tool.canvasReleaseEvent(_FakeMouseEvent(px_gap, Qt.MouseButton.RightButton))
        tool.canvasDoubleClickEvent(
            _FakeMouseEvent(px_a, Qt.MouseButton.LeftButton)
        )

        assert vl.featureCount() == 3  # a+缝隙块融合，b/c 保留
        kept = vl.getFeature(a)
        g = kept.geometry()
        assert not g.isMultipart(), "微缝隙缝合后应是单个 Polygon"
        assert g.area() == pytest.approx(200.0, abs=0.5)  # 缝隙被填掉，面积几乎不变
    finally:
        canvas.unsetMapTool(tool)
        tool.deleteLater()
        canvas.deleteLater()


@pytest.mark.gui
def test_merge_misaligned_gap_stitched_single_polygon(qgis_app, project):
    """错位微缝隙（边界不严实 + 顶点错位）→ 缝合单面，面积几乎不变。

    凹包 0.3 在该场景会丢 99/198.8 的面积；缝合只补缝（面积 ≈199）。
    """
    from PyQt6.QtCore import QPointF, Qt

    from app.ui.tools.merge_geometry import MergeGeometryTool

    vl = _make_polygon_layer()
    # 右块左边界 x=10.02 且上下错位 0.1（手工矢量化典型误差）
    from qgis.core import QgsFeature, QgsGeometry

    gap = QgsGeometry.fromWkt(
        "POLYGON((10.02 0.1, 20 0, 20 10, 10.02 9.9, 10.02 0.1))"
    )
    f = QgsFeature()
    f.setGeometry(gap)
    vl.dataProvider().addFeatures([f])
    canvas = _make_canvas(vl, project)
    tool = MergeGeometryTool(canvas, editing_layer_getter=lambda: vl)
    try:
        vl.startEditing()
        canvas.setMapTool(tool)
        fids = [f.id() for f in vl.getFeatures()]
        a = fids[0]
        t = canvas.getCoordinateTransform()
        px_a = QPointF(t.transform(5, 5).x(), t.transform(5, 5).y())
        px_gap = QPointF(t.transform(15, 5).x(), t.transform(15, 5).y())

        tool.canvasReleaseEvent(_FakeMouseEvent(px_a, Qt.MouseButton.RightButton))
        tool.canvasReleaseEvent(_FakeMouseEvent(px_gap, Qt.MouseButton.RightButton))
        tool.canvasDoubleClickEvent(
            _FakeMouseEvent(px_a, Qt.MouseButton.LeftButton)
        )

        assert vl.featureCount() == 3  # a+缝隙块融合，b/c 保留
        kept = vl.getFeature(a)
        g = kept.geometry()
        assert not g.isMultipart(), "错位缝隙缝合后应是单个 Polygon"
        # 原面积 198.8，缝合后 ≈199（只补缝，不丢面积）
        assert g.area() == pytest.approx(199.0, abs=0.5)
    finally:
        canvas.unsetMapTool(tool)
        tool.deleteLater()
        canvas.deleteLater()


@pytest.mark.gui
def test_merge_overlapping_normal_union(qgis_app, project):
    """重叠要素 → 正常 union 融合成单面（不走凹包）。"""
    from PyQt6.QtCore import QPointF, Qt

    from app.ui.tools.merge_geometry import MergeGeometryTool

    vl = _make_polygon_layer()
    # 与 a 重叠一半（x 5~15）
    from qgis.core import QgsFeature, QgsGeometry

    overlap = QgsGeometry.fromWkt("POLYGON((5 0, 15 0, 15 10, 5 10, 5 0))")
    f = QgsFeature()
    f.setGeometry(overlap)
    vl.dataProvider().addFeatures([f])
    canvas = _make_canvas(vl, project)
    tool = MergeGeometryTool(canvas, editing_layer_getter=lambda: vl)
    try:
        vl.startEditing()
        canvas.setMapTool(tool)
        fids = [f.id() for f in vl.getFeatures()]
        a = fids[0]
        t = canvas.getCoordinateTransform()
        px_a = QPointF(t.transform(2.5, 5).x(), t.transform(2.5, 5).y())  # a 独有区域（重叠块只覆盖 x≥5）
        px_ov = QPointF(t.transform(12, 5).x(), t.transform(12, 5).y())  # 重叠块独有区域

        tool.canvasReleaseEvent(_FakeMouseEvent(px_a, Qt.MouseButton.RightButton))
        tool.canvasReleaseEvent(_FakeMouseEvent(px_ov, Qt.MouseButton.RightButton))
        tool.canvasDoubleClickEvent(
            _FakeMouseEvent(px_a, Qt.MouseButton.LeftButton)
        )

        assert vl.featureCount() == 3  # a+重叠块融合，b/c 保留
        kept = vl.getFeature(a)
        g = kept.geometry()
        assert not g.isMultipart()
        # 并集面积 = 100 + 100 - 重叠 50
        assert g.area() == pytest.approx(150.0)
    finally:
        canvas.unsetMapTool(tool)
        tool.deleteLater()
        canvas.deleteLater()


@pytest.mark.gui
def test_merge_requires_at_least_two(qgis_app, project):
    """少于 2 个要素双击 → 不融合。"""
    from PyQt6.QtCore import QPointF, Qt

    from app.ui.tools.merge_geometry import MergeGeometryTool

    vl = _make_polygon_layer()
    canvas = _make_canvas(vl, project)
    tool = MergeGeometryTool(canvas, editing_layer_getter=lambda: vl)
    try:
        vl.startEditing()
        canvas.setMapTool(tool)
        t = canvas.getCoordinateTransform()
        px_a = QPointF(t.transform(5, 5).x(), t.transform(5, 5).y())

        tool.canvasReleaseEvent(_FakeMouseEvent(px_a, Qt.MouseButton.RightButton))
        tool.canvasDoubleClickEvent(
            _FakeMouseEvent(px_a, Qt.MouseButton.LeftButton)
        )
        assert vl.featureCount() == 3  # 未融合
    finally:
        canvas.unsetMapTool(tool)
        tool.deleteLater()
        canvas.deleteLater()


@pytest.mark.gui
def test_merge_not_editable_hints_without_changes(qgis_app, project):
    """未处于编辑态双击 → 提示、不修改数据。"""
    from PyQt6.QtCore import QPointF, Qt

    from app.ui.tools.merge_geometry import MergeGeometryTool

    vl = _make_polygon_layer()
    canvas = _make_canvas(vl, project)
    hints = []
    tool = MergeGeometryTool(canvas, editing_layer_getter=lambda: vl, hint=hints.append)
    try:
        canvas.setMapTool(tool)
        t = canvas.getCoordinateTransform()
        px_a = QPointF(t.transform(5, 5).x(), t.transform(5, 5).y())
        px_b = QPointF(t.transform(15, 5).x(), t.transform(15, 5).y())

        tool.canvasReleaseEvent(_FakeMouseEvent(px_a, Qt.MouseButton.RightButton))
        tool.canvasReleaseEvent(_FakeMouseEvent(px_b, Qt.MouseButton.RightButton))
        tool.canvasDoubleClickEvent(
            _FakeMouseEvent(px_a, Qt.MouseButton.LeftButton)
        )
        assert vl.featureCount() == 3  # 数据未动
        assert any("未处于编辑态" in h for h in hints)
    finally:
        canvas.unsetMapTool(tool)
        tool.deleteLater()
        canvas.deleteLater()


@pytest.mark.gui
def test_esc_exits_tool(qgis_app, project):
    """Esc → 退出融合工具（延迟 unsetMapTool）。"""
    from PyQt6.QtWidgets import QApplication

    from app.ui.tools.merge_geometry import MergeGeometryTool

    vl = _make_polygon_layer()
    canvas = _make_canvas(vl, project)
    tool = MergeGeometryTool(canvas, editing_layer_getter=lambda: vl)
    try:
        canvas.setMapTool(tool)
        assert canvas.mapTool() is tool
        tool.keyPressEvent(_FakeKeyEvent())
        app = QApplication.instance()
        assert app is not None
        app.processEvents()
        app.processEvents()
        assert canvas.mapTool() is None
    finally:
        canvas.deleteLater()
