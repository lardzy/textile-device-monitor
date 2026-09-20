using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Text.RegularExpressions;
using LegacyFibreCheckRunner;
using Toone.FibreCheck.Base.BaseDAL.DbEntity;
using Toone.FibreCheck.OriRecord.CurrencyItem;

namespace LegacyFibreCheckFinalEntryWriter
{
    internal static class GenericRecordUpdate
    {
        internal static readonly Dictionary<string, string> HeaderFields = new Dictionary<string, string> {
            { "unit", "Unit" }, { "sample_identity", "SampleDescription" }, { "remark", "Remark" },
            { "judge_basis", "JudgeBasis" }, { "judgement", "TotalJudge" },
        };
        internal static readonly Dictionary<string, string> DetailFields = new Dictionary<string, string> {
            { "result_value", "RealValue" }, { "standard_value", "StandardValue" },
        };
        internal static readonly Dictionary<string, string> ProjectionFields = new Dictionary<string, string> {
            { "result_value", "CheckResult" }, { "standard_value", "StandardValue" }, { "unit", "MeasureUnit" },
            { "sample_identity", "SampleIdentity" }, { "remark", "Remark" },
            { "judge_basis", "JudgeBasis" }, { "judgement", "Judgement" },
        };

        internal static Dictionary<string, object> Map(object value)
        {
            var map = value as Dictionary<string, object>;
            if (map == null) throw new PackageValidationException("update_object_required");
            return map;
        }

        internal static string Text(Dictionary<string, object> map, string key)
        {
            object value;
            if (!map.TryGetValue(key, out value) || value == null) return "";
            if (!(value is string)) throw new PackageValidationException("update_text_required");
            return (string)value;
        }

        private static object[] Rows(Dictionary<string, object> map, string key)
        {
            object value;
            if (!map.TryGetValue(key, out value) || !(value is IEnumerable) || value is string)
                throw new PackageValidationException("update_rows_required");
            return ((IEnumerable)value).Cast<object>().ToArray();
        }

        internal static FinalEntryPackage Load(Dictionary<string, object> root)
        {
            if (root.Count != (root.ContainsKey("resume_from") ? 7 : 6)
                || Text(root, "operation_type") != FinalEntryPackage.UpdateOperation
                || !Regex.IsMatch(Text(root, "sample_number"), @"^[0-9A-Z]{9,20}(?:-[0-9A-Z]{1,8})?$"))
                throw new PackageValidationException("update_package_invalid");
            var before = Map(root["before"]); var changes = Map(root["changes"]);
            var header = Map(before["generic_record"]); var register = Map(before["register"]);
            if (Text(before, "record_kind") != "generic"
                || Text(before, "record_ref") != "check-record:" + Text(register, "ID")
                || !Regex.IsMatch(Text(before, "record_ref"), @"^check-record:sha256:[0-9a-f]{16}$")
                || Rows(before, "details").Length != 1 || Rows(before, "key_results").Length != 1
                || Rows(before, "list_data").Length != 0 || Rows(before, "other_data").Length != 0
                || Rows(before, "association_issues").Length != 0
                || header["AttachInfo"] != null || Text(header, "StandardType") != ""
                || Text(header, "SampleNo") != Text(root, "sample_number")
                || Text(register, "OriginalRecordID") != Text(header, "ID")
                || Text(header, "CheckRecordRegisterID") != Text(register, "ID")
                || Text(header, "TestMethod") != FinalEntryPackage.PaperCheckMethod)
                throw new PackageValidationException("update_record_scope_unsupported");
            var detail = Map(Rows(before, "details")[0]);
            if (Text(detail, "RealLocation") != "" || Text(detail, "StandardLocation") != "" || changes.Count == 0)
                throw new PackageValidationException("update_detail_scope_unsupported");
            foreach (var change in changes)
                if ((!HeaderFields.ContainsKey(change.Key) && !DetailFields.ContainsKey(change.Key))
                    || !(change.Value is string) || ((string)change.Value).Length > 4000
                    || ((string)change.Value).Contains("'") || ((string)change.Value).Contains("\0"))
                    throw new PackageValidationException("update_change_invalid");
            var package = new FinalEntryPackage {
                SchemaVersion = 3, OperationType = FinalEntryPackage.UpdateOperation,
                SampleNumber = Text(root, "sample_number"), CheckItemNo = FinalEntryPackage.PaperCheckItemNo,
                CheckItemName = FinalEntryPackage.PaperCheckItemName,
                UpdateBefore = before, UpdateChanges = changes,
                UpdateResumeFrom = root.ContainsKey("resume_from") ? Map(root["resume_from"]) : null,
                GenericRecord = new GenericRecordPayload {
                    Header = new GenericHeader {
                        TestMethod = Text(header, "TestMethod"),
                        SampleDescription = Desired(before, changes, "sample_identity"),
                        TotalJudge = Desired(before, changes, "judgement"),
                    },
                },
            };
            package.TaskProject = FinalEntryPackage.ParseTaskProject(Map(root["task_project"]), package);
            if (package.UpdateResumeFrom != null) VerifyResult(package, package.UpdateResumeFrom, false);
            if (Text(header, "CheckItemID") != package.TaskProject.CheckItemId
                || Text(register, "CheckItemID") != package.TaskProject.CheckItemId
                || string.IsNullOrWhiteSpace(Desired(before, changes, "result_value")))
                throw new PackageValidationException("update_project_or_result_invalid");
            return package;
        }

        private static string Desired(Dictionary<string, object> before, Dictionary<string, object> changes, string key)
        {
            if (changes.ContainsKey(key)) return Text(changes, key);
            return HeaderFields.ContainsKey(key) ? Text(Map(before["generic_record"]), HeaderFields[key])
                : Text(Map(Rows(before, "details")[0]), DetailFields[key]);
        }

        internal static int Run(CommandLine options, FinalEntryPackage package, string connection,
            LegacyLoginFlow.StaffContext staff, ReceiptEmitter emit, Func<bool> awaitPermit)
        {
            var scope = ReadOnlyResolver.Resolve(connection, package);
            if (options.Execute) {
                emit.Stage("update_ready", null);
                if (awaitPermit == null || !awaitPermit())
                    throw new WriterFailureException("side_effect_permit_denied", Program.ExitPermitDenied, false);
                emit.Stage("update_started", null);
            }
            using (var projectLock = options.Execute
                ? ProjectWriteLock.Acquire(connection, scope.TaskId, scope.CheckItemId) : null)
            {
                if (options.Execute) scope = ReadOnlyResolver.Resolve(connection, package);
                // The first correction capability covers the Chinese, one-line paper form.
                // Other report layouts need their own projection mapping, not guessed text.
                if (scope.ReportLanguage != "Cn") throw new PackageValidationException("update_report_language_unsupported");
                LegacySafetyGuards.Verify(connection, package, scope, staff);
                var current = CheckRecordObservation.Read(connection, package.SampleNumber, Text(package.UpdateBefore, "record_ref"));
                if (!CheckRecordObservation.Equal(current.Record, package.UpdateResumeFrom ?? package.UpdateBefore))
                    throw new PackageValidationException("update_record_changed");
                emit.Stage("remote_state_verified", null);
                if (!options.Execute) {
                    emit.Stage("dry_run_completed", null);
                    return emit.Finish(0, null, false, package);
                }
                // Read once after permit delivery and under the project lock. The
                // observation is optimistic; legacy desktop clients do not share this lock.
                try
                {
                    var h = Map(current.Record["generic_record"]);
                    var record = new CurrencyItemRecordEntity {
                        ID = current.GenericId, CheckRecordRegisterID = current.RegisterId,
                        SampleNo = package.SampleNumber, CheckItemID = scope.CheckItemId,
                        CheckItemName = Text(h, "CheckItemName"), TestMethod = Text(h, "TestMethod"),
                        Grade = Text(h, "Grade"), StandardType = Text(h, "StandardType"),
                        ReportCheckItemName = Text(h, "ReportCheckItemName"), AttachInfo = "",
                        Unit = Desired(current.Record, package.UpdateChanges, "unit"),
                        SampleDescription = Desired(current.Record, package.UpdateChanges, "sample_identity"),
                        Remark = Desired(current.Record, package.UpdateChanges, "remark"),
                        JudgeBasis = Desired(current.Record, package.UpdateChanges, "judge_basis"),
                        TotalJudge = Desired(current.Record, package.UpdateChanges, "judgement"),
                    };
                    var details = new List<CurrencyItemRecordDetailEntity> {
                        new CurrencyItemRecordDetailEntity {
                            SeqNum = 1, CurrencyItemRecordNewID = current.GenericId, RealLocation = "", StandardLocation = "",
                            RealValue = Desired(current.Record, package.UpdateChanges, "result_value"),
                            StandardValue = Desired(current.Record, package.UpdateChanges, "standard_value"),
                        },
                    };
                    if (package.UpdateResumeFrom == null) {
                        var saved = new CurrencyItemRecordDAL().Save(record, details, new List<CurrencyItemRecordDetailEntity>(),
                            Toone.FibreCheck.BasicSetup.BasicSetupDAL.BaseData.Laboratory_PY, false);
                        if (saved == null || !saved.Result) throw new InvalidOperationException();
                    }
                    new CurrencyItemGenerateReportDataService().UpdateOriginalData(record, details);
                    var observed = CheckRecordObservation.Read(connection, package.SampleNumber, Text(package.UpdateBefore, "record_ref"));
                    VerifyResult(package, observed.Record);
                    package.UpdatedRecord = observed.Record;
                    emit.Stage("update_verified", null);
                    emit.Stage("completed", null);
                    return emit.Finish(0, null, false, package);
                }
                catch (Exception) {
                    throw new WriterFailureException("update_requires_readback", Program.ExitReconciliationRequired, true);
                }
            }
        }

        internal static void VerifyResult(FinalEntryPackage package, Dictionary<string, object> record, bool checkProjection = true)
        {
            if (Rows(record, "details").Length != 1 || (checkProjection && Rows(record, "key_results").Length != 1))
                throw new PackageValidationException("update_readback_count_mismatch");
            var projection = checkProjection ? Map(Rows(record, "key_results")[0]) : null;
            foreach (var field in ProjectionFields)
            {
                string expected = Desired(package.UpdateBefore, package.UpdateChanges, field.Key);
                if (Desired(record, new Dictionary<string, object>(), field.Key) != expected
                    || (checkProjection && Text(projection, field.Value) != expected))
                    throw new PackageValidationException("update_readback_value_mismatch");
            }
            if (Text(record, "record_ref") != Text(package.UpdateBefore, "record_ref")
                || Text(Map(record["generic_record"]), "ID") != Text(Map(package.UpdateBefore["generic_record"]), "ID"))
                throw new PackageValidationException("update_readback_identity_mismatch");
        }
    }
}
