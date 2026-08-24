from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from sqlmodel import Session, select

from aurora.models import AttemptCheckpoint, Hint, ImportCandidate, Intent, Project, WorkerEvent
from aurora.services.blackboard_repository import BlackboardRepository
from aurora.services.solver_playbooks import select_playbook


URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+")


@dataclass
class ManagerDecision:
    status: str
    reason: str
    proposed_intents: list[dict[str, Any]] = field(default_factory=list)


class ManagerService:
    def run_project(self, session: Session, *, project_id: str) -> ManagerDecision:
        repository = BlackboardRepository()
        project = session.get(Project, project_id)
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
                    budget={"model_role": "triage", "phase": 1, "max_tool_calls": 3, "tool_request": {"url": url, "timeout_seconds": 5}},
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
                objective, capabilities, tool_request = self._intent_for_local_challenge(session, project=project, hint=hint.content)
                result = repository.upsert_intent(
                    session,
                    project_id=project_id,
                    objective=objective,
                    capability_tags=capabilities,
                    priority=1.8,
                    risk_level="low",
                    budget={"model_role": "triage", "phase": 1, "max_tool_calls": 3, **({"tool_request": tool_request} if tool_request else {})},
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
            if checkpoint is not None and (
                checkpoint.generated_intent_ids or checkpoint.status not in {"PARTIAL", "FAILED", "TIMEOUT"}
            ):
                status = "NOOP"
                reason = "The latest checkpoint already authored follow-up work; do not create a second planning loop."
                session.add(
                    WorkerEvent(
                        project_id=project_id,
                        event_type="manager.decision",
                        payload_json={"status": status, "reason": reason, "proposed_intents": []},
                    )
                )
                session.commit()
                return ManagerDecision(status=status, reason=reason, proposed_intents=[])
            if checkpoint is not None and not any(str(step).strip() for step in checkpoint.next_steps):
                status = "NOOP"
                reason = "The latest checkpoint has no evidence-backed next step."
                session.add(
                    WorkerEvent(
                        project_id=project_id,
                        event_type="manager.decision",
                        payload_json={"status": status, "reason": reason, "proposed_intents": [], "checkpoint_id": checkpoint.id},
                    )
                )
                session.commit()
                return ManagerDecision(status=status, reason=reason, proposed_intents=[])
            next_step = next(
                (str(step).strip() for step in (checkpoint.next_steps if checkpoint else []) if str(step).strip()),
                "Inspect current project evidence and produce the next evidence-backed result.",
            )
            failed_route = next(
                (str(route).strip() for route in (checkpoint.failed_routes if checkpoint else []) if str(route).strip()),
                "",
            )
            route_hint = f" Do not repeat failed route {hashlib.sha256(failed_route.encode()).hexdigest()[:12]}." if failed_route else ""
            result = repository.upsert_intent(
                session,
                project_id=project_id,
                objective=(
                    f"Continue checkpoint {checkpoint.id if checkpoint else 'bootstrap'} with exactly one evidence-backed experiment: "
                    f"{next_step[:500]}. Record the expected discriminating observation and checkpoint the result.{route_hint}"
                ),
                capability_tags=self._continuation_capabilities(checkpoint),
                parent_intent_id=checkpoint.intent_id if checkpoint else None,
                priority=0.8,
                risk_level="low",
                budget={
                    "model_role": "reviewer" if min(4, int((checkpoint.budget_json if checkpoint else {}).get("phase", 1) or 1) + 1) == 4 else "solver",
                    "phase": min(4, int((checkpoint.budget_json if checkpoint else {}).get("phase", 1) or 1) + 1),
                    "source_checkpoint_id": checkpoint.id if checkpoint else None,
                    "max_tool_calls": 3,
                    **({"failed_route_fingerprint": hashlib.sha256(failed_route.encode()).hexdigest()[:16]} if failed_route else {}),
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

    def _intent_for_local_challenge(self, session: Session, *, project: Project | None, hint: str) -> tuple[str, list[str], dict[str, Any] | None]:
        challenge_type = (project.challenge_type if project else "") or "generic"
        kind = challenge_type.lower()
        attachments: list[dict[str, Any]] = []
        candidate = session.exec(select(ImportCandidate).where(ImportCandidate.project_id == project.id).order_by(ImportCandidate.created_at.desc())).first() if project else None
        if candidate is not None:
            attachments = [*candidate.staged_attachments_json, *candidate.external_attachments_json]
        names = [str(item.get("filename") or item.get("name") or item.get("path") or "") for item in attachments if isinstance(item, dict)]
        attachment_note = f" Available attachments: {', '.join(name for name in names if name)[:500]}." if names else ""
        playbook = select_playbook(challenge_type, f"{project.name if project else ''} {project.goal if project else ''} {hint}")
        playbooks = {
            "reverse": ("Identify the attached binary, inspect protections and entry points, then use aurora_reverse to locate the validation path." + attachment_note, ["binary.inspect", "sandbox.exec"]),
            "crypto": ("Inventory the attached data and identify encoding, key material, and algebraic structure before writing a reproducible verifier." + attachment_note, ["python.analyze", "sandbox.exec"]),
            "forensics": ("Identify the attached evidence formats, preserve hashes, and extract the highest-signal metadata or embedded payloads." + attachment_note, ["forensic.inspect", "sandbox.exec"]),
            "pwn": ("Inspect the attached executable and identify architecture, mitigations, and the first controllable input before attempting exploitation." + attachment_note, ["binary.inspect", "sandbox.exec"]),
            "web": ("Use the supplied web context to map the application surface and identify the next authorized request; a target is required for network actions." + attachment_note, ["blackboard.query", "http.request"]),
        }
        objective, capabilities = next((value for key, value in playbooks.items() if key in kind), (f"Use the {playbook.challenge_type} playbook: {'; '.join(playbook.first_steps)}.{attachment_note}", list(playbook.capabilities)))
        objective = f"{objective} Operator hint: {hint[:500]}"
        return objective, capabilities, None

    @staticmethod
    def _continuation_capabilities(checkpoint: AttemptCheckpoint | None) -> list[str]:
        text = " ".join((checkpoint.next_steps if checkpoint else []) or []).lower()
        if any(token in text for token in ("artifact", "transcript", "证据文件", "工件")):
            return ["blackboard.query", "sandbox.exec"]
        if any(token in text for token in ("binary", "逆向", "elf", "disassemble")):
            return ["binary.inspect", "sandbox.exec"]
        if any(token in text for token in ("http", "web", "request", "靶机")):
            return ["http.request", "blackboard.query"]
        if any(token in text for token in ("python", "decode", "crypto", "解码")):
            return ["python.analyze", "sandbox.exec"]
        return ["blackboard.query", "sandbox.exec"]

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
