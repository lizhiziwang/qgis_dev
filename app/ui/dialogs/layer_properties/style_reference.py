"""「样式引用」：勾选符号选择器里要显示的分组 / 导入外部样式。

列表项与符号选择器左侧的**分组**一一对应（用户符号、地形、展示、彩色、灰度…）：
取消勾选并确定后，该分组不再出现在符号选择器中。

外部样式只接受 QGIS 可读的 **XML / QML**；ArcMap 的 ``.style`` 是 ESRI 私有
二进制格式，本功能不提供也不假装支持。
"""

from __future__ import annotations

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.message_box import MessageBox
from app.ui.dialogs.layer_properties.symbol_libraries import (
    all_groups,
    delete_symbol_library_file,
    load_hidden_groups,
    load_libraries,
    save_hidden_groups,
    save_libraries,
    symbols_dir,
)

logger = get_logger(__name__)


class StyleReferenceDialog(BaseDialog):
    """勾选符号分组是否显示 / 添加外部 QGIS 样式。"""

    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent, "样式引用")
        self._kind = kind
        self._hidden = load_hidden_groups()
        self.setMinimumSize(0, 0)
        self.resize(480, 520)
        self._build()
        self._fill()

    def _build(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        hint = QLabel("勾选要在「符号选择器」左侧显示的符号分组：", self)
        hint.setObjectName("PropHint")
        root.addWidget(hint)

        self.list = QListWidget(self)
        self.list.setObjectName("SymbolStyleReferenceList")
        self.list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list.itemChanged.connect(self._on_item_changed)
        self.list.currentItemChanged.connect(self._update_location_hint)
        root.addWidget(self.list, 1)

        # 选中分组时显示它对应的文件位置（用户符号 / 外部样式）
        self.location_label = QLabel("", self)
        self.location_label.setObjectName("PropHint")
        self.location_label.setWordWrap(True)
        self.location_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        root.addWidget(self.location_label)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.default_btn = QPushButton("设置为默认列表", self)
        self.default_btn.setToolTip("恢复全部内置分组并移除外来样式")
        self.default_btn.clicked.connect(self._on_default)
        self.add_btn = QPushButton("将样式添加到列表…", self)
        self.add_btn.setToolTip("添加样式 XML / QML 文件")
        self.add_btn.clicked.connect(self._on_add)
        self.remove_btn = QPushButton("移除选中样式", self)
        self.remove_btn.setToolTip("只移除外来样式分组，内置分组只能取消勾选")
        self.remove_btn.clicked.connect(self._on_remove)
        row.addWidget(self.default_btn)
        row.addWidget(self.add_btn)
        row.addWidget(self.remove_btn)
        root.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.setLayout(root)

    def _fill(self) -> None:
        self.list.blockSignals(True)
        try:
            self.list.clear()
            for group in all_groups(self._kind):
                item = QListWidgetItem(group, self.list)
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                item.setCheckState(
                    Qt.CheckState.Unchecked
                    if group in self._hidden
                    else Qt.CheckState.Checked
                )
                external = self._external_path(group)
                if external:
                    item.setToolTip(
                        f"外部样式文件：\n{external}\n"
                        "取消勾选只是隐藏；「移除选中样式」可移除并可同时删除文件"
                    )
                elif group == "用户符号":
                    item.setToolTip(
                        f"用户符号目录：\n{symbols_dir()}\n"
                        "每个符号一个 XML；在符号选择器里选中用户符号后点「删除符号」"
                    )
                else:
                    item.setToolTip("内置符号分组（随程序提供），只能取消勾选隐藏，不能删除")
                self.list.addItem(item)
            if self.list.count():
                self.list.setCurrentRow(0)
        finally:
            self.list.blockSignals(False)
        self._update_location_hint()

    # ------------------------------------------------------------- 文件位置

    def _external_path(self, group: str) -> str:
        """分组名 → 外部样式文件路径；不是外部样式返回空串。"""
        for entry in load_libraries():
            if (
                entry.get("kind") == "external"
                and str(entry.get("name") or "") == group
            ):
                return str(entry.get("path") or "")
        return ""

    def _update_location_hint(self, *_args) -> None:
        """选中分组时在列表下方显示对应的文件位置。"""
        item = self.list.currentItem()
        group = item.text() if item is not None else ""
        external = self._external_path(group)
        if external:
            self.location_label.setText(f"外部样式文件：{external}")
        elif group == "用户符号":
            self.location_label.setText(f"用户符号目录：{symbols_dir()}")
        else:
            self.location_label.setText("")

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        group = item.text()
        if item.checkState() == Qt.CheckState.Checked:
            self._hidden.discard(group)
        else:
            self._hidden.add(group)

    def _on_default(self) -> None:
        answer = MessageBox.question(
            self,
            "设置为默认列表",
            "恢复全部内置符号分组，并移除已添加的外部样式？",
            MessageBox.StandardButton.Yes | MessageBox.StandardButton.No,
            MessageBox.StandardButton.No,
        )
        if answer != MessageBox.StandardButton.Yes:
            return
        self._hidden.clear()
        entries = [e for e in load_libraries() if e.get("kind") != "external"]
        for entry in entries:
            entry["enabled"] = True
        save_libraries(entries)
        self._fill()

    def _on_add(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "将样式添加到列表",
            "",
            "样式文件 (*.xml *.qml);;所有文件 (*)",
        )
        if not path:
            return
        if not os.path.isfile(path):
            MessageBox.warning(self, "样式引用", "文件不存在。")
            return
        entries = load_libraries()
        if any(
            entry.get("kind") == "external" and entry.get("path") == path
            for entry in entries
        ):
            MessageBox.information(self, "样式引用", "该样式已在列表中。")
            return
        name = os.path.splitext(os.path.basename(path))[0]
        entries.append(
            {
                "id": path,
                "name": name,
                "kind": "external",
                "path": path,
                "enabled": True,
            }
        )
        save_libraries(entries)
        self._fill()

    def _on_remove(self) -> None:
        item = self.list.currentItem()
        if item is None:
            MessageBox.information(self, "样式引用", "请先选中要移除的分组。")
            return
        group = item.text()
        entries = load_libraries()
        external = next(
            (
                entry
                for entry in entries
                if entry.get("kind") == "external"
                and str(entry.get("name") or "") == group
            ),
            None,
        )
        remaining = [
            entry
            for entry in entries
            if not (
                entry.get("kind") == "external" and entry.get("name") == group
            )
        ]
        if len(remaining) == len(entries):
            MessageBox.information(
                self, "样式引用", "内置符号分组不能移除，只能取消勾选。"
            )
            return
        save_libraries(remaining)
        self._hidden.discard(group)
        path = str(external.get("path") or "") if external is not None else ""
        if path:
            # 移除注册后询问是否连文件一起删（外部 XML / QML 是我们自己加进来的）
            answer = MessageBox.question(
                self,
                "移除样式",
                f"已从列表移除「{group}」。\n是否同时删除符号库文件？\n\n{path}",
                MessageBox.StandardButton.Yes | MessageBox.StandardButton.No,
                MessageBox.StandardButton.No,
            )
            if answer == MessageBox.StandardButton.Yes and not delete_symbol_library_file(path):
                MessageBox.warning(self, "样式引用", "删除文件失败，详见日志。")
        self._fill()

    def accept(self) -> None:
        save_hidden_groups(self._hidden)
        super().accept()
