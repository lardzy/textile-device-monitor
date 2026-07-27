[CmdletBinding()]
param(
    [ValidateSet("Audit", "Apply", "Validate", "Restore")]
    [string]$Mode = "Audit",

    [string]$ServerIp,

    [string[]]$ManagementClientIp = @(),

    [string[]]$ObservedRemoteAddress = @(),

    [string[]]$ExpectedClientAddress = @(),

    [switch]$ConfirmDockerSourceIpPreserved,

    [string]$ManifestPath,

    [string]$StateDirectory = "$env:ProgramData\TextileDeviceMonitor\Firewall",

    [string]$DisplayName = "Textile Monitor HTTPS 443（受管）"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "InspectionTls.Common.ps1")

Assert-WindowsAdministrator

function ConvertTo-PrivateIpv4 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Address,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if ($Address.Contains("/")) {
        throw "$Label 必须是单个 IPv4 地址，不能使用网段：$Address"
    }
    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($Address, [ref]$parsed)) {
        throw "$Label 不是有效 IPv4 地址：$Address"
    }
    if (
        $parsed.AddressFamily -ne
        [System.Net.Sockets.AddressFamily]::InterNetwork
    ) {
        throw "$Label 只允许 IPv4 地址：$Address"
    }
    $octets = $parsed.GetAddressBytes()
    $isPrivate = (
        $octets[0] -eq 10 -or
        (
            $octets[0] -eq 172 -and
            $octets[1] -ge 16 -and
            $octets[1] -le 31
        ) -or
        ($octets[0] -eq 192 -and $octets[1] -eq 168)
    )
    if (-not $isPrivate) {
        throw "$Label 必须是 RFC1918 局域网 IPv4 地址：$Address"
    }
    return $parsed.ToString()
}

function ConvertTo-UniquePrivateIpv4List {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Address,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    return @(
        foreach ($item in $Address) {
            ConvertTo-PrivateIpv4 -Address $item -Label $Label
        }
    ) | Sort-Object -Unique
}

function Test-PortFilterIncludes443 {
    param(
        [Parameter(Mandatory = $true)]
        [object]$PortFilter
    )

    $protocol = [string]$PortFilter.Protocol
    if ($protocol -notin @("TCP", "6", "Any", "256")) {
        return $false
    }
    foreach ($value in @($PortFilter.LocalPort)) {
        $port = [string]$value
        if ($port -in @("Any", "*", "443")) {
            return $true
        }
        if ($port -match "^(?<start>\d+)-(?<end>\d+)$") {
            if (
                [int]$Matches["start"] -le 443 -and
                [int]$Matches["end"] -ge 443
            ) {
                return $true
            }
        }
    }
    return $false
}

function Get-CompetingHttpsAllowRules {
    param(
        [string]$ExcludedRuleName,

        [Parameter(Mandatory = $true)]
        [string]$TargetServerIp,

        [Parameter(Mandatory = $true)]
        [string[]]$AllowedClientIp
    )

    $matches = New-Object System.Collections.Generic.List[object]
    $rules = @(
        Get-NetFirewallRule -PolicyStore ActiveStore -ErrorAction Stop |
        Where-Object {
            $_.Enabled -eq "True" -and
            $_.Direction -eq "Inbound" -and
            $_.Action -eq "Allow" -and
            (
                [string]::IsNullOrWhiteSpace($ExcludedRuleName) -or
                $_.Name -cne $ExcludedRuleName
            )
        }
    )
    foreach ($rule in $rules) {
        $portFilters = @(
            $rule |
            Get-NetFirewallPortFilter -ErrorAction Stop
        )
        if (
            @(
                $portFilters |
                Where-Object { Test-PortFilterIncludes443 -PortFilter $_ }
            ).Count -eq 0
        ) {
            continue
        }
        $addressFilter = $rule |
            Get-NetFirewallAddressFilter -ErrorAction Stop |
            Select-Object -First 1
        $applicationFilter = $rule |
            Get-NetFirewallApplicationFilter -ErrorAction Stop |
            Select-Object -First 1
        $serviceFilter = $rule |
            Get-NetFirewallServiceFilter -ErrorAction Stop |
            Select-Object -First 1
        $program = [string]$applicationFilter.Program
        $service = [string]$serviceFilter.Service
        $programCouldOwnDockerPublish = (
            [string]::IsNullOrWhiteSpace($program) -or
            $program -in @("Any", "*", "System") -or
            $program -match "(?i)docker|vpnkit|wsl|hostnet"
        )
        $serviceCouldOwnDockerPublish = (
            [string]::IsNullOrWhiteSpace($service) -or
            $service -in @("Any", "*") -or
            $service -match "(?i)docker|wsl|hns|vmcompute"
        )
        if (-not (
            $programCouldOwnDockerPublish -and
            $serviceCouldOwnDockerPublish
        )) {
            # A rule bound to a clearly unrelated executable/service cannot
            # authorize Docker Desktop's published listener.
            continue
        }
        $localAddresses = @(
            $addressFilter.LocalAddress |
            ForEach-Object { [string]$_ }
        )
        $localCouldMatch = $false
        foreach ($local in $localAddresses) {
            if (
                $local -in @("Any", "*", "LocalSubnet") -or
                $local -ceq $TargetServerIp
            ) {
                $localCouldMatch = $true
                break
            }
            if ($local -match "^(?<ip>[^/]+)/32$" -and
                $Matches["ip"] -ceq $TargetServerIp) {
                $localCouldMatch = $true
                break
            }
            if ($local -match "/" -or $local -match "-") {
                # Non-/32 ranges require policy-owner review; fail closed.
                $localCouldMatch = $true
                break
            }
        }
        if (-not $localCouldMatch) {
            continue
        }
        $remoteAddresses = @(
            $addressFilter.RemoteAddress |
            ForEach-Object { [string]$_ }
        )
        $remoteScopeIsSubset = $remoteAddresses.Count -gt 0
        foreach ($remote in $remoteAddresses) {
            $normalizedRemote = if ($remote -match "^(?<ip>[^/]+)/32$") {
                $Matches["ip"]
            }
            else {
                $remote
            }
            if ($AllowedClientIp -notcontains $normalizedRemote) {
                $remoteScopeIsSubset = $false
                break
            }
        }
        if ($remoteScopeIsSubset) {
            # This rule does not authorize a client outside the exact /32
            # whitelist, so it does not weaken the intended boundary.
            continue
        }
        $matches.Add([pscustomobject]@{
            Name = [string]$rule.Name
            DisplayName = [string]$rule.DisplayName
            Profile = [string]$rule.Profile
            LocalPort = @($portFilters.LocalPort) -join ","
            LocalAddress = $localAddresses -join ","
            RemoteAddress = @($addressFilter.RemoteAddress) -join ","
            Program = $program
            Service = $service
        })
    }
    return @($matches)
}

function Get-ActiveProfileBlockers {
    return @(
        Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop |
        Where-Object {
            $_.Enabled -eq "True" -and
            $_.DefaultInboundAction -ne "Block"
        } |
        ForEach-Object {
            [pscustomobject]@{
                Name = [string]$_.Name
                DefaultInboundAction = [string]$_.DefaultInboundAction
            }
        }
    )
}

function Get-Manifest {
    $path = $ManifestPath
    if ([string]::IsNullOrWhiteSpace($path)) {
        $latestPath = Join-Path (
            [System.IO.Path]::GetFullPath($StateDirectory)
        ) "latest.json"
        if (-not (Test-Path -LiteralPath $latestPath -PathType Leaf)) {
            throw "未找到防火墙 latest.json，请显式提供 ManifestPath。"
        }
        $latest = Get-Content -LiteralPath $latestPath -Raw | ConvertFrom-Json
        $path = [string]$latest.manifest_path
    }
    $resolved = [System.IO.Path]::GetFullPath($path)
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "防火墙清单不存在：$resolved"
    }
    return [pscustomobject]@{
        Path = $resolved
        Value = Get-Content -LiteralPath $resolved -Raw | ConvertFrom-Json
    }
}

function Assert-ManagedRuleMatchesManifest {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Manifest
    )

    $rule = Get-NetFirewallRule `
        -Name ([string]$Manifest.rule_name) `
        -PolicyStore ActiveStore `
        -ErrorAction SilentlyContinue
    if ($null -eq $rule) {
        throw "受管 HTTPS 防火墙规则不存在：$($Manifest.rule_name)"
    }
    if (
        $rule.Enabled -ne "True" -or
        $rule.Direction -ne "Inbound" -or
        $rule.Action -ne "Allow" -or
        $rule.EdgeTraversalPolicy -ne "Block" -or
        $rule.Profile -ne "Any" -or
        $rule.Group -cne "TextileDeviceMonitor.Managed" -or
        $rule.DisplayName -cne [string]$Manifest.display_name
    ) {
        throw "受管 HTTPS 防火墙规则身份、动作、方向或范围已漂移。"
    }
    $ports = @($rule | Get-NetFirewallPortFilter -ErrorAction Stop)
    if ($ports.Count -ne 1) {
        throw "受管规则端口过滤器数量异常。"
    }
    $port = $ports[0]
    if ([string]$port.Protocol -notin @("TCP", "6") -or
        [string]$port.LocalPort -cne "443") {
        throw "受管规则不再严格限定 TCP 443。"
    }
    $addresses = @($rule | Get-NetFirewallAddressFilter -ErrorAction Stop)
    if ($addresses.Count -ne 1) {
        throw "受管规则地址过滤器数量异常。"
    }
    $address = $addresses[0]
    $expectedRemote = @(
        foreach ($item in @($Manifest.management_client_ip)) {
            "$item/32"
        }
    ) | Sort-Object
    $actualRemote = @(
        foreach ($item in @($address.RemoteAddress)) {
            $text = [string]$item
            if ($text -notmatch "/") {
                "$text/32"
            }
            else {
                $text
            }
        }
    ) | Sort-Object
    if (@(
        Compare-Object `
            -ReferenceObject $expectedRemote `
            -DifferenceObject $actualRemote
    ).Count -ne 0) {
        throw "受管规则远程地址已漂移，必须仍是逐台 /32 白名单。"
    }
    $localAddress = @($address.LocalAddress)
    if ($localAddress.Count -ne 1 -or
        [string]$localAddress[0] -cne [string]$Manifest.server_ip) {
        throw "受管规则本地地址已漂移。"
    }
    $applications = @(
        $rule | Get-NetFirewallApplicationFilter -ErrorAction Stop
    )
    $services = @($rule | Get-NetFirewallServiceFilter -ErrorAction Stop)
    $interfaces = @(
        $rule | Get-NetFirewallInterfaceTypeFilter -ErrorAction Stop
    )
    if (
        $applications.Count -ne 1 -or
        [string]($applications[0].Program) -notin @("Any", "*") -or
        $services.Count -ne 1 -or
        [string]($services[0].Service) -notin @("Any", "*") -or
        $interfaces.Count -ne 1 -or
        [string]($interfaces[0].InterfaceType) -ne "Any"
    ) {
        throw "受管规则程序、服务或接口过滤器已漂移。"
    }
}

$stateRoot = [System.IO.Path]::GetFullPath($StateDirectory)
if ($Mode -in @("Audit", "Apply")) {
    if ([string]::IsNullOrWhiteSpace($ServerIp)) {
        throw "$Mode 模式必须提供 ServerIp。"
    }
    $normalizedServerIp = ConvertTo-PrivateIpv4 `
        -Address $ServerIp `
        -Label "ServerIp"
    $managementAddresses = @(
        ConvertTo-UniquePrivateIpv4List `
            -Address $ManagementClientIp `
            -Label "ManagementClientIp"
    )
    if ($managementAddresses.Count -eq 0) {
        throw "至少提供一台管理终端的 IPv4 /32 白名单。"
    }
    if ($managementAddresses -contains $normalizedServerIp) {
        throw "管理终端白名单不能用服务器自身地址代替客户端地址。"
    }

    $profileBlockers = @(Get-ActiveProfileBlockers)
    $competingRules = @(
        Get-CompetingHttpsAllowRules `
            -TargetServerIp $normalizedServerIp `
            -AllowedClientIp $managementAddresses
    )
    $audit = [ordered]@{
        schema_version = 1
        audited_at_utc = [datetime]::UtcNow.ToString("o")
        mode = $Mode
        server_ip = $normalizedServerIp
        management_client_ip = $managementAddresses
        remote_scope = @(
            $managementAddresses | ForEach-Object { "$_/32" }
        )
        active_profile_blockers = $profileBlockers
        competing_https_allow_rules = $competingRules
        safe_to_apply = (
            $profileBlockers.Count -eq 0 -and
            $competingRules.Count -eq 0
        )
        note = (
            "只审计本机 Windows 防火墙；不会修改公司 DNS、路由器、" +
            "代理/PAC 或既有防火墙规则。"
        )
    }
    $audit | ConvertTo-Json -Depth 8
    if ($Mode -eq "Audit") {
        if (-not $audit.safe_to_apply) {
            Write-Warning (
                "审计发现阻断项；请由管理员人工处置后重新 Audit。" +
                "本脚本不会禁用既有或 Docker/公司规则。"
            )
        }
        return
    }

    if (-not $audit.safe_to_apply) {
        throw "存在默认入站非 Block 配置或竞争性 TCP 443 Allow 规则，拒绝 Apply。"
    }
    if (-not $ConfirmDockerSourceIpPreserved) {
        throw (
            "必须先从 Nginx remote_addr 取得灰度证据，并显式提供 " +
            "-ConfirmDockerSourceIpPreserved。"
        )
    }
    $observed = @(
        ConvertTo-UniquePrivateIpv4List `
            -Address $ObservedRemoteAddress `
            -Label "ObservedRemoteAddress"
    )
    $expected = @(
        ConvertTo-UniquePrivateIpv4List `
            -Address $ExpectedClientAddress `
            -Label "ExpectedClientAddress"
    )
    if ($observed.Count -eq 0 -or $expected.Count -eq 0) {
        throw "Apply 必须同时提供 Nginx 实际 remote_addr 与预期客户端地址。"
    }
    if ($observed -contains $normalizedServerIp) {
        throw "Nginx remote_addr 显示为服务器自身地址，Docker 未保留客户端源 IP。"
    }
    if (@(
        Compare-Object -ReferenceObject $expected -DifferenceObject $observed
    ).Count -ne 0) {
        throw (
            "Nginx remote_addr 与预期客户端 IP 不一致，疑似代理/NAT，" +
            "拒绝创建可能失效的白名单。"
        )
    }
    foreach ($address in $observed) {
        if ($managementAddresses -notcontains $address) {
            throw "观测到的客户端 $address 不在逐台管理白名单中。"
        }
    }

    New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
    $runId = [datetime]::UtcNow.ToString("yyyyMMdd-HHmmss") + "-" +
        [guid]::NewGuid().ToString("N").Substring(0, 8)
    $runDirectory = Join-Path $stateRoot $runId
    New-Item -ItemType Directory -Path $runDirectory | Out-Null
    $ruleName = "TextileMonitor-Https443-$runId"
    $runManifestPath = Join-Path $runDirectory "firewall-manifest.json"
    $manifest = [ordered]@{
        schema_version = 1
        run_id = $runId
        status = "applying"
        started_at_utc = [datetime]::UtcNow.ToString("o")
        server_ip = $normalizedServerIp
        management_client_ip = $managementAddresses
        observed_remote_address = $observed
        expected_client_address = $expected
        docker_source_ip_preserved_confirmed = $true
        rule_name = $ruleName
        display_name = $DisplayName
        created_rule = $false
        changed_existing_rules = @()
    }
    Write-JsonFileAtomically -Value $manifest -Path $runManifestPath
    try {
        New-NetFirewallRule `
            -Name $ruleName `
            -DisplayName $DisplayName `
            -Group "TextileDeviceMonitor.Managed" `
            -PolicyStore PersistentStore `
            -Enabled True `
            -Direction Inbound `
            -Action Allow `
            -Profile Any `
            -Protocol TCP `
            -LocalAddress $normalizedServerIp `
            -LocalPort 443 `
            -RemoteAddress $managementAddresses `
            -InterfaceType Any `
            -EdgeTraversalPolicy Block | Out-Null
        $manifest.created_rule = $true
        Assert-ManagedRuleMatchesManifest -Manifest ([pscustomobject]$manifest)
        $postCompeting = @(
            Get-CompetingHttpsAllowRules `
                -ExcludedRuleName $ruleName `
                -TargetServerIp $normalizedServerIp `
                -AllowedClientIp $managementAddresses
        )
        if ($postCompeting.Count -ne 0) {
            throw "创建后发现竞争性 TCP 443 Allow 规则，拒绝保留受管规则。"
        }
        $manifest.status = "applied"
        $manifest["applied_at_utc"] = [datetime]::UtcNow.ToString("o")
        Write-JsonFileAtomically -Value $manifest -Path $runManifestPath
        Write-JsonFileAtomically `
            -Value ([ordered]@{
                schema_version = 1
                manifest_path = $runManifestPath
                run_id = $runId
                status = "applied"
            }) `
            -Path (Join-Path $stateRoot "latest.json")
        Write-Host "已创建仅允许逐台客户端 IPv4 /32 访问 TCP 443 的受管规则。"
        Write-Host "清单：$runManifestPath"
    }
    catch {
        $failure = $_
        if ($manifest.created_rule) {
            Remove-NetFirewallRule `
                -Name $ruleName `
                -PolicyStore PersistentStore `
                -ErrorAction SilentlyContinue
        }
        $manifest.status = "failed_rolled_back"
        $manifest["failed_at_utc"] = [datetime]::UtcNow.ToString("o")
        $manifest["error"] = [string]$failure
        Write-JsonFileAtomically -Value $manifest -Path $runManifestPath
        throw $failure
    }
    return
}

$loaded = Get-Manifest
$loadedManifest = $loaded.Value
if ($Mode -eq "Validate") {
    if ($loadedManifest.status -notin @("applied", "validated")) {
        throw "清单状态不允许验证：$($loadedManifest.status)"
    }
    Assert-ManagedRuleMatchesManifest -Manifest $loadedManifest
    $profileBlockers = @(Get-ActiveProfileBlockers)
    if ($profileBlockers.Count -ne 0) {
        throw "活动防火墙配置的默认入站动作不再全部为 Block。"
    }
    $competingRules = @(
        Get-CompetingHttpsAllowRules `
            -ExcludedRuleName ([string]$loadedManifest.rule_name) `
            -TargetServerIp ([string]$loadedManifest.server_ip) `
            -AllowedClientIp @($loadedManifest.management_client_ip)
    )
    if ($competingRules.Count -ne 0) {
        throw "发现竞争性 TCP 443 Allow 规则，Windows 白名单已不再是有效边界。"
    }
    $loadedManifest.status = "validated"
    $loadedManifest | Add-Member `
        -NotePropertyName validated_at_utc `
        -NotePropertyValue ([datetime]::UtcNow.ToString("o")) `
        -Force
    Write-JsonFileAtomically -Value $loadedManifest -Path $loaded.Path
    Write-Host "Windows 防火墙 TCP 443 逐台 /32 白名单验证通过。"
    return
}

if ($loadedManifest.status -notin @("applied", "validated")) {
    throw "清单状态不允许恢复：$($loadedManifest.status)"
}
if (-not [bool]$loadedManifest.created_rule) {
    throw "清单未记录由本次操作创建的规则，拒绝删除任何规则。"
}
Assert-ManagedRuleMatchesManifest -Manifest $loadedManifest
Remove-NetFirewallRule `
    -Name ([string]$loadedManifest.rule_name) `
    -PolicyStore PersistentStore `
    -ErrorAction Stop
$loadedManifest.status = "restored"
$loadedManifest | Add-Member `
    -NotePropertyName restored_at_utc `
    -NotePropertyValue ([datetime]::UtcNow.ToString("o")) `
    -Force
Write-JsonFileAtomically -Value $loadedManifest -Path $loaded.Path
$latestPath = Join-Path $stateRoot "latest.json"
if (Test-Path -LiteralPath $latestPath -PathType Leaf) {
    $latest = Get-Content -LiteralPath $latestPath -Raw | ConvertFrom-Json
    if ([string]$latest.manifest_path -ceq $loaded.Path) {
        $latest.status = "restored"
        Write-JsonFileAtomically -Value $latest -Path $latestPath
    }
}
Write-Host "仅删除了清单记录的本次受管规则；既有/公司规则未被修改。"
