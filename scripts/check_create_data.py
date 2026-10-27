"""「创建数据」自检 CLI：本地库容器 + 库里建图层（无界面）。

验证 :mod:`app.core.domain.create_data.library` 的两个服务，重点是**往已有库里加
图层绝不能伤到库里原有的东西**——这正是 OGR 写容器格式的默认行为会踩的坑
（``actionOnExistingFile`` 默认 ``CreateOrOverwriteFile`` = 整库删掉重建），
所以走的是本工程的 SQL 建表原语。用例：

1. ``container-gpkg``      —— 建空 GeoPackage 库：系统表齐、可被数据管理登记（空清单）
2. ``container-spatialite``—— 建空 SpatiaLite 库
3. ``layer-in-db``         —— ★往已有库建第二个图层后，第一个图层的 3 个要素必须**还在**，
                              两个图层都能打开、都被登记进 ``user_relation``
4. ``duplicate-layer``     —— 库里已有同名图层时拦下，且原图层内容不变
5. ``non-seed-srid``       —— 用非种子坐标系（EPSG:4547）建图层（验证 SRS 补种）

向导第二步「数据字段设置」那一套（字段模型 / 落盘口径 / 模板文件 / 向导步骤）也在
这里自检 —— 它是「界面填的字段结构真的进了数据源」的唯一把关：

6. ``wizard-steps``        —— 页脚随 (页签, 步骤) 切换、上一步保留字段、第 1 步不许翻页
7. ``field-model``         —— 类型目录映射、字典按文本建、四条校验文案逐字、spec 可哈希
8. ``file-with-fields``    —— ★建独立文件带字段：能落的落进文件，**落不下的必须提示**
                              （Shapefile 不存别名 / NOT NULL；GeoJSON 空文件连字段都存不下）
9. ``into-db-with-fields`` —— ★库内建带字段图层：``TEXT(50)`` / 真 NOT NULL /
                              gpkg 别名进 ``gpkg_data_columns``（SpatiaLite 没有，要提示）
10. ``field-template``     —— 生成空 .xlsx 模板再读回来一致；表头两种形态都认
11. ``fields-from-layer``  —— 从已有图层复制字段结构（含别名 / 长度 / 可空 / 字典）
12. ``field-persistence-notes``—— 全落下就一条都不提示（不做无谓的告警）

用法::

    python-qgis.bat -u scripts/check_create_data.py
    python-qgis.bat -u scripts/check_create_data.py --only layer-in-db --clean

产物默认留在 ``.tmp/create_data/``（已 gitignore），可以直接拖进 QGIS 看。
退出码 0 = 全部通过。

复用 ``run_sqlite_test.py`` 的 QGIS 装配与图层校验助手（那边是本仓库既有的
SQLite 家族自检脚本），只借函数、不跑它的用例。
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

import _bootstrap_path  # noqa: F401  把仓库根加进 sys.path

# 输出全是中文：Windows 下 stdout 默认走 cp936，重定向到管道/文件会乱码。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parents[1]

# 同目录的既有自检脚本：借 init_qgis / make_layer / check_layer（它自己带 __main__ 守卫）
from run_sqlite_test import (  # noqa: E402
    CRS,
    POINTS,
    check_layer,
    cleanup,
    init_qgis,
    make_layer,
)

from app.core.domain.create_data import (  # noqa: E402
    CreateContainerService,
    CreateContainerSpec,
    CreateLayerInDbService,
    CreateLayerInDbSpec,
    CreateLibraryError,
    FieldSpec,
)

#: 建空库的格式（显示名, 驱动, 扩展名）
_FORMATS: dict[str, tuple[str, str]] = {
    "gpkg": ("GeoPackage", "GPKG"),
    "spatialite": ("SQLite / SpatiaLite", "SQLite"),
}


def create_container(db: Path, kind: str) -> str:
    """按 ``kind``（``gpkg`` / ``spatialite``）建一个空库，返回路径。"""
    label, driver = _FORMATS[kind]
    return CreateContainerService().create(
        CreateContainerSpec(
            path=str(db.parent),
            file_name=db.name,
            data_format=label,
            driver=driver,
            extension=db.suffix,
        )
    )


def add_empty_layer(db: Path, layer_name: str, geometry_type: str = "Point", crs: str = CRS):
    """往库里建一个空图层，返回回读的图层对象。"""
    return CreateLayerInDbService().create(
        CreateLayerInDbSpec(
            db_path=str(db),
            layer_name=layer_name,
            geometry_type=geometry_type,
            crs_authid=crs,
        )
    )


def add_features(db: Path, layer_name: str, count: int = len(POINTS)) -> None:
    """往库里已有图层写几个点（走 OGR provider，模拟用户后续编辑）。"""
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer

    layer = QgsVectorLayer(f"{db}|layername={layer_name}", layer_name, "ogr")
    if not layer.isValid():
        raise RuntimeError(f"打开 {layer_name} 失败，写不进要素")
    layer.startEditing()
    feats = []
    for city, x, y, code in POINTS[:count]:
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        feats.append(f)
    if not layer.dataProvider().addFeatures(feats):
        raise RuntimeError(f"往 {layer_name} 写要素失败")
    if not layer.commitChanges():
        raise RuntimeError(f"{layer_name} 提交失败：{layer.commitErrors()}")


def feature_count(db: Path, layer_name: str) -> int:
    """库里某图层的要素数（打不开返回 -1）。"""
    from qgis.core import QgsVectorLayer

    layer = QgsVectorLayer(f"{db}|layername={layer_name}", layer_name, "ogr")
    return layer.featureCount() if layer.isValid() else -1


def spatial_layers(db: Path) -> list[str]:
    """数据管理登记表里这个库的空间图层清单（即 ``describe_local_db`` 看到的）。"""
    from app.core.util.local_db_util import describe_local_db

    return describe_local_db(db)["categories"]["spatial"]


def user_field_names(layer) -> list[str]:
    """图层的**用户字段**清单：跳过驱动自己的 FID 列（gpkg 的 ``fid`` 整型主键）。

    gpkg 表必须带整型主键，QGIS 的 OGR provider 才暴露删除 / 改值能力（见
    ``local_db_util.create_vector_table`` 的说明）；代价是它作为一列出现在
    ``fields()`` 里 —— 校验"用户字段"时要把它排除，否则每条断言都要跟着改。
    """
    try:
        primary_keys = set(layer.primaryKeyAttributes())
    except RuntimeError:
        primary_keys = set()
    names: list[str] = []
    for index, field in enumerate(layer.fields()):
        if index in primary_keys and field.name().strip().lower() in ("fid", "ogc_fid"):
            continue
        names.append(field.name())
    return names


# ------------------------------------------------------------------- 用例


def case_container_gpkg(out: Path) -> str:
    """建空 GeoPackage：系统表齐全、当场可被数据管理登记（清单为空）。"""
    import sqlite3

    from app.core.util.local_db_util import local_db_util

    db = out / "empty.gpkg"
    db.unlink(missing_ok=True)
    path = create_container(db, "gpkg")
    if path != str(db) or not db.is_file():
        raise RuntimeError(f"库文件没生成：{db}")

    kind = local_db_util(db).kind
    if kind != "gpkg":
        raise RuntimeError(f"库类型是 {kind}，期望 gpkg")

    con = sqlite3.connect(db)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    need = {"gpkg_contents", "gpkg_geometry_columns", "gpkg_spatial_ref_sys", "user_relation", "metadata"}
    missing = need - tables
    if missing:
        raise RuntimeError(f"缺系统表：{sorted(missing)}（实际 {sorted(tables)}）")

    if spatial_layers(db):
        raise RuntimeError(f"空库不该有已登记图层：{spatial_layers(db)}")
    return f"{db.name} 空库建好，系统表齐、登记清单为空 OK"


def case_container_spatialite(out: Path) -> str:
    """建空 SpatiaLite：库里带 spatial_ref_sys 与 user_relation。"""
    import sqlite3

    from app.core.util.local_db_util import local_db_util

    db = out / "empty.sqlite"
    db.unlink(missing_ok=True)
    create_container(db, "spatialite")
    if not db.is_file():
        raise RuntimeError(f"库文件没生成：{db}")

    util = local_db_util(db)
    kind, has_ext = util.kind, util.has_spatialite
    util.conn.close()
    if kind != "spatialite":
        raise RuntimeError(f"库类型是 {kind}，期望 spatialite")

    con = sqlite3.connect(db)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    if "user_relation" not in tables:
        raise RuntimeError(f"缺 user_relation（实际 {sorted(tables)}）")
    return f"{db.name} 空 SpatiaLite 建好（mod_spatialite 已加载={has_ext}）OK"


def case_layer_in_db(out: Path) -> str:
    """★往已有库建第二个图层：第一个图层的要素必须还在，两层都被登记。"""
    db = out / "multi.gpkg"
    db.unlink(missing_ok=True)
    create_container(db, "gpkg")

    add_empty_layer(db, "first")
    add_features(db, "first")
    if feature_count(db, "first") != len(POINTS):
        raise RuntimeError("first 图层造数据失败")

    # 这一步如果走 OGR 默认行为，会整库删掉重建：first 连同数据一起没
    add_empty_layer(db, "second")

    if feature_count(db, "first") != len(POINTS):
        raise RuntimeError(
            f"建 second 之后 first 的要素数变成 {feature_count(db, 'first')}，"
            f"期望 {len(POINTS)} —— 库被清库重建了！"
        )
    if feature_count(db, "second") != 0:
        raise RuntimeError(f"second 应是空图层，却有 {feature_count(db, 'second')} 个要素")

    registered = spatial_layers(db)
    for name in ("first", "second"):
        if name not in registered:
            raise RuntimeError(f"「{name}」没进 user_relation，数据管理看不到（实际 {registered}）")
    return f"{db.name} 追加第二个图层后 first 的 {len(POINTS)} 个要素完好，两层均已登记 OK"


def case_duplicate_layer(out: Path) -> str:
    """库里已有同名图层时拦下（且不动原图层）。"""
    db = out / "dup.gpkg"
    db.unlink(missing_ok=True)
    create_container(db, "gpkg")
    add_empty_layer(db, "first")
    add_features(db, "first")

    try:
        add_empty_layer(db, "first")
    except CreateLibraryError as exc:
        if "同名图层" not in str(exc):
            raise RuntimeError(f"拦下了但文案不对：{exc}") from exc
    else:
        raise RuntimeError("同名图层居然建成功了（会覆盖用户数据）")

    if feature_count(db, "first") != len(POINTS):
        raise RuntimeError("被拦下后 first 的要素数变了")
    return "同名图层被拦下，原图层内容未受影响 OK"


def case_layer_in_sqlite(out: Path) -> str:
    """★往 SpatiaLite 库建**空**图层：零属性列也要建得成，且不留残留列。

    用户报的场景（「目标库选 SQLite + 创建单图层数据」 → ``near ")": syntax error``）：
    SpatiaLite 的几何列是建完表之后 ``AddGeometryColumn`` 加的，而 SQLite 不允许
    零列表 —— ``CREATE TABLE "x" ()`` 直接语法错。建空图层（创建数据这条）与
    **全中文列名的图层**导入（``columns`` 清洗后为空）都会落到同一个坑上。

    同时钉住两件事：占位列不能残留在最终表里（用户会凭空多一个空属性列）、
    库里原有图层不受影响。
    """
    import sqlite3

    db = out / "sqlite_layer.db"
    db.unlink(missing_ok=True)
    create_container(db, "spatialite")

    add_empty_layer(db, "first")
    add_features(db, "first")
    if feature_count(db, "first") != len(POINTS):
        raise RuntimeError("first 图层造数据失败")

    add_empty_layer(db, "空矢量表", geometry_type="Polygon")

    if feature_count(db, "first") != len(POINTS):
        raise RuntimeError(
            f"在 SpatiaLite 库里建第二个图层后 first 的要素数变成 "
            f"{feature_count(db, 'first')}，期望 {len(POINTS)}"
        )
    con = sqlite3.connect(db)
    try:
        cols = [r[1] for r in con.execute('PRAGMA table_info("空矢量表")')]
        geom = con.execute(
            "SELECT geometry_type FROM geometry_columns WHERE f_table_name = '空矢量表'"
        ).fetchall()
    finally:
        con.close()
    if cols != ["geom"]:
        raise RuntimeError(f"空矢量表该只有 ['geom']，实际 {cols}（建表占位列没清掉？）")
    if geom != [(3,)]:
        raise RuntimeError(f"geometry_columns 里应是 3(POLYGON)，实际 {geom}")
    if feature_count(db, "空矢量表") != 0:
        raise RuntimeError("新建的空图层不该有要素")
    if "空矢量表" not in spatial_layers(db):
        raise RuntimeError(f"空图层没进 user_relation：{spatial_layers(db)}")
    return f"{db.name} 往 SpatiaLite 库建空图层成功（无残留列、first 未受影响）OK"


def case_import_keeps_cjk_columns(out: Path) -> str:
    """★导入**全中文列名**的图层：列名与属性值原样入库，一列都不能丢。

    列名清洗早先是 ``[^A-Za-z0-9_]``：中文被折成 ``_`` 再被 ``strip("_")`` 清空 ——
    全中文列名的图层 ``columns`` 直接为空，SpatiaLite 目标建表报
    ``near ")": syntax error``，GeoPackage 目标看着建成了、属性却全没了。
    """
    import sqlite3

    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer

    from app.core.domain.import_layer import import_vector_layer_to_db

    layer = QgsVectorLayer(
        f"Point?crs={CRS}&field=区县名称:string(50)&field=代码:integer", "中文列名", "memory"
    )
    if not layer.isValid():
        raise RuntimeError("中文列名的内存图层构造失败")
    feats = []
    for city, x, y, code in POINTS:
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        f.setAttributes([city, code])
        feats.append(f)
    layer.dataProvider().addFeatures(feats)

    for name, kind in (("import_cjk.db", "spatialite"), ("import_cjk.gpkg", "gpkg")):
        db = out / name
        db.unlink(missing_ok=True)
        create_container(db, kind)
        import_vector_layer_to_db(
            layer, {"kind": "local", "name": db.stem, "path": str(db)}, CRS
        )
        con = sqlite3.connect(db)
        try:
            cols = [r[1] for r in con.execute('PRAGMA table_info("中文列名")')]
            rows = con.execute(
                'SELECT "区县名称", "代码" FROM "中文列名" ORDER BY "代码"'
            ).fetchall()
        finally:
            con.close()
        if "区县名称" not in cols or "代码" not in cols:
            raise RuntimeError(f"{name} 里中文列名丢了（实际列 {cols}）")
        if len(rows) != len(POINTS):
            raise RuntimeError(f"{name} 里要素数 {len(rows)} != {len(POINTS)}")
        if [r[0] for r in rows] != [p[0] for p in POINTS]:
            raise RuntimeError(f"{name} 里属性值丢了（实际 {rows}）")
    return f"全中文列名导入 {kind} / spatialite 两个目标：列名与属性值都在 OK"


def case_layer_open_policy(out: Path) -> str:
    """★本地库矢量表的打开策略：SpatiaLite 不带 ``rowid`` 伪字段，GPKG 仍走 OGR。

    OGR 的 SQLite 驱动会把 FID 列当普通字段暴露出来（我们的表都没主键，就是 SQLite
    隐式列 ``rowid``）—— 属性表 / 属性识别 / 图层属性 / 导出里凭空多一列。GPKG 驱动
    不报 FID 列，所以只有 sqlite 库有这毛病。spatialite provider 只给真列，两个库的
    图层字段清单因此一致（见 ``local_layer_loader.open_local_vector_table``）。
    """
    from app.core.domain.import_layer import import_vector_layer_to_db

    from app.core.util.local_db_util import describe_local_db
    from app.core.util.local_layer_loader import (
        LocalLayerLoader,
        layer_source_container,
        layer_source_table,
        open_local_vector_table,
    )

    result: list[str] = []
    for name, kind, want_provider in (
        ("policy.db", "spatialite", "spatialite"),
        ("policy.gpkg", "gpkg", "ogr"),
    ):
        db = out / name
        db.unlink(missing_ok=True)
        create_container(db, kind)
        import_vector_layer_to_db(
            make_layer(), {"kind": "local", "name": db.stem, "path": str(db)}, CRS
        )

        layer, err = open_local_vector_table(str(db), "cities")
        if layer is None:
            raise RuntimeError(f"{name} 打不开：{err}")
        if layer.providerType() != want_provider:
            raise RuntimeError(f"{name} 用了 {layer.providerType()}，期望 {want_provider}")
        if user_field_names(layer) != ["name", "code"]:
            raise RuntimeError(f"{name} 字段清单不对：{[f.name() for f in layer.fields()]}")
        if layer.featureCount() != len(POINTS):
            raise RuntimeError(f"{name} 要素数 {layer.featureCount()} != {len(POINTS)}")
        if layer_source_container(layer) != str(db) or layer_source_table(layer) != "cities":
            raise RuntimeError(
                f"{name} 数据源解析不对：{layer_source_container(layer)!r} / "
                f"{layer_source_table(layer)!r}"
            )

        # 「数据管理」双击加载走的是同一条路
        loaded = LocalLayerLoader(None).load(
            {"kind": "local", "name": db.stem, "path": str(db), **describe_local_db(db)},
            "spatial",
            "cities",
        )
        if user_field_names(loaded) != ["name", "code"]:
            raise RuntimeError(
                f"{name} 经数据管理加载后字段清单不对：{[f.name() for f in loaded.fields()]}"
            )
        result.append(f"{name}→{want_provider}")
    return "矢量表打开策略：SpatiaLite 无 rowid 伪字段、GPKG 仍 OGR（" + "、".join(result) + "）OK"


def case_import_gdb_dates(out: Path) -> str:
    """★从 GDB 导入：日期 / 时间戳 / 二进制字段要能落库（用户报的绑参错误）。

    QGIS 的 OGR provider 读 Date / DateTime 给的是 **PyQt 的 QDateTime**、Binary 给
    QByteArray，sqlite3 绑不上 —— 直接塞进 INSERT 会整批回滚，报的却是
    ``Error binding parameter N - probably unsupported type``（N 是字段序号，看不出
    是哪一列）。收口在 ``local_db_util._sqlite_bindable``（见其说明）。

    本机没有可写 GDB 驱动时跳过建 GDB、只验值收口。
    """
    import shutil
    import sqlite3

    from qgis.core import (
        QgsCoordinateTransformContext,
        QgsFeature,
        QgsField,
        QgsGeometry,
        QgsPointXY,
        QgsVectorFileWriter,
        QgsVectorLayer,
    )
    from qgis.PyQt.QtCore import QByteArray, QDate, QDateTime, QMetaType, QTime

    from app.core.domain.import_layer import import_vector_layer_to_db
    from app.core.util.local_db_util import _sqlite_bindable

    # 1. 值收口（不依赖任何数据源）
    cases = (
        (QDateTime(QDate(2024, 5, 6), QTime(7, 8, 9)), "2024-05-06T07:08:09"),
        (QDate(2024, 5, 6), "2024-05-06"),
        (QTime(7, 8, 9), "07:08:09"),
        (QByteArray(bytes([0xDE, 0xAD])), bytes([0xDE, 0xAD])),
        ([1, 2], "[1, 2]"),
        (True, 1),
    )
    for value, want in cases:
        got = _sqlite_bindable(value)
        if got != want or type(got) is not type(want):
            raise RuntimeError(f"{value!r} 收口成 {got!r}，期望 {want!r}")

    # 2. 真建一个带这些字段的 GDB 再导入
    from app.core.domain.create_data import available_drivers

    if "OpenFileGDB" not in available_drivers():
        return "值收口 OK；本机没有可写的 OpenFileGDB 驱动，跳过 GDB 建库验证"

    gdb = out / "imp.gdb"
    if gdb.exists():
        shutil.rmtree(gdb)
    src = QgsVectorLayer(f"Point?crs={CRS}&field=名称:string(50)", "测试层", "memory")
    src.dataProvider().addAttributes([
        QgsField("日期", QMetaType.Type.QDate),
        QgsField("时间戳", QMetaType.Type.QDateTime),
        QgsField("二进制", QMetaType.Type.QByteArray),
    ])
    src.updateFields()
    feat = QgsFeature(src.fields())
    feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*POINTS[0][1:3])))
    feat.setAttributes([
        "北京", QDate(2024, 5, 6),
        QDateTime(QDate(2024, 5, 6), QTime(7, 8, 9)), QByteArray(bytes([0xDE, 0xAD])),
    ])
    if not src.dataProvider().addFeatures([feat]) or src.featureCount() != 1:
        raise RuntimeError("造 GDB 用的内存图层写不进要素")

    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "OpenFileGDB"
    opts.layerName = "测试层"
    opts.fileEncoding = "UTF-8"
    res = QgsVectorFileWriter.writeAsVectorFormatV3(
        src, str(gdb), QgsCoordinateTransformContext(), opts
    )
    if res[0] != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError(f"建 GDB 失败：{res[1]}")
    gdb_layer = QgsVectorLayer(str(gdb), "测试层", "ogr")
    if not gdb_layer.isValid() or gdb_layer.featureCount() != 1:
        raise RuntimeError("GDB 建出来了但读不回要素")

    for name, kind, driver, label in (
        ("gdb_import.db", "spatialite", "SQLite", "SQLite / SpatiaLite"),
        ("gdb_import.gpkg", "gpkg", "GPKG", "GeoPackage"),
    ):
        db = out / name
        db.unlink(missing_ok=True)
        CreateContainerService().create(
            CreateContainerSpec(path=str(out), file_name=name, data_format=label,
                                driver=driver, extension=db.suffix)
        )
        try:
            import_vector_layer_to_db(
                gdb_layer, {"kind": "local", "name": db.stem, "path": str(db)}, CRS
            )
        except Exception as exc:  # noqa: BLE001 —— 原来这里就是「绑参失败」那条
            raise RuntimeError(f"{name} 导入失败：{exc}") from exc
        con = sqlite3.connect(db)
        try:
            row = con.execute(
                'SELECT "日期", "时间戳", "二进制" FROM "测试层"'
            ).fetchone()
        finally:
            con.close()
        if row != ("2024-05-06T00:00:00", "2024-05-06T07:08:09", bytes([0xDE, 0xAD])):
            raise RuntimeError(f"{name} 落库的值不对：{row}")
    # 放掉引用即可；GDB 的文件句柄由 ogr provider 连接池持有，Windows 上要到进程
    # 退出才释放（产物目录在 gitignore 的 .tmp/ 下，残留无害，见 run_sqlite_test.cleanup）
    del src, gdb_layer
    return "GDB 的日期 / 时间戳 / 二进制字段导入 sqlite 与 gpkg 都落库 OK"


def case_local_db_order_and_import_default(out: Path) -> str:
    """★本地库「新的在上」＋ 导入对话框默认选中数据管理里选中的那个库。

    用户要的两条：最后建 / 最后导入的库排在最上面（数据管理左树、创建数据的
    「目标库」下拉都按这个顺序）；在数据管理里选中某个库点「导入」，目标数据库
    默认就落在它上，不用再挑一遍。
    """
    from app.ui.dialogs.create_data_dialog import CreateDataDialog
    from app.ui.dialogs.import_layer_dialog import ImportLayerDialog
    from app.ui.docks.data_manage_dock import DataManageDock
    from app.ui.main_window.mixins.app_actions import AppActionsMixin

    class _Host(AppActionsMixin):
        def __init__(self) -> None:
            self._data_manage_dock = None

    host = _Host()
    dock = DataManageDock(None)

    # 按 a → b → c 的顺序建库并登记（_register_local_db 内部就会 add_connection）
    paths = []
    for stem in ("order_a", "order_b", "order_c"):
        db = out / f"{stem}.gpkg"
        db.unlink(missing_ok=True)
        create_container(db, "gpkg")
        if host._register_local_db(dock, str(db)) is None:
            raise RuntimeError(f"登记 {stem} 失败")
        paths.append(db)
    a, b, c = paths

    # 1. 列表顺序 = 展示顺序 = 新的在上
    names = [Path(str(d.get("path"))).stem for d in dock._databases]
    if names != ["order_c", "order_b", "order_a"]:
        raise RuntimeError(f"数据管理里的本地库顺序不对（该新的在上）：{names}")
    tree_names = [
        dock.tree.topLevelItem(0).child(i).text(0)
        for i in range(dock.tree.topLevelItem(0).childCount())
    ]
    if tree_names != ["order_c", "order_b", "order_a"]:
        raise RuntimeError(f"左树里的顺序不对：{tree_names}")

    # 2. 创建数据的「目标库」下拉跟着同一个顺序（注入的就是 Dock 的清单）
    dlg = CreateDataDialog(None, default_crs=CRS, local_dbs=host._local_db_entries(dock))
    combo = dlg.db_target_combo
    labels = [combo.itemText(i) for i in range(combo.count())]
    if labels[:3] != ["order_c", "order_b", "order_a"]:
        raise RuntimeError(f"创建数据的目标库下拉顺序不对：{labels}")

    # 3. 数据管理里选中 b → 导入对话框的目标数据库默认就是 b
    b_node = next(
        dock.tree.topLevelItem(0).child(i)
        for i in range(dock.tree.topLevelItem(0).childCount())
        if dock.tree.topLevelItem(0).child(i).text(0) == "order_b"
    )
    dock._on_tree_item_clicked(b_node, 0)
    if str((dock._current_db or {}).get("path")) != str(b):
        raise RuntimeError(f"选中 b 之后 _current_db = {dock._current_db}")
    idlg = ImportLayerDialog(
        None,
        vector_layers=[make_layer()],
        databases=list(dock._databases),
        default_db=dock._current_db,
    )
    picked = idlg.db_combo.currentData() or {}
    if str(picked.get("path")) != str(b):
        raise RuntimeError(f"导入对话框默认选中了 {picked.get('path')!r}，期望 {str(b)!r}")

    # 4. 没传 default_db / 传了不在清单里的库 → 不报错，也不乱选中
    for default in (None, {"kind": "local", "name": "外来库", "path": str(out / "nope.gpkg")}):
        other = ImportLayerDialog(
            None, vector_layers=[make_layer()], databases=list(dock._databases), default_db=default
        )
        if (other.db_combo.currentData() or {}).get("path") != str(c):
            raise RuntimeError(f"default_db={default!r} 时不该改变默认选中项")
    if str(a) not in [str(d.get("path")) for d in [dock._databases[2]]]:
        raise RuntimeError("最后建的那个该排在最后（a 是最早建的）")
    return "本地库新的在上（树 / 创建数据下拉）＋ 导入默认选中当前库 OK"


def case_non_seed_srid(out: Path) -> str:
    """用非种子坐标系（EPSG:4547）建图层：SRS 应被自动补种。"""
    db = out / "srid4547.gpkg"
    db.unlink(missing_ok=True)
    create_container(db, "gpkg")

    srid = 4547  # CGCS2000 / 3-degree Gauss-Kruger CM 114E，不在 init_gpkg_db 的种子里
    add_empty_layer(db, "z45", crs=f"EPSG:{srid}")

    from qgis.core import QgsVectorLayer

    layer = QgsVectorLayer(f"{db}|layername=z45", "z45", "ogr")
    if not layer.isValid():
        raise RuntimeError("补种 SRS 后图层仍打不开")
    if layer.crs().authid() != f"EPSG:{srid}":
        raise RuntimeError(f"回读 CRS 是 {layer.crs().authid()}，期望 EPSG:{srid}")

    import sqlite3

    con = sqlite3.connect(db)
    try:
        rows = [r[0] for r in con.execute("SELECT srs_id FROM gpkg_spatial_ref_sys")]
    finally:
        con.close()
    if srid not in rows:
        raise RuntimeError(f"gpkg_spatial_ref_sys 里没有补种的 {srid}（实际 {sorted(rows)}）")
    return f"EPSG:{srid} 建图层成功、SRS 已补种、回读 CRS 正确 OK"


def make_dialog(out: Path, *, local_dbs=(), vector_layers=()) -> object:
    """建一个创建数据对话框（离屏），并把创建路径默认指到自检产物目录。"""
    from app.ui.dialogs.create_data_dialog import CreateDataDialog

    dlg = CreateDataDialog(
        None, default_crs=CRS, local_dbs=list(local_dbs), vector_layers=list(vector_layers)
    )
    dlg.db_path_edit.setText(str(out))
    dlg.path_edit.setText(str(out))
    return dlg


def _footer_texts(dlg, page: int) -> list[str]:
    """页脚栈第 ``page`` 页上看得见的按钮文字（从左到右）。

    ``下一步`` 与 ``创建`` 是同一个位置的两个按钮（按步骤显隐），所以只取可见的那些。
    """
    from qgis.PyQt.QtWidgets import QPushButton

    widget = dlg.footer_stack.widget(page)
    return [btn.text() for btn in widget.findChildren(QPushButton) if btn.isVisibleTo(widget)]


def case_wizard_steps(out: Path) -> str:
    """向导步骤：页脚随 (页签, 步骤) 切换，上一步保留字段表，切页签不回退。

    页面上的「回车会干什么」由**默认按钮**决定（隐藏的按钮必须显式取消默认，
    否则回车还是会落到它身上）—— 这条是「第 1 步敲回车绝不能直接把数据建出来」的保证。
    """
    dlg = make_dialog(out)
    if dlg.footer_stack.currentIndex() != 0 or dlg.step_stack.currentIndex() != 0:
        raise RuntimeError("默认该停在向导第 1 步")
    if dlg.prev_btn.isEnabled():
        raise RuntimeError("第 1 步的「上一步」该是禁用的")
    if not dlg.next_btn.isDefault() or dlg.create_btn.isDefault():
        raise RuntimeError("第 1 步的默认按钮该是「下一步」")

    dlg.tabs.setCurrentIndex(1)
    if dlg.footer_stack.currentIndex() != 1 or not dlg.ok_btn.isDefault():
        raise RuntimeError("容器页该翻到容器页脚、默认按钮是「确定」")
    if dlg.next_btn.isDefault():
        raise RuntimeError("容器页不该把「下一步」留着当默认按钮")
    dlg.tabs.setCurrentIndex(0)
    if dlg._step != 1:
        raise RuntimeError(f"切回向导页把步骤改了：{dlg._step}")

    # 第 1 步校验不过 → 不许翻页（原型也是「点下一步时才提示」）
    dlg.target_combo.setCurrentIndex(0)
    dlg.file_name_edit.setText("")
    dlg.layer_name_edit.setText("")
    dlg.next_step()
    if dlg._step != 1:
        raise RuntimeError("第 1 步没填完就翻到字段页了")

    dlg.file_name_edit.setText("wizard.geojson")
    dlg.layer_name_edit.setText("wizard")
    dlg.next_step()
    if dlg._step != 2 or dlg.step_stack.currentIndex() != 1:
        raise RuntimeError("填完后「下一步」没翻到字段页")
    if _footer_texts(dlg, 0) != ["取消", "上一步", "创建"]:
        raise RuntimeError(f"第 2 步的按钮不对：{_footer_texts(dlg, 0)}")
    if not dlg.prev_btn.isEnabled() or not dlg.create_btn.isDefault():
        raise RuntimeError("第 2 步该能「上一步」、且默认按钮是「创建」")

    dlg.fields_page.set_fields([FieldSpec("A_FIELD", "字段A", "text", 50, 0)])
    dlg.prev_step()
    if dlg._step != 1 or [f.name for f in dlg.fields_page.fields()] != ["A_FIELD"]:
        raise RuntimeError("「上一步」把字段表清掉了")
    dlg.next_step()
    if [f.name for f in dlg.fields_page.fields()] != ["A_FIELD"]:
        raise RuntimeError("回到第 2 步后字段表内容变了")
    return "向导步骤 / 两套页脚 / 上一步保留字段 / 切页签不回退 OK"


def case_field_model(out: Path) -> str:
    """字段模型：类型目录映射 + 校验文案（逐字照抄原型）+ 可哈希可比较。"""
    from qgis.core import QgsFieldConstraints

    from app.core.domain.create_data import (
        FIELD_TYPES,
        FieldSpec,
        qgs_field,
        sqlite_type,
        validate_fields,
    )

    for ftype in FIELD_TYPES:
        spec = FieldSpec("F", "", ftype.key)
        field = qgs_field(spec)
        if field.typeName() != ftype.type_name:
            raise RuntimeError(f"{ftype.key} 的 QgsField 类型名成了 {field.typeName()}")
        if not sqlite_type(spec):
            raise RuntimeError(f"{ftype.key} 算不出 SQL 类型")

    # 字典按文本列建（库里没有「字典列」这种东西，见 fields 模块头）
    dict_spec = FieldSpec("LAND", "地类", "dict", 20, 0, dict_table="地类字典", dict_domain="6项")
    if sqlite_type(dict_spec) != "TEXT(20)" or qgs_field(dict_spec).typeName() != "string":
        raise RuntimeError(f"字典字段该按文本建：{sqlite_type(dict_spec)}")
    # 长度 / 小数位留空时补类型默认值
    if sqlite_type(FieldSpec("T")) != "TEXT(50)" or sqlite_type(FieldSpec("D", type_key="double")) != "REAL":
        raise RuntimeError("长度留空时没补类型默认值")
    # 不可为空 → DDL 里的真 NOT NULL
    if sqlite_type(FieldSpec("N", nullable=False)) != "TEXT(50) NOT NULL":
        raise RuntimeError(f"NOT NULL 没进 DDL：{sqlite_type(FieldSpec('N', nullable=False))}")
    must = qgs_field(FieldSpec("N", nullable=False))
    if not must.constraints().constraints() & QgsFieldConstraints.Constraint.ConstraintNotNull:
        raise RuntimeError("QgsField 上没带上 NotNull 约束")

    # spec 必须可哈希可比较（对话框的「应用」是按 == 判重复创建的）
    pair = (FieldSpec("A"), FieldSpec("B", "乙", "double", 18, 2))
    try:
        hash(pair)
    except TypeError as exc:  # noqa: BLE001 —— 这就是要拦的错误
        raise RuntimeError(f"FieldSpec 不可哈希：{exc}") from exc
    if pair != tuple([FieldSpec("A"), FieldSpec("B", "乙", "double", 18, 2)]):
        raise RuntimeError("同样的字段结构比出不相等")

    cases = (
        ([FieldSpec("")], "存在空字段名，请删除该行"),
        ([FieldSpec("1A")], '字段名"1A"不能以数字开头，请修改。'),
        ([FieldSpec("A B")], '字段名"A B"包含特殊字符，请修改。'),
        ([FieldSpec("A"), FieldSpec("A")], '字段名"A"重复'),
    )
    for fields, want in cases:
        got = validate_fields(fields)
        if got != want:
            raise RuntimeError(f"校验文案不对：{got!r} != {want!r}")
    if validate_fields([FieldSpec("区县名称")]) != "":
        raise RuntimeError("中文字段名该是合法的")
    return f"类型目录 {len(FIELD_TYPES)} 项 / 字典按文本建 / 四条校验文案逐字 / 可哈希 OK"


def case_file_with_fields(out: Path) -> str:
    """新建成独立文件时带上字段结构：能落的落进文件，落不下的如实报出来。"""
    from app.core.domain.create_data import (
        CreateDataService,
        CreateDataSpec,
        FieldSpec,
        configure_created_layer,
    )

    fields = (
        FieldSpec("FEAT_ID", "要素ID", "text", 50, 0, nullable=False),
        FieldSpec("LAND", "地类", "dict", 20, 0, dict_table="地类字典", dict_domain="6项"),
        FieldSpec("SECRET", "内部", "text", 10, 0, visible=False),
    )

    # Shapefile：字段名 / 类型 / 长度保得住；别名与 NOT NULL 保不住 → 落在图层上 + 如实提示
    shp = out / "with_fields.shp"
    shp.unlink(missing_ok=True)
    layer = CreateDataService().create(
        CreateDataSpec(
            path=str(out), file_name=shp.name, data_format="Shapefile",
            driver="ESRI Shapefile", extension=".shp", layer_name="with_fields",
            geometry_type="Point", crs_authid=CRS, fields=fields,
        )
    )
    names = [f.name() for f in layer.fields()]
    if names != ["FEAT_ID", "LAND", "SECRET"]:
        raise RuntimeError(f"shp 里的字段不对：{names}")
    if layer.fields().at(0).length() != 50 or layer.fields().at(2).length() != 10:
        raise RuntimeError(
            f"shp 的长度丢了：{[f.length() for f in layer.fields()]}"
        )
    notes = configure_created_layer(layer, fields, in_db=False)
    if not any("别名" in n for n in notes):
        raise RuntimeError(f"别名没进 shp 却没提示：{notes}")
    if not any("不可为空" in n for n in notes):
        raise RuntimeError(f"NOT NULL 没进 shp 却没提示：{notes}")
    if layer.fields().at(0).alias() != "要素ID":
        raise RuntimeError("别名没落在图层上")
    if layer.fields().at(2).editorWidgetSetup().type() != "Hidden":
        raise RuntimeError("「不可见」没落在图层上")
    if layer.customProperty("create_data/dict_table/LAND", "") != "地类字典":
        raise RuntimeError("字典表没落在图层上")

    # GeoJSON 的空文件存不下字段结构（schema 随要素写）→ 一条都不能默默少
    gj = out / "with_fields.geojson"
    gj.unlink(missing_ok=True)
    geo = CreateDataService().create(
        CreateDataSpec(
            path=str(out), file_name=gj.name, data_format="GeoJSON", driver="GeoJSON",
            extension=".geojson", layer_name="with_fields", geometry_type="Point",
            crs_authid=CRS, fields=fields,
        )
    )
    if geo.fields().size() != 0:
        return f"{shp.name} 字段进去了，别名/NOT NULL 如实提示 OK（GeoJSON 本机居然存下了字段，跳过）"
    notes = configure_created_layer(geo, fields, in_db=False)
    if not any("没有写进数据源" in n for n in notes):
        raise RuntimeError(f"GeoJSON 丢了字段却没提示：{notes}")
    return f"{shp.name} 字段+长度进文件 / 别名与 NOT NULL 如实提示 / GeoJSON 丢字段也如实提示 OK"


def case_into_db_with_fields(out: Path) -> str:
    """★往库里建带字段结构的图层：NOT NULL 与别名在库里是真的（gpkg 走 gpkg_data_columns）。"""
    import sqlite3

    from app.core.domain.create_data import (
        CreateLayerInDbService,
        CreateLayerInDbSpec,
        FieldSpec,
        configure_created_layer,
    )

    fields = (
        FieldSpec("FEAT_ID", "要素ID", "text", 50, 0, nullable=False),
        FieldSpec("AREA", "面积(公顷)", "double", 18, 2),
        FieldSpec("LAND", "地类", "dict", 20, 0, dict_table="地类字典", dict_domain="6项"),
        FieldSpec("SECRET", "内部", "text", 10, 0, visible=False),
    )
    notes_by_kind: dict[str, tuple[str, ...]] = {}
    for name, kind, label in (
        ("fields.gpkg", "gpkg", "GeoPackage"),
        ("fields.sqlite", "spatialite", "SQLite / SpatiaLite"),
    ):
        db = out / name
        db.unlink(missing_ok=True)
        create_container(db, kind)
        layer = CreateLayerInDbService().create(
            CreateLayerInDbSpec(
                db_path=str(db), layer_name="带字段的图层", geometry_type="Polygon",
                crs_authid=CRS, fields=fields,
            )
        )
        if user_field_names(layer) != [f.name for f in fields]:
            raise RuntimeError(f"{name} 回读字段清单不对：{[f.name() for f in layer.fields()]}")

        con = sqlite3.connect(db)
        try:
            ddl = con.execute('PRAGMA table_info("带字段的图层")').fetchall()
            aliases: list[tuple[str, str]] = []
            if kind == "gpkg":
                aliases = con.execute(
                    "SELECT column_name, name FROM gpkg_data_columns"
                ).fetchall()
        finally:
            con.close()
        by_name = {row[1]: row for row in ddl}
        if by_name["FEAT_ID"][2] != "TEXT(50)" or by_name["FEAT_ID"][3] != 1:
            raise RuntimeError(f"{name} 里 FEAT_ID 的列定义不对：{by_name['FEAT_ID']}")
        if by_name["AREA"][2] != "REAL":
            raise RuntimeError(f"{name} 里 AREA 该是 REAL：{by_name['AREA']}")
        if by_name["LAND"][2] != "TEXT(20)":
            raise RuntimeError(f"{name} 里字典字段该按文本建：{by_name['LAND']}")
        if by_name["AREA"][3] != 0:
            raise RuntimeError(f"{name} 里没标可空的字段成了 NOT NULL：{by_name['AREA']}")

        notes_by_kind[kind] = configure_created_layer(layer, fields, in_db=True)
        if layer.fields().at(layer.fields().indexOf("SECRET")).editorWidgetSetup().type() != "Hidden":
            raise RuntimeError(f"{name} 的隐藏字段没落在图层上")
        if layer.customProperty("create_data/dict_domain/LAND", "") != "6项":
            raise RuntimeError(f"{name} 的字典域没落在图层上")

        if kind == "gpkg":
            if ("FEAT_ID", "要素ID") not in aliases:
                raise RuntimeError(f"gpkg 里没有列别名：{aliases}")
            if notes_by_kind["gpkg"]:
                raise RuntimeError(f"gpkg 该全都落下，却有提示：{notes_by_kind['gpkg']}")
        else:
            # SpatiaLite 没有标准的别名目录表 → 写本工程自己的 qgis_dev_column_alias。
            # **关键：关掉再打开还得在**（用户报过"重开别名没了"），所以这里从盘上重开一次。
            con = sqlite3.connect(db)
            try:
                stored = con.execute(
                    "SELECT column_name, alias FROM qgis_dev_column_alias WHERE table_name = ?",
                    ("带字段的图层",),
                ).fetchall()
            finally:
                con.close()
            if ("FEAT_ID", "要素ID") not in stored:
                raise RuntimeError(f"sqlite 库里没记住别名：{stored}")
            if any("别名" in n for n in notes_by_kind["spatialite"]):
                raise RuntimeError(f"别名已经进库了，不该再提示：{notes_by_kind['spatialite']}")
            from app.core.util.local_layer_loader import open_local_vector_table

            fresh, fresh_err = open_local_vector_table(str(db), "带字段的图层")
            if fresh is None:
                raise RuntimeError(f"重开 sqlite 里的图层失败：{fresh_err}")
            index = fresh.fields().indexOf("FEAT_ID")
            if fresh.fields().at(index).alias() != "要素ID":
                raise RuntimeError(
                    f"重开后别名没了：{[f.alias() for f in fresh.fields()]}"
                )
    return "库内字段：TEXT(50)/REAL/字典按文本、真 NOT NULL、gpkg 别名进 gpkg_data_columns OK"


def case_field_template(out: Path) -> str:
    """字段结构模板：生成 .xlsx → 读回来一致；空模板只有表头也能读。"""
    from app.core.domain.create_data import (
        FieldSpec,
        read_template,
        write_template,
        xlsx_available,
    )

    if not xlsx_available():
        return "本机没有可写 XLSX 的驱动，跳过模板生成验证"

    fields = [
        FieldSpec("FEAT_ID", "要素ID", "text", 50, 0, nullable=False),
        FieldSpec("LAND", "地类", "dict", 20, 0, dict_table="地类字典", dict_domain="6项"),
        FieldSpec("AREA", "面积(公顷)", "double", 18, 2),
        FieldSpec("WHEN", "日期", "date"),
        FieldSpec("FLAG", "可用", "bool"),
        FieldSpec("SECRET", "内部", "text", 10, 0, visible=False),
    ]
    tpl = out / "字段结构模板.xlsx"
    tpl.unlink(missing_ok=True)
    write_template(str(tpl), fields)
    if not tpl.is_file():
        raise RuntimeError(f"模板没生成：{tpl}")
    back = read_template(str(tpl))
    if tuple(back) != tuple(fields):
        raise RuntimeError(f"模板往返丢东西：\n{back}\n{fields}")

    # 空模板（0 个字段）也要有表头，读回来是空表而不是报错
    empty = out / "空模板.xlsx"
    empty.unlink(missing_ok=True)
    write_template(str(empty), [])
    if read_template(str(empty)) != []:
        raise RuntimeError("空模板读回来不是空表")

    # 表头也可能被驱动当成「图层字段名」（CSV 就是）：这条兜住那种形态
    csv = out / "模板表头形态.csv"
    csv.write_text(
        "字段名,字段别名,字段类型,字段长度,小数位数,字典表,字典域,是否可为空,是否可见\n"
        "DLMC,地类名称,字典,30,0,地类字典,6项,否,是\n",
        encoding="utf-8",
    )
    csv_back = read_template(str(csv))
    if len(csv_back) != 1 or csv_back[0].name != "DLMC" or csv_back[0].type_key != "dict":
        raise RuntimeError(f"表头被当字段名的那种模板没读对：{csv_back}")
    if csv_back[0].nullable or not csv_back[0].visible:
        raise RuntimeError(f"「是否可为空 / 是否可见」没读对：{csv_back[0]}")

    # 认不出的类型按文本处理（人手填模板，不能太挑）
    loose = out / "类型认不出.xlsx"
    loose.unlink(missing_ok=True)
    write_template(str(loose), [FieldSpec("X", "随便", "text", 8)])
    if read_template(str(loose))[0].type_key != "text":
        raise RuntimeError("认不出的类型没退回文本")
    return f"{tpl.name} 6 个字段往返一致 / 空模板 / 表头两种形态 OK"


def case_fields_from_layer(out: Path) -> str:
    """外部数据集：从已有图层复制字段结构（含别名、长度、小数位、可空、可见、字典）。"""
    from qgis.core import QgsField, QgsFieldConstraints, QgsVectorLayer
    from qgis.PyQt.QtCore import QVariant

    from app.core.domain.create_data import FieldSpec, fields_from_layer

    src = QgsVectorLayer(f"Point?crs={CRS}", "源图层", "memory")
    src.dataProvider().addAttributes([
        QgsField("SRC_NAME", QVariant.String, "string", 30, 0),
        QgsField("SRC_AREA", QVariant.Double, "double", 18, 2),
    ])
    src.updateFields()
    src.setFieldAlias(0, "源名称")
    src.setFieldConstraint(1, QgsFieldConstraints.Constraint.ConstraintNotNull)
    src.setCustomProperty("create_data/dict_table/SRC_NAME", "名称字典")

    specs = fields_from_layer(src)
    if [s.name for s in specs] != ["SRC_NAME", "SRC_AREA"]:
        raise RuntimeError(f"字段清单不对：{[s.name for s in specs]}")
    if specs[0].alias != "源名称" or specs[0].length != 30:
        raise RuntimeError(f"别名 / 长度没带出来：{specs[0]}")
    if specs[1].nullable or specs[1].precision != 2:
        raise RuntimeError(f"可空 / 小数位没带出来：{specs[1]}")
    # 带字典属性的字段要按「字典」还原，不能悄悄降级成普通文本
    if specs[0].type_key != "dict" or specs[0].dict_table != "名称字典":
        raise RuntimeError(f"字典信息没带出来：{specs[0]}")
    if tuple(specs) != tuple([FieldSpec(**{**s.__dict__}) for s in specs]):
        raise RuntimeError("复制出来的 spec 不可比")
    return f"从图层复制 {len(specs)} 个字段（含别名 / 长度 / 小数位 / 可空 / 字典）OK"


def case_field_persistence_notes(out: Path) -> str:
    """落盘实况提示：全落下就一条不说，落不下的必须逐条说清。"""
    from app.core.domain.create_data import (
        CreateLayerInDbService,
        CreateLayerInDbSpec,
        FieldSpec,
        configure_created_layer,
    )

    db = out / "notes.gpkg"
    db.unlink(missing_ok=True)
    create_container(db, "gpkg")
    fields = (
        FieldSpec("FEAT_ID", "要素ID", "text", 50, 0, nullable=False),
        FieldSpec("AREA", "面积", "double", 18, 2),
    )
    layer = CreateLayerInDbService().create(
        CreateLayerInDbSpec(
            db_path=str(db), layer_name="notes_layer", geometry_type="Point",
            crs_authid=CRS, fields=fields,
        )
    )
    notes = configure_created_layer(layer, fields, in_db=True)
    if notes:
        raise RuntimeError(f"gpkg 这条路该全都落下，却有提示：{notes}")
    # 空字段结构（用户没填）不该报任何东西
    if configure_created_layer(layer, (), in_db=False):
        raise RuntimeError("没有字段却报了落盘提示")
    return "gpkg 全落下无提示 / 空字段结构不报提示 OK"


def make_multilayer_gdb(path: Path, layers: list[tuple[str, str]]) -> None:
    """建一个多图层 GDB（第二个起走 ``CreateOrOverwriteLayer``）。

    **不能用创建服务往既有容器里追加**：那按约定是被拦下的（见 ``_check_target``），而且
    OGR 写容器格式默认 ``CreateOrOverwriteFile`` 会把先前的图层整个清掉。
    """
    import shutil as _shutil

    from qgis.core import (
        QgsCoordinateTransformContext,
        QgsVectorFileWriter,
        QgsVectorLayer,
    )

    if path.exists():
        _shutil.rmtree(path)
    action = QgsVectorFileWriter.ActionOnExistingFile
    for index, (name, geometry) in enumerate(layers):
        source = QgsVectorLayer(f"{geometry}?crs={CRS}", name, "memory")
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "OpenFileGDB"
        options.layerName = name
        options.fileEncoding = "UTF-8"
        options.actionOnExistingFile = (
            action.CreateOrOverwriteFile if index == 0 else action.CreateOrOverwriteLayer
        )
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            source, str(path), QgsCoordinateTransformContext(), options
        )
        if result[0] != QgsVectorFileWriter.WriterError.NoError:
            raise RuntimeError(f"造测试 GDB 失败（{name}）：{result[1]}")


def case_data_manage_state(out: Path) -> str:
    """★数据管理的连接清单跟随系统、不跟工程走。

    用户要的：绑定好数据库之后（或加了、删了），**即使当前工程文件没保存**，下次打开
    还是关掉前的样子。所以它落在配置文件目录下的状态文件里（``app.infra.fs.state``，
    可用 ``QGIS_DEV_PATHS_CONFIG`` 重定向），而不是写进 QGIS 工程。
    """
    import os as _os

    from qgis.core import QgsProject

    from app.infra.fs.state import load_state, save_state, state_path
    from app.ui.docks.data_manage_dock import DataManageDock
    from app.ui.main_window.mixins.app_actions import _DATA_MANAGE_STATE, AppActionsMixin

    class _Host(AppActionsMixin):
        def __init__(self) -> None:
            self._data_manage_dock = None

    host = _Host()
    config_dir = out / "state_cfg"
    previous = _os.environ.get("QGIS_DEV_PATHS_CONFIG")
    _os.environ["QGIS_DEV_PATHS_CONFIG"] = str(config_dir)
    try:
        if "state" not in str(state_path(_DATA_MANAGE_STATE)):
            raise RuntimeError(f"状态文件该落在配置目录下：{state_path(_DATA_MANAGE_STATE)}")

        db_a = out / "state_a.gpkg"
        db_b = out / "state_b.gpkg"
        for db in (db_a, db_b):
            db.unlink(missing_ok=True)
            create_container(db, "gpkg")

        # 按主窗口的接法接线（见 AppActionsMixin._on_data_manage）
        dock = DataManageDock(None)
        host._data_manage_dock = dock
        dock.databasesChanged.connect(host._save_data_manage_state)
        dock.add_connection({"kind": "local", "name": "甲库", "path": str(db_a)})
        dock.add_connection({"kind": "local", "name": "乙库", "path": str(db_b)})

        saved = load_state(_DATA_MANAGE_STATE)
        paths = [item.get("path") for item in saved.get("local") or []]
        if paths != [str(db_b), str(db_a)]:  # 新的在上
            raise RuntimeError(f"状态文件里没记对：{saved}")

        # 再开一个新 Dock（相当于下次启动）：只喂状态文件就能还原
        fresh = DataManageDock(None)
        fresh.restore_state(load_state(_DATA_MANAGE_STATE))
        if [str(d.get("path")) for d in fresh._databases] != [str(db_b), str(db_a)]:
            raise RuntimeError(f"下次打开没还原：{fresh._databases}")
        if [name for name in ("甲库", "乙库") if name not in [
            dock.tree.topLevelItem(0).child(i).text(0) for i in range(dock.tree.topLevelItem(0).childCount())
        ]]:
            raise RuntimeError("左树里没出现登记过的库")

        # **不再写进 QGIS 工程**（跟随系统，不跟工程）
        _raw, ok = QgsProject.instance().readEntry("qgis_dev", "data_manage", "")
        if ok:
            raise RuntimeError("连接清单还被写进了 QGIS 工程 —— 应该只进系统级状态文件")

        # 读坏 / 不存在的状态文件不该拦住启动
        broken = state_path(_DATA_MANAGE_STATE)
        broken.write_text("{ 这不是 json", encoding="utf-8")
        if load_state(_DATA_MANAGE_STATE) != {}:
            raise RuntimeError("读坏的状态文件该按空处理")
        # 状态名是用来拼路径的：带斜杠 / 点点的一律拒绝（防目录穿越）
        try:
            state_path("坏名字/../x")
        except ValueError:
            pass
        else:
            raise RuntimeError("不安全的状态名该抛 ValueError")
    finally:
        if previous is None:
            _os.environ.pop("QGIS_DEV_PATHS_CONFIG", None)
        else:
            _os.environ["QGIS_DEV_PATHS_CONFIG"] = previous
    return "连接清单落盘到系统级状态文件 / 下次打开还原 / 不写进工程 / 坏文件不拦启动 OK"


def case_external_dataset_files(out: Path) -> str:
    """★「外部数据集」能挑本地文件，容器数据源要能把里面的图层列出来再选。

    用户报的：选 GDB 之后找不到里面有哪些图层可选（以前这个弹窗只列工程里已加载的
    图层）。容器型数据源（``.gdb`` 目录 / ``.gpkg`` / ``.sqlite``）都得先列图层、再选一个；
    列的时候还要滤掉本工程自己的记账表（``user_relation`` / ``metadata``）——
    它们是库里的普通表，OGR 会当图层报出来，混进候选只会让人困惑。
    """
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import QDialogButtonBox
    from qgis.core import QgsVectorLayer

    from app.core.util.sublayers import list_sublayers, open_sublayer
    from app.ui.dialogs.create_data_fields_step import ExternalDatasetDialog, FieldsStepPage

    gdb = out / "外部.gdb"
    make_multilayer_gdb(gdb, [("小班", "Polygon"), ("道路", "LineString")])
    db = out / "外部.gpkg"
    db.unlink(missing_ok=True)
    create_container(db, "gpkg")
    for name, geometry in (("小班", "Polygon"), ("监测点", "Point")):
        add_empty_layer(db, name, geometry)
    sqlite = out / "外部.sqlite"
    sqlite.unlink(missing_ok=True)
    create_container(sqlite, "spatialite")
    add_empty_layer(sqlite, "小班", "Polygon")
    shp = out / "外部单层.shp"
    shp.unlink(missing_ok=True)
    from app.core.domain.create_data import CreateDataService, CreateDataSpec

    CreateDataService().create(
        CreateDataSpec(
            path=str(out), file_name=shp.name, data_format="Shapefile", driver="ESRI Shapefile",
            extension=".shp", layer_name="外部单层", geometry_type="Point", crs_authid=CRS,
        )
    )

    # 1. 列子图层：GDB 列全、库里滤掉记账表、单层文件就一项
    gdb_names = [s.name for s in list_sublayers(str(gdb))]
    if gdb_names != ["小班", "道路"]:
        raise RuntimeError(f"GDB 里该列出两个图层，实际 {gdb_names}")
    gpkg_names = [s.name for s in list_sublayers(str(db))]
    if "user_relation" in gpkg_names or "metadata" in gpkg_names:
        raise RuntimeError(f"库里的记账表混进候选了：{gpkg_names}")
    if not {"小班", "监测点"} <= set(gpkg_names):
        raise RuntimeError(f"gpkg 里的图层没列全：{gpkg_names}")
    shp_subs = list_sublayers(str(shp))
    if len(shp_subs) != 1:
        raise RuntimeError(f"单层文件该只有一项，实际 {[s.name for s in shp_subs]}")
    if list_sublayers(str(out / "不存在的.gdb")):
        raise RuntimeError("不存在的路径该返回空表")

    # 2. 打开子图层：GDB 走 OGR，本地库走统一策略（sqlite 不许冒出 rowid 列）
    layer, err = open_sublayer(str(gdb), next(s for s in list_sublayers(str(gdb)) if s.name == "小班"))
    if layer is None or not layer.isValid():
        raise RuntimeError(f"打不开 GDB 里的图层：{err}")
    if layer.crs().authid() != CRS:
        raise RuntimeError(f"GDB 图层坐标系不对：{layer.crs().authid()}")
    layer, err = open_sublayer(str(sqlite), next(s for s in list_sublayers(str(sqlite)) if s.name == "小班"))
    if layer is None:
        raise RuntimeError(f"打不开 SpatiaLite 里的图层：{err}")
    if layer.providerType() != "spatialite":
        raise RuntimeError(f"sqlite 该走 spatialite provider，实际 {layer.providerType()}")
    if "rowid" in [f.name() for f in layer.fields()]:
        raise RuntimeError("sqlite 图层冒出了 rowid 伪字段")

    # 3. 弹窗：工程模式 / 文件模式两条路都要能取到图层
    project_layer = QgsVectorLayer(f"Point?crs={CRS}&field=A:string(10)", "工程里的层", "memory")
    dialog = ExternalDatasetDialog(None, vector_layers=[project_layer])
    ok_btn = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    if not ok_btn.isEnabled():
        raise RuntimeError("有工程图层时「确定」该是可点的")
    resolved, error = dialog.resolve()
    if resolved is not project_layer or error:
        raise RuntimeError(f"工程模式没取到图层：{resolved} {error}")

    dialog.file_radio.setChecked(True)
    if dialog.source_stack.currentIndex() != 1:
        raise RuntimeError("切到「本地文件」没翻到对应的页")
    if ok_btn.isEnabled():
        raise RuntimeError("还没选文件时「确定」该是灰的")
    dialog._load_source(str(gdb))
    listed = [dialog.sublayer_list.item(i).text() for i in range(dialog.sublayer_list.count())]
    if len(listed) != 2:
        raise RuntimeError(f"弹窗里该列出 GDB 的两个图层，实际 {listed}")
    if not ok_btn.isEnabled():
        raise RuntimeError("列出来之后「确定」该可点")
    resolved, error = dialog.resolve()
    if error or resolved is None or resolved.name() != listed[0]:
        raise RuntimeError(f"文件模式没打开选中的图层：{resolved} {error}")
    dialog.sublayer_list.setCurrentRow(1)
    resolved, error = dialog.resolve()
    if error or resolved is None or resolved.name() != "道路":
        raise RuntimeError(f"换一个子图层没生效：{resolved} {error}")

    # 4. 从文件里那份数据复制字段结构（走 page 的整条链）
    page = FieldsStepPage(None)
    page.set_fields([])
    from app.core.domain.create_data import fields_from_layer

    page.set_fields(fields_from_layer(resolved))
    if [f.name for f in page.fields()] == []:
        # 「道路」是空图层，只有 OGR 的 FID 伪字段，复制出来可能是空的 —— 换「小班」再试
        raise RuntimeError("从 GDB 图层复制字段结构得到了空表")
    return f"GDB 列全 {gdb_names} / 库里过滤记账表 / 单层文件 1 项 / 弹窗两条来源都能取图层 OK"


def case_crs_picker(out: Path) -> str:
    """★坐标系选择弹窗：六个页签、分带列表与 srs.db 对得上、选完能写回下拉。

    分带列表是**按 EPSG 编码区间 + proj 串核对**查出来的（不是按描述文字匹 ——
    中文版 QGIS 的 srs.db 里全是「国家2000坐标系 经纬度」这类译名，按英文关键词匹
    一条都匹不上）。这里用 srs.db 自己的描述做**交叉验证**：同一个代码，两边应该
    指向同一个投影。
    """
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import QDialogButtonBox
    from qgis.PyQt.QtWidgets import QRadioButton
    from qgis.core import QgsCoordinateReferenceSystem, QgsVectorLayer

    from app.core.util.srs import DATUMS, modes_for, zone_entries
    from app.ui.dialogs.crs_pick_dialog import CrsPickDialog

    # 1. 分带列表：首项是「经纬度」，其余每项都是有效坐标系且投影族对得上；
    #    再抽查几组的项数与首个投影分带
    for datum in DATUMS:
        for mode in modes_for(datum):
            entries = zone_entries(datum, mode)
            if len(entries) < 2:
                raise RuntimeError(f"{datum.key} / {mode.label} 分带太少：{len(entries)}")
            head = entries[0]
            if "经纬度" not in head.label:
                raise RuntimeError(f"{datum.key} 的首项该是经纬度：{head.label!r}")
            head_crs = QgsCoordinateReferenceSystem(head.authid)
            if "+proj=longlat" not in head_crs.toProj():
                raise RuntimeError(f"{head.authid} 不是地理坐标系：{head_crs.toProj()}")
            if not any(tok in head_crs.toProj() for tok in datum.ellps):
                raise RuntimeError(f"{head.authid} 的基准不对：{head_crs.toProj()}")
            want_proj = "+proj=utm" if datum.utm else "+proj=tmerc"
            for entry in entries[1:]:
                crs = QgsCoordinateReferenceSystem(entry.authid)
                if not crs.isValid():
                    raise RuntimeError(f"{entry.authid} 是无效坐标系（{entry.label}）")
                if want_proj not in crs.toProj():
                    raise RuntimeError(f"{entry.authid} 不是 {want_proj}：{crs.toProj()}")

    # (基准, 模式) → (投影分带项数, 第一项的带号, 第一项的中央经线)；总项数还要 +1（经纬度）
    expected = {
        ("北京54", "3带号"): (21, 25, 75),
        ("北京54", "6带号"): (11, 13, 75),
        ("西安80", "3带号"): (21, 25, 75),
        ("CGCS2000", "3带号"): (21, 25, 75),
        ("WGS84", "utm北"): (60, 1, 177),
    }
    for (datum_label, mode_key), (count, first_zone, first_cm) in expected.items():
        datum = next(d for d in DATUMS if d.key == datum_label)
        mode = next(m for m in modes_for(datum) if m.key == mode_key)
        entries = zone_entries(datum, mode)
        if len(entries) != count + 1:
            raise RuntimeError(
                f"{datum_label}/{mode.label} 应有 {count} 项分带 + 1 项经纬度，实际 {len(entries)}"
            )
        first = entries[1]
        if str(first_zone) not in first.label or str(first_cm) not in first.label:
            raise RuntimeError(f"{datum_label}/{mode.label} 首个分带不对：{first.label}")

    # 2. 弹窗结构：六个页签、每张列表各自独立、选完能取到值
    layers = [
        QgsVectorLayer("Point?crs=EPSG:4547", "小班", "memory"),
        QgsVectorLayer("Point?crs=EPSG:4490", "成果", "memory"),
    ]
    dlg = CrsPickDialog(None, current="EPSG:4490", layers=layers)
    titles = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
    if titles != ["图层", "北京54", "西安80", "WGS84", "CGCS2000", "自定义"]:
        raise RuntimeError(f"页签不对：{titles}")
    if "当前空间参考" not in dlg.head.text():
        raise RuntimeError(f"顶部没回显当前空间参考：{dlg.head.text()!r}")
    if dlg.layer_table.rowCount() != 2:
        raise RuntimeError("图层页签没列出注入的图层")
    if dlg.crs_value() != "EPSG:4490":
        raise RuntimeError(f"没动过时该保持原值：{dlg.crs_value()}")

    cg = dlg._zone_lists["CGCS2000"]
    bj = dlg._zone_lists["北京54"]
    # 第 0 项是「经纬度」，所以带号 34（25 起的第 10 个分带）在第 10 行
    cg.setCurrentRow(10)
    picked = cg.currentItem().data(Qt.ItemDataRole.UserRole)
    if dlg.crs_value() != picked or "带号34" not in cg.currentItem().text():
        raise RuntimeError(f"选分带没生效：{dlg.crs_value()} / {cg.currentItem().text()}")
    cg_before, bj_before = cg.count(), bj.count()
    # 切模式只影响本页签的列表
    radios = [r for r in dlg.tabs.widget(4).findChildren(QRadioButton)]
    if len(radios) != 4:
        raise RuntimeError(f"CGCS2000 页签该有 4 个分带模式，实际 {len(radios)}")
    radios[1].setChecked(True)
    if cg.count() != cg_before - 0 and "不加带号" not in cg.item(0).text():
        raise RuntimeError(f"切模式后列表没换：{cg.item(0).text()!r}")
    if bj.count() != bj_before:
        raise RuntimeError("切 CGCS2000 的模式把北京54 的列表也动了")
    # 图层页签：选一行即采用该图层的坐标系
    dlg.layer_table.selectRow(0)
    if dlg.crs_value() != "EPSG:4547":
        raise RuntimeError(f"选图层行没取到它的 CRS：{dlg.crs_value()}")

    # 3. 主对话框那行的「选择」按钮能写回
    import app.ui.dialogs.create_data_dialog as mod

    main = mod.CreateDataDialog(None, default_crs=CRS, vector_layers=layers)
    main.target_combo.setCurrentIndex(0)
    main.file_name_edit.setText("crs_pick.geojson")
    main.layer_name_edit.setText("crs_pick")
    captured: dict = {}

    class _FakePick:
        def __init__(self, *_a, **kwargs):
            captured.update(kwargs)

        def exec(self) -> int:
            return 1

        def crs_value(self) -> str:
            return "EPSG:4547"

        def deleteLater(self) -> None:
            pass

    original = mod.CrsPickDialog
    mod.CrsPickDialog = _FakePick
    try:
        main._on_pick_crs(main.crs_combo)
    finally:
        mod.CrsPickDialog = original
    if main.crs_combo.authid() != "EPSG:4547":
        raise RuntimeError(f"「选择」确定后没写回下拉：{main.crs_combo.authid()}")
    if captured.get("layers") != layers:
        raise RuntimeError("没把工程图层注入弹窗（图层页签会是空的）")
    if "4547" not in main.hint.text():
        raise RuntimeError(f"提示没跟着更新：{main.hint.text()!r}")
    return f"6 个页签 / 分带 {len(cg)} 项可查 / 各行列表互不串 / 选完写回下拉 OK"


def case_export_append_services(out: Path) -> str:
    """★数据管理的「导出数据 / 追加数据」两个领域服务。

    两条最容易出事的语义：

    - **导出到已存在的库**只能替换同名图层，绝不能把整个库清空重建
      （``writeAsVectorFormatV3`` 的默认行为就是整库删掉重建）；
    - **追加**要按目标表的 SRID 投影、按同名匹配字段，并把没跟上的字段如实报出来。
    """
    from qgis.core import (
        QgsFeature,
        QgsGeometry,
        QgsPointXY,
        QgsVectorLayer,
    )

    from app.core.domain.append_layer import append_features_to_table
    from app.core.domain.create_data import available_drivers
    from app.core.domain.export_layer import ExportLayerError, ExportSpec, export_vector_layer

    def source_layer(count: int = 5) -> QgsVectorLayer:
        layer = QgsVectorLayer(
            f"Point?crs={CRS}&field=名称:string(20)&field=代码:integer&field=多余:string(10)",
            "源数据",
            "memory",
        )
        features = []
        for index in range(count):
            feature = QgsFeature(layer.fields())
            feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(116.0 + index * 0.01, 30.0)))
            feature.setAttributes([f"地块{index}", index, "x"])
            features.append(feature)
        layer.dataProvider().addFeatures(features)
        return layer

    # 1. 导出四种格式 + 条件筛选
    source = source_layer()
    for ext, driver, label in (
        (".shp", "ESRI Shapefile", "Shapefile"),
        (".gpkg", "GPKG", "GeoPackage"),
        (".sqlite", "SQLite", "SQLite"),
        (".gdb", "OpenFileGDB", "GDB"),
    ):
        if driver not in available_drivers():
            continue
        target = out / f"导出{ext}"
        if target.exists():
            import shutil

            shutil.rmtree(target) if target.is_dir() else target.unlink()
        spec = ExportSpec(
            path=str(out), file_name=target.name, driver=driver,
            layer_name="导出层", expression='"代码" >= 2',
        )
        export_vector_layer(source, spec)
        uri = (
            f"{target}|layername=导出层"
            if target.is_dir() or ext in (".gpkg", ".sqlite")
            else str(target)
        )
        layer = QgsVectorLayer(uri, "读回", "ogr")
        if not layer.isValid() or layer.featureCount() != 3:
            raise RuntimeError(
                f"{label} 导出回读不对：valid={layer.isValid()} 要素={layer.featureCount()}"
            )

    # 2. 导出到已存在的库：只替换同名图层，别的图层必须还在
    db = out / "导出目标.gpkg"
    db.unlink(missing_ok=True)
    create_container(db, "gpkg")
    add_empty_layer(db, "别的图层", "Polygon")
    add_empty_layer(db, "导出层", "Polygon")
    export_vector_layer(
        source,
        ExportSpec(path=str(out), file_name=db.name, driver="GPKG",
                    layer_name="导出层", expression='"代码" < 2'),
    )
    import sqlite3

    con = sqlite3.connect(db)
    try:
        names = [row[0] for row in con.execute(
            "SELECT table_name FROM gpkg_contents WHERE data_type='features'"
        )]
    finally:
        con.close()
    if "别的图层" not in names:
        raise RuntimeError(f"导出到已存在的库把别的图层弄没了：{names}")
    replaced = QgsVectorLayer(f"{db}|layername=导出层", "导出层", "ogr")
    if replaced.featureCount() != 2:
        raise RuntimeError(f"同名图层没被条件结果替换：{replaced.featureCount()}")

    # 3. 条件写错（引用了不存在的字段）要拦下
    try:
        export_vector_layer(
            source, ExportSpec(path=str(out), file_name="bad.gpkg", driver="GPKG",
                               layer_name="bad", expression='"不存在的字段" = 1'),
        )
    except ExportLayerError as exc:
        if "不存在" not in str(exc):
            raise RuntimeError(f"错误条件拦下了但文案不对：{exc}") from exc
    else:
        raise RuntimeError("引用了不存在字段的条件居然导出成功了")

    # 4. 追加：同名匹配 + 跨坐标系投影
    append_db = out / "追加目标.gpkg"
    append_db.unlink(missing_ok=True)
    create_container(append_db, "gpkg")
    CreateLayerInDbService().create(
        CreateLayerInDbSpec(
            db_path=str(append_db), layer_name="接收层", geometry_type="Point",
            crs_authid="EPSG:4547",
            fields=(FieldSpec("名称", type_key="text", length=20), FieldSpec("代码", type_key="integer")),
        )
    )
    result = append_features_to_table(source_layer(4), str(append_db), "接收层")
    if result.inserted != 4:
        raise RuntimeError(f"追加条数不对：{result.inserted}")
    if set(result.matched_fields) != {"名称", "代码"} or result.skipped_fields != ("多余",):
        raise RuntimeError(f"字段匹配结果不对：{result}")
    received = QgsVectorLayer(f"{append_db}|layername=接收层", "接收层", "ogr")
    if received.featureCount() != 4:
        raise RuntimeError(f"接收层要素数 {received.featureCount()} != 4")
    point = next(received.getFeatures()).geometry().asPoint()
    if abs(point.x()) < 100000:
        raise RuntimeError(f"追加时没按目标 SRID 投影：x={point.x()}")
    return f"导出 4 种格式按条件筛选 / 已存在库只换同名图层 / 追加 4 条并按 4547 投影 OK"


def case_export_dict(out: Path) -> str:
    """导出带字典的图层：GDB 写属性域，GPKG / SQLite 附带字典表与字段元数据。"""
    from osgeo import ogr

    from app.core.domain.create_data.dict_tables import create_local_dict_table
    from app.core.domain.dict_manage import DictService
    from app.core.domain.export_layer import ExportSpec, export_vector_layer
    from app.core.util.field_meta import FieldMeta, read_field_meta, upsert_field_meta
    from app.core.util.local_db_util import local_db_util
    from app.core.util.local_layer_loader import open_local_vector_table

    work = out / "export_dict"
    work.mkdir(parents=True, exist_ok=True)
    src = work / "src.gpkg"
    if src.exists():
        src.unlink()
    db = local_db_util(str(src))
    try:
        db.create_vector_table(
            "地块",
            columns=[("DLBM", "TEXT")],
            geometry_column="geom",
            geometry_type="POINT",
            srid=4490,
            register=True,
        )
        db.batch_add_vector_data(
            "地块", [{"attributes": {"DLBM": "1"}, "geometry": "POINT(100 30)"}]
        )
    finally:
        db.conn.close()
    # 自检目录会保留产物：重复跑时上一次的字典库还在，先把字典库清掉
    dicts_db = work / "dicts.db"
    if dicts_db.exists():
        dicts_db.unlink()
    service = DictService(dicts_db)
    category = service.create_category("测试字典")
    domain = service.create_domain(category.name, "FL_SYS_TEST-图斑地类")
    service.add_item(domain.id, "1", "I级")
    service.add_item(domain.id, "2", "II级")
    create_local_dict_table(
        str(src), "地类字典", source_category=category.name, dict_service=service
    )
    db = local_db_util(str(src))
    try:
        upsert_field_meta(
            db.conn,
            "地块",
            "DLBM",
            FieldMeta(
                dict_table="地类字典",
                dict_domain=domain.domain,
                dict_local_table="地类字典",
            ),
            commit=True,
        )
    finally:
        db.conn.close()
    layer, error = open_local_vector_table(str(src), "地块")
    if layer is None:
        raise RuntimeError(f"源图层打不开：{error}")

    # GPKG / SQLite：字典表 + spatial 注册 + 字段元数据 + 字段别名
    layer.setFieldAlias(layer.fields().indexOf("DLBM"), "权属性质")
    stages: list[str] = []
    for name, driver, ext in (
        ("dict_out.gpkg", "GPKG", ".gpkg"),
        ("dict_out.sqlite", "SQLite", ".sqlite"),
    ):
        target = work / name
        if target.exists():
            target.unlink()
        export_vector_layer(
            layer,
            ExportSpec(
                path=str(work),
                file_name=name,
                driver=driver,
                extension=ext,
                layer_name="地块",
                convert_dict=True,
            ),
            progress=stages.append,
        )
        if name == "dict_out.gpkg":
            if "正在写入图层…" not in stages or "导出完成" not in stages:
                raise RuntimeError(f"导出进度回调不对：{stages}")
        from app.core.util.local_db_util import describe_local_db

        cats = describe_local_db(str(target))["categories"]
        if "地块" not in cats["spatial"] or "地类字典" not in cats["dict"]:
            raise RuntimeError(f"{name} 没有同时导出图层与字典表：{cats}")
        db = local_db_util(str(target))
        try:
            metas = read_field_meta(db.conn, "地块")
            meta = metas.get("DLBM") or next(
                (value for key, value in metas.items() if key.casefold() == "dlbm"),
                None,
            )
        finally:
            db.conn.close()
        if meta is None or meta.dict_domain != domain.domain:
            raise RuntimeError(f"{name} 字段元数据没写：{meta}")
        # 别名也要导出（SQLite 目标写本工程的别名表）
        from app.core.util.local_layer_loader import open_local_vector_table

        reopened, error = open_local_vector_table(str(target), "地块")
        if reopened is None:
            raise RuntimeError(f"导出的图层打不开：{error}")
        fields = reopened.fields()
        index = fields.indexOf("dlbm")
        if index < 0:
            index = fields.indexOf("DLBM")
        field = fields.at(index) if index >= 0 else None
        if field is None or field.alias() != "权属性质":
            raise RuntimeError(
                f"{name} 字段别名没有导出：{field.alias() if field else None}"
            )
        if ext == ".sqlite":
            # SQLite 驱动可能把列名改成小写：重新打开也要能读到字典
            from app.core.util.dict_values import load_field_dict

            info = load_field_dict(reopened, "dlbm") or load_field_dict(
                reopened, "DLBM"
            )
            if info is None or info.name_for("1") != "I级":
                raise RuntimeError(f"SQLite 重新打开后读不到字典值：{info}")
        reopened.deleteLater()

    # GDB：coded value domain 挂到 DLBM 字段上；乱码别名导出时也要修复
    gdb = work / "dict_out.gdb"
    if gdb.exists():
        import shutil

        shutil.rmtree(gdb)
    layer.setFieldAlias(
        layer.fields().indexOf("DLBM"),
        "权属性质".encode("utf-8").decode("latin-1"),
    )
    export_vector_layer(
        layer,
        ExportSpec(
            path=str(work),
            file_name="dict_out.gdb",
            driver="OpenFileGDB",
            extension=".gdb",
            layer_name="地块",
            convert_dict=True,
        ),
    )
    ds = ogr.GetDriverByName("OpenFileGDB").Open(str(gdb))
    if ds is None:
        raise RuntimeError("导出的 GDB 打不开")
    try:
        gdb_layer = ds.GetLayerByName("地块")
        index = gdb_layer.GetLayerDefn().GetFieldIndex("DLBM")
        field_defn = gdb_layer.GetLayerDefn().GetFieldDefn(index)
        domain_name = field_defn.GetDomainName()
        if not domain_name:
            raise RuntimeError("GDB 字段没有挂接属性域")
        enum = ds.GetFieldDomain(domain_name).GetEnumeration()
        if enum.get("1") != "I级":
            raise RuntimeError(f"GDB 属性域内容不对：{enum}")
        if field_defn.GetAlternativeName() != "权属性质":
            raise RuntimeError(
                f"GDB 中文别名没有修复：{field_defn.GetAlternativeName()!r}"
            )
    finally:
        ds = None
    # 导出对话框：有字典时 GPKG 默认勾选；Shapefile 的「导出字典名称」默认不勾选
    from app.ui.dialogs.export_layer_dialog import ExportLayerDialog

    dlg = ExportLayerDialog(None, layer=layer, default_dir=str(work))
    try:
        if not dlg.dict_check.isChecked():
            raise RuntimeError("有字典时 GPKG「转换字典」应默认勾选")
        for index in range(dlg.format_combo.count()):
            fmt = dlg.format_combo.itemData(index)
            if fmt is not None and fmt.extension == ".shp":
                dlg.format_combo.setCurrentIndex(index)
                break
        if not dlg.dict_check.isEnabled() or dlg.dict_check.isChecked():
            raise RuntimeError("Shapefile 下「导出字典名称」应可用且默认不勾选")
        if "导出字典名称" not in dlg.dict_check.text():
            raise RuntimeError(f"Shapefile 字典勾选框文案不对：{dlg.dict_check.text()}")
    finally:
        dlg.deleteLater()
    layer.deleteLater()
    return "导出字典：GDB 属性域 / GPKG、SQLite 字典表与字段元数据 OK"


def case_import_gdb_domain(out: Path) -> str:
    """导入带属性域的 GDB：域落成库内字典表 + 字段元数据，属性表能显示描述。"""
    import shutil

    from osgeo import ogr
    from qgis.core import QgsVectorLayer

    from app.core.domain.import_layer import import_vector_layer_to_db
    from app.core.util.dict_values import load_field_dict
    from app.core.util.field_meta import read_field_meta
    from app.core.util.local_db_util import describe_local_db, local_db_util
    from app.core.util.local_layer_loader import open_local_vector_table

    work = out / "import_gdb_domain"
    work.mkdir(parents=True, exist_ok=True)
    gdb = work / "src.gdb"
    if gdb.exists():
        shutil.rmtree(gdb)
    driver = ogr.GetDriverByName("OpenFileGDB")
    ds = driver.CreateDataSource(str(gdb))
    layer = ds.CreateLayer("小班", geom_type=ogr.wkbPoint)
    layer.CreateField(ogr.FieldDefn("DLBM", ogr.OFTString))
    layer.CreateField(ogr.FieldDefn("QSXZ", ogr.OFTString))
    feature = ogr.Feature(layer.GetLayerDefn())
    feature["DLBM"] = "1"
    feature["QSXZ"] = "A"
    feature.SetGeometry(ogr.CreateGeometryFromWkt("POINT(100 30)"))
    layer.CreateFeature(feature)
    ds = None
    # 重新以可写方式打开，追加 coded value domain 并用新 FieldDefn 挂接。
    # 两个域：导入后必须并进同一张 dict_<图层名> 字典表。
    ds = driver.Open(str(gdb), 1)
    domain = ogr.CreateCodedFieldDomain(
        "DL_DOMAIN",
        "地类",
        ogr.OFTString,
        ogr.OFSTNone,
        {"1": "乔木林地", "2": "灌木林地"},
    )
    ds.AddFieldDomain(domain)
    ds.AddFieldDomain(
        ogr.CreateCodedFieldDomain(
            "QSXZ_DOMAIN",
            "权属性质",
            ogr.OFTString,
            ogr.OFSTNone,
            {"A": "集体土地所有权", "B": "国有土地所有权"},
        )
    )
    layer = ds.GetLayerByName("小班")
    for index, domain_name in ((0, "DL_DOMAIN"), (1, "QSXZ_DOMAIN")):
        original = layer.GetLayerDefn().GetFieldDefn(index)
        new_def = ogr.FieldDefn(original.GetName(), original.GetType())
        new_def.SetWidth(original.GetWidth())
        new_def.SetDomainName(domain_name)
        layer.AlterFieldDefn(index, new_def, ogr.ALTER_DOMAIN_FLAG)
    ds = None

    source = QgsVectorLayer(f"{gdb}|layername=小班", "小班", "ogr")
    if not source.isValid():
        raise RuntimeError("源 GDB 图层打不开")
    # 直接打开 GDB（未导入）时属性表也应能读到 OGR 属性域
    from app.core.util.dict_values import load_field_dict

    direct = load_field_dict(source, "DLBM")
    if direct is None or direct.name_for("1") != "乔木林地":
        raise RuntimeError(f"直接打开 GDB 时属性域没有读出来：{direct}")
    target = work / "out.gpkg"
    if target.exists():
        target.unlink()
    db = local_db_util(str(target))
    db.conn.close()
    result = import_vector_layer_to_db(
        source, {"kind": "local", "path": str(target)}, "EPSG:4490"
    )
    cats = describe_local_db(str(target))["categories"]
    if set(cats["dict"]) != {"dict_小班"}:
        raise RuntimeError(f"多个属性域没有合并成一张 dict_<图层名>：{cats}")
    db = local_db_util(str(target))
    try:
        meta = read_field_meta(db.conn, result.table_name).get("DLBM")
        meta_qsxz = read_field_meta(db.conn, result.table_name).get("QSXZ")
        domains = {
            str(row[0])
            for row in db.cursor.execute(
                'SELECT DISTINCT domain FROM "dict_小班" ORDER BY domain'
            )
        }
    finally:
        db.conn.close()
    if meta is None or meta.dict_domain != "DL_DOMAIN" or meta.dict_table != "dict_小班":
        raise RuntimeError(f"字段元数据没有挂属性域：{meta}")
    if (
        meta_qsxz is None
        or meta_qsxz.dict_domain != "QSXZ_DOMAIN"
        or meta_qsxz.dict_table != "dict_小班"
    ):
        raise RuntimeError(f"第二个属性域的字段元数据不对：{meta_qsxz}")
    if domains != {"DL_DOMAIN", "QSXZ_DOMAIN"}:
        raise RuntimeError(f"合并后的字典表域不齐：{domains}")
    imported, error = open_local_vector_table(str(target), result.table_name)
    if imported is None:
        raise RuntimeError(f"导入后的图层打不开：{error}")
    info = load_field_dict(imported, "DLBM")
    if info is None or info.name_for("1") != "乔木林地":
        raise RuntimeError(f"属性域值没有读出来：{info}")
    info_qsxz = load_field_dict(imported, "QSXZ")
    if info_qsxz is None or info_qsxz.name_for("A") != "集体土地所有权":
        raise RuntimeError(f"第二个属性域值没有读出来：{info_qsxz}")
    imported.deleteLater()
    source.deleteLater()
    return "导入 GDB 多属性域 → 合并成一张 dict_<图层名> / 字段元数据 / 显示值 OK"


def case_create_gdb_domain(out: Path) -> str:
    """创建数据导入带属性域的 GDB：域字段进字段表，字典随建库一起落库。"""
    import shutil

    from osgeo import ogr
    from qgis.core import QgsVectorLayer

    from app.core.domain.create_data.dict_tables import (
        DictPlan,
        build_dict_table_specs,
        source_inline_dicts,
    )
    from app.core.domain.create_data.fields import fields_from_layer
    from app.core.domain.create_data.library import (
        CreateLayerInDbService,
        CreateLayerInDbSpec,
    )
    from app.core.util.dict_values import load_field_dict
    from app.core.util.local_db_util import local_db_util
    from app.core.util.local_layer_loader import open_local_vector_table

    work = out / "create_gdb_domain"
    work.mkdir(parents=True, exist_ok=True)
    gdb = work / "src.gdb"
    if gdb.exists():
        shutil.rmtree(gdb)
    driver = ogr.GetDriverByName("OpenFileGDB")
    ds = driver.CreateDataSource(str(gdb))
    layer = ds.CreateLayer("小班", geom_type=ogr.wkbPoint)
    layer.CreateField(ogr.FieldDefn("DLBM", ogr.OFTString))
    feature = ogr.Feature(layer.GetLayerDefn())
    feature["DLBM"] = "1"
    feature.SetGeometry(ogr.CreateGeometryFromWkt("POINT(100 30)"))
    layer.CreateFeature(feature)
    ds = None
    ds = driver.Open(str(gdb), 1)
    ds.AddFieldDomain(
        ogr.CreateCodedFieldDomain(
            "DL_DOMAIN", "地类", ogr.OFTString, ogr.OFSTNone,
            {"1": "乔木林地", "2": "灌木林地"},
        )
    )
    layer = ds.GetLayerByName("小班")
    original = layer.GetLayerDefn().GetFieldDefn(0)
    new_def = ogr.FieldDefn(original.GetName(), original.GetType())
    new_def.SetWidth(original.GetWidth())
    new_def.SetDomainName("DL_DOMAIN")
    layer.AlterFieldDefn(0, new_def, ogr.ALTER_DOMAIN_FLAG)
    ds = None

    source = QgsVectorLayer(f"{gdb}|layername=小班", "小班", "ogr")
    if not source.isValid():
        raise RuntimeError("源 GDB 图层打不开")
    fields = fields_from_layer(source)
    dlbm = next((field for field in fields if field.name == "DLBM"), None)
    if dlbm is None or dlbm.dict_table != "DL_DOMAIN" or dlbm.dict_domain != "DL_DOMAIN":
        raise RuntimeError(f"属性域没有进字段表：{dlbm}")
    inline = source_inline_dicts(source, fields)
    if not inline or inline[0].category != "DL_DOMAIN":
        raise RuntimeError(f"属性域取值没有带进创建流程：{inline}")
    target = work / "out.gpkg"
    if target.exists():
        target.unlink()
    db = local_db_util(str(target))
    db.conn.close()
    domains = frozenset(
        (field.dict_table, field.dict_domain)
        for field in fields
        if field.dict_table and field.dict_domain
    )
    specs = build_dict_table_specs(
        str(target), DictPlan(domains=domains, inline=inline), None
    )
    created = CreateLayerInDbService().create(
        CreateLayerInDbSpec(
            db_path=str(target),
            layer_name="小班",
            geometry_type="Point",
            crs_authid="EPSG:4490",
            fields=tuple(fields),
            dict_tables=tuple(specs),
        )
    )
    created.deleteLater()
    reopened, error = open_local_vector_table(str(target), "小班")
    if reopened is None:
        raise RuntimeError(f"建出来的图层打不开：{error}")
    info = load_field_dict(reopened, "DLBM")
    if info is None or info.name_for("1") != "乔木林地":
        raise RuntimeError(f"属性域字典没有落库：{info}")
    reopened.deleteLater()
    source.deleteLater()
    return "创建数据导入 GDB 属性域 → 字段表字典列 + 库内字典表 OK"


def case_import_dict_merge(out: Path) -> str:
    """目标库已有同域字典表但缺条目：导入时合并补全，不另建 _1 表。"""
    import shutil

    from osgeo import ogr
    from qgis.core import QgsVectorLayer

    from app.core.domain.import_layer import import_vector_layer_to_db
    from app.core.util.field_meta import read_field_meta
    from app.core.util.local_db_util import describe_local_db, local_db_util

    work = out / "import_dict_merge"
    work.mkdir(parents=True, exist_ok=True)
    gdb = work / "src.gdb"
    if gdb.exists():
        shutil.rmtree(gdb)
    driver = ogr.GetDriverByName("OpenFileGDB")
    ds = driver.CreateDataSource(str(gdb))
    layer = ds.CreateLayer("小班", geom_type=ogr.wkbPoint)
    layer.CreateField(ogr.FieldDefn("DLBM", ogr.OFTString))
    feature = ogr.Feature(layer.GetLayerDefn())
    feature["DLBM"] = "1"
    feature.SetGeometry(ogr.CreateGeometryFromWkt("POINT(100 30)"))
    layer.CreateFeature(feature)
    ds = None
    ds = driver.Open(str(gdb), 1)
    ds.AddFieldDomain(
        ogr.CreateCodedFieldDomain(
            "DL_DOMAIN", "地类", ogr.OFTString, ogr.OFSTNone,
            {"1": "乔木林地", "2": "灌木林地"},
        )
    )
    layer = ds.GetLayerByName("小班")
    original = layer.GetLayerDefn().GetFieldDefn(0)
    new_def = ogr.FieldDefn(original.GetName(), original.GetType())
    new_def.SetWidth(original.GetWidth())
    new_def.SetDomainName("DL_DOMAIN")
    layer.AlterFieldDefn(0, new_def, ogr.ALTER_DOMAIN_FLAG)
    ds = None

    target = work / "out.gpkg"
    if target.exists():
        target.unlink()
    db = local_db_util(str(target))
    try:
        db.create_vector_table(
            "dict_小班",
            columns=[
                ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
                ("domain", "TEXT NOT NULL"),
                ("code", "TEXT NOT NULL"),
                ("name", "TEXT NOT NULL"),
                ("parent_code", "TEXT NOT NULL DEFAULT ''"),
                ("sort_order", "INTEGER NOT NULL DEFAULT 0"),
            ],
            geometry_column=None,
            register=False,
        )
        db.cursor.execute(
            'INSERT INTO "dict_小班" (domain, code, name) VALUES (?, ?, ?)',
            ("DL_DOMAIN", "1", "旧名称"),
        )
        db.add_domain_table(
            "dict_小班",
            "dict",
            '{"schema":"dict_v1","dict_table":"dict_小班","domains":["DL_DOMAIN"]}',
        )
        db.conn.commit()
    finally:
        db.conn.close()

    source = QgsVectorLayer(f"{gdb}|layername=小班", "小班", "ogr")
    result = import_vector_layer_to_db(
        source, {"kind": "local", "path": str(target)}, "EPSG:4490"
    )
    cats = describe_local_db(str(target))["categories"]
    if "dict_小班" not in cats["dict"] or any(
        name.startswith("dict_小班_") for name in cats["dict"]
    ):
        raise RuntimeError(f"同名字典表没有合并复用：{cats}")
    db = local_db_util(str(target))
    try:
        rows = [
            tuple(row)
            for row in db.cursor.execute(
                'SELECT code, name FROM "dict_小班" ORDER BY code'
            )
        ]
        meta = read_field_meta(db.conn, result.table_name).get("DLBM")
    finally:
        db.conn.close()
    if rows != [("1", "乔木林地"), ("2", "灌木林地")]:
        raise RuntimeError(f"字典表没有补全 / 更新：{rows}")
    if meta is None or meta.dict_domain != "DL_DOMAIN" or meta.dict_table != "dict_小班":
        raise RuntimeError(f"字段元数据不对：{meta}")
    source.deleteLater()
    return "dict_<图层名> 字典表合并补全（不建 _1 表）OK"


def case_fields_sort(out: Path) -> str:
    """创建数据字段表：表头排序只改显示，fields() 的真实顺序不变。"""
    from app.core.domain.create_data import FieldSpec
    from app.ui.dialogs.create_data_fields_step import FieldsStepPage, _COL_NAME

    page = FieldsStepPage(None)
    try:
        page.set_fields(
            [
                FieldSpec(name="B", alias="乙"),
                FieldSpec(name="A", alias="甲"),
                FieldSpec(name="C", alias="丙"),
            ]
        )
        page._on_header_sort(_COL_NAME)
        shown = [
            page.table.item(i, _COL_NAME).text()
            for i in range(page.table.rowCount())
        ]
        if shown != ["A", "B", "C"]:
            raise RuntimeError(f"字段表升序不对：{shown}")
        if [field.name for field in page.fields()] != ["B", "A", "C"]:
            raise RuntimeError("字段真实顺序被排序改变了")
        page._on_header_sort(_COL_NAME)
        shown = [
            page.table.item(i, _COL_NAME).text()
            for i in range(page.table.rowCount())
        ]
        if shown != ["C", "B", "A"]:
            raise RuntimeError(f"字段表降序不对：{shown}")
    finally:
        page.deleteLater()
    return "创建数据字段表临时排序通过"


def case_data_manage_menus(out: Path) -> str:
    """★数据管理右键菜单与两个弹窗的预选行为。

    用户点名的三件事：根节点右键时弹窗要停在对应页签（且本地页能直接新建库）；
    库节点要有删除连接 / 打开所在位置 / 创建数据 / 导入数据；图层节点要有
    追加数据 / 导出数据（可带条件，条件来自属性查询器但**不改图层的筛选**）。
    """
    from qgis.PyQt.QtWidgets import QPushButton
    from qgis.core import QgsFeature, QgsVectorLayer

    from app.ui.common.message_box import MessageBox as QMessageBox
    from app.ui.dialogs.add_connection import AddConnectionDialog
    from app.ui.dialogs.create_data_dialog import CreateDataDialog
    from app.ui.dialogs.export_layer_dialog import ExportLayerDialog
    from app.ui.dialogs.query_builder import QueryBuilderDialog
    from app.ui.docks.data_manage_dock import DataManageDock

    db_path = out / "菜单库.gpkg"
    db_path.unlink(missing_ok=True)
    create_container(db_path, "gpkg")
    db = {"kind": "local", "name": db_path.stem, "path": str(db_path)}

    # 1. 新建连接：按根节点预选页签；本地页有「新建数据库…」
    #    **弹窗要绑到变量**：临时对象一被回收，里面的 QTabWidget / 页面就悬空了
    local_dialog = AddConnectionDialog(None, default_kind="local")
    server_dialog = AddConnectionDialog(None, default_kind="server")
    if local_dialog.tabs.currentIndex() != 0:
        raise RuntimeError("本地数据库右键后弹窗没停在本地页")
    if server_dialog.tabs.currentIndex() != 1:
        raise RuntimeError("服务器数据库右键后弹窗没停在服务器页")
    local_page = local_dialog.tabs.widget(0)
    buttons = [b.text() for b in local_page.findChildren(QPushButton)]
    if "新建数据库…" not in buttons or "浏览…" not in buttons:
        raise RuntimeError(f"本地页该有浏览 + 新建数据库：{buttons}")

    # 2. 创建数据：目标库预选 / 直接停在建库页
    main = CreateDataDialog(None, default_crs=CRS, local_dbs=[db], default_target_db=str(db_path))
    picked = main.db_target_combo.currentData()
    if not isinstance(picked, dict) or picked.get("path") != str(db_path):
        raise RuntimeError(f"目标库没预选到右键那个库：{picked}")
    container_dialog = CreateDataDialog(None, default_crs=CRS, start_on_container=True)
    if container_dialog.tabs.currentIndex() != 1:
        raise RuntimeError("start_on_container 没停在本地数据库页")

    # 3. 右键菜单项与回调参数
    dock = DataManageDock(None)
    calls: list[tuple] = []
    dock._on_add_connection = lambda scope: calls.append(("add", scope))
    dock._on_create_data = lambda entry: calls.append(("create", (entry or {}).get("path")))
    dock._on_import = lambda entry: calls.append(("import", (entry or {}).get("path")))
    dock._on_remove_connection = lambda entry: calls.append(("remove", entry.get("path")))
    dock._on_import_data = lambda entry, cat, name: calls.append(("import_data", cat, name))
    dock._on_export_layer = lambda entry, cat, name: calls.append(("export", cat, name))

    def labels(payload: dict) -> list[str]:
        menu = dock._build_context_menu(payload)
        texts = [action.text() for action in menu.actions() if action.text()]
        menu.deleteLater()
        return texts

    if labels({"scope": "local"}) != ["添加数据库连接", "刷新"]:
        raise RuntimeError("本地根节点的菜单项不对")
    # 服务器根节点：动作**必须带上 server**（lambda 的默认值会被 triggered 的 bool 顶掉）
    server_menu = dock._build_context_menu({"scope": "server"})
    server_menu.actions()[0].trigger()
    server_menu.deleteLater()
    if calls[-1] != ("add", "server"):
        raise RuntimeError(f"服务器根节点没把 scope 传下去：{calls[-1:]}")

    if labels({"db": db})[:5] != [
        "导入图层", "创建数据", "打开文件所在位置", "删除连接", "刷新"
    ]:
        raise RuntimeError(f"库节点的菜单项不对：{labels({'db': db})}")
    if labels({"db": db, "cat": "spatial", "name": "小班"}) != [
        "加到地图上展示", "导出字段结构(XLS)", "检查字段结构", "编辑名称",
        "删除图层", "导入数据", "导出数据", "刷新"
    ]:
        raise RuntimeError("图层节点的菜单项不对")
    if "加到地图上展示" in labels({"db": db, "cat": "dict", "name": "字典表"}):
        raise RuntimeError("字典表没有几何，不该出现「加到地图上展示」")
    if "导入数据" in labels({"db": db, "cat": "dict", "name": "字典表"}):
        raise RuntimeError("非空间表不该出现「导入数据」")
    if "编辑名称" not in labels({"db": db, "cat": "dict", "name": "字典表"}):
        raise RuntimeError("字典表叶子应支持「编辑名称」")
    if "添加字典表" not in labels({"db": db, "cat": "dict"}):
        raise RuntimeError("字典分组节点应支持「添加字典表」")
    menu = dock._build_context_menu({"db": db, "cat": "spatial", "name": "小班"})
    for action in menu.actions():
        if action.text() in ("导入数据", "导出数据"):
            action.trigger()
    menu.deleteLater()
    if ("import_data", "spatial", "小班") not in calls or ("export", "spatial", "小班") not in calls:
        raise RuntimeError(f"追加/导出没带回图层信息：{calls[-2:]}")

    # 4. 导出对话框：四种格式、默认 GeoPackage、条件进 spec
    layer = QgsVectorLayer(f"Point?crs={CRS}&field=代码:integer", "小班", "memory")
    layer.dataProvider().addFeatures([QgsFeature(layer.fields()) for _ in range(3)])
    dialog = ExportLayerDialog(None, layer=layer, default_dir=str(out))
    formats = [dialog.format_combo.itemData(i).extension for i in range(dialog.format_combo.count())]
    if formats != [".shp", ".gdb", ".gpkg", ".sqlite"]:
        raise RuntimeError(f"导出格式不对：{formats}")
    if dialog.format_combo.currentData().extension != ".gpkg":
        raise RuntimeError("默认格式该是 GeoPackage")
    dialog._set_condition('"代码" >= 1')
    spec = dialog.spec()
    if spec is None or spec.expression != '"代码" >= 1' or spec.driver != "GPKG":
        raise RuntimeError(f"导出的 spec 不对：{spec}")
    if not dialog.path_edit.text().endswith("小班.gpkg"):
        raise RuntimeError(f"默认保存位置不对：{dialog.path_edit.text()}")

    # 5. 属性查询器「只取条件」：不改图层的子集串；老行为照旧
    source = QgsVectorLayer(f"Point?crs={CRS}&field=代码:integer", "源", "memory")
    source.dataProvider().addFeatures([QgsFeature(source.fields()) for _ in range(3)])
    only = QueryBuilderDialog(source, None, apply_to_layer=False)
    picked: list[str] = []
    only.sqlConfirmed.connect(picked.append)
    only.widget.sql_edit.setPlainText('"代码" >= 1')
    only._on_ok()
    if source.subsetString() != "" or picked != ['"代码" >= 1']:
        raise RuntimeError(f"只取条件模式改动了图层：subset={source.subsetString()!r} picked={picked}")
    applying = QueryBuilderDialog(source, None, apply_to_layer=True)
    applying.widget.sql_edit.setPlainText('"代码" >= 1')
    original_info = QMessageBox.information
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    try:
        applying._on_ok()
    finally:
        QMessageBox.information = original_info
    if source.subsetString() != '"代码" >= 1':
        raise RuntimeError(f"老行为（应用筛选）坏了：{source.subsetString()!r}")
    return "根节点预选页签 / 库节点五项 / 图层节点含追加导出 / 导出对话框 / 查询器只取条件 OK"


def case_data_management_fixes(out: Path) -> str:
    """★这一批用户报的细节：几何类型筛选 / 无字段表追加 / 隐藏服务器 / 导入别名 / GDB 导出。

    每条都是实际撞过的：

    1. 往面图层追加，只该列面数据源（点数据进去也会被库拒掉）；
    2. 追加到**没填字段**的表时，以前会误报「库里没有这个图层」（拿"有没有属性列"
       当"表在不在"）；
    3. 服务器数据库先隐藏（树节点 + 新建连接页签）；
    4. 库节点只留一个「导入图层」，且能像追加那样从本地文件挑；**导入要保留字段别名**；
    5. 导出 GDB 时选已有的 ``.gdb`` 就直接用它，别在里面再套一层；拼默认名要剥掉
       ``.shp`` 这类后缀（否则 ``result_small.shp.gdb``）。
    """
    from qgis.PyQt.QtWidgets import QPushButton
    from qgis.core import QgsFeature

    from app.core.domain.append_layer import append_features_to_table
    from app.core.domain.import_layer import import_vector_layer_to_db
    from app.core.util.local_layer_loader import open_local_vector_table
    from app.core.util.sublayers import geometry_kind_of_layer
    from app.ui.dialogs.add_connection import AddConnectionDialog
    from app.ui.dialogs.create_data_fields_step import ExternalDatasetDialog
    from app.ui.dialogs.export_layer_dialog import ExportLayerDialog, _clean_stem
    from app.ui.dialogs.import_layer_dialog import ImportLayerDialog
    from app.ui.docks.data_manage_dock import DataManageDock

    def memory_layer(name: str, geometry: str = "Point", fields: str = ""):
        from qgis.core import QgsVectorLayer

        return QgsVectorLayer(f"{geometry}?crs={CRS}{fields}", name, "memory")

    # 1. 几何类型筛选（工程图层 + 文件子图层）
    point_layer = memory_layer("点图层")
    polygon_layer = memory_layer("面图层", geometry="Polygon")
    if geometry_kind_of_layer(point_layer) != "point" or geometry_kind_of_layer(polygon_layer) != "polygon":
        raise RuntimeError("几何类别识别不对")
    picker = ExternalDatasetDialog(None, vector_layers=[point_layer, polygon_layer], geometry_kind="polygon")
    names = [picker.layer_list.item(i).text() for i in range(picker.layer_list.count())]
    if names != ["面图层"]:
        raise RuntimeError(f"数据源没按几何类型筛：{names}")
    gdb = out / "类型筛选.gdb"
    make_multilayer_gdb(gdb, [("面层", "Polygon"), ("线层", "LineString")])
    picker._load_source(str(gdb))
    listed = [picker.sublayer_list.item(i).text() for i in range(picker.sublayer_list.count())]
    if listed != ["面层"]:
        raise RuntimeError(f"文件里的子图层没按几何类型筛：{listed}")

    # 2. 追加到「只有几何列」的表
    db = out / "无字段表.gpkg"
    db.unlink(missing_ok=True)
    create_container(db, "gpkg")
    add_empty_layer(db, "ttt", "Point")  # 不填字段
    source = memory_layer("源", fields="&field=名称:string(20)")
    feature = QgsFeature(source.fields())
    from qgis.core import QgsGeometry, QgsPointXY

    feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(116, 30)))
    feature.setAttributes(["甲"])
    source.dataProvider().addFeatures([feature])
    try:
        result = append_features_to_table(source, str(db), "ttt")
    except Exception as exc:  # noqa: BLE001 —— 这就是用户报的误报
        raise RuntimeError(f"追加到无字段表失败：{exc}") from exc
    if result.inserted != 1:
        raise RuntimeError(f"追加条数不对：{result.inserted}")

    # 3. 服务器数据库隐藏
    dock = DataManageDock(None)
    titles = [dock.tree.topLevelItem(i).text(0) for i in range(dock.tree.topLevelItemCount())]
    if titles != ["本地数据库"]:
        raise RuntimeError(f"树里不该再有服务器根节点：{titles}")
    connection_dialog = AddConnectionDialog(None)  # 临时对象被回收会把 tabs 带走
    if connection_dialog.tabs.isTabVisible(1):
        raise RuntimeError("新建连接里服务器页签还看得见")

    # 4. 库节点只有一个导入入口；导入弹窗能从本地文件挑；别名导入后还在
    conn_labels = [
        action.text()
        for action in dock._build_context_menu({"db": {"kind": "local", "name": "x", "path": str(db)}}).actions()
        if action.text()
    ]
    if "导入数据" in conn_labels or "导入图层" not in conn_labels:
        raise RuntimeError(f"库节点的导入入口不对：{conn_labels}")
    import_dialog = ImportLayerDialog(
        None, vector_layers=[], databases=[{"kind": "local", "name": "x", "path": str(db)}]
    )
    # 导入弹窗的源选择入口叫「选择」（点它弹数据源选择器：工程图层 / 本地文件）
    if "选择" not in [b.text() for b in import_dialog.findChildren(QPushButton)]:
        raise RuntimeError("导入弹窗没有「选择」按钮")
    for kind, file_name, label in (
        ("gpkg", "别名导入.gpkg", "GeoPackage"),
        ("spatialite", "别名导入.sqlite", "SQLite / SpatiaLite"),
    ):
        target = out / file_name
        target.unlink(missing_ok=True)
        CreateContainerService().create(
            CreateContainerSpec(
                path=str(out), file_name=file_name, data_format=label,
                driver="GPKG" if kind == "gpkg" else "SQLite", extension=target.suffix,
            )
        )
        aliased = memory_layer("带别名", fields="&field=CODE:integer&field=NAME:string(20)")
        aliased.setFieldAlias(0, "编码")
        aliased.setFieldAlias(1, "名称")
        import_vector_layer_to_db(
            aliased, {"kind": "local", "name": target.stem, "path": str(target)}, CRS
        )
        back, error = open_local_vector_table(str(target), "带别名")
        aliases = (
            [field.alias() for field in back.fields() if field.name() in ("CODE", "NAME")]
            if back is not None else []
        )
        if aliases != ["编码", "名称"]:
            raise RuntimeError(f"{label} 导入后别名丢了：{aliases}（{error}）")

    # 5. 导出 GDB：选已有 .gdb 直接用；主名剥掉数据后缀
    if _clean_stem("result_small.shp") != "result_small" or _clean_stem("小班.gpkg") != "小班":
        raise RuntimeError("导出默认名没剥干净后缀")
    export_dialog = ExportLayerDialog(None, layer=point_layer, default_dir=str(out))
    for index in range(export_dialog.format_combo.count()):
        if export_dialog.format_combo.itemData(index).extension == ".gdb":
            export_dialog.format_combo.setCurrentIndex(index)
            break
    import app.ui.dialogs.export_layer_dialog as export_module

    original_open = export_module.open_file_dir
    export_module.open_file_dir = lambda *a, **k: str(gdb)
    try:
        export_dialog._on_browse()
    finally:
        export_module.open_file_dir = original_open
    if export_dialog.path_edit.text() != str(gdb):
        raise RuntimeError(f"选已有 .gdb 又套了一层：{export_dialog.path_edit.text()}")
    return "几何类型筛选 / 无字段表追加 / 服务器隐藏 / 导入别名保留 / GDB 导出路径 OK"


def case_auto_guid_and_import_data(out: Path) -> str:
    """★本轮五条：mzguid / 属性表删除刷新 / 已有 GDB 导出 / 导入数据（多选+替换）/ IS NULL。

    - **创建图层自动带 ``mzguid``**（成果数据的图斑唯一标识；用户自己建了就不重复加）；
    - **属性表**：编辑时删要素，表里那行要立刻消失（不接 ``featureDeleted`` 就"删除没效果"）；
      撤销删除要补回来；
    - **导出到已有 GDB**：加一个图层，不能报 ``already exists`` —— 判"是不是容器"要用
      扩展名，不能用驱动名（FileGDB 的驱动会被解析成 ``OpenFileGDB``）；
    - **导入数据**：可多选、可追加或替换（默认追加）；替换先清空再写；
    - 属性查询器有 ``IS NULL``。
    """
    import time

    from qgis.PyQt.QtWidgets import QApplication
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer

    from app.core.domain.append_layer import import_features_into_table
    from app.core.domain.create_data import with_mzguid
    from app.core.domain.export_layer import ExportSpec, export_vector_layer
    from app.core.util.local_layer_loader import open_local_vector_table
    from app.core.util.sublayers import list_sublayers
    from app.ui.dialogs.create_data_dialog import CreateDataDialog
    from app.ui.dialogs.create_data_fields_step import ExternalDatasetDialog
    from app.ui.dialogs.query_builder import QueryBuilderDialog
    from app.ui.dialogs.query_builder_widget import _OPERATORS
    from app.ui.docks.attribute_dock import PagedAttributeTableModel

    def memory_layer(name: str, fields: str = ""):
        return QgsVectorLayer(f"Point?crs={CRS}{fields}", name, "memory")

    def fill(layer, count: int, values: list) -> None:
        features = []
        for index in range(count):
            feature = QgsFeature(layer.fields())
            feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(116 + index * 0.01, 30)))
            feature.setAttributes(list(values))
            features.append(feature)
        layer.dataProvider().addFeatures(features)

    # 1. mzguid：空结构补一个；用户自建不重复；建出的文件 / 库内图层都带
    if [spec.name for spec in with_mzguid([])] != ["mzguid"]:
        raise RuntimeError("with_mzguid 没补出字段")
    own = [FieldSpec("MZGUID", type_key="text", length=50)]
    if with_mzguid(own) != tuple(own):
        raise RuntimeError("用户自建的 mzguid 不该再补一个")
    shp_dialog = CreateDataDialog(None, default_crs=CRS)
    for index in range(shp_dialog.format_combo.count()):
        if shp_dialog.format_combo.itemData(index).extension == ".shp":
            shp_dialog.format_combo.setCurrentIndex(index)
            break
    # shp 是一组同名文件：上一次跑剩的任意一个都会让创建被"目标已存在"拦下
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        (out / f"带guid{suffix}").unlink(missing_ok=True)
    shp_dialog.target_combo.setCurrentIndex(0)
    shp_dialog.path_edit.setText(str(out))
    shp_dialog.file_name_edit.setText("带guid.shp")
    shp_dialog.layer_name_edit.setText("带guid")
    shp_dialog._step = 1
    shp_dialog._sync_footer()
    shp_dialog.next_step()
    shp_dialog.accept()
    created = shp_dialog.outcome()
    if created is None or [f.name() for f in created.layer.fields()] != ["mzguid"]:
        raise RuntimeError("建出的文件图层没带 mzguid")
    db = out / "guid.gpkg"
    db.unlink(missing_ok=True)
    create_container(db, "gpkg")
    db_dialog = CreateDataDialog(None, default_crs=CRS, local_dbs=[{"kind": "local", "name": db.stem, "path": str(db)}])
    db_dialog.into_layer_name_edit.setText("库里图层")
    db_dialog.next_step()
    db_dialog.accept()
    created_db = db_dialog.outcome()
    if created_db is None or "mzguid" not in [f.name() for f in created_db.layer.fields()]:
        raise RuntimeError("库内图层没带 mzguid")

    # 2. 属性表：编辑时删要素 → 行消失；撤销 → 回来
    table_layer = memory_layer("属性表", "&field=名称:string(20)")
    fill(table_layer, 5, ["a"])
    table_layer.startEditing()
    model = PagedAttributeTableModel(table_layer, filter_str="1=1")
    deadline = time.time() + 15
    while not model._exhausted and time.time() < deadline:
        QApplication.processEvents()
        time.sleep(0.02)
    before = model.rowCount()
    victim = model._fids[2]
    table_layer.deleteFeature(victim)
    QApplication.processEvents()
    if model.rowCount() != before - 1 or victim in model._fids:
        raise RuntimeError("删要素后表里那一行没消失")
    table_layer.undoStack().undo()
    deadline = time.time() + 3
    while model.rowCount() != before and time.time() < deadline:
        QApplication.processEvents()
        time.sleep(0.02)
    if model.rowCount() != before:
        raise RuntimeError("撤销删除后表里没把行补回来")
    table_layer.rollBack()

    # 3. 导出到已有 GDB：第二个图层要能加进去
    gdb = out / "再次导出.gdb"
    import shutil as _shutil

    if gdb.exists():
        _shutil.rmtree(gdb)
    source = memory_layer("点源", "&field=代码:integer")
    fill(source, 3, [1])
    for layer_name in ("A", "B"):
        export_vector_layer(
            source,
            ExportSpec(path=str(out), file_name=gdb.name, driver="OpenFileGDB",
                       extension=".gdb", layer_name=layer_name),
        )
    names = [s.name for s in list_sublayers(str(gdb))]
    if not {"A", "B"} <= set(names):
        raise RuntimeError(f"往已有 GDB 导出第二个图层失败：{names}")

    # 4. 导入数据：多选 + 追加/替换
    target_db = out / "导入数据.gpkg"
    target_db.unlink(missing_ok=True)
    create_container(target_db, "gpkg")
    CreateLayerInDbService().create(
        CreateLayerInDbSpec(
            db_path=str(target_db), layer_name="目标", geometry_type="Point", crs_authid=CRS,
            fields=(FieldSpec("名称", type_key="text", length=20),),
        )
    )
    first = memory_layer("追加源", "&field=名称:string(20)")
    fill(first, 2, ["x"])
    second = memory_layer("另一份", "&field=名称:string(20)")
    fill(second, 3, ["y"])
    appended = import_features_into_table([first, second], str(target_db), "目标")
    if appended.inserted != 5 or appended.replaced != 0:
        raise RuntimeError(f"追加多源结果不对：{appended}")
    replaced = import_features_into_table([first], str(target_db), "目标", replace=True)
    target_layer, _error = open_local_vector_table(str(target_db), "目标")
    if target_layer is None or target_layer.featureCount() != 2:
        raise RuntimeError(f"替换后要素数不对：{target_layer.featureCount() if target_layer else None}")
    if replaced.replaced != 5:
        raise RuntimeError(f"替换清掉的条数不对：{replaced.replaced}")
    picker = ExternalDatasetDialog(None, vector_layers=[first, second], multi=True, allow_replace=True)
    picker.layer_list.selectAll()
    if len(picker.selected_layers()) != 2:
        raise RuntimeError("数据源弹窗不能多选")
    if picker.mode() != "append":
        raise RuntimeError("导入方式默认该是追加")
    picker.replace_radio.setChecked(True)
    if picker.mode() != "replace":
        raise RuntimeError("切替换没生效")

    # 5. IS NULL
    if "IS NULL" not in _OPERATORS:
        raise RuntimeError("属性查询器没有 IS NULL")
    builder = QueryBuilderDialog(source, None, apply_to_layer=False)
    if builder.widget.condition_error('"代码" IS NULL') is not None:
        raise RuntimeError("IS NULL 条件没通过校验")
    return "mzguid 自动字段 / 属性表删除刷新+撤销 / 已有 GDB 加图层 / 多源追加替换 / IS NULL OK"


def case_import_multi_and_edit_prompts(out: Path) -> str:
    """★导入弹窗（空列表 / 选择+删除 / 锁库 / 重名改名）/ 停止编辑先问保存 / 导出已存在确认 / 两组单选互不干扰。

    - 导入弹窗**初始空列表**；「选择」加层、「删除选中」删条目；不再问目标坐标系
      （每份数据按自己的坐标系入库）；重名自动改成 ``名称_1``；
      从左侧库节点进来时目标库**锁定**，从右侧进来可自由选；
    - 属性表「停止编辑」：保存 / 不保存 / 取消——删除在缓冲里只是临时的，
      选「不保存」要回滚回来、选「保存」才真正落盘；
    - 导出目标已存在（单文件 / 容器里的同名图层）要先问是否替换；
    - 数据源与导入方式**两组单选必须分开**：勾「替换」不能把数据源的勾选清掉。
    """
    from app.core.domain.import_layer import unique_table_name  # noqa: F401 —— 下面用到
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer

    from app.core.domain.export_layer import ExportSpec, existing_target_description
    from app.core.domain.import_layer import import_vector_layer_to_db
    from app.core.util.local_db_util import describe_local_db
    from app.ui.common.message_box import MessageBox as QMessageBox
    from app.ui.dialogs.create_data_fields_step import ExternalDatasetDialog
    from app.ui.dialogs.export_layer_dialog import ExportLayerDialog
    from app.ui.dialogs.import_layer_dialog import ImportLayerDialog
    from app.ui.docks.attribute_dock import PagedAttributeTableWidget

    def memory_layer(name: str) -> QgsVectorLayer:
        layer = QgsVectorLayer(f"Point?crs={CRS}&field=名称:string(20)", name, "memory")
        features = []
        for index in range(3):
            feature = QgsFeature(layer.fields())
            feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(116 + index, 30)))
            feature.setAttributes([f"v{index}"])
            features.append(feature)
        layer.dataProvider().addFeatures(features)
        return layer

    db = out / "多选导入.gpkg"
    db.unlink(missing_ok=True)
    create_container(db, "gpkg")
    first = memory_layer("甲层")
    second = memory_layer("乙层")

    # 1. 导入弹窗：**初始空列表** → 用「选择」加层 → 多选 → result_payload 返回多个
    dialog = ImportLayerDialog(
        None,
        vector_layers=[first, second],
        databases=[{"kind": "local", "name": db.stem, "path": str(db)}],
    )
    if dialog.layer_list.count() != 0:
        raise RuntimeError("待导入图层一开始该是空列表")
    if hasattr(dialog, "crs_combo"):
        raise RuntimeError("弹窗里不该再有目标坐标系下拉")
    if not (dialog.pick_btn.text() == "选择" and dialog.remove_btn.text() == "删除选中"):
        raise RuntimeError("按钮文案不对")
    if not (dialog._add_layer(first) and dialog._add_layer(second)):
        raise RuntimeError("加层失败")
    if dialog._add_layer(first):
        raise RuntimeError("同一层不该重复加进列表")
    # 删除选中只去掉选中的那条
    dialog.layer_list.clearSelection()
    dialog.layer_list.item(0).setSelected(True)
    dialog._on_remove_selected()
    if dialog.layer_list.count() != 1:
        raise RuntimeError("「删除选中」没按选中项删")
    dialog._add_layer(first)
    dialog.layer_list.selectAll()
    layers, target_db = dialog.result_payload()
    if len(layers) != 2 or target_db is None:
        raise RuntimeError(f"result_payload 没返回多个图层：{layers}")
    # 重名自动按序号改名（两张同名表不能都叫「甲层」）
    taken: set[str] = set()
    names = []
    for layer in layers:
        name = unique_table_name(str(db), layer.name(), taken=taken)
        taken.add(name)
        names.append(name)
        import_vector_layer_to_db(layer, target_db, layer.crs().authid(), table_name=name)
    if names[0] == names[1]:
        raise RuntimeError(f"同批同名图层没自动改名：{names}")
    stored = describe_local_db(db)["categories"]["spatial"]
    if not set(names) <= set(stored):
        raise RuntimeError(f"多选导入没有全部进库：{stored}（期望 {names}）")
    # 再导一次同名图层 → 序号名
    again = unique_table_name(str(db), first.name())
    if again != f"{first.name()}_1":
        raise RuntimeError(f"重名时该改成 名称_1，实际 {again}")

    # 目标库锁定：从左侧库节点进来锁死，从右侧进来可切
    locked_dialog = ImportLayerDialog(
        None, vector_layers=[first], databases=[{"kind": "local", "name": db.stem, "path": str(db)}],
        default_db={"kind": "local", "name": db.stem, "path": str(db)}, lock_db=True,
    )
    if locked_dialog.db_combo.isEnabled():
        raise RuntimeError("从左侧库节点进来时目标库该锁定")
    free_dialog = ImportLayerDialog(
        None, vector_layers=[first],
        databases=[{"kind": "local", "name": db.stem, "path": str(db)}],
        default_db={"kind": "local", "name": db.stem, "path": str(db)},
    )
    if not free_dialog.db_combo.isEnabled():
        raise RuntimeError("从右侧进来时目标库该可切换")

    # 2. 停止编辑：取消留在编辑态；不保存回滚；保存才落盘
    edit_layer = memory_layer("编辑层")
    widget = PagedAttributeTableWidget(edit_layer, 50, None)
    widget._start_edit()
    victim = next(edit_layer.getFeatures()).id()
    edit_layer.deleteFeature(victim)
    original_question = QMessageBox.question
    try:
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel)
        widget._stop_edit()
        if not edit_layer.isEditable():
            raise RuntimeError("选「取消」不该退出编辑")
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Discard)
        widget._stop_edit()
        if edit_layer.isEditable() or edit_layer.featureCount() != 3:
            raise RuntimeError("选「不保存」该回滚并恢复被删的要素")
        widget._start_edit()
        edit_layer.deleteFeature(victim)
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Save)
        widget._stop_edit()
        if edit_layer.isEditable() or edit_layer.featureCount() != 2:
            raise RuntimeError("选「保存」该真正落盘")
    finally:
        QMessageBox.question = original_question

    # 3. 导出：已存在要能判出来（文件 / 容器里的同名图层）
    shp = out / "已存在.shp"
    shp.write_text("x", encoding="utf-8")
    if "文件已存在" not in existing_target_description(
        ExportSpec(path=str(out), file_name=shp.name, driver="ESRI Shapefile",
                   extension=".shp", layer_name="x")
    ):
        raise RuntimeError("单文件已存在没判出来")
    if "已有同名图层" not in existing_target_description(
        ExportSpec(path=str(out), file_name=db.name, driver="GPKG",
                   extension=".gpkg", layer_name="甲层")
    ):
        raise RuntimeError("容器里的同名图层没判出来")
    if existing_target_description(
        ExportSpec(path=str(out), file_name=db.name, driver="GPKG",
                   extension=".gpkg", layer_name="全新的层")
    ):
        raise RuntimeError("容器里没有同名图层时不该提示")
    # 弹窗：选「否」不往下走
    export_dialog = ExportLayerDialog(None, layer=first, default_dir=str(out))
    for index in range(export_dialog.format_combo.count()):
        if export_dialog.format_combo.itemData(index).extension == ".gpkg":
            export_dialog.format_combo.setCurrentIndex(index)
            break
    export_dialog.path_edit.setText(str(db))
    export_dialog.layer_name_edit.setText("甲层")
    accepted: list[bool] = []
    export_dialog.accept = lambda: accepted.append(True)
    try:
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
        export_dialog._on_ok()
        if accepted:
            raise RuntimeError("选「否」不该继续导出")
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
        export_dialog._on_ok()
        if accepted != [True]:
            raise RuntimeError("选「是」才该继续")
    finally:
        QMessageBox.question = original_question

    # 4. 两组单选互不干扰（数据源 vs 导入方式）
    picker = ExternalDatasetDialog(None, vector_layers=[first, second], multi=True, allow_replace=True)
    picker.project_radio.setChecked(True)
    picker.layer_list.selectAll()
    picker.replace_radio.setChecked(True)
    if not picker.project_radio.isChecked() or len(picker.selected_layers()) != 2:
        raise RuntimeError("勾「替换」把数据源的勾选清掉了")
    picker.append_radio.setChecked(True)
    if picker.mode() != "append" or not picker.project_radio.isChecked():
        raise RuntimeError("再切回追加时数据源没保持")
    return "导入(空列表/选择+删除/锁库/重名改名) / 停止编辑三分支 / 导出已存在确认 / 两组单选独立 OK"


def case_arcmap_shp_and_editable_gpkg(out: Path) -> str:
    """★ArcMap 打不开导出的 shp / GPKG 图层不能编辑（用户报的，两条根因）。

    1. **shp 的 DBF 字段名只有 10 字节**，GDAL 按**字节**截断中文名会把字符切一半，
       得到非法 UTF-8 的名字 —— QGIS 能容忍，ArcMap 打开就报错。导出前按字符截断、
       去重、保证以字母开头（``shapefile_field_names``），文件名一定合法。
    2. **QGIS 的 OGR provider 在「没有整型主键」的 gpkg 表上不暴露删除 / 改值能力** ——
       属性表里删要素、改单元格全部静默失败（用户看到的就是"删除没效果、停编停不掉"）。
       建表带 ``fid INTEGER PRIMARY KEY`` 后编辑能力齐全；把它再导入别的库时不能把
       ``fid`` 当普通列复制过去（否则目标表又不可编辑）。
    """
    import shutil
    import sqlite3
    import struct

    from qgis.core import (
        QgsFeature,
        QgsField,
        QgsGeometry,
        QgsPointXY,
        QgsVectorDataProvider,
        QgsVectorLayer,
    )
    from qgis.PyQt.QtCore import QMetaType

    from app.core.domain.editing.service import LayerEditService
    from app.core.domain.export_layer import ExportSpec, export_vector_layer, shapefile_field_names
    from app.core.domain.import_layer import import_vector_layer_to_db
    from app.core.util.local_layer_loader import open_local_vector_table

    # 1. 字段名改写规则：合法 UTF-8、≤10 字节、字母开头、不重名
    fields = [
        QgsField("图斑编号", QMetaType.Type.QString, "string", 50, 0),
        QgsField("图斑编号核查时间", QMetaType.Type.QString, "string", 50, 0),
        QgsField("CHECK_RESULT_LONG", QMetaType.Type.QString, "string", 50, 0),
        QgsField("a b.c", QMetaType.Type.QString, "string", 50, 0),
    ]
    layer = QgsVectorLayer("Polygon?crs=EPSG:4490", "变化核实图斑", "memory")
    layer.dataProvider().addAttributes(fields)
    layer.updateFields()
    names = shapefile_field_names(layer.fields())
    used: set[str] = set()
    for original, renamed in names.items():
        raw = renamed.encode("utf-8")
        if len(raw) > 10:
            raise RuntimeError(f"「{original}」改出来的名字超过 10 字节：{renamed!r}")
        if not (renamed[0].isascii() and renamed[0].isalpha()):
            raise RuntimeError(f"「{original}」改出来的名字不是字母开头：{renamed!r}")
        if renamed.lower() in used:
            raise RuntimeError(f"「{original}」改出来的名字与别人重名：{renamed!r}")
        used.add(renamed.lower())

    # 真的导出一份 shp 再读 DBF 头：名字必须是合法 UTF-8、记录数不能丢
    layer.dataProvider().addFeatures([QgsFeature(layer.fields()) for _ in range(2)])
    target = out / "arcmap.shp"
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        (out / f"arcmap{suffix}").unlink(missing_ok=True)
    export_vector_layer(
        layer,
        ExportSpec(path=str(out), file_name=target.name, driver="ESRI Shapefile",
                   extension=".shp", layer_name="arcmap"),
    )
    raw = (out / "arcmap.dbf").read_bytes()
    count = struct.unpack("<I", raw[4:8])[0]
    if count != 2:
        raise RuntimeError(f"导出的 shp 记录数 {count} != 2（要素被丢了）")
    header_len = struct.unpack("<H", raw[8:10])[0]
    pos = 32
    while pos < header_len - 1:
        raw_name = raw[pos:pos + 11].split(b"\x00")[0]
        if raw_name[:1] == b"\x0d" or not raw_name:
            break
        try:
            text = raw_name.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"DBF 字段名不是合法 UTF-8：{raw_name!r}") from exc
        if not text[0].isalpha():
            raise RuntimeError(f"DBF 字段名不是字母开头：{text!r}")
        pos += 32

    # 2. 库里建出的图层必须可编辑；再导入别的库不会把 fid 复制过去
    db = out / "可编辑.gpkg"
    db.unlink(missing_ok=True)
    create_container(db, "gpkg")
    CreateLayerInDbService().create(
        CreateLayerInDbSpec(
            db_path=str(db), layer_name="层", geometry_type="Point", crs_authid=CRS,
            fields=(FieldSpec("名称", type_key="text", length=20),),
        )
    )
    editable, error = open_local_vector_table(str(db), "层")
    if editable is None:
        raise RuntimeError(f"库里图层打不开：{error}")
    capabilities = editable.dataProvider().capabilities()
    if not capabilities & QgsVectorDataProvider.Capability.DeleteFeatures:
        raise RuntimeError("gpkg 图层不支持删除要素（缺整型主键）")
    if not capabilities & QgsVectorDataProvider.Capability.ChangeAttributeValues:
        raise RuntimeError("gpkg 图层不支持修改属性（缺整型主键）")
    features = []
    for index in range(3):
        feature = QgsFeature(editable.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(116 + index, 30)))
        feature.setAttributes(["x"] + [None] * (editable.fields().size() - 1))
        features.append(feature)
    editable.dataProvider().addFeatures(features)
    editable, _error = open_local_vector_table(str(db), "层")
    editing = LayerEditService()
    editable.startEditing()
    if not editable.deleteFeature(next(editable.getFeatures()).id()):
        raise RuntimeError("deleteFeature 仍然失败（主键没生效？）")
    if not editing.stop(editable, save=False):
        raise RuntimeError("不保存停编没有真正结束编辑")
    if editable.isEditable() or editable.featureCount() != 3:
        raise RuntimeError("回滚后编辑态 / 要素数不对")

    db2 = out / "再导入.gpkg"
    db2.unlink(missing_ok=True)
    create_container(db2, "gpkg")
    import_vector_layer_to_db(
        editable, {"kind": "local", "name": db2.stem, "path": str(db2)}, CRS
    )
    con = sqlite3.connect(db2)
    try:
        columns = [row[1] for row in con.execute('PRAGMA table_info("层")')]
    finally:
        con.close()
    if columns != ["fid", "名称", "geom"]:
        raise RuntimeError(f"导入后目标表列不对（fid 被复制了？）：{columns}")
    imported, _err = open_local_vector_table(str(db2), "层")
    if not imported.dataProvider().capabilities() & QgsVectorDataProvider.Capability.DeleteFeatures:
        raise RuntimeError("导入出来的表不可编辑")
    return "shp 字段名合法（ArcMap 可读）/ gpkg 带主键可编辑 / 再导入不复制 fid OK"


def case_sublayer_picker_and_no_io_on_select(out: Path) -> str:
    """★图层面板「添加图层」选容器数据源要列图层；导入数据弹窗选中时不能再打开文件。

    - 图层面板选 ``.gdb`` / ``.gpkg`` / ``.sqlite`` 时，先列出里面的全部图层，可多选后
      加载（以前 gpkg/sqlite 只开第一层、GDB 整库全加）；
    - 「导入数据」里挑本地文件后，**选中变化不该去打开数据源**（每次都开一遍会把界面
      卡死，用户报的）；打开只发生在点「确定」时，且只在选中的那几个上。
    """
    import shutil

    from qgis.core import (
        QgsCoordinateTransformContext,
        QgsVectorFileWriter,
        QgsVectorLayer,
    )

    import app.ui.dialogs.create_data_fields_step as step_module
    from app.ui.dialogs.create_data_fields_step import ExternalDatasetDialog
    from app.ui.dialogs.sublayer_pick_dialog import SublayerPickDialog

    gdb = out / "选择图层.gdb"
    if gdb.exists():
        shutil.rmtree(gdb)
    action = QgsVectorFileWriter.ActionOnExistingFile
    for index, (name, geometry) in enumerate((("小班", "Polygon"), ("道路", "LineString"), ("样点", "Point"))):
        source = QgsVectorLayer(f"{geometry}?crs={CRS}", name, "memory")
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "OpenFileGDB"
        options.layerName = name
        options.fileEncoding = "UTF-8"
        options.actionOnExistingFile = (
            action.CreateOrOverwriteFile if index == 0 else action.CreateOrOverwriteLayer
        )
        QgsVectorFileWriter.writeAsVectorFormatV3(
            source, str(gdb), QgsCoordinateTransformContext(), options
        )

    # 1. 子图层选择框
    picker = SublayerPickDialog(None, path=str(gdb))
    listed = [picker.layer_list.item(i).text() for i in range(picker.layer_list.count())]
    if set(listed) != {"小班", "道路", "样点"}:
        raise RuntimeError(f"子图层没列全：{listed}")
    if picker.selected_sublayers():
        raise RuntimeError("多图层数据源不该默认预选")
    picker.layer_list.selectAll()
    if len(picker.selected_sublayers()) != 3:
        raise RuntimeError("子图层选择框不能多选")

    # 2. 导入数据弹窗：选中不打开、确定才打开
    calls: list[str] = []
    original = step_module.open_sublayer
    step_module.open_sublayer = lambda path, sub: (calls.append(sub.name), original(path, sub))[1]
    try:
        dialog = ExternalDatasetDialog(None, multi=True, allow_replace=True)
        dialog.file_radio.setChecked(True)
        dialog._load_source(str(gdb))
        if calls:
            raise RuntimeError(f"列子图层时就打开了数据源：{calls}")
        dialog.sublayer_list.selectAll()
        if calls:
            raise RuntimeError(f"选中变化打开了数据源（会卡死）：{calls}")
        layers, error = dialog.resolve_all()
        if len(layers) != 3 or error:
            raise RuntimeError(f"确定时没打开全部选中：{len(layers)}（{error}）")
        if len(calls) != 3:
            raise RuntimeError(f"打开次数 {len(calls)} != 选中数 3")
    finally:
        step_module.open_sublayer = original
    return "容器数据源先列图层再加载 / 选中不打开数据源 / 确定才打开 OK"


def case_fields_table_interactions(out: Path) -> str:
    """★字段表的交互与代价：删一行不能触发整表重校验，新加的行不能被筛选藏起来。

    两条都踩过：

    1. ``_refresh_marks`` 早先无条件把「正在重填」的闸门（``_loading``）置回 False，
       而它会在 ``set_fields`` 填表途中被调到（``setRowCount(0)`` 丢选区 → 选中变化）。
       闸门一破，之后每个 ``setItem`` 都触发 ``itemChanged`` → ``fieldsChanged`` →
       对话框整表重校验。实测 200 个字段删一行要 **5.8 秒 / 1593 次全量校验**。
    2. 「字段筛选」开着时新加的行名字还空着，必然「不匹配」→ 被藏起来：用户看着像没加上，
       接着被「存在空字段名」拦住，而那一行点都点不到。
    """
    from app.ui.dialogs.create_data_dialog import CreateDataDialog
    from app.ui.dialogs.create_data_fields_step import FieldsStepPage

    page = FieldsStepPage(None)
    many = [FieldSpec(f"F_{i:03d}", f"字段{i}", "text", 20, 0) for i in range(200)]
    page.set_fields(many)
    if len(page.fields()) != 200:
        raise RuntimeError(f"填 200 个字段后读回 {len(page.fields())} 个")

    dlg = make_dialog(out)
    calls = {"n": 0}
    original = dlg._refresh_hint

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    dlg._refresh_hint = counting
    page.fieldsChanged.connect(dlg._refresh_hint)
    page.table.selectRow(5)
    page.delete_selected()
    if len(page.fields()) != 199:
        raise RuntimeError("删一行之后字段数不对")
    if calls["n"] > 5:
        raise RuntimeError(
            f"删一行触发了 {calls['n']} 次全量校验（该在 1 次上下）—— 重填时的信号没被挡住"
        )

    # 筛选开着时新加的行必须看得见（否则它藏起来、还被校验拦住）
    page.search_edit.setText("zzz")
    page.filter_check.setChecked(True)
    page._apply_filter()
    page._append_field()
    last = page.table.rowCount() - 1
    if page.fields()[-1].name != "":
        raise RuntimeError("新加的行不是空字段")
    if page.table.isRowHidden(last):
        raise RuntimeError("筛选开着时新加的行被藏起来了（用户看不见也点不到）")

    # 一个真实库都没登记时点「＋新增数据库…」：不该把下拉停到「浏览…」上
    bare = CreateDataDialog(None, default_crs=CRS)
    combo = bare.db_target_combo
    combo.setCurrentIndex(combo.count() - 1)
    if combo.currentData() != "new":
        raise RuntimeError(f"没登记库时退到了 {combo.currentText()!r}，该留在「＋新增数据库…」")
    return f"删一行只 1 次全量校验 / 筛选下新行可见 / 无库时不退到「浏览…」OK（实测 {calls['n']} 次）"


def case_field_name_matching(out: Path) -> str:
    """★字段名被格式改写后的配对：不能把别名 / 约束打到隔壁那一列上。

    用户报过的场景形状：``AREA_HA`` 与 ``AREA_HA_TOTAL`` 一起建，Shapefile 把后者截成
    ``AREA_HA_TO``。早先「扫到第一个是前缀的就配」会把 AREA_HA_TOTAL 配到 AREA_HA 上 ——
    别名互相覆盖、不可为空 / 隐藏也打错列（实测复现过）。这里把配对规则钉死：
    **先精确匹配，再按最长公共前缀配，且一列只能被占一次**。
    """
    from app.core.domain.create_data import (
        CreateDataService,
        CreateDataSpec,
        FieldSpec,
        configure_created_layer,
        match_layer_fields,
    )

    fields = (
        FieldSpec("AREA_HA", "面积甲", "text", 10, 0, nullable=False),
        FieldSpec("AREA_HA_TOTAL", "面积乙", "text", 10, 0, visible=False),
        FieldSpec("CHECK_RESULT", "检查结果", "text", 10, 0),
    )
    shp = out / "match_names.shp"
    shp.unlink(missing_ok=True)
    layer = CreateDataService().create(
        CreateDataSpec(
            path=str(out), file_name=shp.name, data_format="Shapefile",
            driver="ESRI Shapefile", extension=".shp", layer_name="match_names",
            geometry_type="Point", crs_authid=CRS, fields=fields,
        )
    )
    names = [f.name() for f in layer.fields()]
    if "AREA_HA" not in names or not any(n.startswith("AREA_HA_T") for n in names):
        return f"{shp.name} 这次没截断出重名前缀（{names}），跳过配对验证"

    matched = match_layer_fields(layer.fields(), fields)
    if len(set(matched)) != len(matched) or any(i < 0 for i in matched):
        raise RuntimeError(f"配对结果不对（该一对一、且都配上）：{matched}")
    if matched[0] == matched[1]:
        raise RuntimeError(f"两个前缀相同的字段配到了同一列：{matched}")
    if not layer.fields().at(matched[1]).name().startswith("AREA_HA_T"):
        raise RuntimeError(f"AREA_HA_TOTAL 配错了列：{layer.fields().at(matched[1]).name()}")

    notes = configure_created_layer(layer, fields, in_db=False)
    aliases = {f.name(): f.alias() for f in layer.fields()}
    if aliases.get("AREA_HA") != "面积甲":
        raise RuntimeError(f"AREA_HA 的别名被覆盖了：{aliases}")
    hidden = [f.name() for f in layer.fields() if f.editorWidgetSetup().type() == "Hidden"]
    if len(hidden) != 1 or not hidden[0].startswith("AREA_HA_T"):
        raise RuntimeError(f"「不可见」打到了别的列上：{hidden}")
    if not any("字段名改写了" in n for n in notes):
        raise RuntimeError(f"字段名被截断却没提示：{notes}")
    return f"{shp.name} 重名前缀字段一对一配对 / 别名与隐藏没打错列 / 截断有提示 OK"


def case_numeric_cell_guard(out: Path) -> str:
    """★「字段长度 / 小数位数」填了怪值不能把程序带走。

    单元格与模板都可能填进 ``inf`` / ``1e999``（``float()`` 认，``int()`` 抛
    :class:`OverflowError`），而解析是从 Qt 槽里调的 —— 异常冒出去会直接结束进程
    （实测），用户只看到程序没了。这里钉住「解析一定给得出一个合用的数」。
    """
    from app.core.domain.create_data import MAX_FIELD_LENGTH, MAX_FIELD_PRECISION, to_int

    cases = (
        ("18", 18), (" 50 ", 50), ("", 0), ("abc", 0), ("inf", 0), ("-inf", 0),
        # inf 与 1e999 都落到 OverflowError → 按 0（= 用类型默认值，比夹到上限更有用）；
        # 有限但荒唐的大数才夹到上限
        ("1e999", 0), ("1e30", MAX_FIELD_LENGTH), ("99999999", MAX_FIELD_LENGTH),
        ("-5", 0), ("2.7", 2),
    )
    for raw, want in cases:
        got = to_int(raw, MAX_FIELD_LENGTH)
        if got != want:
            raise RuntimeError(f"to_int({raw!r}) = {got}，期望 {want}")
    if to_int("99", MAX_FIELD_PRECISION) != 20:
        raise RuntimeError("小数位数没夹到上限")
    # 表格里真的填进去也不该出事（这条走的是 itemChanged → 整表取值那条链）
    from app.ui.dialogs.create_data_fields_step import FieldsStepPage

    page = FieldsStepPage(None)
    page.set_fields([FieldSpec("A", "甲", "text", 50, 0)])
    page.table.item(0, 4).setText("inf")  # 字段长度列
    if page.fields()[0].length != 0:
        raise RuntimeError(f"单元格里的 inf 没被吞掉：{page.fields()[0].length}")
    return f"to_int 夹上限 / 吞掉 inf 与 1e999 / 表格里填怪值不崩 OK（{len(cases)} 组）"


def case_template_write_failure(out: Path) -> str:
    """★写模板失败（只读 / 被 Excel 占着）必须转成 FieldTemplateError，不能冒出槽外。

    ``os.remove`` 与写盘在 Windows 上会抛 ``PermissionError``，而这条链是从按钮槽里
    调过来的 —— 不转成自己的异常类型，异常会冒到 Qt 事件循环外面，**直接结束进程**。
    """
    from app.core.domain.create_data import FieldTemplateError, write_template, xlsx_available

    if not xlsx_available():
        return "本机没有可写 XLSX 的驱动，跳过写失败验证"

    target = out / "只读模板.xlsx"
    target.unlink(missing_ok=True)
    target.mkdir()  # 拿一个同名目录占位：写同名文件必定失败
    try:
        write_template(str(target))
    except FieldTemplateError:
        return "写模板失败被转成 FieldTemplateError（不会冒出槽外）OK"
    except Exception as exc:  # noqa: BLE001 —— 这里要的就是「别的是错的」
        raise RuntimeError(f"抛出了不是 FieldTemplateError 的 {type(exc).__name__}: {exc}") from exc
    finally:
        target.rmdir()
    raise RuntimeError("写进一个同名目录居然成功了？")



def case_dialog_structure(out: Path) -> str:
    """对话框结构：两个页签（单一图层在前）、三种形态、两类格式下拉各司其职。"""
    from app.ui.dialogs.create_data_dialog import CreateDataDialog

    dlg = make_dialog(out)
    if dlg.tabs.count() != 2:
        raise RuntimeError(f"页签数 {dlg.tabs.count()} != 2")
    titles = [dlg.tabs.tabText(i) for i in range(2)]
    if titles != ["单图层数据", "本地数据库"]:
        raise RuntimeError(f"页签标题 / 顺序不对：{titles}")
    if dlg.tabs.currentIndex() != 0:
        raise RuntimeError("默认该停在「单图层数据」页")
    if dlg.target_combo.currentData() != "into_db":
        raise RuntimeError(f"目标默认成了 {dlg.target_combo.currentText()!r}，应是「写入已有本地数据库」")
    if dlg.target_stack.currentIndex() != 1:
        raise RuntimeError("目标栈没跟上下拉初值（默认目标那一页没显示出来）")
    if dlg.target_stack.count() != 2:
        raise RuntimeError(f"目标栈页数 {dlg.target_stack.count()} != 2")

    db_fmts = [dlg.db_format_combo.itemData(i) for i in range(dlg.db_format_combo.count())]
    if [f.driver for f in db_fmts] != ["GPKG", "SQLite"]:
        raise RuntimeError(f"本地数据库页的格式 {[f.label for f in db_fmts]}")

    # 三种形态：页签二两种目标 + 页签一
    if dlg._kind() != "into_db":  # 默认目标就是 into_db
        raise RuntimeError(f"默认 _kind={dlg._kind()}")
    dlg.target_combo.setCurrentIndex(0)
    if dlg._kind() != "file":
        raise RuntimeError(f"模式 a _kind={dlg._kind()}")
    dlg.tabs.setCurrentIndex(1)
    if dlg._kind() != "container":
        raise RuntimeError(f"本地数据库页 _kind={dlg._kind()}")

    # 页脚是个栈：向导页一套（取消/上一步/下一步|创建），容器页一套（应用/取消/确定）
    dlg.tabs.setCurrentIndex(0)
    step1 = _footer_texts(dlg, 0)
    if step1 != ["取消", "上一步", "下一步"]:
        raise RuntimeError(f"向导第 1 步的按钮不对：{step1}")
    if dlg.create_btn.objectName() == "DialogPrimary":
        raise RuntimeError("「创建」不该用 DialogPrimary 主色实心（与取消 / 上一步同色）")
    if not dlg.next_btn.isDefault():
        raise RuntimeError("向导第 1 步该以「下一步」为默认按钮（回车不能直接创建）")
    dlg.tabs.setCurrentIndex(1)
    if _footer_texts(dlg, 1) != ["应用", "取消", "确定"]:
        raise RuntimeError(f"容器页的按钮不对：{_footer_texts(dlg, 1)}")
    if dlg.ok_btn.objectName() == "DialogPrimary":
        raise RuntimeError("「确定」不该用 DialogPrimary 主色实心（与取消 / 应用同色）")
    if not dlg.ok_btn.isDefault():
        raise RuntimeError("「确定」该是默认按钮（Enter 要能确认）")

    # 「本地数据库」页空了库文件名 → 提示要落在「数据库文件名」上
    dlg.db_file_name_edit.setText("")
    err, _ = dlg._validate()
    if "数据库文件名" not in err:
        raise RuntimeError(f"页签一空名没拦住：{err!r}")
    if "坐标系" in err or "图层名" in err:
        raise RuntimeError(f"该页不该问坐标系 / 图层名：{err!r}")
    return f"页签顺序 {titles} / 默认目标 into_db / 三形态 / 两套页脚 OK"


def case_dialog_target_combo(out: Path) -> str:
    """目标库下拉的三个动作项：浏览…、＋新增数据库…（跳页签）、建完自动进下拉。"""
    import app.ui.dialogs.create_data_dialog as mod

    entry = {"kind": "local", "name": "样例库", "path": str(out / "sample.gpkg")}
    dlg = mod.CreateDataDialog(None, default_crs=CRS, local_dbs=[entry])
    combo = dlg.db_target_combo
    labels = [combo.itemText(i) for i in range(combo.count())]
    if combo.count() != 3:
        raise RuntimeError(f"目标库项数 {combo.count()} != 3（1 个库 + 浏览 + 新增）：{labels}")
    if "浏览" not in labels[-2] or "新增数据库" not in labels[-1]:
        raise RuntimeError(f"两个动作项的位置不对（新增该在最后）：{labels}")

    # 「＋新增数据库…」→ 跳「本地数据库」页签，且下拉退回真实库项。
    # **注意：动作项由 activated（用户主动点）触发，不再靠 currentIndexChanged**
    combo.activated.emit(combo.count() - 1)
    if dlg.tabs.currentIndex() != 1 or dlg._kind() != "container":
        raise RuntimeError(f"点新增没跳到本地数据库页：tab={dlg.tabs.currentIndex()}")
    if not isinstance(combo.currentData(), dict):
        raise RuntimeError(f"跳走后下拉停在了 {combo.currentText()!r}，该退回真实库项")

    # 「浏览…」：**能反复点**（用户报的 bug：第二次点弹不出选择框）。
    # 报的根因是它挂在 currentIndexChanged 上 —— 下标没变就不发信号；而且当时一个库都没绑，
    # 取消后下拉还停在「浏览…」上，再点就更没反应了。
    original = mod.open_file_dir
    opened: list[str] = []
    mod.open_file_dir = lambda *a, **k: (opened.append(str(k.get("title"))), "")[1]
    try:
        browse_index = combo.count() - 2
        combo.setCurrentIndex(browse_index)  # 程序改下标：只换项，不该弹框
        if opened:
            raise RuntimeError(f"光是换到动作项就弹框了：{opened}")
        combo.activated.emit(browse_index)
        combo.activated.emit(browse_index)
        combo.activated.emit(browse_index)
        if len(opened) != 3:
            raise RuntimeError(f"「浏览…」该能反复点，实际弹了 {len(opened)} 次")
        if not isinstance(combo.currentData(), dict):
            raise RuntimeError(f"取消浏览后停在了 {combo.currentText()!r}")
    finally:
        mod.open_file_dir = original
    if dlg._target_db_path() != entry["path"]:
        raise RuntimeError(f"退回后的目标库 = {dlg._target_db_path()}")

    # **一个库都没绑**的情形（用户报的场景）：下拉里只有两个动作项，
    # 「浏览…」照样要能反复点开
    bare = mod.CreateDataDialog(None, default_crs=CRS)
    bare_combo = bare.db_target_combo
    if [bare_combo.itemText(i) for i in range(bare_combo.count())] != ["浏览…", "＋ 新增数据库…"]:
        raise RuntimeError(f"没绑库时下拉该只有两个动作项：{bare_combo.itemText(0)}")
    opened.clear()
    mod.open_file_dir = lambda *a, **k: (opened.append("x"), "")[1]
    try:
        for _ in range(2):
            bare_combo.activated.emit(0)
        if len(opened) != 2:
            raise RuntimeError(f"没绑库时「浏览…」点了两下只弹了 {len(opened)} 次")
    finally:
        mod.open_file_dir = original

    # 建完库 → 自动补进目标库下拉并选中（"建库 → 写图层"一口气做完）
    dlg.tabs.setCurrentIndex(1)
    dlg.db_path_edit.setText(str(out))  # 别建到默认目录（用户主目录）去
    db = out / "target_dropdown.gpkg"
    db.unlink(missing_ok=True)
    dlg.db_file_name_edit.setText(db.name)
    dlg.apply_now()  # 应用：建库但不关窗
    if dlg.outcome() is not None:
        raise RuntimeError("「应用」把 outcome 也留下来了，调用方会收尾两遍")
    if combo.count() != 4:
        raise RuntimeError(f"建完库没补进目标库下拉：{[combo.itemText(i) for i in range(combo.count())]}")
    picked = combo.currentData()
    if not isinstance(picked, dict) or picked.get("path") != str(db):
        raise RuntimeError(f"建完库没有自动选中它：{picked}")
    return "浏览…/＋新增数据库…跳页签/建完库自动进下拉并选中 OK"


def case_dialog_apply(out: Path) -> str:
    """「应用」：建了但窗口不关，结果经信号交出去；同一份表单不会被建两遍。"""
    from app.ui.dialogs.create_data_dialog import CreateDataDialog

    db = out / "apply_lib.gpkg"
    db.unlink(missing_ok=True)
    create_container(db, "gpkg")
    entry = {"kind": "local", "name": db.stem, "path": str(db)}

    dlg = CreateDataDialog(None, default_crs=CRS, local_dbs=[entry])
    seen: list = []
    dlg.applied.connect(seen.append)

    got: list = []
    dlg.accept = lambda: got.append("closed")  # 只关心"有没有关窗"，不真关

    # 第一次应用：建出「一号」
    dlg.into_layer_name_edit.setText("一号")
    dlg.apply_now()
    if len(seen) != 1 or seen[0].kind != "into_db":
        raise RuntimeError(f"应用没发出信号 / 内容不对：{seen}")
    if "一号" not in spatial_layers(db):
        raise RuntimeError("应用没有真的建出图层")
    if got:
        raise RuntimeError("「应用」不该关窗")

    # 表单没改再点应用 → 不重复建（会撞同名）
    dlg.apply_now()
    if len(seen) != 1:
        raise RuntimeError("表单没改却重复创建了一次")
    if "已经创建过" not in dlg.hint.text():
        raise RuntimeError(f"重复点应用没有提示：{dlg.hint.text()!r}")

    # 改了再应用 → 建出「二号」
    dlg.into_layer_name_edit.setText("二号")
    dlg.apply_now()
    if len(seen) != 2:
        raise RuntimeError(f"改名后应用没有被创建：{seen}")
    registered = spatial_layers(db)
    if not {"一号", "二号"} <= set(registered):
        raise RuntimeError(f"库里应有 一号 + 二号：{registered}")

    # 应用完再点「确定」：同一份内容不该再建一遍（否则撞「已存在」）
    dlg.into_layer_name_edit.setText("三号")
    dlg.apply_now()  # 建出三号，_applied_spec = 三号
    before = len(seen)
    dlg.accept()  # 真实 accept：表单 == 上次应用 → 只关窗
    if not got:
        raise RuntimeError("应用之后点确定应当关窗（且不再创建）")
    if len(seen) != before:
        raise RuntimeError("应用之后点确定又创建了一遍")
    if sorted(spatial_layers(db)) != ["一号", "三号", "二号"] and len(spatial_layers(db)) != 3:
        raise RuntimeError(f"库里图层数不对：{spatial_layers(db)}")
    return "应用不关窗 / 同表单不重复建 / 改了就新建 / 随后确定只关窗 OK"


def case_dialog_accept_container(out: Path) -> str:
    """「本地数据库」页「确定」→ 空库落盘，outcome 是 container。"""
    db = out / "dlg_empty.gpkg"
    db.unlink(missing_ok=True)

    dlg = make_dialog(out)
    dlg.tabs.setCurrentIndex(1)
    dlg.db_file_name_edit.setText(db.name)
    dlg.accept()

    outcome = dlg.outcome()
    if outcome is None or outcome.kind != "container":
        raise RuntimeError(f"outcome = {outcome}")
    if outcome.container_path != str(db) or not db.is_file():
        raise RuntimeError(f"库文件没建出来：{outcome.container_path}")
    if dlg.created_layer() is not None:
        raise RuntimeError("建空库不该有图层")
    return f"{db.name} 对话框直建空库 OK"


def case_dialog_accept_layer(out: Path) -> str:
    """「单图层数据」页两种目标各「确定」一次：独立文件 + 写进已有库。"""
    from app.ui.dialogs.create_data_dialog import CreateDataDialog

    # 模式 a：新建独立文件（用一个单图层格式）
    (out / "dlg_layer.geojson").unlink(missing_ok=True)
    dlg = make_dialog(out)
    dlg.target_combo.setCurrentIndex(0)
    dlg.file_name_edit.setText("dlg_layer.geojson")
    dlg.layer_name_edit.setText("dlg_layer")
    dlg.accept()
    outcome = dlg.outcome()
    if outcome is None or outcome.kind != "file" or outcome.layer is None:
        raise RuntimeError(f"模式 a outcome = {outcome}")
    if not outcome.layer.isValid():
        raise RuntimeError("模式 a 建出来的图层无效")

    # 模式 b：写进已有库（默认目标就是它，不用再切）
    db = out / "dlg_target.gpkg"
    db.unlink(missing_ok=True)
    create_container(db, "gpkg")
    entry = {"kind": "local", "name": db.stem, "path": str(db)}
    dlg = CreateDataDialog(None, default_crs=CRS, local_dbs=[entry])
    dlg.into_layer_name_edit.setText("里面的图层")
    if dlg._target_db_path() != str(db):
        raise RuntimeError(f"目标库没对上：{dlg._target_db_path()}")
    dlg.accept()
    outcome = dlg.outcome()
    if outcome is None or outcome.kind != "into_db" or outcome.layer is None:
        raise RuntimeError(f"模式 b outcome = {outcome}")
    if outcome.db_path != str(db):
        raise RuntimeError(f"模式 b db_path = {outcome.db_path}")
    if "里面的图层" not in spatial_layers(db):
        raise RuntimeError(f"新图层没登记进库：{spatial_layers(db)}")
    if feature_count(db, "里面的图层") != 0:
        raise RuntimeError("新图层该是空的")
    return "模式 a 建独立文件 / 模式 b 写进已有库 均 OK"


def case_registration_wiring(out: Path) -> str:
    """创建后的登记编排：库进数据管理、库内新图层能被重扫出来、登记失败只提示。

    直接调 ``AppActionsMixin`` 的编排方法（它只依赖 ``messageBar`` 与
    ``_data_manage_dock``），不搭整个 MainWindow。
    """
    from app.ui.docks.data_manage_dock import DataManageDock
    from app.ui.main_window.mixins.app_actions import AppActionsMixin

    class _Bar:
        """记录消息条调用（真实 messageBar 要 QGIS 全局窗口才能建）。"""

        def __init__(self) -> None:
            self.messages: list[str] = []

        def pushMessage(self, text, level=None, duration=None) -> None:  # noqa: N802
            self.messages.append(text)

    class _Host(AppActionsMixin):
        def __init__(self) -> None:
            self.messageBar = _Bar()
            self._data_manage_dock = None

    host = _Host()
    dock = DataManageDock(None)

    # 1. 建空库 → 登记进数据管理
    db = out / "wired.gpkg"
    db.unlink(missing_ok=True)
    create_container(db, "gpkg")
    if host._local_db_entries(dock):
        raise RuntimeError("还没登记，注入清单不该有东西")
    entry = host._register_local_db(dock, str(db))
    if entry is None:
        raise RuntimeError(f"登记失败，消息条：{host.messageBar.messages}")
    if host._local_db_entries(dock) != [entry]:
        raise RuntimeError("登记后注入清单对不上")

    # 2. 往库里建图层 → 重扫后应当出现在 Dock 缓存的 categories 里。
    #    这里特意用「按路径取活对象」的路径（传副本会静默 no-op，见 CreateOutcome 说明）
    add_empty_layer(db, "second")
    live = host._dock_db_entry(dock, str(db))
    if live is None:
        raise RuntimeError("按路径没找到 Dock 里的连接")
    if live is not entry:
        raise RuntimeError("按路径取到的不是 Dock 里那个活对象")
    host._sync_created_db_layer(str(db), "second", dock)
    cached = [n for key in ("spatial", "dict", "image") for n in live["categories"].get(key, [])]
    if "second" not in cached:
        raise RuntimeError(f"重扫后 Dock 缓存里还是没有 second：{cached}")

    # 3. 登记不了的库（不存在的文件）只提示，不抛
    bad = host._register_local_db(dock, str(out / "nope.gpkg"))
    if bad is not None:
        raise RuntimeError("不存在的库不该登记成功")
    if not any("没能登记" in m for m in host.messageBar.messages):
        raise RuntimeError(f"登记失败没给提示：{host.messageBar.messages}")

    # 4. 「应用」那条路走完整链：对话框发信号 → 主窗口收尾（入工程 + 库进树 + 树里见到新图层）
    from qgis.core import QgsProject

    from app.ui.dialogs.create_data_dialog import CreateDataDialog

    host.project = QgsProject.instance()
    host._data_manage_dock = dock  # 生产里由 _on_data_manage 建 Dock 时写入
    dlg = CreateDataDialog(None, default_crs=CRS, local_dbs=[host._local_db_entries(dock)[0]])
    dlg.applied.connect(host._handle_create_outcome)
    dlg.into_layer_name_edit.setText("走信号建的")
    dlg.apply_now()
    added = [lyr for lyr in host.project.mapLayers().values() if lyr.name() == "走信号建的"]
    if not added:
        raise RuntimeError("应用后图层没进工程")
    leaves = []

    def _walk(item):
        for i in range(item.childCount()):
            child = item.child(i)
            leaves.append(child.text(0))
            _walk(child)

    for i in range(dock.tree.topLevelItemCount()):
        _walk(dock.tree.topLevelItem(i))
    if "走信号建的" not in leaves:
        raise RuntimeError(f"应用后数据管理树里没有新图层：{leaves}")
    host.project.removeMapLayer(added[0].id())  # 别把句柄留在共享的 QgsProject 里
    return "登记进树 / 库内新图层重扫可见 / 登记失败只提示 / 应用→入工程+进树 OK"


def case_format_drivers(out: Path) -> str:
    """下拉里每一项的驱动都真能写；容器库不像素化混进「单图层数据」。

    两条规则一起验：

    1. 下拉里留下的格式，驱动**一定**是本机可写的（装不了的隐藏，不灰着占位）；
    2. **FileGDB 两个下拉里都没有** —— 它进不了数据管理（``.gdb`` 是目录、OGR 引擎），
       放进「单图层数据」页又与其"一个文件一层"的语义不符（用户定的：隐藏）。
       但它本身仍然能建（走 ``OpenFileGDB``），只是创建入口收敛到文件浏览器的右键「新建」。
    """
    import shutil

    from app.core.domain.create_data import CreateDataService, CreateDataSpec, available_drivers

    writers = available_drivers()
    dlg = make_dialog(out)
    # 页签二（单图层数据）只该有非容器；页签一（本地数据库）只该有容器
    for combo, want_container, where in (
        (dlg.format_combo, False, "页签二"),
        (dlg.db_format_combo, True, "页签一"),
    ):
        for i in range(combo.count()):
            fmt = combo.itemData(i)
            if fmt.driver not in writers:
                raise RuntimeError(f"{where}留了个建不了的格式：{fmt.label}（驱动 {fmt.driver}）")
            if bool(fmt.container) is not want_container:
                raise RuntimeError(f"{where}混进了不该有的格式：{fmt.label}（container={fmt.container}）")

    for combo, where in ((dlg.format_combo, "页签二"), (dlg.db_format_combo, "页签一")):
        labels = [combo.itemText(i) for i in range(combo.count())]
        if any("Geodatabase" in t for t in labels):
            raise RuntimeError(f"{where}的下拉里还有 File Geodatabase：{labels}")

    # 手打扩展名要能反向切到对应格式（FileGDB 的驱动名被换过，容易在这里断）
    dlg.tabs.setCurrentIndex(1)
    dlg.file_name_edit.setText("回切.shp")
    got_fmt = dlg.format_combo.currentData()
    if got_fmt.extension != ".shp":
        raise RuntimeError(f"手打 .shp 没切到 Shapefile，当前是 {got_fmt.label}")

    # 隐藏 ≠ 砍掉：FileGDB 用解析出的可写驱动仍建得出来（文件浏览器走的就是这条）
    from app.core.util.vector_formats import VECTOR_FORMATS, resolve_format

    fgbd = next(f for f in VECTOR_FORMATS if f.extension == ".gdb")
    resolved = resolve_format(fgbd, writers)
    if resolved is None:
        return "下拉全可写、FileGDB 已隐藏；本机没有 FileGDB 驱动，略过建库验证"

    gdb = out / "probe.gdb"
    if gdb.exists():
        shutil.rmtree(gdb)
    layer = CreateDataService().create(
        CreateDataSpec(
            path=str(out),
            file_name="probe.gdb",
            data_format=fgbd.label,
            driver=resolved.driver,
            extension=".gdb",
            layer_name="probe",
            geometry_type="Point",
            crs_authid=CRS,
        )
    )
    if not gdb.is_dir() or not layer.isValid():
        raise RuntimeError(f"FileGDB 用 {resolved.driver} 建不出来")
    return f"下拉全可写、FileGDB 已隐藏；它本身仍能用 {resolved.driver} 建出 .gdb OK"


def case_default_output_dir(out: Path) -> str:
    """创建路径的默认值必须是个真目录（不能是空串），且跨平台拼路径可用。"""
    from app.ui.dialogs.create_data_dialog import CreateDataDialog, default_output_dir

    d = default_output_dir()
    if not d or not os.path.isdir(d):
        raise RuntimeError(f"默认路径不可用：{d!r}")

    dlg = CreateDataDialog(None, default_crs=CRS)
    for edit, label in ((dlg.db_path_edit, "页签一"), (dlg.path_edit, "页签二")):
        if not edit.text().strip():
            raise RuntimeError(f"{label}的创建路径默认值是空的")
        if not os.path.isdir(edit.text().strip()):
            raise RuntimeError(f"{label}默认路径不是目录：{edit.text()!r}")

    # 默认路径下只填名字就该能过校验（以前工程没存盘时这里会卡在「请填写创建路径」）
    dlg.tabs.setCurrentIndex(1)  # 「本地数据库」页
    dlg.db_file_name_edit.setText("默认路径.gpkg")
    err, _w = dlg._validate()
    if err:
        raise RuntimeError(f"默认路径下校验不通过：{err!r}")
    return f"默认路径 = {d}（按平台取用户主目录 / 工程目录）OK"


def case_dock_refresh_rebuilds_tree(out: Path) -> str:
    """往已登记的库里加图层后刷新：**左树也要出现新叶子**，右表跟着更新。

    用户报过「数据管理里加完图层看不到，只有手动刷新才有」——根因是刷新只重画了
    右表、没重建左树（左树的「矢量 (N) → 图层叶子」是重扫前的快照）。这条用例把
    「传副本也要能刷新」一起钉住（导入链路拿到的就是 Qt itemData 的副本）。
    """
    from app.core.util.local_db_util import describe_local_db
    from app.ui.docks.data_manage_dock import DataManageDock

    db = out / "tree_refresh.gpkg"
    db.unlink(missing_ok=True)
    create_container(db, "gpkg")
    dock = DataManageDock(None)
    dock.add_connection({"kind": "local", "name": db.stem, "path": str(db), **describe_local_db(db)})

    def leaves() -> list[str]:
        found: list[str] = []

        def walk(item) -> None:
            for i in range(item.childCount()):
                child = item.child(i)
                found.append(child.text(0))
                walk(child)

        for i in range(dock.tree.topLevelItemCount()):
            walk(dock.tree.topLevelItem(i))
        return found

    add_empty_layer(db, "新图层")
    if "新图层" in leaves():
        raise RuntimeError("还没刷新，树里就冒出来了（后面两条断言的前提是懒刷新）")

    # 刷新不该改变"用户看到的形状"：先展开另一个库和当前库的类别分组，看刷新后还在不在
    other = out / "tree_refresh_other.gpkg"
    other.unlink(missing_ok=True)
    create_container(other, "gpkg")
    add_empty_layer(other, "别的库的图层")
    dock.add_connection(
        {"kind": "local", "name": other.stem, "path": str(other), **describe_local_db(other)}
    )

    def node(stem: str):
        """按库名取左树节点（库顺序是「新的在上」，别按 child 下标找）。"""
        root = dock.tree.topLevelItem(0)
        for i in range(root.childCount()):
            if root.child(i).text(0) == stem:
                return root.child(i)
        raise RuntimeError(f"树上没有库 {stem}：{leaves()}")

    other_node = node(other.stem)
    other_node.setExpanded(True)
    other_group = other_node.child(0)  # 另一个库的「矢量」分组（它有已登记图层，树上才有）
    if other_group is None:
        raise RuntimeError("另一个库在树上没有类别分组")
    other_group.setExpanded(True)

    # 故意传**副本**：dock 内部要按路径换回 _databases 里的原件，否则缓存写进副本、树按旧缓存重建
    live = next(d for d in dock._databases if str(d.get("path")) == str(db))
    dock.refresh_database(dict(live))
    if "新图层" not in leaves():
        raise RuntimeError(f"刷新后左树里还是没有新图层：{leaves()}")
    table = [dock.table.item(r, 0).text() for r in range(dock.table.rowCount())]
    if "新图层" not in table:
        raise RuntimeError(f"刷新后右表里没有新图层：{table}")
    current = dock.tree.currentItem()
    if current is None or current.text(0) != db.stem:
        raise RuntimeError(f"刷新后选中跑到了 {current.text(0) if current else None!r}")
    if not node(other.stem).isExpanded():
        raise RuntimeError("刷新把另一个库的展开状态收起来了")
    if not node(other.stem).child(0).isExpanded():
        raise RuntimeError("刷新把类别分组的展开状态收起来了")

    # 手动「刷新」按钮走的那条也要重建树
    add_empty_layer(db, "又一个")
    dock.refresh_all()
    if "又一个" not in leaves():
        raise RuntimeError(f"refresh_all 后左树里没有新图层：{leaves()}")
    return "库内加图层后刷新：左树新叶子 + 右表更新 + 选中保持 OK"


CASES: list[tuple[str, Callable[[Path], str]]] = [
    ("container-gpkg", case_container_gpkg),
    ("container-spatialite", case_container_spatialite),
    ("layer-in-db", case_layer_in_db),
    ("layer-in-sqlite", case_layer_in_sqlite),
    ("duplicate-layer", case_duplicate_layer),
    ("import-cjk-columns", case_import_keeps_cjk_columns),
    ("import-gdb-dates", case_import_gdb_dates),
    ("layer-open-policy", case_layer_open_policy),
    ("local-db-order", case_local_db_order_and_import_default),
    ("non-seed-srid", case_non_seed_srid),
    ("format-drivers", case_format_drivers),
    ("default-output-dir", case_default_output_dir),
    ("dialog-structure", case_dialog_structure),
    ("wizard-steps", case_wizard_steps),
    ("field-model", case_field_model),
    ("file-with-fields", case_file_with_fields),
    ("into-db-with-fields", case_into_db_with_fields),
    ("field-template", case_field_template),
    ("fields-from-layer", case_fields_from_layer),
    ("field-persistence-notes", case_field_persistence_notes),
    ("fields-table-interactions", case_fields_table_interactions),
    ("crs-picker", case_crs_picker),
    ("external-dataset-files", case_external_dataset_files),
    ("data-manage-state", case_data_manage_state),
    ("export-append-services", case_export_append_services),
    ("export-dict", case_export_dict),
    ("import-gdb-domain", case_import_gdb_domain),
    ("create-gdb-domain", case_create_gdb_domain),
    ("import-dict-merge", case_import_dict_merge),
    ("fields-sort", case_fields_sort),
    ("data-manage-menus", case_data_manage_menus),
    ("data-management-fixes", case_data_management_fixes),
    ("auto-guid-import-data", case_auto_guid_and_import_data),
    ("import-multi-edit-prompts", case_import_multi_and_edit_prompts),
    ("arcmap-shp-and-editable-gpkg", case_arcmap_shp_and_editable_gpkg),
    ("sublayer-picker", case_sublayer_picker_and_no_io_on_select),
    ("field-name-matching", case_field_name_matching),
    ("numeric-cell-guard", case_numeric_cell_guard),
    ("template-write-failure", case_template_write_failure),
    ("dialog-target-combo", case_dialog_target_combo),
    ("dialog-apply", case_dialog_apply),
    ("dialog-accept-container", case_dialog_accept_container),
    ("dialog-accept-layer", case_dialog_accept_layer),
    ("dock-refresh-tree", case_dock_refresh_rebuilds_tree),
    ("registration-wiring", case_registration_wiring),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="创建数据（本地库容器）自检")
    parser.add_argument(
        "--out",
        type=Path,
        default=_ROOT / ".tmp" / "create_data",
        help="产物输出目录（默认 .tmp/create_data）",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        choices=[name for name, _fn in CASES],
        help="只跑指定用例（默认全跑）",
    )
    parser.add_argument("--clean", action="store_true", help="跑完删除产物")
    args = parser.parse_args()

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    print(f"[env] 输出目录 = {out}")

    init_qgis()
    selected = [(n, fn) for n, fn in CASES if not args.only or n in args.only]

    results: list[tuple[str, bool, str]] = []
    for name, fn in selected:
        try:
            results.append((name, True, fn(out)))
        except Exception as exc:  # noqa: BLE001  自检脚本：任何失败都要归档成结果
            traceback.print_exc()
            results.append((name, False, f"{type(exc).__name__}: {exc}"))

    print("\n" + "=" * 60)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<20} {detail}")
    failed = [n for n, ok, _d in results if not ok]
    print("=" * 60)
    print(
        f"合计 {len(results)} 项，通过 {len(results) - len(failed)} 项"
        + (f"，失败：{', '.join(failed)}" if failed else "")
    )

    if args.clean:
        cleanup(out)
    else:
        print(f"产物保留在 {out}")

    # QGIS 卸载阶段在本机会原生崩溃（见项目笔记），用 _exit 跳过 teardown；
    # _exit 不 flush 缓冲，管道重定向时必须先手动 flush，否则输出全丢。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    raise SystemExit(main())
