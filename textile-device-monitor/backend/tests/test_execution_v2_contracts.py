from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from importlib import resources
from pathlib import Path

import pytest
from app.database import Base
from app.execution.catalog import ensure_default_catalog
from app.execution.models import ExecutionUser, ExecutionWorkflow
from app.execution.registry import node_registry
from app.execution.release_v2 import preflight_release, preview_v1_migration
from app.execution.v2.canonical import (
    SemVerCandidate,
    canonical_json_bytes,
    canonical_sha256,
    resource_set_digest,
    select_highest_stable,
)
from app.execution.v2.examples import build_readonly_file_query_smoke_release
from app.execution.v2.registry import (
    NodeSpecRegistry,
    executable_binding_for,
    get_installed_registry,
    legacy_operation_ref_for_node,
    load_schema,
    reset_installed_registry_cache,
    resolve_node_spec,
    resolve_operation,
    worker_capability_document,
)
from app.execution.v2.snapshots import render_contract_snapshot
from app.execution.worker import ExecutionWorker
from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = REPOSITORY_ROOT / "docs" / "execution-v2"
RESOURCE_ROOT = REPOSITORY_ROOT / "backend" / "app" / "execution" / "v2" / "resources"


@pytest.mark.parametrize(
    "name",
    [
        "workflow-release-v2.schema.json",
        "node-spec-v2.schema.json",
        "pack-manifest-v2.schema.json",
    ],
)
def test_runtime_schema_and_documentation_copy_are_byte_identical(name):
    assert (DOCS_ROOT / name).read_bytes() == (
        RESOURCE_ROOT / "schemas" / name
    ).read_bytes()
    Draft202012Validator.check_schema(load_schema(name))


def test_rfc8785_canonicalization_and_digest_are_stable():
    value = {"b": 1, "a": "€", "nested": {"z": False, "a": None}}
    assert canonical_json_bytes(value) == (
        b'{"a":"\xe2\x82\xac","b":1,"nested":{"a":null,"z":false}}'
    )
    assert canonical_sha256(value) == canonical_sha256(deepcopy(value))


def test_rfc8785_section_3_2_3_official_canonical_vector():
    # RFC 8785 sections 3.2.2-3.2.4, including number serialization,
    # string escaping, recursive property sorting, and final UTF-8 bytes.
    value = {
        "numbers": [
            333333333.33333329,
            1e30,
            4.50,
            2e-3,
            0.000000000000000000000000001,
        ],
        "string": '€$\x0f\nA\'B"\\\\"/',
        "literals": [None, True, False],
    }
    expected = bytes.fromhex(
        "7b226c69746572616c73223a5b6e756c6c2c747275652c66616c73655d2c"
        "226e756d62657273223a5b3333333333333333332e333333333333332c3165"
        "2b33302c342e352c302e3030322c31652d32375d2c22737472696e67223a22"
        "e282ac245c75303030665c6e4127425c225c5c5c5c5c222f227d"
    )
    assert canonical_json_bytes(value) == expected


def test_semver_selects_highest_stable_and_never_masks_broken_highest():
    candidates = [
        SemVerCandidate("1.0.0"),
        SemVerCandidate("1.1.0-beta.1"),
        SemVerCandidate("1.1.0", ready=False),
    ]
    with pytest.raises(RuntimeError, match="installed but not ready"):
        select_highest_stable(candidates, ">=1.0.0 <2.0.0")
    assert select_highest_stable(candidates[:2], ">=1.0.0 <2.0.0").version == "1.0.0"


def test_installed_registry_freezes_all_current_compatibility_contracts():
    registry = get_installed_registry()
    specs = registry.list_node_specs()
    assert len(specs) == 39
    assert sum(item.publishable for item in specs) == 37
    assert sum(not item.publishable for item in specs) == 2
    assert len(registry.list_packs()) == 3
    assert len(registry.list_assets()) == 11
    assert len(registry.list_connectors()) == 1
    assert len(registry.list_connectors()[0]["operations"]) == 7
    assert len(registry.revision) == 64
    assert len({(item.type, item.type_version) for item in specs}) == 39


def test_pack_digest_is_bound_to_declared_implementation_source_bytes():
    pack = get_installed_registry().resolve_pack("textile.execution-kernel", "2.0.0")
    manifest = deepcopy(pack.manifest)
    manifest.pop("distribution_digest", None)
    package_root = resources.files("app.execution")
    inventory = [
        (path, package_root.joinpath(path).read_bytes())
        for path in manifest["resource_paths"]
    ]
    assert any(path.endswith(".py") for path, _content in inventory)
    calculated = canonical_sha256(
        {
            "manifest": manifest,
            "resources_digest": resource_set_digest(inventory),
        }
    )
    assert calculated == pack.distribution_digest
    changed = list(inventory)
    changed[0] = (changed[0][0], changed[0][1] + b"\n# changed")
    changed_digest = canonical_sha256(
        {
            "manifest": manifest,
            "resources_digest": resource_set_digest(changed),
        }
    )
    assert changed_digest != pack.distribution_digest


def test_registry_rejects_a_second_owner_for_same_node_identity():
    current = resolve_node_spec("core.start", 1)
    other_owner = replace(
        current,
        pack_id="another.pack",
        contract_digest="f" * 64,
    )
    with pytest.raises(ValueError, match="multiple owners"):
        NodeSpecRegistry([current, other_owner])


def test_compatibility_node_specs_validate_and_expose_only_portable_slots():
    validator = Draft202012Validator(load_schema("node-spec-v2.schema.json"))
    for spec in get_installed_registry().list_node_specs():
        validator.validate(spec.spec)
        properties = (
            spec.config_schema.get("properties", {})
            if isinstance(spec.config_schema, dict)
            else {}
        )
        assert "root_id" not in properties
        assert not any(key.endswith("_root_id") for key in properties)
        assert "match_rule" not in properties
        assert "candidate_role" not in properties
        for group in spec.spec["requirements"]["resources"].values():
            for requirement in group:
                pointer = requirement["config_pointer"]
                assert pointer.removeprefix("/") in properties


def test_api_installed_readiness_is_separate_from_worker_callable_readiness():
    key = ("file.index_query", 1)
    previous = node_registry._executors.pop(key, None)
    try:
        reset_installed_registry_cache()
        assert executable_binding_for(*key)["ready"] is True
        document = worker_capability_document()
        capability = next(item for item in document["nodes"] if item["type"] == key[0])
        assert capability["ready"] is False
        assert document["capability_digest"] == canonical_sha256(document["nodes"])

        node_registry.set_executor(key[0], key[1], lambda **_kwargs: {})
        refreshed = worker_capability_document()
        capability = next(item for item in refreshed["nodes"] if item["type"] == key[0])
        assert capability["ready"] is True
    finally:
        if previous is None:
            node_registry._executors.pop(key, None)
        else:
            node_registry._executors[key] = previous
        reset_installed_registry_cache()


def test_worker_self_check_advertises_37_ready_compatibility_bindings():
    ExecutionWorker(worker_id="v2-contract-test")
    reset_installed_registry_cache()
    document = worker_capability_document()
    assert len(document["nodes"]) == 39
    assert sum(item["ready"] for item in document["nodes"]) == 37
    assert {item["type"] for item in document["nodes"] if not item["ready"]} == {
        "external.legacy_inspection",
        "external.new_inspection",
    }


def test_legacy_operations_have_exact_contracts_and_zero_picture_semantics():
    assert (
        legacy_operation_ref_for_node("external.legacy_special_wool_image_upload")
        == "legacy_fibrecheck.special_wool.image_upload@1"
    )
    operation = resolve_operation(
        "legacy_fibrecheck",
        ">=1.0.0 <2.0.0",
        "special_wool.image_upload",
        1,
    )
    assert len(operation.contract_digest) == 64
    assert operation.spec["completion_stage"] == "main_record_verified"
    assert operation.spec["extensions"]["textile"] == {
        "picture_records": [],
        "picture_count": 0,
    }


def test_registry_assets_are_verified_and_schema_accepts_canonical_key():
    asset = get_installed_registry().list_assets()[0]
    assert asset["kind"] == "workbook_template"
    assert asset["size_bytes"] > 0
    assert asset["resource_path"].startswith("templates/")
    release = build_readonly_file_query_smoke_release()
    candidate = deepcopy(release)
    candidate["assets"] = [
        {
            "asset_id": asset["asset_id"],
            "kind": asset["kind"],
            "version": asset["version"],
            "media_type": asset["media_type"],
            "size_bytes": asset["size_bytes"],
            "digest": asset["digest"],
            "source": {
                "kind": "registry",
                "registry_key": asset["registry_key"],
            },
        }
    ]
    candidate["integrity"]["digest"] = canonical_sha256(
        {key: value for key, value in candidate.items() if key != "integrity"}
    )
    Draft202012Validator(load_schema("workflow-release-v2.schema.json")).validate(
        candidate
    )

    manifest = json.loads(
        (RESOURCE_ROOT / "manifests" / "textile.execution-v1-compat.json").read_text(
            encoding="utf-8"
        )
    )
    manifest["assets"][0]["resource_path"] = "../outside.xls"
    with pytest.raises(ValidationError):
        Draft202012Validator(load_schema("pack-manifest-v2.schema.json")).validate(
            manifest
        )


def test_readonly_smoke_release_is_schema_valid_and_digest_pinned():
    release = build_readonly_file_query_smoke_release()
    on_disk = json.loads(
        (DOCS_ROOT / "examples" / "v2-readonly-file-query-smoke.json").read_text(
            encoding="utf-8"
        )
    )
    assert on_disk == release
    Draft202012Validator(load_schema("workflow-release-v2.schema.json")).validate(
        release
    )
    unsigned = {key: value for key, value in release.items() if key != "integrity"}
    assert release["integrity"]["digest"] == canonical_sha256(unsigned)
    query = next(
        node for node in release["definition"]["nodes"] if node["id"] == "query"
    )
    spec = resolve_node_spec("file.index_query", 1)
    Draft202012Validator(spec.config_schema).validate(query["config"])
    assert query["config"]["root_slot"] == "source"


def test_readonly_smoke_passes_content_preflight_without_worker_registration():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    try:
        with Session(engine) as db:
            actor = ExecutionUser(
                username="v2-smoke",
                display_name="v2 smoke",
                password_hash="not-used",
                role="admin",
            )
            db.add(actor)
            db.flush()
            report = preflight_release(
                db,
                document=build_readonly_file_query_smoke_release(),
                actor=actor,
                scope="content",
            )
            assert report["content_valid"] is True
            assert report["issues"] == []
            assert report["computed_capabilities"] == {
                "declared": ["file.read"],
                "side_effect_level": "none",
                "requires_human_approval": False,
            }
            assert all(
                item["installed_ready"] is True
                for item in report["resolved_dependencies"]["node_instances"]
            )
    finally:
        engine.dispose()


def test_all_nine_new_install_workflows_generate_deterministic_valid_candidates():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    try:
        with Session(engine) as db:
            ensure_default_catalog(db)
            actor = ExecutionUser(
                username="v2-migration",
                display_name="v2 migration",
                password_hash="not-used",
                role="admin",
            )
            db.add(actor)
            db.flush()
            workflows = (
                db.query(ExecutionWorkflow).order_by(ExecutionWorkflow.slug.asc()).all()
            )
            assert len(workflows) == 9
            for workflow in workflows:
                first = preview_v1_migration(
                    db,
                    workflow_id=workflow.id,
                    source="published",
                    actor=actor,
                )
                second = preview_v1_migration(
                    db,
                    workflow_id=workflow.id,
                    source="published",
                    actor=actor,
                )
                assert first["content_valid"] is True, (
                    workflow.slug,
                    first["issues"],
                )
                assert first["candidate"] == second["candidate"]
                assert first["binding_suggestions"] == second["binding_suggestions"]
                assert first["diff"]["active_pointer_changed"] is False
    finally:
        engine.dispose()


def test_workflow_schema_rejects_unknown_fields_secrets_paths_and_remote_refs():
    validator = Draft202012Validator(load_schema("workflow-release-v2.schema.json"))
    invalid_releases = []

    release = build_readonly_file_query_smoke_release()
    release["unknown_top_level"] = True
    invalid_releases.append(release)

    release = build_readonly_file_query_smoke_release()
    release["definition"]["nodes"][0]["executor"] = "unsafe.module:run"
    invalid_releases.append(release)

    release = build_readonly_file_query_smoke_release()
    release["definition"]["nodes"][1]["config"]["password"] = "unsafe"
    invalid_releases.append(release)

    for absolute_path in (
        "/etc/passwd",
        "C:\\Windows\\system32",
        "\\\\server\\share\\fixture.xls",
        "smb://server/share/fixture.xls",
    ):
        release = build_readonly_file_query_smoke_release()
        release["definition"]["nodes"][1]["config"]["local_path"] = absolute_path
        invalid_releases.append(release)

    release = build_readonly_file_query_smoke_release()
    release["definition"]["input_schema"] = {
        "$ref": "https://example.invalid/schema.json"
    }
    invalid_releases.append(release)

    for release in invalid_releases:
        with pytest.raises(ValidationError):
            validator.validate(release)


def test_p0_snapshot_is_current_and_contains_nine_new_install_workflows():
    expected = render_contract_snapshot()
    actual = (RESOURCE_ROOT / "snapshots" / "execution-v1-contracts.json").read_bytes()
    assert actual == expected
    document = json.loads(actual)
    assert len(document["nodes"]) == 39
    assert len(document["new_install_workflows"]) == 9
    assert len(document["durable_records"]["external_operation"]["contracts"]) == 7
    image_upload = next(
        item
        for item in document["durable_records"]["external_operation"]["contracts"]
        if item["operation_ref"] == "legacy_fibrecheck.special_wool.image_upload@1"
    )
    assert image_upload["receipt_fixture"]["picture_records"] == []
    assert image_upload["receipt_fixture"]["picture_count"] == 0
    assert image_upload["receipt_fixture"]["completion_stage"] == (
        "main_record_verified"
    )
