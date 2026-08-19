from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlmodel import Session, select

from aurora.models import Artifact, FlagCandidate, Intent, ToolTrace, WorkerEvent
from aurora.services.command_runner import AutoCommandRunner, CommandRunner
from aurora.services.artifact_store import ArtifactStore
from aurora.services.policy import PolicyEngine
from aurora.services.browser_interaction import BrowserInteractionService
from aurora.config import get_settings
from aurora.services.flag_validator import FlagValidator
from aurora.services.flag_submission import FlagSubmissionService
from aurora.services.competition_adapter import CompetitionAdapter


DENIED_TOKENS = ["rm -rf", "mkfs", ":(){", "dd if=", "shutdown", "reboot", "docker.sock"]


class FlagVerifyRequest(BaseModel):
    source_artifact_refs: list[str] = Field(min_length=1, max_length=20)
    # Normally a workspace path; bounded inline Python is accepted as a
    # compatibility form for providers that serialize the script body.
    verification_script: str = Field(min_length=1, max_length=1_000_000)
    timeout_seconds: int = Field(default=30, ge=1, le=60)


class FlagSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1, max_length=128)


@dataclass
class ToolResult:
    success: bool
    summary: str
    artifact_refs: list[str]
    metrics: dict[str, Any]
    warnings: list[str]
    trace_id: str


class CapabilityGateway:
    def __init__(
        self,
        artifact_store: ArtifactStore | None = None,
        command_runner: CommandRunner | None = None,
        verification_runner: CommandRunner | None = None,
        policy_engine: PolicyEngine | None = None,
        competition_adapter: CompetitionAdapter | None = None,
    ) -> None:
        self.artifact_store = artifact_store or ArtifactStore()
        self.command_runner = command_runner or AutoCommandRunner()
        self.verification_runner = verification_runner or command_runner or AutoCommandRunner(
            prefer_kali=True,
            allow_local_fallback=False,
            network="none",
            workspace_read_only=True,
        )
        self.policy_engine = policy_engine or PolicyEngine()
        self.competition_adapter = competition_adapter

    def execute(
        self,
        session: Session,
        *,
        project_id: str,
        tool_name: str,
        request: dict[str, Any],
        worker_id: str | None = None,
        intent_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ToolResult:
        policy = self.policy_engine.check_tool_request(session, project_id=project_id, tool_name=tool_name, request=request)
        if not policy.allowed:
            trace = ToolTrace(
                project_id=project_id,
                worker_id=worker_id,
                intent_id=intent_id,
                attempt_id=attempt_id,
                tool_name=tool_name,
                request_json=request,
                policy_decision="deny",
                summary=policy.reason,
            )
            session.add(trace)
            session.commit()
            session.refresh(trace)
            return ToolResult(False, policy.reason, [], {"exit_code": None, "backend": "policy"}, [policy.reason], trace.id)

        if tool_name == "capability.request":
            return self._capability_request_unsupported(session, project_id, request, worker_id, intent_id, attempt_id)
        if tool_name == "sandbox.exec":
            return self._sandbox_exec(session, project_id, request, worker_id, intent_id, attempt_id)
        if tool_name == "flag.verify":
            return self._flag_verify(session, project_id, request, worker_id, intent_id, attempt_id)
        if tool_name == "flag.submit":
            return self._flag_submit(session, project_id, request, worker_id, intent_id, attempt_id)
        if tool_name == "fofa.search":
            return self._fofa_search(session, project_id, request, worker_id, intent_id, attempt_id)
        if tool_name == "blackboard.query":
            return self._blackboard_query(session, project_id, request, worker_id, intent_id, attempt_id)
        if tool_name == "browser.interact":
            return self._browser_interact(session, project_id, request, worker_id, intent_id, attempt_id)
        if tool_name in {"binary.inspect", "forensic.inspect"} and not request.get("path") and request.get("artifact_ref"):
            artifact = session.get(Artifact, str(request["artifact_ref"]))
            if artifact is None or artifact.project_id != project_id:
                return self._tool_error_result(
                    session, project_id, tool_name, request, worker_id, intent_id, attempt_id,
                    "artifact_ref does not identify an artifact in this project",
                )
            request = {**request, "path": artifact.path}
        try:
            command = self._semantic_command(tool_name, request)
        except (TypeError, ValueError) as exc:
            # A malformed model-authored tool request is an ordinary tool
            # failure.  It must not escape the gateway and fail the whole
            # challenge-group runner.
            return self._tool_error_result(
                session, project_id, tool_name, request, worker_id, intent_id, attempt_id, str(exc),
            )
        if command is not None:
            semantic_request = {
                "command": command,
                "cwd": request.get("cwd", "."),
                "timeout_seconds": request.get("timeout_seconds", 15),
                "max_output_bytes": request.get("max_output_bytes", 24_000),
                "semantic_tool": tool_name,
                "semantic_request": request,
            }
            return self._sandbox_exec(session, project_id, semantic_request, worker_id, intent_id, attempt_id, tool_name=tool_name)
        return self._semantic_stub(session, project_id, tool_name, request, worker_id, intent_id, attempt_id)

    @staticmethod
    def _tool_error_result(
        session: Session,
        project_id: str,
        tool_name: str,
        request: dict[str, Any],
        worker_id: str | None,
        intent_id: str | None,
        attempt_id: str | None,
        reason: str,
    ) -> ToolResult:
        trace = ToolTrace(
            project_id=project_id,
            worker_id=worker_id,
            intent_id=intent_id,
            attempt_id=attempt_id,
            tool_name=tool_name,
            request_json=request,
            policy_decision="execution_error",
            summary=reason[:1000],
        )
        session.add(trace)
        session.commit()
        session.refresh(trace)
        return ToolResult(False, reason[:1000], [], {"exit_code": None, "backend": "validation"}, [reason[:1000]], trace.id)

    def _flag_verify(
        self,
        session: Session,
        project_id: str,
        request: dict[str, Any],
        worker_id: str | None,
        intent_id: str | None,
        attempt_id: str | None,
    ) -> ToolResult:
        """Replay a model-authored Python derivation without supplying an expected flag."""
        if not isinstance(request.get("source_artifact_refs"), list) or not request.get("source_artifact_refs"):
            return self._verification_failure(
                session, project_id, request, worker_id, intent_id, attempt_id,
                "flag.verify requires a non-empty source_artifact_refs array",
            )
        if not str(request.get("verification_script") or "").strip():
            return self._verification_failure(
                session, project_id, request, worker_id, intent_id, attempt_id,
                "flag.verify requires verification_script",
            )
        normalized_request = dict(request)
        requested_timeout = normalized_request.get("timeout_seconds", 30)
        try:
            numeric_timeout = int(requested_timeout)
        except (TypeError, ValueError):
            numeric_timeout = requested_timeout
        if isinstance(numeric_timeout, int):
            normalized_request["timeout_seconds"] = max(1, min(numeric_timeout, 60))
        try:
            validated = FlagVerifyRequest.model_validate(normalized_request)
        except ValidationError as exc:
            return self._verification_failure(
                session, project_id, request, worker_id, intent_id, attempt_id,
                f"invalid flag.verify request: {exc.errors(include_url=False)}",
            )
        refs = validated.source_artifact_refs
        script_name = validated.verification_script.strip()
        timeout = validated.timeout_seconds
        if not worker_id:
            return self._verification_failure(
                session, project_id, request, worker_id, intent_id, attempt_id,
                "flag.verify is missing the server-side worker context",
            )
        if not isinstance(refs, list) or not refs:
            return self._verification_failure(
                session, project_id, request, worker_id, intent_id, attempt_id,
                "flag.verify requires a non-empty source_artifact_refs array",
            )
        if not script_name:
            return self._verification_failure(
                session, project_id, request, worker_id, intent_id, attempt_id,
                "flag.verify requires verification_script",
            )

        workspace = (get_settings().codex_workspace_dir / project_id / worker_id).resolve()
        inline_script = "\n" in script_name or script_name.lstrip().startswith(("import ", "from ", "#!", "def ", "print("))
        if inline_script:
            script_bytes = script_name.encode("utf-8")
            if len(script_bytes) > 1_000_000:
                return self._verification_failure(session, project_id, request, worker_id, intent_id, attempt_id, "verification script exceeds 1 MB")
            workspace.mkdir(parents=True, exist_ok=True)
            inline_name = f".aurora-inline-verify-{hashlib.sha256(script_bytes).hexdigest()[:16]}.py"
            script_path = (workspace / inline_name).resolve()
            script_path.write_bytes(script_bytes)
        else:
            # Worker containers see their mounted workspace at /workspace,
            # while the gateway resolves host-side worker files.
            if script_name == "/workspace":
                script_name = ""
            elif script_name.startswith("/workspace/"):
                script_name = script_name[len("/workspace/"):]
            script_path = (workspace / script_name).resolve()
        try:
            script_path.relative_to(workspace)
        except ValueError:
            return self._verification_failure(session, project_id, request, worker_id, intent_id, attempt_id, "verification script escapes the worker workspace")
        if script_path.suffix != ".py" or not script_path.is_file():
            return self._verification_failure(session, project_id, request, worker_id, intent_id, attempt_id, "verification_script must name an existing Python file in the worker workspace")

        artifacts: list[Artifact] = []
        for ref in dict.fromkeys(ref for ref in refs if isinstance(ref, str)):
            artifact = session.get(Artifact, ref)
            if artifact is None or artifact.project_id != project_id or artifact.type in {"codex-transcript", "subagent-transcript", "blackboard-query", "tool-request"}:
                return self._verification_failure(session, project_id, request, worker_id, intent_id, attempt_id, f"untrusted or foreign source artifact: {ref}")
            artifacts.append(artifact)
        if not artifacts:
            return self._verification_failure(session, project_id, request, worker_id, intent_id, attempt_id, "no valid source artifacts were declared")

        script_bytes = script_path.read_bytes()
        if len(script_bytes) > 1_000_000:
            return self._verification_failure(session, project_id, request, worker_id, intent_id, attempt_id, "verification script exceeds 1 MB")
        runs: list[dict[str, Any]] = []
        verify_root = Path(tempfile.mkdtemp(prefix=".aurora-flag-verify-", dir=Path.cwd()))
        try:
            for index in range(2):
                run_dir = verify_root / f"run-{index + 1}"
                input_dir = run_dir / "inputs"
                input_dir.mkdir(parents=True)
                replay_script = run_dir / "verify.py"
                replay_script.write_bytes(script_bytes)
                manifest: list[dict[str, str]] = []
                for artifact in artifacts:
                    source = Path(artifact.path)
                    # Keep the original basename and extension. Verification
                    # scripts commonly need to identify archive/file formats,
                    # and stripping ``.zip`` made correctly declared evidence
                    # indistinguishable inside the isolated replay workspace.
                    target = input_dir / f"{artifact.id}_{source.name}"
                    shutil.copyfile(source, target)
                    target.chmod(0o444)
                    manifest.append({"artifact_id": artifact.id, "path": f"inputs/{target.name}", "sha256": artifact.sha256})
                (input_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
                completed = self.verification_runner.run(command="PYTHONDONTWRITEBYTECODE=1 python3 verify.py inputs/manifest.json", cwd=run_dir, timeout=timeout)
                output = completed.stdout.strip()
                runs.append({"exit_code": completed.exit_code, "stdout": output, "stderr": completed.stderr[-1000:]})
                if completed.exit_code != 0 or not FlagValidator.is_valid_flag_value(output):
                    return self._verification_failure(
                        session, project_id, request, worker_id, intent_id, attempt_id,
                        "verification replay must exit successfully and print exactly one flag",
                        runs=runs,
                    )
            value = runs[0]["stdout"]
            if runs[1]["stdout"] != value:
                return self._verification_failure(session, project_id, request, worker_id, intent_id, attempt_id, "verification replay produced inconsistent results", runs=runs)
            value_hash = hashlib.sha256(value.encode("utf-8")).hexdigest()
            rejected = session.exec(
                select(FlagCandidate).where(
                    FlagCandidate.project_id == project_id,
                    FlagCandidate.value_hash == value_hash,
                    FlagCandidate.status == "REJECTED",
                )
            ).first()
            if rejected is not None:
                return self._verification_failure(session, project_id, request, worker_id, intent_id, attempt_id, "verification produced a previously rejected candidate", runs=runs)
            request_text = json.dumps(request, ensure_ascii=False, sort_keys=True)
            script_text = script_bytes.decode("utf-8", errors="replace")
            if value in request_text or value in script_text:
                return self._verification_failure(session, project_id, request, worker_id, intent_id, attempt_id, "candidate flag is hard-coded in the verification request or script", runs=runs)

            payload = {
                "status": "verified",
                "value": value,
                "source_artifacts": [{"id": artifact.id, "sha256": artifact.sha256} for artifact in artifacts],
                "script_sha256": hashlib.sha256(script_bytes).hexdigest(),
                "runs": runs,
            }
            artifact = self.artifact_store.write_text(
                session,
                project_id=project_id,
                source_attempt_id=attempt_id,
                content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                summary="Flag derivation replayed twice with identical output",
                artifact_type="flag-verification",
                origin_kind="verified_derivation",
            )
            candidate = session.exec(
                select(FlagCandidate).where(
                    FlagCandidate.project_id == project_id,
                    FlagCandidate.value_hash == value_hash,
                )
            ).first()
            if candidate is None:
                candidate = FlagCandidate(project_id=project_id, value=value, value_hash=value_hash)
            if candidate.status not in {"ACCEPTED", "SUBMITTED", "AWAITING_MANUAL_VALIDATION"}:
                candidate.status = "LOCAL_VERIFIED"
                candidate.provenance_kind = "DERIVED_REPLAY"
                candidate.artifact_refs = [artifact.id, *[source.id for source in artifacts]]
                candidate.verification_artifact_ref = artifact.id
                candidate.source_attempt_id = attempt_id
                candidate.source_worker_id = worker_id
                candidate.rejection_reason = None
            session.add(candidate)
            session.flush()
            trace = ToolTrace(
                project_id=project_id, worker_id=worker_id, intent_id=intent_id, attempt_id=attempt_id,
                tool_name="flag.verify", request_json=request, policy_decision="allow", exit_code=0,
                summary="Flag derivation verified by deterministic replay", artifact_refs=[artifact.id, *[source.id for source in artifacts]],
            )
            session.add(trace)
            session.commit()
            session.refresh(trace)
            return ToolResult(
                True,
                trace.summary or "Flag verified",
                [artifact.id, *[source.id for source in artifacts]],
                {
                    "runs": 2,
                    "backend": "isolated-replay",
                    "candidate_id": candidate.id,
                    "effective_timeout_seconds": timeout,
                    "inline_script": inline_script,
                },
                [],
                trace.id,
            )
        finally:
            shutil.rmtree(verify_root, ignore_errors=True)

    def _flag_submit(
        self,
        session: Session,
        project_id: str,
        request: dict[str, Any],
        worker_id: str | None,
        intent_id: str | None,
        attempt_id: str | None,
    ) -> ToolResult:
        try:
            validated = FlagSubmitRequest.model_validate(request)
        except ValidationError as exc:
            return self._tool_error_result(
                session,
                project_id,
                "flag.submit",
                request,
                worker_id,
                intent_id,
                attempt_id,
                f"invalid flag.submit request: {exc.errors(include_url=False)}",
            )
        outcome = FlagSubmissionService().submit(
            session,
            project_id=project_id,
            candidate_id=validated.candidate_id.strip(),
            adapter=self.competition_adapter,
            worker_id=worker_id,
            intent_id=intent_id,
            attempt_id=attempt_id,
        )
        executed = outcome.status in {"accepted", "partial", "rejected"}
        candidate = session.get(FlagCandidate, outcome.candidate_id) if outcome.candidate_id else None
        artifact_refs = list(candidate.artifact_refs) if candidate is not None else []
        trace = ToolTrace(
            project_id=project_id,
            worker_id=worker_id,
            intent_id=intent_id,
            attempt_id=attempt_id,
            tool_name="flag.submit",
            request_json={"candidate_id": validated.candidate_id.strip()},
            policy_decision="allow" if executed else "execution_error",
            exit_code=0 if executed else 1,
            summary=outcome.summary,
            artifact_refs=artifact_refs,
        )
        session.add(trace)
        session.commit()
        session.refresh(trace)
        metrics = {
            "status": outcome.status,
            "candidate_id": outcome.candidate_id,
            "accepted": outcome.accepted,
            "completed": outcome.completed,
            "detail": outcome.detail,
            "backend": "competition-adapter",
        }
        return ToolResult(executed, outcome.summary, artifact_refs, metrics, [] if executed else [outcome.summary], trace.id)

    def _verification_failure(
        self,
        session: Session,
        project_id: str,
        request: dict[str, Any],
        worker_id: str | None,
        intent_id: str | None,
        attempt_id: str | None,
        reason: str,
        *,
        runs: list[dict[str, Any]] | None = None,
    ) -> ToolResult:
        artifact = self.artifact_store.write_text(
            session,
            project_id=project_id,
            source_attempt_id=attempt_id,
            content=json.dumps({"status": "rejected", "reason": reason, "runs": runs or []}, ensure_ascii=False, sort_keys=True),
            summary=f"Flag verification rejected: {reason}",
            artifact_type="flag-verification-failed",
            origin_kind="verification_failure",
        )
        trace = ToolTrace(
            project_id=project_id, worker_id=worker_id, intent_id=intent_id, attempt_id=attempt_id,
            tool_name="flag.verify", request_json=request, policy_decision="allow", exit_code=1,
            summary=reason, artifact_refs=[artifact.id],
        )
        session.add(trace)
        session.commit()
        session.refresh(trace)
        return ToolResult(False, reason, [artifact.id], {"runs": len(runs or []), "backend": "isolated-replay"}, [reason], trace.id)

    def _fofa_search(
        self, session: Session, project_id: str, request: dict[str, Any], worker_id: str | None, intent_id: str | None, attempt_id: str | None
    ) -> ToolResult:
        settings = get_settings()
        if not settings.fofa_configured:
            return self._denied_tool_result(session, project_id, "fofa.search", request, worker_id, intent_id, attempt_id, "FOFA is not configured")
        query = str(request.get("query", "")).strip()
        size = max(1, min(int(request.get("size", 20)), 100))
        params = urllib.parse.urlencode(
            {
                "email": settings.fofa_email,
                "key": settings.fofa_key,
                "qbase64": base64.b64encode(query.encode("utf-8")).decode("ascii"),
                "size": size,
                "fields": "host,ip,port,domain,protocol,title",
            }
        )
        try:
            with urllib.request.urlopen(f"{settings.fofa_base_url}?{params}", timeout=settings.fofa_timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
                payload = json.loads(body)
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            return self._denied_tool_result(session, project_id, "fofa.search", request, worker_id, intent_id, attempt_id, f"FOFA request failed: {exc}")
        if payload.get("error"):
            return self._denied_tool_result(session, project_id, "fofa.search", request, worker_id, intent_id, attempt_id, f"FOFA error: {payload.get('errmsg', 'unknown error')}")
        rows = payload.get("results") or []
        artifact = self.artifact_store.write_text(
            session,
            project_id=project_id,
            source_attempt_id=attempt_id,
            content=json.dumps({"query": query, "results": rows}, ensure_ascii=False, indent=2),
            summary=f"FOFA search returned {len(rows)} result(s) for authorized target",
            artifact_type="fofa-search",
            origin_kind="target_observation",
        )
        trace = ToolTrace(
            project_id=project_id, worker_id=worker_id, intent_id=intent_id, attempt_id=attempt_id,
            tool_name="fofa.search", request_json={"query": query, "size": size}, policy_decision="allow",
            summary=f"FOFA returned {len(rows)} result(s)", artifact_refs=[artifact.id],
        )
        session.add(trace)
        session.commit()
        session.refresh(trace)
        return ToolResult(True, trace.summary or "FOFA search completed", [artifact.id], {"result_count": len(rows), "backend": "fofa-mcp"}, [], trace.id)

    def _blackboard_query(
        self, session: Session, project_id: str, request: dict[str, Any], worker_id: str | None, intent_id: str | None, attempt_id: str | None
    ) -> ToolResult:
        from sqlmodel import select
        from aurora.models import Fact

        limit = max(1, min(int(request.get("limit", 10)), 25))
        facts = session.exec(select(Fact).where(Fact.project_id == project_id, Fact.status == "ACTIVE").limit(limit)).all()
        payload = [{"id": fact.id, "statement": fact.statement, "confidence": fact.confidence, "evidence_refs": fact.evidence_refs, "evidence_items": fact.evidence_items} for fact in facts]
        artifact = self.artifact_store.write_text(session, project_id=project_id, source_attempt_id=attempt_id, content=json.dumps(payload, ensure_ascii=False, indent=2), summary=f"Blackboard query returned {len(payload)} fact(s)", artifact_type="blackboard-query", origin_kind="derived_context")
        trace = ToolTrace(project_id=project_id, worker_id=worker_id, intent_id=intent_id, attempt_id=attempt_id, tool_name="blackboard.query", request_json={"limit": limit}, policy_decision="allow", summary=f"Blackboard returned {len(payload)} fact(s)", artifact_refs=[artifact.id])
        session.add(trace)
        session.commit()
        session.refresh(trace)
        return ToolResult(True, trace.summary or "Blackboard query completed", [artifact.id], {"result_count": len(payload), "backend": "aurora-mcp"}, [], trace.id)

    def _browser_interact(
        self, session: Session, project_id: str, request: dict[str, Any], worker_id: str | None, intent_id: str | None, attempt_id: str | None
    ) -> ToolResult:
        result = BrowserInteractionService().execute(session, project_id=project_id, request=request, worker_id=worker_id, intent_id=intent_id, attempt_id=attempt_id)
        trace = ToolTrace(
            project_id=project_id,
            worker_id=worker_id,
            intent_id=intent_id,
            attempt_id=attempt_id,
            tool_name="browser.interact",
            request_json=request,
            # BrowserInteractionService returns False for both policy failures
            # and ordinary execution outcomes such as a navigation timeout or
            # a page with no launch control.  The policy gate has already run
            # before this point, so recording every unsuccessful interaction
            # as a denial makes Observer stop otherwise viable CTF rounds.
            policy_decision="allow" if result.success else "execution_error",
            summary=result.summary,
            artifact_refs=result.artifact_refs,
        )
        session.add(trace)
        session.commit()
        session.refresh(trace)
        return ToolResult(result.success, result.summary, result.artifact_refs, {"target_urls": result.target_urls, "backend": "playwright"}, [], trace.id)

    def _denied_tool_result(
        self, session: Session, project_id: str, tool_name: str, request: dict[str, Any], worker_id: str | None, intent_id: str | None, attempt_id: str | None, reason: str
    ) -> ToolResult:
        trace = ToolTrace(project_id=project_id, worker_id=worker_id, intent_id=intent_id, attempt_id=attempt_id, tool_name=tool_name, request_json=request, policy_decision="deny", summary=reason)
        session.add(trace)
        session.commit()
        session.refresh(trace)
        return ToolResult(False, reason, [], {"backend": "policy"}, [reason], trace.id)

    def _sandbox_exec(
        self,
        session: Session,
        project_id: str,
        request: dict[str, Any],
        worker_id: str | None,
        intent_id: str | None,
        attempt_id: str | None,
        tool_name: str = "sandbox.exec",
    ) -> ToolResult:
        command = str(request.get("command", "")).strip()
        cwd = Path(str(request.get("cwd", "."))).resolve()
        timeout = min(int(request.get("timeout_seconds", 10)), 30)
        max_output = min(int(request.get("max_output_bytes", 16_384)), 64_000)

        denied_reason = self._deny_reason(command, cwd)
        if denied_reason:
            trace = ToolTrace(
                project_id=project_id,
                worker_id=worker_id,
                intent_id=intent_id,
                attempt_id=attempt_id,
                tool_name=tool_name,
                request_json=request,
                policy_decision="deny",
                command=command,
                cwd=str(cwd),
                timeout_seconds=timeout,
                exit_code=None,
                summary=denied_reason,
            )
            session.add(trace)
            session.commit()
            session.refresh(trace)
            return ToolResult(False, denied_reason, [], {"exit_code": None, "backend": "policy"}, [denied_reason], trace.id)

        completed = self.command_runner.run(command=command, cwd=cwd, timeout=timeout)
        stdout = completed.stdout[:max_output]
        stderr = completed.stderr[:max_output]
        raw = (
            f"backend={completed.backend}\n"
            f"cwd={completed.cwd}\n"
            f"$ {completed.command}\n\n"
            f"[executed]\n{completed.executed_command}\n\n"
            f"[stdout]\n{completed.stdout}\n\n"
            f"[stderr]\n{completed.stderr}"
        )
        origin_kind = "target_observation"
        if tool_name == "sandbox.exec":
            intent = session.get(Intent, intent_id) if intent_id else None
            declared = (intent.budget or {}).get("tool_request") if intent is not None else None
            origin_kind = "operator_observation" if isinstance(declared, dict) and declared == request else "model_generated"
        artifact = self.artifact_store.write_text(
            session,
            project_id=project_id,
            source_attempt_id=attempt_id,
            content=raw,
            summary=f"{tool_name} {completed.backend} exit={completed.exit_code}: {command[:120]}",
            artifact_type="terminal",
            origin_kind=origin_kind,
        )
        summary_source = stdout or stderr or "no output"
        summary = summary_source.replace("\n", " ")[:300]
        trace = ToolTrace(
            project_id=project_id,
            worker_id=worker_id,
            intent_id=intent_id,
            attempt_id=attempt_id,
            tool_name=tool_name,
            request_json={k: v for k, v in request.items() if k != "env"},
            policy_decision="allow",
            command=command,
            cwd=str(cwd),
            timeout_seconds=timeout,
            exit_code=completed.exit_code,
            summary=summary,
            artifact_refs=[artifact.id],
            stderr_summary=stderr.replace("\n", " ")[:300] if stderr else None,
        )
        session.add(trace)
        session.commit()
        session.refresh(trace)
        return ToolResult(
            completed.exit_code == 0,
            summary,
            [artifact.id],
            {"exit_code": completed.exit_code, "output_bytes": len(raw.encode("utf-8")), "backend": completed.backend},
            [],
            trace.id,
        )

    def _semantic_command(self, tool_name: str, request: dict[str, Any]) -> str | None:
        if tool_name == "http.request":
            url = self._safe_url(str(request.get("url", "")))
            method = str(request.get("method", "GET")).upper()
            if method not in {"GET", "HEAD", "OPTIONS"}:
                raise ValueError("http.request MVP only allows GET, HEAD, and OPTIONS")
            return f"curl -i -L --max-time 10 -X {method} {self._quote(url)}"

        if tool_name == "network.scan":
            target = self._safe_target(str(request.get("target", "")))
            ports = str(request.get("ports", "80,443,8080"))
            scan_args = "-sT -Pn --max-retries 1 --host-timeout 30s"
            return f"nmap {scan_args} -p {self._quote(ports)} {self._quote(target)}"

        if tool_name == "web.enumerate":
            url = self._safe_url(str(request.get("url", ""))).rstrip("/")
            wordlist = str(request.get("wordlist", "/usr/share/wordlists/dirb/common.txt"))
            extensions = str(request.get("extensions", ""))
            ffuf = f"ffuf -u {self._quote(url + '/FUZZ')} -w {self._quote(wordlist)} -t 10 -rate 50 -of json"
            if extensions:
                ffuf += f" -e {self._quote(extensions)}"
            return f"command -v ffuf >/dev/null && {ffuf} || dirb {self._quote(url)} {self._quote(wordlist)}"

        if tool_name == "binary.inspect":
            path = self._safe_path(str(request.get("path", "")))
            quoted = self._quote(path)
            return f"file {quoted}; printf '\\n[sha256]\\n'; sha256sum {quoted}; printf '\\n[strings]\\n'; strings -n 6 {quoted} | head -n 80"

        if tool_name == "forensic.inspect":
            path = self._safe_path(str(request.get("path", "")))
            quoted = self._quote(path)
            return f"file {quoted}; printf '\\n[exiftool]\\n'; (exiftool {quoted} 2>/dev/null || true); printf '\\n[binwalk]\\n'; (binwalk {quoted} 2>/dev/null || true)"

        return None

    def _safe_url(self, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be absolute http(s)")
        return value

    def _safe_target(self, value: str) -> str:
        if not value or any(char in value for char in " ;|&`$()<>\\n"):
            raise ValueError("target contains unsafe characters")
        return value

    def _safe_path(self, value: str) -> str:
        if not value or any(char in value for char in "|&`$<>\\n"):
            raise ValueError("path contains unsafe characters")
        return value

    def _quote(self, value: str) -> str:
        return "'" + value.replace("'", "'\\''") + "'"

    def _semantic_stub(
        self,
        session: Session,
        project_id: str,
        tool_name: str,
        request: dict[str, Any],
        worker_id: str | None,
        intent_id: str | None,
        attempt_id: str | None,
    ) -> ToolResult:
        summary = f"{tool_name} accepted as a Kali-first semantic tool request; native adapter implementation is pending."
        artifact = self.artifact_store.write_text(
            session,
            project_id=project_id,
            source_attempt_id=attempt_id,
            content=f"tool={tool_name}\nrequest={request}\nsummary={summary}\n",
            summary=summary,
            artifact_type="tool-request",
            origin_kind="model_generated",
        )
        trace = ToolTrace(
            project_id=project_id,
            worker_id=worker_id,
            intent_id=intent_id,
            attempt_id=attempt_id,
            tool_name=tool_name,
            request_json=request,
            policy_decision="allow_stub",
            summary=summary,
            artifact_refs=[artifact.id],
        )
        session.add(trace)
        session.commit()
        session.refresh(trace)
        return ToolResult(True, summary, [artifact.id], {"stub": True}, [], trace.id)

    def _capability_request_unsupported(
        self,
        session: Session,
        project_id: str,
        request: dict[str, Any],
        worker_id: str | None,
        intent_id: str | None,
        attempt_id: str | None,
    ) -> ToolResult:
        reason = "capability.request is disabled; additional runtime capabilities require an operator escalation and a rebuilt worker image."
        trace = ToolTrace(
            project_id=project_id,
            worker_id=worker_id,
            intent_id=intent_id,
            attempt_id=attempt_id,
            tool_name="capability.request",
            request_json=request,
            policy_decision="deny",
            summary=reason,
        )
        session.add(trace)
        session.add(
            WorkerEvent(
                project_id=project_id,
                worker_id=worker_id,
                intent_id=intent_id,
                attempt_id=attempt_id,
                event_type="operator.escalated",
                payload_json={"reason": "capability_request_denied", "request": request},
            )
        )
        session.commit()
        session.refresh(trace)
        return ToolResult(False, reason, [], {"backend": "policy"}, [reason], trace.id)

    def _deny_reason(self, command: str, cwd: Path) -> str | None:
        if not command:
            return "empty command denied"
        lowered = command.lower()
        if any(token in lowered for token in DENIED_TOKENS):
            return "command denied by sandbox policy"
        root = Path.cwd().resolve()
        try:
            cwd.relative_to(root)
        except ValueError:
            return f"cwd must stay under workspace root: {root}"
        return None
