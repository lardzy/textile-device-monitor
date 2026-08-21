"""Deterministic portable examples used by documentation and integration tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.execution.v2.canonical import canonical_sha256
from app.execution.v2.registry import get_installed_registry, load_schema
from jsonschema import Draft202012Validator


def build_readonly_file_query_smoke_release() -> dict[str, Any]:
    registry = get_installed_registry()
    node_specs = [
        registry.resolve_node_spec("core.start", 1),
        registry.resolve_node_spec("file.index_query", 1),
        registry.resolve_node_spec("core.end", 1),
    ]
    pack_identities = sorted({(spec.pack_id, spec.pack_version) for spec in node_specs})
    packs = []
    for pack_id, pack_version in pack_identities:
        pack = registry.resolve_pack(pack_id, pack_version)
        packs.append(
            {
                "pack_id": pack.pack_id,
                "version_range": pack.pack_version,
                "distribution_digest": pack.distribution_digest,
                "required_on": ["api", "worker"],
            }
        )
    document: dict[str, Any] = {
        "format": "textile-workflow-release",
        "format_version": "2.0",
        "release": {
            "slug": "v2-readonly-file-query-smoke",
            "release_version": 1,
            "name": "Execution v2 只读文件索引闭环",
            "description": "验证导入、root 绑定、发布、运行、导出与回滚。",
            "category_key": "other",
            "release_note": "P1 automatic/read-only smoke release",
        },
        "dependencies": {
            "engine": {"version_range": ">=2.0.0 <3.0.0"},
            "node_types": [
                {
                    "type": spec.type,
                    "type_version": spec.type_version,
                    "contract_digest": spec.contract_digest,
                    "implementation_digest": spec.implementation_digest,
                }
                for spec in node_specs
            ],
            "packs": packs,
            "connectors": [],
        },
        "resources": {
            "root_slots": [
                {
                    "slot_id": "source",
                    "name": "只读文件索引根目录",
                    "description": "绑定到可读 ExecutionStorageRoot。",
                    "access": "read",
                    "required": True,
                }
            ],
            "credential_slots": [],
            "role_slots": [],
            "rule_slots": [],
        },
        "assets": [],
        "capabilities": {
            "declared": ["file.read"],
            "side_effect_level": "none",
            "requires_human_approval": False,
        },
        "definition": {
            "schema_version": "2.0",
            "input_schema": {
                "type": "object",
                "properties": {
                    "inspection_number": {
                        "type": "string",
                        "minLength": 1,
                    }
                },
                "required": ["inspection_number"],
                "additionalProperties": False,
            },
            "global_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "candidates": {"type": "array"},
                    "count": {"type": "integer", "minimum": 0},
                },
                "required": ["candidates", "count"],
                "additionalProperties": False,
            },
            "global_defaults": {},
            "nodes": [
                {
                    "id": "start",
                    "type": "core.start",
                    "type_version": 1,
                    "name": "开始",
                    "config": {},
                    "input_mapping": {},
                    "ui": {"x": 40, "y": 180},
                },
                {
                    "id": "query",
                    "type": "file.index_query",
                    "type_version": 1,
                    "name": "查询文件索引",
                    "config": {
                        "root_slot": "source",
                        "recent_days": 7,
                        "limit": 20,
                    },
                    "input_mapping": {
                        "inspection_number": "$.inputs.inspection_number"
                    },
                    "ui": {"x": 300, "y": 180},
                },
                {
                    "id": "end",
                    "type": "core.end",
                    "type_version": 1,
                    "name": "结束",
                    "config": {},
                    "input_mapping": {
                        "candidates": "$.nodes.query.output.candidates",
                        "count": "$.nodes.query.output.count",
                    },
                    "ui": {"x": 560, "y": 180},
                },
            ],
            "edges": [
                {
                    "id": "start-query",
                    "source": "start",
                    "target": "query",
                    "join_policy": "all",
                },
                {
                    "id": "query-end",
                    "source": "query",
                    "target": "end",
                    "join_policy": "all",
                },
            ],
        },
        "fixtures": [],
    }
    document["integrity"] = {
        "algorithm": "sha256",
        "canonicalization": "RFC8785",
        "scope": "document_without_integrity",
        "digest": canonical_sha256(document),
        "signatures": [],
    }
    Draft202012Validator(load_schema("workflow-release-v2.schema.json")).validate(
        document
    )
    return deepcopy(document)
