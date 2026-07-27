from __future__ import annotations

import unittest
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.execution.events import append_run_event
from app.execution.models import ExecutionOutbox, utcnow
from app.execution.outbox import (
    claim_outbox_record,
    cleanup_outbox,
    dispatch_outbox_record,
    fail_outbox_record,
)


class ExecutionOutboxTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _event(self, suffix: str = "1") -> ExecutionOutbox:
        append_run_event(
            self.db,
            run_id=f"run-{suffix}",
            event_type="test.created",
            payload={"suffix": suffix},
        )
        self.db.commit()
        return self.db.query(ExecutionOutbox).filter_by(
            aggregate_id=f"run-{suffix}"
        ).one()

    def test_record_is_leased_once_and_dispatched(self):
        record = self._event()
        claimed = claim_outbox_record(self.db, worker_id="worker-a")
        self.assertEqual(claimed.id, record.id)
        self.db.commit()

        other = self.Session()
        try:
            self.assertIsNone(
                claim_outbox_record(other, worker_id="worker-b")
            )
            other.rollback()
        finally:
            other.close()

        self.assertTrue(
            dispatch_outbox_record(
                self.db,
                record_id=record.id,
                worker_id="worker-a",
            )
        )
        self.db.commit()
        self.db.refresh(record)
        self.assertIsNotNone(record.dispatched_at)
        self.assertIsNone(record.lease_owner)

    def test_repeated_failure_moves_record_to_dead_letter(self):
        record = self._event("failure")
        for attempt in range(2):
            claimed = claim_outbox_record(
                self.db,
                worker_id="worker-a",
                lease_seconds=10,
            )
            self.assertIsNotNone(claimed)
            self.db.commit()
            self.assertTrue(
                fail_outbox_record(
                    self.db,
                    record_id=record.id,
                    worker_id="worker-a",
                    error=RuntimeError("sink unavailable"),
                    max_attempts=2,
                )
            )
            if attempt == 0:
                record.available_at = utcnow() - timedelta(seconds=1)
            self.db.commit()
        self.db.refresh(record)
        self.assertEqual(record.attempts, 2)
        self.assertIsNotNone(record.dead_lettered_at)
        self.assertIn("sink unavailable", record.last_error)

    def test_cleanup_only_removes_terminal_old_records(self):
        old_dispatched = self._event("old")
        old_dispatched.dispatched_at = utcnow() - timedelta(days=10)
        old_dispatched.created_at = utcnow() - timedelta(days=10)
        pending = self._event("pending")
        pending.created_at = utcnow() - timedelta(days=10)
        old_dispatched_id = old_dispatched.id
        pending_id = pending.id
        self.db.commit()

        self.assertEqual(cleanup_outbox(self.db, retention_days=7), 1)
        self.db.commit()
        self.assertIsNone(self.db.get(ExecutionOutbox, old_dispatched_id))
        self.assertIsNotNone(self.db.get(ExecutionOutbox, pending_id))


if __name__ == "__main__":
    unittest.main()
