using System;
using System.Collections.Generic;

namespace LegacyFibreCheckRunner
{
    internal static class ExitCodes
    {
        public const int Ok = 0;
        public const int UsageError = 2;
        public const int InfrastructureError = 3;
        public const int AccountNotFound = 10;
        public const int DuplicateAccount = 11;
        public const int PasswordMismatch = 12;
        public const int LoginDisabled = 13;
        public const int PermissionDenied = 14;
        public const int InspectorMappingFailed = 15;
        public const int RegionNotSupported = 16;
        public const int DryRunConflict = 17;
        public const int InvalidSampleNumber = 18;
    }

    internal sealed class RunnerOptions
    {
        public string FibreCheckDir;
        public string Account;
        public string Password;
        public string FunctionType = "Toone.FibreCheck.OriRecord.SpecialWool.SpecialWoolSearchUI";
        public List<string> InspectorNames = new List<string>();
        public string OutputPath = "-";
        /// <summary>测试注入用；为空时从旧程序 SystemData 反射读取 DbConnString。</summary>
        public Func<string> ConnectionStringResolver;

        /// <summary>dry-run 上传核验模式：仍零写入，只生成最终变更清单。</summary>
        public bool DryRunUpload;
        public string TargetSampleNumber;
        public string SourceFileName;
        public string SourceInspectionNumber;
    }

    /// <summary>
    /// 复现旧系统登录链的无界面只读版本：
    /// CanLoginSystem -> GetAccoutIsPanYuOrHuaDu -> CheckAccoutInfo ->
    /// LoadDepartmentAndPositionInfo，随后做旧权限表显式只读授权检查和
    /// 检验员中文名 -> User ID 唯一映射。不执行 LoadMainWindowInfo
    /// （主窗/后台服务/登录缓存），不调用任何保存方法。
    /// 按 2026-07-30 确认：实验室仅使用番禺库，花都路径不实现。
    /// </summary>
    internal static class LegacyLoginFlow
    {
        private const string CanLoginSystemSql =
            "select \"InfoValue\" from \"KeyValues\" where \"InfoKey\" = :infokey";

        private const string RegionSql =
            "select \"Department\".\"DepartmentName\" " +
            "From \"User\", \"User_Position\", \"Position\",\"Department\" " +
            "where \"User\".\"ID\" = \"User_Position\".\"UserID\" " +
            "and \"User_Position\".\"PositionID\"=\"Position\".\"ID\" " +
            "and \"Position\".\"DepartmentID\"=\"Department\".\"ID\" " +
            "and \"User\".\"IsDeleted\" != 1 " +
            "and \"LoginName\" = :loginname";

        private const string AccountSql =
            "Select \"ID\", \"LoginPassword\", \"ChineseName\", \"Remark\" From \"User\" " +
            "Where \"IsDeleted\"!=1 and \"LoginName\" = :loginname";

        private const string DepartmentPositionSql =
            "Select b.*, \"Department\".\"DepartmentName\" \"SubDepartmentName\" from ( " +
            "Select a.*,\"Department\".\"DepartmentName\", \"Department\".\"DeptCode\" from ( " +
            "Select \"Position\".\"ID\" \"PositionID\" ,\"Position\".\"PositionName\" ,\"Position\".\"DepartmentID\", \"User_Position\".\"SubDepartmentID\" " +
            "From \"Position\" , \"User_Position\" " +
            "where \"Position\".\"ID\" = \"User_Position\".\"PositionID\" And \"User_Position\".\"UserID\" = :userid ) a , " +
            "\"Department\" where a.\"DepartmentID\" = \"Department\".\"ID\"(+) ) b ,\"Department\" " +
            "where b.\"SubDepartmentID\" = \"Department\".\"ID\"(+)";

        private const string ParentDepartmentSql =
            "SELECT D.\"ID\" FROM \"Department\" D WHERE D.\"DeptCode\" = :deptcode";

        private const string FunctionDefinitionSql =
            "SELECT PVFD.\"FunctionID\", PVFD.\"FunctionName\", PVFD.\"FunctionType\" " +
            "FROM \"PV_FunctionDefinition\" PVFD WHERE PVFD.\"FunctionType\" = :ftype";

        private const string FunctionGrantSql =
            "SELECT COUNT(*) FROM \"PV_PurviewAssign\" PVPA " +
            "WHERE PVPA.\"PurviewID\" = :fid AND PVPA.\"PurviewType\" = '0' " +
            "AND (PVPA.\"OperatorID\" IN ({0}) OR PVPA.\"OperatorID\" = '' OR PVPA.\"OperatorID\" IS NULL)";

        private const string ControlPurviewSql =
            "SELECT PVPD.\"PurviewID\",PVPD.\"FunctionID\",PVPD.\"ControlIdentity\",PVPD.\"PurviewName\"," +
            "PVPD.\"Description\",PVPD.\"SeqNum\",PVPD.\"PurviewControlMode\",PVPA.\"PurviewID\" \"PID\" " +
            "FROM \"PV_PurviewDefinition\" PVPD LEFT OUTER JOIN \"PV_PurviewAssign\" PVPA " +
            "ON PVPA.\"PurviewID\"=PVPD.\"PurviewID\" AND PVPA.\"PurviewType\"='1' " +
            "{0} WHERE PVPD.\"FunctionID\" = :fid ORDER BY PVPD.\"ControlIdentity\", PVPD.\"SeqNum\"";

        private const string InspectorSql =
            "Select \"ID\", \"ChineseName\", \"IsDeleted\" From \"User\" " +
            "Where \"IsDeleted\"!=1 and \"ChineseName\" = :chinesename";

        private const string SysDateSql = "SELECT SYSDATE FROM DUAL";

        private const string KeyValueSql =
            "select \"InfoValue\" from \"KeyValues\" where \"InfoKey\" = :infokey";

        private const string FileDirectorySql =
            "SELECT \"InfoValue\" FROM \"FileDirectory\" WHERE \"InfoKey\" = :infokey";

        private const string ExactSampleCountSql =
            "SELECT COUNT(*) FROM \"SpecialWoolManage\" sw WHERE sw.\"SampleNo\" = :sampleno";

        private const string ContainsSampleCountSql =
            "SELECT COUNT(*) FROM \"SpecialWoolManage\" sw WHERE sw.\"SampleNo\" LIKE :contains ESCAPE '\\'";

        private const string PrefixSampleCountSql =
            "SELECT COUNT(*) FROM \"SpecialWoolManage\" sw WHERE sw.\"SampleNo\" LIKE :prefix ESCAPE '\\'";

        private static readonly System.Text.RegularExpressions.Regex SampleNoPattern =
            new System.Text.RegularExpressions.Regex(@"^[0-9A-Z]{9,20}(?:-[0-9A-Z]{1,8})?$");

        private sealed class StaffContext
        {
            public string Id;
            public string LoginName;
            public string ChineseName;
            public string UserRemark;
            public string PositionID;
            public string PositionName;
            public string DepartmentID;
            public string DepartmentName;
            public string SubDepartmentID;
            public string SubDepartmentName;
            public List<string> ParentDepartmentIds = new List<string>();

            public List<string> OperatorChain()
            {
                var chain = new List<string>();
                chain.AddRange(ParentDepartmentIds);
                chain.Add(DepartmentID);
                chain.Add(PositionID);
                chain.Add(Id);
                return chain.FindAll(item => !string.IsNullOrWhiteSpace(item));
            }
        }

        /// <summary>运行只读登录核验，返回退出码并输出 JSON 文档。dbFactory 按连接串建库，供测试替换。</summary>
        public static int Run(RunnerOptions options, Func<string, ILegacyDb> dbFactory, out SortedDictionary<string, object> document)
        {
            document = new SortedDictionary<string, object>
            {
                { "schema_version", 1 },
                { "mode", "read_only_login_probe" },
                { "generated_at", DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") },
                { "account", Redact.MaskLogin(options.Account ?? "") },
                { "function_type", options.FunctionType },
                { "safety", new SortedDictionary<string, object>
                    {
                        { "database_transaction", "SET TRANSACTION READ ONLY" },
                        { "queries", "parameterized_select_only" },
                        { "commit", false },
                        { "file_server_access", false },
                        { "ui_started", false },
                    }
                },
            };
            var notes = new List<object>();
            document["notes"] = notes;

            string connString;
            try
            {
                connString = options.ConnectionStringResolver != null
                    ? options.ConnectionStringResolver()
                    : SystemDataConnection.Read(options.FibreCheckDir, "DbConnString");
            }
            catch (Exception ex)
            {
                document["error"] = SafeError(ex);
                return ExitCodes.UsageError;
            }
            if (string.IsNullOrWhiteSpace(connString))
            {
                document["error"] = new SortedDictionary<string, object>
                {
                    { "type", "ConfigError" },
                    { "code", "missing_db_conn_string" },
                };
                return ExitCodes.UsageError;
            }

            ILegacyDb db = dbFactory(connString);
            try
            {
                db.OpenReadOnly();

                // 1) CanLoginSystem
                object infoValue = db.Scalar(CanLoginSystemSql, new List<DbParam> { new DbParam("infokey", "CanLoginSystem") });
                bool canLogin = infoValue != null && infoValue != DBNull.Value && Convert.ToInt32(infoValue) != 0;
                document["can_login_system"] = canLogin;
                if (!canLogin)
                {
                    return ExitCodes.LoginDisabled;
                }

                // 2) GetAccoutIsPanYuOrHuaDu（仅番禺；花都按未支持区域处理）
                object deptName = db.Scalar(RegionSql, new List<DbParam> { new DbParam("loginname", options.Account) });
                if (deptName == null || deptName == DBNull.Value)
                {
                    notes.Add("region_unconfirmed: 番禺库未取到部门，按番禺继续（已确认不使用花都）");
                }
                else if (deptName.ToString().StartsWith("花都"))
                {
                    document["region"] = "huadu";
                    return ExitCodes.RegionNotSupported;
                }
                document["region"] = "panyu";

                // 3) CheckAccoutInfo
                using (var accountTable = db.Query(AccountSql, new List<DbParam> { new DbParam("loginname", options.Account) }))
                {
                    if (accountTable.Rows.Count == 0)
                    {
                        return ExitCodes.AccountNotFound;
                    }
                    if (accountTable.Rows.Count > 1)
                    {
                        return ExitCodes.DuplicateAccount;
                    }
                    var row = accountTable.Rows[0];
                    string storedHash = Convert.ToString(row["LoginPassword"]) ?? string.Empty;
                    string inputHash = Redact.EncryptByMd5(options.Password ?? string.Empty);
                    if (!string.Equals(storedHash, inputHash, StringComparison.Ordinal))
                    {
                        document["password_match"] = false;
                        return ExitCodes.PasswordMismatch;
                    }
                    document["password_match"] = true;
                    string policyMessage = Redact.CheckPasswordIsOk(options.Password ?? string.Empty);
                    document["password_policy_ok"] = string.IsNullOrEmpty(policyMessage);
                    if (!string.IsNullOrEmpty(policyMessage))
                    {
                        notes.Add("password_policy: " + policyMessage + "（旧客户端会要求重置，Runner 仅报告）");
                    }

                    var staff = new StaffContext
                    {
                        Id = Convert.ToString(row["ID"]),
                        LoginName = options.Account,
                        ChineseName = Convert.ToString(row["ChineseName"]),
                        UserRemark = Convert.ToString(row["Remark"]),
                    };

                    // 4) LoadDepartmentAndPositionInfo
                    using (var deptTable = db.Query(DepartmentPositionSql, new List<DbParam> { new DbParam("userid", staff.Id) }))
                    {
                        if (deptTable.Rows.Count == 1)
                        {
                            var deptRow = deptTable.Rows[0];
                            staff.PositionID = Convert.ToString(deptRow["PositionID"]);
                            staff.PositionName = Convert.ToString(deptRow["PositionName"]);
                            staff.DepartmentID = Convert.ToString(deptRow["DepartmentID"]);
                            staff.DepartmentName = Convert.ToString(deptRow["DepartmentName"]);
                            staff.SubDepartmentID = Convert.ToString(deptRow["SubDepartmentID"]);
                            staff.SubDepartmentName = Convert.ToString(deptRow["SubDepartmentName"]);
                            string deptCode = deptRow["DeptCode"] == DBNull.Value ? string.Empty : Convert.ToString(deptRow["DeptCode"]);
                            foreach (string prefix in ExpandDeptCodePrefixes(deptCode))
                            {
                                object parentId = db.Scalar(ParentDepartmentSql, new List<DbParam> { new DbParam("deptcode", prefix) });
                                if (parentId != null && parentId != DBNull.Value)
                                {
                                    staff.ParentDepartmentIds.Add(parentId.ToString());
                                }
                            }
                        }
                        else
                        {
                            notes.Add("department_position: 部门岗位记录数=" + deptTable.Rows.Count + "，与旧客户端一致不填充组织上下文");
                        }
                    }

                    document["staff"] = new SortedDictionary<string, object>
                    {
                        { "id", Redact.HashId(staff.Id) },
                        { "login_name", Redact.MaskLogin(staff.LoginName) },
                        { "chinese_name", staff.ChineseName },
                        { "position_name", staff.PositionName },
                        { "department_name", staff.DepartmentName },
                        { "sub_department_name", staff.SubDepartmentName },
                        { "parent_department_ids", staff.ParentDepartmentIds.ConvertAll(Redact.HashId) },
                        { "operator_chain_size", staff.OperatorChain().Count },
                    };

                    // 5) 旧权限表显式只读授权检查
                    bool isFAdmin = string.Equals(options.Account, "fadmin", StringComparison.OrdinalIgnoreCase);
                    int permissionExit = CheckFunctionPermission(db, options.FunctionType, staff, isFAdmin, document, notes);
                    if (permissionExit != ExitCodes.Ok)
                    {
                        return permissionExit;
                    }

                    // 6) 检验员中文名 -> User ID 唯一映射
                    Dictionary<string, string> inspectorIds = new Dictionary<string, string>();
                    if (options.InspectorNames.Count > 0)
                    {
                        bool allMapped = MapInspectors(db, options.InspectorNames, document, inspectorIds);
                        if (!allMapped)
                        {
                            return ExitCodes.InspectorMappingFailed;
                        }
                    }

                    // 7) dry-run 上传核验（仍零写入，只生成最终变更清单）
                    if (options.DryRunUpload)
                    {
                        return RunDryRunUpload(db, options, staff, inspectorIds, document, notes);
                    }
                }
                return ExitCodes.Ok;
            }
            catch (Exception ex)
            {
                if (ex is OutOfMemoryException)
                {
                    throw;
                }
                document["error"] = SafeError(ex);
                return ExitCodes.InfrastructureError;
            }
            finally
            {
                try { db.RollbackAndClose(); }
                catch { /* 关闭异常不影响已取得的只读结果 */ }
                db.Dispose();
            }
        }

        private static int CheckFunctionPermission(
            ILegacyDb db,
            string functionType,
            StaffContext staff,
            bool isFAdmin,
            SortedDictionary<string, object> document,
            List<object> notes)
        {
            string functionId = null;
            string functionName = null;
            using (var table = db.Query(FunctionDefinitionSql, new List<DbParam> { new DbParam("ftype", functionType) }))
            {
                if (table.Rows.Count > 0)
                {
                    functionId = Convert.ToString(table.Rows[0]["FunctionID"]);
                    functionName = Convert.ToString(table.Rows[0]["FunctionName"]);
                }
                if (table.Rows.Count > 1)
                {
                    notes.Add("function_definition: FunctionType 命中 " + table.Rows.Count + " 行，取第一行");
                }
            }

            var permission = new SortedDictionary<string, object>
            {
                { "function_type", functionType },
                { "function_name", functionName },
                { "is_fadmin", isFAdmin },
            };
            document["function_permission"] = permission;

            if (string.IsNullOrWhiteSpace(functionId))
            {
                permission["granted"] = false;
                notes.Add("function_definition: 未找到目标功能定义");
                return ExitCodes.PermissionDenied;
            }
            permission["function_id"] = Redact.HashId(functionId);

            bool granted;
            if (isFAdmin)
            {
                granted = true;
                notes.Add("function_permission: fadmin 按旧系统规则直接放行");
            }
            else
            {
                List<string> chain = staff.OperatorChain();
                var parameters = new List<DbParam> { new DbParam("fid", functionId) };
                string inList = BuildInList(chain, "op", parameters);
                object count = db.Scalar(string.Format(FunctionGrantSql, inList), parameters);
                granted = count != null && count != DBNull.Value && Convert.ToInt32(count) > 0;
            }
            permission["granted"] = granted;

            // 控制级权限清单（仅报告）
            var controlPurviews = new List<object>();
            List<string> joinChain = staff.OperatorChain();
            var controlParams = new List<DbParam> { new DbParam("fid", functionId) };
            string operatorFilter = isFAdmin
                ? string.Empty
                : "AND (PVPA.\"OperatorID\" IN (" + BuildInList(joinChain, "cp", controlParams) + ") OR PVPA.\"OperatorID\" = '' OR PVPA.\"OperatorID\" IS NULL)";
            using (var table = db.Query(string.Format(ControlPurviewSql, operatorFilter), controlParams))
            {
                foreach (System.Data.DataRow row in table.Rows)
                {
                    controlPurviews.Add(new SortedDictionary<string, object>
                    {
                        { "purview_id", Redact.HashId(Convert.ToString(row["PurviewID"])) },
                        { "control_identity", Convert.ToString(row["ControlIdentity"]) },
                        { "purview_name", Convert.ToString(row["PurviewName"]) },
                        { "enabled", row["PID"] != DBNull.Value && !string.IsNullOrWhiteSpace(Convert.ToString(row["PID"])) },
                    });
                }
            }
            document["control_purviews"] = controlPurviews;

            return granted ? ExitCodes.Ok : ExitCodes.PermissionDenied;
        }

        private static bool MapInspectors(ILegacyDb db, List<string> names, SortedDictionary<string, object> document, Dictionary<string, string> resolvedIds)
        {
            var mappings = new List<object>();
            bool allUnique = true;
            foreach (string name in names)
            {
                string status;
                string idHash = null;
                using (var table = db.Query(InspectorSql, new List<DbParam> { new DbParam("chinesename", name) }))
                {
                    if (table.Rows.Count == 1)
                    {
                        status = "unique";
                        string rawId = Convert.ToString(table.Rows[0]["ID"]);
                        idHash = Redact.HashId(rawId);
                        resolvedIds[name] = rawId;
                    }
                    else
                    {
                        status = table.Rows.Count == 0 ? "missing" : "ambiguous";
                        allUnique = false;
                    }
                }
                mappings.Add(new SortedDictionary<string, object>
                {
                    { "name", name },
                    { "status", status },
                    { "id", idHash },
                });
            }
            document["inspector_mappings"] = mappings;
            return allUnique;
        }

        internal static bool IsValidSampleNo(string value)
        {
            return !string.IsNullOrWhiteSpace(value) && SampleNoPattern.IsMatch(value);
        }

        /// <summary>
        /// dry-run：核验目标编号远端缺失、解析文件服务器配置，生成"将复制什么、
        /// 将创建什么"的最终清单。全程只读；目标已存在时返回 DryRunConflict。
        /// </summary>
        private static int RunDryRunUpload(
            ILegacyDb db,
            RunnerOptions options,
            StaffContext staff,
            Dictionary<string, string> inspectorIds,
            SortedDictionary<string, object> document,
            List<object> notes)
        {
            string target = options.TargetSampleNumber;
            if (!IsValidSampleNo(target))
            {
                notes.Add("target_sample_number: 编号格式不合法（须为 9-20 位大写字母/数字，可加 -后缀）");
                return ExitCodes.InvalidSampleNumber;
            }
            if (string.IsNullOrWhiteSpace(options.SourceFileName))
            {
                notes.Add("source_file_name: dry-run 模式必须提供待上传文件名");
                return ExitCodes.UsageError;
            }

            // 远端缺失核验：精确匹配 + 与旧客户端 GetByReportNo 相同的 Contains 语义
            int exact = Convert.ToInt32(db.Scalar(ExactSampleCountSql, new List<DbParam> { new DbParam("sampleno", target) }) ?? 0);
            int contains = Convert.ToInt32(db.Scalar(ContainsSampleCountSql, new List<DbParam> { new DbParam("contains", "%" + target + "%") }) ?? 0);
            string familyPrefix = target.Substring(0, Math.Min(9, target.Length));
            int family = Convert.ToInt32(db.Scalar(PrefixSampleCountSql, new List<DbParam> { new DbParam("prefix", familyPrefix + "%") }) ?? 0);

            document["remote_absence"] = new SortedDictionary<string, object>
            {
                { "target_sample_number", target },
                { "exact_count", exact },
                { "contains_count", contains },
                { "family_prefix", familyPrefix },
                { "family_count", family },
            };
            if (exact != 0 || contains != 0)
            {
                notes.Add("remote_absence: 目标编号在远端已存在（精确 " + exact + " / 包含 " + contains + "），禁止重复创建");
                return ExitCodes.DryRunConflict;
            }

            // 源记录上下文（可选）
            if (!string.IsNullOrWhiteSpace(options.SourceInspectionNumber))
            {
                if (!IsValidSampleNo(options.SourceInspectionNumber))
                {
                    notes.Add("source_inspection_number: 编号格式不合法");
                    return ExitCodes.InvalidSampleNumber;
                }
                int sourceCount = Convert.ToInt32(db.Scalar(ExactSampleCountSql, new List<DbParam> { new DbParam("sampleno", options.SourceInspectionNumber) }) ?? 0);
                document["source_record"] = new SortedDictionary<string, object>
                {
                    { "source_inspection_number", options.SourceInspectionNumber },
                    { "exact_count", sourceCount },
                };
            }

            // 文件服务器配置：目标目录 = FileServer + OriginalData + Files\年\月\日\SpecialWool
            object fileServer = db.Scalar(KeyValueSql, new List<DbParam> { new DbParam("infokey", "FileServer") });
            object originalData = db.Scalar(FileDirectorySql, new List<DbParam> { new DbParam("infokey", "OriginalData") });
            object sysDate = db.Scalar(SysDateSql, new List<DbParam>());
            DateTime serverDate = sysDate is DateTime ? (DateTime)sysDate : DateTime.MinValue;
            string fileServerText = fileServer == null || fileServer == DBNull.Value ? string.Empty : fileServer.ToString();
            string originalDataText = originalData == null || originalData == DBNull.Value ? string.Empty : originalData.ToString();
            if (string.IsNullOrWhiteSpace(fileServerText) || string.IsNullOrWhiteSpace(originalDataText))
            {
                notes.Add("file_server_config: 无法解析 FileServer/OriginalData 配置");
                return ExitCodes.InfrastructureError;
            }

            string inspectorId = inspectorIds.Count > 0 ? new List<string>(inspectorIds.Values)[0] : null;
            var manifest = new SortedDictionary<string, object>
            {
                { "would_insert_record", new SortedDictionary<string, object>
                    {
                        { "table", "SpecialWoolManage" },
                        { "entity_state", "Detached->AddObject" },
                        { "fields", new SortedDictionary<string, object>
                            {
                                { "ID", "<保存时生成 36 位带连字符 GUID>" },
                                { "SampleNo", target },
                                { "FibreSort", "棉再生纤" },
                                { "CheckWay", "定量" },
                                { "CheckUser1", inspectorId == null ? "<未提供检验员>" : Redact.HashId(inspectorId) + " (由 I8 中文名唯一映射)" },
                                { "CheckUserItem1", "棉再生纤定量-根数法" },
                                { "CheckUserNumber1", 1 },
                                { "ReviewUserNumber1", 1 },
                                { "FilePath", options.SourceFileName },
                                { "FileType", "定量试验" },
                                { "CreateUser", Redact.HashId(staff.Id) + " (登录账号人员)" },
                                { "CreateTime", "<保存时 SELECT SYSDATE FROM DUAL>" },
                            }
                        },
                    }
                },
                { "would_copy_file", new SortedDictionary<string, object>
                    {
                        { "file_name", options.SourceFileName },
                        { "file_server", fileServerText },
                        { "target_dir_pattern", fileServerText + originalDataText + "\\Files\\<年>\\<月>\\<日>\\SpecialWool\\（日期取保存时服务器日期）" },
                        { "predicted_target_dir_today", fileServerText + originalDataText + "\\Files\\" + serverDate.Year + "\\" + serverDate.Month + "\\" + serverDate.Day + "\\SpecialWool\\" },
                        { "target_file", "<target_dir>\\" + options.SourceFileName },
                        { "overwrite_behavior", "File.Copy(overwrite: true)：同名文件将被覆盖（旧客户端既有行为）" },
                    }
                },
            };
            document["manifest"] = manifest;
            document["server_date"] = serverDate == DateTime.MinValue ? null : serverDate.ToString("yyyy-MM-dd");
            notes.Add("dry_run: 未执行任何写入；清单需经用户确认后才可进入真实写入阶段");
            return ExitCodes.Ok;
        }

        /// <summary>与 GetParentDepartmentListSQL 一致：0004-0005-0006 -> [0004, 0004-0005]。</summary>
        internal static List<string> ExpandDeptCodePrefixes(string deptCode)
        {
            var prefixes = new List<string>();
            if (string.IsNullOrWhiteSpace(deptCode))
            {
                return prefixes;
            }
            string[] parts = deptCode.Split('-');
            string current = string.Empty;
            for (int i = 0; i < parts.Length - 1; i++)
            {
                current = current.Length == 0 ? parts[i] : current + "-" + parts[i];
                prefixes.Add(current);
            }
            return prefixes;
        }

        private static string BuildInList(List<string> values, string prefix, List<DbParam> parameters)
        {
            var names = new List<string>();
            for (int i = 0; i < values.Count; i++)
            {
                string name = prefix + i;
                parameters.Add(new DbParam(name, values[i]));
                names.Add(":" + name);
            }
            return string.Join(",", names.ToArray());
        }

        internal static SortedDictionary<string, object> SafeError(Exception ex)
        {
            string code = "unclassified";
            var match = System.Text.RegularExpressions.Regex.Match(ex.Message ?? string.Empty, @"\b(?:ORA|DPI|DPY)-\d{3,5}\b", System.Text.RegularExpressions.RegexOptions.IgnoreCase);
            if (match.Success)
            {
                code = match.Value.ToUpperInvariant();
            }
            return new SortedDictionary<string, object>
            {
                { "type", ex.GetType().Name },
                { "code", code },
            };
        }
    }
}
