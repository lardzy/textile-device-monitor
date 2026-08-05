using System;

namespace LegacyFibreCheckFinalEntryWriter
{
    internal static class CollectedRegisterFieldAuthoritySelfTest
    {
        private static int passed;

        private static void Main()
        {
            Verify("", "", "", "", "staff-1", "empty package fields");
            Verify(
                "level-a", "part-a", "device-a", "basis-a", "staff-2",
                "non-empty package fields");
            Verify(null, null, null, null, "staff-3", "null package fields");
            Console.WriteLine(
                "Collected register field authority self-test: "
                + passed + " passed");
        }

        private static void Verify(
            string level,
            string sampleIdentity,
            string equipmentNo,
            string checkBasis,
            string staffId,
            string name)
        {
            AuthoritativeRegisterFieldValues actual =
                CollectedRegisterFieldAuthority.Resolve(
                    level, sampleIdentity, equipmentNo, checkBasis, staffId);
            Same(level, actual.Level, name + " level");
            Same(sampleIdentity, actual.SampleIdentity, name + " sample identity");
            Same(equipmentNo, actual.EquipmentNo, name + " equipment");
            Same(checkBasis, actual.CheckBasis, name + " check basis");
            Same(staffId, actual.CheckUser, name + " check user");
        }

        private static void Same(string expected, string actual, string name)
        {
            if (!string.Equals(expected, actual, StringComparison.Ordinal))
            {
                throw new InvalidOperationException(name + " mismatch");
            }
            passed++;
        }
    }
}
