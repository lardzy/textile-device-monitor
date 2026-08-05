Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function ConvertTo-RoundedNumber {
    param(
        [Parameter(Mandatory = $true)]
        [double]$Value,

        [int]$Digits = 4
    )

    return [Math]::Round($Value, $Digits)
}

function New-GeometryRectangle {
    param(
        [Parameter(Mandatory = $true)]
        [double]$Left,

        [Parameter(Mandatory = $true)]
        [double]$Top,

        [Parameter(Mandatory = $true)]
        [double]$Width,

        [Parameter(Mandatory = $true)]
        [double]$Height
    )

    if ($Width -lt 0 -or $Height -lt 0) {
        throw "矩形的宽度和高度不能为负数。"
    }

    $right = $Left + $Width
    $bottom = $Top + $Height
    $aspectRatio = $null
    if ($Height -gt 0) {
        $aspectRatio = ConvertTo-RoundedNumber -Value ($Width / $Height)
    }

    return [pscustomobject][ordered]@{
        left = ConvertTo-RoundedNumber -Value $Left
        top = ConvertTo-RoundedNumber -Value $Top
        width = ConvertTo-RoundedNumber -Value $Width
        height = ConvertTo-RoundedNumber -Value $Height
        right = ConvertTo-RoundedNumber -Value $right
        bottom = ConvertTo-RoundedNumber -Value $bottom
        aspect_ratio = $aspectRatio
    }
}

function Test-RectangleContained {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Inner,

        [Parameter(Mandatory = $true)]
        [object]$Outer,

        [double]$TolerancePoints = 1.5
    )

    return (
        [double]$Inner.left -ge ([double]$Outer.left - $TolerancePoints) -and
        [double]$Inner.top -ge ([double]$Outer.top - $TolerancePoints) -and
        [double]$Inner.right -le ([double]$Outer.right + $TolerancePoints) -and
        [double]$Inner.bottom -le ([double]$Outer.bottom + $TolerancePoints)
    )
}

function Get-RectangleIntersection {
    param(
        [Parameter(Mandatory = $true)]
        [object]$First,

        [Parameter(Mandatory = $true)]
        [object]$Second,

        [double]$TolerancePoints = 0.5
    )

    $width = [Math]::Max(
        0,
        [Math]::Min([double]$First.right, [double]$Second.right) -
            [Math]::Max([double]$First.left, [double]$Second.left)
    )
    $height = [Math]::Max(
        0,
        [Math]::Min([double]$First.bottom, [double]$Second.bottom) -
            [Math]::Max([double]$First.top, [double]$Second.top)
    )
    $overlaps = $width -gt $TolerancePoints -and $height -gt $TolerancePoints

    return [pscustomobject][ordered]@{
        overlaps = $overlaps
        width = ConvertTo-RoundedNumber -Value $width
        height = ConvertTo-RoundedNumber -Value $height
        area = ConvertTo-RoundedNumber -Value ($width * $height)
    }
}

function Get-PrintAreaLeafName {
    param(
        [AllowEmptyString()]
        [string]$Name
    )

    if ([string]::IsNullOrWhiteSpace($Name)) {
        return ""
    }

    $leaf = $Name.Trim()
    $separatorIndex = $leaf.LastIndexOf("!")
    if ($separatorIndex -ge 0) {
        $leaf = $leaf.Substring($separatorIndex + 1)
    }
    $leaf = $leaf.Trim("'", '"')
    $leaf = $leaf -replace "^_xlnm\.", ""
    return $leaf
}

function Test-IsPrintAreaName {
    param(
        [AllowEmptyString()]
        [string]$Name
    )

    $leaf = Get-PrintAreaLeafName -Name $Name
    return $leaf -match "^(?i:Print_Area|打印区域)$"
}

function ConvertTo-NormalizedPrintArea {
    param(
        [AllowEmptyString()]
        [string]$Value
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ""
    }
    $normalized = $Value.Trim().TrimStart("=")
    $separatorIndex = $normalized.LastIndexOf("!")
    if ($separatorIndex -ge 0) {
        $normalized = $normalized.Substring($separatorIndex + 1)
    }
    return ($normalized -replace "\s+", "").ToUpperInvariant()
}

function Test-NumericExpectation {
    param(
        [Parameter(Mandatory = $true)]
        [double]$Actual,

        [Parameter(Mandatory = $true)]
        [double]$Expected,

        [Parameter(Mandatory = $true)]
        [double]$Tolerance
    )

    $difference = [Math]::Abs($Actual - $Expected)
    return [pscustomobject][ordered]@{
        passed = $difference -le $Tolerance
        actual = ConvertTo-RoundedNumber -Value $Actual
        expected = ConvertTo-RoundedNumber -Value $Expected
        difference = ConvertTo-RoundedNumber -Value $difference
        tolerance = ConvertTo-RoundedNumber -Value $Tolerance
    }
}

Export-ModuleMember -Function @(
    "ConvertTo-RoundedNumber",
    "New-GeometryRectangle",
    "Test-RectangleContained",
    "Get-RectangleIntersection",
    "Get-PrintAreaLeafName",
    "Test-IsPrintAreaName",
    "ConvertTo-NormalizedPrintArea",
    "Test-NumericExpectation"
)
