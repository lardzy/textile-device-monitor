"""
结果服务模块 - 提供最新结果与图片访问
"""

import os
import io
from typing import Optional, List, Dict
from datetime import datetime
from http.server import HTTPServer
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote
from .progress_reader import ProgressReader
from .logger import Logger
import openpyxl
import formulas


class ResultsHandler(BaseHTTPRequestHandler):
    reader: Optional[ProgressReader] = None
    logger: Optional[Logger] = None

    def _get_recent_results(self, limit: int) -> List[Dict]:
        if not self.reader:
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
            files.sort(key=lambda f: os.path.getmtime(os.path.join(result_dir, f))
            xlsx_name = files[-1]

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
        return [item for _, item in candidates[:limit]]

    def _send_json(self, status: int, payload: Dict):
        import json

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))
        self.end_headers()
        self.wfile.write(body)

    def _process_xlsx_with_formulas(self, xlsx_path: str) -> bytes:
        import tempfile
        import shutil

        wb = None
        try:
            temp_dir = tempfile.mkdtemp()
            try:
                xl_model = formulas.ExcelModel().loads(xlsx_path).finish()
                xl_model.calculate()
                result = xl_model.write(dirpath=temp_dir)

                if result:
                    for file_info in result.values():
                        if isinstance(file_info, dict) and len(file_info) > 0:
                            book_key = list(file_info.keys())[0]
                            wb = file_info[book_key]
                            if isinstance(wb, openpyxl.Workbook):
                                output = io.BytesIO()
                                wb.save(output)
                                output.seek(0)
                                wb.close()
                                return output.read()
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as exc:
            if self.logger:
                self.logger.error(f"使用formulas计算失败: {exc}, 降级到openpyxl")

        try:
            wb = openpyxl.load_workbook(xlsx_path, data_only=True)
            output = io.BytesIO()
            wb.save(output)
            output.seek(0)
            wb.close()
            return output.read()
        except Exception as exc:
            if self.logger:
                self.logger.error(f"处理xlsx文件失败: {exc}")
            raise

    def _send_file(self, file_path: str, content_type: str):
        try:
            if (
                content_type
                == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ):
                data = self._process_xlsx_with_formulas(file_path)
            else:
                with open(file_path, "rb") as f:
                    data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data))
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            import traceback

            error_info = f"读取文件失败: {exc}\n{traceback.format_exc()}"
            if self.logger:
                self.logger.error(error_info)
            self._send_json(500, {"error": "file_read_failed", "details": str(exc)})

    def do_GET(self):  # noqa: N802
        if not self.reader:
            self._send_json(500, {"error": "reader_not_ready"})
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/client/results/latest":
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
                files.sort(key=lambda f: os.path.getmtime(os.path.join(result_dir, f))
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
            files.sort(key=lambda f: os.path.getmtime(os.path.join(result_dir, f))
            if not files:
                self._send_json(404, {"error": "xlsx_not_found"})
                return

            xlsx_path = os.path.join(result_dir, files[-1])
            self._send_file(
                xlsx_path,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            return

        if path == "/client/results/images":
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

        if path.startswith("/client/results/image/"):
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
