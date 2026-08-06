"""File Geodatabase → PostGIS 导入。

迁自旧 :mod:`ogr2`（``subprocess.run("ogr2ogr")``）。新代码把
``ogr2ogr`` 作为可执行入口（用 :mod:`shutil.which` 自动发现），同时
也提供 Python API 给业务层调用。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

from app.core.data.pgsql import PgConfig


@dataclass(frozen=True)
class ImportResult:
    """一次导入的结果。"""

    table_name: str
    status: str  # "success" / "failed: xxx"
    rows: int = 0


def _build_ogr2ogr_command(
    gdb_path: str,
    pg_dsn: str,
    options: Optional[List[str]] = None,
) -> List[str]:
    """组装 ogr2ogr 命令行。"""
    cmd = [
        "ogr2ogr",
        "-f", "PostgreSQL",
        f"PG:{pg_dsn}",
        gdb_path,
        "-lco", "LAUNDER=NO",
        "-lco", "OVERWRITE=YES",
        "-lco", "GEOMETRY_NAME=shape",
        "-nlt", "GEOMETRY",
        "-progress",
    ]
    if options:
        cmd.extend(options)
    return cmd


def gdb_to_postgis(
    gdb_path: str,
    pg_dsn: str,
    options: Optional[List[str]] = None,
    timeout: int = 3600,
) -> ImportResult:
    """把 GDB 目录导入 PostgreSQL。

    :param gdb_path: GDB 目录路径
    :param pg_dsn: PostgreSQL 连接字符串，如 ``"host=localhost port=5432 dbname=xxx user=yyy password=zzz"``
    :param options: 额外 ``ogr2ogr`` 选项列表
    :param timeout: 子进程超时（秒）
    """
    if not shutil.which("ogr2ogr"):
        return ImportResult(
            table_name="",
            status="failed: ogr2ogr not in PATH",
        )
    cmd = _build_ogr2ogr_command(gdb_path, pg_dsn, options)
    try:
        result = subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
        return ImportResult(table_name=os.path.basename(gdb_path), status="success")
    except subprocess.CalledProcessError as e:
        return ImportResult(table_name="", status=f"failed: {e.stderr or 'unknown'}")
    except subprocess.TimeoutExpired:
        return ImportResult(table_name="", status="failed: timeout")


__all__ = ["ImportResult", "gdb_to_postgis", "gdb_to_postgis_from_config"]


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="GDB → PostGIS")
    parser.add_argument("--gdb", required=True, help="GDB 目录路径")
    args = parser.parse_args()

    result = gdb_to_postgis_from_config(args.gdb)
    print(f"status: {result.status}")
    sys.exit(0 if result.status == "success" else 1)


def gdb_to_postgis_from_config(
    gdb_path: str,
    cfg: Optional[PgConfig] = None,
) -> ImportResult:
    """便捷封装：从 :class:`PgConfig` 生成 DSN 并调 :func:`gdb_to_postgis`。"""
    config = cfg or PgConfig.from_env()
    dsn = f"host={config.host} port={config.port} dbname={config.dbname} user={config.user} password={config.password}"
    return gdb_to_postgis(gdb_path, dsn)


__all__ = ["ImportResult", "gdb_to_postgis", "gdb_to_postgis_from_config"]
