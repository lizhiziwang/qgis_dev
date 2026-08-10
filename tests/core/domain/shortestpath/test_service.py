"""最短路径分析服务回归测试。

需要真实 QGIS + Processing 插件；CI 默认 ``-m 'not gui'`` 跳过。
"""

from __future__ import annotations

import pytest

from app.core.domain.shortestpath import ShortestPathJob, ShortestPathService


@pytest.fixture(scope="module")
def network_layer(qgis_app):
    """构造简单线网络：三条首尾相接的线（东1、东2、北）。

    求 (0,0) → (20,0) 的最短路径应沿 东1+东2，总长 20（度）。
    """
    from PyQt5.QtCore import QVariant
    from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsProject, QgsVectorLayer

    layer = QgsVectorLayer("LineString?crs=EPSG:4326", "网络", "memory")
    layer.dataProvider().addAttributes([QgsField("name", QVariant.String)])
    layer.updateFields()
    for wkt, name in [
        ("LINESTRING(0 0, 10 0)", "东1"),
        ("LINESTRING(10 0, 20 0)", "东2"),
        ("LINESTRING(0 0, 0 10)", "北"),
    ]:
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        f.setAttribute("name", name)
        assert layer.dataProvider().addFeatures([f])
    QgsProject.instance().addMapLayer(layer)
    yield layer
    QgsProject.instance().removeMapLayers([layer.id()])


@pytest.fixture(scope="module")
def service(qgis_app):
    from processing.core.Processing import Processing

    Processing.initialize()
    return ShortestPathService()


@pytest.mark.gui
def test_point_to_point_shortest(network_layer, service):
    """最短距离策略：路径线 + 总代价正确。"""
    from qgis.core import QgsPointXY

    layer, cost = service.run(
        ShortestPathJob(
            network_layer=network_layer,
            start_point=QgsPointXY(0, 0),
            end_point=QgsPointXY(20, 0),
        )
    )
    assert layer.isValid()
    assert layer.featureCount() == 1
    geom = next(layer.getFeatures()).geometry()
    # 路径应为 (0,0)→(10,0)→(20,0)，首尾一致
    assert geom.asWkt().startswith("LineString (0 0, 10 0, 20 0)")
    # 总代价 = 路径总长（度）
    assert cost == pytest.approx(20.0)


@pytest.mark.gui
def test_fastest_strategy_returns_cost(network_layer, service):
    """最快时间策略能跑通并返回总代价（时间，秒）。"""
    from qgis.core import QgsPointXY

    layer, cost = service.run(
        ShortestPathJob(
            network_layer=network_layer,
            start_point=QgsPointXY(0, 0),
            end_point=QgsPointXY(20, 0),
            strategy=1,
        )
    )
    assert layer.isValid()
    assert layer.featureCount() == 1
    assert cost is not None and cost > 0


@pytest.mark.gui
def test_output_path_writes_gpkg(network_layer, service, tmp_path):
    """给定输出文件路径时写入 gpkg，并返回从文件加载的图层。"""
    from qgis.core import QgsPointXY

    out_path = str(tmp_path / "sp.gpkg")
    layer, cost = service.run(
        ShortestPathJob(
            network_layer=network_layer,
            start_point=QgsPointXY(0, 0),
            end_point=QgsPointXY(20, 0),
            output_name="最短路径结果",
            output_path=out_path,
        )
    )
    assert layer.isValid()
    assert layer.providerType() == "ogr"
    assert layer.name() == "最短路径结果"
    assert cost == pytest.approx(20.0)
    assert tmp_path.joinpath("sp.gpkg").exists()
