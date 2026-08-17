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

# 注册后立即尝试启动；交互型任务要求目标用户当前已登录，
# 未登录时启动会失败，等其登录后由 AtLogOn 触发器拉起。
foreach ($Name in @('TextileExecutionWriteBridge', 'TextileExecutionSnapshotBridge')) {
    try {
        Start-ScheduledTask -TaskName $Name -ErrorAction Stop
        Write-Output "Started: $Name"
    }
    catch {
        Write-Output "WARN: $Name 未能立即启动（$_）；确认用户已登录后可手动 Start-ScheduledTask -TaskName $Name"
    }
}
$Current = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($Current -notlike "*\$UserName" -and $Current -ne $UserName) {
    Write-Output "WARN: 当前登录用户 ($Current) 与注册用户 ($UserName) 不一致；建议 -UserName 直接使用 whoami 的输出。"
}
