"""Windows 更新器脚本（内嵌为字符串，运行时落到每用户更新目录）。

流程：等旧进程退出 → 备份安装目录 → 静默运行 Inno Setup 安装包 → 校验
``build_info.json`` 版本 → 拉起新程序 → 等 ``.startup-ok`` 健康标记。

主程序退出后用户看不到任何东西，所以脚本自己负责“汇报进度”：

- 进程一起来就写**启动心跳**（``-HeartbeatFile``），主程序轮询到它才退出；
- 弹一个 WinForms 窗口，分阶段显示进度、用时和失败原因，长操作期间持续
  ``DoEvents``，窗口不会假死；
- 日志路径由命令行传入（``-LogFile``），读任务失败也能留下记录；
- 失败时回滚后窗口保留，用户点“关闭”才退出；成功则 10 秒后自动关闭。

任一步失败：杀掉新进程（若已启动）→ 用备份恢复整个安装目录 → 拉起旧版本。
更新器由主程序以 ``CREATE_NO_WINDOW`` 启动，主程序退出后它继续运行——所以
它**不能**依赖主程序目录里的任何 Python 代码。

脚本用 UTF-8 BOM 落盘：Windows PowerShell 5.1 对无 BOM 文件按 ANSI 解码，
中文日志会乱码。
"""

from __future__ import annotations

WINDOWS_UPDATER_PS1 = r"""param(
    [Parameter(Mandatory = $true)]
    [string]$JobFile,
    [string]$LogFile = "",
    [string]$HeartbeatFile = "",
    [switch]$NoUi
)

$ErrorActionPreference = "Stop"

$script:LogPath = $LogFile
if ([string]::IsNullOrWhiteSpace($script:LogPath)) {
    $script:LogPath = Join-Path $env:TEMP "qgis_dev-updater.log"
}
$script:HeartbeatPath = $HeartbeatFile
$script:NoUi = [bool]$NoUi
$script:Job = $null
$script:UiReady = $false
$script:CloseAllowed = $false
$script:StartedAt = Get-Date
$script:StageStarted = Get-Date
$script:Form = $null
$script:StageLabel = $null
$script:DetailLabel = $null
$script:ProgressBar = $null
$script:ElapsedLabel = $null
$script:CloseButton = $null
$script:OpenLogButton = $null

function Write-Log([string]$Message) {
    try {
        $path = $script:LogPath
        if ([string]::IsNullOrWhiteSpace($path)) { return }
        $dir = [IO.Path]::GetDirectoryName($path)
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
        }
        $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
        Add-Content -LiteralPath $path -Value $line -Encoding UTF8
    } catch {
        # 日志失败不能影响更新流程
    }
}

function Write-Heartbeat([string]$Stage) {
    try {
        $path = $script:HeartbeatPath
        if ([string]::IsNullOrWhiteSpace($path)) { return }
        $dir = [IO.Path]::GetDirectoryName($path)
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
        }
        $jobId = ""
        if ($script:Job) { $jobId = [string]$script:Job.jobId }
        $payload = [ordered]@{
            pid       = $PID
            jobId     = $jobId
            stage     = $Stage
            logFile   = $script:LogPath
            updatedAt = (Get-Date).ToString("o")
        } | ConvertTo-Json -Compress
        Set-Content -LiteralPath $path -Value $payload -Encoding UTF8
    } catch {
        # 心跳失败不影响更新（主程序最多多等几秒）
    }
}

function Read-Json([string]$Path) {
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Set-ElapsedLabel {
    if (-not $script:UiReady) { return }
    try {
        $seconds = [int]((Get-Date) - $script:StartedAt).TotalSeconds
        $script:ElapsedLabel.Text = "已用时 $seconds 秒    日志：$($script:LogPath)"
    } catch {
    }
}

function Pump-Ui([int]$Milliseconds = 200) {
    if ($script:UiReady) {
        try {
            [System.Windows.Forms.Application]::DoEvents() | Out-Null
            Set-ElapsedLabel
        } catch {
        }
    }
    Start-Sleep -Milliseconds $Milliseconds
}

function Update-Ui {
    param(
        [string]$Stage,
        [string]$Detail = "",
        [int]$Percent = -1,
        [switch]$IsError
    )
    $script:StageStarted = Get-Date
    Write-Heartbeat $Stage
    if (-not $script:UiReady) {
        Write-Log "进度：$Stage $Detail"
        return
    }
    try {
        $script:StageLabel.Text = $Stage
        $script:DetailLabel.Text = $Detail
        if ($IsError) {
            $script:StageLabel.ForeColor = [System.Drawing.Color]::FromArgb(207, 19, 34)
        } else {
            $script:StageLabel.ForeColor = [System.Drawing.Color]::FromArgb(22, 119, 255)
        }
        if ($Percent -ge 0) {
            $script:ProgressBar.Style = [System.Windows.Forms.ProgressBarStyle]::Continuous
            $script:ProgressBar.Value = [Math]::Max(0, [Math]::Min(100, $Percent))
        } else {
            $script:ProgressBar.Style = [System.Windows.Forms.ProgressBarStyle]::Marquee
        }
        Set-ElapsedLabel
        [System.Windows.Forms.Application]::DoEvents() | Out-Null
    } catch {
        Write-Log "刷新进度窗口失败：$($_.Exception.Message)"
    }
}

function Wait-ProcessWithUi {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$Stage,
        [string]$Detail
    )
    Update-Ui -Stage $Stage -Detail $Detail
    $last = -1
    while (-not $Process.HasExited) {
        $seconds = [int]((Get-Date) - $script:StageStarted).TotalSeconds
        if ($seconds -ne $last -and $script:UiReady) {
            $last = $seconds
            try { $script:DetailLabel.Text = "$Detail（已用时 $seconds 秒）" } catch { }
        }
        Pump-Ui 200
    }
    $Process.Refresh()
    return $Process.ExitCode
}

function Hide-ConsoleWindow {
    try {
        $source = @"
using System;
using System.Runtime.InteropServices;
public static class UpdateWindowNative {
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@
        if (-not ("UpdateWindowNative" -as [type])) {
            Add-Type -TypeDefinition $source
        }
        $handle = [UpdateWindowNative]::GetConsoleWindow()
        if ($handle -ne [IntPtr]::Zero) {
            [UpdateWindowNative]::ShowWindow($handle, 0) | Out-Null
        }
    } catch {
        Write-Log "隐藏控制台窗口失败：$($_.Exception.Message)"
    }
}

function Initialize-UpdateUi {
    if ($script:NoUi) {
        Write-Log "已指定 -NoUi：不创建进度窗口"
        return $false
    }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
    } catch {
        Write-Log "无法加载 WinForms，本次更新只在日志中记录进度：$($_.Exception.Message)"
        return $false
    }
    try {
        [System.Windows.Forms.Application]::EnableVisualStyles()

        $form = New-Object System.Windows.Forms.Form
        $form.Text = "图斑核实平台 - 软件更新"
        $form.ClientSize = New-Object System.Drawing.Size(560, 268)
        $form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
        $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
        $form.MaximizeBox = $false
        $form.MinimizeBox = $true
        # 先不置顶：主程序可能弹出“是否保存工程”之类的确认框，置顶会挡住它。
        # 等旧进程真正退出后再置顶。
        $form.TopMost = $false
        $form.ShowInTaskbar = $true
        $form.BackColor = [System.Drawing.Color]::White

        $title = New-Object System.Windows.Forms.Label
        $title.Text = "正在安装新版本"
        $title.Font = [System.Drawing.Font]::new("Microsoft YaHei UI", 16, [System.Drawing.FontStyle]::Bold)
        $title.Location = New-Object System.Drawing.Point(24, 16)
        $title.Size = New-Object System.Drawing.Size(512, 32)
        $form.Controls.Add($title)

        $versionText = "正在准备更新"
        if ($script:Job) {
            $versionText = "目标版本 $($script:Job.expectedVersion)（$($script:Job.channel) 通道）"
        }
        $version = New-Object System.Windows.Forms.Label
        $version.Text = $versionText
        $version.Font = [System.Drawing.Font]::new("Microsoft YaHei UI", 9)
        $version.ForeColor = [System.Drawing.Color]::FromArgb(107, 114, 128)
        $version.Location = New-Object System.Drawing.Point(26, 52)
        $version.Size = New-Object System.Drawing.Size(508, 20)
        $form.Controls.Add($version)

        $script:StageLabel = New-Object System.Windows.Forms.Label
        $script:StageLabel.Text = "正在准备更新"
        $script:StageLabel.Font = [System.Drawing.Font]::new("Microsoft YaHei UI", 12, [System.Drawing.FontStyle]::Bold)
        $script:StageLabel.ForeColor = [System.Drawing.Color]::FromArgb(22, 119, 255)
        $script:StageLabel.Location = New-Object System.Drawing.Point(26, 84)
        $script:StageLabel.Size = New-Object System.Drawing.Size(508, 28)
        $form.Controls.Add($script:StageLabel)

        $script:DetailLabel = New-Object System.Windows.Forms.Label
        $script:DetailLabel.Text = "请稍候…"
        $script:DetailLabel.Font = [System.Drawing.Font]::new("Microsoft YaHei UI", 9)
        $script:DetailLabel.ForeColor = [System.Drawing.Color]::FromArgb(75, 85, 99)
        $script:DetailLabel.Location = New-Object System.Drawing.Point(26, 116)
        $script:DetailLabel.Size = New-Object System.Drawing.Size(508, 44)
        $form.Controls.Add($script:DetailLabel)

        $script:ProgressBar = New-Object System.Windows.Forms.ProgressBar
        $script:ProgressBar.Location = New-Object System.Drawing.Point(26, 164)
        $script:ProgressBar.Size = New-Object System.Drawing.Size(508, 18)
        $script:ProgressBar.Minimum = 0
        $script:ProgressBar.Maximum = 100
        $script:ProgressBar.Style = [System.Windows.Forms.ProgressBarStyle]::Marquee
        $form.Controls.Add($script:ProgressBar)

        $script:ElapsedLabel = New-Object System.Windows.Forms.Label
        $script:ElapsedLabel.Font = [System.Drawing.Font]::new("Microsoft YaHei UI", 8)
        $script:ElapsedLabel.ForeColor = [System.Drawing.Color]::FromArgb(156, 163, 175)
        $script:ElapsedLabel.Location = New-Object System.Drawing.Point(26, 188)
        $script:ElapsedLabel.Size = New-Object System.Drawing.Size(508, 18)
        $form.Controls.Add($script:ElapsedLabel)

        $script:OpenLogButton = New-Object System.Windows.Forms.Button
        $script:OpenLogButton.Text = "打开日志文件夹"
        $script:OpenLogButton.Location = New-Object System.Drawing.Point(316, 220)
        $script:OpenLogButton.Size = New-Object System.Drawing.Size(120, 30)
        $script:OpenLogButton.Visible = $false
        $script:OpenLogButton.Add_Click({
            try {
                $dir = [IO.Path]::GetDirectoryName($script:LogPath)
                if ($dir -and (Test-Path -LiteralPath $dir)) {
                    Start-Process -FilePath "explorer.exe" -ArgumentList @($dir)
                }
            } catch {
            }
        })
        $form.Controls.Add($script:OpenLogButton)

        $script:CloseButton = New-Object System.Windows.Forms.Button
        $script:CloseButton.Text = "关闭"
        $script:CloseButton.Location = New-Object System.Drawing.Point(444, 220)
        $script:CloseButton.Size = New-Object System.Drawing.Size(90, 30)
        $script:CloseButton.Visible = $false
        $script:CloseButton.Add_Click({
            $script:CloseAllowed = $true
            $script:Form.Close()
        })
        $form.Controls.Add($script:CloseButton)

        $form.Add_FormClosing({
            param($sender, $eventArgs)
            if (-not $script:CloseAllowed) {
                $eventArgs.Cancel = $true
                try {
                    $script:DetailLabel.Text = "更新正在进行，请稍候；完成或失败后窗口会自动关闭。"
                } catch {
                }
            }
        })

        $script:Form = $form
        $form.Show()
        $form.Activate()
        [System.Windows.Forms.Application]::DoEvents() | Out-Null
        $script:UiReady = $true
        Set-ElapsedLabel
        return $true
    } catch {
        Write-Log "创建进度窗口失败：$($_.Exception.Message)"
        $script:UiReady = $false
        try { if ($form) { $form.Dispose() } } catch { }
        return $false
    }
}

function Show-FailureUi([string]$Message) {
    Update-Ui -Stage "更新失败" -Detail "$Message。当前版本保持不变。" -IsError
    Write-Log "更新失败：$Message"
    if (-not $script:UiReady) { return }
    try {
        $script:ProgressBar.Style = [System.Windows.Forms.ProgressBarStyle]::Continuous
        $script:ProgressBar.Value = 100
        $script:ProgressBar.ForeColor = [System.Drawing.Color]::FromArgb(207, 19, 34)
        $script:DetailLabel.Text = "$Message`r`n当前版本保持不变。日志：$($script:LogPath)"
        $script:CloseButton.Visible = $true
        $script:OpenLogButton.Visible = $true
        $script:CloseAllowed = $true
        [System.Windows.Forms.Application]::DoEvents() | Out-Null
    } catch {
    }
    # 窗口保留到用户关闭（最多 30 分钟，避免留下永远不退的进程）
    $deadline = (Get-Date).AddMinutes(30)
    while ($script:UiReady -and -not $script:Form.IsDisposed -and $script:Form.Visible -and (Get-Date) -lt $deadline) {
        [System.Windows.Forms.Application]::DoEvents() | Out-Null
        Start-Sleep -Milliseconds 100
    }
    try { if (-not $script:Form.IsDisposed) { $script:Form.Close() } } catch { }
}

function Show-SuccessUi([string]$Message) {
    Update-Ui -Stage "更新完成" -Detail $Message -Percent 100
    Write-Log "更新成功：$Message"
    if (-not $script:UiReady) { return }
    try {
        $script:CloseButton.Visible = $true
        $script:CloseAllowed = $true
    } catch {
    }
    for ($i = 10; $i -ge 1; $i--) {
        if (-not $script:UiReady -or $script:Form.IsDisposed -or -not $script:Form.Visible) { return }
        try {
            $script:DetailLabel.Text = "$Message 本窗口将在 $i 秒后自动关闭。"
            [System.Windows.Forms.Application]::DoEvents() | Out-Null
        } catch {
        }
        Start-Sleep -Seconds 1
    }
    try { $script:Form.Close() } catch { }
}

function Show-CancelledUi([string]$Message) {
    Update-Ui -Stage "已取消本次更新" -Detail $Message -Percent 0
    Write-Log "已取消更新：$Message"
    if (-not $script:UiReady) { return }
    try {
        $script:CloseButton.Visible = $true
        $script:CloseAllowed = $true
    } catch {
    }
    for ($i = 20; $i -ge 1; $i--) {
        if (-not $script:UiReady -or $script:Form.IsDisposed -or -not $script:Form.Visible) { return }
        try {
            $script:DetailLabel.Text = "$Message 本窗口将在 $i 秒后自动关闭。"
            [System.Windows.Forms.Application]::DoEvents() | Out-Null
        } catch {
        }
        Start-Sleep -Seconds 1
    }
    try { $script:Form.Close() } catch { }
}

function Remove-Marker([string]$Name) {
    $path = Join-Path $job.markerDir $Name
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
}

function Stop-StartedProcess {
    $path = Join-Path $job.markerDir ".process-started"
    if (-not (Test-Path -LiteralPath $path)) { return }
    try {
        $marker = Read-Json $path
        if ($marker.pid) {
            Stop-Process -Id ([int]$marker.pid) -Force -ErrorAction SilentlyContinue
            Write-Log "已停止新版本进程 pid=$($marker.pid)"
        }
    } catch {
        Write-Log "读取启动标记失败：$($_.Exception.Message)"
    }
}

function Stop-AppDirProcesses {
    # 兜底：从安装目录启动的任何同名进程都停掉，否则替换目录会因文件占用失败
    # （例如用户又手动启动了一份程序，或标记/LaunchedPid 已丢失）。
    $exeName = [IO.Path]::GetFileName($job.exePath)
    try {
        Get-CimInstance Win32_Process -Filter "Name='$exeName'" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.ExecutablePath -and
                $_.ExecutablePath.StartsWith($job.appDir, [StringComparison]::OrdinalIgnoreCase)
            } |
            ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                Write-Log "已停止安装目录内的进程 pid=$($_.ProcessId)"
            }
    } catch {
        Write-Log "枚举安装目录进程失败：$($_.Exception.Message)"
    }
}

function Remove-WithRetry([string]$Path, [int]$Attempts = 4) {
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            if (-not (Test-Path -LiteralPath $Path)) { return $true }
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return $true
        } catch {
            Write-Log "删除 $Path 失败（第 $i/$Attempts 次）：$($_.Exception.Message)"
            Start-Sleep -Seconds 2
        }
    }
    return $false
}

function Quote-Arg([string]$Value) {
    if ($Value -match '\s') { return '"' + $Value + '"' }
    return $Value
}

function Restore-Backup([string]$Reason) {
    if (-not $script:BackupOk) {
        Write-Log "没有可用备份，无法自动回滚：$Reason"
        return
    }
    try {
        Update-Ui -Stage "正在回滚到原版本" -Detail $Reason -IsError
        Stop-StartedProcess
        if ($script:LaunchedPid -gt 0) {
            # 标记里的 PID 已退出、但 Start-Process 拉起的进程还在时也要停掉，
            # 否则替换安装目录会因文件占用失败。
            Stop-Process -Id $script:LaunchedPid -Force -ErrorAction SilentlyContinue
        }
        $script:LaunchedPid = 0
        Stop-AppDirProcesses
        Write-Log "回滚：恢复备份 $($job.backupDir) -> $($job.appDir)"
        if (Remove-WithRetry $job.appDir) {
            Move-Item -LiteralPath $job.backupDir -Destination $job.appDir
        } else {
            # 仍有文件被占用时不能把安装目录留在半删状态：直接用备份覆盖回去。
            # 个别被锁文件可能仍是新版本，但程序主体能恢复可用。
            robocopy $job.backupDir $job.appDir /E /COPY:DAT /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
            if ($LASTEXITCODE -ge 8) {
                throw "安装目录无法删除，覆盖恢复也失败（robocopy $LASTEXITCODE）"
            }
            Write-Log "警告：安装目录未能完全删除，已用备份覆盖恢复（备份保留在 $($job.backupDir)）"
        }
        Remove-Marker ".process-started"
        Remove-Marker ".startup-ok"
        if (Test-Path -LiteralPath $job.exePath) {
            Start-Process -FilePath $job.exePath -WorkingDirectory $job.appDir | Out-Null
            Write-Log "已重新拉起旧版本"
        }
    } catch {
        Write-Log "回滚失败：$($_.Exception.Message)"
    }
}

function Invoke-Installer {
    $extension = [IO.Path]::GetExtension($job.packagePath).ToLowerInvariant()
    $proc = $null
    if ($extension -eq ".msi") {
        $proc = Start-Process -FilePath "msiexec.exe" `
            -ArgumentList @("/i", (Quote-Arg $job.packagePath), "/qn", "/norestart", "/l*v", (Quote-Arg $job.installerLog)) `
            -PassThru
    } elseif ($extension -eq ".exe") {
        $installerArgs = @(
            "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/CLOSEAPPLICATIONS", "/LOG=$($job.installerLog)"
        )
        $proc = Start-Process -FilePath $job.packagePath -ArgumentList $installerArgs -PassThru
    } else {
        throw "不支持的安装包格式：$extension"
    }
    if (-not $proc) {
        throw "无法启动安装程序"
    }
    $code = Wait-ProcessWithUi -Process $proc -Stage "正在安装新版本" -Detail "安装程序正在运行，请勿关闭本窗口"
    if ($code -ne 0 -and $code -ne 3010) {
        throw "安装器退出码 $code"
    }
    Write-Log "安装器执行完成，退出码 $code"
}

function Test-InstalledVersion {
    $infoPath = Join-Path $job.appDir "build_info.json"
    if (-not (Test-Path -LiteralPath $infoPath)) {
        throw "安装后找不到 build_info.json"
    }
    $info = Read-Json $infoPath
    $code = [int]$info.versionCode
    if ($code -lt [int]$job.expectedVersionCode) {
        throw "安装后版本不符：$code < $($job.expectedVersionCode)"
    }
    Write-Log "安装后版本校验通过：version=$($info.version) versionCode=$code"
}

function Test-PackageHash {
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $job.packagePath).Hash.ToLowerInvariant()
    if ($hash -ne ([string]$job.packageSha256).ToLowerInvariant()) {
        throw "安装包 SHA-256 校验失败"
    }
    Write-Log "安装包 SHA-256 校验通过"
}

function Wait-StartupHealthy {
    # 返回 ok / alive / dead。
    # ok    = 写出了 .startup-ok（主窗口已显示）
    # alive = 超过等待时间但进程仍在（可能停在登录框 / 启动较慢）——不回滚
    # dead  = 进程已退出且没有健康标记——回滚
    $okPath = Join-Path $job.markerDir ".startup-ok"
    $startedPath = Join-Path $job.markerDir ".process-started"
    $deadline = (Get-Date).AddSeconds([int]$job.healthTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $okPath) {
            try {
                $marker = Read-Json $okPath
                if ([int]$marker.versionCode -ge [int]$job.expectedVersionCode) {
                    return "ok"
                }
            } catch {
                # 标记正在写入，下一轮再读
            }
        }
        # 以新程序自己写的 .process-started 为准（PyInstaller 引导器或程序自重启
        # 时 Start-Process 返回的 pid 可能先退出）；没有标记时才用 LaunchedPid。
        # 新进程还没写标记就退出（缺 DLL / 启动即崩）同样必须回滚。
        $appPid = $script:LaunchedPid
        if (Test-Path -LiteralPath $startedPath) {
            try {
                $marker = Read-Json $startedPath
                if ($marker.pid) { $appPid = [int]$marker.pid }
            } catch {
                # 标记正在写入，下一轮再读
            }
        }
        if ($appPid -gt 0 -and -not (Get-Process -Id $appPid -ErrorAction SilentlyContinue)) {
            return "dead"
        }
        Pump-Ui 500
    }
    return "alive"
}

$script:BackupOk = $false
$script:LaunchedPid = 0

# 心跳与日志先于一切：主程序轮询到心跳才退出；任务解析失败也有记录可查。
Write-Log "更新器进程已启动：pid=$PID job=$JobFile"
Write-Heartbeat "启动"

try {
    $job = Read-Json $JobFile
    $script:Job = $job
    if (-not [string]::IsNullOrWhiteSpace([string]$job.logFile)) {
        $script:LogPath = [string]$job.logFile
    }
} catch {
    $message = "读取更新任务失败：$($_.Exception.Message)"
    Write-Log $message
    if (Initialize-UpdateUi) {
        Hide-ConsoleWindow
        Show-FailureUi $message
    }
    exit 1
}

New-Item -ItemType Directory -Force -Path $job.updaterDir | Out-Null
New-Item -ItemType Directory -Force -Path $job.markerDir | Out-Null
Write-Log "更新器启动：job=$($job.jobId) version=$($job.expectedVersion)"

if (Initialize-UpdateUi) {
    Hide-ConsoleWindow
} else {
    Write-Log "进度窗口不可用，本次更新只在日志中记录进度"
}

try {
    if (-not (Test-Path -LiteralPath $job.packagePath -PathType Leaf)) {
        throw "安装包不存在：$($job.packagePath)"
    }
    if ([string]::IsNullOrWhiteSpace($job.appDir) -or $job.appDir.Length -lt 3) {
        throw "安装目录不合法"
    }
    if ([IO.Path]::GetPathRoot($job.appDir) -eq $job.appDir) {
        throw "拒绝以磁盘根目录作为安装目录"
    }

    Update-Ui -Stage "正在校验安装包" -Detail "校验安装包完整性（SHA-256）" -Percent 3
    Test-PackageHash

    # 1) 等旧进程退出（单实例互斥体随进程结束释放；再给安装器 1 秒）
    Update-Ui -Stage "正在等待程序退出" -Detail "程序关闭后会自动继续安装，最多等待 $($job.waitSeconds) 秒。" -Percent 10
    $deadline = (Get-Date).AddSeconds([int]$job.waitSeconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id ([int]$job.pid) -ErrorAction SilentlyContinue)) {
            break
        }
        Pump-Ui 300
    }
    if (Get-Process -Id ([int]$job.pid) -ErrorAction SilentlyContinue) {
        Write-Log "等待旧进程退出超时，放弃本次更新"
        Show-CancelledUi "程序仍在运行，本次更新已放弃（未修改任何文件）。"
        exit 2
    }
    Start-Sleep -Seconds 1
    Write-Log "旧进程已退出"
    if ($script:UiReady) {
        try { $script:Form.TopMost = $true } catch { }
    }

    # 2) 备份（失败则中止，宁可不升级也不能没有回滚路径）
    if ($job.backup) {
        if (Test-Path -LiteralPath $job.backupDir) {
            Update-Ui -Stage "正在清理旧备份" -Detail $job.backupDir -Percent 22
            Remove-Item -LiteralPath $job.backupDir -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path $job.backupDir | Out-Null
        Write-Log "备份安装目录到 $($job.backupDir)"
        $robocopyArgs = @(
            (Quote-Arg $job.appDir), (Quote-Arg $job.backupDir),
            "/E", "/COPY:DAT", "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS", "/NP"
        )
        $backupProc = Start-Process -FilePath "$env:SystemRoot\System32\robocopy.exe" `
            -ArgumentList $robocopyArgs -PassThru -WindowStyle Hidden
        $backupCode = Wait-ProcessWithUi -Process $backupProc -Stage "正在备份当前版本" -Detail "备份到 $($job.backupDir)"
        if ($backupCode -ge 8) {
            throw "备份失败，robocopy 退出码 $backupCode"
        }
        $script:BackupOk = $true
        Write-Log "备份完成：$($job.backupDir)"
    }

    Remove-Marker ".process-started"
    Remove-Marker ".startup-ok"

    # 3) 静默安装
    Update-Ui -Stage "正在安装新版本" -Detail "安装程序正在运行，请勿关闭本窗口" -Percent 50
    Test-PackageHash
    Invoke-Installer
    Update-Ui -Stage "正在校验安装结果" -Detail "检查新版本文件与版本号" -Percent 75
    Test-InstalledVersion

    # 4) 拉起新版本并等健康标记
    Update-Ui -Stage "正在启动新版本" -Detail $job.exePath -Percent 85
    Write-Log "启动新版本：$($job.exePath)"
    $newProc = Start-Process -FilePath $job.exePath -WorkingDirectory $job.appDir -PassThru
    if (-not $newProc) {
        throw "无法启动新版本（Start-Process 未返回进程）"
    }
    $script:LaunchedPid = $newProc.Id
    Update-Ui -Stage "正在等待新版本启动完成" -Detail "最多等待 $($job.healthTimeoutSeconds) 秒" -Percent 92
    $health = Wait-StartupHealthy
    if ($health -eq "dead") {
        throw "新版本启动后已退出，未写出健康标记"
    }
    if ($health -eq "alive") {
        Write-Log "警告：未在 $($job.healthTimeoutSeconds) 秒内确认健康标记，进程仍在运行，保留新版本"
    }
    Write-Log "更新成功：version=$($job.expectedVersion)"

    if ($script:BackupOk -and (Test-Path -LiteralPath $job.backupDir)) {
        Update-Ui -Stage "正在清理临时备份" -Detail $job.backupDir -Percent 100
        Remove-Item -LiteralPath $job.backupDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    Show-SuccessUi "新版本 $($job.expectedVersion) 已安装并启动。"
    exit 0
} catch {
    $message = $_.Exception.Message
    Write-Log "更新失败：$message"
    Restore-Backup $message
    Show-FailureUi $message
    exit 1
}
"""


__all__ = ["WINDOWS_UPDATER_PS1"]
