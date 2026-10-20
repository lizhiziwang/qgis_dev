"""自建「图层属性」对话框外壳。

替代 QGIS 原生的 ``QgsVectorLayerProperties``：原生窗口自带一套不跟随本应用
无边框标题栏与 :class:`~app.ui.common.theme.ThemeManager` 主题的 chrome。

标签页由 :data:`LayerPropertiesDialog._TAB_SPECS` 表驱动，``None`` 表示该页
内容尚未实现、先用 :class:`~app.ui.dialogs.layer_properties.base.PlaceholderTab`
占位。后续补页只需把对应的 ``None`` 换成页类（约定见 ``base.py``），外壳不用动。
"""

from __future__ import annotations

from qgis.core import QgsVectorLayer
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
)

from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.dialogs.layer_properties.base import PlaceholderTab
from app.ui.dialogs.layer_properties.condition import ConditionTab
from app.ui.dialogs.layer_properties.fields import FieldsTab
from app.ui.dialogs.layer_properties.general import GeneralTab
from app.ui.dialogs.layer_properties.labeling import LabelingTab
from app.ui.dialogs.layer_properties.render import RenderTab
from app.ui.dialogs.layer_properties.selection import SelectionTab

logger = get_logger(__name__)

#: 只对矢量图层有意义的页（栅格图层下禁用）
_VECTOR_ONLY = frozenset({"条件", "字段", "选择", "标注", "渲染"})


class LayerPropertiesDialog(BaseDialog):
    """图层属性对话框（常规 / 条件 / 字段 / 选择 / 标注 / 渲染页）。"""

    #: (标签名, 页类)；页类为 None → 占位页。
    _TAB_SPECS = (
        ("常规", GeneralTab),
        ("字段", FieldsTab),
        ("选择", SelectionTab),
        ("标注", LabelingTab),
        ("渲染", RenderTab),
        ("条件", ConditionTab),
    )

    def __init__(self, layer, canvas=None, parent=None) -> None:
        title = f"图层属性 - {layer.name()}" if layer is not None else "图层属性"
        super().__init__(parent, title)
        self.layer = layer
        self.canvas = canvas
        # 基类默认下限 800x600 对这个表单偏大，按 add_connection.py 的写法清掉再 resize
        self.setMinimumSize(0, 0)
        self.resize(880, 580)
        self._tab_widgets = []
        self._build()

    # ------------------------------------------------------------- 构建

    def _build(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        self.tabs = QTabWidget(self)
        is_vector = isinstance(self.layer, QgsVectorLayer)
        for title, tab_cls in self._TAB_SPECS:
            page = tab_cls(self.layer, self) if tab_cls is not None else PlaceholderTab(title, self)
            index = self.tabs.addTab(page, title)
            self._tab_widgets.append(page)
            if title in _VECTOR_ONLY and not is_vector:
                self.tabs.setTabEnabled(index, False)
        layout.addWidget(self.tabs, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.ok_btn = QPushButton("确定", self)
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self._on_ok)
        buttons.addWidget(self.ok_btn)
        self.cancel_btn = QPushButton("取消", self)
        self.cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_btn)
        self.apply_btn = QPushButton("应用", self)
        self.apply_btn.clicked.connect(self._on_apply)
        buttons.addWidget(self.apply_btn)
        layout.addLayout(buttons)

        self.setLayout(layout)

    # ------------------------------------------------------------- 事件

    def _on_ok(self) -> None:
        """确定：全部写回成功才关闭，失败则保持打开（页内部已提示原因）。"""
        if self._apply_all():
            self.accept()

    def _on_apply(self) -> None:
        """应用：写回但不关闭。"""
        self._apply_all()

    def _apply_all(self) -> bool:
        """逐页 ``apply``；任一页返回 False 即中止。

        槽里未捕获的异常在本环境会直接原生崩溃（0xC0000409，无 traceback），
        所以每页都用 try 兜住转成提示。
        """
        if self.layer is None:
            return True
        for page in self._tab_widgets:
            applier = getattr(page, "apply", None)
            if not callable(applier):
                continue
            try:
                ok = applier(self.layer)
            except Exception as exc:  # noqa: BLE001
                logger.error("应用图层属性失败: %s", exc, exc_info=True)
                QMessageBox.warning(self, "图层属性", f"应用失败：{exc}")
                return False
            if not ok:
                return False
        self.setWindowTitle(f"图层属性 - {self.layer.name()}")
        # 重绘不能留在按钮 clicked 的信号栈里同步跑——仓库多次记录过这会原生崩溃
        # （见 buffer.py 的 _on_execute 与 layer_actions.py 的 _remove_layer）。
        QTimer.singleShot(0, self._refresh_canvas)
        return True

    def _refresh_canvas(self) -> None:
        """事件栈外重绘图层与画布。"""
        try:
            if self.layer is not None:
                self.layer.triggerRepaint()
            if self.canvas is not None:
                self.canvas.refresh()
        except Exception as exc:  # noqa: BLE001
            logger.warning("刷新画布失败: %s", exc)
