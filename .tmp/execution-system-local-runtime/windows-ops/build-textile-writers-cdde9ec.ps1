$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$repo = 'C:\Users\lishuyang\Downloads\textile-device-monitor-cdde9ec'
$legacy = 'C:\Users\lishuyang\Downloads\textile-device-monitor'
$fibre = Join-Path $legacy '.tmp\FibreCheck'
$odac = Join-Path $legacy '.tmp\odac32'
$odpEf = Join-Path $legacy '.tmp\odp-ef\lib\net45'

$finalScript = Join-Path $repo 'textile-device-monitor\tools\legacy_fibrecheck_final_entry_writer\build.ps1'
$finalOut = Join-Path $repo 'textile-device-monitor\tools\legacy_fibrecheck_final_entry_writer\out'
& $finalScript -FibreCheckDir $fibre -Odac32Dir $odac -OutDir $finalOut

$writerScript = Join-Path $repo 'textile-device-monitor\tools\legacy_fibrecheck_writer\build.ps1'
$writerOut = Join-Path $repo 'textile-device-monitor\tools\legacy_fibrecheck_writer\out'
& $writerScript -FibreCheckDir $fibre -OdpEfDir $odpEf -Odac32Dir $odac -OutDir $writerOut

$finalExe = Join-Path $finalOut 'FibreCheckFinalEntryWriter.exe'
$writerExe = Join-Path $writerOut 'FibreCheckWriter.exe'
foreach ($path in @($finalExe, $writerExe)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Missing build output: $path"
    }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
    Write-Output ("built=" + $path)
    Write-Output ("sha256=" + $hash)
}
