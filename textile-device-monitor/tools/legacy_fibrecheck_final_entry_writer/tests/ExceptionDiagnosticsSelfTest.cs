using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;

namespace LegacyFibreCheckFinalEntryWriter
{
    internal static class ExceptionDiagnosticsSelfTest
    {
        private static int passed;
        private static Assembly writer;
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

        private static void Check(bool value, string name)
        {
            if (!value) throw new InvalidOperationException(name);
            passed++;
        }

        private static Exception CaptureThrown(Exception exception)
        {
            try { throw exception; }
            catch (Exception caught) { return caught; }
        }

        private static Dictionary<string, object> Diagnostic(Exception exception)
        {
            object result = writer.GetType("LegacyFibreCheckFinalEntryWriter.ExceptionDiagnostics")
                .GetMethod("Capture", BindingFlags.Public | BindingFlags.Static)
                .Invoke(null, new object[] { exception });
            return (Dictionary<string, object>)Json.DeserializeObject(Json.Serialize(result));
        }

        private static Dictionary<string, object> Receipt(string code, Exception exception)
        {
            Type emitterType = writer.GetType("LegacyFibreCheckFinalEntryWriter.ReceiptEmitter");
            object emitter = Activator.CreateInstance(emitterType, true);
            TextWriter original = Console.Out;
            var output = new StringWriter();
            try
            {
                Console.SetOut(output);
                emitterType.GetMethod("Finish").Invoke(emitter,
                    new object[] { code == null ? 0 : 30, code, code != null, null, exception });
            }
            finally { Console.SetOut(original); }
            string text = output.ToString();
            int receiptStart = text.IndexOf("{\n  \"receipt\":", StringComparison.Ordinal);
            if (receiptStart < 0) throw new InvalidOperationException("Writer did not emit a receipt");
            var envelope = (Dictionary<string, object>)Json.DeserializeObject(text.Substring(receiptStart));
            return (Dictionary<string, object>)envelope["receipt"];
        }

        private static Dictionary<string, object> FirstInner(Dictionary<string, object> diagnostic)
        {
            return (Dictionary<string, object>)((object[])diagnostic["inner_exceptions"])[0];
        }

        private static void Main(string[] args)
        {
            writer = Assembly.LoadFrom(args[0]);
            const string password = "diagnostic-password-with=equals";
            const string token = "diagnostic-bridge-token";
            string originalPassword = Environment.GetEnvironmentVariable("FIBRECHECK_RUNNER_PASSWORD");
            string originalToken = Environment.GetEnvironmentVariable("EXECUTION_BRIDGE_TOKEN");
            try
            {
                Environment.SetEnvironmentVariable("FIBRECHECK_RUNNER_PASSWORD", password);
                Environment.SetEnvironmentVariable("EXECUTION_BRIDGE_TOKEN", token);
                Exception inner = CaptureThrown(new IOException("无法读取 C:\\records\\原始记录.xls"));
                Exception com = CaptureThrown(new COMException("Excel 采集失败：" + password, inner));
                com.Data["configuration"] = "this-must-not-be-serialized";
                Dictionary<string, object> diagnostic = Diagnostic(com);
                Check((string)diagnostic["exception_type"] == "System.Runtime.InteropServices.COMException", "COM type");
                Check((string)diagnostic["hresult"] == "0x80004005", "COM default HRESULT");
                Check(((string)diagnostic["message"]).Contains("[redacted]"), "known password redacted");
                Check(((string)diagnostic["stack_trace"]).Contains("CaptureThrown"), "outer stack retained");
                Dictionary<string, object> innerDiagnostic = FirstInner(diagnostic);
                Check((string)innerDiagnostic["exception_type"] == "System.IO.IOException", "inner type");
                Check(((string)innerDiagnostic["message"]).Contains("原始记录.xls"), "unicode path retained");
                Check(((string)innerDiagnostic["stack_trace"]).Contains("CaptureThrown"), "inner stack retained");
                Check(!Json.Serialize(diagnostic).Contains("this-must-not-be-serialized"), "exception Data not captured");

                var explicitCom = Diagnostic(new COMException("Excel 应用程序错误", unchecked((int)0x800A03EC)));
                Check((string)explicitCom["hresult"] == "0x800A03EC", "explicit COM HRESULT");
                Check(explicitCom["stack_trace"] == null, "unthrown exception has null stack");
                Exception generic = CaptureThrown(new InvalidOperationException("Oracle 登记失败", com));
                Dictionary<string, object> genericDiagnostic = Diagnostic(generic);
                Check((string)genericDiagnostic["exception_type"] == "System.InvalidOperationException", "generic type");
                Check((string)FirstInner(FirstInner(genericDiagnostic))["exception_type"] == "System.IO.IOException", "full nested chain");

                Type failureType = writer.GetType("LegacyFibreCheckFinalEntryWriter.WriterFailureException");
                foreach (string kind in new[] { "excel", "generic" })
                {
                    Exception original = kind == "excel" ? com : generic;
                    string code = kind + "_write_failed_" + original.GetType().Name;
                    var wrapped = (Exception)Activator.CreateInstance(failureType,
                        new object[] { code, 30, true, original });
                    Check(Object.ReferenceEquals(wrapped.InnerException, original), kind + " wrapper retains cause");
                    var receipt = Receipt(code, wrapped.InnerException);
                    Check((string)receipt["error"] == code, kind + " legacy business code retained");
                    Check((bool)receipt["reconciliation_required"], kind + " failure state retained");
                    Check((string)((Dictionary<string, object>)receipt["diagnostics"])["exception_type"]
                        == original.GetType().FullName, kind + " diagnostics carried by actual receipt emitter");
                }

                var aggregate = Diagnostic(new AggregateException("two failures", com, generic));
                Check(((object[])aggregate["inner_exceptions"]).Length == 2, "aggregate branches retained");
                string longMessage = new string('x', 12000) + " 尾部完整";
                Check((string)Diagnostic(new Exception(longMessage))["message"] == longMessage, "message not truncated");
                string connectionMessage = "token=" + token + "; Password=\"not;public\"; Pwd='another=secret'; Data Source=server";
                string scrubbed = (string)Diagnostic(new Exception(connectionMessage))["message"];
                Check(!scrubbed.Contains(token) && !scrubbed.Contains("not;public") && !scrubbed.Contains("another=secret"), "token and quoted connection passwords redacted");
                Check(scrubbed.Contains("Data Source=server"), "non-secret error context retained");
                Check(!Receipt(null, null).ContainsKey("diagnostics"), "success receipt schema unchanged");
                Check(!Receipt("validation_error", null).ContainsKey("diagnostics"), "no fabricated exception details");
            }
            finally
            {
                Environment.SetEnvironmentVariable("FIBRECHECK_RUNNER_PASSWORD", originalPassword);
                Environment.SetEnvironmentVariable("EXECUTION_BRIDGE_TOKEN", originalToken);
            }
            Console.WriteLine("Exception diagnostics self-test: " + passed + " passed");
        }
    }
}
