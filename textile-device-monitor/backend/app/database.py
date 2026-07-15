from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.config import settings

engine = create_engine(settings.DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_queue_record_schema() -> None:
    inspector = inspect(engine)
    if "queue_records" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("queue_records")}
    dialect = engine.dialect.name
    false_literal = "FALSE" if dialect == "postgresql" else "0"

    statements: list[str] = []
    if "is_placeholder" not in columns:
        statements.append(
            "ALTER TABLE queue_records "
            f"ADD COLUMN is_placeholder BOOLEAN NOT NULL DEFAULT {false_literal}"
        )
    if "auto_remove_when_inactive" not in columns:
        statements.append(
            "ALTER TABLE queue_records "
            f"ADD COLUMN auto_remove_when_inactive BOOLEAN NOT NULL DEFAULT {false_literal}"
        )

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))

        connection.execute(
            text(
                "UPDATE queue_records "
                f"SET is_placeholder = {false_literal} "
                "WHERE is_placeholder IS NULL"
            )
        )
        connection.execute(
            text(
                "UPDATE queue_records "
                f"SET auto_remove_when_inactive = {false_literal} "
                "WHERE auto_remove_when_inactive IS NULL"
            )
        )


def ensure_area_job_schema() -> None:
    inspector = inspect(engine)
    if "area_job_instances" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("area_job_instances")}
    statements: list[str] = []
    if "source" not in columns:
        statements.append(
            "ALTER TABLE area_job_instances "
            "ADD COLUMN source VARCHAR(32) NOT NULL DEFAULT 'inference'"
        )
    if "initial_class_name" not in columns:
        statements.append(
            "ALTER TABLE area_job_instances "
            "ADD COLUMN initial_class_name VARCHAR(100)"
        )

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(
            text(
                "UPDATE area_job_instances "
                "SET source = 'inference' "
                "WHERE source IS NULL OR source = ''"
            )
        )
        connection.execute(
            text(
                "UPDATE area_job_instances "
                "SET initial_class_name = class_name "
                "WHERE initial_class_name IS NULL OR initial_class_name = ''"
            )
        )
