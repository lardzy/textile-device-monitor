using System;
using System.Collections;
using System.Collections.Generic;
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

        private static readonly Regex SampleNoPattern =
            new Regex(@"^[0-9A-Z]{9,20}(?:-[0-9A-Z]{1,8})?$", RegexOptions.CultureInvariant);
        private static readonly Regex ShaPattern =
            new Regex(@"^[0-9a-fA-F]{64}$", RegexOptions.CultureInvariant);

        public string OperationType;
        public string SampleNumber;
        public string CheckItemNo;
        public string CheckItemName;
        public int ExpectedExistingRegisterCount;
        public GenericRecordPayload GenericRecord;
        public ExcelRecordPayload ExcelRecord;

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

            RequireOnly(root, "schema_version", "operation_type", "sample_number",
                "check_item_no", "check_item_name", "expected_existing_register_count",
                "generic_record", "excel_record");
            if (RequireInt(root, "schema_version") != 1)
            {
                throw new PackageValidationException("schema_version_unsupported");
            }

            var package = new FinalEntryPackage();
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
                package.ExcelRecord = ParseExcel(RequireMap(root, "excel_record"));
                if (package.CheckItemNo != "5103.5"
                    || package.CheckItemName != "纤维微观形貌")
                {
                    throw new PackageValidationException("excel_project_not_supported_in_v1");
                }
            }
            return package;
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

        private static ExcelRecordPayload ParseExcel(Dictionary<string, object> map)
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
                if (string.IsNullOrWhiteSpace(identity) || identity.Length > 512
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
            ValidateExcelV1Scope(result);
            return result;
        }

        private static void ValidateExcelV1Scope(ExcelRecordPayload result)
        {
            // v1 intentionally implements only the exact Excel route proven against
            // 26A045793. Other template-selection branches have additional desktop-only
            // business rules and must remain fail-closed until separately modelled.
            if (result.TemplateName != "微观形貌.xls" || result.KeyResultCount != 1
                || result.ExpectedKeyIdentities.Count != 1
                || (result.ExpectedKeyIdentities[0] != "纵面"
                    && result.ExpectedKeyIdentities[0] != "横截面")
                || !string.IsNullOrEmpty(result.Register.Level)
                || !string.IsNullOrEmpty(result.Register.SampleIdentity)
                || !string.IsNullOrEmpty(result.Register.EquipmentNo)
                || !string.IsNullOrEmpty(result.Register.CheckBasis)
                || Path.GetExtension(result.Workbook.Filename).ToLowerInvariant() != ".xls")
            {
                throw new PackageValidationException("excel_scope_not_supported_in_v1");
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
}
