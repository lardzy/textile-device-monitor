from __future__ import annotations

import argparse
import ipaddress
import json
import os
import posixpath
import socket
import ssl
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import (
    dsa,
    ec,
    ed25519,
    ed448,
    padding,
    rsa,
)
from cryptography.x509.oid import ExtendedKeyUsageOID


EXPECTED_PUBLIC_HOSTNAME = "textile-monitor.internal"
RFC1918_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def _is_rfc1918_network(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> bool:
    return isinstance(network, ipaddress.IPv4Network) and any(
        network.subnet_of(allowed) for allowed in RFC1918_NETWORKS
    )


def _is_rfc1918_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return isinstance(address, ipaddress.IPv4Address) and any(
        address in allowed for allowed in RFC1918_NETWORKS
    )


TLS_FILENAMES = ("fullchain.pem", "privkey.pem", "root-ca.pem")
INSECURE_WGET_FLAG = "no-check-" "certificate"


class DeploymentValidationError(RuntimeError):
    pass


def _fail(message: str) -> None:
    raise DeploymentValidationError(message)


def _integer(
    value: Any,
    *,
    label: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DeploymentValidationError(f"{label} 必须是整数") from exc
    if minimum is not None and parsed < minimum:
        _fail(f"{label} 不得低于 {minimum}")
    if maximum is not None and parsed > maximum:
        _fail(f"{label} 不得高于 {maximum}")
    return parsed


def _certificate_time(
    certificate: x509.Certificate,
    attribute: str,
) -> datetime:
    utc_attribute = f"{attribute}_utc"
    value = getattr(certificate, utc_attribute, None)
    if value is None:
        value = getattr(certificate, attribute)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _verify_signature(
    certificate: x509.Certificate,
    issuer: x509.Certificate,
) -> None:
    public_key = issuer.public_key()
    signature_hash = certificate.signature_hash_algorithm
    try:
        if isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
                padding.PKCS1v15(),
                signature_hash,
            )
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
                ec.ECDSA(signature_hash),
            )
        elif isinstance(public_key, dsa.DSAPublicKey):
            public_key.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
                signature_hash,
            )
        elif isinstance(
            public_key,
            (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey),
        ):
            public_key.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
            )
        else:
            _fail("证书链包含不支持的公钥类型")
    except DeploymentValidationError:
        raise
    except Exception as exc:
        raise DeploymentValidationError("证书链签名校验失败") from exc


def _require_rsa_3072(
    certificate: x509.Certificate,
    label: str,
) -> None:
    public_key = certificate.public_key()
    if not isinstance(public_key, rsa.RSAPublicKey):
        _fail(f"{label} 必须使用 RSA 公钥")
    if public_key.key_size < 3072:
        _fail(f"{label} RSA 密钥长度不得低于 3072 位")


def _validate_certificate_window(
    certificate: x509.Certificate,
    *,
    label: str,
    now: datetime,
    minimum_valid_days: int,
) -> None:
    not_before = _certificate_time(certificate, "not_valid_before")
    not_after = _certificate_time(certificate, "not_valid_after")
    if now < not_before:
        _fail(f"{label} 尚未生效")
    if now >= not_after:
        _fail(f"{label} 已过期")
    remaining_seconds = (not_after - now).total_seconds()
    if remaining_seconds < minimum_valid_days * 86400:
        _fail(f"{label} 剩余有效期不足 {minimum_valid_days} 天")
    hash_algorithm = certificate.signature_hash_algorithm
    if hash_algorithm is not None and hash_algorithm.name.lower() in {
        "md5",
        "sha1",
    }:
        _fail(f"{label} 使用了不安全的签名摘要算法")


def _basic_constraints(
    certificate: x509.Certificate,
    *,
    label: str,
) -> x509.BasicConstraints:
    try:
        extension = certificate.extensions.get_extension_for_class(
            x509.BasicConstraints
        )
    except x509.ExtensionNotFound as exc:
        raise DeploymentValidationError(
            f"{label} 缺少 Basic Constraints"
        ) from exc
    if not extension.critical:
        _fail(f"{label} Basic Constraints 必须标记为 critical")
    return extension.value


def _validate_ca_usage(
    certificate: x509.Certificate,
    *,
    label: str,
) -> x509.BasicConstraints:
    constraints = _basic_constraints(certificate, label=label)
    if not constraints.ca:
        _fail(f"{label} 未声明 CA 用途")
    try:
        key_usage_extension = certificate.extensions.get_extension_for_class(
            x509.KeyUsage
        )
    except x509.ExtensionNotFound as exc:
        raise DeploymentValidationError(
            f"{label} 缺少 Key Usage"
        ) from exc
    if not key_usage_extension.critical:
        _fail(f"{label} Key Usage 必须标记为 critical")
    key_usage = key_usage_extension.value
    if (
        not key_usage.key_cert_sign
        or not key_usage.crl_sign
        or key_usage.digital_signature
        or key_usage.key_encipherment
    ):
        _fail(f"{label} Key Usage 必须仅用于证书和 CRL 签发")
    return constraints


def validate_tls_material(
    tls_dir: Path,
    *,
    expected_hostname: str,
    minimum_valid_days: int,
    now: datetime | None = None,
    repo_root: Path | None = None,
    platform_name: str | None = None,
    icacls_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    tls_dir = tls_dir.expanduser()
    if tls_dir.is_symlink():
        _fail("TLS_DIR_HOST_PATH 不得是符号链接")
    tls_dir = tls_dir.resolve(strict=False)
    if repo_root is not None:
        resolved_repo = repo_root.expanduser().resolve(strict=False)
        if tls_dir == resolved_repo or resolved_repo in tls_dir.parents:
            _fail("TLS_DIR_HOST_PATH 必须位于 Git 工作区之外")
    if not tls_dir.is_dir():
        _fail(f"TLS 目录不存在或不是目录: {tls_dir}")

    paths = {name: tls_dir / name for name in TLS_FILENAMES}
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        _fail(f"TLS 目录缺少文件: {', '.join(missing)}")
    for name, path in paths.items():
        if path.is_symlink():
            _fail(f"TLS 文件不得是符号链接: {name}")

    for path in tls_dir.rglob("*"):
        if path.is_symlink():
            _fail(f"TLS 目录不得包含符号链接: {path.name}")
        if not path.is_file() or path == paths["privkey.pem"]:
            continue
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise DeploymentValidationError(
                f"无法读取 TLS 目录文件: {path.name}"
            ) from exc
        if b"PRIVATE KEY-----" in content:
            _fail(f"服务器 TLS 目录包含额外私钥: {path.name}")

    try:
        fullchain = x509.load_pem_x509_certificates(
            paths["fullchain.pem"].read_bytes()
        )
        root_certificates = x509.load_pem_x509_certificates(
            paths["root-ca.pem"].read_bytes()
        )
        private_key = serialization.load_pem_private_key(
            paths["privkey.pem"].read_bytes(),
            password=None,
        )
    except (OSError, ValueError, TypeError) as exc:
        raise DeploymentValidationError(
            "TLS 证书、证书链或私钥无法解析"
        ) from exc

    if len(root_certificates) != 1:
        _fail("root-ca.pem 必须且只能包含一张根 CA 证书")
    root_certificate = root_certificates[0]
    if len(fullchain) < 2:
        _fail("fullchain.pem 必须包含服务器证书和中间 CA 证书")
    leaf = fullchain[0]
    intermediates = list(fullchain[1:])
    if any(
        certificate.fingerprint(hashes.SHA256())
        == root_certificate.fingerprint(hashes.SHA256())
        for certificate in intermediates
    ):
        _fail("fullchain.pem 不应包含根 CA 证书")

    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    all_certificates = [
        ("服务器证书", leaf),
        *[
            (f"中间 CA 证书 {index}", certificate)
            for index, certificate in enumerate(intermediates, start=1)
        ],
        ("根 CA 证书", root_certificate),
    ]
    for label, certificate in all_certificates:
        _validate_certificate_window(
            certificate,
            label=label,
            now=current_time,
            minimum_valid_days=minimum_valid_days,
        )
        _require_rsa_3072(certificate, label)

    if _basic_constraints(leaf, label="服务器证书").ca:
        _fail("服务器证书不得声明为 CA")
    try:
        san = leaf.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except x509.ExtensionNotFound as exc:
        raise DeploymentValidationError(
            "服务器证书缺少 Subject Alternative Name"
        ) from exc
    dns_names = san.get_values_for_type(x509.DNSName)
    ip_addresses = san.get_values_for_type(x509.IPAddress)
    if dns_names != [expected_hostname] or ip_addresses:
        _fail(
            "服务器证书 SAN 必须且只能包含 "
            f"DNS:{expected_hostname}"
        )
    try:
        extended_usage_extension = leaf.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage
        )
    except x509.ExtensionNotFound as exc:
        raise DeploymentValidationError(
            "服务器证书缺少 Extended Key Usage"
        ) from exc
    if extended_usage_extension.critical:
        _fail("服务器证书 Extended Key Usage 不应标记为 critical")
    extended_usage = extended_usage_extension.value
    if set(extended_usage) != {ExtendedKeyUsageOID.SERVER_AUTH}:
        _fail("服务器证书只能授权用于 TLS Web Server Authentication")
    try:
        key_usage_extension = leaf.extensions.get_extension_for_class(
            x509.KeyUsage
        )
    except x509.ExtensionNotFound as exc:
        raise DeploymentValidationError(
            "服务器证书缺少 Key Usage"
        ) from exc
    if not key_usage_extension.critical:
        _fail("服务器证书 Key Usage 必须标记为 critical")
    key_usage = key_usage_extension.value
    if (
        not key_usage.digital_signature
        or not key_usage.key_encipherment
        or key_usage.key_cert_sign
        or key_usage.crl_sign
    ):
        _fail("服务器证书 Key Usage 不适用于 TLS 服务")

    private_public = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    certificate_public = leaf.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if private_public != certificate_public:
        _fail("服务器证书与 privkey.pem 不匹配")

    root_constraints = _validate_ca_usage(
        root_certificate,
        label="根 CA 证书",
    )
    if root_constraints.path_length != 1:
        _fail("根 CA 证书 Basic Constraints 必须声明 pathlen:1")
    if root_certificate.subject != root_certificate.issuer:
        _fail("root-ca.pem 必须是自签名 CA 证书")
    _verify_signature(root_certificate, root_certificate)

    unused = list(intermediates)
    current = leaf
    visited_fingerprints: set[bytes] = set()
    while current.issuer != root_certificate.subject:
        candidates = [
            certificate
            for certificate in unused
            if certificate.subject == current.issuer
        ]
        if len(candidates) != 1:
            _fail("fullchain.pem 缺少唯一可用的中间 CA 证书")
        issuer = candidates[0]
        fingerprint = issuer.fingerprint(hashes.SHA256())
        if fingerprint in visited_fingerprints:
            _fail("证书链包含循环")
        visited_fingerprints.add(fingerprint)
        intermediate_constraints = _validate_ca_usage(
            issuer,
            label="中间 CA 证书",
        )
        if intermediate_constraints.path_length != 0:
            _fail("中间 CA 证书 Basic Constraints 必须声明 pathlen:0")
        _verify_signature(current, issuer)
        unused.remove(issuer)
        current = issuer
    _verify_signature(current, root_certificate)
    if unused:
        _fail("fullchain.pem 包含未参与服务器证书链的额外证书")

    validate_private_key_permissions(
        paths["privkey.pem"],
        platform_name=platform_name,
        icacls_runner=icacls_runner,
    )


def validate_private_key_permissions(
    private_key_path: Path,
    *,
    platform_name: str | None = None,
    icacls_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    platform_name = platform_name or os.name
    if platform_name != "nt":
        permissions = stat.S_IMODE(private_key_path.stat().st_mode)
        if permissions & 0o077:
            _fail(
                "privkey.pem 权限过宽；POSIX 环境必须移除组和其他用户权限"
            )
        return

    runner = icacls_runner or subprocess.run
    try:
        result = runner(
            ["icacls", str(private_key_path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise DeploymentValidationError(
            "无法调用 icacls 检查 privkey.pem ACL"
        ) from exc
    if result.returncode != 0:
        _fail("icacls 无法读取 privkey.pem ACL")
    unsafe_markers = {
        "everyone:(",
        "authenticated users:(",
        "builtin\\users:(",
        "*s-1-1-0:(",
        "*s-1-5-11:(",
        "*s-1-5-32-545:(",
    }
    for line in result.stdout.splitlines():
        normalized = line.strip().lower()
        if any(marker in normalized for marker in unsafe_markers):
            _fail(f"privkey.pem ACL 向宽泛主体授权: {normalized}")


def validate_management_cidrs(
    raw_value: str,
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw_network in raw_value.split(","):
        value = raw_network.strip()
        if not value:
            continue
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise DeploymentValidationError(
                f"MANAGEMENT_CIDRS 包含非法网段: {value}"
            ) from exc
        if not _is_rfc1918_network(network):
            _fail(f"MANAGEMENT_CIDRS 只允许 RFC1918 IPv4 网段: {network}")
        networks.append(network)
    if not networks:
        _fail("MANAGEMENT_CIDRS 不得为空")
    return networks


def _reject_overlaps(
    paths: dict[str, Path | PurePosixPath],
    *,
    location: str,
) -> None:
    items = list(paths.items())
    for index, (left_name, left_path) in enumerate(items):
        for right_name, right_path in items[index + 1 :]:
            if (
                left_path == right_path
                or left_path in right_path.parents
                or right_path in left_path.parents
            ):
                _fail(
                    f"{location} 执行目录禁止相同或父子嵌套: "
                    f"{left_name}={left_path}, {right_name}={right_path}"
                )


def _container_roots(
    services: dict[str, Any],
    service_name: str,
) -> dict[str, PurePosixPath]:
    environment_names = {
        "source": "EXECUTION_SOURCE_ROOT",
        "runtime": "EXECUTION_RUNTIME_ROOT",
        "publish": "EXECUTION_PUBLISH_ROOT",
    }
    environment = services[service_name]["environment"]
    roots: dict[str, PurePosixPath] = {}
    for role, variable_name in environment_names.items():
        raw_value = str(environment.get(variable_name, "")).strip()
        normalized = PurePosixPath(posixpath.normpath(raw_value))
        if not raw_value or not normalized.is_absolute():
            _fail(f"{service_name} {variable_name} 必须是容器内绝对路径")
        roots[role] = normalized
    _reject_overlaps(roots, location=f"{service_name} 容器内")
    return roots


def _bind_roots(
    services: dict[str, Any],
    service_name: str,
    roots: dict[str, PurePosixPath],
) -> dict[str, tuple[str, str]]:
    mounts = services[service_name].get("volumes", [])
    mount_identities: dict[str, tuple[str, str]] = {}
    bind_roots: dict[str, Path] = {}
    for role, target in roots.items():
        candidates: list[tuple[int, dict[str, Any], PurePosixPath]] = []
        for mount in mounts:
            raw_mount_target = str(mount.get("target", "")).strip()
            if not raw_mount_target:
                continue
            mount_target = PurePosixPath(
                posixpath.normpath(raw_mount_target)
            )
            if mount_target == target or mount_target in target.parents:
                candidates.append(
                    (len(mount_target.parts), mount, mount_target)
                )
        if not candidates:
            _fail(f"{service_name} {target} 缺少执行目录挂载")
        _, mount, mount_target = max(candidates, key=lambda item: item[0])
        relative_target = target.relative_to(mount_target)
        mount_type = str(mount.get("type", ""))
        read_only = bool(mount.get("read_only", False))
        if role == "source" and not read_only:
            _fail(f"{service_name} 源资料挂载必须只读")
        if role != "source" and read_only:
            _fail(f"{service_name} {role} 挂载必须可写")
        if mount_type == "volume":
            if role != "source":
                _fail(
                    f"{service_name} {role} 必须使用宿主机 bind mount"
                )
            volume_name = str(mount.get("source", "")).strip()
            if not volume_name:
                _fail(f"{service_name} 源资料命名卷缺少 source")
            volume_location = (
                volume_name
                if relative_target == PurePosixPath(".")
                else f"{volume_name}/{relative_target.as_posix()}"
            )
            mount_identities[role] = ("volume", volume_location)
            continue
        if mount_type != "bind":
            _fail(
                f"{service_name} {target} 必须使用 bind mount，"
                "源资料也可使用只读命名卷"
            )
        host_path = (
            Path(mount["source"]).expanduser()
            / Path(*relative_target.parts)
        ).resolve(strict=False)
        bind_roots[role] = host_path
        mount_identities[role] = ("bind", str(host_path))
    _reject_overlaps(bind_roots, location=f"{service_name} 宿主机")
    return mount_identities


def validate_compose_config(
    config: dict[str, Any],
    *,
    repo_root: Path,
) -> tuple[Path | None, str, int]:
    services = config.get("services") or {}
    required_services = {"postgres", "backend", "execution-worker", "frontend"}
    missing_services = sorted(required_services - set(services))
    if missing_services:
        _fail(f"Compose 缺少必要服务: {', '.join(missing_services)}")

    published = {
        name: service.get("ports", [])
        for name, service in services.items()
        if service.get("ports")
    }
    if set(published) != {"frontend"}:
        _fail(f"仅允许 frontend 发布端口，当前为: {sorted(published)}")
    frontend_ports = published["frontend"]
    if len(frontend_ports) != 1:
        _fail("frontend 必须且只能发布一个端口")

    backend_environment = services["backend"].get("environment") or {}
    worker_environment = services["execution-worker"].get("environment") or {}
    frontend_environment = services["frontend"].get("environment") or {}
    transports = {
        service_name: str(environment.get("WEB_TRANSPORT", ""))
        .strip()
        .lower()
        for service_name, environment in (
            ("backend", backend_environment),
            ("execution-worker", worker_environment),
            ("frontend", frontend_environment),
        )
    }
    invalid_transports = {
        name: value
        for name, value in transports.items()
        if value not in {"http", "https"}
    }
    if invalid_transports:
        _fail(
            "WEB_TRANSPORT 只允许 http 或 https: "
            f"{invalid_transports}"
        )
    if len(set(transports.values())) != 1:
        _fail("backend、execution-worker 与 frontend 的 WEB_TRANSPORT 必须一致")
    web_transport = transports["backend"]

    frontend_port = frontend_ports[0]
    published_port = _integer(
        frontend_port.get("published", 0),
        label="frontend published port",
        minimum=1,
        maximum=65535,
    )
    if (
        _integer(frontend_port.get("target", 0), label="frontend target port")
        != 8080
    ):
        _fail("frontend 必须将宿主机端口发布到容器 8080")
    if web_transport == "https" and published_port != 443:
        _fail("HTTPS frontend 必须发布宿主机 443")
    bind_value = str(frontend_port.get("host_ip", "")).strip()
    try:
        bind_address = ipaddress.ip_address(bind_value)
    except ValueError as exc:
        raise DeploymentValidationError(
            "frontend 必须绑定服务器当前的局域网 IPv4，不能省略 host_ip"
        ) from exc
    if web_transport == "https" and not _is_rfc1918_address(bind_address):
        _fail(
            "HTTPS frontend host_ip 必须是服务器当前的 RFC1918 局域网 IPv4，"
            "不得使用 0.0.0.0、回环、链路本地或组播地址"
        )
    if (
        web_transport == "http"
        and bind_address not in {
            ipaddress.ip_address("0.0.0.0"),
            ipaddress.ip_address("127.0.0.1"),
        }
        and not _is_rfc1918_address(bind_address)
    ):
        _fail(
            "HTTP frontend host_ip 只允许 0.0.0.0、127.0.0.1 "
            "或 RFC1918 局域网 IPv4"
        )

    expected_secure_cookie = web_transport == "https"
    for service_name, environment in (
        ("backend", backend_environment),
        ("execution-worker", worker_environment),
    ):
        if str(environment.get("APP_ENV", "")).lower() != "production":
            _fail(f"{service_name} APP_ENV 必须为 production")
        secure_cookie_value = str(
            environment.get("EXECUTION_COOKIE_SECURE", "")
        ).strip().lower()
        if secure_cookie_value not in {"true", "false"}:
            _fail(
                f"{service_name} EXECUTION_COOKIE_SECURE 必须为 true 或 false"
            )
        secure_cookie = secure_cookie_value == "true"
        if secure_cookie != expected_secure_cookie:
            expected_value = "true" if expected_secure_cookie else "false"
            _fail(
                f"{service_name} EXECUTION_COOKIE_SECURE 必须为 "
                f"{expected_value}"
            )
        if _integer(
            environment.get("EXECUTION_INDEX_INTERVAL_SECONDS", 0),
            label=f"{service_name} EXECUTION_INDEX_INTERVAL_SECONDS",
        ) < 30:
            _fail(f"{service_name} 索引周期过短")

    public_hostname = str(
        backend_environment.get("PUBLIC_HOSTNAME", "")
    ).strip().lower()
    public_origin = str(backend_environment.get("PUBLIC_ORIGIN", "")).strip()
    parsed_origin = urlsplit(public_origin)
    try:
        origin_port = parsed_origin.port
    except ValueError as exc:
        raise DeploymentValidationError(
            "PUBLIC_ORIGIN 包含非法端口"
        ) from exc
    if (
        parsed_origin.scheme != web_transport
        or parsed_origin.hostname is None
        or not public_hostname
        or parsed_origin.hostname.lower() != public_hostname
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
        or parsed_origin.username is not None
        or parsed_origin.password is not None
    ):
        _fail("PUBLIC_ORIGIN 必须匹配 WEB_TRANSPORT 和 PUBLIC_HOSTNAME")
    if web_transport == "https":
        if public_hostname != EXPECTED_PUBLIC_HOSTNAME:
            _fail(f"HTTPS PUBLIC_HOSTNAME 必须为 {EXPECTED_PUBLIC_HOSTNAME}")
        if origin_port not in {None, 443}:
            _fail("HTTPS PUBLIC_ORIGIN 只允许使用 443 端口")
    elif (origin_port or 80) != published_port:
        _fail("HTTP PUBLIC_ORIGIN 端口必须与 frontend 发布端口一致")
    if frontend_environment.get("PUBLIC_HOSTNAME") != public_hostname:
        _fail("backend 与 frontend 的 PUBLIC_HOSTNAME 必须一致")
    hsts_max_age = _integer(
        frontend_environment.get("HSTS_MAX_AGE", -1),
        label="HSTS_MAX_AGE",
        minimum=0,
        maximum=31536000,
    )
    for environment in (backend_environment, worker_environment):
        if _integer(
            environment.get("HSTS_MAX_AGE", -1),
            label="HSTS_MAX_AGE",
        ) != hsts_max_age:
            _fail("backend、worker 与 frontend 的 HSTS_MAX_AGE 必须一致")
    if web_transport == "http" and hsts_max_age != 0:
        _fail("HTTP 模式的 HSTS_MAX_AGE 必须为 0")

    management_value = str(
        frontend_environment.get("MANAGEMENT_CIDRS", "")
    )
    if management_value.strip():
        validate_management_cidrs(management_value)
    elif web_transport == "https":
        _fail("HTTPS 模式的 MANAGEMENT_CIDRS 不得为空")
    for environment in (backend_environment, worker_environment):
        if str(environment.get("MANAGEMENT_CIDRS", "")) != management_value:
            _fail("所有服务的 MANAGEMENT_CIDRS 必须一致")

    frontend_mounts = [
        mount
        for mount in services["frontend"].get("volumes", [])
        if str(mount.get("target", "")).rstrip("/") == "/etc/nginx/tls"
        or str(mount.get("target", "")).startswith("/etc/nginx/tls/")
    ]
    tls_dir: Path | None = None
    minimum_valid_days = 0
    configured_tls_value = str(
        backend_environment.get("TLS_DIR_HOST_PATH", "")
    ).strip()
    if web_transport == "https":
        if not configured_tls_value:
            _fail("HTTPS 模式的 TLS_DIR_HOST_PATH 不得为空")
        if (
            len(frontend_mounts) != 1
            or frontend_mounts[0].get("type") != "bind"
            or not frontend_mounts[0].get("read_only", False)
        ):
            _fail("frontend 必须将唯一 TLS 目录只读挂载到 /etc/nginx/tls")
        tls_dir = Path(frontend_mounts[0]["source"]).expanduser().resolve(
            strict=False
        )
        configured_tls_dir = Path(configured_tls_value).expanduser().resolve(
            strict=False
        )
        if tls_dir != configured_tls_dir:
            _fail("TLS_DIR_HOST_PATH 与 frontend TLS 挂载源不一致")
        minimum_valid_days = _integer(
            backend_environment.get("TLS_MIN_VALID_DAYS", 0),
            label="TLS_MIN_VALID_DAYS",
            minimum=1,
            maximum=365,
        )
    else:
        if frontend_mounts:
            _fail("HTTP 模式不得挂载 TLS 目录")
        if configured_tls_value:
            _fail("HTTP 模式的 TLS_DIR_HOST_PATH 必须为空")

    networks = config.get("networks") or {}
    textile_network = networks.get("textile-net") or {}
    ipam_configs = (textile_network.get("ipam") or {}).get("config") or []
    if len(ipam_configs) != 1 or not ipam_configs[0].get("subnet"):
        _fail("textile-net 必须配置唯一固定子网")
    try:
        docker_subnet = ipaddress.ip_network(
            ipam_configs[0]["subnet"],
            strict=True,
        )
    except ValueError as exc:
        raise DeploymentValidationError("DOCKER_SUBNET 非法") from exc
    if not _is_rfc1918_network(docker_subnet):
        _fail("DOCKER_SUBNET 必须是 RFC1918 IPv4 网段")
    frontend_networks = services["frontend"].get("networks") or {}
    frontend_network = frontend_networks.get("textile-net") or {}
    try:
        frontend_proxy_ip = ipaddress.ip_address(
            frontend_network["ipv4_address"]
        )
    except (KeyError, ValueError) as exc:
        raise DeploymentValidationError(
            "frontend 必须配置固定 FRONTEND_PROXY_IP"
        ) from exc
    if frontend_proxy_ip not in docker_subnet:
        _fail("FRONTEND_PROXY_IP 不在 DOCKER_SUBNET 内")
    if frontend_proxy_ip in {
        docker_subnet.network_address,
        docker_subnet.broadcast_address,
    }:
        _fail("FRONTEND_PROXY_IP 不得使用子网网络地址或广播地址")
    forwarded_allow_ips = str(
        backend_environment.get("FORWARDED_ALLOW_IPS", "")
    ).strip()
    if (
        forwarded_allow_ips == "*"
        or forwarded_allow_ips != str(frontend_proxy_ip)
    ):
        _fail("Uvicorn 只能信任固定 FRONTEND_PROXY_IP 的转发头")
    healthcheck = services["frontend"].get("healthcheck") or {}
    health_command = " ".join(
        str(value) for value in healthcheck.get("test", [])
    )
    if (
        "http://127.0.0.1:8081/healthz" not in health_command
        or INSECURE_WGET_FLAG in health_command
        or " -k" in health_command
    ):
        _fail("frontend 容器健康检查必须使用仅回环 HTTP healthz")

    backend_container_roots = _container_roots(services, "backend")
    worker_container_roots = _container_roots(services, "execution-worker")
    if backend_container_roots != worker_container_roots:
        _fail("backend 与 execution-worker 的容器执行目录必须一致")
    backend_host_roots = _bind_roots(
        services,
        "backend",
        backend_container_roots,
    )
    worker_host_roots = _bind_roots(
        services,
        "execution-worker",
        worker_container_roots,
    )
    if backend_host_roots != worker_host_roots:
        _fail("backend 与 execution-worker 必须共享同一组宿主执行目录")

    return tls_dir, public_hostname, minimum_valid_days


def render_compose_config(
    *,
    repo_root: Path,
    env_file: Path,
    compose_files: Iterable[Path],
) -> dict[str, Any]:
    command = ["docker", "compose", "--env-file", str(env_file)]
    for compose_file in compose_files:
        command.extend(["-f", str(compose_file)])
    command.extend(["config", "--format", "json"])
    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise DeploymentValidationError(
            "无法执行 docker compose；请确认 Docker 已安装并启动"
        ) from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        _fail(f"docker compose config 失败: {details}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DeploymentValidationError(
            "docker compose 未返回有效 JSON"
        ) from exc


def probe_https_endpoint(
    *,
    address: str,
    port: int,
    hostname: str,
    root_ca_path: Path,
    expected_hsts_max_age: int,
    timeout_seconds: float = 10.0,
) -> None:
    try:
        ipaddress.ip_address(address)
    except ValueError as exc:
        raise DeploymentValidationError(
            "--probe-address 必须是服务器的局域网 IP，不执行 DNS 查询"
        ) from exc
    context = ssl.create_default_context(cafile=str(root_ca_path))
    request = (
        f"GET /health/live HTTP/1.1\r\n"
        f"Host: {hostname}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    response = bytearray()
    try:
        with socket.create_connection(
            (address, port),
            timeout=timeout_seconds,
        ) as plain_socket:
            with context.wrap_socket(
                plain_socket,
                server_hostname=hostname,
            ) as tls_socket:
                tls_socket.sendall(request)
                while b"\r\n\r\n" not in response:
                    chunk = tls_socket.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
                    if len(response) > 65536:
                        _fail("HTTPS 探测响应头过大")
    except (OSError, ssl.SSLError) as exc:
        raise DeploymentValidationError(
            "真实 HTTPS 探测失败；证书链、SAN、SNI 或服务连接无效"
        ) from exc
    header_block = bytes(response).split(b"\r\n\r\n", 1)[0]
    header_lines = header_block.decode("iso-8859-1").split("\r\n")
    if not header_lines or " 200 " not in header_lines[0]:
        _fail("HTTPS /health/live 未返回 200")
    headers: dict[str, str] = {}
    for line in header_lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    expected_hsts = f"max-age={expected_hsts_max_age}"
    if headers.get("strict-transport-security") != expected_hsts:
        _fail(
            "HTTPS 响应的 Strict-Transport-Security 与 HSTS_MAX_AGE 不一致"
        )


def probe_http_endpoint(
    *,
    address: str,
    port: int,
    hostname: str,
    timeout_seconds: float = 10.0,
) -> None:
    try:
        ipaddress.ip_address(address)
    except ValueError as exc:
        raise DeploymentValidationError(
            "--probe-address 必须是服务器的 IP，不执行 DNS 查询"
        ) from exc
    request = (
        "GET /health/live HTTP/1.1\r\n"
        f"Host: {hostname}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    response = bytearray()
    try:
        with socket.create_connection(
            (address, port),
            timeout=timeout_seconds,
        ) as plain_socket:
            plain_socket.sendall(request)
            while b"\r\n\r\n" not in response:
                chunk = plain_socket.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > 65536:
                    _fail("HTTP 探测响应头过大")
    except OSError as exc:
        raise DeploymentValidationError(
            "真实 HTTP 探测失败；服务连接无效"
        ) from exc
    header_block = bytes(response).split(b"\r\n\r\n", 1)[0]
    header_lines = header_block.decode("iso-8859-1").split("\r\n")
    if not header_lines or " 200 " not in header_lines[0]:
        _fail("HTTP /health/live 未返回 200")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="验证生产 Compose；HTTPS 模式同时验证内部 TLS。",
    )
    parser.add_argument(
        "--env-file",
        default=os.getenv("COMPOSE_ENV_FILE", ".env"),
        help="生产环境变量文件，默认 .env",
    )
    parser.add_argument(
        "-f",
        "--compose-file",
        action="append",
        dest="compose_files",
        help="Compose 文件；可重复，默认 docker-compose.yml",
    )
    parser.add_argument(
        "--probe-address",
        help=(
            "部署后使用该服务器 IP 探测当前 WEB_TRANSPORT；"
            "HTTPS 模式会使用根 CA 和固定 SNI；不填写时仅执行部署前检查"
        ),
    )
    parser.add_argument(
        "--probe-port",
        type=int,
        help="部署后探测端口；默认按 HTTP/HTTPS 选择 80/443",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    env_file = Path(args.env_file).expanduser()
    if not env_file.is_absolute():
        env_file = repo_root / env_file
    if not env_file.is_file():
        print(
            f"部署配置预检失败：缺少环境文件 {env_file}",
            file=sys.stderr,
        )
        return 1
    compose_values = args.compose_files or ["docker-compose.yml"]
    compose_files = []
    for raw_value in compose_values:
        path = Path(raw_value).expanduser()
        compose_files.append(path if path.is_absolute() else repo_root / path)

    try:
        config = render_compose_config(
            repo_root=repo_root,
            env_file=env_file,
            compose_files=compose_files,
        )
        tls_dir, hostname, minimum_valid_days = validate_compose_config(
            config,
            repo_root=repo_root,
        )
        web_transport = str(
            config["services"]["backend"]["environment"]["WEB_TRANSPORT"]
        ).strip().lower()
        if tls_dir is not None:
            validate_tls_material(
                tls_dir,
                expected_hostname=hostname,
                minimum_valid_days=minimum_valid_days,
                repo_root=repo_root,
            )
        if args.probe_address:
            if web_transport == "https":
                assert tls_dir is not None
                frontend_environment = config["services"]["frontend"][
                    "environment"
                ]
                probe_https_endpoint(
                    address=args.probe_address,
                    port=args.probe_port or 443,
                    hostname=hostname,
                    root_ca_path=tls_dir / "root-ca.pem",
                    expected_hsts_max_age=int(
                        frontend_environment["HSTS_MAX_AGE"]
                    ),
                )
            else:
                probe_http_endpoint(
                    address=args.probe_address,
                    port=args.probe_port or 80,
                    hostname=hostname,
                )
    except DeploymentValidationError as exc:
        print(f"部署配置预检失败：{exc}", file=sys.stderr)
        return 1

    if web_transport == "https":
        print(
            "部署配置预检通过：HTTPS 443、内部主机名、管理网段、"
            "固定代理、执行目录、证书链和私钥权限均有效。"
        )
    else:
        print(
            "部署配置预检通过：HTTP 入口、固定代理和执行目录均有效；"
            "TLS 未启用。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
