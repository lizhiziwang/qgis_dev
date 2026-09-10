"""maxent 目录 → PostGIS 导入。

迁自旧 :mod:`import_maxent_to_pg`：

- 路径 / DB 配置改为 :class:`PgConfig` dataclass，**不再硬编码**密码
- 业务用 :class:`MaxentImporter.run()` 一次走完
- CLI 通过 :func:`cli_main` 调用
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from typing import List, Tuple

from app.core.data.pgsql import PgConfig, connect


def _sanitize(name: str) -> str:
    """表名清洗：转小写、替换特殊字符。"""
    n = name.lower().replace(" ", "_").replace("-", "_")
    return "".join(c for c in n if c.isalnum() or c == "_")


@dataclass(frozen=True)
class MaxentImportConfig:
    """maxent 导入配置。"""

    pg: PgConfig
    maxent_root: str
    target_schema: str = "maxent"
    clear_schema: bool = True


@dataclass
class ImportSummary:
    """导入汇总。"""

    vector: List[Tuple[str, str]] = field(default_factory=list)
    raster: List[Tuple[str, str]] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 单条 / 批量
# ---------------------------------------------------------------------------

def _import_shapefile(pg_cfg: PgConfig, schema: str, shp_path: str, table_suffix: str) -> Tuple[str, str]:
    """把 shapefile 导入 PostGIS（用 GDAL/OGR Python API）。"""
    from osgeo import ogr

    table_name = _sanitize(f"{table_suffix}_{os.path.splitext(os.path.basename(shp_path))[0]}")
    conn = connect(pg_cfg)
    try:
        cur = conn.cursor()
        try:
            src_ds = ogr.Open(shp_path)
            if src_ds is None:
                return table_name, "failed: cannot open shapefile"
            src_layer = src_ds.GetLayer(0)
            src_srs = src_layer.GetSpatialRef()
            srid = 0
            if src_srs:
                auth = src_srs.GetAttrValue("AUTHORITY", 1)
                srid = int(auth) if auth else 0
            layer_defn = src_layer.GetLayerDefn()
            field_defs = []
            for i in range(layer_defn.GetFieldCount()):
                fd = layer_defn.GetFieldDefn(i)
                field_defs.append((fd.GetName(), fd.GetType()))
            pg_geom_type = "GEOMETRY"
            cur.execute(f'DROP TABLE IF EXISTS {schema}."{table_name}" CASCADE')
            col_defs = ['"fid" SERIAL PRIMARY KEY']
            type_map = {
                ogr.OFTInteger: "INTEGER",
                ogr.OFTInteger64: "BIGINT",
                ogr.OFTReal: "DOUBLE PRECISION",
                ogr.OFTString: "TEXT",
                ogr.OFTDate: "DATE",
                ogr.OFTTime: "TIME",
                ogr.OFTDateTime: "TIMESTAMP",
            }
            for name, otype in field_defs:
                col_defs.append(f'"{name}" {type_map.get(otype, "TEXT")}')
            col_defs.append(f'"geom" geometry({pg_geom_type}, {srid})')
            cur.execute(
                f'CREATE TABLE {schema}."{table_name}" ({", ".join(col_defs)})'
            )
            col_names = ['"fid"'] + [f'"{fd[0]}"' for fd in field_defs] + ['"geom"']
            placeholders = ["%s"] * len(col_names)
            insert_sql = (
                f'INSERT INTO {schema}."{table_name}" '
                f'({", ".join(col_names)}) VALUES ({", ".join(placeholders)})'
            )
            src_layer.ResetReading()
            fid = 0
            for feat in src_layer:
                fid += 1
                row = [fid]
                for name, _ in field_defs:
                    row.append(feat.GetField(name))
                geom = feat.GetGeometryRef()
                row.append(geom.ExportToWkt() if geom else None)
                cur.execute(insert_sql, row)
            conn.commit()
            cur.execute(
                f'CREATE INDEX IF NOT EXISTS idx_{table_name}_geom '
                f'ON {schema}."{table_name}" USING GIST ("geom")'
            )
            conn.commit()
            return table_name, "success"
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        return table_name, f"failed: {e}"


# ---------------------------------------------------------------------------
# 业务类
# ---------------------------------------------------------------------------

class MaxentImporter:
    """maxent 目录导入器。"""

    def __init__(self, cfg: MaxentImportConfig) -> None:
        self.cfg = cfg

    def _ensure_schema(self) -> None:
        """确保目标 schema 存在；按需清空。"""
        conn = connect(self.cfg.pg)
        try:
            cur = conn.cursor()
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {self.cfg.target_schema}")
            if self.cfg.clear_schema:
                cur.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = %s",
                    (self.cfg.target_schema,),
                )
                tables = [row[0] for row in cur.fetchall()]
                for t in tables:
                    cur.execute(
                        f'DROP TABLE IF EXISTS {self.cfg.target_schema}."{t}" CASCADE'
                    )
                print(f"[Schema] cleared {len(tables)} tables in {self.cfg.target_schema}")
            conn.commit()
        finally:
            conn.close()

    def run(self) -> ImportSummary:
        """执行一次完整导入。"""
        summary = ImportSummary()
        if not os.path.isdir(self.cfg.maxent_root):
            print(f"[ERROR] maxent root not found: {self.cfg.maxent_root}")
            return summary

        self._ensure_schema()

        # 1. template
        template_dir = os.path.join(self.cfg.maxent_root, "template", "functionGrap")
        if os.path.isdir(template_dir):
            for f in os.listdir(template_dir):
                if f.endswith(".shp"):
                    shp = os.path.join(template_dir, f)
                    layer = os.path.splitext(f)[0]
                    t, s = _import_shapefile(self.cfg.pg, self.cfg.target_schema, shp, "template")
                    summary.vector.append((t, s))

        # 2. NRxxxx 目录
        for d in sorted(os.listdir(self.cfg.maxent_root)):
            if not d.startswith("NR"):
                continue
            nr_path = os.path.join(self.cfg.maxent_root, d)
            if not os.path.isdir(nr_path):
                continue
            nr = d.lower()
            # region.shp
            region = os.path.join(nr_path, "region", "region.shp")
            if os.path.isfile(region):
                t, s = _import_shapefile(self.cfg.pg, self.cfg.target_schema, region, nr)
                summary.vector.append((t, s))
            # tif
            for sub in ("tif", "thematic", "asc", "other"):
                sub_dir = os.path.join(nr_path, sub)
                if not os.path.isdir(sub_dir):
                    continue
                for f in os.listdir(sub_dir):
                    if f.endswith((".shp", ".tif", ".asc")):
                        # 简化：只把 shp 走 _import_shapefile
                        if f.endswith(".shp"):
                            shp = os.path.join(sub_dir, f)
                            t, s = _import_shapefile(self.cfg.pg, self.cfg.target_schema, shp, nr)
                            summary.vector.append((t, s))
                        else:
                            # 栅格走 ST_FromGDALRaster（需要 raster2pgsql-like 流程）
                            summary.skipped.append(os.path.join(sub_dir, f))

        return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cli_main() -> int:

    parser = argparse.ArgumentParser(description="maxent 目录 → PostGIS")
    parser.add_argument("--maxent-root", required=True, help="maxent 数据根目录")
    parser.add_argument("--schema", default="maxent", help="目标 schema")
    parser.add_argument("--no-clear", action="store_true", help="不清空 schema")
    args = parser.parse_args()

    pg = PgConfig.from_env()
    cfg = MaxentImportConfig(
        pg=pg,
        maxent_root=args.maxent_root,
        target_schema=args.schema,
        clear_schema=not args.no_clear,
    )
    summary = MaxentImporter(cfg).run()
    print(f"vector: {len(summary.vector)} raster: {len(summary.raster)} skipped: {len(summary.skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
