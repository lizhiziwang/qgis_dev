"""「导入图层到数据库」Dialog：数据管理 Dock 右键 / 头部「导入」弹窗。

弹窗内容（按用户原话）：

1. **图层下拉**：列出当前工程中所有 :class:`QgsVectorLayer`（按 name 排序）。
2. **数据库下拉**：合并本地库（``kind=local``）与服务器库（``kind=server``），
   显示名复用 :func:`DataManageDock._db_title` / :meth:`_server_title` 的逻辑
   （避免一处显示规则两份），工具提示展示连接元信息。
3. **坐标系下拉**：默认是所选图层的 :meth:`QgsVectorLayer.crs` 的 ``authid``；
   选项里至少含图层 CRS 与一组常用 EPSG（4326 / 4490 / 3857 / 32650 / 32651），
   用 ``setEditable`` 允许键入任意 EPSG（避免工程里没预设目标 CRS 时不能选）。

不 import :mod:`qgis.core` 之外的 IO / 持久化 / 落盘逻辑——本模块只做"收集三个字段
+ 校验非空"。落库 + 注册 ``user_relation`` 由 :func:`app.core.domain.import_layer
.import_vector_layer_to_db` 负责，调用方在 ``accept()`` 之后调。

设计取舍：

- 把 dialog 限制为**收集参数**而非"含导入流程"，是因为不同 db_kind（local /
  server）走不同的 helper，dialog 不知道也不该知道。本 dialog 与
  :class:`AddConnectionDialog` 同型：collect-only，行为在 mixin。
- 坐标系下拉 ``setEditable(True)`` 而不是只允许列表：用户的目标库 CRS 可能不在
  预设里（比如投影带 3 度分带的 117E / 120E），硬限会让 dialog 拒服务。
- 没选矢量图层 / 没选数据库 → 禁用「确定」按钮（早期失败，避免在「空数据」上点
  确认再让 helper 抛错；与 :class:`AddConnectionDialog` 同样友好的 disable 模式）。
"""

from __future__ import annotations

import re
from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from app.ui.common.base_dialog import BaseDialog

#: 坐标系下拉预设：图层 CRS + 一组常用 EPSG。工程里若有自定义 CRS 也由调用方
#: 追加（导入 dialog 拿不到工程对象，CRS 列表在构造期已确定）。
_COMMON_CRS = (
    "EPSG:4326",  # WGS84 经纬度（最常见）
    "EPSG:4490",  # CGCS2000（中国 2000 国家大地坐标系）
    "EPSG:3857",  # Web 墨卡托（在线瓦片主用）
    "EPSG:32650",  # WGS84 / UTM zone 50N
    "EPSG:32651",  # WGS84 / UTM zone 51N
)

#: authid 形式校验：``字母+数字前缀 : 数字``，例 ``EPSG:4326`` / ``ESRI:102100`` /
#: ``POSTGIS:4490`` / ``USER:100001``。空串 / 纯数字 / 缺冒号都拒绝。
_AUTHID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*:\d+$")


class ImportLayerDialog(BaseDialog):
    """数据管理「导入」弹窗：选矢量图层 + 选目标库 + 选目标 CRS。"""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        vector_layers: list,
        databases: list[dict],
    ) -> None:
        super().__init__(parent, "导入图层到数据库")
        self._vector_layers = list(vector_layers)
        self._databases = list(databases)
        # 收集图层 CRS 列表（去重保序）：图层下拉变动时刷新坐标系下拉"默认项"
        self._layer_crs_list: list[str] = []
        seen: set[str] = set()
        for lyr in self._vector_layers:
            try:
                authid = lyr.crs().authid()
            except Exception:  # noqa: BLE001
                continue
            if authid and authid not in seen:
                seen.add(authid)
                self._layer_crs_list.append(authid)
        self.setMinimumSize(0, 0)
        self.resize(440, 220)
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

        # 1. 图层下拉
        self.layer_combo = QComboBox()
        if not self._vector_layers:
            self.layer_combo.addItem("（工程中无矢量图层）", userData=None)
            self.layer_combo.setEnabled(False)
        else:
            # 排序：按 layer.name() 升序（同名保持原顺序——QgsProject.mapLayers 是 dict）
            ordered = sorted(
                enumerate(self._vector_layers),
                key=lambda pair: (pair[1].name() or "", pair[0]),
            )
            for _orig_i, lyr in ordered:
                self.layer_combo.addItem(lyr.name(), userData=lyr)
        self.layer_combo.currentIndexChanged.connect(self._on_layer_changed)
        form.addRow("待导入图层：", self.layer_combo)

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
        form.addRow("目标数据库：", self.db_combo)

        # 3. 坐标系下拉
        self.crs_combo = QComboBox()
        self.crs_combo.setEditable(True)
        # 选项：图层 CRS 在前，常用 EPSG 在后（同 authid 去重保序）
        seen: set[str] = set()
        for authid in self._layer_crs_list + list(_COMMON_CRS):
            if authid and authid not in seen:
                seen.add(authid)
                self.crs_combo.addItem(authid)
        # 默认选中第一个图层的 CRS（layer 选中后由 _on_layer_changed 覆盖）
        if self._vector_layers:
            current = self.layer_combo.currentData()
            if current is not None:
                self._refresh_default_crs(current)
        form.addRow("目标坐标系：", self.crs_combo)

        # 坐标系说明
        hint = QLabel(
            "默认 = 待导入图层的坐标系；下拉为常见 EPSG，可手填任意 EPSG / authid。"
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

    def _on_layer_changed(self, _index: int) -> None:
        layer = self.layer_combo.currentData()
        if layer is None:
            return
        self._refresh_default_crs(layer)

    def _refresh_default_crs(self, layer) -> None:
        """把坐标系下拉的当前项设为所选图层 CRS（若在列表中），否则追加。"""
        try:
            authid = layer.crs().authid()
        except Exception:  # noqa: BLE001
            return
        if not authid:
            return
        idx = self.crs_combo.findText(authid)
        if idx >= 0:
            self.crs_combo.setCurrentIndex(idx)
        else:
            # 插到列表最前（保持"图层 CRS 优先"的视觉顺序）
            self.crs_combo.insertItem(0, authid)
            self.crs_combo.setCurrentIndex(0)

    def _on_accept(self) -> None:
        layer = self.layer_combo.currentData()
        db = self.db_combo.currentData()
        crs = self.crs_combo.currentText().strip()
        if layer is None or db is None:
            QMessageBox.warning(self, "导入图层", "请先选择矢量图层与目标数据库")
            return
        if not crs:
            QMessageBox.warning(self, "导入图层", "目标坐标系不能为空")
            return
        # authid 形式校验（EPSG:xxxx / ESRI:xxx / POSTGIS:xxxx 都接受，但必须
        # 形如 ``前缀:数字``）；通过则继续，否则提示。允许空——helper 会再用
        # ``QgsCoordinateReferenceSystem(authid)`` 兜底校验。
        if not _AUTHID_RE.match(crs):
            QMessageBox.warning(
                self,
                "导入图层",
                f"目标坐标系格式不合法：{crs!r}（应形如 EPSG:4326）",
            )
            return
        self.accept()

    def _refresh_ok_enabled(self) -> None:
        """空图层 / 空库 → 禁 OK（早期失败）。"""
        can = (
            self.layer_combo.currentData() is not None
            and self.db_combo.currentData() is not None
        )
        ok = self.ok_btn.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(can)

    # ------------------------------------------------------------- 结果

    def result_payload(
        self,
    ) -> tuple[object, dict, str]:
        """``(layer, db, target_crs_authid)``。"""
        return (
            self.layer_combo.currentData(),
            self.db_combo.currentData(),
            self.crs_combo.currentText().strip(),
        )


# --------------------------------------------------------------------- helpers


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
