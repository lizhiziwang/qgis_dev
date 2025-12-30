import math

from qgis._core import QgsVectorLayer, QgsSpatialIndex, QgsFeatureRequest
from component.Logger import *

logger = setup_logger(__name__)

class SpaceCheckResult:
    def __init__(self, result: bool, info=None):
        self.result = result
        self.info = info if info is not None else set()

    def __repr__(self):
        return f"SpaceCheckResult(result={self.result}, info_count={len(self.info)}, info={self.info})"

    def __str__(self):
        return f"SpaceCheckResult(result={self.result}, info_count={len(self.info)}, info={self.info})"



def layer_self_intersect(check_layer: QgsVectorLayer, param = None) -> SpaceCheckResult:
    """
    检查图层内的要素是否有自相交
    :param check_layer:
    :param param:
    :return:
    """
    try:
        if param is None:
            param = {"where": "1=1"}

        info = []
        result = True
        request = QgsFeatureRequest().setFilterExpression(param["where"])

        for feature in check_layer.getFeatures(request):
            geom = feature.geometry()
            if geom.isEmpty():
                continue

            # 验证几何
            errors = geom.validateGeometry()

            # 检查是否有自相交错误
            for error in errors:
                error_msg = error.what()
                if any(keyword in error_msg for keyword in ['intersect', 'self-intersect', 'self intersection']):
                    result = False
                    info.append({
                        "id": feature.id(),
                        "location": (error.where().x(), error.where().y())
                    })

        return SpaceCheckResult(result, info=info)
    except Exception as e:
        logger.error(e)
        logger.error("sql条件存在异常，请检查")
        return SpaceCheckResult(False, info={"message":"过滤条件存在异常！"})


def layer_feature_cover(check_layer: QgsVectorLayer, param=None) -> SpaceCheckResult:
    """
    检查图层内的要素间是否存在重复
    Args:
        check_layer:
        param:
    Author:
        zsh
    Returns:
    """
    try:
        if param is None:
            param = {"where": "1=1"}
        if not check_layer or not check_layer.isValid():
            raise Exception("图层无效！")

        result = True
        recorded_pairs = set()
        # 获取索引
        build_space_index(check_layer)
        index = check_layer.__index__
        request = QgsFeatureRequest().setFilterExpression(param["where"])

        for item in check_layer.getFeatures(request):
            fid = item.id()
            geom = item.geometry()
            bbox = geom.boundingBox()

            target_feature = index.intersects(bbox)
            for feature_id in target_feature:
                if feature_id <= fid:
                    continue
                target = check_layer.getFeature(feature_id).geometry()
                # 相交排除邻接
                if geom.intersects(target) and not geom.touches(target):
                    result = False
                    # 存储有序配对（确保一致性）
                    recorded_pairs.add((fid, feature_id))

        return SpaceCheckResult(result, info=recorded_pairs)
    except Exception as e:
        logger.error(e)
        logger.error("sql条件存在异常，请检查")
        return SpaceCheckResult(False, info={"message": "过滤条件存在异常！"})


def layer_long_surface(check_layer: QgsVectorLayer, param=None) -> SpaceCheckResult:
    """
    Args:
        check_layer:
        param:

    Returns:

    """
    try:
        if param is None:
            param = {"ratio": 5, "where": "1=1"}

        result = True
        info = []
        request = QgsFeatureRequest().setFilterExpression(param["where"])

        for feature in check_layer.getFeatures(request):
            geom = feature.geometry()
            geom.convertToMultiType()
            if geom.isEmpty():
                continue
            tmp = geom.orientedMinimumBoundingBox()
            if tmp is None:
                continue
            g = tmp[0]

            a = g.asPolygon()
            rect_points = []
            for point in a[0]:
                rect_points.append(point)

            edge1 = math.sqrt(
                (rect_points[1].x() - rect_points[0].x()) ** 2 + (rect_points[1].y() - rect_points[0].y()) ** 2)
            edge2 = math.sqrt(
                (rect_points[2].x() - rect_points[1].x()) ** 2 + (rect_points[2].y() - rect_points[1].y()) ** 2)
            ratio = edge1 / edge2 if edge1 / edge2 > 1 else edge2 / edge1
            if ratio > param["ratio"]:
                result = False
                info.append({
                    "id": feature.id(),
                    "radio": ratio
                })

        return SpaceCheckResult(result, info=info)
    except Exception as e:
        logger.error(e)
        return SpaceCheckResult(False, info={"message":"出现异常！"})




def build_space_index(vec_layer: QgsVectorLayer):
    """
    Args:
        vec_layer:

    Returns:
        返回指定图层构建出的空间索引
    """
    if not vec_layer or not vec_layer.isValid():
        raise Exception("图层无效！")
    index = QgsSpatialIndex()
    features = [f for f in vec_layer.getFeatures() if f.hasGeometry() and not f.geometry().isEmpty()]
    index.addFeatures(features)
    # 维护一个索引字段
    vec_layer.__index__ = index
    # for feat in vec_layer.getFeatures():
    #     if feat.hasGeometry() and not feat.geometry().isEmpty():
    #         index.insertFeature(feat)

    # return index


if __name__ == '__main__':
    file_path = f"D:\昌江区_景德镇市昌江区鲇鱼山镇上徐村肉羊养殖基地项目_林地审核审批数据库_20250825.gdb"

    fc_name = "昌江区_景德镇市昌江区鲇鱼山镇上徐村肉羊养殖基地项目_林地图斑_20250825"
    source = f"{file_path}|layername={fc_name}"
    layer = QgsVectorLayer(source, fc_name, "ogr")
    # c_result = layer_self_intersect(layer)
    c_result = layer_long_surface(layer)
    print(c_result)
