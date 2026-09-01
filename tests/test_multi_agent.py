from __future__ import annotations

from threading import Barrier, Lock

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aurora.api import create_app
from aurora.config import get_settings
from aurora.db import engine
from aurora.models import Intent, Project, ProjectCoordinationState, ProjectRuntimePolicy, WorkerEvent
from aurora.services.autorunner import AutoRunLimits, AutoRunnerService
from aurora.services.blackboard_repository import BlackboardRepository
from aurora.services.multi_agent import run_project_exploration_step
from aurora.services.project_coordination import ProjectCoordinationService
from aurora.services.project_reasoner import ProjectReasoner
from aurora.services.project_run_control import project_run_control
from aurora.services.scheduler import Scheduler


def test_multi_agent_policy_requires_global_and_project_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_MULTI_AGENT_EXPLORATION_ENABLED", "true")
    get_settings.cache_clear()
    client = TestClient(create_app())
    with client:
        disabled = client.post("/api/projects", json={"name": "serial", "goal": "serial"})
        enabled = client.post(
            "/api/projects",
            json={
                "name": "parallel",
                "goal": "parallel",
                "multi_agent_exploration_enabled": True,
                "max_parallel_explorers": 3,
            },
        )
        assert disabled.status_code == 200
        assert enabled.status_code == 200
        disabled_policy = client.get(f"/api/projects/{disabled.json()['id']}/runtime-policy").json()
        enabled_policy = client.get(f"/api/projects/{enabled.json()['id']}/runtime-policy").json()
        assert disabled_policy["multi_agent_exploration_enabled"] is False
        assert enabled_policy["multi_agent_exploration_enabled"] is True
        assert enabled_policy["max_parallel_explorers"] == 3


def test_parallel_explorers_claim_distinct_intents_and_overlap(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_MULTI_AGENT_EXPLORATION_ENABLED", "true")
    monkeypatch.setenv("AURORA_MULTI_AGENT_MAX_GLOBAL_WORKERS", "4")
    get_settings.cache_clear()
    client = TestClient(create_app())
    with client:
        project_id = client.post(
            "/api/projects",
            json={
                "name": "parallel-claims",
                "goal": "exercise two branches",
                "multi_agent_exploration_enabled": True,
                "max_parallel_explorers": 2,
            },
        ).json()["id"]

    with Session(engine) as session:
        for intent in session.exec(select(Intent).where(Intent.project_id == project_id)).all():
            session.delete(intent)
        session.add_all([
            Intent(project_id=project_id, objective="branch one", priority=2),
            Intent(project_id=project_id, objective="branch two", priority=1),
        ])
        session.commit()

    barrier = Barrier(2)
    lock = Lock()
    active = 0
    peak = 0
    claimed_intents: list[str] = []

    def fake_execute(worker_session: Session, *, project_id: str) -> dict:
        nonlocal active, peak
        claimed = Scheduler().claim_next(worker_session, project_id=project_id, lease_seconds=60)
        assert claimed is not None
        intent, worker = claimed
        with lock:
            claimed_intents.append(intent.id)
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=5)
        Scheduler().complete(worker_session, intent=intent, worker=worker)
        with lock:
            active -= 1
        return {"status": "completed", "intent_id": intent.id, "worker_id": worker.id}

    monkeypatch.setattr("aurora.services.multi_agent._run_one_demo_step_claimed", fake_execute)
    claim = project_run_control.acquire(project_id=project_id, owner="test")
    assert claim is not None
    try:
        with Session(engine) as session:
            result = run_project_exploration_step(session, project_id=project_id, run_id=claim.run_id)
    finally:
        project_run_control.release(project_id=project_id, run_id=claim.run_id)

    assert result["status"] == "multi_agent_batch"
    assert result["worker_count"] == 2
    assert peak == 2
    assert len(set(claimed_intents)) == 2


def test_multi_agent_step_rejects_unowned_run_id(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_MULTI_AGENT_EXPLORATION_ENABLED", "true")
    get_settings.cache_clear()
    with Session(engine) as session:
        project = Project(name="run-fence", goal="reject stale dispatch")
        session.add_all([
            project,
            ProjectRuntimePolicy(project_id=project.id, multi_agent_exploration_enabled=True),
            Intent(project_id=project.id, objective="must not run"),
        ])
        session.commit()

        result = run_project_exploration_step(session, project_id=project.id, run_id="fabricated")

    assert result == {"status": "busy", "message": "project_run_active"}


def test_graph_versions_fence_reason_passes() -> None:
    client = TestClient(create_app())
    with client:
        project_id = client.post("/api/projects", json={"name": "graph", "goal": "version graph"}).json()["id"]

    with Session(engine) as session:
        repository = BlackboardRepository()
        repository.upsert_fact(session, project_id=project_id, statement="first", confidence=0.8)
        state = session.exec(
            select(ProjectCoordinationState).where(ProjectCoordinationState.project_id == project_id)
        ).one()
        assert state.graph_version == 1
        service = ProjectCoordinationService()
        owner, version = service.claim_reason(session, project_id=project_id) or (None, None)
        assert owner and version == 1
        assert service.claim_reason(session, project_id=project_id) is None
        assert service.finish_reason(session, project_id=project_id, owner=owner, reasoned_version=version)
        assert service.claim_reason(session, project_id=project_id) is None

        repository.upsert_fact(session, project_id=project_id, statement="second", confidence=0.8)
        next_claim = service.claim_reason(session, project_id=project_id)
        assert next_claim is not None and next_claim[1] == 2


def test_reason_pass_creates_bounded_parallel_intents_once(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_MULTI_AGENT_EXPLORATION_ENABLED", "true")
    monkeypatch.setenv("AURORA_LLM_API_KEY", "test-key")
    monkeypatch.setenv("AURORA_MULTI_AGENT_MAX_PROJECT_WORKERS", "3")
    get_settings.cache_clear()
    client = TestClient(create_app())
    with client:
        project_id = client.post(
            "/api/projects",
            json={
                "name": "reason",
                "goal": "fan out",
                "multi_agent_exploration_enabled": True,
                "max_parallel_explorers": 3,
            },
        ).json()["id"]

    response = {
        "choices": [{
            "message": {
                "content": '{"intents": ['
                '{"objective":"branch 1","capabilities":["blackboard.query"],"depends_on_facts":[],"priority":4,"risk_level":"low"},'
                '{"objective":"branch 2","capabilities":["blackboard.query"],"depends_on_facts":[],"priority":3,"risk_level":"low"},'
                '{"objective":"branch 3","capabilities":["blackboard.query"],"depends_on_facts":[],"priority":2,"risk_level":"low"},'
                '{"objective":"branch 4","capabilities":["blackboard.query"],"depends_on_facts":[],"priority":1,"risk_level":"low"}'
                ']}'
            }
        }]
    }
    monkeypatch.setattr("aurora.services.project_reasoner.chat_completion", lambda **kwargs: response)
    with Session(engine) as session:
        BlackboardRepository().upsert_fact(session, project_id=project_id, statement="new graph evidence")
        first = ProjectReasoner().run(session, project_id=project_id)
        second = ProjectReasoner().run(session, project_id=project_id)

    assert first["status"] == "proposed"
    assert len(first["created_intent_ids"]) == 2
    assert first["details"]["open_intents"] == 1
    assert first["details"]["requested_intents"] == 2
    assert second["status"] == "unchanged"


def test_reason_fills_empty_model_plan_with_playbook_branch(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_MULTI_AGENT_EXPLORATION_ENABLED", "true")
    monkeypatch.setenv("AURORA_LLM_API_KEY", "test-key")
    get_settings.cache_clear()
    client = TestClient(create_app())
    with client:
        project_id = client.post(
            "/api/projects",
            json={
                "name": "fallback-fanout",
                "goal": "inspect an authorized web target",
                "challenge_type": "web",
                "multi_agent_exploration_enabled": True,
                "max_parallel_explorers": 2,
            },
        ).json()["id"]

    monkeypatch.setattr(
        "aurora.services.project_reasoner.chat_completion",
        lambda **kwargs: {"choices": [{"message": {"content": '{"intents": []}'}}]},
    )
    with Session(engine) as session:
        BlackboardRepository().upsert_fact(
            session,
            project_id=project_id,
            statement="The authorized target exposes HTTP.",
        )
        result = ProjectReasoner().run(session, project_id=project_id)
        pending = session.exec(
            select(Intent).where(Intent.project_id == project_id, Intent.status == "PENDING")
        ).all()

    assert result["status"] == "proposed"
    assert result["details"]["model_created"] == 0
    assert result["details"]["fallback_created"] == 1
    assert len(pending) == 2


def test_real_reason_worker_blackboard_loop_runs_parallel_batches(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_MULTI_AGENT_EXPLORATION_ENABLED", "true")
    monkeypatch.setenv("AURORA_LLM_API_KEY", "test-key")
    get_settings.cache_clear()
    client = TestClient(create_app())
    with client:
        project_id = client.post(
            "/api/projects",
            json={
                "name": "closed-loop",
                "goal": "exercise a reason-worker-blackboard loop",
                "multi_agent_exploration_enabled": True,
                "max_parallel_explorers": 2,
            },
        ).json()["id"]

    calls = 0

    def reason_response(**kwargs):
        nonlocal calls
        calls += 1
        return {
            "choices": [{
                "message": {
                    "content": (
                        '{"intents":[{"objective":"independent branch '
                        f'{calls}","capabilities":["blackboard.query"],'
                        '"depends_on_facts":[],"priority":4,"risk_level":"low"}]}'
                    )
                }
            }]
        }

    monkeypatch.setattr("aurora.services.project_reasoner.chat_completion", reason_response)
    with Session(engine) as session:
        BlackboardRepository().upsert_fact(
            session,
            project_id=project_id,
            statement="Initial shared evidence is available.",
        )
        result = AutoRunnerService().run_until_stop(
            session,
            project_id=project_id,
            limits=AutoRunLimits(max_iterations=2, no_progress_limit=0),
        )
        batches = session.exec(
            select(WorkerEvent)
            .where(
                WorkerEvent.project_id == project_id,
                WorkerEvent.event_type == "multi_agent.batch_started",
            )
            .order_by(WorkerEvent.created_at)
        ).all()
        reason_events = session.exec(
            select(WorkerEvent).where(
                WorkerEvent.project_id == project_id,
                WorkerEvent.event_type == "reason.completed",
            )
        ).all()
        workers = session.exec(
            select(WorkerEvent).where(
                WorkerEvent.project_id == project_id,
                WorkerEvent.event_type == "worker.started",
            )
        ).all()

    assert result.stop_reason == "max_iterations"
    assert [event.payload_json["worker_count"] for event in batches] == [2, 2]
    assert len(workers) == 4
    assert len(reason_events) == 2
    assert all(event.payload_json["status"] == "proposed" for event in reason_events)
