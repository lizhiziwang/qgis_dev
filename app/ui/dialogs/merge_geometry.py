"""「合并」Dialog：从选中的多个要素里指定「要并入哪个图斑」。

左侧列出**可合并的那一组相邻**要素（显示主键值，取不到时退回图层的显示字段 /
``要素 N``），右侧是该要素的全部字段值；**点列表项**会：

- 地图上**高亮闪烁**该要素（闪几下后保持高亮），便于在密集数据里定位到它；
- 右侧属性表同步刷新。

与其余选中图形不相邻的要素不能参与合并（合并要求共享边界，见
:func:`app.ui.tools.merge_geometry.mergeable_group`），它们不出现在列表里，只在
上方以一行提示标注「已排除」，让用户知道自己的选择有一部分没被算进去。

点「确认」取 :meth:`MergeGeometryDialog.target_fid` 作为合并目标。

只做展示与收集，不做几何与图层写入（那是 :mod:`app.ui.tools.merge_geometry`
的事）。主题由全局 QSS 下发，本类不写 setStyleSheet。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

from qgis.core import QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.common.base_dialog import BaseDialog
from app.ui.common.feature_highlight import FeatureHighlighter
from app.ui.common.widgets import make_table
from app.ui.common.value_format import format_value

#: 视为「无值」的文本：真 NULL 之外，数据里常把空值写成字面量字符串
_NULL_TOKENS = {"", "null", "none", "nil", "<null>", "undefined"}

#: 左侧要素列表宽度（右侧属性表占满剩余空间）
_LIST_WIDTH = 140


def _usable_value(value) -> Optional[str]:
    """把字段值整理成可展示的文本；空 / 字面量 ``NULL`` 一律当无值返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in _NULL_TOKENS:
        return None
    return text


def _field_value(layer: QgsVectorLayer, fid: int, field_name: str) -> Optional[str]:
    """要素在 ``field_name`` 字段上的可展示值；字段不存在 / 无值返回 None。"""
    try:
        idx = layer.fields().indexOf(field_name)
        if idx < 0:
            return None
        feature = layer.getFeature(fid)
        if not feature.isValid():
            return None
        return _usable_value(feature.attributes()[idx])
    except Exception:  # noqa: BLE001 —— 显示名取不到不该阻断合并流程
        return None


def _list_label(layer: QgsVectorLayer, fid: int) -> str:
    """要素在左侧列表里的显示名。

    优先**主键**值（与成果数据的图斑编号一致，也是用户认得的那个号），取不到
    再退回图层自己配置的显示字段（``displayField()``），都没有就显示 ``要素 N``。
    """
    try:
        pk_idx = layer.primaryKeyAttributes()
        if pk_idx:
            value = _field_value(layer, fid, layer.fields().at(pk_idx[0]).name())
            if value:
                return value
        display = layer.displayField()
    except Exception:  # noqa: BLE001 —— 显示名取不到不该阻断弹窗
        display = ""
    if display:
        value = _field_value(layer, fid, display)
        if value:
            return value
    return f"要素 {fid}"


class MergeGeometryDialog(BaseDialog):
    """列出选中的要素，指定合并后保留哪一个（「要并入的图斑」）。"""

    def __init__(
        self,
        layer: QgsVectorLayer,
        fids: Sequence[int],
        excluded: Sequence[int] = (),
        canvas=None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent, title="合并")
        # 缩小 BaseDialog 默认 800×600 的最小尺寸到本弹窗的量级
        self.setMinimumSize(0, 0)
        self.resize(620, 620)
        self.setModal(True)
        self._layer = layer
        self._fids = [int(fid) for fid in fids]
        #: 与这一组不相邻、不能参与合并的其余选中要素（仅用于提示，见 _build_ui）
        self._excluded = [int(fid) for fid in excluded]
        # 点要素时在画布上闪烁定位（见 app.ui.common.feature_highlight）
        self._highlighter = FeatureHighlighter(canvas, layer, parent=self)
        self._build_ui()

    # ============================================================== UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(8)

        head = QLabel("请选择合并到指定要素")
        head.setWordWrap(True)
        layout.addWidget(head)

        # 被排除的要素要明说：用户框选了一堆，不提的话会以为全都并进去了
        if self._excluded:
            names = "、".join(_list_label(self._layer, fid) for fid in self._excluded)
            note = QLabel(f"已排除与其余不相邻的要素：{names}")
            note.setObjectName("PropHint")
            note.setWordWrap(True)
            layout.addWidget(note)

        body = QHBoxLayout()
        body.setSpacing(10)

        self.list = QListWidget(self)
        self.list.setFixedWidth(_LIST_WIDTH)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        for fid in self._fids:
            item = QListWidgetItem(_list_label(self._layer, fid))
            item.setData(Qt.ItemDataRole.UserRole, fid)
            self.list.addItem(item)
        body.addWidget(self.list)

        self.attr_table = make_table(["字段", "值"], parent=self)
        self.attr_table.verticalHeader().setVisible(False)
        # 与设计稿一致：不显示表头，列含义（字段 / 值）自明
        self.attr_table.horizontalHeader().setVisible(False)
        self.attr_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        body.addWidget(self.attr_table, 1)

        layout.addLayout(body, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确认")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)

        # 默认选中第一项并加载其属性；信号在初始化之后才接，避免开窗即闪
        if self._fids:
            self.list.setCurrentRow(0)
            self._load_attributes(self._fids[0])
        self.list.currentRowChanged.connect(self._on_row_changed)

    # ============================================================== 交互

    def _on_row_changed(self, row: int) -> None:
        """列表换项 → 地图高亮闪烁 + 右侧加载该要素属性。"""
        fid = self._fid_at(row)
        if fid is None:
            self._highlighter.clear()
            self._load_attributes(None)
            return
        self._highlighter.flash(fid)
        self._load_attributes(fid)

    def _fid_at(self, row: int) -> Optional[int]:
        item = self.list.item(row)
        if item is None:
            return None
        fid = item.data(Qt.ItemDataRole.UserRole)
        return None if fid is None else int(fid)

    def _load_attributes(self, fid: Optional[int]) -> None:
        """把 ``fid`` 的全部字段值填进属性表；``None`` 清空。"""
        self.attr_table.setRowCount(0)
        if fid is None:
            return
        try:
            feature = self._layer.getFeature(fid)
            if not feature.isValid():
                return
            fields = list(self._layer.fields())
            attrs = feature.attributes()
            self.attr_table.setRowCount(len(fields))
            for row, field in enumerate(fields):
                name_item = QTableWidgetItem(field.name())
                name_item.setData(Qt.ItemDataRole.UserRole, field.name())
                self.attr_table.setItem(row, 0, name_item)
                value = attrs[row] if row < len(attrs) else None
                self.attr_table.setItem(
                    row, 1, QTableWidgetItem("NULL" if value is None else format_value(value))
                )
        except Exception:  # noqa: BLE001 —— 属性展示失败不该阻断合并流程
            self.attr_table.setRowCount(0)

    # ============================================================== 取值

    def target_fid(self) -> Optional[int]:
        """「要并入的图斑」的 fid；未选中任何一项返回 ``None``。"""
        return self._fid_at(self.list.currentRow())

    def _on_ok(self) -> None:
        if self.target_fid() is None:
            QMessageBox.information(self, "提示", "请先选择一个要并入的要素")
            return
        self.accept()

    # ============================================================== 收尾

    def done(self, result: int) -> None:  # noqa: N802 —— Qt 命名
        """关闭（确认 / 取消 / Esc）时务必收掉高亮带与定时器。"""
        self._highlighter.close()
        super().done(result)


__all__ = ["MergeGeometryDialog"]
