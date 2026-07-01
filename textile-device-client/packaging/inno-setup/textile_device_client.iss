#define MyAppName "Textile Device Client"
#include "version.auto.iss"
#define MyAppPublisher "Textile Device Monitor"
#define MyAppExeName "textile-device-client.exe"
#define MyAppShortcutName "Textile Device Client"
#define ProjectRoot AddBackslash(SourcePath) + "..\.."
#define MyAppSourceDir ProjectRoot + "\dist\windows\TextileDeviceClient"
#define MyAppOutputDir ProjectRoot + "\dist\installer"
#define MyAppOutputBaseFilename "textile-device-client-setup-" + MyAppVersion
#define MyAppIconFile ProjectRoot + "\resources\icon.ico"

#ifnexist MyAppSourceDir + "\" + MyAppExeName
  #error "PyInstaller output not found. Build dist/windows/TextileDeviceClient first."
#endif

#ifnexist MyAppIconFile
  #error "Application icon not found. Expected resources/icon.ico."
#endif

[Setup]
AppId={{C12E1D9A-9B3B-4A2B-9E5A-1E2D8B08C6C1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\TextileDeviceClient
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#MyAppOutputDir}
OutputBaseFilename={#MyAppOutputBaseFilename}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#MyAppIconFile}
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
CloseApplicationsFilter={#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}";

[Files]
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userdesktop}\{#MyAppShortcutName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppShortcutName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent
