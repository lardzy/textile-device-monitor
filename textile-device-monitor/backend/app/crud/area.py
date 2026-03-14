from __future__ import annotations

from copy import deepcopy
from sqlalchemy.orm import Session

from app.config import settings
from app.models import SystemConfig

AREA_ROOT_PATH_KEY = "area_root_path"
AREA_MODEL_MAPPING_KEY = "area_model_mapping"

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


def _set_json_value(db: Session, key: str, value: dict[str, str]) -> None:
    row = _get_config_row(db, key)
    if row is None:
        row = SystemConfig(config_key=key, value_json=value)
        db.add(row)
    else:
        row.value_json = value


def ensure_area_config(db: Session) -> None:
    root_row = _get_config_row(db, AREA_ROOT_PATH_KEY)
    mapping_row = _get_config_row(db, AREA_MODEL_MAPPING_KEY)
    changed = False

    if root_row is None or not (root_row.value_text or "").strip():
        _set_text_value(db, AREA_ROOT_PATH_KEY, settings.AREA_ROOT_PATH_DEFAULT.strip())
        changed = True

    if mapping_row is None or not isinstance(mapping_row.value_json, dict):
        _set_json_value(db, AREA_MODEL_MAPPING_KEY, deepcopy(DEFAULT_MODEL_MAPPING))
        changed = True

    if changed:
        db.commit()


def get_area_config(db: Session) -> dict[str, object]:
    ensure_area_config(db)
    root_row = _get_config_row(db, AREA_ROOT_PATH_KEY)
    mapping_row = _get_config_row(db, AREA_MODEL_MAPPING_KEY)

    root_path = (root_row.value_text if root_row else "") or settings.AREA_ROOT_PATH_DEFAULT
    raw_mapping = mapping_row.value_json if mapping_row else {}
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

    if not mapping:
        mapping = deepcopy(DEFAULT_MODEL_MAPPING)

    return {
        "root_path": root_path.strip(),
        "model_mapping": mapping,
    }


def update_area_config(
    db: Session,
    root_path: str,
    model_mapping: dict[str, str],
) -> dict[str, object]:
    normalized_root = root_path.strip()
    normalized_mapping: dict[str, str] = {}
    for model_name, model_file in model_mapping.items():
        key = model_name.strip()
        value = model_file.strip()
        if not key or not value:
            continue
        normalized_mapping[key] = value

    _set_text_value(db, AREA_ROOT_PATH_KEY, normalized_root)
    _set_json_value(db, AREA_MODEL_MAPPING_KEY, normalized_mapping)
    db.commit()
    return get_area_config(db)
