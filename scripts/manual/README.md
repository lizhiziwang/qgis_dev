# 使用手册生成工具

本目录用于生成 `docs/使用手册.docx`（软件「帮助 → 使用手册」打开的本地 Word 文档）。

## 文件

| 文件 | 作用 |
| --- | --- |
| `capture_screenshots.py` | 用 QGIS Python 启动真实主窗口，把界面截图写到 `docs/manual_screenshots/` |
| `build_manual.py` | 读取截图，生成 Word 手册（封面、前言、目录、12 章正文、附录） |
| `collect_toc_pages.py` | 从 PDF 中提取各级标题的实际页码，写 `docs/manual_toc_pages.json` |

## 重建流程

目录页码是静态写入的（不依赖 Word 打开时更新域），因此要跑两遍构建：

```bash
# 1. 生成正文（此时目录页码显示“—”）
python scripts/manual/build_manual.py

# 2. docx → pdf（Word / WPS 均可；也可用 WPS COM，见下）
#    产物路径需与下一步 --pdf 一致
python - <<'PY'
import win32com.client
app = win32com.client.Dispatch("Kwps.Application")
app.Visible = False
doc = app.Documents.Open(r"E:\2026\project\qgis\docs\使用手册.docx", ReadOnly=True)
doc.ExportAsFixedFormat(r"E:\tmp\manual_pdf\使用手册.pdf", 17)
doc.Close(False)
app.Quit()
PY

# 3. 提取页码（必须用 QGIS Python，依赖 QtPdf）
F:/app/qgis421/bin/python-qgis.bat scripts/manual/collect_toc_pages.py \
    --pdf E:/tmp/manual_pdf/使用手册.pdf

# 4. 再次生成，目录即带真实页码
python scripts/manual/build_manual.py
```

第 4 步后如需复核，可再转一次 PDF 确认页数未变（正文长度不变时页码稳定）。

截图重拍（界面改版后）：

```bash
F:/app/qgis421/bin/python-qgis.bat scripts/manual/capture_screenshots.py
```

脚本会在 `docs/manual_screenshots/` 覆盖同名 PNG；`build_manual.py` 里 `fig()` 引用的文件名需与之一致。

## 打包

`tools/build_windows.ps1` 与 `tools/build_linux.sh` 会把 `docs/使用手册.docx`
复制到产物根目录的 `docs/` 下。应用按 `config/app.yaml` 的
`app.help.manual_path`（默认 `docs/使用手册.docx`）相对项目根/便携包根解析，
找不到时弹窗提示路径。

若改为在线文档，把 `app.help.manual_url` 配成 URL 即可，非空时优先于本地文件。
