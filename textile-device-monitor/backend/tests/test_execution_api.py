from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import Request, Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.execution import (
    AuthContext,
    human_task_detail,
    list_runs,
    login,
    logout,
    router,
    run_detail,
    run_event_history,
    update_user,
    workflow_detail,
    workflows,
)
from app.database import Base
from app.execution.catalog import (
    bind_user_role,
    create_workflow,
    ensure_default_catalog,
    ensure_default_rbac,
    publish_workflow,
)
from app.execution.engine import claim_next_node, create_run, execute_claimed_node
from app.execution.errors import ExecutionApiError
from app.execution.events import append_run_event
from app.execution.models import (
    ExecutionCategory,
    ExecutionHumanTask,
    ExecutionLoginThrottle,
    ExecutionUser,
)
from app.execution.schemas import LoginRequest, UserUpdate
from app.execution.schemas import FileRefreshRequest
from app.execution.security import (
    clear_login_failures,
    create_session,
    csrf_token_for_session,
    hash_password,
    login_throttle_key,
    record_login_failure,
    require_csrf,
    require_login_not_throttled,
    require_permission,
    resolve_session,
)


class ExecutionApiContractTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()
        ensure_default_rbac(self.db)
        ensure_default_catalog(self.db)
        self.admin = ExecutionUser(
            username="admin",
            display_name="管理员",
            password_hash=hash_password("admin-password"),
            role="admin",
        )
        self.user = ExecutionUser(
            username="operator",
            display_name="操作员",
            password_hash=hash_password("operator-password"),
            role="user",
        )
        self.db.add_all([self.admin, self.user])
        self.db.flush()
        bind_user_role(
            self.db,
            self.admin,
            "admin",
            created_by_id=self.admin.id,
        )
        bind_user_role(
            self.db,
            self.user,
            "user",
            created_by_id=self.admin.id,
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _create_owned_run(self, actor: ExecutionUser, *, suffix: str):
        category = self.db.query(ExecutionCategory).first()
        definition = {
            "schema_version": "1.0",
            "metadata": {"name": f"事件分页-{suffix}"},
            "input_schema": {"type": "object", "properties": {}},
            "global_schema": {"type": "object", "properties": {}},
            "root_slots": [],
            "credential_slots": [],
            "nodes": [
                {
                    "id": "start",
                    "type": "core.start",
                    "name": "开始",
                    "config": {},
                },
                {
                    "id": "end",
                    "type": "core.end",
                    "name": "结束",
                    "config": {},
                },
            ],
            "edges": [{"source": "start", "target": "end"}],
        }
        workflow = create_workflow(
            self.db,
            actor=self.admin,
            slug=f"event-history-{suffix}",
            category_id=category.id,
            name=f"事件分页-{suffix}",
            description=None,
            definition=definition,
            capabilities={"read": True},
            is_enabled=True,
        )
        publish_workflow(
            self.db,
            workflow_id=workflow.id,
            expected_revision=1,
            actor=self.admin,
            release_note="test",
        )
        run, _ = create_run(
            self.db,
            workflow=workflow,
            actor=actor,
            inspection_number=f"EVENT-{suffix}",
            input_data={},
            global_data={},
            idempotency_key=f"event-history-{suffix}",
        )
        self.db.commit()
        return run

    def test_router_exposes_versioned_contract(self):
        paths = {(method, route.path) for route in router.routes for method in route.methods}
        expected = {
            ("POST", "/execution/v1/auth/login"),
            ("GET", "/execution/v1/auth/me"),
            ("GET", "/execution/v1/auth/csrf"),
            ("GET", "/execution/v1/categories"),
            ("GET", "/execution/v1/workflows"),
            ("POST", "/execution/v1/runs"),
            ("GET", "/execution/v1/runs/{run_id}/events"),
            ("GET", "/execution/v1/runs/{run_id}/event-history"),
            ("GET", "/execution/v1/runs/{run_id}/mutations"),
            (
                "POST",
                "/execution/v1/runs/{run_id}/mutations/preflight",
            ),
            (
                "POST",
                "/execution/v1/runs/{run_id}/mutations/{mutation_id}/copy",
            ),
            (
                "POST",
                "/execution/v1/runs/{run_id}/mutations/{mutation_id}/write",
            ),
            (
                "POST",
                "/execution/v1/runs/{run_id}/mutations/{mutation_id}/verify",
            ),
            (
                "POST",
                "/execution/v1/runs/{run_id}/mutations/{mutation_id}/publish",
            ),
            ("POST", "/execution/v1/human-tasks/{task_id}/submit"),
            ("GET", "/execution/v1/human-tasks/{task_id}"),
            ("GET", "/execution/v1/artifacts/{artifact_id}"),
            ("GET", "/execution/v1/artifacts/{artifact_id}/preview"),
            ("GET", "/execution/v1/artifacts/{artifact_id}/download"),
            ("GET", "/execution/v1/publish-receipts/{receipt_id}"),
            ("GET", "/execution/v1/audit"),
            ("GET", "/execution/v1/files/search"),
            ("POST", "/execution/v1/files/refresh"),
        }
        self.assertTrue(expected.issubset(paths), expected - paths)

    def test_workflow_detail_exposes_only_the_published_definition_to_user(self):
        run = self._create_owned_run(self.user, suffix="published-preview")

        payload = workflow_detail(
            run.workflow_id,
            auth=AuthContext(session=None, user=self.user),
            db=self.db,
        )

        self.assertEqual(
            payload["published_definition"]["metadata"]["name"],
            "事件分页-published-preview",
        )
        self.assertNotIn("draft_definition", payload)

    def test_run_list_supports_status_groups_and_stable_pagination(self):
        running = self._create_owned_run(self.user, suffix="running-list")
        waiting = self._create_owned_run(self.user, suffix="waiting-list")
        completed = self._create_owned_run(self.user, suffix="completed-list")
        running.status = "running"
        waiting.status = "waiting_human"
        completed.status = "completed"
        self.db.commit()
        auth = AuthContext(session=None, user=self.user)

        first_page = list_runs(
            status=None,
            status_group="active",
            inspection_number=None,
            workflow_id=None,
            offset=0,
            limit=1,
            auth=auth,
            db=self.db,
        )
        second_page = list_runs(
            status=None,
            status_group="active",
            inspection_number=None,
            workflow_id=None,
            offset=1,
            limit=1,
            auth=auth,
            db=self.db,
        )
        terminal_page = list_runs(
            status=None,
            status_group="terminal",
            inspection_number=None,
            workflow_id=None,
            offset=0,
            limit=20,
            auth=auth,
            db=self.db,
        )
        workflow_page = list_runs(
            status=None,
            status_group=None,
            inspection_number=None,
            workflow_id=waiting.workflow_id,
            offset=0,
            limit=20,
            auth=auth,
            db=self.db,
        )

        self.assertEqual(first_page["total"], 2)
        self.assertEqual(len(first_page["items"]), 1)
        self.assertEqual(len(second_page["items"]), 1)
        self.assertNotEqual(
            first_page["items"][0]["id"],
            second_page["items"][0]["id"],
        )
        self.assertEqual(terminal_page["total"], 1)
        self.assertEqual(
            terminal_page["items"][0]["inspection_number"],
            "EVENT-completed-list",
        )
        self.assertEqual(workflow_page["total"], 1)
        self.assertEqual(
            workflow_page["items"][0]["workflow_id"],
            waiting.workflow_id,
        )
        self.assertIn("node_progress", first_page["items"][0])

    def test_mutation_routes_enforce_permission_and_csrf_contract(self):
        expected = {
            "/execution/v1/runs/{run_id}/mutations/preflight": "file.write",
            (
                "/execution/v1/runs/{run_id}/mutations/"
                "{mutation_id}/copy"
            ): "file.write",
            (
                "/execution/v1/runs/{run_id}/mutations/"
                "{mutation_id}/write"
            ): "file.write",
            (
                "/execution/v1/runs/{run_id}/mutations/"
                "{mutation_id}/verify"
            ): "file.write",
            (
                "/execution/v1/runs/{run_id}/mutations/"
                "{mutation_id}/publish"
            ): "file.publish",
        }
        routes = {
            route.path: route
            for route in router.routes
            if "POST" in route.methods and route.path in expected
        }
        self.assertEqual(set(routes), set(expected))
        for path, permission_key in expected.items():
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
                guards[0].execution_permission_key,
                permission_key,
            )
            self.assertTrue(guards[0].execution_csrf_required)

    def test_reconciliation_routes_use_dedicated_admin_permission(self):
        expected = {
            (
                "/execution/v1/external-operations/"
                "{operation_id}/reconciliation"
            ): ("GET", False),
            (
                "/execution/v1/external-operations/"
                "{operation_id}/reconcile"
            ): ("POST", True),
        }
        routes = {
            route.path: route
            for route in router.routes
            if route.path in expected
        }
        self.assertEqual(set(routes), set(expected))
        for path, (method, csrf_required) in expected.items():
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
                guards[0].execution_permission_key,
                "external_operation.reconcile",
            )
            self.assertEqual(
                guards[0].execution_csrf_required,
                csrf_required,
            )

    def test_run_event_history_pages_stably_and_checks_visibility(self):
        run = self._create_owned_run(self.user, suffix="owner")
        custom_event_ids = []
        for index in range(5):
            event = append_run_event(
                self.db,
                run_id=run.id,
                event_type="test.history",
                payload={"index": index},
            )
            custom_event_ids.append(event.id)
        self.db.commit()

        auth = AuthContext(session=None, user=self.user)
        latest = run_event_history(
            run.id,
            after_id=None,
            before_id=None,
            limit=2,
            auth=auth,
            db=self.db,
        )
        self.assertEqual(
            [item["payload"]["index"] for item in latest["items"]],
            [3, 4],
        )
        self.assertTrue(latest["has_more"])
        self.assertEqual(
            latest["cursors"],
            {
                "before_id": custom_event_ids[3],
                "after_id": custom_event_ids[4],
            },
        )

        older = run_event_history(
            run.id,
            after_id=None,
            before_id=custom_event_ids[3],
            limit=2,
            auth=auth,
            db=self.db,
        )
        self.assertEqual(
            [item["payload"]["index"] for item in older["items"]],
            [1, 2],
        )
        self.assertTrue(older["has_more"])

        newer = run_event_history(
            run.id,
            after_id=custom_event_ids[1],
            before_id=None,
            limit=2,
            auth=auth,
            db=self.db,
        )
        self.assertEqual(
            [item["payload"]["index"] for item in newer["items"]],
            [2, 3],
        )
        self.assertTrue(newer["has_more"])

        other_run = self._create_owned_run(self.admin, suffix="admin")
        with self.assertRaises(ExecutionApiError) as hidden:
            run_event_history(
                other_run.id,
                after_id=None,
                before_id=None,
                limit=100,
                auth=auth,
                db=self.db,
            )
        self.assertEqual(hidden.exception.code, "resource_not_found")

    def test_run_snapshot_keeps_latest_hundred_events_and_history_cursor(self):
        run = self._create_owned_run(self.user, suffix="snapshot")
        custom_event_ids = []
        for index in range(101):
            event = append_run_event(
                self.db,
                run_id=run.id,
                event_type="test.snapshot",
                payload={"index": index},
            )
            custom_event_ids.append(event.id)
        self.db.commit()

        payload = run_detail(
            run.id,
            auth=AuthContext(session=None, user=self.user),
            db=self.db,
        )
        self.assertEqual(len(payload["events"]), 100)
        self.assertEqual(payload["events"][0]["payload"]["index"], 1)
        self.assertEqual(payload["events"][-1]["payload"]["index"], 100)
        self.assertEqual(
            payload["event_history"],
            {
                "has_more": True,
                "cursors": {
                    "before_id": custom_event_ids[1],
                    "after_id": custom_event_ids[100],
                },
            },
        )

    def test_file_refresh_depth_is_limited_to_first_level(self):
        payload = FileRefreshRequest(
            root_id="special_wool_records",
            max_depth=1,
        )
        self.assertEqual(payload.max_depth, 1)
        with self.assertRaises(ValueError):
            FileRefreshRequest(
                root_id="special_wool_records",
                max_depth=2,
            )

    def test_login_needs_no_csrf_and_creates_bound_csrf_token(self):
        response = Response()
        payload = login(
            LoginRequest(username="admin", password="admin-password"),
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/execution/v1/auth/login",
                    "headers": [],
                    "client": ("127.0.0.1", 12345),
                }
            ),
            response=response,
            db=self.db,
        )
        self.assertIn("csrf_token", payload)
        cookie_header = response.headers["set-cookie"]
        raw_session = cookie_header.split("execution_session=", 1)[1].split(";", 1)[0]
        session, user = resolve_session(self.db, raw_session)
        self.assertEqual(user.id, self.admin.id)
        self.assertEqual(payload["csrf_token"], csrf_token_for_session(session))
        require_csrf(session, payload["csrf_token"])
        with self.assertRaises(ExecutionApiError) as captured:
            require_csrf(session, "wrong-token")
        self.assertEqual(captured.exception.code, "csrf_invalid")

    def test_logout_deletes_cookie_with_matching_security_attributes(self):
        session, _, _ = create_session(self.db, self.admin)
        self.db.commit()
        response = Response()

        with patch(
            "app.api.execution.settings.EXECUTION_COOKIE_SECURE",
            True,
        ):
            result = logout(
                response=response,
                auth=AuthContext(session=session, user=self.admin),
                db=self.db,
            )

        self.assertEqual(result, {"success": True})
        cookie_header = response.headers["set-cookie"]
        self.assertIn("execution_session=", cookie_header)
        self.assertIn("HttpOnly", cookie_header)
        self.assertIn("Path=/api/execution", cookie_header)
        self.assertIn("SameSite=strict", cookie_header)
        self.assertIn("Secure", cookie_header)

    def test_explicit_role_permissions_separate_designer_and_operator(self):
        require_permission(self.db, self.admin, "workflow.design")
        require_permission(self.db, self.user, "workflow.run")
        with self.assertRaises(ExecutionApiError) as captured:
            require_permission(self.db, self.user, "workflow.design")
        self.assertEqual(captured.exception.code, "permission_denied")

    def test_candidate_role_user_can_open_minimal_human_task_detail(self):
        category = self.db.query(ExecutionCategory).first()
        definition = {
            "schema_version": "1.0",
            "metadata": {"name": "跨用户复核"},
            "input_schema": {"type": "object", "properties": {}},
            "global_schema": {"type": "object", "properties": {}},
            "root_slots": [],
            "credential_slots": [],
            "nodes": [
                {
                    "id": "start",
                    "type": "core.start",
                    "name": "开始",
                    "config": {},
                },
                {
                    "id": "review",
                    "type": "human.input",
                    "name": "复核",
                    "config": {
                        "candidate_role": "user",
                        "title": "请复核检测结果",
                    },
                    "input_mapping": {
                        "summary": "$.inputs.inspection_number"
                    },
                },
                {
                    "id": "end",
                    "type": "core.end",
                    "name": "结束",
                    "config": {},
                },
            ],
            "edges": [
                {"source": "start", "target": "review"},
                {"source": "review", "target": "end"},
            ],
        }
        workflow = create_workflow(
            self.db,
            actor=self.admin,
            slug="cross-user-review",
            category_id=category.id,
            name="跨用户复核",
            description=None,
            definition=definition,
            capabilities={"read": True},
            is_enabled=True,
        )
        publish_workflow(
            self.db,
            workflow_id=workflow.id,
            expected_revision=1,
            actor=self.admin,
            release_note="test",
        )
        run, _ = create_run(
            self.db,
            workflow=workflow,
            actor=self.admin,
            inspection_number="26X-REVIEW",
            input_data={},
            global_data={},
            idempotency_key="cross-user-review",
        )
        self.db.commit()
        for _ in range(2):
            node = claim_next_node(self.db, worker_id="api-test-worker")
            self.assertIsNotNone(node)
            node_id, lease_token = node.id, node.lease_token
            self.db.commit()
            execute_claimed_node(self.db, node_id, lease_token)
            self.db.commit()
        task = self.db.query(ExecutionHumanTask).filter_by(run_id=run.id).one()

        detail = human_task_detail(
            task.id,
            auth=AuthContext(session=None, user=self.user),
            db=self.db,
        )
        self.assertEqual(detail["task"]["id"], task.id)
        self.assertEqual(detail["run"]["inspection_number"], "26X-REVIEW")
        self.assertEqual(detail["node_run"]["input_data"]["summary"], "26X-REVIEW")
        self.assertNotIn("definition_snapshot", detail["run"])

    def test_system_write_acceptance_workflow_is_hidden_from_normal_users(self):
        normal = workflows(
            categories=[],
            query=None,
            auth=AuthContext(session=None, user=self.user),
            db=self.db,
        )
        admin = workflows(
            categories=[],
            query=None,
            auth=AuthContext(session=None, user=self.admin),
            db=self.db,
        )
        normal_slugs = {item["slug"] for item in normal["items"]}
        admin_slugs = {item["slug"] for item in admin["items"]}
        self.assertNotIn(
            "system-controlled-xlsx-write-test",
            normal_slugs,
        )
        self.assertIn(
            "system-controlled-xlsx-write-test",
            admin_slugs,
        )

    def test_login_failures_are_rate_limited_and_can_be_cleared(self):
        key = login_throttle_key("operator", "127.0.0.1")
        for _ in range(5):
            record_login_failure(self.db, key)
            self.db.commit()
        with self.assertRaises(ExecutionApiError) as limited:
            require_login_not_throttled(self.db, key)
        self.assertEqual(limited.exception.code, "login_rate_limited")
        self.assertIn("Retry-After", limited.exception.headers)

        clear_login_failures(self.db, key)
        self.db.commit()
        require_login_not_throttled(self.db, key)
        self.assertIsNone(self.db.get(ExecutionLoginThrottle, key))

    def test_password_change_revokes_existing_sessions(self):
        session, _token, _csrf = create_session(self.db, self.user)
        self.db.commit()
        update_user(
            self.user.id,
            UserUpdate(password="operator-new-password"),
            auth=AuthContext(session=None, user=self.admin),
            db=self.db,
        )
        self.db.refresh(session)
        self.assertIsNotNone(session.revoked_at)


if __name__ == "__main__":
    unittest.main()
