'''
    本地sqlite数据创建管理工具
'''

import json
import math
import re
import uuid
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

import pysqlite3 as sqlite3

# ---------------------------------------------------------------------------
# ArcGIS Server REST 图层元信息 → SpatiaLite 建表 映射
# ---------------------------------------------------------------------------

#: 要素图层的 ``geometryType`` → ``AddGeometryColumn`` 几何类型。
#: SpatiaLite 的几何列类型约束是严格匹配的(见 :meth:`create_arcgis_layer`
#: 关于多部件的说明),此处按 ArcGIS 语义一一对应。
_ESRI_GEOM_TYPE_TO_SL: dict[str, str] = {
    "esriGeometryPoint": "POINT",
    "esriGeometryMultipoint": "MULTIPOINT",
    "esriGeometryPolyline": "LINESTRING",
    "esriGeometryPolygon": "POLYGON",
    "esriGeometryMultiPatch": "GEOMETRY",
}

#: 字段 ``esriFieldType*`` → SQLite 声明类型(建表时写在列名后)。
_ESRI_FIELD_TYPE_TO_SQL: dict[str, str] = {
    "esriFieldTypeOID": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "esriFieldTypeInteger": "INTEGER",
    "esriFieldTypeSmallInteger": "INTEGER",
    "esriFieldTypeDouble": "DOUBLE",
    "esriFieldTypeSingle": "DOUBLE",
    "esriFieldTypeString": "TEXT",  # 有 length 时实际用 VARCHAR(length)
    "esriFieldTypeDate": "DATETIME",
    "esriFieldTypeGUID": "TEXT",
    "esriFieldTypeGlobalID": "TEXT",
    "esriFieldTypeXML": "TEXT",
    "esriFieldTypeBlob": "BLOB",
    "esriFieldTypeRaster": "BLOB",
}


def _quote_ident(name: str) -> str:
    """给标识符加双引号,内部 ``"`` 按 SQLite 规则转义。"""
    return '"' + str(name).replace('"', '""') + '"'


def _decode_http_text(raw: bytes, charset: str | None = None) -> str:
    """把 HTTP 响应字节解成文本(ArcGIS JSON 通常是 UTF-8)。

    - 未声明 charset / 声明为 UTF-8 系: 优先 ``utf-8-sig``(顺带去掉 BOM)
    - 声明了其他 charset(如 gbk): 先按声明解,失败再退回 UTF-8
    - 全失败才用 ``errors="replace"`` 容错
    """
    candidates: list[str] = []
    if charset and "utf" not in charset.lower():
        candidates.append(charset)
    candidates += ["utf-8-sig", "utf-8"]
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _http_error_hint(text: str) -> str:
    """从错误响应文本里尽量挖一句人话: JSON ``error.message``,否则去 HTML 标签。"""
    try:
        obj = json.loads(text)
    except ValueError:
        pass
    else:
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, dict):
                msg = (err.get("message") or "").strip()
                code = err.get("code")
                return f"(code={code}) {msg}" if msg else str(err)[:200]
            return str(obj)[:200]
    hint = re.sub(r"<[^>]+>", " ", text)
    hint = re.sub(r"\s+", " ", hint).strip()
    return hint[:200]

def _sql_token(name: str | None) -> str:
    """把任意字符串清洗成可安全作表名/列名的标识符。

    - 保留中文、字母、数字、下划线;其余字符(空格、点、斜杠等)折叠成 ``_``
    - 去掉首尾 ``_``;数字开头补 ``_`` 前缀;空串回退 ``_``
    """
    s = re.sub(r"\W+", "_", str(name or "").strip())
    s = s.strip("_")
    if not s:
        s = "_"
    if s[0].isdigit():
        s = "_" + s
    return s


def _extent_to_wgs84_bounds(meta: dict) -> tuple[float, float, float, float] | None:
    """把图层 ``extent`` 换算成 WGS84 经纬度 (min_lon, min_lat, max_lon, max_lat)。

    - 源为经纬度坐标系(4326 / 4490 等): 数值直接采用
    - 源为 Web 墨卡托(3857 / 102100): 反投影换算
    - 其他投影(如高斯克吕格 4527): 返回 ``None``,由调用方显式传 ``bounds``
    """
    ext = meta.get("extent") or {}
    xmin, ymin, xmax, ymax = (
        ext.get("xmin"),
        ext.get("ymin"),
        ext.get("xmax"),
        ext.get("ymax"),
    )
    if None in (xmin, ymin, xmax, ymax):
        return None
    sr = ext.get("spatialReference") or meta.get("sourceSpatialReference") or {}
    wkid = sr.get("latestWkid") or sr.get("wkid")
    if wkid in (4326, 4490, 4610):  # CGCS2000/WGS84 与 GRS80 经纬度,误差可忽略
        return (xmin, ymin, xmax, ymax)
    if wkid in (3857, 102100, 900913):  # Web 墨卡托 → 经纬度反投影
        radius = 6378137.0

        def lon(mx: float) -> float:
            return mx / radius * 180.0 / math.pi

        def lat(my: float) -> float:
            return (2.0 * math.atan(math.exp(my / radius)) - math.pi / 2.0) * 180.0 / math.pi

        return (lon(xmin), lat(ymin), lon(xmax), lat(ymax))
    return None


#: SpatiaLite ``geometry_columns.geometry_type`` 整数编码 → 类型名
_GEOMETRY_CODE_TO_NAME: dict[int, str] = {
    0: "GEOMETRY",
    1: "POINT",
    2: "LINESTRING",
    3: "POLYGON",
    4: "MULTIPOINT",
    5: "MULTILINESTRING",
    6: "MULTIPOLYGON",
    7: "GEOMETRYCOLLECTION",
}

#: 单部件类型集合(几何列声明这些类型时,拒绝插入多部件几何)
_SINGLE_GEOM_TYPES = {"POINT", "LINESTRING", "POLYGON"}
#: 多部件类型集合(插入单部件时自动 ``ST_Multi`` 包裹)
_MULTI_GEOM_TYPES = {"MULTIPOINT", "MULTILINESTRING", "MULTIPOLYGON"}


def _coord_str(value: Any) -> str:
    """坐标值转文本(保留浮点全精度,避免 4527 等大坐标被截短)。"""
    return repr(value) if isinstance(value, float) else str(value)


def _wkt_kind(text: str) -> str:
    """取 WKT 字符串的类型关键字(去掉 SRID= 前缀后、第一个括号前的词)。"""
    body = re.sub(r"^\s*SRID\s*=\s*\d+\s*;\s*", "", text, flags=re.I)
    token = re.split(r"[\s(]", body.strip(), maxsplit=1)[0].upper()
    return token or "GEOMETRY"


def _ring_wkt(ring: list) -> str:
    """一条坐标环 → WKT 内部 ``(x y, x y, ...)`` 片段。"""
    return "(" + ",".join(" ".join(_coord_str(c) for c in pt) for pt in ring) + ")"


def _esri_geometry_to_wkt(geom: dict) -> str | None:
    """ArcGIS 要素的 geometry JSON → WKT 文本。

    支持 esriGeometryPoint(``x``/``y``)、Multipoint(``points``)、
    Polyline(``paths``)、Polygon(``rings``)。面数据里每个 ring 视作一个
    独立部件(带洞的面请先在外部转成规范 WKT 再传入)。只取 2D 坐标。
    单部件结果给非 MULTI 前缀 WKT,多部件给 MULTI 前缀,便于调用方判断。
    """
    rings = geom.get("rings")
    if rings is not None:
        parts = ["(" + _ring_wkt(r) + ")" for r in rings]
        if len(parts) == 1:
            return "POLYGON " + parts[0]
        return "MULTIPOLYGON (" + ",".join(parts) + ")"
    paths = geom.get("paths")
    if paths is not None:
        parts = [_ring_wkt(p) for p in paths]
        if len(parts) == 1:
            return "LINESTRING " + parts[0]
        return "MULTILINESTRING (" + ",".join(parts) + ")"
    points = geom.get("points")
    if points is not None:
        parts = ["(" + " ".join(_coord_str(c) for c in pt) + ")" for pt in points]
        if len(parts) == 1:
            return "POINT " + parts[0]
        return "MULTIPOINT (" + ",".join(parts) + ")"
    if geom.get("x") is not None and geom.get("y") is not None:
        return "POINT (" + _coord_str(geom["x"]) + " " + _coord_str(geom["y"]) + ")"
    return None


def _to_wkt(geom: Any) -> str | None:
    """把行里的几何值统一成 WKT: 已是 str 直接用(剥掉 SRID= 前缀),dict 按 ArcGIS JSON 转。"""
    if geom is None:
        return None
    if isinstance(geom, str):
        return re.sub(r"^\s*SRID\s*=\s*\d+\s*;\s*", "", geom.strip(), flags=re.I)
    if isinstance(geom, dict):
        return _esri_geometry_to_wkt(geom)
    return None


class local_db_util:
    def __init__(
        self,
        db_path: str | Path,
        *,
        load_spatialite: bool = False,
    ) -> None:
        """打开(或新建)本地 SQLite / SpatiaLite 数据库。

        Args:
            db_path: 数据库文件路径,接受 ``str`` 或 ``Path``。
                - 文件不存在: 自动创建空库并执行 :meth:`init_db`
                  (加载 spatialite、建元数据、建 ``user_relation`` 表),
                  设置 ``is_new=True``
                - 文件已存在: 直接打开,设置 ``is_new=False``,
                  不会执行 :meth:`init_db`(防止重复建表)
                - 父目录不存在: 自动创建(首次建库常见场景)
            load_spatialite: 是否在 ``__init__`` 阶段尝试加载 ``mod_spatialite``
                扩展。加载失败(纯 sqlite / 编译期禁用了扩展加载)不抛错,
                ``has_spatialite`` 保持 ``False``;:meth:`init_db` 会按需再加载。

        Attributes:
            db_path:        规范化后的 ``Path``。
            conn / cursor:  ``sqlite3.Connection`` / ``Cursor``,已开启
                            ``row_factory=Row``、``PRAGMA foreign_keys=ON``、
                            ``detect_types=PARSE_DECLTYPES|PARSE_COLNAMES``。
            is_new:         本次构造时数据库文件是否新建。
                            ``True`` 时 :meth:`init_db` 已自动执行。
            has_spatialite: ``mod_spatialite`` 扩展是否成功加载
                            (仅在 ``load_spatialite=True`` 时尝试)。

        注意:
            事务需要显式 ``commit()``。``init_db`` 只在 ``is_new=True`` 时自动
            触发——已存在的库(可能含业务数据)不会被 ``__init__`` 改写。
        """
        # 规范化路径 + 首次建库前先确保父目录存在
        self.db_path: Path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.is_new: bool = not self.db_path.exists()
        # 库类型: 后缀为 ``.mbtiles`` → 纯 MBTiles 瓦片库(不建 spatialite/"user_relation");
        # 其余按 spatialite 本地库管理(矢量图层 + 注册表)。
        self.kind: str = (
            "mbtiles" if self.db_path.suffix.lower() == ".mbtiles" else "spatialite"
        )

        self.conn: sqlite3.Connection = sqlite3.connect(
            self.db_path,
            # 让 DATETIME / DATE 列在读写时自动转换为 Python datetime,
            # 避免每个查询再手动 str/datetime 互转。
            # pysqlite3 只接受构造参数,不能事后赋值。
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        )
        self.conn.row_factory = sqlite3.Row
        # 默认开启外键约束(逐连接设置,不影响其他连接)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.cursor: sqlite3.Cursor = self.conn.cursor()

        #: 空间元数据(InitSpatialMetaData)是否已就绪,避免重复初始化
        self._spatial_ready: bool = False

        self.has_spatialite: bool = False
        # MBTiles 库不需要 mod_spatialite,跳过加载;只有 spatialite 库才尝试
        # if load_spatialite and self.kind == "spatialite":
        #     # 扩展不可用时静默忽略——纯 sqlite / 编译期禁用了扩展加载都属正常
        #     try:
        self.conn.enable_load_extension(True)
        self.conn.load_extension("mod_spatialite")
        self.has_spatialite = True
            # except (sqlite3.OperationalError, AttributeError):
            #     pass

        # 新建的库自动初始化 schema;已有库不动,避免覆盖业务数据
        if self.is_new:
            if self.kind == "mbtiles":
                self.init_mbtiles_db()
            else:
                self.init_db()


    def init_db(self):
        self.cursor.execute("SELECT load_extension('mod_spatialite')")
        self.cursor.execute("SELECT InitSpatialMetaData(1);")
        self._spatial_ready = True
        self.cursor.execute('''
                CREATE TABLE "user_relation" (
                  "id" text NOT NULL,
                  "table_name" TEXT,
                  "type" TEXT,
                  PRIMARY KEY ("id")
                );
        ''')

    # --------------------------------------------------------- MBTiles 瓦片库

    def init_mbtiles_db(self) -> None:
        """给新建的 ``.mbtiles`` 库做初始化(纯 sqlite,不加载 spatialite)。

        只预建一张全局 ``metadata`` 键值表;瓦片数据表不在这里建,由
        :meth:`create_arcgis_image_store` 按图层名(``layer_meta["name"]``)
        动态创建。注意瓦片行号若按 TMS 约定存放(行号原点在南侧),写入时
        需自行翻转,读取工具一般会处理。
        """
        self._ensure_mbtiles_schema()
        self.conn.commit()

    def _ensure_mbtiles_schema(self) -> None:
        """幂等确保全局 ``metadata`` 表存在(老库缺表 / 换连接时补建)。"""
        self.cursor.execute(
            'CREATE TABLE IF NOT EXISTS "metadata" (name TEXT, value TEXT)'
        )

    # --------------------------------------------------------- 内部工具

    def _ensure_spatial_ready(self) -> None:
        """惰性加载 spatialite 并初始化空间元数据(幂等)。

        老库 / ``load_spatialite=False`` 打开的库首次建空间图层时调用,
        避免重复执行 ``InitSpatialMetaData``(重复执行会打错误日志)。
        """
        if self.kind == "mbtiles":
            raise RuntimeError(
                f"{self.db_path} 是 MBTiles 瓦片库,不支持 create_arcgis_layer 等"
                "空间图层操作,请改用 spatialite 库"
            )
        if self._spatial_ready:
            return
        if not self.has_spatialite:
            try:
                self.conn.enable_load_extension(True)
                self.conn.load_extension("mod_spatialite")
                self.has_spatialite = True
            except (sqlite3.OperationalError, AttributeError):
                pass
        if not self.has_spatialite:
            raise RuntimeError("mod_spatialite 不可用,无法创建空间图层")
        # 库已初始化过空间元数据(老库 / 别的连接建过)就不要再 Init,避免重复执行报错
        has_meta = self.cursor.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE type='table' AND name='spatial_ref_sys'"
        ).fetchone()[0]
        if not has_meta:
            self.cursor.execute("SELECT InitSpatialMetaData(1)")
        # 兼容旧库:没有 user_relation 时补一张,后续注册图层用
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS "user_relation" (
              "id" text NOT NULL,
              "table_name" TEXT,
              "type" TEXT,
              PRIMARY KEY ("id")
            )
        ''')
        self._spatial_ready = True
        self.conn.commit()

    def _drop_spatial_table(self, table_name: str, geometry_column: str) -> None:
        """尽量干净地删除一张(可能带几何列/空间索引的)表。

        依次尝试: 拆空间索引 → 注销几何列 → 删表。
        任一步失败只静默跳过(比如表本来就不存在),保证整体可继续。
        """
        for stmt, args in (
            ("SELECT DisableSpatialIndex(?, ?)", (table_name, geometry_column)),
            ("SELECT DiscardGeometryColumn(?, ?)", (table_name, geometry_column)),
            ("DROP TABLE IF EXISTS " + _quote_ident(table_name), ()),
        ):
            with suppress(sqlite3.Error):
                self.cursor.execute(stmt, args)

    # --------------------------------------------------------- 对外接口

    def add_domain_table(self,tn: str, type_name: str) -> None:
        # 老库/纯 MBTiles 库可能没有 user_relation,先补一张再注册
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS "user_relation" (
              "id" text NOT NULL,
              "table_name" TEXT,
              "type" TEXT,
              PRIMARY KEY ("id")
            )
        ''')
        self.cursor.execute('''
            insert into user_relation (id,table_name,type) values (?,?,?)
        ''', (str(uuid.uuid4()), tn, type_name))
        self.conn.commit()

    def get_arcgis_layer_metadata(
        self,
        url: str,
        *,
        timeout: float = 15.0,
        extra_params: dict[str, Any] | None = None,
    ) -> dict:
        """从 ArcGIS Server REST 服务拉取图层元信息,解析成 dict。

        返回的 dict 可直接传给 :meth:`create_arcgis_layer` 建表。

        Args:
            url: 图层 REST 地址,形如::

                    http://host:6080/arcgis/rest/services/图层名/MapServer/0

                可带 ``?f=json`` / ``token=…`` 等查询参数。方法会自动把
                ``f`` 置为 ``json`` 并保留其余参数;路径中未编码的中文会
                自动补百分号编码。
            timeout: 请求超时秒数。
            extra_params: 额外查询参数(如 ``{"token": "..."}``);与 URL
                自带参数重名时以此为准。

        Returns:
            图层元信息 dict(即服务 ``f=json`` 返回对象,原样解析)。

        Raises:
            ValueError: URL 为空 / 非 http(s) 协议。
            RuntimeError: 网络失败、HTTP 非 200、ArcGIS 返回 ``error``
                信封、返回内容不是 JSON 对象等情况。
        """
        url = (url or "").strip()
        if not url:
            raise ValueError("url 不能为空")
        parts = urlparse(url)
        if parts.scheme not in ("http", "https"):
            raise ValueError(f"仅支持 http/https 地址,收到: {parts.scheme!r}")

        # 路径中的非 ASCII(如中文)补百分号编码;已编码的 %XX 原样保留
        path = quote(parts.path, safe="/%:@!$&'()*+,;=~._-")
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update(extra_params or {})
        query["f"] = "json"
        full_url = urlunparse(
            (parts.scheme, parts.netloc, path, "", urlencode(query), "")
        )

        req = Request(
            full_url,
            headers={
                "User-Agent": "qgis-dev/local_db_util",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                charset = resp.headers.get_content_charset()
        except HTTPError as e:
            # 部分服务 4xx/5xx 也返回 JSON/HTML 错误页,尽量挖出可读信息
            detail = ""
            try:
                body = e.read()
                body_charset = e.headers.get_content_charset() if e.headers else None
                detail = _http_error_hint(_decode_http_text(body, body_charset))
            except Exception:
                pass
            raise RuntimeError(f"ArcGIS 请求失败 HTTP {e.code}: {detail or e.reason}") from e
        except (URLError, TimeoutError, OSError) as e:
            raise RuntimeError(f"请求 ArcGIS 服务失败: {e}") from e

        text = _decode_http_text(raw, charset)
        try:
            data = json.loads(text)
        except ValueError as e:
            raise RuntimeError(
                f"ArcGIS 返回内容不是合法 JSON,前 200 字符: {text[:200]!r}"
            ) from e
        if not isinstance(data, dict):
            raise RuntimeError("ArcGIS 返回内容不是 JSON 对象")
        err = data.get("error")
        if isinstance(err, dict):
            raise RuntimeError(f"ArcGIS 服务返回错误: {_http_error_hint(text)}")
        return data

    def create_arcgis_layer(
        self,
        layer_meta: dict,
        *,
        table_name: str | None = None,
        geometry_column: str | None = None,
        geometry_type: str | None = None,
        srid: int | None = None,
        register: bool = True,
        overwrite: bool = False,
    ) -> str:
        """按 ArcGIS Server 图层元信息在本地库建一张空间表(图层)。

        Args:
            layer_meta: 要素图层元信息 JSON,即服务 ``…/layers/0?f=json``
                返回的对象(可用 :meth:`get_arcgis_layer_metadata` 拉取)。
                必须含 ``geometryType``、``name``、``fields``;
                ``sourceSpatialReference`` / ``geometryField`` / ``extent``
                为可选,用于推导 SRID 与几何列名。
            table_name: 目标表名。缺省由 ``layer_meta["name"]`` 清洗生成
                (保留中文/字母/数字/下划线,其余折成 ``_``)。
            geometry_column: 几何列名。缺省取 ``geometryField.name``,
                没有再回退 ``geom``。
            geometry_type: SpatiaLite 几何类型(如 ``POLYGON``、
                ``MULTIPOLYGON``),缺省由 ``geometryType`` 映射:
                esriGeometryPoint→POINT、esriGeometryMultipoint→MULTIPOINT、
                esriGeometryPolyline→LINESTRING、esriGeometryPolygon→POLYGON、
                esriGeometryMultiPatch→GEOMETRY。

                注意: SpatiaLite 几何列的类型约束是严格匹配的——声明
                ``POLYGON`` 的列只接受单面,声明 ``MULTIPOLYGON`` 的列只接受
                多面(单面需先 ``ST_Multi`` 包裹)。ArcGIS 面/线要素可能存在
                多部件(如含岛屿的行政区),若后续要原样灌入多部件数据,建表时
                请显式传 ``MULTIPOLYGON`` / ``MULTILINESTRING`` 或 ``GEOMETRY``。
            srid: 空间参考 ID。缺省按 ``sourceSpatialReference.latestWkid``
                → ``wkid`` → ``extent.spatialReference`` 取,都缺省时 4326。
            register: 是否写入 ``user_relation``(type=spatial),使图层进入
                :meth:`query_all_table_name` 的空间图层清单。
            overwrite: 同名表已存在时先删旧表再重建;默认 ``False`` 抛异常。

        Returns:
            实际建成的表名(与 ``layer_meta["name"]`` 可能不同)。
        """
        self._ensure_spatial_ready()

        geom_type_key = layer_meta.get("geometryType")
        if not geom_type_key:
            raise ValueError("layer_meta 缺少 geometryType,无法确定几何类型")
        if geometry_type:
            # 允许直接传 ArcGIS 的 key(如 esriGeometryPolygon),方便调用方复用映射
            geom_type = _ESRI_GEOM_TYPE_TO_SL.get(geometry_type) or geometry_type
        else:
            geom_type = _ESRI_GEOM_TYPE_TO_SL.get(geom_type_key)
        if not geom_type:
            raise ValueError(f"不支持的 geometryType: {geom_type_key!r}")

        tn = _sql_token(table_name or layer_meta.get("name"))
        if tn == "_":
            raise ValueError("table_name 与 layer_meta['name'] 均为空,无法生成表名")

        # 几何列名: 参数 > geometryField.name > geom
        gf = layer_meta.get("geometryField") or {}
        geom_col = _sql_token(geometry_column or gf.get("name") or "geom")

        # SRID: 参数 > sourceSpatialReference.latestWkid/wkid > extent 内嵌 > 4326
        if srid is None:
            src_sr = layer_meta.get("sourceSpatialReference") or {}
            if not src_sr:
                src_sr = (layer_meta.get("extent") or {}).get("spatialReference") or {}
            srid = src_sr.get("latestWkid") or src_sr.get("wkid") or 4326
        srid = int(srid)

        # 同名表处理
        exists = self.cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tn,)
        ).fetchone()
        if exists:
            if not overwrite:
                raise ValueError(f"表 {tn!r} 已存在,如需重建请传 overwrite=True")
            self._drop_spatial_table(tn, geom_col)
            self.conn.commit()

        # 由 fields 生成列定义;几何列/几何字段不建属性列
        col_defs: list[str] = []
        pk_seen = False
        for f in layer_meta.get("fields") or []:
            fname = f.get("name")
            if not fname or fname.lower() == geom_col.lower():
                continue
            ftype = f.get("type")
            if ftype == "esriFieldTypeGeometry":
                continue
            sql_type = _ESRI_FIELD_TYPE_TO_SQL.get(ftype, "TEXT")
            if ftype == "esriFieldTypeString":
                length = f.get("length")
                sql_type = f"VARCHAR({int(length)})" if length else "TEXT"
            if sql_type.startswith("INTEGER PRIMARY KEY"):
                # 同一元信息里出现多个 OID 时,只留第一个做主键,其余降级为普通整型
                if pk_seen:
                    sql_type = "INTEGER"
                else:
                    pk_seen = True
            col_defs.append(f"{_quote_ident(fname)} {sql_type}")
        if not col_defs:
            # 极少数图层连属性字段都没有;SQLite 不允许 ``CREATE TABLE t()``,
            # 补一个自增主键占位(不影响后续按字段列表插入数据)
            col_defs = ['"_fid" INTEGER PRIMARY KEY AUTOINCREMENT']

        try:
            self.cursor.execute(
                f"CREATE TABLE {_quote_ident(tn)} ({', '.join(col_defs)})"
            )
            self.cursor.execute(
                "SELECT AddGeometryColumn(?, ?, ?, ?, 2)",
                (tn, geom_col, srid, geom_type),
            )
            self.cursor.execute(
                "SELECT CreateSpatialIndex(?, ?)", (tn, geom_col)
            )
            self.conn.commit()
        except Exception:
            # 半成品清理后原样抛出,不留脏表
            self.conn.rollback()
            self._drop_spatial_table(tn, geom_col)
            self.conn.commit()
            raise

        if register:
            self.cursor.execute(
                "DELETE FROM user_relation WHERE table_name = ?", (tn,)
            )
            self.cursor.execute(
                "INSERT INTO user_relation (id, table_name, type) VALUES (?, ?, ?)",
                (str(uuid.uuid4()), tn, "spatial"),
            )
            self.conn.commit()

        return tn

    def batch_add_vector_data(self, table_name: str, data: list[dict]) -> int:
        """批量把要素数据插入到指定的矢量(空间)表。

        每行接受两种形态(自动识别):

        - ArcGIS 要素字典: ``{"attributes": {...}, "geometry": {...}}``
        - 扁平字典: 键为属性列名;几何列(如 ``shape``)的值可以是 WKT
          字符串或 ArcGIS geometry JSON(dict,含 ``rings`` / ``paths`` /
          ``points`` / ``x``+``y``)

        属性值直接按参数绑定(字符串/数字/None/bytes;未知键自动忽略);
        几何经 ``ST_GeomFromText`` 写入表自身的 SRID。几何列声明为多部件
        类型(MULTIPOLYGON 等)时,传入的单部件几何自动 ``ST_Multi`` 包裹;
        声明为单部件类型而传入多部件几何会抛 ValueError——面/线要素多部件
        很常见,建表时建议用 MULTIPOLYGON / MULTILINESTRING / GEOMETRY
        (见 :meth:`create_arcgis_layer` 说明)。ArcGIS 面数据每个 ring 视作
        独立部件,带洞面请先在外部转成规范 WKT。

        Args:
            table_name: 目标表名(:meth:`create_arcgis_layer` 建的表或任意
                含几何列的 spatialite 表)。
            data: 待插入行;整批在一个事务内完成,任一行失败即回滚。

        Returns:
            实际插入的行数(== ``len(data)``)。

        Raises:
            RuntimeError: 在 MBTiles 库上调用。
            ValueError: 表不存在 / 行不是 dict / 几何与列类型不匹配等。
        """
        if self.kind == "mbtiles":
            raise RuntimeError(
                f"{self.db_path} 是 MBTiles 瓦片库,不支持矢量数据插入,"
                "请改用 spatialite 库"
            )
        tn = str(table_name)
        info = self.cursor.execute(
            f"PRAGMA table_info({_quote_ident(tn)})"
        ).fetchall()
        if not info:
            raise ValueError(f"表 {tn!r} 不存在或没有列")
        cols = [row[1] for row in info]
        if not data:
            return 0

        # 几何列元信息(spatialite 注册表;无几何列时按纯属性表插入)
        gmeta = self.cursor.execute(
            "SELECT f_geometry_column AS gc, geometry_type AS gt, srid AS srid "
            "FROM geometry_columns WHERE f_table_name = ?",
            (tn,),
        ).fetchone()
        geom_col: str | None = None
        col_type: str | None = None
        srid: int | None = None
        if gmeta is not None:
            geom_col = gmeta["gc"]
            col_type = _GEOMETRY_CODE_TO_NAME.get(int(gmeta["gt"]), "GEOMETRY")
            srid = int(gmeta["srid"])
        # 属性列(大小写不敏感匹配,几何列除外)
        attr_cols = {
            col.lower(): col
            for col in cols
            if geom_col is None or col.lower() != geom_col.lower()
        }
        col_base = {
            "POINT": "POINT",
            "MULTIPOINT": "POINT",
            "LINESTRING": "LINESTRING",
            "MULTILINESTRING": "LINESTRING",
            "POLYGON": "POLYGON",
            "MULTIPOLYGON": "POLYGON",
        }.get(col_type or "")

        def geom_expr(kind: str) -> str:
            """按几何列类型与输入 WKT 类型决定 SQL 表达式。"""
            kind_base = {
                "POINT": "POINT",
                "MULTIPOINT": "POINT",
                "LINESTRING": "LINESTRING",
                "MULTILINESTRING": "LINESTRING",
                "POLYGON": "POLYGON",
                "MULTIPOLYGON": "POLYGON",
            }.get(kind)
            if col_base and kind_base and col_base != kind_base:
                raise ValueError(
                    f"几何列类型是 {col_type},不能插入 {kind} 几何"
                )
            if col_type in _SINGLE_GEOM_TYPES and kind in _MULTI_GEOM_TYPES:
                raise ValueError(
                    f"几何列是单部件 {col_type},拒绝插入多部件 {kind};"
                    "面/线要素请建表时用 MULTIPOLYGON / MULTILINESTRING "
                    "或 GEOMETRY(见 create_arcgis_layer)"
                )
            if col_type in _MULTI_GEOM_TYPES and kind in _SINGLE_GEOM_TYPES:
                # 多部件列只收多部件;单部件自动 ST_Multi 归一
                return "ST_Multi(ST_GeomFromText(?, ?))"
            return "ST_GeomFromText(?, ?)"

        inserted = 0
        try:
            for i, row in enumerate(data):
                if not isinstance(row, dict):
                    raise ValueError(f"第 {i} 行不是 dict: {row!r}")
                # 识别 ArcGIS 要素包装 或 扁平字典
                if isinstance(row.get("attributes"), dict):
                    attrs = dict(row["attributes"])
                    geom_val = row.get("geometry")
                else:
                    attrs = dict(row)
                    geom_val = None
                    if geom_col:
                        for key in list(attrs):
                            if isinstance(key, str) and key.lower() == geom_col.lower():
                                geom_val = attrs.pop(key)
                                break

                insert_cols: list[str] = []
                params: list[Any] = []
                for key, val in attrs.items():
                    real = attr_cols.get(str(key).lower())
                    if real is None:
                        continue  # 未知键忽略
                    insert_cols.append(real)
                    params.append(val)

                placeholders = ["?"] * len(insert_cols)
                params = list(params)
                geo_expr_sql = ""
                wkt = _to_wkt(geom_val) if geom_val is not None else None
                if geom_col is not None and wkt:
                    # ST_GeomFromText 解析失败会静默返回 NULL(丢数据),先预校验
                    bad = self.cursor.execute(
                        "SELECT ST_GeomFromText(?, ?) IS NULL", (wkt, srid)
                    ).fetchone()[0]
                    if bad:
                        raise ValueError(
                            f"第 {i} 行的几何无法解析为 WKT: {wkt[:60]!r}"
                        )
                    insert_cols.append(geom_col)
                    geo_expr_sql = geom_expr(_wkt_kind(wkt))
                    params.extend((wkt, srid))
                if not insert_cols:
                    raise ValueError(
                        f"第 {i} 行没有任何可写入 {tn!r} 的列"
                        "(未知键已忽略;若目标表是瓦片/影像表请改用 "
                        "batch_add_image_tile_data)"
                    )
                value_sql = placeholders + ([geo_expr_sql] if geo_expr_sql else [])
                sql = (
                    f"INSERT INTO {_quote_ident(tn)} "
                    f"({', '.join(_quote_ident(c) for c in insert_cols)})"
                    f" VALUES ({', '.join(value_sql)})"
                )
                self.cursor.execute(sql, params)
                inserted += 1
            self.conn.commit()
        except Exception as exc:
            self.conn.rollback()
            if isinstance(exc, (ValueError, RuntimeError)):
                raise
            raise ValueError(f"插入矢量数据失败(第 {inserted} 行附近): {exc}") from exc
        return inserted

    # ------------------------------------------------------- MBTiles 栅格瓦片

    def get_arcgis_image_metadata(
        self,
        url: str,
        *,
        timeout: float = 15.0,
        extra_params: dict[str, Any] | None = None,
    ) -> dict:
        """获取 ArcGIS Server 影像/栅格图层的元信息(适配栅格瓦片形式)。

        内部复用 :meth:`get_arcgis_layer_metadata`,额外校验该地址确实是栅格
        图层(MapServer 中 ``type`` 为 ``Raster Layer`` 等)且带 ``extent``;
        校验通过后原样返回元信息 dict,可传给 :meth:`create_arcgis_image_store`
        建 MBTiles 瓦片库。

        Raises:
            ValueError: 地址返回的是矢量/表格/图层组等非影像图层,或缺少 extent。
        """
        meta = self.get_arcgis_layer_metadata(
            url, timeout=timeout, extra_params=extra_params
        )
        ltype = meta.get("type") or ""
        if ltype in ("Feature Layer", "Table", "Group Layer"):
            raise ValueError(
                f"{url} 返回的是 {ltype},不是影像/栅格图层;"
                "矢量图层请改用 get_arcgis_layer_metadata / create_arcgis_layer"
            )
        ext = meta.get("extent") or {}
        if any(ext.get(k) is None for k in ("xmin", "ymin", "xmax", "ymax")):
            raise ValueError(f"{url} 的影像图层元信息缺少 extent,无法确定范围")
        return meta

    def _write_mbtiles_metadata(self, rows: dict[str, str]) -> None:
        """事务内整批写入 metadata(同名 key 先删后插,实现覆盖)。"""
        self._ensure_mbtiles_schema()
        for key in rows:
            self.cursor.execute("DELETE FROM metadata WHERE name = ?", (key,))
        for key, value in rows.items():
            self.cursor.execute(
                "INSERT INTO metadata (name, value) VALUES (?, ?)",
                (key, str(value)),
            )
        self.conn.commit()

    def set_mbtiles_metadata(self, key: str, value: str | None) -> None:
        """写/覆盖一条 MBTiles ``metadata`` 行;``value=None`` 表示删除该 key。"""
        if value is None:
            self._ensure_mbtiles_schema()
            self.cursor.execute("DELETE FROM metadata WHERE name = ?", (key,))
            self.conn.commit()
        else:
            self._write_mbtiles_metadata({key: value})

    def get_mbtiles_metadata(self) -> dict[str, str]:
        """读取整个 MBTiles ``metadata`` 表为 dict。"""
        self._ensure_mbtiles_schema()
        rows = self.cursor.execute("SELECT name, value FROM metadata")
        return {row[0]: row[1] for row in rows}

    def create_arcgis_image_store(
        self,
        layer_meta: dict,
        *,
        table_name: str | None = None,
        min_zoom: int = 0,
        max_zoom: int = 18,
        tile_format: str = "png",
        bounds: tuple[float, float, float, float] | None = None,
        register: bool = True,
        overwrite: bool = False,
    ) -> dict[str, str]:
        """按 ArcGIS 影像图层元信息创建一张 MBTiles 形式的瓦片数据表。

        数据表名以 ``layer_meta["name"]``(图层名)清洗生成为准,也可用
        ``table_name`` 覆盖;表结构与 MBTiles 瓦片表一致(``zoom_level`` /
        ``tile_column`` / ``tile_row`` + ``tile_data`` BLOB,含三重唯一索引),
        建表后即可用常规 SQL 写入瓦片 PNG/JPG 二进制。影像的名称、范围、
        层级等信息写入库内的全局 ``metadata`` 键值表(另附 ``arcgis_*``
        前缀自定义键,记录回源地址/源 wkid/源 extent)。

        注意: ``metadata`` 每库只有一份,适合一个库放一个影像图层;若同一库
        保存多个影像,各图层数据表相互独立(互不覆盖),但 ``metadata`` 只保留
        最近一次写入的图层信息。

        Args:
            layer_meta: 影像图层元信息(:meth:`get_arcgis_image_metadata` 返回值,
                兼容 :meth:`get_arcgis_layer_metadata` 的结果)。
            table_name: 数据表名,缺省由 ``layer_meta["name"]`` 清洗生成
                (保留中文/字母/数字/下划线,其余折成 ``_``)。
            min_zoom / max_zoom: 计划存储的瓦片层级范围(写进 metadata)。
            tile_format: 瓦片图片格式,写进 metadata(png / jpg / webp ...)。
            bounds: WGS84 经纬度范围 ``(min_lon, min_lat, max_lon, max_lat)``。
                缺省由元信息 extent 推导: 源为经纬度坐标系直接采用,Web 墨卡托
                反投影换算;其他投影必须显式传入(否则报错)。
            register: 是否在 ``user_relation`` 登记一行(type=image,表名与数据表
                同名),使影像进入 :meth:`query_all_table_name` 的影像清单;
                纯 ``.mbtiles`` 库没有 user_relation,自动忽略。
            overwrite: 同名数据表已存在时先删旧表再重建;默认 ``False`` 抛异常。

        Returns:
            摘要 dict: 实际建成的数据表名、图层名、格式、层级、bounds 等。

        Raises:
            ValueError: 传入的是矢量图层元信息 / 缺少名称或范围 / 参数非法。
        """
        ltype = layer_meta.get("type") or ""
        if ltype in ("Feature Layer", "Table"):
            raise ValueError(
                f"{ltype} 不是影像图层,应改用 create_arcgis_layer 建矢量表"
            )
        name = layer_meta.get("name") or ""
        if not name:
            raise ValueError("影像图层元信息缺少 name")
        if bounds is None:
            bounds = _extent_to_wgs84_bounds(layer_meta)
        if bounds is None:
            raise ValueError(
                "无法从元信息推导 WGS84 范围: 源不是经纬度/Web 墨卡托坐标系,"
                "请显式传 bounds="
            )
        min_zoom, max_zoom = int(min_zoom), int(max_zoom)
        if not 0 <= min_zoom <= max_zoom:
            raise ValueError(f"非法层级范围: min_zoom={min_zoom}, max_zoom={max_zoom}")
        tile_format = re.sub(r"[^a-z0-9]", "", (tile_format or "png").lower())
        if not tile_format:
            tile_format = "png"

        # 数据表名: 参数 > 图层名(与 user_relation 注册名保持一致)
        tn = _sql_token(table_name or name)
        if tn == "_":
            raise ValueError("table_name 与 layer_meta['name'] 均为空,无法生成表名")
        if tn.lower() == "metadata":
            raise ValueError("表名不能是 'metadata'(与元信息表冲突)")

        # 同名表处理(与 create_arcgis_layer 一致)
        exists = self.cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tn,)
        ).fetchone()
        if exists and not overwrite:
            raise ValueError(f"表 {tn!r} 已存在,如需重建请传 overwrite=True")
        if exists:
            self.cursor.execute("DROP TABLE IF EXISTS " + _quote_ident(tn))
        # 建 MBTiles 形式的瓦片数据表(列结构与规范 tiles 表一致)
        self.cursor.execute(
            f"CREATE TABLE {_quote_ident(tn)} ("
            "zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, "
            "tile_data BLOB)"
        )
        self.cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            + _quote_ident(f"idx_{tn}_xyz")
            + f" ON {_quote_ident(tn)} (zoom_level, tile_column, tile_row)"
        )
        self.conn.commit()

        self._ensure_mbtiles_schema()
        ext = layer_meta.get("extent") or {}
        sr = (
            ext.get("spatialReference")
            or layer_meta.get("sourceSpatialReference")
            or {}
        )
        wkid = sr.get("latestWkid") or sr.get("wkid")
        rows: dict[str, str] = {
            # MBTiles 规范标准键(QGIS/GDAL/Leaflet 等按这些键读)
            "name": name,
            "format": tile_format,
            "type": "overlay",
            "version": "1.3",
            "bounds": ",".join(f"{v:.6f}" for v in bounds),
            "minzoom": str(min_zoom),
            "maxzoom": str(max_zoom),
            # 自定义键,供后续按源服务下载/回源瓦片
            "arcgis_layer_type": ltype or "Raster Layer",
        }
        if wkid is not None:
            rows["arcgis_wkid"] = str(wkid)
        url = layer_meta.get("url")
        if url:
            rows["arcgis_url"] = url
        ext_vals = {k: ext.get(k) for k in ("xmin", "ymin", "xmax", "ymax")}
        if any(v is None for v in ext_vals.values()):
            ext_vals = {}
        if ext_vals:
            rows["arcgis_extent"] = json.dumps(
                ext_vals, ensure_ascii=False, separators=(",", ":")
            )
        self._write_mbtiles_metadata(rows)

        registered = False
        if register and self.kind == "spatialite":
            # 与数据表同名登记,保证按注册名可直接打开图层表
            self.add_domain_table(tn, "image")
            registered = True

        return {
            "table": tn,
            "name": name,
            "format": tile_format,
            "minzoom": str(min_zoom),
            "maxzoom": str(max_zoom),
            "bounds": ",".join(f"{v:.6f}" for v in bounds),
            "registered": registered,
        }

    def create_arcgis_image_store_from_url(
        self,
        url: str,
        *,
        table_name: str | None = None,
        min_zoom: int = 0,
        max_zoom: int = 18,
        tile_format: str = "png",
        bounds: tuple[float, float, float, float] | None = None,
        register: bool = True,
        overwrite: bool = False,
        timeout: float = 15.0,
        extra_params: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """抓取影像图层元信息后直接创建瓦片存储(一步完成)。

        等价于 ``get_arcgis_image_metadata`` + :meth:`create_arcgis_image_store`,
        并把源地址一并记入 metadata(键 ``arcgis_url``)。参数含义见上述两方法。
        """
        meta = self.get_arcgis_image_metadata(
            url, timeout=timeout, extra_params=extra_params
        )
        meta["url"] = url
        return self.create_arcgis_image_store(
            meta,
            table_name=table_name,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            tile_format=tile_format,
            bounds=bounds,
            register=register,
            overwrite=overwrite,
        )

    def batch_add_image_tile_data(
        self,
        table_name: str,
        data: list[dict],
        *,
        overwrite: bool = False,
    ) -> int:
        """批量把栅格瓦片数据插入 MBTiles 形式的瓦片表。

        目标表须是 :meth:`create_arcgis_image_store` 建的列结构
        ``zoom_level / tile_column / tile_row / tile_data``,并在 spatialite
        或 MBTiles 库中均可使用。

        - 瓦片表的 ``tile_row`` 按 MBTiles 约定为 TMS 行号(自北向南);
          若手头数据是 XYZ/slippy 的 ``y``(自南向北),请先换算
          ``tile_row = 2**zoom - 1 - y`` 再传入,函数本身不做换算。
        - 每行字典键名大小写不敏感,支持别名:
          ``zoom_level``/``z``、``tile_column``/``x``/``column``、
          ``tile_row``/``y``/``row``、``tile_data``/``data``。
          ``tile_data`` 接受 bytes/bytearray/memoryview;str 按 utf-8 编码;
          其余类型或缺失会报错。字典里多余的键自动忽略。

        Args:
            table_name: 瓦片表名(create_arcgis_image_store 返回的 table)。
            data: 待插入瓦片;整批单事务,失败整体回滚。
            overwrite: 同 ``(zoom_level, tile_column, tile_row)`` 已存在时
                True=覆盖(INSERT OR REPLACE),False=报错并回滚。

        Returns:
            实际写入的行数(== ``len(data)``)。

        Raises:
            ValueError: 表不是瓦片表 / 行缺字段或类型不对 / 重复瓦片键。
        """
        tn = str(table_name)
        info = self.cursor.execute(
            f"PRAGMA table_info({_quote_ident(tn)})"
        ).fetchall()
        if not info:
            raise ValueError(f"表 {tn!r} 不存在")
        cols = {str(row[1]).lower() for row in info}
        need = {"zoom_level", "tile_column", "tile_row", "tile_data"}
        if not need.issubset(cols):
            missing = ", ".join(sorted(need - cols))
            raise ValueError(
                f"表 {tn!r} 不是栅格瓦片表(缺少列: {missing});"
                "影像/栅格瓦片请先用 create_arcgis_image_store 建表,"
                "矢量图层数据请改用 batch_add_vector_data"
            )
        if not data:
            return 0

        z_keys = ("zoom_level", "zoom", "z")
        x_keys = ("tile_column", "column", "col", "x")
        y_keys = ("tile_row", "row", "y")
        d_keys = ("tile_data", "data")

        def pick(keys: tuple[str, ...]) -> Any:
            for k in keys:
                if k in low:
                    return low[k]
            return None

        params: list[tuple[int, int, int, bytes]] = []
        for i, row in enumerate(data):
            if not isinstance(row, dict):
                raise ValueError(f"第 {i} 行不是 dict: {row!r}")
            low = {str(k).lower(): v for k, v in row.items()}
            z, x, y, blob = pick(z_keys), pick(x_keys), pick(y_keys), pick(d_keys)
            if None in (z, x, y):
                raise ValueError(
                    f"第 {i} 行缺少 zoom_level/tile_column/tile_row"
                    "(或其别名 z/x/y) 键"
                )
            for name, val in (("zoom_level", z), ("tile_column", x), ("tile_row", y)):
                if isinstance(val, float) and not val.is_integer():
                    raise ValueError(f"第 {i} 行的 {name} 必须是整数: {val!r}")
            try:
                z, x, y = int(z), int(x), int(y)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"第 {i} 行的 zoom_level/tile_column/tile_row 必须是整数: "
                    f"({z!r}, {x!r}, {y!r})"
                ) from exc
            if min(z, x, y) < 0:
                raise ValueError(
                    f"第 {i} 行瓦片编号不能为负: zoom={z}, column={x}, row={y}"
                )
            if blob is None:
                raise ValueError(f"第 {i} 行缺少 tile_data 键")
            if isinstance(blob, str):
                blob = blob.encode("utf-8")
            if not isinstance(blob, (bytes, bytearray, memoryview)):
                raise ValueError(
                    f"第 {i} 行 tile_data 必须是 bytes/str,实际是 {type(blob).__name__}"
                )
            params.append((z, x, y, bytes(blob)))

        verb = "OR REPLACE " if overwrite else ""
        sql = (
            f"INSERT {verb}INTO {_quote_ident(tn)} "
            "(zoom_level, tile_column, tile_row, tile_data) "
            "VALUES (?, ?, ?, ?)"
        )
        try:
            self.cursor.executemany(sql, params)
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise ValueError(
                "插入瓦片数据失败: 存在重复的瓦片键"
                "(zoom_level, tile_column, tile_row),如需覆盖已存在瓦片"
                "请传 overwrite=True"
            ) from exc
        except Exception as exc:
            self.conn.rollback()
            raise ValueError(f"插入瓦片数据失败: {exc}") from exc
        return len(params)

    def query_all_table_name(self) -> dict[Any, Any]:
        """列出已注册图层: ``spatial`` 矢量空间表 / ``dict`` 字典表 / ``image`` 影像。"""
        s, d, v = [], [], []
        try:
            rows = self.cursor.execute("SELECT table_name,type FROM user_relation")
            for row in rows:
                if row[1] == 'spatial':
                    s.append(row[0])
                elif row[1] == 'dict':
                    d.append(row[0])
                elif row[1] == 'image':
                    v.append(row[0])
        except sqlite3.OperationalError:
            # 老库 / 纯 MBTiles 库没有 user_relation,按空列表返回即可
            pass
        return {"spatial": s, "dict": d, "image": v}


# ---------------------------------------------------------------------------
# 本地库目录/展示辅助(数据管理「本地数据库」用)
# ---------------------------------------------------------------------------

#: 注册类别顺序(树/表展示顺序): 矢量 → 字典 → 影像
_CATEGORY_ORDER: tuple[str, ...] = ("spatial", "dict", "image")
#: 注册类别 -> 展示标签(矢量/字典/影像)
_CATEGORY_LABEL = {"spatial": "矢量", "dict": "字典", "image": "影像"}
#: 注册类别 -> 行数单位(矢量要素 / 字典行 / 影像瓦片)
_CATEGORY_UNIT = {"spatial": "要素", "dict": "条", "image": "瓦片"}


#: SpatiaLite ``geometry_columns.geometry_type`` 整数编码 → 中文简称
#: (数据管理表格「几何类型」列 / 行 tooltip 展示用)
_GEOM_CODE_LABEL: dict[int, str] = {
    0: "几何",
    1: "点",
    2: "线",
    3: "面",
    4: "多点",
    5: "多线",
    6: "多面",
    7: "几何集合",
}


def _crs_display(srid: int | None, rec: tuple | None) -> str | None:
    """坐标系展示文案: ``EPSG:4326 · WGS 84``。

    rec 为 spatial_ref_sys 行 (srid, auth_name, auth_srid, ref_sys_name);
    无 auth 记录时退化为 ``SRID <n>``; 都读不到返回 None。
    """
    code: str | None = None
    if rec is not None:
        auth_name, auth_srid = rec[1], rec[2]
        if auth_name and auth_srid is not None:
            code = f"{auth_name}:{auth_srid}".upper()
    if code is None and srid is not None:
        code = f"SRID {srid}"
    parts = [code] if code else []
    if rec is not None and rec[3]:
        parts.append(str(rec[3]))
    return " · ".join(parts) or None


def _table_meta(conn, names: Iterable[str]) -> dict[str, dict]:
    """在已打开的只读连接上批量读取注册表元信息(全程容错)。

    返回 ``{表名: {"geom_label", "geom_col", "srid", "crs", "fields"}}``:
    geom_label 取 geometry_columns 几何类型编码的中文名; crs 为
    spatial_ref_sys 的 ``EPSG:xxxx · 名称`` 文案; fields 为 PRAGMA
    table_info 的 [(列名, 声明类型), ...]。spatial 元数据表缺失 / 表已删 /
    字段读不出都不抛异常, 对应项回落 None / []。
    """
    out: dict[str, dict] = {}
    names = [n for n in names if n]
    if not names:
        return out
    try:
        geom_rows = conn.execute(
            "SELECT f_table_name, f_geometry_column, geometry_type, srid "
            "FROM geometry_columns"
        ).fetchall()
    except sqlite3.Error:
        geom_rows = ()
    gmap: dict[str, tuple] = {}
    for tn, gcol, gcode, gsrid in geom_rows:
        gmap.setdefault(tn, (gcol, gcode, gsrid))

    srids = sorted({int(r[2]) for r in gmap.values() if r[2] is not None})
    crs_rows_map: dict[int, tuple] = {}
    if srids:
        try:
            marks = ",".join("?" * len(srids))
            crs_rows_map = {
                int(r[0]): r
                for r in conn.execute(
                    "SELECT srid, auth_name, auth_srid, ref_sys_name "
                    f"FROM spatial_ref_sys WHERE srid IN ({marks})",
                    srids,
                )
            }
        except sqlite3.Error:
            crs_rows_map = {}

    for n in names:
        gcol = gcode = gsrid = None
        meta = gmap.get(n)
        if meta is not None:
            gcol, gcode, gsrid = meta
        fields: list[tuple[str, str]] = []
        try:
            fields = [
                (str(r[1]), str(r[2]))
                for r in conn.execute(f"PRAGMA table_info({_quote_ident(n)})")
            ]
        except sqlite3.Error:
            fields = []
        srid_i = int(gsrid) if gsrid is not None else None
        crs_rec = crs_rows_map.get(srid_i) if srid_i is not None else None
        out[n] = {
            "geom_label": (
                _GEOM_CODE_LABEL.get(gcode) if gcode is not None else None
            ),
            "geom_col": gcol,
            "srid": srid_i,
            "crs": _crs_display(srid_i, crs_rec),
            "fields": fields,
        }
    return out


def describe_local_db(db_path: str | Path) -> dict[str, Any]:
    """打开现有本地库, 返回三类已注册图层目录与行数/元信息(供数据管理树/表展示)。

    类别表名清单即 ``local_db_util.query_all_table_name()`` 的原始输出
    (按 user_relation 注册顺序); counts 为逐表 ``COUNT(*)`` 的行数, 表已删/
    打不开的表记 ``None``; meta 为逐表图层元信息(见 :func:`_table_meta`,
    空间元数据缺失时几何/坐标系回落 None)。整个过程只读, 不会创建文件 /
    写入任何内容。

    Args:
        db_path: 已存在的 sqlite / spatialite 库文件路径。

    Returns:
        ``{"categories": {"spatial": [...], "dict": [...], "image": [...]},
        "counts": {表名: 行数|None}, "meta": {表名: 元信息 dict}}``。

    Raises:
        ValueError: 文件不存在 / 不是有效的 SQLite 数据库。
    """
    p = Path(db_path)
    if not p.is_file():
        raise ValueError(f"文件不存在: {p}")
    try:
        db = local_db_util(p)
        try:
            cats = db.query_all_table_name()
        finally:
            db.conn.close()
    except sqlite3.Error as exc:
        raise ValueError(
            f"不是有效的 SQLite 数据库文件: {p.name}"
        ) from exc

    names = [n for key in _CATEGORY_ORDER for n in cats.get(key, [])]
    counts: dict[str, Any] = {}
    meta: dict[str, dict] = {}
    if names:
        try:
            conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
            try:
                for n in names:
                    try:
                        counts[n] = conn.execute(
                            f"SELECT COUNT(*) FROM {_quote_ident(n)}"
                        ).fetchone()[0]
                    except sqlite3.Error:
                        counts[n] = None
                meta = _table_meta(conn, names)
            finally:
                conn.close()
        except sqlite3.Error:
            # 连只读通道都打不开(锁/权限)时全部标 None, 不让展示失败
            counts = {n: None for n in names}
            meta = {}
    return {"categories": cats, "counts": counts, "meta": meta}


if __name__ == '__main__':
    db = local_db_util(db_path=Path(r"D:\zsh\code\py\qgis_dev\scripts\test_spatial.db"), load_spatialite=True)

    rows = db.cursor.execute('''
        SELECT ST_AsText(shape) wkt,* FROM "江西省行政区划县区级修订版"
    ''')
    for row in rows:
        print(row['wkt'])

    # arcgis 矢量转db
    # layer_metadata = db.get_arcgis_layer_metadata(url="http://127.0.0.1:6080/arcgis/rest/services/%E6%B1%9F%E8%A5%BF%E7%9C%81%E5%8E%BF%E7%BA%A7%E8%A1%8C%E6%94%BF%E5%8C%BA%E5%9F%9F/MapServer/0?f=json")
    # print(layer_metadata)
    # da = db.create_arcgis_layer(layer_meta=layer_metadata)


    # data = [
    #     {
    #         "province":'江西',
    #         "shape":'POLYGON((39333264.830646805 3035233.4949408066,39333261.052075066 3035217.4716991456,39333260.37548449 3035196.7503594984,39333260.586073324 3035175.795277864,39333254.700589456 3035164.9000810245,39333251.25052545 3035158.517064632,39333249.03570831 3035162.9814221174,39333248.151533596 3035163.4368413305,39333247.76491106 3035164.1072324747,39333247.07104266 3035164.0057802354,39333245.9905518 3035164.5747192404,39333245.59491417 3035164.5800825744,39333245.298185945 3035164.5841050837,39333242.928867824 3035164.948799358,39333242.63213961 3035164.952821932,39333242.63213961 3035164.952821932,39333242.5332302 3035164.9541627914,39333240.15489666 3035164.6538297096,39333239.75625383 3035164.4375172663,39333239.65734442 3035164.438858148,39333239.35761099 3035164.221204835,39333239.25870158 3035164.2225457206,39333239.25870158 3035164.2225457206,39333238.36250643 3035163.7912617926,39333236.76342692 3035162.593498362,39333236.76342692 3035162.593498362,39333236.76342692 3035162.593498362,39333236.661512256 3035162.3731633006,39333235.85370792 3035161.1646727533,39333235.63936012 3035159.9481367585,39333235.538948044 3035159.8386396887,39333235.538948044 3035159.8386396887,39333235.538948044 3035159.8386396887,39333235.53744541 3035159.727801705,39333235.519413866 3035158.397745903,39333235.98541134 3035156.285119665,39333236.65674106 3035154.7240015646,39333239.52335242 3035147.2576301023,39333241.119168326 3035140.9170704717,39333242.26525563 3035130.5917073777,39333241.85158624 3035129.2670150474,39333240.40224019 3035124.5197540037,39333239.263145626 3035120.7660122896,39333238.95139058 3035119.6616551266,39333237.30422293 3035114.9170761313,39333235.854873836 3035110.169815537,39333232.91586494 3035104.999316782,39333227.77378716 3035097.8632357856,39333221.37743052 3035093.0721871974,39333215.222469285 3035091.492761058,39333213.362441406 3035085.642487072,39333209.03962749 3035065.968310678,39333201.86192699 3035047.3305701073,39333195.96403183 3035028.232045762,39333195.287345916 3035007.510708656,39333188.20700479 3034988.760795527,39333182.11124506 3034969.6649592025,39333177.39571565 3034950.2178332563,39333172.97540452 3034930.6558469003,39333170.62320429 3034910.400665877,39333164.13474709 3034891.531878292,39333156.167104684 3034873.015728936,39333151.45150855 3034853.5686113974,39333142.30139108 3034835.401081183,39333137.19161722 3034816.0701722833,39333134.34779506 3034796.0433808146,39333128.8423467 3034776.7178411926,39333120.67826691 3034758.3152322858,39333115.370610215 3034738.987014356,39333114.95090315 3034737.2189737344,39333111.69128799 3034737.59576456,39333110.2151121 3034738.1700805626,39333105.99192873 3034740.4445359525,39333103.84140618 3034742.3583009196,39333101.693891495 3034744.493742245,39333101.69539509 3034744.6045802166,39333101.20985246 3034745.2763171652,39333100.14737257 3034747.1753228274,39333099.95556137 3034747.62135839,39333099.76074296 3034747.845718011,39333099.76074296 3034747.845718011,39333097.9204938 3034750.7530003977,39333096.75308759 3034752.2099963725,39333094.989197694 3034753.4533677846,39333093.31670255 3034754.141207696,39333088.88216868 3034755.4208114245,39333084.4356056 3034755.8137129648,39333081.556609035 3034755.0767644183,39333081.25836694 3034754.969952404,39333079.66673984 3034754.3263964634,39333078.1695139 3034753.3489847626,39333077.770855136 3034753.1326768636,39333077.57152575 3034753.0245229187,39333075.36987924 3034751.1698018396,39333070.85263264 3034746.3533230214,39333062.96447774 3034733.711638752,39333062.66021921 3034733.1614752244,39333060.320551075 3034728.426311143,39333056.759333216 3034721.157979849,39333056.04738997 3034719.7264812924,39333055.84354787 3034719.2858137167,39333057.15568558 3034735.785909449,39333058.42305232 3034756.2775152144,39333061.8589749 3034776.185403889,39333067.36302656 3034795.4000808657,39333072.27659726 3034814.8444883954,39333080.539632715 3034833.245726259,39333090.47663027 3034851.0699655525,39333099.03635317 3034869.4671882014,39333111.141837165 3034886.707730617,39333124.626036204 3034903.486147762,39333130.03253876 3034922.8130277684,39333136.323214136 3034941.6844831803,39333138.182417236 3034962.0572070656,39333141.61817797 3034981.9651138335,39333146.13743834 3035001.5257502263,39333152.330650404 3035020.5093920114,39333157.04620176 3035039.9565107403,39333163.14197487 3035059.0523365727,39333167.85749887 3035078.499458796,39333175.33196757 3035097.1331578353,39333183.65121936 3035105.1130001196,39333185.003182605 3035109.972434874,39333184.67969001 3035115.2980237985,39333183.45999748 3035120.1923326193,39333184.52424877 3035125.7208192693,39333185.876210034 3035130.5802541166,39333187.32557699 3035135.3275098,39333190.254078306 3035139.722132496,39333191.703442745 3035144.469388611,39333193.84367006 3035149.0964181386,39333196.376527995 3035153.49640708,39333199.02031734 3035158.7817592323,39333199.6708864 3035162.9855558383,39333200.895658374 3035173.0570603027,39333198.813410945 3035194.5918211113,39333202.83948523 3035214.270019288,39333211.00318954 3035232.672671968,39333223.207100965 3035249.911965537,39333233.305347465 3035265.0735081984,39333238.88303158 3035260.5635540667,39333261.21909761 3035237.0913630943,39333264.830646805 3035233.4949408066))'
    #     }
    # ]
    # db.batch_add_vector_data(table_name="江西省行政区划县区级修订版",data=data)

    # data = [
    #     {
    #         "zoom_level": 1,
    #         "tile_column": 1,
    #         "tile_row": 1,
    #         "tile_data": '123',
    #     }
    # ]
    # db.batch_add_image_tile_data(table_name='_3605新余市2024年第一季度2米影像_tif', data=data)


    # arcgis iamgeserver 转db

    # image_meta = db.get_arcgis_image_metadata(url="http://127.0.0.1:6080/arcgis/rest/services/test_image/MapServer/0?f=json")
    #
    # layer = db.create_arcgis_image_store(layer_meta=image_meta)
    # print(layer)

