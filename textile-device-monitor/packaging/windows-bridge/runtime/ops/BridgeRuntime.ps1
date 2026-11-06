#requires -Version 5.1
# Common control functions. Dot sourcing never registers, stops or starts tasks.
. (Join-Path $PSScriptRoot 'BridgeSettings.ps1')
$BridgeOpsRoot = $PSScriptRoot

function Get-BridgeTaskName([ValidateSet('Write','Snapshot')][string]$Kind) {
    "TextileExecution$((Get-Culture).TextInfo.ToTitleCase($Kind.ToLowerInvariant()))Bridge"
}

function Get-BridgeTask($Kind) {
    Get-ScheduledTask -TaskName (Get-BridgeTaskName $Kind) -TaskPath '\' -ErrorAction SilentlyContinue
}

function Read-BridgeRecord([string]$Path) {
    if ([IO.File]::Exists($Path)) {
        # Let the writer atomically replace the file while the view reads this generation.
        $stream = [IO.File]::Open($Path, 'Open', 'Read', [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
        $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8)
        try { $reader.ReadToEnd() | ConvertFrom-Json -ErrorAction Stop }
        finally { $reader.Dispose() }
    }
}

function Write-BridgeRecord([string]$Path, $Value) {
    $tmp = "$Path.$([guid]::NewGuid()).tmp"
    try {
        [IO.File]::WriteAllText($tmp, ($Value | ConvertTo-Json -Depth 5 -Compress), [Text.UTF8Encoding]::new($false))
        if ([IO.File]::Exists($Path)) {
            [IO.File]::Replace($tmp, $Path, [System.Management.Automation.Language.NullString]::Value)
        } else {
            [IO.File]::Move($tmp, $Path)
        }
    } finally { if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) } }
}

function Set-BridgeRuntimeStatus($Kind, $State, $Revision, $Message, $DataRoot) {
    $file = Join-Path $DataRoot ('runtime\' + $Kind.ToLowerInvariant() + '.json')
    Write-BridgeRecord $file @{
        State=$State; Revision=$Revision; Authenticated=$false; LastError=$Message; Pid=$PID
    }
}

function Open-BridgeSupervisorLock([string]$DataRoot, [string]$Kind) {
    [IO.File]::Open((Join-Path $DataRoot ('runtime\' + $Kind.ToLowerInvariant() + '.lock')), 'OpenOrCreate', 'ReadWrite', 'None')
}

function Test-BridgeOperatorLoggedOn([string]$Sid) {
    if (!$Sid) { return $false }
    foreach ($session in (Get-CimInstance Win32_LogonSession -Filter 'LogonType=2 OR LogonType=10 OR LogonType=11')) {
        foreach ($user in (Get-CimAssociatedInstance -InputObject $session -Association Win32_LoggedOnUser -ResultClassName Win32_UserAccount)) {
            if ($user.SID -eq $Sid) { return $true }
        }
    }
    return $false
}

function Get-BridgeRuntimeStatus {
    param([Parameter(Mandatory)][ValidateSet('Write','Snapshot')][string]$Kind,
          [string]$DataRoot = (Get-BridgeDataRoot))
    $key = $Kind.ToLowerInvariant()
    $record = Read-BridgeRecord (Join-Path $DataRoot "runtime\$key.json")
    $pending = Read-BridgeRecord (Join-Path $DataRoot "runtime\$key.pending.json")
    $settings = Get-BridgeSettings -DataRoot $DataRoot
    $task = Get-BridgeTask $Kind
    $enabled = [bool]$settings.enabled.$key
    $revision = [int]$record.Revision
    $errorText = [string]$record.LastError
    if ($pending -and ($record.State -eq 'receipt_unknown' -or $task.State -ne 'Running')) {
        $state = 'receipt_unknown'
    } elseif (Test-Path -LiteralPath (Join-Path $DataRoot 'config\maintenance.lock')) {
        $state = 'draining'
    } elseif (!$enabled) {
        $state = if ($task.State -eq 'Running') { 'draining' } else { 'stopped' }
    } elseif ($record.State -eq 'error') {
        $state = 'error'
    } elseif (!$task) {
        $state = 'error'; $errorText = '尚未注册计划任务，请重新启用'
    } elseif ($task.State -ne 'Running') {
        $state = if (Test-BridgeOperatorLoggedOn $settings.operator_sid) { 'stopped' } else { 'waiting_login' }
    } elseif ($revision -ne $settings.revision) {
        $state = 'applying'
    } elseif ($record.State) {
        $state = $record.State
    } else { $state = 'starting' }
    [pscustomobject]@{
        State=$state; Revision=$revision; TargetRevision=$settings.revision
        Authenticated=[bool]($record.Authenticated -and $record.State -in @('ready','working') -and $revision -eq $settings.revision -and $task.State -eq 'Running')
        LastError=$errorText; Operator=$settings.operator; Enabled=$enabled; TaskState=[string]$task.State; AttemptId=$pending.AttemptId
    }
}

function Get-BridgeManifest([string]$InstallRoot) {
    $manifest = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $InstallRoot 'manifest.json') -Raw | ConvertFrom-Json
    foreach ($name in @('FibreCheckWriter','FibreCheckFinalEntryWriter')) {
        $pin = $manifest.writer_sha256."$name.exe"
        $actual = (Get-FileHash -LiteralPath (Join-Path $InstallRoot "writers\$name\$name.exe") -Algorithm SHA256).Hash
        if (!$pin -or $actual -ne $pin) { throw "$name SHA-256 与安装清单不符" }
    }
    return $manifest
}

function Start-BridgeConfiguredTask($Kind, $Settings, $DataRoot = (Get-BridgeDataRoot)) {
    if (!$Settings.operator -or !$Settings.operator_sid) { throw '请先保存 Windows 运行账号' }
    $clock = [Diagnostics.Stopwatch]::StartNew()
    while ((Get-BridgeTask $Kind).State -eq 'Running') {
        # The retiring supervisor publishes this state *before* its final
        # recheck. If it already decided to exit, start a fresh instance when
        # the scheduler releases the old one (IgnoreNew drops overlapping starts).
        $record = Read-BridgeRecord (Join-Path $DataRoot ('runtime\' + $Kind.ToLowerInvariant() + '.json'))
        if ($record.State -notin @('stopped','draining','receipt_unknown')) { return }
        if ($clock.Elapsed.TotalSeconds -ge 15) { throw '旧计划任务仍在退出，请核对运行状态后重新启用' }
        Start-Sleep -Milliseconds 200
    }
    & (Join-Path $BridgeOpsRoot 'Register-BridgeScheduledTasks.ps1') -UserName $Settings.operator -Kind $Kind -RegisterOnly -DataRoot $DataRoot | Out-Null
    if (Test-BridgeOperatorLoggedOn $Settings.operator_sid) {
        Start-ScheduledTask -TaskName (Get-BridgeTaskName $Kind) -TaskPath '\' -ErrorAction Stop
    }
}

function Set-BridgeRunning {
    param([ValidateSet('Write','Snapshot')][string]$Kind,[bool]$Enabled,[int]$ExpectedRevision,
          [string]$DataRoot = (Get-BridgeDataRoot))
    $key = $Kind.ToLowerInvariant()
    if (Test-Path -LiteralPath (Join-Path $DataRoot 'config\maintenance.lock')) { throw '安装维护中，请稍后操作' }
    if ($Enabled) {
        if (Test-Path -LiteralPath (Join-Path $DataRoot "runtime\$key.pending.json")) {
            throw '有待确认的回执，请先在执行系统核对'
        }
        $s = Get-BridgeSettings -DataRoot $DataRoot -WithSecrets
        Assert-BridgeCanStart -Settings $s -Kind $key
        if ($Kind -eq 'Write') { $null = Get-BridgeManifest (Split-Path $BridgeOpsRoot -Parent) }
    }
    $s = Save-BridgeSettings -DataRoot $DataRoot -ExpectedRevision $ExpectedRevision -Enabled @{$key=$Enabled}
    if ($Enabled) { Start-BridgeConfiguredTask $Kind $s $DataRoot }
    return $s
}

function Assert-BridgeCanStart($Settings, $Kind) {
    $keys = @('EXECUTION_BRIDGE_TOKEN')
    if ($Kind -eq 'write') { $keys += 'LEGACY_FIBRECHECK_LOGIN','LEGACY_FIBRECHECK_PASSWORD','SMB_USER_B','SMB_PASS_B' }
    foreach ($key in $keys) {
        if ([string]::IsNullOrWhiteSpace($Settings.secrets[$key])) { throw "请填写 $key" }
    }
    if (!$Settings.operator_sid) { throw '请先保存 Windows 运行账号' }
}

function Sync-BridgeEnabledTasks($DataRoot) {
    if (!(Test-Path -LiteralPath (Join-Path $DataRoot 'config\settings.json'))) { return }
    $settings = Get-BridgeSettings -DataRoot $DataRoot
    foreach ($kind in @('Write','Snapshot')) {
        $key = $kind.ToLowerInvariant()
        if ($settings.enabled.$key -and !(Test-Path -LiteralPath (Join-Path $DataRoot "runtime\$key.pending.json"))) {
            Start-BridgeConfiguredTask $kind $settings $DataRoot
        }
    }
}

function Exit-BridgeMaintenance {
    param([string]$DataRoot = (Get-BridgeDataRoot), [string]$Owner = ('setup:' + $BridgeOpsRoot.ToLowerInvariant()))
    $pause = Join-Path $DataRoot 'config\maintenance.lock'
    if ([IO.File]::Exists($pause)) {
        if ((Read-BridgeRecord $pause).Owner -cne $Owner) { throw '另一维护操作持有暂停标记' }
        [IO.File]::Delete($pause)
    }
    Sync-BridgeEnabledTasks $DataRoot
}

function Get-BridgeLocalProcesses {
    param([string]$Kind)
    $root = Split-Path $BridgeOpsRoot -Parent
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='FibreCheckWriter.exe' OR Name='FibreCheckFinalEntryWriter.exe'" |
        Where-Object {
            ($_.ExecutablePath -and $_.ExecutablePath.StartsWith("$root\", [StringComparison]::OrdinalIgnoreCase) -and
                ($_.Name -ne 'python.exe' -or $_.CommandLine -match 'legacy_fibrecheck_(task_snapshot_)?bridge[\\/]bridge\.py') -and
                (!$Kind -or ($Kind -eq 'Snapshot') -eq ($_.CommandLine -match 'legacy_fibrecheck_task_snapshot_bridge[\\/]bridge\.py')))
        }
}

function Enter-BridgeMaintenance {
    param([string]$DataRoot = (Get-BridgeDataRoot), [int]$TimeoutSeconds=60,
          [string]$Owner = ('setup:' + $BridgeOpsRoot.ToLowerInvariant()))
    $pause = Join-Path $DataRoot 'config\maintenance.lock'
    # Publish a complete owner record in one rename. A new, serialized Inno
    # invocation for this installation may resume a previous interrupted drain.
    $created = $false
    $temp = "$pause.$([guid]::NewGuid()).tmp"
    try {
        Write-BridgeRecord $temp @{Owner=$Owner; Since=[DateTime]::UtcNow.ToString('o')}
        try {
            [IO.File]::Move($temp,$pause)
            $created=$true
        } catch [IO.IOException] {
            if (!(Test-Path -LiteralPath $pause) -or (Read-BridgeRecord $pause).Owner -cne $Owner) {
                throw '另一维护操作持有暂停标记'
            }
        }
    } finally { if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) } }
    try {
        $clock = [Diagnostics.Stopwatch]::StartNew()
        do {
            $busy = $false
            foreach ($kind in @('Write','Snapshot')) {
                try { (Open-BridgeSupervisorLock $DataRoot $kind).Dispose() }
                catch [IO.IOException] { $busy = $true; break }
                if ((Get-BridgeTask $kind).State -in @('Running','Queued')) { $busy=$true; break }
            }
            if (!$busy -and !(Get-BridgeLocalProcesses)) {
                foreach ($kind in @('write','snapshot')) {
                    if (Test-Path -LiteralPath (Join-Path $DataRoot "runtime\$kind.pending.json")) {
                        throw "桥 $kind 有待核对回执，请在执行系统完成对账后再升级"
                    }
                }
                return
            }
            Start-Sleep -Milliseconds 250
        } while ($clock.Elapsed.TotalSeconds -lt $TimeoutSeconds)
        throw '等待当前任务和回执超时，请稍后重试'
    } catch {
        $failure = $_
        if ($created) { Exit-BridgeMaintenance -DataRoot $DataRoot -Owner $Owner }
        throw $failure
    }
}

function Confirm-BridgeReconciledAttempt {
    param([ValidateSet('Write','Snapshot')][string]$Kind, [Parameter(Mandatory)][string]$AttemptId,
          [Parameter(Mandatory)][string]$Reason, [string]$DataRoot = (Get-BridgeDataRoot))
    $key = $Kind.ToLowerInvariant()
    $settings = Get-BridgeSettings -DataRoot $DataRoot
    if ($settings.enabled.$key) { throw '先停用此桥，再核对当前回执' }
    $lock = Open-BridgeSupervisorLock $DataRoot $kind
    try {
        if (Get-BridgeLocalProcesses -Kind $Kind) { throw '仍有 Bridge/Writer 子进程，请等待其退出后再确认' }
        $path = Join-Path $DataRoot "runtime\$key.pending.json"
        $record = Read-BridgeRecord $path
        if (!$record -or $record.AttemptId -cne $AttemptId -or !$Reason.Trim()) { throw '对账编号或原因不符' }
        $audit = Join-Path $DataRoot "runtime\$key.confirmed-$([Guid]::NewGuid()).json"
        Write-BridgeRecord $audit @{ Pending=$record; Reason=$Reason; Operator=[Security.Principal.WindowsIdentity]::GetCurrent().Name; Time=[DateTime]::UtcNow.ToString('o') }
        [IO.File]::Delete($path)
    } finally { $lock.Dispose() }
}


function Invoke-BridgeSupervisor {
    param([ValidateSet('Write','Snapshot')][string]$Kind, [scriptblock]$Run, [switch]$Once,
          [string]$DataRoot = (Get-BridgeDataRoot))
    $key = $Kind.ToLowerInvariant()
    $lock = Open-BridgeSupervisorLock $DataRoot $Kind
    $revision = 0
    try {
        while ($true) {
            if (Test-Path -LiteralPath (Join-Path $DataRoot 'config\maintenance.lock')) {
                Set-BridgeRuntimeStatus $Kind 'draining' $revision '' $DataRoot
                if (Test-Path -LiteralPath (Join-Path $DataRoot 'config\maintenance.lock')) { return 0 }
                continue
            }
            if (Test-Path -LiteralPath (Join-Path $DataRoot "runtime\$key.pending.json")) {
                Set-BridgeRuntimeStatus $Kind 'receipt_unknown' $revision '待核对回执，请先在执行系统对账' $DataRoot
                return 0
            }
            try {
                $settings = Get-BridgeSettings -WithSecrets -DataRoot $DataRoot
                if (!$settings.enabled.$key) {
                    Set-BridgeRuntimeStatus $Kind 'stopped' $settings.revision '' $DataRoot
                    if (!(Get-BridgeSettings -DataRoot $DataRoot).enabled.$key) { return 0 }
                    continue
                }
                $revision = $settings.revision
                Assert-BridgeCanStart $settings $key
                if (Get-BridgeLocalProcesses -Kind $Kind) { throw '此桥已有子进程，等待其安全退出' }
                Set-BridgeRuntimeStatus $Kind 'starting' $revision '' $DataRoot
                $code = & $Run $settings $DataRoot
                if ($Once) { return $code }
                # A task break or change of revision re-enters the loop immediately.
                if ($code -eq 20 -or $code -eq 22) { continue }
                if ($code -ne 21) {
                    Set-BridgeRuntimeStatus $Kind 'error' $revision "Bridge 子进程退出，代码 $code" $DataRoot
                }
            } catch {
                # Only messages deliberately created by our driver may contain details.
                Write-Warning ('Bridge 启动或配置检查失败：' + $_.Exception.Message)
                Set-BridgeRuntimeStatus $Kind 'error' $revision $_.Exception.Message $DataRoot
                if ($Once) { return 2 }
            }
            for ($i = 0; $i -lt 10; $i++) {
                if (Test-Path -LiteralPath (Join-Path $DataRoot 'config\maintenance.lock')) { break }
                Start-Sleep -Milliseconds 500
            }
        }
    } finally { $lock.Dispose() }
}

function Start-BridgeLogging($Kind) {
    $logDir = Join-Path (Split-Path $BridgeOpsRoot -Parent) 'work\logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    try {
        $prefix = $Kind.ToLowerInvariant() + '-bridge-'
        Start-Transcript -Path (Join-Path $logDir ($prefix + (Get-Date -Format 'yyyyMMdd') + '.log')) -Append -ErrorAction Stop | Out-Null
        Get-ChildItem $logDir -Filter ($prefix + '*.log') | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } | Remove-Item -Force -ErrorAction SilentlyContinue
    } catch { Write-Warning '无法初始化日志，请检查安装目录权限' }
}
