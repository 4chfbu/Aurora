from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.models import Artifact, Attempt, ContextSnapshot, LLMTrace, ToolTrace, Worker, WorkerEvent, now_utc
from aurora.services.artifact_store import ArtifactStore
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
    "sandbox.exec",
]


class OpenAICompatibleRuntime:
    executable_tools = EXECUTABLE_TOOLS

    def __init__(self) -> None:
        self.settings = get_settings()
        self.model = self.settings.llm_model

    def execute(self, session: Session, *, worker: Worker, snapshot: ContextSnapshot) -> RuntimeOutput:
        if not self.settings.llm_api_key:
            raise RuntimeError("AURORA_WORKER_RUNTIME=openai requires AURORA_LLM_API_KEY or OPENAI_API_KEY")

        prompt = self._build_prompt(worker, snapshot)
        response = self._call_llm(prompt)
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
            model=self.model,
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

    def _call_llm(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.llm_timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM API request failed: HTTP {exc.code}: {detail[:1000]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM API request failed: {exc}") from exc

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
        summary = structured.get("summary") or "LLM generated a structured worker result."
        tool_requests = structured.get("tool_requests")
        if not isinstance(tool_requests, list):
            tool_requests = []
        visible_tool_names = {tool.get("name") for tool in snapshot.visible_tools_json}
        filtered_tool_requests = []
        ordered_tool_requests = [request for request in tool_requests if isinstance(request, dict)]
        ordered_tool_requests.sort(key=lambda request: request.get("tool_name") != "flag.verify")
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
        if not filtered_tool_requests:
            fallback_tool = self._select_tool(intent, visible_tool_names)
            filtered_tool_requests = [{"tool_name": fallback_tool, "request": self._tool_request(intent, fallback_tool)}]
        decision_summary = structured.get("decision_summary")
        if not isinstance(decision_summary, dict):
            decision_summary = {
                "selected_intent": intent.get("objective", "unknown objective"),
                "reason_summary": "模型生成了结构化工具计划。",
                "next_tool_plan": [request["tool_name"] for request in filtered_tool_requests],
            }
        return {
            "status": status,
            "summary": summary,
            "fact_candidates": structured.get("fact_candidates") if isinstance(structured.get("fact_candidates"), list) else [],
            "hypotheses": structured.get("hypotheses") if isinstance(structured.get("hypotheses"), list) else [],
            "artifact_refs": structured.get("artifact_refs") if isinstance(structured.get("artifact_refs"), list) else [],
            "failed_attempts": structured.get("failed_attempts") if isinstance(structured.get("failed_attempts"), list) else [],
            "suggested_intents": structured.get("suggested_intents") if isinstance(structured.get("suggested_intents"), list) else [],
            "fork_recommendations": structured.get("fork_recommendations") if isinstance(structured.get("fork_recommendations"), list) else [],
            "subagent_reports": structured.get("subagent_reports") if isinstance(structured.get("subagent_reports"), list) else [],
            "candidate_flags": structured.get("candidate_flags") if isinstance(structured.get("candidate_flags"), list) else [],
            "decision_summary": decision_summary,
            "tool_requests": filtered_tool_requests,
        }

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
        model = self.settings.model_for_role(str(worker.budgets.get("model_role", "solver")))
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
                "AURORA_CODEX_MODEL_CONTEXT_WINDOW": str(self.settings.codex_model_context_window),
                "AURORA_CODEX_AUTO_COMPACT_TOKEN_LIMIT": str(self.settings.codex_auto_compact_token_limit),
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
                runner=runner,
                on_output=lambda stream, line: self._record_codex_event(session, worker=worker, attempt=attempt, stream=stream, line=line),
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

        output_file = prompt_file.parent / "aurora-last-message.json"
        structured, output_diagnostic = self._parse_or_synthesize(
            output_file=output_file,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.exit_code,
            failure_kind=completed.failure_kind,
            artifact_id=artifact.id,
            snapshot=snapshot,
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
                "model_role": worker.budgets.get("model_role", "solver"),
                "requested_model": model,
                "model_context_window": self.settings.codex_model_context_window,
                "auto_compact_token_limit": self.settings.codex_auto_compact_token_limit,
                "hard_timeout_seconds": worker.budgets.get("hard_timeout_seconds"),
                "output": output_diagnostic,
                "mcp": mcp_import,
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
        parent = session.get(Attempt, attempt.parent_attempt_id) if attempt.parent_attempt_id else None
        resume_thread_id = parent.codex_thread_id if parent and parent.codex_thread_id else None
        attempt.codex_control_token_hash = hashlib.sha256(control_token.encode("utf-8")).hexdigest()
        attempt.last_event_at = now_utc()
        if resume_thread_id:
            if workspace is not None:
                source_home = self.settings.codex_workspace_dir / parent.project_id / parent.worker_id / "runtime" / "codex-home"
                target_home = workspace / "runtime" / "codex-home"
                if source_home.is_dir():
                    skipped = self._copy_resume_home(source_home, target_home)
                    if skipped:
                        session.add(
                            WorkerEvent(
                                project_id=worker.project_id,
                                worker_id=worker.id,
                                intent_id=worker.intent_id,
                                attempt_id=attempt.id,
                                event_type="codex.resume_files_skipped",
                                payload_json={"paths": skipped[:100], "count": len(skipped)},
                            )
                        )
            attempt.codex_thread_id = resume_thread_id
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

    @staticmethod
    def _copy_resume_home(source_home: Path, target_home: Path) -> list[str]:
        """Copy resumable Codex state while tolerating legacy root-only files."""
        skipped: list[str] = []

        def ignore(directory: str, names: list[str]) -> set[str]:
            ignored: set[str] = set()
            base = Path(directory)
            for name in names:
                path = base / name
                readable = os.access(path, os.R_OK)
                traversable = not path.is_dir() or os.access(path, os.X_OK)
                if not readable or not traversable:
                    ignored.add(name)
                    try:
                        skipped.append(str(path.relative_to(source_home)))
                    except ValueError:
                        skipped.append(name)
            return ignored

        shutil.copytree(source_home, target_home, dirs_exist_ok=True, ignore=ignore)
        return skipped

    @staticmethod
    def _record_codex_event(session: Session, *, worker: Worker, attempt: Attempt | None, stream: str, line: str) -> None:
        if attempt is None or stream != "stdout" or not line.startswith("{"):
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
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
                    "item_type": str(item.get("type") or "")[:100],
                    "item_status": str(item.get("status") or event.get("status") or "")[:100],
                },
            )
        )
        session.commit()

    def _write_prompt(self, session: Session, worker: Worker, snapshot: ContextSnapshot) -> Path:
        workspace = self.settings.codex_workspace_dir / snapshot.project_id / worker.id
        workspace.mkdir(parents=True, exist_ok=True)
        prompt_file = workspace / "aurora-intent.md"
        schema_file = workspace / "aurora-output-schema.json"
        prompt = PromptRenderer().render_codex_task(worker=worker, snapshot=snapshot)
        prompt_file.write_text(prompt, encoding="utf-8")
        schema_file.write_text(json.dumps(self._json_schema(), ensure_ascii=False, indent=2), encoding="utf-8")
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
        session.commit()
        return {"calls": calls, "invalid_lines": invalid_lines, "artifact_id": artifact.id}

    @staticmethod
    def _materialize_project_inputs(session: Session, snapshot: ContextSnapshot, workspace: Path) -> None:
        """Expose only evidence owned by the active project to the container."""
        input_dir = workspace / "inputs"
        input_dir.mkdir(exist_ok=True)
        manifest: list[dict[str, str]] = []
        artifacts = session.exec(
            select(Artifact).where(Artifact.project_id == snapshot.project_id, Artifact.type != "codex-transcript")
        ).all()
        for artifact in artifacts:
            source = Path(artifact.path)
            if not source.is_file():
                continue
            target = input_dir / f"{artifact.id}_{source.name}"
            shutil.copy2(source, target)
            manifest.append({"artifact_id": artifact.id, "path": f"inputs/{target.name}"})
        (input_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def _json_schema(self) -> dict[str, Any]:
        array = {"type": "array", "items": {"type": "object"}}
        return {
            "type": "object",
            "additionalProperties": True,
            "required": ["status", "summary", "decision_summary"],
            "properties": {
                "status": {"type": "string", "enum": ["success", "partial", "failed"]},
                "summary": {"type": "string"},
                "fact_candidates": array,
                "hypotheses": {"type": "array"},
                "artifact_refs": {"type": "array", "items": {"type": "string"}},
                "failed_attempts": array,
                "suggested_intents": array,
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
                        "additionalProperties": True,
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
                    "additionalProperties": True,
                },
                "tool_requests": array,
            },
        }

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

    def _run_command(self, command: str, cwd: Path, *, timeout_seconds: object = None, runner: CommandRunner | None = None, on_output=None) -> CommandResult:
        configured = self.settings.codex_timeout_seconds if self.settings.codex_timeout_seconds > 0 else None
        budget_timeout = int(timeout_seconds) if timeout_seconds else None
        timeout = min(value for value in (configured, budget_timeout) if value is not None) if configured or budget_timeout else None
        selected_runner = runner or self.command_runner
        if selected_runner is None:
            selected_runner = AutoCommandRunner(prefer_kali=True, allow_local_fallback=False)
        if on_output is not None and hasattr(selected_runner, "run_streaming"):
            return selected_runner.run_streaming(command=command, cwd=cwd, timeout=timeout, on_output=on_output)
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
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        diagnostic: dict[str, Any] = {"source": None, "output_file": str(output_file), "output_file_exists": output_file.exists()}
        if output_file.exists():
            try:
                raw = output_file.read_text(encoding="utf-8")
                diagnostic.update({"source": "last_message_file", "bytes": len(raw.encode("utf-8"))})
                parsed = json.loads(raw)
                self._validate_output(parsed)
                return self._normalize(parsed, snapshot, artifact_id), diagnostic
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
            return self._normalize(parsed, snapshot, artifact_id), diagnostic
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
            return self._normalize(parsed, snapshot, artifact_id), diagnostic
        except (json.JSONDecodeError, ValueError) as exc:
            diagnostic.setdefault("stderr_error", str(exc)[:300])
        failure_kind = failure_kind or self._provider_failure_kind(stderr)
        failure_kind = failure_kind or ("command_timed_out" if exit_code == 124 else "resource_terminated" if exit_code == 137 else "output_missing" if not output_file.exists() else "output_invalid_json")
        return self._failure_output(failure_kind, stderr, artifact_id, snapshot), diagnostic

    @staticmethod
    def _provider_failure_kind(stderr: str) -> str | None:
        lowered = stderr.lower()
        if "maximum context length" in lowered or "context_length_exceeded" in lowered:
            return "context_length_exceeded"
        if '"type":"invalid_request_error"' in lowered or "invalid_request_error" in lowered:
            return "provider_invalid_request"
        return None

    def _validate_output(self, parsed: Any) -> None:
        if not isinstance(parsed, dict):
            raise ValueError("final output must be a JSON object")
        missing = {"status", "summary", "decision_summary"} - set(parsed)
        if missing:
            raise ValueError(f"final output missing required fields: {', '.join(sorted(missing))}")
        if parsed["status"] not in {"success", "partial", "failed"}:
            raise ValueError("final output has an invalid status")
        if not isinstance(parsed["summary"], str) or not isinstance(parsed["decision_summary"], dict):
            raise ValueError("final output has invalid summary or decision_summary")

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

    def _normalize(self, structured: dict[str, Any], snapshot: ContextSnapshot, artifact_id: str) -> dict[str, Any]:
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
        ordered_tool_requests = [tool_request for tool_request in structured["tool_requests"] if isinstance(tool_request, dict)]
        ordered_tool_requests.sort(key=lambda request: request.get("tool_name") != "flag.verify")
        for tool_request in ordered_tool_requests[:3]:
            tool_name = tool_request.get("tool_name")
            if not isinstance(tool_name, str) or tool_name not in visible_tool_names:
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


def get_worker_runtime() -> WorkerRuntime:
    runtime = get_settings().worker_runtime.strip().lower()
    if runtime in {"codex", "codex_harness", "harness"}:
        return CodexHarnessRuntime()
    if runtime in {"openai_direct", "openai", "llm", "real"}:
        return OpenAICompatibleRuntime()
    raise RuntimeError(
        f"unsupported AURORA_WORKER_RUNTIME={runtime!r}; expected 'codex' or 'openai_direct'"
    )
