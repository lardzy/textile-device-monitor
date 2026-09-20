"""Thin native bindings for the shared regenerated-fiber services."""

from app.execution.regenerated_fiber import find_regenerated_fiber_records
from app.execution.regenerated_fiber_results import read_regenerated_fiber_records
from app.execution.v2.portable_rules import bound_rule


def find_records(context):
    config = context.node.get("config") or {}
    return find_regenerated_fiber_records(
        context.db,
        method=config["method"],
        inspection_number=context.input_data["inspection_number"],
        limit=config.get("limit", 6),
        root_id=config["root_id"],
        match_rule=config.get("match_rule"),
        project_rule=bound_rule(context) if config.get("match_rule") else None,
    )


def read_results(context):
    config = context.node.get("config") or {}
    result = read_regenerated_fiber_records(
        context.db,
        method=config["method"],
        files=context.input_data["files"],
        root_id=config["root_id"],
        run_id=context.run.id,
        node_run_id=context.node_run.id,
        preview_root_id=config["preview_root_id"],
    )
    return {
        **result,
        "items": [
            {
                "id": record["id"],
                "kind": "artifact",
                "label": record.get("name") or record["relative_path"],
                "root_id": record["root_id"],
                "relative_path": record["relative_path"],
                "fingerprint": record["fingerprint"],
                "metadata": {**record, "presentation": "result_file"},
            }
            for record in result["files"]
            if record["read_status"] == "succeeded"
        ],
    }


NATIVE_HANDLERS = {
    ("regenerated_fiber.find_records", 1): find_records,
    ("regenerated_fiber.read_results", 1): read_results,
}
