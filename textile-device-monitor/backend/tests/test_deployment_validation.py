from __future__ import annotations

import copy
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.deployment_validation import (
    DeploymentValidationError,
    probe_https_endpoint,
    validate_compose_config,
    validate_private_key_permissions,
    validate_tls_material,
)


NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)
HOSTNAME = "textile-monitor.internal"


def _private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(
    *,
    subject: x509.Name,
    issuer: x509.Name,
    public_key,
    issuer_key,
    ca: bool,
    not_before: datetime,
    not_after: datetime,
    hostname: str | None = None,
    server_auth: bool = False,
    key_encipherment: bool | None = None,
    eku_critical: bool = False,
    path_length: int | None = None,
) -> x509.Certificate:
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.BasicConstraints(ca=ca, path_length=path_length),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=not ca,
                content_commitment=False,
                key_encipherment=(
                    not ca if key_encipherment is None else key_encipherment
                ),
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
    )
    if hostname is not None:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]),
            critical=False,
        )
    if server_auth:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=eku_critical,
        )
    return builder.sign(private_key=issuer_key, algorithm=hashes.SHA256())


@pytest.fixture(scope="module")
def certificate_material():
    root_key = _private_key()
    intermediate_key = _private_key()
    leaf_key = _private_key()
    unrelated_key = _private_key()
    root_name = _name("Textile Inspection Root CA")
    intermediate_name = _name("Textile Inspection Issuing CA")
    leaf_name = _name(HOSTNAME)
    root = _certificate(
        subject=root_name,
        issuer=root_name,
        public_key=root_key.public_key(),
        issuer_key=root_key,
        ca=True,
        path_length=1,
        not_before=NOW - timedelta(days=1),
        not_after=NOW + timedelta(days=3650),
    )
    intermediate = _certificate(
        subject=intermediate_name,
        issuer=root_name,
        public_key=intermediate_key.public_key(),
        issuer_key=root_key,
        ca=True,
        path_length=0,
        not_before=NOW - timedelta(days=1),
        not_after=NOW + timedelta(days=1825),
    )

    def leaf(
        *,
        hostname: str = HOSTNAME,
        not_before: datetime = NOW - timedelta(days=1),
        not_after: datetime = NOW + timedelta(days=365),
        server_auth: bool = True,
        key_encipherment: bool = True,
        eku_critical: bool = False,
    ) -> x509.Certificate:
        return _certificate(
            subject=leaf_name,
            issuer=intermediate_name,
            public_key=leaf_key.public_key(),
            issuer_key=intermediate_key,
            ca=False,
            not_before=not_before,
            not_after=not_after,
            hostname=hostname,
            server_auth=server_auth,
            key_encipherment=key_encipherment,
            eku_critical=eku_critical,
        )

    return {
        "root": root,
        "intermediate": intermediate,
        "leaf": leaf,
        "leaf_key": leaf_key,
        "unrelated_key": unrelated_key,
    }


def _pem_certificate(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


def _pem_key(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _write_tls_material(
    target: Path,
    material,
    *,
    leaf: x509.Certificate | None = None,
    include_intermediate: bool = True,
    private_key=None,
    root: x509.Certificate | None = None,
) -> None:
    target.mkdir()
    chain = _pem_certificate(leaf or material["leaf"]())
    if include_intermediate:
        chain += _pem_certificate(material["intermediate"])
    (target / "fullchain.pem").write_bytes(chain)
    (target / "root-ca.pem").write_bytes(
        _pem_certificate(root or material["root"])
    )
    key_path = target / "privkey.pem"
    key_path.write_bytes(_pem_key(private_key or material["leaf_key"]))
    key_path.chmod(0o600)


def test_valid_tls_material_passes(tmp_path, certificate_material) -> None:
    tls_dir = tmp_path / "tls"
    _write_tls_material(tls_dir, certificate_material)

    validate_tls_material(
        tls_dir,
        expected_hostname=HOSTNAME,
        minimum_valid_days=30,
        now=NOW,
    )


@pytest.mark.parametrize(
    ("variant", "message"),
    [
        ("wrong_san", "SAN"),
        ("missing_intermediate", "中间 CA"),
        ("mismatched_key", "不匹配"),
        ("expiring", "不足 30 天"),
        ("expired", "已过期"),
        ("not_yet_valid", "尚未生效"),
        ("wrong_purpose", "Extended Key Usage"),
        ("missing_key_encipherment", "Key Usage"),
        ("critical_eku", "不应标记为 critical"),
    ],
)
def test_tls_material_rejects_certificate_matrix(
    tmp_path,
    certificate_material,
    variant: str,
    message: str,
) -> None:
    tls_dir = tmp_path / variant
    leaf = certificate_material["leaf"]()
    include_intermediate = True
    private_key = certificate_material["leaf_key"]
    if variant == "wrong_san":
        leaf = certificate_material["leaf"](hostname="other.internal")
    elif variant == "missing_intermediate":
        include_intermediate = False
    elif variant == "mismatched_key":
        private_key = certificate_material["unrelated_key"]
    elif variant == "expiring":
        leaf = certificate_material["leaf"](
            not_after=NOW + timedelta(days=5)
        )
    elif variant == "expired":
        leaf = certificate_material["leaf"](
            not_before=NOW - timedelta(days=10),
            not_after=NOW - timedelta(seconds=1),
        )
    elif variant == "not_yet_valid":
        leaf = certificate_material["leaf"](
            not_before=NOW + timedelta(days=1),
            not_after=NOW + timedelta(days=366),
        )
    elif variant == "wrong_purpose":
        leaf = certificate_material["leaf"](server_auth=False)
    elif variant == "missing_key_encipherment":
        leaf = certificate_material["leaf"](key_encipherment=False)
    elif variant == "critical_eku":
        leaf = certificate_material["leaf"](eku_critical=True)
    _write_tls_material(
        tls_dir,
        certificate_material,
        leaf=leaf,
        include_intermediate=include_intermediate,
        private_key=private_key,
    )

    with pytest.raises(DeploymentValidationError, match=message):
        validate_tls_material(
            tls_dir,
            expected_hostname=HOSTNAME,
            minimum_valid_days=30,
            now=NOW,
        )


def test_tls_material_rejects_unknown_root(
    tmp_path,
    certificate_material,
) -> None:
    tls_dir = tmp_path / "unknown-root"
    unrelated_key = certificate_material["unrelated_key"]
    unrelated_name = _name("Unknown Root")
    unrelated_root = _certificate(
        subject=unrelated_name,
        issuer=unrelated_name,
        public_key=unrelated_key.public_key(),
        issuer_key=unrelated_key,
        ca=True,
        path_length=1,
        not_before=NOW - timedelta(days=1),
        not_after=NOW + timedelta(days=3650),
    )
    _write_tls_material(
        tls_dir,
        certificate_material,
        root=unrelated_root,
    )

    with pytest.raises(DeploymentValidationError, match="中间 CA|签名"):
        validate_tls_material(
            tls_dir,
            expected_hostname=HOSTNAME,
            minimum_valid_days=30,
            now=NOW,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission test")
def test_tls_material_rejects_broad_private_key_permissions(
    tmp_path,
    certificate_material,
) -> None:
    tls_dir = tmp_path / "permissions"
    _write_tls_material(tls_dir, certificate_material)
    (tls_dir / "privkey.pem").chmod(0o644)

    with pytest.raises(DeploymentValidationError, match="权限过宽"):
        validate_tls_material(
            tls_dir,
            expected_hostname=HOSTNAME,
            minimum_valid_days=30,
            now=NOW,
        )


def test_windows_acl_rejects_dangerous_ace_on_first_line(tmp_path) -> None:
    private_key = tmp_path / "privkey.pem"
    private_key.write_text("test")

    def fake_icacls(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=(
                f"{private_key} BUILTIN\\Users:(I)(RX)\n"
                "Successfully processed 1 files; Failed processing 0 files\n"
            ),
            stderr="",
        )

    with pytest.raises(DeploymentValidationError, match="宽泛主体"):
        validate_private_key_permissions(
            private_key,
            platform_name="nt",
            icacls_runner=fake_icacls,
        )


def _compose_config(tmp_path: Path) -> dict:
    source = tmp_path / "source"
    runtime = tmp_path / "runtime"
    publish = tmp_path / "publish"
    tls = tmp_path / "tls"
    common_environment = {
        "APP_ENV": "production",
        "EXECUTION_COOKIE_SECURE": "true",
        "EXECUTION_INDEX_INTERVAL_SECONDS": "300",
        "PUBLIC_HOSTNAME": HOSTNAME,
        "PUBLIC_ORIGIN": f"https://{HOSTNAME}",
        "TLS_DIR_HOST_PATH": str(tls),
        "TLS_MIN_VALID_DAYS": "30",
        "HSTS_MAX_AGE": "300",
        "MANAGEMENT_CIDRS": "192.168.106.0/24,10.8.0.0/24",
        "EXECUTION_SOURCE_ROOT": "/data/execution-input",
        "EXECUTION_RUNTIME_ROOT": "/data/execution-runtime",
        "EXECUTION_PUBLISH_ROOT": "/data/execution-publish",
    }
    execution_volumes = [
        {
            "type": "bind",
            "source": str(source),
            "target": "/data/execution-input",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": str(runtime),
            "target": "/data/execution-runtime",
        },
        {
            "type": "bind",
            "source": str(publish),
            "target": "/data/execution-publish",
        },
    ]
    return {
        "services": {
            "postgres": {},
            "backend": {
                "environment": {
                    **common_environment,
                    "FORWARDED_ALLOW_IPS": "172.30.0.10",
                },
                "volumes": copy.deepcopy(execution_volumes),
            },
            "execution-worker": {
                "environment": dict(common_environment),
                "volumes": copy.deepcopy(execution_volumes),
            },
            "frontend": {
                "environment": {
                    "PUBLIC_HOSTNAME": HOSTNAME,
                    "HSTS_MAX_AGE": "300",
                    "MANAGEMENT_CIDRS": "192.168.106.0/24,10.8.0.0/24",
                },
                "ports": [
                    {
                        "target": 443,
                        "published": "443",
                        "host_ip": "192.168.106.50",
                    }
                ],
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(tls),
                        "target": "/etc/nginx/tls",
                        "read_only": True,
                    }
                ],
                "networks": {
                    "textile-net": {"ipv4_address": "172.30.0.10"}
                },
                "healthcheck": {
                    "test": [
                        "CMD-SHELL",
                        (
                            "wget --quiet --spider "
                            "http://127.0.0.1:8080/healthz"
                        ),
                    ]
                },
            },
        },
        "networks": {
            "textile-net": {
                "ipam": {"config": [{"subnet": "172.30.0.0/24"}]}
            }
        },
    }


def test_valid_compose_security_contract(tmp_path) -> None:
    config = _compose_config(tmp_path)

    tls_dir, hostname, minimum_days = validate_compose_config(
        config,
        repo_root=tmp_path / "repo",
    )

    assert tls_dir == (tmp_path / "tls").resolve()
    assert hostname == HOSTNAME
    assert minimum_days == 30


def test_valid_compose_allows_read_only_named_source_volume(tmp_path) -> None:
    config = _compose_config(tmp_path)
    for service_name in ("backend", "execution-worker"):
        service = config["services"][service_name]
        service["environment"]["EXECUTION_SOURCE_ROOT"] = (
            "/data/execution-source/10特纤/02-检验"
        )
        source_mount = next(
            mount
            for mount in service["volumes"]
            if mount["target"] == "/data/execution-input"
        )
        source_mount.update(
            {
                "type": "volume",
                "source": "textile-area-out",
                "target": "/data/execution-source",
                "read_only": True,
            }
        )

    validate_compose_config(config, repo_root=tmp_path / "repo")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda config: config["services"]["backend"]["environment"].update(
                {"FORWARDED_ALLOW_IPS": "*"}
            ),
            "FRONTEND_PROXY_IP",
        ),
        (
            lambda config: config["services"]["postgres"].update(
                {"ports": [{"target": 5432, "published": "5432"}]}
            ),
            "仅允许 frontend",
        ),
        (
            lambda config: config["services"]["frontend"]["ports"][0].update(
                {"published": "8443"}
            ),
            "宿主机 443",
        ),
        (
            lambda config: config["services"]["frontend"]["ports"][0].update(
                {"host_ip": "0.0.0.0"}
            ),
            "RFC1918",
        ),
        (
            lambda config: config["services"]["frontend"]["ports"][0].pop(
                "host_ip"
            ),
            "不能省略 host_ip",
        ),
        (
            lambda config: config["services"]["frontend"]["environment"].update(
                {"MANAGEMENT_CIDRS": "0.0.0.0/0"}
            ),
            "RFC1918",
        ),
        (
            lambda config: config["services"]["frontend"]["environment"].update(
                {"MANAGEMENT_CIDRS": "192.0.2.0/24"}
            ),
            "RFC1918",
        ),
        (
            lambda config: config["services"]["frontend"]["ports"][0].update(
                {"host_ip": "192.0.2.50"}
            ),
            "RFC1918",
        ),
        (
            lambda config: config["services"]["frontend"]["ports"][0].update(
                {"host_ip": "127.0.0.1"}
            ),
            "RFC1918",
        ),
        (
            lambda config: config["networks"]["textile-net"]["ipam"][
                "config"
            ][0].update({"subnet": "192.0.2.0/24"}),
            "DOCKER_SUBNET",
        ),
        (
            lambda config: config["services"]["frontend"].update(
                {
                    "healthcheck": {
                        "test": [
                            "CMD-SHELL",
                            (
                                "wget --no-check-" "certificate "
                                "https://127.0.0.1/healthz"
                            ),
                        ]
                    }
                }
            ),
            "仅回环 HTTP",
        ),
    ],
)
def test_compose_security_contract_rejects_unsafe_values(
    tmp_path,
    mutate,
    message: str,
) -> None:
    config = _compose_config(tmp_path)
    mutate(config)

    with pytest.raises(DeploymentValidationError, match=message):
        validate_compose_config(config, repo_root=tmp_path / "repo")


def test_https_probe_requires_literal_address_without_dns(tmp_path) -> None:
    with pytest.raises(DeploymentValidationError, match="不执行 DNS 查询"):
        probe_https_endpoint(
            address=HOSTNAME,
            port=443,
            hostname=HOSTNAME,
            root_ca_path=tmp_path / "root-ca.pem",
            expected_hsts_max_age=300,
        )


def test_nginx_template_enforces_tls_host_and_health_contract() -> None:
    project_root = Path(__file__).resolve().parents[2]
    template = (project_root / "frontend" / "nginx.conf").read_text()

    assert "listen 127.0.0.1:8080;" in template
    assert template.count("location = /healthz") == 1
    assert template.count("location = /backend-ready") == 1
    assert "proxy_pass http://backend:8000/health/ready;" in template
    assert "listen 443 ssl default_server;" in template
    assert "server_name ${PUBLIC_HOSTNAME};" in template
    assert "server_name _;" not in template
    assert "/etc/nginx/tls/fullchain.pem" in template
    assert "/etc/nginx/tls/privkey.pem" in template
    assert "${NGINX_MANAGEMENT_ALLOW_RULES}" in template
    assert 'max-age=${HSTS_MAX_AGE}' in template
    assert "location = /health/live" in template
    assert "location = /health/ready" in template
    live_location = template.split("location = /health/live", 1)[1].split(
        "}",
        1,
    )[0]
    ready_location = template.split("location = /health/ready", 1)[1].split(
        "}",
        1,
    )[0]
    assert "allow all;" in live_location
    assert "allow all;" not in ready_location
