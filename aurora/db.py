from collections.abc import Generator
import hashlib
from sqlalchemy import event, inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

from aurora.config import get_settings


SCHEMA_VERSION = 7


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """Enable WAL and a busy timeout so concurrent writers (lease heartbeat,
    worker callback endpoints, and parallel challenge-group projects) do not
    raise "database is locked" on the default rollback-journal profile."""
    if not settings.database_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute(f"PRAGMA journal_mode={settings.db_journal_mode}")
    cursor.execute(f"PRAGMA busy_timeout={settings.db_busy_timeout_ms}")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def init_db() -> None:
    from aurora import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    # SQLite's create_all does not add columns to an existing local MVP database.
    if settings.database_url.startswith("sqlite"):
        _add_sqlite_columns()
    _record_schema_version()
    _backfill_coordination_states()
    _backfill_legacy_flag_candidates()
    _invalidate_stale_flag_candidates()
    _invalidate_legacy_false_targets()


def _add_sqlite_columns() -> None:
    expected = {
        "attempt": {
            "parent_attempt_id": "TEXT",
            "codex_control_token_hash": "TEXT",
            "last_event_at": "DATETIME",
            "resume_count": "INTEGER DEFAULT 0",
            "blackboard_version": "INTEGER DEFAULT 0",
            "lease_generation": "INTEGER DEFAULT 0",
            "finalization_reason": "TEXT",
            "resume_manifest_artifact_id": "TEXT",
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
        "worker": {
            "parent_worker_id": "TEXT",
            "execution_kind": "TEXT DEFAULT 'primary'",
            "lease_generation": "INTEGER DEFAULT 0",
        },
        "intent": {"lease_generation": "INTEGER DEFAULT 0"},
        "projectruntimepolicy": {
            "multi_agent_exploration_enabled": "BOOLEAN DEFAULT 0",
            "max_parallel_explorers": "INTEGER DEFAULT 2",
            "max_reason_intents": "INTEGER DEFAULT 3",
            "max_pending_intents": "INTEGER DEFAULT 8",
        },
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
            "flag_prefixes": "JSON",
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
            "phase_started_at": "DATETIME",
            "phase_deadline_at": "DATETIME",
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
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_attempt_resume_manifest_artifact_id ON attempt (resume_manifest_artifact_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_challengegroupitem_phase_deadline_at ON challengegroupitem (phase_deadline_at)"))


def _record_schema_version() -> None:
    from aurora.models import SchemaVersion, now_utc

    with Session(engine) as session:
        version = session.get(SchemaVersion, "aurora")
        if version is None:
            version = SchemaVersion(version=SCHEMA_VERSION)
        else:
            version.version = SCHEMA_VERSION
            version.updated_at = now_utc()
        session.add(version)
        session.commit()


def _backfill_coordination_states() -> None:
    from aurora.models import Project, ProjectCoordinationState

    with Session(engine) as session:
        existing = set(session.exec(select(ProjectCoordinationState.project_id)).all())
        for project_id in session.exec(select(Project.id)).all():
            if project_id not in existing:
                session.add(ProjectCoordinationState(project_id=project_id))
        session.commit()


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


def _invalidate_stale_flag_candidates() -> None:
    """Repair stale validation state without fabricating platform rejections.

    Blacklist and readability rules evolve as false-positive prefixes are
    observed in real worker logs.  Old rows were already written as
    ``LOCAL_VERIFIED``, so they would otherwise keep appearing in submission
    context and candidate lists. Revalidate them at startup and downgrade
    stale rows to ``PROPOSED``. Also rehabilitate legacy local rejections;
    only platform and explicit manual decisions remain durable rejections.
    """
    from aurora.models import FlagCandidate, now_utc
    from aurora.services.flag_validator import FlagValidator
    from aurora.services.flag_prefix_config import flag_prefixes_for_project
    from aurora.services.flag_rejection import is_authoritative_flag_rejection

    validator = FlagValidator()
    with Session(engine) as session:
        candidates = session.exec(
            select(FlagCandidate).where(FlagCandidate.status.in_(["LOCAL_VERIFIED", "REJECTED"]))
        ).all()
        changed = False
        allowed_by_project: dict[str, tuple[str, ...]] = {}
        for candidate in candidates:
            if candidate.status == "REJECTED":
                if is_authoritative_flag_rejection(candidate):
                    continue
                candidate.status = "PROPOSED"
                candidate.updated_at = now_utc()
                session.add(candidate)
                changed = True
                continue
            if candidate.project_id not in allowed_by_project:
                allowed_by_project[candidate.project_id] = flag_prefixes_for_project(session, candidate.project_id)
            if validator.is_valid_flag_value(candidate.value, allowed_by_project[candidate.project_id]):
                continue
            candidate.status = "PROPOSED"
            candidate.provenance_kind = "UNVERIFIED"
            candidate.rejection_reason = "stale_validation_blacklist"
            candidate.updated_at = now_utc()
            session.add(candidate)
            changed = True
        if changed:
            session.commit()


def _invalidate_legacy_false_targets() -> None:
    from aurora.services.target_repair import invalidate_false_targets

    with Session(engine) as session:
        invalidate_false_targets(session)
