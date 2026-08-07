$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$exe = 'C:\Users\lishuyang\Downloads\textile-device-monitor-cdde9ec\textile-device-monitor\tools\legacy_fibrecheck_final_entry_writer\out\FibreCheckFinalEntryWriter.exe'
$dir = Join-Path $env:TEMP 'final-entry-json-probe'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$enc = New-Object System.Text.UTF8Encoding($false)
$base = @'
{
  "schema_version": 1,
  "operation_type": "generic_item_record",
  "sample_number": "260039770",
  "check_item_no": "51.SSJ1",
  "check_item_name": "generic-item",
  "expected_existing_register_count": 0,
  "generic_record": {
    "header": {
      "grade": "", "unit": "%", "judge_basis": "",
      "test_method": "TEST-METHOD",
      "sample_description": "", "standard_type": "",
      "report_check_item_name": "", "attach_info": "",
      "remark": "offline fixture", "total_judge": ""
    },
    "details": [
      {"standard_location":"", "standard_value":"", "real_location":"part-a", "real_value":"60-80"}
    ]
  }
}
'@
$ascii = Join-Path $dir 'ascii.json'
[IO.File]::WriteAllText($ascii, $base, $enc)
$chinese = Join-Path $dir 'chinese.json'
[IO.File]::WriteAllText($chinese, $base.Replace('generic-item', '纸类项目').Replace('60-80', '木浆 100'), $enc)
foreach ($path in @($ascii, $chinese)) {
    Write-Output ("case=" + [IO.Path]::GetFileName($path))
    & $exe --offline-validate --package $path
    Write-Output ("exit=" + $LASTEXITCODE)
}
