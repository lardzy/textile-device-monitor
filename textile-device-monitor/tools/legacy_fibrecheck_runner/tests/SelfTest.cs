using System;
using System.Collections.Generic;
using System.Data;
using System.Linq;

namespace LegacyFibreCheckRunner.Tests
{
    /// <summary>自测：纯函数断言 + FakeDb 全流程场景。不连接真实 Oracle。</summary>
    internal static class SelfTest
    {
        private static int _failures;

        private static int Main()
        {
            TestPureFunctions();
            TestHappyPath();
            TestAccountNotFound();
            TestDuplicateAccount();
            TestPasswordMismatch();
            TestPermissionDenied();
            TestInspectorAmbiguous();

            if (_failures > 0)
            {
                Console.Error.WriteLine("自测失败数: " + _failures);
                return 1;
            }
            Console.WriteLine("全部自测通过");
            return 0;
        }

        private static void Check(bool condition, string name)
        {
            if (!condition)
            {
                _failures++;
                Console.Error.WriteLine("FAIL: " + name);
            }
        }

        private static void TestPureFunctions()
        {
            Check(LegacyLoginFlow.ExpandDeptCodePrefixes("0004-0005-0006").SequenceEqual(new[] { "0004", "0004-0005" }), "ExpandDeptCodePrefixes 前缀展开");
            Check(LegacyLoginFlow.ExpandDeptCodePrefixes("0004").Count == 0, "ExpandDeptCodePrefixes 单段为空");
            Check(LegacyLoginFlow.ExpandDeptCodePrefixes("").Count == 0, "ExpandDeptCodePrefixes 空串");

            Check(Redact.EncryptByMd5("abc123") == "E99A18C428CB38D5F260853678922E03", "MD5 与旧系统格式一致（大写 X2）");
            Check(Redact.CheckPasswordIsOk("Abc12345") == string.Empty, "口令策略通过");
            Check(Redact.CheckPasswordIsOk("abc") != string.Empty, "口令策略拒绝短口令");
            Check(Redact.CheckPasswordIsOk("abcdefgh") != string.Empty, "口令策略拒绝无数字大写");

            Check(Redact.MaskLogin("lisy") == "l**y", "登录名掩码");
            Check(Redact.MaskLogin("ab") == "a*", "登录名掩码两位");
            string hash = Redact.HashId("x");
            Check(hash.StartsWith("sha256:") && hash.Length == 7 + 16, "ID 散列格式");

            string json = MiniJson.Write(new SortedDictionary<string, object> { { "a", "b\"c\nd" }, { "n", 3 } });
            Check(json.Contains("\"a\": \"b\\\"c\\nd\""), "MiniJson 字符串转义");
        }

        private sealed class FakeDb : ILegacyDb
        {
            public readonly List<string> Executed = new List<string>();
            public bool RolledBack;
            private readonly List<KeyValuePair<string, Func<DataTable>>> _tables = new List<KeyValuePair<string, Func<DataTable>>>();
            private readonly List<KeyValuePair<string, Func<object>>> _scalars = new List<KeyValuePair<string, Func<object>>>();

            public void OnTable(string fragment, Func<DataTable> table)
            {
                _tables.RemoveAll(p => p.Key == fragment);
                _tables.Add(new KeyValuePair<string, Func<DataTable>>(fragment, table));
            }

            public void OnScalar(string fragment, Func<object> value)
            {
                _scalars.RemoveAll(p => p.Key == fragment);
                _scalars.Add(new KeyValuePair<string, Func<object>>(fragment, value));
            }

            public void OpenReadOnly() { Executed.Add(OdpNetDb.ReadOnlyTransactionSql); }

            public DataTable Query(string sql, IList<DbParam> parameters)
            {
                Executed.Add(sql);
                foreach (var pair in _tables)
                {
                    if (sql.Contains(pair.Key)) { return pair.Value(); }
                }
                return new DataTable();
            }

            public object Scalar(string sql, IList<DbParam> parameters)
            {
                Executed.Add(sql);
                foreach (var pair in _scalars)
                {
                    if (sql.Contains(pair.Key)) { return pair.Value(); }
                }
                return null;
            }

            public void RollbackAndClose() { RolledBack = true; }
            public void Dispose() { }
        }

        private static DataTable Table(params object[] rowsAndColumns)
        {
            // 第一行是列名，其余每行是一组值
            var table = new DataTable();
            var columns = (string[])rowsAndColumns[0];
            foreach (string column in columns) { table.Columns.Add(column); }
            for (int i = 1; i < rowsAndColumns.Length; i++)
            {
                var values = (object[])rowsAndColumns[i];
                var row = table.NewRow();
                for (int c = 0; c < columns.Length; c++) { row[c] = values[c]; }
                table.Rows.Add(row);
            }
            return table;
        }

        private static FakeDb BuildHappyFake(string loginPasswordHash)
        {
            var fake = new FakeDb();
            fake.OnScalar("KeyValues", () => 1);
            fake.OnScalar("User_Position\", \"Position\",\"Department", () => "材料检测中心");
            fake.OnTable("LoginPassword", () => Table(
                new[] { "ID", "LoginPassword", "ChineseName", "Remark" },
                new object[] { "STAFF-1", loginPasswordHash, "李树阳", "" }));
            fake.OnTable("SubDepartmentName", () => Table(
                new[] { "PositionID", "PositionName", "DepartmentID", "SubDepartmentID", "DepartmentName", "DeptCode", "SubDepartmentName" },
                new object[] { "POS-1", "检验员", "DEPT-1", "SUB-1", "材料检测中心", "0001-0002", "物理组" }));
            fake.OnScalar("FROM \"Department\" D", () => "PDEPT-1");
            fake.OnTable("PV_FunctionDefinition", () => Table(
                new[] { "FunctionID", "FunctionName", "FunctionType" },
                new object[] { "FUNC-1", "检验", "Toone.FibreCheck.OriRecord.SpecialWool.SpecialWoolSearchUI" }));
            fake.OnScalar("COUNT(*) FROM \"PV_PurviewAssign\"", () => 1);
            fake.OnTable("PV_PurviewDefinition", () => Table(
                new[] { "PurviewID", "FunctionID", "ControlIdentity", "PurviewName", "Description", "SeqNum", "PurviewControlMode", "PID" },
                new object[] { "PV-1", "FUNC-1", "btnAdd", "新建", "", 1, 0, "PV-1" }));
            fake.OnTable(":chinesename", () => Table(
                new[] { "ID", "ChineseName", "IsDeleted" },
                new object[] { "STAFF-9", "辜惠珊", 0 }));
            return fake;
        }

        private static RunnerOptions HappyOptions()
        {
            return new RunnerOptions
            {
                FibreCheckDir = ".",
                Account = "lisy",
                Password = "Abc12345",
                InspectorNames = { "辜惠珊" },
                ConnectionStringResolver = () => "fake-connection-string",
            };
        }

        private static int RunWithFake(FakeDb fake, out SortedDictionary<string, object> document)
        {
            // 绕过 SystemData 反射：连接串由 resolver 提供，工厂直接返回 fake
            return LegacyLoginFlow.Run(HappyOptions(), conn => fake, out document);
        }

        private static void TestHappyPath()
        {
            var fake = BuildHappyFake(Redact.EncryptByMd5("Abc12345"));
            SortedDictionary<string, object> document;
            int exit = RunWithFake(fake, out document);
            Check(exit == ExitCodes.Ok, "happy path 退出码");
            Check(fake.Executed.Count > 0 && fake.Executed[0] == OdpNetDb.ReadOnlyTransactionSql, "首条 SQL 为只读事务");
            Check(fake.RolledBack, "结束 rollback");
            Check(fake.Executed.All(sql => sql == OdpNetDb.ReadOnlyTransactionSql
                || sql.TrimStart().StartsWith("Select", StringComparison.OrdinalIgnoreCase)
                || sql.TrimStart().StartsWith("SELECT")), "仅 SELECT");
            Check((bool)document["password_match"], "口令匹配");
            Check((bool)document["can_login_system"], "系统可用");
            var permission = (SortedDictionary<string, object>)document["function_permission"];
            Check((bool)permission["granted"], "功能授权通过");
            Check(Equals(permission["function_id"], Redact.HashId("FUNC-1")), "FunctionID 解析并散列");
            var mappings = (List<object>)document["inspector_mappings"];
            var mapping = (SortedDictionary<string, object>)mappings[0];
            Check(Equals(mapping["status"], "unique"), "检验员唯一映射");
            var staff = (SortedDictionary<string, object>)document["staff"];
            Check(Equals(staff["chinese_name"], "李树阳"), "人员中文名");
            Check(!document.ContainsKey("error"), "无错误字段");
            string json = MiniJson.Write(document);
            Check(!json.Contains("STAFF-1") && !json.Contains("FUNC-1"), "内部 ID 不泄漏");
            Check(!json.Contains("Abc12345"), "口令不泄漏");
        }

        private static void TestAccountNotFound()
        {
            var empty = new FakeDb();
            empty.OnScalar("KeyValues", () => 1);
            empty.OnScalar("User_Position\", \"Position\",\"Department", () => "材料检测中心");
            SortedDictionary<string, object> document;
            int exit = LegacyLoginFlow.Run(HappyOptions(), conn => empty, out document);
            Check(exit == ExitCodes.AccountNotFound, "账号不存在退出码");
        }

        private static void TestDuplicateAccount()
        {
            var fake = BuildHappyFake(Redact.EncryptByMd5("Abc12345"));
            fake.OnTable("LoginPassword", () => Table(
                new[] { "ID", "LoginPassword", "ChineseName", "Remark" },
                new object[] { "A", "x", "张三", "" },
                new object[] { "B", "y", "张三", "" }));
            SortedDictionary<string, object> document;
            int exit = RunWithFake(fake, out document);
            Check(exit == ExitCodes.DuplicateAccount, "重复账号退出码");
        }

        private static void TestPasswordMismatch()
        {
            var fake = BuildHappyFake("WRONGHASH");
            SortedDictionary<string, object> document;
            int exit = RunWithFake(fake, out document);
            Check(exit == ExitCodes.PasswordMismatch, "口令不匹配退出码");
            Check(!(bool)document["password_match"], "口令不匹配标记");
        }

        private static void TestPermissionDenied()
        {
            var fake = BuildHappyFake(Redact.EncryptByMd5("Abc12345"));
            fake.OnScalar("COUNT(*) FROM \"PV_PurviewAssign\"", () => 0);
            SortedDictionary<string, object> document;
            int exit = RunWithFake(fake, out document);
            Check(exit == ExitCodes.PermissionDenied, "权限不足退出码");
        }

        private static void TestInspectorAmbiguous()
        {
            var fake = BuildHappyFake(Redact.EncryptByMd5("Abc12345"));
            fake.OnTable(":chinesename", () => Table(
                new[] { "ID", "ChineseName", "IsDeleted" },
                new object[] { "A", "辜惠珊", 0 },
                new object[] { "B", "辜惠珊", 0 }));
            SortedDictionary<string, object> document;
            int exit = RunWithFake(fake, out document);
            Check(exit == ExitCodes.InspectorMappingFailed, "检验员歧义退出码");
            var mappings = (List<object>)document["inspector_mappings"];
            Check(Equals(((SortedDictionary<string, object>)mappings[0])["status"], "ambiguous"), "检验员歧义状态");
        }
    }
}
