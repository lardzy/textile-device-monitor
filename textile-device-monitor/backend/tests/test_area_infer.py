from __future__ import annotations

import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import requests

from app.services.area_infer import AreaPredictor, DEFAULT_INFER_OPTIONS


class AreaInferTests(unittest.TestCase):
    def test_default_options_include_legacy_named_fields(self):
        self.assertIn("score_threshold", DEFAULT_INFER_OPTIONS)
        self.assertIn("top_k", DEFAULT_INFER_OPTIONS)
        self.assertIn("nms_top_k", DEFAULT_INFER_OPTIONS)
        self.assertIn("nms_conf_thresh", DEFAULT_INFER_OPTIONS)
        self.assertIn("nms_thresh", DEFAULT_INFER_OPTIONS)

    def test_health_timeout_maps_to_infer_timeout(self):
        predictor = AreaPredictor(infer_url="http://area-infer:9001", timeout_sec=5)
        with patch("app.services.area_infer.requests.get", side_effect=requests.Timeout):
            with self.assertRaises(RuntimeError) as ctx:
                predictor.check_service_health()
        self.assertEqual(str(ctx.exception), "infer_timeout")

    def test_predict_parses_response(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "sample.png"
            weight_path = Path(tmpdir) / "mock.pth"
            Image.new("RGB", (32, 24), color=(250, 250, 250)).save(image_path)
            weight_path.write_bytes(b"mock")

            raw_overlay = image_path.read_bytes()
            overlay_b64 = base64.b64encode(raw_overlay).decode("utf-8")

            class _Resp:
                status_code = 200

                def json(self):
                    return {
                        "instances": [
                            {
                                "class_name": "棉",
                                "score": 0.93,
                                "bbox": [1, 2, 16, 20],
                                "area_px": 120,
                            }
                        ],
                        "per_class_area_px": {"棉": 120, "莱赛尔": 0},
                        "overlay_png_b64": overlay_b64,
                        "engine_meta": {"engine": "linux_native_yolact"},
                    }

            predictor = AreaPredictor(infer_url="http://area-infer:9001", timeout_sec=10)
            with patch("app.services.area_infer.requests.post", return_value=_Resp()):
                result = predictor.predict(
                    image_path=image_path,
                    model_name="棉-莱赛尔",
                    weight_path=weight_path,
                    inference_options={"score_threshold": 0.2},
                )

            self.assertEqual(result.total_area_px, 120)
            self.assertEqual(result.per_class_area_px.get("棉"), 120)
            self.assertEqual(len(result.instances), 1)
            self.assertEqual(result.instances[0].class_name, "棉")
            self.assertEqual(result.engine_meta.get("engine"), "linux_native_yolact")


if __name__ == "__main__":
    unittest.main()
