[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PayloadBase64
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Import-Module (
    Join-Path $PSScriptRoot "MicroscopyWorkbookVerifier.Common.psm1"
) -Force

function Test-HasProperty {
    param(
        [AllowNull()]
        [object]$Value,

        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    return $null -ne $Value -and $null -ne $Value.PSObject.Properties[$Name]
}

function Release-ComObject {
    param(
        [AllowNull()]
        [object]$Value
    )

    try {
        if ($null -ne $Value -and [System.Runtime.InteropServices.Marshal]::IsComObject($Value)) {
            [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($Value)
        }
    }
    catch {
        # COM 已由父对象释放时无需再次释放；不得让清理覆盖主验证结果。
    }
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

function Test-SnapshotEqual {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Before,

        [Parameter(Mandatory = $true)]
        [object]$After
    )

    return (
        [long]$Before.length -eq [long]$After.length -and
        [string]$Before.last_write_time_utc -ceq [string]$After.last_write_time_utc -and
        [string]$Before.sha256 -ceq [string]$After.sha256
    )
}

function New-Check {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Code,

        [Parameter(Mandatory = $true)]
        [bool]$Passed,

        [Parameter(Mandatory = $true)]
        [string]$Message,

        [AllowNull()]
        [object]$Details = $null
    )

    return [pscustomobject][ordered]@{
        code = $Code
        passed = $Passed
        message = $Message
        details = $Details
    }
}

function Write-ResultFile {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Value,

        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $json = $Value | ConvertTo-Json -Depth 20
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        $Path,
        $json + [Environment]::NewLine,
        $utf8WithoutBom
    )
}

$payloadJson = [System.Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String($PayloadBase64)
)
$payload = $payloadJson | ConvertFrom-Json
$workbookPath = [System.IO.Path]::GetFullPath([string]$payload.workbook_path)
$expectationPath = [string]$payload.expectation_path
$outputPath = [string]$payload.output_path
$excelPidPath = [string]$payload.excel_pid_path

$expectation = $null
if (-not [string]::IsNullOrWhiteSpace($expectationPath)) {
    $expectation = Get-Content -LiteralPath $expectationPath -Raw -Encoding UTF8 |
        ConvertFrom-Json
}

$worksheetName = "微观形貌"
$canvasAddress = "A4:L32"
$containmentTolerance = 1.5
$overlapTolerance = 0.5
$positionTolerance = 1.5
$sizeTolerance = 1.5
$aspectRatioTolerance = 0.02
$expectedPrintArea = '$A$1:$L$37'

if (Test-HasProperty -Value $expectation -Name "worksheet_name") {
    $worksheetName = [string]$expectation.worksheet_name
}
if (Test-HasProperty -Value $expectation -Name "canvas_address") {
    $canvasAddress = [string]$expectation.canvas_address
}
if (Test-HasProperty -Value $expectation -Name "containment_tolerance_points") {
    $containmentTolerance = [double]$expectation.containment_tolerance_points
}
if (Test-HasProperty -Value $expectation -Name "overlap_tolerance_points") {
    $overlapTolerance = [double]$expectation.overlap_tolerance_points
}
if (Test-HasProperty -Value $expectation -Name "position_tolerance_points") {
    $positionTolerance = [double]$expectation.position_tolerance_points
}
if (Test-HasProperty -Value $expectation -Name "size_tolerance_points") {
    $sizeTolerance = [double]$expectation.size_tolerance_points
}
if (Test-HasProperty -Value $expectation -Name "aspect_ratio_tolerance") {
    $aspectRatioTolerance = [double]$expectation.aspect_ratio_tolerance
}
if (Test-HasProperty -Value $expectation -Name "print_area") {
    $expectedPrintArea = [string]$expectation.print_area
}

$beforeSnapshot = Get-FileSnapshot -Path $workbookPath
$excel = $null
$workbooks = $null
$workbook = $null
$worksheets = $null
$worksheet = $null
$canvasRange = $null
$shapeCollection = $null
$nameCollection = $null
$pageSetup = $null
$cleanupErrors = New-Object System.Collections.Generic.List[string]
$failure = $null
$checks = New-Object System.Collections.Generic.List[object]
$imageResults = New-Object System.Collections.Generic.List[object]
$printAreaNames = New-Object System.Collections.Generic.List[object]
$canvasResult = $null
$pageSetupPrintArea = $null
$printAreaMatches = $false
$isReadOnly = $false
$openCompletedAt = $null

try {
    $excel = New-Object -ComObject Excel.Application
    $excel.Visible = $false
    $excel.DisplayAlerts = $false
    $excel.ScreenUpdating = $false
    $excel.EnableEvents = $false
    $excel.AskToUpdateLinks = $false
    $excel.AutomationSecurity = 3

    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class NativeWindowProcess {
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
}
"@
    [uint32]$excelProcessId = 0
    [void][NativeWindowProcess]::GetWindowThreadProcessId(
        [IntPtr][int]$excel.Hwnd,
        [ref]$excelProcessId
    )
    [System.IO.File]::WriteAllText(
        $excelPidPath,
        [string]$excelProcessId,
        (New-Object System.Text.UTF8Encoding($false))
    )

    $workbooks = $excel.Workbooks
    $workbook = $workbooks.Open(
        $workbookPath,
        0,
        $true
    )
    $openCompletedAt = [DateTime]::UtcNow.ToString("o")
    $isReadOnly = [bool]$workbook.ReadOnly
    $checks.Add((New-Check `
        -Code "workbook_opened_without_blocking_prompt" `
        -Passed $true `
        -Message "Excel 在超时限制内以隐藏、禁止提示模式打开了工作簿。"))
    $checks.Add((New-Check `
        -Code "workbook_is_read_only" `
        -Passed $isReadOnly `
        -Message $(if ($isReadOnly) { "工作簿以只读方式打开。" } else { "工作簿没有以只读方式打开。" })))

    $worksheets = $workbook.Worksheets
    try {
        $worksheet = $worksheets.Item($worksheetName)
    }
    catch {
        $worksheet = $null
    }
    $worksheetExists = $null -ne $worksheet
    $checks.Add((New-Check `
        -Code "worksheet_exists" `
        -Passed $worksheetExists `
        -Message $(if ($worksheetExists) { "已找到工作表 [$worksheetName]。" } else { "未找到工作表 [$worksheetName]。" })))

    $nameCollection = $workbook.Names
    for ($nameIndex = 1; $nameIndex -le [int]$nameCollection.Count; $nameIndex++) {
        $nameObject = $null
        try {
            $nameObject = $nameCollection.Item($nameIndex)
            $rawName = [string]$nameObject.Name
            $localName = [string]$nameObject.NameLocal
            if (
                (Test-IsPrintAreaName -Name $rawName) -or
                (Test-IsPrintAreaName -Name $localName)
            ) {
                $printAreaNames.Add([pscustomobject][ordered]@{
                    name = $rawName
                    name_local = $localName
                    refers_to = [string]$nameObject.RefersTo
                    visible = [bool]$nameObject.Visible
                })
            }
        }
        finally {
            Release-ComObject -Value $nameObject
        }
    }

    if ($worksheetExists) {
        $pageSetup = $worksheet.PageSetup
        try {
            $pageSetupPrintArea = [string]$pageSetup.PrintArea
        }
        catch {
            $pageSetupPrintArea = $null
        }
    }
    $normalizedPrintArea = ConvertTo-NormalizedPrintArea -Value $pageSetupPrintArea
    $normalizedExpectedPrintArea = ConvertTo-NormalizedPrintArea -Value $expectedPrintArea
    $hasExactlyOnePrintAreaName = $printAreaNames.Count -eq 1
    $printAreaMatches = (
        -not [string]::IsNullOrWhiteSpace($normalizedPrintArea) -and
        $normalizedPrintArea -ceq $normalizedExpectedPrintArea
    )
    $printAreaIsValid = $hasExactlyOnePrintAreaName -and $printAreaMatches
    $checks.Add((New-Check `
        -Code "print_area_is_exact_and_conflict_free" `
        -Passed $printAreaIsValid `
        -Message $(if ($printAreaIsValid) { "Print_Area 唯一且打印区域符合预期。" } else { "Print_Area 必须恰好一个，且打印区域必须符合预期。" }) `
        -Details ([pscustomobject][ordered]@{
            name_count = $printAreaNames.Count
            page_setup_print_area = $pageSetupPrintArea
            normalized_actual = $normalizedPrintArea
            expected = $expectedPrintArea
            normalized_expected = $normalizedExpectedPrintArea
        })))

    if ($worksheetExists) {
        $canvasRange = $worksheet.Range($canvasAddress)
        $canvasResult = New-GeometryRectangle `
            -Left ([double]$canvasRange.Left) `
            -Top ([double]$canvasRange.Top) `
            -Width ([double]$canvasRange.Width) `
            -Height ([double]$canvasRange.Height)

        $shapeCollection = $worksheet.Shapes
        $rawImages = New-Object System.Collections.Generic.List[object]
        for ($shapeIndex = 1; $shapeIndex -le [int]$shapeCollection.Count; $shapeIndex++) {
            $shape = $null
            try {
                $shape = $shapeCollection.Item($shapeIndex)
                $shapeType = [int]$shape.Type
                if ($shapeType -notin @(11, 13, 28, 29)) {
                    continue
                }
                $rectangle = New-GeometryRectangle `
                    -Left ([double]$shape.Left) `
                    -Top ([double]$shape.Top) `
                    -Width ([double]$shape.Width) `
                    -Height ([double]$shape.Height)
                $rawImages.Add([pscustomobject][ordered]@{
                    name = [string]$shape.Name
                    shape_type = $shapeType
                    lock_aspect_ratio = [int]$shape.LockAspectRatio
                    geometry = $rectangle
                })
            }
            finally {
                Release-ComObject -Value $shape
            }
        }

        $sortedImages = @(
            $rawImages |
                Sort-Object `
                    @{ Expression = { [double]$_.geometry.top } }, `
                    @{ Expression = { [double]$_.geometry.left } }, `
                    @{ Expression = { [string]$_.name } }
        )
        for ($imageIndex = 0; $imageIndex -lt $sortedImages.Count; $imageIndex++) {
            $image = $sortedImages[$imageIndex]
            $withinBounds = Test-RectangleContained `
                -Inner $image.geometry `
                -Outer $canvasResult `
                -TolerancePoints $containmentTolerance
            $imageResults.Add([pscustomobject][ordered]@{
                index = $imageIndex + 1
                name = $image.name
                shape_type = $image.shape_type
                lock_aspect_ratio = $image.lock_aspect_ratio
                left = $image.geometry.left
                top = $image.geometry.top
                width = $image.geometry.width
                height = $image.geometry.height
                right = $image.geometry.right
                bottom = $image.geometry.bottom
                aspect_ratio = $image.geometry.aspect_ratio
                within_canvas = $withinBounds
                overlaps_with = @()
                expectation_checks = @()
            })
        }

        $hasAtLeastOneImage = $imageResults.Count -ge 1
        $checks.Add((New-Check `
            -Code "image_count_at_least_one" `
            -Passed $hasAtLeastOneImage `
            -Message $(if ($hasAtLeastOneImage) { "工作表至少包含一张图片。" } else { "工作表没有可验证的图片。" }) `
            -Details ([pscustomobject][ordered]@{
                actual = $imageResults.Count
                minimum = 1
            })))

        for ($firstIndex = 0; $firstIndex -lt $imageResults.Count; $firstIndex++) {
            for ($secondIndex = $firstIndex + 1; $secondIndex -lt $imageResults.Count; $secondIndex++) {
                $intersection = Get-RectangleIntersection `
                    -First $imageResults[$firstIndex] `
                    -Second $imageResults[$secondIndex] `
                    -TolerancePoints $overlapTolerance
                if ($intersection.overlaps) {
                    $firstOverlaps = @($imageResults[$firstIndex].overlaps_with)
                    $secondOverlaps = @($imageResults[$secondIndex].overlaps_with)
                    $imageResults[$firstIndex].overlaps_with = @(
                        $firstOverlaps + [pscustomobject][ordered]@{
                            index = $secondIndex + 1
                            width = $intersection.width
                            height = $intersection.height
                            area = $intersection.area
                        }
                    )
                    $imageResults[$secondIndex].overlaps_with = @(
                        $secondOverlaps + [pscustomobject][ordered]@{
                            index = $firstIndex + 1
                            width = $intersection.width
                            height = $intersection.height
                            area = $intersection.area
                        }
                    )
                }
            }
        }

        $allWithinBounds = @(
            $imageResults.ToArray() | Where-Object { -not $_.within_canvas }
        ).Count -eq 0
        $noOverlaps = @(
            $imageResults.ToArray() | Where-Object { $_.overlaps_with.Count -gt 0 }
        ).Count -eq 0
        $checks.Add((New-Check `
            -Code "all_images_within_canvas" `
            -Passed $allWithinBounds `
            -Message $(if ($allWithinBounds) { "所有图片均位于 $canvasAddress 边界内。" } else { "至少一张图片超出 $canvasAddress 边界。" })))
        $checks.Add((New-Check `
            -Code "images_do_not_overlap" `
            -Passed $noOverlaps `
            -Message $(if ($noOverlaps) { "图片之间没有重叠。" } else { "检测到图片重叠。" })))

        if (Test-HasProperty -Value $expectation -Name "image_count") {
            $expectedCount = [int]$expectation.image_count
            $matchesCount = $imageResults.Count -eq $expectedCount
            $checks.Add((New-Check `
                -Code "image_count_matches" `
                -Passed $matchesCount `
                -Message $(if ($matchesCount) { "图片数量符合预期：$expectedCount。" } else { "图片数量不符合预期：实际 $($imageResults.Count)，预期 $expectedCount。" }) `
                -Details ([pscustomobject][ordered]@{
                    actual = $imageResults.Count
                    expected = $expectedCount
                })))
        }

        if (Test-HasProperty -Value $expectation -Name "images") {
            foreach ($expectedImage in @($expectation.images)) {
                if ($null -eq $expectedImage) {
                    continue
                }
                $expectedIndex = [int]$expectedImage.index
                if ($expectedIndex -lt 1 -or $expectedIndex -gt $imageResults.Count) {
                    $checks.Add((New-Check `
                        -Code "image_expectation_$expectedIndex" `
                        -Passed $false `
                        -Message "期望清单中的第 $expectedIndex 张图片不存在。"))
                    continue
                }
                $actualImage = $imageResults[$expectedIndex - 1]
                $propertyChecks = New-Object System.Collections.Generic.List[object]
                foreach ($propertyName in @("left", "top", "width", "height", "aspect_ratio")) {
                    if (-not (Test-HasProperty -Value $expectedImage -Name $propertyName)) {
                        continue
                    }
                    $tolerance = if ($propertyName -in @("left", "top")) {
                        $positionTolerance
                    }
                    elseif ($propertyName -in @("width", "height")) {
                        $sizeTolerance
                    }
                    else {
                        $aspectRatioTolerance
                    }
                    $comparison = Test-NumericExpectation `
                        -Actual ([double]$actualImage.$propertyName) `
                        -Expected ([double]$expectedImage.$propertyName) `
                        -Tolerance $tolerance
                    $propertyChecks.Add([pscustomobject][ordered]@{
                        property = $propertyName
                        passed = $comparison.passed
                        actual = $comparison.actual
                        expected = $comparison.expected
                        difference = $comparison.difference
                        tolerance = $comparison.tolerance
                    })
                }
                $actualImage.expectation_checks = $propertyChecks.ToArray()
                $imageExpectationPassed = @(
                    $propertyChecks | Where-Object { -not $_.passed }
                ).Count -eq 0
                $checks.Add((New-Check `
                    -Code "image_expectation_$expectedIndex" `
                    -Passed $imageExpectationPassed `
                    -Message $(if ($imageExpectationPassed) { "第 $expectedIndex 张图片的几何信息符合预期。" } else { "第 $expectedIndex 张图片的几何信息不符合预期。" }) `
                    -Details $propertyChecks.ToArray()))
            }
        }
    }
}
catch {
    $failure = [pscustomobject][ordered]@{
        type = $_.Exception.GetType().FullName
        message = $_.Exception.Message
        hresult = $_.Exception.HResult
    }
}
finally {
    if ($null -ne $workbook) {
        try {
            $workbook.Close($false)
        }
        catch {
            $cleanupErrors.Add("关闭工作簿失败：$($_.Exception.Message)")
        }
    }
    if ($null -ne $excel) {
        try {
            $excel.Quit()
        }
        catch {
            $cleanupErrors.Add("退出 Excel 失败：$($_.Exception.Message)")
        }
    }

    Release-ComObject -Value $pageSetup
    Release-ComObject -Value $nameCollection
    Release-ComObject -Value $shapeCollection
    Release-ComObject -Value $canvasRange
    Release-ComObject -Value $worksheet
    Release-ComObject -Value $worksheets
    Release-ComObject -Value $workbook
    Release-ComObject -Value $workbooks
    Release-ComObject -Value $excel
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}

$afterSnapshot = Get-FileSnapshot -Path $workbookPath
$unchanged = Test-SnapshotEqual -Before $beforeSnapshot -After $afterSnapshot
$checks.Add((New-Check `
    -Code "workbook_unchanged" `
    -Passed $unchanged `
    -Message $(if ($unchanged) { "验证前后文件大小、时间戳和 SHA-256 均未变化。" } else { "验证前后文件发生变化。" }) `
    -Details ([pscustomobject][ordered]@{
        before = $beforeSnapshot
        after = $afterSnapshot
    })))

if ($null -ne $failure) {
    $checks.Add((New-Check `
        -Code "verification_completed" `
        -Passed $false `
        -Message "Excel 验证未完成：$($failure.message)"))
}
elseif ($cleanupErrors.Count -gt 0) {
    $checks.Add((New-Check `
        -Code "excel_cleanup_completed" `
        -Passed $false `
        -Message "Excel 清理过程出现错误。" `
        -Details $cleanupErrors.ToArray()))
}

$ok = $null -eq $failure -and $cleanupErrors.Count -eq 0 -and @(
    $checks | Where-Object { -not $_.passed }
).Count -eq 0

$result = [pscustomobject][ordered]@{
    schema_version = 1
    ok = $ok
    workbook_path = $workbookPath
    opened_read_only = $isReadOnly
    open_completed_at = $openCompletedAt
    worksheet_name = $worksheetName
    canvas_address = $canvasAddress
    canvas = $canvasResult
    print_area = [pscustomobject][ordered]@{
        page_setup_value = $pageSetupPrintArea
        expected = $expectedPrintArea
        matching_names = $printAreaNames.ToArray()
        conflict = $printAreaNames.Count -ne 1
        matches_expected = $printAreaMatches
    }
    image_count = $imageResults.Count
    images = $imageResults.ToArray()
    checks = $checks.ToArray()
    cleanup_errors = $cleanupErrors.ToArray()
    error = $failure
}

Write-ResultFile -Value $result -Path $outputPath
if ($ok) {
    exit 0
}
exit 1
