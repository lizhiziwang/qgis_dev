"""SQLite 数据库保存 / 打开自检 CLI。

无界面（offscreen）跑完整链路，验证当前 QGIS 运行时的 SQLite 家族读写能力：

1. ``plain``      —— 纯 ``sqlite3``：建库 → 写 → 读 → 改 → 删 → 重开校验持久化
2. ``gpkg``       —— QGIS 图层 → GeoPackage 保存 → ``ogr`` provider 打开回读
3. ``gpkg-multi`` —— 同一个 ``.gpkg`` 追加第二个图层，两层独立可开
4. ``spatialite`` —— QGIS 图层 → SpatiaLite 保存 → ``spatialite`` provider 打开
5. ``extension``  —— ``mod_spatialite`` 扩展加载 + 空间 SQL（``ST_Intersects``）

用法::

    # QGIS 自带 python（3.12），Windows 需要 PYTHONHOME 指向 apps/Python312
    set PYTHONHOME=D:/zsh/app/qgis4.2/apps/Python312
    D:/zsh/app/qgis4.2/bin/python.exe -u scripts/run_sqlite_test.py

    # 只跑某几项 / 跑完清掉产物
    python scripts/run_sqlite_test.py --only gpkg spatialite
    python scripts/run_sqlite_test.py --clean

产物默认留在 ``.tmp/sqlite_test/``（已 gitignore），可以直接拖进 QGIS 看。
退出码 0 = 全部通过，1 = 有用例失败。
"""

from __future__ import annotations

import argparse
import gc
import os
import shutil
import sqlite3
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

import _bootstrap_path  # noqa: F401  把仓库根加进 sys.path

# 输出全是中文：Windows 下 stdout 默认走 cp936，重定向到管道/文件会乱码。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parents[1]

#: QgsApplication 必须全程持有引用——一旦被 GC，底层 C++ 对象析构，
#: 之后任何 QGIS 调用都是静默原生崩溃（bash 里表现为 exit 127，无 traceback）。
_QGS_APP = None

#: 测试图层用的坐标系（与 config/app.yaml 的 app.crs 一致）
CRS = "EPSG:4490"

#: 造数据用的点（经纬度）与属性
POINTS: list[tuple[str, float, float, int]] = [
    ("北京", 116.407, 39.904, 1),
    ("上海", 121.474, 31.230, 2),
    ("广州", 113.264, 23.129, 3),
]


# --------------------------------------------------------------- QGIS 装配


def init_qgis() -> object:
    """按 ``config/app.yaml`` 装配 QGIS 运行时并 ``initQgis``，返回 QgsApplication。

    必须在任何 ``import qgis`` 之前调用 :meth:`QgisRuntime.apply_env`，
    所以 qgis 的 import 都放在函数内部。
    """
    global _QGS_APP

    from app.bootstrap import _build_qgis_runtime
    from app.env.config import AppConfig

    cfg = AppConfig.from_file(_ROOT / "config" / "app.yaml")
    runtime = _build_qgis_runtime(cfg)
    runtime.apply_env()
    # 无界面运行：不需要真实窗口系统
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from qgis.core import QgsApplication

    # Windows/PyQt6：argv 必须是 bytes，传 str 会 TypeError
    argv = [bytes(a, sys.getfilesystemencoding()) for a in sys.argv]
    _QGS_APP = QgsApplication(argv, False)
    QgsApplication.setPrefixPath(str(runtime.qgis_prefix), True)
    QgsApplication.initQgis()
    print(f"[env] QGIS prefix = {runtime.qgis_prefix}")
    print(f"[env] sqlite3 lib = {sqlite3.sqlite_version}")
    return _QGS_APP


def make_layer(name: str = "cities") -> object:
    """造一个内存点图层，带 ``name`` / ``code`` 两个字段和 3 个要素。"""
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer

    layer = QgsVectorLayer(
        f"Point?crs={CRS}&field=name:string(64)&field=code:integer", name, "memory"
    )
    if not layer.isValid():
        raise RuntimeError("内存图层构造失败")

    feats = []
    for city, x, y, code in POINTS:
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        f.setAttributes([city, code])
        feats.append(f)
    ok, _ = layer.dataProvider().addFeatures(feats)
    if not ok or layer.featureCount() != len(POINTS):
        raise RuntimeError(f"内存图层写入失败，要素数 {layer.featureCount()}")
    return layer


def save_layer(layer: object, path: Path, driver: str, layer_name: str,
               append: bool = False) -> None:
    """把图层写进 SQLite 家族文件（GPKG / SpatiaLite）。

    ``append=True`` 时往已存在的库里追加一个新图层（不覆盖原有内容）。
    """
    from qgis.core import QgsCoordinateTransformContext, QgsVectorFileWriter

    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = driver
    opts.layerName = layer_name
    if append:
        # 往已有库里加层；不加这句会整库覆盖
        opts.actionOnExistingFile = (
            QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
        )
    err, msg, _newfile, _newlayer = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, str(path), QgsCoordinateTransformContext(), opts
    )
    if err != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError(f"{driver} 写入失败 err={err} msg={msg}")


def check_layer(layer: object, source: str) -> None:
    """校验打开的图层：有效性 / 要素数 / 字段 / CRS / 属性 / 几何。"""
    from qgis.core import QgsVectorLayer

    assert isinstance(layer, QgsVectorLayer), f"{source} 不是矢量图层"
    if not layer.isValid():
        raise RuntimeError(f"{source} 打开失败：{layer.dataProvider().error().message()}")
    if layer.featureCount() != len(POINTS):
        raise RuntimeError(f"{source} 要素数 {layer.featureCount()} != {len(POINTS)}")

    names = [fld.name() for fld in layer.fields()]
    for expect in ("name", "code"):
        if expect not in names:
            raise RuntimeError(f"{source} 缺字段 {expect}，实际 {names}")
    if layer.crs().authid() != CRS:
        raise RuntimeError(f"{source} CRS 是 {layer.crs().authid()}，期望 {CRS}")

    got = {f["name"]: (f["code"], f.geometry().asPoint()) for f in layer.getFeatures()}
    for city, x, y, code in POINTS:
        if city not in got:
            raise RuntimeError(f"{source} 丢了要素 {city}")
        gcode, pt = got[city]
        if int(gcode) != code:
            raise RuntimeError(f"{source} {city} 的 code={gcode}，期望 {code}")
        # 浮点回读有精度损耗，1e-6 度 ≈ 0.1 米，足够判断"是不是同一个点"
        if abs(pt.x() - x) > 1e-6 or abs(pt.y() - y) > 1e-6:
            raise RuntimeError(f"{source} {city} 几何漂移：{pt.x()},{pt.y()} != {x},{y}")


# ------------------------------------------------------------------- 用例


def case_plain(out: Path) -> str:
    """纯 sqlite3：建库 → 写 → 读 → 改 → 删 → 重开，验证落盘持久化。"""
    db = out / "plain.sqlite"
    db.unlink(missing_ok=True)

    con = sqlite3.connect(db)
    con.execute("CREATE TABLE city (id INTEGER PRIMARY KEY, name TEXT, code INTEGER)")
    con.executemany(
        "INSERT INTO city (name, code) VALUES (?, ?)",
        [(c, code) for c, _x, _y, code in POINTS],
    )
    con.commit()

    n = con.execute("SELECT count(*) FROM city").fetchone()[0]
    if n != len(POINTS):
        raise RuntimeError(f"插入后行数 {n} != {len(POINTS)}")

    con.execute("UPDATE city SET code = 99 WHERE name = ?", ("北京",))
    con.execute("DELETE FROM city WHERE name = ?", ("广州",))
    con.commit()
    con.close()

    # 重新打开：验证数据真的写进了文件，不是只在内存
    con = sqlite3.connect(db)
    rows = dict(con.execute("SELECT name, code FROM city ORDER BY name").fetchall())
    con.close()
    if rows != {"北京": 99, "上海": 2}:
        raise RuntimeError(f"重开后数据不符：{rows}")

    return f"{db.name} 建表/增删改查/持久化 OK（{db.stat().st_size} 字节）"


def case_gpkg(out: Path) -> str:
    """QGIS 图层 → GeoPackage 保存 → ogr provider 打开回读。"""
    from qgis.core import QgsVectorLayer

    gpkg = out / "roundtrip.gpkg"
    gpkg.unlink(missing_ok=True)

    save_layer(make_layer(), gpkg, "GPKG", "cities")
    if not gpkg.is_file():
        raise RuntimeError("GPKG 文件没生成")

    # GeoPackage 本质是 SQLite：能被 sqlite3 直接打开，且有 gpkg_contents 表
    con = sqlite3.connect(gpkg)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    if "gpkg_contents" not in tables:
        raise RuntimeError(f"不是合法 GeoPackage，表清单：{sorted(tables)}")

    check_layer(QgsVectorLayer(f"{gpkg}|layername=cities", "读回", "ogr"), "GPKG")
    return f"{gpkg.name} 写入 + 打开回读 OK（{gpkg.stat().st_size} 字节）"


def case_gpkg_multi(out: Path) -> str:
    """同一个 .gpkg 里放两个图层，各自独立可开（验证追加不覆盖）。"""
    from qgis.core import QgsVectorLayer

    gpkg = out / "multi.gpkg"
    gpkg.unlink(missing_ok=True)

    save_layer(make_layer("first"), gpkg, "GPKG", "first")
    save_layer(make_layer("second"), gpkg, "GPKG", "second", append=True)

    for name in ("first", "second"):
        check_layer(
            QgsVectorLayer(f"{gpkg}|layername={name}", name, "ogr"), f"GPKG:{name}"
        )
    return f"{gpkg.name} 双图层写入 + 分别打开 OK"


def case_spatialite(out: Path) -> str:
    """QGIS 图层 → SpatiaLite 保存 → spatialite provider 打开。"""
    from qgis.core import QgsDataSourceUri, QgsProviderRegistry, QgsVectorLayer

    if "spatialite" not in QgsProviderRegistry.instance().providerList():
        raise RuntimeError("当前 QGIS 没有 spatialite provider")

    db = out / "roundtrip.sqlite"
    db.unlink(missing_ok=True)

    save_layer(make_layer(), db, "SpatiaLite", "cities")
    if not db.is_file():
        raise RuntimeError("SpatiaLite 文件没生成")

    uri = QgsDataSourceUri()
    uri.setDatabase(str(db))
    uri.setDataSource("", "cities", "GEOMETRY")
    check_layer(QgsVectorLayer(uri.uri(), "读回", "spatialite"), "SpatiaLite")
    return f"{db.name} 写入 + spatialite provider 打开 OK（{db.stat().st_size} 字节）"


def case_extension(out: Path) -> str:
    """加载 mod_spatialite 扩展，跑一条真·空间 SQL。"""
    db = out / "roundtrip.sqlite"
    if not db.is_file():
        # 单独跑 --only extension 时先补一个库
        save_layer(make_layer(), db, "SpatiaLite", "cities")

    con = sqlite3.connect(db)
    if not hasattr(con, "enable_load_extension"):
        con.close()
        raise RuntimeError("此 python 的 sqlite3 编译时禁用了扩展加载")
    con.enable_load_extension(True)
    con.load_extension("mod_spatialite")
    ver = con.execute("SELECT spatialite_version()").fetchone()[0]

    # 空间 SQL：数一数落在这个框里的城市（北京 + 上海，广州纬度太低）
    box = "BuildMbr(110, 30, 125, 45, 4490)"
    n = con.execute(
        f"SELECT count(*) FROM cities WHERE ST_Intersects(GEOMETRY, {box})"
    ).fetchone()[0]
    con.close()
    if n != 2:
        raise RuntimeError(f"ST_Intersects 命中 {n} 个，期望 2")

    return f"mod_spatialite {ver} 加载 + ST_Intersects 空间查询 OK"


CASES: list[tuple[str, Callable[[Path], str]]] = [
    ("plain", case_plain),
    ("gpkg", case_gpkg),
    ("gpkg-multi", case_gpkg_multi),
    ("spatialite", case_spatialite),
    ("extension", case_extension),
]


# ------------------------------------------------------------------- CLI


def cleanup(out: Path) -> None:
    """删除产物目录，删不掉就如实报告（不假装删干净了）。

    打开过的 GPKG 由 QGIS 的 ogr provider 连接池持有文件句柄，Windows 上被占用
    的文件删不掉（纯 sqlite / SpatiaLite 的能删掉）。先 ``gc.collect()`` 让没
    引用的 ``QgsVectorLayer`` 析构、放开句柄，再删；仍有残留就打印出来。
    残留无害：产物目录在 gitignore 的 ``.tmp/`` 下，下次跑会覆盖。
    """
    gc.collect()
    shutil.rmtree(out, ignore_errors=True)
    if not out.exists():
        print(f"已清理产物目录 {out}")
        return
    leftover = sorted(p.name for p in out.iterdir())
    print(f"[warn] 产物目录未能完全清理（文件被 ogr provider 句柄占用）：{out}")
    if leftover:
        print(f"[warn] 残留 {len(leftover)} 个文件：{', '.join(leftover)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite 保存 / 打开自检")
    parser.add_argument(
        "--out",
        type=Path,
        default=_ROOT / ".tmp" / "sqlite_test",
        help="产物输出目录（默认 .tmp/sqlite_test）",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        choices=[name for name, _fn in CASES],
        help="只跑指定用例（默认全跑）",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="跑完删除产物（默认保留在 .tmp/ 里，可直接用 QGIS 打开查看）",
    )
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
        except Exception as exc:  # noqa: BLE001  自检脚本：任何失败都要归档成结果
            traceback.print_exc()
            results.append((name, False, f"{type(exc).__name__}: {exc}"))

    print("\n" + "=" * 60)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<12} {detail}")
    failed = [n for n, ok, _d in results if not ok]
    print("=" * 60)
    print(f"合计 {len(results)} 项，通过 {len(results) - len(failed)} 项"
          + (f"，失败：{', '.join(failed)}" if failed else ""))

    if args.clean:
        cleanup(out)
    else:
        print(f"产物保留在 {out}")

    # QGIS 卸载阶段在本机会原生崩溃（见 memory / 项目笔记），用 _exit 跳过 teardown；
    # _exit 不会 flush 缓冲，管道重定向时必须先手动 flush，否则输出全丢。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    raise SystemExit(main())
