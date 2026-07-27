[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PkiDirectory,

    [string]$DnsName = "textile-monitor.internal",

    [string]$OutputDirectory,

    [int]$ValidDays = 365,

    [string]$OpenSslPath = "openssl",

    [string]$PythonPath = "python"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "InternalPki.Common.ps1")

if ($DnsName -cne "textile-monitor.internal") {
    throw "生产服务器证书只允许 DNS SAN：textile-monitor.internal"
}
if ($ValidDays -ne 365) {
    throw "服务器证书有效期固定为 365 天。"
}

$pkiRoot = Get-CanonicalPath -Path $PkiDirectory
Assert-OutsideGitWorktree -Path $pkiRoot
if (-not (Test-Path -LiteralPath $pkiRoot -PathType Container)) {
    throw "PKI 目录不存在：$pkiRoot"
}

$openssl = Get-OpenSslCommand -OpenSslPath $OpenSslPath
$python = Get-Command $PythonPath -ErrorAction SilentlyContinue
if ($null -eq $python) {
    throw "未找到 Python：$PythonPath"
}

$rootCertificate = Join-Path $pkiRoot "certs/root-ca.cert.pem"
$rootDer = Join-Path $pkiRoot "export/root-ca.cer"
$rootPem = Join-Path $pkiRoot "export/root-ca.pem"
$intermediateCertificate = Join-Path $pkiRoot "certs/intermediate-ca.cert.pem"
$intermediateKey = Join-Path $pkiRoot "private/intermediate-ca.key.pem"
foreach ($requiredPath in @(
    $rootCertificate,
    $rootDer,
    $rootPem,
    $intermediateCertificate,
    $intermediateKey
)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "PKI 缺少必要文件：$requiredPath"
    }
}

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $stamp = [datetime]::UtcNow.ToString("yyyyMMdd-HHmmss")
    $OutputDirectory = Join-Path $pkiRoot "issued/$DnsName-$stamp"
}
$target = Get-CanonicalPath -Path $OutputDirectory
Assert-OutsideGitWorktree -Path $target
if (Test-Path -LiteralPath $target) {
    throw "签发输出目录已存在，请换用新目录：$target"
}

$createdTarget = $false
try {
    New-Item -ItemType Directory -Path $target | Out-Null
    $createdTarget = $true
    $workingDirectory = Join-Path $target ".issuance"
    New-Item -ItemType Directory -Path $workingDirectory | Out-Null

    $serverKey = Join-Path $target "privkey.pem"
    $serverRequest = Join-Path $workingDirectory "server.csr.pem"
    $serverConfig = Join-Path $workingDirectory "server.cnf"
    $serverCertificate = Join-Path $target "server-cert.pem"
    $fullChain = Join-Path $target "fullchain.pem"
    $serialFile = Join-Path $pkiRoot "certs/intermediate-ca.cert.srl"

    @"
[req]
prompt = no
distinguished_name = dn
req_extensions = server_req

[dn]
CN = $DnsName

[server_req]
subjectAltName = @server_names

[server_names]
DNS.1 = $DnsName

[server_cert]
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @server_names
"@ | Set-Content -LiteralPath $serverConfig -Encoding ascii

    Invoke-OpenSsl -OpenSslPath $openssl -Arguments @(
        "genpkey",
        "-algorithm", "RSA",
        "-pkeyopt", "rsa_keygen_bits:3072",
        "-out", $serverKey
    )
    Protect-PkiPrivatePath -Path $serverKey
    Invoke-OpenSsl -OpenSslPath $openssl -Arguments @(
        "req",
        "-new",
        "-sha256",
        "-key", $serverKey,
        "-config", $serverConfig,
        "-out", $serverRequest
    )

    $serialArguments = @()
    if (Test-Path -LiteralPath $serialFile -PathType Leaf) {
        $serialArguments = @("-CAserial", $serialFile)
    }
    else {
        $serialArguments = @("-CAcreateserial")
    }
    $signArguments = @(
        "x509",
        "-req",
        "-sha256",
        "-days", "$ValidDays",
        "-in", $serverRequest,
        "-CA", $intermediateCertificate,
        "-CAkey", $intermediateKey
    ) + $serialArguments + @(
        "-extfile", $serverConfig,
        "-extensions", "server_cert",
        "-out", $serverCertificate
    )
    Write-Host "正在使用加密中间 CA 签发证书。OpenSSL 将要求输入中间 CA 口令。"
    Invoke-OpenSsl -OpenSslPath $openssl -Arguments $signArguments

    $leafText = [System.IO.File]::ReadAllText($serverCertificate).Trim()
    $intermediateText = [System.IO.File]::ReadAllText(
        $intermediateCertificate
    ).Trim()
    [System.IO.File]::WriteAllText(
        $fullChain,
        $leafText + [Environment]::NewLine +
            $intermediateText + [Environment]::NewLine,
        (New-Object System.Text.ASCIIEncoding)
    )
    Copy-Item -LiteralPath $rootPem -Destination (Join-Path $target "root-ca.pem")
    Copy-Item -LiteralPath $rootDer -Destination (Join-Path $target "root-ca.cer")

    Remove-Item -LiteralPath $workingDirectory -Recurse -Force
    & $python.Source `
        (Join-Path $PSScriptRoot "validate_tls_bundle.py") `
        --tls-dir $target `
        --hostname $DnsName `
        --min-valid-days 300 `
        --openssl $openssl
    if ($LASTEXITCODE -ne 0) {
        throw "服务器证书发布包预检失败。"
    }

    $rootFingerprint = Get-CertificateSha256Fingerprint `
        -OpenSslPath $openssl `
        -CertificatePath $rootCertificate
    $leafFingerprint = Get-CertificateSha256Fingerprint `
        -OpenSslPath $openssl `
        -CertificatePath $serverCertificate
    $metadata = [ordered]@{
        schema_version = 1
        generated_at_utc = [datetime]::UtcNow.ToString("o")
        hostname = $DnsName
        valid_days = $ValidDays
        root_sha256 = $rootFingerprint
        server_sha256 = $leafFingerprint
        deploy_files = @(
            "fullchain.pem",
            "privkey.pem",
            "root-ca.pem",
            "root-ca.cer"
        )
    }
    Write-JsonAtomically `
        -Value $metadata `
        -Path (Join-Path $target "server-certificate-manifest.json")

    Write-Host ""
    Write-Host "服务器证书发布包已生成：$target"
    Write-Host "服务器证书 SHA-256：$leafFingerprint"
    Write-Host "根证书 SHA-256：$rootFingerprint"
    Write-Host "该目录不包含 CA 私钥，可以安全复制到受控 Docker 主机。"
}
catch {
    if ($createdTarget -and (Test-Path -LiteralPath $target)) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
    throw
}
