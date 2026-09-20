#!/usr/bin/env python3
"""Refresh/check installed JSON templates using an isolated, empty in-memory DB."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.database import Base
from app.execution.catalog import ensure_default_catalog
from app.execution.models import ExecutionWorkflow
from app.execution.project_rules import ensure_default_project_rules
from app.execution.release_v2 import preview_v1_migration
from app.execution.v2.designer import compile_document
from app.execution.v2.workflow_templates import TEMPLATE_ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    stale = []
    try:
        with Session(engine) as db:
            ensure_default_catalog(db)
            ensure_default_project_rules(db)
            db.flush()
            for workflow in db.query(ExecutionWorkflow).order_by(ExecutionWorkflow.slug).all():
                preview = preview_v1_migration(db, workflow_id=workflow.id, source="published", actor=None,
                                              target_profile="native_p4", target_slug=workflow.slug + "-v2")
                candidate = preview["candidate"]
                candidate.pop("migration", None)
                candidate.pop("migration_provenance", None)
                candidate["release"]["release_version"] = 1
                candidate["release"]["release_note"] = "独立 JSON 工作流模板"
                compiled = compile_document(candidate)
                if not compiled["content_valid"]:
                    raise ValueError(compiled["issues"])
                path = TEMPLATE_ROOT / f"{workflow.slug}.json"
                content = json.dumps(compiled["document"], ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                if not path.is_file() or path.read_text(encoding="utf-8") != content:
                    stale.append(path.name)
                    if args.update:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(content, encoding="utf-8")
    finally:
        engine.dispose()
    if stale and not args.update:
        print("Stale workflow templates: " + ", ".join(stale), file=sys.stderr)
        return 1
    print(f"Workflow templates {'updated' if stale else 'checked'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
