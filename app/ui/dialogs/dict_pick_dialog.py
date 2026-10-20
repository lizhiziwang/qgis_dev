"""字典挂接对话框（创建数据字段 / 数据管理字段编辑共用）。

口径：**挂接的必须是当前库自己的字典表**（不再直接列系统字典）——

- 顶部下拉只列当前本地库 ``user_relation(type='dict')`` 里登记过的字典表；
- 当前库还没有字典表时，下拉为空，但可以现场：
  - 「添加字典表…」新建空表，或从「字典管理」的系统字典整表复制；
  - 「导入外部字典…」从 Excel（.xls / .xlsx）或另一个本地库（.gpkg /
    .sqlite / .db）的字典表导入；
- 「字典筛选」+ 查找 / 清除；
- 一个单列树：**不挂接字典域** / **推荐挂接字典域** / **其它字典域**；
- 推荐项按「字典域名与字段名 / 字段别名」的相似度计算；
- 底部 确定 / 取消。

数据管理与创建数据向导都传 ``local_db_path``（目标库 / 即将建出的库文件）。
创建数据向导里目标格式装不下字典表（shp / gdb / geojson…）时传空串，对话框
直接说明"当前数据格式不支持字典表"并禁用确定。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.core.domain.dict_manage import DictError, DictService
from app.core.util.local_db_util import local_db_util
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import open_file_dir
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

logger = get_logger(__name__)

_ROLE_KIND = int(Qt.ItemDataRole.UserRole)
_ROLE_DOMAIN = _ROLE_KIND + 1
#: 搜索结果里，该字典域属于 ``self._entries`` 的第几项
_ROLE_ENTRY = _ROLE_KIND + 2

_K_NONE = "none"
_K_GROUP = "group"
_K_DOMAIN = "domain"

_REC_THRESHOLD = 0.5
_REC_LIMIT = 10

_SPLIT_RE = re.compile(r"[-–—]")


@dataclass
class _Entry:
    source: str  # 目前恒为 "local"（字典挂接只允许当前库）
    category: str
    table: str = ""
    domains: list[tuple[str, int]] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.table and self.table != self.category:
            return f"当前库：{self.category}（{self.table}）"
        return f"当前库：{self.category}"


class DictPickDialog(BaseDialog):
    """选一个字典域（只列当前库里的字典表）。"""

    def __init__(
        self,
        parent=None,
        *,
        service: DictService | None = None,
        category: str = "",
        domain: str = "",
        field_name: str = "",
        field_alias: str = "",
        local_db_path: str = "",
        local_table: str = "",
    ) -> None:
        super().__init__(parent, title="字典挂接", show_min_button=False, show_max_button=True)
        self._service = service if service is not None else DictService()
        self._initial_category = str(category or "")
        self._initial_domain = str(domain or "")
        self._initial_local_table = str(local_table or "")
        self._local_db_path = str(local_db_path or "")
        self._field_name = str(field_name or "").strip()
        self._field_alias = str(field_alias or "").strip()
        self._entries: list[_Entry] = []
        self._domains: list[tuple[str, int]] = []
        #: 程序化切「字典表」下拉（搜索结果跳表）时不要触发 _on_category_changed
        self._switching = False

        self.setMinimumSize(0, 0)
        self.resize(720, 640)
        self._build_ui()
        self._load_categories()

    # ------------------------------------------------------------ 界面

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        row = QHBoxLayout()
        row.addWidget(QLabel("字典表："))
        self.category_combo = QComboBox()
        self.category_combo.setMinimumWidth(300)
        self.category_combo.currentIndexChanged.connect(self._on_category_changed)
        row.addWidget(self.category_combo, 1)
        # 当前库没有字典时，这两个按钮就是出口：新建（可从系统字典复制）/ 导入外部字典
        self.add_btn = QPushButton("添加字典表…")
        self.add_btn.setToolTip("在当前库里新建字典表（可复制「字典管理」里已配置的字典）")
        self.add_btn.clicked.connect(self._add_local_dict)
        row.addWidget(self.add_btn)
        self.import_btn = QPushButton("导入外部字典…")
        self.import_btn.setToolTip("从 Excel 或其它本地库导入一张字典表到当前库")
        self.import_btn.clicked.connect(self._import_external_dict)
        row.addWidget(self.import_btn)
        layout.addLayout(row)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("字典筛选："))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("搜索字典域（跨当前库的全部字典表）")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.returnPressed.connect(self._apply_filter)
        self.filter_edit.textChanged.connect(lambda _text: self._apply_filter())
        filter_row.addWidget(self.filter_edit, 1)
        find_btn = QPushButton("查找")
        find_btn.clicked.connect(self._apply_filter)
        filter_row.addWidget(find_btn)
        clear_btn = QPushButton("清除")
        clear_btn.clicked.connect(self._clear_filter)
        filter_row.addWidget(clear_btn)
        layout.addLayout(filter_row)

        self.hint_label = QLabel("")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(1)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setAnimated(False)
        self.tree.currentItemChanged.connect(self._on_current_changed)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.tree, 1)

        footer = QHBoxLayout()
        self.status = QLabel("")
        footer.addWidget(self.status, 1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        self.ok_btn = QPushButton("确定")
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self.accept)
        footer.addWidget(self.ok_btn)
        layout.addLayout(footer)

        self.setLayout(layout)

    # ------------------------------------------------------------ 数据

    def _load_categories(self, *, select_table: str = "") -> None:
        """只加载**当前库**里的字典表；没有就引导用户在建表 / 导表。"""
        self._entries = []
        can_manage = bool(self._local_db_path)
        self.add_btn.setEnabled(can_manage)
        self.import_btn.setEnabled(can_manage)
        if self.filter_edit.text():
            self.filter_edit.blockSignals(True)
            self.filter_edit.clear()
            self.filter_edit.blockSignals(False)
        if can_manage:
            self._entries.extend(self._load_local_entries(self._local_db_path))

        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        for entry in self._entries:
            self.category_combo.addItem(entry.label, entry)
        index = self._initial_index()
        if select_table:
            for position, entry in enumerate(self._entries):
                if entry.table == select_table:
                    index = position
                    break
        self.category_combo.setCurrentIndex(index)
        self.category_combo.blockSignals(False)

        if not can_manage:
            self.tree.clear()
            self.ok_btn.setEnabled(False)
            self.status.setText(
                "当前目标还不是一个可挂接字典的本地库：独立文件请先创建，"
                "再到「数据管理」里挂接；或改为「往已有库中创建数据」。"
            )
            return
        if not self._entries:
            self.tree.clear()
            self.ok_btn.setEnabled(False)
            self.status.setText(
                "当前库还没有字典表：请点「添加字典表…」（可复制系统字典），"
                "或「导入外部字典…」从 Excel / 其它库导入。"
            )
            return
        self._load_domains()

    def _initial_index(self) -> int:
        if not self._entries:
            return -1
        # 1) 当前字段原来挂接的本地字典表
        if self._initial_local_table:
            for index, entry in enumerate(self._entries):
                if entry.source == "local" and entry.table == self._initial_local_table:
                    return index
        # 2) 同名分类，当前库优先
        if self._initial_category:
            for index, entry in enumerate(self._entries):
                if entry.category == self._initial_category:
                    return index
        # 3) 默认第一项（本地在前）
        return 0

    @staticmethod
    def _load_local_entries(path: str) -> list[_Entry]:
        """当前本地库登记为 dict 的表 → 类别 / 域名清单。"""
        if not path or not os.path.isfile(path):
            return []
        try:
            db = local_db_util(path)
        except Exception:  # noqa: BLE001
            return []
        entries: list[_Entry] = []
        try:
            rows = db.cursor.execute(
                "SELECT table_name, attr FROM user_relation WHERE type = 'dict'"
            ).fetchall()
            for table, attr in rows:
                table = str(table)
                try:
                    data = json.loads(attr) if attr else {}
                except (TypeError, ValueError):
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                # 本地字典**只有一个名字：库内表名**。老数据里的
                # attr.dict_table 是复制来源的类别名，仅供追溯，不参与展示
                # 和挂接（否则重命名后字段上还是旧名）。
                category = table
                domains: list[tuple[str, int]] = []
                quoted = '"' + table.replace('"', '""') + '"'
                try:
                    columns = {
                        str(row[1])
                        for row in db.cursor.execute(f"PRAGMA table_info({quoted})")
                    }
                    if "domain" in columns:
                        for domain, count in db.cursor.execute(
                            f'SELECT domain, COUNT(*) FROM {quoted} '
                            "GROUP BY domain ORDER BY domain"
                        ):
                            domains.append((str(domain or ""), int(count)))
                    elif "code" in columns:
                        count = db.cursor.execute(
                            f'SELECT COUNT(*) FROM {quoted}'
                        ).fetchone()[0]
                        known = [str(value) for value in (data.get("domains") or [])]
                        domains = [(name, int(count)) for name in known] or [
                            (table, int(count))
                        ]
                    # attr 里登记过、但还没有任何条目的空字典域也要露出来
                    # （建表时从系统字典复制的是"域清单"，空的域没有行）。
                    seen = {name for name, _count in domains}
                    for name in data.get("domains") or []:
                        name = str(name or "")
                        if name and name not in seen:
                            domains.append((name, 0))
                            seen.add(name)
                    domains.sort(key=lambda item: item[0])
                except Exception:
                    logger.warning("读取本地字典表失败：%s", table, exc_info=True)
                entries.append(
                    _Entry(
                        source="local",
                        category=category,
                        table=table,
                        domains=domains,
                    )
                )
        finally:
            db.conn.close()
        return entries

    # ------------------------------------------------------------ 现场建表 / 导表

    def _normalize_names(self) -> None:
        """把老库里的字典逻辑名统一成库内表名（挂接/导表后调一次）。"""
        if not self._local_db_path:
            return
        from app.core.domain.create_data.dict_tables import normalize_local_dict_names

        try:
            normalize_local_dict_names(self._local_db_path)
        except Exception:  # noqa: BLE001 —— 迁移失败不影响本次选择
            logger.warning("规范化本地字典表名失败", exc_info=True)

    def _suggest_table_name(self, base: str) -> str:
        """给新表起名：库已存在时按 ``名称_1`` 避让；库还没建出来时直接用。"""
        from app.core.domain.import_layer import unique_table_name

        base = str(base or "").strip() or "字典表"
        if not self._local_db_path or not os.path.isfile(self._local_db_path):
            return base
        return unique_table_name(self._local_db_path, base)

    def _add_local_dict(self) -> None:
        """在当前库里新建字典表（空表，或复制「字典管理」里的整张系统字典）。"""
        from app.core.domain.create_data.dict_tables import (
            DictTableError,
            create_local_dict_table,
        )
        from app.ui.dialogs.add_local_dict import AddLocalDictDialog

        if not self._local_db_path:
            return
        try:
            service = self._service
            categories = [item.name for item in service.list_categories()]
        except Exception:  # noqa: BLE001 —— 字典管理读不到时只允许建空表
            service = None
            categories = []
        dialog = AddLocalDictDialog(
            self,
            suggested_name=self._suggest_table_name("字典表"),
            categories=categories,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            dialog.deleteLater()
            return
        name = dialog.table_name()
        source = dialog.source_category()
        dialog.deleteLater()
        try:
            count = create_local_dict_table(
                self._local_db_path, name, source_category=source, dict_service=service
            )
        except DictTableError as exc:
            QMessageBox.warning(self, "添加字典表", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 —— 槽内异常不能冒出去
            logger.exception("添加本地字典表失败")
            QMessageBox.warning(self, "添加字典表", f"创建失败：{exc}")
            return
        self._normalize_names()
        self._load_categories(select_table=name)
        self.status.setText(
            f"已创建字典表「{name}」"
            + (f"，复制 {count} 条字典项" if count else "（空表）")
        )

    def _import_external_dict(self) -> None:
        """从 Excel / 其它本地库导入一张字典表到当前库。"""
        if not self._local_db_path:
            return
        path = open_file_dir(
            self,
            is_dir=False,
            title="导入外部字典到当前库",
            init_path=str(Path.home()),
            filter_=(
                "字典文件 (*.xls *.xlsx *.gpkg *.sqlite *.db *.sqlite3);;"
                "Excel 字典 (*.xls *.xlsx);;"
                "本地库 (*.gpkg *.sqlite *.db *.sqlite3);;"
                "所有文件 (*)"
            ),
        )
        if not path:
            return
        suffix = os.path.splitext(path)[1].lower()
        if suffix in (".xls", ".xlsx"):
            self._import_excel_dict(path)
        else:
            self._import_db_dict(path)

    def _import_excel_dict(self, source: str) -> None:
        """Excel（字典域/代码/名称/上级代码）→ 当前库的新字典表。"""
        from app.core.domain.create_data.dict_tables import (
            DictTableError,
            create_local_dict_table,
        )
        from app.core.domain.local_dict_excel import (
            LocalDictExcelError,
            import_local_dict_xls,
        )

        suggested = self._suggest_table_name(Path(source).stem or "外部字典")
        name, ok = QInputDialog.getText(
            self,
            "导入外部字典",
            "导入到当前库的新字典表，名称：",
            text=suggested,
        )
        if not ok:
            return
        name = str(name or "").strip()
        if not name:
            QMessageBox.warning(self, "导入外部字典", "字典表名称不能为空")
            return
        if name != suggested:
            name = self._suggest_table_name(name)
        try:
            create_local_dict_table(self._local_db_path, name)
            report = import_local_dict_xls(
                source, self._local_db_path, name, replace=True
            )
        except DictTableError as exc:
            QMessageBox.warning(self, "导入外部字典", str(exc))
            return
        except LocalDictExcelError as exc:
            QMessageBox.warning(self, "导入外部字典", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 —— 槽内异常不能冒出去
            logger.exception("从 Excel 导入本地字典失败")
            QMessageBox.warning(self, "导入外部字典", f"导入失败：{exc}")
            return
        self._normalize_names()
        self._load_categories(select_table=name)
        QMessageBox.information(
            self,
            "导入外部字典",
            f"已导入到字典表「{name}」：新增 {report.inserted} 条，"
            f"更新 {report.updated} 条，跳过 {report.skipped} 行",
        )

    def _import_db_dict(self, source: str) -> None:
        """另一个本地库里的字典表 → 复制成当前库的一张字典表。"""
        from app.core.domain.create_data.dict_tables import (
            DictPlan,
            DictSource,
            DictTableError,
            build_dict_table_specs,
            persist_dict_tables,
        )

        if not os.path.isfile(source):
            QMessageBox.warning(self, "导入外部字典", f"文件不存在：{source}")
            return
        if os.path.abspath(source) == os.path.abspath(self._local_db_path):
            QMessageBox.warning(self, "导入外部字典", "源库与当前库是同一个文件")
            return
        try:
            conn = sqlite3.connect(source, timeout=5.0)
            try:
                conn.execute("PRAGMA query_only = ON")
                rows = conn.execute(
                    "SELECT table_name, attr FROM user_relation WHERE type = 'dict'"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            QMessageBox.warning(self, "导入外部字典", f"不是可读的本地库：{exc}")
            return
        if not rows:
            QMessageBox.warning(
                self, "导入外部字典", "源库里没有登记为字典表的表（user_relation.type=dict）"
            )
            return
        labels: list[str] = []
        by_label: dict[str, tuple[str, str]] = {}
        for table, attr in rows:
            table = str(table)
            try:
                data = json.loads(attr) if attr else {}
            except (TypeError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            category = str(data.get("dict_table") or data.get("category") or table)
            label = table if table == category else f"{category}（{table}）"
            labels.append(label)
            by_label[label] = (table, category)
        label, ok = QInputDialog.getItem(
            self, "导入外部字典", "选择源库里的字典表：", labels, 0, False
        )
        if not ok or not label:
            return
        table, category = by_label[label]
        try:
            plan = DictPlan(
                sources=(
                    DictSource(
                        db_path=source, table=table, category=category
                    ),
                )
            )
            specs = build_dict_table_specs(self._local_db_path, plan, None)
            if not specs:
                raise DictTableError("源库字典表里没有可导入的条目")
            db = local_db_util(self._local_db_path)
            try:
                written = persist_dict_tables(db, specs)
            finally:
                db.conn.close()
        except DictTableError as exc:
            QMessageBox.warning(self, "导入外部字典", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 —— 槽内异常不能冒出去
            logger.exception("从本地库导入字典表失败")
            QMessageBox.warning(self, "导入外部字典", f"导入失败：{exc}")
            return
        name = written[0] if written else ""
        self._normalize_names()
        self._load_categories(select_table=name)
        QMessageBox.information(
            self, "导入外部字典", f"已导入字典表：{'、'.join(written)}"
        )

    def _current_entry(self) -> _Entry | None:
        data = self.category_combo.currentData()
        return data if isinstance(data, _Entry) else None

    def _current_category(self) -> str:
        entry = self._current_entry()
        return entry.category if entry is not None else ""

    def _on_category_changed(self, _index: int) -> None:
        if self._switching:  # 搜索结果自动切表：保留搜索结果，不重载普通视图
            return
        if self.filter_edit.text().strip():
            self._apply_filter()
        else:
            self._load_domains()

    def _load_domains(self) -> None:
        entry = self._current_entry()
        self.tree.clear()
        self._domains = []
        if entry is None:
            self.status.setText("请选择字典表")
            self.ok_btn.setEnabled(False)
            return
        if entry.source == "local":
            self._domains = list(entry.domains)
        else:
            try:
                self._domains = [
                    (domain.domain, domain.item_count)
                    for domain in self._service.list_domains(entry.category)
                ]
            except DictError as exc:
                self.status.setText(str(exc))
                self.ok_btn.setEnabled(False)
                return
        self.hint_label.setText(
            f"「{self._field_alias or self._field_name or '该字段'}」字段可挂接如下字典域"
            + ("（当前库字典）" if entry.source == "local" else "")
        )
        none_item = QTreeWidgetItem(["不挂接字典域"])
        none_item.setData(0, _ROLE_KIND, _K_NONE)
        none_item.setForeground(0, QBrush(QColor("#c0392b")))
        self.tree.addTopLevelItem(none_item)

        recommended, others = self._split_by_similarity(self._domains)
        if recommended:
            group = self._group_item("推荐挂接字典域", "#1f5fbf")
            for name, count in recommended:
                group.addChild(self._domain_item(name, count))
            self.tree.addTopLevelItem(group)
            group.setExpanded(True)
        group = self._group_item("其它字典域", "")
        for name, count in others:
            group.addChild(self._domain_item(name, count))
        if others:
            self.tree.addTopLevelItem(group)
            group.setExpanded(True)

        self._select_initial()
        if not self._domains:
            self.status.setText("当前字典表下还没有字典域，请先在字典管理里创建")

    def _group_item(self, title: str, color: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem([title])
        item.setData(0, _ROLE_KIND, _K_GROUP)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        if color:
            item.setForeground(0, QBrush(QColor(color)))
        return item

    @staticmethod
    def _domain_item(domain: str, count: int) -> QTreeWidgetItem:
        item = QTreeWidgetItem([str(domain)])
        item.setData(0, _ROLE_KIND, _K_DOMAIN)
        item.setData(0, _ROLE_DOMAIN, str(domain))
        item.setToolTip(0, f"{domain}\n共 {count} 条")
        return item

    def _select_initial(self) -> None:
        """优先选中字段原字典域；否则选中第一个推荐项。"""
        if self._initial_domain and self._select_domain(self._initial_domain):
            return
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            if (
                group.data(0, _ROLE_KIND) == _K_GROUP
                and group.text(0) == "推荐挂接字典域"
                and group.childCount()
            ):
                self.tree.setCurrentItem(group.child(0))
                return
        self.tree.setCurrentItem(None)
        self.ok_btn.setEnabled(False)

    def _select_domain(self, domain_name: str) -> bool:
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            for child_index in range(group.childCount()):
                child = group.child(child_index)
                if (
                    child.data(0, _ROLE_KIND) == _K_DOMAIN
                    and child.data(0, _ROLE_DOMAIN) == domain_name
                ):
                    self.tree.setCurrentItem(child)
                    return True
        return False

    # ------------------------------------------------------------ 推荐

    def _split_by_similarity(
        self, domains: list[tuple[str, int]]
    ) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
        scored = []
        for name, count in domains:
            score = self._score(name)
            if score >= _REC_THRESHOLD:
                scored.append((score, name, count))
        scored.sort(key=lambda item: (-item[0], item[1]))
        recommended = [(name, count) for _score, name, count in scored[:_REC_LIMIT]]
        recommended_names = {name for name, _count in recommended}
        others = [item for item in domains if item[0] not in recommended_names]
        return recommended, others

    def _score(self, domain: str) -> float:
        label = _domain_label(domain)
        code = _domain_code(domain)
        alias = self._field_alias.casefold()
        name = self._field_name.casefold()
        score = 0.0
        if alias and label:
            folded = label.casefold()
            if alias == folded:
                score = max(score, 1.0)
            elif alias in folded or folded in alias:
                score = max(score, 0.9)
            else:
                score = max(
                    score,
                    difflib.SequenceMatcher(None, alias, folded).ratio() * 0.85,
                )
            if alias in domain.casefold():
                score = max(score, 0.85)
        if name and code:
            folded_code = code.casefold()
            if name == folded_code:
                score = max(score, 0.95)
            elif name in folded_code or folded_code in name:
                score = max(score, 0.8)
            else:
                score = max(
                    score,
                    difflib.SequenceMatcher(None, name, folded_code).ratio() * 0.6,
                )
        return score

    # ------------------------------------------------------------ 筛选 / 取值

    def _apply_filter(self) -> None:
        """搜索字典域：**跨当前库的全部字典表**。

        输入「权属性质」这类域名时，用户往往不记得它在哪张字典表里；这里按
        域名列出所有命中项，选中其它表里的域名会自动把上方下拉切过去。
        清空关键词即恢复当前字典表的「推荐 / 其它」视图。
        """
        keyword = self.filter_edit.text().strip().casefold()
        if not keyword:
            self._load_domains()
            return
        matches: list[tuple[int, _Entry, list[tuple[str, int]]]] = []
        for index, entry in enumerate(self._entries):
            hit = [
                (name, count)
                for name, count in entry.domains
                if keyword in str(name).casefold()
            ]
            if hit:
                matches.append((index, entry, hit))
        self._domains = []
        self.tree.clear()
        none_item = QTreeWidgetItem(["不挂接字典域"])
        none_item.setData(0, _ROLE_KIND, _K_NONE)
        none_item.setForeground(0, QBrush(QColor("#c0392b")))
        self.tree.addTopLevelItem(none_item)

        if not matches:
            empty = QTreeWidgetItem(["没有匹配的字典域"])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.tree.addTopLevelItem(empty)
            self.tree.setCurrentItem(None)
            self.ok_btn.setEnabled(False)
            self.status.setText(
                f"没有找到匹配「{self.filter_edit.text().strip()}」的字典域"
            )
            return

        current = self.category_combo.currentIndex()
        target = None
        total = 0
        for index, entry, hit in matches:
            total += len(hit)
            group = self._group_item(
                f"{entry.label}（{len(hit)} 个字典域）", "#1f5fbf"
            )
            for name, count in hit:
                child = self._domain_item(name, count)
                child.setData(0, _ROLE_ENTRY, index)
                group.addChild(child)
            self.tree.addTopLevelItem(group)
            group.setExpanded(True)
            if index == current and target is None and group.childCount():
                target = group.child(0)
        if target is None:
            first = self.tree.topLevelItem(1)  # 0 = 不挂接
            if first is not None and first.childCount():
                target = first.child(0)
        self.hint_label.setText(
            f"搜索「{self.filter_edit.text().strip()}」：{total} 个字典域"
            "（可跨字典表）"
        )
        if target is not None:
            self.tree.setCurrentItem(target)
        else:
            self.ok_btn.setEnabled(False)

    def _clear_filter(self) -> None:
        if self.filter_edit.text():
            self.filter_edit.blockSignals(True)
            self.filter_edit.clear()
            self.filter_edit.blockSignals(False)
        self._load_domains()

    def _on_current_changed(self, current, _previous) -> None:
        if current is None:
            self.ok_btn.setEnabled(False)
            return
        kind = current.data(0, _ROLE_KIND)
        if kind == _K_NONE:
            self.ok_btn.setEnabled(True)
            self.status.setText("确定后将清空该字段的字典关联")
            return
        if kind == _K_DOMAIN:
            # 搜索结果来自别的字典表：把上方下拉切过去（阻断信号，保留搜索结果）
            entry_index = current.data(0, _ROLE_ENTRY)
            if (
                isinstance(entry_index, int)
                and 0 <= entry_index < len(self._entries)
                and entry_index != self.category_combo.currentIndex()
            ):
                self._switching = True
                try:
                    self.category_combo.blockSignals(True)
                    self.category_combo.setCurrentIndex(entry_index)
                    self.category_combo.blockSignals(False)
                finally:
                    self._switching = False
            self.ok_btn.setEnabled(True)
            self.status.setText(
                f"已选（当前库）：{self._current_category()} / "
                f"{current.data(0, _ROLE_DOMAIN)}"
            )
            return
        self.ok_btn.setEnabled(False)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.data(0, _ROLE_KIND) in (_K_DOMAIN, _K_NONE):
            self.accept()

    def selected(self) -> tuple[str, str] | None:
        """``(字典表, 字典域)``；选「不挂接字典域」返回 ``None``。"""
        item = self.tree.currentItem()
        if item is None:
            return None
        if item.data(0, _ROLE_KIND) == _K_NONE:
            return None
        if item.data(0, _ROLE_KIND) != _K_DOMAIN:
            return None
        return self._current_category(), str(item.data(0, _ROLE_DOMAIN) or "")

    def selected_source(self) -> str:
        entry = self._current_entry()
        return entry.source if entry is not None else ""

    def selected_local_table(self) -> str:
        """选的是当前库字典表时返回库内表名；系统字典返回空串。"""
        entry = self._current_entry()
        if entry is not None and entry.source == "local":
            return entry.table
        return ""


def _domain_label(domain: str) -> str:
    parts = [part.strip() for part in _SPLIT_RE.split(str(domain or "")) if part.strip()]
    return parts[-1] if parts else str(domain or "").strip()


def _domain_code(domain: str) -> str:
    parts = [part.strip() for part in _SPLIT_RE.split(str(domain or "")) if part.strip()]
    return parts[-2] if len(parts) >= 2 else ""
