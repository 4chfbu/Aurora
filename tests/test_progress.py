from datetime import timedelta
from types import SimpleNamespace
import json
import time

from sqlmodel import Session, select

from aurora.db import engine
from aurora.models import Attempt, ChallengeGroup, ChallengeGroupItem, Fact, Project, ToolTrace, WorkerEvent, now_utc
from aurora.services.artifact_store import ArtifactStore
from aurora.services.challenge_group_runner import ChallengeGroupRunner
from aurora.services.progress import evidence_progress_counts, repeated_experiments, target_transport_failed
from aurora.services.worker_runtime import CodexHarnessRuntime
from aurora.models import Worker


def test_duplicate_observations_and_transport_errors_are_not_progress():
    with Session(engine) as session:
        project = Project(name="progress", goal="measure evidence")
        session.add(project)
        session.commit()
        store = ArtifactStore()
        refs = []
        for content in ("service version 2", "service version 2", "Connection refused"):
            artifact = store.write_text(session, project_id=project.id, content=content, summary=content, artifact_type="terminal", origin_kind="worker_observation")
            refs.append(artifact.id)
        session.add_all([
            Fact(project_id=project.id, statement="version discovered", category="service", evidence_refs=[refs[0]]),
            Fact(project_id=project.id, statement="same version, different wording", category="service", evidence_refs=[refs[1]]),
            Fact(project_id=project.id, statement="port is down", category="blocker", evidence_refs=[refs[2]]),
        ])
        session.commit()
        assert evidence_progress_counts(session, project.id) == (1, 1)


def test_sync_reads_are_not_repeated_experiments():
    traces = [ToolTrace(project_id="project", tool_name="codex.shell", request_json={"command": command}) for command in (
        "cat runtime/blackboard.json", "cat runtime/blackboard.json", "cat inputs/manifest.json",
        "cat runtime/blackboard.json | head -c 2000", "cat runtime/blackboard.json | head -c 2000",
        "curl http://target.example", "curl http://target.example",
    )]
    assert repeated_experiments(traces) == 1


def test_transport_detection_ignores_other_hosts_and_resets_after_recovery():
    with Session(engine) as session:
        project = Project(name="outage", goal="wait for the target", target_url="http://target.example")
        session.add(project)
        for host in ("target.example", "unrelated.example"):
            session.add(ToolTrace(project_id=project.id, tool_name="codex.shell", command=f"curl http://{host}", summary="Failed to connect"))
        session.commit()
        assert not target_transport_failed(session, project.id)
        session.add(ToolTrace(project_id=project.id, tool_name="codex.shell", command="curl http://target.example", summary="Connection refused"))
        session.commit()
        assert target_transport_failed(session, project.id)
        session.add(WorkerEvent(project_id=project.id, event_type="target.transport_recovered"))
        session.commit()
        assert not target_transport_failed(session, project.id)


def test_outage_waits_without_advancing_phase_and_resumes_after_probe(monkeypatch):
    clock = now_utc()
    monkeypatch.setattr("aurora.services.challenge_group_runner.now_utc", lambda: clock)
    probes = []
    def probe(self, url):
        probes.append(url)
        return SimpleNamespace(success=True, public_dict=lambda: {"success": True})
    monkeypatch.setattr("aurora.services.challenge_group_runner.TargetProbeService.probe_transport", probe)
    runner = ChallengeGroupRunner()
    with Session(engine) as session:
        group = ChallengeGroup(name="backoff")
        project = Project(name="outage", goal="preserve phase", target_url="http://target.example")
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=0, phase=2, phase_deadline_at=clock + timedelta(minutes=10))
        session.add_all([group, project, item])
        session.commit()
        original_deadline = item.phase_deadline_at
        runner._resolve_phase(session, group=group, item=item, project=project, outcome="WAITING_RESOURCE", reason="target_unreachable")
        runner._reactivate_waiting_resources(session, group_id=group.id)
        runner._retry_transport_waiters(session, group=group)
        assert probes == []
        assert item.fused_status == "WAITING_RESOURCE"
        assert item.phase == 2 and not item.failure_history
        clock += timedelta(seconds=16)
        runner._retry_transport_waiters(session, group=group)
        assert probes == [project.target_url]
        assert item.fused_status == "PENDING"
        assert item.phase == 2
        assert item.phase_deadline_at.replace(tzinfo=None) == original_deadline.replace(tzinfo=None) + timedelta(seconds=16)
        assert session.exec(select(Attempt).where(Attempt.project_id == project.id)).all() == []


def test_outage_probes_are_bounded_and_release_capacity(monkeypatch):
    clock = now_utc()
    monkeypatch.setattr("aurora.services.challenge_group_runner.now_utc", lambda: clock)
    monkeypatch.setattr("aurora.services.challenge_group_runner.TargetProbeService.probe_transport", lambda *args: SimpleNamespace(success=False, public_dict=lambda: {"success": False}))
    released = []
    monkeypatch.setattr(ChallengeGroupRunner, "_release_managed_environment_after_phase", lambda *args, **kwargs: released.append(kwargs["item"].id))
    runner = ChallengeGroupRunner()
    with Session(engine) as session:
        group = ChallengeGroup(name="bounded backoff")
        project = Project(name="outage", goal="preserve phase", target_url="tcp://target.example:31337")
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=0, phase=2)
        session.add_all([group, project, item])
        session.commit()
        runner._resolve_phase(session, group=group, item=item, project=project, outcome="WAITING_RESOURCE", reason="target_unreachable")
        for delay in (15, 30, 60):
            clock += timedelta(seconds=delay)
            assert runner._retry_transport_waiters(session, group=group) is (delay == 60)
        assert item.fused_status == "WAITING_INPUT"
        assert item.phase == 2 and not item.failure_history
        assert released == [item.id]


def test_worker_finalizes_after_repeated_target_transport_failures(monkeypatch):
    monkeypatch.setattr(CodexHarnessRuntime, "_sync_blackboard", lambda *args, **kwargs: None)
    reasons = []
    monkeypatch.setattr(CodexHarnessRuntime, "_begin_finalization", lambda *args, **kwargs: reasons.append(kwargs["reason"]))
    with Session(engine) as session:
        project = Project(name="early outage", goal="avoid spending an entire phase on a dead target", target_url="http://target.example")
        worker = Worker(project_id=project.id, intent_id="intent")
        attempt = Attempt(project_id=project.id, intent_id=worker.intent_id, worker_id=worker.id)
        session.add_all([project, worker, attempt])
        session.commit()
        event = json.dumps({"type": "item.completed", "item": {"type": "command_execution", "status": "completed", "command": "curl http://target.example", "exit_code": 7, "aggregated_output": "Failed to connect to target.example"}})
        assert CodexHarnessRuntime._record_codex_event(session, worker=worker, attempt=attempt, stream="stdout", line=event) is None
        assert CodexHarnessRuntime._record_codex_event(session, worker=worker, attempt=attempt, stream="stdout", line=event) == "target_unreachable"
        assert reasons == ["target_unreachable"]


def test_checkpoint_and_sync_reads_cannot_reset_experiment_progress(monkeypatch):
    monkeypatch.setattr(CodexHarnessRuntime, "_sync_blackboard", lambda *args, **kwargs: None)
    monkeypatch.setattr(CodexHarnessRuntime, "_begin_finalization", lambda *args, **kwargs: None)
    with Session(engine) as session:
        worker = Worker(project_id="project", intent_id="intent", budgets={"max_no_progress_actions": 2})
        attempt = Attempt(project_id=worker.project_id, intent_id=worker.intent_id, worker_id=worker.id)
        session.add_all([worker, attempt])
        session.commit()
        for index, command in enumerate(("false", "cat runtime/blackboard.json", "false")):
            session.add(WorkerEvent(project_id=worker.project_id, attempt_id=attempt.id, event_type="checkpoint.saved"))
            session.commit()
            event = json.dumps({"type": "item.completed", "item": {"type": "command_execution", "status": "completed", "command": command, "exit_code": 1}})
            reason = CodexHarnessRuntime._record_codex_event(session, worker=worker, attempt=attempt, stream="stdout", line=event)
            assert reason == ("no_progress_exhausted" if index == 2 else None)


def test_manual_stop_releases_the_instance_held_during_transport_backoff(monkeypatch):
    closed = []
    runner = ChallengeGroupRunner()
    monkeypatch.setattr(runner, "_close_environment_with_retry", lambda adapter, session, project_id: closed.append(project_id))
    with Session(engine) as session:
        group = ChallengeGroup(name="stop during outage")
        project = Project(name="outage", goal="release capacity")
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=0, phase=2, fused_status="WAITING_RESOURCE", stop_reason="target_unreachable")
        session.add_all([group, project, item])
        session.commit()
        runner._close_running_environments(session, group_id=group.id)
        assert closed == [project.id]
        assert item.fused_status == "WAITING_INPUT" and item.phase == 2


def test_concurrent_outage_release_wakes_capacity_waiters(monkeypatch):
    capacity = {"released": False, "allocations": 0, "solver_runs": 0}
    class Competition:
        def ensure_environment(self, session, project_id):
            capacity["allocations"] += 1
            return SimpleNamespace(available=capacity["released"], reason="tsecbench_capacity_exhausted", disposition="wait_resource")

        def close_environment(self, session, project_id):
            return None

    class Harvester:
        def run(self, session, project_id, **kwargs):
            capacity["solver_runs"] += 1
            project = session.get(Project, project_id)
            project.status = "COMPLETED"
            session.add(project)
            session.commit()
            return SimpleNamespace(status="COMPLETED", reason="test_completed")

    runner = ChallengeGroupRunner(competition=Competition(), harvester=Harvester())
    with Session(engine) as session:
        group = ChallengeGroup(name="outage capacity handoff", max_concurrent=2)
        outage_project = Project(name="outage", goal="paused")
        waiting_project = Project(name="waiting", goal="solve after capacity is released")
        outage = ChallengeGroupItem(group_id=group.id, project_id=outage_project.id, position=0, phase=2, fused_status="WAITING_RESOURCE", stop_reason="target_unreachable", competition_meta={"platform": "tsecbench"})
        waiting = ChallengeGroupItem(group_id=group.id, project_id=waiting_project.id, position=1, competition_meta={"platform": "tsecbench"})
        session.add_all([group, outage_project, waiting_project, outage, waiting])
        session.commit()
        waiting_id, outage_id = waiting.id, outage.id

        def release_after_capacity_wait(current_session, group):
            current = current_session.get(ChallengeGroupItem, waiting_id)
            if capacity["released"] or current.fused_status != "WAITING_RESOURCE":
                return False
            capacity["released"] = True
            blocked = current_session.get(ChallengeGroupItem, outage_id)
            blocked.status = blocked.fused_status = "WAITING_INPUT"
            current_session.add(blocked)
            current_session.commit()
            return True

        monkeypatch.setattr(runner, "_retry_transport_waiters", release_after_capacity_wait)
        deadline = time.monotonic() + 5
        runner._run_concurrent(session, group_id=group.id, should_stop=lambda: time.monotonic() >= deadline, on_project=None)
        session.expire_all()
        assert session.get(ChallengeGroupItem, waiting_id).fused_status == "COMPLETED"
        assert session.get(ChallengeGroupItem, outage_id).phase == 2
        assert capacity == {"released": True, "allocations": 2, "solver_runs": 1}
