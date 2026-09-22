from sqlalchemy import inspect, text
from sqlmodel import SQLModel, create_engine

from aurora import db


def test_evidence_columns_upgrade_existing_database_idempotently(tmp_path, monkeypatch) -> None:
    database = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    SQLModel.metadata.create_all(database)
    with database.begin() as connection:
        connection.execute(text("ALTER TABLE artifact DROP COLUMN evidence_context"))
        connection.execute(text("ALTER TABLE attempt DROP COLUMN environment_id"))
        connection.execute(text("INSERT INTO artifact (id, project_id, type, path, sha256, size, sensitivity, origin_kind, created_at) VALUES ('old', 'project', 'terminal', 'old.txt', 'digest', 0, 'normal', 'unclassified', '2026-09-04 00:00:00')"))
    monkeypatch.setattr(db, "engine", database)
    db._add_sqlite_columns()
    db._add_sqlite_columns()
    assert "environment_id" in {column["name"] for column in inspect(database).get_columns("attempt")}
    with database.connect() as connection:
        assert connection.execute(text("SELECT evidence_context FROM artifact WHERE id='old'")).scalar_one() == "{}"
