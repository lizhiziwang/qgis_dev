import string
import time

import psycopg2
import math
import threading

from osgeo import ogr
from osgeo.ogr import Layer
from psycopg2 import pool

''' 使用连接池 '''
conn_pool = pool.ThreadedConnectionPool(
    minconn=5,  # 最小空闲连接数
    maxconn=20,  # 最大连接数（根据数据库配置调整）
    host="localhost",
    dbname="postgis_30_sample",
    user="postgres",
    password="Zsh621002",
    port="5432"
)


def get_conn_with_timeout(pool, timeout=3600, interval=0.5):
    """
    从连接池获取连接，支持超时等待
    :param pool: 连接池对象
    :param timeout: 最大等待时间（秒）
    :param interval: 重试间隔（秒）
    :return: 数据库连接
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            return pool.getconn()
        except Exception:
            time.sleep(interval)
    raise TimeoutError(f"等待{timeout}秒后，连接池仍无可用连接")


# if __name__ == '__main__':
#     cons = []
#     for i in range(50):
#         var = get_conn_with_timeout(conn_pool, 10, 1)
#         cons.append(var)
#         print(var)


def create_connect():
    return psycopg2.connect(
        dbname="postgis_30_sample",
        user="postgres",
        password="Zsh621002",
        host="127.0.0.1",
        port="5432",
    )


def create_connect_esri():
    return psycopg2.connect(
        dbname="sde",
        user="sde",
        password="sde@2025",
        host="182.108.40.116",
        port="5432",
    )


def get_table_count(table_name: str, cur):
    sql = f"select count(1) from {table_name}"
    cur.execute(sql)
    r = cur.fetchone()
    return r[0]


def get_table_data(table_name: str, columns: str, cur, data_size, current):
    sql = f"select {columns} from {table_name} order by objectid limit {data_size} offset {current * data_size}; "
    cur.execute(sql)
    return cur.fetchall()


def get_table_columns(table_name: str, cur):
    sql = f"SELECT column_name, data_type FROM information_schema.columns WHERE " \
          f"table_schema = 'sde' AND table_name = '{table_name}';"
    cur.execute(sql)
    rows = cur.fetchall()
    re = []
    for row in rows:
        re.append(ColumnInfo(row[0], row[1]))
    return re


class ColumnInfo:
    def __init__(self, column_name: str, column_type: str):
        self.name = column_name
        self.type = column_type


def value_convert(column1: ColumnInfo, value):
    if value is None:
        return "null"
    if column1.type == "character varying" or column1.type == "timestamp without time zone":
        return "'" + str(value) + "'"
    if column1.type == "integer" or column1.type == "numeric":
        return value
    return value


def get_columns_str(columns):
    return ','.join(item.name for item in columns)


#
def build_insert_sql(source_data, target_table, columns_infos, columns_str):
    sql = f"insert into {target_table} ({columns_str}) values "
    values = []
    for index in range(len(source_data)):
        current_data = source_data[index]
        t2 = []
        for f_index in range(len(columns_infos)):
            try:
                if columns_infos[f_index].name == "shape":
                    var6 = current_data[f_index]
                    var6 = f"'{var6}'::geometry" if var6 != "EMPTY" else "null"
                    t2.append(var6)
                    continue
                t2.append(value_convert(columns_infos[f_index], current_data[f_index]))
            except Exception as e:
                t2.append("null")

        var3 = ','.join(str(item) for item in t2)
        values.append(f"({var3})")
    return sql + ",".join(str(item) for item in values)


''' 生成更新文件 '''


# if __name__ == '__main__':
#     con = create_connect()
#     cur = con.cursor()
#
#     sql = f"SELECT id_23 id,(thl_23-sum(n))*44/12 tan_chu_l from thjs_temp GROUP BY id_23,thl_23"
#     cur.execute(sql)
#     rows = cur.fetchall()
#     with open(r"D:\系统默认\桌面\23碳汇量更新.sql", mode="w+", encoding="UTF-8") as f:
#         for row in rows:
#             f.write(f"update lcstzhjc_2023 set tan_hui_l = {row[1]} where objectid = {row[0]};\n")


# if __name__ == '__main__':
#     con_local = create_connect()
#     cur_local = con_local.cursor()
#
#     con_tan_hui = create_connect_esri()
#     cur_tan_hui = con_tan_hui.cursor()
#     size = 2000
#
#     count = get_table_count("lcstzhjc_2023", cur_tan_hui)
#     columns_info = get_table_columns("lcstzhjc_2023", cur_tan_hui)
#     column = get_columns_str(columns_info)
#     column_insert = column.replace("shape", "st_astext(shape) shape")
#
#     page_num = math.ceil((count / size))
#
#     for i in range(page_num):
#         data = get_table_data("lcstzhjc_2023", column_insert, cur_tan_hui, size, i)
#         sql = build_insert_sql(data, "lcstzhjc_2023", columns_info, column)
#         cur_local.execute(sql)
#         con_local.commit()
#         print(1)
#
#     print("over")

def get_data_layer(path: string):
    ds = ogr.Open(path, 1)
    sd_layers = []
    for index in range(ds.GetLayerCount()):
        layer = ds.GetLayerByIndex(index)
        sd_layers.append(layer)
    return ds, sd_layers


# 从gdb文件创建数据库表
def create_db_table_from_layer(layer: Layer, con):
    layer_de = layer.GetLayerDefn()

    f_count = layer_de.GetFieldCount()
    fields = []
    for index in range(f_count):
        fd = layer_de.GetFieldDefn(index)
        fields.append({
            "name": fd.GetName(),
            "width": fd.GetWidth(),
            "alternativeName": fd.GetAlternativeName(),
            "type": fd.GetType(),
            "field": fd
        })
    fs = []
    for f in fields:
        pg_type = ogr_field_type_to_pg(f["field"])
        # "objectid" int4 NOT NULL,
        fs.append(f'"{f["name"]}" {pg_type} {"COLLATE pg_catalog.default" if "VARCHAR" in pg_type else ""}')
    # 添加几何字段 DROP TABLE table_name;
    fs.append('"geom" geometry(GEOMETRY)')

    var = ",".join(fs)

    var1 = f'DROP TABLE IF EXISTS "{layer.GetName()}";' \
           f'create table "{layer.GetName()}"({var});' \
           f'CREATE INDEX "{layer.GetName() + "GIS_INDEX"}" ON "{layer.GetName()}" USING GIST (geom);'
    cur = con.cursor()
    cur.execute(var1)

    con.commit()
    return fields


# ogr字段映射到pg数据库
def ogr_field_type_to_pg(field_defn) -> string:
    """
    将 OGR 字段定义映射为 PostgreSQL 字段类型字符串。

    参数:
        field_defn (ogr.FieldDefn): OGR 字段定义对象

    返回:
        str: 对应的 PostgreSQL 字段类型
    """
    ogr_type = field_defn.GetType()

    # 基本类型映射
    type_map = {
        ogr.OFTInteger: 'INTEGER',
        ogr.OFTInteger64: 'BIGINT',
        ogr.OFTReal: 'DOUBLE PRECISION',
        ogr.OFTString: 'TEXT',
        ogr.OFTBinary: 'BYTEA',
        ogr.OFTDate: 'DATE',
        ogr.OFTTime: 'TIME',
        ogr.OFTDateTime: 'TIMESTAMP',
        ogr.OFTIntegerList: 'INTEGER[]',
        ogr.OFTInteger64List: 'BIGINT[]',
        ogr.OFTRealList: 'DOUBLE PRECISION[]',
        ogr.OFTStringList: 'TEXT[]',
    }

    pg_type = type_map.get(ogr_type, 'TEXT')  # 默认回退为 TEXT

    # 可选增强：对字符串类型，若宽度较小，可用 VARCHAR
    if ogr_type == ogr.OFTString:
        width = field_defn.GetWidth()
        if 0 < width <= 255:
            pg_type = f'VARCHAR({width})'
        # 若宽度为0或很大，保留 TEXT（TEXT 在 PG 中性能通常优于 VARCHAR）

    # 可选：对 Real 类型，若指定了精度，可考虑 NUMERIC
    # 注意：OGR 的 GetPrecision() 对 Real 字段表示小数位数

    if ogr_type == ogr.OFTReal:
        width = field_defn.GetWidth()
        precision = field_defn.GetPrecision()
        # 示例：如果用户期望高精度（如财务数据），可转为 NUMERIC
        # 但需谨慎，因 OGR 的精度语义与数据库不完全一致
        # 这里保持为 DOUBLE PRECISION（更通用）
        pass

    return pg_type


# 构建插入sql
def build_sql_from_gdb(layer: Layer, field: []) -> []:
    layer.ResetReading()
    quoted_names = [f'"{item["name"]}"' for item in field]

    # 第二步：拼接成 (col1,col2,...) 格式
    columns = f"({','.join(quoted_names)})"
    features = []
    count = layer.GetFeatureCount()
    for index in range(count):
        feature = layer.GetNextFeature()
        for field in field:
            attr = feature.GetField(field["name"])
            print(1)

# def conver_value(f_type, f_value):

if __name__ == '__main__':
    path = f"D:\work\湿地数据\全省湿地保护区.gdb"
    layers = get_data_layer(path)
    con = create_connect()
    fields = create_db_table_from_layer(layers[1][0], con)
    build_sql_from_gdb(layers[1][0], fields)
    print(1)
    # 获取湿地公园内的图斑
    # tb_layer = layers['zrbhdbhtb1']
    # gjsdgy_layer = layers["国家湿地公园"]
    # sjsdgy_layer = layers["省级湿地公园"]

    # tb_layer.ResetReading()
    # gjsdgy_layer.ResetReading()
    # sjsdgy_layer.ResetReading()

    # sdgy_tb = []
    # for feat_idx, feature in enumerate(tb_layer):
    #     print(1)
    #
    #
    # print(layers)
