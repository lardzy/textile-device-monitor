namespace LegacyFibreCheckFinalEntryWriter
{
    internal sealed class AuthoritativeRegisterFieldValues
    {
        internal string Level;
        internal string SampleIdentity;
        internal string EquipmentNo;
        internal string CheckBasis;
        internal string CheckUser;
    }

    internal static class CollectedRegisterFieldAuthority
    {
        internal static AuthoritativeRegisterFieldValues Resolve(
            string level,
            string sampleIdentity,
            string equipmentNo,
            string checkBasis,
            string authenticatedStaffId)
        {
            return new AuthoritativeRegisterFieldValues
            {
                Level = level,
                SampleIdentity = sampleIdentity,
                EquipmentNo = equipmentNo,
                CheckBasis = checkBasis,
                CheckUser = authenticatedStaffId,
            };
        }
    }
}
