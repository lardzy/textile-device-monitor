"""Transport-security validation shared by configuration and HTTP requests."""

from __future__ import annotations

import ssl
from pathlib import Path
from urllib.parse import urlsplit


CONFIG_SCHEMA_VERSION = 2
TRANSPORT_COMPATIBLE = "compatible"
TRANSPORT_REQUIRED = "required"
TRANSPORT_SECURITY_MODES = (TRANSPORT_COMPATIBLE, TRANSPORT_REQUIRED)


class TransportSecurityError(ValueError):
    """A configuration error that can be shown directly to an operator."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.user_message = message


def normalize_server_origin(
    value: object,
    transport_security: str,
) -> str:
    """Validate and normalize an HTTP(S) origin without a path or credentials."""

    raw_value = str(value or "").strip()
    if not raw_value:
        raise TransportSecurityError("server_url_empty", "服务器地址不能为空")
    if any(character.isspace() for character in raw_value):
        raise TransportSecurityError(
            "server_url_whitespace",
            "服务器地址不能包含空格或换行",
        )
    if "\\" in raw_value:
        raise TransportSecurityError(
            "server_url_backslash",
            "服务器地址不能包含反斜杠",
        )
    if transport_security not in TRANSPORT_SECURITY_MODES:
        raise TransportSecurityError(
            "transport_security_invalid",
            "传输安全模式必须为 compatible 或 required",
        )

    try:
        parsed = urlsplit(raw_value)
        port = parsed.port
    except ValueError as exc:
        raise TransportSecurityError(
            "server_url_invalid",
            f"服务器地址格式无效：{exc}",
        ) from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise TransportSecurityError(
            "server_url_scheme",
            "服务器地址必须以 http:// 或 https:// 开头",
        )
    if transport_security == TRANSPORT_REQUIRED and scheme != "https":
        raise TransportSecurityError(
            "https_required",
            "当前配置要求使用 HTTPS，不能保存 HTTP 服务器地址",
        )
    if not parsed.hostname:
        raise TransportSecurityError(
            "server_url_host",
            "服务器地址必须包含有效的主机名",
        )
    if parsed.username is not None or parsed.password is not None:
        raise TransportSecurityError(
            "server_url_credentials",
            "服务器地址不能包含用户名或密码",
        )
    if parsed.path not in {"", "/"}:
        raise TransportSecurityError(
            "server_url_path",
            "服务器地址只能填写站点根地址，不能包含 /api 或其它路径",
        )
    if "?" in raw_value or "#" in raw_value:
        raise TransportSecurityError(
            "server_url_suffix",
            "服务器地址不能包含查询参数或片段",
        )

    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{host}{port_suffix}"


def resolve_ca_bundle(value: object, base_directory: Path) -> Path:
    """Resolve a CA bundle relative to the installed client directory."""

    raw_value = str(value or "").strip()
    if not raw_value:
        raise TransportSecurityError(
            "tls_ca_bundle_empty",
            "HTTPS 配置必须指定内部 CA 证书文件",
        )
    candidate = Path(raw_value).expanduser()
    if not candidate.is_absolute():
        candidate = base_directory / candidate
    return candidate.resolve(strict=False)


def validate_ca_bundle(path: Path) -> Path:
    """Ensure Requests/OpenSSL can load the configured CA bundle."""

    if not path.is_file():
        raise TransportSecurityError(
            "tls_ca_bundle_missing",
            f"内部 CA 证书文件不存在：{path}",
        )
    try:
        ssl.create_default_context(cafile=str(path))
    except (OSError, ssl.SSLError) as exc:
        raise TransportSecurityError(
            "tls_ca_bundle_invalid",
            f"内部 CA 证书文件无效或已损坏：{path}",
        ) from exc
    return path


def diagnose_ssl_error(error: BaseException) -> tuple[str, str]:
    """Translate common Requests/OpenSSL failures into actionable Chinese text."""

    details = str(error).lower()
    if any(
        marker in details
        for marker in (
            "hostname mismatch",
            "doesn't match",
            "does not match",
            "not valid for",
        )
    ):
        return (
            "tls_hostname_mismatch",
            "服务器证书中的主机名与访问地址不一致，请使用 "
            "https://textile-monitor.internal 并检查本机 hosts 配置",
        )
    if "expired" in details:
        return (
            "tls_certificate_expired",
            "服务器 HTTPS 证书已过期，请联系管理员更新证书",
        )
    if "not yet valid" in details:
        return (
            "tls_certificate_not_yet_valid",
            "服务器 HTTPS 证书尚未生效，请检查本机时间或联系管理员",
        )
    if any(
        marker in details
        for marker in (
            "unable to get local issuer",
            "self-signed certificate",
            "unknown ca",
            "certificate verify failed",
        )
    ):
        return (
            "tls_untrusted_ca",
            "无法信任服务器证书，请检查客户端内部 CA 文件及 Windows 根证书",
        )
    return (
        "tls_handshake_failed",
        "HTTPS 安全连接失败，请检查服务器证书、客户端 CA 文件和系统时间",
    )
