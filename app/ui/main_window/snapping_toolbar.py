"""磁性捕捉（吸附）工具条 —— QGIS 桌面「捕捉工具栏」的等效实现。

用途
----
给画布绘制（自定义“添加点/线/面/矩形/圆”工具 + 画布上的各种数字化操作）提供
“吸附”配置：画新要素时，鼠标会自动吸到已有要素的**顶点 / 边**，容差与
单位可调。对应 QGIS 桌面 View → Toolbars → Snapping Toolbar 的功能。

实现边界
--------
QGIS 4.2 不导出“整根内置捕捉工具条”组件，只能按公开 API 拼装：

- 开关 / 类型 / 容差 / 单位 → 写入 :class:`QgsProject` 的 ``snappingConfig()``
- 再 ``QgsMapCanvasSnappingUtils.setConfig(...)`` 同步到画布 ——
  自定义绘制工具落点前经 ``snapped_map_point``（见
  ``app/core/domain/drawing/tools/snap.py``）从 ``canvas.snappingUtils()``
  吸附一次，因此开关/类型/容差/模式全由本工具条一处控制。

模式语义（与 QGIS 桌面一致）：
- **所有图层**：容差 / 类型作用于全部矢量图层
- **当前图层**：只吸附当前选中的编辑图层（内部用 AdvancedConfiguration +
  单图层 IndividualLayerSettings 实现，不依赖画布 active-layer 管道）

注意：4.2 的吸附类型枚举没有"交点(Intersection)"（已被 QGIS 移除），
且磁吸标记 :class:`QgsSnapIndicator` 需要外部喂 match（QGIS 桌面由内部
app 代码驱动），自建应用里先不接——不影响吸附本身生效。
"""

from __future__ import annotations

from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsProject, QgsVectorLayer
    from qgis.gui import QgsMapCanvas

from qgis.core import Qgis, QgsSnappingConfig
from qgis.gui import QgsMapCanvasSnappingUtils
from qgis.PyQt.QtCore import QObject
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QMainWindow,
    QToolBar,
    QToolButton,
)

#: 默认吸附设置（新工程 / 未保存过吸附配置时套用，开箱即用）
_DEFAULT_TOLERANCE_PX = 12.0
_DEFAULT_UNITS = Qgis.MapToolUnit.Pixels
_DEFAULT_MODE = Qgis.SnappingMode.AllLayers


class QgisSnappingToolbar(QObject):
    """磁性捕捉工具条：开关 + 顶点/边 + 模式 + 容差/单位。"""

    def __init__(
        self,
        main_window: "QMainWindow",
        canvas: "QgsMapCanvas",
        project: "QgsProject",
        editing_layer_getter: Optional[Callable[[], "Optional[QgsVectorLayer]"]] = None,
    ) -> None:
        """
        :param main_window: 主窗口
        :param canvas: 地图画布（吸附作用于其上）
        :param project: QgsProject（吸附配置落在这里）
        :param editing_layer_getter: 返回当前编辑/选中图层（或 None），
            供「当前图层」模式取吸附目标
        """
        super().__init__(parent=main_window)
        self.main_window = main_window
        self.canvas = canvas
        self.project = project
        self._editing_layer_getter = editing_layer_getter or (lambda: None)

        #: 「当前图层」模式的吸附目标（由外部 set_current_layer 维护）
        self._current_layer: "Optional[QgsVectorLayer]" = None

        # ---- 接管画布吸附（QgsMapToolCapture 从 canvas.snappingUtils() 读）----
        utils = canvas.snappingUtils()
        if utils is None:
            utils = QgsMapCanvasSnappingUtils(canvas)
            canvas.setSnappingUtils(utils)
        self._utils = utils

        self._build_toolbar()
        self._init_from_project()

    # ============================================================== 装配

    def _build_toolbar(self) -> None:
        self.toolbar = QToolBar("磁性捕捉")
        self.toolbar.setObjectName("QgisSnappingToolbar")

        # 吸附总开关
        self._enable_btn = QToolButton()
        self._enable_btn.setText("捕捉")
        self._enable_btn.setCheckable(True)
        self._enable_btn.setToolTip("开启/关闭磁性捕捉（吸附已有要素顶点/边）")
        self._enable_btn.toggled.connect(self._apply)
        self.toolbar.addWidget(self._enable_btn)

        self.toolbar.addSeparator()

        # 吸附类型：顶点 / 边
        self._vertex_btn = QToolButton()
        self._vertex_btn.setText("顶点")
        self._vertex_btn.setCheckable(True)
        self._vertex_btn.setToolTip("吸附到已有要素的顶点")
        self._vertex_btn.toggled.connect(self._apply)
        self.toolbar.addWidget(self._vertex_btn)

        self._edge_btn = QToolButton()
        self._edge_btn.setText("边")
        self._edge_btn.setCheckable(True)
        self._edge_btn.setToolTip("吸附到已有要素的线段边")
        self._edge_btn.toggled.connect(self._apply)
        self.toolbar.addWidget(self._edge_btn)

        self.toolbar.addSeparator()

        # 模式：所有图层 / 当前图层
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("所有图层", Qgis.SnappingMode.AllLayers)
        self._mode_combo.addItem("当前图层", Qgis.SnappingMode.AdvancedConfiguration)
        self._mode_combo.setToolTip("吸附范围：所有图层 / 仅当前编辑图层")
        self._mode_combo.currentIndexChanged.connect(self._apply)
        self.toolbar.addWidget(self._mode_combo)

        self.toolbar.addSeparator()

        # 容差 + 单位
        self._tolerance_spin = QDoubleSpinBox()
        self._tolerance_spin.setRange(0.0, 9999.0)
        self._tolerance_spin.setDecimals(1)
        self._tolerance_spin.setSingleStep(1.0)
        self._tolerance_spin.setToolTip("吸附容差")
        self._tolerance_spin.valueChanged.connect(self._apply)
        self.toolbar.addWidget(self._tolerance_spin)

        self._unit_combo = QComboBox()
        self._unit_combo.addItem("像素", Qgis.MapToolUnit.Pixels)
        self._unit_combo.addItem("图层单位", Qgis.MapToolUnit.Layer)
        self._unit_combo.setToolTip("容差单位：像素（屏幕距离）/ 图层单位（地图距离）")
        self._unit_combo.currentIndexChanged.connect(self._apply)
        self.toolbar.addWidget(self._unit_combo)

        self.main_window.addToolBar(self.toolbar)

    # ============================================================== 配置

    def _init_from_project(self) -> None:
        """优先沿用工程已保存的吸附配置；从未配置过则套用默认值。"""
        cfg = self.project.snappingConfig()
        # 判定“是否已有显式配置”：仅以 enabled 为准（QGIS 全新工程默认关闭）——
        # 注意：Qgis.SnappingType 运行时枚举没有 NoSnap 成员，不能拿来比对
        configured = cfg.enabled()

        # 先把已存配置还原到控件
        self._enable_btn.setChecked(cfg.enabled())
        type_flag = cfg.typeFlag()
        self._vertex_btn.setChecked(bool(type_flag & Qgis.SnappingType.Vertex))
        self._edge_btn.setChecked(bool(type_flag & Qgis.SnappingType.Segment))
        self._tolerance_spin.setValue(cfg.tolerance())
        unit_idx = self._unit_combo.findData(cfg.units())
        if unit_idx >= 0:
            self._unit_combo.setCurrentIndex(unit_idx)
        mode_idx = self._mode_combo.findData(cfg.mode())
        if mode_idx >= 0:
            self._mode_combo.setCurrentIndex(mode_idx)

        if not configured:
            # 全新工程默认值：开箱即用（开启 + 顶点/边 + 12px + 所有图层）
            self._enable_btn.setChecked(True)
            self._vertex_btn.setChecked(True)
            self._edge_btn.setChecked(True)
            self._tolerance_spin.setValue(_DEFAULT_TOLERANCE_PX)
            self._unit_combo.setCurrentIndex(self._unit_combo.findData(_DEFAULT_UNITS))
            self._mode_combo.setCurrentIndex(self._mode_combo.findData(_DEFAULT_MODE))

        self._apply()

    def _type_flag(self) -> Qgis.SnappingType:
        # 用两个单比特成员的 AND 构造“空类型”值 0（运行时枚举没有 NoSnap 成员）
        flag = Qgis.SnappingType.Vertex & Qgis.SnappingType.Segment
        if self._vertex_btn.isChecked():
            flag |= Qgis.SnappingType.Vertex
        if self._edge_btn.isChecked():
            flag |= Qgis.SnappingType.Segment
        return flag

    def _apply(self) -> None:
        """把控件状态写入 project 吸附配置 + 画布 snapping utils。"""
        enabled = self._enable_btn.isChecked()
        mode = self._mode_combo.currentData() or Qgis.SnappingMode.AllLayers
        type_flag = self._type_flag()
        tolerance = self._tolerance_spin.value()
        units = self._unit_combo.currentData() or Qgis.MapToolUnit.Pixels

        cfg = QgsSnappingConfig(self.project)
        cfg.setEnabled(enabled)
        cfg.setMode(mode)
        cfg.setTypeFlag(type_flag)
        cfg.setTolerance(tolerance)
        cfg.setUnits(units)

        if mode == Qgis.SnappingMode.AdvancedConfiguration:
            # 「当前图层」：只对目标图层开个体吸附设置，其余图层一律不吸
            cfg.clearIndividualLayerSettings()
            target = self._current_layer or self._editing_layer_getter()
            if target is not None and target.type() == target.VectorLayer:
                ils = QgsSnappingConfig.IndividualLayerSettings(
                    True, type_flag, tolerance, units
                )
                cfg.setIndividualLayerSettings(target, ils)
        else:
            # 「所有图层」：走全局设置，清掉历史个体设置避免残留影响
            cfg.clearIndividualLayerSettings()

        self.project.setSnappingConfig(cfg)
        self._utils.setConfig(cfg)

        if enabled:
            # 预建吸附索引：QGS snapping 首次 snapToMap 才异步建索引
            # （首次点击可能 miss）；开启吸附时先触发一次，用户首击即可命中
            try:
                self._utils.snapToMap(self.canvas.extent().center(), None, True)
            except Exception:  # noqa: BLE001
                pass

    # ============================================================== 公共 API

    def set_current_layer(self, layer: "Optional[QgsVectorLayer]") -> None:
        """更新「当前图层」模式的吸附目标（编辑/选中图层变化时调用）。"""
        self._current_layer = layer
        if (self._mode_combo.currentData() or Qgis.SnappingMode.AllLayers) == Qgis.SnappingMode.AdvancedConfiguration:
            self._apply()

    def set_enabled(self, enabled: bool) -> None:
        """程序化开关吸附（等价于点工具条上的「捕捉」按钮）。"""
        self._enable_btn.setChecked(enabled)
