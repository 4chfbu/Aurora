from pathlib import Path
import hashlib
import time

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from aurora.config import Settings
from aurora.models import Artifact, Attempt, AuthorizationScope, ChallengeGroup, ChallengeGroupEvent, ChallengeGroupItem, Fact, Finding, FlagCandidate, Intent, Project, Worker, WorkerEvent, now_utc
from aurora.services.hands_free import MAX_ATTACHMENT_BYTES, HandsFreeService
from aurora.services.challenge_group_runner import ChallengeGroupRegistry, ChallengeGroupRunner, GroupRunState
from aurora.services.challenge_group_runner import fail_group_run, recover_interrupted_groups, recover_legacy_target_blocked_groups
from aurora.services.harvester_runner import HarvesterResult


def test_cataloger_configuration_is_optional_for_deterministic_collection(tmp_path: Path) -> None:
    service = HandsFreeService(
        settings=Settings(artifact_dir=tmp_path, cataloger_llm_api_key=None, cataloger_agent_enabled=False),
        fetch_text=lambda url: ("<html><title>Empty catalog</title></html>", url),
    )
    with Session(service_session_engine()) as session:
        result = service.scan(session, "https://catalog.example/tasks")
        assert result.batch.status == "READY"
        assert result.batch.diagnostics_json[-1]["code"] == "BROWSER_UNAVAILABLE"
        assert result.candidates == []


def test_ctfplus_problem_bank_uses_api_and_respects_url_pagination(tmp_path: Path) -> None:
    requests: list[tuple[str, dict, str | None]] = []

    def post_json(url: str, payload: dict, cookie: str | None) -> dict:
        requests.append((url, payload, cookie))
        return {"code": 200, "data": {"total": 28, "problems": [
            {"id": "2068896697408294912", "name": "ezunser", "desc": "Web challenge", "publicId": "P7815", "difficulty": 1, "tags": [{"name": "Web"}, {"name": "浙江警察学院第九届信息网络安全竞赛决赛"}], "attachments": []},
            {"id": "2068896779608264704", "name": "re_signin", "desc": "Reverse challenge", "publicId": "P7827", "difficulty": 1, "tags": [{"name": "REVERSE"}], "attachments": []},
        ]}}

    source_url = "https://www.ctfplus.cn/learning/problem/problem-bank?page=1&favoriteId=2074085927801589760&size=20&tags=%E6%B5%99%E6%B1%9F%E8%AD%A6%E5%AF%9F%E5%AD%A6%E9%99%A2%E7%AC%AC%E4%B9%9D%E5%B1%8A%E4%BF%A1%E6%81%AF%E7%BD%91%E7%BB%9C%E5%AE%89%E5%85%A8%E7%AB%9E%E8%B5%9B%E5%86%B3%E8%B5%9B"
    service = HandsFreeService(
        settings=Settings(artifact_dir=tmp_path, cataloger_llm_api_key=None),
        fetch_text=lambda url: ("<html><title>CTF+</title><body><div id='root'></div></body></html>", url),
        post_json=post_json,
        ctfplus_attachment_collector=lambda _candidates, _cookie: None,
    )
    with Session(service_session_engine()) as session:
        result = service.scan(session, source_url)
        assert result.batch.status == "READY"
        assert "CTF+ API identified 2 problem(s) across 2 page(s)" in (result.batch.summary or "")
        assert [candidate.title for candidate in result.candidates] == ["ezunser", "re_signin"]
        assert result.candidates[0].challenge_type == "web"
        assert result.candidates[1].challenge_type == "reverse"
        assert result.candidates[0].challenge_url.endswith("/learning/problem/problem-detail/2068896697408294912/description")
        assert "浙江警察学院第九届信息网络安全竞赛决赛" in (result.candidates[0].description or "")
    expected = {
        "order": 3, "name": "", "tags": ["浙江警察学院第九届信息网络安全竞赛决赛"], "publicType": -1,
        "problemType": -1, "problemTypeGroup": -1, "isSolved": -1, "favoriteId": "2074085927801589760",
        "payment": {}, "page": {"page": 1, "size": 20},
    }
    assert requests == [
        ("https://www.ctfplus.cn/api/problem/searchPublicProblem", expected, None),
        ("https://www.ctfplus.cn/api/problem/searchPublicProblem", {**expected, "page": {"page": 2, "size": 20}}, None),
    ]


def test_ctfd_uses_platform_api_and_rejects_navigation_assets(tmp_path: Path) -> None:
    source_url = "https://play.example/challenges"
    html = """
    <html><title>scriptCTF</title><body x-data="ChallengeBoard">
      <a href="/challenges">Challenges</a>
      <a href="https://ctfd.io">Powered by CTFd</a>
      <script src="/themes/MagicTheme/static/assets/challenges.js"></script>
      <img src="/files/logo.png" alt="scriptCTF">
    </body></html>
    """
    requests: list[str] = []

    def fetch_json(url: str, _cookie: str | None) -> dict:
        requests.append(url)
        if url.endswith("/api/v1/challenges"):
            return {"success": True, "data": [{"id": 15, "name": "Rules", "category": "Misc", "value": 348}]}
        assert url.endswith("/api/v1/challenges/15")
        return {"success": True, "data": {"id": 15, "name": "Rules", "category": "Misc", "description": "Read the rules", "value": 348, "files": ["/files/rules.zip"]}}

    service = HandsFreeService(
        settings=Settings(artifact_dir=tmp_path, cataloger_llm_api_key=None),
        fetch_text=lambda url: (html, url),
        fetch_json=fetch_json,
        fetch_bytes=lambda _url, _limit: (b"PK\x03\x04rules", "application/zip"),
        ctfplus_attachment_collector=lambda _candidates, _cookie: None,
    )
    with Session(service_session_engine()) as session:
        result = service.scan(session, source_url)
        assert result.batch.platform == "ctfd"
        assert result.batch.extraction_strategy == "platform_api"
        assert [candidate.title for candidate in result.candidates] == ["Rules"]
        candidate = result.candidates[0]
        assert candidate.challenge_url == f"{source_url}#challenge-15"
        assert candidate.source_metadata_json["challenge_id"] == "15"
        assert candidate.staged_attachments_json[0]["filename"] == "rules.zip"
    assert requests == [f"https://play.example/api/v1/challenges", f"https://play.example/api/v1/challenges/15"]


def test_ctfd_stages_trusted_external_description_attachments(tmp_path: Path) -> None:
    source_url = "https://play.example/challenges"
    html = "<html><title>scriptCTF</title><body x-data='ChallengeBoard'></body></html>"
    downloaded: list[str] = []

    def fetch_json(url: str, _cookie: str | None) -> dict:
        if url.endswith("/api/v1/challenges"):
            return {"success": True, "data": [{"id": 26, "name": "Bruteforced"}]}
        return {"success": True, "data": {
            "id": 26,
            "name": "Bruteforced",
            "category": "Forensics",
            "description": (
                "Read the [event site](https://unrelated.example/info).\n"
                "## Attachments\n"
                "* [log.pcap](https://cdn.example/log.pcap)\n"
                "* [chall](https://bucket.s3.amazonaws.com/chall)\n"
                "* [large.zip](https://cdn.example/large.zip)\n"
                "* [VM mirror](https://drive.google.com/file/d/example/view)\n"
                "## Notes\n"
                "See [writeup](https://unrelated.example/writeup.zip)."
            ),
        }}

    def fetch_bytes(url: str, _limit: int) -> tuple[bytes, str | None]:
        downloaded.append(url)
        if url.endswith("large.zip"):
            raise ValueError("attachment exceeds size limit")
        if url.endswith("chall"):
            return b"\x7fELFpayload", "application/octet-stream"
        return b"\xd4\xc3\xb2\xa1pcap", "application/octet-stream"

    service = HandsFreeService(
        settings=Settings(artifact_dir=tmp_path, cataloger_llm_api_key=None),
        fetch_text=lambda url: (html, url),
        fetch_json=fetch_json,
        fetch_bytes=fetch_bytes,
        ctfplus_attachment_collector=lambda _candidates, _cookie: None,
    )
    with Session(service_session_engine()) as session:
        result = service.scan(session, source_url)
        candidate = result.candidates[0]
        assert [item["filename"] for item in candidate.staged_attachments_json] == ["log.pcap", "chall.elf"]
        assert downloaded == ["https://cdn.example/log.pcap", "https://bucket.s3.amazonaws.com/chall", "https://cdn.example/large.zip"]
        assert candidate.external_attachments_json == [{
            "url": "https://cdn.example/large.zip",
            "status": "external_review_required",
            "reason": "attachment exceeds size limit",
        }, {
            "url": "https://drive.google.com/file/d/example/view",
            "status": "external_review_required",
        }]


def test_ctfd_extracts_html_attachment_section_only() -> None:
    description = """
    <p><a href="https://unrelated.example/rules.pdf">Rules</a></p>
    <h2>Attachments</h2>
    <p><a href="/files/challenge.zip">challenge.zip</a></p>
    <h2>References</h2>
    <p><a href="https://unrelated.example/reference.zip">Reference</a></p>
    """

    assert HandsFreeService._ctfd_description_attachment_urls(description, "https://play.example/challenges") == [
        "https://play.example/files/challenge.zip"
    ]


def test_ctfplus_detail_attachments_are_staged_when_search_api_omits_them(tmp_path: Path) -> None:
    def post_json(_: str, __: dict, ___: str | None) -> dict:
        return {"code": 200, "data": {"total": 1, "problems": [{"id": "42", "name": "attached", "attachments": []}]}}

    def collect(candidates: list[dict], _: str | None) -> None:
        candidates[0]["_downloaded_attachments"] = [{"filename": "challenge.zip", "data": b"zip-data", "mime_type": "application/zip", "source_url": candidates[0]["challenge_url"]}]
        candidates[0]["_attachment_issues"] = [{"filename": "runtime", "status": "dynamic_attachment", "reason": "dynamic"}]

    service = HandsFreeService(
        settings=Settings(artifact_dir=tmp_path, cataloger_llm_api_key=None),
        fetch_text=lambda url: ("<html><title>CTF+</title></html>", url),
        post_json=post_json,
        ctfplus_attachment_collector=collect,
    )
    with Session(service_session_engine()) as session:
        result = service.scan(session, "https://www.ctfplus.cn/learning/problem/problem-bank?page=1&size=20")
        candidate = result.candidates[0]
        assert candidate.staged_attachments_json[0]["filename"] == "challenge.zip"
        assert candidate.staged_attachments_json[0]["size"] == len(b"zip-data")
        assert candidate.external_attachments_json == [{"filename": "runtime", "status": "dynamic_attachment", "reason": "dynamic"}]


def test_ctfplus_all_attachment_sessions_required_pauses_import(tmp_path: Path) -> None:
    def post_json(_: str, __: dict, ___: str | None) -> dict:
        return {"code": 200, "data": {"total": 1, "problems": [{"id": "42", "name": "private", "attachments": []}]}}

    def collect(candidates: list[dict], _: str | None) -> None:
        candidates[0]["_attachment_needs_session"] = True

    service = HandsFreeService(settings=Settings(artifact_dir=tmp_path, cataloger_llm_api_key=None), fetch_text=lambda url: ("<html></html>", url), post_json=post_json, ctfplus_attachment_collector=collect)
    with Session(service_session_engine()) as session:
        result = service.scan(session, "https://www.ctfplus.cn/learning/problem/problem-bank?page=1&size=20")
        assert result.batch.status == "NEEDS_SESSION"


def test_generic_detail_browser_download_is_staged(tmp_path: Path) -> None:
    def collect(candidates: list[dict], _: str | None) -> None:
        candidates[0]["_downloaded_attachments"] = [{"filename": "client.bin", "data": b"browser-download", "mime_type": "application/octet-stream", "source_url": candidates[0]["challenge_url"]}]

    service = HandsFreeService(
        settings=Settings(artifact_dir=tmp_path, cataloger_llm_api_key="test-key"),
        fetch_text=lambda url: ("<a href='/tasks/one'>One</a>", url),
        cataloger=lambda _: {"summary": "", "candidates": [{"title": "One", "challenge_url": "https://catalog.example/tasks/one", "attachment_urls": [], "confidence": 1}]},
        ctfplus_attachment_collector=collect,
    )
    with Session(service_session_engine()) as session:
        result = service.scan(session, "https://catalog.example/tasks")
        assert result.candidates[0].staged_attachments_json[0]["filename"] == "client.bin"


def test_generic_cataloger_rejects_low_confidence_candidates(tmp_path: Path) -> None:
    service = HandsFreeService(
        settings=Settings(artifact_dir=tmp_path, cataloger_llm_api_key="test-key", cataloger_agent_enabled=False),
        fetch_text=lambda url: ("<a href='/tasks/one'>Challenge One</a>", url),
        cataloger=lambda _: {"summary": "uncertain", "candidates": [{"title": "One", "challenge_url": "https://catalog.example/tasks/one", "attachment_urls": [], "confidence": 0.79}]},
        ctfplus_attachment_collector=lambda _candidates, _cookie: None,
    )
    with Session(service_session_engine()) as session:
        result = service.scan(session, "https://catalog.example/tasks")
        assert result.batch.status == "READY"
        assert result.candidates == []


def test_hands_free_stages_same_domain_attachments_and_confirms_projects(tmp_path: Path) -> None:
    downloaded: list[str] = []
    page = """
    <html><title>Summer CTF</title><body>
      <a href="/tasks/web-1">Web task</a>
      <a href="/files/web-1.zip">download</a>
      <a href="https://files.external.example/web-1.zip">mirror</a>
      <a href="/tasks/crypto-1">Crypto task</a>
    </body></html>
    """

    def fetch_text(url: str) -> tuple[str, str]:
        assert url == "https://catalog.example/tasks"
        return page, url

    def fetch_bytes(url: str, limit: int) -> tuple[bytes, str | None]:
        downloaded.append(url)
        assert limit > 100
        return b"zip-content", "application/zip"

    def cataloger(_: dict) -> dict:
        return {"summary": "two tasks", "candidates": [
            {"title": "Web One", "description": "A web challenge", "challenge_url": "https://catalog.example/tasks/web-1", "challenge_type": "web", "confidence": 0.9, "attachment_urls": ["https://catalog.example/files/web-1.zip", "https://files.external.example/web-1.zip"]},
            {"title": "Crypto One", "challenge_url": "https://catalog.example/tasks/crypto-1", "challenge_type": "crypto", "confidence": 0.8, "attachment_urls": []},
        ]}

    settings = Settings(artifact_dir=tmp_path, cataloger_llm_api_key="test-key")
    service = HandsFreeService(settings=settings, fetch_text=fetch_text, fetch_bytes=fetch_bytes, cataloger=cataloger, ctfplus_attachment_collector=lambda _candidates, _cookie: None)
    with Session(service_session_engine()) as session:
        result = service.scan(session, "https://catalog.example/tasks")
        assert result.batch.status == "READY"
        assert len(result.candidates) == 2
        web = result.candidates[0]
        assert downloaded == ["https://catalog.example/files/web-1.zip"]
        assert len(web.staged_attachments_json) == 1
        assert web.external_attachments_json == [{"url": "https://files.external.example/web-1.zip", "status": "external_review_required"}]
        project_name = f"Renamed Web {result.batch.id}"
        created = service.confirm(
            session,
            result.batch.id,
            [candidate.id for candidate in result.candidates],
            {web.id: project_name},
            ["DASCTF", "flag"],
        )
        assert {item["status"] for item in created} == {"created"}
        group_id = created[0]["group_id"]
        group = session.get(ChallengeGroup, group_id)
        assert group is not None
        assert group.flag_prefixes == ["dasctf", "flag"]
        group_items = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group_id).order_by(ChallengeGroupItem.position)).all()
        assert [item.position for item in group_items] == [1, 2]
        assert [item.project_id for item in group_items] == [created[0]["project_id"], created[1]["project_id"]]
        assert session.exec(select(Project).where(Project.name == project_name)).one()
        project_id = created[0]["project_id"]
        assert session.exec(select(Artifact).where(Artifact.project_id == project_id)).first() is not None
        scope = session.exec(select(AuthorizationScope).where(AuthorizationScope.project_id == project_id)).one()
        assert scope.allowed_hosts == []
        import_fact = session.exec(select(Fact).where(Fact.project_id == project_id, Fact.category == "import")).one()
        assert "catalog.example" not in import_fact.statement
        assert "Continue local analysis without a target" in import_fact.statement
        imported_project = session.get(Project, project_id)
        assert imported_project is not None
        assert imported_project.target_verification_status == "UNVERIFIED"
        assert "可继续分析题目与附件" in (imported_project.target_verification_reason or "")
        repeated = service.confirm(session, result.batch.id, [candidate.id for candidate in result.candidates])
        assert {item["status"] for item in repeated} == {"existing"}


def test_login_page_pauses_batch_without_persisting_credentials(tmp_path: Path) -> None:
    service = HandsFreeService(
        settings=Settings(artifact_dir=tmp_path, cataloger_llm_api_key="test-key"),
        fetch_text=lambda _: ("<form action='/login'><input type='password'></form>", "https://catalog.example/login"),
        cataloger=lambda _: {"summary": "", "candidates": []},
    )
    with Session(service_session_engine()) as session:
        result = service.scan(session, "https://catalog.example/tasks")
        assert result.batch.status == "NEEDS_SESSION"
        assert result.batch.login_domain == "catalog.example"
        assert result.batch.auth_method is None
        assert "Cookie" in (result.batch.auth_message or "")


def test_challenge_group_advances_through_terminal_projects() -> None:
    with Session(service_session_engine()) as session:
        first = Project(name="first", goal="done", status="COMPLETED")
        second = Project(name="second", goal="failed", status="FAILED")
        session.add_all([first, second])
        session.commit()
        group = ChallengeGroup(name="batch")
        session.add(group)
        session.commit()
        session.add_all([
            ChallengeGroupItem(group_id=group.id, project_id=first.id, position=1),
            ChallengeGroupItem(group_id=group.id, project_id=second.id, position=2),
        ])
        session.commit()

        ChallengeGroupRunner().run(session, group_id=group.id)
        session.refresh(group)
        items = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group.id).order_by(ChallengeGroupItem.position)).all()
        assert group.status == "COMPLETED"
        assert [item.status for item in items] == ["COMPLETED", "FAILED"]


def test_explicit_no_flag_challenge_is_terminal() -> None:
    with Session(service_session_engine()) as session:
        project = Project(name="attendance", goal="Click Submit. You don't need to input a flag.")
        group = ChallengeGroup(name="batch")
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1)
        session.add(item)
        session.commit()

        runner = ChallengeGroupRunner()
        assert runner._is_explicit_no_flag_challenge(project)
        runner._resolve_phase(
            session,
            group=group,
            item=item,
            project=project,
            outcome="NO_FLAG_COMPLETED",
            reason="statement_explicitly_requires_no_flag",
        )
        session.refresh(project)
        session.refresh(item)
        assert project.status == "COMPLETED"
        assert item.fused_status == "COMPLETED"


def test_challenge_group_runs_phase_waves_before_marking_final_failure() -> None:
    class FailedHarvester:
        def run(self, session, *, project_id, task, limits, should_stop):
            return HarvesterResult(status="FAILED", reason="no_progress")

    class HintAdapter:
        def ensure_environment(self, session, *, project_id):
            from aurora.services.competition_adapter import EnvironmentHealth
            return EnvironmentHealth(True)

        def close_environment(self, *, project_id):
            return None

        def fetch_hint(self, session, *, project_id):
            return "platform hint"

        def submit_flag(self, session, *, project_id, value):
            return False

    with Session(service_session_engine()) as session:
        projects = [Project(name="one", goal="raw one"), Project(name="two", goal="raw two")]
        session.add_all(projects)
        session.commit()
        group = ChallengeGroup(name="waves")
        session.add(group)
        session.commit()
        session.add_all([
            ChallengeGroupItem(group_id=group.id, project_id=projects[0].id, position=1, competition_meta={"solved_by_count": 0, "points": 100}),
            ChallengeGroupItem(group_id=group.id, project_id=projects[1].id, position=2, competition_meta={"solved_by_count": 0, "points": 50}),
        ])
        session.commit()

        ChallengeGroupRunner(harvester=FailedHarvester(), competition=HintAdapter()).run(session, group_id=group.id)
        session.refresh(group)
        items = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group.id).order_by(ChallengeGroupItem.position)).all()
        events = session.exec(select(ChallengeGroupEvent).where(ChallengeGroupEvent.group_id == group.id).order_by(ChallengeGroupEvent.created_at)).all()
        phase_events = [event.payload_json["phase"] for event in events if event.event_type == "group.item.phase_finished"]
        assert group.status == "COMPLETED"
        assert [(item.phase, item.fused_status, item.status) for item in items] == [(3, "FAILED", "FAILED"), (3, "FAILED", "FAILED")]
        assert all(item.hint_taken and item.hint_content == "platform hint" for item in items)
        assert phase_events == [1, 1, 2, 2, 3, 3]


def test_unavailable_target_environment_warns_but_still_dispatches_solver() -> None:
    calls: list[dict] = []

    class RecordingHarvester:
        def run(self, session, *, project_id, task, limits, should_stop):
            calls.append(task)
            return HarvesterResult(status="FAILED", reason="analysis_incomplete")

    class UnavailableEnvironmentAdapter:
        def ensure_environment(self, session, *, project_id):
            from aurora.services.competition_adapter import EnvironmentHealth
            return EnvironmentHealth(False, "target instance is not available")

        def close_environment(self, *, project_id):
            return None

        def fetch_hint(self, session, *, project_id):
            return None

        def submit_flag(self, session, *, project_id, value):
            return None

    with Session(service_session_engine()) as session:
        project = Project(name="offline-target", goal="Analyze the imported attachment.")
        group = ChallengeGroup(name="optional-target")
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1)
        session.add(item)
        session.commit()

        ChallengeGroupRunner(
            harvester=RecordingHarvester(),
            competition=UnavailableEnvironmentAdapter(),
        ).run(session, group_id=group.id)

        warnings = session.exec(
            select(ChallengeGroupEvent).where(
                ChallengeGroupEvent.group_id == group.id,
                ChallengeGroupEvent.event_type == "group.item.environment_unavailable",
            )
        ).all()
        assert len(calls) == 3
        assert all(call["target"] is None for call in calls)
        assert len(warnings) == 3
        assert all(event.payload_json["solver_continues"] is True for event in warnings)


def test_competition_flag_rejection_reopens_project_with_worker_feedback() -> None:
    class RejectingAdapter:
        def submit_flag(self, session, *, project_id, value):
            return False

    with Session(service_session_engine()) as session:
        project = Project(name="rejected", goal="find the correct flag", status="COMPLETED")
        session.add(project)
        session.commit()
        cancelled = Intent(project_id=project.id, objective="continue analysis", status="CANCELLED")
        finding = Finding(project_id=project.id, title="Candidate flag: flag{wrong}", evidence_refs=["artifact_original"])
        candidate = FlagCandidate(project_id=project.id, value="flag{wrong}", value_hash=hashlib.sha256(b"flag{wrong}").hexdigest(), status="LOCAL_VERIFIED", provenance_kind="OBSERVED", artifact_refs=["artifact_original"])
        item = ChallengeGroupItem(group_id="group_rejected", project_id=project.id, position=1)
        session.add_all([cancelled, finding, candidate, item])
        session.commit()
        session.add(
            WorkerEvent(
                project_id=project.id,
                event_type="project.completed",
                payload_json={"cancelled_intent_ids": [cancelled.id]},
            )
        )
        session.commit()

        result = ChallengeGroupRunner(competition=RejectingAdapter())._submit_pending_flag(session, item=item)

        session.refresh(project)
        session.refresh(cancelled)
        assert result is False
        assert item.submission_status == "REJECTED"
        assert project.status == "WORKING"
        assert cancelled.status == "PENDING"
        assert session.get(Finding, finding.id) is None
        feedback = session.exec(select(Fact).where(Fact.project_id == project.id, Fact.category == "flag_validation_feedback")).one()
        assert "flag{wrong}" in feedback.statement
        follow_up = session.exec(select(Intent).where(Intent.project_id == project.id, Intent.objective.contains("flag{wrong}"))).one()
        assert follow_up.status == "PENDING"


@pytest.mark.parametrize("adapter_result", [None, RuntimeError("submission endpoint invalid")])
def test_unavailable_submission_requires_manual_flag_validation(adapter_result: object) -> None:
    class UnavailableAdapter:
        def submit_flag(self, session, *, project_id, value):
            if isinstance(adapter_result, Exception):
                raise adapter_result
            return adapter_result

    with Session(service_session_engine()) as session:
        project = Project(name="manual-review", goal="verify the flag", status="FLAG_READY")
        group = ChallengeGroup(name="manual-review-group", status="RUNNING")
        session.add_all([project, group])
        session.commit()
        finding = Finding(project_id=project.id, title="Candidate flag: flag{needs_review}")
        candidate = FlagCandidate(project_id=project.id, value="flag{needs_review}", value_hash=hashlib.sha256(b"flag{needs_review}").hexdigest(), status="LOCAL_VERIFIED", provenance_kind="OBSERVED")
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, status="RUNNING", fused_status="RUNNING")
        session.add_all([finding, candidate, item])
        session.commit()

        runner = ChallengeGroupRunner(competition=UnavailableAdapter())
        runner._resolve_phase(session, group=group, item=item, project=project, outcome="CANDIDATE_READY", reason="candidate detected")

        session.refresh(group)
        session.refresh(item)
        assert group.status == "AWAITING_MANUAL_VALIDATION"
        assert item.status == "AWAITING_MANUAL_VALIDATION"
        assert item.submission_status == "AWAITING_MANUAL_VALIDATION"
        assert session.get(Finding, finding.id) is not None

        runner.validate_flag_manually(session, group_id=group.id, item_id=item.id, accepted=True)
        session.refresh(group)
        session.refresh(item)
        assert group.status == "COMPLETED"
        assert item.fused_status == "COMPLETED"
        assert item.submission_status == "MANUALLY_ACCEPTED"


def test_manual_flag_rejection_reopens_solver_instead_of_accepting_hallucination() -> None:
    with Session(service_session_engine()) as session:
        project = Project(name="hallucination", goal="find a real flag", status="COMPLETED")
        group = ChallengeGroup(name="review", status="AWAITING_MANUAL_VALIDATION")
        session.add_all([project, group])
        session.commit()
        finding = Finding(project_id=project.id, title="Candidate flag: flag{hallucinated}")
        item = ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            status="AWAITING_MANUAL_VALIDATION",
            fused_status="AWAITING_MANUAL_VALIDATION",
            submission_status="AWAITING_MANUAL_VALIDATION",
        )
        session.add_all([finding, item])
        session.commit()

        ChallengeGroupRunner().validate_flag_manually(session, group_id=group.id, item_id=item.id, accepted=False)

        session.refresh(project)
        session.refresh(group)
        session.refresh(item)
        assert project.status == "WORKING"
        assert group.status == "READY"
        assert item.fused_status == "PENDING"
        assert item.submission_status == "MANUALLY_REJECTED"
        assert session.get(Finding, finding.id) is None
        feedback = session.exec(select(Fact).where(Fact.project_id == project.id, Fact.category == "flag_validation_feedback")).one()
        assert "flag{hallucinated}" in feedback.statement


def test_manual_validation_resume_replaces_runner_that_is_still_unwinding(monkeypatch) -> None:
    registry = ChallengeGroupRegistry()
    paused = GroupRunState(group_id="group_review")
    registry._runs[paused.group_id] = paused
    resumed: list[GroupRunState] = []
    monkeypatch.setattr(registry, "_run", resumed.append)

    state = registry.resume_after_manual_validation(paused.group_id)

    deadline = time.monotonic() + 1
    while not resumed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert state is not paused
    assert registry.status(paused.group_id)["started_at"] == state.started_at
    assert resumed == [state]


def test_manual_flag_acceptance_survives_environment_cleanup_failure() -> None:
    class CleanupFailureAdapter:
        def close_environment(self, *, project_id):
            raise RuntimeError("container engine unavailable")

    with Session(service_session_engine()) as session:
        project = Project(name="cleanup-failure", goal="verify", status="AWAITING_MANUAL_VALIDATION")
        group = ChallengeGroup(name="cleanup-failure-group", status="AWAITING_MANUAL_VALIDATION")
        session.add_all([project, group])
        session.commit()
        finding = Finding(project_id=project.id, title="Candidate flag: flag{cleanup_ok}")
        candidate = FlagCandidate(project_id=project.id, value="flag{cleanup_ok}", value_hash="cleanup-ok-hash", status="AWAITING_MANUAL_VALIDATION", provenance_kind="OBSERVED")
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, status="AWAITING_MANUAL_VALIDATION", fused_status="AWAITING_MANUAL_VALIDATION", submission_status="AWAITING_MANUAL_VALIDATION")
        session.add_all([finding, candidate, item])
        session.commit()

        result = ChallengeGroupRunner(competition=CleanupFailureAdapter()).validate_flag_manually(
            session, group_id=group.id, item_id=item.id, accepted=True
        )

        assert result.submission_status == "MANUALLY_ACCEPTED"
        assert session.get(Project, project.id).status == "COMPLETED"
        cleanup_event = session.exec(
            select(ChallengeGroupEvent).where(ChallengeGroupEvent.group_id == group.id, ChallengeGroupEvent.event_type == "group.item.environment_cleanup_failed")
        ).one()
        assert cleanup_event.payload_json["error"] == "container engine unavailable"


def test_recover_interrupted_group_requeues_its_running_item() -> None:
    with Session(service_session_engine()) as session:
        project = Project(name="interrupted", goal="resume after restart")
        group = ChallengeGroup(name="batch", status="RUNNING")
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, status="RUNNING")
        intent = Intent(project_id=project.id, objective="solve", status="RUNNING", lease_owner="worker_test")
        worker = Worker(id="worker_test", project_id=project.id, intent_id=intent.id, status="RUNNING")
        attempt = Attempt(project_id=project.id, intent_id=intent.id, worker_id=worker.id, status="RUNNING")
        group.current_item_id = item.id
        session.add_all([item, intent, worker, attempt, group])
        session.commit()

        assert recover_interrupted_groups(session) == [group.id]
        session.refresh(item)
        session.refresh(intent)
        session.refresh(worker)
        session.refresh(attempt)
        session.refresh(group)
        assert item.status == "PENDING"
        assert intent.status == "PENDING"
        assert intent.lease_owner is None
        assert worker.status == "INTERRUPTED"
        assert attempt.status == "INTERRUPTED"
        assert group.current_item_id is None


def test_recover_interrupted_groups_resumes_retryable_failed_group() -> None:
    with Session(service_session_engine()) as session:
        project = Project(name="failed-runner-item", goal="resume after runner crash")
        group = ChallengeGroup(name="failed-runner-group", status="FAILED")
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            status="PENDING",
            fused_status="PENDING",
            stop_reason="recovered_after_runner_failure",
        )
        session.add(item)
        session.commit()

        assert recover_interrupted_groups(session) == [group.id]
        session.refresh(group)
        assert group.status == "READY"
        assert group.finished_at is None
        recovered = session.exec(
            select(ChallengeGroupEvent).where(
                ChallengeGroupEvent.group_id == group.id,
                ChallengeGroupEvent.event_type == "group.recovered_after_runner_failure",
            )
        ).one()
        assert recovered.payload_json["pending_items"] == 1


def test_recover_legacy_target_blocked_group_is_scoped_and_idempotent() -> None:
    with Session(service_session_engine()) as session:
        project = Project(
            name="legacy-target-block",
            goal="analyze offline",
            status="FAILED",
            target_verification_status="UNVERIFIED",
        )
        group = ChallengeGroup(name="legacy-target-block", status="COMPLETED", finished_at=now_utc())
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            status="FAILED",
            fused_status="FAILED",
            phase=3,
            phase_attempts={"1": 1, "2": 1, "3": 1},
            failure_history=[
                {"phase": 1, "outcome": "FAILED", "reason": "runtime_error"},
                {"phase": 2, "outcome": "FAILED", "reason": "runtime_error"},
            ],
            stop_reason="runtime_error",
            finished_at=now_utc(),
        )
        session.add_all([
            item,
            Intent(project_id=project.id, objective="bootstrap", status="PENDING"),
            WorkerEvent(
                project_id=project.id,
                event_type="worker.preflight_blocked",
                payload_json={"kind": "target", "status": "UNVERIFIED"},
            ),
        ])
        session.commit()

        assert recover_legacy_target_blocked_groups(session) == [group.id]
        assert recover_legacy_target_blocked_groups(session) == []
        session.refresh(project)
        session.refresh(group)
        session.refresh(item)

        assert project.status == "ACTIVE"
        assert group.status == "READY" and group.finished_at is None
        assert item.status == item.fused_status == "PENDING"
        assert item.phase == 1 and item.failure_history == [] and item.phase_attempts == {}


def test_group_runner_failure_requeues_active_item_and_releases_worker(monkeypatch) -> None:
    monkeypatch.setattr("aurora.services.challenge_group_runner.stop_project_containers", lambda _project_id: [])
    with Session(service_session_engine()) as session:
        project = Project(name="runner-failure", goal="retry after infrastructure failure")
        second_project = Project(name="runner-failure-2", goal="retry the other concurrent item")
        group = ChallengeGroup(name="batch", status="RUNNING")
        session.add_all([project, second_project, group])
        session.commit()
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1, status="RUNNING", fused_status="RUNNING")
        second_item = ChallengeGroupItem(group_id=group.id, project_id=second_project.id, position=2, status="RUNNING", fused_status="RUNNING")
        intent = Intent(project_id=project.id, objective="solve", status="RUNNING", lease_owner="worker_test")
        second_intent = Intent(project_id=second_project.id, objective="solve", status="RUNNING", lease_owner="worker_test_2")
        worker = Worker(id="worker_test", project_id=project.id, intent_id=intent.id, status="RUNNING")
        second_worker = Worker(id="worker_test_2", project_id=second_project.id, intent_id=second_intent.id, status="RUNNING")
        attempt = Attempt(project_id=project.id, intent_id=intent.id, worker_id=worker.id, status="RUNNING")
        second_attempt = Attempt(project_id=second_project.id, intent_id=second_intent.id, worker_id=second_worker.id, status="RUNNING")
        group.current_item_id = item.id
        session.add_all([item, second_item, intent, second_intent, worker, second_worker, attempt, second_attempt, group])
        session.commit()

        fail_group_run(session, group_id=group.id, error="database write failed")

        session.refresh(item)
        session.refresh(intent)
        session.refresh(worker)
        session.refresh(attempt)
        session.refresh(second_item)
        session.refresh(second_intent)
        session.refresh(second_worker)
        session.refresh(second_attempt)
        session.refresh(group)
        assert group.status == "FAILED"
        assert group.current_item_id is None
        assert item.status == "PENDING"
        assert item.fused_status == "PENDING"
        assert item.stop_reason == "recovered_after_runner_failure"
        assert intent.status == "PENDING"
        assert intent.lease_owner is None
        assert worker.status == "INTERRUPTED"
        assert attempt.status == "INTERRUPTED"
        assert second_item.status == second_item.fused_status == "PENDING"
        assert second_item.stop_reason == "recovered_after_runner_failure"
        assert second_intent.status == "PENDING" and second_intent.lease_owner is None
        assert second_worker.status == "INTERRUPTED"
        assert second_attempt.status == "INTERRUPTED"


def test_paused_batch_can_continue_with_ephemeral_authenticated_fetcher(tmp_path: Path) -> None:
    page = "<html><title>Private CTF</title><a href='/tasks/one'>One</a></html>"

    class FakeAuthenticatedFetcher:
        closed = False

        def fetch_text(self, _: str) -> tuple[str, str]:
            return page, "https://catalog.example/tasks"

        def fetch_bytes(self, _: str, __: int) -> tuple[bytes, str | None]:
            return b"", None

        def close(self) -> None:
            self.closed = True

    fetcher = FakeAuthenticatedFetcher()
    service = HandsFreeService(
        settings=Settings(artifact_dir=tmp_path, cataloger_llm_api_key="test-key"),
        fetch_text=lambda _: ("<form action='/login'><input type='password'></form>", "https://catalog.example/login"),
        cataloger=lambda _: {"summary": "private", "candidates": [{"title": "One", "challenge_url": "https://catalog.example/tasks/one", "attachment_urls": [], "confidence": 1}]},
        authenticated_fetcher_factory=lambda *_: fetcher,
        ctfplus_attachment_collector=lambda _candidates, _cookie: None,
    )
    with Session(service_session_engine()) as session:
        paused = service.scan(session, "https://catalog.example/tasks")
        result = service.continue_scan(session, paused.batch.id, cookie="session=secret")
        assert result.batch.status == "READY"
        assert result.batch.auth_method == "cookie"
        assert result.batch.auth_message is None
        assert len(result.candidates) == 1
        assert fetcher.closed


def test_private_literal_urls_are_rejected() -> None:
    with pytest.raises(ValueError, match="local management"):
        HandsFreeService._safe_url("http://192.168.1.10/tasks")


def test_hands_free_discovers_download_links_from_challenge_detail_pages(tmp_path: Path) -> None:
    downloads: list[str] = []

    def fetch_text(url: str) -> tuple[str, str]:
        if url == "https://catalog.example/tasks":
            return "<a href='/tasks/42'>Challenge 42</a>", url
        assert url == "https://catalog.example/tasks/42"
        return "<a href='/attachment/download/file/42.html'>下载</a>", url

    def fetch_bytes(url: str, _: int) -> tuple[bytes, str | None]:
        downloads.append(url)
        return b"challenge-file", "application/octet-stream"

    service = HandsFreeService(
        settings=Settings(artifact_dir=tmp_path, cataloger_llm_api_key="test-key"),
        fetch_text=fetch_text,
        fetch_bytes=fetch_bytes,
        cataloger=lambda _: {"summary": "", "candidates": [{"title": "Challenge 42", "challenge_url": "https://catalog.example/tasks/42", "attachment_urls": [], "confidence": 1}]},
        ctfplus_attachment_collector=lambda _candidates, _cookie: None,
    )
    with Session(service_session_engine()) as session:
        result = service.scan(session, "https://catalog.example/tasks")
        assert downloads == ["https://catalog.example/attachment/download/file/42.html"]
        assert result.candidates[0].staged_attachments_json[0]["filename"] == "42.html"


def test_html_login_interstitial_is_not_staged_as_an_attachment(tmp_path: Path) -> None:
    def fetch_text(url: str) -> tuple[str, str]:
        if url == "https://catalog.example/tasks":
            return "<a href='/tasks/42'>Challenge 42</a>", url
        return "<a href='/attachment/download/file/42.html'>下载</a>", url

    def fetch_bytes(_: str, __: int) -> tuple[bytes, str | None]:
        return "<html><h3>请登录!!</h3><a href='/login'>login</a></html>".encode(), "text/html"

    service = HandsFreeService(
        settings=Settings(artifact_dir=tmp_path, cataloger_llm_api_key="test-key"),
        fetch_text=fetch_text,
        fetch_bytes=fetch_bytes,
        cataloger=lambda _: {"summary": "", "candidates": [{"title": "Challenge 42", "challenge_url": "https://catalog.example/tasks/42", "attachment_urls": [], "confidence": 1}]},
        ctfplus_attachment_collector=lambda _candidates, _cookie: None,
    )
    with Session(service_session_engine()) as session:
        result = service.scan(session, "https://catalog.example/tasks")
        assert result.batch.status == "NEEDS_SESSION"
        assert result.candidates == []
        assert not (tmp_path / "imports").exists()


def test_download_endpoint_html_suffix_uses_detected_archive_suffix(tmp_path: Path) -> None:
    def fetch_text(url: str) -> tuple[str, str]:
        if url == "https://catalog.example/tasks":
            return "<a href='/tasks/42'>Challenge 42</a>", url
        return "<a href='/attachment/download/file/42.html'>下载</a>", url

    service = HandsFreeService(
        settings=Settings(artifact_dir=tmp_path, cataloger_llm_api_key="test-key"),
        fetch_text=fetch_text,
        fetch_bytes=lambda _url, _limit: (b"PK\x03\x04zip-content", "application/octet-stream"),
        cataloger=lambda _: {"summary": "", "candidates": [{"title": "Challenge 42", "challenge_url": "https://catalog.example/tasks/42", "attachment_urls": [], "confidence": 1}]},
        ctfplus_attachment_collector=lambda _candidates, _cookie: None,
    )
    with Session(service_session_engine()) as session:
        result = service.scan(session, "https://catalog.example/tasks")
        assert result.batch.status == "READY"
        assert result.candidates[0].staged_attachments_json[0]["filename"] == "42.zip"


def test_non_login_html_response_is_not_staged_as_attachment(tmp_path: Path) -> None:
    def fetch_text(url: str) -> tuple[str, str]:
        if url == "https://catalog.example/tasks":
            return "<a href='/tasks/42'>Challenge 42</a>", url
        return "<a href='/attachment/download/file/42.html'>下载</a>", url

    service = HandsFreeService(
        settings=Settings(artifact_dir=tmp_path, cataloger_llm_api_key="test-key"),
        fetch_text=fetch_text,
        fetch_bytes=lambda _url, _limit: (b"<!doctype html><title>Download error</title>", "text/html"),
        cataloger=lambda _: {"summary": "", "candidates": [{"title": "Challenge 42", "challenge_url": "https://catalog.example/tasks/42", "attachment_urls": [], "confidence": 1}]},
        ctfplus_attachment_collector=lambda _candidates, _cookie: None,
    )
    with Session(service_session_engine()) as session:
        result = service.scan(session, "https://catalog.example/tasks")
        assert result.batch.status == "READY"
        candidate = result.candidates[0]
        assert candidate.staged_attachments_json == []
        assert candidate.external_attachments_json[0]["status"] == "download_failed"


def test_stalled_attachment_does_not_block_the_import_batch(tmp_path: Path) -> None:
    def fetch_text(url: str) -> tuple[str, str]:
        if url == "https://catalog.example/tasks":
            return "<a href='/tasks/42'>Challenge 42</a>", url
        return "<a href='/files/challenge.zip'>download</a>", url

    def stalled_fetch_bytes(_: str, __: int) -> tuple[bytes, str | None]:
        time.sleep(5)
        return b"late attachment", "application/zip"

    service = HandsFreeService(
        settings=Settings(
            artifact_dir=tmp_path,
            cataloger_llm_api_key="test-key",
            cataloger_attachment_timeout_seconds=1,
        ),
        fetch_text=fetch_text,
        fetch_bytes=stalled_fetch_bytes,
        cataloger=lambda _: {"summary": "", "candidates": [{"title": "Challenge 42", "challenge_url": "https://catalog.example/tasks/42", "attachment_urls": [], "confidence": 1}]},
        ctfplus_attachment_collector=lambda _candidates, _cookie: None,
    )
    started = time.monotonic()
    with Session(service_session_engine()) as session:
        result = service.scan(session, "https://catalog.example/tasks")
        batch_status = result.batch.status
        staged = result.candidates[0].staged_attachments_json
        issue = result.candidates[0].external_attachments_json[0]

    assert time.monotonic() - started < 2
    assert batch_status == "READY"
    assert staged == []
    assert issue["status"] == "download_failed"
    assert issue["reason"] == "attachment download timed out after 1 seconds"


def test_browser_download_remote_fallback_uses_a_bounded_request(tmp_path: Path) -> None:
    class FakeDownload:
        url = "https://catalog.example/files/challenge.zip"

        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

        def path(self) -> Path:
            raise AssertionError("download.path() must not be used")

    class FakeResponse:
        url = "https://cdn.example/challenge.zip"
        ok = True
        status = 200
        headers = {"content-type": "application/zip", "content-length": "7"}

        @staticmethod
        def body() -> bytes:
            return b"zipdata"

    class FakeRequest:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int, int]] = []

        def get(self, url: str, *, timeout: int, max_redirects: int, headers: dict[str, str]) -> FakeResponse:
            assert headers == {"Accept": "application/octet-stream,*/*"}
            self.calls.append((url, timeout, max_redirects))
            return FakeResponse()

    request = FakeRequest()
    context = type("FakeContext", (), {"request": request})()
    download = FakeDownload()
    service = HandsFreeService(settings=Settings(artifact_dir=tmp_path, cataloger_attachment_timeout_seconds=3))

    data, mime_type, source_url = service._read_browser_download(context, download)

    assert download.cancelled
    assert len(request.calls) == 1
    assert request.calls[0][0] == download.url
    assert 2_900 <= request.calls[0][1] <= 3_000
    assert request.calls[0][2] == 5
    assert (data, mime_type, source_url) == (b"zipdata", "application/zip", FakeResponse.url)


def test_browser_download_preserves_original_one_shot_artifact(tmp_path: Path) -> None:
    artifact_path = tmp_path / "playwright-download"
    artifact_path.write_bytes(b"one-shot-data")

    class FakeDownload:
        url = "blob:https://catalog.example/one-shot"
        suggested_filename = "challenge.zip"
        _impl_obj = type("Impl", (), {"_artifact": type("Artifact", (), {"absolute_path": str(artifact_path)})()})()

        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

        def path(self) -> Path:
            raise AssertionError("download.path() must not be used")

    class RejectRequest:
        @staticmethod
        def get(*_args, **_kwargs):
            raise AssertionError("a completed original download must not be replayed")

    context = type("FakeContext", (), {"request": RejectRequest()})()
    download = FakeDownload()
    service = HandsFreeService(settings=Settings(artifact_dir=tmp_path, cataloger_attachment_timeout_seconds=1))

    data, mime_type, source_url = service._read_browser_download(context, download)

    assert not download.cancelled
    assert (data, mime_type, source_url) == (b"one-shot-data", "application/zip", download.url)


def test_direct_attachment_download_retries_transient_failures_with_referer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeHeaders(dict[str, str]):
        @staticmethod
        def get_content_type() -> str:
            return "application/zip"

    class FakeResponse:
        headers = FakeHeaders({"Content-Length": "7"})

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        @staticmethod
        def geturl() -> str:
            return "https://catalog.example/files/challenge.zip"

        @staticmethod
        def read(_: int) -> bytes:
            return b"zipdata"

    class FakeOpener:
        def __init__(self) -> None:
            self.requests = []

        def open(self, request, *, timeout: int):
            assert timeout == 2
            self.requests.append(request)
            if len(self.requests) == 1:
                raise OSError("connection reset")
            return FakeResponse()

    opener = FakeOpener()
    monkeypatch.setattr("aurora.services.hands_free.build_opener", lambda *_args: opener)
    service = HandsFreeService(settings=Settings(artifact_dir=tmp_path, cataloger_attachment_timeout_seconds=2))

    data, mime_type = service._fetch_bytes(
        "https://catalog.example/files/challenge.zip",
        MAX_ATTACHMENT_BYTES,
        referer="https://catalog.example/tasks/42",
    )

    assert len(opener.requests) == 2
    assert opener.requests[0].get_header("Referer") == "https://catalog.example/tasks/42"
    assert opener.requests[0].get_header("Accept") == "application/octet-stream,*/*"
    assert (data, mime_type) == (b"zipdata", "application/zip")


def service_session_engine():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    return engine
