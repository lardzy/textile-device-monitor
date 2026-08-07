# Read-only: evidence counts for 260111037 microscopy final-entry reconciliation.
# Expected after failed attempt 68046d8d (excel_collection_started):
# register=1 file_reference=1 key_linked=1 proofed=1 (unchanged).
import base64
import dataclasses
import sys
from pathlib import Path

PROBE_DIR = Path(r"C:\Users\lishuyang\Downloads\textile-device-monitor-cdde9ec\textile-device-monitor\tools\legacy_fibrecheck_probe")
sys.path.insert(0, str(PROBE_DIR))

import probe  # noqa: E402

FIBRECHECK_DIR = Path(r"C:\Users\lishuyang\Downloads\textile-device-monitor\.tmp\FibreCheck")
ORACLE_CLIENT = Path(r"C:\Users\lishuyang\Downloads\textile-device-monitor\.tmp\oracle-ic\instantclient_19_31")

SAMPLE_NO = "260111037"
ITEM_NO = "5103.5"
ITEM_NAME = base64.b64decode("57qk57u05b6u6KeC5b2i6LKM").decode("utf-8")

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
                "file_reference_count": (
                    'SELECT COUNT(*) FROM "CheckRecordRegister" '
                    'WHERE "SampleNo"=:sample_no AND "CheckItemID"=:item_id '
                    'AND "OriginalDataFilename" IS NOT NULL'
                ),
                "proofed_count": (
                    'SELECT COUNT(*) FROM "CheckRecordRegister" '
                    'WHERE "SampleNo"=:sample_no AND "CheckItemID"=:item_id '
                    'AND "ProofTime" IS NOT NULL AND "ProofUser" IS NOT NULL'
                ),
                "key_linked_count": (
                    'SELECT COUNT(DISTINCT crr.ID) FROM "CheckRecordRegister" crr '
                    'JOIN "OriginalKeyData_CheckItem" okd '
                    'ON okd."OriginalRecordID"=crr.ID '
                    'AND okd."CheckItemID"=crr."CheckItemID" '
                    'AND okd."SampleNo"=crr."SampleNo" '
                    'WHERE crr."SampleNo"=:sample_no AND crr."CheckItemID"=:item_id'
                ),
            }
            for label, sql in queries.items():
                cursor.execute(sql, scope)
                print(label, "=", cursor.fetchone()[0])
            cursor.execute(
                'SELECT ID, "OriginalDataFilename" FROM "CheckRecordRegister" '
                'WHERE "SampleNo"=:sample_no AND "CheckItemID"=:item_id ORDER BY ID',
                scope,
            )
            for row in cursor.fetchall():
                print("register_row:", row[0], "|", row[1])
        finally:
            connection.rollback()
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
