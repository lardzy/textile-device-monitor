using System;

namespace LegacyFibreCheckFinalEntryWriter
{
    internal static class ExcelInteractiveSessionRulesSelfTest
    {
        private static int passed;

        private static void Main()
        {
            Equal(
                false,
                ExcelInteractiveSessionRules.IsOperationAllowed(
                    true, true, false, 0),
                "SYSTEM-like non-interactive session zero is rejected");
            Equal(
                false,
                ExcelInteractiveSessionRules.IsOperationAllowed(
                    true, true, false, 1),
                "non-interactive nonzero session is rejected");
            Equal(
                false,
                ExcelInteractiveSessionRules.IsOperationAllowed(
                    true, true, true, 0),
                "interactive session zero is rejected");
            Equal(
                true,
                ExcelInteractiveSessionRules.IsOperationAllowed(
                    true, true, true, 1),
                "interactive nonzero session is accepted");
            Equal(
                true,
                ExcelInteractiveSessionRules.IsOperationAllowed(
                    false, true, false, 0),
                "generic online operation is unaffected");
            Equal(
                true,
                ExcelInteractiveSessionRules.IsOperationAllowed(
                    true, false, false, 0),
                "Excel offline validation is unaffected");
            Console.WriteLine(
                "Excel interactive session rules self-test: "
                + passed + " passed");
        }

        private static void Equal(bool expected, bool actual, string name)
        {
            if (expected != actual)
            {
                throw new InvalidOperationException(
                    name + ": expected " + expected + ", actual " + actual);
            }
            passed++;
        }
    }
}
