from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

from sqlmodel import Session, select

from aurora.models import Artifact, Attempt, Fact, Project, ToolTrace, WorkerEvent
from aurora.services.artifact_store import ArtifactStore
from aurora.services.blackboard_repository import normalize_fact_category, route_fingerprint
from aurora.services.evidence_context import current_environment_id
from aurora.services.flag_validator import FlagValidator


TRANSPORT_FAILURE = re.compile(
    r"connection (?:timed out|refused)|failed to connect|could not resolve host|network is unreachable|"
    r"no route to host|connect(?:ion)? timeout|operation timed out[^\n]*\b0 bytes|100% packet loss|\b\d+/tcp\s+filtered",
    re.IGNORECASE,
)
NON_PROGRESS_CATEGORIES = {"blocker", "target_unreachable", "route_exhausted", "infrastructure"}


def is_sync_request(trace: ToolTrace) -> bool:
    if trace.tool_name in {"blackboard.query", "mcp.aurora_blackboard.query", "mcp.aurora_blackboard.read_artifact"}:
        return True
    command = str((trace.request_json or {}).get("command") or trace.command or "")
    parts = [part.strip() for part in re.split(r"&&|\|\||[;\n]", command) if part.strip()]
    return trace.tool_name == "codex.shell" and bool(parts) and all(re.fullmatch(
        r"(?:cat|head|tail|jq)\s+[^;&|\n]*(?:runtime/blackboard\.json|inputs/manifest\.json)[\s'\"]*(?:\|\s*(?:head|tail)\s+(?:-[cn]\s*)?\d+\s*)*", part,
    ) for part in parts)


def transport_failed(trace: ToolTrace) -> bool:
    return bool(TRANSPORT_FAILURE.search(f"{trace.summary or ''}\n{trace.stderr_summary or ''}"))


def evidence_progress_counts(session: Session, project_id: str) -> tuple[int, int]:
    traces = session.exec(select(ToolTrace).where(ToolTrace.project_id == project_id)).all()
    by_artifact = {ref: trace for trace in traces for ref in trace.artifact_refs}
    artifacts = session.exec(select(Artifact).where(
        Artifact.project_id == project_id,
        Artifact.origin_kind.in_(["challenge_input", "target_observation", "operator_observation", "worker_observation", "verified_derivation"]),
    )).all()
    validator = FlagValidator()
    environment = current_environment_id(session, project_id)
    cache = session.info.setdefault("progress_evidence_cache", {})
    if len(cache) > 2048:
        cache.clear()
    evidence: dict[str, str] = {}
    for artifact in artifacts:
        trace = by_artifact.get(artifact.id)
        if trace and (trace.exit_code not in (None, 0) or is_sync_request(trace) or transport_failed(trace)):
            continue
        try:
            stat = Path(artifact.path).stat()
        except OSError:
            continue
        cache_key = (artifact.id, artifact.sha256, artifact.origin_kind, (artifact.evidence_context or {}).get("environment_id"), environment, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        current = cache.get(cache_key)
        if current is None or artifact.origin_kind == "verified_derivation":
            current = validator.is_current_evidence(session, artifact)
            cache[cache_key] = current
        if not current:
            continue
        digest = artifact.sha256
        if artifact.type == "terminal":
            content = validator._scannable_content(artifact, ArtifactStore().read_text(artifact, max_bytes=128_000)).strip()
            if not content or TRANSPORT_FAILURE.search(content):
                continue
            digest = hashlib.sha256(content.encode()).hexdigest()
        evidence[artifact.id] = digest
    facts = session.exec(select(Fact).where(Fact.project_id == project_id, Fact.status == "ACTIVE")).all()
    fact_keys = set()
    for fact in facts:
        category = normalize_fact_category(fact.category)
        refs = tuple(sorted({evidence[ref] for ref in fact.evidence_refs if ref in evidence}))
        if refs and category not in NON_PROGRESS_CATEGORIES:
            fact_keys.add((category, refs))
    return len(fact_keys), len(set(evidence.values()))


def target_transport_failed(session: Session, project_id: str) -> bool:
    project = session.get(Project, project_id)
    host = urlparse(project.target_url or "").hostname if project else None
    if not host:
        return False
    environment = current_environment_id(session, project_id)
    recovered = session.exec(select(WorkerEvent).where(
        WorkerEvent.project_id == project_id, WorkerEvent.event_type == "target.transport_recovered",
    ).order_by(WorkerEvent.created_at.desc())).first()
    statement = select(ToolTrace).where(ToolTrace.project_id == project_id)
    if recovered:
        statement = statement.where(ToolTrace.created_at > recovered.created_at)
    matching = []
    for trace in session.exec(statement.order_by(ToolTrace.created_at.desc()).limit(30)).all():
        request = trace.request_json or {}
        target = str(request.get("url") or request.get("command") or trace.command or "")
        if host not in target or is_sync_request(trace):
            continue
        if trace.tool_name == "codex.shell" and not re.search(r"\b(?:curl|wget|nc|netcat|nmap)\b", target):
            continue
        attempt = session.get(Attempt, trace.attempt_id) if trace.attempt_id else None
        if environment and (attempt is None or attempt.environment_id != environment):
            continue
        matching.append(trace)
        if len(matching) == 2:
            break
    return len(matching) == 2 and all(transport_failed(trace) for trace in matching)


def repeated_experiments(traces: list[ToolTrace]) -> int:
    counts: dict[str, int] = {}
    for trace in traces:
        if is_sync_request(trace):
            continue
        key = f"{trace.tool_name}:{route_fingerprint(trace.request_json)}"
        counts[key] = counts.get(key, 0) + 1
    return sum(max(0, count - 1) for count in counts.values())
