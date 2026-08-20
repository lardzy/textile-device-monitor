using System;

namespace LegacyFibreCheckFinalEntryWriter
{
    internal static class CustomerOrgBranchFlagsSelfTest
    {
        private static int passed;

        private static void Main()
        {
            DateTime start = new DateTime(2014, 9, 9);

            CustomerOrgBranchFlags.Resolution resolution =
                CustomerOrgBranchFlags.Resolve(new CustomerOrgBranchFlags.Row[0]);
            True(
                !resolution.Conflict && resolution.Values == null,
                "no rows resolves to the default flags");

            resolution = CustomerOrgBranchFlags.Resolve(new[]
            {
                Row(true, false, true, start),
            });
            True(
                !resolution.Conflict && resolution.Values != null
                    && resolution.Values.ChinaEnglish
                    && !resolution.Values.OnlyChinaEnglish
                    && resolution.Values.ShowAllTarget
                    && resolution.Values.ChinaEnglishStart == start,
                "a single row provides the flags");

            // NULL vs '0' already collapses to the same "off" value when rows
            // are built, so the classic duplicate pair (flags off, no start
            // date) is identical at this level.
            resolution = CustomerOrgBranchFlags.Resolve(new[]
            {
                Row(false, false, false, null),
                Row(false, false, false, null),
            });
            True(
                !resolution.Conflict && resolution.Values != null
                    && !resolution.Values.ChinaEnglish
                    && resolution.Values.ChinaEnglishStart == null,
                "duplicate rows with equal effective flags pass");

            resolution = CustomerOrgBranchFlags.Resolve(new[]
            {
                Row(true, true, false, start),
                Row(true, true, false, start),
                Row(true, true, false, start),
            });
            True(
                !resolution.Conflict && resolution.Values != null,
                "three identical rows pass");

            resolution = CustomerOrgBranchFlags.Resolve(new[]
            {
                Row(true, false, false, null),
                Row(false, false, false, null),
            });
            True(
                resolution.Conflict && resolution.Values == null,
                "disagreeing ChinaEnglish flags conflict");

            resolution = CustomerOrgBranchFlags.Resolve(new[]
            {
                Row(false, true, false, null),
                Row(false, false, false, null),
            });
            True(resolution.Conflict, "disagreeing OnlyChinaEnglish flags conflict");

            resolution = CustomerOrgBranchFlags.Resolve(new[]
            {
                Row(false, false, true, null),
                Row(false, false, false, null),
            });
            True(resolution.Conflict, "disagreeing ShowAllTarget flags conflict");

            resolution = CustomerOrgBranchFlags.Resolve(new[]
            {
                Row(false, false, false, start),
                Row(false, false, false, null),
            });
            True(resolution.Conflict, "a missing start date conflicts with a set one");

            resolution = CustomerOrgBranchFlags.Resolve(new[]
            {
                Row(false, false, false, start),
                Row(false, false, false, start.AddDays(1)),
            });
            True(resolution.Conflict, "different start dates conflict");

            resolution = CustomerOrgBranchFlags.Resolve(new[]
            {
                Row(false, false, false, null),
                Row(false, false, false, null),
                Row(true, false, false, null),
            });
            True(resolution.Conflict, "a later divergent row still conflicts");

            Console.WriteLine(
                "Customer org branch flags self-test: " + passed + " passed");
        }

        private static CustomerOrgBranchFlags.Row Row(
            bool chinaEnglish,
            bool onlyChinaEnglish,
            bool showAllTarget,
            DateTime? chinaEnglishStart)
        {
            return new CustomerOrgBranchFlags.Row
            {
                ChinaEnglish = chinaEnglish,
                OnlyChinaEnglish = onlyChinaEnglish,
                ShowAllTarget = showAllTarget,
                ChinaEnglishStart = chinaEnglishStart,
            };
        }

        private static void True(bool condition, string name)
        {
            if (!condition)
            {
                throw new InvalidOperationException(name);
            }
            passed++;
        }
    }
}
