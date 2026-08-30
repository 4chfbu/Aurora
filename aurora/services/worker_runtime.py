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
from aurora.models import Artifact, Attempt, ContextSnapshot, LLMTrace, ToolTrace, Worker, WorkerEvent, now_utc
from aurora.services.llm_http import LLMRequestError, chat_completion
from aurora.services.artifact_store import ArtifactStore
from aurora.services.blackboard_repository import route_fingerprint
from aurora.services.command_runner import AutoCommandRunner, CommandResult, CommandRunner
from aurora.services.prompt_renderer import PromptRenderer
from aurora.services.tool_profiles import image_for_profile


@dataclass
class RuntimeOutput:
    status: str
    summary: str
    structured_output: dict[str, Any]
    llm_trace: LLMTrace


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
        response = self._call_llm(prompt, model=model)
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

    def _call_llm(self, messages: list[dict[str, str]], *, model: str | None = None) -> dict[str, Any]:
        try:
            body = chat_completion(
                settings=self.settings,
                model=model or self.model,
                messages=messages,
                timeout=self.settings.llm_timeout_seconds,
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
        prompt_file = self._write_prompt(session, worker, snapshot)
        model_role = str(worker.budgets.get("model_role", "solver"))
        model = self.settings.model_for_role(model_role)
        model_context_window, auto_compact_token_limit = self.settings.codex_metadata_for_role(model_role)
        command = self._render_command(prompt_file, model=model)
        started = time.monotonic()
        attempt = session.exec(
            select(Attempt).where(Attempt.worker_id == worker.id, Attempt.status == "RUNNING").order_by(Attempt.started_at.desc())
        ).first()
        control_token = secrets.token_urlsafe(32)
        resume_thread_id = self._prepare_attempt(
            session,
            worker=worker,
            attempt=attempt,
            control_token=control_token,
            workspace=prompt_file.parent,
        )
        tool_environment = snapshot.sections_json.get("tool_environment") or {}
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
                "AURORA_CODEX_RESUME_THREAD_ID": resume_thread_id or "",
            },
        )
        try:
            completed = self._run_command(
                command,
                prompt_file.parent,
                timeout_seconds=worker.budgets.get("hard_timeout_seconds"),
                soft_timeout_seconds=worker.budgets.get("soft_timeout_seconds"),
                finalize_grace_seconds=worker.budgets.get("finalize_grace_seconds"),
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
        elapsed_ms = round((time.monotonic() - started) * 1000)
        transcript = self._bounded_transcript(self._transcript(command, completed))
        artifact = self.artifact_store.write_text(
            session,
            project_id=snapshot.project_id,
            content=transcript,
            summary=f"Codex harness transcript {completed.backend} exit={completed.exit_code}",
            artifact_type="codex-transcript",
            origin_kind="model_output",
        )
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
        structured.setdefault("artifact_refs", [])
        if artifact.id not in structured["artifact_refs"]:
            structured["artifact_refs"].append(artifact.id)
        output_json = json.dumps(structured, ensure_ascii=False, sort_keys=True)
        prompt_text = prompt_file.read_text(encoding="utf-8")
        decision_summary = structured.get("decision_summary") or {}
        trace = LLMTrace(
            project_id=snapshot.project_id,
            worker_id=worker.id,
            intent_id=worker.intent_id,
            context_snapshot_id=snapshot.id,
            prompt_hash=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
            model=model,
            input_chars=len(prompt_text),
            estimated_input_tokens=max(1, len(prompt_text) // 4),
            output_chars=len(output_json),
            estimated_output_tokens=max(1, len(output_json) // 4),
            provider_usage_json={
                "runtime": "codex",
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
        candidates = [requested_parent] if requested_parent is not None and requested_parent.codex_thread_id else []
        fallback_candidates = session.exec(
            select(Attempt)
            .where(
                Attempt.project_id == attempt.project_id,
                Attempt.id != attempt.id,
                Attempt.codex_thread_id.is_not(None),
                Attempt.resume_manifest_artifact_id.is_not(None),
                Attempt.status.in_(["SUCCESS", "COMPLETED", "PARTIAL", "FAILED", "TIMEOUT"]),
            )
            .order_by(Attempt.started_at.desc())
        ).all()
        candidates.extend(candidate for candidate in fallback_candidates if all(candidate.id != current.id for current in candidates))
        attempt.codex_control_token_hash = hashlib.sha256(control_token.encode("utf-8")).hexdigest()
        attempt.last_event_at = now_utc()
        parent: Attempt | None = None
        resume_thread_id: str | None = None
        for candidate in candidates:
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
        self,
        session: Session,
        *,
        parent: Attempt,
        workspace: Path,
    ) -> tuple[bool, dict[str, Any]]:
        workspace.mkdir(parents=True, exist_ok=True)
        manifest_artifact = session.get(Artifact, parent.resume_manifest_artifact_id) if parent.resume_manifest_artifact_id else None
        if manifest_artifact is None or manifest_artifact.project_id != parent.project_id:
            return False, {"reason": "resume_manifest_missing", "parent_attempt_id": parent.id}
        try:
            raw = Path(manifest_artifact.path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != manifest_artifact.sha256:
                raise ValueError("manifest artifact hash mismatch")
            manifest = json.loads(raw)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return False, {"reason": "resume_manifest_invalid", "detail": str(exc)[:500], "parent_attempt_id": parent.id}
        if manifest.get("project_id") != parent.project_id or manifest.get("attempt_id") != parent.id:
            return False, {"reason": "resume_manifest_scope_mismatch", "parent_attempt_id": parent.id}

        errors: list[dict[str, str]] = []
        if not manifest.get("inputs_complete"):
            errors.append({"path": "inputs/manifest.json", "reason": "input_manifest_incomplete"})
        if not manifest.get("work_state_complete"):
            errors.append({"path": "work", "reason": "work_state_incomplete"})
        for item in manifest.get("inputs", []):
            if not isinstance(item, dict):
                continue
            relative = Path(str(item.get("path") or ""))
            if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "inputs":
                errors.append({"path": str(relative), "reason": "unsafe_input_path"})
                continue
            target = workspace / relative
            if not target.is_file():
                errors.append({"path": str(item.get("path") or ""), "reason": "missing_input"})
                continue
            if self._sha256_file(target) != item.get("sha256"):
                errors.append({"path": str(item.get("path") or ""), "reason": "input_hash_mismatch"})

        work_dir = workspace / "work"
        work_dir.mkdir(exist_ok=True)
        for item in manifest.get("work_files", []):
            if not isinstance(item, dict):
                continue
            relative = Path(str(item.get("path") or ""))
            if relative.is_absolute() or ".." in relative.parts:
                errors.append({"path": str(relative), "reason": "unsafe_work_path"})
                continue
            artifact = session.get(Artifact, item.get("artifact_id"))
            if artifact is None or artifact.project_id != parent.project_id or artifact.sha256 != item.get("sha256"):
                errors.append({"path": str(relative), "reason": "work_artifact_invalid"})
                continue
            source = Path(artifact.path)
            if not source.is_file() or self._sha256_file(source) != artifact.sha256:
                errors.append({"path": str(relative), "reason": "work_artifact_hash_mismatch"})
                continue
            target = work_dir / relative
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            except OSError as exc:
                errors.append({"path": str(relative), "reason": f"work_restore_failed:{exc}"})

        source_home = self.settings.codex_workspace_dir / parent.project_id / parent.worker_id / "runtime" / "codex-home"
        target_home = workspace / "runtime" / "codex-home"
        if not manifest.get("codex_state_complete") or not source_home.is_dir():
            errors.append({"path": "runtime/codex-home", "reason": "codex_state_incomplete"})
        else:
            skipped = self._copy_resume_home(source_home, target_home)
            errors.extend({"path": path, "reason": "unreadable_codex_state"} for path in skipped)
            expected_state_paths = {
                str(item.get("path") or "")
                for item in manifest.get("codex_state", [])
                if isinstance(item, dict)
            }
            for item in manifest.get("codex_state", []):
                relative = Path(str(item.get("path") or ""))
                if relative.is_absolute() or ".." in relative.parts:
                    errors.append({"path": str(relative), "reason": "unsafe_codex_state_path"})
                    continue
                target = target_home / relative
                if not target.is_file() or self._sha256_file(target) != item.get("sha256"):
                    errors.append({"path": str(item.get("path") or ""), "reason": "codex_state_hash_mismatch"})
            actual_state_paths = {
                str(path.relative_to(target_home))
                for path in target_home.rglob("*")
                if path.is_file()
            }
            for extra in sorted(actual_state_paths - expected_state_paths):
                errors.append({"path": extra, "reason": "unexpected_codex_state_file"})
        if errors:
            shutil.rmtree(target_home, ignore_errors=True)
            shutil.rmtree(work_dir, ignore_errors=True)
            return False, {
                "reason": "resume_integrity_failed",
                "parent_attempt_id": parent.id,
                "manifest_artifact_id": manifest_artifact.id,
                "errors": errors[:100],
            }
        return True, {
            "reason": "resume_integrity_verified",
            "parent_attempt_id": parent.id,
            "manifest_artifact_id": manifest_artifact.id,
        }

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
                        WorkerEvent.event_type.in_(["checkpoint.saved", "blackboard.fact_appended", "artifact.read", "hypothesis.eliminated"]),
                    )
                    .order_by(WorkerEvent.created_at.desc())
                ).first()
                actions_without_progress = sum(
                    1
                    for candidate in action_count
                    if latest_progress is None or _utc_datetime(candidate.created_at) > _utc_datetime(latest_progress.created_at)
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

    def _write_prompt(self, session: Session, worker: Worker, snapshot: ContextSnapshot) -> Path:
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
        self._materialize_project_inputs(session, snapshot, workspace)
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
    def _materialize_project_inputs(session: Session, snapshot: ContextSnapshot, workspace: Path) -> None:
        """Expose only evidence owned by the active project to the container."""
        input_dir = workspace / "inputs"
        input_dir.mkdir(exist_ok=True)
        manifest: list[dict[str, str]] = []
        artifacts = session.exec(
            select(Artifact).where(
                Artifact.project_id == snapshot.project_id,
                Artifact.type.not_in(["codex-transcript", "resume-manifest", "resume-work-file"]),
            )
        ).all()
        for artifact in artifacts:
            source = Path(artifact.path)
            if not source.is_file():
                continue
            if CodexHarnessRuntime._sha256_file(source) != artifact.sha256:
                continue
            target = input_dir / f"{artifact.id}_{source.name}"
            shutil.copy2(source, target)
            manifest.append({"artifact_id": artifact.id, "path": f"inputs/{target.name}", "sha256": artifact.sha256})
        (input_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

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
        candidates = sorted(path for path in work_dir.rglob("*") if path.is_file()) if work_dir.is_dir() else []
        work_state_complete = len(candidates) <= self.settings.resume_max_files
        for path in candidates[: self.settings.resume_max_files]:
            if path.is_symlink():
                work_state_complete = False
                continue
            size = path.stat().st_size
            if total_bytes + size > self.settings.resume_max_bytes:
                work_state_complete = False
                break
            relative = path.relative_to(work_dir)
            artifact = self.artifact_store.write_file(
                session,
                project_id=attempt.project_id,
                source_attempt_id=attempt.id,
                source=path,
                summary=f"Resumable worker file: {relative}",
                artifact_type="resume-work-file",
                sensitivity="restricted",
                origin_kind="runtime_state",
                deduplicate=True,
            )
            work_files.append({
                "path": str(relative),
                "artifact_id": artifact.id,
                "sha256": artifact.sha256,
                "size": artifact.size,
            })
            total_bytes += size

        codex_state, codex_complete = self._codex_state_manifest(workspace / "runtime" / "codex-home")
        payload = {
            "version": 1,
            "project_id": attempt.project_id,
            "attempt_id": attempt.id,
            "parent_attempt_id": attempt.parent_attempt_id,
            "codex_thread_id": attempt.codex_thread_id,
            "inputs": inputs if isinstance(inputs, list) else [],
            "inputs_complete": inputs_complete,
            "work_files": work_files,
            "work_files_total_bytes": total_bytes,
            "work_state_complete": work_state_complete,
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
            if len(entries) >= self.settings.resume_max_files:
                break
        if walk_errors:
            complete = False
        return entries, complete and bool(entries)

    @staticmethod
    def _is_resumable_codex_state(relative: Path) -> bool:
        """Exclude immutable bundled assets and volatile locks from resume state."""
        return bool(relative.parts) and relative.parts[0] not in {"skills", ".tmp", "thread-writer-locks"}

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

    def _render_command(self, prompt_file: Path, *, model: str | None = None) -> str:
        model = model or self.settings.llm_model
        workspace = Path.cwd().resolve()
        relative_prompt = prompt_file.resolve().relative_to(workspace)
        container_prompt = Path("/workspace") / relative_prompt
        return self.settings.codex_command_template.format(
            prompt_file=prompt_file.name,
            prompt_filename=prompt_file.name,
            output_schema_file="aurora-output-schema.json",
            output_schema_filename="aurora-output-schema.json",
            last_message_file="aurora-last-message.json",
            last_message_filename="aurora-last-message.json",
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
        runner: CommandRunner | None = None,
        on_output=None,
    ) -> CommandResult:
        configured = self.settings.codex_timeout_seconds if self.settings.codex_timeout_seconds > 0 else None
        budget_timeout = int(timeout_seconds) if timeout_seconds else None
        timeout = min(value for value in (configured, budget_timeout) if value is not None) if configured or budget_timeout else None
        selected_runner = runner or self.command_runner
        if selected_runner is None:
            selected_runner = AutoCommandRunner(prefer_kali=True, allow_local_fallback=False)
        if on_output is not None and hasattr(selected_runner, "run_streaming"):
            return selected_runner.run_streaming(
                command=command,
                cwd=cwd,
                timeout=timeout,
                on_output=on_output,
                soft_timeout=int(soft_timeout_seconds) if soft_timeout_seconds else None,
                finalize_grace=int(finalize_grace_seconds) if finalize_grace_seconds else 10,
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
                self._validate_output(parsed)
                return self._normalize(parsed, snapshot, artifact_id, session=session, worker=worker, attempt=attempt), diagnostic
            except json.JSONDecodeError as exc:
                diagnostic.update({"source": "last_message_file", "error": f"invalid JSON at line {exc.lineno}, column {exc.colno}"})
                failure_kind = "output_invalid_json"
            except ValueError as exc:
                diagnostic.update({"source": "last_message_file", "error": str(exc)})
                failure_kind = "output_schema_invalid"
            except OSError as exc:
                diagnostic.update({"source": "last_message_file", "error": str(exc)})
        try:
            parsed = self._parse_json(stdout)
            self._validate_output(parsed)
            diagnostic.update({"source": "stdout_fallback", "stdout_bytes": len(stdout.encode("utf-8", errors="replace"))})
            return self._normalize(parsed, snapshot, artifact_id, session=session, worker=worker, attempt=attempt), diagnostic
        except (json.JSONDecodeError, ValueError) as exc:
            diagnostic.setdefault("error", str(exc)[:300])
        # Some Codex/provider combinations emit the final assistant message to
        # stderr. The process can hit the outer timeout after emitting a result
        # but before writing --output-last-message. Recover only a schema-valid
        # full worker result; progress JSON must not become a completion result.
        try:
            parsed = self._parse_json(stderr)
            self._validate_output(parsed)
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

    def _validate_output(self, parsed: Any) -> None:
        if not isinstance(parsed, dict):
            raise ValueError("final output must be a JSON object")
        allowed = set(self._json_schema()["properties"])
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
