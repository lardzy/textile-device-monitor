"""
进度文件读取模块
"""

import os
import re
import threading
from collections import deque
from datetime import datetime
from typing import Optional, Dict, Any
from .logger import Logger


class ProgressReader:
    def __init__(self, working_path: str, logger: Logger, results_port: int = 9100):
        self.working_path = working_path
        self.logger = logger
        self.results_port = results_port
        self.is_laser_confocal = False

    def read_progress(self) -> int:
        """读取设备进度

        Returns:
            int: 进度值 (0-100)
        """
        if not self.working_path:
            self.logger.warning("工作路径未配置，进度设为 0")
            return 0

        self.logger.debug(f"尝试读取工作路径: {self.working_path}")

        try:
            if not os.path.exists(self.working_path):
                self.logger.warning(f"工作路径不存在: {self.working_path}，进度设为 0")
                return 0

            latest_folder = self._get_latest_modified_folder(self.working_path)
            if not latest_folder:
                self.logger.warning("未找到可用的子文件夹，进度设为 0")
                return 0

            progress = self._check_progress(latest_folder)
            self.logger.debug(f"当前最新文件夹: {latest_folder}，进度: {progress}%")
            return progress

        except PermissionError:
            self.logger.error(f"无权限访问工作路径: {self.working_path}")
            return 0
        except Exception as e:
            self.logger.error(f"读取工作路径失败: {e}，进度设为 0")
            return 0

    def check_path_accessible(self) -> bool:
        """检查工作路径是否可访问

        Returns:
            bool: 是否可访问
        """
        if not self.working_path:
            self.logger.warning("工作路径未配置")
            return False

        try:
            if not os.path.exists(self.working_path):
                self.logger.warning(f"工作路径不存在: {self.working_path}")
                return False

            if not os.path.isdir(self.working_path):
                self.logger.error(f"工作路径不是文件夹: {self.working_path}")
                return False

            self.logger.debug(f"工作路径可访问: {self.working_path}")
            return True

        except Exception as e:
            self.logger.error(f"检查工作路径失败: {e}")
            return False

    def _get_latest_modified_folder(self, base_path: str) -> Optional[str]:
        """获取指定路径下最近修改的子文件夹"""
        try:
            entries = [
                os.path.join(base_path, name)
                for name in os.listdir(base_path)
                if os.path.isdir(os.path.join(base_path, name))
            ]
            if not entries:
                return None
            entries.sort(key=lambda p: os.path.getmtime(p))
            return entries[-1]
        except Exception as e:
            self.logger.error(f"获取最新文件夹失败: {e}")
            return None

    def get_latest_folder_name(self) -> Optional[str]:
        """获取最新文件夹名称"""
        latest_folder = self._get_latest_modified_folder(self.working_path)
        if not latest_folder:
            return None
        return os.path.basename(latest_folder)

    def _check_progress(self, folder_path: str) -> int:
        """根据文件夹结构判断进度"""
        result_folder = os.path.join(folder_path, "result")
        original_image = os.path.join(folder_path, "original_image")
        mask = os.path.join(folder_path, "mask")
        cut_pic = os.path.join(folder_path, "cut_pic")

        if os.path.exists(result_folder) and os.listdir(result_folder):
            return 100

        if (
            os.path.exists(original_image)
            and os.path.exists(mask)
            and os.path.exists(cut_pic)
            and os.path.exists(result_folder)
            and not os.listdir(result_folder)
        ):
            return 80

        if os.path.exists(original_image) and os.path.exists(cut_pic):
            return 20

        return 0

    def get_client_base_url(self) -> Optional[str]:
        """构建客户端结果服务地址"""
        try:
            import socket

            host = None
            hostname = socket.gethostname()
            candidates = socket.gethostbyname_ex(hostname)[2]
            for candidate in candidates:
                if candidate.startswith("127."):
                    continue
                if candidate == "0.0.0.0":
                    continue
                host = candidate
                break
            if not host:
                host = socket.gethostbyname(hostname)
                if host.startswith("127.") or host == "0.0.0.0":
                    host = None

            port = getattr(self, "results_port", None)
            if not port:
                port = 9100
            if not host:
                return None
            return f"http://{host}:{port}"
        except Exception:
            return None


class OlympusProgressReader(ProgressReader):
    def __init__(self, log_path: str, logger: Logger, results_port: int = 9100):
        super().__init__(working_path="", logger=logger, results_port=results_port)
        self.log_path = log_path
        self.is_laser_confocal = True
        if os.name == "nt":
            self._encoding = "gbk"
            self._decode_candidates = [
                "gbk",
                "cp936",
                "gb2312",
                "big5",
                "mbcs",
                "utf-8",
                "latin1",
            ]
        else:
            self._encoding = "utf-8"
            self._decode_candidates = ["utf-8", "gbk", "gb2312", "big5", "latin1"]

        # 线程锁和状态变量
        self._lock = threading.Lock()
        self._offset = 0
        self._initialized = False
        self._current_output_path: Optional[str] = None
        self._current_basename: Optional[str] = None
        self._current_group: Optional[str] = None
        self._groups_started: set[str] = set()
        self._groups_completed: set[str] = set()
        self._group_total: Optional[int] = None
        self._max_group_index = 0
        self._current_frame = 0
        self._current_frame_max = 0
        self._last_frame_total: Optional[int] = None
        self._current_state: Optional[str] = None
        self._task_started = False
        self._task_finished = False
        self._task_started_at: Optional[datetime] = None
        self._last_frame_time: Optional[datetime] = None
        self._has_exports = False
        self._has_frames = False
        self._current_file_name: Optional[str] = None
        self._xy_position: Optional[tuple[int, int]] = None
        self._z_position: Optional[int] = None
        self._acquisition_active = False
        self._recent_results = deque(maxlen=60)
        self._pending_bytes = b""
        self._frame_total: Optional[int] = None
        self._z_range: Optional[tuple[int, int]] = None
        self._output_path_candidates: list[str] = []

        # 定义所有正则表达式
        self._TIMESTAMP_PATTERN = re.compile(
            r"^(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}\.\d+)"
        )
        self._DATAPATH_PATTERN = re.compile(r"datapath=([^\s)]+)")
        self._BASENAME_PATTERN = re.compile(r"basename=([^\s,]+)")
        self._STATE_PATTERN = re.compile(r"Enter State \((State[^)]+)\)")
        self._FRAME_PATTERN = re.compile(
            r"frame:z(\d+)_\d+_\d+"
        )  # 修复：支持frame:z527_0_1格式
        self._GROUP_START_PATTERN = re.compile(
            r"notifyProtocolGroupStarted.*name=G(\d{3})"
        )
        self._GROUP_END_PATTERN = re.compile(
            r"notifyProtocolGroupCompleted.*name=G(\d{3}).*action=end"
        )
        self._EXPORT_PATTERN = re.compile(r"exportAreaImage\(\) filename=([^\s,]+)")
        self._EXPORT_NOTIFY_PATTERN = re.compile(
            r"notifyExportImage.*filename=([^\s,]+)"
        )
        self._EXPORT_PATH_PATTERN = re.compile(r"notifyExportImage.*path=([^,]+)")
        self._EXPORT_PATH_BYTES_PATTERN = re.compile(
            rb"notifyExportImage.*path=([^,]+)"
        )
        self._GROUP_FROM_FILENAME_PATTERN = re.compile(r"_G(\d{3})_A\d+")
        self._GROUP_TOTAL_PATTERN = re.compile(r"numberOfGroup=(\d+)")
        self._XY_PATTERN = re.compile(r"3NXYP\s+(-?\d+),(-?\d+),0")
        self._Z_PATTERN = re.compile(r"1PE\s+(-?\d+),0")
        self._STAGE_POS_PATTERN = re.compile(r"stagePosition\"?[=:](-?\d+)")
        self._REPEAT_COUNT_PATTERN = re.compile(
            r"SetZLoopParam.*?repeatCount\"?[=:](\d+)"
        )
        self._Z_START_PATTERN = re.compile(
            r"SetZLoopParam.*?startPosition\"?[=:](-?\d+)"
        )
        self._Z_END_PATTERN = re.compile(r"SetZLoopParam.*?endPosition\"?[=:](-?\d+)")

        # 添加路径解码候选
        self._path_decode_candidates = [
            "gbk",
            "cp936",
            "gb2312",
            "big5",
            "mbcs",
            "utf-8",
            "latin1",
        ]

    def _count_cjk(self, text: str) -> int:
        return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")

    def _maybe_fix_mojibake(self, text: str) -> str:
        if not text:
            return text

        best_candidate = text
        best_cjk_count = self._count_cjk(text)

        # 扩展的编码转换尝试
        fix_attempts = [
            ("latin1", "gbk"),
            ("latin1", "gb2312"),
            ("latin1", "big5"),
            ("latin1", "cp936"),
            ("latin1", "cp950"),
            ("utf-8", "gbk"),
            ("utf-8", "gb2312"),
            ("cp1252", "gbk"),
            ("cp1252", "gb2312"),
        ]

        for encode_fmt, decode_fmt in fix_attempts:
            try:
                candidate = text.encode(encode_fmt).decode(decode_fmt)
                cjk_count = self._count_cjk(candidate)

                # 检查是否是有效的网络路径
                if cjk_count > best_cjk_count and (
                    "\\\\" in candidate or "//" in candidate or ":" in candidate
                ):
                    # 额外检查常见中文关键词
                    chinese_keywords = ["检验", "特纤", "横截面", "八部"]
                    if any(keyword in candidate for keyword in chinese_keywords):
                        best_candidate = candidate
                        best_cjk_count = cjk_count
            except (UnicodeEncodeError, UnicodeDecodeError, AttributeError):
                continue

        # 如果没有找到中文，但路径结构正确，尝试用常见中文部分替换
        if best_cjk_count == 0 and ("\\\\" in best_candidate or "//" in best_candidate):
            # 检查路径中是否包含已知的中文占位符模式
            if "鐬琝" in best_candidate:
                best_candidate = best_candidate.replace("鐬琝", "检验八部")
            if any(char in best_candidate for char in ["瞬", "‰", ""]):
                best_candidate = (
                    best_candidate.replace("瞬", "特纤")
                    .replace("‰", "特纤")
                    .replace("", "特纤")
                )

        return best_candidate

    def _score_decoded_path(self, text: str, errors: int) -> int:
        cjk = self._count_cjk(text)
        control = sum(1 for ch in text if ord(ch) < 32 and ch not in ("\\", "/"))

        # 检查是否是有效的网络路径或文件路径
        path_validity = 0
        if "\\\\" in text or "//" in text:  # 网络路径
            path_validity += 10
        if ":" in text and ("\\" in text or "/" in text):  # 驱动器路径
            path_validity += 5
        if any(char in text for char in "检验八部特纤横截面"):  # 常见中文文件夹名
            path_validity += 15
        if not text.count("\ufffd") and errors == 0:  # 无解码错误
            path_validity += 8

        return cjk * 6 - errors * 10 - control * 5 + path_validity

    def _decode_path_bytes(self, raw: bytes) -> str:
        best_text = ""
        best_score = -(10**9)
        for encoding in self._path_decode_candidates:
            try:
                decoded = raw.decode(encoding)
                errors = 0
            except UnicodeDecodeError:
                decoded = raw.decode(encoding, errors="replace")
                errors = decoded.count("\ufffd")
            score = self._score_decoded_path(decoded, errors)
            if score > best_score:
                best_score = score
                best_text = decoded
        return self._maybe_fix_mojibake(best_text)

    def _build_path_candidates(self, raw: bytes, fallback: Optional[str]) -> list[str]:
        seen: set[str] = set()
        candidates: list[str] = []
        for encoding in self._path_decode_candidates:
            for errors in ("strict", "replace"):
                try:
                    decoded = raw.decode(encoding, errors=errors)
                except UnicodeDecodeError:
                    continue
                decoded = self._maybe_fix_mojibake(decoded)
                if decoded and decoded not in seen:
                    seen.add(decoded)
                    candidates.append(decoded)
        try:
            latin1_fixed = raw.decode("latin1").encode("latin1").decode("gbk")
            latin1_fixed = self._maybe_fix_mojibake(latin1_fixed)
            if latin1_fixed and latin1_fixed not in seen:
                candidates.append(latin1_fixed)
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        if fallback:
            normalized = self._maybe_fix_mojibake(fallback)
            if normalized and normalized not in seen:
                candidates.append(normalized)
        return candidates

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._initialize_from_tail()

    def _initialize_from_tail(self) -> None:
        if not self.log_path:
            self._initialized = True
            return
        if not os.path.exists(self.log_path):
            self.logger.warning(f"日志文件不存在: {self.log_path}")
            self._initialized = True
            return
        try:
            file_size = os.path.getsize(self.log_path)
            if file_size <= 0:
                self._offset = 0
                self._initialized = True
                return
            tail_bytes = min(file_size, 200000)
            with open(self.log_path, "rb") as f:
                f.seek(file_size - tail_bytes)
                data = f.read()
            if not data:
                self._offset = file_size
                self._initialized = True
                return
            lines = data.split(b"\n")
            if tail_bytes < file_size and lines:
                lines = lines[1:]
            if data.endswith(b"\n"):
                self._pending_bytes = b""
            else:
                self._pending_bytes = lines[-1] if lines else b""
                lines = lines[:-1]
            for line_bytes in lines:
                line = self._decode_line_bytes(line_bytes).rstrip("\r")
                self._process_line(line, line_bytes=line_bytes)
            self._offset = file_size
            self._initialized = True
        except Exception as exc:
            self.logger.error(f"初始化日志读取失败: {exc}")
            self._offset = 0
            self._pending_bytes = b""
            self._initialized = True

    def _read_new_lines(self) -> None:
        if not self.log_path:
            return
        if not os.path.exists(self.log_path):
            return
        try:
            file_size = os.path.getsize(self.log_path)
            if file_size < self._offset:
                self._offset = 0
                self._pending_bytes = b""
            if file_size <= self._offset and not self._pending_bytes:
                return
            with open(self.log_path, "rb") as f:
                f.seek(self._offset)
                data = f.read()
                if not data:
                    return
                buffer = self._pending_bytes + data
                lines = buffer.split(b"\n")
                if buffer.endswith(b"\n"):
                    self._pending_bytes = b""
                    complete_lines = lines[:-1]
                else:
                    self._pending_bytes = lines[-1]
                    complete_lines = lines[:-1]
                for line_bytes in complete_lines:
                    line = self._decode_line_bytes(line_bytes).rstrip("\r")
                    self._process_line(line, line_bytes=line_bytes)
                self._offset = f.tell()
        except Exception as exc:
            self.logger.error(f"读取日志失败: {exc}")

    def _refresh_state(self) -> None:
        with self._lock:
            self._ensure_initialized()
            self._read_new_lines()

    def _parse_timestamp(self, line: str) -> Optional[datetime]:
        match = self._TIMESTAMP_PATTERN.match(line)
        if not match:
            return None
        raw = match.group(1)
        if "." in raw:
            head, frac = raw.split(".", 1)
            frac = (frac + "000000")[:6]
            try:
                return datetime.strptime(f"{head}.{frac}", "%m/%d/%Y %H:%M:%S.%f")
            except ValueError:
                return None
        try:
            return datetime.strptime(raw, "%m/%d/%Y %H:%M:%S")
        except ValueError:
            return None

    def _decode_line_bytes(self, line_bytes: bytes) -> str:
        for encoding in self._decode_candidates:
            try:
                return line_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue
        return line_bytes.decode(self._encoding, errors="replace")

    def _reset_acquisition_state(self) -> None:
        self._groups_started = set()
        self._groups_completed = set()
        self._group_total = None
        self._max_group_index = 0
        self._current_group = None
        self._current_frame = 0
        self._current_frame_max = 0
        self._last_frame_total = None
        self._frame_total = None
        self._z_range = None
        self._acquisition_active = False
        self._task_started = False
        self._task_finished = False
        self._task_started_at = None
        self._last_frame_time = None
        self._has_exports = False
        self._has_frames = False
        self._current_file_name = None
        self._output_path_candidates = []

    def _record_recent_result(self, path: str, timestamp: Optional[datetime]) -> None:
        if not path:
            return
        self._recent_results.append({"path": path, "timestamp": timestamp})

    def _is_unc_path(self, path: str) -> bool:
        return path.startswith("\\\\") or path.startswith("//")

    def _is_temp_output_path(self, path: str) -> bool:
        """检查是否为临时输出路径，排除Olympus设备的临时文件地址"""
        if not path:
            return True

        normalized = path.replace("/", "\\").lower()

        # Olympus设备临时文件路径模式
        olympus_temp_patterns = [
            "programdata\\olympus\\lext-ols50-sw\\microscopeapp\\temp\\image",
            "microscopeapp\\temp\\image",  # 保留原有的检查
            "temp\\image",  # 更宽泛的临时路径检查
        ]

        # 检查是否匹配任何临时路径模式
        for pattern in olympus_temp_patterns:
            if pattern in normalized:
                return True

        # 检查是否为系统临时目录下的文件
        if normalized.startswith("c:\\programdata\\") and "temp" in normalized:
            return True

        if normalized.startswith("c:\\windows\\temp\\"):
            return True

        return False

    def _update_output_path(
        self, path: str, timestamp: Optional[datetime], allow_reset: bool = True
    ) -> None:
        if not path:
            return
        normalized = self._maybe_fix_mojibake(path.strip())
        if not normalized:
            return
        if self._current_output_path:
            current_is_unc = self._is_unc_path(self._current_output_path)
            next_is_unc = self._is_unc_path(normalized)
            if normalized != self._current_output_path:
                should_reset = allow_reset and (
                    self._task_finished or not self._task_started
                )
                if should_reset:
                    self._record_recent_result(self._current_output_path, timestamp)
                    self._reset_acquisition_state()
                self._current_output_path = normalized
                self.working_path = os.path.dirname(normalized)
        else:
            self._current_output_path = normalized
            self.working_path = os.path.dirname(normalized)
            if allow_reset and (self._task_finished or not self._task_started):
                self._reset_acquisition_state()
        if normalized and normalized not in self._output_path_candidates:
            self._output_path_candidates = [normalized] + self._output_path_candidates
        self._record_recent_result(normalized, timestamp)

    def _start_acquisition(self, timestamp: Optional[datetime]) -> None:
        if self._task_finished or not self._task_started:
            self._reset_acquisition_state()
        self._mark_task_started(timestamp)
        self._acquisition_active = True

    def _finish_acquisition(self, timestamp: Optional[datetime]) -> None:
        if self._task_started:
            self._task_finished = True
        self._acquisition_active = False
        self._current_group = None
        self._current_frame = 0
        self._current_frame_max = 0
        if self._current_output_path:
            self._record_recent_result(self._current_output_path, timestamp)

    def _mark_task_started(self, timestamp: Optional[datetime]) -> None:
        if not self._task_started:
            self._task_started = True
            self._task_finished = False
            self._task_started_at = timestamp
        self._acquisition_active = True

    def _process_line(self, line: str, line_bytes: Optional[bytes] = None) -> None:
        if not line:
            return
        timestamp = self._parse_timestamp(line)

        if "saveMATLProperties" in line and "datapath=" in line:
            match = self._DATAPATH_PATTERN.search(line)
            if match:
                datapath = match.group(1).strip()
                self._update_output_path(datapath, timestamp, allow_reset=False)

        if "createImage" in line and "basename=" in line:
            match = self._BASENAME_PATTERN.search(line)
            if match:
                self._current_basename = match.group(1)

        state_match = self._STATE_PATTERN.search(line)
        if state_match:
            self._current_state = state_match.group(1)

        if (
            "notifyMATLStarted" in line
            or "Acquisition start" in line
            or "ProgressDialog is opened" in line
        ):
            self._start_acquisition(timestamp)

        if (
            "notifyMATLFinished" in line
            or "MATLFINISH()" in line
            or "MATLCOMP" in line
            or "Acquisition end" in line
            or "ProgressDialog is closed" in line
        ):
            self._finish_acquisition(timestamp)

        group_start = self._GROUP_START_PATTERN.search(line)
        if group_start:
            group_index = int(group_start.group(1))
            group_name = f"G{group_index:03d}"
            self._current_group = group_name
            self._groups_started.add(group_name)
            self._max_group_index = max(self._max_group_index, group_index)
            self._current_frame = 0
            self._current_frame_max = 0
            self._mark_task_started(timestamp)

        group_end = self._GROUP_END_PATTERN.search(line)
        if group_end:
            group_index = int(group_end.group(1))
            group_name = f"G{group_index:03d}"
            self._groups_completed.add(group_name)
            if self._current_group == group_name and self._current_frame_max > 0:
                self._last_frame_total = self._current_frame_max
            if self._current_group == group_name:
                self._current_group = None
                self._current_frame = 0
                self._current_frame_max = 0

        frame_match = self._FRAME_PATTERN.search(line)
        if frame_match:
            frame_index = int(frame_match.group(1))
            self._current_frame = frame_index
            self._current_frame_max = max(self._current_frame_max, frame_index)
            self._last_frame_time = timestamp
            self._has_frames = True
            self._mark_task_started(timestamp)

        export_match = self._EXPORT_PATTERN.search(line)
        if export_match:
            filename = export_match.group(1)
            self._current_file_name = filename
            self._has_exports = True
            self._mark_task_started(timestamp)
            self._update_group_from_filename(filename)

        notify_export_match = self._EXPORT_NOTIFY_PATTERN.search(line)
        if notify_export_match:
            filename = notify_export_match.group(1)
            self._current_file_name = filename
            self._has_exports = True
            self._mark_task_started(timestamp)
            self._update_group_from_filename(filename)

        export_path_match = self._EXPORT_PATH_PATTERN.search(line)
        export_path = None
        if line_bytes:
            raw_match = self._EXPORT_PATH_BYTES_PATTERN.search(line_bytes)
            if raw_match:
                raw_path = raw_match.group(1)
                export_path = self._decode_path_bytes(raw_path).strip()
                self._output_path_candidates = self._build_path_candidates(
                    raw_path,
                    export_path_match.group(1).strip() if export_path_match else None,
                )
        if export_path is None and export_path_match:
            export_path = export_path_match.group(1).strip()
            # 无论line_bytes是否存在，都尝试修复乱码
            export_path = self._maybe_fix_mojibake(export_path)
            if line_bytes is None:
                self._output_path_candidates = [export_path]
        if export_path:
            self._update_output_path(export_path, timestamp)

        if "SetZLoopParam" in line:
            repeat_match = self._REPEAT_COUNT_PATTERN.search(line)
            if repeat_match:
                repeat_count = int(repeat_match.group(1))
                if repeat_count > 0:
                    self._frame_total = repeat_count
            start_match = self._Z_START_PATTERN.search(line)
            end_match = self._Z_END_PATTERN.search(line)
            if start_match or end_match:
                start_value = int(start_match.group(1)) if start_match else None
                end_value = int(end_match.group(1)) if end_match else None
                if self._z_range:
                    if start_value is None:
                        start_value = self._z_range[0]
                    if end_value is None:
                        end_value = self._z_range[1]
                if start_value is not None and end_value is not None:
                    self._z_range = (start_value, end_value)

        xy_match = self._XY_PATTERN.search(line)
        if xy_match:
            self._xy_position = (int(xy_match.group(1)), int(xy_match.group(2)))
        if '"settingId":8' in line and "stagePosition" in line:
            stage_match = self._STAGE_POS_PATTERN.search(line)
            if stage_match:
                self._z_position = int(stage_match.group(1))

        z_match = self._Z_PATTERN.search(line)
        if z_match and self._z_position is None:
            self._z_position = int(z_match.group(1))

    def _update_group_from_filename(self, filename: str) -> None:
        match = self._GROUP_FROM_FILENAME_PATTERN.search(filename)
        if not match:
            return
        group_index = int(match.group(1))
        group_name = f"G{group_index:03d}"
        self._groups_completed.add(group_name)
        self._max_group_index = max(self._max_group_index, group_index)

    def _calculate_z_axis_progress(self) -> Optional[int]:
        """计算Z轴扫描进度"""

        if not (self._z_range and self._z_position is not None):
            return None

        # 计算Z轴总步数
        total_steps = abs(self._z_range[1] - self._z_range[0])  # stepSize通常为380
        if total_steps <= 0:
            return None

        # 计算当前步数（从起始位置开始）
        current_step = abs(self._z_position - self._z_range[0])

        # 计算进度百分比
        progress = int((current_step / max(total_steps, 1)) * 100)
        return max(0, min(100, progress))

    def _calculate_group_progress(self) -> Optional[int]:
        """计算组进度（基于已完成的组数和总组数）"""
        if not self._group_total or self._group_total <= 0:
            return None

        completed = len(self._groups_completed)

        # 如果有当前正在进行的组，计算其在总进度中的比例
        current_fraction = 0.0
        if self._current_group:
            try:
                current_group_index = (
                    int(self._current_group[1:])
                    if self._current_group.startswith("G")
                    else 0
                )
                current_fraction = current_group_index / self._group_total
            except (ValueError, IndexError):
                current_fraction = 0.0

        # 进度 = (已完成组数 + 当前组进度) / 总组数
        progress = ((completed + current_fraction) / self._group_total) * 100
        return max(0, min(99, int(progress)))  # 最大99%，避免显示100%直到真正完成

    def _calculate_image_progress_enhanced(self) -> Optional[int]:
        """计算当前图像进度（基于实际导出的图像数量）"""
        # 这里应该基于实际的图像导出数量和当前文件推断
        if not self._current_file_name:
            return None

        # 从文件名提取组信息
        match = self._GROUP_FROM_FILENAME_PATTERN.match(self._current_file_name)
        if not match:
            return None

        current_group_index = int(match.group(1))

        # 计算当前组的导出图像数量（通过历史记录估算）
        # 这里简化处理：假设每组有相同数量的图像，使用当前组索引
        total_groups = self._group_total or self._max_group_index or 1

        # 基于组进度和组内平均图像数估算
        group_progress = self._calculate_group_progress()
        if group_progress is None:
            return None

        # 如果是单组任务，基于Z轴进度估算
        if total_groups == 1:
            z_progress = self._calculate_z_axis_progress()
            return z_progress

        # 多组任务：基于组进度
        return group_progress

    def _calculate_image_progress(self) -> Optional[int]:
        """计算当前图像进度（基于当前帧位置估算）"""
        if not (
            self._current_frame
            and self._last_frame_total
            and self._last_frame_total > 0
        ):
            return None

        # 使用Z轴位置来估算进度，因为每个Z轴位置通常对应一个图像
        z_progress = self._calculate_z_axis_progress()
        if z_progress is not None:
            return z_progress

        # 基于历史最大帧数计算进度
        if self._frame_total and self._frame_total > 0:
            progress = int((self._current_frame / self._frame_total) * 100)
            return max(0, min(100, progress))

    def _calculate_overall_progress(self) -> int:
        if not self._task_started:
            return 0
        if self._task_finished:
            return 100

        # 计算总组数
        total_groups = self._group_total or self._max_group_index
        if total_groups <= 0:
            return 0

        # 计算已完成的组数
        completed = len(self._groups_completed)

        # 计算当前组内的进度
        current_fraction = 0.0
        if (
            self._current_group
            and self._last_frame_total
            and self._last_frame_total > 0
        ):
            current_fraction = self._current_frame / self._last_frame_total

        # 计算总体进度
        progress = int(((completed + current_fraction) / total_groups) * 100)

        # 限制在99%，直到任务真正完成
        if not self._task_finished:
            progress = min(99, progress)

        return max(0, progress)

    def read_progress(self) -> int:
        self._refresh_state()
        return self._calculate_overall_progress()

    def check_path_accessible(self) -> bool:
        if not self.log_path:
            self.logger.warning("日志路径未配置")
            return False
        if not os.path.exists(self.log_path):
            self.logger.warning(f"日志路径不存在: {self.log_path}")
            return False
        return True

    def get_latest_folder_name(self) -> Optional[str]:
        self._refresh_state()
        if self._current_output_path:
            return os.path.basename(self._current_output_path)
        return None

    def get_current_output_path(self) -> Optional[str]:
        self._refresh_state()
        return self._current_output_path

    def resolve_output_folder(self, folder_param: Optional[str]) -> Optional[str]:
        self._refresh_state()
        if folder_param:
            candidate = os.path.normpath(folder_param)
            if os.path.isabs(candidate) and os.path.isdir(candidate):
                return candidate
            if self.working_path:
                joined = os.path.join(self.working_path, candidate)
                if os.path.isdir(joined):
                    return joined
        return self._current_output_path

    def on_output_folder_renamed(self, old_path: str, new_path: str) -> None:
        if not old_path or not new_path:
            return
        old_norm = os.path.normpath(old_path)
        new_norm = os.path.normpath(new_path)

        with self._lock:
            if self._current_output_path and os.path.normpath(self._current_output_path) == old_norm:
                self._current_output_path = new_norm
                self.working_path = os.path.dirname(new_norm)

            updated_recent = deque(maxlen=self._recent_results.maxlen)
            for entry in self._recent_results:
                entry_path = entry.get("path")
                if isinstance(entry_path, str) and os.path.normpath(entry_path) == old_norm:
                    updated_recent.append({**entry, "path": new_norm})
                else:
                    updated_recent.append(entry)
            self._recent_results = updated_recent

            updated_candidates: list[str] = []
            seen: set[str] = set()
            for candidate in self._output_path_candidates:
                next_candidate = new_norm if os.path.normpath(candidate) == old_norm else candidate
                if next_candidate not in seen:
                    seen.add(next_candidate)
                    updated_candidates.append(next_candidate)
            self._output_path_candidates = updated_candidates

    def get_recent_results(self, limit: int) -> list[Dict[str, Any]]:
        self._refresh_state()
        today = datetime.now().date()
        items: list[Dict[str, Any]] = []
        seen: set[str] = set()
        for entry in reversed(self._recent_results):
            path = entry.get("path")
            if not path or path in seen:
                continue
            seen.add(path)

            # 过滤临时路径
            if self._is_temp_output_path(path):
                continue

            timestamp = entry.get("timestamp")
            if timestamp and timestamp.date() != today:
                continue
            folder_name = os.path.basename(path)
            image_count = 0
            if os.path.isdir(path):
                image_count = len(
                    [
                        name
                        for name in os.listdir(path)
                        if name.lower().endswith((".jpg", ".jpeg", ".png"))
                    ]
                )
            updated_at = (
                timestamp.isoformat()
                if isinstance(timestamp, datetime)
                else datetime.now().isoformat()
            )
            items.append(
                {
                    "folder": path,
                    "task_name": folder_name,
                    "image_count": image_count,
                    "updated_at": updated_at,
                }
            )
            if len(items) >= limit:
                break
        return items

    def get_extra_metrics(self) -> Dict[str, Any]:
        self._refresh_state()

        # 图像进度（基于当前帧位置估算）
        image_progress = self._calculate_image_progress()

        # 总组数
        total_groups = self._group_total or (self._max_group_index or None)

        # 当前组信息
        group_current = None
        if self._current_group:
            try:
                group_current = (
                    int(self._current_group[1:])
                    if self._current_group.startswith("G")
                    else 0
                )
            except (ValueError, IndexError):
                group_current = None

        # Z轴进度（新增）
        z_progress = self._calculate_z_axis_progress()

        # 组进度
        group_progress = self._calculate_group_progress()

        olympus = {
            "state": self._current_state,
            "active": self.is_task_active(),
            "output_path": self._current_output_path,
            "output_path_candidates": self._output_path_candidates,
            "current_file": self._current_file_name,
            "frame_current": self._current_frame if self._current_frame else None,
            "frame_total": self._frame_total,
            "image_progress": image_progress,
            "group_current": group_current,
            "group_completed": len(self._groups_completed),
            "group_total": total_groups,
            "xy_position": (
                {"x": self._xy_position[0], "y": self._xy_position[1]}
                if self._xy_position
                else None
            ),
            "z_position": self._z_position,
            "z_range": (
                {"start": self._z_range[0], "end": self._z_range[1]}
                if self._z_range
                else None
            ),
            # 新增Z轴进度和相关信息
            "z_axis_progress": z_progress,
            "z_start": self._z_range[0] if self._z_range else None,
            "z_end": self._z_range[1] if self._z_range else None,
            "z_step": 380,  # stepSize通常为380
        }
        return {"device_type": "laser_confocal", "olympus": olympus}

    def get_device_state(self) -> Optional[str]:
        self._refresh_state()
        return self._current_state

    def is_task_active(self) -> bool:
        self._refresh_state()
        if self._task_finished:
            return False
        if self._acquisition_active:
            return True
        if self._task_started and not self._task_finished:
            if self._last_frame_time:
                delta = datetime.now() - self._last_frame_time
                if delta.total_seconds() <= 15:
                    return True
            if self._has_frames or self._has_exports or self._groups_started:
                return True
        return False
