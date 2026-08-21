; =====================================================================
; PerfDog-CN 安装包脚本（Inno Setup）
; ---------------------------------------------------------------------
; 前置条件：
;   1) dist\perfdog.exe 已构建（先跑 packaging\build_exe.ps1 -Mode onefile；
;      build_installer.ps1 会在缺失时自动先构建）
;   2) Inno Setup 6.x 已安装
;      下载地址：https://jrsoftware.org/isdl.php （免费，选 isetup-6.x.x.exe）
;
; 编译安装包（两种方式）：
;   A) 一条命令（推荐）：
;        pwsh -ExecutionPolicy Bypass -File packaging\build_installer.ps1
;      （ISCC 路径可配置：-IsccPath "C:\...\ISCC.exe"）
;   B) 手动：
;        用 Inno Setup IDE 打开本 .iss → 菜单 Build → Compile；
;        或命令行：
;        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\perfdog_installer.iss
;      产物：packaging\installer_output\PerfDog-CN-Setup-<版本>.exe
;
; 安装包行为：
;   - 安装到当前用户目录 {localappdata}\Programs\PerfDog-CN：
;     免管理员权限、无 UAC 弹窗；exe 旁的 output/ 与 config.json 可正常读写
;   - 桌面快捷方式 + 开始菜单（程序 / 使用教程 / 指标说明 / 卸载）
;   - 附带《使用教程-保姆级.md》《指标说明.md》
;   - 标准卸载器（设置→应用，或开始菜单"卸载"项；只删程序，
;     不删用户数据 output/——如需彻底清除请手动删除安装目录）
;
; 重要：本文件必须保存为「UTF-8 带 BOM」编码，否则 Inno Setup 会按
;       ANSI/GBK 误解中文文件名（build_installer.ps1 不处理编码，
;       手工编辑后请确认 BOM 仍在）。
; =====================================================================

#define MyAppName "PerfDog-CN"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "PerfDog-CN"
#define MyAppExeName "perfdog.exe"

[Setup]
; AppId 唯一标识本应用（升级/卸载识别用），勿改动
AppId={{B7E3D2A1-4C5F-4E6A-9D8B-2F1A3C4E5D60}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; 按用户安装：不需要管理员权限，目录可写（exe 首次运行会在旁边生成 config.json / output/）
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
OutputDir=installer_output
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
; exe 是控制台程序，安装/卸载时无需检测"关闭正在运行的应用"
CloseApplications=no
ShowLanguageDialog=no

[Languages]
; 默认英文向导（Inno Setup 自带）。如需中文向导：从
; https://jrsoftware.org/files/istrans/ 下载 ChineseSimplified.isl
; 放入 Inno Setup 安装目录的 Languages\ 后，增加一行：
;   Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; onefile 单 exe（内含采集器 + 实时看板全部代码）
Source: "..\dist\perfdog.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\使用教程-保姆级.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\指标说明.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; 桌面快捷方式（{autodesktop} 在免管理员安装时自动指向当前用户桌面）
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Comment: "启动性能采集 + 实时看板（需先用数据线连接手机）"
; 开始菜单
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\使用教程"; Filename: "{app}\使用教程-保姆级.md"
Name: "{group}\指标说明"; Filename: "{app}\指标说明.md"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
; 安装完成页勾选"打开使用教程"（用系统默认程序查看 md）。
; 不勾选"立即运行程序"：exe 需要已连接的手机，直接跑会报 adb 错误，
; 正确姿势先看教程（连手机 → 开 USB 调试 → 再启动）。
Filename: "{app}\使用教程-保姆级.md"; Description: "打开《使用教程-保姆级》"; Flags: nowait postinstall skipifsilent shellexec
