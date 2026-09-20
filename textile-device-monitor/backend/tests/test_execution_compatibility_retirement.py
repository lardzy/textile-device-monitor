from app.execution.compatibility import active_worker_capabilities, compatibility_audit
from app.execution.models import ExecutionWorkflow, ExecutionRun, utcnow
from tests.test_execution_workflow_replacement import environment, request, run, source_workflow


def test_retirement_tracks_active_runs_and_restores_capabilities_on_revert(environment):
    env = environment
    source = source_workflow(env, "hemp-cotton-source-selection")
    pending = run(env, source.id)
    before = compatibility_audit(env.db)
    assert before["required_count"] > 0
    for workflow in env.db.query(ExecutionWorkflow).all():
        workflow.archived_at = utcnow()
        workflow.is_enabled = False
    env.db.commit()
    audit = request(env, "GET", "v2/compatibility/audit")
    required = [item for item in audit["items"] if item["state"] == "required"]
    assert required
    assert all(ref["kind"] == "run" for item in required for ref in item["references"])
    active = env.db.get(ExecutionRun, pending["id"])
    active.status = "failed"
    env.db.commit()
    assert compatibility_audit(env.db)["required_count"] > 0  # A failed Run can still retry.
    active.status = "completed"
    env.db.commit()
    audit = compatibility_audit(env.db)
    assert audit["required_count"] == 0
    assert audit["retired_count"] == 39
    assert sum(item["historical_reference_count"] for item in audit["items"]) > 0
    reduced = active_worker_capabilities(env.db, env.worker.capability_document)
    assert len(reduced["nodes"]) == len(env.worker.capability_document["nodes"]) - 39
    assert all(node["type_version"] == 2 for node in reduced["nodes"] if node["type"].startswith("core."))
    source.archived_at = None
    source.is_enabled = True
    env.db.commit()
    restored = active_worker_capabilities(env.db, env.worker.capability_document)
    assert len(restored["nodes"]) > len(reduced["nodes"])
    assert any(node["type"] == "core.start" and node["type_version"] == 1 for node in restored["nodes"])
    env.db.refresh(active)
    assert active.status == "completed"
