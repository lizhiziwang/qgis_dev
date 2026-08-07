# 2026-08-07 PyInstaller 打包修复（瓦片/缓冲工具可用）

## 做了什么

让 PyInstaller 打包的 `dist/app` 里的 `processing.run` 真正可用（瓦片/缓冲工具）。修复链条：

1. **`build_windows.ps1`**：
   - 文件转 **UTF-8 带 BOM**（PowerShell 5.1 无 BOM 按 GBK 读，中文注释误读成 `"`/`}` 报假语法错误）。
   - processing 插件复制改用 **robocopy**（PS 5.1 `Copy-Item -Recurse` 到非空目录会报错中止），带 `/R:3 /W:1` 重试 + `$LASTEXITCODE -ge 8` 失败判断。
   - 新增 STAGE 4 关键产物校验（缺 `app.exe`/`plugins/processing`/`launch_qgis.bat`/`qgis`/`proj.db`/`etc`/`i18n`/`qgis_analysis.dll` 直接报错退出）。
2. **`pyinstaller.spec`**：
   - `qgis_analysis.dll` 进 `binaries`（`qgis.analysis` ← `Processing.initialize()` 需要）。
   - `PyQt5.uic`、`psycopg2` 进 `hiddenimports`（processing 插件运行时依赖）。
   - **`excludes=["processing"]`**（关键修复，见下）。
3. **删除了项目根的杂散 `processing/` 目录**。

## 关键结果

- **根因**：项目根目录出现 `processing/`（QGIS processing 在 CWD 建的临时目录 `models/outputs/scripts`），spec 的 `pathex=[PROJECT_ROOT]` 让 PyInstaller 把它当 **namespace 包** 打进 PYZ（`('processing','-','PYMODULE')`）。冻结态下 FrozenImporter 优先于磁盘，`import processing` 命中空 namespace → `import processing.core` 报 `ModuleNotFoundError`。加了 `excludes=["processing"]` 后 PYZ 不再有该条目。
- **验证**：跑 `dist/app/app.exe`，`dist/app/logs/app.log` 无 "Processing 插件初始化失败" warning（`init_plugins()` 只在失败时打日志），`error.log` 为空，主窗口构建 OK、天地图底图加载 OK。
- 全程 3 次完整重建（约 10 分钟/次）验证 spec + 脚本永久修复。
- 产物：`dist/app/`（1.3G，5577 文件）+ `app.zip`（435.7 MB，testzip 通过）。

## 遗留事项

- `GDAL_DATA`/`GDAL_DRIVER_PATH` 指向不存在的 `dist/app/gdal_data`/`gdalplugins`（GDAL 容忍，瓦片/图层加载已验证；缓冲用 GEOS 不需要）。如需更稳可后续打包 `apps/gdal/share/gdal`。
- `.verify_dist_processing.py` 探针已删——dev python + dist 混合探针在 bootloader 外无法加载 dist 的 PyQt5（在 PYZ 里无 `__init__.py`），`QgsApplication` 构造必崩，验证以 app.exe 日志为准。
- 分发前建议确认目标机器解压 `app.zip` 后结构为 `app/app.exe` + `launch_qgis.bat`。

## 后续验证（同日追加）

- **用户确认打包结果可用 QGIS 算法了**（`processing.run("native:buffer", ...)` 在打包后的 `dist/app` 里真实执行成功）——修复链整体闭环。
- **`launch_qgis.bat` 位置**：打包后位于 `dist/app/launch_qgis.bat`（与 `app.exe` 同目录），分发 zip 内为 `app/launch_qgis.bat`；源文件 `tools/runtime/launch_qgis.bat`，由 `build_windows.ps1` STAGE 3 复制（脚本第 83 行）。
- 会话中确认过进程链：用户重跑完整 `build_windows.ps1`（PowerShell → `python-qgis-ltr.bat` → PyInstaller），STAGE 1 清空 dist/build 属正常流程，等 PyInstaller 跑完 STAGE 3 才会出现 launch_qgis.bat。
- **"之前为什么不行"的完整链条**（一次问答总结）：
  1. processing Python 插件（`apps/qgis-ltr/python/plugins/processing`）没被复制进 dist → `QgsNotSupportedException: Processing plugin has not been loaded`（stub run 未替换）。
  2. 复制后又被项目根杂散 `processing/` 目录的 namespace 包在 PYZ 里遮蔽 → `ModuleNotFoundError: No module named 'processing.core'`（FrozenImporter 优先于磁盘路径）。
  3. 依赖缺失：`qgis_analysis.dll`（binaries）、`PyQt5.uic`/`psycopg2`（hiddenimports，processing 运行时 import PyInstaller 静态分析不到）。
  4. 构建脚本本身坏：PS1 无 BOM 被 GBK 误读报假语法错；`Copy-Item -Recurse` 非空目录 bug。
