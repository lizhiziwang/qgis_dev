"""应用级快捷键登记表与用户自定义的持久化。

系统里所有**全局**快捷键在这里登记一次（:data:`DEFAULT_SHORTCUTS`），
「快捷键」面板按它列出、编辑、恢复默认。

用户改过的项以**覆盖值**的形式写在 ``config/shortcuts.yaml``（只存与默认不同的
项）。这样以后代码里新增快捷键时，老配置文件不会把它锁死在旧默认上；删掉该文件
即全部回到默认。

格式::

    shortcuts:
      undo: Ctrl+Alt+Z
      log_dock: ""        # 空串 = 不绑定

只做"登记表 + 读写文件"，不碰 Qt 控件——把值套到 QAction / QShortcut 上是
主窗口的事（见 ``MainWindow.set_app_shortcut``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from app.infra.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ShortcutSpec:
    """一条快捷键登记项。"""

    #: 稳定标识（配置文件里的键）；**不要**随便改，用户配置按它对应
    key: str
    #: 面板里显示的功能名
    label: str
    #: 默认键序列（``QKeySequence`` 能解析的字符串；空串 = 默认不绑定）
    default: str
    #: 是否可在面板里改。鼠标修饰键这类**单独一个修饰键**不是合法的 QKeySequence
    #: （Qt 要求至少一个真实按键），录不进去，所以只做展示。
    editable: bool = True
    #: 是否为“鼠标 + 修饰键”条目（面板里标注一下，免得跟键盘快捷键混淆）
    mouse: bool = False


#: 全系统快捷键清单。顺序即面板里的展示顺序。
DEFAULT_SHORTCUTS: tuple[ShortcutSpec, ...] = (
    ShortcutSpec("undo", "撤销", "Ctrl+Z"),
    ShortcutSpec("redo", "重做", "Ctrl+Shift+Z"),
    ShortcutSpec("redo_alt", "重做（备用键）", "Ctrl+Y"),
    ShortcutSpec("new_project", "新建工程", "Ctrl+N"),
    ShortcutSpec("save_project", "保存工程", "Ctrl+S"),
    ShortcutSpec("save_project_as", "另存为工程", "Ctrl+Shift+S"),
    ShortcutSpec("open_project", "打开工程", "Ctrl+Shift+O"),
    ShortcutSpec("open_file", "打开文件", "Ctrl+O"),
    ShortcutSpec("exit_app", "退出", "Ctrl+Q"),
    ShortcutSpec("log_dock", "日志面板", "Ctrl+L"),
    ShortcutSpec("flow_editor", "流程节点编辑器", "Ctrl+Shift+F"),
    ShortcutSpec("snapping_toggle", "磁性捕捉开关", "S"),
    ShortcutSpec("tracing_toggle", "追踪", "T"),
    # 鼠标修饰键：点选 / 框选时的选择行为（展示用，不可改——见 editable 的说明）
    ShortcutSpec("select_add", "点选/框选：追加选择", "Ctrl + 点击/拖框", editable=False, mouse=True),
    ShortcutSpec("select_remove", "点选/框选：移除选择", "Shift + 点击/拖框", editable=False, mouse=True),
)

#: 可编辑的条目（面板里给录制框的）
EDITABLE_SHORTCUTS: tuple[ShortcutSpec, ...] = tuple(s for s in DEFAULT_SHORTCUTS if s.editable)

#: ``shortcuts.yaml`` 顶层键
_SECTION = "shortcuts"


def default_map() -> dict[str, str]:
    """默认键序列 ``{key: sequence}``（**仅可编辑项**，鼠标修饰键不参与读写）。"""
    return {spec.key: spec.default for spec in EDITABLE_SHORTCUTS}


def load_overrides(path: Path) -> dict[str, str]:
    """读用户覆盖值；文件缺失 / 损坏 / 键不认识 → 对应项按默认处理（只记日志）。"""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 —— 配置坏掉不该拦启动
        logger.warning("读取快捷键配置失败，全部按默认处理：%s", path, exc_info=True)
        return {}
    section = raw.get(_SECTION) if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return {}
    known = default_map()
    out: dict[str, str] = {}
    for key, value in section.items():
        if key not in known:
            logger.warning("快捷键配置里有未知项，已忽略：%s", key)
            continue
        if value is None:
            out[key] = ""
        elif isinstance(value, str):
            out[key] = value
        else:
            logger.warning("快捷键 %s 的值不是字符串，已忽略：%r", key, value)
    return out


def save_overrides(path: Path, overrides: dict[str, str]) -> None:
    """把覆盖值写回 ``path``（自动建父目录）。

    ``overrides`` 传的是**与默认不同的项**；空字典也要落一个空段（等于恢复默认，
    但文件留着，方便用户再看一眼）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 快捷键自定义（只记录与默认不同的项）",
        "# 删掉本文件、或把某项的值改成默认值，即恢复默认快捷键。",
        "# 值为空串表示该功能不绑定快捷键。",
        f"{_SECTION}:",
    ]
    if not overrides:
        lines.append("  {}")
    else:
        for spec in EDITABLE_SHORTCUTS:
            if spec.key not in overrides:
                continue
            value = overrides[spec.key]
            if value == "":
                lines.append(f'  {spec.key}: ""')
            else:
                # 键序列里可能带引号字符，统一用单引号保留字面值
                lines.append(f"  {spec.key}: '{value}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def effective_sequences(overrides: dict[str, str]) -> dict[str, str]:
    """默认 + 覆盖 → 实际生效的键序列。"""
    merged = default_map()
    merged.update({k: v for k, v in overrides.items() if k in merged})
    return merged


def diff_from_defaults(sequences: dict[str, str]) -> dict[str, str]:
    """只留与默认不同的项（写文件用）。"""
    defaults = default_map()
    return {k: v for k, v in sequences.items() if defaults.get(k) != v}


def find_conflicts(sequences: dict[str, str]) -> list[tuple[str, str, str]]:
    """找出重复绑定：``[(键序列, 功能A, 功能B), ...]``（空串不参与）。"""
    seen: dict[str, str] = {}
    conflicts: list[tuple[str, str, str]] = []
    labels = {spec.key: spec.label for spec in EDITABLE_SHORTCUTS}
    for spec in EDITABLE_SHORTCUTS:
        seq = (sequences.get(spec.key) or "").strip()
        if not seq:
            continue
        if seq in seen:
            conflicts.append((seq, labels.get(seen[seq], seen[seq]), spec.label))
        else:
            seen[seq] = spec.key
    return conflicts


__all__ = [
    "DEFAULT_SHORTCUTS",
    "EDITABLE_SHORTCUTS",
    "ShortcutSpec",
    "default_map",
    "diff_from_defaults",
    "effective_sequences",
    "find_conflicts",
    "load_overrides",
    "save_overrides",
]
