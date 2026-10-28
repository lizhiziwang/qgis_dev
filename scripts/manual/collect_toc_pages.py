"""从已生成的 PDF 中提取手册目录页码。

用法（必须用 QGIS Python，依赖 QtPdf）::

    F:/app/qgis421/bin/python-qgis.bat scripts/manual/collect_toc_pages.py \
        --pdf E:/tmp/manual_pdf/使用手册.pdf

读取 ``docs/manual_toc_entries.json``，输出 ``docs/manual_toc_pages.json``。
构建流程：

1. ``python scripts/manual/build_manual.py``（目录页码显示“—”）
2. 用 WPS/Word 把 docx 转成 PDF
3. 运行本脚本提取页码
4. 再次运行 ``build_manual.py``，目录即带真实页码
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRIES_JSON = ROOT / "docs" / "manual_toc_entries.json"
PAGES_JSON = ROOT / "docs" / "manual_toc_pages.json"


def _compact(text: str) -> str:
    """去掉所有空白，抵消 PDF 换行造成的空格。"""
    return re.sub(r"\s+", "", text)


def main() -> int:
    parser = argparse.ArgumentParser(description="提取手册目录页码")
    parser.add_argument("--pdf", required=True, help="手册 PDF 路径")
    parser.add_argument("--entries", default=str(ENTRIES_JSON))
    parser.add_argument("--out", default=str(PAGES_JSON))
    args = parser.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QGuiApplication
    from PyQt6.QtPdf import QPdfDocument

    app = QGuiApplication.instance() or QGuiApplication([])
    doc = QPdfDocument(None)
    err = doc.load(args.pdf)
    if err != QPdfDocument.Error.None_:
        print(f"无法打开 PDF：{args.pdf}（{err}）")
        return 1

    page_texts = [_compact(doc.getAllText(i).text()) for i in range(doc.pageCount())]
    entries = json.loads(Path(args.entries).read_text(encoding="utf-8"))

    # 正文起点：同时出现第 1 章标题和首段正文的页；目录页也含标题，故用正文短句定位
    body_start = 0
    markers = ("第1章产品概述", "1.1产品简介", "图斑核实平台是一套")
    for i, text in enumerate(page_texts):
        if all(m in text for m in markers):
            body_start = i
            break

    result: dict[str, int] = {}
    missing: list[str] = []
    for item in entries:
        title = item["title"]
        needle = _compact(title)
        page = None
        for i in range(body_start, len(page_texts)):
            if needle in page_texts[i]:
                page = i + 1
                break
        if page is None:
            missing.append(title)
        else:
            result[title] = page

    Path(args.out).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"PDF 页数：{doc.pageCount()}，正文起点：第 {body_start + 1} 页")
    print(f"已写入 {len(result)} 条目录页码：{args.out}")
    if missing:
        print("未匹配到页码：")
        for title in missing:
            print(f"  - {title}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
