# from typing import Optional
#
#
# class Point:
#     def __init__(self, x: float, y: float, data):
#         self.x = x
#         self.y = y
#         self.data = data
#
#     def __repr__(self):
#         return f"Point({self.x}, {self.y}, {self.data})"
#
# class Rectangle:
#     def __init__(self, x1, y1, x2, y2, data):
#         """
#         Args:
#             x1: xmin
#             y1: ymin
#             x2: xmax
#             y2: ymax
#             data:
#         """
#         self.xmin = x1
#         self.ymin = y1
#         self.xmax = x2
#         self.ymax = y2
#         self.data = data
#         self.width = x2 - x1
#         self.height = y2 - y1
#
#     def intersect(self, rect: "Rectangle"):
#         """
#         A 在 B 的左侧：x_2 \le x_3
#         A 在 B 的右侧：x_1 \ge x_4
#         A 在 B 的下方：y_2 \le y_3
#         A 在 B 的上方：y_1 \ge y_4
#         取反就是相交
#         Args:
#             rect:
#
#         Returns: bool
#
#         """
#         return (not self.xmax < rect.xmin or self.xmin > rect.xmax
#                 or self.ymax < rect.ymin or self.ymin > rect.ymax)
#
#     def contains(self, rect: "Rectangle"):
#         """
#
#         Args:
#             rect:
#
#         Returns:
#
#         """
#         return (rect.xmin>=self.xmin and rect.xmax<= self.xmax and
#                 rect.ymin>=self.ymin and rect.ymax<=self.ymax)
#
# class Quadtree:
#     def __init__(self):
#         self.boundary = []
#         self.capacity = 4
#         self.boundary = None
#
#         self.is_divided = False
#         self.nw: Optional[Quadtree] = None #西北
#         self.ne: Optional[Quadtree] = None # 东北
#         self.sw: Optional[Quadtree] = None # 西南
#         self.se: Optional[Quadtree] = None # 东南
#         self.datas=[]
#
#     def insert(self, point: Point, fid: int):
#         self.datas.append({"id":fid, "point":point})
#
#     def init_boundary(self):
#         self.datas.sort(key=lambda e: e["point"].x)
#         x_t = [self.datas[0].x, self.datas[-1]["point"].x]
#         self.datas.sort(key=lambda e: e["point"].y)
#         y_t = [self.datas[0].y, self.datas[-1]["point"].y]
#         self.boundary = [x_t[0], y_t[0], x_t[-1], y_t[-1]]
#
#     def build_tree(self):
#         self.init_boundary()
#
#     def skip(self, items):
#         # 每次往下判断然后清除上一步的数据
#         if len(items) <= self.capacity:
#             return
#
#         # 划分网格
#         x_c = (self.boundary[0]+self.boundary[2])/2
#         y_c = (self.boundary[1]+self.boundary[3])/2
#         nw = Quadtree()

# 给定 n 个非负整数表示每个宽度为 1 的柱子的高度图，计算按此排列的柱子，下雨之后能接多少雨水



import networkx as nx
from PyQt5.QtCore import QVariant
from osgeo import gdal, ogr
from qgis._core import QgsVectorLayer, QgsSpatialIndex, QgsGeometry, QgsWkbTypes, QgsPoint, QgsField


# G = nx.Graph()
#
# # 先添加基础节点（确保边的节点存在，无节点时会自动创建节点）
# G.add_nodes_from(["A", "B", "C", "D", "E"])
#
# # 方式1：单个添加边（无权重）
# G.add_edge("A", "B")
# G.add_edge("B", "C")
#
# # 方式2：批量添加边（传入边的元组列表，无权重）
# G.add_edges_from([("C", "D"), ("D", "E"), ("A", "C"), ("B", "D")])
#
# # 方式3：添加带权重的边（最短路径分析关键，用字典指定weight属性）
# # 单个加权边
# G.add_edge("A", "D", weight=2.5)
# # 批量加权边（元组格式：(起点, 终点, {属性名: 属性值})）
# G.add_edges_from([("B", "E", {"weight": 1.8}),
#                   ("C", "E", {"weight": 3.2})])
#
# # 查看图中所有边（返回边视图，可转为列表）
# print("图中所有边：", list(G.edges()))
# # 查看边的属性（加权边的核心）
# print("边A→D的权重：", G.edges["A", "D"]["weight"])
#
# path = nx.shortest_path(G, source="A", target="D", weight="weight")
# print(path)

# (id, x, y)
from scipy.spatial import cKDTree
import numpy as np

def add_fields(layer: QgsVectorLayer):
    layer.startEditing()

    source = QgsField("source_ps", QVariant.Int)
    target = QgsField("target_ps", QVariant.Int)

    layer.dataProvider().addAttributes([source, target])
    layer.updateFields()

    # 提交更改
    layer.commitChanges()


def build_network(path: str, layer_name: str):
    vlayer = QgsVectorLayer(path, layer_name, "ogr")

    index = QgsSpatialIndex(vlayer.getFeatures())
    p_id = 0

    for feat in vlayer.getFeatures():
        fid = feat.id()
        geom = feat.geometry()
        print(f"\n=== 要素FID={fid} 的相交要素 ===")

        ps = get_each_linestring_start_end(geom)
        for p in ps:
            qgs_point = QgsPoint(p.x(), p.y())

            # 2. 核心步骤2：QgsPoint → QgsGeometry（几何对象，具备缓冲区方法）
            point_geometry = QgsGeometry.fromPointXY(qgs_point)
            buffer_geometry = point_geometry.buffer(
                distance=0.00000001,  # 缓冲区半径
                segments=8  # 平滑度边数
            )
            intersect_fids = index.intersects(buffer_geometry.boundingBox())


        # intersect_fids = index.intersects(geom.boundingBox())
        # f_ts = []
        #
        # for intersect_fid in intersect_fids:
        #     if intersect_fid == fid:
        #         continue
        #
        #     t_f = vlayer.getFeature(intersect_fid)
        #     if not geom.intersects(t_f.geometry()):
        #         continue
        #     f_ts.append(t_f)

def is_multi_line_string(geom: QgsGeometry) -> bool:
    return geom.wkbType() == QgsWkbTypes.MultiLineString and not geom.isEmpty()


def get_each_linestring_start_end(geom: QgsGeometry) -> list:
    """
    获取MultiLineString中每个子LineString的首尾节点
    :param geom: 输入的MultiLineString类型QgsGeometry
    :return: 嵌套列表，每个元素为(子线串索引, 起始节点, 结束节点)
    """
    # 1. 验证几何类型
    if not is_multi_line_string(geom):
        raise ValueError("输入几何不是有效的MultiLineString类型")

    # 2. 转换为MultiLineString原生结构（核心步骤）
    multi_line = geom.asMultiPolyline()
    result = []

    # 3. 遍历每个子LineString，提取首尾节点
    for line_idx, linestring in enumerate(multi_line):
        if len(linestring) == 0:
            # 空线串，跳过
            result.append((line_idx, None, None))
            continue

        # 提取首尾节点（linestring是QgsPoint的列表）
        start_point = linestring[0]  # 第一个节点
        end_point = linestring[-1]  # 最后一个节点

        # 存入结果（可按需转换为坐标元组，或保留QgsPoint对象）
        result.append((
            start_point,
            end_point
        ))

    return result


if __name__ == '__main__':
    # ln = "gis_osm_waterways_free_1"
    # path = f"D:/data/zhejiang-latest-free.shp/gis_osm_waterways_free_1.shp|layername={ln}"
    # print(f"当前GDAL版本：{gdal.__version__}")
    # build_network(path,ln)

    # 创建一些随机点
    points = np.random.rand(10, 2)

    # 创建 cKDTree
    tree = cKDTree(points)

    # 最近邻搜索
    point = [0.5, 0.5]
    distance, index = tree.query(point)
    print(f"最近的点索引: {index}, 距离: {distance}")

    # 范围搜索
    radius = 0.2
    indices = tree.query_ball_point(point, radius)
    print(f"在半径 {radius} 内的点索引: {indices}")

