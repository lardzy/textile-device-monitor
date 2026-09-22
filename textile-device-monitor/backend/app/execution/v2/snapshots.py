"""Deterministic snapshot of the installed native contracts, with no database or IO."""
import json
from app.execution.v2.registry import get_installed_registry


def render_contract_snapshot():
    registry = get_installed_registry()
    document = {
        'schema_version': 2,
        'automatic_workflow_seeds': [],
        'nodes': [spec.public_dict() for spec in registry.list_node_specs()],
        'connectors': registry.list_connectors(),
        'packs': [pack.public_dict() for pack in registry.packs.all()],
    }
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)+'\n').encode()
