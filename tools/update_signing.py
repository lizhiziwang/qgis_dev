"""更新清单离线签名工具（发布机专用，绝不进安装包）。

典型流程：

1. 管理页「生成待签清单」下载 ``signed-payload-<version>-<seq>.json``；
2. 在**离线机器**执行::

       python tools/update_signing.py sign \
           --payload signed-payload-1.0.1-7.json \
           --private-key .keys/update-private.pem \
           --out signed-1.0.1-7.json

3. 把 ``signed-*.json`` 的内容粘贴回管理页（或选择文件）→「校验并发布」。

签名对象是 payload 里 ``manifest`` 字段 **base64 解码后的原始字节**，与 Java
服务端保存的字节完全一致；不对 JSON 重新序列化，避免两端空格 / 键序差异。

安全约束：

- 私钥只从本地文件读，工具不打印、不写日志、不落备份；
- 输出文件只含 ``manifest`` / ``signature`` / ``keyId``，不含私钥信息；
- 可用 ``verify`` 在发布前自检（也用内置公钥，顺便确认服务端会认）。

用法::

    python tools/update_signing.py key-info --private-key .keys/update-private.pem
    python tools/update_signing.py sign --payload p.json --private-key k.pem --out s.json
    python tools/update_signing.py verify --signed s.json --public-key pub.pem
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.update import keys  # noqa: E402
from app.update.rsa import KeyFormatError, RsaPrivateKey, RsaPublicKey  # noqa: E402


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"读取失败：{path}：{exc}") from exc
    except ValueError as exc:
        raise SystemExit(f"不是合法 JSON：{path}：{exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"JSON 根节点必须是对象：{path}")
    return data


def _b64decode(value, name: str) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"缺少字段：{name}")
    try:
        return base64.b64decode(value.strip(), validate=True)
    except ValueError as exc:
        raise SystemExit(f"{name} 不是合法 base64：{exc}") from exc


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def cmd_key_info(args: argparse.Namespace) -> int:
    private_path = Path(args.private_key).expanduser()
    try:
        private_key = RsaPrivateKey.from_pem(private_path.read_text(encoding="ascii"))
    except OSError as exc:
        raise SystemExit(f"读取私钥失败：{exc}") from exc
    except (KeyFormatError, UnicodeDecodeError) as exc:
        raise SystemExit(f"私钥格式不合法：{exc}") from exc
    key_id = private_key.key_id
    print(f"keyId    : {key_id}")
    print(f"bits     : {private_key.n.bit_length()}")
    print(f"expected : {keys.KEY_ID}")
    if key_id != keys.KEY_ID:
        print("警告：与服务端 / 客户端内置公钥不一致，发布会被拒绝", file=sys.stderr)
        return 2
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    payload_path = Path(args.payload).expanduser()
    private_path = Path(args.private_key).expanduser()
    out_path = Path(args.out).expanduser()
    payload = _read_json(payload_path)
    manifest = _b64decode(payload.get("manifest"), "manifest")
    expected_sha = str(payload.get("manifestSha256") or "").strip().lower()
    if expected_sha and hashlib.sha256(manifest).hexdigest() != expected_sha:
        raise SystemExit("payload 内 manifest 与 manifestSha256 不一致，文件可能损坏")
    payload_key_id = str(payload.get("keyId") or "").strip().lower()
    if payload_key_id and payload_key_id != keys.KEY_ID:
        raise SystemExit(
            f"服务端期望密钥 {payload_key_id}，与客户端内置 {keys.KEY_ID} 不一致"
        )
    try:
        private_key = RsaPrivateKey.from_pem(private_path.read_text(encoding="ascii"))
    except OSError as exc:
        raise SystemExit(f"读取私钥失败：{exc}") from exc
    except (KeyFormatError, UnicodeDecodeError) as exc:
        raise SystemExit(f"私钥格式不合法：{exc}") from exc
    signature = private_key.sign_sha256(manifest)
    signed = {
        "manifest": payload["manifest"],
        "signature": _b64encode(signature),
        "keyId": keys.KEY_ID,
    }
    out_path.write_text(json.dumps(signed, ensure_ascii=False, indent=2), encoding="utf-8")
    version = payload.get("version") or "?"
    seq = payload.get("publishSeq") or "?"
    print(f"已签名 version={version} publishSeq={seq} -> {out_path}")
    print(f"keyId={keys.KEY_ID} signature={len(signature)} 字节")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    signed_path = Path(args.signed).expanduser()
    signed = _read_json(signed_path)
    manifest = _b64decode(signed.get("manifest"), "manifest")
    signature = _b64decode(signed.get("signature"), "signature")
    key_id = str(signed.get("keyId") or "").strip().lower()
    if args.public_key:
        pem = Path(args.public_key).expanduser().read_text(encoding="ascii")
    else:
        pem = keys.public_key_for(key_id) or ""
    if not pem:
        raise SystemExit(f"未知 keyId：{key_id or '(空)'}")
    try:
        public_key = RsaPublicKey.from_pem(pem)
    except KeyFormatError as exc:
        raise SystemExit(f"公钥格式不合法：{exc}") from exc
    if key_id and key_id != public_key.key_id:
        raise SystemExit(
            f"keyId 不匹配：文件声明 {key_id}，公钥实际 {public_key.key_id}"
        )
    if not public_key.verify_sha256(manifest, signature):
        raise SystemExit("签名校验失败")
    try:
        data = json.loads(manifest.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SystemExit(f"清单不是合法 UTF-8 JSON：{exc}") from exc
    print(
        "签名校验通过："
        f"version={data.get('version')} versionCode={data.get('versionCode')} "
        f"publishSeq={data.get('publishSeq')} keyId={public_key.key_id}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="更新清单离线签名 / 验签（RSA PKCS#1 v1.5 + SHA-256）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    info = sub.add_parser("key-info", help="显示私钥对应的 keyId / 位数")
    info.add_argument("--private-key", required=True, help="PKCS#8/PKCS#1 PEM 私钥路径")
    info.set_defaults(func=cmd_key_info)

    sign = sub.add_parser("sign", help="对待签清单 payload 签名")
    sign.add_argument("--payload", required=True, help="管理页下载的待签清单 JSON")
    sign.add_argument("--private-key", required=True, help="离线私钥 PEM 路径")
    sign.add_argument("--out", required=True, help="输出 signed JSON 路径")
    sign.set_defaults(func=cmd_sign)

    verify = sub.add_parser("verify", help="校验 signed JSON 的签名")
    verify.add_argument("--signed", required=True, help="signed JSON 路径")
    verify.add_argument("--public-key", help="公钥 PEM（缺省用客户端内置公钥）")
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
