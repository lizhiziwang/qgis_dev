"""
将 maxent 数据目录中的空间数据导入 PostgreSQL/PostGIS 数据库。

导入内容:
  - Shapefile (*.shp) -> PostGIS 矢量表 (使用 GDAL Python API)
  - GeoTIFF (*.tif)   -> PostGIS 栅格表 (使用 GDAL + ST_FromGDALRaster)
  - ASCII 栅格 (*.asc) -> PostGIS 栅格表 (同上)

表命名规则:
  - 保护区数据: {nr_code}_{layer_name} (e.g. nr36051_region, nr36111_dem)
  - 模板数据: template_{layer_name} (e.g. template_buffer)

使用方式:
  python import_maxent_to_pg.py
"""

import os

import psycopg2
from osgeo import gdal, ogr, osr

# ============================================================
# 数据库配置 (按需修改)
# ============================================================
DB_CONFIG = {
    "host": "localhost",
    "port": 54344,
    "dbname": "gisdata",
    "user": "sde",
    "password": "sde@2025",
}

# 源数据根目录
MAXENT_ROOT = r"D:\zsh\code\hou\base_gis\starter\src\main\resources\data_file\maxent"

# 目标 schema (将在此 schema 下创建所有表)
TARGET_SCHEMA = "maxent"

# 导入前是否清空该模式下的所有表 (True=清空重新导入, False=追加)
CLEAR_SCHEMA_BEFORE_IMPORT = True

# ============================================================
# 工具函数
# ============================================================

def get_pg_conn():
    """创建 psycopg2 连接"""
    return psycopg2.connect(**DB_CONFIG)


def ensure_schema(conn):
    """确保目标 schema 存在, 并根据 CLEAR_SCHEMA_BEFORE_IMPORT 决定是否清空"""
    cur = conn.cursor()
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {TARGET_SCHEMA}")

    if CLEAR_SCHEMA_BEFORE_IMPORT:
        cur.execute(f"""
            SELECT tablename FROM pg_tables
            WHERE schemaname = '{TARGET_SCHEMA}'
        """)
        tables = [row[0] for row in cur.fetchall()]
        for t in tables:
            cur.execute(f'DROP TABLE IF EXISTS {TARGET_SCHEMA}."{t}" CASCADE')
        if tables:
            print(f"[Schema] 已清空 {TARGET_SCHEMA} 下的 {len(tables)} 个表")
        else:
            print(f"[Schema] {TARGET_SCHEMA} 下无表需要清空")

    conn.commit()
    print(f"[Schema] 已确保 schema '{TARGET_SCHEMA}' 存在")


def sanitize_table_name(name):
    """清理表名: 转小写, 替换特殊字符"""
    name = name.lower().replace(" ", "_").replace("-", "_")
    return "".join(c for c in name if c.isalnum() or c == "_")


def log_import(conn, table_name, source_path, layer_type, nr_code, status):
    """记录导入日志"""
    cur = conn.cursor()
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {TARGET_SCHEMA}.import_log (
            id SERIAL PRIMARY KEY,
            table_name TEXT,
            source_path TEXT,
            layer_type TEXT,
            nr_code TEXT,
            status TEXT,
            imported_at TIMESTAMP DEFAULT now()
        )
    """)
    cur.execute(
        f"INSERT INTO {TARGET_SCHEMA}.import_log "
        f"(table_name, source_path, layer_type, nr_code, status) "
        f"VALUES (%s, %s, %s, %s, %s)",
        (table_name, source_path, layer_type, nr_code, status),
    )
    conn.commit()


def get_srid_from_ds(ds):
    """从 GDAL 数据集读取 SRID"""
    wkt = ds.GetProjection()
    if not wkt:
        return 0
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    auth = srs.GetAttrValue("AUTHORITY", 1)
    return int(auth) if auth else 0


# ============================================================
# 导入 Shapefile (使用 GDAL/OGR Python API)
# ============================================================

def import_shapefile(shp_path, nr_code, table_suffix):
    """使用 GDAL OGR 将 shapefile 导入 PostGIS"""
    table_name = sanitize_table_name(f"{nr_code}_{table_suffix}")
    print(f"  [矢量] 导入: {shp_path} -> {TARGET_SCHEMA}.{table_name}")

    conn = get_pg_conn()
    cur = conn.cursor()

    try:
        src_ds = ogr.Open(shp_path)
        if src_ds is None:
            return table_name, "failed: cannot open shapefile"
        src_layer = src_ds.GetLayer(0)
        src_srs = src_layer.GetSpatialRef()
        srid = 0
        if src_srs:
            auth = src_srs.GetAttrValue("AUTHORITY", 1)
            srid = int(auth) if auth else 0

        layer_defn = src_layer.GetLayerDefn()
        field_defs = []
        for i in range(layer_defn.GetFieldCount()):
            fd = layer_defn.GetFieldDefn(i)
            field_defs.append((fd.GetName(), fd.GetType()))

        pg_geom_type = "GEOMETRY"

        drop_sql = f'DROP TABLE IF EXISTS {TARGET_SCHEMA}."{table_name}" CASCADE'
        cur.execute(drop_sql)

        col_defs = ['"fid" SERIAL PRIMARY KEY']
        for name, otype in field_defs:
            pg_type = {
                ogr.OFTInteger: "INTEGER",
                ogr.OFTInteger64: "BIGINT",
                ogr.OFTReal: "DOUBLE PRECISION",
                ogr.OFTString: "TEXT",
                ogr.OFTDate: "DATE",
                ogr.OFTTime: "TIME",
                ogr.OFTDateTime: "TIMESTAMP",
            }.get(otype, "TEXT")
            col_defs.append(f'"{name}" {pg_type}')

        col_defs.append(f'"geom" geometry({pg_geom_type}, {srid})')
        create_sql = f'CREATE TABLE {TARGET_SCHEMA}."{table_name}" ({", ".join(col_defs)})'
        cur.execute(create_sql)

        col_names = ['"fid"'] + [f'"{fd[0]}"' for fd in field_defs] + ['"geom"']
        placeholders = ["%s"] * len(col_names)

        insert_sql = (
            f'INSERT INTO {TARGET_SCHEMA}."{table_name}" '
            f'({", ".join(col_names)}) VALUES ({", ".join(placeholders)})'
        )

        src_layer.ResetReading()
        fid = 0
        for feat in src_layer:
            fid += 1
            row = [fid]
            for name, _ in field_defs:
                val = feat.GetField(name)
                row.append(val)
            geom = feat.GetGeometryRef()
            if geom:
                row.append(geom.ExportToWkt())
            else:
                row.append(None)
            cur.execute(insert_sql, row)

        conn.commit()

        idx_sql = (
            f'CREATE INDEX IF NOT EXISTS idx_{table_name}_geom '
            f'ON {TARGET_SCHEMA}."{table_name}" USING GIST ("geom")'
        )
        cur.execute(idx_sql)
        conn.commit()

        print(f"    [OK] {table_name} ({fid} 条记录)")
        return table_name, "success"
    except Exception as e:
        conn.rollback()
        print(f"    [FAIL] {e}")
        return table_name, f"failed: {e}"
    finally:
        conn.close()
        if "src_ds" in dir() and src_ds:
            src_ds = None


# ============================================================
# 导入栅格 (使用 GDAL + ST_FromGDALRaster, 无需 raster2pgsql)
# ============================================================

def import_raster_via_pixels(raster_path, nr_code, raster_type):
    """逐像元构建栅格到 per-area 表 (含 type 列)"""
    table_name = sanitize_table_name(f"{nr_code}_raster")
    print(f"  [栅格] 导入: {raster_path} -> {TARGET_SCHEMA}.{table_name} (type={raster_type})")

    ds = gdal.Open(raster_path)
    if ds is None:
        return table_name, f"failed: cannot open {raster_path}"

    cols = ds.RasterXSize
    rows = ds.RasterYSize
    bands = ds.RasterCount
    gt = ds.GetGeoTransform()
    srid = get_srid_from_ds(ds)
    ulx, uly, scalex, scaley, skewx, skewy = gt[0], gt[3], gt[1], gt[5], gt[2], gt[4]

    conn = get_pg_conn()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TARGET_SCHEMA}.{table_name} (
                rid SERIAL PRIMARY KEY,
                type TEXT,
                rast RASTER
            )
        """)

        cur.execute("CREATE TEMP TABLE IF NOT EXISTS _tmp_rast (rast RASTER)")
        cur.execute("TRUNCATE _tmp_rast")
        cur.execute(
            f"INSERT INTO _tmp_rast "
            f"SELECT ST_MakeEmptyRaster({cols},{rows},{ulx},{uly},"
            f"{scalex},{scaley},{skewx},{skewy},{srid})"
        )

        for band_idx in range(1, bands + 1):
            band = ds.GetRasterBand(band_idx)
            pt_map = {
                gdal.GDT_Byte: "8BUI", gdal.GDT_UInt16: "16BUI",
                gdal.GDT_Int16: "16BSI", gdal.GDT_UInt32: "32BUI",
                gdal.GDT_Int32: "32BSI", gdal.GDT_Float32: "32BF",
                gdal.GDT_Float64: "64BF",
            }
            pg_pt = pt_map.get(band.DataType, "32BF")
            nodata = band.GetNoDataValue()

            cur.execute(
                f"UPDATE _tmp_rast SET rast = ST_AddBand(rast, "
                f"'{pg_pt}'::text, 0, %s)",
                (nodata,),
            )

            data = band.ReadAsArray(0, 0, cols, rows)
            flat = data.flatten()
            row_parts = []
            for r in range(rows):
                row_parts.append(
                    "{" + ",".join(
                        str(flat[r * cols + c])
                        for c in range(cols)
                    ) + "}"
                )
            arr_text = "{" + ",".join(row_parts) + "}"

            cur.execute(
                f"UPDATE _tmp_rast SET rast = "
                f"ST_SetValues(rast, {band_idx}, 1, 1, "
                f"'{arr_text}'::double precision[][])"
            )

        cur.execute(
            f"INSERT INTO {TARGET_SCHEMA}.{table_name} (type, rast) "
            f"SELECT %s, rast FROM _tmp_rast",
            (raster_type,),
        )
        cur.execute("DROP TABLE _tmp_rast")

        cur.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table_name}_rast_gist "
            f"ON {TARGET_SCHEMA}.{table_name} USING GIST (ST_ConvexHull(rast))"
        )

        conn.commit()
        print(f"    [OK] {table_name} type={raster_type} ({cols}x{rows}, {bands} band(s))")
        return table_name, "success"
    except Exception as e:
        conn.rollback()
        print(f"    [FAIL] {e}")
        return table_name, f"failed: {e}"
    finally:
        ds = None
        conn.close()


def import_raster(raster_path, nr_code, raster_type=None):
    """单条栅格导入入口, 写入 per-area 表 (通过 ST_FromGDALRaster 或回退逐像元)"""
    if raster_type is None:
        raster_type = sanitize_table_name(os.path.splitext(os.path.basename(raster_path))[0])
    table_name = sanitize_table_name(f"{nr_code}_raster")

    ds = gdal.Open(raster_path)
    if ds is None:
        return table_name, f"failed: cannot open {raster_path}"

    srid = get_srid_from_ds(ds)

    mem_path = "/vsimem/import_maxent_tmp.tif"
    gdal.GetDriverByName("GTiff").CreateCopy(mem_path, ds)
    ds = None

    try:
        fp = gdal.VSIFOpenL(mem_path, "rb")
        if fp is None:
            raise IOError(f"cannot read {mem_path}")
        gdal.VSIFSeekL(fp, 0, 2)
        size = gdal.VSIFTellL(fp)
        gdal.VSIFSeekL(fp, 0, 0)
        tiff_bytes = gdal.VSIFReadL(1, size, fp)
        gdal.VSIFCloseL(fp)
    finally:
        gdal.Unlink(mem_path)

    conn = get_pg_conn()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TARGET_SCHEMA}.{table_name} (
                rid SERIAL PRIMARY KEY,
                type TEXT,
                rast RASTER
            )
        """)

        cur.execute(
            "SELECT count(*) FROM pg_proc WHERE proname = 'st_fromgdalraster'"
        )
        has_func = cur.fetchone()[0] > 0

        if not has_func:
            print("    [诊断] ST_FromGDALRaster 不可用, 切换到逐像元构建模式")
            conn.close()
            return import_raster_via_pixels(raster_path, nr_code, raster_type)

        cur.execute(
            f"INSERT INTO {TARGET_SCHEMA}.{table_name} (type, rast) "
            f"SELECT %s, ST_FromGDALRaster(%s, %s)",
            (raster_type, tiff_bytes, 'GTiff'),
        )
        if srid:
            cur.execute(
                f"UPDATE {TARGET_SCHEMA}.{table_name} "
                f"SET rast = ST_SetSRID(rast, %s) "
                f"WHERE type = %s",
                (srid, raster_type),
            )

        cur.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table_name}_rast_gist "
            f"ON {TARGET_SCHEMA}.{table_name} USING GIST (ST_ConvexHull(rast))"
        )

        conn.commit()
        print(f"    [OK] {table_name} type={raster_type}")
        return table_name, "success"
    except Exception as e:
        conn.rollback()
        conn.close()
        print(f"    [诊断] ST_FromGDALRaster 失败 ({e}), 切换到逐像元构建模式")
        return import_raster_via_pixels(raster_path, nr_code, raster_type)


# ============================================================
# 导入 ASCII 栅格 (GDAL 原生支持 .asc)
# ============================================================

def import_asc_raster(asc_path, nr_code, raster_type):
    """ASCII 栅格由 GDAL 原生支持, 直接导入"""
    return import_raster(asc_path, nr_code, raster_type)


# ============================================================
# 扫描并导入所有数据
# ============================================================

def scan_and_import_all():
    """遍历 maxent 数据目录, 导入所有空间数据"""
    conn = get_pg_conn()
    ensure_schema(conn)

    if not os.path.isdir(MAXENT_ROOT):
        print(f"[ERROR] 数据目录不存在: {MAXENT_ROOT}")
        return

    results = {"vector": [], "raster": [], "skipped": []}

    # --- 1. 导入模板数据 (template/) ---
    template_dir = os.path.join(MAXENT_ROOT, "template", "functionGrap")
    if os.path.isdir(template_dir):
        print("\n=== 模板数据 (functionGrap) ===")
        for f in os.listdir(template_dir):
            if f.endswith(".shp"):
                shp_path = os.path.join(template_dir, f)
                layer_name = os.path.splitext(f)[0]
                table_name, status = import_shapefile(shp_path, "template", layer_name)
                log_import(conn, table_name, shp_path, "vector", "template", status)
                results["vector"].append((table_name, status))

    # --- 2. 导入各保护区数据 ---
    nr_dirs = sorted([
        d for d in os.listdir(MAXENT_ROOT)
        if d.startswith("NR") and os.path.isdir(os.path.join(MAXENT_ROOT, d))
    ])

    for nr_code in nr_dirs:
        nr_path = os.path.join(MAXENT_ROOT, nr_code)
        nr_code_lower = nr_code.lower()
        print(f"\n=== {nr_code} ===")

        # 2a. region shapefile
        region_dir = os.path.join(nr_path, "region")
        region_shp = os.path.join(region_dir, "region.shp")
        if os.path.isfile(region_shp):
            table_name, status = import_shapefile(region_shp, nr_code_lower, "region")
            log_import(conn, table_name, region_shp, "vector", nr_code_lower, status)
            results["vector"].append((table_name, status))

        # 2b. tif 栅格 (全部归入同一张 {nr_code}_raster 表, 用 type 区分)
        tif_dir = os.path.join(nr_path, "tif")
        if os.path.isdir(tif_dir):
            for f in sorted(os.listdir(tif_dir)):
                if f.endswith(".tif") and not f.endswith(".tif.ovr") and not f.endswith(".tif.xml") and ".tif." not in f:
                    tif_path = os.path.join(tif_dir, f)
                    layer_type = os.path.splitext(f)[0]
                    table_name, status = import_raster(tif_path, nr_code_lower, raster_type=layer_type)
                    log_import(conn, table_name, tif_path, "raster", nr_code_lower, status)
                    results["raster"].append((table_name, status))

        # 2c. thematic 栅格 (与 tif 同表, type 前缀 t_)
        thematic_dir = os.path.join(nr_path, "thematic")
        if os.path.isdir(thematic_dir):
            for f in sorted(os.listdir(thematic_dir)):
                if f.endswith(".tif") and not f.endswith(".tif.ovr") and not f.endswith(".tif.xml") and ".tif." not in f:
                    tif_path = os.path.join(thematic_dir, f)
                    layer_type = os.path.splitext(f)[0]
                    table_name, status = import_raster(tif_path, nr_code_lower, raster_type=layer_type)
                    log_import(conn, table_name, tif_path, "raster_thematic", nr_code_lower, status)
                    results["raster"].append((table_name, status))

        # 2d. asc 栅格 (与 tif 同表, type 后缀 _asc)
        asc_dir = os.path.join(nr_path, "asc")
        if os.path.isdir(asc_dir):
            for f in sorted(os.listdir(asc_dir)):
                if f.endswith(".asc"):
                    asc_path = os.path.join(asc_dir, f)
                    layer_type = os.path.splitext(f)[0] + "_asc"
                    table_name, status = import_asc_raster(asc_path, nr_code_lower, raster_type=layer_type)
                    if table_name:
                        log_import(conn, table_name, asc_path, "raster_asc", nr_code_lower, status)
                        results["raster"].append((table_name, status))

        # 2e. other 中的 shapefile (如 NR36201 的 三调水系线)
        other_dir = os.path.join(nr_path, "other")
        if os.path.isdir(other_dir):
            for f in os.listdir(other_dir):
                if f.endswith(".shp"):
                    shp_path = os.path.join(other_dir, f)
                    layer_name = os.path.splitext(f)[0]
                    table_name, status = import_shapefile(shp_path, nr_code_lower, layer_name)
                    log_import(conn, table_name, shp_path, "vector_other", nr_code_lower, status)
                    results["vector"].append((table_name, status))

    conn.close()

    # --- 打印汇总 ---
    print("\n" + "=" * 60)
    print("导入完成汇总:")
    print(f"  矢量表: {len(results['vector'])}")
    for t, s in results["vector"]:
        print(f"    {t}: {s}")
    print(f"  栅格表: {len(results['raster'])}")
    for t, s in results["raster"]:
        print(f"    {t}: {s}")
    print(f"  跳过: {len(results['skipped'])}")
    print("=" * 60)


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    scan_and_import_all()
