param(
    [string]$FibreCheckDir = "$PSScriptRoot\..\..\..\.tmp\FibreCheck",
    [string]$OutDir = "$PSScriptRoot\out"
)

$ErrorActionPreference = 'Stop'
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { throw "找不到 .NET Framework 编译器: $csc" }
$odp = Join-Path $FibreCheckDir 'Oracle.ManagedDataAccess.dll'
if (-not (Test-Path $odp)) { throw "找不到 Oracle.ManagedDataAccess.dll: $odp" }

New-Item -ItemType Directory -Force $OutDir | Out-Null
$sources = @(
    "$PSScriptRoot\src\Db.cs",
    "$PSScriptRoot\src\Redact.cs",
    "$PSScriptRoot\src\MiniJson.cs",
    "$PSScriptRoot\src\LegacyLoginFlow.cs",
    "$PSScriptRoot\src\SystemDataConnection.cs",
    "$PSScriptRoot\src\Program.cs"
)

& $csc -nologo -target:exe -platform:x86 -utf8output -debug- -optimize+ `
    -out:"$OutDir\FibreCheckRunner.exe" `
    -reference:"$odp" `
    $sources
if ($LASTEXITCODE -ne 0) { throw "编译失败: $LASTEXITCODE" }
Write-Host "构建完成: $OutDir\FibreCheckRunner.exe"
