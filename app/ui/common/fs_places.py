"""跨平台「我的电脑」位置枚举：盘符、挂载点、快速访问目录。

「添加数据」弹窗左栏要显示“我的电脑 + 下面所有盘符”，并且同一份代码要跑在
Windows 和 Linux（麒麟 / 统信）上：

- **Windows**：``QStorageInfo.mountedVolumes()`` 列出 ``C:\\``、``D:\\``、网络盘、
  可移动盘；只保留 ``isValid() && isReady()``（光驱空仓、断开的网络盘不显示）。
  显示名优先用卷标（如 ``Data (D:)``），没有卷标则 ``本地磁盘 (D:)``。
- **Linux**：同一个 API 拿到的是**挂载点**（``/``、``/home``、``/media/u/disk``…）。
  过滤 ``proc`` / ``sysfs`` / ``tmpfs`` 等伪文件系统，根固定显示“文件系统根目录”，
  其余用卷标或目录名。

“快速访问”（主目录 / 桌面 / 文档 / 下载）走 ``QStandardPaths``，返回空或不存在的
位置自动跳过；Windows 下用户把“桌面”重定向到 D 盘也能正确取到。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from qgis.PyQt.QtCore import QStandardPaths, QStorageInfo

#: Linux 伪文件系统：出现在 mountedVolumes() 里但不是用户会放数据的盘
_PSEUDO_FS = frozenset(
    {
        "autofs",
        "binfmt_misc",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devpts",
        "devtmpfs",
        "efivarfs",
        "fusectl",
        "hugetlbfs",
        "mqueue",
        "nsfs",
        "overlay",
        "proc",
        "pstore",
        "ramfs",
        "rpc_pipefs",
        "securityfs",
        "snapfuse",
        "squashfs",
        "sysfs",
        "tmpfs",
        "tracefs",
    }
)

#: Linux 下不值得列出的系统挂载点前缀（即使驱动不是伪文件系统）
_SYSTEM_MOUNT_PREFIXES = ("/proc", "/sys", "/dev", "/snap", "/boot/efi")

#: 位置类别（UI 据此选图标）
KIND_HOME = "home"
KIND_DESKTOP = "desktop"
KIND_DOCUMENTS = "documents"
KIND_DOWNLOADS = "downloads"
KIND_DRIVE = "drive"


@dataclass(frozen=True)
class Place:
    """左栏里的一个可导航位置。"""

    label: str
    path: str
    kind: str


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _standard(path: str) -> str:
    """``QStandardPaths`` 的返回值 → 真实存在的目录（不存在返回空串）。"""
    if not path:
        return ""
    try:
        return path if os.path.isdir(path) else ""
    except OSError:
        return ""


def quick_places() -> list[Place]:
    """主目录 / 桌面 / 文档 / 下载（按此顺序，去重、跳过不存在的位置）。"""
    spec = QStandardPaths.StandardLocation
    candidates = [
        (KIND_HOME, "主目录", QStandardPaths.writableLocation(spec.HomeLocation)),
        (KIND_DESKTOP, "桌面", QStandardPaths.writableLocation(spec.DesktopLocation)),
        (KIND_DOCUMENTS, "文档", QStandardPaths.writableLocation(spec.DocumentsLocation)),
        (KIND_DOWNLOADS, "下载", QStandardPaths.writableLocation(spec.DownloadLocation)),
    ]
    out: list[Place] = []
    seen: set[str] = set()
    for kind, label, raw in candidates:
        path = _standard(raw)
        if not path:
            continue
        key = _norm(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(Place(label, path, kind))
    return out


def _drive_label(info: QStorageInfo, root: str) -> str:
    """盘符 / 挂载点的显示名。"""
    display = str(info.displayName() or "").strip()
    if os.name == "nt":
        letter = os.path.splitdrive(root)[0]
        if not letter:
            return display or root
        if display and display.casefold() not in {letter.casefold(), root.casefold()}:
            return f"{display} ({letter})"
        return f"本地磁盘 ({letter})"
    if root == os.sep:
        return "文件系统根目录"
    name = display or Path(root).name or root
    return f"{name} ({root})"


def mounted_places() -> list[Place]:
    """「我的电脑」里的盘符 / 挂载点（只含可用卷，按路径排序、去重）。"""
    out: list[Place] = []
    seen: set[str] = set()
    try:
        volumes = QStorageInfo.mountedVolumes()
    except Exception:  # noqa: BLE001 —— 极端情况下 Qt 后端不可用，左栏退化为只有快速访问
        return out
    for info in volumes:
        try:
            if not info.isValid() or not info.isReady():
                continue
            raw_root = str(info.rootPath() or "").strip()
            if not raw_root or not os.path.isdir(raw_root):
                continue
            root = os.path.normpath(raw_root)
            if os.name != "nt":
                fs = str(info.fileSystemType() or "").lower()
                if fs in _PSEUDO_FS:
                    continue
                if root != os.sep and root.startswith(_SYSTEM_MOUNT_PREFIXES):
                    continue
            key = _norm(root)
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(Place(_drive_label(info, root), root, KIND_DRIVE))
    out.sort(key=lambda place: place.path.casefold())
    return out


def place_sections() -> list[tuple[str, list[Place]]]:
    """左栏分组：``[("快速访问", [...]), ("我的电脑", [...])]``。

    与快速访问重复的挂载点（例如 Linux 把 ``/home`` 单独挂一个分区）不会再列一遍。
    """
    quick = quick_places()
    quick_keys = {_norm(place.path) for place in quick}
    drives = [place for place in mounted_places() if _norm(place.path) not in quick_keys]
    sections: list[tuple[str, list[Place]]] = []
    if quick:
        sections.append(("快速访问", quick))
    if drives:
        sections.append(("我的电脑", drives))
    return sections


__all__ = [
    "KIND_DESKTOP",
    "KIND_DOCUMENTS",
    "KIND_DOWNLOADS",
    "KIND_DRIVE",
    "KIND_HOME",
    "Place",
    "mounted_places",
    "place_sections",
    "quick_places",
]
