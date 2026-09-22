import json
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.db import engine
from aurora.models import Artifact, Attempt, AttemptCheckpoint, ChallengeGroupItem, Fact, Intent, Project, Worker, WorkerEvent, now_utc
from aurora.services.artifact_store import ArtifactStore
from aurora.services.context_builder import ContextBuilder
from aurora.services.round_summary import RoundReflectionService
from aurora.services.worker_control import WorkerControlService
from aurora.services.worker_runtime import CodexHarnessRuntime


def _branch(session):
    project = Project(name="branch memory", goal="continue the original experiment")
    parent_intent = Intent(project_id=project.id, objective="original route")
    parent = Attempt(project_id=project.id, intent_id=parent_intent.id, worker_id="parent", status="PARTIAL")
    intent = Intent(project_id=project.id, objective="finish the route", parent_intent_id=parent_intent.id)
    worker = Worker(project_id=project.id, intent_id=intent.id)
    attempt = Attempt(project_id=project.id, intent_id=intent.id, worker_id=worker.id, parent_attempt_id=parent.id)
    fact = Fact(project_id=project.id, statement="The original experiment isolated the key", source_attempt_id=parent.id)
    checkpoint = AttemptCheckpoint(project_id=project.id, intent_id=parent_intent.id, attempt_id=parent.id, summary="original handoff", fact_refs=[fact.id], failed_routes=["Do not retry the disproved encoding"], next_steps=["Validate the isolated key"])
    session.add_all([project, parent_intent, parent, intent, worker, attempt, fact, checkpoint])
    session.commit()
    for index in range(60):
        session.add(Fact(project_id=project.id, statement=f"unrelated peer fact {index}"))
        session.add(AttemptCheckpoint(project_id=project.id, intent_id="peer", attempt_id=f"peer_{index}", summary=f"peer checkpoint {index}"))
        session.add(WorkerEvent(project_id=project.id, attempt_id=f"peer_{index}", event_type="checkpoint.saved", payload_json={"next_step": "unrelated peer step"}))
    session.commit()
    return project, intent, worker, attempt, parent, fact, checkpoint


def test_context_follows_actual_parent_beyond_recent_window():
    with Session(engine) as session:
        project, intent, worker, attempt, parent, fact, checkpoint = _branch(session)
        snapshot = ContextBuilder().build(session, project_id=project.id, intent_id=intent.id, worker_id=worker.id)
        assert snapshot.sections_json["recent_checkpoints"][0]["id"] == checkpoint.id
        assert fact.id in {entry["id"] for entry in snapshot.sections_json["facts"]}
        assert snapshot.sections_json["session_handoff"]["parent_attempt_id"] == parent.id


def test_live_blackboard_keeps_branch_memory_and_latest_branch_checkpoint():
    with Session(engine) as session:
        project, intent, worker, attempt, parent, fact, checkpoint = _branch(session)
        session.add(WorkerEvent(project_id=project.id, attempt_id=parent.id, event_type="checkpoint.saved", created_at=now_utc() - timedelta(hours=1), payload_json={"next_step": "branch-specific next step"}))
        session.commit()
        board = WorkerControlService().query(session, worker=worker, attempt=attempt)
        assert board["checkpoints"][0]["id"] == checkpoint.id
        assert fact.id in {entry["id"] for entry in board["facts"]}
        assert board["live_checkpoints"][0]["next_step"] == "branch-specific next step"


def test_checkpoint_parent_is_own_branch_instead_of_latest_peer():
    with Session(engine) as session:
        project, intent, worker, attempt, parent, fact, checkpoint = _branch(session)
        attempt.status = "PARTIAL"
        session.add(attempt)
        session.commit()
        created = RoundReflectionService().create(session, attempt=attempt, output={"summary": "continue original branch"}, budget={}, skip_planner=True, author_intents=False)
        assert created.parent_checkpoint_id == checkpoint.id


def test_context_byte_budget_preserves_dependencies_and_archives_full_memory(tmp_path):
    settings = get_settings()
    settings.artifact_dir = tmp_path / "artifacts"
    settings.debug.max_context_snapshot_bytes = 16_000
    with Session(engine) as session:
        project = Project(name="bounded memory", goal="关键题目信息" * 20_000)
        intent = Intent(project_id=project.id, objective="use every required fact")
        facts = [Fact(project_id=project.id, statement=f"dependency {index}: " + "证据" * 100) for index in range(12)]
        intent.dependency_fact_ids = [fact.id for fact in facts]
        session.add_all([project, intent, *facts])
        session.commit()
        snapshot = ContextBuilder().build(session, project_id=project.id, intent_id=intent.id)
        actual_bytes = len(json.dumps(snapshot.sections_json, ensure_ascii=False).encode())
        assert actual_bytes <= settings.debug.max_context_snapshot_bytes
        assert {fact.id for fact in facts} <= {entry["id"] for entry in snapshot.sections_json["facts"]}
        memory = snapshot.sections_json["context_memory"]
        archive = session.get(Artifact, memory["artifact_id"])
        assert archive.project_id == project.id and archive.origin_kind == "runtime_state"
        full = json.loads(ArtifactStore().read_text(archive, max_bytes=archive.size))
        assert full["project_goal"] == project.goal
        assert snapshot.truncation_report_json["kept_bytes"] == actual_bytes
        workspace = tmp_path / "worker"
        workspace.mkdir()
        CodexHarnessRuntime._materialize_project_inputs(session, snapshot, workspace)
        assert json.loads((workspace / memory["path"]).read_text())["project_goal"] == project.goal


def _resumable(session, tmp_path, *, with_input=False):
    runtime = CodexHarnessRuntime(artifact_store=ArtifactStore(tmp_path / "artifacts"))
    runtime.settings.codex_workspace_dir = tmp_path / "workers"
    project = Project(name="durable memory", goal="restore after cleanup")
    parent = Attempt(project_id=project.id, intent_id="original", worker_id="parent", status="PARTIAL", codex_thread_id="original_thread")
    worker = Worker(project_id=project.id, intent_id="continuation")
    attempt = Attempt(project_id=project.id, intent_id=worker.intent_id, worker_id=worker.id, parent_attempt_id=parent.id)
    session.add_all([project, parent, worker, attempt])
    session.commit()
    source = runtime.settings.codex_workspace_dir / project.id / parent.worker_id
    (source / "inputs").mkdir(parents=True)
    (source / "work").mkdir()
    (source / "work" / "solve.py").write_text("print('retained algorithm')\n")
    (source / "runtime" / "codex-home" / "sessions").mkdir(parents=True)
    (source / "runtime" / "codex-home" / "sessions" / "thread.jsonl").write_text('{"type":"retained conversation"}\n')
    inputs = []
    if with_input:
        evidence = runtime.artifact_store.write_text(session, project_id=project.id, content="required old input", summary="old observation", origin_kind="worker_observation")
        (source / "inputs" / "original.txt").write_text("required old input")
        inputs = [{"artifact_id": evidence.id, "path": "inputs/original.txt", "sha256": evidence.sha256}]
    (source / "inputs" / "manifest.json").write_text(json.dumps(inputs))
    manifest = runtime._persist_resume_manifest(session, attempt=parent, workspace=source)
    target = runtime.settings.codex_workspace_dir / project.id / worker.id
    return runtime, parent, worker, attempt, source, target, manifest


def test_resume_survives_parent_workspace_cleanup(tmp_path):
    with Session(engine) as session:
        runtime, parent, worker, attempt, source, target, manifest = _resumable(session, tmp_path)
        shutil.rmtree(source)
        thread = runtime._prepare_attempt(session, worker=worker, attempt=attempt, control_token="test", workspace=target)
        assert thread == "original_thread"
        assert (target / "work" / "solve.py").read_text() == "print('retained algorithm')\n"
        assert (target / "runtime" / "codex-home" / "sessions" / "thread.jsonl").read_text() == '{"type":"retained conversation"}\n'


def test_resume_ignores_volatile_executable_symlinks(tmp_path):
    with Session(engine) as session:
        runtime, parent, worker, attempt, source, target, manifest = _resumable(session, tmp_path)
        temporary = source / "runtime" / "codex-home" / "tmp" / "arg0"
        temporary.mkdir(parents=True)
        (temporary / "codex-execve-wrapper").symlink_to("/missing/container/binary")
        runtime._persist_resume_manifest(session, attempt=parent, workspace=source)
        assert runtime._prepare_attempt(session, worker=worker, attempt=attempt, control_token="test", workspace=target) == "original_thread"
        assert not (target / "runtime" / "codex-home" / "tmp").exists()


def test_overflowing_downloads_restore_key_script_as_partial_work_state(tmp_path):
    with Session(engine) as session:
        runtime, parent, worker, attempt, source, target, manifest = _resumable(session, tmp_path)
        runtime.settings.resume_max_files = 3
        for index in range(6):
            (source / "work" / f"asset-{index}.js").write_text(f"asset {index}")
        manifest = runtime._persist_resume_manifest(session, attempt=parent, workspace=source)
        payload = json.loads(Path(manifest.path).read_text())
        assert not payload["work_state_complete"]
        assert payload["work_files_omitted"] == 4
        shutil.rmtree(source)
        assert runtime._prepare_attempt(session, worker=worker, attempt=attempt, control_token="test", workspace=target) is None
        assert (target / "work" / "solve.py").is_file()
        restored = session.exec(select(WorkerEvent).where(WorkerEvent.attempt_id == attempt.id, WorkerEvent.event_type == "codex.work_state_restored")).one()
        assert restored.payload_json["partial_work_state"] is True
        assert restored.payload_json["work_files_omitted"] == 4


def test_resume_rehydrates_inputs_that_fell_out_of_recent_window(tmp_path):
    with Session(engine) as session:
        runtime, parent, worker, attempt, source, target, manifest = _resumable(session, tmp_path, with_input=True)
        thread = runtime._prepare_attempt(session, worker=worker, attempt=attempt, control_token="test", workspace=target)
        assert thread == "original_thread"
        assert (target / "inputs" / "original.txt").read_text() == "required old input"
        entries = json.loads((target / "inputs" / "manifest.json").read_text())
        assert entries[0]["path"] == "inputs/original.txt"


def test_failed_restore_preserves_existing_new_workspace(tmp_path):
    with Session(engine) as session:
        runtime, parent, worker, attempt, source, target, manifest = _resumable(session, tmp_path, with_input=True)
        payload = json.loads(ArtifactStore().read_text(manifest))
        artifact = session.get(Artifact, payload["work_files"][0]["artifact_id"])
        Path(artifact.path).write_text("corrupted content")
        (target / "work").mkdir(parents=True)
        (target / "work" / "current.txt").write_text("current worker data")
        valid, diagnostic = runtime._restore_resume_manifest(session, parent=parent, workspace=target)
        assert not valid
        assert (target / "work" / "current.txt").read_text() == "current worker data"
        assert not (target / "work" / "solve.py").exists()


@pytest.mark.parametrize("recorded_environment", ["current_instance", "previous_instance"])
def test_environment_change_starts_fresh_thread_with_explicit_handoff(tmp_path, recorded_environment):
    with Session(engine) as session:
        runtime, parent, worker, attempt, source, target, manifest = _resumable(session, tmp_path)
        parent.environment_id = "previous_instance"
        attempt.environment_id = recorded_environment
        item = ChallengeGroupItem(project_id=worker.project_id, group_id="group", position=0, competition_meta={"environment_id": "current_instance"})
        session.add_all([parent, attempt, item])
        session.commit()
        thread = runtime._prepare_attempt(session, worker=worker, attempt=attempt, control_token="test", workspace=target)
        assert thread is None
        assert attempt.parent_attempt_id == parent.id
        assert attempt.environment_id == "current_instance"
        assert (target / "work" / "solve.py").is_file()
        assert not (target / "runtime" / "codex-home").exists()
        events = session.exec(select(WorkerEvent).where(WorkerEvent.attempt_id == attempt.id)).all()
        assert any(event.payload_json.get("reason") == "environment_changed" for event in events)


def test_legacy_resume_manifest_remains_readable(tmp_path):
    import hashlib
    with Session(engine) as session:
        runtime, parent, worker, attempt, source, target, manifest = _resumable(session, tmp_path)
        payload = json.loads(ArtifactStore().read_text(manifest))
        payload["version"] = 1
        for entry in payload["codex_state"]:
            entry.pop("artifact_id", None)
        raw = json.dumps(payload).encode()
        Path(manifest.path).write_bytes(raw)
        manifest.sha256 = hashlib.sha256(raw).hexdigest()
        session.add(manifest)
        session.commit()
        assert runtime._prepare_attempt(session, worker=worker, attempt=attempt, control_token="test", workspace=target) == "original_thread"


def test_corrupt_native_state_keeps_verified_work_for_a_fresh_thread(tmp_path):
    with Session(engine) as session:
        runtime, parent, worker, attempt, source, target, manifest = _resumable(session, tmp_path, with_input=True)
        payload = json.loads(ArtifactStore().read_text(manifest))
        state = session.get(Artifact, payload["codex_state"][0]["artifact_id"])
        Path(state.path).write_text("corrupted native state")
        assert runtime._prepare_attempt(session, worker=worker, attempt=attempt, control_token="test", workspace=target) is None
        assert (target / "work" / "solve.py").read_text() == "print('retained algorithm')\n"
        assert (target / "inputs" / "original.txt").read_text() == "required old input"
        assert not (target / "runtime" / "codex-home").exists()


def test_fallback_prompt_and_inputs_match_the_parent_actually_restored(tmp_path):
    with Session(engine) as session:
        runtime, parent, worker, attempt, source, target, manifest = _resumable(session, tmp_path, with_input=True)
        bad = Attempt(project_id=parent.project_id, intent_id="bad", worker_id="bad", status="PARTIAL", codex_thread_id="bad_thread", resume_manifest_artifact_id="missing")
        intent = Intent(id=worker.intent_id, project_id=worker.project_id, objective="continue actual state")
        checkpoint = AttemptCheckpoint(project_id=parent.project_id, intent_id=parent.intent_id, attempt_id=parent.id, summary="the restored parent handoff")
        attempt.parent_attempt_id = bad.id
        session.add_all([bad, intent, checkpoint, attempt])
        session.commit()
        snapshot = ContextBuilder().build(session, project_id=worker.project_id, intent_id=intent.id, worker_id=worker.id)
        snapshot_id = snapshot.id
        assert snapshot.sections_json["session_handoff"]["parent_attempt_id"] == bad.id

        class CaptureRunner:
            def run_streaming(self, **kwargs):
                prompt = (kwargs["cwd"] / "aurora-intent.md").read_text()
                assert parent.id in prompt and "the restored parent handoff" in prompt
                entries = json.loads((kwargs["cwd"] / "inputs/manifest.json").read_text())
                assert any(entry["path"] == "inputs/original.txt" for entry in entries)
                raise RuntimeError("captured before model invocation")

        runtime.command_runner = CaptureRunner()
        with pytest.raises(RuntimeError, match="captured before model invocation"):
            runtime.execute(session, worker=worker, snapshot=snapshot)
        assert snapshot.id == snapshot_id
        assert snapshot.sections_json["session_handoff"]["parent_attempt_id"] == parent.id


def test_resume_bundle_rejects_foreign_artifacts_without_publishing(tmp_path):
    import hashlib
    with Session(engine) as session:
        runtime, parent, worker, attempt, source, target, manifest = _resumable(session, tmp_path)
        foreign = runtime.artifact_store.write_text(session, project_id="foreign", content="foreign state", summary="foreign")
        payload = json.loads(ArtifactStore().read_text(manifest))
        payload["codex_state"][0].update(artifact_id=foreign.id, sha256=foreign.sha256)
        raw = json.dumps(payload).encode()
        Path(manifest.path).write_bytes(raw)
        manifest.sha256 = hashlib.sha256(raw).hexdigest()
        session.add(manifest)
        session.commit()
        valid, _ = runtime._restore_resume_manifest(session, parent=parent, workspace=target)
        assert not valid
        assert not (target / "runtime" / "codex-home").exists()


def test_lineage_fact_evidence_is_materialized_without_checkpoint_artifact_refs(tmp_path):
    with Session(engine) as session:
        project, intent, worker, attempt, parent, fact, checkpoint = _branch(session)
        evidence = ArtifactStore(tmp_path / "artifacts").write_text(session, project_id=project.id, content="old decisive observation", summary="needed by branch", origin_kind="worker_observation")
        fact.evidence_refs = [evidence.id]
        session.add(fact)
        session.commit()
        get_settings().worker_input_max_files = 1
        ArtifactStore(tmp_path / "artifacts").write_text(session, project_id=project.id, content="new unrelated observation", summary="peer", origin_kind="worker_observation")
        snapshot = ContextBuilder().build(session, project_id=project.id, intent_id=intent.id, worker_id=worker.id)
        workspace = tmp_path / "worker"
        workspace.mkdir()
        CodexHarnessRuntime._materialize_project_inputs(session, snapshot, workspace)
        entries = json.loads((workspace / "inputs/manifest.json").read_text())
        assert evidence.id in {entry["artifact_id"] for entry in entries}


def test_restore_publication_failure_rolls_back_work_and_inputs(monkeypatch, tmp_path):
    with Session(engine) as session:
        runtime, parent, worker, attempt, source, target, manifest = _resumable(session, tmp_path, with_input=True)
        (target / "work").mkdir(parents=True)
        (target / "work/current.txt").write_text("current work")
        (target / "runtime/codex-home").mkdir(parents=True)
        (target / "runtime/codex-home/current.txt").write_text("current session")
        (target / "inputs").mkdir()
        (target / "inputs/manifest.json").write_text("[]")
        replace = Path.replace

        def fail_session_publish(path, destination):
            if path.name == "home" and path.parent.name.startswith(".resume-"):
                raise OSError("simulated publication failure")
            return replace(path, destination)

        monkeypatch.setattr(Path, "replace", fail_session_publish)
        valid, _ = runtime._restore_resume_manifest(session, parent=parent, workspace=target)
        assert not valid
        assert (target / "work/current.txt").read_text() == "current work"
        assert (target / "runtime/codex-home/current.txt").read_text() == "current session"
        assert (target / "inputs/manifest.json").read_text() == "[]"
        assert not (target / "inputs/original.txt").exists()
        assert not (target / "work/solve.py").exists()
