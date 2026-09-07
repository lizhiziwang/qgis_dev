"""
    ArcGIS Server 几何 → WKT 文本 工具

    把 ArcGIS Server 要素 ``query`` 返回的 geometry JSON 对象
    (esriGeometryPoint 的 ``x``/``y``、Multipoint 的 ``points``、
    Polyline 的 ``paths``、Polygon 的 ``rings``)转成标准 WKT 文本,
    可直接交给 :meth:`core.util.local_db_util.local_db_util.batch_add_vector_data`
    或任何 ST_GeomFromText 消费方使用。

    - 输出默认 2D;``include_z=True`` 且所有点都带 ``z`` 时输出带 Z 的 WKT
    - 面数据默认按“包含关系”自动把内环归为洞、外环之间切多部件
      (``detect_holes=False`` 则与 local_db_util 内部一致: 每个环视作
      独立部件)
    - 坐标用 repr 保留浮点全精度(4527 等大坐标不被截短)
    - 只依赖标准库,不引入任何第三方包
"""

import re
from typing import Any

_KNOWN_GEOM_KEYS = ("rings", "paths", "points", "x", "y")


def _coord_text(value: Any) -> str:
    """坐标值转文本(保留浮点全精度)。"""
    return repr(value) if isinstance(value, float) else str(value)


def _read_point(pt: Any) -> tuple[Any, Any, Any]:
    """把坐标数组/坐标对象归一成 ``(x, y, z|None)``,不解析类型精度。"""
    if isinstance(pt, dict):
        x, y, z = pt.get("x"), pt.get("y"), pt.get("z")
    else:
        seq = list(pt)
        if len(seq) < 2:
            raise ValueError(f"坐标点数不足,收到: {pt!r}")
        x, y = seq[0], seq[1]
        z = seq[2] if len(seq) > 2 else None
    if x is None or y is None:
        raise ValueError(f"坐标缺少 x/y,收到: {pt!r}")
    return x, y, z


def _normalize_ring(ring: Any, index: int = 0) -> list[tuple[Any, Any, Any]]:
    """一条坐标环 → 坐标点列表;去掉末尾与首点重复的闭合点。

    ArcGIS JSON 的面环通常首尾同点(闭合),而 WKT 规范不要求重复首点,
    故去掉以免 ST_GeomFromText 报错。闭合后不足 3 个不同点时视为坏环。
    """
    pts = [_read_point(p) for p in ring]
    if len(pts) > 1 and pts[0][0] == pts[-1][0] and pts[0][1] == pts[-1][1]:
        pts = pts[:-1]
    if len(pts) < 3:
        raise ValueError(f"第 {index + 1} 条环闭合后不足 3 个点,不是合法面环: {ring!r}")
    return pts


def _fmt_ring(pts: list[tuple[Any, Any, Any]], use_z: bool) -> str:
    """坐标点列表 → WKT 环片段 ``(x y, x y, ...)``。"""
    body = ",".join(
        " ".join(_coord_text(v) for v in _coords_of(pt, use_z)) for pt in pts
    )
    return "(" + body + ")"


def _fmt_part(pts: list[tuple[Any, Any, Any]], use_z: bool) -> str:
    """坐标点列表 → 点部件片段 ``(x y x y)``(Multipoint 部件,点间空格)。"""
    return "(" + " ".join(
        " ".join(_coord_text(v) for v in _coords_of(pt, use_z)) for pt in pts
    ) + ")"


def _coords_of(pt: tuple[Any, Any, Any], use_z: bool) -> list[Any]:
    x, y, z = pt
    return [x, y, z] if use_z else [x, y]


def _keyword(base: str, use_z: bool) -> str:
    """按是否带 Z 拼类型关键字,如 ``POLYGON`` / ``POLYGON Z``。"""
    return f"{base} Z" if use_z else base


def _ring_has_z(pts: list[tuple[Any, Any, Any]]) -> bool:
    """该环/路径上是否每个点都有 z。"""
    return bool(pts) and all(pt[2] is not None for pt in pts)


def _polygon_content(rings_pts: list[list[tuple[Any, Any, Any]]], use_z: bool) -> str:
    """多边形内容段 ``((外环),(洞1),(洞2))``,用于拼 POLYGON / MULTIPOLYGON。"""
    return "(" + ",".join(_fmt_ring(r, use_z) for r in rings_pts) + ")"


# ---------------------------------------------------------------------------
# 面环归类: 外环 / 洞
# ---------------------------------------------------------------------------
def _signed_area2(pts: list[tuple[Any, Any, Any]]) -> float:
    """鞋带公式有向面积(2 倍),只取前两维。"""
    total = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i][0], pts[i][1]
        x2, y2 = pts[(i + 1) % n][0], pts[(i + 1) % n][1]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _point_in_ring(x: float, y: float, pts: list[tuple[Any, Any, Any]]) -> bool:
    """射线法判断点是否在环内(忽略 z)。"""
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i][0], pts[i][1]
        xj, yj = pts[j][0], pts[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _ring_contained_by(inner: list[tuple[Any, Any, Any]], outer: list[tuple[Any, Any, Any]]) -> bool:
    """inner 的所有边中点都落在 outer 内 → inner 视作 outer 的洞。

    取边中点而非顶点,可避开两环只在顶点相触的共享点歧义。
    """
    n = len(inner)
    if n < 3:
        return False
    for i in range(n):
        a = inner[i]
        b = inner[(i + 1) % n]
        mx = (a[0] + b[0]) / 2.0
        my = (a[1] + b[1]) / 2.0
        if not _point_in_ring(mx, my, outer):
            return False
    return True


def _group_polygon_rings(
    rings_pts: list[list[tuple[Any, Any, Any]]],
) -> list[list[int]]:
    """把一组面环按包含关系归组: 返回 ``[[外环idx, 洞idx...], ...]``。

    对每个环找“能整个包含它、且面积最小”的环作为父环;没有父环的是外环,
    父环是外环的直接子环即为洞。环在数组里的先后顺序不影响结果。
    """
    n = len(rings_pts)
    areas = [abs(_signed_area2(r)) for r in rings_pts]
    parent = [-1] * n
    for i in range(n):
        best = -1
        best_area = float("inf")
        for j in range(n):
            if i == j or areas[j] <= areas[i]:
                continue
            if _ring_contained_by(rings_pts[i], rings_pts[j]) and areas[j] < best_area:
                best = j
                best_area = areas[j]
        parent[i] = best
    outer_idx = [i for i in range(n) if parent[i] == -1]
    return [
        [o] + [i for i in range(n) if parent[i] == o]
        for o in outer_idx
    ]


# ---------------------------------------------------------------------------
# 各几何类型 → WKT
# ---------------------------------------------------------------------------
def _polygon_to_wkt(
    rings: Any,
    *,
    include_z: bool,
    detect_holes: bool,
) -> str | None:
    """``rings`` → ``POLYGON`` / ``MULTIPOLYGON`` WKT;空环列表返回 None。"""
    if not rings:
        return None
    parsed = [_normalize_ring(r, i) for i, r in enumerate(rings)]
    use_z = include_z and all(_ring_has_z(r) for r in parsed)
    if detect_holes:
        groups: list[list[int]] = _group_polygon_rings(parsed)
    else:
        groups = [[i] for i in range(len(parsed))]
    contents = [_polygon_content([parsed[i] for i in group], use_z) for group in groups]
    if len(contents) == 1:
        return _keyword("POLYGON", use_z) + " " + contents[0]
    return _keyword("MULTIPOLYGON", use_z) + " (" + ",".join(contents) + ")"


def _line_to_wkt(paths: Any, *, include_z: bool) -> str | None:
    """``paths`` → ``LINESTRING`` / ``MULTILINESTRING`` WKT;空列表返回 None。"""
    if not paths:
        return None
    parsed = [[_read_point(p) for p in path] for path in paths]
    if any(len(p) < 2 for p in parsed):
        raise ValueError(f"路径点数不足 2,收到: {paths!r}")
    use_z = include_z and all(_ring_has_z(p) for p in parsed)
    parts = [_fmt_ring(p, use_z) for p in parsed]
    if len(parts) == 1:
        return _keyword("LINESTRING", use_z) + " " + parts[0]
    return _keyword("MULTILINESTRING", use_z) + " (" + ",".join(parts) + ")"


def _multipoint_to_wkt(points: Any, *, include_z: bool) -> str | None:
    """``points`` → ``POINT`` / ``MULTIPOINT`` WKT;空列表返回 None。"""
    if not points:
        return None
    parsed = [_read_point(p) for p in points]
    use_z = include_z and all(pt[2] is not None for pt in parsed)
    parts = [_fmt_part([pt], use_z) for pt in parsed]
    if len(parts) == 1:
        return _keyword("POINT", use_z) + " " + parts[0]
    return _keyword("MULTIPOINT", use_z) + " (" + ",".join(parts) + ")"


def arcgis_geometry_to_wkt(
    geometry: dict[str, Any],
    *,
    include_z: bool = False,
    detect_holes: bool = True,
) -> str | None:
    """把 ArcGIS Server geometry JSON(字典)转成 WKT 文本。

    Args:
        geometry: ArcGIS 要素的 ``geometry`` 字段值。支持 ``rings``(面)、
            ``paths``(线)、``points``(多点)、``x``+``y``(点);空字典/空
            几何返回 None。
        include_z: 为 True 且每个坐标点都带 ``z`` 时输出带 Z 的 WKT
            (如 ``POLYGON Z (...)``),否则忽略 z 只输出 2D。
        detect_holes: 面数据为 True 时按包含关系把内环自动归为洞、外环
            之间拆 MULTIPOLYGON;为 False 时每个环都是独立部件
            (与 local_db_util 内部环处理语义一致)。

    Returns:
        WKT 文本,或几何为空/无法识别时依情况返回 None / 抛 ValueError。
    """
    if not isinstance(geometry, dict):
        raise ValueError(f"geometry 必须是字典,收到: {type(geometry).__name__}")
    if not geometry:
        return None
    if "rings" in geometry:
        return _polygon_to_wkt(
            geometry["rings"], include_z=include_z, detect_holes=detect_holes
        )
    if "paths" in geometry:
        return _line_to_wkt(geometry["paths"], include_z=include_z)
    if "points" in geometry:
        return _multipoint_to_wkt(geometry["points"], include_z=include_z)
    if geometry.get("x") is not None and geometry.get("y") is not None:
        z = geometry.get("z")
        use_z = include_z and z is not None
        return _keyword("POINT", use_z) + " " + _fmt_ring([(geometry["x"], geometry["y"], z)], use_z)
    raise ValueError(
        f"无法识别的 ArcGIS 几何字典: {geometry!r};"
        "支持 keys: rings / paths / points / x+y"
    )


def to_wkt(
    geometry: Any,
    *,
    include_z: bool = False,
    detect_holes: bool = True,
) -> str | None:
    """通用几何 → WKT 入口。

    - ``None`` / 空几何 → None
    - ``str``: 已是 WKT 文本 → 剥掉 ``SRID=n;`` 前缀并按单行规整返回
      (ArcGIS Server 个别接口会把几何以 WKT 字符串返回)
    - ``dict``: 按 :func:`arcgis_geometry_to_wkt` 处理

    Returns:
        WKT 文本,空几何返回 None,无法识别抛 ValueError。
    """
    if geometry is None:
        return None
    if isinstance(geometry, str):
        text = re.sub(r"^\s*SRID\s*=\s*\d+\s*;\s*", "", geometry.strip(), flags=re.I)
        text = " ".join(text.split())
        return text or None
    if isinstance(geometry, dict):
        return arcgis_geometry_to_wkt(
            geometry, include_z=include_z, detect_holes=detect_holes
        )
    raise ValueError(f"无法识别的几何类型: {type(geometry).__name__}")


def _gj_pt(pt: Any) -> tuple[Any, Any, Any]:
    """GeoJSON 坐标点(数组) → ``(x, y, z|None)``。"""
    seq = list(pt)
    if len(seq) < 2:
        raise ValueError(f"GeoJSON 坐标点数不足,收到: {pt!r}")
    return seq[0], seq[1], (seq[2] if len(seq) > 2 else None)


def _gj_flat_pts(parts: Any) -> list[tuple[Any, Any, Any]]:
    """把任意深度的 GeoJSON ``coordinates`` 展平为坐标点列表(算 z 一致性用)。"""
    out: list[tuple[Any, Any, Any]] = []
    stack = [parts]
    while stack:
        node = stack.pop()
        if node and isinstance(node[0], (int, float)):
            out.append(_gj_pt(node))
        elif isinstance(node, list):
            stack.extend(node)
    return out


def geojson_geometry_to_wkt(
    geometry: dict[str, Any] | None,
    *,
    include_z: bool = False,
    detect_holes: bool = True,
) -> str | None:
    """GeoJSON 几何(RFC 7946 字典) → WKT 文本。

    支持 ``Point`` / ``MultiPoint`` / ``LineString`` / ``MultiLineString`` /
    ``Polygon`` / ``MultiPolygon`` / ``GeometryCollection``。坐标按原样输出
    (GeoJSON 约定为经度在前,不在此处做轴序转换)。面数据坐标本身就是
    ``[外环, 洞1, ...]`` 顺序,不依赖包含关系推断。

    Args:
        geometry: GeoJSON 几何对象,None 返回 None。
        include_z: 为 True 且所有坐标点都有第 3 维时才输出带 Z 的 WKT。
        detect_holes: 保留以与 :func:`arcgis_geometry_to_wkt` 签名一致;
            GeoJSON 的环顺序已明确,此参数不影响结果。

    Returns:
        WKT 文本或 None。
    """
    if geometry is None:
        return None
    if not isinstance(geometry, dict):
        raise ValueError(f"GeoJSON 几何必须是字典,收到: {type(geometry).__name__}")
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not isinstance(gtype, str):
        raise ValueError(f"GeoJSON 几何缺少 type,收到: {geometry!r}")

    all_pts = _gj_flat_pts(coords) if coords else []
    use_z = include_z and bool(all_pts) and all(pt[2] is not None for pt in all_pts)

    def pts_of(data: Any) -> list[tuple[Any, Any, Any]]:
        return [_gj_pt(p) for p in data]

    if gtype == "Point":
        return _keyword("POINT", use_z) + " " + _fmt_ring(pts_of([coords]), use_z)
    if gtype == "LineString":
        return _keyword("LINESTRING", use_z) + " " + _fmt_ring(pts_of(coords), use_z)
    if gtype == "Polygon":
        rings = [pts_of(r) for r in coords]
        return _keyword("POLYGON", use_z) + " " + _polygon_content(rings, use_z)
    if gtype in ("MultiPoint", "MultiLineString"):
        parts = [
            _fmt_ring(pts_of([c]), use_z) if gtype == "MultiPoint" else _fmt_ring(pts_of(c), use_z)
            for c in coords
        ]
        base = "MULTIPOINT" if gtype == "MultiPoint" else "MULTILINESTRING"
        if len(parts) == 1:
            return _keyword("POINT" if gtype == "MultiPoint" else "LINESTRING", use_z) + " " + parts[0]
        return _keyword(base, use_z) + " (" + ",".join(parts) + ")"
    if gtype == "MultiPolygon":
        contents = [_polygon_content([pts_of(r) for r in poly], use_z) for poly in coords]
        if len(contents) == 1:
            return _keyword("POLYGON", use_z) + " " + contents[0]
        return _keyword("MULTIPOLYGON", use_z) + " (" + ",".join(contents) + ")"
    if gtype == "GeometryCollection":
        parts = [
            geojson_geometry_to_wkt(g, include_z=include_z) or ""
            for g in (geometry.get("geometries") or [])
        ]
        parts = [p for p in parts if p]
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        gc_z = include_z and all(" Z " in p for p in parts)
        return _keyword("GEOMETRYCOLLECTION", gc_z) + " (" + ",".join(parts) + ")"
    raise ValueError(f"无法识别的 GeoJSON 类型: {gtype!r}")


__all__ = ["arcgis_geometry_to_wkt", "geojson_geometry_to_wkt", "to_wkt"]


if __name__ == '__main__':
    arcgis_txt = {'rings': []}
    wkt = to_wkt(geometry=arcgis_txt)
    print(wkt)
