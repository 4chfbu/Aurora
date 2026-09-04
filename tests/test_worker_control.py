import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from aurora.api import create_app
from aurora.db import engine as app_engine
from aurora.models import Artifact, Attempt, Fact, ProjectCoordinationState, Worker, WorkerEvent
from aurora.services.artifact_store import ArtifactStore
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


def test_worker_control_write_request_ids_are_idempotent(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        worker = Worker(id="worker_idempotent", project_id="proj_idempotent", intent_id="intent_idempotent", status="RUNNING")
        attempt = Attempt(id="attempt_idempotent", project_id=worker.project_id, intent_id=worker.intent_id, worker_id=worker.id)
        evidence_file = tmp_path / "evidence.txt"
        evidence_file.write_text("observed", encoding="utf-8")
        artifact = Artifact(project_id=worker.project_id, path=str(evidence_file), sha256="hash", type="tool-output")
        session.add_all([worker, attempt, artifact])
        session.commit()

        service = WorkerControlService()
        first_fact = service.append_fact(
            session,
            worker=worker,
            attempt=attempt,
            statement="The target is reachable.",
            category="web",
            confidence=0.9,
            evidence_refs=[artifact.id],
            request_id="request-fact-0001",
        )
        repeated_fact = service.append_fact(
            session,
            worker=worker,
            attempt=attempt,
            statement="The target is reachable.",
            category="web",
            confidence=0.9,
            evidence_refs=[artifact.id],
            request_id="request-fact-0001",
        )
        first_checkpoint = service.save_checkpoint(
            session,
            worker=worker,
            attempt=attempt,
            summary="Target checked",
            completed_steps=["probe"],
            failed_routes=[],
            next_step="continue",
            artifact_refs=[artifact.id],
            request_id="request-checkpoint-0001",
        )
        repeated_checkpoint = service.save_checkpoint(
            session,
            worker=worker,
            attempt=attempt,
            summary="Target checked",
            completed_steps=["probe"],
            failed_routes=[],
            next_step="continue",
            artifact_refs=[artifact.id],
            request_id="request-checkpoint-0001",
        )
        write_events = session.exec(
            select(WorkerEvent).where(
                WorkerEvent.attempt_id == attempt.id,
                WorkerEvent.event_type.in_(["blackboard.fact_appended", "checkpoint.saved"]),
            )
        ).all()

    assert repeated_fact == {**first_fact, "deduplicated": True}
    assert repeated_checkpoint == {**first_checkpoint, "deduplicated": True}
    assert len(write_events) == 2
    assert attempt.blackboard_version == 2


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


def test_worker_control_registers_workspace_paths_as_project_artifacts(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    workspace_dir = tmp_path / "workspaces"
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    with Session(engine) as session:
        worker = Worker(id="worker_paths", project_id="proj_paths", intent_id="intent_paths", status="RUNNING")
        attempt = Attempt(id="attempt_paths", project_id=worker.project_id, intent_id=worker.intent_id, worker_id=worker.id)
        work_dir = workspace_dir / worker.project_id / worker.id / "work"
        work_dir.mkdir(parents=True)
        evidence_file = work_dir / "response.txt"
        evidence_file.write_text("HTTP 200 admin panel", encoding="utf-8")
        session.add_all([worker, attempt])
        session.commit()

        service = WorkerControlService(artifact_store=artifact_store, workspace_dir=workspace_dir)
        fact_result = service.append_fact(
            session,
            worker=worker,
            attempt=attempt,
            statement="The admin panel is reachable.",
            category="web",
            confidence=0.9,
            evidence_refs=["/workspace/work/response.txt"],
        )
        checkpoint_result = service.save_checkpoint(
            session,
            worker=worker,
            attempt=attempt,
            summary="Admin panel confirmed",
            completed_steps=["Fetched the panel"],
            failed_routes=[],
            next_step="Inspect authentication",
            artifact_refs=["work/response.txt"],
        )
        artifact = session.get(Artifact, fact_result["evidence_refs"][0])
        fact = session.get(Fact, fact_result["fact_id"])
        preview = service.read_artifact(session, worker=worker, artifact_id=artifact.id, max_bytes=8)

    assert artifact is not None
    assert artifact.project_id == worker.project_id
    assert artifact.source_attempt_id == attempt.id
    assert artifact.type == "worker-evidence"
    assert artifact.origin_kind == "worker_observation"
    assert fact is not None
    assert fact.evidence_refs == [artifact.id]
    assert checkpoint_result["artifact_refs"] == [artifact.id]
    assert preview["content"] == "HTTP 200"
    assert preview["truncated"] is True


@pytest.mark.parametrize(
    "reference",
    [
        "../worker_sibling/work/evidence.txt",
        "/workspace/../../worker_sibling/work/evidence.txt",
    ],
)
def test_worker_control_rejects_paths_outside_worker_workspace(tmp_path, reference) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    workspace_dir = tmp_path / "workspaces"
    with Session(engine) as session:
        worker = Worker(id="worker_current", project_id="proj_scope", intent_id="intent_scope", status="RUNNING")
        attempt = Attempt(id="attempt_scope", project_id=worker.project_id, intent_id=worker.intent_id, worker_id=worker.id)
        sibling_file = workspace_dir / worker.project_id / "worker_sibling" / "work" / "evidence.txt"
        sibling_file.parent.mkdir(parents=True)
        sibling_file.write_text("sibling secret", encoding="utf-8")
        session.add_all([worker, attempt])
        session.commit()

        service = WorkerControlService(
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            workspace_dir=workspace_dir,
        )
        with pytest.raises(ValueError, match="escapes the worker workspace"):
            service.append_fact(
                session,
                worker=worker,
                attempt=attempt,
                statement="unsafe",
                category="analysis",
                confidence=0.5,
                evidence_refs=[reference],
            )

    with Session(engine) as session:
        assert session.exec(select(Artifact)).all() == []


def test_worker_control_rejects_symlink_to_file_outside_workspace(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    workspace_dir = tmp_path / "workspaces"
    with Session(engine) as session:
        worker = Worker(id="worker_symlink", project_id="proj_scope", intent_id="intent_scope", status="RUNNING")
        attempt = Attempt(id="attempt_symlink", project_id=worker.project_id, intent_id=worker.intent_id, worker_id=worker.id)
        workspace = workspace_dir / worker.project_id / worker.id
        workspace.mkdir(parents=True)
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("host secret", encoding="utf-8")
        workspace.joinpath("escape.txt").symlink_to(outside_file)
        session.add_all([worker, attempt])
        session.commit()

        service = WorkerControlService(
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            workspace_dir=workspace_dir,
        )
        with pytest.raises(ValueError, match="escapes the worker workspace"):
            service.save_checkpoint(
                session,
                worker=worker,
                attempt=attempt,
                summary="unsafe",
                completed_steps=[],
                failed_routes=[],
                next_step="",
                artifact_refs=["escape.txt"],
            )


def test_worker_control_rejects_reading_foreign_artifact(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        worker = Worker(id="worker_reader", project_id="proj_reader", intent_id="intent_reader", status="RUNNING")
        foreign_file = tmp_path / "foreign.txt"
        foreign_file.write_text("foreign", encoding="utf-8")
        artifact = Artifact(project_id="proj_foreign", path=str(foreign_file), sha256="hash", size=7)
        session.add_all([worker, artifact])
        session.commit()

        with pytest.raises(ValueError, match="this project"):
            WorkerControlService().read_artifact(session, worker=worker, artifact_id=artifact.id)


def test_worker_artifact_endpoint_is_authenticated_scoped_and_bounded(tmp_path) -> None:
    token = "artifact-reader-secret"
    artifact_file = tmp_path / "large.txt"
    artifact_file.write_text("x" * 70_000, encoding="utf-8")
    with Session(app_engine) as session:
        worker = Worker(id="worker_api_reader", project_id="proj_api_reader", intent_id="intent_api_reader", status="RUNNING")
        attempt = Attempt(
            id="attempt_api_reader",
            project_id=worker.project_id,
            intent_id=worker.intent_id,
            worker_id=worker.id,
            codex_control_token_hash=hashlib.sha256(token.encode()).hexdigest(),
        )
        artifact = Artifact(
            project_id=worker.project_id,
            path=str(artifact_file),
            sha256=hashlib.sha256(artifact_file.read_bytes()).hexdigest(),
            size=artifact_file.stat().st_size,
        )
        foreign = Artifact(project_id="proj_foreign", path=str(artifact_file), sha256=artifact.sha256, size=artifact.size)
        session.add_all([worker, attempt, artifact, foreign])
        session.commit()
        worker_id = worker.id
        artifact_id = artifact.id
        foreign_id = foreign.id

    client = TestClient(create_app())
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get(
        f"/internal/workers/{worker_id}/artifacts/{artifact_id}?max_bytes=1000000",
        headers=headers,
    )
    denied = client.get(f"/internal/workers/{worker_id}/artifacts/{artifact_id}")
    foreign_response = client.get(
        f"/internal/workers/{worker_id}/artifacts/{foreign_id}",
        headers=headers,
    )

    assert response.status_code == 200
    assert len(response.json()["content"]) == 64_000
    assert response.json()["truncated"] is True
    assert denied.status_code == 403
    assert foreign_response.status_code == 404
