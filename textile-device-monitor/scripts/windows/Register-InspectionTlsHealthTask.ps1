[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ServerIp,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^(?:[0-9A-Fa-f]{64}|(?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2})$")]
    [string]$ExpectedRootSha256,

    [string]$Hostname = "textile-monitor.internal",

    [int]$HttpsPort = 443,

    [string]$HealthPath = "/health/ready",

    [string]$TaskName = "TextileDeviceMonitor-TlsHealth",

    [datetime]$DailyAt = [datetime]::Today.AddHours(3),

    [string]$InstallDirectory = "$env:ProgramData\TextileDeviceMonitor\TlsHealth",

    [string]$LogPath = "$env:ProgramData\TextileDeviceMonitor\Logs\tls-health.jsonl",

    [switch]$RunNow
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "InspectionTls.Common.ps1")

Assert-WindowsAdministrator
Assert-InspectionHostname -Hostname $Hostname
$normalizedIp = ConvertTo-NormalizedIpAddress -ServerIp $ServerIp
$normalizedRoot = $ExpectedRootSha256.Replace(":", "").ToUpperInvariant()
$targetDirectory = [System.IO.Path]::GetFullPath($InstallDirectory)
New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null

$installedCommon = Join-Path $targetDirectory "InspectionTls.Common.ps1"
$installedCheck = Join-Path $targetDirectory "Test-InspectionTlsHealth.ps1"
Copy-Item `
    -LiteralPath (Join-Path $PSScriptRoot "InspectionTls.Common.ps1") `
    -Destination $installedCommon `
    -Force
Copy-Item `
    -LiteralPath (Join-Path $PSScriptRoot "Test-InspectionTlsHealth.ps1") `
    -Destination $installedCheck `
    -Force

$windowsPowerShell = Join-Path `
    $env:SystemRoot `
    "System32\WindowsPowerShell\v1.0\powershell.exe"
$escapedScript = $installedCheck.Replace('"', '""')
$escapedLog = ([System.IO.Path]::GetFullPath($LogPath)).Replace('"', '""')
$arguments = @(
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$escapedScript`"",
    "-ServerIp", "`"$normalizedIp`"",
    "-ExpectedRootSha256", "`"$normalizedRoot`"",
    "-Hostname", "`"$Hostname`"",
    "-HttpsPort", "$HttpsPort",
    "-HealthPath", "`"$HealthPath`"",
    "-LogPath", "`"$escapedLog`""
) -join " "

$action = New-ScheduledTaskAction `
    -Execute $windowsPowerShell `
    -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Daily -At $DailyAt
$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "每日验证纺织设备监控系统 HTTPS、证书链、主机名和有效期" `
    -Force | Out-Null

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
}

Write-Host "TLS 每日检查任务已创建或更新：$TaskName"
Write-Host "服务器：$normalizedIp ($Hostname)"
Write-Host "每日时间：$($DailyAt.ToString('HH:mm'))"
Write-Host "日志：$LogPath"
