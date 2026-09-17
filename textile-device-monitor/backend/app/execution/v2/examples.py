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


def build_native_human_file_selection_smoke_release() -> dict[str, Any]:
    """Portable P2 Human smoke release; it is never installed or activated."""

    _specs, dependencies = _dependencies_for(
        [
            ("core.start", 1),
            ("file.query", 1),
            ("human.select", 1),
            ("data.aggregate", 1),
            ("core.end", 1),
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
                    "type_version": 1,
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
                    "type_version": 1,
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


def build_domain_records_smoke_release(domain: str = "microscopy") -> dict[str, Any]:
    """Runnable local domain examples; no inspection-system writes."""
    if domain not in {"microscopy", "paper-fiber"}:
        raise ValueError("unsupported_domain")
    microscopy = domain == "microscopy"
    document = build_native_human_file_selection_smoke_release()
    document["release"].update(
        slug=f"v2-{domain}-records-smoke", name="显微记录生成验收" if microscopy else "纸纤维结果选择验收",
        description="原生查询、选择和本地工作簿生成；本示例不要求判定，不执行检务写入。" if microscopy else "原生纸纤维查询与结果卡片选择。",
        release_note="P3 native domain services smoke release",
    )
    document["resources"]["rule_slots"] = [{
        "slot_id": "record_match", "name": "项目匹配规则", "rule_type": "project_match",
        "contract_version": 1, "required": True,
    }]
    definition = document["definition"]
    definition["input_schema"]["properties"]["inspection_number"]["title"] = "检验编号"
    start, query, select, _aggregate, end = definition["nodes"]
    query.update(type="microscopy.image_candidates" if microscopy else "paper_fiber.find_records", name="查询领域资料")
    query["config"] = {
        "root_slot": "source", "rule_slot": "record_match", "require_full_task_match": not microscopy,
        **({"record_family": "microscopy"} if microscopy else {"limit": 6}),
    }
    select["config"].update(
        title="选择结果图片" if microscopy else "选择原始记录",
        description="支持 1、2、3、5、6、7、10 张图片。" if microscopy else "查看定性结果后选择一份原始记录。",
        item_kind="image" if microscopy else "artifact", max_selected=10 if microscopy else 1,
        require_primary=not microscopy, auto_submit_single_candidate=True,
    )
    select["name"] = select["config"]["title"]
    nodes = [start, query, select]
    if microscopy:
        document["resources"]["root_slots"].append({
            "slot_id": "staging", "name": "生成工作簿", "access": "write", "required": True,
        })
        definition["input_schema"]["properties"]["sample_name"] = {"type": "string", "title": "样品名称", "minLength": 1}
        definition["input_schema"]["required"].append("sample_name")
        for node_id, typ, mapping in (
            ("original", "microscopy.original_record.render", {
                "images": "$.nodes.select.output.selected_items", "sample_name": "$.inputs.sample_name",
            }),
            ("check", "microscopy.check_record.render", {
                "image_count": "$.nodes.original.output.image_count", "template_binding": "$.nodes.original.output.template_binding",
            }),
        ):
            nodes.append({
                "id": node_id, "type": typ, "type_version": 1, "name": "生成原始记录" if node_id == "original" else "生成登记工作簿",
                "config": {"record_family": "microscopy", "staging_root_slot": "staging",
                           **({"source_root_slot": "source"} if node_id == "original" else {})},
                "input_mapping": {"inspection_number": "$.inputs.inspection_number", "judgement_required": False, **mapping},
                "ui": {"x": 280 * len(nodes), "y": 180},
            })
        end["input_mapping"] = {"original_record": "$.nodes.original.output.original_record", "registration_workbook": "$.nodes.check.output.check_record"}
        outputs = {"original_record": {"type": "object"}, "registration_workbook": {"type": "object"}}
    else:
        end["input_mapping"] = {"selected_items": "$.nodes.select.output.selected_items", "primary_item": "$.nodes.select.output.primary_item"}
        outputs = {"selected_items": {"type": "array"}, "primary_item": {"type": "object"}}
    nodes.append(end)
    definition["nodes"] = nodes
    definition["edges"] = [{"id": f"{left['id']}-{right['id']}", "source": left["id"], "target": right["id"], "join_policy": "all"} for left, right in zip(nodes, nodes[1:])]
    definition["output_schema"] = {"type": "object", "properties": outputs, "required": list(outputs), "additionalProperties": False}
    _specs, document["dependencies"] = _dependencies_for([(node["type"], 1) for node in nodes])
    document["dependencies"]["engine"] = {"version_range": ">=2.3.0 <3.0.0"}
    document["capabilities"] = {
        "declared": ["file.read", "file.write", "microscopy.records"] if microscopy else ["file.read", "paper-fiber.records"],
        "side_effect_level": "local_write" if microscopy else "none", "requires_human_approval": True,
    }
    return _seal(document)


def build_controlled_xlsx_write_canary_release() -> dict[str, Any]:
    """Derive the hidden P2 canary from the deterministic built-in fixture."""

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.database import Base
    from app.execution.catalog import ensure_default_catalog
    from app.execution.models import ExecutionUser, ExecutionWorkflow
    from app.execution.release_v2 import preview_v1_migration

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            ensure_default_catalog(db)
            workflow = (
                db.query(ExecutionWorkflow)
                .filter(
                    ExecutionWorkflow.slug
                    == "system-controlled-xlsx-write-test"
                )
                .one()
            )
            actor = ExecutionUser(
                id="00000000-0000-0000-0000-000000000001",
                username="p2-canary-builder",
                display_name="P2 canary builder",
                password_hash="not-used",
                role="admin",
                is_active=True,
            )
            document = preview_v1_migration(
                db,
                workflow_id=workflow.id,
                source="published",
                actor=actor,
                target_profile="native_p2",
            )["candidate"]
    finally:
        engine.dispose()
    document = deepcopy(document)
    document["release"] = {
        **document["release"],
        "slug": "v2-controlled-xlsx-write-canary",
        "name": "Execution v2 受控工作簿写入 Canary",
        "description": "仅用于隔离测试 root 的 copy/write/verify/approval/publish 闭环。",
        "release_note": "P2 hidden controlled-write canary; never auto-activate",
    }
    document.pop("migration", None)
    return _seal(document)
