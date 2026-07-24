from __future__ import annotations

from pathlib import Path
from typing import Any


MODEL_METADATA_VERSION = 1

LABEL_ALIASES: dict[str, str] = {
    "粘": "粘纤",
    "莱": "莱赛尔",
    "莫": "莫代尔",
}

# class_names is the actual class-index order used by each trusted weight file.
# The two historically reversed binary models intentionally differ from their
# display names.
MODEL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "model_name": "粘纤-莱赛尔",
        "model_file": "b_v1_1.3.pth",
        "class_names": ("莱赛尔", "粘纤"),
    },
    {
        "model_name": "棉-粘纤",
        "model_file": "b_cv_1.3.pth",
        "class_names": ("棉", "粘纤"),
    },
    {
        "model_name": "棉-莱赛尔",
        "model_file": "b_c1_1.3.pth",
        "class_names": ("莱赛尔", "棉"),
    },
    {
        "model_name": "棉-莫代尔",
        "model_file": "b_cm_1.3.pth",
        "class_names": ("棉", "莫代尔"),
    },
    {
        "model_name": "棉-再生纤维素纤维",
        "model_file": "b_cc_1.3.pth",
        "class_names": ("棉", "再生纤维素纤维"),
    },
    {
        "model_name": "棉-粘-莱-莫",
        "model_file": "b_cvlm_1.3.pth",
        "class_names": ("棉", "粘纤", "莱赛尔", "莫代尔"),
    },
)


def normalize_label(label: str) -> str:
    token = str(label or "").strip()
    if not token:
        return "未分类"
    return LABEL_ALIASES.get(token, token)


def parse_model_classes(model_name: str) -> list[str]:
    classes: list[str] = []
    for item in str(model_name or "").split("-"):
        normalized = normalize_label(item)
        if normalized not in classes:
            classes.append(normalized)
    return classes or ["未分类"]


def find_model_spec(*, model_file: str = "", model_name: str = "") -> dict[str, Any] | None:
    file_key = Path(str(model_file or "").strip()).name.casefold()
    name_key = str(model_name or "").replace(" ", "").strip().casefold()
    for spec in MODEL_SPECS:
        if file_key and str(spec["model_file"]).casefold() == file_key:
            return spec
    for spec in MODEL_SPECS:
        candidate = str(spec["model_name"]).replace(" ", "").strip().casefold()
        if name_key and candidate == name_key:
            return spec
    return None


def resolve_model_classes(*, model_name: str, model_file: str) -> tuple[tuple[str, ...], bool]:
    file_spec = find_model_spec(model_file=model_file) if str(model_file or "").strip() else None
    name_spec = find_model_spec(model_name=model_name) if str(model_name or "").strip() else None
    if name_spec is not None and str(model_file or "").strip() and file_spec is None:
        raise ValueError(
            "model_name_file_mismatch:"
            f"{model_name}:{Path(str(model_file or '')).name}"
        )
    if (
        file_spec is not None
        and name_spec is not None
        and str(file_spec["model_file"]).casefold() != str(name_spec["model_file"]).casefold()
    ):
        raise ValueError(
            "model_name_file_mismatch:"
            f"{model_name}:{Path(str(model_file or '')).name}"
        )
    if file_spec is not None:
        requested_classes = tuple(parse_model_classes(model_name))
        expected_classes = tuple(str(item) for item in file_spec["class_names"])
        if (
            len(requested_classes) != len(expected_classes)
            or set(requested_classes) != set(expected_classes)
        ):
            raise ValueError(
                "model_name_file_mismatch:"
                f"{model_name}:{Path(str(model_file or '')).name}"
            )

    # A supplied filename is authoritative. Unknown/custom weights must not
    # silently inherit metadata from a similarly named built-in model.
    spec = file_spec if str(model_file or "").strip() else name_spec
    if spec is not None:
        return tuple(str(item) for item in spec["class_names"]), True
    return tuple(parse_model_classes(model_name)), False


def resolve_requested_class_mapping(
    *,
    model_name: str,
    model_file: str,
    class_mapping_version: int | None,
) -> tuple[tuple[str, ...], str]:
    """Keep old backends compatible while new callers opt into semantic labels."""
    if int(class_mapping_version or 0) < 1:
        return tuple(parse_model_classes(model_name)), "legacy_display_order"

    classes, trusted = resolve_model_classes(
        model_name=model_name,
        model_file=model_file,
    )
    return classes, "trusted_metadata_v1" if trusted else "caller_display_order_v1"
