"""Execution v2 declarative contracts and installed capability registry.

This package is intentionally read-only at runtime.  Workflow Release JSON can
resolve capabilities that were installed with the application, but it cannot
install code, import executors, or fetch resources from the network.
"""

from app.execution.v2.registry import (
    executable_binding_for,
    get_installed_registry,
    legacy_operation_ref_for_node,
    list_assets,
    list_connectors,
    list_node_specs,
    list_packs,
    registry_revision,
    resolve_asset,
    resolve_connector,
    resolve_node_spec,
    resolve_operation,
    resolve_pack,
    worker_capability_document,
)

__all__ = [
    "executable_binding_for",
    "get_installed_registry",
    "legacy_operation_ref_for_node",
    "list_assets",
    "list_connectors",
    "list_node_specs",
    "list_packs",
    "registry_revision",
    "resolve_asset",
    "resolve_connector",
    "resolve_node_spec",
    "resolve_operation",
    "resolve_pack",
    "worker_capability_document",
]
