#!/usr/bin/env python3
"""Validate a production TLS bundle without modifying the deployment."""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


CERTIFICATE_PATTERN = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s+.*?-----END CERTIFICATE-----\s*",
    re.DOTALL,
)
EXPECTED_FILES = (
    "fullchain.pem",
    "privkey.pem",
    "root-ca.pem",
    "root-ca.cer",
)


class ValidationError(RuntimeError):
    """Raised when a TLS bundle violates a deployment invariant."""


def run(
    arguments: list[str],
    *,
    input_data: bytes | None = None,
    text: bool = True,
) -> str | bytes:
    completed = subprocess.run(
        arguments,
        input=input_data,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        if not detail:
            detail = completed.stdout.decode("utf-8", errors="replace").strip()
        raise ValidationError(
            f"命令执行失败（{completed.returncode}）："
            f"{' '.join(arguments)}\n{detail}"
        )
    if text:
        return completed.stdout.decode("utf-8", errors="replace")
    return completed.stdout


def find_nearest_existing(path: Path) -> Path:
    candidate = path.resolve(strict=False)
    while not candidate.exists():
        if candidate.parent == candidate:
            raise ValidationError(f"无法定位 {path} 的现有父目录")
        candidate = candidate.parent
    return candidate if candidate.is_dir() else candidate.parent


def reject_git_worktree(path: Path) -> None:
    git = shutil.which("git")
    if git is None:
        cursor = find_nearest_existing(path)
        for parent in (cursor, *cursor.parents):
            if (parent / ".git").exists():
                raise ValidationError(
                    f"安全拒绝：TLS/PKI 目录不能位于 Git 工作区：{path}"
                )
        return

    existing = find_nearest_existing(path)
    completed = subprocess.run(
        [git, "-C", str(existing), "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        return
    root = Path(completed.stdout.strip()).resolve()
    target = path.resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError:
        return
    raise ValidationError(f"安全拒绝：TLS/PKI 目录不能位于 Git 工作区：{path}")


def require_files(tls_dir: Path) -> dict[str, Path]:
    paths = {name: tls_dir / name for name in EXPECTED_FILES}
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        raise ValidationError(f"TLS 发布包缺少文件：{', '.join(missing)}")
    for name, path in paths.items():
        if path.is_symlink():
            raise ValidationError(f"TLS 发布文件禁止使用符号链接：{name}")
        if path.stat().st_size == 0:
            raise ValidationError(f"TLS 发布文件为空：{name}")
    return paths


def validate_private_key_permissions(path: Path) -> None:
    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise ValidationError(
                f"私钥权限过宽：{path} 当前为 {oct(mode)}，必须为 0600"
            )
        return

    icacls = shutil.which("icacls.exe") or shutil.which("icacls")
    if icacls is None:
        raise ValidationError("Windows 环境缺少 icacls，无法验证私钥 ACL")
    output = run([icacls, str(path)])
    assert isinstance(output, str)
    compact = output.casefold()
    forbidden_principals = (
        "everyone",
        "authenticated users",
        "builtin\\users",
        "users:",
        "s-1-1-0",
        "s-1-5-11",
        "s-1-5-32-545",
        "所有人",
        "经过身份验证的用户",
    )
    if any(principal in compact for principal in forbidden_principals):
        raise ValidationError(f"私钥 ACL 包含普通用户或 Everyone：\n{output}")


def split_fullchain(fullchain_path: Path) -> tuple[bytes, bytes]:
    certificates = CERTIFICATE_PATTERN.findall(fullchain_path.read_bytes())
    if len(certificates) != 2:
        raise ValidationError(
            "fullchain.pem 必须严格包含服务器证书和一张中间 CA 证书，"
            f"当前包含 {len(certificates)} 张"
        )
    return certificates[0], certificates[1]


def x509_text(openssl: str, certificate: Path) -> str:
    output = run([openssl, "x509", "-in", str(certificate), "-noout", "-text"])
    assert isinstance(output, str)
    return output


def x509_extension(openssl: str, certificate: Path, name: str) -> str:
    output = run(
        [openssl, "x509", "-in", str(certificate), "-noout", "-ext", name]
    )
    assert isinstance(output, str)
    return output


def parse_openssl_time(value: str) -> dt.datetime:
    value = re.sub(r"\s+", " ", value.strip())
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError) as error:
        raise ValidationError(f"无法解析 OpenSSL 时间：{value}") from error
    if parsed is None:
        raise ValidationError(f"无法解析 OpenSSL 时间：{value}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def certificate_dates(openssl: str, certificate: Path) -> tuple[dt.datetime, dt.datetime]:
    output = run(
        [openssl, "x509", "-in", str(certificate), "-noout", "-startdate", "-enddate"]
    )
    assert isinstance(output, str)
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    try:
        return (
            parse_openssl_time(values["notBefore"]),
            parse_openssl_time(values["notAfter"]),
        )
    except (KeyError, ValueError) as error:
        raise ValidationError(f"无法解析证书有效期：{output}") from error


def ensure_currently_valid(
    openssl: str,
    certificate: Path,
    *,
    label: str,
    minimum_days: int = 0,
) -> dt.datetime:
    not_before, not_after = certificate_dates(openssl, certificate)
    now = dt.datetime.now(dt.timezone.utc)
    if now < not_before:
        raise ValidationError(f"{label}尚未生效：{not_before.isoformat()}")
    if now >= not_after:
        raise ValidationError(f"{label}已过期：{not_after.isoformat()}")
    required_until = now + dt.timedelta(days=minimum_days)
    if not_after < required_until:
        remaining = (not_after - now).total_seconds() / 86400
        raise ValidationError(
            f"{label}剩余有效期仅 {remaining:.1f} 天，"
            f"低于要求的 {minimum_days} 天"
        )
    return not_after


def ensure_rsa_3072(text: str, *, label: str) -> None:
    if "rsaEncryption" not in text or not re.search(
        r"Public-Key:\s*\(3072 bit\)", text
    ):
        raise ValidationError(f"{label}必须使用 RSA 3072 位公钥")


def certificate_public_key(openssl: str, certificate: Path) -> bytes:
    pem = run(
        [openssl, "x509", "-in", str(certificate), "-pubkey", "-noout"],
        text=False,
    )
    assert isinstance(pem, bytes)
    der = run(
        [openssl, "pkey", "-pubin", "-outform", "DER"],
        input_data=pem,
        text=False,
    )
    assert isinstance(der, bytes)
    return der


def private_key_public_key(openssl: str, private_key: Path) -> bytes:
    der = run(
        [openssl, "pkey", "-in", str(private_key), "-pubout", "-outform", "DER"],
        text=False,
    )
    assert isinstance(der, bytes)
    return der


def sha256_fingerprint(openssl: str, certificate: Path) -> str:
    output = run(
        [openssl, "x509", "-in", str(certificate), "-noout", "-fingerprint", "-sha256"]
    )
    assert isinstance(output, str)
    _, separator, value = output.strip().partition("=")
    if not separator:
        raise ValidationError(f"无法读取证书指纹：{certificate}")
    return value.replace(":", "").upper()


def ensure_self_issued_root(openssl: str, certificate: Path) -> None:
    output = run(
        [openssl, "x509", "-in", str(certificate), "-noout", "-subject", "-issuer"]
    )
    assert isinstance(output, str)
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    if values.get("subject") != values.get("issuer"):
        raise ValidationError("root-ca.pem 的 Subject 与 Issuer 不一致")


def validate_bundle(
    *,
    tls_dir: Path,
    hostname: str,
    minimum_valid_days: int,
    openssl: str,
) -> dict[str, str | int]:
    if hostname != "textile-monitor.internal":
        raise ValidationError("服务器 DNS 名称必须为 textile-monitor.internal")
    if minimum_valid_days < 1:
        raise ValidationError("最小剩余有效期必须大于 0 天")

    tls_dir = tls_dir.resolve(strict=False)
    reject_git_worktree(tls_dir)
    if not tls_dir.is_dir():
        raise ValidationError(f"TLS 发布包目录不存在：{tls_dir}")
    paths = require_files(tls_dir)
    validate_private_key_permissions(paths["privkey.pem"])

    leaf_pem, intermediate_pem = split_fullchain(paths["fullchain.pem"])
    with tempfile.TemporaryDirectory(prefix="textile-tls-preflight-") as temporary:
        temporary_dir = Path(temporary)
        leaf_path = temporary_dir / "leaf.pem"
        intermediate_path = temporary_dir / "intermediate.pem"
        leaf_path.write_bytes(leaf_pem)
        intermediate_path.write_bytes(intermediate_pem)

        leaf_text = x509_text(openssl, leaf_path)
        intermediate_text = x509_text(openssl, intermediate_path)
        root_text = x509_text(openssl, paths["root-ca.pem"])
        ensure_self_issued_root(openssl, paths["root-ca.pem"])
        ensure_rsa_3072(leaf_text, label="服务器证书")
        ensure_rsa_3072(intermediate_text, label="中间 CA")
        ensure_rsa_3072(root_text, label="根 CA")

        san = x509_extension(openssl, leaf_path, "subjectAltName")
        dns_names = re.findall(r"DNS:([^,\s]+)", san)
        ip_addresses = re.findall(r"IP Address:([^,\s]+)", san)
        if dns_names != [hostname] or ip_addresses:
            raise ValidationError(
                "服务器证书 SAN 必须且只能包含 "
                f"DNS:{hostname}；当前 SAN：{san.strip()}"
            )

        basic_constraints = x509_extension(
            openssl, leaf_path, "basicConstraints"
        )
        if (
            "X509v3 Basic Constraints: critical" not in basic_constraints
            or "CA:FALSE" not in basic_constraints
        ):
            raise ValidationError("服务器证书必须声明 critical CA:FALSE")
        key_usage = x509_extension(openssl, leaf_path, "keyUsage")
        if (
            "X509v3 Key Usage: critical" not in key_usage
            or "Digital Signature" not in key_usage
            or "Key Encipherment" not in key_usage
        ):
            raise ValidationError(
                "服务器证书必须声明 critical Digital Signature/Key Encipherment"
            )
        extended_usage = x509_extension(openssl, leaf_path, "extendedKeyUsage")
        if (
            "X509v3 Extended Key Usage:" not in extended_usage
            or "X509v3 Extended Key Usage: critical" in extended_usage
            or "TLS Web Server Authentication" not in extended_usage
        ):
            raise ValidationError(
                "服务器证书必须声明非 critical TLS Web Server Authentication"
            )

        intermediate_constraints = x509_extension(
            openssl, intermediate_path, "basicConstraints"
        )
        if (
            "X509v3 Basic Constraints: critical" not in intermediate_constraints
            or "CA:TRUE" not in intermediate_constraints
            or "pathlen:0" not in intermediate_constraints
        ):
            raise ValidationError("中间证书必须为 critical CA:TRUE, pathlen:0")
        root_constraints = x509_extension(
            openssl, paths["root-ca.pem"], "basicConstraints"
        )
        if (
            "X509v3 Basic Constraints: critical" not in root_constraints
            or "CA:TRUE" not in root_constraints
        ):
            raise ValidationError("根证书必须为 critical CA:TRUE")

        if certificate_public_key(openssl, leaf_path) != private_key_public_key(
            openssl, paths["privkey.pem"]
        ):
            raise ValidationError("服务器证书与 privkey.pem 不匹配")

        run(
            [
                openssl,
                "verify",
                "-show_chain",
                "-purpose",
                "sslserver",
                "-verify_hostname",
                hostname,
                "-CAfile",
                str(paths["root-ca.pem"]),
                "-untrusted",
                str(intermediate_path),
                str(leaf_path),
            ]
        )
        run(
            [
                openssl,
                "verify",
                "-CAfile",
                str(paths["root-ca.pem"]),
                str(paths["root-ca.pem"]),
            ]
        )

        leaf_expiry = ensure_currently_valid(
            openssl,
            leaf_path,
            label="服务器证书",
            minimum_days=minimum_valid_days,
        )
        ensure_currently_valid(openssl, intermediate_path, label="中间 CA")
        ensure_currently_valid(openssl, paths["root-ca.pem"], label="根 CA")

        root_der = run(
            [
                openssl,
                "x509",
                "-in",
                str(paths["root-ca.pem"]),
                "-outform",
                "DER",
            ],
            text=False,
        )
        assert isinstance(root_der, bytes)
        if root_der != paths["root-ca.cer"].read_bytes():
            raise ValidationError("root-ca.cer 与 root-ca.pem 不是同一张证书")

        return {
            "hostname": hostname,
            "minimum_valid_days": minimum_valid_days,
            "server_not_after": leaf_expiry.isoformat(),
            "server_sha256": sha256_fingerprint(openssl, leaf_path),
            "root_sha256": sha256_fingerprint(openssl, paths["root-ca.pem"]),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tls-dir", required=True, type=Path)
    parser.add_argument(
        "--hostname",
        default="textile-monitor.internal",
    )
    parser.add_argument("--min-valid-days", type=int, default=30)
    parser.add_argument("--openssl", default="openssl")
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    openssl = shutil.which(arguments.openssl)
    if openssl is None:
        print(f"TLS 预检失败：未找到 OpenSSL：{arguments.openssl}", file=sys.stderr)
        return 2
    try:
        result = validate_bundle(
            tls_dir=arguments.tls_dir,
            hostname=arguments.hostname,
            minimum_valid_days=arguments.min_valid_days,
            openssl=openssl,
        )
    except ValidationError as error:
        print(f"TLS 预检失败：{error}", file=sys.stderr)
        return 1

    print(
        "TLS 预检通过："
        f"hostname={result['hostname']}，"
        f"server_not_after={result['server_not_after']}，"
        f"server_sha256={result['server_sha256']}，"
        f"root_sha256={result['root_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
