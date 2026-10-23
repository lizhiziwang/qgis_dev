"""更新发布工具：打 full 包、分片上传、生成待签清单、发布。

服务端（problem-feedback）的管理接口已经能完成同样的事，但打 Linux full 包
（tar.gz）和 450MB 分片上传在浏览器里不现实，发布机用本脚本更稳。

典型发布流程（**私钥始终留在离线机器**）::

    # 1) 打包（Linux：dist/app → tar.gz；Windows：指向已打好的 Inno 安装包）
    python tools/update_release.py package --dist dist/app --out dist/releases
    python tools/update_release.py package --file dist/installer/qgis_dev_setup_1.0.0.exe \\
        --dist dist/app --out dist/releases

    # 2) 建发布单（默认取 build_info.json 的版本 / channel）
    python tools/update_release.py create --base-url http://host:9003/cs-feedback \\
        --token "$APP_RELEASE_ADMIN_TOKEN" --notes-file notes.md

    # 3) 上传安装包（8MB 分片，服务端校验 SHA-256）
    python tools/update_release.py upload --base-url ... --release-id 1 \\
        --descriptor dist/releases/qgis-dev-app-1.0.0-linux-x86_64.tar.gz.json

    # 4) 生成待签清单（可拷到离线机器）
    python tools/update_release.py prepare --base-url ... --release-id 1 \\
        --out dist/releases/payload-1.0.0-3.json

    # 5) 离线签名（私钥不联网）
    python tools/update_signing.py sign --payload payload-1.0.0-3.json \\
        --private-key .keys/update-private.pem --out signed-1.0.0-3.json

    # 6) 回发布机提交签名并发布
    python tools/update_release.py publish --base-url ... --release-id 1 \\
        --signed signed-1.0.0-3.json

注意：

- 当前没有 HTTPS，管理令牌会以明文经过网络；只在可信内网使用，线上必须加
  VPN / IP 白名单并尽快上 TLS（服务端 ``AdminAuthInterceptor`` 注释同）。
- 令牌优先从 ``--token`` 或 ``APP_RELEASE_ADMIN_TOKEN`` 环境变量读取；命令行
  参数会进 shell 历史，生产上建议用环境变量。
- 本工具**不做签名**——签名只走 :mod:`tools.update_signing`，私钥不进发布机。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import APP_ID, CHANNEL  # noqa: E402

CHUNK_SIZE = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 120.0
#: full 包不携带用户运行期目录（日志 / 临时文件 / 旧备份）
_EXCLUDED_TOP_DIRS = {"logs", ".tmp"}
KIND_FULL = "full"
KIND_CODE = "code"


class ReleaseError(Exception):
    """发布流程可预期的失败（网络 / 服务端拒绝 / 参数错误）。"""


# --------------------------------------------------------------------- 打包


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _read_build_info(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleaseError(f"读不到 build_info.json：{path}：{exc}") from exc
    except ValueError as exc:
        raise ReleaseError(f"build_info.json 不是合法 JSON：{path}：{exc}") from exc
    if not isinstance(data, dict):
        raise ReleaseError(f"build_info.json 根节点必须是对象：{path}")
    return data


def _build_info_for(dist: Path, explicit: Path | None = None) -> dict[str, Any]:
    path = explicit or (dist / "build_info.json")
    info = _read_build_info(path)
    info["_path"] = str(path)
    return info


def _platform_parts(info: dict[str, Any]) -> tuple[str, str]:
    platform = str(info.get("platform") or "")
    os_name, _, arch = platform.partition("-")
    os_name = os_name.lower()
    arch = arch.lower()
    if os_name not in {"windows", "linux"} or not arch:
        raise ReleaseError(
            f"build_info.json 的 platform 无法识别：{platform!r}（期望 windows-x86_64 这类）"
        )
    return os_name, arch


def _descriptor(
    artifact: Path,
    info: dict[str, Any],
    *,
    kind: str,
    os_name: str,
    arch: str,
) -> dict[str, Any]:
    runtime_id = str(info.get("runtimeId") or "").strip()
    if not runtime_id:
        raise ReleaseError("build_info.json 缺少 runtimeId，无法生成安装包描述")
    version = str(info.get("version") or "").strip()
    try:
        version_code = int(info.get("versionCode") or 0)
    except (TypeError, ValueError) as exc:
        raise ReleaseError("build_info.json 的 versionCode 不是整数") from exc
    if not version or version_code <= 0:
        raise ReleaseError("build_info.json 缺少 version / versionCode")
    size = artifact.stat().st_size
    if size <= 0:
        raise ReleaseError(f"安装包为空：{artifact}")
    return {
        "appId": APP_ID,
        "kind": kind,
        "fileName": artifact.name,
        "path": str(artifact.resolve()),
        "sizeBytes": size,
        "sha256": _sha256_file(artifact),
        "version": version,
        "versionCode": version_code,
        "channel": str(info.get("channel") or CHANNEL),
        "runtimeId": runtime_id,
        "os": os_name,
        "arch": arch,
        "buildInfo": info.get("_path", ""),
    }


def _write_descriptor(desc: dict[str, Any], artifact: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / (artifact.name + ".json")
    target.write_text(
        json.dumps(desc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def package_linux(dist: Path, out_dir: Path, *, kind: str) -> tuple[Path, Path]:
    """把 ``dist/app`` 打成更新器可解压的 tar.gz（根目录直接是产物内容）。"""
    dist = dist.resolve()
    if not dist.is_dir():
        raise ReleaseError(f"产物目录不存在：{dist}")
    info = _build_info_for(dist)
    os_name, arch = _platform_parts(info)
    if os_name != "linux":
        raise ReleaseError(f"当前产物 platform={info.get('platform')}，不是 Linux 包")
    version = str(info.get("version") or "").strip()
    if not version:
        raise ReleaseError("build_info.json 缺少 version")
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_path = out_dir / f"qgis-dev-app-{version}-linux-{arch}.tar.gz"
    _write_tar_gz(dist, archive_path)
    desc = _descriptor(archive_path, info, kind=kind, os_name=os_name, arch=arch)
    return archive_path, _write_descriptor(desc, archive_path, out_dir)


def _write_tar_gz(root: Path, target: Path) -> None:
    """确定性 tar.gz：根目录即产物内容，mtime/uid/gid 归零。

    Linux 提权更新器拒绝符号链接成员，遇到就报错——打包机上的
    ``tools/build_linux.sh`` 已经用 ``cp -aL`` 解引用过，正常不会出现。
    """
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        with tmp.open("wb") as raw, gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, mtime=0
        ) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
            for current, dirs, files in os.walk(root, followlinks=False):
                current_path = Path(current)
                rel_dir = current_path.relative_to(root)
                if rel_dir == Path("."):
                    dirs[:] = [d for d in dirs if d not in _EXCLUDED_TOP_DIRS]
                dirs.sort()
                if rel_dir != Path("."):
                    # 显式写入目录条目：空目录（如 plugins/、config/state）也要在
                    # 更新后存在；只依赖文件父目录会丢掉它们。
                    dir_info = tar.gettarinfo(str(current_path), arcname=rel_dir.as_posix())
                    dir_info.uid = 0
                    dir_info.gid = 0
                    dir_info.uname = "root"
                    dir_info.gname = "root"
                    dir_info.mtime = 0
                    tar.addfile(dir_info)
                for name in sorted(files):
                    path = current_path / name
                    if path.is_symlink():
                        raise ReleaseError(f"产物含符号链接，更新器会拒绝：{path}")
                    arcname = (rel_dir / name).as_posix()
                    info = tar.gettarinfo(str(path), arcname=arcname)
                    info.uid = 0
                    info.gid = 0
                    info.uname = "root"
                    info.gname = "root"
                    info.mtime = 0
                    with path.open("rb") as handle:
                        tar.addfile(info, handle)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def package_existing(
    artifact: Path,
    dist: Path,
    out_dir: Path,
    *,
    kind: str,
    os_name: str,
    arch: str,
) -> tuple[Path, Path]:
    """为已打好的安装包（如 Inno Setup 的 exe）生成上传描述。"""
    artifact = artifact.resolve()
    if not artifact.is_file():
        raise ReleaseError(f"安装包不存在：{artifact}")
    info = _build_info_for(dist.resolve())
    out_dir.mkdir(parents=True, exist_ok=True)
    desc = _descriptor(artifact, info, kind=kind, os_name=os_name, arch=arch)
    return artifact, _write_descriptor(desc, artifact, out_dir)


# --------------------------------------------------------------------- HTTP


class _AdminClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        operator: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        base = base_url.strip().rstrip("/")
        if not base:
            raise ReleaseError("缺少 --base-url（例：http://host:9003/cs-feedback）")
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ReleaseError(f"--base-url 不是合法 http(s) 地址：{base}")
        self._base = base
        self._token = token
        self._operator = operator
        self._timeout = timeout

    def _url(self, path: str) -> str:
        return self._base + path

    def _open(self, request: urllib.request.Request) -> bytes:
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            message = self._error_message(body) or exc.reason
            raise ReleaseError(f"HTTP {exc.code}：{message}") from exc
        except urllib.error.URLError as exc:
            raise ReleaseError(f"连接失败：{exc.reason}") from exc
        except OSError as exc:
            raise ReleaseError(f"请求失败：{exc}") from exc

    @staticmethod
    def _error_message(body: bytes) -> str:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return body[:300].decode("utf-8", "replace")
        if isinstance(payload, dict):
            return str(payload.get("message") or payload.get("error") or payload)
        return str(payload)

    def _headers(self, content_type: str) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": content_type}
        if self._token:
            headers["X-Admin-Token"] = self._token
        if self._operator:
            headers["X-Admin-Operator"] = self._operator
        return headers

    def call_json(self, method: str, path: str, payload: Any = None) -> Any:
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._url(path),
            data=data,
            method=method,
            headers=self._headers("application/json; charset=utf-8"),
        )
        body = self._open(request)
        try:
            envelope = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ReleaseError(f"服务端返回不是合法 JSON：{body[:200]!r}") from exc
        if not isinstance(envelope, dict):
            raise ReleaseError("服务端返回结构非法")
        if envelope.get("code") != 0:
            raise ReleaseError(
                f"服务端拒绝：{envelope.get('message') or envelope.get('code')}"
            )
        return envelope.get("data")

    def put_chunk(self, upload_id: str, offset: int, chunk: bytes) -> dict[str, Any]:
        request = urllib.request.Request(
            self._url(
                f"/api/admin/app/uploads/{urllib.parse.quote(upload_id)}"
                f"?offset={offset}"
            ),
            data=chunk,
            method="PUT",
            headers=self._headers("application/octet-stream"),
        )
        body = self._open(request)
        try:
            envelope = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ReleaseError(f"分片响应不是合法 JSON：{body[:200]!r}") from exc
        if not isinstance(envelope, dict) or envelope.get("code") != 0:
            message = envelope.get("message") if isinstance(envelope, dict) else body[:200]
            raise ReleaseError(f"分片上传失败：{message}")
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise ReleaseError("分片上传响应缺少数据")
        return data


def _load_descriptor(path: Path) -> dict[str, Any]:
    try:
        desc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleaseError(f"读不到安装包描述：{path}：{exc}") from exc
    except ValueError as exc:
        raise ReleaseError(f"安装包描述不是合法 JSON：{path}：{exc}") from exc
    if not isinstance(desc, dict):
        raise ReleaseError(f"安装包描述根节点必须是对象：{path}")
    artifact = Path(str(desc.get("path") or ""))
    if not artifact.is_file():
        raise ReleaseError(f"描述里的安装包不存在：{artifact}")
    actual_size = artifact.stat().st_size
    if int(desc.get("sizeBytes") or -1) != actual_size:
        raise ReleaseError(
            f"安装包大小与描述不一致：{actual_size} != {desc.get('sizeBytes')}，请重新打包"
        )
    actual_sha = _sha256_file(artifact)
    if str(desc.get("sha256") or "").lower() != actual_sha:
        raise ReleaseError("安装包 SHA-256 与描述不一致，请重新打包")
    return desc


# --------------------------------------------------------------------- 命令


def cmd_package(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).expanduser()
    dist = Path(args.dist).expanduser()
    if args.file:
        os_name = args.os
        arch = args.arch
        if not os_name or not arch:
            info = _build_info_for(dist.resolve())
            detected_os, detected_arch = _platform_parts(info)
            os_name = os_name or detected_os
            arch = arch or detected_arch
        artifact, desc_path = package_existing(
            Path(args.file).expanduser(),
            dist,
            out_dir,
            kind=args.kind,
            os_name=os_name,
            arch=arch,
        )
    else:
        if args.os and args.os != "linux":
            raise ReleaseError("不指定 --file 时只能打 Linux full 包；Windows 请指向 Inno 安装包")
        artifact, desc_path = package_linux(dist, out_dir, kind=args.kind)
    desc = json.loads(desc_path.read_text(encoding="utf-8"))
    print(f"安装包  : {artifact}")
    print(f"描述文件: {desc_path}")
    print(
        f"  version={desc['version']} versionCode={desc['versionCode']} "
        f"os={desc['os']} arch={desc['arch']} runtimeId={desc['runtimeId']}"
    )
    print(f"  size={desc['sizeBytes']} sha256={desc['sha256']}")
    return 0


def _token(args: argparse.Namespace) -> str:
    return str(args.token or os.environ.get("APP_RELEASE_ADMIN_TOKEN") or "").strip()


def _client(args: argparse.Namespace) -> _AdminClient:
    return _AdminClient(
        args.base_url,
        token=_token(args),
        operator=str(args.operator or ""),
        timeout=float(args.timeout),
    )


def cmd_create(args: argparse.Namespace) -> int:
    info = _build_info_for(Path(args.dist).expanduser().resolve())
    version = args.version or str(info.get("version") or "")
    try:
        version_code = int(args.version_code or info.get("versionCode") or 0)
    except (TypeError, ValueError) as exc:
        raise ReleaseError("versionCode 必须是整数") from exc
    if not version or version_code <= 0:
        raise ReleaseError("缺少 version / versionCode（可用 --version / --version-code 指定）")
    notes = ""
    if args.notes_file:
        notes = Path(args.notes_file).expanduser().read_text(encoding="utf-8")
    elif args.notes:
        notes = args.notes
    payload = {
        "appId": args.app_id,
        "channel": args.channel,
        "version": version,
        "versionCode": version_code,
        "releaseNotes": notes,
        "minVersionCode": args.min_version_code,
        "mandatory": bool(args.mandatory),
    }
    data = _client(args).call_json("POST", "/api/admin/app/releases", payload)
    if not isinstance(data, dict):
        raise ReleaseError("创建发布单响应缺少数据")
    print(
        f"已创建发布单 id={data.get('id')} version={data.get('version')} "
        f"versionCode={data.get('versionCode')} status={data.get('status')} "
        f"channel={data.get('channel')}"
    )
    print(f"下一步：upload --release-id {data.get('id')} --descriptor <安装包.json>")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    desc = _load_descriptor(Path(args.descriptor).expanduser())
    artifact = Path(str(desc["path"]))
    client = _client(args)
    requested = {
        "releaseId": args.release_id,
        "fileName": desc["fileName"],
        "totalSize": int(desc["sizeBytes"]),
        "sha256": str(desc["sha256"]),
        "kind": str(desc.get("kind") or KIND_FULL),
        "runtimeId": str(desc.get("runtimeId") or ""),
        "os": str(desc.get("os") or ""),
        "arch": str(desc.get("arch") or ""),
    }
    session = client.call_json("POST", "/api/admin/app/uploads", requested)
    if not isinstance(session, dict) or not session.get("id"):
        raise ReleaseError("创建上传会话响应缺少 id")
    upload_id = str(session["id"])
    total = int(desc["sizeBytes"])
    sent = int(session.get("receivedBytes") or 0)
    print(f"上传会话 {upload_id}，共 {total} 字节（{_human_size(total)}）")
    with artifact.open("rb") as handle:
        handle.seek(sent)
        while sent < total:
            chunk = handle.read(min(CHUNK_SIZE, total - sent))
            if not chunk:
                raise ReleaseError("读取安装包时提前结束")
            state = client.put_chunk(upload_id, sent, chunk)
            sent = int(state.get("receivedBytes") or (sent + len(chunk)))
            _print_progress(sent, total)
    print()
    completed = client.call_json(
        "POST", f"/api/admin/app/uploads/{urllib.parse.quote(upload_id)}/complete"
    )
    if not isinstance(completed, dict) or completed.get("packageId") is None:
        raise ReleaseError("完成上传响应缺少 packageId")
    print(
        f"上传完成 packageId={completed.get('packageId')} "
        f"fileName={completed.get('fileName')} status={completed.get('status')}"
    )
    print(f"下一步：prepare --release-id {args.release_id} --out payload-<version>.json")
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    data = _client(args).call_json(
        "POST", f"/api/admin/app/releases/{args.release_id}/sign-payload"
    )
    if not isinstance(data, dict) or not data.get("manifest"):
        raise ReleaseError("生成待签清单响应缺少 manifest")
    payload = {
        "releaseId": data.get("releaseId"),
        "appId": data.get("appId"),
        "channel": data.get("channel"),
        "version": data.get("version"),
        "versionCode": data.get("versionCode"),
        "publishSeq": data.get("publishSeq"),
        "keyId": data.get("keyId"),
        "manifest": data.get("manifest"),
        "manifestSha256": data.get("manifestSha256"),
    }
    if args.out:
        out_path = Path(args.out).expanduser()
    else:
        version = payload.get("version") or "unknown"
        seq = payload.get("publishSeq") or "0"
        out_path = Path("dist") / "releases" / f"signed-payload-{version}-{seq}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"待签清单：version={payload['version']} publishSeq={payload['publishSeq']} "
        f"keyId={payload['keyId']} -> {out_path}"
    )
    print("下一步（离线机器）：")
    print(
        "  python tools/update_signing.py sign "
        f"--payload \"{out_path}\" --private-key .keys/update-private.pem "
        f"--out signed-{payload.get('version')}-{payload.get('publishSeq')}.json"
    )
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    signed_path = Path(args.signed).expanduser()
    try:
        signed = json.loads(signed_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleaseError(f"读不到签名文件：{exc}") from exc
    except ValueError as exc:
        raise ReleaseError(f"签名文件不是合法 JSON：{exc}") from exc
    if not isinstance(signed, dict):
        raise ReleaseError("签名文件根节点必须是对象")
    payload = {
        "manifest": signed.get("manifest"),
        "signature": signed.get("signature"),
        "keyId": signed.get("keyId"),
    }
    if not payload["manifest"] or not payload["signature"]:
        raise ReleaseError("签名文件缺少 manifest / signature")
    data = _client(args).call_json(
        "POST", f"/api/admin/app/releases/{args.release_id}/publish", payload
    )
    if not isinstance(data, dict):
        raise ReleaseError("发布响应缺少数据")
    print(
        f"已发布 id={data.get('id')} version={data.get('version')} "
        f"channel={data.get('channel')} publishSeq={data.get('publishSeq')} "
        f"status={data.get('status')}"
    )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    desc = _load_descriptor(Path(args.descriptor).expanduser())
    print(json.dumps(desc, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------- 工具


def _human_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _print_progress(done: int, total: int) -> None:
    percent = int(done * 100 / total) if total else 0
    print(
        f"\r  已上传 {_human_size(done)} / {_human_size(total)}（{percent}%）",
        end="",
        flush=True,
    )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", required=True, help="服务端根地址（含 context path）")
    parser.add_argument("--token", default="", help="管理令牌（建议改用 APP_RELEASE_ADMIN_TOKEN）")
    parser.add_argument("--operator", default="", help="操作人（写审计日志）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="请求超时秒数")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="更新发布工具：打包 / 上传 / 待签清单 / 发布（签名见 update_signing.py）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    package = sub.add_parser("package", help="打 Linux tar.gz 或登记已有安装包")
    package.add_argument("--dist", default=str(_PROJECT_ROOT / "dist" / "app"), help="PyInstaller 产物根")
    package.add_argument("--out", default=str(_PROJECT_ROOT / "dist" / "releases"), help="输出目录")
    package.add_argument("--file", default="", help="已有安装包（Windows exe/msi）；不填则打 Linux tar.gz")
    package.add_argument("--kind", choices=[KIND_FULL, KIND_CODE], default=KIND_FULL)
    package.add_argument("--os", choices=["linux", "windows"], default="", help="配合 --file 使用")
    package.add_argument("--arch", choices=["x86_64", "aarch64", "arm64"], default="")
    package.set_defaults(func=cmd_package)

    create = sub.add_parser("create", help="创建发布单（草稿）")
    _add_common(create)
    create.add_argument("--dist", default=str(_PROJECT_ROOT / "dist" / "app"), help="取版本号的产物根")
    create.add_argument("--app-id", default=APP_ID)
    create.add_argument("--channel", default=CHANNEL, choices=["internal", "stable"])
    create.add_argument("--version", default="")
    create.add_argument("--version-code", type=int, default=0)
    create.add_argument("--notes", default="")
    create.add_argument("--notes-file", default="")
    create.add_argument("--min-version-code", type=int, default=0)
    create.add_argument("--mandatory", action="store_true")
    create.set_defaults(func=cmd_create)

    upload = sub.add_parser("upload", help="分片上传安装包")
    _add_common(upload)
    upload.add_argument("--release-id", type=int, required=True)
    upload.add_argument("--descriptor", required=True, help="package 命令生成的 .json")
    upload.set_defaults(func=cmd_upload)

    prepare = sub.add_parser("prepare", help="生成待签清单并落盘")
    _add_common(prepare)
    prepare.add_argument("--release-id", type=int, required=True)
    prepare.add_argument("--out", default="", help="payload 输出路径")
    prepare.set_defaults(func=cmd_prepare)

    publish = sub.add_parser("publish", help="提交离线签名并发布")
    _add_common(publish)
    publish.add_argument("--release-id", type=int, required=True)
    publish.add_argument("--signed", required=True, help="update_signing.py 的签名输出")
    publish.set_defaults(func=cmd_publish)

    show = sub.add_parser("show", help="查看安装包描述")
    show.add_argument("--descriptor", required=True)
    show.set_defaults(func=cmd_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ReleaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
