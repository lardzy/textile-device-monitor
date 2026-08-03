using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using LegacyFibreCheckRunner;

namespace LegacyFibreCheckFinalEntryWriter
{
    internal sealed class CommandLine
    {
        public string FibreCheckDir;
        public string Account;
        public string PackagePath;
        public string SourceRoot;
        public string WorkRoot;
        public bool Execute;
        public bool SideEffectPermitStdin;
        public bool OfflineValidate;

        public static CommandLine Parse(string[] args)
        {
            var options = new CommandLine();
            for (int i = 0; i < args.Length; i++)
            {
                string value = i + 1 < args.Length ? args[i + 1] : null;
                switch (args[i])
                {
                    case "--fibrecheck-dir": options.FibreCheckDir = value; i++; break;
                    case "--account": options.Account = value; i++; break;
                    case "--package": options.PackagePath = value; i++; break;
                    case "--source-root": options.SourceRoot = value; i++; break;
                    case "--work-root": options.WorkRoot = value; i++; break;
                    case "--execute": options.Execute = true; break;
                    case "--side-effect-permit-stdin": options.SideEffectPermitStdin = true; break;
                    case "--offline-validate": options.OfflineValidate = true; break;
                    default: throw new PackageValidationException("unknown_cli_argument");
                }
            }
            if (string.IsNullOrWhiteSpace(options.PackagePath))
            {
                throw new PackageValidationException("package_argument_required");
            }
            if (options.Execute && !options.SideEffectPermitStdin)
            {
                throw new PackageValidationException("execute_requires_side_effect_permit_stdin");
            }
            if (!options.Execute && options.SideEffectPermitStdin)
            {
                throw new PackageValidationException("permit_stdin_without_execute_rejected");
            }
            if (!options.OfflineValidate
                && (string.IsNullOrWhiteSpace(options.FibreCheckDir)
                    || string.IsNullOrWhiteSpace(options.Account)))
            {
                throw new PackageValidationException("legacy_login_arguments_required");
            }
            return options;
        }
    }

    internal sealed class ReceiptEmitter
    {
        private readonly List<object> stages = new List<object>();

        public void Stage(string name, object detail)
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
            stages.Add(entry);
            Console.Out.Write(MiniJson.Write(entry) + "\n");
            Console.Out.Flush();
        }

        public int Finish(int exitCode, string errorCode, bool reconciliationRequired, FinalEntryPackage package)
        {
            if (reconciliationRequired)
            {
                Stage("reconciliation_required", new SortedDictionary<string, object>
                {
                    { "reason", "remote_side_effect_may_have_started" },
                    { "automatic_retry_allowed", false },
                });
            }
            var receipt = new SortedDictionary<string, object>
            {
                { "schema_version", 1 },
                { "mode", package == null ? "unparsed" : package.OperationType },
                { "exit_code", exitCode },
                { "reconciliation_required", reconciliationRequired },
                { "stages", stages },
            };
            if (package != null)
            {
                receipt["sample_number"] = package.SampleNumber;
                receipt["check_item_no"] = package.CheckItemNo;
                receipt["check_item_name"] = package.CheckItemName;
            }
            if (!string.IsNullOrWhiteSpace(errorCode))
            {
                receipt["error"] = errorCode;
            }
            Console.Out.Write(MiniJson.Write(new SortedDictionary<string, object>
            {
                { "receipt", receipt },
            }) + "\n");
            Console.Out.Flush();
            return exitCode;
        }
    }

    internal sealed class WriterFailureException : Exception
    {
        public string Code { get; private set; }
        public int ExitCode { get; private set; }
        public bool ReconciliationRequired { get; private set; }

        public WriterFailureException(string code, int exitCode, bool reconciliationRequired)
            : base(code)
        {
            Code = code;
            ExitCode = exitCode;
            ReconciliationRequired = reconciliationRequired;
        }
    }

    internal static class Program
    {
        internal const int ExitPackageError = 21;
        internal const int ExitSourceMismatch = 22;
        internal const int ExitRemoteConflict = 23;
        internal const int ExitPermitDenied = 24;
        internal const int ExitReconciliationRequired = 30;
        internal const string FunctionType =
            "Toone.FibreCheck.BusinessProcess.BusinessProcessUI.CheckRecord.CheckRecordRegisterUI";

        [STAThread]
        private static int Main(string[] args)
        {
            try { Console.OutputEncoding = System.Text.Encoding.UTF8; }
            catch { }

            var emit = new ReceiptEmitter();
            FinalEntryPackage package = null;
            try
            {
                CommandLine options = CommandLine.Parse(args);
                package = FinalEntryPackage.Load(options.PackagePath);
                emit.Stage("package_validated", new SortedDictionary<string, object>
                {
                    { "operation_type", package.OperationType },
                    { "expected_existing_register_count", package.ExpectedExistingRegisterCount },
                });
                if (package.OperationType == FinalEntryPackage.GenericOperation)
                {
                    emit.Stage("generic_header_validated", null);
                    emit.Stage("generic_details_validated", new SortedDictionary<string, object>
                    {
                        { "row_count", package.GenericRecord.Details.Count },
                    });
                }

                string workbookPath = package.ResolveAndVerifyWorkbook(options.SourceRoot);
                if (package.OperationType == FinalEntryPackage.ExcelOperation)
                {
                    emit.Stage("workbook_verified", new SortedDictionary<string, object>
                    {
                        { "filename", package.ExcelRecord.Workbook.Filename },
                        { "size_bytes", package.ExcelRecord.Workbook.SizeBytes },
                        { "content_sha256", package.ExcelRecord.Workbook.ContentSha256 },
                        { "key_result_count", package.ExcelRecord.KeyResultCount },
                    });
                }

                if (options.OfflineValidate)
                {
                    if (options.Execute)
                    {
                        throw new PackageValidationException("offline_validate_cannot_execute");
                    }
                    emit.Stage("offline_validation_completed", null);
                    return emit.Finish(0, null, false, package);
                }

                string password = Environment.GetEnvironmentVariable("FIBRECHECK_RUNNER_PASSWORD");
                if (string.IsNullOrEmpty(password))
                {
                    throw new PackageValidationException("runner_password_environment_missing");
                }
                SystemDataConnection.InstallAssemblyResolver(options.FibreCheckDir);
                return FinalEntryExecutor.Run(options, package, workbookPath, password, emit, WaitForPermit);
            }
            catch (WriterFailureException ex)
            {
                return emit.Finish(ex.ExitCode, ex.Code, ex.ReconciliationRequired, package);
            }
            catch (PackageValidationException ex)
            {
                return emit.Finish(ExitPackageError, ex.Code, false, package);
            }
            catch (Exception ex)
            {
                // Exception messages can contain credentials, IDs, or paths.  Return only
                // the type category and never echo Message/StackTrace.
                string code = "unhandled_" + ex.GetType().Name;
                return emit.Finish(ExitPackageError, code, false, package);
            }
        }

        private static bool WaitForPermit()
        {
            string decision = null;
            Exception readError = null;
            var completed = new ManualResetEvent(false);
            var thread = new Thread(delegate()
            {
                try { decision = Console.In.ReadLine(); }
                catch (Exception ex) { readError = ex; }
                finally { completed.Set(); }
            });
            thread.IsBackground = true;
            thread.Start();
            if (!completed.WaitOne(TimeSpan.FromSeconds(60)))
            {
                return false;
            }
            return readError == null
                && string.Equals(decision, "PERMIT_REMOTE_WRITE", StringComparison.Ordinal);
        }
    }
}
