import hashlib
import time
from pathlib import Path
from threading import Barrier, Event, Lock
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from aurora.api import create_app
from aurora.config import Settings, get_settings
from aurora.models import Attempt, AuthorizationScope, ChallengeGroup, ChallengeGroupEvent, ChallengeGroupItem, FlagCandidate, Intent, Project, Worker, now_utc
from aurora.services.challenge_group_runner import ChallengeGroupRunner
from aurora.services.competition_adapter import CompetitionSubmissionResult, EnvironmentHealth, TSecBenchCompetitionAdapter
from aurora.services.harvester_runner import HarvesterResult
from aurora.services.hands_free import HandsFreeService
from aurora.services.tsecbench import TSecBenchChallenge, TSecBenchClient, TSecBenchError, TSecBenchNeedsSession, configure_tsecbench, public_tsecbench_config


def test_tsecbench_accepts_platform_native_environment(monkeypatch) -> None:
    monkeypatch.delenv("AURORA_TSECBENCH_BASE_URL", raising=False)
    monkeypatch.delenv("AURORA_TSECBENCH_TOKEN", raising=False)
    monkeypatch.setenv("BENCHMARK_BASE_URL", "https://native-bench.example")
    monkeypatch.setenv("BENCHMARK_TOKEN", "native-token")
    settings = Settings(codex_require_explicit_model_metadata=False)

    assert settings.tsecbench_base_url == "https://native-bench.example"
    assert settings.tsecbench_token == "native-token"
    assert public_tsecbench_config(settings)["token_source"] == "environment"


def test_tsecbench_prefers_aurora_environment_aliases(monkeypatch) -> None:
    monkeypatch.setenv("BENCHMARK_BASE_URL", "https://native-bench.example")
    monkeypatch.setenv("BENCHMARK_TOKEN", "native-token")
    monkeypatch.setenv("AURORA_TSECBENCH_BASE_URL", "https://aurora-bench.example")
    monkeypatch.setenv("AURORA_TSECBENCH_TOKEN", "aurora-token")
    settings = Settings(codex_require_explicit_model_metadata=False)

    assert settings.tsecbench_base_url == "https://aurora-bench.example"
    assert settings.tsecbench_token == "aurora-token"


def test_tsecbench_rejects_invalid_submission_before_network(tmp_path: Path) -> None:
    client = TSecBenchClient(_settings(tmp_path), request_json=lambda *args, **kwargs: None)

    for flag in ("", "x" * 4097):
        try:
            client.submit_result("web-1", flag)
        except ValueError as exc:
            assert "flag length" in str(exc)
        else:
            raise AssertionError("invalid flag length was accepted")


def _settings(tmp_path: Path, *, token: str | None = "token") -> Settings:
    return Settings(
        artifact_dir=tmp_path,
        codex_require_explicit_model_metadata=False,
        cataloger_llm_api_key=None,
        tsecbench_base_url="https://bench.example",
        tsecbench_token=token,
    )


def test_tsecbench_client_uses_expected_endpoints_and_header(tmp_path: Path) -> None:
    calls: list[tuple[str, str, dict | None, str | None]] = []

    def request(url: str, *, method: str, payload: dict | None, token: str | None):
        calls.append((url, method, payload, token))
        if url.endswith("/challenges"):
            return {"data": [{"unique_code": "web-1", "name": "Web", "category": "Web", "points": 100}]}
        if "/hint?" in url:
            return {"data": {"hint": "inspect source"}}
        if url.endswith("/submit"):
            return {"data": {"correct": True}}
        return {"data": {"container_addr": "http://target.example:8080"}}

    client = TSecBenchClient(_settings(tmp_path), request_json=request)
    assert client.list_challenges()[0].unique_code == "web-1"
    assert client.start("web-1")["container_addr"] == "http://target.example:8080"
    assert client.hint("web-1") == "inspect source"
    assert client.submit("web-1", "flag{ok}") is True
    assert all(call[3] == "token" for call in calls)
    assert calls[-1][2] == {"unique_code": "web-1", "flag": "flag{ok}"}


def test_tsecbench_import_preserves_unique_code_and_platform_metadata(tmp_path: Path) -> None:
    class Client:
        def list_challenges(self):
            return [
                TSecBenchChallenge("u-1", "One", "description", "Web", 2, 3, 50, 1, "stopped", None, {}),
                TSecBenchChallenge("u-1", "Duplicate", "ignored", "Web", 2, 3, 50, 1, "stopped", None, {}),
            ]

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    service = HandsFreeService(settings=_settings(tmp_path), tsecbench_client=Client(), fetch_text=lambda url: ("", url))
    with Session(engine) as session:
        result = service.scan(session, "https://bench.example/openapi/v1/challenges")
        assert result.batch.platform == "tsecbench"
        assert len(result.candidates) == 1
        assert result.candidates[0].source_metadata_json["unique_code"] == "u-1"


def test_tsecbench_import_infers_type_from_challenge_description(tmp_path: Path) -> None:
    class Client:
        def list_challenges(self):
            return [
                TSecBenchChallenge(
                    "u-web",
                    "Boundary bypass",
                    "绕过 Web 防护后利用 SQL 注入获取 flag",
                    None,
                    2,
                    1,
                    100,
                    1,
                    "stopped",
                    None,
                    {},
                )
            ]

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    service = HandsFreeService(settings=_settings(tmp_path), tsecbench_client=Client(), fetch_text=lambda url: ("", url))
    with Session(engine) as session:
        result = service.scan(session, "https://bench.example/openapi/v1/challenges")
        assert result.candidates[0].challenge_type == "web"


def test_tsecbench_start_authorizes_returned_container(tmp_path: Path) -> None:
    class Client:
        def start(self, code):
            assert code == "u-1"
            return {"container_addr": "https://target.example:8443"}

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    settings = _settings(tmp_path)
    with Session(engine) as session:
        project = Project(name="challenge", goal="solve", challenge_type="web")
        group = ChallengeGroup(name="group")
        session.add_all([project, group])
        session.commit()
        session.add(AuthorizationScope(project_id=project.id))
        session.add(ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, competition_meta={"platform": "tsecbench", "unique_code": "u-1", "container_status": "stopped"}))
        session.commit()
        adapter = TSecBenchCompetitionAdapter(settings, Client())
        assert adapter.ensure_environment(session, project_id=project.id).available
        session.refresh(project)
        scope = session.exec(select(AuthorizationScope).where(AuthorizationScope.project_id == project.id)).one()
        assert project.target_url == "https://target.example:8443"
        assert scope.allowed_hosts == ["target.example"]


def test_tsecbench_close_releases_target_and_authorization(tmp_path: Path) -> None:
    closed: list[str] = []

    class Client:
        def start(self, code):
            return {"container_addr": "https://target.example:8443"}

        def close(self, code):
            closed.append(code)

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="challenge", goal="solve", challenge_type="web")
        group = ChallengeGroup(name="group")
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, competition_meta={"platform": "tsecbench", "unique_code": "u-1", "container_status": "stopped"})
        session.add_all([AuthorizationScope(project_id=project.id), item])
        session.commit()

        adapter = TSecBenchCompetitionAdapter(_settings(tmp_path), Client())
        assert adapter.ensure_environment(session, project_id=project.id).available
        adapter.close_environment(project_id=project.id, session=session)
        session.commit()

        session.refresh(project)
        session.refresh(item)
        scope = session.exec(select(AuthorizationScope).where(AuthorizationScope.project_id == project.id)).one()
        assert closed == ["u-1"]
        assert project.target_url is None
        assert project.target_verification_status == "UNVERIFIED"
        assert item.competition_meta["container_status"] == "stopped"
        assert item.competition_meta["container_addr"] == []
        assert scope.allowed_hosts == []


def test_tsecbench_close_failure_retains_capacity_and_authorization(tmp_path: Path) -> None:
    class FailingClient:
        def close(self, code):
            raise TSecBenchError("The read operation timed out")

        def start(self, code):
            raise AssertionError("an unconfirmed release must continue occupying the only slot")

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="challenge", goal="solve", challenge_type="web")
        waiting = Project(name="waiting", goal="solve", challenge_type="web")
        group = ChallengeGroup(name="group")
        session.add_all([project, waiting, group])
        session.commit()
        item = ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            competition_meta={
                "platform": "tsecbench",
                "unique_code": "u-1",
                "container_status": "available",
                "container_addr": ["https://target.example:8443"],
            },
        )
        waiting_item = ChallengeGroupItem(
            group_id=group.id,
            project_id=waiting.id,
            position=2,
            competition_meta={"platform": "tsecbench", "unique_code": "u-2", "container_status": "stopped"},
        )
        session.add_all([AuthorizationScope(project_id=project.id, allowed_hosts=["target.example"]), item, waiting_item])
        session.commit()
        project.target_url = "https://target.example:8443"
        project.target_verification_status = "VERIFIED"
        session.add(project)
        session.commit()

        settings = _settings(tmp_path)
        settings.tsecbench_max_concurrent = 1
        adapter = TSecBenchCompetitionAdapter(settings, FailingClient())
        try:
            adapter.close_environment(project_id=project.id, session=session)
        except TSecBenchError:
            pass
        else:
            raise AssertionError("close failure was swallowed")
        session.commit()

        session.refresh(project)
        session.refresh(item)
        scope = session.exec(select(AuthorizationScope).where(AuthorizationScope.project_id == project.id)).one()
        assert project.target_url == "https://target.example:8443"
        assert project.target_verification_status == "VERIFIED"
        assert item.competition_meta["container_status"] == "release_failed"
        assert item.competition_meta["container_addr"] == ["https://target.example:8443"]
        assert scope.allowed_hosts == ["target.example"]
        assert adapter.ensure_environment(session, project_id=waiting.id).reason == "tsecbench_capacity_exhausted"


def test_tsecbench_close_treats_already_finished_as_idempotent_success(tmp_path: Path) -> None:
    class Client:
        def close(self, code):
            raise TSecBenchError(f"task {code} already finished", code="internal_error")

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="finished", goal="solve", target_url="http://target.example:8000", target_verification_status="VERIFIED")
        group = ChallengeGroup(name="group")
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            competition_meta={"platform": "tsecbench", "unique_code": "u-finished", "container_status": "release_failed", "container_addr": ["http://target.example:8000"]},
        )
        session.add_all([item, AuthorizationScope(project_id=project.id, allowed_hosts=["target.example"])])
        session.commit()

        TSecBenchCompetitionAdapter(_settings(tmp_path), Client()).close_environment(project_id=project.id, session=session)
        session.commit()

        session.refresh(project)
        session.refresh(item)
        scope = session.exec(select(AuthorizationScope).where(AuthorizationScope.project_id == project.id)).one()
        assert item.competition_meta["container_status"] == "stopped"
        assert item.competition_meta["container_addr"] == []
        assert project.target_url is None
        assert scope.allowed_hosts == []


def test_tsecbench_reconciles_stale_release_before_capacity_check(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    class Client:
        def close(self, code):
            calls.append(("close", code))
            raise TSecBenchError(f"task {code} already finished", code="task_not_found")

        def start(self, code):
            calls.append(("start", code))
            return {"container_addr": "http://new-target.example:8080"}

    settings = _settings(tmp_path)
    settings.tsecbench_max_concurrent = 1
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        stale = Project(name="stale", goal="solve", target_url="http://old-target.example:8080", target_verification_status="VERIFIED")
        waiting = Project(name="waiting", goal="solve")
        group = ChallengeGroup(name="group")
        session.add_all([stale, waiting, group])
        session.commit()
        session.add_all([
            ChallengeGroupItem(group_id=group.id, project_id=stale.id, position=1, competition_meta={"platform": "tsecbench", "unique_code": "old", "container_status": "release_failed", "container_addr": ["http://old-target.example:8080"]}),
            ChallengeGroupItem(group_id=group.id, project_id=waiting.id, position=2, competition_meta={"platform": "tsecbench", "unique_code": "new", "container_status": "stopped", "container_addr": []}),
        ])
        session.commit()

        health = TSecBenchCompetitionAdapter(settings, Client()).ensure_environment(session, project_id=waiting.id)

        assert health.available is True
        assert calls == [("close", "old"), ("start", "new")]


def test_tsecbench_group_releases_each_target_before_starting_the_next_challenge(tmp_path: Path) -> None:
    lifecycle: list[tuple[str, str]] = []

    class Client:
        active: str | None = None

        def start(self, code):
            assert self.active is None, f"target {self.active} was not released before starting {code}"
            self.active = code
            lifecycle.append(("start", code))
            return {"container_addr": f"http://{code}.target.example:8080"}

        def close(self, code):
            assert self.active == code
            lifecycle.append(("close", code))
            self.active = None

        def hint(self, code):
            return None

    class FailedHarvester:
        def run(self, session, *, project_id, task, limits, should_stop):
            lifecycle.append(("solve", task["title"]))
            return HarvesterResult(status="FAILED", reason="try_next_phase")

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        group = ChallengeGroup(name="sequential-target-cycle", max_concurrent=1)
        projects = [Project(name="one", goal="solve one"), Project(name="two", goal="solve two")]
        session.add_all([group, *projects])
        session.commit()
        session.add_all([
            ChallengeGroupItem(
                group_id=group.id,
                project_id=project.id,
                position=position,
                competition_meta={"platform": "tsecbench", "unique_code": project.name, "container_status": "stopped"},
            )
            for position, project in enumerate(projects, start=1)
        ])
        session.commit()

        adapter = TSecBenchCompetitionAdapter(_settings(tmp_path), Client())
        ChallengeGroupRunner(harvester=FailedHarvester(), competition=adapter).run(session, group_id=group.id)

        session.refresh(group)
        assert group.status == "COMPLETED"
        assert lifecycle == [
            (action, name)
            for _phase in range(3)
            for name in ("one", "two")
            for action in ("start", "solve", "close")
        ]
        items = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group.id)).all()
        assert all(item.fused_status == "FAILED" for item in items)
        assert all(item.competition_meta["container_status"] == "stopped" for item in items)


def test_tsecbench_target_is_released_only_after_phase_state_is_resolved() -> None:
    worker_calls: list[tuple[int, int]] = []
    close_states: list[tuple[str, int]] = []

    class Adapter:
        active = False

        def ensure_environment(self, session, *, project_id):
            self.active = True
            return SimpleNamespace(available=True, reason=None)

        def close_environment(self, *, project_id, session=None):
            assert session is not None
            item = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.project_id == project_id)).one()
            # Closing while RUNNING is the regression: it means a Worker call,
            # rather than the phase transition, owned target lifetime.
            assert item.fused_status != "RUNNING"
            close_states.append((item.fused_status, item.phase))
            self.active = False

        def fetch_hint(self, session, *, project_id):
            return None

    adapter = Adapter()

    class MultiWorkerPhaseHarvester:
        def run(self, session, *, project_id, task, limits, should_stop):
            phase = task["phase"]
            for worker_number in (1, 2):
                assert adapter.active is True
                worker_calls.append((phase, worker_number))
            return HarvesterResult(status="FAILED", reason="phase_exhausted")

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        group = ChallengeGroup(name="phase-owned-target", max_concurrent=1)
        project = Project(name="one", goal="solve one")
        session.add_all([group, project])
        session.commit()
        session.add(ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            competition_meta={"platform": "tsecbench", "unique_code": "one"},
        ))
        session.commit()

        ChallengeGroupRunner(harvester=MultiWorkerPhaseHarvester(), competition=adapter).run(
            session,
            group_id=group.id,
        )

        assert worker_calls == [(phase, worker) for phase in (1, 2, 3) for worker in (1, 2)]
        assert close_states == [("PENDING", 2), ("PENDING", 3), ("FAILED", 3)]
        close_events = session.exec(
            select(ChallengeGroupEvent).where(
                ChallengeGroupEvent.group_id == group.id,
                ChallengeGroupEvent.event_type == "group.item.environment_closed",
            )
        ).all()
        assert [event.payload_json["reason"] for event in close_events] == ["phase_finished"] * 3


def test_tsecbench_target_release_is_deferred_while_a_worker_is_active() -> None:
    class Adapter:
        def close_environment(self, *, project_id, session=None):
            raise AssertionError("active Worker must retain the challenge target")

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        group = ChallengeGroup(name="active-worker-target", status="RUNNING")
        project = Project(name="one", goal="solve one")
        session.add_all([group, project])
        session.commit()
        intent = Intent(project_id=project.id, objective="continue phase", status="RUNNING")
        session.add(intent)
        session.commit()
        worker = Worker(project_id=project.id, intent_id=intent.id, status="RUNNING")
        item = ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            status="RUNNING",
            fused_status="RUNNING",
            competition_meta={"platform": "tsecbench", "unique_code": "one"},
        )
        session.add_all([worker, item])
        session.commit()

        ChallengeGroupRunner(competition=Adapter())._resolve_phase(
            session,
            group=group,
            item=item,
            project=project,
            outcome="FAILED",
            reason="phase scheduler raced with active worker",
        )

        deferred = session.exec(
            select(ChallengeGroupEvent).where(
                ChallengeGroupEvent.group_id == group.id,
                ChallengeGroupEvent.event_type == "group.item.environment_release_deferred",
            )
        ).one()
        assert deferred.payload_json["reason"] == "active_workers"
        assert deferred.payload_json["worker_ids"] == [worker.id]


def test_concurrent_tsecbench_solver_crash_does_not_stop_later_challenges(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    lock = Lock()
    crashed = False

    class Adapter:
        def ensure_environment(self, session, *, project_id):
            return SimpleNamespace(available=True, reason=None)

        def close_environment(self, *, project_id, session=None):
            return None

        def fetch_hint(self, session, *, project_id):
            return None

    class CrashOnceHarvester:
        def run(self, session, *, project_id, task, limits, should_stop):
            nonlocal crashed
            with lock:
                calls.append(task["title"])
                if task["title"] == "one" and not crashed:
                    crashed = True
                    raise ValueError("malformed provider JSON")
            return HarvesterResult(status="FAILED", reason="try_next_phase")

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr("aurora.services.challenge_group_runner.engine", test_engine)
    monkeypatch.setattr("aurora.services.challenge_group_runner.stop_project_containers", lambda _project_id: {})

    with Session(test_engine) as session:
        group = ChallengeGroup(name="crash-contained", max_concurrent=2)
        projects = [Project(name=name, goal=f"solve {name}") for name in ("one", "two", "three")]
        session.add_all([group, *projects])
        session.commit()
        session.add_all([
            ChallengeGroupItem(
                group_id=group.id,
                project_id=project.id,
                position=position,
                competition_meta={"platform": "tsecbench", "unique_code": project.name},
            )
            for position, project in enumerate(projects, start=1)
        ])
        session.commit()

        ChallengeGroupRunner(harvester=CrashOnceHarvester(), competition=Adapter()).run(
            session,
            group_id=group.id,
        )

        session.refresh(group)
        crash_event = session.exec(
            select(ChallengeGroupEvent).where(
                ChallengeGroupEvent.group_id == group.id,
                ChallengeGroupEvent.event_type == "group.item.solver_crashed",
            )
        ).one()
        assert group.status == "COMPLETED"
        assert crash_event.payload_json["error"].startswith("solver_runtime_exception: ValueError")
        assert "three" in calls


def test_concurrent_group_refills_a_slot_before_other_work_finishes(tmp_path: Path, monkeypatch) -> None:
    initial_pair_started = Barrier(2)
    third_started = Event()
    timeline: list[tuple[str, str]] = []
    lock = Lock()
    active = 0
    peak_active = 0

    class Adapter:
        def ensure_environment(self, session, *, project_id):
            return SimpleNamespace(available=True, reason=None)

        def close_environment(self, *, project_id, session=None):
            return None

        def fetch_hint(self, session, *, project_id):
            return None

    class RollingHarvester:
        def run(self, session, *, project_id, task, limits, should_stop):
            nonlocal active, peak_active
            title = task["title"]
            with lock:
                active += 1
                peak_active = max(peak_active, active)
                timeline.append(("started", title))
            try:
                if title in {"one", "two"}:
                    initial_pair_started.wait(timeout=3)
                if title == "two":
                    assert third_started.wait(timeout=3), "the free concurrency slot was not refilled"
                    with lock:
                        timeline.append(("observed_third", title))
                elif title == "three":
                    third_started.set()
                project = session.get(Project, project_id)
                project.status = "COMPLETED"
                session.add(project)
                session.commit()
                return HarvesterResult(status="COMPLETED", reason="solved")
            finally:
                with lock:
                    timeline.append(("finished", title))
                    active -= 1

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'rolling-concurrency.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr("aurora.services.challenge_group_runner.engine", test_engine)

    with Session(test_engine) as session:
        group = ChallengeGroup(name="rolling-concurrency", max_concurrent=2)
        projects = [Project(name=name, goal=f"solve {name}") for name in ("one", "two", "three")]
        session.add_all([group, *projects])
        session.commit()
        session.add_all([
            ChallengeGroupItem(
                group_id=group.id,
                project_id=project.id,
                position=position,
                competition_meta={"platform": "tsecbench", "unique_code": project.name},
            )
            for position, project in enumerate(projects, start=1)
        ])
        session.commit()

        ChallengeGroupRunner(harvester=RollingHarvester(), competition=Adapter()).run(
            session,
            group_id=group.id,
        )

        session.refresh(group)
        assert group.status == "COMPLETED"
        assert peak_active == 2
        assert timeline.index(("started", "three")) < timeline.index(("finished", "two"))


def test_capacity_waiter_is_requeued_after_a_target_is_released(tmp_path: Path, monkeypatch) -> None:
    environment_lock = Lock()
    capacity_denied = Event()
    active_environment: set[str] = set()
    allocated_projects: set[str] = set()
    first_allocation: str | None = None
    capacity_denials = 0

    class Adapter:
        def ensure_environment(self, session, *, project_id):
            nonlocal first_allocation, capacity_denials
            with environment_lock:
                if active_environment and project_id not in active_environment:
                    capacity_denials += 1
                    capacity_denied.set()
                    return SimpleNamespace(available=False, reason="tsecbench_capacity_exhausted")
                active_environment.add(project_id)
                allocated_projects.add(project_id)
                if first_allocation is None:
                    first_allocation = project_id
            return SimpleNamespace(available=True, reason=None)

        def close_environment(self, *, project_id, session=None):
            with environment_lock:
                active_environment.discard(project_id)

        def fetch_hint(self, session, *, project_id):
            return None

    class CompletingHarvester:
        def run(self, session, *, project_id, task, limits, should_stop):
            if project_id == first_allocation:
                assert capacity_denied.wait(timeout=3)
            project = session.get(Project, project_id)
            project.status = "COMPLETED"
            session.add(project)
            session.commit()
            return HarvesterResult(status="COMPLETED", reason="solved")

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'capacity-refill.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr("aurora.services.challenge_group_runner.engine", test_engine)

    with Session(test_engine) as session:
        group = ChallengeGroup(name="capacity-refill", max_concurrent=2)
        projects = [Project(name=name, goal=f"solve {name}") for name in ("one", "two", "three")]
        session.add_all([group, *projects])
        session.commit()
        session.add_all([
            ChallengeGroupItem(
                group_id=group.id,
                project_id=project.id,
                position=position,
                competition_meta={"platform": "tsecbench", "unique_code": project.name},
            )
            for position, project in enumerate(projects, start=1)
        ])
        session.commit()

        ChallengeGroupRunner(harvester=CompletingHarvester(), competition=Adapter()).run(
            session,
            group_id=group.id,
        )

        session.refresh(group)
        assert group.status == "COMPLETED"
        assert capacity_denials >= 1
        assert allocated_projects == {project.id for project in projects}


def test_host_suspend_invalidates_concurrent_group_without_advancing_phase(tmp_path: Path, monkeypatch) -> None:
    class Detector:
        def __init__(self):
            self.polls = 0

        def poll(self):
            self.polls += 1
            return 120.0 if self.polls >= 2 else None

    class Adapter:
        def ensure_environment(self, session, *, project_id):
            return SimpleNamespace(available=True, reason=None)

        def close_environment(self, *, project_id, session=None):
            return None

        def fetch_hint(self, session, *, project_id):
            return None

    class BlockingHarvester:
        def run(self, session, *, project_id, task, limits, should_stop):
            while not should_stop():
                time.sleep(0.01)
            return HarvesterResult(status="FAILED", reason="stopped")

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'suspend-invalidates.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr("aurora.services.challenge_group_runner.engine", test_engine)
    monkeypatch.setattr("aurora.services.challenge_group_runner.SuspendGapDetector", Detector)
    monkeypatch.setattr("aurora.services.challenge_group_runner.stop_project_containers", lambda project_id: None)

    with Session(test_engine) as session:
        group = ChallengeGroup(name="suspend-invalidates", max_concurrent=2)
        project = Project(name="one", goal="solve")
        session.add_all([group, project])
        session.commit()
        item = ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            competition_meta={"platform": "tsecbench", "unique_code": "one"},
        )
        session.add(item)
        session.commit()

        ChallengeGroupRunner(harvester=BlockingHarvester(), competition=Adapter()).run(
            session,
            group_id=group.id,
        )

        session.refresh(group)
        session.refresh(item)
        assert group.status == "STOPPED"
        assert item.phase == 1
        events = session.exec(
            select(ChallengeGroupEvent).where(ChallengeGroupEvent.group_id == group.id)
        ).all()
        assert any(event.event_type == "group.invalidated" for event in events)
        assert not any(event.event_type == "group.item.phase_finished" for event in events)


def test_capacity_only_blocker_uses_waiting_resource(tmp_path: Path, monkeypatch) -> None:
    class Adapter:
        def ensure_environment(self, session, *, project_id):
            return SimpleNamespace(available=False, reason="tsecbench_capacity_exhausted")

        def close_environment(self, *, project_id, session=None):
            return None

        def fetch_hint(self, session, *, project_id):
            return None

    test_engine = create_engine(f"sqlite:///{tmp_path / 'waiting-resource.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr("aurora.services.challenge_group_runner.engine", test_engine)
    with Session(test_engine) as session:
        group = ChallengeGroup(name="resource-wait", max_concurrent=2)
        project = Project(name="one", goal="solve")
        session.add_all([group, project])
        session.commit()
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, competition_meta={"platform": "tsecbench", "unique_code": "one"})
        session.add(item)
        session.commit()

        ChallengeGroupRunner(competition=Adapter()).run(session, group_id=group.id)

        session.refresh(group)
        session.refresh(item)
        assert group.status == "WAITING_RESOURCE"
        assert item.fused_status == "WAITING_RESOURCE"
        assert item.stop_reason == "tsecbench_capacity_exhausted"


def test_tsecbench_capacity_is_checked_before_start(tmp_path: Path) -> None:
    class Client:
        def start(self, code):
            raise AssertionError("a fourth TSecBench target must not be requested")

    settings = _settings(tmp_path)
    settings.tsecbench_max_concurrent = 3
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        group = ChallengeGroup(name="group")
        session.add(group)
        session.commit()
        for position in range(1, 5):
            project = Project(name=f"challenge-{position}", goal="solve")
            session.add(project)
            session.commit()
            active = position <= 3
            session.add(ChallengeGroupItem(
                group_id=group.id,
                project_id=project.id,
                position=position,
                competition_meta={
                    "platform": "tsecbench",
                    "unique_code": f"u-{position}",
                    "container_status": "available" if active else "stopped",
                    "container_addr": [f"10.0.0.{position}:80"] if active else [],
                },
            ))
        session.commit()

        fourth = session.exec(select(Project).where(Project.name == "challenge-4")).one()
        health = TSecBenchCompetitionAdapter(settings, Client()).ensure_environment(session, project_id=fourth.id)
        assert health.available is False
        assert health.reason == "tsecbench_capacity_exhausted"
        assert health.disposition == "wait_resource"


def test_tsecbench_platform_capacity_message_is_normalized(tmp_path: Path) -> None:
    class Client:
        def start(self, code):
            raise TSecBenchError(
                "max active challenge instances reached (3), please close an existing challenge before starting a new one",
                code="resource_unavailable",
            )

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="waiting", goal="solve")
        group = ChallengeGroup(name="group")
        session.add_all([project, group])
        session.commit()
        session.add(ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            competition_meta={"platform": "tsecbench", "unique_code": "u-wait", "container_status": "stopped"},
        ))
        session.commit()

        health = TSecBenchCompetitionAdapter(_settings(tmp_path), Client()).ensure_environment(
            session,
            project_id=project.id,
        )

        assert health.available is False
        assert health.reason == "tsecbench_capacity_exhausted"


def test_start_invalid_state_is_capacity_when_task_list_is_available(tmp_path: Path) -> None:
    class Client:
        def start(self, code):
            raise TSecBenchError("invalid state", status=409, code="invalid_state")

        def list_challenges(self):
            return []

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="waiting", goal="solve")
        group = ChallengeGroup(name="group")
        session.add_all([project, group])
        session.commit()
        session.add(ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            competition_meta={"platform": "tsecbench", "unique_code": "u-wait", "container_status": "stopped"},
        ))
        session.commit()

        health = TSecBenchCompetitionAdapter(_settings(tmp_path), Client()).ensure_environment(
            session,
            project_id=project.id,
        )

        assert health.available is False
        assert health.reason == "tsecbench_capacity_exhausted"
        assert health.disposition == "wait_resource"


def test_start_invalid_state_is_task_terminal_when_list_is_terminal(tmp_path: Path) -> None:
    class Client:
        def start(self, code):
            raise TSecBenchError("invalid state", status=409, code="invalid_state")

        def list_challenges(self):
            raise TSecBenchError("task already finished", status=409, code="invalid_state")

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="expired", goal="solve")
        group = ChallengeGroup(name="group")
        session.add_all([project, group])
        session.commit()
        session.add(ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            competition_meta={"platform": "tsecbench", "unique_code": "u-expired", "container_status": "stopped"},
        ))
        session.commit()

        health = TSecBenchCompetitionAdapter(_settings(tmp_path), Client()).ensure_environment(
            session,
            project_id=project.id,
        )

        assert health.available is False
        assert health.reason == "tsecbench_task_finished"
        assert health.disposition == "task_terminal"


def test_challenge_not_found_is_an_item_terminal_error(tmp_path: Path) -> None:
    class Client:
        def start(self, code):
            raise TSecBenchError("challenge does not exist", status=404, code="challenge_not_found")

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="missing", goal="solve")
        group = ChallengeGroup(name="group")
        session.add_all([project, group])
        session.commit()
        session.add(ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            competition_meta={"platform": "tsecbench", "unique_code": "u-missing", "container_status": "stopped"},
        ))
        session.commit()

        health = TSecBenchCompetitionAdapter(_settings(tmp_path), Client()).ensure_environment(
            session,
            project_id=project.id,
        )

        assert health.reason == "tsecbench_challenge_not_found"
        assert health.disposition == "item_terminal"


def test_task_terminal_fails_all_unfinished_items_without_running_solver() -> None:
    class Adapter:
        def ensure_environment(self, session, *, project_id):
            return EnvironmentHealth(False, "tsecbench_task_finished", "task_terminal", "task already finished")

        def close_environment(self, *, project_id, session=None):
            return None

        def fetch_hint(self, session, *, project_id):
            return None

    class Harvester:
        def run(self, session, *, project_id, task, limits, should_stop):
            raise AssertionError("a terminal benchmark task must not dispatch a solver")

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        group = ChallengeGroup(name="expired-task", max_concurrent=1)
        completed = Project(name="done", goal="done", status="COMPLETED")
        pending = Project(name="pending", goal="solve")
        waiting = Project(name="waiting", goal="solve", status="WAITING_INPUT")
        session.add_all([group, completed, pending, waiting])
        session.commit()
        session.add_all([
            ChallengeGroupItem(
                group_id=group.id,
                project_id=completed.id,
                position=1,
                status="COMPLETED",
                fused_status="COMPLETED",
                competition_meta={"platform": "tsecbench", "unique_code": "done"},
            ),
            ChallengeGroupItem(
                group_id=group.id,
                project_id=pending.id,
                position=2,
                competition_meta={"platform": "tsecbench", "unique_code": "pending"},
            ),
            ChallengeGroupItem(
                group_id=group.id,
                project_id=waiting.id,
                position=3,
                status="WAITING_INPUT",
                fused_status="WAITING_INPUT",
                competition_meta={"platform": "tsecbench", "unique_code": "waiting"},
            ),
        ])
        session.commit()

        ChallengeGroupRunner(harvester=Harvester(), competition=Adapter()).run(session, group_id=group.id)

        session.refresh(group)
        items = session.exec(
            select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group.id).order_by(ChallengeGroupItem.position)
        ).all()
        assert group.status == "COMPLETED"
        assert items[0].fused_status == "COMPLETED"
        assert [item.fused_status for item in items[1:]] == ["FAILED", "FAILED"]
        assert all(item.stop_reason == "tsecbench_task_finished" for item in items[1:])
        events = session.exec(
            select(ChallengeGroupEvent).where(
                ChallengeGroupEvent.group_id == group.id,
                ChallengeGroupEvent.event_type == "group.platform_task_finished",
            )
        ).all()
        assert len(events) == 1
        assert events[0].payload_json["failed_items"] == 2
        assert events[0].payload_json["detail"] == "task already finished"


def test_late_solver_result_cannot_reopen_a_task_terminal_item() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        group = ChallengeGroup(name="expired-task", status="RUNNING")
        project = Project(name="late-result", goal="solve", status="FLAG_READY", target_url="http://stale.example:80", target_verification_status="VERIFIED")
        session.add_all([group, project])
        session.commit()
        item = ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            status="FAILED",
            fused_status="FAILED",
            stop_reason="tsecbench_task_finished",
            competition_meta={
                "platform": "tsecbench",
                "unique_code": "late-result",
                "task_terminal_reason": "tsecbench_task_finished",
            },
        )
        session.add(item)
        session.commit()

        ChallengeGroupRunner()._resolve_phase(
            session,
            group=group,
            item=item,
            project=project,
            outcome="CANDIDATE_READY",
            reason="late solver result",
        )

        session.refresh(item)
        session.refresh(project)
        assert item.fused_status == "FAILED"
        assert project.status == "FAILED"
        assert project.target_url is None


def test_runner_materializes_runnable_intent_before_allocating_target() -> None:
    observed_budgets: list[dict] = []

    class Adapter:
        def ensure_environment(self, session, *, project_id):
            intent = session.exec(
                select(Intent).where(Intent.project_id == project_id, Intent.status == "PENDING")
            ).one()
            observed_budgets.append(dict(intent.budget))
            return SimpleNamespace(available=True, reason=None)

        def close_environment(self, *, project_id, session=None):
            return None

        def fetch_hint(self, session, *, project_id):
            return None

    class CompletingHarvester:
        def run(self, session, *, project_id, task, limits, should_stop):
            project = session.get(Project, project_id)
            project.status = "COMPLETED"
            session.add(project)
            session.commit()
            return HarvesterResult(status="COMPLETED", reason="solved")

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        group = ChallengeGroup(name="preflight-intent")
        project = Project(name="one", goal="solve one")
        session.add_all([group, project])
        session.commit()
        session.add(ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            competition_meta={"platform": "tsecbench", "unique_code": "one"},
        ))
        session.commit()

        ChallengeGroupRunner(harvester=CompletingHarvester(), competition=Adapter()).run(
            session,
            group_id=group.id,
        )

        assert len(observed_budgets) == 1
        assert observed_budgets[0]["phase"] == 1
        assert 2 <= observed_budgets[0]["hard_timeout_seconds"] <= 1800


def test_tsecbench_bare_port_80_is_an_http_target(tmp_path: Path) -> None:
    class Client:
        def start(self, code):
            return {"container_addr": "10.0.176.112:80"}

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="unknown-category-web", goal="solve", challenge_type="unknown")
        group = ChallengeGroup(name="group")
        session.add_all([project, group])
        session.commit()
        session.add(ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, competition_meta={"platform": "tsecbench", "unique_code": "a-05", "container_status": "stopped"}))
        session.commit()

        assert TSecBenchCompetitionAdapter(_settings(tmp_path), Client()).ensure_environment(session, project_id=project.id).available
        session.refresh(project)
        assert project.target_url == "http://10.0.176.112:80"


def test_tsecbench_repairs_legacy_tcp_web_address(tmp_path: Path) -> None:
    class Client:
        def start(self, code):
            raise AssertionError("an available legacy instance should be reused")

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="legacy", goal="solve", challenge_type="unknown", target_url="tcp://10.0.176.112:80")
        group = ChallengeGroup(name="group")
        session.add_all([project, group])
        session.commit()
        session.add(ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, competition_meta={"platform": "tsecbench", "unique_code": "a-05", "container_status": "available", "container_addr": ["tcp://10.0.176.112:80"]}))
        session.commit()

        assert TSecBenchCompetitionAdapter(_settings(tmp_path), Client()).ensure_environment(session, project_id=project.id).available
        session.refresh(project)
        assert project.target_url == "http://10.0.176.112:80"


def test_tsecbench_group_is_detected_for_platform_dispatch() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        group = ChallengeGroup(name="tsec", max_concurrent=3)
        project = Project(name="one", goal="solve")
        session.add_all([group, project])
        session.commit()
        session.add(ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, competition_meta={"platform": "tsecbench", "unique_code": "one"}))
        session.commit()
        assert ChallengeGroupRunner._is_tsecbench_group(session, group.id) is True


def test_unconfigured_tsecbench_never_falls_back_to_local_solver() -> None:
    settings = get_settings()
    original = settings.tsecbench_token
    settings.tsecbench_token = None
    try:
        item = ChallengeGroupItem(
            group_id="group_test",
            project_id="project_test",
            position=1,
            competition_meta={"platform": "tsecbench", "unique_code": "one"},
        )
        adapter = ChallengeGroupRunner()._competition_for(item)
        assert isinstance(adapter, TSecBenchCompetitionAdapter)
        assert adapter.settings.tsecbench_configured is False
    finally:
        settings.tsecbench_token = original


def test_tsecbench_group_uses_platform_concurrency_limit() -> None:
    settings = get_settings()
    original = settings.tsecbench_max_concurrent
    settings.tsecbench_max_concurrent = 3
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            group = ChallengeGroup(name="tsec", max_concurrent=8)
            project = Project(name="one", goal="solve")
            session.add_all([group, project])
            session.commit()
            session.add(ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, competition_meta={"platform": "tsecbench", "unique_code": "one"}))
            session.commit()
            assert ChallengeGroupRunner._max_workers(session, group) == 3
    finally:
        settings.tsecbench_max_concurrent = original


def test_official_challenge_fields_and_container_addresses_are_preserved() -> None:
    challenge = TSecBenchClient.parse_challenge({
        "unique_code": "web_sql_injection_01",
        "description": "SQL injection",
        "difficulty": "easy",
        "level": 1,
        "total_score": 100,
        "flag_count": 2,
        "correct_flag_count": 1,
        "is_completed": False,
        "container_status": "available",
        "container_addr": ["10.0.1.5:8080", "10.0.1.6:8080"],
    })

    assert challenge.points == 100
    assert challenge.correct_flag_count == 1
    assert challenge.container_addr == ["10.0.1.5:8080", "10.0.1.6:8080"]


def test_task_not_found_is_an_authentication_error() -> None:
    try:
        TSecBenchClient._unwrap({"code": "task_not_found", "message": "task does not exist", "detail": {}})
    except TSecBenchNeedsSession as exc:
        assert exc.auth_required is True
        assert exc.code == "task_not_found"
    else:
        raise AssertionError("task_not_found must require a new token")


def test_partial_flag_submission_updates_progress_without_completing_project(tmp_path: Path) -> None:
    class Client:
        def submit_result(self, code, flag):
            from aurora.services.tsecbench import TSecBenchSubmission
            assert (code, flag) == ("multi-flag", "flag{one}")
            return TSecBenchSubmission(True, 50, 50, 1, 2, 0)

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="multi", goal="solve both flags")
        group = ChallengeGroup(name="group")
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, competition_meta={"platform": "tsecbench", "unique_code": "multi-flag", "flag_count": 2})
        session.add(item)
        session.commit()

        result = TSecBenchCompetitionAdapter(_settings(tmp_path), Client()).submit_flag(session, project_id=project.id, value="flag{one}")

        assert isinstance(result, CompetitionSubmissionResult)
        assert result.correct is True
        assert result.completed is False
        session.flush()
        session.refresh(item)
        assert item.competition_meta["correct_flag_count"] == 1
        assert item.competition_meta["is_completed"] is False


def test_runner_continues_after_partial_tsecbench_flag() -> None:
    class PartialAdapter:
        def submit_flag(self, session, *, project_id, value):
            assert value == "flag{one}"
            return CompetitionSubmissionResult(
                correct=True,
                completed=False,
                detail={"correct_flag_count": 1, "total_flag_count": 2},
            )

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="multi", goal="solve both flags", status="FLAG_READY")
        group = ChallengeGroup(name="group", status="RUNNING")
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            phase=2,
            status="RUNNING",
            fused_status="RUNNING",
            competition_meta={"platform": "tsecbench", "unique_code": "multi-flag", "flag_count": 2},
        )
        candidate = FlagCandidate(
            project_id=project.id,
            value="flag{one}",
            value_hash=hashlib.sha256(b"flag{one}").hexdigest(),
            status="LOCAL_VERIFIED",
            provenance_kind="OBSERVED",
        )
        session.add_all([item, candidate])
        session.commit()
        group.current_item_id = item.id
        session.add(group)
        session.commit()

        ChallengeGroupRunner(competition=PartialAdapter())._resolve_phase(
            session,
            group=group,
            item=item,
            project=project,
            outcome="CANDIDATE_READY",
            reason="candidate detected",
        )

        session.refresh(project)
        session.refresh(group)
        session.refresh(item)
        session.refresh(candidate)
        continuation = session.exec(select(Intent).where(Intent.project_id == project.id, Intent.status == "PENDING")).one()
        progress = session.exec(
            select(ChallengeGroupEvent).where(
                ChallengeGroupEvent.group_id == group.id,
                ChallengeGroupEvent.event_type == "group.item.flag_progress",
            )
        ).one()
        assert project.status == "WORKING"
        assert (item.status, item.fused_status, item.phase, item.submission_status) == ("PENDING", "PENDING", 1, "PARTIAL")
        assert group.current_item_id is None
        assert candidate.status == "ACCEPTED"
        assert "1/2" in continuation.objective
        assert progress.payload_json["correct_flag_count"] == 1


def test_web_configuration_does_not_expose_token() -> None:
    from aurora.config import get_settings
    settings = get_settings()
    original = (settings.tsecbench_base_url, settings.tsecbench_token, settings.tsecbench_timeout_seconds, settings.tsecbench_max_concurrent)
    try:
        configured = configure_tsecbench(base_url="https://bench.example", token="secret-token", clear_token=False, timeout_seconds=15, max_concurrent=3)
        assert configured["token_configured"] is True
        assert "secret-token" not in str(configured)
        assert configured.get("token") is None
        assert public_tsecbench_config()["base_url"] == "https://bench.example"
    finally:
        settings.tsecbench_base_url, settings.tsecbench_token, settings.tsecbench_timeout_seconds, settings.tsecbench_max_concurrent = original


def test_tsecbench_configuration_api_saves_tests_and_clears_runtime_token(monkeypatch) -> None:
    from aurora.config import get_settings

    settings = get_settings()
    original = (settings.tsecbench_base_url, settings.tsecbench_token, settings.tsecbench_timeout_seconds, settings.tsecbench_max_concurrent)
    monkeypatch.setattr(
        "aurora.api.test_tsecbench_connection",
        lambda: {
            "api": {"status": "reachable", "challenge_count": 2},
            "vpn": {"status": "reachable", "address": "10.0.1.5:8080", "message": "reachable"},
            "progress": {"completed": 1, "correct_flags": 2, "total_flags": 3},
        },
    )
    client = TestClient(create_app())
    try:
        saved = client.put(
            "/api/settings/tsecbench",
            json={
                "base_url": "https://bench.example",
                "token": "secret-token",
                "timeout_seconds": 15,
                "max_concurrent": 2,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["token_configured"] is True
        assert "secret-token" not in saved.text
        assert "token" not in saved.json()

        tested = client.post("/api/settings/tsecbench/test")
        assert tested.status_code == 200
        assert tested.json()["vpn"]["status"] == "reachable"

        cleared = client.put(
            "/api/settings/tsecbench",
            json={
                "base_url": "https://bench.example",
                "clear_token": True,
                "timeout_seconds": 15,
                "max_concurrent": 2,
            },
        )
        assert cleared.status_code == 200
        assert cleared.json()["token_configured"] is False
    finally:
        settings.tsecbench_base_url, settings.tsecbench_token, settings.tsecbench_timeout_seconds, settings.tsecbench_max_concurrent = original


def test_close_environment_with_retry_retries_then_succeeds() -> None:
    class FlakyAdapter:
        def __init__(self):
            self.calls = 0

        def close_environment(self, *, project_id, session):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient close failure")

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        adapter = FlakyAdapter()
        result = ChallengeGroupRunner()._close_environment_with_retry(adapter, session, project_id="p-1")
        assert result is None
        assert adapter.calls == 2


def test_phase_attempts_records_only_new_attempts() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="p", goal="solve")
        group = ChallengeGroup(name="g")
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, phase=1)
        session.add(item)
        session.commit()

        ChallengeGroupRunner()._record_phase_attempts(session, item=item, attempts_before=0)
        assert item.phase_attempts == {}

        attempt = Attempt(project_id=project.id, intent_id="intent-1", worker_id="worker-1")
        session.add(attempt)
        session.commit()
        ChallengeGroupRunner()._record_phase_attempts(session, item=item, attempts_before=0)
        assert item.phase_attempts == {"1": 1}


def test_phase_deadline_survives_redispatch_and_resets_on_next_phase() -> None:
    item = ChallengeGroupItem(group_id="g", project_id="p", position=1)
    runner = ChallengeGroupRunner()

    first = runner._autorun_limits(item).deadline_at
    item.started_at = now_utc()
    second = runner._autorun_limits(item).deadline_at

    assert first is not None
    assert second == first
    item.phase = 2
    item.phase_started_at = None
    item.phase_deadline_at = None
    third = runner._autorun_limits(item).deadline_at
    assert third is not None and third > first
