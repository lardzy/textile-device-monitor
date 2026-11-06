#requires -Version 5.1
# 配置中心的只读检查。认证状态由运行桥在真实成功响应后记录。
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try {
    . (Join-Path $PSScriptRoot 'BridgeSettings.ps1')
    $settings = Get-BridgeSettings
    $config = $settings.config
    $failed = $false
    $local = & (Join-Path $PSHOME 'powershell.exe') -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'Test-BridgeInstallation.ps1')
    if ($LASTEXITCODE -eq 0) {
        Write-Output '通过　安装物料与本机配置'
    } else {
        $failed = $true
        Write-Output '失败　本机安装自检'
        $local | Where-Object { $_ -match '^FAIL\s' } | ForEach-Object { Write-Output ('　　　'+$_) }
    }
    $base = [Uri]$config.ApiBase
    $uri = New-Object UriBuilder($base)
    $uri.Path = $base.AbsolutePath.TrimEnd('/') -replace '/api/execution/v1$','/health/live'
    $uri.Query = ''
    $uri.Fragment = ''
    $response = $null
    try {
        $request = [Net.HttpWebRequest]::Create($uri.Uri)
        $request.Timeout = 5000
        $request.ReadWriteTimeout = 5000
        $request.AllowAutoRedirect = $false
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -ne 200) { throw "HTTP $([int]$response.StatusCode)" }
        Write-Output '通过　服务端健康接口（HTTP 200）'
    } catch { $failed = $true; Write-Output ('失败　服务端健康接口：'+$_.Exception.GetBaseException().Message) }
    finally { if ($response) { $response.Dispose() } }
    foreach ($path in @($config.ExecutionStagingPath,$config.PaperFiberRecordsPath) + @($config.SmbTargets)) {
        if ([string]::IsNullOrWhiteSpace($path)) { continue }
        if ([IO.Directory]::Exists($path)) { Write-Output ("可见　$path") }
        else { $failed = $true; Write-Output ("未访问到　$path") }
    }
    Write-Output ("目录使用当前 Windows 账号 " + [Security.Principal.WindowsIdentity]::GetCurrent().Name + " 检查。")
    if ('EXECUTION_BRIDGE_TOKEN' -in $settings.credential_keys) {
        Write-Output 'Bridge 令牌已填写；认证结果见运行状态。'
    } else { Write-Output '尚未填写 Bridge 令牌。' }
    if ($failed) { exit 1 }
} catch {
    Write-Output ('检查失败：' + $_.Exception.Message)
    exit 1
}

