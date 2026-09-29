using System;

namespace LegacyFibreCheckFinalEntryWriter
{
    /// <summary>
    /// Pure branch-selection rules matching the desktop client's template lookup.
    /// Kept separate from Oracle access so count semantics can be tested offline.
    /// </summary>
    internal static class LegacyExcelBranchRules
    {
        internal static bool CanSelectTemplate(
            int schemaVersion, bool legacyProjectSupported,
            bool legacyTemplateSupported, string inputUiClassName)
        {
            // The desktop offers both routes when a project has an input UI and
            // Excel templates. Schema 5 selects the template explicitly; the
            // resolver still verifies its unique binding and mapping digest.
            return schemaVersion == 5 || (legacyProjectSupported
                && legacyTemplateSupported && string.IsNullOrWhiteSpace(inputUiClassName));
        }

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
