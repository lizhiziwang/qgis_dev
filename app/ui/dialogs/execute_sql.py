"""Execute SQL Dialog（ogr SQL 查询 + 结果表）。"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
from typing import Any, List, Optional

from osgeo import ogr
from qgis.PyQt.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QTextEdit,
    QVBoxLayout,
)
from qgis.core import QgsVectorLayer

from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog

logger = get_logger(__name__)


class VectorLayerTableModel(QAbstractTableModel):
    """结果表 model（SQLite 落盘 + 可视窗口按需读取，2M+ 行不 OOM）。

    ogr SQL 结果是**只前向**的流，没有按行随机访问能力（无法像属性表那样按
    fid 重取某行）。要做"只渲染可视区域"，先把结果分批写进**临时 SQLite**（磁盘，
    天然随机访问），模型 :meth:`data` 按需 ``SELECT`` 可视行的数据，LRU 缓存
    （``MAX_CACHE_ROWS``，几百行 ≈ 几 MB）随滚动窗口淘汰——内存恒定不随结果
    总量增长，2M+ 行也能完整前后滚动浏览。worker 分批经 :meth:`set_schema` +
    :meth:`append_rows` 投递回主线程（插入在主线程，每批 5000 行事务提交 ~几 ms）。

    ``append_rows`` 只写 sqlite、不再驻留行对象；``rowCount`` 随已落盘行数渐进
    增长（与旧流式 UI 一致）。临时文件由 :meth:`cleanup` 删除（对话框关闭时调用）。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 临时 SQLite 文件（结果落盘，随机访问；随对话框关闭删除）
        fd, self._db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=OFF")  # 一次性临时数据：免 journal
        self._conn.execute("PRAGMA synchronous=OFF")
        self._header: List[str] = []
        #: 已落盘行数（= rowCount；随 append_rows 渐进增长）
        self._n = 0
        #: 可视窗口行缓存（LRU，{row: tuple}）
        self._cache: dict[int, tuple] = {}

    def rowCount(self, parent=QModelIndex()) -> int:
        return self._n

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self._header)

    def _cache_row(self, row: int, data: tuple) -> None:
        """写入 LRU 行缓存，超出 ``MAX_CACHE_ROWS`` 淘汰最久未用的行。"""
        self._cache.pop(row, None)
        self._cache[row] = data
        while len(self._cache) > MAX_CACHE_ROWS:
            self._cache.pop(next(iter(self._cache)))

    def _fetch_row(self, row: int) -> Optional[tuple]:
        """按行号从 SQLite 随机读取一行（0 基 → rowid 1 基）。"""
        if row < 0 or row >= self._n:
            return None
        cur = self._conn.execute("SELECT * FROM t WHERE rowid = ?", (row + 1,))
        return cur.fetchone()

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            row, col = index.row(), index.column()
            row_data = self._cache.get(row)
            if row_data is None:
                row_data = self._fetch_row(row)
                if row_data is None:
                    return None
                self._cache_row(row, row_data)
            if col < len(row_data):
                v = row_data[col]
                return str(v) if v is not None else ""
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self._header[section]
        return None

    def set_schema(self, header: List[str]) -> None:
        """设置列名（首批结果到达时调用；传空列表即清空结果表）。"""
        self.beginResetModel()
        self._header = list(header)
        self._conn.execute("DROP TABLE IF EXISTS t")
        if self._header:
            cols = ",".join(f"c{i} TEXT" for i in range(len(self._header)))
            self._conn.execute(f"CREATE TABLE t ({cols})")
        self._n = 0
        self._cache.clear()
        self.endResetModel()

    def append_rows(self, rows: List[List[Any]]) -> None:
        """把一批结果行写进 SQLite（后台 worker 经信号投递回主线程）。

        只落盘、不驻留行对象：内存不受结果总量影响（见类 docstring）。
        """
        if not rows or not self._header:
            return
        ncols = len(self._header)
        values = [tuple(list(r[:ncols]) + [None] * (ncols - len(r))) for r in rows]
        self._conn.executemany(
            f"INSERT INTO t VALUES ({','.join('?' * ncols)})", values
        )
        self._conn.commit()
        start = self._n
        self._n += len(rows)
        self.beginInsertRows(QModelIndex(), start, start + len(rows) - 1)
        self.endInsertRows()

    def cleanup(self) -> None:
        """关闭数据库连接并删除临时文件（对话框关闭时调用，幂等）。"""
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            os.remove(self._db_path)
        except OSError:
            pass


#: 后台工作线程每攒够这么多行就冲刷一批（结果渐进显示，避免一次性内存峰值）
STREAM_CHUNK = 5000
#: 冲刷时间下限（秒）：单条行转换慢时仍能按时刷新状态栏
FLUSH_INTERVAL = 0.1
#: 模型按需读取的行缓存上限。QTableView 只渲染可视行（几百行），缓存 500 行
#: ≈ 几 MB，随滚动窗口淘汰——内存不随结果总量增长（2M+ 行不会 OOM）。
MAX_CACHE_ROWS = 500
#: 结果**落盘**上限（临时 SQLite 文件）：超过即截断，仅防失控结果（如意外
#: 笛卡尔积）把磁盘写爆。内存不受此限制（可视窗口缓存恒定），正常 2M+ 不触发。
MAX_RESULT_ROWS = 5_000_000


class _SqlStreamWorker(QObject):
    """后台执行 ogr SQL 并分批回传结果行的工作器。

    2M+ 行结果不能在主线程一次性迭代构建列表（卡死 UI + 内存峰值）。全程在
    普通 :class:`threading.Thread` 里执行：ogr.Open / ExecuteSQL / 逐行转换
    都在工作线程内完成并释放（ogr 数据集不能跨线程使用）。结果经信号投递到
    :class:`VectorLayerTableModel` 落盘（SQLite），内存不随总量增长；超过
    ``MAX_RESULT_ROWS`` 时截断（仅防失控结果把磁盘写爆）。
    """

    chunk_ready = pyqtSignal(object, list, list)  #: (worker, 列名, 一批行)
    finished = pyqtSignal(object, int, bool)  #: (worker, 已获取总行数, 是否截断)
    failed = pyqtSignal(object, str)  #: (worker, 错误信息)

    def __init__(self, path: str, sql: str) -> None:
        super().__init__()
        self.path = path
        self.sql = sql
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="sql-stream", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self) -> None:
        ds = None
        try:
            ds = ogr.Open(self.path, 0)
            if ds is None:
                self.failed.emit(self, f"无法打开数据源：{self.path}")
                return
            v_layer = None
            try:
                v_layer = ds.ExecuteSQL(self.sql)
                if v_layer is None:
                    self.finished.emit(self, 0, False)  # SQL 合法但无结果
                    return
                defn = v_layer.GetLayerDefn()
                columns = []
                for i in range(defn.GetFieldCount()):
                    name = defn.GetFieldDefn(i).GetName()
                    if name.upper() in ("SHAPE", "GEOMETRY", "WKB_GEOMETRY", "WKT_GEOMETRY"):
                        continue
                    columns.append(name)
                chunk: List[List[Any]] = []
                v_layer.ResetReading()
                last_flush = time.monotonic()
                n = 0
                truncated = False
                for feat in v_layer:
                    if self._cancel.is_set():
                        break
                    row = []
                    for c in columns:
                        v = feat.GetFieldAsString(c)
                        row.append(v if v is not None else "")
                    chunk.append(row)
                    n += 1
                    if len(chunk) >= STREAM_CHUNK or time.monotonic() - last_flush >= FLUSH_INTERVAL:
                        self.chunk_ready.emit(self, list(columns), chunk)
                        chunk = []
                        last_flush = time.monotonic()
                    # 落盘上限：达到即停（防失控结果把磁盘写爆）
                    if n >= MAX_RESULT_ROWS:
                        truncated = True
                        break
                if chunk and not self._cancel.is_set():
                    self.chunk_ready.emit(self, list(columns), chunk)
                if not self._cancel.is_set():
                    self.finished.emit(self, n, truncated)
            finally:
                if v_layer is not None:
                    ds.ReleaseResultSet(v_layer)  # 不释放会泄漏结果集
        except Exception as e:  # noqa: BLE001
            self.failed.emit(self, str(e))
        finally:
            ds = None


class ExecuteSqlDialog(BaseDialog):
    """执行 ogr SQL 并展示结果。"""

    def __init__(self, layer: QgsVectorLayer, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("执行SQL")
        self.layer = layer
        self._sql_worker: Optional[_SqlStreamWorker] = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout()

        sql_frame = QFrame()
        sql_layout = QVBoxLayout(sql_frame)
        self.sql_input = QTextEdit()
        self.sql_input.setText(f"select * from {self.layer.name().split('.')[0]} limit 1000")
        self.sql_input.setMinimumHeight(40)
        self.sql_input.setMaximumHeight(150)
        sql_layout.addWidget(self.sql_input)
        layout.addWidget(sql_frame)

        ctrl = QHBoxLayout()
        self.execute_btn = QPushButton("执行查询")
        self.stop_btn = QPushButton("停止")
        self.status_label = QLabel("")
        ctrl.addWidget(self.execute_btn)
        ctrl.addWidget(self.stop_btn)
        ctrl.addWidget(self.status_label)
        ctrl.addStretch()
        layout.addLayout(ctrl)

        self.result_table = QTableView()
        self.result_table.setAlternatingRowColors(True)
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        self._table_model = VectorLayerTableModel(self)
        self.result_table.setModel(self._table_model)
        layout.addWidget(self.result_table)

        load_group = QGroupBox("加载为图层")
        load_layout = QVBoxLayout(load_group)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.subset_filter_input = QLineEdit()
        self.subset_filter_input.setPlaceholderText("Enter the optional SQL filter or click on the button to open the query builder tool")
        self.subset_filter_btn = QPushButton("...")
        self.subset_filter_btn.setFixedWidth(30)
        sub = QHBoxLayout()
        sub.addWidget(self.subset_filter_input)
        sub.addWidget(self.subset_filter_btn)
        form.addRow("Subset filter", sub)
        self.layer_name_input = QLineEdit()
        form.addRow("Layer name", self.layer_name_input)
        load_layout.addLayout(form)
        load_btn = QPushButton("加载图层")
        load_btn.clicked.connect(self._on_load_layer)
        btn_h = QHBoxLayout()
        btn_h.addStretch()
        btn_h.addWidget(load_btn)
        load_layout.addLayout(btn_h)
        layout.addWidget(load_group)

        self.execute_btn.clicked.connect(self._on_execute)
        self.stop_btn.clicked.connect(self._on_stop)
        self.subset_filter_btn.clicked.connect(self._on_open_query_builder)
        self.setLayout(layout)

    def _on_execute(self) -> None:
        # 上一次查询还在跑：先取消
        if self._sql_worker is not None:
            self._sql_worker.cancel()
            self._sql_worker = None
        sql = self.sql_input.toPlainText().strip()
        if not sql:
            QMessageBox.warning(self, "警告", "请输入 SQL 查询！")
            return
        path = self.layer.path_c
        self._table_model.set_schema([])  # 清空旧结果
        self.execute_btn.setEnabled(False)
        self.execute_btn.setText("查询中…")
        self.status_label.setText("正在执行 SQL…")
        worker = _SqlStreamWorker(path, sql)
        worker.chunk_ready.connect(self._on_chunk)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        self._sql_worker = worker
        worker.start()

    def _on_chunk(self, worker, columns: List[str], rows: List[List[Any]]) -> None:
        if worker is not self._sql_worker:
            return  # 过期查询的在途批次，丢弃
        if not self._table_model.columnCount():
            self._table_model.set_schema(columns)
        self._table_model.append_rows(rows)
        self.status_label.setText(f"已获取 {self._table_model.rowCount()} 条…")

    def _on_finished(self, worker, n: int, truncated: bool) -> None:
        if worker is not self._sql_worker:
            return
        self._sql_worker = None
        self.execute_btn.setEnabled(True)
        self.execute_btn.setText("执行查询")
        if truncated:
            self.status_label.setText(
                f"结果超过 {MAX_RESULT_ROWS} 条，已显示前 {MAX_RESULT_ROWS} 条（请在 SQL 中加 LIMIT 或条件缩小范围）"
            )
        elif n:
            self.status_label.setText(f"执行完成，共 {n} 条")
        else:
            self.status_label.setText("执行完成，无结果")

    def _on_failed(self, worker, msg: str) -> None:
        if worker is not self._sql_worker:
            return
        self._sql_worker = None
        self.execute_btn.setEnabled(True)
        self.execute_btn.setText("执行查询")
        self.status_label.setText("执行失败")
        logger.error("SQL 执行失败: %s", msg)

    def _on_stop(self) -> None:
        if self._sql_worker is not None:
            self._sql_worker.cancel()
            self._sql_worker = None
            self.execute_btn.setEnabled(True)
            self.execute_btn.setText("执行查询")
            self.status_label.setText("已停止")

    def closeEvent(self, event) -> None:
        """关闭时取消仍在后台跑 SQL 的线程，并删除结果落盘的临时 SQLite 文件。"""
        if self._sql_worker is not None:
            self._sql_worker.cancel()
            self._sql_worker = None
        self._table_model.cleanup()
        super().closeEvent(event)

    def _on_load_layer(self) -> None:
        pass

    def _on_open_query_builder(self) -> None:
        from app.ui.dialogs.query_builder import QueryBuilderDialog
        dlg = QueryBuilderDialog(self.layer, parent=self)
        dlg.exec()

    def sizeHint(self):  # noqa: D401
        return self.minimumSizeHint()
