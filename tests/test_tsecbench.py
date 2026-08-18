import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from aurora.api import create_app
from aurora.config import Settings, get_settings
from aurora.models import AuthorizationScope, ChallengeGroup, ChallengeGroupEvent, ChallengeGroupItem, FlagCandidate, Intent, Project
from aurora.services.challenge_group_runner import ChallengeGroupRunner
from aurora.services.competition_adapter import CompetitionSubmissionResult, TSecBenchCompetitionAdapter
from aurora.services.hands_free import HandsFreeService
from aurora.services.tsecbench import TSecBenchChallenge, TSecBenchClient, TSecBenchNeedsSession, configure_tsecbench, public_tsecbench_config


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
