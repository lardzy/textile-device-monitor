from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.execution.catalog import (
    bind_user_role,
    create_workflow,
    ensure_default_rbac,
    publish_workflow,
)
from app.execution.engine import (
    EXTERNAL_NODE_TYPES,
    _node_input,
    _normalize_human_submission,
    claim_human_task,
    claim_next_node,
    complete_node,
    create_run,
    execute_claimed_node,
    fail_node,
    reject_human_task,
    retry_failed_node,
    set_run_control_status,
    submit_human_task,
)
from app.execution.errors import ExecutionApiError
from app.execution.models import (
    ExecutionCategory,
    ExecutionEvent,
    ExecutionHumanTask,
    ExecutionNodeAttempt,
    ExecutionNodeRun,
    ExecutionUser,
    ExecutionWorkflowVersion,
    utcnow,
)
from app.execution.security import hash_password


def human_definition():
    return {
        "schema_version": "1.0",
        "metadata": {"name": "人工任务测试"},
        "input_schema": {"type": "object", "properties": {}},
        "global_schema": {"type": "object", "properties": {}},
        "root_slots": [],
        "credential_slots": [],
        "nodes": [
            {"id": "start", "type": "core.start", "name": "开始", "config": {}},
            {
                "id": "input",
                "type": "human.input",
                "name": "复核输入",
                "config": {"title": "请输入复核结果"},
            },
            {
                "id": "result",
                "type": "result.aggregate",
                "name": "结果",
                "config": {},
                "input_mapping": {"answer": "$.nodes.input.output.answer"},
            },
            {
                "id": "end",
                "type": "core.end",
                "name": "结束",
                "config": {},
                "input_mapping": {"answer": "$.nodes.result.output.answer"},
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "input"},
            {"id": "e2", "source": "input", "target": "result"},
            {"id": "e3", "source": "result", "target": "end"},
        ],
    }


def parallel_definition(join_policy: str):
    nodes = [
        ("start", "core.start"),
        ("split", "parallel.split"),
        ("left", "result.aggregate"),
        ("right", "result.aggregate"),
        ("join", "parallel.join"),
        ("end", "core.end"),
    ]
    return {
        "schema_version": "1.0",
        "metadata": {"name": f"并行汇合 {join_policy}"},
        "input_schema": {"type": "object", "properties": {}},
        "global_schema": {"type": "object", "properties": {}},
        "root_slots": [],
        "credential_slots": [],
        "nodes": [
            {
                "id": node_id,
                "type": node_type,
                "name": node_id,
                "config": {},
            }
            for node_id, node_type in nodes
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "split"},
            {"id": "e2", "source": "split", "target": "left"},
            {"id": "e3", "source": "split", "target": "right"},
            {
                "id": "e4",
                "source": "left",
                "target": "join",
                "join_policy": join_policy,
            },
            {
                "id": "e5",
                "source": "right",
                "target": "join",
                "join_policy": join_policy,
            },
            {"id": "e6", "source": "join", "target": "end"},
        ],
    }


class ExecutionEngineTests(unittest.TestCase):
    def test_final_entry_node_uses_external_operation_dispatch(self):
        self.assertIn(
            "external.legacy_microscopy_check_record_entry",
            EXTERNAL_NODE_TYPES,
        )

    def test_optional_declared_node_input_allows_missing_upstream_value(self):
        run, _ = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="260111037",
            input_data={},
            global_data={},
            idempotency_key="optional-mapping-0001",
        )
        upstream = (
            self.db.query(ExecutionNodeRun)
            .filter(
                ExecutionNodeRun.run_id == run.id,
                ExecutionNodeRun.node_id == "input",
            )
            .one()
        )
        upstream.output_data = {"selected_project": {}}
        self.db.flush()

        resolved = _node_input(
            self.db,
            run,
            {
                "type": "workbook.microscopy_check_record",
                "type_version": 1,
                "input_mapping": {
                    "inspection_number": "$.run.inspection_number",
                    "indicator_requirement": (
                        "$.nodes.input.output.selected_project."
                        "indicator_requirement"
                    ),
                },
            },
        )

        self.assertEqual(resolved["inspection_number"], "260111037")
        self.assertIsNone(resolved["indicator_requirement"])

    def test_required_declared_node_input_rejects_missing_upstream_value(self):
        run, _ = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="260111037",
            input_data={},
            global_data={},
            idempotency_key="required-mapping-0001",
        )
        upstream = (
            self.db.query(ExecutionNodeRun)
            .filter(
                ExecutionNodeRun.run_id == run.id,
                ExecutionNodeRun.node_id == "input",
            )
            .one()
        )
        upstream.output_data = {}
        self.db.flush()

        with self.assertRaises(ExecutionApiError) as raised:
            _node_input(
                self.db,
                run,
                {
                    "type": "workbook.microscopy_check_record",
                    "type_version": 1,
                    "input_mapping": {
                        "inspection_number": "$.run.inspection_number",
                        "image_count": "$.nodes.input.output.image_count",
                    },
                },
            )

        self.assertEqual(raised.exception.code, "mapping_value_missing")

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.Session()
        ensure_default_rbac(self.db)
        self.user = ExecutionUser(
            username="admin",
            display_name="管理员",
            password_hash=hash_password("test-password"),
            role="admin",
        )
        self.category = ExecutionCategory(key="test", name="测试", sort_order=1)
        self.db.add_all([self.user, self.category])
        self.db.flush()
        bind_user_role(self.db, self.user, "admin", created_by_id=self.user.id)
        self.workflow = create_workflow(
            self.db,
            actor=self.user,
            slug="human-flow",
            category_id=self.category.id,
            name="人工流程",
            description=None,
            definition=human_definition(),
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

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _execute_one(self, worker_id="worker-1"):
        node = claim_next_node(self.db, worker_id=worker_id, lease_seconds=10)
        self.assertIsNotNone(node)
        lease_token = node.lease_token
        node_id = node.id
        self.db.commit()
        execute_claimed_node(self.db, node_id, lease_token)
        self.db.commit()
        return node

    def _create_parallel_run(self, join_policy: str, suffix: str):
        workflow = create_workflow(
            self.db,
            actor=self.user,
            slug=f"parallel-{join_policy}-{suffix}",
            category_id=self.category.id,
            name=f"并行流程 {join_policy}",
            description=None,
            definition=parallel_definition(join_policy),
            capabilities={"read": True},
            is_enabled=True,
        )
        publish_workflow(
            self.db,
            workflow_id=workflow.id,
            expected_revision=1,
            actor=self.user,
            release_note="test",
        )
        run, _ = create_run(
            self.db,
            workflow=workflow,
            actor=self.user,
            inspection_number=f"parallel-{suffix}",
            input_data={},
            global_data={},
            idempotency_key=f"parallel-{join_policy}-{suffix}",
        )
        self.db.commit()
        return run

    def _claim_node(self, expected_node_id: str):
        claimed = claim_next_node(
            self.db,
            worker_id=f"worker-{expected_node_id}",
            lease_seconds=30,
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.node_id, expected_node_id)
        result = (claimed.id, claimed.lease_token)
        self.db.commit()
        return result

    def test_human_task_survives_transaction_and_run_resumes(self):
        run, duplicate = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="260001",
            input_data={},
            global_data={},
            idempotency_key="run-human-0001",
        )
        self.db.commit()
        self.assertFalse(duplicate)

        self._execute_one()
        self._execute_one()
        task = self.db.query(ExecutionHumanTask).one()
        self.db.refresh(run)
        self.assertEqual(run.status, "waiting_human")
        self.assertEqual(task.status, "open")

        task = claim_human_task(
            self.db,
            task_id=task.id,
            expected_revision=task.revision,
            actor=self.user,
        )
        self.db.commit()
        submit_human_task(
            self.db,
            task_id=task.id,
            expected_revision=task.revision,
            data={"answer": 42},
            actor=self.user,
        )
        self.db.commit()

        self._execute_one()
        self._execute_one()
        self.db.refresh(run)
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.output_data, {"answer": 42})

        self.db.close()
        self.db = self.Session()
        restored = self.db.get(type(run), run.id)
        self.assertEqual(restored.status, "completed")

    def test_run_creation_is_idempotent(self):
        first, first_duplicate = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="260002",
            input_data={},
            global_data={},
            idempotency_key="run-idempotent",
        )
        second, second_duplicate = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="260002",
            input_data={},
            global_data={},
            idempotency_key="run-idempotent",
        )
        self.assertFalse(first_duplicate)
        self.assertTrue(second_duplicate)
        self.assertEqual(first.id, second.id)

        with self.assertRaises(ExecutionApiError) as captured:
            create_run(
                self.db,
                workflow=self.workflow,
                actor=self.user,
                inspection_number="different-number",
                input_data={},
                global_data={},
                idempotency_key="run-idempotent",
            )
        self.assertEqual(
            captured.exception.code,
            "run_idempotency_conflict",
        )

    def test_run_input_and_globals_are_validated_against_published_schema(self):
        definition = deepcopy(human_definition())
        definition["input_schema"] = {
            "type": "object",
            "properties": {
                "inspection_number": {"type": "string"},
                "sample_count": {"type": "integer", "minimum": 1},
            },
            "required": ["inspection_number", "sample_count"],
            "additionalProperties": False,
        }
        definition["global_schema"] = {
            "type": "object",
            "properties": {"review_mode": {"enum": ["normal", "strict"]}},
            "required": ["review_mode"],
            "additionalProperties": False,
        }
        workflow = create_workflow(
            self.db,
            actor=self.user,
            slug="schema-validated-flow",
            category_id=self.category.id,
            name="输入校验流程",
            description=None,
            definition=definition,
            capabilities={"read": True},
            is_enabled=True,
        )
        publish_workflow(
            self.db,
            workflow_id=workflow.id,
            expected_revision=1,
            actor=self.user,
            release_note="schema",
        )
        with self.assertRaises(ExecutionApiError) as captured:
            create_run(
                self.db,
                workflow=workflow,
                actor=self.user,
                inspection_number="schema-1",
                input_data={"sample_count": 0},
                global_data={"review_mode": "unknown"},
                idempotency_key="schema-invalid-run",
            )
        self.assertEqual(captured.exception.code, "run_input_invalid")

        run, duplicate = create_run(
            self.db,
            workflow=workflow,
            actor=self.user,
            inspection_number="schema-1",
            input_data={"sample_count": 2},
            global_data={"review_mode": "strict"},
            idempotency_key="schema-valid-run",
        )
        self.assertFalse(duplicate)
        self.assertEqual(run.input_data["sample_count"], 2)

    def test_human_task_submission_uses_server_side_form_schema(self):
        run, _ = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="human-schema",
            input_data={},
            global_data={},
            idempotency_key="human-schema-run",
        )
        self.db.commit()
        self._execute_one()
        self._execute_one()
        task = self.db.query(ExecutionHumanTask).filter_by(run_id=run.id).one()
        task.form_schema = {
            "type": "object",
            "properties": {"answer": {"type": "integer", "minimum": 1}},
            "required": ["answer"],
            "additionalProperties": False,
        }
        task = claim_human_task(
            self.db,
            task_id=task.id,
            expected_revision=task.revision,
            actor=self.user,
        )
        self.db.commit()
        with self.assertRaises(ExecutionApiError) as captured:
            submit_human_task(
                self.db,
                task_id=task.id,
                expected_revision=task.revision,
                data={"answer": 0},
                actor=self.user,
            )
        self.assertEqual(
            captured.exception.code,
            "human_task_input_invalid",
        )

    def test_expired_worker_lease_is_reclaimed_with_new_attempt(self):
        run, _ = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="260003",
            input_data={},
            global_data={},
            idempotency_key="run-lease-0001",
        )
        self.db.commit()
        first = claim_next_node(self.db, worker_id="dead-worker", lease_seconds=1)
        first.lease_expires_at = utcnow() - timedelta(seconds=1)
        self.db.commit()

        reclaimed = claim_next_node(
            self.db,
            worker_id="replacement-worker",
            lease_seconds=10,
        )
        self.assertEqual(reclaimed.id, first.id)
        self.assertEqual(reclaimed.attempt_count, 2)
        attempts = (
            self.db.query(ExecutionNodeAttempt)
            .filter(ExecutionNodeAttempt.node_run_id == first.id)
            .order_by(ExecutionNodeAttempt.attempt_number.asc())
            .all()
        )
        self.assertEqual([item.status for item in attempts], ["abandoned", "running"])

    def test_all_join_waits_for_both_predecessors_and_wakes_once(self):
        run = self._create_parallel_run("all", "all-join")
        self._execute_one()
        self._execute_one()
        left_id, left_token = self._claim_node("left")
        right_id, right_token = self._claim_node("right")

        complete_node(
            self.db,
            node_run_id=left_id,
            lease_token=left_token,
            output_data={"branch": "left"},
        )
        self.db.commit()
        join = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="join")
            .one()
        )
        self.assertEqual(join.status, "pending")

        complete_node(
            self.db,
            node_run_id=right_id,
            lease_token=right_token,
            output_data={"branch": "right"},
        )
        self.db.commit()
        self.db.refresh(join)
        self.assertEqual(join.status, "ready")
        ready_events = (
            self.db.query(ExecutionEvent)
            .filter(
                ExecutionEvent.run_id == run.id,
                ExecutionEvent.event_type == "node.ready",
            )
            .all()
        )
        self.assertEqual(
            [event.payload["node_id"] for event in ready_events].count("join"),
            1,
        )

    def test_any_join_wakes_on_first_predecessor_without_duplicate_event(self):
        run = self._create_parallel_run("any", "any-join")
        self._execute_one()
        self._execute_one()
        left_id, left_token = self._claim_node("left")
        right_id, right_token = self._claim_node("right")

        complete_node(
            self.db,
            node_run_id=left_id,
            lease_token=left_token,
            output_data={"branch": "left"},
        )
        self.db.commit()
        join = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="join")
            .one()
        )
        self.assertEqual(join.status, "ready")

        complete_node(
            self.db,
            node_run_id=right_id,
            lease_token=right_token,
            output_data={"branch": "right"},
        )
        self.db.commit()
        ready_events = (
            self.db.query(ExecutionEvent)
            .filter(
                ExecutionEvent.run_id == run.id,
                ExecutionEvent.event_type == "node.ready",
            )
            .all()
        )
        self.assertEqual(
            [event.payload["node_id"] for event in ready_events].count("join"),
            1,
        )

    def test_cancel_revokes_running_lease_and_rejects_late_completion(self):
        run, _ = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="cancel-running",
            input_data={},
            global_data={},
            idempotency_key="cancel-running",
        )
        self.db.commit()
        node_id, lease_token = self._claim_node("start")
        set_run_control_status(
            self.db,
            run_id=run.id,
            action="cancel",
            actor=self.user,
        )
        self.db.commit()

        with self.assertRaises(ExecutionApiError) as rejected:
            complete_node(
                self.db,
                node_run_id=node_id,
                lease_token=lease_token,
                output_data={"too": "late"},
            )
        self.assertEqual(rejected.exception.code, "run_closed")
        with self.assertRaises(ExecutionApiError) as failed_rejected:
            fail_node(
                self.db,
                node_run_id=node_id,
                lease_token=lease_token,
                error_code="late_failure",
                error_message="迟到错误",
            )
        self.assertEqual(failed_rejected.exception.code, "run_closed")
        self.db.expire_all()
        restored = self.db.get(type(run), run.id)
        self.assertEqual(restored.status, "cancelled")
        self.assertTrue(
            all(node.status == "cancelled" for node in restored.node_runs)
        )
        attempt = self.db.query(ExecutionNodeAttempt).one()
        self.assertEqual(attempt.status, "cancelled")

    def test_cancel_waits_for_claimed_publish_and_records_receipt(self):
        run, _ = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="cancel-publish",
            input_data={},
            global_data={},
            idempotency_key="cancel-publish",
        )
        publish_node = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="start")
            .one()
        )
        publish_node.node_type = "artifact.publish"
        self.db.commit()
        node_id, lease_token = self._claim_node("start")

        set_run_control_status(
            self.db,
            run_id=run.id,
            action="cancel",
            actor=self.user,
        )
        self.db.commit()
        self.db.refresh(run)
        self.db.refresh(publish_node)
        self.assertEqual(run.status, "cancel_pending")
        self.assertEqual(publish_node.status, "running")

        complete_node(
            self.db,
            node_run_id=node_id,
            lease_token=lease_token,
            output_data={"receipt_id": "receipt-1"},
        )
        self.db.commit()
        self.db.refresh(run)
        self.assertEqual(run.status, "cancelled")
        self.assertTrue(run.output_data["cancelled_after_side_effect"])
        self.assertEqual(
            run.output_data["publish_receipt"]["receipt_id"],
            "receipt-1",
        )
        events = (
            self.db.query(ExecutionEvent)
            .filter_by(run_id=run.id, event_type="run.cancelled")
            .all()
        )
        self.assertTrue(events[-1].payload["side_effect_completed"])

    def test_parallel_failure_waits_for_publish_receipt_and_cancels_siblings(self):
        run = self._create_parallel_run("all", "failure-publish")
        self._execute_one()
        self._execute_one()
        publish_node = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="left")
            .one()
        )
        publish_node.node_type = "artifact.publish"
        self.db.commit()

        publish_id, publish_lease = self._claim_node("left")
        failed_id, failed_lease = self._claim_node("right")
        fail_node(
            self.db,
            node_run_id=failed_id,
            lease_token=failed_lease,
            error_code="branch_failed",
            error_message="并行分支失败",
        )
        self.db.commit()
        self.db.refresh(run)
        self.db.refresh(publish_node)
        self.assertEqual(run.status, "failure_pending")
        self.assertEqual(publish_node.status, "running")
        self.assertIsNone(run.finished_at)

        complete_node(
            self.db,
            node_run_id=publish_id,
            lease_token=publish_lease,
            output_data={"receipt_id": "receipt-after-failure"},
        )
        self.db.commit()
        self.db.refresh(run)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "branch_failed")
        self.assertEqual(
            run.output_data["publish_receipt"]["receipt_id"],
            "receipt-after-failure",
        )
        active_nodes = (
            self.db.query(ExecutionNodeRun)
            .filter(
                ExecutionNodeRun.run_id == run.id,
                ExecutionNodeRun.status.in_(
                    ["ready", "running", "pending", "waiting_human"]
                ),
            )
            .count()
        )
        self.assertEqual(active_nodes, 0)

    def test_expired_node_stops_after_automatic_retry_limit(self):
        run, _ = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="retry-limit",
            input_data={},
            global_data={},
            idempotency_key="retry-limit",
        )
        self.db.commit()
        node_id, _lease_token = self._claim_node("start")
        node = self.db.get(ExecutionNodeRun, node_id)
        node.lease_expires_at = utcnow() - timedelta(seconds=1)
        self.db.commit()

        with patch(
            "app.execution.engine.settings.EXECUTION_NODE_MAX_ATTEMPTS",
            1,
        ):
            claimed = claim_next_node(self.db, worker_id="worker-after-limit")
        self.db.commit()
        self.assertIsNone(claimed)
        self.db.refresh(run)
        self.db.refresh(node)
        self.assertEqual(run.status, "failed")
        self.assertEqual(node.status, "failed")
        self.assertEqual(node.error_code, "node_retry_exhausted")

    def test_human_task_preparation_error_fails_without_lease_retry(self):
        run, _ = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="invalid-human-role",
            input_data={},
            global_data={},
            idempotency_key="invalid-human-role",
        )
        definition = deepcopy(run.definition_snapshot)
        next(
            node for node in definition["nodes"] if node["id"] == "input"
        )["config"]["candidate_role"] = "missing-role"
        run.definition_snapshot = definition
        self.db.commit()
        self._execute_one()
        input_id, input_lease = self._claim_node("input")

        execute_claimed_node(self.db, input_id, input_lease)
        self.db.commit()
        input_node = self.db.get(ExecutionNodeRun, input_id)
        self.db.refresh(run)
        self.assertEqual(input_node.status, "failed")
        self.assertEqual(
            input_node.error_code,
            "human_task_candidate_role_invalid",
        )
        self.assertEqual(run.status, "failed")

    def test_publish_rejects_unknown_candidate_role(self):
        definition = human_definition()
        next(
            node for node in definition["nodes"] if node["id"] == "input"
        )["config"]["candidate_role"] = "missing-role"
        workflow = create_workflow(
            self.db,
            actor=self.user,
            slug="missing-role-flow",
            category_id=self.category.id,
            name="无效角色流程",
            description=None,
            definition=definition,
            capabilities={"read": True},
            is_enabled=True,
        )
        with self.assertRaises(ExecutionApiError) as captured:
            publish_workflow(
                self.db,
                workflow_id=workflow.id,
                expected_revision=1,
                actor=self.user,
                release_note="invalid",
            )
        self.assertEqual(
            captured.exception.code,
            "workflow_candidate_roles_missing",
        )

    def test_human_rejection_cancels_parallel_open_task(self):
        run, _ = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="parallel-human-rejection",
            input_data={},
            global_data={},
            idempotency_key="parallel-human-rejection",
        )
        rejected_node = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="input")
            .one()
        )
        rejected_node.status = "waiting_human"
        sibling_node = ExecutionNodeRun(
            run_id=run.id,
            node_id="input-sibling",
            node_type="human.input",
            node_type_version=1,
            node_name="并行人工任务",
            status="waiting_human",
        )
        self.db.add(sibling_node)
        self.db.flush()
        rejected_task = ExecutionHumanTask(
            run_id=run.id,
            node_run_id=rejected_node.id,
            title="待驳回",
            status="claimed",
            claimed_by_id=self.user.id,
        )
        sibling_task = ExecutionHumanTask(
            run_id=run.id,
            node_run_id=sibling_node.id,
            title="兄弟任务",
            status="open",
        )
        self.db.add_all([rejected_task, sibling_task])
        run.status = "waiting_human"
        self.db.commit()

        reject_human_task(
            self.db,
            task_id=rejected_task.id,
            expected_revision=rejected_task.revision,
            reason="复核不通过",
            actor=self.user,
        )
        self.db.commit()
        self.db.refresh(run)
        self.db.refresh(sibling_task)
        self.db.refresh(sibling_node)
        self.assertEqual(run.status, "failed")
        self.assertEqual(sibling_task.status, "cancelled")
        self.assertEqual(sibling_node.status, "cancelled")

    def test_rejected_human_task_is_reopened_on_node_retry(self):
        run, _ = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="retry-human",
            input_data={},
            global_data={},
            idempotency_key="retry-human",
        )
        self.db.commit()
        self._execute_one()
        self._execute_one()
        task = self.db.query(ExecutionHumanTask).filter_by(run_id=run.id).one()
        task_id = task.id
        task = claim_human_task(
            self.db,
            task_id=task.id,
            expected_revision=task.revision,
            actor=self.user,
        )
        self.db.commit()
        rejected_revision = task.revision
        reject_human_task(
            self.db,
            task_id=task.id,
            expected_revision=rejected_revision,
            reason="需要重新复核",
            actor=self.user,
        )
        self.db.commit()

        retry_failed_node(
            self.db,
            run_id=run.id,
            node_id="input",
            actor=self.user,
        )
        self.db.commit()
        restored = {
            item.node_id: item.status
            for item in self.db.query(ExecutionNodeRun)
            .filter(ExecutionNodeRun.run_id == run.id)
            .all()
        }
        self.assertEqual(restored["result"], "pending")
        self.assertEqual(restored["end"], "pending")

        self._execute_one()
        reopened = self.db.query(ExecutionHumanTask).filter_by(id=task_id).one()
        self.assertEqual(reopened.status, "open")
        self.assertGreater(reopened.revision, rejected_revision)
        self.assertIsNone(reopened.claimed_by_id)
        self.assertEqual(
            self.db.query(ExecutionHumanTask).filter_by(run_id=run.id).count(),
            1,
        )
        reopened = claim_human_task(
            self.db,
            task_id=reopened.id,
            expected_revision=reopened.revision,
            actor=self.user,
        )
        self.db.commit()
        submit_human_task(
            self.db,
            task_id=reopened.id,
            expected_revision=reopened.revision,
            data={"answer": "复核通过"},
            actor=self.user,
        )
        self.db.commit()
        self._execute_one()
        self._execute_one()
        self.db.refresh(run)
        self.assertEqual(run.status, "completed")

    def test_pause_accepts_completion_without_unpausing_run(self):
        run, _ = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="pause-complete",
            input_data={},
            global_data={},
            idempotency_key="pause-complete",
        )
        self.db.commit()
        node_id, lease_token = self._claim_node("start")
        set_run_control_status(
            self.db,
            run_id=run.id,
            action="pause",
            actor=self.user,
        )
        self.db.commit()
        complete_node(
            self.db,
            node_run_id=node_id,
            lease_token=lease_token,
            output_data={},
        )
        self.db.commit()
        self.db.refresh(run)
        self.assertEqual(run.status, "paused")
        next_node = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="input")
            .one()
        )
        self.assertEqual(next_node.status, "ready")

        set_run_control_status(
            self.db,
            run_id=run.id,
            action="resume",
            actor=self.user,
        )
        self.db.commit()
        self.db.refresh(run)
        self.assertEqual(run.status, "running")

    def test_pause_preserves_failure_until_resume(self):
        run, _ = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="pause-fail",
            input_data={},
            global_data={},
            idempotency_key="pause-fail",
        )
        self.db.commit()
        node_id, lease_token = self._claim_node("start")
        set_run_control_status(
            self.db,
            run_id=run.id,
            action="pause",
            actor=self.user,
        )
        self.db.commit()
        fail_node(
            self.db,
            node_run_id=node_id,
            lease_token=lease_token,
            error_code="test_failure",
            error_message="测试失败",
        )
        self.db.commit()
        self.db.refresh(run)
        self.assertEqual(run.status, "paused")

        set_run_control_status(
            self.db,
            run_id=run.id,
            action="resume",
            actor=self.user,
        )
        self.db.commit()
        self.db.refresh(run)
        self.assertEqual(run.status, "failed")

    def test_cancel_closes_human_task_and_fences_submit_and_reject(self):
        run, _ = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="cancel-human",
            input_data={},
            global_data={},
            idempotency_key="cancel-human",
        )
        self.db.commit()
        self._execute_one()
        self._execute_one()
        task = self.db.query(ExecutionHumanTask).one()
        task = claim_human_task(
            self.db,
            task_id=task.id,
            expected_revision=task.revision,
            actor=self.user,
        )
        self.db.commit()
        stale_revision = task.revision

        set_run_control_status(
            self.db,
            run_id=run.id,
            action="cancel",
            actor=self.user,
        )
        self.db.commit()
        self.db.refresh(task)
        self.assertEqual(task.status, "cancelled")
        with self.assertRaises(ExecutionApiError) as submit_rejected:
            submit_human_task(
                self.db,
                task_id=task.id,
                expected_revision=stale_revision,
                data={"answer": 1},
                actor=self.user,
            )
        self.assertEqual(submit_rejected.exception.code, "run_closed")
        with self.assertRaises(ExecutionApiError) as reject_rejected:
            reject_human_task(
                self.db,
                task_id=task.id,
                expected_revision=stale_revision,
                reason="too late",
                actor=self.user,
            )
        self.assertEqual(reject_rejected.exception.code, "run_closed")

    def test_human_submit_while_paused_keeps_run_paused(self):
        run, _ = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="pause-human",
            input_data={},
            global_data={},
            idempotency_key="pause-human",
        )
        self.db.commit()
        self._execute_one()
        self._execute_one()
        task = self.db.query(ExecutionHumanTask).one()
        task = claim_human_task(
            self.db,
            task_id=task.id,
            expected_revision=task.revision,
            actor=self.user,
        )
        self.db.commit()
        set_run_control_status(
            self.db,
            run_id=run.id,
            action="pause",
            actor=self.user,
        )
        self.db.commit()
        submit_human_task(
            self.db,
            task_id=task.id,
            expected_revision=task.revision,
            data={"answer": 7},
            actor=self.user,
        )
        self.db.commit()
        self.db.refresh(run)
        self.assertEqual(run.status, "paused")
        result = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="result")
            .one()
        )
        self.assertEqual(result.status, "ready")

    def test_retry_is_forbidden_after_run_cancel_or_completion(self):
        for terminal_status in ("cancelled", "completed"):
            with self.subTest(terminal_status=terminal_status):
                run, _ = create_run(
                    self.db,
                    workflow=self.workflow,
                    actor=self.user,
                    inspection_number=f"retry-{terminal_status}",
                    input_data={},
                    global_data={},
                    idempotency_key=f"retry-{terminal_status}",
                )
                node = (
                    self.db.query(ExecutionNodeRun)
                    .filter_by(run_id=run.id, node_id="start")
                    .one()
                )
                node.status = "failed"
                run.status = terminal_status
                self.db.commit()
                with self.assertRaises(ExecutionApiError) as rejected:
                    retry_failed_node(
                        self.db,
                        run_id=run.id,
                        node_id="start",
                        actor=self.user,
                    )
                self.assertEqual(rejected.exception.code, "run_not_retryable")

    def test_microscopy_print_decision_allows_print_and_skip_with_sha_binding(self):
        sha256 = "a" * 64
        node_run = SimpleNamespace(
            node_type="human.confirm",
            input_data={
                "task_kind": "microscopy_print_confirmation",
                "artifact": {
                    "artifact_id": "artifact-1",
                    "content_sha256": sha256,
                },
            },
        )
        for decision, printed in (("print", True), ("skip", False)):
            normalized = _normalize_human_submission(
                self.db,
                run=SimpleNamespace(),
                node_run=node_run,
                data={
                    "print_decision": decision,
                    **(
                        {"print_completed": True}
                        if decision == "print"
                        else {}
                    ),
                    "artifact_sha256": sha256,
                },
            )
            self.assertEqual(normalized["print_decision"], decision)
            self.assertIs(normalized["print_requested"], decision == "print")
            self.assertIs(normalized["print_completed"], printed)
            self.assertIs(normalized["printed"], printed)
            self.assertEqual(normalized["artifact_sha256"], sha256)

        with self.assertRaises(ExecutionApiError) as raised:
            _normalize_human_submission(
                self.db,
                run=SimpleNamespace(),
                node_run=node_run,
                data={
                    "print_decision": "skip",
                    "artifact_sha256": "b" * 64,
                },
            )
        self.assertEqual(
            raised.exception.code,
            "microscopy_print_artifact_changed",
        )

    def test_microscopy_print_requires_explicit_completion_confirmation(self):
        sha256 = "d" * 64
        node_run = SimpleNamespace(
            node_type="human.confirm",
            input_data={
                "task_kind": "microscopy_print_confirmation",
                "artifact": {"content_sha256": sha256},
            },
        )
        for data in (
            {
                "print_decision": "print",
                "artifact_sha256": sha256,
            },
            {
                "print_decision": "print",
                "print_completed": False,
                "artifact_sha256": sha256,
            },
        ):
            with self.subTest(data=data):
                with self.assertRaises(ExecutionApiError) as raised:
                    _normalize_human_submission(
                        self.db,
                        run=SimpleNamespace(),
                        node_run=node_run,
                        data=data,
                    )
                self.assertEqual(
                    raised.exception.code,
                    "microscopy_print_not_completed",
                )

    def test_microscopy_skip_ignores_spoofed_completion_flag(self):
        sha256 = "e" * 64
        normalized = _normalize_human_submission(
            self.db,
            run=SimpleNamespace(),
            node_run=SimpleNamespace(
                node_type="human.confirm",
                input_data={
                    "task_kind": "microscopy_print_confirmation",
                    "artifact": {"content_sha256": sha256},
                },
            ),
            data={
                "print_decision": "skip",
                "print_completed": True,
                "artifact_sha256": sha256,
            },
        )
        self.assertFalse(normalized["print_requested"])
        self.assertFalse(normalized["print_completed"])
        self.assertFalse(normalized["printed"])

    def test_microscopy_print_keeps_legacy_printed_true_compatibility(self):
        sha256 = "c" * 64
        normalized = _normalize_human_submission(
            self.db,
            run=SimpleNamespace(),
            node_run=SimpleNamespace(
                node_type="human.confirm",
                input_data={
                    "task_kind": "microscopy_print_confirmation",
                    "artifact": {"content_sha256": sha256},
                },
            ),
            data={"printed": True, "artifact_sha256": sha256},
        )
        self.assertEqual(normalized["print_decision"], "print")
        self.assertTrue(normalized["print_requested"])
        self.assertTrue(normalized["print_completed"])
        self.assertTrue(normalized["printed"])

    def test_published_version_is_immutable_snapshot(self):
        version = self.db.query(ExecutionWorkflowVersion).one()
        run, _ = create_run(
            self.db,
            workflow=self.workflow,
            actor=self.user,
            inspection_number="260004",
            input_data={},
            global_data={},
            idempotency_key="run-snapshot-1",
        )
        original_checksum = run.definition_checksum
        original_contract_checksum = run.contract_checksum
        original_capabilities = dict(run.capabilities_snapshot)
        changed = human_definition()
        changed["metadata"]["name"] = "修改后的草稿"
        self.workflow.draft_definition = changed
        self.workflow.capabilities = {
            "read": True,
            "write": True,
            "hidden": True,
        }
        self.db.commit()
        self.db.refresh(run)
        self.assertEqual(run.definition_checksum, original_checksum)
        self.assertEqual(run.contract_checksum, original_contract_checksum)
        self.assertEqual(run.definition_snapshot, version.definition)
        self.assertEqual(run.capabilities_snapshot, original_capabilities)
        self.assertEqual(run.capabilities_snapshot, version.capabilities)
        self.assertNotEqual(
            run.capabilities_snapshot,
            self.workflow.capabilities,
        )


if __name__ == "__main__":
    unittest.main()
