"""「添加自定义底图」Dialog：在线底图 Dock 右键弹窗配置自定义服务。

支持的底图服务类型（写进独立清单文件 ``config/online_basemap_config.yaml`` 的
``type:`` 键）：
- ``xyz`` —— XYZ 瓦片模板（含天地图式 WMTS KVP 模板），可选 referer / 缩放范围；
- ``arcgis`` —— ArcGIS REST MapServer/ImageServer（provider ``arcgismapserver``，
  **动态出图**：连接串带 format/layer/url 参数，不是裸服务 URL）。MapServer
  默认出子图层 0，可填「子图层 ID」指定其他图层（矢量面也由服务端栅格化成图）；
- ``arcgisfeature`` —— ArcGIS REST FeatureServer（provider
  ``arcgisfeatureserver``，**真矢量层**——要素以普通矢量层进工程，「属性」工具
  可识别要素属性）。地址须为开了要素访问的服务、以 /FeatureServer 结尾（MapServer
  服务常带并列的 FeatureServer，把地址尾段换掉即可）；坐标系留空自动从服务识别；
- ``wms`` / ``wmts`` —— OGC 服务（provider ``wms``），图层/样式/格式/CRS
  （+ 瓦片矩阵集）**手填**，不联网抓 GetCapabilities（内网服务发布者已知图名）。

只依赖 ``qgis.PyQt`` + :class:`OnlineBasemapDef`（不 import qgis.core，可离屏
测）。确定后由 MainWindow 走模板同一条持久化路径进 Dock 树 + 写回独立清单文件，
本模块只负责收集字段并组装 :class:`OnlineBasemapDef`。

同模块的 :class:`EditBasemapDialog`（``edit_basemap.py``）子类化本对话框做「编辑
连接」：字段/校验/类型页完全复用，差异只在标题、预填现有条目与额外一行 API key。
"""

from __future__ import annotations

import re
from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.env.config import OnlineBasemapDef
from app.ui.common.base_dialog import BaseDialog

#: 服务类型选项：(显示文本, 内部 type 值)
_SERVICE_OPTIONS = [
    ("XYZ 瓦片", "xyz"),
    ("ArcGIS 影像服务（MapServer/ImageServer）", "arcgis"),
    ("ArcGIS 要素服务（FeatureServer）", "arcgisfeature"),
    ("OGC WMS", "wms"),
    ("OGC WMTS", "wmts"),
]
#: OGC/FeatureServer 连接串参数值里不允许出现的字符（会拆坏 key=value 连接串）
_FORBIDDEN_CHARS = ("&", "=")

_XYZ_HINT = "瓦片地址需包含 {x}/{y}/{z} 占位符（天地图式 wmts 瓦片模板同样适用）"
_ARCGIS_HINT = (
    "服务地址为 ArcGIS REST 服务，以 /MapServer 或 /ImageServer 结尾、不带查询"
    "参数。MapServer 默认出子图层 0（整幅动态出图，矢量面同样适用）；想渲染其它"
    "图层就填该图层在服务里的 ID（ImageServer 不用填）。"
)
_ARCGISFEATURE_HINT = (
    "服务地址为 ArcGIS REST 要素服务，以 /FeatureServer 结尾（如 MapServer 服务开了"
    "要素访问，会有并列的 /FeatureServer 地址——把地址尾段换掉即可）。要素以**真矢量"
    "层**进工程，「属性」工具可识别要素属性。子图层留空 = 0；坐标系留空建图层时自动"
    "从服务识别，也可手填服务实际坐标系。"
)
_WMS_HINT = "多图层用英文逗号分隔（如 base:图层1,图层2）"
_WMTS_HINT = "瓦片矩阵集须与服务端 GetCapabilities 中的标识一致（如 EPSG:3857）"


def _form_row(form: QFormLayout, label: str, widget: QWidget) -> None:
    """在 QFormLayout 上挂一行：标签 + 控件。"""
    lab = QLabel(label)
    lab.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.addRow(lab, widget)


class CustomBasemapDialog(BaseDialog):
    """自定义底图配置对话框（新增 / 编辑共用字段主体）。"""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        title: str = "添加自定义底图",
        groups: Optional[list[str]] = None,
    ) -> None:
        """构造自定义底图 dialog。

        :param groups: 已有分组名列表（v2 schema 持久化顺序）。传入后「分组」字段
            为可编辑下拉（:class:`QComboBox`），用户可从已有分组里选，也可自由
            输入新分组名（→ "建新分组并加条目"连贯动作）。缺省 / 列表为空时只
            含占位项「自定义底图」。传入 ``None`` 同空列表。
        """
        super().__init__(parent, title=title)
        # 缩小 BaseDialog 默认 800×600 的最小尺寸到紧凑表单
        self.setMinimumSize(0, 0)
        self.resize(640, 580)
        self.setModal(True)
        self._initial_groups: list[str] = list(groups) if groups else []
        self._build_ui()

    # ============================================================== UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        # ---- 通用字段 ----
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("如 内网影像")
        _form_row(form, "名称", self.name_edit)

        # 分组 = 可编辑下拉：Dock 当前 _groups_order 为预设项（"先建组后加条目"
        # 仍可通过手动输入新名实现，键入后 setCurrentText 直接生效）。空列表时
        # 也保留「自定义底图」兜底选项，避免只输入未分组时的“看起来无选项”误导。
        self.group_edit = QComboBox()
        self.group_edit.setEditable(True)
        seen: set[str] = set()
        for g in self._initial_groups:
            if g and g not in seen:
                self.group_edit.addItem(g)
                seen.add(g)
        if "自定义底图" not in seen:
            self.group_edit.addItem("自定义底图")
            seen.add("自定义底图")
        self.group_edit.setCurrentText("自定义底图")
        _form_row(form, "分组", self.group_edit)

        self.type_combo = QComboBox()
        for text, value in _SERVICE_OPTIONS:
            self.type_combo.addItem(text, value)
        _form_row(form, "服务类型", self.type_combo)

        self.url_edit = QLineEdit()
        _form_row(form, "服务地址", self.url_edit)
        # 供子类（编辑连接对话框）在 url 行后追加 API key 行
        self._common_form = form
        layout.addLayout(form)

        # ---- 类型相关的服务参数区（QStackedWidget 随服务类型切换）----
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_xyz_page())
        self.stack.addWidget(self._build_arcgis_page())
        self.stack.addWidget(self._build_arcgisfeature_page())
        self.stack.addWidget(self._build_wms_page("wms"))
        self.stack.addWidget(self._build_wms_page("wmts"))
        layout.addWidget(self.stack, 1)

        self.type_combo.currentIndexChanged.connect(self._update_type_ui)
        self._update_type_ui()

        # ---- 按钮（Ok / Cancel，仿 custom_attr.py）----
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self.setLayout(layout)

    def _build_xyz_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(0, 4, 0, 0)

        self.referer_edit = QLineEdit()
        self.referer_edit.setPlaceholderText("如 https://www.tianditu.gov.cn/（可选）")
        _form_row(form, "Referer（可选）", self.referer_edit)

        zoom_row = QHBoxLayout()
        zoom_row.setSpacing(8)
        self.zmin_spin = QSpinBox()
        self.zmin_spin.setRange(0, 24)
        self.zmax_spin = QSpinBox()
        self.zmax_spin.setRange(0, 24)
        self.zmax_spin.setValue(18)
        zoom_row.addWidget(QLabel("最小"))
        zoom_row.addWidget(self.zmin_spin)
        zoom_row.addWidget(QLabel("最大"))
        zoom_row.addWidget(self.zmax_spin)
        zoom_row.addStretch(1)
        form.addRow(QLabel("缩放等级"), zoom_row)

        hint = QLabel(_XYZ_HINT)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        form.addRow("", hint)
        return page

    def _build_arcgis_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(0, 4, 0, 0)

        self.arcgis_layer_edit = QLineEdit()
        self.arcgis_layer_edit.setPlaceholderText("留空 = 0（MapServer 第一个图层）")
        _form_row(form, "子图层 ID（可选）", self.arcgis_layer_edit)

        hint = QLabel(_ARCGIS_HINT)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        form.addRow("", hint)
        return page

    def _build_arcgisfeature_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(0, 4, 0, 0)

        self.arcgisfeature_layer_edit = QLineEdit()
        self.arcgisfeature_layer_edit.setPlaceholderText("留空 = 0（FeatureServer 第一个图层）")
        _form_row(form, "子图层 ID（可选）", self.arcgisfeature_layer_edit)

        self.arcgisfeature_crs_edit = QLineEdit()
        self.arcgisfeature_crs_edit.setPlaceholderText("如 EPSG:4490；留空自动从服务识别")
        _form_row(form, "坐标系（可选）", self.arcgisfeature_crs_edit)

        hint = QLabel(_ARCGISFEATURE_HINT)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        form.addRow("", hint)
        return page

    def _build_wms_page(self, svc_type: str) -> QWidget:
        """OGC 页（wms / wmts 共用构建，wmts 多一行瓦片矩阵集）。

        字段挂在 ``self.<svc_type>_*`` 命名下，避免两页控件重名混淆。
        """
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(0, 4, 0, 0)

        # 必填/可选字段置空文本 + 占位提示；默认值只给 format/crs/矩阵集
        layers_edit = QLineEdit()
        layers_edit.setPlaceholderText("如 base:图层1,图层2（必填，英文逗号分隔）")
        setattr(self, f"{svc_type}_layers_edit", layers_edit)
        _form_row(form, "图层名", layers_edit)

        styles_edit = QLineEdit()
        styles_edit.setPlaceholderText("可选，如 default")
        setattr(self, f"{svc_type}_styles_edit", styles_edit)
        _form_row(form, "样式（可选）", styles_edit)

        fmt_edit = QLineEdit("image/png")
        setattr(self, f"{svc_type}_format_edit", fmt_edit)
        _form_row(form, "格式", fmt_edit)

        crs_edit = QLineEdit("EPSG:3857")
        setattr(self, f"{svc_type}_crs_edit", crs_edit)
        _form_row(form, "CRS", crs_edit)

        if svc_type == "wmts":
            matrix_edit = QLineEdit("EPSG:3857")
            setattr(self, f"{svc_type}_matrix_edit", matrix_edit)
            _form_row(form, "瓦片矩阵集", matrix_edit)

        hint = QLabel(_WMS_HINT if svc_type == "wms" else _WMTS_HINT)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        form.addRow("", hint)
        return page

    def _update_type_ui(self, *_args) -> None:
        """服务类型切换 → 切 QStackedWidget 页 + 换服务地址占位提示。"""
        svc = self.type_combo.currentData() or "xyz"
        idx = {v: i for i, (_, v) in enumerate(_SERVICE_OPTIONS)}.get(svc, 0)
        self.stack.setCurrentIndex(idx)
        placeholders = {
            "xyz": "如 https://host/{z}/{x}/{y}.png 或 wmts GetTile 模板",
            "arcgis": "如 https://host/arcgis/rest/services/影像/MapServer",
            "arcgisfeature": "如 https://host/arcgis/rest/services/影像/FeatureServer",
            "wms": "如 https://host/geoserver/wms 或 GetCapabilities 地址",
            "wmts": "如 https://host/geoserver/gwc/service/wmts",
        }
        self.url_edit.setPlaceholderText(placeholders.get(svc, ""))

    # ============================================================== 校验 / 取值

    def _validate(self) -> Optional[str]:
        """校验当前输入。返回 None=通过，否则中文错误串（便于测试直接断言）。"""
        name = self.name_edit.text().strip()
        url = self.url_edit.text().strip()
        svc = self.type_combo.currentData() or "xyz"

        if not name:
            return "请输入底图名称"
        if not url.startswith(("http://", "https://")):
            return "请输入有效的服务地址（http/https 开头）"

        if svc == "xyz":
            if not all(p in url for p in ("{x}", "{y}", "{z}")):
                return "XYZ 瓦片地址需包含 {x} {y} {z} 占位符"
            if self.zmin_spin.value() > self.zmax_spin.value():
                return "最小缩放等级不能大于最大等级"
            return None

        if svc == "arcgis":
            if not url.rstrip("/").lower().endswith(("mapserver", "imageserver")):
                return "ArcGIS 服务地址需以 /MapServer 或 /ImageServer 结尾"
            layer = self.arcgis_layer_edit.text().strip()
            if layer and not layer.isdigit():
                return "子图层 ID 应为数字（留空默认 0）"
            return None

        if svc == "arcgisfeature":
            if not re.search(r"/featureserver(?:/\d+)?$", url.rstrip("/"), re.IGNORECASE):
                return "要素服务地址需以 /FeatureServer 结尾"
            layer = self.arcgisfeature_layer_edit.text().strip()
            if layer and not layer.isdigit():
                return "子图层 ID 应为数字（留空默认 0）"
            crs = self.arcgisfeature_crs_edit.text().strip()
            if crs and any(ch in crs for ch in _FORBIDDEN_CHARS):
                return "坐标系不能包含 & 或 = 字符"
            return None

        # wms / wmts
        layers = getattr(self, f"{svc}_layers_edit").text().strip()
        if not layers:
            return "请输入图层名"
        for f in ("layers", "styles", "format", "crs"):
            v = getattr(self, f"{svc}_{f}_edit").text().strip()
            if any(ch in v for ch in _FORBIDDEN_CHARS):
                return f"{f} 不能包含 {' 或 '.join(_FORBIDDEN_CHARS)} 字符"
        if svc == "wmts":
            matrix = getattr(self, "wmts_matrix_edit").text().strip()
            if not matrix:
                return "请输入瓦片矩阵集"
            if any(ch in matrix for ch in _FORBIDDEN_CHARS):
                return "瓦片矩阵集不能包含 & 或 = 字符"
        return None

    def _on_ok(self) -> None:
        err = self._validate()
        if err is not None:
            QMessageBox.warning(self, "输入不完整", err)
            return
        self.accept()

    def basemap_def(self) -> OnlineBasemapDef:
        """按当前控件状态组装 :class:`OnlineBasemapDef`（确定后由调用方持久化）。"""
        svc = self.type_combo.currentData() or "xyz"
        data: dict[str, str] = {
            "group": self.group_edit.currentText().strip() or "自定义底图",
            "name": self.name_edit.text().strip(),
            "type": svc,
            "url": self.url_edit.text().strip(),
        }
        if svc == "xyz":
            if self.referer_edit.text().strip():
                data["referer"] = self.referer_edit.text().strip()
            data["zmin"] = str(self.zmin_spin.value())
            data["zmax"] = str(self.zmax_spin.value())
        elif svc == "arcgis":
            layer = self.arcgis_layer_edit.text().strip()
            if layer:
                data["layers"] = layer
        elif svc == "arcgisfeature":
            layer = self.arcgisfeature_layer_edit.text().strip()
            if layer:
                data["layers"] = layer
            crs = self.arcgisfeature_crs_edit.text().strip()
            if crs:
                data["crs"] = crs
        elif svc in ("wms", "wmts"):
            for k in ("layers", "styles", "format", "crs"):
                v = getattr(self, f"{svc}_{k}_edit").text().strip()
                if v:
                    data[k] = v
            if svc == "wmts":
                matrix = getattr(self, "wmts_matrix_edit").text().strip()
                if matrix:
                    data["tile_matrix_set"] = matrix
        return OnlineBasemapDef.from_dict(data)
