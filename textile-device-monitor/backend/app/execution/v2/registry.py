"""Installed Execution v2 capability registry.

The loader consumes only resources shipped in ``app.execution``.  It never
honours module names, executor paths, commands, URLs, or installation requests
from Workflow Release JSON.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import cache, lru_cache
from importlib import resources
from typing import Any

from app.execution.registry import NodeType, node_registry
from app.execution.v2.canonical import (
    bytes_sha256,
    canonical_sha256,
    resource_set_digest,
    select_highest_stable,
    semver_matches,
)
from jsonschema import Draft202012Validator

ENGINE_VERSION = "2.1.0"
PROTOCOL_VERSION = "2.1"

_TRUSTED_RENDERER_PROTOCOLS = {
    ("human.form", "1.0.0"): "native.form.v1",
    ("human.select", "1.0.0"): "native.select.v1",
    ("human.approval", "1.0.0"): "native.approval.v1",
    ("human.decision", "1.0.0"): "native.decision.v1",
    ("file.batch_place.conflict", "1.0.0"): "retry_with_decision.v1",
}


def _trusted_renderer_contracts() -> dict[tuple[str, str], dict[str, Any]]:
    return {
        identity: {
            "capability": identity[0],
            "version": identity[1],
            "protocol": protocol,
            "contract_digest": canonical_sha256(
                {
                    "capability": identity[0],
                    "version": identity[1],
                    "protocol": protocol,
                }
            ),
        }
        for identity, protocol in _TRUSTED_RENDERER_PROTOCOLS.items()
    }

LEGACY_NODE_OPERATION_REFS = {
    "external.legacy_regenerated_fiber_count_upload": (
        "legacy_fibrecheck.regenerated_fiber.count_upload@1"
    ),
    "external.legacy_special_wool_image_upload": (
        "legacy_fibrecheck.special_wool.image_upload@1"
    ),
    "external.legacy_special_wool_review": (
        "legacy_fibrecheck.special_wool.image_review@1"
    ),
    "external.legacy_microscopy_check_record_entry": (
        "legacy_fibrecheck.microscopy.check_record_entry@1"
    ),
    "external.legacy_special_wool_qualitative_upload": (
        "legacy_fibrecheck.paper_fiber.qualitative_upload@1"
    ),
    "external.legacy_special_wool_qualitative_review": (
        "legacy_fibrecheck.paper_fiber.qualitative_review@1"
    ),
    "external.legacy_generic_check_record_entry": (
        "legacy_fibrecheck.check_record.generic_entry@1"
    ),
}


@dataclass(frozen=True)
class InstalledPack:
    pack_id: str
    pack_version: str
    engine_version_range: str
    distribution_digest: str
    ready: bool
    manifest: dict[str, Any]

    @property
    def version(self) -> str:
        return self.pack_version

    def public_dict(self) -> dict[str, Any]:
        provides = deepcopy(self.manifest["provides"])
        return {
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "engine_version_range": self.engine_version_range,
            "distribution_digest": self.distribution_digest,
            "ready": self.ready,
            "provides": provides,
        }


@dataclass(frozen=True)
class InstalledAsset:
    asset_id: str
    kind: str
    version: str
    digest: str
    media_type: str
    size_bytes: int
    resource_path: str
    pack_id: str
    pack_version: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "kind": self.kind,
            "version": self.version,
            "digest": self.digest,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "registry_key": f"{self.pack_id}/{self.asset_id}@{self.version}",
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "resource_path": self.resource_path,
        }


@dataclass(frozen=True)
class InstalledOperation:
    connector_id: str
    connector_version: str
    operation: str
    contract_version: int
    contract_digest: str
    pack_id: str
    pack_version: str
    distribution_digest: str
    spec: dict[str, Any]

    def public_dict(self) -> dict[str, Any]:
        value = deepcopy(self.spec)
        value.update(
            {
                "connector_id": self.connector_id,
                "connector_version": self.connector_version,
                "contract_digest": self.contract_digest,
                "pack_id": self.pack_id,
                "pack_version": self.pack_version,
                "distribution_digest": self.distribution_digest,
            }
        )
        return value


@dataclass(frozen=True)
class InstalledConnector:
    connector_id: str
    version: str
    distribution_digest: str
    pack_id: str
    pack_version: str
    operations: tuple[InstalledOperation, ...]
    queries: tuple[dict[str, Any], ...]
    ready: bool = True

    def public_dict(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "version": self.version,
            "distribution_digest": self.distribution_digest,
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "ready": self.ready,
            "operations": [item.public_dict() for item in self.operations],
            "queries": [deepcopy(item) for item in self.queries],
        }


@dataclass(frozen=True)
class InstalledExecutable:
    node_type: str
    type_version: int
    contract_digest: str
    implementation_digest: str
    handler_channel: str
    installed_ready: bool
    runtime_ready: bool
    source: str
    handler: Callable[[Any], Any] | None = None


@dataclass(frozen=True)
class InstalledNodeSpec:
    type: str
    type_version: int
    contract_digest: str
    implementation_digest: str
    pack_id: str
    pack_version: str
    distribution_digest: str
    execution: dict[str, Any]
    config_schema: dict[str, Any] | bool
    input_schema: dict[str, Any] | bool
    output_schema: dict[str, Any] | bool
    side_effect_class: str
    publishable: bool
    ready: bool
    spec: dict[str, Any]
    handler_channel: str
    source: str
    preferred: bool

    def public_dict(self) -> dict[str, Any]:
        value = deepcopy(self.spec)
        value.update(
            {
                "contract_digest": self.contract_digest,
                "implementation_digest": self.implementation_digest,
                "pack_id": self.pack_id,
                "pack_version": self.pack_version,
                "distribution_digest": self.distribution_digest,
                "publishable": self.publishable,
                "ready": self.ready,
                "source": self.source,
                "preferred": self.preferred,
            }
        )
        return value

    def execution_binding(self) -> dict[str, Any]:
        renderer = deepcopy(
            (self.spec.get("ui_schema") or {}).get("renderer") or {}
        )
        if renderer:
            renderer = deepcopy(
                _trusted_renderer_contracts().get(
                    (
                        str(renderer.get("capability") or ""),
                        str(renderer.get("version") or ""),
                    ),
                    {},
                )
            )
        binding = {
            "type": self.type,
            "type_version": self.type_version,
            "contract_digest": self.contract_digest,
            "implementation_digest": self.implementation_digest,
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "distribution_digest": self.distribution_digest,
            "execution_kind": self.execution["kind"],
            "side_effect_class": self.side_effect_class,
            "handler_channel": self.handler_channel,
            "source": self.source,
            "preferred": self.preferred,
            "config_schema": deepcopy(self.config_schema),
            "input_schema": deepcopy(self.input_schema),
            "output_schema": deepcopy(self.output_schema),
            "schema_bindings": deepcopy(self.spec.get("schema_bindings") or {}),
            "ports": deepcopy(self.spec.get("ports") or {}),
            "requirements": deepcopy(self.spec.get("requirements") or {}),
            "suspension": deepcopy(self.spec.get("suspension")),
            "renderer_contract": renderer,
            "lifecycle": deepcopy(self.spec.get("lifecycle") or {}),
            "extensions": deepcopy(self.spec.get("extensions") or {}),
            "publishable": self.publishable,
            "ready": self.ready,
        }
        digest_fields = {
            key: binding[key]
            for key in (
                "type",
                "type_version",
                "contract_digest",
                "implementation_digest",
                "pack_id",
                "pack_version",
                "distribution_digest",
                "execution_kind",
                "handler_channel",
                "source",
            )
        }
        binding["execution_binding_digest"] = canonical_sha256(digest_fields)
        return binding


class PackRegistry:
    def __init__(self, packs: Iterable[InstalledPack]) -> None:
        self._by_id: dict[str, list[InstalledPack]] = {}
        for pack in packs:
            versions = self._by_id.setdefault(pack.pack_id, [])
            if any(item.pack_version == pack.pack_version for item in versions):
                raise ValueError(
                    f"duplicate pack version: {pack.pack_id}@{pack.pack_version}"
                )
            versions.append(pack)

    def all(self) -> list[InstalledPack]:
        return sorted(
            (pack for versions in self._by_id.values() for pack in versions),
            key=lambda item: (item.pack_id, item.pack_version),
        )

    def resolve(
        self,
        pack_id: str,
        version_range: str,
        distribution_digest: str | None = None,
    ) -> InstalledPack:
        candidates = list(self._by_id.get(pack_id, ()))
        if distribution_digest is not None:
            candidates = [
                item
                for item in candidates
                if item.distribution_digest == distribution_digest
            ]
        try:
            selected = select_highest_stable(candidates, version_range)
        except (LookupError, RuntimeError, ValueError) as exc:
            raise LookupError(
                f"pack dependency unresolved: {pack_id} {version_range}"
            ) from exc
        assert isinstance(selected, InstalledPack)
        return selected


class NodeSpecRegistry:
    def __init__(self, specs: Iterable[InstalledNodeSpec]) -> None:
        self._by_identity: dict[tuple[str, int], dict[str, InstalledNodeSpec]] = {}
        self._owners: dict[tuple[str, int], str] = {}
        self._preferred: dict[tuple[str, int], InstalledNodeSpec] = {}
        for spec in specs:
            identity = (spec.type, spec.type_version)
            previous_owner = self._owners.setdefault(identity, spec.pack_id)
            if previous_owner != spec.pack_id:
                raise ValueError(f"node identity has multiple owners: {identity}")
            versions = self._by_identity.setdefault(identity, {})
            if spec.contract_digest in versions:
                raise ValueError(
                    f"duplicate NodeSpec contract: {identity} {spec.contract_digest}"
                )
            versions[spec.contract_digest] = spec
            if spec.preferred:
                if identity in self._preferred:
                    raise ValueError(
                        f"node identity has multiple preferred contracts: {identity}"
                    )
                self._preferred[identity] = spec
        for identity, contracts in self._by_identity.items():
            if len(contracts) > 1 and identity not in self._preferred:
                raise ValueError(
                    f"node identity requires one preferred contract: {identity}"
                )

    def all(self) -> list[InstalledNodeSpec]:
        return sorted(
            (
                spec
                for contracts in self._by_identity.values()
                for spec in contracts.values()
            ),
            key=lambda item: (item.type, item.type_version, item.contract_digest),
        )

    def resolve(
        self,
        node_type: str,
        type_version: int,
        contract_digest: str | None = None,
    ) -> InstalledNodeSpec:
        contracts = self._by_identity.get((node_type, type_version))
        if not contracts:
            raise LookupError(f"NodeSpec not installed: {node_type}@{type_version}")
        if contract_digest is not None:
            spec = contracts.get(contract_digest)
            if spec is None:
                raise LookupError(
                    f"NodeSpec contract not installed: {node_type}@{type_version} {contract_digest}"
                )
            return spec
        if len(contracts) == 1:
            return next(iter(contracts.values()))
        return self._preferred[(node_type, type_version)]


class ExecutableRegistry:
    def __init__(self, executables: Iterable[InstalledExecutable]) -> None:
        self._items: dict[tuple[str, int, str], InstalledExecutable] = {}
        self._by_binding_digest: dict[str, InstalledExecutable] = {}
        for executable in executables:
            key = (
                executable.node_type,
                executable.type_version,
                executable.contract_digest,
            )
            if key in self._items:
                raise ValueError(f"duplicate executable binding: {key}")
            self._items[key] = executable

    def resolve(self, spec: InstalledNodeSpec) -> InstalledExecutable:
        key = (spec.type, spec.type_version, spec.contract_digest)
        try:
            return self._items[key]
        except KeyError as exc:
            raise LookupError(f"executable binding not installed: {key}") from exc

    def index_binding(
        self, spec: InstalledNodeSpec, executable: InstalledExecutable
    ) -> None:
        digest = spec.execution_binding()["execution_binding_digest"]
        existing = self._by_binding_digest.get(digest)
        if existing is not None and existing != executable:
            raise ValueError(f"duplicate execution binding digest: {digest}")
        self._by_binding_digest[digest] = executable

    def resolve_binding_digest(self, digest: str) -> InstalledExecutable:
        try:
            return self._by_binding_digest[digest]
        except KeyError as exc:
            raise LookupError(
                f"execution binding is not installed: {digest}"
            ) from exc


class AssetRegistry:
    def __init__(self, assets: Iterable[InstalledAsset]) -> None:
        self._items: dict[tuple[str, str], InstalledAsset] = {}
        for asset in assets:
            key = (asset.asset_id, asset.version)
            if key in self._items:
                raise ValueError(f"duplicate asset: {key}")
            self._items[key] = asset

    def all(self) -> list[InstalledAsset]:
        return sorted(
            self._items.values(), key=lambda item: (item.asset_id, item.version)
        )

    def resolve(self, asset_id: str, version: str) -> InstalledAsset:
        try:
            return self._items[(asset_id, version)]
        except KeyError as exc:
            raise LookupError(f"asset not installed: {asset_id}@{version}") from exc


class ConnectorRegistry:
    def __init__(self, connectors: Iterable[InstalledConnector]) -> None:
        self._by_id: dict[str, list[InstalledConnector]] = {}
        self._operations: dict[tuple[str, str, str, int], InstalledOperation] = {}
        for connector in connectors:
            versions = self._by_id.setdefault(connector.connector_id, [])
            if any(item.version == connector.version for item in versions):
                raise ValueError(
                    f"duplicate connector version: {connector.connector_id}@{connector.version}"
                )
            versions.append(connector)
            for operation in connector.operations:
                key = (
                    connector.connector_id,
                    connector.version,
                    operation.operation,
                    operation.contract_version,
                )
                if key in self._operations:
                    raise ValueError(f"duplicate Connector OperationSpec: {key}")
                self._operations[key] = operation

    def all(self) -> list[InstalledConnector]:
        return sorted(
            (item for versions in self._by_id.values() for item in versions),
            key=lambda item: (item.connector_id, item.version),
        )

    def resolve(
        self,
        connector_id: str,
        version_range: str,
        distribution_digest: str | None = None,
    ) -> InstalledConnector:
        candidates = list(self._by_id.get(connector_id, ()))
        if distribution_digest is not None:
            candidates = [
                item
                for item in candidates
                if item.distribution_digest == distribution_digest
            ]
        try:
            selected = select_highest_stable(candidates, version_range)
        except (LookupError, RuntimeError, ValueError) as exc:
            raise LookupError(
                f"connector dependency unresolved: {connector_id} {version_range}"
            ) from exc
        assert isinstance(selected, InstalledConnector)
        return selected

    def resolve_operation(
        self,
        connector_id: str,
        version_range: str,
        operation: str,
        contract_version: int,
        contract_digest: str | None = None,
    ) -> InstalledOperation:
        connector = self.resolve(connector_id, version_range)
        key = (connector_id, connector.version, operation, contract_version)
        try:
            value = self._operations[key]
        except KeyError as exc:
            raise LookupError(
                f"Connector OperationSpec not installed: {connector_id}.{operation}@{contract_version}"
            ) from exc
        if contract_digest is not None and value.contract_digest != contract_digest:
            raise LookupError(
                f"Connector OperationSpec digest mismatch: {connector_id}.{operation}@{contract_version}"
            )
        return value


class InstalledRegistry:
    def __init__(
        self,
        *,
        packs: PackRegistry,
        node_specs: NodeSpecRegistry,
        executables: ExecutableRegistry,
        assets: AssetRegistry,
        connectors: ConnectorRegistry,
    ) -> None:
        self.packs = packs
        self.node_specs = node_specs
        self.executables = executables
        self.assets = assets
        self.connectors = connectors
        for spec in node_specs.all():
            executables.index_binding(spec, executables.resolve(spec))
        self.revision = canonical_sha256(
            {
                "engine_version": ENGINE_VERSION,
                "packs": [
                    {
                        "pack_id": item.pack_id,
                        "pack_version": item.pack_version,
                        "distribution_digest": item.distribution_digest,
                    }
                    for item in packs.all()
                ],
                "node_specs": [
                    {
                        "type": item.type,
                        "type_version": item.type_version,
                        "contract_digest": item.contract_digest,
                        "implementation_digest": item.implementation_digest,
                    }
                    for item in node_specs.all()
                ],
                "assets": [item.public_dict() for item in assets.all()],
                "connectors": [item.public_dict() for item in connectors.all()],
            }
        )

    def list_packs(self) -> list[dict[str, Any]]:
        return [item.public_dict() for item in self.packs.all()]

    def list_assets(self) -> list[dict[str, Any]]:
        return [item.public_dict() for item in self.assets.all()]

    def list_connectors(self) -> list[dict[str, Any]]:
        return [item.public_dict() for item in self.connectors.all()]

    def list_node_specs(self) -> list[InstalledNodeSpec]:
        return self.node_specs.all()

    def resolve_node_spec(
        self,
        node_type: str,
        type_version: int,
        contract_digest: str | None = None,
    ) -> InstalledNodeSpec:
        return self.node_specs.resolve(node_type, type_version, contract_digest)

    def resolve_pack(
        self,
        pack_id: str,
        version_range: str,
        distribution_digest: str | None = None,
    ) -> InstalledPack:
        return self.packs.resolve(pack_id, version_range, distribution_digest)

    def resolve_connector(
        self,
        connector_id: str,
        version_range: str,
        distribution_digest: str | None = None,
    ) -> InstalledConnector:
        return self.connectors.resolve(
            connector_id,
            version_range,
            distribution_digest,
        )

    def resolve_operation(
        self,
        connector_id: str,
        version_range: str,
        operation: str,
        contract_version: int,
        contract_digest: str | None = None,
    ) -> InstalledOperation:
        return self.connectors.resolve_operation(
            connector_id,
            version_range,
            operation,
            contract_version,
            contract_digest,
        )

    def executable_binding_for(
        self,
        node_type: str,
        type_version: int,
        contract_digest: str | None = None,
    ) -> dict[str, Any]:
        spec = self.resolve_node_spec(node_type, type_version, contract_digest)
        executable = self.executables.resolve(spec)
        return replace(spec, ready=executable.installed_ready).execution_binding()

    def handler_for_binding(self, execution_binding_digest: str) -> Callable[[Any], Any] | None:
        executable = self.executables.resolve_binding_digest(
            execution_binding_digest
        )
        if not executable.runtime_ready:
            raise LookupError(
                f"execution binding handler is not ready: {execution_binding_digest}"
            )
        return executable.handler

    def worker_capability_document(self) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        for spec in self.node_specs.all():
            executable = self.executables.resolve(spec)
            runtime_ready = executable.runtime_ready
            if (
                executable.source == "v1_registry_adapter"
                and executable.handler_channel == "worker_callable"
            ):
                runtime_ready = (
                    node_registry.executor(spec.type, spec.type_version)
                    is not None
                )
            binding = replace(spec, ready=runtime_ready).execution_binding()
            nodes.append(
                {
                    key: binding[key]
                    for key in (
                        "execution_binding_digest",
                        "type",
                        "type_version",
                        "contract_digest",
                        "implementation_digest",
                        "pack_id",
                        "pack_version",
                        "execution_kind",
                        "ready",
                    )
                }
            )
        capability_digest = canonical_sha256(nodes)
        return {
            "protocol_version": PROTOCOL_VERSION,
            "engine_version": ENGINE_VERSION,
            "capability_digest": capability_digest,
            "nodes": nodes,
        }


def _load_json_resource(path: str) -> dict[str, Any]:
    value = json.loads(
        resources.files("app.execution.v2.resources")
        .joinpath(path)
        .read_text(encoding="utf-8")
    )
    if not isinstance(value, dict):
        raise TypeError(f"resource must contain a JSON object: {path}")
    return value


@cache
def load_schema(name: str) -> dict[str, Any]:
    if name not in {
        "workflow-release-v2.schema.json",
        "node-spec-v2.schema.json",
        "pack-manifest-v2.schema.json",
    }:
        raise LookupError(f"unknown execution v2 schema: {name}")
    schema = _load_json_resource(f"schemas/{name}")
    Draft202012Validator.check_schema(schema)
    return schema


def _manifest_resources(manifest: dict[str, Any]) -> list[tuple[str, bytes]]:
    package_root = resources.files("app.execution")
    values: list[tuple[str, bytes]] = []
    for path in manifest["resource_paths"]:
        normalized = path.replace("\\", "/")
        if (
            normalized != path
            or path.startswith("/")
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise ValueError(f"manifest resource path is not portable: {path!r}")
        target = package_root.joinpath(path)
        if not target.is_file():
            raise ValueError(f"manifest resource is missing: {path}")
        values.append((path, target.read_bytes()))
    return values


def _manifest_distribution_digest(manifest: dict[str, Any]) -> str:
    document = deepcopy(manifest)
    declared = document.pop("distribution_digest", None)
    digest = canonical_sha256(
        {
            "manifest": document,
            "resources_digest": resource_set_digest(_manifest_resources(manifest)),
        }
    )
    if declared is not None and declared != digest:
        raise ValueError(
            f"pack distribution digest mismatch: {manifest['pack_id']}@{manifest['pack_version']}"
        )
    return digest


def _side_effect_class(node_type: NodeType) -> str:
    if node_type.execution_kind == "external_side_effect":
        return "external_write"
    if node_type.type == "artifact.publish":
        return "durable_write"
    if node_type.type in {
        "workbook.copy",
        "workbook.write_cells",
        "workbook.microscopy_original_record",
        "workbook.microscopy_check_record",
    }:
        return "reversible_local_write"
    if node_type.execution_kind == "human":
        return "none"
    if node_type.type in {
        "core.start",
        "core.end",
        "variables.set",
        "branch.condition",
        "parallel.split",
        "parallel.join",
        "result.aggregate",
        "external.legacy_inspection",
        "external.new_inspection",
    }:
        return "none"
    return "read_only"


def _portable_config_key(key: str) -> str:
    if key == "root_id":
        return "root_slot"
    if key.endswith("_root_id"):
        return f"{key[: -len('_root_id')]}_root_slot"
    if key == "match_rule":
        return "rule_slot"
    if key == "candidate_role":
        return "candidate_role_slot"
    return key


def _requirements(node_type: NodeType) -> dict[str, Any]:
    properties = (
        node_type.config_schema.get("properties", {})
        if isinstance(node_type.config_schema, dict)
        else {}
    )
    required = set(node_type.required_config)
    root_slots = []
    credential_slots = []
    rule_slots = []
    for key in sorted(properties):
        if key != "root_id" and not key.endswith("_root_id"):
            continue
        portable_key = _portable_config_key(key)
        root_slots.append(
            {
                "config_pointer": f"/{portable_key}",
                "access": (
                    "publish"
                    if "publish" in key or node_type.type == "artifact.publish"
                    else "write"
                    if "staging" in key
                    or _side_effect_class(node_type) == "reversible_local_write"
                    else "read"
                ),
                "required": key in required,
            }
        )
    if "credential_slot" in properties:
        credential_slots.append(
            {
                "config_pointer": "/credential_slot",
                "required": "credential_slot" in required,
            }
        )
    if "match_rule" in properties:
        rule_slots.append(
            {
                "config_pointer": "/rule_slot",
                "required": "match_rule" in required,
            }
        )
    role_slots = []
    if "candidate_role" in properties:
        role_slots.append(
            {
                "config_pointer": "/candidate_role_slot",
                "required": "candidate_role" in required,
            }
        )
    return {
        "resources": {
            "root_slots": root_slots,
            "credential_slots": credential_slots,
            "role_slots": role_slots,
            "rule_slots": rule_slots,
        },
        "capabilities": [],
        "connectors": (
            [{"operation_ref": LEGACY_NODE_OPERATION_REFS[node_type.type]}]
            if node_type.type in LEGACY_NODE_OPERATION_REFS
            else []
        ),
        "assets": [],
    }


def _ports(node_type: NodeType) -> dict[str, Any]:
    default_port = {
        "id": "in",
        "min_edges": 0,
        "max_edges": None,
        "default": True,
    }
    output_port = {**default_port, "id": "out"}
    return {
        "inputs": [] if node_type.type == "core.start" else [default_port],
        "outputs": [] if node_type.type == "core.end" else [output_port],
        "condition_mode": (
            "edge_conditions" if node_type.type == "branch.condition" else "none"
        ),
    }


def _portable_config_schema(node_type: NodeType) -> dict[str, Any] | bool:
    schema = deepcopy(node_type.config_schema)
    if not isinstance(schema, dict):
        return schema
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema
    portable_properties: dict[str, Any] = {}
    for key, value in properties.items():
        portable_key = _portable_config_key(key)
        portable_value = deepcopy(value)
        if key == "root_id" or key.endswith("_root_id"):
            if isinstance(portable_value, dict):
                portable_value.pop("const", None)
                portable_value["pattern"] = r"^[a-z][a-z0-9_-]{0,63}$"
                portable_value["description"] = (
                    "Portable root slot name; deployment projection resolves "
                    "it to the v1 root id"
                )
        elif key in {"match_rule", "candidate_role"} and isinstance(
            portable_value, dict
        ):
            portable_value.pop("const", None)
            portable_value.pop("x-param-type", None)
            portable_value["pattern"] = r"^[a-z][a-z0-9_-]{0,63}$"
            portable_value["description"] = "Portable deployment resource slot name"
        portable_properties[portable_key] = portable_value
    schema["properties"] = portable_properties
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [_portable_config_key(item) for item in required]
    return schema


def _compat_node_spec(
    node_type: NodeType,
    *,
    pack: InstalledPack,
    handler_channel: str,
) -> dict[str, Any]:
    side_effect = _side_effect_class(node_type)
    if side_effect == "external_write":
        side_effect_contract = {
            "class": side_effect,
            "idempotency": "operation_fence",
            "cancellation": "fenced",
            "receipt": "required",
            "unknown_outcome": "reconciliation_required",
        }
    elif side_effect == "durable_write":
        side_effect_contract = {
            "class": side_effect,
            "idempotency": "business_key",
            "cancellation": "fenced",
            "receipt": "required",
            "unknown_outcome": "reconciliation_required",
        }
    elif side_effect == "reversible_local_write":
        side_effect_contract = {
            "class": side_effect,
            "idempotency": "engine_key",
            "cancellation": "cooperative",
            "receipt": "optional",
            "unknown_outcome": "fail",
        }
    else:
        side_effect_contract = {
            "class": side_effect,
            "idempotency": "none",
            "cancellation": "immediate",
            "receipt": "none",
            "unknown_outcome": "fail",
        }
    suspension = None
    if node_type.execution_kind == "human":
        suspension = {"kind": "human_task", "resume_protocol": "v1-compat"}
    elif node_type.execution_kind == "external_side_effect":
        suspension = {
            "kind": "external_operation",
            "resume_protocol": "v1-compat",
        }
    owner_kind = (
        "kernel" if pack.pack_id == "textile.execution-kernel" else "capability_pack"
    )
    return {
        "schema_version": "2.0",
        "type": node_type.type,
        "type_version": node_type.version,
        "name": node_type.name,
        "category": node_type.category,
        "description": node_type.description,
        "execution": {
            "kind": node_type.execution_kind,
            "owner": {"kind": owner_kind, "pack_id": pack.pack_id},
            "result_mode": "object",
        },
        "config_schema": _portable_config_schema(node_type),
        "input_schema": deepcopy(node_type.input_schema),
        "output_schema": deepcopy(node_type.output_schema),
        "schema_bindings": {
            "input": {"source": "node_spec"},
            "output": {"source": "node_spec"},
        },
        "ui_schema": {
            "palette": {"group": node_type.category},
            "compatibility_renderer": "v1",
        },
        "ports": _ports(node_type),
        "requirements": _requirements(node_type),
        "side_effect": side_effect_contract,
        "policies": {
            "retry": {"mode": "v1_snapshot", "default_max_attempts": 1},
            "timeout": {"execution_seconds_default": 300},
            "error": {"default_action": "fail_run"},
        },
        "suspension": suspension,
        "lifecycle": {
            "state": "active" if node_type.publishable else "deprecated",
            "publish_mode": "allowed" if node_type.publishable else "blocked",
            "replacement": None,
            "migration_ids": [],
        },
        "extensions": {
            "textile": {
                "compatibility_mode": "v1",
                "required_config": [
                    _portable_config_key(item) for item in node_type.required_config
                ],
                "handler_channel": handler_channel,
            }
        },
    }


def _runtime_ready(node_type: NodeType, handler_channel: str) -> bool:
    if handler_channel == "placeholder":
        return False
    if handler_channel == "worker_callable":
        return node_registry.executor(node_type.type, node_type.version) is not None
    return True


def _load_manifests() -> list[dict[str, Any]]:
    root = resources.files("app.execution.v2.resources").joinpath("manifests")
    validator = Draft202012Validator(load_schema("pack-manifest-v2.schema.json"))
    manifests: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.name.endswith(".json"):
            manifest = json.loads(entry.read_text(encoding="utf-8"))
            validator.validate(manifest)
            manifests.append(manifest)
    return manifests


def _node_spec_from_resource(
    path: str, identity: tuple[str, int]
) -> dict[str, Any]:
    document = _load_json_resource(path.removeprefix("v2/resources/"))
    candidates: list[dict[str, Any]]
    if isinstance(document.get("nodes"), list):
        candidates = [item for item in document["nodes"] if isinstance(item, dict)]
    else:
        candidates = [document]
    matching = [
        item
        for item in candidates
        if (item.get("type"), item.get("type_version")) == identity
    ]
    if len(matching) != 1:
        raise ValueError(
            f"NodeSpec resource must contain exactly one {identity}: {path}"
        )
    return deepcopy(matching[0])


def _native_schema_is_closed(schema: dict[str, Any] | bool) -> bool:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    return schema.get("additionalProperties") is False


def _implementation_digest(
    *,
    pack: InstalledPack,
    descriptor: dict[str, Any],
    contract_digest: str,
) -> str:
    declared_paths = descriptor.get("implementation_resources")
    resource_paths = (
        list(declared_paths)
        if isinstance(declared_paths, list) and declared_paths
        else list(pack.manifest["resource_paths"])
    )
    manifest_paths = set(pack.manifest["resource_paths"])
    if any(path not in manifest_paths for path in resource_paths):
        raise ValueError(
            f"implementation resource is outside pack distribution: "
            f"{descriptor['type']}@{descriptor['type_version']}"
        )
    package_root = resources.files("app.execution")
    inventory = [
        (path, package_root.joinpath(path).read_bytes())
        for path in resource_paths
    ]
    return canonical_sha256(
        {
            "pack_id": pack.pack_id,
            "pack_version": pack.pack_version,
            "type": descriptor["type"],
            "type_version": descriptor["type_version"],
            "contract_digest": contract_digest,
            "source": descriptor["source"],
            "handler_channel": descriptor.get("handler_channel"),
            "resources_digest": resource_set_digest(inventory),
        }
    )


def _build_installed_registry() -> InstalledRegistry:
    manifests = _load_manifests()
    packs: list[InstalledPack] = []
    for manifest in manifests:
        digest = _manifest_distribution_digest(manifest)
        packs.append(
            InstalledPack(
                pack_id=manifest["pack_id"],
                pack_version=manifest["pack_version"],
                engine_version_range=manifest["engine_version_range"],
                distribution_digest=digest,
                ready=semver_matches(ENGINE_VERSION, manifest["engine_version_range"]),
                manifest=deepcopy(manifest),
            )
        )
    pack_registry = PackRegistry(packs)
    pack_by_identity = {(pack.pack_id, pack.pack_version): pack for pack in packs}
    trusted_renderers = _trusted_renderer_contracts()
    renderer_owner: dict[tuple[str, str], str] = {}
    for manifest in manifests:
        for renderer in (manifest["provides"].get("renderers") or []):
            identity = (renderer["capability"], renderer["version"])
            trusted = trusted_renderers.get(identity)
            if trusted is None or renderer["contract_digest"] != trusted["contract_digest"]:
                raise ValueError(f"unknown or damaged renderer contract: {identity}")
            previous = renderer_owner.setdefault(identity, manifest["pack_id"])
            if previous != manifest["pack_id"]:
                raise ValueError(f"renderer capability has multiple owners: {identity}")

    descriptors: list[tuple[InstalledPack, dict[str, Any]]] = []
    owner_by_node: dict[tuple[str, int], str] = {}
    for manifest in manifests:
        pack = pack_by_identity[(manifest["pack_id"], manifest["pack_version"])]
        for descriptor in manifest["provides"]["nodes"]:
            identity = (descriptor["type"], descriptor["type_version"])
            previous_owner = owner_by_node.setdefault(identity, pack.pack_id)
            if previous_owner != pack.pack_id:
                raise ValueError(f"node identity has multiple pack owners: {identity}")
            descriptors.append((pack, descriptor))

    current_nodes = {(node.type, node.version): node for node in node_registry.all()}
    compat_descriptors: dict[
        tuple[str, int], tuple[InstalledPack, dict[str, Any]]
    ] = {}
    for pack, descriptor in descriptors:
        if descriptor["source"] != "v1_registry_adapter":
            continue
        identity = (descriptor["type"], descriptor["type_version"])
        if identity in compat_descriptors:
            raise ValueError(f"duplicate v1 compatibility descriptor: {identity}")
        compat_descriptors[identity] = (pack, descriptor)
    if compat_descriptors.keys() != current_nodes.keys():
        missing = sorted(current_nodes.keys() - compat_descriptors.keys())
        extra = sorted(compat_descriptors.keys() - current_nodes.keys())
        raise ValueError(
            f"v1 compatibility owner set does not match registry; missing={missing}, extra={extra}"
        )

    spec_validator = Draft202012Validator(load_schema("node-spec-v2.schema.json"))
    node_specs: list[InstalledNodeSpec] = []
    executables: list[InstalledExecutable] = []
    for identity, node_type in current_nodes.items():
        pack, descriptor = compat_descriptors[identity]
        handler_channel = descriptor["handler_channel"]
        spec_document = _compat_node_spec(
            node_type,
            pack=pack,
            handler_channel=handler_channel,
        )
        spec_validator.validate(spec_document)
        contract_digest = canonical_sha256(spec_document)
        if descriptor.get("contract_digest") not in (None, contract_digest):
            raise ValueError(f"NodeSpec digest mismatch: {identity}")
        implementation_digest = _implementation_digest(
            pack=pack,
            descriptor=descriptor,
            contract_digest=contract_digest,
        )
        if descriptor.get("implementation_digest") not in (
            None,
            implementation_digest,
        ):
            raise ValueError(f"implementation digest mismatch: {identity}")
        installed_ready = handler_channel != "placeholder"
        runtime_ready = _runtime_ready(node_type, handler_channel)
        installed_spec = InstalledNodeSpec(
            type=node_type.type,
            type_version=node_type.version,
            contract_digest=contract_digest,
            implementation_digest=implementation_digest,
            pack_id=pack.pack_id,
            pack_version=pack.pack_version,
            distribution_digest=pack.distribution_digest,
            execution=deepcopy(spec_document["execution"]),
            config_schema=deepcopy(spec_document["config_schema"]),
            input_schema=deepcopy(spec_document["input_schema"]),
            output_schema=deepcopy(spec_document["output_schema"]),
            side_effect_class=spec_document["side_effect"]["class"],
            publishable=node_type.publishable,
            ready=installed_ready,
            spec=spec_document,
            handler_channel=handler_channel,
            source="v1_registry_adapter",
            preferred=bool(descriptor.get("preferred", False)),
        )
        node_specs.append(installed_spec)
        executables.append(
            InstalledExecutable(
                node_type=node_type.type,
                type_version=node_type.version,
                contract_digest=contract_digest,
                implementation_digest=implementation_digest,
                handler_channel=handler_channel,
                installed_ready=installed_ready,
                runtime_ready=runtime_ready,
                source="v1_registry_adapter",
            )
        )

    from app.execution.v2.native_handlers import native_handler

    manifest_resources = {
        (manifest["pack_id"], manifest["pack_version"]): set(
            manifest["resource_paths"]
        )
        for manifest in manifests
    }
    for pack, descriptor in descriptors:
        if descriptor["source"] != "resource":
            continue
        identity = (descriptor["type"], descriptor["type_version"])
        spec_path = str(descriptor["spec_resource"])
        if spec_path not in manifest_resources[(pack.pack_id, pack.pack_version)]:
            raise ValueError(
                f"NodeSpec resource is not included in pack digest: {spec_path}"
            )
        spec_document = _node_spec_from_resource(spec_path, identity)
        spec_validator.validate(spec_document)
        if spec_document["execution"]["owner"]["pack_id"] != pack.pack_id:
            raise ValueError(f"NodeSpec owner does not match manifest: {identity}")
        renderer = (spec_document.get("ui_schema") or {}).get("renderer")
        if isinstance(renderer, dict):
            renderer_identity = (
                str(renderer.get("capability") or ""),
                str(renderer.get("version") or ""),
            )
            if renderer_owner.get(renderer_identity) != pack.pack_id:
                raise ValueError(
                    f"NodeSpec renderer is not declared by owner pack: {renderer_identity}"
                )
        for schema_key in ("config_schema", "input_schema", "output_schema"):
            if not _native_schema_is_closed(spec_document[schema_key]):
                raise ValueError(
                    f"native NodeSpec {schema_key} must be a closed object: {identity}"
                )
        contract_digest = canonical_sha256(spec_document)
        if descriptor.get("contract_digest") not in (None, contract_digest):
            raise ValueError(f"NodeSpec digest mismatch: {identity}")
        implementation_digest = _implementation_digest(
            pack=pack,
            descriptor=descriptor,
            contract_digest=contract_digest,
        )
        if descriptor.get("implementation_digest") not in (
            None,
            implementation_digest,
        ):
            raise ValueError(f"implementation digest mismatch: {identity}")
        handler_channel = descriptor["handler_channel"]
        handler = (
            native_handler(identity[0], identity[1])
            if handler_channel in {"worker_callable", "kernel_builtin"}
            else None
        )
        execution_kind = spec_document["execution"]["kind"]
        installed_ready = handler_channel != "placeholder" and (
            execution_kind != "automatic" or handler is not None
        )
        runtime_ready = bool(pack.ready and installed_ready)
        lifecycle_mode = spec_document["lifecycle"]["publish_mode"]
        installed_spec = InstalledNodeSpec(
            type=identity[0],
            type_version=identity[1],
            contract_digest=contract_digest,
            implementation_digest=implementation_digest,
            pack_id=pack.pack_id,
            pack_version=pack.pack_version,
            distribution_digest=pack.distribution_digest,
            execution=deepcopy(spec_document["execution"]),
            config_schema=deepcopy(spec_document["config_schema"]),
            input_schema=deepcopy(spec_document["input_schema"]),
            output_schema=deepcopy(spec_document["output_schema"]),
            side_effect_class=spec_document["side_effect"]["class"],
            publishable=lifecycle_mode != "blocked",
            ready=installed_ready,
            spec=spec_document,
            handler_channel=handler_channel,
            source="resource",
            preferred=bool(descriptor.get("preferred", False)),
        )
        node_specs.append(installed_spec)
        executables.append(
            InstalledExecutable(
                node_type=identity[0],
                type_version=identity[1],
                contract_digest=contract_digest,
                implementation_digest=implementation_digest,
                handler_channel=handler_channel,
                installed_ready=installed_ready,
                runtime_ready=runtime_ready,
                source="resource",
                handler=handler,
            )
        )

    assets: list[InstalledAsset] = []
    for manifest in manifests:
        pack = pack_by_identity[(manifest["pack_id"], manifest["pack_version"])]
        resource_map = dict(_manifest_resources(manifest))
        for item in manifest["assets"]:
            content = resource_map.get(item["resource_path"])
            if content is None:
                raise ValueError(
                    f"asset is not listed in resource_paths: {item['asset_id']}"
                )
            if bytes_sha256(content) != item["digest"]:
                raise ValueError(f"asset digest mismatch: {item['asset_id']}")
            if len(content) != item["size_bytes"]:
                raise ValueError(f"asset size mismatch: {item['asset_id']}")
            assets.append(
                InstalledAsset(
                    asset_id=item["asset_id"],
                    kind=item["kind"],
                    version=item["version"],
                    digest=item["digest"],
                    media_type=item["media_type"],
                    size_bytes=item["size_bytes"],
                    resource_path=item["resource_path"],
                    pack_id=pack.pack_id,
                    pack_version=pack.pack_version,
                )
            )

    connectors: list[InstalledConnector] = []
    for manifest in manifests:
        pack = pack_by_identity[(manifest["pack_id"], manifest["pack_version"])]
        for connector_document in manifest["provides"]["connectors"]:
            operations: list[InstalledOperation] = []
            for operation_document in connector_document["operations"]:
                contract_digest = canonical_sha256(
                    {
                        "connector_id": connector_document["connector_id"],
                        "connector_version": connector_document["version"],
                        "operation_spec": operation_document,
                    }
                )
                operations.append(
                    InstalledOperation(
                        connector_id=connector_document["connector_id"],
                        connector_version=connector_document["version"],
                        operation=operation_document["operation"],
                        contract_version=operation_document["contract_version"],
                        contract_digest=contract_digest,
                        pack_id=pack.pack_id,
                        pack_version=pack.pack_version,
                        distribution_digest=pack.distribution_digest,
                        spec=deepcopy(operation_document),
                    )
                )
            connectors.append(
                InstalledConnector(
                    connector_id=connector_document["connector_id"],
                    version=connector_document["version"],
                    distribution_digest=pack.distribution_digest,
                    pack_id=pack.pack_id,
                    pack_version=pack.pack_version,
                    operations=tuple(operations),
                    queries=tuple(deepcopy(connector_document["queries"])),
                    ready=pack.ready,
                )
            )

    return InstalledRegistry(
        packs=pack_registry,
        node_specs=NodeSpecRegistry(node_specs),
        executables=ExecutableRegistry(executables),
        assets=AssetRegistry(assets),
        connectors=ConnectorRegistry(connectors),
    )


@lru_cache(maxsize=1)
def get_installed_registry() -> InstalledRegistry:
    return _build_installed_registry()


def reset_installed_registry_cache() -> None:
    """Clear the process-local cache for tests and trusted startup reloads."""

    get_installed_registry.cache_clear()


def registry_revision() -> str:
    return get_installed_registry().revision


def list_packs() -> list[dict[str, Any]]:
    return get_installed_registry().list_packs()


def resolve_pack(
    pack_id: str,
    version_range: str,
    distribution_digest: str | None = None,
) -> InstalledPack:
    return get_installed_registry().resolve_pack(
        pack_id,
        version_range,
        distribution_digest,
    )


def list_assets() -> list[dict[str, Any]]:
    return get_installed_registry().list_assets()


def list_renderer_capabilities() -> list[dict[str, Any]]:
    installed = get_installed_registry()
    declared = {
        (item["capability"], item["version"])
        for pack in installed.packs.all()
        for item in (pack.manifest["provides"].get("renderers") or [])
    }
    return [
        {**value, "ready": identity in declared}
        for identity, value in sorted(_trusted_renderer_contracts().items())
    ]


def resolve_asset(asset_id: str, version: str) -> InstalledAsset:
    return get_installed_registry().assets.resolve(asset_id, version)


def list_node_specs() -> list[dict[str, Any]]:
    return [item.public_dict() for item in get_installed_registry().list_node_specs()]


def resolve_node_spec(
    node_type: str,
    version: int,
    contract_digest: str | None = None,
) -> InstalledNodeSpec:
    return get_installed_registry().resolve_node_spec(
        node_type,
        version,
        contract_digest,
    )


def executable_binding_for(
    node_type: str,
    version: int,
    contract_digest: str | None = None,
) -> dict[str, Any]:
    return get_installed_registry().executable_binding_for(
        node_type,
        version,
        contract_digest,
    )


def worker_capability_document() -> dict[str, Any]:
    return get_installed_registry().worker_capability_document()


def handler_for_binding(execution_binding_digest: str) -> Callable[[Any], Any] | None:
    """Resolve only a trusted handler installed for an exact immutable binding."""

    return get_installed_registry().handler_for_binding(
        execution_binding_digest
    )


def list_connectors() -> list[dict[str, Any]]:
    return get_installed_registry().list_connectors()


def resolve_connector(
    connector_id: str,
    version_range: str,
    distribution_digest: str | None = None,
) -> InstalledConnector:
    return get_installed_registry().resolve_connector(
        connector_id,
        version_range,
        distribution_digest,
    )


def resolve_operation(
    connector_id: str,
    version_range: str,
    operation: str,
    contract_version: int,
    contract_digest: str | None = None,
) -> InstalledOperation:
    return get_installed_registry().resolve_operation(
        connector_id,
        version_range,
        operation,
        contract_version,
        contract_digest,
    )


def legacy_operation_ref_for_node(node_type: str) -> str | None:
    return LEGACY_NODE_OPERATION_REFS.get(node_type)
