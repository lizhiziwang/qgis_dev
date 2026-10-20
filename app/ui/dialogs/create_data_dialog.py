"""创建数据 Dialog（数据管理 Dock「创建数据」入口）。

**「单图层数据」页签是个两步向导**（这一步照原型 ``code01/app.js`` 的「创建数据向导」
来的），「本地数据库」页签不走向导：

- **第 1 步 · 基础参数**：目标（新建独立文件 / 写入已有本地数据库）+ 创建路径 /
  数据文件名称 / 数据格式 / 图层名 / 几何类型 / 坐标系。页脚 ``[取消] [上一步(禁用)] [下一步]``。
- **第 2 步 · 数据字段设置**：字段结构表（见
  :class:`~app.ui.dialogs.create_data_fields_step.FieldsStepPage`）。
  页脚 ``[取消] [上一步] [创建]``。

``上一步`` 回退时字段表原样留着（不清）；切到「本地数据库」页再切回来也不回退步骤。
**第 1 步敲回车绝不会直接创建** —— 默认按钮跟着步骤走（第 1 步是「下一步」、
第 2 步是「创建」、容器页才是「确定」）。

**两个页签按数据形态分流**（这是本对话框的核心结构，别把两类混回一个表单）。
顺序是「单图层数据」在前、「本地数据库」在后 —— 前者是日常动作，后者低频，
且「目标库 → ＋新增数据库…」会跳到最后那一页：

2. **单图层数据** —— 一次建一个矢量图层，**默认目标就是「写入已有本地数据库」**
   （日常用法是把新图层补进已建好的库里），也可以切到「新建独立文件」：

   a. **新建独立文件**（shp / GeoJSON / KML / GML / DXF / CSV / FlatGeobuf / GPX /
      MapInfo TAB）：问「创建路径 + 数据文件名称 + 数据格式 + 图层名 + 几何类型 +
      坐标系」，一个文件一层；
   b. **写入已有本地数据库**：把新图层作为一张表建进已登记的 gpkg / SpatiaLite 库，
      问「目标库 + 图层名 + 几何类型 + 坐标系」。目标库下拉里是「数据管理」里已登记的
      本地库，末尾还有两个动作项：「浏览…」选库外的文件（非本工程建的库，登记见调用方）、
      「＋新增数据库…」跳到「本地数据库」页签去建库（**建完的库会自动补进这个下拉并选中**，
      所以"建库 → 写图层"可以一口气做完）。

1. **本地数据库** —— 多图层容器（``GeoPackage`` / ``SQLite``+SpatiaLite，一个文件
   装多个图层）。这里**只建空库**：不建图层、没有几何类型与坐标系可言，所以这一页
   只有「创建路径 + 数据库文件名 + 数据格式」三行。建完由调用方把库登记进
   「数据管理」Dock（见 ``AppActionsMixin._on_create_data``）。

**FileGDB 两边都不提供**（两个页签的格式下拉里都没有它）。它语义上也是容器库，但

- 进不了「本地数据库」页：``describe_local_db`` 只认 sqlite 家族，``.gdb`` 是目录、
  引擎是 OGR，登记不进「数据管理」；
- 也不该进「单图层数据」页：那是"一个文件一层"的语义，GDB 是库不是文件。

GDB 的新建图层入口在**数据浏览器对 ``.gdb`` 目录右键「新增图层」**
（``file_browser._on_browser_add_gdb_layer``，用本机真正可写的 ``OpenFileGDB`` 驱动）。
``VECTOR_FORMATS`` 里的 FileGDB 条目仍然保留：它的
``container=True`` 是 ``_CONTAINER_DRIVERS`` 的来源（``|layername=`` 回读要用），
``alt_drivers`` 让本机解析到可写驱动 —— 那条路照旧可用，只是不在本对话框露面。

**「数据格式」与「数据文件名称」的分工**（仅页签二模式 a，两者都会决定扩展名，规则要唯一）：

- **格式下拉是权威**：扩展名由它补/换 —— 不填扩展名照样能创建；
- **认得出的扩展名反向切格式**：文件名写 ``地块.shp`` 会把下拉切成 Shapefile
  （见 :meth:`CreateDataDialog._sync_format_from_name`），两个控件不会互相打脸；
- 认不出的扩展名（如 ``.txt``）不报错，按格式下拉的扩展名收口，底部提示会实时回显。

**职责边界**：本对话框只做「收集 + 校验 + 取值 + 触发创建」——「创建」/「确定」/「应用」
都先过 :meth:`CreateDataDialog._validate`（第 1 步的参数校验 + 第 2 步的字段校验串联），
通过后交给 :class:`~app.core.domain.create_data.CreateDataService`（建图层文件）或
:class:`~app.core.domain.create_data.CreateContainerService` /
:class:`~app.core.domain.create_data.CreateLayerInDbService`（建空库 / 往库里建图层）
落盘；服务失败就把原因留在窗口里、不关窗。建出来的东西经
:meth:`CreateDataDialog.outcome`（「确定」/「创建」）或 :attr:`CreateDataDialog.applied` 信号
（「应用」，窗口不关）交给调用方，**本对话框自己不往工程里加图层、也不碰数据管理
Dock**（UI 不写 IO / 不管编排，见编码规范 §10）。对话框同样不读配置文件：默认坐标系与
「已登记的本地库清单」都由调用方注入（``default_crs`` / ``local_dbs``）。

**建完之后的收尾**：字段结构里「数据源存不下」的那部分（别名 / 不可为空 / 隐藏 / 字典）
由 :func:`~app.core.domain.create_data.configure_created_layer` 落在**返回给调用方的那个
图层对象**上（这是工程状态，创建服务不碰），没落下的部分在这里**如实提示**（见
:meth:`CreateDataDialog._report_field_persistence`）—— 界面不许假装落盘成功。

**页签二模式 a：目标已存在一律拦下**（不覆盖、也不往既有数据源里追加图层）：覆盖会
毁掉既有数据，追加则属于「改既有数据源」而不是「创建」，都由创建服务在写盘前拦下。
**模式 b 是这条之外的另一条路**：它的语义就是「往库里加一张表」，服务只建这一张表、
绝不碰库里别的图层（走 ``local_db_util.create_vector_table``，不用 OGR 的容器写盘——
后者默认会整库删掉重建，见 ``app/core/domain/create_data/library.py`` 的模块头）。
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from app.core.domain.create_data import (
    CreateContainerService,
    CreateContainerSpec,
    CreateDataError,
    CreateDataService,
    CreateDataSpec,
    CreateLayerInDbService,
    CreateLayerInDbSpec,
    CreateLibraryError,
    FieldSpec,
    available_drivers,
    configure_created_layer,
    validate_fields,
    with_mzguid,
)
from app.core.domain.create_data.dict_tables import (
    DictPlan,
    DictTableError,
    build_dict_table_specs,
    local_dict_categories,
    persist_dict_tables,
    resolve_local_table,
)
from app.core.domain.dict_manage import DictService
from app.core.util.field_meta import DICT_LOCAL_TABLE_PROP
from app.core.util.local_db_util import local_db_util
from app.core.util.srs import EPSG_SEP, epsg_entries
from app.core.util.vector_formats import (
    VectorFormat,
    format_for_extension,
    formats_by_kind,
    resolve_format,
)
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.theme import ThemeManager
from app.ui.common.widgets import open_file_dir
from app.ui.dialogs.create_data_fields_step import FieldsStepPage
from app.ui.dialogs.crs_pick_dialog import CrsPickDialog
from qgis.core import QgsCoordinateReferenceSystem, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QCompleter,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

logger = get_logger(__name__)

#: 几何类型下拉项：中文名 → QGIS 几何类型名（与 :class:`QgsWkbTypes` 枚举名一致）
_GEOMETRY_TYPES: tuple[tuple[str, str], ...] = (
    ("点", "Point"),
    ("线", "LineString"),
    ("面", "Polygon"),
    ("多点", "MultiPoint"),
    ("多线", "MultiLineString"),
    ("多面", "MultiPolygon"),
)

#: 文件名称 / 图层名 / 库文件名里不允许出现的字符（Windows 文件名与 gpkg 表名的共同约束）
_ILLEGAL_NAME_CHARS = '\\/:*?"<>|'

#: ``authid`` 形态校验（``EPSG:4490`` / ``ESRI:102100`` …）
_AUTHID_RE = re.compile(r"[A-Za-z]+:\d+")

#: 调用方没注入默认坐标系时的兜底值（CGCS2000 / 中国 2000）。正常路径下默认值
#: 来自配置：``config/app.yaml`` 的 ``app.crs``，由 :class:`MainWindow` 注入 ——
#: 本对话框不自己读配置文件（UI 不写 IO，见模块头「职责边界」）。
_CRS_FALLBACK = "EPSG:4490"

#: 「浏览…」选库文件时的过滤器（与数据管理的「新建连接」同一份）
_DB_FILE_FILTER = "SQLite / GeoPackage (*.gpkg *.sqlite *.sqlite3 *.db);;所有文件 (*)"

#: 目标库下拉里两个「不是某个库」的哨兵项（挂在 ``itemData`` 上；真实库项挂的是连接 dict）
_TARGET_BROWSE = "browse"
_TARGET_NEW = "new"

#: 页签一建得出的容器格式的驱动名。**页签一只列能登记进数据管理的库**：FileGDB 虽然是
#: 容器（``VectorFormat.container`` 为真），但``describe_local_db`` 只认 sqlite 家族
#: （``.gdb`` 是目录、引擎是 OGR），登记不了 —— 见模块头。
_CONTAINER_DB_DRIVERS: tuple[str, ...] = ("GPKG", "SQLite")

#: 页签一「本地数据库」的格式下拉
_CONTAINER_FORMATS: tuple[VectorFormat, ...] = tuple(
    fmt for fmt in formats_by_kind(container=True) if fmt.driver in _CONTAINER_DB_DRIVERS
)

#: 页签二「单图层数据」的格式下拉：**只列真正一个文件一层的格式**。
#: 容器库（GeoPackage / SQLite / FileGDB）一律不出现 —— 前两个在页签一，
#: FileGDB 目前两边都不提供（它进不了数据管理、放进这一页又与其语义不符，
#: 创建入口收敛到文件浏览器的右键「新建」）。
_FILE_FORMATS: tuple[VectorFormat, ...] = formats_by_kind(container=False)

# 坐标系候选（含兜底表）在 :mod:`app.core.util.srs` 里：那边的「坐标系选择」弹窗
# 也要用同一份，读 srs.db 的活计不该在 UI 里各写一遍。


def _epsg_entries() -> list[tuple[str, str]]:
    """坐标系候选表（``srs.db`` / 兜底表都在 :mod:`app.core.util.srs` 里）。"""
    return epsg_entries()


def _illegal_chars(name: str) -> str:
    """返回名字里出现的非法字符（去重、按 :data:`_ILLEGAL_NAME_CHARS` 顺序），便于提示。"""
    return "".join(dict.fromkeys(ch for ch in name if ch in _ILLEGAL_NAME_CHARS))


def default_output_dir() -> str:
    """创建路径的默认值：工程已存盘就用工程所在目录，否则用当前用户主目录。

    **不能直接拿 ``QgsProject.instance().homePath()``**：工程还没存过盘时它是空串，
    用户每建一次数据都得手点一遍「浏览…」（用户报的就是这个）。回退到
    :func:`pathlib.Path.home`，它按平台取 ``%USERPROFILE%`` / ``$HOME``，
    Windows 与 Linux / 麒麟都成立。

    全程只用 :mod:`pathlib` / ``os.path`` 拼路径，**不写死分隔符、不写死盘符**；
    最终写进 spec 前还会过一遍 ``os.path.normpath``。
    """
    project_dir = QgsProject.instance().homePath()
    if project_dir and os.path.isdir(project_dir):
        return project_dir
    return str(Path.home())


class CrsComboBox(QComboBox):
    """支持 EPSG 代码 / 名称搜索的坐标系下拉框。

    候选项显示为 ``EPSG:4490 — China Geodetic Coordinate System 2000``，
    所以输入 ``4490``、``epsg:4490``、``cgcs2000`` 都能过滤到目标项
    （``MatchContains`` + 忽略大小写，与 ``copy_to_layer`` 的字段过滤一致）；
    也允许直接手输候选表里没有的 authid —— :meth:`crs` 自己解析校验。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        # 不插入：允许「输入的内容不在候选里」而不污染候选表
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.addItems([f"{authid}{EPSG_SEP}{name}" for authid, name in _epsg_entries()])
        line_edit = self.lineEdit()
        line_edit.setPlaceholderText("输入 EPSG 代码或名称搜索，如 4490 / CGCS2000")
        line_edit.setClearButtonEnabled(True)
        # 候选一次性灌满（约 7200 项，读一次 srs.db + addItems 几十毫秒）：输入即
        # 过滤是「EPSG 代码搜索」的主用法，懒加载会让首次输入不出候选
        completer = QCompleter(self.model(), self)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setMaxVisibleItems(15)
        self.setCompleter(completer)
        #: 展开列表按最长候选算出来的宽度（算一次就够，字体 / 主题变了重建控件即可）
        self._popup_width = 0
        # 收起时字段放不下的长名称，至少能在悬停提示里看全
        self.currentTextChanged.connect(lambda text: self.setToolTip(text.strip()))

    def showPopup(self) -> None:
        """展开候选列表时按最长名称加宽，避免长坐标系名被截断。"""
        view = self.view()
        if self._popup_width <= 0:
            width = view.sizeHintForColumn(0)
            if width > 0:
                width += view.verticalScrollBar().sizeHint().width() + 16
                # 按最长名称算出的宽度再收 30%，长名称交给悬停提示兜底
                self._popup_width = max(int(width * 0.7), self.width())
        if self._popup_width > 0:
            view.setMinimumWidth(self._popup_width)
        super().showPopup()

    def set_crs(self, authid: str) -> None:
        """把当前选中项置为 ``authid``；候选里没有时直接写进文本框。"""
        if not authid:
            return
        index = self.findText(f"{authid}{EPSG_SEP}", Qt.MatchFlag.MatchStartsWith)
        if index >= 0:
            self.setCurrentIndex(index)
        else:
            self.setEditText(authid)

    def authid(self) -> str:
        """把当前输入解析成 authid（如 ``EPSG:4490``）；解析不出返回空串。"""
        text = self.currentText().strip()
        if not text:
            return ""
        # 下拉项是「authid — 名称」，去掉名称段后只留 authid
        head = text.split(EPSG_SEP, 1)[0].strip()
        if _AUTHID_RE.fullmatch(head):
            return head.upper()
        # 纯数字按 EPSG 代码理解（用户最常这么敲）
        if head.isdigit():
            return f"EPSG:{head}"
        return ""

    def crs(self) -> QgsCoordinateReferenceSystem:
        """当前输入对应的坐标系；输入非法时返回无效 CRS（``isValid()`` 为假）。"""
        authid = self.authid()
        if authid:
            crs = QgsCoordinateReferenceSystem(authid)
            if crs.isValid():
                return crs
        # 非 authid 形态：QGIS 自身也能按描述名 / WKT / proj 串解析
        return QgsCoordinateReferenceSystem(self.currentText().strip())


@dataclass
class CreateOutcome:
    """「确定」建出来的东西（交给调用方做后续编排）。

    ``kind`` 三态与对话框的 ``_kind()`` 一一对应：``container`` 建了空库
    （只有 ``container_path``）、``file`` 建了独立图层文件、``into_db`` 往已有库里
    建了图层（``layer`` + ``db_path``）。

    **不在这里回传「目标库对应的已登记连接对象」**：它若经 Qt 的 ``itemData``
    传出来已是深拷副本，而 Dock 的 ``refresh_database`` 是按值在 ``_databases``
    里找连接的（副本一旦与实时缓存不一致就会静默 no-op）。调用方拿 ``db_path``
    自己去 Dock 里按路径取活对象，比传副本可靠。
    """

    kind: str
    #: 建出来的图层（``file`` / ``into_db`` 有，``container`` 为 ``None``）
    layer: QgsVectorLayer | None = None
    #: 建出来的库文件路径（``container`` 用）
    container_path: str = ""
    #: 写入的目标库路径（``into_db`` 用）
    db_path: str = ""


class CreateDataDialog(BaseDialog):
    """创建数据 Dialog（收集参数 + 校验 + 触发创建，入工程交给调用方）。"""

    #: 「应用」建出来的东西（带 :class:`CreateOutcome`）：调用方接这个信号做入工程 /
    #: 登记，**窗口不关**（连点几次可以连着建）。「确定」那条路仍走 ``exec()`` +
    #: :meth:`outcome`，两条路的结果由调用方同一段收尾处理。
    applied = pyqtSignal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        title: str = "创建数据",
        *,
        default_crs: str = "",
        local_dbs: Sequence[dict] = (),
        vector_layers: Sequence[object] = (),
        default_target_db: str = "",
        start_on_container: bool = False,
    ) -> None:
        """
        Args:
            parent: 父窗口。
            title: 窗口标题。
            default_crs: 调用方注入的默认坐标系。
            local_dbs: 「数据管理」里已登记的本地库（注入，不自己去问 Dock）。
            vector_layers: 工程里已加载的矢量图层（第二步「外部数据集」用）。
            default_target_db: 要把「目标库」下拉**预选**到哪个库（按文件路径匹配）。
                从数据管理的某个库上右键「创建数据」时传进来 —— 用户点的是那个库，
                就别让他再到下拉里找一遍。
            start_on_container: 打开时直接停在「本地数据库」页签（建空库）——
                「新建连接」里的「新建数据库…」走这条。
        """
        super().__init__(parent, title)
        #: 「确定」建出来的东西（未加入工程），见 :meth:`outcome`
        self._outcome: CreateOutcome | None = None
        #: 坐标系下拉的默认值。调用方（MainWindow）按 ``config/app.yaml`` 的
        #: ``app.crs`` 注入；未注入时兜底 :data:`_CRS_FALLBACK`
        self._default_crs = default_crs or _CRS_FALLBACK
        #: 「数据管理」里已登记的本地库（原 dict 清单，见 ``DataManageDock.set_databases``）。
        #: 注入而非自己去问 Dock：对话框不读外部状态、不做 IO（见模块头「职责边界」）
        self._local_dbs: list[dict] = [db for db in local_dbs if db.get("path")]
        #: 工程里已加载的矢量图层（第二步「结构来源 → 外部数据集」用）。同样靠注入 ——
        #: 对话框不自己去问 ``QgsProject``（那是调用方的事）
        self._vector_layers: list[object] = list(vector_layers)
        #: 「目标库」下拉要预选的库路径（从某个库右键「创建数据」进来时给）
        self._default_target_db = str(default_target_db or "")
        #: 打开时停在「本地数据库」页签（见 __init__ 的参数说明）
        self._start_on_container = bool(start_on_container)
        #: 向导当前步（1 = 基础参数，2 = 数据字段设置）；只对「单图层数据」页签有意义
        self._step = 1
        #: 「浏览…」选出来的库文件路径（外部库，不在 :attr:`_local_dbs` 里）
        self._browsed_db_path = ""
        #: 目标库下拉里最后一次落在**真实库**项上的下标（「＋新增数据库」跳走后再
        #: 回到本页时回退到它，免得停在一个没有库的项上、校验一直报"没选库"）
        self._last_target_index = -1
        #: 上一次「应用」成功时那份 spec（frozen dataclass，按值可比）：表单没再改过
        #: 就不重复创建 —— 同一份内容建两遍只会撞「已存在」
        self._applied_spec: object | None = None
        #: 控件是否全部建好。表单控件在构造过程中就会发 ``textChanged``
        #: （如 ``set_crs`` 触发 ``editTextChanged``），那时后面的控件还没建出来 ——
        #: 没有这个闸，``_refresh_hint`` 会去读不存在的属性（见 :meth:`_refresh_hint`）
        self._ui_ready = False
        # 最小宽度给到 760：第二步那张字段表（九列）在更窄的窗口里会出横向滚动条
        self.setMinimumSize(760, 0)
        self.resize(900, 520)
        self._init_ui()

    # --------------------------------------------------------------- 构建

    def _init_ui(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        head = QLabel("新建数据")
        head.setObjectName("SectionLabel")
        root.addWidget(head)

        # 实时校验提示先建出来：下面的控件一连上信号就会回调 _refresh_hint，
        # 那时 self.hint 必须已经存在（布局里插在页签之后，见下方 addWidget）
        self.hint = QLabel()
        self.hint.setObjectName("PropHint")
        self.hint.setWordWrap(True)

        self.tabs = QTabWidget()
        # 顺序：「单图层数据」在前（日常用得多），「本地数据库」在后
        # （建库是低频动作，且「目标库 → ＋新增数据库」会跳到这里）
        self.tabs.addTab(self._build_layer_page(self.tabs), "单图层数据")
        # 「本地数据库」页也先放个空壳：那页只在用户切过去（或点「＋新增数据库…」）时才建。
        # 开窗时把三个页全建出来是白花几百毫秒，见 :meth:`_ensure_container_page`
        self._container_built = False
        self._container_shell = QWidget(self.tabs)
        self._container_shell.setLayout(QVBoxLayout())
        self._container_shell.layout().setContentsMargins(0, 0, 0, 0)
        self.tabs.addTab(self._container_shell, "本地数据库")
        self.tabs.currentChanged.connect(self._on_kind_changed)
        root.addWidget(self.tabs, 1)

        # 实时校验提示：有问题显示问题，全通过则回显将要创建的参数（顺手确认
        # 「坐标系 / 目标库是否解析成功」——这些是本表单最容易填错的项）
        root.addWidget(self.hint)

        root.addWidget(self._build_footer())
        self.setLayout(root)
        # 控件齐了再开闸：构造期间的信号回调一律忽略（见 __init__ 的 _ui_ready）
        self._ui_ready = True
        if self._start_on_container:
            # 建空库那条路：页签切过去（容器那页是按需建的，切换时会建出来），
            # 光标落到「数据库文件名」上，少点一下
            self.tabs.setCurrentIndex(1)
            self.db_file_name_edit.setFocus()
        self._sync_footer()
        self._refresh_hint()
        # 第二步那页（四十多个控件）推迟到窗口显示后的第一个事件循环 tick 再建：
        # 打开是用户直接感受得到的，而这一步他只是刚看到窗口。**不是偷懒不建** ——
        # 推到下一个 tick 之后，点「下一步」时它已经备好了（见 fields_page 属性）。
        QTimer.singleShot(0, self._ensure_fields_page)

    # ---- 页签一：本地数据库（只建空库）----

    def _build_local_db_page(self, parent: QWidget) -> QWidget:
        page = QWidget(parent)
        form = self._new_form(page)

        self._db_path_edit = QLineEdit(default_output_dir())
        self._db_path_edit.setPlaceholderText("选择数据库文件要写入的目录")
        self._db_path_edit.textChanged.connect(self._refresh_hint)
        form.addRow(self._required_label("创建路径"), self._path_row(self._db_path_edit, self._on_browse_db_path))

        self._db_file_name_edit = QLineEdit()
        self._db_file_name_edit.setPlaceholderText("如 新建数据库.gpkg（扩展名可不填，按数据格式补）")
        self._db_file_name_edit.textChanged.connect(self._refresh_hint)
        # 编辑完（失焦 / 回车）把扩展名按格式收口，免得框里留着 .shp 这种对不上的后缀
        self._db_file_name_edit.editingFinished.connect(
            lambda: self._rewrite_extension(self._db_file_name_edit, self._current_db_format())
        )
        form.addRow(self._required_label("数据库文件名"), self._db_file_name_edit)

        self._db_format_combo = self._make_format_combo(_CONTAINER_FORMATS)
        self._db_format_combo.currentIndexChanged.connect(self._on_db_format_changed)
        form.addRow("数据格式", self._db_format_combo)

        note = QLabel(
            "只创建一个空的数据库文件（不建图层）。创建后自动登记到「数据管理」，"
            "再往库里加图层请用「单图层数据」页。"
        )
        note.setObjectName("PropHint")
        note.setWordWrap(True)
        form.addRow("", note)

        # 建完立刻同步一次占位提示（默认格式的扩展名）
        self._on_db_format_changed()
        return page

    # ---- 按需建页 + 控件访问（默认路径用不到的页，开窗时一律不建）----

    def _ensure_file_page(self) -> None:
        """建出「新建独立文件」那页（幂等）。

        **为什么推迟**：本机实测（挂上应用主题 QSS 后）建一个控件平均要十几毫秒 ——
        141 个控件的对话框打开要 2 秒出头。而「目标」默认是「写入已有本地数据库」，
        这页多数时候根本用不上，开窗时白建它就是几百毫秒。它由下面几个控件属性
        按需触发（谁碰 `path_edit` / `file_name_edit` 谁负责把它建出来）。
        """
        if self._file_built:
            return
        # 先把标志置上再建：建页的过程中会回调到这些控件属性（如 _on_format_changed 要读
        # format_combo），标志不置就无限递归 —— PyQt6 里递归爆栈会把进程直接带走
        self._file_built = True
        self._file_shell.layout().addWidget(self._build_file_target_page(self._file_shell))

    def _ensure_container_page(self) -> None:
        """建出「本地数据库」那页（幂等）：只在切到该页签或点「＋新增数据库…」时才建。"""
        if self._container_built:
            return
        self._container_built = True  # 同上：先置位再建，避免建页过程回调时递归
        self._container_shell.layout().addWidget(self._build_local_db_page(self._container_shell))
        self._container_built = True

    @property
    def path_edit(self) -> QLineEdit:
        """「新建独立文件」页的创建路径（访问即把该页建出来）。"""
        self._ensure_file_page()
        return self._path_edit

    @property
    def file_name_edit(self) -> QLineEdit:
        """「新建独立文件」页的数据文件名称。"""
        self._ensure_file_page()
        return self._file_name_edit

    @property
    def format_combo(self) -> QComboBox:
        """「新建独立文件」页的数据格式下拉。"""
        self._ensure_file_page()
        return self._format_combo

    @property
    def layer_name_edit(self) -> QLineEdit:
        """「新建独立文件」页的图层名。"""
        self._ensure_file_page()
        return self._layer_name_edit

    @property
    def geometry_combo(self) -> QComboBox:
        """「新建独立文件」页的几何类型下拉。"""
        self._ensure_file_page()
        return self._geometry_combo

    @property
    def crs_combo(self) -> CrsComboBox:
        """「新建独立文件」页的坐标系下拉。"""
        self._ensure_file_page()
        return self._crs_combo

    @property
    def db_path_edit(self) -> QLineEdit:
        """「本地数据库」页的创建路径（访问即把该页建出来）。"""
        self._ensure_container_page()
        return self._db_path_edit

    @property
    def db_file_name_edit(self) -> QLineEdit:
        """「本地数据库」页的数据库文件名。"""
        self._ensure_container_page()
        return self._db_file_name_edit

    @property
    def db_format_combo(self) -> QComboBox:
        """「本地数据库」页的数据格式下拉。"""
        self._ensure_container_page()
        return self._db_format_combo

    # ---- 页签二：单图层数据 ----

    def _build_layer_page(self, parent: QWidget) -> QWidget:
        """「单图层数据」页：两步向导（第 1 步目标 + 表单，第 2 步字段结构）。"""
        page = QWidget(parent)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)

        # 第 1 步：原来的整块内容（目标行 + 目标栈）
        step_one = QWidget(page)
        step_one_layout = QVBoxLayout(step_one)
        step_one_layout.setContentsMargins(0, 0, 0, 0)
        step_one_layout.setSpacing(10)

        target_row = QHBoxLayout()
        target_row.setContentsMargins(0, 0, 0, 0)
        target_row.setSpacing(8)
        target_row.addWidget(QLabel("目标"))
        self.target_combo = QComboBox()
        self.target_combo.addItem("新建独立文件", "file")
        self.target_combo.addItem("写入已有本地数据库", "into_db")
        # 默认落在「写入已有本地数据库」：日常用法是把新图层补进已经建好的库里
        self.target_combo.setCurrentIndex(1)
        self.target_combo.currentIndexChanged.connect(self._on_kind_changed)
        target_row.addWidget(self.target_combo, 1)
        step_one_layout.addLayout(target_row)

        # 两个目标页**都不在开窗时建**（见 :meth:`_ensure_file_page` 的说明）：
        # 先各放一个空壳当作栈里的占位，真正要用时再往里塞内容（索引不会变）。
        self._file_built = False
        self._file_shell = QWidget()  # addWidget 会把它认到栈底下（parent 不必先给）
        self._file_shell.setLayout(QVBoxLayout())
        self._file_shell.layout().setContentsMargins(0, 0, 0, 0)

        self.target_stack = QStackedWidget()
        self.target_stack.addWidget(self._file_shell)
        self.target_stack.addWidget(self._build_into_db_target_page(self.target_stack))
        # 目标栈必须跟上下拉的初值（_on_kind_changed 在控件建齐前被 _ui_ready 挡着）
        self.target_stack.setCurrentIndex(self.target_combo.currentIndex())
        step_one_layout.addWidget(self.target_stack, 1)

        # 第 2 步：字段结构设置（表格 / 工具条 / 结构来源整块都在那个页里）。
        # **不在开窗时建**：它有四十多个控件，实测光建它就占开窗耗时的一大截 ——
        # 由 :attr:`fields_page` 按需建、并在窗口显示后立刻预热（见 _init_ui 的 singleShot）
        self._fields_page: FieldsStepPage | None = None

        self.step_stack = QStackedWidget(page)
        self.step_stack.addWidget(step_one)
        layout.addWidget(self.step_stack, 1)
        return page

    def _build_file_target_page(self, parent: QWidget) -> QWidget:
        """目标 a：新建独立文件（一个文件一层）——原来的 6 行表单。"""
        page = QWidget(parent)
        form = self._new_form(page)

        self._path_edit = QLineEdit(default_output_dir())
        self._path_edit.setPlaceholderText("选择数据文件要写入的目录")
        self._path_edit.textChanged.connect(self._refresh_hint)
        form.addRow(self._required_label("创建路径"), self._path_row(self._path_edit, self._on_browse_path))

        self._file_name_edit = QLineEdit()
        self._file_name_edit.textChanged.connect(self._refresh_hint)
        # 编辑完（失焦 / 回车）后再做两件「少打一次字」的事：用去扩展名的文件名
        # 预填图层名、按手打的扩展名切换格式下拉（见 _on_file_name_edited）
        self._file_name_edit.editingFinished.connect(self._on_file_name_edited)
        form.addRow(self._required_label("数据文件名称"), self._file_name_edit)

        self._format_combo = self._make_format_combo(_FILE_FORMATS)
        self._format_combo.currentIndexChanged.connect(self._on_format_changed)
        form.addRow("数据格式", self._format_combo)

        self._layer_name_edit = QLineEdit()
        self._layer_name_edit.setPlaceholderText("图层在工程与数据源里显示的名字")
        self._layer_name_edit.textChanged.connect(self._refresh_hint)
        form.addRow(self._required_label("图层名"), self._layer_name_edit)

        self._geometry_combo = self._geometry_combo()
        form.addRow("几何类型", self._geometry_combo)

        self._crs_combo = CrsComboBox()
        self._crs_combo.editTextChanged.connect(self._refresh_hint)
        self._crs_combo.set_crs(self._default_crs)
        form.addRow("坐标系", self._crs_row(self._crs_combo, lambda: self._on_pick_crs(self._crs_combo)))

        # 建完立刻同步一次占位提示（默认格式的扩展名）
        self._on_format_changed()
        return page

    def _build_into_db_target_page(self, parent: QWidget) -> QWidget:
        """目标 b：写入已有本地数据库（库内新建一张表）。"""
        page = QWidget(parent)
        form = self._new_form(page)

        self.db_target_combo = QComboBox()
        for db in self._local_dbs:
            self.db_target_combo.addItem(str(db.get("name") or Path(str(db["path"])).stem), db)
            self.db_target_combo.setItemData(
                self.db_target_combo.count() - 1, str(db["path"]), Qt.ItemDataRole.ToolTipRole
            )
        # 两个哨兵项固定挂在最后：itemData 是字符串标记（已登记库挂的是连接 dict）
        self.db_target_combo.addItem("浏览…", _TARGET_BROWSE)
        self.db_target_combo.addItem("＋ 新增数据库…", _TARGET_NEW)
        self.db_target_combo.currentIndexChanged.connect(self._on_db_target_changed)
        # 两个动作项（浏览… / ＋新增数据库…）挂在 activated 上 —— 用户重选当前项时
        # currentIndexChanged 不发信号，只靠它就"再点浏览没反应"（见 _on_db_target_activated）
        self.db_target_combo.activated.connect(self._on_db_target_activated)
        if self.db_target_combo.count() > 2:
            self._last_target_index = 0
        self._select_default_target_db()
        form.addRow(self._required_label("目标库"), self.db_target_combo)

        self.into_layer_name_edit = QLineEdit()
        self.into_layer_name_edit.setPlaceholderText("新建图层在库里的名字（表名）")
        self.into_layer_name_edit.textChanged.connect(self._refresh_hint)
        form.addRow(self._required_label("图层名"), self.into_layer_name_edit)

        self.into_geometry_combo = self._geometry_combo()
        form.addRow("几何类型", self.into_geometry_combo)

        self.into_crs_combo = CrsComboBox()
        self.into_crs_combo.editTextChanged.connect(self._refresh_hint)
        self.into_crs_combo.set_crs(self._default_crs)
        form.addRow("坐标系", self._crs_row(self.into_crs_combo, lambda: self._on_pick_crs(self.into_crs_combo)))

        note = QLabel("新图层作为一张表建进该库，库里已有的图层不受影响。")
        note.setObjectName("PropHint")
        note.setWordWrap(True)
        form.addRow("", note)
        return page

    # ---- 可复用的行构造 ----

    @staticmethod
    def _new_form(parent: QWidget) -> QFormLayout:
        form = QFormLayout(parent)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setSpacing(10)
        form.setContentsMargins(8, 10, 8, 8)
        return form

    @staticmethod
    def _path_row(edit: QLineEdit, on_browse) -> QHBoxLayout:
        """「输入框 + 浏览…按钮」一行。"""
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(on_browse)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(edit, 1)
        row.addWidget(browse_btn)
        return row

    @staticmethod
    def _required_label(text: str) -> QLabel:
        """必填项的标签：文字前加一颗红星（与原型一致）。

        颜色取当前主题的 ``text_error``（深色主题下是另一种红），所以**用行内富文本而不是
        主题 QSS** —— QSS 是启动时下发一次的，这里现取现用，切主题后新建的对话框也是对的。
        """
        color = ThemeManager.instance().current().text_error
        label = QLabel(f'<span style="color:{color}">*</span>&nbsp;{text}')
        label.setTextFormat(Qt.TextFormat.RichText)
        return label

    @staticmethod
    def _crs_row(combo: CrsComboBox, on_pick) -> QHBoxLayout:
        """「坐标系下拉 + 选择按钮」一行（按钮开原型那个坐标系选择弹窗）。

        下拉本身能搜 EPSG 代码 / 名称（老手够用），「选择」是给不知道代码的人走的：
        按基准与分带挑，或直接照抄某个图层的空间参考。
        """
        pick_btn = QPushButton("选择")
        pick_btn.setToolTip("按基准 / 分带挑选，或沿用某个图层、某份数据的空间参考")
        pick_btn.clicked.connect(on_pick)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(combo, 1)
        row.addWidget(pick_btn)
        return row

    @staticmethod
    def _geometry_combo() -> QComboBox:
        """几何类型下拉：中文名显示、QGIS 几何类型名作 itemData。"""
        combo = QComboBox()
        for label, geometry_type in _GEOMETRY_TYPES:
            combo.addItem(label, geometry_type)
        combo.setCurrentIndex(0)
        return combo

    @staticmethod
    def _make_format_combo(formats: Sequence[VectorFormat]) -> QComboBox:
        """数据格式下拉：**只列本机真能写出的格式**，itemData 挂 :class:`VectorFormat`。

        驱动在本机构建里不存在（如便携包里没带 FileGDB）的格式**直接不出现**：
        灰着占位对用户没有价值（点了也建不出来），隐藏掉才干净。
        能建、但驱动名与首选不同的（FileGDB → ``OpenFileGDB``），
        :func:`~app.core.util.vector_formats.resolve_format` 会把驱动名换好再交出来，
        所以下游拿到的 ``VectorFormat.driver`` 一定是本机可写的那个。

        格式自身的限制（如 Shapefile 字段名 ≤10 字符）放 tooltip，不占界面。
        """
        combo = QComboBox()
        writers = available_drivers()
        for fmt in formats:
            resolved = resolve_format(fmt, writers)
            if resolved is None:
                logger.info("创建数据：本机没有 %s 的可写驱动，格式下拉里隐藏它", fmt.driver)
                continue
            combo.addItem(resolved.label, resolved)
            if resolved.note:
                combo.setItemData(combo.count() - 1, resolved.note, Qt.ItemDataRole.ToolTipRole)
        combo.setCurrentIndex(0)
        return combo

    def _build_footer(self) -> QWidget:
        """页脚：一个栈，两套按钮 —— 向导页一套、容器页一套（见 :meth:`_sync_footer`）。"""
        self.footer_stack = QStackedWidget()
        self.footer_stack.addWidget(self._build_wizard_footer())
        self.footer_stack.addWidget(self._build_container_footer())
        return self.footer_stack

    def _build_wizard_footer(self) -> QWidget:
        """「单图层数据」页的页脚：``[取消] [上一步] [下一步 | 创建]``。

        ``下一步`` 与 ``创建`` 是同一个位置的两个按钮，按步骤显隐：第 1 步是「下一步」，
        最后一步是「创建」（照原型的说法；本对话框里它等价于原来的「确定」= 建完关窗）。
        「上一步」在第 1 步禁用（原型同款）。
        """
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        row.addWidget(cancel_btn)

        self.prev_btn = QPushButton("上一步")
        self.prev_btn.setToolTip("回到基础参数那一步（字段表会原样留着）")
        self.prev_btn.clicked.connect(self.prev_step)
        row.addWidget(self.prev_btn)

        self.next_btn = QPushButton("下一步")
        self.next_btn.setToolTip("进入数据字段设置")
        self.next_btn.clicked.connect(self.next_step)
        row.addWidget(self.next_btn)

        self.create_btn = QPushButton("创建")
        self.create_btn.clicked.connect(self.accept)
        row.addWidget(self.create_btn)
        return widget

    def _build_container_footer(self) -> QWidget:
        """「本地数据库」页的页脚：``[应用] [取消] [确定]``。

        三个都是普通按钮（**确定不加 ``DialogPrimary`` 主色实心样式**，与另两个同色）；
        最右边是「确定」（关窗那条路），它左边才是「应用」（建完不关窗，可以接着建下一个）。

        「确定」必须显式 ``setDefault(True)``：实测（离屏按键）不设的话 **Enter 谁也
        不触发** —— 敲回车没反应，用户以为窗口卡住了。``setDefault`` 只给按钮加一圈
        默认态边框（主题的 ``QPushButton:default``），底色跟另两个一样。
        """
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        self.apply_btn = QPushButton("应用")
        self.apply_btn.setToolTip("建出来但窗口不关，可以接着建下一个")
        self.apply_btn.clicked.connect(self.apply_now)
        row.addWidget(self.apply_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        row.addWidget(cancel_btn)
        self.ok_btn = QPushButton("确定")
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self.accept)
        row.addWidget(self.ok_btn)
        return widget

    # --------------------------------------------------------------- 向导步骤

    def _sync_footer(self) -> None:
        """按 (页签, 步骤) 摆好页脚与步骤栈 —— **所有按钮状态的唯一来源**。

        - 容器页（「本地数据库」）：页脚栈翻到容器那套，默认按钮是「确定」；
        - 向导页第 1 步：``[取消][上一步(禁用)][下一步]``，默认按钮是「下一步」
          —— 回车只会往下走一步，**绝不会直接把数据建出来**；
        - 向导页最后一步：``[取消][上一步][创建]``，默认按钮换成「创建」。
        """
        if self.tabs.currentIndex() != 0:  # 容器页不走向导
            self.footer_stack.setCurrentIndex(1)
            self.ok_btn.setDefault(True)
            self.next_btn.setDefault(False)
            self.create_btn.setDefault(False)
            return

        self.footer_stack.setCurrentIndex(0)
        on_last_step = self._step >= 2
        if on_last_step:
            # 翻页前先把那页建出来：页还没加进栈时 setCurrentIndex(1) 会落空（页脚说第 2 步、
            # 画面还停在第 1 步）
            self._ensure_fields_page()
        self.step_stack.setCurrentIndex(1 if on_last_step else 0)
        self.prev_btn.setEnabled(on_last_step)
        self.next_btn.setVisible(not on_last_step)
        self.create_btn.setVisible(on_last_step)
        # 默认按钮跟着步骤走（隐藏的那个必须显式取消，否则回车还是会落到它身上）
        self.next_btn.setDefault(not on_last_step)
        self.create_btn.setDefault(on_last_step)
        self.ok_btn.setDefault(False)

    def next_step(self) -> None:
        """「下一步」：先按第 1 步的规则校验，过了才翻到字段结构那一步。

        这里先把基础参数校验一遍（原型也是「点下一步时才弹窗提示」），
        免得用户填完字段才发现路径 / 名字有问题。
        """
        if self._step >= 2:
            return
        self._sync_format_from_name()
        error, widget = self._validate_step1()
        if error:
            self._fail(error, widget)
            return
        self._step = 2
        self._sync_footer()
        self.fields_page.focus_table()
        self._refresh_hint()

    def prev_step(self) -> None:
        """「上一步」：回基础参数那一步。**字段表原样留着**，不清空。"""
        if self._step <= 1:
            return
        self._step = 1
        self._sync_footer()
        self._refresh_hint()

    def _ensure_fields_page(self) -> FieldsStepPage:
        """按需建出第二步那页（幂等；:attr:`fields_page` 与开窗后的预热都调它）。"""
        if self._fields_page is None:
            page = FieldsStepPage(
                self.step_stack,
                vector_layers=self._vector_layers,
                default_dir=default_output_dir(),
                # 字典挂接只列当前目标库里的字典表（路径随向导切换现取）
                target_db_path=self._dict_db_path,
            )
            page.fieldsChanged.connect(self._refresh_hint)
            page.statusMessage.connect(self._on_step_message)
            self.step_stack.addWidget(page)
            self._fields_page = page
        return self._fields_page

    @property
    def fields_page(self) -> FieldsStepPage:
        """第二步的字段结构页（第一次访问时才建，见 :meth:`_ensure_fields_page`）。"""
        return self._ensure_fields_page()

    def _on_step_message(self, message: str) -> None:
        """字段页要说的话（导入结果 / 删除了几行）：落在底部提示 + 消息条上。"""
        if not self._ui_ready:
            return
        self.hint.setText(message)
        send("info", message, duration=4)

    # --------------------------------------------------------------- 交互

    def _on_kind_changed(self, *_args: object) -> None:
        """页签 / 目标切换：把目标栈翻到对应页、页脚跟着换，再刷一次提示。

        ``currentChanged`` / ``currentIndexChanged`` 都会带一个 index 参数，空参也兼容。
        **切页签不回退步骤、也不清字段表**：用户只是想去看一眼「本地数据库」页，
        切回来还得接着填字段。
        """
        if not self._ui_ready:
            return
        if self.tabs.currentIndex() == 0:  # 「单图层数据」页才有目标栈
            self.target_stack.setCurrentIndex(max(0, self.target_combo.currentIndex()))
        self._sync_footer()
        self._refresh_hint()

    def _on_browse_path(self) -> None:
        """选目录；起始目录用当前填写的值（没填则交给对话框默认）。"""
        chosen = open_file_dir(
            self,
            is_dir=True,
            title="选择创建路径",
            init_path=self.path_edit.text().strip(),
        )
        if chosen:
            self.path_edit.setText(chosen)

    def _select_default_target_db(self) -> None:
        """把「目标库」下拉预选到调用方指定的那个库（按路径匹配；没给 / 没匹配上不动）。

        从数据管理的某个库上右键「创建数据」进来时用 —— 用户点的是那个库，
        不该让他再到下拉里找一遍。
        """
        wanted = self._default_target_db
        if not wanted:
            return
        for index in range(self.db_target_combo.count()):
            data = self.db_target_combo.itemData(index)
            if isinstance(data, dict) and str(data.get("path") or "") == wanted:
                self.db_target_combo.setCurrentIndex(index)
                self._last_target_index = index
                return

    def _on_browse_db_path(self) -> None:
        """页签一：选空库要写入的目录。"""
        chosen = open_file_dir(
            self,
            is_dir=True,
            title="选择创建路径",
            init_path=self.db_path_edit.text().strip(),
        )
        if chosen:
            self.db_path_edit.setText(chosen)

    def _on_pick_crs(self, combo: CrsComboBox) -> None:
        """坐标系那行的「选择」按钮：开坐标系选择弹窗，确定后把结果写回下拉框。

        弹窗里可能给出的是**自定义坐标系的 WKT**（认不出 EPSG 代码时），
        :meth:`CrsComboBox.set_crs` 与 :meth:`CrsComboBox.crs` 都吃得下这两种形态。
        """
        dialog = CrsPickDialog(
            self,
            current=combo.authid() or combo.currentText().strip(),
            layers=self._vector_layers,
        )
        accepted = dialog.exec()
        value = dialog.crs_value() if accepted else ""
        dialog.deleteLater()
        if value:
            combo.set_crs(value)
            self._refresh_hint()

    def _on_db_target_changed(self, index: int) -> None:
        """目标库下拉换项：真实库项记下来并刷提示。

        两个**动作项**（「浏览…」「＋新增数据库…」）不在这里处理 —— 它们归
        :meth:`_on_db_target_activated`。原因见那边的说明（下标没变时
        ``currentIndexChanged`` 不会发信号，用户第二次点「浏览…」就没反应了）。
        """
        if isinstance(self.db_target_combo.itemData(index) if index >= 0 else None, dict):
            self._last_target_index = index
        self._refresh_hint()

    def _on_db_target_activated(self, index: int) -> None:
        """用户在弹层里**主动选中**某一项（含重选当前项）：处理两个动作项。

        **必须挂在 ``activated`` 上，不能只靠 ``currentIndexChanged``**：用户上一次
        「浏览…」选过库之后，下拉停在这一项上，再点它一次下标没变 ——
        ``currentIndexChanged`` 根本不发信号，"再点浏览弹不出选择框"就是这么来的
        （用户报的）。``activated`` 每次用户选择都会发，包括重选当前项；
        而 ``setCurrentIndex`` 这类**程序改**的选择不会发，正合我意
        （建完库自动补进下拉时不该弹文件框）。
        """
        marker = self.db_target_combo.itemData(index) if index >= 0 else None
        if marker == _TARGET_NEW:
            self._jump_to_container_tab()
        elif marker == _TARGET_BROWSE:
            self._browse_target_db(index)

    def _browse_target_db(self, index: int) -> None:
        """「浏览…」：选一个库外的本地库文件，选中后把下拉那一项改写成它的文件名。"""
        chosen = open_file_dir(
            self,
            title="选择本地数据库文件",
            init_path=self._browsed_db_path or default_output_dir(),
            filter_=_DB_FILE_FILTER,
        )
        if not chosen:
            # 取消：别把下拉停在「浏览…」上（validate 会一直提示没选库）
            self._restore_target_index()
            self._refresh_hint()
            return
        self._browsed_db_path = chosen
        self.db_target_combo.setItemText(index, f"浏览…（{os.path.basename(chosen)}）")
        self._refresh_hint()

    def _restore_target_index(self) -> None:
        """把目标库下拉退回最后一个真实库项。

        一个真实库都没有时**什么都不做**：下拉里只剩「浏览…」与「＋新增数据库…」两个
        哨兵项，退到第 0 项就是停到「浏览…」上（用户会以为自己在选文件），而停在
        「＋新增数据库…」正对上他刚做的动作；建完库会被
        :meth:`_add_created_local_db` 自动选中。
        """
        if self._last_target_index >= 0:
            index = self._last_target_index
        elif isinstance(self.db_target_combo.itemData(0), dict):
            index = 0
        else:
            return
        self.db_target_combo.blockSignals(True)
        self.db_target_combo.setCurrentIndex(index)
        self.db_target_combo.blockSignals(False)

    def _jump_to_container_tab(self) -> None:
        """「＋新增数据库…」→ 跳去「本地数据库」页签建库。

        下拉先退回上一个真实目标：待会儿建完库回到本页时，目标栏不该停在一个
        没有库的项上（建完的库会自动进这个下拉并被选中，见 :meth:`_add_created_local_db`）。
        """
        self._restore_target_index()
        self.tabs.setCurrentIndex(1)
        self.db_file_name_edit.setFocus()

    def _add_created_local_db(self, path: str) -> None:
        """把刚建好的库补进目标库下拉并选中（建库 → 写图层可以一口气做完）。

        插在**最前面**（两个哨兵项固定挂在最后）—— 下拉里已登记的库是
        「新的在上」（数据管理 Dock 的列表顺序，见 ``add_connection``），
        刚建好的这个理应排第一。
        """
        if not path:
            return
        wanted = str(path)
        for i in range(self.db_target_combo.count()):
            data = self.db_target_combo.itemData(i)
            if isinstance(data, dict) and str(data.get("path") or "") == wanted:
                self.db_target_combo.setCurrentIndex(i)
                return
        name = Path(wanted).stem
        index = 0
        self.db_target_combo.insertItem(index, name, {"kind": "local", "name": name, "path": wanted})
        self.db_target_combo.setItemData(index, wanted, Qt.ItemDataRole.ToolTipRole)
        self.db_target_combo.setCurrentIndex(index)

    def _current_format(self) -> VectorFormat:
        """页签二模式 a 当前格式（下拉项 itemData 就是 :class:`VectorFormat`）。"""
        fmt = self.format_combo.currentData()
        return fmt if isinstance(fmt, VectorFormat) else _FILE_FORMATS[0]

    def _current_db_format(self) -> VectorFormat:
        """页签一当前格式（容器格式之一）。"""
        fmt = self.db_format_combo.currentData()
        return fmt if isinstance(fmt, VectorFormat) else _CONTAINER_FORMATS[0]

    def _on_format_changed(self, *_args: object) -> None:
        """切数据格式：占位提示跟上，并把文件名扩展名换成新格式的。"""
        fmt = self._current_format()
        self.file_name_edit.setPlaceholderText(f"如 新建数据{fmt.extension}（扩展名可不填，按数据格式补）")
        self._rewrite_extension(self.file_name_edit, fmt)

    def _on_db_format_changed(self, *_args: object) -> None:
        """页签一切库格式：占位提示与扩展名同理。"""
        fmt = self._current_db_format()
        self.db_file_name_edit.setPlaceholderText(
            f"如 新建数据库{fmt.extension}（扩展名可不填，按数据格式补）"
        )
        self._rewrite_extension(self.db_file_name_edit, fmt)

    @staticmethod
    def _rewrite_extension(edit: QLineEdit, fmt: VectorFormat) -> None:
        """把文件名扩展名换成 ``fmt`` 的（保留主名）；空文件名不动。"""
        raw = edit.text().strip()
        if not raw:
            return
        normalized = f"{os.path.splitext(raw)[0]}{fmt.extension}"
        if normalized != edit.text():
            edit.setText(normalized)

    @staticmethod
    def _select_format_by_extension(combo: QComboBox, extension: str) -> None:
        """把下拉切到扩展名对应的那一项。

        **按扩展名认，不按驱动名认**：下拉项里存的是 :func:`resolve_format` 解析过的
        格式（FileGDB 的驱动已经换成 ``OpenFileGDB``），拿原表的 ``driver`` 去比
        会一个也对不上。扩展名在 :data:`VECTOR_FORMATS` 里是唯一的，正好当身份用。
        """
        for index in range(combo.count()):
            item = combo.itemData(index)
            if isinstance(item, VectorFormat) and item.extension == extension:
                combo.setCurrentIndex(index)
                return

    def _sync_format_from_name(self) -> None:
        """文件名里认得出的扩展名优先（``地块.shp`` → 格式下拉切成 Shapefile）。

        格式下拉改扩展名是正向同步（:meth:`_on_format_changed`），这里兜住反
        过来的路径：用户直接手打扩展名。两条方向合起来，两个控件不可能停在
        互相矛盾的组合上。认不出的扩展名（``.txt``）不动格式，由下拉收口；
        认得出但不在本页候选里的（页签一里打 ``.shp``）也不动。

        **页还没建出来就直接返回**：这两个页是按需建的（见 :meth:`_ensure_file_page`），
        在这里顺着 `self.file_name_edit` 摸下去会把它们提前建出来，等于把推迟建页的
        收益又还回去了。
        """
        is_file_page = self.tabs.currentIndex() == 0
        if is_file_page and not self._file_built:
            return
        if not is_file_page and not self._container_built:
            return
        edit = self.file_name_edit if is_file_page else self.db_file_name_edit
        combo = self.format_combo if is_file_page else self.db_format_combo
        fmt = format_for_extension(os.path.splitext(edit.text().strip())[1])
        if fmt is None:
            return
        current = combo.currentData()
        if isinstance(current, VectorFormat) and current.extension == fmt.extension:
            return
        self._select_format_by_extension(combo, fmt.extension)

    @staticmethod
    def _normalized_file_name(edit: QLineEdit, fmt: VectorFormat) -> str:
        """按 ``fmt`` 补/换扩展名的文件名（「数据格式」是唯一权威）。"""
        raw = edit.text().strip()
        if not raw:
            return ""
        return f"{os.path.splitext(raw)[0]}{fmt.extension}"

    def _on_file_name_edited(self) -> None:
        """数据文件名称编辑完：按扩展名切格式，并给空的图层名预填主名。"""
        self._sync_format_from_name()
        if self.layer_name_edit.text().strip():
            return
        stem = os.path.splitext(self.file_name_edit.text().strip())[0]
        if stem:
            self.layer_name_edit.setText(stem)

    # --------------------------------------------------------------- 取值

    def _kind(self) -> str:
        """当前表单表达的是哪种创建：``container`` / ``file`` / ``into_db``。"""
        if self.tabs.currentIndex() == 1:  # 「本地数据库」页签排在第二个
            return "container"
        return "into_db" if self.target_combo.currentIndex() == 1 else "file"

    def _selected_local_db(self) -> dict | None:
        """目标库下拉里命中的已登记库（原 dict）；「浏览…」时返回 ``None``。"""
        data = self.db_target_combo.currentData()
        return data if isinstance(data, dict) else None

    def _target_db_path(self) -> str:
        """目标库文件路径：命中的已登记库，或「浏览…」选的外部文件。"""
        db = self._selected_local_db()
        return str(db.get("path") or "") if db is not None else self._browsed_db_path

    def _dict_db_path(self) -> str:
        """字典挂接用的"当前库"路径。

        「接入字典」只允许选**当前库里已有**的字典表。创建数据时只有
        「往已有库里建图层」这一种形态有库；独立文件（.gpkg / .sqlite 也一样）
        在创建前还不存在、创建服务又会拒绝已存在的目标文件，所以这里返回空串，
        由选择器提示先建好图层、之后到「数据管理」里挂接字典。
        """
        return self._target_db_path() if self._kind() == "into_db" else ""

    def container_spec(self) -> CreateContainerSpec:
        """页签一取值：建空库。"""
        fmt = self._current_db_format()
        path = self.db_path_edit.text().strip()
        return CreateContainerSpec(
            path=os.path.normpath(path) if path else "",
            file_name=self._normalized_file_name(self.db_file_name_edit, fmt),
            data_format=fmt.label,
            driver=fmt.driver,
            extension=fmt.extension,
        )

    def file_spec(self) -> CreateDataSpec:
        """页签二模式 a 取值：建独立图层文件。"""
        fmt = self._current_format()
        path = self.path_edit.text().strip()
        crs = self.crs_combo.crs()
        is_valid_crs = crs.isValid()
        return CreateDataSpec(
            path=os.path.normpath(path) if path else "",
            file_name=self._normalized_file_name(self.file_name_edit, fmt),
            data_format=fmt.label,
            driver=fmt.driver,
            extension=fmt.extension,
            layer_name=self.layer_name_edit.text().strip(),
            geometry_type=self.geometry_combo.currentData() or "",
            crs_authid=crs.authid() if is_valid_crs else "",
            crs_description=crs.description() if is_valid_crs else "",
            # 新建图层一律带 mzguid（成果数据的图斑唯一标识，见 fields.with_mzguid）
            fields=with_mzguid(self.fields_page.fields()),
        )

    def into_db_spec(self) -> CreateLayerInDbSpec:
        """页签二模式 b 取值：往已有库里建图层。"""
        crs = self.into_crs_combo.crs()
        is_valid_crs = crs.isValid()
        return CreateLayerInDbSpec(
            db_path=self._target_db_path(),
            layer_name=self.into_layer_name_edit.text().strip(),
            geometry_type=self.into_geometry_combo.currentData() or "",
            crs_authid=crs.authid() if is_valid_crs else "",
            crs_description=crs.description() if is_valid_crs else "",
            # 新建图层一律带 mzguid（成果数据的图斑唯一标识，见 fields.with_mzguid）
            fields=with_mzguid(self.fields_page.fields()),
        )

    def data(self) -> CreateDataSpec:
        """兼容旧调用方：等价于 :meth:`file_spec`（页签二模式 a）。"""
        return self.file_spec()

    def _validate(self) -> tuple[str, QWidget | None]:
        """校验整张表单（两步串联）；返回 ``(错误文案, 需要聚焦的控件)``。

        **第 1 步的规则先跑，第 2 步的字段规则后跑**：这样「点创建」时先报的是
        基础参数的问题（用户更容易在那里填错），而且焦点指过去的控件一定在看得见的那一步。
        容器页没有字段可言，只跑第 1 步。
        """
        error, widget = self._validate_step1()
        if error or self._kind() == "container":
            return error, widget
        bad_fields = validate_fields(self.fields_page.fields())
        if bad_fields:
            return bad_fields, self.fields_page
        return "", None

    def _validate_step1(self) -> tuple[str, QWidget | None]:
        """第 1 步（基础参数）的校验规则。"""
        kind = self._kind()
        if kind == "container":
            error, widget = self._validate_dir(self.db_path_edit)
            if error:
                return error, widget
            return self._validate_name(self.db_file_name_edit, "数据库文件名")
        if kind == "into_db":
            if not self._target_db_path():
                return "请选择要写入的本地数据库，或点「浏览…」选一个库文件", self.db_target_combo
            error, widget = self._validate_name(self.into_layer_name_edit, "图层名")
            if error:
                return error, widget
            if not self.into_crs_combo.crs().isValid():
                return "坐标系无效：请输入 EPSG 代码（如 4490）或从下拉候选里选一项", self.into_crs_combo
            return "", None

        error, widget = self._validate_dir(self.path_edit)
        if error:
            return error, widget
        error, widget = self._validate_name(self.file_name_edit, "数据文件名称")
        if error:
            return error, widget
        # 扩展名不在这里卡：漏填由「数据格式」补，写错由 spec 按格式换掉
        error, widget = self._validate_name(self.layer_name_edit, "图层名")
        if error:
            return error, widget
        if not self.crs_combo.crs().isValid():
            return "坐标系无效：请输入 EPSG 代码（如 4490）或从下拉候选里选一项", self.crs_combo
        return "", None

    @staticmethod
    def _validate_dir(edit: QLineEdit) -> tuple[str, QWidget | None]:
        path = edit.text().strip()
        if not path:
            return "请填写创建路径", edit
        path = os.path.normpath(path)
        if not os.path.isdir(path):
            return f"创建路径不存在或不是目录：{path}", edit
        if not os.access(path, os.W_OK):
            return f"创建路径不可写：{path}", edit
        return "", None

    @staticmethod
    def _validate_name(edit: QLineEdit, label: str) -> tuple[str, QWidget | None]:
        name = edit.text().strip()
        if not name:
            return f"请填写{label}", edit
        bad = _illegal_chars(name)
        if bad:
            return f"{label}不能包含字符：{bad}", edit
        return "", None

    def _refresh_hint(self, *_args: object) -> None:
        """刷新底部提示（接在各字段的变更信号上，空参兼容各信号签名）。

        第 2 步只回显字段概况 —— 第 1 步那些控件虽然看不见了，信号还接着，
        不按步骤分流的话它们一响就会把字段页的提示冲掉。
        """
        if not self._ui_ready:
            return
        # 先让格式下拉与文件名对齐，再校验 —— 提示里回显的才是最终会创建的名字
        self._sync_format_from_name()
        error, _widget = self._validate()
        if error:
            self.hint.setText(error)
            return
        kind = self._kind()
        if kind == "container":
            spec = self.container_spec()
            self.hint.setText(
                f"将创建空库：{spec.file_path} · {spec.data_format}（{spec.extension}）"
                " · 创建后登记进「数据管理」"
            )
        elif self._step >= 2:
            self.hint.setText(self._fields_summary(kind))
        elif kind == "into_db":
            spec = self.into_db_spec()
            self.hint.setText(
                f"将在 {os.path.basename(spec.db_path)} 里创建图层：{spec.layer_name}"
                f" · {self.into_geometry_combo.currentText()} · {spec.crs_authid}"
            )
        else:
            spec = self.file_spec()
            fmt_txt = f"{spec.data_format}（{spec.extension}）"
            self.hint.setText(
                f"将创建：{spec.file_path} · {fmt_txt}"
                f" · {self.geometry_combo.currentText()} · {spec.crs_authid}"
            )

    def _fields_summary(self, kind: str) -> str:
        """第 2 步的提示：目标 + 字段概况（让用户在第 2 步也看得见建的是什么）。"""
        if kind == "into_db":
            spec = self.into_db_spec()
            target = f"{os.path.basename(spec.db_path)} 里的图层「{spec.layer_name}」"
        else:
            spec = self.file_spec()
            target = f"{spec.file_path}（{spec.data_format}）"
        fields = with_mzguid(self.fields_page.fields())
        if len(fields) == 1:
            return f"将创建 {target} · 只含自动字段 mzguid（可点「上一步」回去加字段）"
        not_null = sum(1 for f in fields if not f.nullable)
        hidden = sum(1 for f in fields if not f.visible)
        dicts = sum(1 for f in fields if f.dict_table)
        extra = ""
        if not_null:
            extra += f" · {not_null} 个不可为空"
        if hidden:
            extra += f" · {hidden} 个隐藏"
        if dicts:
            extra += f" · {dicts} 个字典字段"
        return f"将创建 {target} · 共 {len(fields)} 个字段（含自动字段 mzguid）{extra}"

    # --------------------------------------------------------------- 落盘

    def accept(self) -> None:
        """「确定」的硬闸：校验不过或建不出来就留在窗口里，把问题指给用户。"""
        # 「应用」过、表单又没再改 → 直接关窗：同一份内容不能再建一遍（会撞「已存在」）
        if self._already_applied():
            self._outcome = None  # 已经交出去了，别让调用方收尾第二遍
            super().accept()
            return
        if self._create() is None:
            return
        super().accept()

    def apply_now(self) -> None:
        """「应用」：建出来但**不关窗**，结果经 :attr:`applied` 直接交给调用方。

        典型用法是连着建几个图层（改个图层名 → 应用 → 再改 → 应用 …），
        最后点「确定」关窗。建不出来时与原「确定」一致：留在窗口里报错。
        """
        if self._already_applied():
            self.hint.setText(
                "这份内容已经创建过了：改一下再点「应用」，或直接点「确定」关闭窗口"
            )
            return
        outcome = self._create()
        if outcome is None:
            return
        # 先记下这份 spec 再动表单：随后把新建的库补进目标库下拉会改控件状态
        self._applied_spec = self._current_spec()
        self._outcome = None  # 结果经信号交出去了，避免调用方收尾两遍
        if outcome.kind == "container":
            self._add_created_local_db(outcome.container_path)
        self.applied.emit(outcome)
        self.hint.setText(f"已创建：{self._describe_outcome(outcome)}（窗口未关闭，可继续创建）")

    def _already_applied(self) -> bool:
        """当前表单是否与上一次「应用」的完全一致（一致就不该再建一遍）。"""
        return self._applied_spec is not None and self._current_spec() == self._applied_spec

    def _current_spec(self) -> object:
        """按当前形态取 spec（三个 frozen dataclass，可比相等）。"""
        kind = self._kind()
        if kind == "container":
            return self.container_spec()
        if kind == "into_db":
            return self.into_db_spec()
        return self.file_spec()

    def _create(self) -> CreateOutcome | None:
        """按当前表单创建；失败把原因留在窗口里并返回 ``None``。"""
        kind = self._kind()
        self._sync_format_from_name()
        error, widget = self._validate()
        if error:
            self._fail(error, widget)
            return None

        try:
            if kind == "container":
                path = CreateContainerService().create(self.container_spec())
                self._outcome = CreateOutcome(kind="container", container_path=path)
            elif kind == "into_db":
                spec = self.into_db_spec()
                dict_specs = ()
                try:
                    dict_specs = self._build_dict_specs(spec.db_path)
                except DictTableError as exc:
                    self._fail(str(exc), self._focus_widget_for(kind))
                    return None
                if dict_specs:
                    spec = replace(spec, dict_tables=dict_specs)
                layer = CreateLayerInDbService().create(spec)
                self._outcome = CreateOutcome(kind="into_db", layer=layer, db_path=spec.db_path)
                self._report_field_persistence(layer, spec.fields, in_db=True)
                self._apply_local_dict_props(layer, spec.fields, dict_specs)
                if dict_specs:
                    send(
                        "info",
                        "已保存字典表：" + "、".join(item.table_name for item in dict_specs),
                        duration=6,
                    )
            else:
                spec = self.file_spec()
                layer = CreateDataService().create(spec)
                self._outcome = CreateOutcome(kind="file", layer=layer)
                actual_crs = layer.crs().authid()
                if actual_crs and actual_crs != spec.crs_authid:
                    # KML / GPX 只认 WGS 84：文件里就是 4326，别让用户以为选了 4490 就存成 4490
                    send(
                        "warning",
                        f"{spec.data_format} 只认 {actual_crs}，图层坐标系已按 {actual_crs} 记录",
                        duration=5,
                    )
                self._report_field_persistence(layer, spec.fields, in_db=False)
                self._persist_file_dicts(spec, layer)
        except (CreateDataError, CreateLibraryError) as exc:
            # 目标已存在 / 驱动缺失 / 写盘失败 / 库里已有同名图层：都留在窗口里让用户改参数重试
            logger.warning("创建数据失败：%s", exc)
            self._fail(str(exc), self._focus_widget_for(kind))
            return None
        return self._outcome

    def _build_dict_specs(self, db_path: str):
        """字段表涉及的字典 → 目标库字典表 specs（没有字典需求返回空元组）。

        新口径下字段挂的是**当前库里已有的字典表**（见 :class:`DictPickDialog`）：
        这类字典的类别不在应用级「字典管理」里，但目标库里有表，必须放行 ——
        :func:`build_dict_table_specs` 会优先从目标库的同名 / 同类别字典表取行。
        应用级字典里也没有、目标库里也没有的类别（手填的旧模板）跳过并提示，
        不让它拦住整个创建流程。
        """
        plan = self.fields_page.dict_plan()
        if not plan.domains and not plan.sources and not plan.inline:
            return ()
        service = DictService()
        known_tables = {item.name for item in service.list_categories()}
        local_tables = local_dict_categories(db_path)
        # GDB / GPKG 属性域：类别本来就不在应用级字典里，取值已经随 plan.inline
        # 带进来了，不能按「未知字典表」拦掉
        inline_categories = {item.category for item in plan.inline}
        valid_domains = set()
        for category, domain in plan.domains:
            if (
                category not in known_tables
                and category not in local_tables
                and category not in inline_categories
            ):
                send(
                    "warning",
                    f"字典表「{category}」不在当前库、也不在字典管理里，"
                    f"字段「{domain}」的字典表未落库",
                    duration=6,
                )
                continue
            valid_domains.add((category, domain))
        if not valid_domains and not plan.sources and not plan.inline:
            return ()
        plan = DictPlan(
            domains=frozenset(valid_domains),
            sources=plan.sources,
            inline=plan.inline,
        )
        return tuple(build_dict_table_specs(db_path, plan, service))

    @staticmethod
    def _apply_local_dict_props(layer, fields, dict_specs) -> None:
        """把「字段 → 目标库里实际落成的字典表名」写到图层自定义属性上。"""
        if layer is None or not dict_specs:
            return
        for field in fields:
            table = resolve_local_table(field, dict_specs)
            if table:
                layer.setCustomProperty(DICT_LOCAL_TABLE_PROP + field.name, table)

    def _persist_file_dicts(self, spec, layer) -> None:
        """独立文件如果是 gpkg / sqlite，也把字典表写进去；其他格式明确提示。"""
        plan = self.fields_page.dict_plan()
        if not plan.domains and not plan.sources and not plan.inline:
            return
        suffix = Path(spec.file_path).suffix.lower()
        if suffix not in (".gpkg", ".sqlite", ".db"):
            send(
                "warning",
                f"{spec.data_format} 不能保存字典表；字典关联只记录在当前工程里",
                duration=8,
            )
            return
        try:
            dict_specs = self._build_dict_specs(spec.file_path)
            if not dict_specs:
                return
            db = local_db_util(spec.file_path)
            try:
                if db.kind not in ("gpkg", "spatialite"):
                    raise DictTableError("目标文件不是可写字典表的数据库")
                persist_dict_tables(db, dict_specs)
            finally:
                db.conn.close()
            self._apply_local_dict_props(layer, spec.fields, dict_specs)
            send(
                "info",
                "已保存字典表：" + "、".join(item.table_name for item in dict_specs),
                duration=6,
            )
        except Exception as exc:
            logger.warning("独立文件字典表写入失败：%s", exc, exc_info=True)
            send(
                "warning",
                f"图层已创建，但字典表未能写入（文件可能正被占用）：{exc}",
                duration=8,
            )

    def _report_field_persistence(
        self, layer: QgsVectorLayer, fields: Sequence[FieldSpec], *, in_db: bool
    ) -> None:
        """建完之后如实报出「字段没完全落盘」的部分。

        别小看这一步：有些格式的空文件根本存不下字段结构（GeoJSON / KML / DXF / GPX，
        它们的 schema 是随要素写的），有些格式不存别名、不存 NOT NULL。用户是按着界面
        上的字段表验收的，所以**建出来的和填的不一样就必须说**，不能默默少几列。

        顺带把「数据源存不下、但能落在工程里」的那部分（别名 / 不可为空 / 隐藏 / 字典）
        打在图层对象上 —— 见
        :func:`~app.core.domain.create_data.fields.configure_created_layer`。
        """
        for note in configure_created_layer(layer, fields, in_db=in_db):
            logger.info("字段落盘提示：%s", note)
            send("warning", note, duration=8)

    @staticmethod
    def _describe_outcome(outcome: CreateOutcome) -> str:
        """「应用」成功后回显一句「建了什么」。"""
        if outcome.kind == "container":
            return outcome.container_path
        layer = outcome.layer
        if layer is None:
            return ""
        if outcome.kind == "into_db":
            return f"{layer.name()}（写进 {Path(outcome.db_path).name}）"
        return layer.source()

    def _focus_widget_for(self, kind: str) -> QWidget:
        """创建失败时该聚焦哪个控件（三种形态各有一个「最可能填错」的项）。

        向导停在字段那一步时，第 1 步的控件都在隐藏页里 —— 聚焦过去用户也看不见，
        所以按当前步骤指位置。
        """
        if kind != "container" and self._step >= 2:
            return self.fields_page
        if kind == "container":
            return self.db_file_name_edit
        if kind == "into_db":
            return self.into_layer_name_edit
        return self.file_name_edit

    def outcome(self) -> CreateOutcome | None:
        """「确定」建出来的东西（未加入工程 / 未登记）；没建成为 ``None``。"""
        return self._outcome

    def created_layer(self) -> QgsVectorLayer | None:
        """「确定」建出来的图层（已落盘、尚未加入工程）；建空库 / 没建成为 ``None``。"""
        return self._outcome.layer if self._outcome is not None else None

    def _fail(self, message: str, widget: QWidget | None) -> None:
        """把问题留在窗口里：底部提示 + 消息条 + 聚焦到要改的控件。"""
        self.hint.setText(message)
        send("warning", message, duration=4)
        if widget is not None:
            widget.setFocus()


__all__ = [
    "CreateDataDialog",
    "CreateDataSpec",
    "CreateOutcome",
    "CrsComboBox",
    "FieldsStepPage",
]
