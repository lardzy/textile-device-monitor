#!/usr/bin/env python3
"""Read-only reference inventory for the configured execution database."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.database import SessionLocal
from app.execution.compatibility import compatibility_audit

if __name__ == "__main__":
    with SessionLocal() as db:
        print(json.dumps(compatibility_audit(db), ensure_ascii=False, indent=2))
