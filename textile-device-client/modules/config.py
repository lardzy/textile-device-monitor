"""Configuration loading, migration, validation and atomic persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Dict, Optional

from modules.transport_security import (
    CONFIG_SCHEMA_VERSION,
    TRANSPORT_COMPATIBLE,
    TRANSPORT_REQUIRED,
    TransportSecurityError,
    normalize_server_origin,
    resolve_ca_bundle,
)


PACKAGED_DEFAULTS_NAME = "client-build-defaults.json"
DEFAULT_SERVER_URL = "https://textile-monitor.internal"
DEFAULT_TLS_CA_BUNDLE = "certs/inspection-root-ca.pem"

DEFAULT_CONFIG = {
    "config_schema_version": CONFIG_SCHEMA_VERSION,
    "device_code": "1号",
    "device_name": "1号",
    "server_url": DEFAULT_SERVER_URL,
    "transport_security": TRANSPORT_REQUIRED,
    "tls_ca_bundle": DEFAULT_TLS_CA_BUNDLE,
    "working_path": "",
    "is_laser_confocal": False,
    "log_path": "C:\\ProgramData\\OLYMPUS\\LEXT-OLS50-SW\\Log\\Olympus.log",
    "report_interval": 5,
    "results_port": 9100,
    "log_level": "INFO",
    "manual_status": None,
    "is_first_run": True,
    "device_registered": False,
}


class ConfigValidationError(ValueError):
    """Invalid local client configuration."""


def _application_directory(config_path: Path) -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return config_path.parent


def _load_packaged_defaults(config_path: Path) -> Dict[str, Any]:
    defaults = DEFAULT_CONFIG.copy()
    defaults_path = _application_directory(config_path) / PACKAGED_DEFAULTS_NAME
    if not defaults_path.is_file():
        return defaults
    try:
        payload = json.loads(defaults_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigValidationError(
            f"安装包默认配置无效：{defaults_path}"
        ) from exc

    allowed = {
        "config_schema_version",
        "server_url",
        "transport_security",
        "tls_ca_bundle",
    }
    defaults.update({key: value for key, value in payload.items() if key in allowed})
    return defaults


def validate_config(
    candidate: Dict[str, Any],
    *,
    defaults: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate a complete config and return a normalized copy."""

    normalized = dict(defaults or DEFAULT_CONFIG)
    normalized.update(candidate)

    try:
        schema_version = int(normalized.get("config_schema_version"))
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError("配置版本号无效") from exc
    if schema_version != CONFIG_SCHEMA_VERSION:
        raise ConfigValidationError(
            f"不支持的配置版本：{schema_version}，当前仅支持版本 "
            f"{CONFIG_SCHEMA_VERSION}"
        )

    transport_security = str(normalized.get("transport_security") or "").strip()
    try:
        normalized["server_url"] = normalize_server_origin(
            normalized.get("server_url"),
            transport_security,
        )
    except TransportSecurityError as exc:
        raise ConfigValidationError(exc.user_message) from exc
    normalized["transport_security"] = transport_security

    tls_ca_bundle = str(normalized.get("tls_ca_bundle") or "").strip()
    if normalized["server_url"].startswith("https://") and not tls_ca_bundle:
        raise ConfigValidationError("HTTPS 配置必须指定内部 CA 证书文件")
    normalized["tls_ca_bundle"] = tls_ca_bundle
    normalized["config_schema_version"] = CONFIG_SCHEMA_VERSION

    try:
        report_interval = int(normalized.get("report_interval", 5))
        results_port = int(normalized.get("results_port", 9100))
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError("上报间隔或结果服务端口必须为整数") from exc
    if not 1 <= report_interval <= 3600:
        raise ConfigValidationError("上报间隔必须在 1 到 3600 秒之间")
    if not 1 <= results_port <= 65535:
        raise ConfigValidationError("结果服务端口必须在 1 到 65535 之间")
    normalized["report_interval"] = report_interval
    normalized["results_port"] = results_port
    return normalized


class Config:
    def __init__(self, config_file: str = "config.json"):
        self.config_file = config_file
        self.config: Dict[str, Any] = {}
        self.last_load_error: Optional[str] = None
        self._last_mtime = 0.0
        self._defaults = _load_packaged_defaults(self.path)
        self.config = self._validated_defaults()
        self.load()

    @property
    def path(self) -> Path:
        return Path(self.config_file).expanduser().resolve(strict=False)

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.bak")

    @property
    def application_directory(self) -> Path:
        return _application_directory(self.path)

    def _validated_defaults(self) -> Dict[str, Any]:
        return validate_config(self._defaults, defaults=self._defaults)

    def _prepare_loaded_config(
        self,
        raw_config: Dict[str, Any],
    ) -> tuple[Dict[str, Any], bool]:
        if not isinstance(raw_config, dict):
            raise ConfigValidationError("配置文件根节点必须是 JSON 对象")

        schema_value = raw_config.get("config_schema_version")
        migrated = schema_value is None or schema_value == 1 or schema_value == "1"
        prepared = dict(raw_config)
        if migrated:
            # Existing v1 installs keep their current HTTP endpoint until the
            # administrator performs the controlled HTTPS migration.
            prepared["config_schema_version"] = CONFIG_SCHEMA_VERSION
            prepared["transport_security"] = TRANSPORT_COMPATIBLE
            prepared.setdefault(
                "tls_ca_bundle",
                self._defaults.get("tls_ca_bundle", DEFAULT_TLS_CA_BUNDLE),
            )
        return validate_config(prepared, defaults=self._defaults), migrated

    def _read_candidate(self) -> tuple[Dict[str, Any], bool]:
        if not self.path.exists():
            return self._validated_defaults(), False
        try:
            raw_config = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigValidationError(f"无法读取配置文件：{exc}") from exc
        return self._prepare_loaded_config(raw_config)

    def load_candidate(self) -> Dict[str, Any]:
        """Read and validate disk state without changing the active config."""

        candidate, _ = self._read_candidate()
        return candidate.copy()

    def load(self) -> Dict[str, Any]:
        """Load valid disk state; retain the active config on failure."""

        try:
            loaded_config, migrated = self._read_candidate()
            if migrated:
                self._write_atomic(loaded_config, create_backup=True)
            self.config = loaded_config
            self.last_load_error = None
        except ConfigValidationError as exc:
            self.last_load_error = str(exc)
            print(f"加载配置文件失败：{exc}；保留当前有效配置")
        return self.config.copy()

    def apply_candidate(self, candidate: Dict[str, Any]) -> None:
        """Activate an already validated candidate without writing it again."""

        self.config = validate_config(candidate, defaults=self._defaults)
        self.last_load_error = None

    def _write_atomic(
        self,
        candidate: Dict[str, Any],
        *,
        create_backup: bool,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                json.dump(candidate, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
                temporary_path = Path(stream.name)

            if create_backup and self.path.is_file():
                backup_fd, backup_temporary_name = tempfile.mkstemp(
                    dir=self.path.parent,
                    prefix=f".{self.backup_path.name}.",
                    suffix=".tmp",
                )
                os.close(backup_fd)
                backup_temporary = Path(backup_temporary_name)
                try:
                    shutil.copy2(self.path, backup_temporary)
                    os.replace(backup_temporary, self.backup_path)
                finally:
                    backup_temporary.unlink(missing_ok=True)
            os.replace(temporary_path, self.path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def save(self) -> bool:
        """Validate and atomically save the active configuration."""

        try:
            candidate = validate_config(self.config, defaults=self._defaults)
            self._write_atomic(candidate, create_backup=True)
            self.config = candidate
            self.last_load_error = None
            return True
        except (ConfigValidationError, OSError) as exc:
            self.last_load_error = str(exc)
            print(f"保存配置文件失败：{exc}")
            return False

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set(self, key: str, value: Any) -> bool:
        return self.update({key: value})

    def get_all(self) -> Dict[str, Any]:
        return self.config.copy()

    def _get_config_mtime(self) -> float:
        if self.path.exists():
            return self.path.stat().st_mtime
        return 0

    def is_config_changed(self) -> bool:
        return self._get_config_mtime() != self._last_mtime

    def set_last_mtime(self):
        self._last_mtime = self._get_config_mtime()

    def update(self, updates: Dict[str, Any]) -> bool:
        """Atomically validate and persist updates without partial mutation."""

        candidate = self.config.copy()
        candidate.update(updates)
        candidate["config_schema_version"] = CONFIG_SCHEMA_VERSION
        try:
            candidate = validate_config(candidate, defaults=self._defaults)
            self._write_atomic(candidate, create_backup=True)
        except (ConfigValidationError, OSError) as exc:
            self.last_load_error = str(exc)
            print(f"保存配置文件失败：{exc}")
            return False
        self.config = candidate
        self.last_load_error = None
        return True

    def resolve_tls_ca_bundle(
        self,
        candidate: Optional[Dict[str, Any]] = None,
    ) -> Path:
        config = candidate or self.config
        return resolve_ca_bundle(
            config.get("tls_ca_bundle"),
            self.application_directory,
        )

    def is_first_run(self) -> bool:
        return self.config.get("is_first_run", True)

    def mark_configured(self) -> bool:
        return self.set("is_first_run", False)

    def is_device_registered(self) -> bool:
        return self.config.get("device_registered", False)

    def mark_device_registered(self) -> bool:
        return self.set("device_registered", True)

    def get_device_code(self) -> str:
        return self.config.get("device_code", "1号")

    def get_device_name(self) -> str:
        return self.config.get("device_name", "1号")

    def get_server_url(self) -> str:
        return self.config.get("server_url", DEFAULT_SERVER_URL)

    def get_transport_security(self) -> str:
        return self.config.get("transport_security", TRANSPORT_REQUIRED)

    def get_tls_ca_bundle(self) -> str:
        return self.config.get("tls_ca_bundle", DEFAULT_TLS_CA_BUNDLE)

    def get_working_path(self) -> str:
        return self.config.get("working_path", "")

    def is_laser_confocal(self) -> bool:
        return bool(self.config.get("is_laser_confocal", False))

    def get_log_path(self) -> str:
        return self.config.get(
            "log_path",
            "C:\\ProgramData\\OLYMPUS\\LEXT-OLS50-SW\\Log\\Olympus.log",
        )

    def get_report_interval(self) -> int:
        return self.config.get("report_interval", 5)

    def get_results_port(self) -> int:
        return int(self.config.get("results_port", 9100))

    def get_manual_status(self) -> Optional[str]:
        return self.config.get("manual_status")

    def set_manual_status(self, status: Optional[str]) -> bool:
        return self.set("manual_status", status)
