"""「字典管理」自检 CLI（无界面）。

验证 :mod:`app.core.domain.dict_manage` 的存储 / 层级维护 / 排序 / Excel 往返：

1. ``crud``          —— 类别与字典域、字典项增删改查、分页、搜索、改代码级联、成环拦截、
                         域/项拖拽排序落库
2. ``import-repair`` —— 导入后的父代码修复：悬空父项 / 成环节点提升为顶级
3. ``import-export`` —— 小样本 Excel（标题行 + 表头 + 重复代码）全部导入指定类别，
                         再按类别导出往返（4 列，与模板一致）
4. ``real-template`` —— 真实业务字典模板（默认桌面路径；不存在则跳过）：
                         73 个域 / 24207 条全部进入所选类别 / 最大域 20269
5. ``dialog``        —— 对话框离屏冒烟：类别下拉 / 新建类别 / 分页 / 搜索 /
                         同级/下级添加 / 域内新增 / 拖拽排序 / 状态栏不显示库路径

用法::

    E:/app/QGIS421/bin/python-qgis.bat -u scripts/check_dict_manage.py
    E:/app/QGIS421/bin/python-qgis.bat -u scripts/check_dict_manage.py --only crud import-export
    E:/app/QGIS421/bin/python-qgis.bat -u scripts/check_dict_manage.py --xls "D:/某处/业务字典.xls"

产物默认留在 ``.tmp/dict_manage/``（已 gitignore）。退出码 0 = 全部通过。
"""

from __future__ import annotations

import argparse
import os
import shutil
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

# 既有的 QGIS 装配助手（它自己带 __main__ 守卫，只借函数不跑用例）
from run_sqlite_test import init_qgis  # noqa: E402

#: 桌面上的真实模板；找不到时 real-template 用例跳过
_DEFAULT_XLS = Path.home() / "Desktop" / "业务字典-FL_GDB_DICTS_林草湿更新成果.xls"
#: 实际使用（``main`` 里由 ``--xls`` 覆盖）
_XLS_PATH = _DEFAULT_XLS

CASES: list[tuple[str, Callable[[Path], str]]] = []


def case(fn):
    CASES.append((fn.__name__.removeprefix("case_").replace("_", "-"), fn))
    return fn


def expect(condition: object, message: str) -> None:
    """自检断言：失败时抛 AssertionError，消息直接进报告。"""
    if not condition:
        raise AssertionError(message)


# --------------------------------------------------------------------- 用例


@case
def case_crud(out: Path) -> str:
    from app.core.domain.dict_manage import DictError, DictService

    db = out / "crud" / "dicts.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    service = DictService(db)

    # 类别：默认两个，可以自建，重名拦下。
    names = [c.name for c in service.list_categories()]
    expect(names == ["政区字典", "资源字典"], f"默认类别不对：{names}")
    category = service.create_category("测试类别")
    try:
        service.create_category("测试类别")
    except DictError:
        pass
    else:
        raise AssertionError("重名类别应被拒绝")

    domain = service.create_domain(category.name, "FL_SYS_ZQSJZD-CRUD-村", "测试域")
    expect(domain.item_count == 0, "新建字典域条目数应为 0")
    expect(domain.category == "测试类别", "字典域类别不对")
    try:
        service.create_domain(category.name, "FL_SYS_ZQSJZD-CRUD-村")
    except DictError:
        pass
    else:
        raise AssertionError("同类别重名字典域应被拒绝")

    a = service.add_item(domain.id, "01", "甲")
    b = service.add_item(domain.id, "02", "乙", parent_code="01")
    c = service.add_item(domain.id, "03", "丙", parent_code="02")
    expect(service.item_by_id(a.id).has_children, "父项 has_children 应为真")
    expect(not c.has_children, "叶子 has_children 应为假")
    expect(len(service.children(domain.id)) == 1, "顶级应只有 1 项")
    expect(service.subtree_count(a.id) == 3, "子树应有 3 项")

    # 分页：再挂 5 个顶级，limit=2 应正好返回 2 条且顺延。
    for index in range(5):
        service.add_item(domain.id, f"10{index}", f"批量{index}")
    page = service.children(domain.id, limit=2)
    page2 = service.children(domain.id, limit=2, offset=2)
    expect(len(page) == 2 and len(page2) == 2, "分页条数不对")
    expect({p.code for p in page}.isdisjoint({p.code for p in page2}), "分页结果重叠")

    # 搜索按代码 / 名称 / 字典域，LIKE 通配符按字面处理。
    expect(any(i.code == "02" for i in service.search(category.name, "02")), "按代码搜索失败")
    expect(any(i.name == "乙" for i in service.search(category.name, "乙")), "按名称搜索失败")
    expect(
        any(i.domain == domain.domain for i in service.search(category.name, "CRUD")),
        "按字典域搜索失败",
    )
    expect(service.search(category.name, "%") == [], "LIKE 通配符没有被转义")

    # 重复代码 / 不存在的父代码 / 挂到自己下级 → 全部拦下。
    try:
        service.add_item(domain.id, "", "空代码")
    except DictError:
        pass
    else:
        raise AssertionError("空代码应被拒绝")
    try:
        service.add_item(domain.id, "01", "重复代码")
    except DictError:
        pass
    else:
        raise AssertionError("重复代码应被拒绝")
    try:
        service.add_item(domain.id, "04", "丁", parent_code="9999")
    except DictError:
        pass
    else:
        raise AssertionError("不存在的父代码应被拒绝")
    try:
        service.update_item(c.id, parent_code="03")
    except DictError:
        pass
    else:
        raise AssertionError("挂到自己的下级应被拒绝")

    # 改代码：后代 parent_code 必须跟着改。
    service.update_item(a.id, code="010")
    expect(service.item_by_id(b.id).parent_code == "010", "改代码后后代 parent_code 没级联")
    expect(service.item_by_id(c.id).parent_code == "02", "孙级 parent_code 被误改")
    renamed = service.update_item(b.id, name="乙改")
    expect(renamed.name == "乙改", "改名失败")

    # 同级重排：按 (id, sort_order) 落库后 children 顺序跟着变。
    reorder_domain = service.create_domain(category.name, "排序域")
    r1 = service.add_item(reorder_domain.id, "R1", "一")
    r2 = service.add_item(reorder_domain.id, "R2", "二")
    r3 = service.add_item(reorder_domain.id, "R3", "三")
    service.reorder_items([(r3.id, 0), (r1.id, 1), (r2.id, 2)])
    ordered_items = [i.code for i in service.children(reorder_domain.id)]
    expect(ordered_items == ["R3", "R1", "R2"], f"同级重排不对：{ordered_items}")
    service.delete_domain(reorder_domain.id)

    # 删除子树 + 删除域。
    deleted = service.delete_item(a.id)
    expect(deleted == 3, f"删除子树应删 3 条，实际 {deleted}")
    expect(service.item_by_code(domain.id, "010") is None, "子树删除后仍能查到")
    removed = service.delete_domain(domain.id)
    expect(removed > 0, "删除域应返回删除条目数")
    expect(service.list_domains(category.name) == [], "删域后列表应为空")

    other = service.create_domain("资源字典", "FL_SYS_ZYSJZD-CRUD-类型")
    expect(service.list_domains(category.name) == [], "类别隔离失效（测试类别）")
    expect(len(service.list_domains("资源字典")) == 1, "类别隔离失效（资源字典）")
    expect(other.category == "资源字典", "类别文案不对")

    # 字典域拖拽排序。
    d1 = service.create_domain(category.name, "域A")
    d2 = service.create_domain(category.name, "域B")
    d3 = service.create_domain(category.name, "域C")
    service.reorder_domains(
        category.name, [(d3.id, 0), (d1.id, 1), (d2.id, 2)]
    )
    ordered = [d.domain for d in service.list_domains(category.name)]
    expect(ordered == ["域C", "域A", "域B"], f"字典域排序不对：{ordered}")

    # 新建域置顶（界面「新建字典域 / 同级添加字典域」用）。
    top_domain = service.create_domain(category.name, "置顶域", at_top=True)
    expect(
        service.list_domains(category.name)[0].id == top_domain.id,
        "at_top=True 没有把新字典域排在最上面",
    )

    # 跨域移动：整棵子树跟着走，根变成目标域的顶级；代码冲突整体拒绝。
    source = service.create_domain(category.name, "移动源")
    target = service.create_domain(category.name, "移动目标")
    root = service.add_item(source.id, "M1", "根")
    child_item = service.add_item(source.id, "M2", "子", parent_code="M1")
    moved_count = service.move_item_to_domain(root.id, target.id)
    expect(moved_count == 2, f"移动条数应为 2，实际 {moved_count}")
    moved_root = service.item_by_id(root.id)
    expect(moved_root.dict_id == target.id, "根项没有移到目标域")
    expect(moved_root.parent_code == "", "移动后的根应成为顶级")
    moved_child = service.item_by_id(child_item.id)
    expect(moved_child.dict_id == target.id, "下级没有跟着移动")
    expect(moved_child.parent_code == "M1", "移动后父子关系被破坏")

    conflict_source = service.create_domain(category.name, "冲突源")
    conflict_item = service.add_item(conflict_source.id, "M1", "冲突")
    try:
        service.move_item_to_domain(conflict_item.id, target.id)
    except DictError:
        pass
    else:
        raise AssertionError("目标域已有同代码项时应拒绝移动")

    # ensure_domain：存在返回 False，不存在建好返回 True。
    _, created = service.ensure_domain(category.name, "域A")
    expect(not created, "已有字典域不应重复创建")
    fresh, created = service.ensure_domain(category.name, "域D")
    expect(created and fresh.domain == "域D", "新字典域应自动创建")

    doomed = service.create_category("待删类别")
    doomed_domain = service.create_domain(doomed.name, "待删域")
    service.add_item(doomed_domain.id, "DEL1", "待删项")
    removed_items = service.delete_category(doomed.name)
    expect(removed_items == 1, f"删除字典表应删 1 条，实际 {removed_items}")
    expect(
        "待删类别" not in [c.name for c in service.list_categories()],
        "删除后类别还在",
    )
    return "字典表 / 增删改查 / 分页 / 搜索 / 级联 / 成环 / 排序 / 跨域移动 / 删字典表 通过"


@case
def case_migration(out: Path) -> str:
    """v1（固定两类 zhengqu/ziyuan）旧库 → v2（动态类别 + 排序）迁移。"""
    import sqlite3

    from app.core.domain.dict_manage import DictService

    db = out / "migration" / "dicts.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        PRAGMA user_version = 1;
        CREATE TABLE dicts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT NOT NULL CHECK (category IN ('zhengqu', 'ziyuan')),
            domain      TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE (category, domain)
        );
        CREATE TABLE dict_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            dict_id     INTEGER NOT NULL REFERENCES dicts(id) ON DELETE CASCADE,
            code        TEXT NOT NULL,
            name        TEXT NOT NULL,
            parent_code TEXT NOT NULL DEFAULT '',
            sort_order  INTEGER NOT NULL DEFAULT 0,
            UNIQUE (dict_id, code)
        );
        INSERT INTO dicts (id, category, domain, description)
            VALUES (1, 'zhengqu', '旧政区域', '旧数据'), (2, 'ziyuan', '旧资源域', '');
        INSERT INTO dict_items (dict_id, code, name, parent_code, sort_order)
            VALUES (1, '0100', '旧甲', '', 0), (1, '0101', '旧乙', '0100', 1);
        """
    )
    conn.commit()
    conn.close()

    service = DictService(db)
    categories = {c.name for c in service.list_categories()}
    expect(categories == {"政区字典", "资源字典"}, f"迁移后类别不对：{categories}")
    zhengqu = service.list_domains("政区字典")
    expect(len(zhengqu) == 1 and zhengqu[0].domain == "旧政区域", "v1 政区域没有迁过来")
    expect(zhengqu[0].item_count == 2, "v1 字典项没有迁过来")
    expect(
        service.item_by_code(zhengqu[0].id, "0101").parent_code == "0100",
        "迁移后层级丢失",
    )
    # 迁移后要能新建任意类别（旧 CHECK 约束必须已经去掉）。
    service.create_category("迁移后新类别")
    service.create_domain("迁移后新类别", "新域")
    expect(len(service.list_domains("迁移后新类别")) == 1, "迁移后不能新建类别/域")
    return "v1 → v2 迁移（类别映射 / 数据保留 / 新类别可建）通过"


@case
def case_import_repair(out: Path) -> str:
    from app.core.domain.dict_manage import DictRow, DictService

    db = out / "repair" / "dicts.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    service = DictService(db)
    service.create_category("测试类别")
    rows = [
        DictRow("测试类别", "D1", "A", "甲", "B", 1),
        DictRow("测试类别", "D1", "B", "乙", "A", 2),
        DictRow("测试类别", "D1", "C", "丙", "999", 3),
    ]
    report = service.apply_import(rows)
    expect(report.items_inserted == 3, "应插入 3 条")
    expect(report.warning_total >= 2, f"成环 + 悬空父项应各记 1 条告警，实际 {report.warning_total}")
    domain = service.list_domains("测试类别")[0]
    root_codes = {item.code for item in service.children(domain.id)}
    expect("C" in root_codes, "悬空父项的 C 应被提升为顶级")
    expect("A" in root_codes or "B" in root_codes, "成环的一侧应被提升为顶级")
    return f"悬空/成环修复通过（告警 {report.warning_total} 条）"


@case
def case_import_export(out: Path) -> str:
    from app.core.domain.dict_manage import (
        DictService,
        export_excel,
        import_excel,
    )

    work = out / "roundtrip"
    work.mkdir(parents=True, exist_ok=True)
    source = work / "source.xlsx"
    _write_raw_xlsx(
        source,
        [
            ["业务字典-测试", "", "", ""],
            ["字典域", "代码", "名称", "上级代码(仅多级字典需填写)"],
            ["D_ZQ", "0100", "甲", ""],
            ["D_ZQ", "0101", "乙", "0100"],
            ["D_ZY", "0200", "丙", ""],
            ["D_ZY", "0201", "丁", "0200"],
            ["D_ZY", "0201", "丁改", "0200"],  # 重复代码：后行覆盖
            ["D_ZQ", "0300", "悬空", "9999"],
            ["FL_SYS_ZQSJZD-X", "0400", "戊", ""],
            ["FL_SYS_ZYSJZD-Y", "0500", "己", ""],
        ],
    )

    db1 = work / "d1.db"
    if db1.exists():
        db1.unlink()
    service = DictService(db1)
    service.create_category("测试字典")
    report = import_excel(source, service=service, category="测试字典")
    expect(report.rows_total == 8, f"应读到 8 行数据，实际 {report.rows_total}")
    expect(report.rows_skipped == 0, f"不应有跳过行，实际 {report.rows_skipped}")
    expect(report.domains_created == 4, f"应新建 4 个域，实际 {report.domains_created}")
    expect(report.items_inserted == 7, f"应新增 7 条，实际 {report.items_inserted}")
    expect(report.items_updated == 1, f"重复代码应更新 1 条，实际 {report.items_updated}")
    expect(report.warning_total == 2, f"应有 2 条告警，实际 {report.warning_total}")
    expect(
        len(service.list_domains("测试字典")) == 4,
        "所有域都应落在「测试字典」类别",
    )
    expect(
        service.list_domains("资源字典") == [],
        "导入不应再按域名前缀分流到其他类别",
    )
    d_zy = {d.domain: d for d in service.list_domains("测试字典")}["D_ZY"]
    expect(service.item_by_code(d_zy.id, "0201").name == "丁改", "重复代码没有做到后行覆盖")

    # 导出 → 空库重导：条数 / 层级 / 前导零都必须保持（导出只有 4 列）。
    exported = work / "exported.xlsx"
    written, count = export_excel(exported, service=service, category="测试字典")
    expect(count == 7, f"导出应为 7 行，实际 {count}")
    expect(Path(written).is_file(), "导出文件不存在")

    db2 = work / "d2.db"
    if db2.exists():
        db2.unlink()
    service2 = DictService(db2)
    service2.create_category("测试字典")
    report2 = import_excel(written, service=service2, category="测试字典")
    expect(report2.items_inserted == 7, f"重导应新增 7 条，实际 {report2.items_inserted}")
    zq = {d.domain: d for d in service2.list_domains("测试字典")}
    item = service2.item_by_code(zq["D_ZQ"].id, "0101")
    expect(item is not None and item.parent_code == "0100", "往返后层级丢失")
    expect(service2.item_by_code(zq["D_ZQ"].id, "0100") is not None, "前导零代码 0100 丢失")

    # replace_domains：只清空文件里出现过的域，别的域不动。
    only = work / "only.xlsx"
    _write_raw_xlsx(
        only,
        [
            ["字典域", "代码", "名称", "上级代码"],
            ["D_ZQ", "9999", "只剩我", ""],
        ],
    )
    report3 = import_excel(only, service=service2, category="测试字典", replace_domains=True)
    expect(report3.domains_created == 0, "替换导入不应新建域")
    zq2 = {d.domain: d for d in service2.list_domains("测试字典")}
    expect(zq2["D_ZQ"].item_count == 1, "替换导入后 D_ZQ 应只剩 1 条")
    expect("FL_SYS_ZQSJZD-X" in zq2, "替换导入误删了别的域")
    return "小样本导入指定类别 / 重复覆盖 / 4 列导出往返 / 替换导入通过"


@case
def case_real_template(out: Path) -> str:
    from app.core.domain.dict_manage import DictService, export_excel, import_excel

    if not _XLS_PATH.is_file():
        return f"跳过：找不到真实模板 {_XLS_PATH}"
    work = out / "real"
    work.mkdir(parents=True, exist_ok=True)
    db1 = work / "real1.db"
    if db1.exists():
        db1.unlink()
    service = DictService(db1)
    service.create_category("林草湿字典")
    report = import_excel(_XLS_PATH, service=service, category="林草湿字典")
    expect(report.rows_total == 24207, f"应有 24207 行数据，实际 {report.rows_total}")
    expect(report.rows_skipped == 0, f"不应跳过行，实际 {report.rows_skipped}")
    expect(report.domains_created == 73, f"应新建 73 个域，实际 {report.domains_created}")
    expect(report.items_inserted == 24207, f"应新增 24207 条，实际 {report.items_inserted}")
    expect(report.warning_total == 0, f"真实模板不应有告警：{report.warnings[:3]}")
    domains = service.list_domains("林草湿字典")
    expect(len(domains) == 73, f"73 个域都应落在所选类别，实际 {len(domains)}")
    expect(max(d.item_count for d in domains) == 20269, "最大的域应含 20269 条")

    # 导出再导入一遍：条数 / 层级 / 前导零代码都要保持。
    written, count = export_excel(work / "real_export.xlsx", service=service, category="林草湿字典")
    expect(count == 24207, f"导出应为 24207 行，实际 {count}")
    db2 = work / "real2.db"
    if db2.exists():
        db2.unlink()
    service2 = DictService(db2)
    service2.create_category("林草湿字典")
    report2 = import_excel(written, service=service2, category="林草湿字典")
    expect(report2.items_inserted == 24207, f"往返后应新增 24207 条，实际 {report2.items_inserted}")
    domains2 = service2.list_domains("林草湿字典")
    expect(max(d.item_count for d in domains2) == 20269, "往返后最大域条目数不对")
    return "73 域 / 24207 条全部导入所选类别 / 导出往返通过"


@case
def case_dialog(out: Path) -> str:
    """离屏冒烟：三列树 / 内联编辑 / 新建域 / 添加 / 拖拽排序 / 状态栏。"""
    from qgis.PyQt.QtWidgets import QApplication, QInputDialog

    from app.core.domain.dict_manage import DictRow, DictService
    from app.ui.common import tool_icons
    from app.ui.common.message_box import MessageBox as QMessageBox
    from app.ui.dialogs.dict_manage import (
        _KIND_DOMAIN,
        _KIND_ITEM,
        _KIND_MORE,
        _KIND_RESULT,
        _ROLE_DICT_ID,
        _ROLE_ITEM_ID,
        _ROLE_KIND,
        DictManageDialog,
    )
    from app.ui.main_window.tool_bar import _TOOL_GROUPS

    # 工具条接线：按钮 key / 图标 key 必须同时存在，否则启动时 KeyError。
    expect(
        any(key == "dict_manage" for _group, items in _TOOL_GROUPS for _text, key in items),
        "工具条里没有 dict_manage 按钮",
    )
    expect(tool_icons.icon("dict_manage", "#000000", 16) is not None, "字典管理图标画不出来")

    db = out / "dialog" / "dicts.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    service = DictService(db)
    small = service.create_domain("政区字典", "FL_SYS_ZQSJZD-DLG", "小字典")
    for index in range(3):
        service.add_item(small.id, f"0{index}", f"项{index}")
    big = service.create_domain("政区字典", "FL_SYS_ZQSJZD-BIG", "大字典")
    service.apply_import(
        [
            DictRow("政区字典", big.domain, f"{i:05d}", f"大项{i}")
            for i in range(1200)
        ]
    )
    reorder = service.create_domain("政区字典", "REORDER", "排序字典")
    for code in ("R1", "R2", "R3"):
        service.add_item(reorder.id, code, f"排序{code}")

    dlg = DictManageDialog(None, service=service)
    # 新增动作会自动进入编辑态；自检里用 setText 模拟编辑，先屏蔽自动编辑。
    original_start_edit = DictManageDialog._start_edit
    DictManageDialog._start_edit = lambda self, node, column: None
    try:
        # 1) 三列树 + 类别下拉 + 新建类别。
        expect(dlg._tree.columnCount() == 3, "左侧树应为名称 / 代码 / 字典域三列")
        expect(
            [dlg._tree.headerItem().text(i) for i in range(3)]
            == ["名称", "代码", "字典域 / 说明"],
            "左侧树表头不对",
        )
        expect(dlg._category_combo.count() >= 2, "字典表下拉没有预置字典表")
        expect(
            dlg._tree.columnWidth(0) > dlg._tree.columnWidth(1),
            "名称列应比代码列宽",
        )
        original_get_text = QInputDialog.getText
        QInputDialog.getText = staticmethod(lambda *a, **k: ("界面新类别", True))
        try:
            dlg._create_category()
        finally:
            QInputDialog.getText = original_get_text
        expect(
            "界面新类别" in [c.name for c in service.list_categories()],
            "创建字典表没有新建字典表",
        )
        expect(dlg._category == "界面新类别", "新建字典表后下拉没有选中它")
        dlg._reload_categories(select="政区字典")

        def domain_node(dict_id: int):
            for index in range(dlg._tree.topLevelItemCount()):
                node = dlg._tree.topLevelItem(index)
                if node.data(0, _ROLE_DICT_ID) == dict_id:
                    return node
            return None

        # 2) 分页：500 + 「加载更多」，点一次到 1000。
        big_node = domain_node(big.id)
        dlg._tree.expandItem(big_node)
        QApplication.processEvents()
        items = [
            big_node.child(i)
            for i in range(big_node.childCount())
            if big_node.child(i).data(0, _ROLE_KIND) == _KIND_ITEM
        ]
        expect(len(items) == 500, f"大字典第一页应加载 500 条，实际 {len(items)}")
        sentinels = [
            big_node.child(i)
            for i in range(big_node.childCount())
            if big_node.child(i).data(0, _ROLE_KIND) == _KIND_MORE
        ]
        expect(len(sentinels) == 1, "大字典第一页末尾应有「加载更多」")
        dlg._on_item_clicked(sentinels[0], 0)
        items = [
            big_node.child(i)
            for i in range(big_node.childCount())
            if big_node.child(i).data(0, _ROLE_KIND) == _KIND_ITEM
        ]
        expect(len(items) == 1000, f"点加载更多后应有 1000 条，实际 {len(items)}")

        # 3) 搜索：保持「字典域 → 字典项」结构，只隐藏不匹配数据。
        dlg._search_edit.setText("大项11")
        dlg._run_search()
        visible_domains = [
            dlg._tree.topLevelItem(i)
            for i in range(dlg._tree.topLevelItemCount())
            if not dlg._tree.topLevelItem(i).isHidden()
        ]
        expect(len(visible_domains) == 1, f"应只显示命中的字典域：{len(visible_domains)}")
        expect(
            visible_domains[0].data(0, _ROLE_KIND) == _KIND_DOMAIN,
            "搜索结果应保留字典域节点",
        )
        result_node = next(
            (
                visible_domains[0].child(i)
                for i in range(visible_domains[0].childCount())
                if visible_domains[0].child(i).data(0, _ROLE_KIND) == _KIND_ITEM
            ),
            None,
        )
        expect(result_node is not None, "字典域下没有匹配的字典项")
        expect(result_node.text(1).startswith("0"), "搜索结果代码列没有填充")
        expect(result_node.text(2) != "", "搜索结果字典域列没有填充")
        # 域节点可直接选中删除（原布局的关键）：先选中再确认按钮可用
        dlg._tree.setCurrentItem(visible_domains[0])
        dlg._update_action_buttons()
        expect(dlg._btn_delete.isEnabled(), "搜索到的字典域应可删除")
        # 3b) 只命中域名（如「地貌」）：域节点保留、不加载未过滤的子项
        dlg._search_edit.setText("BIG")
        dlg._run_search()
        visible_domains = [
            dlg._tree.topLevelItem(i)
            for i in range(dlg._tree.topLevelItemCount())
            if not dlg._tree.topLevelItem(i).isHidden()
        ]
        expect(len(visible_domains) == 1, "按域名搜索应只显示该字典域")
        expect(
            visible_domains[0].data(0, _ROLE_DICT_ID) == big.id,
            "按域名搜索命中的域不对",
        )
        expect(
            visible_domains[0].childCount() > 0,
            "域名命中时应带出该域下匹配的字典项",
        )
        dlg._tree.setCurrentItem(visible_domains[0])
        dlg._update_action_buttons()
        expect(dlg._btn_delete.isEnabled(), "按域名搜索到的字典域应可删除")
        dlg._search_edit.clear()
        dlg._load_domains()

        # 4) 新建字典域 + 树内改名 / 改说明。
        dlg._on_new_domain()
        new_domain = next(
            (d for d in service.list_domains("政区字典") if d.domain.startswith("新字典域")),
            None,
        )
        expect(new_domain is not None, "新建字典域没有落库")
        expect(
            service.list_domains("政区字典")[0].id == new_domain.id,
            "新建的字典域没有排在最上面",
        )
        new_node = domain_node(new_domain.id)
        expect(new_node is not None, "新建字典域没有进树")
        new_node.setText(0, "测试新域")
        expect(
            service.domain_by_id(new_domain.id).domain == "测试新域",
            "字典域改名没有落库",
        )
        new_node.setText(2, "新说明")
        expect(
            service.domain_by_id(new_domain.id).description == "新说明",
            "字典域说明没有落库",
        )

        # 5) 选中字典域 → 下级添加 = 域内顶级项；随后全部在树里内联改名 / 改代码。
        dlg._tree.setCurrentItem(new_node)
        dlg._on_add_child()
        added = service.item_by_code(new_domain.id, "NEW1")
        expect(added is not None and added.parent_code == "", "域内新增顶级项失败")
        added_node = dlg._find_node(new_node, "NEW1")
        expect(added_node is not None, "新增项没有进树")
        added_node.setText(0, "新增项")
        expect(service.item_by_id(added.id).name == "新增项", "字典项改名没有落库")
        added_node.setText(1, "N1")
        expect(service.item_by_id(added.id).code == "N1", "字典项改代码没有落库")

        # 下级添加（挂在 N1 下）。占位代码会复用已空出来的 NEW1。
        dlg._tree.setCurrentItem(added_node)
        dlg._on_add_child()
        child_rows = service.children(new_domain.id, "N1")
        expect(len(child_rows) == 1, "下级添加失败")
        child = child_rows[0]
        expect(child.parent_code == "N1", "下级添加的上级代码不对")

        # 同级添加（N1 是顶级项，同级仍为顶级）。
        dlg._tree.setCurrentItem(added_node)
        dlg._on_add_sibling()
        sibling = service.item_by_code(new_domain.id, "NEW2")
        expect(sibling is not None and sibling.parent_code == "", "同级添加失败")
        # 同级添加会重载域分支，旧节点已失效 —— 重新取 N1。
        added_node = dlg._find_node(new_node, "N1")
        expect(added_node is not None, "重载后找不到 N1")

        # 6) 非法编辑（空名称）必须回滚，不能落库。
        original_warning = QMessageBox.warning
        QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
        try:
            added_node.setText(0, "")
        finally:
            QMessageBox.warning = original_warning
        expect(added_node.text(0) == "新增项", "空名称没有回滚到旧值")
        expect(service.item_by_id(added.id).name == "新增项", "空名称不应落库")

        # 7) 拖拽排序：把 REORDER 域最后一个子项拖到第一位，落库顺序跟着变。
        reorder_node = domain_node(reorder.id)
        dlg._tree.expandItem(reorder_node)
        QApplication.processEvents()
        moved = reorder_node.takeChild(reorder_node.childCount() - 1)
        reorder_node.insertChild(0, moved)
        dlg._persist_order(moved)
        codes = [i.code for i in service.children(reorder.id)]
        expect(codes == ["R3", "R1", "R2"], f"拖拽排序没有落库：{codes}")

        # 8) 字典域同样支持拖拽排序。
        last = dlg._tree.topLevelItem(dlg._tree.topLevelItemCount() - 1)
        moved_domain_id = int(last.data(0, _ROLE_DICT_ID))
        moved_domain_name = service.domain_by_id(moved_domain_id).domain
        taken = dlg._tree.takeTopLevelItem(dlg._tree.topLevelItemCount() - 1)
        dlg._tree.insertTopLevelItem(0, taken)
        dlg._persist_order(taken)
        expect(
            service.list_domains("政区字典")[0].domain == moved_domain_name,
            "字典域拖拽排序没有落库",
        )

        # 9) 状态栏不显示应用级库路径。
        expect(
            str(service.db_path) not in dlg._status_label.text(),
            "状态栏不应再显示字典库路径",
        )

        # 10) 删除（确认框一律 Yes）。
        # 同级添加重载过域分支，N1 的子节点要展开后才会重新加载。
        dlg._tree.expandItem(added_node)
        QApplication.processEvents()
        child_node = dlg._find_node(added_node, child.code)
        expect(child_node is not None, "删除前找不到下级项")
        dlg._tree.setCurrentItem(child_node)
        original_question = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        try:
            dlg._on_delete()
        finally:
            QMessageBox.question = original_question
        expect(
            service.item_by_code(new_domain.id, child.code) is None,
            "删除没有落库",
        )

        # 11) 字典项「字典域」列可改：整棵子树移动到另一个域。
        small_node = domain_node(small.id)
        dlg._tree.expandItem(small_node)
        QApplication.processEvents()
        item_node = dlg._find_node(small_node, "00")
        expect(item_node is not None, "找不到 small/00")
        item00 = service.item_by_code(small.id, "00")
        item_node.setText(2, "REORDER")
        QApplication.processEvents()
        moved_item = service.item_by_id(item00.id)
        expect(moved_item.domain == "REORDER", "树内改字典域没有落库")
        expect(moved_item.parent_code == "", "移动后应成为目标域的顶级")

        # 12) 搜索结果里同样能改字典域。
        dlg._search_edit.setText("项0")
        dlg._run_search()
        result = None
        for index in range(dlg._tree.topLevelItemCount()):
            domain_node_item = dlg._tree.topLevelItem(index)
            if domain_node_item.isHidden():
                continue
            for child_index in range(domain_node_item.childCount()):
                candidate = domain_node_item.child(child_index)
                if candidate.data(0, _ROLE_ITEM_ID) == item00.id:
                    result = candidate
                    break
            if result is not None:
                break
        expect(result is not None, "搜索结果里找不到移动过的项")
        result.setText(2, "FL_SYS_ZQSJZD-DLG")
        QApplication.processEvents()
        expect(
            service.item_by_id(item00.id).domain == "FL_SYS_ZQSJZD-DLG",
            "搜索结果里改字典域没有落库",
        )
        dlg._search_edit.clear()
        dlg._load_domains()

        # 13) 字典域上点「同级添加」= 新建另一个字典域，且排在最上面。
        before = len(service.list_domains("政区字典"))
        dlg._tree.setCurrentItem(domain_node(reorder.id))
        dlg._on_add_sibling()
        expect(
            len(service.list_domains("政区字典")) == before + 1,
            "字典域的同级添加没有新建字典域",
        )
        expect(
            service.list_domains("政区字典")[0].domain.startswith("新字典域"),
            "同级添加的新字典域没有置顶",
        )
    finally:
        DictManageDialog._start_edit = original_start_edit
        dlg.close()
        dlg.deleteLater()
        QApplication.processEvents()
    return "三列树/内联编辑/新建域/同级下级添加/拖拽排序/状态栏通过"


def _write_raw_xlsx(path: Path, rows: list[list[str]]) -> None:
    """写一份「原始行」Excel（不设业务字段名），用于模拟用户手工填的模板。"""
    from qgis.core import (
        QgsCoordinateTransformContext,
        QgsFeature,
        QgsField,
        QgsVectorFileWriter,
        QgsVectorLayer,
    )
    from qgis.PyQt.QtCore import QVariant

    width = max(len(row) for row in rows)
    layer = QgsVectorLayer("None", "字典表", "memory")
    layer.dataProvider().addAttributes(
        [QgsField(f"Field{i + 1}", QVariant.String, "string", 255, 0) for i in range(width)]
    )
    layer.updateFields()
    features = []
    for row in rows:
        values = list(row) + [""] * (width - len(row))
        feature = QgsFeature(layer.fields())
        feature.setAttributes(values)
        features.append(feature)
    layer.dataProvider().addFeatures(features)

    if path.exists():
        path.unlink()
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "XLSX"
    options.layerName = "字典表"
    options.fileEncoding = "UTF-8"
    error, errmsg, _new, _saved = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, str(path), QgsCoordinateTransformContext(), options
    )
    if error != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError(f"写测试 Excel 失败：{errmsg}")


def cleanup(out: Path) -> None:
    try:
        shutil.rmtree(out, ignore_errors=True)
        return
    except OSError:
        pass
    print(f"清理 {out} 失败（不影响自检结果）")


def main() -> int:
    parser = argparse.ArgumentParser(description="字典管理自检")
    parser.add_argument(
        "--out",
        type=Path,
        default=_ROOT / ".tmp" / "dict_manage",
        help="产物输出目录（默认 .tmp/dict_manage）",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        choices=[name for name, _fn in CASES],
        help="只跑指定用例（默认全跑）",
    )
    parser.add_argument("--clean", action="store_true", help="跑完删除产物")
    parser.add_argument(
        "--xls",
        type=Path,
        default=_DEFAULT_XLS,
        help="真实业务字典模板路径（默认桌面上的那份）",
    )
    args = parser.parse_args()

    global _XLS_PATH
    _XLS_PATH = args.xls

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
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<16} {detail}")
    failed = [name for name, ok, _detail in results if not ok]
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
