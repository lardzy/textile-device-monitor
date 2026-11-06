#requires -Version 5.1
# 只在本机回环和唯一临时目录验收检查按钮所调用的脚本。
$ErrorActionPreference='Stop'
$source = Join-Path (Split-Path $PSScriptRoot -Parent) 'runtime\ops'
$root = Join-Path $env:TEMP ('bridge-readonly-check-'+[Guid]::NewGuid().ToString('N'))
$ops = Join-Path $root 'ops'
[void](New-Item -ItemType Directory -Path $ops)
foreach ($file in 'BridgeSettings.ps1','Test-BridgeConnections.ps1') {
    Copy-Item -LiteralPath (Join-Path $source $file) -Destination $ops
}
Set-Content -LiteralPath (Join-Path $ops 'Test-BridgeInstallation.ps1') -Encoding UTF8 -Value "'RESULT: PASS'; exit 0"
$before=$env:TEXTILE_BRIDGE_DATA_ROOT
$env:TEXTILE_BRIDGE_DATA_ROOT = Join-Path $root 'data'
$listener=New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,0)
$child=$null
try {
    $listener.Start()
    $port=$listener.LocalEndpoint.Port
    . (Join-Path $ops 'BridgeSettings.ps1')
    $settings=Initialize-BridgeSettings -InstallRoot $root
    $patch=@{ ApiBase="http://127.0.0.1:$port/api/execution/v1"; ExecutionStagingPath=$root; PaperFiberRecordsPath=$root; SmbTargets=@() }
    $settings=Save-BridgeSettings -ExpectedRevision $settings.revision -Config $patch -Secrets @{EXECUTION_BRIDGE_TOKEN='local-demo-token'}
    $accept=$listener.AcceptTcpClientAsync()
    $start=New-Object Diagnostics.ProcessStartInfo
    $start.FileName=Join-Path $PSHOME 'powershell.exe'
    $start.Arguments='-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+(Join-Path $ops 'Test-BridgeConnections.ps1')+'"'
    $start.UseShellExecute=$false; $start.CreateNoWindow=$true; $start.RedirectStandardOutput=$true
    $start.StandardOutputEncoding=[Text.Encoding]::UTF8
    $child=[Diagnostics.Process]::Start($start)
    $read=$child.StandardOutput.ReadToEndAsync()
    if (!$accept.Wait(14000)) {throw '健康接口未收到请求。'}
    $client=$accept.Result
    try {
        $client.ReceiveTimeout=5000
        $stream=$client.GetStream()
        $reader=New-Object IO.StreamReader($stream)
        $req=$reader.ReadLine()
        if ($req -ne 'GET /health/live HTTP/1.1') {throw '检查发起了预期以外的 API 请求'}
        while ($line=$reader.ReadLine()) {
            if ($line -match 'local-demo-token|^Authorization:|^X-Execution-Bridge-Token:') {throw '健康接口请求携带了凭据'}
        }
        $reply=[Text.Encoding]::ASCII.GetBytes('HTTP/1.1 200 OK'+[Environment]::NewLine+'Content-Length: 2'+[Environment]::NewLine+'Connection: close'+[Environment]::NewLine+[Environment]::NewLine+'OK')
        $stream.Write($reply,0,$reply.Length)
    } finally { $client.Close() }
    if (!$child.WaitForExit(10000)) {throw '只读检查未结束'}
    $output=$read.Result
    if ($child.ExitCode -ne 0 -or $output -notmatch 'HTTP 200' -or $output -notmatch '令牌已填写' -or $output.Contains('local-demo-token')) {
        throw ('检查结果异常：'+$output)
    }
    if ($listener.Pending()) {throw '只读检查发起了额外请求'}
    if ((Get-BridgeSettings).revision -ne $settings.revision) {throw '只读检查改动了配置'}
    'PASS connection check used one read-only health request and disclosed no credentials'
} finally {
    $listener.Stop()
    if ($child) { if (!$child.HasExited) { $child.Kill() }; $child.Dispose() }
    $env:TEXTILE_BRIDGE_DATA_ROOT=$before
}

