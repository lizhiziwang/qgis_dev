"""图层属性「常规」页。

左列：图层名 / 图层类型 / 数据集名称 / 数据源路径 / 比例尺范围
右列：图层对象个数 / 数据源设置 / 坐标系统 / 图层透明度 / 数据范围

写回策略：**所有修改都推迟到** :meth:`GeneralTab.apply`（「确定 / 应用」时），
「设置数据源」「设置坐标系统」两个按钮只把选择结果暂存并回显，这样点「取消」
才是真取消。唯一的例外是「重新计算数据范围」——它只是重算缓存的 extent，不改
任何持久状态，点了立刻生效并刷新显示。
"""

from __future__ import annotations

import os

from qgis.core import QgsRasterLayer, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.util.local_layer_loader import layer_source_container, layer_source_table
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.slider import FlatSlider as QSlider
from app.ui.dialogs.layer_properties.base import LayerPropertyTab

logger = get_logger(__name__)

#: 几何类型 → 中文名（与 app/ui/tools/feature_edit.py 的映射保持一致）
_GEOM_NAMES = {0: "点", 1: "线", 2: "面"}

#: 「设置数据源」的文件过滤器
_SOURCE_FILTER = (
    "全部支持的数据 (*.gpkg *.shp *.geojson *.json *.kml *.gml *.tif *.tiff *.img);;"
    "GeoPackage (*.gpkg);;Shapefile (*.shp);;GeoJSON (*.geojson *.json);;"
    "栅格 (*.tif *.tiff *.img);;全部文件 (*)"
)


class CrsSelectDialog(BaseDialog):
    """坐标系选择小对话框。

    不用 QGIS 原生的 ``QgsProjectionSelectionDialog``（自带一套不跟随本应用主题
    的窗口 chrome），而是把已验证可用的 ``QgsProjectionSelectionWidget``
    （``app/ui/dialogs/reproject.py`` 在用）包进 :class:`BaseDialog`，这样无边框
    标题栏和配色都跟应用一致。
    """

    def __init__(self, crs=None, parent=None) -> None:
        super().__init__(parent, "选择坐标系统")
        self.setMinimumSize(0, 0)
        self.resize(520, 360)

        from qgis.gui import QgsProjectionSelectionWidget

        layout = QVBoxLayout()
        self.crs_widget = QgsProjectionSelectionWidget()
        if crs is not None and crs.isValid():
            self.crs_widget.setCrs(crs)
        layout.addWidget(self.crs_widget)
        layout.addStretch(1)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)
        self.setLayout(layout)

    def selected_crs(self):
        return self.crs_widget.crs()


class GeneralTab(LayerPropertyTab):
    """图层属性「常规」页。"""

    def __init__(self, layer=None, parent=None) -> None:
        super().__init__(parent)
        self._layer = layer
        #: 「设置坐标系统」暂存值（None = 用户没改），apply 时才写回图层
        self._pending_crs = None
        #: 「设置数据源」暂存值 (uri, provider)，同上
        self._pending_source = None
        self._build()
        if layer is not None:
            self.load(layer)

    # ------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        columns = QHBoxLayout()
        columns.setSpacing(12)
        columns.addWidget(self._build_left_column(), 1)
        columns.addWidget(self._build_right_column(), 1)
        root.addLayout(columns, 1)

    def _build_left_column(self) -> QWidget:
        box = QWidget(self)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        info = QGroupBox("基本信息", box)
        form = self._new_form(info)
        self.name_edit = QLineEdit(info)
        form.addRow("图层名", self.name_edit)
        self.type_combo = QComboBox(info)
        # 只读展示：类型由数据源决定，不给改
        self.type_combo.setEnabled(False)
        form.addRow("图层类型", self.type_combo)
        self.dataset_edit = QLineEdit(info)
        self.dataset_edit.setReadOnly(True)
        form.addRow("数据集名称", self.dataset_edit)
        self.source_edit = QLineEdit(info)
        self.source_edit.setReadOnly(True)
        form.addRow("数据源路径", self.source_edit)
        layout.addWidget(info)

        scale = QGroupBox("比例尺范围", box)
        scale_layout = QVBoxLayout(scale)
        scale_layout.setSpacing(8)
        self.scale_check = QCheckBox("图层显示比例尺设置", scale)
        self.scale_check.toggled.connect(self._on_scale_toggled)
        scale_layout.addWidget(self.scale_check)

        scale_form = QFormLayout()
        scale_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        scale_form.setHorizontalSpacing(8)
        self.min_scale_edit = QLineEdit(scale)
        self.min_scale_edit.setPlaceholderText("0")
        scale_form.addRow("最小比例尺 1:", self.min_scale_edit)
        self.max_scale_edit = QLineEdit(scale)
        self.max_scale_edit.setPlaceholderText("0")
        scale_form.addRow("最大比例尺 1:", self.max_scale_edit)
        scale_layout.addLayout(scale_form)

        hint = QLabel("填比例尺分母；0 表示该端不限制", scale)
        hint.setObjectName("PropHint")
        scale_layout.addWidget(hint)
        layout.addWidget(scale)

        layout.addStretch(1)
        return box

    def _build_right_column(self) -> QWidget:
        box = QWidget(self)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        summary = QGroupBox("数据源", box)
        form = self._new_form(summary)
        self.count_label = QLabel("—", summary)
        self.count_label.setObjectName("PropValue")
        form.addRow("图层对象个数", self.count_label)

        self.source_btn = QPushButton("设置数据源", summary)
        self.source_btn.clicked.connect(self._on_set_data_source)
        form.addRow("数据源设置", self.source_btn)

        crs_row = QHBoxLayout()
        crs_row.setSpacing(6)
        self.crs_edit = QLineEdit(summary)
        self.crs_edit.setReadOnly(True)
        crs_row.addWidget(self.crs_edit, 1)
        self.crs_btn = QPushButton("设置坐标系统", summary)
        self.crs_btn.clicked.connect(self._on_set_crs)
        crs_row.addWidget(self.crs_btn)
        form.addRow("坐标系统", crs_row)
        layout.addWidget(summary)

        opacity = QGroupBox("图层透明度", box)
        op_layout = QHBoxLayout(opacity)
        op_layout.setSpacing(8)
        left_tip = QLabel("透明", opacity)
        left_tip.setObjectName("PropHint")
        op_layout.addWidget(left_tip)
        self.opacity_slider = QSlider(opacity)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(100)
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)
        op_layout.addWidget(self.opacity_slider, 1)
        right_tip = QLabel("不透明", opacity)
        right_tip.setObjectName("PropHint")
        op_layout.addWidget(right_tip)
        self.opacity_label = QLabel("100%", opacity)
        self.opacity_label.setObjectName("PropValue")
        self.opacity_label.setMinimumWidth(42)
        self.opacity_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        op_layout.addWidget(self.opacity_label)
        layout.addWidget(opacity)

        extent = QGroupBox("数据范围", box)
        ext_layout = QVBoxLayout(extent)
        ext_layout.setSpacing(8)
        ext_grid = QFormLayout()
        ext_grid.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        ext_grid.setHorizontalSpacing(8)
        self.ext_labels = {}
        for key, text in (("left", "左"), ("top", "上"), ("right", "右"), ("bottom", "下")):
            lbl = QLabel("—", extent)
            lbl.setObjectName("PropValue")
            self.ext_labels[key] = lbl
            ext_grid.addRow(text, lbl)
        ext_layout.addLayout(ext_grid)
        self.recalc_btn = QPushButton("重新计算数据范围", extent)
        self.recalc_btn.clicked.connect(self._on_recalc_extent)
        ext_layout.addWidget(self.recalc_btn)
        layout.addWidget(extent)

        layout.addStretch(1)
        return box

    def _new_form(self, parent) -> QFormLayout:
        """统一的表单布局：标签右对齐，与截图一致。"""
        form = QFormLayout(parent)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(8)
        return form

    # ------------------------------------------------------------- 读取

    def load(self, layer) -> None:
        """从图层读值填控件。"""
        self._layer = layer
        if layer is None:
            return
        self._pending_crs = None
        self._pending_source = None

        self.name_edit.setText(layer.name())
        self.type_combo.clear()
        self.type_combo.addItem(_layer_type_name(layer))
        self.dataset_edit.setText(_dataset_name(layer))
        path = _source_path(layer)
        self.source_edit.setText(path)
        # 路径通常比输入框宽，鼠标悬停能看全
        self.source_edit.setToolTip(path)

        # 比例尺：QGIS 的 minimumScale 是「最缩小端」（分母更大），所以它对应
        # 界面上的「最大比例尺」；maximumScale 是「最放大端」，对应「最小比例尺」。
        # 已用探针确认 QGIS 不会自动 swap 这两个值。
        self.scale_check.setChecked(bool(layer.hasScaleBasedVisibility()))
        self.min_scale_edit.setText(_fmt_scale(layer.maximumScale()))
        self.max_scale_edit.setText(_fmt_scale(layer.minimumScale()))
        self._on_scale_toggled(self.scale_check.isChecked())

        if isinstance(layer, QgsVectorLayer):
            self.count_label.setText(str(layer.featureCount()))
        else:
            self.count_label.setText("—")

        self._show_crs(layer.crs())
        opacity = round(_layer_opacity(layer) * 100)
        self.opacity_slider.setValue(opacity)
        self._on_opacity_changed(opacity)
        self._show_extent(layer)

    def _show_crs(self, crs) -> None:
        if crs is None or not crs.isValid():
            self.crs_edit.setText("—")
            self.crs_edit.setToolTip("")
            return
        text = crs.description() or crs.authid() or "—"
        self.crs_edit.setText(text)
        self.crs_edit.setToolTip(crs.authid())

    def _show_extent(self, layer) -> None:
        ext = layer.extent()
        # QgsRectangle 在本构建没有 isValid()，判空用 isNull()（见仓库备忘录）
        if ext is None or ext.isNull():
            for lbl in self.ext_labels.values():
                lbl.setText("—")
            return
        geographic = layer.crs().isGeographic() if layer.crs().isValid() else False
        fmt = (lambda v: f"{v:.5f}") if geographic else (lambda v: f"{v:.2f}")
        self.ext_labels["left"].setText(fmt(ext.xMinimum()))
        self.ext_labels["top"].setText(fmt(ext.yMaximum()))
        self.ext_labels["right"].setText(fmt(ext.xMaximum()))
        self.ext_labels["bottom"].setText(fmt(ext.yMinimum()))

    # ------------------------------------------------------------- 交互

    def _on_scale_toggled(self, checked: bool) -> None:
        self.min_scale_edit.setEnabled(checked)
        self.max_scale_edit.setEnabled(checked)

    def _on_opacity_changed(self, value: int) -> None:
        self.opacity_label.setText(f"{value}%")

    def _on_set_crs(self) -> None:
        """选新坐标系：只暂存 + 回显，真正写回在 :meth:`apply`。"""
        if self._layer is None:
            return
        try:
            current = self._pending_crs if self._pending_crs is not None else self._layer.crs()
            dlg = CrsSelectDialog(current, self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            crs = dlg.selected_crs()
            if crs is None or not crs.isValid():
                QMessageBox.warning(self, "图层属性", "未选择有效的坐标系统")
                return
            self._pending_crs = crs
            self._show_crs(crs)
        except Exception as exc:  # noqa: BLE001
            # 槽里未捕获的异常在本环境会直接原生崩溃（见仓库备忘录），必须兜住
            logger.error("选择坐标系失败: %s", exc, exc_info=True)
            QMessageBox.warning(self, "图层属性", f"选择坐标系失败：{exc}")

    def _on_set_data_source(self) -> None:
        """选新数据源文件：只暂存 + 回显，真正写回在 :meth:`apply`。"""
        if self._layer is None:
            return
        try:
            start = _source_path(self._layer)
            path, _ = QFileDialog.getOpenFileName(self, "选择数据源", start, _SOURCE_FILTER)
            if not path:
                return
            provider = self._layer.providerType() or "ogr"
            self._pending_source = (path, provider)
            self.source_edit.setText(path)
            self.source_edit.setToolTip(path)
            self.dataset_edit.setText(os.path.splitext(os.path.basename(path))[0])
        except Exception as exc:  # noqa: BLE001
            logger.error("选择数据源失败: %s", exc, exc_info=True)
            QMessageBox.warning(self, "图层属性", f"选择数据源失败：{exc}")

    def _on_recalc_extent(self) -> None:
        """重算数据范围。只刷新缓存的 extent，不改持久状态，故立刻生效。

        ``updateExtents()`` 只在**矢量图层**上有 —— 栅格图层调它会
        ``AttributeError: 'QgsRasterLayer' object has no attribute 'updateExtents'``
        （用户点 tif 的属性 → 重新计算数据范围时报过）。栅格的范围来自文件本身的
        地理变换、没有缓存可重算，所以那边改成让数据源重读一遍元数据再显示。
        """
        if self._layer is None:
            return
        try:
            from qgis.core import QgsVectorLayer

            if isinstance(self._layer, QgsVectorLayer):
                self._layer.updateExtents()
            else:
                provider = self._layer.dataProvider()
                reload_meta = getattr(provider, "reload", None)
                if callable(reload_meta):
                    reload_meta()
            self._show_extent(self._layer)
        except Exception as exc:  # noqa: BLE001
            logger.error("重新计算数据范围失败: %s", exc, exc_info=True)
            QMessageBox.warning(self, "图层属性", f"重新计算数据范围失败：{exc}")

    # ------------------------------------------------------------- 写回

    def apply(self, layer) -> bool:
        """把控件值写回图层；任一项校验不过就提示并返回 ``False``。"""
        if layer is None:
            return True

        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "图层属性", "图层名不能为空")
            self.name_edit.setFocus()
            return False

        use_scale = self.scale_check.isChecked()
        try:
            # 界面「最小比例尺」= 最放大端 = maximumScale；「最大比例尺」= 最缩小端 = minimumScale
            max_scale = _parse_scale(self.min_scale_edit.text(), "最小比例尺")
            min_scale = _parse_scale(self.max_scale_edit.text(), "最大比例尺")
        except ValueError as exc:
            QMessageBox.warning(self, "图层属性", str(exc))
            return False
        if use_scale and max_scale > 0 and min_scale > 0 and max_scale >= min_scale:
            QMessageBox.warning(
                self, "图层属性", "最小比例尺的分母必须小于最大比例尺的分母"
            )
            return False

        # 数据源换了就先换——换源会重建 provider，之后才轮到其它属性
        if self._pending_source is not None:
            uri, provider = self._pending_source
            try:
                layer.setDataSource(uri, name, provider)
            except Exception as exc:  # noqa: BLE001
                logger.error("设置数据源失败: %s", exc, exc_info=True)
                QMessageBox.warning(self, "图层属性", f"设置数据源失败：{exc}")
                return False
            if not layer.isValid():
                QMessageBox.warning(self, "图层属性", f"新数据源无法加载：{uri}")
                return False
            self._pending_source = None

        layer.setName(name)
        layer.setScaleBasedVisibility(use_scale)
        layer.setMaximumScale(max_scale)
        layer.setMinimumScale(min_scale)

        if self._pending_crs is not None:
            layer.setCrs(self._pending_crs)
            self._pending_crs = None

        if hasattr(layer, "setOpacity"):
            layer.setOpacity(self.opacity_slider.value() / 100.0)

        # 写回后重新读一遍：换源 / 换 CRS 会连带改变要素数与数据范围
        self._show_crs(layer.crs())
        self._show_extent(layer)
        if isinstance(layer, QgsVectorLayer):
            self.count_label.setText(str(layer.featureCount()))
        return True


# ----------------------------------------------------------------- 工具函数


def _layer_type_name(layer) -> str:
    """图层类型中文名，如「矢量面要素图层」/「栅格图层」。"""
    if isinstance(layer, QgsRasterLayer):
        return "栅格图层"
    if isinstance(layer, QgsVectorLayer):
        # QGIS sip 枚举是 int 子类，int() 取值安全（见仓库备忘录）
        kind = _GEOM_NAMES.get(int(layer.geometryType()))
        return f"矢量{kind}要素图层" if kind else "矢量图层"
    return "图层"


def _source_path(layer) -> str:
    """数据源路径（容器路径）。

    走 :func:`~app.core.util.local_layer_loader.layer_source_container`：优先加载方
    挂的 ``layer.path_c``（见 ``file_browser.py`` / ``shell.py``），否则从 ``source()``
    里剥掉子图层 / provider 选项（OGR 的 ``|layername=`` 或 spatialite provider 的
    ``dbname='…' table="…" (geom)`` 都认）。
    """
    return str(layer_source_container(layer)).strip()


def _dataset_name(layer) -> str:
    """数据集名称：数据源里的子图层名（表名），否则文件名，再否则图层名。"""
    table = layer_source_table(layer)
    if table:
        return table
    path = _source_path(layer)
    # 内存图层的 source 是 "Polygon?crs=..." 这种，不是路径；带 ? 的一律不当文件名
    if path and "?" not in path and os.path.splitext(path)[1]:
        stem = os.path.splitext(os.path.basename(path))[0]
        if stem:
            return stem
    return layer.name()


def _layer_opacity(layer) -> float:
    """图层不透明度 0.0-1.0；取不到按完全不透明处理。"""
    getter = getattr(layer, "opacity", None)
    if not callable(getter):
        return 1.0
    try:
        return float(getter())
    except Exception:  # noqa: BLE001
        return 1.0


def _fmt_scale(value) -> str:
    """比例尺分母 → 输入框文本（0 / 无效值显示空串，即「不限制」）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    if num <= 0:
        return ""
    return str(int(num)) if num.is_integer() else f"{num:g}"


def _parse_scale(text: str, field: str) -> float:
    """输入框文本 → 比例尺分母。空串 / 0 表示不限制；容忍用户填 ``1:100000``。"""
    raw = (text or "").strip()
    if not raw:
        return 0.0
    if raw.startswith("1:"):
        raw = raw[2:].strip()
    raw = raw.replace(",", "")
    try:
        num = float(raw)
    except ValueError:
        raise ValueError(f"{field}填写有误：「{text}」不是有效数字") from None
    if num < 0:
        raise ValueError(f"{field}不能为负数")
    return num
