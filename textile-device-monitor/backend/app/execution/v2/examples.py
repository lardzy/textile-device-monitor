"""Deterministic portable examples used by documentation and integration tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.execution.v2.canonical import canonical_sha256
from app.execution.v2.registry import get_installed_registry, load_schema
from jsonschema import Draft202012Validator


def _dependencies_for(
    identities: list[tuple[str, int]],
) -> tuple[list[Any], dict[str, Any]]:
    registry = get_installed_registry()
    specs = [
        registry.resolve_node_spec(node_type, type_version)
        for node_type, type_version in identities
    ]
    packs = []
    for pack_id, pack_version in sorted(
        {(spec.pack_id, spec.pack_version) for spec in specs}
    ):
        pack = registry.resolve_pack(pack_id, pack_version)
        packs.append(
            {
                "pack_id": pack.pack_id,
                "version_range": pack.pack_version,
                "distribution_digest": pack.distribution_digest,
                "required_on": ["api", "worker"],
            }
        )
    dependencies = {
        "engine": {"version_range": ">=2.1.0 <3.0.0"},
        "node_types": [
            {
                "type": spec.type,
                "type_version": spec.type_version,
                "contract_digest": spec.contract_digest,
                "implementation_digest": spec.implementation_digest,
            }
            for spec in specs
        ],
        "packs": packs,
        "connectors": [],
    }
    return specs, dependencies


def _seal(document: dict[str, Any]) -> dict[str, Any]:
    document.pop("integrity", None)
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


def build_readonly_file_query_smoke_release() -> dict[str, Any]:
    registry = get_installed_registry()
    node_specs = [
        registry.resolve_node_spec("core.start", 2),
        registry.resolve_node_spec("file.query", 1),
        registry.resolve_node_spec("core.end", 2),
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
            "engine": {"version_range": ">=2.1.0 <3.0.0"},
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
                    "type_version": 2,
                    "name": "开始",
                    "config": {},
                    "input_mapping": {},
                    "ui": {"x": 40, "y": 180},
                },
                {
                    "id": "query",
                    "type": "file.query",
                    "type_version": 1,
                    "name": "查询文件索引",
                    "config": {
                        "root_slot": "source",
                        "recent_days": 7,
                        "limit": 20,
                        "sort": "modified_desc", "projection": ["id", "root_id", "relative_path", "name", "fingerprint"],
                    },
                    "input_mapping": {
                        "inspection_number": "$.inputs.inspection_number"
                    },
                    "ui": {"x": 300, "y": 180},
                },
                {
                    "id": "end",
                    "type": "core.end",
                    "type_version": 2,
                    "name": "结束",
                    "config": {},
                    "input_mapping": {
                        "candidates": "$.nodes.query.output.items",
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
    from app.execution.v2.designer import compile_document
    return compile_document(document, exact=True)["document"]


def build_connector_query_smoke_release(query_name="task_snapshot.get") -> dict[str, Any]:
    """One portable read-only query, without credentials, roots or human tasks."""
    registry = get_installed_registry()
    query = registry.connectors.resolve_query("legacy_fibrecheck", "*", query_name, 1)
    _specs, dependencies = _dependencies_for([
        ("core.start", 2), ("connector.query", 1), ("core.end", 2),
    ])
    dependencies["engine"]["version_range"] = ">=2.4.0 <3.0.0"
    dependencies["packs"].append({
        "pack_id": query.pack_id, "version_range": query.pack_version,
        "distribution_digest": query.distribution_digest, "required_on": ["api", "worker"],
    })
    dependencies["connectors"] = [{
        "connector_id": query.connector_id, "version_range": query.connector_version,
        "distribution_digest": query.distribution_digest, "operations": [],
        "queries": [{"query": query.query, "contract_version": query.contract_version,
                     "contract_digest": query.contract_digest}],
    }]
    document = build_readonly_file_query_smoke_release()
    document["release"].update({
        "slug": "v2-connector-query-smoke", "name": "检务任务快照查询",
        "description": "与直接 API 共用已注册的任务快照读取服务，缓存缺失时返回刷新状态。",
        "release_note": "P4 cached Connector QuerySpec acceptance",
    })
    document["dependencies"] = dependencies
    document["capabilities"]["declared"] = []
    document["resources"] = {key: [] for key in document["resources"]}
    definition = document["definition"]
    definition["input_schema"] = deepcopy(query.spec["input_schema"])
    definition["input_schema"]["properties"].pop("refresh")
    definition["output_schema"] = deepcopy(query.spec["output_schema"])
    if query_name != "task_snapshot.get":
        document["release"].update(slug="v2-connector-record-query-smoke", name="读取项目检验登记",
                                   description="按任务项目读取登记身份、字段及内容指纹，明细缺失时返回刷新状态。")
    start, node, end = definition["nodes"]
    start["input_mapping"] = {"inspection_number": "$.inputs.inspection_number"}
    node.update({"type": "connector.query", "name": "查询任务快照",
                 "config": {"query_ref": query.query_ref},
                 "input_mapping": {"inspection_number": "$.inputs.inspection_number"}})
    for key in query.spec["input_schema"]["required"]:
        node["input_mapping"][key] = f"$.inputs.{key}"
    end["input_mapping"] = {
        key: f"$.nodes.query.output.{key}" for key in query.spec["output_schema"]["properties"]
    }
    return _seal(document)


def build_connector_operation_smoke_release(operation_name="check_record.generic_entry") -> dict[str, Any]:
    registry = get_installed_registry()
    operation = registry.resolve_operation("legacy_fibrecheck", "*", operation_name, 2 if operation_name == "check_record.generic_entry" else 1)
    document = build_connector_query_smoke_release()
    _specs, dependencies = _dependencies_for([("core.start", 2), ("external.operation", 1), ("core.end", 2)])
    dependencies["packs"].append({
        "pack_id": operation.pack_id, "version_range": operation.pack_version,
        "distribution_digest": operation.distribution_digest, "required_on": ["api", "worker"],
    })
    dependencies["connectors"] = [{
        "connector_id": operation.connector_id, "version_range": operation.connector_version,
        "distribution_digest": operation.distribution_digest, "queries": [],
        "operations": [{"operation": operation.operation, "contract_version": operation.contract_version,
                        "contract_digest": operation.contract_digest}],
    }]
    document["dependencies"] = dependencies
    document["release"].update(slug="v2-connector-operation-smoke", name="检务操作接口验收",
                               description="一次提交，后台执行并自动核对。", release_note="P4 generic operation acceptance")
    document["capabilities"].update(side_effect_level="external_write", requires_human_approval=False)
    document["resources"]["credential_slots"] = [{
        "slot_id": "legacy", "name": "检务账号", "connector_id": "legacy_fibrecheck", "credential_kind": "password", "required": True,
    }]
    definition = document["definition"]
    definition["input_schema"] = deepcopy(operation.spec["input_schema"])
    definition["output_schema"] = deepcopy(operation.spec["output_schema"])
    start, node, end = definition["nodes"]
    start["input_mapping"] = {key: f"$.inputs.{key}" for key in definition["input_schema"]["properties"]}
    node.update(type="external.operation", name="执行检务操作", config={"operation_ref": operation.operation_ref, "credential_slot": "legacy"},
                input_mapping=deepcopy(start["input_mapping"]))
    end["input_mapping"] = {key: f"$.nodes.query.output.{key}" for key in definition["output_schema"]["properties"]}
    return _seal(document)


def build_native_human_file_selection_smoke_release() -> dict[str, Any]:
    """Portable P2 Human smoke release; it is never installed or activated."""

    _specs, dependencies = _dependencies_for(
        [
            ("core.start", 2),
            ("file.query", 1),
            ("human.select", 1),
            ("data.aggregate", 1),
            ("core.end", 2),
        ]
    )
    document = {
        "format": "textile-workflow-release",
        "format_version": "2.0",
        "release": {
            "slug": "v2-native-human-file-selection-smoke",
            "release_version": 1,
            "name": "Execution v2 原生人工文件选择闭环",
            "description": "验证索引查询、稳定 ID 选择、汇总与人工任务恢复。",
            "category_key": "other",
            "release_note": "P2 native Human smoke release",
        },
        "dependencies": dependencies,
        "resources": {
            "root_slots": [
                {
                    "slot_id": "source",
                    "name": "文件索引源",
                    "description": "绑定到只读索引根。",
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
            "declared": ["file.index.read", "file.read"],
            "side_effect_level": "none",
            "requires_human_approval": True,
        },
        "definition": {
            "schema_version": "2.0",
            "input_schema": {
                "type": "object",
                "properties": {
                    "inspection_number": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
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
                    "selected_items": {"type": "array", "items": {}},
                    "count": {"type": "integer", "minimum": 0},
                },
                "required": ["selected_items", "count"],
                "additionalProperties": False,
            },
            "global_defaults": {},
            "nodes": [
                {
                    "id": "start",
                    "type": "core.start",
                    "type_version": 2,
                    "name": "开始",
                    "config": {},
                    "input_mapping": {},
                    "ui": {"x": 40, "y": 180},
                },
                {
                    "id": "query",
                    "type": "file.query",
                    "type_version": 1,
                    "name": "查询索引",
                    "config": {
                        "root_slot": "source",
                        "extensions": [".xls", ".xlsx"],
                        "recent_days": 30,
                        "limit": 20,
                        "sort": "modified_desc",
                        "projection": [
                            "id",
                            "root_id",
                            "relative_path",
                            "name",
                            "suffix",
                            "modified_at",
                            "fingerprint",
                        ],
                    },
                    "input_mapping": {
                        "inspection_number": "$.inputs.inspection_number"
                    },
                    "ui": {"x": 300, "y": 180},
                },
                {
                    "id": "select",
                    "type": "human.select",
                    "type_version": 1,
                    "name": "选择文件",
                    "config": {
                        "title": "选择待处理文件",
                        "description": "浏览器仅提交稳定候选 ID。",
                        "item_kind": "artifact",
                        "min_selected": 1,
                        "max_selected": 20,
                        "require_primary": True,
                        "auto_submit_single_candidate": False,
                    },
                    "input_mapping": {"items": "$.nodes.query.output.items"},
                    "ui": {"x": 560, "y": 180},
                },
                {
                    "id": "aggregate",
                    "type": "data.aggregate",
                    "type_version": 1,
                    "name": "汇总选择",
                    "config": {"mode": "collect", "conflict": "error"},
                    "input_mapping": {
                        "items": "$.nodes.select.output.selected_items"
                    },
                    "ui": {"x": 820, "y": 180},
                },
                {
                    "id": "end",
                    "type": "core.end",
                    "type_version": 2,
                    "name": "结束",
                    "config": {},
                    "input_mapping": {
                        "selected_items": "$.nodes.aggregate.output.value",
                        "count": "$.nodes.aggregate.output.count",
                    },
                    "ui": {"x": 1080, "y": 180},
                },
            ],
            "edges": [
                {"id": "start-query", "source": "start", "target": "query", "join_policy": "all"},
                {"id": "query-select", "source": "query", "target": "select", "join_policy": "all"},
                {"id": "select-aggregate", "source": "select", "target": "aggregate", "join_policy": "all"},
                {"id": "aggregate-end", "source": "aggregate", "target": "end", "join_policy": "all"},
            ],
        },
        "fixtures": [],
    }
    return _seal(document)


def build_controlled_xlsx_write_canary_release() -> dict[str, Any]:
    from pathlib import Path
    import json
    from app.execution.v2.designer import compile_document
    document = json.loads((Path(__file__).parent / 'resources/examples/workbook-primitives.json').read_text())
    return compile_document(document, exact=True)['document']
