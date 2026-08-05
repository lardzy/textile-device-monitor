import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:
    from pydantic import BaseModel, ConfigDict

    class BaseSettings(BaseModel):
        model_config = ConfigDict(extra="ignore")

        def __init__(self, **values):
            env_values = {}
            for field_name in self.__class__.model_fields:
                if field_name in values:
                    continue
                env_value = os.getenv(field_name)
                if env_value is not None:
                    env_values[field_name] = env_value
            env_values.update(values)
            super().__init__(**env_values)

    class SettingsConfigDict(dict):
        pass


RFC1918_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def is_rfc1918_network(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> bool:
    return isinstance(network, ipaddress.IPv4Network) and any(
        network.subnet_of(allowed) for allowed in RFC1918_NETWORKS
    )


class Settings(BaseSettings):
    # Production is deliberately the safe default. Local development must opt
    # in with APP_ENV=development (see backend/.env.example).
    APP_ENV: str = "production"
    DATABASE_URL: str = "sqlite:///./runtime/textile-monitor.db"
    SECRET_KEY: str = ""
    HEARTBEAT_TIMEOUT: int = 90
    HEARTBEAT_CHECK_INTERVAL: int = 10
    DATA_RETENTION_DAYS: int = 30
    STATUS_REPORT_RETENTION_HOURS: int = 48
    QUEUE_IDLE_REMIND_SECONDS: int = 60
    QUEUE_IDLE_TIMEOUT_SECONDS: int = 300
    QUEUE_IDLE_EXTEND_SECONDS: int = 300
    QUEUE_IDLE_CHECK_INTERVAL: int = 10
    RESULTS_RECENT_CACHE_TTL: int = 5
    RESULTS_RECENT_CACHE_STALE_TTL: int = 30
    RESULTS_RECENT_INFLIGHT_WAIT_SECONDS: int = 12
    OCR_ENABLED: bool = True
    OCR_SERVICE_URL: str = "http://ocr-adapter:5002"
    OCR_UPLOAD_DIR: str = "/tmp/ocr_uploads"
    OCR_OUTPUT_DIR: str = "/tmp/ocr_outputs"
    OCR_MAX_UPLOAD_MB: int = 30
    OCR_MAX_BATCH_FILES: int = 10
    OCR_JOB_TIMEOUT_SECONDS: int = 600
    OCR_MAX_CONCURRENT_JOBS: int = 1
    OCR_RETENTION_HOURS: int = 24
    AREA_ENABLED: bool = True
    AREA_OUTPUT_DIR: str = "/data/area_outputs"
    AREA_MAX_CONCURRENT_JOBS: int = 1
    AREA_ROOT_PATH_DEFAULT: str = "/tmp/area_inputs"
    AREA_WEIGHTS_DIR: str = "runtime/area-models"
    AREA_EXCEL_TEMPLATE_PATH: str = "/opt/area_templates/-面积法-定量试验原始记录-新系统.xls"
    AREA_INFER_URL: str = "http://area-infer:9001"
    AREA_INFER_TIMEOUT_SEC: int = 60
    STATS_TIMEZONE: str = "Asia/Shanghai"
    WEB_TRANSPORT: str = "http"
    PUBLIC_HOSTNAME: str = "localhost"
    PUBLIC_ORIGIN: str = "http://localhost"
    TLS_DIR_HOST_PATH: str = ""
    TLS_MIN_VALID_DAYS: int = 30
    HSTS_MAX_AGE: int = 0
    MANAGEMENT_CIDRS: str = ""
    # Browser traffic is same-origin behind Nginx in production, so CORS is
    # disabled unless an explicit allow-list is configured.
    CORS_ORIGINS: str = ""
    EXECUTION_ENABLED: bool = True
    EXECUTION_SESSION_SECRET: str = ""
    EXECUTION_SESSION_TTL_HOURS: int = 12
    EXECUTION_COOKIE_SECURE: bool = False
    EXECUTION_BOOTSTRAP_ADMIN_USERNAME: str = ""
    EXECUTION_BOOTSTRAP_ADMIN_PASSWORD: str = ""
    EXECUTION_CREDENTIAL_KEY: str = ""
    EXECUTION_SOURCE_ROOT: str = "/data/execution-input"
    EXECUTION_RUNTIME_ROOT: str = "/data/execution-runtime"
    EXECUTION_PUBLISH_ROOT: str = "/data/execution-publish"
    EXECUTION_INDEX_INTERVAL_SECONDS: int = 300
    EXECUTION_AUTO_INDEX_ROOT_IDS: str = (
        "regenerated_fiber_records,electron_microscopy_records,"
        "paper_fiber_records"
    )
    # Contract-review changes are normally visible within this window. A stale
    # value may still rank the catalog while one background refresh is queued;
    # the first image-selection release exposes stale/missing facts as a
    # warning instead of blocking the operator.
    EXECUTION_TASK_SNAPSHOT_TTL_MINUTES: int = 15
    EXECUTION_TASK_SNAPSHOT_RETRY_SECONDS: int = 60
    EXECUTION_WORKER_POLL_SECONDS: float = 1.0
    EXECUTION_WORKER_LEASE_SECONDS: int = 60
    EXECUTION_NODE_MAX_ATTEMPTS: int = 5
    EXECUTION_WORKER_HEARTBEAT_TIMEOUT_SECONDS: int = 45
    EXECUTION_WORKER_SCHEMA_WAIT_SECONDS: int = 180
    EXECUTION_OUTBOX_MAX_ATTEMPTS: int = 10
    EXECUTION_OUTBOX_RETENTION_DAYS: int = 7
    EXECUTION_SSE_MAX_SECONDS: int = 300
    EXECUTION_EXTERNAL_PREFLIGHT_TTL_MINUTES: int = 30
    EXECUTION_EXTERNAL_APPROVAL_TTL_MINUTES: int = 15
    # Independent kill switch plus shared secret for the external Bridge.
    # Both must be configured before any Bridge endpoint is available.
    EXECUTION_BRIDGE_ENABLED: bool = False
    EXECUTION_BRIDGE_TOKEN: str = ""
    # SpecialWool image upload/review remains an independent deployment
    # capability.  Keeping this separate from the Bridge master switch lets a
    # site validate the read-only task snapshot and writer before enabling the
    # two business mutations.
    EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED: bool = False
    # Keep the final CheckRecord save/proof connector independently disabled
    # until the Windows Bridge and writer for the deployment have been
    # validated.  A controlled 1 -> 2 test additionally requires the exact
    # sample number below; an empty value disables that exception package.
    EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED: bool = False
    EXECUTION_CONTROLLED_FINAL_ENTRY_TEST_SAMPLE_NO: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def cors_origins(self) -> list[str]:
        return [
            origin.strip().rstrip("/")
            for origin in self.CORS_ORIGINS.split(",")
            if origin.strip()
        ]

    def execution_auto_index_root_ids(self) -> list[str]:
        return list(
            dict.fromkeys(
                root_id.strip()
                for root_id in self.EXECUTION_AUTO_INDEX_ROOT_IDS.split(",")
                if root_id.strip()
            )
        )

    def management_cidrs(self) -> list[
        ipaddress.IPv4Network | ipaddress.IPv6Network
    ]:
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for raw_value in self.MANAGEMENT_CIDRS.split(","):
            value = raw_value.strip()
            if not value:
                continue
            try:
                networks.append(ipaddress.ip_network(value, strict=True))
            except ValueError as exc:
                raise RuntimeError(
                    f"MANAGEMENT_CIDRS contains an invalid network: {value}"
                ) from exc
        return networks

    def validate_execution_storage_paths(self) -> None:
        if not self.EXECUTION_ENABLED:
            return

        configured_paths = {
            "EXECUTION_SOURCE_ROOT": self.EXECUTION_SOURCE_ROOT,
            "EXECUTION_RUNTIME_ROOT": self.EXECUTION_RUNTIME_ROOT,
            "EXECUTION_PUBLISH_ROOT": self.EXECUTION_PUBLISH_ROOT,
        }
        resolved_paths: dict[str, Path] = {}
        for name, raw_value in configured_paths.items():
            value = str(raw_value).strip()
            if not value:
                raise RuntimeError(f"{name} must not be empty")
            resolved_paths[name] = Path(value).expanduser().resolve(strict=False)

        items = list(resolved_paths.items())
        for index, (left_name, left_path) in enumerate(items):
            for right_name, right_path in items[index + 1 :]:
                try:
                    same_existing_directory = (
                        left_path.exists()
                        and right_path.exists()
                        and left_path.samefile(right_path)
                    )
                except OSError:
                    same_existing_directory = False
                if (
                    same_existing_directory
                    or left_path == right_path
                    or left_path in right_path.parents
                    or right_path in left_path.parents
                ):
                    raise RuntimeError(
                        "Execution storage roots must be pairwise separate and "
                        "must not contain one another: "
                        f"{left_name}={left_path}, {right_name}={right_path}"
                    )

    def validate_execution_security(self) -> None:
        self.validate_execution_storage_paths()
        web_transport = self.WEB_TRANSPORT.strip().lower()
        if web_transport not in {"http", "https"}:
            raise RuntimeError("WEB_TRANSPORT must be either http or https")
        environment = self.APP_ENV.strip().lower()
        if environment not in {"production", "prod"}:
            return

        weak_values = {
            "",
            "your-secret-key-change-in-production",
            "dev-only-change-me",
            "change-me",
            "changeme",
            "password",
            "password123",
            "secret",
            "example",
        }
        weak_markers = (
            "placeholder",
            "change-me",
            "changeme",
            "your-secret",
            "example-value",
        )

        def is_weak(value: str, *, minimum_length: int = 1) -> bool:
            normalized = value.strip().lower()
            return (
                len(value.strip()) < minimum_length
                or normalized in weak_values
                or any(marker in normalized for marker in weak_markers)
            )

        try:
            database_url = make_url(self.DATABASE_URL.strip())
        except (ArgumentError, TypeError, ValueError) as exc:
            raise RuntimeError("Production DATABASE_URL is missing or invalid") from exc
        if database_url.get_backend_name() not in {"postgresql", "postgres"}:
            raise RuntimeError("Production DATABASE_URL must use PostgreSQL")
        database_password = str(database_url.password or "").strip()
        if not database_url.username or not database_password or not database_url.database:
            raise RuntimeError(
                "Production DATABASE_URL must include a database, user and password"
            )
        if is_weak(database_password):
            raise RuntimeError(
                "Production DATABASE_URL uses a missing or example database password"
            )

        secret_key = self.SECRET_KEY.strip()
        if is_weak(secret_key, minimum_length=32):
            raise RuntimeError(
                "Production SECRET_KEY must contain at least 32 non-example characters"
            )

        cors_origins = self.cors_origins()
        if "*" in cors_origins:
            raise RuntimeError("Production CORS_ORIGINS must not contain a wildcard")
        invalid_origins = []
        for origin in cors_origins:
            parsed_cors_origin = urlsplit(origin)
            if (
                parsed_cors_origin.scheme not in {"http", "https"}
                or parsed_cors_origin.hostname is None
                or parsed_cors_origin.path not in {"", "/"}
                or parsed_cors_origin.query
                or parsed_cors_origin.fragment
                or parsed_cors_origin.username is not None
                or parsed_cors_origin.password is not None
            ):
                invalid_origins.append(origin)
                continue
            if (
                web_transport == "https"
                and parsed_cors_origin.scheme != "https"
            ):
                invalid_origins.append(origin)
        if invalid_origins:
            if web_transport == "https":
                raise RuntimeError(
                    "Production CORS_ORIGINS may only contain explicit "
                    "HTTPS origins"
                )
            raise RuntimeError(
                "Production CORS_ORIGINS contains an invalid explicit origin"
            )

        public_hostname = self.PUBLIC_HOSTNAME.strip().lower()
        parsed_origin = urlsplit(self.PUBLIC_ORIGIN.strip())
        try:
            origin_port = parsed_origin.port
        except ValueError as exc:
            raise RuntimeError(
                "Production PUBLIC_ORIGIN contains an invalid port"
            ) from exc
        if (
            parsed_origin.scheme != web_transport
            or parsed_origin.hostname is None
            or not public_hostname
            or parsed_origin.hostname.lower() != public_hostname
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path not in {"", "/"}
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            raise RuntimeError(
                "Production PUBLIC_ORIGIN must be a pure Origin matching "
                "WEB_TRANSPORT and PUBLIC_HOSTNAME"
            )
        if not 0 <= self.HSTS_MAX_AGE <= 31536000:
            raise RuntimeError(
                "Production HSTS_MAX_AGE must be between 0 and 31536000"
            )
        if web_transport == "https":
            if public_hostname != "textile-monitor.internal":
                raise RuntimeError(
                    "Production HTTPS PUBLIC_HOSTNAME must be "
                    "textile-monitor.internal"
                )
            if origin_port not in {None, 443}:
                raise RuntimeError(
                    "Production HTTPS PUBLIC_ORIGIN may only use port 443"
                )
            if not self.TLS_DIR_HOST_PATH.strip():
                raise RuntimeError(
                    "Production HTTPS TLS_DIR_HOST_PATH must be configured"
                )
            if not 1 <= self.TLS_MIN_VALID_DAYS <= 365:
                raise RuntimeError(
                    "Production TLS_MIN_VALID_DAYS must be between 1 and 365"
                )
            if self.EXECUTION_ENABLED and not self.EXECUTION_COOKIE_SECURE:
                raise RuntimeError(
                    "Production HTTPS EXECUTION_COOKIE_SECURE must be enabled"
                )
        else:
            if self.TLS_DIR_HOST_PATH.strip():
                raise RuntimeError(
                    "Production HTTP TLS_DIR_HOST_PATH must be empty"
                )
            if self.HSTS_MAX_AGE != 0:
                raise RuntimeError(
                    "Production HTTP HSTS_MAX_AGE must be 0"
                )
            if self.EXECUTION_ENABLED and self.EXECUTION_COOKIE_SECURE:
                raise RuntimeError(
                    "Production HTTP EXECUTION_COOKIE_SECURE must be disabled"
                )

        management_networks = self.management_cidrs()
        if web_transport == "https" and not management_networks:
            raise RuntimeError("Production MANAGEMENT_CIDRS must not be empty")
        unsafe_networks = [
            str(network)
            for network in management_networks
            if not is_rfc1918_network(network)
        ]
        if unsafe_networks:
            raise RuntimeError(
                "Production MANAGEMENT_CIDRS may only contain RFC1918 IPv4 "
                f"networks: {', '.join(unsafe_networks)}"
            )

        if not self.EXECUTION_ENABLED:
            return

        session_secret = self.EXECUTION_SESSION_SECRET.strip()
        if is_weak(session_secret, minimum_length=32):
            raise RuntimeError(
                "Production EXECUTION_SESSION_SECRET must contain at least 32 characters"
            )
        credential_key = self.EXECUTION_CREDENTIAL_KEY.strip()
        if is_weak(credential_key, minimum_length=32):
            raise RuntimeError(
                "Production EXECUTION_CREDENTIAL_KEY must contain at least 32 characters"
            )
        if len({secret_key, session_secret, credential_key}) != 3:
            raise RuntimeError(
                "Production SECRET_KEY and execution secrets must be independent"
            )
        if self.EXECUTION_INDEX_INTERVAL_SECONDS < 30:
            raise RuntimeError(
                "Production EXECUTION_INDEX_INTERVAL_SECONDS must be at least 30"
            )
        if not 1 <= self.EXECUTION_NODE_MAX_ATTEMPTS <= 100:
            raise RuntimeError(
                "Production EXECUTION_NODE_MAX_ATTEMPTS must be between 1 and 100"
            )
        if self.EXECUTION_WORKER_HEARTBEAT_TIMEOUT_SECONDS < 15:
            raise RuntimeError(
                "Production worker heartbeat timeout must be at least 15 seconds"
            )
        if not 1 <= self.EXECUTION_TASK_SNAPSHOT_TTL_MINUTES <= 1440:
            raise RuntimeError(
                "Production task snapshot TTL must be between 1 and 1440 minutes"
            )
        if not 10 <= self.EXECUTION_TASK_SNAPSHOT_RETRY_SECONDS <= 3600:
            raise RuntimeError(
                "Production task snapshot retry delay must be between 10 and "
                "3600 seconds"
            )
        if not 1 <= self.EXECUTION_EXTERNAL_PREFLIGHT_TTL_MINUTES <= 1440:
            raise RuntimeError(
                "Production external preflight TTL must be between 1 and "
                "1440 minutes"
            )
        if not 1 <= self.EXECUTION_EXTERNAL_APPROVAL_TTL_MINUTES <= 1440:
            raise RuntimeError(
                "Production external approval TTL must be between 1 and "
                "1440 minutes"
            )
        bootstrap_password = self.EXECUTION_BOOTSTRAP_ADMIN_PASSWORD.strip()
        if bootstrap_password and (
            is_weak(bootstrap_password, minimum_length=12)
        ):
            raise RuntimeError(
                "Production bootstrap administrator password is too weak"
            )


settings = Settings()
