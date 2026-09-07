'''
    本地sqlite数据创建管理工具
'''

import json
import math
import re
import uuid
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
        if load_spatialite and self.kind == "spatialite":
            # 扩展不可用时静默忽略——纯 sqlite / 编译期禁用了扩展加载都属正常
            try:
                self.conn.enable_load_extension(True)
                self.conn.load_extension("mod_spatialite")
                self.has_spatialite = True
            except (sqlite3.OperationalError, AttributeError):
                pass

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

if __name__ == '__main__':
    db = local_db_util(db_path=Path(r"D:\zsh\code\py\qgis_dev\scripts\test_spatial.db"), load_spatialite=True)

    # rows = db.cursor.execute('''
    #     SELECT ST_AsText(shape) wkt,* FROM "江西省行政区划县区级修订版"
    # ''')
    # for row in rows:
    #     print(row['wkt'])

    # arcgis 矢量转db
    # layer_metadata = db.get_arcgis_layer_metadata(url="http://127.0.0.1:6080/arcgis/rest/services/%E6%B1%9F%E8%A5%BF%E7%9C%81%E5%8E%BF%E7%BA%A7%E8%A1%8C%E6%94%BF%E5%8C%BA%E5%9F%9F/MapServer/0?f=json")
    # print(layer_metadata)
    # da = db.create_arcgis_layer(layer_meta=layer_metadata)


    # data = [
    #     {
    #         "province":'江西',
    #         "shape":'Polygon ((115.31397212500007754 27.41940809400006174, 115.3139361620001182 27.41926328100004184, 115.31393233400001463 27.41907640700003412, 115.31393730600007075 27.41888726500002349, 115.31387904400003208 27.41878830700005665, 115.31384490800007825 27.41873032600005899, 115.31382208200011519 27.41876966600005971, 115.31381297100006122 27.41877378000003773, 115.31380884400005016 27.41878032100004248, 115.31380153400004929 27.41877894400005289, 115.31379095600004803 27.41878372000007857, 115.31378668500008189 27.41878415200005747, 115.31378421900001285 27.418784401000039, 115.31375969300006545 27.41878687900003797, 115.31375730900003873 27.41878665100006174, 115.31375724400004401 27.41878664500006835, 115.31375646200001484 27.41878657000006569, 115.31373237400009657 27.41878427200003898, 115.31372751300011714 27.41878206700005194, 115.31372740700010127 27.41878201800005854, 115.31372373400006381 27.4187803520000557, 115.3137232880000056 27.41878015000003188, 115.31372271400005047 27.41877988900006358, 115.31371381500002826 27.41877585100007764, 115.31369844100004229 27.41876525200007109, 115.31369818400003169 27.41876486600006047, 115.31369801600010305 27.4187646130000644, 115.31369714000004478 27.41876329500007614, 115.31368945200006237 27.41875173000005361, 115.31368671800009906 27.41874127500005898, 115.31368648700004087 27.41874039400005358, 115.31368640200003028 27.41874006700004429, 115.31368627600011223 27.41873958600007199, 115.31368608900004347 27.41873887100007323, 115.3136859450000884 27.41872669500003212, 115.31369054000003871 27.41870803000006163, 115.31369767900002898 27.41869368800007578, 115.31372777000001406 27.41862727700004143, 115.31374463700001343 27.41856969900004515, 115.31375842600004944 27.4184765350000248, 115.31375443600006747 27.41846474100003661, 115.31373995800004195 27.41842150700006187, 115.31372925500011206 27.41838842800007114, 115.31372593100002177 27.41837815700006331, 115.31371017200001461 27.41833525300006613, 115.31369552000001022 27.41829206300002397, 115.31366659600007551 27.41824537000007922, 115.31361564400003772 27.41818010200006484, 115.31355168900006447 27.41813619500004506, 115.31349025500003336 27.41812118400002873, 115.31347218500002327 27.41806782700007261, 115.31343146600011096 27.41789046000002372, 115.31336064000004171 27.41772085200005904, 115.31330407100006141 27.41754757000006748, 115.3132996260001164 27.41736085500002673, 115.31323051600008966 27.41719080500007522, 115.31317230500008009 27.41701794600004405, 115.31312657500006935 27.41684187000004158, 115.31308508400002211 27.41666470300003766, 115.31306449800001701 27.41648214700006747, 115.31300071200007551 27.41631072600006291, 115.31292276700003185 27.41614295200002971, 115.3128781230000186 27.41596659700007876, 115.31278825800006871 27.41580189500007236, 115.31273852800006807 27.41562685100007002, 115.31271254800003589 27.41544568600005505, 115.3126598040000772 27.41527141800003164, 115.3125799360000201 27.4151041410000289, 115.31252887200002988 27.41492944000003718, 115.31252484100002675 27.4149125020000497, 115.31249184400007834 27.41491592800002763, 115.31247666100000515 27.41492067800004406, 115.31243375700000797 27.41494145600006505, 115.31241205400010585 27.41495765500002335, 115.3123903960000689 27.41497737800006007, 115.31238952200010317 27.41497817400005488, 115.31238450600005763 27.41498420600004238, 115.31237421900004847 27.41500121000007084, 115.31237160700004551 27.41500484800002369, 115.31236994000005325 27.41500717000002396, 115.31236973200009288 27.41500745900003722, 115.31235123300007217 27.41503322200003367, 115.31233901200005221 27.41504593500002329, 115.31232106400011617 27.41505650500005231, 115.31230399800006126 27.41506329200007031, 115.31225858600009815 27.41507447100002537, 115.31221350300006634 27.41507690600002434, 115.3121851850000894 27.41507025700002487, 115.31218209300004673 27.41506903900005909, 115.31216580400007388 27.41506261900002528, 115.31215060800002448 27.41505404500003351, 115.31214683600001081 27.41505191700002797, 115.31214456600002904 27.41505063600004632, 115.31212267900002644 27.41503390200006152, 115.31207823600004758 27.4149900360000629, 115.31199962100004086 27.41487511400003996, 115.31199669000011454 27.41486960900004988, 115.31197426100004577 27.41482749100003957, 115.31193868000002567 27.4147606770000607, 115.31193207700005132 27.41474828000002617, 115.31192995900005371 27.41474430300007725, 115.31194051700003911 27.41489320100004079, 115.31195053500005088 27.41507848000003378, 115.311983400000031 27.41525786900007233, 115.31203592900010335 27.41543219300007195, 115.31208323400005611 27.41560786300004793, 115.31216365100010535 27.41577499800007445, 115.31226218500000869 27.41593746600005943, 115.31234563800001069 27.4161038200000462, 115.31246594100002767 27.41626067800007149, 115.31260048600006485 27.41641386600002761, 115.31265170900007888 27.4165885250000656, 115.31271349800010739 27.41676046200007022, 115.31272886200008543 27.41694436300002735, 115.31276092100006281 27.41712396200006197, 115.31280411600005209 27.41730069000004733, 115.31286379000005127 27.41747317200002954, 115.31290894300002492 27.41764939600005846, 115.31296762600004513 27.41782213300007243, 115.31301329700011138 27.41799822400002995, 115.31308604800005924 27.41816733600006728, 115.31316949700010355 27.41823976100005211, 115.31318156200006797 27.4182836170000428, 115.31317756600003577 27.41833161200003133, 115.3131654390000449 27.41837595400005512, 115.31317490000003545 27.41842622900003335, 115.31318835300010051 27.41846972800004778, 115.31320165500005714 27.41851326400006883, 115.31323125900007653 27.41855260000005501, 115.31324502800009668 27.4185960180000734, 115.31326555200007533 27.4186376930000506, 115.31329061000008096 27.41867820100003428, 115.31331667500001004 27.41872608500005981, 115.31332303300007425 27.41876377600004844, 115.31333441600008882 27.41885477200003152, 115.31330951900008586 27.41904904900002293, 115.3133480580000878 27.41922697800004016, 115.31342777200006822 27.41939429500007464, 115.31354931500004568 27.41955083400006288, 115.31364920000009988 27.41968865900003038, 115.31370620900008817 27.41964947200006009, 115.31393545400010225 27.41943979600006287, 115.31397212500007754 27.41940809400006174))'
    #     }
    # ]
    # db.batch_add_vector_data(table_name="江西省行政区划县区级修订版",data=data)

    data = [
        {
            "zoom_level": 1,
            "tile_column": 1,
            "tile_row": 1,
            "tile_data": '123',
        }
    ]
    db.batch_add_image_tile_data(table_name='_3605新余市2024年第一季度2米影像_tif', data=data)


    # arcgis iamgeserver 转db

    # image_meta = db.get_arcgis_image_metadata(url="http://127.0.0.1:6080/arcgis/rest/services/test_image/MapServer/0?f=json")
    #
    # layer = db.create_arcgis_image_store(layer_meta=image_meta)
    # print(layer)

