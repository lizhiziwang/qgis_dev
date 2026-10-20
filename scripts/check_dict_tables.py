"""「创建数据 → 字典落库」自检（无界面）。

验证 :mod:`app.core.domain.create_data.dict_tables`：

1. ``app-persist``    —— 应用级字典：一个字典表落一张表、多域用 domain 列共存、
                         登记 user_relation type=dict、唯一索引、重复写入不重复
2. ``source-import``  —— 外部源库 dict 表按字段的字典表/域归一化后并入同一张表
3. ``collision``      —— 目标库已有同名普通表时自动加后缀，不覆盖原表
4. ``empty-domain``   —— 选中空域时不写脏数据，其余域照常落库

用法::

    E:/app/QGIS421/bin/python-qgis.bat -u scripts/check_dict_tables.py
"""

from __future__ import annotations

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


def _new_gpkg(path: Path) -> None:
    from app.core.util.local_db_util import local_db_util

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    db = local_db_util(path)
    db.conn.close()


def _dict_service(path: Path):
    from app.core.domain.dict_manage import DictService

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    service = DictService(path)
    category = service.create_category("测试字典")
    domain_a = service.create_domain(category.name, "域A")
    domain_b = service.create_domain(category.name, "域B")
    service.add_item(domain_a.id, "A1", "甲")
    service.add_item(domain_a.id, "A2", "乙", parent_code="A1")
    service.add_item(domain_b.id, "B1", "丙")
    return service, category


@case
def case_app_persist(out: Path) -> str:
    from app.core.domain.create_data.dict_tables import (
        DictPlan,
        build_dict_table_specs,
        persist_dict_tables,
    )
    from app.core.util.local_db_util import local_db_util

    work = out / "app"
    service, category = _dict_service(work / "dicts.db")
    target = work / "target.gpkg"
    _new_gpkg(target)

    plan = DictPlan(
        domains=frozenset(
            {(category.name, "域A"), (category.name, "域B")}
        )
    )
    specs = build_dict_table_specs(str(target), plan, service)
    expect(len(specs) == 1, f"一个字典表应只产出一张目标表，实际 {len(specs)}")
    db = local_db_util(target)
    try:
        written = persist_dict_tables(db, specs)
    finally:
        db.conn.close()
    expect(len(written) == 1, "应写入 1 张字典表")

    db = local_db_util(target)
    try:
        cats = db.query_all_table_name()
        expect(written[0] in cats["dict"], "字典表没有登记为 user_relation type=dict")
        cur = db.cursor.execute(f'SELECT COUNT(*) FROM "{written[0]}"')
        count = int(cur.fetchone()[0])
        expect(count == 3, f"字典表应有 3 行，实际 {count}")
        rows = db.cursor.execute(
            f'SELECT domain, code, name, parent_code FROM "{written[0]}" '
            "ORDER BY domain, code"
        ).fetchall()
        expect(
            [tuple(r) for r in rows]
            == [("域A", "A1", "甲", ""), ("域A", "A2", "乙", "A1"), ("域B", "B1", "丙", "")],
            f"字典行不对：{rows}",
        )
        # 重复写入不产生重复行
        written2 = persist_dict_tables(db, specs)
        expect(written2 == written, "重复写入应复用同一张表")
        count2 = int(
            db.cursor.execute(f'SELECT COUNT(*) FROM "{written[0]}"').fetchone()[0]
        )
        expect(count2 == 3, f"重复写入后行数应仍为 3，实际 {count2}")
    finally:
        db.conn.close()
    return "应用级字典一表多域 / 登记 / 唯一 / 幂等 通过"


@case
def case_source_import(out: Path) -> str:
    from app.core.domain.create_data.dict_tables import (
        DictPlan,
        DictSource,
        build_dict_table_specs,
        persist_dict_tables,
    )
    from app.core.util.local_db_util import local_db_util

    work = out / "source"
    source = work / "source.gpkg"
    _new_gpkg(source)
    db = local_db_util(source)
    try:
        db.create_vector_table(
            "SRC_DICT",
            columns=[
                ("dict_domain", "TEXT"),
                ("code", "TEXT"),
                ("name", "TEXT"),
                ("parent_code", "TEXT"),
            ],
            geometry_column=None,
            register=False,
        )
        db.add_domain_table("SRC_DICT", "dict")
        db.cursor.executemany(
            'INSERT INTO "SRC_DICT" (dict_domain, code, name, parent_code) '
            "VALUES (?, ?, ?, ?)",
            [
                ("旧域", "S1", "源一", ""),
                ("旧域", "S2", "源二", "S1"),
                ("别域", "S3", "源三", ""),
            ],
        )
        db.conn.commit()
    finally:
        db.conn.close()

    target = work / "target.gpkg"
    _new_gpkg(target)
    service, category = _dict_service(work / "dicts.db")
    plan = DictPlan(
        domains=frozenset({(category.name, "域A")}),
        sources=(DictSource(str(source), "SRC_DICT", category.name, "域A"),),
    )
    specs = build_dict_table_specs(str(target), plan, service)
    expect(len(specs) == 1, "源表和应用级字典应合并到同一个字典表")
    db = local_db_util(target)
    try:
        persist_dict_tables(db, specs)
        rows = db.cursor.execute(
            f'SELECT domain, code FROM "{specs[0].table_name}" ORDER BY domain, code'
        ).fetchall()
    finally:
        db.conn.close()
    codes = [tuple(r) for r in rows]
    expect(("域A", "A1") in codes, "应用级字典行没写进去")
    expect(("旧域", "S1") in codes, "源库 dict 表没有归一化写入")
    expect(("别域", "S3") in codes, "源库第二个域没有写进去")
    return "源库 dict 表归一化合并通过"


@case
def case_collision(out: Path) -> str:
    from app.core.domain.create_data.dict_tables import (
        DictPlan,
        build_dict_table_specs,
        persist_dict_tables,
    )
    from app.core.util.local_db_util import local_db_util

    work = out / "collision"
    service, category = _dict_service(work / "dicts.db")
    target = work / "target.gpkg"
    _new_gpkg(target)
    db = local_db_util(target)
    try:
        db.create_vector_table(
            "测试字典",
            columns=[("v", "TEXT")],
            geometry_column=None,
            register=False,
        )
        db.conn.commit()
    finally:
        db.conn.close()

    plan = DictPlan(domains=frozenset({(category.name, "域A")}))
    specs = build_dict_table_specs(str(target), plan, service)
    expect(specs[0].table_name != "测试字典", "目标库已有同名普通表时应加后缀")
    db = local_db_util(target)
    try:
        persist_dict_tables(db, specs)
        count = int(
            db.cursor.execute('SELECT COUNT(*) FROM "测试字典"').fetchone()[0]
        )
    finally:
        db.conn.close()
    expect(count == 0, "原同名普通表不应被写入字典行")
    return f"表名冲突自动避让（{specs[0].table_name}）通过"


@case
def case_empty_domain(out: Path) -> str:
    from app.core.domain.create_data.dict_tables import (
        DictPlan,
        build_dict_table_specs,
        persist_dict_tables,
    )
    from app.core.util.local_db_util import local_db_util

    work = out / "empty"
    service, category = _dict_service(work / "dicts.db")
    empty = service.create_domain(category.name, "空域")
    target = work / "target.gpkg"
    _new_gpkg(target)
    plan = DictPlan(domains=frozenset({(category.name, "空域")}))
    specs = build_dict_table_specs(str(target), plan, service)
    expect(len(specs) == 1, "空域也应产出一张字典表（列结构仍在）")
    db = local_db_util(target)
    try:
        persist_dict_tables(db, specs)
        count = int(
            db.cursor.execute(f'SELECT COUNT(*) FROM "{specs[0].table_name}"').fetchone()[0]
        )
        attrs = db.cursor.execute(
            "SELECT attr FROM user_relation WHERE table_name = ?",
            (specs[0].table_name,),
        ).fetchone()
    finally:
        db.conn.close()
    expect(count == 0, "空域不应写入数据行")
    expect(attrs is not None and "空域" in str(attrs[0]), "空域信息应记在 attr 里")
    expect(empty.item_count == 0, "空域条目数应为 0")
    return "空域保留表结构与元数据通过"


@case
def case_create_layer_integration(out: Path) -> str:
    """走 CreateLayerInDbService：建图层 + 字典表 + 字段元数据，回读图层能带出字典属性。"""
    from app.core.domain.create_data import (
        CreateLayerInDbService,
        CreateLayerInDbSpec,
        FieldSpec,
    )
    from app.core.domain.create_data.dict_tables import DictPlan, build_dict_table_specs
    from app.core.util.field_meta import (
        DICT_DOMAIN_PROP,
        DICT_LOCAL_TABLE_PROP,
        DICT_TABLE_PROP,
        FIELD_META_TABLE,
        read_field_meta,
    )
    from app.core.util.local_db_util import local_db_util
    from app.core.util.local_layer_loader import open_local_vector_table

    work = out / "integrated"
    service, category = _dict_service(work / "dicts.db")
    target = work / "target.gpkg"
    _new_gpkg(target)
    plan = DictPlan(domains=frozenset({(category.name, "域A")}))
    specs = build_dict_table_specs(str(target), plan, service)
    field = FieldSpec(
        name="area_type",
        alias="地类",
        type_key="dict",
        length=20,
        precision=0,
        dict_table=category.name,
        dict_domain="域A",
        nullable=True,
        visible=False,
    )
    layer = CreateLayerInDbService().create(
        CreateLayerInDbSpec(
            db_path=str(target),
            layer_name="地块",
            geometry_type="Point",
            crs_authid="EPSG:4490",
            fields=(field,),
            dict_tables=tuple(specs),
        )
    )
    expect(layer.isValid(), "建出来的图层无效")
    expect(layer.fields().indexOf("area_type") >= 0, "字段没有建进图层")

    db = local_db_util(target)
    try:
        cats = db.query_all_table_name()
        expect(specs[0].table_name in cats["dict"], "字典表没有登记")
        metas = read_field_meta(db.conn, "地块")
        count = int(
            db.cursor.execute(
                f'SELECT COUNT(*) FROM "{FIELD_META_TABLE}" WHERE table_name = ?',
                ("地块",),
            ).fetchone()[0]
        )
    finally:
        db.conn.close()
    expect(count == 1, f"字段元数据应有 1 行，实际 {count}")
    meta = metas.get("area_type")
    expect(meta is not None, "字段元数据没写")
    expect(meta.dict_table == category.name and meta.dict_domain == "域A", "字典关联不对")
    expect(meta.dict_local_table == specs[0].table_name, "本地字典表名不对")
    expect(not meta.visible, "隐藏字段标记没写")

    reopened, error = open_local_vector_table(str(target), "地块")
    expect(reopened is not None, f"回读失败：{error}")
    expect(
        reopened.customProperty(DICT_TABLE_PROP + "area_type") == category.name,
        "回读图层没带出字典表属性",
    )
    expect(
        reopened.customProperty(DICT_DOMAIN_PROP + "area_type") == "域A",
        "回读图层没带出字典域属性",
    )
    expect(
        reopened.customProperty(DICT_LOCAL_TABLE_PROP + "area_type") == specs[0].table_name,
        "回读图层没带出本地字典表属性",
    )
    return "建图层 + 字典表 + 字段元数据 + 回读属性 通过"


@case
def case_picker(out: Path) -> str:
    """字典挂接对话框：只列当前库的字典表 / 推荐 / 不挂接 / 空库引导。"""
    from app.core.domain.create_data.dict_tables import create_local_dict_table
    from app.ui.dialogs.dict_pick_dialog import DictPickDialog

    work = out / "picker"
    service, category = _dict_service(work / "dicts.db")
    target = work / "picker.gpkg"
    _new_gpkg(target)
    create_local_dict_table(
        str(target), "本地测试字典", source_category=category.name, dict_service=service
    )
    dialog = DictPickDialog(
        None,
        service=service,
        category="本地测试字典",
        domain="域A",
        field_name="城乡村",
        field_alias="村",
        local_db_path=str(target),
    )
    try:
        expect(dialog.category_combo.count() == 1, "只应列出当前库里的 1 张字典表")
        expect(
            dialog.selected() == ("本地测试字典", "域A"),
            f"选中值不对：{dialog.selected()}",
        )
        none_item = None
        for index in range(dialog.tree.topLevelItemCount()):
            item = dialog.tree.topLevelItem(index)
            if item.text(0) == "不挂接字典域":
                none_item = item
        expect(none_item is not None, "缺少「不挂接字典域」")
        dialog.tree.setCurrentItem(none_item)
        expect(dialog.selected() is None, "不挂接字典域应返回 None")
    finally:
        dialog.close()
        dialog.deleteLater()

    # 当前库没有字典：下拉为空、确定禁用，但「添加 / 导入」入口可用
    empty = work / "empty.gpkg"
    _new_gpkg(empty)
    dialog = DictPickDialog(
        None, service=service, field_name="f1", local_db_path=str(empty)
    )
    try:
        expect(dialog.category_combo.count() == 0, "空库不应列出任何字典表")
        expect(not dialog.ok_btn.isEnabled(), "空库时确定应禁用")
        expect(dialog.add_btn.isEnabled(), "空库时应能现场添加字典表")
        expect(dialog.import_btn.isEnabled(), "空库时应能导入外部字典")
        expect("添加字典表" in dialog.status.text(), "空库应给出建表 / 导入引导")
    finally:
        dialog.close()
        dialog.deleteLater()
    return "字典挂接对话框只列当前库字典 / 空库引导通过"


@case
def case_dict_recommend(out: Path) -> str:
    """推荐字典域：域名中文名与字段别名相近的排到推荐组。"""
    from app.core.domain.create_data.dict_tables import create_local_dict_table
    from app.ui.dialogs.dict_pick_dialog import DictPickDialog

    work = out / "recommend"
    service, category = _dict_service(work / "dicts.db")
    service.create_domain(category.name, "FL_SYS_ABC-QSXZ-权属性质")
    service.create_domain(category.name, "FL_SYS_XYZ-GYLQYMC-公益林区域名称")
    target = work / "recommend.gpkg"
    _new_gpkg(target)
    create_local_dict_table(
        str(target), "测试字典", source_category=category.name, dict_service=service
    )
    dialog = DictPickDialog(
        None,
        service=service,
        category=category.name,
        field_name="q_sxz",
        field_alias="权属性质",
        local_db_path=str(target),
    )
    try:
        groups = {}
        for index in range(dialog.tree.topLevelItemCount()):
            item = dialog.tree.topLevelItem(index)
            groups[item.text(0)] = item
        expect("推荐挂接字典域" in groups, f"没有推荐分组：{list(groups)}")
        recommended = [
            groups["推荐挂接字典域"].child(i).text(0)
            for i in range(groups["推荐挂接字典域"].childCount())
        ]
        expect(
            any("权属性质" in name for name in recommended),
            f"权属性质没有被推荐：{recommended}",
        )
        others = [
            groups["其它字典域"].child(i).text(0)
            for i in range(groups["其它字典域"].childCount())
        ] if "其它字典域" in groups else []
        expect(
            any("公益林" in name for name in others),
            f"公益林不应进推荐组：{others}",
        )
        expect(
            dialog.selected() == (category.name, "FL_SYS_ABC-QSXZ-权属性质"),
            f"默认没有选中推荐项：{dialog.selected()}",
        )
    finally:
        dialog.close()
        dialog.deleteLater()
    return "推荐字典域相似度匹配通过"


@case
def case_fields_dict(out: Path) -> str:
    """字段表：双击字典列回填两列并切类型；改回文本时清空字典。"""
    from qgis.PyQt.QtWidgets import QApplication, QDialog

    import app.ui.dialogs.create_data_fields_step as fields_mod
    from app.core.domain.create_data import FieldSpec
    from app.ui.dialogs.create_data_fields_step import FieldsStepPage

    page = FieldsStepPage(None)
    try:
        page.set_fields([FieldSpec(name="f1", type_key="text", length=0)])

        class FakePicker:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

            def selected(self):
                return ("测试字典", "域A")

            def deleteLater(self) -> None:  # noqa: N802 —— 模拟 Qt 方法
                pass

        original = fields_mod.DictPickDialog
        fields_mod.DictPickDialog = FakePicker
        try:
            page._pick_dict(0)
        finally:
            fields_mod.DictPickDialog = original
        spec = page.fields()[0]
        expect(
            spec.dict_table == "测试字典" and spec.dict_domain == "域A",
            f"字典两列没有一起回填：{spec.dict_table}/{spec.dict_domain}",
        )
        expect(spec.type_key == "dict", f"字段类型没切成字典：{spec.type_key}")
        expect(spec.length == 20, f"字典字段默认长度应为 20，实际 {spec.length}")
        expect(
            page.dict_plan().domains == frozenset({("测试字典", "域A")}),
            f"字典计划不对：{page.dict_plan().domains}",
        )

        combo = page.table.cellWidget(0, 3)
        combo.setCurrentIndex(combo.findData("text"))
        QApplication.processEvents()
        spec = page.fields()[0]
        expect(
            not spec.dict_table and not spec.dict_domain,
            "类型改回文本后字典关联没有清空",
        )
    finally:
        page.deleteLater()
    return "字段表字典回填 / 类型联动 / 字典计划 通过"


@case
def case_picker_domain_search(out: Path) -> str:
    """挂接界面搜索字典域：跨当前库的全部字典表，命中别的表会自动切过去。"""
    from app.core.domain.create_data.dict_tables import create_local_dict_table
    from app.ui.dialogs.dict_pick_dialog import DictPickDialog

    work = out / "picker_search"
    service, first = _dict_service(work / "dicts.db")
    second = service.create_category("第二字典")
    domain = service.create_domain(second.name, "FL_SYS_XYZ-GYLQYMC-公益林区域名称")
    service.add_item(domain.id, "Y1", "是")
    target = work / "search.gpkg"
    _new_gpkg(target)
    create_local_dict_table(
        str(target), "第一表", source_category=first.name, dict_service=service
    )
    create_local_dict_table(
        str(target), "第二表", source_category=second.name, dict_service=service
    )
    dialog = DictPickDialog(None, service=service, local_db_path=str(target))
    try:
        expect(dialog.category_combo.currentIndex() == 0, "默认应选第一张字典表")
        dialog.filter_edit.setText("公益林")
        expect(
            dialog.category_combo.currentIndex() == 1,
            f"命中别的字典表时应自动切换：{dialog.category_combo.currentText()}",
        )
        expect(
            dialog.selected() == ("第二表", domain.domain),
            f"跨表搜索结果不对：{dialog.selected()}",
        )
        labels = [
            dialog.tree.topLevelItem(i).text(0)
            for i in range(dialog.tree.topLevelItemCount())
        ]
        expect(
            any("第二表" in label for label in labels),
            f"搜索结果没有按字典表分组：{labels}",
        )
        dialog.filter_edit.setText("")
        expect(
            dialog.tree.topLevelItemCount() >= 2,
            "清空搜索后应恢复当前字典表的域树",
        )
    finally:
        dialog.close()
        dialog.deleteLater()
    return "挂接界面跨字典表搜索字典域通过"


@case
def case_editor_search(out: Path) -> str:
    """数据管理右栏字典表搜索：保留域树，只隐藏不匹配数据（域可直接删）。"""
    from app.core.domain.create_data.dict_tables import create_local_dict_table
    from app.ui.docks.table_detail import DictTableEditor

    work = out / "editor_search"
    service, category = _dict_service(work / "dicts.db")
    target = work / "search.gpkg"
    _new_gpkg(target)
    create_local_dict_table(
        str(target), "搜索字典", source_category=category.name, dict_service=service
    )
    editor = DictTableEditor()
    try:
        editor.load({"path": str(target)}, "搜索字典")
        expect(editor._canonical, "本地字典表应是 canonical 结构")
        editor.search_edit.setText("甲")
        editor._run_search()
        visible = [
            editor.tree.topLevelItem(i)
            for i in range(editor.tree.topLevelItemCount())
            if not editor.tree.topLevelItem(i).isHidden()
        ]
        expect(len(visible) == 1, f"应只显示命中的字典域：{len(visible)}")
        expect(
            visible[0].data(0, editor._KIND) == editor._K_DOMAIN,
            "搜索结果应保留字典域节点",
        )
        expect(
            visible[0].childCount() == 1
            and visible[0].child(0).text(0) == "甲",
            "字典域下的匹配项不对",
        )
        # 域节点可直接选中（原布局的关键）
        editor.tree.setCurrentItem(visible[0])
        expect(
            editor.tree.currentItem().data(0, editor._KIND) == editor._K_DOMAIN,
            "域名节点应可选中",
        )
        # 清空关键词恢复完整树（域 -> 项）
        editor.search_edit.setText("")
        editor._run_search()
        expect(
            editor.tree.topLevelItemCount() >= 1,
            "清空搜索后应恢复字典域树",
        )
    finally:
        editor.deleteLater()
    return "字典表搜索出结果 / 清空恢复通过"


@case
def case_condition_query_domain(out: Path) -> str:
    """条件查询：域字段过滤 + 显示代码（名称(代码)），双击仍写原始代码。"""
    from qgis.PyQt.QtCore import Qt

    from app.core.domain.create_data.dict_tables import create_local_dict_table
    from app.core.util.field_meta import FieldMeta, upsert_field_meta
    from app.core.util.local_db_util import local_db_util
    from app.core.util.local_layer_loader import open_local_vector_table
    from app.ui.dialogs.condition_query import ConditionQueryDialog

    work = out / "condition_query"
    service, category = _dict_service(work / "dicts.db")
    target = work / "query.gpkg"
    _new_gpkg(target)
    db = local_db_util(str(target))
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
            "地块", [{"attributes": {"DLBM": "A1"}, "geometry": "POINT(100 30)"}]
        )
        upsert_field_meta(
            db.conn,
            "地块",
            "DLBM",
            FieldMeta(
                dict_table="地类字典",
                dict_domain="域A",
                dict_local_table="地类字典",
            ),
            commit=True,
        )
    finally:
        db.conn.close()
    create_local_dict_table(
        str(target), "地类字典", source_category=category.name, dict_service=service
    )
    layer, error = open_local_vector_table(str(target), "地块")
    expect(layer is not None, f"图层打不开：{error}")
    dialog = ConditionQueryDialog([layer], current_layer=layer)
    try:
        expect(not dialog.domain_check.isHidden(), "「域字段」应显示出来")
        expect(not dialog.domain_check.isChecked(), "域字段默认不应勾选")
        expect(
            "DLBM"
            in [
                dialog.field_list.item(i).text()
                for i in range(dialog.field_list.count())
            ],
            "默认应显示全部字段（含字典字段）",
        )
        dialog.domain_check.setChecked(True)
        expect(
            [dialog.field_list.item(i).text() for i in range(dialog.field_list.count())]
            == ["DLBM"],
            "勾选域字段后应只列字典字段",
        )
        dialog.domain_check.setChecked(False)
        expect(
            "fid" in [
                dialog.field_list.item(i).text()
                for i in range(dialog.field_list.count())
            ],
            "取消域字段后应显示全部字段",
        )
        dialog.domain_check.setChecked(True)
        dialog.field_list.setCurrentRow(0)
        expect(dialog.show_code_check.isEnabled(), "「显示代码」应可用")
        # 直接勾选「显示代码」应自动获取唯一值（不用先点「获取唯一值」）
        dialog.show_code_check.setChecked(True)
        texts = [
            dialog.unique_list.item(i).text()
            for i in range(dialog.unique_list.count())
        ]
        expect(texts == ["甲(A1)"], f"显示代码应带名称和代码：{texts}")
        dialog.show_code_check.setChecked(False)
        texts = [
            dialog.unique_list.item(i).text()
            for i in range(dialog.unique_list.count())
        ]
        expect(texts == ["甲"], f"不显示代码时应只显示名称：{texts}")
        expect(
            str(dialog.unique_list.item(0).data(Qt.ItemDataRole.UserRole)) == "A1",
            "唯一值的原始代码没有保留",
        )
    finally:
        dialog.deleteLater()
        layer.deleteLater()
    return "条件查询域字段过滤 / 显示代码(名称+代码)通过"


@case
def case_local_dict_reuse(out: Path) -> str:
    """建数据落库时沿用当前库已有的字典表名与内容，不另建重复表。"""
    from app.core.domain.create_data.dict_tables import (
        DictPlan,
        build_dict_table_specs,
        create_local_dict_table,
    )

    work = out / "local_reuse"
    service, category = _dict_service(work / "dicts.db")
    target = work / "reuse.gpkg"
    _new_gpkg(target)
    # 本地字典的唯一名字是库内表名：字段挂接 / 建数据落库都按它来匹配
    create_local_dict_table(
        str(target), "本地测试字典", source_category=category.name, dict_service=service
    )
    plan = DictPlan(domains=frozenset({("本地测试字典", "域A")}))
    specs = build_dict_table_specs(str(target), plan, service)
    expect(len(specs) == 1, f"应只有 1 张表，实际 {len(specs)}")
    expect(specs[0].table_name == "本地测试字典", f"应复用库内表名：{specs[0].table_name}")
    expect(specs[0].category == "本地测试字典", f"类别应等于表名：{specs[0].category}")
    names = {row[2] for row in specs[0].rows}
    expect(names == {"甲", "乙", "丙"}, f"应从当前库字典取内容：{names}")
    return "建数据复用当前库字典表名 / 内容通过"


def cleanup(out: Path) -> None:
    shutil.rmtree(out, ignore_errors=True)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="创建数据字典落库自检")
    parser.add_argument("--out", type=Path, default=_ROOT / ".tmp" / "dict_tables")
    parser.add_argument(
        "--only", nargs="+", choices=[name for name, _fn in CASES]
    )
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
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<14} {detail}")
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
