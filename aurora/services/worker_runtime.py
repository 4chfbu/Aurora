from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.models import Artifact, Attempt, ContextSnapshot, Fact, Intent, LLMTrace, Project, ProjectCoordinationState, ToolTrace, Worker, WorkerEvent, now_utc
from aurora.services.llm_http import LLMRequestError, chat_completion
from aurora.services.artifact_store import ArtifactStore
from aurora.services.blackboard_repository import route_fingerprint
from aurora.services.flag_validator import FlagValidator
from aurora.services.command_runner import AutoCommandRunner, CommandResult, CommandRunner
from aurora.services.prompt_renderer import PromptRenderer
from aurora.services.tool_profiles import image_for_profile
from aurora.services.deadlines import execution_deadline, remaining_seconds
from aurora.services.progress import is_sync_request, target_transport_failed, transport_failed
from aurora.services.context_memory import parent_attempt_candidates
from aurora.services.evidence_context import current_environment_id
from aurora.services.resume_store import restore_resume_manifest
from aurora.services.runtime_usage import session_token_usage, session_usage_delta


@dataclass
class RuntimeOutput:
    status: str
    summary: str
    structured_output: dict[str, Any]
    llm_trace: LLMTrace


@dataclass
class ConcludeFallbackOutcome:
    attempted: bool
    recovered: bool
    diagnostic: dict[str, Any]
    structured_output: dict[str, Any] | None = None
    command_result: CommandResult | None = None
    artifact: Artifact | None = None


class WorkerRuntime(Protocol):
    model: str

    def execute(self, session: Session, *, worker: Worker, snapshot: ContextSnapshot) -> RuntimeOutput: ...


EXECUTABLE_TOOLS = [
    "http.request",
    "browser.interact",
    "network.scan",
    "web.enumerate",
    "binary.inspect",
    "forensic.inspect",
    "flag.verify",
    "flag.submit",
    "sandbox.exec",
]

VALID_STRUCTURED_OUTPUT_SOURCES = frozenset({"last_message_file", "stdout_fallback", "stderr_fallback"})
CONCLUDE_FALLBACK_FATAL_FAILURES = frozenset({
    "provider_unavailable",
    "provider_model_metadata_missing",
    "provider_invalid_request",
    "provider_reasoning_error",
    "resource_terminated",
})


def _utc_datetime(value: datetime) -> datetime:
    """Treat SQLite's timezone-less timestamps as UTC for runtime ordering."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class OpenAICompatibleRuntime:
    executable_tools = EXECUTABLE_TOOLS

    def __init__(self) -> None:
        self.settings = get_settings()
        self.model = self.settings.llm_model

    def execute(self, session: Session, *, worker: Worker, snapshot: ContextSnapshot) -> RuntimeOutput:
        if not self.settings.llm_api_key:
            raise RuntimeError("AURORA_WORKER_RUNTIME=openai requires AURORA_LLM_API_KEY or OPENAI_API_KEY")

        prompt = self._build_prompt(worker, snapshot)
        model = self.settings.model_for_role(str(worker.budgets.get("model_role", "solver")))
        response = self._call_llm(prompt, model=model, deadline_at=execution_deadline(session, worker.project_id, worker.id))
        structured = self._parse_structured_output(response["content"])
        structured = self._normalize_structured_output(structured, snapshot)
        output_json = json.dumps(structured, ensure_ascii=False, sort_keys=True)
        prompt_json = json.dumps(prompt, ensure_ascii=False, sort_keys=True)
        usage = response.get("usage") or {}
        decision_summary = structured.get("decision_summary") or {}
        trace = LLMTrace(
            project_id=snapshot.project_id,
            worker_id=worker.id,
            intent_id=worker.intent_id,
            context_snapshot_id=snapshot.id,
            prompt_hash=hashlib.sha256(prompt_json.encode("utf-8")).hexdigest(),
            model=model,
            input_chars=len(prompt_json),
            estimated_input_tokens=int(usage.get("prompt_tokens") or max(1, len(prompt_json) // 4)),
            output_chars=len(output_json),
            estimated_output_tokens=int(usage.get("completion_tokens") or max(1, len(output_json) // 4)),
            provider_usage_json=usage,
            decision_summary=decision_summary,
            structured_output=structured,
        )
        session.add(trace)
        session.commit()
        session.refresh(trace)
        return RuntimeOutput(structured.get("status", "partial"), structured.get("summary", "LLM runtime completed."), structured, trace)

    def _build_prompt(self, worker: Worker, snapshot: ContextSnapshot) -> list[dict[str, str]]:
        return PromptRenderer().render_messages(worker=worker, snapshot=snapshot)

    def _call_llm(self, messages: list[dict[str, str]], *, model: str | None = None, deadline_at: datetime | None = None) -> dict[str, Any]:
        try:
            body = chat_completion(
                settings=self.settings,
                model=model or self.model,
                messages=messages,
                timeout=self.settings.llm_timeout_seconds,
                deadline_at=deadline_at,
            )
        except LLMRequestError as exc:
            raise RuntimeError(str(exc)) from exc

        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError(f"LLM API returned no choices: {body}")
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if not content:
            raise RuntimeError(f"LLM API returned empty content: {body}")
        return {"content": content, "usage": body.get("usage") or {}, "raw": body}

    def _parse_structured_output(self, content: str) -> dict[str, Any]:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if not match:
                raise RuntimeError(f"LLM output is not JSON: {content[:1000]}")
            parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise RuntimeError("LLM output must be a JSON object")
        return parsed

    def _normalize_structured_output(self, structured: dict[str, Any], snapshot: ContextSnapshot) -> dict[str, Any]:
        intent = snapshot.sections_json.get("current_intent", {})
        status = structured.get("status") or "partial"
        if status not in {"success", "partial", "failed"}:
            status = "partial"
        summary = structured.get("summary") or "LLM generated a structured worker result."
        tool_requests = structured.get("tool_requests")
        if not isinstance(tool_requests, list):
            tool_requests = []
        visible_tool_names = {tool.get("name") for tool in snapshot.visible_tools_json}
        filtered_tool_requests = []
        ordered_tool_requests = [request for request in tool_requests if isinstance(request, dict)]
        ordered_tool_requests.sort(key=lambda request: {"flag.verify": 0, "flag.submit": 1}.get(request.get("tool_name"), 2))
        for request in ordered_tool_requests[:3]:
            tool_name = request.get("tool_name")
            if tool_name in visible_tool_names:
                activity_label = self._safe_activity_label(request.get("activity_label"))
                # Codex commonly calls the tool payload ``parameters`` (the
                # name used by the visible capability schema), while the
                # scheduler historically consumed ``request``.  Normalize
                # both forms at the runtime boundary so a valid call cannot
                # silently become an empty request.
                payload = request.get("request")
                if not isinstance(payload, dict):
                    payload = request.get("parameters")
                if not isinstance(payload, dict):
                    payload = request.get("arguments")
                if not isinstance(payload, dict):
                    payload = request.get("params")
                if not isinstance(payload, dict):
                    payload = {}
                filtered_tool_requests.append({"tool_name": tool_name, "request": payload, **({"activity_label": activity_label} if activity_label else {})})
        if not filtered_tool_requests and status == "partial":
            fallback_tool = self._select_tool(intent, visible_tool_names)
            filtered_tool_requests = [{"tool_name": fallback_tool, "request": self._tool_request(intent, fallback_tool)}]
        decision_summary = structured.get("decision_summary")
        if not isinstance(decision_summary, dict):
            decision_summary = {
                "selected_intent": intent.get("objective", "unknown objective"),
                "reason_summary": "模型生成了结构化工具计划。",
                "next_tool_plan": [request["tool_name"] for request in filtered_tool_requests],
            }
        suggested = self._normalize_suggested_intents(structured.get("suggested_intents"), intent)
        blockers = self._normalize_blockers(structured.get("blockers"))
        if status == "partial" and not suggested and not blockers:
            blockers = [{"kind": "missing_evidence", "reason": "本轮没有形成可执行的续跑路线", "next_step": "查询最新 checkpoint 并提出唯一下一步"}]
        return {
            "status": status,
            "summary": summary,
            "fact_candidates": structured.get("fact_candidates") if isinstance(structured.get("fact_candidates"), list) else [],
            "hypotheses": structured.get("hypotheses") if isinstance(structured.get("hypotheses"), list) else [],
            "artifact_refs": structured.get("artifact_refs") if isinstance(structured.get("artifact_refs"), list) else [],
            "failed_attempts": structured.get("failed_attempts") if isinstance(structured.get("failed_attempts"), list) else [],
            "suggested_intents": suggested,
            "blockers": blockers,
            "fork_recommendations": structured.get("fork_recommendations") if isinstance(structured.get("fork_recommendations"), list) else [],
            "subagent_reports": structured.get("subagent_reports") if isinstance(structured.get("subagent_reports"), list) else [],
            "candidate_flags": structured.get("candidate_flags") if isinstance(structured.get("candidate_flags"), list) else [],
            "decision_summary": decision_summary,
            "tool_requests": filtered_tool_requests,
        }

    @staticmethod
    def _normalize_suggested_intents(value: object, current_intent: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in value[:3]:
            if isinstance(item, str):
                objective = item.strip()[:2000]
                if objective:
                    normalized.append({"objective": objective, "capability_tags": ["blackboard.query"], "priority": 0.5, "risk_level": "low"})
                continue
            if not isinstance(item, dict) or not isinstance(item.get("objective"), str) or not item["objective"].strip():
                continue
            capabilities = item.get("capability_tags", item.get("capabilities", []))
            if not isinstance(capabilities, list):
                capabilities = []
            risk = item.get("risk_level", "low")
            try:
                priority = float(item.get("priority", 0.5) or 0.5)
            except (TypeError, ValueError):
                priority = 0.5
            normalized.append({
                **item,
                "objective": item["objective"].strip()[:2000],
                "capability_tags": [str(tag) for tag in capabilities if str(tag).strip()][:8] or ["blackboard.query"],
                "priority": priority,
                "risk_level": risk if risk in {"low", "medium", "high"} else "low",
            })
        return normalized

    @staticmethod
    def _normalize_blockers(value: object) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, str]] = []
        for item in value[:4]:
            if isinstance(item, str) and item.strip():
                normalized.append({"kind": "missing_evidence", "reason": item.strip()[:1000]})
            elif isinstance(item, dict):
                kind = str(item.get("kind", item.get("type", "missing_evidence")))
                if kind not in {"target", "session", "paid_confirmation", "missing_evidence"}:
                    kind = "missing_evidence"
                reason = str(item.get("reason", item.get("message", "blocker"))).strip()
                if reason:
                    entry = {"kind": kind, "reason": reason[:1000]}
                    if item.get("next_step"):
                        entry["next_step"] = str(item["next_step"])[:1000]
                    normalized.append(entry)
        return normalized

    def _select_tool(self, intent: dict[str, Any], visible_tool_names: set[str]) -> str:
        for tag in intent.get("capability_tags") or []:
            if tag in self.executable_tools and tag in visible_tool_names:
                return tag
        return "sandbox.exec"

    def _tool_request(self, intent: dict[str, Any], selected_tool: str) -> dict[str, Any]:
        explicit = intent.get("tool_request")
        if isinstance(explicit, dict) and explicit:
            return explicit
        if selected_tool == "sandbox.exec":
            return {"command": "printf 'Aurora OpenAI runtime fallback tool request\\n'", "cwd": ".", "timeout_seconds": 5}
        return {"cwd": ".", "timeout_seconds": 10}


class CodexHarnessRuntime:
    model = "codex-harness"

    def __init__(self, artifact_store: ArtifactStore | None = None, command_runner: CommandRunner | None = None) -> None:
        self.settings = get_settings()
        self.artifact_store = artifact_store or ArtifactStore()
        self.command_runner = command_runner

    def execute(self, session: Session, *, worker: Worker, snapshot: ContextSnapshot) -> RuntimeOutput:
        started = time.monotonic()
        workspace = self.settings.codex_workspace_dir / snapshot.project_id / worker.id
        model_role = str(worker.budgets.get("model_role", "solver"))
        model = self.settings.model_for_role(model_role)
        model_context_window, auto_compact_token_limit = self.settings.codex_metadata_for_role(model_role)
        attempt = session.exec(
            select(Attempt).where(Attempt.worker_id == worker.id, Attempt.status == "RUNNING").order_by(Attempt.started_at.desc())
        ).first()
        requested_parent_id = attempt.parent_attempt_id if attempt else None
        control_token = secrets.token_urlsafe(32)
        resume_thread_id = self._prepare_attempt(
            session,
            worker=worker,
            attempt=attempt,
            control_token=control_token,
            workspace=workspace,
        )
        restored_work = session.exec(select(WorkerEvent).where(
            WorkerEvent.attempt_id == attempt.id, WorkerEvent.event_type == "codex.work_state_restored",
        )).first() if attempt else None
        if attempt is not None and (attempt.parent_attempt_id != requested_parent_id or restored_work is not None):
            # A corrupt preferred bundle may select a different valid parent.
            # Render and record the handoff belonging to the state actually restored.
            from aurora.services.context_builder import ContextBuilder
            ContextBuilder().build(session, project_id=worker.project_id, intent_id=worker.intent_id,
                                   worker_id=worker.id, existing_snapshot=snapshot)
        prompt_file = self._write_prompt(session, worker, snapshot, preserve_inputs=(workspace / "inputs" / "manifest.json").is_file())
        command = self._render_command(prompt_file, model=model)
        if attempt is not None:
            try:
                self._sync_blackboard(session, worker=worker, attempt=attempt)
            except Exception:
                session.rollback()
        tool_environment = snapshot.sections_json.get("tool_environment") or {}
        subagent_policy = (snapshot.sections_json.get("operating_mode") or {}).get("subagents") or {}
        profile = str(tool_environment.get("profile") or "heavy")
        runner = self.command_runner or AutoCommandRunner(
            prefer_kali=True,
            allow_local_fallback=False,
            image=image_for_profile(self.settings, profile),
            expected_profile=profile,
            environment_overrides={
                "OPENAI_MODEL": model,
                "AURORA_CODEX_MODEL_CONTEXT_WINDOW": str(model_context_window),
                "AURORA_CODEX_AUTO_COMPACT_TOKEN_LIMIT": str(auto_compact_token_limit),
                "AURORA_WORKER_CONTROL_BASE_URL": self.settings.worker_control_base_url,
                "AURORA_WORKER_ID": worker.id,
                "AURORA_WORKER_CONTROL_TOKEN": control_token,
                "AURORA_WORKER_CONTROL_TIMEOUT_SECONDS": str(self.settings.worker_control_timeout_seconds),
                "AURORA_WORKER_CONTROL_MAX_ATTEMPTS": str(self.settings.worker_control_max_attempts),
                "AURORA_CODEX_RESUME_THREAD_ID": resume_thread_id or "",
                "AURORA_SUBAGENTS_ENABLED": "true" if subagent_policy.get("enabled") else "false",
                "AURORA_SUBAGENTS_MAX_PER_WORKER": str(max(1, int(subagent_policy.get("max_per_worker") or 1))),
                "AURORA_SUBAGENTS_MAX_CONCURRENT": str(max(1, int(subagent_policy.get("max_concurrent") or 1))),
            },
        )
        usage_before = session_token_usage(workspace, resume_thread_id)
        primary_started = time.monotonic()
        try:
            completed = self._run_command(
                command,
                prompt_file.parent,
                timeout_seconds=worker.budgets.get("hard_timeout_seconds"),
                soft_timeout_seconds=worker.budgets.get("soft_timeout_seconds"),
                finalize_grace_seconds=worker.budgets.get("finalize_grace_seconds"),
                deadline_at=execution_deadline(session, worker.project_id, worker.id),
                runner=runner,
                on_output=lambda stream, line: self._record_codex_event(
                    session,
                    worker=worker,
                    attempt=attempt,
                    stream=stream,
                    line=line,
                    artifact_store=self.artifact_store,
                ),
            )
        finally:
            if attempt is not None:
                attempt.codex_control_token_hash = None
                session.add(attempt)
                session.commit()
        primary_finished = time.monotonic()
        transcript = self._bounded_transcript(self._transcript(command, completed))
        artifact = self.artifact_store.write_text(
            session,
            project_id=snapshot.project_id,
            source_attempt_id=attempt.id if attempt else None,
            content=transcript,
            summary=f"Codex harness transcript {completed.backend} exit={completed.exit_code}",
            artifact_type="codex-transcript",
            origin_kind="model_output",
        )
        output_file = prompt_file.parent / "aurora-last-message.json"
        structured, output_diagnostic = self._parse_or_synthesize(
            output_file=output_file,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.exit_code,
            failure_kind=completed.failure_kind,
            artifact_id=artifact.id,
            snapshot=snapshot,
            session=session,
            worker=worker,
            attempt=attempt,
        )
        try:
            conclude_fallback = self._try_conclude_fallback(
                session,
                worker=worker,
                attempt=attempt,
                snapshot=snapshot,
                workspace=prompt_file.parent,
                model=model,
                runner=runner,
                primary_completed=completed,
                primary_diagnostic=output_diagnostic,
            )
        except Exception as exc:
            # Conclude is a recovery path. Its own control-plane or storage
            # failure must never discard the primary transcript and failure
            # result that were already captured.
            session.rollback()
            conclude_fallback = ConcludeFallbackOutcome(
                attempted=True,
                recovered=False,
                diagnostic={
                    "attempted": True,
                    "recovered": False,
                    "error": f"unexpected conclude fallback failure: {str(exc)[:500]}",
                },
            )
            if attempt is not None:
                try:
                    session.add(
                        WorkerEvent(
                            project_id=worker.project_id,
                            worker_id=worker.id,
                            intent_id=worker.intent_id,
                            attempt_id=attempt.id,
                            event_type="attempt.conclude_fallback_failed",
                            payload_json=conclude_fallback.diagnostic,
                        )
                    )
                    session.commit()
                except Exception:
                    session.rollback()
        output_diagnostic["conclude_fallback"] = conclude_fallback.diagnostic
        if conclude_fallback.recovered and conclude_fallback.structured_output is not None:
            structured = conclude_fallback.structured_output
        structured.setdefault("artifact_refs", [])
        if artifact.id not in structured["artifact_refs"]:
            structured["artifact_refs"].append(artifact.id)
        if conclude_fallback.artifact is not None and conclude_fallback.artifact.id not in structured["artifact_refs"]:
            structured["artifact_refs"].append(conclude_fallback.artifact.id)

        # Persist runtime side effects only after the optional resumed turn so
        # future attempts restore the newest Codex state and MCP audit log.
        mcp_import = self._import_mcp_events(
            session,
            worker=worker,
            snapshot=snapshot,
            workspace=prompt_file.parent,
        )
        resume_manifest = self._persist_resume_manifest(
            session,
            attempt=attempt,
            workspace=prompt_file.parent,
        ) if attempt is not None else None

        elapsed_ms = round((time.monotonic() - started) * 1000)
        output_json = json.dumps(structured, ensure_ascii=False, sort_keys=True)
        prompt_text = prompt_file.read_text(encoding="utf-8")
        decision_summary = structured.get("decision_summary") or {}
        usage = dict(attempt.token_usage or {}) if attempt is not None else {}
        usage_source = "turn.completed" if usage else "unavailable"
        session_usage = session_usage_delta(usage_before, session_token_usage(workspace, attempt.codex_thread_id)) if attempt else {}
        if session_usage and all(session_usage.get(key, 0) >= value for key, value in usage.items()):
            usage = session_usage
            usage_source = "session.token_count"
            attempt.token_usage = usage
            session.add(attempt)
        trace = LLMTrace(
            project_id=snapshot.project_id,
            worker_id=worker.id,
            intent_id=worker.intent_id,
            attempt_id=attempt.id if attempt else None,
            context_snapshot_id=snapshot.id,
            prompt_hash=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
            model=model,
            input_chars=len(prompt_text),
            estimated_input_tokens=usage.get("input_tokens", max(1, len(prompt_text) // 4)),
            output_chars=len(output_json),
            estimated_output_tokens=usage.get("output_tokens", max(1, len(output_json) // 4)),
            provider_usage_json={
                "runtime": "codex",
                "usage": usage,
                "usage_source": usage_source,
                "usage_complete": bool(usage) and completed.exit_code == 0 and not completed.finalization_reason,
                "timing_ms": {
                    "setup": round((primary_started - started) * 1000),
                    "primary_command": round((primary_finished - primary_started) * 1000),
                    "finalization": round((time.monotonic() - primary_finished) * 1000),
                },
                "command": command,
                "backend": completed.backend,
                "exit_code": completed.exit_code,
                "stdout_bytes": len(completed.stdout.encode("utf-8", errors="replace")),
                "stderr_bytes": len(completed.stderr.encode("utf-8", errors="replace")),
                "duration_ms": elapsed_ms,
                "model_role": model_role,
                "requested_model": model,
                "model_context_window": model_context_window,
                "auto_compact_token_limit": auto_compact_token_limit,
                "hard_timeout_seconds": worker.budgets.get("hard_timeout_seconds"),
                "soft_timeout_seconds": worker.budgets.get("soft_timeout_seconds"),
                "finalize_grace_seconds": worker.budgets.get("finalize_grace_seconds"),
                "finalization_reason": completed.finalization_reason,
                "model_metadata_source": "explicit" if self.settings.codex_require_explicit_model_metadata else "configured_default",
                "model_metadata_warning": self._has_model_metadata_warning(f"{completed.stdout}\n{completed.stderr}"),
                "output": output_diagnostic,
                "conclude_fallback": {
                    **conclude_fallback.diagnostic,
                    "artifact_id": conclude_fallback.artifact.id if conclude_fallback.artifact else None,
                    "exit_code": conclude_fallback.command_result.exit_code if conclude_fallback.command_result else None,
                    "failure_kind": conclude_fallback.command_result.failure_kind if conclude_fallback.command_result else None,
                },
                "mcp": mcp_import,
                "resume_manifest_artifact_id": resume_manifest.id if resume_manifest else None,
            },
            decision_summary=decision_summary,
            structured_output=structured,
        )
        session.add(trace)
        session.commit()
        session.refresh(trace)
        return RuntimeOutput(structured.get("status", "partial"), structured.get("summary", "Codex harness completed."), structured, trace)

    def _prepare_attempt(self, session: Session, *, worker: Worker, attempt: Attempt | None, control_token: str, workspace: Path | None = None) -> str | None:
        if attempt is None:
            return None
        requested_parent = session.get(Attempt, attempt.parent_attempt_id) if attempt.parent_attempt_id else None
        if requested_parent is not None and requested_parent.project_id != attempt.project_id:
            requested_parent = None
        candidates = parent_attempt_candidates(
            session, project_id=attempt.project_id, intent=session.get(Intent, attempt.intent_id), current_attempt=attempt,
        )
        attempt.codex_control_token_hash = hashlib.sha256(control_token.encode("utf-8")).hexdigest()
        attempt.last_event_at = now_utc()
        parent: Attempt | None = None
        resume_thread_id: str | None = None
        active_environment = current_environment_id(session, attempt.project_id)
        environment_id = active_environment or attempt.environment_id
        if active_environment:
            attempt.environment_id = active_environment
        for candidate in candidates:
            if candidate.environment_id != environment_id:
                session.add(WorkerEvent(
                    project_id=worker.project_id, worker_id=worker.id, intent_id=worker.intent_id,
                    attempt_id=attempt.id, event_type="codex.resume_rejected",
                    payload_json={"reason": "environment_changed", "parent_attempt_id": candidate.id,
                                  "source_environment_id": candidate.environment_id, "current_environment_id": environment_id},
                ))
                continue
            if not candidate.codex_thread_id:
                continue
            valid = True
            diagnostic: dict[str, Any] = {}
            if workspace is not None:
                valid, diagnostic = self._restore_resume_manifest(
                    session,
                    parent=candidate,
                    workspace=workspace,
                )
                if not valid:
                    session.add(
                        WorkerEvent(
                            project_id=worker.project_id,
                            worker_id=worker.id,
                            intent_id=worker.intent_id,
                            attempt_id=attempt.id,
                            event_type="codex.resume_rejected",
                            payload_json=diagnostic,
                        )
                    )
                    continue
            parent = candidate
            resume_thread_id = candidate.codex_thread_id
            break
        attempt.codex_thread_id = resume_thread_id
        attempt.resume_count = 0
        if resume_thread_id is None and workspace is not None:
            # Native state is optional for continuity. Keep verified work and
            # inputs when a thread is unavailable or its target instance changed.
            logical_candidates = parent_attempt_candidates(
                session, project_id=attempt.project_id, intent=session.get(Intent, attempt.intent_id),
                current_attempt=attempt, resumable_only=False,
            )
            for candidate in logical_candidates:
                if not candidate.resume_manifest_artifact_id:
                    continue
                restored, diagnostic = restore_resume_manifest(
                    session, parent=candidate, workspace=workspace, native_session=False,
                    legacy_home=self.settings.codex_workspace_dir / candidate.project_id / candidate.worker_id / "runtime" / "codex-home",
                )
                if restored:
                    attempt.parent_attempt_id = candidate.id
                    session.add(WorkerEvent(
                        project_id=worker.project_id, worker_id=worker.id, intent_id=worker.intent_id,
                        attempt_id=attempt.id, event_type="codex.work_state_restored", payload_json=diagnostic,
                    ))
                    break
        if parent is not None and resume_thread_id:
            if requested_parent is None or parent.id != requested_parent.id:
                session.add(
                    WorkerEvent(
                        project_id=worker.project_id,
                        worker_id=worker.id,
                        intent_id=worker.intent_id,
                        attempt_id=attempt.id,
                        event_type="codex.resume_fallback_selected",
                        payload_json={
                            "parent_attempt_id": parent.id,
                            "requested_parent_attempt_id": requested_parent.id if requested_parent else None,
                        },
                    )
                )
            attempt.parent_attempt_id = parent.id
            attempt.resume_count = parent.resume_count + 1
            session.add(
                WorkerEvent(
                    project_id=worker.project_id,
                    worker_id=worker.id,
                    intent_id=worker.intent_id,
                    attempt_id=attempt.id,
                    event_type="codex.resume_scheduled",
                    payload_json={"thread_id": resume_thread_id, "resume_count": attempt.resume_count, "parent_attempt_id": parent.id},
                )
            )
        session.add(attempt)
        session.commit()
        return resume_thread_id

    def _restore_resume_manifest(
        self, session: Session, *, parent: Attempt, workspace: Path,
    ) -> tuple[bool, dict[str, Any]]:
        return restore_resume_manifest(
            session, parent=parent, workspace=workspace,
            legacy_home=self.settings.codex_workspace_dir / parent.project_id / parent.worker_id / "runtime" / "codex-home",
        )

    @staticmethod
    def _copy_resume_home(source_home: Path, target_home: Path) -> list[str]:
        """Copy resumable Codex state while tolerating legacy root-only files."""
        skipped: list[str] = []

        def ignore(directory: str, names: list[str]) -> set[str]:
            ignored: set[str] = set()
            base = Path(directory)
            for name in names:
                path = base / name
                try:
                    relative = path.relative_to(source_home)
                except ValueError:
                    relative = Path(name)
                if not CodexHarnessRuntime._is_resumable_codex_state(relative):
                    ignored.add(name)
                    continue
                readable = os.access(path, os.R_OK)
                traversable = not path.is_dir() or os.access(path, os.X_OK)
                if not readable or not traversable:
                    ignored.add(name)
                    try:
                        skipped.append(str(relative))
                    except ValueError:
                        skipped.append(name)
            return ignored

        shutil.copytree(source_home, target_home, dirs_exist_ok=True, ignore=ignore)
        return skipped

    @staticmethod
    def _record_codex_event(
        session: Session,
        *,
        worker: Worker,
        attempt: Attempt | None,
        stream: str,
        line: str,
        artifact_store: ArtifactStore | None = None,
    ) -> str | None:
        if attempt is None:
            return None
        if stream == "control":
            CodexHarnessRuntime._begin_finalization(session, worker=worker, attempt=attempt, reason=line)
            return None
        if stream != "stdout" or not line.startswith("{"):
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None
        event_type = str(event.get("type") or "unknown")[:100]
        if event_type == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = {
                key: value for key, value in event["usage"].items()
                if key.endswith("tokens") and isinstance(value, int) and not isinstance(value, bool) and value >= 0
            }
            if usage:
                attempt.token_usage = {
                    **dict(attempt.token_usage or {}),
                    **{key: int((attempt.token_usage or {}).get(key, 0)) + value for key, value in usage.items()},
                }
                session.add(WorkerEvent(
                    project_id=worker.project_id, worker_id=worker.id, intent_id=worker.intent_id,
                    attempt_id=attempt.id, event_type="codex.usage", payload_json={"usage": usage},
                ))
        thread_id = event.get("thread_id") or event.get("session_id")
        turn_id = event.get("turn_id")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if isinstance(thread_id, str) and thread_id:
            attempt.codex_thread_id = thread_id[:200]
        if isinstance(turn_id, str) and turn_id:
            attempt.codex_turn_id = turn_id[:200]
        attempt.last_event_at = now_utc()
        session.add(attempt)
        item_type = str(item.get("type") or "")[:100]
        finalization_reason: str | None = None
        if item_type == "command_execution" and str(item.get("status") or event.get("status") or "") in {"completed", "failed", "done", ""}:
            command = CodexHarnessRuntime._redact_command(item.get("command") or event.get("command"))
            exit_code = item.get("exit_code", event.get("exit_code"))
            try:
                exit_code = int(exit_code) if exit_code is not None else None
            except (TypeError, ValueError):
                exit_code = None
            cwd = str(item.get("cwd") or event.get("cwd") or "")[:300]
            raw_output = str(item.get("aggregated_output") or item.get("output") or event.get("output") or "")
            artifact_refs: list[str] = []
            if artifact_store is not None and raw_output.strip():
                origin_kind = (
                    "target_observation"
                    if CodexHarnessRuntime._is_network_shell_command(command)
                    else "model_output"
                )
                try:
                    shell_artifact = artifact_store.write_text(
                        session,
                        project_id=worker.project_id,
                        source_attempt_id=attempt.id if attempt is not None else None,
                        content=raw_output,
                        summary=f"codex.shell {command[:120]} exit={exit_code}",
                        artifact_type="terminal",
                        origin_kind=origin_kind,
                        evidence_context=FlagValidator.request_evidence_context(str(item.get("command") or event.get("command") or "")),
                    )
                    artifact_refs = [shell_artifact.id]
                except Exception:
                    # Streaming event accounting must not kill the Codex worker
                    # because an output could not be persisted.
                    artifact_refs = []
            trace = ToolTrace(
                project_id=worker.project_id,
                worker_id=worker.id,
                intent_id=worker.intent_id,
                attempt_id=attempt.id,
                tool_name="codex.shell",
                request_json={"command": command, "cwd": cwd},
                command=command,
                cwd=cwd or None,
                exit_code=exit_code,
                summary=raw_output[-1000:] or None,
                artifact_refs=artifact_refs,
            )
            session.add(trace)
            session.flush()
            if transport_failed(trace) and target_transport_failed(session, worker.project_id):
                finalization_reason = "target_unreachable"
            if artifact_refs and exit_code == 0 and not is_sync_request(trace) and not transport_failed(trace):
                observed = session.get(Artifact, artifact_refs[0])
                validator = FlagValidator()
                if observed and observed.origin_kind == "target_observation" and validator.is_current_evidence(session, observed):
                    matching = session.exec(select(Artifact).where(
                        Artifact.project_id == worker.project_id, Artifact.sha256 == observed.sha256,
                        Artifact.id != observed.id, Artifact.origin_kind == "target_observation",
                    )).all()
                    if not any(validator.is_current_evidence(session, previous) for previous in matching):
                        session.add(WorkerEvent(
                            project_id=worker.project_id, worker_id=worker.id, intent_id=worker.intent_id,
                            attempt_id=attempt.id, event_type="evidence.progress", payload_json={"artifact_id": observed.id},
                        ))
            action_count = session.exec(
                select(ToolTrace).where(ToolTrace.attempt_id == attempt.id, ToolTrace.tool_name == "codex.shell")
            ).all()
            session.add(
                WorkerEvent(
                    project_id=worker.project_id,
                    worker_id=worker.id,
                    intent_id=worker.intent_id,
                    attempt_id=attempt.id,
                    event_type="agent.action.completed",
                    payload_json={
                        "action_index": len(action_count),
                        "tool_trace_id": trace.id,
                        "tool_name": "codex.shell",
                        "command": command,
                        "exit_code": exit_code,
                        "duration_ms": item.get("duration_ms") or event.get("duration_ms"),
                        "route_fingerprint": hashlib.sha256(json.dumps(trace.request_json, sort_keys=True).encode()).hexdigest()[:16],
                    },
                )
            )
            max_actions = int((worker.budgets or {}).get("max_agent_actions", 0) or 0)
            if max_actions and len(action_count) >= max_actions:
                already_exhausted = session.exec(
                    select(WorkerEvent).where(
                        WorkerEvent.attempt_id == attempt.id,
                        WorkerEvent.event_type == "attempt.action_budget_exhausted",
                    )
                ).first()
                if already_exhausted is None:
                    session.add(
                        WorkerEvent(
                            project_id=worker.project_id,
                            worker_id=worker.id,
                            intent_id=worker.intent_id,
                            attempt_id=attempt.id,
                            event_type="attempt.action_budget_exhausted",
                            payload_json={"max_agent_actions": max_actions, "observed": len(action_count)},
                        )
                    )
                finalization_reason = finalization_reason or "action_budget_exhausted"
            if exit_code not in (None, 0):
                fingerprint = route_fingerprint(trace.request_json)
                failed_repeats = sum(
                    1
                    for candidate in session.exec(
                        select(ToolTrace).where(
                            ToolTrace.project_id == worker.project_id,
                            ToolTrace.tool_name == "codex.shell",
                        )
                    ).all()
                    if candidate.exit_code not in (None, 0)
                    and route_fingerprint(candidate.request_json) == fingerprint
                )
                max_repeats = int((worker.budgets or {}).get("max_route_repeats", 0) or 0)
                if max_repeats and failed_repeats >= max_repeats:
                    finalization_reason = finalization_reason or "route_repeat_exhausted"

            max_no_progress = int((worker.budgets or {}).get("max_no_progress_actions", 0) or 0)
            if max_no_progress:
                latest_progress = session.exec(
                    select(WorkerEvent)
                    .where(
                        WorkerEvent.attempt_id == attempt.id,
                        WorkerEvent.event_type == "evidence.progress",
                    )
                    .order_by(WorkerEvent.created_at.desc())
                ).first()
                actions_without_progress = sum(
                    1
                    for candidate in action_count
                    if not is_sync_request(candidate) and (latest_progress is None or _utc_datetime(candidate.created_at) > _utc_datetime(latest_progress.created_at))
                )
                if actions_without_progress >= max_no_progress:
                    finalization_reason = finalization_reason or "no_progress_exhausted"
        session.add(
            WorkerEvent(
                project_id=worker.project_id,
                worker_id=worker.id,
                intent_id=worker.intent_id,
                attempt_id=attempt.id,
                event_type="codex.session_started" if event_type == "thread.started" else "codex.progress",
                payload_json={
                    "type": event_type,
                    "thread_id": attempt.codex_thread_id,
                    "turn_id": attempt.codex_turn_id,
                    "item_type": item_type,
                    "item_status": str(item.get("status") or event.get("status") or "")[:100],
                },
            )
        )
        try:
            session.commit()
        except Exception:
            # Streaming Codex progress is non-authoritative. A busy SQLite
            # database must not abort the actual solver command.
            session.rollback()
        if item_type == "command_execution" or event_type == "thread.started":
            try:
                CodexHarnessRuntime._sync_blackboard(session, worker=worker, attempt=attempt)
            except Exception:
                session.rollback()
        if finalization_reason:
            CodexHarnessRuntime._begin_finalization(
                session,
                worker=worker,
                attempt=attempt,
                reason=finalization_reason,
            )
        return finalization_reason

    @staticmethod
    def _is_network_shell_command(command: str) -> bool:
        return re.search(
            r"\b(?:curl|wget|nmap|ffuf|gobuster|dirb|nikto|whatweb|wafw00f|sqlmap|nc|netcat|ncat|socat)\b",
            command,
            re.IGNORECASE,
        ) is not None

    @staticmethod
    def _sync_blackboard(session: Session, *, worker: Worker, attempt: Attempt) -> None:
        from aurora.services.worker_control import WorkerControlService

        workspace = get_settings().codex_workspace_dir / worker.project_id / worker.id
        runtime_dir = workspace / "runtime"
        if not runtime_dir.is_dir():
            return
        snapshot_path = runtime_dir / "blackboard.json"
        version = session.exec(select(ProjectCoordinationState.graph_version).where(ProjectCoordinationState.project_id == worker.project_id)).first()
        try:
            previous = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and previous.get("version") == version:
                return
        except (OSError, ValueError):
            pass
        snapshot = WorkerControlService().query(session, worker=worker, attempt=attempt)
        snapshot["synced_at"] = now_utc().isoformat()
        temporary = snapshot_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        temporary.replace(snapshot_path)

    @staticmethod
    def _begin_finalization(session: Session, *, worker: Worker, attempt: Attempt, reason: str) -> None:
        session.refresh(attempt)
        if attempt.status == "FINALIZING":
            return
        if attempt.status != "RUNNING":
            return
        attempt.status = "FINALIZING"
        attempt.finalization_reason = reason[:100]
        attempt.last_event_at = now_utc()
        traces = session.exec(
            select(ToolTrace)
            .where(ToolTrace.attempt_id == attempt.id, ToolTrace.tool_name == "codex.shell")
            .order_by(ToolTrace.created_at.desc())
            .limit(10)
        ).all()
        failed_routes = [
            route_fingerprint(trace.request_json)
            for trace in traces
            if trace.exit_code not in (None, 0)
        ][:5]
        grace = int((worker.budgets or {}).get("finalize_grace_seconds", 60) or 60)
        event_type = "attempt.soft_deadline" if reason == "soft_timeout" else "attempt.budget_enforced"
        session.add(attempt)
        session.add(
            WorkerEvent(
                project_id=worker.project_id,
                worker_id=worker.id,
                intent_id=worker.intent_id,
                attempt_id=attempt.id,
                event_type=event_type,
                payload_json={"reason": reason, "action": "interrupt_and_finalize", "grace_seconds": grace},
            )
        )
        session.add(
            WorkerEvent(
                project_id=worker.project_id,
                worker_id=worker.id,
                intent_id=worker.intent_id,
                attempt_id=attempt.id,
                event_type="checkpoint.saved",
                payload_json={
                    "summary": f"Runtime requested finalization: {reason}",
                    "completed_steps": [f"Recorded {len(traces)} recent Codex shell action(s)."],
                    "failed_routes": failed_routes,
                    "next_step": "Resume from the latest evidence without repeating failed routes.",
                    "artifact_refs": list(attempt.artifact_refs),
                    "source": "runtime_enforced",
                    "blackboard_version": attempt.blackboard_version + 1,
                },
            )
        )
        attempt.blackboard_version += 1
        session.add(
            WorkerEvent(
                project_id=worker.project_id,
                worker_id=worker.id,
                intent_id=worker.intent_id,
                attempt_id=attempt.id,
                event_type="attempt.finalization_started",
                payload_json={"reason": reason, "mode": "runtime_enforced", "grace_seconds": grace},
            )
        )
        session.commit()

    @staticmethod
    def _redact_command(value: object) -> str:
        command = str(value or "").strip()
        command = re.sub(r"(?i)(authorization|cookie|token|password|api[_-]?key)\s*[:=]\s*[^\s]+", r"\1=<redacted>", command)
        return command[:2000]

    def _write_prompt(self, session: Session, worker: Worker, snapshot: ContextSnapshot, *, preserve_inputs: bool = False) -> Path:
        workspace = self.settings.codex_workspace_dir / snapshot.project_id / worker.id
        workspace.mkdir(parents=True, exist_ok=True)
        prompt_file = workspace / "aurora-intent.md"
        schema_file = workspace / "aurora-output-schema.json"
        prompt = PromptRenderer().render_codex_task(worker=worker, snapshot=snapshot)
        prompt_file.write_text(prompt, encoding="utf-8")
        schema_file.write_text(json.dumps(self._json_schema(), ensure_ascii=False, indent=2), encoding="utf-8")
        (workspace / "work").mkdir(exist_ok=True)
        (workspace / "subagent-context.json").write_text(
            json.dumps(
                {
                    "project_goal": snapshot.sections_json.get("project_goal"),
                    "current_intent": snapshot.sections_json.get("current_intent"),
                    "facts": snapshot.sections_json.get("facts", []),
                    "artifact_summaries": snapshot.sections_json.get("artifact_summaries", []),
                    "authorization_scope": snapshot.sections_json.get("authorization_scope"),
                    "visible_tools": snapshot.visible_tools_json,
                    "output_schema": snapshot.output_schema_json,
                    "subagents_enabled": any(tool.get("name") == "subagent.spawn" for tool in snapshot.visible_tools_json),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._materialize_project_inputs(session, snapshot, workspace, preserve_existing=preserve_inputs)
        runtime_dir = workspace / "runtime"
        runtime_dir.mkdir(exist_ok=True)
        event_log = runtime_dir / "mcp-events.jsonl"
        if event_log.exists():
            event_log.unlink()
        for name in ("codex-via-cc-switch.sh", "aurora-subagent.py"):
            source = Path.cwd() / "scripts" / name
            if source.exists():
                target = runtime_dir / name
                shutil.copy2(source, target)
                target.chmod(target.stat().st_mode | 0o100)
        config_source = Path.cwd() / "container" / "kali-codex" / "codex-config.toml"
        if config_source.is_file():
            shutil.copy2(config_source, runtime_dir / "codex-config.toml")
        rules_source = Path.cwd() / ".codex" / "rules"
        if rules_source.is_dir():
            shutil.copytree(rules_source, workspace / ".codex" / "rules", dirs_exist_ok=True)
        return prompt_file

    def _try_conclude_fallback(
        self,
        session: Session,
        *,
        worker: Worker,
        attempt: Attempt | None,
        snapshot: ContextSnapshot,
        workspace: Path,
        model: str,
        runner: CommandRunner,
        primary_completed: CommandResult,
        primary_diagnostic: dict[str, Any],
    ) -> ConcludeFallbackOutcome:
        seconds = self.settings.codex_conclude_fallback_seconds
        remaining = remaining_seconds(execution_deadline(session, worker.project_id, worker.id))
        if remaining is not None:
            seconds = min(seconds, int(remaining))
        diagnostic: dict[str, Any] = {
            "attempted": False,
            "recovered": False,
            "timeout_seconds": seconds,
        }
        if primary_diagnostic.get("source") in VALID_STRUCTURED_OUTPUT_SOURCES:
            diagnostic["skip_reason"] = "primary_output_valid"
            return ConcludeFallbackOutcome(False, False, diagnostic)
        if primary_completed.finalization_reason == "target_unreachable":
            diagnostic["skip_reason"] = "target_unreachable"
            return ConcludeFallbackOutcome(False, False, diagnostic)
        if seconds <= 0:
            diagnostic["skip_reason"] = "deadline_exhausted" if remaining is not None and remaining < 1 else "disabled"
            return ConcludeFallbackOutcome(False, False, diagnostic)
        if attempt is None:
            diagnostic["skip_reason"] = "attempt_missing"
            return ConcludeFallbackOutcome(False, False, diagnostic)

        session.refresh(attempt)
        session.refresh(worker)
        thread_id = attempt.codex_thread_id
        if not thread_id:
            diagnostic["skip_reason"] = "thread_id_missing"
            return ConcludeFallbackOutcome(False, False, diagnostic)
        failure_kind = self._provider_failure_kind(
            f"{primary_completed.stdout}\n{primary_completed.stderr}"
        ) or primary_completed.failure_kind
        if failure_kind in CONCLUDE_FALLBACK_FATAL_FAILURES:
            diagnostic.update({"skip_reason": "non_recoverable_failure", "primary_failure_kind": failure_kind})
            return ConcludeFallbackOutcome(False, False, diagnostic)

        project = session.get(Project, worker.project_id)
        intent = session.get(Intent, worker.intent_id)
        if project is None or project.status not in {"ACTIVE", "WORKING"}:
            diagnostic["skip_reason"] = "project_inactive"
            return ConcludeFallbackOutcome(False, False, diagnostic)
        if (
            worker.status != "RUNNING"
            or intent is None
            or intent.status != "RUNNING"
            or intent.lease_owner != worker.id
            or intent.lease_generation != worker.lease_generation
        ):
            diagnostic["skip_reason"] = "lease_inactive"
            return ConcludeFallbackOutcome(False, False, diagnostic)
        if attempt.status not in {"RUNNING", "FINALIZING"}:
            diagnostic["skip_reason"] = "attempt_inactive"
            return ConcludeFallbackOutcome(False, False, diagnostic)

        prompt_file = self._write_conclude_prompt(snapshot, workspace)
        output_schema_file = workspace / "aurora-conclude-output-schema.json"
        conclude_schema = self._conclude_json_schema()
        output_schema_file.write_text(
            json.dumps(conclude_schema, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        output_file = workspace / "aurora-conclude-last-message.json"
        if output_file.exists():
            output_file.unlink()
        command = self._render_command(
            prompt_file,
            model=model,
            output_schema_filename=output_schema_file.name,
            last_message_filename=output_file.name,
        )
        command = f"AURORA_CODEX_RESUME_THREAD_ID={shlex.quote(thread_id)} {command}"
        diagnostic.update({"attempted": True, "primary_failure_kind": failure_kind})
        session.add(
            WorkerEvent(
                project_id=worker.project_id,
                worker_id=worker.id,
                intent_id=worker.intent_id,
                attempt_id=attempt.id,
                event_type="attempt.conclude_fallback_started",
                payload_json={"timeout_seconds": seconds, "primary_failure_kind": failure_kind},
            )
        )
        session.commit()

        started = time.monotonic()
        try:
            completed = self._run_command(
                command,
                workspace,
                timeout_seconds=seconds,
                finalize_grace_seconds=min(5, max(1, seconds // 5)),
                deadline_at=execution_deadline(session, worker.project_id, worker.id),
                runner=runner,
                on_output=lambda stream, line: self._record_codex_event(
                    session,
                    worker=worker,
                    attempt=attempt,
                    stream=stream,
                    line=line,
                    artifact_store=self.artifact_store,
                ),
            )
        except Exception as exc:
            diagnostic.update({"error": str(exc)[:500], "duration_ms": round((time.monotonic() - started) * 1000)})
            session.add(
                WorkerEvent(
                    project_id=worker.project_id,
                    worker_id=worker.id,
                    intent_id=worker.intent_id,
                    attempt_id=attempt.id,
                    event_type="attempt.conclude_fallback_failed",
                    payload_json=diagnostic,
                )
            )
            session.commit()
            return ConcludeFallbackOutcome(True, False, diagnostic)

        diagnostic["duration_ms"] = round((time.monotonic() - started) * 1000)
        try:
            transcript = self._bounded_transcript(self._transcript(command, completed))
            artifact = self.artifact_store.write_text(
                session,
                project_id=snapshot.project_id,
                source_attempt_id=attempt.id,
                content=transcript,
                summary=f"Codex conclude fallback transcript {completed.backend} exit={completed.exit_code}",
                artifact_type="codex-conclude-transcript",
                origin_kind="model_output",
            )
            structured, fallback_diagnostic = self._parse_or_synthesize(
                output_file=output_file,
                stdout=completed.stdout,
                stderr=completed.stderr,
                exit_code=completed.exit_code,
                failure_kind=completed.failure_kind,
                artifact_id=artifact.id,
                snapshot=snapshot,
                session=session,
                worker=worker,
                attempt=attempt,
                allow_stream_fallback=False,
                output_schema=conclude_schema,
            )
        except Exception as exc:
            session.rollback()
            diagnostic.update({
                "error": f"failed to persist or parse conclude output: {str(exc)[:500]}",
                "exit_code": completed.exit_code,
                "failure_kind": completed.failure_kind,
            })
            session.add(
                WorkerEvent(
                    project_id=worker.project_id,
                    worker_id=worker.id,
                    intent_id=worker.intent_id,
                    attempt_id=attempt.id,
                    event_type="attempt.conclude_fallback_failed",
                    payload_json=diagnostic,
                )
            )
            session.commit()
            return ConcludeFallbackOutcome(
                attempted=True,
                recovered=False,
                diagnostic=diagnostic,
                command_result=completed,
            )
        recovered = fallback_diagnostic.get("source") in VALID_STRUCTURED_OUTPUT_SOURCES
        diagnostic.update({
            "recovered": recovered,
            "output": fallback_diagnostic,
            "exit_code": completed.exit_code,
            "failure_kind": completed.failure_kind,
        })
        if recovered:
            dropped_requests = len(structured.get("tool_requests") or [])
            structured["tool_requests"] = []
            if dropped_requests:
                diagnostic["dropped_tool_requests"] = dropped_requests
            event_type = "attempt.conclude_fallback_completed"
        else:
            event_type = "attempt.conclude_fallback_failed"
        session.add(
            WorkerEvent(
                project_id=worker.project_id,
                worker_id=worker.id,
                intent_id=worker.intent_id,
                attempt_id=attempt.id,
                event_type=event_type,
                payload_json={**diagnostic, "artifact_id": artifact.id},
            )
        )
        session.commit()
        return ConcludeFallbackOutcome(
            attempted=True,
            recovered=recovered,
            diagnostic=diagnostic,
            structured_output=structured if recovered else None,
            command_result=completed,
            artifact=artifact,
        )

    @staticmethod
    def _write_conclude_prompt(snapshot: ContextSnapshot, workspace: Path) -> Path:
        objective = str(snapshot.sections_json.get("current_intent", {}).get("objective") or "current intent")
        prompt_file = workspace / "aurora-conclude.md"
        prompt_file.write_text(
            "The previous solver turn ended without a schema-valid final response. Stop investigating now. "
            "Use only evidence and observations already present in this same Codex thread.\n\n"
            "Rules:\n"
            "- Do not run commands, call tools or MCP servers, browse, inspect new files, wait, or start subagents.\n"
            "- Do not claim facts or flags that were not already confirmed.\n"
            "- Return one raw JSON object matching the supplied output schema and nothing else.\n"
            "- If the intent is unfinished, use status=partial and provide one concrete suggested_intent with "
            "an expected_observation, or a blocker with a concrete next_step.\n"
            "- Reference only Artifact IDs already known in the thread.\n\n"
            f"Current intent: {objective}\n",
            encoding="utf-8",
        )
        return prompt_file

    def _import_mcp_events(
        self,
        session: Session,
        *,
        worker: Worker,
        snapshot: ContextSnapshot,
        workspace: Path,
    ) -> dict[str, Any]:
        event_log = workspace / "runtime" / "mcp-events.jsonl"
        if not event_log.is_file():
            return {"calls": 0, "invalid_lines": 0}
        raw = event_log.read_text(encoding="utf-8", errors="replace")
        artifact = self.artifact_store.write_text(
            session,
            project_id=snapshot.project_id,
            content=raw,
            summary="Local stdio MCP call log",
            artifact_type="mcp-tool-log",
            origin_kind="tool_log",
        )
        calls = 0
        invalid_lines = 0
        servers_used: set[str] = set()
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines += 1
                continue
            if not isinstance(event, dict) or not event.get("server") or not event.get("tool"):
                invalid_lines += 1
                continue
            servers_used.add(str(event["server"]))
            success = bool(event.get("success"))
            request = event.get("request") if isinstance(event.get("request"), dict) else {}
            request = {**request, "duration_ms": event.get("duration_ms")}
            trace = ToolTrace(
                project_id=snapshot.project_id,
                worker_id=worker.id,
                intent_id=worker.intent_id,
                tool_name=f"mcp.{event['server']}.{event['tool']}",
                request_json=request,
                policy_decision="allow_local_stdio",
                exit_code=0 if success else 1,
                summary=str(event.get("summary") or ("MCP call completed" if success else "MCP call failed"))[:500],
                artifact_refs=[artifact.id],
            )
            session.add(trace)
            calls += 1
        if servers_used:
            session.add(
                WorkerEvent(
                    project_id=snapshot.project_id,
                    worker_id=worker.id,
                    intent_id=worker.intent_id,
                    event_type="mcp.server.observed",
                    payload_json={"servers": sorted(servers_used), "calls": calls},
                )
            )
        session.commit()
        return {"calls": calls, "invalid_lines": invalid_lines, "artifact_id": artifact.id, "servers_used": sorted(servers_used)}

    @staticmethod
    def _materialize_project_inputs(session: Session, snapshot: ContextSnapshot, workspace: Path, *, preserve_existing: bool = False) -> None:
        """Expose only evidence owned by the active project to the container."""
        input_dir = workspace / "inputs"
        input_dir.mkdir(exist_ok=True)
        manifest: list[dict[str, Any]] = []
        settings = get_settings()
        sections = snapshot.sections_json or {}
        required_refs = {
            str(artifact["id"]) for artifact in sections.get("handoff_artifacts", [])
            if isinstance(artifact, dict) and artifact.get("id")
        }
        memory_ref = (sections.get("context_memory") or {}).get("artifact_id")
        if isinstance(memory_ref, str):
            required_refs.add(memory_ref)
        intent = session.get(Intent, snapshot.intent_id)
        if intent and intent.project_id == snapshot.project_id:
            facts = session.exec(select(Fact).where(Fact.project_id == snapshot.project_id, Fact.id.in_(intent.dependency_fact_ids))).all()
            required_refs.update(ref for fact in facts for ref in fact.evidence_refs)
        base = select(Artifact).where(
            Artifact.project_id == snapshot.project_id,
            Artifact.type.not_in(["codex-transcript", "codex-conclude-transcript", "subagent-transcript", "resume-manifest", "resume-work-file", "resume-codex-state", "mcp-events"]),
        )
        mandatory = session.exec(base.where((Artifact.origin_kind == "challenge_input") | Artifact.id.in_(required_refs)).order_by(Artifact.created_at.desc())).all()
        required_ids = {artifact.id for artifact in mandatory}
        recent = session.exec(base.where(Artifact.id.not_in(required_ids), Artifact.origin_kind != "runtime_state").order_by(Artifact.created_at.desc()).limit(settings.worker_input_max_files)).all()
        artifacts = [*mandatory, *recent]
        optional_bytes = 0
        for artifact in artifacts:
            source = Path(artifact.path)
            if not source.is_file():
                continue
            if artifact.id not in required_ids and optional_bytes + source.stat().st_size > settings.worker_input_max_bytes:
                continue
            current = FlagValidator().is_current_evidence(session, artifact)
            if artifact.id not in required_ids:
                if not current:
                    continue
                optional_bytes += source.stat().st_size
            if not current and CodexHarnessRuntime._sha256_file(source) != artifact.sha256:
                continue
            target = input_dir / f"{artifact.id}_{ArtifactStore.original_name(artifact)}"
            shutil.copy2(source, target)
            manifest.append({
                "artifact_id": artifact.id, "path": f"inputs/{target.name}", "sha256": artifact.sha256,
                "environment_id": (artifact.evidence_context or {}).get("environment_id"),
                "current_evidence": current,
            })
        manifest_path = input_dir / "manifest.json"
        if preserve_existing and manifest_path.is_file():
            # These entries were verified and restored before prompt creation.
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            by_path = {}
            for entry in existing:
                artifact = session.get(Artifact, entry.get("artifact_id"))
                path = workspace / entry["path"]
                if (artifact and artifact.project_id == snapshot.project_id
                        and path.resolve().is_relative_to(input_dir.resolve())
                        and path.is_file() and CodexHarnessRuntime._sha256_file(path) == artifact.sha256 == entry.get("sha256")):
                    by_path[entry["path"]] = entry
            by_path.update({entry["path"]: entry for entry in manifest})
            manifest = list(by_path.values())
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def _persist_resume_manifest(self, session: Session, *, attempt: Attempt, workspace: Path) -> Artifact:
        input_manifest = workspace / "inputs" / "manifest.json"
        inputs_complete = True
        try:
            inputs = json.loads(input_manifest.read_text(encoding="utf-8"))
            if not isinstance(inputs, list):
                inputs = []
                inputs_complete = False
        except (OSError, json.JSONDecodeError):
            inputs = []
            inputs_complete = False
        for item in inputs:
            if not isinstance(item, dict):
                inputs_complete = False
                continue
            relative = Path(str(item.get("path") or ""))
            target = workspace / relative
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not target.is_file()
                or self._sha256_file(target) != item.get("sha256")
            ):
                inputs_complete = False

        work_files: list[dict[str, Any]] = []
        work_dir = workspace / "work"
        total_bytes = 0
        candidates = sorted((path for path in work_dir.rglob("*") if path.is_file() or path.is_symlink()), key=lambda path: (
            0 if re.search(r"(?:solve|exploit|verify|decode|derive|poc)", path.stem, re.I) else
            1 if path.suffix in {".py", ".sh", ".sage", ".c", ".cpp", ".rs"} else
            2 if path.parent == work_dir else 3, str(path.relative_to(work_dir)),
        )) if work_dir.is_dir() else []
        work_issues = []
        omitted_count = 0
        for path in candidates:
            relative = path.relative_to(work_dir)
            reason = None
            try:
                if path.is_symlink():
                    reason = "symlink"
                elif len(work_files) >= self.settings.resume_max_files:
                    reason = "file_limit"
                elif total_bytes + path.stat().st_size > self.settings.resume_max_bytes:
                    reason = "byte_limit"
                else:
                    artifact = self.artifact_store.write_file(
                        session, project_id=attempt.project_id, source_attempt_id=attempt.id,
                        source=path, summary=f"Resumable worker file: {relative}",
                        artifact_type="resume-work-file", sensitivity="restricted",
                        origin_kind="runtime_state", deduplicate=True,
                    )
            except OSError:
                reason = "read_failed"
            if reason:
                omitted_count += 1
                if len(work_issues) < 50:
                    work_issues.append({"path": str(relative), "reason": reason})
                continue
            work_files.append({
                "path": str(relative),
                "artifact_id": artifact.id,
                "sha256": artifact.sha256,
                "size": artifact.size,
            })
            total_bytes += artifact.size

        work_state_complete = omitted_count == 0

        codex_home = workspace / "runtime" / "codex-home"
        codex_state, codex_complete = self._codex_state_manifest(codex_home)
        state_bytes = 0
        for entry in codex_state:
            state_bytes += entry["size"]
            if state_bytes > self.settings.resume_max_bytes:
                codex_complete = False
                break
            try:
                state_artifact = self.artifact_store.write_file(
                    session, project_id=attempt.project_id, source_attempt_id=attempt.id,
                    source=codex_home / entry["path"], summary=f"Resumable session state: {entry['path']}",
                    artifact_type="resume-codex-state", sensitivity="restricted", origin_kind="runtime_state", deduplicate=True,
                )
                if state_artifact.sha256 != entry["sha256"]:
                    codex_complete = False
                entry["artifact_id"] = state_artifact.id
            except OSError:
                codex_complete = False
        payload = {
            "version": 2,
            "project_id": attempt.project_id,
            "attempt_id": attempt.id,
            "parent_attempt_id": attempt.parent_attempt_id,
            "codex_thread_id": attempt.codex_thread_id,
            "environment_id": attempt.environment_id,
            "inputs": inputs if isinstance(inputs, list) else [],
            "inputs_complete": inputs_complete,
            "work_files": work_files,
            "work_files_total_bytes": total_bytes,
            "work_state_complete": work_state_complete,
            "partial_work_state_available": bool(work_files) and not work_state_complete,
            "work_files_omitted": omitted_count,
            "work_state_issues": work_issues,
            "codex_state": codex_state,
            "codex_state_complete": codex_complete,
        }
        manifest = self.artifact_store.write_text(
            session,
            project_id=attempt.project_id,
            source_attempt_id=attempt.id,
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            summary="Validated attempt resume manifest",
            artifact_type="resume-manifest",
            sensitivity="restricted",
            origin_kind="runtime_state",
        )
        attempt.resume_manifest_artifact_id = manifest.id
        session.add(attempt)
        session.add(
            WorkerEvent(
                project_id=attempt.project_id,
                worker_id=attempt.worker_id,
                intent_id=attempt.intent_id,
                attempt_id=attempt.id,
                event_type="attempt.resume_manifest_saved",
                payload_json={
                    "manifest_artifact_id": manifest.id,
                    "input_count": len(payload["inputs"]),
                    "work_file_count": len(work_files),
                    "inputs_complete": inputs_complete,
                    "work_state_complete": work_state_complete,
                    "codex_state_complete": codex_complete,
                },
            )
        )
        session.commit()
        return manifest

    def _codex_state_manifest(self, home: Path) -> tuple[list[dict[str, Any]], bool]:
        if not home.is_dir():
            return [], False
        entries: list[dict[str, Any]] = []
        walk_errors: list[OSError] = []
        complete = True
        for root, directories, filenames in os.walk(home, onerror=walk_errors.append):
            directories.sort()
            root_path = Path(root)
            directories[:] = [
                name
                for name in directories
                if self._is_resumable_codex_state((root_path / name).relative_to(home))
            ]
            filenames.sort()
            for filename in filenames:
                path = Path(root) / filename
                relative = path.relative_to(home)
                if not self._is_resumable_codex_state(relative):
                    continue
                if len(entries) >= self.settings.resume_max_files:
                    complete = False
                    break
                try:
                    if path.is_symlink():
                        complete = False
                        continue
                    entries.append({
                        "path": str(relative),
                        "sha256": self._sha256_file(path),
                        "size": path.stat().st_size,
                    })
                except OSError:
                    complete = False
        if walk_errors:
            complete = False
        return entries, complete and bool(entries)

    @staticmethod
    def _is_resumable_codex_state(relative: Path) -> bool:
        """Exclude immutable bundled assets and volatile locks from resume state."""
        return bool(relative.parts) and relative.parts[0] not in {"skills", ".tmp", "tmp", "thread-writer-locks"}

    @staticmethod
    def _has_model_metadata_warning(text: str) -> bool:
        lowered = text.lower()
        return bool(re.search(r"model metadata(?:\s+for\s+[^\n]+?)?\s+not found", lowered))

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _json_schema(self) -> dict[str, Any]:
        array = {"type": "array", "items": {"type": "object"}}
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string", "enum": ["success", "partial", "failed"]},
                "summary": {"type": "string"},
                "fact_candidates": array,
                "hypotheses": {"type": "array", "items": {}},
                "artifact_refs": {"type": "array", "items": {"type": "string"}},
                "failed_attempts": array,
                "suggested_intents": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["objective", "expected_observation", "capabilities", "priority", "risk_level"],
                        "properties": {
                            "objective": {"type": "string"},
                            "expected_observation": {"type": "string"},
                            "capabilities": {"type": "array", "items": {"type": "string"}},
                            "priority": {"type": "number"},
                            "risk_level": {"type": "string"},
                            "tool_request": {"type": "object", "additionalProperties": True},
                            "budget": {"type": "object", "additionalProperties": True},
                        },
                    },
                },
                "fork_recommendations": array,
                "subagent_reports": array,
                "candidate_flags": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["value", "artifact_ref"],
                        "properties": {
                            "value": {"type": "string"},
                            "artifact_ref": {"type": "string"},
                            "provenance_kind": {"type": "string", "enum": ["observed", "derived_replay"]},
                        },
                        "additionalProperties": False,
                    },
                },
                "decision_summary": {
                    "type": "object",
                    "required": ["selected_intent", "reason_summary", "next_tool_plan"],
                    "properties": {
                        "selected_intent": {"type": "string"},
                        "reason_summary": {"type": "string"},
                        "next_tool_plan": {"type": "array"},
                    },
                    "additionalProperties": False,
                },
                "blockers": array,
                "tool_requests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["tool_name", "request"],
                        "properties": {
                            "tool_name": {"type": "string"},
                            "request": {"type": "object", "additionalProperties": True},
                            "activity_label": {"type": "string"},
                        },
                    },
                },
            },
        }
        schema["required"] = list(schema["properties"])
        return schema

    def _conclude_json_schema(self) -> dict[str, Any]:
        full_schema = self._json_schema()
        fields = (
            "status",
            "summary",
            "fact_candidates",
            "artifact_refs",
            "failed_attempts",
            "suggested_intents",
            "candidate_flags",
            "blockers",
            "decision_summary",
        )
        properties = full_schema["properties"]
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {field: properties[field] for field in fields},
            "required": list(fields),
        }

    def _render_command(
        self,
        prompt_file: Path,
        *,
        model: str | None = None,
        output_schema_filename: str = "aurora-output-schema.json",
        last_message_filename: str = "aurora-last-message.json",
    ) -> str:
        model = model or self.settings.llm_model
        workspace = Path.cwd().resolve()
        relative_prompt = Path(os.path.relpath(prompt_file.resolve(), workspace))
        # The container mounts only this Worker's directory at /workspace,
        # regardless of where the operator stores workspaces on the host.
        container_prompt = Path("/workspace") / prompt_file.name
        return self.settings.codex_command_template.format(
            prompt_file=prompt_file.name,
            prompt_filename=prompt_file.name,
            output_schema_file=output_schema_filename,
            output_schema_filename=output_schema_filename,
            last_message_file=last_message_filename,
            last_message_filename=last_message_filename,
            prompt_path=str(relative_prompt),
            host_prompt_file=str(prompt_file),
            container_prompt_file=str(container_prompt),
            llm_model=model,
            llm_model_shell=shlex.quote(model),
            llm_base_url=self._codex_base_url(),
            llm_base_url_shell=shlex.quote(self._codex_base_url()),
        )

    def _codex_base_url(self) -> str:
        base_url = self.settings.llm_base_url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        return base_url

    def _run_command(
        self,
        command: str,
        cwd: Path,
        *,
        timeout_seconds: object = None,
        soft_timeout_seconds: object = None,
        finalize_grace_seconds: object = None,
        deadline_at: datetime | None = None,
        runner: CommandRunner | None = None,
        on_output=None,
    ) -> CommandResult:
        configured = self.settings.codex_timeout_seconds if self.settings.codex_timeout_seconds > 0 else None
        budget_timeout = int(timeout_seconds) if timeout_seconds else None
        timeout = min(value for value in (configured, budget_timeout) if value is not None) if configured or budget_timeout else None
        remaining = remaining_seconds(deadline_at)
        if remaining is not None:
            if remaining < 1:
                return CommandResult(command, command, str(cwd), "", "Execution deadline exhausted", 124, "deadline", failure_kind="timeout")
            timeout = min(timeout, int(remaining)) if timeout is not None else int(remaining)
        soft_timeout = int(soft_timeout_seconds) if soft_timeout_seconds else None
        if timeout is not None and soft_timeout is not None:
            soft_timeout = min(soft_timeout, max(1, timeout - 1))
        grace = int(finalize_grace_seconds) if finalize_grace_seconds else 10
        if timeout is not None:
            grace = min(grace, max(1, timeout - (soft_timeout or timeout)))
        selected_runner = runner or self.command_runner
        if selected_runner is None:
            selected_runner = AutoCommandRunner(prefer_kali=True, allow_local_fallback=False)
        if on_output is not None and hasattr(selected_runner, "run_streaming"):
            return selected_runner.run_streaming(
                command=command,
                cwd=cwd,
                timeout=timeout,
                on_output=on_output,
                soft_timeout=soft_timeout,
                finalize_grace=grace,
            )
        return selected_runner.run(command=command, cwd=cwd, timeout=timeout)

    def _transcript(self, command: str, completed: CommandResult) -> str:
        executed_command = re.sub(
            r"(?i)(AURORA_WORKER_CONTROL_TOKEN=)[^\s]+",
            r"\1<redacted>",
            completed.executed_command,
        )
        return (
            f"runtime=codex\n"
            f"backend={completed.backend}\n"
            f"cwd={completed.cwd}\n"
            f"command={command}\n"
            f"executed_command={executed_command}\n"
            f"exit_code={completed.exit_code}\n\n"
            f"failure_kind={completed.failure_kind or ''}\n\n"
            f"[stdout]\n{completed.stdout}\n\n"
            f"[stderr]\n{completed.stderr}\n"
        )

    def _bounded_transcript(self, transcript: str) -> str:
        limit = max(1024, self.settings.codex_transcript_max_bytes)
        encoded = transcript.encode("utf-8", errors="replace")
        if len(encoded) <= limit:
            return transcript
        marker = f"\n\n[aurora transcript truncated: original_bytes={len(encoded)} kept_bytes={limit}]\n\n".encode()
        payload_limit = max(0, limit - len(marker))
        head_size = payload_limit // 2
        tail_size = payload_limit - head_size
        bounded = encoded[:head_size] + marker + (encoded[-tail_size:] if tail_size else b"")
        return bounded.decode("utf-8", errors="replace")

    def _parse_or_synthesize(
        self,
        *,
        output_file: Path,
        stdout: str,
        stderr: str,
        exit_code: int,
        failure_kind: str | None,
        artifact_id: str,
        snapshot: ContextSnapshot,
        session: Session | None = None,
        worker: Worker | None = None,
        attempt: Attempt | None = None,
        allow_stream_fallback: bool = True,
        output_schema: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        diagnostic: dict[str, Any] = {"source": None, "output_file": str(output_file), "output_file_exists": output_file.exists()}
        if output_file.exists():
            try:
                raw = output_file.read_text(encoding="utf-8")
                diagnostic.update({"source": "last_message_file", "bytes": len(raw.encode("utf-8"))})
                try:
                    parsed = json.loads(raw)
                    diagnostic["tolerant_json"] = False
                except json.JSONDecodeError:
                    # Some providers ignore the plain-JSON requirement and
                    # wrap a valid result in a Markdown fence. Recover the
                    # complete object before declaring the solve lost.
                    parsed = self._parse_json(raw)
                    diagnostic["tolerant_json"] = True
                parsed, repairs = self._repair_output(
                    parsed,
                    snapshot=snapshot,
                    output_schema=output_schema,
                )
                if repairs:
                    diagnostic["repaired"] = True
                    diagnostic["repairs"] = repairs
                self._validate_output(parsed, output_schema=output_schema)
                return self._normalize(parsed, snapshot, artifact_id, session=session, worker=worker, attempt=attempt), diagnostic
            except json.JSONDecodeError as exc:
                diagnostic.update({"source": "last_message_file", "error": f"invalid JSON at line {exc.lineno}, column {exc.colno}"})
                failure_kind = "output_invalid_json"
            except ValueError as exc:
                diagnostic.update({"source": "last_message_file", "error": str(exc)})
                failure_kind = "output_schema_invalid"
            except OSError as exc:
                diagnostic.update({"source": "last_message_file", "error": str(exc)})
        if allow_stream_fallback:
            try:
                parsed = self._parse_json(stdout)
                self._validate_output(parsed, output_schema=output_schema)
                diagnostic.update({"source": "stdout_fallback", "stdout_bytes": len(stdout.encode("utf-8", errors="replace"))})
                return self._normalize(parsed, snapshot, artifact_id, session=session, worker=worker, attempt=attempt), diagnostic
            except (json.JSONDecodeError, ValueError) as exc:
                diagnostic.setdefault("error", str(exc)[:300])
        # Some Codex/provider combinations emit the final assistant message to
        # stderr. The process can hit the outer timeout after emitting a result
        # but before writing --output-last-message. Recover only a schema-valid
        # full worker result; progress JSON must not become a completion result.
        if allow_stream_fallback:
            try:
                parsed = self._parse_json(stderr)
                self._validate_output(parsed, output_schema=output_schema)
                diagnostic.update({"source": "stderr_fallback", "stderr_bytes": len(stderr.encode("utf-8", errors="replace"))})
                return self._normalize(parsed, snapshot, artifact_id, session=session, worker=worker, attempt=attempt), diagnostic
            except (json.JSONDecodeError, ValueError) as exc:
                diagnostic.setdefault("stderr_error", str(exc)[:300])
        # Codex emits structured provider failures (including DeepSeek's
        # reasoning_content contract error) on stdout when --json is enabled.
        # Inspect both streams so the real provider failure is not flattened
        # into the generic output_missing fallback.
        failure_kind = failure_kind or self._provider_failure_kind(f"{stdout}\n{stderr}")
        failure_kind = failure_kind or ("command_timed_out" if exit_code == 124 else "resource_terminated" if exit_code == 137 else "output_missing" if not output_file.exists() else "output_invalid_json")
        return self._failure_output(failure_kind, stderr, artifact_id, snapshot), diagnostic

    @staticmethod
    def _provider_failure_kind(stderr: str) -> str | None:
        lowered = stderr.lower()
        if "aurora cc switch proxy is unavailable" in lowered or "could not resolve host: aurora-cc-switch" in lowered:
            return "provider_unavailable"
        if "maximum context length" in lowered or "context_length_exceeded" in lowered:
            return "context_length_exceeded"
        if "reasoning_content must be passed back" in lowered:
            return "provider_reasoning_error"
        if "model metadata not found" in lowered:
            return "provider_model_metadata_missing"
        if "reasoning_content" in lowered:
            return "provider_reasoning_error"
        if '"type":"invalid_request_error"' in lowered or "invalid_request_error" in lowered:
            return "provider_invalid_request"
        return None

    def _validate_output(
        self,
        parsed: Any,
        *,
        output_schema: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(parsed, dict):
            raise ValueError("final output must be a JSON object")
        allowed = set((output_schema or self._json_schema())["properties"])
        unknown = set(parsed) - allowed
        if unknown:
            raise ValueError(f"final output has unknown fields: {', '.join(sorted(unknown))}")
        # Upgrade pre-strict-contract final messages so an interrupted thread
        # can still be resumed after deployment. New Codex invocations receive
        # the strict output schema and therefore produce these fields directly.
        for key in allowed - {"status", "summary", "decision_summary"}:
            parsed.setdefault(key, [])
        if parsed.get("status") == "partial" and not parsed["suggested_intents"] and not parsed["blockers"]:
            parsed["blockers"] = [{
                "kind": "legacy_partial",
                "reason": "Recovered a partial result written before the strict continuation contract.",
                "next_step": "Resume from the latest evidence and run one discriminating experiment.",
            }]
        missing = allowed - set(parsed)
        if missing:
            raise ValueError(f"final output missing required fields: {', '.join(sorted(missing))}")
        if parsed["status"] not in {"success", "partial", "failed"}:
            raise ValueError("final output has an invalid status")
        if not isinstance(parsed["summary"], str) or not isinstance(parsed["decision_summary"], dict):
            raise ValueError("final output has invalid summary or decision_summary")
        for key in allowed - {"status", "summary", "decision_summary"}:
            if not isinstance(parsed[key], list):
                raise ValueError(f"final output field {key} must be an array")
        decision = parsed["decision_summary"]
        if set(decision) != {"selected_intent", "reason_summary", "next_tool_plan"}:
            raise ValueError("decision_summary must contain only selected_intent, reason_summary, and next_tool_plan")
        if not isinstance(decision["selected_intent"], str) or not isinstance(decision["reason_summary"], str) or not isinstance(decision["next_tool_plan"], list):
            raise ValueError("decision_summary fields have invalid types")
        if parsed["status"] == "partial":
            has_follow_up = any(
                isinstance(item, dict)
                and str(item.get("objective") or "").strip()
                and str(item.get("expected_observation") or "").strip()
                for item in parsed["suggested_intents"]
            )
            has_blocker = any(
                isinstance(item, dict) and str(item.get("next_step") or "").strip()
                for item in parsed["blockers"]
            )
            if not has_follow_up and not has_blocker:
                raise ValueError("partial output requires one follow-up intent with expected_observation or a blocker next_step")

    def _repair_output(
        self,
        parsed: Any,
        *,
        snapshot: ContextSnapshot,
        output_schema: dict[str, Any] | None = None,
    ) -> tuple[Any, list[str]]:
        """Repair structural-only mistakes in the designated final-message file.

        This deliberately never extracts or invents facts, flags, evidence, or
        tool results. Stream fallbacks still require a complete valid object so
        progress events cannot be mistaken for the final answer.
        """
        if not isinstance(parsed, dict):
            return parsed, []
        repairs: list[str] = []
        candidate = dict(parsed)
        if len(candidate) == 1:
            wrapped = next(iter(candidate.values()))
            if isinstance(wrapped, dict) and next(iter(candidate)) in {"result", "output", "final"}:
                candidate = dict(wrapped)
                repairs.append("unwrapped final object")

        schema = output_schema or self._json_schema()
        allowed = set(schema["properties"])
        unknown = sorted(set(candidate) - allowed)
        if unknown:
            candidate = {key: value for key, value in candidate.items() if key in allowed}
            repairs.append(f"removed unknown fields: {', '.join(unknown)}")

        raw_status = candidate.get("status")
        status = str(raw_status or "partial").strip().lower()
        if status not in {"success", "partial", "failed"}:
            status = "partial"
        if raw_status != status:
            candidate["status"] = status
            repairs.append("normalized status")

        summary = candidate.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            candidate["summary"] = (
                str(summary).strip()[:2000]
                if summary is not None and str(summary).strip()
                else "Worker returned a structurally incomplete result."
            )
            repairs.append("normalized summary")

        array_fields = allowed - {"status", "summary", "decision_summary"}
        for field in sorted(array_fields):
            value = candidate.get(field)
            if value is None:
                candidate[field] = []
                repairs.append(f"filled {field}")
            elif not isinstance(value, list):
                candidate[field] = [value] if isinstance(value, (dict, str)) else []
                repairs.append(f"normalized {field} array")

        intent = snapshot.sections_json.get("current_intent", {})
        decision = candidate.get("decision_summary")
        if not isinstance(decision, dict):
            decision = {}
            repairs.append("created decision_summary")
        normalized_decision = {
            "selected_intent": str(decision.get("selected_intent") or intent.get("objective") or "unknown objective")[:2000],
            "reason_summary": str(decision.get("reason_summary") or candidate["summary"])[:2000],
            "next_tool_plan": decision.get("next_tool_plan") if isinstance(decision.get("next_tool_plan"), list) else ([decision["next_tool_plan"]] if isinstance(decision.get("next_tool_plan"), str) else []),
        }
        if decision != normalized_decision:
            repairs.append("normalized decision_summary")
        candidate["decision_summary"] = normalized_decision

        if candidate["status"] == "partial":
            has_follow_up = any(
                isinstance(item, dict)
                and str(item.get("objective") or "").strip()
                and str(item.get("expected_observation") or "").strip()
                for item in candidate.get("suggested_intents", [])
            )
            has_blocker = any(
                isinstance(item, dict) and str(item.get("next_step") or "").strip()
                for item in candidate.get("blockers", [])
            )
            if not has_follow_up and not has_blocker and "blockers" in allowed:
                candidate["blockers"] = [{
                    "kind": "missing_evidence",
                    "reason": "The final response did not contain a valid continuation contract.",
                    "next_step": "Resume from the latest project evidence and run one discriminating experiment.",
                }]
                repairs.append("added safe partial continuation blocker")
        return candidate, repairs

    def _failure_output(self, failure_kind: str, stderr: str, artifact_id: str, snapshot: ContextSnapshot) -> dict[str, Any]:
        intent = snapshot.sections_json.get("current_intent", {})
        timed_out = failure_kind == "command_timed_out"
        suggested_intents = []
        next_tool_plan = []
        if timed_out:
            objective = (
                "Resume the timed-out investigation from current project evidence. "
                "Prioritize the last concrete lead, verify it, and return a partial or successful JSON result "
                "before the soft deadline instead of restarting broad analysis."
            )
            suggested_intents = [{
                "objective": objective,
                "capabilities": ["sandbox.exec", "blackboard.query"],
                "priority": 1.5,
                "risk_level": "low",
            }]
            next_tool_plan = [objective]
        return {
            "status": "failed",
            "summary": (
                f"Codex harness failed: {failure_kind}; the whole solver process exceeded its wall-clock budget, "
                "not an individual sandbox command. Transcript saved as Artifact."
                if timed_out
                else f"Codex harness failed: {failure_kind}; transcript saved as Artifact."
            ),
            "fact_candidates": [],
            "hypotheses": [],
            "artifact_refs": [artifact_id],
            "failed_attempts": [{"reason": failure_kind, "stderr": stderr[-1000:] if stderr else ""}],
            "suggested_intents": suggested_intents,
            "fork_recommendations": [],
            "candidate_flags": [],
            "decision_summary": {
                "selected_intent": intent.get("objective", "unknown objective"),
                "reason_summary": f"Codex harness did not return a valid final result ({failure_kind}).",
                "next_tool_plan": next_tool_plan,
            },
            "tool_requests": [],
        }

    def _parse_json(self, text: str) -> dict[str, Any]:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            candidates: list[tuple[dict[str, Any], int]] = []
            for match in re.finditer(r"\{", text):
                try:
                    value, end = decoder.raw_decode(text[match.start():])
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    candidates.append((value, end))
            if not candidates:
                raise
            # A JSON object contains more JSON objects.  Choosing the last
            # raw-decoded match therefore returned a nested fact or decision
            # object whenever Codex prefixed its final JSON with progress
            # logs.  Prefer a complete worker result, then the largest object
            # as a tolerant fallback for older output formats.
            complete_results = [
                value
                for value, _ in candidates
                if {"status", "summary", "decision_summary"}.issubset(value)
            ]
            if complete_results:
                return complete_results[-1]
            return max(candidates, key=lambda candidate: candidate[1])[0]

    @staticmethod
    def _safe_activity_label(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        label = re.sub(r"\s+", " ", value).strip()
        if not label or len(label) > 120:
            return None
        if re.search(r"(?:authorization|cookie|token|password|api[_ -]?key)\s*[:=]", label, re.IGNORECASE):
            return None
        return label

    def _normalize(
        self,
        structured: dict[str, Any],
        snapshot: ContextSnapshot,
        artifact_id: str,
        *,
        session: Session | None = None,
        worker: Worker | None = None,
        attempt: Attempt | None = None,
    ) -> dict[str, Any]:
        structured.setdefault("status", "partial")
        structured.setdefault("summary", "Codex harness returned structured output.")
        for key in [
            "fact_candidates",
            "hypotheses",
            "artifact_refs",
            "failed_attempts",
            "suggested_intents",
            "fork_recommendations",
            "subagent_reports",
            "candidate_flags",
            "blockers",
            "tool_requests",
        ]:
            if not isinstance(structured.get(key), list):
                structured[key] = []
        visible_tool_names = {
            tool.get("name")
            for tool in getattr(snapshot, "visible_tools_json", [])
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        }
        normalized_requests = []
        dropped_tool_names: list[str] = []
        ordered_tool_requests = [tool_request for tool_request in structured["tool_requests"] if isinstance(tool_request, dict)]
        ordered_tool_requests.sort(key=lambda request: {"flag.verify": 0, "flag.submit": 1}.get(request.get("tool_name"), 2))
        for tool_request in ordered_tool_requests[:3]:
            tool_name = tool_request.get("tool_name")
            if not isinstance(tool_name, str) or tool_name not in visible_tool_names:
                if isinstance(tool_name, str) and tool_name.strip():
                    dropped_tool_names.append(tool_name)
                continue
            payload = tool_request.get("request")
            if not isinstance(payload, dict):
                payload = tool_request.get("parameters")
            if not isinstance(payload, dict):
                payload = tool_request.get("arguments")
            if not isinstance(payload, dict):
                payload = tool_request.get("params")
            if not isinstance(payload, dict):
                payload = {}
            normalized = {"tool_name": tool_name, "request": payload}
            if "activity_label" in tool_request:
                normalized["activity_label"] = tool_request["activity_label"]
            normalized_requests.append(normalized)
        structured["tool_requests"] = normalized_requests
        if dropped_tool_names and session is not None:
            self._record_skipped_tool_requests(
                session,
                worker=worker,
                attempt=attempt,
                snapshot=snapshot,
                dropped_tool_names=dropped_tool_names,
            )
        structured["blockers"] = OpenAICompatibleRuntime._normalize_blockers(structured.get("blockers"))
        if structured.get("status") == "success" and not (
            structured.get("fact_candidates") or structured.get("candidate_flags") or structured.get("artifact_refs")
        ):
            structured["status"] = "partial"
            structured["blockers"].append({
                "kind": "missing_evidence",
                "reason": "success was downgraded because no evidence-backed result was returned",
                "next_step": "Run one discriminating experiment and save its evidence before claiming success",
            })
        if structured.get("status") == "partial" and not structured.get("suggested_intents") and not structured["blockers"]:
            structured["blockers"] = [{"kind": "missing_evidence", "reason": "本轮没有形成可执行的续跑路线", "next_step": "查询最新 checkpoint 并提出唯一下一步"}]
        structured.setdefault(
            "decision_summary",
            {
                "selected_intent": snapshot.sections_json.get("current_intent", {}).get("objective", "unknown objective"),
                "reason_summary": "Codex harness returned structured output.",
                "next_tool_plan": [request.get("tool_name") for request in structured.get("tool_requests", []) if isinstance(request, dict)],
            },
        )
        if artifact_id not in structured["artifact_refs"]:
            structured["artifact_refs"].append(artifact_id)
        return structured

    @staticmethod
    def _record_skipped_tool_requests(
        session: Session,
        *,
        worker: Worker | None,
        attempt: Attempt | None,
        snapshot: ContextSnapshot,
        dropped_tool_names: list[str],
    ) -> None:
        session.add(
            WorkerEvent(
                project_id=snapshot.project_id,
                worker_id=worker.id if worker else None,
                intent_id=worker.intent_id if worker else snapshot.intent_id,
                attempt_id=attempt.id if attempt else None,
                event_type="tool.skipped.non_privileged",
                payload_json={
                    "tool_names": sorted(set(dropped_tool_names)),
                    "reason": "tool_requests were dropped because they fall outside the runtime's visible tool contract",
                },
            )
        )
        session.commit()


def get_worker_runtime() -> WorkerRuntime:
    runtime = get_settings().worker_runtime.strip().lower()
    if runtime in {"codex", "codex_harness", "harness"}:
        return CodexHarnessRuntime()
    if runtime in {"openai_direct", "openai", "llm", "real"}:
        return OpenAICompatibleRuntime()
    raise RuntimeError(
        f"unsupported AURORA_WORKER_RUNTIME={runtime!r}; expected 'codex' or 'openai_direct'"
    )
