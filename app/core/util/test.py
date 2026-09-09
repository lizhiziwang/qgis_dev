import datetime as _datetime
import json as _json
import re
import uuid as _uuid
from decimal import Decimal

from local_db_util import local_db_util

#: 空几何 WKT 尾缀(如 ``MULTIPOLYGON EMPTY``)。SpatiaLite 的 ST_GeomFromText
#: 解析不了 EMPTY(会返回 NULL 并触发 batch_add_vector_data 的预校验报错),
#: 迁移时把这类行按“无几何”落库,属性列保留。
_EMPTY_WKT_RE = re.compile(r"\bEMPTY\s*$", re.IGNORECASE)

#: PostgreSQL 连接参数(MySQL 风格键名,connect_db() 内部会做映射)
conn_param = {
    "user": "sde",
    "password": "sde@2025",
    "host": "127.0.0.1",
    "port": 54344,
    "database": "gisdata",
    "charset": "utf8",
    "type": 'postgresql'
}


def connect_db(param: dict | None = None):
    """按 conn_param 配置建立 PostgreSQL 连接(psycopg2)。

    conn_param 里的键是 MySQL 风格,psycopg2 不认,这里做一次映射:

      - ``database`` → ``dbname``
      - ``charset``  → ``client_encoding``(psycopg2 无 charset 参数)
      - ``type``     → 仅 ``postgresql`` 走 psycopg2,其余类型抛错

    Args:
        param: 连接参数字典;缺省用模块级 ``conn_param``。

    Returns:
        psycopg2 连接对象(用完记得 ``conn.close()``)。

    Raises:
        ValueError: type 不是 postgresql / 缺 host、port、user 等关键参数。
    """
    import psycopg2

    cfg = dict(param or conn_param)
    kind = str(cfg.get("type") or "postgresql").lower()
    if kind not in ("postgresql", "postgres", "pg"):
        raise ValueError(f"暂只支持 postgresql 类型连接,收到: {cfg.get('type')!r}")

    kwargs = {k: cfg[k] for k in ("host", "port", "user", "password") if cfg.get(k) is not None}
    dbname = cfg.get("database") or cfg.get("dbname")
    if not dbname:
        raise ValueError("缺少连接参数: database/dbname")
    kwargs["dbname"] = dbname
    if cfg.get("charset"):
        kwargs["client_encoding"] = cfg["charset"]
    return psycopg2.connect(**kwargs)


# ===========================================================================
# PG(PostGIS) → SQLite(SpatiaLite) 表迁移
# ===========================================================================

#: PG ``information_schema.columns.data_type`` → SQLite 声明类型
_PG2SL_TYPE: dict[str, str] = {
    "smallint": "INTEGER", "integer": "INTEGER", "bigint": "INTEGER",
    "numeric": "REAL", "decimal": "REAL", "real": "REAL",
    "double precision": "REAL",
    "boolean": "INTEGER",
    "bytea": "BLOB",
    "date": "TEXT",
    "time without time zone": "TEXT", "time with time zone": "TEXT",
    "timestamp without time zone": "TEXT", "timestamp with time zone": "TEXT",
    "character": "TEXT", "character varying": "TEXT", "text": "TEXT",
    "uuid": "TEXT", "json": "TEXT", "jsonb": "TEXT",
    "ARRAY": "TEXT", "interval": "TEXT",
}

#: PG 几何类型 → SpatiaLite 建列类型。面/线统一升成 MULTI 版:
#: SpatiaLite 单部件列只收单部件,升成 MULTI 后单部件会被
#: local_db_util.batch_add_vector_data 自动 ``ST_Multi`` 包裹,单/多都能进。
_PG2SL_GEOM: dict[str, str] = {
    "POINT": "POINT",
    "MULTIPOINT": "MULTIPOINT",
    "LINESTRING": "MULTILINESTRING",
    "MULTILINESTRING": "MULTILINESTRING",
    "POLYGON": "MULTIPOLYGON",
    "MULTIPOLYGON": "MULTIPOLYGON",
    # 复杂/混合类型 SpatiaLite 没有对应强类型列,统一 GEOMETRY
    "GEOMETRY": "GEOMETRY",
    "GEOMETRYCOLLECTION": "GEOMETRY",
    "MULTICURVE": "GEOMETRY", "MULTISURFACE": "GEOMETRY",
    "CURVE": "GEOMETRY", "SURFACE": "GEOMETRY",
    "TRIANGLE": "GEOMETRY", "POLYHEDRALSURFACE": "GEOMETRY", "TIN": "GEOMETRY",
}

#: PG 里会被当作空间列的 udt 名(PostGIS 的 geometry/geography + Esri ST_Geometry)
_PG_GEOM_UDTS: tuple[str, ...] = ("geometry", "geography", "st_geometry")

#: 迁移时自动跳过的 PG 系统表 / 注册表
_PG_SYSTEM_TABLES: set[str] = {
    "spatial_ref_sys", "geometry_columns", "geography_columns",
    "raster_columns", "raster_overviews", "layer_styles",
    "sde_layers", "sde_table_registry", "sde_column_registry",
    "sde_geometry_columns", "sde_spatial_references", "sde_coordinate_systems",
    "sde_grid", "sde_versions", "sde_aux", "sde_logfiles", "sde_process_information",
    "sde_server_config", "sde_sql_functions", "sde_xml", "sde_xml_columns",
    "sde_xml_documents", "sde_xml_index_tags", "sde_xml_pk",
}


def _q(name) -> str:
    """标识符加双引号(内部 ``"`` 按 SQL 规则转义)。"""
    return '"' + str(name).replace('"', '""') + '"'


def _to_sqlite_value(value):
    """把 psycopg2 返回的 PG 值转成 sqlite3 可绑定的类型。

    Decimal→float、datetime/date/time→isoformat 文本、uuid→str、
    bytea(memoryview)→bytes、list/dict(数组/json 反序列化)→紧凑 JSON。
    """
    if value is None or isinstance(value, (str, int, float, bytes, bytearray, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, _datetime.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (_datetime.date, _datetime.time)):
        return value.isoformat()
    if isinstance(value, _datetime.timedelta):
        return str(value)
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, (list, dict)):
        return _json.dumps(value, ensure_ascii=False)
    return str(value)


def _pg_table_list(cur, tables: list[str] | None, schema: str | None) -> list[tuple[str, str]]:
    """列出待迁移的 (schema, 表名)。

    tables 为空时返回该连接下所有非系统表;给了 tables 则按名字过滤
    (大小写不敏感,schema 限定后只看该 schema)。
    """
    sql = (
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' "
        "AND table_schema NOT IN ('pg_catalog', 'information_schema') "
        "AND position('pg_' in table_schema) <> 1"  # 排除 pg_toast/pg_temp 等系统 schema
    )
    params: list = []
    if schema:
        sql += " AND table_schema = %s"
        params.append(schema)
    sql += " ORDER BY table_schema, table_name"
    cur.execute(sql, params)
    rows = cur.fetchall()

    if tables:
        want = {str(t).lower(): t for t in tables}
        out: list[tuple[str, str]] = []
        for sch, tn in rows:
            low = str(tn).lower()
            if low in want:
                out.append((sch, tn))
                want.pop(low)
        if want:
            raise ValueError(
                "以下表不存在(或不在指定 schema 内): "
                + ", ".join(repr(t) for t in want.values())
            )
        return out
    return [(s, t) for s, t in rows if str(t).lower() not in _PG_SYSTEM_TABLES]


def _sync_srid(pg_conn, db_util, srid: int | None) -> None:
    """把 PG 的 spatial_ref_sys 行补进本地空间参考表。

    - SpatiaLite: 抄进 ``spatial_ref_sys``(没有该 SRID 时 ST_GeomFromText
      会返回 NULL);
    - GeoPackage: 抄进 ``gpkg_spatial_ref_sys``(``gpkg_contents`` 外键需要
      先有 srs 行);PG 也没有该号时补一个 ``undefined`` 占位行保迁移不卡死
      (几何照常入库,只是坐标系未知)。
    """
    if not srid:
        return
    gpkg = getattr(db_util, "kind", "spatialite") == "gpkg"
    tab = "gpkg_spatial_ref_sys" if gpkg else "spatial_ref_sys"
    idcol = "srs_id" if gpkg else "srid"
    hit = db_util.cursor.execute(
        f"SELECT {idcol} FROM {tab} WHERE {idcol} = ?", (srid,)
    ).fetchone()
    if hit:
        return
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT srid, auth_name, auth_srid, srtext, proj4text "
            "FROM spatial_ref_sys WHERE srid = %s",
            (srid,),
        )
        row = cur.fetchone()
    if gpkg:
        if row is None:
            print(f"[警告] PG spatial_ref_sys 没有 SRID={srid},补占位行(坐标系未知)")
            db_util.cursor.execute(
                "INSERT OR IGNORE INTO gpkg_spatial_ref_sys "
                "(srs_name, srs_id, organization, organization_coordsys_id, "
                "definition) VALUES (?, ?, ?, ?, ?)",
                (f"user-defined:{srid}", srid, "NONE", srid, "undefined"),
            )
        else:
            auth = row[1] or "NONE"
            auth_srid = int(row[2]) if row[2] is not None else int(row[0])
            db_util.cursor.execute(
                "INSERT OR IGNORE INTO gpkg_spatial_ref_sys "
                "(srs_name, srs_id, organization, organization_coordsys_id, "
                "definition) VALUES (?, ?, ?, ?, ?)",
                (
                    f"{auth}:{auth_srid}", int(row[0]), auth, auth_srid,
                    row[3] or "undefined",
                ),
            )
    else:
        if row is None:
            print(f"[警告] PG spatial_ref_sys 里没有 SRID={srid},该表的几何可能写不进去")
            return
        db_util.cursor.execute(
            "INSERT INTO spatial_ref_sys (srid, auth_name, auth_srid, srtext, proj4text) "
            "VALUES (?, ?, ?, ?, ?)",
            (row[0], row[1], row[2], row[3], row[4]),
        )
    db_util.conn.commit()


def _target_exists(db_util, tn: str) -> bool:
    """迁移前检查: sqlite 目标表是否已存在。

    按大小写不敏感匹配(sqlite 里不带引号手工建的表名会被折叠成大写,
    避免同名不同大小写的表被重复创建)。
    """
    row = db_util.cursor.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name = ? COLLATE NOCASE",
        (tn,),
    ).fetchone()
    return row is not None


def _drop_target(db_util, tn: str) -> None:
    """删除 sqlite 端旧表(含几何列/空间索引/旧注册)。

    表名按 sqlite_master 里实际存储的大小写取,几何列名按格式从
    ``gpkg_geometry_columns``(GeoPackage)或 ``geometry_columns``
    (SpatiaLite)不敏感匹配取,再交给 local_db_util 内部清理。
    """
    row = db_util.cursor.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name = ? COLLATE NOCASE",
        (tn,),
    ).fetchone()
    if row is None:
        return
    real = row[0]
    if getattr(db_util, "kind", "spatialite") == "gpkg":
        g = db_util.cursor.execute(
            "SELECT column_name FROM gpkg_geometry_columns "
            "WHERE table_name = ? COLLATE NOCASE",
            (tn,),
        ).fetchone()
    else:
        g = db_util.cursor.execute(
            "SELECT f_geometry_column FROM geometry_columns "
            "WHERE f_table_name = ? COLLATE NOCASE",
            (tn,),
        ).fetchone()
    db_util._drop_spatial_table(real, g[0] if g else "geom")
    db_util.conn.commit()


def _migrate_one_pg_table(
    pg_conn, db_util, schema: str, tn: str, *, overwrite: bool, batch_size: int
) -> int:
    """迁移单张表,返回写入 sqlite 的行数。"""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, udt_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, tn),
        )
        cols = cur.fetchall()
        if not cols:
            raise ValueError("表不存在或没有任何列")
        # PostGIS 几何列元信息(geometry_columns 注册过的才有)
        cur.execute(
            "SELECT f_geometry_column, srid, type, coord_dimension "
            "FROM geometry_columns WHERE f_table_schema = %s AND f_table_name = %s",
            (schema, tn),
        )
        ginfo = {row[0]: row for row in cur.fetchall()}

        # 主几何列 = 第一个空间列;其余空间列降级为 TEXT(WKT) 属性(见建表/取数处)
        geom_col = None
        for name, _, udt in cols:
            if udt.lower() in _PG_GEOM_UDTS and geom_col is None:
                geom_col = name

        srid = None
        gtype = None
        if geom_col is not None:
            meta = ginfo.get(geom_col)
            if meta is not None:
                srid = int(meta[1]) if meta[1] is not None else None
                gtype = meta[2]
            # 未注册/通用 GEOMETRY 类型(或 Esri ST_Geometry,PostGIS 的
            # geometry_columns 视图查不到)时,抽一行真实几何反推 SRID 与类型;
            # 反推失败(如 SDE 无这些函数)则退化 SRID=0 / GEOMETRY 继续建表
            if srid is None or (gtype or "").upper() in ("GEOMETRY", ""):
                try:
                    cur.execute(
                        f"SELECT ST_SRID({_q(geom_col)}), GeometryType({_q(geom_col)}) "
                        f"FROM {_q(schema)}.{_q(tn)} "
                        f"WHERE {_q(geom_col)} IS NOT NULL LIMIT 1"
                    )
                    row = cur.fetchone()
                except Exception:
                    row = None
                if row and row[0] is not None:
                    srid = int(row[0])
                if row and row[1]:
                    gtype = str(row[1]).upper()

        # ---- 迁移前: 检查 sqlite 目标表是否存在,不存在则先建表 ----
        if _target_exists(db_util, tn):
            if not overwrite:
                print(f"[检查] 目标表 {tn!r} 已存在(overwrite=False),跳过该表")
                return 0
            print(f"[检查] 目标表 {tn!r} 已存在,overwrite=True → 删除后重建")
            _drop_target(db_util, tn)
        else:
            print(f"[检查] 目标表 {tn!r} 不存在,将按 PG 结构新建")
        # 清掉同名历史注册(名字可能大小写不同)
        db_util.cursor.execute(
            "DELETE FROM user_relation WHERE table_name = ? COLLATE NOCASE", (tn,)
        )
        db_util.conn.commit()

        attr_defs: list[tuple[str, str]] = []
        for name, data_type, udt in cols:
            if name == geom_col:
                continue
            if udt.lower() in _PG_GEOM_UDTS:
                attr_defs.append((name, "TEXT"))  # 额外空间列按 WKT 文本保存
                continue
            attr_defs.append((name, _PG2SL_TYPE.get((data_type or "").lower(), "TEXT")))
        if not attr_defs and geom_col is None:
            raise ValueError("表没有任何可迁移的列")

        # 建表统一走 local_db_util 公开接口(spatialite/GeoPackage 双格式):
        # 面/线统一升成 MULTI(PG 单部件面/线也常有),空间表自动登记
        # gpkg_contents/gpkg_geometry_columns 或 AddGeometryColumn + 空间索引
        sl_type = _PG2SL_GEOM.get((gtype or "").upper(), "GEOMETRY")
        srid = int(srid) if srid is not None else 0
        _sync_srid(pg_conn, db_util, srid)  # gpkg_contents 外键需 srs 行先存在
        db_util.create_vector_table(
            tn,
            columns=attr_defs,
            geometry_column=geom_col,
            geometry_type=sl_type if geom_col is not None else None,
            srid=srid,
            register=geom_col is not None,  # 空间表自动登记 user_relation(spatial)
        )
        if geom_col is None:
            # 纯属性表: 不入 gpkg 特征图层,仅登记 dict 类(可被目录列出)
            db_util.add_domain_table(tn, "dict")
        print(f"[建表] {schema}.{tn} 几何列={geom_col or '无'} SRID={srid or '—'}")

        # ---- 流式取数并写入 ----
        select_exprs: list[str] = []
        keys: list[str] = []
        for name, _, udt in cols:
            if name == geom_col:
                continue  # 最后统一 ST_AsText
            if udt.lower() in _PG_GEOM_UDTS:
                select_exprs.append(f"ST_AsText({_q(name)}) AS {_q(name)}")
            else:
                select_exprs.append(_q(name))
            keys.append(name)
        if geom_col is not None:
            select_exprs.append(f"ST_AsText({_q(geom_col)}) AS {_q(geom_col)}")
            keys.append(geom_col)
        sql = f"SELECT {', '.join(select_exprs)} FROM {_q(schema)}.{_q(tn)}"

        done = 0
        empty_geom = 0
        cur = pg_conn.cursor(name="pg_migrate_stream")  # 服务端游标,按批取不占内存
        try:
            cur.execute(sql)
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                if geom_col is None:
                    # 纯属性表
                    placeholders = ", ".join(["?"] * len(keys))
                    insert_sql = (
                        f"INSERT INTO {_q(tn)} ({', '.join(_q(k) for k in keys)}) "
                        f"VALUES ({placeholders})"
                    )
                    db_util.cursor.executemany(
                        insert_sql,
                        [tuple(_to_sqlite_value(v) for v in r) for r in rows],
                    )
                    db_util.conn.commit()
                else:
                    # 空间表: 走 local_db_util 公开接口(自动 ST_Multi 包裹、几何校验)
                    data: list[dict] = []
                    for r in rows:
                        rec = {}
                        for i, k in enumerate(keys):
                            if k == geom_col:
                                if r[i] is not None:
                                    wkt = str(r[i])
                                    if _EMPTY_WKT_RE.search(wkt):
                                        empty_geom += 1  # EMPTY 几何按 NULL 落库
                                    else:
                                        rec[k] = wkt
                                continue
                            rec[k] = _to_sqlite_value(r[i])
                        data.append(rec)
                    db_util.batch_add_vector_data(tn, data)
                done += len(rows)
                print(f"[{schema}.{tn}] 已迁移 {done} 行")
        finally:
            cur.close()
        if empty_geom:
            print(
                f"[{schema}.{tn}] 共 {done} 行,其中 {empty_geom} 行空几何"
                "(EMPTY)已按无几何落库"
            )
        return done


def migrate_pg_to_sqlite(
    db_util: local_db_util,
    tables: list[str] | None = None,
    schema: str | None = None,
    *,
    overwrite: bool = False,
    batch_size: int = 2000,
    pg_param: dict | None = None,
) -> dict[str, int]:
    """把 PostgreSQL/PostGIS 的表(含空间表)迁移进本地库。

    - 迁移前逐表检查 sqlite 目标表: 不存在 → 先按 PG 结构(属性列 +
      空间列)建表再迁数据;已存在且 ``overwrite=False`` → 跳过该表;
      已存在且 ``overwrite=True`` → 删除旧表后重建再迁。
    - 空间表: 按 PG ``geometry_columns``(缺失时抽第一行真实几何)推导 SRID /
      几何类型建空间表;面/线统一建成 MULTI 类型列,源数据的单部件行会被
      ``wrap_to_multi`` / ``ST_Multi`` 自动归一,多部件也能原样进入;SRID 在
      本地参考表缺失时会从 PG 的 ``spatial_ref_sys`` 抄一份(GeoPackage 写
      ``gpkg_spatial_ref_sys``,SpatiaLite 写 ``spatial_ref_sys``)。
    - 纯属性表: 建成普通表,注册为 ``dict`` 类;空间表注册为 ``spatial``,
      均可被 :meth:`local_db_util.query_all_table_name` 列出。
    - PG 系统表(spatial_ref_sys / geometry_columns / SDE_* 等)自动跳过。

    Args:
        db_util: :class:`local_db_util` 实例(即目标 sqlite 库)。
        tables: 要迁移的表名列表;None 表示迁移所有非系统表。
        schema: PG 表所在 schema;None 表示遍历当前连接所有非系统 schema。
        overwrite: 目标库已有同名表时 True=删掉重建,False=跳过该表。
        batch_size: 每批拉取/写入行数(服务端游标流式取数,不占内存)。
        pg_param: PG 连接参数,缺省用模块级 ``conn_param``。

    Returns:
        ``{"schema.表名": 迁移行数}``;已存在被跳过的表不在结果里。
    """
    if getattr(db_util, "kind", "spatialite") == "mbtiles":
        raise ValueError("MBTiles 库不支持迁移矢量/字典表,请改用 spatialite/GeoPackage 格式的库")
    if getattr(db_util, "kind", "spatialite") == "spatialite":
        db_util._ensure_spatial_ready()  # 加载 spatialite + 补齐 user_relation

    pg_conn = connect_db(pg_param)
    try:
        with pg_conn.cursor() as cur:
            tasks = _pg_table_list(cur, tables, schema)
        if not tasks:
            print("没有找到可迁移的表")
            return {}
        summary: dict[str, int] = {}
        for sch, tn in tasks:
            key = f"{sch}.{tn}"
            try:
                n = _migrate_one_pg_table(
                    pg_conn, db_util, sch, tn, overwrite=overwrite, batch_size=batch_size
                )
            except Exception as exc:
                print(f"[失败] {key}: {exc}")
                summary[key] = -1
                continue
            if n:
                summary[key] = n
        return summary
    finally:
        pg_conn.close()


if __name__ == '__main__':
    # 本地 sqlite 工具
    db_util = local_db_util('test01.gpkg')  # 新规: 本体 .gpkg
    migrate_pg_to_sqlite(
        db_util,
        tables=["ldsp_ldtb"],
        schema="sde",
        overwrite=True,
        batch_size=1000,
        pg_param=conn_param,
    )
