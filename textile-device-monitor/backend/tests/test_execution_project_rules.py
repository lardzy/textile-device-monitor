from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.execution.errors import ExecutionApiError
from app.execution.microscopy_families import microscopy_family_for_project
from app.execution.models import ExecutionProjectRule, ExecutionStorageRoot, utcnow
from app.execution.project_rules import (
    PAPER_FIBER_RULE_KEY,
    evaluate_task_facts,
    ensure_default_project_rules,
    list_rules,
    microscopy_rule_key,
    parse_task_fact,
    require_enabled_rule,
    resolve_rule,
    rule_for_facts,
    validate_rule_config,
)
from app.execution.project_rule_seeds import default_project_rule_seeds


class ProjectRuleTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()
        self.root = ExecutionStorageRoot(
            root_id="paper_fiber_records",
            name="纸类原始记录",
            local_path="/tmp/paper-root",
            access_mode="read",
            category_key="other",
            is_active=True,
            is_available=True,
            last_scan_finished_at=utcnow(),
        )
        self.db.add(self.root)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_seeds_insert_five_rules_and_are_idempotent(self):
        self.assertTrue(ensure_default_project_rules(self.db))
        self.db.commit()
        keys = {rule.key for rule in list_rules(self.db, ensure=False)}
        self.assertEqual(
            keys,
            {
                "paper_gbt4688_qualitative",
                "microscopy_gbt36422_microscopy",
                "microscopy_gbt36422_cross_section",
                "regenerated_fiber_count_method",
                "regenerated_fiber_area_method",
            },
        )
        self.assertFalse(ensure_default_project_rules(self.db))

    def test_ensure_never_overwrites_admin_edits(self):
        ensure_default_project_rules(self.db)
        row = (
            self.db.query(ExecutionProjectRule)
            .filter(ExecutionProjectRule.rule_key == PAPER_FIBER_RULE_KEY)
            .one()
        )
        row.display_name = "管理员改名"
        row.revision = 7
        self.db.commit()
        self.assertFalse(ensure_default_project_rules(self.db))
        rule = resolve_rule(self.db, PAPER_FIBER_RULE_KEY)
        self.assertEqual(rule.display_name, "管理员改名")
        self.assertEqual(rule.revision, 7)

    def test_seed_configs_pass_validation(self):
        for seed in default_project_rule_seeds():
            self.assertEqual(
                validate_rule_config(seed["config"]),
                [],
                seed["rule_key"],
            )

    def test_validate_rule_config_reports_issues(self):
        base = default_project_rule_seeds()[0]["config"]
        bad_strategy = {
            **base,
            "source": {
                "root_id": "paper_fiber_records",
                "folder_match": {"strategy": "nope", "entry_kind": "workbook"},
            },
        }
        self.assertTrue(
            any("strategy" in issue for issue in validate_rule_config(bad_strategy))
        )
        dup_probes = {
            **base,
            "probes": [
                {"name": "a", "type": "cell_value", "sheet": "S", "cell": "A1"},
                {"name": "a", "type": "cell_value", "sheet": "S", "cell": "A2"},
            ],
        }
        self.assertTrue(
            any("重复" in issue for issue in validate_rule_config(dup_probes))
        )
        missing_cell = {
            **base,
            "probes": [{"name": "a", "type": "cell_value", "sheet": "S"}],
        }
        self.assertTrue(
            any("cell" in issue for issue in validate_rule_config(missing_cell))
        )
        bad_binding = {**base, "binding": {"family_key": "nope"}}
        self.assertTrue(
            any("family_key" in issue for issue in validate_rule_config(bad_binding))
        )
        image_kind_mismatch = {
            **base,
            "source": {
                "root_id": "paper_fiber_records",
                "folder_match": {
                    "strategy": "electron_image_folders",
                    "entry_kind": "workbook",
                },
            },
        }
        self.assertTrue(
            any(
                "entry_kind" in issue
                for issue in validate_rule_config(image_kind_mismatch)
            )
        )

    def test_evaluate_task_facts_matches_aliases(self):
        rule = resolve_rule(self.db, "microscopy_gbt36422_microscopy")
        snapshot = {
            "projects": [
                {
                    "check_item_name": "膜平面形貌",
                    "check_method": "GB/T 36422-2018",
                }
            ]
        }
        conditions, project = evaluate_task_facts(snapshot, rule.task_facts)
        self.assertEqual(conditions, ["task_item_name", "test_method"])
        self.assertIsNotNone(project)
        wrong_method = {
            "projects": [
                {"check_item_name": "纤维微观形貌", "check_method": "GB/T 0000"}
            ]
        }
        conditions, _project = evaluate_task_facts(wrong_method, rule.task_facts)
        self.assertEqual(conditions, ["task_item_name"])

    def test_rule_for_facts_fail_closed(self):
        rule = rule_for_facts(
            self.db,
            check_item_no="5103.426",
            check_item_name="纤维横截面",
        )
        self.assertIsNotNone(rule)
        self.assertEqual(rule.key, "microscopy_gbt36422_cross_section")
        # 别名不命中写门禁（只用规范名）。
        self.assertIsNone(
            rule_for_facts(
                self.db,
                check_item_no="5103.5",
                check_item_name="膜平面形貌",
            )
        )
        row = (
            self.db.query(ExecutionProjectRule)
            .filter(
                ExecutionProjectRule.rule_key
                == "microscopy_gbt36422_cross_section"
            )
            .one()
        )
        row.enabled = False
        self.db.commit()
        self.assertIsNone(
            rule_for_facts(
                self.db,
                check_item_no="5103.426",
                check_item_name="纤维横截面",
            )
        )

    def test_family_lookup_uses_rules_when_db_given(self):
        family = microscopy_family_for_project("5103.426", "纤维横截面", db=self.db)
        self.assertIsNotNone(family)
        self.assertEqual(family.key, "cross_section")
        row = (
            self.db.query(ExecutionProjectRule)
            .filter(
                ExecutionProjectRule.rule_key
                == "microscopy_gbt36422_cross_section"
            )
            .one()
        )
        row.config = {
            **row.config,
            "binding": {
                **row.config["binding"],
                "check_item_name": "纤维横截面（新名称）",
            },
        }
        self.db.commit()
        self.assertIsNone(
            microscopy_family_for_project("5103.426", "纤维横截面", db=self.db)
        )
        self.assertIsNotNone(
            microscopy_family_for_project(
                "5103.426", "纤维横截面（新名称）", db=self.db
            )
        )

    def test_resolve_rule_unknown_and_disabled(self):
        with self.assertRaises(ExecutionApiError) as unknown:
            resolve_rule(self.db, "no_such_rule")
        self.assertEqual(unknown.exception.code, "project_rule_unknown")
        row = (
            self.db.query(ExecutionProjectRule)
            .filter(ExecutionProjectRule.rule_key == PAPER_FIBER_RULE_KEY)
            .one()
        )
        row.enabled = False
        self.db.commit()
        rule = resolve_rule(self.db, PAPER_FIBER_RULE_KEY)
        with self.assertRaises(ExecutionApiError) as disabled:
            require_enabled_rule(rule)
        self.assertEqual(disabled.exception.code, "project_rule_disabled")

    def test_parse_task_fact_normalizes_value_forms(self):
        fact = parse_task_fact(
            {"condition_key": "test_method", "fact": "check_method",
             "op": "eq", "value": "GB/T 4688-2020"}
        )
        self.assertEqual(fact.values, ("GB/T 4688-2020",))
        self.assertEqual(
            microscopy_rule_key("cross_section"),
            "microscopy_gbt36422_cross_section",
        )
        self.assertEqual(microscopy_rule_key(None), "microscopy_gbt36422_microscopy")

    def test_match_rule_summary_via_default_mapping(self):
        from app.execution.project_rules import default_rule_key_for_node_type

        self.assertEqual(
            default_rule_key_for_node_type("file.paper_fiber_gbt4688_qualitative"),
            "paper_gbt4688_qualitative",
        )
        self.assertEqual(
            default_rule_key_for_node_type(
                "file.electron_microscopy_gbt36422", "cross_section"
            ),
            "microscopy_gbt36422_cross_section",
        )
        self.assertIsNone(default_rule_key_for_node_type("core.start"))


class ProjectRuleApiTests(unittest.TestCase):
    """Direct-call coverage for the project-rule endpoints."""

    def setUp(self):
        from app.execution.models import ExecutionUser
        from app.execution.security import hash_password

        self.engine = create_engine("sqlite:///:memory:")
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()
        self.root = ExecutionStorageRoot(
            root_id="paper_fiber_records",
            name="纸类原始记录",
            local_path="/tmp/paper-root",
            access_mode="read",
            category_key="other",
            is_active=True,
            is_available=True,
            last_scan_finished_at=utcnow(),
        )
        self.user = ExecutionUser(
            username="rule-admin",
            display_name="规则管理员",
            password_hash=hash_password("rule-password"),
            role="admin",
        )
        self.db.add_all([self.root, self.user])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_list_detail_update_and_audit(self):
        from app.api.execution import (
            AuthContext,
            project_rule_detail,
            project_rules,
            update_project_rule,
        )
        from app.execution.models import ExecutionAuditLog
        from app.execution.schemas import ProjectRuleUpdateRequest

        auth = AuthContext(session=None, user=self.user)
        listing = project_rules(_auth=auth, db=self.db)
        self.assertEqual(len(listing["items"]), 5)

        detail = project_rule_detail(
            PAPER_FIBER_RULE_KEY, _auth=auth, db=self.db
        )
        config = detail["item"]["config"]
        config["task_facts"][0]["values"] = ["纸、纸板和纸浆纤维鉴别分析", "纸浆纤维鉴别"]
        payload = ProjectRuleUpdateRequest(
            display_name="纸类定性（扩展别名）",
            enabled=True,
            config=config,
        )
        updated = update_project_rule(
            PAPER_FIBER_RULE_KEY, payload, auth=auth, db=self.db
        )
        self.assertEqual(updated["item"]["revision"], 2)
        self.assertEqual(
            updated["item"]["config"]["task_facts"][0]["values"][1],
            "纸浆纤维鉴别",
        )
        audit = (
            self.db.query(ExecutionAuditLog)
            .filter(ExecutionAuditLog.action == "project_rule.update")
            .one()
        )
        self.assertEqual(audit.resource_id, PAPER_FIBER_RULE_KEY)
        self.assertEqual(audit.details["revision"], 2)
        self.assertEqual(
            audit.details["previous"]["config"]["task_facts"][0]["values"],
            ["纸、纸板和纸浆纤维鉴别分析"],
        )

    def test_update_rejects_invalid_config_and_unknown_root(self):
        from app.api.execution import AuthContext, update_project_rule
        from app.execution.schemas import ProjectRuleUpdateRequest

        auth = AuthContext(session=None, user=self.user)
        detail_rule = resolve_rule(self.db, PAPER_FIBER_RULE_KEY)
        bad_config = {
            "source": {"root_id": "paper_fiber_records", "folder_match": {
                "strategy": "nope", "entry_kind": "workbook",
            }},
            "task_facts": [],
            "probes": [],
        }
        with self.assertRaises(ExecutionApiError) as invalid:
            update_project_rule(
                PAPER_FIBER_RULE_KEY,
                ProjectRuleUpdateRequest(
                    display_name=detail_rule.display_name,
                    enabled=True,
                    config=bad_config,
                ),
                auth=auth,
                db=self.db,
            )
        self.assertEqual(invalid.exception.code, "project_rule_config_invalid")

        unknown_root = resolve_rule(self.db, PAPER_FIBER_RULE_KEY)
        config = dict(unknown_root and {} or {})
        config = {
            "default_node_type": "file.paper_fiber_gbt4688_qualitative",
            "source": {"root_id": "no_such_root", "folder_match": {
                "strategy": "level1_contains_number",
                "entry_kind": "workbook",
                "max_depth": 2,
            }},
            "task_facts": [],
            "probes": [],
        }
        with self.assertRaises(ExecutionApiError) as missing_root:
            update_project_rule(
                PAPER_FIBER_RULE_KEY,
                ProjectRuleUpdateRequest(
                    display_name="x",
                    enabled=True,
                    config=config,
                ),
                auth=auth,
                db=self.db,
            )
        self.assertEqual(missing_root.exception.code, "project_rule_root_unknown")

    def test_test_endpoint_dry_runs_without_persisting(self):
        from app.api.execution import AuthContext, test_project_rule
        from app.execution.schemas import ProjectRuleTestRequest

        auth = AuthContext(session=None, user=self.user)
        ensure_default_project_rules(self.db)
        self.db.commit()
        before = (
            self.db.query(ExecutionProjectRule)
            .filter(ExecutionProjectRule.rule_key == PAPER_FIBER_RULE_KEY)
            .one()
            .revision
        )
        result = test_project_rule(
            ProjectRuleTestRequest(
                rule_key=PAPER_FIBER_RULE_KEY,
                inspection_number="26W006701",
            ),
            _auth=auth,
            db=self.db,
        )
        self.assertEqual(result["rule_key"], PAPER_FIBER_RULE_KEY)
        self.assertIn("source_root", result["matched_conditions"])
        after = (
            self.db.query(ExecutionProjectRule)
            .filter(ExecutionProjectRule.rule_key == PAPER_FIBER_RULE_KEY)
            .one()
            .revision
        )
        self.assertEqual(before, after)

        # 临时 config 干跑：未知规则键也可用，不落库。
        draft = test_project_rule(
            ProjectRuleTestRequest(
                config={
                    "default_node_type": "file.paper_fiber_gbt4688_qualitative",
                    "source": {"root_id": "paper_fiber_records", "folder_match": {
                        "strategy": "level1_contains_number",
                        "entry_kind": "workbook",
                        "max_depth": 2,
                    }},
                    "task_facts": [],
                    "probes": [],
                },
                inspection_number="26W006701",
            ),
            _auth=auth,
            db=self.db,
        )
        self.assertEqual(draft["rule_key"], "(draft)")


if __name__ == "__main__":
    unittest.main()
