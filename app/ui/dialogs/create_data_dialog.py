"""创建数据 Dialog（数据管理 Dock「创建数据」入口）。

表单字段与需求一一对应：

1. **创建路径** —— 数据文件要写入的目录，右挂「浏览…」按钮；
2. **数据文件名称** —— 主名 +（可选的）扩展名，如 ``新建数据`` 或 ``新建数据.gpkg``；
3. **数据格式** —— 下拉：GeoPackage / Shapefile / GeoJSON / KML / GML / FileGDB /
   SQLite / MapInfo TAB / DXF / CSV / FlatGeobuf / GPX，取自
   :data:`app.core.util.vector_formats.VECTOR_FORMATS`（显示名 + OGR 驱动名 +
   默认扩展名三件套，建图层时驱动名直接给 ``QgsVectorFileWriter``）；
4. **图层名** —— 结果图层在图层面板与数据源里的名字；
5. **几何类型** —— 下拉：点 / 线 / 面 / 多点 / 多线 / 多面，取值是 QGIS 的
   几何类型名（``Point`` / ``LineString`` / ``Polygon`` / ``Multi*``），
   建图层时可直接拼 ``"<几何类型>?crs=..."``；
6. **坐标系** —— **可搜索下拉框**：默认取配置的 ``app.crs``（``config/app.yaml``，
   默认 ``EPSG:4490`` / CGCS2000；由调用方经 ``default_crs`` 注入，见
   :data:`_CRS_FALLBACK`），输入 ``4490`` / ``EPSG:4490`` /
   ``cgcs2000`` 即时过滤候选。候选来自 QGIS 自带的 ``srs.db``
   （:func:`QgsApplication.srsDatabaseFilePath`，EPSG 非废弃项约 7200 条），
   读不到时退回 :data:`_FALLBACK_CRS`，裁剪过的分发包（无 srs.db）也能用。

**「数据格式」与「数据文件名称」的分工**（两者都会决定扩展名，规则要唯一）：

- **格式下拉是权威**：扩展名由它补/换 —— 不填扩展名照样能创建，
  :meth:`CreateDataDialog.data` 给出的 ``file_name`` 带正确扩展名；
- **认得出的扩展名反向切格式**：文件名写 ``地块.shp`` 会把下拉切成 Shapefile
  （见 :meth:`CreateDataDialog._sync_format_from_name`），两个控件不会互相打脸；
- 认不出的扩展名（如 ``.txt``）不报错，按格式下拉的扩展名收口，
  底部提示会实时回显最终文件名，用户看得见。

**职责边界**：本对话框只做「收集 + 校验 + 取值 + 触发创建」——「确定」先过
:meth:`CreateDataDialog._validate`，通过后交给
:class:`app.core.domain.create_data.CreateDataService` 落盘；服务失败就把原因留在
窗口里、不关窗。建出来的图层经 :meth:`CreateDataDialog.created_layer` 交给调用方，
本对话框自己不往工程里加图层（UI 不写 IO / 算法细节，见编码规范 §10）。

**目标已存在一律拦下**（不覆盖、也不往既有数据源里追加图层）：覆盖会毁掉既有数据，
追加则属于「改既有数据源」而不是「创建」，两种情况都由创建服务在写盘前拦下，
报错文案直接显示在窗口里。
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from qgis.core import QgsApplication, QgsCoordinateReferenceSystem, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QCompleter,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.create_data import (
    CreateDataError,
    CreateDataService,
    CreateDataSpec,
    available_drivers,
)
from app.core.util.vector_formats import VECTOR_FORMATS, VectorFormat, format_for_extension
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import open_file_dir

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

#: 文件名称 / 图层名里不允许出现的字符（Windows 文件名与 gpkg 表名的共同约束）
_ILLEGAL_NAME_CHARS = '\\/:*?"<>|'

#: 下拉项里 authid 与名称的分隔符（:meth:`CrsComboBox.authid` 按它切回 authid）
_CRS_SEP = " — "

#: ``authid`` 形态校验（``EPSG:4490`` / ``ESRI:102100`` …）
_AUTHID_RE = re.compile(r"[A-Za-z]+:\d+")

#: 调用方没注入默认坐标系时的兜底值（CGCS2000 / 中国 2000）。正常路径下默认值
#: 来自配置：``config/app.yaml`` 的 ``app.crs``，由 :class:`MainWindow` 注入 ——
#: 本对话框不自己读配置文件（UI 不写 IO，见模块头「职责边界」）。
_CRS_FALLBACK = "EPSG:4490"

#: srs.db 读不到时的兜底候选（全球通用 + 国内常用，描述与 srs.db 保持一致）
_FALLBACK_CRS: tuple[tuple[str, str], ...] = (
    ("EPSG:4490", "China Geodetic Coordinate System 2000"),
    ("EPSG:4326", "WGS 84"),
    ("EPSG:3857", "WGS 84 / Pseudo-Mercator"),
    ("EPSG:4610", "Xian 1980"),
    ("EPSG:4214", "Beijing 1954"),
    ("EPSG:4491", "CGCS2000 / Gauss-Kruger zone 13"),
    ("EPSG:4508", "CGCS2000 / Gauss-Kruger CM 111E"),
    ("EPSG:4534", "CGCS2000 / 3-degree Gauss-Kruger CM 75E"),
    ("EPSG:4547", "CGCS2000 / 3-degree Gauss-Kruger CM 114E"),
    ("EPSG:32650", "WGS 84 / UTM zone 50N"),
)

#: 坐标系候选缓存：srs.db 七千条读一次就够（对话框可能被反复打开）
_EPSG_CACHE: list[tuple[str, str]] | None = None


def _read_srs_db() -> list[tuple[str, str]]:
    """读 QGIS 自带 ``srs.db`` 的 ``tbl_srs``，返回 ``[(authid, 描述)]``。

    只取 ``auth_name='EPSG'`` 且未废弃的行：其它授权名（ESRI / IGNF / IAU_2015）
    对「EPSG 代码搜索」没有帮助，反而把候选撑到 1.4 万条。

    读不到（文件缺失 / 只读安装目录 / 表结构不符）时返回空表，由调用方走
    :data:`_FALLBACK_CRS` —— 打开对话框这件事不该被坐标系库绊住。
    """
    db_path = Path(QgsApplication.srsDatabaseFilePath())
    if not db_path.is_file():
        logger.warning("未找到 QGIS 坐标系库 %s，坐标系下拉使用内置候选", db_path)
        return []
    try:
        # mode=ro：QGIS 安装目录可能只读，纯读也不需要写权限 / 不留 -journal
        con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT auth_id, description FROM tbl_srs "
                "WHERE auth_name = 'EPSG' AND deprecated = 0 "
                "ORDER BY CAST(auth_id AS INTEGER)"
            ).fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("读取坐标系库 %s 失败，坐标系下拉使用内置候选：%s", db_path, exc)
        return []
    return [(f"EPSG:{auth_id}", description or "") for auth_id, description in rows if auth_id]


def _epsg_entries() -> list[tuple[str, str]]:
    """坐标系候选表（带进程级缓存）：优先 srs.db，失败退回内置候选。"""
    global _EPSG_CACHE
    if _EPSG_CACHE is None:
        _EPSG_CACHE = _read_srs_db() or list(_FALLBACK_CRS)
    return _EPSG_CACHE


def _illegal_chars(name: str) -> str:
    """返回名字里出现的非法字符（去重、按 :data:`_ILLEGAL_NAME_CHARS` 顺序），便于提示。"""
    return "".join(dict.fromkeys(ch for ch in name if ch in _ILLEGAL_NAME_CHARS))


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
        self.addItems([f"{authid}{_CRS_SEP}{name}" for authid, name in _epsg_entries()])
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

    def set_crs(self, authid: str) -> None:
        """把当前选中项置为 ``authid``；候选里没有时直接写进文本框。"""
        if not authid:
            return
        index = self.findText(f"{authid}{_CRS_SEP}", Qt.MatchFlag.MatchStartsWith)
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
        head = text.split(_CRS_SEP, 1)[0].strip()
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


class CreateDataDialog(BaseDialog):
    """创建数据 Dialog（收集参数 + 校验 + 触发创建，入工程交给调用方）。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        title: str = "创建数据",
        *,
        default_crs: str = "",
    ) -> None:
        super().__init__(parent, title)
        #: 「确定」成功建出来的图层（未加入工程），见 :meth:`created_layer`
        self._created_layer: QgsVectorLayer | None = None
        #: 坐标系下拉的默认值。调用方（MainWindow）按 ``config/app.yaml`` 的
        #: ``app.crs`` 注入；未注入时兜底 :data:`_CRS_FALLBACK`
        self._default_crs = default_crs or _CRS_FALLBACK
        # BaseDialog 默认 800x600 对 6 行表单偏大，收敛到内容尺寸（同 CrsSelectDialog）
        self.setMinimumSize(560, 0)
        self.resize(660, 340)
        self._init_ui()

    # --------------------------------------------------------------- 构建

    def _init_ui(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        head = QLabel("新建数据文件与图层")
        head.setObjectName("SectionLabel")
        root.addWidget(head)

        # 实时校验提示先建出来：下面的控件一连上信号就会回调 _refresh_hint，
        # 那时 self.hint 必须已经存在（布局里插在表单之后，见下方 addWidget）
        self.hint = QLabel()
        self.hint.setObjectName("PropHint")
        self.hint.setWordWrap(True)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setSpacing(10)
        self._add_path_row(form)
        self._add_file_name_row(form)
        self._add_format_row(form)
        self._add_layer_name_row(form)
        self._add_geometry_row(form)
        self._add_crs_row(form)
        root.addLayout(form)

        # 实时校验提示：有问题显示问题，全通过则回显将要创建的参数（顺手确认
        # 「坐标系是否解析成功」——这是本表单最容易被输入错的一项）
        root.addWidget(self.hint)

        root.addStretch(1)
        root.addLayout(self._build_footer())
        self.setLayout(root)
        self._refresh_hint()

    def _add_path_row(self, form: QFormLayout) -> None:
        """1. 创建路径：输入框 + 「浏览…」目录选择。"""
        self.path_edit = QLineEdit(QgsProject.instance().homePath())
        self.path_edit.setPlaceholderText("选择数据文件要写入的目录")
        self.path_edit.textChanged.connect(self._refresh_hint)
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self._on_browse_path)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse_btn)
        form.addRow("创建路径", row)

    def _add_file_name_row(self, form: QFormLayout) -> None:
        """2. 数据文件名称：主名 +（可选）扩展名，扩展名以「数据格式」为准。"""
        self.file_name_edit = QLineEdit()
        self.file_name_edit.textChanged.connect(self._refresh_hint)
        # 编辑完（失焦 / 回车）后再做两件「少打一次字」的事：用去扩展名的文件名
        # 预填图层名、按手打的扩展名切换格式下拉（见 _on_file_name_edited）
        self.file_name_edit.editingFinished.connect(self._on_file_name_edited)
        form.addRow("数据文件名称", self.file_name_edit)

    def _add_format_row(self, form: QFormLayout) -> None:
        """3. 数据格式：显示名进下拉，itemData 直接挂 :class:`VectorFormat`。"""
        self.format_combo = QComboBox()
        # 本机没有对应 OGR 驱动的格式（如便携包里没带 FileGDB）直接置灰：
        # 灰掉的项用户点不了，比让他点了再吃一个报错好
        writers = available_drivers()
        for index, fmt in enumerate(VECTOR_FORMATS):
            self.format_combo.addItem(fmt.label, fmt)
            notes = [fmt.note] if fmt.note else []
            if fmt.driver not in writers:
                notes.append(f"本机 QGIS 没有 {fmt.driver} 驱动，建不了这个格式")
                self._disable_format(index)
            if notes:
                # 格式限制（如 Shapefile 字段名 ≤10 字符）放 tooltip，不占界面
                self.format_combo.setItemData(index, "；".join(notes), Qt.ItemDataRole.ToolTipRole)
        self.format_combo.setCurrentIndex(0)
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        form.addRow("数据格式", self.format_combo)
        # 建完立刻同步一次占位提示（默认格式的扩展名）
        self._on_format_changed()

    def _disable_format(self, index: int) -> None:
        """置灰下拉项（QComboBox 默认模型就是 QStandardItemModel，取 item 改标志）。"""
        model = self.format_combo.model()
        if isinstance(model, QStandardItemModel):
            item = model.item(index)
            if item is not None:
                item.setEnabled(False)

    def _add_layer_name_row(self, form: QFormLayout) -> None:
        """4. 图层名。"""
        self.layer_name_edit = QLineEdit()
        self.layer_name_edit.setPlaceholderText("图层在工程与数据源里显示的名字")
        self.layer_name_edit.textChanged.connect(self._refresh_hint)
        form.addRow("图层名", self.layer_name_edit)

    def _add_geometry_row(self, form: QFormLayout) -> None:
        """5. 几何类型：中文名显示、QGIS 几何类型名作 itemData。"""
        self.geometry_combo = QComboBox()
        for label, geometry_type in _GEOMETRY_TYPES:
            self.geometry_combo.addItem(label, geometry_type)
        self.geometry_combo.setCurrentIndex(0)
        form.addRow("几何类型", self.geometry_combo)

    def _add_crs_row(self, form: QFormLayout) -> None:
        """6. 坐标系：可搜索下拉框，默认 :attr:`_default_crs`（配置的 ``app.crs``）。"""
        self.crs_combo = CrsComboBox()
        self.crs_combo.editTextChanged.connect(self._refresh_hint)
        self.crs_combo.set_crs(self._default_crs)
        form.addRow("坐标系", self.crs_combo)

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        row.addWidget(cancel_btn)
        self.ok_btn = QPushButton("确定")
        self.ok_btn.setObjectName("DialogPrimary")
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self.accept)
        row.addWidget(self.ok_btn)
        return row

    # --------------------------------------------------------------- 交互

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

    def _current_format(self) -> VectorFormat:
        """当前数据格式（下拉项 itemData 就是 :class:`VectorFormat`）。"""
        fmt = self.format_combo.currentData()
        return fmt if isinstance(fmt, VectorFormat) else VECTOR_FORMATS[0]

    def _on_format_changed(self, *_args: object) -> None:
        """切数据格式：占位提示跟上，并把文件名扩展名换成新格式的。"""
        fmt = self._current_format()
        self.file_name_edit.setPlaceholderText(f"如 新建数据{fmt.extension}（扩展名可不填，按数据格式补）")
        self._rewrite_file_name_extension(fmt)

    def _rewrite_file_name_extension(self, fmt: VectorFormat) -> None:
        """把文件名扩展名换成 ``fmt`` 的（保留主名）；空文件名不动。"""
        raw = self.file_name_edit.text().strip()
        if not raw:
            return
        normalized = f"{os.path.splitext(raw)[0]}{fmt.extension}"
        if normalized != self.file_name_edit.text():
            self.file_name_edit.setText(normalized)

    def _sync_format_from_name(self) -> None:
        """文件名里认得出的扩展名优先（``地块.shp`` → 格式下拉切成 Shapefile）。

        格式下拉改扩展名是正向同步（:meth:`_on_format_changed`），这里兜住反
        过来的路径：用户直接手打扩展名。两条方向合起来，两个控件不可能停在
        互相矛盾的组合上。认不出的扩展名（``.txt``）不动格式，由下拉收口。
        """
        fmt = format_for_extension(os.path.splitext(self.file_name_edit.text().strip())[1])
        if fmt is None or fmt.driver == self._current_format().driver:
            return
        for index, candidate in enumerate(VECTOR_FORMATS):
            if candidate.driver == fmt.driver:
                self.format_combo.setCurrentIndex(index)
                return

    def _normalized_file_name(self) -> str:
        """按当前格式补/换扩展名的文件名（「数据格式」是唯一权威）。"""
        raw = self.file_name_edit.text().strip()
        if not raw:
            return ""
        return f"{os.path.splitext(raw)[0]}{self._current_format().extension}"

    def _on_file_name_edited(self) -> None:
        """数据文件名称编辑完：按扩展名切格式，并给空的图层名预填主名。"""
        self._sync_format_from_name()
        if self.layer_name_edit.text().strip():
            return
        stem = os.path.splitext(self.file_name_edit.text().strip())[0]
        if stem:
            self.layer_name_edit.setText(stem)

    def _refresh_hint(self, *_args: object) -> None:
        """刷新底部提示（接在各字段的变更信号上，空参兼容各信号签名）。"""
        # 先让格式下拉与文件名对齐，再校验 —— 提示里回显的才是最终会创建的名字
        self._sync_format_from_name()
        error, _widget = self._validate()
        if error:
            self.hint.setText(error)
            return
        spec = self.data()
        fmt_txt = f"{spec.data_format}（{spec.extension}）"
        self.hint.setText(
            f"将创建：{spec.file_path} · {fmt_txt} · {self.geometry_combo.currentText()} · {spec.crs_authid}"
        )

    # --------------------------------------------------------------- 取值

    def _validate(self) -> tuple[str, QWidget | None]:
        """校验表单；返回 ``(错误文案, 需要聚焦的控件)``，无错误时文案为空串。"""
        path = self.path_edit.text().strip()
        if not path:
            return "请填写创建路径", self.path_edit
        path = os.path.normpath(path)
        if not os.path.isdir(path):
            return f"创建路径不存在或不是目录：{path}", self.path_edit
        if not os.access(path, os.W_OK):
            return f"创建路径不可写：{path}", self.path_edit

        file_name = self.file_name_edit.text().strip()
        if not file_name:
            return "请填写数据文件名称", self.file_name_edit
        bad = _illegal_chars(file_name)
        if bad:
            return f"数据文件名称不能包含字符：{bad}", self.file_name_edit
        # 扩展名不在这里卡：漏填由「数据格式」补，写错由 data() 按格式换掉

        layer_name = self.layer_name_edit.text().strip()
        if not layer_name:
            return "请填写图层名", self.layer_name_edit
        bad = _illegal_chars(layer_name)
        if bad:
            return f"图层名不能包含字符：{bad}", self.layer_name_edit

        if not self.crs_combo.crs().isValid():
            return "坐标系无效：请输入 EPSG 代码（如 4490）或从下拉候选里选一项", self.crs_combo
        return "", None

    def data(self) -> CreateDataSpec:
        """取当前表单内容（「确定」前也可调，用于回显 / 预览）。"""
        path = self.path_edit.text().strip()
        crs = self.crs_combo.crs()
        is_valid_crs = crs.isValid()
        fmt = self._current_format()
        return CreateDataSpec(
            path=os.path.normpath(path) if path else "",
            file_name=self._normalized_file_name(),
            data_format=fmt.label,
            driver=fmt.driver,
            extension=fmt.extension,
            layer_name=self.layer_name_edit.text().strip(),
            geometry_type=self.geometry_combo.currentData() or "",
            crs_authid=crs.authid() if is_valid_crs else "",
            crs_description=crs.description() if is_valid_crs else "",
        )

    def accept(self) -> None:
        """「确定」的硬闸：校验不过或建不出来就留在窗口里，把问题指给用户。"""
        self._sync_format_from_name()
        error, widget = self._validate()
        if error:
            self._fail(error, widget)
            return
        spec = self.data()
        try:
            layer = CreateDataService().create(spec)
        except CreateDataError as exc:
            # 目标已存在 / 驱动缺失 / 写盘失败：都留在窗口里让用户改参数重试
            logger.warning("创建数据失败：%s", exc)
            self._fail(str(exc), self.file_name_edit)
            return
        self._created_layer = layer
        actual_crs = layer.crs().authid()
        if actual_crs and actual_crs != spec.crs_authid:
            # KML / GPX 只认 WGS 84：文件里就是 4326，别让用户以为选了 4490 就存成 4490
            send(
                "warning",
                f"{spec.data_format} 只认 {actual_crs}，图层坐标系已按 {actual_crs} 记录",
                duration=5,
            )
        super().accept()

    def created_layer(self) -> QgsVectorLayer | None:
        """「确定」建出来的图层（已落盘、尚未加入工程）；没建成为 ``None``。"""
        return self._created_layer

    def _fail(self, message: str, widget: QWidget | None) -> None:
        """把问题留在窗口里：底部提示 + 消息条 + 聚焦到要改的控件。"""
        self.hint.setText(message)
        send("warning", message, duration=4)
        if widget is not None:
            widget.setFocus()


__all__ = ["CreateDataDialog", "CreateDataSpec", "CrsComboBox"]
