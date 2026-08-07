#requires -Version 5.1
<#
.SYNOPSIS
    收集执行系统 Windows Bridge 的全部物料到 staging\，校验后调用 Inno Setup
    生成单一安装包 output\textile-execution-bridge-setup-<version>.exe。

.DESCRIPTION
    物料来源全部在 bridge-package.psd1 中配置：
      - 执行系统源码树内的三个 Python 工具（写入桥 / 快照桥 / 只读探针）
      - 两个已编译 Writer（校验 SHA-256 钉值，并补齐 x86 Oracle.DataAccess.dll）
      - 冻结 FibreCheck 客户端目录
      - x64 Oracle Instant Client（探针 Thick 模式）
      - CPython 运行时 + probe 离线 pip 依赖（python-deps）
      - 运维脚本 ops\ 与配置模板 config\

    用法（在 Windows 构建机、仓库根之外任意位置）：
      powershell -ExecutionPolicy Bypass -File Build-BridgePackage.ps1
      powershell -ExecutionPolicy Bypass -File Build-BridgePackage.ps1 -StageOnly   # 只收集不调 ISCC
#>
param(
    [string]$ConfigFile = (Join-Path $PSScriptRoot 'bridge-package.psd1'),
    [switch]$StageOnly,
    [string]$Compiler = ''
)

$ErrorActionPreference = 'Stop'
$PackageRoot = $PSScriptRoot
$Staging = Join-Path $PackageRoot 'staging'
$Output = Join-Path $PackageRoot 'output'

$Cfg = Import-PowerShellDataFile -LiteralPath $ConfigFile

function Assert-Path([string]$Path, [string]$What) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "缺少物料: $What -> $Path"
    }
}

function Copy-Tree([string]$Source, [string]$Dest, [string[]]$ExcludeDirs = @()) {
    $args = @($Source, $Dest, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP')
    foreach ($d in $ExcludeDirs) { $args += @('/XD', $d) }
    & robocopy @args | Out-Null
    if ($LASTEXITCODE -gt 7) { throw "robocopy failed ($LASTEXITCODE): $Source -> $Dest" }
}

Write-Output "==> 清理并重建 staging"
if (Test-Path $Staging) { Remove-Item -Recurse -Force $Staging }
New-Item -ItemType Directory -Force -Path $Staging, $Output | Out-Null

# 1. Python 工具（排除测试与缓存）
Write-Output '==> 收集 Python 工具（bridge / snapshot bridge / probe）'
$Tools = @('legacy_fibrecheck_bridge', 'legacy_fibrecheck_task_snapshot_bridge', 'legacy_fibrecheck_probe')
foreach ($tool in $Tools) {
    $src = Join-Path $Cfg.RepoSourceRoot "tools\$tool"
    Assert-Path $src "工具 $tool"
    Copy-Tree $src (Join-Path $Staging "app\$tool") -ExcludeDirs @('__pycache__', 'tests', 'out', '.pytest_cache')
}

# 2. Writer 编译产物 + 校验钉值 + 补 Oracle.DataAccess.dll
Write-Output '==> 收集 Writer 产物并校验 SHA-256 钉值'
$OdaDll = $Cfg.OracleDataAccessDll
Assert-Path $OdaDll 'x86 Oracle.DataAccess.dll'
$WriterMap = @(
    @{ Tool = 'legacy_fibrecheck_writer'; Exe = 'FibreCheckWriter.exe' },
    @{ Tool = 'legacy_fibrecheck_final_entry_writer'; Exe = 'FibreCheckFinalEntryWriter.exe' }
)
$RuntimeHashPins = @{}
foreach ($w in $WriterMap) {
    $exe = Join-Path $Cfg.RepoSourceRoot "tools\$($w.Tool)\out\$($w.Exe)"
    Assert-Path $exe "$($w.Exe) 编译产物"
    $actual = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
    $expected = $Cfg.WriterSourceHashes[$w.Exe].ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "$($w.Exe) SHA-256 与钉值不一致: $actual (期望 $expected)。若已重新编译并通过自测，请更新 bridge-package.psd1。"
    }
    $destDir = Join-Path $Staging "writers\$($w.Exe -replace '\.exe$','')"
    $outDir = Split-Path $exe -Parent
    Copy-Tree $outDir $destDir -ExcludeDirs @('Temp')
    Copy-Item $OdaDll (Join-Path $destDir 'Oracle.DataAccess.dll') -Force
    $RuntimeHashPins[$w.Exe] = $actual
}

# 3. 冻结 FibreCheck 客户端
Write-Output '==> 收集冻结 FibreCheck 客户端'
Assert-Path $Cfg.FibreCheckDir 'FibreCheck 冻结目录'
Copy-Tree $Cfg.FibreCheckDir (Join-Path $Staging 'fibrecheck')

# 4. x64 Instant Client（快照桥探针 Thick 模式）
if (Test-Path -LiteralPath $Cfg.OracleIcX64Dir) {
    Write-Output '==> 收集 x64 Oracle Instant Client'
    Copy-Tree $Cfg.OracleIcX64Dir (Join-Path $Staging 'oracle-ic-x64\instantclient_19_31')
}
else {
    Write-Output '==> 跳过 x64 Instant Client（未配置/不存在；Oracle 19c+ 可用 Thin 模式时不需要）'
}

# 5. CPython 运行时 + probe 离线依赖
Write-Output '==> 收集 CPython 运行时'
Assert-Path (Join-Path $Cfg.PythonRuntimeDir 'python.exe') 'CPython 运行时'
Copy-Tree $Cfg.PythonRuntimeDir (Join-Path $Staging 'python') -ExcludeDirs @('tcl', 'Tools')

Write-Output '==> 安装 probe 离线依赖到 python-deps'
$BundledPython = Join-Path $Staging 'python\python.exe'
# uv 版 CPython 的 ensurepip 被拒（externally-managed-environment），优先使用
# 构建机上已带 pip 的 Python（配置 PipBootstrapPython），只有随包 Python 自带
# pip 可用时才直接用它。
& $BundledPython -m pip --version 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    $PipPython = $BundledPython
}
else {
    $PipPython = $Cfg.PipBootstrapPython
    Assert-Path $PipPython 'PipBootstrapPython（含 pip 的 Python）'
    & $PipPython -m pip --version 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "PipBootstrapPython 无可用 pip: $PipPython" }
}
Write-Output "    pip 引导 Python: $PipPython"
& $PipPython -m pip install --disable-pip-version-check --no-input `
    --target (Join-Path $Staging 'python-deps') `
    -r (Join-Path $Staging 'app\legacy_fibrecheck_probe\requirements.txt') `
    -i $Cfg.PipIndexUrl
if ($LASTEXITCODE -ne 0) { throw "pip 安装 probe 依赖失败: $LASTEXITCODE" }
$env:PYTHONPATH = Join-Path $Staging 'python-deps'
& $BundledPython -c "import oracledb; print('oracledb', oracledb.version)"
if ($LASTEXITCODE -ne 0) { throw 'python-deps 中 oracledb 导入失败' }
Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue

# 6. 运维脚本与配置模板（生成运行时 Writer 钉值）
Write-Output '==> 收集运维脚本与配置模板'
Copy-Tree (Join-Path $PackageRoot 'runtime\ops') (Join-Path $Staging 'ops')
New-Item -ItemType Directory -Force -Path (Join-Path $Staging 'config') | Out-Null
Copy-Item (Join-Path $PackageRoot 'runtime\config\bridge.env.example') (Join-Path $Staging 'config') -Force
$configTemplate = Get-Content -Raw -LiteralPath (Join-Path $PackageRoot 'runtime\config\BridgeConfig.psd1')
foreach ($pair in $RuntimeHashPins.GetEnumerator()) {
    $configTemplate = $configTemplate -replace (
        [regex]::escape("'$($pair.Key)'") + "\s*=\s*'REPLACE_BY_BUILD'"
    ), "'$($pair.Key)'            = '$($pair.Value)'"
}
Set-Content -LiteralPath (Join-Path $Staging 'config\BridgeConfig.psd1') -Value $configTemplate -Encoding UTF8

# 7. manifest + version.auto.iss
Write-Output '==> 生成 manifest 与 version.auto.iss'
$manifest = [ordered]@{
    package         = 'textile-execution-bridge'
    version         = $Cfg.PackageVersion
    built_at        = (Get-Date).ToString('o')
    repo_source     = $Cfg.RepoSourceRoot
    writer_sha256   = $RuntimeHashPins
    staging_files   = (Get-ChildItem $Staging -Recurse -File | Measure-Object).Count
    staging_size_mb = [math]::Round(((Get-ChildItem $Staging -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Staging 'manifest.json') -Encoding UTF8
"#define MyAppVersion `"$($Cfg.PackageVersion)`"" | Set-Content -LiteralPath (Join-Path $PackageRoot 'version.auto.iss') -Encoding ASCII

if ($StageOnly) {
    Write-Output "StageOnly 完成: $Staging"
    exit 0
}

# 8. 调用 Inno Setup
Write-Output '==> 调用 Inno Setup 编译安装包'
$Iscc = $Compiler
if (-not $Iscc) {
    $candidates = @(
        "$env:ISCC_EXE",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) }
    $Iscc = $candidates | Select-Object -First 1
}
if (-not $Iscc) { throw '未找到 ISCC.exe，请安装 Inno Setup 6 或用 -Compiler 指定' }

$IssPath = Join-Path $PackageRoot 'bridge-installer.iss'
& $Iscc $IssPath
if ($LASTEXITCODE -ne 0) { throw "ISCC 编译失败: $LASTEXITCODE" }

$SetupExe = Join-Path $Output "textile-execution-bridge-setup-$($Cfg.PackageVersion).exe"
if (-not (Test-Path $SetupExe)) { throw "ISCC 完成但未找到产物: $SetupExe" }
$Hash = (Get-FileHash -LiteralPath $SetupExe -Algorithm SHA256).Hash.ToLowerInvariant()
"$Hash  $(Split-Path $SetupExe -Leaf)" | Set-Content -LiteralPath "$SetupExe.sha256" -Encoding ASCII

Write-Output ''
Write-Output "安装包: $SetupExe"
Write-Output "SHA256: $Hash"
