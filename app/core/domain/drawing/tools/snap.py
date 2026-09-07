"""自定义绘制工具（点/线/面/矩形/圆）与磁性捕捉的桥接。

自定义工具（``app/core/domain/drawing/tools``）取点直接走
``canvas.getCoordinateTransform().toMapCoordinates(...)``，不经过 QGIS 原生
capture 的吸附管线，因此磁性捕捉设置（``QgisSnappingToolbar`` 写的
``QgsProject.snappingConfig()`` + 画布 ``snappingUtils()``）对它们原本无效。

本模块提供 :func:`snapped_match`（返回完整吸附 match，含顶点/边类型，供磁吸
指示器用）与兼容包装 :func:`snapped_map_point`（只取吸附点）：取事件地图坐标
时先按画布当前吸附配置吸附一次——命中则用吸附点，否则回退原坐标。吸附开关/
类型/容差/模式全部由画布 snapping utils 的现有配置决定，这里不重复判断。

索引就绪等待
------------
QGIS 的 snapping 索引是**懒构建**的：目标层需先被注册为 snapping utils 的
吸附层（snapping_toolbar 把编辑/目标图层注册为 extra snap layer），图层第一
次被吸附（``snapToMap`` 的 ``relaxed=True``）才建索引。索引就绪前每次 relaxed
调用都返回空命中——若取点后直接回退原坐标，用户**第一笔点击必然不吸附**
（观感 = 捕捉失效）。因此命中前若检测到任一 locator ``isIndexing()``（索引
构建中）会短暂轮询等索引建完再吸一次（小图层约 0.06s；大图层首次最多等
:data:`_INDEX_WAIT_MAX_S`）。

``wait_index=False``（鼠标移动预览用）：只触发索引构建不等待，避免拖动时
卡顿；落点取点（默认 True）等待——move 已预热过索引，落点时等待≈0。
"""

from __future__ import annotations

import time as _time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsPointLocator, QgsPointXY
    from qgis.gui import QgsMapCanvas

#: 首击等待索引建完的上限（秒）。大图层建索引慢于此时本次放弃回原坐标，
#: 索引建完后（isIndexing False）后续调用不再等待，无重复卡顿。
_INDEX_WAIT_MAX_S = 2.0


def snapped_match(
    canvas: "QgsMapCanvas",
    event,
    *,
    wait_index: bool = True,
) -> "QgsPointLocator.Match | None":
    """事件像素点 → 吸附命中的 locator match（含顶点/边类型）。

    :param canvas: 画布（读其 snappingUtils）
    :param event: 含 ``pixelPoint()`` 的 QGIS 鼠标事件
    :param wait_index: 吸附索引未就绪时是否等待其建完（落点 True；
        鼠标移动预览传 False，避免拖动卡顿）
    :return: 命中返回完整 :class:`QgsPointLocator.Match`（调用方可经
        ``hasVertex()`` / ``hasEdge()`` 区分顶点/边吸附，或直接喂给
        :class:`QgsSnapIndicator` 画不同图标）；未启用吸附 / 无 utils /
        无命中返回 ``None``
    """
    point = canvas.getCoordinateTransform().toMapCoordinates(event.pixelPoint())
    try:
        utils = canvas.snappingUtils()
        if utils is None:
            return None
        cfg = utils.config()
        if not cfg.enabled():
            return None
        match = utils.snapToMap(point, None, True)
        if match.isValid():
            return match
        # 索引懒构建兜底：relaxed 首调在目标层已注册（extra snap layer）时
        # 会同步建好小图层索引并命中；若仍在后台构建则轮询重试至就绪。
        if wait_index:
            t0 = _time.monotonic()
            while _time.monotonic() - t0 < _INDEX_WAIT_MAX_S:
                _time.sleep(0.02)
                match = utils.snapToMap(point, None, True)
                if match.isValid():
                    return match
                building = False
                try:
                    building = any(
                        loc is not None and loc.isIndexing()
                        for loc in (
                            utils.locatorForLayer(c.layer)
                            for c in utils.layers()
                        )
                    )
                except Exception:  # noqa: BLE001
                    pass
                if not building:
                    # 索引就绪但该点无命中 / 无目标层：不会突然命中，快退
                    break
    except Exception:  # noqa: BLE001 —— 吸附失败不阻断绘制
        pass
    return None


def snapped_map_point(
    canvas: "QgsMapCanvas",
    event,
    *,
    wait_index: bool = True,
) -> "QgsPointXY":
    """事件像素点 → 吸附后的地图坐标点（兼容包装，语义同 :func:`snapped_match`）。

    :param canvas: 画布（读其 snappingUtils）
    :param event: 含 ``pixelPoint()`` 的 QGIS 鼠标事件
    :param wait_index: 吸附索引未就绪时是否等待其建完（落点 True；
        鼠标移动预览传 False，避免拖动卡顿）
    :return: 吸附命中的地图点；未启用吸附 / 无 utils / 无命中时返回原地图点
    """
    match = snapped_match(canvas, event, wait_index=wait_index)
    if match is not None:
        return match.point()
    return canvas.getCoordinateTransform().toMapCoordinates(event.pixelPoint())
