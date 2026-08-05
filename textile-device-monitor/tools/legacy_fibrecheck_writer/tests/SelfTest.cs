using System;
using System.Collections.Generic;
using System.Data;
using LegacyFibreCheckRunner;

namespace LegacyFibreCheckWriter
{
    internal static class SelfTest
    {
        private static int failures;
        private static int passed;

        private static void Main()
        {
            Run("base_number_is_used_when_free", BaseNumberIsUsedWhenFree);
            Run("first_numeric_suffix_is_allocated", FirstNumericSuffixIsAllocated);
            Run("unrelated_family_entries_are_ignored", UnrelatedFamilyEntriesAreIgnored);
            Run("image_and_review_stages_are_separate", ImageAndReviewStagesAreSeparate);
            Run("qualitative_stages_have_no_picture_checkpoint", QualitativeStagesHaveNoPictureCheckpoint);
            Run("project_key_is_stable", ProjectKeyIsStable);
            Run("row_fingerprint_detects_business_change", RowFingerprintDetectsBusinessChange);
            Run("review_invariant_excludes_only_review_fields", ReviewInvariantExcludesOnlyReviewFields);
            Run("children_fingerprint_is_order_independent", ChildrenFingerprintIsOrderIndependent);
            Run("target_filename_uses_final_base_number", TargetFilenameUsesFinalBaseNumber);
            Run("target_filename_uses_final_suffix_number", TargetFilenameUsesFinalSuffixNumber);
            Run("target_filename_uses_second_suffix_number", TargetFilenameUsesSecondSuffixNumber);
            Run("target_filename_fits_legacy_original_data_column", TargetFilenameFitsLegacyOriginalDataColumn);
            Run("qualitative_filename_preserves_source_name", QualitativeFilenamePreservesSourceName);
            Run("review_main_id_binding_rejects_replacement", ReviewMainIdBindingRejectsReplacement);
            Run("inspector_must_match_login_staff", InspectorMustMatchLoginStaff);
            Run("project_check_count_must_remain_one", ProjectCheckCountMustRemainOne);
            Run("legacy_original_path_accepts_exact_value", LegacyOriginalPathAcceptsExactValue);
            Run("legacy_original_path_length_boundaries", LegacyOriginalPathLengthBoundaries);
            Run("legacy_original_path_accepts_only_deterministic_truncation", LegacyOriginalPathAcceptsOnlyDeterministicTruncation);
            Run("target_original_data_filename_rejects_staging_path", TargetOriginalDataFilenameRejectsStagingPath);
            Run("server_file_exact_bytes_are_accepted", ServerFileExactBytesAreAccepted);
            Run("biff_accepts_only_writeaccess_changes", BiffAcceptsOnlyWriteAccessChanges);
            if (failures != 0)
            {
                Environment.ExitCode = 1;
                return;
            }
            Console.WriteLine(
                "SpecialWool writer contract self-test: " + passed + " passed");
        }

        private static void BaseNumberIsUsedWhenFree()
        {
            Equal("260111037", SpecialWoolContracts.AllocateFirstFree(
                "260111037", new[] { "260111037-1" }));
        }

        private static void FirstNumericSuffixIsAllocated()
        {
            Equal("260111037-2", SpecialWoolContracts.AllocateFirstFree(
                "260111037",
                new[] { "260111037", "260111037-1", "260111037-3" }));
        }

        private static void UnrelatedFamilyEntriesAreIgnored()
        {
            Equal("260111037-1", SpecialWoolContracts.AllocateFirstFree(
                "260111037",
                new[] { "260111037", "260111037-A", "2601110370-1", "OTHER" }));
        }

        private static void ImageAndReviewStagesAreSeparate()
        {
            Contains(SpecialWoolContracts.ImageStages, "picture_child_verified");
            NotContains(SpecialWoolContracts.ImageStages, "review_save_started");
            Contains(SpecialWoolContracts.ReviewStages, "review_children_verified");
            NotContains(SpecialWoolContracts.ReviewStages, "file_copy_started");
        }

        private static void QualitativeStagesHaveNoPictureCheckpoint()
        {
            Contains(
                SpecialWoolContracts.QualitativeUploadStages,
                "main_record_verified");
            NotContains(
                SpecialWoolContracts.QualitativeUploadStages,
                "picture_child_verified");
            Contains(
                SpecialWoolContracts.QualitativeReviewStages,
                "review_main_verified");
            NotContains(
                SpecialWoolContracts.QualitativeReviewStages,
                "review_children_verified");
        }

        private static void ProjectKeyIsStable()
        {
            string first = SpecialWoolContracts.ProjectKey(
                "tci", "ci", "5103.5", " 纤维微观形貌 ",
                "GB/T 36422-2018", 1);
            string second = SpecialWoolContracts.ProjectKey(
                "tci", "ci", "5103.5", "纤维微观形貌",
                "GB/T   36422-2018", "1");
            Equal(first, second);
            True(first.StartsWith("task-project:", StringComparison.Ordinal));
            Equal(37, first.Length);
            Equal("task-project:489be6582752e23f9675aee5", first);
        }

        private static void RowFingerprintDetectsBusinessChange()
        {
            DataTable table = MainTable();
            DataRow row = table.Rows.Add("main-1", "260111037", null, null, "图片");
            string before = SpecialWoolContracts.FingerprintRow(row, null);
            row["FileType"] = "定量试验";
            string after = SpecialWoolContracts.FingerprintRow(row, null);
            NotEqual(before, after);
        }

        private static void ReviewInvariantExcludesOnlyReviewFields()
        {
            DataTable table = MainTable();
            DataRow row = table.Rows.Add("main-1", "260111037", null, null, "图片");
            var excluded = new HashSet<string>(StringComparer.Ordinal)
            {
                "ReviewUser",
                "ReviewTime",
            };
            string before = SpecialWoolContracts.FingerprintRow(row, excluded);
            row["ReviewUser"] = "reviewer";
            row["ReviewTime"] = new DateTime(2026, 8, 5, 12, 0, 0);
            Equal(before, SpecialWoolContracts.FingerprintRow(row, excluded));
            row["FileType"] = "changed";
            NotEqual(before, SpecialWoolContracts.FingerprintRow(row, excluded));
        }

        private static void ChildrenFingerprintIsOrderIndependent()
        {
            DataTable first = ChildTable();
            first.Rows.Add("b", "main", "item", "b.xls");
            first.Rows.Add("a", "main", "item", "a.xls");
            DataTable second = ChildTable();
            second.Rows.Add("a", "main", "item", "a.xls");
            second.Rows.Add("b", "main", "item", "b.xls");
            Equal(
                SpecialWoolContracts.FingerprintRows(first),
                SpecialWoolContracts.FingerprintRows(second));
            second.Rows[1]["PictureFileName"] = "changed.xls";
            NotEqual(
                SpecialWoolContracts.FingerprintRows(first),
                SpecialWoolContracts.FingerprintRows(second));
        }

        private static void TargetFilenameUsesFinalBaseNumber()
        {
            Equal(
                "260111037-39-8B-纤维形状截面定量试验-2026.xls",
                SpecialWoolContracts.BuildTargetFilename("260111037"));
        }

        private static void TargetFilenameUsesFinalSuffixNumber()
        {
            Equal(
                "260111037-1-39-8B-纤维形状截面定量试验-2026.xls",
                SpecialWoolContracts.BuildTargetFilename("260111037-1"));
        }

        private static void TargetFilenameUsesSecondSuffixNumber()
        {
            Equal(
                "260111037-2-39-8B-纤维形状截面定量试验-2026.xls",
                SpecialWoolContracts.BuildTargetFilename("260111037-2"));
        }

        private static void TargetFilenameFitsLegacyOriginalDataColumn()
        {
            string filename = SpecialWoolContracts.BuildTargetFilename(
                "260111037-999999");
            True(filename.Length <=
                SpecialWoolContracts.LegacyOriginalDataFileNameMaxLength);
        }

        private static void QualitativeFilenamePreservesSourceName()
        {
            const string source =
                "26W006701-107-8A-（纸类）纤维组成定性分析检验原始记录GB T 4688定量试验.xls";
            Equal(
                "26W006701-1-" + source,
                SpecialWoolContracts.BuildPrefixedTargetFilename(
                    "26W006701-1", source));
            bool rejected = false;
            try
            {
                SpecialWoolContracts.BuildPrefixedTargetFilename(
                    "26W006701-1", @"..\outside.xls");
            }
            catch (ArgumentException)
            {
                rejected = true;
            }
            True(rejected);
        }

        private static void ReviewMainIdBindingRejectsReplacement()
        {
            string expected = Redact.HashId("main-original");
            True(SpecialWoolContracts.MatchesRedactedId(expected, "main-original"));
            True(!SpecialWoolContracts.MatchesRedactedId(expected, "main-replacement"));
            True(!SpecialWoolContracts.MatchesRedactedId("main-original", "main-original"));
        }

        private static void InspectorMustMatchLoginStaff()
        {
            True(SpecialWoolContracts.MatchesLoginInspector(
                "李舒洋", "staff-lisy", " 李舒洋 ", "staff-lisy"));
            True(!SpecialWoolContracts.MatchesLoginInspector(
                "李舒洋", "staff-other", "李舒洋", "staff-lisy"));
            True(!SpecialWoolContracts.MatchesLoginInspector(
                "其他人", "staff-lisy", "李舒洋", "staff-lisy"));
        }

        private static void ProjectCheckCountMustRemainOne()
        {
            True(SpecialWoolContracts.IsExpectedSingleCheckCount(1m, 1L));
            True(!SpecialWoolContracts.IsExpectedSingleCheckCount(2m, 1L));
            True(!SpecialWoolContracts.IsExpectedSingleCheckCount(1m, 2L));
            True(!SpecialWoolContracts.IsExpectedSingleCheckCount(null, 1L));
        }

        private static void LegacyOriginalPathAcceptsExactValue()
        {
            True(SpecialWoolContracts.MatchesLegacyOriginalDataFileName(
                @"C:\records\260111037.xls",
                @"C:\records\260111037.xls"));
        }

        private static void LegacyOriginalPathLengthBoundaries()
        {
            foreach (int length in new[] { 99, 100 })
            {
                string value = new string('a', length);
                True(SpecialWoolContracts.MatchesLegacyOriginalDataFileName(
                    value,
                    value));
            }
            string overLimit = new string('b', 101);
            True(SpecialWoolContracts.MatchesLegacyOriginalDataFileName(
                overLimit.Substring(0, 100),
                overLimit));
            True(!SpecialWoolContracts.MatchesLegacyOriginalDataFileName(
                overLimit.Substring(0, 99),
                overLimit));
        }

        private static void LegacyOriginalPathAcceptsOnlyDeterministicTruncation()
        {
            string expected = @"C:\staging\" + new string('a', 130) + ".xls";
            string truncated = expected.Substring(
                0,
                SpecialWoolContracts.LegacyOriginalDataFileNameMaxLength);
            True(SpecialWoolContracts.MatchesLegacyOriginalDataFileName(
                truncated,
                expected));
            True(!SpecialWoolContracts.MatchesLegacyOriginalDataFileName(
                truncated.Substring(1),
                expected));
            True(!SpecialWoolContracts.MatchesLegacyOriginalDataFileName(
                truncated.Substring(0, truncated.Length - 1) + "x",
                expected));
            True(!SpecialWoolContracts.MatchesLegacyOriginalDataFileName(
                string.Empty,
                expected));
        }

        private static void TargetOriginalDataFilenameRejectsStagingPath()
        {
            const string target = "260111037-1";
            string targetFilename = SpecialWoolContracts.BuildTargetFilename(target);
            True(SpecialWoolContracts.MatchesTargetOriginalDataFileName(
                targetFilename,
                target));
            True(!SpecialWoolContracts.MatchesTargetOriginalDataFileName(
                @"C:\execution-staging\260111037-39-8B-纤维形状截面定量试验-2026.xls",
                target));
        }

        private static void ServerFileExactBytesAreAccepted()
        {
            byte[] value = { 1, 2, 3, 4 };
            LegacyXlsFileVerification result =
                LegacyXlsFileVerifier.Verify(value, (byte[])value.Clone());
            True(result.Verified);
            Equal("exact_sha256", result.Mode);
            Equal(result.SourceSha256, result.RemoteSha256);
            Equal(0, result.ChangedRecordCount);
            Equal(0, result.ChangedRecordIds.Count);
        }

        private static void BiffAcceptsOnlyWriteAccessChanges()
        {
            byte[] source = Biff(
                Record(0x0809, new byte[] { 1, 2 }),
                Record(0x005C, new byte[] { 3, 4, 5 }),
                Record(0x000A, new byte[0]));
            byte[] allowed = Biff(
                Record(0x0809, new byte[] { 1, 2 }),
                Record(0x005C, new byte[] { 9, 8, 7 }),
                Record(0x000A, new byte[0]));
            int changed;
            string error;
            True(LegacyXlsFileVerifier.BiffChangesAreWriteAccessOnly(
                source,
                allowed,
                out changed,
                out error));
            Equal(1, changed);
            Equal(null, error);

            byte[] forbidden = Biff(
                Record(0x0809, new byte[] { 1, 9 }),
                Record(0x005C, new byte[] { 9, 8, 7 }),
                Record(0x000A, new byte[0]));
            True(!LegacyXlsFileVerifier.BiffChangesAreWriteAccessOnly(
                source,
                forbidden,
                out changed,
                out error));
            Equal("biff_record_content_mismatch", error);

            byte[] wrongBoundary = Biff(
                Record(0x0809, new byte[] { 1, 2 }),
                Record(0x0042, new byte[] { 3, 4, 5 }),
                Record(0x000A, new byte[0]));
            True(!LegacyXlsFileVerifier.BiffChangesAreWriteAccessOnly(
                source,
                wrongBoundary,
                out changed,
                out error));
            Equal("biff_record_boundary_mismatch", error);
        }

        private static byte[] Record(ushort id, byte[] body)
        {
            var result = new byte[4 + body.Length];
            result[0] = (byte)(id & 0xFF);
            result[1] = (byte)(id >> 8);
            result[2] = (byte)(body.Length & 0xFF);
            result[3] = (byte)(body.Length >> 8);
            Buffer.BlockCopy(body, 0, result, 4, body.Length);
            return result;
        }

        private static byte[] Biff(params byte[][] records)
        {
            int length = 0;
            foreach (byte[] record in records) length += record.Length;
            var result = new byte[length];
            int offset = 0;
            foreach (byte[] record in records)
            {
                Buffer.BlockCopy(record, 0, result, offset, record.Length);
                offset += record.Length;
            }
            return result;
        }

        private static DataTable MainTable()
        {
            var table = new DataTable();
            table.Columns.Add("ID", typeof(string));
            table.Columns.Add("SampleNo", typeof(string));
            table.Columns.Add("ReviewUser", typeof(string));
            table.Columns.Add("ReviewTime", typeof(DateTime));
            table.Columns.Add("FileType", typeof(string));
            return table;
        }

        private static DataTable ChildTable()
        {
            var table = new DataTable();
            table.Columns.Add("ID", typeof(string));
            table.Columns.Add("SpecialWoolManageID", typeof(string));
            table.Columns.Add("CheckItemID", typeof(string));
            table.Columns.Add("PictureFileName", typeof(string));
            return table;
        }

        private static void Run(string name, Action test)
        {
            try
            {
                test();
                passed++;
                Console.WriteLine("PASS " + name);
            }
            catch (Exception ex)
            {
                failures++;
                Console.Error.WriteLine("FAIL " + name + ": " + ex.Message);
            }
        }

        private static void True(bool value)
        {
            if (!value) throw new Exception("expected true");
        }

        private static void Equal(object expected, object actual)
        {
            if (!object.Equals(expected, actual))
                throw new Exception("expected=" + expected + " actual=" + actual);
        }

        private static void NotEqual(object left, object right)
        {
            if (object.Equals(left, right))
                throw new Exception("values unexpectedly equal: " + left);
        }

        private static void Contains(string[] values, string expected)
        {
            if (Array.IndexOf(values, expected) < 0)
                throw new Exception("missing " + expected);
        }

        private static void NotContains(string[] values, string expected)
        {
            if (Array.IndexOf(values, expected) >= 0)
                throw new Exception("unexpected " + expected);
        }
    }
}
