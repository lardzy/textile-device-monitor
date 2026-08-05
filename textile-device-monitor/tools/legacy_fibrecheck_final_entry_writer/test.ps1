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
$previousControlledTarget = $env:FIBRECHECK_CONTROLLED_TEST_SAMPLE_NO
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

    $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
    $branchSelfTest = Join-Path $outDir 'LegacyExcelBranchRulesSelfTest.exe'
    & $csc -nologo -target:exe -codepage:65001 -utf8output -debug- -optimize+ `
        -out:"$branchSelfTest" `
        (Join-Path $PSScriptRoot 'src\LegacyExcelBranchRules.cs') `
        (Join-Path $PSScriptRoot 'tests\LegacyExcelBranchRulesSelfTest.cs')
    if ($LASTEXITCODE -ne 0) {
        throw "Legacy Excel branch rules SelfTest compilation failed: $LASTEXITCODE"
    }
    $branchOutput = @(& $branchSelfTest 2>&1 | ForEach-Object { $_.ToString() })
    $branchExitCode = $LASTEXITCODE
    if ($branchExitCode -ne 0 `
        -or ($branchOutput -join "`n") -notmatch 'Legacy Excel branch rules self-test: 6 passed') {
        throw "Legacy Excel branch rules SelfTest failed: $branchExitCode`n$($branchOutput -join "`n")"
    }
    $passed++

    $sessionSelfTest = Join-Path $outDir 'ExcelInteractiveSessionRulesSelfTest.exe'
    & $csc -nologo -target:exe -codepage:65001 -utf8output -debug- -optimize+ `
        -out:"$sessionSelfTest" `
        (Join-Path $PSScriptRoot 'src\ExcelInteractiveSessionRules.cs') `
        (Join-Path $PSScriptRoot 'tests\ExcelInteractiveSessionRulesSelfTest.cs')
    if ($LASTEXITCODE -ne 0) {
        throw "Excel interactive session rules SelfTest compilation failed: $LASTEXITCODE"
    }
    $sessionOutput = @(& $sessionSelfTest 2>&1 | ForEach-Object { $_.ToString() })
    $sessionExitCode = $LASTEXITCODE
    if ($sessionExitCode -ne 0 `
        -or ($sessionOutput -join "`n") -notmatch 'Excel interactive session rules self-test: 6 passed') {
        throw "Excel interactive session rules SelfTest failed: $sessionExitCode`n$($sessionOutput -join "`n")"
    }
    $passed++

    $authoritySelfTest = Join-Path $outDir 'CollectedRegisterFieldAuthoritySelfTest.exe'
    & $csc -nologo -target:exe -codepage:65001 -utf8output -debug- -optimize+ `
        -out:"$authoritySelfTest" `
        (Join-Path $PSScriptRoot 'src\CollectedRegisterFieldAuthority.cs') `
        (Join-Path $PSScriptRoot 'tests\CollectedRegisterFieldAuthoritySelfTest.cs')
    if ($LASTEXITCODE -ne 0) {
        throw "Collected register field authority SelfTest compilation failed: $LASTEXITCODE"
    }
    $authorityOutput = @(& $authoritySelfTest 2>&1 | ForEach-Object { $_.ToString() })
    $authorityExitCode = $LASTEXITCODE
    if ($authorityExitCode -ne 0 `
        -or ($authorityOutput -join "`n") -notmatch 'Collected register field authority self-test: 15 passed') {
        throw "Collected register field authority SelfTest failed: $authorityExitCode`n$($authorityOutput -join "`n")"
    }
    $passed++

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

    $paperGeneric = @'
{
  "schema_version": 2,
  "operation_type": "generic_item_record",
  "sample_number": "26W006701",
  "check_item_no": "51.113K",
  "check_item_name": "纸、纸板和纸浆纤维鉴别分析",
  "expected_existing_register_count": 0,
  "task_project": {
    "project_key": "task-project:f7dd1283727651526a49e86c",
    "task_check_item_id": "sha256:868c0194ae593080",
    "check_item_id": "sha256:813b8c0ba468c8eb",
    "check_item_no": "51.113K",
    "check_item_name": "纸、纸板和纸浆纤维鉴别分析",
    "check_method": "GB/T 4688-2020",
    "seq_num": 17,
    "check_count": 1
  },
  "generic_record": {
    "header": {
      "grade": "", "unit": "", "judge_basis": "",
      "test_method": "GB/T 4688-2020",
      "sample_description": "", "standard_type": "",
      "report_check_item_name": "", "attach_info": "",
      "remark": "", "total_judge": ""
    },
    "details": [
      {
        "standard_location": "", "standard_value": "",
        "real_location": "", "real_value": "草浆、木浆、竹浆"
      }
    ]
  }
}
'@
    $paperGenericPath = Join-Path $fixtureDir 'paper-generic.json'
    Write-Utf8NoBom $paperGenericPath $paperGeneric
    Assert-Case 'valid paper generic without percent unit' `
        @('--offline-validate', '--package', $paperGenericPath) 0 `
        @('package_validated', 'generic_details_validated', 'offline_validation_completed')

    $paperHundredPath = Join-Path $fixtureDir 'paper-generic-hundred.json'
    Write-Utf8NoBom $paperHundredPath ($paperGeneric.Replace(
        '"sample_number": "26W006701"',
        '"sample_number": "26W006687"').Replace(
        '"unit": ""',
        '"unit": "%"').Replace(
        '"real_value": "草浆、木浆、竹浆"',
        '"real_value": "木浆 100"'))
    Assert-Case 'valid paper generic with percent unit' `
        @('--offline-validate', '--package', $paperHundredPath) 0 `
        @('package_validated', 'generic_details_validated', 'offline_validation_completed')

    $paperHundredDecimalPath = Join-Path $fixtureDir 'paper-generic-hundred-decimal.json'
    Write-Utf8NoBom $paperHundredDecimalPath (
        (Get-Content -Raw -LiteralPath $paperHundredPath).Replace(
            '"real_value": "木浆 100"',
            '"real_value": "木浆 100.0"'))
    Assert-Case 'paper generic 100.0 uses percent unit' `
        @('--offline-validate', '--package', $paperHundredDecimalPath) 0 `
        @('package_validated', 'generic_details_validated', 'offline_validation_completed')

    foreach ($nonHundredValue in @('棉100，粘纤0', '木浆 100.5', '木浆 1000')) {
        $slug = $nonHundredValue.Replace(' ', '-').Replace('.', '-')
        $path = Join-Path $fixtureDir ("paper-generic-$slug.json")
        Write-Utf8NoBom $path ($paperGeneric.Replace(
            '"real_value": "草浆、木浆、竹浆"',
            ('"real_value": "' + $nonHundredValue + '"')))
        Assert-Case ("paper generic $nonHundredValue keeps empty unit") `
            @('--offline-validate', '--package', $path) 0 `
            @('package_validated', 'generic_details_validated', 'offline_validation_completed')
    }

    $paperUnitMismatchPath = Join-Path $fixtureDir 'paper-generic-unit-mismatch.json'
    Write-Utf8NoBom $paperUnitMismatchPath ($paperGeneric.Replace(
        '"unit": ""',
        '"unit": "%"'))
    Assert-Case 'paper generic percent unit requires standalone 100' `
        @('--offline-validate', '--package', $paperUnitMismatchPath) 21 `
        @('paper_generic_record_contract_invalid')

    $paperMethodMismatchPath = Join-Path $fixtureDir 'paper-generic-method-mismatch.json'
    Write-Utf8NoBom $paperMethodMismatchPath ($paperGeneric.Replace(
        'GB/T 4688-2020',
        'GB/T 4688-2020/XG1-2026'))
    Assert-Case 'paper generic task method is exact' `
        @('--offline-validate', '--package', $paperMethodMismatchPath) 21 `
        @('task_project_binding_invalid')

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

    $v2TemplateMappings = [ordered]@{
        "\u5fae\u89c2\u5f62\u8c8c.xls" = 'a09399783171826d10b239bd01cb596569428bbc34a8c4636077e98f34dc690e'
        "\u7ea4\u7ef4\u5fae\u89c2\u5f62\u8c8c-GB T 36422-2018-2\u5f20\u56fe.xls" = '2ff546b96da9ac423613374ee28955bf7dfe3e62e5d40d8ad979c93636836f23'
        "\u7ea4\u7ef4\u5fae\u89c2\u5f62\u8c8c-GB T 36422-2018-3\u5f20\u56fe.xls" = '43ae3872f231c2499b98976ea63827162b4fddf7151e3e8daceee8a7591d4268'
        "\u7ea4\u7ef4\u5fae\u89c2\u5f62\u8c8c-GB T 36422-2018-5\u5f20\u56fe.xls" = 'd21e82cd1672ada28beab35673e1d679bf3ffa1099969f02dbed466645dc9b6d'
        "\u7ea4\u7ef4\u5fae\u89c2\u5f62\u8c8c-GB T 36422-2018-6\u5f20\u56fe.xls" = '5f56deb633c0dd2b2dc046ba70ab012c2780dbbae9039663b4d40903804a6304'
        "\u7ea4\u7ef4\u5fae\u89c2\u5f62\u8c8c-GB T 36422-2018-7\u5f20\u56fe.xls" = '3aea5aa68bccb1a8e9035be8762d305bb2f0d6e4104ad29557082a3b1abada01'
        "\u7ea4\u7ef4\u5fae\u89c2\u5f62\u8c8c-GB T 36422-2018-10\u5f20\u56fe.xls" = '976a88ed86af2a3fb30df5aa830e0529ea35e15e1e2fa59244e3035578940f74'
    }
    $v2Base = $excel.Replace('"schema_version": 1', '"schema_version": 2').Replace(
        '"expected_existing_register_count": 0,',
        @'
"expected_existing_register_count": 0,
  "task_project": {
    "project_key": "task-project:0dddba88e0b93ac2a58ace0a",
    "task_check_item_id": "sha256:1111111111111111",
    "check_item_id": "sha256:2222222222222222",
    "check_item_no": "5103.5",
    "check_item_name": "\u7ea4\u7ef4\u5fae\u89c2\u5f62\u8c8c",
    "check_method": "GB/T 36422-2018",
    "seq_num": 7,
    "check_count": 1
  },
'@)
    $v2Index = 0
    foreach ($entry in $v2TemplateMappings.GetEnumerator()) {
        $v2Index++
        $v2 = $v2Base.Replace(
            '"template_name": "\u5fae\u89c2\u5f62\u8c8c.xls"',
            ('"template_name": "' + $entry.Key + '"')).Replace(
            '"expected_mapping_config_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"',
            ('"expected_mapping_config_sha256": "' + $entry.Value + '"'))
        $v2Path = Join-Path $fixtureDir ("excel-v2-template-$v2Index.json")
        Write-Utf8NoBom $v2Path $v2
        Assert-Case ("v2 exact template $v2Index") `
            @('--offline-validate', '--package', $v2Path, '--source-root', $sourceDir) 0 `
            @('"schema_version": 2', 'offline_validation_completed')
    }

    $v2EmptyIdentity = $v2Base.Replace(
        '"expected_mapping_config_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"',
        '"expected_mapping_config_sha256": "a09399783171826d10b239bd01cb596569428bbc34a8c4636077e98f34dc690e"').Replace(
        '"expected_key_identities": ["\u7eb5\u9762"]',
        '"expected_key_identities": [""]')
    $v2EmptyIdentityPath = Join-Path $fixtureDir 'excel-v2-empty-identity.json'
    Write-Utf8NoBom $v2EmptyIdentityPath $v2EmptyIdentity
    Assert-Case 'v2 empty key identity' `
        @('--offline-validate', '--package', $v2EmptyIdentityPath, '--source-root', $sourceDir) 0 `
        @('workbook_verified', 'offline_validation_completed')

    $v2ChangedMethodPath = Join-Path $fixtureDir 'excel-v2-project-method-changed.json'
    Write-Utf8NoBom $v2ChangedMethodPath ($v2EmptyIdentity.Replace(
        'GB/T 36422-2018', 'GB/T 36422-2018/XG1-2024'))
    Assert-Case 'v2 project method changed without matching project key' `
        @('--offline-validate', '--package', $v2ChangedMethodPath, '--source-root', $sourceDir) 21 `
        @('task_project_binding_invalid')

    $v2ChangedTaskIdPath = Join-Path $fixtureDir 'excel-v2-project-id-changed.json'
    Write-Utf8NoBom $v2ChangedTaskIdPath ($v2EmptyIdentity.Replace(
        'sha256:1111111111111111', 'sha256:9999999999999999'))
    Assert-Case 'v2 project id changed without matching project key' `
        @('--offline-validate', '--package', $v2ChangedTaskIdPath, '--source-root', $sourceDir) 21 `
        @('task_project_binding_invalid')

    $v2ChangedSeqPath = Join-Path $fixtureDir 'excel-v2-project-seq-changed.json'
    Write-Utf8NoBom $v2ChangedSeqPath ($v2EmptyIdentity.Replace(
        '"seq_num": 7', '"seq_num": 8'))
    Assert-Case 'v2 project seq changed without matching project key' `
        @('--offline-validate', '--package', $v2ChangedSeqPath, '--source-root', $sourceDir) 21 `
        @('task_project_binding_invalid')

    $v2ChangedCountPath = Join-Path $fixtureDir 'excel-v2-project-count-changed.json'
    Write-Utf8NoBom $v2ChangedCountPath ($v2EmptyIdentity.Replace(
        '"check_count": 1', '"check_count": 2'))
    Assert-Case 'v2 project count must be one' `
        @('--offline-validate', '--package', $v2ChangedCountPath, '--source-root', $sourceDir) 21 `
        @('task_project_binding_invalid')

    $v2WrongMappingPath = Join-Path $fixtureDir 'excel-v2-wrong-mapping.json'
    Write-Utf8NoBom $v2WrongMappingPath $v2Base
    Assert-Case 'v2 template mapping pair mismatch' `
        @('--offline-validate', '--package', $v2WrongMappingPath, '--source-root', $sourceDir) 21 `
        @('excel_scope_not_supported_in_v2')

    $override = $v2EmptyIdentity.Replace(
        '"expected_existing_register_count": 0,',
        @'
"expected_existing_register_count": 1,
  "controlled_test_override": {
    "kind": "append_one_when_check_count_one",
    "target_sample_number": "260111037",
    "expected_task_check_count": 1,
    "expected_existing_register_count": 1,
    "resulting_register_count": 2,
    "reason": "受控追加登记离线验证"
  },
'@).Replace('"sample_number": "26A045793"', '"sample_number": "260111037"')
    $overridePath = Join-Path $fixtureDir 'excel-v2-controlled-override.json'
    Write-Utf8NoBom $overridePath $override
    $env:FIBRECHECK_CONTROLLED_TEST_SAMPLE_NO = $null
    Assert-Case 'controlled override requires cli flag' `
        @('--offline-validate', '--package', $overridePath, '--source-root', $sourceDir) 21 `
        @('controlled_test_override_cli_flag_required')
    Assert-Case 'controlled override requires environment target' `
        @('--offline-validate', '--allow-controlled-test-override', '--package', $overridePath,
          '--source-root', $sourceDir) 21 `
        @('controlled_test_override_environment_target_mismatch')
    $env:FIBRECHECK_CONTROLLED_TEST_SAMPLE_NO = '260111038'
    Assert-Case 'controlled override rejects wrong environment target' `
        @('--offline-validate', '--allow-controlled-test-override', '--package', $overridePath,
          '--source-root', $sourceDir) 21 `
        @('controlled_test_override_environment_target_mismatch')
    $env:FIBRECHECK_CONTROLLED_TEST_SAMPLE_NO = '260111037'
    Assert-Case 'controlled override triple match' `
        @('--offline-validate', '--allow-controlled-test-override', '--package', $overridePath,
          '--source-root', $sourceDir) 0 `
        @('"controlled_test_override"', '"active": true', '"applied": false')
    Assert-Case 'v1 cannot inherit controlled override cli flag' `
        @('--offline-validate', '--allow-controlled-test-override', '--package', $excelPath,
          '--source-root', $sourceDir) 21 `
        @('controlled_test_override_package_required')

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
    $env:FIBRECHECK_CONTROLLED_TEST_SAMPLE_NO = $previousControlledTarget
    $resolvedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    $resolvedTest = [System.IO.Path]::GetFullPath($testRoot)
    if ($resolvedTest.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase) `
        -and (Split-Path -Leaf $resolvedTest).StartsWith('fibrecheck-final-entry-tests-')) {
        Remove-Item -LiteralPath $resolvedTest -Recurse -Force -ErrorAction SilentlyContinue
    }
}
