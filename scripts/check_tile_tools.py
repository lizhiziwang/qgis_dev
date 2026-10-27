"""瓦片工具完整自检（无界面）。

覆盖本轮实现：

1. ``formats``              —— ``.mbtiles`` 被识别为栅格、有格式名与筛选器
2. ``mbtiles-load``         —— 合成 MBTiles 能被 GDAL provider 打开
3. ``file-route``           —— 文件浏览器的 ``.mbtiles`` 走栅格加载分支
4. ``tracker``              —— 元瓦片进度日志解析（英文 / 中文 / QGIS 4.2 译文 / 反语序）+ 已用时间
5. ``tracker-ui``           —— XYZ / MBTiles 对话框状态栏、忙碌进度、日志框
6. ``vector-viewer``        —— 矢量瓦片网页查看器（离线资源、无 CDN、node 语法检查）
7. ``vector-inspect``       —— MVT 图层名 / 要素数 / 零要素检测
8. ``viewer-server``        —— 本机 HTTP 服务（index / Leaflet / pbf 及 MIME）
9. ``vector-task``          —— 矢量瓦片任务真实产出 pbf / 进度信号
10. ``vector-cancel``       —— 取消标志传到算法反馈
11. ``vector-dialog``       —— 必填校验、输出路径、范围、任务参数
12. ``vector-defaults``     —— 默认最大等级 14 及提示
13. ``vector-size-warn``    —— 大范围任务执行前按瓦片数确认
14. ``vector-empty``        —— 无要素 pbf 的警告与查看器禁用
15. ``vector-stale``        —— 工程删图层后刷新列表不崩溃
16. ``vector-queue``        —— 批量队列、总进度、成功状态、查看器
17. ``vector-batch``        —— 批量弹窗选择、子目录清洗 / 去重
18. ``mbtiles-preview``     —— MBTiles 瓦片解包预览页（离线资源 + node 语法检查）
19. ``imagery-isolation``   —— 切片临时隐藏其他可见图层（并发安全），结束后恢复
20. ``imagery-extent``      —— 全球范围内缩到墨卡托边界（避免负数/越界瓦片号）
21. ``imagery-worker``      —— 子进程切片：临时工程另存、进度/日志转发、主线程保持响应
22. ``imagery-worker-cancel`` —— 取消子进程切片：进程被终止、结果记为已取消
23. ``imagery-close-prompt`` —— 关闭程序时有影像切片在跑：弹窗确认、可取消关闭

用法::

    PYTHONPATH="E:/2026/project/qgis" \
        "F:/app/qgis421/bin/python-qgis.bat" -u scripts/check_tile_tools.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

import _bootstrap_path  # noqa: F401

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parents[1]

from run_sqlite_test import init_qgis, make_layer  # noqa: E402

CASES: list[tuple[str, Callable[[Path], str]]] = []


def case(fn):
    CASES.append((fn.__name__.removeprefix("case_").replace("_", "-"), fn))
    return fn


def expect(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


# --------------------------------------------------------------- 公共工具


def _fresh_dir(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def _reset_project() -> None:
    from qgis.core import QgsProject

    QgsProject.instance().removeAllMapLayers()


def _add_layer(name: str = "cities"):
    from qgis.core import QgsProject

    layer = make_layer(name)
    QgsProject.instance().addMapLayer(layer)
    return layer


def _extent(layer):
    rect = layer.extent()
    return (
        rect.xMinimum(),
        rect.xMaximum(),
        rect.yMinimum(),
        rect.yMaximum(),
        layer.crs().authid(),
    )


def _varint(value: int) -> bytes:
    """protobuf varint 编码（测试用最小实现）。"""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _fake_pbf(name: str = "test", features: int = 1) -> bytes:
    """造一个最小的 MVT 瓦片：一个图层 + ``features`` 个空要素。"""
    layer = b"\x0a" + _varint(len(name)) + name.encode("utf-8")
    layer += b"\x12\x00" * max(0, int(features))
    layer += b"\x78\x02"  # version = 2
    layer += b"\x28\x0a"  # extent = 10
    return b"\x1a" + _varint(len(layer)) + layer


def _make_mbtiles(path: Path) -> Path:
    """造一个可被 GDAL 打开的最小 MBTiles（单张 256×256 PNG，0 级）。"""
    from qgis.PyQt.QtCore import QBuffer, QByteArray
    from qgis.PyQt.QtGui import QColor, QImage

    path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        candidate.unlink(missing_ok=True)

    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(QColor(20, 80, 160, 255))
    data = QByteArray()
    buf = QBuffer(data)
    expect(buf.open(QBuffer.OpenModeFlag.WriteOnly), "QBuffer 打不开")
    expect(image.save(buf, "PNG"), "测试 PNG 编码失败")
    buf.close()

    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        con.execute(
            "CREATE TABLE tiles ("
            "zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)"
        )
        meta = {
            "name": path.stem,
            "format": "png",
            "bounds": "113.0,23.0,121.0,40.0",
            "minzoom": "0",
            "maxzoom": "0",
            "type": "baselayer",
            "version": "1",
            "description": "check_tile_tools synthetic",
        }
        con.executemany("INSERT INTO metadata VALUES (?,?)", meta.items())
        con.execute("INSERT INTO tiles VALUES (0,0,0,?)", (bytes(data),))
        con.commit()
    finally:
        con.close()
    return path


def _find_node() -> str | None:
    """定位 node：QGIS 启动脚本会重置 PATH，所以还要看 NVM / 常见安装目录。"""
    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        return found
    bases = [
        os.environ.get("NODE_EXE"),
        os.environ.get("NVM_SYMLINK"),
        os.environ.get("ProgramFiles", "") + r"\nodejs",
        os.environ.get("LOCALAPPDATA", "") + r"\Programs\nodejs",
        r"C:\Program Files\nodejs",
        r"C:\Program Files (x86)\nodejs",
        r"E:\nvm\nodejs",
        r"C:\nvm\nodejs",
    ]
    for base in bases:
        if not base:
            continue
        candidate = Path(base)
        if candidate.is_file():
            return str(candidate)
        for name in ("node.exe", "node"):
            if (candidate / name).is_file():
                return str(candidate / name)
    return None


def _node_check_script(html_path: Path, marker: str) -> None:
    """抽出内联脚本交给 ``node --check``，防止模板拼接出语法错误。"""
    node = _find_node()
    expect(node is not None, "环境里找不到 node，无法检查查看器脚本语法")
    text = html_path.read_text(encoding="utf-8")
    marker_at = text.find(marker)
    expect(marker_at >= 0, f"查看器里找不到脚本标记 {marker!r}")
    start = text.rfind("<script>", 0, marker_at)
    expect(start >= 0, "找不到内联 <script> 开始标签")
    start += len("<script>")
    end = text.find("</script>", start)
    expect(end > start, "找不到内联 </script>")
    script = text[start:end]
    script_path = html_path.with_suffix(".check.js")
    script_path.write_text(script, encoding="utf-8")
    try:
        proc = subprocess.run(
            [node, "--check", str(script_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    finally:
        script_path.unlink(missing_ok=True)
    expect(proc.returncode == 0, f"查看器脚本语法错误：\n{proc.stderr}")


def _dispose_dialog(dialog) -> None:
    """断开工程信号并关闭对话框（实例在工具树里会被复用，测试中不留下监听器）。"""
    project = getattr(dialog, "project", None)
    if project is not None:
        for signal in (project.layersAdded, project.layersRemoved):
            try:
                signal.disconnect(dialog._on_project_layers_changed)
            except (TypeError, RuntimeError):
                pass
    dialog.close()
    dialog.deleteLater()


# --------------------------------------------------------------- 格式 / 加载


@case
def case_formats(out: Path) -> str:
    from app.core.util.data_formats import FORMAT_LABELS, RASTER_EXTS, suffix_of
    from app.ui.dialogs.add_data_dialog import _FORMAT_FILTERS, AddDataDialog

    expect(".mbtiles" in RASTER_EXTS, ".mbtiles 没被当栅格")
    expect(FORMAT_LABELS.get(".mbtiles") == "MBTiles 瓦片包", "格式名缺失")
    fmt = [item for item in _FORMAT_FILTERS if item[1] == "fmt:mbtiles"]
    expect(len(fmt) == 1 and fmt[0][2] == frozenset({".mbtiles"}), "缺少 MBTiles 筛选器")
    path = str(out / "demo.MBTILES")
    expect(suffix_of(path) == ".mbtiles", "扩展名大小写归一化失败")
    expect(AddDataDialog._passes_filter(path, "raster", "fmt:mbtiles"), "MBTiles 筛选不通过")
    expect(not AddDataDialog._passes_filter(path, "raster", "fmt:tif"), "被 TIFF 筛选误选")
    return "RASTER_EXTS / 格式名 / 添加数据筛选器 OK"


@case
def case_mbtiles_load(out: Path) -> str:
    from qgis.core import QgsRasterLayer

    path = _make_mbtiles(out / "load" / "synthetic.mbtiles")
    layer = QgsRasterLayer(str(path), "synthetic", "gdal")
    expect(layer.isValid(), f"GDAL 打不开合成 MBTiles：{layer.error().summary() if hasattr(layer, 'error') else ''}")
    expect(layer.bandCount() >= 3, f"波段数不对：{layer.bandCount()}")
    expect(layer.crs().authid() == "EPSG:3857", f"坐标系不对：{layer.crs().authid()}")
    expect(layer.extent().width() > 0 and layer.extent().height() > 0, "范围为空")
    return f"QgsRasterLayer(gdal) valid，{layer.bandCount()} 波段，{layer.crs().authid()}"


@case
def case_file_route(out: Path) -> str:
    from app.ui.main_window.mixins.file_browser import FileBrowserMixin

    mbtiles = _make_mbtiles(out / "route" / "route.mbtiles")

    class Canvas:
        refreshed = False

        def refreshAllLayers(self):
            self.refreshed = True

    class FakeWindow:
        def __init__(self):
            self.canvas = Canvas()
            self.raster = None
            self.vector = None
            self.began = False
            self.ended = False

        def _confirm_large_text_vector(self, _path):
            return True

        def _begin_layer_loading(self, _message):
            self.began = True

        def _end_layer_loading(self):
            self.ended = True

        def _load_raster(self, path):
            self.raster = path
            return object()

        def _load_vector(self, path):
            self.vector = path
            return object()

        def _load_container(self, path):
            raise AssertionError("mbtiles 不应走容器分支")

    fake = FakeWindow()
    FileBrowserMixin.load_file(fake, str(mbtiles))
    expect(fake.raster == str(mbtiles), f".mbtiles 没走栅格加载：raster={fake.raster}")
    expect(fake.vector is None, ".mbtiles 被误当矢量")
    expect(fake.began and fake.ended, "加载状态开始/结束没成对调用")
    expect(fake.canvas.refreshed, "加载后没刷新画布")
    return "FileBrowserMixin.load_file → _load_raster"


# --------------------------------------------------------------- 进度文字


@case
def case_tracker(out: Path) -> str:
    from app.ui.common.tile_progress import MetatileProgressTracker

    tracker = MetatileProgressTracker()
    expect(tracker.describe(0) == "正在准备瓦片…", f"空计划文案不对：{tracker.describe(0)}")
    expect(
        tracker.feed_log("INFO", "1 metatiles (2 tiles) will be created for zoom level 8"),
        "英文单级计划没识别",
    )
    expect(
        tracker.feed_log("INFO", "6 metatiles (121 tiles) will be created for zoom level 9"),
        "英文第二级计划没识别",
    )
    expect(
        tracker.feed_log("INFO", "A total of 7 metatiles (123 tiles) will be created"),
        "英文合计没识别",
    )
    expect(tracker.total_metatiles == 7 and tracker.total_tiles == 123, "合计解析不对")
    expect([z for z, _n in tracker.zoom_plan] == [8, 9], f"缩放计划不对：{tracker.zoom_plan}")
    expect(tracker.zoom_for(0) == 8 and tracker.zoom_for(1) == 8, "第 8 级归属不对")
    expect(tracker.zoom_for(2) == 9, "第 9 级归属不对")
    text = tracker.describe(43)
    expect(text == "元瓦片 3/7 · 缩放级别 9 · 43%", f"进度文案不对：{text}")
    expect("共 7 个元瓦片" in tracker.tooltip() and "123 个瓦片" in tracker.tooltip(), "tooltip 不对")

    tracker.reset()
    tracker.feed_log("INFO", "将为缩放级别 8 创建 1 个元瓦片（2 个瓦片）")
    tracker.feed_log("INFO", "总共将创建 7 个元瓦片（123 个瓦片）")
    expect(tracker.total_metatiles == 7, f"中文合计没识别：{tracker.total_metatiles}")
    expect(tracker.zoom_plan == [(8, 1)], f"中文单级计划没识别：{tracker.zoom_plan}")

    # QGIS 4.2 简体中文的实际译文（无空格、无「个」、用「切片」）
    tracker.reset()
    expect(
        tracker.feed_log("INFO", "将为缩放级别0创建1元切片 (1切片)"),
        "QGIS 4.2 中文单级日志没识别",
    )
    expect(
        tracker.feed_log("INFO", "将共创建5元切片 (5切片)"),
        "QGIS 4.2 中文合计日志没识别",
    )
    expect(tracker.total_metatiles == 5, f"QGIS 4.2 合计不对：{tracker.total_metatiles}")
    expect(tracker.total_tiles == 5, f"QGIS 4.2 瓦片数不对：{tracker.total_tiles}")
    expect(tracker.zoom_plan == [(0, 1)], f"QGIS 4.2 缩放计划不对：{tracker.zoom_plan}")
    expect(tracker.describe(0) == "元瓦片 0/5 · 缩放级别 0 · 0%", f"QGIS 4.2 文案不对：{tracker.describe(0)}")

    tracker.reset()
    expect(
        tracker.feed_log("INFO", "1 个元瓦片（2 个瓦片）将创建于缩放级别 10"),
        "中文反语序没识别",
    )
    expect(tracker.zoom_plan == [(10, 1)], f"反语序计划不对：{tracker.zoom_plan}")

    # 耗时显示：首个元瓦片可能渲染数分钟，状态要带「已用」
    expect(tracker.format_elapsed(0) == "00:00", "00:00 格式化不对")
    expect(tracker.format_elapsed(75) == "01:15", "01:15 格式化不对")
    expect(tracker.format_elapsed(3723) == "1:02:03", "小时格式化不对")
    expect("已用" not in tracker.describe(0), "未开始计时不应显示已用时间")
    tracker.start_clock()
    expect("已用 00:0" in tracker.describe(0), f"计时文案不对：{tracker.describe(0)}")
    tracker.stop_clock()
    expect("已用" not in tracker.describe(0), "停止计时后仍显示已用时间")

    tracker.reset()
    expect(tracker.total_metatiles == 0, "reset 没清空")
    expect(tracker.feed_log("INFO", "开始渲染") is False, "普通日志被误判为计划")
    return "英文 / 中文 / QGIS 4.2 译文 / 反语序日志 → 元瓦片 n/m；已用时间格式正确"


@case
def case_tracker_ui(out: Path) -> str:
    from app.ui.dialogs.imagery_mbtiles import ImageryMbtilesDialog
    from app.ui.dialogs.imagery_tile import ImageryTileDialog

    for cls, label in ((ImageryTileDialog, "XYZ"), (ImageryMbtilesDialog, "MBTiles")):
        dialog = cls(None, f"影像切片（{label}）")
        try:
            dialog._apply_progress(0)
            expect("正在准备瓦片" in dialog.status_label.text(), f"{label} 空计划状态不对")
            dialog._on_task_log(
                "INFO", "1 metatiles (2 tiles) will be created for zoom level 8"
            )
            dialog._on_task_log(
                "INFO", "A total of 7 metatiles (123 tiles) will be created"
            )
            dialog._apply_progress(43)
            status = dialog.status_label.text()
            expect(
                "元瓦片 3/7" in status and "缩放级别 8" in status and "43%" in status,
                f"{label} 状态栏不对：{status}",
            )
            expect("共 7 个元瓦片" in dialog.status_label.toolTip(), f"{label} tooltip 不对")
            log_text = dialog.log_browser.toPlainText()
            expect("A total of 7 metatiles" in log_text, f"{label} 日志框没显示算法日志")
            dialog._on_task_log("INFO", "普通日志一行")
            expect("普通日志一行" in dialog.log_browser.toPlainText(), f"{label} 普通日志丢失")

            # 首个元瓦片完成前进度条保持忙碌动画，收到非零进度才切成百分比
            dialog.loading.setRange(0, 0)
            dialog._apply_progress(0)
            expect(dialog.loading.maximum() == 0, f"{label} 0% 时不该切回百分比进度条")
            dialog._apply_progress(20)
            expect(dialog.loading.maximum() == 100 and dialog.loading.value() == 20,
                   f"{label} 非零进度没切换成百分比")

            # QGIS 4.2 实际译文也要更新状态（用户报告「一直停在正在准备瓦片」）
            dialog._progress.reset()
            dialog._progress.start_clock()
            dialog._current_progress = 0
            dialog.loading.setValue(0)
            dialog._on_task_log("INFO", "将为缩放级别0创建1元切片 (1切片)")
            dialog._on_task_log("INFO", "将共创建5元切片 (5切片)")
            status = dialog.status_label.text()
            expect("元瓦片 0/5" in status and "缩放级别 0" in status,
                   f"{label} QGIS 4.2 日志没转成进度：{status}")
            expect("已用" in status, f"{label} 状态栏缺少已用时间：{status}")
            dialog._progress.stop_clock()
        finally:
            _dispose_dialog(dialog)
    return "两个切片对话框显示元瓦片计划/忙碌进度/已用时间，日志框保留完整日志"


@case
def case_imagery_xyz_e2e(out: Path) -> str:
    """真实线程跑完 XYZ 切片，并检查对话框的进度/日志/完成/查看器。"""
    from qgis.core import QgsRasterLayer
    from qgis.PyQt.QtCore import QEventLoop, QTimer

    from app.ui.dialogs.imagery_tile import ImageryTileDialog

    _reset_project()
    raster = QgsRasterLayer(str(_make_mbtiles(out / "xyz_e2e" / "source.mbtiles")), "source", "gdal")
    expect(raster.isValid(), "测试栅格打不开")
    from qgis.core import QgsProject

    QgsProject.instance().addMapLayer(raster)
    extra = make_layer("overlay")
    QgsProject.instance().addMapLayer(extra)
    target = _fresh_dir(out / "xyz_e2e" / "tiles")

    dialog = ImageryTileDialog(None, "影像切片")
    try:
        dialog._populate_layers()
        index = next(
            (
                row
                for row in range(dialog.input_combo.count())
                if dialog.input_combo.itemData(row) is not None
                and dialog.input_combo.itemData(row).id() == raster.id()
            ),
            -1,
        )
        expect(index >= 0, "图层下拉框里找不到测试栅格")
        dialog.input_combo.setCurrentIndex(index)
        dialog.min_zoom_spin.setValue(0)
        dialog.max_zoom_spin.setValue(1)
        dialog.viewer_check.setChecked(True)
        dialog.output_edit.setText(str(target))

        loop = QEventLoop()
        dialog._on_execute()
        expect(dialog.task is not None, "XYZ 对话框没有启动任务")
        from app.core.domain.imagery_tiles.active_tasks import active_descriptions

        running = active_descriptions()
        expect(
            len(running) == 1 and "→" in running[0],
            f"切片任务没登记到关闭提示清单：{running}",
        )
        dialog.task.taskCompleted.connect(loop.quit)
        dialog.task.taskTerminated.connect(loop.quit)
        QTimer.singleShot(120_000, loop.quit)
        loop.exec()
        from qgis.PyQt.QtWidgets import QApplication

        QApplication.processEvents()

        expect(dialog.task is None, "任务没有收尾")
        # 任务线程在发完信号后才注销登记，稍等一下避免时序误判
        import time as _time

        from app.core.domain.imagery_tiles.active_tasks import (
            active_descriptions as _active_descriptions,
        )

        for _ in range(50):
            if not _active_descriptions():
                break
            _time.sleep(0.1)
        expect(not _active_descriptions(), "任务结束后没从关闭提示清单注销")
        expect(dialog.execute_btn.isEnabled(), "完成后执行按钮没恢复")
        expect(not dialog.cancel_btn.isEnabled(), "完成后取消按钮没禁用")
        expect(dialog.loading.value() == 100, f"完成进度不对：{dialog.loading.value()}")
        expect("完成：2 个瓦片" in dialog.status_label.text(), f"状态不对：{dialog.status_label.text()}")
        expect("共 2 个元瓦片" in dialog.status_label.toolTip(), "状态栏没显示元瓦片计划")
        log_text = dialog.log_browser.toPlainText()
        expect("A total of 2 metatiles" in log_text, "日志框缺少算法计划日志")
        expect("影像切片完成" in log_text, "日志框缺少完成日志")
        expect("仅渲染所选图层" in log_text, "日志框缺少图层隔离说明")
        expect(dialog.only_layer_check.isChecked(), "「仅渲染所选图层」默认未勾选")
        expect(dialog._isolation_token == "", "任务结束后隔离 token 没释放")
        extra_node = QgsProject.instance().layerTreeRoot().findLayer(extra.id())
        expect(
            extra_node is not None and extra_node.itemVisibilityChecked(),
            "任务结束后其他图层没有恢复可见",
        )
        pngs = [p for p in target.rglob("*.png") if "_viewer_assets" not in p.parts]
        expect(len(pngs) == 2, f"XYZ 输出瓦片数不对：{len(pngs)}")
        viewer = target / "index.html"
        expect(viewer.is_file(), "XYZ 没生成网页查看器")
        expect(dialog.viewer_btn.isEnabled() and dialog._viewer_path == str(viewer), "查看器按钮没启用")
        expect((target / "_viewer_assets" / "leaflet" / "leaflet.js").is_file(), "XYZ 查看器没复制本地 Leaflet")
        html_text = viewer.read_text(encoding="utf-8")
        expect("unpkg.com" not in html_text, "XYZ 查看器仍依赖 CDN")
        expect("_viewer_assets/leaflet/leaflet.js" in html_text, "XYZ 查看器没引用本地脚本")
        expect("Created by QGIS" not in html_text, "XYZ 查看器仍有 Created by QGIS 水印")
        expect("attribution:" not in html_text, "XYZ 查看器仍带版权控件")
    finally:
        _dispose_dialog(dialog)
    return "线程执行完成：2 个 PNG、状态/日志/查看器按钮正确"


@case
def case_imagery_mbtiles_e2e(out: Path) -> str:
    """真实线程跑完 MBTiles 切片，检查扩展名补全、预览页与查看器按钮。"""
    from qgis.core import QgsProject, QgsRasterLayer
    from qgis.PyQt.QtCore import QEventLoop, QTimer
    from qgis.PyQt.QtWidgets import QApplication

    from app.ui.dialogs.imagery_mbtiles import ImageryMbtilesDialog

    _reset_project()
    raster = QgsRasterLayer(
        str(_make_mbtiles(out / "mbt_e2e" / "source.mbtiles")), "source", "gdal"
    )
    expect(raster.isValid(), "测试栅格打不开")
    QgsProject.instance().addMapLayer(raster)
    extra = make_layer("overlay")
    QgsProject.instance().addMapLayer(extra)
    target = out / "mbt_e2e" / "result"  # 故意不带扩展名，验证自动补 .mbtiles
    target.parent.mkdir(parents=True, exist_ok=True)
    preview_dir = target.with_name(target.name + "_preview")
    for stale in (
        target,
        Path(str(target) + ".mbtiles"),
        Path(str(target) + ".mbtiles-wal"),
        Path(str(target) + ".mbtiles-shm"),
        Path(str(target) + "-wal"),
        Path(str(target) + "-shm"),
    ):
        stale.unlink(missing_ok=True)
    if preview_dir.exists():
        shutil.rmtree(preview_dir)

    dialog = ImageryMbtilesDialog(None, "影像切片（MBTiles）")
    try:
        dialog._populate_layers()
        index = next(
            (
                row
                for row in range(dialog.input_combo.count())
                if dialog.input_combo.itemData(row) is not None
                and dialog.input_combo.itemData(row).id() == raster.id()
            ),
            -1,
        )
        expect(index >= 0, "图层下拉框里找不到测试栅格")
        dialog.input_combo.setCurrentIndex(index)
        dialog.min_zoom_spin.setValue(0)
        dialog.max_zoom_spin.setValue(1)
        dialog.viewer_check.setChecked(True)
        dialog.output_edit.setText(str(target))

        loop = QEventLoop()
        dialog._on_execute()
        expect(dialog.task is not None, "MBTiles 对话框没有启动任务")
        dialog.task.taskCompleted.connect(loop.quit)
        dialog.task.taskTerminated.connect(loop.quit)
        QTimer.singleShot(120_000, loop.quit)
        loop.exec()
        QApplication.processEvents()

        mbtiles = Path(str(target) + ".mbtiles")
        expect(dialog.output_edit.text() == str(mbtiles), "输出路径没补 .mbtiles")
        expect(dialog.task is None, "任务没有收尾")
        # 任务线程在发完信号后才注销登记，稍等一下避免时序误判
        import time as _time

        from app.core.domain.imagery_tiles.active_tasks import (
            active_descriptions as _active_descriptions,
        )

        for _ in range(50):
            if not _active_descriptions():
                break
            _time.sleep(0.1)
        expect(not _active_descriptions(), "任务结束后没从关闭提示清单注销")
        expect(dialog.execute_btn.isEnabled(), "完成后执行按钮没恢复")
        expect(dialog.loading.value() == 100, f"完成进度不对：{dialog.loading.value()}")
        expect("完成：2 个瓦片" in dialog.status_label.text(), f"状态不对：{dialog.status_label.text()}")
        expect("共 2 个元瓦片" in dialog.status_label.toolTip(), "状态栏没显示元瓦片计划")
        expect("MBTiles 切片完成" in dialog.log_browser.toPlainText(), "日志框缺少完成日志")
        expect("仅渲染所选图层" in dialog.log_browser.toPlainText(), "日志框缺少图层隔离说明")
        expect(dialog.only_layer_check.isChecked(), "「仅渲染所选图层」默认未勾选")
        expect(dialog._isolation_token == "", "任务结束后隔离 token 没释放")
        extra_node = QgsProject.instance().layerTreeRoot().findLayer(extra.id())
        expect(
            extra_node is not None and extra_node.itemVisibilityChecked(),
            "任务结束后其他图层没有恢复可见",
        )
        con = sqlite3.connect(mbtiles)
        try:
            count = con.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        finally:
            con.close()
        expect(count == 2, f"MBTiles 瓦片数不对：{count}")
        preview = target.with_name(target.name + "_preview") / "index.html"
        expect(preview.is_file(), "MBTiles 预览页没生成")
        expect(dialog.viewer_btn.isEnabled() and dialog._viewer_path == str(preview), "查看器按钮没启用")
        assets = preview.parent / "_viewer_assets" / "leaflet"
        expect((assets / "leaflet.js").is_file(), "MBTiles 预览没复制本地 Leaflet")
        preview_text = preview.read_text(encoding="utf-8")
        expect("unpkg.com" not in preview_text, "MBTiles 预览仍依赖 CDN")
        expect("Created by QGIS" not in preview_text, "MBTiles 预览仍有 Created by QGIS 水印")
        expect("attribution:" not in preview_text, "MBTiles 预览仍带版权控件")
    finally:
        _dispose_dialog(dialog)
    return "线程执行完成：.mbtiles 自动补扩展名、2 个瓦片、预览页/查看器按钮正确"


@case
def case_imagery_isolation(out: Path) -> str:
    """仅渲染所选图层：其他可见图层被临时隐藏，并发任务互不干扰，结束后恢复。"""
    from qgis.core import QgsProject, QgsRasterLayer

    from app.core.domain.imagery_tiles.isolation import (
        active_isolation_count,
        isolate_other_layers,
        release_layer_isolation,
        reset_isolation_state,
    )

    _reset_project()
    reset_isolation_state()
    project = QgsProject.instance()
    raster = QgsRasterLayer(
        str(_make_mbtiles(out / "iso" / "source.mbtiles")), "source", "gdal"
    )
    expect(raster.isValid(), "测试栅格打不开")
    project.addMapLayer(raster)
    extra = make_layer("extra")
    project.addMapLayer(extra)
    hidden = make_layer("hidden")
    project.addMapLayer(hidden)
    root = project.layerTreeRoot()
    root.findLayer(hidden.id()).setItemVisibilityChecked(False)

    def checked(layer) -> bool:
        node = root.findLayer(layer.id())
        return bool(node is not None and node.itemVisibilityChecked())

    try:
        names = isolate_other_layers(project, raster, "t1")
        expect(extra.name() in names, f"没有隐藏额外可见图层：{names}")
        expect(not checked(extra), "额外图层仍然可见")
        expect(checked(raster), "目标图层被误隐藏")
        expect(not checked(hidden), "原本隐藏的图层被显示")
        expect(active_isolation_count() == 1, "隔离计数不对")

        # 并发任务：各任务的目标图层都保持可见
        isolate_other_layers(project, extra, "t2")
        expect(checked(extra), "并发任务的目标图层被隐藏")
        expect(checked(raster), "公共目标图层被隐藏")
        expect(active_isolation_count() == 2, "并发隔离计数不对")

        release_layer_isolation(project, "t1")
        expect(active_isolation_count() == 1, "释放后隔离计数不对")
        expect(checked(raster), "先结束任务的目标图层没有恢复")
        release_layer_isolation(project, "t2")
        expect(active_isolation_count() == 0, "全部释放后仍有隔离")
        expect(checked(extra), "额外图层没有恢复可见")
        expect(not checked(hidden), "原本隐藏的图层没有保持隐藏")

        # 目标图层原本隐藏：切片期间临时显示，结束后恢复隐藏
        root.findLayer(raster.id()).setItemVisibilityChecked(False)
        isolate_other_layers(project, raster, "t3")
        expect(checked(raster), "隐藏的目标图层没有临时显示")
        release_layer_isolation(project, "t3")
        expect(not checked(raster), "目标图层的隐藏状态没有恢复")

        # 目标图层在未勾选的组里：切片期间展开祖先组，结束后恢复
        grouped = make_layer("grouped")
        group = root.addGroup("grp")
        # 先不建图例节点，交由组创建，避免同一图层出现两个图例节点
        project.addMapLayer(grouped, False)
        group.addLayer(grouped)
        group.setItemVisibilityChecked(False)
        isolate_other_layers(project, grouped, "t4")
        expect(group.itemVisibilityChecked(), "目标图层所在组没有临时展开")
        expect(checked(grouped), "组里的目标图层没有显示")
        release_layer_isolation(project, "t4")
        expect(not group.itemVisibilityChecked(), "目标图层所在组的可见性没有恢复")
    finally:
        reset_isolation_state()
        _reset_project()
    return "隔离/并发/恢复：其他可见图层临时隐藏且结束后恢复原状"


@case
def case_imagery_extent(out: Path) -> str:
    """全球范围切片：收进墨卡托边界，z0 只出 1 块，不产生负数/越界瓦片号。"""
    from qgis.core import QgsRectangle

    from app.core.domain.imagery_tiles.service import (
        WEB_MERCATOR_MAX_LAT,
        clip_to_mercator_bounds,
        estimate_tile_count,
    )

    world = QgsRectangle(-180.0, -WEB_MERCATOR_MAX_LAT, 180.0, WEB_MERCATOR_MAX_LAT)
    clipped = clip_to_mercator_bounds(world)
    expect(clipped.xMinimum() > -180.0 and clipped.xMaximum() < 180.0, "经度边界没有内缩")
    expect(
        clipped.yMinimum() > -WEB_MERCATOR_MAX_LAT
        and clipped.yMaximum() < WEB_MERCATOR_MAX_LAT,
        "纬度边界没有内缩",
    )
    count_z0 = estimate_tile_count(clipped, 0, 0)
    expect(count_z0 == 1, f"全球范围 z0 不是 1 块：{count_z0}")
    count = estimate_tile_count(clipped, 0, 2)
    expect(count == 1 + 4 + 16, f"全球范围 z0–2 瓦片数不对：{count}")

    # 普通城市影像不能被误改
    city = QgsRectangle(116.68, 27.84, 117.47, 28.63)
    same = clip_to_mercator_bounds(city)
    expect(
        (same.xMinimum(), same.yMinimum(), same.xMaximum(), same.yMaximum())
        == (116.68, 27.84, 117.47, 28.63),
        "普通影像范围被意外改动",
    )

    # 对话框范围选择：默认图层范围，可切到当前画布范围，并传给作业
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsProject,
        QgsRasterLayer,
    )
    from qgis.PyQt.QtWidgets import QWidget

    from app.core.domain.imagery_tiles.service import ImageryTileJob
    from app.ui.dialogs.imagery_tile import ImageryTileDialog

    class _Settings:
        def destinationCrs(self):
            return QgsCoordinateReferenceSystem("EPSG:3857")

    class _Canvas:
        def extent(self):
            return QgsRectangle(12000000, 3000000, 13000000, 4000000)

        def mapSettings(self):
            return _Settings()

    _reset_project()
    raster = QgsRasterLayer(
        str(_make_mbtiles(out / "extent_choice" / "source.mbtiles")), "source", "gdal"
    )
    expect(raster.isValid(), "范围选择测试栅格打不开")
    QgsProject.instance().addMapLayer(raster)
    parent = QWidget()
    parent.canvas = _Canvas()
    dialog = ImageryTileDialog(parent, "影像切片")
    try:
        index = next(
            row
            for row in range(dialog.input_combo.count())
            if dialog.input_combo.itemData(row) is not None
            and dialog.input_combo.itemData(row).id() == raster.id()
        )
        dialog.input_combo.setCurrentIndex(index)
        expect(dialog.extent_edit.currentText() == "图层范围", "输出范围默认不是图层范围")
        expect("EPSG:4326" in dialog.extent_text.text(), "图层范围没有显示")

        dialog.extent_edit.setCurrentText("当前画布范围")
        canvas_ext = dialog.out_extent_wgs84
        expect(canvas_ext is not None, "当前画布范围没有解析")
        ct = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem("EPSG:3857"),
            QgsCoordinateReferenceSystem("EPSG:4326"),
            QgsProject.instance().transformContext(),
        )
        expected = ct.transformBoundingBox(
            QgsRectangle(12000000, 3000000, 13000000, 4000000)
        )
        tolerance = 1e-6
        expect(
            abs(canvas_ext.xMinimum() - expected.xMinimum()) < tolerance
            and abs(canvas_ext.yMinimum() - expected.yMinimum()) < tolerance
            and abs(canvas_ext.xMaximum() - expected.xMaximum()) < tolerance
            and abs(canvas_ext.yMaximum() - expected.yMaximum()) < tolerance,
            "当前画布范围换算结果不对",
        )
        job = ImageryTileJob(
            layer=raster,
            output_dir=str(out / "extent_choice" / "tiles"),
            extent_wgs84=canvas_ext,
        )
        expect(
            job.slice_extent_wgs84() == canvas_ext,
            "作业没有采用画布范围",
        )
    finally:
        _dispose_dialog(dialog)
        parent.deleteLater()
        _reset_project()
    return "全球范围内缩到墨卡托边界（z0=1、z0–2=21 块），普通影像范围不变；范围可选图层/画布"


@case
def case_mbtiles_overwrite(out: Path) -> str:
    """已有 .mbtiles：默认不覆盖；确认后删旧文件 + 旧预览目录再提交任务。"""
    from qgis.core import QgsProject, QgsRasterLayer
    from qgis.PyQt.QtWidgets import QMessageBox

    from app.ui.dialogs.imagery_mbtiles import ImageryMbtilesDialog

    _reset_project()
    raster = QgsRasterLayer(
        str(_make_mbtiles(out / "overwrite" / "source.mbtiles")), "source", "gdal"
    )
    expect(raster.isValid(), "测试栅格打不开")
    QgsProject.instance().addMapLayer(raster)

    base = out / "overwrite" / "existing"
    existing = Path(str(base) + ".mbtiles")
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"old-mbtiles")
    preview_dir = Path(str(base) + "_preview")
    if preview_dir.exists():
        shutil.rmtree(preview_dir)
    preview_dir.mkdir(parents=True)
    (preview_dir / "old.png").write_bytes(b"old-preview")

    class GuardService:
        def __init__(self):
            self.submitted = []
            self.worker_specs = []

        def submit(self, job, worker_spec=None):
            self.submitted.append(job)
            self.worker_specs.append(worker_spec)
            return None  # 不真正跑算法，只检查提交参数

        def cancel(self, _task):
            return None

    dialog = ImageryMbtilesDialog(None, "影像切片（MBTiles）")
    original_question = QMessageBox.question
    try:
        service = GuardService()
        dialog.mbtiles_service = service
        dialog._populate_layers()
        index = next(
            (
                row
                for row in range(dialog.input_combo.count())
                if dialog.input_combo.itemData(row) is not None
                and dialog.input_combo.itemData(row).id() == raster.id()
            ),
            -1,
        )
        expect(index >= 0, "图层下拉框里找不到测试栅格")
        dialog.input_combo.setCurrentIndex(index)
        dialog.min_zoom_spin.setValue(0)
        dialog.max_zoom_spin.setValue(0)
        dialog.output_edit.setText(str(base))  # 不带扩展名

        # 先因规模太大取消：即使已有文件，也不能在用户确认覆盖前删掉
        original_estimate = dialog._estimate_tiles
        dialog._estimate_tiles = lambda *_a, **_k: 10**9
        QMessageBox.question = lambda _p, title, *_a, **_k: (
            QMessageBox.StandardButton.No
            if title == "切片确认"
            else QMessageBox.StandardButton.Yes
        )
        dialog._on_execute()
        expect(existing.is_file() and preview_dir.is_dir(), "因规模取消时删掉了旧文件")
        expect(not service.submitted, "因规模取消时仍提交了任务")
        dialog._estimate_tiles = original_estimate

        QMessageBox.question = lambda *a, **k: QMessageBox.StandardButton.No
        dialog._on_execute()
        expect(existing.is_file() and preview_dir.is_dir(), "选择「否」仍删除了旧文件")
        expect(not service.submitted, "选择「否」仍提交了任务")
        expect(dialog.output_edit.text() == str(existing), "取消覆盖后扩展名没归一化")

        QMessageBox.question = lambda *a, **k: QMessageBox.StandardButton.Yes
        dialog._on_execute()
        expect(not existing.exists(), "选择「是」没有删除旧 .mbtiles")
        expect(not preview_dir.exists(), "选择「是」没有删除旧预览目录")
        expect(len(service.submitted) == 1, "覆盖确认后没有提交任务")
        expect(
            service.worker_specs[0] is not None,
            "MBTiles 切片没有走后台子进程",
        )
        expect(service.submitted[0].output_file == str(existing), "提交的输出路径不对")
        expect(dialog.execute_btn.isEnabled(), "提交失败后执行按钮没恢复")
    finally:
        QMessageBox.question = original_question
        _dispose_dialog(dialog)
    return "已有 .mbtiles：默认保留；确认后清旧文件/预览目录再提交"


# --------------------------------------------------------------- 矢量瓦片


@case
def case_vector_viewer(out: Path) -> str:
    from app.core.domain.tiles import TileJob, write_vector_tile_viewer

    target = _fresh_dir(out / "viewer")
    job = TileJob(
        output_dir=str(target),
        xyz_template="{z}/{x}/{y}.pbf",
        layer_sources=["Point?crs=EPSG:4490"],
        layer_names=("城市/点",),
        min_zoom=0,
        max_zoom=3,
        extent=(113.0, 121.0, 23.0, 40.0, "EPSG:4490"),
    )
    html_path = Path(write_vector_tile_viewer(job, title="城市/点 <测试>"))
    expect(html_path.is_file(), "index.html 没生成")
    text = html_path.read_text(encoding="utf-8")
    expect("L.vectorGrid.protobuf" in text, "查看器没使用 VectorGrid")
    expect("{z}/{x}/{y}.pbf" in text, "查看器模板不对")
    expect("城市/点" in text, "图层名未写进样式键")
    expect("&lt;测试&gt;" in text, "标题未转义")
    expect("maxZoom:3" in text, "最大缩放未写入 fitBounds")
    assets = target / "_viewer_assets" / "leaflet"
    expect((assets / "leaflet.js").is_file(), "没复制本地 leaflet.js")
    expect((assets / "leaflet.css").is_file(), "没复制本地 leaflet.css")
    expect((assets / "Leaflet.VectorGrid.bundled.js").is_file(), "没复制本地 VectorGrid")
    expect("unpkg.com" not in text, "查看器仍引用 CDN")
    expect("_viewer_assets/leaflet/leaflet.js" in text, "查看器没引用本地 leaflet.js")
    expect("location.protocol==='file:'" in text, "查看器缺少 file:// 提示")
    _node_check_script(html_path, "var template=")
    return f"{html_path.name} 生成、离线资源齐备且 JS 语法通过"


@case
def case_vector_inspect(out: Path) -> str:
    """MVT 解析：图层名 / 要素数 / 零要素检测 / 非 pbf 文件容错。"""
    from app.core.domain.tiles.inspect import inspect_vector_tiles, tile_layers

    target = _fresh_dir(out / "inspect")
    (target / "0" / "0").mkdir(parents=True)
    (target / "0" / "0" / "0.pbf").write_bytes(_fake_pbf("cities", 3))
    (target / "1" / "0").mkdir(parents=True)
    (target / "1" / "0" / "0.pbf").write_bytes(_fake_pbf("cities", 2))

    layers = tile_layers((target / "0" / "0" / "0.pbf").read_bytes())
    expect(layers == [("cities", 3)], f"MVT 图层解析不对：{layers}")

    stats = inspect_vector_tiles(str(target))
    expect(stats.tiles_total == 2 and stats.tiles_scanned == 2, f"瓦片计数不对：{stats}")
    expect(stats.layer_names == ["cities"], f"图层名去重不对：{stats.layer_names}")
    expect(stats.feature_count == 5, f"要素数不对：{stats.feature_count}")
    expect(stats.has_features and not stats.truncated, "有效瓦片被判成空瓦片")

    empty = _fresh_dir(out / "inspect_empty")
    (empty / "0" / "0").mkdir(parents=True)
    (empty / "0" / "0" / "0.pbf").write_bytes(_fake_pbf("cities", 0))
    empty_stats = inspect_vector_tiles(str(empty))
    expect(empty_stats.layer_names == ["cities"], "空瓦片也应报告图层名")
    expect(not empty_stats.has_features, "零要素瓦片没被识别")

    garbage = _fresh_dir(out / "inspect_garbage")
    (garbage / "0").mkdir(parents=True)
    (garbage / "0" / "0.pbf").write_bytes(b"\xff\xff\xff")
    bad = inspect_vector_tiles(str(garbage))
    expect(bad.parse_errors >= 1 and not bad.has_features, "损坏的 pbf 没被容错处理")
    return "MVT 图层名/要素数/零要素/损坏文件检测正确"


@case
def case_viewer_server(out: Path) -> str:
    """本机 HTTP 服务：http://127.0.0.1 可访问 index / Leaflet / pbf（MIME 正确）。"""
    import urllib.error
    import urllib.request

    from app.core.domain.tiles import TileJob, write_vector_tile_viewer
    from app.infra.viewer_server import (
        serve_directory,
        shutdown_viewer_servers,
        viewer_url,
    )

    target = _fresh_dir(out / "serve")
    (target / "0" / "0").mkdir(parents=True)
    (target / "0" / "0" / "0.pbf").write_bytes(_fake_pbf("cities", 1))
    job = TileJob(
        output_dir=str(target),
        layer_sources=["Point?crs=EPSG:4490"],
        layer_names=("cities",),
        min_zoom=0,
        max_zoom=0,
        extent=(113.0, 121.0, 23.0, 40.0, "EPSG:4490"),
    )
    html_path = write_vector_tile_viewer(job, title="serve", layer_names=["cities"])
    expect(viewer_url(str(target / "missing.html")) is None, "不存在的页面仍返回 URL")

    base = serve_directory(str(target))
    expect(base and base.startswith("http://127.0.0.1:"), f"服务地址不对：{base}")
    expect(serve_directory(str(target)) == base, "同一目录应复用服务")
    url = viewer_url(html_path)
    expect(url == base + "index.html", f"查看器 URL 不对：{url}")

    def fetch(relative: str) -> tuple[bytes, str]:
        with urllib.request.urlopen(base + relative, timeout=10) as resp:
            return resp.read(), resp.headers.get("Content-Type", "")

    try:
        body, ctype = fetch("index.html")
        expect(b"L.vectorGrid" in body and "text/html" in ctype, "HTTP 读不到查看器页面")
        js, _ = fetch("_viewer_assets/leaflet/leaflet.js")
        expect(len(js) > 100_000, "HTTP 读不到本地 leaflet.js")
        pbf, pbf_type = fetch("0/0/0.pbf")
        expect(pbf == _fake_pbf("cities", 1), "HTTP 读到的 pbf 内容不对")
        expect("application/x-protobuf" in pbf_type, f"pbf MIME 不对：{pbf_type}")
    finally:
        shutdown_viewer_servers()
    return f"HTTP 服务可用（{base}）：index / leaflet.js / pbf 及 MIME 正确"


@case
def case_vector_task(out: Path) -> str:
    from processing.core.Processing import Processing

    Processing.initialize()
    _reset_project()
    layer = _add_layer("task_cities")
    target = _fresh_dir(out / "task")
    from app.core.domain.tiles import TileJob
    from app.core.domain.tiles.task import CreateTileTask

    job = TileJob(
        output_dir=str(target),
        xyz_template="{z}/{x}/{y}.pbf",
        layer_sources=[layer.source()],
        layer_names=(layer.name(),),
        min_zoom=0,
        max_zoom=1,
        extent=_extent(layer),
    )
    task = CreateTileTask("", job)
    progress: list[int] = []
    texts: list[str] = []
    logs: list[tuple[str, str]] = []
    task.progress_updated.connect(progress.append)
    task.progress_text.connect(texts.append)
    task.log_message.connect(lambda level, message: logs.append((level, message)))

    ok = task.run()
    expect(ok, f"任务返回失败：{task.exception}")
    pbfs = list(target.rglob("*.pbf"))
    expect(pbfs, "矢量瓦片没有产出任何 .pbf")
    expect(any(layer.name().encode() in path.read_bytes() for path in pbfs), "pbf 里没有图层名")
    expect(progress and progress[-1] == 100, f"进度信号不对：{progress}")
    expect(all(a <= b for a, b in zip(progress, progress[1:])), f"进度不是递增：{progress}")
    expect(logs, "没收到算法日志")
    return f"{len(pbfs)} 个 pbf，进度 {progress[:3]}…{progress[-1]}%，{len(logs)} 行日志"


@case
def case_vector_async(out: Path) -> str:
    """经 QgsTaskManager 异步跑一次，验证 create → 连信号 → start 流程。"""
    from qgis.PyQt.QtCore import QEventLoop, QTimer
    from qgis.PyQt.QtWidgets import QApplication

    _reset_project()
    layer = _add_layer("async_cities")
    target = _fresh_dir(out / "async")
    from app.core.domain.tiles import TileJob, TileService

    job = TileJob(
        output_dir=str(target),
        layer_sources=[layer.source()],
        layer_names=(layer.name(),),
        min_zoom=0,
        max_zoom=1,
        extent=_extent(layer),
    )
    service = TileService()
    task = service.create(job)
    progress: list[int] = []
    task.progress_updated.connect(progress.append)
    loop = QEventLoop()
    finished: list[str] = []
    task.taskCompleted.connect(lambda: finished.append("completed"))
    task.taskTerminated.connect(lambda: finished.append("terminated"))
    task.taskCompleted.connect(loop.quit)
    task.taskTerminated.connect(loop.quit)
    QTimer.singleShot(120_000, loop.quit)
    service.start(task)
    loop.exec()
    QApplication.processEvents()
    expect(finished == ["completed"], f"异步任务没有正常完成：{finished}")
    # QgsTaskManager 在完成后会自动删除 C++ 任务对象，因此不能再访问 task.isFinished()
    expect(progress and progress[-1] == 100, f"异步进度信号不对：{progress}")
    expect(list(target.rglob("*.pbf")), "异步任务没有产出 pbf")
    return f"QgsTaskManager 异步完成，进度 {progress[:3]}…{progress[-1]}%"


@case
def case_vector_dialog_e2e(out: Path) -> str:
    """对话框点执行 → QgsTaskManager 真实生成 → 完成状态 / 查看器 / 按钮复位。"""
    from qgis.PyQt.QtCore import QEventLoop, QTimer
    from qgis.PyQt.QtWidgets import QApplication

    import app.ui.dialogs.vector_tile as vector_tile_module

    dialog, layer = _vector_dialog_with_layer(out, "dialog_e2e")
    target = _fresh_dir(out / "dialog_e2e_output")
    messages: list[tuple[str, str]] = []
    original_send = vector_tile_module.send
    vector_tile_module.send = lambda level, message, **_kwargs: messages.append((level, message))
    try:
        _check_all_layers(dialog)
        dialog.output_edit.setText(str(target))
        dialog.min_zoom_spin.setValue(0)
        dialog.max_zoom_spin.setValue(1)
        dialog._advanced.viewer = True

        loop = QEventLoop()
        dialog._on_execute()
        expect(dialog.task is not None, "对话框没有启动任务")
        dialog.task.taskCompleted.connect(loop.quit)
        dialog.task.taskTerminated.connect(loop.quit)
        QTimer.singleShot(120_000, loop.quit)
        loop.exec()
        QApplication.processEvents()

        expect(dialog.execute_btn.isEnabled(), "完成后执行按钮没恢复")
        expect(not dialog.cancel_btn.isEnabled(), "完成后取消按钮没禁用")
        expect(dialog.advanced_btn.isEnabled() and dialog.batch_btn.isEnabled(), "高级/批量没恢复")
        expect(dialog.loading.value() == 100, f"完成进度不对：{dialog.loading.value()}")
        expect("完成：1 套，共 2 个瓦片" in dialog.status_label.text(),
               f"完成状态不对：{dialog.status_label.text()}")
        expect(any(level == "success" for level, _m in messages), "没发成功提示")
        expect(len(list(target.rglob("*.pbf"))) == 2, "对话框真实执行没有产出 2 个 pbf")
        viewer = target / "index.html"
        expect(viewer.is_file(), "对话框真实执行没生成网页查看器")
        expect(dialog.viewer_btn.isEnabled() and dialog._viewer_path == str(viewer), "查看器按钮没启用")
        expect(dialog._queue == [] and dialog._queue_total == 0, "队列没清空")
    finally:
        dialog._stop_running_task()
        vector_tile_module.send = original_send
        _dispose_dialog(dialog)
    return "对话框真实执行完成：2 个 pbf、成功提示、查看器按钮、按钮全部复位"


@case
def case_vector_cancel(out: Path) -> str:
    _reset_project()
    layer = _add_layer("cancel_cities")
    target = _fresh_dir(out / "cancel")
    from app.core.domain.tiles import TileJob, TileService

    job = TileJob(
        output_dir=str(target),
        layer_sources=[layer.source()],
        layer_names=(layer.name(),),
        min_zoom=0,
        max_zoom=4,
        extent=_extent(layer),
    )
    service = TileService()
    task = service.create(job)
    expect(not task.isCanceled() and not task.feedback.isCanceled(), "任务一开始就是取消状态")
    service.cancel(task)
    expect(task.isCanceled(), "QgsTask 取消标志没置位")
    expect(task.feedback.isCanceled(), "取消标志没传给算法反馈")
    expect(task.run() is False, "已取消的任务仍然成功返回")
    return "取消标志同步到 QgsTask + QgsProcessingFeedback，run() 返回 False"


from qgis.PyQt.QtCore import QObject, pyqtSignal


class _FakeSignals(QObject):
    """队列测试用的信号宿主（信号必须定义在 QObject 子类上）。"""

    progress_updated = pyqtSignal(int)
    progress_text = pyqtSignal(str)
    log_message = pyqtSignal(str, str)
    taskCompleted = pyqtSignal()
    taskTerminated = pyqtSignal()


class _FakeTask:
    """队列测试用任务：只提供对话框连接的那组信号。"""

    def __init__(self, job):
        self.job = job
        self.exception = None
        self._canceled = False
        self._signals = _FakeSignals()
        self.progress_updated = self._signals.progress_updated
        self.progress_text = self._signals.progress_text
        self.log_message = self._signals.log_message
        self.taskCompleted = self._signals.taskCompleted
        self.taskTerminated = self._signals.taskTerminated

    def isCanceled(self):
        return self._canceled

    def cancel(self):
        self._canceled = True


class _FakeService:
    def __init__(self):
        self.tasks: list[_FakeTask] = []
        self.canceled: list[_FakeTask] = []

    def create(self, job):
        task = _FakeTask(job)
        self.tasks.append(task)
        return task

    def start(self, _task):
        return None

    def cancel(self, task):
        task.cancel()
        self.canceled.append(task)


def _vector_dialog_with_layer(out: Path, name: str):
    _reset_project()
    layer = _add_layer(name)
    from app.ui.dialogs.vector_tile import VectorTileDialog

    dialog = VectorTileDialog(None, "创建矢量瓦片", dia_type="native:writevectortiles_xyz")
    return dialog, layer


def _check_all_layers(dialog) -> None:
    from qgis.PyQt.QtCore import Qt

    dialog.list_widget.blockSignals(True)
    for row in range(dialog.list_widget.count()):
        dialog.list_widget.item(row).setCheckState(Qt.CheckState.Checked)
    dialog.list_widget.blockSignals(False)
    dialog._sync_selected_layers()


@case
def case_vector_dialog(out: Path) -> str:
    import app.ui.dialogs.vector_tile as vector_tile_module

    dialog, layer = _vector_dialog_with_layer(out, "dialog_cities")
    messages: list[tuple[str, str]] = []
    original_send = vector_tile_module.send
    vector_tile_module.send = lambda level, message, **_kwargs: messages.append((level, message))
    started: list[tuple[object, str]] = []
    try:
        dialog._on_execute()
        expect(any("至少勾选" in message for _l, message in messages), "没拦截空图层")

        _check_all_layers(dialog)
        expect([item.name() for item in dialog.select_layers] == [layer.name()], "勾选同步失败")
        messages.clear()
        dialog._on_execute()
        expect(any("输出目录" in message for _l, message in messages), "没拦截空输出目录")

        target = out / "dialog"
        dialog.output_edit.setText(str(target))
        dialog.min_zoom_spin.setValue(5)
        dialog.max_zoom_spin.setValue(2)
        messages.clear()
        dialog._on_execute()
        expect(any("最小等级" in message for _l, message in messages), "没拦截 min>max")

        dialog.min_zoom_spin.setValue(0)
        dialog.max_zoom_spin.setValue(1)
        dialog.xyz_temp.setText("{z}/{x}.pbf")
        messages.clear()
        dialog._on_execute()
        expect(any("{y}" in message for _l, message in messages), "没拦截非法模板")

        dialog.xyz_temp.setText("{z}/{x}/{y}.pbf")
        dialog._start_jobs = lambda jobs: started.extend(jobs)
        messages.clear()
        dialog._on_execute()
        expect(len(started) == 1, f"合法参数没有启动任务：{messages}")
        job, label = started[0]
        expect(job.output_dir == str(target), f"输出目录不对：{job.output_dir}")
        expect(job.xyz_template == "{z}/{x}/{y}.pbf", "模板没传给任务")
        expect(job.layer_sources == [layer.source()], "图层源不对")
        expect(job.layer_names == (layer.name(),), "图层名不对")
        expect(label == layer.name(), f"任务标签不对：{label}")
        expect(job.extent[4] == "EPSG:4490", f"范围 CRS 不对：{job.extent}")
        rect = layer.extent()
        expect(abs(job.extent[0] - rect.xMinimum()) < 1e-9, "xmin 不对")
        expect(abs(job.extent[3] - rect.yMaximum()) < 1e-9, "ymax 不对")
    finally:
        vector_tile_module.send = original_send
        _dispose_dialog(dialog)
    return "空图层/空目录/min>max/坏模板均拦截，合法参数生成正确 TileJob"


@case
def case_vector_defaults(out: Path) -> str:
    """默认最大等级 14：低级别会简化掉面/线，z0–3 常生成空瓦片。"""
    dialog, _layer = _vector_dialog_with_layer(out, "defaults_cities")
    try:
        expect(dialog.min_zoom_spin.value() == 0, "默认最小等级应为 0")
        expect(dialog.max_zoom_spin.value() == 14, f"默认最大等级应为 14：{dialog.max_zoom_spin.value()}")
        expect("14" in dialog.max_zoom_spin.toolTip(), "最大等级缺少提示")
    finally:
        _dispose_dialog(dialog)
    return "默认缩放等级 0–14，并在提示里说明低级别会丢面/线要素"


@case
def case_vector_size_warn(out: Path) -> str:
    """大范围 + 默认 14 级：执行前按瓦片数预警，选「否」不建目录、不启动。"""
    from qgis.PyQt.QtWidgets import QMessageBox

    import app.ui.dialogs.vector_tile as vector_tile_module
    from app.core.domain.tiles import TileJob

    dialog, layer = _vector_dialog_with_layer(out, "warn_cities")
    service = _FakeService()
    dialog.tile_service = service
    target = out / "warn_out"
    if target.exists():
        shutil.rmtree(target)
    job = TileJob(
        output_dir=str(target),
        layer_sources=[layer.source()],
        layer_names=(layer.name(),),
        min_zoom=0,
        max_zoom=14,
        extent=(-180.0, 180.0, -85.0, 85.0, "EPSG:4326"),
    )
    original_question = QMessageBox.question
    try:
        QMessageBox.question = lambda *a, **k: QMessageBox.StandardButton.No
        dialog._start_jobs([(job, "大范围")])
        expect(not service.tasks, "规模预警选「否」仍启动了任务")
        expect(not target.exists(), "规模预警选「否」仍创建了输出目录")
        expect(not dialog._queue, "规模预警选「否」仍建立了队列")

        QMessageBox.question = lambda *a, **k: QMessageBox.StandardButton.Yes
        dialog._start_jobs([(job, "大范围")])
        expect(len(service.tasks) == 1, "规模预警选「是」没启动任务")
        dialog._on_cancel()
        dialog._on_task_terminated()
    finally:
        QMessageBox.question = original_question
        _dispose_dialog(dialog)
    return "大范围任务先估算瓦片数并确认；取消时不创建目录/不排队"


@case
def case_vector_empty(out: Path) -> str:
    """pbf 里没有要素：提示提高最大等级，不生成空白查看器。"""
    import app.ui.dialogs.vector_tile as vector_tile_module
    from app.core.domain.tiles import TileJob

    dialog, layer = _vector_dialog_with_layer(out, "empty_cities")
    messages: list[tuple[str, str]] = []
    original_send = vector_tile_module.send
    vector_tile_module.send = lambda level, message, **_kwargs: messages.append((level, message))
    try:
        service = _FakeService()
        dialog.tile_service = service
        dialog._advanced.viewer = True
        target = _fresh_dir(out / "empty")
        (target / "0" / "0").mkdir(parents=True)
        (target / "0" / "0" / "0.pbf").write_bytes(_fake_pbf(layer.name(), 0))
        job = TileJob(
            output_dir=str(target),
            layer_sources=[layer.source()],
            layer_names=(layer.name(),),
            min_zoom=0,
            max_zoom=3,
            extent=_extent(layer),
        )
        dialog._start_jobs([(job, "空数据")])
        dialog._on_task_completed()

        expect(not (target / "index.html").exists(), "空瓦片不该生成空白查看器")
        expect(not dialog.viewer_btn.isEnabled(), "空瓦片时查看器按钮不该启用")
        expect("没有要素" in dialog.status_label.text(),
               f"空瓦片状态不明：{dialog.status_label.text()}")
        expect(
            any(level == "warning" and "最大等级" in message for level, message in messages),
            f"空瓦片没提示提高最大等级：{messages}",
        )
        expect(dialog.execute_btn.isEnabled(), "空瓦片结束后执行按钮没恢复")

        # 同样的目录换成一个有要素的 pbf：查看器应生成并启用
        (target / "0" / "0" / "0.pbf").write_bytes(_fake_pbf(layer.name(), 2))
        messages.clear()
        dialog._start_jobs([(job, "有数据")])
        dialog._on_task_completed()
        expect((target / "index.html").is_file(), "有要素时没生成查看器")
        expect(dialog.viewer_btn.isEnabled(), "有要素时查看器按钮没启用")
        expect("个要素" in dialog.status_label.text(),
               f"成功状态没显示要素数：{dialog.status_label.text()}")
    finally:
        vector_tile_module.send = original_send
        _dispose_dialog(dialog)
    return "空 pbf → 警告提高最大等级且不生成查看器；有要素 → 正常生成"


@case
def case_vector_stale(out: Path) -> str:
    """工程删图层（wrapper 已销毁）后刷新列表/范围不应崩溃（原 15:06:47 崩溃）。"""
    from qgis.core import QgsProject

    dialog, _first = _vector_dialog_with_layer(out, "stale_keep")
    extra = _add_layer("stale_removed")
    try:
        expect(dialog.list_widget.count() == 2, f"列表应有 2 个图层：{dialog.list_widget.count()}")
        extra_id = extra.id()
        QgsProject.instance().removeMapLayer(extra_id)
        # layersRemoved 已触发一次刷新；再显式触发，覆盖信号顺序不定的情况
        dialog._on_project_layers_changed(extra_id)
        dialog._populate_layers()
        dialog._sync_selected_layers()
        expect(dialog.list_widget.count() == 1, "删除后列表没刷新")
        dialog._project_authid()
        dialog._combined_extent([extra, dialog.select_layers[0] if dialog.select_layers else None])
        dialog._checked_layers()
        dialog._on_extent_changed("图层范围")
    finally:
        _dispose_dialog(dialog)
    return "删除图层后刷新列表 / 取 CRS / 合并范围均不崩溃"


@case
def case_vector_queue(out: Path) -> str:
    import app.ui.dialogs.vector_tile as vector_tile_module

    dialog, layer = _vector_dialog_with_layer(out, "queue_cities")
    messages: list[tuple[str, str]] = []
    original_send = vector_tile_module.send
    vector_tile_module.send = lambda level, message, **_kwargs: messages.append((level, message))
    try:
        service = _FakeService()
        dialog.tile_service = service
        dialog._advanced.viewer = True

        dirs = [_fresh_dir(out / "queue" / "a"), _fresh_dir(out / "queue" / "b")]
        for index, directory in enumerate(dirs, start=1):
            (directory / f"test{index}.pbf").write_bytes(
                _fake_pbf(layer.name(), features=index)
            )

        from app.core.domain.tiles import TileJob

        jobs = [
            TileJob(
                output_dir=str(directory),
                layer_sources=[layer.source()],
                layer_names=(layer.name(),),
                min_zoom=0,
                max_zoom=3,
                extent=_extent(layer),
            )
            for directory in dirs
        ]
        dialog._start_jobs([(jobs[0], "第一套"), (jobs[1], "第二套")])
        expect(len(service.tasks) == 1, "第一套没有创建任务")
        expect(not dialog.execute_btn.isEnabled(), "执行中执行按钮没禁用")
        expect(dialog.cancel_btn.isEnabled(), "执行中取消按钮没启用")
        expect(not dialog.advanced_btn.isEnabled(), "执行中高级按钮没禁用")
        expect(not dialog.batch_btn.isEnabled(), "执行中批量按钮没禁用")
        expect(dialog.loading.maximum() == 0, "首批进度前没显示忙碌状态")
        expect("第 1/2 套" in dialog.status_label.text(), "状态栏没有批处理序号")

        dialog._on_task_progress(50)
        expect(dialog.loading.maximum() == 100, "收到进度后没切回百分比")
        expect(dialog.loading.value() == 25, f"总进度不对：{dialog.loading.value()}")
        expect("缩放级别 2（3/4）" in dialog.status_label.text(), "缩放级别推算不对")
        dialog._on_task_progress_text("算法阶段说明")
        expect("算法阶段说明" in dialog.status_label.text(), "progress_text 没显示")

        dialog._on_task_completed()
        expect(len(service.tasks) == 2, "第一套完成后没启动第二套")
        expect(not dialog.execute_btn.isEnabled(), "批处理中途执行按钮被提前启用")
        expect(dialog._queue_index == 1, "队列索引没前进")
        expect((dirs[0] / "index.html").is_file(), "第一套没生成网页查看器")

        dialog._on_task_completed()
        expect(dialog.execute_btn.isEnabled(), "全部完成后执行按钮没恢复")
        expect(not dialog.cancel_btn.isEnabled(), "全部完成后取消按钮没禁用")
        expect(dialog.advanced_btn.isEnabled() and dialog.batch_btn.isEnabled(), "高级/批量没恢复")
        expect(dialog.loading.value() == 100, f"完成进度不对：{dialog.loading.value()}")
        expect("完成：2 套，共 2 个瓦片" in dialog.status_label.text(), f"完成状态不对：{dialog.status_label.text()}")
        expect(dialog.viewer_btn.isEnabled(), "完成后查看器按钮没启用")
        expect((dirs[1] / "index.html").is_file(), "第二套没生成网页查看器")
        expect(any(level == "success" for level, _m in messages), "没发成功提示")
    finally:
        vector_tile_module.send = original_send
        _dispose_dialog(dialog)
    return "两套顺序执行、总进度 25→100%、成功提示与查看器按钮 OK"


@case
def case_vector_abort(out: Path) -> str:
    import app.ui.dialogs.vector_tile as vector_tile_module

    dialog, layer = _vector_dialog_with_layer(out, "abort_cities")
    messages: list[tuple[str, str]] = []
    original_send = vector_tile_module.send
    vector_tile_module.send = lambda level, message, **_kwargs: messages.append((level, message))
    try:
        from app.core.domain.tiles import TileJob

        service = _FakeService()
        dialog.tile_service = service
        target = _fresh_dir(out / "abort")
        job = TileJob(
            output_dir=str(target),
            layer_sources=[layer.source()],
            layer_names=(layer.name(),),
            min_zoom=0,
            max_zoom=2,
            extent=_extent(layer),
        )

        dialog._start_jobs([(job, "取消套")])
        dialog._on_cancel()
        expect(service.canceled and service.canceled[0].isCanceled(), "取消没传给任务")
        expect(not dialog.cancel_btn.isEnabled() and "取消中" in dialog.cancel_btn.text(), "取消中状态不对")
        dialog._on_task_terminated()
        expect("已取消" in dialog.status_label.text(), f"取消状态不对：{dialog.status_label.text()}")
        expect(dialog.execute_btn.isEnabled(), "取消后执行按钮没恢复")
        expect(dialog.loading.value() == 0, "取消后进度没归零")

        dialog._start_jobs([(job, "失败套")])
        dialog.task.exception = RuntimeError("模拟算法失败")
        dialog._on_task_terminated()
        expect("失败" in dialog.status_label.text() and "模拟算法失败" in dialog.status_label.text(),
               f"失败状态不对：{dialog.status_label.text()}")
        expect(dialog.execute_btn.isEnabled(), "失败后执行按钮没恢复")
        expect(not dialog.cancel_btn.isEnabled(), "失败后取消按钮没禁用")
        expect(any(level == "critical" for level, _m in messages), "失败没发错误提示")
    finally:
        vector_tile_module.send = original_send
        _dispose_dialog(dialog)
    return "取消与失败都能收尾并恢复按钮，状态/提示正确"


@case
def case_vector_batch(out: Path) -> str:
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import QDialog, QMessageBox

    from app.ui.dialogs.vector_tile_batch import VectorTileBatchDialog, safe_subdir_name

    used: set[str] = set()
    expect(safe_subdir_name("a/b:c*?", used) == "a_b_c", "非法字符清洗不对")
    expect(safe_subdir_name("  hello   world  ", used) == "hello_world", "空白清洗不对")
    expect(safe_subdir_name("a/b:c*?", used) == "a_b_c_2", "大小写/重复去重不对")
    expect(safe_subdir_name("layer", {"layer"}) == "layer_2", "显式 existing 去重不对")
    expect(len(safe_subdir_name("x" * 100, set())) == 60, "长度限制不对")
    expect(safe_subdir_name("", set()) == "layer", "空名兜底不对")

    _reset_project()
    first = _add_layer("batch/one")
    second = _add_layer("batch/two")
    dialog = VectorTileBatchDialog(
        None, [first, second], preselected=[first], output_root=str(out / "batch")
    )
    warnings: list[str] = []
    original_warning = QMessageBox.warning
    QMessageBox.warning = lambda _parent, _title, message, *a, **k: warnings.append(message)
    try:
        targets = dialog.selected_targets()
        expect(len(targets) == 1 and targets[0].layer.id() == first.id(), "预选图层不生效")
        expect(dialog.use_layer_extent(), "默认应使用每个图层自己的范围")

        dialog._set_all(Qt.CheckState.Checked)
        targets = dialog.selected_targets()
        expect(len(targets) == 2, f"全选后数量不对：{len(targets)}")
        expect(len({target.subdir.casefold() for target in targets}) == 2, "子目录重名")
        expect(all("/" not in target.subdir and "\\" not in target.subdir for target in targets), "子目录含路径分隔符")

        dialog.use_layer_extent_check.setChecked(False)
        expect(not dialog.use_layer_extent(), "范围开关不生效")

        dialog.output_edit.setText("")
        dialog._validate()
        expect(dialog.result() != QDialog.DialogCode.Accepted, "空输出根目录仍被接受")
        expect(any("输出根目录" in message for message in warnings), "没提示选择输出根目录")

        dialog.output_edit.setText(str(out / "batch"))
        dialog._set_all(Qt.CheckState.Unchecked)
        dialog._validate()
        expect(dialog.result() != QDialog.DialogCode.Accepted, "空图层列表仍被接受")
        expect(any("至少勾选" in message for message in warnings), "没提示勾选图层")

        dialog._set_all(Qt.CheckState.Checked)
        dialog._validate()
        expect(dialog.result() == QDialog.DialogCode.Accepted, "合法批量设置没被接受")
    finally:
        QMessageBox.warning = original_warning
        _dispose_dialog(dialog)
    return "批量弹窗预选/全选/范围开关/校验 OK，子目录清洗去重 OK"


@case
def case_vector_advanced(out: Path) -> str:
    import app.ui.dialogs.vector_tile as vector_tile_module
    from qgis.PyQt.QtWidgets import QDialog, QMessageBox

    from app.ui.dialogs.vector_tile_advanced import (
        VectorTileAdvancedDialog,
        VectorTileAdvancedOptions,
    )

    dialog, _layer = _vector_dialog_with_layer(out, "advanced_cities")
    original_dialog = vector_tile_module.VectorTileAdvancedDialog
    seen: list[VectorTileAdvancedOptions] = []

    class FakeAdvancedDialog:
        def __init__(self, _parent, current):
            seen.append(current)

        def exec(self):
            return 1  # QDialog.Accepted

        def options(self):
            return VectorTileAdvancedOptions(
                template="{z}/{x}/{y}.pbf", clear_output=True, viewer=True
            )

        def deleteLater(self):
            return None

    vector_tile_module.VectorTileAdvancedDialog = FakeAdvancedDialog
    try:
        dialog._open_advanced()
    finally:
        vector_tile_module.VectorTileAdvancedDialog = original_dialog
        _dispose_dialog(dialog)
    expect(seen and seen[0].template == "{z}/{x}/{y}.pbf", "高级弹窗没拿到当前模板")
    expect(dialog._advanced.clear_output and dialog._advanced.viewer, "高级选项没保存")
    expect(dialog.xyz_temp.text() == "{z}/{x}/{y}.pbf", "高级模板没同步回主面板")

    # 真实高级弹窗：选项往返 + 非法模板拦截 + 合法模板接受
    real = VectorTileAdvancedDialog(
        None,
        VectorTileAdvancedOptions(
            template="{z}/{x}/{y}.pbf", clear_output=True, viewer=True
        ),
    )
    warnings: list[str] = []
    original_warning = QMessageBox.warning
    QMessageBox.warning = lambda _p, _t, message, *a, **k: warnings.append(message)
    try:
        opts = real.options()
        expect(opts.template == "{z}/{x}/{y}.pbf", "高级弹窗模板回读不对")
        expect(opts.clear_output and opts.viewer, "高级弹窗开关回读不对")
        real.template_edit.setText("{z}/{x}.pbf")
        real._validate()
        expect(real.result() != QDialog.DialogCode.Accepted, "非法模板被接受")
        expect(warnings and "{y}" in warnings[0], "非法模板没提示 {y}")
        real.template_edit.setText("{z}/{x}/{y}.pbf")
        real._validate()
        expect(real.result() == QDialog.DialogCode.Accepted, "合法模板没被接受")
    finally:
        QMessageBox.warning = original_warning
        real.close()
        real.deleteLater()
    return "高级按钮 → 弹窗 → 模板/清空/查看器选项生效，真实弹窗校验正确"


@case
def case_vector_batch_run(out: Path) -> str:
    import app.ui.dialogs.vector_tile as vector_tile_module
    from app.ui.dialogs.vector_tile_batch import VectorTileBatchTarget

    dialog, first = _vector_dialog_with_layer(out, "batch_run_one")
    second = _add_layer("batch_run_two")
    root = out / "batch_run"
    original_dialog = vector_tile_module.VectorTileBatchDialog

    class FakeBatchDialog:
        def __init__(self, _parent, layers, **_kwargs):
            self.by_name = {layer.name(): layer for layer in layers}

        def exec(self):
            return 1  # QDialog.Accepted

        def selected_targets(self):
            return [
                VectorTileBatchTarget(self.by_name["batch_run_one"], "one"),
                VectorTileBatchTarget(self.by_name["batch_run_two"], "two"),
            ]

        def output_root(self):
            return str(root)

        def use_layer_extent(self):
            return True

        def deleteLater(self):
            return None

    started: list[tuple[object, str]] = []
    original_start = dialog._start_jobs
    dialog._start_jobs = lambda jobs: started.extend(jobs)
    vector_tile_module.VectorTileBatchDialog = FakeBatchDialog
    try:
        dialog._open_batch()
    finally:
        vector_tile_module.VectorTileBatchDialog = original_dialog
        dialog._start_jobs = original_start
        _dispose_dialog(dialog)

    expect(len(started) == 2, f"批量没有生成 2 个任务：{len(started)}")
    jobs = {label: job for job, label in started}
    expect(set(jobs) == {"batch_run_one", "batch_run_two"}, f"批量标签不对：{set(jobs)}")
    expect(jobs["batch_run_one"].output_dir == str(root / "one"), "第一套输出子目录不对")
    expect(jobs["batch_run_two"].output_dir == str(root / "two"), "第二套输出子目录不对")
    expect(jobs["batch_run_one"].layer_sources == [first.source()], "第一套图层源不对")
    expect(jobs["batch_run_two"].layer_sources == [second.source()], "第二套图层源不对")
    expect(jobs["batch_run_one"].layer_names == ("batch_run_one",), "第一套图层名不对")
    expect(jobs["batch_run_one"].extent == _extent(first), "第一套范围不是自己的范围")
    expect(jobs["batch_run_two"].extent == _extent(second), "第二套范围不是自己的范围")
    return "批量按钮 → 每图层一个子目录，范围/图层名各自独立"


@case
def case_vector_clear(out: Path) -> str:
    from qgis.PyQt.QtWidgets import QMessageBox

    import app.ui.dialogs.vector_tile as vector_tile_module

    dialog, layer = _vector_dialog_with_layer(out, "clear_cities")
    target = _fresh_dir(out / "clear")
    stale = target / "old.pbf"
    stale.write_bytes(b"stale")
    stale_dir = target / "9" / "1"
    stale_dir.mkdir(parents=True)
    (stale_dir / "2.pbf").write_bytes(b"stale")
    messages: list[tuple[str, str]] = []
    original_send = vector_tile_module.send
    vector_tile_module.send = lambda level, message, **_kwargs: messages.append((level, message))
    original_question = QMessageBox.question
    question_calls: list[str] = []
    try:
        from app.core.domain.tiles import TileJob

        service = _FakeService()
        dialog.tile_service = service
        dialog._advanced.clear_output = True
        job = TileJob(
            output_dir=str(target),
            layer_sources=[layer.source()],
            layer_names=(layer.name(),),
            min_zoom=0,
            max_zoom=1,
            extent=_extent(layer),
        )

        # 非空目录：默认按钮是「否」，取消后旧文件必须原样保留
        QMessageBox.question = lambda _p, _t, text, *a, **k: (
            question_calls.append(text) or QMessageBox.StandardButton.No
        )
        dialog._start_jobs([(job, "清空测试")])
        expect(question_calls, "非空目录没有二次确认")
        expect(stale.is_file() and (stale_dir / "2.pbf").is_file(), "选择「否」仍然删除了文件")
        expect(not service.tasks, "选择「否」仍然启动了任务")
        expect(dialog.execute_btn.isEnabled(), "取消清空后执行按钮没恢复")

        # 选择「是」后清空目录（含子目录），再启动任务
        QMessageBox.question = lambda *a, **k: QMessageBox.StandardButton.Yes
        dialog._start_jobs([(job, "清空测试")])
        expect(not stale.exists() and not stale_dir.exists(), "选择「是」没有清空旧瓦片")
        expect(len(service.tasks) == 1, "清空后没启动任务")
        dialog._on_cancel()
        dialog._on_task_terminated()
    finally:
        QMessageBox.question = original_question
        vector_tile_module.send = original_send
        _dispose_dialog(dialog)
    return "清空输出目录：非空先确认，「否」保留、「是」清空后执行"


# --------------------------------------------------------------- MBTiles 预览


@case
def case_mbtiles_preview(out: Path) -> str:
    from app.core.domain.imagery_mbtiles import ImageryMbtilesJob
    from app.core.domain.imagery_mbtiles.task import CreateImageryMbtilesTask

    target = _make_mbtiles(out / "preview" / "demo.mbtiles")
    job = ImageryMbtilesJob(
        layer=None,  # 预览只读 .mbtiles，不碰图层
        output_file=str(target),
        min_zoom=0,
        max_zoom=0,
        tile_format=0,
        antialias=True,
        html=True,
    )
    task = CreateImageryMbtilesTask("", job)
    html_path = Path(task._write_preview_html(job))
    expect(html_path.is_file(), "MBTiles 预览页没生成")
    expect(html_path.name == "index.html", "预览页文件名不对")
    preview_tile = html_path.parent / "0" / "0" / "0.png"
    expect(preview_tile.is_file(), f"瓦片没解包到 XYZ 目录：{preview_tile}")
    con = sqlite3.connect(target)
    try:
        original_size = con.execute(
            "SELECT length(tile_data) FROM tiles WHERE zoom_level=0 AND tile_column=0"
        ).fetchone()[0]
    finally:
        con.close()
    expect(preview_tile.stat().st_size == original_size, "解包瓦片大小异常")
    text = html_path.read_text(encoding="utf-8")
    expect("L.tileLayer" in text and "{z}/{x}/{y}.png" in text, "预览页没用相对 XYZ 模板")
    expect("minNativeZoom:0" in text and "maxNativeZoom:0" in text, "预览页缩放范围不对")
    expect("noWrap:true" in text, "预览页没有禁止世界重复，低级别会铺出多份地图")
    _node_check_script(html_path, "var map=L.map")
    return f"{html_path} + 0/0/0.png 解包正确，JS 语法通过"


# --------------------------------------------------------------- 子进程切片


@case
def case_imagery_worker(out: Path) -> str:
    """子进程切片：临时工程/进度日志/输出正确，且跑的时候主线程不卡。"""
    import json
    import threading
    import time

    from qgis.core import QgsProject, QgsRasterLayer
    from qgis.PyQt.QtCore import QEventLoop, QTimer

    from app.core.domain.imagery_tiles.service import ImageryTileJob
    from app.core.domain.imagery_tiles.worker_runner import (
        WorkerCallbacks,
        prepare_worker_job,
        run_worker_process,
    )

    _reset_project()
    raster = QgsRasterLayer(
        str(_make_mbtiles(out / "worker" / "source.mbtiles")), "source", "gdal"
    )
    expect(raster.isValid(), "测试栅格打不开")
    project = QgsProject.instance()
    project.addMapLayer(raster)
    extra = make_layer("extra")
    project.addMapLayer(extra)
    original_file = project.fileName()

    target = _fresh_dir(out / "worker" / "tiles")
    job = ImageryTileJob(
        layer=raster, output_dir=str(target), min_zoom=0, max_zoom=1, html=False
    )
    spec = prepare_worker_job(job, "xyz", only_layer=True)
    expect(spec is not None, "prepare_worker_job 没有返回作业")
    try:
        # 另存工程不能改掉主工程的路径 / 已修改标记（否则用户点保存会存到临时工程）
        expect(project.fileName() == original_file, "另存临时工程后主工程文件名被改了")
        payload = json.loads(spec.job_file.read_text(encoding="utf-8"))
        expect(payload["only_layer"] is True, "作业没带「仅渲染所选图层」")
        expect(Path(payload["project"]).is_absolute(), "临时工程路径不是绝对路径")
        expect(Path(payload["output"]).is_absolute(), "输出路径不是绝对路径")
        expect(payload["layer_id"] == raster.id(), "作业里的图层 id 不对")
        text = spec.project_file.read_text(encoding="utf-8")
        expect("source.mbtiles" in text, "临时工程里没有测试栅格")
        expect('name="extra"' in text, "内存图层没写进临时工程")
        expect("checked=" in text, "临时工程缺少图层可见性信息")

        progress: list[int] = []
        logs: list[tuple[str, str]] = []
        box: dict = {}

        def run():
            box["result"] = run_worker_process(
                spec,
                threading.Event(),
                WorkerCallbacks(
                    progress=progress.append,
                    log=lambda level, msg: logs.append((level, msg)),
                ),
            )

        # 主线程 100ms 心跳：子进程切片期间必须一直拿到事件循环
        ticks: list[float] = []
        timer = QTimer()
        timer.setInterval(100)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        thread = threading.Thread(target=run, daemon=True)
        loop = QEventLoop()

        def check():
            if not thread.is_alive():
                loop.quit()

        poll = QTimer()
        poll.setInterval(100)
        poll.timeout.connect(check)
        timer.start()
        poll.start()
        thread.start()
        QTimer.singleShot(180_000, loop.quit)
        loop.exec()
        poll.stop()
        timer.stop()
        thread.join(timeout=5)

        result = box.get("result")
        expect(result is not None, "子进程切片没有返回结果")
        expect(result.ok, f"子进程切片失败：{result.error}")
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        expect(gaps, "心跳没有跑起来，无法判断界面是否卡顿")
        expect(
            max(gaps) < 0.5,
            f"子进程切片期间主线程被卡住 {max(gaps) * 1000:.0f} ms",
        )
        expect(progress and progress[-1] == 100, f"进度事件不对：{progress}")
        messages = [msg for _level, msg in logs]
        expect(any("仅渲染所选图层" in m for m in messages), "缺少图层隔离日志")
        expect(any("影像切片完成" in m for m in messages), "缺少完成日志")
        pngs = [p for p in target.rglob("*.png")]
        expect(len(pngs) == 2, f"子进程输出瓦片数不对：{len(pngs)}")
        extra_node = project.layerTreeRoot().findLayer(extra.id())
        expect(
            extra_node is not None and extra_node.itemVisibilityChecked(),
            "子进程切片改动了主进程的图层可见性",
        )
    finally:
        spec.cleanup()
    expect(not spec.work_dir.exists(), "作业结束后临时目录没有清理")
    return "子进程切片完成：2 个 PNG、进度/日志转发、主线程最大停顿 < 500ms"


@case
def case_imagery_worker_cancel(out: Path) -> str:
    """取消子进程切片：结果记为已取消，不报异常。"""
    import threading
    import time

    from qgis.core import QgsProject, QgsRasterLayer

    from app.core.domain.imagery_tiles.service import ImageryTileJob
    from app.core.domain.imagery_tiles.worker_runner import (
        prepare_worker_job,
        run_worker_process,
    )

    _reset_project()
    raster = QgsRasterLayer(
        str(_make_mbtiles(out / "worker_cancel" / "source.mbtiles")), "source", "gdal"
    )
    expect(raster.isValid(), "测试栅格打不开")
    QgsProject.instance().addMapLayer(raster)

    target = _fresh_dir(out / "worker_cancel" / "tiles")
    job = ImageryTileJob(
        layer=raster, output_dir=str(target), min_zoom=0, max_zoom=14, html=False
    )
    spec = prepare_worker_job(job, "xyz", only_layer=False)
    expect(spec is not None, "prepare_worker_job 没有返回作业")
    cancel = threading.Event()
    try:
        box: dict = {}

        def run():
            box["result"] = run_worker_process(spec, cancel)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        # 等子进程起来再取消（启动 QGIS 要几秒，取消要能打断）
        time.sleep(1.5)
        cancel.set()
        worker.join(timeout=60)
        expect(not worker.is_alive(), "取消后子进程任务线程没有退出")
        result = box.get("result")
        expect(result is not None and result.canceled, "取消结果没有被标记")
    finally:
        spec.cleanup()
    return "取消子进程切片：进程被终止，结果标记为已取消"


@case
def case_imagery_close_prompt(out: Path) -> str:
    """关闭程序时有影像切片在跑：弹窗提示，可取消关闭或终止切片后退出。"""
    from qgis.PyQt.QtWidgets import QMessageBox, QWidget

    from app.core.domain.imagery_tiles import active_tasks
    from app.ui.main_window.mixins.project_ops import ProjectOpsMixin

    class _Host(ProjectOpsMixin, QWidget):
        pass

    class _Task:
        def __init__(self) -> None:
            self.canceled = False

        def cancel(self) -> None:
            self.canceled = True

    host = _Host()
    first, second = _Task(), _Task()
    active_tasks.register(first, "影像A → D:/tiles/a")
    active_tasks.register(second, "影像B → D:/tiles/b.mbtiles")
    expect(active_tasks.has_active(), "登记后应视为有任务在跑")
    expect(
        active_tasks.active_descriptions() == ["影像A → D:/tiles/a", "影像B → D:/tiles/b.mbtiles"],
        f"登记描述不对：{active_tasks.active_descriptions()}",
    )

    original_exec = QMessageBox.exec
    choice = {"stop": False}
    shown: list[QMessageBox] = []

    def fake_exec(box: QMessageBox) -> int:
        """不真的弹窗：记下内容，按 ``choice`` 点对应的按钮。"""
        shown.append(box)
        role = (
            QMessageBox.ButtonRole.DestructiveRole
            if choice["stop"]
            else QMessageBox.ButtonRole.RejectRole
        )
        for button in box.buttons():
            if box.buttonRole(button) == role:
                button.click()
                return 0
        return 0

    QMessageBox.exec = fake_exec  # type: ignore[method-assign]
    try:
        # 1) 选「取消」：不允许关闭，且不能提前终止切片
        expect(not host._confirm_slicing_on_close(), "点「取消」后不应允许关闭")
        text, info = shown[-1].text(), shown[-1].informativeText()
        expect("2 个影像切片任务" in text, f"提示文字不对：{text}")
        expect("影像A" in info and "影像B" in info, f"提示没列出任务：{info}")
        expect(not first.canceled and not second.canceled, "用户确认前不应终止切片")

        # 2) 选「终止切片并退出」：允许关闭
        choice["stop"] = True
        expect(host._confirm_slicing_on_close(), "点「终止切片并退出」应允许关闭")
    finally:
        QMessageBox.exec = original_exec  # type: ignore[method-assign]

    # 3) 关闭流程里的 cancel_all 会要求每个任务取消
    expect(active_tasks.cancel_all() == 2, "cancel_all 返回的取消个数不对")
    expect(first.canceled and second.canceled, "cancel_all 没有取消任务")
    active_tasks.unregister(first)
    active_tasks.unregister(second)
    expect(not active_tasks.has_active(), "注销后仍有活动任务")

    # 4) 没有任务 / 测试开关关闭时不弹窗
    count = len(shown)
    expect(host._confirm_slicing_on_close(), "没有任务时应直接允许关闭")
    expect(len(shown) == count, "没有任务时不该弹窗")
    active_tasks.register(first, "影像A → D:/tiles/a")
    host.prompt_slices_on_close = False
    expect(host._confirm_slicing_on_close(), "开关关闭时应直接允许关闭")
    expect(len(shown) == count, "开关关闭时不该弹窗")
    active_tasks.unregister(first)
    host.deleteLater()
    return "关闭提示：列出任务、取消不关闭、终止后允许关闭并取消全部任务"


# --------------------------------------------------------------- 入口


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="瓦片工具完整自检")
    parser.add_argument("--out", type=Path, default=_ROOT / ".tmp" / "tile_tools")
    parser.add_argument("--only", nargs="+", choices=[name for name, _fn in CASES])
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    print(f"[env] 输出目录 = {out}")
    init_qgis()

    from processing.core.Processing import Processing

    Processing.initialize()

    selected = [(name, fn) for name, fn in CASES if not args.only or name in args.only]
    results: list[tuple[str, bool, str]] = []
    for name, fn in selected:
        print(f"\n[case] {name}")
        try:
            detail = fn(out)
            results.append((name, True, detail))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            results.append((name, False, f"{type(exc).__name__}: {exc}"))

    print("\n" + "=" * 72)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<18} {detail}")
    failed = [name for name, ok, _detail in results if not ok]
    print("=" * 72)
    print(
        f"合计 {len(results)} 项，通过 {len(results) - len(failed)} 项"
        + (f"，失败：{', '.join(failed)}" if failed else "")
    )
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    raise SystemExit(main())
