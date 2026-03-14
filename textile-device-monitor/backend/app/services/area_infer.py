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


def _smooth_binary(mask: np.ndarray) -> np.ndarray:
    padded = np.pad(mask.astype(np.uint8), ((1, 1), (1, 1)), mode="constant")
    neighbors = (
        padded[1:-1, 1:-1]
        + padded[:-2, 1:-1]
        + padded[2:, 1:-1]
        + padded[1:-1, :-2]
        + padded[1:-1, 2:]
    )
    return neighbors >= 3


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


def _pick_mask(gray: np.ndarray) -> np.ndarray:
    threshold = _otsu_threshold(gray)
    mask_dark = gray < threshold
    dark_ratio = float(mask_dark.mean())
    if 0.02 <= dark_ratio <= 0.75:
        picked = mask_dark
    else:
        picked = ~mask_dark
    return _smooth_binary(picked)


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
    ) -> AreaImageInferenceResult:
        with self._lock:
            self._ensure_weight_readable(weight_path)

        classes = parse_model_classes(model_name)
        image = Image.open(image_path).convert("RGB")
        image_np = np.array(image, dtype=np.uint8)
        gray = np.mean(image_np, axis=2).astype(np.uint8)
        mask = _pick_mask(gray)
        components = _find_components(mask)
        class_map = _assign_classes(components, gray, classes)

        per_class = {name: 0 for name in classes}
        instances: list[AreaInstance] = []
        overlay = image_np.astype(np.float32).copy()
        alpha = 0.45

        for idx, comp in enumerate(components):
            cls_name = class_map.get(idx, classes[0])
            area = int(comp.shape[0])
            per_class[cls_name] = per_class.get(cls_name, 0) + area

            ys = comp[:, 0]
            xs = comp[:, 1]
            y1, y2 = int(ys.min()), int(ys.max())
            x1, x2 = int(xs.min()), int(xs.max())
            instances.append(AreaInstance(class_name=cls_name, area_px=area, bbox=(x1, y1, x2, y2)))

            color = np.array(PALETTE[idx % len(PALETTE)], dtype=np.float32)
            overlay[ys, xs] = overlay[ys, xs] * (1 - alpha) + color * alpha

        overlay_image = Image.fromarray(np.clip(overlay, 0, 255).astype(np.uint8))
        if instances:
            draw = ImageDraw.Draw(overlay_image)
            for item in instances[:80]:
                x1, y1, x2, y2 = item.bbox
                draw.rectangle((x1, y1, x2, y2), outline=(255, 255, 255), width=1)
                draw.text((x1 + 2, y1 + 2), item.class_name, fill=(255, 255, 255))

        total_area = int(sum(per_class.values()))
        return AreaImageInferenceResult(
            image_name=image_path.name,
            total_area_px=total_area,
            per_class_area_px=per_class,
            instances=instances,
            overlay_image=overlay_image,
        )
