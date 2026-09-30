"""Generic grouped selection and sequential same-version Runs, without another queue."""
from copy import deepcopy

from app.execution.errors import ExecutionApiError, conflict
from app.execution.models import ExecutionRun, ExecutionNodeRun, ExecutionUser
from app.execution.v2.canonical import canonical_sha256


def group_form_schema(input_data, config):
    schema = {
        "type": "object", "required": ["groups"], "additionalProperties": False,
        "properties": {"groups": {"type": "array", "minItems": 1,
            "maxItems": len(input_data["groups"]), "items": {
                "type": "object", "required": ["id", "selected_ids"], "additionalProperties": False,
                "properties": {"id": {"type": "string"}, "selected_ids": {
                    "type": "array", "items": {"type": "string"}, "uniqueItems": True,
                    "minItems": 1 if config.get("require_all_groups", True) else 0, "maxItems": 100}},
            }}},
    }
    if "form_schema" in input_data:
        from app.execution.release_v2 import runtime_form_schema_issues

        shared = input_data["form_schema"]
        issues = runtime_form_schema_issues(shared)
        if issues:
            raise ExecutionApiError(422, "human_form_schema_invalid", "输入表单结构无效", details={"issues": issues})
        schema["properties"]["form_data"] = deepcopy(shared)
        schema["required"].append("form_data")
    return schema


def normalize_groups(input_data, data, config, validate_candidate):
    offered = {group["id"]: group for group in input_data["groups"]}
    items = {item["id"]: item for item in input_data["items"]}
    if len(offered) != len(input_data["groups"]) or len(items) != len(input_data["items"]):
        raise ExecutionApiError(422, "group_candidates_ambiguous", "分组或候选项标识重复")
    selected, seen, used, answered = [], set(), set(), set()
    for value in data.get("groups", []):
        key = value["id"]
        if key not in offered or key in answered:
            raise conflict("selection_group_invalid", "所选分组已变化或重复")
        answered.add(key)
        group = offered[key]
        ids = value["selected_ids"]
        if not ids and not config.get("require_all_groups", True):
            continue
        if len(set(ids)) != len(ids) or len(ids) not in group["allowed_selected_counts"]:
            raise ExecutionApiError(422, "group_selection_count_invalid", f"{group['label']} 的选择数量不符合模板配置")
        if any(identifier not in items for identifier in ids):
            raise conflict("group_item_not_offered", "所选项目不在当前候选中")
        if not config.get("allow_item_reuse", False) and used.intersection(ids):
            raise ExecutionApiError(422, "group_item_reused", "同一候选不能同时分配给多个分组")
        for identifier in ids:
            validate_candidate(items[identifier])
        selected.append({**deepcopy(group), "selected_ids": ids, "selected_items": [deepcopy(items[i]) for i in ids]})
        seen.add(key)
        used.update(ids)
    if not selected:
        raise ExecutionApiError(422, "group_selection_empty", "至少选择一个分组")
    if config.get("require_all_groups", True) and seen != set(offered):
        raise ExecutionApiError(422, "group_selection_incomplete", "请为每个分组选择候选项")
    result = {"groups": selected, "unselected_group_ids": [key for key in offered if key not in seen]}
    if "form_schema" in input_data:
        result["form_data"] = deepcopy(data["form_data"])
    return {**result, "selection_digest": canonical_sha256(result)}


def execute_batch(context):
    from app.execution.engine import _lock_run_and_node, create_run
    from app.execution.v2.native_handlers import NodeExecutionResult

    db = context.db
    parent, node = _lock_run_and_node(db, context.node_run.id)
    if node.status != "running" or node.lease_token != context.lease_token:
        raise conflict("node_lease_lost", "节点租约已失效")
    if parent.parent_run_id:
        raise ExecutionApiError(422, "nested_batch_unsupported", "分组运行不能再创建分组；请检查入口分支")
    items = context.input_data["items"]
    node.input_data = deepcopy(context.input_data)
    if len({item["key"] for item in items}) != len(items):
        raise ExecutionApiError(422, "batch_key_duplicate", "分组标识必须唯一")
    children = {child.batch_context["key"]: child for child in db.query(ExecutionRun).filter_by(parent_run_id=parent.id)
                if (child.batch_context or {}).get("node_id") == node.node_id}
    results = []
    for index, item in enumerate(items):
        child = children.get(item["key"])
        if child is None:
            actor = db.get(ExecutionUser, parent.created_by_id)
            input_data = {**parent.input_data, **deepcopy(item["inputs"])}
            child, _ = create_run(db, workflow=parent.workflow, actor=actor,
                inspection_number=parent.inspection_number, input_data=input_data,
                target_sample_number=parent.input_data.get("target_sample_number"),
                global_data=deepcopy(parent.global_data),
                idempotency_key="batch:" + canonical_sha256([parent.id, node.node_id, item["key"]]),
                inherited_run=parent,
                batch_context={"node_id": node.node_id, "key": item["key"], "label": item["label"], "index": index})
        result = {"key": item["key"], "label": item["label"], "run_id": child.id,
                  "status": child.status, "output": deepcopy(child.output_data or {})}
        results.append(result)
        if child.status == "failed":
            # No automatic retries: the existing node retry action keeps each
            # operation's identity, receipt and unknown-outcome handling intact.
            node.output_data = {"groups": results, "count": len(items)}
            raise ExecutionApiError(409, "batch_group_failed", f"分组“{item['label']}”失败，请查看该组后继续",
                                    details={"run_id": child.id, "key": item["key"]})
        if child.status not in {"completed", "cancelled"}:
            return NodeExecutionResult(output={"groups": results, "count": len(items)}, retry_after_seconds=3)
    return {"groups": results, "count": len(items)}


def child_summaries(db, run):
    children = db.query(ExecutionRun).filter_by(parent_run_id=run.id).order_by(ExecutionRun.created_at, ExecutionRun.id).all()
    summaries = [{"id": child.id, "context": child.batch_context, "status": child.status,
             "output_data": child.output_data or {}, "error_message": child.error_message,
             "failed_node_ids": [n.node_id for n in child.node_runs if n.status == "failed"]} for child in children]
    created = {(item["context"]["node_id"], item["context"]["key"]) for item in summaries}
    for node in db.query(ExecutionNodeRun).filter_by(run_id=run.id, node_type="flow.batch"):
        for index, item in enumerate((node.input_data or {}).get("items", [])):
            if (node.node_id, item["key"]) not in created:
                summaries.append({"id": None, "context": {"node_id": node.node_id, "key": item["key"],
                    "label": item["label"], "index": index},
                    "status": "cancelled" if run.status in {"cancelled", "cancel_pending"} else "pending",
                    "output_data": {}, "error_message": None, "failed_node_ids": []})
    return sorted(summaries, key=lambda item: (item["context"]["node_id"], item["context"]["index"]))


def settle_cancelled_batches(db):
    """Worker maintenance, avoiding child -> parent lock inversion on callbacks."""
    from sqlalchemy.orm import aliased
    from app.execution.engine import _settle_cancel_pending_run, RUN_TERMINAL_STATUSES
    child = aliased(ExecutionRun)
    children = db.query(child.id).filter(child.parent_run_id == ExecutionRun.id)
    parents = db.query(ExecutionRun).filter(
        ExecutionRun.status == "cancel_pending", ExecutionRun.parent_run_id.is_(None),
        children.exists(), ~children.filter(child.status.notin_(RUN_TERMINAL_STATUSES)).exists(),
    ).order_by(ExecutionRun.id).with_for_update(skip_locked=True).limit(20).all()
    for parent in parents:
        _settle_cancel_pending_run(db, run=parent)
    return bool(parents)
