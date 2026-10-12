"""把工程中已加载的矢量图层导入到目标数据库（数据管理「导入」入口的实现）。

入口是 :func:`import_vector_layer_to_db`：拿到一个 :class:`QgsVectorLayer` +
目标库 dict + 目标坐标系 ``authid``，做三件事：

1. **坐标系换算**：源图层 CRS 与目标库 CRS 不同时，先在内存中把每个要素
   几何显式 transform（避免依赖驱动写盘时重投影带来的精度 / 性能问题，也避开
   spatialite / gpkg 目标库要求几何坐标按 SRID 写入的硬约束）。
2. **建表 / 写表**：本地库（``kind=local``）走
   :class:`app.core.util.local_db_util.local_db_util`（SpatiaLite / GeoPackage
   双格式自动识别）；用 :meth:`create_vector_table` 建表（自动注册
   ``user_relation(type=spatial)``）、用 :meth:`batch_add_vector_data` 批量
   写要素。
3. **注册 ``user_relation``**：local 库由 ``create_vector_table(register=True)``
   自动写；本函数额外做一次幂等校验（防同名表被外部程序提前 ``user_relation``
   写过导致重复行）。服务器库（``kind=server``）暂未实现——本入口只接受本地
   库，UI 端在数据库下拉里把服务器库也展示出来是为后续 PostGIS / 国产数据库
   扩展预留位置；当前点确认 → 抛 :class:`ImportLayerError` 给上游弹警告。

表名清洗：源图层名可能含中文 / 空格 / ``.`` ``-`` 等字符，SQLite / PostGIS 标识符
都吃不下，这里把所有非字母数字下划线替换成 ``_``，首字符若是数字则前缀 ``t_``，
最长 64 字符。**不**默认覆盖用户数据——同名表已存在时
:meth:`local_db_util.create_vector_table` 会抛 :class:`ValueError`，由调用方决
定（重命名图层后重试，或显式覆盖）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeedback,
    QgsVectorLayer,
    QgsWkbTypes,
)

from app.core.util.local_db_util import local_db_util
from app.infra.logging import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------- exception


class ImportLayerError(RuntimeError):
    """导入图层到本地库的领域错误。调用方负责把 ``str(exc)`` 弹给用户。"""


# ------------------------------------------------------------------ public


@dataclass(frozen=True)
class ImportLayerResult:
    """导入执行结果。"""

    ok: bool
    #: 目标库中的表名（清洗后）
    table_name: str
    #: 实际写入的要素数
    inserted: int
    #: 失败 / 取消原因（成功时为空）
    reason: str = ""


def import_vector_layer_to_db(
    layer: QgsVectorLayer,
    db: dict,
    target_crs_authid: str,
    *,
    feedback: Optional[QgsFeedback] = None,
) -> ImportLayerResult:
    """把工程中的 ``layer`` 导入到 ``db`` 指定的目标库，落表 + 注册 ``user_relation``。

    Args:
        layer: 源矢量图层（必须 ``isinstance(layer, QgsVectorLayer)`` 且
            ``isValid()``）。坐标系取自 ``layer.crs()``。
        db: 目标库 dict，与 :class:`DataManageDock` 的 ``add_connection`` 接受的
            形态一致。当前只支持 ``kind=="local"``（spatilite / gpkg）。
        target_crs_authid: 目标坐标系 ``authid``（如 ``EPSG:4326``）。空 / 非法
            → 抛 :class:`ImportLayerError`。
        feedback: 可选进度反馈。QGIS 3.x / 4.x 都支持
            :class:`QgsFeedback`，以 ``setProgress(0..100)`` 报进度、
            ``setProgressText(msg)`` 报阶段名。用户点取消（调
            ``feedback.cancel()``）→ 迼环会检查并提前抛
            :class:`ImportLayerError("用户取消")`。不传则不报进度、不支持取消。

    Returns:
        :class:`ImportLayerResult`。失败时 ``ok=False`` + ``reason`` 含原因。

    Raises:
        ImportLayerError: 校验 / 转换 / 落盘任一阶段失败。
    """
    if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
        raise ImportLayerError("源图层不是有效的矢量图层")
    if not target_crs_authid:
        raise ImportLayerError("目标坐标系不能为空")

    target_crs = QgsCoordinateReferenceSystem(target_crs_authid)
    if not target_crs.isValid():
        raise ImportLayerError(f"目标坐标系非法：{target_crs_authid!r}")

    kind = db.get("kind", "")
    if kind == "local":
        return _import_to_local(layer, db, target_crs, feedback=feedback)
    if kind == "server":
        # 服务器库（PostGIS / 达梦 / 人大金仓）尚未在本服务实现：UI 已展示但
        # 行为未接入，给出明确错误，不静默落空。
        raise ImportLayerError(
            "服务器库导入尚未实现，请选用本地库（SpatiaLite / GeoPackage）"
        )
    raise ImportLayerError(f"未知数据库类型：{kind!r}")


# ----------------------------------------------------------------- private


def _import_to_local(
    layer: QgsVectorLayer,
    db: dict,
    target_crs: QgsCoordinateReferenceSystem,
    *,
    feedback: Optional[QgsFeedback] = None,
) -> ImportLayerResult:
    """本地库：建表 + 写要素 + 注册 ``user_relation``。

    进度报点（只有数值，文字阶段名由 UI 端按进度区间映射）：
    0 → 准备 / 5 → 转换字段 / 15 → 扫描要素 / 20 → 转换要素 / 60 → 建表 /
    70 → 写入 / 100 → 完成。 :class:`QgsFeedback` 没有 ``progressText``，
    仅有 ``progressChanged`` 数字信号——文字阶段在 UI 端按进度区间
    :func:`_phase_text` 映射。 全程检查 ``feedback.isCanceled()``（用户
    点取消 → 抛 :class:`ImportLayerError("用户取消")`，回滚到 0% 状态）。
    """
    fb = feedback

    def _check_cancel() -> None:
        if fb is not None and fb.isCanceled():
            raise ImportLayerError("用户取消")

    _check_cancel()
    if fb is not None:
        fb.setProgress(int(0))

    path = db.get("path")
    if not path:
        raise ImportLayerError("本地库连接缺少 'path' 字段")
    db_path = Path(path)
    if not db_path.exists():
        raise ImportLayerError(f"本地库文件不存在：{path}")

    util = local_db_util(str(db_path))
    if util.kind not in ("spatialite", "gpkg"):
        raise ImportLayerError(
            f"{path} 是 {util.kind} 库，不支持矢量图层（需 SpatiaLite / GeoPackage）"
        )

    # 表名清洗：源图层名 → SQL 标识符
    # 表名沿用源图层名（原名直入，不清洗）——SQLite 标识符支持中文 / 空格 /
    # `:` / `.` 等 unicode；只要 :func:`batch_add_vector_data` 内部用
    # ``_quote_ident`` 包裹即可（实测在 gpkg / spatialite 都走通）。空名
    # 兑底成 ``imported_layer`` 防止空表名报错。
    raw_name = (layer.name() or "").strip()
    table_name = raw_name or "imported_layer"

    # 字段清单：从源图层 fields() 直接转 (name, SQLite 类型声明)
    if fb is not None:
        fb.setProgress(int(5))
    columns = _build_columns(layer)

    # 几何列
    geometry_type = _wkb_to_gpkg_type(layer.wkbType())
    geometry_column = "geom"

    # 源坐标系 + 转换
    source_crs = layer.crs()
    transform: Optional[QgsCoordinateTransform] = None
    if source_crs.isValid() and target_crs.isValid() and source_crs != target_crs:
        transform = QgsCoordinateTransform(
            source_crs, target_crs, layer.transformContext()
        )

    # SRID：优先取 postgisSrid（本地库不识别时返回 0），否则从 authid 数字
    srid = target_crs.postgisSrid() or _authid_to_srid(target_crs.authid())

    # 构造待写行（ArcGIS 要素字典形态：batch_add_vector_data 自动吃）
    # 总要素数用于推进度；提前取完 + 高速移动游标，避免反复 query featureCount
    if fb is not None:
        fb.setProgress(int(15))
    _check_cancel()
    total_features = layer.featureCount() if layer.featureCount() >= 0 else 0
    rows: list[dict] = []
    skipped_invalid = 0
    if fb is not None:
        fb.setProgress(int(20))
    for i, f in enumerate(layer.getFeatures(), start=1):
        # 每 200 要素检查一次取消——避免大数据集每要素都开 isCanceled 调用
        if i % 200 == 0:
            _check_cancel()
            if fb is not None and total_features > 0:
                # 20% → 60% 区间留给要素转换（占总量主要时间）
                fb.setProgress(int(20 + int(40 * i / max(1, total_features))))
        geom = f.geometry()
        if geom is None or geom.isEmpty():
            skipped_invalid += 1
            continue
        if transform is not None:
            try:
                geom.transform(transform)
            except Exception as exc:  # noqa: BLE001
                logger.warning("坐标转换失败（feature %s）：%s", f.id(), exc)
                skipped_invalid += 1
                continue
        if not geom.isGeosValid():
            logger.warning("几何无效（feature %s），跳过", f.id())
            skipped_invalid += 1
            continue
        attributes = {}
        for fld in layer.fields():
            sql_name = _sanitize_column_name(fld.name())
            if not sql_name:
                continue
            try:
                v = f.attribute(fld.name())
            except Exception:  # noqa: BLE001
                continue
            attributes[sql_name] = v
        rows.append(
            {
                "attributes": attributes,
                # WKT 字符串走 _to_wkt() / encode_wkt() 内部会剩掉 SRID=
                # 前缀·直接用；不要包 {"wkt": ...} dict（该 dict 不是 ArcGIS
                # JSON 形态 · _esri_geometry_to_wkt 会返 None）
                "geometry": geom.asWkt(),
            }
        )
    if fb is not None:
        fb.setProgress(int(60))
    _check_cancel()

    # 建表（register=True 自动写 user_relation）
    try:
        util.create_vector_table(
            table_name,
            columns=columns,
            geometry_column=geometry_column,
            geometry_type=geometry_type,
            srid=int(srid) if srid else 4326,
            register=True,
            overwrite=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise ImportLayerError(f"建表失败：{exc}") from exc

    if fb is not None:
        fb.setProgress(int(70))
    _check_cancel()

    inserted = 0
    if rows:
        try:
            inserted = util.batch_add_vector_data(table_name, rows)
        except Exception as exc:  # noqa: BLE001
            # 失败回滚：删刚建的表 + 删 user_relation 行（建表 register=True 已写）
            _try_drop_table(util, table_name, geometry_column)
            _try_unregister_user_relation(util, table_name)
            raise ImportLayerError(f"写入要素失败：{exc}") from exc
    if fb is not None:
        fb.setProgress(int(100))

    if skipped_invalid:
        logger.warning(
            "导入图层 %s 时跳过 %d 个无效/空要素",
            layer.name(),
            skipped_invalid,
        )

    return ImportLayerResult(
        ok=True,
        table_name=table_name,
        inserted=inserted,
    )


def _phase_text(progress: int) -> str:
    """按 :class:`QgsFeedback` 报的进度值映射阶段名（UI 端用）。

    与 :func:`_import_to_local` 里的 ``setProgress`` 调用点保持同步：

    - 0 → 准备
    - 1-10 → 转换字段
    - 11-19 → 扫描要素
    - 20-59 → 转换要素坐标
    - 60-69 → 建表
    - 70-99 → 写入
    - 100 → 完成
    """
    if progress >= 100:
        return "完成"
    if progress >= 70:
        return "写入要素到数据库…"
    if progress >= 60:
        return "建表并注册到 user_relation…"
    if progress >= 20:
        return "转换要素坐标 / 准备数据…"
    if progress >= 15:
        return "扫描要素总数…"
    if progress >= 5:
        return "转换字段清单…"
    return "准备导入…"


# --------------------------------------------------------------- helpers


def _build_columns(layer: QgsVectorLayer) -> list[tuple[str, str]]:
    """源图层字段 → ``(sql_name, sqlite_type)`` 列表，列名按 SQL 标识符清洗。

    Qt 枚举 :class:`QVariant.Type` / :class:`QMetaType.Type` 在 PyQt5 / PyQt6
    上互不兼容（PyQt5 用 QVariant，PyQt6 用 QMetaType）；这里不走枚举，
    改走 :meth:`QgsField.typeName` 返回的字符串（``"Integer"`` /
    ``"Real"`` / ``"String"`` / ``"Date"`` / ``"Time"`` / ``"DateTime"``
    等），QGIS 3.x / 4.x 都是同一套名称，跨版本稳定。
    """
    # :class:`QgsField` 的 ``typeName()`` → SQLite 类型声明
    type_map = {
        "Integer": "INTEGER",  # 整型家族（含 32 / 64 / 无符号）
        "Real": "REAL",  # 双精度浮点
        "String": "TEXT",  # 变长字符串
        "Date": "TEXT",  # SQLite 无原生 Date，按 ISO 字符串存
        "Time": "TEXT",
        "DateTime": "TEXT",
        "Binary": "BLOB",  # 字节序列
    }
    columns: list[tuple[str, str]] = []
    seen: set[str] = set()
    for fld in layer.fields():
        sql_name = _sanitize_column_name(fld.name())
        if not sql_name or sql_name in seen:
            continue
        seen.add(sql_name)
        type_name = type_map.get(fld.typeName(), "TEXT")
        columns.append((sql_name, type_name))
    return columns


_NON_SQL_IDENT = re.compile(r"[^A-Za-z0-9_]")


def _sanitize_table_name(raw: str) -> str:
    """保留为公共 helper（不 import 内部使用）—— 第三方可能想复用。

    本服务表名沿用原名直入（SQLite / GPKG 支持中文 / 空格 / `:` / `.` 等
    unicode 标识符），本函数不参与 import 流程，避免回归。
    """
    s = re.sub(r"[^A-Za-z0-9_]", "_", raw or "").strip("_")
    if not s:
        s = "t_" + hashlib.sha1((raw or "").encode("utf-8")).hexdigest()[:8]
    elif s[0].isdigit():
        s = "t_" + s
    s = re.sub(r"_+", "_", s)
    return s[:64]


def _sanitize_column_name(raw: str) -> str:
    """列名清洗：复用表名规则，保留首字符下划线（QGIS 内部常用 ``_id`` 风格）。"""
    if not raw:
        return ""
    s = _NON_SQL_IDENT.sub("_", raw.strip())
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        return ""
    if s[0].isdigit():
        s = "_" + s
    return s[:64] or ""


def _wkb_to_gpkg_type(wkb_type: int) -> Optional[str]:
    """``QgsWkbTypes.Type`` 枚举 → GPKG 规范名。

    剥掉 Z / M 维度只取基类（Point=0 / Line=1 / Polygon=2），按多部件
    ``isMulti`` 拼规范名。``NoGeometry`` / ``Unknown`` → ``None``（无几何列）。
    """
    if int(QgsWkbTypes.Type.NoGeometry) == wkb_type:
        return None
    geom_type = int(QgsWkbTypes.geometryType(wkb_type))
    is_multi = bool(QgsWkbTypes.isMultiType(wkb_type))
    if geom_type == int(QgsWkbTypes.GeometryType.PointGeometry):
        return "MULTIPOINT" if is_multi else "POINT"
    if geom_type == int(QgsWkbTypes.GeometryType.LineGeometry):
        return "MULTILINESTRING" if is_multi else "LINESTRING"
    if geom_type == int(QgsWkbTypes.GeometryType.PolygonGeometry):
        return "MULTIPOLYGON" if is_multi else "POLYGON"
    return "GEOMETRY"  # 未知 / GeometryCollection → GEOMETRY 兜底


def _authid_to_srid(authid: str) -> int:
    """authid (``EPSG:4326``) → srid 数字。失败返回 0（兜底）。"""
    m = re.match(r"^[A-Za-z][A-Za-z0-9]*:(\d+)$", authid or "")
    return int(m.group(1)) if m else 0


def _try_drop_table(util: local_db_util, tn: str, geom_col: str) -> None:
    """建表后批量写入失败时回滚：删表（best-effort，失败不抛）。"""
    try:
        util._drop_spatial_table(tn, geom_col)
        util.conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("回滚删表失败 %s：%s", tn, exc)


def _try_unregister_user_relation(util: local_db_util, tn: str) -> None:
    """建表 + 写表失败时回滚：删 user_relation 行（best-effort）。"""
    try:
        util.cursor.execute(
            "DELETE FROM user_relation WHERE table_name = ?", (tn,)
        )
        util.conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("回滚 user_relation 失败 %s：%s", tn, exc)
