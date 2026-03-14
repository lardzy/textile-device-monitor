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
    def predict(self, image_path: Path, model_name: str, weight_path: Path):
        overlay = Image.open(image_path).convert("RGB")
        return AreaImageInferenceResult(
            image_name=image_path.name,
            total_area_px=120,
            per_class_area_px={model_name.split("-")[0]: 120},
            instances=[AreaInstance(class_name=model_name.split("-")[0], area_px=120, bbox=(1, 1, 5, 5))],
            overlay_image=overlay,
        )


class _ErrorPredictor:
    def predict(self, image_path: Path, model_name: str, weight_path: Path):
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


if __name__ == "__main__":
    unittest.main()
