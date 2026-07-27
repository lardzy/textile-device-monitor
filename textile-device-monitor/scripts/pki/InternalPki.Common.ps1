Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-CanonicalPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    return [System.IO.Path]::GetFullPath($Path).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
}

function Get-NearestExistingDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $candidate = Get-CanonicalPath -Path $Path
    while (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
        $parent = [System.IO.Directory]::GetParent($candidate)
        if ($null -eq $parent) {
            throw "无法找到路径 $Path 的现有父目录。"
        }
        $candidate = $parent.FullName
    }
    return $candidate
}

function Assert-OutsideGitWorktree {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $target = Get-CanonicalPath -Path $Path
    $existing = Get-NearestExistingDirectory -Path $target
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($null -eq $git) {
        $cursor = [System.IO.DirectoryInfo]$existing
        while ($null -ne $cursor) {
            if (Test-Path -LiteralPath (Join-Path $cursor.FullName ".git")) {
                throw "安全拒绝：PKI 输出目录不能位于 Git 工作区内：$target"
            }
            $cursor = $cursor.Parent
        }
        return
    }

    $gitRoot = (& $git.Source -C $existing rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($gitRoot)) {
        return
    }

    $root = Get-CanonicalPath -Path ([string]$gitRoot)
    $separator = [string][System.IO.Path]::DirectorySeparatorChar
    if (
        $target.Equals($root, [System.StringComparison]::OrdinalIgnoreCase) -or
        $target.StartsWith(
            $root + $separator,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "安全拒绝：PKI 输出目录不能位于 Git 工作区内：$target"
    }
}

function Get-OpenSslCommand {
    param(
        [string]$OpenSslPath = "openssl"
    )

    $command = Get-Command $OpenSslPath -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "未找到 OpenSSL：$OpenSslPath"
    }
    return $command.Source
}

function Invoke-OpenSsl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$OpenSslPath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $OpenSslPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "OpenSSL 执行失败（退出码 $LASTEXITCODE）：$($Arguments -join ' ')"
    }
}

function Protect-PkiPrivatePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if ($env:OS -eq "Windows_NT") {
        $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $currentSid = $currentIdentity.User.Value
        & icacls.exe $Path /inheritance:r | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "无法关闭私钥 ACL 继承：$Path"
        }
        & icacls.exe $Path /grant:r `
            "*${currentSid}:(F)" `
            "*S-1-5-18:(F)" `
            "*S-1-5-32-544:(F)" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "无法设置私钥 ACL：$Path"
        }
        return
    }

    $chmod = Get-Command chmod -ErrorAction SilentlyContinue
    if ($null -eq $chmod) {
        throw "非 Windows 环境缺少 chmod，无法保护私钥：$Path"
    }
    & $chmod.Source 600 $Path
    if ($LASTEXITCODE -ne 0) {
        throw "无法将私钥权限设置为 0600：$Path"
    }
}

function Protect-PkiPrivateDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "私密目录不存在：$Path"
    }
    if ($env:OS -eq "Windows_NT") {
        $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $currentSid = $currentIdentity.User.Value
        & icacls.exe $Path /inheritance:r | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "无法关闭私密目录 ACL 继承：$Path"
        }
        & icacls.exe $Path /grant:r `
            "*${currentSid}:(OI)(CI)(F)" `
            "*S-1-5-18:(OI)(CI)(F)" `
            "*S-1-5-32-544:(OI)(CI)(F)" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "无法设置私密目录 ACL：$Path"
        }
        return
    }

    $chmod = Get-Command chmod -ErrorAction SilentlyContinue
    if ($null -eq $chmod) {
        throw "非 Windows 环境缺少 chmod，无法保护私密目录：$Path"
    }
    & $chmod.Source 700 $Path
    if ($LASTEXITCODE -ne 0) {
        throw "无法将私密目录权限设置为 0700：$Path"
    }
}

function Assert-SafeDistinguishedNameValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -match "[\x00\r\n/=]") {
        throw "$Label 不能为空，且不能包含换行、斜杠或等号。"
    }
}

function Get-CertificateSha256Fingerprint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$OpenSslPath,

        [Parameter(Mandatory = $true)]
        [string]$CertificatePath
    )

    $line = (& $OpenSslPath x509 -in $CertificatePath -noout -fingerprint -sha256)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($line)) {
        throw "无法读取证书 SHA-256 指纹：$CertificatePath"
    }
    $text = [string]$line
    $separatorIndex = $text.IndexOf("=")
    if ($separatorIndex -lt 0) {
        throw "OpenSSL 未返回可识别的证书指纹：$CertificatePath"
    }
    return $text.Substring(
        $separatorIndex + 1
    ).Trim().Replace(":", "").ToUpperInvariant()
}

function Write-JsonAtomically {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Value,

        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $temporaryPath = "$Path.tmp-$([guid]::NewGuid().ToString('N'))"
    $json = $Value | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText(
        $temporaryPath,
        $json + [Environment]::NewLine,
        (New-Object System.Text.UTF8Encoding($false))
    )
    Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
}
