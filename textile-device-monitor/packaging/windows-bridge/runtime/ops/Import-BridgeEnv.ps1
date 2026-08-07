# 从 config\bridge.env 读取键值并写入进程环境变量。
# 该文件含旧系统账号与桥令牌等机密，不得提交到 Git、不得打印内容。
param(
    [string]$EnvFile = (Join-Path (Split-Path $PSScriptRoot -Parent) 'config\bridge.env')
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "bridge.env not found: $EnvFile (copy bridge.env.example and fill it in)"
}

Get-Content -LiteralPath $EnvFile | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable(
            $matches[1].Trim(),
            $matches[2],
            'Process'
        )
    }
}
