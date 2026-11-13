#requires -Version 5.1
# Real 1.3.0 runtime + new installer Prepare logic, in temporary roots only.
$ErrorActionPreference='Stop'
function Assert($ok,$message) { if (!$ok) {throw $message} }
$InstallRoot=Join-Path $env:TEMP ('Bridge-upgrade-recovery-' + [guid]::NewGuid())
$ops=Join-Path $InstallRoot 'ops'
New-Item -ItemType Directory -Path $ops -Force | Out-Null
$env:TEXTILE_BRIDGE_DATA_ROOT=Join-Path $InstallRoot 'data'
Copy-Item (Join-Path $PSScriptRoot 'fixtures\BridgeRuntime-1.3.0.ps1') (Join-Path $ops 'BridgeRuntime.ps1')
Copy-Item (Join-Path $PSScriptRoot '..\runtime\ops\BridgeSettings.ps1') (Join-Path $ops 'BridgeSettings.ps1')
. (Join-Path $ops 'BridgeRuntime.ps1')
$user = (Get-CimInstance Win32_ComputerSystem).UserName
if (!$user) {
    $user = (Get-CimInstance Win32_UserAccount -Filter 'LocalAccount=TRUE AND Disabled=FALSE' |
        Where-Object { $_.SID -match '-[1-9][0-9]{3,}$' } | Select-Object -First 1).Caption
}
$settings=Initialize-BridgeSettings -InstallRoot $InstallRoot -Operator $user
$root=Get-BridgeDataRoot
$pending=Join-Path $root 'runtime\write.pending.json'
Write-BridgeRecord $pending @{AttemptId='legacy-attempt';OperationId='legacy-operation';Revision=7}
$before=[IO.File]::ReadAllText($pending)
Set-Content (Join-Path $InstallRoot 'manifest.json') '{"package":"textile-execution-bridge","version":"1.3.0"}' -Encoding UTF8
function Get-ScheduledTask {param($TaskName,$TaskPath,$ErrorAction) [pscustomobject]@{State='Ready'} }
function Get-CimInstance {param($ClassName,$Filter) return @() }
function Read-LegacyCheckpoint { return }
function Store-State($state) { $script:preparedState=$state }

# First prove the frozen old implementation reproduces the upgrade blocker.
$oldRejected=$false
try { Enter-BridgeMaintenance -TimeoutSeconds 1 } catch { $oldRejected=$true }
Assert $oldRejected 'The 1.3.0 fixture must reproduce pending receipt rejection'
Assert (Test-Path $pending) 'Old pending was lost'

$tokens=$null; $parseErrors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot '..\runtime\ops\BridgeInstall.ps1'),[ref]$tokens,[ref]$parseErrors)
Assert (!$parseErrors) 'Installer did not parse'
foreach ($name in @('Enter-InstallMaintenance','Prepare-Upgrade')) {
    $function=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$true)
    . ([scriptblock]::Create($function.Extent.Text))
}
Prepare-Upgrade
Assert ($script:preparedState.mode -eq 'managed' -and $script:preparedState.prepared) 'Managed Prepare did not complete'
Assert (([IO.File]::ReadAllText($pending)) -ceq $before) 'Installer must leave the receipt for the new bridge'
Assert (Test-Path (Join-Path $root 'config\maintenance.lock')) 'Prepare did not hold the update pause'
Exit-BridgeMaintenance

# A running Writer still must drain; receipt files alone are not running work.
function Get-CimInstance {
    param($ClassName,$Filter)
    [pscustomobject]@{Name='FibreCheckFinalEntryWriter.exe';ExecutablePath=(Join-Path $InstallRoot 'writers\FibreCheckFinalEntryWriter.exe');CommandLine='writer'}
}
$busyRejected=$false
try { Enter-InstallMaintenance -TimeoutSeconds 0 } catch {$busyRejected=$true}
Assert $busyRejected 'Installer overwrote a live Writer'
Assert (([IO.File]::ReadAllText($pending)) -ceq $before) 'Busy failure changed pending'
Write-Output 'PASS: real 1.3.0 pending upgrades through new Prepare; active Writer still drains'
