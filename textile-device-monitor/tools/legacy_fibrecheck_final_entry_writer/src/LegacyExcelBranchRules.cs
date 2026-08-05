using System;

namespace LegacyFibreCheckFinalEntryWriter
{
    /// <summary>
    /// Pure branch-selection rules matching the desktop client's template lookup.
    /// Kept separate from Oracle access so count semantics can be tested offline.
    /// </summary>
    internal static class LegacyExcelBranchRules
    {
        internal static string ResolveBranch(
            int fixedAttachmentTemplateCount,
            bool gap,
            bool newChinaEnglish,
            bool fila,
            bool clothing)
        {
            if (fixedAttachmentTemplateCount < 0)
            {
                throw new ArgumentOutOfRangeException(
                    "fixedAttachmentTemplateCount");
            }

            // Match SaveOriginalDataRecord's actual ordering. GAP is evaluated
            // before the fixed-template lookup result. A fixed template only
            // suppresses the new Chinese-English/FILA branch; CLOTHING remains
            // eligible afterwards. Multiple rows are equivalent to one because
            // the desktop client only tests FirstOrDefault for null.
            if (gap)
            {
                return "gap";
            }
            if (fixedAttachmentTemplateCount == 0
                && (newChinaEnglish || fila))
            {
                return "new_cnen";
            }
            if (clothing)
            {
                return "clothing";
            }
            return "standard";
        }
    }
}
