from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import queue
import shutil
import threading
from typing import Any
from uuid import uuid4

from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import or_

from app.config import settings
from app.database import SessionLocal
from app.models import AreaJob, AreaJobImage, AreaJobInstance
from app.services.area_infer import (
    DEFAULT_INFER_OPTIONS,
    AreaPredictor,
    parse_model_classes,
)

ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
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


@dataclass
class AreaJobRecord:
    job_id: str
    folder_name: str
    model_name: str
    root_path: str
    model_file: str
    weight_path: str
    output_dir: str
    overlay_dir: str
    result_json_path: str
    excel_path: str
    infer_url: str
    infer_timeout_sec: int
    inference_options: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    total_images: int = 0
    processed_images: int = 0
    succeeded_images: int = 0
    failed_images: int = 0
    result_summary: list[dict[str, Any]] = field(default_factory=list)
    result_rows: list[dict[str, Any]] = field(default_factory=list)
    image_items: list[dict[str, str]] = field(default_factory=list)
    engine_meta: dict[str, Any] = field(default_factory=dict)


class AreaJobManager:
    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue()
        self._jobs: dict[str, AreaJobRecord] = {}
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._workers: list[threading.Thread] = []
        self._started = False
        self._predictor = AreaPredictor(
            infer_url=settings.AREA_INFER_URL,
            timeout_sec=settings.AREA_INFER_TIMEOUT_SEC,
        )

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            Path(settings.AREA_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
            worker_count = max(1, int(settings.AREA_MAX_CONCURRENT_JOBS))
            self._shutdown_event.clear()
            self._workers = []
            for idx in range(worker_count):
                worker = threading.Thread(
                    target=self._worker_loop,
                    name=f"area-worker-{idx + 1}",
                    daemon=True,
                )
                worker.start()
                self._workers.append(worker)
            self._started = True

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._shutdown_event.set()
            workers = list(self._workers)
            self._workers = []
            self._started = False
        for worker in workers:
            worker.join(timeout=1.5)

    def create_job(
        self,
        folder_name: str,
        model_name: str,
        root_path: str,
        model_mapping: dict[str, str],
        weights_dir: str,
        output_root: str,
        default_inference_options: dict[str, Any] | None = None,
        inference_options: dict[str, Any] | None = None,
        infer_url: str | None = None,
        infer_timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        self.start()

        normalized_folder = folder_name.strip()
        normalized_root = root_path.strip()
        normalized_output_root = output_root.strip()
        if not normalized_folder:
            raise ValueError("invalid_folder_name")
        if "/" in normalized_folder or "\\" in normalized_folder:
            raise ValueError("invalid_folder_name")
        if normalized_folder in {".", ".."}:
            raise ValueError("invalid_folder_name")
        if not normalized_root:
            raise ValueError("invalid_root_path")
        if not normalized_output_root:
            raise ValueError("invalid_output_root")

        model_file = model_mapping.get(model_name)
        if not model_file:
            raise ValueError("invalid_model_name")

        defaults = default_inference_options if isinstance(default_inference_options, dict) else DEFAULT_INFER_OPTIONS
        merged_options = dict(defaults) | dict(inference_options or {})
        normalized_options = self._normalize_inference_options(merged_options)

        normalized_infer_url = str(infer_url or settings.AREA_INFER_URL).strip()
        normalized_infer_timeout = max(1, int(infer_timeout_sec or settings.AREA_INFER_TIMEOUT_SEC))
        try:
            target_folder = self._resolve_target_folder(normalized_root, normalized_folder)
        except FileNotFoundError as exc:
            raise ValueError(str(exc)) from exc

        image_paths = [
            path
            for path in target_folder.iterdir()
            if path.is_file() and path.suffix.lower() in ALLOWED_IMAGE_SUFFIXES
        ]
        if not image_paths:
            raise ValueError("empty_image_list")

        try:
            self._predictor.check_service_health(
                infer_url=normalized_infer_url,
                timeout_sec=normalized_infer_timeout,
            )
            self._predictor.warmup_model(
                model_name=model_name,
                model_file=model_file,
                infer_url=normalized_infer_url,
                timeout_sec=normalized_infer_timeout,
            )
        except RuntimeError as exc:
            code = str(exc).strip() or "infer_service_unavailable"
            if code not in {
                "infer_service_unavailable",
                "infer_model_load_failed",
                "infer_timeout",
                "infer_bad_response",
            }:
                code = "infer_service_unavailable"
            raise ValueError(code) from exc

        job_id = uuid4().hex
        output_dir = Path(normalized_output_root) / job_id
        overlay_dir = output_dir / "overlays"
        output_dir.mkdir(parents=True, exist_ok=True)
        overlay_dir.mkdir(parents=True, exist_ok=True)

        record = AreaJobRecord(
            job_id=job_id,
            folder_name=normalized_folder,
            model_name=model_name,
            root_path=normalized_root,
            model_file=model_file,
            weight_path=str((Path(weights_dir) / model_file).resolve()),
            output_dir=str(output_dir),
            overlay_dir=str(overlay_dir),
            result_json_path=str(output_dir / "result.json"),
            excel_path=str(output_dir / "result.xlsx"),
            infer_url=normalized_infer_url,
            infer_timeout_sec=normalized_infer_timeout,
            inference_options=normalized_options,
        )

        self._create_job_row(record)
        with self._lock:
            self._jobs[job_id] = record
        self._queue.put(job_id)

        payload = self.get_job(job_id)
        if payload is None:
            raise RuntimeError("failed_to_create_job")
        return payload

    def list_jobs(
        self,
        limit: int = 200,
        query: str | None = None,
        page: int = 1,
        page_size: int = 5,
    ) -> dict[str, Any]:
        db = SessionLocal()
        try:
            q = db.query(AreaJob)
            if query:
                pattern = f"%{query.strip()}%"
                q = q.filter(
                    or_(
                        AreaJob.job_id.ilike(pattern),
                        AreaJob.folder_name.ilike(pattern),
                        AreaJob.model_name.ilike(pattern),
                    )
                )
            q = q.order_by(AreaJob.created_at.desc())
            total = q.count()
            effective_page_size = max(1, min(page_size, limit, 1000))
            start = max(0, (max(1, page) - 1) * effective_page_size)
            rows = q.offset(start).limit(effective_page_size).all()
            items = [self._serialize_job_row(row) for row in rows]
            return {
                "items": items,
                "total": total,
                "page": max(1, page),
                "page_size": effective_page_size,
            }
        finally:
            db.close()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        db = SessionLocal()
        try:
            row = db.query(AreaJob).filter(AreaJob.job_id == job_id).first()
            if row is None:
                return None
            return self._serialize_job_row(row)
        finally:
            db.close()

    def get_result(self, job_id: str) -> dict[str, Any] | None:
        db = SessionLocal()
        try:
            row = db.query(AreaJob).filter(AreaJob.job_id == job_id).first()
            if row is None:
                return None
            summary = row.summary_json if isinstance(row.summary_json, list) else []
            details = row.detail_json if isinstance(row.detail_json, list) else []
            return {
                "job_id": row.job_id,
                "status": row.status,
                "engine_meta": row.engine_meta if isinstance(row.engine_meta, dict) else {},
                "summary": summary,
                "per_image": details,
            }
        finally:
            db.close()

    def get_excel_path(self, job_id: str) -> Path | None:
        db = SessionLocal()
        try:
            job = db.query(AreaJob).filter(AreaJob.job_id == job_id).first()
            if job is None:
                return None
            self._rebuild_job_outputs(db, job)
            path = Path(job.excel_path)
            if not path.exists():
                return None
            return path
        finally:
            db.close()

    def get_overlay_image_path(self, job_id: str, filename: str) -> Path | None:
        db = SessionLocal()
        try:
            job = db.query(AreaJob).filter(AreaJob.job_id == job_id).first()
            if job is None:
                return None
            path = Path(job.overlay_dir) / filename
            if not path.exists() or not path.is_file():
                image_row = (
                    db.query(AreaJobImage)
                    .filter(
                        AreaJobImage.job_id == job.id,
                        AreaJobImage.overlay_filename == filename,
                    )
                    .first()
                )
                if image_row is not None and not (image_row.error_message or "").strip():
                    rows = (
                        db.query(AreaJobInstance)
                        .filter(AreaJobInstance.image_id == image_row.id)
                        .order_by(AreaJobInstance.sort_index.asc(), AreaJobInstance.id.asc())
                        .all()
                    )
                    self._render_overlay_for_image(job, image_row, rows)
                if not path.exists() or not path.is_file():
                    return None
            return path
        finally:
            db.close()

    def list_overlay_images(
        self, job_id: str, page: int = 1, page_size: int = 50
    ) -> dict[str, Any] | None:
        db = SessionLocal()
        try:
            job = db.query(AreaJob).filter(AreaJob.job_id == job_id).first()
            if job is None:
                return None
            q = db.query(AreaJobImage).filter(AreaJobImage.job_id == job.id).order_by(AreaJobImage.image_name.asc())
            total = q.count()
            start = max(0, (max(1, page) - 1) * max(1, page_size))
            rows = q.offset(start).limit(max(1, page_size)).all()
            items = [
                {
                    "image_id": row.id,
                    "image_name": row.image_name,
                    "overlay_filename": row.overlay_filename,
                }
                for row in rows
            ]
            return {
                "items": items,
                "total": total,
                "page": max(1, page),
                "page_size": max(1, page_size),
            }
        finally:
            db.close()

    def list_editor_images(
        self,
        job_id: str,
        page: int = 1,
        page_size: int = 5,
    ) -> dict[str, Any] | None:
        db = SessionLocal()
        try:
            job = db.query(AreaJob).filter(AreaJob.job_id == job_id).first()
            if job is None:
                return None
            q = db.query(AreaJobImage).filter(AreaJobImage.job_id == job.id).order_by(AreaJobImage.image_name.asc())
            total = q.count()
            start = max(0, (max(1, page) - 1) * max(1, page_size))
            rows = q.offset(start).limit(max(1, page_size)).all()
            items = [
                {
                    "image_id": row.id,
                    "image_name": row.image_name,
                    "overlay_filename": row.overlay_filename,
                    "edited_at": row.edited_at.isoformat() if row.edited_at else None,
                    "edited_by_id": row.edited_by_id,
                    "edit_version": int(row.edit_version or 0),
                    "error": row.error_message or "",
                }
                for row in rows
            ]
            return {
                "items": items,
                "total": total,
                "page": max(1, page),
                "page_size": max(1, page_size),
            }
        finally:
            db.close()

    def get_editor_image(self, job_id: str, image_id: int) -> dict[str, Any] | None:
        db = SessionLocal()
        try:
            job = db.query(AreaJob).filter(AreaJob.job_id == job_id).first()
            if job is None:
                return None
            image_row = (
                db.query(AreaJobImage)
                .filter(AreaJobImage.id == image_id, AreaJobImage.job_id == job.id)
                .first()
            )
            if image_row is None:
                return None
            instances = (
                db.query(AreaJobInstance)
                .filter(AreaJobInstance.image_id == image_row.id)
                .order_by(AreaJobInstance.sort_index.asc(), AreaJobInstance.id.asc())
                .all()
            )
            overlay_filename = str(image_row.overlay_filename or "")
            overlay_exists = False
            if overlay_filename:
                overlay_path = Path(job.overlay_dir) / overlay_filename
                if not overlay_path.exists() or not overlay_path.is_file():
                    self._render_overlay_for_image(job, image_row, instances)
                overlay_exists = overlay_path.exists() and overlay_path.is_file()
            if not overlay_exists:
                overlay_filename = ""
            return {
                "job_id": job.job_id,
                "job_created_at": job.created_at.isoformat() if job.created_at else None,
                "job_updated_at": job.updated_at.isoformat() if job.updated_at else None,
                "image": {
                    "image_id": image_row.id,
                    "image_name": image_row.image_name,
                    "source_image_path": image_row.source_image_path,
                    "overlay_filename": overlay_filename,
                    "overlay_exists": overlay_exists,
                    "width": int(image_row.width or 0),
                    "height": int(image_row.height or 0),
                    "edited_at": image_row.edited_at.isoformat() if image_row.edited_at else None,
                    "edited_by_id": image_row.edited_by_id,
                    "edit_version": int(image_row.edit_version or 0),
                },
                "instances": [self._serialize_instance(row) for row in instances],
            }
        finally:
            db.close()

    def save_editor_image(
        self,
        job_id: str,
        image_id: int,
        instances_payload: list[dict[str, Any]],
        edited_by_id: str,
    ) -> dict[str, Any]:
        db = SessionLocal()
        try:
            job = db.query(AreaJob).filter(AreaJob.job_id == job_id).first()
            if job is None:
                raise ValueError("job_not_found")
            image_row = (
                db.query(AreaJobImage)
                .filter(AreaJobImage.id == image_id, AreaJobImage.job_id == job.id)
                .first()
            )
            if image_row is None:
                raise ValueError("image_not_found")
            db_instances = (
                db.query(AreaJobInstance)
                .filter(AreaJobInstance.image_id == image_row.id)
                .order_by(AreaJobInstance.id.asc())
                .all()
            )
            inst_map = {item.id: item for item in db_instances}
            for payload in instances_payload:
                instance_id = int(payload.get("instance_id") or 0)
                row = inst_map.get(instance_id)
                if row is None:
                    continue
                row.is_deleted = bool(payload.get("is_deleted", False))
                polygon = self._normalize_polygon(payload.get("polygon"))
                bbox = self._normalize_bbox(payload.get("bbox"), image_row.width, image_row.height)
                if polygon:
                    row.polygon = polygon
                if bbox:
                    row.bbox = bbox
                if row.polygon:
                    row.area_px = self._polygon_area_px(row.polygon, image_row.width, image_row.height)
                else:
                    row.area_px = self._bbox_area_px(row.bbox)
                if row.is_deleted:
                    row.area_px = 0

            image_row.edited_by_id = edited_by_id.strip()[:64] if edited_by_id else ""
            image_row.edited_at = datetime.now(timezone.utc)
            image_row.edit_version = int(image_row.edit_version or 0) + 1
            self._refresh_image_area_stats(db, image_row)
            self._rerender_all_overlays_for_job(db, job)
            self._rebuild_job_outputs(db, job)
            db.commit()
            return {
                "status": "ok",
                "image_id": image_row.id,
                "edit_version": int(image_row.edit_version or 0),
            }
        finally:
            db.close()

    def reset_editor_image(
        self,
        job_id: str,
        image_id: int,
        edited_by_id: str,
    ) -> dict[str, Any]:
        db = SessionLocal()
        try:
            job = db.query(AreaJob).filter(AreaJob.job_id == job_id).first()
            if job is None:
                raise ValueError("job_not_found")
            image_row = (
                db.query(AreaJobImage)
                .filter(AreaJobImage.id == image_id, AreaJobImage.job_id == job.id)
                .first()
            )
            if image_row is None:
                raise ValueError("image_not_found")
            db_instances = (
                db.query(AreaJobInstance)
                .filter(AreaJobInstance.image_id == image_row.id)
                .order_by(AreaJobInstance.id.asc())
                .all()
            )
            for row in db_instances:
                row.bbox = list(row.initial_bbox or [])
                row.polygon = list(row.initial_polygon or [])
                row.is_deleted = bool(row.initial_is_deleted)
                row.area_px = int(row.initial_area_px or 0)
            image_row.edited_by_id = edited_by_id.strip()[:64] if edited_by_id else ""
            image_row.edited_at = datetime.now(timezone.utc)
            image_row.edit_version = int(image_row.edit_version or 0) + 1
            self._refresh_image_area_stats(db, image_row)
            self._rerender_all_overlays_for_job(db, job)
            self._rebuild_job_outputs(db, job)
            db.commit()
            return {
                "status": "ok",
                "image_id": image_row.id,
                "edit_version": int(image_row.edit_version or 0),
            }
        finally:
            db.close()

    def search_folders(
        self,
        root_path: str,
        query: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        normalized_query = str(query or "").strip().lower()
        if not normalized_query:
            return []
        roots = self._existing_roots(root_path)
        if not roots:
            raise FileNotFoundError("root_path_not_found")

        matches: list[tuple[float, Path]] = []
        seen: set[str] = set()
        for root in roots:
            try:
                entries = list(root.iterdir())
            except OSError:
                continue
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                name = entry.name
                if normalized_query not in name.lower():
                    continue
                key = str(entry.resolve())
                if key in seen:
                    continue
                seen.add(key)
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    mtime = 0.0
                matches.append((mtime, entry))
        matches.sort(key=lambda item: item[0], reverse=True)
        max_items = max(1, min(int(limit or 5), 100))
        result: list[dict[str, Any]] = []
        for mtime, entry in matches[:max_items]:
            result.append(
                {
                    "folder_name": entry.name,
                    "updated_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                    "image_count": self._count_images_in_dir(entry),
                }
            )
        return result

    def list_recent_folders(
        self,
        root_path: str,
        limit: int = 5,
        page: int = 1,
        page_size: int = 5,
    ) -> dict[str, Any]:
        roots = self._existing_roots(root_path)
        if not roots:
            raise FileNotFoundError("root_path_not_found")

        rows: list[tuple[float, Path]] = []
        seen: set[str] = set()
        for root in roots:
            try:
                entries = list(root.iterdir())
            except OSError:
                continue
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                key = str(entry.resolve())
                if key in seen:
                    continue
                seen.add(key)
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                rows.append((mtime, entry))
        rows.sort(key=lambda item: item[0], reverse=True)

        max_limit = max(1, min(int(limit or 5), 100))
        scoped = rows[:max_limit]
        effective_page = max(1, int(page or 1))
        effective_page_size = max(1, min(int(page_size or 5), 100))
        start = (effective_page - 1) * effective_page_size
        chunk = scoped[start : start + effective_page_size]
        items = [
            {
                "folder_name": path.name,
                "updated_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                "image_count": self._count_images_in_dir(path),
            }
            for mtime, path in chunk
        ]
        return {
            "items": items,
            "total": len(scoped),
            "page": effective_page,
            "page_size": effective_page_size,
        }

    def list_folder_images(
        self,
        root_path: str,
        folder_name: str,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        folder = self._resolve_target_folder(root_path, folder_name)
        files: list[str] = []
        try:
            entries = list(folder.iterdir())
        except OSError:
            entries = []
        for item in entries:
            try:
                if item.is_file() and item.suffix.lower() in ALLOWED_IMAGE_SUFFIXES:
                    files.append(item.name)
            except OSError:
                continue
        files.sort(key=lambda name: name.lower())
        total = len(files)
        effective_page = max(1, int(page or 1))
        effective_page_size = max(1, min(int(page_size or 50), 200))
        start = (effective_page - 1) * effective_page_size
        chunk = files[start : start + effective_page_size]
        return {
            "items": [{"name": item} for item in chunk],
            "total": total,
            "page": effective_page,
            "page_size": effective_page_size,
        }

    def get_folder_image_path(self, root_path: str, folder_name: str, filename: str) -> Path:
        folder = self._resolve_target_folder(root_path, folder_name)
        path = folder / filename
        if not path.exists() or not path.is_file():
            raise FileNotFoundError("image_not_found")
        return path

    def cleanup_folder(
        self,
        root_path: str,
        folder_name: str,
        rename_enabled: bool = False,
        new_folder_name: str | None = None,
    ) -> dict[str, Any]:
        target = self._resolve_target_folder(root_path, folder_name)
        parent = target.parent
        if not parent.exists() or not parent.is_dir():
            raise ValueError("output_parent_missing")

        recycle_dir = parent / ".recycle"
        recycle_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        try:
            entries = list(target.iterdir())
        except OSError:
            entries = []
        for item in entries:
            if item.name in {".", "..", ".recycle"}:
                continue
            try:
                if not item.is_file() or item.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
                    continue
            except OSError:
                continue
            dst = recycle_dir / item.name
            if dst.exists():
                dst = recycle_dir / f"{item.stem}_{uuid4().hex[:8]}{item.suffix}"
            shutil.move(str(item), str(dst))
            moved += 1

        normalized_target = target.resolve()
        old_folder = normalized_target.name
        new_path = normalized_target
        renamed = False

        if rename_enabled:
            candidate_name = str(new_folder_name or "").strip()
            if not candidate_name:
                raise ValueError("rename_name_empty")
            if candidate_name in {".", ".."}:
                raise ValueError("rename_invalid_name")
            invalid_chars = set('\\/:*?"<>|')
            if any(ch in invalid_chars for ch in candidate_name):
                raise ValueError("rename_invalid_name")
            candidate_path = parent / candidate_name
            if candidate_path.exists() and candidate_path.resolve() != normalized_target:
                raise ValueError("rename_target_exists")
            if candidate_path.resolve() != normalized_target:
                target.rename(candidate_path)
                new_path = candidate_path.resolve()
                renamed = True

        return {
            "moved": moved,
            "rename_enabled": bool(rename_enabled),
            "renamed": renamed,
            "old_folder": old_folder,
            "new_folder": new_path.name,
            "old_path": str(normalized_target),
            "new_path": str(new_path),
            "recycle_dir": str(recycle_dir.resolve()),
        }

    def run_archive(
        self,
        root_path: str,
        old_root_path: str,
        older_than_hours: int = 24,
    ) -> dict[str, Any]:
        roots = self._existing_roots(root_path)
        if not roots:
            raise FileNotFoundError("root_path_not_found")
        old_root = Path(old_root_path.strip())
        if not old_root_path.strip():
            raise ValueError("invalid_old_root_path")
        old_root.mkdir(parents=True, exist_ok=True)

        threshold = datetime.now(timezone.utc) - timedelta(hours=max(1, int(older_than_hours or 24)))
        running_folders = self._running_folders()

        moved_items: list[dict[str, str]] = []
        failed_items: list[dict[str, str]] = []
        for root in roots:
            try:
                entries = list(root.iterdir())
            except OSError:
                continue
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                if entry.name.startswith(".") or entry.name == ".recycle":
                    continue
                if entry.name.startswith("_"):
                    continue
                if entry.name in running_folders:
                    continue
                try:
                    mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
                except OSError:
                    continue
                if mtime > threshold:
                    continue
                dest = old_root / entry.name
                if dest.exists():
                    dest = old_root / f"{entry.name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
                self._cleanup_mac_noise_files(entry)
                try:
                    shutil.move(str(entry), str(dest))
                    moved_items.append(
                        {
                            "from": str(entry),
                            "to": str(dest),
                        }
                    )
                except Exception as exc:
                    failed_items.append(
                        {
                            "from": str(entry),
                            "to": str(dest),
                            "error": str(exc),
                        }
                    )
        return {
            "moved_count": len(moved_items),
            "items": moved_items,
            "failed_count": len(failed_items),
            "failed_items": failed_items,
            "threshold_hours": max(1, int(older_than_hours or 24)),
        }

    def _serialize_job_row(self, row: AreaJob) -> dict[str, Any]:
        return {
            "job_id": row.job_id,
            "status": row.status,
            "folder_name": row.folder_name,
            "model_name": row.model_name,
            "model_file": row.model_file,
            "inference_options": row.inference_options if isinstance(row.inference_options, dict) else {},
            "infer_url": row.infer_url,
            "infer_timeout_sec": row.infer_timeout_sec,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "error_code": row.error_code,
            "error_message": row.error_message,
            "total_images": int(row.total_images or 0),
            "processed_images": int(row.processed_images or 0),
            "succeeded_images": int(row.succeeded_images or 0),
            "failed_images": int(row.failed_images or 0),
        }

    def _serialize_instance(self, row: AreaJobInstance) -> dict[str, Any]:
        return {
            "instance_id": row.id,
            "class_name": row.class_name,
            "score": float(row.score) if row.score is not None else None,
            "bbox": list(row.bbox or []),
            "polygon": list(row.polygon or []),
            "area_px": int(row.area_px or 0),
            "is_deleted": bool(row.is_deleted),
            "sort_index": int(row.sort_index or 0),
        }

    def _worker_loop(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._run_job(job_id)
            finally:
                self._queue.task_done()

    def _set_job_failed(self, record: AreaJobRecord, code: str, message: str) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            record.status = "failed"
            record.error_code = code
            record.error_message = message
            record.finished_at = now
        self._update_job_row(
            record.job_id,
            status="failed",
            error_code=code,
            error_message=message,
            finished_at=now,
        )

    def _normalize_inference_options(
        self,
        options: dict[str, Any] | None,
    ) -> dict[str, Any]:
        normalized = dict(DEFAULT_INFER_OPTIONS)
        if not isinstance(options, dict):
            return normalized

        try:
            if "threshold_bias" in options:
                value = int(options.get("threshold_bias", 0))
                normalized["threshold_bias"] = max(-128, min(128, value))

            if "mask_mode" in options:
                mode = str(options.get("mask_mode", "auto")).strip().lower()
                if mode not in {"auto", "dark", "light"}:
                    raise ValueError("invalid_mask_mode")
                normalized["mask_mode"] = mode

            if "smooth_min_neighbors" in options:
                value = int(options.get("smooth_min_neighbors", 3))
                normalized["smooth_min_neighbors"] = max(1, min(5, value))

            if "min_pixels" in options:
                value = int(options.get("min_pixels", 64))
                normalized["min_pixels"] = max(1, min(100000, value))

            if "overlay_alpha" in options:
                value = float(options.get("overlay_alpha", 0.45))
                normalized["overlay_alpha"] = max(0.05, min(0.95, value))

            if "score_threshold" in options:
                value = float(options.get("score_threshold", 0.15))
                if value < 0.0 or value > 1.0:
                    raise ValueError("invalid_score_threshold")
                normalized["score_threshold"] = value

            if "top_k" in options:
                value = int(options.get("top_k", 200))
                if value < 1 or value > 1000:
                    raise ValueError("invalid_top_k")
                normalized["top_k"] = value

            if "nms_top_k" in options:
                value = int(options.get("nms_top_k", 200))
                if value < 1 or value > 1000:
                    raise ValueError("invalid_nms_top_k")
                normalized["nms_top_k"] = value

            if "nms_conf_thresh" in options:
                value = float(options.get("nms_conf_thresh", 0.05))
                if value < 0.0 or value > 1.0:
                    raise ValueError("invalid_nms_conf_thresh")
                normalized["nms_conf_thresh"] = value

            if "nms_thresh" in options:
                value = float(options.get("nms_thresh", 0.5))
                if value < 0.0 or value > 1.0:
                    raise ValueError("invalid_nms_thresh")
                normalized["nms_thresh"] = value
        except (TypeError, ValueError) as exc:
            if str(exc) == "invalid_mask_mode":
                raise ValueError("invalid_inference_options") from exc
            raise ValueError("invalid_inference_options") from exc

        return normalized

    def _root_path_candidates(self, root_path: str) -> list[Path]:
        raw = root_path.strip()
        if not raw:
            return []
        candidates: list[Path] = [Path(raw)]
        if "\\" in raw:
            normalized = raw.replace("\\", "/")
            if normalized and normalized != raw:
                candidates.append(Path(normalized))
        deduped: list[Path] = []
        seen: set[str] = set()
        for item in candidates:
            key = str(item)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _existing_roots(self, root_path: str) -> list[Path]:
        roots = self._root_path_candidates(root_path)
        if not roots:
            return []
        return [root for root in roots if root.exists() and root.is_dir()]

    def _resolve_target_folder(self, root_path: str, folder_name: str) -> Path:
        roots = self._existing_roots(root_path)
        if not roots:
            raise FileNotFoundError("root_path_not_found")
        for root in roots:
            target = root / folder_name
            if target.exists() and target.is_dir():
                return target
        raise FileNotFoundError("folder_not_found")

    def _count_images_in_dir(self, folder: Path) -> int:
        count = 0
        try:
            entries = list(folder.iterdir())
        except OSError:
            return 0
        for item in entries:
            try:
                if item.is_file() and item.suffix.lower() in ALLOWED_IMAGE_SUFFIXES:
                    count += 1
            except OSError:
                continue
        return count

    def _cleanup_mac_noise_files(self, folder: Path) -> None:
        try:
            for path in folder.rglob("*"):
                if not path.is_file():
                    continue
                name = path.name
                if name.startswith("._") or name == ".DS_Store":
                    try:
                        path.unlink()
                    except OSError:
                        continue
        except Exception:
            return

    def _create_job_row(self, record: AreaJobRecord) -> None:
        db = SessionLocal()
        try:
            row = AreaJob(
                job_id=record.job_id,
                folder_name=record.folder_name,
                model_name=record.model_name,
                model_file=record.model_file,
                root_path=record.root_path,
                output_dir=record.output_dir,
                overlay_dir=record.overlay_dir,
                result_json_path=record.result_json_path,
                excel_path=record.excel_path,
                infer_url=record.infer_url,
                infer_timeout_sec=record.infer_timeout_sec,
                inference_options=record.inference_options,
                status=record.status,
                error_code=record.error_code,
                error_message=record.error_message,
                total_images=record.total_images,
                processed_images=record.processed_images,
                succeeded_images=record.succeeded_images,
                failed_images=record.failed_images,
                engine_meta=record.engine_meta,
                summary_json=record.result_summary,
                detail_json=record.result_rows,
                created_at=record.created_at,
            )
            db.add(row)
            db.commit()
        finally:
            db.close()

    def _update_job_row(self, job_id: str, **fields: Any) -> None:
        db = SessionLocal()
        try:
            row = db.query(AreaJob).filter(AreaJob.job_id == job_id).first()
            if row is None:
                return
            for key, value in fields.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            db.commit()
        finally:
            db.close()

    def _build_excel(
        self,
        summary_rows: list[dict[str, Any]],
        detail_rows: list[dict[str, Any]],
        excel_path: Path,
    ) -> None:
        wb = Workbook()
        ws_summary = wb.active
        ws_summary.title = "summary"
        ws_summary.append(
            ["class_name", "total_area_px", "ratio_percent", "image_count"]
        )
        for row in summary_rows:
            ws_summary.append(
                [
                    row["class_name"],
                    int(row["total_area_px"]),
                    float(row["ratio_percent"]),
                    int(row["image_count"]),
                ]
            )

        ws_detail = wb.create_sheet("per_image")
        ws_detail.append(
            [
                "image_name",
                "class_name",
                "instance_count",
                "area_px",
                "ratio_percent",
                "overlay_filename",
                "error",
            ]
        )
        for row in detail_rows:
            ws_detail.append(
                [
                    row.get("image_name", ""),
                    row.get("class_name", ""),
                    int(row.get("instance_count", 0)),
                    int(row.get("area_px", 0)),
                    float(row.get("ratio_percent", 0.0)),
                    row.get("overlay_filename", ""),
                    row.get("error", ""),
                ]
            )
        excel_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(excel_path)
        wb.close()

    def _normalize_polygon(self, raw: Any) -> list[list[int]]:
        if not isinstance(raw, list):
            return []
        out: list[list[int]] = []
        for point in raw:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                continue
            try:
                px = int(point[0])
                py = int(point[1])
            except (TypeError, ValueError):
                continue
            out.append([px, py])
        return out if len(out) >= 3 else []

    def _normalize_bbox(self, raw: Any, width: int, height: int) -> list[int]:
        if not isinstance(raw, (list, tuple)) or len(raw) != 4:
            return []
        try:
            x1 = int(raw[0])
            y1 = int(raw[1])
            x2 = int(raw[2])
            y2 = int(raw[3])
        except (TypeError, ValueError):
            return []
        x1 = max(0, min(x1, max(0, width - 1)))
        y1 = max(0, min(y1, max(0, height - 1)))
        x2 = max(x1, min(x2, max(0, width - 1)))
        y2 = max(y1, min(y2, max(0, height - 1)))
        return [x1, y1, x2, y2]

    def _bbox_area_px(self, bbox: Any) -> int:
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            return 0
        try:
            x1 = int(bbox[0])
            y1 = int(bbox[1])
            x2 = int(bbox[2])
            y2 = int(bbox[3])
        except (TypeError, ValueError):
            return 0
        return max(0, x2 - x1 + 1) * max(0, y2 - y1 + 1)

    def _polygon_area_px(self, polygon: list[list[int]], width: int, height: int) -> int:
        if len(polygon) < 3 or width <= 0 or height <= 0:
            return 0
        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        try:
            draw.polygon([(int(p[0]), int(p[1])) for p in polygon], fill=255)
        except Exception:
            return 0
        data = mask.getdata()
        return sum(1 for value in data if value > 0)

    def _refresh_image_area_stats(self, db, image_row: AreaJobImage) -> None:
        rows = (
            db.query(AreaJobInstance)
            .filter(AreaJobInstance.image_id == image_row.id)
            .order_by(AreaJobInstance.id.asc())
            .all()
        )
        per_class: dict[str, int] = {}
        total = 0
        for row in rows:
            if row.is_deleted:
                continue
            area = max(0, int(row.area_px or 0))
            per_class[row.class_name] = per_class.get(row.class_name, 0) + area
            total += area
        image_row.per_class_area_px = per_class
        image_row.total_area_px = total

    def _render_overlay_for_image(self, job: AreaJob, image_row: AreaJobImage, rows: list[AreaJobInstance]) -> None:
        source_path = Path(image_row.source_image_path)
        if not source_path.exists():
            return
        try:
            base = Image.open(source_path).convert("RGBA")
        except Exception:
            return

        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay, "RGBA")
        classes = parse_model_classes(job.model_name)
        class_to_color: dict[str, tuple[int, int, int]] = {}
        for idx, name in enumerate(classes):
            class_to_color[name] = PALETTE[idx % len(PALETTE)]
        font = ImageFont.load_default()

        for item in sorted(rows, key=lambda r: (int(r.sort_index or 0), int(r.id or 0))):
            cls_name = item.class_name
            color = class_to_color.get(cls_name, PALETTE[0])
            alpha = 20 if item.is_deleted else 110
            outline_alpha = 90 if item.is_deleted else 220
            polygon = item.polygon if isinstance(item.polygon, list) else []
            bbox = item.bbox if isinstance(item.bbox, list) else []

            if len(polygon) >= 3:
                points = [(int(p[0]), int(p[1])) for p in polygon if isinstance(p, (list, tuple)) and len(p) == 2]
                if len(points) >= 3:
                    draw.polygon(points, fill=(color[0], color[1], color[2], alpha), outline=(color[0], color[1], color[2], outline_alpha))
                    x1 = min(p[0] for p in points)
                    y1 = min(p[1] for p in points)
                else:
                    x1 = int(bbox[0]) if len(bbox) == 4 else 0
                    y1 = int(bbox[1]) if len(bbox) == 4 else 0
            else:
                if len(bbox) == 4:
                    x1, y1, x2, y2 = [int(v) for v in bbox]
                    draw.rectangle([x1, y1, x2, y2], outline=(color[0], color[1], color[2], outline_alpha), width=1)
                else:
                    x1 = 0
                    y1 = 0

            code = "C1"
            if cls_name in classes:
                code = f"C{classes.index(cls_name) + 1}"
            draw.text((x1 + 2, y1 + 2), code, fill=(255, 255, 255, 220), font=font)

        out = Image.alpha_composite(base, overlay).convert("RGB")
        target = Path(job.overlay_dir) / image_row.overlay_filename
        target.parent.mkdir(parents=True, exist_ok=True)
        out.save(target)

    def _rerender_all_overlays_for_job(self, db, job: AreaJob) -> None:
        image_rows = (
            db.query(AreaJobImage)
            .filter(AreaJobImage.job_id == job.id)
            .order_by(AreaJobImage.image_name.asc())
            .all()
        )
        for image_row in image_rows:
            instances = (
                db.query(AreaJobInstance)
                .filter(AreaJobInstance.image_id == image_row.id)
                .order_by(AreaJobInstance.sort_index.asc(), AreaJobInstance.id.asc())
                .all()
            )
            self._render_overlay_for_image(job, image_row, instances)

    def _rebuild_job_outputs(self, db, job: AreaJob) -> None:
        classes = parse_model_classes(job.model_name)
        image_rows = (
            db.query(AreaJobImage)
            .filter(AreaJobImage.job_id == job.id)
            .order_by(AreaJobImage.image_name.asc())
            .all()
        )

        class_totals = {name: 0 for name in classes}
        class_image_counts = {name: 0 for name in classes}
        detail_rows: list[dict[str, Any]] = []
        failed_images = 0
        succeeded_images = 0

        for image_row in image_rows:
            if image_row.error_message:
                failed_images += 1
                detail_rows.append(
                    {
                        "image_name": image_row.image_name,
                        "class_name": "",
                        "instance_count": 0,
                        "area_px": 0,
                        "ratio_percent": 0.0,
                        "overlay_filename": image_row.overlay_filename,
                        "error": image_row.error_message,
                    }
                )
                continue

            succeeded_images += 1
            per_class = image_row.per_class_area_px if isinstance(image_row.per_class_area_px, dict) else {}
            rows = (
                db.query(AreaJobInstance)
                .filter(AreaJobInstance.image_id == image_row.id)
                .all()
            )
            per_class_inst: dict[str, int] = {}
            for item in rows:
                if item.is_deleted:
                    continue
                per_class_inst[item.class_name] = per_class_inst.get(item.class_name, 0) + 1

            total_area = max(1, int(sum(max(0, int(v)) for v in per_class.values())))
            for class_name in classes:
                area_px = int(per_class.get(class_name, 0) or 0)
                if area_px > 0:
                    class_image_counts[class_name] += 1
                class_totals[class_name] += area_px
                detail_rows.append(
                    {
                        "image_name": image_row.image_name,
                        "class_name": class_name,
                        "instance_count": int(per_class_inst.get(class_name, 0)),
                        "area_px": area_px,
                        "ratio_percent": round(area_px * 100.0 / total_area, 4),
                        "overlay_filename": image_row.overlay_filename,
                        "error": "",
                    }
                )

        total_area_all = max(1, int(sum(class_totals.values())))
        summary_rows: list[dict[str, Any]] = []
        for class_name in classes:
            area_px = int(class_totals.get(class_name, 0))
            summary_rows.append(
                {
                    "class_name": class_name,
                    "total_area_px": area_px,
                    "ratio_percent": round(area_px * 100.0 / total_area_all, 4),
                    "image_count": int(class_image_counts.get(class_name, 0)),
                }
            )

        payload = {
            "job_id": job.job_id,
            "status": "succeeded_with_errors" if failed_images > 0 and succeeded_images > 0 else ("failed" if succeeded_images <= 0 else "succeeded"),
            "summary": summary_rows,
            "per_image": detail_rows,
        }

        output_json_path = Path(job.result_json_path)
        output_json_path.parent.mkdir(parents=True, exist_ok=True)
        output_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._build_excel(summary_rows, detail_rows, Path(job.excel_path))

        job.summary_json = summary_rows
        job.detail_json = detail_rows
        job.succeeded_images = succeeded_images
        job.failed_images = failed_images
        job.processed_images = len(image_rows)
        job.total_images = len(image_rows)
        if payload["status"] in {"succeeded", "succeeded_with_errors"}:
            job.status = payload["status"]

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            record.status = "running"
            record.started_at = datetime.now(timezone.utc)
            record.error_code = None
            record.error_message = None

        self._update_job_row(
            record.job_id,
            status="running",
            started_at=record.started_at,
            error_code=None,
            error_message=None,
        )

        try:
            target_dir = self._resolve_target_folder(record.root_path, record.folder_name)
            image_paths = sorted(
                [
                    path
                    for path in target_dir.iterdir()
                    if path.is_file() and path.suffix.lower() in ALLOWED_IMAGE_SUFFIXES
                ],
                key=lambda item: item.name.lower(),
            )
            if not image_paths:
                raise ValueError("empty_image_list")

            classes = parse_model_classes(record.model_name)
            engine_meta_snapshot: dict[str, Any] = {}
            infer_error_code: str | None = None

            with self._lock:
                record.total_images = len(image_paths)

            self._update_job_row(record.job_id, total_images=len(image_paths))

            db = SessionLocal()
            try:
                job_row = db.query(AreaJob).filter(AreaJob.job_id == record.job_id).first()
                if job_row is None:
                    raise RuntimeError("job_not_found")
                for image_path in image_paths:
                    try:
                        prediction = self._predictor.predict(
                            image_path=image_path,
                            model_name=record.model_name,
                            weight_path=Path(record.weight_path),
                            inference_options=record.inference_options,
                            infer_url=record.infer_url,
                            timeout_sec=record.infer_timeout_sec,
                            model_file=record.model_file,
                        )
                        if not engine_meta_snapshot and isinstance(prediction.engine_meta, dict):
                            engine_meta_snapshot = dict(prediction.engine_meta)
                        overlay_filename = f"{image_path.stem}_overlay.png"
                        if (Path(record.overlay_dir) / overlay_filename).exists():
                            overlay_filename = f"{image_path.stem}_{uuid4().hex[:6]}_overlay.png"

                        width, height = prediction.overlay_image.size
                        per_class_area = {
                            class_name: int(prediction.per_class_area_px.get(class_name, 0))
                            for class_name in classes
                        }
                        image_row = AreaJobImage(
                            job_id=job_row.id,
                            image_name=image_path.name,
                            overlay_filename=overlay_filename,
                            source_image_path=str(image_path),
                            width=width,
                            height=height,
                            total_area_px=int(sum(per_class_area.values())),
                            per_class_area_px=per_class_area,
                            error_message="",
                        )
                        db.add(image_row)
                        db.flush()

                        for idx, inst in enumerate(prediction.instances):
                            bbox = [int(v) for v in inst.bbox]
                            polygon = self._normalize_polygon(inst.polygon)
                            if not polygon and len(bbox) == 4:
                                polygon = [
                                    [bbox[0], bbox[1]],
                                    [bbox[2], bbox[1]],
                                    [bbox[2], bbox[3]],
                                    [bbox[0], bbox[3]],
                                ]
                            area_px = int(inst.area_px or 0)
                            row = AreaJobInstance(
                                image_id=image_row.id,
                                class_name=inst.class_name,
                                score=float(inst.score) if inst.score is not None else None,
                                bbox=bbox,
                                polygon=polygon,
                                area_px=area_px,
                                is_deleted=False,
                                sort_index=idx,
                                initial_bbox=bbox,
                                initial_polygon=polygon,
                                initial_area_px=area_px,
                                initial_is_deleted=False,
                            )
                            db.add(row)
                        db.flush()
                        instances_for_render = (
                            db.query(AreaJobInstance)
                            .filter(AreaJobInstance.image_id == image_row.id)
                            .order_by(AreaJobInstance.sort_index.asc(), AreaJobInstance.id.asc())
                            .all()
                        )
                        self._render_overlay_for_image(job_row, image_row, instances_for_render)

                        with self._lock:
                            record.succeeded_images += 1
                    except Exception as exc:
                        raw_error = str(exc).strip()
                        if raw_error in {
                            "infer_service_unavailable",
                            "infer_model_load_failed",
                            "infer_timeout",
                            "infer_bad_response",
                        } and infer_error_code is None:
                            infer_error_code = raw_error
                        image_row = AreaJobImage(
                            job_id=job_row.id,
                            image_name=image_path.name,
                            overlay_filename="",
                            source_image_path=str(image_path),
                            width=0,
                            height=0,
                            total_area_px=0,
                            per_class_area_px={},
                            error_message=raw_error or "area_inference_failed",
                        )
                        db.add(image_row)
                        with self._lock:
                            record.failed_images += 1
                    finally:
                        with self._lock:
                            record.processed_images += 1
                        job_row.processed_images = record.processed_images
                        job_row.succeeded_images = record.succeeded_images
                        job_row.failed_images = record.failed_images
                        db.commit()

                if record.succeeded_images <= 0:
                    raise RuntimeError(infer_error_code or "all_images_failed")

                job_row.engine_meta = engine_meta_snapshot
                job_row.finished_at = datetime.now(timezone.utc)
                self._rebuild_job_outputs(db, job_row)
                job_row.error_code = None
                job_row.error_message = None
                db.commit()

                with self._lock:
                    record.status = str(job_row.status)
                    record.finished_at = job_row.finished_at
                    record.engine_meta = engine_meta_snapshot
                    record.error_code = None
                    record.error_message = None
            finally:
                db.close()
        except FileNotFoundError as exc:
            self._set_job_failed(record, str(exc), str(exc))
        except ValueError as exc:
            self._set_job_failed(record, str(exc), str(exc))
        except Exception as exc:
            code = str(exc).strip()
            if code in {
                "infer_service_unavailable",
                "infer_model_load_failed",
                "infer_timeout",
                "infer_bad_response",
                "all_images_failed",
                "job_not_found",
            }:
                self._set_job_failed(record, code, code)
            else:
                self._set_job_failed(record, "area_inference_failed", str(exc))

    def _running_folders(self) -> set[str]:
        with self._lock:
            return {
                item.folder_name
                for item in self._jobs.values()
                if item.status in {"queued", "running"}
            }


area_job_manager = AreaJobManager()
