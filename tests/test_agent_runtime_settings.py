from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aurora.api import create_app
from aurora.db import engine
from aurora.models import ChallengeGroup, Intent
from aurora.services.multi_agent import run_project_exploration_step
from aurora.services.project_run_control import project_run_control


def test_agent_settings_control_global_project_and_group_policies(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_MULTI_AGENT_EXPLORATION_ENABLED", "false")
    client = TestClient(create_app())
    global_payload = {
        "multi_agent_exploration_enabled": True,
        "max_global_workers": 6,
        "default_max_project_workers": 4,
        "default_max_reason_intents": 5,
        "default_max_pending_intents": 12,
        "subagents_enabled": True,
        "default_max_subagents_per_worker": 4,
        "default_max_subagents_concurrent": 3,
        "max_challenge_group_concurrent": 5,
    }

    with client:
        saved_global = client.put("/api/settings/agents", json=global_payload)
        assert saved_global.status_code == 200
        assert client.get("/api/settings/agents").json()["max_global_workers"] == 6

        project = client.post(
            "/api/projects",
            json={
                "name": "runtime-control",
                "goal": "verify runtime controls",
                "multi_agent_exploration_enabled": True,
                "subagents_enabled": True,
            },
        ).json()
        policy = client.get(f"/api/projects/{project['id']}/runtime-policy").json()
        assert policy["max_parallel_explorers"] == 4
        assert policy["max_reason_intents"] == 5
        assert policy["max_pending_intents"] == 12
        assert policy["subagents_enabled"] is True

        policy_payload = {
            "subagents_enabled": False,
            "max_subagents_per_worker": 2,
            "max_subagents_concurrent": 1,
            "multi_agent_exploration_enabled": True,
            "max_parallel_explorers": 3,
            "max_reason_intents": 2,
            "max_pending_intents": 7,
        }
        saved_policy = client.put(f"/api/projects/{project['id']}/runtime-policy", json=policy_payload)
        assert saved_policy.status_code == 200
        assert saved_policy.json()["max_parallel_explorers"] == 3
        assert client.get(f"/api/projects/{project['id']}/runtime-policy").json()["max_pending_intents"] == 7

        with Session(engine) as session:
            group = ChallengeGroup(name="runtime-group", max_concurrent=1)
            session.add(group)
            session.commit()
            session.refresh(group)
            group_id = group.id
        saved_group = client.put(f"/api/challenge-groups/{group_id}/runtime-policy", json={"max_concurrent": 4})
        assert saved_group.status_code == 200
        assert saved_group.json()["max_concurrent"] == 4


def test_agent_settings_reject_inconsistent_limits() -> None:
    client = TestClient(create_app())
    with client:
        current = client.get("/api/settings/agents").json()
        current["default_max_project_workers"] = current["max_global_workers"] + 1
        response = client.put("/api/settings/agents", json=current)
        assert response.status_code == 422

        project = client.post("/api/projects", json={"name": "invalid-policy", "goal": "validate"}).json()
        policy = client.get(f"/api/projects/{project['id']}/runtime-policy").json()
        policy["max_subagents_concurrent"] = policy["max_subagents_per_worker"] + 1
        response = client.put(f"/api/projects/{project['id']}/runtime-policy", json=policy)
        assert response.status_code == 422


def test_updated_global_worker_limit_applies_to_next_dispatch(monkeypatch) -> None:
    client = TestClient(create_app())
    with client:
        settings = client.get("/api/settings/agents").json()
        settings.update({
            "multi_agent_exploration_enabled": True,
            "max_global_workers": 4,
            "default_max_project_workers": 3,
        })
        assert client.put("/api/settings/agents", json=settings).status_code == 200
        project_id = client.post(
            "/api/projects",
            json={
                "name": "dynamic-dispatch",
                "goal": "respect the latest global cap",
                "multi_agent_exploration_enabled": True,
                "max_parallel_explorers": 3,
            },
        ).json()["id"]

        with Session(engine) as session:
            for intent in session.exec(select(Intent).where(Intent.project_id == project_id)).all():
                session.delete(intent)
            session.add_all([
                Intent(project_id=project_id, objective="branch one"),
                Intent(project_id=project_id, objective="branch two"),
                Intent(project_id=project_id, objective="branch three"),
            ])
            session.commit()
            settings["max_global_workers"] = 1
            settings["default_max_project_workers"] = 1
            assert client.put("/api/settings/agents", json=settings).status_code == 200
            monkeypatch.setattr(
                "aurora.services.multi_agent._execute_one",
                lambda current_project_id: {"status": "completed", "project_id": current_project_id},
            )
            claim = project_run_control.acquire(project_id=project_id, owner="dynamic-test")
            assert claim is not None
            try:
                result = run_project_exploration_step(session, project_id=project_id, run_id=claim.run_id)
            finally:
                project_run_control.release(project_id=project_id, run_id=claim.run_id)

    assert result["worker_count"] == 1
