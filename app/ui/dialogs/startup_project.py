"""启动时「打开地图文档」对话框（ArcMap「Getting Started」风格）。

登录校验通过、主窗口显示后由 :func:`app.bootstrap._make_open_project_callback`
立即弹出，**替代系统文件选择框**：

- 左侧分类导航：``新建工程`` / ``最近打开`` / ``浏览…``（参考 ArcMap 启动
  对话框的左栏）;
- 右侧列出最近打开的工程（名称 / 位置 / 修改时间），双击或选中后「打开」即
  载入；列表为空时给出引导文案；
- 底部「文件名」框回显选中路径，也可直接输入 / 粘贴路径后回车；
- 「浏览…」仍复用系统文件框（``open_file_dir``），选完立即打开。

对话框只负责“选哪个工程”，真正的读盘 / 刷新画布交给主窗口
(:meth:`ProjectOpsMixin.open_project_path`); 因此这里不 import 任何主窗口模块。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor, QFont
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.common.base_dialog import BaseDialog
from app.ui.common.message_box import MessageBox
from app.ui.common.recent_projects import load_recent
from app.ui.common.theme import ThemeManager, startup_project_qss
from app.ui.common.widgets import open_file_dir

#: 工程文件过滤器（与 ProjectOpsMixin 的打开/另存保持一致）
_PROJECT_FILTER = "工程文件 (*.pgs);;所有文件 (*)"

#: 左侧导航项携带的类别标识
_KIND_NEW = "new"
_KIND_RECENT = "recent"
_KIND_BROWSE = "browse"

_DEFAULT_APP_NAME = "qgis_dev"


class StartupProjectDialog(BaseDialog):
    """启动时选择要打开的工程（ArcMap「打开地图文档」风格）。"""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        app_name: str = _DEFAULT_APP_NAME,
        recent: Optional[list[str]] = None,
    ) -> None:
        super().__init__(parent, title=f"打开地图文档 - {app_name}")
        # 覆盖 BaseDialog 的 800×600 最小尺寸，做成紧凑的“启动选择”窗口
        self.setMinimumSize(0, 0)
        self.resize(760, 480)
        self.setModal(True)

        self._app_name = app_name or _DEFAULT_APP_NAME
        self._recent: list[str] = list(recent) if recent is not None else load_recent()
        self._create_new = False
        self._nav_recent: Optional[QListWidgetItem] = None
        self._theme = ThemeManager.instance().current()

        self._build_ui()

    # ============================================================== UI

    def _build_ui(self) -> None:
        self.setObjectName("StartupProjectDialog")
        self.setStyleSheet(startup_project_qss(self._theme))

        outer = QVBoxLayout()
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(4)

        heading = QLabel("打开地图文档")
        heading.setObjectName("StartupHeading")
        sub = QLabel("选择一个最近使用的工程，或浏览计算机查找工程文件（.pgs）")
        sub.setObjectName("StartupSub")
        outer.addWidget(heading)
        outer.addWidget(sub)
        outer.addSpacing(10)

        # ---- 主体：左导航 + 右最近列表 ----
        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(self._build_nav())

        right = QVBoxLayout()
        right.setSpacing(4)
        self._tree = QTreeWidget()
        self._tree.setObjectName("StartupRecent")
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["名称", "位置", "修改时间"])
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tree.setColumnWidth(0, 240)
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._populate_recent()
        self._tree.itemSelectionChanged.connect(self._on_recent_selected)
        self._tree.itemDoubleClicked.connect(lambda *_: self._open_selected())
        right.addWidget(self._tree, 1)
        body.addLayout(right, 1)
        outer.addLayout(body, 1)

        outer.addSpacing(10)

        # ---- 文件名行 ----
        file_row = QHBoxLayout()
        file_row.setSpacing(8)
        file_label = QLabel("文件名(&N):")
        file_label.setObjectName("StartupFieldLabel")
        self._path_edit = QLineEdit()
        self._path_edit.setObjectName("StartupPath")
        self._path_edit.setPlaceholderText("工程文件路径（.pgs）")
        self._path_edit.textChanged.connect(self._refresh_open_button)
        self._path_edit.returnPressed.connect(self._open_selected)
        browse_btn = QPushButton("浏览…")
        browse_btn.setObjectName("StartupSecondary")
        browse_btn.setAutoDefault(False)
        browse_btn.clicked.connect(self._browse)
        file_row.addWidget(file_label)
        file_row.addWidget(self._path_edit, 1)
        file_row.addWidget(browse_btn)
        outer.addLayout(file_row)

        # ---- 操作按钮 ----
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._open_btn = QPushButton("打开")
        self._open_btn.setObjectName("StartupPrimary")
        self._open_btn.setDefault(True)
        self._open_btn.clicked.connect(self._open_selected)
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("StartupSecondary")
        cancel_btn.setAutoDefault(False)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._open_btn)
        btn_row.addWidget(cancel_btn)
        outer.addLayout(btn_row)

        self.setLayout(outer)
        self._refresh_open_button()

    def _build_nav(self) -> QListWidget:
        """左侧分类导航（ArcMap 左栏）。"""
        nav = QListWidget()
        nav.setObjectName("StartupNav")
        nav.setFixedWidth(156)
        nav.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._add_nav(nav, "新建", header=True)
        self._add_nav(
            nav,
            "新建工程",
            _KIND_NEW,
            icon=self.style().StandardPixmap.SP_FileDialogNewFolder,
        )
        self._add_nav(nav, "打开现有地图", header=True)
        self._nav_recent = self._add_nav(
            nav,
            "最近打开",
            _KIND_RECENT,
            icon=self.style().StandardPixmap.SP_FileDialogDetailedView,
        )
        self._add_nav(
            nav,
            "浏览…",
            _KIND_BROWSE,
            icon=self.style().StandardPixmap.SP_DirOpenIcon,
        )
        nav.setCurrentItem(self._nav_recent)
        nav.itemClicked.connect(self._on_nav_clicked)
        return nav

    def _add_nav(
        self,
        nav: QListWidget,
        text: str,
        kind: Optional[str] = None,
        *,
        header: bool = False,
        icon=None,
    ) -> QListWidgetItem:
        item = QListWidgetItem(text)
        if header:
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            font = QFont(item.font())
            font.setBold(True)
            item.setFont(font)
            item.setForeground(QBrush(QColor(self._theme.text_secondary)))
            item.setData(Qt.ItemDataRole.UserRole, None)
        else:
            item.setData(Qt.ItemDataRole.UserRole, kind)
            if icon is not None:
                item.setIcon(self.style().standardIcon(icon))
        nav.addItem(item)
        return item

    def _populate_recent(self) -> None:
        """填右侧最近工程列表；为空时放一条占位项。"""
        self._tree.clear()
        if not self._recent:
            placeholder = QTreeWidgetItem(["（暂无最近打开的工程）", "", ""])
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            placeholder.setForeground(0, QBrush(QColor(self._theme.text_secondary)))
            self._tree.addTopLevelItem(placeholder)
            return
        icon = self.style().standardIcon(self.style().StandardPixmap.SP_FileIcon)
        for path in self._recent:
            name, location, mtime = _describe(path)
            item = QTreeWidgetItem([name, location, mtime])
            item.setIcon(0, icon)
            item.setToolTip(0, path)
            item.setToolTip(1, path)
            item.setData(0, Qt.ItemDataRole.UserRole, path)
            self._tree.addTopLevelItem(item)

    # ============================================================== 交互

    def _on_nav_clicked(self, item: QListWidgetItem) -> None:
        kind = item.data(Qt.ItemDataRole.UserRole)
        if kind == _KIND_NEW:
            self._create_new = True
            self.accept()
        elif kind == _KIND_BROWSE:
            self._browse()
            # 取消浏览 → 回到“最近打开”，避免停在“浏览…”上
            if self.result() != QDialog.DialogCode.Accepted and self._nav_recent is not None:
                item.listWidget().setCurrentItem(self._nav_recent)

    def _on_recent_selected(self) -> None:
        selected = self._tree.selectedItems()
        path = selected[0].data(0, Qt.ItemDataRole.UserRole) if selected else ""
        if path:
            self._path_edit.setText(path)

    def _browse(self) -> None:
        """系统文件框选工程；选到则回填并立即打开。"""
        path = open_file_dir(
            self,
            is_dir=False,
            title="打开地图文档",
            filter_=_PROJECT_FILTER,
        )
        if path:
            self._path_edit.setText(path)
            self.accept()

    def _open_selected(self) -> None:
        path = self._path_edit.text().strip()
        if not path:
            MessageBox.information(self, "提示", "请选择或输入要打开的工程文件")
            return
        if not Path(path).is_file():
            MessageBox.warning(self, "打开失败", f"文件不存在：\n{path}")
            return
        self.accept()

    def _refresh_open_button(self, *_args) -> None:
        self._open_btn.setEnabled(bool(self._path_edit.text().strip()))

    # ============================================================== 取值

    def selected_path(self) -> str:
        """用户选择的工程路径（未选/取消时为空串）。"""
        return self._path_edit.text().strip()

    def create_new(self) -> bool:
        """用户是否选择了「新建工程」。"""
        return self._create_new


def _describe(path: str) -> tuple[str, str, str]:
    """返回 (名称, 所在目录, 修改时间文本)。"""
    p = Path(path)
    try:
        mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        mtime = ""
    return p.stem, str(p.parent), mtime


__all__ = ["StartupProjectDialog"]
