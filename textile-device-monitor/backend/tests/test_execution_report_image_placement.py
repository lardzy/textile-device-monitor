from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.execution.catalog import (
    _electron_cross_section_gbt36422_definition,
    _electron_microscopy_gbt36422_definition,
    bind_user_role,
    ensure_default_catalog,
    ensure_default_rbac,
)
from app.execution.engine import (
    NodeExecutionContext,
    _normalize_human_submission,
    create_run,
    submit_human_task,
)
from app.execution.errors import ExecutionApiError
from app.execution.models import (
    ExecutionHumanTask,
    ExecutionNodeRun,
    ExecutionStorageRoot,
    ExecutionUser,
    ExecutionWorkflow,
    ExecutionWorkflowVersion,
)
from app.execution.persistence import register_persistence_executors
from app.execution.report_image_placement import (
    REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY,
    REPORT_IMAGE_ROOT_ID,
    auto_complete_report_image_placement,
    build_placement_plan,
    execute_placement_plan,
    report_image_placement_form_schema,
)
from app.execution.security import hash_password
from app.execution.storage import FileGateway, StorageRoot
from app.execution.validation import (
    definition_checksum,
    validate_definition,
    workflow_contract_checksum,
)


ELECTRON_ROOT_ID = "electron_microscopy_records"


def _image(
    name: str,
    relative_path: str,
    size: int = 10,
    *,
    fingerprint: str = "",
) -> dict:
    return {
        "id": f"img-{name}",
        "root_id": ELECTRON_ROOT_ID,
        "relative_path": relative_path,
        "name": name,
        "suffix": Path(name).suffix.casefold(),
        "size": size,
        "fingerprint": fingerprint,
    }


class ReportImagePlacementPlanTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.source_path = Path(self.tempdir.name) / "source"
        self.source_path.mkdir(parents=True)
        self.target_path = Path(self.tempdir.name) / "report"
        self.target_path.mkdir(parents=True)
        self.gateway = FileGateway(
            [
                StorageRoot(root_id=ELECTRON_ROOT_ID, path=self.source_path),
                StorageRoot(
                    root_id=REPORT_IMAGE_ROOT_ID,
                    path=self.target_path,
                    writable=True,
                ),
            ]
        )
        (self.source_path / "260000001" / "正面").mkdir(parents=True)
        (self.source_path / "260000001" / "正面" / "a.bmp").write_bytes(b"aaaa")
        (self.source_path / "260000001" / "正面" / "b.bmp").write_bytes(b"bbbbbb")

    def tearDown(self):
        self.tempdir.cleanup()

    def _images(self):
        images = []
        for name in ("a.bmp", "b.bmp"):
            relative_path = f"260000001/正面/{name}"
            stat = (self.source_path / relative_path).stat()
            images.append(
                _image(
                    name,
                    relative_path,
                    size=stat.st_size,
                    fingerprint=f"{stat.st_size}:{stat.st_mtime_ns}",
                )
            )
        return images

    def _config(self, **overrides):
        config = {
            "report_image_placement": True,
            "target_root_id": REPORT_IMAGE_ROOT_ID,
            "target_directory": REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY,
        }
        config.update(overrides)
        return config

    def test_plan_names_images_with_identity_in_selection_order(self):
        plan = build_placement_plan(
            self.gateway,
            config=self._config(),
            inspection_number="260000001",
            sample_identity="正面",
            selected_images=self._images(),
        )
        self.assertEqual(
            [item["target_filename"] for item in plan["files"]],
            ["260000001-正面.bmp", "260000001-正面-1.bmp"],
        )
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(
            plan["target_relative_dir"],
            f"{REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY}/260000001",
        )
        self.assertTrue(plan["display_directory"].endswith("\\260000001"))
        self.assertIn("1-微观形貌-GB T 36422", plan["display_directory"])
        self.assertEqual(plan["files"][0]["source"]["id"], "img-a.bmp")
        self.assertEqual(
            plan["files"][0]["source"]["fingerprint"],
            self._images()[0]["fingerprint"],
        )

    def test_plan_without_identity_falls_back_to_plain_number(self):
        plan = build_placement_plan(
            self.gateway,
            config=self._config(),
            inspection_number="260000001",
            sample_identity=None,
            selected_images=self._images(),
        )
        self.assertEqual(
            [item["target_filename"] for item in plan["files"]],
            ["260000001.bmp", "260000001-1.bmp"],
        )

    def test_plan_sanitizes_identity_filename_characters(self):
        plan = build_placement_plan(
            self.gateway,
            config=self._config(),
            inspection_number="260000001",
            sample_identity='正/面: 试验"段"?',
            selected_images=self._images()[:1],
        )
        self.assertEqual(
            plan["files"][0]["target_filename"],
            "260000001-正-面- 试验-段--.bmp",
        )

    def test_plan_rejects_unsafe_inspection_number(self):
        with self.assertRaises(ExecutionApiError) as raised:
            build_placement_plan(
                self.gateway,
                config=self._config(),
                inspection_number="26/000001",
                sample_identity=None,
                selected_images=self._images(),
            )
        self.assertEqual(
            raised.exception.code, "report_image_inspection_number_invalid"
        )

    def test_plan_rejects_invalid_target_directory(self):
        with self.assertRaises(ExecutionApiError) as raised:
            build_placement_plan(
                self.gateway,
                config=self._config(target_directory="..\\escape"),
                inspection_number="260000001",
                sample_identity=None,
                selected_images=self._images(),
            )
        self.assertEqual(
            raised.exception.code, "report_image_target_directory_invalid"
        )

    def test_plan_requires_selected_images(self):
        with self.assertRaises(ExecutionApiError) as raised:
            build_placement_plan(
                self.gateway,
                config=self._config(),
                inspection_number="260000001",
                sample_identity=None,
                selected_images=[],
            )
        self.assertEqual(
            raised.exception.code, "report_image_selection_missing"
        )

    def test_plan_detects_same_name_conflicts(self):
        target_dir = (
            self.target_path
            / REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY
            / "260000001"
        )
        target_dir.mkdir(parents=True)
        (target_dir / "260000001-正面-1.bmp").write_bytes(b"old")
        plan = build_placement_plan(
            self.gateway,
            config=self._config(),
            inspection_number="260000001",
            sample_identity="正面",
            selected_images=self._images(),
        )
        self.assertEqual(plan["conflicts"], ["260000001-正面-1.bmp"])
        self.assertEqual(plan["conflict_count"], 1)

    def test_execute_places_files_with_hash_receipt(self):
        plan = build_placement_plan(
            self.gateway,
            config=self._config(),
            inspection_number="260000001",
            sample_identity="正面",
            selected_images=self._images(),
        )
        receipt = execute_placement_plan(self.gateway, plan, overwrite=False)
        self.assertFalse(receipt["placement_cancelled"])
        self.assertEqual(receipt["placed_count"], 2)
        self.assertEqual(receipt["overwritten_files"], [])
        target_dir = self.target_path / plan["target_relative_dir"]
        first = (target_dir / "260000001-正面.bmp").read_bytes()
        second = (target_dir / "260000001-正面-1.bmp").read_bytes()
        self.assertEqual(first, b"aaaa")
        self.assertEqual(second, b"bbbbbb")
        self.assertEqual(
            receipt["placed_files"][0]["content_sha256"],
            hashlib.sha256(b"aaaa").hexdigest(),
        )

    def test_execute_without_overwrite_refuses_race_conflict(self):
        plan = build_placement_plan(
            self.gateway,
            config=self._config(),
            inspection_number="260000001",
            sample_identity="正面",
            selected_images=self._images(),
        )
        target_dir = (
            self.target_path / plan["target_relative_dir"]
        )
        target_dir.mkdir(parents=True)
        (target_dir / "260000001-正面-1.bmp").write_bytes(b"old")
        with self.assertRaises(ExecutionApiError) as raised:
            execute_placement_plan(self.gateway, plan, overwrite=False)
        self.assertEqual(
            raised.exception.code, "report_image_conflict_detected"
        )
        # 整批在提交前完成预留；竞态冲突不会遗留前面的图片。
        self.assertFalse((target_dir / "260000001-正面.bmp").exists())
        self.assertEqual(
            (target_dir / "260000001-正面-1.bmp").read_bytes(), b"old"
        )

    def test_no_overwrite_commit_failure_removes_entire_batch(self):
        plan = build_placement_plan(
            self.gateway,
            config=self._config(),
            inspection_number="260000001",
            sample_identity="正面",
            selected_images=self._images(),
        )
        target_dir = self.target_path / plan["target_relative_dir"]
        real_replace = os.replace
        staged_commits = 0

        def fail_second_staged_replace(source, target):
            nonlocal staged_commits
            if Path(source).name.endswith(".staged"):
                staged_commits += 1
                if staged_commits == 2:
                    raise OSError("simulated-second-commit-failure")
            return real_replace(source, target)

        with (
            patch(
                "app.execution.report_image_placement.os.replace",
                side_effect=fail_second_staged_replace,
            ),
            self.assertRaises(ExecutionApiError) as raised,
        ):
            execute_placement_plan(self.gateway, plan, overwrite=False)

        self.assertEqual(raised.exception.code, "report_image_write_failed")
        self.assertEqual(list(target_dir.iterdir()), [])

    def test_reservation_fsync_failure_removes_created_target(self):
        plan = build_placement_plan(
            self.gateway,
            config=self._config(),
            inspection_number="260000001",
            sample_identity="正面",
            selected_images=self._images(),
        )
        target_dir = self.target_path / plan["target_relative_dir"]
        real_fsync = os.fsync
        fsync_calls = 0

        def fail_first_reservation(descriptor):
            nonlocal fsync_calls
            fsync_calls += 1
            # Two source staging files are synced before target reservation.
            if fsync_calls == 3:
                raise OSError("simulated-reservation-fsync-failure")
            return real_fsync(descriptor)

        with (
            patch(
                "app.execution.report_image_placement.os.fsync",
                side_effect=fail_first_reservation,
            ),
            self.assertRaises(ExecutionApiError) as raised,
        ):
            execute_placement_plan(self.gateway, plan, overwrite=False)

        self.assertEqual(raised.exception.code, "report_image_write_failed")
        self.assertEqual(list(target_dir.iterdir()), [])

    def test_execute_with_overwrite_replaces_existing_files(self):
        target_dir = (
            self.target_path
            / REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY
            / "260000001"
        )
        target_dir.mkdir(parents=True)
        (target_dir / "260000001-正面-1.bmp").write_bytes(b"old")
        plan = build_placement_plan(
            self.gateway,
            config=self._config(),
            inspection_number="260000001",
            sample_identity="正面",
            selected_images=self._images(),
        )
        receipt = execute_placement_plan(self.gateway, plan, overwrite=True)
        self.assertEqual(receipt["overwritten_files"], ["260000001-正面-1.bmp"])
        self.assertEqual(
            (target_dir / "260000001-正面-1.bmp").read_bytes(), b"bbbbbb"
        )

    def test_overwrite_rejects_conflict_appearing_after_plan(self):
        plan = build_placement_plan(
            self.gateway,
            config=self._config(),
            inspection_number="260000001",
            sample_identity="正面",
            selected_images=self._images(),
        )
        target_dir = self.target_path / plan["target_relative_dir"]
        target_dir.mkdir(parents=True)
        raced = target_dir / "260000001-正面.bmp"
        raced.write_bytes(b"late-conflict")

        with self.assertRaises(ExecutionApiError) as raised:
            execute_placement_plan(self.gateway, plan, overwrite=True)

        self.assertEqual(
            raised.exception.code,
            "report_image_conflict_detected",
        )
        self.assertEqual(raced.read_bytes(), b"late-conflict")
        self.assertFalse(
            (target_dir / "260000001-正面-1.bmp").exists()
        )

    def test_execute_rejects_source_changed_after_selection(self):
        plan = build_placement_plan(
            self.gateway,
            config=self._config(),
            inspection_number="260000001",
            sample_identity="正面",
            selected_images=self._images(),
        )
        source = self.source_path / "260000001" / "正面" / "a.bmp"
        previous = source.stat()
        source.write_bytes(b"replacement")
        os.utime(
            source,
            ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000),
        )

        with self.assertRaises(ExecutionApiError) as raised:
            execute_placement_plan(self.gateway, plan, overwrite=False)

        self.assertEqual(raised.exception.code, "report_image_source_changed")
        target_dir = self.target_path / plan["target_relative_dir"]
        self.assertEqual(list(target_dir.iterdir()), [])

    def test_overwrite_failure_restores_every_original_target(self):
        target_dir = (
            self.target_path
            / REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY
            / "260000001"
        )
        target_dir.mkdir(parents=True)
        first = target_dir / "260000001-正面.bmp"
        second = target_dir / "260000001-正面-1.bmp"
        first.write_bytes(b"old-first")
        second.write_bytes(b"old-second")
        plan = build_placement_plan(
            self.gateway,
            config=self._config(),
            inspection_number="260000001",
            sample_identity="正面",
            selected_images=self._images(),
        )
        real_replace = os.replace
        staged_commits = 0

        def fail_second_staged_replace(source, target):
            nonlocal staged_commits
            if Path(source).name.endswith(".staged"):
                staged_commits += 1
                if staged_commits == 2:
                    raise OSError("simulated-second-commit-failure")
            return real_replace(source, target)

        with (
            patch(
                "app.execution.report_image_placement.os.replace",
                side_effect=fail_second_staged_replace,
            ),
            self.assertRaises(ExecutionApiError) as raised,
        ):
            execute_placement_plan(self.gateway, plan, overwrite=True)

        self.assertEqual(raised.exception.code, "report_image_write_failed")
        self.assertEqual(first.read_bytes(), b"old-first")
        self.assertEqual(second.read_bytes(), b"old-second")
        self.assertEqual(
            sorted(path.name for path in target_dir.iterdir()),
            sorted([first.name, second.name]),
        )

    def test_rollback_failure_preserves_backup_for_reconciliation(self):
        target_dir = (
            self.target_path
            / REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY
            / "260000001"
        )
        target_dir.mkdir(parents=True)
        first = target_dir / "260000001-正面.bmp"
        second = target_dir / "260000001-正面-1.bmp"
        first.write_bytes(b"old-first")
        second.write_bytes(b"old-second")
        plan = build_placement_plan(
            self.gateway,
            config=self._config(),
            inspection_number="260000001",
            sample_identity="正面",
            selected_images=self._images(),
        )
        real_replace = os.replace
        staged_commits = 0

        def fail_commit_and_first_rollback(source, target):
            nonlocal staged_commits
            source_path = Path(source)
            target_path = Path(target)
            if source_path.name.endswith(".staged"):
                staged_commits += 1
                if staged_commits == 2:
                    raise OSError("simulated-second-commit-failure")
            if source_path.name.endswith(".backup"):
                raise OSError("simulated-rollback-failure")
            return real_replace(source, target)

        with (
            patch(
                "app.execution.report_image_placement.os.replace",
                side_effect=fail_commit_and_first_rollback,
            ),
            self.assertRaises(ExecutionApiError) as raised,
        ):
            execute_placement_plan(self.gateway, plan, overwrite=True)

        self.assertEqual(
            raised.exception.code,
            "report_image_reconciliation_required",
        )
        recovery_backups = [
            Path(path)
            for path in raised.exception.details["recovery_backups"]
        ]
        self.assertEqual(len(recovery_backups), 1)
        self.assertTrue(recovery_backups[0].exists())
        self.assertEqual(recovery_backups[0].read_bytes(), b"old-first")
        self.assertEqual(first.read_bytes(), b"aaaa")
        self.assertEqual(second.read_bytes(), b"old-second")


class ReportImagePlacementEngineTests(unittest.TestCase):
    def setUp(self):
        register_persistence_executors()
        self.tempdir = tempfile.TemporaryDirectory()
        self.source_path = Path(self.tempdir.name) / "2026-电镜"
        self.source_path.mkdir(parents=True)
        self.target_path = Path(self.tempdir.name) / "report-images"
        self.target_path.mkdir(parents=True)
        self.engine = create_engine("sqlite:///:memory:")
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()
        ensure_default_rbac(self.db)
        ensure_default_catalog(self.db)
        self.user = ExecutionUser(
            username="placement-admin",
            display_name="放置测试",
            password_hash=hash_password("placement-password"),
            role="admin",
        )
        self.db.add(self.user)
        self.db.flush()
        bind_user_role(
            self.db,
            self.user,
            "admin",
            created_by_id=self.user.id,
        )
        self.db.add(
            ExecutionStorageRoot(
                root_id=ELECTRON_ROOT_ID,
                name="电镜原始资料",
                local_path=str(self.source_path),
                access_mode="read",
                category_key="electron_microscopy",
                is_active=True,
                is_available=True,
            )
        )
        self.db.add(
            ExecutionStorageRoot(
                root_id=REPORT_IMAGE_ROOT_ID,
                name="报告上传图片",
                local_path=str(self.target_path),
                access_mode="write",
                is_active=True,
                is_available=True,
            )
        )
        self.db.commit()
        image_dir = self.source_path / "260000001" / "正面"
        image_dir.mkdir(parents=True)
        (image_dir / "a.bmp").write_bytes(b"aaaa")
        (image_dir / "b.bmp").write_bytes(b"bbbbbb")

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.tempdir.cleanup()

    def _run(self, idempotency_key: str):
        workflow = (
            self.db.query(ExecutionWorkflow)
            .filter_by(slug="electron-microscopy-gbt36422")
            .one()
        )
        run, _ = create_run(
            self.db,
            workflow=workflow,
            actor=self.user,
            inspection_number="260000001",
            input_data={},
            global_data={},
            idempotency_key=idempotency_key,
        )
        self.db.commit()
        return run

    def _placement_context(self, run, *, sample_identity="正面"):
        node_run = (
            self.db.query(ExecutionNodeRun)
            .filter_by(run_id=run.id, node_id="place-report-images")
            .one()
        )
        selected_images = []
        for name in ("a.bmp", "b.bmp"):
            relative_path = f"260000001/正面/{name}"
            stat = (self.source_path / relative_path).stat()
            selected_images.append(
                _image(
                    name,
                    relative_path,
                    size=stat.st_size,
                    fingerprint=f"{stat.st_size}:{stat.st_mtime_ns}",
                )
            )
        input_data = {
            "inspection_number": "260000001",
            "sample_identity": sample_identity,
            "selected_images": selected_images,
        }
        node_run.input_data = input_data
        self.db.flush()
        workflow = (
            self.db.query(ExecutionWorkflow)
            .filter_by(slug="electron-microscopy-gbt36422")
            .one()
        )
        node = next(
            item
            for item in workflow.draft_definition["nodes"]
            if item["id"] == "place-report-images"
        )
        return NodeExecutionContext(
            db=self.db,
            run=run,
            node_run=node_run,
            node=node,
            input_data=input_data,
            worker_id="placement-worker",
            lease_token="",
        )

    def test_auto_complete_places_images_when_no_conflict(self):
        run = self._run("placement-auto")
        context = self._placement_context(run)
        output = auto_complete_report_image_placement(context)
        self.assertIsNotNone(output)
        self.assertEqual(output["auto_submit_reason"], "no_name_conflict")
        self.assertEqual(output["placed_count"], 2)
        self.assertFalse(output["placement_cancelled"])
        target_dir = (
            self.target_path
            / REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY
            / "260000001"
        )
        self.assertEqual(
            (target_dir / "260000001-正面.bmp").read_bytes(), b"aaaa"
        )
        self.assertEqual(
            (target_dir / "260000001-正面-1.bmp").read_bytes(), b"bbbbbb"
        )
        plan = context.node_run.input_data.get("placement_plan")
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(plan["image_count"], 2)

        # If file placement committed but the DB completion did not, the next
        # Worker attempt recognizes the exact same batch instead of opening a
        # false overwrite-confirmation task.
        retried = auto_complete_report_image_placement(context)
        self.assertIsNotNone(retried)
        self.assertTrue(retried["placement_reused"])
        self.assertEqual(
            retried["auto_submit_reason"],
            "matching_prior_attempt",
        )

    def test_worker_releases_database_transaction_before_cifs_copy(self):
        run = self._run("placement-connection-boundary")
        context = self._placement_context(run)
        observed_transactions = []

        def checked_execute(gateway, plan, *, overwrite):
            observed_transactions.append(self.db.in_transaction())
            return execute_placement_plan(
                gateway,
                plan,
                overwrite=overwrite,
            )

        with patch(
            "app.execution.report_image_placement.execute_placement_plan",
            side_effect=checked_execute,
        ):
            output = auto_complete_report_image_placement(context)

        self.assertIsNotNone(output)
        self.assertEqual(observed_transactions, [False])

    def test_conflict_pauses_then_overwrite_or_cancel(self):
        target_dir = (
            self.target_path
            / REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY
            / "260000001"
        )
        target_dir.mkdir(parents=True)
        (target_dir / "260000001-正面.bmp").write_bytes(b"old")

        run = self._run("placement-conflict")
        context = self._placement_context(run)
        # 有同名文件：不自动完成，计划随人工任务下发
        self.assertIsNone(auto_complete_report_image_placement(context))
        plan = context.node_run.input_data.get("placement_plan")
        self.assertEqual(plan["conflicts"], ["260000001-正面.bmp"])
        schema = report_image_placement_form_schema(context)
        self.assertEqual(
            schema["properties"]["placement_action"]["enum"],
            ["overwrite", "cancel"],
        )
        self.assertIn("260000001", schema["properties"]["placement_action"]["description"])

        with self.assertRaises(ExecutionApiError) as raised:
            _normalize_human_submission(
                self.db,
                run=run,
                node_run=context.node_run,
                data={},
            )
        self.assertEqual(
            raised.exception.code, "report_image_placement_decision_required"
        )

        cancelled = _normalize_human_submission(
            self.db,
            run=run,
            node_run=context.node_run,
            data={"placement_action": "cancel"},
        )
        self.assertTrue(cancelled["placement_cancelled"])
        self.assertEqual(cancelled["placed_count"], 0)
        self.assertEqual(
            (target_dir / "260000001-正面.bmp").read_bytes(), b"old"
        )

        overwrite_request = _normalize_human_submission(
            self.db,
            run=run,
            node_run=context.node_run,
            data={"placement_action": "overwrite"},
        )
        self.assertTrue(overwrite_request["placement_deferred"])
        self.assertEqual(overwrite_request["placement_action"], "overwrite")
        self.assertEqual(
            (target_dir / "260000001-正面.bmp").read_bytes(), b"old"
        )

        context.input_data = {
            **context.input_data,
            "placement_request": overwrite_request,
        }
        context.node_run.input_data = context.input_data
        overwritten = auto_complete_report_image_placement(context)
        self.assertIsNotNone(overwritten)
        self.assertEqual(overwritten["auto_submit_reason"], "confirmed_overwrite")
        self.assertEqual(overwritten["placed_count"], 2)
        self.assertEqual(
            overwritten["overwritten_files"], ["260000001-正面.bmp"]
        )
        self.assertEqual(
            (target_dir / "260000001-正面.bmp").read_bytes(), b"aaaa"
        )
        self.assertEqual(
            (target_dir / "260000001-正面-1.bmp").read_bytes(), b"bbbbbb"
        )

    def test_new_conflict_after_confirmation_requires_fresh_approval(self):
        target_dir = (
            self.target_path
            / REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY
            / "260000001"
        )
        target_dir.mkdir(parents=True)
        first = target_dir / "260000001-正面.bmp"
        second = target_dir / "260000001-正面-1.bmp"
        first.write_bytes(b"old-first")

        run = self._run("placement-new-conflict")
        context = self._placement_context(run)
        self.assertIsNone(auto_complete_report_image_placement(context))
        overwrite_request = _normalize_human_submission(
            self.db,
            run=run,
            node_run=context.node_run,
            data={"placement_action": "overwrite"},
        )
        self.assertEqual(
            overwrite_request["conflicts"],
            ["260000001-正面.bmp"],
        )

        # This file appeared after the user approved only the first conflict.
        second.write_bytes(b"late-conflict")
        context.input_data = {
            **context.input_data,
            "placement_request": overwrite_request,
        }
        context.node_run.input_data = context.input_data

        self.assertIsNone(auto_complete_report_image_placement(context))
        self.assertEqual(first.read_bytes(), b"old-first")
        self.assertEqual(second.read_bytes(), b"late-conflict")
        self.assertNotIn("placement_request", context.node_run.input_data)
        self.assertEqual(
            context.node_run.input_data["placement_plan"]["conflicts"],
            ["260000001-正面.bmp", "260000001-正面-1.bmp"],
        )

    def test_submit_records_overwrite_and_requeues_without_cifs_write(self):
        target_dir = (
            self.target_path
            / REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY
            / "260000001"
        )
        target_dir.mkdir(parents=True)
        target = target_dir / "260000001-正面.bmp"
        target.write_bytes(b"old")
        run = self._run("placement-submit-deferred")
        context = self._placement_context(run)
        self.assertIsNone(auto_complete_report_image_placement(context))
        context.node_run.status = "waiting_human"
        run.status = "waiting_human"
        task = ExecutionHumanTask(
            run_id=run.id,
            node_run_id=context.node_run.id,
            title="覆盖冲突图片",
            form_schema=report_image_placement_form_schema(context),
            status="open",
            revision=1,
            assigned_user_id=self.user.id,
        )
        self.db.add(task)
        self.db.commit()

        submitted = submit_human_task(
            self.db,
            task_id=task.id,
            expected_revision=1,
            data={"placement_action": "overwrite"},
            actor=self.user,
        )
        self.db.flush()

        self.assertEqual(submitted.status, "completed")
        self.assertEqual(context.node_run.status, "ready")
        self.assertEqual(
            context.node_run.input_data["placement_request"][
                "placement_action"
            ],
            "overwrite",
        )
        self.assertEqual(target.read_bytes(), b"old")


class ReportImagePlacementCatalogTests(unittest.TestCase):
    def setUp(self):
        register_persistence_executors()
        self.engine = create_engine("sqlite:///:memory:")
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()
        ensure_default_rbac(self.db)
        ensure_default_catalog(self.db)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_current_definitions_include_placement_node(self):
        for definition in (
            _electron_microscopy_gbt36422_definition(),
            _electron_cross_section_gbt36422_definition(),
        ):
            node_ids = {node["id"] for node in definition["nodes"]}
            self.assertIn("place-report-images", node_ids)
            slots = {
                (slot["root_id"], slot["access"])
                for slot in definition["root_slots"]
            }
            self.assertIn((REPORT_IMAGE_ROOT_ID, "write"), slots)
            edges = {
                (edge["source"], edge["target"])
                for edge in definition["edges"]
            }
            self.assertIn(("final-entry", "place-report-images"), edges)
            self.assertIn(("place-report-images", "end"), edges)
            result = validate_definition(definition)
            issues = getattr(result, "issues", result)
            self.assertEqual([str(i) for i in issues], [])

    def test_legacy_variant_omits_placement_node(self):
        for definition in (
            _electron_microscopy_gbt36422_definition(
                legacy_image_placement_contract=True
            ),
            _electron_cross_section_gbt36422_definition(
                legacy_image_placement_contract=True
            ),
        ):
            node_ids = {node["id"] for node in definition["nodes"]}
            self.assertNotIn("place-report-images", node_ids)
            self.assertNotIn(
                "image_placement",
                next(
                    node
                    for node in definition["nodes"]
                    if node["id"] == "end"
                )["input_mapping"],
            )
            result = validate_definition(definition)
            issues = getattr(result, "issues", result)
            self.assertEqual([str(i) for i in issues], [])

    def test_untouched_previous_microscopy_definition_is_upgraded(self):
        workflow = (
            self.db.query(ExecutionWorkflow)
            .filter_by(slug="electron-microscopy-gbt36422")
            .one()
        )
        legacy = _electron_microscopy_gbt36422_definition(
            legacy_image_placement_contract=True
        )
        legacy_checksum = definition_checksum(legacy)
        version_one = next(
            version
            for version in workflow.versions
            if version.version_number == 1
        )
        workflow.draft_definition = legacy
        version_one.definition = legacy
        version_one.checksum = legacy_checksum
        self.db.commit()

        ensure_default_catalog(self.db)
        self.db.commit()
        self.db.refresh(workflow)

        self.assertEqual(workflow.published_version_number, 2)
        self.assertEqual(workflow.draft_revision, 2)
        node_ids = {node["id"] for node in workflow.draft_definition["nodes"]}
        self.assertIn("place-report-images", node_ids)
        latest = self.db.query(ExecutionWorkflowVersion).filter_by(
            workflow_id=workflow.id,
            version_number=2,
        ).one()
        self.assertEqual(
            latest.checksum,
            definition_checksum(_electron_microscopy_gbt36422_definition()),
        )

    def test_untouched_previous_cross_section_definition_is_upgraded(self):
        workflow = (
            self.db.query(ExecutionWorkflow)
            .filter_by(slug="electron-cross-section-gbt36422")
            .one()
        )
        legacy = _electron_cross_section_gbt36422_definition(
            legacy_image_placement_contract=True
        )
        legacy_checksum = definition_checksum(legacy)
        version_one = next(
            version
            for version in workflow.versions
            if version.version_number == 1
        )
        workflow.draft_definition = legacy
        version_one.definition = legacy
        version_one.checksum = legacy_checksum
        self.db.commit()

        ensure_default_catalog(self.db)
        self.db.commit()
        self.db.refresh(workflow)

        self.assertEqual(workflow.published_version_number, 2)
        node_ids = {node["id"] for node in workflow.draft_definition["nodes"]}
        self.assertIn("place-report-images", node_ids)
        # 旧版本保持原样，历史运行不受影响
        self.assertNotIn("place-report-images", {
            node["id"] for node in version_one.definition["nodes"]
        })
