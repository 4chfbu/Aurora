import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from aurora.config import get_settings
from aurora.services.command_runner import CommandResult, CommandRunner, KaliContainerRunner
from aurora.services.artifact_store import ArtifactStore
from aurora.models import Artifact, Attempt, SQLModel, ToolTrace, Worker, WorkerEvent
from sqlmodel import Session, create_engine, select
import pytest

from aurora.services.worker_runtime import CodexHarnessRuntime, OpenAICompatibleRuntime, get_worker_runtime
from aurora.services.demo import _attempt_shell_artifact_refs


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


def test_worker_container_runs_as_workspace_owner(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "codex-workspaces" / "proj_test" / "worker_test"
    workspace.mkdir(parents=True)
    runner = KaliContainerRunner()
    runner.engine = "docker"

    command, _, _ = runner._build_command("id", workspace)

    user_index = command.index("--user")
    stat = workspace.stat()
    assert command[user_index + 1] == f"{stat.st_uid}:{stat.st_gid}"
    assert "HOME=/workspace/runtime/home" in command


def test_streaming_runner_enforces_soft_timeout(monkeypatch, tmp_path) -> None:
    runner = KaliContainerRunner()
    runner.engine = "test"
    command = ["bash", "-lc", "trap 'exit 0' INT; while true; do echo tick; sleep 0.1; done"]
    monkeypatch.setattr(runner, "_build_command", lambda *_: (command, Path("codex-workspaces/proj/worker"), Path("/workspace")))
    monkeypatch.setattr(runner, "_stop_container", lambda *_: None)

    result = runner.run_streaming(
        command="loop",
        cwd=tmp_path,
        timeout=5,
        soft_timeout=1,
        finalize_grace=1,
        on_output=lambda *_: None,
    )

    assert result.exit_code == 124
    assert result.failure_kind == "command_timed_out"
    assert result.finalization_reason == "soft_timeout"


def test_worker_image_check_reports_docker_permission_error(monkeypatch) -> None:
    runner = KaliContainerRunner(image="aurora-kali-codex:heavy")
    runner.engine = "docker"
    monkeypatch.setattr(
        "aurora.services.command_runner.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="permission denied while trying to connect to the docker API"),
    )

    assert runner.available() is False
    assert runner.availability_error is not None
    assert runner.availability_error.startswith("cannot access the container engine")


def test_worker_preflight_rejects_unhealthy_cc_switch(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_CODEX_PROXY_BASE_URL", "http://aurora-cc-switch:15723/v1")
    get_settings.cache_clear()
    runner = KaliContainerRunner()
    runner.engine = "docker"

    def fake_run(command, **_kwargs):
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        if command[:2] == ["docker", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout='{"Status": "restarting", "Health": {"Status": "unhealthy"}}',
                stderr="",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr("aurora.services.command_runner.subprocess.run", fake_run)

    assert runner.available() is False
    assert runner.availability_error == "CC Switch proxy is not ready: container_status=restarting, health=unhealthy"


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


def test_codex_harness_accepts_markdown_fenced_last_message(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    get_settings.cache_clear()
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    output_file = tmp_path / "aurora-last-message.json"
    payload = {
        "status": "success",
        "summary": "flag recovered",
        "decision_summary": {"selected_intent": "extract", "reason_summary": "done", "next_tool_plan": []},
    }
    output_file.write_text(f"```json\n{json.dumps(payload)}\n```", encoding="utf-8")
    snapshot = SimpleNamespace(sections_json={"current_intent": {"objective": "extract"}})

    output, diagnostic = runtime._parse_or_synthesize(
        output_file=output_file,
        stdout="",
        stderr="",
        exit_code=0,
        failure_kind=None,
        artifact_id="artifact_test",
        snapshot=snapshot,
    )

    assert output["summary"] == "flag recovered"
    assert diagnostic["source"] == "last_message_file"
    assert diagnostic["tolerant_json"] is True


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
        visible_tools_json=[{"name": "sandbox.exec"}, {"name": "flag.verify"}, {"name": "flag.submit"}],
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
                {"tool_name": "flag.submit", "request": {"candidate_id": "latest_verified"}},
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
    assert output["tool_requests"][1] == {
        "tool_name": "flag.submit",
        "request": {"candidate_id": "latest_verified"},
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


def test_codex_harness_classifies_reasoning_content_provider_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    get_settings.cache_clear()
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    snapshot = SimpleNamespace(sections_json={"current_intent": {"objective": "inspect"}})

    output, _ = runtime._parse_or_synthesize(
        output_file=tmp_path / "missing.json",
        stdout="",
        stderr="reasoning_content must be passed back when using the Responses API",
        exit_code=1,
        failure_kind=None,
        artifact_id="artifact_test",
        snapshot=snapshot,
    )

    assert output["failed_attempts"][0]["reason"] == "provider_reasoning_error"


def test_codex_harness_classifies_reasoning_content_provider_error_from_json_stdout(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    get_settings.cache_clear()
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    snapshot = SimpleNamespace(sections_json={"current_intent": {"objective": "inspect"}})
    stdout = (
        '{"type":"error","message":"The reasoning_content in the thinking mode '
        'must be passed back to the API."}\n'
        '{"type":"turn.failed"}\n'
    )

    output, _ = runtime._parse_or_synthesize(
        output_file=tmp_path / "missing.json",
        stdout=stdout,
        stderr="",
        exit_code=1,
        failure_kind=None,
        artifact_id="artifact_test",
        snapshot=snapshot,
    )

    assert output["failed_attempts"][0]["reason"] == "provider_reasoning_error"


def test_codex_harness_classifies_missing_model_metadata(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    get_settings.cache_clear()
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    snapshot = SimpleNamespace(sections_json={"current_intent": {"objective": "inspect"}})

    output, _ = runtime._parse_or_synthesize(
        output_file=tmp_path / "missing.json",
        stdout="",
        stderr="Model metadata not found; falling back to default metadata",
        exit_code=1,
        failure_kind=None,
        artifact_id="artifact_test",
        snapshot=snapshot,
    )

    assert output["failed_attempts"][0]["reason"] == "provider_model_metadata_missing"


def test_codex_harness_classifies_unavailable_cc_switch(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    get_settings.cache_clear()
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    snapshot = SimpleNamespace(sections_json={"current_intent": {"objective": "inspect"}})

    output, _ = runtime._parse_or_synthesize(
        output_file=tmp_path / "missing.json",
        stdout="",
        stderr="curl: (6) Could not resolve host: aurora-cc-switch\nAurora CC Switch proxy is unavailable",
        exit_code=69,
        failure_kind=None,
        artifact_id="artifact_test",
        snapshot=snapshot,
    )

    assert output["failed_attempts"][0]["reason"] == "provider_unavailable"


def test_native_shell_artifacts_are_promoted_to_result_evidence() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    worker = Worker(id="worker_shell_artifacts", project_id="proj_shell_artifacts", intent_id="intent_shell_artifacts")
    attempt = Attempt(
        id="attempt_shell_artifacts",
        project_id="proj_shell_artifacts",
        intent_id="intent_shell_artifacts",
        worker_id=worker.id,
    )
    session = Session(engine)
    session.add(worker)
    session.add(attempt)
    session.add_all([
        ToolTrace(
            id="trace_shell_first",
            project_id=attempt.project_id,
            worker_id=worker.id,
            intent_id=attempt.intent_id,
            attempt_id=attempt.id,
            tool_name="codex.shell",
            request_json={"command": "curl target"},
            artifact_refs=["artifact_target", "artifact_shared"],
        ),
        ToolTrace(
            id="trace_shell_second",
            project_id=attempt.project_id,
            worker_id=worker.id,
            intent_id=attempt.intent_id,
            attempt_id=attempt.id,
            tool_name="codex.shell",
            request_json={"command": "read evidence"},
            artifact_refs=["artifact_shared", "artifact_local"],
        ),
    ])
    session.commit()

    assert _attempt_shell_artifact_refs(session, attempt.id) == [
        "artifact_target",
        "artifact_shared",
        "artifact_local",
    ]
    session.close()


def test_codex_harness_bounds_transcript_by_bytes(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_CODEX_TRANSCRIPT_MAX_BYTES", "2048")
    get_settings.cache_clear()
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())

    bounded = runtime._bounded_transcript("start\n" + ("x" * 10000) + "\nend")

    assert len(bounded.encode("utf-8")) <= 2048
    assert bounded.startswith("start")
    assert bounded.endswith("end")
    assert "aurora transcript truncated" in bounded


def test_codex_transcript_redacts_worker_control_token() -> None:
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    result = CommandResult(
        command="codex exec",
        executed_command="docker run -e AURORA_WORKER_CONTROL_TOKEN=super-secret image",
        cwd="/workspace",
        stdout="",
        stderr="",
        exit_code=0,
        backend="test",
    )

    transcript = runtime._transcript("codex exec", result)

    assert "super-secret" not in transcript
    assert "AURORA_WORKER_CONTROL_TOKEN=<redacted>" in transcript


def test_codex_wrapper_avoids_unsupported_view_image_feature_flag() -> None:
    wrapper = Path("scripts/codex-via-cc-switch.sh").read_text(encoding="utf-8")

    assert "--disable view_image" not in wrapper


def test_codex_wrapper_streams_json_and_can_resume() -> None:
    wrapper = Path("scripts/codex-via-cc-switch.sh").read_text(encoding="utf-8")

    assert "--json" in wrapper
    assert "codex exec resume" in wrapper
    assert "AURORA_CODEX_RESUME_THREAD_ID" in wrapper
    assert 'AURORA_CODEX_MODEL_REASONING_EFFORT:-none' in wrapper
    assert 'model_reasoning_effort=' in wrapper


def test_cc_switch_entrypoint_is_restart_safe() -> None:
    entrypoint = Path("container/cc-switch/entrypoint.sh").read_text(encoding="utf-8")

    assert "provider delete" not in entrypoint
    assert "model_reasoning_effort = \"none\"" in entrypoint
    assert "--config-file" in entrypoint
    assert "--api-format chat" in entrypoint
    assert "provider list" in entrypoint
    assert "provider switch" in entrypoint


def test_codex_provider_preflight_exercises_a_tool_continuation() -> None:
    preflight = Path("scripts/check-codex-provider.sh").read_text(encoding="utf-8")

    assert "provider-multiturn-ok" in preflight
    assert 'model_reasoning_effort="none"' in preflight
    assert '"type":"turn.completed"' in preflight
    assert "reasoning_content.*must be passed back" in preflight


def test_codex_attempt_inherits_thread_and_records_progress(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    get_settings.cache_clear()
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())

    with Session(engine) as session:
        parent_worker = Worker(id="worker_parent", project_id="proj_resume", intent_id="intent_parent", status="COMPLETED")
        parent = Attempt(id="attempt_parent", project_id="proj_resume", intent_id="intent_parent", worker_id=parent_worker.id, status="FAILED", codex_thread_id="thread_test", resume_count=1)
        worker = Worker(id="worker_child", project_id="proj_resume", intent_id="intent_child", status="RUNNING")
        attempt = Attempt(id="attempt_child", project_id="proj_resume", intent_id="intent_child", worker_id=worker.id, parent_attempt_id=parent.id)
        session.add_all([parent_worker, parent, worker, attempt])
        session.commit()

        parent_home = tmp_path / "proj_resume" / parent_worker.id / "runtime" / "codex-home"
        parent_home.mkdir(parents=True)
        parent_home.joinpath("session.jsonl").write_text("persisted", encoding="utf-8")
        runtime.settings.codex_workspace_dir = tmp_path
        parent_workspace = tmp_path / "proj_resume" / parent_worker.id
        parent_workspace.joinpath("inputs").mkdir()
        parent_workspace.joinpath("inputs", "manifest.json").write_text("[]", encoding="utf-8")
        parent_workspace.joinpath("work").mkdir()
        runtime._persist_resume_manifest(session, attempt=parent, workspace=parent_workspace)
        child_workspace = tmp_path / "proj_resume" / worker.id
        thread_id = runtime._prepare_attempt(session, worker=worker, attempt=attempt, control_token="secret", workspace=child_workspace)
        runtime._record_codex_event(
            session,
            worker=worker,
            attempt=attempt,
            stream="stdout",
            line=json.dumps({"type": "thread.started", "thread_id": "thread_test"}),
        )
        session.refresh(attempt)
        events = session.exec(select(WorkerEvent).where(WorkerEvent.attempt_id == attempt.id)).all()

    assert thread_id == "thread_test"
    assert attempt.resume_count == 2
    assert attempt.codex_control_token_hash is not None
    assert attempt.codex_thread_id == "thread_test"
    assert (child_workspace / "runtime" / "codex-home" / "session.jsonl").read_text(encoding="utf-8") == "persisted"
    assert {event.event_type for event in events} == {"codex.resume_scheduled", "codex.session_started"}


def test_codex_resume_without_manifest_starts_new_thread(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    runtime.settings.codex_workspace_dir = tmp_path

    with Session(engine) as session:
        parent = Attempt(id="attempt_parent_missing", project_id="proj_resume_missing", intent_id="intent_parent", worker_id="worker_parent", status="PARTIAL", codex_thread_id="thread_old")
        worker = Worker(id="worker_child_missing", project_id=parent.project_id, intent_id="intent_child", status="RUNNING")
        attempt = Attempt(id="attempt_child_missing", project_id=parent.project_id, intent_id=worker.intent_id, worker_id=worker.id, parent_attempt_id=parent.id)
        session.add_all([parent, worker, attempt])
        session.commit()

        thread_id = runtime._prepare_attempt(session, worker=worker, attempt=attempt, control_token="secret", workspace=tmp_path / parent.project_id / worker.id)
        events = session.exec(select(WorkerEvent).where(WorkerEvent.attempt_id == attempt.id)).all()

    assert thread_id is None
    assert [event.event_type for event in events] == ["codex.resume_rejected"]
    assert events[0].payload_json["reason"] == "resume_manifest_missing"


def test_codex_resume_falls_back_after_invalid_parent_manifest(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    runtime = CodexHarnessRuntime(command_runner=NoopRunner())
    runtime.settings.codex_workspace_dir = tmp_path

    with Session(engine) as session:
        valid_worker = Worker(id="worker_valid_fallback", project_id="proj_resume_fallback", intent_id="intent_valid", status="COMPLETED")
        valid_parent = Attempt(
            id="attempt_valid_fallback",
            project_id=valid_worker.project_id,
            intent_id=valid_worker.intent_id,
            worker_id=valid_worker.id,
            status="PARTIAL",
            codex_thread_id="thread_valid_fallback",
            resume_count=2,
        )
        invalid_parent = Attempt(
            id="attempt_invalid_parent",
            project_id=valid_worker.project_id,
            intent_id="intent_invalid",
            worker_id="worker_invalid",
            status="FAILED",
            codex_thread_id="thread_invalid",
        )
        worker = Worker(id="worker_fallback_child", project_id=valid_worker.project_id, intent_id="intent_child", status="RUNNING")
        attempt = Attempt(
            id="attempt_fallback_child",
            project_id=worker.project_id,
            intent_id=worker.intent_id,
            worker_id=worker.id,
            parent_attempt_id=invalid_parent.id,
        )
        session.add_all([valid_worker, valid_parent, invalid_parent, worker, attempt])
        session.commit()

        valid_workspace = tmp_path / valid_parent.project_id / valid_worker.id
        valid_workspace.joinpath("inputs").mkdir(parents=True)
        valid_workspace.joinpath("inputs", "manifest.json").write_text("[]", encoding="utf-8")
        valid_workspace.joinpath("work").mkdir()
        valid_workspace.joinpath("runtime", "codex-home").mkdir(parents=True)
        valid_workspace.joinpath("runtime", "codex-home", "session.jsonl").write_text("persisted", encoding="utf-8")
        runtime._persist_resume_manifest(session, attempt=valid_parent, workspace=valid_workspace)

        child_workspace = tmp_path / worker.project_id / worker.id
        thread_id = runtime._prepare_attempt(
            session,
            worker=worker,
            attempt=attempt,
            control_token="secret",
            workspace=child_workspace,
        )
        events = session.exec(select(WorkerEvent).where(WorkerEvent.attempt_id == attempt.id)).all()
        selected_parent_attempt_id = attempt.parent_attempt_id
        valid_parent_id = valid_parent.id
        resume_count = attempt.resume_count

    assert thread_id == "thread_valid_fallback"
    assert selected_parent_attempt_id == valid_parent_id
    assert resume_count == 3
    assert [event.event_type for event in events] == [
        "codex.resume_rejected",
        "codex.resume_fallback_selected",
        "codex.resume_scheduled",
    ]


def test_codex_action_budget_enters_finalizing_state() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    worker = Worker(id="worker_budget", project_id="proj_budget", intent_id="intent_budget", status="RUNNING", budgets={"max_agent_actions": 1, "max_no_progress_actions": 0, "finalize_grace_seconds": 7})
    attempt = Attempt(id="attempt_budget", project_id=worker.project_id, intent_id=worker.intent_id, worker_id=worker.id)

    with Session(engine) as session:
        session.add_all([worker, attempt])
        session.commit()
        reason = CodexHarnessRuntime._record_codex_event(
            session,
            worker=worker,
            attempt=attempt,
            stream="stdout",
            line=json.dumps({
                "type": "item.completed",
                "item": {"type": "command_execution", "status": "completed", "command": "false", "exit_code": 1},
            }),
        )
        session.refresh(attempt)
        CodexHarnessRuntime._record_codex_event(
            session,
            worker=worker,
            attempt=attempt,
            stream="stdout",
            line=json.dumps({
                "type": "item.completed",
                "item": {"type": "command_execution", "status": "completed", "command": "false --again", "exit_code": 1},
            }),
        )
        events = session.exec(select(WorkerEvent).where(WorkerEvent.attempt_id == attempt.id)).all()

    assert reason == "action_budget_exhausted"
    assert attempt.status == "FINALIZING"
    assert attempt.finalization_reason == "action_budget_exhausted"
    assert {event.event_type for event in events} >= {"checkpoint.saved", "attempt.budget_enforced", "attempt.finalization_started"}
    assert sum(event.event_type == "attempt.action_budget_exhausted" for event in events) == 1
    assert sum(event.event_type == "attempt.finalization_started" for event in events) == 1


def test_codex_resume_skips_legacy_unreadable_files(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    source.joinpath("session.jsonl").write_text("resume state", encoding="utf-8")
    source.joinpath("config.toml").write_text("legacy root-only file", encoding="utf-8")
    real_access = __import__("os").access

    monkeypatch.setattr(
        "aurora.services.worker_runtime.os.access",
        lambda path, mode: False if Path(path).name == "config.toml" else real_access(path, mode),
    )

    skipped = CodexHarnessRuntime._copy_resume_home(source, target)

    assert skipped == ["config.toml"]
    assert target.joinpath("session.jsonl").read_text(encoding="utf-8") == "resume state"
    assert not target.joinpath("config.toml").exists()


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


def test_artifact_store_deduplicates_resume_files_within_project(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    source = tmp_path / "state.bin"
    source.write_bytes(b"stable resume state")
    store = ArtifactStore(tmp_path / "artifacts")
    with Session(engine) as session:
        first = store.write_file(session, project_id="project", source=source, summary="first", artifact_type="resume-work-file", deduplicate=True)
        second = store.write_file(session, project_id="project", source=source, summary="second", artifact_type="resume-work-file", deduplicate=True)
        assert second.id == first.id
        assert len(session.exec(select(Artifact)).all()) == 1


def test_codex_action_progress_comparison_accepts_sqlite_naive_timestamp() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    worker = Worker(
        id="worker_timezones",
        project_id="proj_timezones",
        intent_id="intent_timezones",
        budgets={"max_no_progress_actions": 3},
    )
    attempt = Attempt(
        id="attempt_timezones",
        project_id=worker.project_id,
        intent_id=worker.intent_id,
        worker_id=worker.id,
    )
    progress = WorkerEvent(
        project_id=worker.project_id,
        worker_id=worker.id,
        intent_id=worker.intent_id,
        attempt_id=attempt.id,
        event_type="checkpoint.saved",
        created_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
    )
    event = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "status": "completed",
            "command": "id",
            "exit_code": 0,
        },
    })

    with Session(engine) as session:
        session.add_all([worker, attempt, progress])
        session.commit()
        session.expire(progress)
        assert progress.created_at.tzinfo is None

        reason = CodexHarnessRuntime._record_codex_event(
            session,
            worker=worker,
            attempt=attempt,
            stream="stdout",
            line=event,
        )

        assert reason is None
        assert len(session.exec(select(ToolTrace).where(ToolTrace.attempt_id == attempt.id)).all()) == 1


def test_codex_shell_output_is_persisted_as_trusted_evidence(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    worker = Worker(
        id="worker_shell_evidence",
        project_id="proj_shell_evidence",
        intent_id="intent_shell_evidence",
    )
    attempt = Attempt(
        id="attempt_shell_evidence",
        project_id=worker.project_id,
        intent_id=worker.intent_id,
        worker_id=worker.id,
    )
    event = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "status": "completed",
            "command": "curl http://10.0.0.1/",
            "exit_code": 0,
            "aggregated_output": "flag{observed-in-shell-output}",
        },
    })

    with Session(engine) as session:
        session.add_all([worker, attempt])
        session.commit()
        reason = CodexHarnessRuntime._record_codex_event(
            session,
            worker=worker,
            attempt=attempt,
            stream="stdout",
            line=event,
            artifact_store=artifact_store,
        )
        traces = session.exec(select(ToolTrace).where(ToolTrace.attempt_id == attempt.id)).all()
        artifacts = session.exec(select(Artifact).where(Artifact.project_id == worker.project_id)).all()

    assert reason is None
    assert len(traces) == 1
    assert len(artifacts) == 1
    assert traces[0].artifact_refs == [artifacts[0].id]
    assert artifacts[0].origin_kind == "target_observation"
    assert artifacts[0].type == "terminal"
    assert (tmp_path / "artifacts" / worker.project_id / f"{artifacts[0].id}.txt").read_text(encoding="utf-8") == "flag{observed-in-shell-output}"
