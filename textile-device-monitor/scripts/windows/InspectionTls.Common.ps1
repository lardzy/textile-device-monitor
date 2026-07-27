Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-WindowsAdministrator {
    if ($env:OS -ne "Windows_NT") {
        throw "此脚本只能在 Windows 上运行。"
    }
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
    $administrator = [System.Security.Principal.WindowsBuiltInRole]::Administrator
    if (-not $principal.IsInRole($administrator)) {
        throw "请使用“以管理员身份运行”的 PowerShell 执行此脚本。"
    }
}

function Assert-InspectionHostname {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Hostname
    )

    if ($Hostname -cne "textile-monitor.internal") {
        throw "生产主机名必须为 textile-monitor.internal"
    }
}

function ConvertTo-NormalizedIpAddress {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ServerIp
    )

    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($ServerIp, [ref]$parsed)) {
        throw "ServerIp 不是有效 IP 地址：$ServerIp"
    }
    if (
        [System.Net.IPAddress]::IsLoopback($parsed) -or
        $parsed.Equals([System.Net.IPAddress]::Any) -or
        $parsed.Equals([System.Net.IPAddress]::IPv6Any)
    ) {
        throw "ServerIp 必须是生产服务器的局域网地址，不能是回环或任意地址。"
    }
    if (
        $parsed.AddressFamily -ne
        [System.Net.Sockets.AddressFamily]::InterNetwork
    ) {
        throw "当前终端 hosts 部署只支持局域网 IPv4 地址。"
    }
    $octets = $parsed.GetAddressBytes()
    $isPrivate = (
        $octets[0] -eq 10 -or
        (
            $octets[0] -eq 172 -and
            $octets[1] -ge 16 -and
            $octets[1] -le 31
        ) -or
        ($octets[0] -eq 192 -and $octets[1] -eq 168)
    )
    if (-not $isPrivate) {
        throw "ServerIp 必须是 RFC1918 局域网 IPv4 地址。"
    }
    return $parsed.ToString()
}

function Get-CertificateSha256 {
    param(
        [Parameter(Mandatory = $true)]
        [System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate
    )

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha256.ComputeHash($Certificate.RawData)
    }
    finally {
        $sha256.Dispose()
    }
    return ([System.BitConverter]::ToString($hash)).Replace("-", "")
}

function Write-JsonFileAtomically {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Value,

        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $json = $Value | ConvertTo-Json -Depth 12
    $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes(
        $json + [Environment]::NewLine
    )
    $directory = Split-Path -Parent ([System.IO.Path]::GetFullPath($Path))
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        throw "JSON 目标目录不存在：$directory"
    }
    $temporaryPath = Join-Path `
        $directory `
        (".textile-json-" + [guid]::NewGuid().ToString("N") + ".tmp")
    try {
        $stream = New-Object `
            -TypeName System.IO.FileStream `
            -ArgumentList @(
                $temporaryPath,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::None
            )
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        }
        finally {
            $stream.Dispose()
        }
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            if ($env:OS -eq "Windows_NT") {
                $destinationAcl = Get-Acl -LiteralPath $Path
                Set-Acl -LiteralPath $temporaryPath -AclObject $destinationAcl
            }
            [System.IO.File]::Replace(
                $temporaryPath,
                [System.IO.Path]::GetFullPath($Path),
                $null,
                $true
            )
        }
        else {
            Move-Item -LiteralPath $temporaryPath -Destination $Path
        }
    }
    finally {
        Remove-Item `
            -LiteralPath $temporaryPath `
            -Force `
            -ErrorAction SilentlyContinue
    }
}

function Copy-FileAtomically {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourcePath,

        [Parameter(Mandatory = $true)]
        [string]$DestinationPath
    )

    $source = [System.IO.Path]::GetFullPath($SourcePath)
    $destination = [System.IO.Path]::GetFullPath($DestinationPath)
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "原子复制源文件不存在：$source"
    }
    if ($source.Equals(
        $destination,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        return
    }
    $directory = Split-Path -Parent $destination
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        throw "原子复制目标目录不存在：$directory"
    }
    $temporaryPath = Join-Path `
        $directory `
        (".textile-copy-" + [guid]::NewGuid().ToString("N") + ".tmp")
    $input = $null
    $output = $null
    try {
        $input = New-Object `
            -TypeName System.IO.FileStream `
            -ArgumentList @(
                $source,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read,
                [System.IO.FileShare]::Read
            )
        $output = New-Object `
            -TypeName System.IO.FileStream `
            -ArgumentList @(
                $temporaryPath,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::None
            )
        $input.CopyTo($output)
        $output.Flush($true)
        $output.Dispose()
        $output = $null
        $input.Dispose()
        $input = $null

        if (Test-Path -LiteralPath $destination -PathType Leaf) {
            if ($env:OS -eq "Windows_NT") {
                $destinationAcl = Get-Acl -LiteralPath $destination
                Set-Acl -LiteralPath $temporaryPath -AclObject $destinationAcl
            }
            [System.IO.File]::Replace(
                $temporaryPath,
                $destination,
                $null,
                $true
            )
        }
        else {
            Move-Item -LiteralPath $temporaryPath -Destination $destination
        }
    }
    finally {
        if ($null -ne $output) {
            $output.Dispose()
        }
        if ($null -ne $input) {
            $input.Dispose()
        }
        Remove-Item `
            -LiteralPath $temporaryPath `
            -Force `
            -ErrorAction SilentlyContinue
    }
}

function Clear-InspectionDnsCache {
    $clearCommand = Get-Command Clear-DnsClientCache -ErrorAction SilentlyContinue
    if ($null -ne $clearCommand) {
        $clearCommandName = $clearCommand.Name
        & $clearCommandName
        if (-not $?) {
            throw "Clear-DnsClientCache 执行失败。"
        }
        return
    }

    & ipconfig.exe /flushdns | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "ipconfig /flushdns 执行失败。"
    }
}

function Set-FileBytesAtomically {
    param(
        [Parameter(Mandatory = $true)]
        [string]$DestinationPath,

        [Parameter(Mandatory = $true)]
        [byte[]]$Bytes
    )

    $directory = Split-Path -Parent $DestinationPath
    $temporaryPath = Join-Path `
        $directory `
        (".textile-hosts-" + [guid]::NewGuid().ToString("N") + ".tmp")
    try {
        $stream = New-Object `
            -TypeName System.IO.FileStream `
            -ArgumentList @(
                $temporaryPath,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::None
            )
        try {
            $stream.Write($Bytes, 0, $Bytes.Length)
            $stream.Flush($true)
        }
        finally {
            $stream.Dispose()
        }

        # Keep the restrictive ACL of the Windows hosts file when replacing it.
        $destinationAcl = Get-Acl -LiteralPath $DestinationPath
        Set-Acl -LiteralPath $temporaryPath -AclObject $destinationAcl
        [System.IO.File]::Replace(
            $temporaryPath,
            $DestinationPath,
            $null,
            $true
        )
    }
    finally {
        Remove-Item `
            -LiteralPath $temporaryPath `
            -Force `
            -ErrorAction SilentlyContinue
    }
}

function Set-HostsMapping {
    param(
        [Parameter(Mandatory = $true)]
        [string]$HostsPath,

        [Parameter(Mandatory = $true)]
        [string]$Hostname,

        [Parameter(Mandatory = $true)]
        [string]$ServerIp
    )

    $rawBytes = [System.IO.File]::ReadAllBytes($HostsPath)
    $preambleLength = 0
    if (
        $rawBytes.Length -ge 3 -and
        $rawBytes[0] -eq 0xEF -and
        $rawBytes[1] -eq 0xBB -and
        $rawBytes[2] -eq 0xBF
    ) {
        $encoding = New-Object System.Text.UTF8Encoding($true, $true)
        $preambleLength = 3
    }
    elseif (
        $rawBytes.Length -ge 2 -and
        $rawBytes[0] -eq 0xFF -and
        $rawBytes[1] -eq 0xFE
    ) {
        $encoding = New-Object System.Text.UnicodeEncoding($false, $true, $true)
        $preambleLength = 2
    }
    elseif (
        $rawBytes.Length -ge 2 -and
        $rawBytes[0] -eq 0xFE -and
        $rawBytes[1] -eq 0xFF
    ) {
        $encoding = New-Object System.Text.UnicodeEncoding($true, $true, $true)
        $preambleLength = 2
    }
    else {
        $encoding = New-Object System.Text.UTF8Encoding($false, $true)
        try {
            $null = $encoding.GetString($rawBytes)
        }
        catch [System.Text.DecoderFallbackException] {
            # Legacy Windows hosts files can use the active ANSI code page.
            $ansiCodePage = [System.Globalization.CultureInfo]::CurrentCulture.TextInfo.ANSICodePage
            try {
                $encoding = [System.Text.Encoding]::GetEncoding(
                    $ansiCodePage,
                    [System.Text.EncoderExceptionFallback]::new(),
                    [System.Text.DecoderExceptionFallback]::new()
                )
                $null = $encoding.GetString($rawBytes)
            }
            catch {
                throw "hosts 文件既不是有效 UTF 编码，也无法按当前 ANSI 代码页无损读取。"
            }
        }
    }
    $originalText = $encoding.GetString(
        $rawBytes,
        $preambleLength,
        $rawBytes.Length - $preambleLength
    )
    $newlineMatch = [regex]::Match($originalText, "\r\n|\n|\r")
    $newline = if ($newlineMatch.Success) {
        $newlineMatch.Value
    }
    else {
        [Environment]::NewLine
    }
    $originalLines = @($originalText -split "\r\n|\n|\r")
    $keptLines = New-Object System.Collections.Generic.List[string]
    foreach ($line in $originalLines) {
        $commentIndex = $line.IndexOf("#")
        $content = $line
        $comment = ""
        if ($commentIndex -ge 0) {
            $content = $line.Substring(0, $commentIndex)
            $comment = $line.Substring($commentIndex)
        }
        $tokens = @($content.Trim() -split "\s+" | Where-Object { $_ })
        if ($tokens.Count -le 1) {
            $keptLines.Add($line)
            continue
        }

        $remainingAliases = New-Object System.Collections.Generic.List[string]
        $removedHostname = $false
        foreach ($alias in $tokens[1..($tokens.Count - 1)]) {
            if ($alias.Equals(
                    $Hostname,
                    [System.StringComparison]::OrdinalIgnoreCase
            )) {
                $removedHostname = $true
            }
            else {
                $remainingAliases.Add($alias)
            }
        }
        if (-not $removedHostname) {
            $keptLines.Add($line)
            continue
        }
        if ($remainingAliases.Count -gt 0) {
            $rebuilt = $tokens[0] + "`t" + ($remainingAliases -join "`t")
            if (-not [string]::IsNullOrEmpty($comment)) {
                $rebuilt += "`t" + $comment
            }
            $keptLines.Add($rebuilt)
        }
        elseif (-not [string]::IsNullOrEmpty($comment)) {
            # Preserve a meaningful comment even when the managed hostname was
            # the only alias on the line.
            $keptLines.Add($comment)
        }
    }

    while (
        $keptLines.Count -gt 0 -and
        [string]::IsNullOrWhiteSpace($keptLines[$keptLines.Count - 1])
    ) {
        $keptLines.RemoveAt($keptLines.Count - 1)
    }
    $keptLines.Add("")
    $keptLines.Add(
        "$ServerIp`t$Hostname`t# textile-device-monitor managed"
    )
    $text = ($keptLines -join $newline) + $newline
    $bodyBytes = $encoding.GetBytes($text)
    $preamble = if ($preambleLength -gt 0) {
        $encoding.GetPreamble()
    }
    else {
        [byte[]]@()
    }
    $bytes = [byte[]]::new($preamble.Length + $bodyBytes.Length)
    [System.Array]::Copy($preamble, 0, $bytes, 0, $preamble.Length)
    [System.Array]::Copy(
        $bodyBytes,
        0,
        $bytes,
        $preamble.Length,
        $bodyBytes.Length
    )

    Set-FileBytesAtomically -DestinationPath $HostsPath -Bytes $bytes
    Clear-InspectionDnsCache
}

function Invoke-InspectionTlsProbe {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ServerIp,

        [Parameter(Mandatory = $true)]
        [string]$Hostname,

        [int]$Port = 443,

        [string]$Path = "/health/ready",

        [int]$TimeoutMilliseconds = 10000,

        [Parameter(Mandatory = $true)]
        [ValidatePattern("^(?:[0-9A-Fa-f]{64}|(?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2})$")]
        [string]$ExpectedRootSha256
    )

    Assert-InspectionHostname -Hostname $Hostname
    $normalizedIp = ConvertTo-NormalizedIpAddress -ServerIp $ServerIp
    if ($Port -lt 1 -or $Port -gt 65535) {
        throw "端口必须在 1-65535 之间。"
    }
    if (-not $Path.StartsWith("/")) {
        throw "探测路径必须以 / 开头。"
    }

    $tcpClient = New-Object System.Net.Sockets.TcpClient
    $sslStream = $null
    try {
        $connect = $tcpClient.BeginConnect($normalizedIp, $Port, $null, $null)
        if (-not $connect.AsyncWaitHandle.WaitOne($TimeoutMilliseconds)) {
            throw "连接 $normalizedIp`:$Port 超时。"
        }
        $tcpClient.EndConnect($connect)
        $tcpClient.ReceiveTimeout = $TimeoutMilliseconds
        $tcpClient.SendTimeout = $TimeoutMilliseconds

        # AuthenticateAsClient validates trust, expiry and the DNS name while
        # the TCP connection itself goes directly to ServerIp. No DNS is used.
        $sslStream = New-Object `
            -TypeName System.Net.Security.SslStream `
            -ArgumentList @($tcpClient.GetStream(), $false)
        $sslStream.ReadTimeout = $TimeoutMilliseconds
        $sslStream.WriteTimeout = $TimeoutMilliseconds
        $sslStream.AuthenticateAsClient($Hostname)
        $serverCertificate = New-Object `
            -TypeName System.Security.Cryptography.X509Certificates.X509Certificate2 `
            -ArgumentList @($sslStream.RemoteCertificate)

        $chain = New-Object `
            -TypeName System.Security.Cryptography.X509Certificates.X509Chain
        $chain.ChainPolicy.RevocationMode = [System.Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
        if (-not $chain.Build($serverCertificate)) {
            $details = ($chain.ChainStatus | ForEach-Object {
                "$($_.Status): $($_.StatusInformation.Trim())"
            }) -join "; "
            throw "服务器证书链验证失败：$details"
        }
        $rootCertificate = $chain.ChainElements[
            $chain.ChainElements.Count - 1
        ].Certificate
        $rootSha256 = Get-CertificateSha256 -Certificate $rootCertificate
        if ($rootSha256 -cne $ExpectedRootSha256.Replace(
            ":",
            ""
        ).ToUpperInvariant()) {
            throw "服务器证书链根指纹不匹配。实际：$rootSha256"
        }

        $authority = if ($Port -eq 443) {
            $Hostname
        }
        else {
            "${Hostname}:$Port"
        }
        $request = "GET $Path HTTP/1.1`r`n" +
            "Host: $authority`r`n" +
            "Connection: close`r`n" +
            "User-Agent: TextileTlsHealth/1.0`r`n`r`n"
        $requestBytes = [System.Text.Encoding]::ASCII.GetBytes($request)
        $sslStream.Write($requestBytes, 0, $requestBytes.Length)
        $sslStream.Flush()

        $reader = New-Object `
            -TypeName System.IO.StreamReader `
            -ArgumentList @(
                $sslStream,
                (New-Object System.Text.UTF8Encoding($false)),
                $false,
                1024,
                $true
            )
        $statusLine = $reader.ReadLine()
        if ($statusLine -notmatch "^HTTP/\d(?:\.\d)?\s+200(?:\s|$)") {
            throw "HTTPS 健康检查未返回 200：$statusLine"
        }

        return [pscustomobject]@{
            ServerIp = $normalizedIp
            Hostname = $Hostname
            Port = $Port
            StatusLine = $statusLine
            Certificate = $serverCertificate
            ServerSha256 = Get-CertificateSha256 -Certificate $serverCertificate
            RootSha256 = $rootSha256
        }
    }
    finally {
        if ($null -ne $sslStream) {
            $sslStream.Dispose()
        }
        $tcpClient.Dispose()
    }
}

function Restore-HostsBackup {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BackupPath,

        [Parameter(Mandatory = $true)]
        [string]$HostsPath
    )

    if (-not (Test-Path -LiteralPath $BackupPath -PathType Leaf)) {
        throw "hosts 备份不存在：$BackupPath"
    }
    $backupBytes = [System.IO.File]::ReadAllBytes($BackupPath)
    Set-FileBytesAtomically `
        -DestinationPath $HostsPath `
        -Bytes $backupBytes
    Clear-InspectionDnsCache
}

function Complete-InspectionTlsExternalVerification {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StateDirectory,

        [Parameter(Mandatory = $true)]
        [string]$VerificationOwner,

        [hashtable]$VerificationDetails = @{}
    )

    $latestPath = Join-Path (
        [System.IO.Path]::GetFullPath($StateDirectory)
    ) "latest.json"
    if (-not (Test-Path -LiteralPath $latestPath -PathType Leaf)) {
        throw "找不到待完成的 TLS 迁移记录：$latestPath"
    }
    $latest = Get-Content -LiteralPath $latestPath -Raw | ConvertFrom-Json
    $manifestPath = [string]$latest.manifest_path
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "TLS 迁移清单不存在：$manifestPath"
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.status -cne "external_verification_pending") {
        throw "TLS 迁移不处于外部核验阶段：$($manifest.status)"
    }
    if ($manifest.external_verification_owner -cne $VerificationOwner) {
        throw "外部核验执行方与迁移清单不一致。"
    }
    $manifest.status = "completed"
    $manifest | Add-Member `
        -NotePropertyName completed_at_utc `
        -NotePropertyValue ([datetime]::UtcNow.ToString("o"))
    $manifest | Add-Member `
        -NotePropertyName external_verification_details `
        -NotePropertyValue $VerificationDetails
    Write-JsonFileAtomically -Value $manifest -Path $manifestPath

    $latest.status = "completed"
    Write-JsonFileAtomically -Value $latest -Path $latestPath
}

function Fail-InspectionTlsExternalVerification {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StateDirectory,

        [Parameter(Mandatory = $true)]
        [string]$VerificationOwner,

        [Parameter(Mandatory = $true)]
        [string]$Failure,

        [hashtable]$VerificationDetails = @{}
    )

    $latestPath = Join-Path (
        [System.IO.Path]::GetFullPath($StateDirectory)
    ) "latest.json"
    if (-not (Test-Path -LiteralPath $latestPath -PathType Leaf)) {
        throw "找不到待失败封存的 TLS 迁移记录：$latestPath"
    }
    $latest = Get-Content -LiteralPath $latestPath -Raw | ConvertFrom-Json
    $manifestPath = [string]$latest.manifest_path
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "TLS 迁移清单不存在：$manifestPath"
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.status -cne "external_verification_pending") {
        throw "TLS 迁移不处于外部核验阶段：$($manifest.status)"
    }
    if ($manifest.external_verification_owner -cne $VerificationOwner) {
        throw "外部核验执行方与迁移清单不一致。"
    }

    # Deliberately retain the HTTPS required config, CA bundle, root trust and
    # hosts mapping. Reverting to a backed-up HTTP URL after the server has
    # switched to HTTPS would be an insecure automatic downgrade.
    $manifest.status = "activation_failed"
    $manifest | Add-Member `
        -NotePropertyName activation_failed_at_utc `
        -NotePropertyValue ([datetime]::UtcNow.ToString("o"))
    $manifest | Add-Member `
        -NotePropertyName activation_failure `
        -NotePropertyValue $Failure
    $manifest | Add-Member `
        -NotePropertyName external_verification_details `
        -NotePropertyValue $VerificationDetails
    Write-JsonFileAtomically -Value $manifest -Path $manifestPath

    $latest.status = "activation_failed"
    Write-JsonFileAtomically -Value $latest -Path $latestPath
}
