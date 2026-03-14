from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.crud import area as area_crud
from app.database import get_db
from app.services.area_jobs import area_job_manager

router = APIRouter(prefix="/area", tags=["area"])


class AreaConfigPayload(BaseModel):
    root_path: str = Field(..., min_length=1, max_length=1000)
    model_mapping: dict[str, str]


class AreaJobCreatePayload(BaseModel):
    folder_name: str = Field(..., min_length=1, max_length=255)
    model_name: str = Field(..., min_length=1, max_length=200)


def _ensure_enabled() -> None:
    if not settings.AREA_ENABLED:
        raise HTTPException(status_code=503, detail="area_disabled")


def _with_artifact_urls(job: dict) -> dict:
    payload = dict(job)
    job_id = payload.get("job_id")
    if job_id:
        payload["artifact_urls"] = {
            "result": f"/api/area/jobs/{job_id}/result",
            "excel": f"/api/area/jobs/{job_id}/artifacts/excel",
            "images": f"/api/area/jobs/{job_id}/artifacts/images",
        }
    return payload


@router.get("/config")
def get_area_config(db: Session = Depends(get_db)):
    _ensure_enabled()
    config = area_crud.get_area_config(db)
    model_mapping = config.get("model_mapping", {})
    model_options = sorted(model_mapping.keys())
    return {
        "root_path": config.get("root_path"),
        "model_mapping": model_mapping,
        "model_options": model_options,
    }


@router.put("/config")
def update_area_config(payload: AreaConfigPayload, db: Session = Depends(get_db)):
    _ensure_enabled()
    root_path = payload.root_path.strip()
    if not root_path:
        raise HTTPException(status_code=400, detail="invalid_root_path")
    if not payload.model_mapping:
        raise HTTPException(status_code=400, detail="invalid_model_mapping")
    updated = area_crud.update_area_config(db, root_path, payload.model_mapping)
    return {
        "root_path": updated.get("root_path"),
        "model_mapping": updated.get("model_mapping"),
        "model_options": sorted((updated.get("model_mapping") or {}).keys()),
    }


@router.post("/jobs")
def create_area_job(payload: AreaJobCreatePayload, db: Session = Depends(get_db)):
    _ensure_enabled()
    config = area_crud.get_area_config(db)
    try:
        job = area_job_manager.create_job(
            folder_name=payload.folder_name,
            model_name=payload.model_name,
            root_path=str(config.get("root_path") or ""),
            model_mapping=dict(config.get("model_mapping") or {}),
            weights_dir=settings.AREA_WEIGHTS_DIR,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="area_job_create_failed") from exc
    return _with_artifact_urls(job)


@router.get("/jobs")
def list_area_jobs(limit: int = Query(200, ge=1, le=1000)):
    _ensure_enabled()
    jobs = area_job_manager.list_jobs(limit=limit)
    return [_with_artifact_urls(item) for item in jobs]


@router.get("/jobs/{job_id}")
def get_area_job(job_id: str):
    _ensure_enabled()
    job = area_job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    return _with_artifact_urls(job)


@router.get("/jobs/{job_id}/result")
def get_area_result(job_id: str):
    _ensure_enabled()
    result = area_job_manager.get_result(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    status = result.get("status")
    if status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="job_not_completed")
    if status == "failed":
        raise HTTPException(status_code=409, detail="job_failed")
    return result


@router.get("/jobs/{job_id}/artifacts/excel")
def download_area_excel(job_id: str):
    _ensure_enabled()
    path = area_job_manager.get_excel_path(job_id)
    if path is None:
        raise HTTPException(status_code=404, detail="result_not_found")
    return FileResponse(
        path=path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{job_id}.xlsx",
    )


@router.get("/jobs/{job_id}/artifacts/images")
def list_area_images(
    job_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    _ensure_enabled()
    payload = area_job_manager.list_overlay_images(job_id, page=page, page_size=page_size)
    if payload is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    items = []
    for item in payload["items"]:
        overlay_filename = item.get("overlay_filename", "")
        item_payload = dict(item)
        item_payload["url"] = (
            f"/api/area/jobs/{job_id}/artifacts/image/{overlay_filename}"
            if overlay_filename
            else ""
        )
        items.append(item_payload)
    return {
        "items": items,
        "total": payload["total"],
        "page": payload["page"],
        "page_size": payload["page_size"],
    }


@router.get("/jobs/{job_id}/artifacts/image/{filename}")
def get_area_image(job_id: str, filename: str):
    _ensure_enabled()
    if "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="invalid_filename")
    path = area_job_manager.get_overlay_image_path(job_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="image_not_found")
    media_type = "image/png"
    return FileResponse(path=path, media_type=media_type, filename=Path(filename).name)
