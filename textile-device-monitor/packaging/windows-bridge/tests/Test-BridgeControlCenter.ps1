#requires -Version 5.1
# 在 Windows PowerShell -STA 中运行。计划任务使用内存替身，窗体真实创建控件和事件。
param([string]$Screenshot)
$ErrorActionPreference = 'Stop'
$ops = Join-Path (Split-Path $PSScriptRoot -Parent) 'runtime\ops'
. (Join-Path $ops 'BridgeControlCenter.ps1')
function Assert-Ui($Condition, [string]$Message) { if (!$Condition) { throw $Message } }
$script:testModel = [pscustomobject]@{
    revision=1; operator='LAB\交互用户'; credential_keys=@('EXECUTION_BRIDGE_TOKEN')
    enabled=[pscustomobject]@{write=$false; snapshot=$false}
    config=[pscustomobject]@{
        ApiBase='http://127.0.0.1/api/execution/v1'; ExecutionStagingPath='D:\暂存'; PaperFiberRecordsPath='\\lab\纸浆'
        SmbTargets=@('\\lab\材料','\\lab\图像'); OracleDataSource='lab/orcl'; OracleCredentialProfile='oracle.profile'
        PollSeconds=2; WriteBridgeId='write-demo'; SnapshotBridgeId='snapshot-demo'
    }
}
$script:saves=0
$script:starts=0
$script:stops=0
$script:savedSecrets=$null
function Get-BridgeDataRoot { return $env:TEMP }
function Initialize-BridgeSettings { param($InstallRoot,$Operator) return $script:testModel }
function Get-BridgeSettings {return $script:testModel}
function Save-BridgeSettings {
    param($ExpectedRevision,$Config,$Secrets,$Operator)
    if ($ExpectedRevision -ne $script:testModel.revision) {throw '模拟修订冲突，请重新载入'}
    $script:testModel.config=[pscustomobject](ConvertTo-BridgeConfig $Config)
    $script:savedSecrets=$Secrets
    $script:saves++
    $script:testModel.revision++
    $script:testModel.credential_keys=@('EXECUTION_BRIDGE_TOKEN','SMB_PASS_B')
    return $script:testModel
}
function Set-BridgeRunning {
    param($Kind,$Enabled,$ExpectedRevision)
    if ($ExpectedRevision -ne $script:testModel.revision) { throw 'unexpected concurrency conflict' }
    if ($Enabled) { $script:starts++ } else { $script:stops++ }
    $key=$Kind.ToLowerInvariant()
    $script:testModel.enabled.$key=$Enabled
    $script:testModel.revision++
    return $script:testModel
}
function Get-BridgeRuntimeStatus {
    param($Kind)
    $key=$Kind.ToLowerInvariant()
    $enabled=$script:testModel.enabled.$key
    return [pscustomobject]@{State=$(if($enabled){'ready'}else{'stopped'}); Revision=$script:testModel.revision;
        Enabled=$enabled; Operator=$script:testModel.operator; Authenticated=$false; LastError=''}
}
$form=New-BridgeControlWindow $script:testModel.operator
try {
    $form.Show()
    [Windows.Forms.Application]::DoEvents()
    Assert-Ui $form.IsHandleCreated 'Expected a real Window handle'
    Assert-Ui ($script:center.Mode.Text -eq '与服务端同机') 'Initial deployment mode missing'
    $saveButton=$form.Controls.Find('Save',$true)[0]
    $lastCorner=$form.PointToClient($saveButton.PointToScreen([Drawing.Point]::new($saveButton.Width,$saveButton.Height)))
    Assert-Ui (($lastCorner.X -le $form.ClientSize.Width) -and ($lastCorner.Y -le $form.ClientSize.Height)) 'Save button is clipped'
    Assert-Ui ($script:center.Fields.ApiBase.Text -eq $script:testModel.config.ApiBase) 'API field differs'
    Assert-Ui ($script:center.Fields.EXECUTION_BRIDGE_TOKEN.Text.Length -eq 0 -and $script:center.Hints.EXECUTION_BRIDGE_TOKEN.Text -match '已保存') 'Saved token hint missing'
    Assert-Ui ($script:center.Fields.EXECUTION_BRIDGE_TOKEN.UseSystemPasswordChar) 'Bridge token must be masked'
    Assert-Ui ($script:center.Fields.SMB_PASS_B.UseSystemPasswordChar) 'SMB password must be masked'
    Assert-Ui ($script:starts -eq 0 -and $script:stops -eq 0) 'Opening window called task controls'
    $script:center.Fields.ApiBase.Text='http://localhost:8088'
    $script:center.Fields.SMB_PASS_B.Text='测试=密码'
    $script:center.Fields.SmbTargets.Lines=[string[]]@('\\lab\共享 A','\\lab\共享 B')
    $form.Controls.Find('Save',$true)[0].PerformClick()
    Assert-Ui ($script:saves -eq 1 -and $script:starts -eq 0 -and $script:stops -eq 0) "Saving failed or changed tasks: $($script:center.Notice.Text)"
    Assert-Ui ($script:testModel.config.ApiBase -eq 'http://localhost:8088/api/execution/v1') 'Bare server address not normalized'
    Assert-Ui ($script:savedSecrets.Count -eq 1 -and $script:savedSecrets.SMB_PASS_B -ceq '测试=密码') 'Existing token should be preserved'
    Assert-Ui ($script:center.Fields.SMB_PASS_B.Text -eq '') 'Password remained in control after save'
    Assert-Ui (@($script:testModel.config.SmbTargets).Count -eq 2) 'Multiple UNC paths were changed'
    $script:center.Tabs.SelectedTab=$script:center.Status
    [Windows.Forms.Application]::DoEvents()
    $form.Controls.Find('EnableWrite',$true)[0].PerformClick()
    Assert-Ui ($script:starts -eq 1 -and $script:testModel.enabled.write) "Enable button failed: $($script:center.Notice.Text)"
    $form.Controls.Find('StopWrite',$true)[0].PerformClick()
    Assert-Ui ($script:stops -eq 1 -and !$script:testModel.enabled.write) "Stop button failed: $($script:center.Notice.Text)"
    if ($Screenshot) {
        $script:center.Tabs.SelectedTab=$script:center.Common
        [Windows.Forms.Application]::DoEvents()
        $bitmap=New-Object Drawing.Bitmap($form.Width,$form.Height)
        try {$form.DrawToBitmap($bitmap,[Drawing.Rectangle]::new(0,0,$form.Width,$form.Height)); $bitmap.Save($Screenshot)} finally {$bitmap.Dispose()}
    }
    $form.Close()
    Assert-Ui ($script:starts -eq 1 -and $script:stops -eq 1) 'Closing window changed bridge lifecycle'
    'PASS Windows Bridge form, credential masking, save, independent start/stop, close'
} finally {$form.Dispose()}

