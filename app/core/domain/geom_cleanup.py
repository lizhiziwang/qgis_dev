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


def copy_with_widened_string_fields(
    layer: QgsVectorLayer,
    *,
    only_selected: bool = False,
) -> QgsVectorLayer:
    """把 String 字段长度放宽到实际最大值，返回修正后的临时图层。

    GDB/ArcGIS 导出的图层常有「字段声明长度 < 实际数据长度」的脏数据：
    短代码字段（如 XIANG 声明 3）里存了字面量 ``'NULL'``（4 字符）。
    QGIS memory provider / OGR 输出驱动会严格校验字符串长度，超长值在写入
    时被拒绝，导致 ``native:buffer`` / 叠加算法 / 格式转换报错。此函数扫描
    字段实际最大长度（C++ 聚合，不逐要素进 Python），把超长字段放宽后拷贝成
    临时 memory 图层；无需修正时原样返回，不产生额外开销。声明长度 0 表示
    无上限（GeoJSON 等），不在修正范围内。

    注意：数据本身有超长值时仍需逐要素拷贝一次（字段声明不可原地修改），
    这一步在主线程完成；干净数据只做聚合扫描，不会创建图层。

    Args:
        layer: 待检查的输入图层。
        only_selected: 为 ``True`` 时只拷贝选中要素，并在结果图层中选中它们，
            以便调用方继续按 ``onlySelectedFeatures`` 写出。
    """
    from qgis.core import (
        Qgis,
        QgsFeature,
        QgsFeatureRequest,
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

        if only_selected:
            request = QgsFeatureRequest().setFilterFids(layer.selectedFeatureIds())
        else:
            request = QgsFeatureRequest()
        features = []
        for source in layer.getFeatures(request):
            feature = QgsFeature(cleaned.fields())
            feature.setGeometry(source.geometry())
            feature.setAttributes(source.attributes())
            features.append(feature)
        ok, _added = cleaned.dataProvider().addFeatures(features)
        if not ok:
            logger.warning("字段长度修正后写入临时图层失败，退回原图层")
            return layer
        if only_selected:
            # 临时图层只装了选中要素，重新全选，供 onlySelectedFeatures 写出
            cleaned.selectAll()
        logger.info(
            "输入图层字段长度修正 %s 个字段后用于算法：%s",
            sorted(widened),
            {name: max_lengths[name] for name in widened},
        )
        return cleaned
    except Exception as exc:
        logger.warning("字段长度修正扫描失败，退回原图层: %s", exc, exc_info=True)
        return layer


def copy_with_valid_polygons(layer: QgsVectorLayer) -> QgsVectorLayer:
    """去掉空几何、修复无效多边形，返回可直接用于掩膜 / 裁剪的临时图层。

    ``gdalwarp -cutline`` 对掩膜的要求比 QGIS 画布严格：
    - 有要素几何为空 → ``Cutline feature without a geometry``；
    - 多边形自相交等无效几何 → ``Cutline polygon is invalid``；
    QGIS 的「按掩膜裁剪」没有跳过 / 修复选项，只能在执行前清洗。

    掩膜只用到几何，因此结果图层**不拷贝属性字段**（也省掉大掩膜的逐要素
    属性开销）。全部要素都有效时原样返回，不产生额外图层。

    Args:
        layer: 掩膜矢量图层（多边形）。

    Returns:
        清洗后的 memory 图层；无需清洗或清洗失败时返回原图层。
    """
    from qgis.core import (
        Qgis,
        QgsFeature,
        QgsGeometry,
        QgsVectorLayer,
        QgsWkbTypes,
    )

    try:
        if QgsWkbTypes.geometryType(layer.wkbType()) != Qgis.GeometryType.Polygon:
            return layer

        geometries = []
        dropped = repaired = skipped = 0
        for source in layer.getFeatures():
            geometry = source.geometry()
            if geometry.isNull() or geometry.isEmpty():
                dropped += 1
                continue
            if not geometry.isGeosValid():
                fixed = geometry.makeValid()
                if (
                    fixed is None
                    or fixed.isEmpty()
                    or not fixed.isGeosValid()
                ):
                    skipped += 1
                    continue
                geometry = fixed
                repaired += 1
            # 深拷贝：源要素在下一轮循环就被释放，直接引用会悬垂
            geometries.append(QgsGeometry(geometry))

        if not dropped and not repaired and not skipped:
            return layer
        if not geometries:
            logger.warning("%s：所有要素几何均无效，退回原图层", layer.name())
            return layer

        wkb = QgsWkbTypes.multiType(layer.wkbType())
        uri = QgsWkbTypes.displayString(wkb)
        crs = layer.crs()
        if crs.isValid() and crs.authid():
            uri += f"?crs={crs.authid()}"
        cleaned = QgsVectorLayer(uri, f"{layer.name()}（几何已修复）", "memory")
        features = []
        for geometry in geometries:
            feature = QgsFeature()
            feature.setGeometry(geometry)
            features.append(feature)
        ok, _added = cleaned.dataProvider().addFeatures(features)
        if not ok:
            logger.warning("%s：清洗后写入临时图层失败，退回原图层", layer.name())
            return layer
        logger.info(
            "%s 掩膜几何清洗：跳过空几何 %d 个、修复无效几何 %d 个、无法修复跳过 %d 个",
            layer.name(),
            dropped + skipped,
            repaired,
            skipped,
        )
        return cleaned
    except Exception as exc:  # noqa: BLE001
        logger.warning("掩膜几何清洗失败，退回原图层: %s", exc, exc_info=True)
        return layer


def copy_field_aliases(source: QgsVectorLayer, target: QgsVectorLayer) -> None:
    """按**字段名**把 ``source`` 的字段别名补到 ``target``。

    ``native:*`` 算法的 memory sink、``QgsVectorFileWriter`` 的部分格式输出
    不携带字段别名（实测 ``QgsField.alias()`` 为空），转换后用户会以为字段被
    改名。不能按索引补：GPKG 输出前面可能多一个 ``fid``。别名只是展示信息，
    失败只记日志，不影响结果。
    """
    try:
        aliases = {
            field.name(): field.alias()
            for field in source.fields()
            if field.alias()
        }
        if not aliases:
            return
        target_fields = target.fields()
        for index in range(target_fields.count()):
            field = target_fields.at(index)
            alias = aliases.get(field.name())
            if alias and field.alias() != alias:
                target.setFieldAlias(index, alias)
    except Exception as exc:
        logger.warning("恢复字段别名失败：%s", exc, exc_info=True)


__all__ = [
    "copy_field_aliases",
    "copy_with_valid_polygons",
    "copy_with_widened_string_fields",
]
