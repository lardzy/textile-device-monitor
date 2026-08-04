from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import xlrd
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from xlutils.copy import copy as copy_workbook

from app.database import Base
from app.execution.errors import ExecutionApiError
from app.execution.microscopy_original_record import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    MICROSCOPY_TEMPLATE_SHA256,
    _cell_payload,
    _microscopy_record_context_executor,
    _microscopy_original_record_executor,
    _resolve_selected_images,
    _sha256,
    _single_image_geometry_matches,
    _template_path,
    layout_images,
    prepare_original_record_choices,
    sample_name_v1_candidates,
    split_judgement_basis_options,
    split_multi_value_options,
)
from app.execution.models import (
    ExecutionArtifact,
    ExecutionFileIndexEntry,
    ExecutionStorageRoot,
)


def _fake_uno_writer(payload_path: Path) -> dict:
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    source = xlrd.open_workbook(payload["workbook_path"], formatting_info=True)
    target = copy_workbook(source)
    sheet_index = source.sheet_names().index(payload["sheet_name"])
    sheet = target.get_sheet(sheet_index)
    for cell, value in payload["cells"].items():
        match = __import__("re").fullmatch(r"([A-Z]+)([1-9][0-9]*)", cell)
        column = 0
        for character in match.group(1):
            column = column * 26 + ord(character) - ord("A") + 1
        sheet.write(int(match.group(2)) - 1, column - 1, value)
    target.save(payload["workbook_path"])
    return {
        "verified": True,
        "image_count": len(payload["images"]),
        "images": [
            {
                "x": image["x"],
                "y": image["y"],
                "width": image["width"],
                "height": image["height"],
            }
            for image in payload["images"]
        ],
        "canvas": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
        "print_area": payload["print_area"],
        "print_area_verified": True,
        "reopened": True,
    }


class MicroscopyOriginalRecordPureFunctionTests(unittest.TestCase):
    def test_sample_identification_accepts_three_delimiters_and_deduplicates(self):
        self.assertEqual(
            split_multi_value_options(" 正面，反面,横截面、正面 "),
            ["正面", "反面", "横截面"],
        )
        self.assertEqual(split_multi_value_options(None), [])

    def test_judgement_basis_accepts_lines_and_common_delimiters(self):
        self.assertEqual(
            split_judgement_basis_options("GB/T 1；GB/T 2\nGB/T 1"),
            ["GB/T 1", "GB/T 2"],
        )

    def test_sample_name_keeps_original_and_adds_deterministic_fallback(self):
        analysis = sample_name_v1_candidates(
            "Surgicel-Fibrillar",
            jieba_cut=lambda _text: (_ for _ in ()).throw(RuntimeError("bad dict")),
        )
        self.assertEqual(analysis["source"], "fallback")
        self.assertEqual(
            analysis["candidates"],
            ["Surgicel-Fibrillar", "Surgicel", "Fibrillar"],
        )
        self.assertTrue(analysis["selection_required"])

    def test_sample_name_prefers_supplied_jieba_cut_but_remains_stable(self):
        analysis = sample_name_v1_candidates(
            "医用再生纤维素纤维",
            jieba_cut=lambda _text: ["医用", "再生纤维素", "纤维"],
        )
        self.assertEqual(analysis["source"], "jieba")
        self.assertEqual(analysis["candidates"][0], "医用再生纤维素纤维")
        self.assertIn("再生纤维素", analysis["candidates"])

    def test_sample_name_splits_business_phrases_before_jieba_tokens(self):
        analysis = sample_name_v1_candidates(
            "好奇Coform无纺布样品T、好奇普通无纺布样品",
            jieba_cut=lambda value: [value[:2], value[2:]],
        )
        self.assertEqual(
            analysis["candidates"][:2],
            ["好奇Coform无纺布样品T", "好奇普通无纺布样品"],
        )

    def test_context_prefers_snapshot_sample_names(self):
        choices = prepare_original_record_choices(
            {
                "sample_name": "旧聚合值",
                "sample_names": ["名称甲", "名称乙"],
                "projects": [],
            },
            jieba_cut=lambda value: [value],
        )
        self.assertEqual(
            choices["sample_name"]["candidates"][:2], ["名称甲", "名称乙"]
        )

    def test_context_choices_select_relevant_project_and_auto_single_values(self):
        choices = prepare_original_record_choices(
            {
                "sample_name": "Surgicel-Fibrillar",
                "check_basis": "GB/T 36422-2018",
                "projects": [
                    {
                        "check_item_name": "无关项目",
                        "check_method": "OTHER",
                        "sample_identify": "错误值",
                    },
                    {
                        "check_item_name": "膜平面形貌",
                        "check_method": "GB/T 36422-2018",
                        "sample_identify": "正面",
                        "give_judgement": 1,
                    },
                ],
            },
            jieba_cut=lambda value: [value],
        )
        self.assertTrue(choices["judgement_required"])
        self.assertEqual(
            choices["sample_identification"]["automatic_value"], "正面"
        )
        self.assertFalse(
            choices["sample_identification"]["selection_required"]
        )
        self.assertEqual(
            choices["judgement_basis"]["automatic_value"],
            "GB/T 36422-2018",
        )

    def test_cell_payload_only_writes_judgement_fields_when_required(self):
        without = _cell_payload(
            inspection_number="260061860",
            sample_name="示例",
            sample_identification="正面",
            judgement_required=False,
            judgement_basis="不应写入",
            judgement="不应写入",
        )
        self.assertEqual(without["B33"], "")
        self.assertEqual(without["I34"], "")
        with_judgement = _cell_payload(
            inspection_number="260061860",
            sample_name="示例",
            sample_identification="正面",
            judgement_required=True,
            judgement_basis="GB/T 36422-2018",
            judgement="符合",
        )
        self.assertEqual(with_judgement["B33"], "GB/T 36422-2018")
        self.assertEqual(with_judgement["I34"], "符合")
        self.assertEqual(with_judgement["I33"], "")
        self.assertEqual(with_judgement["B34"], "")
        self.assertEqual(with_judgement["B35"], "")

    def test_layout_1_to_10_preserves_ratios_and_stays_inside_canvas(self):
        for count in range(1, 11):
            ratios = [0.6 + (index % 4) * 0.7 for index in range(count)]
            placements = layout_images(ratios)
            self.assertEqual(len(placements), count)
            for placement, ratio in zip(placements, ratios):
                self.assertGreater(placement.width, 0)
                self.assertGreater(placement.height, 0)
                self.assertGreaterEqual(placement.x, 0)
                self.assertGreaterEqual(placement.y, 0)
                self.assertLessEqual(placement.x + placement.width, CANVAS_WIDTH)
                self.assertLessEqual(placement.y + placement.height, CANVAS_HEIGHT)
                self.assertAlmostEqual(
                    placement.width / placement.height,
                    ratio,
                    delta=0.01,
                )
            for left_index, left in enumerate(placements):
                for right in placements[left_index + 1 :]:
                    overlap = not (
                        left.x + left.width <= right.x
                        or right.x + right.width <= left.x
                        or left.y + left.height <= right.y
                        or right.y + right.height <= left.y
                    )
                    self.assertFalse(overlap)

    def test_single_image_is_maximized_to_fill_the_canvas_without_cropping(self):
        square = layout_images([1.0])[0]
        self.assertEqual(square.height, CANVAS_HEIGHT)
        self.assertEqual(square.width, CANVAS_HEIGHT)
        self.assertEqual(square.y, 0)
        self.assertEqual(
            square.x,
            round((CANVAS_WIDTH - CANVAS_HEIGHT) / 2),
        )

        landscape = layout_images([2.0])[0]
        self.assertEqual(landscape.width, CANVAS_WIDTH)
        self.assertEqual(landscape.x, 0)
        self.assertEqual(landscape.height, round(CANVAS_WIDTH / 2))
        self.assertEqual(
            landscape.y,
            round((CANVAS_HEIGHT - landscape.height) / 2),
        )

    def test_persisted_single_image_must_keep_ratio_fill_and_center(self):
        self.assertTrue(
            _single_image_geometry_matches(
                {"x": 3000, "y": 0, "width": 15589, "height": 11698},
                canvas_width=21596,
                canvas_height=11698,
                expected_aspect_ratio=4 / 3,
            )
        )
        self.assertFalse(
            _single_image_geometry_matches(
                {"x": 3000, "y": 0, "width": 14000, "height": 11698},
                canvas_width=21596,
                canvas_height=11698,
                expected_aspect_ratio=4 / 3,
            )
        )
        self.assertFalse(
            _single_image_geometry_matches(
                {"x": 500, "y": 500, "width": 14400, "height": 10800},
                canvas_width=21600,
                canvas_height=11700,
                expected_aspect_ratio=4 / 3,
            )
        )

    def test_versioned_template_has_expected_sha_and_layout(self):
        template = _template_path()
        self.assertEqual(_sha256(template), MICROSCOPY_TEMPLATE_SHA256)
        workbook = xlrd.open_workbook(str(template), formatting_info=True)
        self.assertIn("微观形貌", workbook.sheet_names())
        sheet = workbook.sheet_by_name("微观形貌")
        merged = set(sheet.merged_cells)
        self.assertIn((3, 32, 0, 6), merged)  # A4:F32
        self.assertIn((3, 32, 6, 12), merged)  # G4:L32


class MicroscopyOriginalRecordExecutorTests(unittest.TestCase):
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
        path = self.electron_path / "260061860-lisy" / "image01.PNG"
        path.parent.mkdir()
        Image.new("RGB", (160, 100), "white").save(path)
        stat = path.stat()
        self.entry = ExecutionFileIndexEntry(
            storage_root_id=self.electron_root.id,
            relative_path="260061860-lisy/image01.PNG",
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

    def _candidate(self, **overrides):
        value = {
            "id": self.entry.id,
            "root_id": "electron_microscopy_records",
            "relative_path": self.entry.relative_path,
            "fingerprint": self.entry.fingerprint,
        }
        value.update(overrides)
        return value

    def _context(self):
        return SimpleNamespace(
            db=self.db,
            run=SimpleNamespace(id="run-1", inspection_number="260061860"),
            node_run=SimpleNamespace(id="node-1"),
            input_data={
                "inspection_number": "260061860",
                "selected_image_ids": [self.entry.id],
                "selected_images": [self._candidate()],
                "task": {
                    "sample_name": "Surgicel-Fibrillar",
                    "check_basis": "GB/T 36422-2018",
                    "projects": [
                        {
                            "check_item_name": "膜平面形貌",
                            "check_method": "GB/T 36422-2018",
                            "sample_identify": "正面",
                            "give_judgement": 1,
                        }
                    ],
                },
                "sample_name": "Surgicel-Fibrillar",
                "sample_identification": "正面",
                "judgement_required": True,
                "judgement_basis": "GB/T 36422-2018",
                "judgement": "符合",
            },
        )

    def test_resolver_requeries_id_and_rejects_forged_frontend_path(self):
        resolved = _resolve_selected_images(
            self.db,
            selected_image_ids=[self.entry.id],
            offered_images=[self._candidate()],
        )
        self.assertEqual(resolved[0][0].id, self.entry.id)
        with self.assertRaises(ExecutionApiError) as raised:
            _resolve_selected_images(
                self.db,
                selected_image_ids=[self.entry.id],
                offered_images=[self._candidate(relative_path="../forged.png")],
            )
        self.assertEqual(raised.exception.code, "image_candidate_stale")

    def test_executor_generates_verified_artifact_and_reuses_same_request(self):
        context = self._context()
        with patch(
            "app.execution.microscopy_original_record._run_uno_writer",
            side_effect=_fake_uno_writer,
        ):
            first = _microscopy_original_record_executor(context)
            second = _microscopy_original_record_executor(context)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["artifact_id"], second["artifact_id"])
        self.assertEqual(first["image_count"], 1)
        self.assertTrue(first["verification"]["verified"])
        self.assertEqual(len(first["verification"]["images"]), 1)
        self.assertTrue(first["verification"]["print_area_verified"])
        self.assertEqual(first["print"]["print_area"], "$A$1:$L$37")
        self.assertIn("-图片-", first["original_record"]["filename"])
        artifact = self.db.get(ExecutionArtifact, first["artifact_id"])
        self.assertEqual(artifact.role, "working")
        output = self.staging_path / artifact.relative_path
        self.assertTrue(output.is_file())
        self.assertEqual(output.read_bytes()[:8], b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        workbook = xlrd.open_workbook(str(output), on_demand=True)
        sheet = workbook.sheet_by_name("微观形貌")
        self.assertEqual(sheet.cell_value(1, 1), "260061860")
        self.assertEqual(sheet.cell_value(2, 1), "Surgicel-Fibrillar")
        self.assertEqual(sheet.cell_value(2, 11), "正面")
        self.assertEqual(sheet.cell_value(32, 1), "GB/T 36422-2018")
        self.assertEqual(sheet.cell_value(33, 8), "符合")

    def test_context_returns_only_exact_matching_projects_and_expected_kind(self):
        context = self._context()
        context.input_data["task"]["projects"] = [
            {
                "check_item_name": "纤维微观形貌",
                "check_method": "OTHER",
            },
            {
                "check_item_name": "其它项目",
                "check_method": "GB/T 36422-2018",
            },
            {
                "check_item_name": "纤维微观形貌",
                "check_method": "GB/T 36422-2018",
                "sample_identify": "正面",
            },
            {
                "check_item_name": "无关项目",
                "check_method": "OTHER",
            },
        ]
        result = _microscopy_record_context_executor(context)
        self.assertEqual(result["task_kind"], "microscopy_record_input")
        self.assertEqual(len(result["projects"]), 1)
        self.assertEqual(result["projects"][0]["check_item_name"], "纤维微观形貌")
        self.assertEqual(result["projects"][0]["test_method"], "GB/T 36422-2018")

    def test_context_fails_when_no_exact_task_project_exists(self):
        context = self._context()
        context.input_data["task"]["projects"] = [
            {
                "check_item_name": "纤维微观形貌",
                "check_method": "OTHER",
            }
        ]
        with self.assertRaises(ExecutionApiError) as raised:
            _microscopy_record_context_executor(context)
        self.assertEqual(
            raised.exception.code, "microscopy_task_project_not_found"
        )


if __name__ == "__main__":
    unittest.main()
