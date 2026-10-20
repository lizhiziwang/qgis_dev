"""「数据管理」本地表编辑自检（无界面）。

验证 :mod:`app.core.domain.local_table_edit`：

1. ``gpkg-rebuild``   —— GPKG 点表把整型列改成文本/实数：数据、fid、几何、
                        要素数、GPKG 元数据全部保留
2. ``gpkg-metadata``  —— 改名 / 删列同步 gpkg_data_columns 与字段元数据
3. ``notnull-guard``  —— 有 NULL 值时不许改成不可为空
4. ``row-crud``       —— 字典表分页读行 / 改行 / 加行 / 删行
5. ``spatialite-attr``—— SpatiaLite 属性表类型重建（空间表重建另行手动验收）
"""

from __future__ import annotations

import gc
import os
import shutil
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

import _bootstrap_path  # noqa: F401

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parents[1]

from run_sqlite_test import init_qgis  # noqa: E402

CASES: list[tuple[str, Callable[[Path], str]]] = []


def case(fn):
    CASES.append((fn.__name__.removeprefix("case_").replace("_", "-"), fn))
    return fn


def expect(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _new_container(work: Path, kind: str) -> str:
    from app.core.domain.create_data import CreateContainerService, CreateContainerSpec

    work.mkdir(parents=True, exist_ok=True)
    if kind == "gpkg":
        spec = CreateContainerSpec(
            path=str(work),
            file_name="test.gpkg",
            data_format="GeoPackage",
            driver="GPKG",
            extension=".gpkg",
        )
    else:
        spec = CreateContainerSpec(
            path=str(work),
            file_name="test.sqlite",
            data_format="SpatiaLite",
            driver="SQLite",
            extension=".sqlite",
        )
    return CreateContainerService().create(spec)


def _make_point_layer(
    db_path: str, table: str = "地块", fields: tuple | None = None
):
    from app.core.domain.create_data import (
        CreateLayerInDbService,
        CreateLayerInDbSpec,
        FieldSpec,
    )

    spec = CreateLayerInDbSpec(
        db_path=db_path,
        layer_name=table,
        geometry_type="Point",
        crs_authid="EPSG:4490",
        fields=fields
        or (
            FieldSpec(name="code", type_key="text", length=20),
            FieldSpec(name="amount", type_key="integer"),
        ),
    )
    return CreateLayerInDbService().create(spec)


def _add_points(layer, values: list[list]) -> None:
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY

    names = [field.name() for field in layer.fields()]
    feats = []
    for index, row in enumerate(values):
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(index, index)))
        by_name = {"code": row[0], "amount": row[1]}
        feature.setAttributes([by_name.get(name) for name in names])
        feats.append(feature)
    ok, _ = layer.dataProvider().addFeatures(feats)
    if not ok:
        raise RuntimeError("加要素失败")
    layer.updateExtents()


@case
def case_gpkg_rebuild(out: Path) -> str:
    from app.core.domain.local_table_edit import (
        read_table_info,
        rebuild_for_types,
    )
    from app.core.util.local_db_util import local_db_util

    work = out / "gpkg_rebuild"
    db_path = _new_container(work, "gpkg")
    layer = _make_point_layer(db_path)
    _add_points(layer, [["A", 1], ["B", 2]])
    del layer
    gc.collect()

    info = read_table_info(db_path, "地块")
    names = [col.name for col in info.editable_columns]
    expect("geom" not in names and "fid" not in names, f"编辑列里不该有几何/fid：{names}")
    expect({"code", "amount"} <= set(names), f"业务字段缺失：{names}")

    rebuild_for_types(db_path, "地块", {"amount": ("REAL", False)})
    db = local_db_util(db_path)
    try:
        types = {
            row[1]: row[2]
            for row in db.cursor.execute('PRAGMA table_info("地块")')
        }
        expect("REAL" in types["amount"].upper(), f"amount 类型没改：{types['amount']}")
        rows = db.cursor.execute(
            'SELECT fid, code, amount, geom IS NOT NULL FROM "地块" ORDER BY fid'
        ).fetchall()
        expect(len(rows) == 2, f"要素数不对：{len(rows)}")
        actual = [tuple(r)[:3] for r in rows]
        expect(actual == [(1, "A", 1.0), (2, "B", 2.0)], f"数据没保住：{actual}")
        expect(all(int(r[3]) == 1 for r in rows), "几何丢了")
        contents = db.cursor.execute(
            "SELECT COUNT(*) FROM gpkg_contents WHERE table_name = '地块'"
        ).fetchone()[0]
        geom_meta = db.cursor.execute(
            "SELECT COUNT(*) FROM gpkg_geometry_columns WHERE table_name = '地块'"
        ).fetchone()[0]
        expect(int(contents) == 1 and int(geom_meta) == 1, "GPKG 元数据丢了")
    finally:
        db.conn.close()
    return "GPKG 类型重建（数据/fid/几何/元数据）通过"


@case
def case_gpkg_metadata(out: Path) -> str:
    from app.core.domain.local_table_edit import (
        drop_column,
        read_table_info,
        rename_column,
    )
    from app.core.util.field_meta import FieldMeta, read_field_meta, upsert_field_meta
    from app.core.util.local_db_util import local_db_util

    work = out / "gpkg_meta"
    db_path = _new_container(work, "gpkg")
    layer = _make_point_layer(db_path)
    _add_points(layer, [["A", 1]])
    del layer
    gc.collect()
    db = local_db_util(db_path)
    try:
        db.set_gpkg_column_alias("地块", "code", "代码别名")
        upsert_field_meta(
            db.conn,
            "地块",
            "code",
            FieldMeta(dict_table="测试字典", dict_domain="域A", visible=False),
            commit=True,
        )
    finally:
        db.conn.close()

    rename_column(db_path, "地块", "code", "code2")
    db = local_db_util(db_path)
    try:
        alias = db.cursor.execute(
            "SELECT column_name, name FROM gpkg_data_columns WHERE table_name='地块'"
        ).fetchone()
        expect(tuple(alias) == ("code2", "代码别名"), f"别名没跟着改名：{alias}")
        metas = read_field_meta(db.conn, "地块")
        expect("code2" in metas and "code" not in metas, f"字段元数据没改名：{metas}")
    finally:
        db.conn.close()

    drop_column(db_path, "地块", "code2")
    db = local_db_util(db_path)
    try:
        alias_count = db.cursor.execute(
            "SELECT COUNT(*) FROM gpkg_data_columns WHERE table_name='地块'"
        ).fetchone()[0]
        metas = read_field_meta(db.conn, "地块")
    finally:
        db.conn.close()
    expect(int(alias_count) == 0, "删列后别名没清")
    expect("code2" not in metas, "删列后字段元数据没清")
    expect(all(c.name != "code2" for c in read_table_info(db_path, "地块").columns),
           "字段没有真的删掉")
    return "改名/删列同步别名与字段元数据通过"


@case
def case_notnull_guard(out: Path) -> str:
    from app.core.domain.local_table_edit import LocalTableError, rebuild_for_types

    work = out / "notnull"
    db_path = _new_container(work, "gpkg")
    layer = _make_point_layer(db_path)
    _add_points(layer, [["A", None]])
    del layer
    gc.collect()
    try:
        rebuild_for_types(db_path, "地块", {"amount": ("INTEGER", True)})
    except LocalTableError:
        pass
    else:
        raise AssertionError("有 NULL 值时不应允许改成不可为空")
    return "NOT NULL 预检拦截通过"


@case
def case_row_crud(out: Path) -> str:
    from app.core.domain.local_table_edit import (
        delete_rows,
        insert_row,
        read_rows,
        update_row,
    )
    from app.core.util.local_db_util import local_db_util

    work = out / "rows"
    db_path = _new_container(work, "gpkg")
    db = local_db_util(db_path)
    try:
        db.create_vector_table(
            "dict_rows",
            columns=[
                ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
                ("domain", "TEXT NOT NULL"),
                ("code", "TEXT NOT NULL"),
                ("name", "TEXT"),
            ],
            geometry_column=None,
            register=False,
        )
        db.add_domain_table("dict_rows", "dict")
        db.cursor.executemany(
            'INSERT INTO "dict_rows" (domain, code, name) VALUES (?, ?, ?)',
            [("域A", "A1", "甲"), ("域A", "A2", "乙")],
        )
        db.conn.commit()
    finally:
        db.conn.close()

    page = read_rows(db_path, "dict_rows", limit=1)
    expect(page.total == 2 and len(page.rows) == 1, "分页读行不对")
    expect(page.key.columns == ("id",), f"行键不对：{page.key}")
    row = page.rows[0]
    update_row(db_path, "dict_rows", row, {"name": "甲改"})
    page = read_rows(db_path, "dict_rows")
    expect(any(r["name"] == "甲改" for r in page.rows), "改行没落库")
    insert_row(db_path, "dict_rows", {"domain": "域A", "code": "A3", "name": "丙"})
    page = read_rows(db_path, "dict_rows")
    expect(page.total == 3, f"加行后应为 3，实际 {page.total}")
    target = next(r for r in page.rows if r["code"] == "A3")
    deleted = delete_rows(db_path, "dict_rows", [target])
    expect(deleted == 1, f"删除条数不对：{deleted}")
    expect(read_rows(db_path, "dict_rows").total == 2, "删行后行数不对")
    return "字典行分页 / 改 / 加 / 删 通过"


@case
def case_spatialite_attr(out: Path) -> str:
    from app.core.domain.local_table_edit import read_table_info, rebuild_for_types
    from app.core.util.local_db_util import local_db_util

    work = out / "spatialite"
    try:
        db_path = _new_container(work, "spatialite")
    except Exception as exc:  # noqa: BLE001
        return f"跳过：建不出 SpatiaLite 库（{exc}）"
    db = local_db_util(db_path)
    try:
        if db.kind != "spatialite":
            return "跳过：本机未启用 SpatiaLite"
        db.create_vector_table(
            "attr_table",
            columns=[("code", "TEXT"), ("num", "INTEGER")],
            geometry_column=None,
            register=False,
        )
        db.cursor.execute(
            'INSERT INTO "attr_table" (code, num) VALUES (?, ?)', ("A", 5)
        )
        db.conn.commit()
    finally:
        db.conn.close()
    rebuild_for_types(db_path, "attr_table", {"num": ("REAL", False)})
    info = read_table_info(db_path, "attr_table")
    amount = next(col for col in info.columns if col.name == "num")
    expect("REAL" in amount.declared_type.upper(), "SpatiaLite 类型没改")
    db = local_db_util(db_path)
    try:
        value = db.cursor.execute('SELECT num FROM "attr_table"').fetchone()[0]
    finally:
        db.conn.close()
    expect(float(value) == 5.0, f"SpataLite 数据没保住：{value}")
    return "SpatiaLite 属性表类型重建通过"


@case
def case_spatialite_spatial(out: Path) -> str:
    """SpatiaLite 空间表：类型重建后几何 / SRID / 要素数 / 空间元数据保留。"""
    from app.core.domain.local_table_edit import rebuild_for_types
    from app.core.util.local_db_util import local_db_util
    from app.core.util.local_layer_loader import open_local_vector_table

    work = out / "spatialite_spatial"
    try:
        db_path = _new_container(work, "spatialite")
    except Exception as exc:  # noqa: BLE001
        return f"跳过：建不出 SpatiaLite 库（{exc}）"
    layer = _make_point_layer(db_path)
    _add_points(layer, [["A", 1], ["B", 2]])
    del layer
    gc.collect()

    db = local_db_util(db_path)
    try:
        if db.kind != "spatialite":
            return "跳过：本机未启用 SpatiaLite"
        spatial = db.cursor.execute(
            "SELECT COUNT(*) FROM geometry_columns WHERE f_table_name = '地块'"
        ).fetchone()[0]
    finally:
        db.conn.close()
    expect(int(spatial) == 1, "建出来的 SpatiaLite 图层没有几何元数据")

    rebuild_for_types(db_path, "地块", {"amount": ("TEXT", False)})
    db = local_db_util(db_path)
    try:
        rows = db.cursor.execute(
            'SELECT code, amount, geom IS NOT NULL FROM "地块" ORDER BY rowid'
        ).fetchall()
        meta = db.cursor.execute(
            "SELECT f_geometry_column, srid FROM geometry_columns "
            "WHERE f_table_name = '地块'"
        ).fetchone()
    finally:
        db.conn.close()
    expect(len(rows) == 2, f"要素数不对：{len(rows)}")
    expect([tuple(r)[:2] for r in rows] == [("A", "1"), ("B", "2")],
           f"数据没保住：{rows}")
    expect(all(int(r[2]) == 1 for r in rows), "几何丢了")
    expect(tuple(meta)[1] == 4490, f"SRID 丢了：{meta}")
    reopened, error = open_local_vector_table(db_path, "地块")
    expect(reopened is not None, f"重建后打不开：{error}")
    expect(reopened.featureCount() == 2, "重建后要素数不对")
    return "SpatiaLite 空间表类型重建（几何/SRID/要素）通过"


@case
def case_dock_detail(out: Path) -> str:
    """数据管理 Dock：点矢量 / 字典叶子整栏切到详情页，点分组/连接切回列表。"""
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import QApplication, QDialog, QMessageBox

    from app.core.util.local_db_util import describe_local_db, local_db_util
    from app.ui.docks.data_manage_dock import DataManageDock

    work = out / "dock"
    db_path = _new_container(work, "gpkg")
    layer = _make_point_layer(db_path)
    _add_points(layer, [["A", 1]])
    del layer
    gc.collect()
    db = local_db_util(db_path)
    try:
        db.create_vector_table(
            "dict_rows",
            columns=[
                ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
                ("domain", "TEXT NOT NULL"),
                ("code", "TEXT NOT NULL"),
                ("name", "TEXT"),
            ],
            geometry_column=None,
            register=False,
        )
        db.add_domain_table("dict_rows", "dict")
        db.cursor.execute(
            'INSERT INTO "dict_rows" (domain, code, name) VALUES (?, ?, ?)',
            ("域A", "A1", "甲"),
        )
        db.conn.commit()
    finally:
        db.conn.close()

    catalog = describe_local_db(db_path)
    entry = {
        "kind": "local",
        "name": Path(db_path).stem,
        "path": db_path,
        **catalog,
    }
    dock = DataManageDock(None)
    try:
        dock.set_databases([entry])
        QApplication.processEvents()

        def find_node(category, name=None):
            stack = [
                dock.tree.topLevelItem(i)
                for i in range(dock.tree.topLevelItemCount())
            ]
            while stack:
                item = stack.pop()
                payload = item.data(0, Qt.ItemDataRole.UserRole)
                if (
                    isinstance(payload, dict)
                    and payload.get("cat") == category
                    and (name is None or payload.get("name") == name)
                    and (name is not None or "name" not in payload)
                ):
                    return item
                stack.extend(item.child(i) for i in range(item.childCount()))
            return None

        leaf = find_node("spatial", "地块")
        expect(leaf is not None, "树里找不到矢量叶子")
        dock._on_tree_item_clicked(leaf, 0)
        QApplication.processEvents()
        expect(dock.stack.currentWidget() is dock.detail_page, "矢量叶子没有切到详情页")
        expect(len(dock.detail_page.vector._rows) >= 2, "字段列表没有加载")

        group = find_node("spatial")
        expect(group is not None, "树里找不到矢量分组")
        dock._on_tree_item_clicked(group, 0)
        expect(dock.stack.currentIndex() == 0, "点分组没有回到数据表列表")

        dict_leaf = find_node("dict", "dict_rows")
        expect(dict_leaf is not None, "树里找不到字典叶子")
        dock._on_tree_item_clicked(dict_leaf, 0)
        QApplication.processEvents()
        expect(dock.stack.currentWidget() is dock.detail_page, "字典叶子没有切到详情页")
        dict_editor = dock.detail_page.dictionary
        expect(dict_editor._canonical, "canonical 字典表应使用字典管理风格树")
        expect(dict_editor.tree.columnCount() == 3, "字典树应为名称/代码/字典域三列")
        expect(dict_editor.tree.topLevelItemCount() == 1, "字典域节点数量不对")
        domain_node = dict_editor.tree.topLevelItem(0)
        dict_editor.tree.expandItem(domain_node)
        QApplication.processEvents()
        item_nodes = [
            domain_node.child(i)
            for i in range(domain_node.childCount())
            if domain_node.child(i).data(0, dict_editor._KIND) == dict_editor._K_ITEM
        ]
        expect(len(item_nodes) == 1, f"字典项没有加载：{len(item_nodes)}")

        # 叶子右键「编辑名称」：改名后详情页 / 树都要跟上
        from qgis.PyQt.QtWidgets import QInputDialog

        original_get_text = QInputDialog.getText
        QInputDialog.getText = staticmethod(lambda *a, **k: ("地块改", True))
        try:
            dock._rename_leaf(entry, "spatial", "地块")
        finally:
            QInputDialog.getText = original_get_text
        QApplication.processEvents()
        expect(
            dock.detail_page.vector._table == "地块改",
            f"改名后详情页没跟上：{dock.detail_page.vector._table}",
        )
        expect(find_node("spatial", "地块改") is not None, "改名后树里找不到新叶子")

        # 空库也要显示「字典」分组，并可从分组添加字典表
        import app.ui.dialogs.add_local_dict as add_mod

        empty_dir = work / "empty"
        shutil.rmtree(empty_dir, ignore_errors=True)
        _new_container(empty_dir, "gpkg")
        empty_path = str(empty_dir / "test.gpkg")
        empty_entry = {
            "kind": "local",
            "name": "empty",
            "path": empty_path,
            **describe_local_db(empty_path),
        }
        dock.set_databases([entry, empty_entry])
        QApplication.processEvents()
        empty_conn = None
        for root_index in range(dock.tree.topLevelItemCount()):
            root = dock.tree.topLevelItem(root_index)
            for child_index in range(root.childCount()):
                node = root.child(child_index)
                payload = node.data(0, Qt.ItemDataRole.UserRole)
                if (
                    isinstance(payload, dict)
                    and (payload.get("db") or {}).get("path") == empty_path
                ):
                    empty_conn = node
        expect(empty_conn is not None, "空库连接节点不存在")
        dict_group = None
        for child_index in range(empty_conn.childCount()):
            node = empty_conn.child(child_index)
            payload = node.data(0, Qt.ItemDataRole.UserRole)
            if (
                isinstance(payload, dict)
                and payload.get("cat") == "dict"
                and "name" not in payload
            ):
                dict_group = node
        expect(dict_group is not None, "空库没有显示字典分组")
        dock._on_tree_item_clicked(dict_group, 0)
        expect(not dock.add_dict_btn.isHidden(), "字典分组没有显示添加按钮")

        class FakeAddDict:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

            def table_name(self) -> str:
                return "本地新字典"

            def source_category(self) -> str:
                return ""

            def deleteLater(self) -> None:  # noqa: N802 —— 模拟 Qt 方法
                pass

        original_information = QMessageBox.information
        original_add = add_mod.AddLocalDictDialog
        QMessageBox.information = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Ok
        )
        add_mod.AddLocalDictDialog = FakeAddDict
        try:
            dock._add_dict_table(empty_entry)
        finally:
            add_mod.AddLocalDictDialog = original_add
            QMessageBox.information = original_information
        QApplication.processEvents()
        db = local_db_util(empty_path)
        try:
            registered = db.query_all_table_name()["dict"]
        finally:
            db.conn.close()
        expect("本地新字典" in registered, f"添加字典表没有登记：{registered}")
    finally:
        dock.close()
        dock.deleteLater()
        QApplication.processEvents()
    return "Dock 叶子详情 / 分组返回 / 字典行加载 通过"


@case
def case_detail_editors(out: Path) -> str:
    """详情编辑器：矢量字段改名/改类型/加字段保存；字典行改/删。"""
    from qgis.PyQt.QtWidgets import QApplication, QMessageBox

    from app.core.util.local_db_util import local_db_util
    from app.ui.docks.table_detail import DictTableEditor, VectorFieldEditor

    work = out / "editors"
    db_path = _new_container(work, "gpkg")
    layer = _make_point_layer(db_path)
    _add_points(layer, [["A", 1], ["B", 2]])
    del layer
    gc.collect()
    db = local_db_util(db_path)
    try:
        db.create_vector_table(
            "dict_rows",
            columns=[
                ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
                ("domain", "TEXT NOT NULL"),
                ("code", "TEXT NOT NULL"),
                ("name", "TEXT"),
            ],
            geometry_column=None,
            register=False,
        )
        db.add_domain_table("dict_rows", "dict")
        db.cursor.execute(
            'INSERT INTO "dict_rows" (domain, code, name) VALUES (?, ?, ?)',
            ("域A", "A1", "甲"),
        )
        db.conn.commit()
    finally:
        db.conn.close()
    entry = {"kind": "local", "name": Path(db_path).stem, "path": db_path}

    original_question = QMessageBox.question
    original_info = QMessageBox.information
    original_warning = QMessageBox.warning
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    try:
        editor = VectorFieldEditor(None)
        editor.load(entry, "地块")
        names = [row.name for row in editor._rows]
        expect("code" in names and "amount" in names, f"字段没加载：{names}")
        code_index = names.index("code")
        amount_index = names.index("amount")
        editor.table.item(code_index, 0).setText("code2")
        combo = editor.table.cellWidget(amount_index, 2)
        combo.setCurrentIndex(combo.findData("text"))
        editor._add_field()
        editor.table.item(len(editor._rows) - 1, 0).setText("note")
        editor.table.item(len(editor._rows) - 1, 1).setText("备注")
        editor._save()
        QApplication.processEvents()

        db = local_db_util(db_path)
        try:
            schema = {
                row[1]: row[2]
                for row in db.cursor.execute('PRAGMA table_info("地块")')
            }
            rows = db.cursor.execute(
                'SELECT code2, amount, note FROM "地块" ORDER BY fid'
            ).fetchall()
        finally:
            db.conn.close()
        expect("code2" in schema and "note" in schema, f"改名/加字段没落库：{schema}")
        expect("TEXT" in schema["amount"].upper(), f"类型没改：{schema['amount']}")
        expect([tuple(r)[:2] for r in rows] == [("A", "1"), ("B", "2")],
               f"数据没保住：{rows}")
        editor.deleteLater()

        dict_editor = DictTableEditor(None)
        dict_editor.load(entry, "dict_rows")
        QApplication.processEvents()
        expect(dict_editor._canonical, "字典表应使用字典管理风格树")
        domain_node = dict_editor.tree.topLevelItem(0)
        dict_editor.tree.expandItem(domain_node)
        QApplication.processEvents()
        item_nodes = [
            domain_node.child(i)
            for i in range(domain_node.childCount())
            if domain_node.child(i).data(0, dict_editor._KIND) == dict_editor._K_ITEM
        ]
        expect(len(item_nodes) == 1, f"字典项没有加载：{len(item_nodes)}")
        item_nodes[0].setText(0, "甲改")
        QApplication.processEvents()
        db = local_db_util(db_path)
        try:
            value = db.cursor.execute(
                'SELECT name FROM "dict_rows" WHERE code = "A1"'
            ).fetchone()[0]
        finally:
            db.conn.close()
        expect(value == "甲改", f"字典行编辑没落库：{value}")
        dict_editor.tree.setCurrentItem(item_nodes[0])
        dict_editor._delete_selected()
        QApplication.processEvents()
        db = local_db_util(db_path)
        try:
            left = db.cursor.execute('SELECT COUNT(*) FROM "dict_rows"').fetchone()[0]
        finally:
            db.conn.close()
        expect(int(left) == 0, f"删除字典行没落库：{left}")
        dict_editor.deleteLater()
    finally:
        QMessageBox.question = original_question
        QMessageBox.information = original_info
        QMessageBox.warning = original_warning
    return "字段编辑器保存 / 字典行编辑与删除 通过"


@case
def case_domain_delete(out: Path) -> str:
    """删除本地字典域：引用它的字段关联要一起清掉，其它域不受影响。"""
    from qgis.PyQt.QtWidgets import QApplication, QMessageBox

    from app.core.domain.create_data.dict_tables import create_local_dict_table
    from app.core.domain.dict_manage import DictService
    from app.core.util.field_meta import (
        FieldMeta,
        read_field_meta,
        upsert_field_meta,
    )
    from app.core.util.local_db_util import local_db_util
    from app.ui.docks.table_detail import DictTableEditor

    work = out / "domain_delete"
    db_path = _new_container(work, "gpkg")
    db = local_db_util(db_path)
    try:
        db.create_vector_table(
            "地块",
            columns=[("code", "TEXT"), ("code_b", "TEXT")],
            geometry_column=None,
            register=True,
        )
        upsert_field_meta(
            db.conn,
            "地块",
            "code",
            FieldMeta(
                dict_table="测试字典", dict_domain="域A", dict_local_table="本地字典"
            ),
        )
        upsert_field_meta(
            db.conn,
            "地块",
            "code_b",
            FieldMeta(
                dict_table="测试字典", dict_domain="域B", dict_local_table="本地字典"
            ),
        )
        db.conn.commit()
    finally:
        db.conn.close()
    service = DictService(work / "dicts.db")
    category = service.create_category("测试字典")
    for name in ("域A", "域B"):
        domain = service.create_domain(category.name, name)
        service.add_item(domain.id, "01", "甲")
    create_local_dict_table(
        db_path, "本地字典", source_category=category.name, dict_service=service
    )

    editor = DictTableEditor(None)
    removed: list[tuple] = []
    editor.dictDomainsDeleted.connect(lambda *args: removed.append(args))
    original_question = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    try:
        editor.load({"path": db_path}, "本地字典")
        node = next(
            editor.tree.topLevelItem(i)
            for i in range(editor.tree.topLevelItemCount())
            if editor.tree.topLevelItem(i).text(0) == "域A"
        )
        editor.tree.setCurrentItem(node)
        editor._delete_selected()
        QApplication.processEvents()
    finally:
        QMessageBox.question = original_question
        editor.deleteLater()

    db = local_db_util(db_path)
    try:
        metas = read_field_meta(db.conn, "地块")
        left = db.cursor.execute(
            'SELECT domain FROM "本地字典" GROUP BY domain ORDER BY domain'
        ).fetchall()
        attr = db.cursor.execute(
            "SELECT attr FROM user_relation WHERE table_name = '本地字典'"
        ).fetchone()[0]
    finally:
        db.conn.close()
    expect(not metas["code"].dict_table, f"域A 的字段关联没清：{metas['code']}")
    expect(
        metas["code_b"].dict_domain == "域B",
        f"域B 的字段关联不应受影响：{metas['code_b']}",
    )
    expect([str(row[0]) for row in left] == ["域B"], f"域A 的行没删干净：{left}")
    expect("域A" not in attr and "域B" in attr, f"注册信息里的域清单没更新：{attr}")
    expect(removed and removed[0][3] == ["域A"], f"删除信号不对：{removed}")
    return "删除字典域同步清字段关联 / 更新域清单 通过"


@case
def case_attribute_dict(out: Path) -> str:
    """属性表：字段挂字典时显示名称，编辑下拉列出字典域全部值。"""
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import QStyleOptionViewItem, QWidget

    from app.core.domain.create_data.dict_tables import create_local_dict_table
    from app.core.domain.dict_manage import DictService
    from app.core.util.field_meta import FieldMeta, upsert_field_meta
    from app.core.util.local_db_util import local_db_util
    from app.core.util.local_layer_loader import open_local_vector_table
    from app.ui.docks.attribute_dock import (
        PagedAttributeTableModel,
        _DictTreeCombo,
        _DictValueDelegate,
    )

    work = out / "attr_dict"
    db_path = _new_container(work, "gpkg")
    service = DictService(work / "dicts.db")
    category = service.create_category("测试字典")
    domain = service.create_domain(category.name, "FL_SYS_TEST-图斑地类")
    service.add_item(domain.id, "1", "I级")
    service.add_item(domain.id, "2", "II级")
    create_local_dict_table(
        db_path, "地类字典", source_category=category.name, dict_service=service
    )
    db = local_db_util(db_path)
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

    layer, error = open_local_vector_table(db_path, "地块")
    expect(layer is not None, f"图层打不开：{error}")
    model = PagedAttributeTableModel(layer)
    column = model.field_names.index("DLBM") + 1
    index = model.index(0, column)
    expect(
        model.data(index, Qt.ItemDataRole.DisplayRole) == "I级",
        f"属性表没有显示字典名称：{model.data(index, Qt.ItemDataRole.DisplayRole)}",
    )
    expect(
        str(model.data(index, Qt.ItemDataRole.EditRole)) == "1",
        "EditRole 应保留原始 code",
    )
    parent = QWidget()
    delegate = _DictValueDelegate(parent, model)
    editor = delegate.createEditor(parent, QStyleOptionViewItem(), index)
    expect(isinstance(editor, _DictTreeCombo), "字典字段应使用树形下拉编辑器")
    expect(
        "图斑地类" in editor._root.text(),
        f"缺少字典域标题：{editor._root.text()}",
    )
    expect(
        {child.text() for child in [editor._root.child(i) for i in range(editor._root.rowCount())]}
        == {"(1)I级", "(2)II级"},
        "字典值没有挂到域节点下",
    )
    delegate.setEditorData(editor, index)
    expect(editor.selected_code() == "1", "编辑器没有回填当前 code")
    layer.startEditing()
    try:
        # 选「空值」应写 NULL；再选具体值应写回 code
        editor._on_tree_clicked(editor._empty_item.index())
        expect(editor.is_cleared(), "空值选项没有生效")
        delegate.setModelData(editor, model, index)
        expect(
            model.data(index, Qt.ItemDataRole.EditRole) is None,
            "空值没有写成 NULL",
        )
        editor.set_value("1")
        editor._on_tree_clicked(editor._items["2"].index())
        delegate.setModelData(editor, model, index)
        expect(
            model.data(index, Qt.ItemDataRole.DisplayRole) == "II级",
            "编辑后没有按字典显示名称",
        )
    finally:
        layer.rollBack()
    layer.deleteLater()
    return "属性表字典显示 / 下拉取值通过"


@case
def case_field_search(out: Path) -> str:
    """数据管理字段结构：搜索框只隐藏不匹配的字段行。"""
    from app.core.util.local_db_util import local_db_util
    from app.ui.docks.table_detail import VectorFieldEditor

    work = out / "field_search"
    db_path = _new_container(work, "gpkg")
    db = local_db_util(db_path)
    try:
        db.create_vector_table(
            "地块",
            columns=[("DLBM", "TEXT"), ("QSXZ", "TEXT"), ("AREA", "REAL")],
            geometry_column=None,
            register=True,
        )
    finally:
        db.conn.close()
    editor = VectorFieldEditor(None)
    try:
        editor.load({"path": db_path}, "地块")

        def hidden() -> list[bool]:
            return [
                editor.table.isRowHidden(i)
                for i in range(editor.table.rowCount())
            ]

        editor.search_edit.setText("QSXZ")
        expect(hidden() == [True, False, True], f"按字段名筛选不对：{hidden()}")
        editor.search_edit.setText("zzz")
        expect(all(hidden()), f"无匹配时应全部隐藏：{hidden()}")
        editor.search_edit.setText("")
        expect(not any(hidden()), f"清空搜索后应全部显示：{hidden()}")
    finally:
        editor.deleteLater()
    return "字段结构搜索通过"


@case
def case_field_sort(out: Path) -> str:
    """字段列表表头排序：只改显示顺序，保存用的真实顺序不变。"""
    from app.core.util.local_db_util import local_db_util
    from app.ui.docks.table_detail import VectorFieldEditor, _COL_NAME

    work = out / "field_sort"
    db_path = _new_container(work, "gpkg")
    db = local_db_util(db_path)
    try:
        db.create_vector_table(
            "地块",
            columns=[("B", "TEXT"), ("A", "TEXT"), ("C", "TEXT")],
            geometry_column=None,
            register=True,
        )
    finally:
        db.conn.close()
    editor = VectorFieldEditor(None)
    try:
        editor.load({"path": db_path}, "地块")
        editor._on_header_sort(_COL_NAME)
        shown = [
            editor.table.item(i, _COL_NAME).text()
            for i in range(editor.table.rowCount())
        ]
        expect(shown == ["A", "B", "C"], f"升序不对：{shown}")
        expect(
            [row.name for row in editor._rows] == ["B", "A", "C"],
            "真实字段顺序不应被排序改变",
        )
        editor._on_header_sort(_COL_NAME)
        shown = [
            editor.table.item(i, _COL_NAME).text()
            for i in range(editor.table.rowCount())
        ]
        expect(shown == ["C", "B", "A"], f"降序不对：{shown}")
        expect(
            [row.name for row in editor._rows] == ["B", "A", "C"],
            "降序后真实字段顺序仍不应改变",
        )
    finally:
        editor.deleteLater()
    return "字段列表临时排序通过"


@case
def case_unused_dict_after_delete(out: Path) -> str:
    """删除图层后，它引用过的字典应变成“未使用”（悬空字段元数据不能算数）。"""
    from app.core.domain.delete_data import (
        delete_registered_data,
        unused_dict_tables,
    )
    from app.core.util.field_meta import (
        FieldMeta,
        read_field_meta,
        upsert_field_meta,
    )
    from app.core.util.local_db_util import local_db_util

    work = out / "unused_dict"
    db_path = _new_container(work, "gpkg")
    db = local_db_util(db_path)
    try:
        for name in ("D1", "D2"):
            db.create_vector_table(
                name,
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
                f'INSERT INTO "{name}" (domain, code, name) VALUES (?, ?, ?)',
                ("dom", "1", "甲"),
            )
            db.add_domain_table(
                name, "dict", '{"dict_table":"%s","domains":["dom"]}' % name
            )
        db.create_vector_table(
            "layer", columns=[("f", "TEXT")], geometry_column=None, register=True
        )
        upsert_field_meta(
            db.conn,
            "layer",
            "f",
            FieldMeta(dict_table="D1", dict_domain="dom", dict_local_table="D1"),
            commit=True,
        )
    finally:
        db.conn.close()

    entry = {"kind": "local", "name": Path(db_path).stem, "path": db_path}
    before = unused_dict_tables(entry)
    expect(before == ["D2"], f"删除前未使用字典不对：{before}")
    delete_registered_data(entry, "spatial", "layer")
    after = unused_dict_tables(entry)
    expect(set(after) == {"D1", "D2"}, f"删除图层后未使用字典不对：{after}")
    db = local_db_util(db_path)
    try:
        expect(not read_field_meta(db.conn, "layer"), "删除图层后字段元数据没有清理")
    finally:
        db.conn.close()
    return "删除图层后字典正确变为未使用通过"


@case
def case_alias_repair(out: Path) -> str:
    """中文别名乱码：字段列表读出来要修复（与属性表显示同一口径）。"""
    from app.core.util.local_db_util import local_db_util
    from app.ui.docks.table_detail import VectorFieldEditor

    work = out / "alias_repair"
    db_path = _new_container(work, "gpkg")
    db = local_db_util(db_path)
    try:
        db.create_vector_table(
            "地块",
            columns=[("GDLX", "TEXT")],
            geometry_column="geom",
            geometry_type="POINT",
            srid=4490,
            register=True,
        )
        db.set_gpkg_column_alias(
            "地块", "GDLX", "林地类型".encode("utf-8").decode("latin-1")
        )
    finally:
        db.conn.close()
    editor = VectorFieldEditor(None)
    try:
        editor.load({"path": db_path}, "地块")
        row = next((item for item in editor._rows if item.name == "GDLX"), None)
        expect(row is not None, "字段没有加载")
        expect(row.alias == "林地类型", f"别名没有修复：{row.alias!r}")
    finally:
        editor.deleteLater()
    return "字段别名乱码修复通过"


@case
def case_attribute_locate(out: Path) -> str:
    """属性表双击定位：_feature_by_fid 必须从 content_widget.model 取缓存。"""
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer

    from app.ui.docks.attribute_dock import AttributeDock

    layer = QgsVectorLayer(
        "Point?crs=EPSG:4490&field=code:string(20)", "定位测试", "memory"
    )
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(1, 2)))
    feature.setAttributes(["A"])
    layer.dataProvider().addFeatures([feature])
    layer.updateExtents()
    dock = AttributeDock(layer, parent=None)
    try:
        expect(dock.content_widget.model is not None, "属性表模型没有建出来")
        fid = next(layer.getFeatures()).id()
        located = dock._feature_by_fid(fid)
        expect(located is not None and located.id() == fid, "按 fid 取要素失败")
    finally:
        dock.close()
        dock.deleteLater()
    return "属性表双击定位取要素通过"


@case
def case_data_manage_dialog(out: Path) -> str:
    """数据管理改成对话框：能构造面板，尺寸比默认 Dock 更宽。"""
    from app.ui.docks.data_manage_dock import DataManageDialog

    dialog = DataManageDialog(None)
    try:
        expect(dialog.panel is not None, "对话框里没有数据管理面板")
        expect(dialog.width() >= 1200, f"对话框宽度不够：{dialog.width()}")
        expect(dialog.height() >= 700, f"对话框高度不够：{dialog.height()}")
    finally:
        dialog.close()
        dialog.deleteLater()
    return "数据管理对话框构造通过"


@case
def case_rename_table(out: Path) -> str:
    """表重命名：SQLite 表名、user_relation、GPKG/SpatiaLite 元数据同步。"""
    from app.core.domain.local_table_edit import rename_local_table
    from app.core.util.local_db_util import local_db_util
    from app.core.util.local_layer_loader import open_local_vector_table

    work = out / "rename"
    db_path = _new_container(work, "gpkg")
    layer = _make_point_layer(db_path)
    _add_points(layer, [["A", 1]])
    del layer
    gc.collect()
    db = local_db_util(db_path)
    try:
        db.create_vector_table(
            "dict_rows",
            columns=[("code", "TEXT"), ("name", "TEXT")],
            geometry_column=None,
            register=False,
        )
        db.add_domain_table("dict_rows", "dict")
        db.conn.commit()
    finally:
        db.conn.close()

    rename_local_table(db_path, "地块", "地块改")
    rename_local_table(db_path, "dict_rows", "dict_rows_new")

    db = local_db_util(db_path)
    try:
        tables = {
            row[0]
            for row in db.cursor.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        rel = {
            row[0]
            for row in db.cursor.execute(
                "SELECT table_name FROM user_relation"
            )
        }
        contents = {
            row[0]
            for row in db.cursor.execute("SELECT table_name FROM gpkg_contents")
        }
        geom_meta = {
            row[0]
            for row in db.cursor.execute(
                "SELECT table_name FROM gpkg_geometry_columns"
            )
        }
    finally:
        db.conn.close()
    expect("地块改" in tables and "地块" not in tables, f"矢量表没改名：{tables}")
    expect("dict_rows_new" in tables and "dict_rows" not in tables, "字典表没改名")
    expect("地块改" in rel and "dict_rows_new" in rel, f"user_relation 没同步：{rel}")
    expect("地块改" in contents and "地块改" in geom_meta, "GPKG 元数据没同步")
    reopened, error = open_local_vector_table(db_path, "地块改")
    expect(reopened is not None, f"改名后打不开：{error}")
    expect(reopened.featureCount() == 1, "改名后要素数不对")
    return "表重命名与元数据同步通过"


@case
def case_field_structure(out: Path) -> str:
    """字段结构导出 .xls + 规范检查（字典缺失 / 字典值缺失 / 本地缺字典表）。"""
    from app.core.domain.dict_manage import DictService
    from app.core.domain.field_structure import (
        check_field_structure,
        export_field_structure_xls,
    )
    from app.core.util.field_meta import FieldMeta, upsert_field_meta
    from app.core.util.local_db_util import local_db_util

    work = out / "field_structure"
    db_path = _new_container(work, "gpkg")
    layer = _make_point_layer(db_path)
    _add_points(layer, [["A1", 1], ["B", 2]])
    del layer
    gc.collect()
    db = local_db_util(db_path)
    try:
        db.create_vector_table(
            "dict_rows",
            columns=[
                ("domain", "TEXT NOT NULL"),
                ("code", "TEXT NOT NULL"),
                ("name", "TEXT"),
            ],
            geometry_column=None,
            register=False,
        )
        db.add_domain_table(
            "dict_rows", "dict", '{"dict_table": "测试字典", "domains": ["域A"]}'
        )
        db.cursor.executemany(
            'INSERT INTO "dict_rows" (domain, code, name) VALUES (?, ?, ?)',
            [("域A", "A1", "甲"), ("域A", "A2", "乙")],
        )
        db.set_gpkg_column_alias("地块", "code", "代码")
        upsert_field_meta(
            db.conn,
            "地块",
            "code",
            FieldMeta(dict_table="测试字典", dict_domain="域A"),
            commit=True,
        )
    finally:
        db.conn.close()

    # 导出 .xls：表头必须与创建数据模板一致，字典字段类型为「字典」
    xls = work / "字段结构.xls"
    written, count = export_field_structure_xls(str(xls), db_path, "地块")
    expect(count == 2, f"应导出 2 个业务字段，实际 {count}")
    import xlrd

    sheet = xlrd.open_workbook(written).sheet_by_index(0)
    headers = [sheet.cell_value(0, c) for c in range(sheet.ncols)]
    from app.core.domain.create_data.template import TEMPLATE_HEADERS

    expect(headers == list(TEMPLATE_HEADERS), f"导出表头不对：{headers}")
    exported = {
        sheet.cell_value(r, 0): [sheet.cell_value(r, c) for c in range(sheet.ncols)]
        for r in range(1, sheet.nrows)
    }
    expect(exported["code"][1] == "代码", "别名没导出")
    expect(exported["code"][2] == "字典", f"字典字段类型不对：{exported['code'][2]}")
    expect(exported["code"][5] == "测试字典", "字典表没导出")
    expect(exported["code"][6] == "域A", "字典域没导出")

    service = DictService(work / "app_dicts.db")
    service.create_category("测试字典")
    domain = service.create_domain("测试字典", "域A")
    service.add_item(domain.id, "A1", "甲")

    issues = check_field_structure(db_path, "地块", dict_service=service)
    errors = [item for item in issues if item.level == "错误"]
    expect(not errors, f"不应有错误：{errors}")
    expect(
        any("字典值缺失" in item.message for item in issues),
        f"没有检查出字典值缺失：{issues}",
    )

    # 本地字典表没了：应用级字典里有 → 警告；没有 → 错误
    db = local_db_util(db_path)
    try:
        db.cursor.execute('DROP TABLE "dict_rows"')
        db.cursor.execute('DELETE FROM user_relation WHERE table_name = "dict_rows"')
        db.conn.commit()
    finally:
        db.conn.close()
    issues = check_field_structure(db_path, "地块", dict_service=service)
    expect(
        any(
            item.level == "错误" and "本地库缺少字典表" in item.message
            for item in issues
        ),
        f"本地缺字典表应为错误级别：{issues}",
    )
    empty_service = DictService(work / "empty_dicts.db")
    issues = check_field_structure(db_path, "地块", dict_service=empty_service)
    expect(
        any(item.level == "错误" and "字典表「测试字典」不存在" in item.message for item in issues),
        f"没有检查出字典表不存在：{issues}",
    )
    return "字段结构 .xls 导出 / 字典规范检查通过"


@case
def case_local_dict_table(out: Path) -> str:
    """本地库添加字典表：空表 / 从应用级字典复制 / 清字段字典关联。"""
    from app.core.domain.create_data.dict_tables import create_local_dict_table
    from app.core.domain.dict_manage import DictService
    from app.core.util.field_meta import (
        FieldMeta,
        clear_dict_links,
        read_field_meta,
        upsert_field_meta,
    )
    from app.core.util.local_db_util import local_db_util

    work = out / "local_dict"
    db_path = _new_container(work, "gpkg")
    count = create_local_dict_table(db_path, "本地字典")
    expect(count == 0, "空字典表不应写入行")
    db = local_db_util(db_path)
    try:
        expect("本地字典" in db.query_all_table_name()["dict"], "空字典表没有登记")
        columns = {
            str(row[1]) for row in db.cursor.execute('PRAGMA table_info("本地字典")')
        }
        expect(
            {"domain", "code", "name", "parent_code", "sort_order"} <= columns,
            f"字典表结构不对：{columns}",
        )
        upsert_field_meta(
            db.conn,
            "some_table",
            "fld",
            FieldMeta(
                dict_table="本地字典",
                dict_domain="域A",
                dict_local_table="本地字典",
            ),
            commit=True,
        )
        cleared = clear_dict_links(
            db.conn, category="本地字典", local_table="本地字典"
        )
        db.conn.commit()
        metas = read_field_meta(db.conn, "some_table")
    finally:
        db.conn.close()
    expect(cleared == 1, f"应清除 1 个字段的字典关联，实际 {cleared}")
    expect(
        not metas["fld"].dict_table and not metas["fld"].dict_domain,
        "字段字典关联没有清空",
    )

    service = DictService(work / "dicts.db")
    category = service.create_category("测试字典")
    domain = service.create_domain(category.name, "域A")
    service.add_item(domain.id, "A1", "甲")
    count = create_local_dict_table(
        db_path,
        "复制字典",
        source_category="测试字典",
        dict_service=service,
    )
    expect(count == 1, f"应复制 1 条字典项，实际 {count}")
    db = local_db_util(db_path)
    try:
        rows = db.cursor.execute(
            'SELECT domain, code, name FROM "复制字典"'
        ).fetchall()
    finally:
        db.conn.close()
    expect(
        [tuple(row) for row in rows] == [("域A", "A1", "甲")],
        f"复制内容不对：{rows}",
    )
    return "本地字典表创建 / 复制 / 清关联 通过"


@case
def case_clear_dict(out: Path) -> str:
    """字段编辑器清除字典挂接：类型回文本、保存有确认、元数据清空。"""
    from qgis.PyQt.QtWidgets import QDialog, QMessageBox

    import app.ui.docks.table_detail as td_mod
    from app.core.domain.create_data import FieldSpec
    from app.core.util.field_meta import (
        FieldMeta,
        read_field_meta,
        upsert_field_meta,
    )
    from app.core.util.local_db_util import local_db_util
    from app.ui.docks.table_detail import VectorFieldEditor

    work = out / "clear_dict"
    db_path = _new_container(work, "gpkg")
    layer = _make_point_layer(
        db_path,
        fields=(
            FieldSpec(
                name="dicode",
                type_key="dict",
                length=20,
                dict_table="测试字典",
                dict_domain="域A",
            ),
        ),
    )
    del layer
    gc.collect()
    db = local_db_util(db_path)
    try:
        upsert_field_meta(
            db.conn,
            "地块",
            "dicode",
            FieldMeta(
                dict_table="测试字典",
                dict_domain="域A",
                dict_local_table="测试字典",
            ),
            commit=True,
        )
    finally:
        db.conn.close()

    class FakeClear:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selected(self):
            return None

        def selected_local_table(self) -> str:
            return ""

        def deleteLater(self) -> None:  # noqa: N802 —— 模拟 Qt 方法
            pass

    editor = VectorFieldEditor(None)
    editor.load({"path": db_path}, "地块")
    row_index = [row.name for row in editor._rows].index("dicode")
    original_dialog = td_mod.DictPickDialog
    td_mod.DictPickDialog = FakeClear
    try:
        editor._pick_dict(row_index)
    finally:
        td_mod.DictPickDialog = original_dialog
    row = editor._rows[row_index]
    expect(row.type_key == "text", f"清除后类型应回文本：{row.type_key}")
    expect(not row.dict_table and not row.dict_domain, "清除后字典两列应为空")

    original_question = QMessageBox.question
    original_information = QMessageBox.information
    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    QMessageBox.information = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Ok
    )
    try:
        editor._save()
    finally:
        QMessageBox.question = original_question
        QMessageBox.information = original_information
    db = local_db_util(db_path)
    try:
        metas = read_field_meta(db.conn, "地块")
    finally:
        db.conn.close()
    meta = metas.get("dicode")
    expect(
        meta is None or not meta.dict_table,
        f"清除字典挂接没有落库：{meta}",
    )
    editor.deleteLater()
    return "清除字典挂接 / 保存提示 / 元数据清空 通过"


@case
def case_local_picker(out: Path) -> str:
    """字典挂接对话框：只列当前库的字典表（不再回退系统字典）。"""
    from app.core.domain.create_data.dict_tables import create_local_dict_table
    from app.core.domain.dict_manage import DictService
    from app.ui.dialogs.dict_pick_dialog import DictPickDialog

    work = out / "local_picker"
    db_path = _new_container(work, "gpkg")
    service = DictService(work / "dicts.db")
    category = service.create_category("系统字典")
    domain = service.create_domain(category.name, "域B")
    service.add_item(domain.id, "B1", "乙")
    create_local_dict_table(
        db_path,
        "本地字典",
        source_category="系统字典",
        dict_service=service,
    )
    dialog = DictPickDialog(
        None,
        service=service,
        local_db_path=db_path,
        category="系统字典",
        field_name="xxx",
        field_alias="域B",
    )
    try:
        texts = [
            dialog.category_combo.itemText(i)
            for i in range(dialog.category_combo.count())
        ]
        expect(len(texts) == 1, f"只应列当前库的字典表：{texts}")
        expect(texts[0].startswith("当前库："), f"字典表标签不对：{texts}")
        expect(
            not any(text.startswith("系统：") for text in texts),
            f"不应再列系统字典：{texts}",
        )
        expect(dialog.selected_source() == "local", "默认应选当前库字典")
        expect(dialog.selected_local_table() == "本地字典", "本地字典表名不对")
        expect(dialog.selected() == ("本地字典", "域B"), f"选中值不对：{dialog.selected()}")
    finally:
        dialog.close()
        dialog.deleteLater()
    return "字典挂接只列当前库字典 通过"


@case
def case_save_system_dict(out: Path) -> str:
    """字段挂接系统字典后保存：自动把字典落进当前库并记下本地表名。"""
    from qgis.PyQt.QtWidgets import QMessageBox

    import app.core.domain.dict_manage as dm_mod
    from app.core.domain.dict_manage import DictService
    from app.core.util.field_meta import FieldMeta, read_field_meta, upsert_field_meta
    from app.core.util.local_db_util import local_db_util
    from app.ui.docks.table_detail import VectorFieldEditor

    work = out / "save_system"
    db_path = _new_container(work, "gpkg")
    layer = _make_point_layer(db_path)
    del layer
    gc.collect()
    db = local_db_util(db_path)
    try:
        upsert_field_meta(
            db.conn,
            "地块",
            "code",
            FieldMeta(dict_table="系统字典", dict_domain="域B"),
            commit=True,
        )
    finally:
        db.conn.close()
    service = DictService(work / "dicts.db")
    category = service.create_category("系统字典")
    domain = service.create_domain(category.name, "域B")
    service.add_item(domain.id, "B1", "乙")

    editor = VectorFieldEditor(None)
    editor.load({"path": db_path}, "地块")
    original_service = dm_mod.DictService
    dm_mod.DictService = lambda *a, **k: service
    original_information = QMessageBox.information
    QMessageBox.information = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Ok
    )
    try:
        editor._save()
    finally:
        dm_mod.DictService = original_service
        QMessageBox.information = original_information
    editor.deleteLater()

    db = local_db_util(db_path)
    try:
        registered = db.query_all_table_name()["dict"]
        metas = read_field_meta(db.conn, "地块")
        count = (
            db.cursor.execute('SELECT COUNT(*) FROM "系统字典"').fetchone()[0]
            if "系统字典" in registered
            else 0
        )
    finally:
        db.conn.close()
    expect("系统字典" in registered, f"系统字典没有落进当前库：{registered}")
    expect(int(count) == 1, f"字典行数不对：{count}")
    meta = metas.get("code")
    expect(
        meta is not None and meta.dict_local_table == "系统字典",
        f"字段没有记下本地字典表：{meta}",
    )
    return "系统字典保存时自动落库 通过"


@case
def case_local_dict_excel(out: Path) -> str:
    """本地字典 Excel 导出 / 导入（.xls）。"""
    from app.core.domain.create_data.dict_tables import create_local_dict_table
    from app.core.domain.dict_manage import DictService
    from app.core.domain.local_dict_excel import (
        export_local_dict_xls,
        import_local_dict_xls,
    )
    from app.core.util.local_db_util import local_db_util

    work = out / "local_excel"
    db_path = _new_container(work, "gpkg")
    service = DictService(work / "dicts.db")
    category = service.create_category("导出字典")
    domain = service.create_domain(category.name, "域A")
    service.add_item(domain.id, "A1", "甲")
    service.add_item(domain.id, "A2", "乙", parent_code="A1")
    create_local_dict_table(
        db_path, "导出字典", source_category="导出字典", dict_service=service
    )
    xls, count = export_local_dict_xls(str(work / "字典.xls"), db_path, "导出字典")
    expect(count == 2, f"导出条数不对：{count}")

    create_local_dict_table(db_path, "导入字典")
    report = import_local_dict_xls(xls, db_path, "导入字典", replace=True)
    expect(
        report.inserted == 2 and report.updated == 0,
        f"导入计数不对：{report}",
    )
    db = local_db_util(db_path)
    try:
        rows = db.cursor.execute(
            'SELECT domain, code, name, parent_code FROM "导入字典" ORDER BY code'
        ).fetchall()
    finally:
        db.conn.close()
    expect(
        [tuple(row) for row in rows]
        == [("域A", "A1", "甲", ""), ("域A", "A2", "乙", "A1")],
        f"导入内容不对：{rows}",
    )
    return "本地字典 Excel 导出 / 导入 通过"


def cleanup(out: Path) -> None:
    shutil.rmtree(out, ignore_errors=True)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="本地表编辑自检")
    parser.add_argument("--out", type=Path, default=_ROOT / ".tmp" / "local_table_edit")
    parser.add_argument("--only", nargs="+", choices=[name for name, _fn in CASES])
    parser.add_argument("--clean", action="store_true")
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
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            results.append((name, False, f"{type(exc).__name__}: {exc}"))
    print("\n" + "=" * 60)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<18} {detail}")
    failed = [name for name, ok, _detail in results if not ok]
    print("=" * 60)
    print(
        f"合计 {len(results)} 项，通过 {len(results) - len(failed)} 项"
        + (f"，失败：{', '.join(failed)}" if failed else "")
    )
    if args.clean:
        cleanup(out)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    raise SystemExit(main())
