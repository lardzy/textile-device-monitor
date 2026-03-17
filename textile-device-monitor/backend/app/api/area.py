from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
    old_root_path: str = Field(..., min_length=1, max_length=1000)
    result_output_root: str = Field(..., min_length=1, max_length=1000)
    model_mapping: dict[str, str]
    inference_defaults: dict[str, Any] = Field(default_factory=dict)
    archive_enabled: bool = False


class AreaJobCreatePayload(BaseModel):
    folder_name: str = Field(..., min_length=1, max_length=255)
    model_name: str = Field(..., min_length=1, max_length=200)
    inference_options: dict[str, object] | None = None


class AreaFolderCleanupPayload(BaseModel):
    rename_enabled: bool = False
    new_folder_name: str | None = None


class AreaEditorSavePayload(BaseModel):
    edited_by_id: str = Field(default="")
    instances: list[dict[str, Any]] = Field(default_factory=list)


class AreaEditorResetPayload(BaseModel):
    edited_by_id: str = Field(default="")


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
            "editor_images": f"/api/area/jobs/{job_id}/editor/images",
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
        "old_root_path": config.get("old_root_path"),
        "result_output_root": config.get("result_output_root"),
        "model_mapping": model_mapping,
        "model_options": model_options,
        "inference_defaults": config.get("inference_defaults", {}),
        "archive_last_run_at": config.get("archive_last_run_at"),
        "archive_enabled": bool(config.get("archive_enabled")),
    }


@router.put("/config")
def update_area_config(payload: AreaConfigPayload, db: Session = Depends(get_db)):
    _ensure_enabled()
    root_path = payload.root_path.strip()
    old_root_path = payload.old_root_path.strip()
    result_output_root = payload.result_output_root.strip()
    if not root_path:
        raise HTTPException(status_code=400, detail="invalid_root_path")
    if not old_root_path:
        raise HTTPException(status_code=400, detail="invalid_old_root_path")
    if not result_output_root:
        raise HTTPException(status_code=400, detail="invalid_output_root")
    if not payload.model_mapping:
        raise HTTPException(status_code=400, detail="invalid_model_mapping")
    updated = area_crud.update_area_config(
        db,
        root_path,
        old_root_path,
        result_output_root,
        payload.model_mapping,
        payload.inference_defaults,
        payload.archive_enabled,
    )
    return {
        "root_path": updated.get("root_path"),
        "old_root_path": updated.get("old_root_path"),
        "result_output_root": updated.get("result_output_root"),
        "model_mapping": updated.get("model_mapping"),
        "model_options": sorted((updated.get("model_mapping") or {}).keys()),
        "inference_defaults": updated.get("inference_defaults", {}),
        "archive_last_run_at": updated.get("archive_last_run_at"),
        "archive_enabled": bool(updated.get("archive_enabled")),
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
            output_root=str(config.get("result_output_root") or settings.AREA_OUTPUT_DIR),
            default_inference_options=dict(config.get("inference_defaults") or {}),
            inference_options=payload.inference_options,
            infer_url=settings.AREA_INFER_URL,
            infer_timeout_sec=settings.AREA_INFER_TIMEOUT_SEC,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="area_job_create_failed") from exc
    return _with_artifact_urls(job)


@router.get("/jobs")
def list_area_jobs(
    limit: int = Query(200, ge=1, le=1000),
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(5, ge=1, le=1000),
):
    _ensure_enabled()
    payload = area_job_manager.list_jobs(limit=limit, query=q, page=page, page_size=page_size)
    items = [_with_artifact_urls(item) for item in payload["items"]]
    return {
        "items": items,
        "total": payload["total"],
        "page": payload["page"],
        "page_size": payload["page_size"],
    }


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
    try:
        path = area_job_manager.get_excel_path(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if path is None:
        raise HTTPException(status_code=404, detail="result_not_found")
    suffix = path.suffix.lower()
    media_type = "application/octet-stream"
    if suffix == ".xls":
        media_type = "application/vnd.ms-excel"
    elif suffix == ".xlsx":
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return FileResponse(
        path=path,
        media_type=media_type,
        filename=path.name,
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


@router.get("/jobs/{job_id}/editor/images")
def list_area_editor_images(
    job_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(5, ge=1, le=200),
):
    _ensure_enabled()
    payload = area_job_manager.list_editor_images(job_id, page=page, page_size=page_size)
    if payload is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    items = []
    for item in payload["items"]:
        image_id = item.get("image_id")
        row = dict(item)
        row["editor_url"] = f"/api/area/jobs/{job_id}/editor/images/{image_id}"
        items.append(row)
    return {
        "items": items,
        "total": payload["total"],
        "page": payload["page"],
        "page_size": payload["page_size"],
    }


@router.get("/jobs/{job_id}/editor/images/{image_id}")
def get_area_editor_image(job_id: str, image_id: int):
    _ensure_enabled()
    payload = area_job_manager.get_editor_image(job_id, image_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="image_not_found")
    image = payload.get("image") or {}
    overlay_filename = image.get("overlay_filename") or ""
    image["overlay_url"] = (
        f"/api/area/jobs/{job_id}/artifacts/image/{overlay_filename}"
        if overlay_filename
        else ""
    )
    payload["image"] = image
    return payload


@router.put("/jobs/{job_id}/editor/images/{image_id}")
def save_area_editor_image(job_id: str, image_id: int, payload: AreaEditorSavePayload):
    _ensure_enabled()
    try:
        return area_job_manager.save_editor_image(
            job_id=job_id,
            image_id=image_id,
            instances_payload=payload.instances,
            edited_by_id=payload.edited_by_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/editor/images/{image_id}/reset")
def reset_area_editor_image(job_id: str, image_id: int, payload: AreaEditorResetPayload):
    _ensure_enabled()
    try:
        return area_job_manager.reset_editor_image(
            job_id=job_id,
            image_id=image_id,
            edited_by_id=payload.edited_by_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/folders/search")
def search_area_folders(
    q: str = Query(..., min_length=1, max_length=255),
    limit: int = Query(5, ge=1, le=100),
    db: Session = Depends(get_db),
):
    _ensure_enabled()
    config = area_crud.get_area_config(db)
    try:
        items = area_job_manager.search_folders(str(config.get("root_path") or ""), q, limit=limit)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": items, "query": q, "limit": limit}


@router.get("/folders/recent")
def list_area_recent_folders(
    limit: int = Query(5, ge=1, le=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(5, ge=1, le=100),
    db: Session = Depends(get_db),
):
    _ensure_enabled()
    config = area_crud.get_area_config(db)
    try:
        return area_job_manager.list_recent_folders(
            str(config.get("root_path") or ""),
            limit=limit,
            page=page,
            page_size=page_size,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/folders/{folder_name}/images")
def list_area_folder_images(
    folder_name: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=200),
    db: Session = Depends(get_db),
):
    _ensure_enabled()
    if "/" in folder_name or "\\" in folder_name:
        raise HTTPException(status_code=400, detail="invalid_folder_name")
    config = area_crud.get_area_config(db)
    try:
        payload = area_job_manager.list_folder_images(
            str(config.get("root_path") or ""),
            folder_name,
            page=page,
            page_size=page_size,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    items = []
    for item in payload["items"]:
        name = item.get("name") or ""
        items.append(
            {
                "name": name,
                "url": f"/api/area/folders/{folder_name}/image/{name}",
            }
        )
    return {
        "items": items,
        "total": payload["total"],
        "page": payload["page"],
        "page_size": payload["page_size"],
    }


@router.get("/folders/{folder_name}/image/{filename}")
def get_area_folder_image(folder_name: str, filename: str, db: Session = Depends(get_db)):
    _ensure_enabled()
    if "/" in folder_name or "\\" in folder_name:
        raise HTTPException(status_code=400, detail="invalid_folder_name")
    if "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="invalid_filename")
    config = area_crud.get_area_config(db)
    try:
        path = area_job_manager.get_folder_image_path(
            str(config.get("root_path") or ""),
            folder_name,
            filename,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    media_type = "image/png"
    suffix = Path(filename).suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        media_type = "image/jpeg"
    return FileResponse(path=path, media_type=media_type, filename=Path(filename).name)


@router.post("/folders/{folder_name}/cleanup")
def cleanup_area_folder(
    folder_name: str,
    payload: AreaFolderCleanupPayload,
    db: Session = Depends(get_db),
):
    _ensure_enabled()
    if "/" in folder_name or "\\" in folder_name:
        raise HTTPException(status_code=400, detail="invalid_folder_name")
    config = area_crud.get_area_config(db)
    try:
        return area_job_manager.cleanup_folder(
            root_path=str(config.get("root_path") or ""),
            folder_name=folder_name,
            rename_enabled=payload.rename_enabled,
            new_folder_name=payload.new_folder_name,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        code = str(exc)
        status_code = 400
        if code == "rename_target_exists":
            status_code = 409
        raise HTTPException(status_code=status_code, detail=code) from exc


@router.get("/archive/status")
def get_area_archive_status(db: Session = Depends(get_db)):
    _ensure_enabled()
    config = area_crud.get_area_config(db)
    enabled = bool(config.get("archive_enabled"))
    last_run = area_crud.get_archive_last_run_at(db)
    now = datetime.now(timezone.utc)
    due = last_run is None or (now - last_run) >= timedelta(hours=48)
    return {
        "enabled": enabled,
        "last_run_at": last_run.isoformat() if last_run else None,
        "is_due": due,
        "interval_hours": 48,
        "next_due_at": (last_run + timedelta(hours=48)).isoformat() if last_run else now.isoformat(),
    }


@router.post("/archive/run")
def run_area_archive(db: Session = Depends(get_db)):
    _ensure_enabled()
    config = area_crud.get_area_config(db)
    try:
        result = area_job_manager.run_archive(
            root_path=str(config.get("root_path") or ""),
            old_root_path=str(config.get("old_root_path") or ""),
            older_than_hours=24,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    now = datetime.now(timezone.utc)
    area_crud.set_archive_last_run_at(db, now)
    return {
        "status": "ok",
        "ran_at": now.isoformat(),
        **result,
    }
