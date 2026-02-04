"""
结果服务模块 - 提供最新结果与图片访问
"""

import os
import io
import threading
import tempfile
import shutil
from collections import OrderedDict
from typing import Optional, List, Dict, Callable, cast
from datetime import datetime
from http.server import HTTPServer
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote
from .progress_reader import ProgressReader
from .logger import Logger
import openpyxl
import formulas
from PIL import Image


class ResultsHandler(BaseHTTPRequestHandler):
    reader: Optional[ProgressReader] = None
    logger: Optional[Logger] = None
    _formula_cache = OrderedDict()
    _formula_cache_lock = threading.Lock()
    _formula_in_progress = set()
    _formula_cache_limit = 6
    _recent_cache_lock = threading.Lock()
    _recent_cache: Dict[str, object] = {}
    _recent_cache_ttl = 30
    _recent_cache_max_items = 20

    @classmethod
    def _process_xlsx_with_formulas(cls, xlsx_path: str) -> Optional[bytes]:
        temp_dir = tempfile.mkdtemp()
        try:
            try:
                xl_model = formulas.ExcelModel().loads(xlsx_path).finish()
                xl_model.calculate()
                result = xl_model.write(dirpath=temp_dir)
                if result:
                    for file_info in result.values():
                        if isinstance(file_info, dict) and file_info:
                            book_key = list(file_info.keys())[0]
                            wb = file_info.get(book_key)
                            if isinstance(wb, openpyxl.Workbook):
                                output = io.BytesIO()
                                wb.save(output)
                                output.seek(0)
                                wb.close()
                                return output.read()
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as exc:
            if cls.logger:
                cls.logger.error(f"使用formulas计算失败: {exc}, 降级到openpyxl")

        return cls._process_xlsx_preview(xlsx_path)

    @classmethod
    def _process_xlsx_preview(cls, xlsx_path: str) -> Optional[bytes]:
        try:
            wb = openpyxl.load_workbook(xlsx_path, data_only=True)
            output = io.BytesIO()
            wb.save(output)
            output.seek(0)
            wb.close()
            return output.read()
        except Exception as exc:
            if cls.logger:
                cls.logger.error(f"处理xlsx文件失败: {exc}")
            return None

    def _build_thumbnail(
        self, image_path: str, size: int
    ) -> Optional[tuple[bytes, str]]:
        try:
            with Image.open(image_path) as img:
                original_format = img.format or "PNG"
                target_size = max(64, min(size, 1024))
                img.thumbnail((target_size, target_size))
                output = io.BytesIO()
                if original_format.upper() in ("JPG", "JPEG"):
                    img = img.convert("RGB")
                    img.save(output, format="JPEG", quality=85, optimize=True)
                    content_type = "image/jpeg"
                else:
                    img.save(output, format="PNG", optimize=True)
                    content_type = "image/png"
                output.seek(0)
                return output.read(), content_type
        except Exception as exc:
            if self.logger:
                self.logger.error(f"生成缩略图失败: {exc}")
            return None

    @classmethod
    def _get_cache_key(cls, file_path: str) -> str:
        return os.path.abspath(file_path)

    @classmethod
    def _get_cached_formula(cls, file_path: str) -> Optional[bytes]:
        try:
            mtime = os.path.getmtime(file_path)
        except OSError:
            return None
        key = cls._get_cache_key(file_path)
        with cls._formula_cache_lock:
            entry = cls._formula_cache.get(key)
            if entry and entry.get("mtime") == mtime:
                cls._formula_cache.move_to_end(key)
                return entry.get("data")
            if entry:
                cls._formula_cache.pop(key, None)
        return None

    @classmethod
    def _store_cached_formula(cls, file_path: str, mtime: float, data: bytes) -> None:
        key = cls._get_cache_key(file_path)
        with cls._formula_cache_lock:
            cls._formula_cache[key] = {"mtime": mtime, "data": data}
            cls._formula_cache.move_to_end(key)
            while len(cls._formula_cache) > cls._formula_cache_limit:
                cls._formula_cache.popitem(last=False)

    @classmethod
    def _build_formula_cache(cls, file_path: str, mtime: float) -> None:
        data = cls._process_xlsx_with_formulas(file_path)
        key = cls._get_cache_key(file_path)
        with cls._formula_cache_lock:
            cls._formula_in_progress.discard(key)
        if not data:
            return
        try:
            current_mtime = os.path.getmtime(file_path)
        except OSError:
            return
        if current_mtime != mtime:
            return
        cls._store_cached_formula(file_path, mtime, data)

    @classmethod
    def _schedule_formula_cache(cls, file_path: str) -> None:
        try:
            mtime = os.path.getmtime(file_path)
        except OSError:
            return
        key = cls._get_cache_key(file_path)
        with cls._formula_cache_lock:
            entry = cls._formula_cache.get(key)
            if entry and entry.get("mtime") == mtime:
                cls._formula_cache.move_to_end(key)
                return
            if key in cls._formula_in_progress:
                return
            cls._formula_in_progress.add(key)
        worker = threading.Thread(
            target=cls._build_formula_cache,
            args=(file_path, mtime),
            daemon=True,
        )
        worker.start()

    @classmethod
    def schedule_formula_cache_for_path(cls, file_path: str) -> None:
        cls._schedule_formula_cache(file_path)

    @classmethod
    def invalidate_recent_cache(cls) -> None:
        with cls._recent_cache_lock:
            cls._recent_cache = {}

    @classmethod
    def _get_recent_cache(cls, limit: int, working_path: str) -> Optional[List[Dict]]:
        import time

        with cls._recent_cache_lock:
            if not cls._recent_cache:
                return None
            cached_at = cls._recent_cache.get("cached_at")
            cached_items = cls._recent_cache.get("items")
            max_items = cls._recent_cache.get("max_items")
            cached_path = cls._recent_cache.get("working_path")
            if (
                cached_at is None
                or cached_items is None
                or max_items is None
                or cached_path != working_path
            ):
                return None
            if time.time() - float(cached_at) > cls._recent_cache_ttl:
                return None
            if int(max_items) < limit:
                return None
            return list(cached_items)[:limit]

    @classmethod
    def _set_recent_cache(
        cls, items: List[Dict], max_items: int, working_path: str
    ) -> None:
        import time

        with cls._recent_cache_lock:
            cls._recent_cache = {
                "items": items,
                "max_items": max_items,
                "working_path": working_path,
                "cached_at": time.time(),
            }

    def _is_confocal(self) -> bool:
        return bool(getattr(self.reader, "is_laser_confocal", False))

    def _resolve_confocal_folder(self, folder_param: Optional[str]) -> Optional[str]:
        if not self.reader:
            return None
        resolver = getattr(self.reader, "resolve_output_folder", None)
        if not callable(resolver):
            return None
        try:
            typed_resolver = cast(Callable[[Optional[str]], Optional[str]], resolver)
            return typed_resolver(folder_param)
        except Exception as exc:
            if self.logger:
                self.logger.error(f"解析输出目录失败: {exc}")
            return None

    def _list_confocal_images(self, folder_path: str) -> List[str]:
        try:
            images = [
                name
                for name in os.listdir(folder_path)
                if os.path.isfile(os.path.join(folder_path, name))
                and name.lower().endswith((".jpg", ".jpeg", ".png"))
            ]
            images.sort()
            return images
        except Exception as exc:
            if self.logger:
                self.logger.error(f"读取图片列表失败: {exc}")
            return []

    def _cleanup_confocal_images(
        self, folder_path: str
    ) -> Dict[str, Optional[str] | int]:
        recycle_dir = os.path.join(folder_path, ".recycle")
        os.makedirs(recycle_dir, exist_ok=True)
        moved = 0
        for name in os.listdir(folder_path):
            if name in {".", "..", ".recycle"}:
                continue
            src = os.path.join(folder_path, name)
            if os.path.isdir(src):
                continue
            lower_name = name.lower()
            if not lower_name.endswith((".jpg", ".jpeg", ".png")):
                continue
            if lower_name.endswith("_i.jpg") or lower_name.endswith("_i.jpeg"):
                continue
            dst = os.path.join(recycle_dir, name)
            shutil.move(src, dst)
            moved += 1
        return {"moved": moved, "recycle_dir": recycle_dir}

    def _get_recent_results(self, limit: int) -> List[Dict]:
        if not self.reader:
            return []
        cached = self._get_recent_cache(limit, self.reader.working_path)
        if cached is not None:
            return cached
        recent_getter = getattr(self.reader, "get_recent_results", None)
        if callable(recent_getter):
            try:
                typed_getter = cast(Callable[[int], List[Dict]], recent_getter)
                return typed_getter(limit)
            except Exception as exc:
                if self.logger:
                    self.logger.error(f"读取共聚焦结果列表失败: {exc}")
                return []
        try:
            entries = [
                os.path.join(self.reader.working_path, name)
                for name in os.listdir(self.reader.working_path)
                if os.path.isdir(os.path.join(self.reader.working_path, name))
            ]
        except Exception as exc:
            if self.logger:
                self.logger.error(f"读取结果列表失败: {exc}")
            return []

        latest_folder = self.reader._get_latest_modified_folder(
            self.reader.working_path
        )
        latest_name = os.path.basename(latest_folder) if latest_folder else None
        today = datetime.now().date()
        candidates = []
        for folder_path in entries:
            folder_name = os.path.basename(folder_path)
            if latest_name and folder_name == latest_name:
                continue
            try:
                mtime = os.path.getmtime(folder_path)
            except Exception:
                continue
            if datetime.fromtimestamp(mtime).date() != today:
                continue

            result_dir = os.path.join(folder_path, "result")
            if not os.path.exists(result_dir):
                continue
            files = [f for f in os.listdir(result_dir) if f.lower().endswith(".xlsx")]
            if not files:
                continue
            files.sort(key=lambda f: os.path.getmtime(os.path.join(result_dir, f)))
            xlsx_name = files[-1]
            xlsx_path = os.path.join(result_dir, xlsx_name)

            cut_pic_dir = os.path.join(folder_path, "cut_pic", "1")
            image_count = 0
            if os.path.exists(cut_pic_dir):
                image_count = len(
                    [f for f in os.listdir(cut_pic_dir) if f.lower().endswith(".png")]
                )

            candidates.append(
                (
                    mtime,
                    {
                        "folder": folder_name,
                        "task_name": folder_name,
                        "xlsx_name": xlsx_name,
                        "image_count": image_count,
                        "updated_at": datetime.fromtimestamp(mtime).isoformat(),
                    },
                )
            )

        candidates.sort(key=lambda item: item[0], reverse=True)
        max_items = max(limit, self._recent_cache_max_items)
        cached_items = [item for _, item in candidates[:max_items]]
        self._set_recent_cache(cached_items, max_items, self.reader.working_path)
        return cached_items[:limit]

    def _send_json(self, status: int, payload: Dict):
        import json

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._add_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._safe_write(body)

    def _send_bytes(self, data: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self._add_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        self._safe_write(data)

    def _send_file(
        self,
        file_path: str,
        content_type: str,
        download_name: Optional[str] = None,
    ):
        try:
            with open(file_path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self._add_cors_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if download_name:
                safe_name = download_name.replace('"', "").replace("\\", "")
                encoded_name = quote(safe_name)
                fallback_name = safe_name.encode("ascii", "ignore").decode("ascii")
                if not fallback_name:
                    ext = os.path.splitext(safe_name)[1]
                    fallback_name = f"download{ext}" if ext else "download"
                disposition = (
                    f'attachment; filename="{fallback_name}"; '
                    f"filename*=UTF-8''{encoded_name}"
                )
                self.send_header("Content-Disposition", disposition)
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self._safe_write(data)
        except Exception as exc:
            if self.logger:
                self.logger.error(f"读取文件失败: {exc}")
            self._send_json(500, {"error": "file_read_failed"})

    def _safe_write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def _add_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._add_cors_headers()
        self.end_headers()
        return

    def do_GET(self):  # noqa: N802
        if not self.reader:
            self._send_json(500, {"error": "reader_not_ready"})
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        is_confocal = self._is_confocal()

        if path == "/client/results/latest":
            if is_confocal:
                latest_folder = self._resolve_confocal_folder(None)
                if not latest_folder or not os.path.isdir(latest_folder):
                    self._send_json(200, {"latest_folder": None})
                    return
                folder_name = os.path.basename(latest_folder)
                images = self._list_confocal_images(latest_folder)
                self._send_json(
                    200,
                    {
                        "latest_folder": folder_name,
                        "image_count": len(images),
                    },
                )
                return
            latest_folder = self.reader._get_latest_modified_folder(
                self.reader.working_path
            )
            if not latest_folder:
                self._send_json(200, {"latest_folder": None})
                return

            folder_name = os.path.basename(latest_folder)
            result_dir = os.path.join(latest_folder, "result")
            cut_pic_dir = os.path.join(latest_folder, "cut_pic", "1")

            xlsx_name = None
            if os.path.exists(result_dir):
                files = [
                    f for f in os.listdir(result_dir) if f.lower().endswith(".xlsx")
                ]
                files.sort(key=lambda f: os.path.getmtime(os.path.join(result_dir, f)))
                if files:
                    xlsx_name = files[-1]

            image_count = 0
            if os.path.exists(cut_pic_dir):
                image_count = len(
                    [f for f in os.listdir(cut_pic_dir) if f.lower().endswith(".png")]
                )

            self._send_json(
                200,
                {
                    "latest_folder": folder_name,
                    "xlsx_name": xlsx_name,
                    "image_count": image_count,
                },
            )
            return

        if path == "/client/results/table":
            if is_confocal:
                self._send_json(404, {"error": "table_not_supported"})
                return
            latest_folder = self.reader._get_latest_modified_folder(
                self.reader.working_path
            )
            if not latest_folder:
                self._send_json(404, {"error": "no_latest_folder"})
                return

            folder_name = None
            if "folder" in query and query.get("folder"):
                folders = query.get("folder")
                if folders:
                    folder_name = unquote(folders[0])
                    candidate = os.path.join(self.reader.working_path, folder_name)
                    if os.path.isdir(candidate):
                        latest_folder = candidate
                    else:
                        self._send_json(404, {"error": "folder_not_found"})
                        return

            result_dir = os.path.join(latest_folder, "result")
            if not os.path.exists(result_dir):
                self._send_json(404, {"error": "result_dir_missing"})
                return

            files = [f for f in os.listdir(result_dir) if f.lower().endswith(".xlsx")]
            files.sort(key=lambda f: os.path.getmtime(os.path.join(result_dir, f)))
            if not files:
                self._send_json(404, {"error": "xlsx_not_found"})
                return

            xlsx_name = files[-1]
            xlsx_path = os.path.join(result_dir, xlsx_name)
            self._send_file(
                xlsx_path,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                download_name=xlsx_name,
            )
            return

        if path == "/client/results/table_preview":
            if is_confocal:
                self._send_json(404, {"error": "table_not_supported"})
                return
            latest_folder = self.reader._get_latest_modified_folder(
                self.reader.working_path
            )
            if not latest_folder:
                self._send_json(404, {"error": "no_latest_folder"})
                return

            if "folder" in query and query.get("folder"):
                folders = query.get("folder")
                if folders:
                    folder_name = unquote(folders[0])
                    candidate = os.path.join(self.reader.working_path, folder_name)
                    if os.path.isdir(candidate):
                        latest_folder = candidate
                    else:
                        self._send_json(404, {"error": "folder_not_found"})
                        return

            result_dir = os.path.join(latest_folder, "result")
            if not os.path.exists(result_dir):
                self._send_json(404, {"error": "result_dir_missing"})
                return

            files = [f for f in os.listdir(result_dir) if f.lower().endswith(".xlsx")]
            files.sort(key=lambda f: os.path.getmtime(os.path.join(result_dir, f)))
            if not files:
                self._send_json(404, {"error": "xlsx_not_found"})
                return

            xlsx_name = files[-1]
            xlsx_path = os.path.join(result_dir, xlsx_name)
            preview_bytes = self._process_xlsx_preview(xlsx_path)
            if not preview_bytes:
                self._send_json(500, {"error": "preview_failed"})
                return
            self._send_bytes(
                preview_bytes,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            return

        if path == "/client/results/table_view":
            if is_confocal:
                self._send_json(404, {"error": "table_not_supported"})
                return
            latest_folder = self.reader._get_latest_modified_folder(
                self.reader.working_path
            )
            if not latest_folder:
                self._send_json(404, {"error": "no_latest_folder"})
                return

            if "folder" in query and query.get("folder"):
                folders = query.get("folder")
                if folders:
                    folder_name = unquote(folders[0])
                    candidate = os.path.join(self.reader.working_path, folder_name)
                    if os.path.isdir(candidate):
                        latest_folder = candidate
                    else:
                        self._send_json(404, {"error": "folder_not_found"})
                        return

            result_dir = os.path.join(latest_folder, "result")
            if not os.path.exists(result_dir):
                self._send_json(404, {"error": "result_dir_missing"})
                return

            files = [f for f in os.listdir(result_dir) if f.lower().endswith(".xlsx")]
            files.sort(key=lambda f: os.path.getmtime(os.path.join(result_dir, f)))
            if not files:
                self._send_json(404, {"error": "xlsx_not_found"})
                return

            xlsx_name = files[-1]
            xlsx_path = os.path.join(result_dir, xlsx_name)
            cached = self._get_cached_formula(xlsx_path)
            if cached:
                self._send_bytes(
                    cached,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                return

            self._schedule_formula_cache(xlsx_path)
            self._send_json(202, {"status": "processing"})
            return

        if path == "/client/results/images":
            if is_confocal:
                folder_param = None
                if "folder" in query and query.get("folder"):
                    folders = query.get("folder")
                    if folders:
                        folder_param = unquote(folders[0])
                target_folder = self._resolve_confocal_folder(folder_param)
                if not target_folder or not os.path.isdir(target_folder):
                    if folder_param:
                        self._send_json(404, {"error": "folder_not_found"})
                    else:
                        self._send_json(
                            200,
                            {"items": [], "total": 0, "page": 1, "folder": None},
                        )
                    return
                folder_value = folder_param or target_folder
                page = int(query.get("page", ["1"])[0])
                page_size = int(query.get("page_size", ["200"])[0])
                images = self._list_confocal_images(target_folder)
                total = len(images)
                start = (page - 1) * page_size
                end = start + page_size
                items = [
                    {
                        "name": name,
                        "url": f"/client/results/image/{quote(name, safe='')}?folder={quote(folder_value, safe='')}",
                    }
                    for name in images[start:end]
                ]
                self._send_json(
                    200,
                    {
                        "items": items,
                        "total": total,
                        "page": page,
                        "folder": folder_value,
                    },
                )
                return
            latest_folder = self.reader._get_latest_modified_folder(
                self.reader.working_path
            )
            if not latest_folder:
                self._send_json(
                    200,
                    {"items": [], "total": 0, "page": 1, "folder": None},
                )
                return

            folder_name = None
            if "folder" in query and query.get("folder"):
                folders = query.get("folder")
                if folders:
                    folder_name = unquote(folders[0])
                    candidate = os.path.join(self.reader.working_path, folder_name)
                    if os.path.isdir(candidate):
                        latest_folder = candidate
                    else:
                        self._send_json(404, {"error": "folder_not_found"})
                        return

            folder_name = os.path.basename(latest_folder)
            cut_pic_dir = os.path.join(latest_folder, "cut_pic", "1")
            if not os.path.exists(cut_pic_dir):
                self._send_json(
                    200,
                    {"items": [], "total": 0, "page": 1, "folder": folder_name},
                )
                return

            page = int(query.get("page", ["1"])[0])
            page_size = int(query.get("page_size", ["200"])[0])
            images = [f for f in os.listdir(cut_pic_dir) if f.lower().endswith(".png")]
            images.sort()
            total = len(images)
            start = (page - 1) * page_size
            end = start + page_size
            items = [
                {
                    "name": name,
                    "url": f"/client/results/image/{quote(name, safe='')}?folder={quote(folder_name, safe='')}",
                }
                for name in images[start:end]
            ]
            self._send_json(
                200,
                {
                    "items": items,
                    "total": total,
                    "page": page,
                    "folder": folder_name,
                },
            )
            return

        if path == "/client/results/recent":
            try:
                limit = int(query.get("limit", ["5"])[0])
            except ValueError:
                limit = 5
            if limit <= 0:
                limit = 5
            items = self._get_recent_results(limit)
            self._send_json(200, {"items": items})
            return

        if path.startswith("/client/results/thumb/"):
            size_param = query.get("size", ["320"])[0]
            try:
                size = int(size_param)
            except ValueError:
                size = 320
            if is_confocal:
                folder_param = None
                if "folder" in query and query.get("folder"):
                    folders = query.get("folder")
                    if folders:
                        folder_param = unquote(folders[0])
                target_folder = self._resolve_confocal_folder(folder_param)
                if not target_folder or not os.path.isdir(target_folder):
                    if folder_param:
                        self._send_json(404, {"error": "folder_not_found"})
                    else:
                        self._send_json(404, {"error": "no_latest_folder"})
                    return
                filename = unquote(path.split("/client/results/thumb/")[-1])
                image_path = os.path.join(target_folder, filename)
                if not os.path.exists(image_path):
                    self._send_json(404, {"error": "image_not_found"})
                    return
                thumbnail = self._build_thumbnail(image_path, size)
                if not thumbnail:
                    self._send_json(500, {"error": "thumb_failed"})
                    return
                data, content_type = thumbnail
                self._send_bytes(data, content_type)
                return

            folder_name = None
            if "folder" in query and query.get("folder"):
                folders = query.get("folder")
                if folders:
                    folder_name = unquote(folders[0])

            latest_folder = self.reader._get_latest_modified_folder(
                self.reader.working_path
            )
            if not latest_folder:
                self._send_json(404, {"error": "no_latest_folder"})
                return

            if folder_name:
                candidate = os.path.join(self.reader.working_path, folder_name)
                if os.path.isdir(candidate):
                    latest_folder = candidate
                else:
                    self._send_json(404, {"error": "folder_not_found"})
                    return

            filename = unquote(path.split("/client/results/thumb/")[-1])
            image_path = os.path.join(latest_folder, "cut_pic", "1", filename)
            if not os.path.exists(image_path):
                self._send_json(404, {"error": "image_not_found"})
                return
            thumbnail = self._build_thumbnail(image_path, size)
            if not thumbnail:
                self._send_json(500, {"error": "thumb_failed"})
                return
            data, content_type = thumbnail
            self._send_bytes(data, content_type)
            return

        if path.startswith("/client/results/image/"):
            if is_confocal:
                folder_param = None
                if "folder" in query and query.get("folder"):
                    folders = query.get("folder")
                    if folders:
                        folder_param = unquote(folders[0])
                target_folder = self._resolve_confocal_folder(folder_param)
                if not target_folder or not os.path.isdir(target_folder):
                    self._send_json(404, {"error": "folder_not_found"})
                    return
                filename = unquote(path.split("/client/results/image/")[-1])
                image_path = os.path.join(target_folder, filename)
                if not os.path.exists(image_path):
                    self._send_json(404, {"error": "image_not_found"})
                    return
                ext = os.path.splitext(filename)[1].lower()
                content_type = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
                self._send_file(image_path, content_type)
                return
            folder_name = None
            if "folder" in query and query.get("folder"):
                folders = query.get("folder")
                if folders:
                    folder_name = unquote(folders[0])

            latest_folder = self.reader._get_latest_modified_folder(
                self.reader.working_path
            )
            if not latest_folder:
                self._send_json(404, {"error": "no_latest_folder"})
                return

            if folder_name:
                candidate = os.path.join(self.reader.working_path, folder_name)
                if os.path.isdir(candidate):
                    latest_folder = candidate

            cut_pic_dir = os.path.join(latest_folder, "cut_pic", "1")
            filename = unquote(path.split("/client/results/image/")[-1])
            image_path = os.path.join(cut_pic_dir, filename)
            if not os.path.exists(image_path):
                self._send_json(404, {"error": "image_not_found"})
                return

            self._send_file(image_path, "image/png")
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        if not self.reader:
            self._send_json(500, {"error": "reader_not_ready"})
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/client/results/cleanup":
            if not self._is_confocal():
                self._send_json(404, {"error": "cleanup_not_supported"})
                return
            folder_param = None
            if "folder" in query and query.get("folder"):
                folders = query.get("folder")
                if folders:
                    folder_param = unquote(folders[0])
            target_folder = self._resolve_confocal_folder(folder_param)
            if not target_folder or not os.path.isdir(target_folder):
                self._send_json(404, {"error": "folder_not_found"})
                return
            try:
                result = self._cleanup_confocal_images(target_folder)
            except Exception as exc:
                if self.logger:
                    self.logger.error(f"清理图片失败: {exc}")
                self._send_json(500, {"error": "cleanup_failed"})
                return
            self._send_json(200, result)
            return

        self._send_json(404, {"error": "not_found"})


class ResultsServer:
    def __init__(
        self,
        reader: ProgressReader,
        logger: Logger,
        host: str = "0.0.0.0",
        port: int = 9100,
    ):
        self.reader = reader
        self.logger = logger
        self.host = host
        self.port = port
        self.httpd: Optional[HTTPServer] = None

    def start(self):
        ResultsHandler.reader = self.reader
        ResultsHandler.logger = self.logger
        self.httpd = ThreadingHTTPServer((self.host, self.port), ResultsHandler)
        self.logger.info(f"结果服务启动: http://{self.host}:{self.port}")
        self.httpd.serve_forever()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd = None

    def prewarm_latest_formulas(self) -> None:
        if not self.reader:
            return
        if getattr(self.reader, "is_laser_confocal", False):
            return
        ResultsHandler.invalidate_recent_cache()
        try:
            latest_folder = self.reader._get_latest_modified_folder(
                self.reader.working_path
            )
            if not latest_folder:
                return
            result_dir = os.path.join(latest_folder, "result")
            if not os.path.exists(result_dir):
                return
            files = [
                f for f in os.listdir(result_dir) if f.lower().endswith(".xlsx")
            ]
            if not files:
                return
            files.sort(key=lambda f: os.path.getmtime(os.path.join(result_dir, f)))
            xlsx_path = os.path.join(result_dir, files[-1])
            ResultsHandler.schedule_formula_cache_for_path(xlsx_path)
        except Exception as exc:
            if self.logger:
                self.logger.error(f"预计算结果表格失败: {exc}")
