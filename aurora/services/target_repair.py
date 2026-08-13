from __future__ import annotations

from sqlmodel import Session, select

from aurora.models import DiscoveredTarget, Fact, Project, WorkerEvent, now_utc
from aurora.services.browser_interaction import BrowserInteractionService


def invalidate_false_targets(session: Session) -> dict[str, int]:
    """Retract targets accepted by the old broad response-URL heuristic."""
    active = session.exec(select(DiscoveredTarget).where(DiscoveredTarget.status == "ACTIVE")).all()
    invalid = [target for target in active if not BrowserInteractionService._target_urls([target.url], "")]
    if not invalid:
        return {"targets": 0, "facts": 0, "projects": 0}

    invalid_urls_by_project: dict[str, set[str]] = {}
    for target in invalid:
        target.status = "INVALIDATED"
        target.invalidated_at = now_utc()
        session.add(target)
        invalid_urls_by_project.setdefault(target.project_id, set()).add(target.url)
        session.add(
            WorkerEvent(
                project_id=target.project_id,
                event_type="target.invalidated",
                payload_json={"target_id": target.id, "url": target.url, "reason": "false target URL heuristic match"},
            )
        )

    retracted_facts = 0
    repaired_projects = 0
    for project_id, urls in invalid_urls_by_project.items():
        facts = session.exec(select(Fact).where(Fact.project_id == project_id, Fact.status == "ACTIVE")).all()
        for fact in facts:
            if fact.statement.startswith("Browser interaction exposed target: ") and fact.statement.removeprefix("Browser interaction exposed target: ") in urls:
                fact.status = "RETRACTED"
                session.add(fact)
                retracted_facts += 1

        project = session.get(Project, project_id)
        if project is not None and project.target_url in urls:
            project.target_url = None
            project.target_verification_status = "UNVERIFIED"
            project.target_verification_reason = "已移除误识别的统计、静态资源、WriteUp 或占位符 URL"
            project.target_verified_at = now_utc()
            session.add(project)
            repaired_projects += 1

    session.commit()
    return {"targets": len(invalid), "facts": retracted_facts, "projects": repaired_projects}
