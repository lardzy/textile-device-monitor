from __future__ import annotations

import logging
import signal
import threading
import time

from app.config import settings
from app.database import SessionLocal
from app.execution.engine import (
    claim_next_node,
    execute_claimed_node,
    renew_node_lease,
)
from app.execution import models as _execution_models  # noqa: F401
from app.execution.persistence import (
    claim_index_job,
    enqueue_due_index_jobs,
    process_index_job,
    register_persistence_executors,
    renew_index_job_lease,
)
from app.execution.mutation_runtime import register_mutation_executors
from app.execution.outbox import (
    claim_outbox_record,
    cleanup_outbox,
    dispatch_outbox_record,
    fail_outbox_record,
)
from app.execution.excel_runtime import register_excel_executors
from app.execution.worker_state import (
    default_worker_id,
    record_worker_heartbeat,
    wait_for_current_database_schema,
)


logger = logging.getLogger(__name__)


class _LeaseHeartbeat:
    def __init__(
        self,
        *,
        work_kind: str,
        work_id: str,
        lease_token: str,
        lease_seconds: int,
    ) -> None:
        self.work_kind = work_kind
        self.work_id = work_id
        self.lease_token = lease_token
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_args):
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        interval = max(min(self.lease_seconds / 3, 20), 1)
        while not self._stop.wait(interval):
            with SessionLocal() as db:
                try:
                    if self.work_kind == "node":
                        renewed = renew_node_lease(
                            db,
                            node_run_id=self.work_id,
                            lease_token=self.lease_token,
                            lease_seconds=self.lease_seconds,
                        )
                    else:
                        renewed = renew_index_job_lease(
                            db,
                            job_id=self.work_id,
                            lease_token=self.lease_token,
                            lease_seconds=max(self.lease_seconds, 300),
                        )
                    if renewed:
                        db.commit()
                    else:
                        db.rollback()
                        return
                except Exception:
                    db.rollback()
                    logger.exception("Failed to renew execution work lease")


class _WorkerHeartbeat:
    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.interval = max(
            min(
                int(settings.EXECUTION_WORKER_HEARTBEAT_TIMEOUT_SECONDS) / 3,
                10,
            ),
            1,
        )

    def _record(self, status: str) -> None:
        with SessionLocal() as db:
            record_worker_heartbeat(
                db,
                worker_id=self.worker_id,
                status=status,
            )
            db.commit()

    def __enter__(self):
        self._record("running")
        self._thread.start()
        return self

    def __exit__(self, *_args):
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self._record("stopped")
        except Exception:
            logger.exception("Failed to record execution worker shutdown")

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._record("running")
            except Exception:
                logger.exception("Failed to record execution worker heartbeat")


class ExecutionWorker:
    def __init__(self, worker_id: str | None = None) -> None:
        register_persistence_executors()
        register_mutation_executors()
        register_excel_executors()
        self.worker_id = worker_id or default_worker_id()
        self._stopping = False
        self._next_outbox_cleanup_at = 0.0

    def stop(self, *_args) -> None:
        self._stopping = True

    def run_once(self) -> bool:
        outbox_record_id: int | None = None
        with SessionLocal() as db:
            try:
                enqueue_due_index_jobs(db)
                if time.monotonic() >= self._next_outbox_cleanup_at:
                    cleanup_outbox(db)
                    self._next_outbox_cleanup_at = time.monotonic() + 3600
                outbox = claim_outbox_record(
                    db,
                    worker_id=self.worker_id,
                    lease_seconds=int(
                        getattr(settings, "EXECUTION_WORKER_LEASE_SECONDS", 60)
                    ),
                )
                if outbox is not None:
                    outbox_record_id = outbox.id
                else:
                    node = claim_next_node(db, worker_id=self.worker_id)
                    if node is None:
                        index_job = claim_index_job(db, worker_id=self.worker_id)
                        if index_job is None:
                            db.commit()
                            return False
                        work_kind = "index"
                        work_id = index_job.id
                        lease_token = index_job.lease_token
                    else:
                        work_kind = "node"
                        work_id = node.id
                        lease_token = node.lease_token
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Failed to claim execution work")
                return False

        if outbox_record_id is not None:
            try:
                with SessionLocal() as db:
                    dispatch_outbox_record(
                        db,
                        record_id=outbox_record_id,
                        worker_id=self.worker_id,
                    )
                    db.commit()
            except Exception as exc:
                logger.exception("Failed to dispatch execution outbox record")
                with SessionLocal() as failure_db:
                    try:
                        fail_outbox_record(
                            failure_db,
                            record_id=outbox_record_id,
                            worker_id=self.worker_id,
                            error=exc,
                        )
                        failure_db.commit()
                    except Exception:
                        failure_db.rollback()
                        logger.exception(
                            "Failed to record execution outbox failure"
                        )
            return True

        with SessionLocal() as db:
            try:
                lease_seconds = int(
                    getattr(settings, "EXECUTION_WORKER_LEASE_SECONDS", 60)
                )
                with _LeaseHeartbeat(
                    work_kind=work_kind,
                    work_id=work_id,
                    lease_token=lease_token,
                    lease_seconds=lease_seconds,
                ):
                    if work_kind == "node":
                        execute_claimed_node(db, work_id, lease_token)
                    else:
                        process_index_job(db, work_id, lease_token)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Execution work failed before durable completion")
        return True

    def run_forever(self) -> None:
        poll_seconds = float(
            getattr(settings, "EXECUTION_WORKER_POLL_SECONDS", 1.0)
        )
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        wait_for_current_database_schema()
        logger.info("Execution worker %s started", self.worker_id)
        with _WorkerHeartbeat(self.worker_id):
            while not self._stopping:
                if not self.run_once():
                    time.sleep(max(poll_seconds, 0.05))
        logger.info("Execution worker %s stopped", self.worker_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings.validate_execution_security()
    register_persistence_executors()
    register_mutation_executors()
    register_excel_executors()
    ExecutionWorker().run_forever()


if __name__ == "__main__":
    main()
