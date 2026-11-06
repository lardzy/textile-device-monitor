param(
    [Parameter(Mandatory)][string]$UserName,
    [ValidateSet('Both','Write','Snapshot')][string]$Kind='Both',
    [switch]$RegisterOnly,
    [string]$DataRoot
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'BridgeRuntime.ps1')
$install = Split-Path $PSScriptRoot -Parent
$principal = New-ScheduledTaskPrincipal -UserId $UserName -LogonType Interactive -RunLevel Highest
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserName
$settings = Get-BridgeSettings -DataRoot $DataRoot
$kinds = if ($Kind -eq 'Both') { 'Write','Snapshot' } else { $Kind }
foreach ($component in $kinds) {
    $name = Get-BridgeTaskName $component
    $entry = Join-Path $PSScriptRoot "Invoke-$($component)Bridge.ps1"
    $arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $entry + '"'
    $task = Get-BridgeTask $component
    if ($task.State -eq 'Running') { continue }
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $install
    Register-ScheduledTask -TaskPath '\' -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $taskSettings -Force | Out-Null
    $key = $component.ToLowerInvariant()
    if (!$RegisterOnly -and $settings.enabled.$key -and (Test-BridgeOperatorLoggedOn $settings.operator_sid)) {
        Start-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction Stop
    }
}
