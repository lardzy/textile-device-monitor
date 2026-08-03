using System;
using System.Data;
using Oracle.ManagedDataAccess.Client;

namespace LegacyFibreCheckFinalEntryWriter
{
    /// <summary>
    /// Cooperative cross-process lock for one Task_CheckItem. It is acquired only after
    /// the explicit side-effect permit and is always rolled back; no application row is
    /// changed. Final preflight runs while the lock is held, closing the two-writer race.
    /// </summary>
    internal sealed class ProjectWriteLock : IDisposable
    {
        private OracleConnection connection;
        private OracleTransaction transaction;

        private ProjectWriteLock(OracleConnection value, OracleTransaction activeTransaction)
        {
            connection = value;
            transaction = activeTransaction;
        }

        internal static ProjectWriteLock Acquire(
            string connectionString, string taskId, string checkItemId)
        {
            OracleConnection candidate = null;
            OracleTransaction candidateTransaction = null;
            try
            {
                candidate = new OracleConnection(connectionString);
                candidate.Open();
                candidateTransaction = candidate.BeginTransaction(IsolationLevel.ReadCommitted);
                using (OracleCommand command = candidate.CreateCommand())
                {
                    command.Transaction = candidateTransaction;
                    command.BindByName = true;
                    command.CommandTimeout = 15;
                    command.CommandText =
                        "SELECT tci.\"TaskID\" FROM \"Task_CheckItem\" tci " +
                        "WHERE tci.\"TaskID\"=:task_id AND tci.\"CheckItemID\"=:item_id " +
                        "FOR UPDATE WAIT 10";
                    command.Parameters.Add("task_id", OracleDbType.Varchar2).Value = taskId;
                    command.Parameters.Add("item_id", OracleDbType.Varchar2).Value = checkItemId;
                    object selected = command.ExecuteScalar();
                    if (selected == null || selected == DBNull.Value
                        || !string.Equals(Convert.ToString(selected), taskId,
                            StringComparison.Ordinal))
                    {
                        throw new InvalidOperationException("project_lock_target_missing");
                    }
                }
                var result = new ProjectWriteLock(candidate, candidateTransaction);
                candidate = null;
                candidateTransaction = null;
                return result;
            }
            catch
            {
                try { if (candidateTransaction != null) candidateTransaction.Rollback(); }
                catch { }
                try { if (candidateTransaction != null) candidateTransaction.Dispose(); }
                catch { }
                try { if (candidate != null) candidate.Dispose(); }
                catch { }
                throw new WriterFailureException(
                    "project_write_lock_unavailable", Program.ExitRemoteConflict, false);
            }
        }

        public void Dispose()
        {
            try { if (transaction != null) transaction.Rollback(); }
            catch { }
            try { if (transaction != null) transaction.Dispose(); }
            catch { }
            try { if (connection != null) connection.Dispose(); }
            catch { }
            transaction = null;
            connection = null;
        }
    }
}
