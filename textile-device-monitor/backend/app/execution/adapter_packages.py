"""Version 1 SDK for trusted, independently installed Connector wheels.

Discovery occurs at startup via Python package entry points. Workflow JSON never
supplies imports, paths, or code for an adapter. Restart API and Worker together
after installation; already published releases keep their exact bindings.
"""

from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import entry_points
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class AdapterPackage:
    manifest: dict
    resource_root: Path
    queries: dict[str, Callable] = field(default_factory=dict)
    operations: dict[str, Callable] = field(default_factory=dict)
    workflow_operations: dict[str, Callable] = field(default_factory=dict)
    receipt_validators: dict[str, Callable] = field(default_factory=dict)
    source_validators: dict[str, Callable] = field(default_factory=dict)
    bridge_payloads: dict[str, Callable] = field(default_factory=dict)
    reconciliation_validators: dict[str, Callable] = field(default_factory=dict)
    api_version: int = 1


@lru_cache(maxsize=1)
def installed_adapters():
    import sys
    from app.config import settings

    if settings.EXECUTION_ADAPTER_PATH:
        directory = str(Path(settings.EXECUTION_ADAPTER_PATH).resolve())
        if directory not in sys.path:
            sys.path.append(directory)
    packages = {}
    for entry in sorted(entry_points(group="textile.execution.adapters"), key=lambda item: item.name):
        package = entry.load()()
        if not isinstance(package, AdapterPackage) or package.api_version != 1:
            raise ValueError(f"Unsupported adapter API: {entry.name}")
        manifest = package.manifest
        if manifest["provides"]["nodes"] or manifest["assets"]:
            raise ValueError("Independent adapters provide Connector operations/queries only")
        if package.workflow_operations:
            raise ValueError("Independent adapters use value-based operation builders shared by API and nodes")
        for connector in manifest["provides"]["connectors"]:
            for operation in connector["operations"]:
                ref = f"{connector['connector_id']}.{operation['operation']}@{operation['contract_version']}"
                if ref not in package.operations and ref not in package.workflow_operations:
                    raise ValueError(f"Missing adapter operation: {ref}")
                for group in (package.receipt_validators, package.source_validators, package.bridge_payloads, package.reconciliation_validators):
                    if ref not in group:
                        raise ValueError(f"Missing operation lifecycle handler: {ref}")
                if operation["write_boundary"] not in operation["stages"] or operation["completion_stage"] not in operation["stages"]:
                    raise ValueError(f"Invalid operation stages: {ref}")
        identity = (manifest["pack_id"], manifest["pack_version"])
        if identity in packages:
            raise ValueError(f"Duplicate adapter package: {identity}")
        packages[identity] = package
    return packages


def package_for(manifest):
    return installed_adapters().get((manifest["pack_id"], manifest["pack_version"]))


def handler_for(manifest, group, connector_id, name, version, fallback):
    package = package_for(manifest)
    return (getattr(package, group).get(f"{connector_id}.{name}@{version}") if package else fallback(connector_id, name, version))


def bound_adapter(operation):
    """Require the precise installed implementation that prepared an operation."""
    if getattr(operation, "connector_key", None) is None:
        return None
    from app.execution.v2.registry import get_installed_registry, resolve_connector_reference
    from app.execution.errors import ExecutionApiError

    submission = (operation.request_summary or {}).get("connector_submission") or {}
    if operation.connector_key == "legacy_fibrecheck" and not submission.get("adapter_package"):
        return None
    registry = get_installed_registry()
    try:
        connector, name, version = resolve_connector_reference(submission["operation_ref"], [operation.connector_key])
        contract = registry.resolve_operation(connector, submission["connector_version"], name, version, submission["contract_digest"])
        if contract.implementation_digest != submission["implementation_digest"]:
            raise ValueError("adapter implementation changed")
        package = installed_adapters().get((contract.pack_id, contract.pack_version))
        if package is None and operation.connector_key == "legacy_fibrecheck":
            return None
        if package is None:
            raise LookupError("adapter package missing")
    except (LookupError, ValueError, KeyError) as exc:
        raise ExecutionApiError(409, "adapter_binding_unavailable", "该操作绑定的适配器版本未安装") from exc
    return package, contract


def credential_system(connector_id):
    return "legacy_inspection" if connector_id == "legacy_fibrecheck" else connector_id


def binding_fields(contract):
    return {"connector_id": contract.connector_id, "implementation_digest": contract.implementation_digest,
            "adapter_package": (contract.pack_id, contract.pack_version) in installed_adapters()}


def seal_manifest(manifest, root):
    """Build-time helper; the resulting manifest is shipped in the wheel."""
    from copy import deepcopy
    from app.execution.v2.canonical import canonical_sha256, resource_set_digest

    value = deepcopy(manifest)
    value.pop("distribution_digest", None)
    digest = canonical_sha256({"manifest": value, "resources_digest": resource_set_digest(
        [(path, (Path(root) / path).read_bytes()) for path in value["resource_paths"]])})
    return {**value, "distribution_digest": digest}
