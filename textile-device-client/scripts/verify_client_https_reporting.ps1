[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DeviceCode,

    [string]$HostName = "textile-monitor.internal",

    [ValidateRange(30, 600)]
    [int]$TimeoutSeconds = 90,

    [Parameter(Mandatory = $true)]
    [string]$ResultPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Net.Http

if ($HostName -cne "textile-monitor.internal") {
    throw "生产主机名必须为 textile-monitor.internal。"
}
if ([string]::IsNullOrWhiteSpace($DeviceCode)) {
    throw "DeviceCode 不能为空。"
}
$origin = "https://$HostName"
$deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
$lastObservedHeartbeat = $null
$baselineHeartbeat = $null

function Get-DeviceSnapshot {
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.UseProxy = $false
    $handler.AllowAutoRedirect = $false
    $httpClient = [System.Net.Http.HttpClient]::new($handler)
    $httpClient.Timeout = [TimeSpan]::FromSeconds(10)
    $response = $null
    try {
        $response = $httpClient.GetAsync(
            "$origin/api/devices?limit=1000"
        ).GetAwaiter().GetResult()
        if ([int]$response.StatusCode -ne 200) {
            throw "设备快照接口返回 HTTP $([int]$response.StatusCode)。"
        }
        return $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() |
            ConvertFrom-Json
    }
    finally {
        if ($null -ne $response) {
            $response.Dispose()
        }
        $httpClient.Dispose()
        $handler.Dispose()
    }
}

do {
    Start-Sleep -Seconds 3
    $clientProcess = Get-Process `
        -Name "textile-device-client" `
        -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $clientProcess) {
        throw "客户端重启后未保持运行。"
    }

    try {
        $devices = Get-DeviceSnapshot
        $device = $devices |
            Where-Object { $_.device_code -ceq $DeviceCode } |
            Select-Object -First 1
        if ($null -ne $device -and -not [string]::IsNullOrWhiteSpace(
            [string]$device.last_heartbeat
        )) {
            $lastObservedHeartbeat = [DateTimeOffset]::Parse(
                [string]$device.last_heartbeat
            )
            if ($null -eq $baselineHeartbeat) {
                # Use the server snapshot itself as the baseline. Comparing to
                # the terminal clock would create false rollbacks when clocks
                # differ between the device and Docker host.
                $baselineHeartbeat = $lastObservedHeartbeat
            }
            elseif ($lastObservedHeartbeat -gt $baselineHeartbeat) {
                $result = [ordered]@{
                    schema_version = 1
                    device_code = $DeviceCode
                    baseline_heartbeat = $baselineHeartbeat.ToString("o")
                    last_heartbeat = $lastObservedHeartbeat.ToString("o")
                    verified_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
                }
                $resultJson = $result | ConvertTo-Json -Depth 4
                [System.IO.File]::WriteAllText(
                    [System.IO.Path]::GetFullPath($ResultPath),
                    $resultJson + [Environment]::NewLine,
                    (New-Object System.Text.UTF8Encoding($false))
                )
                Write-Host "客户端进程与 HTTPS 状态上报核验成功。"
                Write-Host "设备：$DeviceCode"
                Write-Host "基线心跳：$($baselineHeartbeat.ToString('o'))"
                Write-Host "最后心跳：$($lastObservedHeartbeat.ToString('o'))"
                exit 0
            }
        }
    }
    catch {
        # Startup and first-report races are expected. Keep polling until the
        # deadline while continuously checking that the process is alive.
    }
} while ([DateTimeOffset]::UtcNow -lt $deadline)

throw (
    "客户端重启后未在 $TimeoutSeconds 秒内产生新的 HTTPS 状态上报。" +
    "基线：$baselineHeartbeat；最后观察到：$lastObservedHeartbeat"
)
