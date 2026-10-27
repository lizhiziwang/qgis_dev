"""Execute SQL Dialog（ogr SQL 查询 + 结果表）。"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
from typing import Any, List, Optional

from osgeo import ogr
from qgis.core import (
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsMemoryProviderUtils,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import (
    QAbstractTableModel,
    QDate,
    QDateTime,
    QModelIndex,
    QObject,
    Qt,
    QTime,
    QVariant,
    pyqtSignal,
)
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressDialog,
    QPushButton,
    QTableView,
    QTextEdit,
    QVBoxLayout,
)

from app.core.util.local_layer_loader import layer_source_container
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.message_box import MessageBox
from app.ui.dialogs.query_builder import QueryBuilderDialog

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


def _container_path(layer) -> str:
    """矢量图层 → 供 ``ogr.Open`` 的**容器路径**。

    直接交给 :func:`~app.core.util.local_layer_loader.layer_source_container`：
    它会先取加载方挂的 ``layer.path_c``，否则从数据源里剥掉子图层 / provider 选项
    （OGR 形态 ``D:/x.gpkg|layername=t`` 或 spatialite provider 形态
    ``dbname='D:/x.db' table="t" (geom)`` —— 后者的 ``|`` 切法切不出路径）。
    ``ogr.Open`` 只认容器路径，``|layername=`` 是 QGIS provider 选项、GDAL 并不
    认识（直接 Open 会抛 “does not exist … not recognized as a supported
    dataset name”）。
    """
    return layer_source_container(layer)


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


#: 结果超过该行数时，「加载为图层」先确认——内存图层要把要素全部物化，
#: 几十万行会明显吃内存 / 卡顿。
LOAD_LAYER_WARN_ROWS = 200_000

#: OGR 字段类型 → QgsField 类型
_OGR_FIELD_TYPES = {
    ogr.OFTInteger: QVariant.Int,
    ogr.OFTInteger64: QVariant.LongLong,
    ogr.OFTReal: QVariant.Double,
    ogr.OFTString: QVariant.String,
    ogr.OFTDate: QVariant.Date,
    ogr.OFTTime: QVariant.Time,
    ogr.OFTDateTime: QVariant.DateTime,
    ogr.OFTBinary: QVariant.ByteArray,
    # 列表字段 memory provider 支持有限，按 OGR 的字符串表示落成文本
    ogr.OFTIntegerList: QVariant.String,
    ogr.OFTRealList: QVariant.String,
    ogr.OFTStringList: QVariant.String,
}

#: OGR 几何类型（去掉 Z/M 位后的基础类型）→ QgsWkbTypes
_OGR_GEOM_TYPES = {
    ogr.wkbPoint: QgsWkbTypes.Type.Point,
    ogr.wkbLineString: QgsWkbTypes.Type.LineString,
    ogr.wkbPolygon: QgsWkbTypes.Type.Polygon,
    ogr.wkbMultiPoint: QgsWkbTypes.Type.MultiPoint,
    ogr.wkbMultiLineString: QgsWkbTypes.Type.MultiLineString,
    ogr.wkbMultiPolygon: QgsWkbTypes.Type.MultiPolygon,
    ogr.wkbGeometryCollection: QgsWkbTypes.Type.GeometryCollection,
    ogr.wkbCircularString: QgsWkbTypes.Type.CircularString,
    ogr.wkbCompoundCurve: QgsWkbTypes.Type.CompoundCurve,
    ogr.wkbCurvePolygon: QgsWkbTypes.Type.CurvePolygon,
    ogr.wkbMultiCurve: QgsWkbTypes.Type.MultiCurve,
    ogr.wkbMultiSurface: QgsWkbTypes.Type.MultiSurface,
}


def _ogr_base_geom_type(ogr_type: int):
    """OGR 几何类型去掉 25D 标志 / ISO 1000 段后返回基础类型。"""
    t = int(ogr_type)
    if t & ogr.wkb25DBit:
        t &= ~ogr.wkb25DBit
    if t >= 1000:
        t %= 1000
    return t


def _ogr_value(feat, index: int, ogr_type: int, subtype: int):
    """把 OGR 要素的一个字段值转成 QgsFeature 能接受的 Python 值。"""
    if not feat.IsFieldSet(index):
        return None
    if ogr_type in (ogr.OFTInteger, ogr.OFTInteger64):
        if subtype == ogr.OFSTBoolean:
            return bool(feat.GetFieldAsInteger(index))
        return feat.GetField(index)
    if ogr_type == ogr.OFTReal:
        return feat.GetField(index)
    if ogr_type in (ogr.OFTDate, ogr.OFTTime, ogr.OFTDateTime):
        year, month, day, hour, minute, second, _tz = feat.GetFieldAsDateTime(index)
        if ogr_type == ogr.OFTTime:
            return QTime(hour, minute, second)
        date = QDate(year, month, day)
        if not date.isValid():
            return None
        if ogr_type == ogr.OFTDate:
            return date
        return QDateTime(date, QTime(hour, minute, second))
    if ogr_type == ogr.OFTBinary:
        raw = feat.GetFieldAsBinary(index)
        return bytes(raw) if raw is not None else None
    if ogr_type in (ogr.OFTIntegerList, ogr.OFTRealList, ogr.OFTStringList):
        return feat.GetFieldAsString(index)
    return feat.GetField(index)


def build_sql_result_layer(
    path: str,
    sql: str,
    name: str,
    crs,
    *,
    progress=None,
):
    """按 OGR SQL 结果物化一个内存矢量图层（保留几何 / 坐标系 / 字段）。

    复用 :class:`_SqlStreamWorker` 同一套 ``ogr.Open + ExecuteSQL`` 语义，
    但保留几何与字段类型（结果表里只存了显示用的字符串）。结果集完整重读，
    不受 :data:`MAX_RESULT_ROWS` 截断影响。

    Args:
        path: 容器路径（``_container_path``）。
        sql: 已执行的 SQL（不是编辑框里可能被改过的新文本）。
        name: 新图层名。
        crs: 源图层坐标系（结果图层沿用）。
        progress: 可选回调 ``(已完成, 总数) -> bool``；返回 False 表示取消，
            此时返回 ``None``。总数未知时为 0。

    Returns:
        ``(图层, 要素数)``；用户取消时返回 ``None``。

    Raises:
        RuntimeError: 数据源打不开 / SQL 执行失败 / 建图层失败。
    """
    ds = ogr.Open(path, 0)
    if ds is None:
        raise RuntimeError(f"无法打开数据源：{path}")
    result = None
    try:
        result = ds.ExecuteSQL(sql)
        if result is None:
            raise RuntimeError("SQL 执行失败或没有结果")
        defn = result.GetLayerDefn()

        fields = QgsFields()
        field_defs = []  # (字段序号, OGR 类型, 子类型)
        for i in range(defn.GetFieldCount()):
            fd = defn.GetFieldDefn(i)
            ogr_type = fd.GetType()
            subtype = fd.GetSubType()
            field_defs.append((i, ogr_type, subtype))
            qtype = _OGR_FIELD_TYPES.get(ogr_type, QVariant.String)
            if ogr_type == ogr.OFTInteger and subtype == ogr.OFSTBoolean:
                qtype = QVariant.Bool
            fields.append(
                QgsField(
                    fd.GetName(),
                    qtype,
                    "",
                    fd.GetWidth(),
                    fd.GetPrecision(),
                )
            )

        geom_type = _OGR_GEOM_TYPES.get(_ogr_base_geom_type(defn.GetGeomType()))
        if geom_type is None:
            # wkbUnknown：先探一条有几何的要素推断；确实没有几何就是纯属性表
            geom_type = QgsWkbTypes.Type.NoGeometry
            if _ogr_base_geom_type(defn.GetGeomType()) == ogr.wkbUnknown:
                result.ResetReading()
                for feat in result:
                    geom = feat.GetGeometryRef()
                    if geom is not None:
                        geom_type = _OGR_GEOM_TYPES.get(
                            _ogr_base_geom_type(geom.GetGeometryType()),
                            QgsWkbTypes.Type.NoGeometry,
                        )
                        break
                result.ResetReading()

        layer = QgsMemoryProviderUtils.createMemoryLayer(name, fields, geom_type, crs)
        if layer is None:
            raise RuntimeError("创建内存图层失败")

        provider = layer.dataProvider()
        total = max(result.GetFeatureCount(), 0)
        batch = []
        n = 0
        result.ResetReading()
        for feat in result:
            qf = QgsFeature()
            qf.setAttributes(
                [_ogr_value(feat, i, ogr_type, subtype) for i, ogr_type, subtype in field_defs]
            )
            geom = feat.GetGeometryRef()
            if geom is not None:
                try:
                    wkb = geom.ExportToIsoWkb()
                except Exception:  # noqa: BLE001 —— 个别驱动只给 25D WKB
                    wkb = geom.ExportToWkb()
                # QGIS 4 的 QgsGeometry.fromWkb 是实例方法（原地设置），不是静态构造
                qgs_geom = QgsGeometry()
                qgs_geom.fromWkb(wkb)
                if not qgs_geom.isNull():
                    qf.setGeometry(qgs_geom)
            batch.append(qf)
            n += 1
            if len(batch) >= 1000:
                if not provider.addFeatures(batch):
                    raise RuntimeError("写入查询结果要素失败")
                batch = []
                if progress is not None and not progress(n, total):
                    return None
        if batch and not provider.addFeatures(batch):
            raise RuntimeError("写入查询结果要素失败")
        if progress is not None and not progress(n, total):
            return None
        layer.updateExtents()
        layer.updateFields()
        return layer, n
    finally:
        if result is not None:
            ds.ReleaseResultSet(result)
        ds = None


class ExecuteSqlDialog(BaseDialog):
    """执行 ogr SQL 并展示结果。"""

    def __init__(self, layer: QgsVectorLayer, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("执行SQL")
        self.layer = layer
        self._sql_worker: Optional[_SqlStreamWorker] = None
        #: 最近一次真正执行的 SQL（「加载为图层」按它重读，不取编辑框当前文本）
        self._executed_sql = ""
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout()

        sql_frame = QFrame()
        sql_layout = QVBoxLayout(sql_frame)
        self.sql_input = QTextEdit()
        # OGR SQL 在**容器**上执行，FROM 用的是容器内层名；双引号包裹并转义
        # 内部引号，防层名含空格/特殊字符时解析失败
        _tbl = self.layer.name().split(".")[0].replace('"', '""')
        self.sql_input.setText(f'select * from "{_tbl}" limit 1000')
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
        self.subset_filter_input.setPlaceholderText("输入可选的 SQL 过滤条件，或点击右侧按钮打开查询构建器")
        self.subset_filter_btn = QPushButton("...")
        self.subset_filter_btn.setFixedWidth(30)
        self.subset_filter_btn.setToolTip("打开查询构建器")
        sub = QHBoxLayout()
        sub.addWidget(self.subset_filter_input)
        sub.addWidget(self.subset_filter_btn)
        form.addRow("过滤条件", sub)
        self.layer_name_input = QLineEdit()
        self.layer_name_input.setPlaceholderText("留空则自动命名为「图层名_SQL结果」")
        form.addRow("图层名称", self.layer_name_input)
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
            MessageBox.warning(self, "警告", "请输入 SQL 查询！")
            return
        path = _container_path(self.layer)
        # 仅支持本地文件/容器数据源（shp / gpkg / gdb / geojson / 本地库里的 Sqlite
        # 矢量层…）：内存图层、postgres 等连接源 ogr.Open 打不开，弹窗而非抛 GDAL 错。
        # spatialite 也放行 —— 本地库的 SpatiaLite 表走的就是该 provider（见
        # local_layer_loader.open_local_vector_table），容器路径同样是那个 .db 文件。
        if (
            self.layer.providerType() not in ("ogr", "spatialite")
            or not path
            or not (os.path.isfile(path) or os.path.isdir(path))
        ):
            MessageBox.warning(
                self,
                "警告",
                "仅支持本地文件数据源（shp / gpkg / gdb / geojson 等）的 SQL 查询",
            )
            return
        self._table_model.set_schema([])  # 清空旧结果
        self._executed_sql = sql
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
        """把查询结果物化成内存矢量图层加入工程（保留几何 / 坐标系 / 字段）。"""
        if self._sql_worker is not None:
            MessageBox.information(self, "提示", "查询仍在执行，请等待完成后再加载图层")
            return
        total = self._table_model.rowCount()
        if total <= 0:
            MessageBox.information(self, "提示", "请先执行查询并获得结果")
            return
        sql = self._executed_sql
        if not sql:
            MessageBox.information(self, "提示", "请先执行查询并获得结果")
            return
        if total > LOAD_LAYER_WARN_ROWS:
            ret = MessageBox.question(
                self,
                "加载为图层",
                f"查询结果共 {total:,} 条。加载为内存图层需要把全部要素读入内存，"
                "可能耗时较长并占用较多内存。是否继续？",
            )
            if ret != MessageBox.StandardButton.Yes:
                return

        name = self.layer_name_input.text().strip() or f"{self.layer.name()}_SQL结果"
        path = _container_path(self.layer)
        dialog_total = total if total > 0 else 0
        progress = QProgressDialog("正在加载查询结果…", "取消", 0, dialog_total, self)
        progress.setWindowTitle("加载为图层")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(300)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        def _on_progress(done: int, count: int) -> bool:
            if count <= 0:
                progress.setRange(0, 0)  # 总数未知：转圈
            else:
                progress.setValue(min(done, count))
            return not progress.wasCanceled()

        try:
            built = build_sql_result_layer(
                path,
                sql,
                name,
                self.layer.crs(),
                progress=_on_progress,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("加载查询结果为图层失败")
            MessageBox.warning(self, "加载为图层", f"加载失败：{exc}")
            return
        finally:
            progress.close()

        if built is None:  # 用户取消
            self.status_label.setText("已取消加载图层")
            return
        layer, count = built

        subset = self.subset_filter_input.text().strip()
        if subset and not layer.setSubsetString(subset):
            logger.warning("加载图层时过滤条件无效，已忽略：%s", subset)
            MessageBox.warning(
                self,
                "加载为图层",
                f"过滤条件无效，已忽略：\n{subset}",
            )

        QgsProject.instance().addMapLayer(layer)
        self.status_label.setText(f"已加载图层「{name}」，共 {count} 条")
        logger.info("SQL 结果已加载为图层：%s（%s 条）", name, count)

    def _on_open_query_builder(self) -> None:
        from app.ui.common.modeless import show_modeless
        from app.ui.dialogs.query_builder import QueryBuilderDialog

        show_modeless(
            self,
            "execute_sql_query_builder",
            lambda: QueryBuilderDialog(self.layer, parent=self),
            layer=self.layer,
        )

    def sizeHint(self):  # noqa: D401
        return self.minimumSizeHint()
