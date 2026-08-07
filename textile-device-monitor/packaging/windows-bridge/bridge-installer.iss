; Textile Execution Bridge 集中式 Windows Bridge 安装包
; 由 Build-BridgePackage.ps1 生成 version.auto.iss 并调用 ISCC 编译。
; 物料必须先收集到本脚本同级的 staging\ 目录（脚本自动完成）。

#define MyAppName "Textile Execution Bridge"
#define MyAppPublisher "Textile Device Monitor"
#define MyAppInstallDir "C:\TextileExecutionBridge"
#include "version.auto.iss"
#define MyAppSourceDir AddBackslash(SourcePath) + "staging"
#define MyAppOutputDir AddBackslash(SourcePath) + "output"
#define MyAppOutputBaseFilename "textile-execution-bridge-setup-" + MyAppVersion

#ifnexist MyAppSourceDir + "\manifest.json"
  #error "staging\manifest.json not found. Run Build-BridgePackage.ps1 first."
#endif
#ifnexist MyAppSourceDir + "\writers\FibreCheckWriter\FibreCheckWriter.exe"
  #error "FibreCheckWriter.exe not staged. Run Build-BridgePackage.ps1 first."
#endif
#ifnexist MyAppSourceDir + "\writers\FibreCheckFinalEntryWriter\FibreCheckFinalEntryWriter.exe"
  #error "FibreCheckFinalEntryWriter.exe not staged. Run Build-BridgePackage.ps1 first."
#endif
#ifnexist MyAppSourceDir + "\fibrecheck\FibreCheck.exe"
  #error "Frozen FibreCheck client not staged. Run Build-BridgePackage.ps1 first."
#endif
#ifnexist MyAppSourceDir + "\python\python.exe"
  #error "Bundled CPython runtime not staged. Run Build-BridgePackage.ps1 first."
#endif

[Setup]
AppId={{7F3A9C21-4E5B-4A1D-9C2E-5B8D1F0A6E3C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={#MyAppInstallDir}
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir={#MyAppOutputDir}
OutputBaseFilename={#MyAppOutputBaseFilename}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Dirs]
; 运行期目录：FinalEntry Writer 的 COM Excel 工作根与桥日志。卸载时保留。
Name: "{app}\work\final-entry"
Name: "{app}\work\logs"

[Files]
; 第三方/二进制物料与 Python 应用
Source: "{#MyAppSourceDir}\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#MyAppSourceDir}\writers\*"; DestDir: "{app}\writers"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#MyAppSourceDir}\fibrecheck\*"; DestDir: "{app}\fibrecheck"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#MyAppSourceDir}\oracle-ic-x64\*"; DestDir: "{app}\oracle-ic-x64"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "{#MyAppSourceDir}\python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#MyAppSourceDir}\python-deps\*"; DestDir: "{app}\python-deps"; Flags: ignoreversion recursesubdirs createallsubdirs
; 运维脚本与配置模板
Source: "{#MyAppSourceDir}\ops\*"; DestDir: "{app}\ops"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#MyAppSourceDir}\config\bridge.env.example"; DestDir: "{app}\config"; Flags: ignoreversion onlyifdoesntexist
Source: "{#MyAppSourceDir}\config\BridgeConfig.psd1"; DestDir: "{app}\config"; Flags: ignoreversion onlyifdoesntexist
Source: "{#MyAppSourceDir}\manifest.json"; DestDir: "{app}"; Flags: ignoreversion

[Run]
; 安装结束后不自动启动任何桥：凭据与配置需管理员先填写 config\bridge.env
; 与 config\BridgeConfig.psd1，再用 ops\Register-BridgeScheduledTasks.ps1 注册任务。
