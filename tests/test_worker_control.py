import hashlib

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from aurora.models import Artifact, Attempt, Fact, ProjectCoordinationState, Worker, WorkerEvent
from aurora.services.worker_control import WorkerControlService


def test_worker_control_is_scoped_and_versions_live_updates(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    token = "worker-secret"
    with Session(engine) as session:
        worker = Worker(id="worker_control", project_id="proj_control", intent_id="intent_control", status="RUNNING")
        attempt = Attempt(id="attempt_control", project_id=worker.project_id, intent_id=worker.intent_id, worker_id=worker.id, codex_control_token_hash=hashlib.sha256(token.encode()).hexdigest())
        evidence_file = tmp_path / "evidence.txt"
        evidence_file.write_text("observed", encoding="utf-8")
        artifact = Artifact(project_id=worker.project_id, path=str(evidence_file), sha256="hash", type="tool-output")
        session.add_all([worker, attempt, artifact])
        session.commit()

        service = WorkerControlService()
        authenticated_worker, authenticated_attempt = service.authenticate(session, worker_id=worker.id, token=token)
        result = service.append_fact(session, worker=authenticated_worker, attempt=authenticated_attempt, statement="The target exposes a login form.", category="web", confidence=0.9, evidence_refs=[artifact.id])
        checkpoint = service.save_checkpoint(session, worker=authenticated_worker, attempt=authenticated_attempt, summary="Login route confirmed", completed_steps=["Fetched landing page"], failed_routes=["Default credentials"], next_step="Inspect the session cookie", artifact_refs=[artifact.id])
        board = service.query(session, worker=authenticated_worker, attempt=authenticated_attempt)
        facts = session.exec(select(Fact).where(Fact.project_id == worker.project_id)).all()
        coordination = session.exec(
            select(ProjectCoordinationState).where(ProjectCoordinationState.project_id == worker.project_id)
        ).one()
        events = session.exec(select(WorkerEvent).where(WorkerEvent.attempt_id == attempt.id)).all()

        with pytest.raises(PermissionError):
            service.authenticate(session, worker_id=worker.id, token="wrong")

    assert result["version"] == 1
    assert checkpoint["version"] == 2
    assert coordination.graph_version == 2
    assert len(facts) == 1
    assert board["version"] == 2
    assert board["live_checkpoints"][0]["next_step"] == "Inspect the session cookie"
    assert {event.event_type for event in events} >= {"blackboard.fact_appended", "checkpoint.saved"}


def test_worker_control_rejects_foreign_evidence(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        worker = Worker(id="worker_scope", project_id="proj_one", intent_id="intent_one", status="RUNNING")
        attempt = Attempt(project_id=worker.project_id, intent_id=worker.intent_id, worker_id=worker.id)
        artifact = Artifact(project_id="proj_two", path=str(tmp_path / "foreign"), sha256="hash")
        session.add_all([worker, attempt, artifact])
        session.commit()

        with pytest.raises(ValueError, match="this project"):
            WorkerControlService().append_fact(session, worker=worker, attempt=attempt, statement="foreign", category="analysis", confidence=0.5, evidence_refs=[artifact.id])
