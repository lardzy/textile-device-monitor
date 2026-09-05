"""Trusted native Execution v2 handlers.

The functions in this module are installed by application deployment.  A
Workflow Release can select only an exact contract already present in the
registry; it cannot name or import any callable from JSON.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Callable

from sqlalchemy import or_

from app.execution.errors import ExecutionApiError
from app.execution.models import (
    ExecutionEdgeRun,
    ExecutionFileIndexEntry,
    ExecutionNodeRun,
    ExecutionStorageRoot,
)
from app.execution.v2.canonical import canonical_sha256


@dataclass(frozen=True)
class NodeExecutionResult:
    """Internal control envelope returned only by trusted native handlers."""

    output: dict[str, Any]
    globals_patch: dict[str, Any] | None = None
    globals_conflict: str | None = None
    selected_edge_ids: tuple[str, ...] | None = None
    suspension: dict[str, Any] | None = None


NativeHandler = Callable[[Any], dict[str, Any] | NodeExecutionResult]

_JSON_SAFE_INTEGER_MAX = (1 << 53) - 1


def _portable_json_value(value: Any) -> Any:
    """Keep native v2 payloads lossless across JSON/JavaScript boundaries."""

    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > _JSON_SAFE_INTEGER_MAX else value
    if isinstance(value, float):
        return value
    if isinstance(value, dict):
        return {
            str(key): (
                str(item)
                if key == "modified_ns" and isinstance(item, int)
                else _portable_json_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_portable_json_value(item) for item in value]
    return value


def _flow_passthrough(context: Any) -> dict[str, Any]:
    return dict(context.input_data or {})


def _flow_branch(context: Any) -> NodeExecutionResult:
    """Select every matching edge, or the single declared default edge."""

    from app.execution.engine import _evaluate_condition, _run_context

    edges = (
        context.db.query(ExecutionEdgeRun)
        .filter(
            ExecutionEdgeRun.run_id == context.run.id,
            ExecutionEdgeRun.source_node_id == context.node_run.node_id,
            ExecutionEdgeRun.status == "pending",
        )
        .order_by(ExecutionEdgeRun.edge_id.asc())
        .all()
    )
    evaluation_context = _run_context(context.db, context.run)
    matching = [
        edge
        for edge in edges
        if edge.condition not in (None, "", "default")
        and _evaluate_condition(edge.condition, evaluation_context)
    ]
    if not matching:
        defaults = [
            edge for edge in edges if edge.condition in (None, "", "default")
        ]
        if len(defaults) != 1:
            raise ExecutionApiError(
                500,
                "flow_branch_default_invalid",
                "flow.branch 必须锁定唯一 default 出边",
            )
        matching = defaults
    return NodeExecutionResult(
        output=dict(context.input_data or {}),
        selected_edge_ids=tuple(edge.edge_id for edge in matching),
    )


def _flow_join(context: Any) -> dict[str, Any]:
    """Collect the selected incoming branch outputs in a stable order.

    ``first_selected`` is woken by the first selected edge.  A later edge may
    resolve before the Worker claims the join, so the handler orders by the
    durable resolution timestamp and edge id instead of query/commit order.
    The engine changes a node from ``pending`` only once, which means late
    edges never schedule a second execution and sibling branches are not
    cancelled.
    """

    mode = str((context.node.get("config") or {}).get("mode") or "all_selected")
    selected_edges = (
        context.db.query(ExecutionEdgeRun)
        .filter(
            ExecutionEdgeRun.run_id == context.run.id,
            ExecutionEdgeRun.target_node_id == context.node_run.node_id,
            ExecutionEdgeRun.status == "selected",
        )
        .all()
    )
    selected_edges.sort(
        key=lambda edge: (
            edge.resolved_at.isoformat() if edge.resolved_at is not None else "",
            edge.edge_id,
        )
    )
    if mode == "first_selected":
        selected_edges = selected_edges[:1]
    source_ids = {edge.source_node_id for edge in selected_edges}
    source_runs = {
        row.node_id: row
        for row in context.db.query(ExecutionNodeRun)
        .filter(
            ExecutionNodeRun.run_id == context.run.id,
            ExecutionNodeRun.node_id.in_(source_ids),
        )
        .all()
    }
    selected_outputs = [
        dict(source_runs[edge.source_node_id].output_data or {})
        for edge in selected_edges
        if edge.source_node_id in source_runs
    ]
    mapped_items = (context.input_data or {}).get("items")
    if mode == "all_selected" and isinstance(mapped_items, list):
        return {"items": list(mapped_items)}
    return {"items": selected_outputs}


def _data_assign(context: Any) -> NodeExecutionResult:
    config = context.node.get("config") or {}
    configured = config.get("values") or {}
    supplied = (context.input_data or {}).get("values") or {}
    if not isinstance(configured, dict) or not isinstance(supplied, dict):
        raise ExecutionApiError(
            422,
            "data_assign_values_invalid",
            "data.assign 只接受对象形式的 globals patch",
        )
    values = {**supplied, **configured}
    if config.get("conflict") == "error":
        conflicts = sorted(set(values).intersection(context.run.global_data or {}))
        if conflicts:
            raise ExecutionApiError(
                409,
                "data_assign_conflict",
                "data.assign 将覆盖已有 globals 字段",
                details={"keys": conflicts},
            )
    return NodeExecutionResult(
        output={"globals": values},
        globals_patch=values,
        globals_conflict=str(config.get("conflict") or "overwrite"),
    )


def _data_aggregate(context: Any) -> dict[str, Any]:
    config = context.node.get("config") or {}
    mode = str(config.get("mode") or "collect")
    conflict = str(config.get("conflict") or "error")
    input_data = context.input_data or {}
    raw_items = input_data.get("items")
    if raw_items is None:
        raw_items = [input_data.get("values") or {}]
    if not isinstance(raw_items, list):
        raise ExecutionApiError(
            422,
            "data_aggregate_items_invalid",
            "data.aggregate 的 items 必须是数组",
        )
    items = list(raw_items)
    if mode == "collect":
        value: Any = items
    elif mode == "entries":
        entries: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ExecutionApiError(
                    422,
                    "data_aggregate_item_invalid",
                    "entries 模式只接受对象元素",
                    details={"index": index},
                )
            entries.extend(
                {"key": key, "value": item[key]}
                for key in sorted(item)
            )
        value = entries
    elif mode == "merge":
        merged: dict[str, Any] = {}
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ExecutionApiError(
                    422,
                    "data_aggregate_item_invalid",
                    "merge 模式只接受对象元素",
                    details={"index": index},
                )
            for key in sorted(item):
                if key in merged:
                    if conflict == "error":
                        raise ExecutionApiError(
                            409,
                            "data_aggregate_conflict",
                            "data.aggregate 合并字段发生冲突",
                            details={"key": key, "index": index},
                        )
                    if conflict == "first":
                        continue
                merged[key] = item[key]
        value = merged
    else:
        raise ExecutionApiError(
            422,
            "data_aggregate_mode_invalid",
            "data.aggregate 模式不受支持",
        )
    return {"value": value, "count": len(items)}


def _file_query(context: Any) -> dict[str, Any]:
    config = context.node.get("config") or {}
    root_id = str(config.get("root_id") or "")
    root = (
        context.db.query(ExecutionStorageRoot)
        .filter(
            ExecutionStorageRoot.root_id == root_id,
            ExecutionStorageRoot.is_active.is_(True),
            ExecutionStorageRoot.access_mode == "read",
        )
        .one_or_none()
    )
    if root is None:
        raise ExecutionApiError(
            422,
            "file_query_root_unavailable",
            "file.query 绑定的只读索引根不可用",
        )
    input_data = context.input_data or {}
    query_text = str(
        input_data.get("inspection_number")
        or input_data.get("query")
        or context.run.inspection_number
        or ""
    ).strip()
    statement = context.db.query(ExecutionFileIndexEntry).filter(
        ExecutionFileIndexEntry.storage_root_id == root.id,
        ExecutionFileIndexEntry.missing_since.is_(None),
    )
    if query_text:
        escaped = (
            query_text.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        statement = statement.filter(
            or_(
                ExecutionFileIndexEntry.inspection_number.ilike(
                    pattern, escape="\\"
                ),
                ExecutionFileIndexEntry.filename.ilike(pattern, escape="\\"),
                ExecutionFileIndexEntry.relative_path.ilike(
                    pattern, escape="\\"
                ),
            )
        )
    extensions = [str(value).casefold() for value in config.get("extensions") or []]
    if extensions:
        statement = statement.filter(
            ExecutionFileIndexEntry.extension.in_(extensions)
        )
    recent_days = config.get("recent_days")
    if recent_days is not None:
        statement = statement.filter(
            ExecutionFileIndexEntry.modified_at
            >= datetime.now(timezone.utc) - timedelta(days=int(recent_days))
        )
    sort = str(config.get("sort") or "modified_desc")
    order = {
        "modified_desc": (
            ExecutionFileIndexEntry.modified_at.desc(),
            ExecutionFileIndexEntry.relative_path.asc(),
        ),
        "modified_asc": (
            ExecutionFileIndexEntry.modified_at.asc(),
            ExecutionFileIndexEntry.relative_path.asc(),
        ),
        "name_asc": (
            ExecutionFileIndexEntry.filename.asc(),
            ExecutionFileIndexEntry.relative_path.asc(),
        ),
        "name_desc": (
            ExecutionFileIndexEntry.filename.desc(),
            ExecutionFileIndexEntry.relative_path.asc(),
        ),
    }.get(sort)
    if order is None:
        raise ExecutionApiError(422, "file_query_sort_invalid", "file.query sort 无效")
    limit = min(max(int(config.get("limit") or 20), 1), 100)
    rows = statement.order_by(*order).limit(limit + 1).all()
    truncated = len(rows) > limit
    items = []
    for entry in rows[:limit]:
        items.append(
            {
                "id": entry.id,
                "kind": "artifact",
                "label": entry.filename,
                "root_id": root.root_id,
                "relative_path": entry.relative_path,
                "fingerprint": entry.fingerprint,
                "metadata": {
                    "name": entry.filename,
                    "suffix": entry.extension,
                    "size": entry.size_bytes,
                    "modified_at": entry.modified_at.isoformat(),
                    "category": root.category_key,
                    **(entry.metadata_json or {}),
                },
            }
        )
    return {"items": items, "count": len(items), "truncated": truncated}


def _file_group(context: Any) -> dict[str, Any]:
    config = context.node.get("config") or {}
    if config.get("group_strategy") != "same_acquisition_name":
        raise ExecutionApiError(
            422,
            "file_group_strategy_invalid",
            "P2 仅支持 same_acquisition_name 分组策略",
        )
    raw_items = (context.input_data or {}).get("items") or []
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        relative_path = PurePosixPath(str(item.get("relative_path") or ""))
        if not relative_path.name:
            continue
        key = (
            str(item.get("root_id") or ""),
            str(relative_path.parent).casefold(),
            relative_path.stem.casefold(),
        )
        groups.setdefault(key, []).append(dict(item))
    values: list[dict[str, Any]] = []
    for key in sorted(groups):
        members = sorted(
            groups[key],
            key=lambda item: (
                str(item.get("relative_path") or "").casefold(),
                str(item.get("id") or ""),
            ),
        )
        identity = canonical_sha256(
            {
                "strategy": "same_acquisition_name",
                "root_id": key[0],
                "parent": key[1],
                "name": key[2],
                "item_ids": [str(item.get("id") or "") for item in members],
            }
        )
        values.append(
            {
                "id": f"group:{identity[:32]}",
                "kind": "artifact",
                "label": key[2],
                "fingerprint": identity,
                "items": members,
                "metadata": {
                    "group_strategy": "same_acquisition_name",
                    "root_id": key[0],
                    "parent": key[1],
                },
            }
        )
    limit = min(max(int(config.get("limit") or 100), 1), 100)
    return {
        "groups": values[:limit],
        "count": min(len(values), limit),
        "truncated": len(values) > limit,
    }


def _workbook_classify(context: Any) -> dict[str, Any]:
    from app.execution.excel_runtime import _classify_executor

    original = context.node
    config = dict(original.get("config") or {})
    converted_types = []
    for candidate in config.get("types") or []:
        features = []
        for feature in candidate.get("features") or []:
            converted = {
                "sheet": feature.get("sheet"),
                "cell": feature.get("cell"),
            }
            operator = feature.get("operator")
            if operator == "nonempty":
                converted["nonempty"] = True
            elif operator in {"equals", "contains"}:
                converted[operator] = feature.get("value")
            features.append(converted)
        converted_types.append({"name": candidate.get("name"), "features": features})
    context.node = {**original, "config": {**config, "types": converted_types}}
    try:
        return _classify_executor(context, detached_io=True)
    finally:
        context.node = original


def _workbook_extract_fields(context: Any) -> dict[str, Any]:
    from app.execution.excel_runtime import _extract_executor

    return _extract_executor(context, detached_io=True)


def _workbook_copy(context: Any) -> dict[str, Any]:
    from app.execution.mutation_runtime import _copy_executor

    return _portable_json_value(_copy_executor(context, detached_io=True))


def _workbook_write_cells(context: Any) -> dict[str, Any]:
    from app.execution.mutation_runtime import _write_executor

    receipt = _write_executor(context, detached_io=True)
    working = receipt.get("working_copy") or {}
    before = working.get("before_fingerprint") or {}
    after = working.get("after_fingerprint") or {}
    return _portable_json_value({
        "mutation_id": receipt.get("mutation_id"),
        "working_copy": {
            "root_id": working.get("root_id"),
            "relative_path": working.get("relative_path"),
            "fingerprint": after,
        },
        "before_fingerprint": before,
        "after_fingerprint": after,
        "writes": list(receipt.get("writes") or []),
        "verification": dict(receipt.get("verification") or {}),
    })


def _workbook_verify(context: Any) -> dict[str, Any]:
    from app.execution.models import ExecutionFileMutation, utcnow
    from app.execution.mutation_runtime import _verify_executor

    verification = _verify_executor(context, detached_io=True)
    mutation_id = str((context.input_data or {}).get("mutation_id") or "")
    mutation = (
        context.db.query(ExecutionFileMutation)
        .filter(
            ExecutionFileMutation.run_id == context.run.id,
            ExecutionFileMutation.mutation_id == mutation_id,
        )
        .with_for_update()
        .one()
    )
    subject = {
        "subject_type": "workbook_mutation",
        "run_id": context.run.id,
        "mutation_id": mutation_id,
        "working_content_sha256": verification.get("working_content_sha256"),
        "change_plan_checksum": verification.get("change_plan_checksum"),
        "target": (context.input_data or {}).get("target"),
        "verification": {
            key: value
            for key, value in verification.items()
            if key != "approval_context"
        },
    }
    subject_digest = canonical_sha256(subject)
    verified_at = utcnow().isoformat()
    receipt_payload = {
        "subject_type": "workbook_mutation",
        "subject_digest": subject_digest,
        "verified_at": verified_at,
        "mutation_id": mutation_id,
    }
    receipt_digest = canonical_sha256(receipt_payload)
    mutation.verification_result = {
        **(mutation.verification_result or {}),
        "subject": subject,
        "subject_type": "workbook_mutation",
        "subject_digest": subject_digest,
        "receipt_digest": receipt_digest,
        "verified_at": verified_at,
    }
    context.db.flush()
    return _portable_json_value({
        "mutation_id": mutation_id,
        "working_copy": (context.input_data or {}).get("working_copy"),
        "target": (context.input_data or {}).get("target"),
        "verification": verification,
        "verified_at": verified_at,
        "subject_type": "workbook_mutation",
        "subject_digest": subject_digest,
        "receipt_digest": receipt_digest,
    })


def _artifact_publish_v2(context: Any) -> dict[str, Any]:
    from app.execution.models import (
        ExecutionFileMutation,
        ExecutionHumanApprovalReceipt,
        ExecutionHumanApprovalReceiptConsumption,
        utcnow,
    )
    from app.execution.mutation_runtime import (
        _source_ref,
        _working_ref_from_input,
        publish_file_mutation,
    )

    input_data = context.input_data or {}
    receipt_id = str(input_data.get("approval_receipt_id") or "")
    receipt = (
        context.db.query(ExecutionHumanApprovalReceipt)
        .filter(ExecutionHumanApprovalReceipt.id == receipt_id)
        .with_for_update()
        .one_or_none()
    )
    if (
        receipt is None
        or receipt.run_id != context.run.id
        or receipt.decision != "approved"
    ):
        raise ExecutionApiError(
            409,
            "approval_receipt_invalid",
            "批准回执不存在、已失效或不属于当前运行",
        )
    mutation_id = str(input_data.get("mutation_id") or "")
    mutation = (
        context.db.query(ExecutionFileMutation)
        .filter(
            ExecutionFileMutation.run_id == context.run.id,
            ExecutionFileMutation.mutation_id == mutation_id,
        )
        .with_for_update()
        .one_or_none()
    )
    verification = mutation.verification_result if mutation is not None else {}
    expected_subject = str(verification.get("subject_digest") or "")
    supplied_subject = str(
        (input_data.get("verification_receipt") or {}).get("subject_digest") or ""
    )
    if (
        not expected_subject
        or receipt.subject_digest != expected_subject
        or receipt.subject_type != verification.get("subject_type")
        or supplied_subject != expected_subject
    ):
        raise ExecutionApiError(
            409,
            "approval_subject_mismatch",
            "批准回执与当前 mutation 核验 subject 不一致",
        )
    target_ref = _source_ref(input_data.get("target"))
    publish_root_id = (context.node.get("config") or {}).get("publish_root_id")
    if target_ref.root_id != publish_root_id:
        raise ExecutionApiError(422, "publish_root_mismatch", "发布目标根与绑定不一致")
    consumption = context.db.get(
        ExecutionHumanApprovalReceiptConsumption, receipt.id
    )
    if consumption is not None and (
        consumption.run_id != context.run.id
        or consumption.node_run_id != context.node_run.id
        or consumption.mutation_id != mutation_id
    ):
        raise ExecutionApiError(
            409,
            "approval_receipt_invalid",
            "批准回执已被其它发布节点消费",
        )
    if consumption is None:
        expires_at = receipt.expires_at
        now = utcnow()
        if expires_at is None:
            raise ExecutionApiError(
                409,
                "approval_receipt_invalid",
                "批准回执缺少有效期",
            )
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            raise ExecutionApiError(
                409,
                "approval_receipt_invalid",
                "批准回执已过期",
            )
        consumption = ExecutionHumanApprovalReceiptConsumption(
            approval_receipt_id=receipt.id,
            run_id=context.run.id,
            node_run_id=context.node_run.id,
            mutation_id=mutation_id,
            created_at=now,
        )
        context.db.add(consumption)
        context.db.flush()
    physical = _portable_json_value(publish_file_mutation(
        context.db,
        run=context.run,
        mutation_id=mutation_id,
        target_ref=target_ref,
        approval_context=verification.get("approval_context") or {},
        node_run_id=context.node_run.id,
        lease_token=context.lease_token,
        publish_node=context.node,
        expected_working_ref=_working_ref_from_input(input_data.get("working_copy")),
        approval_actor_user_id=receipt.actor_user_id,
    ))
    receipt_digest = canonical_sha256(
        {
            "run_id": context.run.id,
            "mutation_id": mutation_id,
            "approval_receipt_id": receipt.id,
            "subject_digest": expected_subject,
            "physical_receipt": physical,
        }
    )
    return {
        "artifact": physical.get("published") or physical.get("target") or input_data.get("target"),
        "publish_receipt": physical,
        "receipt_digest": receipt_digest,
    }


def _batch_place_output(
    plan: dict[str, Any],
    receipt: dict[str, Any],
    *,
    cancelled: bool = False,
) -> dict[str, Any]:
    placed_files = list(receipt.get("placed_files") or [])
    stable_receipt = {
        "placement_cancelled": cancelled,
        "plan_digest": canonical_sha256(plan),
        "target_root_id": str(plan.get("target_root_id") or ""),
        "target_relative_dir": str(plan.get("target_relative_dir") or ""),
        "placed_count": 0 if cancelled else len(placed_files),
        "placed_files": [] if cancelled else placed_files,
        "reconciliation_required": False,
    }
    return {
        **stable_receipt,
        "receipt_digest": canonical_sha256(stable_receipt),
    }


def _batch_place(context: Any) -> dict[str, Any] | NodeExecutionResult:
    """Execute one exact placement plan or suspend for a scoped decision.

    The internal request is inserted by the engine after validating the
    portable node input, so it can never be supplied by Workflow JSON or by a
    browser.  The browser submits only ``overwrite`` or ``cancel``; the worker
    recomputes the complete plan and source fingerprints before acting.
    """

    from app.execution.persistence import build_file_gateway
    from app.execution.report_image_placement import (
        _recognized_or_resumed_receipt,
        build_placement_plan,
        execute_placement_plan,
    )

    config = context.node.get("config") or {}
    input_data = context.input_data or {}
    inspection_number = (
        input_data.get("inspection_number") or context.run.inspection_number
    )
    gateway = build_file_gateway(context.db)
    # Release the business connection before target traversal and file I/O.
    # Worker lease renewal uses its existing independent short transaction.
    context.db.flush()
    context.db.commit()
    plan = build_placement_plan(
        gateway,
        config=config,
        inspection_number=inspection_number,
        sample_identity=input_data.get("sample_identity"),
        selected_images=input_data.get("items"),
    )
    plan_digest = canonical_sha256(plan)
    request = input_data.get("_native_suspension_request") or {}
    if request:
        if request.get("plan_digest") != plan_digest:
            raise ExecutionApiError(
                409,
                "file_batch_place_plan_changed",
                "文件或目标冲突状态已变化，原决定未执行，请重新运行并确认",
            )
        decision = request.get("decision")
        if decision == "cancel":
            return _batch_place_output(plan, {}, cancelled=True)
        if decision != "overwrite":
            raise ExecutionApiError(
                422,
                "file_batch_place_decision_invalid",
                "批量放置决定必须是 overwrite 或 cancel",
            )
        try:
            receipt = execute_placement_plan(gateway, plan, overwrite=True)
        except ExecutionApiError as exc:
            if exc.code == "report_image_reconciliation_required":
                raise ExecutionApiError(
                    500,
                    "file_batch_place_reconciliation_required",
                    "批量放置结果未知，需要人工核对目标目录",
                    details=exc.details,
                ) from exc
            raise
        return _batch_place_output(plan, receipt)

    if plan.get("conflicts"):
        try:
            reused = _recognized_or_resumed_receipt(gateway, plan)
        except ExecutionApiError as exc:
            if exc.code == "report_image_reconciliation_required":
                raise ExecutionApiError(
                    500,
                    "file_batch_place_reconciliation_required",
                    "批量放置结果未知，需要人工核对目标目录",
                    details=exc.details,
                ) from exc
            raise
        if reused is not None:
            return _batch_place_output(plan, reused)
        conflicts = sorted(str(value) for value in plan.get("conflicts") or [])
        return NodeExecutionResult(
            output={},
            suspension={
                "kind": "human_task",
                "resume_protocol": "retry_with_decision",
                "title": "确认批量文件放置冲突",
                "description": "目标目录存在同名文件，请选择覆盖或取消。",
                "form_schema": {
                    "type": "object",
                    "properties": {
                        "decision": {
                            "type": "string",
                            "enum": ["overwrite", "cancel"],
                        }
                    },
                    "required": ["decision"],
                    "additionalProperties": False,
                },
                "state": {"plan_digest": plan_digest},
                "renderer_payload": {
                    "plan_digest": plan_digest,
                    "target_root_id": plan.get("target_root_id"),
                    "target_relative_dir": plan.get("target_relative_dir"),
                    "display_directory": plan.get("display_directory"),
                    "target_filenames": [
                        item.get("target_filename")
                        for item in plan.get("files") or []
                    ],
                    "conflicts": conflicts,
                },
            },
        )
    try:
        receipt = execute_placement_plan(gateway, plan, overwrite=False)
    except ExecutionApiError as exc:
        if exc.code == "report_image_reconciliation_required":
            raise ExecutionApiError(
                500,
                "file_batch_place_reconciliation_required",
                "批量放置结果未知，需要人工核对目标目录",
                details=exc.details,
            ) from exc
        raise
    return _batch_place_output(plan, receipt)


_NATIVE_HANDLERS: dict[tuple[str, int], NativeHandler] = {
    ("flow.branch", 1): _flow_branch,
    ("flow.fork", 1): _flow_passthrough,
    ("flow.join", 1): _flow_join,
    ("data.aggregate", 1): _data_aggregate,
    ("data.assign", 1): _data_assign,
    ("file.query", 1): _file_query,
    ("file.group", 1): _file_group,
    ("file.batch_place", 1): _batch_place,
    ("artifact.publish", 2): _artifact_publish_v2,
    ("workbook.classify", 1): _workbook_classify,
    ("workbook.extract_fields", 1): _workbook_extract_fields,
    ("workbook.copy", 1): _workbook_copy,
    ("workbook.write_cells", 2): _workbook_write_cells,
    ("workbook.verify", 2): _workbook_verify,
}


def native_handler(node_type: str, type_version: int) -> NativeHandler | None:
    return _NATIVE_HANDLERS.get((node_type, type_version))


def native_handler_identities() -> set[tuple[str, int]]:
    return set(_NATIVE_HANDLERS)
