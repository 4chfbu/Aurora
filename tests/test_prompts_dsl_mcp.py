from aurora.config import get_settings
from aurora.models import ContextSnapshot, Worker
from aurora.services.intent_dsl import IntentDSL
from aurora.services.mcp_registry import visible_mcp_tools
from aurora.services.prompt_renderer import PromptRenderer


def test_solver_prompt_assets_render_context_and_tool_contract() -> None:
    worker = Worker(id="worker_prompt", project_id="proj_prompt", intent_id="intent_prompt")
    snapshot = ContextSnapshot(
        id="ctx_prompt",
        project_id="proj_prompt",
        intent_id="intent_prompt",
        sections_json={"current_intent": {"objective": "Inspect the authorized target."}},
        section_metrics_json={},
        visible_tools_json=[{"name": "http.request"}],
        output_schema_json={"status": "success | partial | failed"},
    )

    renderer = PromptRenderer()
    messages = renderer.render_messages(worker=worker, snapshot=snapshot)
    task = renderer.render_codex_task(worker=worker, snapshot=snapshot)

    assert [message["role"] for message in messages] == ["system", "developer", "user"]
    assert "http.request" in messages[1]["content"]
    assert "Inspect the authorized target." in task
    assert "{{context_payload}}" not in task
    assert len(renderer.version_hash(worker=worker)) == 64


def test_intent_dsl_rejects_duplicate_capabilities() -> None:
    intent = IntentDSL(objective="Read a scoped blackboard view.", capabilities=["blackboard.query"], risk_level="low")
    assert intent.capabilities == ["blackboard.query"]

    try:
        IntentDSL(objective="invalid", capabilities=["http.request", "http.request"])
    except ValueError as exc:
        assert "capabilities must be unique" in str(exc)
    else:
        raise AssertionError("duplicate capability DSL should be rejected")


def test_fofa_tool_is_hidden_without_credentials(monkeypatch) -> None:
    monkeypatch.delenv("AURORA_FOFA_EMAIL", raising=False)
    monkeypatch.delenv("AURORA_FOFA_KEY", raising=False)
    get_settings.cache_clear()
    assert "fofa.search" not in {tool["name"] for tool in visible_mcp_tools(get_settings())}


def test_subagent_tool_is_only_visible_when_context_builder_enables_it(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_SUBAGENTS_ENABLED", "true")
    get_settings.cache_clear()
    visible = {tool["name"] for tool in visible_mcp_tools(get_settings(), allow_subagents=True)}
    hidden = {tool["name"] for tool in visible_mcp_tools(get_settings(), allow_subagents=False)}
    assert "subagent.spawn" in visible
    assert "subagent.spawn" not in hidden
