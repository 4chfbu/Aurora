from __future__ import annotations

import json
from threading import Barrier, Event, Lock

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aurora.api import create_app
from aurora.config import get_settings
from aurora.db import engine
from aurora.models import ChallengeGroup, ChallengeGroupItem, Intent, Project, ProjectCoordinationState, ProjectRuntimePolicy, WorkerEvent
from aurora.services.autorunner import AutoRunLimits, AutoRunnerService
from aurora.services.blackboard_repository import BlackboardRepository
from aurora.services.multi_agent import run_project_exploration_step
from aurora.services.project_coordination import ProjectCoordinationService
from aurora.services.project_reasoner import ProjectReasoner
from aurora.services.project_run_control import project_run_control
from aurora.services.scheduler import Scheduler
from aurora.services.artifact_store import ArtifactStore
from aurora.services.demo import _select_parent_attempt


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


def test_phase_gate_forces_serial_execution_despite_multi_agent_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_MULTI_AGENT_EXPLORATION_ENABLED", "true")
    get_settings.cache_clear()
    serial_calls: list[str] = []
    monkeypatch.setattr(
        "aurora.services.multi_agent.run_one_demo_step",
        lambda session, project_id, run_id: serial_calls.append(project_id) or {"status": "serial"},
    )
    monkeypatch.setattr(
        "aurora.services.multi_agent._execute_one",
        lambda project_id: (_ for _ in ()).throw(AssertionError("phase 1 must not start peer explorers")),
    )
    with Session(engine) as session:
        project = Project(name="serial-phase", goal="solve directly")
        session.add(project)
        session.commit()
        session.add_all([
            ProjectRuntimePolicy(
                project_id=project.id,
                multi_agent_exploration_enabled=True,
                max_parallel_explorers=2,
            ),
            Intent(project_id=project.id, objective="route one"),
            Intent(project_id=project.id, objective="route two"),
        ])
        session.commit()
        claim = project_run_control.acquire(project_id=project.id, owner="test")
        assert claim is not None
        try:
            result = run_project_exploration_step(
                session,
                project_id=project.id,
                run_id=claim.run_id,
                allow_multi_agent=False,
            )
        finally:
            project_run_control.release(project_id=project.id, run_id=claim.run_id)

    assert result["status"] == "serial"
    assert serial_calls == [project.id]
    get_settings.cache_clear()


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


def test_parallel_batch_stops_sibling_after_project_becomes_terminal(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_MULTI_AGENT_EXPLORATION_ENABLED", "true")
    monkeypatch.setenv("AURORA_MULTI_AGENT_MAX_GLOBAL_WORKERS", "4")
    get_settings.cache_clear()
    with Session(engine) as session:
        project = Project(name="short-circuit", goal="stop sibling")
        session.add_all([
            project,
            ProjectRuntimePolicy(
                project_id=project.id,
                multi_agent_exploration_enabled=True,
                max_parallel_explorers=2,
            ),
            Intent(project_id=project.id, objective="winning branch", priority=2),
            Intent(project_id=project.id, objective="slow branch", priority=1),
        ])
        session.commit()
        project_id = project.id

    barrier = Barrier(2)
    stop_requested = Event()
    lock = Lock()
    calls = 0

    def fake_execute(worker_session: Session, *, project_id: str) -> dict:
        nonlocal calls
        with lock:
            calls += 1
            call = calls
        barrier.wait(timeout=5)
        if call == 1:
            project = worker_session.get(Project, project_id)
            assert project is not None
            project.status = "FLAG_READY"
            worker_session.add(project)
            worker_session.commit()
            assert stop_requested.wait(timeout=5)
            return {"status": "completed"}
        assert stop_requested.wait(timeout=5)
        return {"status": "cancelled"}

    monkeypatch.setattr("aurora.services.multi_agent._run_one_demo_step_claimed", fake_execute)
    monkeypatch.setattr(
        "aurora.services.multi_agent.stop_project_containers",
        lambda project_id: (stop_requested.set() or {"stopped": ["worker-container"], "errors": []}),
    )
    claim = project_run_control.acquire(project_id=project_id, owner="test")
    assert claim is not None
    try:
        with Session(engine) as session:
            result = run_project_exploration_step(session, project_id=project_id, run_id=claim.run_id)
            event = session.exec(
                select(WorkerEvent).where(
                    WorkerEvent.project_id == project_id,
                    WorkerEvent.event_type == "multi_agent.batch_short_circuited",
                )
            ).one()
    finally:
        project_run_control.release(project_id=project_id, run_id=claim.run_id)

    assert result["worker_count"] == 2
    assert event.payload_json["project_status"] == "FLAG_READY"
    assert event.payload_json["stopped_containers"] == ["worker-container"]


def test_group_projects_receive_a_fair_share_of_global_workers(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_MULTI_AGENT_EXPLORATION_ENABLED", "true")
    monkeypatch.setenv("AURORA_MULTI_AGENT_MAX_GLOBAL_WORKERS", "4")
    get_settings.cache_clear()
    with Session(engine) as session:
        group = ChallengeGroup(name="fair", max_concurrent=3)
        projects = [Project(name=f"fair-{index}", goal="share capacity") for index in range(3)]
        session.add(group)
        session.add_all(projects)
        session.flush()
        session.add_all([
            ChallengeGroupItem(group_id=group.id, project_id=project.id, position=index)
            for index, project in enumerate(projects, start=1)
        ])
        session.add(ProjectRuntimePolicy(
            project_id=projects[0].id,
            multi_agent_exploration_enabled=True,
            max_parallel_explorers=2,
        ))
        session.add_all([
            Intent(project_id=projects[0].id, objective="branch one", priority=2),
            Intent(project_id=projects[0].id, objective="branch two", priority=1),
        ])
        session.commit()
        project_id = projects[0].id

    monkeypatch.setattr(
        "aurora.services.multi_agent._run_one_demo_step_claimed",
        lambda worker_session, project_id: {"status": "completed"},
    )
    claim = project_run_control.acquire(project_id=project_id, owner="test")
    assert claim is not None
    try:
        with Session(engine) as session:
            result = run_project_exploration_step(session, project_id=project_id, run_id=claim.run_id)
    finally:
        project_run_control.release(project_id=project_id, run_id=claim.run_id)

    assert result["worker_count"] == 2


def test_group_fair_share_uses_priority_dispatched_items(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_MULTI_AGENT_EXPLORATION_ENABLED", "true")
    monkeypatch.setenv("AURORA_MULTI_AGENT_MAX_GLOBAL_WORKERS", "4")
    get_settings.cache_clear()
    with Session(engine) as session:
        group = ChallengeGroup(name="priority-fair", max_concurrent=3)
        projects = [Project(name=f"priority-{position}", goal="share capacity") for position in range(1, 7)]
        session.add(group)
        session.add_all(projects)
        session.flush()
        items = [
            ChallengeGroupItem(
                group_id=group.id,
                project_id=project.id,
                position=position,
                status="RUNNING" if position in {4, 5, 6} else "PENDING",
                fused_status="RUNNING" if position in {4, 5, 6} else "PENDING",
            )
            for position, project in enumerate(projects, start=1)
        ]
        session.add_all(items)
        selected = projects[4]
        session.add(ProjectRuntimePolicy(
            project_id=selected.id,
            multi_agent_exploration_enabled=True,
            max_parallel_explorers=2,
        ))
        session.add(Intent(project_id=selected.id, objective="dispatched branch", priority=1))
        session.commit()
        project_id = selected.id

    monkeypatch.setattr(
        "aurora.services.multi_agent._run_one_demo_step_claimed",
        lambda worker_session, project_id: {"status": "completed"},
    )
    claim = project_run_control.acquire(project_id=project_id, owner="test")
    assert claim is not None
    try:
        with Session(engine) as session:
            result = run_project_exploration_step(session, project_id=project_id, run_id=claim.run_id)
    finally:
        project_run_control.release(project_id=project_id, run_id=claim.run_id)

    assert result["status"] == "multi_agent_batch"
    assert result["worker_count"] == 1


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


def test_reasoner_prioritizes_evidenced_continuation_and_resumes_its_branch(tmp_path, monkeypatch) -> None:
    from aurora.models import Attempt, AttemptCheckpoint, Fact

    monkeypatch.setenv("AURORA_LLM_API_KEY", "")
    get_settings.cache_clear()
    with Session(engine) as session:
        project = Project(name="continuation", goal="read protected file", challenge_type="web")
        policy = ProjectRuntimePolicy(project_id=project.id, multi_agent_exploration_enabled=True, max_parallel_explorers=2)
        branch = Intent(project_id=project.id, objective="establish file read", status="COMPLETED")
        attempt = Attempt(project_id=project.id, intent_id=branch.id, worker_id="worker_breakthrough", status="PARTIAL", codex_thread_id="thread_breakthrough", resume_manifest_artifact_id="manifest_breakthrough")
        session.add_all([project, policy, branch, attempt])
        session.commit()
        evidence = ArtifactStore(tmp_path).write_text(session, project_id=project.id, content="/challenge", summary="directory listing", origin_kind="target_observation")
        fact = Fact(project_id=project.id, statement="File read reproduced", category="vulnerability_confirmed", confidence=0.95, evidence_refs=[evidence.id], source_attempt_id=attempt.id)
        checkpoint = AttemptCheckpoint(project_id=project.id, intent_id=branch.id, attempt_id=attempt.id, summary="File read established", status="PARTIAL", next_steps=["Fix JSON escaping and read the protected file"], artifact_refs=[evidence.id], fact_refs=[fact.id])
        session.add_all([fact, checkpoint])
        session.commit()
        created = ProjectReasoner()._reason(session, project_id=project.id, policy=policy, facts=[fact], phase=2)
        pending = [session.get(Intent, intent_id) for intent_id in created]
        assert len(pending) == 2
        continuation = pending[0]
        assert "Fix JSON escaping" in continuation.objective
        assert continuation.priority > pending[1].priority
        assert continuation.dependency_fact_ids == [fact.id]
        assert continuation.parent_intent_id == branch.id
        assert _select_parent_attempt(session, project_id=project.id, intent=continuation).id == attempt.id
        fact.evidence_refs = []
        session.add(fact)
        session.commit()
        assert ProjectReasoner._continuation_candidates(session, facts=[fact], checkpoints=[checkpoint]) == []


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
                '{"objective":"branch 1","depends_on_facts":[],"priority":4,"risk_level":"low"},'
                '{"objective":"branch 2","capabilities":["not.available"],"depends_on_facts":[],"priority":3,"risk_level":"low"},'
                '{"objective":"branch 3","capabilities":["blackboard.query"],"depends_on_facts":[],"priority":2,"risk_level":"low"},'
                '{"objective":"branch 4","capabilities":["blackboard.query"],"depends_on_facts":[],"priority":1,"risk_level":"low"}'
                ']}'
            }
        }]
    }
    prompts: list[dict] = []

    def reason_response(**kwargs):
        prompts.append(json.loads(kwargs["messages"][0]["content"]))
        return response

    monkeypatch.setattr("aurora.services.project_reasoner.chat_completion", reason_response)
    with Session(engine) as session:
        bootstrap = session.exec(
            select(Intent).where(Intent.project_id == project_id).order_by(Intent.created_at)
        ).first()
        assert bootstrap is not None
        bootstrap.status = "COMPLETED"
        session.add(bootstrap)
        session.commit()
        BlackboardRepository().upsert_fact(session, project_id=project_id, statement="new graph evidence")
        first = ProjectReasoner().run(session, project_id=project_id, phase=2)
        second = ProjectReasoner().run(session, project_id=project_id, phase=2)
        created = [session.get(Intent, intent_id) for intent_id in first["created_intent_ids"]]

    assert first["status"] == "proposed"
    assert len(first["created_intent_ids"]) == 3
    assert first["details"]["open_intents"] == 0
    assert first["details"]["requested_intents"] == 3
    assert first["details"]["current_phase"] == 2
    assert all(intent is not None and intent.budget["phase"] == 2 for intent in created)
    assert all(intent is not None and intent.capability_tags for intent in created)
    assert "blackboard.query" in prompts[0]["allowed_capabilities"]
    assert second["status"] == "unchanged"


def test_reason_waits_for_bootstrap_despite_import_fact(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_MULTI_AGENT_EXPLORATION_ENABLED", "true")
    monkeypatch.setenv("AURORA_LLM_API_KEY", "test-key")
    get_settings.cache_clear()
    client = TestClient(create_app())
    with client:
        project_id = client.post(
            "/api/projects",
            json={
                "name": "bootstrap-fence",
                "goal": "do not fan out before bootstrap",
                "multi_agent_exploration_enabled": True,
            },
        ).json()["id"]

    monkeypatch.setattr(
        "aurora.services.project_reasoner.chat_completion",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("planner must not run before bootstrap")),
    )
    with Session(engine) as session:
        BlackboardRepository().upsert_fact(
            session,
            project_id=project_id,
            statement="Imported challenge metadata is available.",
            category="import",
        )
        result = ProjectReasoner().run(session, project_id=project_id)

    assert result == {"status": "waiting_for_bootstrap", "created_intent_ids": []}


def test_reason_respects_valid_empty_model_plan_as_noop(monkeypatch) -> None:
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
        bootstrap = session.exec(
            select(Intent).where(Intent.project_id == project_id).order_by(Intent.created_at)
        ).first()
        assert bootstrap is not None
        bootstrap.status = "COMPLETED"
        session.add(bootstrap)
        session.commit()
        BlackboardRepository().upsert_fact(
            session,
            project_id=project_id,
            statement="The authorized target exposes HTTP.",
        )
        result = ProjectReasoner().run(session, project_id=project_id)
        pending = session.exec(
            select(Intent).where(Intent.project_id == project_id, Intent.status == "PENDING")
        ).all()

    assert result["status"] == "noop"
    assert result["details"]["model_created"] == 0
    assert result["details"]["fallback_created"] == 0
    assert len(pending) == 0


def test_reason_records_invalid_candidate_and_uses_fallback(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_MULTI_AGENT_EXPLORATION_ENABLED", "true")
    monkeypatch.setenv("AURORA_LLM_API_KEY", "test-key")
    get_settings.cache_clear()
    client = TestClient(create_app())
    with client:
        project_id = client.post(
            "/api/projects",
            json={
                "name": "invalid-candidate",
                "goal": "inspect an authorized web target",
                "challenge_type": "web",
                "multi_agent_exploration_enabled": True,
                "max_parallel_explorers": 2,
            },
        ).json()["id"]

    monkeypatch.setattr(
        "aurora.services.project_reasoner.chat_completion",
        lambda **kwargs: {"choices": [{"message": {"content": '{"intents":[42]}'}}]},
    )
    with Session(engine) as session:
        bootstrap = session.exec(
            select(Intent).where(Intent.project_id == project_id).order_by(Intent.created_at)
        ).first()
        assert bootstrap is not None
        bootstrap.status = "COMPLETED"
        session.add(bootstrap)
        session.commit()
        BlackboardRepository().upsert_fact(session, project_id=project_id, statement="HTTP is reachable.")
        result = ProjectReasoner().run(session, project_id=project_id)

    assert result["status"] == "proposed"
    assert result["details"]["fallback_created"] == 2
    assert result["details"]["model_candidate_rejections"] == [
        {"index": 0, "reason": "candidate_not_object"}
    ]


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
    assert [event.payload_json["worker_count"] for event in batches] == [1, 2]
    assert len(workers) == 3
    assert len(reason_events) == 1
    assert all(event.payload_json["status"] == "proposed" for event in reason_events)
