[Setup]
AppId={{C12E1D9A-9B3B-4A2B-9E5A-1E2D8B08C6C1}
AppName=Textile Device Client (Console)
AppVersion=1.0.1
AppPublisher=Textile Device Monitor
DefaultDirName={localappdata}\TextileDeviceClient
DefaultGroupName=Textile Device Client
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir=dist-installer
OutputBaseFilename=textile-device-client-console-setup
Compression=lzma
SolidCompression=yes
SetupIconFile=resources\icon.ico
UninstallDisplayIcon={app}\textile-device-client.exe

[Files]
Source: "dist\textile-device-client-console\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userdesktop}\Textile Device Client"; Filename: "{app}\textile-device-client.exe"; WorkingDir: "{app}"
Name: "{commonstartup}\Textile Device Client"; Filename: "{app}\textile-device-client.exe"; WorkingDir: "{app}"

[Run]
Filename: "{app}\textile-device-client.exe"; Description: "运行 Textile Device Client"; Flags: nowait postinstall skipifsilent
