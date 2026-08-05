Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "Test-MicroscopyWorkbookVerifier.Common.ps1")
& (Join-Path $PSScriptRoot "Test-MicroscopyWorkbookVerifier.Static.ps1")

Write-Host "PASS microscopy-workbook-verifier self-tests"
