from __future__ import annotations

import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.execution.catalog import bind_user_role, ensure_default_catalog, ensure_default_rbac
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
from app.execution.report_image_placement import (
    REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY,
    REPORT_IMAGE_ROOT_ID,
    _reservation_marker,
    _sweep_stale_temporaries,
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

    def test_plan_ignores_own_reservation_remnant(self):
        # 崩溃遗留的本系统预留标记不算冲突，重试不会陷入幻影人工确认。
        target_dir = (
            self.target_path
            / REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY
            / "260000001"
        )
        target_dir.mkdir(parents=True)
        remnant = target_dir / "260000001-正面-1.bmp"
        remnant.write_bytes(_reservation_marker())
        plan = build_placement_plan(
            self.gateway,
            config=self._config(),
            inspection_number="260000001",
            sample_identity="正面",
            selected_images=self._images(),
        )
        self.assertEqual(plan["conflicts"], [])

    def test_plan_still_flags_foreign_placeholder_files(self):
        # 非本系统标记的占位文件（包括零字节）必须仍然算冲突。
        target_dir = (
            self.target_path
            / REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY
            / "260000001"
        )
        target_dir.mkdir(parents=True)
        (target_dir / "260000001-正面.bmp").write_bytes(b"")
        almost_marker = (
            b"textile-report-image-placement-reservation:" + b"0" * 31
        )
        (target_dir / "260000001-正面-1.bmp").write_bytes(almost_marker)
        plan = build_placement_plan(
            self.gateway,
            config=self._config(),
            inspection_number="260000001",
            sample_identity="正面",
            selected_images=self._images(),
        )
        self.assertEqual(
            plan["conflicts"],
            ["260000001-正面.bmp", "260000001-正面-1.bmp"],
        )

    def test_execute_takes_over_reservation_remnant(self):
        target_dir = (
            self.target_path
            / REPORT_IMAGE_DEFAULT_TARGET_DIRECTORY
            / "260000001"
        )
        target_dir.mkdir(parents=True)
        remnant = target_dir / "260000001-正面-1.bmp"
        remnant.write_bytes(_reservation_marker())
        plan = build_placement_plan(
            self.gateway,
            config=self._config(),
            inspection_number="260000001",
            sample_identity="正面",
            selected_images=self._images(),
        )
        receipt = execute_placement_plan(self.gateway, plan, overwrite=False)
        self.assertEqual(receipt["placed_count"], 2)
        self.assertEqual(remnant.read_bytes(), b"bbbbbb")
        self.assertEqual(
            (target_dir / "260000001-正面.bmp").read_bytes(), b"aaaa"
        )

    def test_sweep_removes_only_stale_system_temporaries(self):
        target_dir = self.target_path / "sweep"
        target_dir.mkdir()
        stale_staged = target_dir / (".a.bmp." + "a" * 32 + ".staged")
        stale_backup = target_dir / (".a.bmp." + "b" * 32 + ".backup")
        stale_remnant = target_dir / "260000001-正面.bmp"
        fresh_staged = target_dir / (".b.bmp." + "c" * 32 + ".staged")
        user_file = target_dir / "notes.txt"
        for path in (stale_staged, stale_backup, fresh_staged):
            path.write_bytes(b"x")
        stale_remnant.write_bytes(_reservation_marker())
        user_file.write_bytes(b"user")
        old = time.time() - 7200
        for path in (stale_staged, stale_backup, stale_remnant, user_file):
            os.utime(path, (old, old))

        _sweep_stale_temporaries(target_dir)

        remaining = {path.name for path in target_dir.iterdir()}
        self.assertEqual(remaining, {fresh_staged.name, user_file.name})
