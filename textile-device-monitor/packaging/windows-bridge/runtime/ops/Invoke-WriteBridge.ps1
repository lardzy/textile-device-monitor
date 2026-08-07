# 写入桥：领取已批准的特种毛上传 / 特纤复核 / 检验记录登记外部操作并驱动 Writer。
# 必须在已登录用户的交互会话中运行（FinalEntry Writer 需要 COM Excel 交互会话），
# 由计划任务“仅当用户登录时运行”触发，或管理员手动运行。
param(
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
$InstallRoot = Split-Path $PSScriptRoot -Parent

. (Join-Path $PSScriptRoot 'Import-BridgeEnv.ps1') -EnvFile (Join-Path $InstallRoot 'config\bridge.env')
$Config = Import-PowerShellDataFile -LiteralPath (Join-Path $InstallRoot 'config\BridgeConfig.psd1')

foreach ($name in @('EXECUTION_BRIDGE_TOKEN', 'LEGACY_FIBRECHECK_LOGIN', 'LEGACY_FIBRECHECK_PASSWORD', 'SMB_USER_B', 'SMB_PASS_B')) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, 'Process'))) {
        throw "bridge.env 缺少必需项: $name"
    }
}

$Busy = @(
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('FibreCheckWriter.exe', 'FibreCheckFinalEntryWriter.exe') -or (
            $_.Name -match '^python(\.exe)?$' -and
            $_.CommandLine -like '*legacy_fibrecheck_bridge\bridge.py*'
        )
    }
)
if ($Busy.Count -gt 0) {
    throw 'Another FibreCheck Bridge or Writer process is already running'
}

$WriterDir = Join-Path $InstallRoot 'writers\FibreCheckWriter'
$FinalWriterDir = Join-Path $InstallRoot 'writers\FibreCheckFinalEntryWriter'
$Writer = Join-Path $WriterDir 'FibreCheckWriter.exe'
$FinalWriter = Join-Path $FinalWriterDir 'FibreCheckFinalEntryWriter.exe'
foreach ($pair in $Config.WriterHashes.GetEnumerator()) {
    $Path = if ($pair.Key -eq 'FibreCheckWriter.exe') { $Writer } else { $FinalWriter }
    $Actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne $pair.Value.ToLowerInvariant()) {
        throw "Writer hash mismatch for $($pair.Key): $Actual (upgrade BridgeConfig.psd1 pins after a package upgrade)"
    }
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
}
'@
if (-not ('SmbNativeMethods' -as [type])) {
    Add-Type -TypeDefinition $NativeSource
}
foreach ($Target in $Config.SmbTargets) {
    $Resource = New-Object SmbNativeMethods+NETRESOURCE
    $Resource.dwType = 1
    $Resource.lpRemoteName = $Target
    $Result = [SmbNativeMethods]::WNetAddConnection2(
        [ref]$Resource, $env:SMB_PASS_B, $env:SMB_USER_B, 0
    )
    if ($Result -ne 0 -and $Result -ne 1219) {
        throw "WNetAddConnection2($Target) failed with code $Result"
    }
}

$Python = Join-Path $InstallRoot 'python\python.exe'
$Bridge = Join-Path $InstallRoot 'app\legacy_fibrecheck_bridge\bridge.py'
$FinalWork = Join-Path $InstallRoot 'work\final-entry'
New-Item -ItemType Directory -Force -Path $FinalWork, (Join-Path $InstallRoot 'work\logs') | Out-Null

$BridgeArgs = @(
    $Bridge,
    '--api-base', $Config.ApiBase,
    '--bridge-id', $Config.WriteBridgeId,
    '--token-env', 'EXECUTION_BRIDGE_TOKEN',
    '--writer', $Writer,
    '--final-entry-writer', $FinalWriter,
    '--final-entry-work-root', $FinalWork,
    '--fibrecheck-dir', (Join-Path $InstallRoot 'fibrecheck'),
    '--account-env', 'LEGACY_FIBRECHECK_LOGIN',
    '--password-env', 'LEGACY_FIBRECHECK_PASSWORD',
    '--source-root', "execution_staging=$($Config.ExecutionStagingPath)",
    '--source-root', "paper_fiber_records=$($Config.PaperFiberRecordsPath)",
    '--poll-seconds', "$($Config.PollSeconds)"
)
if ($Once) { $BridgeArgs += '--once' }

Write-Output "WRITE_BRIDGE_START $(Get-Date -Format o)"
if ($Once) {
    & $Python @BridgeArgs
    $ExitCode = $LASTEXITCODE
    Write-Output "WRITE_BRIDGE_EXIT=$ExitCode"
    exit $ExitCode
}

# bridge.py 每完成一个任务即主动退出（一任务一进程的隔离设计），
# 因此长期驻留必须靠外层守护循环重启；空闲轮询异常（如后端重启）也一并兜底。
while ($true) {
    & $Python @BridgeArgs
    $ExitCode = $LASTEXITCODE
    Write-Output "WRITE_BRIDGE_EXIT=$ExitCode $(Get-Date -Format o)（5 秒后重启）"
    Start-Sleep -Seconds 5
}
