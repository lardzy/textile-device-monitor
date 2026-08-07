using System;
using System.Collections.Generic;
using System.Data;
using System.Linq;
using System.Text;
using LegacyFibreCheckRunner;

namespace LegacyFibreCheckFinalEntryWriter
{
    internal static class LegacySafetyGuards
    {
        private const string OuterFunctionType =
            "Toone.FibreCheck.BusinessProcess.BusinessProcessUI.CheckRecord.CheckRecordRegisterUI";

        public static string Verify(
            string connectionString, FinalEntryPackage package, PreflightSnapshot snapshot,
            LegacyLoginFlow.StaffContext staff)
        {
            using (ILegacyDb db = new OdpNetDb(connectionString))
            {
                db.OpenReadOnly();
                try
                {
                    string[] outerControls = package.OperationType == FinalEntryPackage.ExcelOperation
                        ? new[] { "btnAddOriginalData", "btnSave", "btnReview" }
                        : new[] { "btnAddOriginalData" };
                    VerifyFunction(db, OuterFunctionType, outerControls, staff, true);
                    // The generic registration UI opens only as a child window of
                    // CheckRecordRegisterUI ("增加原始记录").  The desktop client
                    // enforces no separate function grant on that navigation path:
                    // accounts allowed to add original data records in the outer
                    // UI can save generic registrations.  Requiring a standalone
                    // CurrencyItemRecordUI grant here would reject operators who
                    // legitimately perform this flow interactively, so the inner
                    // function grant is intentionally not verified.
                    string projectDepartment = VerifyBusinessAuthorization(
                        db, package, snapshot, staff);
                    string branchFingerprint;
                    if (package.OperationType == FinalEntryPackage.GenericOperation)
                    {
                        if (!string.Equals(
                            snapshot.TaskCheckMethod,
                            package.GenericRecord.Header.TestMethod,
                            StringComparison.Ordinal))
                        {
                            throw new PackageValidationException(
                                "generic_task_method_changed");
                        }
                        if (snapshot.GiveJudgement != 0 && snapshot.GiveJudgement != 1)
                        {
                            throw new PackageValidationException("give_judgement_invalid");
                        }
                        bool taskRequiresJudgement = snapshot.GiveJudgement == 1;
                        bool packageHasJudgement = !string.IsNullOrWhiteSpace(
                            package.GenericRecord.Header.TotalJudge);
                        if (taskRequiresJudgement != packageHasJudgement)
                        {
                            throw new PackageValidationException(
                                "judgement_requires_interactive_confirmation");
                        }
                        branchFingerprint = "generic:" + snapshot.GiveJudgement;
                    }
                    else
                    {
                        branchFingerprint = VerifyStandardExcelBranch(db, package, snapshot);
                    }
                    return BuildSafetyFingerprint(
                        package, snapshot, staff, projectDepartment, branchFingerprint);
                }
                finally
                {
                    db.RollbackAndClose();
                }
            }
        }

        private static void VerifyFunction(
            ILegacyDb db, string functionType, string[] controls,
            LegacyLoginFlow.StaffContext staff, bool requireDefinition)
        {
            const string functionSql =
                "SELECT fd.\"FunctionID\" FROM \"PV_FunctionDefinition\" fd " +
                "WHERE fd.\"FunctionType\"=:function_type";
            string functionId = null;
            using (DataTable table = db.Query(functionSql, new List<DbParam>
            {
                new DbParam("function_type", functionType),
            }))
            {
                if (table.Rows.Count > 1 || (requireDefinition && table.Rows.Count != 1))
                {
                    throw new PackageValidationException("permission_function_definition_not_unique");
                }
                if (table.Rows.Count == 0)
                {
                    return; // inner UI with no definition keeps its default controls enabled
                }
                functionId = Text(table.Rows[0], "FunctionID");
            }
            if (string.IsNullOrWhiteSpace(functionId))
            {
                throw new PackageValidationException("permission_function_id_missing");
            }

            bool fadmin = string.Equals(staff.LoginName, "fadmin", StringComparison.OrdinalIgnoreCase);
            if (fadmin)
            {
                return;
            }
            List<string> operators = OperatorChain(staff);
            var functionParameters = new List<DbParam>
            {
                new DbParam("function_id", functionId),
            };
            string functionPredicate = OperatorPredicate("pa", operators, functionParameters);
            string grantSql =
                "SELECT COUNT(*) FROM \"PV_PurviewAssign\" pa " +
                "WHERE pa.\"PurviewID\"=:function_id AND pa.\"PurviewType\"=0 AND "
                + functionPredicate;
            if (Convert.ToInt32(db.Scalar(grantSql, functionParameters)) < 1)
            {
                throw new PackageValidationException("function_permission_denied");
            }

            var controlParameters = new List<DbParam>
            {
                new DbParam("function_id", functionId),
            };
            string controlPredicate = OperatorPredicate("pa", operators, controlParameters);
            string controlSql =
                "SELECT pd.\"ControlIdentity\", CASE WHEN EXISTS (" +
                "SELECT 1 FROM \"PV_PurviewAssign\" pa " +
                "WHERE pa.\"PurviewID\"=pd.\"PurviewID\" AND pa.\"PurviewType\"=1 AND " +
                controlPredicate + ") THEN 1 ELSE 0 END \"IsEnabled\" " +
                "FROM \"PV_PurviewDefinition\" pd " +
                "WHERE pd.\"FunctionID\"=:function_id ORDER BY pd.\"ControlIdentity\",pd.\"SeqNum\"";
            using (DataTable definitions = db.Query(controlSql, controlParameters))
            {
                foreach (string control in controls)
                {
                    foreach (DataRow row in definitions.Rows)
                    {
                        if (MatchesControl(Text(row, "ControlIdentity"), control)
                            && Convert.ToInt32(row["IsEnabled"]) != 1)
                        {
                            throw new PackageValidationException(
                                "control_permission_denied_" + control);
                        }
                    }
                }
            }
        }

        private static List<string> OperatorChain(LegacyLoginFlow.StaffContext staff)
        {
            var result = new List<string>();
            foreach (string value in staff.OperatorChain())
            {
                if (!string.IsNullOrWhiteSpace(value) && !result.Contains(value))
                {
                    result.Add(value);
                }
            }
            return result;
        }

        private static string OperatorPredicate(
            string alias, IList<string> operators, IList<DbParam> parameters)
        {
            var names = new List<string>();
            for (int index = 0; index < operators.Count; index++)
            {
                string name = "operator_" + index;
                names.Add(":" + name);
                parameters.Add(new DbParam(name, operators[index]));
            }
            string global = "(" + alias + ".\"OperatorID\" IS NULL OR "
                + alias + ".\"OperatorID\"='')";
            if (names.Count == 0)
            {
                return global;
            }
            return "(" + alias + ".\"OperatorID\" IN (" + string.Join(",", names)
                + ") OR " + alias + ".\"OperatorID\" IS NULL OR "
                + alias + ".\"OperatorID\"='')";
        }

        private static bool MatchesControl(string identity, string target)
        {
            foreach (string group in (identity ?? string.Empty).Split(','))
            {
                string[] path = group.Split(new[] { '-' }, StringSplitOptions.RemoveEmptyEntries);
                if (path.Length > 0 && string.Equals(
                    path[path.Length - 1].Trim(), target, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
            return false;
        }

        private static string VerifyBusinessAuthorization(
            ILegacyDb db, FinalEntryPackage package, PreflightSnapshot snapshot,
            LegacyLoginFlow.StaffContext staff)
        {
            const string sql =
                "SELECT d.\"DepartmentName\" FROM \"CheckItem\" ci " +
                "JOIN \"Position\" p ON p.ID=ci.\"PositionID\" " +
                "JOIN \"Department\" d ON d.ID=p.\"DepartmentID\" " +
                "WHERE ci.ID=:item_id";
            string projectDepartment;
            using (DataTable table = db.Query(sql, new List<DbParam>
            {
                new DbParam("item_id", snapshot.CheckItemId),
            }))
            {
                if (table.Rows.Count != 1
                    || string.IsNullOrWhiteSpace(projectDepartment = Text(table.Rows[0], "DepartmentName")))
                {
                    throw new PackageValidationException("project_department_not_unique");
                }
            }
            string department = staff.DepartmentName ?? string.Empty;
            string name = staff.ChineseName ?? string.Empty;
            bool canAdd = projectDepartment == department
                || department == "质量保证部" || department == "检验二部"
                || department == "技术部" || name == "孙宇";
            bool canSave = projectDepartment == department
                || (projectDepartment == "检验七部" && (name == "孙宇" || name == "肖敏"))
                || department == "质量保证部" || name == "熊菁蕾" || name == "唐泳容";
            bool saveRequired = package.OperationType == FinalEntryPackage.ExcelOperation;
            if (!canAdd || (saveRequired && !canSave))
            {
                throw new PackageValidationException(
                    !canAdd ? "business_add_permission_denied" : "business_save_permission_denied");
            }
            return projectDepartment;
        }

        private static string BuildSafetyFingerprint(
            FinalEntryPackage package, PreflightSnapshot snapshot,
            LegacyLoginFlow.StaffContext staff, string projectDepartment,
            string branchFingerprint)
        {
            var canonical = new StringBuilder();
            AppendCanonical(canonical, package.SchemaVersion.ToString());
            AppendCanonical(canonical, package.OperationType);
            AppendCanonical(canonical, package.SampleNumber);
            AppendCanonical(canonical, package.CheckItemNo);
            AppendCanonical(canonical, package.CheckItemName);
            AppendCanonical(canonical, package.ExpectedExistingRegisterCount.ToString());
            AppendCanonical(canonical, package.ControlledTestOverrideActive.ToString());
            AppendCanonical(canonical, package.ControlledTestOverrideApplied.ToString());
            if (package.ControlledTestOverride != null)
            {
                AppendCanonical(canonical, package.ControlledTestOverride.Kind);
                AppendCanonical(canonical,
                    package.ControlledTestOverride.TargetSampleNumber);
                AppendCanonical(canonical,
                    package.ControlledTestOverride.ExpectedTaskCheckCount.ToString());
                AppendCanonical(canonical,
                    package.ControlledTestOverride.ExpectedExistingRegisterCount.ToString());
                AppendCanonical(canonical,
                    package.ControlledTestOverride.ResultingRegisterCount.ToString());
                AppendCanonical(canonical, package.ControlledTestOverride.Reason);
            }
            AppendCanonical(canonical, snapshot.TaskId);
            AppendCanonical(canonical, snapshot.CheckItemId);
            AppendCanonical(canonical, snapshot.CheckItemPositionId);
            AppendCanonical(canonical, snapshot.OriginalDataInputUiClassName);
            AppendCanonical(canonical, snapshot.TaskCheckBasis);
            AppendCanonical(canonical, snapshot.TaskCheckMethod);
            AppendCanonical(canonical, snapshot.GiveJudgement.ToString());
            AppendCanonical(canonical, snapshot.DelegateOrgName);
            AppendCanonical(canonical, snapshot.ReportLanguage);
            AppendCanonical(canonical, snapshot.SampleCategory);
            AppendCanonical(canonical, snapshot.SampleReceiveTime.HasValue
                ? snapshot.SampleReceiveTime.Value.ToString("O") : string.Empty);
            AppendCanonical(canonical, snapshot.ExpectedResultCount.ToString());
            AppendCanonical(canonical, snapshot.ExistingRegisterCount.ToString());
            AppendCanonical(canonical, snapshot.ExistingGenericRecordCount.ToString());
            AppendCanonical(canonical, snapshot.ExistingFileReferenceCount.ToString());
            AppendCanonical(canonical, snapshot.ExistingProofedCount.ToString());
            AppendCanonical(canonical, snapshot.ExistingKeyLinkedRecordCount.ToString());
            AppendCanonical(canonical, snapshot.ExistingKeyResultCount.ToString());
            AppendCanonical(canonical, snapshot.ExistingKeyIdentitySha256);
            AppendCanonical(canonical, snapshot.TemplateDocumentId);
            AppendCanonical(canonical, snapshot.MappingDataTableName);
            AppendCanonical(canonical, snapshot.MappingDataTableExists.ToString());
            AppendCanonical(canonical, snapshot.MappingConfigSha256);
            AppendCanonical(canonical, snapshot.MappingConfigCount.ToString());
            AppendCanonical(canonical, snapshot.ServerDate.ToString("yyyy-MM-dd"));
            AppendCanonical(canonical, snapshot.FileServer);
            AppendCanonical(canonical, snapshot.OriginalDataRoot);
            AppendCanonical(canonical, projectDepartment);
            AppendCanonical(canonical, branchFingerprint);
            AppendCanonical(canonical, staff.Id);
            AppendCanonical(canonical, staff.PositionID);
            AppendCanonical(canonical, staff.DepartmentID);
            AppendCanonical(canonical, staff.SubDepartmentID);
            var parents = new List<string>(staff.ParentDepartmentIds);
            parents.Sort(StringComparer.Ordinal);
            foreach (string value in parents)
            {
                AppendCanonical(canonical, value);
            }
            return FinalEntryPackage.Sha256Text(canonical.ToString());
        }

        private static void AppendCanonical(StringBuilder builder, string value)
        {
            value = value ?? string.Empty;
            builder.Append(value.Length).Append(':').Append(value).Append('|');
        }

        private static string VerifyStandardExcelBranch(
            ILegacyDb db, FinalEntryPackage package, PreflightSnapshot snapshot)
        {
            const string languageSql =
                "SELECT ps.\"ReportLanguage\" FROM \"Task_PrintSetting\" ps " +
                "WHERE ps.\"TaskID\"=:task_id";
            string language;
            using (DataTable table = db.Query(languageSql, new List<DbParam>
            {
                new DbParam("task_id", snapshot.TaskId),
            }))
            {
                if (table.Rows.Count != 1
                    || string.IsNullOrWhiteSpace(language = Text(table.Rows[0], "ReportLanguage")))
                {
                    throw new PackageValidationException("report_language_not_unique");
                }
            }
            if (!string.Equals(language, snapshot.ReportLanguage, StringComparison.Ordinal))
            {
                throw new PackageValidationException("report_language_snapshot_mismatch");
            }
            if (!snapshot.SampleReceiveTime.HasValue
                || string.IsNullOrWhiteSpace(snapshot.DelegateOrgName)
                || package.SampleNumber.Length < 3)
            {
                throw new PackageValidationException("excel_branch_input_missing");
            }

            bool chinaEnglish = false;
            bool onlyChinaEnglish = false;
            bool showAllTarget = false;
            DateTime? chinaEnglishStart = null;
            const string customerSql =
                "SELECT co.\"IsChinaEngType\",co.\"StartChinaEngTypeDate\", " +
                "co.\"IsOnlyChinaEngReportType\",co.\"IsShowAllTarget\" " +
                "FROM \"CustomerOrg\" co WHERE co.\"FullName\"=:org";
            using (DataTable table = db.Query(customerSql, new List<DbParam>
            {
                new DbParam("org", snapshot.DelegateOrgName),
            }))
            {
                if (table.Rows.Count > 1)
                {
                    throw new PackageValidationException("customer_org_not_unique");
                }
                if (table.Rows.Count == 1)
                {
                    DataRow row = table.Rows[0];
                    chinaEnglish = Text(row, "IsChinaEngType") == "1";
                    onlyChinaEnglish = Text(row, "IsOnlyChinaEngReportType") == "1";
                    showAllTarget = Text(row, "IsShowAllTarget") == "1";
                    if (row["StartChinaEngTypeDate"] != DBNull.Value)
                    {
                        chinaEnglishStart = Convert.ToDateTime(row["StartChinaEngTypeDate"]);
                    }
                }
            }
            const string fixedSql =
                "SELECT COUNT(*) FROM \"FixedAttachmentTemplate\" f " +
                "WHERE f.\"CheckItemID\"=:item_id AND f.\"Language\"=:language";
            int fixedCount = Convert.ToInt32(db.Scalar(fixedSql, new List<DbParam>
            {
                new DbParam("item_id", snapshot.CheckItemId),
                new DbParam("language", "CnEn"),
            }));

            DateTime received = snapshot.SampleReceiveTime.Value;
            string org = snapshot.DelegateOrgName;
            string third = package.SampleNumber.Substring(2, 1);
            if (third == "G" || third == "J" || third == "S")
            {
                // The desktop workflow has a separate report-signature prerequisite for
                // these report families before proof.  v1 cannot reproduce that check
                // without broadening the writer, so reject the family even though its
                // workbook collector happens to be the standard collector.
                throw new PackageValidationException(
                    "signature_requirement_not_supported_in_v1");
            }
            bool gap = language == "CnEn"
                && (org.Contains("盖璞") || org == "盖璞（上海）商业有限公司")
                && received >= new DateTime(2014, 9, 9);
            bool newCnEn = chinaEnglish && chinaEnglishStart.HasValue
                && received >= chinaEnglishStart.Value
                && (!onlyChinaEnglish || language == "CnEn");
            bool fila = language == "CnEn" && IsFilaOrg(org, received);
            bool clothing = language == "En"
                && (org.Contains("PD CLOTHING & TEXTILES (ZHONG SHAN). LTD")
                    || org.Contains("Toray Sakai Weaving&Dyeing (Nantong) Co.,LTD"));
            string branch = LegacyExcelBranchRules.ResolveBranch(
                fixedCount,
                gap,
                newCnEn,
                fila,
                clothing);
            if (branch != "standard")
            {
                throw new PackageValidationException("excel_collection_branch_not_supported_" + branch);
            }
            if (language == "Cn" && showAllTarget)
            {
                throw new PackageValidationException("show_all_target_transform_not_supported_in_v1");
            }
            var canonical = new StringBuilder();
            canonical.Append(language).Append('|').Append(org).Append('|')
                .Append(received.ToString("O")).Append('|')
                .Append(chinaEnglish).Append('|').Append(chinaEnglishStart).Append('|')
                .Append(onlyChinaEnglish).Append('|').Append(showAllTarget).Append('|')
                // Keep the exact count fenced in the safety fingerprint even
                // though FirstOrDefault makes positive counts equivalent to
                // one another at the fixed-template existence test. A concurrent
                // row-count change is still detected before the write boundary.
                .Append(fixedCount).Append('|').Append(branch);
            return FinalEntryPackage.Sha256Text(canonical.ToString());
        }

        private static bool IsFilaOrg(string org, DateTime received)
        {
            if (org == "上海威尔胜体育用品有限公司" || org == "斐乐体育有限公司"
                || org == "安踏(中国)有限公司" || org == "亚玛芬体育用品贸易（上海）有限公司")
            {
                return true;
            }
            if (received < new DateTime(2020, 2, 11))
            {
                return false;
            }
            return org == "G.A. OPERATIONS S.P.A"
                || org == "G.A. OPERATIONS S.P.A.- DIV.VERTEMATE"
                || org == "海恩斯莫里斯（上海）商业有限公司"
                || org == "乔治阿玛尼（上海）商贸有限公司";
        }

        private static string Text(DataRow row, string column)
        {
            object value = row[column];
            return value == null || value == DBNull.Value ? string.Empty : Convert.ToString(value);
        }
    }
}
