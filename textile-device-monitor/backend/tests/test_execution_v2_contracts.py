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
from app.execution.release_v2 import preflight_release
from app.execution.v2.canonical import (
    SemVerCandidate,
    canonical_json_bytes,
    canonical_sha256,
    resource_set_digest,
    select_highest_stable,
)
from app.execution.v2.examples import (
    build_controlled_xlsx_write_canary_release,
    build_native_human_file_selection_smoke_release,
    build_readonly_file_query_smoke_release,
)
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




def test_native_contracts_are_closed_and_workbook_copy_preferred_is_native():
    registry = get_installed_registry()
    native = [
        item for item in registry.list_node_specs() if item.source == "resource"
    ]
    for spec in native:
        for schema in (
            spec.config_schema,
            spec.input_schema,
            spec.output_schema,
        ):
            assert schema["type"] == "object"
            assert schema["additionalProperties"] is False
    preferred = registry.resolve_node_spec("workbook.copy", 1)
    assert preferred.source == "resource"
    assert preferred.pack_id == "textile.execution-workbook"


def test_pack_digest_is_bound_to_declared_implementation_source_bytes():
    pack = get_installed_registry().resolve_pack("textile.execution-kernel", "2.10.0")
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
    current = resolve_node_spec("core.start", 2)
    other_owner = replace(
        current,
        pack_id="another.pack",
        contract_digest="f" * 64,
    )
    with pytest.raises(ValueError, match="multiple owners"):
        NodeSpecRegistry([current, other_owner])










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
        (RESOURCE_ROOT / "manifests" / "textile.execution-workbook.json").read_text(
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
    spec = resolve_node_spec("file.query", 1)
    Draft202012Validator(spec.config_schema).validate(query["config"])
    assert query["config"]["root_slot"] == "source"


@pytest.mark.parametrize(
    ("name", "builder"),
    [
        (
            "v2-native-human-file-selection-smoke.json",
            build_native_human_file_selection_smoke_release,
        ),
        (
            "v2-controlled-xlsx-write-canary.json",
            build_controlled_xlsx_write_canary_release,
        ),
    ],
)
def test_p2_example_releases_are_deterministic_and_digest_pinned(name, builder):
    first = builder()
    second = builder()
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert json.loads((DOCS_ROOT / "examples" / name).read_text()) == first
    unsigned = deepcopy(first)
    unsigned.pop("integrity")
    assert first["integrity"]["digest"] == canonical_sha256(unsigned)




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
                "declared": ["file.index.read", "file.read"],
                "side_effect_level": "none",
                "requires_human_approval": False,
            }
            assert all(
                item["installed_ready"] is True
                for item in report["resolved_dependencies"]["node_instances"]
            )
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
