"""New installations and a deliberate reset must stay empty after restart."""
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from app.database import Base
from app.execution.catalog import ensure_default_catalog, ensure_default_rbac
from app.execution.models import ExecutionUser, ExecutionWorkflow, ExecutionStorageRoot, ExecutionCategory
from app.execution.project_rules import list_rules
from app.execution.v2.designer import starter_document, compile_document
from app.execution.v2.registry import get_installed_registry
from app.execution.worker import ExecutionWorker


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_bootstrap_never_recreates_workflows_or_rules(db):
    for _ in range(2):
        ensure_default_catalog(db)
        ensure_default_rbac(db)
        assert list_rules(db) == []
        db.commit()
    assert db.query(ExecutionWorkflow).count() == 0
    assert db.query(ExecutionCategory).count() > 0


def test_only_generic_native_nodes_are_installed_and_worker_ready():
    specs = get_installed_registry().list_node_specs()
    assert specs
    assert all(spec.source == "resource" for spec in specs)
    assert not any(spec.type.startswith(("paper_fiber.", "microscopy.", "inspection.record.", "regenerated_fiber.")) for spec in specs)
    assert ExecutionWorker("reset-test").capability_document["nodes"]
    result = compile_document(starter_document())
    assert result["content_valid"], result["issues"]


def test_reset_is_scoped_and_preview_does_not_delete(db):
    path = Path(__file__).parents[2] / "tools" / "reset_execution_workspace.py"
    spec = importlib.util.spec_from_file_location("reset_workspace", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ensure_default_catalog(db)
    user = ExecutionUser(username="kept", display_name="Kept", password_hash="test", role="admin")
    root = ExecutionStorageRoot(root_id="source", name="Source", local_path="/source", access_mode="read")
    db.add_all([user, root]); db.flush()
    category = db.query(ExecutionCategory).first()
    db.add(ExecutionWorkflow(slug="old", name="Old", category_id=category.id, draft_definition={}))
    db.commit()
    preview = module.reset(db.connection())
    assert preview["before"]["execution_workflows"] == 1
    assert db.query(ExecutionWorkflow).count() == 1
    report = module.reset(db.connection(), apply=True)
    db.commit()
    assert not any(report["after"].values())
    assert db.query(ExecutionUser).one().username == "kept"
    assert db.query(ExecutionStorageRoot).one().local_path == "/source"
    ensure_default_catalog(db)
    assert db.query(ExecutionWorkflow).count() == 0
