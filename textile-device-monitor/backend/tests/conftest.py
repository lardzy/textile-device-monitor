"""Fail-closed database isolation for the complete backend test suite.

This module is loaded by pytest before it imports any test module.  That order
is important because ``app.database`` creates its engine at import time.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from sqlalchemy import MetaData


def _is_disposable_test_url(url: str, *, allow_memory: bool) -> bool:
    parsed = urlparse(url)
    if parsed.scheme == "sqlite":
        if parsed.path in {"/:memory:", ":memory:"}:
            return allow_memory
        return Path(parsed.path).stem.lower().endswith("_test")
    database_name = parsed.path.rsplit("/", 1)[-1].lower()
    return bool(database_name) and database_name.endswith("_test")


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    raise pytest.UsageError(
        "拒绝启动测试：必须显式设置 TEST_DATABASE_URL，且数据库名必须以 _test 结尾"
    )
if not _is_disposable_test_url(TEST_DATABASE_URL, allow_memory=False):
    raise pytest.UsageError(
        "拒绝启动测试：TEST_DATABASE_URL 必须指向名称以 _test 结尾的独立测试数据库"
    )

# Must happen before collection imports app.config/app.database.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["TEXTILE_TESTING"] = "1"
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("OCR_ENABLED", "false")
os.environ.setdefault("AREA_ENABLED", "false")
os.environ.setdefault("AREA_OUTPUT_DIR", "/tmp/textile_area_outputs_test")


_original_create_all = MetaData.create_all
_original_drop_all = MetaData.drop_all


def _guard_test_ddl(bind) -> None:
    database_url = str(getattr(bind, "url", ""))
    if not database_url or not _is_disposable_test_url(
        database_url,
        allow_memory=True,
    ):
        raise RuntimeError(
            "拒绝测试 DDL：建表或清表目标不是明确的 _test 数据库或内存数据库"
        )


def _safe_create_all(self, bind, *args, **kwargs):
    _guard_test_ddl(bind)
    return _original_create_all(self, bind, *args, **kwargs)


def _safe_drop_all(self, bind, *args, **kwargs):
    _guard_test_ddl(bind)
    return _original_drop_all(self, bind, *args, **kwargs)


MetaData.create_all = _safe_create_all
MetaData.drop_all = _safe_drop_all


@pytest.fixture(scope="session", autouse=True)
def _prepare_explicit_test_schema():
    """Create application tables only after the disposable target is proven."""

    from app.database import Base, engine
    import app.execution.models  # noqa: F401
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
