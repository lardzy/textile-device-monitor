from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.execution.catalog import (
    bind_user_role,
    create_workflow,
    ensure_default_rbac,
    publish_workflow,
)
from app.execution.engine import claim_next_node, create_run
from app.execution.models import (
    ExecutionCategory,
    ExecutionNodeAttempt,
    ExecutionNodeRun,
    ExecutionUser,
    ExecutionWorkerNodeCapability,
)
from app.execution.security import hash_password
from app.execution.worker_state import record_worker_heartbeat


def _definition() -> dict:
    return {
        "schema_version": "1.0",
        "metadata": {"name": "v2 worker capability test"},
        "input_schema": {"type": "object", "properties": {}},
        "global_schema": {"type": "object", "properties": {}},
        "root_slots": [],
        "credential_slots": [],
        "nodes": [
            {
                "id": "start",
                "type": "core.start",
                "type_version": 1,
                "name": "start",
                "config": {},
            },
            {
                "id": "end",
                "type": "core.end",
                "type_version": 1,
                "name": "end",
                "config": {},
            },
        ],
        "edges": [{"id": "edge", "source": "start", "target": "end"}],
    }


def _capability_document(
    *,
    capability_digest: str,
    binding_digest: str,
) -> dict:
    return {
        "protocol_version": "2.0",
        "engine_version": "2.0.0",
        "capability_digest": capability_digest,
        "nodes": [
            {
                "execution_binding_digest": binding_digest,
                "type": "core.start",
                "type_version": 1,
                "contract_digest": "c" * 64,
                "implementation_digest": "i" * 64,
                "pack_id": "textile.execution-kernel",
                "pack_version": "2.0.0",
                "execution_kind": "automatic",
                "ready": True,
            }
        ],
    }


class ExecutionV2WorkerCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.Session()
        ensure_default_rbac(self.db)
        self.user = ExecutionUser(
            username="v2-worker-admin",
            display_name="v2 worker admin",
            password_hash=hash_password("test-password"),
            role="admin",
        )
        self.category = ExecutionCategory(
            key="v2-worker-test",
            name="v2 worker test",
            sort_order=1,
        )
        self.db.add_all([self.user, self.category])
        self.db.flush()
        bind_user_role(
            self.db,
            self.user,
            "admin",
            created_by_id=self.user.id,
        )
        self.workflow = create_workflow(
            self.db,
            actor=self.user,
            slug="v2-worker-capability-test",
            category_id=self.category.id,
            name="v2 worker capability test",
            description=None,
            definition=_definition(),
            capabilities={"read": True},
            is_enabled=True,
        )
        publish_workflow(
            self.db,
            workflow_id=self.workflow.id,
            expected_revision=1,
            actor=self.user,
            release_note="test",
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _create_run(self, suffix: str):
        run, _ = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number=f"V2-{suffix}",
            input_data={},
            global_data={},
            idempotency_key=f"v2-worker-{suffix}",
        )
        self.db.flush()
        return run

    def test_heartbeat_rewrites_exact_rows_only_when_digest_changes(self):
        first = _capability_document(
            capability_digest="a" * 64,
            binding_digest="1" * 64,
        )
        record_worker_heartbeat(
            self.db,
            worker_id="worker-a",
            capability_document=first,
        )
        self.db.commit()
        row = self.db.query(ExecutionWorkerNodeCapability).one()
        original_id = row.id

        record_worker_heartbeat(
            self.db,
            worker_id="worker-a",
            capability_document=first,
        )
        self.db.commit()
        self.assertEqual(
            self.db.query(ExecutionWorkerNodeCapability).one().id,
            original_id,
        )

        second = _capability_document(
            capability_digest="b" * 64,
            binding_digest="2" * 64,
        )
        record_worker_heartbeat(
            self.db,
            worker_id="worker-a",
            capability_document=second,
        )
        self.db.commit()
        replacement = self.db.query(ExecutionWorkerNodeCapability).one()
        self.assertNotEqual(replacement.id, original_id)
        self.assertEqual(replacement.execution_binding_digest, "2" * 64)

    def test_enforced_claim_skips_incompatible_queue_head(self):
        incompatible = self._create_run("incompatible")
        compatible = self._create_run("compatible")
        first = self.db.query(ExecutionNodeRun).filter_by(
            run_id=incompatible.id,
            node_id="start",
        ).one()
        second = self.db.query(ExecutionNodeRun).filter_by(
            run_id=compatible.id,
            node_id="start",
        ).one()
        first.execution_binding_digest = "1" * 64
        first.execution_kind = "automatic"
        second.execution_binding_digest = "2" * 64
        second.execution_kind = "automatic"
        record_worker_heartbeat(
            self.db,
            worker_id="worker-b",
            capability_document=_capability_document(
                capability_digest="b" * 64,
                binding_digest="2" * 64,
            ),
        )
        self.db.commit()

        with patch.object(settings, "EXECUTION_CONTRACT_MODE", "enforced"):
            claimed = claim_next_node(self.db, worker_id="worker-b")

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.run_id, compatible.id)
        self.assertEqual(claimed.execution_binding_digest, "2" * 64)
        self.assertEqual(first.status, "ready")
        attempt = self.db.query(ExecutionNodeAttempt).filter_by(
            node_run_id=claimed.id
        ).one()
        self.assertEqual(attempt.execution_kind, "automatic")
        self.assertEqual(attempt.execution_binding_digest, "2" * 64)

    def test_enforced_claim_leaves_node_ready_without_live_capability(self):
        run = self._create_run("unavailable")
        node = self.db.query(ExecutionNodeRun).filter_by(
            run_id=run.id,
            node_id="start",
        ).one()
        node.execution_binding_digest = "3" * 64
        node.execution_kind = "automatic"
        self.db.commit()

        with patch.object(settings, "EXECUTION_CONTRACT_MODE", "enforced"):
            claimed = claim_next_node(self.db, worker_id="worker-c")

        self.assertIsNone(claimed)
        self.db.refresh(node)
        self.assertEqual(node.status, "ready")
        self.assertEqual(node.attempt_count, 0)


if __name__ == "__main__":
    unittest.main()
