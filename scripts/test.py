import uuid
from typing import Any

import pysqlite3 as sqlite3

# 1.创建/打开db文件，如果不存在自动生成 test_spatial.db
conn = sqlite3.connect("test_spatial.db")
conn.enable_load_extension(True)


cursor = conn.cursor()

# 2.启用SpatiaLite，初始化空间元数据表（必须执行！）
def init_sqlite3_spatial():
    cursor.execute("SELECT load_extension('mod_spatialite')")
    cursor.execute("SELECT InitSpatialMetaData(1);")
    cursor.execute('''
        CREATE TABLE "user_relation" (
          "id" text NOT NULL,
          "table_name" TEXT,
          "type" TEXT,
          PRIMARY KEY ("id")
        );
    ''')

def create_spatial_table_data():
    # 3.创建空间表，两种写法
    # 方式A：普通表，增加GEOMETRY字段（SpatiaLite标准写法）
    cursor.execute('''
    CREATE TABLE poi (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL
    );
    ''')

    # 给表添加几何列，SRID=4326(WGS84经纬度)
    cursor.execute("SELECT AddGeometryColumn('poi', 'geom', 4326, 'POINT', 2);")
    # 创建空间索引（非常重要，加快空间查询）
    cursor.execute("SELECT CreateSpatialIndex('poi', 'geom');")

    # 4.插入一条点数据 经度,纬度
    cursor.execute('''
    INSERT INTO poi(name,geom) VALUES (?,ST_GeomFromText(?,4326))
    ''', ("南昌", "POINT(115.86 28.68)"))

    conn.commit()


def query_all_table_name() -> dict[Any, Any]:
    rows = cursor.execute("SELECT table_name,type FROM user_relation")
    s,d= [] ,[]
    for row in rows:
        if row[1] == 'spatial':
            s.append(row[0])
        if row[1] == 'dict':
            d.append(row[0])
    return {"spatial":s,"dict":d}


def add_domain_table(tn: str, type_name: str)->None:
    cursor.execute('''
        insert into user_relation (id,table_name,type) values (?,?,?)
    ''',(str(uuid.uuid4()),tn,type_name))
    conn.commit()


if __name__ == '__main__':
    add_domain_table("line", "spatial")
    a = query_all_table_name()
    print(a)