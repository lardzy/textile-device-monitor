#requires -Version 5.1
# 配置中心、升级程序和任务监督器共用。不要将解密结果写入日志。
Add-Type -AssemblyName System.Security
$script:BridgeActiveSecretKeys = @('EXECUTION_BRIDGE_TOKEN','SMB_USER_B','SMB_PASS_B')
# 1.3.0/1.3.1 已保存的检务凭据继续保留；实际写入使用服务端随任务下发的凭据。
$script:BridgeSecretKeys = $script:BridgeActiveSecretKeys + @('LEGACY_FIBRECHECK_LOGIN','LEGACY_FIBRECHECK_PASSWORD')
$script:BridgePortableKeys = @('ApiBase','ExecutionStagingPath','PaperFiberRecordsPath','SmbTargets','OracleDataSource','OracleCredentialProfile','PollSeconds')
$script:BridgeConfigKeys = $script:BridgePortableKeys + @('WriteBridgeId','SnapshotBridgeId')
$script:BridgeUtf8 = New-Object Text.UTF8Encoding($false, $true)

function Get-BridgeDataRoot([string]$DataRoot) {
    $root = if ($DataRoot) { $DataRoot } else { $env:TEXTILE_BRIDGE_DATA_ROOT }
    if (!$root) { $root = Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'TextileExecutionBridge' }
    if (![IO.Path]::IsPathRooted($root)) { throw 'Bridge 配置目录必须是本机绝对路径。' }
    $root = [IO.Path]::GetFullPath($root).TrimEnd('\')
    if (!$root -or $root -eq ([IO.Path]::GetPathRoot($root)).TrimEnd('\') -or
        $root -eq [Environment]::GetFolderPath('CommonApplicationData') -or $root.StartsWith('\\')) {
        throw '请使用专用的本机 Bridge 配置目录。'
    }
    return $root
}

function Get-BridgeSettingsPath([string]$DataRoot) { Join-Path (Get-BridgeDataRoot $DataRoot) 'config\settings.json' }

function ConvertTo-BridgeMap($Value) {
    $map = @{}
    if ($Value -is [Collections.IDictionary]) {
        foreach ($key in $Value.Keys) { $map[$key] = $Value[$key] }
    } elseif ($Value -is [pscustomobject]) {
        foreach ($prop in $Value.PSObject.Properties) { $map[$prop.Name] = $prop.Value }
    } else { throw '配置值应是 JSON 对象。' }
    return $map
}

function Get-BridgeDefaultConfig([string]$InstallRoot) {
    $id = [guid]::NewGuid().ToString('N')
    $cfg = @{
        ApiBase = 'http://127.0.0.1/api/execution/v1'; PollSeconds = 2
        WriteBridgeId = "write-$id"; SnapshotBridgeId = "snapshot-$id"
        ExecutionStagingPath = 'D:\textile-monitor\runtime\execution-runtime'
        PaperFiberRecordsPath = ''; SmbTargets = @()
        OracleDataSource = ''; OracleCredentialProfile = ''
    }
    $template = Join-Path $InstallRoot 'defaults\BridgeConfig.psd1'
    if (Test-Path -LiteralPath $template -PathType Leaf) {
        $defaults = Import-PowerShellDataFile -LiteralPath $template
        foreach ($k in $script:BridgePortableKeys) {
            if ($k -ne 'ApiBase' -and $defaults.ContainsKey($k)) { $cfg[$k] = $defaults[$k] }
        }
    }
    return $cfg
}

function Test-BridgePathValue([string]$Path) {
    return ($Path -match '^[A-Za-z]:\\' -or $Path -match '^\\\\[^\\/]+\\[^\\/]+(?:\\|$)') -and
        $Path -notmatch '[\r\n\x00]' -and $Path -notmatch '^\\\\[?.]\\'
}

function ConvertTo-BridgeConfig([Collections.IDictionary]$Config) {
    $cfg = ConvertTo-BridgeMap $Config
    foreach ($k in $cfg.Keys) {
        if ($k -notin $script:BridgeConfigKeys) { throw "未知的现场参数：$k" }
    }
    foreach ($k in $script:BridgeConfigKeys) {
        if (!$cfg.ContainsKey($k)) { throw "配置缺少字段：$k" }
        if ($k -notin @('SmbTargets','PollSeconds')) {
            if ($null -ne $cfg[$k] -and $cfg[$k] -isnot [string]) { throw "配置字段 $k 应为文本。" }
            $cfg[$k] = ([string]$cfg[$k]).Trim()
        }
    }
    $uri = $null
    if (![Uri]::TryCreate($cfg.ApiBase, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -notin @('http','https') -or $uri.UserInfo -or $uri.Query -or $uri.Fragment) {
        throw '请填写 HTTP 或 HTTPS 服务端地址。'
    }
    $cfg.ApiBase = $uri.AbsoluteUri.TrimEnd('/')
    if ($uri.AbsolutePath -eq '/') { $cfg.ApiBase += '/api/execution/v1' }
    if (!$cfg.ApiBase.EndsWith('/api/execution/v1')) { throw '服务端地址应指向 /api/execution/v1。' }
    foreach ($k in @('WriteBridgeId','SnapshotBridgeId')) {
        if ([string]::IsNullOrWhiteSpace($cfg[$k]) -or $cfg[$k] -match '[\x00-\x1f]') {
            throw "请填写有效的 Bridge ID：$k"
        }
    }
    foreach ($k in @('ExecutionStagingPath','PaperFiberRecordsPath')) {
        if ($cfg[$k] -and !(Test-BridgePathValue $cfg[$k])) { throw "$k 需要本地绝对路径或 UNC 路径。" }
    }
    $seconds = 0.0
    if (![double]::TryParse([string]$cfg.PollSeconds, [Globalization.NumberStyles]::Float,
            [Globalization.CultureInfo]::InvariantCulture, [ref]$seconds) -or
        [double]::IsNaN($seconds) -or [double]::IsInfinity($seconds) -or $seconds -le 0) {
        throw '轮询间隔必须是大于零的秒数。'
    }
    $cfg.PollSeconds = $seconds
    $targets = @()
    foreach ($path in @($cfg.SmbTargets)) {
        if (![string]::IsNullOrWhiteSpace($path)) {
            $target = $path.Trim().TrimEnd('\')
            if ($target -notmatch '^\\\\[^\\/]+\\[^\\/]+$' -or !(Test-BridgePathValue $target)) {
                throw 'SMB 目标需要共享根路径，例如 \\server\share。'
            }
            if ($target -notin $targets) { $targets += $target }
        }
    }
    $cfg.SmbTargets = $targets
    return $cfg
}

function Resolve-BridgeOperator([string]$Account) {
    if ([string]::IsNullOrWhiteSpace($Account)) { return @{ Name=''; Sid='' } }
    if ($Account.StartsWith('.\')) { $Account = $env:COMPUTERNAME + $Account.Substring(1) }
    try {
        if ($Account -match '^S-1-') { $sid = [Security.Principal.SecurityIdentifier]::new($Account) }
        else { $sid = ([Security.Principal.NTAccount]::new($Account)).Translate([Security.Principal.SecurityIdentifier]) }
        $name = $sid.Translate([Security.Principal.NTAccount]).Value
    } catch { throw '无法解析运行任务的 Windows 账号，请使用 域名\用户名 或 计算机名\用户名。' }
    if ($sid.Value -in @('S-1-5-18','S-1-5-19','S-1-5-20')) { throw '请选择可登录桌面并运行 Excel 的操作员账号。' }
    return @{ Name=$name; Sid=$sid.Value }
}

function New-BridgeDirectoryAcl([string]$OperatorSid, [switch]$Runtime) {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true,$false)
    foreach ($id in @('S-1-5-32-544','S-1-5-18',$OperatorSid)) {
        if (!$id) { continue }
        $principal = [Security.Principal.SecurityIdentifier]::new($id)
        $rights = if ($id -in @('S-1-5-32-544','S-1-5-18')) { 'FullControl' }
                  elseif ($Runtime) { 'Modify' } else { 'ReadAndExecute' }
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $principal,$rights,'ContainerInherit,ObjectInherit','None','Allow'))
    }
    return $acl
}

function Set-BridgeFolders([string]$OperatorSid, [string]$DataRoot, [switch]$CreateOnly) {
    $root = Get-BridgeDataRoot $DataRoot
    foreach ($path in @($root,(Join-Path $root 'config'),(Join-Path $root 'runtime'))) {
        $acl = New-BridgeDirectoryAcl $OperatorSid -Runtime:($path -eq (Join-Path $root 'runtime'))
        if ([IO.Directory]::Exists($path)) {
            $dir = [IO.DirectoryInfo]::new($path)
            if (($dir.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Bridge 配置目录不能使用目录链接。'
            }
            if (!$CreateOnly) { $dir.SetAccessControl($acl) }
        } else {
            [void][IO.Directory]::CreateDirectory($path,$acl)
        }
    }
}

function Open-BridgeSettingsLock([string]$DataRoot) {
    $path = Join-Path (Get-BridgeDataRoot $DataRoot) 'config\settings.lock'
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        try { return [IO.File]::Open($path, 'OpenOrCreate', 'ReadWrite', 'None') }
        catch [IO.IOException] {
            if (($_.Exception.GetBaseException().HResult -band 0xffff) -notin @(32,33)) { throw }
            Start-Sleep -Milliseconds 60
        }
    }
    throw '另一个配置操作尚未结束，请稍后重试。'
}

function Test-BridgeRunnerBusy([string]$DataRoot) {
    foreach ($channel in @('write','snapshot')) {
        $path = Join-Path (Get-BridgeDataRoot $DataRoot) "runtime\$channel.lock"
        if (![IO.File]::Exists($path)) { continue }
        try { [IO.File]::Open($path,'Open','Read','None').Dispose() }
        catch [IO.IOException] {
            if (($_.Exception.GetBaseException().HResult -band 0xffff) -in @(32,33)) { return $true }
            throw
        }
    }
    return $false
}

function Protect-BridgeSecrets([Collections.IDictionary]$Secrets) {
    $text = ConvertTo-Json -InputObject $Secrets -Compress
    $bytes = $script:BridgeUtf8.GetBytes($text)
    try {
        $protected = [Security.Cryptography.ProtectedData]::Protect($bytes,$null,'LocalMachine')
        return [Convert]::ToBase64String($protected)
    } finally { [Array]::Clear($bytes,0,$bytes.Length) }
}

function Unlock-BridgeSecrets([string]$Ciphertext) {
    $bytes = $null
    try {
        $bytes = [Security.Cryptography.ProtectedData]::Unprotect(
            [Convert]::FromBase64String($Ciphertext),$null,'LocalMachine')
        $map = ConvertTo-BridgeMap (ConvertFrom-Json -InputObject $script:BridgeUtf8.GetString($bytes) -ErrorAction Stop)
        foreach ($key in $map.Keys) {
            if ($key -notin $script:BridgeSecretKeys -or $map[$key] -isnot [string]) { throw 'invalid' }
        }
        return $map
    } catch { throw '无法解密本机 Bridge 凭据。请检查本机配置或恢复同机备份。' }
    finally { if ($null -ne $bytes) { [Array]::Clear($bytes,0,$bytes.Length) } }
}

function Read-BridgeSettingsDocument([string]$DataRoot) {
    $path = Get-BridgeSettingsPath $DataRoot
    $stream = [IO.File]::Open($path,'Open','Read',([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    try {
        $reader = [IO.StreamReader]::new($stream,$script:BridgeUtf8,$true)
        try { $doc = ConvertTo-BridgeMap (ConvertFrom-Json -InputObject $reader.ReadToEnd() -ErrorAction Stop) }
        finally { $reader.Dispose() }
    } finally { $stream.Dispose() }
    if ($doc.schema_version -ne 1 -or $doc.revision -isnot [ValueType] -or [long]$doc.revision -lt 1) {
        throw '配置格式版本或修订号无效，请检查 settings.json 及上一份备份。'
    }
    $doc.config = ConvertTo-BridgeConfig (ConvertTo-BridgeMap $doc.config)
    $doc.enabled = ConvertTo-BridgeMap $doc.enabled
    foreach ($key in @('write','snapshot')) {
        if ($doc.enabled[$key] -isnot [bool]) { throw "配置缺少 $key 的启停状态。" }
    }
    return $doc
}

function Get-BridgeSettings([switch]$WithSecrets, [string]$DataRoot) {
    $ErrorActionPreference = 'Stop'
    $doc = Read-BridgeSettingsDocument $DataRoot
    $secrets = Unlock-BridgeSecrets $doc.secrets_dpapi
    $result = [ordered]@{ schema_version=1; revision=[long]$doc.revision; config=[pscustomobject]$doc.config
        operator=$doc.operator; operator_sid=$doc.operator_sid; enabled=[pscustomobject]$doc.enabled
        credential_keys=@($script:BridgeSecretKeys | Where-Object { ![string]::IsNullOrEmpty($secrets[$_]) }) }
    if ($WithSecrets) { $result.secrets = $secrets }
    return [pscustomobject]$result
}

function Write-BridgeSettingsDocument([Collections.IDictionary]$Document, [string]$DataRoot) {
    $path = Get-BridgeSettingsPath $DataRoot
    $tmp = Join-Path (Split-Path $path -Parent) ('.settings-' + [guid]::NewGuid() + '.tmp')
    $json = ConvertTo-Json -InputObject $Document -Depth 8
    try {
        $stream = [IO.File]::Open($tmp,'CreateNew','Write','None')
        try {
            $bytes = $script:BridgeUtf8.GetBytes($json)
            $stream.Write($bytes,0,$bytes.Length)
            $stream.Flush($true)
        } finally { $stream.Dispose() }
        if (![IO.File]::Exists($path)) { [IO.File]::Move($tmp,$path); return }
        for ($i=0; $i -lt 15; $i++) {
            try { [IO.File]::Replace($tmp,$path,"$path.bak"); return }
            catch [IO.IOException] {
                if (($_.Exception.GetBaseException().HResult -band 0xffff) -notin @(32,33) -or $i -eq 14) { throw }
                Start-Sleep -Milliseconds 40
            }
        }
    } finally { if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) } }
}

function Initialize-BridgeSettings {
    param([string]$InstallRoot = (Split-Path $PSScriptRoot -Parent), [string]$Operator, [string]$DataRoot)
    $ErrorActionPreference = 'Stop'
    if ([IO.File]::Exists((Get-BridgeSettingsPath $DataRoot))) { return Get-BridgeSettings -DataRoot $DataRoot }
    Set-BridgeFolders -OperatorSid '' -DataRoot $DataRoot -CreateOnly
    $lock = Open-BridgeSettingsLock $DataRoot
    try {
        if ([IO.File]::Exists((Get-BridgeSettingsPath $DataRoot))) { return Get-BridgeSettings -DataRoot $DataRoot }
        $cfg = Get-BridgeDefaultConfig $InstallRoot
        $legacy = Join-Path $InstallRoot 'config\BridgeConfig.psd1'
        $envFile = Join-Path $InstallRoot 'config\bridge.env'
        $secrets = @{}
        if ([IO.File]::Exists($legacy)) {
            $old = Import-PowerShellDataFile -LiteralPath $legacy -ErrorAction Stop
            foreach ($key in $script:BridgeConfigKeys) { if ($old.ContainsKey($key)) { $cfg[$key] = $old[$key] } }
            if (!$Operator) {
                $principals = @(foreach ($task in 'TextileExecutionWriteBridge','TextileExecutionSnapshotBridge') {
                    $existing = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
                    if ($existing) { (Resolve-BridgeOperator $existing.Principal.UserId).Sid }
                })
                $principals = @($principals | Select-Object -Unique)
                if ($principals.Count -gt 1) { throw '原有两个计划任务使用不同账号，请明确选择本机操作员后再迁移。' }
                if ($principals.Count -eq 1) { $Operator = $principals[0] }
            }
        }
        if ([IO.File]::Exists($envFile)) {
            try { $lines = [IO.File]::ReadAllLines($envFile,$script:BridgeUtf8) }
            catch [Text.DecoderFallbackException] { $lines = [IO.File]::ReadAllLines($envFile,[Text.Encoding]::Default) }
            foreach ($line in $lines) {
                $i = $line.IndexOf('=')
                if ($i -lt 1) { continue }
                $key = $line.Substring(0,$i).Trim()
                if ($key -in $script:BridgeSecretKeys) { $secrets[$key] = $line.Substring($i+1) }
            }
        }
        $identity = Resolve-BridgeOperator $Operator
        $doc = [ordered]@{ schema_version=1; revision=1; config=(ConvertTo-BridgeConfig $cfg)
            operator=$identity.Name; operator_sid=$identity.Sid; enabled=@{ write=$false; snapshot=$false }
            secrets_dpapi=(Protect-BridgeSecrets $secrets) }
        Set-BridgeFolders $identity.Sid $DataRoot
        Write-BridgeSettingsDocument $doc $DataRoot
        return Get-BridgeSettings -DataRoot $DataRoot
    } finally { $lock.Dispose() }
}

function Save-BridgeSettings {
    param([Parameter(Mandatory)][long]$ExpectedRevision, [Collections.IDictionary]$Config,
          [Collections.IDictionary]$Secrets, [string]$Operator, [Collections.IDictionary]$Enabled,
          [string]$DataRoot)
    $ErrorActionPreference = 'Stop'
    if ($null -eq $Config) { $Config = @{} }
    if ($null -eq $Secrets) { $Secrets = @{} }
    if ($null -eq $Enabled) { $Enabled = @{} }
    $lock = Open-BridgeSettingsLock $DataRoot
    try {
        $doc = Read-BridgeSettingsDocument $DataRoot
        if ($doc.revision -ne $ExpectedRevision) { throw '其他窗口已更新配置。请重新载入后保存，当前编辑内容仍可核对。' }
        $plain = Unlock-BridgeSecrets $doc.secrets_dpapi
        foreach ($key in $Config.Keys) {
            if ($key -notin $script:BridgeConfigKeys) { throw "未知的现场参数：$key" }
            $doc.config[$key] = $Config[$key]
        }
        $doc.config = ConvertTo-BridgeConfig $doc.config
        foreach ($key in $Secrets.Keys) {
            if ($key -notin $script:BridgeSecretKeys) { throw '未知凭据字段。' }
            if (![string]::IsNullOrEmpty($Secrets[$key])) { $plain[$key] = [string]$Secrets[$key] }
        }
        $wasEnabled = $doc.enabled.write -or $doc.enabled.snapshot
        foreach ($key in $Enabled.Keys) {
            if ($key -notin @('write','snapshot') -or $Enabled[$key] -isnot [bool]) { throw '无效的启停状态。' }
            $doc.enabled[$key] = $Enabled[$key]
        }
        $previousSid = $doc.operator_sid
        if ($PSBoundParameters.ContainsKey('Operator')) {
            $identity = Resolve-BridgeOperator $Operator
            $doc.operator = $identity.Name; $doc.operator_sid = $identity.Sid
        }
        if ($previousSid -ne $doc.operator_sid -and ($wasEnabled -or (Test-BridgeRunnerBusy $DataRoot))) {
            throw '请先停用两个桥，等待在途任务收尾后，再更换 Windows 运行账号。'
        }
        if ($doc.enabled.write -or $doc.enabled.snapshot) {
            if (!$doc.operator_sid) { throw '启用前请选择已登录的操作员账号。' }
            $needed = if ($doc.enabled.write) { $script:BridgeActiveSecretKeys } else { @('EXECUTION_BRIDGE_TOKEN') }
            foreach ($key in $needed) {
                if ([string]::IsNullOrWhiteSpace($plain[$key])) { throw "启用前请填写凭据：$key" }
            }
            if ($doc.enabled.write -and !$doc.config.ExecutionStagingPath) { throw '请选择执行暂存目录。' }
            if ($doc.enabled.snapshot -and (!$doc.config.OracleDataSource -or !$doc.config.OracleCredentialProfile)) {
                throw '请填写 Oracle 数据源和凭据配置名称。'
            }
        }
        $doc.revision = [long]$doc.revision + 1
        $doc.secrets_dpapi = Protect-BridgeSecrets $plain
        try {
            if ($previousSid -ne $doc.operator_sid) { Set-BridgeFolders $doc.operator_sid $DataRoot }
            Write-BridgeSettingsDocument $doc $DataRoot
        } catch {
            if ($previousSid -ne $doc.operator_sid) { Set-BridgeFolders $previousSid $DataRoot }
            throw
        }
        return Get-BridgeSettings -DataRoot $DataRoot
    } finally { $lock.Dispose() }
}

function Export-BridgeParameters([Parameter(Mandatory)][string]$Path, [string]$DataRoot) {
    $ErrorActionPreference = 'Stop'
    $full = [IO.Path]::GetFullPath($Path)
    if ([IO.Path]::GetExtension($full) -ne '.json' -or
        $full.StartsWith((Get-BridgeDataRoot $DataRoot) + '\',[StringComparison]::OrdinalIgnoreCase)) {
        throw '请选择配置目录之外的 .json 文件导出。'
    }
    $cfg = (Get-BridgeSettings -DataRoot $DataRoot).config
    $portable = [ordered]@{}
    foreach ($key in $script:BridgePortableKeys) { $portable[$key] = $cfg.$key }
    $out = @{ format='textile-bridge-parameters'; version=1; parameters=$portable }
    [IO.File]::WriteAllText($Path,(ConvertTo-Json -InputObject $out -Depth 6),$script:BridgeUtf8)
}

function Import-BridgeParameters([Parameter(Mandatory)][string]$Path) {
    $ErrorActionPreference = 'Stop'
    $data = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($Path,$script:BridgeUtf8)) -ErrorAction Stop
    if ($data.format -ne 'textile-bridge-parameters' -or $data.version -ne 1) { throw '请选择 Bridge 导出的普通参数文件。' }
    $values = ConvertTo-BridgeMap $data.parameters
    foreach ($key in $values.Keys) {
        if ($key -notin $script:BridgePortableKeys) { throw "导入文件包含非普通参数：$key" }
    }
    $validated = ConvertTo-BridgeConfig ($values + @{ WriteBridgeId='unused'; SnapshotBridgeId='unused' })
    $result = @{}
    foreach ($key in $script:BridgePortableKeys) { $result[$key] = $validated[$key] }
    return $result
}

