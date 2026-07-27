[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RootCertificatePath,

    [Parameter(Mandatory = $true)]
    [string]$ServerIp,

    [string]$Hostname = "textile-monitor.internal",

    [int]$HttpsPort = 443,

    [string]$HealthPath = "/health/ready",

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^(?:[0-9A-Fa-f]{64}|(?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2})$")]
    [string]$ExpectedRootSha256,

    [string]$StateDirectory = "$env:ProgramData\TextileDeviceMonitor\TlsMigration",

    [string]$ClientConfigPath,

    [string]$RootPemPath,

    [string]$ClientCaBundlePath,

    [string]$ClientCaBundleConfigValue,

    [string[]]$AllowedAdditionalRootSha256 = @(),

    [string]$ExpectedServerRootSha256,

    [ValidateSet("compatible", "required")]
    [string]$TransportSecurity = "compatible",

    [ValidateSet("HotReload", "Restart", "External")]
    [string]$ClientApplyMode,

    [string]$ClientProcessName,

    [string]$ClientExecutablePath,

    [string[]]$ClientExecutableArgument = @(),

    [int]$ClientApplyWaitSeconds = 5,

    [string]$ClientVerificationScriptPath,

    [string[]]$ClientVerificationArgument = @(),

    [string]$ExternalVerificationOwner,

    [switch]$StageOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "InspectionTls.Common.ps1")

Assert-WindowsAdministrator
Assert-InspectionHostname -Hostname $Hostname
$normalizedIp = ConvertTo-NormalizedIpAddress -ServerIp $ServerIp
$rootPath = [System.IO.Path]::GetFullPath($RootCertificatePath)
if (-not (Test-Path -LiteralPath $rootPath -PathType Leaf)) {
    throw "根证书文件不存在：$rootPath"
}

$rootCertificate = New-Object `
    -TypeName System.Security.Cryptography.X509Certificates.X509Certificate2 `
    -ArgumentList @($rootPath)
if ($rootCertificate.HasPrivateKey) {
    throw "安全拒绝：终端安装包中的根证书不得包含私钥。"
}
if ($rootCertificate.Subject -cne $rootCertificate.Issuer) {
    throw "根证书 Subject 与 Issuer 不一致，不是自签名根证书。"
}
$basicConstraintsExtension = $rootCertificate.Extensions |
    Where-Object { $_.Oid.Value -eq "2.5.29.19" } |
    Select-Object -First 1
if ($null -eq $basicConstraintsExtension) {
    throw "根证书缺少 Basic Constraints。"
}
$basicConstraints = New-Object `
    -TypeName System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension `
    -ArgumentList @(
        $basicConstraintsExtension,
        $basicConstraintsExtension.Critical
    )
if (-not $basicConstraints.CertificateAuthority -or -not $basicConstraints.Critical) {
    throw "根证书必须声明 critical CA:TRUE。"
}
if ($rootCertificate.PublicKey.Oid.Value -cne "1.2.840.113549.1.1.1") {
    throw "根证书公钥算法必须为 RSA（1.2.840.113549.1.1.1）。"
}
if ($rootCertificate.PublicKey.Key.KeySize -ne 3072) {
    throw "根证书 RSA 公钥必须为 3072 位。"
}
if (
    [datetime]::UtcNow -lt $rootCertificate.NotBefore.ToUniversalTime() -or
    [datetime]::UtcNow -ge $rootCertificate.NotAfter.ToUniversalTime()
) {
    throw "根证书尚未生效或已经过期。"
}

$rootSha256 = Get-CertificateSha256 -Certificate $rootCertificate
if ($rootSha256 -cne $ExpectedRootSha256.Replace(":", "").ToUpperInvariant()) {
    throw "根证书 SHA-256 指纹不匹配。实际：$rootSha256"
}
$serverRootSha256 = if ([string]::IsNullOrWhiteSpace(
    $ExpectedServerRootSha256
)) {
    $rootSha256
}
else {
    if (
        $ExpectedServerRootSha256 -notmatch
        "^(?:[0-9A-Fa-f]{64}|(?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2})$"
    ) {
        throw "ExpectedServerRootSha256 不是有效 SHA-256 指纹。"
    }
    $ExpectedServerRootSha256.Replace(":", "").ToUpperInvariant()
}
$normalizedAdditionalRoots = @(
    foreach ($fingerprint in $AllowedAdditionalRootSha256) {
        if (
            $fingerprint -notmatch
            "^(?:[0-9A-Fa-f]{64}|(?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2})$"
        ) {
            throw "AllowedAdditionalRootSha256 包含无效指纹：$fingerprint"
        }
        $fingerprint.Replace(":", "").ToUpperInvariant()
    }
) | Sort-Object -Unique
if ($normalizedAdditionalRoots -contains $rootSha256) {
    throw "AllowedAdditionalRootSha256 不应重复本次安装的根指纹。"
}

if (-not [string]::IsNullOrWhiteSpace($ClientConfigPath)) {
    if ($StageOnly) {
        throw "StageOnly 仅预置根证书和 hosts，不允许修改客户端配置。"
    }
    if ([string]::IsNullOrWhiteSpace($ClientApplyMode)) {
        throw "迁移客户端配置时必须明确指定 ClientApplyMode（HotReload 或 Restart）。"
    }
    if ($ClientApplyMode -eq "External") {
        if ([string]::IsNullOrWhiteSpace($ExternalVerificationOwner)) {
            throw "External 模式必须提供 ExternalVerificationOwner。"
        }
    }
    else {
        if ([string]::IsNullOrWhiteSpace($ClientVerificationScriptPath)) {
            throw "迁移客户端配置时必须提供 ClientVerificationScriptPath，禁止跳过上报核验。"
        }
        $verificationScript = [System.IO.Path]::GetFullPath(
            $ClientVerificationScriptPath
        )
        if (-not (Test-Path -LiteralPath $verificationScript -PathType Leaf)) {
            throw "客户端上报核验脚本不存在：$verificationScript"
        }
    }
    if ($ClientApplyMode -eq "Restart") {
        if (
            [string]::IsNullOrWhiteSpace($ClientProcessName) -or
            [string]::IsNullOrWhiteSpace($ClientExecutablePath)
        ) {
            throw "Restart 模式必须提供 ClientProcessName 和 ClientExecutablePath。"
        }
        $clientExecutable = [System.IO.Path]::GetFullPath(
            $ClientExecutablePath
        )
        if (-not (Test-Path -LiteralPath $clientExecutable -PathType Leaf)) {
            throw "客户端可执行文件不存在：$clientExecutable"
        }
    }
}

function Invoke-ClientConfigurationApply {
    if ($ClientApplyMode -eq "Restart") {
        Get-Process -Name $ClientProcessName -ErrorAction SilentlyContinue |
            Stop-Process -Force
        Start-Process `
            -FilePath $clientExecutable `
            -WorkingDirectory (Split-Path -Parent $clientExecutable) `
            -ArgumentList $ClientExecutableArgument | Out-Null
    }
    if ($ClientApplyWaitSeconds -gt 0) {
        Start-Sleep -Seconds $ClientApplyWaitSeconds
    }
}

function Invoke-ClientReportingVerification {
    $currentPowerShell = (Get-Process -Id $PID).Path
    & $currentPowerShell `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $verificationScript `
        @ClientVerificationArgument
    if ($LASTEXITCODE -ne 0) {
        throw "客户端上报核验脚本失败（退出码 $LASTEXITCODE）。"
    }
}

$stateRoot = [System.IO.Path]::GetFullPath($StateDirectory)
New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
$runId = [datetime]::UtcNow.ToString("yyyyMMdd-HHmmss") + "-" +
    [guid]::NewGuid().ToString("N").Substring(0, 8)
$runDirectory = Join-Path $stateRoot $runId
New-Item -ItemType Directory -Path $runDirectory | Out-Null
$hostsPath = "$env:SystemRoot\System32\drivers\etc\hosts"
$hostsBackup = Join-Path $runDirectory "hosts.before"
Copy-Item -LiteralPath $hostsPath -Destination $hostsBackup

$certificateStorePath = "Cert:\LocalMachine\Root"
$existingCertificate = Get-ChildItem $certificateStorePath |
    Where-Object { $_.Thumbprint -eq $rootCertificate.Thumbprint } |
    Select-Object -First 1
$certificateWasPresent = $null -ne $existingCertificate
$clientConfigBackup = $null
$clientCaExisted = $false
$clientCaBackup = $null
$installedCertificate = $false
$secureConfigWritten = $false
$manifestPath = Join-Path $runDirectory "migration-manifest.json"

$manifest = [ordered]@{
    schema_version = 1
    run_id = $runId
    started_at_utc = [datetime]::UtcNow.ToString("o")
    status = "started"
    hostname = $Hostname
    server_ip = $normalizedIp
    https_port = $HttpsPort
    health_path = $HealthPath
    hosts_path = $hostsPath
    hosts_backup = $hostsBackup
    root_sha256 = $rootSha256
    expected_server_root_sha256 = $serverRootSha256
    allowed_additional_root_sha256 = $normalizedAdditionalRoots
    root_thumbprint_sha1 = $rootCertificate.Thumbprint
    certificate_was_present = $certificateWasPresent
    certificate_installed_by_run = $false
    client_config_path = $ClientConfigPath
    client_config_backup = $null
    client_ca_bundle_path = $ClientCaBundlePath
    client_ca_bundle_config_value = $ClientCaBundleConfigValue
    client_ca_existed = $false
    client_ca_backup = $null
    client_apply_mode = $ClientApplyMode
    client_process_name = $ClientProcessName
    client_executable_path = $ClientExecutablePath
    client_verification_script = $ClientVerificationScriptPath
    external_verification_owner = $ExternalVerificationOwner
    stage_only = [bool]$StageOnly
}
Write-JsonFileAtomically -Value $manifest -Path $manifestPath

try {
    if (-not $certificateWasPresent) {
        Import-Certificate `
            -FilePath $rootPath `
            -CertStoreLocation $certificateStorePath | Out-Null
        $installedCertificate = $true
        $manifest.certificate_installed_by_run = $true
    }

    $installed = Get-ChildItem $certificateStorePath |
        Where-Object { $_.Thumbprint -eq $rootCertificate.Thumbprint } |
        Select-Object -First 1
    if ($null -eq $installed) {
        throw "根证书导入后无法在 LocalMachine\Root 中找到。"
    }
    $installedSha256 = Get-CertificateSha256 -Certificate $installed
    if ($installedSha256 -cne $rootSha256) {
        throw "已安装根证书指纹核验失败。"
    }

    # DNS is deliberately not required. Re-running this script removes the
    # previous mapping for the hostname and writes the new ServerIp once.
    Set-HostsMapping `
        -HostsPath $hostsPath `
        -Hostname $Hostname `
        -ServerIp $normalizedIp

    if ($StageOnly) {
        $manifest.status = "trust_staged_pending_server"
        $manifest["staged_at_utc"] = [datetime]::UtcNow.ToString("o")
        Write-JsonFileAtomically -Value $manifest -Path $manifestPath
        $latest = [ordered]@{
            schema_version = 1
            manifest_path = $manifestPath
            run_id = $runId
            status = $manifest.status
        }
        Write-JsonFileAtomically `
            -Value $latest `
            -Path (Join-Path $stateRoot "latest.json")
        Write-Host "内部根 CA 与 hosts 已预置；尚未探测 HTTPS，也未修改客户端配置。"
        Write-Host "hosts：$normalizedIp $Hostname"
        Write-Host "根证书 SHA-256：$rootSha256"
        Write-Host "待服务器 HTTPS 上线后重新运行激活阶段。"
        Write-Warning (
            "Chrome/Edge 可能受 Windows 代理或 PAC 影响；请运行 " +
            "Test-InspectionBrowserProxy.ps1，并在灰度浏览器确认 " +
            "$Hostname 走 DIRECT。本脚本不会修改公司代理策略。"
        )
        return
    }

    $probe = Invoke-InspectionTlsProbe `
        -ServerIp $normalizedIp `
        -Hostname $Hostname `
        -Port $HttpsPort `
        -Path $HealthPath `
        -ExpectedRootSha256 $serverRootSha256

    if (-not [string]::IsNullOrWhiteSpace($ClientConfigPath)) {
        $configPath = [System.IO.Path]::GetFullPath($ClientConfigPath)
        if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
            throw "客户端配置文件不存在：$configPath"
        }
        if (
            [string]::IsNullOrWhiteSpace($RootPemPath) -or
            [string]::IsNullOrWhiteSpace($ClientCaBundlePath)
        ) {
            throw "迁移客户端配置时必须同时提供 RootPemPath 和 ClientCaBundlePath。"
        }
        $pemPath = [System.IO.Path]::GetFullPath($RootPemPath)
        if (-not (Test-Path -LiteralPath $pemPath -PathType Leaf)) {
            throw "Requests 根证书 PEM 不存在：$pemPath"
        }
        $pemText = [System.IO.File]::ReadAllText($pemPath)
        if ($pemText -match "-----BEGIN (?:ENCRYPTED )?PRIVATE KEY-----") {
            throw "Requests 根证书 PEM 不得包含私钥。"
        }
        $pemMatches = [regex]::Matches(
            $pemText,
            "-----BEGIN CERTIFICATE-----\s*(?<body>[A-Za-z0-9+/=\r\n]+?)\s*-----END CERTIFICATE-----"
        )
        if ($pemMatches.Count -lt 1) {
            throw "Requests 根证书 PEM 至少必须包含一张根证书。"
        }
        $pemRemainder = [regex]::Replace(
            $pemText,
            "-----BEGIN CERTIFICATE-----\s*[A-Za-z0-9+/=\r\n]+?\s*-----END CERTIFICATE-----",
            ""
        )
        if (-not [string]::IsNullOrWhiteSpace($pemRemainder)) {
            throw "Requests 根证书 PEM 包含证书以外的内容。"
        }
        $pemFingerprints = @(
            foreach ($pemMatch in $pemMatches) {
                try {
                    $pemDer = [Convert]::FromBase64String(
                        ($pemMatch.Groups["body"].Value -replace "\s", "")
                    )
                    $pemCertificate = New-Object `
                        -TypeName System.Security.Cryptography.X509Certificates.X509Certificate2 `
                        -ArgumentList @(,$pemDer)
                }
                catch {
                    throw "Requests 根证书 PEM 无法解析：$_"
                }
                if (
                    $pemCertificate.HasPrivateKey -or
                    $pemCertificate.Subject -cne $pemCertificate.Issuer
                ) {
                    throw "Requests CA bundle 只能包含不带私钥的自签名根证书。"
                }
                $pemConstraintsExtension = $pemCertificate.Extensions |
                    Where-Object { $_.Oid.Value -eq "2.5.29.19" } |
                    Select-Object -First 1
                if ($null -eq $pemConstraintsExtension) {
                    throw "Requests CA bundle 中的证书缺少 Basic Constraints。"
                }
                $pemConstraints = New-Object `
                    -TypeName System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension `
                    -ArgumentList @(
                        $pemConstraintsExtension,
                        $pemConstraintsExtension.Critical
                    )
                if (
                    -not $pemConstraints.CertificateAuthority -or
                    -not $pemConstraints.Critical -or
                    $pemCertificate.PublicKey.Oid.Value -cne
                        "1.2.840.113549.1.1.1" -or
                    $pemCertificate.PublicKey.Key.KeySize -ne 3072 -or
                    [datetime]::UtcNow -lt
                        $pemCertificate.NotBefore.ToUniversalTime() -or
                    [datetime]::UtcNow -ge
                        $pemCertificate.NotAfter.ToUniversalTime()
                ) {
                    throw "Requests CA bundle 包含无效、非 RSA 3072 或过期根证书。"
                }
                Get-CertificateSha256 -Certificate $pemCertificate
            }
        )
        if (($pemFingerprints | Sort-Object -Unique).Count -ne $pemFingerprints.Count) {
            throw "Requests CA bundle 包含重复根证书。"
        }
        $expectedPemFingerprints = @(
            $rootSha256
            $normalizedAdditionalRoots
        ) | Sort-Object -Unique
        $fingerprintDifference = @(
            Compare-Object `
                -ReferenceObject $expectedPemFingerprints `
                -DifferenceObject ($pemFingerprints | Sort-Object)
        )
        if ($fingerprintDifference.Count -ne 0) {
            throw (
                "Requests CA bundle 根指纹集合不符合线下白名单。" +
                "实际：$($pemFingerprints -join ',')"
            )
        }
        $bundlePath = [System.IO.Path]::GetFullPath($ClientCaBundlePath)
        $bundleConfigValue = if ([string]::IsNullOrWhiteSpace(
            $ClientCaBundleConfigValue
        )) {
            $bundlePath
        }
        else {
            if ([System.IO.Path]::IsPathRooted($ClientCaBundleConfigValue)) {
                throw "ClientCaBundleConfigValue 必须是安装目录内的相对路径。"
            }
            $configSegments = @(
                $ClientCaBundleConfigValue.Replace("\", "/").Split("/") |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
            )
            if (
                $configSegments.Count -eq 0 -or
                $configSegments -contains ".." -or
                $configSegments -contains "."
            ) {
                throw "ClientCaBundleConfigValue 不是安全的相对路径。"
            }
            $configSegments -join "/"
        }
        New-Item `
            -ItemType Directory `
            -Path (Split-Path -Parent $bundlePath) `
            -Force | Out-Null
        $clientConfigBackup = Join-Path $runDirectory "client-config.before.json"
        Copy-Item -LiteralPath $configPath -Destination $clientConfigBackup
        $manifest.client_config_backup = $clientConfigBackup
        if (Test-Path -LiteralPath $bundlePath -PathType Leaf) {
            $clientCaExisted = $true
            $clientCaBackup = Join-Path $runDirectory "client-ca.before.pem"
            Copy-Item -LiteralPath $bundlePath -Destination $clientCaBackup
            $manifest.client_ca_existed = $true
            $manifest.client_ca_backup = $clientCaBackup
        }
        if (-not $pemPath.Equals(
            $bundlePath,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            Copy-FileAtomically `
                -SourcePath $pemPath `
                -DestinationPath $bundlePath
        }

        try {
            $configuration = Get-Content `
                -LiteralPath $configPath `
                -Raw | ConvertFrom-Json
        }
        catch {
            throw "客户端配置不是有效 JSON，已保留旧配置：$_"
        }
        $properties = [ordered]@{
            config_schema_version = 2
            server_url = "https://$Hostname"
            transport_security = $TransportSecurity
            tls_ca_bundle = $bundleConfigValue
        }
        foreach ($entry in $properties.GetEnumerator()) {
            if ($configuration.PSObject.Properties.Name -contains $entry.Key) {
                $configuration.($entry.Key) = $entry.Value
            }
            else {
                $configuration | Add-Member `
                    -NotePropertyName $entry.Key `
                    -NotePropertyValue $entry.Value
            }
        }

        Copy-Item -LiteralPath $configPath -Destination "$configPath.bak" -Force
        Write-JsonFileAtomically -Value $configuration -Path $configPath
        $secureConfigWritten = $true
        if ($ClientApplyMode -ne "External") {
            Invoke-ClientConfigurationApply
            Invoke-ClientReportingVerification
        }
    }

    $manifest["server_certificate_sha256"] = $probe.ServerSha256
    $manifest["server_certificate_not_after"] = $probe.Certificate.NotAfter.ToUniversalTime().ToString("o")
    if ($ClientApplyMode -eq "External") {
        $manifest.status = "external_verification_pending"
    }
    else {
        $manifest.status = "completed"
        $manifest["completed_at_utc"] = [datetime]::UtcNow.ToString("o")
    }
    Write-JsonFileAtomically -Value $manifest -Path $manifestPath
    $latest = [ordered]@{
        schema_version = 1
        manifest_path = $manifestPath
        run_id = $runId
        status = $manifest.status
    }
    Write-JsonFileAtomically `
        -Value $latest `
        -Path (Join-Path $stateRoot "latest.json")

    Write-Host "内部 CA 已安装并核验。"
    Write-Host "hosts：$normalizedIp $Hostname"
    Write-Host "根证书 SHA-256：$rootSha256"
    Write-Host "HTTPS 探测：$($probe.StatusLine)"
    Write-Host "回滚清单：$manifestPath"
    Write-Warning (
        "Chrome/Edge 可能受 Windows 代理或 PAC 影响；请运行 " +
        "Test-InspectionBrowserProxy.ps1，并在灰度浏览器确认 " +
        "$Hostname 走 DIRECT。本脚本不会修改公司代理策略。"
    )
    if ($ClientApplyMode -eq "External") {
        Write-Warning (
            "客户端配置已暂存，必须由 $ExternalVerificationOwner 完成重启、" +
            "状态上报核验，并调用 Complete-InspectionTlsExternalVerification；" +
            "当前审计状态仍为 external_verification_pending。"
        )
    }
}
catch {
    $failure = $_
    if ($secureConfigWritten) {
        # HTTPS has already been trusted and the required client config has
        # been written. Keep that secure state for diagnosis; restoring the
        # forensic HTTP backup would be an automatic protocol downgrade.
        $manifest.status = "activation_failed"
        $manifest["activation_failed_at_utc"] = [datetime]::UtcNow.ToString("o")
        $manifest["activation_failure"] = [string]$failure
        Write-JsonFileAtomically -Value $manifest -Path $manifestPath
        $latest = [ordered]@{
            schema_version = 1
            manifest_path = $manifestPath
            run_id = $runId
            status = "activation_failed"
        }
        Write-JsonFileAtomically `
            -Value $latest `
            -Path (Join-Path $stateRoot "latest.json")
        if ($ClientApplyMode -ne "External") {
            try {
                Invoke-ClientConfigurationApply
            }
            catch {
                Write-Warning "HTTPS 配置已保留，但客户端重新加载失败：$_"
            }
        }
        throw $failure
    }

    $manifest.status = "failed"
    $manifest["failed_at_utc"] = [datetime]::UtcNow.ToString("o")
    $manifest["error"] = [string]$failure
    Write-JsonFileAtomically -Value $manifest -Path $manifestPath

    Restore-HostsBackup -BackupPath $hostsBackup -HostsPath $hostsPath
    if ($installedCertificate) {
        Remove-Item `
            -LiteralPath "$certificateStorePath\$($rootCertificate.Thumbprint)" `
            -Force `
            -ErrorAction SilentlyContinue
    }
    if ($null -ne $clientConfigBackup) {
        Copy-FileAtomically `
            -SourcePath $clientConfigBackup `
            -DestinationPath $ClientConfigPath
    }
    if (-not [string]::IsNullOrWhiteSpace($ClientCaBundlePath)) {
        if ($clientCaExisted -and $null -ne $clientCaBackup) {
            Copy-FileAtomically `
                -SourcePath $clientCaBackup `
                -DestinationPath $ClientCaBundlePath
        }
        elseif (-not $clientCaExisted) {
            Remove-Item `
                -LiteralPath $ClientCaBundlePath `
                -Force `
                -ErrorAction SilentlyContinue
        }
    }
    if (
        -not [string]::IsNullOrWhiteSpace($ClientConfigPath) -and
        $ClientApplyMode -ne "External"
    ) {
        try {
            Invoke-ClientConfigurationApply
        }
        catch {
            Write-Warning "旧配置已恢复，但客户端重新加载失败：$_"
        }
    }
    throw $failure
}
