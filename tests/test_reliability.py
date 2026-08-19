from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

from sqlmodel import Session, select

from aurora.db import engine
from aurora.models import Attempt, AttemptCheckpoint, FlagCandidate, Intent, Project, Worker, WorkerEvent, now_utc
from aurora.services.reliability import ReliabilityService
from aurora.services.scheduler import Scheduler


def test_reliability_report_exposes_checkpoint_and_stale_worker_gates() -> None:
    project_id = "proj_reliability"
    with Session(engine) as session:
        project = Project(id=project_id, name="reliability", goal="test")
        intent = Intent(id="intent_reliability", project_id=project_id, objective="test", status="RUNNING", lease_owner="worker_reliability", lease_generation=2, lease_expires_at=now_utc() + timedelta(minutes=5))
        worker = Worker(id="worker_reliability", project_id=project_id, intent_id=intent.id, status="RUNNING", lease_generation=2)
        attempt = Attempt(id="attempt_reliability", project_id=project_id, intent_id=intent.id, worker_id=worker.id, status="PARTIAL")
        session.add_all([project, intent, worker, attempt])
        session.add(FlagCandidate(
            project_id=project_id,
            value="flag{verified}",
            value_hash="verified-hash",
            status="LOCAL_VERIFIED",
            provenance_kind="DERIVED_REPLAY",
            verification_artifact_ref="artifact_verify",
        ))
        session.commit()

        before = ReliabilityService().project_report(session, project_id=project_id)
        session.add(AttemptCheckpoint(project_id=project_id, intent_id=intent.id, worker_id=worker.id, attempt_id=attempt.id, status="PARTIAL", summary="saved"))
        session.commit()
        after = ReliabilityService().project_report(session, project_id=project_id)

    assert before["gates"]["all_terminal_attempts_checkpointed"] is False
    assert before["gates"]["no_stale_workers"] is True
    assert before["verification"]["derived_candidates"] == 1
    assert before["verification"]["derived_verification_coverage"] == 1.0
    assert after["attempts"]["checkpoint_coverage"] == 1.0


def test_scheduler_reconciles_orphan_and_creates_checkpoint(monkeypatch) -> None:
    monkeypatch.setattr("aurora.services.scheduler.stop_worker_containers", lambda worker_id: {"stopped": [], "errors": []})
    project_id = "proj_orphan"
    with Session(engine) as session:
        project = Project(id=project_id, name="orphan", goal="recover")
        intent = Intent(id="intent_orphan", project_id=project_id, objective="recover", status="RUNNING", lease_owner="worker_orphan", lease_generation=2, lease_expires_at=now_utc() + timedelta(minutes=5))
        worker = Worker(id="worker_orphan", project_id=project_id, intent_id=intent.id, status="RUNNING", lease_generation=1)
        attempt = Attempt(id="attempt_orphan", project_id=project_id, intent_id=intent.id, worker_id=worker.id, lease_generation=1)
        session.add_all([project, intent, worker, attempt])
        session.commit()

        assert Scheduler().reconcile_orphans(session) == 1
        session.refresh(worker)
        session.refresh(attempt)
        checkpoint = session.exec(select(AttemptCheckpoint).where(AttemptCheckpoint.attempt_id == attempt.id)).first()
        events = session.exec(select(WorkerEvent).where(WorkerEvent.worker_id == worker.id)).all()

    assert worker.status == "TIMEOUT"
    assert attempt.status == "TIMEOUT"
    assert attempt.finalization_reason == "orphan_reconciled"
    assert checkpoint is not None
    assert any(event.event_type == "worker.reconciled" for event in events)


def test_scheduler_claims_an_intent_only_once_under_concurrency() -> None:
    project_id = "proj_atomic_claim"
    with Session(engine) as session:
        session.add(Project(id=project_id, name="atomic", goal="claim once"))
        session.add(Intent(id="intent_atomic_claim", project_id=project_id, objective="claim once"))
        session.commit()

    def claim() -> bool:
        with Session(engine) as session:
            return Scheduler().claim_next(session, project_id=project_id) is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))

    with Session(engine) as session:
        workers = session.exec(select(Worker).where(Worker.project_id == project_id)).all()

    assert sorted(results) == [False, True]
    assert len(workers) == 1
