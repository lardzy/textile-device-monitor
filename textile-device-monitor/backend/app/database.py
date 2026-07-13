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
