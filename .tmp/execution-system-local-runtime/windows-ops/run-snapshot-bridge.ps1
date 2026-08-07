$ErrorActionPreference = 'Stop'

$Root = 'C:\Users\lishuyang\Downloads\textile-device-monitor-cdde9ec'
$Repo = Join-Path $Root 'textile-device-monitor'
$LegacyRoot = 'C:\Users\lishuyang\Downloads\textile-device-monitor'
$Secrets = Join-Path $LegacyRoot '.tmp\execution-system-secrets\inspection-systems.env'
Get-Content -LiteralPath $Secrets | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable(
            $matches[1].Trim(),
            $matches[2],
            'Process'
        )
    }
}
if ([string]::IsNullOrWhiteSpace($env:LEGACY_FIBRECHECK_PASSWORD)) {
    throw 'Local legacy credential is unavailable'
}
$TokenMaterial = (
    'execution-bridge-local-test|260111037|' +
    $env:LEGACY_FIBRECHECK_PASSWORD
)
$TokenBytes = [Text.Encoding]::UTF8.GetBytes($TokenMaterial)
$TokenHash = [Security.Cryptography.SHA256]::Create().ComputeHash($TokenBytes)
$env:EXECUTION_BRIDGE_TOKEN = -join (
    $TokenHash | ForEach-Object { $_.ToString('x2') }
)

$ProbePython = Join-Path $Root '.tmp\probe-venv\Scripts\python.exe'
$ProbeScript = Join-Path $Repo 'tools\legacy_fibrecheck_probe\probe.py'
$SnapshotBridge = Join-Path $Repo 'tools\legacy_fibrecheck_task_snapshot_bridge\bridge.py'
$FibreCheck = Join-Path $LegacyRoot '.tmp\FibreCheck'
$OracleClient = Join-Path $LegacyRoot '.tmp\oracle-ic\instantclient_19_31'

& $ProbePython $SnapshotBridge `
    --api-base 'http://10.211.55.2/api/execution/v1' `
    --bridge-id 'task-snapshot-parallels-win11-cdde9ec-01' `
    --probe-python $ProbePython `
    --probe-script $ProbeScript `
    --fibrecheck-dir $FibreCheck `
    --oracle-client-dir $OracleClient `
    --credential-profile 'WebService.dll.config:PanYuJianWu' `
    --data-source '192.168.105.106/orcl' `
    --once

if ($LASTEXITCODE -ne 0) {
    throw "Task snapshot bridge failed: $LASTEXITCODE"
}
