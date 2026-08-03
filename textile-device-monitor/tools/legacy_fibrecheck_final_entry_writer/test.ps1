param(
    [string]$FibreCheckDir = "$PSScriptRoot\..\..\..\.tmp\FibreCheck",
    [string]$Odac32Dir = "$PSScriptRoot\..\..\..\.tmp\odac32"
)

$ErrorActionPreference = 'Stop'
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) `
    ('fibrecheck-final-entry-tests-' + [Guid]::NewGuid().ToString('N'))
$outDir = Join-Path $testRoot 'out'
$fixtureDir = Join-Path $testRoot 'fixtures'
$sourceDir = Join-Path $testRoot 'source'
$previousPassword = $env:FIBRECHECK_RUNNER_PASSWORD
$sentinel = 'offline-password-sentinel-must-not-appear'

function Write-Utf8NoBom([string]$Path, [string]$Value) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Value, $encoding)
}

function Invoke-Case([string[]]$Arguments) {
    $output = @(& $script:exe @Arguments 2>&1 | ForEach-Object { $_.ToString() })
    return [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Text = ($output -join "`n")
    }
}

function Assert-Case(
    [string]$Name,
    [string[]]$Arguments,
    [int]$ExpectedExit,
    [string[]]$ExpectedText
) {
    $result = Invoke-Case $Arguments
    if ($result.ExitCode -ne $ExpectedExit) {
        throw "$Name exit code $($result.ExitCode), expected $ExpectedExit`n$($result.Text)"
    }
    foreach ($value in $ExpectedText) {
        if ($result.Text -notmatch [Regex]::Escape($value)) {
            throw "$Name missing output marker: $value`n$($result.Text)"
        }
    }
    if ($result.Text.Contains($sentinel) -or $result.Text.Contains($testRoot)) {
        throw "$Name leaked a password or local absolute path"
    }
    $script:passed++
}

try {
    New-Item -ItemType Directory -Force -Path $outDir, $fixtureDir, $sourceDir | Out-Null
    & (Join-Path $PSScriptRoot 'build.ps1') `
        -FibreCheckDir $FibreCheckDir -Odac32Dir $Odac32Dir -OutDir $outDir
    $exe = Join-Path $outDir 'FibreCheckFinalEntryWriter.exe'
    if (-not (Test-Path -LiteralPath $exe)) {
        throw "Build output not found: $exe"
    }
    $env:FIBRECHECK_RUNNER_PASSWORD = $sentinel
    $passed = 0

    $generic = @'
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
      {"standard_location":"", "standard_value":"", "real_location":"part-a", "real_value":"60-80"},
      {"standard_location":"", "standard_value":"", "real_location":"part-b", "real_value":"20-40"}
    ]
  }
}
'@
    $genericPath = Join-Path $fixtureDir 'generic.json'
    Write-Utf8NoBom $genericPath $generic
    Assert-Case 'valid generic' @('--offline-validate', '--package', $genericPath) 0 `
        @('package_validated', 'generic_details_validated', 'offline_validation_completed')

    $unknownPath = Join-Path $fixtureDir 'generic-unknown.json'
    Write-Utf8NoBom $unknownPath ($generic.Replace('"schema_version": 1,', '"schema_version": 1, "unexpected": true,'))
    Assert-Case 'unknown field' @('--offline-validate', '--package', $unknownPath) 21 `
        @('unknown_field_unexpected')

    $quotePath = Join-Path $fixtureDir 'generic-quote.json'
    Write-Utf8NoBom $quotePath ($generic.Replace('"unit": "%"', '"unit": "x''y"'))
    Assert-Case 'legacy quote rejection' @('--offline-validate', '--package', $quotePath) 21 `
        @('unsafe_or_invalid_string_unit')

    $payloadDir = Join-Path $sourceDir 'payload'
    New-Item -ItemType Directory -Force -Path $payloadDir | Out-Null
    $workbook = Join-Path $payloadDir 'fake.xls'
    [System.IO.File]::WriteAllBytes($workbook, [byte[]](0, 1, 2, 3, 4, 255))
    $sha = (Get-FileHash -LiteralPath $workbook -Algorithm SHA256).Hash.ToLowerInvariant()
    $size = (Get-Item -LiteralPath $workbook).Length
    $excel = @"
{
  "schema_version": 1,
  "operation_type": "excel_check_record",
  "sample_number": "26A045793",
  "check_item_no": "5103.5",
  "check_item_name": "\u7ea4\u7ef4\u5fae\u89c2\u5f62\u8c8c",
  "expected_existing_register_count": 0,
  "excel_record": {
    "template_name": "\u5fae\u89c2\u5f62\u8c8c.xls",
    "collection_mode": "standard",
    "expected_mapping_config_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "key_result_count": 1,
    "expected_key_identities": ["\u7eb5\u9762"],
    "register": {"level":"", "sample_identity":"", "equipment_no":"", "check_basis":""},
    "workbook": {
      "relative_path": "payload\\fake.xls", "filename": "fake.xls",
      "size_bytes": $size, "content_sha256": "$sha"
    }
  }
}
"@
    $excelPath = Join-Path $fixtureDir 'excel.json'
    Write-Utf8NoBom $excelPath $excel
    Assert-Case 'valid excel manifest' `
        @('--offline-validate', '--package', $excelPath, '--source-root', $sourceDir) 0 `
        @('workbook_verified', 'offline_validation_completed')

    $gapPath = Join-Path $fixtureDir 'excel-gap.json'
    Write-Utf8NoBom $gapPath ($excel.Replace('"collection_mode": "standard"', '"collection_mode": "gap"'))
    Assert-Case 'unsupported excel mode' `
        @('--offline-validate', '--package', $gapPath, '--source-root', $sourceDir) 21 `
        @('collection_mode_not_supported_in_v1')

    $identityCountPath = Join-Path $fixtureDir 'excel-identity-count.json'
    Write-Utf8NoBom $identityCountPath ($excel.Replace(
        '"expected_key_identities": ["\u7eb5\u9762"]',
        '"expected_key_identities": []'))
    Assert-Case 'excel identity count' `
        @('--offline-validate', '--package', $identityCountPath, '--source-root', $sourceDir) 21 `
        @('expected_key_identities_count_mismatch')

    $identityScopePath = Join-Path $fixtureDir 'excel-identity-scope.json'
    Write-Utf8NoBom $identityScopePath ($excel.Replace(
        '"expected_key_identities": ["\u7eb5\u9762"]',
        '"expected_key_identities": ["unsupported-side"]'))
    Assert-Case 'excel identity scope' `
        @('--offline-validate', '--package', $identityScopePath, '--source-root', $sourceDir) 21 `
        @('excel_scope_not_supported_in_v1')

    $projectScopePath = Join-Path $fixtureDir 'excel-project-scope.json'
    Write-Utf8NoBom $projectScopePath ($excel.Replace('"5103.5"', '"9999"'))
    Assert-Case 'excel project scope' `
        @('--offline-validate', '--package', $projectScopePath, '--source-root', $sourceDir) 21 `
        @('excel_project_not_supported_in_v1')

    $badHashPath = Join-Path $fixtureDir 'excel-bad-hash.json'
    Write-Utf8NoBom $badHashPath ($excel.Replace($sha, ('b' * 64)))
    Assert-Case 'excel hash mismatch' `
        @('--offline-validate', '--package', $badHashPath, '--source-root', $sourceDir) 21 `
        @('workbook_sha256_mismatch')

    $traversalPath = Join-Path $fixtureDir 'excel-traversal.json'
    Write-Utf8NoBom $traversalPath ($excel.Replace('payload\\fake.xls', '..\\fake.xls'))
    Assert-Case 'excel traversal' `
        @('--offline-validate', '--package', $traversalPath, '--source-root', $sourceDir) 21 `
        @('workbook_outside_source_root_or_missing')

    Assert-Case 'offline execute rejection' `
        @('--offline-validate', '--execute', '--side-effect-permit-stdin', '--package', $genericPath) 21 `
        @('offline_validate_cannot_execute')
    Assert-Case 'execute missing permit' @('--execute', '--package', $genericPath) 21 `
        @('execute_requires_side_effect_permit_stdin')

    Write-Host "Offline tests passed: $passed"
}
finally {
    $env:FIBRECHECK_RUNNER_PASSWORD = $previousPassword
    $resolvedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    $resolvedTest = [System.IO.Path]::GetFullPath($testRoot)
    if ($resolvedTest.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase) `
        -and (Split-Path -Leaf $resolvedTest).StartsWith('fibrecheck-final-entry-tests-')) {
        Remove-Item -LiteralPath $resolvedTest -Recurse -Force -ErrorAction SilentlyContinue
    }
}
