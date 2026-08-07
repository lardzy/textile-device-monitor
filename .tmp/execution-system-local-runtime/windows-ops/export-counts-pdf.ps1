$ErrorActionPreference = 'Stop'
$ProbeDir = 'C:\Mac\Home\PycharmProjects\textile-device-monitor\.tmp\execution-system-local-runtime\geom-probe\counts'
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false
try {
    foreach ($file in (Get-ChildItem -Path $ProbeDir -Filter 'record-n*.xls' | Sort-Object Name)) {
        $workbook = $excel.Workbooks.Open($file.FullName, 0, $true)
        try {
            $pdf = [System.IO.Path]::ChangeExtension($file.FullName, '.pdf')
            $workbook.ExportAsFixedFormat(0, $pdf)
            Write-Output "exported $pdf"
        }
        finally {
            $workbook.Close($false)
        }
    }
}
finally {
    $excel.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($excel) | Out-Null
}
