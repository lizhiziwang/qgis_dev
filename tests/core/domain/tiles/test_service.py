"""测试 :class:`app.core.domain.tiles.service.TileJob` 与 :func:`to_native_params`。"""

from __future__ import annotations

from app.core.domain.tiles.service import TileJob


def test_default_job_xyz_template():
    job = TileJob(output_dir="D:/tmp")
    assert job.xyz_template == "{z}/{x}/{y}.pbf"
    assert job.min_zoom == 0
    assert job.max_zoom == 3


def test_to_native_params_full():
    job = TileJob(
        output_dir="D:/tmp/xyz",
        xyz_template="{z}/{x}/{y}.pbf",
        layer_sources=[
            "D:/data/region.shp|layername=region",
            "D:/data/dem.tif",
        ],
        min_zoom=5,
        max_zoom=12,
        extent=(100.0, 200.0, 50.0, 150.0, "EPSG:4490"),
    )
    p = job.to_native_params()
    assert p["OUTPUT_DIRECTORY"] == "D:/tmp/xyz"
    assert p["XYZ_TEMPLATE"] == "{z}/{x}/{y}.pbf"
    assert p["MIN_ZOOM"] == 5
    assert p["MAX_ZOOM"] == 12
    assert p["LAYERS"] == [
        {"layer": "D:/data/region.shp|layername=region"},
        {"layer": "D:/data/dem.tif"},
    ]
    assert p["EXTENT"] == "100.0,200.0,50.0,150.0 [EPSG:4490]"


def test_to_native_params_empty_layers():
    job = TileJob(output_dir="D:/out")
    p = job.to_native_params()
    assert p["LAYERS"] == []
    assert p["EXTENT"].endswith("[EPSG:4490]")
