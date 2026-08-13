from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, select

from aurora.models import ImportCandidate, Project, WorkerEvent, now_utc
from aurora.services.browser_interaction import BrowserInteractionService
from aurora.services.browser_sessions import browser_session_registry
from aurora.services.target_management import TargetManagementService
from aurora.services.target_probe import TargetProbeService


@dataclass(frozen=True)
class TargetVerificationResult:
    status: str
    reason: str
    target_url: str | None = None


class TargetVerificationService:
    """Verify a challenge's launch page using the ephemeral project browser session."""

    def __init__(self, probe_service: TargetProbeService | None = None) -> None:
        self.targets = TargetManagementService(probe_service=probe_service)

    def verify(self, session: Session, *, project_id: str, source_url: str | None = None, source_metadata: dict | None = None, allow_paid_launch: bool = False) -> TargetVerificationResult:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError("project not found")
        browser_session = browser_session_registry.get_project_session(project_id)
        if browser_session is None:
            return self._record(session, project, "NEEDS_SESSION", "需要已登录的 Cookie 才能验证靶机启动", None)

        if source_url is None:
            candidate = session.exec(select(ImportCandidate).where(ImportCandidate.project_id == project_id).order_by(ImportCandidate.created_at.desc())).first()
            source_url = candidate.challenge_url if candidate is not None else browser_session.source_url

        request = {"url": source_url or browser_session.source_url, "wait_seconds": 5, "allow_paid_launch": allow_paid_launch}
        locator = source_metadata.get("locator") if isinstance(source_metadata, dict) else None
        if isinstance(locator, dict):
            request["locator"] = locator
        result = BrowserInteractionService().execute(
            session,
            project_id=project_id,
            request=request,
            worker_id=None,
            intent_id=None,
            attempt_id=None,
        )
        if not result.success:
            return self._record(session, project, "UNVERIFIED", result.summary, None)
        # Only probe targets returned by this interaction.  Looking up the
        # newest historical row could revive an analytics/write-up URL from a
        # previous bad extraction.
        candidates = result.target_candidates or [{"url": url, "source": "browser", "score": 85, "artifact_ref": result.artifact_refs[0] if result.artifact_refs else None} for url in result.target_urls]
        if not candidates:
            if result.requires_confirmation:
                return self._record(session, project, "NEEDS_CONFIRMATION", result.summary, None)
            return self._record(session, project, "UNVERIFIED", "未发现可访问的靶机地址", None)
        managed = self.targets.evaluate_automatic(session, project_id=project_id, candidates=candidates, requires_confirmation=result.requires_confirmation)
        return TargetVerificationResult(managed.status, managed.reason, managed.target_url)

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
