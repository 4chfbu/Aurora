import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

os.environ["AURORA_DB_URL"] = "sqlite:////tmp/aurora_test.db"
os.environ["AURORA_ARTIFACT_DIR"] = "/tmp/aurora_test_artifacts"

db_path = Path("/tmp/aurora_test.db")
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient  # noqa: E402

from aurora.api import create_app  # noqa: E402
from aurora.db import engine  # noqa: E402
from aurora.models import Intent, Project, WorkerEvent, now_utc  # noqa: E402
from aurora.services.autorunner import AutoRunLimits, AutoRunnerService  # noqa: E402
from aurora.services.project_run_control import project_run_control  # noqa: E402
from sqlmodel import Session, select  # noqa: E402


def test_autorun_stops_when_candidate_is_ready_for_final_validation() -> None:
    client = TestClient(create_app())
    with client:
        project = client.post(
            "/api/projects",
            json={"name": "autorun-flag", "goal": "自动执行并拿到 flag", "allowed_hosts": ["127.0.0.1"]},
        ).json()
        project_id = project["id"]
        intent = client.post(
            f"/api/projects/{project_id}/intents",
            json={
                "objective": "输出 flag{autorun_ok}",
                "capability_tags": ["sandbox.exec"],
                "priority": 10,
                "risk_level": "low",
                "tool_request": {"command": "printf 'flag{autorun_ok}'", "cwd": ".", "timeout_seconds": 5},
            },
        )
        assert intent.status_code == 200
        result = client.post(
            f"/api/projects/{project_id}/autorun/start",
            json={"max_iterations": 5, "max_minutes": 5, "no_progress_limit": 2, "stop_on_observer_escalate": True},
        )
        assert result.status_code == 200
        body = result.json()
        assert body["autorun"]["status"] == "candidate_ready"
        assert body["summary"]["project"]["status"] == "FLAG_READY"
        assert body["summary"]["findings"][0]["title"] == "Candidate flag: flag{autorun_ok}"


def test_autorun_stops_on_observer_escalate() -> None:
    client = TestClient(create_app())
    with client:
        project = client.post(
            "/api/projects",
            json={"name": "autorun-escalate", "goal": "触发观察器停止", "allowed_hosts": ["127.0.0.1"]},
        ).json()
        project_id = project["id"]
        # capability.request is unsupported and records a policy deny, which
        # Observer escalates on to stop the autorun.
        denied = client.post(
            f"/api/projects/{project_id}/tools/capability.request/execute",
            json={"request": {"capability": "target.url"}},
        )
        assert denied.status_code == 200
        result = client.post(f"/api/projects/{project_id}/autorun/start", json={"max_iterations": 5})
        assert result.status_code == 200
        body = result.json()
        assert body["autorun"]["status"] == "blocked"
        assert body["autorun"]["stop_reason"] == "observer_escalate"


def test_autorun_zero_no_progress_limit_disables_duplicate_blocking() -> None:
    client = TestClient(create_app())
    with client:
        project = client.post(
            "/api/projects",
            json={"name": "autorun-unlimited-duplicates", "goal": "允许重复路由继续执行", "allowed_hosts": ["127.0.0.1"]},
        ).json()
        project_id = project["id"]
        payload = {"request": {"command": "printf 'repeat\\n'", "cwd": ".", "timeout_seconds": 5}}
        assert client.post(f"/api/projects/{project_id}/tools/sandbox.exec/execute", json=payload).status_code == 200
        assert client.post(f"/api/projects/{project_id}/tools/sandbox.exec/execute", json=payload).status_code == 200

        result = client.post(
            f"/api/projects/{project_id}/autorun/start",
            json={"max_iterations": 1, "max_minutes": 5, "no_progress_limit": 0},
        )
        assert result.status_code == 200
        assert result.json()["autorun"]["stop_reason"] != "duplicate_tool_streak"


def test_capacity_wait_uses_backoff_without_triggering_no_progress(monkeypatch) -> None:
    import aurora.services.autorunner as autorunner_module

    clock = {"now": 0.0}
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(
        autorunner_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock["now"], sleep=sleep),
    )
    monkeypatch.setattr(
        autorunner_module,
        "run_project_exploration_step",
        lambda session, project_id, run_id, allow_multi_agent=True, on_dispatch=None: {"status": "capacity_wait"},
    )

    with Session(engine) as session:
        project = Project(name="capacity-backoff", goal="wait fairly")
        session.add_all([project, Intent(project_id=project.id, objective="pending")])
        session.commit()

        result = AutoRunnerService().run_until_stop(
            session,
            project_id=project.id,
            limits=AutoRunLimits(max_iterations=2, no_progress_limit=1),
        )
        capacity_events = session.exec(
            select(WorkerEvent).where(
                WorkerEvent.project_id == project.id,
                WorkerEvent.event_type == "autorun.capacity_wait_started",
            )
        ).all()
        iteration_events = session.exec(
            select(WorkerEvent).where(
                WorkerEvent.project_id == project.id,
                WorkerEvent.event_type.in_([
                    "autorun.iteration.started",
                    "autorun.iteration.completed",
                ]),
            )
        ).all()

    assert result.stop_reason == "max_iterations"
    assert result.iterations == 2
    assert sum(sleeps) > 0.7
    assert len(capacity_events) == 1
    assert iteration_events == []


def test_autorun_passes_scheduler_phase_to_reasoner(monkeypatch) -> None:
    phases: list[int | None] = []
    monkeypatch.setattr(
        "aurora.services.autorunner.ProjectReasoner.run",
        lambda self, session, project_id, phase=None, deadline_at=None: phases.append(phase) or {"status": "unchanged"},
    )
    monkeypatch.setattr(
        "aurora.services.autorunner.run_project_exploration_step",
        lambda session, project_id, run_id, allow_multi_agent=True, on_dispatch=None: (
            on_dispatch() or {"status": "idle"}
        ),
    )
    with Session(engine) as session:
        project = Project(name="phase-two", goal="propagate phase")
        session.add_all([project, Intent(project_id=project.id, objective="pending")])
        session.commit()

        AutoRunnerService().run_until_stop(
            session,
            project_id=project.id,
            limits=AutoRunLimits(max_iterations=1, phase=2),
        )

    assert phases == [2]


def test_phase_one_defers_reasoning_runs_once_and_seeds_phase_two_handoff(monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(
        "aurora.services.autorunner.ProjectReasoner.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("phase 1 must not fan out")),
    )
    monkeypatch.setattr(
        "aurora.services.autorunner.ManagerService.run_project",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("phase 1 must not ask Manager to fan out")),
    )

    def run_serial(session, project_id, run_id, allow_multi_agent=True, on_dispatch=None):
        calls.append(allow_multi_agent)
        intent = session.exec(
            select(Intent).where(Intent.project_id == project_id, Intent.status == "PENDING")
        ).one()
        intent.status = "COMPLETED"
        session.add(intent)
        session.commit()
        if on_dispatch:
            on_dispatch()
        return {"status": "completed_round"}

    monkeypatch.setattr("aurora.services.autorunner.run_project_exploration_step", run_serial)
    with Session(engine) as session:
        project = Project(name="phase-one", goal="solve directly")
        session.add(project)
        session.commit()

        result = AutoRunnerService().run_until_stop(
            session,
            project_id=project.id,
            limits=AutoRunLimits(
                max_iterations=1,
                phase=1,
                allow_multi_agent=False,
                handoff_phase=2,
            ),
        )
        handoff = session.exec(
            select(Intent).where(Intent.project_id == project.id, Intent.status == "PENDING")
        ).one()

    assert result.stop_reason == "max_iterations"
    assert calls == [False]
    assert handoff.budget["phase"] == 2


def test_autorun_preserves_reason_noop_without_seeding_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "aurora.services.autorunner.ProjectReasoner.run",
        lambda self, session, project_id, phase=None, deadline_at=None: {"status": "noop"},
    )
    monkeypatch.setattr(
        "aurora.services.autorunner.ManagerService.run_project",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("manager must not override noop")),
    )
    with Session(engine) as session:
        project = Project(name="reason-noop", goal="stop cleanly")
        session.add(project)
        session.commit()

        result = AutoRunnerService().run_until_stop(
            session,
            project_id=project.id,
            limits=AutoRunLimits(max_iterations=1),
        )
        pending = session.exec(
            select(Intent).where(Intent.project_id == project.id, Intent.status == "PENDING")
        ).all()

    assert result.stop_reason == "no_runnable_work"
    assert result.iterations == 0
    assert pending == []


def test_autorun_background_start_reports_status() -> None:
    client = TestClient(create_app())
    with client:
        project = client.post(
            "/api/projects",
            json={"name": "autorun-bg", "goal": "后台自动执行", "allowed_hosts": ["127.0.0.1"]},
        ).json()
        project_id = project["id"]
        intent = client.post(
            f"/api/projects/{project_id}/intents",
            json={
                "objective": "输出 flag{autorun_bg}",
                "capability_tags": ["sandbox.exec"],
                "priority": 10,
                "risk_level": "low",
                "tool_request": {"command": "printf 'flag{autorun_bg}'", "cwd": ".", "timeout_seconds": 5},
            },
        )
        assert intent.status_code == 200
        started = client.post(
            f"/api/projects/{project_id}/autorun/start",
            json={"max_iterations": 5, "max_minutes": 5, "no_progress_limit": 2, "background": True},
        )
        assert started.status_code == 200
        assert started.json()["autorun"]["project_id"] == project_id

        status = client.get(f"/api/projects/{project_id}/autorun/status")
        assert status.status_code == 200
        assert status.json()["background"]["project_id"] == project_id


def test_autorunner_rejects_a_second_project_loop() -> None:
    with Session(engine) as session:
        project = Project(name="single-flight", goal="Only one outer loop may run")
        session.add(project)
        session.commit()
        session.refresh(project)
        claim = project_run_control.acquire(project_id=project.id, owner="test")
        assert claim is not None
        try:
            result = AutoRunnerService().run_until_stop(
                session,
                project_id=project.id,
                limits=AutoRunLimits(max_iterations=1),
            )
        finally:
            project_run_control.release(project_id=project.id, run_id=claim.run_id)

        starts = session.exec(
            select(WorkerEvent).where(
                WorkerEvent.project_id == project.id,
                WorkerEvent.event_type == "autorun.started",
            )
        ).all()

    assert result.status == "busy"
    assert result.stop_reason == "project_run_active"
    assert starts == []


def test_project_run_stop_request_reaches_non_background_loops() -> None:
    claim = project_run_control.acquire(project_id="proj_stop_control", owner="test")
    assert claim is not None
    try:
        assert project_run_control.should_stop(project_id=claim.project_id, run_id=claim.run_id) is False
        project_run_control.request_stop(claim.project_id)
        assert project_run_control.should_stop(project_id=claim.project_id, run_id=claim.run_id) is True
    finally:
        project_run_control.release(project_id=claim.project_id, run_id=claim.run_id)


def test_autorun_api_returns_conflict_for_an_active_project_loop() -> None:
    client = TestClient(create_app())
    with client:
        project = client.post(
            "/api/projects",
            json={"name": "single-flight-api", "goal": "Reject duplicate starts", "allowed_hosts": ["127.0.0.1"]},
        ).json()
        claim = project_run_control.acquire(project_id=project["id"], owner="test")
        assert claim is not None
        try:
            response = client.post(
                f"/api/projects/{project['id']}/autorun/start",
                json={"max_iterations": 1, "background": True},
            )
        finally:
            project_run_control.release(project_id=project["id"], run_id=claim.run_id)

    assert response.status_code == 409
    assert response.json()["detail"] == "project already has an active run"


def test_autorunner_seeds_fallback_continuation_intent() -> None:
    with Session(engine) as session:
        project = Project(name="fallback-seed", goal="continue and solve")
        session.add(project)
        session.commit()
        session.refresh(project)

        AutoRunnerService()._seed_fallback_intent(session, project_id=project.id)

        intent = session.exec(
            select(Intent).where(Intent.project_id == project.id, Intent.status == "PENDING")
        ).one()
        event = session.exec(
            select(WorkerEvent).where(
                WorkerEvent.project_id == project.id,
                WorkerEvent.event_type == "manager.fallback_intent_seeded",
            )
        ).one()
        assert "different evidence-backed route" in intent.objective
        assert intent.capability_tags == ["blackboard.query", "codex.shell"]
        assert event.payload_json["phase"] == 1


def test_pending_intent_is_clamped_to_absolute_phase_deadline() -> None:
    with Session(engine) as session:
        project = Project(name="deadline-clamp", goal="respect the phase deadline")
        session.add(project)
        session.commit()
        session.refresh(project)
        intent = Intent(
            project_id=project.id,
            objective="dynamically proposed work",
            budget={"model_role": "triage", "finalize_grace_seconds": 60},
        )
        session.add(intent)
        session.commit()
        deadline_at = now_utc() + timedelta(seconds=90)

        assert AutoRunnerService._clamp_pending_intents_to_deadline(
            session,
            project_id=project.id,
            phase=2,
            deadline_at=deadline_at,
        )

        session.refresh(intent)
        assert 2 <= intent.budget["hard_timeout_seconds"] <= 90
        assert 1 <= intent.budget["soft_timeout_seconds"] < intent.budget["hard_timeout_seconds"]
        assert intent.budget["phase"] == 2
        assert intent.budget["phase_deadline_at"] == deadline_at.isoformat()
