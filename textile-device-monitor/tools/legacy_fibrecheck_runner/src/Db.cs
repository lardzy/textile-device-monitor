using System;
using System.Collections.Generic;
using System.Data;
using Oracle.ManagedDataAccess.Client;

namespace LegacyFibreCheckRunner
{
    /// <summary>命名参数，避免把 ODP.NET 类型泄漏到流程层，便于 Fake 测试。</summary>
    internal sealed class DbParam
    {
        public string Name { get; private set; }
        public object Value { get; private set; }

        public DbParam(string name, object value)
        {
            Name = name;
            Value = value;
        }
    }

    /// <summary>
    /// 只读数据库访问抽象。硬性边界：实现内只允许 SET TRANSACTION READ ONLY、
    /// 参数化 SELECT 和 ROLLBACK，不得出现任何 DML/DDL/存储过程调用。
    /// </summary>
    internal interface ILegacyDb : IDisposable
    {
        void OpenReadOnly();
        void RollbackAndClose();
        DataTable Query(string sql, IList<DbParam> parameters);
        object Scalar(string sql, IList<DbParam> parameters);
    }

    internal sealed class OdpNetDb : ILegacyDb
    {
        public const string ReadOnlyTransactionSql = "SET TRANSACTION READ ONLY";

        private readonly string _connectionString;
        private OracleConnection _connection;

        public OdpNetDb(string connectionString)
        {
            _connectionString = connectionString;
        }

        public void OpenReadOnly()
        {
            _connection = new OracleConnection(_connectionString);
            _connection.Open();
            using (OracleCommand command = _connection.CreateCommand())
            {
                command.CommandText = ReadOnlyTransactionSql;
                command.ExecuteNonQuery();
            }
        }

        public DataTable Query(string sql, IList<DbParam> parameters)
        {
            using (OracleCommand command = BuildCommand(sql, parameters))
            using (OracleDataAdapter adapter = new OracleDataAdapter(command))
            {
                var table = new DataTable();
                adapter.Fill(table);
                return table;
            }
        }

        public object Scalar(string sql, IList<DbParam> parameters)
        {
            using (OracleCommand command = BuildCommand(sql, parameters))
            {
                return command.ExecuteScalar();
            }
        }

        public void RollbackAndClose()
        {
            if (_connection == null)
            {
                return;
            }
            try
            {
                if (_connection.State == ConnectionState.Open)
                {
                    using (OracleCommand command = _connection.CreateCommand())
                    {
                        command.CommandText = "ROLLBACK";
                        command.ExecuteNonQuery();
                    }
                }
            }
            finally
            {
                Dispose();
            }
        }

        public void Dispose()
        {
            if (_connection != null)
            {
                _connection.Dispose();
                _connection = null;
            }
        }

        private OracleCommand BuildCommand(string sql, IList<DbParam> parameters)
        {
            OracleCommand command = _connection.CreateCommand();
            command.BindByName = true;
            command.CommandText = sql;
            if (parameters != null)
            {
                foreach (DbParam parameter in parameters)
                {
                    command.Parameters.Add(parameter.Name, parameter.Value);
                }
            }
            return command;
        }
    }
}
