[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PkiDirectory,

    [string]$Organization = "Textile Inspection Department",

    [string]$RootCommonName = "Textile Inspection Offline Root CA",

    [string]$IntermediateCommonName = "Textile Inspection Server Issuing CA",

    [string]$OpenSslPath = "openssl"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "InternalPki.Common.ps1")

$rootDays = 3650
$intermediateDays = 1825
$rsaBits = 3072
$target = Get-CanonicalPath -Path $PkiDirectory
$createdTarget = $false

Assert-OutsideGitWorktree -Path $target
Assert-SafeDistinguishedNameValue -Value $Organization -Label "组织名称"
Assert-SafeDistinguishedNameValue -Value $RootCommonName -Label "根 CA 名称"
Assert-SafeDistinguishedNameValue `
    -Value $IntermediateCommonName `
    -Label "中间 CA 名称"
$openssl = Get-OpenSslCommand -OpenSslPath $OpenSslPath

if (Test-Path -LiteralPath $target) {
    throw "目标目录已存在。为防止覆盖 CA，请换用全新的仓库外目录：$target"
}

try {
    New-Item -ItemType Directory -Path $target | Out-Null
    $createdTarget = $true
    $privateDirectory = Join-Path $target "private"
    $certificateDirectory = Join-Path $target "certs"
    $exportDirectory = Join-Path $target "export"
    $configDirectory = Join-Path $target "config"
    New-Item -ItemType Directory -Path $privateDirectory | Out-Null
    New-Item -ItemType Directory -Path $certificateDirectory | Out-Null
    New-Item -ItemType Directory -Path $exportDirectory | Out-Null
    New-Item -ItemType Directory -Path $configDirectory | Out-Null
    Protect-PkiPrivateDirectory -Path $privateDirectory

    $rootKey = Join-Path $privateDirectory "root-ca.key.pem"
    $rootCertificate = Join-Path $certificateDirectory "root-ca.cert.pem"
    $intermediateKey = Join-Path $privateDirectory "intermediate-ca.key.pem"
    $intermediateRequest = Join-Path $certificateDirectory "intermediate-ca.csr.pem"
    $intermediateCertificate = Join-Path $certificateDirectory "intermediate-ca.cert.pem"
    $rootConfig = Join-Path $configDirectory "root-ca.cnf"
    $intermediateExtensions = Join-Path $configDirectory "intermediate-ca.ext"

    @"
[req]
prompt = no
distinguished_name = dn
x509_extensions = v3_root_ca

[dn]
O = $Organization
CN = $RootCommonName

[v3_root_ca]
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
basicConstraints = critical, CA:true, pathlen:1
keyUsage = critical, keyCertSign, cRLSign
"@ | Set-Content -LiteralPath $rootConfig -Encoding ascii

    @"
[v3_intermediate_ca]
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
basicConstraints = critical, CA:true, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
"@ | Set-Content -LiteralPath $intermediateExtensions -Encoding ascii

    Write-Host "正在生成加密根 CA 私钥。OpenSSL 将要求输入并确认根 CA 口令。"
    Invoke-OpenSsl -OpenSslPath $openssl -Arguments @(
        "genpkey",
        "-algorithm", "RSA",
        "-aes-256-cbc",
        "-pkeyopt", "rsa_keygen_bits:$rsaBits",
        "-out", $rootKey
    )
    Protect-PkiPrivatePath -Path $rootKey

    Invoke-OpenSsl -OpenSslPath $openssl -Arguments @(
        "req",
        "-new",
        "-x509",
        "-sha256",
        "-days", "$rootDays",
        "-key", $rootKey,
        "-config", $rootConfig,
        "-extensions", "v3_root_ca",
        "-out", $rootCertificate
    )

    Write-Host "正在生成加密中间 CA 私钥。OpenSSL 将要求输入并确认中间 CA 口令。"
    Invoke-OpenSsl -OpenSslPath $openssl -Arguments @(
        "genpkey",
        "-algorithm", "RSA",
        "-aes-256-cbc",
        "-pkeyopt", "rsa_keygen_bits:$rsaBits",
        "-out", $intermediateKey
    )
    Protect-PkiPrivatePath -Path $intermediateKey

    Invoke-OpenSsl -OpenSslPath $openssl -Arguments @(
        "req",
        "-new",
        "-sha256",
        "-key", $intermediateKey,
        "-subj", "/O=$Organization/CN=$IntermediateCommonName",
        "-out", $intermediateRequest
    )
    Invoke-OpenSsl -OpenSslPath $openssl -Arguments @(
        "x509",
        "-req",
        "-sha256",
        "-days", "$intermediateDays",
        "-in", $intermediateRequest,
        "-CA", $rootCertificate,
        "-CAkey", $rootKey,
        "-CAcreateserial",
        "-extfile", $intermediateExtensions,
        "-extensions", "v3_intermediate_ca",
        "-out", $intermediateCertificate
    )
    Invoke-OpenSsl -OpenSslPath $openssl -Arguments @(
        "verify",
        "-CAfile", $rootCertificate,
        $intermediateCertificate
    )

    $rootPemExport = Join-Path $exportDirectory "root-ca.pem"
    $rootDerExport = Join-Path $exportDirectory "root-ca.cer"
    Copy-Item -LiteralPath $rootCertificate -Destination $rootPemExport
    Invoke-OpenSsl -OpenSslPath $openssl -Arguments @(
        "x509",
        "-in", $rootCertificate,
        "-outform", "DER",
        "-out", $rootDerExport
    )

    $rootFingerprint = Get-CertificateSha256Fingerprint `
        -OpenSslPath $openssl `
        -CertificatePath $rootCertificate
    $intermediateFingerprint = Get-CertificateSha256Fingerprint `
        -OpenSslPath $openssl `
        -CertificatePath $intermediateCertificate

    $metadata = [ordered]@{
        schema_version = 1
        generated_at_utc = [datetime]::UtcNow.ToString("o")
        rsa_bits = $rsaBits
        root_valid_days = $rootDays
        intermediate_valid_days = $intermediateDays
        root_sha256 = $rootFingerprint
        intermediate_sha256 = $intermediateFingerprint
        root_private_key = "private/root-ca.key.pem"
        intermediate_private_key = "private/intermediate-ca.key.pem"
        windows_root_certificate = "export/root-ca.cer"
        requests_root_bundle = "export/root-ca.pem"
    }
    Write-JsonAtomically `
        -Value $metadata `
        -Path (Join-Path $target "pki-manifest.json")

    Write-Host ""
    Write-Host "离线 PKI 初始化完成：$target"
    Write-Host "根证书 SHA-256：$rootFingerprint"
    Write-Host "请将根 CA 私钥移至加密离线介质；Docker 主机不得保存任何 CA 私钥。"
    Write-Host "终端安装文件：$rootDerExport"
    Write-Host "Requests CA 文件：$rootPemExport"
}
catch {
    if ($createdTarget -and (Test-Path -LiteralPath $target)) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
    throw
}
