from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from sqlmodel import Session, select

from aurora.models import AttemptCheckpoint, Hint, Intent, WorkerEvent
from aurora.services.blackboard_repository import BlackboardRepository


URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+")


@dataclass
class ManagerDecision:
    status: str
    reason: str
    proposed_intents: list[dict[str, Any]] = field(default_factory=list)


class ManagerService:
    def run_project(self, session: Session, *, project_id: str) -> ManagerDecision:
        repository = BlackboardRepository()
        hints = session.exec(
            select(Hint).where(Hint.project_id == project_id, Hint.consumed == False).order_by(Hint.created_at)  # noqa: E712
        ).all()
        proposed: list[dict[str, Any]] = []

        for hint in hints:
            url = self._extract_url(hint.content)
            if url:
                result = repository.upsert_intent(
                    session,
                    project_id=project_id,
                    objective=f"Investigate user-provided HTTP target from hint: {url}",
                    capability_tags=["http.request"],
                    priority=2.5,
                    risk_level="low",
                    budget={"model_role": "planner", "max_tool_calls": 3, "tool_request": {"url": url, "timeout_seconds": 5}},
                )
                hint.consumed = True
                session.add(hint)
                proposed.append(
                    {
                        "intent_id": result.item.id,
                        "created": result.created,
                        "objective": result.item.objective,
                        "source_hint_id": hint.id,
                    }
                )
            else:
                result = repository.upsert_intent(
                    session,
                    project_id=project_id,
                    objective=f"Review user hint and identify the next concrete action: {hint.content[:160]}",
                    capability_tags=["sandbox.exec"],
                    priority=1.2,
                    risk_level="low",
                    budget={
                        "model_role": "planner",
                        "max_tool_calls": 3,
                        "tool_request": {
                            "command": "printf 'Manager queued a non-URL hint for review.\\n'",
                            "cwd": ".",
                            "timeout_seconds": 5,
                        }
                    },
                )
                hint.consumed = True
                session.add(hint)
                proposed.append(
                    {
                        "intent_id": result.item.id,
                        "created": result.created,
                        "objective": result.item.objective,
                        "source_hint_id": hint.id,
                    }
                )

        if not proposed and not self._has_runnable_intents(session, project_id):
            checkpoint = session.exec(
                select(AttemptCheckpoint)
                .where(AttemptCheckpoint.project_id == project_id)
                .order_by(AttemptCheckpoint.created_at.desc())
            ).first()
            next_step = next(
                (str(step).strip() for step in (checkpoint.next_steps if checkpoint else []) if str(step).strip()),
                "Inspect current project evidence and produce the next evidence-backed result.",
            )
            result = repository.upsert_intent(
                session,
                project_id=project_id,
                objective=f"Continue from the latest checkpoint: {next_step[:500]}",
                capability_tags=["sandbox.exec", "blackboard.query"],
                priority=0.8,
                risk_level="low",
                budget={
                    "model_role": "planner",
                    "max_tool_calls": 3,
                },
            )
            proposed.append({"intent_id": result.item.id, "created": result.created, "objective": result.item.objective})

        status = "PROPOSED" if proposed else "NOOP"
        reason = "Generated intents from hints or idle project state." if proposed else "No unconsumed hints and runnable intents already exist."
        session.add(
            WorkerEvent(
                project_id=project_id,
                event_type="manager.decision",
                payload_json={"status": status, "reason": reason, "proposed_intents": proposed},
            )
        )
        session.commit()
        return ManagerDecision(status=status, reason=reason, proposed_intents=proposed)

    def _extract_url(self, content: str) -> str | None:
        match = URL_PATTERN.search(content)
        if not match:
            return None
        url = match.group(0).rstrip(".,;)")
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return url
        return None

    def _has_runnable_intents(self, session: Session, project_id: str) -> bool:
        existing = session.exec(
            select(Intent).where(Intent.project_id == project_id, Intent.status.in_(["PENDING", "CLAIMED", "RUNNING", "CONCLUDING"]))
        ).first()
        return existing is not None
