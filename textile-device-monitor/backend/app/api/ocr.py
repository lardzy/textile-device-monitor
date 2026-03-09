from __future__ import annotations

from pathlib import Path
import re
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import settings
from app.services.ocr_jobs import ocr_job_manager

router = APIRouter(prefix="/ocr", tags=["ocr"])

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
INVALID_FILENAME_CHARS = re.compile(r"[^\w.\- ]+")


class OcrDocxExportRequest(BaseModel):
    job_ids: list[str]


def _ensure_enabled() -> None:
    if not settings.OCR_ENABLED:
        raise HTTPException(status_code=503, detail="ocr_disabled")


def _normalize_filename(name: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", Path(name).name.strip())
    return cleaned or "upload"


def _validate_extension(filename: str) -> None:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="invalid_file_type")


async def _save_upload_file(file: UploadFile) -> tuple[Path, str]:
    filename = _normalize_filename(file.filename or "upload")
    _validate_extension(filename)

    upload_dir = Path(settings.OCR_UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)

    upload_name = f"{uuid4().hex}_{filename}"
    upload_path = upload_dir / upload_name
    max_bytes = max(1, settings.OCR_MAX_UPLOAD_MB) * 1024 * 1024

    total_size = 0
    try:
        with upload_path.open("wb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > max_bytes:
                    raise HTTPException(status_code=413, detail="file_too_large")
                handle.write(chunk)
    except HTTPException:
        upload_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="ocr_inference_failed") from exc
    finally:
        await file.close()

    return upload_path, filename


@router.post("/jobs")
async def create_ocr_job(
    file: UploadFile = File(...),
    page_range: str | None = Form(None),
    note: str | None = Form(None),
):
    _ensure_enabled()
    upload_path, filename = await _save_upload_file(file)

    try:
        job = ocr_job_manager.create_job(
            upload_path=str(upload_path),
            original_filename=filename,
            page_range=page_range,
            note=note,
        )
    except Exception as exc:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="ocr_inference_failed") from exc

    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "original_filename": job.get("original_filename") or filename,
    }


@router.post("/jobs/batch")
async def create_ocr_jobs_batch(
    files: list[UploadFile] = File(...),
    page_range: str | None = Form(None),
    note: str | None = Form(None),
):
    _ensure_enabled()

    if not files:
        raise HTTPException(status_code=400, detail="empty_file_list")

    max_batch_files = max(1, settings.OCR_MAX_BATCH_FILES)
    if len(files) > max_batch_files:
        raise HTTPException(status_code=400, detail="too_many_files")

    saved_uploads: list[tuple[Path, str]] = []
    try:
        for file in files:
            upload_path, filename = await _save_upload_file(file)
            saved_uploads.append((upload_path, filename))
    except Exception:
        for upload_path, _ in saved_uploads:
            upload_path.unlink(missing_ok=True)
        raise

    created_jobs: list[dict[str, str | int]] = []
    try:
        for index, (upload_path, filename) in enumerate(saved_uploads, start=1):
            job = ocr_job_manager.create_job(
                upload_path=str(upload_path),
                original_filename=filename,
                page_range=page_range,
                note=note,
            )
            created_jobs.append(
                {
                    "job_id": job["job_id"],
                    "status": job["status"],
                    "original_filename": job.get("original_filename") or filename,
                    "upload_index": index,
                }
            )
    except Exception as exc:
        for upload_path, _ in saved_uploads[len(created_jobs) :]:
            upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="ocr_inference_failed") from exc

    return {
        "jobs": created_jobs,
        "total": len(created_jobs),
    }


@router.get("/jobs/{job_id}")
def get_ocr_job(job_id: str):
    _ensure_enabled()
    job = ocr_job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    return job


@router.get("/jobs/{job_id}/result")
def get_ocr_result(job_id: str):
    _ensure_enabled()
    job = ocr_job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    if job["status"] != "succeeded":
        if job["status"] == "failed":
            raise HTTPException(
                status_code=409,
                detail=job.get("error_code") or "ocr_inference_failed",
            )
        raise HTTPException(status_code=409, detail="job_not_completed")

    result = ocr_job_manager.get_result(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="result_not_found")

    result["artifact_urls"] = {
        "md": f"/api/ocr/jobs/{job_id}/artifacts/md",
        "json": f"/api/ocr/jobs/{job_id}/artifacts/json",
    }
    return result


@router.get("/jobs/{job_id}/artifacts/{kind}")
def download_artifact(job_id: str, kind: str):
    _ensure_enabled()
    if kind not in {"md", "json"}:
        raise HTTPException(status_code=400, detail="invalid_artifact_kind")

    artifact_path = ocr_job_manager.get_artifact_path(job_id, kind)
    if artifact_path is None:
        raise HTTPException(status_code=404, detail="result_not_found")

    media_type = "text/markdown; charset=utf-8" if kind == "md" else "application/json"
    filename = f"{job_id}.{kind}"
    return FileResponse(path=artifact_path, media_type=media_type, filename=filename)


@router.post("/jobs/export/docx")
def export_docx(payload: OcrDocxExportRequest):
    _ensure_enabled()
    try:
        artifact_path, filename = ocr_job_manager.export_jobs_docx(payload.job_ids)
    except ValueError as exc:
        detail = str(exc)
        if detail in {"empty_job_ids", "job_not_found", "job_not_completed"}:
            raise HTTPException(status_code=400, detail=detail) from exc
        raise HTTPException(status_code=500, detail="ocr_inference_failed") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="ocr_inference_failed") from exc

    return FileResponse(
        path=artifact_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
    )
