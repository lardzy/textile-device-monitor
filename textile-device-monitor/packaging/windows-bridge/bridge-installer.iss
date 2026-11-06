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
CloseApplications=no
RestartApplications=no
SetupMutex=TextileExecutionBridgeInstaller
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
Source: "{#MyAppSourceDir}\ops\BridgeInstall.ps1"; DestDir: "{tmp}"; DestName: "BridgeInstallPreflight.ps1"; Flags: dontcopy
Source: "{#MyAppSourceDir}\config\BridgeConfig.psd1"; DestDir: "{app}\defaults"; Flags: ignoreversion
Source: "{#MyAppSourceDir}\manifest.json"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{commonprograms}\Textile Execution Bridge\Bridge 配置中心"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\ops\BridgeControlCenter.ps1"""; WorkingDir: "{app}"

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\ops\BridgeControlCenter.ps1"""; Description: "打开 Bridge 配置中心"; Flags: postinstall nowait skipifsilent runasoriginaluser

[Code]
var
  Prepared, CopyStarted: Boolean;

function Hook(Phase: String; LegacyIdle: Boolean): String;
var
  Args, ErrFile: String;
  Code: Integer;
  Detail: AnsiString;
begin
  Result := '';
  ErrFile := ExpandConstant('{tmp}\bridge-install-error.txt');
  DeleteFile(ErrFile);
  Args := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{tmp}\BridgeInstallPreflight.ps1') + '" -Phase ' + Phase +
    ' -InstallRoot "' + ExpandConstant('{app}') + '" -StateFile "' +
    ExpandConstant('{tmp}\bridge-maintenance.json') + '" -ErrorFile "' + ErrFile + '"';
  if LegacyIdle then Args := Args + ' -LegacyIdle';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Args, '',
              SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then begin
    Result := 'Bridge 安装检查未通过，请核对计划任务、配置和安装日志。';
    if LoadStringFromFile(ErrFile, Detail) then Result := UTF8Decode(Detail);
    Log(Result);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var Confirmed: Boolean;
begin
  if Prepared then begin Result := ''; exit; end;
  ExtractTemporaryFile('BridgeInstallPreflight.ps1');
  Confirmed := ExpandConstant('{param:LEGACYIDLE|0}') = '1';
  Result := Hook('Prepare', Confirmed);
  if Result = 'LEGACY_MAINTENANCE_REQUIRED' then begin
    if WizardSilent then begin Result := '旧版升级请先核实维护窗口，交互运行安装程序。'; exit; end;
    Confirmed := MsgBox('这是旧版 Bridge 的首次迁移。请暂停发起及批准新业务，确认已批准待领取、正在执行和待回执的操作都已收尾。现在进入维护窗口？',
                        mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
    if not Confirmed then begin Result := '请在维护窗口内重新安装。'; exit; end;
    Result := Hook('Prepare', True);
  end;
  Prepared := Result = '';
end;
procedure CurStepChanged(Step: TSetupStep);
var Err: String;
begin
  if Step = ssInstall then CopyStarted := True;
  if Step = ssPostInstall then begin
    Err := Hook('Complete', False);
    if Err <> '' then RaiseException(Err);
  end;
end;

procedure DeinitializeSetup;
begin
  if Prepared and not CopyStarted then Hook('Abort', False);
end;
