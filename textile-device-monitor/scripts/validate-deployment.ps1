[CmdletBinding()]
param(
    [string]$EnvFile = ".env",
    [string[]]$ComposeFile = @("docker-compose.yml"),
    [string]$ProbeAddress = "",
    [string]$PythonPath = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonCommand = Get-Command $PythonPath -ErrorAction SilentlyContinue
if ($null -eq $PythonCommand) {
    throw "未找到 Python：$PythonPath"
}
& $PythonCommand.Source -c "import cryptography"
if ($LASTEXITCODE -ne 0) {
    throw (
        "部署预检缺少依赖，请先执行：$PythonPath -m pip install -r " +
        (Join-Path $PSScriptRoot "requirements-deployment.txt")
    )
}
$env:PYTHONPATH = @(
    (Join-Path $ProjectRoot "backend"),
    $env:PYTHONPATH
) -join [IO.Path]::PathSeparator

$Arguments = @(
    "-m",
    "app.deployment_validation",
    "--env-file",
    $EnvFile
)
foreach ($File in $ComposeFile) {
    $Arguments += @("--compose-file", $File)
}
if ($ProbeAddress) {
    $Arguments += @("--probe-address", $ProbeAddress)
}

& $PythonCommand.Source @Arguments
exit $LASTEXITCODE
