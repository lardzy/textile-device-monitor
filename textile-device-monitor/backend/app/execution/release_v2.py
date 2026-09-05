"""Execution v2 portable release lifecycle.

The portable document, environment binding and executable projection are kept
as three different records.  A release can therefore be reviewed and staged
without mutating a v1 workflow, while a published version remains replayable
without consulting the latest installed registry.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
from copy import deepcopy
from datetime import timedelta
from importlib import resources
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.execution.errors import ExecutionApiError, conflict, not_found
from app.execution.events import append_audit_log
from app.execution.models import (
    ExecutionCategory,
    ExecutionCredential,
    ExecutionDeploymentBinding,
    ExecutionProjectRule,
    ExecutionReleasePreflight,
    ExecutionRole,
    ExecutionStorageRoot,
    ExecutionUser,
    ExecutionWorkflow,
    ExecutionWorkflowActivationReceipt,
    ExecutionWorkflowRelease,
    ExecutionWorkflowVersion,
    ExecutionWorkerHeartbeat,
    ExecutionWorkerNodeCapability,
    utcnow,
)
from app.execution.validation import (
    ValidationResult,
    definition_checksum,
    validate_definition,
    workflow_contract_checksum,
)
from app.execution.v2.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    semver_matches,
)


ENGINE_VERSION = "2.1.0"
CONTRACT_FORMAT = "workflow-release-v2"
SIDE_EFFECT_ORDER = {
    "none": 0,
    "local_write": 1,
    "publish": 2,
    "external_write": 3,
}
SIDE_EFFECT_SUMMARY = {
    "none": "none",
    "read_only": "none",
    "reversible_local_write": "local_write",
    "durable_write": "publish",
    "external_write": "external_write",
}
ROLLOUT_PROFILE_RANK = {
    "p1_readonly": 0,
    "p2_human": 1,
    "p2_local_write": 2,
    "p2_publish": 3,
}
SLOT_GROUPS = (
    "root_slots",
    "credential_slots",
    "role_slots",
    "rule_slots",
)
SLOT_CONFIG_SUFFIXES = {
    "_root_slot": "_root_id",
    "_role_slot": "_role_key",
    "_rule_slot": "_rule_key",
}
SLOT_GROUP_BY_SUFFIX = {
    "_root_slot": "root_slots",
    "_role_slot": "role_slots",
    "_rule_slot": "rule_slots",
}


def _issue(
    code: str,
    path: str,
    message: str,
    *,
    level: str = "error",
) -> dict[str, str]:
    return {"level": level, "code": code, "path": path, "message": message}


def _workflow_schema() -> dict[str, Any]:
    schema_file = resources.files("app.execution.v2.resources").joinpath(
        "schemas/workflow-release-v2.schema.json"
    )
    return json.loads(schema_file.read_text(encoding="utf-8"))


def _release_digest(document: dict[str, Any]) -> str:
    payload = {key: value for key, value in document.items() if key != "integrity"}
    return canonical_sha256(payload)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(48)


def _registry_api():
    # Kept local so migrations and model-only utilities do not initialize the
    # registry or trusted handlers as an import side effect.
    from app.execution.v2 import registry as registry_api

    return registry_api


def _public_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    public_dict = getattr(value, "public_dict", None)
    if callable(public_dict):
        return deepcopy(public_dict())
    raise TypeError(f"unsupported registry value: {type(value).__name__}")


def _execution_kind(binding: dict[str, Any]) -> str:
    execution = binding.get("execution")
    if isinstance(execution, dict):
        return str(execution.get("kind") or "")
    return str(execution or binding.get("execution_kind") or "")


def rollout_profile_blockers(
    node_instances: list[dict[str, Any]],
    *,
    profile: str | None = None,
) -> list[dict[str, Any]]:
    """Return deterministic P2 rollout blockers for immutable node bindings."""

    selected = (profile or settings.EXECUTION_V2_ROLLOUT_PROFILE).strip().lower()
    rank = ROLLOUT_PROFILE_RANK.get(selected, -1)
    blockers: list[dict[str, Any]] = []
    for instance in sorted(node_instances, key=lambda item: item.get("node_id", "")):
        kind = str(instance.get("execution_kind") or "")
        side_effect = str(instance.get("side_effect_class") or "none")
        source = str(instance.get("source") or "v1_registry_adapter")
        code: str | None = None
        if kind == "external_side_effect" or side_effect == "external_write":
            code = "external_write_blocked_until_p4"
        elif source == "v1_registry_adapter" and side_effect in {
            "reversible_local_write",
            "durable_write",
        }:
            code = "compatibility_write_blocked_p2"
        elif kind == "human" and (source != "resource" or rank < 1):
            code = "native_human_profile_required"
        elif side_effect == "reversible_local_write" and (
            source != "resource" or rank < 2
        ):
            code = "local_write_profile_required"
        elif side_effect == "durable_write" and (
            source != "resource" or rank < 3
        ):
            code = "publish_profile_required"
        if code is not None:
            blockers.append(
                {
                    "node_id": instance.get("node_id"),
                    "code": code,
                    "profile": selected,
                }
            )
    return blockers


def _validate_document_shape(document: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    try:
        encoded = canonical_json_bytes(document)
    except (TypeError, ValueError) as exc:
        return [_issue("release_json_invalid", "$", str(exc))]
    max_bytes = int(settings.EXECUTION_RELEASE_JSON_MAX_BYTES)
    if len(encoded) > max_bytes:
        issues.append(
            _issue(
                "release_too_large",
                "$",
                f"Release JSON exceeds the {max_bytes} byte limit",
            )
        )
        return issues
    validator = Draft202012Validator(
        _workflow_schema(), format_checker=FormatChecker()
    )
    for error in sorted(
        validator.iter_errors(document),
        key=lambda item: (list(item.absolute_path), item.message),
    ):
        suffix = "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        issues.append(
            _issue(
                "release_schema_invalid",
                f"${suffix}",
                error.message,
            )
        )
    return issues


def _trusted_key(key_id: str) -> Optional[Ed25519PublicKey]:
    directory = str(settings.EXECUTION_RELEASE_TRUSTED_KEYS_DIR or "").strip()
    if not directory or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}", key_id):
        return None
    base = Path(directory).resolve(strict=False)
    candidates = (base / f"{key_id}.pub", base / f"{key_id}.pem")
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(base)
            raw = resolved.read_bytes().strip()
        except (FileNotFoundError, OSError, ValueError):
            continue
        try:
            loaded = serialization.load_pem_public_key(raw)
            if isinstance(loaded, Ed25519PublicKey):
                return loaded
        except ValueError:
            pass
        try:
            decoded = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
            return Ed25519PublicKey.from_public_bytes(decoded)
        except (ValueError, TypeError):
            continue
    return None


def _verify_integrity(
    document: dict[str, Any], release_digest: str
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    integrity = document.get("integrity") or {}
    if integrity.get("digest") != release_digest:
        issues.append(
            _issue(
                "release_digest_mismatch",
                "$.integrity.digest",
                "The RFC 8785 release digest does not match the document",
            )
        )
    signatures = integrity.get("signatures") or []
    policy = settings.EXECUTION_RELEASE_SIGNATURE_POLICY.strip().lower()
    if policy == "disabled":
        return issues
    verified = 0
    for index, signature in enumerate(signatures):
        path = f"$.integrity.signatures[{index}]"
        if signature.get("signed_digest") != release_digest:
            issues.append(
                _issue(
                    "release_signature_digest_mismatch",
                    f"{path}.signed_digest",
                    "Signature digest does not match the release digest",
                )
            )
            continue
        public_key = _trusted_key(str(signature.get("key_id") or ""))
        if public_key is None:
            issues.append(
                _issue(
                    "release_signature_untrusted",
                    f"{path}.key_id",
                    "Signature key is not trusted by this environment",
                )
            )
            continue
        try:
            raw_signature = base64.urlsafe_b64decode(
                str(signature.get("signature") or "") + "=="
            )
            public_key.verify(raw_signature, bytes.fromhex(release_digest))
        except (ValueError, binascii.Error, InvalidSignature):
            issues.append(
                _issue(
                    "release_signature_invalid",
                    f"{path}.signature",
                    "Ed25519 signature verification failed",
                )
            )
            continue
        verified += 1
    if policy == "required" and verified == 0:
        issues.append(
            _issue(
                "release_signature_required",
                "$.integrity.signatures",
                "At least one trusted Ed25519 signature is required",
            )
        )
    return issues


def _schema_issues(
    schema: Any, value: Any, path: str
) -> list[dict[str, str]]:
    if not isinstance(schema, (dict, bool)):
        return [_issue("node_schema_invalid", path, "Effective schema is invalid")]
    validator = Draft202012Validator(schema)
    issues: list[dict[str, str]] = []
    for error in sorted(
        validator.iter_errors(value), key=lambda item: list(item.absolute_path)
    ):
        suffix = "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        issues.append(
            _issue("node_config_invalid", f"{path}{suffix}", error.message)
        )
    return issues


def _portable_human_form_schema_issues(
    schema: Any, path: str
) -> list[dict[str, str]]:
    if not isinstance(schema, dict):
        return [_issue("human_form_schema_invalid", path, "form_schema must be an object schema")]
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        return [_issue("human_form_schema_invalid", path, str(exc))]
    forbidden = {
        "$ref",
        "$dynamicRef",
        "if",
        "then",
        "else",
        "not",
        "allOf",
        "anyOf",
        "oneOf",
        "dependentSchemas",
        "patternProperties",
        "unevaluatedProperties",
        "contentEncoding",
        "contentMediaType",
    }
    issues: list[dict[str, str]] = []

    def visit(value: Any, current_path: str, *, root: bool = False) -> None:
        if not isinstance(value, dict):
            issues.append(
                _issue(
                    "human_form_schema_not_supported",
                    current_path,
                    "Every form field schema must be an object",
                )
            )
            return
        blocked = sorted(forbidden.intersection(value))
        if blocked:
            issues.append(
                _issue(
                    "human_form_schema_not_supported",
                    current_path,
                    f"Unsupported form keywords: {', '.join(blocked)}",
                )
            )
        declared_type = value.get("type")
        types = set(declared_type) if isinstance(declared_type, list) else {declared_type}
        types.discard(None)
        types.discard("null")
        if root and types != {"object"}:
            issues.append(
                _issue(
                    "human_form_schema_not_supported",
                    current_path,
                    "The root form schema must be an object",
                )
            )
        if "object" in types:
            if value.get("additionalProperties") is not False:
                issues.append(
                    _issue(
                        "human_form_schema_not_supported",
                        current_path,
                        "Form object schemas must be closed",
                    )
                )
            properties = value.get("properties") or {}
            if not isinstance(properties, dict):
                issues.append(
                    _issue(
                        "human_form_schema_invalid",
                        f"{current_path}.properties",
                        "properties must be an object",
                    )
                )
            else:
                for name in sorted(properties):
                    visit(properties[name], f"{current_path}.properties.{name}")
        if "array" in types:
            items = value.get("items")
            item_type = items.get("type") if isinstance(items, dict) else None
            item_types = set(item_type) if isinstance(item_type, list) else {item_type}
            if "object" in item_types or "array" in item_types or items is None:
                issues.append(
                    _issue(
                        "human_form_schema_not_supported",
                        f"{current_path}.items",
                        "P2 forms support scalar arrays only",
                    )
                )
            elif isinstance(items, dict):
                visit(items, f"{current_path}.items")

    visit(schema, path, root=True)
    return issues


def _effective_schemas(
    node: dict[str, Any],
    binding: dict[str, Any],
    document: dict[str, Any],
) -> tuple[Any, Any]:
    definition = document["definition"]
    node_type = node["type"]
    if node_type == "core.start":
        schema = deepcopy(definition["input_schema"])
        return schema, deepcopy(schema)
    if node_type == "core.end":
        schema = deepcopy(definition["output_schema"])
        return schema, deepcopy(schema)
    config = node.get("config") or {}

    def pointer(root: Any, value: str | None) -> Any:
        current = root
        for part in str(value or "").split("/")[1:]:
            key = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(current, dict) or key not in current:
                raise LookupError(value)
            current = current[key]
        return current

    def resolve(direction: str) -> Any:
        bindings = binding.get("schema_bindings") or {}
        schema_binding = bindings.get(direction) or {"source": "node_spec"}
        source = schema_binding.get("source")
        if source == "node_spec":
            return deepcopy(binding.get(f"{direction}_schema") or {})
        if source == "workflow_input_schema":
            return deepcopy(definition["input_schema"])
        if source == "workflow_output_schema":
            return deepcopy(definition["output_schema"])
        if source == "node_config":
            return deepcopy(pointer(config, schema_binding.get("pointer")))
        raise LookupError(source)

    try:
        return resolve("input"), resolve("output")
    except LookupError as exc:
        raise ValueError(
            f"effective schema binding cannot be resolved for {node_type}: {exc}"
        ) from exc


def _binding_digest(binding: dict[str, Any]) -> str:
    explicit = binding.get("execution_binding_digest")
    if isinstance(explicit, str) and re.fullmatch(r"[a-f0-9]{64}", explicit):
        return explicit
    identity = {
        key: binding.get(key)
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
        )
    }
    return canonical_sha256(identity)


def _resolve_dependencies(
    document: dict[str, Any], issues: list[dict[str, str]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    registry_api = _registry_api()
    revision = registry_api.registry_revision()
    dependency = document["dependencies"]
    engine_range = dependency["engine"]["version_range"]
    try:
        engine_ok = semver_matches(ENGINE_VERSION, engine_range)
    except ValueError as exc:
        issues.append(_issue("engine_version_range_invalid", "$.dependencies.engine", str(exc)))
        engine_ok = False
    if not engine_ok:
        issues.append(
            _issue(
                "engine_version_unavailable",
                "$.dependencies.engine.version_range",
                f"Installed engine {ENGINE_VERSION} does not satisfy {engine_range}",
            )
        )

    resolved_packs: list[dict[str, Any]] = []
    for index, required in enumerate(dependency.get("packs") or []):
        try:
            pack = registry_api.get_installed_registry().resolve_pack(
                required["pack_id"],
                required["version_range"],
                required.get("distribution_digest"),
            )
            resolved_packs.append(_public_value(pack))
        except (LookupError, RuntimeError, ValueError) as exc:
            issues.append(
                _issue(
                    "pack_dependency_unavailable",
                    f"$.dependencies.packs[{index}]",
                    str(exc),
                )
            )

    resolved_connectors: list[dict[str, Any]] = []
    resolved_operation_keys: set[tuple[str, str, int]] = set()
    connector_dependencies: dict[str, dict[str, Any]] = {}
    registry = registry_api.get_installed_registry()
    for index, required in enumerate(dependency.get("connectors") or []):
        connector_id = required["connector_id"]
        if connector_id in connector_dependencies:
            issues.append(
                _issue(
                    "connector_dependency_duplicate",
                    f"$.dependencies.connectors[{index}]",
                    "A connector may only be declared once",
                )
            )
            continue
        connector_dependencies[connector_id] = required
        try:
            connector = registry.resolve_connector(
                connector_id,
                required["version_range"],
                required.get("distribution_digest"),
            )
            connector_value = _public_value(connector)
        except (LookupError, RuntimeError, ValueError, TypeError) as exc:
            issues.append(
                _issue(
                    "connector_dependency_unavailable",
                    f"$.dependencies.connectors[{index}]",
                    str(exc),
                )
            )
            continue
        locked_operations: list[dict[str, Any]] = []
        for operation_index, operation in enumerate(required["operations"]):
            try:
                resolved = registry.resolve_operation(
                    connector_id,
                    required["version_range"],
                    operation["operation"],
                    operation["contract_version"],
                    operation["contract_digest"],
                )
                resolved_value = _public_value(resolved)
                locked_operations.append(resolved_value)
                resolved_operation_keys.add(
                    (
                        connector_id,
                        operation["operation"],
                        operation["contract_version"],
                    )
                )
            except (LookupError, RuntimeError, ValueError, TypeError) as exc:
                issues.append(
                    _issue(
                        "connector_operation_unavailable",
                        (
                            f"$.dependencies.connectors[{index}]."
                            f"operations[{operation_index}]"
                        ),
                        str(exc),
                    )
                )
        if required["queries"]:
            issues.append(
                _issue(
                    "connector_query_not_supported_p1",
                    f"$.dependencies.connectors[{index}].queries",
                    "Connector QuerySpec execution is not enabled in P1",
                )
            )
        resolved_connectors.append(
            {
                "connector_id": connector_value["connector_id"],
                "version": connector_value["version"],
                "distribution_digest": connector_value[
                    "distribution_digest"
                ],
                "pack_id": connector_value["pack_id"],
                "pack_version": connector_value["pack_version"],
                "operations": sorted(
                    locked_operations,
                    key=lambda item: (
                        item["operation"],
                        item["contract_version"],
                    ),
                ),
                "queries": [],
            }
        )

    declared_nodes: dict[tuple[str, int], dict[str, Any]] = {}
    for index, required in enumerate(dependency.get("node_types") or []):
        identity = (required["type"], required["type_version"])
        if identity in declared_nodes:
            issues.append(
                _issue(
                    "node_dependency_duplicate",
                    f"$.dependencies.node_types[{index}]",
                    "A node type/version may only be declared once",
                )
            )
        declared_nodes[identity] = required

    node_instances: list[dict[str, Any]] = []
    used_identities: set[tuple[str, int]] = set()
    max_side_effect = "none"
    execution_kinds: set[str] = set()
    declared_capabilities: set[str] = set()
    used_operation_keys: set[tuple[str, str, int]] = set()
    pack_by_id = {pack.get("pack_id"): pack for pack in resolved_packs}
    resource_slots = {
        group: {
            slot["slot_id"]: slot
            for slot in document["resources"].get(group) or []
        }
        for group in SLOT_GROUPS
    }
    for index, node in enumerate(document["definition"]["nodes"]):
        identity = (node["type"], node["type_version"])
        used_identities.add(identity)
        required = declared_nodes.get(identity)
        if required is None:
            issues.append(
                _issue(
                    "node_dependency_missing",
                    f"$.definition.nodes[{index}]",
                    f"No exact dependency is declared for {identity[0]}@{identity[1]}",
                )
            )
            continue
        try:
            spec = registry_api.resolve_node_spec(
                identity[0], identity[1], required["contract_digest"]
            )
            binding = registry_api.executable_binding_for(
                identity[0], identity[1], required["contract_digest"]
            )
            spec_value = _public_value(spec)
            binding = _public_value(binding)
        except (LookupError, RuntimeError, ValueError, TypeError) as exc:
            issues.append(
                _issue(
                    "node_executable_missing",
                    f"$.definition.nodes[{index}]",
                    str(exc),
                )
            )
            continue
        if required.get("implementation_digest") not in {
            None,
            binding.get("implementation_digest"),
        }:
            issues.append(
                _issue(
                    "node_implementation_digest_mismatch",
                    f"$.dependencies.node_types[{identity[0]}]",
                    "Installed implementation does not match the release pin",
                )
            )
        pack = pack_by_id.get(binding.get("pack_id"))
        if pack is None:
            issues.append(
                _issue(
                    "node_owner_pack_missing",
                    f"$.definition.nodes[{index}]",
                    f"Owner pack {binding.get('pack_id')} is not declared",
                )
            )
        elif pack.get("pack_version") != binding.get("pack_version"):
            issues.append(
                _issue(
                    "node_owner_pack_version_mismatch",
                    f"$.definition.nodes[{index}]",
                    "Node and resolved pack versions do not match",
                )
            )
        issues.extend(
            _schema_issues(
                spec_value.get("config_schema") or {},
                node.get("config") or {},
                f"$.definition.nodes[{index}].config",
            )
        )
        if identity == ("human.form", 1):
            issues.extend(
                _portable_human_form_schema_issues(
                    (node.get("config") or {}).get("form_schema"),
                    f"$.definition.nodes[{index}].config.form_schema",
                )
            )
        if node.get("runtime_policy"):
            issues.append(
                _issue(
                    "runtime_policy_not_supported_p2",
                    f"$.definition.nodes[{index}].runtime_policy",
                    "P2 uses the fixed engine lease, retry and fail_run policy",
                )
            )
        try:
            input_schema, output_schema = _effective_schemas(
                node, binding, document
            )
        except ValueError as exc:
            issues.append(
                _issue(
                    "node_schema_binding_invalid",
                    f"$.definition.nodes[{index}]",
                    str(exc),
                )
            )
            input_schema, output_schema = {}, {}
        kind = _execution_kind(binding)
        execution_kinds.add(kind)
        side_effect_class = str(binding.get("side_effect_class") or "none")
        summarized = SIDE_EFFECT_SUMMARY.get(side_effect_class)
        if summarized is None:
            issues.append(
                _issue(
                    "node_side_effect_unknown",
                    f"$.definition.nodes[{index}]",
                    f"Unknown side-effect class: {side_effect_class}",
                )
            )
            summarized = "external_write"
        if SIDE_EFFECT_ORDER[summarized] > SIDE_EFFECT_ORDER[max_side_effect]:
            max_side_effect = summarized
        requirements = spec_value.get("requirements") or {}
        resources = requirements.get("resources") or {}
        config = node.get("config") or {}
        access_rank = {"read": 0, "write": 1, "publish": 2}
        for requirement in resources.get("root_slots") or []:
            pointer = str(requirement.get("config_pointer") or "")
            key = pointer[1:] if pointer.startswith("/") else ""
            slot_id = config.get(key) if key else None
            if slot_id in (None, "") and not requirement.get("required"):
                continue
            declared_slot = resource_slots["root_slots"].get(slot_id)
            if declared_slot is None:
                issues.append(
                    _issue(
                        "node_root_slot_missing",
                        f"$.definition.nodes[{index}].config.{key}",
                        "Node config does not reference a declared root slot",
                    )
                )
                continue
            required_access = str(requirement.get("access") or "read")
            if access_rank[declared_slot["access"]] < access_rank[required_access]:
                issues.append(
                    _issue(
                        "root_access_mismatch",
                        f"$.definition.nodes[{index}].config.{key}",
                        "Declared root slot does not provide the required access",
                    )
                )
        for requirement in requirements.get("capabilities") or []:
            capability_id = requirement.get("capability_id")
            if capability_id:
                declared_capabilities.add(str(capability_id))
        for connector_requirement in requirements.get("connectors") or []:
            operation_ref = str(
                connector_requirement.get("operation_ref") or ""
            )
            matching_connector_ids = [
                connector_id
                for connector_id in connector_dependencies
                if operation_ref.startswith(connector_id + ".")
            ]
            try:
                ref_name, ref_version = operation_ref.rsplit("@", 1)
                contract_version = int(ref_version)
            except (ValueError, TypeError):
                matching_connector_ids = []
                ref_name = ""
                contract_version = 0
            if len(matching_connector_ids) != 1:
                issues.append(
                    _issue(
                        "connector_operation_dependency_missing",
                        f"$.definition.nodes[{index}]",
                        f"Node operation_ref is not declared: {operation_ref}",
                    )
                )
                continue
            connector_id = matching_connector_ids[0]
            operation_name = ref_name[len(connector_id) + 1 :]
            operation_key = (
                connector_id,
                operation_name,
                contract_version,
            )
            used_operation_keys.add(operation_key)
            if operation_key not in resolved_operation_keys:
                issues.append(
                    _issue(
                        "connector_operation_dependency_missing",
                        f"$.definition.nodes[{index}]",
                        f"Exact Connector OperationSpec is not locked: {operation_ref}",
                    )
                )
        node_instances.append(
            {
                "node_id": node["id"],
                "type": identity[0],
                "type_version": identity[1],
                "contract_digest": binding["contract_digest"],
                "implementation_digest": binding["implementation_digest"],
                "pack_id": binding["pack_id"],
                "pack_version": binding["pack_version"],
                "execution_kind": kind,
                "execution_binding_digest": _binding_digest(binding),
                "source": binding.get("source"),
                "side_effect_class": side_effect_class,
                "handler_channel": binding.get("handler_channel"),
                "schema_bindings": deepcopy(binding.get("schema_bindings") or {}),
                "ports": deepcopy(binding.get("ports") or {}),
                "suspension": deepcopy(binding.get("suspension")),
                "renderer_contract": deepcopy(
                    binding.get("renderer_contract") or {}
                ),
                "lifecycle": deepcopy(binding.get("lifecycle") or {}),
                "publishable": bool(binding.get("publishable")),
                "installed_ready": bool(binding.get("ready")),
                "effective_input_schema": input_schema,
                "effective_input_schema_digest": canonical_sha256(input_schema),
                "effective_output_schema": output_schema,
                "effective_output_schema_digest": canonical_sha256(output_schema),
            }
        )
    for identity in sorted(set(declared_nodes) - used_identities):
        issues.append(
            _issue(
                "node_dependency_unused",
                "$.dependencies.node_types",
                f"Declared dependency {identity[0]}@{identity[1]} is not used",
                level="warning",
            )
        )
    for operation_key in sorted(resolved_operation_keys - used_operation_keys):
        issues.append(
            _issue(
                "connector_operation_dependency_unused",
                "$.dependencies.connectors",
                (
                    f"Declared operation {operation_key[0]}."
                    f"{operation_key[1]}@{operation_key[2]} is not used"
                ),
                level="warning",
            )
        )
    for connector in resolved_connectors:
        pack = pack_by_id.get(connector["pack_id"])
        if pack is None or pack.get("pack_version") != connector["pack_version"]:
            issues.append(
                _issue(
                    "connector_owner_pack_missing",
                    "$.dependencies.connectors",
                    "Connector owner pack is not declared at the resolved version",
                )
            )

    for slot in document["resources"].get("root_slots") or []:
        access = slot["access"]
        declared_capabilities.add(f"file.{access}")
        root_effect = {"read": "none", "write": "local_write", "publish": "publish"}[access]
        if SIDE_EFFECT_ORDER[root_effect] > SIDE_EFFECT_ORDER[max_side_effect]:
            max_side_effect = root_effect
    computed = {
        "declared": sorted(declared_capabilities),
        "side_effect_level": max_side_effect,
        "requires_human_approval": "human" in execution_kinds,
    }
    if document.get("capabilities") != computed:
        issues.append(
            _issue(
                "capability_declaration_mismatch",
                "$.capabilities",
                "Declared capabilities do not match the installed contracts",
            )
        )
    lock = {
        "lock_version": "1.0",
        "engine": {
            "version": ENGINE_VERSION,
            "registry_revision": revision,
        },
        "packs": sorted(
            resolved_packs,
            key=lambda item: (str(item.get("pack_id")), str(item.get("pack_version"))),
        ),
        "connectors": sorted(
            resolved_connectors,
            key=lambda item: (item["connector_id"], item["version"]),
        ),
        "node_instances": sorted(node_instances, key=lambda item: item["node_id"]),
    }
    lock["digest"] = canonical_sha256(lock)
    return lock, computed


def _resolve_asset_lock(
    document: dict[str, Any], issues: list[dict[str, str]]
) -> dict[str, Any]:
    installed = [_public_value(item) for item in _registry_api().get_installed_registry().list_assets()]
    lock_items: list[dict[str, Any]] = []
    for index, asset in enumerate(document.get("assets") or []):
        source = asset["source"]
        if source["kind"] == "bundle":
            issues.append(
                _issue(
                    "bundle_transport_not_supported",
                    f"$.assets[{index}].source",
                    "Bare JSON endpoints only accept registry assets",
                )
            )
            continue
        matching = [
            candidate
            for candidate in installed
            if candidate.get("asset_id") == asset["asset_id"]
            and candidate.get("version") == asset["version"]
            and candidate.get("digest") == asset["digest"]
            and candidate.get("registry_key") == source["registry_key"]
        ]
        if len(matching) != 1:
            issues.append(
                _issue(
                    "registry_asset_unavailable",
                    f"$.assets[{index}]",
                    "The exact registry asset is not installed",
                )
            )
            continue
        installed_asset = matching[0]
        for key in ("kind", "media_type", "size_bytes"):
            if installed_asset.get(key) != asset.get(key):
                issues.append(
                    _issue(
                        "registry_asset_contract_mismatch",
                        f"$.assets[{index}].{key}",
                        f"Installed asset {key} does not match the release",
                    )
                )
        lock_items.append(installed_asset)
    value = {"assets": sorted(lock_items, key=lambda item: item["asset_id"])}
    value["digest"] = canonical_sha256(value)
    return value


def _required_bindings(document: dict[str, Any]) -> list[dict[str, Any]]:
    required: list[dict[str, Any]] = []
    for group in SLOT_GROUPS:
        for slot in document["resources"].get(group) or []:
            required.append(
                {
                    "kind": group[:-1],
                    "slot_id": slot["slot_id"],
                    "required": bool(slot["required"]),
                    **{key: value for key, value in slot.items() if key not in {"slot_id", "name", "description", "required"}},
                }
            )
    return required


def _current_binding(
    db: Session,
    release_id: str,
    environment: Optional[str] = None,
) -> Optional[ExecutionDeploymentBinding]:
    environment = environment or settings.EXECUTION_ENVIRONMENT_ID
    return (
        db.query(ExecutionDeploymentBinding)
        .filter(
            ExecutionDeploymentBinding.release_id == release_id,
            ExecutionDeploymentBinding.environment == environment,
        )
        .order_by(ExecutionDeploymentBinding.revision.desc())
        .first()
    )


def _live_binding_issues(
    db: Session, binding_values: dict[str, Any]
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for slot_id, value in (binding_values.get("root_slots") or {}).items():
        root = db.get(ExecutionStorageRoot, value.get("storage_root_id"))
        if (
            root is None
            or not root.is_active
            or not root.is_available
            or root.root_id != value.get("root_id")
            or int(root.binding_revision or 1) != value.get("revision")
        ):
            issues.append(
                _issue(
                    "deployment_binding_stale",
                    f"$.deployment_binding.root_slots.{slot_id}",
                    "StorageRoot identity, availability or revision changed",
                )
            )
    for slot_id, value in (binding_values.get("credential_slots") or {}).items():
        credential = db.get(ExecutionCredential, value.get("credential_id"))
        if (
            credential is None
            or not credential.is_active
            or credential.revision != value.get("revision")
            or credential.system_key != value.get("system_key")
        ):
            issues.append(
                _issue(
                    "deployment_binding_stale",
                    f"$.deployment_binding.credential_slots.{slot_id}",
                    "Credential identity or revision changed",
                )
            )
    for slot_id, value in (binding_values.get("role_slots") or {}).items():
        role = db.get(ExecutionRole, value.get("role_id"))
        current_permissions = (
            {item.permission.key for item in role.permission_bindings}
            if role is not None
            else set()
        )
        if (
            role is None
            or role.key != value.get("role_key")
            or not set(value.get("permissions") or []).issubset(
                current_permissions
            )
        ):
            issues.append(
                _issue(
                    "deployment_binding_stale",
                    f"$.deployment_binding.role_slots.{slot_id}",
                    "Role identity or permissions changed",
                )
            )
    for slot_id, value in (binding_values.get("rule_slots") or {}).items():
        rule = db.get(ExecutionProjectRule, value.get("rule_id"))
        if (
            rule is None
            or not rule.enabled
            or rule.rule_key != value.get("rule_key")
            or rule.revision != value.get("revision")
        ):
            issues.append(
                _issue(
                    "deployment_binding_stale",
                    f"$.deployment_binding.rule_slots.{slot_id}",
                    "Rule identity, enabled state or revision changed",
                )
            )
    return issues


def _binding_publish_issues(
    db: Session,
    document: dict[str, Any],
    binding: Optional[ExecutionDeploymentBinding],
) -> list[dict[str, str]]:
    if binding is None:
        return [
            _issue(
                "deployment_binding_missing",
                "$.deployment_binding",
                "A deployment binding is required before publish",
            )
        ]
    issues: list[dict[str, str]] = []
    expected_digest = canonical_sha256(
        {"environment": binding.environment, "bindings": binding.binding}
    )
    if expected_digest != binding.digest:
        issues.append(
            _issue(
                "deployment_binding_digest_invalid",
                "$.deployment_binding.digest",
                "Deployment binding digest does not match its immutable payload",
            )
        )
    issues.extend(_live_binding_issues(db, binding.binding or {}))
    groups = binding.binding or {}
    for group in SLOT_GROUPS:
        values = groups.get(group) or {}
        for index, slot in enumerate(document["resources"].get(group) or []):
            if slot["required"] and slot["slot_id"] not in values:
                issues.append(
                    _issue(
                        "required_binding_missing",
                        f"$.resources.{group}[{index}]",
                        f"Required slot {slot['slot_id']} is not bound",
                    )
                )
    return issues


def _content_semantic_issues(
    document: dict[str, Any]
) -> list[dict[str, str]]:
    """Validate graph semantics without consulting deployment identities."""

    bindings: dict[str, dict[str, Any]] = {
        group: {} for group in SLOT_GROUPS
    }
    # Some v1 compatibility schemas pin a particular physical root with a
    # ``const``.  Portable NodeSpecs deliberately replace that value with a
    # logical slot.  For this deployment-independent semantic pass, project a
    # slot to the legacy const when one exists; an actual environment identity
    # is still required and revalidated during publish preflight.
    legacy_root_by_slot: dict[str, str] = {}
    from app.execution.registry import NodeRegistry, NodeType, node_registry

    for node in document["definition"].get("nodes") or []:
        node_type = node_registry.get(
            str(node.get("type") or ""),
            int(node.get("type_version") or 1),
        )
        properties = (
            (node_type.config_schema or {}).get("properties") or {}
            if node_type is not None
            and isinstance(node_type.config_schema, dict)
            else {}
        )
        for key, slot_id in (node.get("config") or {}).items():
            if not isinstance(slot_id, str):
                continue
            if key == "root_slot":
                legacy_key = "root_id"
            elif key.endswith("_root_slot"):
                legacy_key = key[: -len("_root_slot")] + "_root_id"
            else:
                continue
            legacy_schema = properties.get(legacy_key) or {}
            if isinstance(legacy_schema, dict) and isinstance(
                legacy_schema.get("const"), str
            ):
                legacy_root_by_slot.setdefault(
                    slot_id, legacy_schema["const"]
                )
    for slot in document["resources"].get("root_slots") or []:
        # A generated v1-shaped root id lets the established DAG validator
        # check config references/access without leaking or guessing a target
        # environment StorageRoot.
        dummy_root = legacy_root_by_slot.get(slot["slot_id"])
        if dummy_root is None:
            dummy_root = "v2_" + hashlib.sha256(
                slot["slot_id"].encode("utf-8")
            ).hexdigest()[:16]
        bindings["root_slots"][slot["slot_id"]] = {
            "storage_root_id": dummy_root,
            "root_id": dummy_root,
            "revision": 0,
        }
    for slot in document["resources"].get("credential_slots") or []:
        connector_id = slot["connector_id"]
        bindings["credential_slots"][slot["slot_id"]] = {
            "credential_id": "v2-preview",
            "revision": 0,
            "system_key": (
                "legacy_inspection"
                if connector_id == "legacy_fibrecheck"
                else connector_id
            ),
        }
    for slot in document["resources"].get("role_slots") or []:
        bindings["role_slots"][slot["slot_id"]] = {
            "role_id": "v2-preview",
            "role_key": "v2_preview",
            "permissions": slot["required_permissions"],
        }
    for slot in document["resources"].get("rule_slots") or []:
        bindings["rule_slots"][slot["slot_id"]] = {
            "rule_id": "v2-preview",
            "rule_key": "v2_preview",
            "revision": 0,
        }
    projection = compile_runtime_projection(
        document, SimpleNamespace(binding=bindings)
    )
    dependency_by_identity = {
        (item["type"], item["type_version"]): item
        for item in document["dependencies"].get("node_types") or []
    }
    semantic_registry = NodeRegistry()
    compatibility_node_ids: set[str] = set()

    def project_config_schema(schema: Any) -> Any:
        if not isinstance(schema, dict):
            return deepcopy(schema)
        value = deepcopy(schema)
        properties = value.get("properties")
        if isinstance(properties, dict):
            projected: dict[str, Any] = {}
            for key, item in properties.items():
                if key == "root_slot":
                    target = "root_id"
                elif key.endswith("_root_slot"):
                    target = key[: -len("_root_slot")] + "_root_id"
                elif key.endswith("_role_slot"):
                    target = key[: -len("_role_slot")] + "_role_key"
                elif key.endswith("_rule_slot"):
                    target = key[: -len("_rule_slot")] + "_rule_key"
                else:
                    target = key
                projected[target] = item
            value["properties"] = projected
            value["required"] = [
                (
                    "root_id"
                    if key == "root_slot"
                    else key[: -len("_root_slot")] + "_root_id"
                    if key.endswith("_root_slot")
                    else key[: -len("_role_slot")] + "_role_key"
                    if key.endswith("_role_slot")
                    else key[: -len("_rule_slot")] + "_rule_key"
                    if key.endswith("_rule_slot")
                    else key
                )
                for key in value.get("required") or []
            ]
        return value

    registered: set[tuple[str, int]] = set()
    for portable_node, projected_node in zip(
        document["definition"]["nodes"], projection["nodes"], strict=True
    ):
        identity = (
            portable_node["type"],
            portable_node["type_version"],
        )
        dependency = dependency_by_identity.get(identity)
        if dependency is None:
            continue
        try:
            installed = _registry_api().resolve_node_spec(
                identity[0], identity[1], dependency["contract_digest"]
            )
        except (LookupError, ValueError, TypeError):
            continue
        if installed.source == "v1_registry_adapter":
            compatibility_node_ids.add(projected_node["id"])
        if identity in registered:
            continue
        if installed.source == "v1_registry_adapter":
            node_type = node_registry.get(*identity)
            if node_type is None:
                continue
        else:
            spec = installed.spec
            config_schema = project_config_schema(spec["config_schema"])
            node_type = NodeType(
                type=identity[0],
                version=identity[1],
                name=spec["name"],
                category=spec["category"],
                description=spec["description"],
                execution_kind=spec["execution"]["kind"],
                required_config=tuple(config_schema.get("required") or ()),
                config_schema=config_schema,
                input_schema=deepcopy(installed.input_schema),
                output_schema=deepcopy(installed.output_schema),
                publishable=installed.publishable,
            )
        semantic_registry.register(node_type)
        registered.add(identity)
    result = validate_definition(
        projection,
        registry=semantic_registry,
        for_publish=False,
        compatibility_node_ids=compatibility_node_ids,
    )
    return [
        _issue(
            issue.code,
            issue.path,
            issue.message,
            level=issue.level,
        )
        for issue in result.issues
    ]


def preflight_release(
    db: Session,
    *,
    document: dict[str, Any],
    actor: ExecutionUser,
    release: Optional[ExecutionWorkflowRelease] = None,
    scope: str = "content",
) -> dict[str, Any]:
    issues = _validate_document_shape(document)
    release_digest = _release_digest(document) if not issues else ""
    if not issues:
        issues.extend(_verify_integrity(document, release_digest))
    content_base_end = len(issues)
    dependency_lock: dict[str, Any] = {}
    computed_capabilities: dict[str, Any] = {}
    asset_lock: dict[str, Any] = {}
    if not issues:
        dependency_lock, computed_capabilities = _resolve_dependencies(document, issues)
        asset_lock = _resolve_asset_lock(document, issues)
        issues.extend(_content_semantic_issues(document))
    content_valid = not any(
        issue["level"] == "error" for issue in issues
    )
    # content_base_end is intentionally not used to hide dependency failures:
    # installed contract resolution is part of import validity.
    del content_base_end
    binding = _current_binding(db, release.id) if release is not None else None
    if scope == "publish" and content_valid:
        issues.extend(_binding_publish_issues(db, document, binding))
        category_exists = (
            db.query(ExecutionCategory.id)
            .filter(
                ExecutionCategory.key
                == document["release"]["category_key"]
            )
            .first()
            is not None
        )
        if not category_exists:
            issues.append(
                _issue(
                    "release_category_missing",
                    "$.release.category_key",
                    "Release category does not exist in this environment",
                )
            )
        workflow = (
            db.query(ExecutionWorkflow)
            .filter(
                ExecutionWorkflow.slug == document["release"]["slug"]
            )
            .one_or_none()
        )
        if workflow is not None and workflow.management_mode != "release_v2":
            issues.append(
                _issue(
                    "workflow_slug_conflict",
                    "$.release.slug",
                    "A v1-managed workflow already uses this slug",
                )
            )
        heartbeat_threshold = utcnow() - timedelta(
            seconds=int(settings.EXECUTION_WORKER_HEARTBEAT_TIMEOUT_SECONDS)
        )
        for instance in dependency_lock.get("node_instances") or []:
            compatible = (
                db.query(ExecutionWorkerNodeCapability.id)
                .join(
                    ExecutionWorkerHeartbeat,
                    ExecutionWorkerHeartbeat.worker_id
                    == ExecutionWorkerNodeCapability.worker_id,
                )
                .filter(
                    ExecutionWorkerNodeCapability.execution_binding_digest
                    == instance["execution_binding_digest"],
                    ExecutionWorkerNodeCapability.ready.is_(True),
                    ExecutionWorkerHeartbeat.status == "running",
                    ExecutionWorkerHeartbeat.last_seen_at
                    >= heartbeat_threshold,
                )
                .first()
            )
            if compatible is None:
                issues.append(
                    _issue(
                        "node_capability_unavailable",
                        f"$.definition.nodes[{instance['node_id']}]",
                        "No fresh Worker currently advertises this exact binding",
                        level="warning",
                    )
                )
        if settings.EXECUTION_CONTRACT_MODE.strip().lower() != "enforced":
            issues.append(
                _issue(
                    "execution_contract_mode_not_enforced",
                    "$.deployment",
                    "Execution v2 publish is enabled only in enforced mode",
                )
            )
    node_instances = dependency_lock.get("node_instances") or []
    profile = settings.EXECUTION_V2_ROLLOUT_PROFILE.strip().lower()
    profile_blockers = rollout_profile_blockers(
        node_instances,
        profile=profile,
    )
    rollout_ready = bool(
        content_valid
        and not profile_blockers
        and all(
            item.get("publishable") is True
            and item.get("installed_ready") is True
            for item in node_instances
        )
    )
    if content_valid:
        for instance in dependency_lock.get("node_instances") or []:
            if instance.get("publishable") is not True:
                issues.append(
                    _issue(
                        "node_not_publishable",
                        f"$.definition.nodes[{instance['node_id']}]",
                        "Installed NodeSpec is metadata-only and cannot be published",
                        level="warning",
                    )
                )
            elif instance.get("installed_ready") is not True:
                issues.append(
                    _issue(
                        "node_executable_not_ready",
                        f"$.definition.nodes[{instance['node_id']}]",
                        "Installed node binding is not ready",
                        level="warning",
                    )
                )
    for blocker in profile_blockers:
        issues.append(
            _issue(
                blocker["code"],
                f"$.definition.nodes[{blocker['node_id']}]",
                f"Node is blocked by rollout profile {profile}",
                level="warning",
            )
        )
    if content_valid and not rollout_ready and profile == "p1_readonly":
        issues.append(
            _issue(
                "p1_publish_gate_blocked",
                "$.capabilities",
                "The default p1_readonly profile does not admit this Release",
                level="warning",
            )
        )
    publish_ready = bool(
        scope == "publish"
        and content_valid
        and rollout_ready
        and not any(issue["level"] == "error" for issue in issues)
    )
    report = {
        "content_valid": content_valid,
        "publish_ready": publish_ready,
        "release_digest": release_digest,
        "registry_revision": _registry_api().registry_revision(),
        "issues": issues,
        "resolved_dependencies": dependency_lock,
        "required_bindings": _required_bindings(document) if content_valid else [],
        "computed_capabilities": computed_capabilities,
        "asset_lock": asset_lock,
        "binding_revision": binding.revision if binding is not None else None,
        "rollout_profile": profile,
        "rollout_blockers": profile_blockers,
    }
    token = _new_token()
    ttl = int(settings.EXECUTION_RELEASE_PREFLIGHT_TTL_MINUTES)
    row = ExecutionReleasePreflight(
        release_id=release.id if release is not None else None,
        scope=scope,
        token_hash=_token_hash(token),
        release_digest=release_digest or "0" * 64,
        registry_revision=report["registry_revision"],
        binding_revision=report["binding_revision"],
        report=deepcopy(report),
        portable_document=deepcopy(document) if content_valid else None,
        created_by_id=actor.id,
        expires_at=utcnow() + timedelta(minutes=ttl),
    )
    db.add(row)
    db.flush()
    report["preflight_token"] = token
    report["preflight_expires_at"] = row.expires_at.isoformat()
    return report


def _consume_preflight(
    db: Session,
    *,
    token: str,
    actor: ExecutionUser,
    expected_scope: str,
    release_digest: str,
    release_id: Optional[str] = None,
) -> ExecutionReleasePreflight:
    row = (
        db.query(ExecutionReleasePreflight)
        .filter(ExecutionReleasePreflight.token_hash == _token_hash(token))
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        raise ExecutionApiError(422, "preflight_token_invalid", "预检令牌无效")
    if row.created_by_id != actor.id:
        raise ExecutionApiError(
            403,
            "preflight_token_owner_mismatch",
            "预检令牌只能由创建它的用户消费",
        )
    now = utcnow()
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=now.tzinfo)
    if row.consumed_at is not None:
        raise conflict("preflight_token_replayed", "预检令牌已被使用")
    if expires_at <= now:
        raise conflict("preflight_token_expired", "预检令牌已过期")
    if row.scope != expected_scope or row.release_digest != release_digest:
        raise conflict("preflight_token_mismatch", "预检令牌与当前 Release 不匹配")
    if release_id is not None and row.release_id != release_id:
        raise conflict("preflight_token_mismatch", "预检令牌与 staged Release 不匹配")
    if not bool((row.report or {}).get("content_valid")):
        raise ExecutionApiError(422, "release_content_invalid", "Release 内容预检未通过")
    row.consumed_at = now
    row.consumed_by_id = actor.id
    return row


def apply_release(
    db: Session,
    *,
    document: Optional[dict[str, Any]],
    preflight_token: str,
    actor: ExecutionUser,
) -> ExecutionWorkflowRelease:
    if document is None:
        pending = (
            db.query(ExecutionReleasePreflight)
            .filter(
                ExecutionReleasePreflight.token_hash
                == _token_hash(preflight_token)
            )
            .one_or_none()
        )
        if pending is None or pending.portable_document is None:
            raise ExecutionApiError(
                422, "preflight_token_invalid", "预检令牌无效"
            )
        document = deepcopy(pending.portable_document)
    digest = _release_digest(document)
    row = _consume_preflight(
        db,
        token=preflight_token,
        actor=actor,
        expected_scope="content",
        release_digest=digest,
    )
    if row.registry_revision != _registry_api().registry_revision():
        raise conflict(
            "registry_revision_changed",
            "安装契约已变化，请重新预检",
            expected=row.registry_revision,
            current=_registry_api().registry_revision(),
        )
    metadata = document["release"]
    existing = (
        db.query(ExecutionWorkflowRelease)
        .filter(
            ExecutionWorkflowRelease.source_slug == metadata["slug"],
            ExecutionWorkflowRelease.source_version == metadata["release_version"],
        )
        .one_or_none()
    )
    if existing is not None:
        if existing.release_digest != digest:
            raise conflict(
                "release_source_identity_conflict",
                "相同 slug/release_version 已对应不同内容",
                release_id=existing.id,
            )
        return existing
    release = ExecutionWorkflowRelease(
        source_slug=metadata["slug"],
        source_version=metadata["release_version"],
        release_digest=digest,
        format_version=document["format_version"],
        portable_document=deepcopy(document),
        status="staged",
        created_by_id=actor.id,
    )
    try:
        # Keep the outer token-consumption transaction usable after a unique
        # identity race.  The savepoint lets us re-read the concurrent winner
        # and preserve the specified same-digest idempotency semantics.
        with db.begin_nested():
            db.add(release)
            db.flush()
    except IntegrityError as exc:
        concurrent = (
            db.query(ExecutionWorkflowRelease)
            .filter(
                ExecutionWorkflowRelease.source_slug == metadata["slug"],
                ExecutionWorkflowRelease.source_version
                == metadata["release_version"],
            )
            .one_or_none()
        )
        if concurrent is not None and concurrent.release_digest == digest:
            return concurrent
        raise conflict(
            "release_source_identity_conflict",
            "相同 slug/release_version 已被并发创建",
        ) from exc
    append_audit_log(
        db,
        action="workflow_release.apply",
        resource_type="workflow_release",
        resource_id=release.id,
        actor_user_id=actor.id,
        details={"release_digest": digest},
    )
    return release


def _validate_binding_payload(
    db: Session,
    *,
    release: ExecutionWorkflowRelease,
    payload: dict[str, Any],
    actor: ExecutionUser,
) -> dict[str, Any]:
    allowed_groups = set(SLOT_GROUPS)
    extra_groups = set(payload) - allowed_groups
    if extra_groups:
        raise ExecutionApiError(
            422,
            "deployment_binding_invalid",
            "环境绑定包含未知字段",
            details={"unknown": sorted(extra_groups)},
        )
    document = release.portable_document
    normalized: dict[str, dict[str, Any]] = {group: {} for group in SLOT_GROUPS}
    declared = {
        group: {slot["slot_id"]: slot for slot in document["resources"].get(group) or []}
        for group in SLOT_GROUPS
    }
    for group in SLOT_GROUPS:
        supplied = payload.get(group) or {}
        if not isinstance(supplied, dict):
            raise ExecutionApiError(422, "deployment_binding_invalid", f"{group} 必须是对象")
        unknown = set(supplied) - set(declared[group])
        if unknown:
            raise ExecutionApiError(
                422,
                "deployment_binding_unknown_slot",
                "环境绑定引用了未声明的 slot",
                details={"group": group, "slots": sorted(unknown)},
            )
    for slot_id, value in (payload.get("root_slots") or {}).items():
        if not isinstance(value, dict) or set(value) - {"root_id", "revision"}:
            raise ExecutionApiError(422, "deployment_binding_invalid", "root binding 只允许 root_id/revision")
        root = (
            db.query(ExecutionStorageRoot)
            .filter(ExecutionStorageRoot.root_id == value.get("root_id"))
            .one_or_none()
        )
        if root is None or not root.is_active:
            raise not_found("存储根", str(value.get("root_id") or ""))
        if not root.is_available:
            raise ExecutionApiError(422, "storage_root_unavailable", "存储根当前不可用")
        required_access = declared["root_slots"][slot_id]["access"]
        access_rank = {"read": 0, "write": 1, "publish": 2}
        if access_rank.get(root.access_mode, -1) < access_rank[required_access]:
            raise ExecutionApiError(
                422,
                "root_access_mismatch",
                "存储根权限与 slot 声明不一致",
                details={"slot_id": slot_id, "required": required_access, "actual": root.access_mode},
            )
        current_revision = int(root.binding_revision or 1)
        if value.get("revision") not in {None, current_revision}:
            raise conflict(
                "storage_root_revision_conflict",
                "存储根 revision 已变化",
                current_revision=current_revision,
            )
        normalized["root_slots"][slot_id] = {
            "storage_root_id": root.id,
            "root_id": root.root_id,
            "revision": current_revision,
        }
    for slot_id, value in (payload.get("credential_slots") or {}).items():
        if not isinstance(value, dict) or set(value) != {"credential_id", "revision"}:
            raise ExecutionApiError(422, "deployment_binding_invalid", "credential binding 必须包含 credential_id/revision")
        credential = db.get(ExecutionCredential, value["credential_id"])
        if credential is None or credential.user_id != actor.id or not credential.is_active:
            raise not_found("凭据", str(value["credential_id"]))
        if credential.revision != value["revision"]:
            raise conflict("credential_revision_conflict", "凭据 revision 已变化", current_revision=credential.revision)
        normalized["credential_slots"][slot_id] = {
            "credential_id": credential.id,
            "revision": credential.revision,
            "system_key": credential.system_key,
        }
    for slot_id, value in (payload.get("role_slots") or {}).items():
        if not isinstance(value, dict) or set(value) != {"role_key"}:
            raise ExecutionApiError(422, "deployment_binding_invalid", "role binding 必须只包含 role_key")
        role = db.query(ExecutionRole).filter(ExecutionRole.key == value["role_key"]).one_or_none()
        if role is None:
            raise not_found("角色", str(value["role_key"]))
        permissions = sorted(binding.permission.key for binding in role.permission_bindings)
        required_permissions = set(declared["role_slots"][slot_id]["required_permissions"])
        if not required_permissions.issubset(permissions):
            raise ExecutionApiError(422, "role_permissions_missing", "角色缺少 slot 要求的权限")
        normalized["role_slots"][slot_id] = {"role_id": role.id, "role_key": role.key, "permissions": permissions}
    for slot_id, value in (payload.get("rule_slots") or {}).items():
        if not isinstance(value, dict) or set(value) != {"rule_key", "revision"}:
            raise ExecutionApiError(422, "deployment_binding_invalid", "rule binding 必须包含 rule_key/revision")
        rule = db.query(ExecutionProjectRule).filter(ExecutionProjectRule.rule_key == value["rule_key"]).one_or_none()
        if rule is None or not rule.enabled:
            raise not_found("项目规则", str(value["rule_key"]))
        if rule.revision != value["revision"]:
            raise conflict("rule_revision_conflict", "规则 revision 已变化", current_revision=rule.revision)
        normalized["rule_slots"][slot_id] = {"rule_id": rule.id, "rule_key": rule.rule_key, "revision": rule.revision}
    for group in SLOT_GROUPS:
        for slot_id, slot in declared[group].items():
            if slot["required"] and slot_id not in normalized[group]:
                raise ExecutionApiError(
                    422,
                    "required_binding_missing",
                    f"必填 slot 尚未绑定：{slot_id}",
                    details={"group": group, "slot_id": slot_id},
                )
    return normalized


def put_deployment_binding(
    db: Session,
    *,
    release_id: str,
    environment: str,
    expected_revision: int,
    bindings: dict[str, Any],
    actor: ExecutionUser,
) -> ExecutionDeploymentBinding:
    release = (
        db.query(ExecutionWorkflowRelease)
        .filter(ExecutionWorkflowRelease.id == release_id)
        .with_for_update()
        .one_or_none()
    )
    if release is None:
        raise not_found("Workflow Release", release_id)
    current = _current_binding(db, release_id, environment)
    current_revision = current.revision if current is not None else 0
    if expected_revision != current_revision:
        raise conflict(
            "deployment_binding_revision_conflict",
            "环境绑定已发生变化",
            current_revision=current_revision,
        )
    normalized = _validate_binding_payload(db, release=release, payload=bindings, actor=actor)
    digest = canonical_sha256({"environment": environment, "bindings": normalized})
    if current is not None and current.digest == digest:
        return current
    row = ExecutionDeploymentBinding(
        release_id=release.id,
        environment=environment,
        revision=current_revision + 1,
        binding=normalized,
        digest=digest,
        created_by_id=actor.id,
    )
    db.add(row)
    db.flush()
    append_audit_log(
        db,
        action="workflow_release.binding.create",
        resource_type="workflow_release",
        resource_id=release.id,
        actor_user_id=actor.id,
        details={"environment": environment, "revision": row.revision, "digest": digest},
    )
    return row


def _project_config_value(value: Any, binding: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [_project_config_value(item, binding) for item in value]
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            new_key = key
            group: Optional[str] = None
            if key == "root_slot":
                new_key, group = "root_id", "root_slots"
            elif key in {"role_slot", "candidate_role_slot"}:
                new_key, group = "candidate_role", "role_slots"
            elif key == "rule_slot":
                new_key, group = "match_rule", "rule_slots"
            else:
                for suffix, replacement in SLOT_CONFIG_SUFFIXES.items():
                    if key.endswith(suffix):
                        new_key = key[: -len(suffix)] + replacement
                        group = SLOT_GROUP_BY_SUFFIX[suffix]
                        break
            if group and isinstance(item, str):
                slot_binding = (binding.get(group) or {}).get(item) or {}
                value_key = {"root_slots": "root_id", "role_slots": "role_key", "rule_slots": "rule_key"}[group]
                projected[new_key] = slot_binding.get(value_key)
            else:
                projected[new_key] = _project_config_value(item, binding)
        return projected
    return value


def compile_runtime_projection(
    document: dict[str, Any], binding: ExecutionDeploymentBinding
) -> dict[str, Any]:
    definition = document["definition"]
    bindings = binding.binding
    root_slots = []
    for slot in document["resources"].get("root_slots") or []:
        resolved = (bindings.get("root_slots") or {}).get(slot["slot_id"])
        if resolved is None:
            continue
        root_slots.append(
            {"name": slot["name"], "root_id": resolved["root_id"], "access": slot["access"]}
        )
    credential_slots = []
    for slot in document["resources"].get("credential_slots") or []:
        # v1 stores only the logical credential slot. User-scoped record IDs
        # remain in the deployment binding and never enter the workflow JSON.
        resolved = (bindings.get("credential_slots") or {}).get(
            slot["slot_id"]
        ) or {}
        credential_slots.append(
            {
                "name": slot["slot_id"],
                "system_key": resolved.get("system_key")
                or slot["connector_id"],
            }
        )
    nodes = []
    native_join_modes: dict[str, str] = {}
    for node in definition["nodes"]:
        value = deepcopy(node)
        value["config"] = _project_config_value(value.get("config") or {}, bindings)
        value.pop("runtime_policy", None)
        nodes.append(value)
        if value.get("type") == "flow.join":
            native_join_modes[str(value.get("id"))] = str(
                (value.get("config") or {}).get("mode") or "all_selected"
            )
    edges = []
    for edge in definition["edges"]:
        value = deepcopy(edge)
        join_mode = native_join_modes.get(str(value.get("target")))
        if join_mode is not None:
            value["join_policy"] = (
                "any" if join_mode == "first_selected" else "all"
            )
        if value.get("join_policy") == "all":
            value.pop("join_policy", None)
        edges.append(value)
    return {
        "schema_version": "1.0",
        "metadata": {
            "slug": document["release"]["slug"],
            "name": document["release"]["name"],
            "category": document["release"]["category_key"],
            "release_digest": _release_digest(document),
        },
        "input_schema": deepcopy(definition["input_schema"]),
        "global_schema": deepcopy(definition["global_schema"]),
        "root_slots": root_slots,
        "credential_slots": credential_slots,
        "nodes": nodes,
        "edges": edges,
    }


def _validate_v2_runtime_projection(
    document: dict[str, Any], projection: dict[str, Any]
) -> ValidationResult:
    """Run the established DAG validator with a release-scoped registry.

    Native contracts deliberately never enter the global v1 ``node_registry``.
    The projection validator therefore materializes only the exact contracts
    pinned by this Release, while marking compatibility nodes so legacy domain
    policies cannot leak onto native primitives.
    """

    from app.execution.registry import NodeRegistry, NodeType, node_registry

    dependencies = {
        (item["type"], item["type_version"]): item
        for item in document["dependencies"].get("node_types") or []
    }
    semantic_registry = NodeRegistry()
    compatibility_node_ids: set[str] = set()
    registered: set[tuple[str, int]] = set()

    def projected_config_schema(schema: Any) -> Any:
        if not isinstance(schema, dict):
            return deepcopy(schema)
        value = deepcopy(schema)
        properties = value.get("properties")
        if not isinstance(properties, dict):
            return value
        projected: dict[str, Any] = {}
        for key, item in properties.items():
            if key == "root_slot":
                target = "root_id"
            elif key.endswith("_root_slot"):
                target = key[: -len("_root_slot")] + "_root_id"
            elif key.endswith("_role_slot"):
                target = key[: -len("_role_slot")] + "_role_key"
            elif key.endswith("_rule_slot"):
                target = key[: -len("_rule_slot")] + "_rule_key"
            else:
                target = key
            projected[target] = item
        value["properties"] = projected
        value["required"] = [
            (
                "root_id"
                if key == "root_slot"
                else key[: -len("_root_slot")] + "_root_id"
                if key.endswith("_root_slot")
                else key[: -len("_role_slot")] + "_role_key"
                if key.endswith("_role_slot")
                else key[: -len("_rule_slot")] + "_rule_key"
                if key.endswith("_rule_slot")
                else key
            )
            for key in value.get("required") or []
        ]
        return value

    for portable, projected in zip(
        document["definition"]["nodes"],
        projection["nodes"],
        strict=True,
    ):
        identity = (portable["type"], portable["type_version"])
        dependency = dependencies.get(identity)
        if dependency is None:
            continue
        installed = _registry_api().resolve_node_spec(
            identity[0], identity[1], dependency["contract_digest"]
        )
        if installed.source == "v1_registry_adapter":
            compatibility_node_ids.add(str(projected["id"]))
        if identity in registered:
            continue
        if installed.source == "v1_registry_adapter":
            node_type = node_registry.get(*identity)
            if node_type is None:
                continue
        else:
            spec = installed.spec
            config_schema = projected_config_schema(spec["config_schema"])
            node_type = NodeType(
                type=identity[0],
                version=identity[1],
                name=spec["name"],
                category=spec["category"],
                description=spec["description"],
                execution_kind=spec["execution"]["kind"],
                required_config=tuple(config_schema.get("required") or ()),
                config_schema=config_schema,
                input_schema=deepcopy(installed.input_schema),
                output_schema=deepcopy(installed.output_schema),
                publishable=installed.publishable,
            )
        semantic_registry.register(node_type)
        registered.add(identity)
    return validate_definition(
        projection,
        registry=semantic_registry,
        for_publish=True,
        compatibility_node_ids=compatibility_node_ids,
    )


def _release_row(db: Session, release_id: str) -> ExecutionWorkflowRelease:
    release = db.get(ExecutionWorkflowRelease, release_id)
    if release is None:
        raise not_found("Workflow Release", release_id)
    return release


def publish_release(
    db: Session,
    *,
    release_id: str,
    preflight_token: str,
    actor: ExecutionUser,
    reason: Optional[str],
) -> tuple[ExecutionWorkflowRelease, ExecutionWorkflowVersion, ExecutionWorkflowActivationReceipt]:
    release = (
        db.query(ExecutionWorkflowRelease)
        .filter(ExecutionWorkflowRelease.id == release_id)
        .with_for_update()
        .one_or_none()
    )
    if release is None:
        raise not_found("Workflow Release", release_id)
    token_row = _consume_preflight(
        db,
        token=preflight_token,
        actor=actor,
        expected_scope="publish",
        release_digest=release.release_digest,
        release_id=release.id,
    )
    report = token_row.report or {}
    if not report.get("publish_ready"):
        raise ExecutionApiError(422, "release_not_publishable", "Release 发布预检未通过", details=report)
    if token_row.registry_revision != _registry_api().registry_revision():
        raise conflict("registry_revision_changed", "安装契约已变化，请重新预检")
    binding = _current_binding(db, release.id)
    if binding is None or binding.revision != token_row.binding_revision:
        raise conflict("deployment_binding_revision_changed", "环境绑定已变化，请重新预检")
    binding_issues = _binding_publish_issues(
        db, release.portable_document, binding
    )
    if any(item["level"] == "error" for item in binding_issues):
        raise conflict(
            "deployment_binding_stale",
            "环境绑定的底层 identity 或 revision 已变化，请重新绑定",
            issues=binding_issues,
        )
    document = release.portable_document
    projection = compile_runtime_projection(document, binding)
    validation = _validate_v2_runtime_projection(document, projection)
    if not validation.valid:
        raise ExecutionApiError(
            422,
            "runtime_projection_invalid",
            "v2 Release 无法编译为当前运行时投影",
            details=validation.as_dict(),
        )
    category = (
        db.query(ExecutionCategory)
        .filter(ExecutionCategory.key == document["release"]["category_key"])
        .one_or_none()
    )
    if category is None:
        raise ExecutionApiError(422, "release_category_missing", "Release 分类在当前环境不存在")
    workflow = (
        db.query(ExecutionWorkflow)
        .filter(ExecutionWorkflow.slug == release.source_slug)
        .with_for_update()
        .one_or_none()
    )
    if workflow is None:
        candidate_workflow = ExecutionWorkflow(
            slug=release.source_slug,
            category_id=category.id,
            name=document["release"]["name"],
            description=document["release"].get("description"),
            draft_definition=deepcopy(projection),
            draft_revision=1,
            management_mode="release_v2",
            capabilities=deepcopy(report["computed_capabilities"]),
            required_input_count=len((projection["input_schema"] or {}).get("required") or []),
            is_enabled=True,
            created_by_id=actor.id,
            updated_by_id=actor.id,
        )
        try:
            # Different source versions of one portable slug lock different
            # Release rows.  Their initial workflow lookup can therefore both
            # observe no row.  Isolate the unique-slug insert so the loser can
            # retain its publish transaction and continue against the winner.
            with db.begin_nested():
                db.add(candidate_workflow)
                db.flush()
            workflow = candidate_workflow
        except IntegrityError as exc:
            workflow = (
                db.query(ExecutionWorkflow)
                .filter(ExecutionWorkflow.slug == release.source_slug)
                .with_for_update()
                .one_or_none()
            )
            if workflow is None:
                raise conflict(
                    "workflow_slug_conflict",
                    "同 slug 的流程已被并发创建，请重试发布",
                    slug=release.source_slug,
                ) from exc
            if workflow.management_mode != "release_v2":
                raise conflict(
                    "workflow_slug_conflict",
                    "同 slug 的 v1 草稿流程已存在",
                    workflow_id=workflow.id,
                ) from exc
    elif workflow.management_mode != "release_v2":
        raise conflict("workflow_slug_conflict", "同 slug 的 v1 草稿流程已存在", workflow_id=workflow.id)
    dependency_lock = deepcopy(report["resolved_dependencies"])
    asset_lock = deepcopy(report["asset_lock"])
    deployed_checksum = canonical_sha256(
        {
            "definition_checksum": definition_checksum(projection),
            "capabilities": report["computed_capabilities"],
            "dependency_lock_digest": dependency_lock["digest"],
            "asset_lock_digest": asset_lock["digest"],
            "deployment_binding_digest": binding.digest,
            "deployment_binding_revision": binding.revision,
            "release_digest": release.release_digest,
        }
    )
    existing = (
        db.query(ExecutionWorkflowVersion)
        .filter(
            ExecutionWorkflowVersion.workflow_id == workflow.id,
            ExecutionWorkflowVersion.release_id == release.id,
            ExecutionWorkflowVersion.release_digest == release.release_digest,
            ExecutionWorkflowVersion.deployed_contract_checksum
            == deployed_checksum,
            ExecutionWorkflowVersion.deployment_binding_digest
            == binding.digest,
            ExecutionWorkflowVersion.dependency_lock_digest
            == dependency_lock["digest"],
        )
        .one_or_none()
    )
    if existing is not None:
        from_version = workflow.published_version_number
        receipt = (
            db.query(ExecutionWorkflowActivationReceipt)
            .filter(
                ExecutionWorkflowActivationReceipt.workflow_id == workflow.id,
                ExecutionWorkflowActivationReceipt.release_id == release.id,
                ExecutionWorkflowActivationReceipt.action == "publish",
                ExecutionWorkflowActivationReceipt.to_version_number
                == existing.version_number,
                ExecutionWorkflowActivationReceipt.deployment_binding_digest
                == binding.digest,
            )
            .order_by(ExecutionWorkflowActivationReceipt.created_at.desc())
            .first()
        )
        if from_version != existing.version_number or receipt is None:
            receipt = ExecutionWorkflowActivationReceipt(
                workflow_id=workflow.id,
                release_id=release.id,
                action="publish",
                from_version_number=from_version,
                to_version_number=existing.version_number,
                release_digest=release.release_digest,
                deployment_binding_digest=binding.digest,
                actor_user_id=actor.id,
                reason=reason or "reactivate identical release contract",
            )
            db.add(receipt)
            workflow.published_version_number = existing.version_number
            # The v2 workflow draft is a read-only compatibility mirror for
            # legacy list/detail APIs.  Reactivation must move that mirror with
            # the active pointer just like a rollback does.
            workflow.draft_definition = deepcopy(existing.definition)
            workflow.capabilities = deepcopy(existing.capabilities or {})
            workflow.required_input_count = len(
                (existing.definition.get("input_schema") or {}).get(
                    "required"
                )
                or []
            )
            workflow.draft_revision += 1
            workflow.category_id = category.id
            workflow.name = document["release"]["name"]
            workflow.description = document["release"].get("description")
            workflow.updated_by_id = actor.id
            append_audit_log(
                db,
                action="workflow_release.reactivate",
                resource_type="workflow",
                resource_id=workflow.id,
                actor_user_id=actor.id,
                details={
                    "from_version": from_version,
                    "to_version": existing.version_number,
                    "release_id": release.id,
                },
            )
            db.flush()
        release.status = "published"
        release.published_at = release.published_at or utcnow()
        release.workflow_id = workflow.id
        return release, existing, receipt
    latest_number = db.query(func.max(ExecutionWorkflowVersion.version_number)).filter(
        ExecutionWorkflowVersion.workflow_id == workflow.id
    ).scalar() or 0
    from_version = workflow.published_version_number
    version = ExecutionWorkflowVersion(
        workflow_id=workflow.id,
        version_number=int(latest_number) + 1,
        schema_version="1.0",
        definition=deepcopy(projection),
        checksum=definition_checksum(projection),
        capabilities=deepcopy(report["computed_capabilities"]),
        contract_checksum=workflow_contract_checksum(projection, report["computed_capabilities"]),
        contract_format=CONTRACT_FORMAT,
        release_id=release.id,
        release_digest=release.release_digest,
        dependency_lock=dependency_lock,
        dependency_lock_digest=dependency_lock["digest"],
        deployment_binding_snapshot={
            "environment": binding.environment,
            "revision": binding.revision,
            "bindings": deepcopy(binding.binding),
        },
        deployment_binding_digest=binding.digest,
        asset_lock=asset_lock,
        engine_version=ENGINE_VERSION,
        deployed_contract_checksum=deployed_checksum,
        release_note=document["release"].get("release_note"),
        published_by_id=actor.id,
    )
    db.add(version)
    db.flush()
    workflow.published_version_number = version.version_number
    workflow.draft_definition = deepcopy(projection)
    workflow.draft_revision += 1
    workflow.category_id = category.id
    workflow.name = document["release"]["name"]
    workflow.description = document["release"].get("description")
    workflow.capabilities = deepcopy(report["computed_capabilities"])
    workflow.updated_by_id = actor.id
    release.workflow_id = workflow.id
    release.status = "published"
    release.published_at = utcnow()
    receipt = ExecutionWorkflowActivationReceipt(
        workflow_id=workflow.id,
        release_id=release.id,
        action="publish",
        from_version_number=from_version,
        to_version_number=version.version_number,
        release_digest=release.release_digest,
        deployment_binding_digest=binding.digest,
        actor_user_id=actor.id,
        reason=reason,
    )
    db.add(receipt)
    db.flush()
    append_audit_log(
        db,
        action="workflow_release.publish",
        resource_type="workflow",
        resource_id=workflow.id,
        actor_user_id=actor.id,
        details={
            "release_id": release.id,
            "local_version": version.version_number,
            "release_digest": release.release_digest,
            "deployed_contract_checksum": deployed_checksum,
        },
    )
    db.flush()
    return release, version, receipt


def release_view(db: Session, release: ExecutionWorkflowRelease) -> dict[str, Any]:
    binding = _current_binding(db, release.id)
    workflow = (
        db.get(ExecutionWorkflow, release.workflow_id)
        if release.workflow_id is not None
        else None
    )
    versions = (
        db.query(ExecutionWorkflowVersion)
        .filter(ExecutionWorkflowVersion.release_id == release.id)
        .order_by(ExecutionWorkflowVersion.version_number)
        .all()
    )
    version_contracts = [
        {
            "local_version": version.version_number,
            "contract_format": version.contract_format,
            "release_digest": version.release_digest,
            "dependency_lock": deepcopy(version.dependency_lock),
            "dependency_lock_digest": version.dependency_lock_digest,
            "deployment_binding_snapshot": deepcopy(
                version.deployment_binding_snapshot
            ),
            "deployment_binding_digest": version.deployment_binding_digest,
            "asset_lock": deepcopy(version.asset_lock),
            "engine_version": version.engine_version,
            "deployed_contract_checksum": version.deployed_contract_checksum,
            "published_at": version.published_at.isoformat(),
        }
        for version in versions
    ]
    return {
        "id": release.id,
        "workflow_id": release.workflow_id,
        "source_slug": release.source_slug,
        "source_version": release.source_version,
        "release_digest": release.release_digest,
        "format_version": release.format_version,
        "status": release.status,
        "document": deepcopy(release.portable_document),
        "deployment_binding": (
            {
                "id": binding.id,
                "environment": binding.environment,
                "revision": binding.revision,
                "digest": binding.digest,
                "bindings": deepcopy(binding.binding),
                "created_at": binding.created_at.isoformat(),
            }
            if binding is not None
            else None
        ),
        "local_version": versions[-1].version_number if versions else None,
        "active_local_version": (
            workflow.published_version_number if workflow is not None else None
        ),
        "local_versions": [version.version_number for version in versions],
        "version_contracts": version_contracts,
        "created_at": release.created_at.isoformat(),
        "published_at": release.published_at.isoformat() if release.published_at else None,
    }


def export_version_release(
    db: Session, *, workflow_id: str, local_version: int
) -> dict[str, Any]:
    version = (
        db.query(ExecutionWorkflowVersion)
        .filter(
            ExecutionWorkflowVersion.workflow_id == workflow_id,
            ExecutionWorkflowVersion.version_number == local_version,
        )
        .one_or_none()
    )
    if version is None:
        raise not_found("流程版本", f"{workflow_id}@{local_version}")
    if version.release_id is None:
        raise ExecutionApiError(409, "version_not_release_v2", "该版本不是 v2 Release 发布版本")
    return deepcopy(_release_row(db, version.release_id).portable_document)


def _validate_rollback_target(
    db: Session, target: ExecutionWorkflowVersion
) -> None:
    lock = deepcopy(target.dependency_lock or {})
    recorded_lock_digest = lock.pop("digest", None)
    if (
        not recorded_lock_digest
        or canonical_sha256(lock) != recorded_lock_digest
        or target.dependency_lock_digest != recorded_lock_digest
    ):
        raise ExecutionApiError(
            409,
            "rollback_dependency_lock_invalid",
            "目标版本 dependency lock 摘要不一致",
        )
    profile_blockers = rollout_profile_blockers(lock.get("node_instances") or [])
    if profile_blockers:
        raise ExecutionApiError(
            409,
            "rollback_rollout_profile_blocked",
            "目标版本超出当前 rollout profile",
            details={"blockers": profile_blockers},
        )
    heartbeat_threshold = utcnow() - timedelta(
        seconds=int(settings.EXECUTION_WORKER_HEARTBEAT_TIMEOUT_SECONDS)
    )
    for instance in lock.get("node_instances") or []:
        if (
            canonical_sha256(instance.get("effective_input_schema"))
            != instance.get("effective_input_schema_digest")
            or canonical_sha256(instance.get("effective_output_schema"))
            != instance.get("effective_output_schema_digest")
        ):
            raise ExecutionApiError(
                409,
                "rollback_node_binding_mismatch",
                "目标版本的 effective schema 摘要不一致",
                details={"node_id": instance.get("node_id")},
            )
        compatible = (
            db.query(ExecutionWorkerNodeCapability.id)
            .join(
                ExecutionWorkerHeartbeat,
                ExecutionWorkerHeartbeat.worker_id
                == ExecutionWorkerNodeCapability.worker_id,
            )
            .filter(
                ExecutionWorkerNodeCapability.execution_binding_digest
                == instance.get("execution_binding_digest"),
                ExecutionWorkerNodeCapability.ready.is_(True),
                ExecutionWorkerHeartbeat.status == "running",
                ExecutionWorkerHeartbeat.last_seen_at >= heartbeat_threshold,
            )
            .first()
        )
        if compatible is None:
            raise ExecutionApiError(
                409,
                "rollback_node_capability_unavailable",
                "目标版本没有 fresh ready Worker 提供精确执行能力",
                details={"node_id": instance.get("node_id")},
            )

    registry_api = _registry_api()
    asset_lock = deepcopy(target.asset_lock or {})
    recorded_asset_digest = asset_lock.pop("digest", None)
    if not recorded_asset_digest or canonical_sha256(asset_lock) != recorded_asset_digest:
        raise ExecutionApiError(
            409,
            "rollback_asset_lock_invalid",
            "目标版本 asset lock 摘要不一致",
        )
    installed_assets = {
        (
            value.get("asset_id"),
            value.get("version"),
            value.get("digest"),
        )
        for value in (
            _public_value(item)
            for item in registry_api.get_installed_registry().list_assets()
        )
    }
    for asset in asset_lock.get("assets") or []:
        identity = (asset.get("asset_id"), asset.get("version"), asset.get("digest"))
        if identity not in installed_assets:
            raise ExecutionApiError(
                409,
                "rollback_asset_unavailable",
                "目标版本的精确资产当前不可用",
                details={"asset_id": asset.get("asset_id")},
            )

    snapshot = target.deployment_binding_snapshot or {}
    if set(snapshot) != {"environment", "revision", "bindings"}:
        raise ExecutionApiError(
            409,
            "rollback_binding_snapshot_invalid",
            "目标版本缺少完整 deployment binding 快照",
        )
    if canonical_sha256(
        {"environment": snapshot["environment"], "bindings": snapshot["bindings"]}
    ) != target.deployment_binding_digest:
        raise ExecutionApiError(
            409,
            "rollback_binding_digest_invalid",
            "目标版本 deployment binding 摘要不一致",
        )
    binding_issues = _live_binding_issues(db, snapshot["bindings"])
    if binding_issues:
        raise ExecutionApiError(
            409,
            "rollback_binding_stale",
            "目标版本的 deployment binding 已失效",
            details={"issues": binding_issues},
        )
    expected_deployed_checksum = canonical_sha256(
        {
            "definition_checksum": definition_checksum(target.definition),
            "capabilities": target.capabilities,
            "dependency_lock_digest": recorded_lock_digest,
            "asset_lock_digest": recorded_asset_digest,
            "deployment_binding_digest": target.deployment_binding_digest,
            "deployment_binding_revision": snapshot["revision"],
            "release_digest": target.release_digest,
        }
    )
    if expected_deployed_checksum != target.deployed_contract_checksum:
        raise ExecutionApiError(
            409,
            "rollback_deployed_checksum_invalid",
            "目标版本 deployed contract checksum 不一致",
        )


def rollback_workflow(
    db: Session,
    *,
    workflow_id: str,
    target_local_version: int,
    reason: str,
    actor: ExecutionUser,
) -> ExecutionWorkflowActivationReceipt:
    workflow = (
        db.query(ExecutionWorkflow)
        .filter(ExecutionWorkflow.id == workflow_id)
        .with_for_update()
        .one_or_none()
    )
    if workflow is None:
        raise not_found("流程", workflow_id)
    if workflow.management_mode != "release_v2":
        raise conflict("workflow_not_release_v2", "只有 v2-managed Workflow 可以使用此回滚接口")
    target = (
        db.query(ExecutionWorkflowVersion)
        .filter(
            ExecutionWorkflowVersion.workflow_id == workflow.id,
            ExecutionWorkflowVersion.version_number == target_local_version,
            ExecutionWorkflowVersion.contract_format == CONTRACT_FORMAT,
        )
        .one_or_none()
    )
    if target is None:
        raise not_found("v2 流程版本", f"{workflow_id}@{target_local_version}")
    if not target.dependency_lock or not target.deployment_binding_snapshot:
        raise ExecutionApiError(409, "rollback_target_incomplete", "目标版本缺少不可变运行快照")
    _validate_rollback_target(db, target)
    from_version = workflow.published_version_number
    workflow.published_version_number = target.version_number
    workflow.draft_definition = deepcopy(target.definition)
    workflow.capabilities = deepcopy(target.capabilities or {})
    workflow.required_input_count = len(
        (target.definition.get("input_schema") or {}).get("required") or []
    )
    workflow.draft_revision += 1
    workflow.updated_by_id = actor.id
    target_release = (
        db.get(ExecutionWorkflowRelease, target.release_id)
        if target.release_id
        else None
    )
    if target_release is not None:
        metadata = target_release.portable_document.get("release") or {}
        workflow.name = metadata.get("name") or workflow.name
        workflow.description = metadata.get("description")
        category = (
            db.query(ExecutionCategory)
            .filter(ExecutionCategory.key == metadata.get("category_key"))
            .one_or_none()
        )
        if category is not None:
            workflow.category_id = category.id
    receipt = ExecutionWorkflowActivationReceipt(
        workflow_id=workflow.id,
        release_id=target.release_id,
        action="rollback",
        from_version_number=from_version,
        to_version_number=target.version_number,
        release_digest=target.release_digest,
        deployment_binding_digest=target.deployment_binding_digest,
        actor_user_id=actor.id,
        reason=reason,
    )
    db.add(receipt)
    db.flush()
    append_audit_log(
        db,
        action="workflow_release.rollback",
        resource_type="workflow",
        resource_id=workflow.id,
        actor_user_id=actor.id,
        details={
            "from_version": from_version,
            "to_version": target.version_number,
            "receipt_id": receipt.id,
        },
    )
    return receipt


def _compat_node_dependency(node: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    registry_api = _registry_api()
    identity = (node["type"], int(node.get("type_version") or 1))
    matching = [
        item
        for item in registry_api.get_installed_registry().list_node_specs()
        if (item.type, item.type_version) == identity
        and item.source == "v1_registry_adapter"
    ]
    if len(matching) != 1:
        raise LookupError(
            f"v1 compatibility NodeSpec not installed: {identity[0]}@{identity[1]}"
        )
    spec = matching[0]
    value = _public_value(spec)
    dependency = {
        "type": value["type"],
        "type_version": value["type_version"],
        "contract_digest": value["contract_digest"],
        "implementation_digest": value["implementation_digest"],
    }
    return dependency, value


def _portable_slot_identifier(
    value: object,
    *,
    prefix: str,
    used: set[str],
) -> str:
    """Return a deterministic, Schema-valid and collision-free slot id."""

    raw = str(value or "").strip().lower()
    base = re.sub(r"[^a-z0-9_-]", "-", raw).strip("-")
    if not base or not base[0].isalpha():
        base = f"{prefix}-{base}".strip("-")
    base = base[:64].rstrip("-") or prefix
    candidate = base
    if candidate in used:
        suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        candidate = f"{base[:55].rstrip('-')}-{suffix}"
        ordinal = 2
        while candidate in used:
            ordinal_suffix = f"-{ordinal}"
            candidate = f"{base[:64-len(ordinal_suffix)].rstrip('-')}{ordinal_suffix}"
            ordinal += 1
    used.add(candidate)
    return candidate


def _migrate_v1_definition(
    db: Session,
    workflow: ExecutionWorkflow,
    definition: dict[str, Any],
    source_version: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    nodes = []
    dependencies: dict[tuple[str, int], dict[str, Any]] = {}
    packs: dict[str, dict[str, Any]] = {}
    legacy_operation_refs: set[str] = set()
    root_slot_by_id: dict[str, str] = {}
    role_slot_by_key: dict[str, str] = {}
    rule_slot_by_key: dict[str, str] = {}
    suggestions: dict[str, Any] = {group: {} for group in SLOT_GROUPS}
    root_slots = []
    role_slots = []
    rule_slots = []
    used_root_slots: set[str] = set()
    used_role_slots: set[str] = set()
    used_rule_slots: set[str] = set()
    for index, slot in enumerate(definition.get("root_slots") or []):
        root_id = str(slot.get("root_id") or f"root-{index}")
        slot_id = _portable_slot_identifier(
            root_id,
            prefix="root",
            used=used_root_slots,
        )
        root_slot_by_id[root_id] = slot_id
        root_slots.append(
            {
                "slot_id": slot_id,
                "name": str(slot.get("name") or root_id),
                "access": str(slot.get("access") or "read"),
                "required": True,
            }
        )
        suggestions["root_slots"][slot_id] = {"root_id": root_id}
    for raw_node in definition.get("nodes") or []:
        node = deepcopy(raw_node)
        dependency, spec = _compat_node_dependency(node)
        operation_ref = _registry_api().legacy_operation_ref_for_node(
            node["type"]
        )
        if operation_ref:
            legacy_operation_refs.add(operation_ref)
        identity = (dependency["type"], dependency["type_version"])
        dependencies[identity] = dependency
        packs[spec["pack_id"]] = {
            "pack_id": spec["pack_id"],
            "version_range": spec["pack_version"],
            "required_on": ["api", "worker"],
            **(
                {"distribution_digest": spec["distribution_digest"]}
                if spec.get("distribution_digest")
                else {}
            ),
        }
        config = node.get("config") or {}
        converted: dict[str, Any] = {}
        for key, value in config.items():
            if key == "root_id" and value in root_slot_by_id:
                converted["root_slot"] = root_slot_by_id[value]
            elif key.endswith("_root_id") and value in root_slot_by_id:
                converted[key[:-8] + "_root_slot"] = root_slot_by_id[value]
            elif key == "candidate_role" and isinstance(value, str) and value:
                slot_id = role_slot_by_key.get(value)
                if slot_id is None:
                    slot_id = _portable_slot_identifier(
                        value,
                        prefix="role",
                        used=used_role_slots,
                    )
                    role_slot_by_key[value] = slot_id
                    role_slots.append(
                        {
                            "slot_id": slot_id,
                            "name": f"Role for {value}",
                            "required_permissions": ["human_task.handle"],
                            "required": True,
                        }
                    )
                    suggestions["role_slots"][slot_id] = {
                        "role_key": value
                    }
                converted["candidate_role_slot"] = slot_id
            elif key == "match_rule" and isinstance(value, str) and value:
                slot_id = rule_slot_by_key.get(value)
                if slot_id is None:
                    slot_id = _portable_slot_identifier(
                        value,
                        prefix="rule",
                        used=used_rule_slots,
                    )
                    rule_slot_by_key[value] = slot_id
                    rule_slots.append(
                        {
                            "slot_id": slot_id,
                            "name": f"Project rule for {value}",
                            "rule_type": "project_match",
                            "contract_version": 1,
                            "required": True,
                        }
                    )
                    rule = (
                        db.query(ExecutionProjectRule)
                        .filter(ExecutionProjectRule.rule_key == value)
                        .one_or_none()
                    )
                    suggestions["rule_slots"][slot_id] = {
                        "rule_key": value,
                        **(
                            {"revision": rule.revision}
                            if rule is not None
                            else {}
                        ),
                    }
                converted["rule_slot"] = slot_id
            else:
                converted[key] = value
        node["config"] = converted
        node.pop("disabled", None)
        nodes.append(node)
    connector_dependencies: list[dict[str, Any]] = []
    if legacy_operation_refs:
        registry = _registry_api().get_installed_registry()
        connector = registry.resolve_connector("legacy_fibrecheck", "*")
        connector_value = _public_value(connector)
        operation_dependencies: list[dict[str, Any]] = []
        for operation_ref in sorted(legacy_operation_refs):
            ref_name, ref_version = operation_ref.rsplit("@", 1)
            operation_name = ref_name[len("legacy_fibrecheck.") :]
            operation = registry.resolve_operation(
                "legacy_fibrecheck",
                connector_value["version"],
                operation_name,
                int(ref_version),
            )
            operation_value = _public_value(operation)
            operation_dependencies.append(
                {
                    "operation": operation_value["operation"],
                    "contract_version": operation_value["contract_version"],
                    "contract_digest": operation_value["contract_digest"],
                }
            )
        connector_dependencies.append(
            {
                "connector_id": connector_value["connector_id"],
                "version_range": connector_value["version"],
                "distribution_digest": connector_value[
                    "distribution_digest"
                ],
                "operations": operation_dependencies,
                "queries": [],
            }
        )
        owner_pack = registry.resolve_pack(
            connector_value["pack_id"], connector_value["pack_version"]
        )
        owner_pack_value = _public_value(owner_pack)
        packs[owner_pack_value["pack_id"]] = {
            "pack_id": owner_pack_value["pack_id"],
            "version_range": owner_pack_value["pack_version"],
            "distribution_digest": owner_pack_value[
                "distribution_digest"
            ],
            "required_on": ["api", "bridge"],
        }
    edges = []
    for raw_edge in definition.get("edges") or []:
        edge = deepcopy(raw_edge)
        edge["join_policy"] = edge.get("join_policy") or "all"
        edges.append(edge)
    credential_slots = []
    used_credential_slots: set[str] = set()
    for slot in definition.get("credential_slots") or []:
        system_key = str(slot.get("system_key") or "legacy_inspection")
        slot_id = _portable_slot_identifier(
            slot.get("name") or system_key,
            prefix="credential",
            used=used_credential_slots,
        )
        credential_slots.append(
            {
                "slot_id": slot_id,
                "name": slot_id,
                "connector_id": (
                    "legacy_fibrecheck"
                    if system_key == "legacy_inspection"
                    else system_key
                ),
                "credential_kind": "account_password",
                "required": True,
            }
        )
        suggestions["credential_slots"][slot_id] = {
            "system_key": system_key
        }
    migrated = {
        "format": "textile-workflow-release",
        "format_version": "2.0",
        "release": {
            "slug": workflow.slug,
            "release_version": source_version,
            "name": workflow.name,
            "description": workflow.description or "",
            "category_key": workflow.category.key,
            "release_note": "Deterministic v1 compatibility candidate",
        },
        "dependencies": {
            "engine": {"version_range": ">=2.0.0 <3.0.0"},
            "node_types": sorted(dependencies.values(), key=lambda item: (item["type"], item["type_version"])),
            "packs": sorted(packs.values(), key=lambda item: item["pack_id"]),
            "connectors": connector_dependencies,
        },
        "resources": {
            "root_slots": root_slots,
            "credential_slots": credential_slots,
            "role_slots": role_slots,
            "rule_slots": rule_slots,
        },
        "assets": [],
        "capabilities": {
            "declared": [],
            "side_effect_level": "none",
            "requires_human_approval": False,
        },
        "definition": {
            "schema_version": "2.0",
            "input_schema": deepcopy(definition.get("input_schema") or {"type": "object"}),
            "global_schema": deepcopy(definition.get("global_schema") or {"type": "object"}),
            "output_schema": {"type": "object", "properties": {}, "additionalProperties": True},
            "global_defaults": {},
            "nodes": nodes,
            "edges": edges,
        },
        "fixtures": [],
        "migration": {
            "source_format": "textile-execution-workflow",
            "source_format_version": "1.0",
            "source_digest": definition_checksum(definition),
        },
        "integrity": {
            "algorithm": "sha256",
            "canonicalization": "RFC8785",
            "scope": "document_without_integrity",
            "digest": "0" * 64,
            "signatures": [],
        },
    }
    # Compute the capability summary with the same resolver, then seal digest.
    scratch: list[dict[str, str]] = []
    _lock, computed = _resolve_dependencies(migrated, scratch)
    migrated["capabilities"] = computed
    migrated["integrity"]["digest"] = _release_digest(migrated)
    return migrated, suggestions


P2_COMPLETE_WORKFLOW_SLUGS = {
    "electron-source-selection",
    "hemp-cotton-source-selection",
    "special-wool-source-selection",
    "system-controlled-xlsx-write-test",
}


def _installed_node_for_source(
    node_type: str,
    type_version: int,
    source: str,
):
    matching = [
        item
        for item in _registry_api().get_installed_registry().list_node_specs()
        if item.type == node_type
        and item.type_version == type_version
        and item.source == source
    ]
    if len(matching) != 1:
        raise LookupError(
            f"exact {source} NodeSpec unavailable: {node_type}@{type_version}"
        )
    return matching[0]


def _rebuild_candidate_dependencies(candidate: dict[str, Any]) -> None:
    dependencies: dict[tuple[str, int], dict[str, Any]] = {}
    packs: dict[str, dict[str, Any]] = {}
    for node in candidate["definition"]["nodes"]:
        identity = (node["type"], int(node.get("type_version") or 1))
        preferred_source = (
            "resource"
            if node.pop("__native_p2", False)
            else "v1_registry_adapter"
        )
        installed = _installed_node_for_source(
            identity[0], identity[1], preferred_source
        )
        dependencies[identity] = {
            "type": identity[0],
            "type_version": identity[1],
            "contract_digest": installed.contract_digest,
            "implementation_digest": installed.implementation_digest,
        }
        pack = _registry_api().get_installed_registry().resolve_pack(
            installed.pack_id,
            installed.pack_version,
            installed.distribution_digest,
        )
        packs[pack.pack_id] = {
            "pack_id": pack.pack_id,
            "version_range": pack.pack_version,
            "distribution_digest": pack.distribution_digest,
            "required_on": ["api", "worker"],
        }
    # Connector dependencies from deferred compatibility nodes remain intact;
    # pack ownership for them is already present in the compat candidate.
    for existing in candidate["dependencies"].get("packs") or []:
        if existing.get("required_on") == ["api", "bridge"]:
            packs[existing["pack_id"]] = existing
    candidate["dependencies"]["engine"] = {
        "version_range": ">=2.1.0 <3.0.0"
    }
    candidate["dependencies"]["node_types"] = sorted(
        dependencies.values(),
        key=lambda item: (item["type"], item["type_version"]),
    )
    candidate["dependencies"]["packs"] = sorted(
        packs.values(), key=lambda item: item["pack_id"]
    )


def _native_marker(node: dict[str, Any]) -> dict[str, Any]:
    node["__native_p2"] = True
    return node


def _native_p2_candidate(
    workflow: ExecutionWorkflow,
    compat_candidate: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    candidate = deepcopy(compat_candidate)
    transformations: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    nodes = candidate["definition"]["nodes"]
    edges = candidate["definition"]["edges"]
    if workflow.slug in {
        "electron-source-selection",
        "hemp-cotton-source-selection",
        "special-wool-source-selection",
    }:
        node_by_id = {node["id"]: node for node in nodes}
        query = node_by_id["query"]
        original_query_type = query["type"]
        original_config = deepcopy(query.get("config") or {})
        query["type"] = "file.query"
        query["type_version"] = 1
        query["config"] = {
            "root_slot": original_config["root_slot"],
            "recent_days": original_config.get("recent_days", 7),
            "limit": original_config.get("limit", 6),
            "sort": "modified_desc",
            "projection": [
                "id",
                "root_id",
                "relative_path",
                "name",
                "suffix",
                "size",
                "modified_at",
                "category",
                "fingerprint",
                "metadata",
            ],
            **(
                {"extensions": [".sif", ".bmp", ".txt"]}
                if original_query_type == "electron.group"
                else {}
            ),
        }
        _native_marker(query)
        selection_source = "$.nodes.query.output.items"
        if original_query_type == "electron.group":
            group_id = f"{query['id']}-group"
            group_node = _native_marker(
                {
                    "id": group_id,
                    "type": "file.group",
                    "type_version": 1,
                    "name": "按采集名称分组",
                    "config": {
                        "group_strategy": "same_acquisition_name",
                        "limit": original_config.get("limit", 6),
                    },
                    "input_mapping": {
                        "items": "$.nodes.query.output.items"
                    },
                    "ui": {
                        "x": (query.get("ui") or {}).get("x", 280) + 130,
                        "y": (query.get("ui") or {}).get("y", 180),
                    },
                }
            )
            query_index = nodes.index(query)
            nodes.insert(query_index + 1, group_node)
            outgoing = [edge for edge in edges if edge["source"] == query["id"]]
            for edge in outgoing:
                edge["target"] = group_id
            edges.append(
                {
                    "id": f"{query['id']}-group-to-select",
                    "source": group_id,
                    "target": "select",
                    "join_policy": "all",
                }
            )
            selection_source = f"$.nodes.{group_id}.output.groups"
            transformations.append(
                {
                    "migration_id": "electron-group-v1-to-file-query-group-v1",
                    "source_node_id": query["id"],
                    "source": {"type": "electron.group", "type_version": 1},
                    "targets": [
                        {"node_id": query["id"], "type": "file.query", "type_version": 1},
                        {"node_id": group_id, "type": "file.group", "type_version": 1},
                    ],
                    "edge_changes": ["query outgoing edge retargeted through derived group node"],
                }
            )
        else:
            transformations.append(
                {
                    "migration_id": "file-index-query-v1-to-file-query-v1",
                    "source_node_id": query["id"],
                    "source": {"type": original_query_type, "type_version": 1},
                    "targets": [{"node_id": query["id"], "type": "file.query", "type_version": 1}],
                    "config_changes": ["root_slot preserved; stable sort and projection frozen"],
                }
            )
        select = node_by_id["select"]
        old_select_type = select["type"]
        old_select_config = select.get("config") or {}
        select["type"] = "human.select"
        select["type_version"] = 1
        select["config"] = {
            "title": old_select_config.get("title") or "选择文件",
            "description": old_select_config.get("description") or "",
            "item_kind": "artifact",
            "min_selected": 1,
            "max_selected": 100 if old_select_config.get("allow_multiple", True) else 1,
            "require_primary": bool(old_select_config.get("require_primary", False)),
            "auto_submit_single_candidate": bool(
                old_select_config.get("auto_submit_single_candidate", False)
            ),
        }
        select["input_mapping"] = {"items": selection_source}
        _native_marker(select)
        transformations.append(
            {
                "migration_id": "human-file-selection-v1-to-human-select-v1",
                "source_node_id": select["id"],
                "source": {"type": old_select_type, "type_version": 1},
                "targets": [{"node_id": select["id"], "type": "human.select", "type_version": 1}],
                "mapping_changes": [f"items <- {selection_source}"],
            }
        )
        result = node_by_id["result"]
        old_result_type = result["type"]
        result["type"] = "data.aggregate"
        result["type_version"] = 1
        result["config"] = {"mode": "collect", "conflict": "error"}
        result["input_mapping"] = {
            "items": "$.nodes.select.output.selected_items"
        }
        _native_marker(result)
        transformations.append(
            {
                "migration_id": "result-aggregate-v1-to-data-aggregate-v1",
                "source_node_id": result["id"],
                "source": {"type": old_result_type, "type_version": 1},
                "targets": [{"node_id": result["id"], "type": "data.aggregate", "type_version": 1}],
            }
        )
    elif workflow.slug == "system-controlled-xlsx-write-test":
        node_by_id = {node["id"]: node for node in nodes}
        replacements = {
            "classify": ("workbook.classify", 1),
            "extract": ("workbook.extract_fields", 1),
            "copy": ("workbook.copy", 1),
            "write": ("workbook.write_cells", 2),
            "verify": ("workbook.verify", 2),
            "confirm": ("human.approval", 1),
            "publish": ("artifact.publish", 2),
        }
        for node_id, target in replacements.items():
            node = node_by_id[node_id]
            source_identity = {"type": node["type"], "type_version": node.get("type_version", 1)}
            node["type"], node["type_version"] = target
            _native_marker(node)
            transformations.append(
                {
                    "migration_id": f"{source_identity['type'].replace('.', '-')}-to-{target[0].replace('.', '-')}-v{target[1]}",
                    "source_node_id": node_id,
                    "source": source_identity,
                    "targets": [{"node_id": node_id, "type": target[0], "type_version": target[1]}],
                }
            )
        node_by_id["classify"]["config"] = {
            "data_only": True,
            "types": [
                {
                    "name": "controlled-test-workbook",
                    "features": [
                        {"sheet": "Sheet1", "cell": "A1", "operator": "nonempty"}
                    ],
                }
            ],
        }
        node_by_id["extract"]["config"] = {
            "data_only": True,
            "fail_on_error": False,
            "fields": [
                {"name": "source_a1", "sheet": "Sheet1", "cell": "A1", "required": False}
            ],
        }
        node_by_id["copy"]["config"] = {}
        node_by_id["write"]["config"] = {}
        node_by_id["verify"]["config"] = {}
        node_by_id["confirm"]["config"] = {"title": "确认测试发布"}
        node_by_id["confirm"]["input_mapping"] = {
            "subject": {
                "type": "workbook_mutation",
                "digest": "$.nodes.verify.output.subject_digest",
                "summary": "$.nodes.verify.output.verification",
            }
        }
        publish_config = node_by_id["publish"].get("config") or {}
        node_by_id["publish"]["config"] = {
            "publish_root_slot": publish_config["publish_root_slot"]
        }
        node_by_id["publish"]["input_mapping"] = {
            "mutation_id": "$.inputs.mutation_id",
            "working_copy": "$.nodes.copy.output.working_copy",
            "target": "$.inputs.target",
            "verification_receipt": "$.nodes.verify.output",
            "approval_receipt_id": "$.nodes.confirm.output.approval_receipt.id",
        }
    else:
        for node in nodes:
            if node["type"].startswith("external."):
                phase = "P4"
                code = "external_connector_deferred"
            elif node["type"] not in {"core.start", "core.end"}:
                phase = "P3"
                code = "domain_primitive_deferred"
            else:
                continue
            blockers.append(
                {
                    "phase": phase,
                    "code": code,
                    "node_id": node["id"],
                    "message": f"{node['type']} remains a compatibility node",
                }
            )
    candidate["definition"]["edges"] = sorted(
        edges, key=lambda edge: str(edge.get("id"))
    )
    candidate["release"]["release_note"] = (
        "Deterministic native P2 migration candidate"
    )
    # Migration diagnostics belong to the preview envelope.  Keep the portable
    # Release document itself within the frozen v2 schema so the downloaded
    # candidate can be fed straight back into content preflight.
    _rebuild_candidate_dependencies(candidate)
    scratch: list[dict[str, str]] = []
    _lock, computed = _resolve_dependencies(candidate, scratch)
    candidate["capabilities"] = computed
    candidate["integrity"]["digest"] = _release_digest(candidate)
    return candidate, transformations, blockers


def preview_v1_migration(
    db: Session,
    *,
    workflow_id: str,
    source: str,
    actor: ExecutionUser,
    target_profile: str = "compat_v1",
) -> dict[str, Any]:
    del actor
    workflow = db.get(ExecutionWorkflow, workflow_id)
    if workflow is None:
        raise not_found("流程", workflow_id)
    if workflow.management_mode != "draft_v1":
        raise conflict("workflow_already_release_v2", "该 Workflow 已由 v2 Release 管理")
    if source == "published":
        if workflow.published_version_number is None:
            raise ExecutionApiError(409, "workflow_not_published", "流程尚无已发布版本")
        version = next(
            item for item in workflow.versions if item.version_number == workflow.published_version_number
        )
        definition = version.definition
        source_version = version.version_number
    else:
        definition = workflow.draft_definition
        source_version = workflow.draft_revision
    candidate, suggestions = _migrate_v1_definition(
        db, workflow, definition, source_version
    )
    transformations: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    if target_profile == "native_p2":
        candidate, transformations, blockers = _native_p2_candidate(
            workflow, candidate
        )
    issues = _validate_document_shape(candidate)
    if not issues:
        issues.extend(_verify_integrity(candidate, _release_digest(candidate)))
        _lock, _capabilities = _resolve_dependencies(candidate, issues)
        issues.extend(_content_semantic_issues(candidate))
    content_valid = not any(issue["level"] == "error" for issue in issues)
    dependency_source = {}
    for dependency in candidate["dependencies"].get("node_types") or []:
        try:
            installed = _registry_api().resolve_node_spec(
                dependency["type"],
                dependency["type_version"],
                dependency["contract_digest"],
            )
        except (LookupError, TypeError, ValueError):
            continue
        dependency_source[(installed.type, installed.type_version)] = installed.source
    native_count = sum(
        dependency_source.get((node["type"], node["type_version"])) == "resource"
        for node in candidate["definition"]["nodes"]
    )
    compatibility_count = len(candidate["definition"]["nodes"]) - native_count
    migration_status = (
        "p2_complete"
        if target_profile == "native_p2"
        and workflow.slug in P2_COMPLETE_WORKFLOW_SLUGS
        and not blockers
        else "p3_p4_deferred"
        if target_profile == "native_p2"
        else "compatibility_preview"
    )
    return {
        "workflow_id": workflow.id,
        "source": source,
        "target_profile": target_profile,
        "migration_status": migration_status,
        "candidate": candidate,
        "binding_suggestions": suggestions,
        "transformations": transformations,
        "native_node_count": native_count,
        "compatibility_node_count": compatibility_count,
        "blockers": blockers,
        "diff": {
            "source_schema_version": definition.get("schema_version"),
            "target_format_version": "2.0",
            "node_count": len(candidate["definition"]["nodes"]),
            "edge_count": len(candidate["definition"]["edges"]),
            "active_pointer_changed": False,
            "transformations": transformations,
            "config_changes": [
                item
                for transformation in transformations
                for item in transformation.get("config_changes", [])
            ],
            "mapping_changes": [
                item
                for transformation in transformations
                for item in transformation.get("mapping_changes", [])
            ],
            "edge_changes": [
                item
                for transformation in transformations
                for item in transformation.get("edge_changes", [])
            ],
        },
        "content_valid": content_valid,
        "publish_ready": False,
        "issues": issues,
    }


def receipt_view(receipt: ExecutionWorkflowActivationReceipt) -> dict[str, Any]:
    return {
        "id": receipt.id,
        "workflow_id": receipt.workflow_id,
        "release_id": receipt.release_id,
        "action": receipt.action,
        "from_version": receipt.from_version_number,
        "to_version": receipt.to_version_number,
        "release_digest": receipt.release_digest,
        "deployment_binding_digest": receipt.deployment_binding_digest,
        "reason": receipt.reason,
        "created_at": receipt.created_at.isoformat(),
    }
