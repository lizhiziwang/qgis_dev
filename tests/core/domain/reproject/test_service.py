"""重投影服务回归测试。

需要真实 QGIS + Processing 插件；CI 默认 ``-m 'not gui'`` 跳过。
"""

from __future__ import annotations

import pytest

from app.core.domain.reproject import ReprojectJob, ReprojectService


@pytest.fixture(scope="module")
def input_layer(qgis_app):
    """EPSG:4326 单点图层：北京 (116.39, 39.90)。"""
    from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer

    layer = QgsVectorLayer("Point?crs=EPSG:4326", "北京点", "memory")
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromWkt("POINT(116.39 39.90)"))
    layer.dataProvider().addFeatures([f])
    QgsProject.instance().addMapLayer(layer)
    yield layer
    QgsProject.instance().removeMapLayers([layer.id()])


@pytest.fixture(scope="module")
def service(qgis_app):
    from processing.core.Processing import Processing

    Processing.initialize()
    return ReprojectService()


@pytest.mark.gui
def test_reproject_to_web_mercator(input_layer, service):
    """WGS84 → Web Mercator：CRS 正确、坐标按目标坐标系重算。"""
    from qgis.core import QgsCoordinateReferenceSystem

    result = service.run(
        ReprojectJob(
            input_layer=input_layer,
            target_crs=QgsCoordinateReferenceSystem("EPSG:3857"),
        )
    )
    assert result.isValid()
    assert result.crs().authid() == "EPSG:3857"
    assert result.featureCount() == 1
    geom = next(result.getFeatures()).geometry()
    point = geom.asPoint()
    # 116.39,39.90 在 Web Mercator 下约 (12956475.5, 4851421.2)
    assert point.x() == pytest.approx(12956475.53, rel=1e-4)
    assert point.y() == pytest.approx(4851421.18, rel=1e-4)


@pytest.mark.gui
def test_memory_output_keeps_name(input_layer, service):
    """memory 输出图层名与输入一致，CRS 为目标坐标系。"""
    from qgis.core import QgsCoordinateReferenceSystem

    result = service.run(
        ReprojectJob(
            input_layer=input_layer,
            target_crs=QgsCoordinateReferenceSystem("EPSG:32650"),
            output_name="重投影点",
        )
    )
    assert result.isValid()
    assert result.name() == "重投影点"
    assert result.crs().authid() == "EPSG:32650"


@pytest.mark.gui
def test_output_path_writes_gpkg(input_layer, service, tmp_path):
    """给定输出文件路径时写入 gpkg，并返回从文件加载的图层。"""
    from qgis.core import QgsCoordinateReferenceSystem

    out_path = str(tmp_path / "reproject.gpkg")
    result = service.run(
        ReprojectJob(
            input_layer=input_layer,
            target_crs=QgsCoordinateReferenceSystem("EPSG:3857"),
            output_name="重投影文件",
            output_path=out_path,
        )
    )
    assert result.isValid()
    assert result.providerType() == "ogr"
    assert result.name() == "重投影文件"
    assert tmp_path.joinpath("reproject.gpkg").exists()


@pytest.mark.gui
def test_invalid_crs_raises(input_layer, service):
    """目标坐标系无效时抛 ValueError。"""
    from qgis.core import QgsCoordinateReferenceSystem

    with pytest.raises(ValueError):
        service.run(
            ReprojectJob(
                input_layer=input_layer,
                target_crs=QgsCoordinateReferenceSystem("EPSG:999999"),
            )
        )
