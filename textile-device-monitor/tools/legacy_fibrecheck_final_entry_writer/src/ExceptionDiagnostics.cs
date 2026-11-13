using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text.RegularExpressions;

namespace LegacyFibreCheckFinalEntryWriter
{
    internal static class ExceptionDiagnostics
    {
        private static readonly string[] SecretVariables = {
            "FIBRECHECK_RUNNER_PASSWORD", "LEGACY_FIBRECHECK_PASSWORD",
            "EXECUTION_BRIDGE_TOKEN", "SMB_PASS_A", "SMB_PASS_B",
        };
        private static readonly Regex PasswordAssignment = new Regex(
            @"(?i)(\b(?:password|pwd)\s*=\s*)(?:""[^""]*""|'[^']*'|[^;\r\n]*)");

        public static object Capture(Exception exception)
        {
            if (exception == null) return null;
            var inner = new List<object>();
            var aggregate = exception as AggregateException;
            if (aggregate != null)
            {
                foreach (Exception child in aggregate.InnerExceptions)
                    inner.Add(Capture(child));
            }
            else if (exception.InnerException != null)
            {
                inner.Add(Capture(exception.InnerException));
            }
            // Capture exception fields only; Exception.Data and process/configuration
            // contents are deliberately not part of the diagnostic receipt.
            return new SortedDictionary<string, object>
            {
                { "exception_type", exception.GetType().FullName },
                { "message", Scrub(exception.Message) },
                { "hresult", "0x" + unchecked((uint)exception.HResult).ToString("X8", CultureInfo.InvariantCulture) },
                { "stack_trace", Scrub(exception.StackTrace) },
                { "inner_exceptions", inner },
            };
        }

        private static string Scrub(string value)
        {
            if (value == null) return null;
            foreach (string name in SecretVariables)
            {
                string secret = Environment.GetEnvironmentVariable(name);
                if (!string.IsNullOrEmpty(secret)) value = value.Replace(secret, "[redacted]");
            }
            return PasswordAssignment.Replace(value, "$1[redacted]");
        }
    }
}
