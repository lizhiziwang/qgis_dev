import os, sys

current_file = os.path.abspath(__file__)
current_dir = os.path.dirname(current_file)
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

from component.check.DataCheck import *
from component.check.SpaceDataCheck import *

# if __name__ == '__main__':
#     file_path = f"D:\work\龙南市_赣州市恒兴矿业有限公司龙南里仁风车咀矿区瓷土矿开采项目_2025111\龙南市_赣州市恒兴矿业有限公司龙南里仁风车咀矿区瓷土矿开采项目_20251117.gdb"
#
#     fc_name = "龙南市_赣州市恒兴矿业有限公司龙南里仁风车咀矿区瓷土矿开采项目_林地图斑_20251117"
#     source = f"{file_path}|layername={fc_name}"
#     layer = QgsVectorLayer(source, fc_name, "ogr")
#     param = CheckParam(layer=layer, check_name="非空校验",
#                        field_name=["ZRBHQ_DJ"],
#                        where="(ZRBHQ_DJ IN ('国家级', '省级', '其他') AND ZRBHQ_MC IS NOT NULL) OR (ZRBHQ_DJ = '无' AND ZRBHQ_MC = '无') OR (ZRBHQ_DJ IS NULL AND ZRBHQ_MC IS NULL) or (ZRBHQ_DJ = '' AND ZRBHQ_MC = '') or (ZRBHQ_DJ = ' ' AND ZRBHQ_MC = ' ')",
#                        check_id=132132)
#
#     n_check = check_service[2]
#
#     features = n_check.check(param)
#     feat_list = list(features)
#     count = len(feat_list)
#     print(f"要素数量：{count}")
#     for item in feat_list:
#         print(item.attribute("OBJECTID"))

if __name__ == '__main__':
    file_path = f"D:\昌江区_景德镇市昌江区鲇鱼山镇上徐村肉羊养殖基地项目_林地审核审批数据库_20250825.gdb"

    fc_name = "昌江区_景德镇市昌江区鲇鱼山镇上徐村肉羊养殖基地项目_林地图斑_20250825"
    source = f"{file_path}|layername={fc_name}"
    layer = QgsVectorLayer(source, fc_name, "ogr")
    order_by_clause = QgsFeatureRequest.OrderByClause('$id', ascending=False)

    request = (QgsFeatureRequest().setFilterExpression("1 =1")
               .setLimit(5)
               .setOrderBy(QgsFeatureRequest.OrderBy([order_by_clause])))
    data = list(layer.getFeatures(request))
    print(len(data))
    for item in data:
        print(item.id())
    # c_result = layer_self_intersect(layer)
    # c_result = layer_feature_cover(layer)
    # print()
    # print(c_result)