"""动态加载属性表 model 回归测试。

修复前（bug1）：分页依赖 ``QgsFeatureRequest.setOffset``，而本 QGIS 构建没有该
方法（``AttributeError`` 被吞掉），页面永远返回 0 行 —— 属性表数据"不全"。
修复后：总量 > ``DYNAMIC_LOAD_THRESHOLD``（默认 1000）才分批动态加载（每批
``STREAM_BATCH``，默认 2000）；≤ 阈值一次性全量加载。

修复前（bug2）：过滤/大图层在 **UI 线程同步全表扫描**，500w 级直接卡死。
现改为 **后台线程流式加载**：``FeatureStreamWorker`` 在工作线程迭代，分批经
信号投递回主线程渐进显示；``show_ids`` 走 ``setFilterFids`` 快路径（替代
``$id in (...)`` 巨表达式，实测 1000 fids × 30 万图层 = 40s 会卡死）。

因此流式路径的 ``load_more`` 是**异步**的（抬高后台 worker 目标即返回），
测试用 :func:`_pump` 轮询事件队列等待结果；≤ 阈值的小图层仍同步全量，测试
可直接断言。

需要真实 QGIS（PyQt5 + QgsApplication）；CI 默认 ``-m 'not gui'`` 跳过。
"""

from __future__ import annotations

import time

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
    return list(model.fids)


def _pump(qgis_app, model, cond, timeout: float = 5.0) -> bool:
    """轮询事件队列直到条件成立或超时（后台 worker 结果经队列信号投递）。

    ``qgis_app`` 是 QApplication 子类，``processEvents()`` 驱动事件投递。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qgis_app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    qgis_app.processEvents()
    return cond()


def _load_all(qgis_app, model, timeout: float = 10.0) -> None:
    """反复 load_more 直到后台流式加载全部完成（has_more 为 False）。"""
    deadline = time.monotonic() + timeout
    while model.has_more:
        model.load_more()
        qgis_app.processEvents()
        time.sleep(0.005)
        if time.monotonic() > deadline:
            break
    while model.has_more and time.monotonic() < deadline:
        qgis_app.processEvents()
        time.sleep(0.005)
    qgis_app.processEvents()


@pytest.mark.gui
def test_large_layer_first_batch_uses_stream_batch(layer, qgis_app, monkeypatch):
    """总量 > 阈值：每批固定 ``STREAM_BATCH`` 条，初始即加载首批（异步）。"""
    _patch_stream(monkeypatch, 10, 24)
    model = PagedAttributeTableModel(layer, page_size=50)

    assert model.total_features == 120
    assert model.total_known is True
    assert model.batch_size == 24
    assert model.scanning is True
    assert _pump(qgis_app, model, lambda: model.rowCount() == 24 and not model.scanning)
    assert model.loaded_count == 24
    assert model.has_more is True


@pytest.mark.gui
def test_incremental_load_until_all_rows(layer, qgis_app, monkeypatch):
    """120 条 / 批 24：load_more 逐批追加 24/48/72/96/120，ID 连续覆盖。"""
    _patch_stream(monkeypatch, 10, 24)
    model = PagedAttributeTableModel(layer, page_size=50)

    expected_counts = [24, 48, 72, 96, 120]
    for i, expected in enumerate(expected_counts, 1):
        if expected < 120:
            assert _pump(qgis_app, model, lambda e=expected: model.rowCount() >= e)
            assert _ids(model)[:expected] == list(range(1, expected + 1))
            assert model.has_more is True
            model.load_more()
        else:
            assert _pump(qgis_app, model, lambda: model.rowCount() == 120 and not model.has_more)
            assert _ids(model) == list(range(1, 121))
            assert model.has_more is False

    # 已加载完：has_more False，再 load_more 是无操作
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
    assert _pump(qgis_app, model, lambda: model.rowCount() == 1)
    model.load_more()
    assert _pump(qgis_app, model, lambda: model.rowCount() == 2)
    model.load_more()
    assert _pump(qgis_app, model, lambda: model.rowCount() == 3 and not model.has_more)
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
    assert model.total_known is True
    assert model.batch_size == 201
    assert _pump(qgis_app, model, lambda: model.rowCount() >= 201)
    assert model.has_more is True

    # 分批加载到完
    _load_all(qgis_app, model)
    assert model.rowCount() == 1001
    assert model.has_more is False


@pytest.mark.gui
def test_filter_resets_to_first_batch(layer, qgis_app, monkeypatch):
    """筛选后重开迭代器，回到首屏首批；过滤路径总数流式结束补齐。"""
    _patch_stream(monkeypatch, 10, 6)
    model = PagedAttributeTableModel(layer, page_size=50)
    _load_all(qgis_app, model)
    assert model.rowCount() == 120

    model.set_filter("$id <= 30")
    # 过滤后无法免遍历获知总数：未知，流式逐批追加
    assert model.total_known is False
    assert model.batch_size == 6
    assert _pump(qgis_app, model, lambda: model.rowCount() == 6 and not model.scanning)
    assert _ids(model) == list(range(1, 7))

    _load_all(qgis_app, model)
    assert _ids(model) == list(range(1, 31))
    assert model.total_features == 30
    assert model.total_known is True
    assert model.has_more is False


@pytest.mark.gui
def test_show_ids_subset(layer, qgis_app, monkeypatch):
    """show_ids 限制为指定要素子集（setFilterFids），总数立即可知。"""
    _patch_stream(monkeypatch, 4, 2)
    wanted = [5, 6, 7, 8, 9, 10, 11, 12]
    model = PagedAttributeTableModel(layer, page_size=10, show_ids=wanted)

    # show_ids 仅自身：总数 = len 已知；超阈值走分批流式
    assert model.total_known is True
    assert model.total_features == len(wanted)
    assert model.batch_size == 2
    # 首屏加载 batch_size 条（setFilterFids 返回顺序不保证，只断言条数与归属）
    assert _pump(qgis_app, model, lambda: model.rowCount() >= 2)
    assert len(_ids(model)) == 2
    assert set(_ids(model)) <= set(wanted)

    _load_all(qgis_app, model)
    # setFilterFids 返回顺序不保证，按集合比较
    assert set(_ids(model)) == set(wanted)
    assert model.total_features == len(wanted)
    assert model.total_known is True
    assert model.has_more is False


@pytest.mark.gui
def test_show_ids_with_filter_composes(layer, qgis_app, monkeypatch):
    """show_ids（setFilterFids）与筛选表达式可组合，取交集。"""
    _patch_stream(monkeypatch, 4, 2)
    model = PagedAttributeTableModel(layer, page_size=10, filter_str="$id <= 7", show_ids=[5, 6, 7, 8])

    # show_ids + 过滤：总数需流式结束补齐
    assert model.total_known is False
    _load_all(qgis_app, model)
    assert set(_ids(model)) == {5, 6, 7}
    assert model.total_features == 3
    assert model.has_more is False


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
def test_cancel_stream_stops_loading(layer, qgis_app, monkeypatch):
    """取消流式（编辑开始/关闭时）：不再续取、不崩溃、load_more 无操作。"""
    _patch_stream(monkeypatch, 10, 24)
    model = PagedAttributeTableModel(layer, page_size=50)
    assert _pump(qgis_app, model, lambda: model.rowCount() >= 24)

    model.cancel_stream()
    assert model.has_more is False
    assert model.scanning is False
    rows_before = model.rowCount()
    model.load_more()  # 无操作
    assert model.rowCount() == rows_before


@pytest.mark.gui
def test_truncation_caps_memory(layer, qgis_app, monkeypatch):
    """结果超过 MAX_MODEL_FIDS：截断加载、不再增长、状态可读（防失控兜底）。"""
    _patch_stream(monkeypatch, 10, 24)
    monkeypatch.setattr("app.ui.docks.attribute_dock.MAX_MODEL_FIDS", 60)
    model = PagedAttributeTableModel(layer, page_size=50)

    _load_all(qgis_app, model)
    assert model.rowCount() == 60
    assert model.truncated is True
    assert model.has_more is False
    rows_before = model.rowCount()
    model.load_more()  # 截断后无操作
    assert model.rowCount() == rows_before


@pytest.mark.gui
def test_feature_at_on_demand_and_cache_bounded(layer, qgis_app, monkeypatch):
    """按需取要素：feature_at 只取可视行，缓存有界、超限淘汰最久未用。"""
    from app.ui.docks import attribute_dock as ad

    _patch_stream(monkeypatch, 10, 24)
    monkeypatch.setattr(ad, "MAX_FEATURE_CACHE", 5)
    model = PagedAttributeTableModel(layer, page_size=50)
    _load_all(qgis_app, model)
    assert model.rowCount() == 120

    # 按需取：未访问过的不在缓存，feature_at 触达后进入缓存
    assert len(model._feature_cache) == 0
    feat = model.feature_at(0)
    assert feat is not None and feat.id() == model.fids[0]
    assert set(model._feature_cache) == {1}

    # 遍历超过缓存上限：只保留最近 5 个，内存不随总行数增长
    for r in range(10):
        model.feature_at(r)
    assert len(model._feature_cache) <= 5
    assert set(model._feature_cache) <= set(range(1, 11))

    # 旧窗口被淘汰后重取仍正确（缓存未命中时按 fid 重取）
    assert model.feature_at(0).id() == 1


@pytest.mark.gui
def test_widget_loads_more_on_button(layer, qgis_app, monkeypatch):
    """widget 初始显示首批，点"加载更多"逐批追加，状态文本随进度更新。"""
    from app.ui.docks.attribute_dock import PagedAttributeTableWidget

    _patch_stream(monkeypatch, 10, 24)
    w = PagedAttributeTableWidget(layer, page_size=50)
    assert _pump(qgis_app, w.model, lambda: w.model.rowCount() == 24 and not w.model.scanning)
    assert "24 / 120" in w.load_status_label.text()
    assert w.load_more_btn.isEnabled()

    w.load_more_btn.click()
    assert _pump(qgis_app, w.model, lambda: w.model.rowCount() == 48 and not w.model.scanning)
    assert "48 / 120" in w.load_status_label.text()

    # 全部加载完：按钮禁用，标签显示全部加载
    _load_all(qgis_app, w.model)
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
def test_widget_filter_status_unknown_total(layer, qgis_app, monkeypatch):
    """过滤后总数未知：先"正在加载/已加载 X 条"，流式结束补齐总数。"""
    from app.ui.docks.attribute_dock import PagedAttributeTableWidget

    _patch_stream(monkeypatch, 10, 6)
    w = PagedAttributeTableWidget(layer, page_size=50)
    w.model.set_filter("$id <= 30")

    assert w.model.total_known is False
    assert _pump(qgis_app, w.model, lambda: w.model.rowCount() == 6 and not w.model.scanning)
    assert "已加载 6 条" in w.load_status_label.text()
    assert w.load_more_btn.isEnabled()

    _load_all(qgis_app, w.model)
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

    fid = w.model.fids[0]
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

    fid = w.model.fids[0]
    extent = w.model.feature_at(0).geometry().boundingBox()
    w.zoom_to_map.emit("zoomTo", {"extent": extent, "id": [fid]})

    assert list(layer.selectedFeatureIds()) == [fid]
    assert len(zoom_calls) == 1
    assert zoom_calls[0][0] is extent


@pytest.mark.gui
def test_main_window_has_zoom_to_layer_extent_transform(qgis_app):
    """MainWindow 提供 zoom_to_layer_extent_transform（"缩放到要素"依赖它）。"""
    from app.ui.main_window.main_window import MainWindow

    assert hasattr(MainWindow, "zoom_to_layer_extent_transform")


@pytest.mark.gui
def test_widget_columns_stretch_to_fill_container(layer, qgis_app):
    """列宽默认 Stretch：均分填满容器宽度，不留右侧空白。"""
    from PyQt6.QtWidgets import QHeaderView

    from app.ui.docks.attribute_dock import PagedAttributeTableWidget

    w = PagedAttributeTableWidget(layer, page_size=50)
    header = w.table_view.horizontalHeader()
    assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.Stretch
    assert header.stretchLastSection() is False
