param(
    [string]$FibreCheckDir = "$PSScriptRoot\..\..\..\.tmp\FibreCheck",
    [string]$RunnerSrc = "$PSScriptRoot\..\legacy_fibrecheck_runner\src",
    [string]$Odac32Dir = "$PSScriptRoot\..\..\..\.tmp\odac32",
    [string]$OutDir = "$PSScriptRoot\out"
)

$ErrorActionPreference = 'Stop'
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) {
    throw "32-bit .NET Framework compiler not found: $csc"
}

$references = @(
    (Join-Path $FibreCheckDir 'Oracle.ManagedDataAccess.dll'),
    (Join-Path $FibreCheckDir 'Toone.FibreCheck.Entites.CommonEntities.dll'),
    (Join-Path $FibreCheckDir 'Toone.FibreCheck.Entites.dll'),
    (Join-Path $FibreCheckDir 'BaseDAL.dll'),
    (Join-Path $FibreCheckDir 'BasicSetupDAL.dll'),
    (Join-Path $FibreCheckDir 'OriRecord.dll'),
    (Join-Path $FibreCheckDir 'BusinessProcessDAL.dll'),
    (Join-Path $FibreCheckDir 'BusinessProcessUI.dll'),
    'System.Data.Entity.dll',
    'System.Data.dll',
    'System.Core.dll',
    'System.Xml.dll',
    'System.Web.Extensions.dll'
)
foreach ($reference in $references) {
    if ($reference -match '^[A-Za-z]:\\' -and -not (Test-Path -LiteralPath $reference)) {
        throw "Missing compile reference: $reference"
    }
}
$refArgs = $references | ForEach-Object { "-reference:`"$_`"" }

$sources = @(
    (Join-Path $RunnerSrc 'Db.cs'),
    (Join-Path $RunnerSrc 'Redact.cs'),
    (Join-Path $RunnerSrc 'MiniJson.cs'),
    (Join-Path $RunnerSrc 'LegacyLoginFlow.cs'),
    (Join-Path $RunnerSrc 'SystemDataConnection.cs')
)
$sources += Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'src') -Filter '*.cs' |
    Sort-Object Name |
    Select-Object -ExpandProperty FullName
foreach ($source in $sources) {
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Missing source file: $source"
    }
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$exe = Join-Path $OutDir 'FibreCheckFinalEntryWriter.exe'
& $csc -nologo -target:exe -platform:x86 -codepage:65001 -utf8output -debug- -optimize+ `
    -out:"$exe" `
    $refArgs `
    $sources
if ($LASTEXITCODE -ne 0) {
    throw "Compilation failed: $LASTEXITCODE"
}

Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'app.config') `
    -Destination "$exe.config" -Force

# The official DAL ultimately uses 32-bit Oracle.DataAccess 11.2. Managed
# FibreCheck assemblies are loaded from --fibrecheck-dir by AssemblyResolve.
if ([string]::IsNullOrWhiteSpace($Odac32Dir)) {
    throw 'ODAC directory argument is empty'
}
$oraOpsPath = Join-Path $Odac32Dir 'odp4bin\OraOps11w.dll'
$instantClientDir = Join-Path $Odac32Dir 'instantclient_11_2'
if (-not (Test-Path -LiteralPath $oraOpsPath) -or -not (Test-Path -LiteralPath $instantClientDir)) {
    throw "Missing 32-bit ODAC runtime: $Odac32Dir"
}
Copy-Item -LiteralPath $oraOpsPath -Destination $OutDir -Force
Get-ChildItem -LiteralPath $instantClientDir -Filter '*.dll' | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $OutDir -Force
}

Write-Host "Build completed: $exe"
