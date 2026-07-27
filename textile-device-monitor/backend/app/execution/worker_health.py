from __future__ import annotations

import sys

from app.database import SessionLocal
from app.execution.worker_state import (
    default_worker_id,
    worker_heartbeat_is_fresh,
)


def main() -> None:
    with SessionLocal() as db:
        fresh, heartbeat = worker_heartbeat_is_fresh(
            db,
            worker_id=default_worker_id(),
        )
    if not fresh:
        worker_id = heartbeat.worker_id if heartbeat is not None else "missing"
        raise SystemExit(f"execution worker heartbeat stale: {worker_id}")
    print("execution worker healthy")


if __name__ == "__main__":
    sys.exit(main())
