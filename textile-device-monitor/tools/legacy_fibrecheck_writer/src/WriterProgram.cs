using System;
using System.Collections.Generic;
using System.Runtime.CompilerServices;
using System.Threading;
using LegacyFibreCheckRunner;
using Toone.FibreCheck.Entites.CommonEntities;
using Toone.FibreCheck.Entites.CommonEntities.CommunicationEntities;
using Toone.FibreCheck.OriRecord.SpecialWool;

namespace LegacyFibreCheckWriter
{
    /// <summary>
    /// 写入机制 spike：验证旧系统官方 DAL 能否在本进程内经托管 ODP.NET 工作。
    /// 本模式仅执行只读 GetByReportNo，不调用任何保存方法。
    /// </summary>
    internal static class WriterProgram
    {
        [STAThread]
        private static int Main(string[] args)
        {
            // Bridge 以 UTF-8 解析 stdout；中文 Windows 控制台默认 GBK，必须显式切换
            try
            {
                Console.OutputEncoding = System.Text.Encoding.UTF8;
            }
            catch
            {
                // 某些宿主不允许改编码，忽略（对端按容错解码）
            }
            string fibreCheckDir = null;
            string account = null;
            string probeSampleNo = null;
            string packagePath = null;
            string sourceRoot = null;
            bool executeUpload = false;
            bool sideEffectPermitStdin = false;
            for (int i = 0; i < args.Length; i++)
            {
                string value = i + 1 < args.Length ? args[i + 1] : null;
                switch (args[i])
                {
                    case "--fibrecheck-dir": fibreCheckDir = value; i++; break;
                    case "--account": account = value; i++; break;
                    case "--probe-sample-no": probeSampleNo = value; i++; break;
                    case "--execute-upload": executeUpload = true; break;
                    case "--side-effect-permit-stdin": sideEffectPermitStdin = true; break;
                    case "--package": packagePath = value; i++; break;
                    case "--source-root": sourceRoot = value; i++; break;
                    default:
                        Console.Error.WriteLine("未知参数: " + args[i]);
                        return 2;
                }
            }
            if (string.IsNullOrWhiteSpace(fibreCheckDir) || string.IsNullOrWhiteSpace(account))
            {
                Console.Error.WriteLine("用法: FibreCheckWriter.exe --fibrecheck-dir <目录> --account <账号> [--probe-sample-no <编号> | --execute-upload --side-effect-permit-stdin --package <任务包.json> --source-root <目录>]");
                return 2;
            }
            if (executeUpload && (
                string.IsNullOrWhiteSpace(packagePath)
                || string.IsNullOrWhiteSpace(sourceRoot)
                || !sideEffectPermitStdin))
            {
                Console.Error.WriteLine("--execute-upload 模式必须提供任务包、源目录和 stdin 副作用许可协议");
                return 2;
            }
            string password = Environment.GetEnvironmentVariable("FIBRECHECK_RUNNER_PASSWORD");
            if (string.IsNullOrEmpty(password))
            {
                Console.Error.WriteLine("缺少口令：请设置环境变量 FIBRECHECK_RUNNER_PASSWORD");
                return 2;
            }

            // 必须先安装程序集解析器，再进入任何引用旧程序 DLL / ODP.NET 的代码路径
            SystemDataConnection.InstallAssemblyResolver(fibreCheckDir);
            if (executeUpload)
            {
                return RunUpload(fibreCheckDir, account, password, packagePath, sourceRoot);
            }
            return RunIsolated(fibreCheckDir, account, probeSampleNo, password);
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        private static int RunUpload(string fibreCheckDir, string account, string password, string packagePath, string sourceRoot)
        {
            Action<object> emit = entry => Console.Out.Write(MiniJson.Write(entry) + "\n");
            try
            {
                return UploadExecutor.Execute(
                    fibreCheckDir,
                    account,
                    password,
                    packagePath,
                    sourceRoot,
                    emit,
                    WaitForSideEffectPermit);
            }
            catch (Exception ex)
            {
                emit(new SortedDictionary<string, object>
                {
                    { "stage", "reconciliation_required" },
                    { "at", DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") },
                    { "detail", "未捕获异常: " + ex.GetType().Name },
                });
                string message = (ex.Message ?? string.Empty).Replace(password, "***");
                emit(new SortedDictionary<string, object>
                {
                    { "receipt", new SortedDictionary<string, object>
                        {
                            { "error", "unhandled: " + ex.GetType().Name },
                            { "error_detail", message },
                            { "reconciliation_required", true },
                        }
                    },
                });
                return UploadExecutor.ExitReconciliationRequired;
            }
        }

        private static bool WaitForSideEffectPermit()
        {
            string decision = null;
            Exception readError = null;
            var completed = new ManualResetEvent(false);
            var reader = new Thread(() =>
            {
                try
                {
                    decision = Console.In.ReadLine();
                }
                catch (Exception ex)
                {
                    readError = ex;
                }
                finally
                {
                    completed.Set();
                }
            });
            reader.IsBackground = true;
            reader.Start();
            if (!completed.WaitOne(TimeSpan.FromSeconds(60)))
            {
                return false;
            }
            return readError == null
                && string.Equals(
                    decision,
                    "PERMIT_REMOTE_WRITE",
                    StringComparison.Ordinal);
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        private static int RunIsolated(string fibreCheckDir, string account, string probeSampleNo, string password)
        {
            var document = new SortedDictionary<string, object>
            {
                { "schema_version", 1 },
                { "mode", "dal_read_probe" },
                { "generated_at", DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") },
                { "account", Redact.MaskLogin(account) },
            };

            try
            {
                var options = new RunnerOptions
                {
                    FibreCheckDir = fibreCheckDir,
                    Account = account,
                    Password = password,
                };
                SortedDictionary<string, object> loginDocument;
                LegacyLoginFlow.StaffContext staff;
                int exit = LegacyLoginFlow.RunCore(options, conn => new OdpNetDb(conn), out loginDocument, out staff);
                document["login"] = loginDocument;
                if (exit != 0 || staff == null)
                {
                    Console.Out.Write(MiniJson.Write(document) + "\n");
                    return exit;
                }

                // 与旧客户端一致：登录后注入全局登录人（DAL 据此写 CreateUser 审计字段）
                SystemData.Instance.CurrentLoginedStaff = new StaffEntity
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

                var dal = new SpecialWoolDAL();
                // 诊断：记录 EntityClient 将实际使用的 provider 工厂来源
                try
                {
                    var factory = System.Data.Common.DbProviderFactories.GetFactory("Oracle.DataAccess.Client");
                    var asm = factory.GetType().Assembly;
                    using (var probeConn = factory.CreateConnection())
                    {
                        var services = System.Data.Common.DbProviderServices.GetProviderServices(probeConn);
                        document["provider_factory"] = new SortedDictionary<string, object>
                        {
                            { "assembly", asm.FullName },
                            { "location", asm.Location },
                            { "services_type", services.GetType().FullName },
                            { "services_assembly", services.GetType().Assembly.Location },
                        };
                    }
                }
                catch (Exception factoryEx)
                {
                    document["provider_factory"] = new SortedDictionary<string, object>
                    {
                        { "error", factoryEx.GetType().Name + ": " + factoryEx.Message },
                    };
                }
                var record = string.IsNullOrWhiteSpace(probeSampleNo)
                    ? null
                    : dal.GetByReportNo(probeSampleNo, "");
                document["dal_read"] = new SortedDictionary<string, object>
                {
                    { "method", "SpecialWoolDAL.GetByReportNo" },
                    { "probe_sample_no", probeSampleNo },
                    { "found", record != null },
                    { "record", record == null ? null : new SortedDictionary<string, object>
                        {
                            { "id", Redact.HashId(record.ID) },
                            { "sample_no", record.SampleNo },
                            { "fibre_sort", record.FibreSort },
                            { "check_way", record.CheckWay },
                            { "check_user_item1", record.CheckUserItem1 },
                            { "check_user_number1", record.CheckUserNumber1 },
                            { "check_user1", Redact.HashId(record.CheckUser1) },
                            { "file_type", record.FileType },
                            { "create_time", record.CreateTime.HasValue ? record.CreateTime.Value.ToString("yyyy-MM-ddTHH:mm:ss") : null },
                        }
                    },
                };
                Console.Out.Write(MiniJson.Write(document) + "\n");
                return 0;
            }
            catch (Exception ex)
            {
                document["error"] = LegacyLoginFlow.SafeError(ex);
                string detail = ex.GetType().FullName + ": " + ex.Message;
                document["error_detail"] = password == null ? detail : detail.Replace(password, "***");
                string trace = ex.StackTrace ?? string.Empty;
                if (ex.InnerException != null)
                {
                    trace = ex.InnerException.StackTrace + "\n--outer--\n" + trace;
                }
                document["error_trace"] = (password == null ? trace : trace.Replace(password, "***"));
                Console.Out.Write(MiniJson.Write(document) + "\n");
                return 3;
            }
        }
    }
}
