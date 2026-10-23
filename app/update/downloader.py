"""安装包下载：HTTP Range 断点续传 + SHA-256 校验 + 原子落盘。

服务端 ``GET /api/app/download/{id}`` 支持单段 Range。下载过程：

1. 目标文件已存在且 SHA-256 匹配 → 直接复用（重试 / 重启后不重复下载）；
2. ``<file>.part`` 存在 → 从已有字节数续传（``Range: bytes=<n>-``）；
3. 服务端返回 200（不支持 Range）→ 从头重下，截断旧分片；
4. 边写边算 SHA-256，完成后与清单比对，**通过后才** ``os.replace`` 成正式
   文件名——更新器拿到的路径永远是一个校验过的完整包。

取消（:class:`threading.Event`）只中止当前请求，``.part`` 保留，下次续传。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from app import __version__
from app.infra.logging import get_logger
from app.update.errors import UpdateCancelledError, UpdateDownloadError
from app.update.manifest import UpdatePackage, join_url

logger = get_logger(__name__)

_CHUNK_SIZE = 256 * 1024
_MIN_FREE_MARGIN = 32 * 1024 * 1024
_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$")

ProgressCallback = Callable[[int, int], None]


def download_package(
    *,
    package: UpdatePackage,
    base_url: str,
    dest_dir: Path,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    timeout: float = 30.0,
) -> Path:
    """下载 ``package`` 并返回校验通过的本地路径。

    :param dest_dir: 下载目录（通常 ``update_paths.downloads_dir()``）
    :param progress: ``(已下载字节, 总字节)``；在调用线程里同步执行
    :param cancel: 置位后在下一个分片边界中止并抛 :class:`UpdateCancelledError`
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{package.id}-{_safe_name(package.file_name)}"
    part = target.with_name(target.name + ".part")

    if target.is_file():
        if _sha256_file(target) == package.sha256:
            logger.info("安装包已存在且校验通过，跳过下载：%s", target)
            _emit(progress, package.size_bytes, package.size_bytes)
            return target
        logger.warning("已有安装包校验不符，删除后重下：%s", target)
        target.unlink(missing_ok=True)

    offset = part.stat().st_size if part.is_file() else 0
    if offset > package.size_bytes:
        logger.warning("分片文件大于清单声明大小，重新下载：%s", part)
        part.unlink(missing_ok=True)
        offset = 0
    if offset == package.size_bytes:
        if _sha256_file(part) == package.sha256:
            os.replace(part, target)
            _emit(progress, package.size_bytes, package.size_bytes)
            return target
        part.unlink(missing_ok=True)
        offset = 0

    _check_disk_space(dest_dir, package.size_bytes - offset)
    url = join_url(base_url, package.url)
    logger.info(
        "下载安装包：%s（%s，从 %d 字节续传）",
        url,
        _human_size(package.size_bytes),
        offset,
    )

    hasher = hashlib.sha256()
    if offset:
        _hash_prefix(part, hasher, cancel)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"qgis_dev/{__version__}",
            "Accept": "application/octet-stream",
            **({"Range": f"bytes={offset}-"} if offset else {}),
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            _prepare_response(response, offset, package, part)
            if response.status == 200 and offset:
                # 服务端忽略了 Range：从头写，哈希器也要重置
                offset = 0
                hasher = hashlib.sha256()
            downloaded = offset
            _emit(progress, downloaded, package.size_bytes)
            with part.open("ab" if offset else "wb") as handle:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise UpdateCancelledError("下载已取消")
                    chunk = response.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if downloaded > package.size_bytes:
                        raise UpdateDownloadError("下载数据超过清单声明的大小")
                    _emit(progress, downloaded, package.size_bytes)
    except UpdateCancelledError:
        raise
    except UpdateDownloadError:
        raise
    except urllib.error.HTTPError as exc:
        raise UpdateDownloadError(f"下载安装包失败：HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise UpdateDownloadError(f"下载安装包失败：{exc.reason}") from exc
    except TimeoutError as exc:
        raise UpdateDownloadError("下载安装包超时，可稍后继续（已保留断点）") from exc
    except OSError as exc:
        raise UpdateDownloadError(f"下载安装包失败：{exc}") from exc

    actual_size = part.stat().st_size
    if actual_size != package.size_bytes:
        raise UpdateDownloadError(
            f"安装包大小不符：期望 {package.size_bytes}，实际 {actual_size}"
        )
    actual_sha = hasher.hexdigest()
    if actual_sha != package.sha256:
        # 分片已污染，保留只会让后续每次续传都失败
        part.unlink(missing_ok=True)
        raise UpdateDownloadError("安装包 SHA-256 校验失败，已删除损坏文件")

    os.replace(part, target)
    logger.info("安装包下载完成并通过校验：%s", target)
    _emit(progress, package.size_bytes, package.size_bytes)
    return target


# --------------------------------------------------------------------- 内部


def _safe_name(name: str) -> str:
    """文件名只保留基名（清单解析已校验，这里再兜一层）。"""
    base = Path(name).name
    return base if base and base not in (".", "..") else "update.bin"


def _prepare_response(response, offset: int, package: UpdatePackage, part: Path) -> None:
    """校验 Range 响应头；不支持 Range 时由调用方从头写。"""
    if response.status == 206:
        header = response.headers.get("Content-Range", "")
        match = _CONTENT_RANGE_RE.match(header.strip())
        if not match:
            raise UpdateDownloadError(f"下载响应 Content-Range 不合法：{header!r}")
        start, end = int(match.group(1)), int(match.group(2))
        total = match.group(3)
        if start != offset:
            raise UpdateDownloadError(
                f"续传区间不匹配：请求从 {offset} 开始，服务端返回 {start}"
            )
        if end < start:
            raise UpdateDownloadError("下载响应 Content-Range 区间非法")
        if total != "*" and int(total) != package.size_bytes:
            raise UpdateDownloadError(
                f"服务端包大小与清单不一致：{total} != {package.size_bytes}"
            )
        return
    if response.status == 200:
        return
    raise UpdateDownloadError(f"下载安装包失败：HTTP {response.status}")


def _check_disk_space(dest_dir: Path, needed: int) -> None:
    try:
        free = shutil.disk_usage(dest_dir).free
    except OSError:
        return
    if free < needed + _MIN_FREE_MARGIN:
        raise UpdateDownloadError(
            f"磁盘空间不足：需要约 {_human_size(needed)}，"
            f"可用 {_human_size(free)}"
        )


def _hash_prefix(path: Path, hasher, cancel: threading.Event | None) -> None:
    """把已下载分片喂给哈希器（续传时保证最终 SHA-256 覆盖全文件）。"""
    with path.open("rb") as handle:
        while True:
            if cancel is not None and cancel.is_set():
                raise UpdateCancelledError("下载已取消")
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                return
            hasher.update(chunk)


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _emit(progress: ProgressCallback | None, done: int, total: int) -> None:
    if progress is None:
        return
    try:
        progress(done, total)
    except Exception:
        logger.debug("下载进度回调异常", exc_info=True)


def _human_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


__all__ = ["download_package"]
