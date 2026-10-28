"""生成《图斑核实平台使用手册》（Word .docx）。

运行（系统 Python 即可，需 python-docx）::

    python scripts/manual/build_manual.py

输出：``docs/使用手册.docx``（帮助 → 使用手册 默认打开的文件）。

手册中的界面截图来自 ``docs/manual_screenshots/``，由
``scripts/manual/capture_screenshots.py`` 采集，少量补充图来自
``docs/qa_report/screenshots/``。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import (WD_ALIGN_PARAGRAPH, WD_LINE_SPACING,
                            WD_TAB_ALIGNMENT, WD_TAB_LEADER)
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[2]
IMG = ROOT / "docs" / "manual_screenshots"
OUT = ROOT / "docs" / "使用手册.docx"
ENTRIES_JSON = ROOT / "docs" / "manual_toc_entries.json"
PAGES_JSON = ROOT / "docs" / "manual_toc_pages.json"

APP_NAME = "图斑核实平台"
APP_VERSION = "1.0.9"
DOC_VERSION = "V1.0"
DOC_DATE = "2026 年 10 月"

FONT = "微软雅黑"
MONO = "Consolas"

BLUE = RGBColor(0x1F, 0x4E, 0x79)
DARK = RGBColor(0x26, 0x26, 0x26)
GRAY = RGBColor(0x59, 0x59, 0x59)
ACCENT = RGBColor(0x2E, 0x74, 0xB5)

MISSING_IMAGES: list[str] = []


# ---------------------------------------------------------------------------
# 基础排版工具
# ---------------------------------------------------------------------------


def _set_run_font(run, name: str = FONT, size: float | None = None,
                  bold: bool | None = None, color: RGBColor | None = None) -> None:
    run.font.name = name
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:ascii"), name)
    rfonts.set(qn("w:hAnsi"), name)
    rfonts.set(qn("w:eastAsia"), name)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color


def _set_style_font(style, name: str = FONT, size: float | None = None,
                    bold: bool | None = None, color: RGBColor | None = None) -> None:
    style.font.name = name
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:ascii"), name)
    rfonts.set(qn("w:hAnsi"), name)
    rfonts.set(qn("w:eastAsia"), name)
    if size is not None:
        style.font.size = Pt(size)
    if bold is not None:
        style.font.bold = bold
    if color is not None:
        style.font.color.rgb = color


def _shade(cell, fill: str) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tcpr.append(shd)


def _table_borders(table, color: str = "BFBFBF", sz: int = 4) -> None:
    tbl = table._tbl
    tblpr = tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(sz))
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), color)
        borders.append(el)
    tblpr.append(borders)


def _cell_margins(table, top: int = 40, bottom: int = 40,
                  left: int = 80, right: int = 80) -> None:
    tblpr = table._tbl.tblPr
    mar = OxmlElement("w:tblCellMar")
    for edge, value in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:w"), str(value))
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    tblpr.append(mar)


def _repeat_header(row) -> None:
    trpr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:tblHeader")
    el.set(qn("w:val"), "true")
    trpr.append(el)


def _add_field(paragraph, instr: str, placeholder: str = "") -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    run._r.append(begin)
    run = paragraph.add_run()
    it = OxmlElement("w:instrText")
    it.set(qn("xml:space"), "preserve")
    it.text = instr
    run._r.append(it)
    run = paragraph.add_run()
    sep = OxmlElement("w:fldChar")
    sep.set(qn("w:fldCharType"), "separate")
    run._r.append(sep)
    if placeholder:
        run = paragraph.add_run(placeholder)
        _set_run_font(run, size=9, color=GRAY)
    run = paragraph.add_run()
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(end)


_RICH_RE = re.compile(r"(\*\*.+?\*\*|`.+?`)")


def _add_rich(paragraph, text: str, size: float | None = None,
              color: RGBColor | None = None) -> None:
    """写入文本，支持 **加粗** 与 `等宽` 两种行内标记。"""
    for part in _RICH_RE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            _set_run_font(run, size=size, bold=True, color=color)
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            _set_run_font(run, name=MONO, size=(size or 10.5) - 1, color=RGBColor(0xA3, 0x15, 0x15))
        else:
            run = paragraph.add_run(part)
            _set_run_font(run, size=size, color=color)


class ManualBuilder:
    def __init__(self, toc_pages: dict[str, int] | None = None) -> None:
        self.toc_pages = toc_pages or {}
        self.toc_entries: list[tuple[int, str]] = []
        self.doc = Document()
        self.chapter_label = ""
        self.fig_no = 0
        self.table_no = 0
        self.fig_total = 0
        self.table_total = 0
        self._new_document()

    def _new_document(self) -> None:
        """重置文档与计数器（目录两遍渲染时复用）。"""
        self.doc = Document()
        self.chapter_label = ""
        self.fig_no = 0
        self.table_no = 0
        self.fig_total = 0
        self.table_total = 0
        self._setup_page()
        self._setup_styles()
        self._setup_header_footer()

    # ------------------------------------------------------------ 页面 / 样式

    def _setup_page(self) -> None:
        sec = self.doc.sections[0]
        sec.page_width = Cm(21.0)
        sec.page_height = Cm(29.7)
        sec.top_margin = Cm(2.2)
        sec.bottom_margin = Cm(2.0)
        sec.left_margin = Cm(2.2)
        sec.right_margin = Cm(2.2)
        sec.header_distance = Cm(1.2)
        sec.footer_distance = Cm(1.2)
        sec.different_first_page_header_footer = True

    def _setup_styles(self) -> None:
        normal = self.doc.styles["Normal"]
        _set_style_font(normal, FONT, 10.5, color=DARK)
        normal.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
        normal.paragraph_format.space_after = Pt(3)

        specs = {
            "Heading 1": (18, BLUE, 18, 10),
            "Heading 2": (14, BLUE, 14, 6),
            "Heading 3": (12, ACCENT, 10, 4),
            "Heading 4": (10.5, DARK, 8, 3),
        }
        for name, (size, color, before, after) in specs.items():
            st = self.doc.styles[name]
            _set_style_font(st, FONT, size, bold=True, color=color)
            st.paragraph_format.space_before = Pt(before)
            st.paragraph_format.space_after = Pt(after)
            st.paragraph_format.keep_with_next = True
            st.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE

        for name, size in (("List Bullet", 10.5), ("List Number", 10.5)):
            st = self.doc.styles[name]
            _set_style_font(st, FONT, size, color=DARK)
            st.paragraph_format.space_after = Pt(2)
            st.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE

        cap = self.doc.styles["Caption"]
        _set_style_font(cap, FONT, 9, bold=False, color=GRAY)
        cap.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap.paragraph_format.space_before = Pt(3)
        cap.paragraph_format.space_after = Pt(10)
        cap.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE

        # 文档设置：打开时提示更新域，便于生成目录与页码
        settings = self.doc.settings.element
        update = OxmlElement("w:updateFields")
        update.set(qn("w:val"), "true")
        settings.append(update)

    def _setup_header_footer(self) -> None:
        sec = self.doc.sections[0]
        hp = sec.header.paragraphs[0]
        hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        run = hp.add_run(f"{APP_NAME} 使用手册")
        _set_run_font(run, size=8.5, color=GRAY)
        # 页眉下边线
        ppr = hp._p.get_or_add_pPr()
        pbdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "4")
        bottom.set(qn("w:space"), "1")
        bottom.set(qn("w:color"), "BFBFBF")
        pbdr.append(bottom)
        ppr.append(pbdr)

        fp = sec.footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = fp.add_run("第 ")
        _set_run_font(run, size=9, color=GRAY)
        _add_field(fp, "PAGE", "1")
        run = fp.add_run(" 页 / 共 ")
        _set_run_font(run, size=9, color=GRAY)
        _add_field(fp, "NUMPAGES", "1")
        run = fp.add_run(" 页")
        _set_run_font(run, size=9, color=GRAY)

    # ------------------------------------------------------------ 文本块

    def p(self, text: str = "", *, size: float | None = None, bold: bool = False,
          color: RGBColor | None = None, align=None, indent: float | None = None,
          space_after: float | None = None, keep_with_next: bool = False) -> None:
        para = self.doc.add_paragraph()
        if align is not None:
            para.alignment = align
        if indent is not None:
            para.paragraph_format.first_line_indent = Cm(indent)
        if space_after is not None:
            para.paragraph_format.space_after = Pt(space_after)
        if keep_with_next:
            para.paragraph_format.keep_with_next = True
        if text:
            if bold:
                run = para.add_run(text)
                _set_run_font(run, size=size, bold=True, color=color)
            else:
                _add_rich(para, text, size=size, color=color)

    def bullet(self, text: str) -> None:
        para = self.doc.add_paragraph(style="List Bullet")
        _add_rich(para, text)

    def step(self, text: str) -> None:
        para = self.doc.add_paragraph()
        para.paragraph_format.left_indent = Cm(0.6)
        para.paragraph_format.first_line_indent = Cm(-0.6)
        para.paragraph_format.space_after = Pt(3)
        run = para.add_run("▶ ")
        _set_run_font(run, size=9, color=ACCENT)
        _add_rich(para, text)

    def chapter(self, label: str, title: str) -> None:
        self.chapter_label = label
        self.fig_no = 0
        self.table_no = 0
        para = self.doc.add_paragraph(style="Heading 1")
        para.paragraph_format.page_break_before = True
        text = f"第 {label} 章  {title}" if label.isdigit() else f"附录 {label}  {title}"
        self.toc_entries.append((1, " ".join(text.split())))
        run = para.add_run(text)
        _set_run_font(run, size=18, bold=True, color=BLUE)
        # 章标题下加一条粗横线
        ppr = para._p.get_or_add_pPr()
        pbdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "12")
        bottom.set(qn("w:space"), "4")
        bottom.set(qn("w:color"), "1F4E79")
        pbdr.append(bottom)
        ppr.append(pbdr)

    def h2(self, text: str) -> None:
        self.toc_entries.append((2, " ".join(text.split())))
        para = self.doc.add_paragraph(style="Heading 2")
        run = para.add_run(text)
        _set_run_font(run, size=14, bold=True, color=BLUE)

    def h3(self, text: str) -> None:
        para = self.doc.add_paragraph(style="Heading 3")
        run = para.add_run(text)
        _set_run_font(run, size=12, bold=True, color=ACCENT)

    def h4(self, text: str) -> None:
        para = self.doc.add_paragraph(style="Heading 4")
        run = para.add_run(text)
        _set_run_font(run, size=10.5, bold=True, color=DARK)

    def note(self, text: str, label: str = "提示") -> None:
        table = self.doc.add_table(rows=1, cols=1)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = True
        _table_borders(table, color="D9E2F3", sz=4)
        _cell_margins(table, top=70, bottom=70, left=120, right=120)
        cell = table.cell(0, 0)
        _shade(cell, "EAF1FB")
        para = cell.paragraphs[0]
        para.paragraph_format.space_after = Pt(0)
        para.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
        run = para.add_run(f"【{label}】")
        _set_run_font(run, size=9.5, bold=True, color=BLUE)
        _add_rich(para, text, size=9.5)
        self.p("", space_after=2)

    def table(self, headers: list[str], rows: list[list[str]], *,
              caption: str = "", widths: list[float] | None = None,
              font_size: float = 9, first_col_bold: bool = False) -> None:
        if caption:
            self.table_no += 1
            self.table_total += 1
            cap = self.doc.add_paragraph(style="Caption")
            cap.paragraph_format.keep_with_next = True
            cap.paragraph_format.space_before = Pt(6)
            cap.paragraph_format.space_after = Pt(3)
            run = cap.add_run(f"表 {self.chapter_label}-{self.table_no}  {caption}")
            _set_run_font(run, size=9, color=GRAY)

        table = self.doc.add_table(rows=1, cols=len(headers))
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = widths is None
        _table_borders(table)
        _cell_margins(table)
        if widths:
            for idx, width in enumerate(widths):
                for cell in table.columns[idx].cells:
                    cell.width = Cm(width)

        hdr = table.rows[0]
        _repeat_header(hdr)
        for i, text in enumerate(headers):
            cell = hdr.cells[i]
            _shade(cell, "D9E2F3")
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            para = cell.paragraphs[0]
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            para.paragraph_format.space_after = Pt(0)
            para.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
            run = para.add_run(text)
            _set_run_font(run, size=font_size, bold=True, color=BLUE)

        for row_data in rows:
            row = table.add_row()
            for i, text in enumerate(row_data):
                cell = row.cells[i]
                cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
                para = cell.paragraphs[0]
                para.paragraph_format.space_after = Pt(0)
                para.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
                bold = first_col_bold and i == 0
                _add_rich(para, text, size=font_size)
                if bold:
                    for run in para.runs:
                        run.font.bold = True
        self.p("", space_after=2)

    def fig(self, filename: str, caption: str, width_cm: float = 14.2) -> None:
        path = IMG / filename
        para = self.doc.add_paragraph()
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        para.paragraph_format.space_before = Pt(6)
        para.paragraph_format.space_after = Pt(2)
        para.paragraph_format.keep_with_next = True
        if not path.is_file():
            MISSING_IMAGES.append(filename)
            run = para.add_run(f"[缺少截图：{filename}]")
            _set_run_font(run, size=10, color=RGBColor(0xC0, 0x00, 0x00))
        else:
            run = para.add_run()
            run.add_picture(str(path), width=Cm(width_cm))
        self.fig_no += 1
        self.fig_total += 1
        cap = self.doc.add_paragraph(style="Caption")
        run = cap.add_run(f"图 {self.chapter_label}-{self.fig_no}  {caption}")
        _set_run_font(run, size=9, color=GRAY)

    # ------------------------------------------------------------ 封面 / 前言 / 目录

    def cover(self) -> None:
        self.p("", space_after=0)
        for _ in range(4):
            self.doc.add_paragraph()
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(APP_NAME)
        _set_run_font(run, size=40, bold=True, color=BLUE)

        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(10)
        run = p.add_run("使 用 手 册")
        _set_run_font(run, size=28, bold=True, color=DARK)

        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(24)
        run = p.add_run("User Manual")
        _set_run_font(run, name="Segoe UI", size=14, color=GRAY)

        for _ in range(6):
            self.doc.add_paragraph()

        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(f"适用版本：{APP_VERSION}")
        _set_run_font(run, size=12, color=DARK)

        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(f"文档版本：{DOC_VERSION}")
        _set_run_font(run, size=12, color=DARK)

        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(6)
        run = p.add_run(DOC_DATE)
        _set_run_font(run, size=12, color=DARK)

        self.doc.add_page_break()

    def foreword(self) -> None:
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("前  言")
        _set_run_font(run, size=20, bold=True, color=BLUE)
        self.p("")

        self.p(
            f"《{APP_NAME}使用手册》面向平台的一线作业人员、数据管理人员和系统管理员，"
            "系统介绍软件的界面布局、工程与数据管理、地图浏览查询、图层编辑、质量检查、"
            "空间分析、瓦片制作和流程编排等常用功能。"
        )
        self.p(
            "手册按“先上手、再深入”的顺序组织：第 1～2 章介绍产品定位与快速入门；"
            "第 3 章说明界面组成；第 4～10 章按业务场景展开；第 11～12 章及附录提供"
            "个性化设置、常见问题与速查资料。初次使用建议先阅读第 2 章，再按实际工作"
            "查阅相应章节。"
        )
        self.p(
            "手册中的截图取自当前版本的实际界面，不同分辨率、主题或数据状态下，"
            "按钮位置和显示内容可能略有差异，请以软件实际界面为准。"
        )

        self.p("")
        p = self.doc.add_paragraph()
        run = p.add_run("文档信息")
        _set_run_font(run, size=12, bold=True, color=BLUE)

        self.table(
            ["项目", "内容"],
            [
                ["文档名称", f"{APP_NAME}使用手册"],
                ["适用软件版本", APP_VERSION],
                ["文档版本", DOC_VERSION],
                ["编制日期", DOC_DATE],
                ["文档格式", "Microsoft Word（.docx）"],
                ["默认存放路径", r"docs\使用手册.docx"],
                ["打开方式", "软件顶部「帮助」页签 →「使用手册」"],
            ],
            widths=[4.2, 11.2],
            font_size=9.5,
            first_col_bold=True,
        )

        self.note(
            "本手册默认随软件一起分发。若「帮助 → 使用手册」提示找不到文件，"
            "请确认 `docs/使用手册.docx` 是否存在，或在 `config/app.yaml` 的 "
            "`app.help.manual_path` 中改为实际路径；也可以配置 `app.help.manual_url` "
            "指向在线文档。",
            label="说明",
        )

        self.doc.add_page_break()

    def toc(self, entries: list[tuple[int, str]]) -> None:
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("目  录")
        _set_run_font(run, size=20, bold=True, color=BLUE)
        self.p("")

        for level, text in entries:
            para = self.doc.add_paragraph()
            pf = para.paragraph_format
            pf.tab_stops.add_tab_stop(Cm(16.5), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS)
            pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
            if level == 1:
                pf.space_before = Pt(7)
                pf.space_after = Pt(2)
                size = 11
                bold = True
                color = BLUE
                pf.left_indent = Cm(0)
            else:
                pf.space_before = Pt(0)
                pf.space_after = Pt(1.5)
                size = 10
                bold = False
                color = DARK
                pf.left_indent = Cm(0.75)

            key = " ".join(text.split())
            page = self.toc_pages.get(key)
            run = para.add_run(text)
            _set_run_font(run, size=size, bold=bold, color=color)
            run = para.add_run("\t")
            _set_run_font(run, size=size, color=color)
            run = para.add_run(str(page) if page is not None else "—")
            _set_run_font(run, size=size, bold=bold, color=color)

        self.p("")
        self.note(
            "目录页码按当前版本的实际排版生成，可直接点击 Word 的“导航窗格”按标题跳转。"
            "若增删章节或调整内容，请重新运行 `scripts/manual/build_manual.py` 生成手册。",
            label="目录说明",
        )

    # ------------------------------------------------------------ 各章节

    def ch1(self) -> None:
        self.chapter("1", "产品概述")
        self.h2("1.1 产品简介")
        self.p(
            f"{APP_NAME}是一套面向国土、林业、测绘等业务场景的桌面 GIS 软件。"
            "软件以 QGIS 为地图引擎，将工程管理、数据接入、地图浏览、图层编辑、"
            "质量检查、空间分析、瓦片制作和流程编排整合在同一界面中，"
            "用于图斑数据的核查、处理与成果输出。"
        )
        self.p(
            "软件采用工程化的工作方式：一个工程（.pgs）可以组织多个矢量、栅格和在线"
            "底图图层，并保存图层顺序、样式、坐标系和当前视图范围。日常作业通常按照"
            "“新建工程 → 加载数据 → 浏览/查询/编辑 → 质检/分析 → 导出或切片”的流程进行。"
        )

        self.h2("1.2 主要功能")
        self.table(
            ["功能模块", "主要能力"],
            [
                ["工程管理", "新建、打开、保存、另存为工程；保存图层、样式、坐标系与视图范围。"],
                ["数据接入", "添加 Shapefile、GeoJSON、KML、DXF、GeoPackage、SQLite、GDB、GeoTIFF、MBTiles、Excel、CSV 等数据；连接本地数据库。"],
                ["地图浏览", "平移、缩放、全图、视图历史、坐标定位、长度/面积测量、分屏对比、卷帘对比。"],
                ["查询选择", "点选、框选、选择集、i 查询、条件查询、空间查询、选择屏蔽、范围裁剪。"],
                ["图层编辑", "开始/停止/保存编辑，绘制点线面，节点编辑，合并、分割、挖洞、补洞、补缝、分解、边界修改、移动、删除、复制到。"],
                ["质量检查", "狭长面、细碎面、面几何角、面自相交、跨边界面不相交、面多部件、面重叠七类检查。"],
                ["空间分析", "矢量缓冲区、最小外包矩形、叠加分析、最短路径、坐标转换、格式转换，以及 GDAL 栅格/矢量工具。"],
                ["瓦片制作", "矢量瓦片（XYZ/PBF）、影像切片（XYZ 目录）、影像切片（MBTiles），支持图层范围/画布范围、高级参数与批量处理。"],
                ["流程编排", "节点式编辑器，把数据源、矢量分析、栅格分析和处理算法串成可复用的流程。"],
                ["数据管理", "本地数据库连接、建库建表、图层导入导出、字段结构检查、业务字典维护、未登记图层登记。"],
                ["成果输出", "导出图层、导出 CSV、保存样式、生成网页查看器、离线下载任务。"],
            ],
            widths=[3.6, 11.8],
            font_size=9,
            first_col_bold=True,
            caption="主要功能模块",
        )

        self.h2("1.3 运行环境")
        self.table(
            ["项目", "建议配置"],
            [
                ["操作系统", "Windows 10 / Windows 11 64 位；麒麟（Kylin）桌面版。"],
                ["处理器", "四核及以上，主频 2.0 GHz 以上。"],
                ["内存", "最低 8 GB，处理大幅影像或大图层建议 16 GB 及以上。"],
                ["硬盘", "安装与运行预留 10 GB 以上；切片、离线下载需按数据量另行预留空间。"],
                ["显示", "最低 1366×768；建议 1920×1080 及以上。"],
                ["运行库", "发行包已内置 QGIS 运行时，无需单独安装 QGIS；开发模式需本机安装 QGIS 4.x。"],
                ["网络", "登录、在线底图、问题反馈、检查更新需要网络；本地数据处理可离线进行。"],
            ],
            widths=[3.2, 12.2],
            font_size=9.5,
            first_col_bold=True,
            caption="运行环境建议",
        )

        self.h2("1.4 文档约定")
        self.bullet("界面元素用「」标注，如「文件」页签、「新建工程」按钮。")
        self.bullet("操作路径用“→”连接，如「文件」→「新建工程」。")
        self.bullet("需要输入的文本、文件名或配置项使用 `等宽字体` 表示。")
        self.bullet("“提示”“注意”用于说明容易出错的步骤或版本差异。")
        self.bullet("截图中的示例数据仅用于演示，不代表实际业务数据。")

    def ch2(self) -> None:
        self.chapter("2", "快速入门")
        self.p(
            "本章以一个完整的小例子串起最常用的操作：登录软件、新建工程、加载数据、"
            "浏览地图、保存工程并退出。按本章步骤操作一遍，即可掌握平台的基本工作方式。"
        )

        self.h2("2.1 启动与登录")
        self.p(
            "双击桌面快捷方式或发行目录中的启动程序。发行版通常提供 `app.exe`，"
            "开发模式使用 QGIS 自带的 `python-qgis.bat` 启动。程序启动后先显示登录窗口。"
        )
        self.step("在「用户名」中输入账号，在「密码」中输入密码。")
        self.step("如需使用本机保存过的账号，点击用户名输入框，从下拉列表中选择。")
        self.step("点击「登  录」。校验期间按钮显示「登录中…」，输入区暂时禁用。")
        self.step("校验通过后进入主界面；点击「取消」或关闭登录窗口则退出程序。")
        self.fig("03_登录.png", "登录窗口", width_cm=8.0)

        self.note(
            "账号由授权服务器统一校验，登录状态不会跨启动保存，每次启动都需要重新登录。"
            "「手机号登录」页在部分版本中为预留功能，验证码尚未接入，请优先使用账号登录。",
            label="注意",
        )

        self.h2("2.2 主界面总览")
        self.p(
            "主窗口由标题栏、顶部页签工具条、中央地图画布、左右停靠面板、底部面板和"
            "状态栏组成。界面采用无边框设计，标题栏空白处可拖动窗口，双击可最大化/还原。"
        )
        self.fig("01_主界面.png", "主界面总览", width_cm=15.0)

        self.table(
            ["区域", "说明"],
            [
                ["标题栏", "应用名称、当前工程名、登录用户、主题切换、任务/工程模式、窗口按钮。"],
                ["顶部页签", "文件、查看、编辑、通用、质检、帮助六个页签，切换页签显示对应工具条。"],
                ["左侧工具条", "悬浮在画布左上角，提供点选、框选、漫游、缩放、查询、测量等常用地图工具。"],
                ["中央画布", "显示地图图层；画布上方为消息条，右上角可显示坐标定位气泡。"],
                ["左侧面板", "「图层列表」与「数据浏览器」，默认显示图层列表。"],
                ["右侧面板", "「工程」「在线底图」「分析工具」「质检」「要素详情」等，按需打开并叠成标签页。"],
                ["底部面板", "属性表、日志信息等，按需打开并叠成标签页。"],
                ["状态栏", "鼠标坐标、比例尺、工程坐标系。"],
            ],
            widths=[3.0, 12.4],
            font_size=9.5,
            first_col_bold=True,
            caption="主界面区域说明",
        )

        self.h2("2.3 新建与打开工程")
        self.h3("2.3.1 新建工程")
        self.step("点击「文件」页签 →「新建工程」（快捷键 Ctrl+N）。")
        self.step("若当前已有图层，软件询问是否清空，确认后继续。")
        self.step("在「新建工程」对话框的「请输入工程名：」中输入名称，点击确定。")
        self.step("软件清空当前内容，建立「数据图层」（上）和「参考图层」（下）两个分组，"
                  "并在「参考图层」中加载默认底图。")
        self.step("窗口标题变为“应用名 - 工程名”，右侧「工程」面板显示工程名。")
        self.fig("51_新建工程后.png", "新建工程后的界面", width_cm=14.8)

        self.p(
            "新建工程只确定名称和分组结构，并不会立即生成工程文件；第一次执行"
            "「保存工程」时才会选择保存路径。"
        )

        self.h3("2.3.2 打开工程")
        self.step("点击「文件」页签 →「打开工程」（快捷键 Ctrl+Shift+O）。")
        self.step("在「打开地图文档」对话框中，从「最近打开」列表选择工程，"
                  "或点击「浏览…」选择其他 `.pgs` 文件。")
        self.step("点击「打开」。若当前工程有未保存内容，软件会先询问是否保存。")
        self.fig("50_打开工程.png", "打开地图文档对话框", width_cm=13.6)

        self.h2("2.4 加载数据")
        self.p(
            "「打开文件」与图层列表中的「添加数据」都会打开「添加数据」对话框。"
            "该对话框支持一次选择多个文件，也支持展开 GDB、GeoPackage、SQLite 后选择内部图层。"
        )
        self.step("点击「文件」页签 →「打开文件」（Ctrl+O），或点击图层列表工具栏的「添加数据」。")
        self.step("在左侧选择目录；可用顶部筛选切换「全部数据」「矢量数据」「栅格数据」"
                  "「表格数据」「本地数据库」。")
        self.step("单击选择一个或多个文件；GDB、GeoPackage、SQLite 可双击展开内部图层。")
        self.step("点击「添加」。数据加载到当前工程，图层出现在左侧「图层列表」。")
        self.fig("10_添加数据.png", "添加数据对话框", width_cm=14.6)

        self.note(
            "MBTiles 直接按栅格图层加载；Excel/CSV 表格可加载为属性表或用于建库。"
            "选择多个文件时，添加顺序与列表顺序一致。",
            label="提示",
        )

        self.h2("2.5 浏览地图")
        self.p(
            "最常用的浏览操作在「通用」页签的「视图」组和画布左侧工具条中。"
            "鼠标滚轮也可缩放地图，具体行为可在「编辑设置」中调整。"
        )
        self.bullet("「平移」：按住左键拖动画布。")
        self.bullet("「放大」「缩小」：单击放大/缩小一级，或拉框按框选范围缩放。")
        self.bullet("「全图」：缩放到所有有效图层的合并范围；无有效图层时会提示。")
        self.bullet("「刷新」：重新绘制画布。")
        self.bullet("左侧「前视图」「后视图」：在最多 50 步视图历史中前后切换。")
        self.fig("02_页签_通用.png", "「通用」页签与视图工具", width_cm=14.8)

        self.h2("2.6 保存工程与退出")
        self.h3("2.6.1 保存工程")
        self.bullet("「保存工程」（Ctrl+S）：已有工程文件时直接覆盖；没有工程文件时转入另存为。")
        self.bullet("「另存为」（Ctrl+Shift+S）：选择路径保存为新的 `.pgs` 工程文件。")
        self.bullet("工程文件未输入扩展名时，软件自动追加 `.pgs`。")
        self.p(
            "保存成功后，消息条提示“已保存工程：路径”。工程保存的内容包括图层清单、"
            "图层顺序、样式、工程坐标系和当前视图范围。"
        )

        self.h3("2.6.2 退出程序")
        self.p(
            "点击「文件」页签 →「退出」（Ctrl+Q），或点击标题栏关闭按钮。关闭前软件按顺序检查："
        )
        self.step("是否存在正在进行的影像切片任务，如有则询问是否终止切片并退出。")
        self.step("是否存在未保存的编辑图层，逐层询问保存、放弃或取消。")
        self.step("工程本身是否存在未保存修改，询问是否保存工程。")
        self.p("全部确认后程序退出。选择“取消”可返回主界面继续处理。")

    def ch3(self) -> None:
        self.chapter("3", "界面与导航")
        self.p(
            "平台没有传统菜单栏，而是用顶部页签组织命令。每个页签对应一行 Ribbon 风格"
            "工具条，按钮为“图标在上、文字在下”，工具条内按组排列，组名显示在按钮组下方。"
        )

        self.h2("3.1 顶部页签")
        self.table(
            ["页签", "主要命令"],
            [
                ["文件", "新建工程、保存工程、另存为、打开工程、打开文件、退出。"],
                ["查看", "日志、流程编排。"],
                ["编辑", "当前编辑图层与编辑工具、磁性捕捉设置。"],
                ["通用", "平移、放大、缩小、全图、刷新；点选、框选、取消选择；i 查询；分析；卷帘对比；在线底图、字典管理、创建数据、数据管理、离线下载。"],
                ["质检", "打开「质检」面板（七类检查）。"],
                ["帮助", "使用手册、问题反馈、关于、检查更新。"],
            ],
            widths=[2.2, 13.2],
            font_size=9.5,
            first_col_bold=True,
            caption="顶部页签与主要命令",
        )
        self.fig("02_页签_文件.png", "「文件」页签", width_cm=14.8)
        self.fig("02_页签_查看.png", "「查看」页签", width_cm=14.8)

        self.h3("3.1.1 「文件」页签")
        self.bullet("「新建工程」：清空当前工程并新建，快捷键 Ctrl+N。")
        self.bullet("「保存工程」：保存当前工程，Ctrl+S。")
        self.bullet("「另存为」：另选路径保存，Ctrl+Shift+S。")
        self.bullet("「打开工程」：打开「打开地图文档」对话框，Ctrl+Shift+O。")
        self.bullet("「打开文件」：打开「添加数据」对话框，Ctrl+O。")
        self.bullet("「退出」：关闭程序，Ctrl+Q。")

        self.h3("3.1.2 「查看」页签")
        self.bullet("「日志」：打开底部「日志信息」面板，Ctrl+L。")
        self.bullet("「流程编排」：打开节点式流程编辑器，Ctrl+Shift+F。")

        self.h3("3.1.3 「编辑」页签")
        self.p(
            "「编辑」页签分为两行：第一行是编辑工具，第二行是磁性捕捉。最左侧为"
            "「当前编辑图层」下拉框，未选择时显示「未选择编辑图层」，正在编辑时组名变为"
            "「当前编辑图层（编辑中）」。编辑工具的详细用法见第 6 章。"
        )
        self.fig("02_页签_编辑.png", "「编辑」页签", width_cm=14.8)

        self.h3("3.1.4 「通用」页签")
        self.p(
            "「通用」页签集中了最常用的地图工具和数据入口，分为视图、选择、查询、分析、"
            "对比和数据六组。质检按钮不在「通用」页签显示，质检入口保留在「质检」页签。"
        )
        self.fig("02_页签_通用.png", "「通用」页签", width_cm=14.8)

        self.h3("3.1.5 「质检」页签")
        self.p("「质检」页签只有一个「质检」按钮，用于打开右侧「质检」面板。")
        self.fig("02_页签_质检.png", "「质检」页签", width_cm=14.8)

        self.h3("3.1.6 「帮助」页签")
        self.p(
            "「帮助」页签分为「文档」和「支持」两组。「使用手册」打开本手册；"
            "「问题反馈」在浏览器中打开在线反馈页并带上当前用户信息；「关于」显示版本；"
            "「检查更新」立即检查新版本。"
        )
        self.fig("02_页签_帮助.png", "「帮助」页签", width_cm=14.8)

        self.h2("3.2 左侧竖排工具条")
        self.p(
            "左侧工具条悬浮在画布左上角，自上而下排列常用地图工具。画布高度不足时，"
            "放不下的按钮会收进底部「更多」小三角面板。地图工具为单选高亮，"
            "分屏、定位、范围裁剪等会话型工具在开启期间保持高亮。"
        )
        self.table(
            ["按钮", "功能"],
            [
                ["点选", "单击选择要素；Ctrl 追加，Shift 移除。"],
                ["框选", "拉矩形框选择要素；修饰键规则与点选相同。"],
                ["漫游", "平移地图。"],
                ["缩放", "拉框放大；按住 Shift 拉框缩小。"],
                ["全图", "缩放到全部有效图层范围。"],
                ["前视图", "回到上一个视图范围。"],
                ["后视图", "前进到下一个视图范围。"],
                ["清除", "清空选择，并清除测量等临时图形。"],
                ["i查询", "打开识别查询面板。"],
                ["条件查询", "打开「条件查询」对话框。"],
                ["空间查询", "打开「空间查询」对话框。"],
                ["选择屏蔽", "只显示选中图形，屏蔽未选中图形。"],
                ["取消屏蔽", "还原被屏蔽图层。"],
                ["分屏", "打开「地图对比」对话框。"],
                ["定位", "打开画布右上角的坐标定位气泡。"],
                ["线测量", "测量折线长度。"],
                ["面测量", "测量面积和周长。"],
                ["范围裁剪", "拉框后只显示框内矢量要素。"],
            ],
            widths=[3.0, 12.4],
            font_size=9.3,
            first_col_bold=True,
            caption="左侧竖排工具条",
        )

        self.h2("3.3 图层列表")
        self.p(
            "「图层列表」位于左侧，默认显示。顶部工具栏提供图层顺序/数据源切换、添加数据、"
            "渲染设置、图层组、删除、展开/折叠、上移/下移等操作。图层行包含可见性勾选框、"
            "图例、图层名称；矢量图层行尾有「标注」开关。"
        )
        self.table(
            ["按钮", "作用"],
            [
                ["按图层顺序展示", "按当前绘制顺序排列图层，默认模式。"],
                ["按数据源展示", "按数据源文件或服务分组展示。"],
                ["添加数据", "打开「添加数据」对话框。"],
                ["打开图层渲染设置", "打开当前图层的渲染设置。"],
                ["添加图层组", "在当前层级新建图层组。"],
                ["删除图层/组", "移除选中的图层或图层组。"],
                ["全部展开 / 全部折叠", "展开或折叠图层树。"],
                ["上移 / 下移", "调整图层或图层组的顺序。"],
            ],
            widths=[4.2, 11.2],
            font_size=9.3,
            first_col_bold=True,
            caption="图层列表工具栏",
        )
        self.p(
            "双击图层文字可打开属性表，双击符号图标可打开符号选择器，按 F2 可重命名。"
            "数据源丢失时图层名称前显示红色感叹号，点击后可重设数据源。底部「图层信息」"
            "区域显示当前图层和要素数量。"
        )

        self.h3("3.3.1 图层右键菜单")
        self.p("在图层列表中右键单击图层，可执行常用管理操作：")
        self.table(
            ["菜单项", "说明"],
            [
                ["缩放至图层", "把画布缩放到该图层范围。"],
                ["放大至选中要素", "矢量图层有选中要素时可用。"],
                ["打开属性表", "打开该矢量图层的属性表。"],
                ["图层属性", "打开常规、字段、选择、标注、渲染、条件等设置页。"],
                ["渲染样式", "编辑样式、导入样式、保存样式、复制/粘贴样式。"],
                ["导出数据", "导出为 shp、gdb、gpkg、sqlite 等格式。"],
                ["执行SQL", "对本地矢量数据执行 SQL 查询。"],
                ["重命名 / 复制图层", "重命名或生成“原名称_副本”。"],
                ["移到参考图层 / 数据图层", "在工程的两个分组之间移动。"],
                ["关闭所有图层 / 打开所有图层", "批量切换可见性。"],
                ["移除 / 移除所有", "从工程移除，不删除磁盘数据文件。"],
                ["置顶 / 上移 / 下移 / 置底", "调整图层顺序。"],
            ],
            widths=[4.6, 11.0],
            font_size=9.2,
            first_col_bold=True,
            caption="图层右键菜单",
        )
        self.p(
            "多选图层时，右键菜单提供「移除」「组合」「打开」「关闭」「缩放图层」。"
            "图层组右键菜单提供「缩放到组」「复制组」「粘贴图层/组」「重命名组」「添加组」"
            "以及排序、显示和移除操作。"
        )

        self.h2("3.4 数据浏览器")
        self.p(
            "「数据浏览器」与「图层列表」叠成左侧标签页，用于浏览本机目录和数据源。"
            "双击节点按类型加载图层：矢量按矢量加载，其他按栅格加载。右键可刷新，"
            "对 `.gdb` 目录还可「新增图层」，选择名称和几何类型（点/线/面）。"
        )

        self.h2("3.5 右侧面板")
        self.p(
            "右侧停靠区按需显示「工程」「在线底图」「分析工具」「质检」「要素详情」等面板，"
            "多个面板会叠成标签页，同一时刻显示一个。面板可拖动、浮动或关闭。"
        )
        self.h3("3.5.1 工程面板")
        self.p(
            "新建或打开带分组的工程后，右侧出现「工程」面板，显示工程名、"
            "「数据图层」树、「参考图层」树以及「渲染与标注」区域。"
        )
        self.fig("06_工程面板.png", "工程面板", width_cm=7.2)
        self.p(
            "选中图层后，底部显示「当前图层」，并可点击「渲染设置」「标注设置」进入对应页。"
            "在工程面板中右键图层，使用与图层列表相同的右键菜单。"
        )

        self.h3("3.5.2 在线底图面板")
        self.fig("07_在线底图面板.png", "在线底图面板", width_cm=7.2)
        self.p(
            "面板树按「我的收藏」「天地图」「高德地图」「自定义底图」等分组展示。"
            "双击或右键「添加到画布」即可加载；右键还可编辑连接、移动分组或删除连接。"
        )

        self.h3("3.5.3 分析工具面板")
        self.fig("05_分析工具面板.png", "分析工具面板", width_cm=7.4)
        self.p(
            "顶部搜索框可按名称过滤工具；分组包括「矢量分析」「切片导出」「流程编排」"
            "以及 GDAL 栅格分析、地形、数据源、元数据、矢量分析、数据导出等。"
            "单击工具即可打开对应参数对话框。"
        )

        self.h3("3.5.4 质检面板")
        self.fig("04_质检面板.png", "质检面板", width_cm=7.2)
        self.p(
            "质检面板列出七类面检查：狭长面、细碎面、面几何角检查、面自相交、"
            "跨边界面不相交、面多部件检查、面重叠。双击或单击检查项打开参数对话框。"
        )

        self.h2("3.6 底部面板")
        self.h3("3.6.1 属性表")
        self.p(
            "双击图层或右键「打开属性表」可打开「属性表 - 图层名」面板。表格上方为编辑、"
            "筛选、字段计算器、组织列、导出 CSV、缩放到所选记录等工具；底部可切换"
            "「全部要素」「仅选中要素」「仅指定要素」，并支持分页加载。"
        )
        self.fig("08_属性表.png", "属性表面板", width_cm=14.6)

        self.h3("3.6.2 日志信息")
        self.p(
            "「查看」→「日志」（Ctrl+L）打开底部「日志信息」面板。可按文本、时间范围、"
            "级别和行数筛选当前会话日志，支持清空、刷新、保存日志和导出诊断包。"
        )
        self.fig("38_日志面板.png", "日志信息面板", width_cm=14.6)

        self.h2("3.7 标题栏与状态栏")
        self.h3("3.7.1 标题栏")
        self.p(
            "标题栏从左到右依次为应用图标、窗口标题、登录用户区、主题按钮、"
            "任务/工程模式切换和窗口按钮。点击用户区可查看账号、姓名、单位、区域、"
            "手机、上次登录和会话有效期等信息；这些信息为只读展示。"
        )
        self.fig("40_用户菜单.png", "标题栏用户信息菜单", width_cm=5.0)
        self.p(
            "主题按钮提供「浅色」「深色」「蓝色」「绿色」四种主题，当前主题带勾选。"
            "任务模式与工程模式用于标识工作模式，当前版本切换时主要记录日志，"
            "界面功能保持一致。"
        )

        self.h3("3.7.2 状态栏")
        self.bullet("鼠标坐标：显示光标当前位置，提示“当前鼠标位置坐标”。")
        self.bullet("比例尺：随画布缩放实时变化。")
        self.bullet("坐标系：显示当前工程 CRS，点击可切换；切换后同时更新工程和画布。")
        self.p("图层加载或渲染较慢时，状态栏左侧会显示“正在加载图层…”或“正在渲染图层…”。")

    def ch4(self) -> None:
        self.chapter("4", "工程与数据管理")
        self.p(
            "工程是平台组织数据的基本单位。本章介绍工程文件、图层管理、数据接入、"
            "本地数据库、字典和在线底图等内容。"
        )

        self.h2("4.1 工程文件与分组")
        self.p(
            "工程文件扩展名为 `.pgs`，保存图层清单、绘制顺序、样式、工程坐标系和当前视图范围。"
            "新建工程时自动建立两个分组："
        )
        self.bullet("「数据图层」：位于上层，放置业务数据（图斑、道路、点位等）。")
        self.bullet("「参考图层」：位于下层，放置影像、在线底图等参考数据。")
        self.p(
            "图层顺序遵循“上面的图层先绘制”。可通过图层列表的上移/下移或右键排序调整。"
            "工程文件不复制原始数据，移动工程到其他电脑时，应同时保证数据文件路径可访问；"
            "路径失效的图层会显示红色感叹号。"
        )

        self.h2("4.2 图层管理")
        self.h3("4.2.1 添加与移除")
        self.bullet("添加：图层列表工具栏「添加数据」，或「文件」→「打开文件」。")
        self.bullet("移除：选中图层后点击「删除图层/组」，或右键「移除」；只从工程移除，不删除磁盘文件。")
        self.bullet("复制：右键「复制图层」，生成“原名称_副本”。")
        self.bullet("组合：多选图层后右键「组合」，归入新建图层组。")

        self.h3("4.2.2 显示与顺序")
        self.bullet("勾选/取消图层前的复选框控制可见性。")
        self.bullet("「关闭所有图层」「打开所有图层」批量切换可见性。")
        self.bullet("右键「置顶」「上移」「下移」「置底」调整顺序，到边界时菜单项置灰。")
        self.bullet("「按数据源展示」按 Shapefile 文件夹、GeoPackage/SQLite/GDB 文件、内存图层、在线服务等分组。")

        self.h3("4.2.3 重命名与数据源")
        self.p(
            "选中图层按 F2 或右键「重命名」可修改显示名称。数据源丢失时，名称前出现红色感叹号，"
            "点击后按提示重新指定数据源；修复后图层恢复显示。"
        )

        self.h2("4.3 添加数据")
        self.p(
            "「添加数据」对话框支持从本机选择矢量、栅格、表格和本地数据库数据。文件列表可多选，"
            "GDB、GeoPackage、SQLite 等容器可展开选择内部图层，文件和图层可一起添加。"
        )
        self.table(
            ["数据类型", "常见格式"],
            [
                ["矢量数据", "Shapefile（.shp）、GeoJSON（.geojson/.json）、KML（.kml）、DXF（.dxf）、GeoPackage、SQLite、GDB。"],
                ["栅格数据", "GeoTIFF（.tif/.tiff）、MBTiles（.mbtiles）及其他 GDAL 支持的影像格式。"],
                ["表格数据", "Excel（.xlsx/.xls）、CSV。"],
                ["本地数据库", "GeoPackage（.gpkg）、SQLite（.sqlite/.db）、SpatiaLite、GDB。"],
            ],
            widths=[3.4, 12.0],
            font_size=9.4,
            first_col_bold=True,
            caption="支持的数据类型",
        )
        self.fig("54_添加数据_浏览.png", "在添加数据对话框中浏览目录", width_cm=14.4)
        self.p(
            "列表底部显示“未选择数据”或已选数量；点击「全选」可选择当前目录的全部文件，"
            "按住 Ctrl/Shift 可多选。双击容器节点可展开内部图层。"
        )

        self.h2("4.4 创建数据")
        self.p(
            "「通用」→「创建数据」打开向导。向导包含「单图层数据」和「本地数据库」两个页签，"
            "分两步完成：先设置目标与基本参数，再设置字段结构。"
        )
        self.h3("4.4.1 第一步：目标与基本参数")
        self.p(
            "「目标」下拉框可选择新建独立数据文件、写入已有本地数据库或创建空库；"
            "根据目标不同，界面显示「创建路径」「数据文件名称」「数据格式」「目标库」"
            "「图层名」「几何类型」「坐标系」等字段。默认坐标系为 EPSG:4490（CGCS2000）。"
        )
        self.fig("11_创建数据.png", "创建数据向导第一步", width_cm=14.2)
        self.note(
            "GeoJSON、KML、GPX 等格式按规范强制使用 WGS84 坐标系；"
            "需要保留 CGCS2000 或地方坐标系时，请选择 Shapefile、GeoPackage、SQLite 等格式。",
            label="注意",
        )

        self.h3("4.4.2 第二步：字段结构")
        self.p(
            "第二步「数据字段设置」用于定义属性表结构。表头包括「字段名」「字段别名」"
            "「字段类型」「字段长度」「小数位数」「字典表」「字典域」「是否可为空」「是否可见」。"
            "可通过「导入」读取模板或外部数据字段，也可点击「生成空模板文件」。"
        )
        self.fig("53_创建数据_字段.png", "创建数据向导第二步：字段结构", width_cm=14.4)
        self.bullet("双击单元格直接编辑；双击「字典表」「字典域」可选择业务字典。")
        self.bullet("回车或失去焦点确认修改，方向键切换单元格，Delete 删除选中行。")
        self.bullet("系统会自动增加 `mzguid` 字段作为要素唯一标识。")
        self.bullet("点击「上一步」可返回修改目标；点击「创建」完成建库建表。")
        self.p("创建完成后，软件提示结果，新图层可直接加入当前地图。")

        self.h2("4.5 数据管理与连接")
        self.p(
            "「通用」→「数据管理」打开「数据管理」对话框。左侧为「数据库连接」树，"
            "根节点为「本地数据库」和「服务器数据库」；右侧显示所选库中的图层列表，"
            "列为「数据名称」「类型」「几何类型」「坐标系」「说明」。"
        )
        self.fig("27_数据管理.png", "数据管理对话框", width_cm=14.6)

        self.h3("4.5.1 新建连接")
        self.step("点击左下角「新建连接」。")
        self.step("在「本地数据库」页签点击「浏览…」，选择 SQLite、GeoPackage 或 SpatiaLite 文件；"
                  "也可点击「新建数据库…」创建一个空库。")
        self.step("如需自动登记库中已有但未加入清单的矢量图层，勾选相应选项。")
        self.step("点击「确定」，连接出现在左侧树中。")
        self.fig("12_新建连接.png", "新建本地数据库连接", width_cm=13.2)
        self.p(
            "「服务器数据库」页签用于 PostgreSQL 等服务器库，字段包括「数据库类型」「主机」"
            "「端口」「数据库名」「用户名」「密码」，端口默认 5432。该页签在部分版本中默认隐藏，"
            "且服务器目录浏览尚未接入，请以实际版本为准。"
        )

        self.h3("4.5.2 库节点操作")
        self.p("在左侧库节点上右键，可执行：")
        self.bullet("「导入图层」：把当前工程中的图层导入该库。")
        self.bullet("「登记未登记图层…」：把库中已有但未登记到清单的图层纳入管理。")
        self.bullet("「打开文件所在位置」：在文件管理器中定位库文件。")
        self.bullet("「移除全部图层」「删除连接」「刷新」。")
        self.fig("52_数据管理_连接后.png", "已连接的本地数据库", width_cm=14.6)

        self.h3("4.5.3 图层操作")
        self.p("在右侧图层列表中选择图层后，可执行：")
        self.bullet("「加到地图上展示」：把库中图层加载到当前工程。")
        self.bullet("「导出字段结构(XLS)」：导出字段清单。")
        self.bullet("「检查字段结构」：检查字段命名、类型、长度等问题，结果按「级别」「字段」「问题」列出。")
        self.bullet("「编辑名称」「删除图层」「导入数据」「导出数据」。")
        self.fig("81_数据管理_库节点.png", "选择库节点后的图层列表", width_cm=14.6)

        self.h3("4.5.4 导入图层")
        self.step("在库节点上点击「导入」或在图层上点击「导入数据」。")
        self.step("在「导入图层到数据库」对话框中选择要导入的图层。")
        self.step("确认目标库、目标表名和字段映射后执行导入。")
        self.step("导入完成后点击「刷新」查看新表。")
        self.fig("83_数据管理_导入.png", "导入图层到数据库", width_cm=13.8)
        self.note(
            "导入前请确认目标库可写、磁盘空间充足；导入过程会按数据量耗时，"
            "大图层建议在业务空闲时执行。",
            label="提示",
        )

        self.h2("4.6 图层属性")
        self.p(
            "右键图层 →「图层属性」打开图层属性对话框。对话框按页签组织："
            "「常规」「字段」「选择」「标注」「渲染」「条件」等，不同图层类型显示的页签略有差异。"
        )
        self.fig("09_图层属性.png", "图层属性对话框", width_cm=14.4)
        self.bullet("常规：图层名称、数据源、坐标系、范围等信息。")
        self.bullet("字段：查看字段名、类型、长度和别名。")
        self.bullet("选择：设置选择符号和选择行为。")
        self.bullet("标注：配置标注字段、字体、位置和规则。")
        self.bullet("渲染：设置符号、分类、分级等渲染方式。")
        self.bullet("条件：按表达式控制要素显示。")
        self.p("修改后可点击「应用」预览，确认后点击「确定」保存到工程。")

        self.h2("4.7 字典管理")
        self.p(
            "业务字典用于约束属性取值，例如地类、权属、行政区等。「通用」→「字典管理」"
            "打开字典管理对话框。顶部选择「字典表」，下方树按「名称」「代码」"
            "「字典域/说明」展示字典域和字典项。"
        )
        self.fig("26_字典管理.png", "字典管理对话框", width_cm=14.4)
        self.bullet("「创建字典表」「删除字典表」：管理字典表集合。")
        self.bullet("「同级添加」「下级添加」：在选中节点旁或下方新增字典域/字典项。")
        self.bullet("「新建字典域」「修改」「删除」：维护当前字典域。")
        self.bullet("「Excel导入」「Excel导出」：按当前字典表批量导入/导出。")
        self.bullet("双击单元格可直接修改；同级节点可拖拽排序；右键可继续添加或删除。")
        self.p(
            "删除字典表或字典项前请确认没有属性字段和已有数据引用，"
            "避免历史值无法匹配。Excel 导入默认针对当前字典表，可选择先清除同名域。"
        )

        self.h2("4.8 在线底图")
        self.p(
            "「通用」→「在线底图」打开右侧面板。面板内置天地图的影像、矢量、影像标注、"
            "矢量标注，以及高德的矢量、路网、影像，并支持「自定义底图」。"
        )
        self.fig("07_在线底图面板.png", "在线底图面板", width_cm=7.0)
        self.bullet("展开分组，双击或右键「添加到画布」加载底图。")
        self.bullet("右键「编辑连接」可修改名称、地址、层级和 API Key。")
        self.bullet("右键「移动到分组」「删除连接」管理清单。")
        self.bullet("「自定义底图…」支持 XYZ、ArcGIS MapServer/ImageServer/FeatureServer、WMS、WMTS。")
        self.p(
            "XYZ 地址必须包含 `{x}`、`{y}`、`{z}` 占位符。天地图等服务的 Key 可在"
            "`config/app.yaml` 的 `map.keys` 中统一配置，修改后重新加载底图生效。"
        )

        self.h2("4.9 离线下载")
        self.p(
            "「通用」→「离线下载」打开「下载离线图层」对话框，用于把任务所需的数据和影像"
            "预先下载到本地。"
        )
        self.fig("71_离线下载.png", "下载离线图层对话框", width_cm=12.6)
        self.bullet("「选择任务」：选择要下载的核查任务。")
        self.bullet("「空间范围」：全部空间范围、视图范围或框选范围。")
        self.bullet("「下载图层」：勾选任务涉及的矢量、属性模板、质检规则、字典、参考影像等。")
        self.bullet("「影像层级 z 最小/最大」：默认 0～16，层级越高数据量越大。")
        self.bullet("「下载位置」：选择保存目录，界面显示剩余空间。")
        self.bullet("「下载管理」：查看待下载、下载中、暂停、成功、部分失败、失败、已取消等状态。")
        self.p(
            "当前版本的实际落盘结果为 GeoPackage 等数据文件，界面中出现的“压缩包/zip”"
            "字样属于历史文案，请以输出目录中的实际文件为准。"
        )

    def ch5(self) -> None:
        self.chapter("5", "地图浏览与查询")
        self.p(
            "本章介绍地图视图控制、要素选择、识别与查询、测量、定位、屏蔽、裁剪和对比等功能。"
            "这些工具主要位于「通用」页签和画布左侧工具条。"
        )

        self.h2("5.1 视图控制")
        self.bullet("「平移」：按住左键拖动画布；也可用鼠标中键临时平移。")
        self.bullet("「放大」「缩小」：单击缩放一级，或拉框按框选范围缩放。")
        self.bullet("左侧「缩放」：默认拉框放大，按住 Shift 拉框缩小。")
        self.bullet("「全图」：合并所有有效图层范围缩放；无有效图层时提示“当前无有效图层”。")
        self.bullet("「前视图」「后视图」：在最多 50 步视图历史中切换，到边界会提示。")
        self.bullet("「刷新」：重新绘制画布。")
        self.p("画布右下角显示比例尺和工程坐标系；状态栏同步显示鼠标坐标。")

        self.h2("5.2 选择与选择集")
        self.h3("5.2.1 点选与框选")
        self.p(
            "「点选」单击选择要素，优先选择几何真正包含点击点的要素，未命中时使用约 5 像素容差；"
            "多个要素叠压时只选最上层一个。「框选」拉出青色矩形，松开后选择矩形内要素。"
        )
        self.bullet("无修饰键：替换原有选择；点空白处清空。")
        self.bullet("Ctrl：追加选择。")
        self.bullet("Shift：从选择中移除。")
        self.bullet("画布上按 Delete：删除处于编辑态且有选中要素的图层中的选中要素，可 Ctrl+Z 撤销。")
        self.fig("69_框选过程.png", "框选要素", width_cm=14.4)

        self.h3("5.2.2 选择集面板")
        self.p(
            "框选完成后默认自动打开「选择集」面板，标题显示“当前选择集 · N 个要素”。"
            "多图层时可通过下拉框切换图层，表格列为 ID 加各字段，支持分页。"
        )
        self.fig("30_选择集.png", "选择集面板", width_cm=12.4)
        self.bullet("点击行：在画布上闪烁对应图斑，不缩放。")
        self.bullet("「定位」：缩放到当前行要素并闪烁。")
        self.bullet("「清除选择」：只清除面板中列出的图层，不关闭面板。")
        self.bullet("「关闭」：关闭面板。")
        self.p(
            "选择集显示位置和是否自动弹出可在「编辑设置」中调整。"
            "选择集会跟随选择变化实时刷新，且不抢占主窗口焦点。"
        )

        self.h2("5.3 i 查询")
        self.p(
            "点击「通用」→「i查询」或左侧「i查询」，再点击地图要素，即可打开「i查询」面板。"
        )
        self.fig("31_i查询.png", "i查询结果面板", width_cm=7.2)
        self.bullet("「识别来源」：最顶层图层、所有可见图层、所有图层或指定图层。")
        self.bullet("「识别结果（N）」：按图层分组列出命中的要素。")
        self.bullet("「位置」：显示命中点坐标。")
        self.bullet("属性表：按「字段」「值」两列显示要素属性。")
        self.bullet("单击结果：高亮对应要素；双击结果：定位并闪烁。")
        self.bullet("「复制信息」：复制识别结果；「关闭」：关闭面板并清除高亮。")

        self.h2("5.4 条件查询")
        self.p(
            "左侧「条件查询」或图层右键相关入口打开「条件查询」对话框。选择图层和字段，"
            "设置运算符与值后执行查询。结果选择方式包括："
        )
        self.fig("76_条件查询_设置.png", "条件查询设置", width_cm=14.2)
        self.bullet("「创建一个新的选择集」：替换当前选择。")
        self.bullet("「增加到当前选择集」：在现有选择上追加。")
        self.bullet("「从当前选择集中移除」：从现有选择中剔除命中要素。")
        self.fig("48_条件查询_结果.png", "条件查询结果", width_cm=14.2)
        self.p("执行后命中的要素在地图上高亮，并可在选择集或属性表中继续查看。")

        self.h2("5.5 空间查询")
        self.p(
            "「空间查询」按几何关系查找要素，支持相交、包含、被包含、重叠、穿越、接触、等于、"
            "不相交以及距离范围内等关系。"
        )
        self.fig("77_空间查询_设置.png", "空间查询设置", width_cm=14.2)
        self.step("选择输入图层和参考图层。")
        self.step("选择空间关系；选择“距离范围内”时设置距离。")
        self.step("选择结果处理方式（新建/追加/移除选择集）。")
        self.step("点击执行，查看地图高亮和结果。")
        self.fig("49_空间查询_结果.png", "空间查询结果", width_cm=14.2)
        self.p("当前工程没有可查询的矢量图层时，软件会提示“当前工程没有可做空间查询的矢量图层”。")

        self.h2("5.6 坐标定位与测量")
        self.h3("5.6.1 坐标定位")
        self.p(
            "点击左侧「定位」，画布右上角出现定位气泡。选择输入坐标系，输入「横坐标 X」"
            "「纵坐标 Y」，点击「定位」或按 Enter。可选坐标系包括经纬度（CGCS2000）、"
            "CGCS2000 3 度带 114E/117E、3 度带 38 带/39 带等。"
        )
        self.p(
            "定位会把画布中心移到目标坐标，尽量保留当前缩放级别；若当前视野过大，"
            "会收窄到约 2 公里范围，并在画布上显示红色十字标点。按 Esc 或点击气泡上的「×」关闭。"
        )

        self.h3("5.6.2 长度与面积测量")
        self.p(
            "「线测量」用于测量折线长度，「面测量」用于测量面积和周长。左键连续落点，右键结束；"
            "移动鼠标时显示预览虚线，结果显示在画布底部居中的测量读数条中。"
        )
        self.fig("32_线测量.png", "线测量", width_cm=14.2)
        self.fig("33_面测量.png", "面测量", width_cm=14.2)
        self.bullet("Backspace：撤销上一个落点。")
        self.bullet("Esc：清除全部落点、图形和读数。")
        self.bullet("右键结束后结果和图形保留，再左键开始新一次测量。")
        self.bullet("再次点击当前测量按钮可取消测量，并恢复进入测量前的画布工具。")
        self.bullet("测量落点可参与磁性捕捉。")

        self.h2("5.7 选择屏蔽与范围裁剪")
        self.h3("5.7.1 选择屏蔽")
        self.p(
            "先点选或框选要素，再点击左侧「选择屏蔽」，软件按图层屏蔽未选中图形。"
            "清除或改选不会改变已屏蔽结果。点击「取消屏蔽」恢复被屏蔽图层的渲染。"
            "没有选中要素时会提示先用点选/框选选中图形；没有开启屏蔽时会提示“当前没有开启选择屏蔽”。"
        )

        self.h3("5.7.2 范围裁剪")
        self.p(
            "点击左侧「范围裁剪」，在画布上按住左键拉矩形框，软件只显示框内的矢量要素，"
            "影像图层不受影响。再次点击「范围裁剪」取消裁剪并还原图层。"
            "提示信息会显示裁剪或还原的图层数量。"
        )

        self.h2("5.8 地图对比与卷帘对比")
        self.h3("5.8.1 地图对比（分屏）")
        self.p(
            "点击左侧「分屏」打开「地图对比」对话框。选择对比图层（可多选）和模式："
            "垂直分割（左/右）、水平分割（上/下）、放大镜（跟随鼠标）、镜像分屏（双画布）。"
            "放大镜模式可设置圆形/方形和大小。点击「开始对比」进入对比，点击「取消」关闭。"
        )
        self.fig("23_地图对比.png", "地图对比对话框", width_cm=12.6)

        self.h3("5.8.2 卷帘对比")
        self.p(
            "「通用」→「卷帘对比」需要工程中至少有二个栅格图层，否则提示“请先加载至少两个栅格图层”。"
            "在画布右上角的「卷帘对比」面板中选择基准图层、对比图层和方向，点击「开始卷帘」。"
            "再次点击工具按钮或关闭面板即退出。"
        )
        self.fig("24_卷帘对比.png", "卷帘对比面板", width_cm=9.6)

    def ch6(self) -> None:
        self.chapter("6", "图层编辑")
        self.p(
            "图层编辑用于新建、修改和删除矢量要素。编辑操作必须先选择编辑图层并开始编辑，"
            "修改完成后保存并停止编辑。编辑过程中可随时撤销/重做。"
        )

        self.h2("6.1 开始编辑")
        self.step("切换到「编辑」页签。")
        self.step("在左侧「当前编辑图层」下拉框中选择目标图层；无可用图层时显示「无可编辑图层」。")
        self.step("点击「开始编辑」。若尚未选择图层，软件弹出「选择编辑图层」对话框。")
        self.step("图层进入编辑态后，组名显示「当前编辑图层（编辑中）」，可开始绘制或修改。")
        self.fig("70_进入编辑态.png", "进入编辑态", width_cm=14.6)
        self.fig("34_选择编辑图层.png", "选择编辑图层对话框", width_cm=10.8)

        self.h2("6.2 绘制要素")
        self.p(
            "「绘制」组提供「添加点」「添加线」「添加面」。选择工具后在画布上单击落点，"
            "右键结束绘制；点要素单击一次即可。绘制完成后按提示录入属性。"
        )
        self.bullet("绘制过程中 Ctrl+Z 撤销上一个落点，Ctrl+Y 重做。")
        self.bullet("右键结束当前要素；Esc 取消当前绘制。")
        self.bullet("绘制时可使用磁性捕捉，落点自动吸附到顶点或边。")
        self.bullet("属性录入完成后要素写入编辑缓冲区，需保存编辑才写入数据源。")

        self.h2("6.3 节点编辑")
        self.p(
            "「几何」组「节点编辑」用于修改要素形状。点选要素后显示节点：拖动节点改变位置，"
            "双击边添加节点，右键节点删除，Esc 退出。"
        )
        self.fig("35_节点编辑.png", "节点编辑", width_cm=14.6)
        self.p(
            "「节点列表」主要面向编辑中的面图层，以列表方式展示节点序号和坐标，"
            "便于精确定位和批量处理。"
        )
        self.fig("36_节点列表.png", "节点列表面板", width_cm=10.8)

        self.h2("6.4 几何编辑工具")
        self.p("「编辑」页签的「几何」和「分割」组提供以下工具：")
        self.table(
            ["工具", "说明"],
            [
                ["合并", "把同一编辑图层中至少两个相邻的选中要素合并为一个。"],
                ["边界修改", "对选中面要素修改公共边界。"],
                ["移动要素", "移动选中的要素。"],
                ["删除", "删除选中要素，可 Ctrl+Z 撤销。"],
                ["复制到", "把选中要素复制到同几何类型的其他图层。"],
                ["线分割", "用绘制的线分割选中要素。"],
                ["面分割", "用绘制的线分割选中面。"],
                ["挖洞", "在选中面内部挖出空洞。"],
                ["补洞", "填补面中的空洞。"],
                ["补缝", "在至少两个相邻面之间填补缺口。"],
                ["分解", "把多部件要素分解为单部件。"],
                ["要素分割", "用其他图层上的选中要素作为分割依据。"],
            ],
            widths=[3.2, 12.2],
            font_size=9.3,
            first_col_bold=True,
            caption="常用几何编辑工具",
        )
        self.fig("55_编辑_合并.png", "合并要素", width_cm=14.4)
        self.fig("56_编辑_挖洞.png", "挖洞", width_cm=14.4)
        self.fig("57_编辑_补洞.png", "补洞", width_cm=14.4)
        self.fig("58_编辑_分解.png", "分解多部件要素", width_cm=14.4)
        self.fig("59_编辑_补缝.png", "补缝", width_cm=14.4)
        self.fig("60_编辑_边界修改.png", "边界修改", width_cm=14.2)
        self.fig("61_编辑_移动要素.png", "移动要素", width_cm=14.2)
        self.fig("62_编辑_复制到.png", "复制要素到其他图层", width_cm=14.2)

        self.note(
            "几何编辑工具要求先选中要素并处于编辑态。合并要求至少两个相邻要素；"
            "补缝要求至少两个相邻面。操作失败时请检查选择数量、几何类型和相邻关系。",
            label="提示",
        )

        self.h2("6.5 属性录入与属性表")
        self.p(
            "「属性录入」会自动进入编辑状态并打开录入对话框，按字段填写后确认保存。"
            "也可以双击图层打开属性表，在表格中直接修改字段值。"
        )
        self.fig("08_属性表.png", "在属性表中编辑属性", width_cm=14.6)
        self.bullet("属性表工具栏提供开始编辑、保存编辑、停止编辑、属性筛选、字段计算器、组织列、导出 CSV 等。")
        self.bullet("字段绑定字典时，单元格显示下拉选项，避免手工输入错误。")
        self.bullet("底部可切换「全部要素」「仅选中要素」「仅指定要素」，并分页加载。")
        self.bullet("修改后需点击「保存编辑」或停止编辑时选择保存，才会写入数据源。")

        self.h2("6.6 捕捉与追踪")
        self.p(
            "「编辑」页签第二行为「磁性捕捉」工具条。捕捉默认开启，默认同时吸附「顶点」和「边」，"
            "容差默认 12 像素，单位可选「像素」或「图层单位」。"
        )
        self.bullet("「捕捉」：开关磁性捕捉；快捷键 S。")
        self.bullet("「顶点」「边」：选择吸附对象，默认均开启。")
        self.bullet("「追踪」：沿已有要素追踪绘制；需先开启捕捉，快捷键 T。")
        self.bullet("图层范围：可选「所有图层」或「当前图层」。")
        self.bullet("容差：0～9999，默认 12。")
        self.p("关闭捕捉会同时取消追踪；绘制和测量落点均可参与捕捉。")

        self.h2("6.7 撤销、保存与停止编辑")
        self.bullet("「撤销」Ctrl+Z，可回退编辑和绘制落点；「重做」Ctrl+Y。")
        self.bullet("「保存编辑」保存当前修改；没有修改时提示“没有需要保存的修改”。")
        self.bullet("「停止编辑」询问“是否保存编辑的内容！”，可选择是/否/取消。")
        self.bullet("关闭程序时，未保存的编辑图层会逐层询问保存、放弃或取消。")
        self.fig("73_编辑_删除后.png", "删除要素后可撤销", width_cm=14.2)
        self.fig("74_编辑_撤销后.png", "撤销后的状态", width_cm=14.2)

        self.h2("6.8 编辑设置")
        self.p(
            "「编辑」页签最右侧「编辑设置」打开设置对话框，包含「常规」和「快捷键」两个页签。"
            "常规页可设置选择集是否自动弹出、显示位置、长度/面积默认计算方法、"
            "默认长度/面积单位、滚轮缩放方向等；快捷键页可查看和修改编辑工具快捷键。"
        )
        self.fig("37_编辑设置.png", "编辑设置对话框", width_cm=12.2)
        self.p("点击「保存」后设置立即生效；「恢复默认」可把当前页恢复为默认值。")

    def ch7(self) -> None:
        self.chapter("7", "质量检查")
        self.p(
            "质量检查用于发现面数据中的几何和逻辑问题。平台提供七类检查，"
            "检查结果会生成新图层并定位到问题要素，便于逐条核实和修正。"
        )

        self.h2("7.1 质检面板与通用流程")
        self.p(
            "「质检」页签 →「质检」打开右侧「质检」面板。面板按检查类型列出七项，"
            "点击任一检查打开对应参数对话框。"
        )
        self.fig("04_质检面板.png", "质检面板", width_cm=7.2)
        self.p("七类检查的对话框结构基本一致，通常包含：")
        self.bullet("「输入图层」：选择要检查的面图层。")
        self.bullet("「过滤条件」：可选，只检查满足表达式的要素；点「配置」可用表达式构建器。")
        self.bullet("「输出图层」：留空则生成临时图层；也可指定落盘路径。")
        self.bullet("右侧「参考提示」：显示算法说明和示意图。")
        self.bullet("底部进度条和状态文字：显示检查进度。")
        self.bullet("「取消」「确认」：开始或取消检查。")
        self.p(
            "检查完成后，结果图层加入工程，源图层中对应问题要素被选中，并自动打开属性表，"
            "便于逐条核对。输出图层默认名称如「狭长面结果」「细碎面结果」。"
        )

        self.h2("7.2 七类检查")
        self.table(
            ["检查项", "判定规则与默认参数"],
            [
                ["狭长面", "对面要素求定向最小外包矩形，长边/短边超过「长宽比」阈值即判为狭长面。默认长宽比 10。"],
                ["细碎面", "面积小于「最小面积」（默认 20 平方米），或周长/面积比大于「周长面积比」（默认 10 米/平方米），或开启多部件检查且为多部件，命中任一即判为细碎面。"],
                ["面几何角检查", "检查面内夹角小于阈值（默认 30°）的顶点，用于发现尖角、毛刺。"],
                ["面自相交", "检查面几何自身相交（如环交叉、8 字形），无数值阈值。"],
                ["跨边界面不相交", "检查输入图层与参考图层边界是否相交/包含，用于发现跨边界图斑；无数值阈值。"],
                ["面多部件检查", "检查面要素是否由多个部件组成，无数值阈值。"],
                ["面重叠", "检查同一图层内要素之间是否重叠，无数值阈值。"],
            ],
            widths=[3.0, 12.4],
            font_size=9.2,
            first_col_bold=True,
            caption="七类质量检查",
        )

        self.h3("7.2.1 狭长面")
        self.p(
            "用于发现细长、带状的面要素。参数包括输入图层、过滤条件、输出图层和「长宽比」。"
            "长宽比 = 定向最小外包矩形长边 / 短边，默认 10；值越小越严格。"
        )
        self.fig("41_质检_狭长面.png", "狭长面检查对话框", width_cm=13.0)

        self.h3("7.2.2 细碎面")
        self.p(
            "用于发现面积过小或形状过于破碎的面。可选择「几何面积」或「属性面积」；"
            "选择属性面积时需指定面积字段。默认最小面积 20 平方米、周长面积比 10 米/平方米，"
            "可勾选「多部件检查」。几何面积和周长在等积投影下计算，结果与面积统计口径一致。"
        )
        self.fig("42_质检_细碎面.png", "细碎面检查对话框", width_cm=13.0)

        self.h3("7.2.3 面几何角检查")
        self.p(
            "用于发现面内夹角过小的尖角。默认角度阈值 30°，可调整。"
            "检查结果包含问题顶点位置，便于在节点编辑中修整。"
        )
        self.fig("43_质检_面几何角.png", "面几何角检查对话框", width_cm=13.0)

        self.h3("7.2.4 面自相交")
        self.p(
            "用于发现面几何自身相交的问题。该检查没有数值阈值，参数为输入图层、过滤条件和输出图层。"
            "结果图层中选中的要素需要借助节点编辑或重建几何修正。"
        )
        self.fig("44_质检_面自相交.png", "面自相交检查对话框", width_cm=13.0)

        self.h3("7.2.5 跨边界面不相交")
        self.p(
            "用于检查输入图层与参考图层之间的边界关系，常用于发现跨行政区、跨项目边界的图斑。"
            "除输入图层和过滤条件外，还需要选择「参考图层」及其过滤条件。"
        )
        self.fig("45_质检_跨边界.png", "跨边界面不相交检查对话框", width_cm=13.0)

        self.h3("7.2.6 面多部件检查")
        self.p(
            "用于发现一个面要素由多个分离部件组成的情况。该检查没有数值阈值，"
            "结果可配合「分解」工具拆分为单部件。"
        )
        self.fig("46_质检_多部件.png", "面多部件检查对话框", width_cm=13.0)

        self.h3("7.2.7 面重叠")
        self.p(
            "用于检查同一图层内要素之间的重叠。该检查没有数值阈值；"
            "结果图层中会标出重叠的要素，可结合属性表和选择集逐条处理。"
        )
        self.fig("47_质检_面重叠.png", "面重叠检查对话框", width_cm=13.0)

        self.h2("7.3 结果处理建议")
        self.bullet("先按问题类型批量筛选，再逐条核实；不要直接删除所有命中要素。")
        self.bullet("狭长面、尖角：优先用节点编辑调整节点，必要时重建几何。")
        self.bullet("细碎面：确认是否为独立图斑；可合并到相邻图斑或删除。")
        self.bullet("自相交、多部件：可先用「分解」「节点编辑」修正，再复检。")
        self.bullet("跨边界、重叠：结合参考图层和业务规则判断归属，必要时调整边界。")
        self.bullet("修正后重新执行同一检查，确认问题数量下降。")
        self.p(
            "面之间的缺口可在「编辑」页签使用「补缝」工具处理。补缝结果表包含"
            "「序号」「缺口面积(㎡)」「合并到（目标多边形）」，可按面积和相邻关系选择目标。"
        )

    def ch8(self) -> None:
        self.chapter("8", "空间分析与数据处理")
        self.p(
            "平台把常用的矢量分析、GDAL 栅格/矢量工具集中在「分析工具」面板，"
            "并另外提供导出、SQL 和样式管理入口。"
        )

        self.h2("8.1 分析工具面板")
        self.p(
            "「通用」→「分析」打开右侧「分析工具」面板。顶部搜索框可按名称过滤；"
            "分组包括「矢量分析」「切片导出」「流程编排」以及 GDAL 栅格分析、地形、"
            "数据源、元数据、矢量分析、数据导出等。单击工具打开参数对话框。"
        )
        self.fig("05_分析工具面板.png", "分析工具面板", width_cm=7.4)
        self.p(
            "大多数工具对话框右侧带有「帮助说明」，解释算法用途和参数。"
            "输出图层可留空生成临时图层，也可指定文件路径落盘；执行完成后结果通常自动加入工程。"
        )

        self.h2("8.2 矢量分析")
        self.table(
            ["工具", "用途"],
            [
                ["矢量缓冲区", "按固定或动态距离生成缓冲区。"],
                ["最小外包矩形", "生成要素的最小外接矩形。"],
                ["叠加分析", "相交、并集、差集等图层叠加。"],
                ["最短路径分析", "在路网图层上计算两点间最短路径。"],
                ["坐标转换", "把图层重投影到目标坐标系。"],
                ["格式转换", "在 Shapefile、GeoPackage、GeoJSON 等格式之间转换。"],
            ],
            widths=[3.4, 12.0],
            font_size=9.4,
            first_col_bold=True,
            caption="内置矢量分析工具",
        )
        self.fig("63_工具_缓冲区.png", "矢量缓冲区", width_cm=13.2)
        self.fig("64_工具_叠加分析.png", "叠加分析", width_cm=13.2)
        self.fig("65_工具_最短路径.png", "最短路径分析", width_cm=13.2)
        self.fig("21_坐标转换.png", "坐标转换", width_cm=13.2)
        self.fig("22_格式转换.png", "格式转换", width_cm=13.2)

        self.h3("8.2.1 矢量缓冲区")
        self.p(
            "选择输入图层，设置缓冲距离、圆弧分段数、端点样式和连接样式，"
            "可选择是否融合重叠缓冲区。输出图层留空时生成临时图层。"
            "距离单位随图层坐标系变化，使用投影坐标系时单位为米。"
        )

        self.h3("8.2.2 坐标转换与格式转换")
        self.p(
            "坐标转换用于把图层从源坐标系转换到目标坐标系，常用于 CGCS2000、WGS84、"
            "地方投影之间的转换。格式转换用于输出不同文件格式，转换时可选择字段和目标路径。"
        )
        self.note(
            "坐标转换前请确认源坐标系正确。源坐标系错误时，转换结果会产生系统性偏移，"
            "且无法通过再次转换自动修复。",
            label="注意",
        )

        self.h2("8.3 GDAL 栅格与矢量工具")
        self.p(
            "GDAL 分组覆盖常用的栅格处理：栅格计算器、按范围/掩膜裁剪、重投影、格式转换、"
            "拼接、重排波段、PCT/RGB 转换、COG、虚拟栅格、栅格矢量化、筛除小图斑、"
            "矢栅转换、填充 NoData、去黑边；地形工具包括坡向、坡度、山体阴影、粗糙度、"
            "TPI、TRI、距离栅格、彩色渲染、等高线、可视域；此外还有金字塔、重切片、"
            "gdal2tiles、栅格信息、Grid 插值等。"
        )
        self.fig("66_工具_栅格计算器.png", "栅格计算器", width_cm=13.2)
        self.fig("67_工具_按掩膜裁剪.png", "按掩膜裁剪栅格", width_cm=13.2)
        self.fig("68_工具_山体阴影.png", "山体阴影", width_cm=13.2)
        self.p(
            "GDAL 工具的参数与 QGIS/processing 保持一致；输出路径、NoData、分辨率、"
            "重采样方式等按实际需求设置。栅格数据量较大时，处理时间和磁盘占用会明显增加。"
        )

        self.h2("8.4 导出图层")
        self.p(
            "图层右键「导出数据」或「导出图层」打开导出对话框。支持 shp、gdb、gpkg、sqlite、"
            "dxf、kml、xlsx、geojson 等格式。"
        )
        self.fig("19_导出图层.png", "导出图层对话框", width_cm=13.4)
        self.bullet("「导出字段」：勾选需要输出的字段，可全选/全不选。")
        self.bullet("「为导出名称使用别名」：输出时使用字段别名。")
        self.bullet("「转换字典」：把字典代码转换为显示值。")
        self.bullet("「筛选数据」：导出全部、选中数据或自定义条件。")
        self.bullet("「导出后加载到地图」：导出完成后自动加入工程。")
        self.p("点击「确定」后选择目标路径，完成后消息条提示导出结果。")

        self.h2("8.5 执行 SQL")
        self.p(
            "图层右键「执行SQL」打开「执行SQL」对话框。SQL 编辑区默认生成"
            "`select * from \"图层名\" limit 1000`，可修改后点击「执行查询」。"
            "查询结果在下方表格显示，可「加载为图层」继续使用。"
        )
        self.fig("16_执行SQL.png", "执行SQL对话框", width_cm=14.2)
        self.fig("78_执行SQL_结果.png", "SQL 查询结果", width_cm=14.2)
        self.bullet("仅支持本地文件源（如 GeoPackage、SQLite、Shapefile），不支持在线服务。")
        self.bullet("「过滤条件」可输入额外的 SQL 条件，或点击右侧按钮打开查询构建器。")
        self.bullet("「图层名称」留空时自动命名为“图层名_SQL结果”。")
        self.bullet("执行大查询前建议加 `limit`，避免一次性加载过多要素。")

        self.h2("8.6 样式管理")
        self.p(
            "图层右键 →「渲染样式」提供「编辑样式」「导入样式」「保存样式」「复制样式」"
            "「粘贴样式」。保存样式可把当前符号、分类、标注等设置存为样式文件，"
            "供其他图层复用。"
        )
        self.fig("25_保存样式.png", "保存样式对话框", width_cm=10.4)
        self.bullet("编辑样式：打开符号选择器或渲染设置。")
        self.bullet("导入样式：从文件加载样式。")
        self.bullet("保存样式：把当前样式写入文件。")
        self.bullet("复制/粘贴样式：在同一工程内的图层之间快速复用。")

    def ch9(self) -> None:
        self.chapter("9", "瓦片制作")
        self.p(
            "瓦片制作把矢量或影像数据切成按层级组织的地图瓦片，便于在浏览器、移动端或"
            "离线环境中快速展示。平台支持矢量瓦片（XYZ/PBF）、影像切片（XYZ 目录）"
            "和影像切片（MBTiles）三种输出。"
        )

        self.h2("9.1 三种输出对比")
        self.table(
            ["类型", "输出", "适用场景"],
            [
                ["矢量瓦片（XYZ）", "按 `{z}/{x}/{y}.pbf` 组织的目录", "Web 端矢量渲染、样式灵活、数据量小。"],
                ["影像切片（XYZ）", "按 `{z}/{x}/{y}.png|jpg` 组织的目录", "离线底图、静态影像发布、通用 XYZ 服务。"],
                ["影像切片（MBTiles）", "单个 `.mbtiles` 文件", "便于拷贝和分发，适合离线地图包。"],
            ],
            widths=[4.2, 5.4, 6.0],
            font_size=9.2,
            first_col_bold=True,
            caption="三种瓦片输出",
        )

        self.h2("9.2 矢量瓦片")
        self.p(
            "入口：「分析工具」→「切片导出」→「生成矢量瓦片（XYZ）」。"
            "对话框顶部为文件模板，默认 `{z}/{x}/{y}.pbf`。"
        )
        self.fig("13_创建矢量瓦片.png", "创建矢量瓦片对话框", width_cm=14.2)
        self.step("在「输入图层」中勾选一个或多个图层。")
        self.step("设置「最小等级」「最大等级」，默认 0～14；小范围数据建议最大等级不低于 14。")
        self.step("选择「输出范围」：「图层范围」按勾选图层合并范围，「当前画布范围」按当前视图。")
        self.step("选择「输出目录」，目录不存在时自动创建。")
        self.step("如需清空目录、生成网页查看器，点击「高级…」设置。")
        self.step("点击「执行」。达到较大瓦片数量时，软件会先估算并确认。")
        self.step("执行完成后可点击「打开网页查看器」，用浏览器预览瓦片。")
        self.note(
            "「批量处理…」会为每个勾选图层输出到独立子目录并顺序执行。"
            "默认最大等级 14 在大范围数据上可能产生数十万瓦片，执行前请留意估算提示。",
            label="提示",
        )

        self.h2("9.3 影像切片（XYZ 目录）")
        self.p(
            "入口：「分析工具」→「切片导出」→「影像切片（XYZ）」。"
            "该工具按画布渲染结果切片，输出标准 XYZ 目录。"
        )
        self.fig("14_影像切片XYZ.png", "影像切片（XYZ）对话框", width_cm=14.2)
        self.step("选择「输入影像图层」。")
        self.step("设置「缩放等级」最小/最大，默认 0～8。")
        self.step("选择「输出范围」：图层范围或当前画布范围。")
        self.step("设置「瓦片格式」（PNG/JPG）、「瓦片大小（px）」（默认 256）、是否启用抗锯齿。")
        self.step("可勾选「仅渲染所选图层（隐藏其他图层）」以加快速度。")
        self.step("可勾选「生成网页查看器（HTML）」，便于本地预览。")
        self.step("选择「输出目录」，点击「执行」。")
        self.p(
            "任务日志显示切片进度；切片在独立子进程中执行，界面不会卡死。"
            "切片过程中关闭程序会先弹窗确认是否终止任务。"
        )
        self.note(
            "缩放等级每增加一级，瓦片数量约增加 4 倍。大范围影像建议先确定合适的最低/最高层级，"
            "或按当前画布范围分块切片。",
            label="性能建议",
        )

        self.h2("9.4 影像切片（MBTiles）")
        self.p(
            "入口：「分析工具」→「切片导出」→「影像切片（MBTiles）」。"
            "参数与 XYZ 切片基本一致，区别在于输出为单个 `.mbtiles` 文件。"
        )
        self.fig("15_影像切片MBTiles.png", "影像切片（MBTiles）对话框", width_cm=14.2)
        self.bullet("输入影像图层、缩放等级、输出范围、格式、抗锯齿、仅渲染所选图层与 XYZ 切片相同。")
        self.bullet("「输出文件（MBTiles）」选择 `.mbtiles` 保存路径。")
        self.bullet("执行完成后可直接用支持 MBTiles 的 GIS 软件或平台加载。")
        self.bullet("MBTiles 便于拷贝和分发，但单文件体积会随层级和范围快速增大。")

        self.h2("9.5 查看器与发布")
        self.p(
            "矢量瓦片和影像切片均可勾选生成 HTML 查看器。执行完成后点击「打开网页查看器」，"
            "用默认浏览器预览结果。查看器适合本地核查切片是否完整、层级是否正确。"
        )
        self.p(
            "输出的 XYZ 目录可通过任意静态文件服务发布；发布时需保证目录结构与"
            "`{z}/{x}/{y}.png|jpg|pbf` 一致，并配置正确的跨域和缓存策略。"
        )
        self.note(
            "切片结果依赖输出时的坐标系与范围。发布到第三方平台前，请确认对方的"
            "瓦片坐标系（通常为 Web 墨卡托 EPSG:3857）和层级约定与输出一致。",
            label="注意",
        )

        self.h2("9.6 性能与磁盘建议")
        self.bullet("先用低层级和较小范围试切，确认效果后再扩大范围。")
        self.bullet("勾选「仅渲染所选图层」可避免重复渲染底图和无关图层。")
        self.bullet("JPG 适合影像底图，PNG 适合需要透明背景的图层。")
        self.bullet("切片输出目录应预留充足磁盘空间；MBTiles 单文件可能达到数 GB。")
        self.bullet("切片任务在子进程执行，期间可继续浏览地图，但不要重复启动同一任务。")

    def ch10(self) -> None:
        self.chapter("10", "流程编排")
        self.p(
            "流程编排把数据源、矢量分析、栅格分析和处理算法组织成节点图，"
            "一次配置、重复运行，适合固定步骤的数据处理任务。"
        )

        self.h2("10.1 打开与界面")
        self.p(
            "「查看」→「流程编排」（Ctrl+Shift+F），或「分析工具」→「流程编排」→"
            "「节点式流程编排」，打开「流程节点编辑器」。"
        )
        self.fig("29_流程编排.png", "流程节点编辑器", width_cm=14.6)
        self.table(
            ["区域", "说明"],
            [
                ["顶部工具栏", "运行全部、单步执行、重置参数、拓扑校验、高亮依赖链、新建、打开、保存、另存为、导入/导出 JSON 等。"],
                ["节点库", "按数据源、矢量分析、栅格分析、通用、处理算法分组；支持搜索、展开/折叠。"],
                ["画布", "拖入节点、连线、选中和移动节点。"],
                ["节点参数", "选中节点后显示输入、输出和执行参数。"],
                ["运行日志", "显示运行进度、输出和错误信息。"],
            ],
            widths=[3.2, 12.2],
            font_size=9.3,
            first_col_bold=True,
            caption="流程节点编辑器界面",
        )

        self.h2("10.2 新建流程")
        self.step("从左侧节点库展开分组，把需要的节点拖到画布。")
        self.step("从上游节点的输出端口拖线到下游节点的输入端口，建立依赖关系。")
        self.step("选中节点，在右侧「节点参数」中设置图层、字段、阈值和输出路径。")
        self.step("重复上述步骤，直到流程完整。")
        self.step("点击「拓扑校验」检查是否有孤立节点、缺失输入或循环依赖。")
        self.step("点击「运行全部」执行流程；也可用「单步执行」逐个调试。")

        self.h2("10.3 节点与参数")
        self.p(
            "节点库分组包括「数据源」（加载矢量图层、加载栅格图层、常量数值）、"
            "「矢量分析」（缓冲区、裁剪、投影转换、字段计算、属性过滤、相交、并集、差集、对称差等）、"
            "「栅格分析」以及「通用」「处理算法」等。不同节点的参数由算法定义自动生成。"
        )
        self.bullet("节点输入端口必须连接有效输出，否则运行前校验会报错。")
        self.bullet("输出节点可指定落盘路径，也可保留为临时结果。")
        self.bullet("参数修改后需重新运行才能生效。")

        self.h2("10.4 运行、日志与保存")
        self.p(
            "点击「运行全部」后，流程按拓扑顺序执行，底部「运行日志」显示每一步的状态和输出。"
            "运行前可能弹出运行参数对话框，用于确认输出目录或覆盖策略。"
        )
        self.fig("29_流程编排.png", "流程运行时查看日志", width_cm=14.2)
        self.bullet("「单步执行」：只运行当前节点，便于定位问题。")
        self.bullet("「重置参数」：把当前节点参数恢复为默认值。")
        self.bullet("「高亮依赖链」：高亮当前节点的上下游依赖。")
        self.bullet("「保存」/「另存为…」：把流程保存为文件，便于复用和分享。")
        self.bullet("「打开…」：加载已有流程。")
        self.bullet("JSON 导入/导出：便于在不同环境之间迁移流程定义。")

        self.note(
            "流程文件中保存的是节点、连线和参数，不包含实际数据。迁移到其他电脑时，"
            "需同步准备输入数据并检查路径。",
            label="提示",
        )

    def ch11(self) -> None:
        self.chapter("11", "系统设置与个性化")
        self.p("本章介绍主题、快捷键、日志、坐标系和帮助入口等系统级设置。")

        self.h2("11.1 主题切换")
        self.p(
            "点击标题栏的调色盘按钮打开主题菜单，可选择「浅色」「深色」「蓝色」「绿色」。"
            "当前主题带勾选标记，切换后立即生效。"
        )
        self.fig("39_主题_深色.png", "深色主题示例", width_cm=14.6)

        self.h2("11.2 快捷键")
        self.p(
            "系统快捷键在 `app/infra/shortcuts.py` 中登记，可在「编辑设置」→「快捷键」页查看和修改。"
            "用户修改的值保存在 `config/shortcuts.yaml`，只记录与默认不同的项；删除该文件即恢复默认。"
        )
        self.table(
            ["功能", "默认快捷键", "说明"],
            [
                ["撤销", "Ctrl+Z", "包括绘制时的节点撤销。"],
                ["重做", "Ctrl+Y", "包括绘制时的节点重做。"],
                ["新建工程", "Ctrl+N", "清空当前工程并新建。"],
                ["保存工程", "Ctrl+S", "保存当前工程。"],
                ["另存为工程", "Ctrl+Shift+S", "另选路径保存。"],
                ["打开工程", "Ctrl+Shift+O", "打开地图文档对话框。"],
                ["打开文件", "Ctrl+O", "打开添加数据对话框。"],
                ["退出", "Ctrl+Q", "关闭程序。"],
                ["日志面板", "Ctrl+L", "打开底部日志面板。"],
                ["流程节点编辑器", "Ctrl+Shift+F", "打开流程编排。"],
                ["捕捉", "S", "切换磁性捕捉。"],
                ["追踪", "T", "切换追踪绘制。"],
                ["显隐首个影像图层", "Space", "切换第一个影像图层可见性。"],
                ["追加选择", "Ctrl + 点击/拖框", "点选/框选时追加。"],
                ["移除选择", "Shift + 点击/拖框", "点选/框选时移除。"],
            ],
            widths=[4.4, 4.4, 7.0],
            font_size=9.1,
            first_col_bold=True,
            caption="默认快捷键",
        )

        self.h2("11.3 标题栏与用户信息")
        self.p(
            "标题栏显示当前登录用户。点击用户区可查看「账号」「姓名」「单位」「区域」"
            "「手机」「上次登录」「会话有效期」等信息；这些内容为只读，不提供退出登录入口。"
            "会话过期时显示“已过期”，服务端要求重置密码时会显示提示。"
        )
        self.fig("40_用户菜单.png", "用户信息菜单", width_cm=5.0)
        self.p(
            "「任务模式」和「工程模式」用于标识当前工作模式。当前版本切换时只记录日志，"
            "不改变界面布局和功能。"
        )

        self.h2("11.4 日志与诊断")
        self.p(
            "日志面板默认显示当前会话。可按文本、时间范围、级别和行数筛选；"
            "支持清空、刷新、保存日志和导出诊断包。反馈问题时，附上诊断包有助于定位。"
        )
        self.fig("38_日志面板.png", "日志信息面板", width_cm=14.6)

        self.h2("11.5 工程坐标系")
        self.p(
            "状态栏右侧显示当前工程坐标系，点击可打开坐标系选择对话框。"
            "选择新坐标系后，工程和画布同时切换，消息条提示“工程坐标系已切换为 …”。"
            "坐标系影响面积、长度计算和坐标显示，开始作业前应确认与数据一致。"
        )
        self.note(
            "切换工程坐标系只改变显示和计算基准，不会自动重投影已有数据。"
            "需要转换数据坐标系时，请使用「坐标转换」工具。",
            label="注意",
        )

        self.h2("11.6 帮助与反馈")
        self.p(
            "「帮助」页签提供「使用手册」「问题反馈」「关于」「检查更新」。"
            "「使用手册」优先打开 `app.help.manual_url` 配置的在线地址；未配置时打开 "
            "`app.help.manual_path` 指向的本地 Word 文档，默认 `docs/使用手册.docx`。"
            "文件不存在时，软件会提示实际查找路径。"
        )
        self.fig("28_关于.png", "关于对话框", width_cm=9.0)
        self.p(
            "「问题反馈」会用系统浏览器打开在线反馈页，并带上当前用户信息；"
            "「检查更新」立即检查新版本并反馈结果。"
        )

    def ch12(self) -> None:
        self.chapter("12", "常见问题")
        self.p("本章汇总使用过程中常见的问题和处理方法。若仍无法解决，请通过「问题反馈」提交日志和诊断包。")

        qa = [
            ("登录失败或提示账号密码错误",
             "确认账号、密码和网络连接；确认授权服务器地址配置正确。"
             "账号由服务端校验，本地无法重置密码；如提示会话过期或要求重置密码，请联系管理员。"),
            ("「帮助 → 使用手册」提示找不到文件",
             "确认 `docs/使用手册.docx` 是否存在。开发模式相对项目根目录解析；"
             "发行版相对便携包根目录解析。也可在 `config/app.yaml` 的 `app.help.manual_path` "
             "填写绝对路径，或配置 `app.help.manual_url` 使用在线文档。"),
            ("图层名称前出现红色感叹号",
             "表示数据源不存在或路径失效。点击红色感叹号，按提示重新指定数据源；"
             "移动工程或数据后容易出现，建议保持数据目录结构不变。"),
            ("添加数据时看不到 GDB 或数据库中的图层",
             "GDB、GeoPackage、SQLite 属于容器，需要双击展开后选择内部图层。"
             "GDB 还需确认是有效的文件地理数据库目录。"),
            ("属性表为空或只显示部分要素",
             "检查底部模式是否为「仅选中要素」或「仅指定要素」；"
             "切换到「全部要素」并点击「加载更多/加载全部」。数据量较大时分页加载属正常现象。"),
            ("质检没有发现结果",
             "确认输入图层为面图层、过滤条件没有排除全部要素、阈值设置合理。"
             "若图层本身没有对应问题，结果为空；可先用示例数据验证参数。"),
            ("质检结果和预期不一致",
             "检查工程坐标系和面积/长度计算口径。细碎面的面积和周长在等积投影下计算；"
             "坐标系错误会导致面积偏差。阈值越小越严格。"),
            ("编辑后关闭程序提示保存",
             "编辑内容先写入编辑缓冲区，需「保存编辑」或停止编辑时选择保存。"
             "关闭程序会逐层询问，选择“取消”可返回继续处理。"),
            ("切片速度慢或结果空白",
             "降低最大层级、缩小输出范围、勾选「仅渲染所选图层」；"
             "确认输入图层有有效数据且可见。矢量瓦片层级过低时小范围数据可能没有要素。"),
            ("切片过程中关闭程序",
             "软件会先询问是否终止切片并退出。选择取消可继续等待任务完成。"),
            ("发布瓦片后地图偏移",
             "确认瓦片输出坐标系与发布平台一致（通常为 EPSG:3857），"
             "并检查 `{z}/{x}/{y}` 目录结构和层级约定。"),
            ("离线下载结果与界面文案不一致",
             "界面中的“压缩包/zip”为历史文案，当前版本实际输出 GeoPackage 等数据文件，"
             "请以下载目录中的实际文件为准。"),
            ("服务器数据库页签看不到",
             "服务器数据库功能在部分版本中默认隐藏或尚未接入目录浏览，"
             "请使用本地 SQLite/GeoPackage 连接，或联系管理员确认版本能力。"),
            ("如何恢复默认快捷键",
             "删除 `config/shortcuts.yaml`，或在「编辑设置」→「快捷键」中逐项恢复默认。"),
            ("日志太大或需要反馈问题",
             "在日志面板中筛选当前会话，使用「导出诊断包」生成压缩包，"
             "连同问题描述、操作步骤和截图一起提交。"),
        ]
        for idx, (question, answer) in enumerate(qa, start=1):
            self.h3(f"12.{idx} {question}")
            self.p(answer)

        self.note(
            "提交问题时，请尽量说明软件版本、操作系统、数据格式、操作步骤和完整报错信息。"
            "涉及数据安全的内容请先脱敏。",
            label="反馈建议",
        )

    def appendix(self) -> None:
        self.chapter("A", "快捷键与常用路径")
        self.h2("A.1 默认快捷键")
        self.p("完整快捷键表见第 11.2 节。下表为最常用的几项：")
        self.table(
            ["快捷键", "功能"],
            [
                ["Ctrl+N", "新建工程"],
                ["Ctrl+S", "保存工程"],
                ["Ctrl+Shift+S", "另存为工程"],
                ["Ctrl+Shift+O", "打开工程"],
                ["Ctrl+O", "打开文件（添加数据）"],
                ["Ctrl+Q", "退出"],
                ["Ctrl+Z / Ctrl+Y", "撤销 / 重做"],
                ["Ctrl+L", "日志面板"],
                ["Ctrl+Shift+F", "流程节点编辑器"],
                ["S / T", "捕捉 / 追踪"],
                ["Space", "显隐首个影像图层"],
                ["Esc", "取消当前工具 / 清除测量"],
                ["Backspace", "撤销上一个测量落点"],
                ["Delete", "删除选中要素（编辑态）"],
            ],
            widths=[5.0, 10.4],
            font_size=9.4,
            first_col_bold=True,
            caption="常用快捷键",
        )

        self.h2("A.2 常用路径")
        self.table(
            ["路径", "说明"],
            [
                ["`config/app.yaml`", "主配置：应用名称、版本、坐标系、帮助文档、底图、更新等。"],
                ["`config/online_basemap_config.yaml`", "在线底图清单。"],
                ["`config/shortcuts.yaml`", "用户自定义快捷键（只存与默认不同的项）。"],
                ["`docs/使用手册.docx`", "本手册默认路径。"],
                ["`docs/manual_screenshots/`", "手册截图源文件。"],
                ["`logs/`", "运行日志。"],
                ["`.tmp/`", "临时文件与测试数据。"],
            ],
            widths=[6.4, 9.0],
            font_size=9.4,
            first_col_bold=True,
            caption="常用文件与目录",
        )

        self.h2("A.3 数据格式速查")
        self.table(
            ["类别", "格式", "说明"],
            [
                ["矢量", "SHP、GeoJSON、KML、DXF、GPKG、SQLite、GDB", "可编辑、查询、质检、导出。"],
                ["栅格", "GeoTIFF、MBTiles 等", "可浏览、切片、GDAL 分析。"],
                ["表格", "XLSX、XLS、CSV", "可加载属性、建库、字段模板。"],
                ["工程", "PGS", "保存图层、样式、坐标系和视图。"],
                ["瓦片", "PBF、PNG、JPG、MBTiles", "切片输出与离线发布。"],
            ],
            widths=[2.6, 7.2, 5.6],
            font_size=9.2,
            first_col_bold=True,
            caption="数据格式速查",
        )

        self.h2("A.4 获取帮助")
        self.bullet("软件内：「帮助」→「使用手册」「问题反馈」「关于」「检查更新」。")
        self.bullet("反馈时附上版本号、操作系统、操作步骤、截图和诊断包。")
        self.bullet("本手册默认随发行包分发；升级后请以新版本手册为准。")

    # ------------------------------------------------------------ 生成

    def _render_body(self) -> None:
        self.ch1()
        self.ch2()
        self.ch3()
        self.ch4()
        self.ch5()
        self.ch6()
        self.ch7()
        self.ch8()
        self.ch9()
        self.ch10()
        self.ch11()
        self.ch12()
        self.appendix()

    def build(self) -> Path:
        # 第一遍：只渲染正文，收集一、二级标题，作为静态目录条目。
        # （静态目录在 WPS/Word 中都能直接显示，不依赖“更新域”。）
        self._render_body()
        entries = list(self.toc_entries)
        ENTRY_DATA = [
            {"level": level, "title": title} for level, title in entries
        ]
        ENTRIES_JSON.parent.mkdir(parents=True, exist_ok=True)
        ENTRIES_JSON.write_text(
            json.dumps(ENTRY_DATA, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 第二遍：正式排版（封面 / 前言 / 静态目录 / 正文）。
        self._new_document()
        self.toc_entries = []
        self.doc.core_properties.title = f"{APP_NAME}使用手册"
        self.doc.core_properties.subject = "用户使用手册"
        self.doc.core_properties.author = APP_NAME
        self.doc.core_properties.keywords = "GIS, 图斑核实, 使用手册"
        self.doc.core_properties.comments = f"适用版本 {APP_VERSION}"

        self.cover()
        self.foreword()
        self.toc(entries)
        self._render_body()

        OUT.parent.mkdir(parents=True, exist_ok=True)
        self.doc.save(str(OUT))
        return OUT


def _load_pages(path: Path) -> dict[str, int]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        print(f"警告：读取目录页码失败，忽略：{path}（{exc}）")
        return {}
    if isinstance(raw, dict):
        return {str(k): int(v) for k, v in raw.items() if str(v).isdigit()}
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="生成图斑核实平台使用手册")
    parser.add_argument(
        "--toc-pages",
        default=str(PAGES_JSON),
        help="目录页码 JSON（默认 docs/manual_toc_pages.json，不存在时显示“—”）",
    )
    args = parser.parse_args()

    print(f"截图目录：{IMG}")
    if not IMG.is_dir():
        print("错误：截图目录不存在，请先运行 capture_screenshots.py")
        return 1
    pages = _load_pages(Path(args.toc_pages))
    if pages:
        print(f"目录页码：{len(pages)} 条（{args.toc_pages}）")
    else:
        print("目录页码：未提供，目录页码显示为“—”")

    builder = ManualBuilder(toc_pages=pages)
    out = builder.build()
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"已生成：{out}（{size_mb:.1f} MB）")
    print(f"截图 {builder.fig_total} 张，表格 {builder.table_total} 个")
    print(f"目录条目：{ENTRIES_JSON}")
    if MISSING_IMAGES:
        print("缺少截图：")
        for name in MISSING_IMAGES:
            print(f"  - {name}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
