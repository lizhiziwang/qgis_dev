"""GeoPackage(1.3 规范)几何二进制编码 — 纯标准库,不依赖 GDAL / mod_spatialite。

GPKG 几何列存的是: 魔数 ``GP`` + 版本(1B) + flags(1B) + srs_id(LE int32)
+ [可选 envelope] + WKB。本模块只做 2D 编码, 并按 GDAL/OGR 写出的
"标准/常规" GeoPackage 几何来落库:

- flags=0x03: bit0=1(头字段小端) + bits1-3=1(带 XY envelope)——
  与 GDAL 一致; envelope 为 4 个 little-endian double
  ``minx, maxx, miny, maxy``。不能图省事只写 flags(如 0x00 / 0x01):
  0x00 会被当成大端 header, srs_id 读错, 部分环境甚至判几何损坏
  ("Unable to read geometry");
- WKB 一律小端;
- 空几何 / ZM / 三维: 统一不支持,抛 ``ValueError``(与现有 spatialite
  路径 ``ST_GeomFromText(...) IS NULL`` 报错语义对齐)。

输入形态与 :mod:`app.core.util.local_db_util` 矢量写入现状对齐:
- ArcGIS geometry JSON(dict): ``rings`` / ``paths`` / ``points`` / ``x``+``y``
  —— 多 ring 的 Polygon JSON 沿用老行为: 每个 ring 视为独立部件
  (带洞面请先转成规范 WKT 再传);
- WKT 字符串(允许 ``SRID=n;`` 前缀;只支持 POINT / LINESTRING / POLYGON /
  MULTIPOINT / MULTILINESTRING / MULTIPOLYGON,其中 POLYGON 的多 ring 按
  规范语义视为一个带洞多边形)。

对外提供:
- ``encode_esri_json(geom, srs_id)`` / ``encode_wkt(wkt, srs_id)``:
  → ``(BLOB bytes, 规范类型名)``(无几何内容返回 ``None``);
- ``wrap_to_multi(blob, kind)``: 单部件 → 对应 MULTI 部件(已是 MULTI 原样);
- ``blob_kind_name(blob)`` / ``blob_xy_envelope(blob)``: 反读校验/调试;
- ``decompress_blob(blob)``: 把早期私有压缩几何还原成规范 GPKG 几何。

早期本工程曾把几何做 XOR 差分 + zlib 压缩并置 flags bit5
(``FLAG_COMPRESSED``)。这是**私有格式**, QGIS/GDAL 读不了, 现已废弃:
本模块只写规范几何, ``decompress_blob`` 仅用于修复存量压缩库。
"""
from __future__ import annotations

import re
import struct
import zlib
from typing import Any

#: 规范几何类型名 → WKB 类型码
_GEOM_CODE: dict[str, int] = {
    "POINT": 1,
    "LINESTRING": 2,
    "POLYGON": 3,
    "MULTIPOINT": 4,
    "MULTILINESTRING": 5,
    "MULTIPOLYGON": 6,
}
_CODE_TO_GEOM: dict[int, str] = {v: k for k, v in _GEOM_CODE.items()}

_SINGLE = {"POINT", "LINESTRING", "POLYGON"}
_MULTI = {"MULTIPOINT", "MULTILINESTRING", "MULTIPOLYGON"}
#: 单部件 → 对应多部件
_SINGLE_TO_MULTI = {
    "POINT": "MULTIPOINT",
    "LINESTRING": "MULTILINESTRING",
    "POLYGON": "MULTIPOLYGON",
}

_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


class GpkgGeomError(ValueError):
    """GPKG 几何编码失败(输入不是几何 / WKT 无法解析 / 类型不支持)。"""


# ----------------------------------------------------------------------
# WKB 构件(小端)
# ----------------------------------------------------------------------


def _wkb(code: int, payload: bytes) -> bytes:
    return b"\x01" + struct.pack("<I", code) + payload


def _pt(x: float, y: float) -> bytes:
    return struct.pack("<dd", float(x), float(y))


def _pt_seq(pts: list[tuple[float, float]]) -> bytes:
    return struct.pack("<I", len(pts)) + b"".join(_pt(x, y) for x, y in pts)


def _point_wkb(x: float, y: float) -> bytes:
    return _wkb(_GEOM_CODE["POINT"], _pt(x, y))


def _line_wkb(pts: list[tuple[float, float]]) -> bytes:
    return _wkb(_GEOM_CODE["LINESTRING"], _pt_seq(pts))


def _poly_wkb(rings: list[list[tuple[float, float]]]) -> bytes:
    body = struct.pack("<I", len(rings)) + b"".join(_pt_seq(r) for r in rings)
    return _wkb(_GEOM_CODE["POLYGON"], body)


def _multi_wkb(kind: str, members: list[bytes]) -> bytes:
    """把若干 member 的**完整 WKB**(含各自字节序头)包成 MULTI* 几何。"""
    return _wkb(_GEOM_CODE[kind], struct.pack("<I", len(members)) + b"".join(members))


#: envelope 指示位(bits1-3) → envelope 字节数(bit0 是字节序, 不参与)
_ENVELOPE_LEN: dict[int, int] = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


def _wkb_offset(blob: bytes) -> int:
    """从 GPKG 几何头反推 WKB 起始偏移(按 envelope 指示位算)。"""
    return 8 + _ENVELOPE_LEN.get((blob[3] >> 1) & 0x07, 0)


def _blob(srs_id: int, wkb: bytes) -> bytes:
    """GPKG 几何 BLOB: 'GP' + 版本 0 + flags 0x03 + srs_id + XY envelope + WKB。

    按 GDAL/OGR 的标准写法落库: flags=0x03(头小端 + 带 XY envelope),
    envelope 为 4 个 little-endian double ``minx, maxx, miny, maxy``。
    **不要**只写 flags=0x00 —— 规范里 bit0=0 表示大端 header, srs_id 会被
    读错, 部分环境(实测 QGIS 4.2 / GDAL 3.13)还连带判几何损坏。
    理论上算不出包络时退回 flags=0x01(小端、不带 envelope)保证仍可读。
    """
    srs_le = struct.pack("<i", int(srs_id))
    env = _wkb_envelope(wkb)
    if env is None:
        return b"GP" + b"\x00" + b"\x01" + srs_le + wkb
    minx, miny, maxx, maxy = env
    return (
        b"GP" + b"\x00" + b"\x03" + srs_le
        + struct.pack("<dddd", minx, maxx, miny, maxy)
        + wkb
    )


# ----------------------------------------------------------------------
# 存量修复: 解压早期私有压缩几何(现已废弃, 只读旧库时需要)
# ----------------------------------------------------------------------

#: flags bit5: 早期本工程“几何差分压缩”私有标记。规范里 bit5 未使用,
#: QGIS/GDAL 不认 —— 打开这类库会报 ``Unable to read geometry``。
FLAG_COMPRESSED = 0x20


def gpkg_header_len(blob: bytes) -> int | None:
    """GPKG 几何头字节数(含 envelope, 不含 WKB): 8/40/56/56/72;非法返回 None。"""
    if (
        not isinstance(blob, (bytes, bytearray))
        or len(blob) < 8
        or bytes(blob[:2]) != b"GP"
    ):
        return None
    return 8 + _ENVELOPE_LEN.get((blob[3] >> 1) & 0x07, 0)


def is_compressed_blob(blob: bytes) -> bool:
    """是否为早期私有压缩几何(BLOB 魔数 ``GP`` 且 flags bit5=1)。"""
    return (
        isinstance(blob, (bytes, bytearray))
        and len(blob) >= 8
        and bytes(blob[:2]) == b"GP"
        and bool(blob[3] & FLAG_COMPRESSED)
    )


def _coord_offsets(wkb: bytes) -> list[int]:
    """遍历 WKB 结构, 返回每个坐标对(16B)的起始偏移。

    只按结构字节走(结构未被压缩改动), 供 :func:`_xor_delta` 反解。支持
    POINT/LINESTRING/POLYGON 及 MULTI*/GEOMETRYCOLLECTION 递归; 解析不出或
    长度对不上抛 :class:`GpkgGeomError`。
    """
    offsets: list[int] = []

    def walk(pos: int) -> int:
        if pos + 5 > len(wkb):
            raise GpkgGeomError("WKB 结构截断")
        fmt = "<" if wkb[pos] == 1 else ">"
        code = struct.unpack_from(fmt + "I", wkb, pos + 1)[0]
        pos += 5
        if code == 1:  # POINT
            offsets.append(pos)
            return pos + 16
        if code in (2, 3):  # LINESTRING / POLYGON
            groups = 1 if code == 2 else struct.unpack_from(fmt + "I", wkb, pos)[0]
            if code == 3:
                pos += 4
            for _ in range(groups):
                (n,) = struct.unpack_from(fmt + "I", wkb, pos)
                pos += 4
                for _ in range(n):
                    offsets.append(pos)
                    pos += 16
            return pos
        if code in (4, 5, 6, 7):  # MULTI* / GEOMETRYCOLLECTION
            (n,) = struct.unpack_from(fmt + "I", wkb, pos)
            pos += 4
            for _ in range(n):
                pos = walk(pos)
            return pos
        raise GpkgGeomError(f"不支持的 WKB 类型码 {code}")

    if walk(0) != len(wkb):
        raise GpkgGeomError("WKB 长度与结构不一致")
    return offsets


def _xor_delta(raw: bytes, offsets: list[int], *, decode: bool) -> bytes:
    """坐标差分的编/解码(首坐标 XOR 0 保持不变)。

    压缩: ``out_i = orig_i XOR orig_{i-1}``; 还原: ``orig_i = out_i XOR
    orig_{i-1}``。两者靠 ``prev`` 取“原值”还是“还原值”区分 —— ``decode=False``
    (压缩)时 prev 取读到的原值, ``decode=True``(解压)时取还原值。
    """
    buf = bytearray(raw)
    prev_x = prev_y = 0
    for off in offsets:
        ix = struct.unpack_from("<Q", buf, off)[0]
        iy = struct.unpack_from("<Q", buf, off + 8)[0]
        x = ix ^ prev_x
        y = iy ^ prev_y
        struct.pack_into("<QQ", buf, off, x, y)
        prev_x, prev_y = (x, y) if decode else (ix, iy)
    return bytes(buf)


def decompress_blob(blob: bytes) -> bytes:
    """把早期私有压缩几何还原为规范 GPKG 几何(未压缩的原样返回)。

    还原后清掉 flags bit5(压缩标记); 若头里没 envelope, 再把 bit0 对齐成
    WKB 实际字节序(本工程写的小端 WKB → flags bit0=1), 顺带修掉老版
    flags=0x00 被当大端 header 读的毛病。

    Raises:
        GpkgGeomError: 头非法 / zlib 解压失败 / WKB 结构解析不出。
    """
    blob = bytes(blob)
    if not is_compressed_blob(blob):
        return blob
    hlen = gpkg_header_len(blob)
    if hlen is None or hlen >= len(blob):
        raise GpkgGeomError("压缩几何 BLOB 头非法")
    try:
        raw = zlib.decompress(blob[hlen:])
    except zlib.error as exc:
        raise GpkgGeomError(f"压缩几何 zlib 解压失败: {exc}") from exc
    try:
        coords = _coord_offsets(raw)
    except (struct.error, IndexError) as exc:
        raise GpkgGeomError("压缩几何 WKB 结构无法解析") from exc
    wkb = _xor_delta(raw, coords, decode=True)
    head = bytearray(blob[:hlen])
    flags = head[3] & ~FLAG_COMPRESSED
    if ((head[3] >> 1) & 0x07) == 0:  # 无 envelope 时才动头字节序位
        # 对齐 WKB 实际字节序(本工程写小端 WKB → bit0=1),
        # 顺带修掉老版 flags=0x00 被当大端 header 读的毛病。
        flags = (flags & 0xFE) | (0x01 if wkb[:1] == b"\x01" else 0x00)
    head[3] = flags
    return bytes(head) + wkb


def _xy2(pt: Any) -> tuple[float, float] | None:
    """点结构 → (x, y): 接受 [x, y, ...] / (x, y) / {'x':..,'y':..}。"""
    if isinstance(pt, (list, tuple)):
        if len(pt) < 2:
            return None
        try:
            return float(pt[0]), float(pt[1])
        except (TypeError, ValueError):
            return None
    if isinstance(pt, dict):
        x, y = pt.get("x"), pt.get("y")
        if x is None or y is None:
            return None
        try:
            return float(x), float(y)
        except (TypeError, ValueError):
            return None
    return None


def _xy_seq(pts: list) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for p in pts:
        xy = _xy2(p)
        if xy is None:
            raise GpkgGeomError(f"无法解析坐标点: {p!r}")
        out.append(xy)
    return out


# ----------------------------------------------------------------------
# ArcGIS geometry JSON → GPKG BLOB
# ----------------------------------------------------------------------


def encode_esri_json(geom: dict, srs_id: int) -> tuple[bytes, str] | None:
    """ArcGIS 要素 geometry JSON → ``(BLOB, 规范类型名)``;空几何返回 None。

    支持 ``rings``(Polygon)/ ``paths``(Polyline)/ ``points``(Multipoint)/
    ``x``+``y``(Point)。只取 2D。多 ring 的 Polygon 每个 ring 作为独立部件
    (与 spatialite 老路径的 WKT 语义一致)。
    """
    if not isinstance(geom, dict):
        raise GpkgGeomError(f"几何必须是 dict(ArcGIS JSON), 实际 {type(geom).__name__}")

    rings = geom.get("rings")
    if rings is not None:
        parts = [_poly_wkb([_xy_seq(r)]) for r in rings]
        parts = [p for p in parts if p is not None]
        if not parts:
            return None
        kind = "MULTIPOLYGON" if len(parts) > 1 else "POLYGON"
        return _blob(srs_id, _multi_wkb(kind, parts) if len(parts) > 1 else parts[0]), kind

    paths = geom.get("paths")
    if paths is not None:
        lines = []
        for p in paths:
            xy = _xy_seq(p)
            if len(xy) < 2:
                raise GpkgGeomError("Polyline 段至少需要 2 个点")
            lines.append(_line_wkb(xy))
        if not lines:
            return None
        if len(lines) == 1:
            return _blob(srs_id, lines[0]), "LINESTRING"
        return _blob(srs_id, _multi_wkb("MULTILINESTRING", lines)), "MULTILINESTRING"

    points = geom.get("points")
    if points is not None:
        pts = _xy_seq(points)
        if not pts:
            return None
        if len(pts) == 1:
            return _blob(srs_id, _point_wkb(*pts[0])), "POINT"
        return _blob(srs_id, _multi_wkb("MULTIPOINT", [_point_wkb(x, y) for x, y in pts])), "MULTIPOINT"

    if geom.get("x") is not None or geom.get("y") is not None:
        xy = _xy2(geom)
        if xy is None:
            return None
        return _blob(srs_id, _point_wkb(*xy)), "POINT"
    return None


# ----------------------------------------------------------------------
# WKT → GPKG BLOB
# ----------------------------------------------------------------------


def _strip_outer(s: str) -> str:
    """剥掉字符串最外层一对圆括号(不剥则原样返回)。"""
    s = s.strip()
    if s.startswith("(") and s.endswith(")"):
        depth = 0
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    return s
        return s[1:-1].strip()
    return s


def _split_top(s: str) -> list[str]:
    """按顶层逗号拆分(括号内的逗号不拆)。"""
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def _nums(text: str) -> list[float]:
    vals = [float(m) for m in _NUM_RE.findall(text)]
    if len(vals) % 2:
        raise GpkgGeomError(f"坐标点缺少偶数个数值: {text!r}")
    return vals


def _wkt_xy_pairs(text: str) -> list[tuple[float, float]]:
    vals = _nums(text)
    return list(zip(vals[0::2], vals[1::2]))


def _parse_polygon(body: str) -> list[bytes]:
    """WKT POLYGON 主体 ``((ring),(ring))`` → 各 ring 的完整 POLYGON WKB 的
    内部结构按单带洞多边形处理? 见注释。"""
    # POLYGON 主体 = 多个 ring, ring0 外环、其余内环 → 一个 POLYGON(带洞)
    inner = _strip_outer(body)
    rings_text = _split_top(inner) if inner.startswith("(") else ([inner] if inner else [])
    rings: list[list[tuple[float, float]]] = []
    for rt in rings_text:
        pts = _wkt_xy_pairs(_strip_outer(rt))
        if len(pts) < 4 or pts[0] != pts[-1]:
            raise GpkgGeomError("Polygon ring 至少 4 点且首尾闭合")
        rings.append(pts)
    return [_poly_wkb(rings)]


def _parse_multi_polygon(body: str) -> list[bytes]:
    """WKT MULTIPOLYGON 主体 ``(((ring)),((ring)))`` → 每个子多边形一块。"""
    inner = _strip_outer(body)
    polys = _split_top(inner)  # 每个子多边形主体, 形如 "((ring))"
    if not polys:
        raise GpkgGeomError("MULTIPOLYGON 为空")
    out: list[bytes] = []
    for p in polys:
        part = _parse_polygon(p)
        out.extend(part)
    return out


def encode_wkt(wkt: str, srs_id: int) -> tuple[bytes, str]:
    """WKT 字符串 → ``(BLOB, 规范类型名)``。EMPTY / 不支持的族抛错。

    内部把 WKT 统一解析成: POINT / LINESTRING / POLYGON(ring0 外环、
    其余内环,即带洞面)/ MULTIPOINT / MULTILINESTRING / MULTIPOLYGON,
    与 GPKG 列声明的类型一一对应。
    """
    text = re.sub(r"^\s*SRID\s*=\s*\d+\s*;\s*", "", str(wkt).strip(), flags=re.I)
    if not text:
        raise GpkgGeomError("空 WKT")
    m = re.match(r"^\s*([A-Za-z]+)\s*(.*)$", text, flags=re.S)
    if not m:
        raise GpkgGeomError(f"WKT 无法解析: {wkt[:60]!r}")
    kind = m.group(1).upper()
    rest = m.group(2).strip()
    if rest.upper() == "EMPTY" or not rest:
        raise GpkgGeomError(f"暂不支持 EMPTY 几何: {kind}")

    if kind == "POINT":
        pts = _wkt_xy_pairs(_strip_outer(rest))
        if len(pts) != 1:
            raise GpkgGeomError(f"POINT 需要恰好 1 个坐标: {wkt[:60]!r}")
        return _blob(srs_id, _point_wkb(*pts[0])), "POINT"

    if kind == "LINESTRING":
        pts = _wkt_xy_pairs(_strip_outer(rest))
        if len(pts) < 2:
            raise GpkgGeomError("LINESTRING 至少需要 2 个点")
        return _blob(srs_id, _line_wkb(pts)), "LINESTRING"

    if kind == "POLYGON":
        return _blob(srs_id, _parse_polygon(rest)[0]), "POLYGON"

    if kind == "MULTIPOINT":
        inner = _strip_outer(rest)
        groups = _split_top(inner)
        if not groups:
            raise GpkgGeomError("MULTIPOINT 为空")
        pts = [
            _wkt_xy_pairs(_strip_outer(g))[0] if g.startswith("(") else _wkt_xy_pairs(g)[0]
            for g in groups
        ]
        return _blob(srs_id, _multi_wkb("MULTIPOINT", [_point_wkb(x, y) for x, y in pts])), "MULTIPOINT"

    if kind == "MULTILINESTRING":
        inner = _strip_outer(rest)
        lines = [_line_wkb(_wkt_xy_pairs(_strip_outer(g))) for g in _split_top(inner)]
        if not lines:
            raise GpkgGeomError("MULTILINESTRING 为空")
        return _blob(srs_id, _multi_wkb("MULTILINESTRING", lines)), "MULTILINESTRING"

    if kind == "MULTIPOLYGON":
        parts = _parse_multi_polygon(rest)
        return _blob(srs_id, _multi_wkb("MULTIPOLYGON", parts)), "MULTIPOLYGON"

    if kind == "GEOMETRYCOLLECTION":
        raise GpkgGeomError("暂不支持 GEOMETRYCOLLECTION 几何")
    raise GpkgGeomError(f"不支持的 WKT 几何类型: {kind}")


# ----------------------------------------------------------------------
# 统一入口 / 工具
# ----------------------------------------------------------------------


def encode_geometry(value: Any, srs_id: int) -> tuple[bytes, str] | None:
    """把行内几何值统一编码: str 按 WKT, dict 按 ArcGIS JSON;None 原样返回。"""
    if value is None:
        return None
    if isinstance(value, str):
        return encode_wkt(value, srs_id)
    if isinstance(value, dict):
        return encode_esri_json(value, srs_id)
    raise GpkgGeomError(f"几何值类型不支持: {type(value).__name__}")


def geometry_kind(value: Any) -> str | None:
    """不编码,仅判输入几何的规范类型名(用于建列前校验)。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = re.sub(r"^\s*SRID\s*=\s*\d+\s*;\s*", "", value.strip(), flags=re.I)
        return (re.split(r"[\s(]", text, maxsplit=1)[0].upper() or None)
    if isinstance(value, dict):
        if value.get("rings") is not None:
            return "MULTIPOLYGON" if len(value["rings"]) > 1 else "POLYGON"
        if value.get("paths") is not None:
            return "MULTILINESTRING" if len(value["paths"]) > 1 else "LINESTRING"
        if value.get("points") is not None:
            return "MULTIPOINT" if len(value["points"]) > 1 else "POINT"
        if value.get("x") is not None or value.get("y") is not None:
            return "POINT"
    return None


def wrap_to_multi(blob: bytes, kind: str) -> bytes:
    """单部件 BLOB → 对应 MULTI 部件(保留原 srs);已是 MULTI 原样返回。

    实现层面只在 WKB 段外包一层: ``MULTI*`` 的成员本就是**完整 WKB**(含各自
    字节序头),因此单部件 WKB 原样作唯一成员即可。外层再走 :func:`_blob`
    重算 envelope(原件的 envelope 不再适用)。
    """
    if kind in _MULTI:
        return blob
    if kind not in _SINGLE:
        raise GpkgGeomError(f"无法把 {kind} 包成 MULTI")
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 9 or bytes(blob[:2]) != b"GP":
        raise GpkgGeomError("wrap_to_multi 需要完整的 GPKG 几何 BLOB")
    srs = struct.unpack("<i", bytes(blob[4:8]))[0]
    wkb = bytes(blob[_wkb_offset(blob):])
    code = _GEOM_CODE[_SINGLE_TO_MULTI[kind]]
    return _blob(srs, _wkb(code, struct.pack("<I", 1) + wkb))


def blob_kind_name(blob: bytes) -> str | None:
    """从已编码 BLOB 反读规范几何类型名(兼容早期私有压缩几何)。"""
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 9:
        return None
    if bytes(blob[:2]) != b"GP":
        return None
    try:
        blob = decompress_blob(bytes(blob))
    except GpkgGeomError:
        return None
    wkb = bytes(blob[_wkb_offset(bytes(blob)):])
    if len(wkb) < 5 or wkb[0] not in (0, 1):
        return None
    code = struct.unpack("<I", wkb[1:5])[0]
    return _CODE_TO_GEOM.get(code)


def blob_srs_id(blob: bytes) -> int | None:
    """从已编码 BLOB 反读 srs_id(调试用)。"""
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 8 or bytes(blob[:2]) != b"GP":
        return None
    return struct.unpack("<i", bytes(blob[4:8]))[0]


def blob_xy_envelope(blob: bytes) -> tuple[float, float, float, float] | None:
    """从已编码 BLOB 反读外包络 ``(xmin, ymin, xmax, ymax)``(测试/未来 rtree 用)。

    递归遍历 WKB,取所有坐标的最小/最大。无法解析返回 None。
    """
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 9 or bytes(blob[:2]) != b"GP":
        return None
    try:
        blob = decompress_blob(bytes(blob))
    except GpkgGeomError:
        return None
    wkb = bytes(blob[_wkb_offset(bytes(blob)):])
    try:
        return _wkb_envelope(wkb)
    except (struct.error, IndexError):
        return None


def _wkb_envelope(wkb: bytes) -> tuple[float, float, float, float] | None:
    """带位置游标的递归 WKB 外包络解析(坐标全部进 xs/ys)。"""
    if len(wkb) < 5 or wkb[0] not in (0, 1):
        return None
    xs: list[float] = []
    ys: list[float] = []

    def read_point(pos: int) -> int:
        x, y = struct.unpack_from("<dd", wkb, pos)
        xs.append(x)
        ys.append(y)
        return pos + 16

    def read_seq(pos: int) -> int:
        (n,) = struct.unpack_from("<I", wkb, pos)
        pos += 4
        for _ in range(n):
            pos = read_point(pos)
        return pos

    def parse_ring_group(pos: int, n_rings: int) -> int:
        for _ in range(n_rings):
            pos = read_seq(pos)
        return pos

    def parse_full(pos: int) -> int:
        """解析一个完整 WKB(含字节序头),返回成员结束位置。"""
        if pos + 5 > len(wkb):
            return pos
        code = struct.unpack_from("<I", wkb, pos + 1)[0]
        pos += 5
        if code == 1:  # POINT
            pos = read_point(pos)
        elif code == 2:  # LINESTRING
            pos = read_seq(pos)
        elif code == 3:  # POLYGON
            (n,) = struct.unpack_from("<I", wkb, pos)
            pos = parse_ring_group(pos + 4, n)
        elif code in (4, 5, 6):  # MULTI*
            (n,) = struct.unpack_from("<I", wkb, pos)
            pos += 4
            for _ in range(n):
                pos = parse_full(pos)
        else:
            return pos
        return pos

    try:
        parse_full(0)
    except struct.error:
        return None
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


__all__ = [
    "FLAG_COMPRESSED",
    "GpkgGeomError",
    "blob_kind_name",
    "blob_srs_id",
    "blob_xy_envelope",
    "decompress_blob",
    "encode_esri_json",
    "encode_geometry",
    "encode_wkt",
    "geometry_kind",
    "gpkg_header_len",
    "is_compressed_blob",
    "wrap_to_multi",
]
