from __future__ import annotations

from dataclasses import dataclass
import base64
import json
from pathlib import Path
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse

from sqlmodel import Session

from aurora.models import ToolTrace
from aurora.services.command_runner import AutoCommandRunner, CommandRunner
from aurora.services.artifact_store import ArtifactStore
from aurora.services.policy import PolicyEngine
from aurora.services.browser_interaction import BrowserInteractionService
from aurora.config import get_settings


DENIED_TOKENS = ["rm -rf", "mkfs", ":(){", "dd if=", "shutdown", "reboot", "docker.sock"]


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
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self.artifact_store = artifact_store or ArtifactStore()
        self.command_runner = command_runner or AutoCommandRunner()
        self.policy_engine = policy_engine or PolicyEngine()

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

        if tool_name == "sandbox.exec":
            return self._sandbox_exec(session, project_id, request, worker_id, intent_id, attempt_id)
        if tool_name == "fofa.search":
            return self._fofa_search(session, project_id, request, worker_id, intent_id, attempt_id)
        if tool_name == "blackboard.query":
            return self._blackboard_query(session, project_id, request, worker_id, intent_id, attempt_id)
        if tool_name == "browser.interact":
            return self._browser_interact(session, project_id, request, worker_id, intent_id, attempt_id)
        command = self._semantic_command(tool_name, request)
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
        payload = [{"id": fact.id, "statement": fact.statement, "confidence": fact.confidence, "evidence_refs": fact.evidence_refs} for fact in facts]
        artifact = self.artifact_store.write_text(session, project_id=project_id, source_attempt_id=attempt_id, content=json.dumps(payload, ensure_ascii=False, indent=2), summary=f"Blackboard query returned {len(payload)} fact(s)", artifact_type="blackboard-query")
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
            policy_decision="allow" if result.success else "deny",
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
        artifact = self.artifact_store.write_text(
            session,
            project_id=project_id,
            source_attempt_id=attempt_id,
            content=raw,
            summary=f"{tool_name} {completed.backend} exit={completed.exit_code}: {command[:120]}",
            artifact_type="terminal",
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
