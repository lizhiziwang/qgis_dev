"""文本字段的“UTF-8 被当成 Latin-1”乱码修复（导入 GDB 中文别名时常见）。

某些 FileGDB 的中文别名经 QGIS / OGR 读出来是
``'æ\x9d\x83å±\x9eæ\x80§è´¨'`` 这种样子——原始 UTF-8 字节被当成 Latin-1 解了一遍。
按 ``latin-1 → utf-8`` 转回去就还原成「权属性质」；转不过去（本来就不是这种
乱码，比如正常的 ``Café``）就原样返回，不会误伤。
"""

from __future__ import annotations


def repair_mojibake(text: object) -> str:
    """修复 Latin-1 误读的 UTF-8 文本；不是这种乱码时原样返回。"""
    value = str(text or "")
    if not value:
        return value
    try:
        repaired = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    return repaired or value


__all__ = ["repair_mojibake"]
