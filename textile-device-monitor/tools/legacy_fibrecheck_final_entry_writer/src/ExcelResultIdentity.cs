using System;
using System.Collections.Generic;
using System.Globalization;

namespace LegacyFibreCheckFinalEntryWriter
{
    internal static class ExcelResultIdentity
    {
        // Official templates may omit a sequence, use nonconsecutive sequences,
        // or repeat a sequence in separate configuration groups. Preserve those
        // values for readback instead of inventing a one-based row number.
        internal static string Key(string recordId, string group, object sequence)
        {
            return recordId + "\0" + (group ?? string.Empty) + "\0"
                + Convert.ToString(sequence, CultureInfo.InvariantCulture);
        }

        internal static bool Same(
            IDictionary<string, string> left, IDictionary<string, string> right)
        {
            if (left.Count != right.Count) return false;
            foreach (var row in left)
            {
                string value;
                if (!right.TryGetValue(row.Key, out value)
                    || !string.Equals(row.Value, value, StringComparison.Ordinal)) return false;
            }
            return true;
        }
    }
}
