$ErrorActionPreference = 'Stop'
$ProbeDir = 'C:\Mac\Home\Downloads\geom-probe\counts'
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false

try {
    $all = @()
    foreach ($file in (Get-ChildItem -Path $ProbeDir -Filter 'record-n*.xls' | Sort-Object Name)) {
        $workbook = $excel.Workbooks.Open($file.FullName, 0, $true)
        try {
            $sheet = $workbook.Worksheets.Item([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('5b6u6KeC5b2i6LKM')))
            $a4 = $sheet.Range('A4')
            $info = [ordered]@{
                file = $file.Name
                A4Left = $a4.Left
                A4Top = $a4.Top
                Shapes = @()
            }
            foreach ($shape in $sheet.Shapes) {
                $info.Shapes += [ordered]@{
                    Name = $shape.Name
                    Left = $shape.Left; Top = $shape.Top
                    Width = $shape.Width; Height = $shape.Height
                }
            }
            $all += $info
        }
        finally {
            $workbook.Close($false)
        }
    }
    $all | ConvertTo-Json -Depth 5 -Compress
}
finally {
    $excel.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($excel) | Out-Null
}
