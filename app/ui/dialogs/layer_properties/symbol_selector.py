"""ArcMap 式「符号选择器」：左侧符号库 / 右侧当前符号参数。

单一符号渲染页点符号预览打开本对话框：

- **左侧**：搜索框 + 「全部样式 / 收藏符号」筛选 + 分组符号网格
  （收藏 / 用户符号 / QGIS 系统样式标签）。系统样式来自 ``QgsStyle.defaultStyle()``；
  用户符号以 XML 存在配置目录 ``symbols/`` 下（不写 QSettings，便于测试重定向）。
- **右侧**：当前符号预览 + 参数（复用 :class:`SymbolParamsWidget`，面 / 线 / 点各自一套）
  + 另存为 / 收藏 / 重置 / 导入样式。
- 选符号后右侧参数实时回填；改参数时在选中符号的拷贝上原地改（保留 SVG / 多符号层
  等其它属性），实时刷新预览。
"""

from __future__ import annotations

import os

from qgis.core import (
    QgsStyle,
    QgsSymbolLayerUtils,
)
from qgis.gui import QgsColorButton, QgsSymbolSelectorWidget
from qgis.PyQt.QtCore import QEvent, QSize, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.infra.fs.state import load_state, save_state
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.message_box import MessageBox
from app.ui.common.wheel_guard import WheelValueGuard
from app.ui.dialogs.layer_properties.render import (
    SymbolParamsWidget,
    default_symbol,
)
from app.ui.dialogs.layer_properties.style_reference import StyleReferenceDialog
from app.ui.dialogs.layer_properties.symbol_libraries import (
    LibrarySymbol as _LibrarySymbol,
)
from app.ui.dialogs.layer_properties.symbol_libraries import (
    delete_user_symbol,
    libraries_dir,
    load_libraries,
    load_library_symbols,
    safe_file_stem,
    save_libraries,
    save_symbol_library,
    save_user_symbol,
    symbols_dir,
)
from app.ui.dialogs.layer_properties.widgets import pick_color

logger = get_logger(__name__)

_FAVORITES_STATE = "symbol_favorites"
_SWATCH = QSize(40, 40)
_GRID = QSize(80, 80)
#: 符号库网格列数（左侧栏加宽后 5 列：5×80 + 间距仍能放下）
_GRID_COLUMNS = 5
_KIND_NAMES = {"point": "点", "line": "线", "polygon": "面"}


def _favorites() -> set[str]:
    data = load_state(_FAVORITES_STATE)
    values = data.get("symbols")
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values}


def _save_favorites(keys: set[str]) -> None:
    save_state(_FAVORITES_STATE, {"symbols": sorted(keys)})


class SymbolEditorDialog(BaseDialog):
    """符号编辑器：只保留符号层树 + 属性，不再带下面的符号库浏览区。

    使用 ``QgsSymbolSelectorWidget``（QGIS 的符号层编辑控件）而不是
    ``QgsSymbolSelectorDialog`` —— 后者自带搜索/符号列表/保存/高级，本应用用不上。
    """

    #: QGIS 内部符号库列表的 objectName（切回顶层符号时会**重建**一个新实例）
    _LIBRARY_NAME = "mStyleItemsListWidget"

    #: 符号编辑器里不显示的两个勾选项（启用符号图层 / 绘制效果）：
    #: 「启用」还会连带它的数据定义按钮，「绘制效果」整块（勾选 + 效果按钮）都收掉
    _HIDDEN_CHECKBOX_NAMES = frozenset({"mEnabledCheckBox"})
    _HIDDEN_CHECKBOX_TEXTS = frozenset(
        {
            "enable symbol layer",
            "启用符号图层",
            "draw effects",
            "绘制效果",
        }
    )
    _HIDDEN_EXTRA_WIDGETS = ("mEnabledDDBtn", "mEffectWidget")

    def __init__(self, symbol, parent: QWidget | None = None) -> None:
        super().__init__(parent, "符号编辑器")
        self.setMinimumSize(0, 0)
        self.resize(780, 580)
        self._extras_pending = False
        # 已绑统一取色框的颜色按钮（QgsColorButton 会被重建，按对象身份记）
        self._bound_color_buttons: set = set()
        # QgsSymbolSelectorWidget 不接管符号所有权：这里必须一直持有
        self._symbol = symbol.clone() if symbol is not None else None
        layout = QVBoxLayout()
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        self.widget = QgsSymbolSelectorWidget(
            self._symbol, QgsStyle.defaultStyle(), None, self
        )
        # 颜色条右侧留出下拉箭头的空间；数据定义按钮的箭头居中
        self._style_editor_controls()
        # 只隐藏底部的**符号库列表**（搜索 / 列表 / 保存 / 高级），保留上面的
        # 符号层属性面板（pageFill 等）。以前沿着搜索框往上找容器，会一路隐藏
        # 到整个 scrollArea，把属性面板也一起藏掉（用户看到的空白区）。
        self._hide_symbol_library()
        # 切回顶层符号时 QGIS 会重建符号库列表（新的对象、默认可见），隐藏一次
        # 不够；盯住子树里新出现的实例，见到就再隐藏（见 eventFilter）。
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        layout.addWidget(self.widget, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setLayout(layout)
        # 滚轮经过下拉框 / 数值框时不改值，继续滚动（否则用户滚不完整页）
        self._wheel_guard = WheelValueGuard(self)

    # ------------------------------------------------------------- 符号库隐藏

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 —— Qt 命名
        """子树里新出现 / 被重新显示的符号库列表立即再隐藏；顺手收掉多余勾选项。"""
        kind = event.type()
        if kind not in (
            QEvent.Type.ChildAdded,
            QEvent.Type.Show,
            QEvent.Type.ShowToParent,
        ):
            return False
        try:
            inside = isinstance(obj, QWidget) and (
                obj is self.widget or self.widget.isAncestorOf(obj)
            )
        except RuntimeError:  # 控件已销毁
            inside = False
        if inside:
            # 「启用符号图层 / 绘制效果」两行随选中的符号层出现 / 复用，每次
            # 显示过之后都补收一次（延迟到事件栈外，避免在 Show 里直接改布局）
            self._schedule_hide_extras()
            if kind == QEvent.Type.ChildAdded:
                child = event.child()
                if isinstance(child, QWidget):
                    self._hide_symbol_library(child)
                    self._style_editor_control(child)
            elif (
                isinstance(obj, QWidget)
                and obj.objectName() == self._LIBRARY_NAME
            ):
                obj.hide()
                return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------- 编辑器控件样式

    def _style_editor_controls(self) -> None:
        """调整符号编辑器里的颜色条 / 数据定义按钮（构造时调用一次）。"""
        for button in self.widget.findChildren(QgsColorButton):
            self._style_editor_control(button)
        for button in self.widget.findChildren(QToolButton):
            if button.objectName().endswith("DDBtn"):
                self._style_editor_control(button)

    def _style_editor_control(self, widget) -> None:
        """单个控件：颜色条右侧留出箭头位置；数据定义箭头垂直居中。"""
        if isinstance(widget, QgsColorButton):
            widget.setStyleSheet(
                "QgsColorButton { padding-right: 24px; }"
                "QgsColorButton::menu-indicator {"
                " subcontrol-position: center right; right: 2px; }"
            )
            self._bind_color_button(widget)
        elif isinstance(widget, QToolButton) and widget.objectName().endswith(
            "DDBtn"
        ):
            widget.setStyleSheet(
                "QToolButton::menu-indicator {"
                " subcontrol-position: center right; right: 1px; top: 0px; }"
            )
        elif isinstance(widget, QWidget):
            # 新加进来的属性页容器：里面的控件可能比容器先建好，
            # 没经过 ChildAdded，这里补刷一遍
            for button in widget.findChildren(QgsColorButton):
                self._style_editor_control(button)
            for button in widget.findChildren(QToolButton):
                if button.objectName().endswith("DDBtn"):
                    self._style_editor_control(button)

    # ------------------------------------------------------- 统一取色界面

    def _bind_color_button(self, button: QgsColorButton) -> None:
        """把 QGIS 自带的取色框换成应用统一的「选择颜色」界面。

        不能换掉按钮的 C++ 类型，只能改它的行为：``SignalOnly`` 让点击不再弹
        QGIS 的 ``QgsColorDialog``、改发 ``colorClicked``；这里接上自绘取色框，
        选完再 ``setColor`` 写回（按钮自己的 ``colorChanged`` 会把新颜色应用
        到符号层）。右侧下拉箭头（复制 / 粘贴颜色等菜单）不受影响。
        """
        if button in self._bound_color_buttons:
            return
        self._bound_color_buttons.add(button)
        button.setBehavior(QgsColorButton.Behavior.SignalOnly)
        button.setColorDialogTitle("选择颜色")
        # 推迟到事件栈外再弹取色框：在按钮自己的鼠标事件栈里开模态窗 / 回写
        # 颜色（setColor 会连带刷新符号层）实测会把进程打崩（退出码 127）
        # ``colorClicked`` 自带一个 QColor 实参，必须用 ``*_`` 吃掉，否则默认参数
        # 会被信号实参顶掉（拿到的不是按钮）
        button.colorClicked.connect(
            lambda *_, b=button: QTimer.singleShot(
                0, lambda: self._pick_color_for(b)
            )
        )

    def _pick_color_for(self, button: QgsColorButton) -> None:
        """颜色按钮点击：用应用统一的取色框，确定后写回按钮。

        跑在定时器槽里，异常必须自己兜住（未捕获会直接杀进程）。
        """
        try:
            chosen = pick_color(self, button.color())
            if chosen is not None:
                button.setColor(chosen)
        except RuntimeError:  # 按钮 / 窗口已销毁
            pass
        except Exception:  # noqa: BLE001 —— 槽里未捕获异常会原生崩溃
            logger.warning("符号编辑器取色失败", exc_info=True)

    # --------------------------------------------------- 多余的勾选项隐藏

    def _schedule_hide_extras(self) -> None:
        """把「收勾选项」推迟到事件栈外执行（一帧只排一次）。"""
        if self._extras_pending:
            return
        self._extras_pending = True
        QTimer.singleShot(0, self._hide_extras_now)

    def _hide_extras_now(self) -> None:
        self._extras_pending = False
        try:
            self._hide_layer_extras()
        except Exception:  # noqa: BLE001 —— 槽里未捕获异常会原生崩溃
            logger.warning("隐藏符号编辑器多余控件失败", exc_info=True)

    def _hide_layer_extras(self) -> None:
        """隐藏「启用符号图层」「绘制效果」两行（这两项在本应用里用不上）。

        勾选框的文案随 QGIS 语言变化，中英文都认；「启用」行的数据定义按钮
        与「绘制效果」整块（勾选 + 效果按钮）一起收掉，避免剩一个孤零零的图标。
        """
        for box in self.widget.findChildren(QCheckBox):
            text = box.text().replace("&", "").strip().casefold()
            if (
                box.objectName() in self._HIDDEN_CHECKBOX_NAMES
                or text in self._HIDDEN_CHECKBOX_TEXTS
            ):
                box.hide()
        for name in self._HIDDEN_EXTRA_WIDGETS:
            for widget in self.widget.findChildren(QWidget, name):
                widget.hide()

    def _hide_symbol_library(self, root: QWidget | None = None) -> None:
        """隐藏 QGIS 自带的符号库浏览区；``root`` 为刚新增的子树（可选）。"""
        base = root if root is not None else self.widget
        libraries = (
            [base]
            if base.objectName() == self._LIBRARY_NAME
            else base.findChildren(QWidget, self._LIBRARY_NAME)
        )
        for library in libraries:
            library.hide()
        for btn in base.findChildren(QPushButton):
            text = btn.text().strip().lower().rstrip("…. ")
            if text in ("save symbol", "保存符号", "advanced", "高级"):
                btn.hide()

    def done(self, result: int) -> None:  # noqa: N802 —— Qt 命名
        # 卸掉应用级过滤器：对话框销毁后不该再被回调
        app = QApplication.instance()
        if app is not None:
            try:
                app.removeEventFilter(self)
            except RuntimeError:
                pass
        self._wheel_guard.detach()
        super().done(result)

    def selected_symbol(self):
        symbol = self.widget.symbol()
        return symbol.clone() if symbol is not None else None


class SaveSymbolDialog(BaseDialog):
    """另存符号：存到用户符号库，或新建一份符号库文件。"""

    def __init__(self, symbol, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent, "保存符号")
        self._symbol = symbol
        self._kind = kind
        self.setMinimumSize(0, 0)
        self.resize(500, 280)
        self._build()
        self._sync_new_state()

    def _build(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)
        form = QFormLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(8)
        self.name_edit = QLineEdit("符号", self)
        form.addRow("符号名称", self.name_edit)

        radios = QVBoxLayout()
        self.user_radio = QRadioButton("保存到用户符号库", self)
        self.user_radio.setChecked(True)
        self.new_radio = QRadioButton("新建符号库", self)
        radios.addWidget(self.user_radio)
        radios.addWidget(self.new_radio)
        group = QButtonGroup(self)
        group.addButton(self.user_radio)
        group.addButton(self.new_radio)
        form.addRow("保存位置", radios)

        path_row = QHBoxLayout()
        path_row.setSpacing(6)
        self.path_edit = QLineEdit(self)
        self.path_edit.setPlaceholderText("新建符号库文件的保存路径（.xml）")
        path_row.addWidget(self.path_edit, 1)
        self.browse_btn = QPushButton("浏览…", self)
        self.browse_btn.clicked.connect(self._on_browse)
        path_row.addWidget(self.browse_btn)
        form.addRow("符号库文件", path_row)
        root.addLayout(form)
        root.addStretch(1)

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

        self.user_radio.toggled.connect(self._sync_new_state)
        self.new_radio.toggled.connect(self._sync_new_state)

    def _sync_new_state(self, *_args) -> None:
        new = self.new_radio.isChecked()
        self.path_edit.setEnabled(new)
        self.browse_btn.setEnabled(new)
        if new and not self.path_edit.text().strip():
            name = self.name_edit.text().strip() or "符号库"
            self.path_edit.setText(
                str(libraries_dir() / f"{safe_file_stem(name)}.xml")
            )

    def _on_browse(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "新建符号库",
            self.path_edit.text().strip() or str(libraries_dir()),
            "样式文件 (*.xml);;所有文件 (*)",
        )
        if chosen:
            if not chosen.lower().endswith(".xml"):
                chosen = f"{chosen}.xml"
            self.path_edit.setText(chosen)

    def values(self) -> tuple[str, str, str]:
        """``(符号名称, "user" | "library", 符号库路径)``。"""
        name = self.name_edit.text().strip()
        if self.new_radio.isChecked():
            return name, "library", self.path_edit.text().strip()
        return name, "user", ""

    def accept(self) -> None:
        name, mode, path = self.values()
        if not name:
            MessageBox.warning(self, "保存符号", "符号名称不能为空。")
            return
        if mode == "library":
            if not path:
                MessageBox.warning(self, "保存符号", "请选择符号库文件。")
                return
            import os

            if os.path.exists(path):
                MessageBox.warning(
                    self, "保存符号", "该符号库文件已存在，请换一个文件名。"
                )
                return
        super().accept()


class SymbolSelectorDialog(BaseDialog):
    """左侧符号库 / 右侧参数编辑的符号选择器。"""

    def __init__(self, symbol, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent, f"符号选择器 - {_KIND_NAMES.get(kind, kind)}")
        self._kind = kind
        self._current_symbol = symbol.clone() if symbol is not None else default_symbol(kind)
        self._favorites = _favorites()
        self._library_symbols = load_library_symbols(kind)
        self._selected_item: _LibrarySymbol | None = None
        self._symbol_buttons: list[tuple[_LibrarySymbol, QToolButton]] = []
        self._group_widgets: list[QWidget] = []
        self.setMinimumSize(0, 0)
        self.resize(810, 660)
        self._build()
        self._set_current(self._current_symbol)
        self._rebuild_library()
        # 滚轮经过下拉框 / 数值框时不改值，继续滚动（否则用户滚不完整页）
        self._wheel_guard = WheelValueGuard(self)

    def done(self, result: int) -> None:  # noqa: N802 —— Qt 命名
        self._wheel_guard.detach()
        super().done(result)

    # ------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = QHBoxLayout()
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        # 左：搜索 + 筛选 + 符号库
        left = QWidget(self)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(8)

        self.search_edit = QLineEdit(left)
        self.search_edit.setPlaceholderText("在此输入以进行搜索")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._rebuild_library)
        left_lay.addWidget(self.search_edit)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)
        filter_row.addWidget(QLabel("搜索:", left))
        self.all_radio = QRadioButton("全部样式", left)
        self.all_radio.setChecked(True)
        self.fav_radio = QRadioButton("收藏符号", left)
        group = QButtonGroup(self)
        group.addButton(self.all_radio)
        group.addButton(self.fav_radio)
        self.all_radio.toggled.connect(self._rebuild_library)
        filter_row.addWidget(self.all_radio)
        filter_row.addWidget(self.fav_radio)
        filter_row.addStretch(1)
        left_lay.addLayout(filter_row)

        self.scroll = QScrollArea(left)
        self.scroll.setWidgetResizable(True)
        self.scroll_body = QWidget(self.scroll)
        self.scroll_lay = QVBoxLayout(self.scroll_body)
        self.scroll_lay.setContentsMargins(4, 8, 4, 8)
        self.scroll_lay.setSpacing(8)
        self.scroll.setWidget(self.scroll_body)
        left_lay.addWidget(self.scroll, 1)
        # 左侧符号网格固定 5 列：栏宽加宽后能多放一列，右侧参数区不被挤窄
        left.setFixedWidth(470)
        root.addWidget(left, 0)

        # 右：当前符号 + 参数 + 操作按钮（固定约 300px，左侧符号库占主宽度）
        right = QWidget(self)
        right.setFixedWidth(300)
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(8)
        title = QLabel("当前符号", right)
        title.setObjectName("SectionLabel")
        right_lay.addWidget(title)
        self.preview = QLabel(right)
        self.preview.setFixedSize(130, 80)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet(
            "QLabel { border: 1px solid palette(mid); background: palette(base);"
            " border-radius: 3px; }"
        )
        right_lay.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignLeft)

        self.params = SymbolParamsWidget(self._kind, right)
        self.params.changed.connect(self._on_params_changed)
        right_lay.addWidget(self.params)

        ops = QGridLayout()
        ops.setSpacing(6)
        self.edit_btn = QPushButton("编辑符号…", right)
        self.edit_btn.setToolTip("打开完整符号编辑器（多符号层 / 数据定义属性）")
        self.edit_btn.clicked.connect(self._on_edit_symbol)
        self.save_btn = QPushButton("另存为…", right)
        self.save_btn.setToolTip("把当前符号保存到用户符号库")
        self.save_btn.clicked.connect(self._on_save_symbol)
        self.fav_btn = QPushButton("收藏符号", right)
        self.fav_btn.setToolTip("收藏 / 取消收藏当前选中的库符号")
        self.fav_btn.setEnabled(False)
        self.fav_btn.clicked.connect(self._on_toggle_favorite)
        self.delete_btn = QPushButton("删除符号", right)
        self.delete_btn.setToolTip("从用户符号库删除选中的符号（只对用户符号可用）")
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self._on_delete_symbol)
        self.reset_btn = QPushButton("重置", right)
        self.reset_btn.setToolTip("恢复该几何类型的默认符号")
        self.reset_btn.clicked.connect(self._on_reset)
        self.import_btn = QPushButton("样式引用…", right)
        self.import_btn.setToolTip("导入样式 XML / QML 符号")
        self.import_btn.clicked.connect(self._on_style_reference)
        for index, btn in enumerate(
            (
                self.edit_btn,
                self.save_btn,
                self.fav_btn,
                self.delete_btn,
                self.reset_btn,
                self.import_btn,
            )
        ):
            ops.addWidget(btn, index // 2, index % 2)
        right_lay.addLayout(ops)
        right_lay.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        right_lay.addWidget(buttons)
        root.addWidget(right, 1)
        self.setLayout(root)

    # ------------------------------------------------------------- 符号库

    def _rebuild_library(self, *_args) -> None:
        """按搜索词 / 收藏筛选重建左侧符号网格。"""
        term = self.search_edit.text().strip().casefold()
        favorites_only = self.fav_radio.isChecked()
        entries = list(self._library_symbols)
        if favorites_only:
            entries = [item for item in entries if item.key in self._favorites]
        if term:
            entries = [
                item
                for item in entries
                if term in item.name.casefold() or term in item.group.casefold()
            ]

        # 清空旧内容
        while self.scroll_lay.count():
            item = self.scroll_lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._group_widgets.clear()
        self._symbol_buttons.clear()

        groups: dict[str, list[_LibrarySymbol]] = {}
        for entry in entries:
            groups.setdefault(entry.group, []).append(entry)
        if not groups:
            hint = QLabel("没有匹配的符号。", self.scroll_body)
            hint.setObjectName("PropHint")
            self.scroll_lay.addWidget(hint)
            self.scroll_lay.addStretch(1)
            return
        group_order = sorted(groups, key=lambda name: name.casefold())
        for priority in ("通用符号", "用户符号"):
            if priority in group_order:
                group_order.remove(priority)
                group_order.insert(0, priority)
        # 目标是 用户符号 → 通用符号 → 其它；上面的循环先插通用再插用户，
        # 最终用户在最前、通用第二。
        for group_name in group_order:
            self.scroll_lay.addWidget(self._group_widget(group_name, groups[group_name]))
        self.scroll_lay.addStretch(1)

    def _group_widget(self, name: str, entries: list[_LibrarySymbol]) -> QWidget:
        box = QWidget(self.scroll_body)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(4, 6, 4, 6)
        lay.setSpacing(6)
        label = QLabel(name, box)
        label.setStyleSheet("font-weight: 600; color: palette(highlight);")
        lay.addWidget(label)

        host = QWidget(box)
        grid = QGridLayout(host)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(8)
        grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        for index, entry in enumerate(entries):
            btn = QToolButton(host)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            btn.setIcon(QgsSymbolLayerUtils.symbolPreviewIcon(entry.symbol, _SWATCH))
            btn.setIconSize(_SWATCH)
            btn.setText(entry.name)
            btn.setToolTip(f"{entry.name}\n分组：{entry.group}")
            btn.setFixedSize(_GRID)
            btn.setAutoRaise(True)
            btn.setCheckable(True)
            if self._selected_item is not None and entry.key == self._selected_item.key:
                btn.setChecked(True)
            btn.clicked.connect(
                lambda _checked=False, item=entry: self._on_library_entry_clicked(item)
            )
            grid.addWidget(
                btn,
                index // _GRID_COLUMNS,
                index % _GRID_COLUMNS,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            )
            self._symbol_buttons.append((entry, btn))
        lay.addWidget(host)
        return box

    def _on_library_entry_clicked(self, entry: _LibrarySymbol) -> None:
        self._selected_item = entry
        for item, button in self._symbol_buttons:
            button.setChecked(item.key == entry.key)
        self.fav_btn.setEnabled(True)
        self.fav_btn.setText(
            "取消收藏" if entry.key in self._favorites else "收藏符号"
        )
        # 只有用户符号库里的单个 XML 才能直接从界面删
        self.delete_btn.setEnabled(entry.source == "user")
        self._set_current(entry.symbol)

    # ------------------------------------------------------------- 当前符号

    def _set_current(self, symbol) -> None:
        self._current_symbol = symbol.clone() if symbol is not None else default_symbol(self._kind)
        self.params.set_symbol(self._current_symbol)
        self._refresh_current_preview()

    def _refresh_current_preview(self) -> None:
        try:
            pixmap = QgsSymbolLayerUtils.symbolPreviewPixmap(
                self._current_symbol, QSize(118, 68)
            )
            self.preview.setPixmap(pixmap)
        except Exception:  # noqa: BLE001
            self.preview.clear()

    def _on_params_changed(self) -> None:
        symbol = self.params.build_symbol()
        if symbol is None:
            return
        self._current_symbol = symbol
        self._refresh_current_preview()

    def selected_symbol(self):
        """确定的符号（拷贝，调用方自己持有）。"""
        return (
            self._current_symbol.clone()
            if self._current_symbol is not None
            else default_symbol(self._kind)
        )

    # ------------------------------------------------------------- 操作

    def _on_edit_symbol(self) -> None:
        """符号编辑器：只保留符号层树 / 属性（不带符号库浏览区）。"""
        dlg = SymbolEditorDialog(self._current_symbol, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            symbol = dlg.selected_symbol()
            if symbol is not None:
                self._set_current(symbol)
        dlg.deleteLater()

    def _on_save_symbol(self) -> None:
        dlg = SaveSymbolDialog(self._current_symbol, self._kind, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            dlg.deleteLater()
            return
        name, mode, path = dlg.values()
        dlg.deleteLater()
        if mode == "user":
            stem = save_user_symbol(name, self._current_symbol)
            if stem is None:
                MessageBox.warning(self, "保存符号", "保存失败，详见日志。")
                return
            message = f"已保存到用户符号库：{stem}"
        else:
            if not save_symbol_library(path, name, self._current_symbol):
                MessageBox.warning(self, "保存符号", "写入符号库失败，详见日志。")
                return
            entries = load_libraries()
            if not any(entry.get("path") == path for entry in entries):
                entries.append(
                    {
                        "id": path,
                        "name": os.path.splitext(os.path.basename(path))[0],
                        "kind": "external",
                        "path": path,
                        "enabled": True,
                    }
                )
                save_libraries(entries)
            message = f"已新建符号库并加载：{path}"
        # 保存后刷新当前选择器，并尽量选中刚存的那个符号
        self._library_symbols = load_library_symbols(self._kind)
        self._rebuild_library()
        key_match = (
            (lambda entry: entry.source == "user" and entry.name == name)
            if mode == "user"
            else (
                lambda entry: entry.source == "external"
                and str(path) in entry.ident
                and entry.name == name
            )
        )
        for entry in self._library_symbols:
            if key_match(entry):
                self._on_library_entry_clicked(entry)
                break
        MessageBox.information(self, "保存符号", message)

    def _on_delete_symbol(self) -> None:
        """删除用户符号库里的当前符号（对应 ``config/symbols/xxx.xml``）。"""
        entry = self._selected_item
        if entry is None or entry.source != "user":
            MessageBox.information(
                self, "删除符号", "只能删除「用户符号」分组里的符号。"
            )
            return
        path = symbols_dir() / f"{entry.ident}.xml"
        answer = MessageBox.question(
            self,
            "删除符号",
            f"确定从用户符号库中删除「{entry.name}」吗？\n\n文件：{path}",
            MessageBox.StandardButton.Yes | MessageBox.StandardButton.No,
            MessageBox.StandardButton.No,
        )
        if answer != MessageBox.StandardButton.Yes:
            return
        if not delete_user_symbol(entry.ident):
            MessageBox.warning(self, "删除符号", "删除失败，详见日志。")
            return
        if entry.key in self._favorites:
            self._favorites.discard(entry.key)
            _save_favorites(self._favorites)
        self._selected_item = None
        self.fav_btn.setEnabled(False)
        self.fav_btn.setText("收藏符号")
        self.delete_btn.setEnabled(False)
        self._library_symbols = load_library_symbols(self._kind)
        self._rebuild_library()

    def _on_toggle_favorite(self) -> None:
        entry = self._selected_item
        if entry is None:
            MessageBox.information(self, "收藏符号", "请先在左侧选择一个符号。")
            return
        if entry.key in self._favorites:
            self._favorites.discard(entry.key)
        else:
            self._favorites.add(entry.key)
        _save_favorites(self._favorites)
        self.fav_btn.setText(
            "取消收藏" if entry.key in self._favorites else "收藏符号"
        )
        if self.fav_radio.isChecked():
            self._rebuild_library()

    def _on_reset(self) -> None:
        self._set_current(default_symbol(self._kind))
        self._selected_item = None
        self.fav_btn.setEnabled(False)
        self.fav_btn.setText("收藏符号")
        self.delete_btn.setEnabled(False)

    def _on_style_reference(self) -> None:
        """样式引用：管理要显示的符号库（勾选启用 / 导入外部样式）。"""
        dlg = StyleReferenceDialog(self._kind, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._library_symbols = load_library_symbols(self._kind)
            self._rebuild_library()
        dlg.deleteLater()
