"""动态加载属性表 model 回归测试。

修复前（bug1）：分页依赖 ``QgsFeatureRequest.setOffset``，而本 QGIS 构建没有该
方法（``AttributeError`` 被吞掉），页面永远返回 0 行 —— 属性表数据"不全"。
修复后：总量 > ``DYNAMIC_LOAD_THRESHOLD``（默认 1000）才分批动态加载（每批
``STREAM_BATCH``，默认 2000）；≤ 阈值一次性全量加载。

修复前（bug2，本文件主测对象）：为分页在 UI 线程**预收集全部要素 ID** 并取
总量的 20% 作批次，500w 级图层直接卡死。现改为持有一个 ``QgsFeatureIterator``
流式续取，不预收集任何 ID；总数在无过滤时用 ``featureCount()`` 快路径，
带过滤/show_ids 时流式结束（迭代器耗尽）后补齐（期间 ``total_known`` 为 False）。

分页机制测试用 monkeypatch 把 ``STREAM_BATCH`` 调小，让 120 条的小图层也走
分页路径；阈值行为测试覆盖"小图层全量 / 大图层分批"。

需要真实 QGIS（PyQt5 + QgsApplication）；CI 默认 ``-m 'not gui'`` 跳过。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from app.ui.docks.attribute_dock import PagedAttributeTableModel  # noqa: E402


@pytest.fixture
def layer(qgis_app):
    """120 个要素的 memory 点图层（provider 分配 $id 1..120）。"""
    from qgis.core import QgsFeature, QgsGeometry, QgsVectorLayer

    lyr = QgsVectorLayer("Point?crs=EPSG:4326", "动态加载测试点", "memory")
    feats = []
    for i in range(120):
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromWkt(f"POINT({i} {i})"))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    return lyr


def _patch_stream(monkeypatch, threshold: int, batch: int) -> None:
    """调小动态加载阈值与批次，让 120 条的小图层也走分批路径。"""
    monkeypatch.setattr("app.ui.docks.attribute_dock.DYNAMIC_LOAD_THRESHOLD", threshold)
    monkeypatch.setattr("app.ui.docks.attribute_dock.STREAM_BATCH", batch)


def _ids(model):
    return [f.id() for f in model.features]


@pytest.mark.gui
def test_large_layer_first_batch_uses_stream_batch(layer, monkeypatch):
    """总量 > 阈值：每批固定 ``STREAM_BATCH`` 条，初始即加载首批。"""
    _patch_stream(monkeypatch, 10, 24)
    model = PagedAttributeTableModel(layer, page_size=50)

    assert model.total_features == 120
    assert model.total_known is True
    assert model.batch_size == 24
    assert model.rowCount() == 24
    assert model.loaded_count == 24
    assert model.has_more is True


@pytest.mark.gui
def test_incremental_load_until_all_rows(layer, monkeypatch):
    """120 条 / 批 24：load_more 逐批追加 24/48/72/96/120，ID 连续覆盖。"""
    _patch_stream(monkeypatch, 10, 24)
    model = PagedAttributeTableModel(layer, page_size=50)

    expected_counts = [24, 48, 72, 96, 120]
    for i, expected in enumerate(expected_counts, 1):
        assert model.rowCount() == expected
        assert _ids(model) == list(range(1, expected + 1))
        assert model.has_more is (i < len(expected_counts))
        if i < len(expected_counts):
            model.load_more()

    # 已加载完：has_more False，再 load_more 是无操作
    assert model.has_more is False
    model.load_more()
    assert model.rowCount() == 120


@pytest.mark.gui
def test_small_layer_batch_at_least_one(qgis_app, monkeypatch):
    """小数据量：批次至少 1 条，且逐批加载完不超过总量。"""
    from qgis.core import QgsFeature, QgsGeometry, QgsVectorLayer

    lyr = QgsVectorLayer("Point?crs=EPSG:4326", "少", "memory")
    feats = []
    for i in range(3):
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromWkt(f"POINT({i} {i})"))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)

    _patch_stream(monkeypatch, 1, 1)  # 3 > 1 → 仍走分页
    model = PagedAttributeTableModel(lyr, page_size=50)
    assert model.batch_size == 1
    assert model.rowCount() == 1
    model.load_more()
    assert model.rowCount() == 2
    model.load_more()
    assert model.rowCount() == 3
    assert model.has_more is False


@pytest.mark.gui
def test_small_layer_loads_all_at_once(qgis_app):
    """≤ 阈值（默认 1000）：一次性全量加载，不出现分页。"""
    from qgis.core import QgsFeature, QgsGeometry, QgsVectorLayer

    lyr = QgsVectorLayer("Point?crs=EPSG:4326", "小", "memory")
    feats = []
    for i in range(3):
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromWkt(f"POINT({i} {i})"))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)

    model = PagedAttributeTableModel(lyr, page_size=50)
    assert model.batch_size == 3  # 总量本身
    assert model.rowCount() == 3
    assert _ids(model) == [1, 2, 3]
    assert model.has_more is False


@pytest.mark.gui
def test_layer_over_threshold_pages(qgis_app, monkeypatch):
    """> 阈值（1000）：按 ``STREAM_BATCH`` 分批动态加载。"""
    from qgis.core import QgsFeature, QgsGeometry, QgsVectorLayer

    lyr = QgsVectorLayer("Point?crs=EPSG:4326", "大", "memory")
    feats = []
    for i in range(1001):
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromWkt(f"POINT({i} {i})"))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)

    _patch_stream(monkeypatch, 1000, 201)
    model = PagedAttributeTableModel(lyr, page_size=50)
    assert model.total_features == 1001
    assert model.batch_size == 201
    assert model.rowCount() == 201
    assert model.has_more is True

    # 分批加载到完
    while model.has_more:
        model.load_more()
    assert model.rowCount() == 1001
    assert model.has_more is False


@pytest.mark.gui
def test_filter_resets_to_first_batch(layer, monkeypatch):
    """筛选后重开迭代器，回到首屏首批；过滤路径总数流式结束补齐。"""
    _patch_stream(monkeypatch, 10, 6)
    model = PagedAttributeTableModel(layer, page_size=50)
    model.load_more()
    model.load_more()
    assert model.rowCount() == 18

    model.set_filter("$id <= 30")
    # 过滤后无法免遍历获知总数：未知，流式逐批追加
    assert model.total_known is False
    assert model.batch_size == 6
    assert model.rowCount() == 6
    assert _ids(model) == list(range(1, 7))

    while model.has_more:
        model.load_more()
    assert _ids(model) == list(range(1, 31))
    assert model.total_features == 30
    assert model.total_known is True
    assert model.has_more is False


@pytest.mark.gui
def test_show_ids_subset(layer, monkeypatch):
    """show_ids 限制为指定要素子集，动态加载只在这些 ID 内进行。"""
    _patch_stream(monkeypatch, 4, 2)
    wanted = [5, 6, 7, 8, 9, 10, 11, 12]
    model = PagedAttributeTableModel(layer, page_size=10, show_ids=wanted)

    # show_ids 走过滤路径：总数未知，流式结束后补齐
    assert model.total_known is False
    assert model.batch_size == 2
    assert _ids(model) == wanted[:2]

    while model.has_more:
        model.load_more()
    assert _ids(model) == wanted
    assert model.total_features == len(wanted)
    assert model.total_known is True


@pytest.mark.gui
def test_empty_layer_no_crash(qgis_app):
    """空图层：0 条，has_more False，load_more 无操作。"""
    from qgis.core import QgsVectorLayer

    empty = QgsVectorLayer("Point?crs=EPSG:4326", "空", "memory")
    model = PagedAttributeTableModel(empty, page_size=50)

    assert model.total_features == 0
    assert model.batch_size == 0
    assert model.rowCount() == 0
    assert model.has_more is False
    model.load_more()
    assert model.rowCount() == 0


@pytest.mark.gui
def test_widget_loads_more_on_button(layer, monkeypatch):
    """widget 初始显示首批，点"加载更多"逐批追加，状态文本随进度更新。"""
    from app.ui.docks.attribute_dock import PagedAttributeTableWidget

    _patch_stream(monkeypatch, 10, 24)
    w = PagedAttributeTableWidget(layer, page_size=50)
    assert w.model.rowCount() == 24
    assert "24 / 120" in w.load_status_label.text()
    assert w.load_more_btn.isEnabled()

    w.load_more_btn.click()
    assert w.model.rowCount() == 48
    assert "48 / 120" in w.load_status_label.text()

    # 全部加载完：按钮禁用，标签显示全部加载
    while w.model.has_more:
        w.load_more_btn.click()
    assert w.model.rowCount() == 120
    assert not w.load_more_btn.isEnabled()
    assert "已全部加载" in w.load_status_label.text()


@pytest.mark.gui
def test_widget_small_layer_no_load_button(qgis_app):
    """≤ 阈值：widget 一次性全量加载，"加载更多"按钮默认禁用、状态显示已全部。"""
    from qgis.core import QgsFeature, QgsGeometry, QgsVectorLayer

    from app.ui.docks.attribute_dock import PagedAttributeTableWidget

    lyr = QgsVectorLayer("Point?crs=EPSG:4326", "小", "memory")
    feats = []
    for i in range(5):
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromWkt(f"POINT({i} {i})"))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)

    w = PagedAttributeTableWidget(lyr, page_size=50)
    assert w.model.rowCount() == 5
    assert not w.load_more_btn.isEnabled()
    assert "已全部加载" in w.load_status_label.text()


@pytest.mark.gui
def test_widget_filter_status_unknown_total(layer, monkeypatch):
    """过滤后总数未知：状态栏先显示"已加载 X 条"，流式结束补齐总数。"""
    from app.ui.docks.attribute_dock import PagedAttributeTableWidget

    _patch_stream(monkeypatch, 10, 6)
    w = PagedAttributeTableWidget(layer, page_size=50)
    w.model.set_filter("$id <= 30")

    assert w.model.total_known is False
    assert w.model.rowCount() == 6
    assert "已加载 6 条" in w.load_status_label.text()
    assert w.load_more_btn.isEnabled()

    while w.model.has_more:
        w.load_more_btn.click()
    assert w.model.total_features == 30
    assert w.model.total_known is True
    assert not w.load_more_btn.isEnabled()
    assert "共 30 条" in w.load_status_label.text()


@pytest.mark.gui
def test_context_menu_select_feature_selects_and_refreshes(layer):
    """右键"选中要素"：经 zoom_to_map 信号选中要素并刷新画布（修复前无刷新）。"""
    from types import SimpleNamespace

    from PyQt6.QtWidgets import QMainWindow

    from app.ui.docks.attribute_dock import AttributeDock

    refreshed = []
    parent = QMainWindow()
    parent.canvas = SimpleNamespace(refresh=lambda: refreshed.append(True))
    dock = AttributeDock(layer, page_size=50, parent=parent)
    w = dock.content_widget

    fid = w.model.features[0].id()
    w.zoom_to_map.emit("selectFeature", {"id": [fid]})

    assert list(layer.selectedFeatureIds()) == [fid]
    assert refreshed


@pytest.mark.gui
def test_context_menu_zoom_to_feature_zooms_and_selects(layer):
    """右键"缩放到要素"：选中要素并调用 topParent 的 zoom_to_layer_extent_transform。

    修复前 MainWindow 没有该方法，dock handler 的 hasattr 检查为 False，缩放被静默跳过。
    """
    from PyQt6.QtWidgets import QMainWindow

    from app.ui.docks.attribute_dock import AttributeDock

    zoom_calls = []
    parent = QMainWindow()
    parent.canvas = None
    parent.zoom_to_layer_extent_transform = (
        lambda extent=None, source_crs=None: zoom_calls.append((extent, source_crs))
    )
    dock = AttributeDock(layer, page_size=50, parent=parent)
    w = dock.content_widget

    fid = w.model.features[0].id()
    extent = w.model.features[0].geometry().boundingBox()
    w.zoom_to_map.emit("zoomTo", {"extent": extent, "id": [fid]})

    assert list(layer.selectedFeatureIds()) == [fid]
    assert len(zoom_calls) == 1
    assert zoom_calls[0][0] is extent


@pytest.mark.gui
def test_main_window_has_zoom_to_layer_extent_transform(qgis_app):
    """MainWindow 提供 zoom_to_layer_extent_transform（"缩放到要素"依赖它）。"""
    from app.ui.main_window.main_window import MainWindow

    assert hasattr(MainWindow, "zoom_to_layer_extent_transform")
