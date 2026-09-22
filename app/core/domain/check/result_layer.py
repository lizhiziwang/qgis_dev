"""质检结果落层：把命中要素另存为新的内存图层（弹窗里的「输出图层」）。

质检本身只回要素 id（:attr:`CheckResult.failed_feature_ids`），要不要落层由调用方
决定。这里集中实现"按 id 抽取要素、保留字段 / 几何类型 / 坐标系"，供各质检弹窗复用，
避免每个弹窗各写一份拷贝逻辑。
"""

from __future__ import annotations

from collections.abc import Iterable

from qgis.core import QgsFeature, QgsFeatureRequest, QgsVectorLayer, QgsWkbTypes

from app.infra.logging import get_logger

logger = get_logger(__name__)


def build_result_layer(
    source: QgsVectorLayer,
    feature_ids: Iterable[int],
    name: str,
) -> QgsVectorLayer:
    """把 ``source`` 中 ``feature_ids`` 对应的要素拷到新的内存图层并返回。

    字段、几何类型、坐标系全部沿用源图层；**不**写入工程（由调用方 addMapLayer）。

    :raises ValueError: ``feature_ids`` 为空。
    :raises RuntimeError: 图层创建或要素写入失败。
    """
    ids = [int(fid) for fid in feature_ids]
    if not ids:
        raise ValueError("没有命中要素，无法生成输出图层")

    uri = QgsWkbTypes.displayString(source.wkbType())
    crs = source.crs()
    if crs.isValid() and crs.authid():
        uri += f"?crs={crs.authid()}"

    result = QgsVectorLayer(uri, name, "memory")
    if not result.isValid():
        raise RuntimeError(f"输出图层创建失败：{name}")

    provider = result.dataProvider()
    if not provider.addAttributes(list(source.fields())):
        raise RuntimeError(f"输出图层字段创建失败：{name}")
    result.updateFields()

    # 逐要素新建（而不是直接拷贝 QgsFeature）：连带丢掉源要素 id，交给 memory
    # provider 重新编号，避免与源图层 fid 冲突。
    #
    # 注意：**不要**对大 fid 集合用 ``QgsFeatureRequest.setFilterFids``。OGR/GPKG
    # 后端会把 fid 过滤退化成 ``O(n×m)`` 的逐行判定：实测 2 万要素、4148 个 id 的
    # gpkg 图层要 7 分钟以上。改成“全表扫描 + set 判定”后同一图层 <1s。只有 id
    # 集合很小、而图层又明显更大时，fid 过滤才更划算。
    id_set = {int(fid) for fid in ids}
    count = source.featureCount()
    use_fid_filter = (count < 0 and len(id_set) <= 200) or (count > len(id_set) * 500)
    if use_fid_filter:
        iterator = source.getFeatures(QgsFeatureRequest().setFilterFids(list(id_set)))
    else:
        iterator = (feat for feat in source.getFeatures() if feat.id() in id_set)

    features = []
    for feat in iterator:
        copy = QgsFeature(result.fields())
        copy.setGeometry(feat.geometry())
        copy.setAttributes(feat.attributes())
        features.append(copy)
    if not provider.addFeatures(features):
        raise RuntimeError(f"输出图层写入失败：{name}")
    result.updateExtents()
    logger.info("质检结果落层：%s 的 %d 个要素 → %s", source.name(), len(features), name)
    return result


__all__ = ["build_result_layer"]
