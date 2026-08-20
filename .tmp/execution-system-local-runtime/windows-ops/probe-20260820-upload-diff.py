# Read-only: compare flow-uploaded vs manually-uploaded original-record rows,
# and inspect the current register state of 26A045793 after the ambiguous
# excel_collection failure (reconciliation_required).
# Usage: run on the Windows VM with the probe venv python; no arguments.
import dataclasses
import json
import sys
from pathlib import Path

PROBE_DIR = Path(r"C:\Users\lishuyang\Downloads\textile-device-monitor-cdde9ec\textile-device-monitor\tools\legacy_fibrecheck_probe")
sys.path.insert(0, str(PROBE_DIR))

import probe  # noqa: E402

FIBRECHECK_DIR = Path(r"C:\Users\lishuyang\Downloads\textile-device-monitor\.tmp\FibreCheck")
ORACLE_CLIENT = Path(r"C:\Users\lishuyang\Downloads\textile-device-monitor\.tmp\oracle-ic\instantclient_19_31")

SAMPLES = ["260210750", "260111037", "26A045793"]
SCHEMA_TABLES = [
    "OriginalDataPictureFile",
    "CheckRecordRegister",
    "OriginalKeyData_CheckItem",
    "CurrencyItemRecordNew",
]


def emit(section, data):
    print(json.dumps({"section": section, "data": data}, ensure_ascii=True, default=str))


def rows_as_dicts(cursor):
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def q(cursor, sql, **params):
    cursor.execute(sql, params)
    return rows_as_dicts(cursor)


def truncate(value, limit=100):
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "...<truncated>"


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

        schemas = {}
        for table in SCHEMA_TABLES:
            try:
                rows = q(
                    cursor,
                    "SELECT column_name FROM all_tab_columns "
                    "WHERE table_name=:t ORDER BY column_id",
                    t=table,
                )
                schemas[table] = [r["COLUMN_NAME"] for r in rows]
            except Exception as exc:  # keep going; emit the error as data
                schemas[table] = "error: %s" % exc
        emit("schemas", schemas)

        for sample in SAMPLES:
            try:
                rows = q(
                    cursor,
                    'SELECT * FROM "OriginalDataPictureFile" WHERE "SampleNo"=:sn '
                    'ORDER BY "ID"',
                    sn=sample,
                )
                rows = [
                    {key: truncate(value) for key, value in row.items()}
                    for row in rows
                ]
            except Exception as exc:
                rows = "error: %s" % exc
            emit("picture_file:%s" % sample, rows)

        for sample in SAMPLES:
            try:
                rows = q(
                    cursor,
                    'SELECT * FROM "CheckRecordRegister" WHERE "SampleNo"=:sn '
                    'ORDER BY "ID"',
                    sn=sample,
                )
                rows = [
                    {key: truncate(value) for key, value in row.items()}
                    for row in rows
                ]
            except Exception as exc:
                rows = "error: %s" % exc
            emit("register:%s" % sample, rows)

        # 26A045793: task + item context and any rows touched today.
        try:
            tasks = q(
                cursor,
                'SELECT ID, "ReportNo", "DelegateOrgName", "SampleReceiveTime" '
                'FROM "Task" WHERE "ReportNo"=:sn',
                sn="26A045793",
            )
        except Exception as exc:
            tasks = "error: %s" % exc
        emit("task:26A045793", tasks)

        if isinstance(tasks, list):
            for task in tasks:
                try:
                    items = q(
                        cursor,
                        'SELECT tci.ID "TciID", tci."CheckItemID", tci."CheckItemNo", '
                        'tci."CheckItemName", tci."CheckMethod", tci."CheckCount", '
                        'tci."SampleIdentify" '
                        'FROM "Task_CheckItem" tci WHERE tci."TaskID"=:tid '
                        'ORDER BY tci."SeqNum"',
                        tid=task["ID"],
                    )
                except Exception as exc:
                    items = "error: %s" % exc
                emit("task_check_items:26A045793", {"task_id": task["ID"], "rows": items})

        for table in ("OriginalKeyData_CheckItem", "CurrencyItemRecordNew"):
            cols = schemas.get(table)
            if not isinstance(cols, list):
                continue
            sample_col = next(
                (c for c in cols if c.upper() in ("SAMPLENO", "SAMPLE_NO")),
                None,
            )
            if not sample_col:
                emit("table_skip", {"table": table, "reason": "no sample column", "cols": cols})
                continue
            try:
                rows = q(
                    cursor,
                    'SELECT * FROM "%s" WHERE "%s"=:sn ORDER BY 1' % (table, sample_col),
                    sn="26A045793",
                )
                rows = [
                    {key: truncate(value) for key, value in row.items()}
                    for row in rows
                ]
            except Exception as exc:
                rows = "error: %s" % exc
            emit("%s:26A045793" % table, rows)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
