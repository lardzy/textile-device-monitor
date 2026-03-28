from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest

from PIL import Image
import xlrd
import xlwt

from app.config import settings
from app.services.area_infer import AreaImageInferenceResult, AreaInstance, parse_model_classes
from app.services.area_jobs import AreaJobManager


class _FakePredictor:
    def check_service_health(self, *, infer_url: str | None = None, timeout_sec: int | None = None):
        return {"status": "ok", "infer_url": infer_url, "timeout_sec": timeout_sec}

    def warmup_model(
        self,
        *,
        model_name: str,
        model_file: str,
        infer_url: str | None = None,
        timeout_sec: int | None = None,
    ):
        return {"status": "ok", "model_name": model_name, "model_file": model_file}

    def predict(
        self,
        image_path: Path,
        model_name: str,
        weight_path: Path,
        inference_options: dict | None = None,
        *,
        infer_url: str | None = None,
        timeout_sec: int | None = None,
        model_file: str | None = None,
    ):
        overlay = Image.open(image_path).convert("RGB")
        class_name = parse_model_classes(model_name)[0]
        return AreaImageInferenceResult(
            image_name=image_path.name,
            total_area_px=120,
            per_class_area_px={class_name: 120},
            instances=[AreaInstance(class_name=class_name, area_px=120, bbox=(1, 1, 5, 5))],
            overlay_image=overlay,
            engine_meta={"engine": "mock-native"},
        )


class _ErrorPredictor(_FakePredictor):
    def predict(
        self,
        image_path: Path,
        model_name: str,
        weight_path: Path,
        inference_options: dict | None = None,
        *,
        infer_url: str | None = None,
        timeout_sec: int | None = None,
        model_file: str | None = None,
    ):
        raise RuntimeError("mock_error")


class _ServiceDownPredictor(_FakePredictor):
    def check_service_health(self, *, infer_url: str | None = None, timeout_sec: int | None = None):
        raise RuntimeError("infer_service_unavailable")


class _HugePredictor(_FakePredictor):
    def predict(
        self,
        image_path: Path,
        model_name: str,
        weight_path: Path,
        inference_options: dict | None = None,
        *,
        infer_url: str | None = None,
        timeout_sec: int | None = None,
        model_file: str | None = None,
    ):
        overlay = Image.open(image_path).convert("RGB")
        class_name = parse_model_classes(model_name)[0]
        instances = [
            AreaInstance(class_name=class_name, area_px=1, bbox=(0, 0, 1, 1))
            for _ in range(2991)
        ]
        return AreaImageInferenceResult(
            image_name=image_path.name,
            total_area_px=2991,
            per_class_area_px={class_name: 2991},
            instances=instances,
            overlay_image=overlay,
            engine_meta={"engine": "mock-native"},
        )


def _create_xls_template(path: Path) -> None:
    wb = xlwt.Workbook()
    wb.add_sheet("原始数据")
    wb.add_sheet("截面统计报告1")
    wb.save(str(path))


def _excel_col_to_index(col: str) -> int:
    value = 0
    for ch in col.upper():
        value = value * 26 + (ord(ch) - ord("A") + 1)
    return value - 1


class AreaJobsTests(unittest.TestCase):
    def test_parse_model_classes_alias(self):
        self.assertEqual(parse_model_classes("棉-粘-莱-莫"), ["棉", "粘纤", "莱赛尔", "莫代尔"])

    def test_job_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "root"
            root.mkdir(parents=True, exist_ok=True)
            target = root / "sample-001"
            target.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (48, 48), color=(255, 255, 255)).save(target / "a.png")

            weights_dir = Path(tmpdir) / "weights"
            weights_dir.mkdir(parents=True, exist_ok=True)
            (weights_dir / "b_c1_1.3.pth").write_bytes(b"mock")
            template_path = Path(tmpdir) / "template.xls"
            _create_xls_template(template_path)

            old_output = settings.AREA_OUTPUT_DIR
            old_template = settings.AREA_EXCEL_TEMPLATE_PATH
            try:
                settings.AREA_OUTPUT_DIR = str(Path(tmpdir) / "out")
                settings.AREA_EXCEL_TEMPLATE_PATH = str(template_path)
                manager = AreaJobManager()
                manager._predictor = _FakePredictor()
                job = manager.create_job(
                    folder_name="sample-001",
                    model_name="棉-莱赛尔",
                    root_path=str(root),
                    model_mapping={"棉-莱赛尔": "b_c1_1.3.pth"},
                    weights_dir=str(weights_dir),
                    output_root=str(Path(tmpdir) / "out"),
                    infer_url="http://area-infer:9001",
                    infer_timeout_sec=20,
                )
                for _ in range(50):
                    payload = manager.get_job(job["job_id"])
                    if payload and payload["status"] not in {"queued", "running"}:
                        break
                    time.sleep(0.1)
                payload = manager.get_job(job["job_id"])
                self.assertIsNotNone(payload)
                self.assertIn(payload["status"], {"succeeded", "succeeded_with_errors"})
                self.assertIn("score_threshold", payload["inference_options"])
                self.assertIn("top_k", payload["inference_options"])
                self.assertIn("nms_top_k", payload["inference_options"])
                self.assertIn("nms_conf_thresh", payload["inference_options"])
                self.assertIn("nms_thresh", payload["inference_options"])
                result = manager.get_result(job["job_id"])
                self.assertIsNotNone(result)
                self.assertEqual((result or {}).get("engine_meta", {}).get("engine"), "mock-native")
                excel_path = manager.get_excel_path(job["job_id"])
                self.assertIsNotNone(excel_path)
                self.assertTrue(excel_path.exists())
                self.assertEqual(excel_path.suffix.lower(), ".xls")
                self.assertEqual(excel_path.name, "sample-001.xls")
                self.assertTrue(excel_path.parent.name.startswith("sample-001_"))

                book = xlrd.open_workbook(str(excel_path))
                sheet_raw = book.sheet_by_name("原始数据")
                sheet_report = book.sheet_by_name("截面统计报告1")
                col_ba = _excel_col_to_index("BA")
                col_bb = _excel_col_to_index("BB")
                col_n = _excel_col_to_index("N")
                col_o = _excel_col_to_index("O")
                self.assertEqual(sheet_raw.cell_value(8, col_ba), "sample-001")
                self.assertEqual(sheet_raw.cell_value(10, col_ba), "棉")
                self.assertEqual(sheet_raw.cell_value(10, col_bb), "莱赛尔")
                self.assertEqual(int(sheet_raw.cell_value(10, col_n)), 1)
                self.assertEqual(int(sheet_raw.cell_value(10, col_o)), 120)
                self.assertEqual(sheet_report.cell_value(7, _excel_col_to_index("F")), "sample-001")
                manager.stop()
            finally:
                settings.AREA_OUTPUT_DIR = old_output
                settings.AREA_EXCEL_TEMPLATE_PATH = old_template

    def test_job_all_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "root"
            root.mkdir(parents=True, exist_ok=True)
            target = root / "sample-err"
            target.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (32, 32), color=(128, 128, 128)).save(target / "a.jpg")

            weights_dir = Path(tmpdir) / "weights"
            weights_dir.mkdir(parents=True, exist_ok=True)
            (weights_dir / "b_v1_1.3.pth").write_bytes(b"mock")

            old_output = settings.AREA_OUTPUT_DIR
            try:
                settings.AREA_OUTPUT_DIR = str(Path(tmpdir) / "out")
                manager = AreaJobManager()
                manager._predictor = _ErrorPredictor()
                job = manager.create_job(
                    folder_name="sample-err",
                    model_name="粘纤-莱赛尔",
                    root_path=str(root),
                    model_mapping={"粘纤-莱赛尔": "b_v1_1.3.pth"},
                    weights_dir=str(weights_dir),
                    output_root=str(Path(tmpdir) / "out"),
                    infer_url="http://area-infer:9001",
                    infer_timeout_sec=20,
                )
                for _ in range(50):
                    payload = manager.get_job(job["job_id"])
                    if payload and payload["status"] not in {"queued", "running"}:
                        break
                    time.sleep(0.1)
                payload = manager.get_job(job["job_id"])
                self.assertIsNotNone(payload)
                self.assertEqual(payload["status"], "failed")
                manager.stop()
            finally:
                settings.AREA_OUTPUT_DIR = old_output

    def test_invalid_folder_name(self):
        manager = AreaJobManager()
        manager._predictor = _FakePredictor()
        with self.assertRaises(ValueError):
            manager.create_job(
                folder_name="../bad",
                model_name="棉-莫代尔",
                root_path="/tmp",
                model_mapping={"棉-莫代尔": "b_cm_1.3.pth"},
                weights_dir="/tmp",
                output_root="/tmp",
                infer_url="http://area-infer:9001",
                infer_timeout_sec=20,
            )
        manager.stop()

    def test_folder_not_found_rejected_before_enqueue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "root"
            root.mkdir(parents=True, exist_ok=True)
            weights_dir = Path(tmpdir) / "weights"
            weights_dir.mkdir(parents=True, exist_ok=True)
            (weights_dir / "b_c1_1.3.pth").write_bytes(b"mock")

            manager = AreaJobManager()
            manager._predictor = _FakePredictor()
            with self.assertRaises(ValueError) as ctx:
                manager.create_job(
                    folder_name="not-exists",
                    model_name="棉-莱赛尔",
                    root_path=str(root),
                    model_mapping={"棉-莱赛尔": "b_c1_1.3.pth"},
                    weights_dir=str(weights_dir),
                    output_root=str(Path(tmpdir) / "out"),
                    infer_url="http://area-infer:9001",
                    infer_timeout_sec=20,
                )
            self.assertEqual(str(ctx.exception), "folder_not_found")
            manager.stop()

    def test_root_path_backslash_style_supported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "root"
            target = root / "sample"
            target.mkdir(parents=True, exist_ok=True)

            manager = AreaJobManager()
            root_backslash = str(root).replace("/", "\\")
            resolved = manager._resolve_target_folder(root_backslash, "sample")
            self.assertEqual(resolved.name, "sample")
            manager.stop()

    def test_cleanup_folder_keeps_i_suffix_images(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "root"
            target = root / "sample"
            target.mkdir(parents=True, exist_ok=True)
            (target / "keep_I.jpg").write_bytes(b"x")
            (target / "keep_i.jpeg").write_bytes(b"x")
            (target / "move_a.jpg").write_bytes(b"x")
            (target / "move_b.png").write_bytes(b"x")
            (target / "note.txt").write_text("keep", encoding="utf-8")

            manager = AreaJobManager()
            try:
                payload = manager.cleanup_folder(
                    root_path=str(root),
                    folder_name="sample",
                    rename_enabled=False,
                    new_folder_name=None,
                )
                self.assertEqual(payload.get("moved"), 2)
                self.assertTrue((target / "keep_I.jpg").exists())
                self.assertTrue((target / "keep_i.jpeg").exists())
                self.assertTrue((target / "note.txt").exists())
                self.assertFalse((target / "move_a.jpg").exists())
                self.assertFalse((target / "move_b.png").exists())

                recycle = root / ".recycle"
                self.assertTrue((recycle / "move_a.jpg").exists())
                self.assertTrue((recycle / "move_b.png").exists())
            finally:
                manager.stop()

    def test_run_archive_skips_nested_old_root_branch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "root"
            root.mkdir(parents=True, exist_ok=True)
            old_root = root / "旧"
            old_root.mkdir(parents=True, exist_ok=True)
            archived = root / "sample-old"
            archived.mkdir(parents=True, exist_ok=True)
            archived_file = archived / "a.jpg"
            archived_file.write_bytes(b"x")
            kept_in_old = old_root / "already-there"
            kept_in_old.mkdir(parents=True, exist_ok=True)
            nested_file = kept_in_old / "keep.txt"
            nested_file.write_text("keep", encoding="utf-8")

            stale_time = time.time() - 60 * 60 * 48
            os.utime(archived, (stale_time, stale_time))
            os.utime(old_root, (stale_time, stale_time))

            manager = AreaJobManager()
            try:
                result = manager.run_archive(
                    root_path=str(root),
                    old_root_path=str(old_root),
                    older_than_hours=24,
                )
                self.assertEqual(result["moved_count"], 1)
                self.assertEqual(result["failed_count"], 0)
                self.assertFalse(archived.exists())
                self.assertTrue((old_root / "sample-old" / "a.jpg").exists())
                self.assertTrue(old_root.exists())
                self.assertTrue(nested_file.exists())
            finally:
                manager.stop()

    def test_inference_option_new_fields_validated(self):
        manager = AreaJobManager()
        valid = manager._normalize_inference_options(
            {
                "score_threshold": 0.2,
                "top_k": 100,
                "nms_top_k": 120,
                "nms_conf_thresh": 0.1,
                "nms_thresh": 0.6,
            }
        )
        self.assertEqual(valid["score_threshold"], 0.2)
        self.assertEqual(valid["top_k"], 100)
        self.assertEqual(valid["nms_top_k"], 120)
        self.assertEqual(valid["nms_conf_thresh"], 0.1)
        self.assertEqual(valid["nms_thresh"], 0.6)

        invalid_cases = [
            {"score_threshold": -0.1},
            {"score_threshold": 1.1},
            {"top_k": 0},
            {"top_k": 1001},
            {"nms_top_k": 0},
            {"nms_top_k": 1001},
            {"nms_conf_thresh": -0.1},
            {"nms_conf_thresh": 1.1},
            {"nms_thresh": -0.1},
            {"nms_thresh": 1.1},
        ]
        for case in invalid_cases:
            with self.assertRaises(ValueError) as ctx:
                manager._normalize_inference_options(case)
            self.assertEqual(str(ctx.exception), "invalid_inference_options")
        manager.stop()

    def test_infer_precheck_failure_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "root"
            root.mkdir(parents=True, exist_ok=True)
            target = root / "sample-001"
            target.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (32, 32), color=(255, 255, 255)).save(target / "a.png")

            weights_dir = Path(tmpdir) / "weights"
            weights_dir.mkdir(parents=True, exist_ok=True)
            (weights_dir / "b_c1_1.3.pth").write_bytes(b"mock")

            manager = AreaJobManager()
            manager._predictor = _ServiceDownPredictor()
            with self.assertRaises(ValueError) as ctx:
                manager.create_job(
                    folder_name="sample-001",
                    model_name="棉-莱赛尔",
                    root_path=str(root),
                    model_mapping={"棉-莱赛尔": "b_c1_1.3.pth"},
                    weights_dir=str(weights_dir),
                    output_root=str(Path(tmpdir) / "out"),
                    infer_url="http://area-infer:9001",
                    infer_timeout_sec=20,
                )
            self.assertEqual(str(ctx.exception), "infer_service_unavailable")
            manager.stop()

    def test_excel_capacity_exceeded_marks_job_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "root"
            root.mkdir(parents=True, exist_ok=True)
            target = root / "sample-big"
            target.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (16, 16), color=(255, 255, 255)).save(target / "a.png")

            weights_dir = Path(tmpdir) / "weights"
            weights_dir.mkdir(parents=True, exist_ok=True)
            (weights_dir / "b_c1_1.3.pth").write_bytes(b"mock")

            template_path = Path(tmpdir) / "template.xls"
            _create_xls_template(template_path)

            old_output = settings.AREA_OUTPUT_DIR
            old_template = settings.AREA_EXCEL_TEMPLATE_PATH
            try:
                settings.AREA_OUTPUT_DIR = str(Path(tmpdir) / "out")
                settings.AREA_EXCEL_TEMPLATE_PATH = str(template_path)
                manager = AreaJobManager()
                manager._predictor = _HugePredictor()
                job = manager.create_job(
                    folder_name="sample-big",
                    model_name="棉-莱赛尔",
                    root_path=str(root),
                    model_mapping={"棉-莱赛尔": "b_c1_1.3.pth"},
                    weights_dir=str(weights_dir),
                    output_root=str(Path(tmpdir) / "out"),
                    infer_url="http://area-infer:9001",
                    infer_timeout_sec=20,
                )
                for _ in range(60):
                    payload = manager.get_job(job["job_id"])
                    if payload and payload["status"] not in {"queued", "running"}:
                        break
                    time.sleep(0.1)
                payload = manager.get_job(job["job_id"])
                self.assertIsNotNone(payload)
                self.assertEqual(payload["status"], "failed")
                self.assertEqual(payload["error_code"], "excel_template_capacity_exceeded")
                manager.stop()
            finally:
                settings.AREA_OUTPUT_DIR = old_output
                settings.AREA_EXCEL_TEMPLATE_PATH = old_template


if __name__ == "__main__":
    unittest.main()
