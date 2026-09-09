"""下载管理面板（方案 §8.3）：列出全部离线下载任务、状态/进度/目标，支持
取消 / 重试 / 打开目标目录。

非模态弹窗，监听 :class:`DownloadManager.record_changed` 刷新（进度高频时 300ms
防抖）；「下载离线图层」弹窗与工具条均可打开它。
"""

from __future__ import annotations

import os
import webbrowser
from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog

logger = get_logger(__name__)

_HEADERS = ("任务", "状态", "进度", "目标 zip", "错误/阶段")
_STATUS_LABELS = {
    "pending": "待下载",
    "running": "下载中",
    "paused": "暂停",
    "succeeded": "成功",
    "partial": "部分失败",
    "failed": "失败",
    "cancelled": "已取消",
}


class OfflineDownloadManagerDialog(BaseDialog):
    """离线下载管理弹窗。"""

    def __init__(self, parent=None, service=None, title: str = "下载管理") -> None:
        super().__init__(parent, title)
        from app.core.domain.offline_download.service import OfflineDownloadService

        self.service = service or OfflineDownloadService()
        self.manager = self.service.manager
        self.manager.record_changed.connect(self._debounce_refresh)
        # 独立顶层窗口：最小化进任务栏（Dialog 类型会最小化到父窗口/消失）
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setMinimumSize(760, 460)
        self._build_ui()
        self.refresh()
        self._detach_from_parent()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)

        tip = QLabel("运行中任务在「下载离线图层」窗口查看进度；这里可取消/重试/打开目录")
        tip.setStyleSheet("color:#888;")
        layout.addWidget(tip)

        btns = QHBoxLayout()
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh)
        btns.addWidget(refresh_btn)
        btns.addStretch(1)
        open_btn = QPushButton("打开目录")
        open_btn.clicked.connect(self._open_dir)
        btns.addWidget(open_btn)
        import_btn = QPushButton("导入 gpkg 到地图…")
        import_btn.setToolTip("选包含 .gpkg 的目录，把矢量表与影像加载进当前工程")
        import_btn.clicked.connect(self._import_gpkg_to_map)
        import_btn.hide()  # 暂未开放
        btns.addWidget(import_btn)
        cancel_btn = QPushButton("取消任务")
        cancel_btn.clicked.connect(self._cancel_job)
        btns.addWidget(cancel_btn)
        retry_btn = QPushButton("重试")
        retry_btn.clicked.connect(self._retry_job)
        btns.addWidget(retry_btn)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        btns.addWidget(close_btn)
        layout.addLayout(btns)

        self.setLayout(layout)

    # ------------------------------------------------------------------ 数据

    def _detach_from_parent(self) -> None:
        """脱离父窗口成独立顶层窗口（最小化进任务栏，同 QGIS 弹窗）。"""
        parent = self.parentWidget()
        geo = parent.geometry() if parent is not None else None
        self.setParent(None)
        if geo is not None:
            self.move(
                geo.center().x() - self.width() // 2,
                geo.center().y() - self.height() // 2,
            )

    def refresh(self) -> None:
        records = self.manager.list_records()
        self.table.setRowCount(len(records))
        for row, rec in enumerate(records):
            job_id = rec.get("job_id", "")
            self.table.setItem(row, 0, self._item(rec.get("task_name", ""), job_id))
            self.table.setItem(row, 1, self._item(_status_text(rec), job_id))
            progress = rec.get("progress", 0)
            stage = rec.get("stage", "")
            self.table.setItem(row, 2, self._item(f"{progress}%（{stage}）", job_id))
            self.table.setItem(row, 3, self._item(str(rec.get("zip_name", "")), job_id))
            self.table.setItem(row, 4, self._item(str(rec.get("error") or ""), job_id))
        self.table.resizeColumnsToContents()

    def _item(self, text: str, job_id: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, job_id)
        return item

    def _selected_job(self) -> Optional[str]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    # ------------------------------------------------------------------ 动作

    def _cancel_job(self) -> None:
        job_id = self._selected_job()
        if job_id is None:
            return
        if self.manager.cancel(job_id):
            self.refresh()
        else:
            logger.warning("取消失败：job 不存在 %s", job_id)

    def _retry_job(self) -> None:
        job_id = self._selected_job()
        if job_id is None:
            return
        if not self.service.retry_job(job_id):
            from app.infra.messagebar import send

            send("warning", "重试失败：任务信息缺失或已过期", duration=4)
            return
        self.refresh()

    def _open_dir(self) -> None:
        job_id = self._selected_job()
        if job_id is None:
            return
        rec = self.manager.get_record(job_id)
        if not rec:
            return
        target = str(rec.get("target_dir", ""))
        if not target or not os.path.isdir(target):
            from app.infra.messagebar import send

            send("warning", "目标目录尚不存在（下载可能未完成或已被清理）", duration=4)
            return
        if os.name == "nt":
            os.startfile(target)  # noqa: S606
        else:
            webbrowser.open(target)

    def _import_gpkg_to_map(self) -> None:
        """选目录里第一个 .gpkg，加载矢量 feature 表；若含瓦片表再用 gdal 直开。"""
        from app.infra.messagebar import send

        root = QFileDialog.getExistingDirectory(self, "选择含 .gpkg 的目录")
        if not root:
            return
        gpkgs = [
            os.path.join(root, f)
            for f in os.listdir(root)
            if f.lower().endswith(".gpkg")
        ]
        if not gpkgs:
            send("warning", "目录里没有 .gpkg 文件", duration=4)
            return
        path = gpkgs[0]
        try:
            from app.core.data.local_db import scan_local_sqlite

            rows = scan_local_sqlite(path)
        except Exception as exc:  # noqa: BLE001
            send("critical", f"无法读取 .gpkg：{exc}", duration=6)
            return
        loaded, errors = [], []
        vectors = []
        has_tiles = False
        for row in rows:
            geo = row.get("geo")
            name = row.get("name", "")
            if geo == "影像" or geo == "tiles":
                has_tiles = True
                continue
            if not geo:
                continue
            from app.core.domain.offline_download.offline_load import (
                open_local_db_vector,
            )

            layer = open_local_db_vector(path, name)
            if layer is not None:
                loaded.append(f"矢量 {name}（{layer.featureCount()}）")
                vectors.append(layer)
            else:
                errors.append(f"矢量 {name}")
        if has_tiles:
            from qgis.core import QgsRasterLayer

            layer = QgsRasterLayer(path, os.path.basename(path) + " 影像", "gdal")
            if layer.isValid():
                loaded.append("影像瓦片")
            else:
                errors.append("影像瓦片（gdal 打开失败）")
        if loaded:
            send("success", "已加载：" + "；".join(loaded), duration=6)
        if errors:
            send("warning", "以下未加载：" + "；".join(errors), duration=8)
        _zoom_to_vector_layers(vectors)

    def _debounce_refresh(self, _job_id: str) -> None:
        from qgis.PyQt.QtCore import QTimer

        QTimer.singleShot(300, self._safe_refresh)

    def _safe_refresh(self) -> None:
        try:
            self.refresh()
        except RuntimeError:
            pass  # 对话框已销毁

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self.manager.record_changed.disconnect(self._debounce_refresh)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)


def _zoom_to_vector_layers(layers) -> None:
    """把画布缩放到矢量图层并集范围（找不到画布就跳过）。"""
    if not layers:
        return
    from qgis.core import QgsRectangle
    from qgis.PyQt.QtWidgets import QApplication
    from qgis.gui import QgsMapCanvas

    canvas = None
    for w in QApplication.topLevelWidgets():
        if isinstance(w, QgsMapCanvas):
            canvas = w
            break
    if canvas is None:
        return
    ext = None
    for layer in layers:
        e = layer.extent()
        ext = QgsRectangle(e) if ext is None else ext.combineExtentWith(e)
    if ext is not None and not ext.isNull():
        try:
            canvas.setExtent(ext)
            canvas.refresh()
        except Exception:  # noqa: BLE001
            pass


def _status_text(rec: dict) -> str:
    status = rec.get("status", "")
    return _STATUS_LABELS.get(status, status)
