Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-Matches {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Content,

        [Parameter(Mandatory = $true)]
        [string]$Pattern,

        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if ($Content -notmatch $Pattern) {
        throw "静态检查失败：$Message"
    }
}

function Assert-NotMatches {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Content,

        [Parameter(Mandatory = $true)]
        [string]$Pattern,

        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if ($Content -match $Pattern) {
        throw "静态检查失败：$Message"
    }
}

$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$workerPath = Join-Path $root "Verify-MicroscopyWorkbook.Worker.ps1"
$launcherPath = Join-Path $root "Verify-MicroscopyWorkbook.ps1"
$worker = Get-Content -LiteralPath $workerPath -Raw -Encoding UTF8
$launcher = Get-Content -LiteralPath $launcherPath -Raw -Encoding UTF8

foreach ($scriptPath in @($workerPath, $launcherPath)) {
    $tokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        $scriptPath,
        [ref]$tokens,
        [ref]$parseErrors
    )
    if ($parseErrors.Count -gt 0) {
        $summary = @(
            $parseErrors | ForEach-Object {
                "line $($_.Extent.StartLineNumber): $($_.Message)"
            }
        ) -join "; "
        throw "PowerShell parse failed for $scriptPath`: $summary"
    }
}

Assert-Matches -Content $worker -Pattern '\$workbooks\.Open\(' -Message "Worker 必须通过 Excel COM 打开工作簿"
Assert-Matches -Content $worker -Pattern '(?s)\$workbooks\.Open\(.{0,200}?\$true\s*\)' -Message "Open 的 ReadOnly 参数必须为 true"
Assert-Matches -Content $worker -Pattern '\$excel\.DisplayAlerts\s*=\s*\$false' -Message "必须禁用 Excel 交互提示"
Assert-Matches -Content $worker -Pattern '\$workbook\.Close\(\$false\)' -Message "关闭工作簿时必须明确不保存"
Assert-Matches -Content $worker -Pattern '(?s)finally\s*\{.{0,1800}?\$excel\.Quit\(\)' -Message "必须在 finally 中退出 Excel"
Assert-NotMatches -Content $worker -Pattern '(?i)\.(Save|SaveAs|PrintOut|ExportAsFixedFormat)\s*\(' -Message "只读验证器不得保存、导出或打印"

Assert-Matches -Content $launcher -Pattern 'WaitForExit\(' -Message "启动器必须限制 Excel 验证时长"
Assert-Matches -Content $launcher -Pattern 'excel_pid_path' -Message "启动器必须使用本次独立 Excel 的 PID 标记"
Assert-Matches -Content $launcher -Pattern 'Stop-Process\s+-Id\s+\$excelPid' -Message "超时后只能清理本次记录的 Excel 实例"
Assert-NotMatches -Content $launcher -Pattern '(?i)\.(Save|SaveAs|PrintOut|ExportAsFixedFormat)\s*\(' -Message "启动器不得包含保存、导出或打印调用"

Write-Host "PASS Test-MicroscopyWorkbookVerifier.Static.ps1"
