"""挖洞工具：在面要素上画多边形区域，该区域被挖去成为洞。

编辑工具条「挖洞」按钮激活的交互工具（与「面分割」同一套画多边形交互：
左键加点（磁性捕捉）、移动预览、双击执行、右键清除、Esc 退出/清多边形）：

- 双击完成 → 画的多边形作为**挖除区域**，被它覆盖的面要素执行
  ``difference``——区域内部分被挖去：完全在区域内部画的坑变成**洞**（内环），
  压着边画的则是削角 / 削边；
- 一次可同时挖多个被覆盖的要素；没挖到任何要素（没覆盖 / 完全覆盖被跳过 /
  退化接触）时提示并可重画；
- 属性：仅几何变化，FGUID / 普通属性不动；面积 / 周长类字段按挖洞后的几何
  重算（见 :mod:`app.ui.tools.feature_attrs`）。

全部改动写进一个 ``beginEditCommand("挖洞")``，Ctrl+Z 一次撤销。要素变更后
清空间索引缓存；画布刷新按仓库约束 ``QTimer.singleShot(0, ...)`` 推迟执行。
"""

from __future__ import annotations

from qgis.core import QgsGeometry, QgsWkbTypes
from qgis.PyQt.QtCore import QTimer

from app.core.domain.check.query import intersect_features
from app.core.domain.check.space_index import clear_spatial_index
from app.infra.logging import get_logger
from app.ui.tools.feature_attrs import refresh_area_fields
from app.ui.tools.split_polygon import SplitPolygonTool

logger = get_logger(__name__)


class DigHoleTool(SplitPolygonTool):
    """在面要素上画挖除区域（复用面分割的画多边形交互），双击挖洞。"""

    def _run_cutter_action(self) -> None:
        """双击完成：把画的多边形区域从被覆盖的面要素中挖去。"""
        layer, cutter = self._prepare_cutter()
        if cutter is None:
            return
        if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            self._hint("挖洞只支持面图层，请切换到面图层")
            self._clear_polygon()
            return

        fids = intersect_features(layer, cutter, layer.crs())
        plans: list[tuple[int, QgsGeometry, list]] = []
        skipped_covered = 0
        for fid in fids:
            feature = layer.getFeature(fid)
            if not feature.isValid():
                continue
            geom = feature.geometry()
            if geom is None or geom.isNull() or geom.isEmpty():
                continue
            dug = geom.difference(cutter)
            if dug is None or dug.isNull() or dug.isEmpty():
                # 区域完全覆盖要素 → 不挖（避免整块删掉要素）
                skipped_covered += 1
                continue
            # 只有真挖掉了才写回（区域在要素外 / 擦边 → 面积不变，跳过）
            if abs(self._measure(layer, geom) - self._measure(layer, dug)) <= 1e-9:
                continue
            plans.append((fid, dug, list(feature.attributes())))
        if not plans:
            msg = "多边形未挖到任何面要素：请把挖除区域画在要素内部（不要完全盖住要素）"
            if skipped_covered:
                msg = f"{msg}（{skipped_covered} 个要素被完全覆盖，已跳过）"
            self._hint(msg)
            self._clear_polygon()
            return

        layer.beginEditCommand("挖洞")
        try:
            for fid, dug, attrs in plans:
                layer.changeGeometry(fid, dug)
                # 面积 / 周长类字段按挖洞后的几何重算（只为真正变了的字段写回）
                before = list(attrs)
                refreshed = refresh_area_fields(layer, attrs, dug)
                for idx, value in enumerate(refreshed):
                    if before[idx] != value:
                        layer.changeAttributeValue(fid, idx, value)
        except RuntimeError:
            layer.destroyEditCommand()
            self._hint("挖洞失败：图层编辑 API 异常")
            self._clear_polygon()
            return
        layer.endEditCommand()

        clear_spatial_index(layer)
        changed = [fid for fid, _g, _a in plans]
        try:
            layer.select(changed)
        except RuntimeError:
            logger.warning("挖洞后选中要素失败（不影响挖洞结果）", exc_info=True)
        self._hint(
            f"已从 {len(changed)} 个面要素中挖除多边形区域（可 Ctrl+Z 撤销）"
        )
        self._clear_polygon()
        # 双击事件栈内不直接 refresh（仓库约束），推迟出栈后再刷新
        QTimer.singleShot(0, self._safe_refresh)


__all__ = ["DigHoleTool"]
