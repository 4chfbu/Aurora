import json
from pathlib import Path
from types import SimpleNamespace

from aurora.config import get_settings
from aurora.services.command_runner import CommandResult, CommandRunner, KaliContainerRunner
from aurora.services.artifact_store import ArtifactStore
from aurora.models import Artifact, SQLModel, ToolTrace, Worker
from sqlmodel import Session, create_engine, select
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


def test_worker_container_environment_override_selects_role_model(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_LLM_MODEL", "default-model")
    get_settings.cache_clear()

    args = KaliContainerRunner(environment_overrides={
        "OPENAI_MODEL": "solver-model",
        "AURORA_CODEX_MODEL_CONTEXT_WINDOW": "1000000",
    })._env_args()

    assert "OPENAI_MODEL=solver-model" in args
    assert "OPENAI_MODEL=default-model" not in args
    assert "AURORA_CODEX_MODEL_CONTEXT_WINDOW=1000000" in args


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


def test_codex_harness_normalizes_parameters_tool_payload(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    get_settings.cache_clear()
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    snapshot = SimpleNamespace(
        sections_json={"current_intent": {"objective": "verify"}},
        visible_tools_json=[{"name": "flag.verify"}],
    )

    output = runtime._normalize(
        {
            "status": "partial",
            "summary": "ready",
            "decision_summary": {},
            "tool_requests": [{
                "tool_name": "flag.verify",
                "parameters": {
                    "source_artifact_refs": ["artifact_input"],
                    "verification_script": "/workspace/runtime/verify_flag.py",
                },
            }],
        },
        snapshot,
        "artifact_transcript",
    )

    assert output["tool_requests"] == [{
        "tool_name": "flag.verify",
        "request": {
            "source_artifact_refs": ["artifact_input"],
            "verification_script": "/workspace/runtime/verify_flag.py",
        },
    }]


def test_codex_harness_normalizes_arguments_tool_payload(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    get_settings.cache_clear()
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    snapshot = SimpleNamespace(
        sections_json={"current_intent": {"objective": "verify"}},
        visible_tools_json=[{"name": "flag.verify"}],
    )

    output = runtime._normalize(
        {
            "status": "partial",
            "summary": "ready",
            "decision_summary": {},
            "tool_requests": [{
                "tool_name": "flag.verify",
                "arguments": {
                    "source_artifact_refs": ["artifact_input"],
                    "verification_script": "/workspace/verify_flag.py",
                },
            }],
        },
        snapshot,
        "artifact_transcript",
    )

    assert output["tool_requests"][0]["request"] == {
        "source_artifact_refs": ["artifact_input"],
        "verification_script": "/workspace/verify_flag.py",
    }


def test_codex_harness_normalizes_params_and_prioritizes_flag_verify(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    get_settings.cache_clear()
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    snapshot = SimpleNamespace(
        sections_json={"current_intent": {"objective": "verify"}},
        visible_tools_json=[{"name": "sandbox.exec"}, {"name": "flag.verify"}],
    )

    output = runtime._normalize(
        {
            "status": "partial",
            "summary": "ready",
            "decision_summary": {},
            "tool_requests": [
                {"tool_name": "sandbox.exec", "request": {"command": "true"}},
                {"tool_name": "sandbox.exec", "request": {"command": "pwd"}},
                {"tool_name": "sandbox.exec", "request": {"command": "id"}},
                {
                    "tool_name": "flag.verify",
                    "params": {
                        "source_artifact_refs": ["artifact_input"],
                        "verification_script": "/workspace/verify_flag.py",
                    },
                },
            ],
        },
        snapshot,
        "artifact_transcript",
    )

    assert output["tool_requests"][0] == {
        "tool_name": "flag.verify",
        "request": {
            "source_artifact_refs": ["artifact_input"],
            "verification_script": "/workspace/verify_flag.py",
        },
    }
    assert len(output["tool_requests"]) == 3


def test_codex_harness_drops_missing_and_unknown_tool_names(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    get_settings.cache_clear()
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    snapshot = SimpleNamespace(
        sections_json={"current_intent": {"objective": "inspect"}},
        visible_tools_json=[{"name": "sandbox.exec"}],
    )

    output = runtime._normalize(
        {
            "status": "success",
            "summary": "done",
            "tool_requests": [
                {"tool_name": None, "request": {}},
                {"tool_name": "unknown.tool", "request": {}},
            ],
        },
        snapshot,
        "artifact_transcript",
    )

    assert output["tool_requests"] == []


def test_codex_harness_classifies_context_length_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    get_settings.cache_clear()
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    snapshot = SimpleNamespace(sections_json={"current_intent": {"objective": "inspect"}})
    stderr = "This model's maximum context length is 100 tokens. However, you requested 200 tokens."

    output, diagnostic = runtime._parse_or_synthesize(
        output_file=tmp_path / "missing.json",
        stdout="",
        stderr=stderr,
        exit_code=1,
        failure_kind=None,
        artifact_id="artifact_test",
        snapshot=snapshot,
    )

    assert output["failed_attempts"][0]["reason"] == "context_length_exceeded"
    assert diagnostic["output_file_exists"] is False


def test_codex_harness_bounds_transcript_by_bytes(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_CODEX_TRANSCRIPT_MAX_BYTES", "2048")
    get_settings.cache_clear()
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())

    bounded = runtime._bounded_transcript("start\n" + ("x" * 10000) + "\nend")

    assert len(bounded.encode("utf-8")) <= 2048
    assert bounded.startswith("start")
    assert bounded.endswith("end")
    assert "aurora transcript truncated" in bounded


def test_codex_wrapper_avoids_unsupported_view_image_feature_flag() -> None:
    wrapper = Path("scripts/codex-via-cc-switch.sh").read_text(encoding="utf-8")

    assert "--disable view_image" not in wrapper


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
    assert "whole solver process" in output["summary"]
    assert output["suggested_intents"]
    assert diagnostic["output_file_exists"] is False


def test_codex_harness_recovers_complete_result_from_stderr_after_timeout(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    get_settings.cache_clear()
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    snapshot = SimpleNamespace(
        sections_json={"current_intent": {"objective": "recover"}},
        visible_tools_json=[],
    )
    result = {
        "status": "partial",
        "summary": "preserved before timeout",
        "decision_summary": {
            "selected_intent": "recover",
            "reason_summary": "evidence retained",
            "next_tool_plan": [],
        },
    }

    output, diagnostic = runtime._parse_or_synthesize(
        output_file=tmp_path / "missing.json",
        stdout="",
        stderr=f"progress log\n{json.dumps(result)}\nprocess still running",
        exit_code=124,
        failure_kind="command_timed_out",
        artifact_id="artifact_test",
        snapshot=snapshot,
    )

    assert output["summary"] == "preserved before timeout"
    assert output["status"] == "partial"
    assert diagnostic["source"] == "stderr_fallback"
    assert "artifact_test" in output["artifact_refs"]


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


def test_codex_runtime_imports_local_mcp_events(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    workspace = tmp_path / "worker"
    event_dir = workspace / "runtime"
    event_dir.mkdir(parents=True)
    event_dir.joinpath("mcp-events.jsonl").write_text(
        '\n'.join([
            json.dumps({"server": "aurora_reverse", "tool": "open_binary", "request": {"path": "/workspace/inputs/test"}, "success": True, "summary": "opened", "duration_ms": 12}),
            "not-json",
        ]),
        encoding="utf-8",
    )
    runtime = CodexHarnessRuntime(artifact_store=ArtifactStore(tmp_path / "artifacts"), command_runner=NoopRunner())
    worker = Worker(id="worker_mcp", project_id="proj_mcp", intent_id="intent_mcp")
    snapshot = SimpleNamespace(project_id="proj_mcp")
    with Session(engine) as session:
        result = runtime._import_mcp_events(session, worker=worker, snapshot=snapshot, workspace=workspace)
        traces = session.exec(select(ToolTrace)).all()
        artifacts = session.exec(select(Artifact)).all()
    assert result["calls"] == 1
    assert result["invalid_lines"] == 1
    assert traces[0].tool_name == "mcp.aurora_reverse.open_binary"
    assert traces[0].request_json["duration_ms"] == 12
    assert artifacts[0].type == "mcp-tool-log"
