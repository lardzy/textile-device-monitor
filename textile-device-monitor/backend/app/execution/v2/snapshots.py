"""Deterministic P0 contract snapshot generation.

The generator uses an in-memory SQLite database and package resources only.
It never builds a FileGateway, opens a CIFS/UNO/Bridge connection, or invokes
an external-operation preparer/executor.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from app.database import Base
from app.execution.catalog import ensure_default_catalog
from app.execution.models import (
    ExecutionArtifact,
    ExecutionExternalOperation,
    ExecutionFileMutation,
    ExecutionHumanTask,
    ExecutionWorkflow,
)
from app.execution.registry import node_registry
from app.execution.v2.canonical import canonical_sha256
from app.execution.v2.registry import (
    LEGACY_NODE_OPERATION_REFS,
    get_installed_registry,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

BASELINE_COMMIT = "d1fb01bbd12e3b020f1e27a2ecbeab20bf039ac4"


def _column_contract(model: type[Any]) -> list[dict[str, Any]]:
    values = []
    for column in model.__table__.columns:
        values.append(
            {
                "name": column.name,
                "nullable": bool(column.nullable),
                "primary_key": bool(column.primary_key),
                "type": str(column.type),
            }
        )
    return values


def _new_install_workflows() -> list[dict[str, Any]]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    try:
        with Session(engine) as db:
            ensure_default_catalog(db)
            db.flush()
            rows = (
                db.query(ExecutionWorkflow).order_by(ExecutionWorkflow.slug.asc()).all()
            )
            values = []
            for workflow in rows:
                published = next(
                    version
                    for version in workflow.versions
                    if version.version_number == workflow.published_version_number
                )
                values.append(
                    {
                        "slug": workflow.slug,
                        "name": workflow.name,
                        "is_enabled": workflow.is_enabled,
                        "published_version_number": (workflow.published_version_number),
                        "capabilities": deepcopy(published.capabilities),
                        "checksum": published.checksum,
                        "contract_checksum": published.contract_checksum,
                        "definition": deepcopy(published.definition),
                    }
                )
            if len(values) != 9:
                raise ValueError(
                    f"new-install catalog must contain 9 workflows, got {len(values)}"
                )
            return values
    finally:
        engine.dispose()


def _node_contracts() -> list[dict[str, Any]]:
    installed = get_installed_registry()
    by_identity = {
        (spec.type, spec.type_version): spec for spec in installed.list_node_specs()
    }
    values = []
    for node_type in sorted(node_registry.all(), key=lambda item: item.type):
        public = node_type.public_dict()
        spec = by_identity[(node_type.type, node_type.version)]
        values.append(
            {
                "registry_fact": public,
                "registry_fact_digest": canonical_sha256(public),
                "compatibility_nodespec": {
                    "contract_digest": spec.contract_digest,
                    "implementation_digest": spec.implementation_digest,
                    "pack_id": spec.pack_id,
                    "pack_version": spec.pack_version,
                },
            }
        )
    if len(values) != 39:
        raise ValueError(f"node registry must contain 39 entries, got {len(values)}")
    return values


def _external_operation_contracts() -> list[dict[str, Any]]:
    registry = get_installed_registry()
    connector = registry.resolve_connector(
        "legacy_fibrecheck",
        ">=1.0.0 <2.0.0",
    )
    operation_type_by_ref = {
        operation_ref: node_type.removeprefix("external.")
        for node_type, operation_ref in LEGACY_NODE_OPERATION_REFS.items()
    }
    values = []
    for operation in connector.operations:
        operation_ref = (
            f"{operation.connector_id}.{operation.operation}"
            f"@{operation.contract_version}"
        )
        operation_type = operation_type_by_ref[operation_ref]
        stages = tuple(operation.spec["stages"])
        write_boundary = operation.spec["write_boundary"]
        completion_stage = operation.spec["completion_stage"]
        receipt_fixture: dict[str, Any] = {
            "schema_version": 1,
            "receipt_type": operation_type,
            "operation_id": "00000000-0000-0000-0000-000000000001",
            "payload_checksum": "0" * 64,
            "stages": [{"name": stage} for stage in stages],
            "reconciliation_required": False,
        }
        if operation_type in {
            "legacy_special_wool_image_upload",
            "legacy_special_wool_qualitative_upload",
        }:
            receipt_fixture.update(
                {
                    "picture_records": [],
                    "picture_count": 0,
                    "completion_stage": "main_record_verified",
                }
            )
        if operation_type in {
            "legacy_special_wool_review",
            "legacy_special_wool_qualitative_review",
        }:
            receipt_fixture["picture_count"] = 0
        values.append(
            {
                "operation_ref": operation_ref,
                "contract_digest": operation.contract_digest,
                "request_fixture": {
                    "schema_version": 1,
                    "operation_type": operation_type,
                    "source_inspection_number": "TEST-0001",
                    "target_sample_number": "TEST-0001",
                    "safety": {
                        "remote_write_performed": False,
                        "requires_source_reverification": True,
                        "overwrite_allowed": False,
                    },
                },
                "stage_contract": {
                    "stages": list(stages),
                    "write_boundary": write_boundary,
                    "completion_stage": completion_stage,
                },
                "receipt_fixture": receipt_fixture,
                "reconciliation_fixture": {
                    "status": "reconciliation_required",
                    "last_stage": write_boundary,
                    "error_code": "external_result_unknown_after_remote_write",
                    "automatic_retry_allowed": False,
                },
            }
        )
    if len(values) != 7:
        raise ValueError(
            f"legacy connector must contain 7 operations, got {len(values)}"
        )
    return sorted(values, key=lambda item: item["operation_ref"])


def build_contract_snapshot() -> dict[str, Any]:
    workflows = _new_install_workflows()
    snapshot = {
        "snapshot_schema_version": "1.0",
        "baseline_commit": BASELINE_COMMIT,
        "normalization": {
            "allowed": ["uuid", "timestamp", "temporary_path"],
            "forbidden": [
                "node_id",
                "business_field",
                "digest",
                "error_code",
                "side_effect_stage",
            ],
        },
        "nodes": _node_contracts(),
        "new_install_workflows": workflows,
        "workflow_inventory_digest": canonical_sha256(workflows),
        "durable_records": {
            "human_task": {
                "columns": _column_contract(ExecutionHumanTask),
                "receipt_fixture": {
                    "status": "completed",
                    "task_kind": "confirm",
                    "result": {"approved": True},
                },
            },
            "artifact": {
                "columns": _column_contract(ExecutionArtifact),
                "receipt_fixture": {
                    "root_id": "execution_staging",
                    "relative_path": "runs/RUN/artifacts/example.xls",
                    "sha256": "0" * 64,
                },
            },
            "mutation": {
                "columns": _column_contract(ExecutionFileMutation),
                "receipt_fixture": {
                    "status": "verified",
                    "source_sha256": "0" * 64,
                    "working_sha256": "1" * 64,
                    "verified": True,
                },
            },
            "external_operation": {
                "columns": _column_contract(ExecutionExternalOperation),
                "contracts": _external_operation_contracts(),
            },
        },
    }
    snapshot["snapshot_digest"] = canonical_sha256(snapshot)
    return snapshot


def render_contract_snapshot() -> bytes:
    return (
        json.dumps(
            build_contract_snapshot(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
