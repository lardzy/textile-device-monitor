from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any
from urllib.parse import unquote

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.execution.registry import NodeRegistry, node_registry


NODE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
ROOT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
ALLOWED_JOIN_POLICIES = {"all", "any"}
ALLOWED_CONDITION_OPERATORS = {
    "truthy",
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "contains",
}
NODE_ROOT_ACCESS_REQUIREMENTS = {
    "file.index_query": {"root_id": {"read"}},
    "file.regenerated_fiber_count_method": {"root_id": {"read"}},
    "file.regenerated_fiber_area_method": {"root_id": {"read"}},
    "electron.group": {"root_id": {"read"}},
    "file.electron_microscopy_gbt36422": {"root_id": {"read"}},
    "file.paper_fiber_gbt4688_qualitative": {"root_id": {"read"}},
    "workbook.microscopy_original_record": {"staging_root_id": {"write"}},
    "workbook.microscopy_check_record": {"staging_root_id": {"write"}},
    "workbook.copy": {"staging_root_id": {"write"}},
    "artifact.publish": {"publish_root_id": {"publish"}},
    # 报告图片放置节点（report_image_placement 标记的 human.input）写入目标根。
    "human.input": {"target_root_id": {"write"}},
}
SECRET_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "credential",
    "credential_value",
}
RUN_CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "inspection_number": {"type": "string"},
        "mode": {"type": "string"},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    path: str
    level: str = "error"

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "level": self.level,
        }


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    issues: tuple[ValidationIssue, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "issues": [issue.as_dict() for issue in self.issues],
        }


def canonical_json(document: dict[str, Any]) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def definition_checksum(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def workflow_contract_checksum(
    definition: dict[str, Any],
    capabilities: dict[str, Any],
) -> str:
    """Hash every immutable runtime contract field published with a workflow."""

    return hashlib.sha256(
        canonical_json(
            {
                "definition": definition,
                "capabilities": capabilities,
            }
        ).encode("utf-8")
    ).hexdigest()


def validate_json_instance(
    schema: dict[str, Any],
    instance: Any,
    *,
    path_prefix: str,
) -> ValidationResult:
    issues: list[ValidationIssue] = []
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return ValidationResult(
            valid=False,
            issues=(
                ValidationIssue(
                    "json_schema_invalid",
                    "数据结构定义不合法",
                    path_prefix,
                ),
            ),
        )
    validator = Draft202012Validator(schema)
    for error in sorted(
        validator.iter_errors(instance),
        key=lambda item: list(item.absolute_path),
    ):
        suffix = "".join(
            f"[{item}]" if isinstance(item, int) else f".{item}"
            for item in error.absolute_path
        )
        issues.append(
            ValidationIssue(
                "schema_validation_failed",
                error.message,
                f"{path_prefix}{suffix}",
            )
        )
    return ValidationResult(valid=not issues, issues=tuple(issues))


def _validate_schema_document(
    value: Any,
    *,
    path: str,
    issues: list[ValidationIssue],
) -> None:
    if not isinstance(value, dict):
        issues.append(
            ValidationIssue(
                "json_schema_required",
                "数据结构定义必须是 JSON Schema 对象",
                path,
            )
        )
        return
    try:
        Draft202012Validator.check_schema(value)
    except SchemaError as exc:
        issues.append(
            ValidationIssue(
                "json_schema_invalid",
                f"数据结构定义不合法：{exc.message}",
                path,
            )
        )


def _validate_condition(
    condition: Any,
    *,
    path: str,
    issues: list[ValidationIssue],
) -> None:
    if condition in (None, "", "default") or isinstance(condition, bool):
        return
    if not isinstance(condition, dict):
        issues.append(
            ValidationIssue(
                "condition_invalid",
                "分支条件必须是对象、布尔值或 default",
                path,
            )
        )
        return
    condition_path = condition.get("path")
    if (
        not isinstance(condition_path, str)
        or not condition_path.startswith("$.")
        or len(condition_path) > 500
    ):
        issues.append(
            ValidationIssue(
                "condition_path_invalid",
                "条件 path 必须是以 $. 开头的数据路径",
                f"{path}.path",
            )
        )
    operator = condition.get("operator", "truthy")
    if operator not in ALLOWED_CONDITION_OPERATORS:
        issues.append(
            ValidationIssue(
                "condition_operator_invalid",
                f"不支持的条件运算符：{operator}",
                f"{path}.operator",
            )
        )
    if operator in {"in", "not_in"} and not isinstance(
        condition.get("value"),
        (list, tuple, set, str),
    ):
        issues.append(
            ValidationIssue(
                "condition_value_invalid",
                f"{operator} 条件的 value 必须是数组或字符串",
                f"{path}.value",
            )
        )


def runtime_definition(document: dict[str, Any]) -> dict[str, Any]:
    """Return the executable DAG, excluding parked draft nodes and their edges."""

    value = deepcopy(document)
    raw_nodes = value.get("nodes")
    if not isinstance(raw_nodes, list):
        return value
    active_nodes = [
        node
        for node in raw_nodes
        if not isinstance(node, dict) or node.get("disabled") is not True
    ]
    active_ids = {
        node.get("id")
        for node in active_nodes
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    value["nodes"] = active_nodes
    raw_edges = value.get("edges")
    if isinstance(raw_edges, list):
        value["edges"] = [
            edge
            for edge in raw_edges
            if not isinstance(edge, dict)
            or (
                edge.get("source") in active_ids
                and edge.get("target") in active_ids
            )
        ]
    return value


def _looks_like_absolute_path(value: str) -> bool:
    decoded = unquote(value).strip()
    if decoded.startswith(("/", "\\\\", "smb://", "file://")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", decoded):
        return True
    try:
        return PurePath(decoded).is_absolute()
    except (TypeError, ValueError):
        return False


def _inspect_values(value: Any, path: str, issues: list[ValidationIssue]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            nested_path = f"{path}.{key}"
            if key.lower() in SECRET_KEYS and nested not in (None, "", {}, []):
                issues.append(
                    ValidationIssue(
                        "embedded_secret",
                        "流程定义不得包含真实凭据",
                        nested_path,
                    )
                )
            _inspect_values(nested, nested_path, issues)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _inspect_values(nested, f"{path}[{index}]", issues)
    elif isinstance(value, str) and _looks_like_absolute_path(value):
        issues.append(
            ValidationIssue(
                "absolute_path_forbidden",
                "流程定义只能引用根目录别名和相对路径",
                path,
            )
        )


def _inspect_declared_root_references(
    value: Any,
    *,
    path: str,
    declared_root_ids: set[str],
    issues: list[ValidationIssue],
) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            nested_path = f"{path}.{key}"
            if (
                (key == "root_id" or key.endswith("_root_id"))
                and nested not in (None, "")
                and nested not in declared_root_ids
            ):
                issues.append(
                    ValidationIssue(
                        "undeclared_root_reference",
                        "节点引用的根目录必须在 root_slots 中声明",
                        nested_path,
                    )
                )
            _inspect_declared_root_references(
                nested,
                path=nested_path,
                declared_root_ids=declared_root_ids,
                issues=issues,
            )
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _inspect_declared_root_references(
                nested,
                path=f"{path}[{index}]",
                declared_root_ids=declared_root_ids,
                issues=issues,
            )


def _iter_mapping_references(
    value: Any,
    *,
    path: str,
) -> list[tuple[str, str]]:
    references: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            references.extend(
                _iter_mapping_references(
                    nested,
                    path=f"{path}.{key}",
                )
            )
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            references.extend(
                _iter_mapping_references(
                    nested,
                    path=f"{path}[{index}]",
                )
            )
    elif isinstance(value, str) and value.startswith("$."):
        references.append((path, value))
    return references


def _schema_at_path(
    schema: Any,
    segments: list[str],
) -> tuple[dict[str, Any] | None, bool]:
    """Resolve a declared JSON Schema path.

    The boolean indicates that a declared object contract rejected a segment.
    Empty/dynamic schemas intentionally return ``(None, False)`` so they do not
    claim type information they cannot prove.
    """

    current = schema if isinstance(schema, dict) else {}
    for segment in segments:
        raw_type = current.get("type")
        types = (
            {raw_type}
            if isinstance(raw_type, str)
            else set(raw_type)
            if isinstance(raw_type, list)
            else set()
        )
        if "array" in types and segment.isdigit():
            current = (
                current.get("items")
                if isinstance(current.get("items"), dict)
                else {}
            )
            continue
        properties = current.get("properties")
        if isinstance(properties, dict) and properties:
            if segment not in properties:
                return None, True
            nested = properties[segment]
            current = nested if isinstance(nested, dict) else {}
            continue
        return None, False
    return (current or None), False


def _schema_types(schema: dict[str, Any] | None) -> set[str]:
    if not schema:
        return set()
    raw = schema.get("type")
    values = {raw} if isinstance(raw, str) else set(raw or [])
    # JSON Schema integers are compatible with number inputs.
    if "integer" in values:
        values.add("number")
    return values - {"null"}


def _node_is_upstream(
    source_id: str,
    target_id: str,
    outgoing: dict[str, list[str]],
) -> bool:
    queue = deque(outgoing.get(source_id, ()))
    visited: set[str] = set()
    while queue:
        node_id = queue.popleft()
        if node_id == target_id:
            return True
        if node_id in visited:
            continue
        visited.add(node_id)
        queue.extend(outgoing.get(node_id, ()))
    return False


def _validate_mapping_reference(
    *,
    expression: str,
    path: str,
    target_node_id: str,
    target_schema: dict[str, Any] | None,
    input_schema: dict[str, Any],
    global_schema: dict[str, Any],
    node_by_id: dict[str, dict[str, Any]],
    node_contract_by_id: dict[str, Any],
    outgoing: dict[str, list[str]],
    issues: list[ValidationIssue],
) -> None:
    source_schema: dict[str, Any] | None = None
    rejected_path = False
    if expression in {"$.inputs", "$.globals", "$.run"}:
        source_schema = {
            "$.inputs": input_schema,
            "$.globals": global_schema,
            "$.run": RUN_CONTEXT_SCHEMA,
        }[expression]
    elif expression.startswith("$.inputs."):
        source_schema, rejected_path = _schema_at_path(
            input_schema,
            expression[len("$.inputs.") :].split("."),
        )
    elif expression.startswith("$.globals."):
        source_schema, rejected_path = _schema_at_path(
            global_schema,
            expression[len("$.globals.") :].split("."),
        )
    elif expression.startswith("$.run."):
        source_schema, rejected_path = _schema_at_path(
            RUN_CONTEXT_SCHEMA,
            expression[len("$.run.") :].split("."),
        )
    elif expression.startswith("$.nodes."):
        remainder = expression[len("$.nodes.") :]
        source_id = next(
            (
                node_id
                for node_id in sorted(node_by_id, key=len, reverse=True)
                if remainder.startswith(f"{node_id}.")
            ),
            None,
        )
        if source_id is None:
            issues.append(
                ValidationIssue(
                    "mapping_source_node_missing",
                    "输入映射引用的来源节点不存在",
                    path,
                )
            )
            return
        if not _node_is_upstream(source_id, target_node_id, outgoing):
            issues.append(
                ValidationIssue(
                    "mapping_source_not_upstream",
                    "输入映射只能引用当前节点的上游节点",
                    path,
                )
            )
            return
        suffix = remainder[len(source_id) + 1 :]
        parts = suffix.split(".") if suffix else []
        if not parts or parts[0] not in {"output", "status"}:
            issues.append(
                ValidationIssue(
                    "mapping_node_path_invalid",
                    "节点引用必须指向 output 或 status",
                    path,
                )
            )
            return
        if parts[0] == "status":
            if len(parts) != 1:
                rejected_path = True
            else:
                source_schema = {"type": "string"}
        else:
            contract = node_contract_by_id.get(source_id)
            output_schema = (
                contract.output_schema if contract is not None else {}
            )
            source_schema, rejected_path = _schema_at_path(
                output_schema,
                parts[1:],
            )
    else:
        issues.append(
            ValidationIssue(
                "mapping_reference_invalid",
                "映射引用必须从 inputs、globals、run 或上游节点 output 开始",
                path,
            )
        )
        return

    if rejected_path:
        issues.append(
            ValidationIssue(
                "mapping_source_path_unknown",
                "输入映射引用的字段不在来源契约中",
                path,
            )
        )
        return
    source_types = _schema_types(source_schema)
    target_types = _schema_types(target_schema)
    if source_types and target_types and source_types.isdisjoint(target_types):
        issues.append(
            ValidationIssue(
                "mapping_type_mismatch",
                "输入映射的来源类型与节点输入契约不兼容",
                path,
            )
        )


def validate_definition(
    document: dict[str, Any],
    *,
    registry: NodeRegistry = node_registry,
    for_publish: bool = False,
) -> ValidationResult:
    raw_nodes = document.get("nodes")
    if isinstance(raw_nodes, list) and any(
        isinstance(node, dict) and node.get("disabled") is True
        for node in raw_nodes
    ):
        executable = runtime_definition(document)
        base = validate_definition(
            executable,
            registry=registry,
            for_publish=for_publish,
        )
        parked_issues: list[ValidationIssue] = []
        seen_ids: set[str] = set()
        for index, node in enumerate(raw_nodes):
            path = f"$.nodes[{index}]"
            if not isinstance(node, dict):
                continue
            node_id = node.get("id")
            if isinstance(node_id, str):
                if node_id in seen_ids:
                    parked_issues.append(
                        ValidationIssue(
                            "node_id_duplicate",
                            "节点 ID 重复",
                            f"{path}.id",
                        )
                    )
                seen_ids.add(node_id)
            if "disabled" in node and not isinstance(node["disabled"], bool):
                parked_issues.append(
                    ValidationIssue(
                        "node_disabled_invalid",
                        "disabled 必须是布尔值",
                        f"{path}.disabled",
                    )
                )
            if node.get("disabled") is True:
                _inspect_values(
                    node.get("config") or {},
                    f"{path}.config",
                    parked_issues,
                )
                _inspect_values(
                    node.get("input_mapping") or {},
                    f"{path}.input_mapping",
                    parked_issues,
                )
        issues = (*base.issues, *parked_issues)
        return ValidationResult(
            valid=not any(issue.level == "error" for issue in issues),
            issues=issues,
        )

    issues: list[ValidationIssue] = []
    if document.get("schema_version") != "1.0":
        issues.append(
            ValidationIssue(
                "unsupported_schema_version",
                "仅支持 schema_version=1.0",
                "$.schema_version",
            )
        )

    _validate_schema_document(
        document.get("input_schema"),
        path="$.input_schema",
        issues=issues,
    )

    root_slots = document.get("root_slots")
    declared_root_ids: set[str] = set()
    root_access_by_id: dict[str, str] = {}
    if not isinstance(root_slots, list):
        issues.append(
            ValidationIssue(
                "root_slots_invalid",
                "root_slots 必须是数组",
                "$.root_slots",
            )
        )
        root_slots = []
    for index, slot in enumerate(root_slots):
        path = f"$.root_slots[{index}]"
        if not isinstance(slot, dict):
            issues.append(
                ValidationIssue("root_slot_invalid", "根目录槽位必须是对象", path)
            )
            continue
        root_id = slot.get("root_id")
        if not isinstance(root_id, str) or not ROOT_ID_PATTERN.fullmatch(root_id):
            issues.append(
                ValidationIssue(
                    "root_id_invalid",
                    "root_id 不合法",
                    f"{path}.root_id",
                )
            )
        elif root_id in declared_root_ids:
            issues.append(
                ValidationIssue(
                    "root_id_duplicate",
                    "同一流程不能重复声明根目录",
                    f"{path}.root_id",
                )
            )
        else:
            declared_root_ids.add(root_id)
        access = slot.get("access", "read")
        if access not in {"read", "write", "publish"}:
            issues.append(
                ValidationIssue(
                    "root_access_invalid",
                    "根目录 access 只能是 read、write 或 publish",
                    f"{path}.access",
                )
            )
        elif isinstance(root_id, str):
            root_access_by_id[root_id] = access
        _inspect_values(slot, path, issues)

    credential_slots = document.get("credential_slots")
    if not isinstance(credential_slots, list):
        issues.append(
            ValidationIssue(
                "credential_slots_invalid",
                "credential_slots 必须是数组",
                "$.credential_slots",
            )
        )
        credential_slots = []
    credential_names: set[str] = set()
    credential_system_by_name: dict[str, str] = {}
    for index, slot in enumerate(credential_slots):
        path = f"$.credential_slots[{index}]"
        if not isinstance(slot, dict):
            issues.append(
                ValidationIssue(
                    "credential_slot_invalid",
                    "凭据槽位必须是对象",
                    path,
                )
            )
            continue
        name = slot.get("name")
        system_key = slot.get("system_key")
        if not isinstance(name, str) or not name.strip():
            issues.append(
                ValidationIssue(
                    "credential_slot_name_required",
                    "凭据槽位必须提供 name",
                    f"{path}.name",
                )
            )
        elif name in credential_names:
            issues.append(
                ValidationIssue(
                    "credential_slot_duplicate",
                    "凭据槽位 name 重复",
                    f"{path}.name",
                )
            )
        else:
            credential_names.add(name)
            if isinstance(system_key, str):
                credential_system_by_name[name] = system_key
        if system_key not in {"legacy_inspection", "new_inspection"}:
            issues.append(
                ValidationIssue(
                    "credential_system_invalid",
                    "凭据槽位只能引用已注册的外部系统",
                    f"{path}.system_key",
                )
            )
        _inspect_values(slot, path, issues)

    _inspect_values(document.get("metadata") or {}, "$.metadata", issues)
    _validate_schema_document(
        document.get("global_schema"),
        path="$.global_schema",
        issues=issues,
    )

    nodes = document.get("nodes")
    edges = document.get("edges")
    if not isinstance(nodes, list) or not nodes:
        issues.append(ValidationIssue("nodes_required", "流程至少需要一个节点", "$.nodes"))
        nodes = []
    if not isinstance(edges, list):
        issues.append(ValidationIssue("edges_invalid", "edges 必须是数组", "$.edges"))
        edges = []

    node_by_id: dict[str, dict[str, Any]] = {}
    node_contract_by_id: dict[str, Any] = {}
    start_ids: list[str] = []
    end_ids: list[str] = []
    for index, node in enumerate(nodes):
        path = f"$.nodes[{index}]"
        if not isinstance(node, dict):
            issues.append(ValidationIssue("node_invalid", "节点必须是对象", path))
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not NODE_ID_PATTERN.fullmatch(node_id):
            issues.append(ValidationIssue("node_id_invalid", "节点 ID 不合法", f"{path}.id"))
            continue
        if node_id in node_by_id:
            issues.append(ValidationIssue("node_id_duplicate", "节点 ID 重复", f"{path}.id"))
            continue
        node_by_id[node_id] = node
        node_type = node.get("type")
        version = node.get("type_version", 1)
        definition = registry.get(node_type, version) if isinstance(version, int) else None
        if definition is None:
            issues.append(
                ValidationIssue(
                    "unknown_node_type",
                    f"未知节点类型或版本：{node_type}@{version}",
                    f"{path}.type",
                )
            )
        else:
            node_contract_by_id[node_id] = definition
            config = node.get("config") or {}
            if not isinstance(config, dict):
                issues.append(
                    ValidationIssue(
                        "node_config_invalid",
                        "节点 config 必须是对象",
                        f"{path}.config",
                    )
                )
                config = {}
            if definition.config_schema:
                config_result = validate_json_instance(
                    definition.config_schema,
                    config,
                    path_prefix=f"{path}.config",
                )
                issues.extend(config_result.issues)
            for required_key in definition.required_config:
                if config.get(required_key) in (None, ""):
                    issues.append(
                        ValidationIssue(
                            "node_config_required",
                            f"节点缺少配置：{required_key}",
                            f"{path}.config.{required_key}",
                        )
                    )
            if for_publish and not definition.publishable:
                issues.append(
                    ValidationIssue(
                        "node_not_publishable",
                        f"节点“{definition.name}”尚未启用，不能发布",
                        path,
                    )
                )
            if "form_schema" in config:
                _validate_schema_document(
                    config.get("form_schema"),
                    path=f"{path}.config.form_schema",
                    issues=issues,
                )
            if "candidate_role" in config and (
                not isinstance(config.get("candidate_role"), str)
                or not re.fullmatch(
                    r"[a-z][a-z0-9_.-]{0,49}",
                    config.get("candidate_role", ""),
                )
            ):
                issues.append(
                    ValidationIssue(
                        "candidate_role_invalid",
                        "candidate_role 不合法",
                        f"{path}.config.candidate_role",
                    )
                )
        if node_type == "core.start":
            start_ids.append(node_id)
        elif node_type == "core.end":
            end_ids.append(node_id)
        _inspect_values(node.get("config") or {}, f"{path}.config", issues)
        _inspect_values(
            node.get("input_mapping") or {},
            f"{path}.input_mapping",
            issues,
        )
        if not isinstance(node.get("input_mapping") or {}, dict):
            issues.append(
                ValidationIssue(
                    "node_input_mapping_invalid",
                    "节点 input_mapping 必须是对象",
                    f"{path}.input_mapping",
                )
            )
        _inspect_declared_root_references(
            node.get("config") or {},
            path=f"{path}.config",
            declared_root_ids=declared_root_ids,
            issues=issues,
        )
        for config_key, allowed_access in NODE_ROOT_ACCESS_REQUIREMENTS.get(
            str(node_type),
            {},
        ).items():
            configured_root = (node.get("config") or {}).get(config_key)
            if (
                configured_root in declared_root_ids
                and root_access_by_id.get(str(configured_root))
                not in allowed_access
            ):
                issues.append(
                    ValidationIssue(
                        "root_access_mismatch",
                        "节点使用方式与 root_slots 中声明的 access 不一致",
                        f"{path}.config.{config_key}",
                    )
                )

    if len(start_ids) != 1:
        issues.append(
            ValidationIssue(
                "single_start_required",
                "流程必须且只能有一个开始节点",
                "$.nodes",
            )
        )
    if not end_ids:
        issues.append(
            ValidationIssue("end_required", "流程至少需要一个结束节点", "$.nodes")
        )

    outgoing: dict[str, list[str]] = defaultdict(list)
    incoming: dict[str, list[str]] = defaultdict(list)
    incoming_join_policies: dict[str, set[str]] = defaultdict(set)
    edge_ids: set[str] = set()
    for index, edge in enumerate(edges):
        path = f"$.edges[{index}]"
        if not isinstance(edge, dict):
            issues.append(ValidationIssue("edge_invalid", "边必须是对象", path))
            continue
        edge_id = edge.get("id") or f"edge-{index}"
        if edge_id in edge_ids:
            issues.append(ValidationIssue("edge_id_duplicate", "边 ID 重复", f"{path}.id"))
        edge_ids.add(edge_id)
        source = edge.get("source")
        target = edge.get("target")
        if source not in node_by_id:
            issues.append(ValidationIssue("edge_source_missing", "起点不存在", f"{path}.source"))
        if target not in node_by_id:
            issues.append(ValidationIssue("edge_target_missing", "终点不存在", f"{path}.target"))
        if source == target and source is not None:
            issues.append(ValidationIssue("self_edge_forbidden", "节点不能连接自身", path))
        if source in node_by_id and target in node_by_id and source != target:
            outgoing[source].append(target)
            incoming[target].append(source)
        join_policy = edge.get("join_policy", "all")
        if join_policy not in ALLOWED_JOIN_POLICIES:
            issues.append(
                ValidationIssue(
                    "join_policy_invalid",
                    "join_policy 只能是 all 或 any",
                    f"{path}.join_policy",
                )
            )
        elif target in node_by_id:
            incoming_join_policies[target].add(join_policy)
        condition = edge.get("condition")
        _inspect_values(condition, f"{path}.condition", issues)
        source_node = node_by_id.get(source)
        if source_node is not None and source_node.get("type") == "branch.condition":
            _validate_condition(
                condition,
                path=f"{path}.condition",
                issues=issues,
            )
        elif condition not in (None, ""):
            issues.append(
                ValidationIssue(
                    "condition_source_invalid",
                    "只有条件分支节点的出边可以配置 condition",
                    f"{path}.condition",
                )
            )

    for target, policies in incoming_join_policies.items():
        if len(policies) > 1:
            issues.append(
                ValidationIssue(
                    "mixed_join_policy",
                    "同一目标节点的所有入边必须使用相同 join_policy",
                    f"$.nodes[{target}]",
                )
            )

    for node_id, node in node_by_id.items():
        mapping = node.get("input_mapping") or {}
        if not isinstance(mapping, dict):
            continue
        contract = node_contract_by_id.get(node_id)
        input_contract = contract.input_schema if contract is not None else {}
        input_properties = (
            input_contract.get("properties")
            if isinstance(input_contract, dict)
            else {}
        )
        for mapping_key, mapping_value in mapping.items():
            target_schema = (
                input_properties.get(mapping_key)
                if isinstance(input_properties, dict)
                else None
            )
            for reference_path, expression in _iter_mapping_references(
                mapping_value,
                path=f"$.nodes[{node_id}].input_mapping.{mapping_key}",
            ):
                _validate_mapping_reference(
                    expression=expression,
                    path=reference_path,
                    target_node_id=node_id,
                    target_schema=target_schema,
                    input_schema=document.get("input_schema") or {},
                    global_schema=document.get("global_schema") or {},
                    node_by_id=node_by_id,
                    node_contract_by_id=node_contract_by_id,
                    outgoing=outgoing,
                    issues=issues,
                )

    for source_id, node in node_by_id.items():
        if node.get("type") != "branch.condition":
            continue
        branch_edges = [
            edge
            for edge in edges
            if isinstance(edge, dict) and edge.get("source") == source_id
        ]
        default_count = sum(
            edge.get("condition") in (None, "", "default")
            for edge in branch_edges
        )
        if default_count != 1:
            issues.append(
                ValidationIssue(
                    "branch_default_required",
                    "条件分支必须且只能有一条 default 出边",
                    f"$.nodes[{source_id}]",
                )
            )

    external_upload_nodes = [
        (node_id, node)
        for node_id, node in node_by_id.items()
        if node.get("type")
        == "external.legacy_regenerated_fiber_count_upload"
    ]
    for external_id, external_node in external_upload_nodes:
        config = external_node.get("config") or {}
        credential_slot = config.get("credential_slot")
        if (
            not isinstance(credential_slot, str)
            or credential_system_by_name.get(credential_slot)
            != "legacy_inspection"
        ):
            issues.append(
                ValidationIssue(
                    "legacy_credential_slot_invalid",
                    "旧系统上传节点必须引用 legacy_inspection 凭据槽位",
                    f"$.nodes[{external_id}].config.credential_slot",
                )
            )

        selection_id = config.get("selection_node_id")
        selection_node = node_by_id.get(selection_id)
        if (
            selection_node is None
            or selection_node.get("type") != "human.file_selection"
        ):
            issues.append(
                ValidationIssue(
                    "legacy_selection_node_invalid",
                    "旧系统上传节点必须引用人工文件选择节点",
                    f"$.nodes[{external_id}].config.selection_node_id",
                )
            )
            continue
        mapping = external_node.get("input_mapping") or {}
        expected_selected = (
            f"$.nodes.{selection_id}.output.selected_files"
        )
        expected_primary = (
            f"$.nodes.{selection_id}.output.primary_file_id"
        )
        if mapping.get("selected_files") != expected_selected:
            issues.append(
                ValidationIssue(
                    "legacy_selected_files_mapping_invalid",
                    "旧系统上传只能使用人工选择节点签发的文件列表",
                    f"$.nodes[{external_id}].input_mapping.selected_files",
                )
            )
        if mapping.get("primary_file_id") != expected_primary:
            issues.append(
                ValidationIssue(
                    "legacy_primary_file_mapping_invalid",
                    "旧系统上传只能使用人工选择节点确认的主单",
                    f"$.nodes[{external_id}].input_mapping.primary_file_id",
                )
            )

        selection_files_mapping = (
            selection_node.get("input_mapping") or {}
        ).get("files")
        if (
            not isinstance(selection_files_mapping, str)
            or not selection_files_mapping.startswith("$.nodes.")
            or not selection_files_mapping.endswith(".output.files")
        ):
            issues.append(
                ValidationIssue(
                    "legacy_result_files_mapping_invalid",
                    "人工选择节点必须接收根数法结果读取节点的文件",
                    f"$.nodes[{selection_id}].input_mapping.files",
                )
            )
            continue
        source_id = selection_files_mapping[
            len("$.nodes.") : -len(".output.files")
        ]
        source_node = node_by_id.get(source_id)
        if (
            source_node is None
            or source_node.get("type")
            != "result.regenerated_fiber_count_method"
        ):
            issues.append(
                ValidationIssue(
                    "legacy_result_node_invalid",
                    "旧系统根数法上传必须来自再生纤根数法结果读取节点",
                    f"$.nodes[{selection_id}].input_mapping.files",
                )
            )

    special_wool_image_nodes = [
        (node_id, node)
        for node_id, node in node_by_id.items()
        if node.get("type")
        == "external.legacy_special_wool_image_upload"
    ]
    for external_id, external_node in special_wool_image_nodes:
        config = external_node.get("config") or {}
        credential_slot = config.get("credential_slot")
        if (
            not isinstance(credential_slot, str)
            or credential_system_by_name.get(credential_slot)
            != "legacy_inspection"
        ):
            issues.append(
                ValidationIssue(
                    "legacy_credential_slot_invalid",
                    "特种毛图片上传节点必须引用 legacy_inspection 凭据槽位",
                    f"$.nodes[{external_id}].config.credential_slot",
                )
            )
        generation_id = config.get("generation_node_id")
        generation_node = node_by_id.get(generation_id)
        if (
            generation_node is None
            or generation_node.get("type")
            != "workbook.microscopy_original_record"
        ):
            issues.append(
                ValidationIssue(
                    "legacy_special_wool_generation_node_invalid",
                    "特种毛图片上传必须引用微观形貌原始记录生成节点",
                    f"$.nodes[{external_id}].config.generation_node_id",
                )
            )
            continue
        mapping = external_node.get("input_mapping") or {}
        expected = f"$.nodes.{generation_id}.output.original_record"
        if mapping.get("original_record") != expected:
            issues.append(
                ValidationIssue(
                    "legacy_special_wool_artifact_mapping_invalid",
                    "特种毛图片上传只能使用生成节点签发的原始记录制品",
                    f"$.nodes[{external_id}].input_mapping.original_record",
                )
            )
        expected_project_key = (
            "$.nodes.record-input.output.selected_project_key"
        )
        expected_project = "$.nodes.record-input.output.selected_project"
        if mapping.get("selected_project_key") != expected_project_key:
            issues.append(
                ValidationIssue(
                    "legacy_special_wool_project_key_mapping_invalid",
                    "特种毛图片上传必须使用人工确认节点签发的项目键",
                    (
                        f"$.nodes[{external_id}].input_mapping."
                        "selected_project_key"
                    ),
                )
            )
        if mapping.get("selected_project") != expected_project:
            issues.append(
                ValidationIssue(
                    "legacy_special_wool_project_mapping_invalid",
                    "特种毛图片上传必须使用人工确认节点签发的任务项目快照",
                    f"$.nodes[{external_id}].input_mapping.selected_project",
                )
            )

    special_wool_review_nodes = [
        (node_id, node)
        for node_id, node in node_by_id.items()
        if node.get("type") == "external.legacy_special_wool_review"
    ]
    for review_id, review_node in special_wool_review_nodes:
        config = review_node.get("config") or {}
        credential_slot = config.get("credential_slot")
        if (
            not isinstance(credential_slot, str)
            or credential_system_by_name.get(credential_slot)
            != "legacy_inspection"
        ):
            issues.append(
                ValidationIssue(
                    "legacy_credential_slot_invalid",
                    "特纤复核节点必须引用 legacy_inspection 凭据槽位",
                    f"$.nodes[{review_id}].config.credential_slot",
                )
            )
        upload_id = config.get("upload_node_id")
        upload_node = node_by_id.get(upload_id)
        if (
            upload_node is None
            or upload_node.get("type")
            != "external.legacy_special_wool_image_upload"
        ):
            issues.append(
                ValidationIssue(
                    "legacy_special_wool_upload_node_invalid",
                    "特纤复核必须引用同一流程的特种毛图片上传节点",
                    f"$.nodes[{review_id}].config.upload_node_id",
                )
            )
            continue
        mapping = review_node.get("input_mapping") or {}
        expected = f"$.nodes.{upload_id}.output"
        if mapping.get("upload_result") != expected:
            issues.append(
                ValidationIssue(
                    "legacy_special_wool_review_mapping_invalid",
                    "特纤复核只能使用所引用图片上传节点的完整回执",
                    f"$.nodes[{review_id}].input_mapping.upload_result",
                )
            )

    final_entry_nodes = [
        (node_id, node)
        for node_id, node in node_by_id.items()
        if node.get("type")
        == "external.legacy_microscopy_check_record_entry"
    ]
    for entry_id, entry_node in final_entry_nodes:
        config = entry_node.get("config") or {}
        credential_slot = config.get("credential_slot")
        if (
            not isinstance(credential_slot, str)
            or credential_system_by_name.get(credential_slot)
            != "legacy_inspection"
        ):
            issues.append(
                ValidationIssue(
                    "legacy_credential_slot_invalid",
                    "检验记录登记节点必须引用 legacy_inspection 凭据槽位",
                    f"$.nodes[{entry_id}].config.credential_slot",
                )
            )
        generation_id = config.get("generation_node_id")
        generation_node = node_by_id.get(generation_id)
        if (
            generation_node is None
            or generation_node.get("type")
            != "workbook.microscopy_check_record"
        ):
            issues.append(
                ValidationIssue(
                    "legacy_final_entry_generation_node_invalid",
                    "检验记录登记必须引用按选图数量生成的登记工作簿",
                    f"$.nodes[{entry_id}].config.generation_node_id",
                )
            )
        review_id = config.get("review_node_id")
        review_node = node_by_id.get(review_id)
        if (
            review_node is None
            or review_node.get("type")
            != "external.legacy_special_wool_review"
        ):
            issues.append(
                ValidationIssue(
                    "legacy_final_entry_review_node_invalid",
                    "检验记录登记必须衔接同一流程的已完成特纤复核",
                    f"$.nodes[{entry_id}].config.review_node_id",
                )
            )
        mapping = entry_node.get("input_mapping") or {}
        registration_decision_node = node_by_id.get(
            "registration-decision"
        )
        uses_registration_decision = bool(
            isinstance(registration_decision_node, dict)
            and registration_decision_node.get("type") == "human.input"
            and (
                registration_decision_node.get("config") or {}
            ).get("legacy_existing_record_decision") is True
        )
        project_source = (
            "$.nodes.registration-decision.output"
            if uses_registration_decision
            else "$.nodes.record-input.output"
        )
        expected_mappings = {
            "registration_workbook": (
                f"$.nodes.{generation_id}.output.legacy_registration_workbook"
            ),
            "template_binding": (
                f"$.nodes.{generation_id}.output.template_binding"
            ),
            "review_result": f"$.nodes.{review_id}.output",
            "selected_project_key": (
                f"{project_source}.selected_project_key"
            ),
            "selected_project": (
                f"{project_source}.selected_project"
            ),
            **(
                {
                    "registration_decision": (
                        "$.nodes.registration-decision.output"
                    ),
                    "record_input": "$.nodes.record-input.output",
                }
                if uses_registration_decision
                else {}
            ),
        }
        for key, expected in expected_mappings.items():
            if mapping.get(key) != expected:
                issues.append(
                    ValidationIssue(
                        f"legacy_final_entry_{key}_mapping_invalid",
                        "检验记录登记节点的制品、模板、项目或复核来源映射不符合固定契约",
                        f"$.nodes[{entry_id}].input_mapping.{key}",
                    )
                )
        override_mapping = mapping.get("controlled_test_override")
        if override_mapping not in {
            None,
            "$.inputs.controlled_test_override",
        }:
            issues.append(
                ValidationIssue(
                    "legacy_final_entry_controlled_override_mapping_invalid",
                    "受控测试覆盖只能来自本次运行的显式输入",
                    (
                        f"$.nodes[{entry_id}].input_mapping."
                        "controlled_test_override"
                    ),
                )
            )

    publish_nodes = [
        (node_id, node)
        for node_id, node in node_by_id.items()
        if node.get("type") == "artifact.publish"
    ]
    if len(publish_nodes) > 1:
        issues.append(
            ValidationIssue(
                "multiple_publish_nodes_forbidden",
                "首版流程最多只能包含一个制品发布节点",
                "$.nodes",
            )
        )

    for publish_id, publish_node in publish_nodes:
        confirmation_id = (publish_node.get("config") or {}).get(
            "confirmation_node_id"
        )
        confirmation_node = node_by_id.get(confirmation_id)
        if (
            confirmation_node is None
            or confirmation_node.get("type") != "human.confirm"
        ):
            issues.append(
                ValidationIssue(
                    "publish_confirmation_node_invalid",
                    "发布节点必须引用同一流程中的人工确认节点",
                    f"$.nodes[{publish_id}].config.confirmation_node_id",
                )
            )
            continue
        approval_mapping = (
            confirmation_node.get("input_mapping") or {}
        ).get("approval_context")
        if (
            not isinstance(approval_mapping, str)
            or not approval_mapping.startswith("$.nodes.")
            or not approval_mapping.endswith(".output.approval_context")
        ):
            issues.append(
                ValidationIssue(
                    "publish_approval_context_missing",
                    "人工确认节点必须映射核对节点生成的 approval_context",
                    f"$.nodes[{confirmation_id}].input_mapping.approval_context",
                )
            )
        reachable_from_confirmation: set[str] = set()
        queue = deque([confirmation_id])
        while queue:
            current = queue.popleft()
            if current in reachable_from_confirmation:
                continue
            reachable_from_confirmation.add(current)
            queue.extend(outgoing[current])
        if publish_id not in reachable_from_confirmation:
            issues.append(
                ValidationIssue(
                    "publish_confirmation_order_invalid",
                    "人工确认节点必须位于发布节点之前",
                    f"$.nodes[{publish_id}]",
                )
            )

    if node_by_id:
        indegree = {node_id: len(incoming[node_id]) for node_id in node_by_id}
        queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
        visited: list[str] = []
        while queue:
            node_id = queue.popleft()
            visited.append(node_id)
            for target in outgoing[node_id]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if len(visited) != len(node_by_id):
            issues.append(
                ValidationIssue("cycle_forbidden", "首版流程必须是无环 DAG", "$.edges")
            )

    if len(start_ids) == 1:
        reachable: set[str] = set()
        queue = deque(start_ids)
        while queue:
            node_id = queue.popleft()
            if node_id in reachable:
                continue
            reachable.add(node_id)
            queue.extend(outgoing[node_id])
        for node_id in set(node_by_id) - reachable:
            issues.append(
                ValidationIssue(
                    "node_unreachable",
                    "节点无法从开始节点到达",
                    f"$.nodes[{node_id}]",
                )
            )

    if end_ids:
        can_reach_end: set[str] = set()
        queue = deque(end_ids)
        while queue:
            node_id = queue.popleft()
            if node_id in can_reach_end:
                continue
            can_reach_end.add(node_id)
            queue.extend(incoming[node_id])
        for node_id in set(node_by_id) - can_reach_end:
            issues.append(
                ValidationIssue(
                    "no_path_to_end",
                    "节点没有通往结束节点的路径",
                    f"$.nodes[{node_id}]",
                )
            )

    return ValidationResult(
        valid=not any(issue.level == "error" for issue in issues),
        issues=tuple(issues),
    )
