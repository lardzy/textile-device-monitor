using System;
using System.Collections.Generic;

namespace LegacyFibreCheckFinalEntryWriter
{
    /// <summary>
    /// Pure duplicate-row resolution for the legacy CustomerOrg lookup.
    /// The table carries many duplicate FullName rows whose branch flags are
    /// identical in practice (often differing only by NULL vs '0'). Duplicates
    /// only block execution when their effective branch flags genuinely
    /// disagree; callers normalize NULL flags to the same "off" value as '0'
    /// before building rows. Kept separate from Oracle access so the semantics
    /// can be tested offline.
    /// </summary>
    internal static class CustomerOrgBranchFlags
    {
        internal sealed class Row
        {
            internal bool ChinaEnglish;
            internal bool OnlyChinaEnglish;
            internal bool ShowAllTarget;
            internal DateTime? ChinaEnglishStart;
        }

        internal sealed class Resolution
        {
            internal bool Conflict;
            internal Row Values; // null when there are no rows or rows conflict
        }

        internal static Resolution Resolve(IList<Row> rows)
        {
            var resolution = new Resolution();
            if (rows == null || rows.Count == 0)
            {
                return resolution;
            }
            Row first = rows[0];
            for (int i = 1; i < rows.Count; i++)
            {
                if (!Same(first, rows[i]))
                {
                    resolution.Conflict = true;
                    return resolution;
                }
            }
            resolution.Values = first;
            return resolution;
        }

        private static bool Same(Row left, Row right)
        {
            // Exact DateTime equality is deliberate: any divergence between
            // duplicate rows (including the start timestamp) stays a conflict,
            // matching the previous "more than one row blocks" strictness for
            // genuinely different rows.
            return left.ChinaEnglish == right.ChinaEnglish
                && left.OnlyChinaEnglish == right.OnlyChinaEnglish
                && left.ShowAllTarget == right.ShowAllTarget
                && Nullable.Equals(left.ChinaEnglishStart, right.ChinaEnglishStart);
        }
    }
}
