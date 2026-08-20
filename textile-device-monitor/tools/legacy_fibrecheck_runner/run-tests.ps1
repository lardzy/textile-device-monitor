param(
    [string]$FibreCheckDir = "$PSScriptRoot\..\..\..\.tmp\FibreCheck",
    [string]$OutDir = "$PSScriptRoot\out"
)

$ErrorActionPreference = 'Stop'
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
$odp = Join-Path $FibreCheckDir 'Oracle.ManagedDataAccess.dll'
New-Item -ItemType Directory -Force $OutDir | Out-Null

$sources = @(
    "$PSScriptRoot\src\Db.cs",
    "$PSScriptRoot\src\Redact.cs",
    "$PSScriptRoot\src\MiniJson.cs",
    "$PSScriptRoot\src\LegacyLoginFlow.cs",
    "$PSScriptRoot\src\SystemDataConnection.cs",
    "$PSScriptRoot\src\Program.cs",
    "$PSScriptRoot\tests\SelfTest.cs"
)

& $csc -nologo -target:exe -platform:x86 -utf8output -debug- -optimize+ `
    -main:LegacyFibreCheckRunner.Tests.SelfTest `
    -out:"$OutDir\FibreCheckRunner.SelfTest.exe" `
    -reference:"$odp" `
    $sources
if ($LASTEXITCODE -ne 0) { throw "自测编译失败: $LASTEXITCODE" }

& "$OutDir\FibreCheckRunner.SelfTest.exe"
if ($LASTEXITCODE -ne 0) { throw "自测未通过: $LASTEXITCODE" }
Write-Host "自测全部通过"
