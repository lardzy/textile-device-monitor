namespace LegacyFibreCheckFinalEntryWriter
{
    /// <summary>
    /// Pure Windows-session rule kept separate from process inspection so it
    /// can be exercised without Oracle, FibreCheck, or Excel.
    /// </summary>
    internal static class ExcelInteractiveSessionRules
    {
        internal static bool IsOperationAllowed(
            bool isExcelOperation,
            bool isOnlineOperation,
            bool userInteractive,
            int sessionId)
        {
            if (!isExcelOperation || !isOnlineOperation)
            {
                return true;
            }
            return userInteractive && sessionId > 0;
        }
    }
}
