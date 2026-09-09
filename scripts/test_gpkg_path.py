"""纯 sqlite 回归: local_db_util 的 GeoPackage 路径(本机无 mod_spatialite)。

用法: python scripts/test_gpkg_path.py [db_path]
环境无 pysqlite3 时通过 sys.modules 别名绕到 stdlib sqlite3;
无 mod_spatialite DLL → gpkg 路径跳过 load_extension,可完整覆盖。
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.modules.setdefault("pysqlite3", sqlite3)  # 本机无 pysqlite3 → 别名 stdlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.util import gpkg_geom  # noqa: E402
from app.core.util import local_db_util  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS" if cond else "FAIL"), name, extra)


def read_all(con, sql, args=()):
    return con.execute(sql, args).fetchall()


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(tempfile.gettempdir()) / "gpkg_test_01.db")
    if db_path.exists():
        db_path.unlink()

    db = local_db_util.local_db_util(db_path=db_path)
    check("new db kind=gpkg", db.kind == "gpkg", db.kind)
    check("new db has_spatialite False(未 load 扩展)", db.has_spatialite is False)

    tables = {r[0] for r in db.cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    need = {"gpkg_spatial_ref_sys", "gpkg_contents", "gpkg_geometry_columns",
            "gpkg_tile_matrix_set", "gpkg_tile_matrix", "user_relation", "metadata"}
    check("gpkg 系统表+user_relation+metadata 就位", need <= tables,
          str(sorted(tables)))

    srs = {r[0] for r in db.cursor.execute(
        "SELECT srs_id FROM gpkg_spatial_ref_sys")}
    check("SRS 种子含 0/-1/4326/3857/4490", {0, -1, 4326, 3857, 4490} <= srs, srs)

    # ---------- 矢量: 建表 + 插入 + 读回 ----------
    meta = {
        "name": "测试县界面",
        "geometryType": "esriGeometryPolygon",
        "fields": [
            {"name": "OBJECTID", "type": "esriFieldTypeOID"},
            {"name": "name", "type": "esriFieldTypeString"},
            {"name": "code", "type": "esriFieldTypeInteger"},
        ],
        "extent": {"xmin": 114.0, "ymin": 27.0, "xmax": 116.0, "ymax": 29.0,
                   "spatialReference": {"latestWkid": 4490}},
    }
    r = db.create_arcgis_layer(layer_meta=meta, register=True)
    check("矢量建表", r == "测试县界面", r)
    # OID 字段应转 INTEGER PRIMARY KEY,geom 列 BLOB
    info = {row[1]: row[2] for row in read_all(db.cursor, "PRAGMA table_info(测试县界面)")}
    check("几何列 BLOB 并入 CREATE", info.get("geom") == "BLOB", info)
    gcc = [tuple(x) for x in read_all(
        db.cursor, "SELECT geometry_type_name, srs_id FROM gpkg_geometry_columns")]
    check("geometry_columns 登记", gcc == [("POLYGON", 4490)], gcc)
    cc = [tuple(x) for x in read_all(
        db.cursor, "SELECT data_type, srs_id FROM gpkg_contents "
                   "WHERE table_name='测试县界面'")]
    check("contents 登记 features", cc == [("features", 4490)], cc)

    rows = [
        {"attributes": {"OBJECTID": 1, "name": "甲县", "code": 11},
         "geometry": {"rings": [[[114.5, 27.5], [115.5, 27.5], [115.5, 28.5],
                                 [114.5, 28.5], [114.5, 27.5]]]}},
        {"attributes": {"OBJECTID": 2, "name": "乙县", "code": 12},
         "geometry": "POLYGON((115.0 28.0,115.6 28.0,115.6 28.6,115.0 28.6,115.0 28.0))"},
        {"attributes": {"OBJECTID": 3, "name": "丙县", "code": 13},
         "geometry": "POLYGON((114.0 28.0,114.4 28.0,114.4 28.4,114.0 28.4,114.0 28.0))"},
    ]
    n = db.batch_add_vector_data(table_name="测试县界面", data=rows)
    check("矢量插入 3 行", n == 3, n)

    # 读回验证 blob 头 & srs
    out = read_all(db.cursor, 'SELECT name, geom FROM "测试县界面" ORDER BY OBJECTID')
    kinds = []
    for name, blob in out:
        b = bytes(blob)
        kinds.append((name, b[:2].decode(), int.from_bytes(b[4:8], "little")))
    check("三个 blob 都是 GP 头", all(k[1] == "GP" for k in kinds), kinds)
    check("blob srs_id=4490", {k[2] for k in kinds} == {4490}, kinds)

    # 多部件列: 单部件自动包 MULTIPOLYGON
    r2v = db.create_arcgis_layer(layer_meta=meta, table_name="县界多部件",
                                 geometry_type="MULTIPOLYGON", register=True)
    db.batch_add_vector_data(table_name=r2v, data=[
        {"attributes": {"OBJECTID": 1}, "geometry": {"rings": [
            [[114.5, 27.5], [115.5, 27.5], [115.5, 28.5], [114.5, 28.5], [114.5, 27.5]]]}},
        {"attributes": {"OBJECTID": 2}, "geometry": "MULTIPOLYGON((("
         "115.0 28.0,115.6 28.0,115.6 28.6,115.0 28.6,115.0 28.0)))"},
    ])
    kinds2 = [tuple(x) for x in read_all(db.cursor, "SELECT OBJECTID, length(geom) "
        "FROM 县界多部件 ORDER BY OBJECTID")]
    check("多部件列 2 行", len(kinds2) == 2)
    # 类型不一致应报错(单面列进不了多面)
    try:
        db.batch_add_vector_data(table_name="测试县界面", data=[
            {"attributes": {"OBJECTID": 9, "name": "bad"},
             "geometry": "MULTIPOLYGON(((0 0,1 0,1 1,0 0)))"}])
        check("单面列拒绝多面", False, "未抛错")
    except ValueError as e:
        check("单面列拒绝多面", "多部件" in str(e) or "部件" in str(e), e)

    # ---------- 影像: 建存储 + 金字塔 + 瓦片翻转 ----------
    img_meta = {
        "name": "_3605新余市2024年第一季度2米影像_tif",
        "type": "Raster Layer",
        "extent": {"xmin": 114.471350, "ymin": 27.530586, "xmax": 115.408385,
                   "ymax": 28.121334,
                   "spatialReference": {"latestWkid": 4490}},
        "url": "http://127.0.0.1:6080/arcgis/rest/services/xinyu/MapServer/0",
    }
    r2 = db.create_arcgis_image_store(
        layer_meta=img_meta, min_zoom=0, max_zoom=4, tile_format="png",
        register=True, attr={"wkid": 4490, "src": "test"})
    check("影像建表", r2.get("table") == "_3605新余市2024年第一季度2米影像_tif", r2)
    check("影像 pyramid 登记 True", r2.get("pyramid") is True, r2)

    tm = [tuple(x) for x in read_all(
        db.cursor, "SELECT table_name, data_type, min_x, min_y, max_x, max_y, srs_id "
        "FROM gpkg_contents WHERE data_type='tiles'")]
    check("tiles contents 度网格范围",
          len(tm) == 1 and tm[0][2:] == (-180.0, -90.0, 180.0, 90.0, 4490), tm)
    tms_rows = [tuple(x) for x in read_all(
        db.cursor, "SELECT table_name, min_x, min_y, max_x, max_y, srs_id "
        "FROM gpkg_tile_matrix_set")]
    check("tile_matrix_set 与 contents 一致",
          len(tms_rows) == 1 and tms_rows[0][1:] == tm[0][2:], tms_rows)
    levels = [tuple(x) for x in read_all(
        db.cursor, "SELECT zoom_level, matrix_width, matrix_height, "
        "tile_width, tile_height FROM gpkg_tile_matrix "
        "ORDER BY zoom_level")]
    expect_lv = [(z, 1 << z, max(1, (1 << z) >> 1), 256, 256) for z in range(5)]
    check("tile_matrix 每级矩阵 2^z×(2^(z-1))", levels == expect_lv, levels)

    # 瓦片插入: 入参 TMS(z=4, x=13, tms 行 54263 → 实际行 11367)
    z, x, tms_row = 4, 13, 11  # n=16, xyz = 15-11 = 4
    db.batch_add_image_tile_data(
        table_name=r2["table"],
        data=[{"z": z, "x": x, "y": tms_row, "data": b"\x89PNG\x0d\x0aTEST"},
              {"z": 4, "x": 14, "tile_row": tms_row, "tile_data": b"\x89PNG..."}],
    )
    stored = [tuple(x) for x in read_all(
        db.cursor, f"SELECT zoom_level, tile_column, tile_row, "
        f"tile_data FROM {local_db_util._quote_ident(r2['table'])}")]
    check("gpkg 落库行号=XYZ(翻转后 4 / 4)",
          {(s[0], s[1], s[2]) for s in stored} == {(4, 13, 4), (4, 14, 4)}, stored)
    check("tile_data 原样 bytes", all(bytes(s[3])[:4] == b"\x89PNG" for s in stored))

    # 重复键(已存在)overwrite=False 应报错
    try:
        db.batch_add_image_tile_data(table_name=r2["table"],
                                     data=[{"z": 4, "x": 13, "y": tms_row, "data": b"x"}])
        check("重复瓦片拒绝", False, "未抛错")
    except ValueError:
        check("重复瓦片拒绝", True)

    # ---------- 目录/描述 ----------
    cats = db.query_all_table_name()
    check("query_all_table_name 两类齐全",
          "测试县界面" in cats["spatial"] and r2["table"] in cats["image"], cats)
    db.conn.close()

    # 重新打开(内容探测) + describe_local_db
    db2 = local_db_util.local_db_util(db_path=db_path)
    check("重开内容探测 kind=gpkg", db2.kind == "gpkg", db2.kind)
    check("重开不加载 spatialite", db2.has_spatialite is False)
    desc = local_db_util.describe_local_db(db_path)
    check("describe categories 有 spatial+image",
          "测试县界面" in desc["categories"]["spatial"]
          and "县界多部件" in desc["categories"]["spatial"]
          and desc["categories"]["image"] == [r2["table"]], desc["categories"])
    check("describe counts", desc["counts"]["测试县界面"] == 3
          and desc["counts"][r2["table"]] == 2, desc["counts"])
    m = desc["meta"]["测试县界面"]
    check("meta 几何标签 面 / srid / crs",
          m["geom_label"] == "面" and m["srid"] == 4490
          and m["crs"] and "4490" in m["crs"], m)
    check("meta fields 含 geom 与属性列",
          {f[0] for f in m["fields"]} >= {"geom", "OBJECTID", "name", "code"},
          m["fields"])
    # 影像表 meta 无几何列信息
    im = desc["meta"][r2["table"]]
    check("影像表 meta 无几何列信息", im.get("geom_label") is None, im)
    check("影像表 meta 无几何列名", im.get("geom_col") is None)

    # ---------- e2e: 3857 墨卡托影像金字塔登记(GDAL/GPKG 直读的依据) ----------
    rcon = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # 墨卡托(3857) gpkg 影像: 建表只登记, 再补两行验证金字塔矩阵
        im2 = dict(img_meta)
        im2["name"] = "webmerc_影像"
        im2["extent"] = {"xmin": 100, "ymin": 0, "xmax": 140, "ymax": 60,
                          "spatialReference": {"latestWkid": 3857}}
        r3 = db2.create_arcgis_image_store(layer_meta=im2, min_zoom=1, max_zoom=1,
                                           register=True)
        db2.batch_add_image_tile_data(table_name=r3["table"], data=[
            {"z": 1, "x": 0, "y": 0, "data": b"PNG-A"},
            {"z": 1, "x": 1, "y": 1, "data": b"PNG-B"}])
        mtm = [tuple(x) for x in rcon.execute(
            "SELECT zoom_level, matrix_width, matrix_height, pixel_x_size, "
            "pixel_y_size FROM gpkg_tile_matrix WHERE table_name=? ",
            (r3["table"],))]
        w = 2.0 * 3.141592653589793 * 6378137.0
        check("墨卡托金字塔矩阵 2^1×2^1 & 像素步长",
              len(mtm) == 1 and mtm[0][1:3] == (2, 2)
              and abs(mtm[0][3] - (2 * w / (2 * 256))) < 1e-6, mtm)
    finally:
        rcon.close()

    # ---------- e2e: create_vector_table(PG 迁移等显式列建表入口) ----------
    vt = db2.create_vector_table(
        "迁移面表",
        columns=[("fid", "INTEGER"), ("name", "TEXT"), ("code", "REAL")],
        geometry_column="shape", geometry_type="MULTIPOLYGON", srid=4490,
    )
    check("create_vector_table 返回表名", vt == "迁移面表", vt)
    n = db2.batch_add_vector_data(vt, [
        {"fid": 1, "name": "甲", "code": 1.5,
         "shape": "POLYGON ((0 0, 1 0, 1 1, 0 0))"},
        {"fid": 2, "name": "乙", "code": 2.5,
         "shape": "MULTIPOLYGON (((2 2, 3 2, 3 3, 2 2)))"},
    ])
    check("单部件入 MULTI 列自动包(2 行)", n == 2, n)
    blobs = [r[0] for r in db2.cursor.execute(
        f"SELECT shape FROM {local_db_util._quote_ident(vt)} ORDER BY fid")]
    check("两行均变 MULTIPOLYGON & srs=4490",
          [gpkg_geom.blob_kind_name(b) for b in blobs] == ["MULTIPOLYGON", "MULTIPOLYGON"]
          and all(gpkg_geom.blob_srs_id(b) == 4490 for b in blobs))
    gcol = db2.cursor.execute(
        "SELECT geometry_type_name, srs_id FROM gpkg_geometry_columns "
        "WHERE table_name = '迁移面表'").fetchone()
    check("gpkg_geometry_columns 登记 MULTIPOLYGON/4490",
          tuple(gcol) == ("MULTIPOLYGON", 4490), tuple(gcol))
    db2.create_vector_table(
        "迁移面表", columns=[("a", "INTEGER")],
        geometry_column="shape", geometry_type="POLYGON", srid=4490,
        overwrite=True,
    )
    gcol2 = db2.cursor.execute(
        "SELECT geometry_type_name FROM gpkg_geometry_columns "
        "WHERE table_name = '迁移面表'").fetchone()
    check("同表 overwrite 重建(登记改 POLYGON)",
          gcol2 and gcol2[0] == "POLYGON", gcol2)
    # 纯属性表(gpkg 里为普通表,不入 gpkg_contents)
    dt = db2.create_vector_table("纯属性表", columns=[("k", "TEXT"), ("v", "INTEGER")])
    db2.cursor.execute(
        f'INSERT INTO {local_db_util._quote_ident(dt)} (k, v) VALUES ("x", 1)')
    db2.conn.commit()
    gc = db2.cursor.execute(
        "SELECT count(*) FROM gpkg_contents WHERE table_name='纯属性表'").fetchone()[0]
    check("纯属性表不入 gpkg_contents", gc == 0, gc)
    db2.add_domain_table("纯属性表", "dict")
    cats = db2.query_all_table_name()
    check("纯属性表登记 dict 可列出", "纯属性表" in cats["dict"], cats["dict"])

    db2.conn.close()

    # ---- gpkg_open_path: .db 内容 gpkg → 同 inode 的 .gpkg 硬链接别名 ----
    alias = local_db_util.gpkg_open_path(db_path)
    ap = Path(alias)
    check("gpkg_open_path 返回 .gpkg 路径", ap.suffix.lower() == ".gpkg", alias)
    check("别名硬链接存在", ap.is_file())
    check("别名与原库同 inode(硬链接, 零拷贝)",
          ap.stat().st_ino == db_path.stat().st_ino)
    con_alias = sqlite3.connect(alias)
    try:
        gc = con_alias.execute(
            "SELECT count(*) FROM gpkg_contents"
        ).fetchone()[0]
    finally:
        con_alias.close()
    check("别名可见 gpkg 内容表(指向同一库)", gc >= 2, gc)
    check("幂等: 再次调用返回同一路径",
          local_db_util.gpkg_open_path(db_path) == alias)
    # Windows: 别名应带 Hidden 属性(资源管理器默认视图不可见)
    if sys.platform == "win32":
        check("别名带 Hidden 属性(Windows)",
              local_db_util._is_hidden(ap), "visible-file attr")
    # 陈旧别名重建: 换成异 inode 的普通副本后, gpkg_open_path 应重建为硬链接
    wrong = Path(str(ap) + ".tmp")
    wrong.write_bytes(b"not the same inode" + b"x" * 64)
    os.replace(wrong, ap)  # 顶掉原硬链接(变成普通文件)
    alias2 = local_db_util.gpkg_open_path(db_path)
    check("陈旧别名被重建为硬链接(同 inode)",
          Path(alias2).stat().st_ino == db_path.stat().st_ino, alias2)
    # 非 gpkg 内容 → 原样返回, 不建别名
    plain_txt = Path(tempfile.gettempdir()) / "gpkg_test_plain.txt"
    plain_txt.write_text("hello", encoding="utf-8")
    check("非 gpkg 内容原样返回",
          local_db_util.gpkg_open_path(plain_txt) == str(plain_txt)
          and not Path(str(plain_txt) + ".gpkg").exists())
    plain_txt.unlink(missing_ok=True)
    Path(str(plain_txt) + ".gpkg").unlink(missing_ok=True)

    # ---- sweep_orphan_gpkg_aliases: 只删“孤儿”别名, 不误删用户真 .gpkg ----
    if sys.platform == "win32":
        odir = Path(tempfile.gettempdir()) / "gpkg_sweep_test"
        odir.mkdir(exist_ok=True)
        try:
            odb = odir / "orphan.db"; oalias = odir / "orphan.gpkg"
            odb.write_bytes(b"sqlite-not-really")
            # 手工伪造一个“我们建的别名”: 同 stem 的 .db + Hidden 属性
            os.link(odb, oalias)
            local_db_util._set_hidden(oalias)
            # 真用户 .gpkg(无 Hidden)必须幸存
            real = odir / "userlib.gpkg"
            real.write_bytes(b"genuine gpkg data")
            odb.unlink()  # 库没了 → 别名成孤儿
            n = local_db_util.sweep_orphan_gpkg_aliases(odir)
            check("孤儿清扫只删孤儿别名", n == 1 and not oalias.exists(), n)
            check("用户真 .gpkg 不被误删", real.exists())
            real.unlink(missing_ok=True)
        finally:
            for f in odir.iterdir():
                f.unlink(missing_ok=True)
            odir.rmdir()

    # ---- release_gpkg_alias: 不再被引用的别名 → 删除; 非本模块别名 → 不动 ----
    ok = local_db_util.release_gpkg_alias(alias)
    check("release: 无活引用别名被删(尽力而为)", ok and not ap.exists(), ok)
    # 重建一个, 供下面“拒删”对照(顺带验证重建幂等)
    alias3 = local_db_util.gpkg_open_path(db_path)
    check("release 后重新加载自动重建别名", Path(alias3).exists())
    udir = Path(tempfile.gettempdir()) / "gpkg_release_test"
    udir.mkdir(exist_ok=True)
    try:
        user_db = udir / "mine.db"
        user_gpkg = udir / "mine.gpkg"
        user_db.write_bytes(b"not sqlite")  # 同 stem .db 存在但不是 gpkg 内容
        user_gpkg.write_bytes(b"user data")  # 无隐藏属性、异 inode
        check("release 拒删: 同 stem .db 非 gpkg 内容",
              local_db_util.release_gpkg_alias(user_gpkg) is False
              and user_gpkg.exists())
        solo = udir / "solo.gpkg"
        solo.write_bytes(b"genuine gpkg")
        check("release 拒删: 无同 stem .db 的真 .gpkg",
              local_db_util.release_gpkg_alias(solo) is False and solo.exists())
    finally:
        for f in udir.iterdir():
            f.unlink(missing_ok=True)
        udir.rmdir()

    # ---- 新规: 本体即 .gpkg 的库 → gpkg_open_path 原样直开, 不建任何文件 ----
    canon = Path(tempfile.gettempdir()) / "gpkg_canonical_01.gpkg"
    if canon.exists():
        canon.unlink()
    cdb = local_db_util.local_db_util(db_path=canon)
    check("新规 .gpkg 库 kind=gpkg", cdb.kind == "gpkg")
    before = {f.name for f in canon.parent.iterdir()}
    ret = local_db_util.gpkg_open_path(canon)
    after = {f.name for f in canon.parent.iterdir()}
    check(".gpkg 本体 gpkg_open_path 原样返回",
          os.path.normcase(ret) == os.path.normcase(str(canon)), ret)
    check(".gpkg 本体不产生任何第二文件(目录无新增)", after <= before, after - before)
    check(".gpkg 本体 nlink=1(无硬链接)", canon.stat().st_nlink == 1,
          canon.stat().st_nlink)
    try:
        from osgeo import ogr

        ds = ogr.Open(str(canon), 0)
        check(".gpkg 本体 OGR 直开驱动=GPKG",
              bool(ds) and ds.GetDriver().GetName() == "GPKG",
              ds.GetDriver().GetName() if ds else "None")
        del ds
    except ImportError:
        pass  # 纯 sqlite 环境无 osgeo → 跳过 OGR 探测
    import gc

    gc.collect()  # 确保 OGR/sqlite 句柄释放后再删
    for _ in range(5):
        try:
            canon.unlink(missing_ok=True)
            break
        except OSError:
            gc.collect()
            import time

            time.sleep(0.3)

    # 清理
    try:
        os.unlink(db_path)
        os.unlink(alias)
        os.unlink(alias3)
        print("cleaned", db_path)
    except OSError:
        pass

    print(f"\n== {len(PASS)} pass, {len(FAIL)} fail ==")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
