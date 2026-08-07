$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$repo = 'C:\Users\lishuyang\Downloads\textile-device-monitor-cdde9ec'
$legacy = 'C:\Users\lishuyang\Downloads\textile-device-monitor'
$fibre = Join-Path $legacy '.tmp\FibreCheck'
$odac = Join-Path $legacy '.tmp\odac32'
$odpEf = Join-Path $legacy '.tmp\odp-ef\lib\net45'

& (Join-Path $repo 'textile-device-monitor\tools\legacy_fibrecheck_final_entry_writer\test.ps1') `
    -FibreCheckDir $fibre -Odac32Dir $odac
Write-Output 'final_entry_writer_tests=passed'

& (Join-Path $repo 'textile-device-monitor\tools\legacy_fibrecheck_writer\test.ps1') `
    -FibreCheckDir $fibre -OdpEfDir $odpEf -Odac32Dir $odac
Write-Output 'special_wool_writer_tests=passed'
