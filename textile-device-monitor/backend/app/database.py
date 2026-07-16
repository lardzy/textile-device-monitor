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
    add_column = (
        "ADD COLUMN IF NOT EXISTS" if dialect == "postgresql" else "ADD COLUMN"
    )

    statements: list[str] = []
    if "is_placeholder" not in columns:
        statements.append(
            "ALTER TABLE queue_records "
            f"{add_column} is_placeholder BOOLEAN NOT NULL DEFAULT {false_literal}"
        )
    if "auto_remove_when_inactive" not in columns:
        statements.append(
            "ALTER TABLE queue_records "
            f"{add_column} auto_remove_when_inactive BOOLEAN NOT NULL DEFAULT {false_literal}"
        )

    with engine.begin() as connection:
        if dialect == "postgresql":
            # The compatibility rewrite and index creation must see a stable
            # queue even if an older backend instance is still serving writes.
            connection.execute(
                text("LOCK TABLE queue_records IN ACCESS EXCLUSIVE MODE")
            )

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

        # Existing installations predate the waiting-position invariant. Put
        # every device queue into a stable 1..N order before adding the partial
        # unique index. Temporary values are below the current minimum so this
        # is also safe when the index already exists.
        waiting_rows = connection.execute(
            text(
                "SELECT id, device_id, position "
                "FROM queue_records "
                "WHERE UPPER(CAST(status AS TEXT)) = 'WAITING' "
                "ORDER BY device_id, position, submitted_at, id"
            )
        ).mappings().all()
        if waiting_rows:
            minimum_position = min(int(row["position"]) for row in waiting_rows)
            temporary_base = min(minimum_position, 0) - len(waiting_rows) - 1
            for offset, row in enumerate(waiting_rows):
                connection.execute(
                    text(
                        "UPDATE queue_records SET position = :position "
                        "WHERE id = :queue_id"
                    ),
                    {
                        "position": temporary_base - offset,
                        "queue_id": row["id"],
                    },
                )

            positions_by_device: dict[int, int] = {}
            for row in waiting_rows:
                device_id = int(row["device_id"])
                next_position = positions_by_device.get(device_id, 0) + 1
                positions_by_device[device_id] = next_position
                version_increment = (
                    1 if int(row["position"]) != next_position else 0
                )
                connection.execute(
                    text(
                        "UPDATE queue_records "
                        "SET position = :position, "
                        "version = COALESCE(version, 0) + :version_increment "
                        "WHERE id = :queue_id"
                    ),
                    {
                        "position": next_position,
                        "version_increment": version_increment,
                        "queue_id": row["id"],
                    },
                )

        if dialect in {"postgresql", "sqlite"}:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_queue_records_waiting_device_position "
                    "ON queue_records (device_id, position) "
                    "WHERE status = 'WAITING'"
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
