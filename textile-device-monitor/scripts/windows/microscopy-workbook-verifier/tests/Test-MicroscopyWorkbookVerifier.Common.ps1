Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Import-Module (
    Join-Path $PSScriptRoot "..\MicroscopyWorkbookVerifier.Common.psm1"
) -Force

function Assert-True {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$Condition,

        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if (-not $Condition) {
        throw "断言失败：$Message"
    }
}

function Assert-Equal {
    param(
        [AllowNull()]
        [object]$Actual,

        [AllowNull()]
        [object]$Expected,

        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if ($Actual -ne $Expected) {
        throw "断言失败：$Message；实际=$Actual，预期=$Expected"
    }
}

$canvas = New-GeometryRectangle -Left 0 -Top 0 -Width 612 -Height 331.5
$inside = New-GeometryRectangle -Left 0.5 -Top 0.5 -Width 611 -Height 330
$outside = New-GeometryRectangle -Left -3 -Top 0 -Width 612 -Height 331.5
Assert-True `
    -Condition (Test-RectangleContained -Inner $inside -Outer $canvas -TolerancePoints 1.5) `
    -Message "容差范围内的图片应被判定为位于画布内"
Assert-True `
    -Condition (-not (Test-RectangleContained -Inner $outside -Outer $canvas -TolerancePoints 1.5)) `
    -Message "明显越界的图片不应被判定为位于画布内"

$first = New-GeometryRectangle -Left 0 -Top 0 -Width 100 -Height 100
$touching = New-GeometryRectangle -Left 100 -Top 0 -Width 100 -Height 100
$overlapping = New-GeometryRectangle -Left 90 -Top 10 -Width 100 -Height 100
$touchResult = Get-RectangleIntersection -First $first -Second $touching
$overlapResult = Get-RectangleIntersection -First $first -Second $overlapping
Assert-True -Condition (-not $touchResult.overlaps) -Message "仅边缘接触不应判定为重叠"
Assert-True -Condition $overlapResult.overlaps -Message "有面积交叉应判定为重叠"
Assert-Equal -Actual $overlapResult.width -Expected 10 -Message "重叠宽度应准确"
Assert-Equal -Actual $overlapResult.height -Expected 90 -Message "重叠高度应准确"

Assert-Equal `
    -Actual (Get-PrintAreaLeafName -Name "'微观形貌'!_xlnm.Print_Area") `
    -Expected "Print_Area" `
    -Message "应规范化内建打印区域名称"
Assert-True `
    -Condition (Test-IsPrintAreaName -Name "微观形貌!Print_Area") `
    -Message "应识别英文 Print_Area 名称"
Assert-True `
    -Condition (Test-IsPrintAreaName -Name "微观形貌!打印区域") `
    -Message "应识别中文本地化打印区域名称"
Assert-True `
    -Condition (-not (Test-IsPrintAreaName -Name "微观形貌!普通名称")) `
    -Message "普通名称不应误判为打印区域"
Assert-Equal `
    -Actual (ConvertTo-NormalizedPrintArea -Value "='微观形貌'!`$A`$1:`$L`$37") `
    -Expected '$A$1:$L$37' `
    -Message "应规范化带工作表前缀的打印区域"

$numericPass = Test-NumericExpectation -Actual 1.3337 -Expected 1.3333 -Tolerance 0.01
$numericFail = Test-NumericExpectation -Actual 1.05 -Expected 1.3333 -Tolerance 0.02
Assert-True -Condition $numericPass.passed -Message "容差内宽高比应通过"
Assert-True -Condition (-not $numericFail.passed) -Message "近似方形图片不应通过 4:3 预期"

Write-Host "PASS Test-MicroscopyWorkbookVerifier.Common.ps1"
