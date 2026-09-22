from datetime import timedelta
from threading import Lock
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.db import engine
from aurora.models import Attempt, ChallengeGroup, ChallengeGroupEvent, ChallengeGroupItem, Intent, Project, Worker, now_utc
from aurora.services.artifact_store import ArtifactStore
from aurora.services.challenge_group_runner import ChallengeGroupRunner
from aurora.services.harvester_runner import HarvesterResult
from aurora.services.worker_runtime import CodexHarnessRuntime


class InstanceAdapter:
    def __init__(self):
        self.active = {}
        self.starts = []
        self.closes = []
        self.lock = Lock()

    def ensure_environment(self, session, *, project_id):
        with self.lock:
            if project_id not in self.active:
                self.active[project_id] = f"instance-{len(self.starts)}"
                self.starts.append(project_id)
            item = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.project_id == project_id)).one()
            item.competition_meta = {**item.competition_meta, "environment_id": self.active[project_id],
                                     "container_status": "available", "container_addr": ["http://test.invalid:80"]}
            session.add(item)
            session.commit()
        return SimpleNamespace(available=True, reason=None)

    def close_environment(self, *, project_id, session):
        with self.lock:
            self.active.pop(project_id, None)
            self.closes.append(project_id)
            item = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.project_id == project_id)).one()
            item.competition_meta = {**item.competition_meta, "container_status": "stopped", "container_addr": []}
            session.add(item)

    def fetch_hint(self, session, *, project_id):
        return None


class ContinuingHarvester:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.calls = []
        self.resumable_from_phase = 1

    def run(self, session, *, project_id, task, limits, should_stop):
        project = session.get(Project, project_id)
        phase = task["phase"]
        if project.name == "untouched":
            self.calls.append((project.name, phase, None))
            project.status = "COMPLETED"
            session.add(project)
            session.commit()
            return HarvesterResult(status="COMPLETED", reason="project_completed")
        item = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.project_id == project_id)).one()
        intent = session.exec(select(Intent).where(Intent.project_id == project_id, Intent.status == "PENDING").order_by(Intent.created_at)).first()
        worker = Worker(project_id=project_id, intent_id=intent.id, status="COMPLETED")
        attempt = Attempt(project_id=project_id, intent_id=intent.id, worker_id=worker.id,
                          parent_attempt_id=intent.budget.get("continuation_attempt_id"),
                          environment_id=item.competition_meta["environment_id"])
        session.add_all([worker, attempt])
        session.commit()
        runtime = CodexHarnessRuntime(artifact_store=ArtifactStore(self.root / "artifacts"))
        runtime.settings.codex_workspace_dir = self.root / "workers"
        workspace = runtime.settings.codex_workspace_dir / project_id / worker.id
        thread = runtime._prepare_attempt(session, worker=worker, attempt=attempt, control_token="test", workspace=workspace)
        self.calls.append((project.name, phase, thread))
        attempt.codex_thread_id = thread or f"thread-{phase}"
        attempt.status = "PARTIAL"
        attempt.finished_at = now_utc()
        intent.status = "FAILED"
        session.add_all([attempt, intent])
        session.commit()
        (workspace / "inputs").mkdir(parents=True, exist_ok=True)
        (workspace / "inputs" / "manifest.json").write_text("[]")
        (workspace / "work").mkdir(exist_ok=True)
        (workspace / "work" / "experiment.py").write_text("print('saved experiment')\n")
        native = workspace / "runtime" / "codex-home" / "sessions"
        native.mkdir(parents=True, exist_ok=True)
        (native / "thread.jsonl").write_text('{"type":"retained conversation"}\n')
        if phase >= self.resumable_from_phase:
            runtime._persist_resume_manifest(session, attempt=attempt, workspace=workspace)
        if phase < 3:
            session.add(Intent(project_id=project_id, objective=f"Continue saved experiment in phase {phase + 1}",
                               parent_intent_id=intent.id, capability_tags=["codex.shell"],
                               budget={"continuation_attempt_id": attempt.id}))
            session.commit()
        return HarvesterResult(status="FAILED", reason="max_minutes")


@pytest.mark.parametrize("concurrent", [1, 2])
def test_phase_handoff_resumes_native_thread_and_releases_after_one_extra_phase(tmp_path, concurrent):
    adapter = InstanceAdapter()
    harvester = ContinuingHarvester(tmp_path)
    with Session(engine) as session:
        group = ChallengeGroup(name="retained handoff", max_concurrent=concurrent)
        first = Project(name="continuation", goal="finish saved experiment")
        other = Project(name="untouched", goal="attempt this challenge too")
        session.add_all([group, first, other])
        session.commit()
        for position, project in enumerate([first, other]):
            session.add(ChallengeGroupItem(group_id=group.id, project_id=project.id, position=position,
                                          competition_meta={"platform": "tsecbench", "unique_code": project.name}))
            session.add(Intent(project_id=project.id, objective="initial experiment"))
        session.commit()
        ChallengeGroupRunner(harvester=harvester, competition=adapter).run(session, group_id=group.id)
        calls = [call for call in harvester.calls if call[0] == first.name]
        assert calls == ([(first.name, 1, None), (first.name, 2, None), (first.name, 3, "thread-2")] if concurrent == 1 else
                         [(first.name, 1, None), (first.name, 2, "thread-1"), (first.name, 3, None)])
        assert adapter.starts.count(first.id) == 2
        assert adapter.closes.count(first.id) == 2
        assert not adapter.active
        assert session.exec(select(Attempt).where(Attempt.project_id == first.id, Attempt.resume_count == 1)).one()
        retained = session.exec(select(ChallengeGroupEvent).where(
            ChallengeGroupEvent.group_id == group.id, ChallengeGroupEvent.event_type == "group.item.environment_retained",
        )).all()
        assert len(retained) == 1
        if concurrent == 1:
            assert [(name, phase) for name, phase, _ in harvester.calls] == [
                (first.name, 1), (other.name, 1), (first.name, 2), (first.name, 3),
            ]


def test_stop_releases_instance_held_for_pending_continuation(tmp_path):
    adapter = InstanceAdapter()
    harvester = ContinuingHarvester(tmp_path)
    with Session(engine) as session:
        group = ChallengeGroup(name="stop handoff", max_concurrent=1)
        project = Project(name="continuation", goal="continue")
        session.add_all([group, project])
        session.commit()
        session.add(ChallengeGroupItem(group_id=group.id, project_id=project.id, position=0,
                                      competition_meta={"platform": "tsecbench", "unique_code": "one"}))
        session.add(Intent(project_id=project.id, objective="initial experiment"))
        session.commit()
        ChallengeGroupRunner(harvester=harvester, competition=adapter).run(
            session, group_id=group.id, should_stop=lambda: bool(harvester.calls),
        )
        assert not adapter.active
        assert adapter.closes == [project.id]
        session.refresh(group)
        assert group.status == "STOPPED"


@pytest.mark.parametrize("blocker", ["disabled", "changed_instance", "unreachable", "missing_thread", "foreign_manifest", "unbound_parent", "deadline"])
def test_ineligible_continuation_releases_target(tmp_path, blocker):
    adapter = InstanceAdapter()
    harvester = ContinuingHarvester(tmp_path)
    with Session(engine) as session:
        group = ChallengeGroup(name="ineligible handoff")
        project = Project(name="continuation", goal="continue")
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=0,
                                  fused_status="RUNNING", competition_meta={"platform": "tsecbench"})
        session.add_all([group, project, item, Intent(project_id=project.id, objective="initial experiment")])
        session.commit()
        adapter.ensure_environment(session, project_id=project.id)
        harvester.run(session, project_id=project.id, task={"phase": 1}, limits=None, should_stop=None)
        parent = session.exec(select(Attempt).where(Attempt.project_id == project.id)).one()
        if blocker == "disabled":
            get_settings().tsecbench_retain_environment_for_continuation = False
        elif blocker == "changed_instance":
            parent.environment_id = "previous-instance"
        elif blocker == "unreachable":
            parent.finalization_reason = "target_unreachable"
        elif blocker == "missing_thread":
            parent.codex_thread_id = None
        elif blocker == "foreign_manifest":
            from aurora.models import Artifact

            manifest = session.get(Artifact, parent.resume_manifest_artifact_id)
            manifest.project_id = "foreign-project"
            session.add(manifest)
        elif blocker == "unbound_parent":
            intent = session.exec(select(Intent).where(Intent.project_id == project.id, Intent.status == "PENDING")).one()
            intent.parent_intent_id = "unrelated-intent"
            session.add(intent)
        else:
            group.deadline_at = now_utc() - timedelta(seconds=1)
        session.add_all([parent, group])
        session.commit()
        ChallengeGroupRunner(competition=adapter)._resolve_phase(
            session, group=group, item=item, project=project,
            outcome="FAILED", reason="max_minutes",
        )
        assert not adapter.active
        assert adapter.closes == [project.id]


def test_retained_phase_uses_global_deadline(tmp_path):
    adapter = InstanceAdapter()
    harvester = ContinuingHarvester(tmp_path)
    with Session(engine) as session:
        group = ChallengeGroup(name="bounded handoff", deadline_at=now_utc() + timedelta(minutes=2))
        project = Project(name="continuation", goal="continue")
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=0,
                                  fused_status="RUNNING", competition_meta={"platform": "tsecbench"})
        session.add_all([group, project, item, Intent(project_id=project.id, objective="initial experiment")])
        session.commit()
        adapter.ensure_environment(session, project_id=project.id)
        harvester.run(session, project_id=project.id, task={"phase": 1}, limits=None, should_stop=None)
        runner = ChallengeGroupRunner(competition=adapter)
        runner._resolve_phase(session, group=group, item=item, project=project, outcome="FAILED", reason="max_minutes")
        session.refresh(group)
        session.refresh(item)
        assert item.phase == 2 and item.phase_deadline_at == group.deadline_at
        assert project.id in adapter.active


def test_second_phase_can_retain_a_new_instance_for_final_phase(tmp_path):
    adapter = InstanceAdapter()
    harvester = ContinuingHarvester(tmp_path)
    harvester.resumable_from_phase = 2
    with Session(engine) as session:
        group = ChallengeGroup(name="late continuation", max_concurrent=1)
        project = Project(name="continuation", goal="continue")
        session.add_all([group, project])
        session.commit()
        session.add(ChallengeGroupItem(group_id=group.id, project_id=project.id, position=0,
                                      competition_meta={"platform": "tsecbench", "unique_code": "one"}))
        session.add(Intent(project_id=project.id, objective="initial experiment"))
        session.commit()
        ChallengeGroupRunner(harvester=harvester, competition=adapter).run(session, group_id=group.id)
        assert harvester.calls == [(project.name, 1, None), (project.name, 2, None), (project.name, 3, "thread-2")]
        assert not adapter.active and len(adapter.starts) == len(adapter.closes) == 2
