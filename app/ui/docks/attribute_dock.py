"""属性表 Dock（分页模型 + 按需取要素 + 与图层选中集双向同步）。

三条**必须守住的约定**，改这里之前先读：

1. **带筛选 / 带子集的表是完全隔离的**。表的"内容"由 ``filter_str``（表达式）或
   ``show_ids``（子集）决定；质检结果、空间查询、"只看选中"都走后者。这两种表是
   "**就显示这些**"的约定，所以：
   - 不往里补插任何东西（见 ``PagedAttributeTableModel.shows_everything``）——
     否则地图上选中一个无关要素，质检结果表就会多出一行；
   - 地图选中集变化时只**高亮表里已有的行**，不改变行集合。
   只有"全量表"（无筛选、无子集）才允许为"选中但未加载"的要素临时补插行。

2. **打开就全量加载**（:data:`LOAD_ALL_ON_OPEN`）。分页会让"全选"只选中已加载的那部分、
   排序也无从谈起（排序要全量）。后台线程照旧在跑，所以不会冻界面；分页按钮随之退场。

3. **大批选中不进 Qt 选中集，改用模型底色画高亮**（:data:`MAX_TABLE_SELECTION_ROWS`）。
   Qt 表格视图处理一次选中变化是 O(行数)、约 0.4 ms/行（14.8 万行 ≈65 秒），所以
   超过阈值只标"底色行"，Qt 选中集留空。
"""

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
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import (QAbstractTableModel, QEvent, QItemSelection, QItemSelectionModel,
                              QModelIndex, QObject, QPoint, QPointF, QRect, Qt, QTimer, QVariant, pyqtSignal)
from qgis.PyQt.QtGui import (QBrush, QColor, QKeySequence, QPainter, QPalette, QPen, QPolygonF,
                             QStandardItem, QStandardItemModel)
from qgis.PyQt.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDockWidget,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.editing import LayerEditService
from app.core.util.dict_values import FieldDict, layer_dict_fields
from app.infra.logging import get_logger
from app.ui.common import tool_icons
from app.ui.common.feature_highlight import FeatureHighlighter
from app.ui.common.icons import Icons
from app.ui.common.theme import ThemeManager, dock_selection_color
from app.ui.common.value_format import format_value
from app.ui.common.widgets import save_file
from app.ui.dialogs.organize_columns import OrganizeColumnsDialog
from app.ui.dialogs.query_builder import QueryBuilderDialog
from app.ui.tools.feature_attrs import (
    field_alias,
    readonly_field_indices,
)

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
#: 打开属性表是否就（后台）把结果**全部**加载完，而不是先给一批、再按需分页。
#: 分页会让"全选"只选中已加载的那部分，排序更是无从谈起（排序必须全量）。
#: 置 False 可退回旧的分页流式行为（那时表底部会重新出现「加载更多 / 加载全部」）。
LOAD_ALL_ON_OPEN = True

#: 「地图上选中了但表里还没加载到」时，最多往表里临时补插多少条。超出部分不同步，
#: 只提示一句——一次补几十万行会把表格和滚动条拖垮，而且用户也看不过来。
EXTRA_SELECT_LIMIT = 5000

#: 表内**逐行高亮**的选中行数上限。Qt 视图处理一次选中变化是 O(行数)、约 0.4 ms/行#: （实测：1000 行 0.46 s、20000 行 8.5 s、148278 行 ≈65 s，纯 Qt 内部开销，与本模型
#: 无关；换自定义选中模型也不管用，实测一样慢）。超过这个数就不往 Qt 选中集里塞，
#: 改成**用模型背景色画高亮**（:meth:`PagedAttributeTableModel.set_band_rows`）——
#: 视图只问可视行的 ``data()``，代价与总行数无关，所以多少行都不卡。
MAX_TABLE_SELECTION_ROWS = 1000

#: 冻结列集合里代表**ID 列**（第 0 列，无对应字段）的哨兵键。字段列直接以字段名
#: 为键（按名存，模型 reset / 新增字段后仍能对上，同 :attr:`_hidden_field_names`）。
_ID_COLUMN_KEY = "\uf8ff__id__"

#: 导出 CSV 时每批写多少行。写完一批就回事件循环（刷新进度、保持界面可响应），
#: 否则十几万行一口气写完界面会僵住。
_EXPORT_CHUNK = 2000

#: 「缩放到所选记录」时，选中要素超过这个数就退回按图层范围缩放 ——
#: ``boundingBoxOfSelected()`` 要逐个读选中要素的几何，十几万条会明显卡住。
_ZOOM_SELECTION_MAX = 20000

#: 缩放到某个图形时四周留的边距比例（图形不至于顶到屏幕边）
_ZOOM_PADDING = 1.2

#: 「当前定位行」的底色透明度常量已随"定位行只留三角、不要底色"一起移除


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

    def __init__(self, layer: QgsVectorLayer, where: str, show_ids: Optional[list], epoch: int,
                 order_by: Optional[tuple] = None) -> None:
        super().__init__()
        self.layer = layer
        self.where = where or "1=1"
        self.show_ids = list(show_ids) if show_ids else []
        #: ``(字段名, 是否升序)``；None = 不排序。走 provider 的 ORDER BY。
        self.order_by = order_by
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
        if self.order_by:
            request.addOrderBy(self.order_by[0], self.order_by[1])
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
        #: 是否处于"只看某个子集"模式（见 set_show_ids）。**不能只看 ``show_ids`` 是否为空**
        #: ——"只看空集"与"看全部"的 show_ids 都是空。
        self.subset_active = bool(self.show_ids)
        #: 大选中时"用底色画高亮"的行集合：这些行**不进 Qt 选中集**（进不起，见
        #: :data:`MAX_TABLE_SELECTION_ROWS`），而是由 :meth:`data` 的 BackgroundRole 画。
        self._band_rows: set[int] = set()
        self._band_brush: QBrush | None = None
        #: 「当前定位行」（"转到特定记录"跳过去的那一行）。**不是选中**——ArcMap 里跳记录
        #: 不动选择集；标记就是行号栏（最左那一窄列）里的一个 ▶，见 _LocatedHeaderView。
        self.located_row = -1
        #: 排序列（0 = ID 列，-1 = 未排序）与方向
        self._sort_column = -1
        self._sort_ascending = True
        self.field_names = [f.name() for f in layer.fields()]
        # 列头显示名：优先字段别名，无别名降级为字段名（attributeAlias 返回空串即无）
        self.header_names = [
            field_alias(layer.fields().at(i)) or name for i, name in enumerate(self.field_names)
        ]
        #: 编辑态下**禁止改值**的列（字段下标 +1，列 0 是 ID 列）：全局唯一标识
        #: 与数据源自增/自维护的主键。与属性录入弹窗同一口径，见 feature_attrs。
        self._readonly_cols = self._compute_readonly_cols()
        #: 字段下标 → 字典域（值与 code→name 映射）。属性表显示 / 编辑用；
        #: 没挂字典的字段不在里面。见 :mod:`app.core.util.dict_values`。
        self._dict_fields: dict[int, FieldDict] = {}
        self._load_dict_fields()
        #: 结果 **fid 列表**（内存主驻留物；要素按需取，见 ``feature_at``）
        self._fids: List[int] = []
        #: fid → 行号（选中集同步要按 fid 反查行，线性找在 2M 行上不可用）
        self._row_of_fid: dict[int, int] = {}
        #: **临时补插**的 fid：选中了但还没流式加载到，先补进表里好让对应行能被选中。
        #: 它们不属于筛选结果，取消选中就撤掉（见 :meth:`drop_stale_extras`）。
        self._extra_fids: set[int] = set()
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
        # 图层缓冲里的值变了（编辑工具改属性 / 挖洞等几何编辑后重算面积）→ 丢掉
        # 要素缓存并让视图重画。不接这个信号的话，**已经打开**的属性表还显示旧值，
        # 要手动重新点一下才刷新（用户报过）。
        layer.attributeValueChanged.connect(self._on_layer_values_changed)
        layer.geometryChanged.connect(self._on_layer_values_changed)
        # 字段定义变化（增 / 删 / 改名，含「图层属性 → 字段」里删字段）→ 重读列定义。
        # updatedFields 在 provider 直接改与编辑缓冲改两条路都会发（实测：非编辑态
        # deleteAttributes、编辑态 deleteAttribute/addAttribute、renameAttributes 都发），
        # 所以一个信号就能覆盖所有入口；否则已打开的属性表还挂着被删列（取值/编辑错位）。
        layer.updatedFields.connect(self._on_fields_updated)
        # 字段**别名**变化只发 layerModified（实测 setFieldAlias 不发 updatedFields），
        # 且字段名没变 → 不能走 reload_fields。这里只刷新列头文字（见 _on_layer_modified）。
        layer.layerModified.connect(self._on_layer_modified)
        # 图层显示条件（subsetString，含「图层属性 → 条件」页 / 属性查询器）变了 →
        # 按新条件重流。不接这个信号的话，已打开的属性表仍显示旧条件的数据。
        layer.subsetStringChanged.connect(self._on_subset_string_changed)
        # 删 / 增要素也要让表跟上：**不接 featureDeleted 的话，编辑时右键「删除」只改了
        # 编辑缓冲，表格还显示那一行** —— 用户看到的就是"删除没有效果"（用户报的）。
        layer.featureDeleted.connect(self._on_feature_deleted)
        layer.featureAdded.connect(self._on_feature_added)
        self._start_stream()

    def _load_dict_fields(self) -> None:
        """重建「字段下标 → 字典域」表（打开 / 字段变更时调用）。"""
        fields = self.layer.fields()
        mapping: dict[int, FieldDict] = {}
        for name, info in layer_dict_fields(self.layer):
            index = fields.indexOf(name)
            if index >= 0:
                mapping[index] = info
        self._dict_fields = mapping

    def dict_info(self, column: int) -> FieldDict | None:
        """第 ``column`` 列（含 ID 列偏移）挂接的字典；没挂返回 ``None``。"""
        if column <= 0:
            return None
        return self._dict_fields.get(column - 1)

    def _on_feature_deleted(self, fid: int) -> None:
        """图层里删了一个要素：把表里对应那一行立刻撤掉（撤销删除由 featureAdded 补回）。"""
        fid = int(fid)
        self._feature_cache.pop(fid, None)
        self._extra_fids.discard(fid)
        row = self._row_of_fid.get(fid)
        if row is None:
            return
        self._remove_rows([row])
        self.loaded_count = len(self._fids)
        if self.total_known:
            self.total_features = max(0, self.total_features - 1)
        self.state_changed.emit()

    def _on_feature_added(self, fid: int) -> None:
        """图层里新增了一个要素（含**撤销删除**）→ 全量视图且已加载完时补到末尾。

        带筛选 / 还在流式加载 / 正在按某列排序时不动手：新要素是否命中筛选、该排在哪
        都得重算，交给用户点「刷新」。这里只兜住"删了又撤销"这个最常见的情形。
        """
        if self.subset_active or not self._exhausted or self._sort_column >= 0:
            return
        added = self._append_fids([int(fid)])
        if not added:
            return
        self.loaded_count = len(self._fids)
        if self.total_known:
            self.total_features += 1
        self.state_changed.emit()

    def _on_fields_updated(self) -> None:
        """图层字段定义变了 → 字段名有变化才重读列（避免 commit 等重复触发白重载）。"""
        if [f.name() for f in self.layer.fields()] == self.field_names:
            return
        self.reload_fields()

    def _on_layer_modified(self, *_args) -> None:
        """图层被改动（主要是字段别名变化）→ **只刷新列头文字**。

        别名变化：字段名不变、只发 ``layerModified``。若为此走 ``reload_fields``
        （beginResetModel/endResetModel），会把整表 reset —— 清选中、重建可见行、
        还可能打扰滚动；对百万行只是浪费。这里只重算列头并 ``headerDataChanged``，
        只重绘表头，**代价与行数无关**（O(列数)）。
        """
        fields = self.layer.fields()
        if fields.count() != len(self.field_names):
            return  # 数量变了：交给 _on_fields_updated / reload_fields 处理
        headers = [
            field_alias(fields.at(i)) or name for i, name in enumerate(self.field_names)
        ]
        if headers == self.header_names:
            return
        self.header_names = headers
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, self.columnCount() - 1)

    def _on_subset_string_changed(self, *_args) -> None:
        """图层显示条件变了 → 清掉当前结果，按新条件重新流式加载。"""
        self._reset_and_restart()

    def _on_layer_values_changed(self, *_args) -> None:
        """编辑缓冲改了值：清要素缓存 + 让整张表重画。

        ``dataChanged`` 覆盖全部已加载行只是给视图一个重画提示，Qt 只重绘可视区域，
        大图层也不慢；这里**不做 modelReset**——那会把流式加载的 fid 列表清空、
        整表重新扫描，代价大得多。
        """
        self._feature_cache.clear()
        rows, cols = self.rowCount(), self.columnCount()
        if rows > 0 and cols > 0:
            self.dataChanged.emit(self.index(0, 0), self.index(rows - 1, cols - 1))

    def _build_where(self) -> str:
        """筛选表达式（show_ids 走 setFilterFids，不拼进表达式）。"""
        return self.filter_str or "1=1"

    def _compute_readonly_cols(self) -> set[int]:
        """系统自维护 / 全局唯一标识字段的**列号**（列 0 = ID 列，字段从第 1 列起）。

        与属性录入弹窗同一口径（``uid_field_index`` / ``auto_id_field_index``）：
        - 全局唯一标识（MZGUID / FGUID / GlobalID…）：只给系统自动赋值，手工改会
          破坏"一要素一标识"；
        - 数据源自增/自维护的主键（OBJECTID / fid / ogc_fid…）：保存时数据源自己填。
        """
        fields = self.layer.fields()
        return {i + 1 for i in readonly_field_indices(fields)}

    @property
    def readonly_cols(self) -> set[int]:
        """编辑态下禁止改值的列（系统自维护 / 全局唯一标识；列 0 是 ID 列）。

        只读列不仅有 :meth:`flags` 里的不可编辑，属性表列头右键也据此**禁止删除**。
        """
        return self._readonly_cols

    @property
    def has_more(self) -> bool:
        """是否还有未加载的要素（流式未结束即视为有）。"""
        return not self._exhausted

    def _fast_total(self):
        """总数快路径：show_ids 仅自身 → len；无过滤表达式 → featureCount()。

        :return: ``(total, known)``。带过滤/show_ids 组合时总量必须流式结束后才知道。
        """
        if self.subset_active and not self.show_ids:
            return 0, True          # 只看空集 = 一行都没有
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
        self._row_of_fid = {}
        self._extra_fids = set()
        self._band_rows = set()
        self.located_row = -1
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
        worker = FeatureStreamWorker(self.layer, self.where, self.show_ids,
                                     epoch=self._epoch, order_by=self._order_by())
        worker.chunk_ready.connect(self._on_chunk)
        worker.finished.connect(self._on_finished)
        worker.paused.connect(self._on_paused)
        worker.start()
        self._worker = worker
        self.scanning = True
        self.state_changed.emit()
        # 默认一次要到位（全量）；关掉 LOAD_ALL_ON_OPEN 才退回"先给一批、按需再要"
        worker.request_more(MAX_MODEL_FIDS if LOAD_ALL_ON_OPEN else self.batch_size)

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
            order = self._order_by()
            if order:
                request.addOrderBy(order[0], order[1])
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
        self._reset_and_restart()

    def set_show_ids(self, show_ids, subset_active: Optional[bool] = None) -> None:
        """换「只看哪些要素」的子集并重流（**原地**切，不用重建整个表格）。

        :param subset_active: 是否处于"只看子集"模式。**必须显式传**才能表达
            "只看空集"——"选了「仅选中要素」但当前没有选中"和"看全部"的 ``show_ids``
            都是空，光看 ``show_ids`` 分不开。
        """
        new_ids = [int(i) for i in show_ids] if show_ids else []
        active = bool(new_ids) if subset_active is None else bool(subset_active)
        if new_ids == self.show_ids and active == self.subset_active:
            return
        self.show_ids = new_ids
        self.subset_active = active
        self._reset_and_restart()

    def _reset_and_restart(self) -> None:
        """按当前 ``filter_str`` / ``show_ids`` 重开一轮：重置 → 重算总数 → 重新流式。"""
        self.where = self._build_where()
        self.beginResetModel()
        self._refresh_totals()
        self.endResetModel()
        self._start_stream()

    def load_all(self) -> None:
        """一次性把结果全部加载完（不必等用户滚到底点「加载更多」）。

        仍然走后台 worker 分批回传，所以界面不会僵住、进度照常刷新；只是把目标
        一次性抬到上限，让它一路跑到迭代器耗尽。上限仍是 ``MAX_MODEL_FIDS``。
        """
        if self._exhausted or self._worker is None:
            return
        self.scanning = True
        self.state_changed.emit()
        self._worker.request_more(MAX_MODEL_FIDS)

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
        self._readonly_cols = self._compute_readonly_cols()
        self._load_dict_fields()
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
        added = self._append_fids(chunk[:room])
        self.loaded_count += len(added)
        if len(self._fids) >= MAX_MODEL_FIDS:
            self._truncate()

    def _append_fids(self, fids: List[int]) -> List[int]:
        """把**还没有的** fid 追加到末尾（去重），维护 fid→行号；返回真正追加的那些。"""
        fresh: List[int] = []
        for fid in fids:
            fid = int(fid)
            if fid in self._row_of_fid:
                # 它已经有一行了。若之前在"临时补插"集合里，说明这次是流式结果自己也带到了
                # ——它现在属于正常结果，别再当临时项（否则取消选中时会被误删）。
                self._extra_fids.discard(fid)
                continue
            if len(self._fids) + len(fresh) >= MAX_MODEL_FIDS:
                break
            fresh.append(fid)
        if not fresh:
            return []
        start = len(self._fids)
        self.beginInsertRows(QModelIndex(), start, start + len(fresh) - 1)
        for offset, fid in enumerate(fresh):
            self._row_of_fid[fid] = start + offset
            self._fids.append(fid)
        self.endInsertRows()
        return fresh

    def _remove_rows(self, rows: List[int]) -> None:
        """删除若干行（``rows`` 降序）并维护 ``_row_of_fid`` 的行号前移。"""
        if not rows:
            return
        i = 0
        while i < len(rows):
            end = rows[i]
            start = end
            j = i
            while j + 1 < len(rows) and rows[j + 1] == start - 1:
                start = rows[j + 1]
                j += 1
            self.beginRemoveRows(QModelIndex(), start, end)
            for fid in self._fids[start:end + 1]:
                self._row_of_fid.pop(fid, None)
                self._feature_cache.pop(fid, None)
            del self._fids[start:end + 1]
            # 删掉的那一段之后，行号整体前移
            for idx in range(start, len(self._fids)):
                self._row_of_fid[self._fids[idx]] = idx
            self.endRemoveRows()
            i = j + 1

    # ------------------------------------------------------------ 选中集同步

    @property
    def shows_everything(self) -> bool:
        """表里是不是"没有任何筛选 / 子集"的全量表。

        带筛选（按表达式）或带子集（``show_ids``：质检结果 / 空间查询 / 只看选中…）时
        都是"**就显示这些**"的约定，不能往里塞别的东西（见 ``apply_layer_selection``）。
        """
        return not self.subset_active and (self.where or "").strip() in ("", "1=1")

    # ------------------------------------------------------------ 排序

    @property
    def sort_column(self) -> int:
        """当前排序列（0 = ID，-1 = 未排序）。"""
        return self._sort_column

    @property
    def sort_ascending(self) -> bool:
        return self._sort_ascending

    def _order_by(self):
        """当前排序 → ``(字段名, 是否升序)``；不排序、或按 ID 列时返回 ``None``。

        ID 列（第 0 列）就是 fid，交给 python 重排（``_fids`` 全在手上，瞬间完成），
        不走 provider——``$id`` 当 ORDER BY 表达式在部分 provider 上不可靠。
        """
        if self._sort_column <= 0:
            return None
        col = self._sort_column - 1
        if col >= len(self.field_names):
            return None
        return (self.field_names[col], self._sort_ascending)

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        """按某列排序（表头点击进来）。

        字段列走 provider 的 ``ORDER BY`` **重新取一遍 fid 顺序**；ID 列直接在 python 里
        重排。两路都会让"行号"整体改变，所以调用方（表格）要按 fid 把选中重新标回去。
        """
        ascending = order != Qt.SortOrder.DescendingOrder
        if column == 0:
            # ID 列：全部 fid 就在手上，直接重排（不劳 provider）
            self._sort_column, self._sort_ascending = 0, ascending
            self._fids.sort(reverse=not ascending)
            self.beginResetModel()
            self._row_of_fid = {fid: i for i, fid in enumerate(self._fids)}
            self._band_rows = set()
            self.endResetModel()
            return
        if column == self._sort_column and ascending == self._sort_ascending:
            return
        self._sort_column, self._sort_ascending = column, ascending
        self._reset_and_restart()

    def row_of_fid(self, fid) -> int:
        """fid → 行号；不在表里返回 ``-1``。"""
        return self._row_of_fid.get(int(fid), -1)

    def add_extra_fids(self, fids) -> List[int]:
        """把"选中了但还没流式加载到"的 fid 临时补进表里（返回真正补进的）。

        只为让对应行能被选中；它们记在 :attr:`_extra_fids` 里，取消选中即撤掉。
        """
        added = self._append_fids(list(fids))
        self._extra_fids.update(added)
        return added

    def drop_stale_extras(self, keep) -> None:
        """撤掉不再被选中、且不是流式结果一部分的临时行。

        不撤的话，表里会攒下越来越多**不属于当前筛选结果**的行。
        """
        if not self._extra_fids:
            return
        keep_set = {int(f) for f in keep}
        stale = [f for f in self._extra_fids if f not in keep_set]
        if not stale:
            return
        rows = sorted((self._row_of_fid[f] for f in stale if f in self._row_of_fid),
                      reverse=True)
        self._extra_fids.difference_update(stale)
        self._remove_rows(rows)

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

    def feature_by_fid(self, fid: int) -> Optional[QgsFeature]:
        """按 fid 取要素并走 LRU 缓存（定位 / 闪烁也复用这份缓存）。"""
        feat = self._feature_cache.get(int(fid))
        if feat is not None:
            return feat
        feat = self.layer.getFeature(int(fid))
        if feat is None or not feat.isValid():
            return None
        self._cache_feature(int(fid), feat)
        return feat

    def feature_at(self, row: int) -> Optional[QgsFeature]:
        """取第 ``row`` 行的要素：按 fid 从图层按需取回，LRU 缓存可视窗口。

        QTableView 渲染只调可视行，滚动出窗口的要素被淘汰——2M+ 结果内存恒定。
        要素在图层里被删除时返回 None。
        """
        if row < 0 or row >= len(self._fids):
            return None
        return self.feature_by_fid(self._fids[row])

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._fids):
            return None
        if role == Qt.ItemDataRole.BackgroundRole and self._band_rows:
            # 大选中：这些行不进 Qt 选中集（进不起，见 MAX_TABLE_SELECTION_ROWS），
            # 改由这里给一层底色。视图只问**可视行**，所以多少行都不卡。
            if index.row() in self._band_rows:
                return self._band_brush_value()
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if index.column() == 0:
                return self._fids[index.row()]
            feature = self.feature_at(index.row())
            if feature is None:
                return None
            field_idx = index.column() - 1
            if field_idx < len(feature.fields()):
                value = feature.attribute(field_idx)
                if role == Qt.ItemDataRole.DisplayRole:
                    info = self._dict_fields.get(field_idx)
                    if info is not None:
                        name = info.name_for(value)
                        if name is not None:
                            return name
                # EditRole 保留原始 code：编辑框/委托按 code 回填并写回 code
                return value
        return None

    def _band_brush_value(self) -> QBrush:
        """高亮底色（与 QSS 里选中行同色，主题切换时重算）。"""
        color = dock_selection_color(ThemeManager.instance().current())
        if self._band_brush is None or self._band_brush.color().name() != color:
            self._band_brush = QBrush(QColor(color))
        return self._band_brush

    def set_located_row(self, row) -> bool:
        """设置「当前定位行」（-1 = 没有）；返回是否真的变了（调用方据此决定要不要重绘）。"""
        row = int(row)
        if row < 0 or row >= len(self._fids):
            row = -1
        if row == self.located_row:
            return False
        self.located_row = row
        return True

    def set_band_rows(self, rows) -> bool:
        """设置"用底色高亮"的行集合；返回是否真的变了（调用方据此决定要不要重绘）。

        这些行**不进 Qt 选中集**。用底色画是因为 Qt 的选中集在大批量下是 O(行数)、
        约 0.4 ms/行（14.8 万行要 65 秒），而底色只随可视行花代价。
        """
        new = {int(r) for r in rows}
        if new == self._band_rows:
            return False
        self._band_rows = new
        return True

    def clear_band_rows(self) -> bool:
        return self.set_band_rows(())

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
        if self.layer.isEditable() and index.column() not in self._readonly_cols:
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        if index.column() == 0 or index.column() in self._readonly_cols:
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
        if value is not None:
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
        # 值没变就别写：changeAttributeValue 即使写同值也会进撤销栈、把图层标脏。
        # 用户只是点开字典下拉、没选任何新值，退出编辑就会误报「有未保存修改」。
        try:
            current = feature.attribute(col)
        except Exception:  # noqa: BLE001
            current = None
        if value is None:
            if current is None or current == "":
                return True
        else:
            try:
                if current is not None and current == value:
                    return True
            except Exception:  # noqa: BLE001 —— 无法比较的类型按真修改处理
                pass
        success = self.layer.changeAttributeValue(fid, col, value)
        if success:
            # 缓存里的旧要素失效：弹出，下次渲染按需重取新值
            self._feature_cache.pop(fid, None)
            self.dataChanged.emit(index, index, [role])
            return True
        return False


_DICT_CODE_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_DICT_EMPTY_ROLE = _DICT_CODE_ROLE + 1


class _DictTreeView(QTreeView):
    """字典下拉树：禁用键盘 type-ahead 搜索。

    QAbstractItemView 的 ``keyboardSearch`` 会逐项扫模型；字典有 2 万条时输入
    一个字符就会长时间卡住。这里字典只能鼠标 / 方向键选择。
    """

    def keyboardSearch(self, search: str) -> None:  # noqa: N802 —— Qt 命名
        return


class _DictTreeCombo(QComboBox):
    """属性表字典编辑器：下拉是一棵「字典域 → 字典值」树。

    QComboBox 自带的下拉只支持平铺项，子项无法成为 ``currentIndex``；这里
    自己接管树节点的点击：选中叶子后记录 code、把显示文本写进只读编辑框，
    再通过 ``valuePicked`` 通知委托提交/关闭编辑器。
    """

    valuePicked = pyqtSignal()

    def __init__(self, parent, info: FieldDict, current_code) -> None:
        super().__init__(parent)
        self._info = info
        self._code = ""
        self._cleared = False
        self._picking = False
        self._items: dict[str, QStandardItem] = {}
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        # 不挂补全器：树模型 + 大字典时，输入字符会触发 combo 的键盘搜索/
        # 补全，实测会把界面卡死。字典只能选，不允许手输。
        self.setCompleter(None)
        if self.lineEdit() is not None:
            self.lineEdit().setReadOnly(True)
            self.lineEdit().setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.lineEdit().setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
            )
        self.setMaxVisibleItems(24)
        self.setMinimumWidth(360)

        view = _DictTreeView(self)
        view.setHeaderHidden(True)
        view.setRootIsDecorated(True)
        view.setUniformRowHeights(True)
        view.setExpandsOnDoubleClick(False)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.setMinimumWidth(360)
        # 用 pressed 而不是 clicked：QComboBox 的下拉容器可能先隐藏弹窗，
        # 导致 clicked 来不及发出，表现为"选了但没写回"。
        view.pressed.connect(self._on_tree_clicked)
        view.activated.connect(self._on_tree_clicked)
        self.setView(view)

        model = QStandardItemModel(self)
        # 「空值」放在树最上面：字典字段允许不填任何值
        empty = QStandardItem("（空值 / 不填）")
        empty.setData(True, _DICT_EMPTY_ROLE)
        empty.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        model.appendRow(empty)
        root = QStandardItem(f"({info.domain})")
        root.setFlags(Qt.ItemFlag.ItemIsEnabled)
        root.setData(None, _DICT_CODE_ROLE)
        font = root.font()
        font.setBold(True)
        root.setFont(font)
        model.appendRow(root)
        for _depth, value in info.as_tree():
            item = QStandardItem(f"({value.code}){value.name}")
            item.setData(value.code, _DICT_CODE_ROLE)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            parent_item = self._items.get(value.parent_code, root)
            parent_item.appendRow(item)
            self._items[value.code] = item
        self.setModel(model)
        self._root = root
        self._empty_item = empty
        view.expandAll()
        self.set_value(current_code)

    def set_value(self, code) -> None:
        self._picking = False
        value = "" if code is None else str(code)
        if not value:
            self._code = ""
            self._cleared = True
            self.setEditText("（空值 / 不填）")
            self.view().setCurrentIndex(self._empty_item.index())
            return
        self._code = value
        self._cleared = False
        item = self._items.get(value)
        if item is not None:
            self.setEditText(item.text())
            self.view().setCurrentIndex(item.index())
        else:
            self.setEditText(f"（当前值）{value}")
            self.view().setCurrentIndex(self._root.index())

    def selected_code(self) -> str:
        return self._code

    def keyPressEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        """字典只能选：屏蔽可打印字符，避免 combo 在大字典上做键盘搜索卡死。"""
        allowed = {
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
            Qt.Key.Key_Home,
            Qt.Key.Key_End,
            Qt.Key.Key_PageUp,
            Qt.Key.Key_PageDown,
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Escape,
            Qt.Key.Key_Tab,
            Qt.Key.Key_Backtab,
        }
        if event.key() in allowed:
            super().keyPressEvent(event)
        else:
            event.ignore()

    def is_cleared(self) -> bool:
        return self._cleared

    def _on_tree_clicked(self, index) -> None:
        if self._picking:
            return
        if index.data(_DICT_EMPTY_ROLE):
            self._picking = True
            self._code = ""
            self._cleared = True
            self.setEditText("（空值 / 不填）")
            self.hidePopup()
            self.valuePicked.emit()
            return
        code = index.data(_DICT_CODE_ROLE)
        if code is None:  # 点到域标题：只是折叠/展开
            return
        self._picking = True
        self._code = str(code)
        self._cleared = False
        self.setEditText(index.data(Qt.ItemDataRole.DisplayRole))
        self.hidePopup()
        self.valuePicked.emit()

    def showPopup(self) -> None:  # noqa: N802 —— Qt 命名
        self._picking = False
        self.view().expandAll()
        item = self._items.get(self._code) if not self._cleared else self._empty_item
        if item is not None:
            self.view().setCurrentIndex(item.index())
        super().showPopup()
        try:
            popup = self.view().window()
            popup.setMinimumWidth(max(360, self.width()))
            # 比整屏还高的下拉很难用：限制高度，行多了走滚动条
            popup.setMinimumHeight(160)
            popup.setMaximumHeight(200)
            self.view().setMinimumHeight(158)
            self.view().setMaximumHeight(198)
            self.view().setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded
            )
            # 属性表高度有限：下方放不下且上方放得下时，翻到单元格上方弹出。
            # **弹窗的 show 是延后的**（showPopup() 返回时 isVisible() 还是 False），
            # 但此刻它的 geometry 已经算好、还没绘制：现在摆位置不会闪，等可见后
            # 再摆反而会看到它先出现在 Qt 算错的位置、再跳过来（用户报的「飞出去」）。
            self._place_popup(popup)
            self._place_popup_later(popup)
        except RuntimeError:
            pass

    def _place_popup_later(self, popup, attempts: int = 0) -> None:
        """弹窗真正可见后再摆一次（防止 Qt 在 show 时又改位置）。"""
        try:
            if not popup.isVisible():
                if attempts < 20:
                    QTimer.singleShot(
                        10,
                        lambda p=popup, n=attempts: self._place_popup_later(p, n + 1),
                    )
                return
            self._place_popup(popup)
        except RuntimeError:
            pass

    def _place_popup(self, popup) -> None:
        """按单元格位置摆放下拉弹窗（下方放不下就翻到单元格上方，紧贴单元格）。

        **不能拿表格视口当边界**：属性表 Dock 往往只有一两百像素高，向下放不下、
        向上也放不下整张弹窗，旧逻辑就放弃了手动定位、退回 Qt 的默认位置 ——
        而 Qt 在滚动视口里会把弹窗扔到视口顶上方很远（用户报的「飞上去」）。
        这里只按**屏幕可用区域**判断上下，弹窗允许盖住表格上方的画布。
        """
        try:
            from qgis.PyQt.QtGui import QGuiApplication

            natural = popup.height()
            popup_h = min(200, max(160, natural if natural > 0 else 160))
            if natural != popup_h:
                popup.resize(popup.width(), popup_h)
            cell_top = self.mapToGlobal(QPoint(0, 0)).y()
            cell_bottom = cell_top + self.height()
            screen = QGuiApplication.screenAt(self.mapToGlobal(self.rect().center()))
            if screen is None:
                screen = QGuiApplication.primaryScreen()
            avail = screen.availableGeometry() if screen is not None else None
            if avail is None:
                return
            if avail.bottom() - cell_bottom >= popup_h:
                popup.move(popup.x(), cell_bottom)  # 下方放得下：贴单元格下沿
            else:
                # 上方：底部紧贴单元格上沿；连屏幕顶都放不下时贴屏幕上沿
                popup.move(popup.x(), max(avail.top(), cell_top - popup_h))
        except RuntimeError:
            pass


class _DictValueDelegate(QStyledItemDelegate):
    """挂了字典的单元格：编辑时用「字典域 → 全部值」树形下拉。

    选中后写回原始 code；显示仍由 model 的 DisplayRole 映射成名称
    （见 :meth:`PagedAttributeTableModel.data`）。
    """

    def __init__(self, parent, model: "PagedAttributeTableModel") -> None:
        super().__init__(parent)
        self._model = model
        #: 所属视图（主表 / 冻结表各有一份委托）：箭头点击要在**这个**视图里开编辑器
        self._view = parent

    # ------------------------------------------------------------ 字典箭头

    @staticmethod
    def _arrow_rect(rect: QRect) -> QRect:
        """单元格右侧的字典箭头按钮矩形（视图坐标系）。"""
        size = 16
        return QRect(
            rect.right() - size - 2,
            rect.center().y() - size // 2,
            size,
            size,
        )

    def paint(self, painter, option, index) -> None:  # noqa: N802 —— Qt 命名
        """当前单元格是字典字段时，在右侧画一个"下拉箭头"按钮。

        只有**当前/聚焦**的单元格才画，避免整列都是箭头把数据看花。
        """
        super().paint(painter, option, index)
        try:
            if self._model.dict_info(index.column()) is None:
                return
            # 非编辑态：字典单元格只读，不画箭头（用户要求）
            if not self._model.layer.isEditable():
                return
            if not (self._view.hasFocus() and self._view.currentIndex() == index):
                return
            rect = self._arrow_rect(option.rect)
            if not rect.isValid():
                return
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # 用主题色而不是 palette：表格底色由 QSS 指定（window_bg / 交替行
            # panel_bg，选中 toolbar_hover），调色板里的 Base/Highlight 对不上，
            # 会在深色主题下露出白块
            theme = ThemeManager.instance().current()
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            if selected:
                bg = QColor(theme.toolbar_hover)
            else:
                bg = QColor(theme.panel_bg if index.row() % 2 else theme.window_bg)
            fg = QColor(theme.menu_text)
            # 先把箭头区域的文字盖掉，再画一个小下拉箭头
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(bg)
            painter.drawRect(rect.adjusted(-3, -2, 2, 2))
            painter.setPen(QPen(fg, 1.4))
            cx, cy = rect.center().x(), rect.center().y()
            painter.drawLine(cx - 4, cy - 2, cx, cy + 2)
            painter.drawLine(cx, cy + 2, cx + 4, cy - 2)
            painter.restore()
        except Exception:  # noqa: BLE001 —— 绘制里不能抛异常
            return

    def editorEvent(self, event, model, option, index):  # noqa: N802 —— Qt 命名
        """点字典单元格右侧箭头 → 打开字典下拉（不点正文）。"""
        try:
            if (
                self._model.dict_info(index.column()) is not None
                and self._model.layer.isEditable()
                and self._view.currentIndex() == index
                and event.button() == Qt.MouseButton.LeftButton
                and event.type()
                in (
                    QEvent.Type.MouseButtonPress,
                    QEvent.Type.MouseButtonRelease,
                    QEvent.Type.MouseButtonDblClick,
                )
                and self._arrow_rect(option.rect)
                .adjusted(-3, -2, 2, 2)
                .contains(event.position().toPoint())
            ):
                if event.type() == QEvent.Type.MouseButtonRelease:
                    QTimer.singleShot(0, lambda i=index: self._edit_dict(i))
                return True
        except Exception:  # noqa: BLE001 —— 事件里不能抛异常
            return False
        return super().editorEvent(event, model, option, index)

    def _edit_dict(self, index) -> None:
        try:
            if self._model.layer.isEditable() and self._view.isVisible():
                self._view.edit(index)
        except RuntimeError:
            pass

    def createEditor(self, parent, option, index):  # noqa: N802 —— Qt 命名
        info = self._model.dict_info(index.column())
        if info is None:
            return super().createEditor(parent, option, index)
        current = index.model().data(index, Qt.ItemDataRole.EditRole)
        editor = _DictTreeCombo(parent, info, current)
        editor.valuePicked.connect(lambda: self._commit_editor(editor))

        def _open_popup() -> None:
            # 编辑器可能在下一个事件循环前就被销毁（用户马上点走/切行）
            try:
                if editor.isVisible():
                    editor.showPopup()
            except RuntimeError:
                pass

        # 点箭头进入编辑态后直接展开字典树，不用再点一次下拉按钮
        QTimer.singleShot(0, _open_popup)
        return editor

    def _commit_editor(self, editor) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(
            editor, QAbstractItemDelegate.EndEditHint.NoHint
        )

    def setEditorData(self, editor, index):  # noqa: N802 —— Qt 命名
        if not isinstance(editor, _DictTreeCombo):
            return super().setEditorData(editor, index)
        value = index.model().data(index, Qt.ItemDataRole.EditRole)
        editor.set_value(value)

    def setModelData(self, editor, model, index):  # noqa: N802 —— Qt 命名
        if not isinstance(editor, _DictTreeCombo):
            return super().setModelData(editor, model, index)
        # 当前值（EditRole 是原始 code）：没变就别写回，否则点开下拉又关掉也会
        # 触发 changeAttributeValue，把图层标成「有未保存修改」
        current = index.model().data(index, Qt.ItemDataRole.EditRole)
        if editor.is_cleared():
            if current is None or current == "":
                return
            model.setData(index, None, Qt.ItemDataRole.EditRole)
            return
        code = editor.selected_code()
        if not code:
            return
        current_text = "" if current is None else str(current)
        if str(code) == current_text:
            return
        model.setData(index, code, Qt.ItemDataRole.EditRole)


class _SortHeaderView(QHeaderView):
    """属性表表头：**只有右侧的三角箭头可点来排序**。

    整列点击不排序（用户要求）——拖列宽 / 调整列时很容易误触整列点击。
    每列右侧常驻一个箭头（未排序时是淡色的上下双箭头，已排序时按方向显示
    单个实心三角），点击箭头才发出 :attr:`sortClicked`。
    """

    sortClicked = pyqtSignal(int)  # N815 —— Qt 信号命名

    #: 箭头命中区宽度（含留白）
    _ARROW_WIDTH = 18

    def __init__(self, orientation, parent=None) -> None:
        super().__init__(orientation, parent)
        # 自己画箭头：**已排序列**常驻，其它列只在鼠标悬停时显示（不留一列
        # 多余的图标）；QHeaderView 自带的指示器只在已排序列画，关掉避免重影
        self.setSortIndicatorShown(False)
        self.setSectionsClickable(True)
        self.setMouseTracking(True)
        self._hover_section = -1

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        section = int(self.logicalIndexAt(event.position().toPoint()))
        if section != self._hover_section:
            self._hover_section = section
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        if self._hover_section != -1:
            self._hover_section = -1
            self.viewport().update()
        super().leaveEvent(event)

    def arrow_rect(self, section: int) -> QRect:
        """某列右侧箭头的矩形（表头坐标系）。"""
        if section < 0:
            return QRect()
        width = int(self.sectionSize(section))
        if width <= 0:
            return QRect()
        left = int(self.sectionViewportPosition(section)) + width - self._ARROW_WIDTH
        # 右边留 3px 给「拖列宽」的边界热区，别让点箭头把拖拽也吃掉
        return QRect(left, 0, self._ARROW_WIDTH - 3, self.height())

    def paintSection(self, painter, rect, logicalIndex) -> None:  # noqa: N802 —— Qt 命名
        super().paintSection(painter, rect, logicalIndex)
        if logicalIndex < 0 or not rect.isValid():
            return
        try:
            sorted_here = (
                self.sortIndicatorSection() >= 0
                and self.sortIndicatorSection() == logicalIndex
            )
            # 未排序列只有鼠标悬停时才露出（否则每列右边都多一个图标）
            if not sorted_here and logicalIndex != self._hover_section:
                return
            painter.save()
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                order = self.sortIndicatorOrder()
                color = self.palette().color(QPalette.ColorRole.WindowText)
                if not sorted_here:
                    color.setAlpha(105)  # 未排序列：淡色提示"这里可以排序"
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                box = self.arrow_rect(logicalIndex)
                cx = box.center().x() + 0.5
                cy = float(rect.center().y())
                half = 3.6
                if sorted_here:
                    if order == Qt.SortOrder.AscendingOrder:
                        points = [QPointF(cx - half, cy + 2), QPointF(cx + half, cy + 2), QPointF(cx, cy - 3)]
                    else:
                        points = [QPointF(cx - half, cy - 2), QPointF(cx + half, cy - 2), QPointF(cx, cy + 3)]
                else:
                    # 未排序：上下两个小三角（可升可降）
                    points = [
                        QPointF(cx - half, cy - 2.2), QPointF(cx + half, cy - 2.2), QPointF(cx, cy - 6.2),
                        QPointF(cx - half, cy + 2.2), QPointF(cx + half, cy + 2.2), QPointF(cx, cy + 6.2),
                    ]
                painter.drawPolygon(QPolygonF(points))
            finally:
                painter.restore()
        except Exception:  # noqa: BLE001 —— 绘制里不能抛异常
            return

    def _hit_arrow(self, pos: QPoint) -> int:
        """坐标命中的“箭头”所在列；点的是列其它位置返回 -1。"""
        section = int(self.logicalIndexAt(pos))
        if section < 0:
            return -1
        return section if self.arrow_rect(section).contains(pos) else -1

    def mousePressEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        # 记下按下时是否落在箭头上：只有"按下和松开都在同一列的箭头"才算排序，
        # 避免在列文字上按下、拖到箭头松开时误触
        self._press_arrow = -1
        if event.button() == Qt.MouseButton.LeftButton:
            section = self._hit_arrow(event.position().toPoint())
            if section >= 0:
                self._press_arrow = section
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        press_arrow = getattr(self, "_press_arrow", -1)
        self._press_arrow = -1
        if event.button() == Qt.MouseButton.LeftButton:
            section = self._hit_arrow(event.position().toPoint())
            if section >= 0 and section == press_arrow:
                self.sortClicked.emit(section)
                event.accept()
                return
        # QHeaderView 自己会在整列点击时翻转排序指示器（即使 sortIndicatorShown
        # 为 False）。排序只由箭头驱动，这里把内部状态还原，避免"点文字也换方向"。
        old_section = self.sortIndicatorSection()
        old_order = self.sortIndicatorOrder()
        super().mouseReleaseEvent(event)
        if (self.sortIndicatorSection(), self.sortIndicatorOrder()) != (
            old_section,
            old_order,
        ):
            self.blockSignals(True)
            try:
                self.setSortIndicator(old_section, old_order)
            finally:
                self.blockSignals(False)


class _AttributeTableView(QTableView):
    """属性表视图：接管 Ctrl+A、以及"第二次点同一格才进入编辑"的鼠标行为。

    Qt 自带的 ``selectAll()`` 会把每一行都塞进选中模型，而视图处理选中变化是
    O(行数)、约 0.4 ms/行（见 :data:`MAX_TABLE_SELECTION_ROWS`）。所以这里不直接
    全选，改成问上层要"行数少就照选、行数多就只选要素"。

    编辑：第一次点单元格只聚焦；**再点一次同一格**才开编辑器（字典格只认右侧
    箭头，见 :class:`_DictValueDelegate`）。键盘 F2 / 直接输入仍可编辑。
    """

    #: 用户按了 Ctrl+A
    select_all_requested = pyqtSignal()

    #: 视图几何变化（尺寸 / 内边距被 Qt 重算）后的回调。宿主把它指到
    #: :meth:`PagedAttributeTableWidget._update_frozen_geometry`：``QTableView``
    #: 会在每次 ``updateGeometries`` 里把 ``setViewportMargins`` 重置回默认
    #: （只按表头留边），所以冻结区必须在这里**重新**压回去。
    on_resized = None

    def resizeEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        super().resizeEvent(event)
        callback = self.on_resized
        if callback is not None:
            try:
                callback()
            except Exception:  # noqa: BLE001 —— 槽里未捕获异常是原生崩溃，兜住
                pass

    def mousePressEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        """记录按下时它是不是**当前单元格**（判断是不是"第二次点同一格"）。"""
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.position().toPoint())
            self._press_index = index
            self._press_was_current = bool(
                index.isValid() and index == self.currentIndex()
            )
        else:
            self._press_index = None
            self._press_was_current = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        """第二次点同一格才开始编辑（第一次只聚焦）。

        字典单元格例外：编辑入口是单元格右侧的箭头（见 :class:`_DictValueDelegate`），
        点正文不弹编辑器 —— 避免"点开字典看一眼"就进编辑态。
        """
        super().mouseReleaseEvent(event)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        index = self.indexAt(event.position().toPoint())
        if not (
            index.isValid()
            and index == getattr(self, "_press_index", None)
            and getattr(self, "_press_was_current", False)
        ):
            return
        model = self.model()
        dict_info = getattr(model, "dict_info", None)
        if dict_info is not None and dict_info(index.column()) is not None:
            return  # 字典格：只认右侧箭头
        QTimer.singleShot(0, lambda i=index: self.edit(i))

    def keyPressEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.select_all_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class _LocatedHeaderView(QHeaderView):
    """行号栏（表格**最左边那一窄列**）：给「当前定位行」画一个 ▶（ArcMap 那样）。

    画在这里而不是画成模型的一列：模型那边第 0 列是 ID、后面跟着字段，插一列会牵动
    数据/编辑/导出/隐藏列一大串；行号栏本来就空着，正合适。
    没有定位行（比如表是空的）时什么都不画。
    """

    #: 这一窄列的宽度
    GUTTER_W = 20

    def __init__(self, orientation, parent=None) -> None:
        super().__init__(orientation, parent)
        self.setFixedWidth(self.GUTTER_W)

    def paintSection(self, painter, rect, logicalIndex) -> None:  # noqa: N802 —— Qt 命名
        super().paintSection(painter, rect, logicalIndex)
        row = getattr(self.model(), "located_row", -1)
        if row < 0 or logicalIndex != row or rect.height() <= 0:
            return
        try:
            from qgis.PyQt.QtCore import QPoint
            painter.save()
            painter.setRenderHint(painter.RenderHint.Antialiasing, True)
            color = QColor(ThemeManager.instance().current().menu_text)
            painter.setPen(color)
            painter.setBrush(color)
            cy = rect.center().y()
            cx = rect.center().x()
            painter.drawPolygon(QPoint(cx - 3, cy - 4), QPoint(cx - 3, cy + 4), QPoint(cx + 3, cy))
            painter.restore()
        except RuntimeError:
            pass  # 收尾阶段 C++ 对象已销毁


class PagedAttributeTableWidget(QWidget):
    """可停靠的属性表 widget。"""

    zoom_to_map = pyqtSignal(str, object)

    #: 表格里选中行有变化 → 选中集的 fid 列表 ``list[int]``（交给上层改图层选中集）。
    selection_changed = pyqtSignal(list)
    #: 双击某行单元格 → 请上层**定位 + 在它上面闪几下**（``fid``）。
    feature_activated = pyqtSignal(int)
    #: 点了「缩放到所选记录」→ 请上层把地图缩放到选中要素（画布的事归 Dock 管）
    zoom_to_selection_requested = pyqtSignal()

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
        #: 被冻结的列（按名存；ID 列用 :data:`_ID_COLUMN_KEY`）。冻结列由**冻结视图**
        #: 显示，主表里对应隐藏（见 :meth:`_sync_column_visibility`）。
        self._frozen_keys: set[str] = set()
        #: 冻结视图（主表的子控件，盖在左侧；无冻结列时隐藏）。模型建好后装配，
        #: 见 :meth:`_setup_frozen_view`。
        self.frozen_view: Optional[_AttributeTableView] = None
        #: 装了列头右键事件过滤器的 widget → 对应的列表头（见 :meth:`_install_header_menu`）
        self._header_event_targets: dict[QObject, object] = {}
        #: 程序性改表格选中集期间为 True：防止"表格→图层→表格"来回触发成环
        self._suppress_selection_sync = False
        #: 刚排过序、行号整体变了，等加载完要按 fid 把选中重新标回去
        self._pending_reselect = False
        #: 正在进行的导出（None = 没在导）。分批写，见 _export_csv / _export_chunk
        self._export = None
        self._export_timer = QTimer(self)
        self._export_timer.setSingleShot(True)
        self._export_timer.timeout.connect(self._export_chunk)
        #: (按钮, 图标 key)：这些图标是**生成时**把主题色烤进画布的，切主题要按新颜色重画
        self._icon_items: list[tuple[QPushButton, str]] = []
        self.editing_service = LayerEditService()
        # 回滚 = 缓冲里的增删改全部作废：表里必须重扫一遍，否则删掉的行不会回来
        layer.afterRollBack.connect(self._on_layer_rolled_back)
        # 编辑态可能从**别的入口**变化（地图工具条开始/停止编辑）：按钮跟着真实状态走
        layer.editingStarted.connect(self._refresh_edit_buttons)
        layer.editingStopped.connect(self._refresh_edit_buttons)
        self._setup_ui()
        self._refresh_edit_buttons()
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)

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
        # 删除选中要素：从单元格右键菜单提到工具条（用户报"只在右键里不方便"）。
        # 与右键「删除」同一条路（图层编辑缓冲 + 撤销命令），可用性由真实
        # 编辑态 + 选中集推导，见 :meth:`_refresh_delete_button`。
        self.delete_btn = QPushButton()
        self.delete_btn.setToolTip("删除选中的要素（需先开始编辑）")
        self.delete_btn.setFixedSize(26, 26)
        self._icon_items.append((self.delete_btn, "trash"))
        self.delete_btn.clicked.connect(self._delete_selected_features)
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
        self.columns_btn.setToolTip("组织列")
        self.columns_btn.setFixedSize(26, 26)
        self._icon_items.append((self.columns_btn, "organize_columns"))
        self.export_btn = QPushButton()
        self.export_btn.setObjectName("exportCsvBtn")
        self.export_btn.setToolTip("导出 CSV（导出表格当前内容：含筛选、不含隐藏列）")
        self.export_btn.setFixedSize(26, 26)
        self._icon_items.append((self.export_btn, "export_csv"))
        self.start_btn.clicked.connect(self._start_edit)
        self.save_btn.clicked.connect(self._save_edit)
        self.stop_btn.clicked.connect(self._stop_edit)
        self.filter_btn.clicked.connect(self._filter)
        self.calc_btn.clicked.connect(self._field_calc)
        self.columns_btn.clicked.connect(self._organize_columns)
        self.export_btn.clicked.connect(self._export_csv)
        # 选中操作：图标按钮（与这一排其它按钮同款），放在导出前面
        self.refresh_btn = QPushButton()
        self.refresh_btn.setToolTip("刷新：按当前筛选 / 显示范围重新读一遍")
        self.refresh_btn.clicked.connect(self._refresh_table)
        self.zoom_selection_btn = QPushButton()
        self.zoom_selection_btn.setToolTip("缩放到所选记录")
        self.zoom_selection_btn.clicked.connect(self.zoom_to_selection_requested.emit)
        self.select_all_btn = QPushButton()
        self.select_all_btn.setToolTip("全选（等同 Ctrl+A）")
        self.select_all_btn.clicked.connect(self._on_select_all_requested)
        self.invert_btn = QPushButton()
        self.invert_btn.setToolTip("反选：没选中的选中、选中的取消")
        self.invert_btn.clicked.connect(self._invert_selection)
        self.clear_selection_btn = QPushButton()
        self.clear_selection_btn.setToolTip("取消选择（表格与地图是同一份选中集）")
        self.clear_selection_btn.clicked.connect(self._clear_selection)
        for b in (self.select_all_btn, self.invert_btn, self.clear_selection_btn,
                  self.refresh_btn, self.zoom_selection_btn):
            b.setFixedSize(26, 26)        # 与这一排小按钮同尺寸
        self._icon_items += [
            (self.refresh_btn, "refresh"),
            (self.zoom_selection_btn, "zoom_to_selection"),
            (self.select_all_btn, "select_all"),
            (self.invert_btn, "invert_selection"),
            (self.clear_selection_btn, "clear_selection"),
        ]
        for b in (self.start_btn, self.save_btn, self.stop_btn,
                  self.delete_btn,
                  self.refresh_btn, self.filter_btn, self.calc_btn,
                  self.columns_btn,
                  self.zoom_selection_btn,
                  self.select_all_btn, self.invert_btn, self.clear_selection_btn,
                  self.export_btn):
            tb.addWidget(b)
        tb.addStretch(0)
        main_layout.addLayout(tb)

        # 表格
        self.table_view = _AttributeTableView()
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # 多选：普通点=只选它，Ctrl 点=加/减，Shift 点=连选（与 QGIS 属性表一致）
        self.table_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        header = _SortHeaderView(Qt.Orientation.Horizontal, self.table_view)
        self.table_view.setHorizontalHeader(header)
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
        # 排序：**不**用 setSortingEnabled(True)——它一开启就会按当前指示列立刻排一次
        # （默认第 0 列），而我们要的是"点哪列的箭头才排哪列"。
        # 只有列右侧的三角箭头可点（_SortHeaderView），点整列不排序。
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)   # 未排序
        header.sortClicked.connect(self._on_header_clicked)
        # 列头右键 → 冻结 / 取消冻结该列（见 _show_header_menu）
        self._install_header_menu(header)
        self.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self._show_context_menu)
        main_layout.addWidget(self.table_view)

        # 冻结列视图：主表的子控件，浮在左侧盖住主表行号栏那一带（见
        # _update_frozen_geometry）。自身画一条行号栏（▶ 定位标记）+ 冻结列，
        # 与主表共享 model / selectionModel / 纵向滚动。默认隐藏，不占空间。
        self.frozen_view = _AttributeTableView(self.table_view)
        self.frozen_view.setObjectName("FrozenColumnsView")
        self.frozen_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.frozen_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.frozen_view.setAlternatingRowColors(True)
        self.frozen_view.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self.frozen_view.setVerticalHeader(
            _LocatedHeaderView(Qt.Orientation.Vertical, self.frozen_view)
        )
        fheader = _SortHeaderView(Qt.Orientation.Horizontal, self.frozen_view)
        self.frozen_view.setHorizontalHeader(fheader)
        fheader.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        fheader.setStretchLastSection(False)
        fheader.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self._install_header_menu(fheader)
        self.frozen_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.frozen_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.frozen_view.hide()

        # 底部动态加载条（加载状态 + 加载更多按钮）
        self._setup_load_bar(main_layout)

        self.setLayout(main_layout)
        # 图标是**生成时**把主题色烤进画布的（与 Icons.* 那批固定配色图片不同）：
        # 等上下两条工具条都建完再统一画一遍；切换主题时由 _on_theme_changed 重画。
        self._apply_icons(ThemeManager.instance().current().toolbar_text)

        self.model = PagedAttributeTableModel(self.layer, self.page_size, filter_str=self.filter_str, show_ids=self.show_ids)
        self.table_view.setModel(self.model)
        # 挂了字典的字段：显示映射成名称、编辑时按字典域下拉取值
        self._dict_delegate = _DictValueDelegate(self.table_view, self.model)
        self.table_view.setItemDelegate(self._dict_delegate)
        if self._preset_ids:
            # 让下拉如实反映"当前只看指定子集"
            idx = self.mode_combo.findData("preset")
            if idx >= 0:
                self.mode_combo.setCurrentIndex(idx)
        # 筛选重置后刷新加载状态；流式 chunk/结束也自动刷新；滚动到底自动加载下一批
        self.model.modelReset.connect(self._refresh_load_state)
        self.model.state_changed.connect(self._refresh_load_state)
        # 排序后要等加载完才能按 fid 把选中标回去
        self.model.modelReset.connect(self._reselect_if_ready)
        self.model.state_changed.connect(self._reselect_if_ready)
        # 字段刷新/筛选重置后模型列数变化，按名重算隐藏列（新增字段默认可见）
        self.model.modelReset.connect(self._sync_column_visibility)
        # 列表刷新 / 换显示范围 → 定位行归位；有数据后默认落到第 1 行
        self.model.modelReset.connect(self._on_model_reset)
        self.model.state_changed.connect(self._ensure_located_row)
        # 小图层在**模型构造时**就同步加载完了（那时这些信号还没接上），补一次：
        # 保证"打开属性表即默认落在第 1 行 / 空表显示 0"。
        self._on_model_reset()
        self._ensure_located_row()
        self.table_view.verticalScrollBar().valueChanged.connect(self._on_scroll)
        # 表格选中行 → 图层选中集（**单击/多选都走这里**，不再单独接 clicked：
        # 选中集一变就同步，行为与 QGIS 属性表一致）；双击只是额外闪一下
        sel_model = self.table_view.selectionModel()
        if sel_model is not None:
            sel_model.selectionChanged.connect(self._on_table_selection_changed)
        self.table_view.doubleClicked.connect(self._on_row_double_clicked)
        # 行号栏（最左那一窄列）换成能画 ▶ 的：**「当前定位行」的标记画在这儿**
        # （ArcMap 那样）。点单元格**不改变**定位行，所以不接 clicked/currentChanged。
        self.table_view.setVerticalHeader(
            _LocatedHeaderView(Qt.Orientation.Vertical, self.table_view)
        )
        # Ctrl+A 自己接管：行数多时不能真的逐行全选（Qt 要 ~0.4ms/行，见视图类注释）
        self.table_view.select_all_requested.connect(self._on_select_all_requested)
        self._setup_frozen_view()
        self._refresh_load_state()

    def _setup_load_bar(self, main_layout) -> None:
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(4)

        # 「转到特定记录」：首/前 + 行号 + 后/末（放在显示范围下拉前，同 ArcMap）
        self.goto_first_btn = QPushButton()
        self.goto_first_btn.setToolTip("移到表开始处")
        self.goto_first_btn.clicked.connect(self._goto_first)
        self.goto_prev_btn = QPushButton()
        self.goto_prev_btn.setToolTip("前一条记录")
        self.goto_prev_btn.clicked.connect(lambda: self._step_row(-1))
        self.row_edit = QLineEdit()
        self.row_edit.setFixedWidth(60)
        self.row_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.row_edit.setToolTip("输入**行号**（第几条记录）后回车，跳到该行")
        self.row_edit.returnPressed.connect(self._goto_row_from_edit)
        self.goto_next_btn = QPushButton()
        self.goto_next_btn.setToolTip("下一条记录")
        self.goto_next_btn.clicked.connect(lambda: self._step_row(1))
        self.goto_last_btn = QPushButton()
        self.goto_last_btn.setToolTip("移到表结束处")
        self.goto_last_btn.clicked.connect(self._goto_last)
        for b in (self.goto_first_btn, self.goto_prev_btn, self.goto_next_btn, self.goto_last_btn):
            b.setFixedSize(24, 22)
        self._icon_items += [
            (self.goto_first_btn, "row_first"),
            (self.goto_prev_btn, "row_prev"),
            (self.goto_next_btn, "row_next"),
            (self.goto_last_btn, "row_last"),
        ]
        for wdg in (self.goto_first_btn, self.goto_prev_btn, self.row_edit,
                    self.goto_next_btn, self.goto_last_btn):
            bar.addWidget(wdg)
        bar.addSpacing(6)

        # 显示范围：全部要素（按筛选）/ 仅选中要素（切了就重流，见 _on_display_mode_changed）
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("全部要素", "all")
        self.mode_combo.addItem("仅选中要素", "selected")
        # 打开时就带着子集的入口（「查看失败的要素」这类）：单给一个档位，
        # 免得下拉写着"全部要素"、表里却只有一部分
        self._preset_ids = list(self.show_ids) if self.show_ids else []
        if self._preset_ids:
            self.mode_combo.addItem("仅指定要素", "preset")
        self.mode_combo.setToolTip("「仅选中要素」只列出当前选中集，并随选中集实时更新")
        self.mode_combo.currentIndexChanged.connect(self._on_display_mode_changed)
        bar.addWidget(self.mode_combo)

        self.load_status_label = QLabel("加载中…")
        bar.addWidget(self.load_status_label)

        # 选中了多少条（没选中就不显示）
        bar.addSpacing(10)
        self.selection_label = QLabel("")
        bar.addWidget(self.selection_label)

        self.load_more_btn = QPushButton("加载更多")
        self.load_more_btn.clicked.connect(self._do_load_batch)
        bar.addWidget(self.load_more_btn)

        self.load_all_btn = QPushButton("加载全部")
        self.load_all_btn.setToolTip("一次性把结果全部加载完（大图层会跑一会儿）")
        self.load_all_btn.clicked.connect(self._load_all)
        bar.addWidget(self.load_all_btn)
        # 打开即全量时这两个按钮没有意义（表本来就会加载完）
        if LOAD_ALL_ON_OPEN:
            self.load_more_btn.hide()
            self.load_all_btn.hide()

        bar.addStretch()

        # 导出进度（只在导出期间显示）
        self.export_progress = QProgressBar()
        self.export_progress.setFixedWidth(170)
        self.export_progress.setTextVisible(True)
        self.export_progress.hide()
        bar.addWidget(self.export_progress)

        main_layout.addLayout(bar)

    def _invert_selection(self) -> None:
        """反选：表里没选中的那些改成选中、已选中的取消。

        走的是和「全选」同一条路：本地先按行改（小选中进 Qt 选中集、超限自动走底色），
        再把新的 fid 列表交给上层去改图层选中集——不能只发信号等回路，表格→图层这条
        路上的防环标志会把回路挡住。
        """
        model = self.model
        if model is None or self.layer is None:
            return
        try:
            selected = set(self.layer.selectedFeatureIds())
        except RuntimeError:
            return
        fids = model.fids
        rows = [row for row in range(len(fids)) if fids[row] not in selected]
        self._suppress_selection_sync = True
        try:
            self._select_rows(rows)
        finally:
            self._suppress_selection_sync = False
        self._emit_selection([fids[row] for row in rows])

    # ------------------------------------------------------------ 转到特定记录

    def _goto_row(self, row) -> None:
        """跳到第 ``row`` 行（0 基）：滚到它、标成「当前定位行」，**不动选择集**。

        ArcMap 里"转到某条记录"只是把当前记录挪过去，已选中的要素**不该被清掉**，
        所以这里用 ``NoUpdate`` 只移"当前项"、不碰选中集；定位标记就是行号栏里的 ▶
        （见 :class:`_LocatedHeaderView`），**不额外给行底色**。
        """
        model = self.model
        if model is None or model.rowCount() == 0:
            return
        row = max(0, min(int(row), model.rowCount() - 1))
        idx = model.index(row, 0)
        self.table_view.scrollTo(idx, QAbstractItemView.ScrollHint.PositionAtCenter)
        sel_model = self.table_view.selectionModel()
        if sel_model is not None:
            sel_model.setCurrentIndex(idx, QItemSelectionModel.SelectionFlag.NoUpdate)
        if model.set_located_row(row):
            self._repaint_located()
        self._sync_row_edit(row)

    def _repaint_located(self) -> None:
        """定位标记变了 → 让表格与行号栏都重画。"""
        self.table_view.viewport().update()
        header = self.table_view.verticalHeader()
        if header is not None:
            header.viewport().update()
        if self.frozen_view is not None:
            self.frozen_view.viewport().update()
            fheader = self.frozen_view.verticalHeader()
            if fheader is not None:
                fheader.viewport().update()

    def _on_model_reset(self) -> None:
        """表被重置（刷新 / 换筛选 / 换显示范围）→ 定位行先归零。

        数据是随后异步到的，等它到了再由 :meth:`_ensure_located_row` 默认落到第 1 行。
        """
        model = self.model
        if model is None:
            return
        if model.set_located_row(-1):
            self._repaint_located()
        self._sync_row_edit(-1)

    def _ensure_located_row(self) -> None:
        """还没定位行、而表里已经有数据 → 默认定位到**第 1 行**（ArcMap 那样）。"""
        model = self.model
        if model is None or model.located_row >= 0 or model.rowCount() == 0:
            return
        model.set_located_row(0)
        self._repaint_located()
        self._sync_row_edit(0)

    def _goto_first(self) -> None:
        self._goto_row(0)

    def _goto_last(self) -> None:
        model = self.model
        self._goto_row(model.rowCount() - 1 if model is not None else 0)

    def _step_row(self, delta: int) -> None:
        """前 / 后一条记录：从**当前定位行**出发。

        不能用表格的 currentIndex——鼠标点单元格也会把 currentIndex 挪走，而这一套
        是"只有左下角的按钮才切换当前记录"（用户要求，同 ArcMap）。
        """
        model = self.model
        if model is None or model.rowCount() == 0:
            return
        base = model.located_row if model.located_row >= 0 else 0
        self._goto_row(base + delta)

    def _goto_row_from_edit(self) -> None:
        """文本框里输入的是**行号**（从 1 数起），回车即跳过去；非法输入还原。"""
        text = self.row_edit.text().strip()
        model = self.model
        if not text.isdigit():
            self._sync_row_edit(model.located_row if model is not None else -1)
            return
        self._goto_row(int(text) - 1)

    def _sync_row_edit(self, row: int) -> None:
        """把文本框刷成定位行号（**1 基**）；没有定位行 / 没有数据就显示 **0**。

        "0" 是 ArcMap 的说法：表里没数据，谈不上第几条。
        """
        model = self.model
        if model is None or row < 0 or model.rowCount() == 0:
            self.row_edit.setText("0")
            return
        self.row_edit.setText(str(max(0, min(int(row), model.rowCount() - 1)) + 1))

    # ------------------------------------------------------------ 刷新 / 缩放到所选

    def _refresh_table(self) -> None:
        """刷新：按当前筛选 / 显示范围重新读一遍（字段也可能改过，一并重读）。"""
        model = self.model
        if model is None or self.layer is None:
            return
        self._suppress_selection_sync = True
        try:
            model.reload_fields()
            if self.mode_combo.currentData() == "selected":
                try:
                    model.set_show_ids(self.layer.selectedFeatureIds(), True)
                except RuntimeError:
                    model.set_show_ids([], True)
            else:
                model.set_filter(model.filter_str)
        finally:
            self._suppress_selection_sync = False
        self._refresh_load_state()

    def _set_selection_count(self, count: int) -> None:
        """底部显示"已选中 N 条"（没选中就不显示），顺带刷新工具条删除按钮。"""
        self.selection_label.setText(f"已选中 {count} 条" if count else "")
        # 选中集变化是所有入口（表格点选 / 地图选择 / 全选 / 反选 / 清空）的必经之路；
        # 传 count 而不是回查图层：此刻图层选中集还没被上层替换（见 _emit_selection）
        self._refresh_delete_button(count)

    def _emit_selection(self, fids) -> None:
        """把新的选中集交给上层，**顺带把底部计数刷了**。

        表格→图层 这条路上的防环标志会把回路挡住（上层不会回头喊 apply_layer_selection），
        所以计数必须在这里就更新，不能等回流。
        """
        self._set_selection_count(len(fids))
        self.selection_changed.emit([int(f) for f in fids])

    def _apply_icons(self, color: str) -> None:
        """按 ``color`` 重画这一排图标。"""
        for btn, key in self._icon_items:
            btn.setIcon(tool_icons.icon(key, color))

    def _on_theme_changed(self, theme=None) -> None:
        """主题切换 → 用新颜色重画图标。

        「组织列 / 全选 / 反选 / 取消选择 / 导出」这几个图标是
        ``tool_icons.icon(key, 颜色)`` **在生成时**把颜色烤进画布的（与 ``Icons.*``
        那批固定配色的图片不同）。不重画的话，切到深色模式后仍是按浅色主题生成的深色
        描边，落在深色按钮底上就看不清了（用户报过）。
        """
        try:
            self._apply_icons((theme or ThemeManager.instance().current()).toolbar_text)
            # 主题可能连字体一起换（表头高度变）→ 重算冻结区几何，避免错位
            self._update_frozen_geometry()
        except RuntimeError:
            pass  # 收尾阶段 C++ 对象已销毁

    def _clear_selection(self) -> None:
        """取消选择：清掉表格选中（连带清掉图层选中集，二者本来就是同一份）。"""
        self._suppress_selection_sync = True
        try:
            self.table_view.clearSelection()
            self._set_band_rows(())
        finally:
            self._suppress_selection_sync = False
        self._emit_selection([])

    # ------------------------------------------------------------ 显示范围

    def _on_display_mode_changed(self, *_args) -> None:
        """「全部要素 / 仅选中要素 / 仅指定要素」切换 → 原地重流（不重建表格）。"""
        mode = self.mode_combo.currentData()
        if mode == "selected":
            self._show_selected_only()
        elif mode == "preset":
            self._set_show_ids(self._preset_ids, True)
        else:
            self._set_show_ids([], False)

    def _show_selected_only(self) -> None:
        """把显示范围设为当前选中集（当前没选中就是"只看空集"）。"""
        if self.model is None or self.layer is None:
            return
        try:
            self._set_show_ids(self.layer.selectedFeatureIds(), True)
        except RuntimeError:
            self._set_show_ids([], True)

    def _set_show_ids(self, ids, active: bool) -> None:
        """换显示子集并重流；期间抑制"表格选中变化→改图层选中集"的回流。"""
        self._suppress_selection_sync = True
        try:
            if self.model is not None:
                self.model.set_show_ids(ids, active)
        finally:
            self._suppress_selection_sync = False
        self._refresh_load_state()

    def _load_all(self) -> None:
        """「加载全部」：让模型一口气把结果加载完。"""
        if self.model is not None:
            self.model.load_all()

    def _refresh_load_state(self) -> None:
        """按模型加载进度刷新状态文本与"加载更多"按钮可用性。"""
        if self.model is None:
            return
        loaded = self.model.rowCount()
        total = self.model.total_features
        if self.mode_combo.currentData() == "selected" and not self.model.show_ids:
            self.load_status_label.setText("当前没有选中要素（在地图上选择，或用选择工具框选）")
            self.load_more_btn.setEnabled(False)
            self.load_all_btn.setEnabled(False)
            return
        if self.model.scanning:
            self.load_status_label.setText(f"正在加载，已获取 {loaded} 条…")
            self.load_more_btn.setEnabled(self.model.has_more)
            self.load_all_btn.setEnabled(self.model.has_more)
            return
        if self.model.truncated:
            self.load_status_label.setText(
                f"已显示前 {loaded} 条（结果过多，仅加载前 {MAX_MODEL_FIDS} 条，请缩小筛选范围）"
            )
            self.load_more_btn.setEnabled(False)
            self.load_all_btn.setEnabled(False)
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
        self.load_all_btn.setEnabled(self.model.has_more)

    # ------------------------------------------------------------ 表格选中集 ⇄ 图层

    def _on_table_selection_changed(self, *_args) -> None:
        """表格里选中的行变了 → 把对应 fid 列表交给上层去改图层选中集。

        **必须走 ``selection()`` 的区段**，不能用 ``selectedRows(0)``：后者会给每一行
        造一个 QModelIndex，全选 14.8 万行实测要 **67 秒**（原生卡死）；按区段取只要
        个位数毫秒（全选就是一个区段，切片一次拿完）。
        """
        if self._suppress_selection_sync:
            return
        try:
            model = self.model
            sel_model = self.table_view.selectionModel()
            if model is None or sel_model is None:
                return
            fids = model.fids
            n = len(fids)
            selected: List[int] = []
            for rng in sel_model.selection():
                top, bottom = rng.top(), rng.bottom()
                if top > bottom or top >= n:
                    continue
                selected.extend(fids[top:min(bottom, n - 1) + 1])
            # 用户自己在表里改了选中 → 之前那批"底色高亮"作废（它属于上一次的大选中）
            self._set_band_rows(())
            self._emit_selection(selected)
        except Exception:  # noqa: BLE001 —— 槽里未捕获异常是原生崩溃
            logger.debug("同步表格选中集失败", exc_info=True)

    def _on_row_double_clicked(self, index) -> None:
        """双击单元格：请上层**定位到它并闪几下**（选中本身已由选中集同步做完）。"""
        try:
            model = self.model
            if model is None or not index.isValid():
                return
            row = index.row()
            if 0 <= row < len(model.fids):
                self.feature_activated.emit(int(model.fids[row]))
        except Exception:  # noqa: BLE001
            logger.debug("请求闪烁要素失败", exc_info=True)

    def apply_layer_selection(self, fids) -> int:
        """图层选中集变了 → 让表格选中对应行；没加载的 fid 先临时补进表里。

        这就是"地图上选中要素 → 属性表自动选中行"。补插是为了让**还没流式加载到**
        的要素也有行可选中；它们只在被选中期间存在（见 ``drop_stale_extras``）。

        :return: 因"选中集太大"而没能补进表的条数（>0 时已顺带提示过）
        """
        model = self.model
        if model is None:
            return 0
        self._set_selection_count(len(fids))
        if self.mode_combo.currentData() == "selected":
            # 「仅选中要素」模式下，表的**行集合本身就是选中集** → 直接跟着重流，
            # 不必（也无需）在表里给未加载的要素补插行。
            self._set_show_ids(fids, True)
            return 0
        self._suppress_selection_sync = True
        try:
            wanted = [int(f) for f in fids]
            # 选中太多时走"底色高亮"，不必再往表里补插临时行（那些行也不进 Qt 选中集）
            too_many = len(wanted) > MAX_TABLE_SELECTION_ROWS
            # **只有全量表才补插**：带筛选 / 带子集的表（质检结果、空间查询、只看选中…）
            # 是"就显示这些"的约定，往里塞不匹配的行会污染结果。
            can_insert = (not too_many) and model.shows_everything
            missing = [f for f in wanted if model.row_of_fid(f) < 0] if can_insert else []
            skipped = 0
            if missing:
                if len(missing) > EXTRA_SELECT_LIMIT:
                    skipped = len(missing) - EXTRA_SELECT_LIMIT
                    missing = missing[:EXTRA_SELECT_LIMIT]
                model.add_extra_fids(missing)
            model.drop_stale_extras(wanted)
            rows = sorted({r for r in (model.row_of_fid(f) for f in wanted) if r >= 0})
            self._select_rows(rows)
            if skipped:
                self._notify(
                    f"选中要素过多，属性表只同步了 {EXTRA_SELECT_LIMIT} 条未加载的要素",
                    warn=True,
                )
            return skipped
        finally:
            self._suppress_selection_sync = False

    def _select_rows(self, rows) -> None:
        """把表格选中集设成这些行（程序性；外层已置 :attr:`_suppress_selection_sync`）。

        **按连续区段一次 select**，别逐行 ``select(idx, idx)``。行数超过
        :data:`MAX_TABLE_SELECTION_ROWS` 时**不进 Qt 选中集**（Qt 要 ~0.4 ms/行，
        14.8 万行 ≈65 秒），改用模型底色画高亮——视觉一样，代价只随可视行走。
        """
        sel_model = self.table_view.selectionModel()
        if sel_model is None or self.model is None:
            return
        ordered = sorted(set(rows))
        if len(ordered) > MAX_TABLE_SELECTION_ROWS:
            sel_model.clearSelection()
            self._set_band_rows(ordered)
            return
        self._set_band_rows(())
        selection = QItemSelection()
        if ordered:
            start = prev = ordered[0]
            for row in ordered[1:]:
                if row == prev + 1:
                    prev = row
                    continue
                selection.select(self.model.index(start, 0), self.model.index(prev, 0))
                start = prev = row
            selection.select(self.model.index(start, 0), self.model.index(prev, 0))
        flags = (QItemSelectionModel.SelectionFlag.Clear
                 | QItemSelectionModel.SelectionFlag.Select
                 | QItemSelectionModel.SelectionFlag.Rows)
        sel_model.select(selection, flags)
        if ordered:
            self.table_view.scrollTo(self.model.index(ordered[0], 0),
                                     QAbstractItemView.ScrollHint.EnsureVisible)

    def _set_band_rows(self, rows) -> None:
        """换"底色高亮"的行集合并让视图重画（只重画可视区，与行数无关）。"""
        if self.model is not None and self.model.set_band_rows(rows):
            self.table_view.viewport().update()
            if self.frozen_view is not None:
                self.frozen_view.viewport().update()

    def _on_header_clicked(self, section: int) -> None:
        """点表头 → 按该列排序；同一列再点切换升降序。"""
        model = self.model
        if model is None:
            return
        ascending = (not model.sort_ascending) if section == model.sort_column else True
        self._sort_by_column(section, ascending)

    def _sort_by_column(self, section: int, ascending: bool) -> None:
        """按指定列 / 方向排序（表头点击与列头右键菜单「升序/降序」共用）。"""
        model = self.model
        if model is None:
            return
        order = Qt.SortOrder.AscendingOrder if ascending else Qt.SortOrder.DescendingOrder
        self.table_view.horizontalHeader().setSortIndicator(section, order)
        if self.frozen_view is not None:
            self.frozen_view.horizontalHeader().setSortIndicator(section, order)
        self._suppress_selection_sync = True
        try:
            model.sort(section, order)
        finally:
            self._suppress_selection_sync = False
        # 排序后行号整体变了：按 fid 把图层选中集重新标到新行上（表还在加载就等它完）
        self._pending_reselect = True
        self._reselect_if_ready()

    def _reselect_if_ready(self) -> None:
        """排序后按 fid 重新标选中；表还在加载就先挂着，等加载完再标。"""
        model = self.model
        if not self._pending_reselect or model is None:
            return
        if model.scanning or model.has_more:
            return
        self._pending_reselect = False
        if self.layer is None:
            return
        try:
            self.apply_layer_selection(self.layer.selectedFeatureIds())
        except RuntimeError:
            pass

    def _on_select_all_requested(self) -> None:
        """Ctrl+A：行数少就照常全选；行数多则只选要素，高亮交给底色。"""
        model = self.model
        if model is None:
            return
        if model.rowCount() <= MAX_TABLE_SELECTION_ROWS:
            self.table_view.selectAll()
            return
        self._suppress_selection_sync = True
        try:
            self.table_view.clearSelection()
        finally:
            self._suppress_selection_sync = False
        # 本地就把底色打上：图层选中回路回来时，Dock 那边的 _syncing 会挡住它
        # （表格→图层这条路本来就只能在本地收尾）
        self._set_band_rows(range(model.rowCount()))
        self._emit_selection(model.fids)

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
        self.editing_service.start(self.layer)
        self._refresh_edit_buttons()

    def _save_edit(self) -> None:
        if self.layer.isEditable():
            self.editing_service.commit(self.layer)
        self._refresh_edit_buttons()

    def _refresh_edit_buttons(self) -> None:
        """开始 / 保存 / 停止三个按钮的可用性**由图层真实编辑态推导**。

        以前是「开始编辑时打开、停止编辑末尾关掉」两处各改一次 —— 中途从外部入口
        （地图工具条）停编、或「不保存」分支提前 return，按钮就跟真实状态对不上，
        看起来像"停不掉编辑"。改成单一来源后，任何入口停下都能跟上。
        """
        layer = self.layer
        try:
            editable = bool(layer is not None and layer.isEditable())
        except RuntimeError:  # 图层已被销毁
            editable = False
        self.start_btn.setEnabled(not editable)
        self.save_btn.setEnabled(editable)
        self.stop_btn.setEnabled(editable)
        self._refresh_delete_button()

    def _refresh_delete_button(self, count: int | None = None) -> None:
        """工具条「删除选中」的可用性 = 图层处于编辑态 **且** 有选中要素。

        单一来源：编辑态从图层真实 ``isEditable()`` 读。选中数优先用调用方报的
        ``count``（表格 → 图层 的同步是**先发计数、后改图层**，此刻直接问图层
        拿到的是旧选中集，按钮会慢一拍）；没传时再问图层选中集。
        """
        layer = self.layer
        try:
            editable = bool(layer is not None and layer.isEditable())
        except RuntimeError:  # 图层已销毁
            editable = False
        if count is None:
            try:
                has_selection = bool(layer is not None and layer.selectedFeatureIds())
            except RuntimeError:
                has_selection = False
        else:
            has_selection = count > 0
        btn = getattr(self, "delete_btn", None)
        if btn is not None:
            btn.setEnabled(editable and has_selection)

    def _delete_selected_features(self) -> None:
        """删除当前选中的要素（编辑缓冲内，可撤销；保存编辑时才落盘）。

        与单元格右键「删除」同一语义，但一次删掉整个选中集。包在一条编辑命令里，
        撤销时整批一起回来；``beginEditCommand`` / ``endEditCommand`` 必须成对，
        否则撤销栈会失效（见项目经验）。
        """
        layer = self.layer
        if layer is None:
            return
        try:
            if not layer.isEditable():
                return
            fids = [int(fid) for fid in layer.selectedFeatureIds()]
        except RuntimeError:  # 图层已销毁
            return
        if not fids:
            self._refresh_delete_button()
            return

        layer.beginEditCommand(f"删除 {len(fids)} 个要素")
        try:
            layer.deleteFeatures(fids)
        except Exception as exc:  # noqa: BLE001 —— 槽里抛异常会带走进程
            logger.error("属性表删除选中要素失败：%s", exc, exc_info=True)
            QMessageBox.warning(
                self, "删除失败", f"没能删除选中的要素：{exc}"
            )
        finally:
            layer.endEditCommand()
        self._refresh_delete_button()

    def _stop_edit(self) -> None:
        """停止编辑：**有未保存改动时先问是否保存**（删除等编辑只是缓冲，这一步才落盘）。

        与地图工具条的「停止编辑」同一口径：保存 / 不保存 / 取消。
        「取消」留在编辑态；保存失败也留在编辑态并把原因说出来 —— 不能假装已经停了。
        **不保存 = 回滚并结束编辑**；表格由 ``afterRollBack`` 的延迟刷新补回被删的行
        （不能在回滚的信号栈里同步重扫：后台读流会和"结束编辑"抢锁，导致停不干净）。
        三个按钮的可用性统一由 :meth:`_refresh_edit_buttons` 按真实编辑态刷新。
        """
        layer = self.layer
        if layer is None or not layer.isEditable():
            self._refresh_edit_buttons()
            return
        if layer.isModified():
            ret = QMessageBox.question(
                self,
                "停止编辑",
                f"图层「{layer.name()}」有未保存的修改，是否保存？",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if ret == QMessageBox.StandardButton.Cancel:
                return
            if ret == QMessageBox.StandardButton.Save:
                if not self.editing_service.stop(layer, save=True):
                    QMessageBox.warning(
                        self,
                        "保存失败",
                        f"图层「{layer.name()}」的修改没能保存，仍在编辑状态。\n"
                        "请检查数据源（是否被别的程序占用 / 有约束冲突）后重试。",
                    )
                    self._refresh_edit_buttons()
                    return
            else:
                self.editing_service.stop(layer, save=False)
        else:
            self.editing_service.stop(layer, save=True)
        self._refresh_edit_buttons()

    def _on_layer_rolled_back(self) -> None:
        """图层回滚：缓冲里的增删改全部作废，表必须重扫（删掉的行要回来）。

        **推迟到下一个事件循环 tick**：这个槽是在 ``rollBack()`` 的信号栈里被调的，
        当场重启后台读流会和"结束编辑"抢数据源的锁，实测会造成停编停不干净
        （用户报的"选不保存反而关不掉编辑"）。
        """
        QTimer.singleShot(0, self._refresh_after_rollback)

    def _refresh_after_rollback(self) -> None:
        try:
            if self.layer is None:  # 收尾阶段图层已销毁
                return
            self._refresh_table()
        except RuntimeError:  # 控件 / 图层已销毁
            return

    def _filter(self) -> None:
        dlg = QueryBuilderDialog(self.layer, parent=self)
        dlg.sqlConfirmed.connect(self._apply_filter)
        dlg.exec()

    def _export_rows(self) -> List[int]:
        """要导出的行号。

        **有选中就只导选中的**（表格里点的、Ctrl/Shift 多选的、地图上点选 / 框选的都算
        ——它们在同一个选中集里），并且只导落在当前显示范围内的那些；没有选中才导当前
        显示的全部行（已带上筛选 / 子集）。
        """
        model = self.model
        if model is None or self.layer is None:
            return []
        total = model.rowCount()
        try:
            selected = set(self.layer.selectedFeatureIds())
        except RuntimeError:
            selected = set()
        if not selected:
            return list(range(total))
        fids = model.fids
        return [row for row in range(total) if fids[row] in selected]

    def _export_csv(self) -> None:
        """把表格内容导成 CSV：**有选中就只导选中的**，没选中才导当前显示的全部。

        - 行：见 :meth:`_export_rows`；
        - 列：ID 列 + 未被「组织列」隐藏的字段列，表头用字段别名；
        - 编码 **utf-8-sig**（带 BOM）：Excel 双击打开中文才不乱码；
        - **分批 + 回事件循环**（:data:`_EXPORT_CHUNK`）：十几万行逐行取要素要好一会儿，
          一口气写完界面会僵住；而每行都要 ``layer.getFeature``，放后台线程会与表格
          渲染并发读同一个图层（provider 不保证线程安全），所以就在主线程分批做。
        """
        if self._export is not None:
            self._notify("正在导出，请稍候…", warn=True)
            return
        import csv  # 仅此处用到，避免模块级多一个 import

        model = self.model
        if model is None or self.layer is None:
            return
        rows = self._export_rows()
        if not rows:
            self._notify("没有可导出的行", warn=True)
            return
        path = save_file(
            self,
            "导出 CSV",
            f"{self.layer.name()}.csv",
            "CSV 文件 (*.csv);;所有文件 (*)",
        )
        if not path:
            return

        columns = [
            (i, name, alias)
            for i, (name, alias) in enumerate(zip(model.field_names, model.header_names))
            if name not in self._hidden_field_names
        ]
        # 有字典字段时先问：导出显示名称还是库里的原始代码
        dict_columns = [
            index for index, _name, _alias in columns
            if model.dict_info(index + 1) is not None
        ]
        dict_names = False
        if dict_columns:
            choice, ok = QInputDialog.getItem(
                self,
                "导出 CSV",
                "字典字段导出方式：",
                ["字典名称（显示值）", "原始代码值"],
                0,
                False,
            )
            if not ok:
                return
            dict_names = choice.startswith("字典名称")
        try:
            handle = open(path, "w", newline="", encoding="utf-8-sig")
            writer = csv.writer(handle)
            writer.writerow(["ID", *(alias for _, _, alias in columns)])
        except OSError as exc:
            logger.exception("导出 CSV 失败: %s", path)
            self._notify(f"导出失败：{exc}", warn=True)
            return
        self._export = {
            "path": path, "rows": rows, "index": 0, "written": 0,
            "columns": columns, "handle": handle, "writer": writer,
            "dict_names": dict_names,
        }
        self.export_progress.setRange(0, len(rows))
        self.export_progress.setValue(0)
        self.export_progress.setFormat("导出 %v/%m")
        self.export_progress.show()
        self._export_timer.start(0)

    def _export_chunk(self) -> None:
        """写一小批（由 ``_export_timer`` 驱动），写完再排下一批——中途一直能响应界面。"""
        state = self._export
        model = self.model
        if state is None or model is None:
            return
        rows = state["rows"]
        end = min(state["index"] + _EXPORT_CHUNK, len(rows))
        try:
            for row in rows[state["index"]:end]:
                feature = model.feature_at(row)
                if feature is None:      # 要素已被删 / 取不到，跳过这一行
                    continue
                attrs = feature.attributes()
                values = []
                for index, _name, _alias in state["columns"]:
                    raw = attrs[index] if index < len(attrs) else ""
                    if state["dict_names"]:
                        info = model.dict_info(index + 1)
                        if info is not None:
                            label = info.name_for(raw)
                            if label is not None:
                                values.append(label)
                                continue
                    values.append(format_value(raw))
                state["writer"].writerow([feature.id(), *values])
                state["written"] += 1
        except Exception:  # noqa: BLE001 —— 取值异常不该把文件留成半截还不给说法
            logger.exception("导出 CSV 时读取出错")
            self._finish_export(False)
            return
        state["index"] = end
        self.export_progress.setValue(end)
        if end >= len(rows):
            self._finish_export(True)
        else:
            self._export_timer.start(0)

    def _finish_export(self, ok: bool) -> None:
        """收尾：关文件、收进度条、报一句结果。"""
        state, self._export = self._export, None
        self._export_timer.stop()
        self.export_progress.hide()
        if state is None:
            return
        try:
            state["handle"].close()
        except OSError:
            pass
        if not ok:
            self._notify("导出失败：读取属性时出错（详情见日志）", warn=True)
            return
        written, path = state["written"], state["path"]
        if self.model is not None and self.model.has_more:
            # 表还没加载完 —— 说清楚，别让用户以为导全了
            self._notify(
                f"已导出 {written} 行到 {path}（表格仍在加载 / 已截断，导出的是当前已显示的内容）",
                warn=True,
            )
        else:
            self._notify(f"已导出 {written} 行到 {path}")

    def _abort_export(self) -> None:
        """关 Dock 时把没写完的导出收掉（别留半截文件句柄）。"""
        if self._export is not None:
            self._finish_export(False)

    def _notify(self, text: str, *, warn: bool = False) -> None:
        """给用户一句反馈：优先走主窗口的浮动消息条，拿不到再退回消息框。"""
        bar = getattr(self.window(), "messageBar", None)
        if bar is not None:
            from qgis.core import Qgis

            level = Qgis.MessageLevel.Warning if warn else Qgis.MessageLevel.Success
            try:
                bar.pushMessage(text, level=level)
                return
            except RuntimeError:  # 窗口关闭流程里 C++ 对象已销毁
                pass
        (QMessageBox.warning if warn else QMessageBox.information)(self, "导出 CSV", text)

    def _apply_filter(self, cond: str) -> None:
        """筛选确认后更新模型筛选并刷新（重新统计，回到首屏首批）。"""
        if self.model:
            # 模型重置会清掉表格选中集；那是"表变了"不是"用户取消选中"，
            # 别顺势把图层选中集也清掉（否则筛选一下地图上的选中就没了）
            self._suppress_selection_sync = True
            try:
                self.model.set_filter(cond)
            finally:
                self._suppress_selection_sync = False
            self.table_view.scrollToTop()
            self._refresh_load_state()

    def _field_calc(self) -> None:
        """打开自建字段计算器（含自建表达式构建器与自定义函数入口）。

        不用 QGIS 原生 ``QgsFieldCalculator``：其函数编辑器依赖未初始化的
        ``QgsPythonRunner``，保存会写到盘符根目录且不注册。对话框 OK 时把
        结果应用到图层（必要时自动进入编辑态），关闭后刷新表格列/值。
        """
        from app.ui.dialogs.field_calculator import FieldCalculatorDialog

        dlg = FieldCalculatorDialog(self.layer, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
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
        """按字段名重算主表 / 冻结视图的列显示，并更新冻结区几何。

        列显示走 ``QTableView.setColumnHidden``（视图层隐藏）：模型列定义不变，
        取值/编辑/筛选逻辑无需改动，列宽在重新勾选后也能保留。第 0 列是 fid
        （ID）。规则：

        - 被「组织列」隐藏的字段列 → 主表隐藏（本就该隐藏）；
        - 被冻结的列 → 主表隐藏、冻结视图显示；
        - 其余列 → 主表显示、冻结视图隐藏。
        """
        if self.model is None or self.frozen_view is None:
            return
        # 冻结列若被「组织列」隐去（或字段已删）→ 一并取消冻结（冻结列必须可见）
        for key in list(self._frozen_keys):
            col = self._column_of_key(key)
            if col is None or self._column_key(col) in self._hidden_field_names:
                self._frozen_keys.discard(key)
        frozen_cols = set(self._frozen_columns())
        self.table_view.setColumnHidden(0, 0 in frozen_cols)
        self.frozen_view.setColumnHidden(0, 0 not in frozen_cols)
        for col, name in enumerate(self.model.field_names, start=1):
            hidden = name in self._hidden_field_names
            self.table_view.setColumnHidden(col, hidden or col in frozen_cols)
            self.frozen_view.setColumnHidden(col, col not in frozen_cols)
        self._update_frozen_geometry()

    # ------------------------------------------------- 冻结 / 解冻列

    def _setup_frozen_view(self) -> None:
        """装配冻结视图：共享 model / selectionModel，接好滚动与列宽同步。"""
        fv = self.frozen_view
        if fv is None or self.model is None:
            return
        fv.setModel(self.model)
        # 两个视图各用一份委托：委托要记住"自己属于哪个视图"（箭头点击后在该
        # 视图里开编辑器 / 判断当前单元格），共用一份会把冻结列的编辑开在主表上
        self._frozen_dict_delegate = _DictValueDelegate(fv, self.model)
        fv.setItemDelegate(self._frozen_dict_delegate)
        sel_model = self.table_view.selectionModel()
        if sel_model is not None:
            fv.setSelectionModel(sel_model)
        # 默认列宽 / 行高与主表一致，换视图不会跳宽跳高
        fv.horizontalHeader().setDefaultSectionSize(
            self.table_view.horizontalHeader().defaultSectionSize()
        )
        fv.verticalHeader().setDefaultSectionSize(
            self.table_view.verticalHeader().defaultSectionSize()
        )
        # 纵向滚动双向同步：两视图 viewport 高度与行高一致 → 精确对齐（见类注释）
        self.table_view.verticalScrollBar().valueChanged.connect(
            fv.verticalScrollBar().setValue
        )
        fv.verticalScrollBar().valueChanged.connect(
            self.table_view.verticalScrollBar().setValue
        )
        # 冻结列被拖宽 / 点表头箭头排序 / 双击定位 / Ctrl+A / 右键菜单，行为与主表一致
        fv.horizontalHeader().sectionResized.connect(lambda *_: self._update_frozen_geometry())
        header = fv.horizontalHeader()
        if hasattr(header, "sortClicked"):
            header.sortClicked.connect(self._on_header_clicked)
        fv.doubleClicked.connect(self._on_row_double_clicked)
        fv.select_all_requested.connect(self._on_select_all_requested)
        fv.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        fv.customContextMenuRequested.connect(self._show_context_menu)
        # 主表每次几何重算（含 Qt 自己重置 viewport 内边距）后，把冻结区重新压回去
        self.table_view.on_resized = self._update_frozen_geometry
        self._sync_column_visibility()

    def _column_key(self, col: int):
        """列号 → 冻结标识（ID 列用哨兵、字段列用字段名；越界返回 ``None``）。"""
        if col == 0:
            return _ID_COLUMN_KEY
        idx = col - 1
        if self.model is not None and 0 <= idx < len(self.model.field_names):
            return self.model.field_names[idx]
        return None

    def _column_of_key(self, key):
        """冻结标识 → 当前列号（字段已不存在时返回 ``None``）。"""
        if key == _ID_COLUMN_KEY:
            return 0
        if self.model is None:
            return None
        try:
            return self.model.field_names.index(key) + 1
        except ValueError:
            return None

    def _frozen_columns(self) -> list[int]:
        """当前冻结列号（升序；字段已删的键自动落空）。"""
        cols = []
        for key in self._frozen_keys:
            col = self._column_of_key(key)
            if col is not None:
                cols.append(col)
        return sorted(cols)

    def _update_frozen_geometry(self) -> None:
        """重排主表左内边距与冻结视图几何（没有任何冻结列时隐藏并还原）。

        冻结视图是主表的子控件，盖住主表左侧（含主表原行号栏）那一带；它自己画
        一条行号栏（▶ 定位标记）+ 冻结列。主表 viewport 左内边距留出与冻结视图
        等宽的空间，两者严丝合缝、不重叠不露缝。
        """
        fv = self.frozen_view
        if fv is None:
            return
        try:
            header = self.table_view.horizontalHeader()
            vheader = self.table_view.verticalHeader()
            vh = vheader.width() if (vheader is not None and vheader.isVisible()) else 0
            hh = header.height() if (header is not None and header.isVisible()) else 0
            cols = self._frozen_columns()
            if not cols:
                fv.hide()
                self.table_view.setViewportMargins(vh, hh, 0, 0)
                return
            fw = vh + sum(fv.columnWidth(c) for c in cols)
            # 先摆好冻结视图（show / raise），**最后**才设主表内边距。setViewportMargins
            # 会同步触发一次 resizeEvent（Qt 在 updateGeometries 里把内边距重置回默认），
            # 那次 resizeEvent 会再进本方法把内边距**重新**压回去——所以**不能**加防重入
            # 闸门挡住它，否则内边距每次都被重置掉。重入自然收敛：第二次设的是同一组值，
            # 不再改变几何、不再触发 resizeEvent。
            fv.setGeometry(1, 1, fw, self.table_view.viewport().height() + hh)
            fv.show()
            fv.raise_()
            self.table_view.setViewportMargins(fw, hh, 0, 0)
        except RuntimeError:
            pass  # 收尾阶段 C++ 对象已销毁

    def _freeze_column(self, key) -> None:
        """冻结某一列：先把它当前的列宽带到冻结视图（视觉不跳），再隐藏到冻结区。"""
        col = self._column_of_key(key)
        if col is None or self.frozen_view is None:
            return
        self.frozen_view.setColumnWidth(col, self.table_view.columnWidth(col))
        self._frozen_keys.add(key)
        self._sync_column_visibility()

    def _unfreeze_column(self, key) -> None:
        """解冻某一列：列宽带回主表后重新显示。"""
        if key not in self._frozen_keys:
            return
        self._frozen_keys.discard(key)
        col = self._column_of_key(key)
        if col is not None and self.frozen_view is not None:
            self.table_view.setColumnWidth(col, self.frozen_view.columnWidth(col))
        self._sync_column_visibility()

    def _unfreeze_all(self) -> None:
        """取消全部冻结。"""
        if not self._frozen_keys:
            return
        widths = {c: self.frozen_view.columnWidth(c) for c in self._frozen_columns()}
        self._frozen_keys.clear()
        for col, width in widths.items():
            self.table_view.setColumnWidth(col, width)
        self._sync_column_visibility()

    def _install_header_menu(self, header) -> None:
        """给列头装右键菜单（事件过滤器方式）。

        Qt6 里表头右键事件的落点在不同平台/控件层级可能是 ``header`` 本身，也可能是
        它的 ``viewport()``；而 ``setContextMenuPolicy`` 只有在事件**真正落到设了
        policy 的那个 widget** 时才发 ``customContextMenuRequested``，设错一层就
        "右键表头没反应"（用户报过）。这里干脆在两层 widget 上都装事件过滤器，谁先
        收到 ``ContextMenu`` 谁弹菜单（装了过滤器后不会再走 policy 那条路）。
        """
        self._header_event_targets[header] = header
        self._header_event_targets[header.viewport()] = header
        header.installEventFilter(self)
        header.viewport().installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 —— Qt 命名
        """拦列表头（或其 viewport）的右键事件 → 弹出冻结/解冻菜单。"""
        header = self._header_event_targets.get(obj)
        if header is not None and event.type() == QEvent.Type.ContextMenu:
            try:
                try:
                    global_pos = event.globalPos()
                except (AttributeError, TypeError):
                    global_pos = event.globalPosition().toPoint()
                pos = header.viewport().mapFromGlobal(global_pos)
                self._show_header_menu(header, pos)
            except Exception:  # noqa: BLE001 —— Qt 虚函数里未捕获异常是原生崩溃
                logger.debug("列头右键菜单失败", exc_info=True)
            return True
        return super().eventFilter(obj, event)

    def _show_header_menu(self, header, position) -> None:
        """列头右键：冻结 / 解冻该列；字段列还可删除字段 / 打开字段计算器。"""
        from qgis.PyQt.QtGui import QAction
        from qgis.PyQt.QtWidgets import QMenu

        col = header.logicalIndexAt(position)
        if col < 0:
            return
        key = self._column_key(col)
        if key is None:
            return
        menu = QMenu(self)
        # 排序（ID 列与字段列都可排）
        act_asc = QAction("升序", self)
        act_asc.triggered.connect(lambda: self._sort_by_column(col, True))
        menu.addAction(act_asc)
        act_desc = QAction("降序", self)
        act_desc.triggered.connect(lambda: self._sort_by_column(col, False))
        menu.addAction(act_desc)
        menu.addSeparator()
        if key in self._frozen_keys:
            act = QAction("取消冻结列", self)
            act.triggered.connect(lambda: self._unfreeze_column(key))
        else:
            act = QAction("冻结列", self)
            act.triggered.connect(lambda: self._freeze_column(key))
        menu.addAction(act)
        if self._frozen_keys:
            menu.addSeparator()
            act_all = QAction("取消全部冻结", self)
            act_all.triggered.connect(self._unfreeze_all)
            menu.addAction(act_all)
        # ID 列没有对应字段：删除字段 / 字段计算器只对字段列开放
        if key != _ID_COLUMN_KEY:
            menu.addSeparator()
            readonly = bool(self.model is not None and col in self.model.readonly_cols)
            editing = bool(self.layer is not None and self.layer.isEditable())
            act_del = QAction("删除字段", self)
            act_del.triggered.connect(lambda: self._delete_field(key))
            if readonly or editing:
                # 只读字段 / 编辑状态下都不允许删除字段（置灰并给出原因）
                act_del.setEnabled(False)
                if editing:
                    act_del.setToolTip("图层处于编辑状态，请先保存 / 停止编辑后再删除字段")
                else:
                    act_del.setToolTip("系统自维护 / 唯一标识字段，不允许删除")
            menu.addAction(act_del)
            act_calc = QAction("字段计算器", self)
            act_calc.triggered.connect(lambda: self._open_field_calculator(key))
            if readonly:
                act_calc.setEnabled(False)
                act_calc.setToolTip("系统自维护 / 唯一标识字段（ID / OBJECTID / GUID 等），不允许计算")
            menu.addAction(act_calc)
            # 几何计算：把周长 / 长度 / 面积写进**该列**数值字段。
            # 点图层没有可算的；系统自维护字段、非数值字段也不允许写。
            act_geom = QAction("几何计算", self)
            field_index = self.layer.fields().indexOf(key)
            field = (
                self.layer.fields().at(field_index)
                if field_index >= 0
                else None
            )
            numeric = field is not None and field.type() in (
                QVariant.Int,
                QVariant.LongLong,
                QVariant.UInt,
                QVariant.ULongLong,
                QVariant.Double,
            )
            geometric = self.layer is not None and self.layer.geometryType() in (
                QgsWkbTypes.PolygonGeometry,
                QgsWkbTypes.LineGeometry,
            )
            geom_ok = geometric and numeric and not readonly
            act_geom.setEnabled(geom_ok)
            if readonly:
                act_geom.setToolTip("系统自维护 / 唯一标识字段，不允许写入几何计算结果")
            elif not geometric:
                act_geom.setToolTip("点图层没有长度 / 面积可计算")
            elif not numeric:
                act_geom.setToolTip("几何计算只能写入数值字段")
            act_geom.triggered.connect(lambda: self._open_geometry_calculator(key))
            menu.addAction(act_geom)
        menu.setToolTipsVisible(True)
        menu.exec(header.viewport().mapToGlobal(position))

    def _delete_field(self, name: str) -> None:
        """列头菜单「删除字段」：确认后从图层删字段（与图层属性「字段」页同一逻辑）。"""
        from app.ui.dialogs.layer_properties.fields import (
            _delete_field_from_layer,
            is_geojson_layer,
        )

        if self.layer is None or not name:
            return
        # 系统自维护 / 唯一标识字段只读：不允许删除（菜单项已置灰，这里再兜一层）
        col = self._column_of_key(name)
        if col is not None and self.model is not None and col in self.model.readonly_cols:
            QMessageBox.information(
                self, "删除字段",
                f"字段「{name}」为系统自维护 / 唯一标识字段，不允许删除。",
            )
            return
        # 编辑状态不允许删除字段（先保存/停止编辑再改 schema）
        if self.layer.isEditable():
            QMessageBox.information(
                self, "删除字段",
                "图层处于编辑状态，请先保存 / 停止编辑后再删除字段。",
            )
            return
        if is_geojson_layer(self.layer):
            answer = QMessageBox.warning(
                self,
                "删除字段",
                f"GeoJSON 是文本交换格式，删除字段会重写整个文件，"
                f"大图层可能长时间无响应。\n\n建议先将图层另存为 GeoPackage 再编辑。\n\n"
                f"仍要删除字段「{name}」吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        else:
            answer = QMessageBox.question(
                self, "删除字段", f"确定从图层中删除字段「{name}」？该操作不可撤销。"
            )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            ok = _delete_field_from_layer(self.layer, name)
        except Exception as exc:  # noqa: BLE001
            logger.error("删除字段失败: %s", exc, exc_info=True)
            QMessageBox.warning(self, "删除字段", f"删除字段失败：{exc}")
            return
        if not ok:
            QMessageBox.warning(
                self, "删除字段",
                f"删除字段「{name}」失败（provider 不支持或图层只读）",
            )
            return
        # 字段没了：从冻结集合里摘掉，并让模型重读列定义（updatedFields 信号多半也会触发，
        # 这里显式再刷一次，保证面板立即跟上）。
        self._frozen_keys.discard(name)
        if self.model is not None:
            self.model.reload_fields()

    def _open_field_calculator(self, name: str) -> None:
        """列头菜单「字段计算器」：打开并默认切到「更新已有字段」= 该列字段。"""
        from app.ui.dialogs.field_calculator import FieldCalculatorDialog

        index = self.layer.fields().indexOf(name)
        if index in readonly_field_indices(self.layer.fields()):
            QMessageBox.information(
                self,
                "字段计算器",
                f"字段「{name}」为系统自维护 / 唯一标识字段，不允许计算。",
            )
            return
        dlg = FieldCalculatorDialog(self.layer, self, initial_field=name)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        # 计算器可能启动了编辑 / 改了字段：同步按钮并刷新列与值
        self._sync_edit_buttons()
        if self.model is not None:
            self.model.reload_fields()

    def _open_geometry_calculator(self, name: str) -> None:
        """列头菜单「几何计算」：把周长 / 长度 / 面积写进**该列字段**。

        非模态：算完可以继续在表里滚动 / 改选中要素，再回来重新计算。
        目标字段固定为右键的那一列（菜单里已保证它是可写数值字段）。
        """
        from app.ui.dialogs.geometry_calculator import GeometryCalculatorDialog

        existing = getattr(self, "_geometry_dialog", None)
        if (
            existing is not None
            and not sip.isdeleted(existing)
            and existing.isVisible()
        ):
            existing.raise_()
            existing.activateWindow()
            return
        dlg = GeometryCalculatorDialog(self.layer, self, field_name=name)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        def _refresh() -> None:
            # 对话框可能启动了编辑 / 批量改值：同步按钮并让可视行重取新值
            self._sync_edit_buttons()
            if self.model is not None:
                self.model.reload_fields()

        dlg.accepted.connect(_refresh)
        dlg.destroyed.connect(lambda *_: setattr(self, "_geometry_dialog", None))
        self._geometry_dialog = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _show_context_menu(self, position) -> None:
        from qgis.PyQt.QtGui import QAction
        from qgis.PyQt.QtWidgets import QMenu
        if not self.model:
            return
        # 主表 / 冻结视图共用本方法：按信号发出者取正确的视图与坐标系
        view = self.sender()
        if not isinstance(view, QAbstractItemView):
            view = self.table_view
        idx = view.indexAt(position)
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
        menu.exec(view.viewport().mapToGlobal(position))


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
        # 图层被删（任何入口：面板删除 / 分组删除 / 算法替换 / 清空工程）时，
        # **抢在其 C++ 对象销毁前**关闭本 Dock。否则 Dock 仍持有已删图层，视图
        # 重绘会经 model.flags/data 访问它 → RuntimeError: wrapped C/C++ object
        # of type QgsVectorLayer has been deleted。
        QgsProject.instance().layersWillBeRemoved.connect(self._on_layers_will_be_removed)
        self.content_widget = PagedAttributeTableWidget(layer, page_size, self, filter_str=filter_str, show_ids=show_ids)
        self.setWidget(self.content_widget)
        self._wire_content_widget(self.content_widget)
        #: 双击行时在选中要素上闪几下的件（懒建，见 _ensure_flasher）
        self._highlighter = None
        #: 正在做"选中集同步"期间为 True：把 表格→图层→表格 的回环掐掉
        self._syncing = False
        #: 当前挂了 selectionChanged 的图层（换图层 / 关闭时要摘掉）
        self._selection_layer = None
        self._attach_selection_sync()
        self.setMinimumSize(200, 100)

    def _wire_content_widget(self, widget) -> None:
        """把内容控件的信号接到 Dock 上。

        **换图层（``add_new_layer``）会新建一个 widget，接线必须走这里**——漏接哪个信号
        就会出"第一次好用、重开就没反应"那种 bug（右键的「缩放到要素 / 选中要素」当初
        就是因为漏了 ``zoom_to_map`` 而失效）。
        """
        widget.zoom_to_map.connect(self.zoom_to_map)
        widget.selection_changed.connect(self._on_table_selection_changed)
        widget.feature_activated.connect(self._activate_feature)
        widget.zoom_to_selection_requested.connect(self._zoom_to_selection)

    def add_new_layer(self, layer, parent, filter_str: str, show_ids, page_size: int = 50, title: str = "属性表") -> None:
        # 先取消旧 widget 的后台流式线程（避免滞留持有图层读锁）
        if self.content_widget is not None and self.content_widget.model is not None:
            self.content_widget.model.cancel_stream()
        self.layer = layer
        self.show_ids = list(show_ids) if show_ids else []
        self.content_widget = PagedAttributeTableWidget(layer, page_size, self, filter_str=filter_str, show_ids=show_ids)
        self.setWidget(self.content_widget)
        self._wire_content_widget(self.content_widget)
        # 闪烁件绑定的是**旧图层**，换了图层必须丢掉重建；选中同步也要改挂到新图层
        self._discard_highlighter()
        self._attach_selection_sync()
        self.setWindowTitle(title)

    def _on_layers_will_be_removed(self, layer_ids) -> None:
        """本 Dock 正显示的图层即将被删 → 立即关闭（见 ``__init__`` 注释）。

        关掉后视图不再重绘；``closeEvent`` 会取消后台流式并清子集，避免后续
        访问已删的 ``QgsVectorLayer``。
        """
        if self.layer is None or not layer_ids:
            return
        try:
            lid = self.layer.id()
        except RuntimeError:
            return  # 已是悬空对象，无需处理
        if lid in set(layer_ids):
            self.close()

    def closeEvent(self, event) -> None:
        try:
            if self.content_widget is not None and self.content_widget.model is not None:
                self.content_widget.model.cancel_stream()
                try:
                    self.layer.subsetStringChanged.disconnect(
                        self.content_widget.model._on_subset_string_changed
                    )
                except (TypeError, RuntimeError):
                    pass
            if self.layer.subsetString():
                self.layer.setSubsetString("")
            if self.layer.isEditable():
                self.content_widget._stop_edit()
            # 高亮带挂在画布上，关了 Dock 得把它收掉，否则残留一个高亮框在地图上
            self._discard_highlighter()
            self._detach_selection_sync()
            if self.content_widget is not None:
                self.content_widget._abort_export()   # 没写完的导出收掉，别留半截文件
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

    # ------------------------------------------------------------ 选中集同步

    def _on_table_selection_changed(self, fids) -> None:
        """表格里改选中行 → 改图层选中集（替换式）。

        "属性表里选中的数据"与"地图上选中的数据"是**同一份**（``selectByIds``），
        所以选择工具、属性表、各种按选中集工作的功能全都一致；画布上的高亮由选中
        高亮覆盖层按「图层属性 → 选择」页配的样式画出来。
        """
        if self._syncing or self.layer is None:
            return
        self._syncing = True
        try:
            self.layer.selectByIds([int(f) for f in fids])
            self._refresh_canvas()   # selectByIds 不会自动重绘画布
        except Exception:  # noqa: BLE001 —— 槽里未捕获异常是原生崩溃
            logger.warning("按表格选中集改图层选中失败", exc_info=True)
        finally:
            self._syncing = False

    def _on_layer_selection_changed(self, *_args) -> None:
        """图层选中集变了（地图上选 / 工具选 / 程序选）→ 属性表自动选中对应行。"""
        if self._syncing or self.layer is None or self.content_widget is None:
            return
        self._syncing = True
        try:
            self.content_widget.apply_layer_selection(self.layer.selectedFeatureIds())
        except Exception:  # noqa: BLE001
            logger.warning("按图层选中集同步表格失败", exc_info=True)
        finally:
            self._syncing = False

    def _attach_selection_sync(self) -> None:
        """把选中同步挂到当前图层（换图层前先摘掉旧的）。"""
        self._detach_selection_sync()
        layer = self.layer
        if layer is None:
            return
        try:
            layer.selectionChanged.connect(self._on_layer_selection_changed)
            self._selection_layer = layer
        except Exception:  # noqa: BLE001
            logger.warning("挂接图层选中同步失败", exc_info=True)
            return
        # 打开表格时图层就已经有选中集：立刻在表里标出来（没加载到的行会临时补插，
        # 上限见 EXTRA_SELECT_LIMIT）。不然要等下一次选中变化才看得到。
        try:
            if layer.selectedFeatureIds():
                self._on_layer_selection_changed()
        except RuntimeError:
            pass  # 图层已销毁

    def _detach_selection_sync(self) -> None:
        layer, self._selection_layer = self._selection_layer, None
        if layer is None:
            return
        try:
            layer.selectionChanged.disconnect(self._on_layer_selection_changed)
        except Exception:  # noqa: BLE001 —— 收尾 / 图层已销毁属正常
            pass

    # ------------------------------------------------------------ 闪烁

    # ------------------------------------------------------------ 缩放到所选

    def _zoom_to_selection(self) -> None:
        """把地图缩放到本图层**选中要素**的外接矩形。

        用 ``boundingBoxOfSelected()``（QGIS 自己「缩放到选中」用的也是它）。选中要素
        太多时逐个读几何会很慢，所以超过阈值就退回按图层范围缩放、并说明一句。
        """
        if self.layer is None or self.content_widget is None or self.topParent is None:
            return
        try:
            fids = self.layer.selectedFeatureIds()
        except RuntimeError:
            return
        if not fids:
            self._notify("还没有选中要素，先选几条再缩放", warn=True)
            return
        huge = len(fids) > _ZOOM_SELECTION_MAX
        try:
            bbox = self.layer.extent() if huge else self.layer.boundingBoxOfSelected()
        except RuntimeError:
            return
        if bbox is None or bbox.isNull():
            self._notify("选中的要素没有有效范围", warn=True)
            return
        if huge:
            self._notify(f"选中 {len(fids)} 条，按图层范围缩放")
        extent = self._zoom_extent_for(bbox)     # 退化框（共线/单点）在这里被撑开
        zoomer = getattr(self.topParent, "zoom_to_layer_extent_transform", None)
        if callable(zoomer):
            zoomer(extent, self.layer.crs())
        else:
            canvas = getattr(self.topParent, "canvas", None)
            if canvas is not None:
                canvas.setExtent(extent)
                canvas.refresh()

    def _notify(self, text: str, *, warn: bool = False) -> None:
        """给用户一句反馈（转发给内容控件的消息条入口，Dock 自己不弹窗）。"""
        widget = self.content_widget
        if widget is not None:
            widget._notify(text, warn=warn)

    def _activate_feature(self, fid: int) -> None:
        """双击单元格 → **定位 + 闪烁**（选中本身已由选中集同步做完）。

        定位沿用「坐标定位」那套语义：**保留当前缩放、只把中心挪过去**，不会突然
        放大到某个图斑。要素**已经在当前视野里**就不动地图——不然双击一下就把视图
        拽走，很讨厌。
        """
        self._locate_feature(fid)
        self._flash_feature(fid)

    def _zoom_extent_for(self, bbox) -> QgsRectangle:
        """把包围盒撑成"适合缩放的框"。

        - 正常的框：四周留 :data:`_ZOOM_PADDING` 的边距，图形不至于顶到屏幕边；
        - **退化**的框（单点是零宽零高、共线是零高或零宽）不是"没有范围"，而是范围退化了
          ——QGIS 的 ``setExtent`` 会把退化矩形当空处理（等于不动），所以按**画布宽高比**
          补齐缺的那一维；补出来的框正好贴合画布，缩放才紧、才像"缩到这块图形"。
        """
        canvas = getattr(self.topParent, "canvas", None)
        size = canvas.size() if canvas is not None else None
        aspect = (size.width() / size.height()) if size is not None and size.height() else 1.0
        w, h = bbox.width(), bbox.height()
        if w <= 0 and h <= 0:
            # 单点：按图层范围的 1/50 给个跨度（同 CRS，量级合理；图层本身也退化就退回 1）
            ext = self.layer.extent() if self.layer is not None else None
            base = max(ext.width(), ext.height()) / 50.0 if (ext is not None and not ext.isNull()) else 1.0
            if base <= 0:
                base = 1.0
            h = base
            w = base * aspect
        elif w <= 0:
            w = h * aspect
        elif h <= 0:
            h = w / aspect
        else:
            w *= _ZOOM_PADDING
            h *= _ZOOM_PADDING
        c = bbox.center()
        return QgsRectangle(c.x() - w / 2.0, c.y() - h / 2.0, c.x() + w / 2.0, c.y() + h / 2.0)

    def _feature_by_fid(self, fid: int) -> Optional[QgsFeature]:
        """按 fid 取要素：优先用属性表模型的 LRU 缓存。

        GeoJSON 等无空间索引的 OGR 数据源按 fid 取要素可能从头扫描整个文件；
        属性表渲染已经缓存了可视窗口的要素，双击定位 / 闪烁直接复用，避免第二次扫描。
        """
        content = getattr(self, "content_widget", None)
        model = getattr(content, "model", None) if content is not None else None
        getter = getattr(model, "feature_by_fid", None) if model is not None else None
        if callable(getter):
            try:
                feature = getter(int(fid))
                if feature is not None:
                    return feature
            except Exception:
                logger.debug("从属性表缓存取要素失败: fid=%s", fid, exc_info=True)
        if self.layer is None:
            return None
        try:
            feature = self.layer.getFeature(int(fid))
        except Exception:
            logger.debug("从图层取要素失败: fid=%s", fid, exc_info=True)
            return None
        if feature is None or not feature.isValid():
            return None
        return feature

    def _locate_feature(self, fid: int) -> None:
        """把地图**缩放到**这个要素（属性表双击单元格用）。

        以前是"没在视野里才平移过去"；但图斑可能很小、就算已经在视野里也看不出闪的是
        哪一块（用户报过），所以改成**一律缩放到它**，四周留点边距。
        """
        if self.topParent is None or self.layer is None:
            return
        canvas = getattr(self.topParent, "canvas", None)
        if canvas is None:
            return
        try:
            feature = self._feature_by_fid(fid)
            geom = feature.geometry() if feature is not None else None
            if geom is None or geom.isNull() or geom.isEmpty():
                return
            extent = self._zoom_extent_for(geom.boundingBox())
        except Exception:  # noqa: BLE001 —— 槽里未捕获异常是原生崩溃
            logger.warning("定位到要素失败: fid=%s", fid, exc_info=True)
            return
        zoomer = getattr(self.topParent, "zoom_to_layer_extent_transform", None)
        if callable(zoomer):
            zoomer(extent, self.layer.crs())     # CRS 变换交给它（含脏几何回退）
        else:
            canvas.setExtent(extent)
            canvas.refresh()

    def _flash_feature(self, fid: int) -> None:
        """双击行 → 在它上面闪几下。

        用 :class:`FeatureHighlighter` 的**洋红**，与选中高亮区分得开——同色的话在
        自己已经高亮的要素上闪烁根本看不出来。
        """
        if self.topParent is None or self.layer is None:
            return
        canvas = getattr(self.topParent, "canvas", None)
        if canvas is None:
            return
        try:
            feature = self._feature_by_fid(fid)
            if feature is None:
                return
            flasher = self._ensure_flasher(canvas)
            if flasher is not None:
                flasher.flash(fid, feature=feature)
        except Exception:  # noqa: BLE001
            logger.warning("闪烁要素失败: fid=%s", fid, exc_info=True)

    def _ensure_flasher(self, canvas):
        """懒建闪烁件（绑定当前图层，所以换图层时要丢掉重建）。"""
        if self._highlighter is None:
            self._highlighter = FeatureHighlighter(canvas, self.layer, parent=self)
            # **闪完就收掉**：属性表双击只是"帮你在一堆图形里找到它"，要素本身已经进了
            # 选中集、会用选中样式画出来；再留一个粉红框挂着纯属碍事（用户报过）。
            # FeatureHighlighter 默认"闪完保持可见"是给"补缝/合并"那种要对照挑面的场景的。
            self._highlighter.finished.connect(self._highlighter.clear)
        return self._highlighter

    def _discard_highlighter(self) -> None:
        """丢掉闪烁件（换图层 / 关 Dock 时调），顺手把画布上的残留清掉。"""
        highlighter, self._highlighter = self._highlighter, None
        if highlighter is not None:
            try:
                highlighter.close()
            except Exception:  # noqa: BLE001
                pass
