[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$CandidateTlsDirectory,

    [Parameter(Mandatory = $true)]
    [string]$ActiveTlsDirectory,

    [Parameter(Mandatory = $true)]
    [string]$ServerIp,

    [Parameter(Mandatory = $true)]
    [string]$ComposeProjectDirectory,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^(?:[0-9A-Fa-f]{64}|(?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2})$")]
    [string]$ExpectedRootSha256,

    [string[]]$ComposeFile = @("docker-compose.yml"),

    [Parameter(Mandatory = $true)]
    [string]$EnvFile,

    [string]$Hostname = "textile-monitor.internal",

    [int]$HttpsPort = 443,

    [string]$ExternalLivePath = "/health/live",

    [string]$InternalReadinessUrl = "http://127.0.0.1:8080/backend-ready",

    [int]$MinimumValidDays = 30,

    [ValidateSet("Recreate", "Reload")]
    [string]$ActivationMode = "Recreate",

    [string]$FrontendService = "frontend",

    [string[]]$InitialServices = @(
        "postgres",
        "area-infer",
        "backend",
        "execution-worker",
        "frontend"
    ),

    [string]$PythonPath = "python",

    [string]$OpenSslPath = "openssl"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "InternalPki.Common.ps1")

if ($Hostname -cne "textile-monitor.internal") {
    throw "生产主机名必须为 textile-monitor.internal"
}
$parsedIp = $null
if (-not [System.Net.IPAddress]::TryParse($ServerIp, [ref]$parsedIp)) {
    throw "ServerIp 不是有效 IP 地址：$ServerIp"
}
if (
    $parsedIp.AddressFamily -ne
    [System.Net.Sockets.AddressFamily]::InterNetwork
) {
    throw "服务器部署当前只支持局域网 IPv4 地址。"
}
$serverOctets = $parsedIp.GetAddressBytes()
$serverIpIsPrivate = (
    $serverOctets[0] -eq 10 -or
    (
        $serverOctets[0] -eq 172 -and
        $serverOctets[1] -ge 16 -and
        $serverOctets[1] -le 31
    ) -or
    ($serverOctets[0] -eq 192 -and $serverOctets[1] -eq 168)
)
if (-not $serverIpIsPrivate) {
    throw "ServerIp 必须是 RFC1918 局域网 IPv4 地址。"
}
if ($HttpsPort -lt 1 -or $HttpsPort -gt 65535) {
    throw "HttpsPort 必须在 1-65535 之间。"
}
if ($ExternalLivePath -cne "/health/live") {
    throw "外部可信探测必须使用精确的 /health/live 存活端点。"
}
if ($InternalReadinessUrl -cne "http://127.0.0.1:8080/backend-ready") {
    throw "容器内就绪探测必须固定走 frontend loopback 的 /backend-ready。"
}

$candidate = Get-CanonicalPath -Path $CandidateTlsDirectory
$active = Get-CanonicalPath -Path $ActiveTlsDirectory
$projectDirectory = Get-CanonicalPath -Path $ComposeProjectDirectory
Assert-OutsideGitWorktree -Path $candidate
Assert-OutsideGitWorktree -Path $active

$python = Get-Command $PythonPath -ErrorAction SilentlyContinue
if ($null -eq $python) {
    throw "未找到 Python：$PythonPath"
}
$openssl = Get-OpenSslCommand -OpenSslPath $OpenSslPath
$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($null -eq $docker) {
    throw "未找到 Docker CLI。"
}
$curl = Get-Command curl.exe -ErrorAction SilentlyContinue
if ($null -eq $curl) {
    $curl = Get-Command curl -ErrorAction SilentlyContinue
}
if ($null -eq $curl) {
    throw "未找到 curl，无法执行可信 HTTPS 探测。"
}
if (-not (Test-Path -LiteralPath $projectDirectory -PathType Container)) {
    throw "Compose 项目目录不存在：$projectDirectory"
}

$composeBaseArguments = @("compose")
$envPath = if ([System.IO.Path]::IsPathRooted($EnvFile)) {
    Get-CanonicalPath -Path $EnvFile
}
else {
    Get-CanonicalPath -Path (Join-Path $projectDirectory $EnvFile)
}
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    throw "Compose 环境文件不存在：$envPath"
}
$composeBaseArguments += @("--env-file", $envPath)
$resolvedComposeFiles = New-Object System.Collections.Generic.List[string]
foreach ($file in $ComposeFile) {
    $composePath = if ([System.IO.Path]::IsPathRooted($file)) {
        Get-CanonicalPath -Path $file
    }
    else {
        Get-CanonicalPath -Path (Join-Path $projectDirectory $file)
    }
    if (-not (Test-Path -LiteralPath $composePath -PathType Leaf)) {
        throw "Compose 文件不存在：$composePath"
    }
    $resolvedComposeFiles.Add($composePath)
    $composeBaseArguments += @("-f", $composePath)
}

function Invoke-DockerCompose {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [switch]$AllowFailure
    )

    Push-Location $projectDirectory
    try {
        $dockerArguments = $composeBaseArguments + $Arguments
        & $docker.Source @dockerArguments
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if (-not $AllowFailure -and $exitCode -ne 0) {
        throw "docker compose 执行失败（退出码 $exitCode）：$($Arguments -join ' ')"
    }
    return $exitCode
}

function Get-RunningComposeServices {
    Push-Location $projectDirectory
    try {
        $statusArguments = $composeBaseArguments +
            @("ps", "--status", "running", "--services")
        $runningServices = @(& $docker.Source @statusArguments)
        if ($LASTEXITCODE -ne 0) {
            throw "无法读取 Compose 服务状态。"
        }
    }
    finally {
        Pop-Location
    }
    return @($runningServices)
}

function Test-FrontendRunning {
    return (Get-RunningComposeServices) -contains $FrontendService
}

function Invoke-ApplicationDeploymentPreflight {
    param(
        [switch]$Probe
    )

    $backendDirectory = Join-Path $projectDirectory "backend"
    $previousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = @(
        $backendDirectory,
        $previousPythonPath
    ) -join [System.IO.Path]::PathSeparator
    $validationArguments = @(
        "-m",
        "app.deployment_validation",
        "--env-file",
        $envPath
    )
    foreach ($composePath in $resolvedComposeFiles) {
        $validationArguments += @("--compose-file", $composePath)
    }
    if ($Probe) {
        $validationArguments += @(
            "--probe-address", $ServerIp,
            "--probe-port", "$HttpsPort"
        )
    }
    Push-Location $projectDirectory
    try {
        & $python.Source @validationArguments
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
        $env:PYTHONPATH = $previousPythonPath
    }
    if ($exitCode -ne 0) {
        $phase = if ($Probe) { "部署后" } else { "部署前" }
        throw "$phase应用部署完整预检失败。"
    }
}

function Invoke-TrustedHttpsProbe {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RootPem
    )

    $authority = if ($HttpsPort -eq 443) {
        $Hostname
    }
    else {
        "${Hostname}:$HttpsPort"
    }
    $url = "https://$authority$ExternalLivePath"
    $resolve = "${Hostname}:$HttpsPort`:$ServerIp"
    & $curl.Source `
        --silent `
        --show-error `
        --fail `
        --connect-timeout 5 `
        --max-time 15 `
        --noproxy "*" `
        --cacert $RootPem `
        --resolve $resolve `
        $url | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "HTTPS 可信探测失败：$url（直连 $ServerIp，未跳过证书验证）"
    }
}

function Invoke-ContainerReadinessProbe {
    # This path is reachable only through the frontend container's loopback
    # listener. It checks backend /health/ready without weakening the external
    # MANAGEMENT_CIDRS boundary or allow-listing a Docker gateway.
    Invoke-DockerCompose -Arguments @(
        "exec",
        "-T",
        $FrontendService,
        "wget",
        "-qO-",
        $InternalReadinessUrl
    ) | Out-Null
}

function Get-DeployedServerFingerprint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RootPem
    )

    $probeCode = @'
import hashlib
import socket
import ssl
import sys

address, port, hostname, cafile = sys.argv[1:]
context = ssl.create_default_context(cafile=cafile)
with socket.create_connection((address, int(port)), timeout=10) as plain:
    with context.wrap_socket(plain, server_hostname=hostname) as secured:
        certificate = secured.getpeercert(binary_form=True)
print(hashlib.sha256(certificate).hexdigest().upper())
'@
    $fingerprint = & $python.Source `
        -c $probeCode `
        $ServerIp `
        "$HttpsPort" `
        $Hostname `
        $RootPem
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($fingerprint)) {
        throw "无法读取已部署服务器证书指纹。"
    }
    return ([string]$fingerprint).Trim().ToUpperInvariant()
}

function Activate-Frontend {
    if ($ActivationMode -eq "Reload") {
        Invoke-DockerCompose -Arguments @(
            "exec", "-T", $FrontendService, "nginx", "-s", "reload"
        ) | Out-Null
        return
    }
    Invoke-DockerCompose -Arguments @(
        "up", "-d", "--no-deps", "--force-recreate", $FrontendService
    ) | Out-Null
}

function Invoke-TlsBundleValidation {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TlsDirectory,

        [Parameter(Mandatory = $true)]
        [int]$ValidDays,

        [Parameter(Mandatory = $true)]
        [string]$FailureMessage
    )

    & $python.Source `
        (Join-Path $PSScriptRoot "validate_tls_bundle.py") `
        --tls-dir $TlsDirectory `
        --hostname $Hostname `
        --min-valid-days $ValidDays `
        --openssl $openssl
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

Invoke-TlsBundleValidation `
    -TlsDirectory $candidate `
    -ValidDays $MinimumValidDays `
    -FailureMessage "候选 TLS 发布包预检失败，未修改当前证书。"
$candidateRootFingerprint = Get-CertificateSha256Fingerprint `
    -OpenSslPath $openssl `
    -CertificatePath (Join-Path $candidate "root-ca.pem")
$normalizedExpectedRoot = $ExpectedRootSha256.Replace(
    ":",
    ""
).ToUpperInvariant()
if ($candidateRootFingerprint -cne $normalizedExpectedRoot) {
    throw "候选发布包根 CA 指纹与线下记录不一致。实际：$candidateRootFingerprint"
}
$candidateServerFingerprint = Get-CertificateSha256Fingerprint `
    -OpenSslPath $openssl `
    -CertificatePath (Join-Path $candidate "fullchain.pem")
Invoke-DockerCompose -Arguments @("config", "--quiet") | Out-Null
& $python.Source -c "import cryptography"
if ($LASTEXITCODE -ne 0) {
    throw (
        "部署预检缺少 cryptography，请先安装 " +
        "scripts/requirements-deployment.txt 中的依赖。"
    )
}

$requiredFiles = @(
    "fullchain.pem",
    "privkey.pem",
    "root-ca.pem",
    "root-ca.cer"
)
$activeDirectoryExists = Test-Path -LiteralPath $active -PathType Container
$activeBundleFiles = @()
$activeDirectoryEntries = @()
if ($activeDirectoryExists) {
    $activeDirectoryEntries = @(Get-ChildItem -LiteralPath $active -Force)
    $activeBundleFiles = @(
        $requiredFiles |
        Where-Object {
            Test-Path `
                -LiteralPath (Join-Path $active $_) `
                -PathType Leaf
        }
    )
}
if (
    $activeBundleFiles.Count -gt 0 -and
    $activeBundleFiles.Count -lt $requiredFiles.Count
) {
    throw (
        "当前 TLS 目录包含不完整的旧发布包，已拒绝覆盖。" +
        "请先保留现场并人工核对：$active"
    )
}
if (
    $activeBundleFiles.Count -eq 0 -and
    $activeDirectoryEntries.Count -gt 0
) {
    throw (
        "当前 TLS 目录非空但不是可识别的完整发布包，已拒绝覆盖：" +
        "$active"
    )
}
if (
    $activeBundleFiles.Count -eq $requiredFiles.Count -and
    $activeDirectoryEntries.Count -ne $requiredFiles.Count
) {
    throw (
        "当前 TLS 目录除固定四个发布文件外还包含其它内容，已拒绝递归备份或覆盖：" +
        "$active"
    )
}
$oldBundleValid = $activeBundleFiles.Count -eq $requiredFiles.Count
if ($oldBundleValid) {
    Invoke-TlsBundleValidation `
        -TlsDirectory $active `
        -ValidDays 1 `
        -FailureMessage (
            "当前 TLS 发布包不完整、已失效或无法验证，已拒绝自动覆盖。" +
            "请先保留现场并人工核对：$active"
        )
}
$initialDeployment = -not $oldBundleValid
$runningServicesBefore = @(Get-RunningComposeServices)
$frontendWasRunningBefore = $runningServicesBefore -contains $FrontendService
if ($initialDeployment -and $frontendWasRunningBefore) {
    throw (
        "首次 HTTPS 部署没有可回滚的旧 TLS 发布包，但 frontend 正在运行。" +
        "请先进入维护窗口并停止 frontend，再重新执行；脚本拒绝在无法可靠回滚时切换。"
    )
}
if ($oldBundleValid -and $frontendWasRunningBefore) {
    $activeServerFingerprint = Get-CertificateSha256Fingerprint `
        -OpenSslPath $openssl `
        -CertificatePath (Join-Path $active "fullchain.pem")
    $liveServerFingerprintBefore = Get-DeployedServerFingerprint `
        -RootPem (Join-Path $active "root-ca.pem")
    if ($liveServerFingerprintBefore -cne $activeServerFingerprint) {
        throw (
            "当前 Nginx 内存中的服务器证书与活动目录不一致，无法保证回滚。" +
            "请先人工核对并完成一次受控 reload。"
        )
    }
}
$activeParent = Split-Path -Parent $active
$activeLeaf = Split-Path -Leaf $active
$timestamp = [datetime]::UtcNow.ToString("yyyyMMdd-HHmmss")
$backupRoot = Join-Path $activeParent "tls-backups"
$backupDirectory = Join-Path $backupRoot "$activeLeaf-$timestamp"
$stagingDirectory = Join-Path (
    $activeParent
) ".$activeLeaf.incoming-$([guid]::NewGuid().ToString('N'))"

New-Item -ItemType Directory -Path $activeParent -Force | Out-Null
New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $stagingDirectory | Out-Null
Protect-PkiPrivateDirectory -Path $backupDirectory
Protect-PkiPrivateDirectory -Path $stagingDirectory

$deploymentSucceeded = $false
try {
    if ($oldBundleValid) {
        foreach ($item in Get-ChildItem -LiteralPath $active -Force) {
            Copy-Item `
                -LiteralPath $item.FullName `
                -Destination $backupDirectory `
                -Recurse `
                -Force
        }
        Protect-PkiPrivatePath `
            -Path (Join-Path $backupDirectory "privkey.pem")
    }

    foreach ($name in $requiredFiles) {
        Copy-Item `
            -LiteralPath (Join-Path $candidate $name) `
            -Destination (Join-Path $stagingDirectory $name)
    }
    Protect-PkiPrivatePath -Path (Join-Path $stagingDirectory "privkey.pem")
    if (-not (Test-Path -LiteralPath $active -PathType Container)) {
        New-Item -ItemType Directory -Path $active | Out-Null
    }

    # Nginx keeps the old certificate in memory until activation. Each file is
    # replaced by a same-volume rename; an interrupted replacement is restored
    # from the backup before Nginx is reloaded or recreated.
    foreach ($name in $requiredFiles) {
        $destination = Join-Path $active $name
        $incoming = Join-Path $active ".$name.incoming"
        Copy-Item `
            -LiteralPath (Join-Path $stagingDirectory $name) `
            -Destination $incoming `
            -Force
        if ($name -eq "privkey.pem") {
            Protect-PkiPrivatePath -Path $incoming
        }
        Move-Item -LiteralPath $incoming -Destination $destination -Force
    }

    # This is the authoritative application preflight: ports, management CIDRs,
    # fixed proxy trust, bind mounts, TLS chain and private-key ACL are all
    # checked against the now-staged active directory before Nginx activation.
    Invoke-ApplicationDeploymentPreflight

    if ($frontendWasRunningBefore) {
        # Renewal path: validate the new files in the existing container before
        # the certificate is activated.
        Invoke-DockerCompose -Arguments @(
            "exec", "-T", $FrontendService, "nginx", "-t"
        ) | Out-Null
        Activate-Frontend
    }
    else {
        # First deployment (or a deliberately stopped frontend) has no
        # container available for exec. Starting/recreating it is the first
        # Nginx validation gate; immediately repeat nginx -t after startup.
        if ($initialDeployment) {
            Invoke-DockerCompose -Arguments @(
                @("up", "-d") + $InitialServices
            ) | Out-Null
        }
        else {
            Invoke-DockerCompose -Arguments @(
                "up", "-d", "--force-recreate", $FrontendService
            ) | Out-Null
        }
        Invoke-DockerCompose -Arguments @(
            "exec", "-T", $FrontendService, "nginx", "-t"
        ) | Out-Null
    }

    $rootPem = Join-Path $active "root-ca.pem"
    $lastProbeError = $null
    for ($attempt = 1; $attempt -le 10; $attempt++) {
        try {
            Invoke-TrustedHttpsProbe -RootPem $rootPem
            $lastProbeError = $null
            break
        }
        catch {
            $lastProbeError = $_
            if ($attempt -lt 10) {
                Start-Sleep -Seconds 2
            }
        }
    }
    if ($null -ne $lastProbeError) {
        throw $lastProbeError
    }
    $deployedServerFingerprint = Get-DeployedServerFingerprint `
        -RootPem $rootPem
    if ($deployedServerFingerprint -cne $candidateServerFingerprint) {
        throw (
            "线上服务器证书不是本次候选证书。" +
            "候选：$candidateServerFingerprint，实际：$deployedServerFingerprint"
        )
    }
    $lastReadinessError = $null
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        try {
            Invoke-ContainerReadinessProbe
            $lastReadinessError = $null
            break
        }
        catch {
            $lastReadinessError = $_
            if ($attempt -lt 30) {
                Start-Sleep -Seconds 2
            }
        }
    }
    if ($null -ne $lastReadinessError) {
        throw $lastReadinessError
    }
    Invoke-ApplicationDeploymentPreflight -Probe

    $manifest = [ordered]@{
        schema_version = 1
        deployed_at_utc = [datetime]::UtcNow.ToString("o")
        candidate_directory = $candidate
        active_directory = $active
        backup_directory = $backupDirectory
        hostname = $Hostname
        server_ip = $ServerIp
        https_port = $HttpsPort
        external_live_path = $ExternalLivePath
        internal_readiness_url = $InternalReadinessUrl
        external_tls_live_probe = "passed"
        internal_backend_readiness_probe = "passed"
        activation_mode = $ActivationMode
        initial_deployment = $initialDeployment
        root_sha256 = $candidateRootFingerprint
        server_sha256 = $candidateServerFingerprint
    }
    Write-JsonAtomically `
        -Value $manifest `
        -Path (Join-Path $backupDirectory "deployment-manifest.json")
    $deploymentSucceeded = $true
    Write-Host "服务器证书部署完成。备份目录：$backupDirectory"
}
catch {
    $deploymentError = $_
    Write-Warning "部署失败，正在恢复上一版 TLS 文件：$deploymentError"
    $failureManifest = [ordered]@{
        schema_version = 1
        failed_at_utc = [datetime]::UtcNow.ToString("o")
        candidate_directory = $candidate
        active_directory = $active
        backup_directory = $backupDirectory
        hostname = $Hostname
        server_ip = $ServerIp
        root_sha256 = $candidateRootFingerprint
        server_sha256 = $candidateServerFingerprint
        error = [string]$deploymentError
    }
    Write-JsonAtomically `
        -Value $failureManifest `
        -Path (Join-Path $backupDirectory "deployment-failure.json")
    try {
        if ($oldBundleValid) {
            foreach ($name in $requiredFiles) {
                $backupFile = Join-Path $backupDirectory $name
                if (Test-Path -LiteralPath $backupFile -PathType Leaf) {
                    Copy-Item `
                        -LiteralPath $backupFile `
                        -Destination (Join-Path $active ".$name.rollback") `
                        -Force
                    if ($name -eq "privkey.pem") {
                        Protect-PkiPrivatePath `
                            -Path (Join-Path $active ".$name.rollback")
                    }
                    Move-Item `
                        -LiteralPath (Join-Path $active ".$name.rollback") `
                        -Destination (Join-Path $active $name) `
                        -Force
                }
                else {
                    Remove-Item `
                        -LiteralPath (Join-Path $active $name) `
                        -Force `
                        -ErrorAction SilentlyContinue
                }
            }
        }
        else {
            foreach ($name in $requiredFiles) {
                Remove-Item `
                    -LiteralPath (Join-Path $active $name) `
                    -Force `
                    -ErrorAction SilentlyContinue
            }
        }
        if ($oldBundleValid -and $frontendWasRunningBefore) {
            Activate-Frontend
        }
        elseif (-not $frontendWasRunningBefore) {
            $runningAfterFailure = @(Get-RunningComposeServices)
            $servicesStartedByDeployment = @(
                $runningAfterFailure |
                Where-Object { $runningServicesBefore -notcontains $_ }
            )
            if ($servicesStartedByDeployment.Count -gt 0) {
                Invoke-DockerCompose `
                    -Arguments @(
                        @("stop") + $servicesStartedByDeployment
                    ) `
                    -AllowFailure | Out-Null
            }
        }
    }
    catch {
        Write-Error "自动恢复也失败，请保留现场并从 $backupDirectory 手工恢复：$_"
    }
    throw $deploymentError
}
finally {
    Remove-Item `
        -LiteralPath $stagingDirectory `
        -Recurse `
        -Force `
        -ErrorAction SilentlyContinue
    if (-not $deploymentSucceeded) {
        Write-Warning "失败现场备份保留在：$backupDirectory"
    }
}
