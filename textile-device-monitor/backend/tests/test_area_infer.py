from __future__ import annotations

import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import requests

from app.services.area_infer import (
    AreaPredictor,
    DEFAULT_INFER_OPTIONS,
    canonicalize_infer_class_name,
    requires_legacy_class_remap,
)


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

    def test_warmup_requests_canonical_class_mapping(self):
        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok"}

        predictor = AreaPredictor(infer_url="http://area-infer:9001", timeout_sec=5)
        with patch("app.services.area_infer.requests.post", return_value=_Resp()) as post:
            predictor.warmup_model(
                model_name="粘纤-莱赛尔",
                model_file="b_v1_1.3.pth",
            )

        self.assertEqual(post.call_args.kwargs["json"]["class_mapping_version"], 1)

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
                        "engine_meta": {
                            "engine": "linux_native_yolact",
                            "class_mapping": "trusted_metadata_v1",
                        },
                    }

            predictor = AreaPredictor(infer_url="http://area-infer:9001", timeout_sec=10)
            with patch("app.services.area_infer.requests.post", return_value=_Resp()) as post:
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
            self.assertEqual(result.engine_meta.get("class_mapping"), "trusted_metadata_v1")
            self.assertEqual(post.call_args.kwargs["json"]["class_mapping_version"], 1)

    def test_predict_canonicalizes_legacy_reversed_response_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "sample.png"
            weight_path = Path(tmpdir) / "b_v1_1.3.pth"
            Image.new("RGB", (32, 24), color=(250, 250, 250)).save(image_path)
            weight_path.write_bytes(b"mock")
            overlay_b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")

            class _Resp:
                status_code = 200

                def json(self):
                    return {
                        "instances": [
                            {
                                # Legacy area-infer called actual Viscose index 1
                                # "莱赛尔" because it used display-name order.
                                "class_name": "莱赛尔",
                                "score": 0.91,
                                "bbox": [1, 2, 16, 20],
                                "area_px": 120,
                            }
                        ],
                        "per_class_area_px": {"粘纤": 0, "莱赛尔": 120},
                        "overlay_png_b64": overlay_b64,
                        "engine_meta": {"engine": "legacy-linux-native-yolact"},
                    }

            predictor = AreaPredictor(infer_url="http://area-infer:9001", timeout_sec=10)
            with patch("app.services.area_infer.requests.post", return_value=_Resp()):
                result = predictor.predict(
                    image_path=image_path,
                    model_name="粘纤-莱赛尔",
                    weight_path=weight_path,
                    model_file=weight_path.name,
                )

            self.assertEqual(result.instances[0].class_name, "粘纤")
            self.assertEqual(result.per_class_area_px["粘纤"], 120)
            self.assertEqual(result.per_class_area_px["莱赛尔"], 0)
            self.assertEqual(
                result.engine_meta.get("class_mapping"),
                "canonicalized_by_backend_v1",
            )
            self.assertTrue(result.engine_meta.get("legacy_class_remap_applied"))

    def test_legacy_mapping_covers_both_reversed_binary_models(self):
        cases = [
            ("粘纤-莱赛尔", "莱赛尔", "粘纤"),
            ("粘纤-莱赛尔", "粘纤", "莱赛尔"),
            ("棉-莱赛尔", "莱赛尔", "棉"),
            ("棉-莱赛尔", "棉", "莱赛尔"),
        ]
        for model_name, raw_class, semantic_class in cases:
            with self.subTest(model_name=model_name, raw_class=raw_class):
                self.assertEqual(
                    canonicalize_infer_class_name(
                        model_name,
                        raw_class,
                        engine_meta={},
                        model_file=(
                            "b_v1_1.3.pth"
                            if model_name == "粘纤-莱赛尔"
                            else "b_c1_1.3.pth"
                        ),
                    ),
                    semantic_class,
                )

    def test_legacy_mapping_uses_weight_identity_and_supports_alias_names(self):
        self.assertTrue(
            requires_legacy_class_remap("粘-莱", "b_v1_1.3.pth")
        )
        self.assertEqual(
            canonicalize_infer_class_name(
                "粘-莱",
                "莱赛尔",
                engine_meta={},
                model_file="b_v1_1.3.pth",
            ),
            "粘纤",
        )
        self.assertFalse(
            requires_legacy_class_remap("粘纤-莱赛尔", "custom.pth")
        )
        self.assertEqual(
            canonicalize_infer_class_name(
                "粘纤-莱赛尔",
                "莱赛尔",
                engine_meta={},
                model_file="custom.pth",
            ),
            "莱赛尔",
        )
        self.assertEqual(
            canonicalize_infer_class_name(
                "粘纤-莱赛尔",
                "莱赛尔",
                engine_meta={"class_mapping": "caller_display_order_v1"},
                model_file="b_v1_1.3.pth",
            ),
            "莱赛尔",
        )

    def test_predict_does_not_double_swap_trusted_response(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "sample.png"
            weight_path = Path(tmpdir) / "b_v1_1.3.pth"
            Image.new("RGB", (32, 24), color=(250, 250, 250)).save(image_path)
            weight_path.write_bytes(b"mock")
            overlay_b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")

            class _Resp:
                status_code = 200

                def json(self):
                    return {
                        "instances": [
                            {
                                "class_name": "粘纤",
                                "score": 0.91,
                                "bbox": [1, 2, 16, 20],
                                "area_px": 120,
                            }
                        ],
                        "per_class_area_px": {"粘纤": 120, "莱赛尔": 0},
                        "overlay_png_b64": overlay_b64,
                        "engine_meta": {
                            "engine": "linux_native_yolact",
                            "class_mapping": "trusted_metadata_v1",
                        },
                    }

            predictor = AreaPredictor(infer_url="http://area-infer:9001", timeout_sec=10)
            with patch("app.services.area_infer.requests.post", return_value=_Resp()):
                result = predictor.predict(
                    image_path=image_path,
                    model_name="粘纤-莱赛尔",
                    weight_path=weight_path,
                    model_file=weight_path.name,
                )

            self.assertEqual(result.instances[0].class_name, "粘纤")
            self.assertEqual(result.per_class_area_px["粘纤"], 120)
            self.assertFalse(result.engine_meta.get("legacy_class_remap_applied", False))


if __name__ == "__main__":
    unittest.main()
