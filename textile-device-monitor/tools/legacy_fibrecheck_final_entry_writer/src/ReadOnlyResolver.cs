using System;
using System.Collections.Generic;
using System.Data;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using LegacyFibreCheckRunner;

namespace LegacyFibreCheckFinalEntryWriter
{
    internal sealed class PreflightSnapshot
    {
        // Raw legacy identifiers remain process-internal.  Only TaskProject's
        // one-way sha256 identifiers may be placed in a receipt.
        public string TaskId;
        public string CheckItemId;
        public string CheckItemPositionId;
        public string OriginalDataInputUiClassName;
        public string TaskCheckBasis;
        public string TaskCheckMethod;
        public string TaskSampleIdentify;
        public int GiveJudgement;
        public string DelegateOrgName;
        public string ReportLanguage;
        public string SampleCategory;
        public DateTime? SampleReceiveTime;
        public int ExpectedResultCount;
        public int ExistingRegisterCount;
        public int ExistingGenericRecordCount;
        public int ExistingFileReferenceCount;
        public int ExistingProofedCount;
        public int ExistingKeyLinkedRecordCount;
        public int ExistingKeyResultCount;
        public string ExistingKeyIdentitySha256;
        public TaskProjectPayload TaskProject;
        public readonly List<string> ExistingExcelRecordIds = new List<string>();
        public readonly List<string> ExistingKeyIdentities = new List<string>();
        public readonly Dictionary<string, string> ExistingExcelRecordTemplates =
            new Dictionary<string, string>(StringComparer.Ordinal);
        public readonly Dictionary<string, string> ExistingKeyIdentityByRecordId =
            new Dictionary<string, string>(StringComparer.Ordinal);
        public string TemplateDocumentId;
        public string MappingDataTableName;
        public string MappingConfigSha256;
        public int MappingConfigCount;
        public bool MappingDataTableExists;
        public DateTime ServerDate;
        public string FileServer;
        public string OriginalDataRoot;
    }

    internal static class ReadOnlyResolver
    {
        private const string CurrencyUiClass =
            "Toone.FibreCheck.OriRecord.CurrencyItem.CurrencyItemRecordUI";

        private const string ResolveItemSql =
            "SELECT t.ID \"TaskID\", t.\"CheckBasis\" \"TaskCheckBasis\", " +
            "t.\"DelegateOrgName\" \"DelegateOrgName\", t.\"SampleReceiveTime\" \"SampleReceiveTime\", " +
            "tci.ID \"TaskCheckItemID\", " +
            "tci.\"CheckItemID\" \"TaskCheckItemCatalogID\", " +
            "tci.\"CheckItemNo\" \"TaskCheckItemNo\", " +
            "tci.\"CheckItemName\" \"TaskCheckItemName\", " +
            "tci.\"CheckMethod\" \"TaskCheckMethod\", " +
            "tci.\"SampleIdentify\" \"TaskSampleIdentify\", " +
            "tci.\"CheckCount\" \"CheckCount\", " +
            "tci.\"SeqNum\" \"TaskSeqNum\", " +
            "tci.\"GiveJudgement\" \"GiveJudgement\", " +
            "ci.ID \"CheckItemID\", ci.\"PositionID\" \"CheckItemPositionID\", " +
            "ci.\"No\" \"CatalogCheckItemNo\", ci.\"ItemName\" \"CatalogCheckItemName\", " +
            "ci.\"OriginalDataInputUIClassName\" \"OriginalDataInputUIClassName\" " +
            "FROM \"Task\" t " +
            "JOIN \"Task_CheckItem\" tci ON t.ID=tci.\"TaskID\" " +
            "JOIN \"CheckItem\" ci ON tci.\"CheckItemID\"=ci.ID " +
            "WHERE t.\"ReportNo\"=:sample_no " +
            "AND tci.\"CheckItemNo\"=:item_no AND tci.\"CheckItemName\"=:item_name " +
            "AND ci.\"No\"=:item_no AND ci.\"ItemName\"=:item_name";

        private const string ReportContextSql =
            "SELECT NVL((SELECT x.\"ReportLanguage\" FROM \"Task_PrintSetting\" x " +
            "WHERE x.\"TaskID\"=t.ID AND ROWNUM=1), 'Cn') \"ReportLanguage\", " +
            "(SELECT s.\"SampleCategory\" FROM \"Task_Sample\" s " +
            "WHERE s.\"TaskID\"=t.ID AND ROWNUM=1) \"SampleCategory\" " +
            "FROM \"Task\" t WHERE t.ID=:task_id";

        private const string RegisterCountSql =
            "SELECT COUNT(*) FROM \"CheckRecordRegister\" " +
            "WHERE \"SampleNo\"=:sample_no AND \"CheckItemID\"=:item_id";

        private const string GenericRecordCountSql =
            "SELECT COUNT(*) FROM \"CurrencyItemRecordNew\" " +
            "WHERE \"SampleNo\"=:sample_no AND \"CheckItemID\"=:item_id";

        private const string GenericBridgeCountSql =
            "SELECT COUNT(*) FROM \"CurrencyItemRecordNew\" cir " +
            "JOIN \"CheckRecordRegister\" crr " +
            "ON crr.ID=cir.\"CheckRecordRegisterID\" " +
            "AND crr.\"OriginalRecordID\"=cir.ID " +
            "WHERE cir.\"SampleNo\"=:sample_no AND cir.\"CheckItemID\"=:item_id " +
            "AND crr.\"SampleNo\"=:sample_no AND crr.\"CheckItemID\"=:item_id";

        private const string GenericKeyLinkedCountSql =
            "SELECT COUNT(DISTINCT cir.ID) FROM \"CurrencyItemRecordNew\" cir " +
            "JOIN \"OriginalKeyData_CheckItem\" okd " +
            "ON okd.\"OriginalRecordID\"=cir.ID AND okd.\"CheckItemID\"=cir.\"CheckItemID\" " +
            "AND okd.\"SampleNo\"=cir.\"SampleNo\" " +
            "WHERE cir.\"SampleNo\"=:sample_no AND cir.\"CheckItemID\"=:item_id";

        private const string FileReferenceCountSql =
            "SELECT COUNT(*) FROM \"CheckRecordRegister\" " +
            "WHERE \"SampleNo\"=:sample_no AND \"CheckItemID\"=:item_id " +
            "AND \"OriginalDataFilename\" IS NOT NULL";

        private const string ProofedCountSql =
            "SELECT COUNT(*) FROM \"CheckRecordRegister\" " +
            "WHERE \"SampleNo\"=:sample_no AND \"CheckItemID\"=:item_id " +
            "AND \"ProofTime\" IS NOT NULL AND \"ProofUser\" IS NOT NULL";

        private const string ExcelRegisterStateSql =
            "SELECT ID,\"TemplateFilename\" \"TemplateFilename\" " +
            "FROM \"CheckRecordRegister\" " +
            "WHERE \"SampleNo\"=:sample_no AND \"CheckItemID\"=:item_id " +
            "ORDER BY ID";

        private const string ExcelKeyLinkedCountSql =
            "SELECT COUNT(DISTINCT crr.ID) FROM \"CheckRecordRegister\" crr " +
            "JOIN \"OriginalKeyData_CheckItem\" okd " +
            "ON okd.\"OriginalRecordID\"=crr.ID AND okd.\"CheckItemID\"=crr.\"CheckItemID\" " +
            "AND okd.\"SampleNo\"=crr.\"SampleNo\" " +
            "WHERE crr.\"SampleNo\"=:sample_no AND crr.\"CheckItemID\"=:item_id";

        private const string DuplicateIdentityCountSql =
            "SELECT COUNT(*) FROM \"CheckRecordRegister\" crr " +
            "JOIN \"OriginalKeyData_CheckItem\" okd " +
            "ON okd.\"OriginalRecordID\"=crr.ID AND okd.\"CheckItemID\"=crr.\"CheckItemID\" " +
            "AND okd.\"SampleNo\"=crr.\"SampleNo\" " +
            "WHERE crr.\"SampleNo\"=:sample_no AND crr.\"CheckItemID\"=:item_id " +
            "AND okd.\"SampleIdentity\"=:sample_identity";

        private const string ExcelKeyIdentitySql =
            "SELECT okd.\"OriginalRecordID\" \"OriginalRecordID\", " +
            "okd.\"SampleIdentity\" \"SampleIdentity\",okd.\"SeqNum\" \"SeqNum\", " +
            "okd.\"CheckItemName\" \"CheckItemName\", " +
            "okd.\"ExcelTemplateName\" \"ExcelTemplateName\" " +
            "FROM \"OriginalKeyData_CheckItem\" okd " +
            "WHERE okd.\"SampleNo\"=:sample_no AND okd.\"CheckItemID\"=:item_id " +
            "ORDER BY okd.\"SampleIdentity\",okd.\"SeqNum\", " +
            "okd.\"CheckItemName\",okd.\"OriginalRecordID\"";

        private const string ExcelKeyScopeMismatchCountSql =
            "SELECT COUNT(*) FROM \"OriginalKeyData_CheckItem\" okd " +
            "WHERE EXISTS (SELECT 1 FROM \"CheckRecordRegister\" crr " +
            "WHERE crr.\"SampleNo\"=:sample_no AND crr.\"CheckItemID\"=:item_id " +
            "AND crr.ID=okd.\"OriginalRecordID\") AND " +
            "(okd.\"SampleNo\" IS NULL OR okd.\"SampleNo\"<>:sample_no OR " +
            "okd.\"CheckItemID\" IS NULL OR okd.\"CheckItemID\"<>:item_id)";

        private const string TemplateSql =
            "SELECT d.ID \"DocumentID\" FROM \"StandardDocument\" sd " +
            "JOIN \"Document\" d ON sd.\"DocumentID\"=d.ID " +
            "WHERE sd.\"StandardID\"=:item_id AND d.\"DocumentName\"=:template_name";

        private const string MappingSql =
            "SELECT \"DataTableName\" FROM \"OriginalKeyDataTableMapping\" " +
            "WHERE \"CheckItemID\"=:item_id AND \"ExcelTemplateName\"=:template_name";

        private const string ConfigSql =
            "SELECT \"KeyDataField\",\"KeyDataType\",\"ConfigValue\",\"ConfigValue_En\", " +
            "\"ConfigValue_CnEn\",\"ConfigValue_NewCnEn\",\"SeqNum\" " +
            "FROM \"OriginalKeyDataConfig\" WHERE \"CheckItemTable\"=:table_name " +
            "ORDER BY \"SeqNum\",\"KeyDataField\"";

        private const string TableExistsSql =
            "SELECT COUNT(*) FROM USER_TABLES WHERE TABLE_NAME=:table_name";

        private static readonly Regex OracleIdentifier =
            new Regex(@"^[A-Z][A-Z0-9_$#]{0,29}$", RegexOptions.CultureInvariant);

        public static PreflightSnapshot Resolve(string connectionString, FinalEntryPackage package)
        {
            if (package.SchemaVersion == 2)
            {
                package.MeasuredTaskProject = null;
            }
            var snapshot = new PreflightSnapshot();
            using (ILegacyDb db = new OdpNetDb(connectionString))
            {
                db.OpenReadOnly();
                try
                {
                    using (DataTable item = db.Query(ResolveItemSql, new List<DbParam>
                    {
                        new DbParam("sample_no", package.SampleNumber),
                        new DbParam("item_no", package.CheckItemNo),
                        new DbParam("item_name", package.CheckItemName),
                    }))
                    {
                        if (item.Rows.Count != 1)
                        {
                            throw new PackageValidationException(
                                item.Rows.Count == 0 ? "task_check_item_not_found" : "task_check_item_not_unique");
                        }
                        DataRow row = item.Rows[0];
                        snapshot.TaskId = Text(row, "TaskID");
                        snapshot.CheckItemId = Text(row, "CheckItemID");
                        snapshot.CheckItemPositionId = Text(row, "CheckItemPositionID");
                        snapshot.OriginalDataInputUiClassName = Text(row, "OriginalDataInputUIClassName");
                        snapshot.TaskCheckBasis = Text(row, "TaskCheckBasis");
                        snapshot.TaskCheckMethod = Text(row, "TaskCheckMethod");
                        snapshot.TaskSampleIdentify = Text(
                            row, "TaskSampleIdentify");
                        snapshot.DelegateOrgName = Text(row, "DelegateOrgName");
                        snapshot.SampleReceiveTime = Date(row, "SampleReceiveTime");
                        snapshot.ExpectedResultCount = NonNegativeInt(row, "CheckCount");
                        snapshot.GiveJudgement = NonNegativeInt(row, "GiveJudgement");
                        if (package.SchemaVersion == 2)
                        {
                            snapshot.TaskProject = ResolveTaskProject(row);
                            VerifyTaskProject(package.TaskProject, snapshot.TaskProject);
                            package.MeasuredTaskProject = snapshot.TaskProject;
                        }
                    }

                    using (DataTable context = db.Query(ReportContextSql, new List<DbParam>
                    {
                        new DbParam("task_id", snapshot.TaskId),
                    }))
                    {
                        if (context.Rows.Count != 1)
                        {
                            throw new PackageValidationException("task_report_context_not_unique");
                        }
                        snapshot.ReportLanguage = Text(context.Rows[0], "ReportLanguage");
                        snapshot.SampleCategory = Text(context.Rows[0], "SampleCategory");
                    }

                    if (snapshot.ExpectedResultCount < 1)
                    {
                        throw new PackageValidationException("task_check_count_would_be_exceeded");
                    }
                    bool additionalRegistrationAllowed =
                        package.AllowsConfirmedSingleCopyAppend(
                            snapshot.ExpectedResultCount);
                    if (package.ControlledTestOverrideActive
                        && !additionalRegistrationAllowed)
                    {
                        throw new PackageValidationException(
                            "controlled_test_override_remote_scope_mismatch");
                    }
                    if (package.ExistingRecordDecision != null
                        && !additionalRegistrationAllowed)
                    {
                        throw new PackageValidationException(
                            "existing_record_decision_remote_scope_mismatch");
                    }
                    if (additionalRegistrationAllowed)
                    {
                        // Both single-copy exceptions are checksum-bound in the
                        // package. The live count is verified below before either
                        // contract is marked as applied.
                    }
                    else if (snapshot.ExpectedResultCount == 1
                        && package.ExpectedExistingRegisterCount > 0)
                    {
                        throw new PackageValidationException(
                            "existing_record_decision_required_for_single_copy");
                    }

                    if (package.OperationType == FinalEntryPackage.GenericOperation)
                    {
                        if (!string.Equals(snapshot.OriginalDataInputUiClassName, CurrencyUiClass, StringComparison.Ordinal))
                        {
                            throw new PackageValidationException("check_item_not_currency_ui_input");
                        }
                        snapshot.ExistingRegisterCount = Count(db, RegisterCountSql, package, snapshot, null);
                        snapshot.ExistingGenericRecordCount = Count(db, GenericRecordCountSql, package, snapshot, null);
                        if (snapshot.ExistingRegisterCount != package.ExpectedExistingRegisterCount
                            || snapshot.ExistingGenericRecordCount != package.ExpectedExistingRegisterCount)
                        {
                            throw new PackageValidationException("expected_existing_register_count_mismatch");
                        }
                        int bridgeCount = Count(db, GenericBridgeCountSql, package, snapshot, null);
                        snapshot.ExistingKeyLinkedRecordCount = Count(
                            db, GenericKeyLinkedCountSql, package, snapshot, null);
                        if (bridgeCount != snapshot.ExistingRegisterCount
                            || snapshot.ExistingKeyLinkedRecordCount != snapshot.ExistingRegisterCount)
                        {
                            throw new PackageValidationException("existing_generic_records_incomplete");
                        }
                        VerifyGenericSampleIdentity(package, snapshot);
                    }
                    else
                    {
                        ResolveExcel(db, package, snapshot);
                    }

                    snapshot.ServerDate = Convert.ToDateTime(db.Scalar(
                        LegacyLoginFlow.SysDateSql, new List<DbParam>()));
                    snapshot.FileServer = Convert.ToString(db.Scalar(
                        LegacyLoginFlow.KeyValueSql,
                        new List<DbParam> { new DbParam("infokey", "FileServer") })) ?? string.Empty;
                    snapshot.OriginalDataRoot = Convert.ToString(db.Scalar(
                        LegacyLoginFlow.FileDirectorySql,
                        new List<DbParam> { new DbParam("infokey", "OriginalData") })) ?? string.Empty;
                    if (package.OperationType == FinalEntryPackage.ExcelOperation
                        && (string.IsNullOrWhiteSpace(snapshot.FileServer)
                            || string.IsNullOrWhiteSpace(snapshot.OriginalDataRoot)))
                    {
                        throw new PackageValidationException("original_data_file_config_unavailable");
                    }
                    if (package.ControlledTestOverrideActive)
                    {
                        // Mark the override as applied only after the complete read-only
                        // project/count/template/key/file-configuration preflight passes.
                        // Failed preflight receipts must not imply that an exception was used.
                        package.ControlledTestOverrideApplied = true;
                    }
                    if (package.ExistingRecordDecision != null)
                    {
                        package.ExistingRecordDecisionApplied = true;
                    }
                }
                finally
                {
                    db.RollbackAndClose();
                }
            }
            return snapshot;
        }

        private static void VerifySelectedSampleIdentity(
            string selected, PreflightSnapshot snapshot)
        {
            var offered = new HashSet<string>(StringComparer.Ordinal);
            foreach (string part in Regex.Split(
                snapshot.TaskSampleIdentify ?? string.Empty, "[，,、]"))
            {
                string value = CompactText(part);
                if (!string.IsNullOrWhiteSpace(value))
                {
                    offered.Add(value);
                }
            }
            if ((offered.Count == 0 && !string.IsNullOrEmpty(selected))
                || (offered.Count > 0 && !offered.Contains(selected)))
            {
                throw new PackageValidationException(
                    "selected_sample_identity_not_available");
            }
        }

        private static void VerifyGenericSampleIdentity(
            FinalEntryPackage package, PreflightSnapshot snapshot)
        {
            VerifySelectedSampleIdentity(
                package.GenericRecord.Header.SampleDescription ?? string.Empty,
                snapshot);
        }

        private static void ResolveExcel(
            ILegacyDb db, FinalEntryPackage package, PreflightSnapshot snapshot)
        {
            bool templateSupported = package.SchemaVersion == 1
                ? package.ExcelRecord.TemplateName == "微观形貌.xls"
                : FinalEntryPackage.SupportedMicroscopyTemplates.ContainsKey(
                    package.ExcelRecord.TemplateName);
            if (package.CheckItemNo != "5103.5" || package.CheckItemName != "纤维微观形貌"
                || !string.IsNullOrWhiteSpace(snapshot.OriginalDataInputUiClassName)
                || !templateSupported)
            {
                throw new PackageValidationException("excel_route_not_supported_in_v1");
            }
            using (DataTable template = db.Query(TemplateSql, new List<DbParam>
            {
                new DbParam("item_id", snapshot.CheckItemId),
                new DbParam("template_name", package.ExcelRecord.TemplateName),
            }))
            {
                if (template.Rows.Count != 1)
                {
                    throw new PackageValidationException(
                        template.Rows.Count == 0 ? "excel_template_not_found" : "excel_template_not_unique");
                }
                snapshot.TemplateDocumentId = Text(template.Rows[0], "DocumentID");
            }

            // Concurrency/idempotency is scoped to the complete task project, not just
            // rows that happened to use the selected template. Counting only one
            // TemplateFilename could silently add a result beside an unexpected row
            // created with another configured template.
            var recordIds = new HashSet<string>(StringComparer.Ordinal);
            using (DataTable registers = db.Query(ExcelRegisterStateSql, new List<DbParam>
            {
                new DbParam("sample_no", package.SampleNumber),
                new DbParam("item_id", snapshot.CheckItemId),
            }))
            {
                snapshot.ExistingRegisterCount = registers.Rows.Count;
                foreach (DataRow row in registers.Rows)
                {
                    string recordId = Text(row, "ID");
                    string templateName = Text(row, "TemplateFilename");
                    bool supportedTemplate = package.SchemaVersion == 1
                        ? string.Equals(templateName,
                            package.ExcelRecord.TemplateName, StringComparison.Ordinal)
                        : FinalEntryPackage.SupportedMicroscopyTemplates.ContainsKey(
                            templateName);
                    if (string.IsNullOrWhiteSpace(recordId) || !recordIds.Add(recordId)
                        || !supportedTemplate)
                    {
                        throw new PackageValidationException(
                            "existing_excel_register_template_or_identity_invalid");
                    }
                    snapshot.ExistingExcelRecordIds.Add(recordId);
                    snapshot.ExistingExcelRecordTemplates.Add(recordId, templateName);
                }
            }
            if (snapshot.ExistingRegisterCount != package.ExpectedExistingRegisterCount)
            {
                throw new PackageValidationException("expected_existing_register_count_mismatch");
            }
            snapshot.ExistingFileReferenceCount = Count(
                db, FileReferenceCountSql, package, snapshot, null);
            snapshot.ExistingProofedCount = Count(
                db, ProofedCountSql, package, snapshot, null);
            if (snapshot.ExistingFileReferenceCount != snapshot.ExistingRegisterCount
                || snapshot.ExistingProofedCount != snapshot.ExistingRegisterCount)
            {
                throw new PackageValidationException("existing_excel_records_incomplete");
            }

            var identityCanonical = new StringBuilder();
            var existingIdentities = new HashSet<string>(StringComparer.Ordinal);
            var linkedRecordIds = new HashSet<string>(StringComparer.Ordinal);
            using (DataTable identities = db.Query(ExcelKeyIdentitySql, new List<DbParam>
            {
                new DbParam("sample_no", package.SampleNumber),
                new DbParam("item_id", snapshot.CheckItemId),
            }))
            {
                snapshot.ExistingKeyResultCount = identities.Rows.Count;
                foreach (DataRow row in identities.Rows)
                {
                    string recordId = Text(row, "OriginalRecordID");
                    string identity = Text(row, "SampleIdentity");
                    string registeredTemplate;
                    bool identityInvalid = package.SchemaVersion == 1
                        && identity != "纵面" && identity != "横截面";
                    bool identityDuplicate = package.SchemaVersion == 1
                        && !existingIdentities.Add(identity);
                    if (!recordIds.Contains(recordId) || !linkedRecordIds.Add(recordId)
                        || !snapshot.ExistingExcelRecordTemplates.TryGetValue(
                            recordId, out registeredTemplate)
                        || identityInvalid || identityDuplicate
                        || Text(row, "SeqNum") != "1"
                        || !string.Equals(Text(row, "CheckItemName"),
                            package.CheckItemName, StringComparison.Ordinal)
                        || !string.Equals(Text(row, "ExcelTemplateName"),
                            registeredTemplate, StringComparison.Ordinal))
                    {
                        throw new PackageValidationException(
                            "existing_excel_key_contract_invalid");
                    }
                    snapshot.ExistingKeyIdentities.Add(identity);
                    snapshot.ExistingKeyIdentityByRecordId.Add(recordId, identity);
                    AppendCanonical(identityCanonical, recordId);
                    AppendCanonical(identityCanonical, identity);
                    AppendCanonical(identityCanonical, Text(row, "SeqNum"));
                    AppendCanonical(identityCanonical, Text(row, "CheckItemName"));
                    AppendCanonical(identityCanonical, Text(row, "ExcelTemplateName"));
                }
            }
            if (snapshot.ExistingKeyResultCount != snapshot.ExistingRegisterCount)
            {
                throw new PackageValidationException("existing_excel_key_result_count_mismatch");
            }
            snapshot.ExistingKeyLinkedRecordCount = linkedRecordIds.Count;
            if (snapshot.ExistingKeyLinkedRecordCount != snapshot.ExistingRegisterCount
                || Convert.ToInt32(db.Scalar(
                    ExcelKeyScopeMismatchCountSql,
                    new List<DbParam>
                    {
                        new DbParam("sample_no", package.SampleNumber),
                        new DbParam("item_id", snapshot.CheckItemId),
                    })) != 0)
            {
                throw new PackageValidationException("existing_excel_key_scope_mismatch");
            }
            snapshot.ExistingKeyIdentitySha256 =
                FinalEntryPackage.Sha256Text(identityCanonical.ToString());

            foreach (string expectedIdentity in package.ExcelRecord.ExpectedKeyIdentities)
            {
                if (package.SchemaVersion == 1
                    && existingIdentities.Contains(expectedIdentity))
                {
                    throw new PackageValidationException("key_identity_already_exists");
                }
            }
            VerifySelectedSampleIdentity(
                package.ExcelRecord.ExpectedKeyIdentities[0], snapshot);

            // This explicit SELECT is a mandatory safety boundary.  The official
            // OriginalKeyDataConfigUtility inserts a mapping when it cannot find one.
            // No official BLL/service may be constructed before this unique-row check.
            using (DataTable mapping = db.Query(MappingSql, new List<DbParam>
            {
                new DbParam("item_id", snapshot.CheckItemId),
                new DbParam("template_name", package.ExcelRecord.TemplateName),
            }))
            {
                if (mapping.Rows.Count != 1)
                {
                    throw new PackageValidationException(
                        mapping.Rows.Count == 0 ? "original_data_mapping_missing" : "original_data_mapping_not_unique");
                }
                snapshot.MappingDataTableName = Convert.ToString(mapping.Rows[0]["DataTableName"]) ?? string.Empty;
            }
            if (!OracleIdentifier.IsMatch(snapshot.MappingDataTableName))
            {
                throw new PackageValidationException("original_data_mapping_table_name_invalid");
            }
            object tableCount = db.Scalar(TableExistsSql, new List<DbParam>
            {
                new DbParam("table_name", snapshot.MappingDataTableName),
            });
            int mappingTableCount = Convert.ToInt32(tableCount);
            if (mappingTableCount < 0 || mappingTableCount > 1)
            {
                throw new PackageValidationException("original_data_table_state_invalid");
            }
            snapshot.MappingDataTableExists = mappingTableCount == 1;

            var canonical = new StringBuilder();
            AppendCanonical(canonical, snapshot.MappingDataTableName);
            using (DataTable configs = db.Query(ConfigSql, new List<DbParam>
            {
                new DbParam("table_name", snapshot.MappingDataTableName),
            }))
            {
                if (configs.Rows.Count == 0)
                {
                    throw new PackageValidationException("original_data_config_missing");
                }
                snapshot.MappingConfigCount = configs.Rows.Count;
                var sequenceFields = new Dictionary<string, HashSet<string>>(
                    StringComparer.Ordinal);
                foreach (DataRow row in configs.Rows)
                {
                    string sequence = Text(row, "SeqNum");
                    string field = Text(row, "KeyDataField");
                    HashSet<string> fields;
                    if (!sequenceFields.TryGetValue(sequence, out fields))
                    {
                        fields = new HashSet<string>(StringComparer.Ordinal);
                        sequenceFields.Add(sequence, fields);
                    }
                    if (!fields.Add(field))
                    {
                        throw new PackageValidationException(
                            "original_data_config_duplicate_seq_field");
                    }
                    AppendCanonical(canonical, sequence);
                    AppendCanonical(canonical, field);
                    AppendCanonical(canonical, Text(row, "KeyDataType"));
                    AppendCanonical(canonical, Text(row, "ConfigValue"));
                    AppendCanonical(canonical, Text(row, "ConfigValue_En"));
                    AppendCanonical(canonical, Text(row, "ConfigValue_CnEn"));
                    AppendCanonical(canonical, Text(row, "ConfigValue_NewCnEn"));
                }
            }
            snapshot.MappingConfigSha256 = FinalEntryPackage.Sha256Text(canonical.ToString());
            if (!string.Equals(snapshot.MappingConfigSha256,
                package.ExcelRecord.ExpectedMappingConfigSha256, StringComparison.OrdinalIgnoreCase))
            {
                throw new PackageValidationException("mapping_config_sha256_mismatch");
            }
        }

        private static int Count(
            ILegacyDb db, string sql, FinalEntryPackage package,
            PreflightSnapshot snapshot, string templateName)
        {
            var parameters = new List<DbParam>
            {
                new DbParam("sample_no", package.SampleNumber),
                new DbParam("item_id", snapshot.CheckItemId),
            };
            if (templateName != null)
            {
                parameters.Add(new DbParam("template_name", templateName));
            }
            return Convert.ToInt32(db.Scalar(sql, parameters));
        }

        private static TaskProjectPayload ResolveTaskProject(DataRow row)
        {
            string rawTaskCheckItemId = Text(row, "TaskCheckItemID");
            string rawTaskCheckItemCatalogId = Text(
                row, "TaskCheckItemCatalogID");
            string rawCatalogCheckItemId = Text(row, "CheckItemID");
            string taskCheckItemNo = CompactText(Text(row, "TaskCheckItemNo"));
            string taskCheckItemName = CompactText(
                Text(row, "TaskCheckItemName"));
            string checkMethod = CompactText(Text(row, "TaskCheckMethod"));
            string catalogCheckItemNo = CompactText(
                Text(row, "CatalogCheckItemNo"));
            string catalogCheckItemName = CompactText(
                Text(row, "CatalogCheckItemName"));
            int seqNum = NonNegativeInt(row, "TaskSeqNum");
            int checkCount = NonNegativeInt(row, "CheckCount");
            if (string.IsNullOrWhiteSpace(rawTaskCheckItemId)
                || string.IsNullOrWhiteSpace(rawTaskCheckItemCatalogId)
                || !string.Equals(
                    rawTaskCheckItemCatalogId,
                    rawCatalogCheckItemId,
                    StringComparison.Ordinal)
                || !string.Equals(
                    taskCheckItemNo, catalogCheckItemNo, StringComparison.Ordinal)
                || !string.Equals(
                    taskCheckItemName, catalogCheckItemName, StringComparison.Ordinal))
            {
                throw new PackageValidationException(
                    "task_project_catalog_binding_changed");
            }

            string taskCheckItemId = Redact.HashId(rawTaskCheckItemId);
            string checkItemId = Redact.HashId(rawTaskCheckItemCatalogId);
            string identity = string.Join("\0", new string[]
            {
                CompactText(taskCheckItemId),
                CompactText(checkItemId),
                taskCheckItemNo,
                taskCheckItemName,
                checkMethod,
                CompactText(seqNum.ToString(CultureInfo.InvariantCulture)),
            });
            return new TaskProjectPayload
            {
                ProjectKey = "task-project:"
                    + FinalEntryPackage.Sha256Text(identity).Substring(0, 24),
                TaskCheckItemId = taskCheckItemId,
                CheckItemId = checkItemId,
                CheckItemNo = taskCheckItemNo,
                CheckItemName = taskCheckItemName,
                CheckMethod = checkMethod,
                SeqNum = seqNum,
                CheckCount = checkCount,
            };
        }

        private static void VerifyTaskProject(
            TaskProjectPayload expected, TaskProjectPayload actual)
        {
            if (expected == null || actual == null
                || !string.Equals(
                    actual.ProjectKey, expected.ProjectKey, StringComparison.Ordinal)
                || !string.Equals(
                    actual.TaskCheckItemId,
                    expected.TaskCheckItemId,
                    StringComparison.Ordinal)
                || !string.Equals(
                    actual.CheckItemId, expected.CheckItemId, StringComparison.Ordinal)
                || !string.Equals(
                    actual.CheckItemNo, expected.CheckItemNo, StringComparison.Ordinal)
                || !string.Equals(
                    actual.CheckItemName, expected.CheckItemName, StringComparison.Ordinal)
                || !string.Equals(
                    actual.CheckMethod, expected.CheckMethod, StringComparison.Ordinal)
                || actual.SeqNum != expected.SeqNum
                || actual.CheckCount != expected.CheckCount
                || actual.CheckCount < 1)
            {
                throw new PackageValidationException(
                    "task_project_binding_changed");
            }
        }

        private static string CompactText(string value)
        {
            return Regex.Replace((value ?? string.Empty).Trim(), @"\s+", " ");
        }

        private static void AppendCanonical(StringBuilder builder, string value)
        {
            value = value ?? string.Empty;
            builder.Append(value.Length).Append(':').Append(value).Append('|');
        }

        private static string Text(DataRow row, string column)
        {
            object value = row[column];
            return value == null || value == DBNull.Value ? string.Empty : Convert.ToString(value);
        }

        private static DateTime? Date(DataRow row, string column)
        {
            object value = row[column];
            return value == null || value == DBNull.Value ? (DateTime?)null : Convert.ToDateTime(value);
        }

        private static int NonNegativeInt(DataRow row, string column)
        {
            object value = row[column];
            int parsed;
            if (value == null || value == DBNull.Value
                || !int.TryParse(Convert.ToString(value), out parsed) || parsed < 0)
            {
                throw new PackageValidationException("task_check_count_invalid");
            }
            return parsed;
        }
    }
}
