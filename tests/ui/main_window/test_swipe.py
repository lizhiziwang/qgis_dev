"""卷帘对比工具（swipe）回归测试。

覆盖：
1. tool_icons 注册了 "swipe" 图标（防主题切换 KeyError；纯单测，不依赖 QGIS）
2. 通用工具条包含「卷帘对比」按钮
3. 无栅格图层时 ``_on_swipe`` 弹提示而非崩溃
4. 卷帘参数对话框默认垂直方向；空列表点「开始」弹警告、不 accept
5. 分屏会话常驻：激活后画布只剩左影像；切平移工具不拆；退出才还原原图层集
6. 只有按住分隔条附近拖动才改变占比；点其它位置不改变（拖拽命中判定）
7. 矢量两侧都可见：画布只留左影像，矢量等其它图层交给覆盖层离屏渲染；
   分屏期新增矢量也被纳入覆盖层渲染集
8. 会话存续时再点「卷帘对比」直接恢复、不弹窗

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


def _make_tif(path, fill):
    """GDAL 造一张 8×8 单波段 GeoTIFF（无需 numpy）。"""
    from osgeo import gdal

    ds = gdal.GetDriverByName("GTiff").Create(str(path), 8, 8, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((100.0, 1.0, 0.0, 200.0, 0.0, -1.0))
    ds.SetProjection(
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,'
        '298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",'
        "0.0174532925199433]]"
    )
    ds.GetRasterBand(1).Fill(fill)
    ds.FlushCache()
    ds = None


@pytest.fixture
def two_rasters(qgis_app, tmp_path):
    """工程里两张 8×8 GeoTIFF 栅格（base 值 10 / compare 值 200）。"""
    from qgis.core import QgsProject, QgsRasterLayer

    base_path = tmp_path / "base.tif"
    compare_path = tmp_path / "compare.tif"
    _make_tif(base_path, 10)
    _make_tif(compare_path, 200)

    base = QgsRasterLayer(str(base_path), "base")
    compare = QgsRasterLayer(str(compare_path), "compare")
    assert base.isValid()
    assert compare.isValid()

    project = QgsProject.instance()
    project.addMapLayers([base, compare])
    yield project, base, compare
    project.removeMapLayers([base.id(), compare.id()])


def test_swipe_glyph_registered():
    """tool_icons 必须注册 "swipe" 图标（缺 key 时主题切换会 KeyError）。"""
    from app.ui.common import tool_icons

    assert "swipe" in tool_icons._GLYPHS


@pytest.mark.gui
def test_swipe_button_present(win):
    """通用工具条应包含「卷帘对比」按钮。"""
    acts = [a for a in win.tool_bar.actions() if not a.isSeparator()]
    assert any(a.text() == "卷帘对比" for a in acts)


@pytest.mark.gui
def test_on_swipe_no_raster_warns_instead_of_crash(win, monkeypatch):
    """无栅格图层时 _on_swipe 弹提示，不打开对话框、不崩溃。"""
    from qgis.core import QgsProject

    monkeypatch.setattr(win, "project", QgsProject())  # 隔离共享单例
    warned = []
    monkeypatch.setattr(
        "app.ui.main_window.main_window.QMessageBox.information",
        lambda *a, **k: warned.append(a),
    )
    win._on_swipe()
    assert warned, "应提示先加载栅格图层"


@pytest.mark.gui
def test_swipe_dialog_defaults_and_empty_start(qgis_app, monkeypatch):
    """对话框默认垂直方向；空工程点「开始」弹警告、不 accept。"""
    from PyQt6.QtWidgets import QDialog
    from qgis.core import QgsProject

    from app.ui.dialogs.swipe_compare import SwipeCompareDialog

    dlg = SwipeCompareDialog(None)
    try:
        monkeypatch.setattr(dlg, "project", QgsProject())  # 隔离共享单例
        dlg._populate_layers()
        assert dlg.vertical_radio.isChecked()
        assert dlg.selected_params() == (None, None, "vertical")

        warned = []
        monkeypatch.setattr(
            "app.ui.dialogs.swipe_compare.QMessageBox.warning",
            lambda *a, **k: warned.append(a),
        )
        dlg._on_start()
        assert warned, "空栅格列表点「开始」应弹警告"
        assert dlg.result() != QDialog.DialogCode.Accepted
    finally:
        dlg.close()


@pytest.mark.gui
def test_swipe_session_persists_and_quit_restores(qgis_app, two_rasters):
    """分屏会话常驻：激活后画布只剩左影像；切平移工具不拆；退出才还原原图层集。"""
    from PyQt6.QtWidgets import QApplication
    from qgis.gui import QgsMapCanvas, QgsMapToolPan

    from app.ui.tools.swipe import SwipeTool

    project, base, compare = two_rasters
    canvas = QgsMapCanvas()
    try:
        canvas.setProject(project)
        canvas.setLayers([base, compare])

        tool = SwipeTool(canvas, base, compare)
        canvas.setMapTool(tool)
        assert tool.is_alive()
        assert [layer.id() for layer in canvas.layers()] == [base.id()]

        # 切平移工具：会话保持、画布仍只剩左影像（视图状态常驻）
        canvas.setMapTool(QgsMapToolPan(canvas))
        app = QApplication.instance()
        assert app is not None
        app.processEvents()
        app.processEvents()
        assert tool.is_alive()
        assert [layer.id() for layer in canvas.layers()] == [base.id()]

        # 显式退出（Esc 路径）：还原原图层集
        tool._exit_session()
        app.processEvents()
        app.processEvents()
        assert not tool.is_alive()
        assert [layer.id() for layer in canvas.layers()] == [base.id(), compare.id()]
    finally:
        canvas.deleteLater()


class _FakeMouseEvent:
    """最小事件桩：只暴露 button()/pixelPoint()，驱动 SwipeTool 的拖动逻辑。"""

    def __init__(self, pos) -> None:
        self._pos = pos

    def button(self):
        from PyQt6.QtCore import Qt

        return Qt.MouseButton.LeftButton

    def pixelPoint(self):  # noqa: N802 —— Qt 事件方法命名
        return self._pos


@pytest.mark.gui
def test_swipe_divider_only_drags_from_handle(qgis_app, two_rasters):
    """点分隔条以外不改变占比；只有按住分隔条附近拖动才改变占比。"""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtWidgets import QApplication
    from qgis.gui import QgsMapCanvas

    from app.ui.tools.swipe import SwipeTool

    project, base, compare = two_rasters
    canvas = QgsMapCanvas()
    try:
        canvas.setProject(project)
        canvas.setLayers([base, compare])
        tool = SwipeTool(canvas, base, compare)
        canvas.setMapTool(tool)

        ov = tool._overlay
        assert ov is not None
        ov.resize(400, 300)  # 固定覆盖层几何，测试不依赖真实窗口尺寸
        assert ov.divider_px() == 200  # 初始居中（0.5 * 400）

        def _press(x):
            tool.canvasPressEvent(_FakeMouseEvent(QPointF(x, 0)))

        def _move(x):
            tool.canvasMoveEvent(_FakeMouseEvent(QPointF(x, 0)))

        # 1) 点左边缘（距分隔条 195px，超容差）→ 不进拖拽，移动也不改占比
        _press(5)
        _move(100)
        assert not tool._dragging
        assert ov._divider == pytest.approx(0.5)

        # 2) 按住分隔条附近 → 进拖拽，拖动才改变占比
        _press(200)
        assert tool._dragging
        _move(100)
        assert ov._divider == pytest.approx(0.25)
        _move(300)
        assert ov._divider == pytest.approx(0.75)

        # 3) 松开后移动不再改变占比
        tool.canvasReleaseEvent(_FakeMouseEvent(QPointF(300, 0)))
        assert not tool._dragging
        _move(40)
        assert ov._divider == pytest.approx(0.75)

        # 显式退出会话，deleteLater 前不留 pending 渲染 / 信号连接
        tool._exit_session()
        app = QApplication.instance()
        assert app is not None
        app.processEvents()
    finally:
        canvas.deleteLater()


@pytest.mark.gui
def test_swipe_keeps_vector_layers(qgis_app, two_rasters):
    """矢量两侧都可见：画布只留左影像，矢量等其它图层交给覆盖层离屏渲染；
    分屏期新增矢量也被纳入覆盖层渲染集。"""
    from PyQt6.QtWidgets import QApplication
    from qgis.core import QgsVectorLayer
    from qgis.gui import QgsLayerTreeMapCanvasBridge, QgsMapCanvas

    from app.ui.tools.swipe import SwipeTool

    project, base, compare = two_rasters
    vector = QgsVectorLayer("Point?crs=EPSG:4326", "vec", "memory")
    assert vector.isValid()
    project.addMapLayers([vector])

    canvas = QgsMapCanvas()
    bridge = QgsLayerTreeMapCanvasBridge(project.layerTreeRoot(), canvas)
    bridge.setAutoSetupOnFirstLayer(True)
    try:
        canvas.setProject(project)
        canvas.setLayers([base, compare, vector])

        tool = SwipeTool(canvas, base, compare)
        canvas.setMapTool(tool)
        # 画布只留左影像；矢量进入覆盖层渲染集（画在两侧影像之上）
        assert [layer.id() for layer in canvas.layers()] == [base.id()]
        assert [layer.id() for layer in tool._others] == [vector.id()]

        # 分屏期新增矢量 → 桥接器加进画布 → 纳入覆盖层渲染集，画布仍只留左影像
        vector2 = QgsVectorLayer("Point?crs=EPSG:4326", "vec2", "memory")
        assert vector2.isValid()
        project.addMapLayers([vector2])
        app = QApplication.instance()
        assert app is not None
        for _ in range(5):
            app.processEvents()
        assert [layer.id() for layer in canvas.layers()] == [base.id()]
        others = [layer.id() for layer in tool._others]
        assert vector.id() in others
        assert vector2.id() in others
        assert compare.id() not in others

        # 显式退出会话，deleteLater 前不留渲染 job / 信号连接 / 桥接器残留
        tool._exit_session()
        app = QApplication.instance()
        assert app is not None
        app.processEvents()
        app.processEvents()
    finally:
        canvas.deleteLater()


@pytest.mark.gui
def test_on_swipe_resumes_alive_session_without_dialog(qgis_app, tmp_path, monkeypatch):
    """分屏会话存续时再点「卷帘对比」直接恢复（不弹窗）；只有新会话才弹窗。

    不用 ``win``/``two_rasters`` fixture：两者组合会让真实 MainWindow 的图层树
    桥接器（挂在共享单例上）在 teardown 移除图层时堆损坏（已单独验证）。这里用
    独立 QgsProject + 裸 QgsMapCanvas + 仅暴露 canvas/project 的桩。
    """
    from PyQt6.QtWidgets import QApplication, QDialog
    from qgis.core import QgsProject, QgsRasterLayer
    from qgis.gui import QgsMapCanvas

    from app.ui.dialogs import swipe_compare
    from app.ui.tools.swipe import SwipeTool

    base_path = tmp_path / "base.tif"
    compare_path = tmp_path / "compare.tif"
    _make_tif(base_path, 10)
    _make_tif(compare_path, 200)
    base = QgsRasterLayer(str(base_path), "base")
    compare = QgsRasterLayer(str(compare_path), "compare")
    assert base.isValid()
    assert compare.isValid()

    project = QgsProject()  # 独立工程，不碰共享单例
    project.addMapLayers([base, compare])
    canvas = QgsMapCanvas()
    canvas.setProject(project)
    canvas.setLayers([base, compare])

    class _FakeToolBar:
        """工具条高亮桩：真实 _on_swipe 会调 set_active。"""

        def set_active(self, key=None):
            pass

        def set_session_active(self, key, active):
            pass

    class _FakeWin:
        """只暴露 _on_swipe 用到的 self.canvas / self.project。"""

        def __init__(self, c, p) -> None:
            self.canvas = c
            self.project = p
            self.tool_bar_builder = _FakeToolBar()

    win = _FakeWin(canvas, project)
    from app.ui.main_window.main_window import MainWindow

    win._on_swipe = MainWindow._on_swipe.__get__(win)  # 绑上真实实现
    tool = SwipeTool(canvas, base, compare, "vertical", parent=win)
    win._swipe_tool = tool
    canvas.setMapTool(tool)
    assert tool.is_alive()

    dialogs = []
    monkeypatch.setattr(
        swipe_compare.SwipeCompareDialog,
        "exec",
        lambda *a, **k: dialogs.append(a) or QDialog.DialogCode.Rejected,
    )
    win._on_swipe()
    assert not dialogs, "会话存续时点「卷帘对比」不应弹窗"
    assert canvas.mapTool() is tool

    # 清理：退出会话、卸下工具、销毁画布、断引用，QApplication 存活时显式 GC。
    # 否则 ``_FakeWin ↔ tool`` 循环留到 pytest unconfigure 的 shutdown GC，
    # 那时 QgsApplication 已半毁，收集到残留 C++ 对象会段错误。
    tool._exit_session()
    canvas.unsetMapTool(tool)
    canvas.deleteLater()
    win._swipe_tool = None
    tool.parent_window = None
    app = QApplication.instance()
    assert app is not None
    app.processEvents()
    app.processEvents()  # 处理 deleteLater → 画布销毁 → tool 的 C++ 对象随之销毁
    del tool, win
    import gc

    gc.collect()
