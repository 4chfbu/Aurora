from collections.abc import Generator
import hashlib
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

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
    _backfill_legacy_flag_candidates()
    _invalidate_legacy_false_targets()


def _add_sqlite_columns() -> None:
    expected = {
        "attempt": {
            "parent_attempt_id": "TEXT",
            "codex_control_token_hash": "TEXT",
            "last_event_at": "DATETIME",
            "resume_count": "INTEGER DEFAULT 0",
            "blackboard_version": "INTEGER DEFAULT 0",
        },
        "attemptcheckpoint": {"generated_intent_ids": "JSON DEFAULT '[]'"},
        "fact": {"evidence_items": "JSON DEFAULT '[]'"},
        "artifact": {"origin_kind": "TEXT DEFAULT 'unclassified'"},
        "discoveredtarget": {
            "source": "TEXT DEFAULT 'automatic'",
            "confidence": "FLOAT DEFAULT 0.0",
            "probe_json": "JSON DEFAULT '{}'",
            "updated_at": "DATETIME",
        },
        "worker": {"parent_worker_id": "TEXT", "execution_kind": "TEXT DEFAULT 'primary'"},
        "importbatch": {
            "auth_method": "TEXT",
            "login_domain": "TEXT",
            "auth_message": "TEXT",
            "platform": "TEXT",
            "extraction_strategy": "TEXT",
            "pages_scanned": "INTEGER DEFAULT 0",
            "diagnostics_json": "JSON DEFAULT '[]'",
        },
        "importcandidate": {"source_metadata_json": "JSON DEFAULT '{}'"},
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
        connection.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS uq_flagcandidate_project_value ON flagcandidate (project_id, value_hash)")
        )


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


def _backfill_legacy_flag_candidates() -> None:
    """Retain historical flag Findings for review without trusting them."""
    from aurora.models import Finding, FlagCandidate

    with Session(engine) as session:
        findings = session.exec(select(Finding).where(Finding.title.startswith("Candidate flag: "))).all()
        changed = False
        for finding in findings:
            value = finding.title.removeprefix("Candidate flag: ").strip()
            if not value:
                continue
            value_hash = hashlib.sha256(value.encode("utf-8")).hexdigest()
            existing = session.exec(
                select(FlagCandidate).where(FlagCandidate.project_id == finding.project_id, FlagCandidate.value_hash == value_hash)
            ).first()
            if existing is not None:
                continue
            session.add(
                FlagCandidate(
                    project_id=finding.project_id,
                    value=value,
                    value_hash=value_hash,
                    status="AWAITING_MANUAL_VALIDATION",
                    provenance_kind="LEGACY_UNVERIFIED",
                    artifact_refs=finding.evidence_refs,
                )
            )
            changed = True
        if changed:
            session.commit()


def _invalidate_legacy_false_targets() -> None:
    from aurora.services.target_repair import invalidate_false_targets

    with Session(engine) as session:
        invalidate_false_targets(session)
