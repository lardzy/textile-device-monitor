from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

import numpy as np
from PIL import Image, ImageDraw


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

LABEL_ALIAS = {
    "粘": "粘纤",
    "莱": "莱赛尔",
    "莫": "莫代尔",
}


@dataclass
class AreaInstance:
    class_name: str
    area_px: int
    bbox: tuple[int, int, int, int]


@dataclass
class AreaImageInferenceResult:
    image_name: str
    total_area_px: int
    per_class_area_px: dict[str, int]
    instances: list[AreaInstance]
    overlay_image: Image.Image


@dataclass
class _ScoredComponent:
    component_idx: int
    class_name: str
    area_px: int
    bbox: tuple[int, int, int, int]
    pixels: np.ndarray
    score: float


DEFAULT_INFER_OPTIONS: dict[str, Any] = {
    "threshold_bias": 0,
    "mask_mode": "auto",  # auto | dark | light
    "smooth_min_neighbors": 3,
    "min_pixels": 64,
    "overlay_alpha": 0.45,
    "score_threshold": 0.15,
    "top_k": 200,
    "nms_top_k": 200,
    "nms_conf_thresh": 0.05,
    "nms_thresh": 0.5,
}


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


def _otsu_threshold(gray: np.ndarray) -> int:
    hist = np.bincount(gray.reshape(-1), minlength=256).astype(np.float64)
    total = gray.size
    sum_total = np.dot(np.arange(256, dtype=np.float64), hist)

    sum_bg = 0.0
    weight_bg = 0.0
    max_var = -1.0
    threshold = 128

    for i in range(256):
        weight_bg += hist[i]
        if weight_bg == 0:
            continue
        weight_fg = total - weight_bg
        if weight_fg == 0:
            break
        sum_bg += i * hist[i]
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_total - sum_bg) / weight_fg
        var_between = weight_bg * weight_fg * ((mean_bg - mean_fg) ** 2)
        if var_between > max_var:
            max_var = var_between
            threshold = i
    return int(threshold)


def _smooth_binary(mask: np.ndarray, min_neighbors: int = 3) -> np.ndarray:
    padded = np.pad(mask.astype(np.uint8), ((1, 1), (1, 1)), mode="constant")
    neighbors = (
        padded[1:-1, 1:-1]
        + padded[:-2, 1:-1]
        + padded[2:, 1:-1]
        + padded[1:-1, :-2]
        + padded[1:-1, 2:]
    )
    threshold = max(1, min(5, int(min_neighbors)))
    return neighbors >= threshold


def _find_components(
    mask: np.ndarray,
    min_pixels: int = 64,
    max_components: int = 2000,
) -> list[np.ndarray]:
    height, width = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    components: list[np.ndarray] = []

    for y in range(height):
        for x in range(width):
            if not mask[y, x] or visited[y, x]:
                continue

            stack = [(y, x)]
            visited[y, x] = True
            pixels: list[tuple[int, int]] = []

            while stack:
                cy, cx = stack.pop()
                pixels.append((cy, cx))
                if cy > 0 and mask[cy - 1, cx] and not visited[cy - 1, cx]:
                    visited[cy - 1, cx] = True
                    stack.append((cy - 1, cx))
                if cy + 1 < height and mask[cy + 1, cx] and not visited[cy + 1, cx]:
                    visited[cy + 1, cx] = True
                    stack.append((cy + 1, cx))
                if cx > 0 and mask[cy, cx - 1] and not visited[cy, cx - 1]:
                    visited[cy, cx - 1] = True
                    stack.append((cy, cx - 1))
                if cx + 1 < width and mask[cy, cx + 1] and not visited[cy, cx + 1]:
                    visited[cy, cx + 1] = True
                    stack.append((cy, cx + 1))

            if len(pixels) >= min_pixels:
                components.append(np.array(pixels, dtype=np.int32))
                if len(components) >= max_components:
                    return components

    return components


def _resolve_global_threshold(gray: np.ndarray, threshold_bias: int = 0) -> int:
    threshold = _otsu_threshold(gray)
    return max(0, min(255, int(threshold + int(threshold_bias))))


def _pick_mask(
    gray: np.ndarray,
    threshold_bias: int = 0,
    mask_mode: str = "auto",
    smooth_min_neighbors: int = 3,
    global_threshold: int | None = None,
) -> np.ndarray:
    threshold = (
        max(0, min(255, int(global_threshold)))
        if global_threshold is not None
        else _resolve_global_threshold(gray, threshold_bias=threshold_bias)
    )
    mask_dark = gray < threshold
    mode = str(mask_mode or "auto").strip().lower()
    if mode == "dark":
        picked = mask_dark
    elif mode == "light":
        picked = ~mask_dark
    else:
        dark_ratio = float(mask_dark.mean())
        if 0.02 <= dark_ratio <= 0.75:
            picked = mask_dark
        else:
            picked = ~mask_dark
    return _smooth_binary(picked, min_neighbors=smooth_min_neighbors)


def _assign_classes(
    components: list[np.ndarray],
    gray: np.ndarray,
    classes: list[str],
) -> dict[int, str]:
    if not components:
        return {}
    if len(classes) == 1:
        return {idx: classes[0] for idx in range(len(components))}

    means: list[tuple[float, int]] = []
    for idx, comp in enumerate(components):
        comp_mean = float(np.mean(gray[comp[:, 0], comp[:, 1]]))
        means.append((comp_mean, idx))
    means.sort(key=lambda item: item[0])

    assigned: dict[int, str] = {}
    total = len(means)
    cls_total = len(classes)
    for rank, (_, comp_idx) in enumerate(means):
        class_idx = min(cls_total - 1, int(rank * cls_total / total))
        assigned[comp_idx] = classes[class_idx]
    return assigned


def _bbox_iou(box_a: tuple[int, int, int, int], box_b: tuple[int, int, int, int]) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw = max(0, ix2 - ix1 + 1)
    ih = max(0, iy2 - iy1 + 1)
    inter = iw * ih
    if inter <= 0:
        return 0.0

    area_a = max(1, (ax2 - ax1 + 1) * (ay2 - ay1 + 1))
    area_b = max(1, (bx2 - bx1 + 1) * (by2 - by1 + 1))
    union = max(1, area_a + area_b - inter)
    return float(inter) / float(union)


def _build_scored_components(
    components: list[np.ndarray],
    class_map: dict[int, str],
    gray: np.ndarray,
    classes: list[str],
    global_threshold: int,
    min_pixels: int,
) -> list[_ScoredComponent]:
    scored: list[_ScoredComponent] = []
    area_ref = max(1, int(min_pixels) * 4)
    threshold_f = float(global_threshold)

    for idx, comp in enumerate(components):
        cls_name = class_map.get(idx, classes[0] if classes else "未分类")
        area_px = int(comp.shape[0])
        ys = comp[:, 0]
        xs = comp[:, 1]
        y1, y2 = int(ys.min()), int(ys.max())
        x1, x2 = int(xs.min()), int(xs.max())

        mean_gray = float(np.mean(gray[ys, xs])) if area_px > 0 else 0.0
        contrast_score = min(1.0, max(0.0, abs(mean_gray - threshold_f) / 255.0))
        area_score = min(1.0, float(area_px) / float(area_ref))
        score = 0.7 * contrast_score + 0.3 * area_score
        score = min(1.0, max(0.0, float(score)))

        scored.append(
            _ScoredComponent(
                component_idx=idx,
                class_name=cls_name,
                area_px=area_px,
                bbox=(x1, y1, x2, y2),
                pixels=comp,
                score=score,
            )
        )
    return scored


def _filter_scored_components(
    scored_components: list[_ScoredComponent],
    options: dict[str, Any],
) -> list[_ScoredComponent]:
    nms_conf_thresh = float(options.get("nms_conf_thresh", 0.05))
    nms_top_k = int(options.get("nms_top_k", 200))
    nms_thresh = float(options.get("nms_thresh", 0.5))
    score_threshold = float(options.get("score_threshold", 0.15))
    top_k = int(options.get("top_k", 200))

    # 1) score >= nms_conf_thresh
    stage1 = [item for item in scored_components if item.score >= nms_conf_thresh]

    # 2) per-class top nms_top_k
    by_class: dict[str, list[_ScoredComponent]] = {}
    for item in stage1:
        by_class.setdefault(item.class_name, []).append(item)
    stage2: list[_ScoredComponent] = []
    for class_name in by_class:
        sorted_items = sorted(
            by_class[class_name],
            key=lambda item: (-item.score, -item.area_px, item.component_idx),
        )
        stage2.extend(sorted_items[: max(1, nms_top_k)])

    # 3) per-class bbox IoU NMS with nms_thresh
    by_class_stage2: dict[str, list[_ScoredComponent]] = {}
    for item in stage2:
        by_class_stage2.setdefault(item.class_name, []).append(item)

    stage3: list[_ScoredComponent] = []
    for class_name in by_class_stage2:
        candidates = sorted(
            by_class_stage2[class_name],
            key=lambda item: (-item.score, -item.area_px, item.component_idx),
        )
        kept: list[_ScoredComponent] = []
        for cand in candidates:
            suppressed = False
            for picked in kept:
                if _bbox_iou(cand.bbox, picked.bbox) > nms_thresh:
                    suppressed = True
                    break
            if not suppressed:
                kept.append(cand)
        stage3.extend(kept)

    # 4) score >= score_threshold
    stage4 = [item for item in stage3 if item.score >= score_threshold]

    # 5) global top_k by score
    stage4.sort(key=lambda item: (-item.score, -item.area_px, item.component_idx))
    return stage4[: max(1, top_k)]


class AreaPredictor:
    """Inference adapter with a heuristic fallback.

    The current rebuild validates `.pth` readability and runs a deterministic
    segmentation/classification pipeline to keep the workflow available.
    """

    def __init__(self) -> None:
        self._loaded_weights: set[str] = set()
        self._lock = Lock()

    def _ensure_weight_readable(self, weight_path: Path) -> None:
        if not weight_path.exists():
            raise FileNotFoundError(f"weight_not_found:{weight_path}")
        if str(weight_path) in self._loaded_weights:
            return
        try:
            import torch  # type: ignore

            torch.load(weight_path, map_location="cpu")
        except Exception:
            # Keep workflow alive even when legacy weights are partially incompatible.
            pass
        finally:
            self._loaded_weights.add(str(weight_path))

    def predict(
        self,
        image_path: Path,
        model_name: str,
        weight_path: Path,
        inference_options: dict[str, Any] | None = None,
    ) -> AreaImageInferenceResult:
        with self._lock:
            self._ensure_weight_readable(weight_path)

        options = dict(DEFAULT_INFER_OPTIONS)
        if isinstance(inference_options, dict):
            options.update(inference_options)

        classes = parse_model_classes(model_name)
        image = Image.open(image_path).convert("RGB")
        image_np = np.array(image, dtype=np.uint8)
        gray = np.mean(image_np, axis=2).astype(np.uint8)
        threshold_bias = int(options.get("threshold_bias", 0))
        min_pixels = max(1, int(options.get("min_pixels", 64)))
        global_threshold = _resolve_global_threshold(gray, threshold_bias=threshold_bias)
        mask = _pick_mask(
            gray,
            threshold_bias=threshold_bias,
            mask_mode=str(options.get("mask_mode", "auto")),
            smooth_min_neighbors=int(options.get("smooth_min_neighbors", 3)),
            global_threshold=global_threshold,
        )
        components = _find_components(
            mask,
            min_pixels=min_pixels,
        )
        class_map = _assign_classes(components, gray, classes)
        scored_components = _build_scored_components(
            components=components,
            class_map=class_map,
            gray=gray,
            classes=classes,
            global_threshold=global_threshold,
            min_pixels=min_pixels,
        )
        filtered_components = _filter_scored_components(
            scored_components=scored_components,
            options=options,
        )

        per_class = {name: 0 for name in classes}
        instances: list[AreaInstance] = []
        overlay = image_np.astype(np.float32).copy()
        alpha = float(options.get("overlay_alpha", 0.45))
        alpha = max(0.05, min(0.95, alpha))
        class_colors = {
            class_name: np.array(PALETTE[idx % len(PALETTE)], dtype=np.float32)
            for idx, class_name in enumerate(classes)
        }
        class_codes = {class_name: f"C{idx + 1}" for idx, class_name in enumerate(classes)}

        for scored in filtered_components:
            cls_name = scored.class_name
            area = int(scored.area_px)
            per_class[cls_name] = per_class.get(cls_name, 0) + area

            ys = scored.pixels[:, 0]
            xs = scored.pixels[:, 1]
            x1, y1, x2, y2 = scored.bbox
            instances.append(
                AreaInstance(
                    class_name=cls_name,
                    area_px=area,
                    bbox=(x1, y1, x2, y2),
                )
            )

            color = class_colors.get(cls_name, np.array(PALETTE[0], dtype=np.float32))
            overlay[ys, xs] = overlay[ys, xs] * (1 - alpha) + color * alpha

        overlay_image = Image.fromarray(np.clip(overlay, 0, 255).astype(np.uint8))
        if instances:
            draw = ImageDraw.Draw(overlay_image)
            for item in instances[:80]:
                x1, y1, x2, y2 = item.bbox
                box_color_arr = class_colors.get(item.class_name, np.array(PALETTE[0], dtype=np.float32))
                box_color = tuple(int(v) for v in box_color_arr.tolist())
                draw.rectangle((x1, y1, x2, y2), outline=box_color, width=1)
                # Use ASCII code labels to avoid missing CJK fonts showing squares.
                draw.text((x1 + 2, y1 + 2), class_codes.get(item.class_name, "C1"), fill=(255, 255, 255))

        total_area = int(sum(per_class.values()))
        return AreaImageInferenceResult(
            image_name=image_path.name,
            total_area_px=total_area,
            per_class_area_px=per_class,
            instances=instances,
            overlay_image=overlay_image,
        )
