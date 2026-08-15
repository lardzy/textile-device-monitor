using System;
using System.Collections.Generic;
using System.Data;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using LegacyFibreCheckRunner;

namespace LegacyFibreCheckWriter
{
    /// <summary>
    /// 图片类特种毛上传/复核的纯契约工具。这里不引用旧系统 DLL，可由离线
    /// SelfTest 独立编译，避免把数据库连通性误当成契约测试。
    /// </summary>
    internal static class SpecialWoolContracts
    {
        internal const string ImageOperation = "legacy_special_wool_image_upload";
        internal const string ReviewOperation = "legacy_special_wool_review";
        internal const string QualitativeUploadOperation =
            "legacy_special_wool_qualitative_upload";
        internal const string QualitativeReviewOperation =
            "legacy_special_wool_qualitative_review";
        internal const string ImageReceiptType = "legacy_special_wool_image_upload";
        internal const string ReviewReceiptType = "legacy_special_wool_review";
        internal const string QualitativeUploadReceiptType =
            "legacy_special_wool_qualitative_upload";
        internal const string QualitativeReviewReceiptType =
            "legacy_special_wool_qualitative_review";
        internal const string ImageObservationType = "legacy_special_wool_image_upload_dry_run";
        internal const string ReviewObservationType = "legacy_special_wool_review_dry_run";
        internal const string QualitativeUploadObservationType =
            "legacy_special_wool_qualitative_upload_dry_run";
        internal const string QualitativeReviewObservationType =
            "legacy_special_wool_qualitative_review_dry_run";
        internal const string OriginalTemplateFilename =
            "39-8B-纤维形状截面定量试验-2026.xls";
        internal const int LegacyOriginalDataFileNameMaxLength = 100;

        internal static readonly string[] ImageStages =
        {
            "authenticated",
            "permission_verified",
            "remote_state_verified",
            "task_project_verified",
            "file_copy_ready",
            "file_copy_started",
            "file_copy_verified",
            "main_record_save_started",
            "main_record_verified",
            "picture_child_verified",
            "completed",
        };

        internal static readonly string[] ReviewStages =
        {
            "authenticated",
            "permission_verified",
            "remote_state_verified",
            "review_save_ready",
            "review_save_started",
            "review_main_verified",
            "review_children_verified",
            "completed",
        };

        internal static readonly string[] QualitativeUploadStages =
        {
            "authenticated",
            "permission_verified",
            "remote_state_verified",
            "task_project_verified",
            "file_copy_ready",
            "file_copy_started",
            "file_copy_verified",
            "main_record_save_started",
            "main_record_verified",
            "completed",
        };

        internal static readonly string[] QualitativeReviewStages =
        {
            "authenticated",
            "permission_verified",
            "remote_state_verified",
            "review_save_ready",
            "review_save_started",
            "review_main_verified",
            "completed",
        };

        private static readonly Regex FamilySuffix = new Regex(
            @"^(?<base>[0-9A-Z]{9,20})(?:-(?<suffix>[1-9][0-9]*))?$",
            RegexOptions.CultureInvariant);
        private static readonly Regex RedactedId = new Regex(
            @"^sha256:[0-9a-f]{16}$",
            RegexOptions.CultureInvariant);

        internal static string BuildTargetFilename(string targetSampleNumber)
        {
            string target = (targetSampleNumber ?? string.Empty).Trim().ToUpperInvariant();
            if (!FamilySuffix.IsMatch(target))
            {
                throw new ArgumentException("target_sample_number_invalid", "targetSampleNumber");
            }
            string filename = target + "-" + OriginalTemplateFilename;
            if (!string.Equals(Path.GetFileName(filename), filename, StringComparison.Ordinal)
                || filename.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0
                || filename.IndexOf('/') >= 0
                || filename.IndexOf('\\') >= 0)
            {
                throw new ArgumentException("target_filename_invalid", "targetSampleNumber");
            }
            return filename;
        }

        internal static string BuildPrefixedTargetFilename(
            string targetSampleNumber,
            string sourceFilename)
        {
            string target = (targetSampleNumber ?? string.Empty)
                .Trim()
                .ToUpperInvariant();
            string source = sourceFilename ?? string.Empty;
            if (!FamilySuffix.IsMatch(target)
                || string.IsNullOrWhiteSpace(source)
                || !string.Equals(
                    Path.GetFileName(source), source, StringComparison.Ordinal)
                || source == "."
                || source == ".."
                || source.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0
                || !string.Equals(
                    Path.GetExtension(source), ".xls",
                    StringComparison.OrdinalIgnoreCase))
            {
                throw new ArgumentException(
                    "target_or_source_filename_invalid", "sourceFilename");
            }
            string filename = target + "-" + source;
            if (filename.Length > 240
                || !string.Equals(
                    Path.GetFileName(filename), filename, StringComparison.Ordinal))
            {
                throw new ArgumentException(
                    "target_filename_invalid", "sourceFilename");
            }
            return filename;
        }

        internal static bool IsRedactedId(string value)
        {
            return !string.IsNullOrWhiteSpace(value) && RedactedId.IsMatch(value);
        }

        internal static bool MatchesRedactedId(string expected, string rawId)
        {
            return IsRedactedId(expected)
                && !string.IsNullOrWhiteSpace(rawId)
                && string.Equals(expected, Redact.HashId(rawId), StringComparison.Ordinal);
        }

        internal static bool MatchesLoginInspector(
            string inspectorName,
            string inspectorId,
            string loginChineseName,
            string loginStaffId)
        {
            return !string.IsNullOrWhiteSpace(inspectorId)
                && !string.IsNullOrWhiteSpace(loginStaffId)
                && string.Equals(
                    NormalizeBusinessText(inspectorId),
                    NormalizeBusinessText(loginStaffId),
                    StringComparison.Ordinal)
                && string.Equals(
                    NormalizeBusinessText(inspectorName),
                    NormalizeBusinessText(loginChineseName),
                    StringComparison.Ordinal);
        }

        internal static bool TryBindInspectorToAuthenticatedStaff(
            string executionActorDisplayName,
            string loginChineseName,
            string loginStaffId,
            out string inspectorName,
            out string inspectorId)
        {
            // 原始记录由当前凭据登录旧系统后写入，旧系统账号是所有旧系统流程
            // 的通用身份。执行系统账号的 display_name 只用于审计，不能拿它去
            // 旧库 User.ChineseName 做二次映射；二者并不要求同名（例如内置
            // “执行系统管理员”账号）。已认证的旧系统 Staff 是实际 CheckUser1
            // 的唯一权威来源。
            inspectorName = NormalizeBusinessText(loginChineseName);
            inspectorId = NormalizeBusinessText(loginStaffId);
            return !string.IsNullOrWhiteSpace(executionActorDisplayName)
                && !string.IsNullOrWhiteSpace(inspectorName)
                && !string.IsNullOrWhiteSpace(inspectorId);
        }

        internal static bool MatchesExpectedPositiveCheckCount(
            object actual,
            object expected)
        {
            decimal actualCount;
            decimal expectedCount;
            return TryReadPositiveIntegerCount(actual, out actualCount)
                && TryReadPositiveIntegerCount(expected, out expectedCount)
                && actualCount == expectedCount;
        }

        private static bool TryReadPositiveIntegerCount(
            object value,
            out decimal count)
        {
            count = 0m;
            string text = NormalizeBusinessText(value);
            if (!Regex.IsMatch(
                text,
                @"^[1-9][0-9]*(?:\.0+)?$",
                RegexOptions.CultureInvariant))
            {
                return false;
            }
            return decimal.TryParse(
                text,
                NumberStyles.AllowDecimalPoint,
                CultureInfo.InvariantCulture,
                out count);
        }

        internal static bool MatchesLegacyOriginalDataFileName(
            string actual,
            string expected)
        {
            string actualValue = actual ?? string.Empty;
            string expectedValue = expected ?? string.Empty;
            if (string.Equals(
                actualValue,
                expectedValue,
                StringComparison.Ordinal))
            {
                return true;
            }
            // Oracle/EF 模型中 OriginalDataFileName 是 NVARCHAR2(100)。旧客户端
            // 直接保存本机选择路径；当 Bridge 的隔离 staging 路径超过该上限时，
            // 旧 DAL/Provider 会保留前 100 个字符。只接受这个确定性前缀，不接受
            // 任意短值，避免把真正的子记录串线误判为合法规范化。
            return expectedValue.Length > LegacyOriginalDataFileNameMaxLength
                && actualValue.Length == LegacyOriginalDataFileNameMaxLength
                && string.Equals(
                    actualValue,
                    expectedValue.Substring(
                        0,
                        LegacyOriginalDataFileNameMaxLength),
                    StringComparison.Ordinal);
        }

        internal static bool MatchesTargetOriginalDataFileName(
            string actual,
            string targetSampleNumber)
        {
            return MatchesLegacyOriginalDataFileName(
                actual,
                BuildTargetFilename(targetSampleNumber));
        }

        internal static string AllocateFirstFree(
            string baseNumber,
            IEnumerable<string> occupiedNumbers)
        {
            if (string.IsNullOrWhiteSpace(baseNumber)
                || !Regex.IsMatch(baseNumber, @"^[0-9A-Z]{9,20}$", RegexOptions.CultureInvariant))
            {
                throw new ArgumentException("target_base_invalid", "baseNumber");
            }
            var occupied = new HashSet<string>(StringComparer.Ordinal);
            if (occupiedNumbers != null)
            {
                foreach (string raw in occupiedNumbers)
                {
                    string value = (raw ?? string.Empty).Trim().ToUpperInvariant();
                    Match match = FamilySuffix.Match(value);
                    if (match.Success
                        && string.Equals(match.Groups["base"].Value, baseNumber, StringComparison.Ordinal))
                    {
                        occupied.Add(value);
                    }
                }
            }
            if (!occupied.Contains(baseNumber))
            {
                return baseNumber;
            }
            for (int suffix = 1; suffix < int.MaxValue; suffix++)
            {
                string candidate = baseNumber + "-" + suffix.ToString(CultureInfo.InvariantCulture);
                if (!occupied.Contains(candidate))
                {
                    return candidate;
                }
            }
            throw new InvalidOperationException("target_number_exhausted");
        }

        internal static string ProjectKey(
            string taskCheckItemId,
            string checkItemId,
            string checkItemNo,
            string checkItemName,
            string checkMethod,
            object seqNum)
        {
            string identity = string.Join("\0", new[]
            {
                NormalizeBusinessText(Redact.HashId(taskCheckItemId)),
                NormalizeBusinessText(Redact.HashId(checkItemId)),
                NormalizeBusinessText(checkItemNo),
                NormalizeBusinessText(checkItemName),
                NormalizeBusinessText(checkMethod),
                NormalizeBusinessText(seqNum),
            });
            return "task-project:" + Sha256Hex(Encoding.UTF8.GetBytes(identity)).Substring(0, 24);
        }

        internal static string NormalizeBusinessText(object value)
        {
            string text = Convert.ToString(value, CultureInfo.InvariantCulture) ?? string.Empty;
            string[] parts = Regex.Split(text.Trim(), @"\s+");
            return string.Join(" ", Array.FindAll(parts, item => item.Length > 0));
        }

        internal static bool MatchesBusinessText(object actual, object expected)
        {
            return string.Equals(
                NormalizeBusinessText(actual),
                NormalizeBusinessText(expected),
                StringComparison.Ordinal);
        }

        internal static string FingerprintRow(DataRow row, ISet<string> excludedColumns)
        {
            if (row == null)
            {
                return Sha256Hex(new byte[0]);
            }
            var columns = new List<DataColumn>();
            foreach (DataColumn column in row.Table.Columns)
            {
                if (excludedColumns == null || !excludedColumns.Contains(column.ColumnName))
                {
                    columns.Add(column);
                }
            }
            columns.Sort((left, right) => string.CompareOrdinal(left.ColumnName, right.ColumnName));
            var builder = new StringBuilder();
            foreach (DataColumn column in columns)
            {
                AppendFramed(builder, column.ColumnName);
                AppendFramed(builder, CanonicalValue(row[column]));
            }
            return Sha256Hex(Encoding.UTF8.GetBytes(builder.ToString()));
        }

        internal static string FingerprintRows(DataTable table)
        {
            var rowFingerprints = new List<string>();
            if (table != null)
            {
                foreach (DataRow row in table.Rows)
                {
                    rowFingerprints.Add(FingerprintRow(row, null));
                }
            }
            rowFingerprints.Sort(StringComparer.Ordinal);
            return Sha256Hex(Encoding.UTF8.GetBytes(string.Join("\n", rowFingerprints.ToArray())));
        }

        internal static string Sha256Hex(byte[] data)
        {
            using (SHA256 sha = SHA256.Create())
            {
                byte[] digest = sha.ComputeHash(data ?? new byte[0]);
                var builder = new StringBuilder();
                foreach (byte value in digest)
                {
                    builder.Append(value.ToString("x2", CultureInfo.InvariantCulture));
                }
                return builder.ToString();
            }
        }

        private static void AppendFramed(StringBuilder builder, string value)
        {
            string text = value ?? string.Empty;
            builder.Append(text.Length.ToString(CultureInfo.InvariantCulture));
            builder.Append(':');
            builder.Append(text);
            builder.Append(';');
        }

        private static string CanonicalValue(object value)
        {
            if (value == null || value == DBNull.Value)
            {
                return "<null>";
            }
            byte[] bytes = value as byte[];
            if (bytes != null)
            {
                return "bytes:" + Convert.ToBase64String(bytes);
            }
            if (value is DateTime)
            {
                return ((DateTime)value).ToString("yyyy-MM-ddTHH:mm:ss.fffffff", CultureInfo.InvariantCulture);
            }
            if (value is bool)
            {
                return (bool)value ? "true" : "false";
            }
            IFormattable formattable = value as IFormattable;
            return formattable != null
                ? formattable.ToString(null, CultureInfo.InvariantCulture)
                : value.ToString();
        }
    }
}
