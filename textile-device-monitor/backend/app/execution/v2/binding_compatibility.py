"""Explicit handler aliases for the Bridge receipt-recovery patch.

The resource was exported from d583b9d's installed registry. This is not a
fallback for arbitrary historical bindings: only the listed, unchanged node
and Connector contracts can run on the current handler. Run snapshots stay
intact, and Workflow publication continues to freeze current bindings.
"""
from __future__ import annotations

import json
from copy import deepcopy
from importlib import resources

from app.execution.v2.canonical import canonical_sha256

RESOURCE = "compatibility/bridge-recovery-d583b9d.json"
BINDING_IDENTITY = (
    "type", "type_version", "contract_digest", "pack_id", "pack_version",
    "execution_kind", "handler_channel", "source",
)
CAPABILITY_FIELDS = (
    "execution_binding_digest", "type", "type_version", "contract_digest",
    "implementation_digest", "pack_id", "pack_version", "execution_kind", "ready",
)


def binding_digest(binding: dict) -> str:
    """Reconstruct the original exact digest, including the Connector wrapper."""
    digest = canonical_sha256({key: binding[key] for key in (
        *BINDING_IDENTITY, "implementation_digest", "distribution_digest",
    )})
    for kind in ("query", "operation"):
        connector = binding.get("connector_" + kind)
        if connector:
            digest = canonical_sha256({
                "node_binding_digest": digest,
                kind + "_contract_digest": connector["contract_digest"],
                kind + "_implementation_digest": connector["implementation_digest"],
                "connector_distribution_digest": connector["distribution_digest"],
            })
    return digest


def contract_identity(binding: dict) -> tuple:
    identity = tuple(binding[key] for key in BINDING_IDENTITY)
    for kind in ("query", "operation"):
        connector = binding.get("connector_" + kind)
        if connector:
            identity += (kind, *(connector[key] for key in (
                "connector_id", "connector_version", kind, "contract_version",
                "contract_digest", "pack_id", "pack_version",
            )))
    return identity


def load_shipped_binding_compatibility() -> dict:
    return json.loads(resources.files("app.execution.v2.resources").joinpath(RESOURCE).read_text(encoding="utf-8"))


def install_shipped_binding_compatibility(registry) -> None:
    current = [spec.execution_binding() for spec in registry.node_specs.all()
               if spec.type not in {"connector.query", "external.operation"}]
    current += registry.query_bindings + registry.operation_bindings
    current_by_contract = {contract_identity(binding): binding for binding in current}
    registry.compatibility_bindings = []
    for previous in load_shipped_binding_compatibility()["bindings"]:
        digest = previous["execution_binding_digest"]
        if binding_digest(previous) != digest:
            raise ValueError("shipped previous execution binding is damaged")
        binding = current_by_contract.get(contract_identity(previous))
        # A future contract change must retire its alias, never silently use a
        # newer input/output or external protocol for an existing Run.
        if binding is None or digest == binding["execution_binding_digest"]:
            continue
        executable = registry.executables.resolve_binding_digest(binding["execution_binding_digest"])
        if not executable.runtime_ready:
            continue
        registry.executables.index_digest(digest, executable)
        capability = {key: deepcopy(previous[key]) for key in CAPABILITY_FIELDS}
        capability["ready"] = executable.runtime_ready
        registry.compatibility_bindings.append(capability)
