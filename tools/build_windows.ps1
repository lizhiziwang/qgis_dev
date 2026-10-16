# 统一 Windows 打包脚本
# 单一入口，所有路径从 config/app.yaml 读取
#
# 用法（在项目根目录）：
#   pwsh tools/build_windows.ps1              # 默认开启 Cython 原生编译保护
#   pwsh tools/build_windows.ps1 -NoProtect   # 关闭保护（快速调试构建）

param(
    [switch]$NoProtect
)

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

# install_path 支持 bash 风格 ${VAR} / ${VAR:-默认} 环境变量引用（语义与
# app/env/config.py 的 _expand_env 一致，避免同一份 yaml 在 Win/麒麟 上维护两处）。
# 必须在 Join-Path 前展开，否则字面量 "${QGIS_INSTALL_PATH:-...}" 会把
# ${QGIS_INSTALL_PATH 误当盘符，报 DriveNotFoundException。
function Expand-EnvRef {
    param([string]$Value)
    # 去掉 YAML 行内注释（" # " 后内容）；路径本身不会包含该分隔
    $Value = ($Value -split '\s+#')[0].Trim()
    $pattern = '\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}'
    $evaluator = {
        param($m)
        $var = $m.Groups[1].Value
        $hasDefault = $m.Groups[2].Success
        $default = $m.Groups[2].Value
        $v = [Environment]::GetEnvironmentVariable($var)
        if ($v) { return $v }
        if ($hasDefault) { return $default }
        return ''
    }
    return [regex]::Replace($Value, $pattern, $evaluator)
}
$QGISInstall = Expand-EnvRef $QGISInstall

$PythonLauncher = Join-Path $QGISInstall "bin/python-qgis.bat"
if (-not (Test-Path $PythonLauncher)) {
    Write-Error "未找到 Python launcher: $PythonLauncher"
    exit 1
}

$AppName = "app"
$SpecFile = Join-Path $ProjectRoot "tools/pyinstaller.spec"
$DistPath = Join-Path $ProjectRoot "dist/$AppName"
$QgisPython = Join-Path $QGISInstall "apps/qgis/python"

# STAGE 1: 清理
Write-Host "STAGE 1: Cleaning..." -ForegroundColor Cyan
if (Test-Path (Join-Path $ProjectRoot "dist")) {
    Remove-Item (Join-Path $ProjectRoot "dist") -Recurse -Force
}
if (Test-Path (Join-Path $ProjectRoot "build")) {
    Remove-Item (Join-Path $ProjectRoot "build") -Recurse -Force
}

# STAGE 2: Cython 原生编译保护（.py -> .pyd，产物里不留字节码）
Write-Host "STAGE 2: Cython native protection..." -ForegroundColor Cyan
$ProtectDir = Join-Path $ProjectRoot "build/protect"
$ProtectManifest = Join-Path $ProtectDir "manifest.json"
$env:PYTHONUTF8 = "1"   # 保护工具输出 UTF-8，避免中文日志乱码
if ($NoProtect) {
    Write-Host "  -NoProtect: skipped" -ForegroundColor Yellow
    Remove-Item Env:QGIS_PROTECT_MANIFEST -ErrorAction SilentlyContinue
} else {
    & $PythonLauncher (Join-Path $ProjectRoot "tools/protect_cython.py") `
        --project-root $ProjectRoot `
        --config (Join-Path $ProjectRoot "config/app.yaml") `
        --out $ProtectDir `
        --manifest $ProtectManifest `
        --clean
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Cython 原生编译保护失败"
        exit 1
    }
    if (Test-Path $ProtectManifest) {
        $env:QGIS_PROTECT_MANIFEST = $ProtectManifest
        Write-Host "  保护清单: $ProtectManifest" -ForegroundColor Green
    } else {
        Write-Host "  未生成 manifest，PyInstaller 将不做原生保护" -ForegroundColor Yellow
    }
}

# STAGE 3: PyInstaller
Write-Host "STAGE 3: Running PyInstaller..." -ForegroundColor Cyan
& $PythonLauncher -m PyInstaller --noconfirm $SpecFile
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed"
    exit 1
}

# STAGE 4: 复制 QGIS runtime
Write-Host "STAGE 4: Copying QGIS runtime..." -ForegroundColor Cyan
if (-not (Test-Path $DistPath)) {
    Write-Error "dist folder not created"
    exit 1
}

Copy-Item -Path (Join-Path $QGISInstall "etc") -Destination (Join-Path $DistPath "etc") -Recurse -Force
Copy-Item -Path (Join-Path $QGISInstall "bin/python3*.dll") -Destination $DistPath -Force
Copy-Item -Path (Join-Path $QGISInstall "bin/o4w_env.bat") -Destination $DistPath -Force
Copy-Item -Path (Join-Path $QGISInstall "apps/qgis/plugins") -Destination (Join-Path $DistPath "plugins") -Recurse -Force
Copy-Item -Path (Join-Path $QGISInstall "apps/qgis/i18n") -Destination (Join-Path $DistPath "i18n") -Recurse -Force
# 把 Python 插件 processing 复制进 plugins 目录（C++ plugins 目录只有 DLL，
# processing 是 Python 插件，位于 apps/qgis/python/plugins/processing）。
# 不复制会导致 `processing.run` 始终是 stub，报
# "Processing plugin has not been loaded"（瓦片 / 缓冲工具不可用）。
# PowerShell 5.1 的 Copy-Item -Recurse 在目标目录非空时有已知 bug，改用 robocopy
# （/R:3 /W:1 自动重试锁文件；退出码 0-7 为成功，8+ 为失败）。
$processingSrc = Join-Path $QGISInstall "apps/qgis/python/plugins/processing"
$processingDst = Join-Path $DistPath "plugins/processing"
robocopy $processingSrc $processingDst /E /R:3 /W:1 /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
if ($LASTEXITCODE -ge 8) {
    Write-Host "ERROR: 复制 processing 插件失败（robocopy 退出码 $LASTEXITCODE）" -ForegroundColor Red
    exit 1
}
if (Test-Path (Join-Path $QGISInstall "apps/qgis/providers")) {
    Copy-Item -Path (Join-Path $QGISInstall "apps/qgis/providers") -Destination (Join-Path $DistPath "providers") -Recurse -Force
}
# 把启动器复制进产物，和目标机器一起分发
Copy-Item -Path (Join-Path $ProjectRoot "tools/runtime/launch_qgis.bat") -Destination (Join-Path $DistPath "launch_qgis.bat") -Force

# STAGE 5: 校验关键产物，缺了直接报错退出，避免分发不完整包
Write-Host "STAGE 5: Verifying build output..." -ForegroundColor Cyan
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

# 原生保护生效性校验：启用保护时产物里必须存在 .pyd
if ($env:QGIS_PROTECT_MANIFEST) {
    $nativeCount = (Get-ChildItem -Path (Join-Path $DistPath "app") -Recurse -Filter *.pyd -ErrorAction SilentlyContinue | Measure-Object).Count
    if ($nativeCount -lt 1) {
        Write-Host "ERROR: 已启用原生编译保护，但产物里找不到任何 .pyd" -ForegroundColor Red
        exit 1
    }
    Write-Host "OK: 原生扩展 $nativeCount 个 .pyd" -ForegroundColor Green
}
Write-Host "OK: 关键文件齐全" -ForegroundColor Green

Write-Host ""
Write-Host "=================================" -ForegroundColor Green
Write-Host "  B U I L D   C O M P L E T E ! " -ForegroundColor Green
Write-Host "=================================" -ForegroundColor Green
Write-Host "Portable app: $DistPath"
Write-Host "Run: $DistPath/$AppName.exe"