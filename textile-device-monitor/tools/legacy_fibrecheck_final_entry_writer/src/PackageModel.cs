using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;

namespace LegacyFibreCheckFinalEntryWriter
{
    internal sealed class PackageValidationException : Exception
    {
        public string Code { get; private set; }

        public PackageValidationException(string code)
            : base(code)
        {
            Code = code;
        }
    }

    internal sealed class FinalEntryPackage
    {
        internal const string GenericOperation = "generic_item_record";
        internal const string ExcelOperation = "excel_check_record";
        internal const string ControlledTestOverrideEnvironment =
            "FIBRECHECK_CONTROLLED_TEST_SAMPLE_NO";
        internal const string ControlledTestOverrideKind =
            "append_one_when_check_count_one";
        internal const string PaperCheckItemNo = "51.113K";
        internal const string PaperCheckItemName =
            "纸、纸板和纸浆纤维鉴别分析";
        internal const string PaperCheckMethod = "GB/T 4688-2020";

        internal static readonly Dictionary<string, string> SupportedMicroscopyTemplates =
            new Dictionary<string, string>(StringComparer.Ordinal)
            {
                {
                    "微观形貌.xls",
                    "a09399783171826d10b239bd01cb596569428bbc34a8c4636077e98f34dc690e"
                },
                {
                    "纤维微观形貌-GB T 36422-2018-2张图.xls",
                    "2ff546b96da9ac423613374ee28955bf7dfe3e62e5d40d8ad979c93636836f23"
                },
                {
                    "纤维微观形貌-GB T 36422-2018-3张图.xls",
                    "43ae3872f231c2499b98976ea63827162b4fddf7151e3e8daceee8a7591d4268"
                },
                {
                    "纤维微观形貌-GB T 36422-2018-5张图.xls",
                    "d21e82cd1672ada28beab35673e1d679bf3ffa1099969f02dbed466645dc9b6d"
                },
                {
                    "纤维微观形貌-GB T 36422-2018-6张图.xls",
                    "5f56deb633c0dd2b2dc046ba70ab012c2780dbbae9039663b4d40903804a6304"
                },
                {
                    "纤维微观形貌-GB T 36422-2018-7张图.xls",
                    "3aea5aa68bccb1a8e9035be8762d305bb2f0d6e4104ad29557082a3b1abada01"
                },
                {
                    "纤维微观形貌-GB T 36422-2018-10张图.xls",
                    "976a88ed86af2a3fb30df5aa830e0529ea35e15e1e2fa59244e3035578940f74"
                },
            };

        private static readonly Regex SampleNoPattern =
            new Regex(@"^[0-9A-Z]{9,20}(?:-[0-9A-Z]{1,8})?$", RegexOptions.CultureInvariant);
        private static readonly Regex ShaPattern =
            new Regex(@"^[0-9a-fA-F]{64}$", RegexOptions.CultureInvariant);
        private static readonly Regex ProjectKeyPattern =
            new Regex(@"^task-project:[0-9a-f]{24}$", RegexOptions.CultureInvariant);
        private static readonly Regex RedactedIdPattern =
            new Regex(@"^sha256:[0-9a-f]{16}$", RegexOptions.CultureInvariant);

        public int SchemaVersion;
        public string OperationType;
        public string SampleNumber;
        public string CheckItemNo;
        public string CheckItemName;
        public TaskProjectPayload TaskProject;
        // This is populated only from a successful read-only Oracle rebind.  It
        // is safe to return because both legacy IDs are one-way hashes.
        public TaskProjectPayload MeasuredTaskProject;
        public int ExpectedExistingRegisterCount;
        public GenericRecordPayload GenericRecord;
        public ExcelRecordPayload ExcelRecord;
        public ControlledTestOverridePayload ControlledTestOverride;
        public bool ControlledTestOverrideActive;
        public bool ControlledTestOverrideApplied;

        public static FinalEntryPackage Load(string path)
        {
            if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
            {
                throw new PackageValidationException("package_file_unavailable");
            }

            Dictionary<string, object> root;
            try
            {
                string json = File.ReadAllText(path, Encoding.UTF8);
                root = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(json);
            }
            catch (PackageValidationException)
            {
                throw;
            }
            catch
            {
                throw new PackageValidationException("package_json_invalid");
            }
            if (root == null)
            {
                throw new PackageValidationException("package_root_invalid");
            }

            int schemaVersion = RequireInt(root, "schema_version");
            if (schemaVersion != 1 && schemaVersion != 2)
            {
                throw new PackageValidationException("schema_version_unsupported");
            }
            if (schemaVersion == 1)
            {
                RequireOnly(root, "schema_version", "operation_type", "sample_number",
                    "check_item_no", "check_item_name", "expected_existing_register_count",
                    "generic_record", "excel_record");
            }
            else
            {
                RequireOnly(root, "schema_version", "operation_type", "sample_number",
                    "check_item_no", "check_item_name", "expected_existing_register_count",
                    "generic_record", "excel_record", "controlled_test_override",
                    "task_project");
            }

            var package = new FinalEntryPackage();
            package.SchemaVersion = schemaVersion;
            package.OperationType = RequireString(root, "operation_type", false, 64, false);
            if (package.OperationType != GenericOperation && package.OperationType != ExcelOperation)
            {
                throw new PackageValidationException("operation_type_unsupported");
            }
            package.SampleNumber = RequireString(root, "sample_number", false, 32, false);
            if (!SampleNoPattern.IsMatch(package.SampleNumber))
            {
                throw new PackageValidationException("sample_number_invalid");
            }
            package.CheckItemNo = RequireString(root, "check_item_no", false, 128, true);
            package.CheckItemName = RequireString(root, "check_item_name", false, 512, true);
            package.ExpectedExistingRegisterCount = RequireInt(root, "expected_existing_register_count");
            if (package.ExpectedExistingRegisterCount < 0)
            {
                throw new PackageValidationException("expected_existing_register_count_invalid");
            }

            if (package.OperationType == GenericOperation)
            {
                if (root.ContainsKey("excel_record"))
                {
                    throw new PackageValidationException("generic_package_contains_excel_record");
                }
                package.GenericRecord = ParseGeneric(RequireMap(root, "generic_record"));
            }
            else
            {
                if (root.ContainsKey("generic_record"))
                {
                    throw new PackageValidationException("excel_package_contains_generic_record");
                }
                package.ExcelRecord = ParseExcel(
                    RequireMap(root, "excel_record"), schemaVersion);
                if (package.CheckItemNo != "5103.5"
                    || package.CheckItemName != "纤维微观形貌")
                {
                    throw new PackageValidationException("excel_project_not_supported_in_v1");
                }
            }
            if (schemaVersion == 2)
            {
                package.TaskProject = ParseTaskProject(
                    RequireMap(root, "task_project"), package);
                if (package.OperationType == GenericOperation)
                {
                    ValidatePaperGenericScope(package);
                }
            }
            if (root.ContainsKey("controlled_test_override"))
            {
                // Schema v2 allows the single-sample controlled append for both
                // the microscopy Excel route and the paper generic route.  The
                // payload itself enforces the exact 1->2 count contract, and the
                // generic route is already paper-scoped by ValidatePaperGenericScope.
                if (schemaVersion != 2)
                {
                    throw new PackageValidationException(
                        "controlled_test_override_requires_excel_schema_v2");
                }
                package.ControlledTestOverride = ParseControlledTestOverride(
                    RequireMap(root, "controlled_test_override"), package);
            }
            return package;
        }

        private static TaskProjectPayload ParseTaskProject(
            Dictionary<string, object> map, FinalEntryPackage package)
        {
            RequireOnly(map, "project_key", "task_check_item_id", "check_item_id",
                "check_item_no", "check_item_name", "check_method", "seq_num",
                "check_count");
            var result = new TaskProjectPayload
            {
                ProjectKey = RequireString(map, "project_key", false, 100, false),
                TaskCheckItemId = RequireString(
                    map, "task_check_item_id", false, 80, false),
                CheckItemId = RequireString(map, "check_item_id", false, 80, false),
                CheckItemNo = RequireString(map, "check_item_no", false, 128, true),
                CheckItemName = RequireString(
                    map, "check_item_name", false, 512, true),
                CheckMethod = RequireString(
                    map, "check_method", false, 512, true),
                SeqNum = RequireInt(map, "seq_num"),
                CheckCount = RequireInt(map, "check_count"),
            };
            bool supportedProject = package.OperationType == ExcelOperation
                ? result.CheckItemNo == "5103.5"
                    && result.CheckItemName == "纤维微观形貌"
                    && result.CheckMethod == "GB/T 36422-2018"
                : result.CheckItemNo == PaperCheckItemNo
                    && result.CheckItemName == PaperCheckItemName
                    && result.CheckMethod == PaperCheckMethod;
            if (!ProjectKeyPattern.IsMatch(result.ProjectKey)
                || !RedactedIdPattern.IsMatch(result.TaskCheckItemId)
                || !RedactedIdPattern.IsMatch(result.CheckItemId)
                || result.CheckItemNo != package.CheckItemNo
                || result.CheckItemName != package.CheckItemName
                || !supportedProject
                || result.SeqNum < 0
                || result.CheckCount != 1
                || result.ProjectKey != BuildTaskProjectKey(result))
            {
                throw new PackageValidationException(
                    "task_project_binding_invalid");
            }
            return result;
        }

        private static void ValidatePaperGenericScope(FinalEntryPackage package)
        {
            if (package.CheckItemNo != PaperCheckItemNo
                || package.CheckItemName != PaperCheckItemName
                || package.GenericRecord == null
                || package.GenericRecord.Header == null
                || package.GenericRecord.Details == null
                || package.GenericRecord.Details.Count != 1)
            {
                throw new PackageValidationException(
                    "generic_schema_v2_scope_not_supported");
            }
            GenericHeader header = package.GenericRecord.Header;
            GenericDetail detail = package.GenericRecord.Details[0];
            string normalizedResult = (detail.RealValue ?? string.Empty)
                .Normalize(NormalizationForm.FormKC);
            bool containsOneHundred = Regex.IsMatch(
                normalizedResult,
                @"(?<![\p{L}\p{N}_.])100(?:\.0+)?(?![\p{L}\p{N}_.])",
                RegexOptions.CultureInvariant);
            // 两种合法形态共用的基线：实测值、测试方法、单位与其余空白字段。
            if (string.IsNullOrWhiteSpace(detail.RealValue)
                || !string.IsNullOrEmpty(detail.StandardLocation)
                || !string.IsNullOrEmpty(detail.RealLocation)
                || header.TestMethod != PaperCheckMethod
                || header.Unit != (containsOneHundred ? "%" : string.Empty)
                || !string.IsNullOrEmpty(header.Grade)
                || !string.IsNullOrEmpty(header.SampleDescription)
                || !string.IsNullOrEmpty(header.StandardType)
                || !string.IsNullOrEmpty(header.AttachInfo)
                || !string.IsNullOrEmpty(header.Remark))
            {
                throw new PackageValidationException(
                    "paper_generic_record_contract_invalid");
            }
            bool hasJudgement = !string.IsNullOrWhiteSpace(header.TotalJudge);
            if (hasJudgement)
            {
                // 判定变体（与人工登记样式一致，参照 260191286）：判定依据
                // 与总评定必填、报告项目名称等于项目名、标准值列与实测值
                // 同文。是否与任务单 GiveJudgement 匹配由 LegacySafetyGuards
                // 在执行前强制核验。
                if (string.IsNullOrWhiteSpace(header.JudgeBasis)
                    || header.ReportCheckItemName != PaperCheckItemName
                    || string.IsNullOrEmpty(detail.StandardValue)
                    || detail.StandardValue != detail.RealValue)
                {
                    throw new PackageValidationException(
                        "paper_generic_record_contract_invalid");
                }
            }
            else if (!string.IsNullOrEmpty(header.JudgeBasis)
                || !string.IsNullOrEmpty(header.TotalJudge)
                || !string.IsNullOrEmpty(header.ReportCheckItemName)
                || !string.IsNullOrEmpty(detail.StandardValue))
            {
                throw new PackageValidationException(
                    "paper_generic_record_contract_invalid");
            }
        }

        private static string BuildTaskProjectKey(TaskProjectPayload project)
        {
            string identity = string.Join("\0", new string[]
            {
                CompactText(project.TaskCheckItemId),
                CompactText(project.CheckItemId),
                CompactText(project.CheckItemNo),
                CompactText(project.CheckItemName),
                CompactText(project.CheckMethod),
                CompactText(project.SeqNum.ToString(CultureInfo.InvariantCulture)),
            });
            return "task-project:" + Sha256Text(identity).Substring(0, 24);
        }

        private static string CompactText(string value)
        {
            return Regex.Replace((value ?? string.Empty).Trim(), @"\s+", " ");
        }

        internal void BindControlledTestOverride(bool cliEnabled, string environmentTarget)
        {
            if (ControlledTestOverride == null)
            {
                if (cliEnabled)
                {
                    throw new PackageValidationException(
                        "controlled_test_override_package_required");
                }
                ControlledTestOverrideActive = false;
                return;
            }
            if (!cliEnabled)
            {
                throw new PackageValidationException(
                    "controlled_test_override_cli_flag_required");
            }
            string target = (environmentTarget ?? string.Empty).Trim().ToUpperInvariant();
            if (!SampleNoPattern.IsMatch(target)
                || !string.Equals(target, SampleNumber, StringComparison.Ordinal)
                || !string.Equals(
                    target,
                    ControlledTestOverride.TargetSampleNumber,
                    StringComparison.Ordinal))
            {
                throw new PackageValidationException(
                    "controlled_test_override_environment_target_mismatch");
            }
            ControlledTestOverrideActive = true;
        }

        internal bool AllowsOneAdditionalRegistration(int taskCheckCount)
        {
            return ControlledTestOverrideActive
                && ControlledTestOverride != null
                && taskCheckCount == ControlledTestOverride.ExpectedTaskCheckCount
                && ExpectedExistingRegisterCount
                    == ControlledTestOverride.ExpectedExistingRegisterCount
                && ExpectedExistingRegisterCount + 1
                    == ControlledTestOverride.ResultingRegisterCount;
        }

        private static ControlledTestOverridePayload ParseControlledTestOverride(
            Dictionary<string, object> map, FinalEntryPackage package)
        {
            RequireOnly(map, "kind", "target_sample_number",
                "expected_task_check_count", "expected_existing_register_count",
                "resulting_register_count", "reason");
            var result = new ControlledTestOverridePayload
            {
                Kind = RequireString(map, "kind", false, 64, false),
                TargetSampleNumber = RequireString(
                    map, "target_sample_number", false, 32, false).ToUpperInvariant(),
                ExpectedTaskCheckCount = RequireInt(map, "expected_task_check_count"),
                ExpectedExistingRegisterCount = RequireInt(
                    map, "expected_existing_register_count"),
                ResultingRegisterCount = RequireInt(map, "resulting_register_count"),
                Reason = RequireString(map, "reason", false, 500, true),
            };
            if (result.Kind != ControlledTestOverrideKind
                || result.TargetSampleNumber != package.SampleNumber
                || result.ExpectedTaskCheckCount != 1
                || result.ExpectedExistingRegisterCount != 1
                || result.ResultingRegisterCount != 2
                || package.ExpectedExistingRegisterCount != 1)
            {
                throw new PackageValidationException(
                    "controlled_test_override_scope_invalid");
            }
            return result;
        }

        private static GenericRecordPayload ParseGeneric(Dictionary<string, object> map)
        {
            RequireOnly(map, "header", "details");

            // Header is deliberately validated before details.  The old UI clears detail
            // values while changing judgement/header selections, so task producers must bind
            // a complete header snapshot before supplying ordered rows.
            Dictionary<string, object> headerMap = RequireMap(map, "header");
            RequireOnly(headerMap, "grade", "unit", "judge_basis", "test_method",
                "sample_description", "standard_type", "report_check_item_name",
                "attach_info", "remark", "total_judge");
            var header = new GenericHeader
            {
                Grade = RequireLegacyString(headerMap, "grade"),
                Unit = RequireLegacyString(headerMap, "unit"),
                JudgeBasis = RequireLegacyString(headerMap, "judge_basis"),
                TestMethod = RequireLegacyString(headerMap, "test_method"),
                SampleDescription = RequireLegacyString(headerMap, "sample_description"),
                StandardType = RequireLegacyString(headerMap, "standard_type"),
                ReportCheckItemName = RequireLegacyString(headerMap, "report_check_item_name"),
                AttachInfo = RequireLegacyString(headerMap, "attach_info"),
                Remark = RequireLegacyString(headerMap, "remark"),
                TotalJudge = RequireLegacyString(headerMap, "total_judge"),
            };

            IList values = RequireList(map, "details");
            if (values.Count == 0 || values.Count > 1000)
            {
                throw new PackageValidationException("generic_details_count_invalid");
            }
            var details = new List<GenericDetail>();
            foreach (object value in values)
            {
                Dictionary<string, object> row = value as Dictionary<string, object>;
                if (row == null)
                {
                    throw new PackageValidationException("generic_detail_invalid");
                }
                RequireOnly(row, "standard_location", "standard_value", "real_location", "real_value");
                details.Add(new GenericDetail
                {
                    StandardLocation = RequireLegacyString(row, "standard_location"),
                    StandardValue = RequireLegacyString(row, "standard_value"),
                    RealLocation = RequireLegacyString(row, "real_location"),
                    RealValue = RequireLegacyString(row, "real_value"),
                });
            }
            return new GenericRecordPayload { Header = header, Details = details };
        }

        private static ExcelRecordPayload ParseExcel(
            Dictionary<string, object> map, int schemaVersion)
        {
            RequireOnly(map, "template_name", "collection_mode", "expected_mapping_config_sha256",
                "key_result_count", "expected_key_identities", "register", "workbook");
            var result = new ExcelRecordPayload();
            result.TemplateName = RequireString(map, "template_name", false, 260, true);
            result.CollectionMode = RequireString(map, "collection_mode", false, 32, false);
            // v1 deliberately supports only the unmodified CollectData path.  The old
            // CheckRecordRegisterMaintenanceBLL applies additional private transforms
            // after CollectData_GAP/NewChEn/CLOTHING; calling those collector methods
            // directly would silently persist data different from the desktop client.
            if (result.CollectionMode != "standard")
            {
                throw new PackageValidationException("collection_mode_not_supported_in_v1");
            }
            result.ExpectedMappingConfigSha256 = RequireString(
                map, "expected_mapping_config_sha256", false, 64, false).ToLowerInvariant();
            if (!ShaPattern.IsMatch(result.ExpectedMappingConfigSha256))
            {
                throw new PackageValidationException("mapping_config_sha256_invalid");
            }
            result.KeyResultCount = RequireInt(map, "key_result_count");
            if (result.KeyResultCount < 1 || result.KeyResultCount > 10000)
            {
                throw new PackageValidationException("key_result_count_invalid");
            }
            IList identityValues = RequireList(map, "expected_key_identities");
            if (identityValues.Count != result.KeyResultCount)
            {
                throw new PackageValidationException("expected_key_identities_count_mismatch");
            }
            result.ExpectedKeyIdentities = new List<string>();
            foreach (object raw in identityValues)
            {
                string identity = raw as string;
                if (identity == null || (schemaVersion == 1 && string.IsNullOrWhiteSpace(identity))
                    || identity.Length > 512
                    || identity.IndexOf('\0') >= 0 || identity.IndexOf('\'') >= 0)
                {
                    throw new PackageValidationException("expected_key_identity_invalid");
                }
                result.ExpectedKeyIdentities.Add(identity);
            }

            Dictionary<string, object> register = RequireMap(map, "register");
            RequireOnly(register, "level", "sample_identity", "equipment_no", "check_basis");
            result.Register = new ExcelRegisterFields
            {
                Level = RequireLegacyString(register, "level"),
                SampleIdentity = RequireLegacyString(register, "sample_identity"),
                EquipmentNo = RequireLegacyString(register, "equipment_no"),
                CheckBasis = RequireLegacyString(register, "check_basis"),
            };

            Dictionary<string, object> workbook = RequireMap(map, "workbook");
            RequireOnly(workbook, "relative_path", "filename", "size_bytes", "content_sha256");
            result.Workbook = new WorkbookPayload
            {
                RelativePath = RequireString(workbook, "relative_path", false, 1024, false),
                Filename = RequireString(workbook, "filename", false, 260, false),
                SizeBytes = RequireLong(workbook, "size_bytes"),
                ContentSha256 = RequireString(workbook, "content_sha256", false, 64, false).ToLowerInvariant(),
            };
            ValidateWorkbook(result.Workbook);
            ValidateExcelScope(result, schemaVersion);
            return result;
        }

        private static void ValidateExcelScope(
            ExcelRecordPayload result, int schemaVersion)
        {
            // v1 intentionally implements only the exact Excel route proven against
            // 26A045793. Other template-selection branches have additional desktop-only
            // business rules and must remain fail-closed until separately modelled.
            bool identityInvalid = schemaVersion == 1
                && result.ExpectedKeyIdentities.Count == 1
                && result.ExpectedKeyIdentities[0] != "纵面"
                && result.ExpectedKeyIdentities[0] != "横截面";
            bool templateInvalid;
            if (schemaVersion == 1)
            {
                templateInvalid = result.TemplateName != "微观形貌.xls";
            }
            else
            {
                string expectedMapping;
                templateInvalid = !SupportedMicroscopyTemplates.TryGetValue(
                        result.TemplateName, out expectedMapping)
                    || !string.Equals(
                        expectedMapping,
                        result.ExpectedMappingConfigSha256,
                        StringComparison.OrdinalIgnoreCase);
            }
            if (templateInvalid || result.KeyResultCount != 1
                || result.ExpectedKeyIdentities.Count != 1 || identityInvalid
                || !string.IsNullOrEmpty(result.Register.Level)
                || !string.IsNullOrEmpty(result.Register.SampleIdentity)
                || !string.IsNullOrEmpty(result.Register.EquipmentNo)
                || !string.IsNullOrEmpty(result.Register.CheckBasis)
                || Path.GetExtension(result.Workbook.Filename).ToLowerInvariant() != ".xls")
            {
                throw new PackageValidationException(
                    schemaVersion == 1
                        ? "excel_scope_not_supported_in_v1"
                        : "excel_scope_not_supported_in_v2");
            }
        }

        private static void ValidateWorkbook(WorkbookPayload workbook)
        {
            if (workbook.SizeBytes < 0 || !ShaPattern.IsMatch(workbook.ContentSha256))
            {
                throw new PackageValidationException("workbook_manifest_invalid");
            }
            if (Path.IsPathRooted(workbook.RelativePath)
                || !string.Equals(Path.GetFileName(workbook.Filename), workbook.Filename, StringComparison.Ordinal)
                || workbook.Filename == "." || workbook.Filename == ".."
                || workbook.Filename.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
            {
                throw new PackageValidationException("workbook_path_invalid");
            }
            string extension = Path.GetExtension(workbook.Filename).ToLowerInvariant();
            if (extension != ".xls" && extension != ".xlsx" && extension != ".xlsm")
            {
                throw new PackageValidationException("workbook_extension_invalid");
            }
            string relativeFile = Path.GetFileName(workbook.RelativePath);
            if (!string.Equals(relativeFile, workbook.Filename, StringComparison.Ordinal))
            {
                throw new PackageValidationException("workbook_filename_mismatch");
            }
        }

        internal string ResolveAndVerifyWorkbook(string sourceRoot)
        {
            if (OperationType != ExcelOperation)
            {
                return null;
            }
            if (string.IsNullOrWhiteSpace(sourceRoot) || !Directory.Exists(sourceRoot))
            {
                throw new PackageValidationException("source_root_unavailable");
            }
            string root = Path.GetFullPath(sourceRoot).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            string resolved = Path.GetFullPath(Path.Combine(root, ExcelRecord.Workbook.RelativePath));
            string prefix = root + Path.DirectorySeparatorChar;
            if (!resolved.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) || !File.Exists(resolved))
            {
                throw new PackageValidationException("workbook_outside_source_root_or_missing");
            }
            var info = new FileInfo(resolved);
            if (info.Length != ExcelRecord.Workbook.SizeBytes)
            {
                throw new PackageValidationException("workbook_size_mismatch");
            }
            string actualSha = Sha256File(resolved);
            if (!string.Equals(actualSha, ExcelRecord.Workbook.ContentSha256, StringComparison.OrdinalIgnoreCase))
            {
                throw new PackageValidationException("workbook_sha256_mismatch");
            }
            return resolved;
        }

        internal static string Sha256File(string path)
        {
            using (FileStream stream = File.OpenRead(path))
            using (SHA256 sha = SHA256.Create())
            {
                return Hex(sha.ComputeHash(stream));
            }
        }

        internal static string Sha256Text(string value)
        {
            using (SHA256 sha = SHA256.Create())
            {
                return Hex(sha.ComputeHash(Encoding.UTF8.GetBytes(value ?? string.Empty)));
            }
        }

        private static string Hex(byte[] bytes)
        {
            var builder = new StringBuilder(bytes.Length * 2);
            foreach (byte value in bytes)
            {
                builder.Append(value.ToString("x2"));
            }
            return builder.ToString();
        }

        private static string RequireLegacyString(Dictionary<string, object> map, string key)
        {
            // Oracle treats the empty string as NULL; the official DAL reads it back as "".
            // Keeping the exact package string still matters for ordered comparison and receipts.
            return RequireString(map, key, true, 4000, true);
        }

        private static string RequireString(
            Dictionary<string, object> map, string key, bool allowEmpty, int maxLength, bool rejectQuote)
        {
            object raw;
            if (!map.TryGetValue(key, out raw) || !(raw is string))
            {
                throw new PackageValidationException("missing_or_invalid_string_" + key);
            }
            string value = (string)raw;
            if ((!allowEmpty && string.IsNullOrWhiteSpace(value)) || value.Length > maxLength
                || value.IndexOf('\0') >= 0 || (rejectQuote && value.IndexOf('\'') >= 0))
            {
                throw new PackageValidationException("unsafe_or_invalid_string_" + key);
            }
            return value;
        }

        private static int RequireInt(Dictionary<string, object> map, string key)
        {
            long value = RequireLong(map, key);
            if (value < int.MinValue || value > int.MaxValue)
            {
                throw new PackageValidationException("integer_out_of_range_" + key);
            }
            return (int)value;
        }

        private static long RequireLong(Dictionary<string, object> map, string key)
        {
            object raw;
            long value;
            if (!map.TryGetValue(key, out raw) || raw == null
                || !long.TryParse(raw.ToString(), out value))
            {
                throw new PackageValidationException("missing_or_invalid_integer_" + key);
            }
            return value;
        }

        private static Dictionary<string, object> RequireMap(Dictionary<string, object> map, string key)
        {
            object raw;
            Dictionary<string, object> value;
            if (!map.TryGetValue(key, out raw) || (value = raw as Dictionary<string, object>) == null)
            {
                throw new PackageValidationException("missing_or_invalid_object_" + key);
            }
            return value;
        }

        private static IList RequireList(Dictionary<string, object> map, string key)
        {
            object raw;
            IList value;
            if (!map.TryGetValue(key, out raw) || (value = raw as IList) == null)
            {
                throw new PackageValidationException("missing_or_invalid_array_" + key);
            }
            return value;
        }

        private static void RequireOnly(Dictionary<string, object> map, params string[] allowed)
        {
            var set = new HashSet<string>(allowed, StringComparer.Ordinal);
            foreach (string key in map.Keys)
            {
                if (!set.Contains(key))
                {
                    throw new PackageValidationException("unknown_field_" + key);
                }
            }
        }
    }

    internal sealed class GenericRecordPayload
    {
        public GenericHeader Header;
        public List<GenericDetail> Details;
    }

    internal sealed class GenericHeader
    {
        public string Grade;
        public string Unit;
        public string JudgeBasis;
        public string TestMethod;
        public string SampleDescription;
        public string StandardType;
        public string ReportCheckItemName;
        public string AttachInfo;
        public string Remark;
        public string TotalJudge;
    }

    internal sealed class GenericDetail
    {
        public string StandardLocation;
        public string StandardValue;
        public string RealLocation;
        public string RealValue;
    }

    internal sealed class ExcelRecordPayload
    {
        public string TemplateName;
        public string CollectionMode;
        public string ExpectedMappingConfigSha256;
        public int KeyResultCount;
        public List<string> ExpectedKeyIdentities;
        public ExcelRegisterFields Register;
        public WorkbookPayload Workbook;
    }

    internal sealed class ExcelRegisterFields
    {
        public string Level;
        public string SampleIdentity;
        public string EquipmentNo;
        public string CheckBasis;
    }

    internal sealed class WorkbookPayload
    {
        public string RelativePath;
        public string Filename;
        public long SizeBytes;
        public string ContentSha256;
    }

    internal sealed class TaskProjectPayload
    {
        public string ProjectKey;
        public string TaskCheckItemId;
        public string CheckItemId;
        public string CheckItemNo;
        public string CheckItemName;
        public string CheckMethod;
        public int SeqNum;
        public int CheckCount;

        public SortedDictionary<string, object> ToReceiptMap()
        {
            return new SortedDictionary<string, object>
            {
                { "project_key", ProjectKey },
                { "task_check_item_id", TaskCheckItemId },
                { "check_item_id", CheckItemId },
                { "check_item_no", CheckItemNo },
                { "check_item_name", CheckItemName },
                { "check_method", CheckMethod },
                { "seq_num", SeqNum },
                { "check_count", CheckCount },
            };
        }
    }

    internal sealed class ControlledTestOverridePayload
    {
        public string Kind;
        public string TargetSampleNumber;
        public int ExpectedTaskCheckCount;
        public int ExpectedExistingRegisterCount;
        public int ResultingRegisterCount;
        public string Reason;
    }
}
