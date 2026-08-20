# Read-only: inspect legacy Oracle state for inspection 260210750.
# Diagnoses the ghost register record (named like the uploaded original
# record) and the customer_org_not_unique preflight failure.
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

SAMPLE_NO = "260210750"
SCHEMA_TABLES = [
    "CheckRecordRegister",
    "OriginalKeyData_CheckItem",
    "CurrencyItemRecordNew",
    "Document",
    "StandardDocument",
    "CustomerOrg",
]


def emit(section, data):
    print(json.dumps({"section": section, "data": data}, ensure_ascii=True, default=str))


def rows_as_dicts(cursor):
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def q(cursor, sql, **params):
    cursor.execute(sql, params)
    return rows_as_dicts(cursor)


def truncate(value, limit=120):
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

        try:
            tasks = q(
                cursor,
                'SELECT ID, "ReportNo", "DelegateOrgName", "SampleReceiveTime" '
                'FROM "Task" WHERE "ReportNo"=:sn',
                sn=SAMPLE_NO,
            )
        except Exception as exc:
            tasks = "error: %s" % exc
        emit("task", tasks)

        org_names = []
        if isinstance(tasks, list):
            org_names = sorted({str(t.get("DelegateOrgName") or "") for t in tasks})
        for org in org_names:
            if not org:
                continue
            try:
                rows = q(
                    cursor,
                    'SELECT "FullName", "IsChinaEngType", "StartChinaEngTypeDate", '
                    '"IsOnlyChinaEngReportType", "IsShowAllTarget" '
                    'FROM "CustomerOrg" WHERE "FullName"=:org',
                    org=org,
                )
                emit("customer_org_rows", {"org": org, "count": len(rows), "rows": rows})
            except Exception as exc:
                emit("customer_org_rows", {"org": org, "error": str(exc)})

        task_ids = [t["ID"] for t in tasks] if isinstance(tasks, list) else []
        for task_id in task_ids:
            try:
                items = q(
                    cursor,
                    'SELECT tci.ID "TciID", tci."CheckItemID", tci."CheckItemNo", '
                    'tci."CheckItemName", tci."CheckMethod", tci."CheckCount", '
                    'tci."SampleIdentify" '
                    'FROM "Task_CheckItem" tci WHERE tci."TaskID"=:tid '
                    'ORDER BY tci."SeqNum"',
                    tid=task_id,
                )
            except Exception as exc:
                items = "error: %s" % exc
            emit("task_check_items", {"task_id": task_id, "rows": items})

        try:
            registers = q(
                cursor,
                'SELECT * FROM "CheckRecordRegister" WHERE "SampleNo"=:sn',
                sn=SAMPLE_NO,
            )
            registers = [
                {key: truncate(value) for key, value in row.items()}
                for row in registers
            ]
        except Exception as exc:
            registers = "error: %s" % exc
        emit("check_record_register", registers)

        doc_cols = schemas.get("Document") or []
        if isinstance(doc_cols, list):
            name_col = next(
                (c for c in doc_cols if c.upper() in ("FILENAME", "NAME", "DOCNAME")),
                None,
            )
            if name_col:
                try:
                    docs = q(
                        cursor,
                        'SELECT * FROM "Document" WHERE "%s" LIKE :pat' % name_col,
                        pat=SAMPLE_NO + "%",
                    )
                    docs = [
                        {key: truncate(value) for key, value in row.items()}
                        for row in docs
                    ]
                    emit("documents_by_name", {"column": name_col, "rows": docs})
                except Exception as exc:
                    emit("documents_by_name", {"column": name_col, "error": str(exc)})
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
