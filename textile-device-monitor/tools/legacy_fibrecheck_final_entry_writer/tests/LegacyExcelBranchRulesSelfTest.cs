using System;

namespace LegacyFibreCheckFinalEntryWriter
{
    internal static class LegacyExcelBranchRulesSelfTest
    {
        private static int passed;

        private static void Main()
        {
            Equal(true, LegacyExcelBranchRules.CanSelectTemplate(
                5, false, false,
                "Toone.FibreCheck.OriRecord.CurrencyItem.CurrencyItemRecordUI"),
                "neutral Excel route can coexist with the generic input UI");
            Equal(true, LegacyExcelBranchRules.CanSelectTemplate(
                5, false, false, ""),
                "neutral Excel route does not depend on legacy business allowlists");
            Equal(false, LegacyExcelBranchRules.CanSelectTemplate(
                2, true, true, "ExistingUI"),
                "older package preserves its input UI restriction");
            Equal(false, LegacyExcelBranchRules.CanSelectTemplate(
                2, false, true, ""),
                "older package preserves its project restriction");
            Equal(false, LegacyExcelBranchRules.CanSelectTemplate(
                2, true, false, ""),
                "older package preserves its template restriction");
            Equal(true, LegacyExcelBranchRules.CanSelectTemplate(
                2, true, true, ""),
                "older supported Excel package still works");
            Equal(
                "gap",
                LegacyExcelBranchRules.ResolveBranch(
                    2,
                    true,
                    true,
                    true,
                    true),
                "GAP precedes fixed template existence");
            Equal(
                "standard",
                LegacyExcelBranchRules.ResolveBranch(
                    2,
                    false,
                    true,
                    true,
                    false),
                "fixed templates suppress new Chinese-English and FILA");
            Equal(
                "clothing",
                LegacyExcelBranchRules.ResolveBranch(
                    2,
                    false,
                    false,
                    false,
                    true),
                "CLOTHING remains eligible after fixed templates");
            Equal(
                "gap",
                LegacyExcelBranchRules.ResolveBranch(
                    0,
                    true,
                    true,
                    true,
                    true),
                "GAP has first priority without a fixed template");
            Equal(
                "new_cnen",
                LegacyExcelBranchRules.ResolveBranch(
                    0,
                    false,
                    true,
                    false,
                    true),
                "new Chinese-English precedes CLOTHING without a fixed template");
            Equal(
                "clothing",
                LegacyExcelBranchRules.ResolveBranch(
                    0,
                    false,
                    false,
                    false,
                    true),
                "CLOTHING applies without earlier matches");
            Console.WriteLine(
                "Legacy Excel branch rules self-test: " + passed + " passed");
        }

        private static void Equal<T>(T expected, T actual, string name)
        {
            if (!object.Equals(expected, actual))
            {
                throw new InvalidOperationException(
                    name + ": expected " + expected + ", actual " + actual);
            }
            passed++;
        }
    }
}
