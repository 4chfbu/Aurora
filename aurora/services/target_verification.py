from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, select

from aurora.models import DiscoveredTarget, Project, WorkerEvent, now_utc
from aurora.services.browser_interaction import BrowserInteractionService
from aurora.services.browser_sessions import browser_session_registry


@dataclass(frozen=True)
class TargetVerificationResult:
    status: str
    reason: str
    target_url: str | None = None


class TargetVerificationService:
    """Verify a challenge's launch page using the ephemeral project browser session."""

    def verify(self, session: Session, *, project_id: str, source_url: str | None = None) -> TargetVerificationResult:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError("project not found")
        browser_session = browser_session_registry.get_project_session(project_id)
        if browser_session is None:
            return self._record(session, project, "NEEDS_SESSION", "需要已登录的 Cookie 才能验证靶机启动", None)

        result = BrowserInteractionService().execute(
            session,
            project_id=project_id,
            request={"url": source_url or browser_session.source_url, "wait_seconds": 5},
            worker_id=None,
            intent_id=None,
            attempt_id=None,
        )
        if not result.success:
            return self._record(session, project, "UNVERIFIED", result.summary, None)
        target = session.exec(select(DiscoveredTarget).where(DiscoveredTarget.project_id == project_id).order_by(DiscoveredTarget.created_at.desc())).first()
        target_url = target.url if target else (result.target_urls[0] if result.target_urls else None)
        if not target_url:
            return self._record(session, project, "UNVERIFIED", "未发现可访问的靶机地址", None)
        return self._record(session, project, "VERIFIED", "已发现并验证靶机启动地址", target_url)

    @staticmethod
    def _record(session: Session, project: Project, status: str, reason: str, target_url: str | None) -> TargetVerificationResult:
        project.target_verification_status = status
        project.target_verification_reason = reason[:1000]
        project.target_verified_at = now_utc()
        if target_url:
            project.target_url = target_url
        session.add(project)
        session.add(WorkerEvent(
            project_id=project.id,
            event_type="target.verification.completed",
            payload_json={"status": status, "reason": reason[:1000], "target_url": target_url},
        ))
        session.commit()
        return TargetVerificationResult(status, reason, target_url)
