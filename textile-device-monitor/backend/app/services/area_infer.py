from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from PIL import Image


LABEL_ALIAS = {
    "粘": "粘纤",
    "莱": "莱赛尔",
    "莫": "莫代尔",
}

CANONICAL_CLASS_MAPPINGS = {
    "trusted_metadata_v1",
    "canonicalized_by_backend_v1",
    "caller_display_order_v1",
}

# Older area-infer images used the display-name order as the class-index order.
# The actual index order of these two weights is reversed, so old responses
# need one compatibility conversion before they are persisted.
LEGACY_CLASS_SWAP_BY_MODEL: dict[str, dict[str, str]] = {
    "棉-莱赛尔": {
        "棉": "莱赛尔",
        "莱赛尔": "棉",
    },
    "粘纤-莱赛尔": {
        "粘纤": "莱赛尔",
        "莱赛尔": "粘纤",
    },
}
LEGACY_CLASS_SWAP_MODEL_FILES: dict[str, str] = {
    "棉-莱赛尔": "b_c1_1.3.pth",
    "粘纤-莱赛尔": "b_v1_1.3.pth",
}


@dataclass
class AreaInstance:
    class_name: str
    area_px: int
    bbox: tuple[int, int, int, int]
    score: float | None = None
    polygon: list[list[int]] = field(default_factory=list)


@dataclass
class AreaImageInferenceResult:
    image_name: str
    total_area_px: int
    per_class_area_px: dict[str, int]
    instances: list[AreaInstance]
    overlay_image: Image.Image
    engine_meta: dict[str, Any] = field(default_factory=dict)


DEFAULT_INFER_OPTIONS: dict[str, Any] = {
    "threshold_bias": 0,
    "mask_mode": "auto",
    "smooth_min_neighbors": 3,
    "min_pixels": 64,
    "overlay_alpha": 0.45,
    "score_threshold": 0.15,
    "top_k": 200,
    "nms_top_k": 200,
    "nms_conf_thresh": 0.05,
    "nms_thresh": 0.5,
}

KNOWN_INFER_ERRORS = {
    "infer_service_unavailable",
    "infer_model_load_failed",
    "infer_timeout",
    "infer_bad_response",
}

CLASS_MAPPING_VERSION = 1


def parse_model_classes(model_name: str) -> list[str]:
    classes: list[str] = []
    for item in model_name.split("-"):
        token = item.strip()
        if not token:
            continue
        classes.append(LABEL_ALIAS.get(token, token))
    deduped: list[str] = []
    for item in classes:
        if item not in deduped:
            deduped.append(item)
    return deduped or ["未分类"]


def normalize_model_name(model_name: str) -> str:
    token = str(model_name or "").replace(" ", "").strip()
    if not token:
        return ""
    return "-".join(parse_model_classes(token))


def normalize_class_name(class_name: str) -> str:
    token = str(class_name or "").strip() or "未分类"
    return LABEL_ALIAS.get(token, token)


def uses_canonical_class_mapping(engine_meta: dict[str, Any] | None) -> bool:
    if not isinstance(engine_meta, dict):
        return False
    return str(engine_meta.get("class_mapping") or "").strip() in CANONICAL_CLASS_MAPPINGS


def requires_legacy_class_remap(
    model_name: str,
    model_file: str | None = None,
) -> bool:
    normalized_model = normalize_model_name(model_name)
    expected_file = LEGACY_CLASS_SWAP_MODEL_FILES.get(normalized_model)
    if not expected_file:
        return False
    supplied_file = Path(str(model_file or "").strip()).name
    if supplied_file:
        return supplied_file.casefold() == expected_file.casefold()
    return True


def canonicalize_infer_class_name(
    model_name: str,
    class_name: str,
    *,
    engine_meta: dict[str, Any] | None,
    model_file: str | None = None,
) -> str:
    normalized = normalize_class_name(class_name)
    if uses_canonical_class_mapping(engine_meta):
        return normalized
    if not requires_legacy_class_remap(model_name, model_file):
        return normalized
    mapping = LEGACY_CLASS_SWAP_BY_MODEL.get(normalize_model_name(model_name), {})
    return mapping.get(normalized, normalized)


class AreaPredictor:
    def __init__(
        self,
        *,
        infer_url: str | None = None,
        timeout_sec: int = 30,
    ) -> None:
        self._infer_url = self._normalize_base_url(infer_url or "")
        self._timeout_sec = max(1, int(timeout_sec or 30))

    def _normalize_base_url(self, raw: str) -> str:
        return str(raw or "").strip().rstrip("/")

    def _resolve_url(self, infer_url: str | None = None) -> str:
        url = self._normalize_base_url(infer_url or "")
        if url:
            return url
        return self._infer_url

    def _resolve_timeout(self, timeout_sec: int | None = None) -> int:
        if timeout_sec is None:
            return self._timeout_sec
        return max(1, int(timeout_sec))

    def _extract_error_code(self, response: requests.Response) -> str:
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or "").strip()
            if detail:
                return detail
        return "infer_bad_response"

    def check_service_health(
        self,
        *,
        infer_url: str | None = None,
        timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        base_url = self._resolve_url(infer_url)
        if not base_url:
            raise RuntimeError("infer_service_unavailable")
        timeout = self._resolve_timeout(timeout_sec)
        try:
            response = requests.get(f"{base_url}/health", timeout=timeout)
        except requests.Timeout as exc:
            raise RuntimeError("infer_timeout") from exc
        except requests.RequestException as exc:
            raise RuntimeError("infer_service_unavailable") from exc

        if response.status_code >= 400:
            code = self._extract_error_code(response)
            if code in KNOWN_INFER_ERRORS:
                raise RuntimeError(code)
            raise RuntimeError("infer_service_unavailable")

        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError("infer_bad_response") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("infer_bad_response")
        status = str(payload.get("status") or "").lower()
        if status and status != "ok":
            raise RuntimeError("infer_service_unavailable")
        return payload

    def warmup_model(
        self,
        *,
        model_name: str,
        model_file: str,
        infer_url: str | None = None,
        timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        base_url = self._resolve_url(infer_url)
        if not base_url:
            raise RuntimeError("infer_service_unavailable")
        timeout = self._resolve_timeout(timeout_sec)
        payload = {
            "model_name": model_name,
            "model_file": model_file,
            "class_mapping_version": CLASS_MAPPING_VERSION,
        }
        try:
            response = requests.post(f"{base_url}/v1/warmup", json=payload, timeout=timeout)
        except requests.Timeout as exc:
            raise RuntimeError("infer_timeout") from exc
        except requests.RequestException as exc:
            raise RuntimeError("infer_service_unavailable") from exc

        if response.status_code >= 400:
            code = self._extract_error_code(response)
            if code in KNOWN_INFER_ERRORS:
                raise RuntimeError(code)
            raise RuntimeError("infer_bad_response")

        try:
            body = response.json()
        except Exception as exc:
            raise RuntimeError("infer_bad_response") from exc
        if not isinstance(body, dict):
            raise RuntimeError("infer_bad_response")
        return body

    def predict(
        self,
        image_path: Path,
        model_name: str,
        weight_path: Path,
        inference_options: dict[str, Any] | None = None,
        *,
        infer_url: str | None = None,
        timeout_sec: int | None = None,
        model_file: str | None = None,
    ) -> AreaImageInferenceResult:
        base_url = self._resolve_url(infer_url)
        if not base_url:
            raise RuntimeError("infer_service_unavailable")
        timeout = self._resolve_timeout(timeout_sec)
        request_payload = {
            "model_name": model_name,
            "model_file": model_file or weight_path.name,
            "class_mapping_version": CLASS_MAPPING_VERSION,
            "image_bytes_b64": base64.b64encode(image_path.read_bytes()).decode("utf-8"),
            "inference_options": dict(DEFAULT_INFER_OPTIONS) | dict(inference_options or {}),
        }

        try:
            response = requests.post(
                f"{base_url}/v1/infer",
                json=request_payload,
                timeout=timeout,
            )
        except requests.Timeout as exc:
            raise RuntimeError("infer_timeout") from exc
        except requests.RequestException as exc:
            raise RuntimeError("infer_service_unavailable") from exc

        if response.status_code >= 400:
            code = self._extract_error_code(response)
            if code in KNOWN_INFER_ERRORS:
                raise RuntimeError(code)
            raise RuntimeError("infer_bad_response")

        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError("infer_bad_response") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("infer_bad_response")

        raw_engine_meta = payload.get("engine_meta")
        if not isinstance(raw_engine_meta, dict):
            raw_engine_meta = {}
        upstream_mapping = str(raw_engine_meta.get("class_mapping") or "").strip()
        requested_model_file = model_file or weight_path.name
        legacy_remap_required = (
            not uses_canonical_class_mapping(raw_engine_meta)
            and requires_legacy_class_remap(model_name, requested_model_file)
        )
        engine_meta = dict(raw_engine_meta)
        if not uses_canonical_class_mapping(engine_meta):
            if upstream_mapping:
                engine_meta["upstream_class_mapping"] = upstream_mapping
            engine_meta["class_mapping"] = "canonicalized_by_backend_v1"
            engine_meta["class_names"] = parse_model_classes(model_name)
            if legacy_remap_required:
                engine_meta["legacy_class_remap_applied"] = True

        classes = parse_model_classes(model_name)
        per_class: dict[str, int] = {name: 0 for name in classes}
        raw_per_class = payload.get("per_class_area_px")
        if isinstance(raw_per_class, dict):
            for class_name, area in raw_per_class.items():
                key = canonicalize_infer_class_name(
                    model_name,
                    str(class_name or ""),
                    engine_meta=raw_engine_meta,
                    model_file=requested_model_file,
                )
                try:
                    area_px = int(area)
                except (TypeError, ValueError):
                    area_px = 0
                per_class[key] = per_class.get(key, 0) + max(0, area_px)

        instances: list[AreaInstance] = []
        raw_instances = payload.get("instances")
        if isinstance(raw_instances, list):
            for item in raw_instances:
                if not isinstance(item, dict):
                    continue
                class_name = canonicalize_infer_class_name(
                    model_name,
                    str(item.get("class_name") or ""),
                    engine_meta=raw_engine_meta,
                    model_file=requested_model_file,
                )
                bbox_raw = item.get("bbox")
                if not isinstance(bbox_raw, (list, tuple)) or len(bbox_raw) != 4:
                    continue
                try:
                    bbox = (
                        int(bbox_raw[0]),
                        int(bbox_raw[1]),
                        int(bbox_raw[2]),
                        int(bbox_raw[3]),
                    )
                except (TypeError, ValueError):
                    continue
                try:
                    area_px = int(item.get("area_px") or 0)
                except (TypeError, ValueError):
                    area_px = 0
                raw_score = item.get("score")
                score: float | None
                try:
                    score = float(raw_score) if raw_score is not None else None
                except (TypeError, ValueError):
                    score = None
                polygon: list[list[int]] = []
                raw_polygon = item.get("polygon")
                if isinstance(raw_polygon, list):
                    for point in raw_polygon:
                        if not isinstance(point, (list, tuple)) or len(point) != 2:
                            continue
                        try:
                            px = int(point[0])
                            py = int(point[1])
                        except (TypeError, ValueError):
                            continue
                        polygon.append([px, py])
                instances.append(
                    AreaInstance(
                        class_name=class_name,
                        area_px=max(0, area_px),
                        bbox=bbox,
                        score=score,
                        polygon=polygon,
                    )
                )
                if class_name not in per_class:
                    per_class[class_name] = 0
                if not isinstance(raw_per_class, dict):
                    per_class[class_name] += max(0, area_px)

        overlay_png_b64 = payload.get("overlay_png_b64")
        if isinstance(overlay_png_b64, str) and overlay_png_b64.strip():
            try:
                overlay_bytes = base64.b64decode(overlay_png_b64)
                overlay_image = Image.open(io.BytesIO(overlay_bytes)).convert("RGB")
            except Exception as exc:
                raise RuntimeError("infer_bad_response") from exc
        else:
            overlay_image = Image.open(image_path).convert("RGB")

        total_area = int(sum(max(0, int(v)) for v in per_class.values()))
        if total_area <= 0:
            total_area = int(sum(max(0, int(inst.area_px)) for inst in instances))
        total_area = max(0, total_area)

        return AreaImageInferenceResult(
            image_name=str(payload.get("image_name") or image_path.name),
            total_area_px=total_area,
            per_class_area_px=per_class,
            instances=instances,
            overlay_image=overlay_image,
            engine_meta=engine_meta,
        )
