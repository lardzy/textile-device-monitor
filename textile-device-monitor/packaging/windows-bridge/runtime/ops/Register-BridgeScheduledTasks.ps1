# 注册两个桥的计划任务。两者都以“仅当用户登录时运行”方式注册：
# 写入桥必须处于交互会话（FinalEntry Writer 使用 COM Excel，Session 0 会被拒绝）；
# 快照桥为保持一致也采用同样方式。桥主机重启后需该用户登录（可配置自动登录）。
param(
    [Parameter(Mandatory = $true)]
    [string]$UserName
)

$ErrorActionPreference = 'Stop'
$InstallRoot = Split-Path $PSScriptRoot -Parent
$OpsDir = $PSScriptRoot

$Principal = New-ScheduledTaskPrincipal `
    -UserId $UserName `
    -LogonType Interactive `
    -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserName

$WriteAction = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$OpsDir\Invoke-WriteBridge.ps1`"" `
    -WorkingDirectory $InstallRoot
Register-ScheduledTask `
    -TaskName 'TextileExecutionWriteBridge' `
    -Action $WriteAction `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

$SnapshotAction = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$OpsDir\Invoke-SnapshotBridge.ps1`"" `
    -WorkingDirectory $InstallRoot
Register-ScheduledTask `
    -TaskName 'TextileExecutionSnapshotBridge' `
    -Action $SnapshotAction `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

Write-Output 'Registered scheduled tasks:'
Write-Output '  TextileExecutionWriteBridge   (AtLogOn, interactive)'
Write-Output '  TextileExecutionSnapshotBridge (AtLogOn, interactive)'
Write-Output 'Start now with: Start-ScheduledTask -TaskName <name>'
