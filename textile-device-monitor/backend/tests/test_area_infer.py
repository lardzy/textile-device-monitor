from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw

from app.services.area_infer import AreaPredictor, DEFAULT_INFER_OPTIONS


class AreaInferTests(unittest.TestCase):
    def _build_test_image(self, path: Path) -> None:
        image = Image.new("RGB", (220, 140), color=(245, 245, 245))
        draw = ImageDraw.Draw(image)
        # Draw multiple separated dark regions to guarantee >2 instances.
        blocks = [
            (10, 12, 42, 44),
            (56, 16, 92, 52),
            (108, 18, 144, 54),
            (18, 74, 54, 112),
            (74, 72, 112, 114),
            (138, 70, 176, 116),
        ]
        for box in blocks:
            draw.rectangle(box, fill=(20, 20, 20))
        image.save(path)

    def test_default_options_include_legacy_named_fields(self):
        self.assertIn("score_threshold", DEFAULT_INFER_OPTIONS)
        self.assertIn("top_k", DEFAULT_INFER_OPTIONS)
        self.assertIn("nms_top_k", DEFAULT_INFER_OPTIONS)
        self.assertIn("nms_conf_thresh", DEFAULT_INFER_OPTIONS)
        self.assertIn("nms_thresh", DEFAULT_INFER_OPTIONS)

    def test_strict_options_do_not_increase_instances(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "sample.png"
            weight_path = Path(tmpdir) / "mock.pth"
            self._build_test_image(image_path)
            weight_path.write_bytes(b"mock")

            predictor = AreaPredictor()
            common = {
                "mask_mode": "dark",
                "threshold_bias": 10,
                "smooth_min_neighbors": 2,
                "min_pixels": 20,
                "overlay_alpha": 0.45,
            }

            loose = predictor.predict(
                image_path=image_path,
                model_name="棉-莱赛尔",
                weight_path=weight_path,
                inference_options={
                    **common,
                    "score_threshold": 0.0,
                    "top_k": 1000,
                    "nms_top_k": 1000,
                    "nms_conf_thresh": 0.0,
                    "nms_thresh": 1.0,
                },
            )
            strict = predictor.predict(
                image_path=image_path,
                model_name="棉-莱赛尔",
                weight_path=weight_path,
                inference_options={
                    **common,
                    "score_threshold": 0.35,
                    "top_k": 1,
                    "nms_top_k": 1,
                    "nms_conf_thresh": 0.2,
                    "nms_thresh": 0.3,
                },
            )

            self.assertGreater(len(loose.instances), 1)
            self.assertLessEqual(len(strict.instances), len(loose.instances))
            self.assertLess(len(strict.instances), len(loose.instances))
            self.assertLessEqual(strict.total_area_px, loose.total_area_px)


if __name__ == "__main__":
    unittest.main()
