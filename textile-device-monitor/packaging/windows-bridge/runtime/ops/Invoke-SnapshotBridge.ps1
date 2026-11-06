param([switch]$Once)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'BridgeRuntime.ps1')
$InstallRoot = Split-Path $PSScriptRoot -Parent
Start-BridgeLogging Snapshot
$run = {
    param($settings,$dataRoot)
    $cfg = $settings.config
    $env:EXECUTION_BRIDGE_TOKEN = $settings.secrets.EXECUTION_BRIDGE_TOKEN
    $env:PYTHONPATH = Join-Path $InstallRoot 'python-deps'
    $python = Join-Path $InstallRoot 'python\python.exe'
    $bridgeArgs = @(
        '-u', (Join-Path $InstallRoot 'app\legacy_fibrecheck_task_snapshot_bridge\bridge.py'),
        '--api-base', $cfg.ApiBase, '--bridge-id', $cfg.SnapshotBridgeId,
        '--token-env', 'EXECUTION_BRIDGE_TOKEN', '--probe-python', $python,
        '--probe-script', (Join-Path $InstallRoot 'app\legacy_fibrecheck_probe\probe.py'),
        '--fibrecheck-dir', (Join-Path $InstallRoot 'fibrecheck'), '--credential-profile', $cfg.OracleCredentialProfile,
        '--data-source', $cfg.OracleDataSource, '--poll-seconds', [string]$cfg.PollSeconds,
        '--managed-root', $dataRoot, '--settings-revision', [string]$settings.revision
    )
    $client = Join-Path $InstallRoot 'oracle-ic-x64\instantclient_19_31'
    if (Test-Path -LiteralPath $client -PathType Container) { $bridgeArgs += @('--oracle-client-dir', $client) }
    if ($Once) { $bridgeArgs += '--once' }
    & $python @bridgeArgs | Out-Host
    return $LASTEXITCODE
}
exit (Invoke-BridgeSupervisor -Kind Snapshot -Run $run -Once:$Once)
