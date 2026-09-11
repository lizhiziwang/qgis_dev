"""图层属性「字段」页。

仿 QGIS 原生 vector layer properties → Fields：6 列表格显示当前图层的字段，
可改字段别名（双击）、选主显示字段（combo）。筛选只是视图过滤，不改图层。
所有修改推迟到 :meth:`FieldsTab.apply`（与「常规」页的写法一致——只有
「取消」才会丢修改）。

「显示」列 checkbox：**仅视觉呈现**，默认勾上；不联动
``QgsAttributeTableConfig``（4.2 上 setAttributeTableConfig 是支持的，但内存
provider 不一定落盘，且截图里的列就是普通 checkbox，没要求写回属性表配置）。
后续如需真正写回，在 :meth:`apply` 里读每行 checkState → ``cfg.columnHidden``
→ ``setAttributeTableConfig``，并 try/except 兜内存 provider。
"""

from __future__ import annotations

from qgis.core import QgsField, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.infra.logging import get_logger
from app.ui.dialogs.layer_properties.add_field_dialog import AddFieldDialog
from app.ui.dialogs.layer_properties.base import LayerPropertyTab

logger = get_logger(__name__)

_HEADERS = ("显示", "字段名", "字段别名", "字段类型", "字段长度", "小数位数")


class FieldsTab(LayerPropertyTab):
    """图层属性「字段」页。"""

    def __init__(self, layer=None, parent=None) -> None:
        super().__init__(parent)
        self._layer = layer
        self._build()
        if layer is not None:
            self.load(layer)

    # ------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # 顶部：筛选 + 添加字段 + 刷新
        top = QHBoxLayout()
        top.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入字段名或别名筛选")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        top.addWidget(self.search_edit, 1)
        self.add_btn = QPushButton("添加字段")
        self.add_btn.clicked.connect(self._on_add_field)
        top.addWidget(self.add_btn)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self._on_refresh)
        top.addWidget(self.refresh_btn)
        root.addLayout(top)

        # 中部：字段表
        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        # 整表允许编辑触发；具体哪些列真能改，由 item flags 控制
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        header = self.table.horizontalHeader()
        # 表头行为：固定列用 Interactive（用户可拖）+ 默认宽度合理；
        # 「小数位数」这一列设 Stretch 模式，对话框变宽时把剩余空间吃掉——
        # 这样表格始终撑满父容器（与 QGIS 原生字段表观感一致）。
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(48)
        header.setStretchLastSection(True)
        # 各列默认宽度（像素）——「小数位数」列因为是 Stretch，会自动吃掉剩余空间
        for col, width in (
            (0, 50),    # 显示：checkbox
            (1, 110),   # 字段名
            (2, 180),   # 字段别名
            (3, 130),   # 字段类型
            (4, 90),    # 字段长度
            (5, 90),    # 小数位数（Stretch，吃剩余）
        ):
            self.table.setColumnWidth(col, width)
        # 表头整体略高一点，让标题不贴边
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(self.table, 1)

        # 底部：主显示字段 + 字段个数
        bot = QGroupBox(self)
        bot_layout = QHBoxLayout(bot)
        bot_layout.setContentsMargins(10, 6, 10, 6)
        bot_layout.setSpacing(8)
        lbl = QLabel("主显示字段:", bot)
        lbl.setObjectName("PropHint")
        bot_layout.addWidget(lbl)
        self.display_combo = QComboBox(bot)
        self.display_combo.setMinimumWidth(220)
        bot_layout.addWidget(self.display_combo, 1)
        self.count_label = QLabel("字段个数: —", bot)
        self.count_label.setObjectName("PropValue")
        bot_layout.addWidget(self.count_label)
        root.addWidget(bot)

    # ------------------------------------------------------------- 读取

    def load(self, layer) -> None:
        """从 ``layer`` 重填控件。「刷新」按钮和构造时都会触发。"""
        self._layer = layer
        if not isinstance(layer, QgsVectorLayer):
            self.table.setRowCount(0)
            self.count_label.setText("字段个数: —")
            self.display_combo.clear()
            return
        try:
            fields = layer.fields()
            self._populate_table(fields)
            self._populate_display_combo(fields, layer.displayExpression())
            self.count_label.setText(f"字段个数: {fields.size()}")
            # 重填后再跑一次筛选（保持之前的过滤状态）
            self._apply_filter(self.search_edit.text())
        except Exception as exc:  # noqa: BLE001
            # 探针确认过：本环境下层接口稳定，但留兜底防止 provider 临时失败
            logger.error("读取字段失败: %s", exc, exc_info=True)

    def _populate_table(self, fields) -> None:
        """把 ``QgsFields`` 写进 6 列表格。"""
        n = fields.size()
        self.table.setRowCount(n)
        for i in range(n):
            fld = fields.at(i)

            # 列 0：checkbox（默认勾上；不改 attributeTableConfig）
            cb_item = QTableWidgetItem()
            cb_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            cb_item.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(i, 0, cb_item)

            # 列 1：字段名（只读）
            name_item = QTableWidgetItem(fld.name())
            name_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(i, 1, name_item)

            # 列 2：字段别名（可编辑）
            alias_item = QTableWidgetItem(fld.alias() or "")
            self.table.setItem(i, 2, alias_item)

            # 列 3：类型
            type_item = QTableWidgetItem(fld.typeName() or "")
            type_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            self.table.setItem(i, 3, type_item)

            # 列 4：长度（>0 才显示，与 QGIS 原生一致）
            length = fld.length()
            length_str = str(length) if length > 0 else ""
            length_item = QTableWidgetItem(length_str)
            length_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            self.table.setItem(i, 4, length_item)

            # 列 5：小数位数（>0 才显示）
            precision = fld.precision()
            prec_str = str(precision) if precision > 0 else ""
            prec_item = QTableWidgetItem(prec_str)
            prec_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            self.table.setItem(i, 5, prec_item)

    def _append_pending_row(self, spec: dict) -> None:
        """把「添加字段」对话框返回的 spec 追加成一行 pending 状态。

        pending 行在 :meth:`apply` 里通过 ``addAttributes`` 真正写进图层；
        用户点「取消」→ 表格丢弃 pending 行；点「刷新」→ 同样丢弃。
        """
        row = self.table.rowCount()
        self.table.insertRow(row)

        # 列 0：checkbox + 在 UserRole 上挂 pending 标记
        cb_item = QTableWidgetItem()
        cb_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        cb_item.setCheckState(Qt.CheckState.Checked)
        cb_item.setData(Qt.ItemDataRole.UserRole, {"pending": True, "spec": spec})
        self.table.setItem(row, 0, cb_item)

        # 列 1：字段名（只读；pending 也不允许改名，避免和 spec 不一致）
        name_item = QTableWidgetItem(spec["name"])
        name_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, 1, name_item)

        # 列 2：字段别名（可编辑；用户可以再调整）
        alias_item = QTableWidgetItem(spec.get("alias") or "")
        self.table.setItem(row, 2, alias_item)

        # 列 3：类型（只读）
        type_item = QTableWidgetItem(spec["type_name"])
        type_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        )
        self.table.setItem(row, 3, type_item)

        # 列 4：长度（>0 才显示）
        length = spec.get("length", 0)
        length_item = QTableWidgetItem(str(length) if length > 0 else "")
        length_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        )
        self.table.setItem(row, 4, length_item)

        # 列 5：小数位数
        precision = spec.get("precision", 0)
        prec_item = QTableWidgetItem(str(precision) if precision > 0 else "")
        prec_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        )
        self.table.setItem(row, 5, prec_item)

        # 同步进「主显示字段」combo —— 让用户也能把新字段选作主显示
        self.display_combo.addItem(
            f"{spec['name']} · {spec.get('alias') or spec['name']}",
            spec["name"],
        )

    def _is_pending_row(self, row: int) -> dict | None:
        """row 是否是 pending 添加；是的话返回 spec，否则返回 None。"""
        cb = self.table.item(row, 0)
        if cb is None:
            return None
        data = cb.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, dict) and data.get("pending"):
            return data.get("spec")
        return None

    def _populate_display_combo(self, fields, current_expr: str) -> None:
        """每个字段填进 combo。itemData 存原始字段名（apply 时直接 setDisplayExpression）。"""
        self.display_combo.clear()
        for i in range(fields.size()):
            name = fields.at(i).name()
            alias = fields.at(i).alias() or name
            self.display_combo.addItem(f"{name} · {alias}", name)
        # 选中 current；current 可能不是裸字段名（如 "concat(a, b)"），找不着就保持 index=0
        if current_expr:
            for i in range(self.display_combo.count()):
                if self.display_combo.itemData(i) == current_expr:
                    self.display_combo.setCurrentIndex(i)
                    return

    # ------------------------------------------------------------- 交互

    def _apply_filter(self, text: str) -> None:
        """按字段名 / 别名 子串过滤（大小写不敏感）。空文本 = 全显。"""
        needle = (text or "").strip().lower()
        for row in range(self.table.rowCount()):
            if not needle:
                self.table.setRowHidden(row, False)
                continue
            name = (self.table.item(row, 1).text() or "").lower()
            alias = (self.table.item(row, 2).text() or "").lower()
            self.table.setRowHidden(
                row, needle not in name and needle not in alias
            )

    def _on_refresh(self) -> None:
        """「刷新」按钮：从图层重读一遍（用户在 QGIS 里改了字段结构也跟上）。

        注意会丢弃所有 pending 添加行——刷新是从图层重读，pending 在图层里
        还不存在。
        """
        if self._layer is None:
            return
        self.load(self._layer)

    def _on_add_field(self) -> None:
        """「添加字段」按钮：弹子对话框拿 spec，append 成 pending 行。

        校验：字段名不能与图层已有字段或已有的 pending 行重名（重名 →
        QMessageBox.warning，不加）。
        """
        if self._layer is None:
            return
        dlg = AddFieldDialog(self)
        if dlg.exec() != AddFieldDialog.DialogCode.Accepted:
            return
        spec = dlg.field_spec()
        if spec is None:
            QMessageBox.warning(self, "添加字段", "字段名不能为空")
            return
        new_name = spec["name"]
        # 重名校验：图层里已有 + 表格里已有 pending
        existing = {self.table.item(r, 1).text() for r in range(self.table.rowCount())
                    if self.table.item(r, 1)}
        if new_name in existing:
            QMessageBox.warning(
                self, "添加字段",
                f"字段名「{new_name}」已存在（与图层字段或已添加的 pending 行冲突）",
            )
            return
        self._append_pending_row(spec)
        # 字段个数立即反映在标签上
        self.count_label.setText(f"字段个数: {self.table.rowCount()}")

    # ------------------------------------------------------------- 写回

    def apply(self, layer) -> bool:
        """把控件值写回图层。统一 try/except 兜底——任何字段写失败都弹窗 + 返回 False。

        写回分两步：
        1. 收集所有 pending 行（用户通过「添加字段」按钮加的），批量
           ``dataProvider().addAttributes`` 落到图层，再 ``updateFields`` 让
           新字段出现在 ``layer.fields()`` 里；
        2. 遍历整张表（原始字段 + 新加字段），按字段名查索引 → 写别名，
           这样新字段的别名（用户在第 2 列手填的）也能跟上去；
        3. 写主显示字段 ``setDisplayExpression``。
        """
        if not isinstance(layer, QgsVectorLayer):
            return True
        try:
            # 1) 批量添加 pending 字段
            pending_specs: list[dict] = []
            for row in range(self.table.rowCount()):
                spec = self._is_pending_row(row)
                if spec is not None:
                    pending_specs.append(spec)
            if pending_specs:
                new_fields = [
                    QgsField(
                        s["name"], s["qtype"], s["type_name"], s["length"], s["precision"]
                    )
                    for s in pending_specs
                ]
                ok = layer.dataProvider().addAttributes(new_fields)
                if not ok:
                    raise RuntimeError("dataProvider.addAttributes 返回 False（provider 可能不允许改 schema）")
                layer.updateFields()

            # 2) 写别名：按字段名查索引（addAttributes 后 layer.fields() 已含新字段）
            for row in range(self.table.rowCount()):
                # pending 行的别名可能被用户在第 2 列手改过；原始行也可能改了别名
                new_alias = (self.table.item(row, 2).text() or "").strip()
                name = (self.table.item(row, 1).text() or "").strip()
                idx = layer.fields().indexOf(name)
                if idx >= 0:
                    layer.setFieldAlias(idx, new_alias)

            # 3) 写主显示字段：combo 的 itemData 存的就是字段名
            name = self.display_combo.currentData()
            if name:
                layer.setDisplayExpression(name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("应用字段属性失败: %s", exc, exc_info=True)
            QMessageBox.warning(self, "图层属性", f"应用字段属性失败：{exc}")
            return False