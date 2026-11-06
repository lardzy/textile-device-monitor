# 安装自检：不连接旧系统、不产生任何副作用，只校验本地物料完整性。
param([switch]$ProgramOnly)
$ErrorActionPreference = 'Stop'
$InstallRoot = Split-Path $PSScriptRoot -Parent
$Failures = 0

function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) { Write-Output "PASS  $Name  $Detail" }
    else { Write-Output "FAIL  $Name  $Detail"; $script:Failures++ }
}

$Manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $InstallRoot 'manifest.json') | ConvertFrom-Json
if (-not $ProgramOnly) {
    . (Join-Path $PSScriptRoot 'BridgeSettings.ps1')
    try {
        $Settings = Get-BridgeSettings -WithSecrets
        Check 'machine settings readable' ($Settings.revision -ge 0)
    } catch { Check 'machine settings readable' $false }
}

$Python = Join-Path $InstallRoot 'python\python.exe'
Check 'bundled python.exe' (Test-Path $Python)
if (Test-Path $Python) {
    $PyVer = & $Python --version 2>&1
    Check 'python runs' ($LASTEXITCODE -eq 0) "$PyVer"
    $env:PYTHONPATH = Join-Path $InstallRoot 'python-deps'
    & $Python -c "import oracledb" 2>$null
    Check 'oracledb importable' ($LASTEXITCODE -eq 0)
}

foreach ($Sub in @('FibreCheckWriter', 'FibreCheckFinalEntryWriter')) {
    $Name = "$Sub.exe"
    $Expected = $Manifest.writer_sha256.$Name
    $Path = Join-Path $InstallRoot "writers\$Sub\$Name"
    if (Test-Path $Path) {
        $Actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
        Check "$Name sha256" ($Expected -and $Actual -eq $Expected)
        $NativeDlls = @('oci.dll', 'OraOps11w.dll', 'oraociei11.dll', 'Oracle.DataAccess.dll')
        foreach ($dll in $NativeDlls) {
            Check "$Sub\$dll present" (Test-Path (Join-Path $InstallRoot "writers\$Sub\$dll"))
        }
    }
    else {
        Check "$Name exists" $false $Path
    }
}

Check 'fibrecheck\FibreCheck.exe' (Test-Path (Join-Path $InstallRoot 'fibrecheck\FibreCheck.exe'))
Check 'fibrecheck WebService.dll.config' (Test-Path (Join-Path $InstallRoot 'fibrecheck\WebService.dll.config'))
$WriteBridge = Join-Path $InstallRoot 'app\legacy_fibrecheck_bridge\bridge.py'
$ActualBridgeHash = if (Test-Path -LiteralPath $WriteBridge) {
    (Get-FileHash -LiteralPath $WriteBridge -Algorithm SHA256).Hash
}
Check 'bridge.py (write) sha256' ($ActualBridgeHash -and $ActualBridgeHash -eq $Manifest.write_bridge_sha256)
foreach ($relative in @('app/legacy_fibrecheck_bridge/managed.py','app/legacy_fibrecheck_task_snapshot_bridge/bridge.py')) {
    $file = Join-Path $InstallRoot $relative
    $pin = $Manifest.runtime_sha256.$relative
    Check "$relative sha256" ($pin -and (Test-Path -LiteralPath $file) -and (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -eq $pin)
}
foreach ($file in 'BridgeSettings','BridgeRuntime','BridgeControlCenter','Test-BridgeConnections','Register-BridgeScheduledTasks') {
    Check "$file.ps1 present" (Test-Path -LiteralPath (Join-Path $PSScriptRoot "$file.ps1"))
}
Check 'probe.py' (Test-Path (Join-Path $InstallRoot 'app\legacy_fibrecheck_probe\probe.py'))
$WriteTest = Join-Path $InstallRoot ('work\final-entry\.write-test-' + [guid]::NewGuid())
Check 'final-entry work dir writable' (
    (Test-Path (Join-Path $InstallRoot 'work\final-entry')) -and
    (New-Item -ItemType File -Path $WriteTest -ErrorAction SilentlyContinue) -ne $null
)
Remove-Item -LiteralPath $WriteTest -ErrorAction SilentlyContinue

$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319'
Check '.NET Framework 4.x runtime' (Test-Path $csc)

if ($Failures -gt 0) {
    Write-Output "RESULT: FAIL ($Failures)"
    exit 1
}
Write-Output 'RESULT: PASS'
exit 0
