#requires -Version 5.1
# Isolated test. Never registers or starts a real Windows task.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\runtime\ops\BridgeRuntime.ps1')
$env:TEXTILE_BRIDGE_DATA_ROOT = Join-Path $env:TEMP ('Bridge-runtime-test-' + [Guid]::NewGuid())
$root = Get-BridgeDataRoot
$install = Join-Path $env:TEMP ('Bridge-runtime-install-' + [Guid]::NewGuid())
New-Item -ItemType Directory -Path $install | Out-Null
$user = (Get-CimInstance Win32_ComputerSystem).UserName
if (!$user) {
    $user = (Get-CimInstance Win32_UserAccount -Filter 'LocalAccount=TRUE AND Disabled=FALSE' |
        Where-Object { $_.SID -match '-[1-9][0-9]{3,}$' } | Select-Object -First 1).Caption
}
$s = Initialize-BridgeSettings -InstallRoot $install -Operator $user
$actualStart = (Get-Command Start-BridgeConfiguredTask).ScriptBlock
function Assert($ok,$message) { if (!$ok) { throw $message } }
$script:taskState='Ready'
function Get-BridgeTask($Kind) { [pscustomobject]@{State=$script:taskState} }
function Test-BridgeOperatorLoggedOn($Sid) { return $false }
function Get-BridgeLocalProcesses { return }
function Start-BridgeConfiguredTask($Kind,$Settings) { $script:started += $Kind }
function Assert-BridgeCanStart($Settings,$Kind) { if (!$Settings.secrets.EXECUTION_BRIDGE_TOKEN) {throw 'test token missing'} }
function Get-BridgeManifest($InstallRoot) { return @{} }
$script:started = @()
$secrets = @{}
foreach ($key in @('EXECUTION_BRIDGE_TOKEN','LEGACY_FIBRECHECK_LOGIN','LEGACY_FIBRECHECK_PASSWORD','SMB_USER_B','SMB_PASS_B')) { $secrets[$key]='simulated' }
$s = Save-BridgeSettings -ExpectedRevision $s.revision -Secrets $secrets
$s = Set-BridgeRunning -Kind Write -Enabled $true -ExpectedRevision $s.revision
Assert ($s.enabled.write -and !$s.enabled.snapshot -and $script:started.Count -eq 1) 'Selective enable failed'
$state = Get-BridgeRuntimeStatus -Kind Write
Assert ($state.State -eq 'waiting_login' -and !$state.Authenticated) 'Should be waiting for login'
Write-BridgeRecord (Join-Path $root 'runtime\write.json') @{State='ready'; Revision=$s.revision; Authenticated=$true}
Write-BridgeRecord (Join-Path $root 'runtime\write.json') @{State='working'; Revision=$s.revision; Authenticated=$true}
Assert ((Read-BridgeRecord (Join-Path $root 'runtime\write.json')).State -eq 'working') 'Atomic update failed'
$script:taskState='Running'
Assert ((Get-BridgeRuntimeStatus -Kind Write).Authenticated) 'Missing successful authentication'
Write-BridgeRecord (Join-Path $root 'runtime\write.json') @{State='starting'; Revision=$s.revision; Authenticated=$false}
Assert (!(Get-BridgeRuntimeStatus -Kind Write).Authenticated) 'Saved token incorrectly implies authentication'
$script:taskState='Ready'
Enter-BridgeMaintenance -TimeoutSeconds 1
Assert (Test-Path -LiteralPath (Join-Path $root 'config\maintenance.lock')) 'Missing preinstall barrier'
Enter-BridgeMaintenance -TimeoutSeconds 1
$rejected = $false
try { Enter-BridgeMaintenance -TimeoutSeconds 1 -Owner other } catch { $rejected=$true }
Assert ($rejected -and (Test-Path -LiteralPath (Join-Path $root 'config\maintenance.lock'))) 'A competing updater removed a pause'
$rejected = $false
try { Set-BridgeRunning -Kind Write -Enabled $false -ExpectedRevision $s.revision } catch { $rejected=$true }
Assert $rejected 'Maintenance did not reject state change'
Exit-BridgeMaintenance
Assert ($script:started.Count -eq 2 -and $script:started[1] -eq 'Write') 'Resume changed snapshot state'
$hold = Open-BridgeSupervisorLock $root 'Write'
try {
    $rejected=$false
    try { Enter-BridgeMaintenance -TimeoutSeconds 1 } catch { $rejected=$true }
    Assert ($rejected -and !(Test-Path -LiteralPath (Join-Path $root 'config\maintenance.lock'))) 'Timed out maintenance left a barrier'
} finally { $hold.Dispose() }
$pending=Join-Path $root 'runtime\write.pending.json'
Write-BridgeRecord $pending @{AttemptId='test-1';Revision=$s.revision}
$rejected=$false
try { Enter-BridgeMaintenance -TimeoutSeconds 1 } catch { $rejected=$true }
Assert ($rejected -and (Test-Path $pending)) 'Unknown receipt allowed upgrade'
$s=Set-BridgeRunning -Kind Write -Enabled $false -ExpectedRevision $s.revision
$bad=$false
try { Confirm-BridgeReconciledAttempt -Kind Write -AttemptId 'wrong' -Reason 'synthetic' } catch { $bad=$true }
Assert ($bad -and (Test-Path $pending)) 'Wrong attempt cleared'
Confirm-BridgeReconciledAttempt -Kind Write -AttemptId 'test-1' -Reason 'synthetic reconciliation'
Assert (!(Test-Path $pending)) 'Confirmation did not clear pending marker'
$st = Get-BridgeRuntimeStatus -Kind Write
Assert ($st.State -eq 'stopped') 'Stopped task state lost'
# Supervisor returns a scalar code, stops before work while disabled.
$code = Invoke-BridgeSupervisor -Kind Write -Run { throw 'Unexpected claim' }
Assert ($code -eq 0) 'Disabled supervisor did work'
# Publication of stopping precedes the final intent read. An enable between
# those steps must preserve the existing worker.
$originalStatus=(Get-Command Set-BridgeRuntimeStatus).ScriptBlock
function Set-BridgeRuntimeStatus($Kind, $State, $Revision, $Message, $DataRoot) {
    & $originalStatus $Kind $State $Revision $Message $DataRoot
    if ($State -eq 'stopped') {
        $now=Get-BridgeSettings -DataRoot $DataRoot
        $null=Save-BridgeSettings -DataRoot $DataRoot -ExpectedRevision $now.revision -Enabled @{write=$true}
    }
}
$code=Invoke-BridgeSupervisor -Kind Write -Once -Run {42}
Assert ($code -eq 42) 'Enable committed before final stopped recheck was lost'
# If the old task already decided to exit, wait for the scheduler's Ready
# state. Use a harmless replacement registrar and start command.
$ops=Join-Path $install 'ops'
New-Item -ItemType Directory -Force $ops | Out-Null
$BridgeOpsRoot=$ops
Set-Content -LiteralPath (Join-Path $ops 'Register-BridgeScheduledTasks.ps1') -Value 'param($UserName,$Kind,[switch]$RegisterOnly,$DataRoot)' -Encoding UTF8
Write-BridgeRecord (Join-Path $root 'runtime\write.json') @{State='stopped';Revision=$s.revision;Authenticated=$false}
$script:checks=0; $script:starts=0
function Get-BridgeTask($Kind) {
    $script:checks++
    [pscustomobject]@{State=$(if ($script:checks -eq 1) {'Running'} else {'Ready'})}
}
function Test-BridgeOperatorLoggedOn($Sid) {return $true}
function Start-ScheduledTask($TaskName,$TaskPath,$ErrorAction) {$script:starts++}
& $actualStart Write (Get-BridgeSettings) $root
Assert ($script:checks -ge 2 -and $script:starts -eq 1) 'A dying scheduler instance swallowed the new start'
Write-Output 'PASS: selective enable, status, atomic record, upgrade drain, timeout rollback, unknown receipt, explicit settlement and disabled supervisor'

