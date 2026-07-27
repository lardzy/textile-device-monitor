import pytest
from unittest.mock import patch

from app.config import Settings


def production_settings(**overrides) -> Settings:
    values = {
        "APP_ENV": "production",
        "DATABASE_URL": (
            "postgresql://textile_app:StrongDbPassword_2026"
            "@postgres:5432/textile_monitor"
        ),
        "SECRET_KEY": "secret-" + ("a" * 48),
        "EXECUTION_ENABLED": True,
        "EXECUTION_SESSION_SECRET": "session-" + ("b" * 48),
        "EXECUTION_CREDENTIAL_KEY": "credential-" + ("c" * 48),
        "EXECUTION_COOKIE_SECURE": True,
        "CORS_ORIGINS": "",
        "PUBLIC_HOSTNAME": "textile-monitor.internal",
        "PUBLIC_ORIGIN": "https://textile-monitor.internal",
        "TLS_DIR_HOST_PATH": "/srv/textile-monitor/tls",
        "TLS_MIN_VALID_DAYS": 30,
        "HSTS_MAX_AGE": 300,
        "MANAGEMENT_CIDRS": "192.168.106.0/24,10.8.0.0/24",
        "EXECUTION_INDEX_INTERVAL_SECONDS": 300,
    }
    values.update(overrides)
    return Settings(**values)


def test_valid_production_security_configuration() -> None:
    production_settings().validate_execution_security()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (
            {"DATABASE_URL": "postgresql://admin:password123@postgres/db"},
            "database password",
        ),
        ({"DATABASE_URL": "sqlite:///production.db"}, "PostgreSQL"),
        ({"SECRET_KEY": "change-me"}, "SECRET_KEY"),
        (
            {"SECRET_KEY": "replace-with-placeholder-" + ("x" * 32)},
            "SECRET_KEY",
        ),
        ({"EXECUTION_SESSION_SECRET": "change-me"}, "SESSION_SECRET"),
        ({"EXECUTION_CREDENTIAL_KEY": "change-me"}, "CREDENTIAL_KEY"),
        ({"EXECUTION_COOKIE_SECURE": False}, "COOKIE_SECURE"),
        ({"CORS_ORIGINS": "*"}, "wildcard"),
        ({"CORS_ORIGINS": "http://monitor.local"}, "HTTPS"),
        ({"PUBLIC_HOSTNAME": "192.168.106.50"}, "PUBLIC_HOSTNAME"),
        (
            {"PUBLIC_ORIGIN": "http://textile-monitor.internal"},
            "PUBLIC_ORIGIN",
        ),
        (
            {"PUBLIC_ORIGIN": "https://textile-monitor.internal:bad"},
            "invalid port",
        ),
        ({"TLS_DIR_HOST_PATH": ""}, "TLS_DIR_HOST_PATH"),
        ({"TLS_MIN_VALID_DAYS": 0}, "TLS_MIN_VALID_DAYS"),
        ({"HSTS_MAX_AGE": 31536001}, "HSTS_MAX_AGE"),
        ({"MANAGEMENT_CIDRS": ""}, "MANAGEMENT_CIDRS"),
        ({"MANAGEMENT_CIDRS": "0.0.0.0/0"}, "RFC1918"),
        ({"MANAGEMENT_CIDRS": "8.8.8.0/24"}, "RFC1918"),
        ({"MANAGEMENT_CIDRS": "192.0.2.0/24"}, "RFC1918"),
        ({"MANAGEMENT_CIDRS": "127.0.0.0/8"}, "RFC1918"),
        ({"MANAGEMENT_CIDRS": "169.254.0.0/16"}, "RFC1918"),
        ({"MANAGEMENT_CIDRS": "fd00::/8"}, "RFC1918"),
        ({"MANAGEMENT_CIDRS": "192.168.1.3/24"}, "invalid network"),
        ({"EXECUTION_INDEX_INTERVAL_SECONDS": 5}, "at least 30"),
        ({"EXECUTION_NODE_MAX_ATTEMPTS": 0}, "between 1 and 100"),
        (
            {"EXECUTION_WORKER_HEARTBEAT_TIMEOUT_SECONDS": 5},
            "at least 15 seconds",
        ),
    ],
)
def test_production_rejects_unsafe_configuration(
    override: dict,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        production_settings(**override).validate_execution_security()


def test_production_requires_independent_secrets() -> None:
    shared = "shared-" + ("x" * 48)
    with pytest.raises(RuntimeError, match="independent"):
        production_settings(
            SECRET_KEY=shared,
            EXECUTION_SESSION_SECRET=shared,
        ).validate_execution_security()


def test_disabling_execution_does_not_bypass_database_or_app_secret_checks() -> None:
    with pytest.raises(RuntimeError, match="database password"):
        production_settings(
            EXECUTION_ENABLED=False,
            DATABASE_URL="postgresql://admin:password123@postgres/db",
        ).validate_execution_security()


def test_development_can_use_http_and_insecure_cookie() -> None:
    settings = Settings(
        APP_ENV="development",
        DATABASE_URL="sqlite:///./runtime/development.db",
        SECRET_KEY="",
        EXECUTION_SESSION_SECRET="",
        EXECUTION_CREDENTIAL_KEY="",
        EXECUTION_COOKIE_SECURE=False,
        CORS_ORIGINS="http://127.0.0.1:3100",
    )
    settings.validate_execution_security()


def test_cors_allow_list_is_trimmed_and_normalized() -> None:
    settings = Settings(
        APP_ENV="development",
        CORS_ORIGINS=" https://monitor.example/,, https://admin.example ",
    )
    assert settings.cors_origins() == [
        "https://monitor.example",
        "https://admin.example",
    ]


def test_index_interval_safe_default_is_five_minutes() -> None:
    settings = Settings(APP_ENV="development")
    assert settings.EXECUTION_INDEX_INTERVAL_SECONDS == 300
    assert settings.EXECUTION_NODE_MAX_ATTEMPTS == 5
    assert settings.EXECUTION_WORKER_HEARTBEAT_TIMEOUT_SECONDS == 45
    assert settings.PUBLIC_HOSTNAME == "textile-monitor.internal"
    assert settings.PUBLIC_ORIGIN == "https://textile-monitor.internal"
    assert settings.TLS_MIN_VALID_DAYS == 30
    assert settings.HSTS_MAX_AGE == 300


def test_management_cidrs_are_parsed_as_networks() -> None:
    settings = Settings(
        APP_ENV="development",
        MANAGEMENT_CIDRS=" 192.168.106.0/24, 10.8.0.0/24 ",
    )
    assert [str(network) for network in settings.management_cidrs()] == [
        "192.168.106.0/24",
        "10.8.0.0/24",
    ]


def test_execution_storage_roots_reject_same_or_nested_real_paths(
    tmp_path,
) -> None:
    source = tmp_path / "source"
    publish = tmp_path / "publish"
    source.mkdir()
    publish.mkdir()

    with pytest.raises(RuntimeError, match="pairwise separate"):
        Settings(
            APP_ENV="development",
            EXECUTION_SOURCE_ROOT=str(source),
            EXECUTION_RUNTIME_ROOT=str(source),
            EXECUTION_PUBLISH_ROOT=str(publish),
        ).validate_execution_security()

    with pytest.raises(RuntimeError, match="pairwise separate"):
        Settings(
            APP_ENV="development",
            EXECUTION_SOURCE_ROOT=str(source),
            EXECUTION_RUNTIME_ROOT=str(source / "runtime"),
            EXECUTION_PUBLISH_ROOT=str(publish),
        ).validate_execution_security()


def test_execution_storage_roots_resolve_symlink_aliases(tmp_path) -> None:
    source = tmp_path / "source"
    runtime = tmp_path / "runtime"
    publish_alias = tmp_path / "publish-alias"
    source.mkdir()
    runtime.mkdir()
    publish_alias.symlink_to(runtime, target_is_directory=True)

    with pytest.raises(RuntimeError, match="pairwise separate"):
        Settings(
            APP_ENV="development",
            EXECUTION_SOURCE_ROOT=str(source),
            EXECUTION_RUNTIME_ROOT=str(runtime),
            EXECUTION_PUBLISH_ROOT=str(publish_alias),
        ).validate_execution_security()


def test_worker_entrypoint_fails_before_starting_on_unsafe_configuration() -> None:
    from app.execution import worker

    with (
        patch.object(
            Settings,
            "validate_execution_security",
            side_effect=RuntimeError("unsafe worker configuration"),
        ) as validate,
        patch.object(worker, "ExecutionWorker") as worker_class,
    ):
        with pytest.raises(RuntimeError, match="unsafe worker configuration"):
            worker.main()

    validate.assert_called_once_with()
    worker_class.assert_not_called()
