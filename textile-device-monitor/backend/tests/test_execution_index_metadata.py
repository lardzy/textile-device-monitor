from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import xlwt
from openpyxl import Workbook

from app.execution.index_metadata import (
    extract_index_metadata,
    inspection_numbers_in_text,
)


class ExecutionIndexMetadataTests(unittest.TestCase):
    def test_identifier_pattern_supports_current_business_codes(self):
        self.assertEqual(
            inspection_numbers_in_text("样品 26X910095-1 原始记录"),
            ["26X910095-1"],
        )
        self.assertEqual(
            inspection_numbers_in_text("编号：260001，复核 260002"),
            ["260001", "260002"],
        )

    def test_xlsx_metadata_extracts_internal_number_and_expected_type(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "record.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "原始记录"
            sheet["A1"] = "再生纤维素纤维定量分析"
            sheet["B2"] = "检验编号：26X910095-1"
            workbook.save(path)
            workbook.close()

            metadata = extract_index_metadata(
                path,
                expected_category="regenerated_fiber",
            )
            self.assertEqual(metadata["parse_status"], "parsed")
            self.assertEqual(metadata["type_status"], "matched")
            self.assertEqual(
                metadata["internal_inspection_numbers"],
                ["26X910095-1"],
            )

    def test_legacy_xls_is_read_only_and_mismatch_is_visible(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "record.xls"
            workbook = xlwt.Workbook()
            sheet = workbook.add_sheet("原始记录")
            sheet.write(0, 0, "苎麻与棉定量分析")
            sheet.write(1, 1, "260001")
            workbook.save(str(path))
            before = path.read_bytes()

            metadata = extract_index_metadata(
                path,
                expected_category="special_wool",
            )
            self.assertEqual(metadata["parse_status"], "parsed")
            self.assertEqual(metadata["type_status"], "mismatch")
            self.assertIn("hemp_cotton", metadata["detected_categories"])
            self.assertEqual(path.read_bytes(), before)

    def test_malformed_workbook_does_not_abort_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "broken.xlsx"
            path.write_bytes(b"not-a-workbook")
            metadata = extract_index_metadata(
                path,
                expected_category="special_wool",
            )
            self.assertEqual(metadata["parse_status"], "failed")
            self.assertIn("parse_error", metadata)


if __name__ == "__main__":
    unittest.main()
