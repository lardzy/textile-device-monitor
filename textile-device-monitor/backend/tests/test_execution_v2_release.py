from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.api.execution import AuthContext, start_run
from app.api.execution_v2 import (
    apply_staged_release,
    assets,
    content_preflight,
    export_release,
    get_release,
    migration_preview,
    monitoring,
    node_spec,
    node_specs,
    packs,
    publish_staged_release,
    rollback_release,
    router as execution_v2_router,
    staged_preflight,
    update_deployment_binding,
)
from app.config import settings
from app.database import Base
from app.execution.catalog import (
    bind_user_role,
    ensure_default_catalog,
    ensure_default_rbac,
)
from app.execution.engine import claim_next_node, create_run, execute_claimed_node
from app.execution.errors import ExecutionApiError
from app.execution.models import (
    ExecutionAuditLog,
    ExecutionFileIndexEntry,
    ExecutionReleasePreflight,
    ExecutionStorageRoot,
    ExecutionUser,
    ExecutionWorkflow,
    ExecutionWorkflowActivationReceipt,
    ExecutionWorkflowRelease,
    ExecutionWorkflowVersion,
    utcnow,
)
from app.execution.persistence import register_persistence_executors
from app.execution.project_rules import ensure_default_project_rules
from app.execution.release_v2 import (
    _release_digest,
    apply_release,
    compile_runtime_projection,
    export_version_release,
    preflight_release,
    preview_v1_migration,
    publish_release,
    put_deployment_binding,
    release_view,
    rollback_workflow,
)
from app.execution.schemas import (
    RunCreate,
    WorkflowReleaseApplyRequest,
    WorkflowReleaseBindingRequest,
    WorkflowReleasePreflightRequest,
    WorkflowReleasePublishRequest,
    WorkflowReleaseRollbackRequest,
    WorkflowV1MigrationPreviewRequest,
)
from app.execution.security import hash_password
from app.execution.v2.canonical import canonical_json_bytes
from app.execution.v2.examples import build_readonly_file_query_smoke_release
from app.execution.v2.registry import (
    get_installed_registry,
    worker_capability_document,
)
from app.execution.worker_state import record_worker_heartbeat


def _empty_bindings() -> dict:
    return {
        "root_slots": {},
        "credential_slots": {},
        "role_slots": {},
        "rule_slots": {},
    }


class _MissingWorkflowQuery:
    def filter(self, *_args, **_kwargs):
        return self

    def with_for_update(self):
        return self

    def one_or_none(self):
        return None


def _node_release(node_type: str) -> dict:
    """Build a valid metadata-only release around one compatibility node."""

    registry = get_installed_registry()
    node_types = ("core.start", node_type, "core.end")
    specs = [registry.resolve_node_spec(value, 1) for value in node_types]
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
    document = {
        "format": "textile-workflow-release",
        "format_version": "2.0",
        "release": {
            "slug": "placeholder-" + node_type.replace(".", "-"),
            "release_version": 1,
            "name": f"placeholder {node_type}",
            "description": "publishability gate fixture",
            "category_key": "other",
            "release_note": "test",
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
                for spec in specs
            ],
            "packs": packs,
            "connectors": [],
        },
        "resources": {
            "root_slots": [],
            "credential_slots": [],
            "role_slots": [],
            "rule_slots": [],
        },
        "assets": [],
        "capabilities": {
            "declared": [],
            "side_effect_level": "none",
            "requires_human_approval": False,
        },
        "definition": {
            "schema_version": "2.0",
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            "global_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            },
            "global_defaults": {},
            "nodes": [
                {
                    "id": "start",
                    "type": "core.start",
                    "type_version": 1,
                    "name": "start",
                    "config": {},
                    "input_mapping": {},
                },
                {
                    "id": "placeholder",
                    "type": node_type,
                    "type_version": 1,
                    "name": "placeholder",
                    "config": {},
                    "input_mapping": {},
                },
                {
                    "id": "end",
                    "type": "core.end",
                    "type_version": 1,
                    "name": "end",
                    "config": {},
                    "input_mapping": {},
                },
            ],
            "edges": [
                {
                    "id": "start-placeholder",
                    "source": "start",
                    "target": "placeholder",
                    "join_policy": "all",
                },
                {
                    "id": "placeholder-end",
                    "source": "placeholder",
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
        "digest": _release_digest(document),
        "signatures": [],
    }
    return document


class ExecutionV2ReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        register_persistence_executors()
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.Session()
        ensure_default_rbac(self.db)
        ensure_default_catalog(self.db)
        ensure_default_project_rules(self.db)
        self.admin = ExecutionUser(
            username="release-v2-admin",
            display_name="Release v2 admin",
            password_hash=hash_password("test-password"),
            role="admin",
        )
        self.other_admin = ExecutionUser(
            username="release-v2-other-admin",
            display_name="Release v2 other admin",
            password_hash=hash_password("test-password"),
            role="admin",
        )
        self.root_a = ExecutionStorageRoot(
            root_id="v2_source_a",
            name="v2 source A",
            local_path="/tmp/execution-v2-source-a",
            access_mode="read",
            category_key="other",
            is_active=True,
            is_available=True,
            scan_generation=1,
        )
        self.root_b = ExecutionStorageRoot(
            root_id="v2_source_b",
            name="v2 source B",
            local_path="/tmp/execution-v2-source-b",
            access_mode="read",
            category_key="other",
            is_active=True,
            is_available=True,
            scan_generation=2,
        )
        self.db.add_all(
            [
                self.admin,
                self.other_admin,
                self.root_a,
                self.root_b,
            ]
        )
        self.db.flush()
        for user in (self.admin, self.other_admin):
            bind_user_role(
                self.db,
                user,
                "admin",
                created_by_id=self.admin.id,
            )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _preflight_apply_bind(self):
        document = build_readonly_file_query_smoke_release()
        content = preflight_release(
            self.db,
            document=document,
            actor=self.admin,
            scope="content",
        )
        self.assertTrue(content["content_valid"], content["issues"])
        self.assertFalse(content["publish_ready"])
        release = apply_release(
            self.db,
            document=None,
            preflight_token=content["preflight_token"],
            actor=self.admin,
        )
        binding = put_deployment_binding(
            self.db,
            release_id=release.id,
            environment=settings.EXECUTION_ENVIRONMENT_ID,
            expected_revision=0,
            bindings={
                **_empty_bindings(),
                "root_slots": {
                    "source": {
                        "root_id": self.root_a.root_id,
                        "revision": self.root_a.binding_revision,
                    }
                },
            },
            actor=self.admin,
        )
        return document, release, binding

    def _publish(self, release):
        with patch.object(settings, "EXECUTION_CONTRACT_MODE", "enforced"):
            report = preflight_release(
                self.db,
                document=release.portable_document,
                actor=self.admin,
                release=release,
                scope="publish",
            )
            self.assertTrue(report["content_valid"], report["issues"])
            self.assertTrue(report["publish_ready"], report["issues"])
            return publish_release(
                self.db,
                release_id=release.id,
                preflight_token=report["preflight_token"],
                actor=self.admin,
                reason="test publish",
            )

    def _insert_competing_workflow(self, document, binding, management_mode):
        projection = compile_runtime_projection(document, binding)
        category_id = (
            self.db.query(ExecutionWorkflow)
            .order_by(ExecutionWorkflow.slug)
            .first()
            .category_id
        )
        workflow = ExecutionWorkflow(
            slug=document["release"]["slug"],
            category_id=category_id,
            name="Concurrent workflow winner",
            description="publish workflow-create race fixture",
            draft_definition=deepcopy(projection),
            draft_revision=1,
            management_mode=management_mode,
            capabilities={},
            required_input_count=1,
            is_enabled=True,
            created_by_id=self.admin.id,
            updated_by_id=self.admin.id,
        )
        self.db.add(workflow)
        self.db.commit()
        return workflow

    def test_readonly_release_lifecycle_runs_exports_rolls_back_and_reactivates(self):
        document, release, binding_a = self._preflight_apply_bind()
        release, version_a, receipt_a = self._publish(release)
        self.db.commit()

        self.assertEqual(version_a.version_number, 1)
        self.assertEqual(receipt_a.action, "publish")
        self.assertEqual(release.workflow_id, version_a.workflow_id)
        workflow = self.db.get(ExecutionWorkflow, version_a.workflow_id)
        self.assertEqual(workflow.management_mode, "release_v2")
        self.assertEqual(workflow.draft_definition, version_a.definition)
        self.assertEqual(
            export_version_release(
                self.db,
                workflow_id=workflow.id,
                local_version=version_a.version_number,
            ),
            document,
        )
        view = release_view(self.db, release)
        self.assertEqual(view["document"], document)
        self.assertEqual(view["active_local_version"], 1)
        self.assertEqual(
            view["version_contracts"][0]["dependency_lock_digest"],
            version_a.dependency_lock_digest,
        )
        for instance in version_a.dependency_lock["node_instances"]:
            self.assertEqual(len(instance["execution_binding_digest"]), 64)
            self.assertIn(instance["execution_kind"], {"automatic"})
            self.assertIn("effective_input_schema", instance)
            self.assertIn("effective_output_schema", instance)

        self.db.add(
            ExecutionFileIndexEntry(
                storage_root_id=self.root_a.id,
                relative_path="SMOKE-001/source.xlsx",
                filename="source.xlsx",
                extension=".xlsx",
                file_kind="workbook",
                inspection_number="SMOKE-001",
                group_key="SMOKE-001",
                size_bytes=10,
                modified_at=datetime.now(timezone.utc),
                fingerprint="smoke-file-1",
                scan_generation=self.root_a.scan_generation,
            )
        )
        record_worker_heartbeat(
            self.db,
            worker_id="release-v2-worker",
            capability_document=worker_capability_document(),
        )
        run, existed = create_run(
            self.db,
            workflow=workflow,
            actor=self.admin,
            inspection_number="SMOKE-001",
            input_data={"inspection_number": "SMOKE-001"},
            global_data={},
            idempotency_key="release-v2-smoke-001",
        )
        self.assertFalse(existed)
        self.assertEqual(run.release_id, release.id)
        self.assertEqual(
            run.deployed_contract_checksum,
            version_a.deployed_contract_checksum,
        )
        self.db.commit()
        with patch.object(settings, "EXECUTION_CONTRACT_MODE", "enforced"):
            for _ in range(3):
                claimed = claim_next_node(
                    self.db, worker_id="release-v2-worker"
                )
                self.assertIsNotNone(claimed)
                claimed_id = claimed.id
                lease_token = claimed.lease_token
                self.db.commit()
                execute_claimed_node(self.db, claimed_id, lease_token)
                self.db.commit()
        self.db.refresh(run)
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.output_data["count"], 1)
        self.assertEqual(len(run.output_data["candidates"]), 1)

        binding_b = put_deployment_binding(
            self.db,
            release_id=release.id,
            environment=settings.EXECUTION_ENVIRONMENT_ID,
            expected_revision=binding_a.revision,
            bindings={
                **_empty_bindings(),
                "root_slots": {
                    "source": {
                        "root_id": self.root_b.root_id,
                        "revision": self.root_b.binding_revision,
                    }
                },
            },
            actor=self.admin,
        )
        self.assertEqual(binding_b.revision, 2)
        release, version_b, _receipt_b = self._publish(release)
        self.db.commit()
        self.assertEqual(version_b.version_number, 2)
        self.assertNotEqual(
            version_b.deployment_binding_digest,
            version_a.deployment_binding_digest,
        )

        # Index freshness is operational state, not deployment configuration;
        # a normal scan must not invalidate a previously published binding.
        self.root_a.scan_generation += 1
        self.db.flush()
        rollback_receipt = rollback_workflow(
            self.db,
            workflow_id=workflow.id,
            target_local_version=version_a.version_number,
            reason="test rollback",
            actor=self.admin,
        )
        self.db.commit()
        self.db.refresh(workflow)
        self.assertEqual(workflow.published_version_number, 1)
        self.assertEqual(workflow.draft_definition, version_a.definition)
        rolled_back_view = release_view(self.db, release)
        self.assertEqual(rolled_back_view["local_version"], 2)
        self.assertEqual(rolled_back_view["active_local_version"], 1)
        rollback_audit = (
            self.db.query(ExecutionAuditLog)
            .filter(ExecutionAuditLog.action == "workflow_release.rollback")
            .one()
        )
        self.assertEqual(
            rollback_audit.details["receipt_id"], rollback_receipt.id
        )
        self.assertIsNotNone(rollback_audit.details["receipt_id"])

        release, reactivated, reactivate_receipt = self._publish(release)
        self.db.commit()
        self.db.refresh(workflow)
        self.assertEqual(reactivated.id, version_b.id)
        self.assertEqual(workflow.published_version_number, 2)
        self.assertEqual(workflow.draft_definition, version_b.definition)
        self.assertEqual(reactivate_receipt.from_version_number, 1)
        self.assertEqual(reactivate_receipt.to_version_number, 2)
        self.assertEqual(
            self.db.query(ExecutionWorkflowVersion)
            .filter_by(workflow_id=workflow.id)
            .count(),
            2,
        )

    def test_start_run_race_rechecks_current_v2_deployed_checksum(self):
        _document, release, binding_a = self._preflight_apply_bind()
        _release, version_a, _receipt = self._publish(release)
        workflow = self.db.get(ExecutionWorkflow, version_a.workflow_id)
        winner, existed = create_run(
            self.db,
            workflow=workflow,
            actor=self.admin,
            inspection_number="RACE-001",
            input_data={"inspection_number": "RACE-001"},
            global_data={},
            idempotency_key="release-v2-race-001",
        )
        self.assertFalse(existed)
        self.db.commit()

        put_deployment_binding(
            self.db,
            release_id=release.id,
            environment=settings.EXECUTION_ENVIRONMENT_ID,
            expected_revision=binding_a.revision,
            bindings={
                **_empty_bindings(),
                "root_slots": {
                    "source": {
                        "root_id": self.root_b.root_id,
                        "revision": self.root_b.binding_revision,
                    }
                },
            },
            actor=self.admin,
        )
        _release, version_b, _receipt = self._publish(release)
        self.db.commit()
        self.assertNotEqual(
            version_a.deployed_contract_checksum,
            version_b.deployed_contract_checksum,
        )

        call_count = 0

        def create_run_after_lost_insert(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise IntegrityError(
                    "INSERT INTO execution_runs",
                    {},
                    RuntimeError("simulated unique-key race"),
                )
            return create_run(*args, **kwargs)

        with (
            patch(
                "app.api.execution.create_run",
                side_effect=create_run_after_lost_insert,
            ),
            self.assertRaises(ExecutionApiError) as raced,
        ):
            start_run(
                RunCreate(
                    workflow_id=workflow.id,
                    inspection_number="RACE-001",
                    input_data={"inspection_number": "RACE-001"},
                    global_data={},
                    idempotency_key="release-v2-race-001",
                ),
                auth=AuthContext(session=None, user=self.admin),
                db=self.db,
            )

        self.assertEqual(call_count, 2)
        self.assertEqual(raced.exception.code, "run_idempotency_conflict")
        self.assertEqual(raced.exception.details["existing_run_id"], winner.id)

    def test_publish_workflow_create_race_reuses_release_v2_winner(self):
        document, release, binding = self._preflight_apply_bind()
        with patch.object(settings, "EXECUTION_CONTRACT_MODE", "enforced"):
            report = preflight_release(
                self.db,
                document=release.portable_document,
                actor=self.admin,
                release=release,
                scope="publish",
            )
        self.assertTrue(report["publish_ready"], report["issues"])
        winner = self._insert_competing_workflow(
            document, binding, "release_v2"
        )
        real_query = self.db.query
        workflow_query_count = 0

        def stale_workflow_query(*entities, **kwargs):
            nonlocal workflow_query_count
            if len(entities) == 1 and entities[0] is ExecutionWorkflow:
                workflow_query_count += 1
                if workflow_query_count == 1:
                    return _MissingWorkflowQuery()
            return real_query(*entities, **kwargs)

        with (
            patch.object(
                self.db, "query", side_effect=stale_workflow_query
            ),
            patch.object(settings, "EXECUTION_CONTRACT_MODE", "enforced"),
        ):
            published, version, receipt = publish_release(
                self.db,
                release_id=release.id,
                preflight_token=report["preflight_token"],
                actor=self.admin,
                reason="concurrent workflow winner",
            )

        self.assertEqual(workflow_query_count, 2)
        self.assertEqual(published.workflow_id, winner.id)
        self.assertEqual(version.workflow_id, winner.id)
        self.assertEqual(receipt.to_version_number, 1)
        self.assertEqual(
            release_view(self.db, published)["active_local_version"], 1
        )

    def test_publish_workflow_create_race_with_v1_winner_is_stable_conflict(
        self,
    ):
        document, release, binding = self._preflight_apply_bind()
        with patch.object(settings, "EXECUTION_CONTRACT_MODE", "enforced"):
            report = preflight_release(
                self.db,
                document=release.portable_document,
                actor=self.admin,
                release=release,
                scope="publish",
            )
        self.assertTrue(report["publish_ready"], report["issues"])
        winner = self._insert_competing_workflow(
            document, binding, "draft_v1"
        )
        real_query = self.db.query
        workflow_query_count = 0

        def stale_workflow_query(*entities, **kwargs):
            nonlocal workflow_query_count
            if len(entities) == 1 and entities[0] is ExecutionWorkflow:
                workflow_query_count += 1
                if workflow_query_count == 1:
                    return _MissingWorkflowQuery()
            return real_query(*entities, **kwargs)

        with (
            patch.object(
                self.db, "query", side_effect=stale_workflow_query
            ),
            patch.object(settings, "EXECUTION_CONTRACT_MODE", "enforced"),
            self.assertRaises(ExecutionApiError) as raced,
        ):
            publish_release(
                self.db,
                release_id=release.id,
                preflight_token=report["preflight_token"],
                actor=self.admin,
                reason="v1 workflow wins create race",
            )

        self.assertEqual(workflow_query_count, 2)
        self.assertEqual(raced.exception.code, "workflow_slug_conflict")
        self.assertEqual(raced.exception.details["workflow_id"], winner.id)

    def test_v2_api_wire_contract_permissions_csrf_and_lifecycle(self):
        expected_guards = {
            "/execution/v2/node-specs": ("GET", "workflow.design", False),
            "/execution/v2/node-specs/{node_type}/{type_version}": (
                "GET",
                "workflow.design",
                False,
            ),
            "/execution/v2/packs": ("GET", "workflow.design", False),
            "/execution/v2/assets": ("GET", "workflow.design", False),
            "/execution/v2/renderer-capabilities": (
                "GET",
                "workflow.design",
                False,
            ),
            "/execution/v2/monitoring": ("GET", "audit.read", False),
            "/execution/v2/workflow-releases": (
                "GET",
                "workflow.design",
                False,
            ),
            "/execution/v2/workflow-releases/preflight": (
                "POST",
                "workflow.design",
                True,
            ),
            "/execution/v2/workflow-releases/apply": (
                "POST",
                "workflow.design",
                True,
            ),
            "/execution/v2/workflow-releases/{release_id}": (
                "GET",
                "workflow.design",
                False,
            ),
            (
                "/execution/v2/workflow-releases/{release_id}/"
                "deployment-binding"
            ): ("PUT", "workflow.publish", True),
            "/execution/v2/workflow-releases/{release_id}/preflight": (
                "POST",
                "workflow.publish",
                True,
            ),
            "/execution/v2/workflow-releases/{release_id}/publish": (
                "POST",
                "workflow.publish",
                True,
            ),
            (
                "/execution/v2/workflows/{workflow_id}/versions/"
                "{local_version}/export"
            ): ("GET", "workflow.design", False),
            "/execution/v2/workflows/{workflow_id}/rollback": (
                "POST",
                "workflow.publish",
                True,
            ),
            "/execution/v2/migrations/v1/preview": (
                "POST",
                "workflow.design",
                True,
            ),
        }
        routes = {
            route.path: route
            for route in execution_v2_router.routes
            if route.path in expected_guards
        }
        self.assertEqual(set(routes), set(expected_guards))
        for path, (method, permission_key, csrf_required) in (
            expected_guards.items()
        ):
            self.assertIn(method, routes[path].methods)
            guards = [
                dependency.call
                for dependency in routes[path].dependant.dependencies
                if hasattr(
                    dependency.call,
                    "execution_permission_key",
                )
            ]
            self.assertEqual(len(guards), 1)
            self.assertEqual(
                guards[0].execution_permission_key, permission_key
            )
            self.assertEqual(
                guards[0].execution_csrf_required, csrf_required
            )

        auth = AuthContext(session=None, user=self.admin)
        self.assertEqual(len(packs(_auth=auth)["items"]), 5)
        self.assertGreaterEqual(len(assets(_auth=auth)["items"]), 1)
        specs = node_specs(node_type=None, _auth=auth)["items"]
        self.assertEqual(len(specs), 57)
        selected_spec = specs[0]
        selected_detail = node_spec(
            selected_spec["type"],
            selected_spec["type_version"],
            contract_digest=selected_spec["contract_digest"],
            _auth=auth,
        )
        self.assertEqual(
            selected_detail["contract_digest"],
            selected_spec["contract_digest"],
        )
        monitor = monitoring(_auth=auth, db=self.db)
        self.assertEqual(len(monitor["registry_revision"]), 64)
        self.assertEqual(len(monitor["pack_readiness"]), 5)

        document = build_readonly_file_query_smoke_release()
        content = content_preflight(
            WorkflowReleasePreflightRequest(document=document),
            auth=auth,
            db=self.db,
        )
        self.assertTrue(content["content_valid"], content["issues"])
        applied = apply_staged_release(
            WorkflowReleaseApplyRequest(
                preflight_token=content["preflight_token"]
            ),
            auth=auth,
            db=self.db,
        )
        release_id = applied["id"]
        binding = update_deployment_binding(
            release_id,
            WorkflowReleaseBindingRequest(
                environment="default",
                expected_revision=0,
                root_bindings=[
                    {
                        "slot": "source",
                        "storage_root_id": self.root_a.id,
                        "role": "read",
                    }
                ],
            ),
            auth=auth,
            db=self.db,
        )
        self.assertEqual(binding["deployment_binding"]["revision"], 1)

        with patch.object(settings, "EXECUTION_CONTRACT_MODE", "enforced"):
            publish_report = staged_preflight(
                release_id, auth=auth, db=self.db
            )
            self.assertTrue(
                publish_report["publish_ready"], publish_report["issues"]
            )
            published = publish_staged_release(
                release_id,
                WorkflowReleasePublishRequest(
                    preflight_token=publish_report["preflight_token"],
                    reason="API function smoke",
                ),
                auth=auth,
                db=self.db,
            )
        workflow_id = published["workflow_id"]
        local_version = published["local_version"]
        release_detail = get_release(
            release_id, _auth=auth, db=self.db
        )
        self.assertEqual(
            release_detail["version_contracts"][0]["local_version"],
            local_version,
        )
        self.assertEqual(
            release_detail["active_local_version"], local_version
        )
        exported = export_release(
            workflow_id,
            local_version,
            _auth=auth,
            db=self.db,
        )
        self.assertEqual(
            canonical_json_bytes(exported), canonical_json_bytes(document)
        )
        with self.assertRaises(ExecutionApiError) as unavailable:
            rollback_release(
                workflow_id,
                WorkflowReleaseRollbackRequest(
                    target_local_version=local_version,
                    reason="worker unavailable",
                ),
                auth=auth,
                db=self.db,
            )
        self.assertEqual(
            unavailable.exception.code,
            "rollback_node_capability_unavailable",
        )
        self.db.rollback()
        record_worker_heartbeat(
            self.db,
            worker_id="release-v2-api-worker",
            capability_document=worker_capability_document(),
        )
        rolled_back = rollback_release(
            workflow_id,
            WorkflowReleaseRollbackRequest(
                target_local_version=local_version,
                reason="API function rollback smoke",
            ),
            auth=auth,
            db=self.db,
        )
        self.assertEqual(rolled_back["activation"]["action"], "rollback")

        v1_workflow = (
            self.db.query(ExecutionWorkflow)
            .filter(ExecutionWorkflow.management_mode == "draft_v1")
            .order_by(ExecutionWorkflow.slug)
            .first()
        )
        preview = migration_preview(
            WorkflowV1MigrationPreviewRequest(
                workflow_id=v1_workflow.id,
                source="published",
            ),
            auth=auth,
            db=self.db,
        )
        self.assertTrue(preview["content_valid"], preview["issues"])

    def test_preflight_token_is_owner_bound_single_use_and_expires(self):
        document = build_readonly_file_query_smoke_release()
        report = preflight_release(
            self.db,
            document=document,
            actor=self.admin,
            scope="content",
        )
        with self.assertRaises(ExecutionApiError) as wrong_owner:
            apply_release(
                self.db,
                document=None,
                preflight_token=report["preflight_token"],
                actor=self.other_admin,
            )
        self.assertEqual(
            wrong_owner.exception.code, "preflight_token_owner_mismatch"
        )
        apply_release(
            self.db,
            document=None,
            preflight_token=report["preflight_token"],
            actor=self.admin,
        )
        with self.assertRaises(ExecutionApiError) as replayed:
            apply_release(
                self.db,
                document=None,
                preflight_token=report["preflight_token"],
                actor=self.admin,
            )
        self.assertEqual(replayed.exception.code, "preflight_token_replayed")

        expiring = deepcopy(document)
        expiring["release"]["release_version"] = 2
        expiring["integrity"]["digest"] = _release_digest(expiring)
        expiring_report = preflight_release(
            self.db,
            document=expiring,
            actor=self.admin,
            scope="content",
        )
        token_row = (
            self.db.query(ExecutionReleasePreflight)
            .filter(
                ExecutionReleasePreflight.created_by_id == self.admin.id,
                ExecutionReleasePreflight.consumed_at.is_(None),
            )
            .order_by(ExecutionReleasePreflight.created_at.desc())
            .first()
        )
        token_row.expires_at = utcnow() - timedelta(seconds=1)
        self.db.flush()
        with self.assertRaises(ExecutionApiError) as expired:
            apply_release(
                self.db,
                document=None,
                preflight_token=expiring_report["preflight_token"],
                actor=self.admin,
            )
        self.assertEqual(expired.exception.code, "preflight_token_expired")

    def test_same_source_identity_with_different_digest_is_a_stable_conflict(self):
        document = build_readonly_file_query_smoke_release()
        first_report = preflight_release(
            self.db,
            document=document,
            actor=self.admin,
            scope="content",
        )
        apply_release(
            self.db,
            document=None,
            preflight_token=first_report["preflight_token"],
            actor=self.admin,
        )

        changed = deepcopy(document)
        changed["release"]["release_note"] = "same identity, changed content"
        changed["integrity"]["digest"] = _release_digest(changed)
        changed_report = preflight_release(
            self.db,
            document=changed,
            actor=self.admin,
            scope="content",
        )
        with self.assertRaises(ExecutionApiError) as conflict_error:
            apply_release(
                self.db,
                document=None,
                preflight_token=changed_report["preflight_token"],
                actor=self.admin,
            )
        self.assertEqual(
            conflict_error.exception.code,
            "release_source_identity_conflict",
        )

    def test_publish_token_rejects_registry_and_binding_revision_changes(self):
        _document, release, binding = self._preflight_apply_bind()
        with patch.object(settings, "EXECUTION_CONTRACT_MODE", "enforced"):
            registry_report = preflight_release(
                self.db,
                document=release.portable_document,
                actor=self.admin,
                release=release,
                scope="publish",
            )
        token_row = (
            self.db.query(ExecutionReleasePreflight)
            .filter(
                ExecutionReleasePreflight.release_id == release.id,
                ExecutionReleasePreflight.scope == "publish",
                ExecutionReleasePreflight.consumed_at.is_(None),
            )
            .order_by(ExecutionReleasePreflight.created_at.desc())
            .first()
        )
        token_row.registry_revision = "0" * 64
        self.db.flush()
        with self.assertRaises(ExecutionApiError) as registry_changed:
            publish_release(
                self.db,
                release_id=release.id,
                preflight_token=registry_report["preflight_token"],
                actor=self.admin,
                reason="stale registry token",
            )
        self.assertEqual(
            registry_changed.exception.code,
            "registry_revision_changed",
        )

        with patch.object(settings, "EXECUTION_CONTRACT_MODE", "enforced"):
            binding_report = preflight_release(
                self.db,
                document=release.portable_document,
                actor=self.admin,
                release=release,
                scope="publish",
            )
        put_deployment_binding(
            self.db,
            release_id=release.id,
            environment=settings.EXECUTION_ENVIRONMENT_ID,
            expected_revision=binding.revision,
            bindings={
                **_empty_bindings(),
                "root_slots": {
                    "source": {
                        "root_id": self.root_b.root_id,
                        "revision": self.root_b.binding_revision,
                    }
                },
            },
            actor=self.admin,
        )
        with self.assertRaises(ExecutionApiError) as binding_changed:
            publish_release(
                self.db,
                release_id=release.id,
                preflight_token=binding_report["preflight_token"],
                actor=self.admin,
                reason="stale binding token",
            )
        self.assertEqual(
            binding_changed.exception.code,
            "deployment_binding_revision_changed",
        )

    def test_apply_unique_race_reuses_same_digest_concurrent_release(self):
        document = build_readonly_file_query_smoke_release()
        report = preflight_release(
            self.db,
            document=document,
            actor=self.admin,
            scope="content",
        )
        concurrent = ExecutionWorkflowRelease(
            id="concurrent-release",
            source_slug=document["release"]["slug"],
            source_version=document["release"]["release_version"],
            release_digest=_release_digest(document),
            format_version=document["format_version"],
            portable_document=deepcopy(document),
            status="staged",
            created_by_id=self.admin.id,
        )
        real_query = self.db.query
        real_flush = self.db.flush
        release_query_count = 0

        class StaticReleaseQuery:
            def __init__(self, value):
                self.value = value

            def filter(self, *_args, **_kwargs):
                return self

            def one_or_none(self):
                return self.value

        def racing_query(*entities, **kwargs):
            nonlocal release_query_count
            if len(entities) == 1 and entities[0] is ExecutionWorkflowRelease:
                release_query_count += 1
                return StaticReleaseQuery(
                    None if release_query_count == 1 else concurrent
                )
            return real_query(*entities, **kwargs)

        def racing_flush(*args, **kwargs):
            if any(
                isinstance(value, ExecutionWorkflowRelease)
                for value in self.db.new
            ):
                raise IntegrityError(
                    "INSERT INTO execution_workflow_releases",
                    {},
                    RuntimeError("simulated unique-key race"),
                )
            return real_flush(*args, **kwargs)

        with (
            patch.object(self.db, "query", side_effect=racing_query),
            patch.object(self.db, "flush", side_effect=racing_flush),
        ):
            applied = apply_release(
                self.db,
                document=None,
                preflight_token=report["preflight_token"],
                actor=self.admin,
            )

        self.assertIs(applied, concurrent)
        self.assertEqual(release_query_count, 2)

    def test_ed25519_signature_policy_accepts_valid_and_rejects_invalid(self):
        document = build_readonly_file_query_smoke_release()
        private_key = Ed25519PrivateKey.generate()
        signature = private_key.sign(bytes.fromhex(document["integrity"]["digest"]))
        encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
        document["integrity"]["signatures"] = [
            {
                "algorithm": "ed25519",
                "key_id": "release-test-key",
                "signed_digest": document["integrity"]["digest"],
                "signature": encoded_signature,
            }
        ]
        with TemporaryDirectory() as key_directory:
            public_key = private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            Path(key_directory, "release-test-key.pem").write_bytes(public_key)
            with (
                patch.object(
                    settings,
                    "EXECUTION_RELEASE_SIGNATURE_POLICY",
                    "required",
                ),
                patch.object(
                    settings,
                    "EXECUTION_RELEASE_TRUSTED_KEYS_DIR",
                    key_directory,
                ),
            ):
                valid = preflight_release(
                    self.db,
                    document=document,
                    actor=self.admin,
                    scope="content",
                )
                self.assertTrue(valid["content_valid"], valid["issues"])

                invalid_document = deepcopy(document)
                invalid_document["integrity"]["signatures"][0][
                    "signature"
                ] = ("A" if encoded_signature[0] != "A" else "B") + encoded_signature[1:]
                invalid = preflight_release(
                    self.db,
                    document=invalid_document,
                    actor=self.admin,
                    scope="content",
                )
                self.assertFalse(invalid["content_valid"])
                self.assertIn(
                    "release_signature_invalid",
                    {item["code"] for item in invalid["issues"]},
                )

                unsigned = build_readonly_file_query_smoke_release()
                required = preflight_release(
                    self.db,
                    document=unsigned,
                    actor=self.admin,
                    scope="content",
                )
                self.assertFalse(required["content_valid"])
                self.assertIn(
                    "release_signature_required",
                    {item["code"] for item in required["issues"]},
                )

    def test_publish_revalidates_underlying_binding_revision(self):
        _document, release, _binding = self._preflight_apply_bind()
        with patch.object(settings, "EXECUTION_CONTRACT_MODE", "enforced"):
            report = preflight_release(
                self.db,
                document=release.portable_document,
                actor=self.admin,
                release=release,
                scope="publish",
            )
            self.assertTrue(report["publish_ready"], report["issues"])
            self.root_a.binding_revision += 1
            self.db.flush()
            with self.assertRaises(ExecutionApiError) as stale:
                publish_release(
                    self.db,
                    release_id=release.id,
                    preflight_token=report["preflight_token"],
                    actor=self.admin,
                    reason="must fail",
                )
        self.assertEqual(stale.exception.code, "deployment_binding_stale")

    def test_index_scan_generation_does_not_stale_release_binding(self):
        _document, release, _binding = self._preflight_apply_bind()
        with patch.object(settings, "EXECUTION_CONTRACT_MODE", "enforced"):
            report = preflight_release(
                self.db,
                document=release.portable_document,
                actor=self.admin,
                release=release,
                scope="publish",
            )
            self.assertTrue(report["publish_ready"], report["issues"])
            bound_revision = self.root_a.binding_revision
            self.root_a.scan_generation += 1
            self.db.flush()
            _release, version, receipt = publish_release(
                self.db,
                release_id=release.id,
                preflight_token=report["preflight_token"],
                actor=self.admin,
                reason="scan generation must not stale binding",
            )

        root_snapshot = version.deployment_binding_snapshot["bindings"][
            "root_slots"
        ]["source"]
        self.assertEqual(root_snapshot["revision"], bound_revision)
        self.assertNotEqual(
            root_snapshot["revision"], self.root_a.scan_generation
        )
        self.assertEqual(receipt.to_version_number, version.version_number)

    def test_monitoring_aggregates_rollout_signals(self):
        _document, release, _binding = self._preflight_apply_bind()
        _release, _version, _receipt = self._publish(release)
        workflow = self.db.get(ExecutionWorkflow, release.workflow_id)
        run, _existed = create_run(
            self.db,
            workflow=workflow,
            actor=self.admin,
            inspection_number="MONITOR-001",
            input_data={"inspection_number": "MONITOR-001"},
            global_data={},
            idempotency_key="release-v2-monitor-001",
        )
        self.db.add(
            ExecutionAuditLog(
                action="execution_v2.claim.shadow_mismatch",
                resource_type="node_run",
                resource_id="monitor-fixture",
                details={"test": True},
            )
        )
        self.db.commit()

        payload = monitoring(
            _auth=AuthContext(session=None, user=self.admin),
            db=self.db,
        )
        self.assertEqual(payload["shadow_mismatch"]["count"], 1)
        self.assertEqual(payload["v2_runs"]["total"], 1)
        self.assertEqual(payload["v2_runs"]["by_status"][run.status], 1)
        self.assertEqual(
            payload["node_capability_unavailable"][
                "unavailable_node_count"
            ],
            1,
        )
        self.assertEqual(
            payload["activation_receipts"]["by_action"]["publish"], 1
        )
        self.assertGreaterEqual(
            payload["preflights"]["issue_codes"].get(
                "node_capability_unavailable", 0
            ),
            1,
        )

    def test_content_preflight_runs_dag_semantics(self):
        document = build_readonly_file_query_smoke_release()
        document["definition"]["edges"].append(
            {
                "id": "cycle",
                "source": "end",
                "target": "query",
                "join_policy": "all",
            }
        )
        document["integrity"]["digest"] = _release_digest(document)
        report = preflight_release(
            self.db,
            document=document,
            actor=self.admin,
            scope="content",
        )
        self.assertFalse(report["content_valid"])
        codes = {item["code"] for item in report["issues"]}
        self.assertTrue(
            {"cycle_forbidden", "workflow_cycle", "end_node_has_outgoing"}
            & codes,
            report["issues"],
        )

    def test_bare_json_preflight_rejects_bundle_asset_transport(self):
        document = build_readonly_file_query_smoke_release()
        document["assets"] = [
            {
                "asset_id": "bundle-fixture",
                "kind": "workbook_template",
                "version": "1.0.0",
                "media_type": "application/vnd.ms-excel",
                "size_bytes": 0,
                "digest": "0" * 64,
                "source": {
                    "kind": "bundle",
                    "relative_path": "assets/fixture.xls",
                },
            }
        ]
        document["integrity"]["digest"] = _release_digest(document)
        report = preflight_release(
            self.db,
            document=document,
            actor=self.admin,
            scope="content",
        )
        self.assertFalse(report["content_valid"])
        self.assertIn(
            "bundle_transport_not_supported",
            {item["code"] for item in report["issues"]},
        )

    def test_metadata_only_placeholders_are_content_valid_but_not_publishable(self):
        for node_type in (
            "external.legacy_inspection",
            "external.new_inspection",
        ):
            with self.subTest(node_type=node_type):
                report = preflight_release(
                    self.db,
                    document=_node_release(node_type),
                    actor=self.admin,
                    scope="content",
                )
                self.assertTrue(report["content_valid"], report["issues"])
                self.assertFalse(report["publish_ready"])
                codes = {item["code"] for item in report["issues"]}
                self.assertIn("node_not_publishable", codes)
                self.assertIn("p1_publish_gate_blocked", codes)

    def test_all_nine_builtin_candidates_are_stable_and_content_valid(self):
        workflows = self.db.query(ExecutionWorkflow).order_by(
            ExecutionWorkflow.slug
        ).all()
        self.assertEqual(len(workflows), 9)
        for workflow in workflows:
            with self.subTest(slug=workflow.slug):
                original_pointer = workflow.published_version_number
                original_draft = deepcopy(workflow.draft_definition)
                preview_a = preview_v1_migration(
                    self.db,
                    workflow_id=workflow.id,
                    source="published",
                    actor=self.admin,
                )
                preview_b = preview_v1_migration(
                    self.db,
                    workflow_id=workflow.id,
                    source="published",
                    actor=self.admin,
                )
                self.assertTrue(preview_a["content_valid"], preview_a["issues"])
                self.assertEqual(
                    canonical_json_bytes(preview_a["candidate"]),
                    canonical_json_bytes(preview_b["candidate"]),
                )
                report = preflight_release(
                    self.db,
                    document=preview_a["candidate"],
                    actor=self.admin,
                    scope="content",
                )
                self.assertTrue(report["content_valid"], report["issues"])

                suggestions = preview_a["binding_suggestions"]
                projection_bindings = {
                    "root_slots": {
                        slot: {
                            "root_id": value["root_id"],
                            "storage_root_id": "preview",
                            "revision": 0,
                        }
                        for slot, value in suggestions["root_slots"].items()
                    },
                    "credential_slots": {
                        slot: {"system_key": value["system_key"]}
                        for slot, value in suggestions[
                            "credential_slots"
                        ].items()
                    },
                    "role_slots": {
                        slot: {"role_key": value["role_key"]}
                        for slot, value in suggestions["role_slots"].items()
                    },
                    "rule_slots": {
                        slot: {"rule_key": value["rule_key"]}
                        for slot, value in suggestions["rule_slots"].items()
                    },
                }
                projected = compile_runtime_projection(
                    preview_a["candidate"],
                    SimpleNamespace(binding=projection_bindings),
                )
                source_version = self.db.query(ExecutionWorkflowVersion).filter_by(
                    workflow_id=workflow.id,
                    version_number=workflow.published_version_number,
                ).one()
                original_configs = {
                    node["id"]: node.get("config") or {}
                    for node in source_version.definition["nodes"]
                }
                projected_configs = {
                    node["id"]: node.get("config") or {}
                    for node in projected["nodes"]
                }
                self.assertEqual(projected_configs, original_configs)
                self.assertEqual(
                    workflow.published_version_number, original_pointer
                )
                self.assertEqual(workflow.draft_definition, original_draft)
        self.assertEqual(
            self.db.query(ExecutionWorkflowActivationReceipt).count(), 0
        )


if __name__ == "__main__":
    unittest.main()
