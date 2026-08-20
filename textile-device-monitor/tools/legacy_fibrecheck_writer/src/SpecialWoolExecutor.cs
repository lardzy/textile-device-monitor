using System;
using System.Collections;
using System.Collections.Generic;
using System.Data;
using System.Globalization;
using System.IO;
using LegacyFibreCheckRunner;
using Toone.FibreCheck.Entites.CommonEntities.CommunicationEntities;
using Toone.FibreCheck.Entites.OrmEntites;
using Toone.FibreCheck.OriRecord.SpecialWool;

namespace LegacyFibreCheckWriter
{
    /// <summary>
    /// 图片类特种毛上传与复核执行器。两种操作共享登录、项目绑定和协作锁，
    /// 但使用完全独立的任务包字段、阶段和回读契约。
    /// </summary>
    internal static class SpecialWoolExecutor
    {
        private const string SearchFunctionType =
            "Toone.FibreCheck.OriRecord.SpecialWool.SpecialWoolSearchUI";
        private const string ReviewFunctionType =
            "Toone.FibreCheck.OriRecord.SpecialWool.SpecialWoolCheckUI";

        private const string ProjectSql =
            "SELECT t.\"ID\" AS \"TaskID\", tci.\"ID\" AS \"TaskCheckItemID\", " +
            "tci.\"CheckItemID\", tci.\"CheckItemNo\", tci.\"CheckItemName\", " +
            "tci.\"CheckMethod\", tci.\"SeqNum\", tci.\"CheckCount\" " +
            "FROM \"Task\" t JOIN \"Task_CheckItem\" tci ON tci.\"TaskID\"=t.\"ID\" " +
            "WHERE t.\"ReportNo\"=:sample_no ORDER BY tci.\"SeqNum\",tci.\"ID\"";

        private const string FamilySql =
            "SELECT sw.\"SampleNo\" FROM \"SpecialWoolManage\" sw " +
            "WHERE sw.\"SampleNo\"=:target_base OR sw.\"SampleNo\" LIKE :target_prefix " +
            "ORDER BY sw.\"SampleNo\"";

        private const string ExactMainSql =
            "SELECT * FROM \"SpecialWoolManage\" sw " +
            "WHERE sw.\"SampleNo\"=:target_sample_no ORDER BY sw.\"ID\"";

        private const string PictureChildrenSql =
            "SELECT * FROM \"OriginalDataPictureFile\" p " +
            "WHERE p.\"SpecialWoolManageID\"=:main_id ORDER BY p.\"ID\"";

        private sealed class TaskProject
        {
            internal string TaskId;
            internal string TaskCheckItemId;
            internal string CheckItemId;
        }

        private sealed class SourceArtifact
        {
            internal string ArtifactId;
            internal string RelativePath;
            internal string FileName;
            internal string Path;
            internal byte[] Bytes;
            internal long Size;
            internal string Sha256;
        }

        internal static int Execute(
            string fibreCheckDir,
            string account,
            string password,
            string sourceRoot,
            Dictionary<string, object> operation,
            Dictionary<string, object> summary,
            string operationType,
            UploadExecutor.Result result,
            Action<string, object> stage,
            Action<object> emit,
            Func<bool> awaitSideEffectPermit)
        {
            string contractError = ValidatePackageContract(
                operation, summary, operationType);
            if (contractError != null)
            {
                result.Receipt["error"] = contractError;
                return UploadExecutor.Finish(
                    result, UploadExecutor.ExitPackageError, null, emit);
            }

            bool uploadOperation = string.Equals(
                    operationType,
                    SpecialWoolContracts.ImageOperation,
                    StringComparison.Ordinal)
                || string.Equals(
                    operationType,
                    SpecialWoolContracts.QualitativeUploadOperation,
                    StringComparison.Ordinal);
            return uploadOperation
                ? ExecuteUpload(
                    fibreCheckDir, account, password, sourceRoot,
                    operation, summary, operationType, result, stage, emit,
                    awaitSideEffectPermit)
                : ExecuteReview(
                    fibreCheckDir, account, password,
                    operation, summary, operationType, result, stage, emit,
                    awaitSideEffectPermit);
        }

        internal static int ReconcileExistingImageUpload(
            string fibreCheckDir,
            string account,
            string password,
            string sourceRoot,
            Dictionary<string, object> operation,
            Dictionary<string, object> summary,
            UploadExecutor.Result result,
            Action<string, object> stage,
            Action<object> emit)
        {
            string contractError = ValidatePackageContract(
                operation,
                summary,
                SpecialWoolContracts.ImageOperation);
            if (contractError != null)
            {
                return PackageFailure(result, emit, contractError);
            }

            string target = UploadExecutor.GetStr(
                summary,
                "target_sample_number");
            string targetFilename;
            try
            {
                targetFilename = SpecialWoolContracts.BuildTargetFilename(
                    target);
            }
            catch (ArgumentException)
            {
                return PackageFailure(
                    result,
                    emit,
                    "target_filename_invalid");
            }
            if (!string.Equals(
                UploadExecutor.GetStr(summary, "target_filename"),
                targetFilename,
                StringComparison.Ordinal))
            {
                return PackageFailure(
                    result,
                    emit,
                    "target_filename_mismatch");
            }

            string sourceNumber = UploadExecutor.GetStr(
                summary,
                "source_inspection_number");
            string inspectorName = UploadExecutor.GetStr(summary, "inspector");
            Dictionary<string, object> expectedProject =
                UploadExecutor.GetMap(summary, "task_project");
            if (!LegacyLoginFlow.IsValidSampleNo(target)
                || string.IsNullOrWhiteSpace(sourceNumber)
                || string.IsNullOrWhiteSpace(inspectorName)
                || expectedProject == null)
            {
                return PackageFailure(
                    result,
                    emit,
                    "package_fields_invalid");
            }

            SourceArtifact source;
            string sourceError;
            if (!TryReadSource(
                summary,
                sourceRoot,
                out source,
                out sourceError))
            {
                result.Receipt["error"] = sourceError;
                return UploadExecutor.Finish(
                    result,
                    sourceError != null && sourceError.StartsWith(
                        "source_",
                        StringComparison.Ordinal)
                        ? UploadExecutor.ExitSourceMismatch
                        : UploadExecutor.ExitPackageError,
                    null,
                    emit);
            }

            LegacyLoginFlow.StaffContext staff;
            SortedDictionary<string, object> loginDocument;
            int loginExit = Authenticate(
                fibreCheckDir,
                account,
                password,
                SearchFunctionType,
                false,
                stage,
                out staff,
                out loginDocument);
            if (loginExit != 0 || staff == null)
            {
                result.Receipt["error"] =
                    "login_or_permission_failed:" + loginExit;
                result.Receipt["login"] = loginDocument;
                return UploadExecutor.Finish(
                    result,
                    loginExit,
                    null,
                    emit);
            }
            BindStaff(account, password, staff);

            DataRow mainRow;
            TaskProject project;
            string inspectorId;
            string targetPath;
            string connectionString = SystemDataConnection.Read(
                fibreCheckDir,
                "DbConnString");
            using (var db = new OdpNetDb(connectionString))
            {
                db.OpenReadOnly();
                string projectError;
                project = ResolveTaskProject(
                    db,
                    sourceNumber,
                    expectedProject,
                    out projectError);
                if (project == null)
                {
                    return PackageFailure(result, emit, projectError);
                }

                if (!SpecialWoolContracts.TryBindInspectorToAuthenticatedStaff(
                    inspectorName,
                    staff.ChineseName,
                    staff.Id,
                    out inspectorName,
                    out inspectorId))
                {
                    result.Receipt["error"] = "authenticated_inspector_missing";
                    return UploadExecutor.Finish(
                        result,
                        ExitCodes.InspectorMappingFailed,
                        null,
                        emit);
                }
                if (!SpecialWoolContracts.MatchesLoginInspector(
                    inspectorName,
                    inspectorId,
                    staff.ChineseName,
                    staff.Id))
                {
                    result.Receipt["error"] =
                        "inspector_login_identity_mismatch";
                    return UploadExecutor.Finish(
                        result,
                        ExitCodes.InspectorMappingFailed,
                        null,
                        emit);
                }

                DataTable main = db.Query(
                    ExactMainSql,
                    new List<DbParam>
                    {
                        new DbParam("target_sample_no", target),
                    });
                if (main.Rows.Count != 1)
                {
                    result.Receipt["error"] =
                        "reconciliation_main_record_count_mismatch";
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitSourceMismatch,
                        null,
                        emit);
                }
                mainRow = main.Rows[0];
                string mainId = Text(mainRow, "ID");
                var mismatches = VerifyImageMain(
                    mainRow,
                    mainId,
                    target,
                    inspectorId,
                    targetFilename,
                    staff.Id);
                if (mismatches.Count != 0)
                {
                    result.Receipt["error"] =
                        "reconciliation_main_record_verify_mismatch";
                    result.Receipt["mismatched_fields"] = mismatches;
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitSourceMismatch,
                        null,
                        emit);
                }

                DataTable pictures = db.Query(
                    PictureChildrenSql,
                    new List<DbParam>
                    {
                        new DbParam("main_id", mainId),
                    });
                // 与官方客户端手工上传同形：图片类记录不写
                // OriginalDataPictureFile 子表，对账时必须为 0 条。
                if (pictures.Rows.Count != 0)
                {
                    result.Receipt["error"] =
                        "reconciliation_picture_child_count_mismatch";
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitSourceMismatch,
                        null,
                        emit);
                }

                DateTime? createTime = NullableDate(mainRow, "CreateTime");
                string fileServer = Convert.ToString(db.Scalar(
                    LegacyLoginFlow.KeyValueSql,
                    new List<DbParam>
                    {
                        new DbParam("infokey", "FileServer"),
                    }) ?? "");
                string originalData = Convert.ToString(db.Scalar(
                    LegacyLoginFlow.FileDirectorySql,
                    new List<DbParam>
                    {
                        new DbParam("infokey", "OriginalData"),
                    }) ?? "");
                if (!createTime.HasValue
                    || string.IsNullOrWhiteSpace(fileServer)
                    || string.IsNullOrWhiteSpace(originalData))
                {
                    result.Receipt["error"] =
                        "reconciliation_file_location_unavailable";
                    return UploadExecutor.Finish(
                        result,
                        ExitCodes.InfrastructureError,
                        null,
                        emit);
                }
                DateTime fileDate = createTime.Value;
                targetPath = Path.Combine(
                    fileServer,
                    originalData,
                    "Files",
                    fileDate.Year.ToString(CultureInfo.InvariantCulture),
                    fileDate.Month.ToString(CultureInfo.InvariantCulture),
                    fileDate.Day.ToString(CultureInfo.InvariantCulture),
                    "SpecialWool",
                    targetFilename);

                stage("remote_state_verified", new SortedDictionary<string, object>
                {
                    { "target_sample_number", target },
                    { "exact_count", 1 },
                });
                stage("task_project_verified", new SortedDictionary<string, object>
                {
                    { "project_key", UploadExecutor.GetStr(expectedProject, "project_key") },
                    { "check_item_id", Redact.HashId(project.CheckItemId) },
                });
            }

            byte[] remoteBytes;
            try
            {
                remoteBytes = File.ReadAllBytes(targetPath);
            }
            catch (Exception ex)
            {
                result.Receipt["error"] =
                    "reconciliation_server_file_unreadable:" +
                    ex.GetType().Name;
                return UploadExecutor.Finish(
                    result,
                    UploadExecutor.ExitSourceMismatch,
                    null,
                    emit);
            }
            LegacyXlsFileVerification serverVerification =
                LegacyXlsFileVerifier.Verify(source.Bytes, remoteBytes);
            if (!serverVerification.Verified
                || serverVerification.SourceSize != source.Size
                || !string.Equals(
                    serverVerification.SourceSha256,
                    source.Sha256,
                    StringComparison.OrdinalIgnoreCase))
            {
                result.Receipt["error"] =
                    "reconciliation_server_file_verify_mismatch";
                result.Receipt["server_file_verification_error"] =
                    serverVerification.Error;
                return UploadExecutor.Finish(
                    result,
                    UploadExecutor.ExitSourceMismatch,
                    null,
                    emit);
            }
            stage("file_copy_verified", new SortedDictionary<string, object>
            {
                { "file_name", targetFilename },
                { "content_sha256", serverVerification.RemoteSha256 },
                { "verification_mode", serverVerification.Mode },
            });
            stage("main_record_verified", new SortedDictionary<string, object>
            {
                { "record_id", Redact.HashId(Text(mainRow, "ID")) },
                { "field_fingerprint", SpecialWoolContracts.FingerprintRow(mainRow, null) },
            });
            BuildImageReceipt(
                result,
                operation,
                summary,
                UploadExecutor.GetStr(summary, "target_sample_number"),
                expectedProject,
                source,
                targetFilename,
                mainRow,
                null,
                serverVerification);
            stage("completed", null);
            return UploadExecutor.Finish(result, 0, null, emit);
        }

        private static int ExecuteUpload(
            string fibreCheckDir,
            string account,
            string password,
            string sourceRoot,
            Dictionary<string, object> operation,
            Dictionary<string, object> summary,
            string operationType,
            UploadExecutor.Result result,
            Action<string, object> stage,
            Action<object> emit,
            Func<bool> awaitSideEffectPermit)
        {
            string target = UploadExecutor.GetStr(summary, "target_sample_number");
            bool imageUpload = string.Equals(
                operationType,
                SpecialWoolContracts.ImageOperation,
                StringComparison.Ordinal);
            string targetFilename = null;
            if (imageUpload)
            {
                try
                {
                    targetFilename = SpecialWoolContracts.BuildTargetFilename(target);
                }
                catch (ArgumentException)
                {
                    return PackageFailure(result, emit, "target_filename_invalid");
                }
            }
            string sourceNumber = UploadExecutor.GetStr(summary, "source_inspection_number");
            string inspectorName = UploadExecutor.GetStr(summary, "inspector");
            Dictionary<string, object> allocation = UploadExecutor.GetMap(
                summary, "target_allocation");
            string targetBase = UploadExecutor.GetStr(allocation, "base_number");
            Dictionary<string, object> expectedProject = UploadExecutor.GetMap(
                summary, "task_project");
            if (!LegacyLoginFlow.IsValidSampleNo(target)
                || string.IsNullOrWhiteSpace(sourceNumber)
                || string.IsNullOrWhiteSpace(inspectorName)
                || string.IsNullOrWhiteSpace(targetBase)
                || expectedProject == null)
            {
                return PackageFailure(result, emit, "package_fields_invalid");
            }

            SourceArtifact source;
            string sourceError;
            if (!TryReadSource(summary, sourceRoot, out source, out sourceError))
            {
                result.Receipt["error"] = sourceError;
                return UploadExecutor.Finish(
                    result,
                    sourceError != null && sourceError.StartsWith(
                        "source_", StringComparison.Ordinal)
                        ? UploadExecutor.ExitSourceMismatch
                        : UploadExecutor.ExitPackageError,
                    null,
                    emit);
            }
            if (!imageUpload)
            {
                try
                {
                    targetFilename =
                        SpecialWoolContracts.BuildPrefixedTargetFilename(
                            target, source.FileName);
                }
                catch (ArgumentException)
                {
                    return PackageFailure(
                        result, emit, "target_filename_invalid");
                }
            }
            if (!string.Equals(
                UploadExecutor.GetStr(summary, "target_filename"),
                targetFilename,
                StringComparison.Ordinal))
            {
                return PackageFailure(result, emit, "target_filename_mismatch");
            }

            LegacyLoginFlow.StaffContext staff;
            SortedDictionary<string, object> loginDocument;
            int loginExit = Authenticate(
                fibreCheckDir, account, password, SearchFunctionType,
                false, stage, out staff, out loginDocument);
            if (loginExit != 0 || staff == null)
            {
                result.Receipt["error"] = "login_or_permission_failed:" + loginExit;
                result.Receipt["login"] = loginDocument;
                return UploadExecutor.Finish(result, loginExit, null, emit);
            }
            BindStaff(account, password, staff);

            string connectionString = SystemDataConnection.Read(
                fibreCheckDir, "DbConnString");
            TaskProject project;
            string inspectorId;
            DateTime serverDate;
            string targetDir;
            string planned;
            string plannedFilename;
            using (var db = new OdpNetDb(connectionString))
            {
                db.OpenReadOnly();
                string projectError;
                project = ResolveTaskProject(
                    db, sourceNumber, expectedProject, out projectError);
                if (project == null)
                {
                    return PackageFailure(result, emit, projectError);
                }

                List<string> occupied = ReadOccupied(db, targetBase);
                try
                {
                    planned = SpecialWoolContracts.AllocateFirstFree(
                        targetBase, occupied);
                }
                catch (Exception)
                {
                    return PackageFailure(
                        result, emit, "target_allocation_invalid");
                }
                // 顺号规则：预检请求号只是建议值，实际写入号在写锁内按旧系统
                // 当前实况取第一空闲号。人工新增/删除造成的漂移不再阻断执行，
                // 只在这里与回执中记录 planned/requested 供审计。
                stage("remote_state_verified", new SortedDictionary<string, object>
                {
                    { "requested_sample_number", target },
                    { "planned_sample_number", planned },
                    {
                        "renumber_planned",
                        !string.Equals(planned, target, StringComparison.Ordinal)
                    },
                    { "occupied_family_count", occupied.Count },
                });
                stage("task_project_verified", new SortedDictionary<string, object>
                {
                    { "project_key", UploadExecutor.GetStr(expectedProject, "project_key") },
                    { "check_item_id", Redact.HashId(project.CheckItemId) },
                });

                // 检验员统一绑定到已认证的旧系统 Staff：旧系统账号是所有旧系统
                // 流程的通用身份，执行系统账号的显示名只用于审计（与图片上传一致）。
                if (!SpecialWoolContracts.TryBindInspectorToAuthenticatedStaff(
                    inspectorName,
                    staff.ChineseName,
                    staff.Id,
                    out inspectorName,
                    out inspectorId))
                {
                    result.Receipt["error"] = "authenticated_inspector_missing";
                    return UploadExecutor.Finish(
                        result,
                        ExitCodes.InspectorMappingFailed,
                        null,
                        emit);
                }
                if (!SpecialWoolContracts.MatchesLoginInspector(
                    inspectorName,
                    inspectorId,
                    staff.ChineseName,
                    staff.Id))
                {
                    result.Receipt["error"] = "inspector_login_identity_mismatch";
                    return UploadExecutor.Finish(
                        result, ExitCodes.InspectorMappingFailed, null, emit);
                }

                string fileServer = Convert.ToString(db.Scalar(
                    LegacyLoginFlow.KeyValueSql,
                    new List<DbParam> { new DbParam("infokey", "FileServer") }) ?? "");
                string originalData = Convert.ToString(db.Scalar(
                    LegacyLoginFlow.FileDirectorySql,
                    new List<DbParam> { new DbParam("infokey", "OriginalData") }) ?? "");
                object serverDateValue = db.Scalar(
                    LegacyLoginFlow.SysDateSql, new List<DbParam>());
                if (string.IsNullOrWhiteSpace(fileServer)
                    || string.IsNullOrWhiteSpace(originalData)
                    || !(serverDateValue is DateTime))
                {
                    result.Receipt["error"] = "file_server_config_unavailable";
                    return UploadExecutor.Finish(
                        result, ExitCodes.InfrastructureError, null, emit);
                }
                serverDate = (DateTime)serverDateValue;
                targetDir = Path.Combine(
                    fileServer,
                    originalData,
                    "Files",
                    serverDate.Year.ToString(CultureInfo.InvariantCulture),
                    serverDate.Month.ToString(CultureInfo.InvariantCulture),
                    serverDate.Day.ToString(CultureInfo.InvariantCulture),
                    "SpecialWool");
                try
                {
                    plannedFilename = imageUpload
                        ? SpecialWoolContracts.BuildTargetFilename(planned)
                        : SpecialWoolContracts.BuildPrefixedTargetFilename(
                            planned, source.FileName);
                }
                catch (ArgumentException)
                {
                    return PackageFailure(result, emit, "target_filename_invalid");
                }
            }

            stage("file_copy_ready", new SortedDictionary<string, object>
            {
                { "file_name", plannedFilename },
                { "planned_sample_number", planned },
                { "size_bytes", source.Size },
                { "content_sha256", source.Sha256 },
            });
            if (awaitSideEffectPermit == null || !awaitSideEffectPermit())
            {
                result.Receipt["error"] = "side_effect_permit_denied";
                return UploadExecutor.Finish(
                    result, UploadExecutor.ExitSideEffectPermitDenied, null, emit);
            }

            using (SpecialWoolWriteLock writeLock = AcquireLock(
                connectionString, project, "file_copy_started", result, emit))
            {
                if (writeLock == null)
                {
                    return UploadExecutor.ExitReconciliationRequired;
                }
                // 顺号规则：以写锁内旧系统当前实际记录为准取第一空闲号；
                // 与预检请求号不一致（人工新增/删除漂移）不再阻断执行。
                List<string> lockedOccupied = ReadOccupiedUnderLock(
                    writeLock, targetBase);
                string actual = SpecialWoolContracts.AllocateFirstFree(
                    targetBase, lockedOccupied);
                string actualFilename = imageUpload
                    ? SpecialWoolContracts.BuildTargetFilename(actual)
                    : SpecialWoolContracts.BuildPrefixedTargetFilename(
                        actual, source.FileName);
                string actualPath = Path.Combine(targetDir, actualFilename);
                while (File.Exists(actualPath))
                {
                    // 文件残留但主表无记录（人工只删了记录）：跳过该号继续顺号。
                    lockedOccupied.Add(actual);
                    actual = SpecialWoolContracts.AllocateFirstFree(
                        targetBase, lockedOccupied);
                    actualFilename = imageUpload
                        ? SpecialWoolContracts.BuildTargetFilename(actual)
                        : SpecialWoolContracts.BuildPrefixedTargetFilename(
                            actual, source.FileName);
                    actualPath = Path.Combine(targetDir, actualFilename);
                }
                DataTable lockedExisting = QueryMain(writeLock, actual);
                if (lockedExisting.Rows.Count != 0)
                {
                    // 与并发写入撞号（理论上 AllocateFirstFree 已排除）：
                    // 交人工对账，不自动推进。
                    result.Receipt["error"] = "target_conflict_under_lock";
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitReconciliationRequired,
                        "file_copy_started",
                        emit);
                }

                stage("file_copy_started", new SortedDictionary<string, object>
                {
                    { "file_name", actualFilename },
                    { "size_bytes", source.Size },
                    { "content_sha256", source.Sha256 },
                });
                try
                {
                    Directory.CreateDirectory(targetDir);
                    using (var stream = new FileStream(
                        actualPath,
                        FileMode.CreateNew,
                        FileAccess.Write,
                        FileShare.None))
                    {
                        stream.Write(source.Bytes, 0, source.Bytes.Length);
                        stream.Flush();
                    }
                    byte[] copied = File.ReadAllBytes(actualPath);
                    if (copied.LongLength != source.Size
                        || !string.Equals(
                            UploadExecutor.Sha256Hex(copied),
                            source.Sha256,
                            StringComparison.OrdinalIgnoreCase))
                    {
                        result.Receipt["error"] = "file_copy_verify_mismatch";
                        return UploadExecutor.Finish(
                            result,
                            UploadExecutor.ExitReconciliationRequired,
                            "file_copy_started",
                            emit);
                    }
                }
                catch (Exception ex)
                {
                    result.Receipt["error"] = "file_copy_failed:" + ex.GetType().Name;
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitReconciliationRequired,
                        "file_copy_started",
                        emit);
                }
                stage("file_copy_verified", new SortedDictionary<string, object>
                {
                    { "file_name", actualFilename },
                    { "content_sha256", source.Sha256 },
                });

                stage("main_record_save_started", null);
                string savedId;
                try
                {
                    var record = new SpecialWoolManage
                    {
                        SampleNo = actual,
                        FibreSort = imageUpload ? "图片" : "棉再生纤",
                        CheckWay = imageUpload ? string.Empty : "定量",
                        CheckUser1 = inspectorId,
                        CheckUserItem1 = imageUpload
                            ? "图片"
                            : "棉再生纤定性",
                        CheckUserNumber1 = 1,
                        // 与官方客户端手工上传同形：图片类复核项目保持 NULL，
                        // 官方复核也只设 ReviewUser/ReviewTime。
                        ReviewUserItem1 = imageUpload
                            ? null
                            : "棉再生纤定性",
                        ReviewUserNumber1 = 1,
                        FilePath = actualFilename,
                        FileType = "定量试验",
                    };
                    // 与官方客户端手工上传同形：图片类上传不写
                    // OriginalDataPictureFile 子记录。
                    var picturesToSave = new List<OriginalDataPictureFile>();
                    var dal = new SpecialWoolDAL();
                    dal.SaveSpecialWoolManage(
                        record,
                        new List<List<WoolFinenessRecord>>(),
                        picturesToSave,
                        new List<QuantificationTest>());
                    savedId = record.ID;
                }
                catch (Exception ex)
                {
                    result.Receipt["error"] = "main_record_save_failed:" + ex.GetType().Name;
                    result.Receipt["error_detail"] = SafeMessage(ex, password);
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitReconciliationRequired,
                        "main_record_save_started",
                        emit);
                }

                DataTable main = QueryMain(writeLock, actual);
                if (main.Rows.Count != 1)
                {
                    result.Receipt["error"] = "main_record_readback_count_mismatch";
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitReconciliationRequired,
                        "main_record_save_started",
                        emit);
                }
                DataRow mainRow = main.Rows[0];
                var mismatches = VerifyUploadMain(
                    mainRow,
                    savedId,
                    actual,
                    inspectorId,
                    actualFilename,
                    staff.Id,
                    imageUpload);
                if (mismatches.Count != 0)
                {
                    result.Receipt["error"] = "main_record_verify_mismatch";
                    result.Receipt["mismatched_fields"] = mismatches;
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitReconciliationRequired,
                        "main_record_save_started",
                        emit);
                }
                string mainId = Text(mainRow, "ID");
                stage("main_record_verified", new SortedDictionary<string, object>
                {
                    { "record_id", Redact.HashId(mainId) },
                    { "field_fingerprint", SpecialWoolContracts.FingerprintRow(mainRow, null) },
                });

                DataTable pictures = QueryPictures(writeLock, mainId);
                // 与官方客户端手工上传同形：图片类上传后也不应读回到
                // OriginalDataPictureFile 子记录。
                if (pictures.Rows.Count != 0)
                {
                    result.Receipt["error"] = "picture_child_readback_count_mismatch";
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitReconciliationRequired,
                        "main_record_save_started",
                        emit);
                }

                LegacyXlsFileVerification serverVerification;
                try
                {
                    // 单次读取远端缓冲区：raw SHA 与 CFB/BIFF 语义核验必须来自
                    // 同一快照，避免共享盘空闲扇区变化造成两次读取结果串线。
                    serverVerification = LegacyXlsFileVerifier.Verify(
                        source.Bytes,
                        File.ReadAllBytes(actualPath));
                }
                catch (Exception ex)
                {
                    result.Receipt["error"] =
                        "server_file_final_read_failed:" + ex.GetType().Name;
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitReconciliationRequired,
                        "main_record_save_started",
                        emit);
                }
                if (!serverVerification.Verified
                    || serverVerification.SourceSize != source.Size
                    || !string.Equals(
                        serverVerification.SourceSha256,
                        source.Sha256,
                        StringComparison.OrdinalIgnoreCase))
                {
                    result.Receipt["error"] =
                        "server_file_final_verify_mismatch";
                    result.Receipt["server_file_verification_error"] =
                        serverVerification.Error;
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitReconciliationRequired,
                        "main_record_save_started",
                        emit);
                }

                if (imageUpload)
                {
                    BuildImageReceipt(
                        result,
                        operation,
                        summary,
                        actual,
                        expectedProject,
                        source,
                        actualFilename,
                        mainRow,
                        null,
                        serverVerification);
                }
                else
                {
                    BuildQualitativeUploadReceipt(
                        result,
                        operation,
                        summary,
                        actual,
                        expectedProject,
                        source,
                        actualFilename,
                        mainRow,
                        serverVerification);
                }
            }

            stage("completed", null);
            return UploadExecutor.Finish(result, 0, null, emit);
        }

        private static int ExecuteReview(
            string fibreCheckDir,
            string account,
            string password,
            Dictionary<string, object> operation,
            Dictionary<string, object> summary,
            string operationType,
            UploadExecutor.Result result,
            Action<string, object> stage,
            Action<object> emit,
            Func<bool> awaitSideEffectPermit)
        {
            bool qualitativeReview = string.Equals(
                operationType,
                SpecialWoolContracts.QualitativeReviewOperation,
                StringComparison.Ordinal);
            // 与官方客户端手工上传同形：图片类记录同样没有
            // OriginalDataPictureFile 子记录，复核前后都必须为 0 条。
            const int expectedPictureCount = 0;
            string target = UploadExecutor.GetStr(summary, "target_sample_number");
            string sourceNumber = UploadExecutor.GetStr(summary, "source_inspection_number");
            Dictionary<string, object> expectedProject = UploadExecutor.GetMap(
                summary, "task_project");
            Dictionary<string, object> sourceOperation = UploadExecutor.GetMap(
                summary, "source_operation");
            string expectedMainHash = UploadExecutor.GetStr(
                sourceOperation, "main_id");
            if (!LegacyLoginFlow.IsValidSampleNo(target)
                || string.IsNullOrWhiteSpace(sourceNumber)
                || expectedProject == null
                || sourceOperation == null
                || string.IsNullOrWhiteSpace(
                    UploadExecutor.GetStr(sourceOperation, "operation_id"))
                || string.IsNullOrWhiteSpace(
                    UploadExecutor.GetStr(sourceOperation, "receipt_checksum"))
                || !SpecialWoolContracts.IsRedactedId(expectedMainHash))
            {
                return PackageFailure(result, emit, "review_package_fields_invalid");
            }

            LegacyLoginFlow.StaffContext staff;
            SortedDictionary<string, object> loginDocument;
            int loginExit = Authenticate(
                fibreCheckDir, account, password, ReviewFunctionType,
                true, stage, out staff, out loginDocument);
            if (loginExit != 0 || staff == null)
            {
                result.Receipt["error"] = "login_or_permission_failed:" + loginExit;
                result.Receipt["login"] = loginDocument;
                return UploadExecutor.Finish(result, loginExit, null, emit);
            }
            BindStaff(account, password, staff);

            string connectionString = SystemDataConnection.Read(
                fibreCheckDir, "DbConnString");
            TaskProject project;
            DataTable preMain;
            DataTable prePictures;
            string preMainFingerprint;
            string preInvariantFingerprint;
            string preChildrenFingerprint;
            string mainId;
            using (var db = new OdpNetDb(connectionString))
            {
                db.OpenReadOnly();
                string projectError;
                project = ResolveTaskProject(
                    db, sourceNumber, expectedProject, out projectError);
                if (project == null)
                {
                    return PackageFailure(result, emit, projectError);
                }
                preMain = db.Query(
                    ExactMainSql,
                    new List<DbParam>
                    {
                        new DbParam("target_sample_no", target),
                    });
                if (preMain.Rows.Count != 1)
                {
                    result.Receipt["error"] = "review_target_main_count_mismatch";
                    return UploadExecutor.Finish(
                        result, ExitCodes.DryRunConflict, null, emit);
                }
                DataRow row = preMain.Rows[0];
                if (!IsNull(row, "ReviewUser") || !IsNull(row, "ReviewTime"))
                {
                    result.Receipt["error"] = "review_target_already_reviewed";
                    return UploadExecutor.Finish(
                        result, ExitCodes.DryRunConflict, null, emit);
                }
                mainId = Text(row, "ID");
                if (!SpecialWoolContracts.MatchesRedactedId(
                    expectedMainHash, mainId))
                {
                    result.Receipt["error"] = "review_target_main_id_mismatch";
                    return UploadExecutor.Finish(
                        result, ExitCodes.DryRunConflict, null, emit);
                }
                prePictures = db.Query(
                    PictureChildrenSql,
                    new List<DbParam> { new DbParam("main_id", mainId) });
                if (prePictures.Rows.Count != expectedPictureCount)
                {
                    result.Receipt["error"] = "review_picture_child_count_mismatch";
                    return UploadExecutor.Finish(
                        result, ExitCodes.DryRunConflict, null, emit);
                }
                preMainFingerprint = SpecialWoolContracts.FingerprintRow(row, null);
                preInvariantFingerprint = SpecialWoolContracts.FingerprintRow(
                    row,
                    new HashSet<string>(StringComparer.Ordinal)
                    {
                        "ReviewUser",
                        "ReviewTime",
                    });
                preChildrenFingerprint = SpecialWoolContracts.FingerprintRows(
                    prePictures);
            }

            stage("remote_state_verified", new SortedDictionary<string, object>
            {
                { "target_sample_number", target },
                { "main_id", Redact.HashId(mainId) },
                { "picture_count", prePictures.Rows.Count },
                { "children_fingerprint", preChildrenFingerprint },
            });
            stage("review_save_ready", new SortedDictionary<string, object>
            {
                { "main_id", Redact.HashId(mainId) },
                { "will_update", new[] { "ReviewUser", "ReviewTime" } },
            });
            if (awaitSideEffectPermit == null || !awaitSideEffectPermit())
            {
                result.Receipt["error"] = "side_effect_permit_denied";
                return UploadExecutor.Finish(
                    result, UploadExecutor.ExitSideEffectPermitDenied, null, emit);
            }

            using (SpecialWoolWriteLock writeLock = AcquireLock(
                connectionString, project, "review_save_started", result, emit))
            {
                if (writeLock == null)
                {
                    return UploadExecutor.ExitReconciliationRequired;
                }
                DataTable lockedMain = QueryMain(writeLock, target);
                if (lockedMain.Rows.Count != 1
                    || !string.Equals(
                        Text(lockedMain.Rows[0], "ID"), mainId,
                        StringComparison.Ordinal)
                    || !IsNull(lockedMain.Rows[0], "ReviewUser")
                    || !IsNull(lockedMain.Rows[0], "ReviewTime")
                    || !string.Equals(
                        SpecialWoolContracts.FingerprintRow(
                            lockedMain.Rows[0], null),
                        preMainFingerprint,
                        StringComparison.Ordinal))
                {
                    result.Receipt["error"] = "review_main_changed_under_lock";
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitReconciliationRequired,
                        "review_save_started",
                        emit);
                }
                DataTable lockedPictures = QueryPictures(writeLock, mainId);
                if (lockedPictures.Rows.Count != prePictures.Rows.Count
                    || !string.Equals(
                        SpecialWoolContracts.FingerprintRows(lockedPictures),
                        preChildrenFingerprint,
                        StringComparison.Ordinal))
                {
                    result.Receipt["error"] = "review_children_changed_under_lock";
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitReconciliationRequired,
                        "review_save_started",
                        emit);
                }

                object serverDateValue = writeLock.Scalar(
                    LegacyLoginFlow.SysDateSql, new List<LockParam>());
                if (!(serverDateValue is DateTime))
                {
                    result.Receipt["error"] = "server_time_unavailable";
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitReconciliationRequired,
                        "review_save_started",
                        emit);
                }
                DateTime reviewTime = (DateTime)serverDateValue;
                stage("review_save_started", new SortedDictionary<string, object>
                {
                    { "main_id", Redact.HashId(mainId) },
                });
                try
                {
                    var dal = new SpecialWoolDAL();
                    SpecialWoolManage record = dal.Get(mainId);
                    if (record == null
                        || !string.Equals(record.SampleNo, target, StringComparison.Ordinal)
                        || !string.IsNullOrWhiteSpace(record.ReviewUser)
                        || record.ReviewTime.HasValue)
                    {
                        throw new InvalidOperationException("review_target_changed");
                    }
                    record.ReviewUser = staff.Id;
                    record.ReviewTime = reviewTime;
                    dal.SaveSpecialWoolManage(record, null, null, null);
                }
                catch (Exception ex)
                {
                    result.Receipt["error"] = "review_save_failed:" + ex.GetType().Name;
                    result.Receipt["error_detail"] = SafeMessage(ex, password);
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitReconciliationRequired,
                        "review_save_started",
                        emit);
                }

                DataTable postMain = QueryMain(writeLock, target);
                var mismatches = new List<string>();
                if (postMain.Rows.Count != 1)
                {
                    mismatches.Add("main_count");
                }
                DataRow postRow = postMain.Rows.Count == 1
                    ? postMain.Rows[0]
                    : null;
                if (postRow != null)
                {
                    if (!string.Equals(Text(postRow, "ID"), mainId, StringComparison.Ordinal))
                        mismatches.Add("ID");
                    if (!string.Equals(Text(postRow, "ReviewUser"), staff.Id, StringComparison.Ordinal))
                        mismatches.Add("ReviewUser");
                    DateTime? savedReviewTime = NullableDate(postRow, "ReviewTime");
                    if (!savedReviewTime.HasValue
                        || Math.Abs((savedReviewTime.Value - reviewTime).TotalSeconds) > 1.0)
                        mismatches.Add("ReviewTime");
                    string postInvariantFingerprint = SpecialWoolContracts.FingerprintRow(
                        postRow,
                        new HashSet<string>(StringComparer.Ordinal)
                        {
                            "ReviewUser",
                            "ReviewTime",
                        });
                    if (!string.Equals(
                        postInvariantFingerprint,
                        preInvariantFingerprint,
                        StringComparison.Ordinal))
                        mismatches.Add("unexpected_main_fields");
                }
                if (mismatches.Count != 0)
                {
                    result.Receipt["error"] = "review_main_verify_mismatch";
                    result.Receipt["mismatched_fields"] = mismatches;
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitReconciliationRequired,
                        "review_save_started",
                        emit);
                }
                string postMainFingerprint = SpecialWoolContracts.FingerprintRow(
                    postRow, null);
                stage("review_main_verified", new SortedDictionary<string, object>
                {
                    { "main_id", Redact.HashId(mainId) },
                    { "review_user", Redact.HashId(staff.Id) },
                    { "review_time", Iso(NullableDate(postRow, "ReviewTime")) },
                });

                DataTable postPictures = QueryPictures(writeLock, mainId);
                string postChildrenFingerprint = SpecialWoolContracts.FingerprintRows(
                    postPictures);
                if (postPictures.Rows.Count != prePictures.Rows.Count
                    || !string.Equals(
                        postChildrenFingerprint,
                        preChildrenFingerprint,
                        StringComparison.Ordinal))
                {
                    result.Receipt["error"] = "review_children_verify_mismatch";
                    return UploadExecutor.Finish(
                        result,
                        UploadExecutor.ExitReconciliationRequired,
                        "review_save_started",
                        emit);
                }
                if (!qualitativeReview)
                {
                    stage("review_children_verified", new SortedDictionary<string, object>
                    {
                        { "picture_count", postPictures.Rows.Count },
                        { "children_fingerprint", postChildrenFingerprint },
                    });
                }

                BuildReviewReceipt(
                    result,
                    operation,
                    summary,
                    sourceOperation,
                    mainId,
                    staff.Id,
                    NullableDate(postRow, "ReviewTime"),
                    preMainFingerprint,
                    postMainFingerprint,
                    postPictures.Rows.Count,
                    preChildrenFingerprint,
                    postChildrenFingerprint,
                    qualitativeReview);
            }

            stage("completed", null);
            return UploadExecutor.Finish(result, 0, null, emit);
        }

        private static string ValidatePackageContract(
            Dictionary<string, object> operation,
            Dictionary<string, object> summary,
            string operationType)
        {
            if (operation == null || summary == null
                || UploadExecutor.GetStr(operation, "id") == null
                || UploadExecutor.GetStr(operation, "payload_checksum") == null)
            {
                return "package_operation_identity_missing";
            }
            if (UploadExecutor.GetLong(summary, "schema_version") != 1)
            {
                return "request_summary_schema_unsupported";
            }
            bool imageUpload = string.Equals(
                operationType,
                SpecialWoolContracts.ImageOperation,
                StringComparison.Ordinal);
            bool imageReview = string.Equals(
                operationType,
                SpecialWoolContracts.ReviewOperation,
                StringComparison.Ordinal);
            bool qualitativeUpload = string.Equals(
                operationType,
                SpecialWoolContracts.QualitativeUploadOperation,
                StringComparison.Ordinal);
            bool qualitativeReview = string.Equals(
                operationType,
                SpecialWoolContracts.QualitativeReviewOperation,
                StringComparison.Ordinal);
            if (!imageUpload && !imageReview
                && !qualitativeUpload && !qualitativeReview)
            {
                return "operation_type_unsupported";
            }
            string expectedProfile = imageUpload
                ? "special_wool_image_v1"
                : imageReview
                    ? "special_wool_review_v1"
                    : qualitativeUpload
                        ? "special_wool_qualitative_upload_v1"
                        : "special_wool_qualitative_review_v1";
            if (!string.Equals(
                UploadExecutor.GetStr(summary, "profile"),
                expectedProfile,
                StringComparison.Ordinal))
            {
                return "request_summary_profile_mismatch";
            }
            Dictionary<string, object> capability = UploadExecutor.GetMap(
                summary, "execution_capability");
            object available;
            if (capability == null
                || !capability.TryGetValue("available", out available)
                || !(available is bool)
                || !(bool)available)
            {
                return "writer_capability_not_authorized_by_backend";
            }
            Dictionary<string, object> contract = UploadExecutor.GetMap(
                summary, "machine_contract");
            if (contract == null
                || UploadExecutor.GetLong(contract, "schema_version") != 1)
            {
                return "machine_contract_missing";
            }
            object readOnlyProbeRequired;
            if (!contract.TryGetValue(
                    "read_only_probe_required", out readOnlyProbeRequired)
                || !(readOnlyProbeRequired is bool)
                || !(bool)readOnlyProbeRequired)
            {
                return "machine_contract_readonly_probe_required";
            }
            string expectedObservation = imageUpload
                ? SpecialWoolContracts.ImageObservationType
                : imageReview
                    ? SpecialWoolContracts.ReviewObservationType
                    : qualitativeUpload
                        ? SpecialWoolContracts.QualitativeUploadObservationType
                        : SpecialWoolContracts.QualitativeReviewObservationType;
            string expectedReceipt = imageUpload
                ? SpecialWoolContracts.ImageReceiptType
                : imageReview
                    ? SpecialWoolContracts.ReviewReceiptType
                    : qualitativeUpload
                        ? SpecialWoolContracts.QualitativeUploadReceiptType
                        : SpecialWoolContracts.QualitativeReviewReceiptType;
            if (!string.Equals(
                    UploadExecutor.GetStr(contract, "observation_type"),
                    expectedObservation,
                    StringComparison.Ordinal)
                || !string.Equals(
                    UploadExecutor.GetStr(contract, "receipt_type"),
                    expectedReceipt,
                    StringComparison.Ordinal))
            {
                return "machine_contract_type_mismatch";
            }
            Dictionary<string, object> business = UploadExecutor.GetMap(
                summary, "business_fields");
            string expectedFiberCategory = imageUpload || imageReview
                ? "图片"
                : "棉再生纤";
            if (business == null
                || !string.Equals(
                    UploadExecutor.GetStr(business, "fiber_category"),
                    expectedFiberCategory,
                    StringComparison.Ordinal))
            {
                return "business_contract_mismatch";
            }
            if (imageUpload || qualitativeUpload)
            {
                string expectedTargetFilename;
                try
                {
                    if (imageUpload)
                    {
                        expectedTargetFilename =
                            SpecialWoolContracts.BuildTargetFilename(
                                UploadExecutor.GetStr(
                                    summary, "target_sample_number"));
                    }
                    else
                    {
                        List<object> files = UploadExecutor.GetList(
                            summary, "files");
                        Dictionary<string, object> file = files.Count == 1
                            ? UploadExecutor.GetMap(files[0])
                            : null;
                        expectedTargetFilename =
                            SpecialWoolContracts.BuildPrefixedTargetFilename(
                                UploadExecutor.GetStr(
                                    summary, "target_sample_number"),
                                UploadExecutor.GetStr(file, "filename"));
                    }
                }
                catch (ArgumentException)
                {
                    return "target_filename_invalid";
                }
                if (!string.Equals(
                    UploadExecutor.GetStr(summary, "target_filename"),
                    expectedTargetFilename,
                    StringComparison.Ordinal))
                {
                    return "target_filename_mismatch";
                }
                string expectedMethod = imageUpload ? string.Empty : "定量";
                string expectedItem = imageUpload ? "图片" : "棉再生纤定性";
                // 与官方客户端手工上传同形：图片类复核项目为 NULL，后端同步发空串。
                string expectedReviewItem = imageUpload
                    ? string.Empty
                    : "棉再生纤定性";
                if (!string.Equals(
                        UploadExecutor.GetStr(business, "inspection_method")
                            ?? string.Empty,
                        expectedMethod,
                        StringComparison.Ordinal)
                    || !string.Equals(
                        UploadExecutor.GetStr(business, "inspection_item"),
                        expectedItem,
                        StringComparison.Ordinal)
                    || UploadExecutor.GetLong(business, "inspection_copies") != 1
                    || !string.Equals(
                        UploadExecutor.GetStr(business, "review_item")
                            ?? string.Empty,
                        expectedReviewItem,
                        StringComparison.Ordinal)
                    || UploadExecutor.GetLong(business, "review_copies") != 1)
                {
                    return imageUpload
                        ? "image_business_contract_mismatch"
                        : "qualitative_business_contract_mismatch";
                }
            }
            else
            {
                string expectedReviewItem = imageReview
                    ? string.Empty
                    : "棉再生纤定性";
                if (!string.Equals(
                        UploadExecutor.GetStr(business, "review_action"),
                        "特纤复核",
                        StringComparison.Ordinal)
                    || !string.Equals(
                        UploadExecutor.GetStr(business, "review_item")
                            ?? string.Empty,
                        expectedReviewItem,
                        StringComparison.Ordinal)
                    || UploadExecutor.GetLong(
                        business, "review_copies") != 1)
                {
                    return imageReview
                        ? "review_business_contract_mismatch"
                        : "qualitative_review_business_contract_mismatch";
                }
                Dictionary<string, object> source = UploadExecutor.GetMap(
                    summary, "source_operation");
                Dictionary<string, object> machine = UploadExecutor.GetMap(
                    operation, "machine_payload");
                Dictionary<string, object> machineSource = UploadExecutor.GetMap(
                    machine, "source_upload");
                if (source == null
                    || machine == null
                    || machineSource == null
                    || UploadExecutor.GetLong(machine, "schema_version") != 1
                    || !string.Equals(
                        UploadExecutor.GetStr(machine, "operation_type"),
                        operationType,
                        StringComparison.Ordinal)
                    || !string.Equals(
                        UploadExecutor.GetStr(machine, "target_sample_number"),
                        UploadExecutor.GetStr(summary, "target_sample_number"),
                        StringComparison.Ordinal)
                    || !SpecialWoolContracts.IsRedactedId(
                        UploadExecutor.GetStr(source, "main_id")))
                {
                    return "review_machine_identity_binding_missing";
                }
                foreach (string key in new[]
                {
                    "operation_id",
                    "payload_checksum",
                    "receipt_checksum",
                    "main_id",
                })
                {
                    if (!string.Equals(
                        UploadExecutor.GetStr(source, key),
                        UploadExecutor.GetStr(machineSource, key),
                        StringComparison.Ordinal))
                    {
                        return "review_machine_identity_binding_mismatch";
                    }
                }
            }
            return null;
        }

        private static int Authenticate(
            string fibreCheckDir,
            string account,
            string password,
            string functionType,
            bool requireReviewControl,
            Action<string, object> stage,
            out LegacyLoginFlow.StaffContext staff,
            out SortedDictionary<string, object> document)
        {
            var options = new RunnerOptions
            {
                FibreCheckDir = fibreCheckDir,
                Account = account,
                Password = password,
                FunctionType = functionType,
            };
            int exit = LegacyLoginFlow.RunCore(
                options,
                connection => new OdpNetDb(connection),
                out document,
                out staff);
            if (exit != 0 || staff == null)
            {
                return exit;
            }
            stage("authenticated", null);
            if (requireReviewControl && !ReviewControlGranted(document))
            {
                staff = null;
                return ExitCodes.PermissionDenied;
            }
            stage("permission_verified", new SortedDictionary<string, object>
            {
                { "function_type", functionType },
                { "btn_check_required_if_configured", requireReviewControl },
            });
            return 0;
        }

        private static bool ReviewControlGranted(
            SortedDictionary<string, object> document)
        {
            object value;
            if (document == null
                || !document.TryGetValue("control_purviews", out value))
            {
                return true;
            }
            IEnumerable values = value as IEnumerable;
            if (values == null)
            {
                return true;
            }
            bool configured = false;
            bool enabled = false;
            foreach (object item in values)
            {
                var map = item as SortedDictionary<string, object>;
                if (map == null)
                {
                    continue;
                }
                object identity;
                if (map.TryGetValue("control_identity", out identity)
                    && string.Equals(
                        Convert.ToString(identity),
                        "btnCheck",
                        StringComparison.OrdinalIgnoreCase))
                {
                    configured = true;
                    object state;
                    enabled = enabled || (
                        map.TryGetValue("enabled", out state)
                        && state is bool
                        && (bool)state);
                }
            }
            return !configured || enabled;
        }

        private static void BindStaff(
            string account,
            string password,
            LegacyLoginFlow.StaffContext staff)
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

        private static TaskProject ResolveTaskProject(
            ILegacyDb db,
            string sourceNumber,
            Dictionary<string, object> expected,
            out string error)
        {
            error = null;
            string expectedKey = UploadExecutor.GetStr(expected, "project_key");
            string expectedTaskItem = UploadExecutor.GetStr(
                expected, "task_check_item_id");
            string expectedItem = UploadExecutor.GetStr(expected, "check_item_id");
            object expectedCheckCount;
            expected.TryGetValue("check_count", out expectedCheckCount);
            if (string.IsNullOrWhiteSpace(expectedKey)
                || string.IsNullOrWhiteSpace(expectedTaskItem)
                || string.IsNullOrWhiteSpace(expectedItem))
            {
                error = "task_project_contract_invalid";
                return null;
            }
            DataTable table = db.Query(
                ProjectSql,
                new List<DbParam> { new DbParam("sample_no", sourceNumber) });
            var matches = new List<TaskProject>();
            foreach (DataRow row in table.Rows)
            {
                string rawTaskItem = Text(row, "TaskCheckItemID");
                string rawItem = Text(row, "CheckItemID");
                string key = SpecialWoolContracts.ProjectKey(
                    rawTaskItem,
                    rawItem,
                    Text(row, "CheckItemNo"),
                    Text(row, "CheckItemName"),
                    Text(row, "CheckMethod"),
                    row["SeqNum"]);
                if (string.Equals(key, expectedKey, StringComparison.Ordinal)
                    && string.Equals(
                        Redact.HashId(rawTaskItem),
                        expectedTaskItem,
                        StringComparison.Ordinal)
                    && string.Equals(
                        Redact.HashId(rawItem),
                        expectedItem,
                        StringComparison.Ordinal)
                    && SameExpected(row, "CheckItemNo", expected, "check_item_no")
                    && SameExpected(row, "CheckItemName", expected, "check_item_name")
                    && SameExpected(row, "CheckMethod", expected, "check_method")
                    && SameExpected(row, "SeqNum", expected, "seq_num")
                    && SpecialWoolContracts.MatchesExpectedPositiveCheckCount(
                        row["CheckCount"], expectedCheckCount))
                {
                    matches.Add(new TaskProject
                    {
                        TaskId = Text(row, "TaskID"),
                        TaskCheckItemId = rawTaskItem,
                        CheckItemId = rawItem,
                    });
                }
            }
            if (matches.Count != 1)
            {
                error = matches.Count == 0
                    ? "task_project_not_found"
                    : "task_project_not_unique";
                return null;
            }
            return matches[0];
        }

        private static bool SameExpected(
            DataRow row,
            string column,
            Dictionary<string, object> expected,
            string key)
        {
            object expectedValue;
            expected.TryGetValue(key, out expectedValue);
            return string.Equals(
                SpecialWoolContracts.NormalizeBusinessText(row[column]),
                SpecialWoolContracts.NormalizeBusinessText(expectedValue),
                StringComparison.Ordinal);
        }

        private static List<string> ReadOccupied(ILegacyDb db, string baseNumber)
        {
            DataTable table = db.Query(
                FamilySql,
                new List<DbParam>
                {
                    new DbParam("target_base", baseNumber),
                    new DbParam("target_prefix", baseNumber + "-%"),
                });
            var result = new List<string>();
            foreach (DataRow row in table.Rows)
            {
                result.Add(Text(row, "SampleNo"));
            }
            return result;
        }

        private static List<string> ReadOccupiedUnderLock(
            SpecialWoolWriteLock writeLock,
            string baseNumber)
        {
            DataTable table = writeLock.Query(
                FamilySql,
                new List<LockParam>
                {
                    new LockParam("target_base", baseNumber),
                    new LockParam("target_prefix", baseNumber + "-%"),
                });
            var occupied = new List<string>();
            foreach (DataRow row in table.Rows)
            {
                occupied.Add(Text(row, "SampleNo"));
            }
            return occupied;
        }

        private static SpecialWoolWriteLock AcquireLock(
            string connectionString,
            TaskProject project,
            string boundaryStage,
            UploadExecutor.Result result,
            Action<object> emit)
        {
            try
            {
                return SpecialWoolWriteLock.Acquire(
                    connectionString,
                    project.TaskCheckItemId,
                    project.CheckItemId);
            }
            catch (Exception ex)
            {
                result.Receipt["error"] = "project_write_lock_unavailable:" + ex.GetType().Name;
                UploadExecutor.Finish(
                    result,
                    UploadExecutor.ExitReconciliationRequired,
                    boundaryStage,
                    emit);
                return null;
            }
        }

        private static bool TryReadSource(
            Dictionary<string, object> summary,
            string sourceRoot,
            out SourceArtifact source,
            out string error)
        {
            source = null;
            error = null;
            List<object> files = UploadExecutor.GetList(summary, "files");
            if (files.Count != 1 || string.IsNullOrWhiteSpace(sourceRoot))
            {
                error = "first_write_supports_single_file_only";
                return false;
            }
            Dictionary<string, object> entry = UploadExecutor.GetMap(files[0]);
            string artifactId = UploadExecutor.GetStr(entry, "artifact_id");
            string relativePath = UploadExecutor.GetStr(entry, "relative_path");
            string fileName = UploadExecutor.GetStr(entry, "filename");
            string expectedSha = UploadExecutor.GetStr(entry, "content_sha256");
            long expectedSize = UploadExecutor.GetLong(entry, "size_bytes");
            if (string.IsNullOrWhiteSpace(artifactId)
                || string.IsNullOrWhiteSpace(relativePath)
                || string.IsNullOrWhiteSpace(fileName)
                || string.IsNullOrWhiteSpace(expectedSha)
                || expectedSize < 0)
            {
                error = "package_file_entry_invalid";
                return false;
            }
            if (!string.Equals(Path.GetFileName(fileName), fileName, StringComparison.Ordinal)
                || fileName == "."
                || fileName == ".."
                || fileName.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
            {
                error = "package_filename_invalid";
                return false;
            }
            string root;
            string path;
            try
            {
                root = Path.GetFullPath(sourceRoot);
                if (!root.EndsWith(Path.DirectorySeparatorChar.ToString(), StringComparison.Ordinal)
                    && !root.EndsWith(Path.AltDirectorySeparatorChar.ToString(), StringComparison.Ordinal))
                {
                    root += Path.DirectorySeparatorChar;
                }
                path = Path.GetFullPath(Path.Combine(
                    root,
                    relativePath.Replace('/', Path.DirectorySeparatorChar)));
            }
            catch (Exception)
            {
                error = "package_relative_path_invalid";
                return false;
            }
            if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase))
            {
                error = "package_relative_path_outside_source_root";
                return false;
            }
            byte[] bytes;
            try
            {
                bytes = File.ReadAllBytes(path);
            }
            catch (Exception ex)
            {
                error = "source_unreadable:" + ex.GetType().Name;
                return false;
            }
            string sha = UploadExecutor.Sha256Hex(bytes);
            if (bytes.LongLength != expectedSize
                || !string.Equals(sha, expectedSha, StringComparison.OrdinalIgnoreCase))
            {
                error = "source_content_changed";
                return false;
            }
            source = new SourceArtifact
            {
                ArtifactId = artifactId,
                RelativePath = relativePath,
                FileName = fileName,
                Path = path,
                Bytes = bytes,
                Size = bytes.LongLength,
                Sha256 = sha,
            };
            return true;
        }

        private static DataTable QueryMain(
            SpecialWoolWriteLock writeLock,
            string target)
        {
            return writeLock.Query(
                ExactMainSql,
                new List<LockParam>
                {
                    new LockParam("target_sample_no", target),
                });
        }

        private static DataTable QueryPictures(
            SpecialWoolWriteLock writeLock,
            string mainId)
        {
            return writeLock.Query(
                PictureChildrenSql,
                new List<LockParam>
                {
                    new LockParam("main_id", mainId),
                });
        }

        private static List<string> VerifyImageMain(
            DataRow row,
            string savedId,
            string target,
            string inspectorId,
            string fileName,
            string staffId)
        {
            return VerifyUploadMain(
                row, savedId, target, inspectorId, fileName, staffId, true);
        }

        private static List<string> VerifyUploadMain(
            DataRow row,
            string savedId,
            string target,
            string inspectorId,
            string fileName,
            string staffId,
            bool imageUpload)
        {
            var mismatches = new List<string>();
            Check(mismatches, "ID", Text(row, "ID"), savedId);
            Check(mismatches, "SampleNo", Text(row, "SampleNo"), target);
            Check(
                mismatches,
                "FibreSort",
                Text(row, "FibreSort"),
                imageUpload ? "图片" : "棉再生纤");
            Check(
                mismatches,
                "CheckWay",
                Text(row, "CheckWay"),
                imageUpload ? string.Empty : "定量");
            if (!SpecialWoolContracts.MatchesBusinessText(
                Text(row, "CheckUser1"), inspectorId))
            {
                mismatches.Add("CheckUser1");
            }
            string expectedCheckItem = imageUpload ? "图片" : "棉再生纤定性";
            // 与官方客户端手工上传同形：图片类复核项目保持 NULL，读回为空串。
            string expectedReviewItem = imageUpload
                ? string.Empty
                : "棉再生纤定性";
            Check(
                mismatches,
                "CheckUserItem1",
                Text(row, "CheckUserItem1"),
                expectedCheckItem);
            Check(
                mismatches,
                "ReviewUserItem1",
                Text(row, "ReviewUserItem1"),
                expectedReviewItem);
            Check(mismatches, "FilePath", Text(row, "FilePath"), fileName);
            Check(
                mismatches,
                "FileType",
                Text(row, "FileType"),
                "定量试验");
            Check(mismatches, "CreateUser", Text(row, "CreateUser"), staffId);
            CheckInt(mismatches, row, "CheckUserNumber1", 1);
            CheckInt(mismatches, row, "ReviewUserNumber1", 1);
            if (IsNull(row, "CreateTime")) mismatches.Add("CreateTime");
            return mismatches;
        }

        private static void BuildImageReceipt(
            UploadExecutor.Result result,
            Dictionary<string, object> operation,
            Dictionary<string, object> summary,
            string actualSampleNumber,
            Dictionary<string, object> expectedProject,
            SourceArtifact source,
            string targetFilename,
            DataRow main,
            DataRow picture,
            LegacyXlsFileVerification serverVerification)
        {
            string mainId = Text(main, "ID");
            // 图片类记录不再写 OriginalDataPictureFile 子表；
            // original_data_filename 沿用主行 FilePath（与 targetFilename 同值），
            // 保持回执契约字段不变。
            string originalDataFilename = Text(main, "FilePath");
            string requested = UploadExecutor.GetStr(summary, "target_sample_number");
            result.Receipt["schema_version"] = 1;
            result.Receipt["receipt_type"] = SpecialWoolContracts.ImageReceiptType;
            result.Receipt["operation_id"] = UploadExecutor.GetStr(operation, "id");
            result.Receipt["payload_checksum"] = UploadExecutor.GetStr(operation, "payload_checksum");
            result.Receipt["target_sample_number"] = actualSampleNumber;
            result.Receipt["requested_sample_number"] = requested;
            result.Receipt["renumbered"] = !string.Equals(
                actualSampleNumber, requested, StringComparison.Ordinal);
            result.Receipt["target_filename"] = targetFilename;
            result.Receipt["source_artifact"] = new SortedDictionary<string, object>
            {
                { "artifact_id", source.ArtifactId },
                { "filename", source.FileName },
                { "size_bytes", source.Size },
                { "content_sha256", source.Sha256 },
            };
            result.Receipt["task_project"] = CopyProject(expectedProject);
            result.Receipt["server_file"] = new SortedDictionary<string, object>
            {
                { "filename", targetFilename },
                { "size_bytes", serverVerification.RemoteSize },
                { "content_sha256", serverVerification.RemoteSha256 },
                { "verification", serverVerification.ToDocument() },
            };
            result.Receipt["main_record"] = new SortedDictionary<string, object>
            {
                { "id", Redact.HashId(mainId) },
                { "field_fingerprint", SpecialWoolContracts.FingerprintRow(main, null) },
                { "create_user", Redact.HashId(Text(main, "CreateUser")) },
                { "create_time", Iso(NullableDate(main, "CreateTime")) },
                { "file_path", Text(main, "FilePath") },
            };
            // 与官方客户端手工上传同形：无 OriginalDataPictureFile 子记录。
            result.Receipt["picture_records"] = new List<object>();
            result.Receipt["readback"] = new SortedDictionary<string, object>
            {
                { "main_count", 1 },
                { "picture_count", 0 },
                { "mismatches", new List<string>() },
                { "target_filename", targetFilename },
                { "original_data_filename", originalDataFilename },
                { "verified_at", DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") },
            };
        }

        private static void BuildQualitativeUploadReceipt(
            UploadExecutor.Result result,
            Dictionary<string, object> operation,
            Dictionary<string, object> summary,
            string actualSampleNumber,
            Dictionary<string, object> expectedProject,
            SourceArtifact source,
            string targetFilename,
            DataRow main,
            LegacyXlsFileVerification serverVerification)
        {
            string mainId = Text(main, "ID");
            string requested = UploadExecutor.GetStr(summary, "target_sample_number");
            result.Receipt["schema_version"] = 1;
            result.Receipt["receipt_type"] =
                SpecialWoolContracts.QualitativeUploadReceiptType;
            result.Receipt["operation_id"] =
                UploadExecutor.GetStr(operation, "id");
            result.Receipt["payload_checksum"] =
                UploadExecutor.GetStr(operation, "payload_checksum");
            result.Receipt["target_sample_number"] = actualSampleNumber;
            result.Receipt["requested_sample_number"] = requested;
            result.Receipt["renumbered"] = !string.Equals(
                actualSampleNumber, requested, StringComparison.Ordinal);
            result.Receipt["target_filename"] = targetFilename;
            result.Receipt["picture_count"] = 0;
            result.Receipt["source_artifact"] =
                new SortedDictionary<string, object>
                {
                    { "artifact_id", source.ArtifactId },
                    { "filename", source.FileName },
                    { "size_bytes", source.Size },
                    { "content_sha256", source.Sha256 },
                };
            result.Receipt["task_project"] = CopyProject(expectedProject);
            result.Receipt["server_file"] =
                new SortedDictionary<string, object>
                {
                    { "filename", targetFilename },
                    { "size_bytes", serverVerification.RemoteSize },
                    { "content_sha256", serverVerification.RemoteSha256 },
                    { "verification", serverVerification.ToDocument() },
                };
            result.Receipt["main_record"] =
                new SortedDictionary<string, object>
                {
                    { "id", Redact.HashId(mainId) },
                    { "field_fingerprint",
                        SpecialWoolContracts.FingerprintRow(main, null) },
                    { "create_user", Redact.HashId(Text(main, "CreateUser")) },
                    { "create_time", Iso(NullableDate(main, "CreateTime")) },
                    { "file_path", Text(main, "FilePath") },
                };
            result.Receipt["readback"] =
                new SortedDictionary<string, object>
                {
                    { "main_count", 1 },
                    { "picture_count", 0 },
                    { "mismatches", new List<string>() },
                    { "target_filename", targetFilename },
                    { "verified_at",
                        DateTime.UtcNow.ToString(
                            "yyyy-MM-ddTHH:mm:ss.fffffffZ") },
                };
        }

        private static void BuildReviewReceipt(
            UploadExecutor.Result result,
            Dictionary<string, object> operation,
            Dictionary<string, object> summary,
            Dictionary<string, object> sourceOperation,
            string mainId,
            string reviewUser,
            DateTime? reviewTime,
            string preMainFingerprint,
            string postMainFingerprint,
            int pictureCount,
            string beforeChildren,
            string afterChildren,
            bool qualitativeReview)
        {
            string mainHash = Redact.HashId(mainId);
            result.Receipt["schema_version"] = 1;
            result.Receipt["receipt_type"] = qualitativeReview
                ? SpecialWoolContracts.QualitativeReviewReceiptType
                : SpecialWoolContracts.ReviewReceiptType;
            result.Receipt["operation_id"] = UploadExecutor.GetStr(operation, "id");
            result.Receipt["payload_checksum"] = UploadExecutor.GetStr(operation, "payload_checksum");
            result.Receipt["target_sample_number"] = UploadExecutor.GetStr(summary, "target_sample_number");
            result.Receipt["source_upload"] = new SortedDictionary<string, object>
            {
                { "operation_id", UploadExecutor.GetStr(sourceOperation, "operation_id") },
                { "receipt_checksum", UploadExecutor.GetStr(sourceOperation, "receipt_checksum") },
                { "main_id", mainHash },
            };
            result.Receipt["main_record"] = new SortedDictionary<string, object>
            {
                { "id", mainHash },
                { "review_user", Redact.HashId(reviewUser) },
                { "review_time", Iso(reviewTime) },
                { "pre_fingerprint", preMainFingerprint },
                { "post_fingerprint", postMainFingerprint },
            };
            result.Receipt["children"] = new SortedDictionary<string, object>
            {
                { "picture_count", pictureCount },
                { "before_fingerprint", beforeChildren },
                { "after_fingerprint", afterChildren },
                { "unchanged", string.Equals(beforeChildren, afterChildren, StringComparison.Ordinal) },
            };
            result.Receipt["readback"] = new SortedDictionary<string, object>
            {
                { "main_count", 1 },
                { "mismatches", new List<string>() },
                { "verified_at", DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") },
            };
        }

        private static SortedDictionary<string, object> CopyProject(
            Dictionary<string, object> source)
        {
            var result = new SortedDictionary<string, object>();
            foreach (string key in new[]
            {
                "project_key",
                "task_check_item_id",
                "check_item_id",
                "check_item_no",
                "check_item_name",
                "check_method",
                "seq_num",
                "check_count",
            })
            {
                object value;
                source.TryGetValue(key, out value);
                result[key] = value;
            }
            return result;
        }

        private static int PackageFailure(
            UploadExecutor.Result result,
            Action<object> emit,
            string code)
        {
            result.Receipt["error"] = code ?? "package_invalid";
            return UploadExecutor.Finish(
                result, UploadExecutor.ExitPackageError, null, emit);
        }

        private static void Check(
            List<string> mismatches,
            string field,
            string actual,
            string expected)
        {
            if (!string.Equals(actual ?? string.Empty, expected ?? string.Empty, StringComparison.Ordinal))
            {
                mismatches.Add(field);
            }
        }

        private static void CheckInt(
            List<string> mismatches,
            DataRow row,
            string field,
            int expected)
        {
            try
            {
                if (row[field] == DBNull.Value
                    || Convert.ToInt32(row[field], CultureInfo.InvariantCulture) != expected)
                {
                    mismatches.Add(field);
                }
            }
            catch
            {
                mismatches.Add(field);
            }
        }

        private static string Text(DataRow row, string column)
        {
            object value = row[column];
            return value == null || value == DBNull.Value
                ? string.Empty
                : Convert.ToString(value, CultureInfo.InvariantCulture);
        }

        private static bool IsNull(DataRow row, string column)
        {
            object value = row[column];
            return value == null
                || value == DBNull.Value
                || (value is string && string.IsNullOrWhiteSpace((string)value));
        }

        private static DateTime? NullableDate(DataRow row, string column)
        {
            object value = row[column];
            if (value == null || value == DBNull.Value)
            {
                return null;
            }
            return Convert.ToDateTime(value, CultureInfo.InvariantCulture);
        }

        private static string Iso(DateTime? value)
        {
            return value.HasValue
                ? value.Value.ToString("yyyy-MM-ddTHH:mm:ss.fffffff", CultureInfo.InvariantCulture)
                : null;
        }

        private static string SafeMessage(Exception exception, string password)
        {
            string message = exception == null ? string.Empty : exception.Message ?? string.Empty;
            return string.IsNullOrEmpty(password)
                ? message
                : message.Replace(password, "***");
        }
    }
}
