"""Deterministic P3 migration of the two regenerated-fiber workflows."""

from copy import deepcopy

from app.execution.project_rules import REGENERATED_RULE_KEYS, resolve_rule


WORKFLOW_METHODS = {
    "regenerated-fiber-area-method": "area",
    "regenerated-fiber-count-method": "count",
}


def migrate_records(db, workflow, document, suggestions):
    candidate = deepcopy(document)
    nodes = candidate["definition"]["nodes"]
    method = WORKFLOW_METHODS[workflow.slug]
    query_type = f"file.regenerated_fiber_{method}_method"
    result_type = f"result.regenerated_fiber_{method}_method"
    found = {
        kind: [node for node in nodes if node["type"] == kind]
        for kind in (query_type, result_type, "human.file_selection")
    }
    if any(len(values) != 1 for values in found.values()):
        return candidate, [], [{
            "phase": "P3", "code": "migration_required_node_inactive",
            "message": "再生纤迁移需要各一个活动的查询、结果读取和选择节点",
        }]
    query, result, select = (found[kind][0] for kind in found)
    root_slot = query["config"].get("root_slot")
    if (
        not root_slot
        or result.get("input_mapping") != {"files": f"$.nodes.{query['id']}.output.candidates"}
        or select.get("input_mapping") != {"files": f"$.nodes.{result['id']}.output.files"}
    ):
        return candidate, [], [{
            "phase": "P3", "code": "migration_custom_mapping",
            "message": "结果读取或选择使用了自定义来源，请先核对映射",
        }]

    resources = candidate["resources"]
    preview_slot = "result_previews"
    occupied = {slot["slot_id"] for slot in resources["root_slots"]}
    while preview_slot in occupied:
        preview_slot += "_output"
    resources["root_slots"].append({
        "slot_id": preview_slot, "name": "结果插图", "access": "write", "required": True,
    })
    suggestions["root_slots"][preview_slot] = {"root_id": "execution_staging"}

    rule_slot = query["config"].get("rule_slot")
    if not rule_slot:
        rule = resolve_rule(db, REGENERATED_RULE_KEYS[query_type])
        rule_slot = "record_match"
        occupied_rules = {slot["slot_id"] for slot in resources["rule_slots"]}
        while rule_slot in occupied_rules:
            rule_slot += "_rule"
        resources["rule_slots"].append({
            "slot_id": rule_slot, "name": rule.display_name, "rule_type": "project_match",
            "contract_version": 1, "required": True,
        })
        suggestions["rule_slots"][rule_slot] = {"rule_key": rule.key, "revision": rule.revision}

    transformations = []
    for node, target in ((query, "regenerated_fiber.find_records"),
                         (result, "regenerated_fiber.read_results"),
                         (select, "human.select")):
        transformations.append({
            "migration_id": f"{target.replace('.', '-')}-v1",
            "source_node_id": node["id"],
            "source": {"type": node["type"], "type_version": node["type_version"]},
            "targets": [{"node_id": node["id"], "type": target, "type_version": 1}],
        })
        node["type"] = target
        node["type_version"] = 1
        node["__native_p2"] = True
    query["config"] = {
        "method": method, "root_slot": root_slot, "rule_slot": rule_slot,
        "limit": query["config"].get("limit", 6),
    }
    result["config"] = {"method": method, "root_slot": root_slot, "preview_root_slot": preview_slot}
    old_select = select["config"]
    select["config"] = {
        "title": old_select.get("title") or "选择结果文件",
        "description": old_select.get("description") or "",
        "item_kind": "artifact", "min_selected": 1,
        "max_selected": 6 if old_select.get("allow_multiple", True) else 1,
        "require_primary": bool(old_select.get("require_primary", True)),
        "auto_submit_single_candidate": True,
        **({"candidate_role_slot": old_select["candidate_role_slot"]}
           if old_select.get("candidate_role_slot") else {}),
    }
    select["input_mapping"] = {"items": f"$.nodes.{result['id']}.output.items"}

    replacements = {
        f"$.nodes.{select['id']}.output.{old}": f"$.nodes.{select['id']}.output.{new}"
        for old, new in (("selected_files", "selected_items"),
                         ("primary_file_id", "primary_id"), ("primary_file", "primary_item"))
    }

    def rewrite(value):
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return replacements.get(value, value) if isinstance(value, str) else value

    for node in nodes:
        node["input_mapping"] = rewrite(node.get("input_mapping") or {})
    blockers = [{
        "phase": "P4" if node["type"].startswith("external.") else "P3",
        "code": "custom_node_deferred", "node_id": node["id"],
        "message": f"{node['type']} remains a compatibility node",
    } for node in nodes if not node.get("__native_p2") and node["type"] not in {"core.start", "core.end"}]
    candidate["release"]["release_note"] = "Native P3 regenerated-fiber candidate; one valid result is selected automatically"
    return candidate, transformations, blockers
