# 任务快照桥（只读）：把旧检务系统任务单信息刷入执行系统缓存。
# 只做只读 Oracle 查询，可在后台长期运行；与写入桥互不冲突。
param(
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
$InstallRoot = Split-Path $PSScriptRoot -Parent

. (Join-Path $PSScriptRoot 'Import-BridgeEnv.ps1') -EnvFile (Join-Path $InstallRoot 'config\bridge.env')
$Config = Import-PowerShellDataFile -LiteralPath (Join-Path $InstallRoot 'config\BridgeConfig.psd1')

if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable('EXECUTION_BRIDGE_TOKEN', 'Process'))) {
    throw 'bridge.env 缺少必需项: EXECUTION_BRIDGE_TOKEN'
}

$Busy = @(
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^python(\.exe)?$' -and
        $_.CommandLine -like '*legacy_fibrecheck_task_snapshot_bridge\bridge.py*'
    }
)
if ($Busy.Count -gt 0) {
    throw 'Another task snapshot bridge process is already running'
}

$Python = Join-Path $InstallRoot 'python\python.exe'
# probe 依赖（oracledb）随安装包放在 python-deps，加入 PYTHONPATH
$env:PYTHONPATH = Join-Path $InstallRoot 'python-deps'

$SnapshotArgs = @(
    (Join-Path $InstallRoot 'app\legacy_fibrecheck_task_snapshot_bridge\bridge.py'),
    '--api-base', $Config.ApiBase,
    '--bridge-id', $Config.SnapshotBridgeId,
    '--token-env', 'EXECUTION_BRIDGE_TOKEN',
    '--probe-python', $Python,
    '--probe-script', (Join-Path $InstallRoot 'app\legacy_fibrecheck_probe\probe.py'),
    '--fibrecheck-dir', (Join-Path $InstallRoot 'fibrecheck'),
    '--credential-profile', $Config.OracleCredentialProfile,
    '--data-source', $Config.OracleDataSource,
    '--poll-seconds', "$($Config.PollSeconds)"
)
$OracleClient = Join-Path $InstallRoot 'oracle-ic-x64\instantclient_19_31'
if (Test-Path -LiteralPath $OracleClient -PathType Container) {
    $SnapshotArgs += @('--oracle-client-dir', $OracleClient)
}
if ($Once) { $SnapshotArgs += '--once' }

Write-Output "SNAPSHOT_BRIDGE_START $(Get-Date -Format o)"
if ($Once) {
    & $Python @SnapshotArgs
    $ExitCode = $LASTEXITCODE
    Write-Output "SNAPSHOT_BRIDGE_EXIT=$ExitCode"
    exit $ExitCode
}

# 守护循环：后端重启等瞬断导致桥进程退出时自动拉起。
while ($true) {
    & $Python @SnapshotArgs
    $ExitCode = $LASTEXITCODE
    Write-Output "SNAPSHOT_BRIDGE_EXIT=$ExitCode $(Get-Date -Format o)（5 秒后重启）"
    Start-Sleep -Seconds 5
}
