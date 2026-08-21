from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from aurora.api import create_app
from aurora.config import Settings, get_settings
from aurora.db import get_session
from aurora.models import Artifact, AuthorizationScope, ChallengeGroup, ChallengeGroupItem, Fact, Project
from aurora.services.competition_adapter import CompetitionSubmissionResult, SlabMatchCompetitionAdapter
from aurora.services.challenge_group_runner import ChallengeGroupRunner
from aurora.services.slab_match import SlabMatchChallenge, SlabMatchClient, SlabMatchError, _SlabMatchRedirectHandler, configure_slab_match, normalize_slab_match_base_url, public_slab_match_config, test_slab_match_connection as check_slab_match_connection
from aurora.services.slab_match_import import SlabMatchDirectImporter
from aurora.services.slab_match_notices import SlabMatchNoticePoller


def _settings(tmp_path: Path, *, access_key: str | None = "agent-key") -> Settings:
    return Settings(
        artifact_dir=tmp_path,
        codex_require_explicit_model_metadata=False,
        cataloger_llm_api_key=None,
        slab_match_base_url="https://agent.example/slab-match/api/v1/agent",
        slab_match_access_key=access_key,
        slab_match_max_concurrent=2,
    )


def _challenge(
    *,
    exercise_id: int = 1001,
    endpoints: list[dict] | None = None,
    attachments: list[dict[str, str]] | None = None,
    is_need_init: bool = False,
    is_need_check: bool = False,
) -> SlabMatchChallenge:
    return SlabMatchChallenge(
        exercise_id=exercise_id,
        title="easy-web",
        description="Find the flag.",
        challenge_type="Web",
        difficulty="EASY",
        points="100",
        attachments=attachments if attachments is not None else [{"name": "attachment.zip", "url": "https://files.example/attachment.zip", "ext": "zip"}],
        endpoints=endpoints or [],
        has_solved=False,
        is_need_init=is_need_init,
        is_need_check=is_need_check,
        raw={},
        match_info={"note": "Read the notice", "rule": "No brute force"},
    )


def test_slab_match_client_uses_expected_endpoints_and_header(tmp_path: Path) -> None:
    calls: list[tuple[str, str, dict | None, str | None]] = []

    def request(url: str, *, method: str, payload: dict | None, access_key: str | None):
        calls.append((url, method, payload, access_key))
        if url.endswith("/match/notice/match-info"):
            return {"code": "00000", "message": "", "data": {"note": "note", "rule": "rule"}}
        if url.endswith("/ctf/exercise-list"):
            return {"code": "00000", "message": "", "data": [{"id": 10, "name": "Web", "corpus": [{"id": 1001, "name": "easy-web", "hasSolved": False, "isOpen": True}, {"id": 1002, "name": "closed", "isOpen": False}]}]}
        if "/ctf/exercise?" in url:
            return {"code": "00000", "message": "", "data": {"id": 1001, "name": "easy-web", "description": "desc", "difficulty": "EASY", "score": "100", "attachment": {"files": [{"name": "a.zip", "url": "https://files.example/a.zip", "ext": "zip"}]}, "endpoints": [], "isNeedInit": False, "isNeedCheck": False}}
        if url.endswith("/ctf/build-exercise-env"):
            return {"code": "00000", "message": "", "data": {}}
        if url.endswith("/ctf/recover-exercise-env"):
            return {"code": "00000", "message": "", "data": {}}
        if url.endswith("/match/notice/now-list"):
            return {"code": "00000", "message": "", "data": [{"id": 501, "title": "notice"}]}
        if url.endswith("/match/notice/detail?id=501"):
            return {"code": "00000", "message": "", "data": {"id": 501, "title": "notice", "content": "updated statement"}}
        if url.endswith("/answer-panel/answer"):
            return {"code": "00000", "message": "", "data": {"isCorrect": True}}
        raise AssertionError(url)

    client = SlabMatchClient(_settings(tmp_path), request_json=request)
    challenge = client.list_challenges()[0]
    assert challenge.exercise_id == 1001
    assert challenge.attachments[0]["url"] == "https://files.example/a.zip"
    assert client.notice_list()[0]["id"] == 501
    assert client.notice_detail(501)["content"] == "updated statement"
    assert client.submit(1001, "flag{ok}") is True
    assert all(call[3] == "agent-key" for call in calls)
    assert calls[-1][2] == {"exerciseId": 1001, "flag": "flag{ok}"}


def test_slab_match_normalizes_origin_and_agent_endpoint_urls() -> None:
    expected = "https://agent.example/slab-match/api/v1/agent"
    assert normalize_slab_match_base_url("https://agent.example") == expected
    assert normalize_slab_match_base_url(f"{expected}/ctf/exercise-list") == expected
    assert normalize_slab_match_base_url(f"{expected}/ctf/exercise?exerciseId=1001") == expected


def test_slab_match_redirects_cannot_leak_access_key_or_reach_private_download_targets() -> None:
    api_redirect = _SlabMatchRedirectHandler("https://agent.example", allow_cross_origin=False)
    credentialed = Request("https://agent.example/start", headers={"X-Agent-AccessKey": "secret"})
    with pytest.raises(SlabMatchError, match="cross-origin"):
        api_redirect.redirect_request(
            credentialed,
            None,
            302,
            "Found",
            {},
            "https://redirect.example/capture",
        )

    attachment_redirect = _SlabMatchRedirectHandler(
        "https://agent.example",
        allow_cross_origin=True,
        public_targets_only=True,
    )
    with pytest.raises(ValueError, match="local or metadata"):
        attachment_redirect.redirect_request(
            credentialed,
            None,
            302,
            "Found",
            {},
            "http://169.254.169.254/latest/meta-data",
        )


def test_slab_match_parses_nested_string_and_variant_attachment_fields() -> None:
    challenge = SlabMatchClient.parse_challenge({
        "id": 10663,
        "name": "解压缩",
        "attachment": '{"fileList":[{"fileName":"archive.zip","fileUrl":"/downloads/archive.zip"}]}',
        "attachments": [{"filename": "second.7z", "downloadUrl": "https://files.example/second.7z"}],
    })

    assert challenge.attachments == [
        {"name": "archive.zip", "url": "/downloads/archive.zip", "ext": ""},
        {"name": "second.7z", "url": "https://files.example/second.7z", "ext": ""},
    ]


def test_slab_match_recursively_parses_provider_specific_attachment_wrappers() -> None:
    challenge = SlabMatchClient.parse_challenge({
        "id": 10663,
        "name": "解压缩",
        "attachmentPayload": {"payload": {"objects": [{"fileName": "archive.zip", "ossUrl": "/download/archive.zip"}]}},
    })

    assert challenge.attachments == [{"name": "archive.zip", "url": "/download/archive.zip", "ext": ""}]


def test_slab_match_incorrect_answer_code_is_a_rejection(tmp_path: Path) -> None:
    client = SlabMatchClient(
        _settings(tmp_path),
        request_json=lambda *args, **kwargs: {"code": "ANSWER_WRONG", "message": "答案错误", "data": None},
    )

    assert client.submit_result(1001, "flag{wrong}") is False


def test_slab_match_connection_test_only_reads_the_challenge_list(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    class Client:
        def __init__(self, settings):
            calls.append("init")

        def exercise_list(self):
            calls.append("exercise_list")
            return [{"corpus": [{"id": 1, "isOpen": True}, {"id": 2, "isOpen": False}]}]

    monkeypatch.setattr("aurora.services.slab_match.SlabMatchClient", Client)

    result = check_slab_match_connection(_settings(tmp_path))

    assert calls == ["init", "exercise_list"]
    assert result["api"]["challenge_count"] == 1
    assert result["endpoint"]["status"] == "unverified"


def test_slab_match_client_serializes_requests_across_threads(tmp_path: Path) -> None:
    active = 0
    max_active = 0
    lock = threading.Lock()

    def request(*args, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return {"code": "00000", "data": {}}

    client = SlabMatchClient(_settings(tmp_path), request_json=request, request_interval_seconds=0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: client.overview(), range(8)))

    assert max_active == 1


def test_slab_match_start_authorizes_returned_endpoint_and_appends_credentials(tmp_path: Path) -> None:
    class Client:
        recovered = []

        def get_exercise(self, exercise_id):
            assert exercise_id == 1001
            return _challenge(is_need_init=True)

        def build_environment(self, exercise_id):
            assert exercise_id == 1001
            return {}

        def wait_until_ready(self, exercise_id):
            assert exercise_id == 1001
            return _challenge(
                endpoints=[
                    {
                        "exposeIps": ["10.0.0.10"],
                        "ports": ["80", "22"],
                        "users": [{"username": "root", "password": "password"}],
                        "proxyIps": [],
                        "portMappings": [],
                        "isProxy": False,
                    }
                ],
                is_need_init=False,
                is_need_check=False,
            )

        def recover_environment(self, exercise_id):
            self.recovered.append(exercise_id)
            return {}

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    settings = _settings(tmp_path)
    with Session(engine) as session:
        project = Project(name="challenge", goal="solve", challenge_type="web")
        group = ChallengeGroup(name="group")
        session.add_all([project, group])
        session.commit()
        session.add(AuthorizationScope(project_id=project.id))
        session.add(ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, competition_meta={"platform": "slab_match", "exercise_id": 1001, "is_need_init": True}))
        session.commit()

        platform_client = Client()
        adapter = SlabMatchCompetitionAdapter(settings, platform_client)
        assert adapter.ensure_environment(session, project_id=project.id).available
        session.refresh(project)
        scope = session.exec(select(AuthorizationScope).where(AuthorizationScope.project_id == project.id)).one()
        assert project.target_url == "http://10.0.0.10:80"
        assert "root:password" in project.goal
        assert scope.allowed_hosts == ["10.0.0.10"]
        adapter.close_environment(project_id=project.id, session=session)
        adapter.close_environment(project_id=project.id, session=session)
        session.refresh(project)
        session.refresh(scope)
        assert platform_client.recovered == [1001]
        assert project.target_url is None
        assert scope.allowed_hosts == []


def test_slab_match_does_not_accept_an_endpoint_while_environment_is_preparing(tmp_path: Path) -> None:
    preparing = _challenge(
        endpoints=[{"exposeIps": ["10.0.0.10"], "ports": ["80"], "isProxy": False}],
        is_need_check=True,
    )

    class Client:
        recovered = []

        def get_exercise(self, exercise_id):
            return preparing

        def wait_until_ready(self, exercise_id):
            return preparing

        def recover_environment(self, exercise_id):
            self.recovered.append(exercise_id)
            return {}

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="challenge", goal="solve", challenge_type="web")
        group = ChallengeGroup(name="group")
        session.add_all([project, group])
        session.commit()
        session.add(ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, competition_meta={"platform": "slab_match", "exercise_id": 1001, "endpoints": preparing.endpoints}))
        session.commit()

        client = Client()
        health = SlabMatchCompetitionAdapter(_settings(tmp_path), client).ensure_environment(session, project_id=project.id)

        session.refresh(project)
        session.refresh(session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.project_id == project.id)).one())
        assert health.available is False
        assert "still preparing" in str(health.reason)
        assert project.target_url is None
        assert client.recovered == [1001]


def test_slab_match_static_attachment_item_runs_without_environment(tmp_path: Path) -> None:
    class Client:
        def get_exercise(self, exercise_id):
            return _challenge()

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="static", goal="analyze", challenge_type="reverse")
        group = ChallengeGroup(name="group", limits={"max_dynamic_environments": 1})
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, competition_meta={"platform": "slab_match", "exercise_id": 1001, "requires_environment": False, "attachments": [{"artifact_id": "artifact-one"}]})
        session.add(item)
        session.commit()

        health = SlabMatchCompetitionAdapter(_settings(tmp_path), Client()).ensure_environment(session, project_id=project.id)

        session.refresh(item)
        assert health.available is True
        assert item.competition_meta["container_status"] == "not_required"
        assert item.competition_meta["attachments"][0]["artifact_id"] == "artifact-one"
        SlabMatchCompetitionAdapter(_settings(tmp_path), Client()).close_environment(project_id=project.id, session=session)
        session.commit()
        session.refresh(item)
        assert item.competition_meta["container_status"] == "not_required"


def test_slab_match_direct_import_preserves_platform_order_and_planner_metadata(tmp_path: Path) -> None:
    static = _challenge(exercise_id=1001)
    dynamic = _challenge(exercise_id=1002, is_need_init=True)

    class Client:
        base_url = "https://agent.example/slab-match/api/v1/agent"

        def list_challenges(self):
            return [dynamic, static]

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    importer = SlabMatchDirectImporter(_settings(tmp_path), Client(), downloader=lambda url, limit: (b"PK\x03\x04fixture", "application/zip"))
    with Session(engine) as session:
        result = importer.import_all(session)

        group = session.get(ChallengeGroup, result["group"].id)
        items = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group.id).order_by(ChallengeGroupItem.position)).all()
        artifacts = session.exec(select(Artifact)).all()
        assert group.status == "READY"
        assert group.max_concurrent == 1
        assert group.limits["max_dynamic_environments"] == 2
        assert group.limits["managed_by"] == "planner"
        assert items[0].competition_meta["exercise_id"] == 1002
        assert items[0].competition_meta["requires_environment"] is True
        assert items[1].competition_meta["requires_environment"] is False
        assert len(artifacts) == 2
        assert result["attachment_first_count"] == 1


def test_slab_match_direct_import_resolves_relative_attachment_urls(tmp_path: Path) -> None:
    challenge = _challenge(attachments=[{"name": "archive.zip", "url": "/downloads/archive.zip", "ext": "zip"}])
    downloads: list[str] = []

    class Client:
        base_url = "https://agent.example/slab-match/api/v1/agent"

        def list_challenges(self):
            return [challenge]

    def download(url, limit):
        downloads.append(url)
        return b"PK\x03\x04fixture", "application/zip"

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    importer = SlabMatchDirectImporter(_settings(tmp_path), Client(), downloader=download)
    with Session(engine) as session:
        result = importer.import_all(session)
        item = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == result["group"].id)).one()

        assert downloads == ["https://agent.example/downloads/archive.zip"]
        assert item.competition_meta["attachments"][0]["status"] == "staged"
        assert item.competition_meta["attachments"][0]["artifact_id"]


def test_slab_match_attachment_download_falls_back_from_api_relative_to_origin_relative(tmp_path: Path) -> None:
    challenge = _challenge(attachments=[{"name": "archive", "url": "files/archive", "ext": "zip"}])
    downloads: list[str] = []

    class Client:
        base_url = "https://agent.example/slab-match/api/v1/agent"

        def list_challenges(self):
            return [challenge]

    def download(url, limit):
        downloads.append(url)
        if "/slab-match/api/v1/agent/" in url:
            return b"<html>not found</html>", "text/html"
        return b"PK\x03\x04fixture", "application/zip"

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        result = SlabMatchDirectImporter(_settings(tmp_path), Client(), downloader=download).import_all(session)
        item = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == result["group"].id)).one()

        assert downloads == [
            "https://agent.example/slab-match/api/v1/agent/files/archive",
            "https://agent.example/files/archive",
        ]
        assert item.competition_meta["attachments"][0]["filename"] == "archive.zip"


def test_slab_match_repairs_missing_group_attachments_idempotently(tmp_path: Path) -> None:
    challenge = _challenge(exercise_id=10663, attachments=[{"name": "archive.zip", "url": "/downloads/archive.zip", "ext": "zip"}])

    class Client:
        base_url = "https://agent.example/slab-match/api/v1/agent"

        def get_exercise(self, exercise_id):
            assert exercise_id == 10663
            return challenge

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    importer = SlabMatchDirectImporter(_settings(tmp_path), Client(), downloader=lambda url, limit: (b"PK\x03\x04fixture", "application/zip"))
    with Session(engine) as session:
        project = Project(name="解压缩", goal="extract", challenge_type="misc")
        group = ChallengeGroup(name="Slab Match", limits={"platform": "slab_match", "base_url": Client.base_url})
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            competition_meta={"platform": "slab_match", "exercise_id": 10663, "requires_environment": False, "attachments": []},
        )
        session.add(item)
        session.commit()

        first = importer.repair_group_attachments(session, group.id)
        second = importer.repair_group_attachments(session, group.id)

        session.refresh(item)
        assert first["repaired_projects"] == 1
        assert first["artifact_count"] == 1
        assert first["failures"] == []
        assert second["artifact_count"] == 0
        assert item.competition_meta["attachment_only"] is True
        assert item.competition_meta["attachments"][0]["artifact_id"]
        assert len(session.exec(select(Artifact).where(Artifact.project_id == project.id)).all()) == 1


def test_slab_match_submit_flag_marks_completion(tmp_path: Path) -> None:
    class Client:
        def submit_result(self, exercise_id, flag):
            assert (exercise_id, flag) == (1001, "flag{ok}")
            return True

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="challenge", goal="solve")
        group = ChallengeGroup(name="group")
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, competition_meta={"platform": "slab_match", "exercise_id": 1001})
        session.add(item)
        session.commit()

        result = SlabMatchCompetitionAdapter(_settings(tmp_path), Client()).submit_flag(session, project_id=project.id, value="flag{ok}")

        assert isinstance(result, CompetitionSubmissionResult)
        assert result.correct is True
        assert result.completed is True


def test_slab_match_submits_only_brace_payload_when_rule_requires_it(tmp_path: Path) -> None:
    class Client:
        def submit_result(self, exercise_id, flag):
            assert (exercise_id, flag) == (10663, "ni_cai?")
            return True

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="解压缩", goal="flag格式为DASCTF{}")
        group = ChallengeGroup(name="group")
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            competition_meta={
                "platform": "slab_match",
                "exercise_id": 10663,
                "match_info": {"rule": "flag格式均为DASCTF{}或者flag{}，提交时仅需提交{}内内容即可。"},
            },
        )
        session.add(item)
        session.commit()

        result = SlabMatchCompetitionAdapter(_settings(tmp_path), Client()).submit_flag(
            session,
            project_id=project.id,
            value="DASCTF{ni_cai?}",
        )

        assert isinstance(result, CompetitionSubmissionResult)
        assert result.correct is True
        assert result.detail["submission_format"] == "brace_payload"


def test_slab_match_prefers_proxy_and_preserves_https_service_scheme(tmp_path: Path) -> None:
    project = Project(name="challenge", goal="solve", challenge_type="web")
    addresses = SlabMatchCompetitionAdapter._endpoint_addresses(
        project,
        [{
            "exposeIps": ["10.0.0.10"],
            "ports": ["443"],
            "proxyIps": ["1.2.3.4"],
            "portMappings": [{"type": "tcp", "port": "443", "proxy": "30443"}],
            "isProxy": True,
        }],
    )

    assert addresses == ["https://1.2.3.4:30443", "https://10.0.0.10:443"]


def test_slab_match_normalizes_real_world_host_and_service_endpoint_fields() -> None:
    project = Project(name="challenge", goal="solve", challenge_type="pwn")

    addresses = SlabMatchCompetitionAdapter._endpoint_addresses(project, [{
        "exposeIps": ["1.14.76.59:15975"],
        "ports": ["nc/9999"],
        "proxyIps": ["1.14.76.59"],
        "portMappings": [{"type": "nc", "port": "9999", "proxy": "15975"}],
        "isProxy": True,
    }])

    assert addresses == ["tcp://1.14.76.59:15975"]
    assert SlabMatchCompetitionAdapter._environment_note("pwn", [{
        "exposeIps": ["1.14.76.59:15975"],
        "ports": ["nc/9999"],
    }]) == "Reachable endpoints: 1.14.76.59:15975"


def test_slab_match_notice_poller_persists_context_and_files_idempotently(tmp_path: Path) -> None:
    class Client:
        base_url = "https://agent.example/slab-match/api/v1/agent"

        def notice_list(self):
            return [{"id": 501, "title": "题目修正", "content": "请重新检查压缩包", "createdTime": 1780000000000}]

        def notice_detail(self, notice_id):
            assert notice_id == 501
            return {
                "id": 501,
                "title": "题目修正",
                "content": "附件已替换，口令在题目描述中。",
                "file": {"files": [{"name": "replacement.zip", "url": "/files/replacement.zip", "ext": "zip"}]},
            }

        def download_attachment(self, url, limit):
            assert url == "https://agent.example/files/replacement.zip"
            return b"PK\x03\x04replacement", "application/zip"

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="archive", goal="extract", challenge_type="misc")
        group = ChallengeGroup(name="Slab Match", limits={"platform": "slab_match", "base_url": Client.base_url})
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            competition_meta={"platform": "slab_match", "exercise_id": 10663, "attachments": []},
        )
        session.add(item)
        session.commit()

        poller = SlabMatchNoticePoller(_settings(tmp_path), Client())
        first = poller.poll_once(session)
        second = poller.poll_once(session)

        session.refresh(item)
        session.refresh(group)
        artifacts = session.exec(select(Artifact).where(Artifact.project_id == project.id)).all()
        facts = session.exec(select(Fact).where(Fact.project_id == project.id, Fact.category == "competition_notice")).all()
        assert first == {"groups_checked": 1, "notices_added": 1, "projects_updated": 1, "failures": []}
        assert second == {"groups_checked": 1, "notices_added": 0, "projects_updated": 0, "failures": []}
        assert group.limits["notice_ids"] == ["501"]
        assert item.competition_meta["notices"][0]["content"] == "附件已替换，口令在题目描述中。"
        assert len(artifacts) == 2
        assert len(facts) == 1


def test_competition_group_has_no_solver_turn_limit() -> None:
    item = ChallengeGroupItem(group_id="group", project_id="project", position=1, phase=2)
    limits = ChallengeGroupRunner._autorun_limits(item)

    assert limits.max_iterations == 0
    assert limits.no_progress_limit == 0


def test_slab_match_configuration_api_saves_tests_and_clears_access_key(monkeypatch) -> None:
    settings = get_settings()
    original = (settings.slab_match_base_url, settings.slab_match_access_key, settings.slab_match_timeout_seconds, settings.slab_match_max_concurrent)
    monkeypatch.setattr(
        "aurora.api.test_slab_match_connection",
        lambda: {
            "api": {"status": "reachable", "challenge_count": 2, "match_info": {}},
            "endpoint": {"status": "reachable", "address": "10.0.0.10:80", "message": "reachable"},
            "progress": {"stagePoint": 88.5, "stageRank": 7},
        },
    )
    client = TestClient(create_app())
    try:
        saved = client.put(
            "/api/settings/slab-match",
            json={
                "base_url": "https://agent.example",
                "access_key": "secret-key",
                "timeout_seconds": 15,
                "max_concurrent": 2,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["base_url"] == "https://agent.example/slab-match/api/v1/agent"
        assert saved.json()["access_key_configured"] is True
        assert "agent_concurrency" not in saved.json()
        assert "secret-key" not in saved.text
        assert "access_key" not in saved.json()

        tested = client.post("/api/settings/slab-match/test")
        assert tested.status_code == 200
        assert tested.json()["endpoint"]["status"] == "reachable"

        cleared = client.put(
            "/api/settings/slab-match",
            json={
                "base_url": "https://agent.example",
                "clear_access_key": True,
                "timeout_seconds": 15,
                "max_concurrent": 2,
            },
        )
        assert cleared.status_code == 200
        assert cleared.json()["access_key_configured"] is False
    finally:
        settings.slab_match_base_url, settings.slab_match_access_key, settings.slab_match_timeout_seconds, settings.slab_match_max_concurrent = original


def test_slab_match_direct_import_api_uses_dedicated_importer(monkeypatch, tmp_path: Path) -> None:
    calls = []

    class Importer:
        def import_all(self, session):
            calls.append("import")
            group = ChallengeGroup(name="direct", max_concurrent=1, limits={"managed_by": "planner"})
            session.add(group)
            session.commit()
            session.refresh(group)
            return {"group": group, "projects": [], "challenge_count": 0, "attachment_first_count": 0, "max_environments": 1}

    engine = create_engine(f"sqlite:///{tmp_path / 'direct-import.db'}")
    SQLModel.metadata.create_all(engine)
    app = create_app()

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    monkeypatch.setattr("aurora.api.SlabMatchDirectImporter", Importer)
    response = TestClient(app).post("/api/slab-match/import")

    assert response.status_code == 200
    assert response.json()["group"]["status"] == "READY"
    assert response.json()["group"]["max_concurrent"] == 1
    assert calls == ["import"]


def test_slab_match_runtime_configuration_does_not_expose_access_key() -> None:
    settings = get_settings()
    original = (settings.slab_match_base_url, settings.slab_match_access_key, settings.slab_match_timeout_seconds, settings.slab_match_max_concurrent)
    try:
        configured = configure_slab_match(base_url="https://agent.example", access_key="secret-key", clear_access_key=False, timeout_seconds=15, max_concurrent=2)
        assert configured["access_key_configured"] is True
        assert "secret-key" not in str(configured)
        assert public_slab_match_config()["base_url"] == "https://agent.example/slab-match/api/v1/agent"
    finally:
        settings.slab_match_base_url, settings.slab_match_access_key, settings.slab_match_timeout_seconds, settings.slab_match_max_concurrent = original


def test_slab_match_group_uses_platform_concurrency_limit() -> None:
    settings = get_settings()
    original = settings.slab_match_max_concurrent
    settings.slab_match_max_concurrent = 2
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            group = ChallengeGroup(name="slab", max_concurrent=8)
            project = Project(name="one", goal="solve")
            session.add_all([group, project])
            session.commit()
            session.add(ChallengeGroupItem(
                group_id=group.id,
                project_id=project.id,
                position=1,
                competition_meta={"platform": "slab_match", "exercise_id": 1001},
            ))
            session.commit()
            assert ChallengeGroupRunner._is_slab_match_group(session, group.id) is True
            assert ChallengeGroupRunner._max_workers(session, group) == 2
    finally:
        settings.slab_match_max_concurrent = original


def test_unconfigured_slab_match_never_falls_back_to_local_solver() -> None:
    settings = get_settings()
    original = settings.slab_match_access_key
    settings.slab_match_access_key = None
    try:
        item = ChallengeGroupItem(
            group_id="group_test",
            project_id="project_test",
            position=1,
            competition_meta={"platform": "slab_match", "exercise_id": 1001},
        )
        adapter = ChallengeGroupRunner()._competition_for(item)
        assert isinstance(adapter, SlabMatchCompetitionAdapter)
        assert adapter.settings.slab_match_configured is False
    finally:
        settings.slab_match_access_key = original
