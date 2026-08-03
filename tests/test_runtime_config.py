import json
from pathlib import Path
from types import SimpleNamespace

from aurora.config import get_settings
from aurora.services.command_runner import CommandResult, CommandRunner, KaliContainerRunner
import pytest

from aurora.services.worker_runtime import CodexHarnessRuntime, OpenAICompatibleRuntime, get_worker_runtime


def test_default_worker_runtime_is_codex(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    get_settings.cache_clear()
    assert isinstance(get_worker_runtime(), CodexHarnessRuntime)


def test_unknown_worker_runtime_fails_loudly(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "unknown")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="unsupported AURORA_WORKER_RUNTIME"):
        get_worker_runtime()


def test_worker_container_receives_proxy_placeholder_not_real_key(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_LLM_API_KEY", "real-secret-key")
    monkeypatch.setenv("AURORA_CODEX_PROXY_BASE_URL", "http://aurora-cc-switch:15723/v1")
    monkeypatch.setenv("AURORA_LLM_MODEL", "test-model")
    get_settings.cache_clear()

    args = KaliContainerRunner()._env_args()
    rendered = " ".join(args)

    assert "real-secret-key" not in rendered
    assert "OPENAI_API_KEY=aurora-proxy-placeholder" in args
    assert "OPENAI_BASE_URL=http://aurora-cc-switch:15723/v1" in args
    assert "OPENAI_MODEL=test-model" in args


def test_openai_worker_runtime_selected_by_env(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "openai_direct")
    monkeypatch.setenv("AURORA_LLM_API_KEY", "test-key")
    monkeypatch.setenv("AURORA_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("AURORA_LLM_MODEL", "test-model")
    get_settings.cache_clear()
    runtime = get_worker_runtime()
    assert isinstance(runtime, OpenAICompatibleRuntime)
    assert runtime.model == "test-model"


def test_codex_harness_runtime_selected_by_env(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    monkeypatch.setenv("AURORA_CODEX_COMMAND_TEMPLATE", "codex exec {prompt_filename}")
    get_settings.cache_clear()
    runtime = get_worker_runtime()
    assert isinstance(runtime, CodexHarnessRuntime)


def test_model_roles_fall_back_and_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_LLM_MODEL", "default-model")
    monkeypatch.setenv("AURORA_PLANNER_MODEL", "fast-model")
    monkeypatch.setenv("AURORA_SOLVER_MODEL", "strong-model")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.model_for_role("planner") == "fast-model"
    assert settings.model_for_role("solver") == "strong-model"
    assert settings.model_for_role("unknown") == "default-model"


def test_codex_harness_renders_prompt_filename_for_worker_container(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    monkeypatch.setenv("AURORA_CODEX_COMMAND_TEMPLATE", "codex exec {prompt_filename}")
    get_settings.cache_clear()
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    prompt_file = Path.cwd() / "codex-workspaces" / "proj_test" / "worker_test" / "aurora-intent.md"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text("test", encoding="utf-8")
    assert runtime._render_command(prompt_file) == "codex exec aurora-intent.md"


def test_codex_harness_prefers_last_message_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    get_settings.cache_clear()
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    output_file = tmp_path / "aurora-last-message.json"
    output_file.write_text(json.dumps({"status": "success", "summary": "from file", "decision_summary": {"selected_intent": "test", "reason_summary": "ok", "next_tool_plan": []}}), encoding="utf-8")
    snapshot = SimpleNamespace(sections_json={"current_intent": {"objective": "test"}})

    output, diagnostic = runtime._parse_or_synthesize(
        output_file=output_file,
        stdout="not json",
        stderr="",
        exit_code=0,
        failure_kind=None,
        artifact_id="artifact_test",
        snapshot=snapshot,
    )

    assert output["summary"] == "from file"
    assert diagnostic["source"] == "last_message_file"


def test_codex_harness_classifies_timeout_without_output(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    get_settings.cache_clear()
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    snapshot = SimpleNamespace(sections_json={"current_intent": {"objective": "test"}})

    output, diagnostic = runtime._parse_or_synthesize(
        output_file=tmp_path / "missing.json",
        stdout="",
        stderr="timed out",
        exit_code=124,
        failure_kind="command_timed_out",
        artifact_id="artifact_test",
        snapshot=snapshot,
    )

    assert "command_timed_out" in output["summary"]
    assert diagnostic["output_file_exists"] is False


class NoopRunner(CommandRunner):
    def run(self, *, command: str, cwd: Path, timeout: int) -> CommandResult:
        return CommandResult(
            command=command,
            executed_command=command,
            cwd=str(cwd),
            stdout='{"status":"partial","summary":"noop","tool_requests":[]}',
            stderr="",
            exit_code=0,
            backend="test",
        )
