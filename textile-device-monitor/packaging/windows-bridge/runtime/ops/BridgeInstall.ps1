param(
    [Parameter(Mandatory)][ValidateSet('Prepare','Complete','Abort')][string]$Phase,
    [Parameter(Mandatory)][string]$InstallRoot,
    [Parameter(Mandatory)][string]$StateFile,
    [Parameter(Mandatory)][string]$ErrorFile,
    [switch]$LegacyIdle
)
$ErrorActionPreference = 'Stop'
$ops = Join-Path $InstallRoot 'ops'
$names = @{write='TextileExecutionWriteBridge'; snapshot='TextileExecutionSnapshotBridge'}
$checkpointKey = 'HKLM:\SOFTWARE\TextileExecutionBridge\Installer'

# This non-secret journal survives Inno rolling back a file copy or losing power.
# Only the installer may mutate it. It never contains credentials or task actions.
function Read-LegacyCheckpoint {
    $json = (Get-ItemProperty -LiteralPath $checkpointKey -ErrorAction SilentlyContinue).PendingLegacy
    if (!$json) { return }
    $record = $json | ConvertFrom-Json
    if ($record.root -ne $InstallRoot) { throw '另一安装目录还有未完成的旧版迁移，请先收尾。' }
    return $record
}
function Save-LegacyCheckpoint($State) {
    if (!(Test-Path -LiteralPath $checkpointKey)) { New-Item -Path $checkpointKey -Force | Out-Null }
    New-ItemProperty -LiteralPath $checkpointKey -Name PendingLegacy -PropertyType String -Force -Value ($State | ConvertTo-Json -Depth 5 -Compress) | Out-Null
}
function Clear-LegacyCheckpoint {
    if (Read-LegacyCheckpoint) { Remove-ItemProperty -LiteralPath $checkpointKey -Name PendingLegacy }
}

function Get-LegacyTasks {
    $rows = foreach ($kind in @('write','snapshot')) {
        $task = Get-ScheduledTask -TaskPath '\' -TaskName $names[$kind] -ErrorAction SilentlyContinue
        if ($task) {
            if (-not ($task.Actions | Where-Object { $_.Arguments -like "*$ops\Invoke-*Bridge.ps1*" })) {
                throw "同名计划任务并非来自此安装位置：$($task.TaskName)"
            }
            $principal = $task.Principal.UserId
            $sid = if ($principal -match '^S-1-') { $principal } else {
                ([Security.Principal.NTAccount]::new($principal)).Translate([Security.Principal.SecurityIdentifier]).Value
            }
            $loggedOn = $false
            foreach ($session in (Get-CimInstance Win32_LogonSession -Filter 'LogonType=2 OR LogonType=10 OR LogonType=11')) {
                if ((Get-CimAssociatedInstance -InputObject $session -Association Win32_LoggedOnUser -ResultClassName Win32_UserAccount).SID -contains $sid) { $loggedOn=$true; break }
            }
            [pscustomobject]@{kind=$kind; name=$task.TaskName; user=$sid; logged_on=$loggedOn;
                enabled=[bool]$task.Settings.Enabled; running=($task.State -eq 'Running')}
        }
    }
    return $rows
}

function Get-BridgeProcesses {
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('FibreCheckWriter.exe','FibreCheckFinalEntryWriter.exe') -or
        ($_.Name -match '^(python|powershell)\.exe$' -and $_.CommandLine -and
         $_.CommandLine.IndexOf($InstallRoot,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and
         $_.CommandLine -match '(legacy_fibrecheck_(task_snapshot_)?bridge[\\/]bridge\.py|Invoke-(Write|Snapshot)Bridge\.ps1|legacy_fibrecheck_probe[\\/]probe\.py)')
    }
}

function Restore-Legacy($saved) {
    foreach ($row in @($saved.tasks)) {
        if ($row.enabled) {
            Enable-ScheduledTask -TaskPath '\' -TaskName $row.name | Out-Null
            if ($row.running) { Start-ScheduledTask -TaskPath '\' -TaskName $row.name }
        }
    }
}

function Store-State($state) {
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $StateFile -Encoding UTF8
}

function Enter-InstallMaintenance([int]$TimeoutSeconds = 90) {
    # This function ships inside the new installer's temporary preflight script.
    # Do not call the installed 1.3.0 Enter-BridgeMaintenance: it rejects a
    # durable pending receipt even after every Writer has already exited.
    $dataRoot = Get-BridgeDataRoot
    $pause = Join-Path $dataRoot 'config\maintenance.lock'
    $owner = 'setup:' + $ops.ToLowerInvariant()
    $created = $false
    $temp = "$pause.$([guid]::NewGuid()).tmp"
    try {
        Write-BridgeRecord $temp @{Owner=$owner; Since=[DateTime]::UtcNow.ToString('o')}
        try { [IO.File]::Move($temp,$pause); $created=$true }
        catch [IO.IOException] {
            if (!(Test-Path -LiteralPath $pause) -or (Read-BridgeRecord $pause).Owner -cne $owner) {
                throw '另一维护操作持有暂停标记'
            }
        }
    } finally { if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) } }
    try {
        $clock = [Diagnostics.Stopwatch]::StartNew()
        do {
            $busy = $false
            foreach ($kind in @('Write','Snapshot')) {
                try { (Open-BridgeSupervisorLock $dataRoot $kind).Dispose() }
                catch [IO.IOException] { $busy=$true; break }
                if ((Get-BridgeTask $kind).State -in @('Running','Queued')) { $busy=$true; break }
            }
            if (!$busy -and !(Get-BridgeLocalProcesses)) { return }
            Start-Sleep -Milliseconds 250
        } while ($clock.Elapsed.TotalSeconds -lt $TimeoutSeconds)
        throw '正在等待当前 Writer 结束，请稍后重试升级'
    } catch {
        if ($created) { Exit-BridgeMaintenance -DataRoot $dataRoot -Owner $owner }
        throw
    }
}

function Prepare-Upgrade {
    $state = [ordered]@{root=$InstallRoot;mode='fresh';tasks=@();owner='';prepared=$false}
    $manifest = Join-Path $InstallRoot 'manifest.json'
    $checkpoint = Read-LegacyCheckpoint
    if ((Test-Path -LiteralPath $manifest) -or $checkpoint) {
        if (!(Test-Path -LiteralPath $manifest)) {throw '安装清单丢失，请先恢复原版本，再重试迁移。'}
        $current = Get-Content -LiteralPath $manifest -Encoding UTF8 -Raw | ConvertFrom-Json
        if ($current.package -ne 'textile-execution-bridge') {throw '安装目录中的清单不属于 Windows Bridge'}
        if ([version]$current.version -ge [version]'1.3.0' -and !$checkpoint) {
            $state.mode = 'managed'
            . (Join-Path $ops 'BridgeRuntime.ps1')
            Enter-InstallMaintenance -TimeoutSeconds 90
            $state.prepared = $true
            Store-State $state
            return
        }
        $state.mode = 'legacy'
        $currentTasks = @(Get-LegacyTasks)
        $state.tasks = $currentTasks
        $state.resuming = [bool]$checkpoint
        if ($checkpoint) { $state.tasks = @($checkpoint.tasks | Where-Object { $_ }) }
        $accounts = @($state.tasks | ForEach-Object {$_.user} | Select-Object -Unique)
        if ($accounts.Count -gt 1) {throw '旧两桥使用不同 Windows 账号。请先核对并统一运行账号。'}
        if ($accounts.Count -eq 1) {$state.owner = $accounts[0]}
        if (-not $LegacyIdle) {throw 'LEGACY_MAINTENANCE_REQUIRED'}
        if (@(Get-BridgeProcesses | Where-Object { $_.Name -match 'Writer\.exe$' -or ($_.CommandLine -match '[\\/]probe\.py' -and $_.CommandLine -notmatch '[\\/]bridge\.py') }).Count) {
            throw 'Writer 或探针仍在工作，请待本次处理完成后重试。'
        }
        Store-State $state
        Save-LegacyCheckpoint $state
        try {
            foreach ($row in $currentTasks) {
                Disable-ScheduledTask -TaskPath '\' -TaskName $row.name | Out-Null
                if ($row.running) {Stop-ScheduledTask -TaskPath '\' -TaskName $row.name}
            }
            for ($i=0; $i -lt 20 -and @(Get-BridgeProcesses).Count; $i++) {Start-Sleep -Milliseconds 250}
            if (@(Get-BridgeProcesses).Count) {throw '仍有旧桥进程，已停止覆盖。请完成维护窗口收尾后重试。'}
        } catch { if (!$checkpoint) {Restore-Legacy $state; Clear-LegacyCheckpoint}; throw }
    } elseif (@(Get-LegacyTasks).Count) {
        throw '检测到已有计划任务但找不到安装清单，请核对安装目录。'
    }
    $state.prepared = $true
    Store-State $state
}

try {
    if ($Phase -eq 'Prepare') { Prepare-Upgrade; exit 0 }
    $state = Get-Content -LiteralPath $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($Phase -eq 'Abort') {
        if ($state.prepared) {
            if ($state.mode -eq 'legacy' -and !$state.resuming) {Restore-Legacy $state; Clear-LegacyCheckpoint}
            if ($state.mode -eq 'managed') {. (Join-Path $ops 'BridgeRuntime.ps1'); Exit-BridgeMaintenance}
        }
        exit 0
    }
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $ops 'Test-BridgeInstallation.ps1') -ProgramOnly
    if ($LASTEXITCODE -ne 0) {throw '安装物料自检失败，桥保持停止；请重试安装或使用已备份的安装包回退。'}
    . (Join-Path $ops 'BridgeSettings.ps1')
    $init = @{InstallRoot=$InstallRoot}
    if ($state.owner) {$init.Operator = $state.owner}
    $settings = Initialize-BridgeSettings @init
    if ($state.mode -eq 'legacy' -and @($state.tasks).Count) {
        $enabled = @{write=$false; snapshot=$false}
        foreach ($row in $state.tasks) {
            # A registered task of a signed-out user resumes on their next logon.
            $enabled[$row.kind] = $row.enabled -and ($row.running -or -not $row.logged_on)
        }
        $settings = Save-BridgeSettings -ExpectedRevision $settings.revision -Enabled $enabled
    }
    if ($state.mode -eq 'managed' -or $settings.enabled.write -or $settings.enabled.snapshot) {
        . (Join-Path $ops 'BridgeRuntime.ps1')
        Exit-BridgeMaintenance
    }
    if ($state.mode -eq 'legacy') { Clear-LegacyCheckpoint }
    exit 0
} catch {
    [IO.File]::WriteAllText($ErrorFile,$_.Exception.Message,[Text.UTF8Encoding]::new($false))
    exit 1
}

