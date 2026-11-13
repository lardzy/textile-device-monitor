#requires -Version 5.1
# Run elevated on an isolated Windows machine. All data and legacy fixtures live in a unique temp directory.
param([string]$ModulePath = (Join-Path $PSScriptRoot '..\runtime\ops\BridgeSettings.ps1'))
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
. (Resolve-Path -LiteralPath $ModulePath)
$module = (Resolve-Path -LiteralPath $ModulePath).Path
$base = Join-Path ([IO.Path]::GetTempPath()) ('BridgeSettingsTest-' + [guid]::NewGuid().ToString('N'))
$actor = Get-CimInstance Win32_UserAccount -Filter 'LocalAccount=True' | Where-Object { $_.SID -match '-500$' } | Select-Object -First 1
if (!$actor) { throw 'An existing local administrator test identity is required.' }
$account = $actor.Domain + '\' + $actor.Name
$utf8 = New-Object Text.UTF8Encoding($true)
[void][IO.Directory]::CreateDirectory($base)

function Assert-Case([bool]$Ok,[string]$Name) {
    if (!$Ok) { throw "FAILED: $Name" }
    Write-Output "PASS: $Name"
}
function Expect-Failure([scriptblock]$Action,[string]$Name) {
    $rejected = $false
    try { & $Action | Out-Null } catch { $rejected = $true }
    Assert-Case $rejected $Name
}
function Assert-PrivateAcl([string]$Path,[string]$Sid) {
    $acl = Get-Acl -LiteralPath $Path
    $rules = $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])
    $unwanted = @($rules | Where-Object { $_.IdentityReference.Value -notin @('S-1-5-32-544','S-1-5-18',$Sid) })
    Assert-Case ($unwanted.Count -eq 0) "only administrators, system and selected operator may read $(Split-Path $Path -Leaf)"
}

try {
    $app = Join-Path $base 'fake-install'
    $fresh = Join-Path $base 'fresh'
    [void][IO.Directory]::CreateDirectory($app)
    $created = Initialize-BridgeSettings -InstallRoot $app -DataRoot $fresh
    Assert-Case ($created.revision -eq 1 -and !$created.operator -and !$created.enabled.write -and !$created.enabled.snapshot) 'fresh install is disabled and unassigned'
    Assert-Case ($created.config.WriteBridgeId -ne $created.config.SnapshotBridgeId) 'distinct stable IDs'
    $created = Save-BridgeSettings -DataRoot $fresh -ExpectedRevision $created.revision -Operator $account `
        -Secrets @{EXECUTION_BRIDGE_TOKEN='fixture-token';SMB_USER_B='fixture-share-user';SMB_PASS_B='fixture-share-password'} `
        -Enabled @{write=$true}
    Assert-Case ($created.enabled.write -and $created.credential_keys.Count -eq 3 -and
        @($created.credential_keys | Where-Object { $_ -like 'LEGACY_FIBRECHECK_*' }).Count -eq 0) 'fresh write bridge enables without local FibreCheck account or password'

    $legacy = Join-Path $base 'legacy-install\config'
    $active = Join-Path $base 'data'
    [void][IO.Directory]::CreateDirectory($legacy)
    $selector = @'
@{
  ApiBase='https://example.invalid/api/execution/v1'; WriteBridgeId='existing-01'; SnapshotBridgeId='snapshot-02'
  ExecutionStagingPath='D:\检验 文件\暂存'; PaperFiberRecordsPath='\\server\纸浆 原始'
  SmbTargets=@('\\server\纸浆 原始'); OracleDataSource='127.0.0.1/service'; OracleCredentialProfile='profile:中文'; PollSeconds=3
  WriterHashes=@{'old.exe'='obsolete'}
}
'@
    $fakeToken = 'fake-test-token=中'
    $fakePassword = ' 带空格=和引号"￥ '
    [IO.File]::WriteAllText((Join-Path $legacy 'BridgeConfig.psd1'),$selector,$utf8)
    [IO.File]::WriteAllText((Join-Path $legacy 'bridge.env'),
        "EXECUTION_BRIDGE_TOKEN=$fakeToken" + [Environment]::NewLine +
        "LEGACY_FIBRECHECK_LOGIN=fake-user" + [Environment]::NewLine +
        "LEGACY_FIBRECHECK_PASSWORD=$fakePassword" + [Environment]::NewLine +
        "SMB_USER_B=fake-smb" + [Environment]::NewLine + "SMB_PASS_B=fake-share-password", $utf8)
    $before = (Get-FileHash -LiteralPath (Join-Path $legacy 'bridge.env')).Hash
    $current = Initialize-BridgeSettings -InstallRoot (Split-Path $legacy -Parent) -Operator $account -DataRoot $active
    $path = Get-BridgeSettingsPath $active
    Assert-Case ($current.config.WriteBridgeId -eq 'existing-01' -and $current.config.ExecutionStagingPath -eq 'D:\检验 文件\暂存') 'legacy IDs and Unicode paths imported'
    Assert-Case (!$current.enabled.write -and !$current.enabled.snapshot -and $current.credential_keys.Count -eq 5) 'migration retains configured credentials without enabling work'
    $private = Get-BridgeSettings -DataRoot $active -WithSecrets
    Assert-Case ($private.secrets['LEGACY_FIBRECHECK_PASSWORD'] -ceq $fakePassword -and $private.secrets.EXECUTION_BRIDGE_TOKEN -ceq $fakeToken) 'DPAPI round trip preserves Unicode, whitespace and equals'
    Assert-Case ($current.PSObject.Properties.Name -notcontains 'secrets') 'ordinary read never returns credential values'
    $raw = [IO.File]::ReadAllText($path)
    Assert-Case (!$raw.Contains($fakeToken) -and !$raw.Contains($fakePassword) -and !$raw.Contains('fake-user')) 'plaintext credentials absent from settings'
    Assert-Case ((Get-FileHash -LiteralPath (Join-Path $legacy 'bridge.env')).Hash -eq $before) 'legacy configuration preserved'
    $saved = Save-BridgeSettings -DataRoot $active -ExpectedRevision $current.revision -Secrets @{LEGACY_FIBRECHECK_PASSWORD=''} -Config @{ApiBase='http://localhost:8080'}
    Assert-Case ($saved.revision -eq 2 -and $saved.config.ApiBase -eq 'http://localhost:8080/api/execution/v1') 'new revision and normalized endpoint'
    Assert-Case ((Get-BridgeSettings -DataRoot $active -WithSecrets).secrets.LEGACY_FIBRECHECK_PASSWORD -ceq $fakePassword) 'blank password preserves existing credential'
    Assert-Case ((ConvertFrom-Json ([IO.File]::ReadAllText("$path.bak"))).revision -eq 1) 'single previous generation retained'
    Assert-PrivateAcl $path $actor.SID
    Assert-PrivateAcl "$path.bak" $actor.SID
    Assert-PrivateAcl (Split-Path $path -Parent) $actor.SID
    $oldSettingsHash = (Get-FileHash -LiteralPath $path).Hash
    $again = Initialize-BridgeSettings -DataRoot $active -InstallRoot (Split-Path $legacy -Parent)
    Assert-Case ($again.revision -eq 2 -and $again.config.ApiBase -eq $saved.config.ApiBase) 'migration only runs once'
    Assert-Case ((Get-FileHash -LiteralPath $path).Hash -eq $oldSettingsHash -and
        (Get-BridgeSettings -DataRoot $active -WithSecrets).secrets.LEGACY_FIBRECHECK_PASSWORD -ceq $fakePassword) 'existing 1.3.0 and 1.3.1 settings remain readable and unchanged'

    $portable = Join-Path $base '普通参数.json'
    Export-BridgeParameters -DataRoot $active -Path $portable
    $text = [IO.File]::ReadAllText($portable)
    $import = Import-BridgeParameters -Path $portable
    Assert-Case ($import.SmbTargets[0] -eq '\\server\纸浆 原始' -and $text -notmatch 'existing-01|secrets|enabled|operator|fake-user|fake-smb|SMB_PASS_B') 'portable projection only'
    Expect-Failure { Export-BridgeParameters -DataRoot $active -Path $path } 'export cannot overwrite live configuration'
    $foreign = ConvertFrom-Json $text
    $foreign.parameters | Add-Member NoteProperty WriteBridgeId 'foreign-id'
    [IO.File]::WriteAllText($portable,($foreign | ConvertTo-Json -Depth 6),$utf8)
    Expect-Failure { Import-BridgeParameters -Path $portable } 'foreign identity cannot be imported'
    Expect-Failure { Save-BridgeSettings -DataRoot $active -ExpectedRevision 1 -Config @{PollSeconds=10} } 'stale editor rejected'
    $jobs = @(
        foreach ($seconds in @(5,7)) {
            Start-Job -ScriptBlock {
                param($Module,$Root,$Seconds)
                $ErrorActionPreference='Stop'
                . $Module
                try {
                    Save-BridgeSettings -DataRoot $Root -ExpectedRevision 2 -Config @{PollSeconds=$Seconds} | Out-Null
                    'applied'
                } catch {
                    if ($_.Exception.Message -notmatch '其他窗口') { throw }
                    'conflict'
                }
            } -ArgumentList $module,$active,$seconds
        }
    )
    try { $outcomes = @($jobs | Wait-Job | Receive-Job) }
    finally { $jobs | Remove-Job -Force }
    Assert-Case (($outcomes | Sort-Object) -join ',' -eq 'applied,conflict') 'concurrent edits settle once'
    $now = Get-BridgeSettings -DataRoot $active
    $now = Save-BridgeSettings -DataRoot $active -ExpectedRevision $now.revision -Enabled @{write=$true}
    Assert-Case ($now.enabled.write -and !$now.enabled.snapshot) 'independent enabled flags'
    Expect-Failure { Save-BridgeSettings -DataRoot $active -ExpectedRevision $now.revision -Operator '' -Enabled @{write=$false} } 'cannot switch identity while a bridge is enabled'
    $now = Save-BridgeSettings -DataRoot $active -ExpectedRevision $now.revision -Enabled @{write=$false}
    $drain = [IO.File]::Open((Join-Path $active 'runtime\write.lock'),'OpenOrCreate','ReadWrite','Read')
    try { Expect-Failure { Save-BridgeSettings -DataRoot $active -ExpectedRevision $now.revision -Operator '' } 'cannot revoke identity until the last supervisor exits' }
    finally { $drain.Dispose() }
    $next = Get-CimInstance Win32_UserAccount -Filter 'LocalAccount=True' | Where-Object { $_.SID -match '-501$' } | Select-Object -First 1
    $now = Save-BridgeSettings -DataRoot $active -ExpectedRevision $now.revision -Operator $next.SID
    Assert-Case ($now.operator_sid -eq $next.SID) 'stopped bridges can change task account'
    Assert-PrivateAcl $path $next.SID
    Assert-PrivateAcl "$path.bak" $next.SID
    $priorHash = (Get-FileHash $path).Hash
    $held = [IO.File]::Open($path,'Open','Read','Read')
    try { Expect-Failure { Save-BridgeSettings -DataRoot $active -ExpectedRevision $now.revision -Config @{PollSeconds=9} } 'locked destination rejects incomplete replacement' }
    finally { $held.Dispose() }
    Assert-Case (((Get-FileHash $path).Hash -eq $priorHash) -and !(Get-ChildItem (Split-Path $path -Parent) -Filter '.settings-*.tmp')) 'failed save leaves valid previous bytes and no temp file'
    $corrupt = ConvertFrom-Json ([IO.File]::ReadAllText($path))
    $corrupt.secrets_dpapi = 'invalid'
    [IO.File]::WriteAllText($path,($corrupt | ConvertTo-Json -Depth 8),$utf8)
    $brokenHash = (Get-FileHash $path).Hash
    Expect-Failure { Save-BridgeSettings -DataRoot $active -ExpectedRevision $now.revision -Config @{PollSeconds=11} } 'corrupt machine credentials cannot be overwritten'
    Assert-Case ((Get-FileHash $path).Hash -eq $brokenHash) 'corrupt input preserved for administrator recovery'
    Write-Output 'RESULT: PASS'
} finally {
    if ((Split-Path $base -Leaf) -match '^BridgeSettingsTest-[0-9a-f]{32}$') {
        Remove-Item -LiteralPath $base -Recurse -Force
    }
}

