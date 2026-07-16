"""PostgreSQL-only queue concurrency gates.

Run explicitly with a disposable database whose name ends in ``_test``::

    TEST_DATABASE_URL=postgresql://.../queue_concurrency_test pytest -q \
        tests/test_queue_concurrency_postgres.py
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

import pytest


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
database_name = urlparse(TEST_DATABASE_URL).path.rsplit("/", 1)[-1]
if not TEST_DATABASE_URL.startswith("postgresql") or not database_name.endswith("_test"):
    pytest.skip(
        "requires an explicit disposable PostgreSQL TEST_DATABASE_URL ending in _test",
        allow_module_level=True,
    )

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("OCR_ENABLED", "false")
os.environ.setdefault("AREA_ENABLED", "false")

from fastapi.responses import JSONResponse

from app.api.devices import report_device_status
from app.api.queue import change_queue_position, join_queue, leave_queue
from app.crud import devices as device_crud
from app.crud import queue as queue_crud
from app.database import Base, SessionLocal, engine, ensure_queue_record_schema
from app.models import Device, DeviceStatus, QueueChangeLog
from app.schemas import PositionChange, QueueCreate, StatusReport

if engine.dialect.name != "postgresql":
    pytest.skip(
        "app database engine was already initialized with a non-PostgreSQL URL; "
        "run this file in a separate pytest process",
        allow_module_level=True,
    )


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    ensure_queue_record_schema()
    yield
    Base.metadata.drop_all(bind=engine)


def _create_device(code: str) -> int:
    db = SessionLocal()
    try:
        device = Device(
            device_code=code,
            name=code,
            status=DeviceStatus.IDLE,
            metrics={"device_type": "standard"},
        )
        db.add(device)
        db.commit()
        return int(device.id)
    finally:
        db.close()


def _join(device_id: int, user_id: str, copies: int = 1):
    db = SessionLocal()
    try:
        return asyncio.run(
            join_queue(
                QueueCreate(
                    inspector_name=user_id,
                    device_id=device_id,
                    copies=copies,
                    created_by_id=user_id,
                ),
                db=db,
            )
        )
    finally:
        db.close()


def _waiting(device_id: int):
    db = SessionLocal()
    try:
        return queue_crud.serialize_queue(db, device_id)
    finally:
        db.close()


def test_parallel_joins_on_one_device_have_contiguous_unique_positions():
    device_id = _create_device("parallel-one-device")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda number: _join(device_id, f"browser-{number}"),
                range(8),
            )
        )

    assert sum(len(result) for result in results) == 8
    queue = _waiting(device_id)
    assert [record["position"] for record in queue] == list(range(1, 9))
    assert len({record["position"] for record in queue}) == 8


def test_parallel_cross_device_joins_respect_browser_quota():
    first_device_id = _create_device("quota-device-1")
    second_device_id = _create_device("quota-device-2")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_join, first_device_id, "same-browser", 2),
            pool.submit(_join, second_device_id, "same-browser", 2),
        ]
        results = [future.result() for future in futures]

    assert sum(len(result) for result in results) == 3
    assert len(_waiting(first_device_id)) + len(_waiting(second_device_id)) == 3


def test_complete_reorder_delete_race_preserves_queue_invariant():
    device_id = _create_device("mixed-mutations")
    for number in range(6):
        _join(device_id, f"browser-{number}")

    initial = _waiting(device_id)
    reorder_record = initial[-1]
    delete_record = initial[2]

    def complete():
        db = SessionLocal()
        try:
            record = queue_crud.complete_first_in_queue(db, device_id)
            db.commit()
            return record
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def reorder():
        db = SessionLocal()
        try:
            return asyncio.run(
                change_queue_position(
                    reorder_record["id"],
                    PositionChange(
                        new_position=1,
                        changed_by="operator",
                        changed_by_id="operator",
                        version=reorder_record["version"],
                        target_queue_id=initial[0]["id"],
                        target_version=initial[0]["version"],
                    ),
                    db=db,
                )
            )
        finally:
            db.close()

    def delete():
        db = SessionLocal()
        try:
            return asyncio.run(
                leave_queue(delete_record["id"], changed_by_id="operator", db=db)
            )
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(complete),
            pool.submit(reorder),
            pool.submit(delete),
        ]
        results = [future.result() for future in futures]

    # A stale reorder/delete is allowed to return the uniform 409 response; it
    # must never corrupt positions or complete more than the requested record.
    for result in results:
        if isinstance(result, JSONResponse):
            assert result.status_code == 409

    queue = _waiting(device_id)
    positions = [record["position"] for record in queue]
    assert positions == list(range(1, len(queue) + 1))
    assert len(positions) == len(set(positions))
    assert len(queue) in {4, 5}


def test_status_completion_and_device_delete_do_not_deadlock():
    device_id = _create_device("delete-vs-status")
    _join(device_id, "browser-1")

    setup_db = SessionLocal()
    try:
        asyncio.run(
            report_device_status(
                "delete-vs-status",
                StatusReport(
                    status="busy",
                    task_id="delete-race-task",
                    task_key="delete-race-task",
                    task_name="delete race",
                    task_progress=50,
                    report_id="33333333-3333-4333-8333-333333333333",
                    reported_at=datetime.now(timezone.utc),
                ),
                db=setup_db,
            )
        )
    finally:
        setup_db.close()

    def finish_status():
        db = SessionLocal()
        try:
            return asyncio.run(
                report_device_status(
                    "delete-vs-status",
                    StatusReport(
                        status="idle",
                        task_id="delete-race-task",
                        task_key="delete-race-task",
                        task_name="delete race",
                        task_progress=100,
                        report_id="44444444-4444-4444-8444-444444444444",
                        reported_at=datetime.now(timezone.utc),
                    ),
                    db=db,
                )
            )
        finally:
            db.close()

    def delete():
        db = SessionLocal()
        try:
            return device_crud.delete_device(db, device_id)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        status_future = pool.submit(finish_status)
        delete_future = pool.submit(delete)
        status_error = None
        try:
            status_future.result(timeout=10)
        except Exception as exc:  # delete may deterministically win with 404
            status_error = exc
        assert delete_future.result(timeout=10) is True

    if status_error is not None:
        assert getattr(status_error, "status_code", None) == 404


def test_complete_and_leave_race_has_one_terminal_log():
    device_id = _create_device("complete-vs-leave")
    record = _join(device_id, "browser-1")[0]

    def complete():
        db = SessionLocal()
        try:
            result = queue_crud.complete_first_in_queue(db, device_id)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def leave():
        db = SessionLocal()
        try:
            return asyncio.run(
                leave_queue(record["id"], changed_by_id="browser-1", db=db)
            )
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(complete), pool.submit(leave)]
        results = [future.result(timeout=10) for future in futures]

    for result in results:
        if isinstance(result, JSONResponse):
            assert result.status_code == 409

    db = SessionLocal()
    try:
        logs = (
            db.query(QueueChangeLog)
            .filter(QueueChangeLog.queue_id == record["id"])
            .all()
        )
        terminal_logs = [log for log in logs if log.change_type in {"complete", "leave"}]
        assert len(terminal_logs) == 1
        assert _waiting(device_id) == []
    finally:
        db.close()
