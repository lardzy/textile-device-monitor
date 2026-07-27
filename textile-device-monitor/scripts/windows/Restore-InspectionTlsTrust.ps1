[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [string]$ManifestPath,

    [string]$StateDirectory = "$env:ProgramData\TextileDeviceMonitor\TlsMigration",

    [string]$ClientExecutablePath,

    [string]$ClientProcessName = "textile-device-client",

    [int]$ClientRestartWaitSeconds = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "InspectionTls.Common.ps1")

Assert-WindowsAdministrator
$stateRoot = [System.IO.Path]::GetFullPath($StateDirectory)
if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    $latestPath = Join-Path $stateRoot "latest.json"
    if (-not (Test-Path -LiteralPath $latestPath -PathType Leaf)) {
        throw "找不到最近一次迁移记录：$latestPath"
    }
    $latest = Get-Content -LiteralPath $latestPath -Raw | ConvertFrom-Json
    $ManifestPath = $latest.manifest_path
}
$manifestFile = [System.IO.Path]::GetFullPath($ManifestPath)
if (-not (Test-Path -LiteralPath $manifestFile -PathType Leaf)) {
    throw "迁移清单不存在：$manifestFile"
}
$manifest = Get-Content -LiteralPath $manifestFile -Raw | ConvertFrom-Json

$activationStatuses = @(
    "external_verification_pending",
    "activation_failed",
    "completed"
)
$hasClientConfig = -not [string]::IsNullOrWhiteSpace(
    [string]$manifest.client_config_path
)
$hasClientConfigBackup = (
    $hasClientConfig -and
    -not [string]::IsNullOrWhiteSpace(
        [string]$manifest.client_config_backup
    )
)
$mustRetainHttps = (
    $hasClientConfig -and
    $activationStatuses -contains [string]$manifest.status
)

if ($mustRetainHttps) {
    $configPath = [string]$manifest.client_config_path
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw "安全 HTTPS 配置不存在，拒绝继续自动恢复：$configPath"
    }
    $configuration = Get-Content `
        -LiteralPath $configPath `
        -Raw | ConvertFrom-Json
    if (
        [string]($configuration.server_url) -cne
            "https://textile-monitor.internal" -or
        [string]($configuration.transport_security) -cne "required"
    ) {
        throw "当前客户端配置不是 HTTPS required，拒绝执行可能的协议降级。"
    }
    if ([string]::IsNullOrWhiteSpace($ClientExecutablePath)) {
        throw "保留安全 HTTPS 配置后必须提供 ClientExecutablePath 重新加载运行态。"
    }
    $restartExecutable = [System.IO.Path]::GetFullPath(
        $ClientExecutablePath
    )
    if (-not (Test-Path -LiteralPath $restartExecutable -PathType Leaf)) {
        throw "客户端可执行文件不存在：$restartExecutable"
    }
    if (-not $PSCmdlet.ShouldProcess(
        "$($manifest.hostname) / $($manifest.server_ip)",
        "保留 HTTPS required 配置和信任，仅重新加载客户端运行态"
    )) {
        return
    }

    Get-Process `
        -Name $ClientProcessName `
        -ErrorAction SilentlyContinue |
        Stop-Process -Force
    Start-Process `
        -FilePath $restartExecutable `
        -WorkingDirectory (Split-Path -Parent $restartExecutable) | Out-Null
    if ($ClientRestartWaitSeconds -gt 0) {
        Start-Sleep -Seconds $ClientRestartWaitSeconds
    }
    $runningClient = Get-Process `
        -Name $ClientProcessName `
        -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $runningClient) {
        throw "客户端未能在保留 HTTPS 配置后重新启动。"
    }

    $retained = [ordered]@{
        schema_version = 1
        recorded_at_utc = [datetime]::UtcNow.ToString("o")
        action = "secure_https_state_retained"
        source_manifest = $manifestFile
        original_status = [string]$manifest.status
        hostname = $manifest.hostname
        server_ip = $manifest.server_ip
    }
    Write-JsonFileAtomically `
        -Value $retained `
        -Path (Join-Path (
            Split-Path -Parent $manifestFile
        ) "secure-state-retained.json")
    Write-Warning (
        "未恢复旧配置：服务器切换 HTTPS 后禁止自动降级 HTTP。" +
        "当前 HTTPS required 配置、CA 和 hosts 均已保留并重新加载。"
    )
    return
}

if (
    $hasClientConfigBackup -and
    [string]::IsNullOrWhiteSpace($ClientExecutablePath)
) {
    throw "恢复客户端配置后必须提供 ClientExecutablePath 重新加载运行态。"
}

if (-not $PSCmdlet.ShouldProcess(
    "$($manifest.hostname) / $($manifest.server_ip)",
    "恢复 hosts、客户端配置，并按清单移除本次新装的根证书"
)) {
    return
}

Restore-HostsBackup `
    -BackupPath $manifest.hosts_backup `
    -HostsPath $manifest.hosts_path

if ($manifest.certificate_installed_by_run) {
    $certificatePath = "Cert:\LocalMachine\Root\$($manifest.root_thumbprint_sha1)"
    Remove-Item -LiteralPath $certificatePath -Force -ErrorAction SilentlyContinue
}

if (
    $hasClientConfigBackup
) {
    Copy-FileAtomically `
        -SourcePath $manifest.client_config_backup `
        -DestinationPath $manifest.client_config_path
}

if (-not [string]::IsNullOrWhiteSpace(
    [string]$manifest.client_ca_bundle_path
)) {
    if (
        $manifest.client_ca_existed -and
        -not [string]::IsNullOrWhiteSpace(
            [string]$manifest.client_ca_backup
        )
    ) {
        Copy-FileAtomically `
            -SourcePath $manifest.client_ca_backup `
            -DestinationPath $manifest.client_ca_bundle_path
    }
    else {
        Remove-Item `
            -LiteralPath $manifest.client_ca_bundle_path `
            -Force `
            -ErrorAction SilentlyContinue
    }
}

if ($hasClientConfigBackup) {
    $restartExecutable = [System.IO.Path]::GetFullPath(
        $ClientExecutablePath
    )
    if (-not (Test-Path -LiteralPath $restartExecutable -PathType Leaf)) {
        throw "客户端可执行文件不存在：$restartExecutable"
    }
    Get-Process `
        -Name $ClientProcessName `
        -ErrorAction SilentlyContinue |
        Stop-Process -Force
    Start-Process `
        -FilePath $restartExecutable `
        -WorkingDirectory (Split-Path -Parent $restartExecutable) | Out-Null
    if ($ClientRestartWaitSeconds -gt 0) {
        Start-Sleep -Seconds $ClientRestartWaitSeconds
    }
    if ($null -eq (
        Get-Process `
            -Name $ClientProcessName `
            -ErrorAction SilentlyContinue |
            Select-Object -First 1
    )) {
        throw "客户端配置恢复后未能重新启动。"
    }
}

$rollback = [ordered]@{
    schema_version = 1
    rolled_back_at_utc = [datetime]::UtcNow.ToString("o")
    source_manifest = $manifestFile
    hostname = $manifest.hostname
    server_ip = $manifest.server_ip
}
Write-JsonFileAtomically `
    -Value $rollback `
    -Path (Join-Path (Split-Path -Parent $manifestFile) "rollback.json")
$manifest.status = "rolled_back"
$manifest | Add-Member `
    -NotePropertyName rolled_back_at_utc `
    -NotePropertyValue $rollback.rolled_back_at_utc
Write-JsonFileAtomically -Value $manifest -Path $manifestFile
$latestPath = Join-Path $stateRoot "latest.json"
if (Test-Path -LiteralPath $latestPath -PathType Leaf) {
    $latest = Get-Content -LiteralPath $latestPath -Raw | ConvertFrom-Json
    if ([string]($latest.manifest_path) -ceq $manifestFile) {
        $latest.status = "rolled_back"
        Write-JsonFileAtomically -Value $latest -Path $latestPath
    }
}
Write-Host "已根据清单完成回滚：$manifestFile"
