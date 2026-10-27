"""「未登记图层」发现 / 登记 / 数据管理接入自检（无界面）。

覆盖 :mod:`app.core.domain.local_layer_register` 与数据管理 Dock 的接线：

1. ``scan-gpkg``      —— OGR 写出的 GPKG 两层都能被发现（几何 / 坐标系可读）
2. ``register-gpkg``  —— 登记一层后数据管理可见，重扫只剩另一层；登记全部后清空
3. ``idempotent``     —— 重复登记同名表只保留一行注册
4. ``scan-sqlite``    —— 普通 SQLite / SpatiaLite 同样能发现并登记
5. ``spatial-only``   —— 纯属性表（无 geometry_columns）不被当成矢量图层
6. ``invalid``        —— 不存在的文件返回空 / 登记抛 LocalRegisterError
7. ``dock-connect``   —— 新建连接带 ``register_unregistered`` 时自动登记；
                        不带时缓存未登记清单，右键菜单出现「登记未登记图层…」
8. ``dialog-flag``    —— 新建连接弹窗扫描到未登记图层时默认勾选，连接 dict 带标记

用法::

    PYTHONPATH="E:/2026/project/qgis" "F:/app/qgis421/bin/python-qgis.bat" scripts/check_unregistered_adoption.py
"""

from __future__ import annotations

import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

import _bootstrap_path  # noqa: F401

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parents[1]

from run_sqlite_test import init_qgis, make_layer, save_layer  # noqa: E402

CASES: list[tuple[str, Callable[[Path], str]]] = []


def case(fn):
    CASES.append((fn.__name__.removeprefix("case_").replace("_", "-"), fn))
    return fn


def expect(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _gpkg(out: Path, name: str, layers: tuple[str, ...] = ("layer_a",)) -> Path:
    target = out / "gpkg" / f"{name}.gpkg"
    target.parent.mkdir(parents=True, exist_ok=True)
    for index, layer_name in enumerate(layers):
        save_layer(
            make_layer(layer_name),
            target,
            "GPKG",
            layer_name,
            append=index > 0,
        )
    return target


def _sqlite(out: Path, name: str, layers: tuple[str, ...] = ("layer_a",)) -> Path:
    target = out / "sqlite" / f"{name}.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    for index, layer_name in enumerate(layers):
        save_layer(
            make_layer(layer_name),
            target,
            "SQLite",
            layer_name,
            append=index > 0,
        )
    return target


def _find(path: Path):
    from app.core.domain.local_layer_register import (
        find_unregistered_spatial_layers,
    )

    return find_unregistered_spatial_layers(path)


def _register(path: Path, names: list[str]) -> int:
    from app.core.domain.local_layer_register import register_spatial_layers

    return register_spatial_layers(path, names)


def _spatial(path: Path) -> list[str]:
    from app.core.util.local_db_util import describe_local_db

    return list(describe_local_db(str(path))["categories"]["spatial"])


def _relation_rows(path: Path, table: str) -> int:
    import sqlite3

    conn = sqlite3.connect(str(path))
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM user_relation WHERE table_name = ?", (table,)
            ).fetchone()[0]
        )
    finally:
        conn.close()


@case
def case_scan_gpkg(out: Path) -> str:
    target = _gpkg(out, "scan", ("layer_a", "layer_b"))
    found = _find(target)
    expect([item.name for item in found] == ["layer_a", "layer_b"], f"发现结果不对：{found}")
    for item in found:
        expect(item.geometry and item.geometry != "几何", f"几何类型没读出来：{item}")
        expect("4490" in item.crs, f"坐标系没读出来：{item}")
    expect(_spatial(target) == [], "还没登记就出现在数据管理里了")
    return f"发现 2 层：{[item.name for item in found]}，几何={found[0].geometry}"


@case
def case_register_gpkg(out: Path) -> str:
    target = _gpkg(out, "register", ("layer_a", "layer_b"))
    expect(_register(target, ["layer_a"]) == 1, "登记 layer_a 失败")
    expect(_spatial(target) == ["layer_a"], f"数据管理可见内容不对：{_spatial(target)}")
    remaining = [item.name for item in _find(target)]
    expect(remaining == ["layer_b"], f"登记后重扫不对：{remaining}")
    expect(_register(target, ["layer_b"]) == 1, "登记 layer_b 失败")
    expect(set(_spatial(target)) == {"layer_a", "layer_b"}, "两层没都登记上")
    expect(_find(target) == [], "全部登记后仍有未登记图层")
    return f"先登记一层再补另一层，spatial={sorted(_spatial(target))}"


@case
def case_idempotent(out: Path) -> str:
    target = _gpkg(out, "idem", ("layer_a",))
    _register(target, ["layer_a"])
    _register(target, ["LAYER_A"])  # 大小写不同也算同一张表
    rows = _relation_rows(target, "layer_a")
    expect(rows == 1, f"重复登记出现 {rows} 行")
    expect(_spatial(target) == ["layer_a"], "重复登记后目录不对")
    return "重复登记保持 1 行注册"


@case
def case_scan_sqlite(out: Path) -> str:
    target = _sqlite(out, "scan", ("layer_a", "layer_b"))
    found = _find(target)
    expect({item.name for item in found} == {"layer_a", "layer_b"}, f"发现结果不对：{found}")
    _register(target, [item.name for item in found])
    spatial = {name.lower() for name in _spatial(target)}
    expect(spatial == {"layer_a", "layer_b"}, f"登记后数据管理可见内容不对：{_spatial(target)}")
    expect(_find(target) == [], "SQLite 全部登记后仍有未登记图层")
    return f"SQLite 两层已登记：{sorted(_spatial(target))}"


@case
def case_spatial_only(out: Path) -> str:
    import sqlite3

    target = _gpkg(out, "spatial_only", ("layer_a",))
    conn = sqlite3.connect(str(target))
    try:
        conn.execute("CREATE TABLE plain_attr (name TEXT, code INTEGER)")
        conn.commit()
    finally:
        conn.close()
    found = [item.name for item in _find(target)]
    expect(found == ["layer_a"], f"纯属性表被当成矢量图层：{found}")
    return "纯属性表未列入未登记矢量图层"


@case
def case_invalid(out: Path) -> str:
    from app.core.domain.local_layer_register import (
        LocalRegisterError,
        register_spatial_layers,
    )

    missing = out / "missing" / "nope.gpkg"
    expect(_find(missing) == [], "不存在的文件不应返回图层")
    target = _gpkg(out, "invalid", ("layer_a",))
    expect(_register(target, ["no_such_table"]) == 0, "非空间表不应被登记")
    bogus = out / "invalid" / "broken.gpkg"
    bogus.parent.mkdir(parents=True, exist_ok=True)
    bogus.write_bytes(b"not a database")
    # 坏文件：读元数据失败视为「没有未登记图层」，登记也安全地返回 0
    expect(_find(bogus) == [], "坏文件不应返回图层")
    try:
        expect(register_spatial_layers(bogus, ["layer_a"]) == 0, "坏文件不应登记成功")
    except LocalRegisterError:
        pass  # 打不开库抛错同样可以接受
    return "缺失 / 坏文件 / 非空间表都被安全拒绝"


@case
def case_dock_connect(out: Path) -> str:
    from app.core.util.local_db_util import describe_local_db
    from app.ui.docks.data_manage_dock import DataManageDock

    dock = DataManageDock(None)
    try:
        auto = _gpkg(out, "dock_auto", ("layer_a", "layer_b"))
        catalog = describe_local_db(str(auto))
        dock.add_connection(
            {
                "kind": "local",
                "name": "auto",
                "path": str(auto),
                "categories": catalog["categories"],
                "counts": catalog["counts"],
                "meta": catalog["meta"],
                "register_unregistered": True,
            }
        )
        live = dock._databases[0]
        expect(
            set(live["categories"]["spatial"]) == {"layer_a", "layer_b"},
            f"连接时自动登记没生效：{live['categories']['spatial']}",
        )
        expect(not live.get("unregistered"), "自动登记后仍有未登记缓存")
        expect(
            not any("登记未登记图层" in a.text() for a in dock._build_context_menu(
                {"db": live}
            ).actions()),
            "已全部登记却仍显示登记菜单",
        )

        manual = _gpkg(out, "dock_manual", ("layer_a",))
        catalog = describe_local_db(str(manual))
        dock.add_connection(
            {
                "kind": "local",
                "name": "manual",
                "path": str(manual),
                "categories": catalog["categories"],
                "counts": catalog["counts"],
                "meta": catalog["meta"],
            }
        )
        live = dock._databases[0]
        expect(live["categories"]["spatial"] == [], "未勾选时不应自动登记")
        expect(
            [item.name for item in live.get("unregistered") or []] == ["layer_a"],
            f"未登记缓存不对：{live.get('unregistered')}",
        )
        # 走真实树节点 payload（QVariantMap 往返），确认对象化清单能带过去
        from app.ui.docks.data_manage_dock import _ROLE

        payload = dock.tree.topLevelItem(0).child(0).data(0, _ROLE)
        expect(isinstance(payload, dict), f"树节点 payload 不对：{payload!r}")
        expect(
            [item.name for item in payload["db"].get("unregistered") or []]
            == ["layer_a"],
            f"树节点 payload 里的未登记清单丢了：{payload['db'].get('unregistered')!r}",
        )
        labels = [a.text() for a in dock._build_context_menu(payload).actions()]
        expect(
            any(text.startswith("登记未登记图层…") for text in labels),
            f"右键菜单缺少登记入口：{labels}",
        )
    finally:
        dock.deleteLater()
    return "连接时自动登记 / 右键入口两种情况都正确"


@case
def case_dialog_flag(out: Path) -> str:
    from app.ui.dialogs.add_connection import AddConnectionDialog

    target = _gpkg(out, "dialog", ("layer_a", "layer_b"))
    dialog = AddConnectionDialog(None)
    try:
        expect(dialog.set_sqlite_file(str(target)), "文件校验失败")
        check = dialog.register_unregistered_check
        expect(
            check.isVisibleTo(dialog) and check.isChecked(),
            "未登记图层勾选框没默认勾上",
        )
        expect("2 个未登记" in check.text(), f"勾选框文案不对：{check.text()}")
        conn = dialog.connection()
        expect(
            conn is not None and conn.get("register_unregistered") is True,
            f"连接 dict 没带登记标记：{conn}",
        )
        check.setChecked(False)
        conn = dialog.connection()
        expect(
            conn is not None and "register_unregistered" not in conn,
            "取消勾选后仍带登记标记",
        )
        check.setChecked(True)
        conn = dialog.connection()
        expect(conn is not None, "连接 dict 为空")
    finally:
        dialog.deleteLater()

    from app.ui.docks.data_manage_dock import DataManageDock

    dock = DataManageDock(None)
    try:
        dock.add_connection(conn)
        live = dock._databases[0]
        expect(
            set(live["categories"]["spatial"]) == {"layer_a", "layer_b"},
            f"弹窗连接后未登记图层没进数据管理：{live['categories']['spatial']}",
        )
    finally:
        dock.deleteLater()
    return "弹窗勾选 → 连接 dict 标记 → 入树时完成登记"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="未登记图层登记自检")
    parser.add_argument("--out", type=Path, default=_ROOT / ".tmp" / "unregistered_adoption")
    parser.add_argument("--only", nargs="+", choices=[name for name, _fn in CASES])
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
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    raise SystemExit(main())
