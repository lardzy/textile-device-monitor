#requires -Version 5.1
param([string]$DesktopOperator)
$ErrorActionPreference = 'Stop'
$script:BridgeInstall = Split-Path $PSScriptRoot -Parent
. (Join-Path $PSScriptRoot 'BridgeRuntime.ps1')

function Get-DesktopOperator {
    $session = [Diagnostics.Process]::GetCurrentProcess().SessionId
    $shell = Get-CimInstance Win32_Process -Filter "Name='explorer.exe' AND SessionId=$session" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($shell) {
        $owner = Invoke-CimMethod -InputObject $shell -MethodName GetOwner -ErrorAction SilentlyContinue
        if ($owner.ReturnValue -eq 0 -and $owner.User) { return "$($owner.Domain)\$($owner.User)" }
    }
    return [Security.Principal.WindowsIdentity]::GetCurrent().Name
}

function Set-CenterNotice([string]$Text, [bool]$Failed = $false) {
    $script:center.Notice.ForeColor = if ($Failed) { [Drawing.Color]::Firebrick } else { [Drawing.Color]::FromArgb(32, 78, 95) }
    $script:center.Notice.Text = $Text
}
function Invoke-CenterAction([scriptblock]$Action) {
    try { & $Action }
    catch { Set-CenterNotice $_.Exception.Message $true }
}
function Set-CenterDirty {
    if (!$script:center.Loading) {
        $script:center.Dirty = $true
        Set-CenterNotice '有未保存的修改。'
    }
}

function New-CenterButton([string]$Text, [string]$Name, [scriptblock]$Action) {
    $button = New-Object Windows.Forms.Button
    $button.Name = $Name
    $button.Text = $Text
    $button.AutoSize = $true
    $button.Padding = New-Object Windows.Forms.Padding(6, 2, 6, 2)
    $button.Add_Click($Action)
    return $button
}
function New-CenterLabel([string]$Text) {
    $label = New-Object Windows.Forms.Label
    $label.Text = $Text
    $label.AutoSize = $true
    $label.Anchor = 'Left'
    $label.Margin = New-Object Windows.Forms.Padding(3, 6, 9, 5)
    return $label
}
function New-CenterGrid($Tab) {
    $Tab.AutoScroll = $true
    $Tab.Padding = New-Object Windows.Forms.Padding(12)
    $grid = New-Object Windows.Forms.TableLayoutPanel
    $grid.Dock = 'Top'
    $grid.AutoSize = $true
    $grid.ColumnCount = 3
    [void]$grid.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('AutoSize')))
    [void]$grid.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Percent', 100)))
    [void]$grid.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('AutoSize')))
    $Tab.Controls.Add($grid)
    return $grid
}
function Add-CenterField($Grid, [string]$Key, [string]$Caption, [string]$Kind = 'text') {
    $row = $Grid.RowCount
    $Grid.RowCount++
    $label = New-CenterLabel $Caption
    $Grid.Controls.Add($label, 0, $row)
    $box = New-Object Windows.Forms.TextBox
    $box.Name = $Key
    $box.AccessibleName = $Caption
    $box.Dock = 'Fill'
    $box.Margin = New-Object Windows.Forms.Padding(3, 5, 3, 5)
    if ($Kind -eq 'password') { $box.UseSystemPasswordChar = $true }
    if ($Kind -eq 'list') {
        $box.Multiline = $true
        $box.Height = 65
        $box.ScrollBars = 'Vertical'
    }
    $script:center.Fields[$Key] = $box
    $Grid.Controls.Add($box, 1, $row)
    $box.Add_TextChanged({ Set-CenterDirty })
    if ($Kind -eq 'path') {
        $browse = New-CenterButton '浏览…' ('Browse' + $Key) {
            param($sender, $eventArgs)
            $picker = New-Object Windows.Forms.FolderBrowserDialog
            try {
                $target = $script:center.Fields[$sender.Tag]
                $picker.SelectedPath = $target.Text
                $picker.ShowNewFolderButton = $false
                if ($picker.ShowDialog($script:center.Form) -eq 'OK') { $target.Text = $picker.SelectedPath }
            } finally { $picker.Dispose() }
        }
        $browse.Tag = $Key
        $Grid.Controls.Add($browse, 2, $row)
    } elseif ($Key -in $script:center.SecretKeys) {
        $hint = New-CenterLabel ''
        $script:center.Hints[$Key] = $hint
        $Grid.Controls.Add($hint, 2, $row)
    } else { $Grid.SetColumnSpan($box, 2) }
}
function Set-CenterFields($source) {
    foreach ($key in $script:center.ConfigKeys) {
        if ($source -is [Collections.IDictionary]) {
            if (!$source.Contains($key)) { continue }
            $value = $source[$key]
        } else { $value = $source.$key }
        if ($key -eq 'SmbTargets') { $script:center.Fields[$key].Lines = [string[]]@($value) }
        else { $script:center.Fields[$key].Text = [string]$value }
    }
    $address = $null
    $local = [Uri]::TryCreate($script:center.Fields.ApiBase.Text, [UriKind]::Absolute, [ref]$address) -and $address.IsLoopback
    $script:center.Mode.SelectedIndex = if ($local) { 0 } else { 1 }
}
function Set-CenterModel($Model) {
    $script:center.Loading = $true
    try {
        $script:center.Model = $Model
        Set-CenterFields $Model.config
        $script:center.Fields.Operator.Text = if ($Model.operator) { $Model.operator } else { $script:center.DesktopOperator }
        foreach ($key in $script:center.SecretKeys) {
            $script:center.Fields[$key].Clear()
            $script:center.Hints[$key].Text = if ($key -in $Model.credential_keys) { '已保存，留空沿用' } else { '尚未填写' }
        }
        $script:center.Dirty = $false
    } finally { $script:center.Loading = $false }
}
function Save-CenterDraft {
    $cfg = @{}
    foreach ($key in $script:center.ConfigKeys) {
        if ($key -eq 'SmbTargets') { $cfg[$key] = @($script:center.Fields[$key].Lines) }
        else { $cfg[$key] = $script:center.Fields[$key].Text.Trim() }
    }
    $secret = @{}
    foreach ($key in $script:center.SecretKeys) {
        $text = $script:center.Fields[$key].Text
        if ($text.Length) { $secret[$key] = $text }
    }
    $next = Save-BridgeSettings -ExpectedRevision $script:center.Model.revision -Config $cfg -Secrets $secret -Operator $script:center.Fields.Operator.Text.Trim()
    Set-CenterModel $next
    Set-CenterNotice "已保存修订 $($next.revision)。运行中的任务完成后应用。"
    Refresh-CenterStatus
}
function Select-CenterParameters([bool]$Export) {
    if ($Export -and $script:center.Dirty) {throw '请先保存，再导出当前配置。'}
    $dialog = if ($Export) { New-Object Windows.Forms.SaveFileDialog } else { New-Object Windows.Forms.OpenFileDialog }
    try {
        $dialog.Filter = 'Bridge 配置 (*.json)|*.json'
        if ($Export) { $dialog.FileName = 'Bridge-普通参数.json' }
        if ($dialog.ShowDialog($script:center.Form) -ne 'OK') { return }
        if ($Export) {
            Export-BridgeParameters -Path $dialog.FileName
            Set-CenterNotice '普通参数已导出。'
        } else {
            $fields = Import-BridgeParameters -Path $dialog.FileName
            $script:center.Loading = $true
            try { Set-CenterFields $fields } finally { $script:center.Loading = $false }
            Set-CenterDirty
            Set-CenterNotice '普通参数已载入表单，保存后生效。'
            $script:center.Tabs.SelectedTab = $script:center.Common
        }
    } finally { $dialog.Dispose() }
}

function Refresh-CenterStatus {
    $names = @{stopped='已停用'; waiting_login='等待操作员登录'; starting='正在启动'; ready='等待任务'; working='处理任务中';
        applying='等待任务完成并应用'; draining='等待任务收尾'; error='需要处理'; receipt_unknown='等待回执核对'; recovering='正在恢复任务回执'}
    foreach ($kind in 'Write','Snapshot') {
        $line = $script:center.Bridges[$kind]
        try {
            $state = Get-BridgeRuntimeStatus -Kind $kind
            $title = $names[[string]$state.State]
            if (!$title) { $title = [string]$state.State }
            $line.State.Text = $title
            $line.State.ForeColor = if ($state.State -in 'error','receipt_unknown') { [Drawing.Color]::Firebrick } elseif ($state.State -in 'working','ready') { [Drawing.Color]::DarkGreen } else { [Drawing.Color]::DimGray }
            $target = if ($state.TargetRevision) { $state.TargetRevision } else { $script:center.Model.revision }
            $rev = if (!$state.Enabled) { '' } elseif ($state.Revision -eq $target) {
                "已应用修订 $($state.Revision)"
            } else { "等待应用修订 $target" }
            $auth = if ($state.Authenticated) { '已成功认证' } elseif ('EXECUTION_BRIDGE_TOKEN' -in $script:center.Model.credential_keys) {
                '令牌已填写；当前参数待认证'
            } else { '需填写令牌' }
            $pending = if ($state.State -eq 'receipt_unknown' -and $state.AttemptId) { '请在执行系统核对尝试 ' + $state.AttemptId }
            $line.Detail.Text = (@($state.Operator, $rev, $auth, $state.LastError, $pending) | Where-Object { $_ }) -join '  ·  '
            $line.Start.Enabled = (!$state.Enabled -or $state.State -in 'error','stopped','recovering') -and !$script:center.Dirty -and $state.State -notin 'draining','receipt_unknown'
            $line.Stop.Enabled = [bool]$state.Enabled
            if ($target -ne $script:center.Model.revision) { Set-CenterNotice '其他窗口已更新配置，请重新载入后再保存。' $true }
        } catch {
            $line.State.Text = '状态不可用'
            $line.Detail.Text = $_.Exception.Message
            $line.Start.Enabled = $false
            $line.Stop.Enabled = $false
        }
    }
}
function Set-CenterRunning([string]$Kind, [bool]$Enabled) {
    if ($Enabled -and $script:center.Dirty) {throw '请先保存修改，再启用。'}
    $model = Set-BridgeRunning -Kind $Kind -Enabled $Enabled -ExpectedRevision $script:center.Model.revision
    if ($script:center.Dirty) { $script:center.Model = $model } else { Set-CenterModel $model }
    Set-CenterNotice $(if ($Enabled) { '已启用，请查看运行状态。' } else { '已请求停用，将在当前任务收尾后停止。' })
    Refresh-CenterStatus
}
function Start-CenterCheck {
    if ($script:center.Dirty) { throw '请先保存参数，再检查连接。' }
    if ($script:center.CheckProcess) { return }
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = Join-Path $PSHOME 'powershell.exe'
    $psi.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'Test-BridgeConnections.ps1') + '"'
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
    $process = [Diagnostics.Process]::Start($psi)
    $script:center.CheckProcess = $process
    $script:center.CheckStdout = $process.StandardOutput.ReadToEndAsync()
    $script:center.CheckStderr = $process.StandardError.ReadToEndAsync()
    $script:center.CheckStarted = [DateTime]::UtcNow
    $script:center.CheckButton.Enabled = $false
    $script:center.Results.Text = '正在检查本机物料、API 健康状态和目录可读性…'
    $script:center.Tabs.SelectedTab = $script:center.Status
}
function Update-CenterCheck {
    $check = $script:center.CheckProcess
    if (!$check) { return }
    if (!$check.HasExited -and ([DateTime]::UtcNow - $script:center.CheckStarted).TotalSeconds -gt 45) {
        $check.Kill()
        $script:center.Results.Text = '检查超时，请核对服务器或共享目录的连通性。'
    } elseif ($check.HasExited) {
        $output = $script:center.CheckStdout.Result.Trim()
        $script:center.Results.Text = if ($output) { $output } else { '检查程序异常退出，请查看安装物料和网络。' }
    } else { return }
    $check.Dispose()
    $script:center.CheckProcess = $null
    $script:center.CheckButton.Enabled = $true
}

function Add-CenterStatusCard($Grid, [string]$Kind, [string]$Caption) {
    $row = $Grid.RowCount
    $Grid.RowCount += 2
    $Grid.Controls.Add((New-CenterLabel $Caption), 0, $row)
    $state = New-CenterLabel ''
    $Grid.Controls.Add($state, 1, $row)
    $buttons = New-Object Windows.Forms.FlowLayoutPanel
    $buttons.AutoSize = $true
    $Grid.Controls.Add($buttons, 2, $row)
    $start = New-CenterButton '启用' ('Enable'+$Kind) {param($s,$e) Invoke-CenterAction {Set-CenterRunning $s.Tag $true}}
    $stop = New-CenterButton '停用' ('Stop'+$Kind) {param($s,$e) Invoke-CenterAction {Set-CenterRunning $s.Tag $false}}
    $log = New-CenterButton '日志' ('Logs'+$Kind) {
        param($s,$e)
        Invoke-CenterAction {
            $name = $s.Tag.ToLowerInvariant()+'-bridge-'+(Get-Date -Format 'yyyyMMdd')+'.log'
            $dir = Join-Path $script:BridgeInstall 'work\logs'
            $file = Join-Path $dir $name
            if (Test-Path -LiteralPath $file) { Start-Process notepad.exe -ArgumentList ('"'+$file+'"') }
            elseif (Test-Path -LiteralPath $dir) { Invoke-Item -LiteralPath $dir }
            else { Set-CenterNotice '尚未生成运行日志。' }
        }
    }
    foreach ($b in $start,$stop,$log) { $b.Tag = $Kind; $buttons.Controls.Add($b) }
    $detail = New-CenterLabel ''
    $detail.MaximumSize = New-Object Drawing.Size(610, 0)
    $Grid.Controls.Add($detail, 0, $row+1)
    $Grid.SetColumnSpan($detail,3)
    $script:center.Bridges[$Kind] = @{State=$state; Detail=$detail; Start=$start; Stop=$stop}
}
function New-BridgeControlWindow([string]$Operator) {
    Add-Type -AssemblyName System.Windows.Forms,System.Drawing
    if (!('BridgeDisplay' -as [type])) {
        Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public static class BridgeDisplay { [DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); }'
        [void][BridgeDisplay]::SetProcessDPIAware()
    }
    [Windows.Forms.Application]::EnableVisualStyles()
    $model = Initialize-BridgeSettings -InstallRoot $script:BridgeInstall
    $script:center = @{
        Model=$model; DesktopOperator=$Operator; Fields=@{}; Hints=@{}; Bridges=@{}; Dirty=$false; Loading=$true; CheckProcess=$null; NextStatusRead=[DateTime]::MinValue
        ConfigKeys=@('ApiBase','ExecutionStagingPath','PaperFiberRecordsPath','SmbTargets','OracleDataSource','OracleCredentialProfile','PollSeconds','WriteBridgeId','SnapshotBridgeId')
        SecretKeys=@('EXECUTION_BRIDGE_TOKEN','SMB_USER_B','SMB_PASS_B')
    }
    $form = New-Object Windows.Forms.Form
    $script:center.Form = $form
    $form.Text = 'Windows Bridge 配置中心'
    $form.StartPosition = 'CenterScreen'
    $form.Font = New-Object Drawing.Font('Microsoft YaHei UI',9)
    $form.AutoScaleDimensions = New-Object Drawing.SizeF(96,96)
    $form.AutoScaleMode = 'Dpi'
    $form.ClientSize = New-Object Drawing.Size(850,600)
    $form.MinimumSize = New-Object Drawing.Size(650,460)
    $layout = New-Object Windows.Forms.TableLayoutPanel
    $layout.Dock = 'Fill'
    $layout.RowCount = 3
    $layout.Padding = New-Object Windows.Forms.Padding(12)
    [void]$layout.RowStyles.Add((New-Object Windows.Forms.RowStyle('Percent',100)))
    [void]$layout.RowStyles.Add((New-Object Windows.Forms.RowStyle('AutoSize')))
    [void]$layout.RowStyles.Add((New-Object Windows.Forms.RowStyle('AutoSize')))
    $layout.ColumnCount = 1
    [void]$layout.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Percent',100)))
    $form.Controls.Add($layout)
    $tabs = New-Object Windows.Forms.TabControl
    $tabs.Dock = 'Fill'
    $script:center.Tabs = $tabs
    $layout.Controls.Add($tabs,0,0)
    $common = New-Object Windows.Forms.TabPage('常用设置')
    $advanced = New-Object Windows.Forms.TabPage('高级设置')
    $status = New-Object Windows.Forms.TabPage('运行状态')
    $script:center.Common = $common
    $script:center.Status = $status
    foreach ($tab in $common,$advanced,$status) { $tabs.TabPages.Add($tab) }

    $grid = New-CenterGrid $common
    $grid.RowCount=1
    $grid.Controls.Add((New-CenterLabel '服务器部署'),0,0)
    $mode = New-Object Windows.Forms.ComboBox
    $mode.DropDownStyle='DropDownList'
    $mode.Name='ServerMode'
    $mode.Dock='Fill'
    [void]$mode.Items.AddRange(@('与服务端同机','指定服务器'))
    $grid.Controls.Add($mode,1,0); $grid.SetColumnSpan($mode,2)
    $script:center.Mode = $mode
    Add-CenterField $grid 'ApiBase' '服务端地址'
    $mode.Add_SelectedIndexChanged({
        if ($script:center.Loading) { return }
        if ($script:center.Mode.SelectedIndex -eq 0) { $script:center.Fields.ApiBase.Text='http://127.0.0.1/api/execution/v1' }
        else { $script:center.Fields.ApiBase.SelectAll(); $script:center.Fields.ApiBase.Focus() | Out-Null }
    })
    Add-CenterField $grid 'ExecutionStagingPath' '执行暂存目录' path
    Add-CenterField $grid 'PaperFiberRecordsPath' '纸浆原始记录目录' path
    Add-CenterField $grid 'Operator' 'Windows 任务账号'
    Add-CenterField $grid 'EXECUTION_BRIDGE_TOKEN' 'Bridge 令牌' password
    Add-CenterField $grid 'SMB_USER_B' '共享账号'
    Add-CenterField $grid 'SMB_PASS_B' '共享密码' password

    $grid = New-CenterGrid $advanced
    Add-CenterField $grid 'SmbTargets' 'SMB 共享（每行一个）' list
    Add-CenterField $grid 'OracleDataSource' 'Oracle 数据源'
    Add-CenterField $grid 'OracleCredentialProfile' 'Oracle 凭据配置名称'
    Add-CenterField $grid 'WriteBridgeId' '写入桥 ID'
    Add-CenterField $grid 'SnapshotBridgeId' '快照桥 ID'
    Add-CenterField $grid 'PollSeconds' '轮询间隔（秒）'
    $paths=New-CenterLabel ('本机配置目录：'+(Join-Path (Get-BridgeDataRoot) 'config'))
    $grid.Controls.Add($paths,0,$grid.RowCount); $grid.SetColumnSpan($paths,3)
    $grid = New-CenterGrid $status
    Add-CenterStatusCard $grid Write '写入桥'
    Add-CenterStatusCard $grid Snapshot '快照桥'
    $results = New-Object Windows.Forms.TextBox
    $results.Name='ConnectionResults'
    $results.AccessibleName='连接检查结果'
    $results.Multiline=$true; $results.ReadOnly=$true; $results.ScrollBars='Vertical'
    $results.Dock='Fill'; $results.Height=145
    $results.Text='点击“检查连接”查看本机、API 和共享目录的状态。'
    $grid.Controls.Add($results,0,$grid.RowCount); $grid.SetColumnSpan($results,3)
    $script:center.Results=$results
    $notice=New-CenterLabel ''
    $notice.MaximumSize=New-Object Drawing.Size(730,0)
    $script:center.Notice=$notice
    $layout.Controls.Add($notice,0,1)
    $footer=New-Object Windows.Forms.FlowLayoutPanel
    $footer.Dock='Fill'; $footer.AutoSize=$true; $footer.WrapContents=$true
    $layout.Controls.Add($footer,0,2)
    $save=New-CenterButton '保存参数' 'Save' {Invoke-CenterAction {Save-CenterDraft}}
    $check=New-CenterButton '检查连接' 'Check' {Invoke-CenterAction {Start-CenterCheck}}
    $script:center.CheckButton=$check
    $reload=New-CenterButton '重新载入' 'Reload' {
        Invoke-CenterAction {
            if ($script:center.Dirty -and [Windows.Forms.MessageBox]::Show($script:center.Form,'放弃未保存的修改并读取最新配置？','重新载入','YesNo') -ne 'Yes') { return }
            Set-CenterModel (Get-BridgeSettings)
            Refresh-CenterStatus
            Set-CenterNotice "已载入修订 $($script:center.Model.revision)。"
        }
    }
    $import=New-CenterButton '导入普通参数…' 'Import' {Invoke-CenterAction {Select-CenterParameters $false}}
    $export=New-CenterButton '导出普通参数…' 'Export' {Invoke-CenterAction {Select-CenterParameters $true}}
    foreach ($b in $save,$check,$reload,$import,$export) { $footer.Controls.Add($b) }

    Set-CenterModel $model
    Refresh-CenterStatus
    Set-CenterNotice "配置修订 $($model.revision)。"
    if ($model.enabled.write -or $model.enabled.snapshot) { $tabs.SelectedTab=$status }
    $timer=New-Object Windows.Forms.Timer
    $timer.Interval=1500
    $script:center.Timer=$timer
    $timer.Add_Tick({
        Invoke-CenterAction {
            Update-CenterCheck
            if ($script:center.Tabs.SelectedTab -eq $script:center.Status -and [DateTime]::UtcNow -ge $script:center.NextStatusRead) {
                Refresh-CenterStatus
                $script:center.NextStatusRead=[DateTime]::UtcNow.AddSeconds(5)
            }
        }
    })
    $tabs.Add_SelectedIndexChanged({ if ($script:center.Tabs.SelectedTab -eq $script:center.Status) {Refresh-CenterStatus} })
    $form.Add_Shown({
        $visible=[Windows.Forms.Screen]::FromControl($script:center.Form).WorkingArea
        if ($script:center.Form.Height -gt $visible.Height) { $script:center.Form.Height=$visible.Height; $script:center.Form.Top=$visible.Top }
        if ($script:center.Form.Width -gt $visible.Width) { $script:center.Form.Width=$visible.Width; $script:center.Form.Left=$visible.Left }
        $script:center.Timer.Start()
    })
    $form.Add_FormClosing({
        param($sender,$closingEvent)
        if ($script:center.Dirty -and [Windows.Forms.MessageBox]::Show($sender,'放弃未保存的修改并关闭？','关闭配置中心','YesNo') -ne 'Yes') {
            $closingEvent.Cancel=$true
        }
    })
    $form.Add_FormClosed({
        $script:center.Timer.Dispose()
        if ($script:center.CheckProcess) {
            if (!$script:center.CheckProcess.HasExited) { $script:center.CheckProcess.Kill() }
            $script:center.CheckProcess.Dispose()
        }
    })
    return $form
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        if (!$DesktopOperator) { $DesktopOperator = Get-DesktopOperator }
        $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
        if (!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            $arguments = '-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -DesktopOperator "' + $DesktopOperator.Replace('"','') + '"'
            Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $arguments -Verb RunAs
            exit
        }
        $window = New-BridgeControlWindow $DesktopOperator
        try { [void]$window.ShowDialog() } finally { $window.Dispose() }
    } catch {
        Add-Type -AssemblyName System.Windows.Forms
        [void][Windows.Forms.MessageBox]::Show($_.Exception.Message,'Bridge 配置中心','OK','Error')
        exit 1
    }
}

