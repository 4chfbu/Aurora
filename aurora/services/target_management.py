from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlmodel import Session, select

from aurora.models import DiscoveredTarget, Project, WorkerEvent, now_utc
from aurora.services.artifact_store import ArtifactStore
from aurora.services.blackboard_repository import BlackboardRepository
from aurora.services.target_probe import TargetProbeResult, TargetProbeService, normalize_target_url


@dataclass(frozen=True)
class TargetStateResult:
    status: str
    reason: str
    target_url: str | None = None
    target_id: str | None = None
    probe: dict[str, object] | None = None


class TargetManagementService:
    """Shared validation, probing, history, activation, and audit flow."""

    def __init__(self, probe_service: TargetProbeService | None = None, artifact_store: ArtifactStore | None = None) -> None:
        self.probe_service = probe_service or TargetProbeService()
        self.artifact_store = artifact_store or ArtifactStore()

    def submit_manual(self, session: Session, *, project_id: str, url: str, probe: bool = True) -> TargetStateResult:
        if not probe:
            raise ValueError("manual targets must be probed before activation")
        return self._evaluate_one(session, project_id=project_id, url=url, source="manual", confidence=1.0, activate=True)

    def evaluate_automatic(
        self,
        session: Session,
        *,
        project_id: str,
        candidates: list[dict[str, object]],
        requires_confirmation: bool = False,
    ) -> TargetStateResult:
        project = self._project(session, project_id)
        normalized: list[dict[str, object]] = []
        seen: set[str] = set()
        for candidate in sorted(candidates, key=lambda item: float(item.get("score", 0)), reverse=True):
            try:
                url = normalize_target_url(str(candidate.get("url") or ""))
            except ValueError:
                continue
            if url in seen:
                continue
            seen.add(url)
            normalized.append({**candidate, "url": url})

        if not normalized:
            return self._set_project_state(session, project, "UNVERIFIED", "未发现可信的靶机地址", None)

        reachable: list[DiscoveredTarget] = []
        failed: list[DiscoveredTarget] = []
        for candidate in normalized[:10]:
            result = self.probe_service.probe(str(candidate["url"]))
            target = self._save_candidate(
                session,
                project_id=project_id,
                url=str(candidate["url"]),
                source="automatic",
                confidence=min(1.0, max(0.0, float(candidate.get("score", 0)) / 100.0)),
                probe=result,
                status="CANDIDATE" if result.success else "PROVISIONING",
                source_artifact_id=str(candidate.get("artifact_ref") or "") or None,
            )
            (reachable if result.success else failed).append(target)

        high_confidence = [target for target in reachable if target.confidence >= 0.8]
        if len(reachable) == 1 and len(high_confidence) == 1 and not requires_confirmation:
            return self.activate(session, project_id=project_id, target_id=reachable[0].id, reprobe=False)
        if reachable:
            reason = "启动需要付费或人工确认" if requires_confirmation else f"发现 {len(reachable)} 个可达候选，请确认正确靶机"
            return self._set_project_state(session, project, "NEEDS_CONFIRMATION", reason, None)
        reason = failed[0].probe_json.get("summary") if failed else "靶机正在创建，尚不可达"
        return self._set_project_state(session, project, "PROVISIONING", str(reason), None)

    def activate(self, session: Session, *, project_id: str, target_id: str, reprobe: bool = True) -> TargetStateResult:
        project = self._project(session, project_id)
        target = session.get(DiscoveredTarget, target_id)
        if target is None or target.project_id != project_id:
            raise ValueError("target candidate not found")
        result = self.probe_service.probe(target.url) if reprobe else self._probe_from_target(target)
        if not result.success:
            target.status = "PROVISIONING"
            target.probe_json = result.public_dict()
            target.updated_at = now_utc()
            session.add(target)
            session.add(WorkerEvent(project_id=project_id, event_type="target.probe_failed", payload_json={"target_id": target.id, "target_url": target.url, **result.public_dict()}))
            session.commit()
            return self._set_project_state(session, project, "PROVISIONING", f"{result.code}: {result.summary}", None, target.id, result)

        active = session.exec(select(DiscoveredTarget).where(DiscoveredTarget.project_id == project_id, DiscoveredTarget.status == "ACTIVE")).all()
        for previous in active:
            if previous.id == target.id:
                continue
            previous.status = "INVALIDATED"
            previous.invalidated_at = now_utc()
            previous.updated_at = now_utc()
            session.add(previous)
            session.add(WorkerEvent(project_id=project_id, event_type="target.invalidated", payload_json={"target_id": previous.id, "url": previous.url, "replacement_target_id": target.id}))
        target.status = "ACTIVE"
        target.probe_json = result.public_dict()
        target.updated_at = now_utc()
        session.add(target)
        session.add(WorkerEvent(project_id=project_id, event_type="target.activated", payload_json={"target_id": target.id, "target_url": target.url, "source": target.source, **result.public_dict()}))
        session.commit()

        BlackboardRepository().upsert_fact(
            session,
            project_id=project_id,
            statement=f"Verified target: {target.url}",
            category="target",
            confidence=max(target.confidence, 0.9),
            evidence_refs=[target.source_artifact_id] if target.source_artifact_id else [],
        )
        return self._set_project_state(session, project, "VERIFIED", result.summary, target.url, target.id, result)

    def _evaluate_one(self, session: Session, *, project_id: str, url: str, source: str, confidence: float, activate: bool) -> TargetStateResult:
        project = self._project(session, project_id)
        normalized = normalize_target_url(url)
        result = self.probe_service.probe(normalized)
        audit = self.artifact_store.write_text(
            session,
            project_id=project_id,
            artifact_type="target-audit",
            origin_kind="manual" if source == "manual" else "target_observation",
            summary=f"{source} target probe: {result.code}",
            content=json.dumps({"source": source, "url": normalized, "probe": result.public_dict()}, ensure_ascii=False, indent=2),
        )
        target = self._save_candidate(
            session,
            project_id=project_id,
            url=normalized,
            source=source,
            confidence=confidence,
            probe=result,
            status="CANDIDATE" if result.success else "PROVISIONING",
            source_artifact_id=audit.id,
        )
        session.add(WorkerEvent(project_id=project_id, event_type="target.manual_submitted" if source == "manual" else "target.discovered", payload_json={"target_id": target.id, "target_url": normalized, "artifact_ref": audit.id, **result.public_dict()}))
        session.commit()
        if result.success and activate:
            return self.activate(session, project_id=project_id, target_id=target.id, reprobe=False)
        active = session.exec(select(DiscoveredTarget).where(DiscoveredTarget.project_id == project_id, DiscoveredTarget.status == "ACTIVE")).first()
        if active is not None:
            return TargetStateResult("PROVISIONING", f"{result.code}: {result.summary}", active.url, target.id, result.public_dict())
        return self._set_project_state(session, project, "PROVISIONING", f"{result.code}: {result.summary}", None, target.id, result)

    @staticmethod
    def _project(session: Session, project_id: str) -> Project:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError("project not found")
        return project

    @staticmethod
    def _save_candidate(
        session: Session,
        *,
        project_id: str,
        url: str,
        source: str,
        confidence: float,
        probe: TargetProbeResult,
        status: str,
        source_artifact_id: str | None,
    ) -> DiscoveredTarget:
        target = DiscoveredTarget(
            project_id=project_id,
            url=url,
            host=(urlparse(url).hostname or "").lower().rstrip("."),
            source=source,
            confidence=confidence,
            probe_json=probe.public_dict(),
            source_artifact_id=source_artifact_id,
            status=status,
        )
        session.add(target)
        session.commit()
        session.refresh(target)
        return target

    @staticmethod
    def _probe_from_target(target: DiscoveredTarget) -> TargetProbeResult:
        data = target.probe_json
        return TargetProbeResult(bool(data.get("success")), str(data.get("code") or "UNKNOWN"), str(data.get("summary") or ""), dict(data.get("diagnostics") or {}))

    @staticmethod
    def _set_project_state(
        session: Session,
        project: Project,
        status: str,
        reason: str,
        target_url: str | None,
        target_id: str | None = None,
        probe: TargetProbeResult | None = None,
    ) -> TargetStateResult:
        project.target_verification_status = status
        project.target_verification_reason = reason[:1000]
        project.target_verified_at = now_utc()
        project.target_url = target_url
        project.updated_at = now_utc()
        session.add(project)
        session.add(WorkerEvent(project_id=project.id, event_type="target.verification.completed", payload_json={"status": status, "reason": reason[:1000], "target_url": target_url, "target_id": target_id}))
        session.commit()
        return TargetStateResult(status, reason, target_url, target_id, probe.public_dict() if probe else None)
