"""Exact, in-place paper record corrections on the existing operation queue."""

from copy import deepcopy

from app.execution.connector_records import record_fingerprint
from app.execution.errors import ExecutionApiError, conflict
from app.execution.models import ExecutionTaskSnapshotCache


GENERIC_UPDATE = "legacy_fibrecheck.check_record.generic_update@1"
UPDATE_OPERATION = "legacy_generic_check_record_update"
UPDATE_STAGES = ("authenticated", "permission_verified", "update_ready", "update_started",
                 "remote_state_verified", "update_verified", "completed")
HEADER_FIELDS = {"unit": "Unit", "sample_identity": "SampleDescription", "remark": "Remark",
                 "judge_basis": "JudgeBasis", "judgement": "TotalJudge"}
DETAIL_FIELDS = {"result_value": "RealValue", "standard_value": "StandardValue"}
PROJECTION_FIELDS = {"result_value": "CheckResult", "standard_value": "StandardValue",
                     "unit": "MeasureUnit", "sample_identity": "SampleIdentity", "remark": "Remark",
                     "judge_basis": "JudgeBasis", "judgement": "Judgement"}


def business_values(record):
    return {**{key: record["generic_record"].get(field) or "" for key, field in HEADER_FIELDS.items()},
            **{key: record["details"][0].get(field) or "" for key, field in DETAIL_FIELDS.items()}}


def update_summary(db, data):
    from app.execution.connector_original_records import project_binding

    number = data["inspection_number"].strip().upper()
    cached = db.get(ExecutionTaskSnapshotCache, number)
    snapshot = cached.snapshot if cached else None
    selected = next((p for p in (snapshot or {}).get("projects", [])
                     if p.get("project_key") == data["project_key"]), None)
    if selected is None:
        raise conflict("connector_task_project_missing", "请先读取任务信息并选择项目")
    project, _ = project_binding(db, number, selected)
    # The existing v1 correction Writer has this explicitly versioned scope.
    # Do not depend on a seeded workflow rule to validate its wire contract.
    if (project["check_item_no"] != "51.113K"
            or project["check_item_name"] != "纸、纸板和纸浆纤维鉴别分析"
            or project["check_method"] != "GB/T 4688-2020"):
        raise conflict("connector_record_update_unsupported", "v1 更正接口只支持纸纤维单行通用记录")
    record = next((r for r in (cached.check_records or {}).get("records", [])
                   if r.get("record_ref") == data["record_ref"]
                   and r.get("register", {}).get("CheckItemID") == project["check_item_id"]), None)
    if record is None:
        raise conflict("connector_record_missing", "请先读取要修改的原记录")
    if record_fingerprint(record) != data["expected_content_fingerprint"]:
        raise conflict("connector_record_changed", "记录已变化，请刷新后重新提交修改")
    if (record.get("record_kind") != "generic" or record.get("association_issues")
            or not isinstance(record.get("generic_record"), dict)
            or len(record.get("details", [])) != 1 or len(record.get("key_results", [])) != 1
            or record.get("list_data") or record.get("other_data")
            or record["details"][0].get("RealLocation") or record["details"][0].get("StandardLocation")):
        raise conflict("connector_record_update_unsupported", "当前更正支持纸纤维单行通用记录")
    header = record["generic_record"]
    if (header.get("TestMethod") != "GB/T 4688-2020" or header.get("AttachInfo")
            or header.get("StandardType") or record["details"][0].get("SeqNum") != 1):
        raise conflict("connector_record_update_unsupported", "该记录使用其他附件或结果版式，暂不支持直接更正")
    before = business_values(record)
    changes = {key: value.strip() for key, value in data["changes"].items()}
    after = {**before, **changes}
    if not after["result_value"]:
        raise ExecutionApiError(422, "connector_result_required", "实测结果不能为空")
    # Preserve fields not edited by the caller; the Writer compares the complete
    # observed record again at execution, including proof and report projection.
    return {
        "schema_version": 1, "operation_type": UPDATE_OPERATION,
        "profile": "generic_check_record_update_v1", "target_sample_number": number,
        "source_inspection_number": number, "task_project": project,
        "record_ref": data["record_ref"], "before_values": before, "after_values": after,
        "expected_content_fingerprint": data["expected_content_fingerprint"],
        "final_entry_package": {
            "schema_version": 3, "operation_type": "generic_item_record_update",
            "sample_number": number, "task_project": project,
            "before": deepcopy(record), "changes": changes,
        },
        "files": [], "execution_capability": {"available": True},
    }


def updated_record_matches(summary, record, *, projection=True):
    """Recognize only the submitted edits and the official DAL's own metadata.

    A partial projection can be repaired without saving the generic row again.
    Everything else, including original authorship and fields outside the edit,
    must still match the frozen observation.
    """
    before = summary["final_entry_package"]["before"]
    try:
        if (record["record_ref"] != summary["record_ref"] or record["record_kind"] != "generic"
                or record["association_issues"] or record["list_data"] or record["other_data"]
                or len(record["details"]) != 1 or business_values(record) != summary["after_values"]
                or record["content_fingerprint"] != record_fingerprint(record)):
            return False
        header = deepcopy(before["generic_record"])
        header.update({field: summary["after_values"][key] or None for key, field in HEADER_FIELDS.items()})
        # Oracle uses NULL for empty strings; compare the full rows with that normalization.
        normalized = lambda row: {key: None if value == "" else value for key, value in row.items()}
        if normalized(record["generic_record"]) != normalized(header):
            return False
        register = {key: value for key, value in record["register"].items()
                    if key not in {"LastUpdateTime", "ProofTime", "ProofUser", "SampleIdentity"}}
        old_register = {key: value for key, value in before["register"].items() if key in register}
        if normalized(register) != normalized(old_register) or set(register) != set(before["register"]) - {
                "LastUpdateTime", "ProofTime", "ProofUser", "SampleIdentity"}:
            return False
        if (record["register"].get("SampleIdentity") or "") != summary["after_values"]["sample_identity"]:
            return False
        detail = deepcopy(before["details"][0])
        detail.pop("ID", None)
        detail.update({field: summary["after_values"][key] or None for key, field in DETAIL_FIELDS.items()})
        if normalized({key: value for key, value in record["details"][0].items() if key != "ID"}) != normalized(detail):
            return False
        if projection:
            if len(record["key_results"]) != 1:
                return False
            key_result = record["key_results"][0]
            if any((key_result.get(field) or "") != summary["after_values"][key]
                   for key, field in PROJECTION_FIELDS.items()):
                return False
            if any(key_result.get(field) != before["key_results"][0].get(field)
                   for field in ("SampleNo", "CheckItemID", "OriginalRecordID")):
                return False
        else:
            # A missing or unchanged old projection is a known interrupted update.
            # Never overwrite a third set of report values written by someone else.
            if record["key_results"] and record["key_results"] != before["key_results"]:
                return False
        return True
    except (KeyError, TypeError, IndexError):
        return False


def update_receipt(operation, record):
    return {"schema_version": 1, "receipt_type": UPDATE_OPERATION, "operation_id": operation.id,
            "payload_checksum": operation.payload_checksum,
            "target_sample_number": operation.request_summary["target_sample_number"],
            "stages": list(UPDATE_STAGES), "reconciliation_required": False, "record": deepcopy(record),
            "changed": isinstance(record, dict) and record.get("content_fingerprint") !=
            operation.request_summary["expected_content_fingerprint"]}


def validate_update_receipt(operation, receipt):
    """Use measured rows, not a Writer boolean or a changed registration count."""
    if (not isinstance(receipt, dict) or receipt != update_receipt(operation, receipt.get("record"))
            or not updated_record_matches(operation.request_summary, receipt.get("record"))):
        raise ExecutionApiError(422, "external_receipt_invalid", "更正回执与原记录身份或提交字段不一致")
    return deepcopy(receipt)
