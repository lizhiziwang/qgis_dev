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
if (Test-Path (Join-Path $QGISInstall "apps/qgis-ltr/providers")) {
    Copy-Item -Path (Join-Path $QGISInstall "apps/qgis-ltr/providers") -Destination (Join-Path $DistPath "providers") -Recurse -Force
}
# 把启动器复制进产物，和目标机器一起分发
Copy-Item -Path (Join-Path $ProjectRoot "tools/runtime/launch_qgis.bat") -Destination (Join-Path $DistPath "launch_qgis.bat") -Force

Write-Host ""
Write-Host "=================================" -ForegroundColor Green
Write-Host "  B U I L D   C O M P L E T E ! " -ForegroundColor Green
Write-Host "=================================" -ForegroundColor Green
Write-Host "Portable app: $DistPath"
Write-Host "Run: $DistPath/$AppName.exe"
