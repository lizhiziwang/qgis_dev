from osgeo import ogr



if __name__ == '__main__':
    path = r'D:\work\test.gdb'

    ds = ogr.Open(path,update=1)
    layer_num = ds.GetLayerCount()
    layers = []
    for layer_num in range(layer_num):
        layer = ds.GetLayer(layer_num)
        layers.append(layer)
    rows = ds.ExecuteSQL(f'select * from "{layers[1].GetName()}"')
    for row in rows:
        print(row)

    u_r = ds.CreateLayer("user_relation", None, ogr.wkbNone)
    if u_r is None:
        print("新建图层失败！GDAL<3.6或者GDB只读!")
    field_def = ogr.FieldDefn("id", ogr.OFTString)
    field_def.SetWidth(128)
    u_r.CreateField(field_def)

    field_def = ogr.FieldDefn("table_name", ogr.OFTString)
    field_def.SetWidth(128)
    u_r.CreateField(field_def)

    field_def = ogr.FieldDefn("type", ogr.OFTString)
    field_def.SetWidth(128)
    u_r.CreateField(field_def)

    feat_def = u_r.GetLayerDefn()
    feat = ogr.Feature(feat_def)
    # 设置属性
    feat.SetField("id", "1")
    feat.SetField("table_name", '123')
    feat.SetField("type", "image")
    u_r.CreateFeature(feat)

    ds.Destroy()