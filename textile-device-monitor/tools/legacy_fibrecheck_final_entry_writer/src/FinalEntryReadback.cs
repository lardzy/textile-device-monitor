using System;
using System.Collections.Generic;
using System.Data;
using System.IO;
using System.Text.RegularExpressions;
using LegacyFibreCheckRunner;
using Toone.FibreCheck.Base.BaseDAL.DbEntity;
using Toone.FibreCheck.Entites.OrmEntites;

namespace LegacyFibreCheckFinalEntryWriter
{
    /// <summary>
    /// Post-write reconciliation.  Every query runs in an Oracle read-only transaction;
    /// a mismatch is terminal and forbids an automatic retry because the legacy writer
    /// may already have committed part (generic) or all (Excel) of the operation.
    /// </summary>
    internal static class FinalEntryReadback
    {
        private static readonly Regex OracleIdentifier =
            new Regex(@"^[A-Z][A-Z0-9_$#]{0,29}$", RegexOptions.CultureInvariant);

        private const string GenericHeaderSql =
            "SELECT cir.\"CheckRecordRegisterID\" \"RegisterID\", " +
            "cir.\"CheckItemName\" \"CheckItemName\",cir.\"CheckUser\" \"RecordCheckUser\", " +
            "cir.\"Grade\" \"Grade\",cir.\"Unit\" \"Unit\", " +
            "cir.\"JudgeBasis\" \"JudgeBasis\",cir.\"TestMethod\" \"TestMethod\", " +
            "cir.\"SampleDescription\" \"SampleDescription\", " +
            "cir.\"StandardType\" \"StandardType\", " +
            "cir.\"ReportCheckItemName\" \"ReportCheckItemName\", " +
            "cir.\"AttachInfo\" \"AttachInfo\",cir.\"Remark\" \"Remark\", " +
            "cir.\"TotalJudge\" \"TotalJudge\", " +
            "crr.\"OriginalRecordID\" \"RegisterOriginalRecordID\", " +
            "crr.\"CreateUser\" \"RegisterCreateUser\", " +
            "crr.\"CheckUser\" \"RegisterCheckUser\", " +
            "crr.\"PositionID\" \"RegisterPositionID\", " +
            "crr.\"SampleIdentity\" \"RegisterSampleIdentity\" " +
            "FROM \"CurrencyItemRecordNew\" cir " +
            "JOIN \"CheckRecordRegister\" crr ON crr.ID=cir.\"CheckRecordRegisterID\" " +
            "WHERE cir.ID=:record_id AND cir.\"SampleNo\"=:sample_no " +
            "AND cir.\"CheckItemID\"=:item_id " +
            "AND crr.\"SampleNo\"=:sample_no AND crr.\"CheckItemID\"=:item_id";

        private const string GenericDetailSql =
            "SELECT \"SeqNum\",\"StandardLocation\",\"StandardValue\", " +
            "\"RealLocation\",\"RealValue\" FROM \"CurrencyItemRecordNewDetail\" " +
            "WHERE \"CurrencyItemRecordNewID\"=:record_id ORDER BY \"SeqNum\",ID";

        private const string ExcelRegisterSql =
            "SELECT \"TemplateFilename\",\"OriginalDataFilename\",\"Level\", " +
            "\"CreateUser\",\"CreateTime\",\"LastUpdateTime\",\"LastUpdateUser\", " +
            "\"OriginalRecordID\",\"SampleIdentity\",\"CheckUser\",\"EquipmentNo\", " +
            "\"ProofUser\",\"ProofTime\",\"CheckBasis\" " +
            "FROM \"CheckRecordRegister\" WHERE ID=:record_id " +
            "AND \"SampleNo\"=:sample_no AND \"CheckItemID\"=:item_id";

        private const string ExcelProjectRegisterSql =
            "SELECT ID \"ID\",\"TemplateFilename\" \"TemplateFilename\" " +
            "FROM \"CheckRecordRegister\" " +
            "WHERE \"SampleNo\"=:sample_no AND \"CheckItemID\"=:item_id ORDER BY ID";

        private const string ExcelProjectKeySql =
            "SELECT \"OriginalRecordID\" \"OriginalRecordID\", " +
            "\"SampleIdentity\" \"SampleIdentity\",\"SeqNum\" \"SeqNum\", " +
            "\"CheckItemName\" \"CheckItemName\", " +
            "\"ExcelTemplateName\" \"ExcelTemplateName\" " +
            "FROM \"OriginalKeyData_CheckItem\" " +
            "WHERE \"SampleNo\"=:sample_no AND \"CheckItemID\"=:item_id " +
            "ORDER BY \"SampleIdentity\",\"SeqNum\",\"CheckItemName\",\"OriginalRecordID\"";

        private const string ExcelKeyScopeMismatchCountSql =
            "SELECT COUNT(*) FROM \"OriginalKeyData_CheckItem\" okd " +
            "WHERE EXISTS (SELECT 1 FROM \"CheckRecordRegister\" crr " +
            "WHERE crr.\"SampleNo\"=:sample_no AND crr.\"CheckItemID\"=:item_id " +
            "AND crr.ID=okd.\"OriginalRecordID\") AND " +
            "(okd.\"SampleNo\" IS NULL OR okd.\"SampleNo\"<>:sample_no OR " +
            "okd.\"CheckItemID\" IS NULL OR okd.\"CheckItemID\"<>:item_id)";

        internal static int VerifyGeneric(
            string connectionString, FinalEntryPackage package, PreflightSnapshot snapshot,
            LegacyLoginFlow.StaffContext staff, CurrencyItemRecordEntity record,
            IList<CurrencyItemRecordDetailEntity> details)
        {
            if (record == null || string.IsNullOrWhiteSpace(record.ID)
                || string.IsNullOrWhiteSpace(record.CheckRecordRegisterID))
            {
                throw Reconciliation("generic_readback_record_identity_missing");
            }
            using (ILegacyDb db = new OdpNetDb(connectionString))
            {
                db.OpenReadOnly();
                try
                {
                    List<DbParam> scope = Scope(package, snapshot, record.ID);
                    using (DataTable table = db.Query(GenericHeaderSql, scope))
                    {
                        if (table.Rows.Count != 1)
                        {
                            throw Reconciliation("generic_readback_header_not_unique");
                        }
                        DataRow row = table.Rows[0];
                        GenericHeader expected = package.GenericRecord.Header;
                        if (!Same(Text(row, "RegisterID"), record.CheckRecordRegisterID)
                            || !Same(Text(row, "CheckItemName"), package.CheckItemName)
                            || !Same(Text(row, "RecordCheckUser"), staff.Id)
                            || !Same(Text(row, "Grade"), expected.Grade)
                            || !Same(Text(row, "Unit"), expected.Unit)
                            || !Same(Text(row, "JudgeBasis"), expected.JudgeBasis)
                            || !Same(Text(row, "TestMethod"), expected.TestMethod)
                            || !Same(Text(row, "SampleDescription"), expected.SampleDescription)
                            || !Same(Text(row, "StandardType"), expected.StandardType)
                            || !Same(Text(row, "ReportCheckItemName"), expected.ReportCheckItemName)
                            || !Same(Text(row, "AttachInfo"), expected.AttachInfo)
                            || !Same(Text(row, "Remark"), expected.Remark)
                            || !Same(Text(row, "TotalJudge"), expected.TotalJudge)
                            || !Same(Text(row, "RegisterOriginalRecordID"), record.ID)
                            || !Same(Text(row, "RegisterCreateUser"), staff.Id)
                            || !Same(Text(row, "RegisterCheckUser"), staff.Id)
                            || !Same(Text(row, "RegisterSampleIdentity"), expected.SampleDescription)
                            || (!string.IsNullOrWhiteSpace(staff.PositionID)
                                && !Same(Text(row, "RegisterPositionID"), staff.PositionID)))
                        {
                            throw Reconciliation("generic_readback_header_mismatch");
                        }
                    }

                    using (DataTable table = db.Query(GenericDetailSql,
                        new List<DbParam> { new DbParam("record_id", record.ID) }))
                    {
                        if (table.Rows.Count != details.Count)
                        {
                            throw Reconciliation("generic_readback_detail_count_mismatch");
                        }
                        for (int index = 0; index < table.Rows.Count; index++)
                        {
                            DataRow row = table.Rows[index];
                            GenericDetail expected = package.GenericRecord.Details[index];
                            if (Convert.ToInt32(row["SeqNum"]) != index + 1
                                || !Same(Text(row, "StandardLocation"), expected.StandardLocation)
                                || !Same(Text(row, "StandardValue"), expected.StandardValue)
                                || !Same(Text(row, "RealLocation"), expected.RealLocation)
                                || !Same(Text(row, "RealValue"), expected.RealValue))
                            {
                                throw Reconciliation("generic_readback_detail_mismatch");
                            }
                        }
                    }

                    int expectedCount = package.ExpectedExistingRegisterCount + 1;
                    if (Count(db,
                            "SELECT COUNT(*) FROM \"CheckRecordRegister\" WHERE \"SampleNo\"=:sample_no AND \"CheckItemID\"=:item_id",
                            ProjectScope(package, snapshot)) != expectedCount
                        || Count(db,
                            "SELECT COUNT(*) FROM \"CurrencyItemRecordNew\" WHERE \"SampleNo\"=:sample_no AND \"CheckItemID\"=:item_id",
                            ProjectScope(package, snapshot)) != expectedCount)
                    {
                        throw Reconciliation("generic_readback_project_count_mismatch");
                    }
                    int projectionCount = Count(db,
                        "SELECT COUNT(*) FROM \"OriginalKeyData_CheckItem\" " +
                        "WHERE \"SampleNo\"=:sample_no AND \"CheckItemID\"=:item_id " +
                        "AND \"OriginalRecordID\"=:record_id", scope);
                    if (projectionCount < 1)
                    {
                        throw Reconciliation("generic_readback_projection_missing");
                    }
                    return projectionCount;
                }
                finally
                {
                    db.RollbackAndClose();
                }
            }
        }

        internal static void VerifyExcel(
            string connectionString, FinalEntryPackage package, PreflightSnapshot snapshot,
            LegacyLoginFlow.StaffContext staff, CheckRecordRegister record, string targetPath,
            int listCount, int otherCount, bool proofExpected)
        {
            VerifyWorkbook(targetPath, package.ExcelRecord.Workbook);
            if (record == null || string.IsNullOrWhiteSpace(record.ID))
            {
                throw Reconciliation("excel_readback_record_identity_missing");
            }
            using (ILegacyDb db = new OdpNetDb(connectionString))
            {
                db.OpenReadOnly();
                try
                {
                    List<DbParam> scope = Scope(package, snapshot, record.ID);
                    using (DataTable table = db.Query(ExcelRegisterSql, scope))
                    {
                        if (table.Rows.Count != 1)
                        {
                            throw Reconciliation("excel_readback_register_not_unique");
                        }
                        DataRow row = table.Rows[0];
                        ExcelRegisterFields expected = package.ExcelRecord.Register;
                        if (!Same(Text(row, "TemplateFilename"), package.ExcelRecord.TemplateName)
                            || !Same(Text(row, "OriginalDataFilename"), Path.GetFileName(targetPath))
                            || !Same(Text(row, "Level"), expected.Level)
                            || !Same(Text(row, "CreateUser"), staff.Id)
                            || !HasValue(row, "CreateTime") || !HasValue(row, "LastUpdateTime")
                            || !Same(Text(row, "OriginalRecordID"), string.Empty)
                            || !Same(Text(row, "SampleIdentity"), expected.SampleIdentity)
                            || !Same(Text(row, "CheckUser"), staff.Id)
                            || !Same(Text(row, "EquipmentNo"), expected.EquipmentNo)
                            || !Same(Text(row, "CheckBasis"), expected.CheckBasis))
                        {
                            throw Reconciliation("excel_readback_register_mismatch");
                        }
                        bool proofTimePresent = HasValue(row, "ProofTime");
                        if (proofExpected)
                        {
                            if (!proofTimePresent || !Same(Text(row, "ProofUser"), staff.Id)
                                || !Same(Text(row, "LastUpdateUser"), staff.Id))
                            {
                                throw Reconciliation("excel_readback_proof_mismatch");
                            }
                        }
                        else if (proofTimePresent || !Same(Text(row, "ProofUser"), string.Empty)
                            || !Same(Text(row, "LastUpdateUser"), string.Empty))
                        {
                            throw Reconciliation("excel_readback_unexpected_proof_state");
                        }
                    }

                    int expectedCount = package.ExpectedExistingRegisterCount + 1;
                    var allowedRecordIds = new HashSet<string>(
                        snapshot.ExistingExcelRecordIds, StringComparer.Ordinal);
                    if (allowedRecordIds.Count != package.ExpectedExistingRegisterCount
                        || !allowedRecordIds.Add(record.ID)
                        || allowedRecordIds.Count != expectedCount)
                    {
                        throw Reconciliation("excel_readback_register_identity_mismatch");
                    }
                    var expectedTemplatesByRecordId = new Dictionary<string, string>(
                        snapshot.ExistingExcelRecordTemplates, StringComparer.Ordinal);
                    if (expectedTemplatesByRecordId.Count
                            != package.ExpectedExistingRegisterCount
                        || expectedTemplatesByRecordId.ContainsKey(record.ID))
                    {
                        throw Reconciliation(
                            "excel_readback_existing_template_state_mismatch");
                    }
                    expectedTemplatesByRecordId.Add(
                        record.ID, package.ExcelRecord.TemplateName);
                    var remainingRecordIds = new HashSet<string>(
                        allowedRecordIds, StringComparer.Ordinal);
                    using (DataTable projectRegisters = db.Query(
                        ExcelProjectRegisterSql, ProjectScope(package, snapshot)))
                    {
                        if (projectRegisters.Rows.Count != expectedCount)
                        {
                            throw Reconciliation("excel_readback_project_count_mismatch");
                        }
                        foreach (DataRow row in projectRegisters.Rows)
                        {
                            string existingRecordId = Text(row, "ID");
                            string expectedTemplate;
                            if (!remainingRecordIds.Remove(existingRecordId)
                                || !expectedTemplatesByRecordId.TryGetValue(
                                    existingRecordId, out expectedTemplate)
                                || !Same(Text(row, "TemplateFilename"),
                                    expectedTemplate))
                            {
                                throw Reconciliation(
                                    "excel_readback_project_register_contract_mismatch");
                            }
                        }
                    }
                    if (remainingRecordIds.Count != 0
                        || Count(db,
                            "SELECT COUNT(*) FROM \"CheckRecordRegister\" WHERE \"SampleNo\"=:sample_no AND \"CheckItemID\"=:item_id AND \"OriginalDataFilename\" IS NOT NULL",
                            ProjectScope(package, snapshot)) != expectedCount)
                    {
                        throw Reconciliation("excel_readback_project_count_mismatch");
                    }
                    int proofedCount = Count(db,
                        "SELECT COUNT(*) FROM \"CheckRecordRegister\" WHERE \"SampleNo\"=:sample_no AND \"CheckItemID\"=:item_id AND \"ProofTime\" IS NOT NULL AND \"ProofUser\" IS NOT NULL",
                        ProjectScope(package, snapshot));
                    int expectedProofed = package.ExpectedExistingRegisterCount + (proofExpected ? 1 : 0);
                    if (proofedCount != expectedProofed)
                    {
                        throw Reconciliation("excel_readback_project_proof_count_mismatch");
                    }

                    var expectedIdentitiesByRecordId =
                        new Dictionary<string, string>(
                            snapshot.ExistingKeyIdentityByRecordId,
                            StringComparer.Ordinal);
                    if (expectedIdentitiesByRecordId.Count
                            != package.ExpectedExistingRegisterCount
                        || expectedIdentitiesByRecordId.ContainsKey(record.ID)
                        || package.ExcelRecord.ExpectedKeyIdentities.Count != 1)
                    {
                        throw Reconciliation("excel_readback_existing_key_identity_mismatch");
                    }
                    expectedIdentitiesByRecordId.Add(
                        record.ID, package.ExcelRecord.ExpectedKeyIdentities[0]);
                    var remainingKeyRecordIds = new HashSet<string>(
                        allowedRecordIds, StringComparer.Ordinal);
                    using (DataTable projectKeys = db.Query(
                        ExcelProjectKeySql, ProjectScope(package, snapshot)))
                    {
                        if (projectKeys.Rows.Count != expectedCount)
                        {
                            throw Reconciliation("excel_readback_key_result_count_mismatch");
                        }
                        foreach (DataRow row in projectKeys.Rows)
                        {
                            string keyRecordId = Text(row, "OriginalRecordID");
                            string expectedIdentity;
                            string expectedTemplate;
                            if (!remainingKeyRecordIds.Remove(keyRecordId)
                                || !expectedIdentitiesByRecordId.TryGetValue(
                                    keyRecordId, out expectedIdentity)
                                || !expectedTemplatesByRecordId.TryGetValue(
                                    keyRecordId, out expectedTemplate)
                                || !Same(Text(row, "SampleIdentity"), expectedIdentity)
                                || !Same(Text(row, "SeqNum"), "1")
                                || !Same(Text(row, "CheckItemName"), package.CheckItemName)
                                || !Same(Text(row, "ExcelTemplateName"),
                                    expectedTemplate))
                            {
                                throw Reconciliation(
                                    "excel_readback_key_result_contract_mismatch");
                            }
                        }
                    }
                    if (remainingKeyRecordIds.Count != 0
                        || Count(db, ExcelKeyScopeMismatchCountSql,
                            ProjectScope(package, snapshot)) != 0)
                    {
                        throw Reconciliation("excel_readback_key_result_scope_mismatch");
                    }
                    if (Count(db,
                            "SELECT COUNT(*) FROM \"OriginalKeyData_List\" " +
                            "WHERE \"SampleNo\"=:sample_no AND \"CheckItemID\"=:item_id " +
                            "AND \"OriginalRecordID\"=:record_id", scope) != listCount
                        || Count(db,
                            "SELECT COUNT(*) FROM \"OriginalKeyData_Other\" " +
                            "WHERE \"SampleNo\"=:sample_no AND \"CheckItemNo\"=:item_no " +
                            "AND \"OriginalRecordID\"=:record_id", OtherScope(package, record.ID)) != otherCount)
                    {
                        throw Reconciliation("excel_readback_auxiliary_data_mismatch");
                    }
                    if (snapshot.MappingDataTableExists)
                    {
                        if (!OracleIdentifier.IsMatch(snapshot.MappingDataTableName))
                        {
                            throw Reconciliation("excel_readback_mapping_table_invalid");
                        }
                        string customSql = "SELECT COUNT(*) FROM \""
                            + snapshot.MappingDataTableName
                            + "\" WHERE \"样品编号\"=:sample_no AND \"OriginalRecordID\"=:record_id";
                        if (Count(db, customSql, new List<DbParam>
                        {
                            new DbParam("sample_no", package.SampleNumber),
                            new DbParam("record_id", record.ID),
                        }) != 1)
                        {
                            throw Reconciliation("excel_readback_custom_data_mismatch");
                        }
                    }
                }
                finally
                {
                    db.RollbackAndClose();
                }
            }
        }

        private static List<DbParam> ProjectScope(
            FinalEntryPackage package, PreflightSnapshot snapshot)
        {
            return new List<DbParam>
            {
                new DbParam("sample_no", package.SampleNumber),
                new DbParam("item_id", snapshot.CheckItemId),
            };
        }

        private static List<DbParam> Scope(
            FinalEntryPackage package, PreflightSnapshot snapshot, string recordId)
        {
            List<DbParam> result = ProjectScope(package, snapshot);
            result.Add(new DbParam("record_id", recordId));
            return result;
        }

        private static List<DbParam> OtherScope(FinalEntryPackage package, string recordId)
        {
            return new List<DbParam>
            {
                new DbParam("sample_no", package.SampleNumber),
                new DbParam("item_no", package.CheckItemNo),
                new DbParam("record_id", recordId),
            };
        }

        private static int Count(ILegacyDb db, string sql, IList<DbParam> parameters)
        {
            return Convert.ToInt32(db.Scalar(sql, parameters));
        }

        private static void VerifyWorkbook(string path, WorkbookPayload expected)
        {
            var file = new FileInfo(path);
            if (!file.Exists || file.Length != expected.SizeBytes
                || !string.Equals(FinalEntryPackage.Sha256File(path), expected.ContentSha256,
                    StringComparison.OrdinalIgnoreCase))
            {
                throw Reconciliation("excel_readback_file_mismatch");
            }
        }

        private static bool HasValue(DataRow row, string column)
        {
            return row[column] != null && row[column] != DBNull.Value;
        }

        private static string Text(DataRow row, string column)
        {
            object value = row[column];
            return value == null || value == DBNull.Value ? string.Empty : Convert.ToString(value);
        }

        private static bool Same(string left, string right)
        {
            return string.Equals(left ?? string.Empty, right ?? string.Empty,
                StringComparison.Ordinal);
        }

        private static WriterFailureException Reconciliation(string code)
        {
            return new WriterFailureException(
                code, Program.ExitReconciliationRequired, true);
        }
    }
}
