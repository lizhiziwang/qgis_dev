"""动态加载属性表 Dock。

迁自 :class:`component.map_tool.PagedAttributeDockWidget` 等 5 个类。"""

from __future__ import annotations

import threading
import time
from typing import List, Optional

from qgis.core import (
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QTimer, QVariant, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDockWidget,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.editing import LayerEditService
from app.infra.logging import get_logger
from app.ui.common import tool_icons
from app.ui.common.icons import Icons
from app.ui.common.theme import ThemeManager
from app.ui.dialogs.organize_columns import OrganizeColumnsDialog
from app.ui.dialogs.query_builder import QueryBuilderDialog
from app.ui.tools.feature_attrs import field_alias

logger = get_logger(__name__)

#: 超过该数量才启用分批动态加载；≤ 阈值时一次性全量加载（避免小图层分页闪烁）
DYNAMIC_LOAD_THRESHOLD = 1000
#: ``show_ids`` 走 ``setFilterFids`` 的上限。OGR/GPKG 下大 fid 集合会退化成
#: ``O(n×m)`` 的逐行判定（实测 2 万要素 / 4148 个 id >2min），超过该值时
#: 改为“全表扫描 + python set 判定”（同一图层 <1s）。
FID_FILTER_MAX = 1000
#: 大图层每批固定加载条数（首屏即 2000，滚动/「加载更多」后续批同量）。
#: 用固定值而不是总量的百分比：500w 级图层按 20% 算首屏就是 100w 条，一样卡死。
STREAM_BATCH = 2000
#: 后台工作线程每攒够这么多条就冲刷一批信号（保证过滤时 UI 渐进显示）
CHUNK_FLUSH = 500
#: 冲刷时间下限（秒）：匹配密集但单条 ``next()`` 慢时仍能按时刷新
FLUSH_INTERVAL = 0.1
#: 结果要素 **fid 列表** 的上限。模型只存 fid 不存要素（2M 条 ≈ 70MB，
#: 见 :meth:`PagedAttributeTableModel.feature_at` 的可视窗口缓存）。超过即
#: 截断加载，仅作防失控兜底（正常 2M+ 图层不会触发）。
MAX_MODEL_FIDS = 5_000_000
#: 按需取要素的 LRU 缓存上限。QTableView 只渲染可视行（几百行），缓存
#: 2000 个要素 ≈ 几 MB，随滚动窗口淘汰，内存不随结果总量增长。
MAX_FEATURE_CACHE = 2000


class FeatureStreamWorker(QObject):
    """后台流式迭代要素的工作器（一个线程 = 一段流式会话）。

    在主线程创建，用普通 :class:`threading.Thread` 执行——不用
    :class:`QgsTask`，见 ``app/core/domain/imagery_tiles/task.py`` 的踩坑：
    QgsTask 后台线程跑会原生崩溃（退出码 127）。

    - 迭代器在**工作线程内**打开/消费/关闭，不在线程间传递（最安全）。
    - 结果只回传 **fid 列表**（``chunk_ready``），不存要素：模型按需再取
      可视窗口里的要素（见 :meth:`PagedAttributeTableModel.feature_at`）。
    - 到达当前 ``target`` 暂停，等下一次 :meth:`request_more` 唤醒；迭代器
      耗尽发 :attr:`finished`；:meth:`cancel` 快速退出并 close 迭代器
      （释放 QGIS 读锁）。
    - ``epoch`` 供主线程丢弃过期信号（重置/换筛选后旧 worker 的在途信号）。
    """

    chunk_ready = pyqtSignal(object, object)  #: (worker, list[int] fid)
    finished = pyqtSignal(object, int)  #: (worker, 真实总数)
    #: 到达当前目标后暂停（等待下一次 request_more），供主线程退出"正在加载"态
    paused = pyqtSignal(object)

    def __init__(self, layer: QgsVectorLayer, where: str, show_ids: Optional[list], epoch: int) -> None:
        super().__init__()
        self.layer = layer
        self.where = where or "1=1"
        self.show_ids = list(show_ids) if show_ids else []
        self.epoch = epoch
        self._target = 0
        self._sent = 0
        self._cancel = threading.Event()
        self._go = threading.Event()
        self._thread: Optional[threading.Thread] = None
        #: 大批 ``show_ids`` 改在 python 侧按 set 判定（见 :meth:`_prepare_filter`）
        self._id_set = set(self.show_ids)
        self._filter_ids_py = False
        #: 批次边界探测暂存的 1 条要素（多取 1 条用于判断是否已耗尽）
        self._stashed: Optional[QgsFeature] = None
        #: show_ids + 表达式组合时，表达式在 python 侧逐条求值（见 _prepare_filter）
        self._expr: Optional[QgsExpression] = None
        self._expr_ctx: Optional[QgsExpressionContext] = None

    # -------------------------------------------------------- 主线程 API

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="attr-stream", daemon=True)
        self._thread.start()

    def request_more(self, target: int) -> None:
        """把加载目标抬高到 ``target`` 并唤醒工作线程（无操作安全）。"""
        self._target = max(self._target, target)
        self._go.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._go.set()

    # -------------------------------------------------------- 工作线程

    def _prepare_filter(self) -> QgsFeatureRequest:
        """构造请求。

        两条路径：

        * ``show_ids`` 较小（≤ :data:`FID_FILTER_MAX`）→ 走 ``setFilterFids`` 快路径；
          与表达式组合时以 fids 为主过滤，表达式在 :meth:`_next_accepted` 里逐条求值
          （本构建 QGIS 4.2 里 ``setFilterFids`` 与 ``setFilterExpression`` 不能组合）。
        * ``show_ids`` 很大 → OGR/GPKG 的 fid 过滤会退化成 ``O(n×m)``，改为取全表
          （或已按表达式过滤的结果），在 :meth:`_next_accepted` 里按 python set 丢弃
          非命中 fid（只取 fid + ``NoGeometry``，开销很小）。
        """
        request = QgsFeatureRequest()
        has_expr = self.where.strip() not in ("", "1=1")
        # 大批 show_ids 不用 ``setFilterFids``：OGR/GPKG 后端会退化成 O(n×m)，
        # 改为取全表（或已按表达式过滤的结果）后在 :meth:`_next_accepted` 里
        # 按 python set 丢弃非命中 fid（只取 fid + NoGeometry，开销很小）。
        use_fid_filter = bool(self.show_ids) and len(self.show_ids) <= FID_FILTER_MAX
        self._filter_ids_py = bool(self.show_ids) and not use_fid_filter
        if use_fid_filter and has_expr:
            request.setFilterFids(self.show_ids)
            self._expr = QgsExpression(self.where)
            self._expr_ctx = QgsExpressionContext()
            self._expr_ctx.appendScopes(
                QgsExpressionContextUtils.globalProjectLayerScopes(self.layer)
            )
            self._expr.prepare(self._expr_ctx)
        else:
            if has_expr:
                request.setFilterExpression(self.where)
            if use_fid_filter:
                request.setFilterFids(self.show_ids)
            # 非组合路径：QGIS 内部完成过滤，我们只取 fid → 裁掉几何与属性，
            # 迭代快得多（不构造 geometry/不物化属性值），每要素几字节。
            request.setFlags(QgsFeatureRequest.Flag.NoGeometry)
            request.setNoAttributes()
        return request

    def _next_accepted(self, iterator):
        """拉取下一条通过过滤的要素；耗尽抛 StopIteration。

        无 python 侧过滤时直接返回迭代器的下一条（零开销快路径）；否则先按
        ``_id_set`` 丢弃非命中 fid，再按表达式求值。
        """
        while True:
            feat = next(iterator)
            if self._filter_ids_py and feat.id() not in self._id_set:
                continue
            if self._expr is None:
                return feat
            self._expr_ctx.setFeature(feat)
            if bool(self._expr.evaluate(self._expr_ctx)):
                return feat

    def _run(self) -> None:
        iterator = None
        exhausted = False
        pending: List[int] = []
        try:
            request = self._prepare_filter()
            iterator = self.layer.getFeatures(request)
            last_flush = time.monotonic()
            while not self._cancel.is_set():
                self._go.wait()
                if self._cancel.is_set():
                    break
                self._go.clear()
                # 上一轮暂停时暂存了 1 条"探测"要素，先入队（只取 fid）
                if self._stashed is not None:
                    pending.append(self._stashed.id())
                    self._stashed = None
                while self._sent + len(pending) < self._target and not self._cancel.is_set():
                    try:
                        pending.append(self._next_accepted(iterator).id())
                    except StopIteration:
                        exhausted = True
                        break
                    if len(pending) >= CHUNK_FLUSH or time.monotonic() - last_flush >= FLUSH_INTERVAL:
                        self._flush(pending)
                        pending = []
                        last_flush = time.monotonic()
                # 批次边界探测：多取 1 条判断是否已耗尽。停在整批倍数（如
                # 120 条/批 24）的图层若不探测，暂停处永远不知道已加载完，
                # has_more 会一直为 True。探测到的要素暂存到下一轮。
                if not exhausted and not self._cancel.is_set():
                    try:
                        self._stashed = self._next_accepted(iterator)
                    except StopIteration:
                        exhausted = True
                self._flush(pending)
                pending = []
                if exhausted or self._cancel.is_set():
                    break
                self.paused.emit(self)
        except Exception:  # noqa: BLE001
            # 迭代中异常（图层被外部改动使游标失效）：按耗尽收尾，避免死循环
            exhausted = True
        finally:
            if iterator is not None:
                try:
                    iterator.close()
                except Exception:  # noqa: BLE001
                    pass
        if exhausted and not self._cancel.is_set():
            self.finished.emit(self, self._sent)

    def _flush(self, pending: List[int]) -> None:
        if not pending:
            return
        self._sent += len(pending)
        self.chunk_ready.emit(self, list(pending))


class PagedAttributeTableModel(QAbstractTableModel):
    """动态加载属性表 model（后台线程流式实现）。

    总量 **大于** ``DYNAMIC_LOAD_THRESHOLD``（默认 1000）时分批动态加载：
    :class:`FeatureStreamWorker` 在工作线程从迭代器续取，每批冲刷
    ``CHUNK_FLUSH`` 条经信号投递回主线程（首屏即 ``STREAM_BATCH``）。总量
    ≤ 阈值时一次性同步全量加载（避免小图层线程抖动）。

    过滤/show_ids 查询的总数在流式结束前未知（``total_known=False``），迭代器
    耗尽后由 ``finished`` 信号补齐。``show_ids`` 子集改用 ``setFilterFids``
    快路径——实测 ``$id in (N 个 id)`` 表达式是 O(要素数 × N)：30 万图层 +
    1000 个 fids 要 40 秒，直接卡死。

    内存策略：模型**只存结果 fid 列表**（2M 条 ≈ 70MB），不驻留要素对象；
    表格渲染按需经 :meth:`feature_at` 取可视行对应的要素，用 LRU 缓存
    （``MAX_FEATURE_CACHE``，几 MB）按滚动窗口淘汰——内存不随结果总量增长，
    200w+ 也能完整浏览全部结果，不会溢出。
    """

    state_changed = pyqtSignal()

    def __init__(self, layer: QgsVectorLayer, page_size: int = 50, filter_str: str = "1=1", show_ids: Optional[list] = None) -> None:
        super().__init__()
        self.layer = layer
        # page_size 仅保留兼容旧调用（main_window 传 50），动态加载不再按页
        self.page_size = max(1, int(page_size))
        self.filter_str = filter_str or "1=1"
        self.show_ids = list(show_ids) if show_ids else []
        self.field_names = [f.name() for f in layer.fields()]
        # 列头显示名：优先字段别名，无别名降级为字段名（attributeAlias 返回空串即无）
        self.header_names = [
            field_alias(layer.fields().at(i)) or name for i, name in enumerate(self.field_names)
        ]
        #: 结果 **fid 列表**（内存主驻留物；要素按需取，见 ``feature_at``）
        self._fids: List[int] = []
        #: 按需取要素的 LRU 缓存（可视窗口内的要素，滚动淘汰，见 ``_cache_feature``）
        self._feature_cache: dict[int, QgsFeature] = {}
        #: 每批加载条数：≤ 阈值时 = 总量（一次全量）；否则固定 STREAM_BATCH
        self.batch_size = 0
        #: 已加载条数（= _fids 长度；为 0 表示尚未加载）
        self.loaded_count = 0
        #: 总数是否已确知。无过滤（1=1）时 featureCount() 直接给出；
        #: show_ids 仅自身时 len() 直接给出；带过滤时需流式结束后补齐
        self.total_known = False
        self.total_features = 0
        #: 后台线程正在扫描中（供状态栏显示"正在加载"）
        self.scanning = False
        self._worker: Optional[FeatureStreamWorker] = None
        #: 世代号：set_filter/取消时递增，用于丢弃过期 worker 的在途信号
        self._epoch = 0
        self._exhausted = False
        #: 结果超过 ``MAX_MODEL_FIDS`` 已截断（停止加载更多，状态栏提示）
        self.truncated = False
        self.where = self._build_where()
        self._refresh_totals()
        self._start_stream()

    def _build_where(self) -> str:
        """筛选表达式（show_ids 走 setFilterFids，不拼进表达式）。"""
        return self.filter_str or "1=1"

    @property
    def has_more(self) -> bool:
        """是否还有未加载的要素（流式未结束即视为有）。"""
        return not self._exhausted

    def _fast_total(self):
        """总数快路径：show_ids 仅自身 → len；无过滤表达式 → featureCount()。

        :return: ``(total, known)``。带过滤/show_ids 组合时总量必须流式结束后才知道。
        """
        if self.show_ids and self.filter_str.strip() in ("", "1=1"):
            return len(self.show_ids), True
        if self.where.strip() in ("", "1=1"):
            try:
                return int(self.layer.featureCount()), True
            except Exception:  # noqa: BLE001
                pass
        return 0, False

    def _refresh_totals(self) -> None:
        """重置流式状态（数据/筛选变化后调用）：快路径总数 + 批次大小。"""
        self.total_features, self.total_known = self._fast_total()
        self.loaded_count = 0
        self._fids = []
        self._feature_cache.clear()
        self.truncated = False
        # 小图层一次全量，避免线程抖动；大图层/未知总数用固定批次
        self.batch_size = (
            self.total_features
            if self.total_known and self.total_features <= DYNAMIC_LOAD_THRESHOLD
            else STREAM_BATCH
        )

    def _start_stream(self) -> None:
        """同步/流式加载决策：已知空图层标记耗尽；小图层同步全量；否则后台 worker。"""
        self._epoch += 1
        self.scanning = False
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        if self.total_known and self.total_features == 0:
            self._exhausted = True
            self.state_changed.emit()
            return
        if self.total_known and self.total_features <= DYNAMIC_LOAD_THRESHOLD:
            self._exhausted = False
            self._sync_load_all()
            self._exhausted = True
            self.state_changed.emit()
            return
        # 流式路径：后台线程分批加载
        self._exhausted = False
        worker = FeatureStreamWorker(self.layer, self.where, self.show_ids, epoch=self._epoch)
        worker.chunk_ready.connect(self._on_chunk)
        worker.finished.connect(self._on_finished)
        worker.paused.connect(self._on_paused)
        worker.start()
        self._worker = worker
        self.scanning = True
        self.state_changed.emit()
        worker.request_more(self.batch_size)

    def _sync_load_all(self) -> None:
        """小图层一次全量加载（同步，只取 fid）。失败（如游标失效）按空处理。"""
        try:
            request = QgsFeatureRequest()
            if self.where.strip() not in ("", "1=1"):
                # 顺序：表达式先、fids 后（setFilterExpression 会清空 fids）
                request.setFilterExpression(self.where)
            if self.show_ids:
                request.setFilterFids(self.show_ids)
            # 同步路径只可能是无过滤或纯 show_ids（带过滤组合总数未知会走流式），
            # 不需要属性/几何，裁掉以加速
            request.setFlags(QgsFeatureRequest.Flag.NoGeometry)
            request.setNoAttributes()
            self._insert_fids([f.id() for f in self.layer.getFeatures(request)])
        except Exception:  # noqa: BLE001
            pass

    def load_more(self) -> None:
        """追加加载下一批：流式路径异步（抬高 worker 目标）；同步路径已全量。"""
        if self._exhausted:
            return
        if self.loaded_count >= MAX_MODEL_FIDS:
            self._truncate()
            return
        if self._worker is not None:
            self.scanning = True
            self.state_changed.emit()
            self._worker.request_more(self.loaded_count + self.batch_size)

    def _truncate(self) -> None:
        """结果过多：取消后台流式并标记截断（状态栏提示，不再加载更多）。"""
        if self.truncated:
            return
        self.truncated = True
        self.cancel_stream()
        self.state_changed.emit()

    def cancel_stream(self) -> None:
        """取消后台流式加载（编辑开始 / dock 关闭 / 换图层时调用）。

        取消后不再自动续取：编辑态下 QGIS 写锁会被后台读锁阻塞。
        """
        self._epoch += 1
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        self.scanning = False
        self._exhausted = True

    def set_filter(self, filter_str: str) -> None:
        """更换筛选表达式并刷新（重开迭代器，回到首屏首批）。"""
        self.filter_str = filter_str or "1=1"
        self.where = self._build_where()
        self.beginResetModel()
        self._refresh_totals()
        self.endResetModel()
        self._start_stream()

    def reload_fields(self) -> None:
        """字段计算器改动图层字段后刷新列定义与已渲染值。

        只重读字段名/列头并清要素缓存后原地 reset：**不重流 fids**（加字段
        不改要素数/顺序，500w 图层也只需重取可视行，无额外全表扫描）。
        新增字段 → ``columnCount``/列头更新；仅更新已有字段值 → 可视行从
        图层重取新值（编辑缓冲内的值同样可见）。
        """
        self.field_names = [f.name() for f in self.layer.fields()]
        # 列头显示名：优先字段别名，无别名降级为字段名（同 __init__ 逻辑）
        self.header_names = [
            field_alias(self.layer.fields().at(i)) or name for i, name in enumerate(self.field_names)
        ]
        self._feature_cache.clear()
        self.beginResetModel()
        self.endResetModel()

    # ------------------------------------------------- worker 信号槽（主线程）

    def _on_chunk(self, worker, fids: List[int]) -> None:
        if worker is not self._worker:
            return  # 过期会话的在途信号，丢弃
        self._insert_fids(fids)
        self.state_changed.emit()

    def _on_paused(self, worker) -> None:
        if worker is not self._worker:
            return
        self.scanning = False
        self.state_changed.emit()

    def _on_finished(self, worker, total: int) -> None:
        if worker is not self._worker:
            return
        self.total_features = total
        self.total_known = True
        self._exhausted = True
        self.scanning = False
        self.state_changed.emit()

    def _insert_fids(self, chunk: List[int]) -> None:
        if not chunk:
            return
        # 兜底上限：fid 列表超预算时截断插入，随后取消后台流式
        room = MAX_MODEL_FIDS - len(self._fids)
        if room <= 0:
            self._truncate()
            return
        if len(chunk) > room:
            chunk = chunk[:room]
        self.beginInsertRows(QModelIndex(), self.loaded_count, self.loaded_count + len(chunk) - 1)
        self._fids.extend(chunk)
        self.endInsertRows()
        self.loaded_count += len(chunk)
        if len(self._fids) >= MAX_MODEL_FIDS:
            self._truncate()

    @property
    def fids(self) -> List[int]:
        """已加载结果的 fid 列表（外部/测试只读）。"""
        return self._fids

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._fids)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.field_names) + 1

    def _cache_feature(self, fid: int, feat: QgsFeature) -> None:
        """写入 LRU 缓存，超出 ``MAX_FEATURE_CACHE`` 淘汰最久未用的要素。

        可视窗口几百行、缓存 2000 个要素 ≈ 几 MB：滚动时旧窗口要素被淘汰，
        内存不随结果总量增长（QGIS 自带属性表同样用这种按需缓存）。
        """
        self._feature_cache.pop(fid, None)
        self._feature_cache[fid] = feat
        while len(self._feature_cache) > MAX_FEATURE_CACHE:
            self._feature_cache.pop(next(iter(self._feature_cache)))

    def feature_at(self, row: int) -> Optional[QgsFeature]:
        """取第 ``row`` 行的要素：按 fid 从图层按需取回，LRU 缓存可视窗口。

        QTableView 渲染只调可视行，滚动出窗口的要素被淘汰——2M+ 结果内存恒定。
        要素在图层里被删除时返回 None。
        """
        if row < 0 or row >= len(self._fids):
            return None
        fid = self._fids[row]
        feat = self._feature_cache.get(fid)
        if feat is not None:
            return feat
        feat = self.layer.getFeature(fid)
        if feat is None or not feat.isValid():
            return None
        self._cache_feature(fid, feat)
        return feat

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._fids):
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if index.column() == 0:
                return self._fids[index.row()]
            feature = self.feature_at(index.row())
            if feature is None:
                return None
            field_idx = index.column() - 1
            if field_idx < len(feature.fields()):
                return feature.attribute(field_idx)
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            if section == 0:
                return "ID"
            if section - 1 < len(self.header_names):
                return self.header_names[section - 1]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.ItemIsEnabled
        if index.column() == 0:
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if self.layer.isEditable():
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        if index.column() == 0:
            return False
        if index.row() >= len(self._fids):
            return False
        fid = self._fids[index.row()]
        feature = self.feature_at(index.row())
        if feature is None:
            return False
        col = index.column() - 1
        if col >= len(feature.fields()):
            return False
        field: QgsField = self.layer.fields().at(col)
        if field.type() == QVariant.Int:
            try:
                value = int(value)
            except (ValueError, TypeError):
                return False
        elif field.type() == QVariant.Double:
            try:
                value = float(value)
            except (ValueError, TypeError):
                return False
        success = self.layer.changeAttributeValue(fid, col, value)
        if success:
            # 缓存里的旧要素失效：弹出，下次渲染按需重取新值
            self._feature_cache.pop(fid, None)
            self.dataChanged.emit(index, index, [role])
            return True
        return False


class PagedAttributeTableWidget(QWidget):
    """可停靠的属性表 widget。"""

    zoom_to_map = pyqtSignal(str, object)

    def __init__(self, layer, page_size: int = 50, parent=None, filter_str: str = "1=1", show_ids: Optional[list] = None) -> None:
        super().__init__(parent)
        self.parent = parent
        self.layer = layer
        self.page_size = page_size
        self.filter_str = filter_str
        self.show_ids = show_ids
        self.model: Optional[PagedAttributeTableModel] = None
        #: 「组织列」隐藏的字段名（按名存，模型 reset/新增字段后仍能对上）
        self._hidden_field_names: set[str] = set()
        self.editing_service = LayerEditService()
        self._setup_ui()

    def _setup_ui(self) -> None:
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(2)

        # 工具条
        tb = QHBoxLayout()
        self.start_btn = QPushButton()
        self.start_btn.setIcon(Icons.edit())
        self.start_btn.setToolTip("开始编辑")
        self.start_btn.setIconSize(self.start_btn.iconSize())
        self.start_btn.setFixedSize(26, 26)
        self.save_btn = QPushButton()
        self.save_btn.setEnabled(False)
        self.save_btn.setIcon(Icons.save_edit())
        self.save_btn.setToolTip("保存编辑")
        self.save_btn.setFixedSize(26, 26)
        self.stop_btn = QPushButton()
        self.stop_btn.setEnabled(False)
        self.stop_btn.setIcon(Icons.stop_edit())
        self.stop_btn.setToolTip("停止编辑")
        self.stop_btn.setFixedSize(26, 26)
        self.filter_btn = QPushButton()
        self.filter_btn.setIcon(Icons.filter())
        self.filter_btn.setToolTip("属性筛选")
        self.filter_btn.setFixedSize(26, 26)
        self.calc_btn = QPushButton()
        self.calc_btn.setObjectName("fieldCalcBtn")
        self.calc_btn.setIcon(Icons.field_calc())
        self.calc_btn.setToolTip("字段计算器")
        self.calc_btn.setFixedSize(26, 26)
        self.columns_btn = QPushButton()
        self.columns_btn.setObjectName("organizeColumnsBtn")
        self.columns_btn.setIcon(
            tool_icons.icon("organize_columns", ThemeManager.instance().current().toolbar_text)
        )
        self.columns_btn.setToolTip("组织列")
        self.columns_btn.setFixedSize(26, 26)
        self.start_btn.clicked.connect(self._start_edit)
        self.save_btn.clicked.connect(self._save_edit)
        self.stop_btn.clicked.connect(self._stop_edit)
        self.filter_btn.clicked.connect(self._filter)
        self.calc_btn.clicked.connect(self._field_calc)
        self.columns_btn.clicked.connect(self._organize_columns)
        for b in (self.start_btn, self.save_btn, self.stop_btn, self.filter_btn, self.calc_btn, self.columns_btn):
            tb.addWidget(b)
        tb.addStretch(0)
        main_layout.addLayout(tb)

        # 表格
        self.table_view = QTableView()
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.table_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        header = self.table_view.horizontalHeader()
        # 列宽以"字符"为单位：以当前字体全角字符宽为基准，默认列宽 8 字符、
        # 最小 4 字符（防止列被拖拽到消失）。用全角"中"作基准保证默认宽度下
        # 中文字符 / ASCII 编码值都能完整显示。
        _char_w = max(1, self.table_view.fontMetrics().horizontalAdvance("中"))
        header.setMinimumSectionSize(_char_w * 4)
        # 注意：不设 setMaximumSectionSize —— 它会把**用户手动拖拽**也一起卡死在
        # 8 字符内。默认 8 字符只是初始宽度，手动调宽应当自由。
        # Interactive：用户可拖拽列边界手动调宽（Stretch 均分填满 → 不可拖）。
        # stretchLastSection 保证列数不够铺满时最后一列拉伸填满可视区，列总宽
        # 超可视区时最后一列保持固定宽度、QTableView 自动出横向滚动条。
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setDefaultSectionSize(_char_w * 8)
        header.setStretchLastSection(True)
        self.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self._show_context_menu)
        main_layout.addWidget(self.table_view)

        # 底部动态加载条（加载状态 + 加载更多按钮）
        self._setup_load_bar(main_layout)

        self.setLayout(main_layout)

        self.model = PagedAttributeTableModel(self.layer, self.page_size, filter_str=self.filter_str, show_ids=self.show_ids)
        self.table_view.setModel(self.model)
        # 筛选重置后刷新加载状态；流式 chunk/结束也自动刷新；滚动到底自动加载下一批
        self.model.modelReset.connect(self._refresh_load_state)
        # 字段刷新/筛选重置后模型列数变化，按名重算隐藏列（新增字段默认可见）
        self.model.modelReset.connect(self._sync_column_visibility)
        self.model.state_changed.connect(self._refresh_load_state)
        self.table_view.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self._refresh_load_state()

    def _setup_load_bar(self, main_layout) -> None:
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(4)

        self.load_status_label = QLabel("加载中…")
        bar.addWidget(self.load_status_label)

        self.load_more_btn = QPushButton("加载更多")
        self.load_more_btn.clicked.connect(self._do_load_batch)
        bar.addWidget(self.load_more_btn)

        bar.addStretch()
        main_layout.addLayout(bar)

    def _refresh_load_state(self) -> None:
        """按模型加载进度刷新状态文本与"加载更多"按钮可用性。"""
        if self.model is None:
            return
        loaded = self.model.rowCount()
        total = self.model.total_features
        if self.model.scanning:
            self.load_status_label.setText(f"正在加载，已获取 {loaded} 条…")
            self.load_more_btn.setEnabled(self.model.has_more)
            return
        if self.model.truncated:
            self.load_status_label.setText(
                f"已显示前 {loaded} 条（结果过多，仅加载前 {MAX_MODEL_FIDS} 条，请缩小筛选范围）"
            )
            self.load_more_btn.setEnabled(False)
            return
        if not self.model.total_known:
            # 带过滤/show_ids：总数流式结束前未知，先显示已加载条数
            if self.model.has_more:
                self.load_status_label.setText(f"已加载 {loaded} 条，滚动到底或点击加载更多")
            else:
                self.load_status_label.setText(f"共 {loaded} 条（已全部加载）")
        elif total == 0:
            self.load_status_label.setText("0 条")
        elif self.model.has_more:
            self.load_status_label.setText(f"已加载 {loaded} / {total} 条，滚动到底或点击加载更多")
        elif loaded < total:
            # 取消流式（如进入编辑）后的停止态
            self.load_status_label.setText(f"已加载 {loaded} / {total} 条（已停止加载）")
        else:
            self.load_status_label.setText(f"共 {total} 条（已全部加载）")
        self.load_more_btn.setEnabled(self.model.has_more)

    def _on_scroll(self, value: int) -> None:
        """滚动到接近底部时追加加载下一批。"""
        if self.model is None or not self.model.has_more:
            return
        bar = self.table_view.verticalScrollBar()
        if value >= bar.maximum() - 10:
            self._load_batch()

    def _load_batch(self) -> None:
        # 延迟到下一事件循环再加载：endInsertRows 会同步触发滚动 valueChanged，
        # 直接调 load_more 会重入（见 buffer 对话框的 Qt 信号重入踩坑）
        QTimer.singleShot(0, self._do_load_batch)

    def _do_load_batch(self) -> None:
        if self.model is None:
            return
        self.model.load_more()
        self._refresh_load_state()

    def _start_edit(self) -> None:
        # 编辑会拿 QGIS 写锁，后台流式读锁会阻塞编辑：先取消流式
        if self.model is not None:
            self.model.cancel_stream()
        ok = self.editing_service.start(self.layer)
        if ok:
            self.save_btn.setEnabled(True)
            self.stop_btn.setEnabled(True)

    def _save_edit(self) -> None:
        if self.layer.isEditable():
            self.editing_service.commit(self.layer)

    def _stop_edit(self) -> None:
        self.editing_service.stop(self.layer, save=True)
        self.save_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)

    def _filter(self) -> None:
        dlg = QueryBuilderDialog(self.layer, parent=self)
        dlg.sqlConfirmed.connect(self._apply_filter)
        dlg.exec()

    def _apply_filter(self, cond: str) -> None:
        """筛选确认后更新模型筛选并刷新（重新统计，回到首屏首批）。"""
        if self.model:
            self.model.set_filter(cond)
            self.table_view.scrollToTop()
            self._refresh_load_state()

    def _field_calc(self) -> None:
        """打开 QGIS 内置字段计算器（复用矢量图层属性窗口字段页的那个）。

        对话框 OK 时直接把计算结果应用到图层（必要时自动进入编辑态），
        关闭后刷新表格列/值。
        """
        from qgis.gui import QgsFieldCalculator

        before_names = [f.name() for f in self.layer.fields()]
        dlg = QgsFieldCalculator(self.layer, self)
        dlg.exec()
        if dlg.result() != QDialog.DialogCode.Accepted:
            return
        # 内置对话框不暴露表达式/输出字段，日志按字段状态变化记录
        after_names = [f.name() for f in self.layer.fields()]
        new_fields = [n for n in after_names if n not in before_names]
        if new_fields:
            logger.info(
                "字段计算完成：图层=%s，新增字段 %s（字段数 %d→%d）",
                self.layer.name(), new_fields, len(before_names), len(after_names),
            )
        else:
            logger.info(
                "字段计算完成：图层=%s，更新已有字段（字段数 %d）",
                self.layer.name(), len(after_names),
            )
        # 计算器可能启动了编辑：同步"保存编辑/停止编辑"按钮可用性
        self._sync_edit_buttons()
        # 新增字段 → 列定义刷新；仅更新值 → 可视行重取新值，两者都覆盖
        if self.model is not None:
            self.model.reload_fields()

    def _sync_edit_buttons(self) -> None:
        """按图层编辑态同步"保存编辑/停止编辑"按钮可用性。"""
        editing = bool(self.layer.isEditable())
        self.save_btn.setEnabled(editing)
        self.stop_btn.setEnabled(editing)

    # ------------------------------------------------- 组织列（列显示设置）

    def _visible_field_names(self) -> list[str]:
        """当前可见（未被隐藏）的字段名，供组织列对话框预置勾选。"""
        if self.model is None:
            return [f.name() for f in self.layer.fields()]
        return [n for n in self.model.field_names if n not in self._hidden_field_names]

    def _organize_columns(self) -> None:
        """弹「组织列」对话框，按勾选结果隐藏未选中的字段列（ID 列恒显示）。"""
        dlg = OrganizeColumnsDialog(self.layer, self._visible_field_names(), parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._apply_column_visibility(dlg.checked_field_names())

    def _apply_column_visibility(self, names) -> None:
        """记录要隐藏的字段名并同步到视图（按字段名存，模型结构不动）。"""
        if self.model is None:
            return
        chosen = set(names)
        self._hidden_field_names = {n for n in self.model.field_names if n not in chosen}
        self._sync_column_visibility()

    def _sync_column_visibility(self) -> None:
        """按字段名重算隐藏列。

        列显示走 ``QTableView.setColumnHidden``（视图层隐藏）：模型列定义不变，
        取值/编辑/筛选逻辑无需改动，列宽在重新勾选后也能保留。第 0 列是 fid
        （ID），恒显示，故字段列从 1 起映射。
        """
        if self.model is None:
            return
        for col, name in enumerate(self.model.field_names, start=1):
            self.table_view.setColumnHidden(col, name in self._hidden_field_names)

    def _show_context_menu(self, position) -> None:
        from qgis.PyQt.QtGui import QAction
        from qgis.PyQt.QtWidgets import QMenu
        if not self.model:
            return
        idx = self.table_view.indexAt(position)
        if not idx.isValid():
            return
        feature = self.model.feature_at(idx.row())
        if feature is None:
            return  # 要素已被删除
        menu = QMenu(self)
        zoom = QAction("缩放到要素", self)
        zoom.triggered.connect(lambda: self.zoom_to_map.emit("zoomTo", {"extent": feature.geometry().boundingBox(), "id": [feature.id()]}))
        menu.addAction(zoom)
        sel = QAction("选中要素", self)
        sel.triggered.connect(lambda: self.zoom_to_map.emit("selectFeature", {"id": [feature.id()]}))
        menu.addAction(sel)
        if self.layer.isEditable():
            delete = QAction("删除", self)
            delete.triggered.connect(lambda: self.layer.deleteFeature(fid=feature.id()))
            menu.addAction(delete)
        menu.exec(self.table_view.viewport().mapToGlobal(position))


class AttributeDock(QDockWidget):
    """动态加载属性表 dock。"""

    def __init__(self, layer, page_size: int = 50, parent=None, filter_str: str = "1=1", show_ids: Optional[list] = None) -> None:
        title = f"属性表 - {layer.name()}"
        super().__init__(title, parent)
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.BottomDockWidgetArea)
        self.topParent = parent
        self.layer = layer
        self.show_ids = list(show_ids) if show_ids else []
        self.content_widget = PagedAttributeTableWidget(layer, page_size, self, filter_str=filter_str, show_ids=show_ids)
        self.setWidget(self.content_widget)
        self.content_widget.zoom_to_map.connect(self.zoom_to_map)
        self.setMinimumSize(200, 100)

    def add_new_layer(self, layer, parent, filter_str: str, show_ids, page_size: int = 50, title: str = "属性表") -> None:
        # 先取消旧 widget 的后台流式线程（避免滞留持有图层读锁）
        if self.content_widget is not None and self.content_widget.model is not None:
            self.content_widget.model.cancel_stream()
        self.layer = layer
        self.show_ids = list(show_ids) if show_ids else []
        self.content_widget = PagedAttributeTableWidget(layer, page_size, self, filter_str=filter_str, show_ids=show_ids)
        self.setWidget(self.content_widget)
        self.setWindowTitle(title)

    def closeEvent(self, event) -> None:
        try:
            if self.content_widget is not None and self.content_widget.model is not None:
                self.content_widget.model.cancel_stream()
            if self.layer.subsetString():
                self.layer.setSubsetString("")
            if self.layer.isEditable():
                self.content_widget._stop_edit()
            # **不清** ``topParent.current_attribute_dock``：清了下次打开就以为是
            # "没建过"，重新 new 一个 —— 新 Dock 没有位置记忆，用户把它浮动到地图上
            # 再关掉后，再打开会直接落回左上角。保留实例复用，Qt 才会记住浮动位置。
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)

    def zoom_to_map(self, action_type: str, data: dict) -> None:
        if self.topParent is None:
            return
        if action_type == "zoomTo":
            self.layer.selectByIds(data["id"])
            if hasattr(self.topParent, "zoom_to_layer_extent_transform"):
                self.topParent.zoom_to_layer_extent_transform(extent=data["extent"], source_crs=self.layer.crs())
        elif action_type == "selectFeature":
            self.layer.selectByIds(data["id"])
            self._refresh_canvas()
        elif action_type == "zoom_to_features":
            self.topParent.canvas.setExtent(data["extent"])
            self.layer.selectByIds(data["ids"])
            self.topParent.canvas.refresh()

    def _refresh_canvas(self) -> None:
        """选中要素后刷新画布，让高亮立即显示（selectByIds 不会自动刷新）。"""
        canvas = getattr(self.topParent, "canvas", None)
        if canvas is not None:
            canvas.refresh()
