[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$WorkbookPath,

    [Parameter(Position = 1)]
    [string]$ExpectationPath,

    [ValidateRange(5, 300)]
    [int]$TimeoutSeconds = 45
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8WithoutBom
$OutputEncoding = $utf8WithoutBom

function Write-JsonToStdout {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Value
    )

    if ($Value -is [string]) {
        [Console]::Out.WriteLine($Value.TrimEnd())
        return
    }
    [Console]::Out.WriteLine(($Value | ConvertTo-Json -Depth 20))
}

function Get-FileSnapshot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $item = Get-Item -LiteralPath $Path
    return [pscustomobject][ordered]@{
        length = [long]$item.Length
        last_write_time_utc = $item.LastWriteTimeUtc.ToString("o")
        sha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

if ($env:OS -ne "Windows_NT") {
    Write-JsonToStdout -Value ([pscustomobject][ordered]@{
        schema_version = 1
        ok = $false
        code = "windows_excel_required"
        message = "此验证工具必须在安装了 Microsoft Excel 的 Windows 上运行。"
    })
    exit 1
}

$resolvedWorkbookPath = $null
$resolvedExpectationPath = ""
$outputPath = Join-Path ([System.IO.Path]::GetTempPath()) (
    "microscopy-workbook-verifier-{0}.json" -f [Guid]::NewGuid().ToString("N")
)
$excelPidPath = Join-Path ([System.IO.Path]::GetTempPath()) (
    "microscopy-workbook-verifier-{0}.pid" -f [Guid]::NewGuid().ToString("N")
)
$workerStdoutPath = Join-Path ([System.IO.Path]::GetTempPath()) (
    "microscopy-workbook-verifier-{0}.stdout" -f [Guid]::NewGuid().ToString("N")
)
$workerStderrPath = Join-Path ([System.IO.Path]::GetTempPath()) (
    "microscopy-workbook-verifier-{0}.stderr" -f [Guid]::NewGuid().ToString("N")
)
$workerProcess = $null
$beforeSnapshot = $null
$workerOutput = $null

try {
    $resolvedWorkbookPath = [System.IO.Path]::GetFullPath($WorkbookPath)
    if (-not (Test-Path -LiteralPath $resolvedWorkbookPath -PathType Leaf)) {
        throw "工作簿不存在：$resolvedWorkbookPath"
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectationPath)) {
        $resolvedExpectationPath = [System.IO.Path]::GetFullPath($ExpectationPath)
        if (-not (Test-Path -LiteralPath $resolvedExpectationPath -PathType Leaf)) {
            throw "期望清单不存在：$resolvedExpectationPath"
        }
    }
    $beforeSnapshot = Get-FileSnapshot -Path $resolvedWorkbookPath

    $workerPath = Join-Path $PSScriptRoot "Verify-MicroscopyWorkbook.Worker.ps1"
    $payload = [pscustomobject][ordered]@{
        workbook_path = $resolvedWorkbookPath
        expectation_path = $resolvedExpectationPath
        output_path = $outputPath
        excel_pid_path = $excelPidPath
    }
    $payloadJson = $payload | ConvertTo-Json -Compress
    $payloadBase64 = [Convert]::ToBase64String(
        [System.Text.Encoding]::UTF8.GetBytes($payloadJson)
    )
    $escapedWorkerPath = $workerPath.Replace("'", "''")
    $encodedInvocation = "& '$escapedWorkerPath' -PayloadBase64 '$payloadBase64'"
    $encodedCommand = [Convert]::ToBase64String(
        [System.Text.Encoding]::Unicode.GetBytes($encodedInvocation)
    )

    $workerProcess = Start-Process `
        -FilePath (Join-Path $PSHOME "powershell.exe") `
        -ArgumentList @(
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-EncodedCommand", $encodedCommand
        ) `
        -WindowStyle Hidden `
        -RedirectStandardOutput $workerStdoutPath `
        -RedirectStandardError $workerStderrPath `
        -PassThru

    if (-not $workerProcess.WaitForExit($TimeoutSeconds * 1000)) {
        try {
            Stop-Process -Id $workerProcess.Id -Force -ErrorAction Stop
        }
        catch {
            # 进程可能恰好在超时边界自行退出。
        }
        if (Test-Path -LiteralPath $excelPidPath -PathType Leaf) {
            $excelPidText = Get-Content -LiteralPath $excelPidPath -Raw
            [int]$excelPid = 0
            if ([int]::TryParse($excelPidText.Trim(), [ref]$excelPid) -and $excelPid -gt 0) {
                try {
                    Stop-Process -Id $excelPid -Force -ErrorAction Stop
                }
                catch {
                    # 仅终止本次 Worker 记录的 Excel 实例；它可能已经退出。
                }
            }
        }

        $afterSnapshot = Get-FileSnapshot -Path $resolvedWorkbookPath
        $unchanged = (
            $beforeSnapshot.length -eq $afterSnapshot.length -and
            $beforeSnapshot.last_write_time_utc -ceq $afterSnapshot.last_write_time_utc -and
            $beforeSnapshot.sha256 -ceq $afterSnapshot.sha256
        )
        Write-JsonToStdout -Value ([pscustomobject][ordered]@{
            schema_version = 1
            ok = $false
            code = "excel_open_timeout"
            message = "Excel 未在 $TimeoutSeconds 秒内完成验证，可能存在阻塞弹窗或文件损坏。"
            workbook_path = $resolvedWorkbookPath
            open_without_blocking_prompt = $false
            timed_out = $true
            workbook_unchanged = $unchanged
            before = $beforeSnapshot
            after = $afterSnapshot
        })
        exit 2
    }

    if (-not (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
        $workerError = if (Test-Path -LiteralPath $workerStderrPath -PathType Leaf) {
            (Get-Content -LiteralPath $workerStderrPath -Raw).Trim()
        }
        else {
            ""
        }
        throw "验证 Worker 未生成 JSON 结果，退出码：$($workerProcess.ExitCode)；$workerError"
    }
    $workerOutput = Get-Content -LiteralPath $outputPath -Raw -Encoding UTF8
    Write-JsonToStdout -Value $workerOutput
    exit $workerProcess.ExitCode
}
catch {
    $afterSnapshot = $null
    if (
        -not [string]::IsNullOrWhiteSpace($resolvedWorkbookPath) -and
        (Test-Path -LiteralPath $resolvedWorkbookPath -PathType Leaf)
    ) {
        $afterSnapshot = Get-FileSnapshot -Path $resolvedWorkbookPath
    }
    Write-JsonToStdout -Value ([pscustomobject][ordered]@{
        schema_version = 1
        ok = $false
        code = "verifier_failed"
        message = $_.Exception.Message
        workbook_path = $resolvedWorkbookPath
        before = $beforeSnapshot
        after = $afterSnapshot
    })
    exit 1
}
finally {
    foreach ($temporaryPath in @(
        $outputPath,
        $excelPidPath,
        $workerStdoutPath,
        $workerStderrPath
    )) {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
}
