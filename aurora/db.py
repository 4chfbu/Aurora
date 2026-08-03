from collections.abc import Generator
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from aurora.config import get_settings


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)


def init_db() -> None:
    from aurora import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    # SQLite's create_all does not add columns to an existing local MVP database.
    if settings.database_url.startswith("sqlite"):
        _add_sqlite_columns()


def _add_sqlite_columns() -> None:
    expected = {
        "attempt": {"parent_attempt_id": "TEXT"},
        "worker": {"parent_worker_id": "TEXT", "execution_kind": "TEXT DEFAULT 'primary'"},
        "importbatch": {"auth_method": "TEXT", "login_domain": "TEXT", "auth_message": "TEXT"},
        "project": {
            "target_verification_status": "TEXT DEFAULT 'UNVERIFIED'",
            "target_verification_reason": "TEXT",
            "target_verified_at": "DATETIME",
            "target_url": "TEXT",
        },
        "challengegroup": {
            "deadline_at": "DATETIME",
            "max_concurrent": "INTEGER DEFAULT 1",
            "finished_at": "DATETIME",
        },
        "challengegroupitem": {
            "fused_status": "TEXT DEFAULT 'PENDING'",
            "phase": "INTEGER DEFAULT 1",
            "phase_attempts": "JSON DEFAULT '{}'",
            "failure_history": "JSON DEFAULT '[]'",
            "competition_meta": "JSON DEFAULT '{}'",
            "hint_taken": "BOOLEAN DEFAULT 0",
            "hint_content": "TEXT",
            "submission_status": "TEXT DEFAULT 'NOT_SUBMITTED'",
        },
    }
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table, columns in expected.items():
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
