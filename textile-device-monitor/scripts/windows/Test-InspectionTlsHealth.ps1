[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ServerIp,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^(?:[0-9A-Fa-f]{64}|(?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2})$")]
    [string]$ExpectedRootSha256,

    [string]$Hostname = "textile-monitor.internal",

    [int]$HttpsPort = 443,

    [string]$HealthPath = "/health/ready",

    [string]$LogPath = "$env:ProgramData\TextileDeviceMonitor\Logs\tls-health.jsonl"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "InspectionTls.Common.ps1")

function Write-HealthRecord {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Record
    )

    $logFile = [System.IO.Path]::GetFullPath($LogPath)
    New-Item `
        -ItemType Directory `
        -Path (Split-Path -Parent $logFile) `
        -Force | Out-Null
    $line = ($Record | ConvertTo-Json -Compress -Depth 8) +
        [Environment]::NewLine
    $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($line)
    $stream = New-Object `
        -TypeName System.IO.FileStream `
        -ArgumentList @(
            $logFile,
            [System.IO.FileMode]::Append,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::Read
        )
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
    }
    finally {
        $stream.Dispose()
    }
}

$normalizedIp = ConvertTo-NormalizedIpAddress -ServerIp $ServerIp
$normalizedRoot = $ExpectedRootSha256.Replace(":", "").ToUpperInvariant()
$checkedAt = [datetime]::UtcNow

try {
    $resolvedAddresses = @(
        [System.Net.Dns]::GetHostAddresses($Hostname) |
        ForEach-Object { $_.ToString() }
    )
    if ($resolvedAddresses -notcontains $normalizedIp) {
        throw "$Hostname 未解析到预期服务器 IP $normalizedIp；" +
            "请重新运行 Install-InspectionTlsTrust.ps1 更新 hosts。"
    }

    $probe = Invoke-InspectionTlsProbe `
        -ServerIp $normalizedIp `
        -Hostname $Hostname `
        -Port $HttpsPort `
        -Path $HealthPath `
        -ExpectedRootSha256 $normalizedRoot
    $notAfter = $probe.Certificate.NotAfter.ToUniversalTime()
    $remainingDays = ($notAfter - $checkedAt).TotalDays
    if ($remainingDays -le 7) {
        $level = "critical"
        $exitCode = 2
    }
    elseif ($remainingDays -le 14) {
        $level = "high"
        $exitCode = 2
    }
    elseif ($remainingDays -le 30) {
        $level = "warning"
        $exitCode = 1
    }
    elseif ($remainingDays -le 60) {
        $level = "notice"
        $exitCode = 0
    }
    else {
        $level = "ok"
        $exitCode = 0
    }
    $record = [ordered]@{
        checked_at_utc = $checkedAt.ToString("o")
        status = $level
        hostname = $Hostname
        server_ip = $normalizedIp
        https_port = $HttpsPort
        health_path = $HealthPath
        certificate_not_after_utc = $notAfter.ToString("o")
        remaining_days = [math]::Round($remainingDays, 2)
        server_sha256 = $probe.ServerSha256
        root_sha256 = $probe.RootSha256
        http_status = $probe.StatusLine
    }
    Write-HealthRecord -Record $record
    Write-Host (
        "TLS 检查完成：级别={0}，剩余={1:N1} 天，根指纹={2}" -f
        $level,
        $remainingDays,
        $probe.RootSha256
    )
    exit $exitCode
}
catch {
    $record = [ordered]@{
        checked_at_utc = $checkedAt.ToString("o")
        status = "failed"
        hostname = $Hostname
        server_ip = $normalizedIp
        https_port = $HttpsPort
        health_path = $HealthPath
        expected_root_sha256 = $normalizedRoot
        error = [string]$_
    }
    Write-HealthRecord -Record $record
    Write-Error "TLS 每日检查失败：$_"
    exit 2
}
