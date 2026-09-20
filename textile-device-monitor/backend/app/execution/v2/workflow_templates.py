"""Portable workflow templates, available without legacy Workflow rows."""

import json
from pathlib import Path

from sqlalchemy import func

from app.execution.models import ExecutionWorkflow, ExecutionWorkflowRelease
from app.execution.v2.designer import seal


TEMPLATE_ROOT = Path(__file__).with_name("resources") / "workflows"


def list_workflow_templates(db):
    occupied = {row.slug: row for row in db.query(ExecutionWorkflow).all()}
    templates = []
    for path in sorted(TEMPLATE_ROOT.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        base_slug = document["release"]["slug"]
        slug, suffix = base_slug, 2
        while slug in occupied:
            slug, suffix = f"{base_slug}-{suffix}", suffix + 1
        document["release"]["slug"] = slug
        latest = db.query(func.max(ExecutionWorkflowRelease.source_version)).filter_by(source_slug=slug).scalar()
        document["release"]["release_version"] = (latest or 0) + 1
        seal(document)
        source = occupied.get(path.stem)
        templates.append({"name": document["release"]["name"], "candidate": document,
                          "template_id": path.stem, "workflow_id": source.id if source else None,
                          "replacement_source": None, "content_valid": True})
    return templates
