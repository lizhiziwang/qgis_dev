"""「格式转换」产出 GPKG / SQLite 后能被数据管理识别（无界面自检）。

验证 :class:`app.core.domain.vector_format.VectorConvertService`：

1. ``gpkg-new``      —— 新写出的 GPKG 在 ``describe_local_db`` 里登记为 spatial
2. ``gpkg-append``   —— 往已有 GPKG 追加第二层：两层都在、第一层数据不丢
3. ``gpkg-rerun``    —— 同一层重复转换只保留一行注册（幂等）
4. ``sqlite-new``    —— SQLite 目标同样登记
"""

from __future__ import annotations

import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

import _bootstrap_path  # noqa: F401

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parents[1]

from run_sqlite_test import init_qgis, make_layer  # noqa: E402

CASES: list[tuple[str, Callable[[Path], str]]] = []


def case(fn):
    CASES.append((fn.__name__.removeprefix("case_").replace("_", "-"), fn))
    return fn


def expect(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _convert(layer, path: str, fmt_label: str, layer_name: str | None = None) -> str:
    from app.core.domain.vector_format import (
        VECTOR_FORMATS,
        VectorConvertJob,
        VectorConvertService,
    )

    fmt = next(item for item in VECTOR_FORMATS if item.label == fmt_label)
    service = VectorConvertService()
    job = VectorConvertJob(
        layer=layer,
        output_path=path,
        fmt=fmt,
        layer_name=layer_name or Path(path).stem,
    )
    prepared = service.prepare(job)
    written = service.write(prepared)
    del prepared
    return written


def _catalog(path: str) -> dict:
    from app.core.util.local_db_util import describe_local_db

    return describe_local_db(path)


def _spatial(path: str) -> list[str]:
    return list(_catalog(path)["categories"]["spatial"])


@case
def case_gpkg_new(out: Path) -> str:
    target = out / "gpkg_new" / "result.gpkg"
    target.parent.mkdir(parents=True, exist_ok=True)
    layer = make_layer("cities")
    _convert(layer, str(target), "GeoPackage")
    spatial = _spatial(str(target))
    expect(spatial == ["result"], f"注册结果不对：{spatial}")
    catalog = _catalog(str(target))
    expect(catalog["counts"]["result"] == 3, "行数不对")
    meta = catalog["meta"]["result"]
    expect(meta.get("geom_label"), f"几何类型没读出来：{meta}")
    expect("4490" in str(meta.get("crs") or ""), f"坐标系没读出来：{meta}")
    return f"新 GPKG 已登记 spatial={spatial}，几何={meta.get('geom_label')}，坐标系={meta.get('crs')}"


@case
def case_gpkg_append(out: Path) -> str:
    import sqlite3

    target = out / "gpkg_append" / "library.gpkg"
    target.parent.mkdir(parents=True, exist_ok=True)
    _convert(make_layer("first"), str(target), "GeoPackage", layer_name="第一层")
    _convert(make_layer("second"), str(target), "GeoPackage", layer_name="第二层")
    spatial = _spatial(str(target))
    expect(set(spatial) == {"第一层", "第二层"}, f"两层没都在：{spatial}")
    # 第一层的数据必须还在（旧的 CreateOrOverwriteFile 会整库重建）
    conn = sqlite3.connect(str(target))
    try:
        first_count = conn.execute('SELECT COUNT(*) FROM "第一层"').fetchone()[0]
    finally:
        conn.close()
    expect(first_count == 3, f"第一层数据丢了：{first_count}")
    # 多图层库里统计 / 加载都必须定位到指定图层，而不是第 0 层
    from app.core.domain.vector_format import VECTOR_FORMATS, VectorConvertService

    fmt = next(item for item in VECTOR_FORMATS if item.label == "GeoPackage")
    service = VectorConvertService()
    expect(
        service.count_output_features(str(target), fmt, "第一层") == 3,
        "多图层库里统计要素数取错了图层",
    )
    loaded = service.load_result(str(target), "第二层")
    expect(loaded.isValid() and loaded.featureCount() == 3, "load_result 打开的不是刚写入的图层")
    return f"已有库追加成功，spatial={spatial}"


@case
def case_gpkg_rerun(out: Path) -> str:
    import sqlite3

    target = out / "gpkg_rerun" / "again.gpkg"
    target.parent.mkdir(parents=True, exist_ok=True)
    layer = make_layer("cities")
    _convert(layer, str(target), "GeoPackage", layer_name="重复层")
    _convert(layer, str(target), "GeoPackage", layer_name="重复层")
    conn = sqlite3.connect(str(target))
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM user_relation WHERE table_name = '重复层'"
        ).fetchone()[0]
    finally:
        conn.close()
    expect(rows == 1, f"重复注册行：{rows}")
    expect(_spatial(str(target)) == ["重复层"], "重复转换后目录不对")
    return "重复转换注册行保持 1 行"


@case
def case_sqlite_new(out: Path) -> str:
    target = out / "sqlite_new" / "result.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    _convert(make_layer("cities"), str(target), "SQLite")
    spatial = _spatial(str(target))
    # SQLite 驱动可能把表名改成小写，按不区分大小写比较
    expect([name.lower() for name in spatial] == ["result"], f"注册结果不对：{spatial}")
    from app.core.domain.vector_format import VECTOR_FORMATS, VectorConvertService

    fmt = next(item for item in VECTOR_FORMATS if item.label == "SQLite")
    loaded = VectorConvertService().load_result(str(target), "result")
    expect(loaded.isValid() and loaded.featureCount() == 3, "SQLite 产出打不开")
    return f"新 SQLite 已登记 spatial={spatial}"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="格式转换数据管理登记自检")
    parser.add_argument("--out", type=Path, default=_ROOT / ".tmp" / "vector_format_register")
    parser.add_argument("--only", nargs="+", choices=[name for name, _fn in CASES])
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    print(f"[env] 输出目录 = {out}")
    init_qgis()
    selected = [(n, fn) for n, fn in CASES if not args.only or n in args.only]
    results: list[tuple[str, bool, str]] = []
    for name, fn in selected:
        try:
            results.append((name, True, fn(out)))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            results.append((name, False, f"{type(exc).__name__}: {exc}"))
    print("\n" + "=" * 60)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<14} {detail}")
    failed = [name for name, ok, _detail in results if not ok]
    print("=" * 60)
    print(
        f"合计 {len(results)} 项，通过 {len(results) - len(failed)} 项"
        + (f"，失败：{', '.join(failed)}" if failed else "")
    )
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    raise SystemExit(main())
