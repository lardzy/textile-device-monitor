using System;
using System.Collections;
using System.Collections.Generic;
using System.Data;
using System.Globalization;
using System.Linq;
using System.Text.RegularExpressions;
using LegacyFibreCheckRunner;

namespace LegacyFibreCheckFinalEntryWriter
{
    // The same six read-only projections as legacy_fibrecheck_probe. No raw IDs
    // or filesystem paths leave the Writer. Tests compare the query definitions.
    internal sealed class CheckRecordObservation
    {
        public string GenericId;
        public string RegisterId;
        public Dictionary<string, object> Record;
        internal static readonly Dictionary<string, string> Queries = new Dictionary<string, string>
        {
            { "currency_item_records", @"SELECT cir.""ID"", cir.""CheckRecordRegisterID"", cir.""SampleNo"", cir.""Grade"", cir.""JudgeBasis"", cir.""CheckItemID"", cir.""CheckItemName"", cir.""SampleDescription"", cir.""TestMethod"", cir.""StandardType"", cir.""Unit"", cir.""ReportCheckItemName"", cir.""AttachInfo"", cir.""Remark"", cir.""TotalJudge"", cir.""CheckUser"" FROM ""CurrencyItemRecordNew"" cir WHERE cir.""SampleNo"" = :sample_no ORDER BY cir.""ID""" },
            { "currency_item_record_details", @"SELECT d.""ID"", d.""CurrencyItemRecordNewID"", d.""StandardLocation"", d.""StandardValue"", d.""RealLocation"", d.""RealValue"", d.""SeqNum"" FROM ""CurrencyItemRecordNewDetail"" d JOIN ""CurrencyItemRecordNew"" cir ON cir.""ID"" = d.""CurrencyItemRecordNewID"" WHERE cir.""SampleNo"" = :sample_no ORDER BY d.""CurrencyItemRecordNewID"", d.""SeqNum"", d.""ID""" },
            { "original_key_data_list", @"SELECT okdl.""SampleNo"", okdl.""CheckItemID"", okdl.""ExcelTemplateName"", okdl.""KeyDataField"", okdl.""SeqNum"", okdl.""OriginalRecordID"", okdl.""Column1"", okdl.""Column2"", okdl.""Column3"", okdl.""Column4"", okdl.""Column5"", okdl.""Column6"", okdl.""Column7"", okdl.""Column8"", okdl.""Column9"", okdl.""Column10"", okdl.""Column11"", okdl.""Column12"", okdl.""Column13"", okdl.""Column14"", okdl.""Column15"", okdl.""Column16"", okdl.""Column17"", okdl.""Column18"", okdl.""Column19"", okdl.""Column20"" FROM ""OriginalKeyData_List"" okdl WHERE okdl.""SampleNo"" = :sample_no ORDER BY okdl.""CheckItemID"", okdl.""ExcelTemplateName"", okdl.""SeqNum"", okdl.""OriginalRecordID""" },
            { "original_key_data_other", @"SELECT okdo.""ID"", okdo.""SampleNo"", okdo.""ExcelTemplateName"", okdo.""OriginalRecordID"", okdo.""CheckItemNo"", okdo.""OriginalData"", okdo.""DataType"" FROM ""OriginalKeyData_Other"" okdo WHERE okdo.""SampleNo"" = :sample_no ORDER BY okdo.""CheckItemNo"", okdo.""ExcelTemplateName"", okdo.""OriginalRecordID"", okdo.""ID""" },
            { "check_record_register", @"SELECT crr.""ID"", crr.""SampleNo"", crr.""CheckItemID"", crr.""PositionID"", crr.""TemplateFilename"", crr.""OriginalDataFilename"", crr.""Level"", crr.""CreateUser"", crr.""CreateTime"", crr.""ReviewUser"", crr.""ReviewTime"", crr.""AuditUser"", crr.""AuditTime"", crr.""LastUpdateTime"", crr.""LastUpdateUser"", crr.""OriginalRecordID"", crr.""SampleIdentity"", crr.""CheckUser"", crr.""EquipmentNo"", crr.""ProofUser"", crr.""ProofTime"", crr.""CheckBasis"", crr.""OriginalPictureID"" FROM ""CheckRecordRegister"" crr WHERE crr.""SampleNo"" = :sample_no ORDER BY crr.""CreateTime"", crr.""ID""" },
            { "original_key_data", @"SELECT okd.""SampleNo"", okd.""CheckItemID"", okd.""ExcelTemplateName"", okd.""ConfigGroupKey"", okd.""OriginalRecordID"", okd.""SeqNum"", okd.""CheckItemName"", okd.""MeasureUnit"", okd.""SampleIdentity"", okd.""CheckMethod"", okd.""StandardValue"", okd.""CheckResult"", okd.""Judgement"", okd.""Remark"", okd.""JudgeBasis"", okd.""IsSubCheckItem"", okd.""CheckResult2"", okd.""IsShowAfterDetail"", okd.""IsShowBeforeDetail"", okd.""Grade"", okd.""TestLocation"", okd.""CheckResult3"", okd.""CheckMethod1"" FROM ""OriginalKeyData_CheckItem"" okd WHERE okd.""SampleNo"" = :sample_no ORDER BY okd.""SeqNum"", okd.""CheckItemID""" },
        };

        internal static CheckRecordObservation Read(string connection, string number, string recordRef)
        {
            var tables = new Dictionary<string, DataTable>();
            using (ILegacyDb db = new OdpNetDb(connection))
            {
                db.OpenReadOnly();
                try
                {
                    foreach (var entry in Queries)
                        tables[entry.Key] = db.Query(entry.Value, new List<DbParam> { new DbParam("sample_no", number) });
                }
                finally { db.RollbackAndClose(); }
            }
            var registers = tables["check_record_register"].Rows.Cast<DataRow>()
                .Where(r => "check-record:" + Redact.HashId(Convert.ToString(r["ID"])) == recordRef).ToList();
            if (registers.Count != 1) throw new PackageValidationException("update_record_not_unique");
            string registerId = Convert.ToString(registers[0]["ID"]);
            var generic = tables["currency_item_records"].Rows.Cast<DataRow>()
                .Where(r => Convert.ToString(r["CheckRecordRegisterID"]) == registerId).ToList();
            if (generic.Count != 1) throw new PackageValidationException("update_generic_record_not_unique");
            string id = Convert.ToString(generic[0]["ID"]);
            if (id != Convert.ToString(registers[0]["OriginalRecordID"])
                || Convert.ToString(generic[0]["CheckItemID"]) != Convert.ToString(registers[0]["CheckItemID"]))
                throw new PackageValidationException("update_record_association_changed");
            var record = new Dictionary<string, object> {
                { "record_ref", recordRef }, { "record_kind", "generic" },
                { "register", PublicRow(registers[0]) }, { "generic_record", PublicRow(generic[0]) },
                { "association_issues", new object[0] },
            };
            record["details"] = Linked(tables["currency_item_record_details"], "CurrencyItemRecordNewID", id);
            record["key_results"] = Linked(tables["original_key_data"], "OriginalRecordID", id);
            record["list_data"] = Linked(tables["original_key_data_list"], "OriginalRecordID", id);
            record["other_data"] = Linked(tables["original_key_data_other"], "OriginalRecordID", id);
            foreach (DataTable table in tables.Values) table.Dispose();
            return new CheckRecordObservation { GenericId = id, RegisterId = registerId, Record = record };
        }

        private static object[] Linked(DataTable table, string column, string id)
        {
            return table.Rows.Cast<DataRow>().Where(r => Convert.ToString(r[column]) == id)
                .Select(r => (object)PublicRow(r)).ToArray();
        }

        private static Dictionary<string, object> PublicRow(DataRow row)
        {
            var value = new Dictionary<string, object>();
            foreach (DataColumn column in row.Table.Columns)
                value[column.ColumnName] = PublicValue(column.ColumnName, row[column]);
            return value;
        }

        internal static object PublicValue(string column, object value)
        {
            if (value == null || value == DBNull.Value) return null;
            if (value is DateTime) {
                var date = (DateTime)value;
                return date.ToString(date.Ticks % TimeSpan.TicksPerSecond == 0
                    ? "yyyy-MM-ddTHH:mm:ss" : "yyyy-MM-ddTHH:mm:ss.ffffff", CultureInfo.InvariantCulture);
            }
            if (!(value is string)) return value;
            var text = (string)value;
            if (new[] { "AttachInfo", "TemplateFilename", "OriginalDataFilename" }.Contains(column))
            {
                var parts = text.Split(',').Select(p => p.Trim()).Where(p => p.Length > 0)
                    .Select(p => (object)new Dictionary<string, object> {
                        { "basename", p.Replace('\\', '/').Split('/').Last() }, { "path_hash", Redact.HashId(p) }
                    }).ToArray();
                return new Dictionary<string, object> { { "count", parts.Length }, { "items", parts } };
            }
            return Regex.IsMatch(column, @"(?:^ID$|ID$|USER\d*$)", RegexOptions.IgnoreCase)
                ? Redact.HashId(text) : text;
        }

        internal static bool Equal(object left, object right)
        {
            var lmap = left as IDictionary; var rmap = right as IDictionary;
            if (lmap != null && rmap != null)
            {
                var lk = lmap.Keys.Cast<string>().Where(k => k != "content_fingerprint").OrderBy(k => k).ToArray();
                var rk = rmap.Keys.Cast<string>().Where(k => k != "content_fingerprint").OrderBy(k => k).ToArray();
                return lk.SequenceEqual(rk) && lk.All(k => Equal(lmap[k], rmap[k]));
            }
            if (left is IEnumerable && right is IEnumerable && !(left is string) && !(right is string))
            {
                var la = ((IEnumerable)left).Cast<object>().ToArray();
                var ra = ((IEnumerable)right).Cast<object>().ToArray();
                return la.Length == ra.Length && la.Select((v, i) => Equal(v, ra[i])).All(v => v);
            }
            if (left == null || right == null) return left == right;
            if (left is string || right is string) return left is string && right is string && (string)left == (string)right;
            return Convert.ToDecimal(left, CultureInfo.InvariantCulture) == Convert.ToDecimal(right, CultureInfo.InvariantCulture);
        }
    }
}
