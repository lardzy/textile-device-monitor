using System;
using System.Collections.Generic;
using System.Data;
using System.IO;
using System.Text;
using LegacyFibreCheckRunner;
using Toone.FibreCheck.Base.BaseDAL;
using Toone.FibreCheck.Base.BaseDAL.DbEntity;
using Toone.FibreCheck.BusinessProcess.BusinessProcessDAL;
using Toone.FibreCheck.BusinessProcess.BusinessProcessUI.CheckRecord;
using Toone.FibreCheck.Entites.CommonEntities.CommunicationEntities;
using Toone.FibreCheck.Entites.OrmEntites;
using Toone.FibreCheck.OriRecord.CurrencyItem;

namespace LegacyFibreCheckFinalEntryWriter
{
    internal static class FinalEntryExecutor
    {
        public static int Run(
            CommandLine options, FinalEntryPackage package, string workbookPath,
            string password, ReceiptEmitter emit, Func<bool> awaitPermit)
        {
            bool remoteWriteCompleted = false;
            var loginOptions = new RunnerOptions
            {
                FibreCheckDir = options.FibreCheckDir,
                Account = options.Account,
                Password = password,
                FunctionType = Program.FunctionType,
            };
            SortedDictionary<string, object> loginDocument;
            LegacyLoginFlow.StaffContext staff;
            int loginExit = LegacyLoginFlow.RunCore(
                loginOptions, value => new OdpNetDb(value), out loginDocument, out staff);
            if (loginExit != 0 || staff == null)
            {
                return emit.Finish(loginExit, "legacy_login_or_permission_denied", false, package);
            }
            emit.Stage("authenticated", null);
            emit.Stage("function_permission_verified", null);
            InjectStaff(staff, options.Account, password);
            string staffFingerprint = StaffFingerprint(staff);

            string connectionString = SystemDataConnection.Read(options.FibreCheckDir, "DbConnString");
            PreflightSnapshot snapshot = ReadOnlyResolver.Resolve(connectionString, package);
            string safetyFingerprint = LegacySafetyGuards.Verify(
                connectionString, package, snapshot, staff);
            emit.Stage("remote_preflight_verified", PreflightDetail(package, snapshot));

            string targetFilename = null;
            string initialTargetPath = null;
            string workRoot = null;
            if (package.OperationType == FinalEntryPackage.ExcelOperation)
            {
                targetFilename = Guid.NewGuid().ToString("D").ToLowerInvariant()
                    + Path.GetExtension(package.ExcelRecord.Workbook.Filename).ToLowerInvariant();
                initialTargetPath = BuildTargetPath(snapshot, targetFilename);
                if (File.Exists(initialTargetPath))
                {
                    throw new WriterFailureException(
                        "target_file_already_exists", Program.ExitRemoteConflict, false);
                }
                if (options.Execute)
                {
                    workRoot = ValidateWorkRoot(options.WorkRoot);
                }
            }

            if (!options.Execute)
            {
                emit.Stage("dry_run_completed", null);
                return emit.Finish(0, null, false, package);
            }

            emit.Stage("remote_write_ready", new SortedDictionary<string, object>
            {
                { "operation_type", package.OperationType },
                { "expected_existing_register_count", package.ExpectedExistingRegisterCount },
                { "target_filename", targetFilename },
                { "controlled_test_override_applied",
                    package.ControlledTestOverrideApplied },
            });
            if (awaitPermit == null || !awaitPermit())
            {
                throw new WriterFailureException(
                    "side_effect_permit_denied", Program.ExitPermitDenied, false);
            }
            emit.Stage("side_effect_permit_accepted", null);

            // Serialize writers for the same task project, then repeat every guard while
            // holding the lock. A second writer can only proceed after this one releases
            // the row and will therefore observe the updated register/key counts.
            using (ProjectWriteLock projectLock = ProjectWriteLock.Acquire(
                connectionString, snapshot.TaskId, snapshot.CheckItemId))
            {
            emit.Stage("project_write_lock_acquired", null);
            SortedDictionary<string, object> revalidatedLoginDocument;
            LegacyLoginFlow.StaffContext revalidatedStaff;
            int revalidatedLoginExit = LegacyLoginFlow.RunCore(
                loginOptions, value => new OdpNetDb(value),
                out revalidatedLoginDocument, out revalidatedStaff);
            if (revalidatedLoginExit != 0 || revalidatedStaff == null
                || !string.Equals(staffFingerprint, StaffFingerprint(revalidatedStaff),
                    StringComparison.Ordinal))
            {
                throw new WriterFailureException(
                    "account_or_staff_context_changed", Program.ExitRemoteConflict, false);
            }
            staff = revalidatedStaff;
            InjectStaff(staff, options.Account, password);
            snapshot = ReadOnlyResolver.Resolve(connectionString, package);
            string revalidatedSafetyFingerprint = LegacySafetyGuards.Verify(
                connectionString, package, snapshot, staff);
            if (!string.Equals(
                safetyFingerprint, revalidatedSafetyFingerprint, StringComparison.Ordinal))
            {
                throw new WriterFailureException(
                    "safety_inputs_changed", Program.ExitRemoteConflict, false);
            }
            if (package.OperationType == FinalEntryPackage.ExcelOperation)
            {
                string reverified = package.ResolveAndVerifyWorkbook(options.SourceRoot);
                if (!string.Equals(
                    Path.GetFullPath(reverified), Path.GetFullPath(workbookPath),
                    StringComparison.OrdinalIgnoreCase))
                {
                    throw new WriterFailureException(
                        "workbook_path_changed", Program.ExitSourceMismatch, false);
                }
                string revalidatedTargetPath = BuildTargetPath(snapshot, targetFilename);
                if (!string.Equals(initialTargetPath, revalidatedTargetPath,
                        StringComparison.OrdinalIgnoreCase))
                {
                    throw new WriterFailureException(
                        "target_path_changed", Program.ExitRemoteConflict, false);
                }
                initialTargetPath = revalidatedTargetPath;
                if (File.Exists(initialTargetPath))
                {
                    throw new WriterFailureException(
                        "target_file_appeared", Program.ExitRemoteConflict, false);
                }
            }
            emit.Stage("remote_state_revalidated", null);

            if (package.OperationType == FinalEntryPackage.GenericOperation)
            {
                ExecuteGeneric(connectionString, package, snapshot, staff, emit);
            }
            else
            {
                ExecuteExcel(
                    connectionString, package, snapshot, staff, emit,
                    workbookPath, workRoot, targetFilename, initialTargetPath);
            }
            remoteWriteCompleted = true;
            try
            {
                emit.Stage("completed", null);
            }
            catch
            {
                return Program.ExitReconciliationRequired;
            }
            }
            try
            {
                return emit.Finish(0, null, false, package);
            }
            catch
            {
                return remoteWriteCompleted
                    ? Program.ExitReconciliationRequired : Program.ExitPackageError;
            }
        }

        private static object PreflightDetail(FinalEntryPackage package, PreflightSnapshot snapshot)
        {
            var detail = new SortedDictionary<string, object>
            {
                { "expected_result_count", snapshot.ExpectedResultCount },
                { "existing_register_count", snapshot.ExistingRegisterCount },
                { "controlled_test_override_applied",
                    package.ControlledTestOverrideApplied },
            };
            if (package.OperationType == FinalEntryPackage.ExcelOperation)
            {
                detail["mapping_config_sha256"] = snapshot.MappingConfigSha256;
                detail["mapping_config_count"] = snapshot.MappingConfigCount;
            }
            return detail;
        }

        private static void ExecuteGeneric(
            string connectionString, FinalEntryPackage package, PreflightSnapshot snapshot,
            LegacyLoginFlow.StaffContext staff, ReceiptEmitter emit)
        {
            bool sideEffectStarted = false;
            try
            {
                var header = package.GenericRecord.Header;
                var record = new CurrencyItemRecordEntity
                {
                    SampleNo = package.SampleNumber,
                    CheckItemID = snapshot.CheckItemId,
                    CheckItemName = package.CheckItemName,
                    CheckUser = staff.Id,
                    Grade = header.Grade,
                    Unit = header.Unit,
                    JudgeBasis = header.JudgeBasis,
                    TestMethod = header.TestMethod,
                    SampleDescription = header.SampleDescription,
                    StandardType = header.StandardType,
                    ReportCheckItemName = header.ReportCheckItemName,
                    AttachInfo = header.AttachInfo,
                    Remark = header.Remark,
                    TotalJudge = header.TotalJudge,
                };
                var details = new List<CurrencyItemRecordDetailEntity>();
                int seq = 1;
                foreach (GenericDetail value in package.GenericRecord.Details)
                {
                    details.Add(new CurrencyItemRecordDetailEntity
                    {
                        SeqNum = seq++,
                        StandardLocation = value.StandardLocation,
                        StandardValue = value.StandardValue,
                        RealLocation = value.RealLocation,
                        RealValue = value.RealValue,
                    });
                }

                // CurrencyItemRecordDAL routes anything other than Laboratory_PY to the
                // HuaDu connection.  This module is deliberately PanYu-only.
                emit.Stage("generic_save_started", null);
                sideEffectStarted = true;
                var dal = new CurrencyItemRecordDAL();
                BoolResult save = dal.Save(
                    record, details, new List<CurrencyItemRecordDetailEntity>(),
                    Toone.FibreCheck.BasicSetup.BasicSetupDAL.BaseData.Laboratory_PY,
                    false);
                if (save == null || !save.Result)
                {
                    throw new WriterFailureException(
                        "generic_dal_save_failed", Program.ExitReconciliationRequired, true);
                }
                emit.Stage("generic_rows_saved", new SortedDictionary<string, object>
                {
                    { "detail_count", details.Count },
                    { "record_fingerprint", Redact.HashId(record.ID) },
                });

                emit.Stage("generic_projection_started", null);
                new CurrencyItemGenerateReportDataService().UpdateOriginalData(record, details);
                int projectionCount = VerifyGeneric(
                    connectionString, package, snapshot, staff, record, details);
                emit.Stage("generic_readback_verified", new SortedDictionary<string, object>
                {
                    { "detail_count", details.Count },
                    { "key_result_count", projectionCount },
                    { "record_fingerprint", Redact.HashId(record.ID) },
                });
            }
            catch (WriterFailureException)
            {
                throw;
            }
            catch (Exception ex)
            {
                throw new WriterFailureException(
                    "generic_write_failed_" + ex.GetType().Name,
                    sideEffectStarted ? Program.ExitReconciliationRequired : Program.ExitSourceMismatch,
                    sideEffectStarted);
            }
        }

        private static void ExecuteExcel(
            string connectionString, FinalEntryPackage package, PreflightSnapshot snapshot,
            LegacyLoginFlow.StaffContext staff, ReceiptEmitter emit, string workbookPath,
            string workRoot, string targetFilename, string targetPath)
        {
            bool sideEffectStarted = false;
            string stagingPath = Path.Combine(workRoot, targetFilename);
            try
            {
                if (File.Exists(stagingPath))
                {
                    throw new WriterFailureException(
                        "staging_file_already_exists", Program.ExitSourceMismatch, false);
                }
                CopyCreateNew(workbookPath, stagingPath);
                VerifyFile(stagingPath, package.ExcelRecord.Workbook.SizeBytes,
                    package.ExcelRecord.Workbook.ContentSha256, "staging_file_mismatch", false);
                emit.Stage("staging_file_verified", new SortedDictionary<string, object>
                {
                    { "filename", targetFilename },
                    { "size_bytes", package.ExcelRecord.Workbook.SizeBytes },
                    { "content_sha256", package.ExcelRecord.Workbook.ContentSha256 },
                });

                var record = new CheckRecordRegister
                {
                    ID = Guid.NewGuid().ToString(),
                    SampleNo = package.SampleNumber,
                    CheckItemID = snapshot.CheckItemId,
                    TemplateFilename = package.ExcelRecord.TemplateName,
                    OriginalDataFilename = targetFilename,
                    Level = package.ExcelRecord.Register.Level,
                    SampleIdentity = package.ExcelRecord.Register.SampleIdentity,
                    EquipmentNo = package.ExcelRecord.Register.EquipmentNo,
                    CheckBasis = package.ExcelRecord.Register.CheckBasis,
                    CheckUser = staff.Id,
                    CreateTime = snapshot.ServerDate,
                    LastUpdateTime = snapshot.ServerDate,
                    // CreateUser must remain empty: the official DAL uses it, not ID,
                    // to choose INSERT versus UPDATE and fills it from CurrentLoginedStaff.
                };

                // The service constructor can insert a missing mapping.  The mapping was
                // checked twice, but a concurrent delete is still possible, so this is the
                // conservative remote side-effect boundary.
                emit.Stage("excel_collection_started", null);
                sideEffectStarted = true;
                OriginalKeyDataSet firstData = CollectStandard(package, snapshot, record, stagingPath);
                RestoreAuthoritativeRegisterFields(package, staff, record);
                ValidateCollectedData(package, snapshot, staff, record, firstData);

                emit.Stage("remote_file_copy_started", null);
                string targetDirectory = Path.GetDirectoryName(targetPath);
                Directory.CreateDirectory(targetDirectory);
                CopyCreateNew(stagingPath, targetPath);
                VerifyFile(targetPath, package.ExcelRecord.Workbook.SizeBytes,
                    package.ExcelRecord.Workbook.ContentSha256, "remote_file_mismatch", true);
                emit.Stage("remote_file_verified", new SortedDictionary<string, object>
                {
                    { "filename", targetFilename },
                    { "content_sha256", package.ExcelRecord.Workbook.ContentSha256 },
                });

                emit.Stage("excel_register_save_started", null);
                var dal = new CheckRecordRegisterDAL();
                dal.SaveCheckRecordRegister(record, firstData);
                VerifyExcel(
                    connectionString, package, snapshot, staff, record, targetPath,
                    firstData.ListData.Count, firstData.OtherData.Count, false);
                emit.Stage("excel_register_saved_and_verified", null);

                VerifyFile(targetPath, package.ExcelRecord.Workbook.SizeBytes,
                    package.ExcelRecord.Workbook.ContentSha256, "remote_file_changed_before_proof", true);
                DateTime proofTime = ReadServerDate(connectionString);
                record.ProofTime = proofTime;
                record.ProofUser = staff.Id;
                record.ProofUserName = staff.ChineseName;
                record.LastUpdateTime = proofTime;
                record.LastUpdateUser = staff.Id;

                emit.Stage("excel_proof_save_started", null);
                OriginalKeyDataSet proofData = CollectStandard(package, snapshot, record, stagingPath);
                RestoreAuthoritativeRegisterFields(package, staff, record);
                ValidateCollectedData(package, snapshot, staff, record, proofData);
                // With a detached CRR the official DAL cannot derive CheckItem.No when
                // deleting OtherData.  The workbook is unchanged, so preserve the rows
                // saved in the first transaction and do not add a duplicate set.
                proofData.OtherData.Clear();
                dal.SaveCheckRecordRegister(record, proofData);
                VerifyExcel(
                    connectionString, package, snapshot, staff, record, targetPath,
                    firstData.ListData.Count, firstData.OtherData.Count, true);
                emit.Stage("excel_proof_verified", new SortedDictionary<string, object>
                {
                    { "key_result_count", package.ExcelRecord.KeyResultCount },
                    { "record_fingerprint", Redact.HashId(record.ID) },
                });
            }
            catch (WriterFailureException ex)
            {
                if (sideEffectStarted && !ex.ReconciliationRequired)
                {
                    throw new WriterFailureException(
                        ex.Code, Program.ExitReconciliationRequired, true);
                }
                throw;
            }
            catch (Exception ex)
            {
                throw new WriterFailureException(
                    "excel_write_failed_" + ex.GetType().Name,
                    sideEffectStarted ? Program.ExitReconciliationRequired : Program.ExitSourceMismatch,
                    sideEffectStarted);
            }
            finally
            {
                try { if (File.Exists(stagingPath)) File.Delete(stagingPath); }
                catch { }
            }
        }

        private static OriginalKeyDataSet CollectStandard(
            FinalEntryPackage package, PreflightSnapshot snapshot,
            CheckRecordRegister record, string stagingPath)
        {
            var service = new CollectOriginalDataService(
                package.SampleNumber, record, snapshot.CheckItemId,
                package.CheckItemNo, package.ExcelRecord.TemplateName, stagingPath);
            service.ReportLanguage = snapshot.ReportLanguage;
            service.DelegateOrgName = snapshot.DelegateOrgName;
            if (!string.IsNullOrWhiteSpace(snapshot.SampleCategory))
            {
                service.SampleCategory = snapshot.SampleCategory;
            }
            return service.CollectData();
        }

        private static void ValidateCollectedData(
            FinalEntryPackage package, PreflightSnapshot snapshot,
            LegacyLoginFlow.StaffContext staff, CheckRecordRegister record,
            OriginalKeyDataSet data)
        {
            if (data == null || data.CheckItemInfoData == null
                || data.ListData == null || data.OtherData == null || data.DataScripts == null
                || data.CheckItemInfoData.Count != package.ExcelRecord.KeyResultCount)
            {
                throw new WriterFailureException(
                    "collected_key_result_count_mismatch",
                    Program.ExitReconciliationRequired, true);
            }
            if (!string.Equals(data.CustomDataTableName, snapshot.MappingDataTableName,
                    StringComparison.Ordinal)
                || data.DataScripts.Count != 2)
            {
                throw new WriterFailureException(
                    "collected_custom_data_contract_mismatch",
                    Program.ExitReconciliationRequired, true);
            }
            ExcelRegisterFields expected = package.ExcelRecord.Register;
            if (!SameLegacyText(record.Level, expected.Level)
                || !SameLegacyText(record.SampleIdentity, expected.SampleIdentity)
                || !SameLegacyText(record.EquipmentNo, expected.EquipmentNo)
                || !SameLegacyText(record.CheckBasis, expected.CheckBasis)
                || !SameLegacyText(record.CheckUser, staff.Id))
            {
                throw new WriterFailureException(
                    "collected_register_fields_mismatch",
                    Program.ExitReconciliationRequired, true);
            }
            bool identityFound = string.IsNullOrWhiteSpace(package.ExcelRecord.Register.SampleIdentity);
            for (int index = 0; index < data.CheckItemInfoData.Count; index++)
            {
                OriginalKeyData_CheckItem item = data.CheckItemInfoData[index];
                if (item.OriginalRecordID != record.ID || item.CheckItemID != snapshot.CheckItemId
                    || item.SampleNo != package.SampleNumber
                    || item.SeqNum != 1
                    || !SameLegacyText(item.CheckItemName, package.CheckItemName)
                    || !SameLegacyText(item.ExcelTemplateName,
                        package.ExcelRecord.TemplateName))
                {
                    throw new WriterFailureException(
                        "collected_key_result_scope_mismatch",
                        Program.ExitReconciliationRequired, true);
                }
                if (!SameLegacyText(
                    item.SampleIdentity, package.ExcelRecord.ExpectedKeyIdentities[index]))
                {
                    throw new WriterFailureException(
                        "collected_key_identity_mismatch",
                        Program.ExitReconciliationRequired, true);
                }
                if (item.SampleIdentity == package.ExcelRecord.Register.SampleIdentity)
                {
                    identityFound = true;
                }
            }
            if (!identityFound)
            {
                throw new WriterFailureException(
                    "collected_sample_identity_mismatch",
                    Program.ExitReconciliationRequired, true);
            }
        }

        private static void RestoreAuthoritativeRegisterFields(
            FinalEntryPackage package, LegacyLoginFlow.StaffContext staff,
            CheckRecordRegister record)
        {
            // CollectOriginalDataService extracts register fields from the workbook and
            // mutates the supplied entity.  The execution package is the authoritative
            // source for these user-confirmed fields, while CheckUser must be the
            // authenticated operator ID.  Re-apply them before validation and before
            // either DAL save; workbook-derived key-result rows are still validated
            // independently below.
            ExcelRegisterFields expected = package.ExcelRecord.Register;
            AuthoritativeRegisterFieldValues fields =
                CollectedRegisterFieldAuthority.Resolve(
                    expected.Level,
                    expected.SampleIdentity,
                    expected.EquipmentNo,
                    expected.CheckBasis,
                    staff.Id);
            record.Level = fields.Level;
            record.SampleIdentity = fields.SampleIdentity;
            record.EquipmentNo = fields.EquipmentNo;
            record.CheckBasis = fields.CheckBasis;
            record.CheckUser = fields.CheckUser;
        }

        private static bool SameLegacyText(string left, string right)
        {
            return string.Equals(left ?? string.Empty, right ?? string.Empty,
                StringComparison.Ordinal);
        }

        private static string ValidateWorkRoot(string value)
        {
            if (string.IsNullOrWhiteSpace(value))
            {
                throw new PackageValidationException("execute_excel_requires_work_root");
            }
            string root = Path.GetFullPath(value)
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            if (!Directory.Exists(root))
            {
                throw new PackageValidationException("work_root_unavailable");
            }
            return root;
        }

        private static string BuildTargetPath(PreflightSnapshot snapshot, string filename)
        {
            if (!Path.IsPathRooted(snapshot.FileServer)
                || Path.IsPathRooted(snapshot.OriginalDataRoot)
                || Path.GetFileName(snapshot.CheckItemId) != snapshot.CheckItemId
                || Path.GetFileName(filename) != filename)
            {
                throw new PackageValidationException("original_data_path_config_invalid");
            }
            string serverRoot = Path.GetFullPath(snapshot.FileServer)
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            string root = Path.GetFullPath(Path.Combine(
                serverRoot, snapshot.OriginalDataRoot.TrimStart('\\', '/')));
            string serverPrefix = serverRoot + Path.DirectorySeparatorChar;
            if (!root.StartsWith(serverPrefix, StringComparison.OrdinalIgnoreCase))
            {
                throw new PackageValidationException("original_data_root_outside_file_server");
            }
            string target = Path.GetFullPath(Path.Combine(
                root, "Files", snapshot.ServerDate.Year.ToString(),
                snapshot.ServerDate.Month.ToString(), snapshot.ServerDate.Day.ToString(),
                snapshot.CheckItemId, filename));
            string prefix = root.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)
                + Path.DirectorySeparatorChar;
            if (!target.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            {
                throw new PackageValidationException("original_data_target_outside_root");
            }
            return target;
        }

        private static void CopyCreateNew(string source, string destination)
        {
            using (FileStream input = new FileStream(source, FileMode.Open, FileAccess.Read, FileShare.Read))
            using (FileStream output = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                input.CopyTo(output);
                output.Flush();
            }
        }

        private static void VerifyFile(
            string path, long size, string sha256, string code, bool reconciliation)
        {
            var info = new FileInfo(path);
            if (!info.Exists || info.Length != size
                || !string.Equals(FinalEntryPackage.Sha256File(path), sha256, StringComparison.OrdinalIgnoreCase))
            {
                throw new WriterFailureException(
                    code,
                    reconciliation ? Program.ExitReconciliationRequired : Program.ExitSourceMismatch,
                    reconciliation);
            }
        }

        private static DateTime ReadServerDate(string connectionString)
        {
            using (ILegacyDb db = new OdpNetDb(connectionString))
            {
                db.OpenReadOnly();
                try { return Convert.ToDateTime(db.Scalar(LegacyLoginFlow.SysDateSql, new List<DbParam>())); }
                finally { db.RollbackAndClose(); }
            }
        }

        private static void InjectStaff(
            LegacyLoginFlow.StaffContext staff, string account, string password)
        {
            Toone.FibreCheck.Entites.CommonEntities.SystemData.Instance.CurrentLoginedStaff =
                new StaffEntity
                {
                    ID = staff.Id,
                    ChineseName = staff.ChineseName,
                    Name = account,
                    Password = Redact.EncryptByMd5(password),
                    UserRemark = staff.UserRemark ?? string.Empty,
                    PanYuId = staff.Id,
                    HuaDuId = string.Empty,
                    PositionID = staff.PositionID,
                    PositionName = staff.PositionName,
                    DepartmentID = staff.DepartmentID,
                    DepartmentName = staff.DepartmentName,
                    SubDepartmentID = staff.SubDepartmentID,
                    SubDepartmentName = staff.SubDepartmentName,
                    ParentDepartmentIds = new List<string>(staff.ParentDepartmentIds),
                };
        }

        private static string StaffFingerprint(LegacyLoginFlow.StaffContext staff)
        {
            var canonical = new StringBuilder();
            AppendFingerprint(canonical, staff.Id);
            AppendFingerprint(canonical, staff.LoginName);
            AppendFingerprint(canonical, staff.ChineseName);
            AppendFingerprint(canonical, staff.UserRemark);
            AppendFingerprint(canonical, staff.PositionID);
            AppendFingerprint(canonical, staff.PositionName);
            AppendFingerprint(canonical, staff.DepartmentID);
            AppendFingerprint(canonical, staff.DepartmentName);
            AppendFingerprint(canonical, staff.SubDepartmentID);
            AppendFingerprint(canonical, staff.SubDepartmentName);
            var parents = new List<string>(staff.ParentDepartmentIds);
            parents.Sort(StringComparer.Ordinal);
            foreach (string value in parents)
            {
                AppendFingerprint(canonical, value);
            }
            return FinalEntryPackage.Sha256Text(canonical.ToString());
        }

        private static void AppendFingerprint(StringBuilder builder, string value)
        {
            value = value ?? string.Empty;
            builder.Append(value.Length).Append(':').Append(value).Append('|');
        }

        // Readback implementations live in a separate file so they can be tested without
        // making the side-effect executor itself any broader.
        private static int VerifyGeneric(
            string connectionString, FinalEntryPackage package, PreflightSnapshot snapshot,
            LegacyLoginFlow.StaffContext staff, CurrencyItemRecordEntity record,
            IList<CurrencyItemRecordDetailEntity> details)
        {
            return FinalEntryReadback.VerifyGeneric(
                connectionString, package, snapshot, staff, record, details);
        }

        private static void VerifyExcel(
            string connectionString, FinalEntryPackage package, PreflightSnapshot snapshot,
            LegacyLoginFlow.StaffContext staff, CheckRecordRegister record, string targetPath,
            int listCount, int otherCount, bool proofExpected)
        {
            FinalEntryReadback.VerifyExcel(
                connectionString, package, snapshot, staff, record, targetPath,
                listCount, otherCount, proofExpected);
        }
    }
}
