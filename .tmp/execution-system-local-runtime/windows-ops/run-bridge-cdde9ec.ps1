param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('upload', 'review', 'final-entry', 'paper-upload', 'paper-review', 'paper-final-entry')]
    [string]$Step,
    [string]$ControlledSampleNo = ''
)

$ErrorActionPreference = 'Stop'
$Root = 'C:\Users\lishuyang\Downloads\textile-device-monitor-cdde9ec'
$Repo = Join-Path $Root 'textile-device-monitor'
$LegacyRoot = 'C:\Users\lishuyang\Downloads\textile-device-monitor'
$Secrets = Join-Path $LegacyRoot '.tmp\execution-system-secrets\inspection-systems.env'
$Python = 'C:\Users\lishuyang\AppData\Roaming\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe'
$Bridge = Join-Path $Repo 'tools\legacy_fibrecheck_bridge\bridge.py'
$Writer = Join-Path $Repo 'tools\legacy_fibrecheck_writer\out\FibreCheckWriter.exe'
$FinalWriter = Join-Path $Repo 'tools\legacy_fibrecheck_final_entry_writer\out\FibreCheckFinalEntryWriter.exe'
$FibreCheck = Join-Path $LegacyRoot '.tmp\FibreCheck'
$Staging = Join-Path $Root '.tmp\execution-win-runtime\staging'
$FinalWork = Join-Path $Root '.tmp\execution-win-runtime\publish'
$StageSync = 'Z:\Downloads\exec-stage-sync'

Get-Content -LiteralPath $Secrets | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable(
            $matches[1].Trim(),
            $matches[2],
            'Process'
        )
    }
}
if ([string]::IsNullOrWhiteSpace($env:LEGACY_FIBRECHECK_LOGIN) -or
    [string]::IsNullOrWhiteSpace($env:LEGACY_FIBRECHECK_PASSWORD)) {
    throw 'Windows-local legacy credential is unavailable'
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

$Busy = @(
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @(
            'FibreCheckWriter.exe',
            'FibreCheckFinalEntryWriter.exe'
        ) -or (
            $_.Name -match '^python(\.exe)?$' -and
            $_.CommandLine -like '*legacy_fibrecheck_bridge\bridge.py*'
        )
    }
)
if ($Busy.Count -gt 0) {
    throw 'Another FibreCheck Bridge or Writer process is running'
}

$ExpectedWriterHashes = @{
    'FibreCheckWriter.exe' = 'de6541b1043a6fa95ca4dac5cc61d7c21a87a688a7deebad06927ca51257a26f'
    'FibreCheckFinalEntryWriter.exe' = '9bd509c61d6ddc40b7f1c0de9c11081a92461c0c22b8a372ce7a520cc7eeec03'
}
foreach ($pair in $ExpectedWriterHashes.GetEnumerator()) {
    $Path = if ($pair.Key -eq 'FibreCheckWriter.exe') { $Writer } else { $FinalWriter }
    $Actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne $pair.Value) {
        throw "Writer hash mismatch for $($pair.Key): $Actual"
    }
}

$SmbRemote = '\\192.168.105.82\fibrecheckfile$'
$SmbConnectionCreated = $false
if ($Step -in @('upload', 'final-entry', 'paper-upload', 'paper-review', 'paper-final-entry')) {
    if ([string]::IsNullOrWhiteSpace($env:SMB_USER_B) -or
        [string]::IsNullOrWhiteSpace($env:SMB_PASS_B)) {
        throw 'Windows-local SMB credential is unavailable'
    }
    $NativeSource = @'
using System;
using System.Runtime.InteropServices;

public static class SmbNativeMethods
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct NETRESOURCE
    {
        public int dwScope;
        public int dwType;
        public int dwDisplayType;
        public int dwUsage;
        public string lpLocalName;
        public string lpRemoteName;
        public string lpComment;
        public string lpProvider;
    }

    [DllImport("mpr.dll", CharSet = CharSet.Unicode)]
    public static extern int WNetAddConnection2(
        ref NETRESOURCE netResource,
        string password,
        string username,
        int flags);

    [DllImport("mpr.dll", CharSet = CharSet.Unicode)]
    public static extern int WNetCancelConnection2(
        string name,
        int flags,
        bool force);
}
'@
    if (-not ('SmbNativeMethods' -as [type])) {
        Add-Type -TypeDefinition $NativeSource
    }
    $Resource = New-Object SmbNativeMethods+NETRESOURCE
    $Resource.dwType = 1
    $Resource.lpRemoteName = $SmbRemote
    $ConnectionResult = [SmbNativeMethods]::WNetAddConnection2(
        [ref]$Resource,
        $env:SMB_PASS_B,
        $env:SMB_USER_B,
        0
    )
    if ($ConnectionResult -ne 0 -and $ConnectionResult -ne 1219) {
        throw "WNetAddConnection2 failed with code $ConnectionResult"
    }
    $SmbConnectionCreated = $ConnectionResult -eq 0
    if (-not (Test-Path -LiteralPath $SmbRemote -PathType Container)) {
        throw 'FibreCheck SMB root is not accessible'
    }
}

New-Item -ItemType Directory -Force -Path $Staging, $FinalWork | Out-Null
if (Test-Path -LiteralPath $StageSync -PathType Container) {
    & robocopy $StageSync $Staging /E /NFL /NDL /NJH /NJS | Out-Null
}

$BridgeArgs = @(
    $Bridge,
    '--api-base', 'http://10.211.55.2/api/execution/v1',
    '--bridge-id', "legacy-cdde9ec-$Step-01",
    '--token-env', 'EXECUTION_BRIDGE_TOKEN',
    '--writer', $Writer,
    '--fibrecheck-dir', $FibreCheck,
    '--account-env', 'LEGACY_FIBRECHECK_LOGIN',
    '--password-env', 'LEGACY_FIBRECHECK_PASSWORD',
    '--once'
)

if ($Step -in @('upload', 'paper-upload')) {
    $BridgeArgs += @('--source-root', "execution_staging=$Staging")
}
elseif ($Step -in @('final-entry', 'paper-final-entry')) {
    $BridgeArgs += @(
        '--final-entry-writer', $FinalWriter,
        '--final-entry-work-root', $FinalWork,
        '--source-root', "execution_staging=$Staging"
    )
    if ($Step -eq 'final-entry') {
        $env:FIBRECHECK_CONTROLLED_TEST_SAMPLE_NO = '260111037'
        $BridgeArgs += '--allow-controlled-final-entry-test-override'
    }
    if ($Step -eq 'paper-final-entry' -and $ControlledSampleNo) {
        $env:FIBRECHECK_CONTROLLED_TEST_SAMPLE_NO = $ControlledSampleNo
        $BridgeArgs += '--allow-controlled-final-entry-test-override'
    }
}
if ($Step -in @('paper-upload', 'paper-review', 'paper-final-entry')) {
    $PaperRoot = Join-Path $Staging 'paper_fiber_records'
    if (-not (Test-Path -LiteralPath $PaperRoot -PathType Container)) {
        throw 'Paper fiber records local staging root is missing'
    }
    $BridgeArgs += @('--source-root', "paper_fiber_records=$PaperRoot")
}

Write-Output "BRIDGE_STEP_START=$Step"
try {
    & $Python @BridgeArgs
    $ExitCode = $LASTEXITCODE
}
finally {
    if ($SmbConnectionCreated) {
        $null = [SmbNativeMethods]::WNetCancelConnection2(
            $SmbRemote,
            0,
            $false
        )
    }
}
Write-Output "BRIDGE_STEP_EXIT=$ExitCode"
if ($ExitCode -ne 0) {
    throw "Bridge returned exit code $ExitCode"
}
