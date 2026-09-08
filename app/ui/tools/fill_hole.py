"""补洞工具：点击面要素洞的内部，该洞被填平（删除内环，几何恢复实心）。

与「挖洞」互为逆操作。编辑工具条「补洞」按钮激活：在要填补的洞**内部**
单击（洞 = 面要素的内环，常见于挖洞 / 勾绘跑道式边界的数据），对应内环被
删除，要素恢复实心；FGUID / 普通属性不动，面积 / 周长类字段按填洞后的
几何重算（见 :mod:`app.ui.tools.feature_attrs`）。**不新增要素**——只是把
要素自己的洞删掉。

实现：洞一定落在要素的**外接框**内（按外接框包含点击点筛候选），再逐部件
逐内环判断哪个环包含点击点（环连成面后 contains 点击点），取**离点击点最近**
的那个环删除。支持多部件几何；改动写进一个 ``beginEditCommand("补洞")``，
可 Ctrl+Z 撤销。
"""

from __future__ import annotations

from collections.abc import Callable

from qgis.core import (
    QgsCoordinateTransform,
    QgsGeometry,
    QgsLineString,
    QgsMultiPolygon,
    QgsPoint,
    QgsPointXY,
    QgsPolygon,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool
from qgis.PyQt.QtCore import QPoint, QPointF, Qt, QTimer

from app.core.domain.check.space_index import clear_spatial_index
from app.core.domain.drawing.tools.base import _snap_indicator_for
from app.core.domain.drawing.tools.snap import snapped_match
from app.infra.logging import get_logger
from app.ui.tools.feature_attrs import refresh_area_fields

logger = get_logger(__name__)


class FillHoleTool(QgsMapTool):
    """点击洞内部 → 删除该内环（补洞）。Esc 退出。"""

    def __init__(
        self,
        canvas,
        editing_layer_getter: Callable[[], QgsVectorLayer | None] = lambda: None,
        hint: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self._layer_getter = editing_layer_getter
        self._hint = hint or (lambda text: None)
        self.setCursor(Qt.CursorShape.CrossCursor)

    # -------------------------------------------------- 工具生命周期

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            event.ignore()
            # 不在键盘事件栈内 unsetMapTool，推迟出栈
            QTimer.singleShot(0, self._quit)
        else:
            super().keyPressEvent(event)

    def _quit(self) -> None:
        try:
            self.canvas.unsetMapTool(self)
        except RuntimeError:
            pass

    # -------------------------------------------------- 交互

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        """左键单击：填补点击点所在的洞（删除对应内环）。"""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        layer = self._layer_getter()
        if layer is None or layer.type() != layer.VectorLayer:
            self._hint("没有编辑图层：请先点「开始编辑」选择图层")
            return
        if not layer.isEditable():
            self._hint("图层未处于编辑态，无法补洞")
            return
        if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            self._hint("补洞只支持面图层")
            return

        # 点击点：优先吸附（吸到环边上也算命中洞边界附近）
        match = snapped_match(self.canvas, event, wait_index=True)
        self._set_snap_match(match)
        if match is not None:
            map_pt = match.point()
        else:
            map_pt = self.canvas.getCoordinateTransform().toMapCoordinates(
                self._pixel_of_event(event)
            )

        layer_pt = self._to_layer_point(layer, map_pt)
        if layer_pt is None:
            return
        self._fill_at(layer, layer_pt)

    # -------------------------------------------------- 补洞

    def _fill_at(self, layer: QgsVectorLayer, layer_pt: QgsPoint) -> None:
        """在点击点处找洞并填补（一个撤销命令；找不到提示）。"""
        # 洞内部不属于要素本体 → 按点击点相交查询必然查不到；洞一定落在要素
        # **外接框**内 → 用「外接框包含点击点」筛候选，再逐内环判定归属。
        pt_xy = QgsPointXY(layer_pt.x(), layer_pt.y())
        pt_geom = QgsGeometry.fromPointXY(pt_xy)

        target: tuple[int, int, int, float] | None = None  # (fid, part_idx, ring_idx, dist)
        for feature in layer.getFeatures():
            if not feature.isValid():
                continue
            geom = feature.geometry()
            if geom is None or geom.isNull() or geom.isEmpty():
                continue
            try:
                if not geom.boundingBox().contains(pt_xy):
                    continue
            except Exception:  # noqa: BLE001
                continue
            for part_idx, part in enumerate(geom.constParts()):
                for ring_idx in range(part.numInteriorRings()):
                    ring = QgsLineString(part.interiorRing(ring_idx).points())
                    ring_poly = QgsGeometry(QgsPolygon(ring))
                    # 点击点在该环的内部（环连成面后包含点击点）才算洞内
                    if not ring_poly.contains(pt_geom):
                        continue
                    dist = ring_poly.distance(pt_geom)
                    if target is None or dist < target[3]:
                        target = (feature.id(), part_idx, ring_idx, dist)
        if target is None:
            self._hint("未找到洞：请点击面要素洞的内部（挖洞产生的内环区域）")
            return

        fid, part_idx, ring_idx, _dist = target
        feature = layer.getFeature(fid)
        filled = _remove_ring(feature.geometry(), part_idx, ring_idx)
        if filled is None:
            self._hint("补洞失败：洞环重建异常")
            return

        layer.beginEditCommand("补洞")
        try:
            layer.changeGeometry(fid, filled)
            # 面积 / 周长类字段按填洞后的几何重算：attrs 作可变目标、before 留
            # 旧值副本对比（refresh_area_fields 就地改写目标列表，二者不能同一）
            attrs = list(feature.attributes())
            before = list(attrs)
            refreshed = refresh_area_fields(layer, attrs, filled)
            for idx, value in enumerate(refreshed):
                if before[idx] != value:
                    layer.changeAttributeValue(fid, idx, value)
        except RuntimeError:
            layer.destroyEditCommand()
            self._hint("补洞失败：图层编辑 API 异常")
            return
        layer.endEditCommand()

        clear_spatial_index(layer)
        try:
            layer.select([fid])
        except RuntimeError:
            logger.warning("补洞后选中要素失败（不影响补洞结果）", exc_info=True)
        self._hint("已填补要素的洞（可 Ctrl+Z 撤销）")
        # 单击事件栈内不直接 refresh（仓库约束），推迟出栈后再刷新
        QTimer.singleShot(0, self._safe_refresh)

    # -------------------------------------------------- 工具辅助

    def _to_layer_point(self, layer: QgsVectorLayer, map_pt: QgsPointXY) -> QgsPoint | None:
        """画布地图坐标 → 图层 CRS 坐标（CRS 相同 / 无效时恒等）。"""
        src = self.canvas.mapSettings().destinationCrs()
        dst = layer.crs()
        if not src.isValid() or not dst.isValid() or src == dst:
            return QgsPoint(map_pt)
        try:
            xform = QgsCoordinateTransform(src, dst, self.canvas.project() or QgsProject.instance())
            return QgsPoint(xform.transform(QgsPointXY(map_pt)))
        except Exception:
            logger.warning("补洞点击点坐标转换失败", exc_info=True)
            return None

    def _set_snap_match(self, match) -> None:
        """把吸附 match 喂给画布共享的 :class:`QgsSnapIndicator`（吸不到隐藏）。"""
        widget = _snap_indicator_for(self.canvas)
        if widget is None:
            return
        try:
            if match is not None and match.isValid():
                widget.setMatch(match)
                widget.setVisible(True)
            else:
                widget.setVisible(False)
        except Exception:  # noqa: BLE001
            pass

    def _pixel_of_event(self, event) -> QPoint:
        """事件像素坐标（QPointF 测试桩 / 真实 QPoint 都兼容）。"""
        raw = event.pixelPoint()
        if isinstance(raw, QPointF):
            return raw.toPoint()
        return QPoint(raw.x(), raw.y())

    def _safe_refresh(self) -> None:
        try:
            self.canvas.refresh()
        except RuntimeError:
            pass


def _remove_ring(
    geom: QgsGeometry, part_idx: int, ring_idx: int
) -> QgsGeometry | None:
    """重建几何：删除 ``part_idx`` 部件的 ``ring_idx`` 内环（深拷贝防 double-free）。

    QGIS 4.2 的 :class:`QgsGeometry` 不导出 ``removeInteriorRing``，这里按
    外环 + 其余内环重建目标部件（部件间用 WKT 深拷贝——仓库既定结论：
    ``QgsGeometry(part)`` 转移所有权会段错误），其余部件原样保留。
    """
    try:
        parts_out: list[QgsPolygon] = []
        for i, part in enumerate(geom.constParts()):
            if i == part_idx:
                ext = QgsLineString(part.exteriorRing().points())
                interiors = [
                    QgsLineString(part.interiorRing(j).points())
                    for j in range(part.numInteriorRings())
                    if j != ring_idx
                ]
                parts_out.append(QgsPolygon(ext, interiors))
            else:
                clone = QgsGeometry.fromWkt(part.asWkt())
                if clone.isNull():
                    return None
                parts_out.append(clone.constGet().clone())
        if not parts_out:
            return None
        if len(parts_out) == 1:
            return QgsGeometry(parts_out[0])
        multi = QgsMultiPolygon()
        for poly in parts_out:
            multi.addGeometry(poly)
        return QgsGeometry(multi)
    except Exception:  # noqa: BLE001
        logger.exception("删除内环失败: part=%s ring=%s", part_idx, ring_idx)
        return None


__all__ = ["FillHoleTool"]
