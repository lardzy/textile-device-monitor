"""电镜—纤维横截面（5103.426 / GB/T 36422-2018）流程变体的后端覆盖。

横截面家族与微观形貌家族共用同一条链路，差异集中在：任务项目匹配、
生成原始记录的 A1 标题、旧系统检验记录登记模板族（仅 1/2/3 张图）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import xlrd
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.execution.catalog import (
    ELECTRON_CROSS_SECTION_WORKFLOW,
    _electron_cross_section_gbt36422_definition,
    _electron_microscopy_gbt36422_definition,
)
from app.execution.errors import ExecutionApiError
from app.execution.microscopy_check_record import (
    MICROSCOPY_CHECK_RECORD_SHEET_NAME,
    _cell_coordinates,
    microscopy_check_record_executor,
)
from app.execution.microscopy_families import (
    ALL_PROJECT_NAME_ALIASES,
    CROSS_SECTION_LEGACY_TEMPLATE_BINDINGS,
    MICROSCOPY_RECORD_FAMILIES,
    microscopy_family_for_key,
    microscopy_family_for_project,
    microscopy_family_from_config,
)
from app.execution.microscopy_original_record import (
    _microscopy_original_record_executor,
    _microscopy_record_context_executor,
    _sha256,
    _template_path,
    resolve_microscopy_legacy_template_binding,
)
from app.execution.models import (
    ExecutionArtifact,
    ExecutionFileIndexEntry,
    ExecutionStorageRoot,
)
from app.execution.validation import definition_checksum, validate_definition


class CrossSectionFamilyRegistryTests(unittest.TestCase):
    def test_config_defaults_to_microscopy(self):
        self.assertIs(
            microscopy_family_from_config(None),
            MICROSCOPY_RECORD_FAMILIES["microscopy"],
        )
        self.assertIs(
            microscopy_family_from_config({}),
            MICROSCOPY_RECORD_FAMILIES["microscopy"],
        )

    def test_config_resolves_cross_section(self):
        family = microscopy_family_from_config(
            {"record_family": "cross_section"}
        )
        self.assertEqual(family.key, "cross_section")
        self.assertEqual(family.check_item_no, "5103.426")
        self.assertEqual(family.check_item_name, "纤维横截面")
        self.assertEqual(family.test_method, "GB/T 36422-2018")
        self.assertEqual(family.record_title, "纤维横截面原始记录")
        self.assertEqual(family.max_selected_images, 3)
        self.assertEqual(
            tuple(sorted(family.template_bindings)), (1, 2, 3)
        )

    def test_unknown_family_key_is_fail_closed(self):
        self.assertIsNone(microscopy_family_for_key("nope"))
        with self.assertRaises(ExecutionApiError) as raised:
            microscopy_family_from_config({"record_family": "nope"})
        self.assertEqual(
            raised.exception.code, "microscopy_record_family_unknown"
        )

    def test_project_lookup_requires_exact_no_and_name(self):
        family = microscopy_family_for_project("5103.426", "纤维横截面")
        self.assertIs(
            family, MICROSCOPY_RECORD_FAMILIES["cross_section"]
        )
        self.assertIs(
            microscopy_family_for_project("5103.5", "纤维微观形貌"),
            MICROSCOPY_RECORD_FAMILIES["microscopy"],
        )
        # 别名不能替代精确项目钉值
        self.assertIsNone(
            microscopy_family_for_project("5103.5", "膜平面形貌")
        )
        self.assertIsNone(
            microscopy_family_for_project("5103.426", "纤维微观形貌")
        )

    def test_alias_union_covers_both_families(self):
        self.assertEqual(
            ALL_PROJECT_NAME_ALIASES,
            frozenset({"纤维微观形貌", "膜平面形貌", "纤维横截面"}),
        )

    def test_cross_section_binding_assets_match_repo_files(self):
        for image_count, binding in CROSS_SECTION_LEGACY_TEMPLATE_BINDINGS.items():
            with self.subTest(image_count=image_count):
                asset = _template_path().parent / binding["local_asset_name"]
                self.assertTrue(asset.is_file())
                self.assertEqual(
                    _sha256(asset), binding["local_asset_sha256"]
                )

    def test_cross_section_rejects_unsupported_image_counts(self):
        family = MICROSCOPY_RECORD_FAMILIES["cross_section"]
        for image_count in (4, 5, 10):
            with self.subTest(image_count=image_count):
                with self.assertRaises(ExecutionApiError) as raised:
                    resolve_microscopy_legacy_template_binding(
                        image_count, family=family
                    )
                self.assertEqual(
                    raised.exception.code,
                    "microscopy_template_image_count_unsupported",
                )


class CrossSectionWorkflowDefinitionTests(unittest.TestCase):
    def test_definition_validates_for_publish(self):
        definition = _electron_cross_section_gbt36422_definition()
        result = validate_definition(definition, for_publish=True)
        self.assertTrue(result.valid, result.as_dict())

    def test_definition_pins_cross_section_family(self):
        definition = _electron_cross_section_gbt36422_definition()
        self.assertEqual(
            definition["metadata"]["slug"], ELECTRON_CROSS_SECTION_WORKFLOW[0]
        )
        nodes = {node["id"]: node for node in definition["nodes"]}
        for node_id in (
            "discover",
            "prepare-record",
            "generate-record",
            "generate-check-record",
        ):
            with self.subTest(node_id=node_id):
                self.assertEqual(
                    nodes[node_id]["config"]["record_family"],
                    "cross_section",
                )
        select = nodes["select-images"]
        self.assertEqual(select["config"]["maximum"], 3)
        self.assertEqual(
            select["input_mapping"]["record_family"],
            "$.nodes.discover.output.record_family",
        )
        self.assertEqual(
            select["input_mapping"]["max_selected_images"],
            "$.nodes.discover.output.max_selected_images",
        )

    def test_microscopy_definition_keeps_default_family(self):
        definition = _electron_microscopy_gbt36422_definition()
        for node in definition["nodes"]:
            self.assertNotIn(
                "record_family", node.get("config") or {}
            )
            self.assertNotIn(
                "record_family", node.get("input_mapping") or {}
            )
        # 既有微观形貌定义的校验和保持稳定（目录自动升级依赖它）。
        self.assertEqual(
            definition_checksum(definition),
            "e97d7cf760466d02119e833856148559ae257d3e3fe219f4c731eb71458b2055",
        )


def _cross_section_task_snapshot() -> dict:
    return {
        "schema_version": 5,
        "inspection_number": "260191285",
        "sample_name": "薇尔®卫生巾（本草芯）",
        "sample_names": ["薇尔®卫生巾（本草芯）"],
        "check_basis": "---",
        "special_wool_occupied_numbers": ["260191285"],
        "projects": [
            {
                "project_key": "task-project:9fcf3fda6a7bc97c4d77cba8",
                "task_check_item_id": "sha256:767adadff7f21eea",
                "check_item_id": "sha256:1dbbee32883d3edd",
                "check_item_no": "51.7",
                "check_item_name": "纤维成分含量",
                "check_method": "FZ/T 01057",
                "check_count": 1,
                "register_count": 0,
                "seq_num": 1,
                "sample_identify": "贴肤层面料",
                "give_judgement": 0,
            },
            {
                "project_key": "task-project:c2acd363a0791fca5430ac24",
                "task_check_item_id": "sha256:71968e60bd8a30d7",
                "check_item_id": "sha256:be4e155d5e220255",
                "check_item_no": "5103.426",
                "check_item_name": "纤维横截面",
                "check_method": "GB/T 36422-2018",
                "check_count": 1,
                "register_count": 1,
                "seq_num": 2,
                "sample_identify": "贴肤层面料",
                "give_judgement": 0,
            },
        ],
    }


class CrossSectionExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.electron_path = base / "electron"
        self.staging_path = base / "staging"
        self.electron_path.mkdir()
        self.staging_path.mkdir()
        self.engine = create_engine("sqlite:///:memory:")
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()
        self.electron_root = ExecutionStorageRoot(
            root_id="electron_microscopy_records",
            name="电镜",
            local_path=str(self.electron_path),
            access_mode="read",
            is_active=True,
            is_available=True,
        )
        self.staging_root = ExecutionStorageRoot(
            root_id="execution_staging",
            name="暂存",
            local_path=str(self.staging_path),
            access_mode="write",
            is_active=True,
            is_available=True,
        )
        self.db.add_all([self.electron_root, self.staging_root])
        self.db.flush()
        path = self.electron_path / "260191285-lisy" / "image01.PNG"
        path.parent.mkdir()
        Image.new("RGB", (160, 100), "white").save(path)
        stat = path.stat()
        self.entry = ExecutionFileIndexEntry(
            storage_root_id=self.electron_root.id,
            relative_path="260191285-lisy/image01.PNG",
            filename=path.name,
            extension=".png",
            file_kind="file",
            size_bytes=stat.st_size,
            modified_at=__import__("datetime").datetime.fromtimestamp(
                stat.st_mtime, __import__("datetime").timezone.utc
            ),
            fingerprint=f"{stat.st_size}:{stat.st_mtime_ns}",
            metadata_json={},
        )
        self.db.add(self.entry)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temporary.cleanup()

    def _node(self, family: str = "cross_section") -> dict:
        return {"config": {"record_family": family}}

    def _record_context(self, family: str = "cross_section"):
        return SimpleNamespace(
            db=self.db,
            run=SimpleNamespace(id="run-cs-1", inspection_number="260191285"),
            node_run=SimpleNamespace(id="node-cs-1"),
            node=self._node(family),
            input_data={
                "inspection_number": "260191285",
                "selected_image_ids": [self.entry.id],
                "selected_images": [
                    {
                        "id": self.entry.id,
                        "root_id": "electron_microscopy_records",
                        "relative_path": self.entry.relative_path,
                        "fingerprint": self.entry.fingerprint,
                    }
                ],
                "task": _cross_section_task_snapshot(),
            },
        )

    def test_record_context_matches_cross_section_project(self):
        context = self._record_context()
        result = _microscopy_record_context_executor(context)
        self.assertEqual(result["task_kind"], "microscopy_record_input")
        self.assertEqual(len(result["projects"]), 1)
        project = result["projects"][0]
        self.assertEqual(project["check_item_no"], "5103.426")
        self.assertEqual(project["check_item_name"], "纤维横截面")
        self.assertEqual(
            result["template_binding"]["legacy_template_name"],
            "纤维横截面.xls",
        )

    def test_record_context_rejects_over_capacity_image_count(self):
        context = self._record_context()
        context.input_data["selected_image_ids"] = [
            f"image-{index}" for index in range(5)
        ]
        with self.assertRaises(ExecutionApiError) as raised:
            _microscopy_record_context_executor(context)
        self.assertEqual(raised.exception.code, "selected_images_required")
        self.assertIn("1 至 3", raised.exception.message)

    def test_record_context_without_cross_section_project_fails(self):
        context = self._record_context()
        snapshot = _cross_section_task_snapshot()
        snapshot["projects"] = [
            project
            for project in snapshot["projects"]
            if project["check_item_name"] != "纤维横截面"
        ]
        context.input_data["task"] = snapshot
        with self.assertRaises(ExecutionApiError) as raised:
            _microscopy_record_context_executor(context)
        self.assertEqual(
            raised.exception.code, "microscopy_task_project_not_found"
        )
        self.assertIn("纤维横截面", raised.exception.message)

    def test_original_record_writes_cross_section_a1_title(self):
        context = SimpleNamespace(
            db=self.db,
            run=SimpleNamespace(id="run-cs-2", inspection_number="260191285"),
            node_run=SimpleNamespace(id="node-cs-2"),
            node=self._node(),
            input_data={
                "inspection_number": "260191285",
                "selected_image_ids": [self.entry.id],
                "selected_images": [
                    {
                        "id": self.entry.id,
                        "root_id": "electron_microscopy_records",
                        "relative_path": self.entry.relative_path,
                        "fingerprint": self.entry.fingerprint,
                    }
                ],
                "task": _cross_section_task_snapshot(),
                "sample_name": "薇尔®卫生巾（本草芯）",
                "sample_identification": "贴肤层面料",
                "judgement_required": False,
            },
        )
        from test_execution_microscopy_original_record import (
            _fake_uno_writer,
        )
        from unittest.mock import patch

        with patch(
            "app.execution.microscopy_original_record._run_uno_writer",
            side_effect=_fake_uno_writer,
        ):
            result = _microscopy_original_record_executor(context)
        self.assertEqual(
            result["template_binding"]["legacy_template_name"],
            "纤维横截面.xls",
        )
        artifact = self.db.get(ExecutionArtifact, result["artifact_id"])
        output = self.staging_path / artifact.relative_path
        workbook = xlrd.open_workbook(str(output), on_demand=True)
        sheet = workbook.sheet_by_name("微观形貌")
        self.assertEqual(sheet.cell_value(0, 0), "纤维横截面原始记录")
        self.assertEqual(sheet.cell_value(1, 1), "260191285")  # B2
        self.assertEqual(sheet.cell_value(2, 11), "贴肤层面料")  # L3
        self.assertTrue(result["verification"]["verified"])

    def test_check_record_uses_cross_section_template_and_filename(self):
        binding = resolve_microscopy_legacy_template_binding(
            1, family=MICROSCOPY_RECORD_FAMILIES["cross_section"]
        )
        context = SimpleNamespace(
            db=self.db,
            run=SimpleNamespace(id="run-cs-3", inspection_number="260191285"),
            node_run=SimpleNamespace(id="node-cs-3"),
            node=self._node(),
            input_data={
                "inspection_number": "260191285",
                "image_count": 1,
                "selected_image_ids": [self.entry.id],
                "template_binding": binding,
                "selected_project": {
                    "check_item_name": "纤维横截面",
                    "check_method": "GB/T 36422-2018",
                    "sample_identify": "贴肤层面料",
                    "give_judgement": 0,
                },
                "sample_identity": "贴肤层面料",
                "test_method": "GB/T 36422-2018",
                "judgement_required": False,
            },
        )
        result = microscopy_check_record_executor(context)
        self.assertEqual(
            result["check_record"]["filename"],
            "260191285-纤维横截面-检验记录登记.xls",
        )
        self.assertEqual(result["template_binding"], binding)
        self.assertTrue(result["verification"]["verified"])
        artifact = self.db.get(ExecutionArtifact, result["artifact_id"])
        output = self.staging_path / artifact.relative_path
        workbook = xlrd.open_workbook(str(output), on_demand=True)
        sheet = workbook.sheet_by_name(MICROSCOPY_CHECK_RECORD_SHEET_NAME)
        row, column = _cell_coordinates("Z7")
        self.assertEqual(sheet.cell_value(row, column), "贴肤层面料")
        row, column = _cell_coordinates("BI7")
        self.assertEqual(sheet.cell_value(row, column), "纤维横截面")
        row, column = _cell_coordinates("AS4")
        self.assertEqual(sheet.cell_value(row, column), 260191285.0)


if __name__ == "__main__":
    unittest.main()
