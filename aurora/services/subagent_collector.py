from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlmodel import Session

from aurora.models import Attempt, ContextSnapshot, LLMTrace, Worker, WorkerEvent, now_utc
from aurora.services.artifact_store import ArtifactStore


class SubagentCollector:
    """Imports synchronous same-container child runs after the parent Codex command exits."""

    def __init__(self, artifact_store: ArtifactStore | None = None) -> None:
        self.artifact_store = artifact_store or ArtifactStore()

    def collect(self, session: Session, *, parent_worker: Worker, parent_attempt: Attempt, parent_snapshot: ContextSnapshot, workspace: Path) -> list[dict[str, Any]]:
        manifest = workspace / "subagents" / "manifest.jsonl"
        if not manifest.exists():
            return []
        reports: list[dict[str, Any]] = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            run_id = str(record.get("run_id", ""))
            if not run_id or session.get(Worker, run_id) is not None:
                continue
            reports.append(self._import_record(session, parent_worker, parent_attempt, parent_snapshot, record))
        return reports

    def _import_record(self, session: Session, parent_worker: Worker, parent_attempt: Attempt, parent_snapshot: ContextSnapshot, record: dict[str, Any]) -> dict[str, Any]:
        run_id = str(record["run_id"])
        objective = str(record.get("objective", "Same-container subagent task"))
        output = record.get("output") if isinstance(record.get("output"), dict) else {}
        status = str(output.get("status", "failed")).upper()
        if status not in {"SUCCESS", "PARTIAL", "FAILED"}:
            status = "FAILED"
        worker = Worker(id=run_id, project_id=parent_worker.project_id, intent_id=parent_worker.intent_id, parent_worker_id=parent_worker.id, execution_kind="subagent", agent_profile_id="solver.subagent", capability_set=list(record.get("capability_tags") or []), status="COMPLETED" if status != "FAILED" else "FAILED", lease=parent_worker.lease)
        sections = dict(parent_snapshot.sections_json)
        sections["current_intent"] = {"id": parent_worker.intent_id, "objective": objective, "parent_worker_id": parent_worker.id}
        snapshot = ContextSnapshot(project_id=parent_worker.project_id, intent_id=parent_worker.intent_id, worker_id=worker.id, sections_json=sections, section_metrics_json={"derived_from_parent": {"estimated_tokens": parent_snapshot.estimated_tokens}}, visible_tools_json=[tool for tool in parent_snapshot.visible_tools_json if tool.get("name") != "subagent.spawn"], output_schema_json=parent_snapshot.output_schema_json, total_chars=len(json.dumps(sections, ensure_ascii=False)), estimated_tokens=max(1, len(json.dumps(sections, ensure_ascii=False)) // 4), truncation_report_json={"derived_from": parent_snapshot.id})
        session.add(worker)
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)
        artifact = self.artifact_store.write_text(session, project_id=parent_worker.project_id, content=str(record.get("transcript", "")), summary=f"Same-container subagent {run_id} exit={record.get('exit_code')}", artifact_type="subagent-transcript", origin_kind="model_output")
        attempt = Attempt(project_id=parent_worker.project_id, intent_id=parent_worker.intent_id, worker_id=worker.id, parent_attempt_id=parent_attempt.id, status=status, result_summary=str(output.get("summary", "Subagent did not return a summary.")), failure_reason=str(record.get("error")) if record.get("error") else None, artifact_refs=[artifact.id], finished_at=now_utc())
        session.add(attempt)
        session.commit()
        session.refresh(attempt)
        session.add(LLMTrace(project_id=parent_worker.project_id, worker_id=worker.id, intent_id=parent_worker.intent_id, attempt_id=attempt.id, context_snapshot_id=snapshot.id, prompt_hash=hashlib.sha256(str(record.get("prompt", "")).encode("utf-8")).hexdigest(), model="codex-subagent", input_chars=len(str(record.get("prompt", ""))), estimated_input_tokens=max(1, len(str(record.get("prompt", ""))) // 4), output_chars=len(json.dumps(output, ensure_ascii=False)), estimated_output_tokens=max(1, len(json.dumps(output, ensure_ascii=False)) // 4), provider_usage_json={"runtime": "same-container-subagent", "exit_code": record.get("exit_code")}, decision_summary=output.get("decision_summary") or {}, structured_output=output))
        session.add(WorkerEvent(project_id=parent_worker.project_id, worker_id=worker.id, intent_id=parent_worker.intent_id, attempt_id=attempt.id, event_type="subagent.completed", payload_json={"parent_worker_id": parent_worker.id, "parent_attempt_id": parent_attempt.id, "status": status, "artifact_refs": [artifact.id]}))
        session.commit()
        return {"worker_id": worker.id, "status": status.lower(), "summary": attempt.result_summary, "artifact_refs": [artifact.id]}
