from __future__ import annotations

import math
import hashlib
from typing import Any

from sqlmodel import Session, select

from aurora.models import Artifact, Attempt, Finding, FlagCandidate, LLMTrace, Project, WorkerEvent, now_utc
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
        evidence_verified_values = {
            str(candidate.get("value")).strip()
            for candidate in proposed_flags
            if isinstance(candidate, dict)
            and candidate.get("value")
            and validator.is_verified_candidate(
                session,
                value=str(candidate.get("value")),
                artifact_ref=candidate.get("artifact_ref"),
                project_id=attempt.project_id,
            )
        }
        unsupported_values = set(normalized_flags) - evidence_verified_values

        for candidate in self._objects(output.get("fact_candidates")):
            statement = str(candidate.get("statement", "unspecified fact"))
            # Do not preserve a worker's "solved" conclusion when the value
            # itself explicitly says that it is a fake flag.
            if any(value.lower() in statement.lower() for value in [*decoy_flags, *unsupported_values]):
                continue
            candidate_refs = candidate.get("evidence_refs", []) if isinstance(candidate.get("evidence_refs"), list) else []
            evidence_refs = self._project_artifact_refs(
                session,
                project_id=attempt.project_id,
                refs=artifact_refs or candidate_refs,
            )
            repository.upsert_fact(
                session,
                project_id=attempt.project_id,
                statement=statement,
                category=candidate.get("category", "general"),
                confidence=self._confidence(candidate.get("confidence")),
                evidence_refs=evidence_refs,
                evidence_items=self._evidence_items(
                    candidate.get("evidence_items"),
                    allowed_refs=set(evidence_refs),
                    default_refs=evidence_refs,
                ),
                source_intent_id=attempt.intent_id,
                source_attempt_id=attempt.id,
            )

        verified_flags: list[tuple[str, list[str]]] = []
        decoy_feedback: list[str] = []
        for candidate in proposed_flags:
            value = candidate.get("value") if isinstance(candidate, dict) else str(candidate)
            if not value:
                continue
            artifact_ref = candidate.get("artifact_ref") if isinstance(candidate, dict) else None
            evidence_refs = self._project_artifact_refs(
                session,
                project_id=attempt.project_id,
                refs=[artifact_ref] if artifact_ref else artifact_refs,
            )
            if validator.is_decoy_flag_value(value):
                reason = "its brace payload explicitly identifies it as a fake/decoy flag"
                self._upsert_flag_candidate(
                    session,
                    attempt=attempt,
                    value=value,
                    status="REJECTED",
                    provenance_kind="UNVERIFIED",
                    evidence_refs=evidence_refs,
                    rejection_reason=reason,
                )
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
            if value.strip() in evidence_verified_values and not directly_verified:
                # The deterministic artifact scanner appends a provenance-bearing
                # copy of model-proposed values found in trusted output.
                continue
            if not syntactically_valid or not directly_verified:
                reason = (
                    "not a meaningful prefix{payload} candidate or is a placeholder"
                    if not syntactically_valid
                    else "the candidate is not present in trusted target evidence or a successful flag.verify replay"
                )
                self._upsert_flag_candidate(
                    session,
                    attempt=attempt,
                    value=value,
                    status="REJECTED",
                    provenance_kind="UNVERIFIED",
                    evidence_refs=evidence_refs,
                    rejection_reason=reason,
                )
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
            evidence_artifact = session.get(Artifact, artifact_ref)
            provenance_kind = "DERIVED_REPLAY" if evidence_artifact and evidence_artifact.origin_kind == "verified_derivation" else "OBSERVED"
            flag_candidate = self._upsert_flag_candidate(
                session,
                attempt=attempt,
                value=value,
                status="LOCAL_VERIFIED",
                provenance_kind=provenance_kind,
                evidence_refs=evidence_refs,
                verification_artifact_ref=artifact_ref if provenance_kind == "DERIVED_REPLAY" else None,
            )
            existing = session.exec(
                select(Finding).where(Finding.project_id == attempt.project_id, Finding.title == f"Candidate flag: {value}")
            ).first()
            if existing is None:
                session.add(
                    Finding(
                        project_id=attempt.project_id,
                        severity="info",
                        title=f"Candidate flag: {value}",
                        reproduction=f"Locally verified from {provenance_kind.lower()} evidence; final correctness requires platform or manual acceptance.",
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
                    payload_json={"value": value, "candidate_id": flag_candidate.id, "status": "LOCAL_VERIFIED", "provenance_kind": provenance_kind, "evidence_refs": evidence_refs},
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
                project.status = "FLAG_READY"
                project.updated_at = now_utc()
                session.add(project)
                session.add(
                    WorkerEvent(
                        project_id=attempt.project_id,
                        worker_id=attempt.worker_id,
                        intent_id=attempt.intent_id,
                        attempt_id=attempt.id,
                        event_type="project.flag_ready",
                        payload_json={
                            "reason": "candidate flag passed local evidence validation",
                            "candidate_flags": [value for value, _ in verified_flags],
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
    def _upsert_flag_candidate(
        session: Session,
        *,
        attempt: Attempt,
        value: str,
        status: str,
        provenance_kind: str,
        evidence_refs: list[str],
        verification_artifact_ref: str | None = None,
        rejection_reason: str | None = None,
    ) -> FlagCandidate:
        normalized = value.strip()
        value_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        candidate = session.exec(
            select(FlagCandidate).where(FlagCandidate.project_id == attempt.project_id, FlagCandidate.value_hash == value_hash)
        ).first()
        if candidate is None:
            candidate = FlagCandidate(project_id=attempt.project_id, value=normalized, value_hash=value_hash)
        # Trusted evidence may rehabilitate a previously unverified proposal.
        if candidate.status != "ACCEPTED" and (status == "LOCAL_VERIFIED" or candidate.status != "LOCAL_VERIFIED"):
            candidate.status = status
            candidate.provenance_kind = provenance_kind
            candidate.artifact_refs = list(dict.fromkeys(evidence_refs))
            candidate.verification_artifact_ref = verification_artifact_ref
            candidate.rejection_reason = rejection_reason
            candidate.source_attempt_id = attempt.id
            candidate.source_worker_id = attempt.worker_id
            candidate.updated_at = now_utc()
        session.add(candidate)
        session.flush()
        return candidate

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
    def _project_artifact_refs(session: Session, *, project_id: str, refs: object) -> list[str]:
        if not isinstance(refs, list):
            return []
        trusted: list[str] = []
        for ref in refs:
            if not isinstance(ref, str) or ref in trusted:
                continue
            artifact = session.get(Artifact, ref)
            if artifact is not None and artifact.project_id == project_id:
                trusted.append(ref)
        return trusted

    @staticmethod
    def _evidence_items(value: object, *, allowed_refs: set[str], default_refs: list[str]) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        items: list[dict[str, Any]] = []
        for item in value[:10]:
            if not isinstance(item, dict) or not isinstance(item.get("description"), str):
                continue
            description = item["description"].strip()
            refs = item.get("artifact_refs")
            if not description or not isinstance(refs, list):
                continue
            artifact_refs = list(dict.fromkeys(ref for ref in refs if isinstance(ref, str) and ref in allowed_refs)) or default_refs
            if artifact_refs:
                items.append({"description": description[:2000], "artifact_refs": artifact_refs})
        return items

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
