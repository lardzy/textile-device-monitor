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
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from xlutils.copy import copy as copy_workbook

from app.database import Base
from app.execution.errors import ExecutionApiError
from app.execution.microscopy_original_record import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    MICROSCOPY_BIFF_EXCEL_X_SCALE,
    MICROSCOPY_LEGACY_TEMPLATE_BINDINGS,
    MICROSCOPY_PRINT_AREA,
    MICROSCOPY_SHEET_NAME,
    MICROSCOPY_SUPPORTED_TEMPLATE_IMAGE_COUNTS,
    MICROSCOPY_TEMPLATE_SHA256,
    _cell_payload,
    _microscopy_record_context_executor,
    _microscopy_original_record_executor,
    _persisted_images_geometry,
    _resolve_selected_images,
    _run_uno_writer,
    _sha256,
    _single_image_geometry_matches,
    _template_path,
    _verify_generated_workbook,
    layout_images,
    prepare_original_record_choices,
    resolve_microscopy_legacy_template_binding,
    sample_name_v1_candidates,
    split_judgement_basis_options,
    split_multi_value_options,
)
from app.execution.microscopy_original_record_uno import (
    _decode_horizontal,
    _encode_horizontal,
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
    x_scale = payload["canvas"]["biff_excel_x_scale"]
    images = []
    for image in payload["images"]:
        logical = {
            "x": image["x"],
            "y": image["y"],
            "width": image["width"],
            "height": image["height"],
        }
        images.append(
            {
                "index": image["index"],
                "source_id": image["source_id"],
                **logical,
                "logical": logical,
                "encoded": {
                    "x": _encode_horizontal(image["x"], 1.0, x_scale),
                    "y": image["y"],
                    "width": _encode_horizontal(
                        image["width"], 1.0, x_scale
                    ),
                    "height": image["height"],
                },
                "resize_with_cell": False,
            }
        )
    return {
        "verified": True,
        "image_count": len(payload["images"]),
        "images": images,
        "canvas": {
            "width": CANVAS_WIDTH,
            "height": CANVAS_HEIGHT,
            "biff_excel_x_scale": x_scale,
        },
        "print_area": payload["print_area"],
        "print_area_verified": True,
        "ordinary_print_area_removed": True,
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

    def test_biff_horizontal_compensation_round_trips_logical_geometry(self):
        # SimSun (宋体) is available in the runtime image, so LibreOffice and
        # Excel derive identical column widths and the factor must stay 1.0.
        self.assertEqual(MICROSCOPY_BIFF_EXCEL_X_SCALE, 1.0)
        for logical in (0, 3006, 15589, CANVAS_WIDTH):
            encoded = _encode_horizontal(
                logical, 1.0, MICROSCOPY_BIFF_EXCEL_X_SCALE
            )
            decoded = _decode_horizontal(
                encoded, MICROSCOPY_BIFF_EXCEL_X_SCALE
            )
            self.assertAlmostEqual(decoded, logical, delta=1)

    def test_reopened_geometry_validates_non_square_1_2_5_and_10_images(self):
        source_ratios = [4 / 3, 2.0, 0.5, 1.6, 0.75, 1.25, 0.8, 1.8, 0.6, 1.1]
        for count in (1, 2, 5, 10):
            ratios = source_ratios[:count]
            placements = layout_images(ratios)
            expected_images = []
            actual_images = []
            for placement, ratio in zip(placements, ratios):
                expected = {
                    "source_id": f"source-{placement.index}",
                    "aspect_ratio": ratio,
                    **placement.as_dict(),
                }
                logical = {
                    "x": placement.x,
                    "y": placement.y,
                    "width": placement.width,
                    "height": placement.height,
                }
                actual = {
                    "index": placement.index,
                    "source_id": expected["source_id"],
                    **logical,
                    "logical": logical,
                    "encoded": {
                        "x": _encode_horizontal(
                            placement.x,
                            1.0,
                            MICROSCOPY_BIFF_EXCEL_X_SCALE,
                        ),
                        "y": placement.y,
                        "width": _encode_horizontal(
                            placement.width,
                            1.0,
                            MICROSCOPY_BIFF_EXCEL_X_SCALE,
                        ),
                        "height": placement.height,
                    },
                    "resize_with_cell": False,
                }
                expected_images.append(expected)
                actual_images.append(actual)
            result = _persisted_images_geometry(
                actual_images,
                expected_images=expected_images,
                canvas_width=CANVAS_WIDTH,
                canvas_height=CANVAS_HEIGHT,
            )
            self.assertTrue(result["verified"], result["issues"])
            self.assertTrue(result["non_overlapping"])

    def test_reopened_geometry_rejects_square_conversion_and_overlap(self):
        ratios = [2.0, 0.5]
        placements = layout_images(ratios)
        expected_images = [
            {
                "source_id": f"source-{placement.index}",
                "aspect_ratio": ratio,
                **placement.as_dict(),
            }
            for placement, ratio in zip(placements, ratios)
        ]
        actual_images = []
        for placement in placements:
            logical = placement.as_dict()
            logical.pop("index")
            actual_images.append(
                {
                    "index": placement.index,
                    "source_id": f"source-{placement.index}",
                    **logical,
                    "logical": dict(logical),
                    "encoded": dict(logical),
                    "resize_with_cell": False,
                }
            )
        actual_images[0]["logical"]["width"] = actual_images[0]["logical"][
            "height"
        ]
        actual_images[1]["logical"]["x"] = actual_images[0]["logical"]["x"]
        actual_images[1]["logical"]["y"] = actual_images[0]["logical"]["y"]
        result = _persisted_images_geometry(
            actual_images,
            expected_images=expected_images,
            canvas_width=CANVAS_WIDTH,
            canvas_height=CANVAS_HEIGHT,
        )
        codes = {issue["code"] for issue in result["issues"]}
        self.assertFalse(result["verified"])
        self.assertIn("persisted_image_aspect_ratio_mismatch", codes)
        self.assertIn("persisted_images_overlap", codes)

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
        self.assertEqual(
            template.name,
            "gbt36422-2018-microscopy-original-record-v1.xls",
        )
        workbook = xlrd.open_workbook(str(template), formatting_info=True)
        self.assertIn("微观形貌", workbook.sheet_names())
        sheet = workbook.sheet_by_name("微观形貌")
        merged = set(sheet.merged_cells)
        self.assertIn((3, 32, 0, 6), merged)  # A4:F32
        self.assertIn((3, 32, 6, 12), merged)  # G4:L32

    def test_legacy_template_bindings_match_versioned_assets(self):
        expected = {
            1: (
                "微观形貌.xls",
                "gbt36422-2018-microscopy-1-image-v1.xls",
                "a09399783171826d10b239bd01cb596569428bbc34a8c4636077e98f34dc690e",
            ),
            2: (
                "纤维微观形貌-GB T 36422-2018-2张图.xls",
                "gbt36422-2018-microscopy-2-images-v1.xls",
                "2ff546b96da9ac423613374ee28955bf7dfe3e62e5d40d8ad979c93636836f23",
            ),
            3: (
                "纤维微观形貌-GB T 36422-2018-3张图.xls",
                "gbt36422-2018-microscopy-3-images-v1.xls",
                "43ae3872f231c2499b98976ea63827162b4fddf7151e3e8daceee8a7591d4268",
            ),
            5: (
                "纤维微观形貌-GB T 36422-2018-5张图.xls",
                "gbt36422-2018-microscopy-5-images-v1.xls",
                "d21e82cd1672ada28beab35673e1d679bf3ffa1099969f02dbed466645dc9b6d",
            ),
            6: (
                "纤维微观形貌-GB T 36422-2018-6张图.xls",
                "gbt36422-2018-microscopy-6-images-v1.xls",
                "5f56deb633c0dd2b2dc046ba70ab012c2780dbbae9039663b4d40903804a6304",
            ),
            7: (
                "纤维微观形貌-GB T 36422-2018-7张图.xls",
                "gbt36422-2018-microscopy-7-images-v1.xls",
                "3aea5aa68bccb1a8e9035be8762d305bb2f0d6e4104ad29557082a3b1abada01",
            ),
            10: (
                "纤维微观形貌-GB T 36422-2018-10张图.xls",
                "gbt36422-2018-microscopy-10-images-v1.xls",
                "976a88ed86af2a3fb30df5aa830e0529ea35e15e1e2fa59244e3035578940f74",
            ),
        }
        self.assertEqual(
            MICROSCOPY_SUPPORTED_TEMPLATE_IMAGE_COUNTS,
            tuple(expected),
        )
        for image_count, values in expected.items():
            with self.subTest(image_count=image_count):
                binding = resolve_microscopy_legacy_template_binding(
                    image_count
                )
                self.assertEqual(binding["legacy_template_name"], values[0])
                self.assertEqual(binding["local_asset_name"], values[1])
                self.assertEqual(binding["mapping_config_sha256"], values[2])
                asset = _template_path().parent / binding["local_asset_name"]
                self.assertEqual(
                    _sha256(asset), binding["local_asset_sha256"]
                )
                self.assertEqual(
                    binding,
                    MICROSCOPY_LEGACY_TEMPLATE_BINDINGS[image_count],
                )

    def test_legacy_template_binding_rejects_unsupported_counts(self):
        for image_count in (4, 8, 9):
            with self.subTest(image_count=image_count):
                with self.assertRaises(ExecutionApiError) as raised:
                    resolve_microscopy_legacy_template_binding(image_count)
                self.assertEqual(
                    raised.exception.code,
                    "microscopy_template_image_count_unsupported",
                )


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
        return self._candidate_for(self.entry, **overrides)

    def _candidate_for(self, entry, **overrides):
        value = {
            "id": entry.id,
            "root_id": "electron_microscopy_records",
            "relative_path": entry.relative_path,
            "fingerprint": entry.fingerprint,
        }
        value.update(overrides)
        return value

    def _add_indexed_image(self, index: int, width: int, height: int):
        path = self.electron_path / "260061860-lisy" / f"image{index:02d}.PNG"
        Image.new("RGB", (width, height), "white").save(path)
        stat = path.stat()
        entry = ExecutionFileIndexEntry(
            storage_root_id=self.electron_root.id,
            relative_path=f"260061860-lisy/{path.name}",
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
        self.db.add(entry)
        self.db.flush()
        return entry

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
        self.assertEqual(
            first["template_binding"], second["template_binding"]
        )
        self.assertEqual(
            first["template_binding"]["legacy_template_name"],
            "微观形貌.xls",
        )
        self.assertTrue(first["verification"]["verified"])
        self.assertEqual(len(first["verification"]["images"]), 1)
        self.assertTrue(first["verification"]["print_area_verified"])
        self.assertEqual(first["print"]["print_area"], "$A$1:$L$37")
        self.assertEqual(
            first["original_record"]["filename"],
            "260061860-39-8B-纤维形状截面定量试验-2026.xls",
        )
        artifact = self.db.get(ExecutionArtifact, first["artifact_id"])
        self.assertEqual(artifact.role, "working")
        self.assertEqual(
            artifact.metadata_json["template_binding"],
            first["template_binding"],
        )
        self.assertEqual(len(artifact.metadata_json["request_digest"]), 64)
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

    def test_executor_rejects_forged_declared_template_binding(self):
        context = self._context()
        forged = resolve_microscopy_legacy_template_binding(1)
        forged["legacy_template_name"] = "被篡改的模板.xls"
        context.input_data["template_binding"] = forged
        with self.assertRaises(ExecutionApiError) as raised:
            _microscopy_original_record_executor(context)
        self.assertEqual(
            raised.exception.code,
            "microscopy_template_binding_mismatch",
        )

    def test_executor_rechecks_actual_image_count_against_template_map(self):
        entries = [self.entry]
        entries.extend(
            self._add_indexed_image(index, 160, 100)
            for index in range(2, 5)
        )
        self.db.commit()
        context = self._context()
        context.input_data["selected_image_ids"] = [
            entry.id for entry in entries
        ]
        context.input_data["selected_images"] = [
            self._candidate_for(entry) for entry in entries
        ]
        with self.assertRaises(ExecutionApiError) as raised:
            _microscopy_original_record_executor(context)
        self.assertEqual(
            raised.exception.code,
            "microscopy_template_image_count_unsupported",
        )

    def test_executor_preserves_source_identity_for_2_5_and_10_images(self):
        entries = [self.entry]
        ratios = [(200, 100), (100, 200), (160, 100), (100, 160), (4, 3)]
        for index in range(2, 11):
            width, height = ratios[(index - 1) % len(ratios)]
            entries.append(self._add_indexed_image(index, width, height))
        self.db.commit()

        for count in (2, 5, 10):
            selected = entries[:count]
            context = self._context()
            context.run.id = f"run-{count}"
            context.node_run.id = f"node-{count}"
            context.input_data["selected_image_ids"] = [
                entry.id for entry in selected
            ]
            context.input_data["selected_images"] = [
                self._candidate_for(entry) for entry in selected
            ]
            with patch(
                "app.execution.microscopy_original_record._run_uno_writer",
                side_effect=_fake_uno_writer,
            ):
                result = _microscopy_original_record_executor(context)
            verification = result["verification"]
            self.assertEqual(result["image_count"], count)
            self.assertTrue(verification["all_images_geometry_verified"])
            self.assertTrue(verification["multi_image_non_overlap_verified"])
            self.assertEqual(
                [image["source_id"] for image in verification["images"]],
                [entry.id for entry in selected],
            )

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
        self.assertEqual(result["template_binding"]["image_count"], 1)
        self.assertEqual(
            result["template_binding"]["legacy_template_name"],
            "微观形貌.xls",
        )

    def test_context_rejects_image_counts_without_legacy_template(self):
        for image_count in (4, 8, 9):
            context = self._context()
            context.input_data["selected_image_ids"] = [
                f"image-{index}" for index in range(image_count)
            ]
            with self.subTest(image_count=image_count):
                with self.assertRaises(ExecutionApiError) as raised:
                    _microscopy_record_context_executor(context)
                self.assertEqual(
                    raised.exception.code,
                    "microscopy_template_image_count_unsupported",
                )

    def test_context_recovers_latest_snapshot_for_already_running_definition(self):
        context = self._context()
        expected = context.input_data.pop("task")
        with patch(
            "app.execution.microscopy_original_record.cached_task_snapshot",
            return_value={"cache_state": "ready", "snapshot": expected},
        ) as read_cache:
            result = _microscopy_record_context_executor(context)

        read_cache.assert_called_once_with(
            self.db,
            inspection_number="260061860",
        )
        self.assertEqual(result["projects"][0]["check_item_name"], "膜平面形貌")

    def test_context_reports_task_snapshot_not_ready_instead_of_project_missing(self):
        context = self._context()
        context.input_data.pop("task")
        with patch(
            "app.execution.microscopy_original_record.cached_task_snapshot",
            return_value={"cache_state": "pending", "snapshot": None},
        ):
            with self.assertRaises(ExecutionApiError) as raised:
                _microscopy_record_context_executor(context)

        self.assertEqual(
            raised.exception.code,
            "microscopy_task_snapshot_not_ready",
        )

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


@unittest.skipUnless(
    os.getenv("EXECUTION_RUN_UNO_INTEGRATION_TESTS") == "1",
    "set EXECUTION_RUN_UNO_INTEGRATION_TESTS=1 inside the worker image",
)
class MicroscopyOriginalRecordUnoIntegrationTests(unittest.TestCase):
    def test_real_xls_reopen_validates_non_square_1_2_5_and_10_images(self):
        source_sizes = [
            (1280, 960),
            (800, 400),
            (400, 800),
            (1600, 1000),
            (750, 1000),
            (1250, 1000),
            (800, 1000),
            (1800, 1000),
            (600, 1000),
            (1100, 1000),
        ]
        cells = {
            "B2": "UNO-INTEGRATION",
            "B3": "非方形图片验证",
            "L3": "正面",
            "B33": "",
            "I33": "",
            "B34": "",
            "I34": "",
            "B35": "",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_paths = []
            for index, size in enumerate(source_sizes):
                path = root / f"source-{index}.png"
                Image.new("RGB", size, "white").save(path)
                image_paths.append(path)

            for count in (1, 2, 5, 10):
                workbook_path = root / f"working-{count}.xls"
                shutil.copyfile(_template_path(), workbook_path)
                ratios = [width / height for width, height in source_sizes[:count]]
                placements = layout_images(ratios)
                payload_images = [
                    {
                        "path": str(image_paths[index]),
                        "source_id": f"source-{index}",
                        "aspect_ratio": ratios[index],
                        **placement.as_dict(),
                    }
                    for index, placement in enumerate(placements)
                ]
                payload = {
                    "workbook_path": str(workbook_path),
                    "sheet_name": MICROSCOPY_SHEET_NAME,
                    "print_area": MICROSCOPY_PRINT_AREA,
                    "cells": cells,
                    "canvas": {
                        "range": "A4:L32",
                        "max_width": CANVAS_WIDTH,
                        "max_height": CANVAS_HEIGHT,
                        "biff_excel_x_scale": MICROSCOPY_BIFF_EXCEL_X_SCALE,
                    },
                    "images": payload_images,
                }
                payload_path = root / f"payload-{count}.json"
                payload_path.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
                uno_result = _run_uno_writer(payload_path)
                verification = _verify_generated_workbook(
                    workbook_path,
                    expected_cells=cells,
                    expected_images=payload_images,
                    uno_result=uno_result,
                )
                self.assertTrue(verification["all_images_geometry_verified"])
                self.assertTrue(
                    verification["multi_image_non_overlap_verified"]
                )
                names = xlrd.open_workbook(
                    str(workbook_path), formatting_info=True
                ).name_obj_list
                print_area_names = [
                    name for name in names if name.name == "Print_Area"
                ]
                self.assertEqual(len(print_area_names), 1)
                self.assertEqual(print_area_names[0].builtin, 1)


if __name__ == "__main__":
    unittest.main()
