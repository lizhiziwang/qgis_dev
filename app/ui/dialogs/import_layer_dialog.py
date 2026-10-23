"""「导入图层到数据库」Dialog：数据管理 Dock 右键 / 头部「导入」弹窗。

弹窗内容（按用户原话）：

1. **待导入图层**：**一开始是空列表**；两个入口分别挑「工程图层」/「本地文件」
   （GDB / GeoPackage / SQLite 会把里面的图层全列出来，可多选），选中的图层
   直接进列表。列表里可选中后用「删除选中」去掉。
   **不再问目标坐标系**：每份数据按**自己的坐标系**入库（多份数据可以各带各的
   CRS，GPKG / SpatiaLite 每张表各存各的 SRS）。
2. **数据库下拉**：合并本地库（``kind=local``）与服务器库（``kind=server``），
   顺序即调用方给的顺序（数据管理的列表顺序：新的在上），显示名复用
   :func:`DataManageDock._db_title` / :meth:`_server_title` 的逻辑
   （避免一处显示规则两份），工具提示展示连接元信息。**默认选中
   ``default_db``** —— 调用方传「数据管理里当前选中的那个库」，点「导入」时
   不用再挑一遍（见 ``AppActionsMixin._on_import``）。
   从**左侧库节点**右键进来时调用方传 ``lock_db=True``：目标库锁定为那个库，
   不能再切（用户明确要的）；从右侧面板的「导入」按钮进来则可自由选。

不 import :mod:`qgis.core` 之外的 IO / 持久化 / 落盘逻辑——本模块只做"收集图层
+ 校验非空"。落库 + 注册 ``user_relation`` 由 :func:`app.core.domain.import_layer
.import_vector_layer_to_db` 负责（重名自动改序号也在调用方用
:func:`~app.core.domain.import_layer.unique_table_name` 算好传进去），
调用方在 ``accept()`` 之后调。

设计取舍：

- 把 dialog 限制为**收集参数**而非"含导入流程"，是因为不同 db_kind（local /
  server）走不同的 helper，dialog 不知道也不该知道。本 dialog 与
  :class:`AddConnectionDialog` 同型：collect-only，行为在 mixin。
- 没选图层 / 没选数据库 → 禁用「确定」按钮（早期失败，避免在「空数据」上点
  确认再让 helper 抛错；与 :class:`AddConnectionDialog` 同样友好的 disable 模式）。
"""

from __future__ import annotations

import os
from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.common.base_dialog import BaseDialog


class ImportLayerDialog(BaseDialog):
    """数据管理「导入」弹窗：选要导入的图层 + 选目标库。"""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        vector_layers: list,
        databases: list[dict],
        default_db: Optional[dict] = None,
        lock_db: bool = False,
    ) -> None:
        """
        Args:
            parent: 父窗口。
            vector_layers: 工程里已加载的矢量图层（选择器里可挑；列表本身从空开始）。
            databases: 可用的目标库（本地 / 服务器）。
            default_db: 默认选中的目标库。
            lock_db: ``True`` = 目标库锁定（从左侧库节点右键进来），不能切换。
        """
        super().__init__(parent, "导入图层到数据库")
        self._vector_layers = list(vector_layers)
        self._databases = list(databases)
        #: 目标数据库下拉的默认选中项：调用方传「数据管理里当前选中的那个库」，
        #: 用户点「导入」时就不用再挑一遍（见 ``AppActionsMixin._on_import``）。
        self._default_db = default_db
        #: 目标库是否锁定（左侧库节点右键进来时为 True：用户点的是那个库）
        self._lock_db = bool(lock_db)
        #: **强引用**列表里的图层。Qt 的 ``QListWidgetItem.setData`` 只存一个
        #: QObject 指针，**不持有 Python 包装对象的引用**：局部变量 / 上游列表一释放，
        #: sip 包装对象就被回收，item.data() 再取出来只剩一个裸 ``QObject``
        #: （`.name()` → AttributeError）。从本地文件（GDB / gpkg）选进来的图层
        #: 没有任何别处持有它，用户点「导入」时就死在这里 —— 既不落库也没提示
        #: （用户报的"选 GDB 图层点导入没反应"）。这里由弹窗自己保活到对话框销毁。
        self._layer_refs: list = []
        self.setMinimumSize(0, 0)
        self.resize(520, 320)
        self._build()

    # ------------------------------------------------------------------ UI

    def _build(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(8)

        # 1. 待导入图层：**一开始是空列表**；两个入口分别挑工程图层 / 本地文件，
        #    容器数据源会列出里面的图层、可多选；选中的直接进列表；可选中后删除
        self.layer_list = QListWidget()
        #: QSS 作用域锚点：选中色由主题里 #ImportLayerList 规则加深
        #: （全局 toolbar_hover 太浅，用户会以为没选上）
        self.layer_list.setObjectName("ImportLayerList")
        self.layer_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.layer_list.setMinimumHeight(120)
        self.layer_list.itemSelectionChanged.connect(self._refresh_ok_enabled)
        layer_row = QHBoxLayout()
        layer_row.setContentsMargins(0, 0, 0, 0)
        layer_row.setSpacing(6)
        layer_row.addWidget(self.layer_list, 1)
        side = QVBoxLayout()
        side.setSpacing(4)
        self.pick_project_btn = QPushButton("从工程图层…")
        self.pick_project_btn.setToolTip("从当前工程已加载的矢量图层里选（可多选）")
        self.pick_project_btn.clicked.connect(self._on_pick_project_layers)
        side.addWidget(self.pick_project_btn)
        self.pick_file_btn = QPushButton("从本地文件…")
        self.pick_file_btn.setToolTip(
            "直接打开「添加数据」：GDB / GeoPackage / SQLite 可就地展开选图层，"
            "选完直接进待导入列表（可按住 Ctrl / Shift 多选）"
        )
        self.pick_file_btn.clicked.connect(self._on_pick_local_layers)
        side.addWidget(self.pick_file_btn)
        self.remove_btn = QPushButton("删除选中")
        self.remove_btn.setToolTip("把选中的条目从待导入列表里去掉（不影响任何数据）")
        self.remove_btn.clicked.connect(self._on_remove_selected)
        side.addWidget(self.remove_btn)
        side.addStretch(1)
        layer_row.addLayout(side)
        form.addRow("待导入图层：", layer_row)

        # 2. 数据库下拉
        self.db_combo = QComboBox()
        if not self._databases:
            self.db_combo.addItem("（无本地/服务器数据库）", userData=None)
            self.db_combo.setEnabled(False)
        else:
            for db in self._databases:
                self.db_combo.addItem(_format_db_label(db), userData=db)
                tip = _format_db_tooltip(db)
                if tip:
                    self.db_combo.setItemData(
                        self.db_combo.count() - 1, tip, Qt.ItemDataRole.ToolTipRole
                    )
            # 默认选中调用方指定的那个库（数据管理里当前选中的）：库 dict 经 Qt 的
            # itemData 出来是**副本**，按身份（本地=路径 / 服务器=类型+主机+库名）认。
            if self._default_db is not None:
                want = _db_identity(self._default_db)
                for i in range(self.db_combo.count()):
                    data = self.db_combo.itemData(i)
                    if isinstance(data, dict) and _db_identity(data) == want:
                        self.db_combo.setCurrentIndex(i)
                        break
            if self._lock_db:
                # 从左侧库节点右键进来：用户点的是那个库，锁死不让切（用户明确要的）
                self.db_combo.setEnabled(False)
                self.db_combo.setToolTip("从左侧库节点进入导入：目标库已锁定")
        form.addRow("目标数据库：", self.db_combo)

        # 说明
        hint = QLabel(
            "每份数据按自己的坐标系入库；目标库里已有同名图层时自动改名（名称_1、名称_2…）。"
        )
        hint.setObjectName("ImportLayerDialogHint")
        hint.setWordWrap(True)
        form.addRow("", hint)

        layout.addLayout(form)

        # OK / Cancel
        self.ok_btn = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_btn.accepted.connect(self._on_accept)
        self.ok_btn.rejected.connect(self.reject)
        layout.addWidget(self.ok_btn)

        self.setLayout(layout)
        self._refresh_ok_enabled()

    # ------------------------------------------------------------- 事件

    def _on_pick_project_layers(self) -> None:
        """「从工程图层…」：只列工程里的矢量图层，多选后追加进待导入列表。"""
        from app.ui.dialogs.create_data_fields_step import ExternalDatasetDialog

        dialog = ExternalDatasetDialog(
            self,
            vector_layers=self._vector_layers,
            multi=True,
            show_local=False,
        )
        accepted = dialog.exec()
        layers, error = dialog.resolve_all() if accepted else ([], "")
        dialog.deleteLater()
        if error:
            QMessageBox.warning(self, "选择数据源", error)
        for layer in layers:
            self._add_layer(layer)
        self._refresh_ok_enabled()

    def _on_pick_local_layers(self) -> None:
        """「从本地文件…」：直接打开「添加数据」，选中图层直接进待导入列表。

        不再经过「选择外部数据集」中间窗口：选完 / 取消都只进出一次，
        避免用户为了选一个本地图层先后开三层弹窗。
        """
        from app.ui.dialogs.add_data_dialog import AddDataDialog

        picker = AddDataDialog(self)
        accepted = picker.exec()
        selections = picker.selected_sources() if accepted else []
        picker.deleteLater()
        if not selections:
            return
        layers, errors = self._open_selections(selections)
        for layer in layers:
            self._add_layer(layer)
        if errors:
            QMessageBox.warning(
                self,
                "选择数据源",
                "以下数据没有导入：\n" + "\n".join(errors),
            )
        self._refresh_ok_enabled()

    @staticmethod
    def _open_selections(selections) -> tuple[list[object], list[str]]:
        """把「添加数据」的选择展开成图层对象；返回 ``(图层, 失败原因)``。"""
        from app.core.util.sublayers import list_sublayers, open_sublayer

        layers: list[object] = []
        errors: list[str] = []
        for selection in selections:
            path = str(getattr(selection, "path", "") or "")
            sublayer = getattr(selection, "sublayer", None)
            if not path:
                continue
            candidates = [sublayer] if sublayer is not None else list_sublayers(path)
            if not candidates:
                # 普通矢量文件（shp / geojson 等）枚举不到子图层时直开
                from qgis.core import QgsVectorLayer

                layer = QgsVectorLayer(path, os.path.basename(path), "ogr")
                if layer.isValid():
                    layers.append(layer)
                else:
                    errors.append(f"{os.path.basename(path)}：打不开")
                continue
            for candidate in candidates:
                layer, error = open_sublayer(path, candidate)
                if layer is not None:
                    layers.append(layer)
                elif error:
                    errors.append(error)
        return layers, errors

    def _add_layer(self, layer) -> bool:
        """把图层追加进列表（同名跳过，避免同一层导两遍）。"""
        for row in range(self.layer_list.count()):
            existing = self.layer_list.item(row).data(Qt.ItemDataRole.UserRole)
            try:
                same = existing is layer or (
                    existing is not None and existing.name() == layer.name()
                )
            except (AttributeError, RuntimeError):
                # 旧条目里的图层已被回收（见 __init__ 里 _layer_refs 的说明）
                same = False
            if same:
                return False
        # 列表里带上**来源文件**，用户一眼能看出这个条目是从哪个数据文件选的
        source_name = _source_file_name(layer)
        text = f"{layer.name()}（{source_name}）" if source_name else layer.name()
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, layer)
        item.setToolTip(layer.source())
        self.layer_list.addItem(item)
        # **保活**：item data 只存 QObject 指针，不持有 Python 包装对象（见 __init__）
        self._layer_refs.append(layer)
        item.setSelected(True)
        return True

    def _on_remove_selected(self) -> None:
        """「删除选中」：把选中的条目从待导入列表里去掉（只动列表，不动数据）。"""
        for item in self.layer_list.selectedItems():
            layer = item.data(Qt.ItemDataRole.UserRole)
            # 同步放掉强引用，避免关窗前一直占着图层 / 文件句柄
            for index, ref in enumerate(self._layer_refs):
                if ref is layer:
                    del self._layer_refs[index]
                    break
            self.layer_list.takeItem(self.layer_list.row(item))
        self._refresh_ok_enabled()

    def _on_accept(self) -> None:
        if not self.selected_layers() or self.db_combo.currentData() is None:
            QMessageBox.warning(self, "导入图层", "请先选择要导入的图层与目标数据库")
            return
        self.accept()

    def _refresh_ok_enabled(self) -> None:
        """没选图层 / 没选库 → 禁 OK（早期失败）。"""
        can = bool(self.selected_layers()) and self.db_combo.currentData() is not None
        ok = self.ok_btn.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(can)

    # ------------------------------------------------------------- 结果

    def selected_layers(self) -> list[object]:
        """列表里**选中**的图层（可能多个）。

        取不到有效图层的条目直接跳过：Qt 的 item data 只存 QObject 指针，万一
        上游把图层回收了，这里拿到的会是裸 ``QObject`` —— 交给调用方只会在
        ``layer.name()`` 处崩掉（历史上就是"点导入没反应、也没提示"的来源）。
        """
        out: list[object] = []
        for item in self.layer_list.selectedItems():
            layer = item.data(Qt.ItemDataRole.UserRole)
            if layer is None:
                continue
            try:
                layer.name()
            except (AttributeError, RuntimeError):
                continue
            out.append(layer)
        return out

    def result_payload(self) -> tuple[list[object], dict]:
        """``(layers, db)`` —— ``layers`` 是待导入列表里选中的全部图层。

        **不再返回目标坐标系**：每份数据按自己的坐标系入库（见模块头）。
        """
        return self.selected_layers(), self.db_combo.currentData()


def _source_file_name(layer) -> str:
    """图层的来源文件名（本地库取容器文件名，单文件取文件名）；不是文件来源返回空串。

    只用来在列表里给个提示（"这个条目是从哪个数据文件选的"），所以**只认真实存在的
    文件 / 目录**：内存图层（source 是 ``Point?crs=…&field=…`` 这种 URI）没有文件可言，
    不能把一整串 URI 塞进列表（实测会显示成一长串参数）。
    """
    try:
        source = str(layer.source() or "")
    except Exception:  # noqa: BLE001 —— 图层已销毁
        return ""
    container = source.split("|", 1)[0]
    # SpatiaLite 的 source 是 `dbname='…' table="…" (geom)` 这种，取 dbname
    marker = "dbname='"
    if marker in container:
        container = container.split(marker, 1)[1].split("'", 1)[0]
    container = container.strip()
    if not container or not (os.path.isfile(container) or os.path.isdir(container)):
        return ""
    name = os.path.basename(container.rstrip("/\\"))
    try:
        layer_name = str(layer.name())
    except RuntimeError:
        return ""
    return "" if not name or name == layer_name else name


# --------------------------------------------------------------------- helpers


def _db_identity(db: dict) -> tuple[str, ...]:
    """数据库连接的**身份**（用于在 Qt 的 ``itemData`` 副本里认出同一个库）。

    本地库认路径；服务器连接认「类型 + 主机 + 端口 + 库名 + 名称」这一串（它的
    ``name`` 可能被用户改过，所以不用它单独当身份）。
    """
    if str(db.get("kind") or "") == "server":
        return ("server",) + tuple(
            str(db.get(key) or "")
            for key in ("db_type", "host", "port", "database", "name")
        )
    return ("local", str(db.get("path") or ""))


def _format_db_label(db: dict) -> str:
    """数据库下拉的展示文本：复用 Dock 的命名规则。"""
    kind = db.get("kind", "")
    if kind == "server":
        label = db.get("db_type_label") or db.get("db_type") or "服务器"
        return f"[服务器] {label} - {db.get('name', '?')}"
    if kind == "local":
        return f"[本地] {db.get('name', '?')}"
    return db.get("name", "未命名数据库")


def _format_db_tooltip(db: dict) -> str:
    """数据库下拉的悬停提示：让用户确认库标识。"""
    kind = db.get("kind", "")
    if kind == "server":
        return (
            f"{db.get('db_type_label', db.get('db_type', '?'))} - "
            f"{db.get('host', '?')}:{db.get('port', '?')}/{db.get('database', '?')}"
        )
    if kind == "local":
        path = db.get("path") or ""
        return f"本地库文件：{path}"
    return ""
