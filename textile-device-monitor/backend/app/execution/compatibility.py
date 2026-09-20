"""Keep compatibility execution only while editable or resumable references exist."""

from collections import Counter
from copy import deepcopy

from app.execution.models import (
    ExecutionExternalOperation, ExecutionHumanTask, ExecutionRun, ExecutionWorkflow,
    ExecutionWorkflowRelease, ExecutionWorkflowVersion,
)
from app.execution.v2.canonical import canonical_sha256
from app.execution.v2.registry import get_installed_registry


def compatibility_audit(db, *, include_history=True):
    registry = get_installed_registry()
    specs = [item for item in registry.list_node_specs() if item.source == "v1_registry_adapter"]
    identities = {(item.type, item.type_version): item for item in specs}
    refs = {key: [] for key in identities}
    historical = Counter()
    unknown = []

    def collect(definition, kind, identity, *, live=True, lock=None, dependencies=None):
        contracts = (lock or {}).get("node_instances") or []
        contracts = list(contracts.values()) if isinstance(contracts, dict) else contracts
        by_node = {item.get("node_id"): item for item in contracts}
        by_type = {(item.get("type"), item.get("type_version")): item for item in (dependencies or {}).get("node_types", [])}
        for node in (definition or {}).get("nodes", []):
            if node.get("disabled"):
                continue
            key = (node.get("type"), node.get("type_version", 1))
            binding = by_node.get(node.get("id"))
            source = (binding or {}).get("source")
            if source is None and key in by_type:
                try:
                    source = registry.resolve_node_spec(*key, by_type[key]["contract_digest"]).source
                except LookupError:
                    unknown.append({"kind": kind, "id": identity, "node_id": node.get("id"), "type": key[0], "type_version": key[1]})
            if source == "resource" or key not in identities:
                continue
            if live:
                refs[key].append({"kind": kind, "id": identity, "node_id": node.get("id")})
            else:
                historical[key] += 1

    workflows = {row.id: row for row in db.query(ExecutionWorkflow).all()}
    for workflow in workflows.values():
        if workflow.management_mode == "draft_v1":
            collect(workflow.draft_definition, "draft", workflow.id, live=workflow.archived_at is None)
    for version in db.query(ExecutionWorkflowVersion).yield_per(200):
        workflow = workflows.get(version.workflow_id)
        live = workflow is not None and workflow.archived_at is None
        if live or include_history:
            collect(version.definition, "published_version", version.id, live=live, lock=version.dependency_lock)
    for release in db.query(ExecutionWorkflowRelease).filter(ExecutionWorkflowRelease.status == "staged").yield_per(200):
        document = release.portable_document or {}
        collect(document.get("definition"), "staged_release", release.id, dependencies=document.get("dependencies"))
    # Failed Runs can be retried. Even a terminal Run's outstanding human or
    # external work retains its frozen compatibility handlers until settled.
    pending_run_ids = {row[0] for row in db.query(ExecutionHumanTask.run_id).filter(ExecutionHumanTask.status == "open").distinct()}
    pending_run_ids.update(row[0] for row in db.query(ExecutionExternalOperation.run_id).filter(
        ExecutionExternalOperation.run_id.isnot(None),
        ExecutionExternalOperation.status.notin_(["completed", "cancelled", "failed"]),
    ).distinct())
    runs = db.query(ExecutionRun)
    if not include_history:
        from sqlalchemy import or_
        runs = runs.filter(or_(ExecutionRun.status.notin_(["completed", "cancelled"]), ExecutionRun.id.in_(pending_run_ids)))
    for run in runs.yield_per(200):
        collect(run.definition_snapshot, "run", run.id,
                live=run.status not in {"completed", "cancelled"} or run.id in pending_run_ids, lock=run.dependency_lock)
    items = [{"type": key[0], "type_version": key[1], "active_reference_count": len(refs[key]),
              "historical_reference_count": historical[key], "state": "required" if refs[key] else "retired",
              "references": refs[key]} for key in sorted(identities)]
    return {"items": items, "required_count": sum(bool(refs[key]) for key in identities),
            "retired_count": sum(not refs[key] for key in identities), "unknown_contracts": unknown,
            "policy": "保留可编辑流程、可回滚版本、待发布 Release、未完成及可重试 Run 所引用的兼容能力；其余停止 Worker 广告。历史记录保留，回切后自动恢复能力。"}


def active_worker_capabilities(db, document):
    """A fresh reference check makes retirement reversible without database edits."""
    audit = compatibility_audit(db, include_history=False)
    retired = {(item["type"], item["type_version"]) for item in audit["items"] if item["state"] == "retired"}
    registry = get_installed_registry()
    compat_digests = {spec.contract_digest for spec in registry.list_node_specs() if spec.source == "v1_registry_adapter"}
    result = deepcopy(document)
    result["nodes"] = [node for node in document["nodes"] if not (
        (node["type"], node["type_version"]) in retired and node["contract_digest"] in compat_digests
    )]
    result["capability_digest"] = canonical_sha256(result["nodes"])
    return result
