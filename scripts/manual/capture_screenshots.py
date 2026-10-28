"""为使用手册采集当前版本界面截图。

运行（必须用 QGIS 自带 Python）::

    F:/app/qgis421/bin/python-qgis.bat scripts/manual/capture_screenshots.py

输出到 ``docs/manual_screenshots/``。脚本沿用 QA 的启动方式：绕过登录构建主窗口，
加载 ``.tmp/qa_data`` 测试数据，逐个打开面板 / 对话框并 ``QWidget.grab()``。
只打开窗口不执行写操作，不会修改测试数据。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "manual_screenshots"

sys.path.insert(0, str(ROOT / "scripts" / "qa"))
import qa_common as qa  # noqa: E402


def shot(widget, name: str) -> None:
    from qgis.PyQt.QtWidgets import QApplication

    QApplication.processEvents()
    try:
        pix = widget.grab()
        if pix.isNull():
            print(f"[SKIP] {name}: empty grab")
            return
        path = OUT / f"{name}.png"
        pix.save(str(path), "PNG")
        print(f"[OK] {path.name} {pix.width()}x{pix.height()}")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name}: {exc}")


def safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        print("[FAIL]", traceback.format_exc(limit=2))
        return None


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ctx = qa.boot(use_basemap=False, test_data=True, run="manual")
    win = ctx.window
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import QApplication
    from qgis.core import QgsProject

    # ---- 主界面与页签 ----
    shot(win, "01_主界面")
    tabs = ["文件", "查看", "编辑", "通用", "质检", "帮助"]
    for i, label in enumerate(tabs):
        win._tab_bar.setCurrentIndex(i)
        ctx.pump(180)
        shot(win, f"02_页签_{label}")

    # ---- 登录框 ----
    def login():
        from app.ui.dialogs.login import LoginDialog

        dlg = LoginDialog(None, app_name=ctx.cfg.app_display_name)
        dlg.resize(920, 560)
        dlg.show()
        ctx.pump(300)
        shot(dlg, "03_登录")
        dlg.close()

    safe(login)

    # ---- 独立面板 ----
    def dock(cls, name, w=420, h=820, expand=False):
        d = cls(None)
        d.setWindowFlag(Qt.WindowType.Window, True)
        d.resize(w, h)
        d.show()
        ctx.pump(300)
        if expand:
            tree = getattr(d, "tree", None)
            if tree is not None:
                tree.expandAll()
                ctx.pump(120)
        shot(d, name)
        d.close()

    def quality_dock():
        from app.ui.docks.quality_dock import QualityDock

        dock(QualityDock, "04_质检面板", 420, 700, True)

    def tools_dock():
        from app.ui.docks.tools_dock import ToolsDock

        dock(ToolsDock, "05_分析工具面板", 420, 860, True)

    def basemap_dock():
        # 通过主窗口入口打开，Dock 才会绑定底图清单回调
        win._on_online_basemap()
        ctx.pump(600)
        d = getattr(win, "_online_basemap_dock", None)
        if d is None:
            print("[SKIP] basemap: dock not created")
            return
        if d.isFloating():
            d.resize(360, 700)
        ctx.pump(300)
        shot(d, "07_在线底图面板")

    safe(quality_dock)
    safe(tools_dock)
    safe(basemap_dock)

    # ---- 属性表 ----
    def attr():
        from app.ui.docks.attribute_dock import AttributeDock

        layers = QgsProject.instance().mapLayersByName("QA_地块")
        if not layers:
            print("[SKIP] attr: no layer")
            return
        d = AttributeDock(layers[0], parent=None)
        d.setWindowFlag(Qt.WindowType.Window, True)
        d.resize(1100, 620)
        d.show()
        ctx.pump(500)
        shot(d, "08_属性表")
        d.close()

    safe(attr)

    # ---- 图层属性 ----
    def props():
        from app.ui.dialogs.layer_properties.dialog import LayerPropertiesDialog

        layer = QgsProject.instance().mapLayersByName("QA_地块")[0]
        d = LayerPropertiesDialog(layer, canvas=win.canvas, parent=win)
        d.resize(980, 720)
        d.show()
        ctx.pump(400)
        shot(d, "09_图层属性")
        d.close()

    safe(props)

    # ---- 数据 / 工程对话框 ----
    def dialog(name, factory, w=1000, h=680):
        d = factory()
        if d is None:
            return
        try:
            d.resize(w, h)
        except Exception:  # noqa: BLE001
            pass
        d.show()
        ctx.pump(350)
        shot(d, name)
        try:
            d.close()
        except Exception:  # noqa: BLE001
            pass

    from app.ui.dialogs.add_connection import AddConnectionDialog
    from app.ui.dialogs.add_data_dialog import AddDataDialog
    from app.ui.dialogs.about import AboutDialog
    from app.ui.dialogs.buffer import BufferDialog
    from app.ui.dialogs.condition_query import ConditionQueryDialog
    from app.ui.dialogs.create_data_dialog import CreateDataDialog
    from app.ui.dialogs.dict_manage import DictManageDialog
    from app.ui.dialogs.execute_sql import ExecuteSqlDialog
    from app.ui.dialogs.export_layer_dialog import ExportLayerDialog
    from app.ui.dialogs.imagery_mbtiles import ImageryMbtilesDialog
    from app.ui.dialogs.imagery_tile import ImageryTileDialog
    from app.ui.dialogs.qmap_compare import QMapCompareDialog
    from app.ui.dialogs.reproject import ReprojectDialog
    from app.ui.dialogs.spatial_query import SpatialQueryDialog
    from app.ui.dialogs.style_io import StyleSaveDialog
    from app.ui.dialogs.swipe_compare import SwipeCompareDialog
    from app.ui.dialogs.vector_format import VectorFormatDialog
    from app.ui.dialogs.vector_tile import VectorTileDialog
    from app.ui.docks.data_manage_dock import DataManageDialog

    safe(lambda: dialog("10_添加数据", lambda: AddDataDialog(win)))
    safe(lambda: dialog("11_创建数据", lambda: CreateDataDialog(win), 900, 720))
    safe(lambda: dialog("12_新建连接", lambda: AddConnectionDialog(win), 760, 560))
    safe(
        lambda: dialog(
            "13_创建矢量瓦片",
            lambda: VectorTileDialog(
                win, title="创建矢量瓦片", dia_type="native:writevectortiles_xyz"
            ),
            900,
            720,
        )
    )
    safe(
        lambda: dialog(
            "14_影像切片XYZ", lambda: ImageryTileDialog(win, title="影像切片"), 900, 720
        )
    )
    safe(
        lambda: dialog(
            "15_影像切片MBTiles",
            lambda: ImageryMbtilesDialog(win, title="影像切片（MBTiles）"),
            900,
            720,
        )
    )
    safe(
        lambda: dialog(
            "16_执行SQL",
            lambda: ExecuteSqlDialog(
                QgsProject.instance().mapLayersByName("QA_地块")[0], win
            ),
            980,
            720,
        )
    )

    def cond():
        layers = list(QgsProject.instance().mapLayers().values())
        return ConditionQueryDialog(layers, layers[0] if layers else None, win)

    safe(lambda: dialog("17_条件查询", cond, 980, 720))

    def spatial():
        layers = list(QgsProject.instance().mapLayers().values())
        return SpatialQueryDialog(layers, layers[0] if layers else None, win)

    safe(lambda: dialog("18_空间查询", spatial, 980, 720))

    def export():
        layer = QgsProject.instance().mapLayersByName("QA_地块")[0]
        return ExportLayerDialog(win, layer=layer, default_dir=str(ROOT / ".tmp"))

    safe(lambda: dialog("19_导出图层", export, 900, 700))
    safe(lambda: dialog("20_矢量缓冲区", lambda: BufferDialog(win, title="矢量缓冲区"), 900, 700))
    safe(lambda: dialog("21_坐标转换", lambda: ReprojectDialog(win, title="坐标转换"), 900, 700))
    safe(lambda: dialog("22_格式转换", lambda: VectorFormatDialog(win, title="格式转换"), 900, 700))
    safe(lambda: dialog("23_地图对比", lambda: QMapCompareDialog(win), 760, 600))
    safe(lambda: dialog("24_卷帘对比", lambda: SwipeCompareDialog(win), 480, 340))
    safe(
        lambda: dialog(
            "25_保存样式",
            lambda: StyleSaveDialog(QgsProject.instance().mapLayersByName("QA_地块")[0], win),
            520,
            400,
        )
    )
    safe(
        lambda: dialog(
            "26_字典管理",
            lambda: DictManageDialog(win),
            1000,
            700,
        )
    )
    safe(lambda: dialog("27_数据管理", lambda: DataManageDialog(win), 1100, 720))
    safe(
        lambda: dialog(
            "28_关于",
            lambda: AboutDialog(win, app_name=ctx.cfg.app_display_name),
            620,
            420,
        )
    )

    # ---- 流程编排 ----
    def flow():
        win._open_flow_editor()
        ctx.pump(600)
        editor = win._flow_editor_window
        if editor is not None:
            shot(editor, "29_流程编排")
            editor.close()

    safe(flow)

    # ---- 工程面板（放到最后：新建工程会清空图层） ----
    def project_dock():
        from app.ui.docks.project_dock import ProjectDock

        if getattr(win, "project_dock", None) is None:
            win.project_dock = ProjectDock(win)
            win.place_right_dock(win.project_dock)
        win._on_project_create("示例工程")
        ctx.pump(500)
        shot(win.project_dock, "06_工程面板")

    safe(project_dock)

    ctx.window.close()
    QApplication.processEvents()
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
