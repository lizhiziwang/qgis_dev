; 图斑核实平台 Windows 安装包脚本（Inno Setup 6）
;
; 不直接调用本文件，由 tools/build_windows.ps1 的 STAGE 6 生成
; build/installer/defs.iss 后通过 /DDefsFile=... 传入并调用 ISCC。
; defs.iss 提供：AppName / AppVersion / AppDirName / AppPublisher /
; DistDir（PyInstaller 产物目录）/ OutputDir / OutputBaseFilename /
; SetupIconFile（可选）。
;
; 设计要点：
;   - 默认装到 %LOCALAPPDATA%\<AppDirName>，PrivilegesRequired=lowest：
;     应用会往 exe 同级写 logs/.tmp/config，不能装 Program Files。
;   - config\app.yaml 升级时保留用户改动（onlyifdoesntexist）。
;   - 不打包 logs/.tmp/__pycache__，装完建空目录。
;   - 所有运行路径都是相对 %~dp0，安装到任意目录都能跑。

#ifndef DefsFile
  #error 必须通过 /DDefsFile=<build/installer/defs.iss> 传入生成的定义文件
#endif
#include DefsFile

#ifndef AppMutexName
  #define AppMutexName "qgis-desktop-single-instance"
#endif

[Setup]
; AppId 固定不变，升级才能识别为同一程序。改它会变成两个程序。
AppId={{8D3F0C9A-5B2E-4E77-9A31-6C0E4B7D1F22}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} V{#AppVersionFull}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\{#AppDirName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
UninstallDisplayIcon={app}\app.exe
UninstallDisplayName={#AppName}
#ifdef SetupIconFile
SetupIconFile={#SetupIconFile}
#endif
; 应用依赖 Windows 10 以上的 DPI / Qt6 行为
MinVersion=10.0

; 更新器 / 用户再次双击安装包时，等主程序释放单实例互斥体后再覆盖文件。
; 应用侧用 app/__init__.py 的 APP_MUTEX 创建同名互斥体。
AppMutex={#AppMutexName}
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "{#SourcePath}\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; 主产物。以下内容不随版本覆盖：
;   logs/.tmp/缓存                 运行期产物
;   config\*.yaml                 用户可编辑配置（升级保留；新增键由代码默认值兜底）
;   config\state\*                用户状态（最近工程 / 条件查询 / 字典库）
;   config\dicts.db               应用级共享字典库（运行时生成）
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "logs\*,logs,.tmp\*,.tmp,config\dicts.db,config\dicts.db-*,config\state\*,config\*.yaml,__pycache__,__pycache__\*"
; 用户配置：仅首次安装写入；已存在则保留，卸载时不删除
Source: "{#DistDir}\config\*.yaml"; DestDir: "{app}\config"; Flags: onlyifdoesntexist uninsneveruninstall

[Dirs]
Name: "{app}\logs"
Name: "{app}\.tmp"
Name: "{app}\config\state"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\app.exe"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\app.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\app.exe"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 运行期生成、安装器未登记的文件
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\.tmp"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: dirifempty; Name: "{app}"
