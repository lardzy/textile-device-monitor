$ErrorActionPreference = 'Stop'
$zip = 'C:\Mac\Home\Downloads\textile-device-monitor-cdde9ec.zip'
$dest = 'C:\Users\lishuyang\Downloads\textile-device-monitor-cdde9ec'
if (Test-Path -LiteralPath $dest) {
    throw "Destination already exists: $dest"
}
Expand-Archive -LiteralPath $zip -DestinationPath $dest
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Write-Output ("zip_sha256=" + (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant())
Write-Output ("dest=" + $dest)
Get-ChildItem -LiteralPath $dest | Select-Object -First 10 -ExpandProperty Name
