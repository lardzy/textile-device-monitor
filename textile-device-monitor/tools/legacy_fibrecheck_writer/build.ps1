param(
    [string]$FibreCheckDir = "$PSScriptRoot\..\..\..\.tmp\FibreCheck",
    [string]$OdpEfDir = "$PSScriptRoot\..\..\..\.tmp\odp-ef\lib\net45",
    [string]$Odac32Dir = "$PSScriptRoot\..\..\..\.tmp\odac32",
    [string]$RunnerSrc = "$PSScriptRoot\..\legacy_fibrecheck_runner\src",
    [string]$OutDir = "$PSScriptRoot\out"
)

$ErrorActionPreference = 'Stop'
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { throw "找不到 .NET Framework 编译器: $csc" }

New-Item -ItemType Directory -Force $OutDir | Out-Null
$references = @(
    (Join-Path $FibreCheckDir 'Oracle.ManagedDataAccess.dll'),
    (Join-Path $FibreCheckDir 'Toone.FibreCheck.Entites.CommonEntities.dll'),
    (Join-Path $FibreCheckDir 'Toone.FibreCheck.Entites.dll'),
    (Join-Path $FibreCheckDir 'BaseDAL.dll'),
    (Join-Path $FibreCheckDir 'OriRecord.dll'),
    (Join-Path $OdpEfDir 'Oracle.ManagedDataAccess.EntityFramework.dll'),
    'System.Data.Entity.dll',
    'System.Data.dll',
    'System.Core.dll',
    'System.Xml.dll'
)
$refArgs = $references | ForEach-Object { "-reference:`"$_`"" }

$sources = @(
    "$RunnerSrc\Db.cs",
    "$RunnerSrc\Redact.cs",
    "$RunnerSrc\MiniJson.cs",
    "$RunnerSrc\LegacyLoginFlow.cs",
    "$RunnerSrc\SystemDataConnection.cs",
    "$PSScriptRoot\src\WriterProgram.cs",
    "$PSScriptRoot\src\UploadExecutor.cs",
    "$PSScriptRoot\src\SpecialWoolContracts.cs",
    "$PSScriptRoot\src\LegacyXlsFileVerifier.cs",
    "$PSScriptRoot\src\SpecialWoolWriteLock.cs",
    "$PSScriptRoot\src\SpecialWoolExecutor.cs"
)

& $csc -nologo -target:exe -platform:x86 -utf8output -debug- -optimize+ `
    -out:"$OutDir\FibreCheckWriter.exe" `
    $refArgs `
    $sources
if ($LASTEXITCODE -ne 0) { throw "编译失败: $LASTEXITCODE" }

Copy-Item "$PSScriptRoot\app.config" "$OutDir\FibreCheckWriter.exe.config" -Force
Copy-Item (Join-Path $OdpEfDir 'Oracle.ManagedDataAccess.EntityFramework.dll') $OutDir -Force

# 32 位非托管 ODP.NET 运行时件（官方 DAL 路径需要 11.2 客户端）
if (Test-Path "$Odac32Dir\odp4bin\OraOps11w.dll") {
    Copy-Item "$Odac32Dir\odp4bin\OraOps11w.dll" $OutDir -Force
    Copy-Item "$Odac32Dir\instantclient_11_2\*.dll" $OutDir -Force
}
Write-Host "构建完成: $OutDir\FibreCheckWriter.exe"
