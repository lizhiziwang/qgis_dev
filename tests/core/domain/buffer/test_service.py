"""缓冲区服务回归测试：GDB 图层短字符串字段超长值触发「无法将要素写入 OUTPUT」。

根因（2026-08-07 查明）：GDB/ArcGIS 导出图层中短代码字段的声明长度小于实际
数据（如 ``XIANG`` 声明 3 却存字面量 ``'NULL'`` 占 4 字符）。QGIS 的 memory
provider 严格校验字符串长度，超长值在写入时被拒绝（``addFeatures`` 返回 False、
``QgsVectorLayerFeatureSink`` 拒绝），算法 sink 因此报「无法将要素写入 OUTPUT」。

缓冲服务在跑算法前先调用
:func:`~app.core.domain.geom_cleanup.copy_with_widened_string_fields`
把 String 字段长度放宽到实际最大值。本文件为回归保护；CI 默认 ``-m 'not gui'``
跳过（需要真实 QGIS）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.domain.buffer import BufferJob, BufferService
from app.core.domain.geom_cleanup import copy_with_widened_string_fields

#: 真实触发数据：GDB 短代码字段声明长度小于实际存储值。可用环境变量覆盖，
#: 文件不存在时相关测试自动跳过（不影响其它环境）。
_GDB = r"D:\work\安远县塘村乡白兔村长坑猪场年出栏4000头生猪养殖项目林地审核审批数据库.gdb(1)\安远县塘村乡白兔村长坑猪场年出栏4000头生猪养殖项目林地审核审批数据库.gdb"
_GDB_LAYER = "XX县_XX项目名称_林地图斑_20260304"


@pytest.fixture(scope="module")
def geojson_short_string_layer(qgis_app, tmp_path_factory):
    """构造 String 字段声明长度小于实际值的图层（GeoJSON 不强制长度）。

    GeoJSON 字符串字段在 QGIS 里报告长度 0（无上限），数据可存 4 字符
    ``'NULL'``，而宽化逻辑按「实际值长度 > 声明长度」触发，正好可测。
    """
    from qgis.core import QgsVectorLayer

    gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"code": "NULL"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                },
            }
        ],
    }
    path = tmp_path_factory.mktemp("gj") / "trigger.geojson"
    path.write_text(json.dumps(gj, ensure_ascii=False), encoding="utf-8")
    layer = QgsVectorLayer(str(path), "触发层", "ogr")
    assert layer.isValid()
    return layer


@pytest.fixture(scope="module")
def gdb_layer(qgis_app):
    """真实 GDB 触发图层；数据文件缺失则跳过。"""
    import os

    from qgis.core import QgsVectorLayer

    gdb = os.environ.get("QGIS_BUFFER_GDB", _GDB)
    if not Path(gdb).exists():
        pytest.skip(f"真实 GDB 数据不存在：{gdb}")
    layer = QgsVectorLayer(f"{gdb}|layername={_GDB_LAYER}", "林地图斑", "ogr")
    assert layer.isValid()
    return layer


@pytest.mark.gui
def test_widen_string_fields_when_value_exceeds_declared_length(geojson_short_string_layer):
    """超长值字段的长度应放宽到实际最大值，且数据保真。"""
    cleaned = copy_with_widened_string_fields(geojson_short_string_layer)
    assert cleaned is not geojson_short_string_layer
    assert cleaned.fields().field("code").length() == 4
    assert [f.attribute("code") for f in cleaned.getFeatures()] == ["NULL"]


@pytest.mark.gui
def test_no_change_when_string_lengths_fit(qgis_app):
    """字段声明长度足够时原样返回，不产生拷贝开销。"""
    from PyQt6.QtCore import QVariant
    from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsVectorLayer

    layer = QgsVectorLayer("Polygon?crs=EPSG:4490", "宽字段层", "memory")
    layer.dataProvider().addAttributes([QgsField("code", QVariant.String, "string", 50, 0)])
    layer.updateFields()
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,1 0,1 1,0 1,0 0))"))
    feature.setAttribute("code", "NULL")
    layer.dataProvider().addFeatures([feature])

    cleaned = copy_with_widened_string_fields(layer)
    assert cleaned is layer


@pytest.mark.gui
def test_buffer_succeeds_on_real_gdb(gdb_layer):
    """真实 GDB 图层经修正后 buffer 全量写出（此前报「无法将要素写入 OUTPUT」）。"""
    from processing.core.Processing import Processing

    Processing.initialize()
    result = BufferService().run(
        BufferJob(layer=gdb_layer, distance=0.01, output_name="回归测试")
    )
    assert result.featureCount() == gdb_layer.featureCount()


@pytest.mark.gui
def test_output_path_writes_gpkg(qgis_app, tmp_path):
    """给定输出文件路径时写入 gpkg，并返回从文件加载的图层。"""
    from PyQt6.QtCore import QVariant
    from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsVectorLayer

    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "缓冲输入", "memory")
    layer.dataProvider().addAttributes([QgsField("name", QVariant.String)])
    layer.updateFields()
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,10 0,10 10,0 10,0 0))"))
    feature.setAttribute("name", "A")
    layer.dataProvider().addFeatures([feature])

    out_path = str(tmp_path / "buf.gpkg")
    result = BufferService().run(
        BufferJob(layer=layer, distance=1.0, output_name="缓冲区结果", output_path=out_path)
    )
    assert result.isValid()
    assert result.providerType() == "ogr"
    assert result.featureCount() == 1
    assert Path(out_path).exists()
