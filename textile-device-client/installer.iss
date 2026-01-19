[Setup]
AppName=纺织品检测设备客户端
AppVersion=1.0.0
AppPublisher=纺织品检测监控系统
AppCopyright=Copyright (C) 2026
DefaultDirName={pf}\TextileDeviceClient
DefaultGroupName=纺织品检测设备监控系统
OutputDir=installer
OutputBaseFilename=TextileDeviceClient-Setup.exe
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
UninstallDisplayIcon={app}\textile-device-client.exe
WizardStyle=modern

[Files]
Source: "dist\textile-device-client\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\纺织品检测设备客户端"; Filename: "{app}\textile-device-client.exe"
Name: "{commondesktop}\纺织品检测设备客户端"; Filename: "{app}\textile-device-client.exe"

[Run]
Filename: "{app}\textile-device-client.exe"; Description: "启动纺织品检测设备客户端"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "TextileDeviceClient"; ValueData: """{app}\textile-device-client.exe"""

[UninstallDelete]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "TextileDeviceClient"
