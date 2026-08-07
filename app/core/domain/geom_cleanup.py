"""要素/几何清洗共用工具（领域层，无 PyQt5 顶层依赖）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer

logger = get_logger(__name__)


def copy_with_widened_string_fields(layer: "QgsVectorLayer") -> "QgsVectorLayer":
    """把 String 字段长度放宽到实际最大值，返回修正后的临时图层。

    GDB/ArcGIS 导出的图层常有「字段声明长度 < 实际数据长度」的脏数据：
    短代码字段（如 XIANG 声明 3）里存了字面量 ``'NULL'``（4 字符）。
    QGIS memory provider 严格校验字符串长度，超长值在 sink 写入时被拒绝，
    导致 ``native:buffer`` / 叠加算法报「无法将要素写入 OUTPUT」。此函数扫描一遍数据，
    把超长字段的长度放宽到实际最大值，并拷贝成临时 memory 图层；
    无需修正时原样返回，不产生额外开销。
    """
    from PyQt5.QtCore import QVariant
    from qgis.core import (
        Qgis,
        QgsFeature,
        QgsField,
        QgsFields,
        QgsVectorLayer,
        QgsWkbTypes,
    )

    try:
        fields = layer.fields()
        string_fields = [fd for fd in fields if fd.type() == QVariant.String]
        if not string_fields:
            return layer
        # 扫描一遍，取每个字符串字段的实际最大长度
        max_lengths = {fd.name(): fd.length() for fd in string_fields}
        for feature in layer.getFeatures():
            for name in max_lengths:
                value = feature.attribute(name)
                if value is not None and value != "":
                    actual = len(str(value))
                    if actual > max_lengths[name]:
                        max_lengths[name] = actual
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("字段长度修正扫描失败，退回原图层: %s", exc, exc_info=True)
        return layer


__all__ = ["copy_with_widened_string_fields"]
