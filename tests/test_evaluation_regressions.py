from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from aurora.db import engine
from aurora.models import ChallengeGroup, ChallengeGroupItem, Intent, Project, now_utc
from aurora.services.autorunner import AutoRunnerService
from aurora.services.challenge_group_runner import ChallengeGroupRunner
from aurora.services.deadlines import as_utc
from aurora.services.harvester_runner import HarvesterResult


@pytest.mark.parametrize("concurrent", [1, 2])
@pytest.mark.parametrize("reason", ["tsecbench_capacity_exhausted", "tsecbench_control_plane_unavailable"])
def test_resource_retry_preserves_first_solve_budget(monkeypatch, concurrent, reason):
    current = now_utc()
    allocations = []
    executions = []

    class Adapter:
        def ensure_environment(self, session, *, project_id):
            nonlocal current
            item = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.project_id == project_id)).one()
            allocations.append(item.phase_deadline_at)
            if len(allocations) == 1:
                current += timedelta(minutes=13)
                return SimpleNamespace(available=False, reason=reason, disposition="wait_resource")
            return SimpleNamespace(available=True, reason=None)

        def close_environment(self, *, project_id, session=None):
            pass

    class Harvester:
        def run(self, session, *, project_id, task, limits, should_stop):
            executions.append((task["phase"], (as_utc(limits.deadline_at) - current).total_seconds()))
            project = session.get(Project, project_id)
            project.status = "COMPLETED"
            session.add(project)
            session.commit()
            return HarvesterResult(status="COMPLETED", reason="project_completed")

    monkeypatch.setattr("aurora.services.challenge_group_runner.now_utc", lambda: current)
    monkeypatch.setattr("aurora.services.autorunner.now_utc", lambda: current)
    with Session(engine) as session:
        group = ChallengeGroup(name="resource retry", max_concurrent=concurrent,
                               limits={"resource_retry_base_seconds": 0, "resource_retry_limit": 2})
        project = Project(name="first attempt", goal="solve")
        session.add_all([group, project])
        session.commit()
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=0,
                                  competition_meta={"platform": "tsecbench", "unique_code": "test"})
        session.add_all([item, Intent(project_id=project.id, objective="solve")])
        session.commit()
        ChallengeGroupRunner(harvester=Harvester(), competition=Adapter()).run(session, group_id=group.id)
        session.refresh(group)
        assert group.status == "COMPLETED"
        assert allocations == [None, None]
        assert executions == [(1, 720)]


def test_derived_timeout_can_grow_in_next_phase_but_explicit_cap_is_preserved():
    with Session(engine) as session:
        project = Project(name="budgets", goal="solve")
        session.add(project)
        session.commit()
        derived = Intent(project_id=project.id, objective="default phase budget")
        explicit = Intent(project_id=project.id, objective="operator cap", budget={"hard_timeout_seconds": 90})
        session.add_all([derived, explicit])
        session.commit()
        AutoRunnerService._clamp_pending_intents_to_deadline(
            session, project_id=project.id, phase=1, deadline_at=now_utc() + timedelta(seconds=30),
        )
        AutoRunnerService._clamp_pending_intents_to_deadline(
            session, project_id=project.id, phase=2, deadline_at=now_utc() + timedelta(minutes=25),
        )
        session.refresh(derived)
        session.refresh(explicit)
        assert derived.budget["hard_timeout_seconds"] >= 1498
        assert explicit.budget["hard_timeout_seconds"] == 90


def test_resource_wait_does_not_reset_existing_phase_budget(monkeypatch):
    current = now_utc()
    monkeypatch.setattr("aurora.services.challenge_group_runner.now_utc", lambda: current)
    item = ChallengeGroupItem(group_id="group", project_id="project", position=0, phase=2,
                              phase_started_at=current - timedelta(minutes=20),
                              phase_deadline_at=current + timedelta(minutes=5))
    group = ChallengeGroup(id="group", name="group", deadline_at=current + timedelta(minutes=30))
    ChallengeGroupRunner._record_resource_wait(item, group=group)
    current += timedelta(minutes=13)
    ChallengeGroupRunner._start_phase_window(item, deadline_at=group.deadline_at)
    assert (as_utc(item.phase_deadline_at) - current).total_seconds() == 300
    assert (current - as_utc(item.phase_started_at)).total_seconds() == 1200


@pytest.mark.parametrize("duplicate", [False, True])
def test_partial_flag_does_not_reset_phase_or_deadline(monkeypatch, duplicate):
    from aurora.services.competition_adapter import CompetitionSubmissionResult

    with Session(engine) as session:
        project = Project(name="multi flag", goal="solve remaining flags", status="FLAG_READY")
        group = ChallengeGroup(name="multi flag")
        session.add_all([project, group])
        session.commit()
        deadline = now_utc() + timedelta(minutes=4)
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=0, phase=2,
                                  phase_started_at=now_utc() - timedelta(minutes=21), phase_deadline_at=deadline)
        session.add(item)
        session.commit()
        runner = ChallengeGroupRunner()
        monkeypatch.setattr(runner, "_submit_pending_flag", lambda *args, **kwargs: CompetitionSubmissionResult(
            correct=True, completed=False, detail={"duplicate": duplicate, "new_progress": not duplicate},
        ))
        runner._resolve_phase(session, group=group, item=item, project=project, outcome="CANDIDATE_READY", reason="candidate")
        assert item.phase == 2
        assert as_utc(item.phase_deadline_at) == deadline


@pytest.mark.parametrize("concurrent", [1, 2])
def test_persistent_resource_error_has_bounded_retries_without_using_phases(concurrent):
    allocations = []

    class Adapter:
        def ensure_environment(self, session, *, project_id):
            allocations.append(project_id)
            return SimpleNamespace(available=False, reason="tsecbench_control_plane_unavailable", disposition="wait_resource")

    with Session(engine) as session:
        project = Project(name="temporary error", goal="solve")
        group = ChallengeGroup(name="bounded retry", max_concurrent=concurrent,
                               limits={"resource_retry_base_seconds": 0, "resource_retry_limit": 2})
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(group_id=group.id, project_id=project.id, position=0,
                                  competition_meta={"platform": "tsecbench"})
        session.add(item)
        session.commit()
        ChallengeGroupRunner(competition=Adapter()).run(session, group_id=group.id)
        session.refresh(item)
        session.refresh(group)
        assert len(allocations) == 3
        assert group.status == item.fused_status == "WAITING_RESOURCE"
        assert item.phase == 1 and item.phase_attempts == {} and item.phase_deadline_at is None


def test_resource_wait_restoration_is_bounded_by_global_deadline(monkeypatch):
    current = now_utc()
    monkeypatch.setattr("aurora.services.challenge_group_runner.now_utc", lambda: current)
    group = ChallengeGroup(name="deadline", deadline_at=current + timedelta(minutes=15))
    item = ChallengeGroupItem(group_id=group.id, project_id="project", position=0,
                              phase_started_at=current, phase_deadline_at=current + timedelta(minutes=12))
    ChallengeGroupRunner._record_resource_wait(item, group=group)
    current += timedelta(minutes=13)
    ChallengeGroupRunner._start_phase_window(item, deadline_at=group.deadline_at)
    assert item.phase_deadline_at == group.deadline_at


def test_retention_reserves_capacity_and_global_time_for_untouched_items():
    with Session(engine) as session:
        group = ChallengeGroup(name="coverage", max_concurrent=1)
        solved_route = Project(name="continuing", goal="continue")
        untouched = Project(name="unattempted", goal="first try")
        session.add_all([group, solved_route, untouched])
        session.commit()
        item = ChallengeGroupItem(group_id=group.id, project_id=solved_route.id, position=0, phase=2, phase_attempts={"1": 1})
        fresh = ChallengeGroupItem(group_id=group.id, project_id=untouched.id, position=1)
        session.add_all([item, fresh])
        session.commit()
        runner = ChallengeGroupRunner()
        assert not runner._can_retain_capacity(session, group=group, item=item)
        group.max_concurrent = 2
        assert runner._can_retain_capacity(session, group=group, item=item)
        group.deadline_at = now_utc() + timedelta(minutes=5)
        assert not runner._can_retain_capacity(session, group=group, item=item)
