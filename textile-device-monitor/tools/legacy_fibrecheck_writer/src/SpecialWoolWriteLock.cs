using System;
using System.Collections.Generic;
using System.Data;
using Oracle.ManagedDataAccess.Client;

namespace LegacyFibreCheckWriter
{
    /// <summary>
    /// 以 Task_CheckItem 行作为同一样品项目的协作锁。锁连接只执行 SELECT FOR
    /// UPDATE，最终固定 ROLLBACK，不修改任务行；官方 DAL 在另一连接完成业务保存。
    /// </summary>
    internal sealed class SpecialWoolWriteLock : IDisposable
    {
        private OracleConnection connection;
        private OracleTransaction transaction;

        private SpecialWoolWriteLock(
            OracleConnection value,
            OracleTransaction activeTransaction)
        {
            connection = value;
            transaction = activeTransaction;
        }

        internal static SpecialWoolWriteLock Acquire(
            string connectionString,
            string taskCheckItemId,
            string checkItemId)
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
                        "SELECT tci.\"ID\" FROM \"Task_CheckItem\" tci " +
                        "WHERE tci.\"ID\"=:task_check_item_id " +
                        "AND tci.\"CheckItemID\"=:check_item_id FOR UPDATE WAIT 10";
                    command.Parameters.Add(
                        "task_check_item_id",
                        OracleDbType.Varchar2).Value = taskCheckItemId;
                    command.Parameters.Add(
                        "check_item_id",
                        OracleDbType.Varchar2).Value = checkItemId;
                    object selected = command.ExecuteScalar();
                    if (selected == null || selected == DBNull.Value
                        || !string.Equals(
                            Convert.ToString(selected),
                            taskCheckItemId,
                            StringComparison.Ordinal))
                    {
                        throw new InvalidOperationException("project_lock_target_missing");
                    }
                }
                var result = new SpecialWoolWriteLock(candidate, candidateTransaction);
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
                throw new InvalidOperationException("project_write_lock_unavailable");
            }
        }

        internal DataTable Query(string sql, IList<LockParam> parameters)
        {
            using (OracleCommand command = BuildCommand(sql, parameters))
            using (OracleDataAdapter adapter = new OracleDataAdapter(command))
            {
                var table = new DataTable();
                adapter.Fill(table);
                return table;
            }
        }

        internal object Scalar(string sql, IList<LockParam> parameters)
        {
            using (OracleCommand command = BuildCommand(sql, parameters))
            {
                return command.ExecuteScalar();
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

        private OracleCommand BuildCommand(string sql, IList<LockParam> parameters)
        {
            OracleCommand command = connection.CreateCommand();
            command.Transaction = transaction;
            command.BindByName = true;
            command.CommandTimeout = 30;
            command.CommandText = sql;
            if (parameters != null)
            {
                foreach (LockParam parameter in parameters)
                {
                    command.Parameters.Add(
                        parameter.Name,
                        OracleDbType.Varchar2).Value = parameter.Value;
                }
            }
            return command;
        }
    }

    internal sealed class LockParam
    {
        internal string Name;
        internal string Value;

        internal LockParam(string name, string value)
        {
            Name = name;
            Value = value;
        }
    }
}
