"""Offline adapter example. A real system supplies its own Bridge transport."""
import json
from pathlib import Path

from app.execution.adapter_packages import AdapterPackage
from app.execution.connector_queries import QueryResult
from app.execution.errors import ExecutionApiError

REFERENCE = "lab.example.record.save@1"


def echo(db, data):
    return QueryResult({"value": data["value"]})


def prepare(db, data):
    return {"schema_version": 1, "operation_type": REFERENCE,
            "source_inspection_number": data["inspection_number"], "target_sample_number": data["inspection_number"],
            "values": data["values"], "execution_capability": {"available": True}}


def verify_sources(db, operation):
    # This sample freezes values. File-backed adapters reread explicit references here.
    return None


def machine_payload(operation):
    return {"operation_id": operation.id, "payload_checksum": operation.payload_checksum,
            "values": operation.request_summary["values"]}


def verify_receipt(operation, receipt):
    if receipt.get("values") != operation.request_summary["values"]:
        raise ExecutionApiError(422, "example_readback_mismatch", "返回值与提交值不一致")
    return receipt


def reconcile(operation, attempt, action, evidence):
    if action == "confirm_completed":
        return evidence["receipt"]
    if action == "confirm_no_side_effect" and evidence.get("absent") is True:
        return None
    raise ExecutionApiError(422, "example_evidence_invalid", "缺少示例对账证据")


def register():
    root = Path(__file__).parent
    return AdapterPackage(manifest=json.loads((root / "manifest.json").read_text()), resource_root=root,
        queries={"lab.example.echo@1": echo}, operations={REFERENCE: prepare},
        source_validators={REFERENCE: verify_sources}, receipt_validators={REFERENCE: verify_receipt},
        bridge_payloads={REFERENCE: machine_payload}, reconciliation_validators={REFERENCE: reconcile})
