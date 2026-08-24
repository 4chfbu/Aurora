from __future__ import annotations

from types import SimpleNamespace

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.db import engine
from aurora.models import AuthorizationScope, Project, ToolTrace, Worker, WorkerEvent
from aurora.services.capability_gateway import CapabilityGateway
from aurora.services.mcp_registry import visible_mcp_tools
from aurora.services.tool_contract import tools_for_runtime
from aurora.services.worker_runtime import CodexHarnessRuntime


def test_codex_native_contract_excludes_local_semantic_tools(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    monkeypatch.setenv("AURORA_TOOL_CONTRACT", "native_privileged")
    monkeypatch.setenv("AURORA_NATIVE_ALLOW_BLACKBOARD_QUERY", "true")
    get_settings.cache_clear()

    settings = get_settings()
    names = tools_for_runtime(settings)
    assert names is not None
    assert "sandbox.exec" not in names
    assert "binary.inspect" not in names
    assert "forensic.inspect" not in names
    assert "capability.request" not in names
    assert {
        "flag.verify",
        "flag.submit",
        "fofa.search",
        "browser.interact",
        "http.request",
        "network.scan",
        "web.enumerate",
        "blackboard.query",
    } <= names

    visible = {tool["name"] for tool in visible_mcp_tools(settings, allow_subagents=False)}
    assert "sandbox.exec" not in visible
    assert "capability.request" not in visible
    assert "flag.verify" in visible
    assert "flag.submit" in visible


def test_codex_kali_shell_contract_keeps_only_server_gates(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    monkeypatch.setenv("AURORA_TOOL_CONTRACT", "kali_shell")
    monkeypatch.setenv("AURORA_NATIVE_ALLOW_BLACKBOARD_QUERY", "false")
    get_settings.cache_clear()

    settings = get_settings()
    names = tools_for_runtime(settings)
    assert names is not None
    assert {
        "flag.verify",
        "flag.submit",
    } <= names
    assert "http.request" not in names
    assert "network.scan" not in names
    assert "web.enumerate" not in names
    assert "sandbox.exec" not in names
    assert "binary.inspect" not in names
    assert "forensic.inspect" not in names

    visible = {tool["name"] for tool in visible_mcp_tools(settings, allow_subagents=False)}
    assert "flag.verify" in visible
    assert "flag.submit" in visible
    assert "http.request" not in visible
    assert "network.scan" not in visible
    assert "web.enumerate" not in visible


def test_codex_native_contract_subagent_still_visible_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    monkeypatch.setenv("AURORA_TOOL_CONTRACT", "native_privileged")
    monkeypatch.setenv("AURORA_SUBAGENTS_ENABLED", "true")
    get_settings.cache_clear()

    visible = {tool["name"] for tool in visible_mcp_tools(get_settings(), allow_subagents=True)}
    hidden = {tool["name"] for tool in visible_mcp_tools(get_settings(), allow_subagents=False)}
    assert "subagent.spawn" in visible
    assert "subagent.spawn" not in hidden


def test_openai_direct_keeps_full_gateway_contract(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "openai_direct")
    monkeypatch.setenv("AURORA_TOOL_CONTRACT", "native_privileged")
    get_settings.cache_clear()

    settings = get_settings()
    assert tools_for_runtime(settings) is None
    visible = {tool["name"] for tool in visible_mcp_tools(settings)}
    assert {"sandbox.exec", "binary.inspect", "forensic.inspect", "capability.request"} <= visible


def test_full_gateway_override_restores_legacy_contract(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    monkeypatch.setenv("AURORA_TOOL_CONTRACT", "full_gateway")
    get_settings.cache_clear()

    settings = get_settings()
    assert tools_for_runtime(settings) is None
    visible = {tool["name"] for tool in visible_mcp_tools(settings)}
    assert "sandbox.exec" in visible


def test_codex_normalize_records_dropped_non_privileged_requests(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    get_settings.cache_clear()

    with Session(engine) as session:
        worker = Worker(project_id="proj_contract", intent_id="intent_contract")
        session.add(worker)
        session.commit()
        session.refresh(worker)

        snapshot = SimpleNamespace(
            project_id="proj_contract",
            intent_id="intent_contract",
            sections_json={"current_intent": {"objective": "verify"}},
            visible_tools_json=[{"name": "flag.verify"}, {"name": "http.request"}],
        )
        runtime = CodexHarnessRuntime()
        runtime._normalize(
            {
                "status": "partial",
                "summary": "ok",
                "decision_summary": {},
                "tool_requests": [
                    {"tool_name": "sandbox.exec", "request": {"command": "id"}},
                    {"tool_name": "flag.verify", "request": {}},
                ],
            },
            snapshot,
            "artifact_transcript",
            session=session,
            worker=worker,
        )

        events = session.exec(
            select(WorkerEvent).where(
                WorkerEvent.project_id == "proj_contract",
                WorkerEvent.event_type == "tool.skipped.non_privileged",
            )
        ).all()
        assert len(events) == 1
        assert events[0].payload_json["tool_names"] == ["sandbox.exec"]


def test_capability_request_is_denied_and_escalates() -> None:
    with Session(engine) as session:
        project = Project(name="capability-request", goal="deny stub")
        session.add(project)
        session.commit()
        session.refresh(project)
        session.add(AuthorizationScope(project_id=project.id, allowed_hosts=["127.0.0.1"]))
        session.commit()

        result = CapabilityGateway().execute(
            session,
            project_id=project.id,
            tool_name="capability.request",
            request={"capability": "target.url"},
        )

        assert result.success is False
        trace = session.get(ToolTrace, result.trace_id)
        assert trace is not None and trace.policy_decision == "deny"
        events = session.exec(
            select(WorkerEvent).where(
                WorkerEvent.project_id == project.id,
                WorkerEvent.event_type == "operator.escalated",
            )
        ).all()
        assert len(events) == 1
        assert events[0].payload_json["reason"] == "capability_request_denied"

import json

from aurora.services.artifact_store import ArtifactStore
from aurora.models import Intent
from aurora.services.context_builder import ContextBuilder


def test_import_mcp_events_records_observed_servers(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "runtime").mkdir(parents=True)
    (workspace / "runtime" / "mcp-events.jsonl").write_text(
        "\n".join([
            json.dumps({"server": "aurora_blackboard", "tool": "query", "success": True, "request": {}, "summary": "ok", "duration_ms": 10}),
            json.dumps({"server": "aurora_reverse", "tool": "open_binary", "success": True, "request": {}, "summary": "ok", "duration_ms": 10}),
        ]),
        encoding="utf-8",
    )

    with Session(engine) as session:
        worker = Worker(project_id="proj_mcp", intent_id="intent_mcp")
        session.add(worker)
        session.commit()
        session.refresh(worker)

        runtime = CodexHarnessRuntime(artifact_store=ArtifactStore(base_dir=tmp_path))
        snapshot = SimpleNamespace(project_id="proj_mcp")
        result = runtime._import_mcp_events(session, worker=worker, snapshot=snapshot, workspace=workspace)

        assert result["calls"] == 2
        assert result["servers_used"] == ["aurora_blackboard", "aurora_reverse"]
        events = session.exec(
            select(WorkerEvent).where(WorkerEvent.event_type == "mcp.server.observed")
        ).all()
        assert len(events) == 1
        assert set(events[0].payload_json["servers"]) == {"aurora_blackboard", "aurora_reverse"}


def test_context_builder_filters_removed_capability_tags(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    monkeypatch.setenv("AURORA_TOOL_CONTRACT", "native_privileged")
    monkeypatch.setenv("AURORA_NATIVE_ALLOW_BLACKBOARD_QUERY", "true")
    get_settings.cache_clear()

    with Session(engine) as session:
        project = Project(name="tag-filter", goal="filter removed tags", challenge_type="web")
        session.add(project)
        session.commit()
        session.refresh(project)
        session.add(AuthorizationScope(project_id=project.id, allowed_hosts=["10.0.0.1"]))
        intent = Intent(
            project_id=project.id,
            objective="Inspect target then continue.",
            capability_tags=["blackboard.query", "sandbox.exec", "http.request"],
            risk_level="low",
        )
        session.add(intent)
        session.commit()
        session.refresh(intent)

        snapshot = ContextBuilder().build(session, project_id=project.id, intent_id=intent.id)
        assert snapshot.sections_json["current_intent"]["capability_tags"] == ["blackboard.query", "http.request"]
        assert snapshot.sections_json["current_intent"]["capability_tags"] != intent.capability_tags
        assert snapshot.sections_json["solver_playbook"]["challenge_type"] == "web"
        assert snapshot.sections_json["solver_playbook"]["first_steps"]
