"""应用配置加载。"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

# 环境变量引用：${VAR} 或 ${VAR:-默认值}（空变量也走默认值）
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?}")


@dataclass(frozen=True)
class AppConfig:
    """``config/app.yaml`` 反序列化结果。"""

    env: str  # "dev" | "prod"
    app_name: str
    language: str
    crs: str
    map: "MapConfig"
    qgis: "QgisConfig"
    paths: "PathsConfig"
    logging: "LoggingConfig"
    server: "ServerConfig"
    auth: "AuthConfig"

    @classmethod
    def from_file(cls, path: Path) -> "AppConfig":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        return cls(
            env=data.get("env", "dev"),
            app_name=data.get("app", {}).get("name", "qgis_dev"),
            language=data.get("app", {}).get("language", "zh-Hans"),
            crs=data.get("app", {}).get("crs", "EPSG:4490"),
            map=MapConfig.from_dict(data.get("map", {})),
            qgis=QgisConfig.from_dict(data.get("qgis", {})),
            paths=PathsConfig.from_dict(data.get("paths", {})),
            logging=LoggingConfig.from_dict(data.get("logging", {})),
            server=ServerConfig.from_dict(data.get("server", {})),
            auth=AuthConfig.from_dict(data.get("auth", {})),
        )


@dataclass(frozen=True)
class QgisConfig:
    install_path: Optional[Path]  # 仅 dev 用；prod 走环境变量
    python_path: Optional[Path]
    bin_path: Optional[Path]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QgisConfig":
        return cls(
            install_path=_to_path(data.get("install_path")),
            python_path=_to_path(data.get("python_path")),
            bin_path=_to_path(data.get("bin_path")),
        )


@dataclass(frozen=True)
class PathsConfig:
    resource: Optional[Path]
    log: Optional[Path]
    tmp: Optional[Path]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PathsConfig":
        return cls(
            resource=_to_path(data.get("resource")),
            log=_to_path(data.get("log")),
            tmp=_to_path(data.get("tmp")),
        )


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    file_level: str
    rotation_mb: int
    backup_count: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoggingConfig":
        return cls(
            level=data.get("level", "INFO"),
            file_level=data.get("file_level", "DEBUG"),
            rotation_mb=int(data.get("rotation_mb", 10)),
            backup_count=int(data.get("backup_count", 5)),
        )


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    workers: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServerConfig":
        return cls(
            host=data.get("host", "0.0.0.0"),
            port=int(data.get("port", 8000)),
            workers=int(data.get("workers", 1)),
        )


@dataclass(frozen=True)
class BaseTileConfig:
    """XYZ 瓦片底图配置（如天地图）。"""

    enabled: bool
    name: str
    type: str
    url: str
    referer: str
    zmin: int
    zmax: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BaseTileConfig":
        return cls(
            enabled=bool(data.get("enabled", True)),
            name=data.get("name", "天地图影像"),
            type=data.get("type", "xyz"),
            url=data.get("url", ""),
            referer=data.get("referer", ""),
            zmin=int(data.get("zmin", 0)),
            zmax=int(data.get("zmax", 18)),
        )


@dataclass(frozen=True)
class OnlineBasemapDef(BaseTileConfig):
    """「在线底图」清单条目：比普通底图多一个 ``group``（Dock 归组名）。

    子类化 :class:`BaseTileConfig`，字段（enabled/name/type/url/referer/
    zmin/zmax）与解析逻辑直接复用，``build_online_basemap_layer`` 等按
    ``BaseTileConfig`` 标注的函数可直接收下。

    ``type`` 取值：``xyz``（瓦片模板，默认）/ ``arcgis``（ArcGIS REST
    MapServer/ImageServer，provider ``arcgismapserver``，动态出图）/
    ``arcgisfeature``（ArcGIS REST FeatureServer，provider
    ``arcgisfeatureserver``，**真矢量层**——要素可被「属性」工具识别）/ ``wms``
    （OGC WMS）/ ``wmts``（OGC WMTS）。WMS/WMTS 行用 ``layers/styles/format/
    crs``（+ ``tile_matrix_set``）指定图层；arcgis 行用 ``layers`` 指定子图层
    id（缺省整图 = 0）、``format`` 指定出图格式（缺省 PNG32）；arcgisfeature
    行用 ``layers`` 指定子图层 id（缺省 0）、``crs`` 指定服务坐标系（可选，
    留空则建图层时自动从服务元数据识别）；zmin/zmax/referer 仅 xyz 有意义，
    非 xyz 行写回时省写。
    """

    group: str = "在线底图"
    #: WMS/WMTS 图层名（可逗号分隔多图层）；arcgis = MapServer 子图层 id；arcgisfeature = FeatureServer 子图层 id（如 "0"，缺省 0）
    layers: str = ""
    #: WMS/WMTS 样式（可选）
    styles: str = ""
    #: WMS/WMTS 输出格式（如 image/png）；arcgis = ArcGIS 出图格式（如 PNG32）
    format: str = ""
    #: WMS/WMTS 出图坐标系（如 EPSG:3857）；arcgisfeature = 服务坐标系（如 EPSG:4490，可选，留空自动从服务识别）
    crs: str = ""
    #: WMTS 瓦片矩阵集标识（如 EPSG:3857，须与服务端一致）
    tile_matrix_set: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OnlineBasemapDef":
        return cls(
            group=data.get("group", "在线底图"),
            enabled=bool(data.get("enabled", True)),
            name=data.get("name", "在线底图"),
            type=data.get("type", "xyz"),
            url=data.get("url", ""),
            referer=data.get("referer", "") or "",
            zmin=int(data.get("zmin", 0)),
            zmax=int(data.get("zmax", 18)),
            # None → ""：手工 yaml 写空值（layers:）时防 None 毒化相等性
            layers=data.get("layers") or "",
            styles=data.get("styles") or "",
            format=data.get("format") or "",
            crs=data.get("crs") or "",
            tile_matrix_set=data.get("tile_matrix_set") or "",
        )

    def to_dict(self) -> dict[str, Any]:
        """反序列化成 yaml 条目字典（键序与 app.yaml 书写一致）。

        ``type`` 感知省写：arcgis 只写 url + 非空的 layers（子图层 id）/
        format（出图格式）；arcgisfeature 写 url + 非空的 layers（子图层 id）/
        crs（服务坐标系）；wms/wmts 写 url + 非空的 layers/styles/format/crs
        （wmts 加 tile_matrix_set），**不写** zmin/zmax/referer；其余
        （xyz/缺省/未知类型）维持 xyz 老形状。``enabled`` 为默认 True 时不写
        （与现有条目的省写风格一致）。
        """
        out: dict[str, Any] = {
            "group": self.group,
            "name": self.name,
            "type": self.type,
            "url": self.url,
        }
        if self.type == "arcgis":
            for k in ("layers", "format"):
                v = getattr(self, k)
                if v:
                    out[k] = v
        elif self.type == "arcgisfeature":
            for k in ("layers", "crs"):
                v = getattr(self, k)
                if v:
                    out[k] = v
        elif self.type in ("wms", "wmts"):
            for k in ("layers", "styles", "format", "crs"):
                v = getattr(self, k)
                if v:
                    out[k] = v
            if self.type == "wmts" and self.tile_matrix_set:
                out["tile_matrix_set"] = self.tile_matrix_set
        else:
            # xyz / 缺省 / 未知类型 → 维持现有 xyz 形状（向后兼容护栏）
            if self.referer:
                out["referer"] = self.referer
            out["zmin"] = self.zmin
            out["zmax"] = self.zmax
        if not self.enabled:
            out["enabled"] = False
        return out


@dataclass(frozen=True)
class MapKeysConfig:
    """按 provider 集中管理的底图服务 key。

    底图 URL 不再内嵌真实 key，改用占位符（天地图 ``{tk}`` / 高德
    ``{gaode_key}``）引用本段；构建 QGIS 图层时才替换（见 basemap 模块
    :func:`apply_key_placeholders`）。换 key 只改这里一处。
    """

    tianditu_tk: str = ""
    gaode_key: str = ""

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "MapKeysConfig":
        data = data or {}
        return cls(
            tianditu_tk=data.get("tianditu_tk") or "",
            gaode_key=data.get("gaode_key") or "",
        )


@dataclass(frozen=True)
class MapConfig:
    """地图相关配置（``map:`` 段：base_tile + keys）。

    **不含 online_basemaps**：在线底图清单已独立到 app.yaml 同目录的
    ``online_basemap_config.yaml``（顶层 YAML 列表），读写见
    :func:`load_online_basemaps` / :func:`append_online_basemap_to_yaml`；
    app.yaml 里残留的内联 ``map.online_basemaps`` 条目不再解析（单一数据源）。
    """

    base_tile: Optional[BaseTileConfig]
    keys: MapKeysConfig = field(default_factory=MapKeysConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MapConfig":
        base = data.get("base_tile")
        return cls(
            base_tile=BaseTileConfig.from_dict(base) if base else None,
            keys=MapKeysConfig.from_dict(data.get("keys")),
        )


@dataclass(frozen=True)
class AuthConfig:
    """【已废弃】旧版启动登录的本地校验账号。

    登录已改为提交到授权服务器（:mod:`app.core.auth.config` / :class:`app.core.auth.client.AuthClient`），
    本配置不再参与校验；仅为 ``AppConfig`` 兼容与旧配置文件保留（``config/app.yaml`` 的 ``auth`` 段）。
    """

    username: str
    password: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuthConfig":
        data = data or {}
        return cls(
            username=data.get("username", "admin"),
            password=data.get("password", "admin"),
        )


def _expand_env(s: str) -> str:
    """展开 ``${VAR}`` / ``${VAR:-默认}`` 环境变量引用。

    用于让同一份 ``config/app.yaml`` 在 Windows 开发机与麒麟真机都能用：
    麒麟上设 ``QGIS_INSTALL_PATH=$CONDA_PREFIX`` 即覆盖默认的 Windows 路径。
    """

    def _repl(m: re.Match) -> str:
        var, default = m.group(1), m.group(2)
        v = os.environ.get(var)
        if v:
            return v
        return default if default is not None else ""

    return _ENV_REF.sub(_repl, s)


def _to_path(v: Any) -> Optional[Path]:
    if v is None:
        return None
    if isinstance(v, str):
        v = _expand_env(v)
    return Path(v)


#: 在线底图清单文件名（与 app.yaml 同目录的独立配置文件）
ONLINE_BASEMAPS_FILENAME = "online_basemap_config.yaml"

#: 独立清单文件的注释头（建新文件 / 首条入库时写；v2 schema 说明）
_ONLINE_BASEMAPS_HEADER = [
    "# 在线底图清单（Dock「在线底图」树的数据源；独立于 app.yaml）",
    "# 顶层结构（v2）：",
    "#   groups:   分组名列表（独立持久化，顺序即 Dock 显示顺序；空分组也可见）",
    "#   basemaps: 底图条目列表（每条通过 group 字段引用上面分组名）",
    "# 旧版（顶层直接是底图 list）由各写回原语检测后**整体升级**为 dict，注释头保留。",
    "# group → Dock 分组名；type 取值 xyz（默认）/ arcgis / arcgisfeature（FeatureServer 矢量要素）/",
    "#   wms / wmts（非 xyz 不写 zmin/zmax/referer，见各条目注释）。双击条目 / 叶子右键「添加到画布」→ 出图。",
    "# Dock 右键「添加在线底图」（内置模板）/「添加自定义底图」（弹窗配置）都**文本追加**",
    "# 写回本文件——条目注释/排版原样保留，只把新条目排在清单末尾。叶子右键「编辑连接」",
    "# （弹窗编辑名称/url/key/认证等）则**原位替换**该条目，其余文本同样保留。",
    "# 分组右键「新建 / 重命名 / 删除分组」**文本维护** groups 段；空分组允许独立存在。",
    "# 天地图 URL 里的 tk 用 {tk} 占位符，真实 key 见 app.yaml 的 map.keys。",
]


def _online_basemap_groups_from_defs(defs: list[OnlineBasemapDef]) -> list[str]:
    """从 defs 推导分组列表（按 group 字段首次出现顺序，dedupe，缺省回落「在线底图」）。"""
    order: list[str] = []
    seen: set[str] = set()
    for d in defs:
        g = d.group or "在线底图"
        if g not in seen:
            seen.add(g)
            order.append(g)
    return order


def _online_basemap_parse(text: str) -> tuple[list, list[str]]:
    """解析 v2 dict / 旧 list / 空 / 注释 → ``(basemaps, groups)``。

    顶层 dict：取 ``basemaps`` + ``groups`` 字段（缺省空 list）。
    顶层 list：取 defs 并按 def.group 推导 groups（与旧版「按条目聚合」行为一致）。
    空 / 纯注释：``([], [])``。
    """
    data = yaml.safe_load(text) if text.strip() else None
    if data is None:
        return [], []
    if isinstance(data, list):
        defs = [OnlineBasemapDef.from_dict(x) for x in data if isinstance(x, dict)]
        return defs, _online_basemap_groups_from_defs(defs)
    if isinstance(data, dict):
        if "basemaps" not in data:
            raise ValueError("v2 dict 缺 basemaps 字段（数据损坏）")
        bm = data["basemaps"]
        if bm is None:
            bm = []  # “basemaps:”（后无值）→ YAML 解析为 None，视为空 list
        if not isinstance(bm, list):
            raise ValueError(
                f"v2 dict 的 basemaps 字段应为列表，实际为 {type(bm).__name__}"
            )
        defs = [OnlineBasemapDef.from_dict(x) for x in bm if isinstance(x, dict)]
        grp = data.get("groups", []) or []
        if grp is None:
            grp = []
        if not isinstance(grp, list):
            raise ValueError(
                f"v2 dict 的 groups 字段应为列表，实际为 {type(grp).__name__}"
            )
        # groups 字段是权威；旧版遗留「按条目聚合」只在 list 顶层时使用
        return defs, [str(g) for g in grp]
    raise ValueError(f"顶层应为在线底图清单（YAML list 或 v2 dict），实际为 {type(data).__name__}")


def _dict_top_block_range(
    lines: list[str], key: str
) -> tuple[Optional[int], int, int]:
    """找 v2 dict 形态下 ``key:`` 段在 ``lines`` 里的范围。

    :returns: ``(key_kw_i, child_indent, end_i)``
        - ``key_kw_i``：``key:`` 行 index（``None`` = 没找到）
        - ``child_indent``：段内子项标准缩进（``key_kw`` 缩进 + 2）
        - ``end_i``：段结尾（第一个缩进 ≤ key_kw 缩进的非空非注释行 / EOF）
    """
    pat = re.compile(rf"^(\s*){re.escape(key)}:\s*(?:#.*)?$")
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if m:
            key_indent = len(m.group(1))
            end = i + 1
            while end < len(lines):
                ln2 = lines[end]
                if not ln2.strip() or ln2.lstrip().startswith("#"):
                    end += 1
                    continue
                if len(ln2) - len(ln2.lstrip()) > key_indent:
                    end += 1
                    continue
                break
            return i, key_indent + 2, end
    return None, 0, 0


def _basemaps_block_range(lines: list[str]) -> tuple[Optional[int], int, int]:
    """``basemaps:`` 段范围（``_dict_top_block_range`` 的语义糖；top-level key 缩进 0）。"""
    return _dict_top_block_range(lines, "basemaps")


def _migrate_legacy_list_to_dict_text(text: str) -> str:
    """把旧 list 形态文本**整体重写**为 v2 dict 形态文本（一次性迁移）。

    旧 list 形态：``# 注释`` + ``- group: ...`` + ``  name: ...``
    新 dict 形态：``# 注释`` + ``groups:`` + ``  - ...`` + ``basemaps:`` + ``  - ...``

    升级会**重缩进**原条目块（每行 +2），原条目**行内**注释随条目块整体平移；行间空行保留。
    groups 段从 defs 推导（按 group 字段首次出现顺序）。无条目（``- `` 行都没有）的旧 list
    不升级（留给首次添加时再升级）。返回新文本（不落盘，调用方负责写）。
    """
    lines = text.splitlines()
    first_dash_i = None
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("- "):
            first_dash_i = i
            break
    if first_dash_i is None:
        return text  # 纯注释头 / 空 list → 不升级
    head_lines = lines[:first_dash_i]
    body_lines = lines[first_dash_i:]
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, list):
        return text
    defs = [OnlineBasemapDef.from_dict(x) for x in parsed if isinstance(x, dict)]
    order = _online_basemap_groups_from_defs(defs)
    new_lines = list(head_lines)
    if new_lines and new_lines[-1].strip():
        new_lines.append("")
    new_lines.append("groups:")
    for g in order:
        new_lines.append(f"  - {g}")
    new_lines.append("")
    new_lines.append("basemaps:")
    for ln in body_lines:
        if not ln.strip():
            new_lines.append("")
        else:
            new_lines.append("  " + ln)
    return "\n".join(new_lines) + "\n"


def load_online_basemaps(path: Path) -> list[OnlineBasemapDef]:
    """读取独立在线底图清单文件，返回 :class:`OnlineBasemapDef` 列表。

    支持两种顶层 schema（v2 dict / 旧 list）：

    - **v2 dict** ``{groups: [...], basemaps: [...]}``：直接读 ``basemaps`` 字段；
    - **旧 list** ``[def, ...]``：直接读 defs（各写回原语会在首次落盘时整体升级为 v2 dict）。

    文件缺失 / 空 / 纯注释 → ``[]``；顶层既非 list 也非 dict / dict 缺 ``basemaps`` 字段 →
    抛 :class:`ValueError`（调用方记日志转 critical，Dock 显示「未配置」）。
    """
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    defs, _ = _online_basemap_parse(text)
    return defs


def load_online_basemap_groups(path: Path) -> list[str]:
    """读独立在线底图清单文件的 groups 列表（首次出现顺序；空分组也可见）。

    - **v2 dict** ``{groups: [...], basemaps: [...]}``：直接读 ``groups`` 字段（权威）；
    - **旧 list** ``[def, ...]``：按 ``def.group`` 首次出现顺序推导（与旧版「按条目聚合」行为一致）；
    - 文件缺失 / 空 / 纯注释 → ``[]``。
    """
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    _, groups = _online_basemap_parse(text)
    return groups


def _write_online_basemap_file(
    path: Path, defs: list[OnlineBasemapDef], groups: Optional[list[str]] = None
) -> None:
    """整体写独立清单文件（v2 dict schema）：注释头 + groups 段 + basemaps 段。

    ``groups`` 缺省时按 ``defs`` 推导（按 group 字段首次出现顺序，dedupe）。
    ``defs`` 为空时 ``basemaps:`` 段写 ``[]`` 占位（避免解析成 ``None``）。"""
    if groups is None:
        groups = _online_basemap_groups_from_defs(defs)
    lines = list(_ONLINE_BASEMAPS_HEADER)
    lines.append("")
    lines.append("groups:")
    for g in groups:
        lines.append(f"  - {g}")
    lines.append("")
    if not defs:
        lines.append("basemaps: []")
    else:
        lines.append("basemaps:")
        for i, d in enumerate(defs):
            if i:
                lines.append("")
            lines.extend(_item_block(d, 2))  # dash_col=2：basemaps: 段内项缩进
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_basemap_to_v2_text(text: str, d: OnlineBasemapDef) -> str:
    """v2 dict 形态下追加一条 basemap（自动维护 groups 段）。返回新文本（不落盘）。

    - 在 ``basemaps:`` 段末尾追加条目（与旧 list 形态同样的"文本追加保留注释"语义）；
    - 若 ``d.group`` 不在 ``groups:`` 段则一并追加 group 名（避免「加条目忘了建分组」的不一致）。
    """
    lines = text.splitlines()
    bm_i, _bm_indent, _bm_end = _basemaps_block_range(lines)
    if bm_i is None:
        raise ValueError("v2 dict 缺 basemaps: 段（数据损坏）")

    # 找 basemaps 段内最后一条 `- ` 项（缩进 == bm_indent）
    last_dash_i: Optional[int] = None
    for i in range(bm_i + 1, _bm_end):
        if lines[i].startswith(" " * 2 + "- "):
            last_dash_i = i
    if last_dash_i is None:
        # 空 basemaps 段（极少见）→ 在 basemaps: 行后插入第一项
        insert = _item_block(d, 2)
        lines[bm_i + 1 : bm_i + 1] = ["", *insert]
    else:
        # 块终点 = 遇列 2 的下一条 `- ` 或列 0 的非空行 / EOF
        end = last_dash_i + 1
        while end < len(lines):
            ln = lines[end]
            if not ln.strip() or ln.startswith("  ") or ln[:1].isspace():
                end += 1
                continue
            break
        insert = ["", *_item_block(d, 2)]
        if end < len(lines):
            insert.append("")  # 新条目与文件尾注释之间留空行
        lines[end:end] = insert

    # 自动维护 groups：若 d.group 不在 groups 段则追加
    g = d.group or "在线底图"
    grp_i, grp_item_indent, grp_end = _dict_top_block_range(lines, "groups")
    if grp_i is not None:
        existing: set[str] = set()
        for i in range(grp_i + 1, grp_end):
            s = lines[i].strip()
            if s.startswith("- "):
                existing.add(s[2:].strip())
        if g not in existing:
            insert_grp = [f"{' ' * grp_item_indent}- {g}"]
            if grp_end < len(lines):
                insert_grp.append("")
            lines[grp_end:grp_end] = insert_grp
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def append_online_basemap_to_yaml(path: Path, d: OnlineBasemapDef) -> list[OnlineBasemapDef]:
    """把一条在线底图**文本追加**到独立清单文件 ``path``。

    支持 v2 dict / 旧 list 两种 schema：

    - **v2 dict**：在 ``basemaps:`` 段末尾追加条目；若 ``d.group`` 不在 ``groups:`` 段
      则一并追加 group 名（自动维护 groups，避免「加条目忘了建分组」的不一致）。
    - **旧 list**：首次调用时把整文件**整体升级**为 v2 dict 形式（一次性迁移，注释头保留），
      再按 v2 逻辑追加。

    与 :func:`load_online_basemaps` 同样按行处理（PyYAML 整文件 round-trip 会丢注释）：
    注释 / 排版 / 已有条目文本原样保留。返回追加后的完整清单（调用方用它刷新 Dock 树）。
    """
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        _write_online_basemap_file(path, [d], [d.group or "在线底图"])
        return load_online_basemaps(path)

    text = path.read_text(encoding="utf-8")
    # 先校验当前文件可解析：坏文件 / 非 list 非 dict 顶层 → 直接抛（不落盘）
    load_online_basemaps(path)
    parsed = yaml.safe_load(text)
    if isinstance(parsed, list):
        # 旧 list 结构 → 一次性整体升级为 v2 dict，再按 v2 逻辑追加
        migrated = _migrate_legacy_list_to_dict_text(text)
        new_text = _append_basemap_to_v2_text(migrated, d)
    else:
        new_text = _append_basemap_to_v2_text(text, d)
    path.write_text(new_text, encoding="utf-8")
    return load_online_basemaps(path)


def _item_block_lines(lines: list[str], dash_i: int) -> int:
    """顶层列表项文本块的范围终点：``dash_i`` 之后遇到 ``<= dash_indent`` 的非空行
    （下一条目 / 顶层键 / 文件尾）或 EOF 为止。块内空行 / 缩进 ``> dash_indent``
    的子字段行（含行内注释）都属本条。

    同一规则也适用于 v2 dict 段内（``basemaps:`` 段内 ``  - `` 起、缩进 2 的项）：
    段内下一条同缩进的 ``  - `` 或列 0 的下一顶层键为止。"""
    dash_line = lines[dash_i]
    dash_indent = len(dash_line) - len(dash_line.lstrip())
    end = dash_i + 1
    while end < len(lines):
        ln = lines[end]
        if not ln.strip():
            end += 1
            continue
        ln_indent = len(ln) - len(ln.lstrip())
        if ln_indent > dash_indent:
            end += 1
            continue
        break
    return end


def _locate_online_basemap_block(
    lines: list[str], target: OnlineBasemapDef, indent: int
) -> tuple[Optional[int], int]:
    """找缩进 ``indent`` 的、与 ``target`` 字段级相等的 ``- ...`` 块。

    :returns: ``(dash_i, end)`` 或 ``(None, None)``。``end`` 为 :func:`_item_block_lines`
        算出的块终点行 index（块后下一行）。

    旧 list 形态 ``indent=0``、v2 dict 形态 ``indent=2``（``basemaps:`` 段内项）都走同一个规则。
    """
    prefix = " " * indent + "- "
    for i, ln in enumerate(lines):
        if not ln.startswith(prefix):
            continue
        block_end = _item_block_lines(lines, i)
        block = "\n".join(lines[i:block_end])
        try:
            parsed = yaml.safe_load(block)
        except yaml.YAMLError:
            continue  # load_online_basemaps 已整体校验过，这里防御性跳过
        if (
            isinstance(parsed, list)
            and parsed
            and isinstance(parsed[0], dict)
            and OnlineBasemapDef.from_dict(parsed[0]) == target
        ):
            return i, block_end
    return None, None


def _ensure_group_in_v2_text(text: str, group: str) -> str:
    """v2 dict 形态下确保 ``group`` 在 ``groups:`` 段内；缺则末尾追加。返回新文本（不落盘）。

    调用方应在替换条目（``update``）或追加条目（``append``）之后调用：避免「加条目忘了
    建分组」的不一致。**不**主动从 ``groups:`` 移除（让用户显式调"删除分组"避免误删空分组）。"""
    lines = text.splitlines()
    grp_i, grp_item_indent, grp_end = _dict_top_block_range(lines, "groups")
    if grp_i is None:
        return text
    existing: set[str] = set()
    for i in range(grp_i + 1, grp_end):
        s = lines[i].strip()
        if s.startswith("- "):
            existing.add(s[2:].strip())
    if group in existing:
        return text
    insert_grp = [f"{' ' * grp_item_indent}- {group}"]
    if grp_end < len(lines):
        insert_grp.append("")
    lines[grp_end:grp_end] = insert_grp
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def update_online_basemap_in_yaml(path: Path, old: OnlineBasemapDef, new: OnlineBasemapDef) -> list[OnlineBasemapDef]:
    """把独立清单文件里与 ``old`` 字段相等的条目**原位文本替换**成 ``new``。

    Dock 叶子右键「编辑连接」的写回原语：``old`` 须是 :func:`load_online_basemaps`
    解析出的条目之一（Dock 树刷新时拿到的正是这份 def，字段级相等即可命中）。只重写
    命中条目的文本块（条目内的行内注释随值变化整体重写，避免旧注释误导），文件头 /
    其余条目 / 尾部注释与排版原样保留——与 :func:`append_online_basemap_to_yaml`
    同按行处理（PyYAML 整文件 round-trip 会丢注释）。找不到 ``old``（文件被外部
    改动）或顶层 schema 不支持 → 抛 :class:`ValueError`，不落盘。返回替换后的完整清单
    （调用方用于刷新 Dock 树）。

    支持 v2 dict / 旧 list 两种 schema：

    - **v2 dict**：定位 ``basemaps:`` 段内匹配项，块替换（缩进 2 → 4）；自动维护
      ``groups:`` 段（``new.group`` 不在则追加）。**不动** ``old.group`` 残留（让用户
      显式调"删除分组"避免误删空分组）。
    - **旧 list**：原位替换（缩进 0 → 2），行为与原版一致。
    """
    if not path.exists():
        raise ValueError(f"{path} 不存在，无法编辑在线底图")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"{path} 为空，无法编辑在线底图")
    load_online_basemaps(path)  # 坏文件 / 非 list 非 dict 顶层先抛（不落盘）
    parsed = yaml.safe_load(text)

    if isinstance(parsed, list):
        lines = text.splitlines()
        dash_i, end = _locate_online_basemap_block(lines, old, indent=0)
        if dash_i is None:
            raise ValueError(f"未在 {path} 中找到要编辑的条目（文件可能已被外部修改，请刷新后重试）")
        had_blank = any(not ln.strip() for ln in lines[dash_i:end])
        new_lines = lines[:dash_i] + _item_block(new, 0)
        if had_blank:
            new_lines.append("")  # 原条目与后续文本（下一条目 / 尾注释）之间的空行补回
        new_lines += lines[end:]
        new_text = "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")
    elif isinstance(parsed, dict):
        lines = text.splitlines()
        bm_i, _, _ = _basemaps_block_range(lines)
        if bm_i is None:
            raise ValueError(f"{path} v2 dict 缺 basemaps: 段（数据损坏）")
        dash_i, end = _locate_online_basemap_block(lines, old, indent=2)
        if dash_i is None:
            raise ValueError(f"未在 {path} 中找到要编辑的条目（文件可能已被外部修改，请刷新后重试）")
        had_blank = any(not ln.strip() for ln in lines[dash_i:end])
        new_lines = lines[:dash_i] + _item_block(new, 2)
        if had_blank:
            new_lines.append("")  # 原条目与后续文本之间的空行补回
        new_lines += lines[end:]
        new_text = "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")
        new_text = _ensure_group_in_v2_text(new_text, new.group or "在线底图")
    else:
        raise ValueError(f"{path} 顶层 schema 不支持（既非 list 也非 dict）")

    path.write_text(new_text, encoding="utf-8")
    # 回读清单，供调用方刷新
    return load_online_basemaps(path)


def remove_online_basemap_from_yaml(path: Path, target: OnlineBasemapDef) -> list[OnlineBasemapDef]:
    """把独立清单文件里与 ``target`` 字段相等的条目**文本移除**（其余原样保留）。

    Dock 叶子右键「删除连接」的写回原语：``target`` 须是 :func:`load_online_basemaps`
    解析出的条目之一（Dock 树刷新时拿到的正是这份 def）。只删除命中条目的文本块
    （块内空行归本条，见 :func:`_item_block_lines`），文件头 / 其余条目 / 尾部注释
    与排版原样保留——与 :func:`append_online_basemap_to_yaml` /
    :func:`update_online_basemap_in_yaml` 同按行处理（PyYAML 整文件 round-trip 会丢
    注释）。删光所有条目后文件只剩头部注释 + groups 段（可正常解析为空清单）。

    支持 v2 dict / 旧 list 两种 schema：

    - **v2 dict**：定位 ``basemaps:`` 段内匹配项，块删除；**不动** ``groups:`` 段
      （让用户显式调"删除分组"避免误删空分组，空分组允许独立存在）。
    - **旧 list**：原文本删除，行为与原版一致。

    找不到 ``target`` / 顶层 schema 不支持 → 抛 :class:`ValueError`，不落盘。返回删除后的
    完整清单（可为空列表；调用方用于刷新 Dock 树）。
    """
    if not path.exists():
        raise ValueError(f"{path} 不存在，无法删除在线底图")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"{path} 为空，无法删除在线底图")
    load_online_basemaps(path)  # 坏文件 / 非 list 非 dict 顶层先抛（不落盘）
    parsed = yaml.safe_load(text)

    if isinstance(parsed, list):
        lines = text.splitlines()
        dash_i, end = _locate_online_basemap_block(lines, target, indent=0)
        if dash_i is None:
            raise ValueError(f"未在 {path} 中找到要删除的条目（文件可能已被外部修改，请刷新后重试）")
        new_lines = lines[:dash_i] + lines[end:]
        new_text = "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")
    elif isinstance(parsed, dict):
        lines = text.splitlines()
        dash_i, end = _locate_online_basemap_block(lines, target, indent=2)
        if dash_i is None:
            raise ValueError(f"未在 {path} 中找到要删除的条目（文件可能已被外部修改，请刷新后重试）")
        new_lines = lines[:dash_i] + lines[end:]
        new_text = "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")
    else:
        raise ValueError(f"{path} 顶层 schema 不支持（既非 list 也非 dict）")

    path.write_text(new_text, encoding="utf-8")
    # 回读清单，供调用方刷新（可能为空列表）
    return load_online_basemaps(path)


def _item_block(d: OnlineBasemapDef, dash_col: int) -> list[str]:
    """把一条在线底图序列化成 ``- key: val`` 列表项文本行（缩进 dash_col）。"""
    body = (
        yaml.safe_dump(d.to_dict(), allow_unicode=True, sort_keys=False, default_flow_style=False).strip().splitlines()
    )
    dash = " " * dash_col + "- "
    cont = " " * dash_col + "  "
    return [dash + body[0]] + [cont + ln for ln in body[1:]]


# ==================== 分组管理原语（groups 段）====================


def _validate_group_name(name: str) -> str:
    """分组名校验：返回 ``name`` 原文，失败抛 :class:`ValueError`（中文错误串）。

    规则：

    - ``name`` 须为 :class:`str`（已 ``strip``）；
    - 非空；
    - 长度 ≤ 64；
    - 拒绝 YAML / 行文本特殊字符（避免破文件结构、避免与键值语法冲突）：
      换行 / 回车 / ``:`` / ``#`` / 行首 ``-``。
    """
    if not isinstance(name, str):
        raise ValueError("分组名必须为字符串")
    n = name.strip()
    if not n:
        raise ValueError("分组名不能为空")
    if len(n) > 64:
        raise ValueError("分组名不能超过 64 个字符")
    if any(ch in n for ch in ("\n", "\r")):
        raise ValueError("分组名不能包含换行符")
    if ":" in n or "#" in n:
        raise ValueError("分组名不能包含 ':' 或 '#'（YAML 保留字符）")
    if n.lstrip().startswith("-"):
        raise ValueError("分组名不能以 '-' 开头（与列表项语法冲突）")
    return n


def _locate_group_item(
    lines: list[str], name: str
) -> tuple[Optional[int], int, int]:
    """找 v2 dict 形态下 ``groups:`` 段中匹配 ``name`` 的 ``  - name`` 行。

    :returns: ``(grp_i, item_i, end_i)``
        - ``grp_i``：``groups:`` 行 index（``None`` = 没找到）
        - ``item_i``：命中的 ``  - name`` 行 index（``None`` = 段存在但无此名）
        - ``end_i``：段结尾（见 :func:`_dict_top_block_range`）

    旧 list 形态没有独立的 groups 段 → 调本函数前应先用 :func:`load_online_basemap_groups`
    判断是否需要先升级。"""
    grp_i, _, grp_end = _dict_top_block_range(lines, "groups")
    if grp_i is None:
        return None, None, 0
    for i in range(grp_i + 1, grp_end):
        s = lines[i].strip()
        if not s.startswith("- "):
            continue
        item_name = s[2:].strip()
        if item_name == name:
            return grp_i, i, grp_end
    return grp_i, None, grp_end


def add_online_basemap_group_to_yaml(path: Path, name: str) -> list[str]:
    """在 ``groups:`` 段下**文本追加** ``- name``（独立持久化的分组管理原语）。

    Dock 空白处 / 分组 / 叶子右键「新建分组」的写回原语：

    - 校验 ``name``（非空 / ≤ 64 / 拒绝 ``:`` ``#`` / 不以 ``-`` 开头），失败抛 :class:`ValueError`。
    - 已存在同名分组 → 抛 :class:`ValueError`（不重复建）。
    - 文件缺失 / 空 → 建文件（注释头 + ``groups: [name]`` + 空 ``basemaps:`` 段，注释头保留）。
    - 旧 list 形态 → 整体升级为 v2 dict（一次性迁移，注释头保留），再按 v2 写入。
    - v2 dict 形态 → 在 ``groups:`` 段末尾追加 ``- name``，其余文本原样保留。
    - 不主动在 ``basemaps:`` 段添加任何条目（分组可空，独立存在）。

    返回更新后的完整 ``groups`` 列表（顺序即 Dock 显示顺序）。
    """
    name = _validate_group_name(name)

    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        # 空文件 / 缺失 → 建文件（注释头 + groups 单元素 + 空 basemaps: 段）
        _write_online_basemap_file(path, [], [name])
        return load_online_basemap_groups(path)

    text = path.read_text(encoding="utf-8")
    # 先校验当前文件可解析
    load_online_basemap_groups(path)
    parsed = yaml.safe_load(text)

    if isinstance(parsed, list):
        # 旧 list 形态：迁移为 v2 dict 后追加 group
        defs = [OnlineBasemapDef.from_dict(x) for x in parsed if isinstance(x, dict)]
        existing = {d.group or "在线底图" for d in defs}
        if name in existing:
            raise ValueError(f"分组已存在：{name}")
        migrated = _migrate_legacy_list_to_dict_text(text)
        new_text = _ensure_group_in_v2_text(migrated, name)
        path.write_text(new_text, encoding="utf-8")
        return load_online_basemap_groups(path)

    if isinstance(parsed, dict):
        # v2 dict 形态
        lines = text.splitlines()
        grp_i, item_i, grp_end = _locate_group_item(lines, name)
        if grp_i is None:
            raise ValueError(f"{path} v2 dict 缺 groups: 段（数据损坏）")
        if item_i is not None:
            raise ValueError(f"分组已存在：{name}")
        # 在 groups: 段末尾追加
        grp_indent_match = re.match(r"^(\s*)groups:", lines[grp_i])
        if not grp_indent_match:
            raise ValueError(f"{path} groups: 行格式异常")
        item_indent = len(grp_indent_match.group(1)) + 2
        insert = [f"{' ' * item_indent}- {name}"]
        if grp_end < len(lines):
            insert.append("")
        lines[grp_end:grp_end] = insert
        new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        path.write_text(new_text, encoding="utf-8")
        return load_online_basemap_groups(path)

    raise ValueError(f"{path} 顶层 schema 不支持（既非 list 也非 dict）")


def reassign_online_basemap_group_in_basemaps(
    path: Path, old: str, new: str
) -> list[OnlineBasemapDef]:
    """把 ``basemaps:`` 段内所有 ``group: old`` 的条目 ``group`` 字段改成 ``new``。

    Dock「删除分组」时把条目重分配到目标分组的写回原语：

    - 校验 ``old`` / ``new``（同 :func:`_validate_group_name`），失败抛 :class:`ValueError`。
    - 旧 list 形态 → 整体升级为 v2 dict（一次性迁移）。
    - v2 dict 形态 → 文本替换 ``basemaps:`` 段内 ``group: <old>``（精确匹配，区分大小写）；
      ``groups:`` 段**不动**（让用户显式调"删除分组"移除 ``- old``）。
    - ``basemaps:`` 段缺 → 直接返回（v2 dict 但仅 ``groups:`` 段有内容，不报错）。

    返回更新后的完整 ``defs`` 列表（调用方用它刷新 Dock 树）。
    """
    old = _validate_group_name(old)
    new = _validate_group_name(new)

    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return []  # 空文件 → 无条目可重分配

    text = path.read_text(encoding="utf-8")
    load_online_basemaps(path)  # 校验文件可解析
    parsed = yaml.safe_load(text)

    if isinstance(parsed, list):
        # 旧 list 形态：迁移为 v2 dict，再走 v2 文本替换
        migrated = _migrate_legacy_list_to_dict_text(text)
        new_text = _reassign_group_in_v2_basemaps(migrated, old, new)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
        return load_online_basemaps(path)

    if isinstance(parsed, dict):
        new_text = _reassign_group_in_v2_basemaps(text, old, new)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
        return load_online_basemaps(path)

    raise ValueError(f"{path} 顶层 schema 不支持（既非 list 也非 dict）")


def _reassign_group_in_v2_basemaps(text: str, old: str, new: str) -> str:
    """v2 dict 形态下文本替换 ``basemaps:`` 段内 ``group: old`` → ``group: new``。

    精确匹配 ``group: old`` / ``group: "old"`` / ``group: 'old'``（含引号、含行尾注释），
    也吃 ``- group: old``（列表项首行 = basemaps: 段内常见情况）。
    只动 ``basemaps:`` 段；``groups:`` 段不动。返回新文本（不落盘）。"""
    lines = text.splitlines()
    bm_i, _bm_indent, bm_end = _basemaps_block_range(lines)
    if bm_i is None:
        return text  # v2 dict 但缺 basemaps: 段（极少见）→ 不报错
    # 匹配行：``  group: old`` / ``  - group: "old"`` / 带行尾注释
    # group(1) = 前导空白 + 可选 ``- ``，group(2) = 尾随空白，group(3) = 行尾注释
    group_field_pat = re.compile(
        rf'^(\s*(?:-\s+)?)group:\s*["\']?{re.escape(old)}["\']?(\s*)(#.*)?$'
    )
    changed = False
    for i in range(bm_i + 1, bm_end):
        m = group_field_pat.match(lines[i])
        if m:
            prefix, tail_ws, tail_comment = m.group(1), m.group(2), m.group(3) or ""
            lines[i] = f'{prefix}group: "{new}"{tail_ws}{tail_comment}'
            changed = True
    if not changed:
        return text
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def remove_online_basemap_group_from_yaml(
    path: Path, name: str, cascade: bool = False
) -> list[str]:
    """从 ``groups:`` 段移除 ``- name``（独立持久化的分组管理原语）。

    Dock 分组右键「删除分组」的写回原语：

    - 校验 ``name``（同 :func:`_validate_group_name`），失败抛 :class:`ValueError`。
    - ``groups:`` 段缺该名 → 抛 :class:`ValueError`（不重复删）。
    - ``cascade=False``（默认）：若 ``basemaps:`` 段还有 ``group: name`` 的条目 → 抛
      :class:`ValueError`（让调用方先做"重分配到目标分组"或传 ``cascade=True``）。
    - ``cascade=True``：同时移除 ``basemaps:`` 段所有 ``group: name`` 的条目
      （破坏性操作，需调用方二次确认）。
    - 旧 list 形态：原 schema 没有独立 groups 段 → 直接抛 :class:`ValueError`
      （旧版分组隐式聚合自条目，删分组无意义；调用方应先升级文件再做）。

    返回更新后的完整 ``groups`` 列表。
    """
    name = _validate_group_name(name)

    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        raise ValueError(f"分组不存在：{name}")

    text = path.read_text(encoding="utf-8")
    load_online_basemap_groups(path)  # 校验文件可解析
    parsed = yaml.safe_load(text)

    if isinstance(parsed, list):
        # 旧 list 形态没有独立 groups 段 → 升级后再删
        # 但升级会破坏用户可能加的行内注释；这里选择"先升级再删"以保证删除语义统一
        # 实际上，调用方（mixin）应在 add/append/edit/remove_basemap 任意一次后让文件
        # 升级到 v2，本函数假定文件已是 v2；如果还没升级（理论上不应该发生）→ 抛错
        raise ValueError(
            f"{path} 仍为旧 list schema，请先触发一次 add/append/edit 升级到 v2"
        )

    if not isinstance(parsed, dict):
        raise ValueError(f"{path} 顶层 schema 不支持（既非 list 也非 dict）")

    lines = text.splitlines()
    grp_i, item_i, grp_end = _locate_group_item(lines, name)
    if grp_i is None:
        raise ValueError(f"{path} v2 dict 缺 groups: 段（数据损坏）")
    if item_i is None:
        raise ValueError(f"分组不存在：{name}")

    # 检查 basemaps: 段是否还有 group: name 的条目
    bm_i, _, bm_end = _basemaps_block_range(lines)
    if not cascade and bm_i is not None:
        ref_pat = re.compile(rf'^\s*(?:-\s+)?group:\s*["\']?{re.escape(name)}["\']?(\s|$)')
        for i in range(bm_i + 1, bm_end):
            if ref_pat.match(lines[i]):
                raise ValueError(
                    f"分组「{name}」下还有底图条目，请先重分配到目标分组或确认一并删除"
                )

    # 从 groups: 段移除 `- name` 行
    if cascade and bm_i is not None:
        # 同步移除 basemaps: 段内所有 group: name 的条目。
        # ``basemaps:`` 段在 ``groups:`` 段**后面**，删 basemaps 条目不影响 groups 段行号，
        # 所以删前记录下的 ``item_i`` 在删后仍有效（重新查找会失败是因为 basemaps 重定位）。
        to_remove_ranges: list[tuple[int, int]] = []
        prefix = "  - "
        i = bm_i + 1
        while i < bm_end:
            if not lines[i].startswith(prefix):
                i += 1
                continue
            block_end = _item_block_lines(lines, i)
            block = "\n".join(lines[i:block_end])
            try:
                parsed_block = yaml.safe_load(block)
            except yaml.YAMLError:
                i = block_end
                continue
            if (
                isinstance(parsed_block, list)
                and parsed_block
                and isinstance(parsed_block[0], dict)
                and parsed_block[0].get("group") == name
            ):
                to_remove_ranges.append((i, block_end))
            i = block_end
        # 从后往前删（避免 index 漂移）
        for s, e in reversed(to_remove_ranges):
            del lines[s:e]
        # item_i 仍然指向 groups: 段中原始 `- name` 行位置（basemaps 删除不会动 groups 段）
        # 不重新查 _locate_group_item（避免它把空 basemaps 段之后的“其它”误为 groups: 段结尾）

    # 从 groups: 段删除 item_i 行
    del lines[item_i]
    new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    path.write_text(new_text, encoding="utf-8")
    return load_online_basemap_groups(path)


def reorder_online_basemap_groups_in_yaml(
    path: Path, new_order: list[str]
) -> list[str]:
    """按 ``new_order`` 重排 ``groups:`` 段（仅动 groups 段，basemaps 段不动）。

    Dock 分组节点拖拽排序的写回原语：用户拖动顶部分组到新位置后，调用方拿到新顺序
    调本函数把 groups 段重新排列成同一顺序。**不**动 basemaps 段（basemaps 段是
    条目列表，重排不在本原语职责范围；Dock 显示顺序由调用方拿新 groups 传
    :meth:`OnlineBasemapDock.set_layers` 重建）。

    旧 list schema 没有独立 groups 段 → 抛 :class:`ValueError`（调用方应先升级文件
    到 v2 形态再拖动——这与 :func:`remove_online_basemap_group_from_yaml` 的“先升级
    再操作”语义一致）。

    校验：
    - 文件存在 / 可解析 / 顶层是 v2 dict 且含 groups: 段；否则抛 :class:`ValueError`。
    - ``new_order`` 长度 / 元素集合均与当前 ``groups:`` 段一致（**不改名 / 不增删**）；
      顺序差异才被接受（也仅顺序会被写回）。
    - 同名校验：``new_order`` 内部不能有重复名（会产生两个 - name 同名行，
      PyYAML 解析为同一列表 → 静默丢一）。

    返回更新后的 ``groups`` 列表（== ``new_order``）。空 ``groups:`` 段 / 仅注释
    场景调用方不走拖动（不产生 reorder 意图）→ 本函数不专门处理。
    """
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        raise ValueError(f"{path} 为空，无法重排分组")
    text = path.read_text(encoding="utf-8")
    current = load_online_basemap_groups(path)  # 校验文件可解析
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise ValueError(
            f"{path} 仍为旧 list schema，请先触发一次 add/append/edit 升级到 v2 后再拖动排序"
        )

    if Counter(new_order) != Counter(current):
        raise ValueError(
            f"new_order 必须是当前 groups: 段的一个重排（每个分组名出现次数一致）；"
            f"current={current}，new_order={new_order}"
        )

    if new_order == current:
        return current  # no-op：未变更，**不**落盘、不改 timestamp

    lines = text.splitlines()
    grp_i, child_indent, grp_end = _dict_top_block_range(lines, "groups")
    if grp_i is None:
        raise ValueError(f"{path} v2 dict 缺 groups: 段（数据损坏）")

    # 收集每个 group 的块：起始于 ``- name: X`` 行（与本段同缩进），结束于下一个
    # 同行级 ``- name`` 之前。块内可携带该 group 后的尾随空行 / 注释（—从 - name 行
    # 起直到下一个 - name 之前的所有行都顺组走，重排后仍顺该组）。
    blocks: dict[str, list[str]] = {name: [] for name in current}
    i = grp_i + 1
    while i < grp_end:
        s = lines[i].lstrip()
        if s.startswith("- "):
            name = s[2:].split("#", 1)[0].strip()  # 处理 "- 名称 # 注释" 形态
            if name not in blocks:
                # groups: 段下的 - 行但不匹配任何 group 名（除名后有大量尾随注释
                # 被误进，下个 - name 仍会正确块）——保守走 fallthrough：把此行
                # 并入“上一个已收 block”末尾，避免丢行。
                if blocks:
                    last = next(reversed(blocks))
                    blocks[last].append(lines[i])
                i += 1
                continue
            cur_block: list[str] = [lines[i]]
            j = i + 1
            while j < grp_end:
                ln2 = lines[j]
                if ln2.lstrip().startswith("- ") and (
                    len(ln2) - len(ln2.lstrip()) == child_indent
                ):
                    break  # 下一个 - name
                cur_block.append(ln2)
                j += 1
            blocks[name] = cur_block
            i = j
        else:
            # 跳过段首缩进外的空行 / 注释（首块前面跟随的 — 原全文件都保留）
            i += 1

    # 按 new_order 重排，组间补一个空行（统一格式；原块内空行跟随本组）
    new_block_lines: list[str] = []
    for k, name in enumerate(new_order):
        if k > 0:
            new_block_lines.append("")
        new_block_lines.extend(blocks[name])

    new_lines = lines[: grp_i + 1] + new_block_lines + lines[grp_end:]
    new_text = "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")
    path.write_text(new_text, encoding="utf-8")
    return new_order


_MAP_KEY = re.compile(r"^map:\s*$")  # 顶层 map:（缩进 0）
_KEYS_KEY = re.compile(r"^(\s*)keys:\s*$")
_KEY_FIELD = re.compile(r"^(\s*)(tianditu_tk|gaode_key):\s*(.*)$")
_KEY_FIELDS = ("tianditu_tk", "gaode_key")


def _key_scalar(v: str) -> str:
    """字段值文本：空串写 ``""``（仓库空值风格），非空走 safe_dump（自动引号）。

    ``yaml.safe_dump`` 对**根级标量**会额外补 ``...`` 文档结束标记行（防止后面
    顶层键被并进同一文档），拼接单行键值前必须剥掉该行。
    """
    if v == "":
        return '""'
    out = yaml.safe_dump(str(v), allow_unicode=True).rstrip("\n")
    if out.endswith("\n..."):
        out = out[:-4]  # 剥掉文档结束标记 `...` 行
    return out


def _keys_block_lines(keys: MapKeysConfig, field_indent: int) -> list[str]:
    """``keys:`` 内部字段行（tianditu_tk → gaode_key 固定序）。"""
    ind = " " * field_indent
    return [f"{ind}tianditu_tk: {_key_scalar(keys.tianditu_tk)}", f"{ind}gaode_key: {_key_scalar(keys.gaode_key)}"]


def set_map_keys(path: Path, keys: MapKeysConfig) -> None:
    """把 ``map.keys`` 集中 key **文本写回** config（只动本段，注释/其余排版保留）。

    找不到 ``map.keys`` 键则在 ``map:`` 行后整块插入；已存在则逐行更新
    ``tianditu_tk:`` / ``gaode_key:``（保留行前导缩进与行尾注释），缺失字段
    在块尾补齐。缺顶层 ``map:`` 时抛 :class:`ValueError`。
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    map_i = next((i for i, ln in enumerate(lines) if _MAP_KEY.match(ln)), None)
    if map_i is None:
        raise ValueError(f"{path} 缺少顶层 map: 键，无法写回 map.keys")

    def _indent(ln: str) -> int:
        return len(ln) - len(ln.lstrip())

    def _is_comment_or_blank(ln: str) -> bool:
        return (not ln.strip()) or ln.lstrip().startswith("#")

    # map: 的子键缩进 = 第一个非注释非空行缩进；缺省 2（map: 是顶层，这里兜底）
    child_indent = None
    for ln in lines[map_i + 1 :]:
        if not _is_comment_or_blank(ln):
            child_indent = _indent(ln)
            break
    if child_indent is None:
        child_indent = 2
    keys_i = None
    for i in range(map_i + 1, len(lines)):
        ln = lines[i]
        if _indent(ln) < child_indent and not _is_comment_or_blank(ln):
            break  # 越过 map: 子块（已到 qgis: 等顶层键）
        if _KEYS_KEY.match(ln) and _indent(ln) == child_indent:
            keys_i = i
            break

    if keys_i is None:
        # 整块插到 map: 行之后（map: 与首子键之间的注释行也随之后移）
        comment = [
            "",
            " " * child_indent + "# 底图服务 API Key（provider 集中管理，见下方注释）",
            " " * child_indent + "# 天地图 {tk} → tianditu_tk；高德 {gaode_key}（预留）。换 key 只改这里。",
            " " * child_indent + "keys:",
        ]
        lines[map_i + 1 : map_i + 1] = comment + _keys_block_lines(keys, child_indent + 2)
    else:
        keys_indent = _indent(lines[keys_i])
        # keys: 内部范围 = 其后至第一个缩进 <= keys_indent 的非注释非空行（兄弟块）
        end = keys_i + 1
        while end < len(lines):
            ln = lines[end]
            if _is_comment_or_blank(ln):
                end += 1
                continue
            if _indent(ln) > keys_indent:
                end += 1
                continue
            break
        # 逐字段更新既有行；没找到的记下补插位置
        missing: list[str] = []
        for name in _KEY_FIELDS:
            hit = None
            for j in range(keys_i + 1, end):
                m = _KEY_FIELD.match(lines[j])
                if m and m.group(2) == name:
                    hit = j
                    break
            if hit is not None:
                indent = len(lines[hit]) - len(lines[hit].lstrip())
                tail = ""
                m = _KEY_FIELD.match(lines[hit])
                rest = m.group(3)
                hash_i = rest.find("#")
                if hash_i >= 0:
                    # 行尾注释连同其前导空白一起保留（值后无缝拼接；`#` 前需空白才是注释）
                    start = hash_i
                    while start > 0 and rest[start - 1] == " ":
                        start -= 1
                    tail = rest[start:]
                lines[hit] = f"{' ' * indent}{name}: {_key_scalar(getattr(keys, name))}{tail}"
            else:
                missing.append(name)
        if missing:
            # 在 keys: 内部最后一个非空行之后补缺字段
            insert_at = keys_i + 1
            for j in range(end - 1, keys_i, -1):
                if lines[j].strip():
                    insert_at = j + 1
                    break
            ind = " " * (keys_indent + 2)
            missing_lines = [f"{ind}{name}: {_key_scalar(getattr(keys, name))}" for name in missing]
            lines[insert_at:insert_at] = missing_lines

    path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
