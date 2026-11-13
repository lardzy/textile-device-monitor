param([switch]$Once)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'BridgeRuntime.ps1')
$InstallRoot = Split-Path $PSScriptRoot -Parent
Start-BridgeLogging Write
function Connect-BridgeShares($Config, $Secrets) {
    if (-not ('TextileBridgeSmb' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class TextileBridgeSmb {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct Resource {
        public int scope, type, display, usage;
        public string local, remote, comment, provider;
    }
    [DllImport("mpr.dll", CharSet=CharSet.Unicode)]
    public static extern int WNetAddConnection2(ref Resource resource, string password, string username, int flags);
}
'@
    }
    foreach ($target in $Config.SmbTargets) {
        $resource = New-Object TextileBridgeSmb+Resource
        $resource.type = 1
        $resource.remote = $target
        $rc = [TextileBridgeSmb]::WNetAddConnection2([ref]$resource, $Secrets.SMB_PASS_B, $Secrets.SMB_USER_B, 0)
        if ($rc -eq 1219) { throw "共享 $target 已有其他身份的连接，请先核对并关闭冲突连接" }
        if ($rc -ne 0) { throw "连接共享 $target 失败（Windows $rc）" }
    }
}
$run = {
    param($settings,$dataRoot)
    $cfg = $settings.config
    $null = Get-BridgeManifest $InstallRoot
    Connect-BridgeShares $cfg $settings.secrets
    [Environment]::SetEnvironmentVariable('EXECUTION_BRIDGE_TOKEN', $settings.secrets.EXECUTION_BRIDGE_TOKEN, 'Process')
    $finalWork = Join-Path $InstallRoot 'work\final-entry'
    New-Item -ItemType Directory -Force -Path $finalWork | Out-Null
    $bridgeArgs = @(
        '-u', (Join-Path $InstallRoot 'app\legacy_fibrecheck_bridge\bridge.py'),
        '--api-base', $cfg.ApiBase, '--bridge-id', $cfg.WriteBridgeId,
        '--token-env', 'EXECUTION_BRIDGE_TOKEN',
        '--writer', (Join-Path $InstallRoot 'writers\FibreCheckWriter\FibreCheckWriter.exe'),
        '--final-entry-writer', (Join-Path $InstallRoot 'writers\FibreCheckFinalEntryWriter\FibreCheckFinalEntryWriter.exe'),
        '--final-entry-work-root', $finalWork, '--fibrecheck-dir', (Join-Path $InstallRoot 'fibrecheck'),
        '--source-root', "execution_staging=$($cfg.ExecutionStagingPath)", '--source-root', "paper_fiber_records=$($cfg.PaperFiberRecordsPath)",
        '--poll-seconds', [string]$cfg.PollSeconds, '--managed-root', $dataRoot, '--settings-revision', [string]$settings.revision
    )
    if ($Once) { $bridgeArgs += '--once' }
    & (Join-Path $InstallRoot 'python\python.exe') @bridgeArgs | Out-Host
    return $LASTEXITCODE
}
exit (Invoke-BridgeSupervisor -Kind Write -Run $run -Once:$Once)
