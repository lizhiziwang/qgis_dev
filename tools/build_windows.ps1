# 统一 Windows 打包脚本
# 单一入口，所有路径从 config/app.yaml 读取
#
# 用法（在项目根目录）：
#   pwsh tools/build_windows.ps1              # 默认开启 Cython 原生编译保护
#   pwsh tools/build_windows.ps1 -NoProtect   # 关闭保护（快速调试构建）
#   pwsh tools/build_windows.ps1 -NoInstaller # 只出目录产物，不生成安装包
#   pwsh tools/build_windows.ps1 -AllowDirty  # 本地调试：允许工作区有未提交改动

param(
    [switch]$NoProtect,
    [switch]$NoInstaller,
    [switch]$AllowDirty,
    [string]$Channel = ""
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"   # 工具脚本 / 打包日志统一 UTF-8，避免中文乱码

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

# STAGE 1.5: 版本戳（必须早于 Cython / PyInstaller：app/_build.py 要进 PYZ）
Write-Host "STAGE 1.5: Stamping build metadata..." -ForegroundColor Cyan
$VersionTool = Join-Path $ProjectRoot "tools/version.py"
$VersionJson = & $PythonLauncher $VersionTool --format json
if ($LASTEXITCODE -ne 0) {
    Write-Error "读取版本号失败（tools/version.py）"
    exit 1
}
$VersionInfo = $VersionJson | ConvertFrom-Json
if ($Channel) {
    $VersionInfo.channel = $Channel
}
$StampArgs = @(
    (Join-Path $ProjectRoot "tools/stamp_source.py"),
    "--project-root", $ProjectRoot,
    "--channel", $VersionInfo.channel
)
if ($AllowDirty) { $StampArgs += "--allow-dirty" }
& $PythonLauncher @StampArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "生成 app/_build.py 失败（工作区不干净？本地调试可加 -AllowDirty）"
    exit 1
}
Write-Host "  版本 $($VersionInfo.version) / $($VersionInfo.channel)" -ForegroundColor Green

# STAGE 2: Cython 原生编译保护（.py -> .pyd，产物里不留字节码）
Write-Host "STAGE 2: Cython native protection..." -ForegroundColor Cyan
$ProtectDir = Join-Path $ProjectRoot "build/protect"
$ProtectManifest = Join-Path $ProtectDir "manifest.json"
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

# STAGE 4: 复制 QGIS runtime（归到 runtime/，根目录只留可执行/配置/资源）
Write-Host "STAGE 4: Copying QGIS runtime..." -ForegroundColor Cyan
if (-not (Test-Path $DistPath)) {
    Write-Error "dist folder not created"
    exit 1
}
$InternalPath = Join-Path $DistPath "_internal"
$RuntimePath = Join-Path $DistPath "runtime"
New-Item -ItemType Directory -Path $RuntimePath -Force | Out-Null

# 4.1 QGIS 运行时数据 -> runtime/
Copy-Item -Path (Join-Path $QGISInstall "etc") -Destination (Join-Path $RuntimePath "etc") -Recurse -Force
Copy-Item -Path (Join-Path $QGISInstall "apps/qgis/plugins") -Destination (Join-Path $RuntimePath "plugins") -Recurse -Force
Copy-Item -Path (Join-Path $QGISInstall "apps/qgis/i18n") -Destination (Join-Path $RuntimePath "i18n") -Recurse -Force
Copy-Item -Path (Join-Path $QGISInstall "apps/qt6/plugins/platforms") -Destination (Join-Path $RuntimePath "platforms") -Recurse -Force
Copy-Item -Path (Join-Path $QGISInstall "share/proj/proj.db") -Destination (Join-Path $RuntimePath "proj.db") -Force
New-Item -ItemType Directory -Path (Join-Path $RuntimePath "resources") -Force | Out-Null
Copy-Item -Path (Join-Path $QGISInstall "apps/qgis/resources/srs.db") -Destination (Join-Path $RuntimePath "resources/srs.db") -Force
if (Test-Path (Join-Path $QGISInstall "apps/qgis/providers")) {
    Copy-Item -Path (Join-Path $QGISInstall "apps/qgis/providers") -Destination (Join-Path $RuntimePath "providers") -Recurse -Force
}

# 把 Python 插件 processing 复制进 plugins 目录（C++ plugins 目录只有 DLL，
# processing 是 Python 插件，位于 apps/qgis/python/plugins/processing）。
# 不复制会导致 `processing.run` 始终是 stub，报
# "Processing plugin has not been loaded"（瓦片 / 缓冲工具不可用）。
# PowerShell 5.1 的 Copy-Item -Recurse 在目标目录非空时有已知 bug，改用 robocopy
# （/R:3 /W:1 自动重试锁文件；退出码 0-7 为成功，8+ 为失败）。
$processingSrc = Join-Path $QGISInstall "apps/qgis/python/plugins/processing"
$processingDst = Join-Path $RuntimePath "plugins/processing"
robocopy $processingSrc $processingDst /E /R:3 /W:1 /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
if ($LASTEXITCODE -ge 8) {
    Write-Host "ERROR: 复制 processing 插件失败（robocopy 退出码 $LASTEXITCODE）" -ForegroundColor Red
    exit 1
}

# 4.2 用户可改的配置 / 资源 -> 产物根（明文，可直接改）
Copy-Item -Path (Join-Path $ProjectRoot "config") -Destination (Join-Path $DistPath "config") -Recurse -Force
Copy-Item -Path (Join-Path $ProjectRoot "resource") -Destination (Join-Path $DistPath "resource") -Recurse -Force

# 4.3 少量 Python 运行时文件 -> _internal/（避免污染根目录）
Copy-Item -Path (Join-Path $QGISInstall "bin/python3*.dll") -Destination $InternalPath -Force
Copy-Item -Path (Join-Path $QGISInstall "bin/o4w_env.bat") -Destination $InternalPath -Force

# 4.4 启动器 -> 产物根，和目标机器一起分发
Copy-Item -Path (Join-Path $ProjectRoot "tools/runtime/start_runtime.bat") -Destination (Join-Path $DistPath "start_runtime.bat") -Force

# STAGE 4.5: 产物根 build_info.json（与 app/_build.py 同源，更新器/运维可直接读）
Write-Host "STAGE 4.5: Writing build_info.json..." -ForegroundColor Cyan
$StampBuildArgs = @(
    (Join-Path $ProjectRoot "tools/stamp_build.py"),
    "--project-root", $ProjectRoot,
    "--dist", $DistPath,
    "--channel", $VersionInfo.channel
)
& $PythonLauncher @StampBuildArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "写 build_info.json 失败"
    exit 1
}

# STAGE 5: 校验关键产物，缺了直接报错退出，避免分发不完整包
Write-Host "STAGE 5: Verifying build output..." -ForegroundColor Cyan
$missingFiles = @()
foreach ($required in @("app.exe", "build_info.json", "start_runtime.bat", "config/app.yaml", "resource", "runtime/proj.db", "runtime/etc", "runtime/i18n", "runtime/plugins/processing", "runtime/platforms", "_internal/qgis", "_internal/qgis_analysis.dll")) {
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
    $nativeCount = (Get-ChildItem -Path (Join-Path $DistPath "_internal/app") -Recurse -Filter *.pyd -ErrorAction SilentlyContinue | Measure-Object).Count
    if ($nativeCount -lt 1) {
        Write-Host "ERROR: 已启用原生编译保护，但产物里找不到任何 .pyd" -ForegroundColor Red
        exit 1
    }
    Write-Host "OK: 原生扩展 $nativeCount 个 .pyd" -ForegroundColor Green
}
Write-Host "OK: 关键文件齐全" -ForegroundColor Green

# STAGE 6: 安装包（Inno Setup 6）
# 目录产物已经可用；安装包只是把它压成单文件 exe，默认装到用户目录。
# 没装 Inno Setup 时跳过，不影响原有“直接分发 dist/app”的用法。
Write-Host "STAGE 6: Building installer..." -ForegroundColor Cyan
$InstallerPath = $null
if ($NoInstaller) {
    Write-Host "  -NoInstaller: skipped" -ForegroundColor Yellow
} else {
    $IsccCandidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs/Inno Setup 6/ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6/ISCC.exe")
    )
    if (${env:ProgramFiles(x86)}) {
        $IsccCandidates += (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6/ISCC.exe")
    }
    $Iscc = $IsccCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $Iscc) { $Iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source }

    if (-not $Iscc) {
        Write-Host "  未找到 Inno Setup 6（ISCC.exe），跳过安装包生成" -ForegroundColor Yellow
        Write-Host "  可执行: winget install JRSoftware.InnoSetup --source winget" -ForegroundColor Yellow
    } else {
        # 显示名取 config/app.yaml 的 app.name（版本号在 app.version，由 tools/version.py 读取）
        # 显式 UTF8：app.yaml 无 BOM，Windows PowerShell 5.1 默认按 ANSI(936) 读会乱码
        $cfgUtf8 = Get-Content $CfgPath -Raw -Encoding UTF8
        $AppNameRaw = "图斑核实平台"
        $AppBlock = [regex]::Match($cfgUtf8, '(?ms)^app:\s*\r?\n(.*?)(?=^\S|\z)')
        if ($AppBlock.Success) {
            $nameMatch = [regex]::Match($AppBlock.Groups[1].Value, '(?m)^\s+name:\s*(.+?)\s*$')
            if ($nameMatch.Success) {
                $AppNameRaw = $nameMatch.Groups[1].Value.Trim().Trim('"').Trim("'")
            }
        }
        $AppVersion = (& $PythonLauncher $VersionTool --format inno).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $AppVersion) {
            Write-Error "生成 Inno 版本号失败"
            exit 1
        }
        $AppDirName = ($AppNameRaw -replace '\s*[Vv]?\d+(?:\.\d+){1,3}\s*$', '').Trim()
        if (-not $AppDirName) { $AppDirName = $AppNameRaw }
        $OutputBase = "${AppDirName}_V${AppVersion}_Setup"

        # 生成定义文件（UTF-8 BOM：Inno 预处理器要求，否则中文会乱码）
        $DefsDir = Join-Path $ProjectRoot "build/installer"
        New-Item -ItemType Directory -Path $DefsDir -Force | Out-Null
        $DefsPath = Join-Path $DefsDir "defs.iss"
        $OutputDir = Join-Path $ProjectRoot "dist"
        $SetupIcon = Join-Path $ProjectRoot "build/app_icon.ico"
        $iconDefine = ""
        if (Test-Path $SetupIcon) {
            $iconDefine = "#define SetupIconFile `"$SetupIcon`"`r`n"
        }
        $defs = @"
; 由 tools/build_windows.ps1 生成，勿手改
#define AppName "$AppNameRaw"
#define AppVersion "$AppVersion"
#define AppVersionFull "$($VersionInfo.version)"
#define AppDirName "$AppDirName"
#define AppPublisher "$AppDirName"
#define AppMutexName "$($VersionInfo.app_mutex)"
#define DistDir "$DistPath"
#define OutputDir "$OutputDir"
#define OutputBaseFilename "$OutputBase"
$iconDefine
"@
        [System.IO.File]::WriteAllText($DefsPath, $defs, (New-Object System.Text.UTF8Encoding($true)))

        $IssPath = Join-Path $ProjectRoot "tools/installer/installer.iss"
        Write-Host "  Inno Setup: $Iscc"
        Write-Host "  安装包名:   $OutputBase.exe（LZMA2 压缩，1.4 GB 产物需要几分钟）" -ForegroundColor DarkGray
        Push-Location $ProjectRoot
        try {
            & $Iscc "/Qp" "/DDefsFile=$DefsPath" $IssPath
        } finally {
            Pop-Location
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Host "ERROR: 安装包生成失败（ISCC 退出码 $LASTEXITCODE）" -ForegroundColor Red
            exit 1
        }
        $InstallerPath = Join-Path $ProjectRoot "dist/$OutputBase.exe"
        if (Test-Path $InstallerPath) {
            $mb = [math]::Round((Get-Item $InstallerPath).Length / 1MB, 1)
            Write-Host "OK: 安装包 $InstallerPath ($mb MB)" -ForegroundColor Green
        } else {
            Write-Host "ERROR: ISCC 返回成功但未找到安装包：$InstallerPath" -ForegroundColor Red
            exit 1
        }
    }
}

Write-Host ""
Write-Host "=================================" -ForegroundColor Green
Write-Host "  B U I L D   C O M P L E T E ! " -ForegroundColor Green
Write-Host "=================================" -ForegroundColor Green
Write-Host "Portable app: $DistPath"
Write-Host "  version           $($VersionInfo.version) ($($VersionInfo.channel))"
Write-Host "  app.exe           入口"
Write-Host "  start_runtime.bat 启动器（显式设 QGIS 环境）"
Write-Host "  config/ resource/ 可编辑配置与资源"
Write-Host "  runtime/          QGIS 运行时（etc/plugins/i18n/providers/platforms/proj.db）"
Write-Host "  _internal/        Python 运行时与全部依赖库"
Write-Host "Run: $DistPath/$AppName.exe"
if ($InstallerPath) {
    Write-Host "Installer: $InstallerPath" -ForegroundColor Green
}