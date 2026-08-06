"""PostgreSQL 连接管理。

迁自旧 :mod:`util.Com` 的连接池（但避免硬编码密码）。所有连接信息
从配置或环境变量读。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional


@dataclass(frozen=True)
class PgConfig:
    """PostgreSQL 连接配置。"""

    host: str = "localhost"
    port: int = 5432
    dbname: str = "postgis_30_sample"
    user: str = "postgres"
    password: str = ""
    schema: str = "public"

    @classmethod
    def from_env(cls) -> "PgConfig":
        return cls(
            host=os.environ.get("PGHOST", "localhost"),
            port=int(os.environ.get("PGPORT", "5432")),
            dbname=os.environ.get("PGDATABASE", "postgis_30_sample"),
            user=os.environ.get("PGUSER", "postgres"),
            password=os.environ.get("PGPASSWORD", ""),
            schema=os.environ.get("PGSCHEMA", "public"),
        )

    def dsn(self) -> str:
        """返回 ``psycopg2`` 可用的 DSN dict。"""
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "password": self.password,
        }


@contextmanager
def connect(cfg: Optional[PgConfig] = None) -> Iterator:
    """获取 PostgreSQL 连接（用 :class:`PgConfig.from_env` 默认值）。"""
    import psycopg2

    config = cfg or PgConfig.from_env()
    conn = psycopg2.connect(**config.dsn())
    try:
        yield conn
    finally:
        conn.close()


__all__ = ["PgConfig", "connect"]
