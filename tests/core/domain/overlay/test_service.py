"""叠加分析服务回归测试。

需要真实 QGIS + Processing 插件；CI 默认 ``-m 'not gui'`` 跳过。
"""

from __future__ import annotations

import pytest

from app.core.domain.overlay import OPERATION_NAMES, OverlayJob, OverlayService


@pytest.fixture(scope="module")
def overlay_layers(qgis_app):
    """构造两个相交的矩形 memory 图层：
    A 为 [0,0]-[10,10]，B 为 [5,5]-[15,15]，重叠区 [5,5]-[10,10]。
    """
    from PyQt6.QtCore import QVariant
    from qgis.core import (
        QgsFeature,
        QgsField,
        QgsGeometry,
        QgsProject,
        QgsVectorLayer,
    )

    def make_layer(name, wkt):
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", name, "memory")
        layer.dataProvider().addAttributes([QgsField("name", QVariant.String)])
        layer.updateFields()
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromWkt(wkt))
        feature.setAttribute("name", name)
        assert layer.dataProvider().addFeatures([feature])
        QgsProject.instance().addMapLayer(layer)
        return layer

    a = make_layer("A", "POLYGON((0 0,10 0,10 10,0 10,0 0))")
    b = make_layer("B", "POLYGON((5 5,15 5,15 15,5 15,5 5))")
    yield a, b
    QgsProject.instance().removeMapLayers([a.id(), b.id()])


@pytest.fixture(scope="module")
def service(qgis_app):
    from processing.core.Processing import Processing

    Processing.initialize()
    return OverlayService()


@pytest.mark.gui
def test_intersection_keeps_overlap(overlay_layers, service):
    a, b = overlay_layers
    result = service.run(OverlayJob(input_layer=a, overlay_layer=b, operations=("intersection",)))
    layer = result["intersection"]
    assert layer.isValid()
    assert layer.featureCount() == 1
    geom = next(layer.getFeatures()).geometry()
    assert geom.area() == pytest.approx(25.0)  # 5x5 重叠区
    # 交集携带双方属性
    assert layer.fields().count() == a.fields().count() + b.fields().count()


@pytest.mark.gui
def test_union_splits_overlap(overlay_layers, service):
    a, b = overlay_layers
    result = service.run(OverlayJob(input_layer=a, overlay_layer=b, operations=("union",)))
    layer = result["union"]
    assert layer.isValid()
    assert layer.featureCount() == 3  # A独有 + B独有 + 重叠


@pytest.mark.gui
def test_difference_removes_overlap(overlay_layers, service):
    a, b = overlay_layers
    result = service.run(OverlayJob(input_layer=a, overlay_layer=b, operations=("difference",)))
    layer = result["difference"]
    assert layer.isValid()
    # A 去重叠后只剩 100-25=75 面积，单要素
    assert layer.featureCount() == 1
    geom = next(layer.getFeatures()).geometry()
    assert geom.area() == pytest.approx(75.0)


@pytest.mark.gui
def test_symmetrical_difference_excludes_overlap(overlay_layers, service):
    a, b = overlay_layers
    result = service.run(
        OverlayJob(input_layer=a, overlay_layer=b, operations=("symmetricaldifference",))
    )
    layer = result["symmetricaldifference"]
    assert layer.isValid()
    assert layer.featureCount() == 2  # A独有 + B独有
    total = sum(f.geometry().area() for f in layer.getFeatures())
    assert total == pytest.approx(150.0)


@pytest.mark.gui
def test_all_operations_at_once(overlay_layers, service):
    a, b = overlay_layers
    job = OverlayJob(input_layer=a, overlay_layer=b, operations=tuple(OPERATION_NAMES))
    result = service.run(job)
    assert set(result) == set(OPERATION_NAMES)
    for key, layer in result.items():
        assert layer.isValid(), key
        assert layer.name() == f"叠加结果_{OPERATION_NAMES[key]}"


@pytest.mark.gui
def test_unknown_operation_is_skipped(overlay_layers, service):
    a, b = overlay_layers
    result = service.run(OverlayJob(input_layer=a, overlay_layer=b, operations=("intersection", "nope")))
    assert set(result) == {"intersection"}


@pytest.mark.gui
def test_output_dir_writes_gpkg_files(overlay_layers, service, tmp_path):
    """给出输出目录时，每个运算写一个 gpkg 文件并返回从文件加载的图层。"""
    a, b = overlay_layers
    job = OverlayJob(
        input_layer=a,
        overlay_layer=b,
        operations=("intersection", "union"),
        output_dir=str(tmp_path),
    )
    result = service.run(job)
    assert set(result) == {"intersection", "union"}
    for op, layer in result.items():
        assert layer.isValid(), op
        assert layer.providerType() == "ogr", op
        assert layer.name() == f"叠加结果_{OPERATION_NAMES[op]}"
    written = {p.name for p in tmp_path.iterdir()}
    assert written == {"叠加结果_交集.gpkg", "叠加结果_并集.gpkg"}
