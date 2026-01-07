import subprocess
import sys

def gdb_to_postgis(gdb_path, pg_conn_str, options=None):
    """
    将 File Geodatabase 导入 PostgreSQL/PostGIS，并统一转换为4490坐标系

    :param gdb_path: .gdb 文件夹路径 (str)
    :param pg_conn_str: PostgreSQL 连接字符串 (str)，如 "host=localhost port=5432 dbname=mydb user=postgres password=123456"
    :param options: 额外 ogr2ogr 选项 (list)，如 ['-lco', 'LAUNDER=NO']
    """
    # 构造 ogr2ogr 命令，添加4490坐标系转换参数
    cmd = [
        'ogr2ogr',
        '-f', 'PostgreSQL',
        f'PG:{pg_conn_str}',
        gdb_path,
        '-lco', 'LAUNDER=NO',  # 保留字段名大小写
        '-lco', 'OVERWRITE=YES',  # 覆盖已存在表
        '-lco', 'GEOMETRY_NAME=shape',  # 几何字段名
        '-nlt', 'PROMOTE_TO_MULTI',  # 统一为 Multi 类型
        '-t_srs', 'EPSG:4490',  # 核心修改：转换为4490坐标系
        # '-s_srs', 'EPSG:4490',  # 兜底：若源数据无坐标系，默认按4490处理
        '-progress'  # 显示进度（可选）
    ]

    # 添加额外选项
    if options:
        cmd.extend(options)

    print("执行命令:", ' '.join(cmd))

    try:
        # 执行命令，捕获输出
        result = subprocess.run(
            cmd,
            check=True,  # 非零退出码时抛出异常
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8'
        )
        print("导入成功！数据已转换为4490坐标系")
        if result.stdout:
            print("输出:", result.stdout)
    except subprocess.CalledProcessError as e:
        print("导入失败:", e.stderr, file=sys.stderr)
        raise
    except FileNotFoundError:
        print("错误: 未找到 ogr2ogr 命令，请确保 GDAL 已安装并加入 PATH", file=sys.stderr)
        raise


# ============ 使用示例 ============
if __name__ == "__main__":
    GDB_PATH = f"D:\zsh\work\sdsj\湿地数据\全省湿地保护区.gdb"
    # 修改为目的数据库连接信息
    # PG_CONN = "host=localhost port=5432 dbname=postgis_30_sample user=postgres password=Zsh621002"
    PG_CONN = "host=localhost port=54321 dbname=postgres user=postgres password=Aa@123456"

    # 执行导入
    gdb_to_postgis(GDB_PATH, PG_CONN)