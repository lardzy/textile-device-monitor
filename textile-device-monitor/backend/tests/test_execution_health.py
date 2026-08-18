from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from app.database import SessionLocal
from app.execution.models import ExecutionWorkerHeartbeat, utcnow
from app.execution.worker_state import worker_heartbeat_is_fresh
from app.main import _readiness_payload


def test_worker_heartbeat_freshness_expires() -> None:
    with SessionLocal() as db:
        db.query(ExecutionWorkerHeartbeat).delete()
        heartbeat = ExecutionWorkerHeartbeat(
            worker_id="health-test-worker",
            status="running",
            started_at=utcnow(),
            last_seen_at=utcnow() - timedelta(seconds=90),
        )
        db.add(heartbeat)
        db.commit()

        fresh, found = worker_heartbeat_is_fresh(
            db,
            worker_id=heartbeat.worker_id,
            timeout_seconds=45,
        )
        assert not fresh
        assert found is not None

        heartbeat.last_seen_at = utcnow()
        db.commit()
        fresh, _ = worker_heartbeat_is_fresh(
            db,
            worker_id=heartbeat.worker_id,
            timeout_seconds=45,
        )
        assert fresh

        heartbeat.status = "stopped"
        db.commit()
        fresh, _ = worker_heartbeat_is_fresh(
            db,
            worker_id=heartbeat.worker_id,
            timeout_seconds=45,
        )
        assert not fresh


def test_readiness_requires_worker_and_writable_storage(tmp_path) -> None:
    staging = tmp_path / "staging"
    publish = tmp_path / "publish"
    report_images = tmp_path / "report-images"
    staging.mkdir()
    publish.mkdir()
    report_images.mkdir()
    fresh_heartbeat = ExecutionWorkerHeartbeat(
        worker_id="ready-worker",
        status="running",
        started_at=utcnow(),
        last_seen_at=utcnow(),
    )

    with (
        patch("app.main.database_schema_is_current", return_value=(True, "head", "head")),
        patch(
            "app.main.worker_heartbeat_is_fresh",
            return_value=(True, fresh_heartbeat),
        ),
        patch("app.main.settings.EXECUTION_ENABLED", True),
        patch(
            "app.main.settings.EXECUTION_RUNTIME_ROOT",
            str(staging),
        ),
        patch(
            "app.main.settings.EXECUTION_PUBLISH_ROOT",
            str(publish),
        ),
        patch(
            "app.main.settings.EXECUTION_REPORT_IMAGE_ROOT",
            str(report_images),
        ),
    ):
        payload, status_code = _readiness_payload()
    assert status_code == 200
    assert payload["status"] == "ready"
    assert payload["components"]["execution_storage"]["ready"]

    with (
        patch("app.main.database_schema_is_current", return_value=(True, "head", "head")),
        patch(
            "app.main.worker_heartbeat_is_fresh",
            return_value=(False, None),
        ),
        patch("app.main.settings.EXECUTION_ENABLED", True),
        patch(
            "app.main.settings.EXECUTION_RUNTIME_ROOT",
            str(staging),
        ),
        patch(
            "app.main.settings.EXECUTION_PUBLISH_ROOT",
            str(publish),
        ),
        patch(
            "app.main.settings.EXECUTION_REPORT_IMAGE_ROOT",
            str(report_images),
        ),
    ):
        payload, status_code = _readiness_payload()
    assert status_code == 503
    assert payload["components"]["execution_worker"]["ready"] is False


def test_readiness_fails_when_publish_root_is_missing(tmp_path) -> None:
    staging = tmp_path / "staging"
    report_images = tmp_path / "report-images"
    staging.mkdir()
    report_images.mkdir()
    with (
        patch("app.main.database_schema_is_current", return_value=(True, "head", "head")),
        patch(
            "app.main.worker_heartbeat_is_fresh",
            return_value=(True, None),
        ),
        patch("app.main.settings.EXECUTION_ENABLED", True),
        patch(
            "app.main.settings.EXECUTION_RUNTIME_ROOT",
            str(staging),
        ),
        patch(
            "app.main.settings.EXECUTION_PUBLISH_ROOT",
            str(tmp_path / "missing"),
        ),
        patch(
            "app.main.settings.EXECUTION_REPORT_IMAGE_ROOT",
            str(report_images),
        ),
    ):
        payload, status_code = _readiness_payload()
    assert status_code == 503
    assert not payload["components"]["execution_storage"]["publish_writable"]


def test_readiness_fails_when_report_image_root_is_missing(tmp_path) -> None:
    staging = tmp_path / "staging"
    publish = tmp_path / "publish"
    staging.mkdir()
    publish.mkdir()
    with (
        patch("app.main.database_schema_is_current", return_value=(True, "head", "head")),
        patch(
            "app.main.worker_heartbeat_is_fresh",
            return_value=(True, None),
        ),
        patch("app.main.settings.EXECUTION_ENABLED", True),
        patch("app.main.settings.EXECUTION_RUNTIME_ROOT", str(staging)),
        patch("app.main.settings.EXECUTION_PUBLISH_ROOT", str(publish)),
        patch(
            "app.main.settings.EXECUTION_REPORT_IMAGE_ROOT",
            str(tmp_path / "missing-report-images"),
        ),
    ):
        payload, status_code = _readiness_payload()
    assert status_code == 503
    assert not payload["components"]["execution_storage"][
        "report_images_writable"
    ]
