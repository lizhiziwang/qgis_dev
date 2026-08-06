"""GDB → PostGIS CLI 入口。"""

from __future__ import annotations

import argparse

import _bootstrap_path  # noqa: F401

from app.core.data.gdb_to_postgis import gdb_to_postgis_from_config


def main() -> int:
    parser = argparse.ArgumentParser(description="GDB → PostGIS")
    parser.add_argument("--gdb", required=True, help="GDB 目录路径")
    args = parser.parse_args()

    result = gdb_to_postgis_from_config(args.gdb)
    print(f"status: {result.status}")
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
