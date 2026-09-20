"""Complete built-in domain workflows with ordinary nodes and shared operations."""

from copy import deepcopy

from app.execution.v2.domain_record_migration import migrate_domain_records
from app.execution.v2.registry import LEGACY_NODE_OPERATION_REFS, get_installed_registry


FORM_FIELDS = {
    key: {"type": ["string", "null"]} for key in (
        "selected_project_key", "sample_name", "sample_identity", "judge_basis", "judgement",
        "standard_value", "indicator_requirement", "test_result", "remark",
    )
}
FORM_RESULT = {"type": "object", "properties": FORM_FIELDS, "required": ["selected_project_key"], "additionalProperties": False}


def migrate_business(db, workflow, document, suggestions):
    candidate, changes, _ = migrate_domain_records(db, workflow, document, suggestions)
    original_nodes = candidate["definition"]["nodes"]
    paper = workflow.slug.startswith("paper-fiber-")
    family = "paper_fiber" if paper else "cross_section" if "cross-section" in workflow.slug else "microscopy"
    queries = [n for n in original_nodes if n["type"] == ("paper_fiber.find_records" if paper else "microscopy.image_candidates")]
    selections = [n for n in original_nodes if n["type"] == ("human.file_selection" if paper else "human.image_selection")]
    forms = [n for n in original_nodes if n["type"] == "human.input" and (
        (n.get("config", {}).get("paper_judgement") or n.get("config", {}).get("legacy_generic_record_input")) if paper
        else "sample_name" in n.get("config", {}).get("form_schema", {}).get("properties", {})
    )]
    uploads = [n for n in original_nodes if n["type"] == ("external.legacy_special_wool_qualitative_upload" if paper else "external.legacy_special_wool_image_upload")]
    reviews = [n for n in original_nodes if n["type"] == ("external.legacy_special_wool_qualitative_review" if paper else "external.legacy_special_wool_review")]
    entries = [n for n in original_nodes if n["type"] == ("external.legacy_generic_check_record_entry" if paper else "external.legacy_microscopy_check_record_entry")]
    if any(len(group) != 1 for group in (queries, selections, forms, uploads, reviews, entries)):
        return candidate, changes, [{"phase": "P4", "code": "business_migration_shape_changed", "message": "完整迁移需要活动的查询、选择、业务输入、上传、复核和登记节点"}]
    known = {"core.start", "core.end", "human.file_selection", "human.image_selection", "human.input", "human.confirm",
             "data.microscopy_record_context", "branch.condition", "microscopy.image_candidates", "paper_fiber.find_records",
             "microscopy.original_record.render", "microscopy.check_record.render", *LEGACY_NODE_OPERATION_REFS}
    unknown = [n for n in original_nodes if n["type"] not in known]
    if unknown:
        return candidate, changes, [{"phase": "P4", "code": "custom_node_deferred", "node_id": n["id"],
                                     "message": "自定义步骤需在设计器中核对"} for n in unknown]
    query, select, form, upload, review, entry = (group[0] for group in (queries, selections, forms, uploads, reviews, entries))
    ids = {n["id"] for n in original_nodes}

    def unique(base):
        while base in ids:
            base += "-v2"
        ids.add(base)
        return base

    prepare_id, validate_id = unique("prepare-input"), unique("validate-input")
    path = lambda node, field="": f"$.nodes.{node['id'] if isinstance(node, dict) else node}.output" + (f".{field}" if field else "")
    selected_images = path(prepare_id, "context.selected_images")
    decisions = [n for n in original_nodes if n.get("config", {}).get("legacy_existing_record_decision") or n.get("config", {}).get("paper_existing_record_decision")]
    retained_ids = {n["id"] for group in (queries, selections, forms, uploads, reviews, entries) for n in group}
    decision_paths = {path(n, "registration_cancelled") for n in decisions}
    removable_types = {"core.start", "core.end", "data.microscopy_record_context",
                       "microscopy.original_record.render", "microscopy.check_record.render"}
    extra = [n for n in original_nodes if n["id"] not in retained_ids
             and n not in decisions and n["type"] not in removable_types
             and not (n["type"] == "human.input" and n.get("config", {}).get("report_image_placement"))
             and not (n["type"] == "branch.condition" and not n.get("config")
                      and set(n.get("input_mapping", {})) == {"registration_cancelled"}
                      and n["input_mapping"]["registration_cancelled"] in decision_paths)]
    if extra:
        return candidate, changes, [{"phase": "P4", "code": "custom_node_deferred", "node_id": n["id"],
                                     "message": "自定义步骤需在设计器中核对，未生成完整候选"} for n in extra]
    replacements = {path(form): path(validate_id), path(select, "task"): path(query, "task"),
                    path(select, "selected_image_ids"): path(select, "selected_ids"),
                    path(select, "selected_images"): selected_images}
    replacements.update({path(n): path(validate_id, "registration_decision") for n in decisions})

    def rewrite(value):
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, str):
            for old, new in sorted(replacements.items(), key=lambda pair: -len(pair[0])):
                if value == old or value.startswith(old + "."):
                    return new + value[len(old):]
        return value

    select.update(type="human.select", type_version=1, __native_p2=True,
                  config={"title": "选择原始记录" if paper else "选择图片", "item_kind": "artifact" if paper else "image",
                          "min_selected": 1, "max_selected": 1 if paper else 3 if family == "cross_section" else 10,
                          "require_primary": paper, "auto_submit_single_candidate": True},
                  input_mapping={"items": path(query, "items")})
    prepare_input = {"inspection_number": "$.inputs.inspection_number", "task": path(query, "task")}
    if paper:
        prepare_input.update(selected_project=path(query, "matched_task_project"), result=path(select, "primary_item.metadata.result"))
    else:
        prepare_input.update(selected_image_ids=path(select, "selected_ids"), selected_images=path(select, "selected_items"))
    prepare = {"id": prepare_id, "type": "inspection.record.prepare", "type_version": 1, "name": "准备登记输入",
               "config": {"record_family": family}, "input_mapping": prepare_input, "__native_p2": True}
    form.update(type="human.form", type_version=2, __native_p2=True,
                config={"title": "填写登记信息", "result_schema": deepcopy(FORM_RESULT), "auto_submit_complete": True},
                input_mapping={key: path(prepare_id, key) for key in ("form_schema", "defaults", "context")})
    validate = {"id": validate_id, "type": "inspection.record.validate", "type_version": 1, "name": "整理登记字段",
                "config": {"record_family": family}, "input_mapping": {"context": path(prepare_id, "context"), "form": path(form)}, "__native_p2": True}
    start = next(n for n in original_nodes if n["type"] == "core.start")
    nodes = [start, query, select, prepare, form, validate]
    original_record = next((n for n in original_nodes if n["type"] == "microscopy.original_record.render"), None)
    check_record = next((n for n in original_nodes if n["type"] == "microscopy.check_record.render"), None)
    if not paper and (original_record is None or check_record is None):
        return candidate, changes, [{"phase": "P4", "code": "business_workbook_node_missing", "message": "完整迁移需要活动的原始记录和检验记录渲染节点"}]
    if original_record:
        original_record["input_mapping"] = rewrite(original_record["input_mapping"])
        nodes.append(original_record)
    operations = []
    registry = get_installed_registry()
    for node in (upload, review, entry):
        reference = ("legacy_fibrecheck.paper_fiber.check_record_entry@1" if paper and node is entry else LEGACY_NODE_OPERATION_REFS[node["type"]])
        node.update(type="external.operation", type_version=1, __native_p2=True,
                    config={"operation_ref": reference, "credential_slot": node["config"]["credential_slot"]},
                    input_mapping=rewrite(node["input_mapping"]))
        installed = registry.resolve_operation("legacy_fibrecheck", "*", reference.split(".", 1)[1].split("@")[0], 1)
        operations.append({"operation": installed.operation, "contract_version": 1, "contract_digest": installed.contract_digest})
    if paper:
        upload["input_mapping"].update(selected_files=[path(select, "primary_item.metadata")], primary_file_id=path(select, "primary_id"), primary_file=path(select, "primary_item.metadata"))
        entry["input_mapping"]["judgement_input"] = path(validate_id)
    nodes.extend([upload, review])
    if check_record:
        check_record["input_mapping"] = rewrite(check_record["input_mapping"])
        nodes.append(check_record)
    nodes.append(entry)
    placement = next((n for n in original_nodes if n.get("config", {}).get("report_image_placement")), None)
    if placement:
        slot = placement["config"].get("target_root_slot")
        placement.update(type="file.batch_place", type_version=1, __native_p2=True,
                         config={"target_root_slot": slot, "target_directory": placement["config"].get("target_directory", ""), "naming": "inspection-sample-index"},
                         input_mapping={"inspection_number": "$.inputs.inspection_number", "sample_identity": path(validate_id, "sample_identity"), "items": path(validate_id, "placement_items")})
        nodes.append(placement)
    end = next((n for n in original_nodes if n["id"] == "end" and n["type"] == "core.end"),
               [n for n in original_nodes if n["type"] == "core.end"][-1])
    end["name"] = "完成"
    end["input_mapping"] = {"registration": path(entry), "upload": path(upload), "review": path(review)}
    if original_record:
        end["input_mapping"]["original_record"] = path(original_record, "original_record")
    if placement:
        end["input_mapping"]["report_images"] = path(placement)
    if paper:
        end["input_mapping"]["primary_file"] = path(select, "primary_item.metadata")
    nodes.append(end)
    for index, node in enumerate(nodes):
        node["ui"] = {"x": 40 + index * 240, "y": 160}
    candidate["definition"].update(nodes=nodes, edges=[{"id": f"step-{index}", "source": left["id"], "target": right["id"], "join_policy": "all"} for index, (left, right) in enumerate(zip(nodes, nodes[1:]), 1)])
    connector = registry.resolve_connector("legacy_fibrecheck", "*")
    candidate["dependencies"]["connectors"] = [{"connector_id": connector.connector_id, "version_range": connector.version,
        "distribution_digest": connector.distribution_digest, "operations": operations, "queries": []}]
    candidate["release"]["release_note"] = "Native P4 business flow: select, fill missing fields, submit once"
    changes.append({"migration_id": "native-p4-business-v1", "source_node_id": form["id"], "source": {"type": "human.input", "type_version": 1},
                    "targets": [{"node_id": n["id"], "type": n["type"], "type_version": n["type_version"]} for n in nodes]})
    return candidate, changes, []
