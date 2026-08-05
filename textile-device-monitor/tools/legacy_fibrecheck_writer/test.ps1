$ErrorActionPreference = 'Stop'
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { throw "找不到 .NET Framework 编译器: $csc" }

$testOut = Join-Path $env:TEMP 'FibreCheckWriterContractSelfTest.exe'
& $csc -nologo -target:exe -utf8output -debug- -optimize+ `
    -reference:System.Data.dll `
    -reference:System.Core.dll `
    -out:$testOut `
    "$PSScriptRoot\..\legacy_fibrecheck_runner\src\Redact.cs" `
    "$PSScriptRoot\src\SpecialWoolContracts.cs" `
    "$PSScriptRoot\src\LegacyXlsFileVerifier.cs" `
    "$PSScriptRoot\tests\SelfTest.cs"
if ($LASTEXITCODE -ne 0) { throw "SelfTest 编译失败: $LASTEXITCODE" }

& $testOut
if ($LASTEXITCODE -ne 0) { throw "SelfTest 失败: $LASTEXITCODE" }
Remove-Item $testOut -Force
