from __future__ import annotations

from threading import Barrier, Lock

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aurora.api import create_app
from aurora.config import get_settings
from aurora.db import engine
from aurora.models import Intent, ProjectCoordinationState, ProjectRuntimePolicy
from aurora.services.blackboard_repository import BlackboardRepository
from aurora.services.multi_agent import run_project_exploration_step
from aurora.services.project_coordination import ProjectCoordinationService
from aurora.services.project_reasoner import ProjectReasoner
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
    with Session(engine) as session:
        result = run_project_exploration_step(session, project_id=project_id, run_id="run_test")

    assert result["status"] == "multi_agent_batch"
    assert result["worker_count"] == 2
    assert peak == 2
    assert len(set(claimed_intents)) == 2


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
    get_settings.cache_clear()
    client = TestClient(create_app())
    with client:
        project_id = client.post(
            "/api/projects",
            json={"name": "reason", "goal": "fan out", "multi_agent_exploration_enabled": True},
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
    assert len(first["created_intent_ids"]) == 3
    assert second["status"] == "unchanged"
