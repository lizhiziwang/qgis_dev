"""要素/几何清洗共用工具（领域层，无 PyQt5 顶层依赖）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer

logger = get_logger(__name__)


def _maximum_string_length(layer: QgsVectorLayer, field_name: str):
    """返回 ``(最大字符串长度, 是否成功)``；失败时调用方退回声明长度。"""
    from qgis.core import QgsAggregateCalculator

    try:
        return layer.aggregate(
            QgsAggregateCalculator.Aggregate.StringMaximumLength, field_name
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("聚合取字段 %s 最大长度失败: %s", field_name, exc)
        return None, False


def copy_with_widened_string_fields(layer: QgsVectorLayer) -> QgsVectorLayer:
    """把 String 字段长度放宽到实际最大值，返回修正后的临时图层。

    GDB/ArcGIS 导出的图层常有「字段声明长度 < 实际数据长度」的脏数据：
    短代码字段（如 XIANG 声明 3）里存了字面量 ``'NULL'``（4 字符）。
    QGIS memory provider 严格校验字符串长度，超长值在 sink 写入时被拒绝，
    导致 ``native:buffer`` / 叠加算法报「无法将要素写入 OUTPUT」。此函数扫描
    字段实际最大长度（C++ 聚合，不逐要素进 Python），把超长字段放宽后拷贝成
    临时 memory 图层；无需修正时原样返回，不产生额外开销。声明长度 0 表示
    无上限（GeoJSON 等），不在修正范围内。

    注意：数据本身有超长值时仍需逐要素拷贝一次（字段声明不可原地修改），
    这一步在主线程完成；干净数据只做聚合扫描，不会创建图层。
    """
    from qgis.core import (
        Qgis,
        QgsFeature,
        QgsField,
        QgsFields,
        QgsVectorLayer,
        QgsWkbTypes,
    )
    from qgis.PyQt.QtCore import QVariant

    try:
        fields = layer.fields()
        # 声明长度 0 = 无上限（GeoJSON / 部分 GPKG 等），memory provider 不会
        # 拒绝长值，无需扫描，更不能为此整层拷贝（大图层会卡住主线程）。
        string_fields = [
            fd for fd in fields if fd.type() == QVariant.String and fd.length() > 0
        ]
        if not string_fields:
            return layer
        # 取每个字符串字段的实际最大长度。用 QgsAggregateCalculator（C++ 计算，
        # 数据库源还可能下推成 SQL）而不是 Python 逐要素循环：这一步在异步路径里
        # 仍跑在主线程，逐要素 Python 会把大图层的 UI 卡住数秒。
        max_lengths = {fd.name(): fd.length() for fd in string_fields}
        for name in max_lengths:
            value, ok = _maximum_string_length(layer, name)
            if ok and value is not None and int(value) > max_lengths[name]:
                max_lengths[name] = int(value)
        widened = {
            name
            for name, length in max_lengths.items()
            if length > fields.field(name).length()
        }
        if not widened:
            return layer

        wkb = layer.wkbType()
        if wkb in (Qgis.WkbType.Unknown, Qgis.WkbType.NoGeometry):
            return layer  # 几何类型缺失，算法自身会报错，不在此处理
        uri = QgsWkbTypes.displayString(wkb)
        crs = layer.crs()
        if crs.isValid() and crs.authid():
            uri += f"?crs={crs.authid()}"

        new_fields = QgsFields()
        for fd in fields:
            copy = QgsField(fd)
            if fd.name() in widened:
                copy.setLength(max_lengths[fd.name()])
            new_fields.append(copy)

        cleaned = QgsVectorLayer(uri, "输入图层（字段长度已修正）", "memory")
        cleaned.dataProvider().addAttributes(new_fields)
        cleaned.updateFields()

        features = []
        for source in layer.getFeatures():
            feature = QgsFeature(cleaned.fields())
            feature.setGeometry(source.geometry())
            feature.setAttributes(source.attributes())
            features.append(feature)
        ok, _ = cleaned.dataProvider().addFeatures(features)
        if not ok:
            logger.warning("字段长度修正后写入临时图层失败，退回原图层")
            return layer
        logger.info(
            "输入图层字段长度修正 %s 个字段后用于算法：%s",
            sorted(widened),
            {name: max_lengths[name] for name in widened},
        )
        return cleaned
    except Exception as exc:
        logger.warning("字段长度修正扫描失败，退回原图层: %s", exc, exc_info=True)
        return layer


__all__ = ["copy_with_widened_string_fields"]
