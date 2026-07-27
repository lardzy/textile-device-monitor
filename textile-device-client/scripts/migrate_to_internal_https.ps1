[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ServerIp,

    [Parameter(Mandatory = $true)]
    [string]$RootCertificate,

    [string]$CaBundle,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedRootSha256,

    [string]$HostName = "textile-monitor.internal",

    [string[]]$AllowedAdditionalRootSha256 = @(),

    [string]$ExpectedServerRootSha256,

    [ValidateSet("Prepare", "Activate")]
    [string]$Phase = "Activate",

    [ValidateSet("compatible", "required")]
    [string]$TransportSecurity = "compatible",

    [string]$InstallDirectory = "$env:LOCALAPPDATA\TextileDeviceClient",

    [string]$StateDirectory = "$env:ProgramData\TextileDeviceMonitor\TlsMigration"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Certificate, hosts, PEM identity, client apply and audit transitions are
# implemented once in the shared deployment scripts copied into admin-tools.
$installTrustScript = Join-Path $PSScriptRoot "Install-InspectionTlsTrust.ps1"
$commonScript = Join-Path $PSScriptRoot "InspectionTls.Common.ps1"
$verificationScript = Join-Path $PSScriptRoot "verify_client_https_reporting.ps1"
$requiredScripts = @($installTrustScript, $commonScript)
if ($Phase -eq "Activate") {
    $requiredScripts += $verificationScript
}
foreach ($requiredScript in $requiredScripts) {
    if (-not (Test-Path -LiteralPath $requiredScript -PathType Leaf)) {
        throw "缺少 HTTPS 部署组件：$requiredScript。请使用完整正式安装包。"
    }
}

$baseInstallArguments = @{
    RootCertificatePath = $RootCertificate
    ExpectedRootSha256 = $ExpectedRootSha256
    ServerIp = $ServerIp
    Hostname = $HostName
    StateDirectory = $StateDirectory
}
if ($Phase -eq "Prepare") {
    & $installTrustScript @baseInstallArguments -StageOnly
    if (-not $?) {
        throw "HTTPS 信任预置失败。"
    }
    Write-Host (
        "客户端 HTTPS 信任预置完成；客户端配置尚未修改。" +
        "服务器 HTTPS 上线后请使用 -Phase Activate 执行激活。"
    ) -ForegroundColor Green
    return
}

if ([string]::IsNullOrWhiteSpace($CaBundle)) {
    throw "Activate 阶段必须提供 -CaBundle。"
}
$sourceCaBundle = [System.IO.Path]::GetFullPath($CaBundle)
if (-not (Test-Path -LiteralPath $sourceCaBundle -PathType Leaf)) {
    throw "Requests 根证书 PEM 不存在：$sourceCaBundle"
}

$configPath = Join-Path $InstallDirectory "config.json"
$executablePath = Join-Path $InstallDirectory "textile-device-client.exe"
$clientCaPath = Join-Path $InstallDirectory "certs\inspection-root-ca.pem"
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "客户端配置文件不存在：$configPath"
}
if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
    throw "客户端程序不存在：$executablePath"
}

try {
    $oldConfiguration = Get-Content `
        -LiteralPath $configPath `
        -Raw | ConvertFrom-Json
}
catch {
    throw "客户端配置不是有效 JSON，拒绝迁移：$_"
}
$oldTransportProperty = $oldConfiguration.PSObject.Properties[
    "transport_security"
]
if (
    $null -ne $oldTransportProperty -and
    [string]$oldTransportProperty.Value -ceq "required" -and
    $TransportSecurity -ceq "compatible"
) {
    throw (
        "当前客户端已经是 required，拒绝降级为 compatible。" +
        "如需续签或轮换证书，请显式使用 -TransportSecurity required。"
    )
}
$deviceCodeProperty = $oldConfiguration.PSObject.Properties["device_code"]
$deviceCode = if ($null -eq $deviceCodeProperty) {
    ""
}
else {
    [string]$deviceCodeProperty.Value
}
if ([string]::IsNullOrWhiteSpace($deviceCode)) {
    throw "客户端配置缺少 device_code，无法核验迁移后的状态上报。"
}
$reportInterval = 5
$reportIntervalProperty = $oldConfiguration.PSObject.Properties["report_interval"]
if ($null -ne $reportIntervalProperty) {
    $reportInterval = [int]$reportIntervalProperty.Value
}
$reportTimeoutSeconds = [Math]::Min(
    600,
    [Math]::Max(60, ($reportInterval * 2) + 20)
)
$verificationOwner = "migrate_to_internal_https.ps1"
$verificationResultPath = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ("textile-client-tls-verification-" + [guid]::NewGuid().ToString("N") + ".json")
$temporaryCaBundlePath = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ("textile-client-root-ca-" + [guid]::NewGuid().ToString("N") + ".pem")
$latestPath = Join-Path $StateDirectory "latest.json"
$latestManifestBefore = ""
if (Test-Path -LiteralPath $latestPath -PathType Leaf) {
    try {
        $previousLatest = Get-Content `
            -LiteralPath $latestPath `
            -Raw | ConvertFrom-Json
        $latestManifestBefore = [string]$previousLatest.manifest_path
    }
    catch {
        $latestManifestBefore = ""
    }
}
$externalVerificationPending = $false
$installStarted = $false
try {
    Copy-Item `
        -LiteralPath $sourceCaBundle `
        -Destination $temporaryCaBundlePath
    $activationArguments = @{
        RootCertificatePath = $RootCertificate
        RootPemPath = $temporaryCaBundlePath
        ExpectedRootSha256 = $ExpectedRootSha256
        ServerIp = $ServerIp
        Hostname = $HostName
        StateDirectory = $StateDirectory
        ClientConfigPath = $configPath
        ClientCaBundlePath = $clientCaPath
        ClientCaBundleConfigValue = "certs/inspection-root-ca.pem"
        TransportSecurity = $TransportSecurity
        ClientApplyMode = "External"
        ExternalVerificationOwner = $verificationOwner
        AllowedAdditionalRootSha256 = $AllowedAdditionalRootSha256
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedServerRootSha256)) {
        $activationArguments.ExpectedServerRootSha256 = $ExpectedServerRootSha256
    }
    $installStarted = $true
    & $installTrustScript @activationArguments
    if (-not $?) {
        throw "通用 HTTPS 信任安装脚本执行失败。"
    }
    $externalVerificationPending = $true

    Get-Process `
        -Name "textile-device-client" `
        -ErrorAction SilentlyContinue |
        Stop-Process -Force
    Start-Process `
        -FilePath $executablePath `
        -WorkingDirectory $InstallDirectory

    $currentPowerShell = (Get-Process -Id $PID).Path
    & $currentPowerShell `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $verificationScript `
        -DeviceCode $deviceCode `
        -HostName $HostName `
        -TimeoutSeconds $reportTimeoutSeconds `
        -ResultPath $verificationResultPath
    if ($LASTEXITCODE -ne 0) {
        throw "客户端状态上报核验失败（退出码 $LASTEXITCODE）。"
    }
    if (-not (Test-Path -LiteralPath $verificationResultPath -PathType Leaf)) {
        throw "客户端核验脚本未生成结构化结果。"
    }
    $verificationResult = Get-Content `
        -LiteralPath $verificationResultPath `
        -Raw | ConvertFrom-Json
    . $commonScript
    Complete-InspectionTlsExternalVerification `
        -StateDirectory $StateDirectory `
        -VerificationOwner $verificationOwner `
        -VerificationDetails @{
            device_code = [string]$verificationResult.device_code
            baseline_heartbeat = [string]$verificationResult.baseline_heartbeat
            last_heartbeat = [string]$verificationResult.last_heartbeat
            verified_at_utc = [string]$verificationResult.verified_at_utc
            transport_security = $TransportSecurity
        }
    $externalVerificationPending = $false
}
catch {
    $failure = $_
    if ($externalVerificationPending) {
        Get-Process `
            -Name "textile-device-client" `
            -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
        try {
            . $commonScript
            Fail-InspectionTlsExternalVerification `
                -StateDirectory $StateDirectory `
                -VerificationOwner $verificationOwner `
                -Failure ([string]$failure) `
                -VerificationDetails @{
                    device_code = $deviceCode
                    transport_security = $TransportSecurity
                    client_process_stopped = $true
                }
        }
        catch {
            Write-Warning "HTTPS 激活失败状态写入失败，请保护现场并人工检查迁移清单：$_"
        }
        throw (
            "HTTPS 已可信并写入客户端配置，但重启或状态上报核验失败。" +
            "为防止自动降级到旧 HTTP，客户端已停止且安全配置已保留。" +
            "请排查后重新执行 Activate。原始错误：$failure"
        )
    }

    # The common script records activation_failed itself if it wrote the HTTPS
    # configuration but failed before returning control to this wrapper.
    $latestStatus = ""
    $latestManifest = ""
    if (Test-Path -LiteralPath $latestPath -PathType Leaf) {
        try {
            $latestState = Get-Content `
                -LiteralPath $latestPath `
                -Raw | ConvertFrom-Json
            $latestStatus = [string]$latestState.status
            $latestManifest = [string]$latestState.manifest_path
        }
        catch {
            $latestStatus = ""
            $latestManifest = ""
        }
    }
    if (
        $installStarted -and
        $latestStatus -ceq "activation_failed" -and
        $latestManifest -cne $latestManifestBefore
    ) {
        Get-Process `
            -Name "textile-device-client" `
            -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
        throw (
            "HTTPS 配置激活失败。为防止自动降级到旧 HTTP，客户端已停止，" +
            "安全 HTTPS 配置与失败现场均已保留。原始错误：$failure"
        )
    }
    throw $failure
}
finally {
    Remove-Item `
        -LiteralPath $verificationResultPath `
        -Force `
        -ErrorAction SilentlyContinue
    Remove-Item `
        -LiteralPath $temporaryCaBundlePath `
        -Force `
        -ErrorAction SilentlyContinue
}

Write-Host (
    "客户端 HTTPS 迁移、重启及状态上报核验完成。" +
    "当前传输安全模式：$TransportSecurity"
) -ForegroundColor Green
