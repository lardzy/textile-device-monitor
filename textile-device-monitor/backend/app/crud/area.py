from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from sqlalchemy.orm import Session

from app.config import settings
from app.models import SystemConfig
from app.services.area_infer import DEFAULT_INFER_OPTIONS

AREA_ROOT_PATH_KEY = "area_root_path"
AREA_MODEL_MAPPING_KEY = "area_model_mapping"
AREA_OLD_ROOT_PATH_KEY = "area_old_root_path"
AREA_RESULT_OUTPUT_ROOT_KEY = "area_result_output_root"
AREA_INFERENCE_DEFAULTS_KEY = "area_inference_defaults"
AREA_ARCHIVE_LAST_RUN_AT_KEY = "area_archive_last_run_at"

DEFAULT_OLD_ROOT_PATH = r"\\192.168.105.82\材料检测中心\10特纤\02-检验"

DEFAULT_MODEL_MAPPING: dict[str, str] = {
    "粘纤-莱赛尔": "b_v1_1.3.pth",
    "棉-粘纤": "b_cv_1.3.pth",
    "棉-莱赛尔": "b_c1_1.3.pth",
    "棉-莫代尔": "b_cm_1.3.pth",
    "棉-再生纤维素纤维": "b_cc_1.3.pth",
    "棉-粘-莱-莫": "b_cvlm_1.3.pth",
}


def _get_config_row(db: Session, key: str) -> SystemConfig | None:
    return db.query(SystemConfig).filter(SystemConfig.config_key == key).first()


def _set_text_value(db: Session, key: str, value: str) -> None:
    row = _get_config_row(db, key)
    if row is None:
        row = SystemConfig(config_key=key, value_text=value)
        db.add(row)
    else:
        row.value_text = value


def _set_json_value(db: Session, key: str, value: dict[str, Any]) -> None:
    row = _get_config_row(db, key)
    if row is None:
        row = SystemConfig(config_key=key, value_json=value)
        db.add(row)
    else:
        row.value_json = value


def _normalize_mapping(raw_mapping: Any) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if isinstance(raw_mapping, dict):
        for model_name, model_file in raw_mapping.items():
            if not isinstance(model_name, str):
                continue
            if not isinstance(model_file, str):
                continue
            key = model_name.strip()
            value = model_file.strip()
            if key and value:
                mapping[key] = value
    return mapping


def _normalize_inference_defaults(raw: Any) -> dict[str, Any]:
    normalized = dict(DEFAULT_INFER_OPTIONS)
    if not isinstance(raw, dict):
        return normalized
    try:
        if "threshold_bias" in raw:
            normalized["threshold_bias"] = max(-128, min(128, int(raw.get("threshold_bias", 0))))
        if "mask_mode" in raw:
            mode = str(raw.get("mask_mode", "auto")).strip().lower()
            normalized["mask_mode"] = mode if mode in {"auto", "dark", "light"} else "auto"
        if "smooth_min_neighbors" in raw:
            normalized["smooth_min_neighbors"] = max(1, min(5, int(raw.get("smooth_min_neighbors", 3))))
        if "min_pixels" in raw:
            normalized["min_pixels"] = max(1, min(100000, int(raw.get("min_pixels", 64))))
        if "overlay_alpha" in raw:
            normalized["overlay_alpha"] = max(0.05, min(0.95, float(raw.get("overlay_alpha", 0.45))))
        if "score_threshold" in raw:
            normalized["score_threshold"] = max(0.0, min(1.0, float(raw.get("score_threshold", 0.15))))
        if "top_k" in raw:
            normalized["top_k"] = max(1, min(1000, int(raw.get("top_k", 200))))
        if "nms_top_k" in raw:
            normalized["nms_top_k"] = max(1, min(1000, int(raw.get("nms_top_k", 200))))
        if "nms_conf_thresh" in raw:
            normalized["nms_conf_thresh"] = max(0.0, min(1.0, float(raw.get("nms_conf_thresh", 0.05))))
        if "nms_thresh" in raw:
            normalized["nms_thresh"] = max(0.0, min(1.0, float(raw.get("nms_thresh", 0.5))))
    except (TypeError, ValueError):
        return dict(DEFAULT_INFER_OPTIONS)
    return normalized


def ensure_area_config(db: Session) -> None:
    root_row = _get_config_row(db, AREA_ROOT_PATH_KEY)
    mapping_row = _get_config_row(db, AREA_MODEL_MAPPING_KEY)
    old_root_row = _get_config_row(db, AREA_OLD_ROOT_PATH_KEY)
    output_root_row = _get_config_row(db, AREA_RESULT_OUTPUT_ROOT_KEY)
    infer_defaults_row = _get_config_row(db, AREA_INFERENCE_DEFAULTS_KEY)
    archive_last_run_row = _get_config_row(db, AREA_ARCHIVE_LAST_RUN_AT_KEY)
    changed = False

    if root_row is None or not (root_row.value_text or "").strip():
        _set_text_value(db, AREA_ROOT_PATH_KEY, settings.AREA_ROOT_PATH_DEFAULT.strip())
        changed = True

    if not _normalize_mapping(mapping_row.value_json if mapping_row else {}):
        _set_json_value(db, AREA_MODEL_MAPPING_KEY, deepcopy(DEFAULT_MODEL_MAPPING))
        changed = True

    if old_root_row is None or not (old_root_row.value_text or "").strip():
        _set_text_value(db, AREA_OLD_ROOT_PATH_KEY, DEFAULT_OLD_ROOT_PATH)
        changed = True

    if output_root_row is None or not (output_root_row.value_text or "").strip():
        _set_text_value(db, AREA_RESULT_OUTPUT_ROOT_KEY, settings.AREA_OUTPUT_DIR.strip())
        changed = True

    if infer_defaults_row is None or not isinstance(infer_defaults_row.value_json, dict):
        _set_json_value(db, AREA_INFERENCE_DEFAULTS_KEY, dict(DEFAULT_INFER_OPTIONS))
        changed = True

    if archive_last_run_row is None:
        _set_text_value(db, AREA_ARCHIVE_LAST_RUN_AT_KEY, "")
        changed = True

    if changed:
        db.commit()


def get_area_config(db: Session) -> dict[str, object]:
    ensure_area_config(db)
    root_row = _get_config_row(db, AREA_ROOT_PATH_KEY)
    mapping_row = _get_config_row(db, AREA_MODEL_MAPPING_KEY)
    old_root_row = _get_config_row(db, AREA_OLD_ROOT_PATH_KEY)
    output_root_row = _get_config_row(db, AREA_RESULT_OUTPUT_ROOT_KEY)
    infer_defaults_row = _get_config_row(db, AREA_INFERENCE_DEFAULTS_KEY)
    archive_last_run_row = _get_config_row(db, AREA_ARCHIVE_LAST_RUN_AT_KEY)

    root_path = ((root_row.value_text if root_row else "") or settings.AREA_ROOT_PATH_DEFAULT).strip()
    old_root_path = ((old_root_row.value_text if old_root_row else "") or DEFAULT_OLD_ROOT_PATH).strip()
    result_output_root = ((output_root_row.value_text if output_root_row else "") or settings.AREA_OUTPUT_DIR).strip()
    mapping = _normalize_mapping(mapping_row.value_json if mapping_row else {})
    if not mapping:
        mapping = deepcopy(DEFAULT_MODEL_MAPPING)
    inference_defaults = _normalize_inference_defaults(
        infer_defaults_row.value_json if infer_defaults_row else {}
    )
    archive_last_run_at = (archive_last_run_row.value_text if archive_last_run_row else "") or ""

    return {
        "root_path": root_path,
        "old_root_path": old_root_path,
        "result_output_root": result_output_root,
        "model_mapping": mapping,
        "inference_defaults": inference_defaults,
        "archive_last_run_at": archive_last_run_at.strip(),
    }


def update_area_config(
    db: Session,
    root_path: str,
    old_root_path: str,
    result_output_root: str,
    model_mapping: dict[str, str],
    inference_defaults: dict[str, Any],
) -> dict[str, object]:
    normalized_root = root_path.strip()
    normalized_old_root = old_root_path.strip()
    normalized_result_output_root = result_output_root.strip()
    normalized_mapping: dict[str, str] = {}
    for model_name, model_file in model_mapping.items():
        key = model_name.strip()
        value = model_file.strip()
        if not key or not value:
            continue
        normalized_mapping[key] = value

    normalized_defaults = _normalize_inference_defaults(inference_defaults)
    _set_text_value(db, AREA_ROOT_PATH_KEY, normalized_root)
    _set_text_value(db, AREA_OLD_ROOT_PATH_KEY, normalized_old_root)
    _set_text_value(db, AREA_RESULT_OUTPUT_ROOT_KEY, normalized_result_output_root)
    _set_json_value(db, AREA_MODEL_MAPPING_KEY, normalized_mapping)
    _set_json_value(db, AREA_INFERENCE_DEFAULTS_KEY, normalized_defaults)
    db.commit()
    return get_area_config(db)


def get_archive_last_run_at(db: Session) -> datetime | None:
    ensure_area_config(db)
    row = _get_config_row(db, AREA_ARCHIVE_LAST_RUN_AT_KEY)
    raw = (row.value_text if row else "") or ""
    text = raw.strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    except Exception:
        return None


def set_archive_last_run_at(db: Session, value: datetime) -> None:
    utc_value = value.astimezone(timezone.utc).isoformat()
    _set_text_value(db, AREA_ARCHIVE_LAST_RUN_AT_KEY, utc_value)
    db.commit()
