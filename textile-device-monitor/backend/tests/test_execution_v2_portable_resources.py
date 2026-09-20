from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.execution.models import ExecutionProjectRule, ExecutionStorageRoot
from app.execution.errors import ExecutionApiError
from app.execution.v2.portable_rules import export_rule, bind_rule, bound_rule, definition_issues
from app.execution.v2.templates import install_templates, list_templates, resolve_template
from tests.test_execution_workflow_replacement import environment


def test_rule_is_portable_and_runtime_ignores_later_database_edits(environment):
    env = environment
    row = env.db.query(ExecutionProjectRule).filter_by(rule_key="paper_gbt4688_qualitative").one()
    definition = export_rule(row, "documents")
    assert "root_id" not in definition["config"]["source"]
    before = deepcopy(definition)
    first = bind_rule(definition, {"documents": {"root_id": "first_machine"}})
    second = bind_rule(definition, {"documents": {"root_id": "second_machine"}})
    row.revision += 1
    row.enabled = False
    env.db.flush()
    def resolve(binding):
        return bound_rule(SimpleNamespace(db=env.db, node={"config": {"match_rule": row.rule_key}},
            run=SimpleNamespace(deployment_binding_snapshot={"bindings": {"rule_slots": {"match": binding}}})))
    assert resolve(first).source_root_id == "first_machine"
    assert resolve(second).source_root_id == "second_machine"
    assert resolve(first).revision == row.revision - 1
    assert resolve(first).probes == resolve(second).probes
    assert definition == before
    with pytest.raises(ExecutionApiError, match="规则数据目录"):
        bind_rule(definition, {})


def test_invalid_rule_fails_before_import(environment):
    row = environment.db.query(ExecutionProjectRule).first()
    definition = export_rule(row, "missing")
    issues = definition_issues({"resources": {"root_slots": [], "rule_slots": [{"slot_id": "rule", "definition": definition}]}})
    assert issues[0]["code"] == "rule_definition_invalid"


def test_installed_and_shared_templates_are_selectable_and_never_overwritten(environment):
    env = environment
    installed = install_templates()
    original = next(installed.glob("*.xls"))
    original.write_bytes(b"local change")
    install_templates()
    assert original.read_bytes() == b"local change"
    env.db.add(ExecutionStorageRoot(root_id="shared_templates", name="共享模板", local_path=str(installed),
                                    access_mode="read", is_active=True, is_available=True))
    env.db.flush()
    entries = list_templates(env.db, "shared_templates")
    chosen = next(item for item in entries if item["name"] == original.name)
    assert resolve_template(env.db, chosen) == original
    original.write_bytes(b"changed after publishing")
    with pytest.raises(ExecutionApiError):
        resolve_template(env.db, chosen)
