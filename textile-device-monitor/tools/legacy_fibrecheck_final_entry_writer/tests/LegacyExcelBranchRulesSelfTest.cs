using System;

namespace LegacyFibreCheckFinalEntryWriter
{
    internal static class LegacyExcelBranchRulesSelfTest
    {
        private static int passed;

        private static void Main()
        {
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

        private static void Equal(string expected, string actual, string name)
        {
            if (!string.Equals(expected, actual, StringComparison.Ordinal))
            {
                throw new InvalidOperationException(
                    name + ": expected " + expected + ", actual " + actual);
            }
            passed++;
        }
    }
}
