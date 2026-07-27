from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    UniqueConstraint,
    inspect,
)
from sqlalchemy.schema import Index, Table

import app.models as _legacy_models  # noqa: F401 -- register legacy metadata
from app.config import settings
from app.database import Base, engine


LEGACY_BASELINE_REVISION = "0001_legacy_baseline"

_POSTGRES_CAST_RE = re.compile(
    r"::(?:[a-z_][a-z0-9_]*\.)?[a-z_][a-z0-9_]*(?:\[\])?"
    r"(?:\s+(?:varying|precision|without\s+time\s+zone|with\s+time\s+zone))?",
    re.IGNORECASE,
)


def _legacy_metadata_tables() -> dict[str, Table]:
    """Return the complete legacy schema, even if execution models were imported."""

    return {
        table.name: table
        for table in Base.metadata.sorted_tables
        if not table.name.startswith("execution_")
    }


def _strip_balanced_outer_parentheses(value: str) -> str:
    result = value.strip()
    while result.startswith("(") and result.endswith(")"):
        depth = 0
        encloses_all = True
        for index, character in enumerate(result):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(result) - 1:
                    encloses_all = False
                    break
        if not encloses_all or depth != 0:
            break
        result = result[1:-1].strip()
    return result


def _normalize_sql_expression(value: Any) -> str | None:
    """Normalize reflected SQL without requiring dialect-specific text identity.

    PostgreSQL adds casts to reflected defaults/checks and SQLite adds enclosing
    parentheses. They are representation differences, not schema drift.
    """

    if value is None:
        return None
    rendered = str(value).strip().rstrip(";")
    rendered = _strip_balanced_outer_parentheses(rendered)
    rendered = _POSTGRES_CAST_RE.sub("", rendered)
    rendered = _strip_balanced_outer_parentheses(rendered)
    rendered = re.sub(r'\bpublic\.', "", rendered, flags=re.IGNORECASE)
    rendered = rendered.replace('"', "")
    rendered = re.sub(r"\s+", "", rendered).lower()
    rendered = re.sub(r"\(([a-z_][a-z0-9_.]*)\)", r"\1", rendered)
    if rendered in {"now()", "current_timestamp", "current_timestamp()"}:
        return "current_timestamp"
    if rendered in {"true", "'true'", "1", "'1'"}:
        return "true"
    if rendered in {"false", "'false'", "0", "'0'"}:
        return "false"
    return rendered


def _render_server_default(column: Column[Any], dialect: Any) -> str | None:
    if column.server_default is None:
        return None
    argument = column.server_default.arg
    try:
        return str(
            argument.compile(
                dialect=dialect,
                compile_kwargs={"literal_binds": True},
            )
        )
    except (AttributeError, TypeError):
        return str(argument)


def _is_implicit_autoincrement_default(
    column: Column[Any],
    reflected_default: Any,
) -> bool:
    """PostgreSQL reflects SERIAL columns as nextval(...), unlike ORM metadata."""

    normalized = _normalize_sql_expression(reflected_default) or ""
    return (
        bool(column.primary_key)
        and column.autoincrement in {True, "auto"}
        and normalized.startswith("nextval(")
    )


def _expected_foreign_keys(table: Table) -> set[tuple[Any, ...]]:
    foreign_keys: set[tuple[Any, ...]] = set()
    for constraint in table.constraints:
        if not isinstance(constraint, ForeignKeyConstraint):
            continue
        target_columns = tuple(element.target_fullname for element in constraint.elements)
        foreign_keys.add(
            (
                tuple(column.name for column in constraint.columns),
                target_columns,
                (constraint.ondelete or "").upper(),
            )
        )
    return foreign_keys


def _reflected_foreign_keys(
    reflected: list[dict[str, Any]],
) -> set[tuple[Any, ...]]:
    foreign_keys: set[tuple[Any, ...]] = set()
    for constraint in reflected:
        schema = constraint.get("referred_schema")
        table = constraint.get("referred_table")
        target_columns = tuple(
            (
                f"{schema + '.' if schema and schema != 'public' else ''}"
                f"{table}.{column}"
            )
            for column in constraint.get("referred_columns") or ()
        )
        options = constraint.get("options") or {}
        foreign_keys.add(
            (
                tuple(constraint.get("constrained_columns") or ()),
                target_columns,
                str(options.get("ondelete") or "").upper(),
            )
        )
    return foreign_keys


def _expected_unique_constraints(table: Table) -> set[tuple[Any, ...]]:
    return {
        (constraint.name or "", tuple(column.name for column in constraint.columns))
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _reflected_unique_constraints(
    reflected: list[dict[str, Any]],
) -> set[tuple[Any, ...]]:
    return {
        (
            constraint.get("name") or "",
            tuple(constraint.get("column_names") or ()),
        )
        for constraint in reflected
    }


def _index_predicate(index: Index, dialect_name: str) -> str | None:
    if dialect_name not in {"postgresql", "sqlite"}:
        return None
    return _normalize_sql_expression(
        index.dialect_options[dialect_name].get("where")
    )


def _expected_indexes(
    table: Table,
    dialect_name: str,
) -> dict[str, tuple[Any, ...]]:
    return {
        index.name: (
            tuple(column.name for column in index.columns),
            bool(index.unique),
            _index_predicate(index, dialect_name),
        )
        for index in table.indexes
        if index.name
    }


def _reflected_indexes(
    reflected: list[dict[str, Any]],
    dialect_name: str,
) -> dict[str, tuple[Any, ...]]:
    indexes: dict[str, tuple[Any, ...]] = {}
    predicate_key = f"{dialect_name}_where"
    for index in reflected:
        # PostgreSQL also reflects the backing index of a UNIQUE constraint.
        # The constraint itself is checked independently.
        if index.get("duplicates_constraint"):
            continue
        name = index.get("name")
        if not name:
            continue
        dialect_options = index.get("dialect_options") or {}
        indexes[name] = (
            tuple(index.get("column_names") or ()),
            bool(index.get("unique")),
            _normalize_sql_expression(dialect_options.get(predicate_key)),
        )
    return indexes


def _expected_checks(table: Table) -> set[tuple[str, str | None]]:
    return {
        (
            constraint.name or "",
            _normalize_sql_expression(constraint.sqltext),
        )
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }


def _reflected_checks(
    reflected: list[dict[str, Any]],
) -> set[tuple[str, str | None]]:
    return {
        (
            constraint.get("name") or "",
            _normalize_sql_expression(constraint.get("sqltext")),
        )
        for constraint in reflected
    }


def _render_set(values: set[tuple[Any, ...]]) -> str:
    return ", ".join(repr(value) for value in sorted(values, key=repr))


def _alembic_config() -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    return config


def expected_head_revision() -> str:
    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    if not head:
        raise RuntimeError("Alembic migration head is not configured")
    return head


def preflight_legacy_schema() -> None:
    expected_tables = _legacy_metadata_tables()
    drift: list[str] = []

    with engine.connect() as connection:
        inspector = inspect(connection)
        migration_impl = MigrationContext.configure(connection).impl
        dialect = connection.dialect
        table_names = set(inspector.get_table_names())
        unexpected_execution_tables = sorted(
            table_name
            for table_name in table_names
            if table_name.startswith("execution_")
        )
        missing_tables = sorted(set(expected_tables) - table_names)
        unexpected_tables = sorted(
            table_names
            - set(expected_tables)
            - set(unexpected_execution_tables)
            - {"alembic_version"}
        )

        if unexpected_execution_tables:
            drift.append(
                "unversioned execution tables: "
                + ", ".join(unexpected_execution_tables)
            )
        if missing_tables:
            drift.append(f"missing tables: {', '.join(missing_tables)}")
        if unexpected_tables:
            drift.append(f"unexpected tables: {', '.join(unexpected_tables)}")

        for table_name, expected_table in sorted(expected_tables.items()):
            if table_name not in table_names:
                continue

            reflected_columns = {
                column["name"]: column
                for column in inspector.get_columns(table_name)
            }
            expected_columns = {
                column.name: column
                for column in expected_table.columns
            }
            missing_columns = sorted(set(expected_columns) - set(reflected_columns))
            unexpected_columns = sorted(
                set(reflected_columns) - set(expected_columns)
            )
            if missing_columns:
                drift.append(
                    f"{table_name} missing columns: {', '.join(missing_columns)}"
                )
            if unexpected_columns:
                drift.append(
                    f"{table_name} unexpected columns: "
                    + ", ".join(unexpected_columns)
                )

            for column_name in sorted(set(expected_columns) & set(reflected_columns)):
                expected_column = expected_columns[column_name]
                reflected_column = reflected_columns[column_name]
                inspector_column = Column(
                    column_name,
                    reflected_column["type"],
                    nullable=bool(reflected_column.get("nullable", True)),
                )
                if migration_impl.compare_type(
                    inspector_column,
                    expected_column,
                ):
                    drift.append(
                        f"{table_name}.{column_name} type: "
                        f"expected {expected_column.type}, "
                        f"found {reflected_column['type']}"
                    )

                expected_nullable = bool(expected_column.nullable)
                reflected_nullable = bool(reflected_column.get("nullable", True))
                if expected_nullable != reflected_nullable:
                    drift.append(
                        f"{table_name}.{column_name} nullable: "
                        f"expected {expected_nullable}, found {reflected_nullable}"
                    )

                expected_default = _normalize_sql_expression(
                    _render_server_default(expected_column, dialect)
                )
                reflected_default_raw = reflected_column.get("default")
                reflected_default = _normalize_sql_expression(
                    reflected_default_raw
                )
                if (
                    expected_default is None
                    and _is_implicit_autoincrement_default(
                        expected_column,
                        reflected_default_raw,
                    )
                ):
                    reflected_default = None
                if expected_default != reflected_default:
                    drift.append(
                        f"{table_name}.{column_name} server default: "
                        f"expected {expected_default!r}, "
                        f"found {reflected_default!r}"
                    )

            expected_pk = tuple(
                column.name for column in expected_table.primary_key.columns
            )
            reflected_pk = tuple(
                inspector.get_pk_constraint(table_name).get(
                    "constrained_columns",
                    (),
                )
                or ()
            )
            if expected_pk != reflected_pk:
                drift.append(
                    f"{table_name} primary key: "
                    f"expected {expected_pk!r}, found {reflected_pk!r}"
                )

            expected_foreign_keys = _expected_foreign_keys(expected_table)
            reflected_foreign_keys = _reflected_foreign_keys(
                inspector.get_foreign_keys(table_name)
            )
            if expected_foreign_keys != reflected_foreign_keys:
                drift.append(
                    f"{table_name} foreign keys: "
                    f"missing [{_render_set(expected_foreign_keys - reflected_foreign_keys)}], "
                    f"unexpected [{_render_set(reflected_foreign_keys - expected_foreign_keys)}]"
                )

            expected_uniques = _expected_unique_constraints(expected_table)
            reflected_uniques = _reflected_unique_constraints(
                inspector.get_unique_constraints(table_name)
            )
            if expected_uniques != reflected_uniques:
                drift.append(
                    f"{table_name} unique constraints: "
                    f"missing [{_render_set(expected_uniques - reflected_uniques)}], "
                    f"unexpected [{_render_set(reflected_uniques - expected_uniques)}]"
                )

            expected_indexes = _expected_indexes(
                expected_table,
                dialect.name,
            )
            reflected_indexes = _reflected_indexes(
                inspector.get_indexes(table_name),
                dialect.name,
            )
            if expected_indexes != reflected_indexes:
                missing_indexes = sorted(
                    set(expected_indexes) - set(reflected_indexes)
                )
                unexpected_indexes = sorted(
                    set(reflected_indexes) - set(expected_indexes)
                )
                changed_indexes = sorted(
                    name
                    for name in set(expected_indexes) & set(reflected_indexes)
                    if expected_indexes[name] != reflected_indexes[name]
                )
                drift.append(
                    f"{table_name} indexes: missing {missing_indexes!r}, "
                    f"unexpected {unexpected_indexes!r}, "
                    f"changed {changed_indexes!r}"
                )

            expected_checks = _expected_checks(expected_table)
            reflected_checks = _reflected_checks(
                inspector.get_check_constraints(table_name)
            )
            if expected_checks != reflected_checks:
                drift.append(
                    f"{table_name} check constraints: "
                    f"missing [{_render_set(expected_checks - reflected_checks)}], "
                    f"unexpected [{_render_set(reflected_checks - expected_checks)}]"
                )

    if drift:
        raise RuntimeError(
            "Existing database does not match the verified legacy baseline ("
            + " | ".join(drift)
            + "). Refusing to stamp it."
        )


def migrate_database() -> None:
    settings.validate_execution_security()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    config = _alembic_config()

    if "alembic_version" not in tables and tables:
        preflight_legacy_schema()
        command.stamp(config, LEGACY_BASELINE_REVISION)

    command.upgrade(config, "head")


def assert_safe_test_database_url(url: str | None = None) -> str:
    explicit = url or os.getenv("TEST_DATABASE_URL")
    if not explicit:
        raise RuntimeError("TEST_DATABASE_URL must be explicitly configured")

    if explicit.startswith("sqlite:///:memory:"):
        return explicit

    parsed = urlparse(explicit)
    if parsed.scheme == "sqlite":
        database_name = Path(parsed.path).stem.lower()
    else:
        database_name = parsed.path.rsplit("/", 1)[-1].lower()
    if not database_name.endswith("_test"):
        raise RuntimeError("Test database name must end with '_test'")
    return explicit


if __name__ == "__main__":
    migrate_database()
