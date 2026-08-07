$ErrorActionPreference = 'Stop'
$ProbeDir = 'C:\Mac\Home\PycharmProjects\textile-device-monitor\.tmp\execution-system-local-runtime\geom-probe'
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false

function Get-Geometry($path, $label) {
    $workbook = $excel.Workbooks.Open($path, 0, $true)
    try {
        $sheet = $workbook.Worksheets.Item([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('5b6u6KeC5b2i6LKM')))
        $info = [ordered]@{ label = $label }
        $a4 = $sheet.Range('A4')
        $g4 = $sheet.Range('G4')
        $info.A4 = [ordered]@{ Left = $a4.Left; Top = $a4.Top; MergeCells = $a4.MergeCells; MergeArea = $a4.MergeArea.Address($false, $false) }
        $info.G4 = [ordered]@{ Left = $g4.Left; Top = $g4.Top; MergeCells = $g4.MergeCells; MergeArea = $g4.MergeArea.Address($false, $false) }
        $g4Area = $g4.MergeArea
        $info.G4Right = $g4Area.Left + $g4Area.Width
        $cols = @()
        foreach ($letter in @('A','B','C','D','E','F','G','H','I','J','K','L','M')) {
            $r = $sheet.Range($letter + '4')
            $cols += [ordered]@{ Col = $letter; Left = $r.Left; Width = $r.Width }
        }
        $info.Columns = $cols
        $rows = @()
        foreach ($n in @(3, 4, 31, 32, 33)) {
            $r = $sheet.Range('A' + $n)
            $rows += [ordered]@{ Row = $n; Top = $r.Top; Height = $r.Height }
        }
        $info.Rows = $rows
        $info.PrintArea = ''
        try { $info.PrintArea = $sheet.PageSetup.PrintArea } catch {}
        $shapes = @()
        foreach ($shape in $sheet.Shapes) {
            $shapes += [ordered]@{
                Name = $shape.Name
                Left = $shape.Left; Top = $shape.Top
                Width = $shape.Width; Height = $shape.Height
            }
        }
        $info.Shapes = $shapes
        return $info
    }
    finally {
        $workbook.Close($false)
    }
}

try {
    $all = @()
    $all += Get-Geometry (Join-Path $ProbeDir 'template.xls') 'template'
    $all += Get-Geometry (Join-Path $ProbeDir 'old-dejavu.xls') 'old-dejavu'
    $all += Get-Geometry (Join-Path $ProbeDir 'new-simsun.xls') 'new-simsun'
    $all | ConvertTo-Json -Depth 6 -Compress
}
finally {
    $excel.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($excel) | Out-Null
}
