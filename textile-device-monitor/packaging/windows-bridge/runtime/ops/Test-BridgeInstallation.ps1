# 安装自检：不连接旧系统、不产生任何副作用，只校验本地物料完整性。
$ErrorActionPreference = 'Continue'
$InstallRoot = Split-Path $PSScriptRoot -Parent
$Failures = 0

function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) { Write-Output "PASS  $Name  $Detail" }
    else { Write-Output "FAIL  $Name  $Detail"; $script:Failures++ }
}

$ConfigPath = Join-Path $InstallRoot 'config\BridgeConfig.psd1'
Check 'BridgeConfig.psd1 exists' (Test-Path $ConfigPath)
$Config = Import-PowerShellDataFile -LiteralPath $ConfigPath

Check 'bridge.env exists' (Test-Path (Join-Path $InstallRoot 'config\bridge.env'))

$Python = Join-Path $InstallRoot 'python\python.exe'
Check 'bundled python.exe' (Test-Path $Python)
if (Test-Path $Python) {
    $PyVer = & $Python --version 2>&1
    Check 'python runs' ($LASTEXITCODE -eq 0) "$PyVer"
    $env:PYTHONPATH = Join-Path $InstallRoot 'python-deps'
    & $Python -c "import oracledb" 2>$null
    Check 'oracledb importable' ($LASTEXITCODE -eq 0)
}

foreach ($pair in $Config.WriterHashes.GetEnumerator()) {
    $Sub = if ($pair.Key -eq 'FibreCheckWriter.exe') { 'FibreCheckWriter' } else { 'FibreCheckFinalEntryWriter' }
    $Path = Join-Path $InstallRoot "writers\$Sub\$($pair.Key)"
    if (Test-Path $Path) {
        $Actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
        Check "$($pair.Key) sha256" ($Actual -eq $pair.Value.ToLowerInvariant())
        $NativeDlls = @('oci.dll', 'OraOps11w.dll', 'oraociei11.dll', 'Oracle.DataAccess.dll')
        foreach ($dll in $NativeDlls) {
            Check "$Sub\$dll present" (Test-Path (Join-Path $InstallRoot "writers\$Sub\$dll"))
        }
    }
    else {
        Check "$($pair.Key) exists" $false $Path
    }
}

Check 'fibrecheck\FibreCheck.exe' (Test-Path (Join-Path $InstallRoot 'fibrecheck\FibreCheck.exe'))
Check 'fibrecheck WebService.dll.config' (Test-Path (Join-Path $InstallRoot 'fibrecheck\WebService.dll.config'))
Check 'bridge.py (write)' (Test-Path (Join-Path $InstallRoot 'app\legacy_fibrecheck_bridge\bridge.py'))
Check 'bridge.py (snapshot)' (Test-Path (Join-Path $InstallRoot 'app\legacy_fibrecheck_task_snapshot_bridge\bridge.py'))
Check 'probe.py' (Test-Path (Join-Path $InstallRoot 'app\legacy_fibrecheck_probe\probe.py'))
Check 'final-entry work dir writable' (
    (Test-Path (Join-Path $InstallRoot 'work\final-entry')) -and
    (New-Item -ItemType File -Force -Path (Join-Path $InstallRoot 'work\final-entry\.write-test') -ErrorAction SilentlyContinue) -ne $null
)
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $InstallRoot 'work\final-entry\.write-test')

$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319'
Check '.NET Framework 4.x runtime' (Test-Path $csc)

if ($Failures -gt 0) {
    Write-Output "RESULT: FAIL ($Failures)"
    exit 1
}
Write-Output 'RESULT: PASS'
exit 0
