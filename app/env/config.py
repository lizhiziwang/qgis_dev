"""应用配置加载。"""

from __future__ import annotations

import os
import re
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
    """启动登录的本地校验账号（未接真实 auth 服务前只认这一对账号）。"""

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

#: 独立清单文件的注释头（建新文件 / 首条入库时写）
_ONLINE_BASEMAPS_HEADER = [
    "# 在线底图清单（Dock「在线底图」树的数据源；独立于 app.yaml）",
    "# group → Dock 分组名；type 取值 xyz（默认）/ arcgis / arcgisfeature（FeatureServer 矢量要素）/",
    "#   wms / wmts（非 xyz 不写 zmin/zmax/referer，见各条目注释）。双击条目 / 叶子右键「添加到画布」→ 出图。",
    "# Dock 右键「添加在线底图」（内置模板）/「添加自定义底图」（弹窗配置）都**文本追加**",
    "# 写回本文件——条目注释/排版原样保留，只把新条目排在清单末尾。叶子右键「编辑连接」",
    "# （弹窗编辑名称/url/key/认证等）则**原位替换**该条目，其余文本同样保留。",
    "# 天地图 URL 里的 tk 用 {tk} 占位符，真实 key 见 app.yaml 的 map.keys。",
]


def load_online_basemaps(path: Path) -> list[OnlineBasemapDef]:
    """读取独立在线底图清单文件（顶层 YAML 列表）。

    文件缺失 / 空 / 纯注释 → ``[]``；顶层不是列表（yaml 解析出 dict/标量等）→
    抛 :class:`ValueError`（调用方记日志转 critical，Dock 显示「未配置」）。
    """
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    data = yaml.safe_load(text)
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError(f"{path} 顶层应为在线底图清单（YAML 列表），实际为 {type(data).__name__}")
    return [OnlineBasemapDef.from_dict(x) for x in data]


def _write_online_basemap_file(path: Path, defs: list[OnlineBasemapDef]) -> None:
    """整体写独立清单文件：注释头 + 条目块（每条前空行分隔）。"""
    lines = list(_ONLINE_BASEMAPS_HEADER)
    for i, d in enumerate(defs):
        if i:
            lines.append("")
        lines.extend(_item_block(d, 0))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def append_online_basemap_to_yaml(path: Path, d: OnlineBasemapDef) -> list[OnlineBasemapDef]:
    """把一条在线底图**文本追加**到独立清单文件 ``path``（顶层 YAML 列表）。

    在线底图配置自 ``config/app.yaml`` 独立到 ``online_basemap_config.yaml`` 后，
    这是唯一的写回原语：文件缺失 / 为空 → 建文件（注释头 + 第一条目）；已有 →
    在**最后一个列表项**之后插入新条目块，文件其余部分（注释、排版）原样保留——
    PyYAML 整文件 round-trip 会丢注释，故按行处理。返回追加后的完整清单
    （调用方通常用它刷新 Dock 树）。
    """
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        _write_online_basemap_file(path, [d])
        return load_online_basemaps(path)
    text = path.read_text(encoding="utf-8")
    # 先校验当前文件可解析成列表：坏文件 / 非列表顶层（如残留 dict）不落盘直接抛给
    # 调用方，避免往无法解析的文件里继续拼条目
    load_online_basemaps(path)
    lines = text.splitlines()

    last_dash_i = None
    for i, ln in enumerate(lines):
        if ln.startswith("- "):  # 顶层列表项（列 0 的 `- `）
            last_dash_i = i
    if last_dash_i is None:
        # 纯注释头（尚无条目）→ 追加在末尾（注释仍留文件顶）
        lines += ["", *_item_block(d, 0)]
    else:
        # 从最后一项末尾往后延：空行 / 缩进 > 0 的行属该项正文；遇缩进 0 的非空行
        # （文件尾注释）或 EOF 即止——把注释块留在插入点后面
        j = last_dash_i + 1
        while j < len(lines):
            line = lines[j]
            if not line.strip():
                j += 1
                continue
            if line[:1].isspace():
                j += 1
                continue
            break
        insert = ["", *_item_block(d, 0)]
        if j < len(lines):
            insert.append("")  # 新条目与文件尾注释之间留空行
        lines[j:j] = insert

    path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
    # 回读清单，供调用方刷新
    return load_online_basemaps(path)


def _item_block_lines(lines: list[str], dash_i: int) -> int:
    """顶层列表项文本块的范围终点：``dash_i`` 之后遇到列 0 非空行（下一条目 /
    文件尾注释）或 EOF 为止（块内空行 / 缩进行——含行内注释——都属本条）。"""
    end = dash_i + 1
    while end < len(lines):
        ln = lines[end]
        if not ln.strip() or ln[:1].isspace():
            end += 1
            continue
        break
    return end


def update_online_basemap_in_yaml(path: Path, old: OnlineBasemapDef, new: OnlineBasemapDef) -> list[OnlineBasemapDef]:
    """把独立清单文件里与 ``old`` 字段相等的条目**原位文本替换**成 ``new``。

    Dock 叶子右键「编辑连接」的写回原语：``old`` 须是 :func:`load_online_basemaps`
    解析出的条目之一（Dock 树刷新时拿到的正是这份 def，字段级相等即可命中）。只重写
    命中条目的文本块（条目内的行内注释随值变化整体重写，避免旧注释误导），文件头 /
    其余条目 / 尾部注释与排版原样保留——与 :func:`append_online_basemap_to_yaml`
    同按行处理（PyYAML 整文件 round-trip 会丢注释）。找不到 ``old``（文件被外部
    改动）或顶层不是列表 → 抛 :class:`ValueError`，不落盘。返回替换后的完整清单
    （调用方用于刷新 Dock 树）。
    """
    if not path.exists():
        raise ValueError(f"{path} 不存在，无法编辑在线底图")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"{path} 为空，无法编辑在线底图")
    load_online_basemaps(path)  # 坏文件 / 非列表顶层先抛（不落盘）
    lines = text.splitlines()

    # 定位与 old 字段级相等的顶层列表项（列 0 的 `- ` 起、块终点见 _item_block_lines）
    dash_i = None
    end = None
    for i, ln in enumerate(lines):
        if not ln.startswith("- "):
            continue
        block_end = _item_block_lines(lines, i)
        block = "\n".join(lines[i:block_end])
        try:
            parsed = yaml.safe_load(block)
        except yaml.YAMLError:
            continue  # load_online_basemaps 已整体校验过，这里防御性跳过
        if isinstance(parsed, list) and parsed and OnlineBasemapDef.from_dict(parsed[0]) == old:
            dash_i, end = i, block_end
            break
    if dash_i is None:
        raise ValueError(f"未在 {path} 中找到要编辑的条目（文件可能已被外部修改，请刷新后重试）")

    had_blank = any(not ln.strip() for ln in lines[dash_i:end])
    new_lines = lines[:dash_i] + _item_block(new, 0)
    if had_blank:
        new_lines.append("")  # 原条目与后续文本（下一条目 / 尾注释）之间的空行补回
    new_lines += lines[end:]
    path.write_text(
        "\n".join(new_lines) + ("\n" if text.endswith("\n") else ""),
        encoding="utf-8",
    )
    # 回读清单，供调用方刷新
    return load_online_basemaps(path)


def remove_online_basemap_from_yaml(path: Path, target: OnlineBasemapDef) -> list[OnlineBasemapDef]:
    """把独立清单文件里与 ``target`` 字段相等的条目**文本移除**（其余原样保留）。

    Dock 叶子右键「删除连接」的写回原语：``target`` 须是 :func:`load_online_basemaps`
    解析出的条目之一（Dock 树刷新时拿到的正是这份 def）。只删除命中条目的文本块
    （块内空行归本条，见 :func:`_item_block_lines`），文件头 / 其余条目 / 尾部注释
    与排版原样保留——与 :func:`append_online_basemap_to_yaml` /
    :func:`update_online_basemap_in_yaml` 同按行处理（PyYAML 整文件 round-trip 会丢
    注释）。删光所有条目后文件只剩头部注释（可正常解析为空清单）。找不到
    ``target``（文件被外部改动）或顶层不是列表 → 抛 :class:`ValueError`，不落盘。
    返回删除后的完整清单（可为空列表；调用方用于刷新 Dock 树）。
    """
    if not path.exists():
        raise ValueError(f"{path} 不存在，无法删除在线底图")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"{path} 为空，无法删除在线底图")
    load_online_basemaps(path)  # 坏文件 / 非列表顶层先抛（不落盘）
    lines = text.splitlines()

    # 定位与 target 字段级相等的顶层列表项（列 0 的 `- ` 起、块终点见 _item_block_lines）
    dash_i = None
    end = None
    for i, ln in enumerate(lines):
        if not ln.startswith("- "):
            continue
        block_end = _item_block_lines(lines, i)
        block = "\n".join(lines[i:block_end])
        try:
            parsed = yaml.safe_load(block)
        except yaml.YAMLError:
            continue  # load_online_basemaps 已整体校验过，这里防御性跳过
        if isinstance(parsed, list) and parsed and OnlineBasemapDef.from_dict(parsed[0]) == target:
            dash_i, end = i, block_end
            break
    if dash_i is None:
        raise ValueError(f"未在 {path} 中找到要删除的条目（文件可能已被外部修改，请刷新后重试）")

    path.write_text(
        "\n".join(lines[:dash_i] + lines[end:]) + ("\n" if text.endswith("\n") else ""),
        encoding="utf-8",
    )
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
