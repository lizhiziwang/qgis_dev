"""字典管理的 SQLite 存取（Qt-free）。

存储位置是**应用级**共享库（默认 ``<config>/state/dicts.db``，见
:func:`default_db_path`）：字典是业务基础数据，换工程不应该丢；这一点与
:mod:`app.infra.fs.state` 里「数据管理连接」是同一套语义。

结构（v2）：

- ``dict_categories`` —— 类别（字典表），用户可自建；预置政区 / 资源两个；
- ``dicts`` —— 类别下的字典域（Excel 的「字典域」列）；``sort_order`` 支持拖拽排序；
- ``dict_items`` —— 字典项，``parent_code`` 表达层级，``sort_order`` 支持同级重排。

设计要点：

- 每次操作开一个短连接（``sqlite3`` 连接不是线程安全的），
  ``PRAGMA foreign_keys`` / ``busy_timeout`` 每个连接都要重设；
- 字典项层级没有 SQL 外键：改代码时要靠服务层级联更新后代的 ``parent_code``，
  并拦下「挂到自己下级」这类环；
- 大字典（真实模板里「村」有 2 万条）按 ``LIMIT/OFFSET`` 分页取，不在
  服务层一次拉全量。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from app.core.domain.dict_manage.models import (
    DEFAULT_CATEGORIES,
    LEGACY_CATEGORY_NAMES,
    DictCategory,
    DictDomain,
    DictError,
    DictItem,
    DictRow,
    ImportReport,
)
from app.infra.fs.paths import Paths
from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 库结构版本；v1 = 固定两类（zhengqu/ziyuan），v2 = 动态类别 + sort_order
SCHEMA_VERSION = 2

#: 列表分页默认值（2 万条的「村」字典一次性塞进 QTreeWidget 会卡死）
DEFAULT_PAGE_SIZE = 500
#: 搜索结果上限（用户找一条记录，不需要把几万条全列出来）
DEFAULT_SEARCH_LIMIT = 1000

_SCHEMA_V2_SQL = """
CREATE TABLE IF NOT EXISTS dict_categories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dicts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT NOT NULL,
    domain      TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (category, domain)
);

CREATE TABLE IF NOT EXISTS dict_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dict_id     INTEGER NOT NULL REFERENCES dicts(id) ON DELETE CASCADE,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    parent_code TEXT NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (dict_id, code)
);

CREATE INDEX IF NOT EXISTS idx_dict_items_parent
    ON dict_items (dict_id, parent_code, sort_order, id);
"""

#: v1 → v2：补类别表、给字典域加 sort_order、把类别代码换成界面名称。
#: 需要临时关掉外键（重建 ``dicts`` 时 ``dict_items`` 还引用它）。
_MIGRATE_V1_SQL = f"""
BEGIN;

CREATE TABLE IF NOT EXISTS dict_categories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO dict_categories (name, sort_order) VALUES
    ('{DEFAULT_CATEGORIES[0]}', 0),
    ('{DEFAULT_CATEGORIES[1]}', 1);

CREATE TABLE dicts_v2 (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT NOT NULL,
    domain      TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (category, domain)
);

INSERT INTO dicts_v2 (id, category, domain, description, sort_order, created_at)
SELECT id,
       CASE category
           WHEN 'zhengqu' THEN '{LEGACY_CATEGORY_NAMES["zhengqu"]}'
           WHEN 'ziyuan' THEN '{LEGACY_CATEGORY_NAMES["ziyuan"]}'
           ELSE category
       END,
       domain,
       description,
       0,
       created_at
FROM dicts;

INSERT OR IGNORE INTO dict_categories (name, sort_order)
SELECT DISTINCT category, 100 FROM dicts_v2;

DROP TABLE dicts;
ALTER TABLE dicts_v2 RENAME TO dicts;

COMMIT;
"""

_ITEM_COLUMNS = """
    i.id, i.dict_id, i.code, i.name, i.parent_code, i.sort_order,
    EXISTS (
        SELECT 1 FROM dict_items c
        WHERE c.dict_id = i.dict_id AND c.parent_code = i.code
    ) AS has_children
"""


def default_db_path() -> Path:
    """字典库的默认位置：``<config>/state/dicts.db``（应用级，不随工程走）。"""
    return Paths.from_env().config / "state" / "dicts.db"


def _text(value: object) -> str:
    """统一成去首尾空白的字符串（``None`` → 空串）。"""
    return str(value or "").strip()


def _like_escape(value: str) -> str:
    """转义 ``LIKE`` 通配符，避免用户输入的 ``%`` / ``_`` 被当成通配符。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _item_from_row(row: sqlite3.Row) -> DictItem:
    keys = row.keys()
    return DictItem(
        id=int(row["id"]),
        dict_id=int(row["dict_id"]),
        code=row["code"],
        name=row["name"],
        parent_code=row["parent_code"] or "",
        sort_order=int(row["sort_order"]),
        has_children=bool(row["has_children"]) if "has_children" in keys else False,
        domain=row["domain"] if "domain" in keys else "",
        category=row["category"] if "category" in keys else "",
    )


class DictService:
    """字典库的读写入口。所有方法失败都抛 :class:`DictError`（消息可展示）。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else default_db_path()
        self.ensure_schema()

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ------------------------------------------------------------ 连接 / 建表

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """开一个短连接；正常结束提交，异常回滚。"""
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            # 每个连接都必须重设：foreign_keys 不是库级持久设置。
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        """建目录、建表、按版本迁移（幂等，构造时调用）。"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise DictError(
                    f"字典库版本过新（{version} > {SCHEMA_VERSION}），"
                    "请升级本程序后再打开，避免旧版本改坏数据"
                )
            if version == 0:
                conn.executescript(_SCHEMA_V2_SQL)
                self._seed_categories(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 1:
                self._migrate_v1_to_v2(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _seed_categories(conn: sqlite3.Connection) -> None:
        for order, name in enumerate(DEFAULT_CATEGORIES):
            conn.execute(
                "INSERT OR IGNORE INTO dict_categories (name, sort_order) VALUES (?, ?)",
                (name, order),
            )

    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:
        """旧库迁移：类别代码 → 名称、字典域补排序号。"""
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.executescript(_MIGRATE_V1_SQL)
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise DictError("字典库迁移后外键校验失败，请备份后联系维护人员")
        logger.info("字典库已从 v1 迁移到 v2（动态类别 + 排序）")

    # ------------------------------------------------------------ 类别（字典表）

    def list_categories(self) -> list[DictCategory]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT c.id, c.name, c.sort_order,
                       (SELECT COUNT(*) FROM dicts d WHERE d.category = c.name) AS domain_count
                FROM dict_categories c
                ORDER BY c.sort_order, c.id
                """
            ).fetchall()
        return [
            DictCategory(
                id=int(r["id"]),
                name=r["name"],
                sort_order=int(r["sort_order"]),
                domain_count=int(r["domain_count"]),
            )
            for r in rows
        ]

    def create_category(self, name: str) -> DictCategory:
        """新建类别（字典表）；重名直接报错。"""
        text = _text(name)
        if not text:
            raise DictError("字典表名称不能为空")
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM dict_categories"
                ).fetchone()
                cur = conn.execute(
                    "INSERT INTO dict_categories (name, sort_order) VALUES (?, ?)",
                    (text, int(row["next"])),
                )
                category_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise DictError(f"字典表「{text}」已存在") from exc
        logger.info("新建字典表：%s", text)
        return DictCategory(id=category_id, name=text)

    def delete_category(self, name: str) -> int:
        """删除整个字典表（类别）及其全部字典域 / 字典项；返回删除的字典项条数。"""
        text = _text(name)
        self._check_category(text)
        with self._connection() as conn:
            cur = conn.execute(
                "DELETE FROM dict_items WHERE dict_id IN "
                "(SELECT id FROM dicts WHERE category = ?)",
                (text,),
            )
            removed = max(0, int(cur.rowcount or 0))
            conn.execute("DELETE FROM dicts WHERE category = ?", (text,))
            conn.execute("DELETE FROM dict_categories WHERE name = ?", (text,))
        logger.info("删除字典表：%s（%d 条字典项）", text, removed)
        return removed

    def _check_category(self, category: str) -> None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM dict_categories WHERE name = ?", (_text(category),)
            ).fetchone()
        if row is None:
            raise DictError(f"字典表「{category}」不存在，请先创建字典表")

    # ------------------------------------------------------------ 字典域

    def list_domains(self, category: str) -> list[DictDomain]:
        """某类别下的全部字典域（附条目数），按拖拽排序号排列。"""
        self._check_category(category)
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT d.id, d.category, d.domain, d.description, d.sort_order,
                       (SELECT COUNT(*) FROM dict_items i WHERE i.dict_id = d.id) AS item_count
                FROM dicts d
                WHERE d.category = ?
                ORDER BY d.sort_order, d.id
                """,
                (_text(category),),
            ).fetchall()
        return [
            DictDomain(
                id=int(r["id"]),
                category=r["category"],
                domain=r["domain"],
                description=r["description"] or "",
                sort_order=int(r["sort_order"]),
                item_count=int(r["item_count"]),
            )
            for r in rows
        ]

    def domain_by_id(self, dict_id: int) -> DictDomain:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT d.id, d.category, d.domain, d.description, d.sort_order,
                       (SELECT COUNT(*) FROM dict_items i WHERE i.dict_id = d.id) AS item_count
                FROM dicts d WHERE d.id = ?
                """,
                (int(dict_id),),
            ).fetchone()
        if row is None:
            raise DictError("字典域不存在（可能已被删除），请刷新后重试")
        return DictDomain(
            id=int(row["id"]),
            category=row["category"],
            domain=row["domain"],
            description=row["description"] or "",
            sort_order=int(row["sort_order"]),
            item_count=int(row["item_count"]),
        )

    def create_domain(
        self,
        category: str,
        domain: str,
        description: str = "",
        *,
        at_top: bool = False,
    ) -> DictDomain:
        """新建字典域（挂在已有字典表下）；同表下重名直接报错。

        ``at_top=True`` 时排到最前面（手工「新建字典域 / 同级添加字典域」用）；
        默认追加到末尾（Excel 导入用，保持文件顺序）。
        """
        self._check_category(category)
        name = _text(domain)
        if not name:
            raise DictError("字典域名称不能为空")
        try:
            with self._connection() as conn:
                if at_top:
                    row = conn.execute(
                        """
                        SELECT COALESCE(MIN(sort_order), 1) - 1 AS next
                        FROM dicts WHERE category = ?
                        """,
                        (_text(category),),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT COALESCE(MAX(sort_order), -1) + 1 AS next
                        FROM dicts WHERE category = ?
                        """,
                        (_text(category),),
                    ).fetchone()
                cur = conn.execute(
                    """
                    INSERT INTO dicts (category, domain, description, sort_order)
                    VALUES (?, ?, ?, ?)
                    """,
                    (_text(category), name, _text(description), int(row["next"])),
                )
                dict_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise DictError(f"字典表「{category}」下已存在字典域「{name}」") from exc
        logger.info("新建字典域：%s / %s", category, name)
        return self.domain_by_id(dict_id)

    def ensure_domain(
        self, category: str, domain: str, description: str = ""
    ) -> tuple[DictDomain, bool]:
        """取字典域；不存在就新建。返回 ``(域, 是否新建)``。"""
        self._check_category(category)
        name = _text(domain)
        if not name:
            raise DictError("字典域名称不能为空")
        with self._connection() as conn:
            row = conn.execute(
                "SELECT id FROM dicts WHERE category = ? AND domain = ?",
                (_text(category), name),
            ).fetchone()
        if row is not None:
            return self.domain_by_id(int(row["id"])), False
        return self.create_domain(category, name, description), True

    def update_domain(
        self,
        dict_id: int,
        *,
        domain: str | None = None,
        description: str | None = None,
    ) -> DictDomain:
        """改字典域名 / 说明；条目通过 ``dict_id`` 关联，不受改名影响。"""
        current = self.domain_by_id(dict_id)
        name = current.domain if domain is None else _text(domain)
        if not name:
            raise DictError("字典域名称不能为空")
        desc = current.description if description is None else _text(description)
        try:
            with self._connection() as conn:
                conn.execute(
                    "UPDATE dicts SET domain = ?, description = ? WHERE id = ?",
                    (name, desc, current.id),
                )
        except sqlite3.IntegrityError as exc:
            raise DictError(f"字典表「{current.category}」下已存在字典域「{name}」") from exc
        return self.domain_by_id(current.id)

    def delete_domain(self, dict_id: int) -> int:
        """删除字典域及其全部条目；返回被删的条目数。"""
        info = self.domain_by_id(dict_id)
        with self._connection() as conn:
            conn.execute("DELETE FROM dicts WHERE id = ?", (info.id,))
        logger.info("删除字典域：%s（%d 条）", info.domain, info.item_count)
        return info.item_count

    def reorder_domains(self, category: str, orders: Sequence[tuple[int, int]]) -> None:
        """拖拽排序：按 ``(dict_id, sort_order)`` 批量落库。"""
        self._check_category(category)
        with self._connection() as conn:
            for dict_id, order in orders:
                conn.execute(
                    "UPDATE dicts SET sort_order = ? WHERE id = ? AND category = ?",
                    (int(order), int(dict_id), _text(category)),
                )

    # ------------------------------------------------------------ 字典项

    def children(
        self,
        dict_id: int,
        parent_code: str = "",
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[DictItem]:
        """取某父级下的直接子项（分页）；``parent_code=""`` 为域内顶级。"""
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT {_ITEM_COLUMNS}
                FROM dict_items i
                WHERE i.dict_id = ? AND i.parent_code = ?
                ORDER BY i.sort_order, i.id
                LIMIT ? OFFSET ?
                """,
                (int(dict_id), _text(parent_code), max(1, int(limit)), max(0, int(offset))),
            ).fetchall()
        return [_item_from_row(r) for r in rows]

    def count_children(self, dict_id: int, parent_code: str = "") -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM dict_items WHERE dict_id = ? AND parent_code = ?",
                (int(dict_id), _text(parent_code)),
            ).fetchone()
        return int(row["n"])

    def item_by_id(self, item_id: int) -> DictItem:
        with self._connection() as conn:
            row = conn.execute(
                f"""
                SELECT {_ITEM_COLUMNS}, d.domain, d.category
                FROM dict_items i JOIN dicts d ON d.id = i.dict_id
                WHERE i.id = ?
                """,
                (int(item_id),),
            ).fetchone()
        if row is None:
            raise DictError("字典项不存在（可能已被删除），请刷新后重试")
        return _item_from_row(row)

    def item_by_code(self, dict_id: int, code: str) -> DictItem | None:
        with self._connection() as conn:
            row = conn.execute(
                f"""
                SELECT {_ITEM_COLUMNS}
                FROM dict_items i
                WHERE i.dict_id = ? AND i.code = ?
                """,
                (int(dict_id), _text(code)),
            ).fetchone()
        return _item_from_row(row) if row is not None else None

    def add_item(
        self,
        dict_id: int,
        code: str,
        name: str,
        parent_code: str = "",
        sort_order: int | None = None,
    ) -> DictItem:
        """新增字典项；``parent_code`` 非空时父项必须已在同一字典域里。"""
        domain = self.domain_by_id(dict_id)
        code_text, name_text, parent = _text(code), _text(name), _text(parent_code)
        if not code_text:
            raise DictError("代码不能为空")
        if not name_text:
            raise DictError("名称不能为空")
        if parent:
            if parent == code_text:
                raise DictError("上级代码不能是自己")
            if self.item_by_code(domain.id, parent) is None:
                raise DictError(f"上级代码「{parent}」在字典域「{domain.domain}」里不存在")
        try:
            with self._connection() as conn:
                if sort_order is None:
                    row = conn.execute(
                        """
                        SELECT COALESCE(MAX(sort_order), -1) + 1 AS next
                        FROM dict_items WHERE dict_id = ? AND parent_code = ?
                        """,
                        (domain.id, parent),
                    ).fetchone()
                    order = int(row["next"])
                else:
                    order = int(sort_order)
                cur = conn.execute(
                    """
                    INSERT INTO dict_items (dict_id, code, name, parent_code, sort_order)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (domain.id, code_text, name_text, parent, order),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise DictError(
                f"字典域「{domain.domain}」里已存在代码「{code_text}」"
            ) from exc
        return self.item_by_id(item_id)

    def update_item(
        self,
        item_id: int,
        *,
        code: str | None = None,
        name: str | None = None,
        parent_code: str | None = None,
    ) -> DictItem:
        """修改字典项。

        - 改代码：同一事务里把后代 ``parent_code`` 跟着改，避免层级断掉；
        - 改上级：拒绝挂到自己或自己的后代（成环），也拒绝跨字典域；
        - 名称不能改成空。
        """
        current = self.item_by_id(item_id)
        new_code = current.code if code is None else _text(code)
        new_name = current.name if name is None else _text(name)
        new_parent = current.parent_code if parent_code is None else _text(parent_code)
        if not new_code:
            raise DictError("代码不能为空")
        if not new_name:
            raise DictError("名称不能为空")
        if new_parent:
            if new_parent == new_code:
                raise DictError("上级代码不能是自己")
            if self.item_by_code(current.dict_id, new_parent) is None:
                raise DictError(f"上级代码「{new_parent}」在同一个字典域里不存在")
            if new_parent in self._descendant_codes(current.id):
                raise DictError(f"上级代码「{new_parent}」是当前项的下级，会形成环")

        with self._connection() as conn:
            if new_code != current.code:
                exists = conn.execute(
                    """
                    SELECT 1 FROM dict_items
                    WHERE dict_id = ? AND code = ? AND id <> ?
                    """,
                    (current.dict_id, new_code, current.id),
                ).fetchone()
                if exists is not None:
                    raise DictError(f"同一个字典域里已存在代码「{new_code}」")
                # 先改后代，再改自己：顺序反了会在中间态出现悬空的 parent_code。
                conn.execute(
                    "UPDATE dict_items SET parent_code = ? WHERE dict_id = ? AND parent_code = ?",
                    (new_code, current.dict_id, current.code),
                )
            conn.execute(
                "UPDATE dict_items SET code = ?, name = ?, parent_code = ? WHERE id = ?",
                (new_code, new_name, new_parent, current.id),
            )
        return self.item_by_id(current.id)

    def reorder_items(self, orders: Sequence[tuple[int, int]]) -> None:
        """拖拽排序：按 ``(item_id, sort_order)`` 批量落库（调用方保证同一父级）。"""
        with self._connection() as conn:
            for item_id, order in orders:
                conn.execute(
                    "UPDATE dict_items SET sort_order = ? WHERE id = ?",
                    (int(order), int(item_id)),
                )

    def move_item_to_domain(self, item_id: int, target_dict_id: int) -> int:
        """把字典项（连同整棵子树）移动到另一个字典域；返回移动条数。

        移动后该项在目标域里成为顶级（它原来的上级不在新域里），后代之间的
        ``parent_code`` 关系保持不变；目标域已有同代码项时整体拒绝。
        """
        item = self.item_by_id(item_id)
        target = self.domain_by_id(target_dict_id)
        if target.id == item.dict_id:
            return 0
        if target.category != item.category:
            raise DictError("不能把字典项移动到另一个字典表的字典域")
        with self._connection() as conn:
            conn.execute("DROP TABLE IF EXISTS temp._dict_move_sub")
            conn.execute("CREATE TEMP TABLE _dict_move_sub (code TEXT PRIMARY KEY)")
            try:
                conn.execute(
                    """
                    WITH RECURSIVE sub(code) AS (
                        SELECT code FROM dict_items WHERE id = ?
                        UNION
                        SELECT i.code FROM dict_items i
                        JOIN sub s ON i.parent_code = s.code
                        WHERE i.dict_id = ?
                    )
                    INSERT OR IGNORE INTO _dict_move_sub SELECT code FROM sub
                    """,
                    (item.id, item.dict_id),
                )
                conflict = conn.execute(
                    """
                    SELECT i.code FROM dict_items i
                    JOIN _dict_move_sub s ON s.code = i.code
                    WHERE i.dict_id = ?
                    LIMIT 1
                    """,
                    (target.id,),
                ).fetchone()
                if conflict is not None:
                    raise DictError(
                        f"目标字典域「{target.domain}」里已存在代码「{conflict['code']}」，"
                        "不能移动"
                    )
                moved = int(
                    conn.execute("SELECT COUNT(*) AS n FROM _dict_move_sub").fetchone()["n"]
                )
                order_row = conn.execute(
                    """
                    SELECT COALESCE(MAX(sort_order), -1) + 1 AS next
                    FROM dict_items WHERE dict_id = ? AND parent_code = ''
                    """,
                    (target.id,),
                ).fetchone()
                conn.execute(
                    """
                    UPDATE dict_items SET dict_id = ?
                    WHERE dict_id = ? AND code IN (SELECT code FROM _dict_move_sub)
                    """,
                    (target.id, item.dict_id),
                )
                conn.execute(
                    """
                    UPDATE dict_items SET parent_code = '', sort_order = ?
                    WHERE id = ?
                    """,
                    (int(order_row["next"]), item.id),
                )
            finally:
                conn.execute("DROP TABLE IF EXISTS temp._dict_move_sub")
        logger.info(
            "字典项移动：%s/%s → %s/%s（%d 条）",
            item.domain, item.code, target.domain, item.code, moved,
        )
        return moved

    def subtree_count(self, item_id: int) -> int:
        """该项及其全部后代的条数（删除确认文案用）。"""
        current = self.item_by_id(item_id)
        with self._connection() as conn:
            row = conn.execute(
                """
                WITH RECURSIVE sub(code) AS (
                    SELECT code FROM dict_items WHERE id = ?
                    UNION
                    SELECT i.code FROM dict_items i
                    JOIN sub s ON i.parent_code = s.code
                    WHERE i.dict_id = ?
                )
                SELECT COUNT(*) AS n FROM sub
                """,
                (current.id, current.dict_id),
            ).fetchone()
        return int(row["n"])

    def delete_item(self, item_id: int) -> int:
        """删除该项及整棵子树；返回删除条数。"""
        current = self.item_by_id(item_id)
        count = self.subtree_count(current.id)
        with self._connection() as conn:
            conn.execute(
                """
                WITH RECURSIVE sub(code) AS (
                    SELECT code FROM dict_items WHERE id = ?
                    UNION
                    SELECT i.code FROM dict_items i
                    JOIN sub s ON i.parent_code = s.code
                    WHERE i.dict_id = ?
                )
                DELETE FROM dict_items
                WHERE dict_id = ? AND code IN (SELECT code FROM sub)
                """,
                (current.id, current.dict_id, current.dict_id),
            )
        logger.info("删除字典项：%s/%s（含下级共 %d 条）", current.domain, current.code, count)
        return count

    def _descendant_codes(self, item_id: int) -> set[str]:
        """该项的全部后代代码（不含自己），用于成环校验。"""
        current = self.item_by_id(item_id)
        with self._connection() as conn:
            rows = conn.execute(
                """
                WITH RECURSIVE sub(code) AS (
                    SELECT code FROM dict_items
                    WHERE dict_id = ? AND parent_code = ?
                    UNION
                    SELECT i.code FROM dict_items i
                    JOIN sub s ON i.parent_code = s.code
                    WHERE i.dict_id = ?
                )
                SELECT code FROM sub
                """,
                (current.dict_id, current.code, current.dict_id),
            ).fetchall()
        return {r["code"] for r in rows}

    # ------------------------------------------------------------ 搜索 / 导出

    def search(
        self,
        category: str,
        text: str,
        *,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[DictItem]:
        """按代码 / 名称 / **字典域**模糊搜索某类别下的字典项（最多 ``limit`` 条）。

        字典域也参与匹配：用户记得「权属性质」这个域、却记不住代码时，
        输入域名就能把该域下的条目一起带出来。
        """
        self._check_category(category)
        keyword = _text(text)
        if not keyword:
            return []
        pattern = f"%{_like_escape(keyword)}%"
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT i.id, i.dict_id, i.code, i.name, i.parent_code, i.sort_order,
                       EXISTS (
                           SELECT 1 FROM dict_items c
                           WHERE c.dict_id = i.dict_id AND c.parent_code = i.code
                       ) AS has_children,
                       d.domain, d.category
                FROM dict_items i
                JOIN dicts d ON d.id = i.dict_id
                WHERE d.category = ?
                  AND (
                       i.code LIKE ? ESCAPE '\\'
                       OR i.name LIKE ? ESCAPE '\\'
                       OR d.domain LIKE ? ESCAPE '\\'
                  )
                ORDER BY d.sort_order, d.domain, i.sort_order, i.id
                LIMIT ?
                """,
                (
                    _text(category),
                    pattern,
                    pattern,
                    pattern,
                    max(1, int(limit)),
                ),
            ).fetchall()
        return [_item_from_row(r) for r in rows]

    def export_items(self, category: str | None = None) -> list[DictItem]:
        """导出用的全量条目（带 ``domain`` / ``category``）。

        只在「Excel 导出」时调用；按类别、域排序号、排序号排列，层级顺序由
        调用方再组织（父必须先于子写出）。
        """
        if category is not None:
            self._check_category(category)
        sql = """
            SELECT i.id, i.dict_id, i.code, i.name, i.parent_code, i.sort_order,
                   0 AS has_children, d.domain, d.category
            FROM dict_items i JOIN dicts d ON d.id = i.dict_id
        """
        params: tuple = ()
        if category is not None:
            sql += " WHERE d.category = ?"
            params = (_text(category),)
        sql += " ORDER BY d.category, d.sort_order, d.domain, i.sort_order, i.id"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_item_from_row(r) for r in rows]

    def export_items_for_domains(
        self,
        category: str,
        domains: Sequence[str],
    ) -> list[DictItem]:
        """只取某类别下指定字典域的条目（创建数据落库用，避免拉全量大字典）。"""
        self._check_category(category)
        names = [d for d in (_text(name) for name in domains) if d]
        if not names:
            return []
        marks = ", ".join("?" for _ in names)
        sql = f"""
            SELECT i.id, i.dict_id, i.code, i.name, i.parent_code, i.sort_order,
                   0 AS has_children, d.domain, d.category
            FROM dict_items i JOIN dicts d ON d.id = i.dict_id
            WHERE d.category = ? AND d.domain IN ({marks})
            ORDER BY d.sort_order, d.domain, i.sort_order, i.id
        """
        with self._connection() as conn:
            rows = conn.execute(sql, (_text(category), *names)).fetchall()
        return [_item_from_row(r) for r in rows]

    # ------------------------------------------------------------ 批量导入

    def apply_import(
        self,
        rows: Sequence[DictRow],
        *,
        replace_domains: bool = False,
    ) -> ImportReport:
        """把解析好的 Excel 行写进库（合并 / 更新，单事务）。

        - 同域同代码已存在 → 更新名称与上级（计 updated），否则插入；
        - ``replace_domains=True`` 时，先清空**文件中出现过的**域再写入，
          没出现在文件里的域不动；
        - 父代码可以指向库里已有但文件里没有的项（按「已有 + 文件内」校验）；
        - 悬空父代码 / 成环的项会被提升为顶级并记告警，不静默丢数据。
        """
        report = ImportReport()
        if not rows:
            return report
        for category in {row.category for row in rows}:
            self._check_category(category)

        with self._connection() as conn:
            domain_ids: dict[tuple[str, str], int] = {}
            existing_codes: dict[int, set[str]] = {}
            cleared: set[int] = set()
            seen_codes: set[tuple[int, str]] = set()
            next_order: dict[tuple[int, str], int] = {}

            for row in rows:
                report.rows_total += 1
                code = _text(row.code)
                name = _text(row.name)
                domain_name = _text(row.domain)
                if not (code and name and domain_name):
                    report.rows_skipped += 1
                    report.warn(
                        f"第 {row.row_number} 行缺少字典域 / 代码 / 名称，已跳过"
                    )
                    continue
                key = (row.category, domain_name)
                dict_id = domain_ids.get(key)
                if dict_id is None:
                    found = conn.execute(
                        "SELECT id FROM dicts WHERE category = ? AND domain = ?",
                        key,
                    ).fetchone()
                    if found is None:
                        order_row = conn.execute(
                            """
                            SELECT COALESCE(MAX(sort_order), -1) + 1 AS next
                            FROM dicts WHERE category = ?
                            """,
                            (row.category,),
                        ).fetchone()
                        cur = conn.execute(
                            """
                            INSERT INTO dicts (category, domain, sort_order)
                            VALUES (?, ?, ?)
                            """,
                            (row.category, domain_name, int(order_row["next"])),
                        )
                        dict_id = int(cur.lastrowid)
                        report.domains_created += 1
                    else:
                        dict_id = int(found["id"])
                    domain_ids[key] = dict_id
                    existing_codes[dict_id] = {
                        r["code"]
                        for r in conn.execute(
                            "SELECT code FROM dict_items WHERE dict_id = ?", (dict_id,)
                        )
                    }
                if replace_domains and dict_id not in cleared:
                    conn.execute("DELETE FROM dict_items WHERE dict_id = ?", (dict_id,))
                    existing_codes[dict_id].clear()
                    cleared.add(dict_id)

                parent = _text(row.parent_code)
                order_key = (dict_id, parent)
                if order_key not in next_order:
                    row_max = conn.execute(
                        """
                        SELECT COALESCE(MAX(sort_order), -1) AS m
                        FROM dict_items WHERE dict_id = ? AND parent_code = ?
                        """,
                        order_key,
                    ).fetchone()
                    next_order[order_key] = int(row_max["m"]) + 1
                order = next_order[order_key]
                next_order[order_key] = order + 1

                is_new = code not in existing_codes[dict_id]
                if (dict_id, code) in seen_codes:
                    report.warn(
                        f"第 {row.row_number} 行代码「{code}」与前面重复，后行覆盖前行"
                    )
                seen_codes.add((dict_id, code))
                conn.execute(
                    """
                    INSERT INTO dict_items (dict_id, code, name, parent_code, sort_order)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(dict_id, code) DO UPDATE SET
                        name = excluded.name,
                        parent_code = excluded.parent_code,
                        sort_order = excluded.sort_order
                    """,
                    (dict_id, code, name, parent, order),
                )
                if is_new:
                    existing_codes[dict_id].add(code)
                    report.items_inserted += 1
                else:
                    report.items_updated += 1

            for dict_id in domain_ids.values():
                self._repair_hierarchy(conn, dict_id, report)

        logger.info(
            "字典导入完成：%d 行，新建域 %d 个，新增 %d 条，更新 %d 条，跳过 %d 行",
            report.rows_total,
            report.domains_created,
            report.items_inserted,
            report.items_updated,
            report.rows_skipped,
        )
        return report

    def _repair_hierarchy(
        self,
        conn: sqlite3.Connection,
        dict_id: int,
        report: ImportReport,
    ) -> None:
        """导入后修父子关系：悬空父代码提升为顶级，成环的节点断开并告警。"""
        rows = conn.execute(
            "SELECT code, parent_code FROM dict_items WHERE dict_id = ?",
            (dict_id,),
        ).fetchall()
        domain = conn.execute(
            "SELECT domain FROM dicts WHERE id = ?", (dict_id,)
        ).fetchone()
        domain_name = domain["domain"] if domain is not None else str(dict_id)
        parent_of: dict[str, str] = {r["code"]: (r["parent_code"] or "") for r in rows}
        codes = set(parent_of)

        # 1) 悬空父代码：库里没有这个父项 → 当顶级，别让整支在树里消失。
        for code, parent in list(parent_of.items()):
            if parent and parent not in codes:
                parent_of[code] = ""
                conn.execute(
                    "UPDATE dict_items SET parent_code = '' WHERE dict_id = ? AND code = ?",
                    (dict_id, code),
                )
                report.warn(
                    f"字典域「{domain_name}」的「{code}」上级代码「{parent}」不存在，已按顶级处理"
                )

        # 2) 成环：沿 parent 链往上走，回到本次路径里的节点就断开这一个节点。
        safe: set[str] = set()
        for code in list(parent_of):
            if code in safe:
                continue
            path: list[str] = []
            current = code
            while current and current not in safe:
                if current in path:
                    parent_of[current] = ""
                    conn.execute(
                        "UPDATE dict_items SET parent_code = '' WHERE dict_id = ? AND code = ?",
                        (dict_id, current),
                    )
                    report.warn(
                        f"字典域「{domain_name}」的「{current}」上级关系成环，已按顶级处理"
                    )
                    break
                path.append(current)
                current = parent_of.get(current, "")
            safe.update(path)
