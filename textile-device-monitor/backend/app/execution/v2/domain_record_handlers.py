"""Native nodes compose the same domain services as the direct APIs."""

from app.execution.electron_microscopy import (
    electron_microscopy_match,
    microscopy_image_candidates,
)
from app.execution.errors import ExecutionApiError
from app.execution.microscopy_check_record import (
    _cell_payload as check_record_cells,
    create_check_record_artifact,
)
from app.execution.microscopy_families import microscopy_family_from_config
from app.execution.microscopy_original_record import (
    _resolve_selected_images,
    _safe_inspection_number,
    create_original_record_artifact,
    original_record_cells,
    resolve_microscopy_legacy_template_binding,
)
from app.execution.paper_fiber import _paper_fiber_executor
from app.execution.v2.portable_rules import bound_rule as _bound_rule


def _selection_items(records, *, kind):
    return [{
        "id": record["id"], "kind": kind,
        "label": record.get("name") or record["relative_path"],
        "root_id": record["root_id"], "relative_path": record["relative_path"],
        "fingerprint": record["fingerprint"],
        "metadata": {**record, **({"presentation": "result_file"} if kind == "artifact" else {})},
    } for record in records]


def microscopy_candidates(context):
    config = context.node["config"]
    family = microscopy_family_from_config(config)
    match = electron_microscopy_match(
        context.db, inspection_number=context.input_data["inspection_number"],
        root_id=config["root_id"], family=family, rule_key=config["match_rule"],
        rule=_bound_rule(context),
    )
    result = microscopy_image_candidates(
        match, family=family,
        require_full_task_match=config.get("require_full_task_match", False),
    )
    return {**result, "items": _selection_items(result["images"], kind="image")}


def paper_candidates(context):
    result = _paper_fiber_executor(
        context, root_id=context.node["config"]["root_id"], rule=_bound_rule(context),
    )
    return {**result, "items": _selection_items(result["candidates"], kind="artifact")}


def original_record(context):
    config, data = context.node["config"], context.input_data
    family = microscopy_family_from_config(config)
    number = _safe_inspection_number(data["inspection_number"])
    images = _resolve_selected_images(
        context.db, root_id=config["source_root_id"],
        selected_image_ids=[image["id"] for image in data["images"]], offered_images=data["images"],
    )
    binding = resolve_microscopy_legacy_template_binding(len(images), family=family)
    cells = original_record_cells(
        data, inspection_number=number, judgement_required=data["judgement_required"], family=family,
    )
    return create_original_record_artifact(
        context.db, run_id=context.run.id, node_run_id=context.node_run.id,
        inspection_number=number, cells=cells, selected=images, template_binding=binding,
        staging_root_id=config["staging_root_id"],
    )


def check_record(context):
    config, data = context.node["config"], context.input_data
    family = microscopy_family_from_config(config)
    number = _safe_inspection_number(data["inspection_number"])
    binding = resolve_microscopy_legacy_template_binding(
        data["image_count"], declared_binding=data.get("template_binding"), family=family,
    )
    cells = check_record_cells({**data, "check_item_name": data.get("check_item_name") or family.check_item_name}, number)
    return create_check_record_artifact(
        context.db, run_id=context.run.id, node_run_id=context.node_run.id,
        inspection_number=number, image_count=data["image_count"], cells=cells,
        template_binding=binding, family=family, staging_root_id=config["staging_root_id"],
    )


NATIVE_HANDLERS = {
    ("microscopy.image_candidates", 1): microscopy_candidates,
    ("paper_fiber.find_records", 1): paper_candidates,
    ("microscopy.original_record.render", 1): original_record,
    ("microscopy.check_record.render", 1): check_record,
}
NATIVE_HANDLERS.update({(name, 2): handler for (name, version), handler in list(NATIVE_HANDLERS.items()) if name.startswith("microscopy.")})
