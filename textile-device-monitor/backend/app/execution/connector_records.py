"""Exact record reads over the existing read-only cache and Bridge queue."""

from copy import deepcopy
import hashlib
import json
import re

from app.execution.electron_microscopy import cached_task_snapshot, request_task_snapshot_refresh
from app.execution.errors import ExecutionApiError


RECORD_REF = re.compile(r"^check-record:sha256:[0-9a-f]{16}$")


def record_fingerprint(record):
    value = {key: item for key, item in record.items() if key != "content_fingerprint"}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_record_snapshot(value, inspection_number):
    if value is None:
        return None  # Older Bridges still serve ordinary task snapshots.
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("records"), list):
        raise ExecutionApiError(422, "check_records_invalid", "检验记录快照格式无效")
    seen = set()
    for record in value["records"]:
        if not isinstance(record, dict):
            raise ExecutionApiError(422, "check_records_invalid", "检验记录格式无效")
        ref = record.get("record_ref", "")
        register = record.get("register") or {}
        if (not isinstance(ref, str) or not RECORD_REF.fullmatch(ref) or ref in seen
                or not isinstance(register, dict) or register.get("SampleNo") != inspection_number
                or ref != "check-record:" + str(register.get("ID"))
                or record.get("content_fingerprint") != record_fingerprint(record)):
            raise ExecutionApiError(422, "check_records_invalid", "检验记录身份或内容摘要不一致")
        seen.add(ref)
    return deepcopy(value)


def read_records(db, data, *, single=False):
    from app.execution.connector_queries import QueryResult

    number = data["inspection_number"].strip().upper()
    if data.get("refresh"):
        request_task_snapshot_refresh(db, inspection_number=number, force=True, include_check_records=True)
    cached = cached_task_snapshot(db, inspection_number=number, include_check_records=True)
    snapshot = cached.get("snapshot")
    records = None
    lookup = "pending" if cached["refresh_status"] in {"queued", "running"} else "unavailable"
    if snapshot is not None:
        project = next((p for p in snapshot["projects"] if p["project_key"] == data["project_key"]), None)
        if project is not None and not project.get("check_item_id"):
            raise ExecutionApiError(409, "check_record_project_identity_missing", "任务项目缺少精确身份，请刷新任务信息")
        records = [record for record in cached["check_records"]["records"]
                   if project is not None and record["register"].get("CheckItemID") == project.get("check_item_id")]
        if single:
            records = [record for record in records if record["record_ref"] == data["record_ref"]]
        lookup = "found" if records else "not_found"
    result = {name: cached.get(name) for name in (
        "cache_state", "refresh_status", "revision", "fetched_at", "expires_at", "error_code",
    )}
    result.update(inspection_number=number, project_key=data["project_key"], lookup_state=lookup)
    if single:
        result.update(record_ref=data["record_ref"], record=deepcopy(records[0]) if records else None)
    else:
        result["records"] = deepcopy(records)
    return QueryResult(result, {"inspection_number": number,
                       "status_url": f"/api/execution/v1/task-snapshots/{number}/status"}
                       if cached["refresh_status"] in {"queued", "running"} else None)


def list_records(db, data):
    return read_records(db, data)


def get_record(db, data):
    return read_records(db, data, single=True)
