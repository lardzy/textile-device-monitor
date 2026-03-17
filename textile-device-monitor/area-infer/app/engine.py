from __future__ import annotations

import base64
import io
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image


PALETTE: list[tuple[int, int, int]] = [
    (255, 87, 34),
    (30, 136, 229),
    (67, 160, 71),
    (142, 36, 170),
    (255, 179, 0),
    (0, 172, 193),
    (94, 53, 177),
    (216, 27, 96),
]

LABEL_ALIAS: dict[str, str] = {
    "粘": "粘纤",
    "莱": "莱赛尔",
    "莫": "莫代尔",
}


class InferServiceError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def parse_model_classes(model_name: str) -> list[str]:
    classes: list[str] = []
    for item in str(model_name or "").split("-"):
        token = item.strip()
        if not token:
            continue
        classes.append(LABEL_ALIAS.get(token, token))

    deduped: list[str] = []
    for item in classes:
        if item not in deduped:
            deduped.append(item)
    return deduped or ["未分类"]


def remap_class_index_for_model(model_name: str, class_index: int, class_count: int) -> int:
    normalized_model_name = str(model_name or "").replace(" ", "")
    # Legacy weight b_v1_1.3 has class index order opposite to model display name.
    if normalized_model_name == "粘纤-莱赛尔" and class_count >= 2:
        if class_index == 0:
            return 1
        if class_index == 1:
            return 0
    return class_index


@dataclass
class _ModelRuntime:
    model_name: str
    model_file: str
    class_names: tuple[str, ...]
    cfg_name: str
    cfg_obj: Any
    net: Any
    loaded_at: float


class AreaNativeEngine:
    def __init__(self, *, weights_dir: str, vendor_root: str, default_cfg_name: str = "yolact_base_config") -> None:
        self.weights_dir = Path(weights_dir).resolve()
        self.vendor_root = Path(vendor_root).resolve()
        self.default_cfg_name = default_cfg_name
        self._lock = threading.RLock()
        self._runtime_loaded = False
        self._cache: dict[tuple[str, tuple[str, ...]], _ModelRuntime] = {}

        self._torch = None
        self._cfg = None
        self._yolact_cls = None
        self._fast_transform_cls = None
        self._postprocess_fn = None

    def _ensure_runtime(self) -> None:
        if self._runtime_loaded:
            return

        if not self.vendor_root.exists():
            raise InferServiceError("infer_service_unavailable", f"vendor_root_not_found:{self.vendor_root}")

        vendor_str = str(self.vendor_root)
        if vendor_str not in sys.path:
            sys.path.insert(0, vendor_str)

        try:
            import torch
            from data import cfg
            from data.config import yolact_base_config
            from yolact import Yolact
            from utils.augmentations import FastBaseTransform
            from layers.output_utils import postprocess
        except Exception as exc:
            raise InferServiceError("infer_service_unavailable", f"runtime_import_failed:{exc}") from exc

        self._torch = torch
        self._cfg = cfg
        self._yolact_cls = Yolact
        self._fast_transform_cls = FastBaseTransform
        self._postprocess_fn = postprocess
        self._yolact_base_config = yolact_base_config
        self._runtime_loaded = True

    def _build_cfg(self, class_names: list[str]) -> Any:
        dataset = self._yolact_base_config.dataset.copy(
            {
                "name": "TextileFiber",
                "class_names": tuple(class_names),
                "label_map": {idx + 1: idx + 1 for idx in range(len(class_names))},
            }
        )
        return self._yolact_base_config.copy(
            {
                "name": self.default_cfg_name.replace("_config", ""),
                "dataset": dataset,
                "num_classes": len(class_names) + 1,
            }
        )

    def _apply_cfg(self, cfg_obj: Any) -> None:
        self._cfg.replace(cfg_obj)
        self._cfg.name = getattr(cfg_obj, "name", None) or self.default_cfg_name.replace("_config", "")
        if not hasattr(self._cfg, "mask_proto_debug"):
            self._cfg.mask_proto_debug = False
        if not hasattr(self._cfg, "rescore_bbox"):
            self._cfg.rescore_bbox = False
        if not hasattr(self._cfg, "eval_mask_branch"):
            self._cfg.eval_mask_branch = True

    def _normalize_options(self, inference_options: dict[str, Any] | None) -> dict[str, Any]:
        options = dict(inference_options or {})
        normalized = {
            "score_threshold": float(options.get("score_threshold", 0.15) or 0.15),
            "top_k": int(options.get("top_k", 200) or 200),
            "nms_top_k": int(options.get("nms_top_k", 200) or 200),
            "nms_conf_thresh": float(options.get("nms_conf_thresh", 0.05) or 0.05),
            "nms_thresh": float(options.get("nms_thresh", 0.5) or 0.5),
            "overlay_alpha": float(options.get("overlay_alpha", 0.45) or 0.45),
        }
        normalized["score_threshold"] = max(0.0, min(1.0, normalized["score_threshold"]))
        normalized["nms_conf_thresh"] = max(0.0, min(1.0, normalized["nms_conf_thresh"]))
        normalized["nms_thresh"] = max(0.0, min(1.0, normalized["nms_thresh"]))
        normalized["top_k"] = max(1, min(1000, normalized["top_k"]))
        normalized["nms_top_k"] = max(1, min(1000, normalized["nms_top_k"]))
        normalized["overlay_alpha"] = max(0.05, min(0.95, normalized["overlay_alpha"]))
        return normalized

    def _decode_image(self, image_bytes_b64: str) -> np.ndarray:
        try:
            raw = base64.b64decode(image_bytes_b64)
            arr = np.frombuffer(raw, dtype=np.uint8)
            image_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        except Exception as exc:
            raise InferServiceError("infer_bad_response", f"invalid_image_bytes:{exc}") from exc

        if image_bgr is None:
            raise InferServiceError("infer_bad_response", "invalid_image_decode")
        return image_bgr

    def _mask_to_polygon(self, mask_bool: np.ndarray | None) -> list[list[int]]:
        if not isinstance(mask_bool, np.ndarray):
            return []
        if mask_bool.dtype != np.uint8:
            mask_u8 = mask_bool.astype(np.uint8) * 255
        else:
            mask_u8 = mask_bool
        if mask_u8.ndim != 2:
            return []
        contours, _ = cv2.findContours(mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return []
        largest = max(contours, key=cv2.contourArea)
        if largest is None or len(largest) < 3:
            return []
        perimeter = cv2.arcLength(largest, True)
        epsilon = max(1.0, 0.0035 * perimeter)
        approx = cv2.approxPolyDP(largest, epsilon, True)
        points = approx[:, 0, :] if approx.ndim == 3 and approx.shape[1] == 1 else approx
        polygon: list[list[int]] = []
        if not isinstance(points, np.ndarray):
            return []
        for p in points.tolist():
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                continue
            polygon.append([int(p[0]), int(p[1])])
        if len(polygon) < 3:
            return []
        return polygon

    def _resolve_weight_path(self, model_file: str) -> Path:
        model_key = Path(str(model_file or "").strip()).name
        if not model_key:
            raise InferServiceError("infer_model_load_failed", "invalid_model_file")
        path = self.weights_dir / model_key
        if not path.exists() or not path.is_file():
            raise InferServiceError("infer_model_load_failed", f"weight_not_found:{path}")
        return path

    def _model_cache_key(self, model_file: str, class_names: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
        return (Path(model_file).name, class_names)

    def _load_model(self, *, model_name: str, model_file: str) -> _ModelRuntime:
        self._ensure_runtime()

        classes = tuple(parse_model_classes(model_name))
        cache_key = self._model_cache_key(model_file=model_file, class_names=classes)
        if cache_key in self._cache:
            return self._cache[cache_key]

        weight_path = self._resolve_weight_path(model_file)
        cfg_obj = self._build_cfg(list(classes))
        self._apply_cfg(cfg_obj)

        try:
            net = self._yolact_cls()
            net.load_weights(str(weight_path))
            net.eval()
            net.detect.use_fast_nms = True
            net.detect.use_cross_class_nms = False
        except Exception as exc:
            raise InferServiceError("infer_model_load_failed", f"load_model_failed:{exc}") from exc

        runtime = _ModelRuntime(
            model_name=model_name,
            model_file=Path(model_file).name,
            class_names=classes,
            cfg_name=getattr(cfg_obj, "name", "yolact_base"),
            cfg_obj=cfg_obj,
            net=net,
            loaded_at=time.time(),
        )
        self._cache[cache_key] = runtime
        return runtime

    def health(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_runtime()
            return {
                "status": "ok",
                "weights_dir": str(self.weights_dir),
                "vendor_root": str(self.vendor_root),
                "cached_models": [
                    {
                        "model_file": item.model_file,
                        "class_names": list(item.class_names),
                        "cfg_name": item.cfg_name,
                        "loaded_at": item.loaded_at,
                    }
                    for item in self._cache.values()
                ],
                "runtime": {
                    "torch_version": getattr(self._torch, "__version__", "unknown"),
                },
            }

    def warmup(self, *, model_name: str, model_file: str) -> dict[str, Any]:
        with self._lock:
            runtime = self._load_model(model_name=model_name, model_file=model_file)
            return {
                "status": "ok",
                "model_file": runtime.model_file,
                "class_names": list(runtime.class_names),
                "cfg_name": runtime.cfg_name,
            }

    def _render_overlay(
        self,
        image_bgr: np.ndarray,
        instances: list[dict[str, Any]],
        class_names: tuple[str, ...],
        alpha: float,
    ) -> str:
        overlay = image_bgr.astype(np.float32).copy()
        class_to_color: dict[str, np.ndarray] = {
            name: np.array(PALETTE[idx % len(PALETTE)], dtype=np.float32)
            for idx, name in enumerate(class_names)
        }

        for item in instances:
            cls_name = str(item.get("class_name") or "未分类")
            mask = item.get("mask")
            if not isinstance(mask, np.ndarray):
                continue
            ys, xs = np.where(mask)
            if ys.size <= 0:
                continue
            color = class_to_color.get(cls_name, np.array(PALETTE[0], dtype=np.float32))
            overlay[ys, xs] = overlay[ys, xs] * (1.0 - alpha) + color * alpha

        out = np.clip(overlay, 0, 255).astype(np.uint8)

        class_code_map = {name: f"C{idx + 1}" for idx, name in enumerate(class_names)}
        for item in instances[:120]:
            x1, y1, x2, y2 = item.get("bbox", [0, 0, 0, 0])
            cls_name = str(item.get("class_name") or "未分类")
            color = class_to_color.get(cls_name, np.array(PALETTE[0], dtype=np.float32))
            cv_color = tuple(int(v) for v in color.tolist())
            cv2.rectangle(out, (int(x1), int(y1)), (int(x2), int(y2)), cv_color, 1)
            code = class_code_map.get(cls_name, "C1")
            cv2.putText(out, code, (int(x1) + 2, int(y1) + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)

        ok, encoded = cv2.imencode(".png", out)
        if not ok:
            raise InferServiceError("infer_bad_response", "overlay_encode_failed")
        return base64.b64encode(encoded.tobytes()).decode("utf-8")

    def infer(
        self,
        *,
        model_name: str,
        model_file: str,
        image_bytes_b64: str,
        inference_options: dict[str, Any] | None,
    ) -> dict[str, Any]:
        t0 = time.time()
        with self._lock:
            runtime = self._load_model(model_name=model_name, model_file=model_file)
            options = self._normalize_options(inference_options)
            self._apply_cfg(runtime.cfg_obj)

            self._cfg.nms_top_k = options["nms_top_k"]
            self._cfg.nms_conf_thresh = options["nms_conf_thresh"]
            self._cfg.nms_thresh = options["nms_thresh"]
            self._cfg.max_num_detections = max(1, int(options["top_k"]))

            net = runtime.net
            net.detect.top_k = int(options["nms_top_k"])
            net.detect.conf_thresh = float(options["nms_conf_thresh"])
            net.detect.nms_thresh = float(options["nms_thresh"])
            net.detect.use_fast_nms = True
            net.detect.use_cross_class_nms = False

            image_bgr = self._decode_image(image_bytes_b64)
            h, w = image_bgr.shape[:2]

            try:
                frame = self._torch.from_numpy(image_bgr).float().unsqueeze(0)
                batch = self._fast_transform_cls()(frame)
                with self._torch.no_grad():
                    preds = net(batch)
                classes_t, scores_t, boxes_t, masks_t = self._postprocess_fn(
                    preds,
                    w,
                    h,
                    score_threshold=float(options["score_threshold"]),
                    crop_masks=True,
                )
            except InferServiceError:
                raise
            except Exception as exc:
                raise InferServiceError("infer_bad_response", f"runtime_infer_failed:{exc}") from exc

            instances: list[dict[str, Any]] = []
            per_class_area_px: dict[str, int] = {name: 0 for name in runtime.class_names}

            if hasattr(scores_t, "numel") and int(scores_t.numel()) > 0:
                scores_np = scores_t.detach().cpu().numpy()
                order = np.argsort(-scores_np)
                order = order[: max(1, int(options["top_k"]))]

                classes_np = classes_t.detach().cpu().numpy()
                boxes_np = boxes_t.detach().cpu().numpy()
                masks_np = masks_t.detach().cpu().numpy()

                for i in order.tolist():
                    cls_idx = int(classes_np[i])
                    cls_idx = remap_class_index_for_model(runtime.model_name, cls_idx, len(runtime.class_names))
                    score = float(scores_np[i])
                    box = boxes_np[i].tolist()
                    if len(box) != 4:
                        continue
                    x1, y1, x2, y2 = [int(v) for v in box]

                    if 0 <= cls_idx < len(runtime.class_names):
                        cls_name = runtime.class_names[cls_idx]
                    else:
                        cls_name = "未分类"

                    raw_mask = masks_np[i]
                    mask_bool = (raw_mask > 0.5) if isinstance(raw_mask, np.ndarray) else None
                    area_px = int(mask_bool.sum()) if isinstance(mask_bool, np.ndarray) else max(0, (x2 - x1 + 1) * (y2 - y1 + 1))
                    polygon = self._mask_to_polygon(mask_bool)

                    per_class_area_px[cls_name] = per_class_area_px.get(cls_name, 0) + area_px
                    instances.append(
                        {
                            "class_name": cls_name,
                            "score": score,
                            "bbox": [x1, y1, x2, y2],
                            "area_px": area_px,
                            "polygon": polygon,
                            "mask": mask_bool,
                        }
                    )

            overlay_png_b64 = self._render_overlay(
                image_bgr=image_bgr,
                instances=instances,
                class_names=runtime.class_names,
                alpha=float(options["overlay_alpha"]),
            )
            for item in instances:
                item.pop("mask", None)

            return {
                "instances": instances,
                "per_class_area_px": per_class_area_px,
                "overlay_png_b64": overlay_png_b64,
                "engine_meta": {
                    "engine": "linux_native_yolact",
                    "cfg_name": runtime.cfg_name,
                    "model_file": runtime.model_file,
                    "elapsed_ms": round((time.time() - t0) * 1000.0, 2),
                    "instance_count": len(instances),
                },
            }


DEFAULT_VENDOR_ROOT = Path(__file__).resolve().parents[1] / "vendor" / "yolact"
DEFAULT_WEIGHTS_DIR = os.environ.get("AREA_WEIGHTS_DIR", "/opt/area_weights")

engine = AreaNativeEngine(
    weights_dir=DEFAULT_WEIGHTS_DIR,
    vendor_root=os.environ.get("AREA_YOLACT_VENDOR_ROOT", str(DEFAULT_VENDOR_ROOT)),
)
