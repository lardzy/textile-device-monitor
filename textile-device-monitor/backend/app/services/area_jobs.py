from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import threading
from typing import Any
from uuid import uuid4

from openpyxl import Workbook

from app.config import settings
from app.services.area_infer import (
    DEFAULT_INFER_OPTIONS,
    AreaPredictor,
    parse_model_classes,
)

ALLOWED_IMAGE_SUFFIXES = {".jpg", ".png"}


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


class AreaJobManager:
    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue()
        self._jobs: dict[str, AreaJobRecord] = {}
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._workers: list[threading.Thread] = []
        self._started = False
        self._predictor = AreaPredictor()

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
        inference_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.start()

        normalized_folder = folder_name.strip()
        normalized_root = root_path.strip()
        if not normalized_folder:
            raise ValueError("invalid_folder_name")
        if "/" in normalized_folder or "\\" in normalized_folder:
            raise ValueError("invalid_folder_name")
        if normalized_folder in {".", ".."}:
            raise ValueError("invalid_folder_name")
        if not normalized_root:
            raise ValueError("invalid_root_path")

        model_file = model_mapping.get(model_name)
        if not model_file:
            raise ValueError("invalid_model_name")
        normalized_options = self._normalize_inference_options(inference_options)
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

        job_id = uuid4().hex
        output_dir = Path(settings.AREA_OUTPUT_DIR) / job_id
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
            inference_options=normalized_options,
        )

        with self._lock:
            self._jobs[job_id] = record
        self._queue.put(job_id)

        payload = self.get_job(job_id)
        if payload is None:
            raise RuntimeError("failed_to_create_job")
        return payload

    def list_jobs(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._jobs.values())
        records.sort(key=lambda item: item.created_at, reverse=True)
        return [self._serialize_job(item) for item in records[:limit]]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            return self._serialize_job(record)

    def get_result(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            return {
                "job_id": record.job_id,
                "status": record.status,
                "summary": record.result_summary,
                "per_image": record.result_rows,
            }

    def get_excel_path(self, job_id: str) -> Path | None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            path = Path(record.excel_path)
        if not path.exists():
            return None
        return path

    def get_overlay_image_path(self, job_id: str, filename: str) -> Path | None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            path = Path(record.overlay_dir) / filename
        if not path.exists() or not path.is_file():
            return None
        return path

    def list_overlay_images(
        self, job_id: str, page: int = 1, page_size: int = 50
    ) -> dict[str, Any] | None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            items = list(record.image_items)
        total = len(items)
        start = max(0, (page - 1) * page_size)
        end = start + page_size
        return {
            "items": items[start:end],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def _serialize_job(self, record: AreaJobRecord) -> dict[str, Any]:
        return {
            "job_id": record.job_id,
            "status": record.status,
            "folder_name": record.folder_name,
            "model_name": record.model_name,
            "model_file": record.model_file,
            "inference_options": record.inference_options,
            "created_at": record.created_at.isoformat(),
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "finished_at": record.finished_at.isoformat() if record.finished_at else None,
            "error_code": record.error_code,
            "error_message": record.error_message,
            "total_images": record.total_images,
            "processed_images": record.processed_images,
            "succeeded_images": record.succeeded_images,
            "failed_images": record.failed_images,
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
        with self._lock:
            record.status = "failed"
            record.error_code = code
            record.error_message = message
            record.finished_at = datetime.now(timezone.utc)

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

    def _resolve_target_folder(self, root_path: str, folder_name: str) -> Path:
        roots = self._root_path_candidates(root_path)
        if not roots:
            raise FileNotFoundError("root_path_not_found")
        existing_roots = [root for root in roots if root.exists() and root.is_dir()]
        if not existing_roots:
            raise FileNotFoundError("root_path_not_found")
        for root in existing_roots:
            target = root / folder_name
            if target.exists() and target.is_dir():
                return target
        raise FileNotFoundError("folder_not_found")

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
        wb.save(excel_path)
        wb.close()

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            record.status = "running"
            record.started_at = datetime.now(timezone.utc)
            record.error_code = None
            record.error_message = None

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
            class_totals = {name: 0 for name in classes}
            class_image_counts = {name: 0 for name in classes}
            detail_rows: list[dict[str, Any]] = []
            image_items: list[dict[str, str]] = []

            with self._lock:
                record.total_images = len(image_paths)

            for image_path in image_paths:
                try:
                    prediction = self._predictor.predict(
                        image_path=image_path,
                        model_name=record.model_name,
                        weight_path=Path(record.weight_path),
                        inference_options=record.inference_options,
                    )
                    overlay_filename = f"{image_path.stem}_overlay.png"
                    overlay_save_path = Path(record.overlay_dir) / overlay_filename
                    if overlay_save_path.exists():
                        overlay_filename = f"{image_path.stem}_{uuid4().hex[:6]}_overlay.png"
                        overlay_save_path = Path(record.overlay_dir) / overlay_filename
                    prediction.overlay_image.save(overlay_save_path)
                    image_items.append(
                        {
                            "image_name": image_path.name,
                            "overlay_filename": overlay_filename,
                        }
                    )

                    total_area = max(1, prediction.total_area_px)
                    per_class_inst: dict[str, int] = {}
                    for inst in prediction.instances:
                        per_class_inst[inst.class_name] = (
                            per_class_inst.get(inst.class_name, 0) + 1
                        )

                    for class_name in classes:
                        area_px = int(prediction.per_class_area_px.get(class_name, 0))
                        if area_px > 0:
                            class_image_counts[class_name] += 1
                        class_totals[class_name] += area_px
                        detail_rows.append(
                            {
                                "image_name": image_path.name,
                                "class_name": class_name,
                                "instance_count": int(per_class_inst.get(class_name, 0)),
                                "area_px": area_px,
                                "ratio_percent": round(area_px * 100.0 / total_area, 4),
                                "overlay_filename": overlay_filename,
                                "error": "",
                            }
                        )

                    with self._lock:
                        record.succeeded_images += 1
                except Exception as exc:
                    detail_rows.append(
                        {
                            "image_name": image_path.name,
                            "class_name": "",
                            "instance_count": 0,
                            "area_px": 0,
                            "ratio_percent": 0.0,
                            "overlay_filename": "",
                            "error": str(exc),
                        }
                    )
                    with self._lock:
                        record.failed_images += 1
                finally:
                    with self._lock:
                        record.processed_images += 1

            if record.succeeded_images <= 0:
                raise RuntimeError("all_images_failed")

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

            self._build_excel(summary_rows, detail_rows, Path(record.excel_path))
            result_payload = {
                "job_id": record.job_id,
                "status": "succeeded_with_errors"
                if record.failed_images > 0
                else "succeeded",
                "summary": summary_rows,
                "per_image": detail_rows,
            }
            Path(record.result_json_path).write_text(
                json.dumps(result_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            with self._lock:
                record.status = result_payload["status"]
                record.finished_at = datetime.now(timezone.utc)
                record.result_summary = summary_rows
                record.result_rows = detail_rows
                record.image_items = image_items
                record.error_code = None
                record.error_message = None
        except FileNotFoundError as exc:
            self._set_job_failed(record, str(exc), str(exc))
        except ValueError as exc:
            self._set_job_failed(record, str(exc), str(exc))
        except Exception as exc:
            self._set_job_failed(record, "area_inference_failed", str(exc))


area_job_manager = AreaJobManager()
