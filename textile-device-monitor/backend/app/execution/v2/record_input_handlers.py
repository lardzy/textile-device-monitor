"""Prepare one ordinary schema form; validate business choices in one service."""

from copy import deepcopy
from types import SimpleNamespace

from app.execution.errors import ExecutionApiError
from app.execution.microscopy_original_record import _microscopy_record_context_executor, _truthy_judgement_flag
from app.execution.record_input import validate_microscopy_input


def _options(value):
    import re

    values = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(part.strip() for value in values
                             for part in re.split(r"[，,、]", str(value or "")) if part.strip()))


def _judgement(project):
    return _truthy_judgement_flag(project.get("give_judgement"))


def _text(title, *, default=None, options=None):
    field = {"type": "string", "title": title, "maxLength": 2000}
    if default is not None:
        field["default"] = default
    if options:
        field["enum"] = options
    return field


def prepare_record_input(context):
    family, data = context.node["config"]["record_family"], context.input_data
    paper = family == "paper_fiber"
    if paper:
        project = data["selected_project"]
        state = {"projects": [project], "task": data["task"], "result": data.get("result") or {}}
    else:
        values = {**data, "selected_images": [item.get("metadata", item) for item in data["selected_images"]]}
        state = _microscopy_record_context_executor(SimpleNamespace(
            db=context.db, run=context.run, node=context.node, input_data=values,
        ))
        state.pop("task_kind", None)
    projects = state["projects"]
    if not projects:
        raise ExecutionApiError(422, "record_project_missing", "任务单没有可用检测项目，请先刷新任务信息")
    properties = {
        "selected_project_key": {**_text("检测项目", options=[p["project_key"] for p in projects]),
                                 "enumNames": [p.get("check_item_name", p["project_key"]) for p in projects]},
    }
    defaults = {}
    if len(projects) == 1:
        defaults["selected_project_key"] = projects[0]["project_key"]
        properties["selected_project_key"]["default"] = projects[0]["project_key"]
    if not paper:
        name = state.get("sample_name_analysis") or {}
        # Preserve name analysis; ambiguous names remain a user choice.
        suggested = name.get("automatic_value") or name.get("suggested_value")
        properties["sample_name"] = _text("样品名称", default=suggested)
        properties["sample_name"]["x-suggestions"] = name.get("candidates", [])
        if suggested:
            defaults["sample_name"] = suggested
    alternatives = []
    all_fields = ["sample_identity", "judge_basis", "judgement", "standard_value",
                  "indicator_requirement", "test_result", "remark"]
    for name in all_fields:
        properties[name] = {"type": ["string", "null"], "maxLength": 2000, "x-hidden": True}
    for project in projects:
        identities = _options(project.get("sample_identify"))
        fields = {}
        required = []
        if identities:
            fields["sample_identity"] = _text("样品识别", options=identities,
                                               default=identities[0] if len(identities) == 1 else None)
            required.append("sample_identity")
        if _judgement(project):
            basis = _options(project.get("check_basis_options") or project.get("judge_basis_options")
                             or state.get("check_basis_options") or state["task"].get("check_basis"))
            fields["judge_basis"] = _text("判定依据", default=basis[0] if len(basis) == 1 else None)
            if basis:
                fields["judge_basis"]["x-suggestions"] = basis
            fields["judgement"] = _text("判定结果", options=_options(project.get("judgement_options")) or ["符合", "不符合"])
            if paper:
                fields["standard_value"] = _text("标准值与允差")
                fields["standard_value"]["x-copy-sources"] = [
                    {"label": "原始记录", "text": str((state.get("result") or {}).get("value") or "")},
                    {"label": "任务说明", "text": str(project.get("remark") or "")},
                ]
            else:
                fields["indicator_requirement"] = _text("指标要求")
                fields["test_result"] = _text("测试结果")
            required.extend(key for key in fields if key != "sample_identity")
        if not paper:
            fields["remark"] = _text("备注")
        alternatives.append({
            "if": {"properties": {"selected_project_key": {"const": project["project_key"]}}, "required": ["selected_project_key"]},
            "then": {"properties": fields, "required": required},
        })
        if len(projects) == 1:
            defaults.update({key: field["default"] for key, field in fields.items() if "default" in field})
    schema = {"type": "object", "properties": properties, "additionalProperties": False,
              "required": ["selected_project_key"] + ([] if paper else ["sample_name"]), "allOf": alternatives}
    return {"form_schema": schema, "defaults": defaults, "context": {**state, "record_family": family}}


def validate_record_input(context):
    state, data = context.input_data["context"], context.input_data["form"]
    if state["record_family"] != "paper_fiber":
        result = validate_microscopy_input(state, data)
    else:
        project = next((p for p in state["projects"] if p["project_key"] == data["selected_project_key"]), None)
        if project is None:
            raise ExecutionApiError(409, "record_project_not_offered", "检测项目不属于本次任务")
        identities = _options(project.get("sample_identify"))
        identity = str(data.get("sample_identity") or "").strip()
        if (identities and identity not in identities) or (not identities and identity):
            raise ExecutionApiError(422, "paper_sample_identity_not_offered", "样品识别不属于任务单")
        result = {"selected_project_key": project["project_key"], "selected_project": project,
                  "sample_identity": identity or None, "judgement_required": _judgement(project),
                  **{key: str(data.get(key) or "").strip() if _judgement(project) else ""
                     for key in ("judge_basis", "judgement", "standard_value")}}
        if result["judgement_required"] and not all(result[k] for k in ("judge_basis", "judgement", "standard_value")):
            raise ExecutionApiError(422, "paper_judgement_required", "请填写判定依据、结果和标准值")
    project = result["selected_project"]
    count = project.get("register_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ExecutionApiError(422, "record_registration_count_missing", "任务登记数量不完整，请刷新任务信息")
    return {**result, "task": deepcopy(state["task"]),
            "placement_items": [{key: image[key] for key in ("id", "root_id", "relative_path", "fingerprint", "name")}
                                for image in state.get("selected_images", [])],
            "registration_decision": {"expected_existing_register_count": count,
                                      "existing_record_action": "append" if count else "continue",
                                      "registration_cancelled": False,
                                      "selected_project_key": project["project_key"], "selected_project": project,
                                      "task": deepcopy(state["task"])}}


NATIVE_HANDLERS = {
    ("inspection.record.prepare", 1): prepare_record_input,
    ("inspection.record.validate", 1): validate_record_input,
}
