"""RSA PKCS#1 v1.5 + SHA-256 签名 / 验签（纯标准库）。

为什么不依赖 ``cryptography``：客户端是 PyInstaller 单目录包，QGIS 运行时已经
带了大量原生库；为了一个“验一个签名”再加一个原生依赖，体积、ABI、打包三处
都要多担一份风险。这里的实现只覆盖更新清单需要的两种操作：

- 客户端：解析 PEM 公钥 → :meth:`RsaPublicKey.verify_sha256`
- 发布机：解析 PEM 私钥 → :meth:`RsaPrivateKey.sign_sha256`

算法与 Java 端 ``Signature.getInstance("SHA256withRSA")`` 完全一致：对**原始
字节**做 SHA-256，再按 EMSA-PKCS1-v1_5 填充后做模幂。注意签名对象永远是“清单
原文的 UTF-8 字节”，不是重新序列化后的 JSON——这是 Java 与 Python 能对上的
前提。

安全约束：

- 只接受 PEM 的 ``PUBLIC KEY``（SPKI）/ ``RSA PUBLIC KEY``（PKCS#1）与
  ``PRIVATE KEY``（PKCS#8）/ ``RSA PRIVATE KEY``（PKCS#1）；
- 公钥最短 2048 位（当前发布密钥 3072 位）；短于 2048 位直接拒绝加载；
- 验签失败一律返回 ``False``，不抛异常——调用方才能把“签名不对”统一处理成
  拒绝更新，而不是把异常类型差异漏成放行。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass

#: SHA-256 的 DigestInfo 前缀（RFC 8017 附录 A.2.4）
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")

_PEM_RE = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 ]+)-----(?P<body>.*?)-----END (?P=label)-----",
    re.DOTALL,
)

_MIN_KEY_BITS = 2048


class KeyFormatError(ValueError):
    """PEM / DER 结构不合法。"""


# --------------------------------------------------------------------- PEM


def _pem_blocks(pem: str) -> list[tuple[str, bytes]]:
    """解析 PEM 中所有块，返回 ``(label, der)``；畸形块抛 :class:`KeyFormatError`。"""
    if not isinstance(pem, str):
        raise KeyFormatError("PEM 必须是文本")
    blocks: list[tuple[str, bytes]] = []
    for match in _PEM_RE.finditer(pem):
        label = match.group("label").strip()
        body = "".join(match.group("body").split())
        try:
            der = base64.b64decode(body, validate=True)
        except ValueError as exc:  # binascii.Error 是 ValueError 的子类
            raise KeyFormatError(f"PEM 内容不是合法 base64：{label}") from exc
        blocks.append((label, der))
    if not blocks:
        raise KeyFormatError("不是 PEM 格式")
    return blocks


def _pem_first(pem: str, labels: tuple[str, ...]) -> tuple[str, bytes]:
    for label, der in _pem_blocks(pem):
        if label in labels:
            return label, der
    raise KeyFormatError("PEM 中找不到支持的块：" + " / ".join(labels))


# --------------------------------------------------------------------- DER


def _der_tlv(data: bytes, pos: int = 0) -> tuple[int, bytes, int]:
    """读一个 DER TLV，返回 ``(tag, value, next_pos)``。"""
    if pos < 0 or pos + 2 > len(data):
        raise KeyFormatError("DER 数据被截断")
    tag = data[pos]
    first = data[pos + 1]
    pos += 2
    if first & 0x80:
        count = first & 0x7F
        if count == 0 or count > 4 or pos + count > len(data):
            raise KeyFormatError("DER 长度字段不合法")
        length = int.from_bytes(data[pos : pos + count], "big")
        pos += count
    else:
        length = first
    end = pos + length
    if end > len(data):
        raise KeyFormatError("DER 数据被截断")
    return tag, data[pos:end], end


def _der_children(value: bytes) -> list[tuple[int, bytes]]:
    """把 SEQUENCE/SET 的内容拆成子 TLV 列表。"""
    children: list[tuple[int, bytes]] = []
    pos = 0
    while pos < len(value):
        tag, child, pos = _der_tlv(value, pos)
        children.append((tag, child))
    return children


def _der_sequence(value: bytes) -> list[tuple[int, bytes]]:
    tag, content, end = _der_tlv(value, 0)
    if tag != 0x30 or end != len(value):
        raise KeyFormatError("期望 DER SEQUENCE")
    return _der_children(content)


def _der_int(item: tuple[int, bytes], name: str) -> int:
    tag, value = item
    if tag != 0x02 or not value:
        raise KeyFormatError(f"DER INTEGER 缺失：{name}")
    return int.from_bytes(value, "big", signed=False)


def _spki_der(modulus: int, exponent: int) -> bytes:
    """把 RSA 公钥编码成 SPKI DER（用于计算 keyId，不参与验签）。"""
    rsa_public_key = _der_encode_sequence(
        _der_encode_int(modulus) + _der_encode_int(exponent)
    )
    algorithm = _der_encode_sequence(
        bytes.fromhex("06092a864886f70d010101")  # OID 1.2.840.113549.1.1.1
        + b"\x05\x00"                          # NULL
    )
    bit_string = b"\x03" + _der_length(len(rsa_public_key) + 1) + b"\x00" + rsa_public_key
    return _der_encode_sequence(algorithm + bit_string)


def _der_encode_int(value: int) -> bytes:
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return b"\x02" + _der_length(len(raw)) + raw


def _der_encode_sequence(content: bytes) -> bytes:
    return b"\x30" + _der_length(len(content)) + content


def _der_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


# --------------------------------------------------------------------- 公钥


@dataclass(frozen=True)
class RsaPublicKey:
    """RSA 公钥（模数 / 指数 / 原始 SPKI DER）。"""

    n: int
    e: int
    spki_der: bytes

    @property
    def size_bytes(self) -> int:
        """模长（字节），也是签名长度。"""
        return (self.n.bit_length() + 7) // 8

    @property
    def key_id(self) -> str:
        """与 Java ``AppSigningService.keyIdOf`` 一致：SPKI DER 的 SHA-256 前 16 位。"""
        return hashlib.sha256(self.spki_der).hexdigest()[:16]

    @classmethod
    def from_pem(cls, pem: str) -> RsaPublicKey:
        label, der = _pem_first(pem, ("PUBLIC KEY", "RSA PUBLIC KEY"))
        if label == "PUBLIC KEY":
            children = _der_sequence(der)
            if len(children) < 2 or children[1][0] != 0x03:
                raise KeyFormatError("SPKI 结构不合法")
            bit_string = children[1][1]
            if not bit_string or bit_string[0] != 0:
                raise KeyFormatError("SPKI BIT STRING 不合法")
            rsa_der = bit_string[1:]
        else:
            rsa_der = der
            der = _spki_der_from_pkcs1(rsa_der)
        parts = _der_sequence(rsa_der)
        if len(parts) != 2:
            raise KeyFormatError("RSAPublicKey 结构不合法")
        n = _der_int(parts[0], "n")
        e = _der_int(parts[1], "e")
        if n.bit_length() < _MIN_KEY_BITS:
            raise KeyFormatError(f"RSA 公钥过短（{n.bit_length()} 位，至少 {_MIN_KEY_BITS} 位）")
        if e < 3 or e % 2 == 0:
            raise KeyFormatError("RSA 公钥指数不合法")
        return cls(n=n, e=e, spki_der=der)

    def verify_sha256(self, payload: bytes, signature: bytes) -> bool:
        """校验 ``SHA256withRSA`` 签名；任何异常都按失败处理。"""
        try:
            k = self.size_bytes
            if not signature or len(signature) != k:
                return False
            s = int.from_bytes(signature, "big")
            if s >= self.n:
                return False
            encoded = pow(s, self.e, self.n).to_bytes(k, "big")
            return hmac.compare_digest(encoded, _emsa_pkcs1_v15_sha256(payload, k))
        except (ValueError, OverflowError):
            return False


def _spki_der_from_pkcs1(rsa_der: bytes) -> bytes:
    """PKCS#1 RSAPublicKey DER → SPKI DER。"""
    parts = _der_sequence(rsa_der)
    if len(parts) != 2:
        raise KeyFormatError("RSAPublicKey 结构不合法")
    n = _der_int(parts[0], "n")
    e = _der_int(parts[1], "e")
    return _spki_der(n, e)


# --------------------------------------------------------------------- 私钥


@dataclass(frozen=True)
class RsaPrivateKey:
    """RSA 私钥（发布机离线使用，客户端不加载）。"""

    n: int
    e: int
    d: int

    @property
    def size_bytes(self) -> int:
        return (self.n.bit_length() + 7) // 8

    @property
    def key_id(self) -> str:
        """由 ``n``/``e`` 重建 SPKI 后算 keyId（发布工具自检用）。"""
        return hashlib.sha256(_spki_der(self.n, self.e)).hexdigest()[:16]

    @classmethod
    def from_pem(cls, pem: str) -> RsaPrivateKey:
        label, der = _pem_first(pem, ("PRIVATE KEY", "RSA PRIVATE KEY"))
        if label == "PRIVATE KEY":
            children = _der_sequence(der)
            if len(children) < 3 or children[2][0] != 0x04:
                raise KeyFormatError("PKCS#8 结构不合法")
            der = children[2][1]
        parts = _der_sequence(der)
        if len(parts) < 4:
            raise KeyFormatError("RSAPrivateKey 结构不合法")
        n = _der_int(parts[1], "n")
        e = _der_int(parts[2], "e")
        d = _der_int(parts[3], "d")
        if n.bit_length() < _MIN_KEY_BITS:
            raise KeyFormatError(f"RSA 私钥过短（{n.bit_length()} 位，至少 {_MIN_KEY_BITS} 位）")
        if d <= 1 or d >= n:
            raise KeyFormatError("RSA 私钥指数不合法")
        return cls(n=n, e=e, d=d)

    @classmethod
    def from_pem_bytes(cls, pem: bytes) -> RsaPrivateKey:
        return cls.from_pem(pem.decode("ascii"))

    def sign_sha256(self, payload: bytes) -> bytes:
        """生成 ``SHA256withRSA`` 签名（PKCS#1 v1.5）。"""
        k = self.size_bytes
        encoded = _emsa_pkcs1_v15_sha256(payload, k)
        signature = pow(int.from_bytes(encoded, "big"), self.d, self.n)
        return signature.to_bytes(k, "big")


def _emsa_pkcs1_v15_sha256(payload: bytes, k: int) -> bytes:
    """EMSA-PKCS1-v1_5 编码：``00 01 FF..FF 00 DigestInfo(SHA-256(msg))``。"""
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(payload).digest()
    padding = k - len(digest_info) - 3
    if padding < 8:
        raise KeyFormatError("RSA 模长不足以容纳 SHA-256 签名")
    return b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info


__all__ = [
    "KeyFormatError",
    "RsaPrivateKey",
    "RsaPublicKey",
]
