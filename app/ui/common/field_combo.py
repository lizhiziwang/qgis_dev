"""字段下拉通用增强：可输入过滤 + 精确匹配落位 + 无效输入回退。

很多对话框里的字段下拉只是普通 ``QComboBox``：字段一多（几十个、带别名）就
只能滚列表找。这里统一加一层轻量配置，不改变调用方的取值方式（仍用
``currentData()`` / ``currentText()``）：

- 可编辑 + ``QCompleter``（MatchContains、忽略大小写），输入字段名 / 别名即可筛选；
- 从补全列表选中、失焦或回车时，按显示文本 / itemData / 别名精确落回对应项；
- 手输了不存在的字段：恢复上一次选中项，并把原因写进 tooltip（不弹模态框）；
- 不添加清除按钮，避免把"没选字段"变成调用方没准备好的 ``-1`` 状态。

**不用 ``lineEdit().editingFinished``**：在隐藏窗口 / 无焦点控件上手动触发该信号
会走到 QComboBox 内部槽，实测能把进程打崩（127/139）。这里改用事件过滤器 +
``QTimer.singleShot(0)``：等失焦/回车事件彻底走完再落位，不重入。

适用：字段数不多、但希望快速定位的普通下拉。300+ 字段的映射表仍建议用
``app/ui/dialogs/copy_to_layer.py`` 里的惰性填充方案。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QEvent, QObject, Qt, QTimer
from qgis.PyQt.QtWidgets import QComboBox, QCompleter

#: 显示文本里可能出现的「字段名 / 别名」分隔符
_SEPARATORS = (" · ", "（", "(")


def _candidates(combo: QComboBox, index: int) -> set[str]:
    """某一项可用于精确匹配的文本：显示文本、itemData、分隔出的字段名/别名。"""
    out: set[str] = set()
    display = combo.itemText(index).strip()
    if display:
        out.add(display)
    data = combo.itemData(index)
    if data is not None and str(data).strip():
        out.add(str(data).strip())
    for sep in _SEPARATORS:
        if sep not in display:
            continue
        left, right = display.split(sep, 1)
        if left.strip():
            out.add(left.strip())
        right = right.rstrip("）)").strip()
        if right:
            out.add(right)
        break
    return out


def _find_index(combo: QComboBox, text: str) -> int:
    """按显示文本 / itemData / 字段名 / 别名精确匹配；找不到返回 -1。"""
    target = text.strip().casefold()
    if not target:
        return -1
    for i in range(combo.count()):
        if any(target == candidate.casefold() for candidate in _candidates(combo, i)):
            return i
    return -1


class _FieldComboFilter(QObject):
    """监听字段下拉输入框的失焦 / 回车，等事件走完再提交或回退。"""

    def __init__(self, combo: QComboBox, *, warn_invalid: bool) -> None:
        super().__init__(combo)
        self._combo = combo
        self._warn_invalid = warn_invalid

    def eventFilter(self, obj, event):  # noqa: N802 —— Qt 命名
        if event.type() == QEvent.Type.FocusOut:
            QTimer.singleShot(0, self._commit_or_revert)
        elif (
            event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        ):
            QTimer.singleShot(0, self._commit_or_revert)
        elif (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            # 可编辑下拉默认只有点箭头才展开；点输入区也自动弹出候选
            QTimer.singleShot(0, self._show_popup)
        return False

    def _show_popup(self) -> None:
        """点输入区时展开候选。

        **用 completer.complete() 而不是 combo.showPopup()**：后者会把键盘焦点
        交给弹出列表（QListView），用户就无法继续在输入框里退格 / 输入过滤。
        completer 弹层保持 line edit 焦点，输入、删除、方向键选候选都正常。
        """
        combo = self._combo
        try:
            completer = combo.completer()
            if completer is None:
                return
            # 先确保键盘焦点在输入框，再展开全部候选：点击时不要用当前选中值
            # 当过滤前缀，否则只会列出当前字段一项。
            combo.lineEdit().setFocus()
            completer.setCompletionPrefix("")
            completer.complete()
        except RuntimeError:  # 控件已销毁
            pass

    def _commit_or_revert(self) -> None:
        combo = self._combo
        try:
            text = combo.currentText().strip()
        except RuntimeError:  # 控件已销毁
            return
        if not text:
            return
        index = _find_index(combo, text)
        if index >= 0:
            if index != combo.currentIndex():
                combo.setCurrentIndex(index)
            return
        # 不存在：恢复上一次选中项的显示文本（输入过程中 currentIndex 不会变）
        previous = combo.currentIndex()
        was_blocked = combo.blockSignals(True)
        try:
            if 0 <= previous < combo.count():
                combo.setEditText(combo.itemText(previous))
            else:
                combo.setEditText("")
        finally:
            combo.blockSignals(was_blocked)
        if self._warn_invalid:
            restored = combo.currentText().strip()
            tip = (
                f"字段「{text}」不存在，已恢复为「{restored}」"
                if restored
                else f"字段「{text}」不存在"
            )
            combo.setToolTip(tip)


def make_field_combo_searchable(
    combo: QComboBox,
    *,
    placeholder: str = "输入字段名 / 别名筛选…",
    warn_invalid: bool = True,
) -> QComboBox:
    """把普通字段下拉升级为可搜索下拉（幂等，可重复调用）。

    Args:
        combo: 已有的字段下拉；调用方继续用 ``currentData()`` / ``currentText()``。
        placeholder: 输入框占位文案。
        warn_invalid: 手输不存在字段时是否回退并把原因写进 tooltip。默认开启。

    Returns:
        同一个 ``combo``，便于链式调用。
    """
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    # 复用条件页那套「紧凑箭头」QSS；已有 objectName 的不要覆盖
    if not combo.objectName():
        combo.setObjectName("FieldSearchCombo")

    line_edit = combo.lineEdit()
    if line_edit is not None:
        line_edit.setPlaceholderText(placeholder)
        # 不启用原生清除按钮：清空会进入"无字段"状态，调用方不一定处理 -1
        line_edit.setClearButtonEnabled(False)
        # 事件过滤器挂在 combo 上保活（line edit 是 combo 的子对象）
        field_filter = _FieldComboFilter(combo, warn_invalid=warn_invalid)
        line_edit.installEventFilter(field_filter)
        combo._field_filter = field_filter  # type: ignore[attr-defined]

    completer = QCompleter(combo.model(), combo)
    completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
    completer.setFilterMode(Qt.MatchFlag.MatchContains)
    completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    completer.setMaxVisibleItems(15)
    combo.setCompleter(completer)

    def _commit_completion(text: str) -> None:
        index = _find_index(combo, text)
        if index >= 0:
            combo.setCurrentIndex(index)

    completer.activated[str].connect(_commit_completion)
    combo.setToolTip("可直接输入字段名 / 别名筛选")
    return combo


__all__ = ["make_field_combo_searchable"]
