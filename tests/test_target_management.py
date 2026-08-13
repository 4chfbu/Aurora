from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from aurora.models import AuthorizationScope, DiscoveredTarget, Project
from aurora.services.artifact_store import ArtifactStore
from aurora.services.browser_sessions import BrowserSessionRegistry
from aurora.services.policy import PolicyEngine
from aurora.services.target_management import TargetManagementService
from aurora.services.target_probe import TargetProbeResult
from aurora.services.tool_profiles import profile_for_challenge


class ProbeSequence:
    def __init__(self, *results: TargetProbeResult) -> None:
        self.results = list(results)

    def probe(self, url: str) -> TargetProbeResult:
        result = self.results.pop(0)
        return TargetProbeResult(result.success, result.code, result.summary, {**result.diagnostics, "url": url})


def reachable() -> TargetProbeResult:
    return TargetProbeResult(True, "REACHABLE", "宿主机和 Worker 均可访问靶机", {"host_http": "reachable", "worker_http_exit_code": 0})


def unavailable() -> TargetProbeResult:
    return TargetProbeResult(False, "PORT_NOT_READY", "靶机地址已生成，但端口尚未就绪", {"host_http": "failed", "worker_http_exit_code": 7})


def setup_project(session: Session) -> Project:
    project = Project(name="target", goal="verify target")
    session.add(project)
    session.commit()
    session.refresh(project)
    session.add(AuthorizationScope(project_id=project.id))
    session.commit()
    return project


def test_manual_target_activation_replacement_and_failed_probe_preserve_active(tmp_path: Path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = setup_project(session)
        service = TargetManagementService(probe_service=ProbeSequence(reachable(), unavailable(), reachable()), artifact_store=ArtifactStore(tmp_path))

        first = service.submit_manual(session, project_id=project.id, url="http://target-one.example:8080", probe=True)
        failed = service.submit_manual(session, project_id=project.id, url="http://target-two.example:8081", probe=True)
        failed_target = session.get(DiscoveredTarget, failed.target_id)
        still_allowed = PolicyEngine().check_tool_request(session, project_id=project.id, tool_name="http.request", request={"url": first.target_url})
        failed_denied = PolicyEngine().check_tool_request(session, project_id=project.id, tool_name="http.request", request={"url": "http://target-two.example:8081/"})
        replacement = service.submit_manual(session, project_id=project.id, url="https://target-three.example", probe=True)
        targets = session.exec(select(DiscoveredTarget).where(DiscoveredTarget.project_id == project.id)).all()
        session.refresh(project)

    assert first.status == "VERIFIED"
    assert failed.status == "PROVISIONING" and failed.target_url == first.target_url
    assert failed_target is not None and failed_target.status == "PROVISIONING"
    assert still_allowed.allowed is True and failed_denied.allowed is False
    assert replacement.status == "VERIFIED" and project.target_url == "https://target-three.example/"
    assert len([target for target in targets if target.status == "ACTIVE"]) == 1
    assert any(target.url == first.target_url and target.status == "INVALIDATED" for target in targets)
    assert len(list(tmp_path.rglob("*.txt"))) == 3


def test_automatic_unique_activation_and_multiple_confirmation(tmp_path: Path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        first_project = setup_project(session)
        unique = TargetManagementService(probe_service=ProbeSequence(reachable()), artifact_store=ArtifactStore(tmp_path)).evaluate_automatic(
            session,
            project_id=first_project.id,
            candidates=[{"url": "http://unique.example:8000", "score": 95}],
        )
        second_project = setup_project(session)
        multiple = TargetManagementService(probe_service=ProbeSequence(reachable(), reachable()), artifact_store=ArtifactStore(tmp_path)).evaluate_automatic(
            session,
            project_id=second_project.id,
            candidates=[{"url": "http://one.example:8000", "score": 98}, {"url": "http://two.example:8000", "score": 90}],
        )
        candidates = session.exec(select(DiscoveredTarget).where(DiscoveredTarget.project_id == second_project.id)).all()

    assert unique.status == "VERIFIED"
    assert multiple.status == "NEEDS_CONFIRMATION"
    assert {target.status for target in candidates} == {"CANDIDATE"}


def test_batch_cookie_is_bound_to_each_challenge_detail_page() -> None:
    registry = BrowserSessionRegistry()
    registry.register_batch(batch_id="batch", source_url="https://ctf.example/tasks", cookie="session=secret")
    registry.bind_batch_project_sources(batch_id="batch", project_sources={"p1": "https://ctf.example/detail/1", "p2": "https://ctf.example/detail/2"})

    assert registry.get_project_session("p1").source_url.endswith("/detail/1")
    assert registry.get_project_session("p2").source_url.endswith("/detail/2")
    assert profile_for_challenge("web") == "core"
