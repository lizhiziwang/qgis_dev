"""符号库注册表：用户符号 / QGIS 系统符号 / 外部样式文件。

「样式引用」对话框与「符号选择器」共用本模块：

- 内置库：**用户符号**（``<config>/symbols/*.xml``）与 **QGIS 系统符号库**
  （``QgsStyle.defaultStyle()``）；
- 外部库：用户通过「将样式添加到列表」引入的 QGIS XML / QML 文件，按文件读取符号；
- 启用状态落在 ``<config>/state/symbol_libraries.json``（不写 QSettings），
  符号选择器只列出勾选启用的库。

**ArcMap 的 ``.style`` 是 ESRI 私有二进制格式**，QGIS/GDAL 没有读取能力，
这里明确拒绝（提示用户转成 QGIS XML/QML），不假装支持。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from qgis.core import (
    QgsReadWriteContext,
    QgsStyle,
    QgsSymbol,
    QgsSymbolLayerUtils,
)
from qgis.PyQt.QtXml import QDomDocument

from app.infra.fs.paths import Paths
from app.infra.fs.state import load_state, save_state
from app.infra.logging import get_logger

logger = get_logger(__name__)

_STATE = "symbol_libraries"

#: 状态里记录「隐藏的分组」（样式引用里取消勾选的分组）
_HIDDEN_KEY = "hidden_groups"

#: 内置库（用户符号在前，系统库在后）
_DEFAULT_LIBRARIES = (
    {"id": "user", "name": "用户符号", "kind": "user"},
    {"id": "system", "name": "QGIS 系统符号库", "kind": "system"},
)

#: QGIS 默认样式的标签 → 中文显示名
_TAG_NAMES = {
    "Showcase": "展示",
    "Colorful": "彩色",
    "Grayscale": "灰度",
    "Topography": "地形",
}

_SYMBOL_KINDS = {
    QgsSymbol.SymbolType.Marker: "point",
    QgsSymbol.SymbolType.Line: "line",
    QgsSymbol.SymbolType.Fill: "polygon",
}

#: 通用符号的颜色变体（红 / 绿 / 蓝，够用且不把符号网格撑得太长）
_GENERAL_COLORS = (
    ("red", "红", "#d73027"),
    ("green", "绿", "#1a9850"),
    ("blue", "蓝", "#2b83ba"),
)


@dataclass(frozen=True)
class LibrarySymbol:
    source: str  # "user" / "system" / "external"
    ident: str
    name: str
    group: str
    symbol: object

    @property
    def key(self) -> str:
        return f"{self.source}:{self.ident}"


def symbols_dir():
    return Paths.from_env().config / "symbols"


def libraries_dir():
    """「新建符号库」文件的默认目录（与用户单符号目录分开）。"""
    return Paths.from_env().config / "symbol_libraries"


def safe_file_stem(name: str) -> str:
    stem = re.sub(r'[\\/:*?"<>|]+', "_", str(name or "")).strip(" .")
    return stem or "symbol"


def save_user_symbol(name: str, symbol) -> str | None:
    """把符号存成用户符号库里的 XML；返回实际文件名（不含扩展名）。"""
    folder = symbols_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("创建符号目录失败：%s", exc)
        return None
    stem = safe_file_stem(name)
    target = folder / f"{stem}.xml"
    index = 2
    while target.exists():
        target = folder / f"{stem}_{index}.xml"
        index += 1
    try:
        doc = QDomDocument("qgis_style")
        root = doc.createElement("qgis_style")
        doc.appendChild(root)
        root.appendChild(
            QgsSymbolLayerUtils.saveSymbol(
                stem, symbol.clone(), doc, QgsReadWriteContext()
            )
        )
        target.write_text(doc.toString(2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("保存用户符号失败：%s", exc)
        return None
    return target.stem


def save_symbol_library(path: str, name: str, symbol) -> bool:
    """把符号写成一份新的 QGIS 样式 XML（一个符号库文件）。"""
    from pathlib import Path

    target = Path(str(path))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        doc = QDomDocument("qgis_style")
        root = doc.createElement("qgis_style")
        doc.appendChild(root)
        root.appendChild(
            QgsSymbolLayerUtils.saveSymbol(
                str(name), symbol.clone(), doc, QgsReadWriteContext()
            )
        )
        target.write_text(doc.toString(2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("保存符号库失败 %s：%s", path, exc)
        return False
    return True


def delete_user_symbol(stem: str) -> bool:
    """从用户符号库删除一个符号（删 ``config/symbols/<stem>.xml``）。

    ``stem`` 来自 :func:`_load_user_symbols` 的 ``path.stem``，这里只取文件名，
    不接受带路径的值。文件已不存在也视为成功（结果一致）。
    """
    name = os.path.basename(str(stem or ""))
    if not name:
        return False
    target = symbols_dir() / f"{name}.xml"
    try:
        target.unlink()
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.warning("删除用户符号失败 %s：%s", target, exc)
        return False
    return True


def delete_symbol_library_file(path: str) -> bool:
    """删除一个外部符号库文件；文件已不存在也视为成功。"""
    if not str(path or "").strip():
        return False
    from pathlib import Path

    file_path = Path(str(path))
    try:
        file_path.unlink()
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.warning("删除符号库文件失败 %s：%s", file_path, exc)
        return False
    return True


def _load_user_symbols(kind: str) -> list[LibrarySymbol]:
    folder = symbols_dir()
    if not folder.is_dir():
        return []
    out: list[LibrarySymbol] = []
    for path in sorted(folder.glob("*.xml")):
        try:
            doc = QDomDocument()
            if not doc.setContent(path.read_text(encoding="utf-8")):
                continue
            nodes = doc.elementsByTagName("symbol")
            if nodes.count() < 1:
                continue
            symbol = QgsSymbolLayerUtils.loadSymbol(
                nodes.at(0).toElement(), QgsReadWriteContext()
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("读取用户符号 %s 失败：%s", path, exc)
            continue
        if symbol is None or _SYMBOL_KINDS.get(symbol.type()) != kind:
            continue
        out.append(
            LibrarySymbol(
                source="user",
                ident=path.stem,
                name=path.stem,
                group="用户符号",
                symbol=symbol,
            )
        )
    return out


def _load_system_symbols(kind: str) -> list[LibrarySymbol]:
    try:
        style = QgsStyle.defaultStyle()
        names = style.symbolNames()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取系统符号库失败：%s", exc)
        return []
    out: list[LibrarySymbol] = []
    for name in names:
        try:
            symbol = style.symbol(name)
        except Exception:  # noqa: BLE001
            continue
        if symbol is None or _SYMBOL_KINDS.get(symbol.type()) != kind:
            continue
        try:
            tags = list(style.tagsOfSymbol(QgsStyle.SymbolEntity, name))
        except Exception:  # noqa: BLE001
            tags = []
        group = _TAG_NAMES.get(str(tags[0]), str(tags[0])) if tags else "系统样式"
        out.append(
            LibrarySymbol(
                source="system",
                ident=str(name),
                name=str(name),
                group=group,
                symbol=symbol,
            )
        )
    return out


def _load_general_symbols(kind: str) -> list[LibrarySymbol]:
    """应用内置的「通用符号」：形状 / 填充样式 / 线型 × 红 / 绿 / 蓝。

    这些原来在右侧参数的下拉里选；改成符号库分组后，用户直接从左侧点选，
    右侧只保留颜色 / 大小 / 线宽等可继续微调的参数。
    """
    from qgis.core import QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol

    from app.ui.dialogs.layer_properties.render import (
        _DEFAULT_FILL,
        _DEFAULT_STROKE,
        _FILL_STYLES,
        _LINE_STYLES,
        _MARKER_SHAPES,
    )

    out: list[LibrarySymbol] = []
    if kind == "polygon":
        for text, name, _enum in _FILL_STYLES:
            for key, label, color in _GENERAL_COLORS:
                # 「无填充」颜色没处可上，用描边色区分几种变体
                outline = color if (name == "no" and color) else _DEFAULT_STROKE.name()
                symbol = QgsFillSymbol.createSimple(
                    {
                        "color": color or _DEFAULT_FILL.name(),
                        "outline_color": outline,
                        "style": name,
                        "outline_width": "0.26",
                    }
                )
                out.append(
                    LibrarySymbol(
                        "general",
                        f"fill:{name}:{key}",
                        f"{text} · {label}",
                        "通用符号",
                        symbol,
                    )
                )
    elif kind == "line":
        for text, name, _enum in _LINE_STYLES:
            for key, label, color in _GENERAL_COLORS:
                symbol = QgsLineSymbol.createSimple(
                    {
                        "line_color": color,
                        "line_style": name,
                        "line_width": "0.26",
                    }
                )
                out.append(
                    LibrarySymbol(
                        "general",
                        f"line:{name}:{key}",
                        f"{text} · {label}",
                        "通用符号",
                        symbol,
                    )
                )
    elif kind == "point":
        for text, name, _enum in _MARKER_SHAPES:
            for key, label, color in _GENERAL_COLORS:
                symbol = QgsMarkerSymbol.createSimple(
                    {
                        "name": name,
                        "color": color,
                        # 空心标记（十字等）靠描边色上色；实心标记描边宽 0，不受影响
                        "outline_color": color,
                        # 通用点符号默认 4 毫米（原来是 QGIS 的 2 毫米，实际显示偏小）
                        "size": "4.0",
                        "outline_width": "0.0",
                    }
                )
                out.append(
                    LibrarySymbol(
                        "general",
                        f"shape:{name}:{key}",
                        f"{text} · {label}",
                        "通用符号",
                        symbol,
                    )
                )
    return out


def _load_external_symbols(kind: str, path: str, label: str) -> list[LibrarySymbol]:
    """外部 QGIS 样式 XML / QML：逐个 <symbol> 读取。"""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        doc = QDomDocument()
        if not doc.setContent(content):
            return []
    except (OSError, ValueError) as exc:
        logger.info("读取外部样式 %s 失败：%s", path, exc)
        return []
    out: list[LibrarySymbol] = []
    nodes = doc.elementsByTagName("symbol")
    for index in range(nodes.count()):
        element = nodes.at(index).toElement()
        try:
            symbol = QgsSymbolLayerUtils.loadSymbol(
                element, QgsReadWriteContext()
            )
        except Exception:  # noqa: BLE001
            continue
        if symbol is None or _SYMBOL_KINDS.get(symbol.type()) != kind:
            continue
        name = element.attribute("name") or os.path.splitext(os.path.basename(path))[0]
        out.append(
            LibrarySymbol(
                source="external",
                ident=f"{path}#{index}",
                name=name,
                group=label,
                symbol=symbol,
            )
        )
    return out


def load_libraries() -> list[dict]:
    """当前注册的库（内置 + 外部），带 ``enabled`` 状态。"""
    data = load_state(_STATE).get("libraries")
    saved = {
        str(entry.get("id") or entry.get("path")): entry
        for entry in data
        if isinstance(entry, dict)
    } if isinstance(data, list) else {}
    result: list[dict] = []
    for default in _DEFAULT_LIBRARIES:
        row = saved.get(default["id"], {})
        result.append(
            {
                **default,
                "enabled": bool(row.get("enabled", True)),
            }
        )
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict) or entry.get("kind") != "external":
                continue
            path = str(entry.get("path") or "").strip()
            if not path:
                continue
            result.append(
                {
                    "id": path,
                    "name": str(entry.get("name") or os.path.basename(path)),
                    "kind": "external",
                    "path": path,
                    "enabled": bool(entry.get("enabled", True)),
                }
            )
    return result


def save_libraries(entries: list[dict]) -> None:
    data = load_state(_STATE)
    data["libraries"] = [
        {
            key: row[key]
            for key in ("id", "name", "kind", "path", "enabled")
            if key in row
        }
        for row in entries
    ]
    save_state(_STATE, data)


def load_hidden_groups() -> set[str]:
    """「样式引用」里被取消勾选的分组名。"""
    data = load_state(_STATE)
    values = data.get(_HIDDEN_KEY)
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values}


def save_hidden_groups(groups: set[str]) -> None:
    data = load_state(_STATE)
    data[_HIDDEN_KEY] = sorted(str(group) for group in groups)
    save_state(_STATE, data)


def _load_all_library_symbols(kind: str) -> list[LibrarySymbol]:
    """未按「样式引用」过滤的全部符号。"""
    out: list[LibrarySymbol] = []
    for entry in load_libraries():
        if not entry.get("enabled"):
            continue
        entry_kind = entry.get("kind")
        if entry_kind == "user":
            out.extend(_load_user_symbols(kind))
        elif entry_kind == "system":
            out.extend(_load_system_symbols(kind))
        elif entry_kind == "external":
            out.extend(
                _load_external_symbols(
                    kind, str(entry.get("path") or ""), str(entry.get("name") or "外部样式")
                )
            )
    out.extend(_load_general_symbols(kind))
    return out


def all_groups(kind: str) -> list[str]:
    """符号选择器里会出现的分组（用户符号 → 通用符号 → 其它）。"""
    seen: list[str] = []
    for symbol in _load_all_library_symbols(kind):
        if symbol.group not in seen:
            seen.append(symbol.group)
    priority = ["用户符号", "通用符号"]
    ordered = [name for name in priority if name in seen]
    ordered.extend(
        name for name in sorted(seen, key=str.casefold) if name not in priority
    )
    return ordered


def load_library_symbols(kind: str) -> list[LibrarySymbol]:
    """按「库启用 + 分组勾选」过滤后的符号（用户库排在最前）。"""
    hidden = load_hidden_groups()
    return [
        symbol
        for symbol in _load_all_library_symbols(kind)
        if symbol.group not in hidden
    ]


__all__ = [
    "LibrarySymbol",
    "all_groups",
    "delete_symbol_library_file",
    "delete_user_symbol",
    "libraries_dir",
    "load_hidden_groups",
    "load_libraries",
    "load_library_symbols",
    "save_hidden_groups",
    "save_libraries",
    "save_symbol_library",
    "save_user_symbol",
    "symbols_dir",
]
