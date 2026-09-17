"""Migrate the local portion of microscopy and paper workflows in place."""

from copy import deepcopy

from app.execution.project_rules import PAPER_FIBER_RULE_KEY, microscopy_rule_key, resolve_rule


WORKFLOW_SLUGS = {
    "electron-microscopy-gbt36422",
    "electron-cross-section-gbt36422",
    "paper-fiber-gbt4688-2020-qualitative",
}
TARGETS = {
    "file.electron_microscopy_gbt36422": "microscopy.image_candidates",
    "file.paper_fiber_gbt4688_qualitative": "paper_fiber.find_records",
    "workbook.microscopy_original_record": "microscopy.original_record.render",
    "workbook.microscopy_check_record": "microscopy.check_record.render",
}


def migrate_domain_records(db, workflow, document, suggestions):
    candidate = deepcopy(document)
    nodes = candidate["definition"]["nodes"]
    transformations, blockers = [], []
    discovery_roots = {
        node["config"].get("root_slot") for node in nodes
        if node["type"] == "file.electron_microscopy_gbt36422"
    } - {None}
    for node in nodes:
        old = node["type"]
        if old not in TARGETS:
            continue
        config = node.get("config") or {}
        family = config.get("record_family", "microscopy")
        inputs = deepcopy(node.get("input_mapping") or {})
        target = TARGETS[old]
        if old.startswith("file."):
            if not config.get("root_slot"):
                continue
            rule_slot = config.get("rule_slot")
            if not rule_slot:
                key = PAPER_FIBER_RULE_KEY if target.startswith("paper_fiber.") else microscopy_rule_key(family)
                rule = resolve_rule(db, key)
                rule_slot = "record_match"
                occupied = {slot["slot_id"] for slot in candidate["resources"]["rule_slots"]}
                while rule_slot in occupied:
                    rule_slot += "_rule"
                candidate["resources"]["rule_slots"].append({
                    "slot_id": rule_slot, "name": rule.display_name, "rule_type": "project_match",
                    "contract_version": 1, "required": True,
                })
                suggestions["rule_slots"][rule_slot] = {"rule_key": rule.key, "revision": rule.revision}
            new_config = {
                "root_slot": config["root_slot"], "rule_slot": rule_slot,
                "require_full_task_match": config.get("require_full_task_match", False),
                **({"record_family": family} if target.startswith("microscopy.")
                   else {"limit": config.get("limit", 6)}),
            }
        else:
            if not config.get("staging_root_slot") or "judgement_required" not in inputs:
                continue
            new_config = {"staging_root_slot": config["staging_root_slot"], "record_family": family}
            if target == "microscopy.original_record.render":
                if len(discovery_roots) != 1 or "selected_images" not in inputs:
                    continue
                new_config["source_root_slot"] = next(iter(discovery_roots))
                inputs["images"] = inputs.pop("selected_images")
            elif "check_item_name" not in inputs and isinstance(inputs.get("selected_project"), str):
                inputs["check_item_name"] = inputs["selected_project"] + ".check_item_name"
            for redundant in ("task", "selected_project", "selected_image_ids", "expected_key_identities"):
                inputs.pop(redundant, None)
        node.update(type=target, type_version=1, config=new_config, input_mapping=inputs, __native_p2=True)
        transformations.append({
            "migration_id": target.replace(".", "-") + "-v1", "source_node_id": node["id"],
            "source": {"type": old, "type_version": 1},
            "targets": [{"node_id": node["id"], "type": target, "type_version": 1}],
        })

    for node in nodes:
        if node.get("__native_p2") or node["type"] in {"core.start", "core.end"}:
            continue
        external = node["type"].startswith("external.")
        blockers.append({
            "phase": "P4" if external else "P3", "node_id": node["id"],
            "code": "external_connector_deferred" if external else "domain_adapter_deferred",
            "message": f"{node['type']} remains a compatibility node",
        })
    candidate["release"]["release_note"] = "Native P3 domain services; external operations and business forms remain deferred"
    return candidate, transformations, blockers
