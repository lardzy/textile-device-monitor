from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import xlrd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.execution.errors import ExecutionApiError
from app.execution.microscopy_check_record import (
    MICROSCOPY_CHECK_RECORD_CELLS,
    MICROSCOPY_CHECK_RECORD_GENERATOR_VERSION,
    MICROSCOPY_CHECK_RECORD_SHEET_NAME,
    _cell_coordinates,
    _template_path,
    microscopy_check_record_cells,
    microscopy_check_record_executor,
)
from app.execution.microscopy_original_record import (
    MICROSCOPY_SUPPORTED_TEMPLATE_IMAGE_COUNTS,
    resolve_microscopy_legacy_template_binding,
)
from app.execution.models import ExecutionArtifact, ExecutionStorageRoot


class MicroscopyCheckRecordPureFunctionTests(unittest.TestCase):
    def test_cells_cover_required_sheet1_fields(self):
        cells = microscopy_check_record_cells(
            inspection_number="260111037",
            sample_identification="纵向",
            test_method="GB/T 36422-2018",
            check_item_name="纤维微观形貌",
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
        # OriginalKeyDataConfig feed cells carry final literals.
        self.assertEqual(cells["BI7"], "纤维微观形貌")
        self.assertEqual(cells["BK7"], "纵向")
        self.assertEqual(cells["BI8"], "GB/T 36422-2018")
        self.assertEqual(cells["BI9"], "GB/T 36422-2018")
        self.assertEqual(cells["BI10"], "符合标准要求")
        self.assertEqual(cells["BI11"], "呈纵向沟槽")
        self.assertEqual(cells["BI12"], "测试备注")
        self.assertEqual(cells["BI13"], "符合")

    def test_item_name_defaults_to_microscopy(self):
        cells = microscopy_check_record_cells(
            inspection_number="260111037",
        )
        self.assertEqual(cells["BI7"], "纤维微观形貌")
        self.assertEqual(cells["BI8"], "GB/T 36422-2018")
        self.assertEqual(cells["BK7"], "")

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
        self.assertEqual(cells["BI9"], "")
        self.assertEqual(cells["BI10"], "")
        self.assertEqual(cells["BI11"], "")
        self.assertEqual(cells["BI13"], "")
        self.assertEqual(cells["BK7"], "")
        self.assertEqual(cells["BI8"], "GB/T 36422-2018")

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

    def _context(self, image_count: int = 1, number: str = "260111037"):
        return SimpleNamespace(
            db=self.db,
            run=SimpleNamespace(
                id=f"run-{image_count}-{number}", inspection_number=number
            ),
            node_run=SimpleNamespace(id=f"check-record-{image_count}-{number}"),
            input_data={
                "inspection_number": number,
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
        row, column = _cell_coordinates("AS4")
        self.assertEqual(sheet.cell_value(row, column), 260111037.0)
        # 隐藏类别镜像格与公式缓存必须与可见格一致，公式本身完整保留。
        from app.execution.biff_patch import formula_cells, ole_read

        ole = ole_read(output)
        stream = next(s.data for s in ole.streams if s.name == "Workbook")
        formulas = formula_cells(stream)
        self.assertEqual(len(formulas), 8)
        self.assertEqual(formulas[(6, 60)][1], "纤维微观形貌")
        self.assertEqual(formulas[(6, 62)][1], "纵向")
        self.assertEqual(formulas[(7, 8)][1], "GB/T 36422-2018")
        for mirror, expected in {
            "BI9": "GB/T 36422-2018",
            "BI10": "符合标准要求",
            "BI11": "呈纵向沟槽",
            "BI12": "测试备注",
            "BI13": "符合",
        }.items():
            row, column = _cell_coordinates(mirror)
            self.assertEqual(sheet.cell_value(row, column), expected)
        row, column = _cell_coordinates("BI8")
        self.assertEqual(sheet.cell_value(row, column), "GB/T 36422-2018")
        workbook.release_resources()

    def test_written_cells_keep_template_format_without_duplicates(self):
        # 登记模板把 Z7、I9-I11、G12、G13、BI8 等空格存放在 MULBLANK 区间里
        # （Z7=79、I9/G12 系=74、G13=75）。写入必须拆分区间并继承该列被分配
        # 的 XF；若在区间外再插一条外来 XF 的记录，Excel 会用后一条记录覆盖
        # 模板底色（Z7 填充色丢失的实测回归）。
        import struct

        from app.execution.biff_patch import ole_read

        single_ids = {0x00FD, 0x0201, 0x0203, 0x027E, 0x0204, 0x0006, 0x00D6}

        def cell_format_map(path):
            """address -> xf, expanding MUL ranges; plus duplicate detection."""
            data = next(
                stream.data
                for stream in ole_read(path).streams
                if stream.name == "Workbook"
            )
            formats: dict[tuple[int, int], int] = {}
            duplicates: list[tuple[int, int]] = []
            pos = 0
            while pos + 4 <= len(data):
                record_id, size = struct.unpack_from("<HH", data, pos)
                payload = data[pos + 4 : pos + 4 + size]
                if record_id in (0x00BD, 0x00BE):
                    row, first = struct.unpack_from("<HH", payload, 0)
                    last = struct.unpack_from("<H", payload, len(payload) - 2)[0]
                    step = 6 if record_id == 0x00BD else 2
                    for column in range(first, last + 1):
                        xf = struct.unpack_from(
                            "<H", payload, 4 + step * (column - first)
                        )[0]
                        if (row, column) in formats:
                            duplicates.append((row, column))
                        formats[(row, column)] = xf
                elif record_id in single_ids and size >= 6:
                    row, column, xf = struct.unpack_from("<HHH", payload, 0)
                    if (row, column) in formats:
                        duplicates.append((row, column))
                    formats[(row, column)] = xf
                pos += 4 + size
            return formats, duplicates

        context = self._context(1)
        result = microscopy_check_record_executor(context)
        artifact = self.db.get(ExecutionArtifact, result["artifact_id"])
        output = self.staging_path / artifact.relative_path
        template = _template_path(result["template_binding"])

        template_formats, _ = cell_format_map(template)
        output_formats, output_duplicates = cell_format_map(output)

        written = ("AS4", "Z7", "I8", "I9", "I10", "I11", "G12", "G13", "BI8")
        for address in written:
            coords = _cell_coordinates(address)
            with self.subTest(cell=address):
                self.assertIn(coords, template_formats)
                self.assertEqual(
                    output_formats.get(coords),
                    template_formats[coords],
                )
        self.assertEqual(output_duplicates, [])

    def test_every_configured_image_count_uses_its_bound_asset(self):
        for image_count in MICROSCOPY_SUPPORTED_TEMPLATE_IMAGE_COUNTS:
            with self.subTest(image_count=image_count):
                context = self._context(image_count)
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

    def test_alphanumeric_report_number_is_written_as_text(self):
        # 检验编号第三位可能是字母（如 26A045793）：AS4 必须按文本写入，
        # 不能数值化（float('26A045793') 曾直接让生成节点崩溃）。
        context = self._context(1, number="26A045793")
        result = microscopy_check_record_executor(context)
        artifact = self.db.get(ExecutionArtifact, result["artifact_id"])
        self.assertTrue(artifact.metadata_json["verification"]["verified"])
        output = self.staging_path / artifact.relative_path
        workbook = xlrd.open_workbook(str(output), on_demand=True)
        sheet = workbook.sheet_by_name(MICROSCOPY_CHECK_RECORD_SHEET_NAME)
        row, column = _cell_coordinates("AS4")
        cell = sheet.cell(row, column)
        self.assertEqual(cell.ctype, xlrd.XL_CELL_TEXT)
        self.assertEqual(cell.value, "26A045793")
        workbook.release_resources()

    def test_leading_zero_number_keeps_text_form(self):
        # 前导零纯数字编号一旦数值化会丢位（'026...' -> 26...），同样按文本写入。
        context = self._context(1, number="026004579")
        result = microscopy_check_record_executor(context)
        artifact = self.db.get(ExecutionArtifact, result["artifact_id"])
        output = self.staging_path / artifact.relative_path
        workbook = xlrd.open_workbook(str(output), on_demand=True)
        sheet = workbook.sheet_by_name(MICROSCOPY_CHECK_RECORD_SHEET_NAME)
        row, column = _cell_coordinates("AS4")
        self.assertEqual(sheet.cell_value(row, column), "026004579")
        workbook.release_resources()

    def test_long_numeric_number_keeps_exact_text_form(self):
        number = "12345678901234567"
        context = self._context(1, number=number)
        result = microscopy_check_record_executor(context)
        artifact = self.db.get(ExecutionArtifact, result["artifact_id"])
        output = self.staging_path / artifact.relative_path
        workbook = xlrd.open_workbook(str(output), on_demand=True)
        sheet = workbook.sheet_by_name(MICROSCOPY_CHECK_RECORD_SHEET_NAME)
        row, column = _cell_coordinates("AS4")
        cell = sheet.cell(row, column)
        self.assertEqual(cell.ctype, xlrd.XL_CELL_TEXT)
        self.assertEqual(cell.value, number)
        workbook.release_resources()

    def test_generator_version_change_does_not_reuse_old_artifact(self):
        context = self._context(1, number="026004579")
        with patch(
            "app.execution.microscopy_check_record."
            "MICROSCOPY_CHECK_RECORD_GENERATOR_VERSION",
            "gbt36422-2018-microscopy-check-record-v3",
        ):
            old = microscopy_check_record_executor(context)

        current = microscopy_check_record_executor(context)

        self.assertFalse(old["reused"])
        self.assertFalse(current["reused"])
        self.assertNotEqual(old["artifact_id"], current["artifact_id"])
        old_artifact = self.db.get(ExecutionArtifact, old["artifact_id"])
        current_artifact = self.db.get(
            ExecutionArtifact, current["artifact_id"]
        )
        self.assertNotEqual(
            old_artifact.relative_path,
            current_artifact.relative_path,
        )
        self.assertEqual(
            current_artifact.metadata_json["generator_version"],
            MICROSCOPY_CHECK_RECORD_GENERATOR_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
