"""补缝（相邻面之间的**封闭**拓扑间隙修补）领域。

地块 / 宗地这类面数据在采集或编辑后，常在多个相邻面之间留下没有被任何面
覆盖的空洞。本模块只做**纯几何**：从输入几何并集中找出这些缺口，并给出把
缺口并入某个目标面的新几何。图层读写、编辑命令、属性重算由 UI 层负责
（:mod:`app.ui.tools.gap_repair`），本模块不得 import ``app.ui``。

判定思路：

0. **先把并集按极小尺度重结点化**。两个面只在**一个点**上相接时，
   ``unaryUnion`` 会给「多部件 + 0 内环」——口袋状的缝隙在视觉上被完全围住，
   拓扑上却不算内环，用户会报「明明有缝隙却识别不出来」。极小 ``d`` 的
   闭运算（见 :func:`_seal_degenerate_touches`）把这种退化接触补实，
   随后的内环判定才看得见它。
1. **候选缺口 = 并集的内环**。重结点化之后，内环按定义就是被完全
   包围的空洞，因此「只补封闭缝隙」由结构本身满足——开口到外边界的不构成
   内环，无需额外判断。
2. **内环 ≠ 缺口**。单个面自带的内环（湖、塘、天井）同样是内环，必须排除。
   判据是**边界由 ≥2 个输入几何共同贡献**：缝隙被相邻面围成，其边界段来自
   不同的面；自有内环的整圈边界只属于它自己。
3. **重复 / 共面选中的同一要素**会让判据 2 误判（两个相同几何各自贡献整圈
   边界、贡献者数为 2），因此再加一条：**任一贡献者的共享边界达到缺口整周长
   即排除**。

已知局限：一个湖若被两个面各占一半（谁都没有把它作为自有内环），拓扑上与
真实的「两块之间的缝」**无法区分**，会被报出。这是算法固有边界，由调用方在
交互层提供「跳过」选项兜底。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from qgis.core import QgsGeometry, QgsLineString, QgsPolygon

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 共享边判定阈值（相对缺口周长）：低于此视为点接触 / 浮点噪声
_SHARED_RATIO = 1e-6

#: 兜底缝合的膨胀宽度（取包围盒短边比例）与圆弧分段数
_STITCH_WIDTH_RATIO = 0.05
_STITCH_SEGMENTS = 8

#: 并集「重结点化」尺度（取输入总包围盒对角线比例）。取值只需大于点接触的
#: 数值宽度、又远小于任何真实几何特征——实测 1e-6 已足够补实点接触，
#: 且夹具面积在 1e-6 处与原始值完全一致（1e-3 起才会出现可见漂移）。
#: 数值薄片下限（相对选中面总包围盒面积）：重结点化 / 求并的浮点残渣会形成
#: 面积趋近 0 的假内环。与 :attr:`Gap.area` 同单位（平方米）后按比例过滤，
#: 真实缝隙远大于这个量级。
_MIN_AREA_RATIO = 1e-9
_SEAL_RATIO = 1e-6
_BUFFER_SEGMENTS = 8


@dataclass(frozen=True)
class Gap:
    """一处封闭缝隙（并集内环围成的面）。"""

    #: 在检测结果中的序号（0 起，供 UI 与用户分配结果对应）
    index: int
    #: 缺口多边形（外环即缺口边界；深拷贝语义，与输入几何无共享）
    geometry: QgsGeometry
    #: 面积（**平方米**；未传 ``crs`` 时为图层 CRS 单位²）。见 :func:`_area_m2`。
    area: float
    #: 参与围成该缺口的**输入几何下标**（升序，长度 ≥ 2）
    contributor_indices: tuple[int, ...]


def _boundary(geom: QgsGeometry) -> QgsGeometry | None:
    """取几何边界。

    本构建的 ``QgsGeometry`` **没有** ``.boundary()``（实测 ``hasattr`` 为
    False），必须走抽象几何 :meth:`QgsAbstractGeometry.boundary`，再 ``clone()``
    后包回 :class:`QgsGeometry` 接管所有权。
    """
    if geom is None or geom.isNull() or geom.isEmpty():
        return None
    try:
        abstract = geom.constGet()
        if abstract is None:
            return None
        boundary = abstract.boundary()
        if boundary is None:
            return None
        return QgsGeometry(boundary.clone())
    except Exception:  # noqa: BLE001 —— GEOS 偶发失败属可预期，退化为「无边界」
        logger.warning("补缝：取几何边界失败", exc_info=True)
        return None


def _iter_holes(union: QgsGeometry) -> Iterator[QgsGeometry]:
    """遍历并集的全部内环，逐个包成面几何。

    ``QgsPolygon(QgsLineString(ring.points()))`` 是拷贝语义（点的序列被复制），
    不会触发仓库里记录过的 ``QgsGeometry(part)`` 所有权转移段错误——
    与 :mod:`app.ui.tools.fill_hole` 取内环的写法一致。
    """
    for part in union.constParts():
        count = part.numInteriorRings() if hasattr(part, "numInteriorRings") else 0
        for i in range(count):
            ring = part.interiorRing(i)
            if ring is None:
                continue
            hole = QgsGeometry(QgsPolygon(QgsLineString(ring.points())))
            if hole.isEmpty():
                continue
            yield hole


def _shared_lengths(
    gap: QgsGeometry,
    geometries: Sequence[QgsGeometry],
    *,
    tolerance: float = 0.0,
) -> list[tuple[int, float]]:
    """返回 ``[(几何下标, 与缺口边界贴合的长度), ...]``，只含真正的贴边者。

    **不能直接对两条边界求交**：缺口环是从重结点化后的并集上取下来的，与原始
    面边的坐标差在 ~1e-16 量级（实测两者 ``distance`` 为 0，但 ``intersection``
    的长度算出来是 0），直接求交会被 GEOS 塌成点，贡献者全丢——这正是真实数据
    fid3/665 一开始检不出缺口的原因。改为把缺口边界加宽成一条 ``tolerance``
    宽的带，用各几何边界落在带内的长度作为「贴合长度」：

    - 真正贴边的面 → 整段边都在带内，长度 ≈ 边本身；
    - 只在一点相接的面 → 穿过带的那一小段 ≈ ``2·tolerance``，被下面的
      :func:`max` 下限（``10·tolerance``）排除。
    """
    h_boundary = _boundary(gap)
    if h_boundary is None:
        return ()
    perimeter = gap.length()
    if perimeter <= 0.0:
        return ()

    band = h_boundary
    if tolerance > 0.0:
        widened = h_boundary.buffer(tolerance, _BUFFER_SEGMENTS)
        if widened is not None and not widened.isNull() and not widened.isEmpty():
            band = widened
    floor = max(perimeter * _SHARED_RATIO, 10.0 * tolerance)

    shares: list[tuple[int, float]] = []
    for idx, geom in enumerate(geometries):
        if geom is None or geom.isNull() or geom.isEmpty():
            continue
        g_boundary = _boundary(geom)
        if g_boundary is None:
            continue
        overlap = g_boundary.intersection(band)
        if overlap is None or overlap.isEmpty():
            continue
        length = overlap.length()
        if length <= floor:
            continue
        shares.append((idx, length))
    return shares


def gap_contributors(
    gap: QgsGeometry,
    geometries: Sequence[QgsGeometry],
    *,
    tolerance: float = 0.0,
) -> tuple[int, ...]:
    """返回边界与 ``gap`` 贴合的**输入几何下标**（升序）。

    ``tolerance`` 为贴合判定的容差（0 表示精确求交，仅适用于缺口取自原始
    几何的情形；缺口若来自重结点化的并集，须传入与重结点化同量级的容差）。
    """
    return tuple(
        idx for idx, _ in _shared_lengths(gap, geometries, tolerance=tolerance)
    )


def _diagonal(geometries: Sequence[QgsGeometry]) -> float:
    """输入几何总包围盒的对角线长度（各比例阈值的基准尺度）。"""
    xs_min = ys_min = float("inf")
    xs_max = ys_max = float("-inf")
    for geom in geometries:
        if geom is None or geom.isNull() or geom.isEmpty():
            continue
        bbox = geom.boundingBox()
        xs_min = min(xs_min, bbox.xMinimum())
        ys_min = min(ys_min, bbox.yMinimum())
        xs_max = max(xs_max, bbox.xMaximum())
        ys_max = max(ys_max, bbox.yMaximum())
    if xs_min > xs_max or ys_min > ys_max:
        return 0.0
    return ((xs_max - xs_min) ** 2 + (ys_max - ys_min) ** 2) ** 0.5


def _seal_degenerate_touches(union: QgsGeometry, diagonal: float) -> QgsGeometry:
    """把并集做一次**极小尺度**闭运算，让「仅在一点相接」的退化拓扑长出真正的内环。

    为什么需要：两个面只在**一个点**上相接时，``unaryUnion`` 返回 2 个部件、
    **0 个内环**——但两者之间的口袋在视觉上是被完全围住的，用户会认为
    「明明有缝隙却识别不出来」。以极小 ``d`` 做 ``buffer(+d).buffer(-d)``
    等价于一次重新结点化：把这种点接触补实，口袋随即成为内环。

    ``d`` 取包围盒对角线的 :data:`_SEAL_RATIO`，只需大于点接触的数值宽度、
    又远小于任何真实几何特征——实测既不改变夹具 A 的 100.0000，也不影响
    单面内环（湖）的判定。传入并集为空 / 运算失败时原样返回。
    """
    if diagonal <= 0.0 or union.isEmpty():
        return union
    distance = diagonal * _SEAL_RATIO
    try:
        sealed = union.buffer(distance, _BUFFER_SEGMENTS).buffer(
            -distance, _BUFFER_SEGMENTS
        )
    except Exception:  # noqa: BLE001 —— GEOS 失败属可预期，退回原始并集
        logger.warning("补缝：并集重结点化失败，改用原始并集", exc_info=True)
        return union
    if sealed is None or sealed.isNull() or sealed.isEmpty():
        return union
    return sealed


def _refine_gap(
    hole: QgsGeometry,
    union: QgsGeometry,
    distance: float,
) -> QgsGeometry:
    """把「重结点化后的内环」精确化成**原始面边界**围出来的缺口。

    内环取自被 buffer 过的并集，其边界相对原始面边有 ~``distance`` 的偏移与抖动。
    直接拿它并入目标面，并集的公共边会退化成几十个间距 ~1e-8 的微顶点（渲染出来
    就是那条「细线」），面积也会差一个发丝量。

    做法：把内环外扩 ``2·distance`` 后**减去原始并集**——减掉的部分正是被原面覆盖
    的区域，剩下的边界就精确落在原始面边上。外扩会从点接触的缝里漏出一小块，
    因此取「包含原环内部点」的那一块（退化时退回面积最大的一块）。
    """
    if distance <= 0.0:
        return hole
    grown = hole.buffer(2.0 * distance, _BUFFER_SEGMENTS)
    if grown is None or grown.isNull() or grown.isEmpty():
        return hole
    diff = grown.difference(union)
    if diff is None or diff.isNull() or diff.isEmpty():
        return hole

    parts = list(diff.constParts())
    if len(parts) > 1:
        probe = hole.pointOnSurface()
        picked = None
        for part in parts:
            candidate = QgsGeometry.fromWkt(part.asWkt())
            if candidate.contains(probe):
                picked = candidate
                break
        if picked is None:
            # 退化兜底：外扩漏出去的那块面积远小于缺口本身
            picked = QgsGeometry.fromWkt(max(parts, key=lambda p: p.area()).asWkt())
        diff = picked
    return _drop_duplicate_nodes(diff, distance)


def _drop_duplicate_nodes(geometry: QgsGeometry, tolerance: float) -> QgsGeometry:
    """去掉近重合顶点。

    并集 / 差集会在贴合边上留下间距 ~1e-8 的微顶点，渲染时表现为一条细线。
    容差取重结点化同量级——对原始几何而言可忽略。
    """
    if tolerance <= 0.0 or not hasattr(geometry, "removeDuplicateNodes"):
        return geometry
    try:
        # 与 convertToMultiType 同一个坑：**就地修改、返回 bool 成功标志**，
        # 拿返回值当几何用会在下一步挂掉
        geometry.removeDuplicateNodes(tolerance)
    except Exception:  # noqa: BLE001 —— 清理失败不影响主流程
        logger.warning("补缝：去重顶点失败，保留原几何", exc_info=True)
    return geometry


def _area_m2(geometry: QgsGeometry, crs) -> float:
    """几何面积，单位**平方米**。

    经纬度坐标系直接 ``geometry.area()`` 得到的是「平方度」，数值小到没有意义
    （用户会问「面积怎么这么小」）。改走 :class:`QgsDistanceArea` 的椭球面积
    计算：地理坐标系按椭球算出 m²，投影坐标系按其单位算出 m²（英尺制 CRS 除外，
    那类数据本身就不以米为单位）。CRS 缺失 / 计算失败时退回平面面积。
    """
    if crs is None or not crs.isValid():
        return geometry.area()
    try:
        from qgis.core import QgsDistanceArea, QgsProject

        measure = QgsDistanceArea()
        measure.setSourceCrs(crs, QgsProject.instance().transformContext())
        ellipsoid = crs.ellipsoidAcronym()
        if ellipsoid:
            measure.setEllipsoid(ellipsoid)
        return measure.measureArea(geometry)
    except Exception:  # noqa: BLE001 —— 量算失败退回平面面积
        logger.warning("补缝：椭球面积量算失败，退回平面面积", exc_info=True)
        return geometry.area()


def detect_gaps(
    geometries: Sequence[QgsGeometry],
    *,
    min_area: float = 0.0,
    crs=None,
) -> list[Gap]:
    """检测输入多边形并集中的**封闭缝隙**。

    :param geometries: 参与检测的面几何（通常即图层当前选中的要素）
    :param min_area: 缺口面积下限（**平方米**，与 :attr:`Gap.area` 同单位），
        用于滤掉数值薄片；默认 0.0 表示只要求面积为正
    :param crs: 输入几何的坐标系，用于把面积换算成平方米（见 :func:`_area_m2`）；
        不传则退回平面面积（图层 CRS 单位²）
    :return: 检测到的缺口列表；``contributor_indices`` 是**传参序列的下标**
        （与非法几何被跳过无关），调用方据此映射回要素 fid。
    """
    # 保留原下标：非法几何被跳过时不能让贡献者下标整体前移
    indexed = [
        (i, geom)
        for i, geom in enumerate(geometries)
        if geom is not None and not geom.isNull() and not geom.isEmpty()
    ]
    if len(indexed) < 2:
        return []
    positions = [i for i, _ in indexed]
    geoms = [geom for _, geom in indexed]

    try:
        union = QgsGeometry.unaryUnion(geoms)
    except Exception:  # noqa: BLE001 —— GEOS 求并失败属可预期
        logger.warning("补缝：选中面求并失败，跳过检测", exc_info=True)
        return []
    if union is None or union.isNull() or union.isEmpty():
        return []

    # 点接触会让并集退化成「多部件 + 无内环」，口袋状的缝隙因此检不出来；
    # 先按极小尺度重结点化，再取内环。
    diagonal = _diagonal(geoms)
    source = _seal_degenerate_touches(union, diagonal)
    tolerance = diagonal * _SEAL_RATIO
    # 薄片下限与缺口面积同单位（平方米）：拿选区包围盒的面积量出来再按比例取，
    # 否则地理坐标系下「平方度」与「平方米」混比会误杀真实缝隙
    floor = max(min_area, _area_m2(QgsGeometry.fromRect(union.boundingBox()), crs) * _MIN_AREA_RATIO)

    gaps: list[Gap] = []
    for hole in _iter_holes(source):
        # 内环取自被 buffer 过的并集，先还原成原始面边精确围出的缺口，
        # 否则并入目标面时会留下微顶点链（渲染成细线）
        gap = _refine_gap(hole, union, tolerance)
        area = _area_m2(gap, crs)
        if area <= 0.0 or area < floor:
            continue
        perimeter = gap.length()
        if perimeter <= 0.0:
            continue

        # 缺口已精确到原始面边 → 优先精确求交；退化时再退回带容差判定
        shares = _shared_lengths(gap, geoms)
        if len(shares) < 2:
            shares = _shared_lengths(gap, geoms, tolerance=tolerance)
        # 判据一：只有单个几何贡献边界 → 该面自有内环（湖 / 塘），不是缝隙
        if len(shares) < 2:
            continue
        # 判据二：某个几何独自贡献整圈边界 → 重复 / 共面选中的同一要素
        if any(length >= perimeter * (1.0 - _SHARED_RATIO) for _, length in shares):
            continue

        gaps.append(
            Gap(
                index=len(gaps),
                geometry=gap,
                area=area,
                contributor_indices=tuple(positions[p] for p, _ in shares),
            )
        )
    return gaps


def merge_gap_into(
    target: QgsGeometry,
    gaps: Sequence[QgsGeometry],
    *,
    stitch_ratio: float = _STITCH_WIDTH_RATIO,
) -> QgsGeometry | None:
    """把 ``gaps`` 并入选定的目标面，返回新几何；失败返回 ``None``。

    目标面取自缺口的贡献者集合时，并集必然是单部件（缺口与目标共享整段边，
    顶点由 union 结点对齐），实测无需缝合。若仍得到多部件（目标面与缺口其实
    不共边，理论上不该发生），用 ``buffer(+w).buffer(-w)`` 微膨胀缝合兜底；
    再失败则返回 ``None`` 让调用方跳过并告警，**绝不返回坏几何**。
    """
    if target is None or target.isNull() or target.isEmpty():
        return None
    parts = [
        target,
        *(g for g in gaps if g is not None and not g.isNull() and not g.isEmpty()),
    ]
    if len(parts) < 2:
        return None

    try:
        merged = QgsGeometry.unaryUnion(parts)
    except Exception:  # noqa: BLE001
        logger.warning("补缝：目标面与缺口求并失败", exc_info=True)
        return None
    if merged is None or merged.isNull() or merged.isEmpty():
        return None
    if not merged.isMultipart():
        return merged

    bbox = merged.boundingBox()
    width = min(bbox.width(), bbox.height()) * stitch_ratio
    if width > 0:
        try:
            stitched = merged.buffer(width, _STITCH_SEGMENTS).buffer(
                -width, _STITCH_SEGMENTS
            )
        except Exception:  # noqa: BLE001
            stitched = None
        if (
            stitched is not None
            and not stitched.isNull()
            and not stitched.isEmpty()
            and not stitched.isMultipart()
        ):
            return stitched

    logger.warning("补缝：目标面与缺口并集为多部件，已跳过该缝隙")
    return None


__all__ = ["Gap", "detect_gaps", "gap_contributors", "merge_gap_into"]
