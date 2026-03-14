from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest

from PIL import Image

from app.config import settings
from app.services.area_infer import AreaImageInferenceResult, AreaInstance, parse_model_classes
from app.services.area_jobs import AreaJobManager


class _FakePredictor:
    def predict(
        self,
        image_path: Path,
        model_name: str,
        weight_path: Path,
        inference_options: dict | None = None,
    ):
        overlay = Image.open(image_path).convert("RGB")
        return AreaImageInferenceResult(
            image_name=image_path.name,
            total_area_px=120,
            per_class_area_px={model_name.split("-")[0]: 120},
            instances=[AreaInstance(class_name=model_name.split("-")[0], area_px=120, bbox=(1, 1, 5, 5))],
            overlay_image=overlay,
        )


class _ErrorPredictor:
    def predict(
        self,
        image_path: Path,
        model_name: str,
        weight_path: Path,
        inference_options: dict | None = None,
    ):
        raise RuntimeError("mock_error")


class AreaJobsTests(unittest.TestCase):
    def test_parse_model_classes_alias(self):
        self.assertEqual(parse_model_classes("棉-粘-莱-莫"), ["棉", "粘纤", "莱赛尔", "莫代尔"])

    def test_job_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "root"
            root.mkdir(parents=True, exist_ok=True)
            target = root / "sample-001"
            target.mkdir(parents=True, exist_ok=True)
            img_path = target / "a.png"
            Image.new("RGB", (48, 48), color=(255, 255, 255)).save(img_path)

            weights_dir = Path(tmpdir) / "weights"
            weights_dir.mkdir(parents=True, exist_ok=True)
            (weights_dir / "b_c1_1.3.pth").write_bytes(b"mock")

            old_output = settings.AREA_OUTPUT_DIR
            try:
                settings.AREA_OUTPUT_DIR = str(Path(tmpdir) / "out")
                manager = AreaJobManager()
                manager._predictor = _FakePredictor()
                job = manager.create_job(
                    folder_name="sample-001",
                    model_name="棉-莱赛尔",
                    root_path=str(root),
                    model_mapping={"棉-莱赛尔": "b_c1_1.3.pth"},
                    weights_dir=str(weights_dir),
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
                excel_path = manager.get_excel_path(job["job_id"])
                self.assertIsNotNone(excel_path)
                self.assertTrue(excel_path.exists())
                manager.stop()
            finally:
                settings.AREA_OUTPUT_DIR = old_output

    def test_job_all_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "root"
            root.mkdir(parents=True, exist_ok=True)
            target = root / "sample-err"
            target.mkdir(parents=True, exist_ok=True)
            img_path = target / "a.jpg"
            Image.new("RGB", (32, 32), color=(128, 128, 128)).save(img_path)

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
        with self.assertRaises(ValueError):
            manager.create_job(
                folder_name="../bad",
                model_name="棉-莫代尔",
                root_path="/tmp",
                model_mapping={"棉-莫代尔": "b_cm_1.3.pth"},
                weights_dir="/tmp",
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
            with self.assertRaises(ValueError) as ctx:
                manager.create_job(
                    folder_name="not-exists",
                    model_name="棉-莱赛尔",
                    root_path=str(root),
                    model_mapping={"棉-莱赛尔": "b_c1_1.3.pth"},
                    weights_dir=str(weights_dir),
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


if __name__ == "__main__":
    unittest.main()
