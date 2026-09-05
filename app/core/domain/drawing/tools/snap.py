"""自定义绘制工具（点/线/面/矩形/圆）与磁性捕捉的桥接。

自定义工具（``app/core/domain/drawing/tools``）取点直接走
``canvas.getCoordinateTransform().toMapCoordinates(...)``，不经过 QGIS 原生
capture 的吸附管线，因此磁性捕捉设置（``QgisSnappingToolbar`` 写的
``QgsProject.snappingConfig()`` + 画布 ``snappingUtils()``）对它们原本无效。

本模块提供 :func:`snapped_map_point`：取事件地图坐标时先按画布当前吸附配置
吸附一次——命中则用吸附点，否则回退原坐标。吸附开关/类型/容差/模式全部由
画布 snapping utils 的现有配置决定，这里不重复判断。

说明：

- ``snapToMap`` 第三参 ``relaxed=True`` 首次调用会**异步**建索引（QGIS 桌面
  行为一致：首次绘制前索引在后台就绪）。启用捕捉时 ``QgisSnappingToolbar``
  会做一次暖调用预建索引，正常操作下首击即可命中。
- 吸附返回点与 ``toMapCoordinates`` 同为画布 CRS，直接替换不影响后续流程。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsPointXY
    from qgis.gui import QgsMapCanvas


def snapped_map_point(canvas: "QgsMapCanvas", event) -> "QgsPointXY":
    """事件像素点 → 吸附后的地图坐标点。

    :param canvas: 画布（读其 snappingUtils）
    :param event: 含 ``pixelPoint()`` 的 QGIS 鼠标事件
    :return: 吸附命中的地图点；未启用吸附 / 无 utils / 无命中时返回原地图点
    """
    point = canvas.getCoordinateTransform().toMapCoordinates(event.pixelPoint())
    try:
        utils = canvas.snappingUtils()
        if utils is None:
            return point
        match = utils.snapToMap(point, None, True)
        if match.isValid():
            return match.point()
    except Exception:  # noqa: BLE001 —— 吸附失败不阻断绘制
        pass
    return point
