# 统一 Windows 打包脚本
# 单一入口，所有路径从 config/app.yaml 读取
#
# 用法（在项目根目录）：
#   pwsh tools/build_windows.ps1

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path "$PSScriptRoot/.."
$CfgPath = Join-Path $ProjectRoot "config/app.yaml"

if (-not (Test-Path $CfgPath)) {
    Write-Error "config/app.yaml not found: $CfgPath"
    exit 1
}

# 从 yaml 读 qgis install_path（用 PowerShell 简易解析：找 qgis.install_path 行）
$cfgContent = Get-Content $CfgPath -Raw
if ($cfgContent -match 'install_path:\s*(.+)') {
    $QGISInstall = $Matches[1].Trim()
} else {
    Write-Error "config/app.yaml 缺少 qgis.install_path"
    exit 1
}

$PythonLauncher = Join-Path $QGISInstall "bin/python-qgis-ltr.bat"
if (-not (Test-Path $PythonLauncher)) {
    Write-Error "未找到 Python launcher: $PythonLauncher"
    exit 1
}

$AppName = "app"
$SpecFile = Join-Path $ProjectRoot "tools/pyinstaller.spec"
$DistPath = Join-Path $ProjectRoot "dist/$AppName"
$QgisPython = Join-Path $QGISInstall "apps/qgis-ltr/python"

# STAGE 1: 清理
Write-Host "STAGE 1: Cleaning..." -ForegroundColor Cyan
if (Test-Path (Join-Path $ProjectRoot "dist")) {
    Remove-Item (Join-Path $ProjectRoot "dist") -Recurse -Force
}
if (Test-Path (Join-Path $ProjectRoot "build")) {
    Remove-Item (Join-Path $ProjectRoot "build") -Recurse -Force
}

# STAGE 2: PyInstaller
Write-Host "STAGE 2: Running PyInstaller..." -ForegroundColor Cyan
& $PythonLauncher -m PyInstaller --noconfirm $SpecFile
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed"
    exit 1
}

# STAGE 3: 复制 QGIS runtime
Write-Host "STAGE 3: Copying QGIS runtime..." -ForegroundColor Cyan
if (-not (Test-Path $DistPath)) {
    Write-Error "dist folder not created"
    exit 1
}

Copy-Item -Path (Join-Path $QGISInstall "etc") -Destination (Join-Path $DistPath "etc") -Recurse -Force
Copy-Item -Path (Join-Path $QGISInstall "bin/python3*.dll") -Destination $DistPath -Force
Copy-Item -Path (Join-Path $QGISInstall "bin/o4w_env.bat") -Destination $DistPath -Force
Copy-Item -Path (Join-Path $QGISInstall "apps/qgis-ltr/plugins") -Destination (Join-Path $DistPath "plugins") -Recurse -Force
Copy-Item -Path (Join-Path $QGISInstall "apps/qgis-ltr/i18n") -Destination (Join-Path $DistPath "i18n") -Recurse -Force
# 把 Python 插件 processing 复制进 plugins 目录（C++ plugins 目录只有 DLL，
# processing 是 Python 插件，位于 apps/qgis-ltr/python/plugins/processing）。
# 不复制会导致 `processing.run` 始终是 stub，报
# "Processing plugin has not been loaded"（瓦片 / 缓冲工具不可用）。
# PowerShell 5.1 的 Copy-Item -Recurse 在目标目录非空时有已知 bug，改用 robocopy
# （/R:3 /W:1 自动重试锁文件；退出码 0-7 为成功，8+ 为失败）。
$processingSrc = Join-Path $QGISInstall "apps/qgis-ltr/python/plugins/processing"
$processingDst = Join-Path $DistPath "plugins/processing"
robocopy $processingSrc $processingDst /E /R:3 /W:1 /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
if ($LASTEXITCODE -ge 8) {
    Write-Host "ERROR: 复制 processing 插件失败（robocopy 退出码 $LASTEXITCODE）" -ForegroundColor Red
    exit 1
}
if (Test-Path (Join-Path $QGISInstall "apps/qgis-ltr/providers")) {
    Copy-Item -Path (Join-Path $QGISInstall "apps/qgis-ltr/providers") -Destination (Join-Path $DistPath "providers") -Recurse -Force
}
# 把启动器复制进产物，和目标机器一起分发
Copy-Item -Path (Join-Path $ProjectRoot "tools/runtime/launch_qgis.bat") -Destination (Join-Path $DistPath "launch_qgis.bat") -Force

# STAGE 4: 校验关键产物，缺了直接报错退出，避免分发不完整包
Write-Host "STAGE 4: Verifying build output..." -ForegroundColor Cyan
$missingFiles = @()
foreach ($required in @("app.exe", "plugins/processing", "launch_qgis.bat", "qgis", "proj.db", "etc", "i18n", "qgis_analysis.dll")) {
    if (-not (Test-Path (Join-Path $DistPath $required))) {
        $missingFiles += $required
    }
}
if ($missingFiles.Count -gt 0) {
    Write-Host "ERROR: 以下关键文件缺失，打包不完整：$($missingFiles -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "OK: 关键文件齐全" -ForegroundColor Green

Write-Host ""
Write-Host "=================================" -ForegroundColor Green
Write-Host "  B U I L D   C O M P L E T E ! " -ForegroundColor Green
Write-Host "=================================" -ForegroundColor Green
Write-Host "Portable app: $DistPath"
Write-Host "Run: $DistPath/$AppName.exe"
