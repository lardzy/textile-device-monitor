[CmdletBinding()]
param(
    [string]$Hostname = "textile-monitor.internal"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "InspectionTls.Common.ps1")

Assert-InspectionHostname -Hostname $Hostname
if ($env:OS -ne "Windows_NT") {
    throw "此只读检查只能在 Windows 终端执行。"
}

function Get-RegistryProxySnapshot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{
            label = $Label
            exists = $false
        }
    }
    $item = Get-ItemProperty -LiteralPath $Path
    $values = [ordered]@{
        label = $Label
        exists = $true
    }
    foreach ($name in @(
        "ProxyEnable",
        "ProxyServer",
        "ProxyOverride",
        "AutoConfigURL",
        "AutoDetect",
        "ProxyMode",
        "ProxyPacUrl",
        "ProxyBypassList"
    )) {
        if ($item.PSObject.Properties.Name -contains $name) {
            $values[$name] = $item.$name
        }
    }
    return [pscustomobject]$values
}

$snapshots = @(
    Get-RegistryProxySnapshot `
        -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" `
        -Label "当前用户 WinINET"
    Get-RegistryProxySnapshot `
        -Path "HKCU:\Software\Policies\Google\Chrome" `
        -Label "当前用户 Chrome 策略"
    Get-RegistryProxySnapshot `
        -Path "HKLM:\Software\Policies\Google\Chrome" `
        -Label "计算机 Chrome 策略"
    Get-RegistryProxySnapshot `
        -Path "HKCU:\Software\Policies\Microsoft\Edge" `
        -Label "当前用户 Edge 策略"
    Get-RegistryProxySnapshot `
        -Path "HKLM:\Software\Policies\Microsoft\Edge" `
        -Label "计算机 Edge 策略"
)

$winHttp = @(& netsh.exe winhttp show proxy 2>&1)
$hasProxySignal = $false
foreach ($snapshot in $snapshots) {
    if (-not [bool]$snapshot.exists) {
        continue
    }
    foreach ($name in @(
        "ProxyServer",
        "AutoConfigURL",
        "ProxyPacUrl"
    )) {
        $property = $snapshot.PSObject.Properties[$name]
        if (
            $null -ne $property -and
            -not [string]::IsNullOrWhiteSpace([string]$property.Value)
        ) {
            $hasProxySignal = $true
        }
    }
    foreach ($name in @("ProxyEnable", "AutoDetect")) {
        $property = $snapshot.PSObject.Properties[$name]
        if ($null -ne $property -and [int]$property.Value -ne 0) {
            $hasProxySignal = $true
        }
    }
    $proxyMode = $snapshot.PSObject.Properties["ProxyMode"]
    if (
        $null -ne $proxyMode -and
        [string]$proxyMode.Value -notin @("", "direct", "system")
    ) {
        $hasProxySignal = $true
    }
}

$result = [ordered]@{
    schema_version = 1
    checked_at_utc = [datetime]::UtcNow.ToString("o")
    hostname = $Hostname
    registry = $snapshots
    winhttp_output = $winHttp
    proxy_or_pac_signal_detected = $hasProxySignal
    status = "manual_browser_confirmation_required"
    required_action = (
        "在 Chrome/Edge 灰度终端确认该主机名走 DIRECT。" +
        "若公司 PAC/本机代理接管，请仅由终端管理员为 " +
        "$Hostname 添加本机代理例外；本脚本不会修改代理、PAC 或公司策略。"
    )
}
$result | ConvertTo-Json -Depth 8
Write-Warning (
    "hosts 与根 CA 不代表浏览器一定直连。Chrome/Edge 仍可能受 " +
    "Windows 代理或 PAC 影响；上线门禁必须人工确认 " +
    "$Hostname 为 DIRECT。"
)
