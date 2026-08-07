# Read-only: evidence counts for paper generic final-entry reconciliation.
# Usage: probe-counts-paper.py <sample_no> <item_no> <item_name_b64_utf8>
import base64
import dataclasses
import sys
from pathlib import Path

PROBE_DIR = Path(r"C:\Users\lishuyang\Downloads\textile-device-monitor-cdde9ec\textile-device-monitor\tools\legacy_fibrecheck_probe")
sys.path.insert(0, str(PROBE_DIR))

import probe  # noqa: E402

FIBRECHECK_DIR = Path(r"C:\Users\lishuyang\Downloads\textile-device-monitor\.tmp\FibreCheck")
ORACLE_CLIENT = Path(r"C:\Users\lishuyang\Downloads\textile-device-monitor\.tmp\oracle-ic\instantclient_19_31")

SAMPLE_NO = sys.argv[1]
ITEM_NO = sys.argv[2]
ITEM_NAME = base64.b64decode(sys.argv[3]).decode("utf-8")

PROJECT_SQL = (
    'SELECT ci.ID "CheckItemID" '
    'FROM "Task" t '
    'JOIN "Task_CheckItem" tci ON t.ID=tci."TaskID" '
    'JOIN "CheckItem" ci ON tci."CheckItemID"=ci.ID '
    'WHERE t."ReportNo"=:sample_no '
    'AND tci."CheckItemNo"=:item_no AND tci."CheckItemName"=:item_name '
    'AND ci."No"=:item_no AND ci."ItemName"=:item_name'
)


def main() -> int:
    profile = probe.load_credential_profile(
        FIBRECHECK_DIR, "WebService.dll.config:PanYuJianWu"
    )
    profile = dataclasses.replace(
        profile,
        data_source=probe.validate_data_source_override("192.168.105.106/orcl"),
    )
    connection = probe.connect_oracle(profile, ORACLE_CLIENT)
    try:
        cursor = connection.cursor()
        cursor.execute("SET TRANSACTION READ ONLY")
        try:
            cursor.execute(
                PROJECT_SQL,
                sample_no=SAMPLE_NO, item_no=ITEM_NO, item_name=ITEM_NAME,
            )
            rows = cursor.fetchall()
            if len(rows) != 1:
                print("project_rows=", len(rows))
                return 1
            item_id = rows[0][0]
            print("check_item_id=", item_id)
            scope = {"sample_no": SAMPLE_NO, "item_id": item_id}
            queries = {
                "register_count": (
                    'SELECT COUNT(*) FROM "CheckRecordRegister" '
                    'WHERE "SampleNo"=:sample_no AND "CheckItemID"=:item_id'
                ),
                "generic_record_count": (
                    'SELECT COUNT(*) FROM "CurrencyItemRecordNew" '
                    'WHERE "SampleNo"=:sample_no AND "CheckItemID"=:item_id'
                ),
                "generic_key_linked_count": (
                    'SELECT COUNT(DISTINCT cir.ID) FROM "CurrencyItemRecordNew" cir '
                    'JOIN "OriginalKeyData_CheckItem" okd '
                    'ON okd."OriginalRecordID"=cir.ID '
                    'AND okd."CheckItemID"=cir."CheckItemID" '
                    'AND okd."SampleNo"=cir."SampleNo" '
                    'WHERE cir."SampleNo"=:sample_no AND cir."CheckItemID"=:item_id'
                ),
                "proofed_count": (
                    'SELECT COUNT(*) FROM "CheckRecordRegister" '
                    'WHERE "SampleNo"=:sample_no AND "CheckItemID"=:item_id '
                    'AND "ProofTime" IS NOT NULL AND "ProofUser" IS NOT NULL'
                ),
            }
            for label, sql in queries.items():
                cursor.execute(sql, scope)
                print(label, "=", cursor.fetchone()[0])
            cursor.execute(
                'SELECT cir.ID, cir."Unit", cir."TestMethod", cir."JudgeBasis", '
                'cir."CheckRecordRegisterID" FROM "CurrencyItemRecordNew" cir '
                'WHERE cir."SampleNo"=:sample_no AND cir."CheckItemID"=:item_id '
                'ORDER BY cir.ID',
                scope,
            )
            for row in cursor.fetchall():
                unit = row[1].encode("utf-8").hex() if row[1] else repr(row[1])
                print("generic_row:", row[0], "| unit_hex:", unit,
                      "| method:", row[2], "| basis:", row[3], "| register:", row[4])
            cursor.execute(
                'SELECT ID, "ProofTime", "ProofUser", "CreateTime" '
                'FROM "CheckRecordRegister" '
                'WHERE "SampleNo"=:sample_no AND "CheckItemID"=:item_id '
                'ORDER BY "CreateTime"',
                scope,
            )
            for row in cursor.fetchall():
                print("register_row:", row[0], "| proof_time:", row[1],
                      "| proof_user:", row[2], "| created:", row[3])
            cursor.execute(
                'SELECT okd."OriginalRecordID", okd."SampleIdentity", okd."SeqNum", '
                'okd."CheckItemName" FROM "OriginalKeyData_CheckItem" okd '
                'WHERE okd."SampleNo"=:sample_no AND okd."CheckItemID"=:item_id '
                'ORDER BY okd."OriginalRecordID", okd."SeqNum"',
                scope,
            )
            for row in cursor.fetchall():
                print("projection_row:", row[0], "|", row[1], "|", row[2], "|", row[3])
        finally:
            connection.rollback()
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
