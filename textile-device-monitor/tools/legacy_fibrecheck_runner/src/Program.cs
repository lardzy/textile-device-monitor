using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace LegacyFibreCheckRunner
{
    /// <summary>
    /// 旧检务系统无界面只读登录核验 Runner（x86 / .NET Framework 4.x）。
    /// 口令只从环境变量 FIBRECHECK_RUNNER_PASSWORD 或 --password-stdin 读取，
    /// 永不进入命令行参数、日志或输出 JSON。
    /// </summary>
    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            RunnerOptions options;
            string parseError;
            if (!TryParseArgs(args, out options, out parseError))
            {
                Console.Error.WriteLine(parseError);
                Console.Error.WriteLine("用法: FibreCheckRunner.exe --fibrecheck-dir <目录> --account <账号> [--function-type <类型全名>] [--inspector-names <名1,名2>] [--out <路径|->] [--password-stdin]");
                return ExitCodes.UsageError;
            }

            if (!Directory.Exists(options.FibreCheckDir))
            {
                Console.Error.WriteLine("--fibrecheck-dir 不存在: " + options.FibreCheckDir);
                return ExitCodes.UsageError;
            }

            if (string.IsNullOrEmpty(options.Password))
            {
                Console.Error.WriteLine("缺少口令：请设置环境变量 FIBRECHECK_RUNNER_PASSWORD 或使用 --password-stdin");
                return ExitCodes.UsageError;
            }

            SystemDataConnection.InstallAssemblyResolver(options.FibreCheckDir);

            SortedDictionary<string, object> document;
            int exitCode = LegacyLoginFlow.Run(options, conn => new OdpNetDb(conn), out document);
            string json = MiniJson.Write(document);
            if (options.OutputPath == "-")
            {
                Console.Out.Write(json);
                Console.Out.Write('\n');
            }
            else
            {
                File.WriteAllText(options.OutputPath, json + "\n", new UTF8Encoding(false));
            }
            return exitCode;
        }

        private static bool TryParseArgs(string[] args, out RunnerOptions options, out string error)
        {
            options = new RunnerOptions();
            error = null;
            bool passwordFromStdin = false;

            for (int i = 0; i < args.Length; i++)
            {
                string current = args[i];
                string value = i + 1 < args.Length ? args[i + 1] : null;
                switch (current)
                {
                    case "--fibrecheck-dir":
                        options.FibreCheckDir = value; i++; break;
                    case "--account":
                        options.Account = value; i++; break;
                    case "--function-type":
                        options.FunctionType = value; i++; break;
                    case "--inspector-names":
                        if (!string.IsNullOrWhiteSpace(value))
                        {
                            foreach (string name in value.Split(','))
                            {
                                string trimmed = name.Trim();
                                if (trimmed.Length > 0)
                                {
                                    options.InspectorNames.Add(trimmed);
                                }
                            }
                        }
                        i++; break;
                    case "--out":
                        options.OutputPath = value; i++; break;
                    case "--dry-run-upload":
                        options.DryRunUpload = true; break;
                    case "--target-sample-number":
                        options.TargetSampleNumber = value; i++; break;
                    case "--source-file-name":
                        options.SourceFileName = value; i++; break;
                    case "--source-inspection-number":
                        options.SourceInspectionNumber = value; i++; break;
                    case "--password-stdin":
                        passwordFromStdin = true; break;
                    default:
                        error = "未知参数: " + current;
                        return false;
                }
            }

            if (string.IsNullOrWhiteSpace(options.FibreCheckDir) || string.IsNullOrWhiteSpace(options.Account))
            {
                error = "--fibrecheck-dir 与 --account 为必填项";
                return false;
            }

            if (options.DryRunUpload && string.IsNullOrWhiteSpace(options.TargetSampleNumber))
            {
                error = "--dry-run-upload 模式必须提供 --target-sample-number";
                return false;
            }

            options.Password = Environment.GetEnvironmentVariable("FIBRECHECK_RUNNER_PASSWORD");
            if (passwordFromStdin)
            {
                options.Password = Console.In.ReadLine();
            }
            return true;
        }
    }
}
