from __future__ import annotations

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.db import engine
import pytest

from aurora.models import ChallengeGroup, ChallengeGroupEvent, ChallengeGroupItem, EvaluationItemResult, EvaluationRun, EvaluationSuite, FlagCandidate, Intent, Project, ProjectRuntimePolicy, now_utc
from aurora.services.challenge_group_runner import ChallengeGroupRunner
from aurora.services.evaluation import EvaluationService
from aurora.services.tsecbench import TSecBenchChallenge


class FakeClient:
    def list_challenges(self) -> list[TSecBenchChallenge]:
        return [
            TSecBenchChallenge(
                unique_code="fresh-web",
                title="Fresh Web",
                description="Find the flag.",
                challenge_type="web",
                difficulty="easy",
                level=1,
                points=100,
                flag_count=1,
                container_status=None,
                container_addr=[],
                raw={},
                correct_flag_count=0,
                is_completed=False,
            ),
            TSecBenchChallenge(
                unique_code="already-solved",
                title="Solved",
                description="Historical result.",
                challenge_type="reverse",
                difficulty="easy",
                level=1,
                points=50,
                flag_count=1,
                container_status=None,
                container_addr=[],
                raw={},
                correct_flag_count=1,
                is_completed=True,
            ),
        ]


def test_evaluation_suite_is_frozen_and_excludes_completed_challenges() -> None:
    with Session(engine) as session:
        suite = EvaluationService().create_suite(session, name="internet", client=FakeClient())

        assert [item["unique_code"] for item in suite.items_json] == ["fresh-web"]
        assert suite.items_json[0]["version_hash"]
        assert suite.content_hash


def test_started_evaluation_api_returns_run_identifiers(monkeypatch) -> None:
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from aurora.api import create_app

    monkeypatch.setattr("aurora.services.evaluation.TSecBenchClient.list_challenges", lambda self: FakeClient().list_challenges())
    monkeypatch.setattr("aurora.api.challenge_group_registry.start", lambda group_id: SimpleNamespace(group_id=group_id, status="RUNNING"))
    with TestClient(create_app()) as client:
        suite = client.post("/api/evaluations/suites", json={"name": "api-run"}).json()
        response = client.post(f"/api/evaluations/suites/{suite['id']}/runs", json={
            "label": "baseline", "variant": "baseline", "start": True,
        })

    assert response.status_code == 200
    data = response.json()
    assert data["run"]["id"]
    assert data["run"]["group_id"] == data["background"]["group_id"]
    assert data["run"]["status"] == "RUNNING"


def test_evaluation_projects_opt_into_multi_agent_when_globally_enabled(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_MULTI_AGENT_EXPLORATION_ENABLED", "true")
    monkeypatch.setenv("AURORA_MULTI_AGENT_MAX_PROJECT_WORKERS", "3")
    get_settings.cache_clear()
    try:
        with Session(engine) as session:
            suite = EvaluationService().create_suite(session, name="parallel-evaluation", client=FakeClient())
            run = EvaluationService().create_run(
                session,
                suite_id=suite.id,
                label="parallel",
                variant="candidate",
                client=FakeClient(),
            )
            result = session.exec(select(EvaluationItemResult).where(EvaluationItemResult.run_id == run.id)).one()
            policy = session.exec(
                select(ProjectRuntimePolicy).where(ProjectRuntimePolicy.project_id == result.project_id)
            ).one()

            assert policy.multi_agent_exploration_enabled is True
            assert policy.max_parallel_explorers == 3
    finally:
        get_settings.cache_clear()


def test_evaluation_run_uses_platform_completion_and_compares_same_suite() -> None:
    service = EvaluationService()
    with Session(engine) as session:
        suite = service.create_suite(session, name="internet", client=FakeClient())
        baseline = service.create_run(session, suite_id=suite.id, label="before", variant="baseline", client=FakeClient())
        baseline_group = session.get(ChallengeGroup, baseline.group_id)
        assert baseline_group is not None
        baseline_group.status = "COMPLETED"
        baseline_group.finished_at = now_utc()
        session.add(baseline_group)
        session.commit()
        candidate = service.create_run(session, suite_id=suite.id, label="after", variant="candidate", client=FakeClient())
        assert candidate.config_json["phase_minutes"] == [12, 25, 40]
        assert candidate.config_json["max_minutes_per_challenge"] == 77

        candidate_result = session.exec(select(EvaluationItemResult).where(EvaluationItemResult.run_id == candidate.id)).one()
        project = session.get(Project, candidate_result.project_id)
        ChallengeGroupRunner._apply_phase_attempt_budget(session, project_id=project.id, phase=1)
        intent = session.exec(select(Intent).where(Intent.project_id == project.id, Intent.status == "PENDING")).one()
        assert intent.budget["soft_timeout_seconds"] == 660
        assert intent.budget["hard_timeout_seconds"] == 720
        assert intent.budget["max_handoff_intents"] == 1
        intent.budget = {}
        session.add(intent)
        session.commit()
        ChallengeGroupRunner._apply_phase_attempt_budget(session, project_id=project.id, phase=2)
        session.refresh(intent)
        assert intent.budget["soft_timeout_seconds"] == 1440
        assert intent.budget["hard_timeout_seconds"] == 1500
        assert "max_handoff_intents" not in intent.budget
        intent.budget = {}
        session.add(intent)
        session.commit()
        ChallengeGroupRunner._apply_phase_attempt_budget(session, project_id=project.id, phase=3)
        session.refresh(intent)
        assert intent.budget["soft_timeout_seconds"] == 2340
        assert intent.budget["hard_timeout_seconds"] == 2400
        project.status = "COMPLETED"
        item = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.project_id == project.id)).one()
        item.submission_status = "ACCEPTED"
        item.fused_status = "COMPLETED"
        group = session.get(ChallengeGroup, candidate.group_id)
        group.status = "COMPLETED"
        group.finished_at = now_utc()
        session.add_all([
            project,
            item,
            group,
            FlagCandidate(
                project_id=project.id,
                value="flag{verified}",
                value_hash="evaluation-verified-hash",
                status="ACCEPTED",
                provenance_kind="DERIVED_REPLAY",
                verification_artifact_ref="artifact_verify",
            ),
        ])
        session.commit()

        baseline_report = service.refresh(session, run_id=baseline.id)
        candidate_report = service.refresh(session, run_id=candidate.id)
        comparison = service.compare(session, baseline_run_id=baseline.id, candidate_run_id=candidate.id)

        assert baseline_report["metrics"]["success_rate"] == 0.0
        assert candidate_report["metrics"]["success_rate"] == 1.0
        assert candidate_report["metrics"]["derived_verification_coverage"] == 1.0
        assert comparison.improvement_points == 100.0
        assert comparison.promotion_eligible is False
        assert comparison.promoted is False


def test_evaluation_rejects_overlapping_runs_dirty_platform_state_and_unmaterialized_attachments() -> None:
    service = EvaluationService()
    with Session(engine) as session:
        suite = service.create_suite(session, name="clean", client=FakeClient())
        service.create_run(session, suite_id=suite.id, label="first", client=FakeClient())
        with pytest.raises(ValueError, match="still active"):
            service.create_run(session, suite_id=suite.id, label="overlap", client=FakeClient())

    class DirtyClient(FakeClient):
        def list_challenges(self) -> list[TSecBenchChallenge]:
            challenge = super().list_challenges()[0]
            return [TSecBenchChallenge(**{**challenge.__dict__, "correct_flag_count": 1, "is_completed": True})]

    with Session(engine) as session:
        suite = service.create_suite(session, name="dirty", client=FakeClient())
        with pytest.raises(ValueError, match="clean platform session"):
            service.create_run(session, suite_id=suite.id, label="dirty", client=DirtyClient())

    class AttachmentClient(FakeClient):
        def list_challenges(self) -> list[TSecBenchChallenge]:
            challenge = super().list_challenges()[0]
            return [TSecBenchChallenge(**{**challenge.__dict__, "raw": {"attachments": [{"url": "https://files.example/a.zip"}]}})]

    with Session(engine) as session:
        suite = service.create_suite(session, name="attachments", client=AttachmentClient())
        with pytest.raises(ValueError, match="materialization is not implemented"):
            service.create_run(session, suite_id=suite.id, label="attachments", client=AttachmentClient())


@pytest.mark.parametrize("submission_status", ["MANUALLY_ACCEPTED", "SUBMITTED"])
def test_evaluation_requires_platform_acceptance(submission_status) -> None:
    service = EvaluationService()
    with Session(engine) as session:
        suite = service.create_suite(session, name="platform-only", client=FakeClient())
        run = service.create_run(session, suite_id=suite.id, label="candidate", client=FakeClient())
        result = session.exec(select(EvaluationItemResult).where(EvaluationItemResult.run_id == run.id)).one()
        project = session.get(Project, result.project_id)
        item = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.project_id == project.id)).one()
        project.status = item.fused_status = "COMPLETED"
        item.submission_status = submission_status
        session.add_all([project, item])
        session.commit()

        report = service.refresh(session, run_id=run.id)

        assert report["metrics"]["completed"] == 0
        assert report["results"][0].platform_correct is None


def test_evaluation_keeps_rejected_submission_history_after_success() -> None:
    service = EvaluationService()
    with Session(engine) as session:
        suite = service.create_suite(session, name="submission-history", client=FakeClient())
        run = service.create_run(session, suite_id=suite.id, label="candidate", client=FakeClient())
        result = session.exec(select(EvaluationItemResult).where(EvaluationItemResult.run_id == run.id)).one()
        project = session.get(Project, result.project_id)
        item = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.project_id == project.id)).one()
        project.status = item.fused_status = "COMPLETED"
        item.submission_status = "ACCEPTED"
        session.add_all([project, item])
        for index in range(2):
            session.add(ChallengeGroupEvent(
                group_id=run.group_id, item_id=item.id,
                event_type="group.item.flag_submission_rejected",
                payload_json={"candidate_id": f"rejected-{index}"},
            ))
        session.commit()

        report = service.refresh(session, run_id=run.id)

        assert report["metrics"]["completed"] == 1
        assert report["metrics"]["wrong_submissions"] == 2


@pytest.mark.parametrize("candidate_status", ["RUNNING", "COMPLETED"])
def test_comparison_uses_the_same_eligible_challenges(candidate_status) -> None:
    service = EvaluationService()
    with Session(engine) as session:
        suite = EvaluationSuite(name="paired", content_hash="frozen", items_json=[
            {"unique_code": str(index)} for index in range(40)
        ])
        baseline = EvaluationRun(suite_id=suite.id, label="before", variant="baseline", status="COMPLETED")
        candidate = EvaluationRun(suite_id=suite.id, label="after", variant="candidate", status=candidate_status)
        session.add_all([suite, baseline, candidate])
        for index in range(40):
            # Candidate loses ten failures to environment errors. This must not
            # manufacture a 25-point improvement or satisfy the promotion gate.
            for run in (baseline, candidate):
                session.add(EvaluationItemResult(
                    run_id=run.id, challenge_key=str(index),
                    status="COMPLETED" if index < 30 else "FAILED",
                    platform_completed=index < 30,
                    environment_error=run.id == candidate.id and index >= 30,
                ))
        session.commit()

        comparison = service.compare(session, baseline_run_id=baseline.id, candidate_run_id=candidate.id)

        assert comparison.sample_size == 30
        assert comparison.baseline_success_rate == comparison.candidate_success_rate == 1.0
        assert comparison.improvement_points == 0.0
        assert comparison.promoted is False
        assert comparison.promotion_eligible is (candidate_status == "COMPLETED")
