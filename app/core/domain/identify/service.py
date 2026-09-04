"""点击识别服务。

迁自 :class:`component.map_tool.FindFeatureTool`，去掉对 :class:`ModernFeatureInfoDialog`
的依赖，业务层只负责"识别 + 高亮"。

UI 层：调 :meth:`identify` 拿 :class:`IdentifyResult` 后，弹自己的 dialog。

**ArcGIS MapServer 栅格富化**：QGIS 对 ``arcgismapserver`` 栅格的 identifyRaster
只返回要素几何（可高亮），属性为空；:meth:`FeatureIdentifyService.identify` 对这类
图层改调服务端 ``…/MapServer/identify`` REST 接口，把属性一并取回，
从而在属性识别工具里能看到属性。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from qgis.core import QgsFeature, QgsMapLayer, QgsVectorLayer
    from qgis.gui import QgsHighlight, QgsMapCanvas, QgsMapToolIdentify


@dataclass(frozen=True)
class IdentifyResult:
    """点击识别的结果：单条矢量要素，或栅格图层某像元的波段值。"""

    layer: "QgsMapLayer"
    feature: Optional["QgsFeature"] = None
    geometry_wkt: str = ""
    attributes: dict = field(default_factory=dict)
    #: 栅格图层时有效：波段名 → 该像元值（如 {"Band 1": "137"}）
    raster_values: dict = field(default_factory=dict)

    @property
    def is_raster(self) -> bool:
        from qgis.core import QgsRasterLayer

        return isinstance(self.layer, QgsRasterLayer)

    @property
    def fid(self) -> int:
        return int(self.feature.id()) if self.feature is not None else -1


#: ``arcgismapserver`` 连接串形如 ``format='PNG32' layer='0' url='…/MapServer'``
_ARC_URI_URL_RE = re.compile(r"\burl='([^']*)'")
_ARC_URI_LAYER_RE = re.compile(r"\b(?:layer|layers)='([^']*)'")
_ARC_SVC_SUBLAYER_RE = re.compile(r"/(MapServer|ImageServer)/(\d+)$", re.IGNORECASE)
_ARC_SVC_RE = re.compile(r"/(MapServer|ImageServer)$", re.IGNORECASE)


def parse_arcgis_mapserver_uri(uri: str) -> Tuple[Optional[str], Optional[int]]:
    """解析 ``arcgismapserver`` 连接串 → ``(服务地址, 子图层号)``。

    - ``url='…/MapServer'`` + ``layer='0'``（本仓库生成的连接串形态）；
    - url 自带 ``…/MapServer/0`` 子图层路径（用户在 QGIS 里手填的形态）。

    两者都不匹配时返回 ``(None, None)``。
    """
    m = _ARC_URI_URL_RE.search(uri or "")
    if not m:
        return None, None
    url = m.group(1)
    sm = _ARC_SVC_SUBLAYER_RE.search(url)
    if sm:
        try:
            # 去掉末尾的 "/<子图层号>"，保留服务根（…/MapServer）
            return url[: sm.start() + len(sm.group(1)) + 1], int(sm.group(2))
        except ValueError:
            return None, None
    if not _ARC_SVC_RE.search(url):
        return None, None
    lm = _ARC_URI_LAYER_RE.search(uri or "")
    try:
        sub = int(lm.group(1)) if lm else 0
    except ValueError:
        sub = 0
    return url, sub


def rings_to_wkt(rings) -> Optional[str]:
    """ArcGIS REST 多边形 ``rings`` → WKT ``MultiPolygon``。

    每个环独立成一个面（不判断内外环/洞，避免把不相邻的环误判成洞），
    坐标按 y-up 的 X Y 顺序输出；无有效环返回 ``None``。
    """
    parts: List[str] = []
    for ring in rings or []:
        pts: List[str] = []
        for p in ring or []:
            try:
                pts.append(f"{float(p[0]):.6f} {float(p[1]):.6f}")
            except (TypeError, ValueError, IndexError):
                continue
        if len(pts) < 3:
            continue
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        parts.append("((" + ", ".join(pts) + "))")
    if not parts:
        return None
    return "MultiPolygon (" + ", ".join(parts) + ")"


class FeatureIdentifyService:
    """点击识别服务。"""

    def identify(
        self,
        canvas: "QgsMapCanvas",
        x: float,
        y: float,
        mode: Optional["QgsMapToolIdentify.IdentifyMode"] = None,
    ) -> List[IdentifyResult]:
        """点击画布坐标 ``(x, y)`` 识别要素。

        ``mode`` 默认 ``TopDownAll``（所有图层里所有可识别要素）。
        """
        from qgis.gui import QgsMapToolIdentify
        from qgis.core import QgsRasterLayer

        if mode is None:
            mode = QgsMapToolIdentify.IdentifyMode.TopDownAll
        tool = QgsMapToolIdentify(canvas)
        # QGIS 4.x 的 identify(x, y, mode) 要求 int 坐标 + IdentifyMode 枚举，
        # 传 float / int 都会 TypeError（int 在 Qt6 绑定不再自动转枚举）
        results = tool.identify(int(x), int(y), mode)
        # ArcGIS MapServer 栅格层：QGIS 的 identifyRaster 只带回要素几何（可高亮），
        # 属性全空 —— 对这类图层先做一次服务端 REST identify 富化（属性 + 几何），
        # 富化成功就顶替原始栅格结果；失败则回退到默认栅格像元分支。
        enriched: Dict[int, List[IdentifyResult]] = {}
        for layer in {
            r.mLayer
            for r in results
            if isinstance(r.mLayer, QgsRasterLayer) and r.mLayer.providerType() == "arcgismapserver"
        }:
            got = self._identify_mapserver_features(canvas, layer, int(x), int(y))
            if got:
                enriched[id(layer)] = got
        out: List[IdentifyResult] = []
        done: set = set()
        for r in results:
            layer = r.mLayer
            feature = r.mFeature
            if isinstance(layer, QgsRasterLayer) and id(layer) in enriched:
                # 该图层已被 REST identify 结果顶替，只放一次
                if id(layer) not in done:
                    done.add(id(layer))
                    out.extend(enriched[id(layer)])
                continue
            if isinstance(layer, QgsRasterLayer):
                # 栅格像元：波段值在 QgsMapToolIdentify 结果的 mAttributes
                # （如 {"Band 1": "137"}），要素几何是点击处的像元格子
                geom_wkt = ""
                try:
                    if feature is not None:
                        geom_wkt = feature.geometry().asWkt()
                except Exception:  # noqa: BLE001
                    pass
                out.append(
                    IdentifyResult(
                        layer=layer,
                        feature=feature,
                        geometry_wkt=geom_wkt,
                        raster_values={k: str(v) for k, v in r.mAttributes.items()},
                    )
                )
                continue
            if feature is None:
                continue
            attrs = {f.name(): feature.attribute(f.name()) for f in layer.fields()}
            geom_wkt = ""
            try:
                geom_wkt = feature.geometry().asWkt()
            except Exception:  # noqa: BLE001
                pass
            out.append(IdentifyResult(layer=layer, feature=feature, geometry_wkt=geom_wkt, attributes=attrs))
        return out

    def _identify_mapserver_features(
        self,
        canvas: "QgsMapCanvas",
        layer: "QgsRasterLayer",
        x: int,
        y: int,
    ) -> Optional[List[IdentifyResult]]:
        """ArcGIS MapServer 栅格层：调服务端 ``…/MapServer/identify`` 取点击要素的属性。

        QGIS 对 ``arcgismapserver`` 的 identifyRaster 只返回几何（可高亮），属性为空；
        这里用与 QGIS 相同语义的 REST identify（点击点 + 容差 + 当前视图）直接取回
        要素属性与几何，构造与矢量结果等价的 :class:`IdentifyResult`。

        任何失败（非 MapServer、CRS 缺失、网络错误、服务无结果）都返回 ``None``，
        由调用方回退到默认栅格像元分支。
        """
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtNetwork import QNetworkRequest
        from qgis.core import (
            QgsBlockingNetworkRequest,
            QgsCoordinateTransform,
            QgsCoordinateTransformContext,
            QgsFeature,
            QgsGeometry,
            QgsPointXY,
            QgsRectangle,
        )

        try:
            base_url, sublayer = parse_arcgis_mapserver_uri(layer.source())
            if base_url is None or sublayer is None:
                return None
            authid = layer.crs().authid()
            if not authid.upper().startswith("EPSG:"):
                return None
            wkid = int(authid.split(":")[1])
            settings = canvas.mapSettings()
            width = settings.outputSize().width()
            height = settings.outputSize().height()
            if not width or not height:
                return None
            ct = QgsCoordinateTransform(settings.destinationCrs(), layer.crs(), QgsCoordinateTransformContext())
            pt = ct.transform(settings.mapToPixel().toMapCoordinates(x, y))
            # 屏幕范围 → 图层 CRS（四角各转一次，规避 transformBoundingBox 的绑定差异）
            extent = settings.extent()
            corners = [
                ct.transform(QgsPointXY(extent.xMinimum(), extent.yMinimum())),
                ct.transform(QgsPointXY(extent.xMaximum(), extent.yMinimum())),
                ct.transform(QgsPointXY(extent.xMinimum(), extent.yMaximum())),
                ct.transform(QgsPointXY(extent.xMaximum(), extent.yMaximum())),
            ]
            ext = QgsRectangle(
                min(p.x() for p in corners),
                min(p.y() for p in corners),
                max(p.x() for p in corners),
                max(p.y() for p in corners),
            )
            qs = (
                "?f=json"
                f"&geometry={pt.x():.6f},{pt.y():.6f}"
                "&geometryType=esriGeometryPoint"
                f"&sr={wkid}"
                f"&layers=all:{sublayer}"
                "&tolerance=10"
                f"&mapExtent={ext.xMinimum():.6f},{ext.yMinimum():.6f},"
                f"{ext.xMaximum():.6f},{ext.yMaximum():.6f}"
                f"&imageDisplay={width},{height},96"
                "&returnGeometry=true&returnZ=false&returnM=false"
            )
            req = QgsBlockingNetworkRequest()
            code = req.get(QNetworkRequest(QUrl(base_url + "/identify" + qs)))
            if code != QgsBlockingNetworkRequest.ErrorCode.NoError:
                return None
            doc = json.loads(bytes(req.reply().content()).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 —— 富化失败就回退，不让识别流程崩
            return None
        results_out: List[IdentifyResult] = []
        for res in doc.get("results") or []:
            raw_attrs = res.get("attributes")
            if not isinstance(raw_attrs, dict):
                continue
            attrs = {str(k): ("" if v is None else v) for k, v in raw_attrs.items()}
            feature = QgsFeature()
            wkt = ""
            geom_data = res.get("geometry")
            if isinstance(geom_data, dict):
                wkt = rings_to_wkt(geom_data.get("rings")) or ""
                if wkt:
                    geom = QgsGeometry.fromWkt(wkt)
                    if geom is not None and not geom.isNull():
                        feature.setGeometry(geom)
            oid = next((v for k, v in raw_attrs.items() if str(k).lower() == "objectid"), None)
            if oid is not None:
                try:
                    feature.setId(int(oid))
                except (TypeError, ValueError):
                    pass
            results_out.append(
                IdentifyResult(
                    layer=layer,
                    feature=feature,
                    geometry_wkt=wkt,
                    attributes=attrs,
                )
            )
        return results_out or None

    def highlight(
        self,
        canvas: "QgsMapCanvas",
        layer: "QgsVectorLayer",
        feature: "QgsFeature",
    ) -> "QgsHighlight":
        """创建高亮对象（红色边框），调用方负责 ``deleteLater()``。"""
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtGui import QColor
        from qgis.gui import QgsHighlight

        h = QgsHighlight(canvas, feature, layer)
        h.setColor(QColor(255, 0, 0))
        h.setFillColor(QColor(255, 0, 0, 50))
        h.setWidth(1)
        h.show()
        return h
