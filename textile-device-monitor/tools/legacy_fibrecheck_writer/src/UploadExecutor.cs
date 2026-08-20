using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using LegacyFibreCheckRunner;
using Toone.FibreCheck.Entites.OrmEntites;
using Toone.FibreCheck.OriRecord.SpecialWool;

namespace LegacyFibreCheckWriter
{
    /// <summary>
    /// 旧系统上传写入执行器。阶段与交接手册一致：
    /// authenticated → permission_verified → remote_absence_verified →
    /// file_copy_ready →（等待 Bridge 许可）→ file_copy_started →
    /// file_copy_verified → main_record_save_started →
    /// main_record_verified → completed。
    /// file_copy_started 起的任何失败以 reconciliation_required 收尾，绝不自动重试。
    /// </summary>
    internal static class UploadExecutor
    {
        private const string RegeneratedCountOperation = "legacy_regenerated_fiber_count_upload";
        private const string SpecialWoolImageOperation = "legacy_special_wool_image_upload";
        private const string SpecialWoolReviewOperation = "legacy_special_wool_review";
        private const string SpecialWoolQualitativeUploadOperation =
            "legacy_special_wool_qualitative_upload";
        private const string SpecialWoolQualitativeReviewOperation =
            "legacy_special_wool_qualitative_review";
        public const int ExitReconciliationRequired = 30;
        public const int ExitPackageError = 21;
        public const int ExitSourceMismatch = 22;
        public const int ExitTargetFileConflict = 23;
        public const int ExitSideEffectPermitDenied = 24;

        private static readonly string[] SideEffectStages =
        {
            "file_copy_started",
            "file_copy_verified",
            "main_record_save_started",
            "main_record_verified",
            "review_save_started",
            "review_main_verified",
            "review_children_verified",
            "completed",
        };

        public sealed class Result
        {
            public int ExitCode;
            public string FailureStage;
            public bool ReconciliationRequired;
            public SortedDictionary<string, object> Receipt = new SortedDictionary<string, object>();
            public List<object> Stages = new List<object>();
        }

        public static int Execute(
            string fibreCheckDir,
            string account,
            string password,
            string packagePath,
            string sourceRoot,
            Action<object> emit,
            Func<bool> awaitSideEffectPermit)
        {
            var result = new Result();
            Action<string, object> stage = (name, detail) =>
            {
                var entry = new SortedDictionary<string, object>
                {
                    { "stage", name },
                    { "at", DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") },
                };
                if (detail != null)
                {
                    entry["detail"] = detail;
                }
                result.Stages.Add(entry);
                emit(entry);
            };

            // 1) 读取并校验任务包
            Dictionary<string, object> package;
            try
            {
                string json = File.ReadAllText(packagePath, System.Text.Encoding.UTF8);
                var serializer = new System.Web.Script.Serialization.JavaScriptSerializer();
                package = serializer.Deserialize<Dictionary<string, object>>(json);
            }
            catch (Exception ex)
            {
                result.Receipt["error"] = "package_read_failed: " + ex.GetType().Name;
                return Finish(result, ExitPackageError, null, emit);
            }

            var operation = GetMap(package, "operation");
            var summary = operation == null ? null : GetMap(operation, "request_summary");
            if (summary == null)
            {
                result.Receipt["error"] = "package_missing_request_summary";
                return Finish(result, ExitPackageError, null, emit);
            }
            string operationType = GetStr(summary, "operation_type");
            if (string.IsNullOrWhiteSpace(operationType))
            {
                // 兼容早期已批准的根数法任务包；新操作必须显式声明类型。
                operationType = RegeneratedCountOperation;
            }
            if (string.Equals(operationType, SpecialWoolImageOperation, StringComparison.Ordinal)
                || string.Equals(operationType, SpecialWoolReviewOperation, StringComparison.Ordinal)
                || string.Equals(
                    operationType,
                    SpecialWoolQualitativeUploadOperation,
                    StringComparison.Ordinal)
                || string.Equals(
                    operationType,
                    SpecialWoolQualitativeReviewOperation,
                    StringComparison.Ordinal))
            {
                return SpecialWoolExecutor.Execute(
                    fibreCheckDir,
                    account,
                    password,
                    sourceRoot,
                    operation,
                    summary,
                    operationType,
                    result,
                    stage,
                    emit,
                    awaitSideEffectPermit);
            }
            if (!string.Equals(operationType, RegeneratedCountOperation, StringComparison.Ordinal))
            {
                result.Receipt["error"] = "writer_capability_unavailable";
                result.Receipt["operation_type"] = operationType;
                result.Receipt["remote_write_performed"] = false;
                result.Receipt["missing_proof"] = "unknown_operation_profile";
                // 严禁让新节点落入根数法硬编码保存路径。
                return Finish(result, ExitPackageError, null, emit);
            }
            string target = GetStr(summary, "target_sample_number");
            string sourceNumber = GetStr(summary, "source_inspection_number");
            string inspectorName = GetStr(summary, "inspector");
            var files = GetList(summary, "files");
            if (!LegacyLoginFlow.IsValidSampleNo(target) || string.IsNullOrWhiteSpace(inspectorName) || files.Count == 0)
            {
                result.Receipt["error"] = "package_fields_invalid";
                return Finish(result, ExitPackageError, null, emit);
            }
            if (files.Count != 1)
            {
                result.Receipt["error"] = "first_write_supports_single_file_only";
                return Finish(result, ExitPackageError, null, emit);
            }
            var fileEntry = GetMap(files[0]);
            string relativePath = GetStr(fileEntry, "relative_path");
            string fileName = GetStr(fileEntry, "filename");
            string expectedSha = GetStr(fileEntry, "content_sha256");
            long expectedSize = GetLong(fileEntry, "size_bytes");
            if (string.IsNullOrWhiteSpace(relativePath) || string.IsNullOrWhiteSpace(fileName))
            {
                result.Receipt["error"] = "package_file_entry_invalid";
                return Finish(result, ExitPackageError, null, emit);
            }
            try
            {
                if (!string.Equals(Path.GetFileName(fileName), fileName, StringComparison.Ordinal)
                    || fileName == "."
                    || fileName == ".."
                    || fileName.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
                {
                    result.Receipt["error"] = "package_filename_invalid";
                    return Finish(result, ExitPackageError, null, emit);
                }
            }
            catch (Exception)
            {
                result.Receipt["error"] = "package_filename_invalid";
                return Finish(result, ExitPackageError, null, emit);
            }

            // 2) 只读登录核验（账号口令、区域、组织上下文、权限）
            var options = new RunnerOptions
            {
                FibreCheckDir = fibreCheckDir,
                Account = account,
                Password = password,
            };
            SortedDictionary<string, object> loginDocument;
            LegacyLoginFlow.StaffContext staff;
            int loginExit = LegacyLoginFlow.RunCore(options, conn => new OdpNetDb(conn), out loginDocument, out staff);
            if (loginExit != 0 || staff == null)
            {
                result.Receipt["error"] = "login_failed:" + loginExit;
                result.Receipt["login"] = loginDocument;
                return Finish(result, loginExit, null, emit);
            }
            stage("authenticated", null);

            var permission = loginDocument.ContainsKey("function_permission")
                ? loginDocument["function_permission"] as SortedDictionary<string, object>
                : null;
            bool granted = permission != null
                && permission.ContainsKey("granted")
                && permission["granted"] is bool
                && (bool)permission["granted"];
            if (!granted)
            {
                result.Receipt["error"] = "permission_denied";
                return Finish(result, ExitCodes.PermissionDenied, null, emit);
            }
            stage("permission_verified", null);

            // 3) 注入全局登录人（官方 DAL 据此写审计字段）
            Toone.FibreCheck.Entites.CommonEntities.SystemData.Instance.CurrentLoginedStaff =
                new Toone.FibreCheck.Entites.CommonEntities.CommunicationEntities.StaffEntity
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

            using (var db = new OdpNetDb(SystemDataConnection.Read(fibreCheckDir, "DbConnString")))
            {
                db.OpenReadOnly();

                // 4) 远端缺失核验（精确 + 与旧客户端相同的 Contains 语义）
                int exact = Convert.ToInt32(db.Scalar(LegacyLoginFlow.ExactSampleCountSql, new List<DbParam> { new DbParam("sampleno", target) }) ?? 0);
                int contains = Convert.ToInt32(db.Scalar(LegacyLoginFlow.ContainsSampleCountSql, new List<DbParam> { new DbParam("contains", "%" + target + "%") }) ?? 0);
                if (exact != 0 || contains != 0)
                {
                    result.Receipt["error"] = "target_already_exists";
                    result.Receipt["remote_absence"] = new SortedDictionary<string, object>
                    {
                        { "exact_count", exact },
                        { "contains_count", contains },
                    };
                    return Finish(result, ExitCodes.DryRunConflict, null, emit);
                }
                stage("remote_absence_verified", new SortedDictionary<string, object>
                {
                    { "target_sample_number", target },
                    { "exact_count", 0 },
                    { "contains_count", 0 },
                });

                // 5) 检验员唯一映射
                var inspector = LegacyLoginFlow.ResolveInspector(db, inspectorName);
                if (inspector.Value != "unique")
                {
                    result.Receipt["error"] = "inspector_not_unique:" + inspector.Value;
                    return Finish(result, ExitCodes.InspectorMappingFailed, null, emit);
                }
                string inspectorId = inspector.Key;

                // 6) 源文件核验（存在、大小、SHA-256 与预检单一致）
                string normalizedSourceRoot;
                string sourcePath;
                try
                {
                    normalizedSourceRoot = Path.GetFullPath(sourceRoot);
                    if (!normalizedSourceRoot.EndsWith(Path.DirectorySeparatorChar.ToString(), StringComparison.Ordinal)
                        && !normalizedSourceRoot.EndsWith(Path.AltDirectorySeparatorChar.ToString(), StringComparison.Ordinal))
                    {
                        normalizedSourceRoot += Path.DirectorySeparatorChar;
                    }
                    sourcePath = Path.GetFullPath(Path.Combine(
                        normalizedSourceRoot,
                        relativePath.Replace('/', Path.DirectorySeparatorChar)));
                }
                catch (Exception)
                {
                    result.Receipt["error"] = "package_relative_path_invalid";
                    return Finish(result, ExitPackageError, null, emit);
                }
                if (!sourcePath.StartsWith(normalizedSourceRoot, StringComparison.OrdinalIgnoreCase))
                {
                    result.Receipt["error"] = "package_relative_path_outside_source_root";
                    return Finish(result, ExitPackageError, null, emit);
                }
                byte[] sourceBytes;
                try
                {
                    sourceBytes = File.ReadAllBytes(sourcePath);
                }
                catch (Exception ex)
                {
                    result.Receipt["error"] = "source_unreadable: " + ex.GetType().Name;
                    return Finish(result, ExitSourceMismatch, null, emit);
                }
                string sourceSha = Sha256Hex(sourceBytes);
                if (sourceBytes.LongLength != expectedSize
                    || !string.Equals(sourceSha, expectedSha, StringComparison.OrdinalIgnoreCase))
                {
                    result.Receipt["error"] = "source_content_changed";
                    result.Receipt["source"] = new SortedDictionary<string, object>
                    {
                        { "size_bytes", sourceBytes.LongLength },
                        { "content_sha256", sourceSha },
                    };
                    return Finish(result, ExitSourceMismatch, null, emit);
                }

                // 7) 目标目录：FileServer + OriginalData + Files\年\月\日\SpecialWool（日期取服务器 SYSDATE）
                string fileServer = Convert.ToString(db.Scalar(LegacyLoginFlow.KeyValueSql, new List<DbParam> { new DbParam("infokey", "FileServer") }) ?? "");
                string originalData = Convert.ToString(db.Scalar(LegacyLoginFlow.FileDirectorySql, new List<DbParam> { new DbParam("infokey", "OriginalData") }) ?? "");
                object sysDateObj = db.Scalar(LegacyLoginFlow.SysDateSql, new List<DbParam>());
                if (string.IsNullOrWhiteSpace(fileServer) || string.IsNullOrWhiteSpace(originalData) || !(sysDateObj is DateTime))
                {
                    result.Receipt["error"] = "file_server_config_unavailable";
                    return Finish(result, ExitCodes.InfrastructureError, null, emit);
                }
                DateTime serverDate = (DateTime)sysDateObj;
                string targetDir = Path.Combine(
                    fileServer,
                    originalData,
                    "Files",
                    serverDate.Year.ToString(),
                    serverDate.Month.ToString(),
                    serverDate.Day.ToString(),
                    "SpecialWool");
                string targetPath = Path.Combine(targetDir, fileName);
                if (File.Exists(targetPath))
                {
                    result.Receipt["error"] = "target_file_already_exists";
                    result.Receipt["target_path_hash"] = Redact.HashId(targetPath);
                    return Finish(result, ExitTargetFileConflict, null, emit);
                }

                // 8) Bridge 必须先把副作用边界持久化到服务端，再通过 stdin
                // 发放一次性许可。没有许可时本进程不得创建目录或文件。
                stage("file_copy_ready", new SortedDictionary<string, object>
                {
                    { "file_name", fileName },
                    { "size_bytes", sourceBytes.LongLength },
                    { "content_sha256", sourceSha },
                });
                if (awaitSideEffectPermit == null || !awaitSideEffectPermit())
                {
                    result.Receipt["error"] = "side_effect_permit_denied";
                    return Finish(result, ExitSideEffectPermitDenied, null, emit);
                }

                // 文件复制是副作用起点；此后失败一律 reconciliation_required。
                stage("file_copy_started", new SortedDictionary<string, object>
                {
                    { "file_name", fileName },
                    { "size_bytes", sourceBytes.LongLength },
                    { "content_sha256", sourceSha },
                });
                try
                {
                    Directory.CreateDirectory(targetDir);
                    using (var stream = new FileStream(
                        targetPath,
                        FileMode.CreateNew,
                        FileAccess.Write,
                        FileShare.None))
                    {
                        stream.Write(sourceBytes, 0, sourceBytes.Length);
                        stream.Flush();
                    }
                }
                catch (Exception ex)
                {
                    result.Receipt["error"] = "file_copy_failed: " + ex.GetType().Name;
                    return Finish(result, ExitReconciliationRequired, "file_copy_started", emit);
                }
                try
                {
                    byte[] copied = File.ReadAllBytes(targetPath);
                    string copiedSha = Sha256Hex(copied);
                    if (copied.LongLength != sourceBytes.LongLength
                        || !string.Equals(copiedSha, sourceSha, StringComparison.OrdinalIgnoreCase))
                    {
                        result.Receipt["error"] = "file_copy_verify_mismatch";
                        return Finish(result, ExitReconciliationRequired, "file_copy_started", emit);
                    }
                }
                catch (Exception ex)
                {
                    result.Receipt["error"] = "file_copy_verify_failed: " + ex.GetType().Name;
                    return Finish(result, ExitReconciliationRequired, "file_copy_started", emit);
                }
                stage("file_copy_verified", new SortedDictionary<string, object>
                {
                    { "file_name", fileName },
                    { "content_sha256", sourceSha },
                });

                // 9) 主记录保存（官方 DAL）
                stage("main_record_save_started", null);
                string savedId = null;
                DateTime? savedCreateTime = null;
                try
                {
                    var record = new SpecialWoolManage
                    {
                        SampleNo = target,
                        FibreSort = "棉再生纤",
                        CheckWay = "定量",
                        CheckUser1 = inspectorId,
                        CheckUserItem1 = "棉再生纤定量-根数法",
                        CheckUserNumber1 = 1,
                        ReviewUserNumber1 = 1,
                        FilePath = fileName,
                        FileType = "定量试验",
                    };
                    var dal = new SpecialWoolDAL();
                    dal.SaveSpecialWoolManage(
                        record,
                        new List<List<WoolFinenessRecord>>(),
                        new List<OriginalDataPictureFile>(),
                        new List<QuantificationTest>());
                    savedId = record.ID;
                    savedCreateTime = record.CreateTime;
                }
                catch (Exception ex)
                {
                    result.Receipt["error"] = "main_record_save_failed: " + ex.GetType().Name;
                    result.Receipt["error_detail"] = (ex.Message ?? string.Empty).Replace(password, "***");
                    return Finish(result, ExitReconciliationRequired, "main_record_save_started", emit);
                }

                // 10) 保存后回读核验
                try
                {
                    var dal2 = new SpecialWoolDAL();
                    var saved = dal2.GetByReportNo(target, "");
                    if (saved == null || saved.SampleNo != target)
                    {
                        result.Receipt["error"] = "main_record_readback_missing";
                        return Finish(result, ExitReconciliationRequired, "main_record_save_started", emit);
                    }
                    var mismatches = new List<string>();
                    if (!string.Equals(saved.ID, savedId, StringComparison.Ordinal)) mismatches.Add("ID");
                    if (saved.FibreSort != "棉再生纤") mismatches.Add("FibreSort");
                    if (saved.CheckWay != "定量") mismatches.Add("CheckWay");
                    if (saved.CheckUser1 != inspectorId) mismatches.Add("CheckUser1");
                    if (saved.CheckUserItem1 != "棉再生纤定量-根数法") mismatches.Add("CheckUserItem1");
                    if (saved.CheckUserNumber1 != 1) mismatches.Add("CheckUserNumber1");
                    if (saved.ReviewUserNumber1 != 1) mismatches.Add("ReviewUserNumber1");
                    if (saved.FilePath != fileName) mismatches.Add("FilePath");
                    if (saved.FileType != "定量试验") mismatches.Add("FileType");
                    if (saved.CreateUser != staff.Id) mismatches.Add("CreateUser");
                    if (mismatches.Count > 0)
                    {
                        result.Receipt["error"] = "main_record_verify_mismatch";
                        result.Receipt["mismatched_fields"] = mismatches;
                        return Finish(result, ExitReconciliationRequired, "main_record_save_started", emit);
                    }
                    savedId = saved.ID;
                    savedCreateTime = saved.CreateTime;
                }
                catch (Exception ex)
                {
                    result.Receipt["error"] = "main_record_readback_failed: " + ex.GetType().Name;
                    return Finish(result, ExitReconciliationRequired, "main_record_save_started", emit);
                }
                stage("main_record_verified", new SortedDictionary<string, object>
                {
                    { "record_id", Redact.HashId(savedId) },
                    { "create_time", savedCreateTime.HasValue ? savedCreateTime.Value.ToString("yyyy-MM-ddTHH:mm:ss") : null },
                });
            }

            result.Receipt["target_sample_number"] = target;
            result.Receipt["source_inspection_number"] = sourceNumber;
            result.Receipt["inspector"] = inspectorName;
            stage("completed", null);
            return Finish(result, 0, null, emit);
        }

        public static int ReconcileSpecialWoolImage(
            string fibreCheckDir,
            string account,
            string password,
            string packagePath,
            string sourceRoot,
            Action<object> emit)
        {
            var result = new Result();
            Action<string, object> stage = (name, detail) =>
            {
                var entry = new SortedDictionary<string, object>
                {
                    { "stage", name },
                    { "at", DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") },
                };
                if (detail != null)
                {
                    entry["detail"] = detail;
                }
                result.Stages.Add(entry);
                emit(entry);
            };

            Dictionary<string, object> package;
            try
            {
                string json = File.ReadAllText(
                    packagePath,
                    System.Text.Encoding.UTF8);
                var serializer =
                    new System.Web.Script.Serialization.JavaScriptSerializer();
                package = serializer.Deserialize<Dictionary<string, object>>(
                    json);
            }
            catch (Exception ex)
            {
                result.Receipt["error"] =
                    "package_read_failed: " + ex.GetType().Name;
                return Finish(result, ExitPackageError, null, emit);
            }

            Dictionary<string, object> operation = GetMap(package, "operation");
            Dictionary<string, object> summary = operation == null
                ? null
                : GetMap(operation, "request_summary");
            if (summary == null
                || !string.Equals(
                    GetStr(summary, "operation_type"),
                    SpecialWoolImageOperation,
                    StringComparison.Ordinal))
            {
                result.Receipt["error"] =
                    "reconciliation_requires_special_wool_image_package";
                return Finish(result, ExitPackageError, null, emit);
            }
            return SpecialWoolExecutor.ReconcileExistingImageUpload(
                fibreCheckDir,
                account,
                password,
                sourceRoot,
                operation,
                summary,
                result,
                stage,
                emit);
        }

        internal static int Finish(Result result, int exitCode, string failureStage, Action<object> emit)
        {
            result.ExitCode = exitCode;
            result.FailureStage = failureStage;
            result.ReconciliationRequired = failureStage != null;
            if (failureStage != null)
            {
                result.Receipt["failure_stage"] = failureStage;
                emit(new SortedDictionary<string, object>
                {
                    { "stage", "reconciliation_required" },
                    { "at", DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") },
                    { "detail", "失败发生于 " + failureStage + "（含）之后；不得自动重试，需只读对账" },
                });
            }
            result.Receipt["stages"] = result.Stages;
            result.Receipt["reconciliation_required"] = result.ReconciliationRequired;
            emit(new SortedDictionary<string, object> { { "receipt", result.Receipt } });
            return exitCode;
        }

        internal static bool IsSideEffectStage(string stage)
        {
            foreach (string item in SideEffectStages)
            {
                if (item == stage)
                {
                    return true;
                }
            }
            return false;
        }

        internal static string Sha256Hex(byte[] data)
        {
            using (var sha = SHA256.Create())
            {
                byte[] digest = sha.ComputeHash(data);
                var builder = new System.Text.StringBuilder();
                foreach (byte b in digest)
                {
                    builder.Append(b.ToString("x2"));
                }
                return builder.ToString();
            }
        }

        internal static Dictionary<string, object> GetMap(object value)
        {
            return value as Dictionary<string, object>;
        }

        internal static Dictionary<string, object> GetMap(Dictionary<string, object> map, string key)
        {
            object value;
            return map != null && map.TryGetValue(key, out value) ? value as Dictionary<string, object> : null;
        }

        internal static string GetStr(Dictionary<string, object> map, string key)
        {
            object value;
            return map != null && map.TryGetValue(key, out value) && value != null ? value.ToString() : null;
        }

        internal static long GetLong(Dictionary<string, object> map, string key)
        {
            object value;
            if (map == null || !map.TryGetValue(key, out value) || value == null)
            {
                return -1;
            }
            long parsed;
            return long.TryParse(value.ToString(), out parsed) ? parsed : -1;
        }

        internal static List<object> GetList(Dictionary<string, object> map, string key)
        {
            object value;
            if (map == null || !map.TryGetValue(key, out value) || value == null)
            {
                return new List<object>();
            }
            var ilist = value as System.Collections.IList;
            if (ilist != null)
            {
                var copy = new List<object>();
                foreach (object item in ilist)
                {
                    copy.Add(item);
                }
                return copy;
            }
            return new List<object>();
        }
    }
}
