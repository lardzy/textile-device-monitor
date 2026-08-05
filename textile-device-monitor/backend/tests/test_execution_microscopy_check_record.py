from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import xlrd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from xlutils.copy import copy as copy_workbook

from app.database import Base
from app.execution.errors import ExecutionApiError
from app.execution.microscopy_check_record import (
    MICROSCOPY_CHECK_RECORD_CELLS,
    MICROSCOPY_CHECK_RECORD_GENERATOR_VERSION,
    MICROSCOPY_CHECK_RECORD_SHEET_NAME,
    _cell_coordinates,
    _run_uno_writer,
    _template_path,
    _verify_generated_workbook,
    microscopy_check_record_cells,
    microscopy_check_record_executor,
)
from app.execution.microscopy_original_record import (
    MICROSCOPY_SUPPORTED_TEMPLATE_IMAGE_COUNTS,
    resolve_microscopy_legacy_template_binding,
)
from app.execution.models import ExecutionArtifact, ExecutionStorageRoot


def _fake_uno_writer(payload_path: Path) -> dict:
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    source = xlrd.open_workbook(payload["workbook_path"], formatting_info=True)
    target = copy_workbook(source)
    sheet_index = source.sheet_names().index(payload["sheet_name"])
    sheet = target.get_sheet(sheet_index)
    for cell, value in payload["cells"].items():
        row, column = _cell_coordinates(cell)
        sheet.write(row, column, value)
    target.save(payload["workbook_path"])
    return {
        "verified": True,
        "sheet_name": payload["sheet_name"],
        "cells": payload["cells"],
        "recalculated": True,
        "reopened": True,
    }


class MicroscopyCheckRecordPureFunctionTests(unittest.TestCase):
    def test_cells_cover_required_sheet1_fields(self):
        cells = microscopy_check_record_cells(
            inspection_number="260111037",
            sample_identification="纵向",
            test_method="GB/T 36422-2018",
            judgement_required=True,
            judgement_basis="GB/T 36422-2018",
            indicator_requirement="符合标准要求",
            test_result="呈纵向沟槽",
            remark="测试备注",
            judgement="符合",
        )
        self.assertEqual(set(cells), set(MICROSCOPY_CHECK_RECORD_CELLS))
        self.assertEqual(cells["AS4"], "260111037")
        self.assertEqual(cells["Z7"], "纵向")
        self.assertEqual(cells["I8"], "GB/T 36422-2018")
        self.assertEqual(cells["I9"], "GB/T 36422-2018")
        self.assertEqual(cells["I10"], "符合标准要求")
        self.assertEqual(cells["I11"], "呈纵向沟槽")
        self.assertEqual(cells["G12"], "测试备注")
        self.assertEqual(cells["G13"], "符合")

    def test_explicit_no_judgement_clears_all_judgement_fields(self):
        cells = microscopy_check_record_cells(
            inspection_number="260111037",
            sample_identification=None,
            judgement_required=False,
            judgement_basis="不得写入",
            indicator_requirement="不得写入",
            test_result="不得写入",
            judgement="不得写入",
        )
        self.assertEqual(cells["Z7"], "")
        self.assertEqual(cells["I9"], "")
        self.assertEqual(cells["I10"], "")
        self.assertEqual(cells["I11"], "")
        self.assertEqual(cells["G13"], "")
        self.assertEqual(cells["I8"], "GB/T 36422-2018")

    def test_rejects_other_test_method(self):
        with self.assertRaises(ExecutionApiError) as raised:
            microscopy_check_record_cells(
                inspection_number="260111037",
                test_method="OTHER",
            )
        self.assertEqual(
            raised.exception.code,
            "microscopy_check_record_method_mismatch",
        )


class MicroscopyCheckRecordExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.staging_path = Path(self.temporary.name) / "staging"
        self.staging_path.mkdir()
        self.engine = create_engine("sqlite:///:memory:")
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()
        self.staging_root = ExecutionStorageRoot(
            root_id="execution_staging",
            name="暂存",
            local_path=str(self.staging_path),
            access_mode="write",
            is_active=True,
            is_available=True,
        )
        self.db.add(self.staging_root)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temporary.cleanup()

    def _context(self, image_count: int = 1):
        return SimpleNamespace(
            db=self.db,
            run=SimpleNamespace(
                id=f"run-{image_count}", inspection_number="260111037"
            ),
            node_run=SimpleNamespace(id=f"check-record-{image_count}"),
            input_data={
                "inspection_number": "260111037",
                "image_count": image_count,
                "selected_image_ids": [
                    f"image-{index + 1}" for index in range(image_count)
                ],
                "template_binding": resolve_microscopy_legacy_template_binding(
                    image_count
                ),
                "sample_identity": "纵向",
                "test_method": "GB/T 36422-2018",
                "judgement_required": True,
                "judge_basis": "GB/T 36422-2018",
                "indicator_requirement": "符合标准要求",
                "test_result": "呈纵向沟槽",
                "remark": "测试备注",
                "judgement": "符合",
            },
        )

    def test_executor_generates_verified_artifact_and_reuses_request(self):
        context = self._context(1)
        with patch(
            "app.execution.microscopy_check_record._run_uno_writer",
            side_effect=_fake_uno_writer,
        ):
            first = microscopy_check_record_executor(context)
            second = microscopy_check_record_executor(context)

        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["artifact_id"], second["artifact_id"])
        self.assertEqual(
            first["template_binding"]["legacy_template_name"],
            "微观形貌.xls",
        )
        self.assertEqual(
            first["legacy_registration_workbook"], first["check_record"]
        )
        self.assertEqual(first["expected_key_identities"], ["纵向"])
        artifact = self.db.get(ExecutionArtifact, first["artifact_id"])
        self.assertEqual(artifact.role, "working")
        self.assertEqual(
            artifact.metadata_json["generator_version"],
            MICROSCOPY_CHECK_RECORD_GENERATOR_VERSION,
        )
        self.assertEqual(
            artifact.metadata_json["template_binding"],
            first["template_binding"],
        )
        self.assertTrue(artifact.metadata_json["verification"]["verified"])
        self.assertEqual(
            artifact.metadata_json["expected_key_identities"], ["纵向"]
        )
        output = self.staging_path / artifact.relative_path
        self.assertTrue(output.is_file())
        self.assertEqual(
            output.read_bytes()[:8], b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        )
        workbook = xlrd.open_workbook(str(output), on_demand=True)
        sheet = workbook.sheet_by_name(MICROSCOPY_CHECK_RECORD_SHEET_NAME)
        for cell, expected in {
            "AS4": "260111037",
            "Z7": "纵向",
            "I8": "GB/T 36422-2018",
            "I9": "GB/T 36422-2018",
            "I10": "符合标准要求",
            "I11": "呈纵向沟槽",
            "G12": "测试备注",
            "G13": "符合",
        }.items():
            row, column = _cell_coordinates(cell)
            self.assertEqual(sheet.cell_value(row, column), expected)
        workbook.release_resources()

    def test_every_configured_image_count_uses_its_bound_asset(self):
        for image_count in MICROSCOPY_SUPPORTED_TEMPLATE_IMAGE_COUNTS:
            with self.subTest(image_count=image_count):
                context = self._context(image_count)
                with patch(
                    "app.execution.microscopy_check_record._run_uno_writer",
                    side_effect=_fake_uno_writer,
                ):
                    result = microscopy_check_record_executor(context)
                binding = resolve_microscopy_legacy_template_binding(image_count)
                self.assertEqual(result["image_count"], image_count)
                self.assertEqual(result["template_binding"], binding)
                artifact = self.db.get(ExecutionArtifact, result["artifact_id"])
                self.assertEqual(
                    artifact.metadata_json["template_sha256"],
                    binding["local_asset_sha256"],
                )

    def test_unsupported_or_inconsistent_image_count_fails_before_write(self):
        unsupported = self._context(1)
        unsupported.input_data["image_count"] = 4
        unsupported.input_data["selected_image_ids"] = [
            f"image-{index}" for index in range(4)
        ]
        unsupported.input_data.pop("template_binding")
        with self.assertRaises(ExecutionApiError) as raised:
            microscopy_check_record_executor(unsupported)
        self.assertEqual(
            raised.exception.code,
            "microscopy_template_image_count_unsupported",
        )

        inconsistent = self._context(2)
        inconsistent.input_data["selected_image_ids"] = ["image-1"]
        with self.assertRaises(ExecutionApiError) as raised:
            microscopy_check_record_executor(inconsistent)
        self.assertEqual(
            raised.exception.code,
            "microscopy_check_record_image_count_mismatch",
        )

    def test_forged_binding_is_rejected(self):
        context = self._context(7)
        context.input_data["template_binding"] = dict(
            context.input_data["template_binding"]
        )
        context.input_data["template_binding"]["legacy_template_name"] = (
            "伪造模板.xls"
        )
        with self.assertRaises(ExecutionApiError) as raised:
            microscopy_check_record_executor(context)
        self.assertEqual(
            raised.exception.code,
            "microscopy_template_binding_mismatch",
        )


@unittest.skipUnless(
    os.getenv("EXECUTION_RUN_UNO_INTEGRATION_TESTS") == "1",
    "set EXECUTION_RUN_UNO_INTEGRATION_TESTS=1 inside the worker image",
)
class MicroscopyCheckRecordUnoIntegrationTests(unittest.TestCase):
    def test_real_xls_is_saved_recalculated_and_reopened(self):
        binding = resolve_microscopy_legacy_template_binding(7)
        cells = microscopy_check_record_cells(
            inspection_number="UNO-CHECK-RECORD",
            sample_identification="纵向",
            test_method="GB/T 36422-2018",
            judgement_required=False,
            remark="UNO 重读核对",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workbook_path = root / "working.xls"
            shutil.copyfile(_template_path(binding), workbook_path)
            payload_path = root / "payload.json"
            payload_path.write_text(
                json.dumps(
                    {
                        "workbook_path": str(workbook_path),
                        "sheet_name": MICROSCOPY_CHECK_RECORD_SHEET_NAME,
                        "cells": cells,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = _run_uno_writer(payload_path)
            verification = _verify_generated_workbook(
                workbook_path,
                expected_cells=cells,
                uno_result=result,
            )
        self.assertTrue(verification["verified"])
        self.assertTrue(verification["recalculated"])
        self.assertTrue(verification["reopened"])


if __name__ == "__main__":
    unittest.main()
