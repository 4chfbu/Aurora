from __future__ import annotations

import math
from typing import Any

from sqlmodel import Session, select

from aurora.models import Attempt, Finding, Intent, LLMTrace, Project, WorkerEvent, now_utc
from aurora.services.blackboard_repository import BlackboardRepository
from aurora.services.flag_rejection import record_flag_rejection
from aurora.services.flag_validator import FlagValidator


class ResultProcessor:
    def apply(self, session: Session, *, attempt: Attempt, output: dict, llm_trace: LLMTrace) -> None:
        repository = BlackboardRepository()
        artifact_refs = list(output.get("artifact_refs", []))
        for tool_call in attempt.tool_calls:
            artifact_refs.extend(tool_call.get("artifact_refs", []))
        artifact_refs = sorted(set(artifact_refs))

        validator = FlagValidator()
        proposed_flags = output.get("candidate_flags", [])
        normalized_flags = self._flag_values(proposed_flags)
        decoy_flags = [value for value in normalized_flags if validator.is_decoy_flag_value(value)]

        for candidate in self._objects(output.get("fact_candidates")):
            statement = str(candidate.get("statement", "unspecified fact"))
            # Do not preserve a worker's "solved" conclusion when the value
            # itself explicitly says that it is a fake flag.
            if any(value.lower() in statement.lower() for value in decoy_flags):
                continue
            repository.upsert_fact(
                session,
                project_id=attempt.project_id,
                statement=statement,
                category=candidate.get("category", "general"),
                confidence=self._confidence(candidate.get("confidence")),
                evidence_refs=artifact_refs or candidate.get("evidence_refs", []),
                source_intent_id=attempt.intent_id,
                source_attempt_id=attempt.id,
            )

        for suggested in self._objects(output.get("suggested_intents")):
            repository.upsert_intent(
                session,
                project_id=attempt.project_id,
                objective=suggested.get("objective", "Review next step"),
                capability_tags=suggested.get("capability_tags", []),
                parent_intent_id=attempt.intent_id,
                priority=self._priority(suggested.get("priority")),
                risk_level=suggested.get("risk_level", "low"),
                budget=suggested.get("budget", {}),
            )

        verified_flags: list[tuple[str, list[str]]] = []
        decoy_feedback: list[str] = []
        for candidate in proposed_flags:
            value = candidate.get("value") if isinstance(candidate, dict) else str(candidate)
            if not value:
                continue
            artifact_ref = candidate.get("artifact_ref") if isinstance(candidate, dict) else None
            evidence_refs = [artifact_ref] if artifact_ref else artifact_refs
            if validator.is_decoy_flag_value(value):
                reason = "its brace payload explicitly identifies it as a fake/decoy flag"
                record_flag_rejection(
                    session,
                    project_id=attempt.project_id,
                    value=value,
                    reason=reason,
                    evidence_refs=evidence_refs,
                    worker_id=attempt.worker_id,
                    intent_id=attempt.intent_id,
                    attempt_id=attempt.id,
                    event_type="finding.flag_candidate_decoy",
                )
                feedback = f"Candidate flag {value} was rejected: {reason}. Do not submit it again; continue investigating for the correct flag."
                decoy_feedback.append(feedback)
                continue

            syntactically_valid = validator.is_valid_flag_value(value)
            directly_verified = bool(
                artifact_ref and validator.is_verified_candidate(session, value=value, artifact_ref=artifact_ref, project_id=attempt.project_id)
            )
            derived_verified = self._is_evidence_backed_derivation(
                session,
                validator=validator,
                value=value,
                evidence_refs=evidence_refs,
                fact_candidates=output.get("fact_candidates", []),
                project_id=attempt.project_id,
            )
            # A meaningful identifier followed by a non-placeholder brace
            # payload is itself sufficient to treat a worker proposal as a
            # candidate.  Artifact checks remain useful provenance, but are
            # not a gate: many valid CTF prefixes are competition-specific.
            if not syntactically_valid and not directly_verified and not derived_verified:
                reason = "not a meaningful prefix{payload} candidate or is a placeholder"
                record_flag_rejection(
                    session,
                    project_id=attempt.project_id,
                    value=value,
                    reason=reason,
                    evidence_refs=evidence_refs,
                    worker_id=attempt.worker_id,
                    intent_id=attempt.intent_id,
                    attempt_id=attempt.id,
                )
                decoy_feedback.append(
                    f"Candidate flag {value} was rejected: {reason}. Do not submit it again; continue investigating for the correct flag."
                )
                continue
            existing = session.exec(
                select(Finding).where(Finding.project_id == attempt.project_id, Finding.title == f"Candidate flag: {value}")
            ).first()
            if existing is None:
                session.add(
                    Finding(
                        project_id=attempt.project_id,
                        severity="info",
                        title=f"Candidate flag: {value}",
                        reproduction="Recognized by flag candidate syntax; artifact references retained when available.",
                        evidence_refs=evidence_refs,
                    )
                )
            session.add(
                WorkerEvent(
                    project_id=attempt.project_id,
                    worker_id=attempt.worker_id,
                    intent_id=attempt.intent_id,
                    attempt_id=attempt.id,
                    event_type="finding.flag_candidate",
                    payload_json={"value": value, "evidence_refs": evidence_refs},
                )
            )
            verified_flags.append((value, evidence_refs))

        if decoy_feedback and not verified_flags:
            # Reflect the validation decision in this round's checkpoint as
            # well as the next pending intent.  This prevents a fake flag from
            # being summarized as a successful terminal result.
            output["status"] = "partial"
            output["summary"] = " ".join(decoy_feedback)
            decision = output.get("decision_summary") if isinstance(output.get("decision_summary"), dict) else {}
            decision["reason_summary"] = decoy_feedback[-1]
            decision["next_tool_plan"] = ["Continue deeper investigation for the real flag"]
            output["decision_summary"] = decision

        if verified_flags:
            project = session.get(Project, attempt.project_id)
            if project is not None:
                project.status = "COMPLETED"
                project.updated_at = now_utc()
                session.add(project)
                pending_intents = session.exec(
                    select(Intent).where(
                        Intent.project_id == attempt.project_id,
                        Intent.status == "PENDING",
                        Intent.id != attempt.intent_id,
                    )
                ).all()
                for intent in pending_intents:
                    intent.status = "CANCELLED"
                    intent.updated_at = now_utc()
                    session.add(intent)
                session.add(
                    WorkerEvent(
                        project_id=attempt.project_id,
                        worker_id=attempt.worker_id,
                        intent_id=attempt.intent_id,
                        attempt_id=attempt.id,
                        event_type="project.completed",
                        payload_json={
                            "reason": "candidate flag detected",
                            "candidate_flags": [value for value, _ in verified_flags],
                            "cancelled_intent_ids": [intent.id for intent in pending_intents],
                        },
                    )
                )

        attempt.status = output.get("status", "partial").upper()
        attempt.result_summary = output.get("summary")
        attempt.artifact_refs = artifact_refs
        attempt.finished_at = now_utc()
        llm_trace.attempt_id = attempt.id
        session.add(attempt)
        session.add(llm_trace)
        session.add(
            WorkerEvent(
                project_id=attempt.project_id,
                worker_id=attempt.worker_id,
                intent_id=attempt.intent_id,
                attempt_id=attempt.id,
                event_type="attempt.completed",
                payload_json={"status": attempt.status, "summary": attempt.result_summary},
            )
        )
        session.commit()

    @staticmethod
    def _objects(value: object) -> list[dict[str, Any]]:
        """Ignore malformed model list entries instead of aborting a run."""
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    @staticmethod
    def _flag_values(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        values: list[str] = []
        for item in value:
            candidate = item.get("value") if isinstance(item, dict) else item
            if candidate is not None and str(candidate).strip():
                values.append(str(candidate).strip())
        return values

    @staticmethod
    def _confidence(value: object) -> float:
        labels = {"very_low": 0.1, "low": 0.3, "medium": 0.5, "moderate": 0.5, "high": 0.8, "very_high": 0.95}
        if isinstance(value, str):
            value = labels.get(value.strip().lower().replace(" ", "_"), 0.5)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.5
        return min(1.0, max(0.0, number)) if math.isfinite(number) else 0.5

    @staticmethod
    def _priority(value: object) -> float:
        labels = {"low": 0.25, "medium": 0.5, "high": 0.8, "critical": 1.0}
        if isinstance(value, str):
            value = labels.get(value.strip().lower(), 0.5)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.5
        return min(1.0, max(0.0, number)) if math.isfinite(number) else 0.5

    @staticmethod
    def _is_evidence_backed_derivation(
        session: Session,
        *,
        validator: FlagValidator,
        value: str,
        evidence_refs: list[str],
        fact_candidates: object,
        project_id: str,
    ) -> bool:
        """Accept a computed flag when the worker ties it to trusted evidence.

        Some challenges contain only an encoded value, so the recovered flag
        cannot be found verbatim in an artifact.  In that case the final
        result remains archivable when a fact explicitly records the derived
        value and cites one of the attempt's trusted artifacts.
        """
        if not validator.is_valid_flag_value(value):
            return False
        trusted_refs = {ref for ref in evidence_refs if validator.is_trusted_artifact_ref(session, ref, project_id=project_id)}
        if not trusted_refs or not isinstance(fact_candidates, list):
            return False
        normalized_value = value.strip().lower()
        for fact in fact_candidates:
            if not isinstance(fact, dict):
                continue
            statement = fact.get("statement")
            fact_refs = fact.get("evidence_refs")
            if not isinstance(statement, str) or not isinstance(fact_refs, list):
                continue
            if normalized_value in statement.lower() and trusted_refs.intersection(fact_refs):
                return True
        return False
