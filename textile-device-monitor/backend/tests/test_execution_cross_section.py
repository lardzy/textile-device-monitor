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




if __name__ == "__main__":
    unittest.main()
