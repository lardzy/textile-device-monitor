"""Portable rule contents and immutable deployment snapshots.

Only a logical root slot travels in JSON. Database identities and resolved paths
belong to the deployment binding. Legacy releases may still bind a local rule.
"""

from copy import deepcopy
from types import SimpleNamespace

from app.execution.errors import ExecutionApiError
from app.execution.project_rules import resolve_rule, row_to_rule, validate_rule_config
from app.execution.v2.canonical import canonical_sha256


def export_rule(row, root_slot):
    config = deepcopy(row.config)
    config["source"].pop("root_id", None)
    config["source"]["root_slot"] = root_slot
    return {"rule_key": row.rule_key, "display_name": row.display_name,
            "category_key": row.category_key, "revision": row.revision, "config": config}


def bind_rule(definition, roots):
    snapshot = deepcopy(definition)
    source = snapshot["config"].setdefault("source", {})
    slot = source.pop("root_slot", None)
    if slot not in roots:
        raise ExecutionApiError(422, "rule_root_unbound", f"规则数据目录尚未绑定：{slot}")
    source["root_id"] = roots[slot]["root_id"]
    issues = validate_rule_config(snapshot["config"])
    if issues:
        raise ExecutionApiError(422, "rule_definition_invalid", "；".join(issues))
    return {"rule_key": snapshot["rule_key"], "revision": snapshot["revision"],
            "definition": snapshot, "definition_digest": canonical_sha256(snapshot)}


def definition_issues(document):
    issues = []
    roots = {slot["slot_id"]: {"root_id": "preview"} for slot in document["resources"].get("root_slots", [])}
    seen = {}
    for slot in document["resources"].get("rule_slots", []):
        if "definition" not in slot:
            continue
        try:
            binding = bind_rule(slot["definition"], roots)
            digest = canonical_sha256(slot["definition"])
            if seen.setdefault(binding["rule_key"], digest) != digest:
                raise ValueError("同一发布不能包含同名且内容不同的规则")
        except (ExecutionApiError, ValueError, KeyError, TypeError, AttributeError) as exc:
            issues.append({"level": "error", "code": "rule_definition_invalid",
                           "path": f"$.resources.rule_slots.{slot['slot_id']}.definition", "message": str(exc)})
    return issues


def bound_rule(context):
    key = context.node["config"].get("match_rule")
    bindings = (context.run.deployment_binding_snapshot or {}).get("bindings") or {}
    values = [item for item in (bindings.get("rule_slots") or {}).values() if item["rule_key"] == key]
    snapshots = [item for item in values if "definition" in item]
    if snapshots:
        if len({item["definition_digest"] for item in snapshots}) != 1:
            raise ExecutionApiError(422, "rule_definition_ambiguous", "同名规则包含不同定义")
        return row_to_rule(SimpleNamespace(**snapshots[0]["definition"], enabled=True))
    rule = resolve_rule(context.db, key)
    if {item["revision"] for item in values} != {rule.revision} or not rule.enabled:
        raise ExecutionApiError(409, "deployment_binding_stale", "项目规则已更新，请重新绑定并发布流程")
    return rule
