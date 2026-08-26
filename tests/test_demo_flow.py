import os
from pathlib import Path
from datetime import timedelta

os.environ["AURORA_DB_URL"] = "sqlite:////tmp/aurora_test.db"
os.environ["AURORA_ARTIFACT_DIR"] = "/tmp/aurora_test_artifacts"
os.environ["AURORA_TOOL_CONTRACT"] = "native_privileged"

db_path = Path("/tmp/aurora_test.db")
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient  # noqa: E402

from aurora.api import create_app  # noqa: E402
from aurora.db import engine  # noqa: E402
from aurora.models import Attempt, AttemptCheckpoint, AuthorizationScope, ChallengeGroup, ChallengeGroupItem, DiscoveredTarget, Fact, Finding, FlagCandidate, ImportCandidate, Intent, LLMTrace, Project, ToolTrace, Worker, WorkerEvent, now_utc  # noqa: E402
from aurora.services.challenge_group_runner import GroupRunState  # noqa: E402
from aurora.services.artifact_store import ArtifactStore  # noqa: E402
from aurora.services.browser_interaction import BrowserInteractionService  # noqa: E402
from aurora.services.capability_gateway import CapabilityGateway  # noqa: E402
from aurora.services.command_runner import LocalCommandRunner  # noqa: E402
from aurora.services.demo import _repeat_failure_count, _route_request  # noqa: E402
from aurora.services.flag_validator import FlagValidator  # noqa: E402
from aurora.services.policy import PolicyEngine  # noqa: E402
from aurora.services.result_processor import ResultProcessor  # noqa: E402
from aurora.services.round_summary import RoundReflectionService  # noqa: E402
from aurora.services.scheduler import Scheduler  # noqa: E402
from aurora.services.worker_runtime import CodexHarnessRuntime  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

import pytest  # noqa: E402

from aurora.config import get_settings  # noqa: E402
from aurora.services.flag_prefix_config import configure_flag_prefixes  # noqa: E402


@pytest.fixture(autouse=True)
def _demo_flag_prefixes() -> None:
    settings = get_settings()
    original = list(settings.flag_prefixes or ["flag"])
    configure_flag_prefixes(["flag", "ctf", "qwxf", "susctf"])
    yield
    configure_flag_prefixes(original)


def test_demo_flow_creates_blackboard_and_debug_traces() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={
                "name": "smoke",
                "goal": "Verify the Aurora MVP loop.",
                "allowed_hosts": ["127.0.0.1"],
            },
        )
        assert created.status_code == 200
        project_id = created.json()["id"]

        demo = client.post(f"/api/projects/{project_id}/run-demo")
        assert demo.status_code == 200
        assert demo.json()["status"] == "completed"

        blackboard = client.get(f"/api/projects/{project_id}/blackboard")
        assert blackboard.status_code == 200
        body = blackboard.json()
        assert len(body["facts"]) >= 1
        assert len(body["attempts"]) == 1
        assert len(body["artifacts"]) >= 1
        assert len(body["checkpoints"]) == 1
        assert body["checkpoints"][0]["source"] == "fallback"
        assert len(body["checkpoints"][0]["generated_intent_ids"]) == 1
        reflected_intent = next(intent for intent in body["intents"] if intent["id"] == body["checkpoints"][0]["generated_intent_ids"][0])
        assert reflected_intent["parent_intent_id"] == body["checkpoints"][0]["intent_id"]

        contexts = client.get(f"/api/projects/{project_id}/debug/context-snapshots")
        assert contexts.status_code == 200
        assert contexts.json()[0]["estimated_tokens"] > 0
        assert contexts.json()[0]["sections_json"]["current_intent"]["budget"]["soft_timeout_seconds"] > 0

        traces = client.get(f"/api/projects/{project_id}/debug/llm-traces")
        assert traces.status_code == 200
        assert traces.json()[0]["decision_summary"]["next_tool_plan"] == ["sandbox.exec"]

        tool_traces = client.get(f"/api/projects/{project_id}/debug/tool-traces")
        assert tool_traces.status_code == 200
        assert tool_traces.json()[0]["tool_name"] == "sandbox.exec"

        artifact_id = body["artifacts"][0]["id"]
        artifact_content = client.get(f"/api/artifacts/{artifact_id}/content")
        assert artifact_content.status_code == 200
        assert "Aurora Kali-first sandbox smoke test" in artifact_content.json()["content"]


def test_manual_flag_acceptance_resumes_group_with_pending_items(monkeypatch) -> None:
    client = TestClient(create_app())
    with client:
        with Session(engine) as session:
            solved = Project(name="manual-solved", goal="solved", status="COMPLETED")
            pending = Project(name="manual-next", goal="next")
            group = ChallengeGroup(name="manual-resume", status="AWAITING_MANUAL_VALIDATION")
            session.add_all([solved, pending, group])
            session.commit()
            reviewed = ChallengeGroupItem(
                group_id=group.id,
                project_id=solved.id,
                position=1,
                status="AWAITING_MANUAL_VALIDATION",
                fused_status="AWAITING_MANUAL_VALIDATION",
                submission_status="AWAITING_MANUAL_VALIDATION",
            )
            next_item = ChallengeGroupItem(group_id=group.id, project_id=pending.id, position=2)
            finding = Finding(project_id=solved.id, title="Candidate flag: flag{manual_review}")
            session.add_all([reviewed, next_item, finding])
            session.commit()
            group_id, item_id = group.id, reviewed.id

        resumed: list[str] = []

        def resume(group_id: str) -> GroupRunState:
            resumed.append(group_id)
            return GroupRunState(group_id=group_id)

        monkeypatch.setattr("aurora.api.challenge_group_registry.resume_after_manual_validation", resume)
        response = client.post(
            f"/api/challenge-groups/{group_id}/items/{item_id}/flag-validation",
            json={"accepted": True},
        )

        assert response.status_code == 200
        assert resumed == [group_id]
        assert response.json()["background"]["status"] == "running"
        with Session(engine) as session:
            assert session.get(ChallengeGroup, group_id).status == "READY"
            assert session.get(ChallengeGroupItem, item_id).submission_status == "MANUALLY_ACCEPTED"


def test_project_flag_acceptance_advances_awaiting_group_item(monkeypatch) -> None:
    client = TestClient(create_app())
    with client:
        with Session(engine) as session:
            solved = Project(name="top-review", goal="solved", status="AWAITING_MANUAL_VALIDATION")
            pending = Project(name="top-next", goal="next")
            group = ChallengeGroup(name="top-resume", status="AWAITING_MANUAL_VALIDATION")
            session.add_all([solved, pending, group])
            session.commit()
            reviewed = ChallengeGroupItem(
                group_id=group.id,
                project_id=solved.id,
                position=1,
                status="AWAITING_MANUAL_VALIDATION",
                fused_status="AWAITING_MANUAL_VALIDATION",
                submission_status="AWAITING_MANUAL_VALIDATION",
            )
            next_item = ChallengeGroupItem(group_id=group.id, project_id=pending.id, position=2)
            finding = Finding(project_id=solved.id, title="Candidate flag: flag{top_review}")
            candidate = FlagCandidate(
                project_id=solved.id,
                value="flag{top_review}",
                value_hash="top-review-hash",
                status="AWAITING_MANUAL_VALIDATION",
                provenance_kind="OBSERVED",
            )
            session.add_all([reviewed, next_item, finding, candidate])
            session.commit()
            project_id, candidate_id, group_id, item_id = solved.id, candidate.id, group.id, reviewed.id

        resumed: list[str] = []
        monkeypatch.setattr(
            "aurora.api.challenge_group_registry.resume_after_manual_validation",
            lambda current_group_id: resumed.append(current_group_id) or GroupRunState(group_id=current_group_id),
        )
        response = client.post(
            f"/api/projects/{project_id}/flag-candidates/{candidate_id}/validation",
            json={"accepted": True},
        )

        assert response.status_code == 200
        assert response.json()["groups"] == [group_id]
        assert resumed == [group_id]
        with Session(engine) as session:
            assert session.get(ChallengeGroup, group_id).status == "READY"
            assert session.get(ChallengeGroupItem, item_id).submission_status == "MANUALLY_ACCEPTED"
            assert session.get(Project, project_id).status == "COMPLETED"


def test_deleting_project_removes_group_items_and_resets_import_candidate() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post("/api/projects", json={"name": "delete-project", "goal": "delete", "allowed_hosts": ["127.0.0.1"]})
        project_id = created.json()["id"]
        with Session(engine) as session:
            group = ChallengeGroup(name="shared")
            candidate = ImportCandidate(batch_id="batch_delete", title="candidate", challenge_url="https://example.test/task", project_id=project_id, confirmed=True)
            session.add_all([group, candidate])
            session.commit()
            session.add(ChallengeGroupItem(group_id=group.id, project_id=project_id, position=1))
            session.commit()
            group_id = group.id
            candidate_id = candidate.id

        deleted = client.delete(f"/api/projects/{project_id}")
        assert deleted.status_code == 200
        assert deleted.json()["removed_group_items"] == 1
        assert client.get(f"/api/projects/{project_id}").status_code == 404
        with Session(engine) as session:
            assert session.get(ChallengeGroup, group_id) is not None
            assert not session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.project_id == project_id)).all()
            candidate = session.get(ImportCandidate, candidate_id)
            assert candidate is not None and candidate.project_id is None and candidate.confirmed is False


def test_deleting_group_cascades_projects_and_removes_shared_group_items() -> None:
    client = TestClient(create_app())
    with client:
        first = client.post("/api/projects", json={"name": "group-first", "goal": "delete", "allowed_hosts": ["127.0.0.1"]}).json()["id"]
        second = client.post("/api/projects", json={"name": "group-second", "goal": "delete", "allowed_hosts": ["127.0.0.1"]}).json()["id"]
        with Session(engine) as session:
            primary = ChallengeGroup(name="primary")
            secondary = ChallengeGroup(name="secondary")
            session.add_all([primary, secondary])
            session.commit()
            session.add_all([
                ChallengeGroupItem(group_id=primary.id, project_id=first, position=1),
                ChallengeGroupItem(group_id=primary.id, project_id=second, position=2),
                ChallengeGroupItem(group_id=secondary.id, project_id=first, position=1),
            ])
            session.commit()
            primary_id, secondary_id = primary.id, secondary.id

        deleted = client.delete(f"/api/challenge-groups/{primary_id}")
        assert deleted.status_code == 200
        assert set(deleted.json()["deleted_project_ids"]) == {first, second}
        assert client.get(f"/api/challenge-groups/{primary_id}").status_code == 404
        assert client.get(f"/api/projects/{first}").status_code == 404
        assert client.get(f"/api/projects/{second}").status_code == 404
        with Session(engine) as session:
            assert session.get(ChallengeGroup, secondary_id) is not None
            assert not session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == secondary_id)).all()


def test_manual_tool_execution_not_gated_by_authorization() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={
                "name": "policy",
                "goal": "Verify policy checks.",
                "allowed_hosts": ["127.0.0.1"],
            },
        )
        assert created.status_code == 200
        project_id = created.json()["id"]

        # Authorization gating has been removed: an out-of-scope host is
        # allowed by policy and only fails later if it is unreachable.
        with Session(engine) as session:
            decision = PolicyEngine().check_tool_request(
                session,
                project_id=project_id,
                tool_name="http.request",
                request={"url": "http://8.8.8.8:18080/"},
            )
            assert decision.allowed is True

        allowed = client.post(
            f"/api/projects/{project_id}/tools/http.request/execute",
            json={"request": {"url": "http://127.0.0.1/", "timeout_seconds": 1}},
        )
        assert allowed.status_code == 200
        assert allowed.json()["trace_id"].startswith("tool_")

        tool_traces = client.get(f"/api/projects/{project_id}/debug/tool-traces")
        assert tool_traces.status_code == 200
        decisions = [trace["policy_decision"] for trace in tool_traces.json()]
        assert "deny" not in decisions


def test_fofa_query_not_gated_by_authorization() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "fofa-policy", "goal": "Verify FOFA authorization.", "allowed_hosts": ["127.0.0.1"]},
        )
        project_id = created.json()["id"]

        # Authorization scoping is removed; any FOFA query shape is allowed by
        # policy. Execution still needs a configured FOFA credential, which is
        # not present in tests, so only the policy decision is asserted here.
        with Session(engine) as session:
            for query in ('title="nginx"', 'host="example.com"'):
                decision = PolicyEngine().check_tool_request(
                    session,
                    project_id=project_id,
                    tool_name="fofa.search",
                    request={"query": query},
                )
                assert decision.allowed is True


def test_project_runtime_policy_requires_global_and_project_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_SUBAGENTS_ENABLED", "true")
    from aurora.config import get_settings

    get_settings.cache_clear()
    client = TestClient(create_app())
    with client:
        disabled = client.post(
            "/api/projects",
            json={"name": "child-off", "goal": "Check child defaults.", "allowed_hosts": ["127.0.0.1"]},
        )
        enabled = client.post(
            "/api/projects",
            json={"name": "child-on", "goal": "Check child opt-in.", "allowed_hosts": ["127.0.0.1"], "subagents_enabled": True},
        )
        assert disabled.status_code == 200
        assert enabled.status_code == 200
        off_policy = client.get(f"/api/projects/{disabled.json()['id']}/runtime-policy")
        on_policy = client.get(f"/api/projects/{enabled.json()['id']}/runtime-policy")
        assert off_policy.json()["subagents_enabled"] is False
        assert on_policy.json()["subagents_enabled"] is True
        assert on_policy.json()["max_subagents_concurrent"] == 2


def test_scheduler_runs_semantic_intent_tool_request() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={
                "name": "semantic-intent",
                "goal": "Verify semantic intent execution.",
                "allowed_hosts": ["127.0.0.1"],
            },
        )
        assert created.status_code == 200
        project_id = created.json()["id"]

        intent = client.post(
            f"/api/projects/{project_id}/intents",
            json={
                "objective": "Request the authorized local HTTP endpoint.",
                "capability_tags": ["http.request"],
                "priority": 2,
                "risk_level": "low",
                "tool_request": {"url": "http://127.0.0.1/", "timeout_seconds": 1},
            },
        )
        assert intent.status_code == 200
        intent_id = intent.json()["id"]

        result = client.post(f"/api/projects/{project_id}/scheduler/run-next")
        assert result.status_code == 200
        assert result.json()["intent_id"] == intent_id

        tool_traces = client.get(f"/api/projects/{project_id}/debug/tool-traces")
        assert tool_traces.status_code == 200
        assert tool_traces.json()[0]["tool_name"] == "http.request"

        traces = client.get(f"/api/projects/{project_id}/debug/llm-traces")
        assert traces.status_code == 200
        assert traces.json()[0]["decision_summary"]["next_tool_plan"] == ["http.request"]


def test_scheduler_runs_semantic_tool_intent() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={
                "name": "semantic-intent",
                "goal": "Run a semantic HTTP intent through the scheduler.",
                "allowed_hosts": ["127.0.0.1"],
            },
        )
        assert created.status_code == 200
        project_id = created.json()["id"]

        intent = client.post(
            f"/api/projects/{project_id}/intents",
            json={
                "objective": "Fetch the authorized local HTTP target.",
                "capability_tags": ["http.request"],
                "priority": 2,
                "risk_level": "low",
                "tool_request": {"url": "http://127.0.0.1/", "timeout_seconds": 1},
            },
        )
        assert intent.status_code == 200

        run_next = client.post(f"/api/projects/{project_id}/scheduler/run-next")
        assert run_next.status_code == 200
        assert run_next.json()["status"] == "completed"

        traces = client.get(f"/api/projects/{project_id}/debug/tool-traces")
        assert traces.status_code == 200
        first = traces.json()[0]
        assert first["tool_name"] == "http.request"
        assert first["policy_decision"] == "allow"
        assert first["artifact_refs"]

        llm_traces = client.get(f"/api/projects/{project_id}/debug/llm-traces")
        assert llm_traces.status_code == 200
        assert llm_traces.json()[0]["decision_summary"]["next_tool_plan"] == ["http.request"]

        events = client.get(f"/api/projects/{project_id}/events")
        assert events.status_code == 200
        event_types = {event["event_type"] for event in events.json()}
        assert {"worker.started", "context.built", "llm.completed", "tool.executed", "attempt.completed"}.issubset(event_types)


def test_imported_project_runs_without_a_verified_target() -> None:
    client = TestClient(create_app())
    with client:
        project = client.post(
            "/api/projects",
            json={"name": "offline-import", "goal": "Analyze imported files before a target is available.", "allowed_hosts": []},
        ).json()
        project_id = project["id"]
        with Session(engine) as session:
            imported = session.get(Project, project_id)
            assert imported is not None
            imported.target_verification_status = "NEEDS_SESSION"
            imported.target_verification_reason = "target session not available"
            session.add(imported)
            session.add(
                ImportCandidate(
                    batch_id="import_optional_target",
                    title="Offline challenge",
                    challenge_url="https://catalog.example/challenge/1",
                    project_id=project_id,
                    confirmed=True,
                )
            )
            session.commit()

        result = client.post(f"/api/projects/{project_id}/scheduler/run-next")
        assert result.status_code == 200
        assert result.json()["status"] == "completed"

        contexts = client.get(f"/api/projects/{project_id}/debug/context-snapshots").json()
        assert contexts[0]["sections_json"]["target_access"] == {
            "status": "NEEDS_SESSION",
            "url": None,
            "reason": "target session not available",
            "required_for_solver_start": False,
        }
        events = client.get(f"/api/projects/{project_id}/events").json()
        event_types = {event["event_type"] for event in events}
        assert "worker.started" in event_types
        assert "worker.preflight_blocked" not in event_types


def test_scheduler_heartbeat_and_reap_expired_leases() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "leases", "goal": "Verify lease lifecycle.", "allowed_hosts": ["127.0.0.1"]},
        )
        assert created.status_code == 200
        project_id = created.json()["id"]

        with Session(engine) as session:
            claimed = Scheduler().claim_next(session, project_id=project_id, lease_seconds=1)
            assert claimed is not None
            intent, worker = claimed

        heartbeat = client.post(f"/api/workers/{worker.id}/heartbeat", json={"lease_seconds": 30})
        assert heartbeat.status_code == 200
        assert heartbeat.json()["id"] == worker.id

        with Session(engine) as session:
            session.add(Attempt(project_id=project_id, intent_id=intent.id, worker_id=worker.id, status="RUNNING"))
            running_intent = session.get(Intent, intent.id)
            assert running_intent is not None
            running_intent.lease_expires_at = now_utc() - timedelta(seconds=1)
            session.add(running_intent)
            session.commit()

        reaped = client.post(f"/api/projects/{project_id}/scheduler/reap-expired")
        assert reaped.status_code == 200
        assert reaped.json()["reaped"] == 1

        with Session(engine) as session:
            timed_out_worker = session.get(Worker, worker.id)
            timed_out_attempt = session.exec(select(Attempt).where(Attempt.worker_id == worker.id)).one()
            retried_intent = session.get(Intent, intent.id)
            assert timed_out_worker is not None and timed_out_worker.status == "TIMEOUT"
            assert timed_out_attempt.status == "TIMEOUT"
            assert timed_out_attempt.finished_at is not None
            assert retried_intent is not None and retried_intent.status == "PENDING"
            checkpoint = session.exec(select(AttemptCheckpoint).where(AttemptCheckpoint.attempt_id == timed_out_attempt.id)).one()
            assert checkpoint.source == "fallback"
            assert checkpoint.generated_intent_ids == []

        events = client.get(f"/api/projects/{project_id}/events")
        assert events.status_code == 200
        event_types = {event["event_type"] for event in events.json()}
        assert "worker.heartbeat" in event_types
        assert "intent.lease_expired" in event_types
        assert {"worker.timed_out", "attempt.timed_out"}.issubset(event_types)
        assert "reflection.fallback" in event_types


def test_reflection_is_the_only_automatic_intent_authority_and_is_idempotent() -> None:
    client = TestClient(create_app())
    with client:
        project_id = client.post(
            "/api/projects",
            json={"name": "reflection-authority", "goal": "Verify reflection intent ownership.", "allowed_hosts": ["127.0.0.1"]},
        ).json()["id"]
        with Session(engine) as session:
            source_intent = session.exec(select(Intent).where(Intent.project_id == project_id)).one()
            worker = Worker(project_id=project_id, intent_id=source_intent.id, status="COMPLETED")
            session.add(worker)
            session.commit()
            attempt = Attempt(project_id=project_id, intent_id=source_intent.id, worker_id=worker.id)
            session.add(attempt)
            session.commit()
            session.refresh(attempt)
            trace = LLMTrace(
                project_id=project_id,
                worker_id=worker.id,
                intent_id=source_intent.id,
                context_snapshot_id="ctx_reflection_authority",
                prompt_hash="reflection-test",
                model="test",
            )
            output = {
                "status": "partial",
                "summary": "One route remains.",
                "suggested_intents": [
                    {
                        "objective": "Inspect the blackboard evidence.",
                        "capability_tags": ["blackboard.query"],
                        "priority": 2.0,
                        "budget": {"model_role": "planner", "max_tool_calls": 2, "unsafe_key": "discarded"},
                    },
                    {"objective": "Rejected unsafe capability.", "capability_tags": ["unknown.exec"], "priority": 9.0},
                ],
                "decision_summary": {"next_tool_plan": ["blackboard.query"]},
            }
            ResultProcessor().apply(session, attempt=attempt, output=output, llm_trace=trace)
            assert session.exec(select(Intent).where(Intent.parent_intent_id == source_intent.id)).all() == []

            first = RoundReflectionService().create(session, attempt=attempt, output=output, budget={})
            second = RoundReflectionService().create(session, attempt=attempt, output=output, budget={})
            assert first.id == second.id
            assert len(first.generated_intent_ids) == 1
            generated = session.get(Intent, first.generated_intent_ids[0])
            assert generated is not None and generated.capability_tags == ["blackboard.query"]
            assert generated.budget == {"model_role": "planner", "max_tool_calls": 2}
            assert len(session.exec(select(AttemptCheckpoint).where(AttemptCheckpoint.attempt_id == attempt.id)).all()) == 1


def test_completed_project_reflects_without_generating_intents() -> None:
    client = TestClient(create_app())
    with client:
        project_id = client.post(
            "/api/projects",
            json={"name": "final-reflection", "goal": "Keep a final reflection.", "allowed_hosts": ["127.0.0.1"]},
        ).json()["id"]
        with Session(engine) as session:
            intent = session.exec(select(Intent).where(Intent.project_id == project_id)).one()
            worker = Worker(project_id=project_id, intent_id=intent.id, status="COMPLETED")
            session.add(worker)
            session.commit()
            attempt = Attempt(project_id=project_id, intent_id=intent.id, worker_id=worker.id, status="SUCCESS", result_summary="Solved")
            session.add(attempt)
            project = session.get(Project, project_id)
            assert project is not None
            project.status = "COMPLETED"
            session.add(project)
            session.commit()
            session.refresh(attempt)
            checkpoint = RoundReflectionService().create(
                session,
                attempt=attempt,
                output={
                    "status": "success",
                    "summary": "Solved",
                    "suggested_intents": [{"objective": "Do unnecessary work", "capability_tags": ["blackboard.query"]}],
                    "decision_summary": {"next_tool_plan": []},
                },
                budget={},
            )
            assert checkpoint.generated_intent_ids == []


def test_planner_reflection_caps_generated_intents_at_three(monkeypatch) -> None:
    client = TestClient(create_app())
    with client:
        project_id = client.post(
            "/api/projects",
            json={"name": "planner-reflection", "goal": "Bound reflected branches.", "allowed_hosts": ["127.0.0.1"]},
        ).json()["id"]
        with Session(engine) as session:
            intent = session.exec(select(Intent).where(Intent.project_id == project_id)).one()
            worker = Worker(project_id=project_id, intent_id=intent.id, status="COMPLETED")
            session.add(worker)
            session.commit()
            attempt = Attempt(project_id=project_id, intent_id=intent.id, worker_id=worker.id, status="PARTIAL", result_summary="Reflect")
            session.add(attempt)
            session.commit()
            session.refresh(attempt)
            planned = {
                "summary": "Planner reflection",
                "conclusions": [],
                "hypotheses": [],
                "failed_routes": [],
                "next_steps": ["one", "two", "three"],
                "intents": [
                    {"objective": f"Reflected branch {index}", "capabilities": ["blackboard.query"], "priority": index}
                    for index in range(1, 5)
                ],
            }
            monkeypatch.setattr(RoundReflectionService, "_planner_summary", lambda self, fallback: planned)
            checkpoint = RoundReflectionService().create(session, attempt=attempt, output={}, budget={})
            assert checkpoint.source == "planner"
            assert len(checkpoint.generated_intent_ids) == 3


def test_blackboard_suppresses_duplicate_intents_and_merges_facts() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "dedup", "goal": "Verify blackboard hygiene.", "allowed_hosts": ["127.0.0.1"]},
        )
        assert created.status_code == 200
        project_id = created.json()["id"]

        payload = {
            "objective": "Fetch the authorized local HTTP target.",
            "capability_tags": ["http.request"],
            "priority": 2,
            "risk_level": "low",
            "tool_request": {"url": "http://127.0.0.1/", "timeout_seconds": 1},
        }
        first = client.post(f"/api/projects/{project_id}/intents", json=payload)
        second = client.post(f"/api/projects/{project_id}/intents", json=payload)
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]

        intents = client.get(f"/api/projects/{project_id}/intents")
        runnable_matches = [intent for intent in intents.json() if intent["objective"] == payload["objective"]]
        assert len(runnable_matches) == 1

        run_once = client.post(f"/api/projects/{project_id}/scheduler/run-next")
        run_twice = client.post(f"/api/projects/{project_id}/scheduler/run-next")
        assert run_once.status_code == 200
        assert run_twice.status_code == 200

        blackboard = client.get(f"/api/projects/{project_id}/blackboard")
        assert blackboard.status_code == 200
        statements = [fact["statement"].lower() for fact in blackboard.json()["facts"]]
        assert len(statements) == len(set(statements))

        events = client.get(f"/api/projects/{project_id}/events")
        assert events.status_code == 200
        event_types = {event["event_type"] for event in events.json()}
        assert "intent.duplicate_suppressed" in event_types
        assert "fact.merged" in event_types or "fact.created" in event_types


def test_observer_escalates_policy_denial() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "observer-deny", "goal": "Verify observer denial detection.", "allowed_hosts": ["127.0.0.1"]},
        )
        assert created.status_code == 200
        project_id = created.json()["id"]

        # capability.request is unsupported and still records a policy deny,
        # which Observer must escalate on.
        denied = client.post(
            f"/api/projects/{project_id}/tools/capability.request/execute",
            json={"request": {"capability": "target.url"}},
        )
        assert denied.status_code == 200
        assert denied.json()["success"] is False

        observer = client.post(f"/api/projects/{project_id}/observer/run")
        assert observer.status_code == 200
        assert observer.json()["decision"] == "ESCALATE"

        events = client.get(f"/api/projects/{project_id}/events")
        assert events.status_code == 200
        assert "observer.decision" in {event["event_type"] for event in events.json()}


def test_browser_execution_error_does_not_escalate_as_policy_denial(monkeypatch) -> None:
    client = TestClient(create_app())
    with client:
        project_id = client.post(
            "/api/projects",
            json={"name": "browser-error", "goal": "browser error classification", "allowed_hosts": ["127.0.0.1"]},
        ).json()["id"]

        def failed_browser(*args, **kwargs):
            from aurora.services.browser_interaction import BrowserInteractionResult
            return BrowserInteractionResult(False, "browser interaction failed: timeout", [], [])

        monkeypatch.setattr(BrowserInteractionService, "execute", failed_browser)
        with Session(engine) as session:
            result = CapabilityGateway().execute(
                session,
                project_id=project_id,
                worker_id=None,
                intent_id=None,
                attempt_id=None,
                tool_name="browser.interact",
                    request={"url": "http://127.0.0.1/challenges"},
            )
        # The trace classification is what Observer consumes; execution
        # failures must remain distinguishable from policy denials.
        with Session(engine) as session:
            trace = session.get(ToolTrace, result.trace_id)
            assert trace is not None
            assert trace.policy_decision == "execution_error"


def test_semantic_artifact_ref_is_resolved_and_invalid_request_does_not_escape(tmp_path) -> None:
    project_id = "proj_semantic_artifact"
    with Session(engine) as session:
        session.add(Project(id=project_id, name="semantic artifact", goal="inspect evidence"))
        session.add(AuthorizationScope(project_id=project_id))
        session.commit()
        artifact = ArtifactStore(tmp_path).write_text(
            session,
            project_id=project_id,
            content="evidence",
            summary="evidence",
        )
        gateway = CapabilityGateway(artifact_store=ArtifactStore(tmp_path), command_runner=LocalCommandRunner())
        resolved = gateway.execute(
            session,
            project_id=project_id,
            tool_name="forensic.inspect",
            request={"artifact_ref": artifact.id},
        )
        invalid = gateway.execute(
            session,
            project_id=project_id,
            tool_name="forensic.inspect",
            request={},
        )

        assert resolved.trace_id
        assert "path contains unsafe characters" in invalid.summary
        assert session.get(ToolTrace, invalid.trace_id).policy_decision == "execution_error"


def test_repeat_failure_count_matches_non_adjacent_failed_routes() -> None:
    project_id = "proj_repeat_route"
    intent_id = "intent_repeat_route"
    repeated_request = {"url": "http://127.0.0.1/challenges"}
    with Session(engine) as session:
        session.add_all([
            ToolTrace(project_id=project_id, intent_id=intent_id, tool_name="browser.interact", request_json=repeated_request, policy_decision="execution_error"),
            ToolTrace(project_id=project_id, intent_id=intent_id, tool_name="blackboard.query", request_json={}, policy_decision="allow", exit_code=0),
            ToolTrace(project_id=project_id, intent_id=intent_id, tool_name="browser.interact", request_json=repeated_request, policy_decision="execution_error"),
        ])
        session.commit()

        assert _repeat_failure_count(
            session,
            project_id=project_id,
            tool_name="browser.interact",
            request=repeated_request,
        ) == 2


def test_browser_route_fingerprint_changes_with_session_cookie() -> None:
    from aurora.services.browser_sessions import browser_session_registry

    project_id = "proj_browser_fingerprint"
    request = {"url": "https://example.test/challenge"}
    browser_session_registry.set_project_session(project_id=project_id, source_url=request["url"], cookie="session=one")
    first = _route_request(project_id, "browser.interact", request)
    browser_session_registry.set_project_session(project_id=project_id, source_url=request["url"], cookie="session=two")
    second = _route_request(project_id, "browser.interact", request)
    browser_session_registry.clear_project_session(project_id)

    assert first["_aurora_browser_session"] != second["_aurora_browser_session"]
    assert "session=one" not in str(first)


def test_scheduler_suppresses_same_failed_route_across_intents(monkeypatch) -> None:
    from aurora.services.browser_interaction import BrowserInteractionResult

    def failed_browser(*args, **kwargs):
        return BrowserInteractionResult(False, "no labeled target address or launch control found", [], [])

    monkeypatch.setattr(BrowserInteractionService, "execute", failed_browser)
    client = TestClient(create_app())
    with client:
        project_id = client.post(
            "/api/projects",
            json={"name": "repeat-browser-route", "goal": "Suppress a repeated browser dead end.", "allowed_hosts": ["127.0.0.1"]},
        ).json()["id"]
        route = {"url": "http://127.0.0.1/challenge"}
        results = []
        for index in range(3):
            created = client.post(
                f"/api/projects/{project_id}/intents",
                json={
                    "objective": f"Inspect the same browser route attempt {index}",
                    "capability_tags": ["browser.interact"],
                    "priority": 10,
                    "risk_level": "low",
                    "tool_request": route,
                },
            )
            assert created.status_code == 200
            results.append(client.post(f"/api/projects/{project_id}/scheduler/run-next").json())

        traces = client.get(f"/api/projects/{project_id}/debug/tool-traces").json()
        events = client.get(f"/api/projects/{project_id}/events").json()

    assert len([trace for trace in traces if trace["tool_name"] == "browser.interact"]) == 2
    assert results[2]["tool_calls"][0]["skipped"] is True
    assert any(event["event_type"] == "tool.skipped.repeat_failure" for event in events)


def test_observer_redirects_duplicate_tool_calls() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "observer-duplicate", "goal": "Verify observer duplicate detection.", "allowed_hosts": ["127.0.0.1"]},
        )
        assert created.status_code == 200
        project_id = created.json()["id"]
        payload = {"request": {"command": "printf 'repeat\\n'", "cwd": ".", "timeout_seconds": 5}}

        first = client.post(f"/api/projects/{project_id}/tools/sandbox.exec/execute", json=payload)
        second = client.post(f"/api/projects/{project_id}/tools/sandbox.exec/execute", json=payload)
        assert first.status_code == 200
        assert second.status_code == 200

        observer = client.post(f"/api/projects/{project_id}/observer/run")
        assert observer.status_code == 200
        assert observer.json()["decision"] == "REDIRECT"


def test_observer_does_not_redirect_non_consecutive_duplicate_tool_calls() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "observer-non-consecutive", "goal": "Verify observer streak detection.", "allowed_hosts": ["127.0.0.1"]},
        )
        assert created.status_code == 200
        project_id = created.json()["id"]
        repeated = {"request": {"command": "printf 'repeat\\n'", "cwd": ".", "timeout_seconds": 5}}
        different = {"request": {"command": "printf 'different\\n'", "cwd": ".", "timeout_seconds": 5}}

        assert client.post(f"/api/projects/{project_id}/tools/sandbox.exec/execute", json=repeated).status_code == 200
        assert client.post(f"/api/projects/{project_id}/tools/sandbox.exec/execute", json=different).status_code == 200
        assert client.post(f"/api/projects/{project_id}/tools/sandbox.exec/execute", json=repeated).status_code == 200

        observer = client.post(f"/api/projects/{project_id}/observer/run")
        assert observer.status_code == 200
        assert observer.json()["decision"] == "CONTINUE"


def test_manager_generates_intent_from_url_hint_without_duplicates() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={
                "name": "manager-hint",
                "goal": "Verify manager hint planning.",
                "allowed_hosts": ["127.0.0.1"],
                "hint": "Start with http://127.0.0.1/ and inspect the response.",
            },
        )
        assert created.status_code == 200
        project_id = created.json()["id"]

        first = client.post(f"/api/projects/{project_id}/manager/run")
        second = client.post(f"/api/projects/{project_id}/manager/run")
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["status"] == "PROPOSED"

        intents = client.get(f"/api/projects/{project_id}/intents")
        assert intents.status_code == 200
        hint_intents = [intent for intent in intents.json() if "http://127.0.0.1/" in intent["objective"]]
        assert len(hint_intents) == 1
        assert hint_intents[0]["capability_tags"] == ["http.request"]
        assert hint_intents[0]["budget"]["tool_request"]["url"] == "http://127.0.0.1/"

        events = client.get(f"/api/projects/{project_id}/events")
        assert events.status_code == 200
        assert "manager.decision" in {event["event_type"] for event in events.json()}


def test_manager_does_not_create_a_second_planning_loop_after_checkpoint() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "manager-stop", "goal": "Stop without an explicit follow-up", "allowed_hosts": ["127.0.0.1"]},
        )
        project_id = created.json()["id"]

        with Session(engine) as session:
            intent = session.exec(select(Intent).where(Intent.project_id == project_id)).one()
            intent.status = "COMPLETED"
            worker = Worker(project_id=project_id, intent_id=intent.id, status="COMPLETED")
            attempt = Attempt(project_id=project_id, intent_id=intent.id, worker_id=worker.id, status="COMPLETED")
            session.add_all([intent, worker, attempt])
            session.commit()
            session.add(AttemptCheckpoint(
                project_id=project_id,
                intent_id=intent.id,
                worker_id=worker.id,
                attempt_id=attempt.id,
                summary="No explicit follow-up was proposed.",
                next_steps=["Do not synthesize another planner loop."],
                generated_intent_ids=[],
            ))
            session.commit()

        manager = client.post(f"/api/projects/{project_id}/manager/run")
        intents = client.get(f"/api/projects/{project_id}/intents").json()

    assert manager.status_code == 200
    assert manager.json()["status"] == "NOOP"
    assert not any(intent["status"] == "PENDING" for intent in intents)


def test_manager_authors_continuation_from_partial_checkpoint() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "manager-resume", "goal": "Continue evidence-backed work", "allowed_hosts": ["127.0.0.1"]},
        )
        project_id = created.json()["id"]
        with Session(engine) as session:
            intent = session.exec(select(Intent).where(Intent.project_id == project_id)).one()
            intent.status = "COMPLETED"
            worker = Worker(project_id=project_id, intent_id=intent.id, status="COMPLETED")
            attempt = Attempt(project_id=project_id, intent_id=intent.id, worker_id=worker.id, status="PARTIAL")
            session.add_all([intent, worker, attempt])
            session.commit()
            session.add(AttemptCheckpoint(
                project_id=project_id,
                intent_id=intent.id,
                worker_id=worker.id,
                attempt_id=attempt.id,
                status="PARTIAL",
                summary="A discriminating experiment remains.",
                next_steps=["Inspect the imported artifact header with a bounded command"],
                generated_intent_ids=[],
                budget_json={"phase": 1},
            ))
            session.commit()

        manager = client.post(f"/api/projects/{project_id}/manager/run")
        intents = client.get(f"/api/projects/{project_id}/intents").json()

    continuation = next(intent for intent in intents if intent["status"] == "PENDING")
    assert manager.json()["status"] == "PROPOSED"
    assert continuation["parent_intent_id"] is not None
    assert continuation["budget"]["phase"] == 2
    assert continuation["capability_tags"] == ["blackboard.query", "sandbox.exec"]


def test_post_creation_hint_is_consumed_by_manager() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "hint-injection", "goal": "Verify injected hints.", "allowed_hosts": ["127.0.0.1"]},
        )
        assert created.status_code == 200
        project_id = created.json()["id"]

        hint = client.post(
            f"/api/projects/{project_id}/hints",
            json={"content": "New target appeared at http://127.0.0.1/", "source": "user"},
        )
        assert hint.status_code == 200
        assert hint.json()["consumed"] is False

        hints_before = client.get(f"/api/projects/{project_id}/hints")
        assert hints_before.status_code == 200
        assert len(hints_before.json()) == 1

        manager = client.post(f"/api/projects/{project_id}/manager/run")
        assert manager.status_code == 200
        assert manager.json()["status"] == "PROPOSED"

        hints_after = client.get(f"/api/projects/{project_id}/hints")
        assert hints_after.status_code == 200
        assert hints_after.json()[0]["consumed"] is True

        events = client.get(f"/api/projects/{project_id}/events")
        assert events.status_code == 200
        event_types = {event["event_type"] for event in events.json()}
        assert "hint.created" in event_types
        assert "manager.decision" in event_types


def test_candidate_flag_creates_finding_and_waits_for_final_validation() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "flag-flow", "goal": "Recover a candidate flag.", "allowed_hosts": ["127.0.0.1"]},
        )
        assert created.status_code == 200
        project_id = created.json()["id"]

        intent = client.post(
            f"/api/projects/{project_id}/intents",
            json={
                "objective": "Emit a candidate flag for validation.",
                "capability_tags": ["sandbox.exec"],
                "priority": 5,
                "risk_level": "low",
                "tool_request": {"command": "printf 'flag{aurora_demo}'", "cwd": ".", "timeout_seconds": 5},
            },
        )
        assert intent.status_code == 200

        run_next = client.post(f"/api/projects/{project_id}/scheduler/run-next")
        assert run_next.status_code == 200
        assert run_next.json()["status"] == "completed"

        project = client.get(f"/api/projects/{project_id}")
        assert project.status_code == 200
        assert project.json()["status"] == "FLAG_READY"

        findings = client.get(f"/api/projects/{project_id}/findings")
        assert findings.status_code == 200
        assert findings.json()[0]["title"] == "Candidate flag: flag{aurora_demo}"

        events = client.get(f"/api/projects/{project_id}/events")
        assert events.status_code == 200
        event_types = {event["event_type"] for event in events.json()}
        assert "finding.flag_candidate" in event_types
        assert "project.flag_ready" in event_types
        assert "project.completed" not in event_types


def test_unreplayed_derived_flag_is_rejected() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "derived-flag", "goal": "Archive a decoded flag.", "allowed_hosts": ["127.0.0.1"]},
        )
        project_id = created.json()["id"]

        with Session(engine) as session:
            intent = Intent(project_id=project_id, objective="Decode the ciphertext", status="RUNNING")
            worker = Worker(project_id=project_id, intent_id=intent.id, status="RUNNING")
            attempt = Attempt(project_id=project_id, intent_id=intent.id, worker_id=worker.id)
            trace = LLMTrace(project_id=project_id, worker_id=worker.id, intent_id=intent.id, context_snapshot_id="ctx_test", prompt_hash="test")
            session.add_all([intent, worker, attempt, trace])
            session.commit()
            source = ArtifactStore().write_text(
                session,
                project_id=project_id,
                source_attempt_id=attempt.id,
                artifact_type="sandbox-result",
                summary="ROT13 ciphertext",
                content="Fhfpgs{3r811r068s5pr27ro4op1p37723q7rr2}",
            )

            value = "Susctf{3e811e068f5ce27eb4bc1c37723d7ee2}"
            ResultProcessor().apply(
                session,
                attempt=attempt,
                llm_trace=trace,
                output={
                    "status": "success",
                    "artifact_refs": [source.id],
                    "candidate_flags": [value],
                    "fact_candidates": [{
                        "statement": f"ROT13 decode yields {value}",
                        "evidence_refs": [source.id],
                        "evidence_items": [{"description": "Applying ROT13 to the ciphertext produced the candidate flag.", "artifact_refs": []}],
                    }],
                },
            )
            assert session.exec(select(Fact).where(Fact.statement == f"ROT13 decode yields {value}")).first() is None

        project = client.get(f"/api/projects/{project_id}")
        assert project.json()["status"] == "ACTIVE"
        findings = client.get(f"/api/projects/{project_id}/findings")
        assert findings.json() == []


def test_event_specific_prefix_without_evidence_is_rejected() -> None:
    client = TestClient(create_app())
    with client:
        project_id = client.post(
            "/api/projects",
            json={"name": "event-prefix", "goal": "Accept the recovered event flag.", "allowed_hosts": ["127.0.0.1"]},
        ).json()["id"]

        with Session(engine) as session:
            intent = Intent(project_id=project_id, objective="Recover flag", status="RUNNING")
            worker = Worker(project_id=project_id, intent_id=intent.id, status="RUNNING")
            attempt = Attempt(project_id=project_id, intent_id=intent.id, worker_id=worker.id)
            trace = LLMTrace(project_id=project_id, worker_id=worker.id, intent_id=intent.id, context_snapshot_id="ctx_test", prompt_hash="test")
            session.add_all([intent, worker, attempt, trace])
            session.commit()
            ResultProcessor().apply(
                session,
                attempt=attempt,
                llm_trace=trace,
                output={"status": "success", "candidate_flags": ["qwxf{you_say_chick_beautiful?}"]},
            )

        assert client.get(f"/api/projects/{project_id}").json()["status"] == "ACTIVE"
        assert client.get(f"/api/projects/{project_id}/findings").json() == []


def test_decoy_flag_returns_continue_feedback_and_deep_investigation_intent() -> None:
    client = TestClient(create_app())
    with client:
        project_id = client.post(
            "/api/projects",
            json={"name": "decoy-flag", "goal": "Find the real flag.", "allowed_hosts": ["127.0.0.1"]},
        ).json()["id"]
        value = "qwxf{this_is_a_fake_flag}"

        with Session(engine) as session:
            intent = Intent(project_id=project_id, objective="Validate candidate", status="RUNNING")
            worker = Worker(project_id=project_id, intent_id=intent.id, status="RUNNING")
            attempt = Attempt(project_id=project_id, intent_id=intent.id, worker_id=worker.id)
            trace = LLMTrace(project_id=project_id, worker_id=worker.id, intent_id=intent.id, context_snapshot_id="ctx_test", prompt_hash="test")
            session.add_all([intent, worker, attempt, trace])
            session.commit()
            ResultProcessor().apply(
                session,
                attempt=attempt,
                llm_trace=trace,
                output={
                    "status": "success",
                    "candidate_flags": [value],
                    "fact_candidates": [{"statement": f"Challenge solved with {value}", "confidence": 1.0}],
                },
            )
            session.refresh(attempt)
            assert attempt.status == "PARTIAL"
            assert "Do not submit it again" in (attempt.result_summary or "")

        board = client.get(f"/api/projects/{project_id}/blackboard").json()
        assert board["project"]["status"] == "ACTIVE"
        assert client.get(f"/api/projects/{project_id}/findings").json() == []
        feedback = [fact for fact in board["facts"] if fact["category"] == "flag_validation_feedback"]
        assert len(feedback) == 1
        assert value in feedback[0]["statement"] and "continue investigating" in feedback[0]["statement"]
        assert not any("Challenge solved" in fact["statement"] for fact in board["facts"])
        deep_intents = [intent for intent in board["intents"] if value in intent["objective"]]
        assert len(deep_intents) == 1 and deep_intents[0]["status"] == "PENDING"
        events = client.get(f"/api/projects/{project_id}/events").json()
        decoy_event = next(event for event in events if event["event_type"] == "finding.flag_candidate_decoy")
        assert decoy_event["payload_json"]["continue"] is True


def test_harness_parses_final_result_after_progress_logs() -> None:
    value = "Susctf{3e811e068f5ce27eb4bc1c37723d7ee2}"
    output = f'''progress: decoded ciphertext
```json
{{
  "status": "success",
  "summary": "Recovered {value}",
  "candidate_flags": ["{value}"],
  "fact_candidates": [{{"statement": "Decoded {value}"}}],
  "decision_summary": {{"selected_intent": "decode", "reason_summary": "ROT13", "next_tool_plan": []}}
}}
```
'''

    parsed = CodexHarnessRuntime()._parse_json(output)

    assert parsed["candidate_flags"] == [value]
    assert parsed["fact_candidates"][0]["statement"] == f"Decoded {value}"


def test_completed_project_cancels_pending_intents_and_blocks_run_next() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "completion-guard", "goal": "Stop after flag.", "allowed_hosts": ["127.0.0.1"]},
        )
        assert created.status_code == 200
        project_id = created.json()["id"]

        flag_intent = client.post(
            f"/api/projects/{project_id}/intents",
            json={
                "objective": "Emit a candidate flag.",
                "capability_tags": ["sandbox.exec"],
                "priority": 10,
                "risk_level": "low",
                "tool_request": {"command": "printf 'flag{stop_now}'", "cwd": ".", "timeout_seconds": 5},
            },
        )
        extra_intent = client.post(
            f"/api/projects/{project_id}/intents",
            json={
                "objective": "This should be cancelled after completion.",
                "capability_tags": ["sandbox.exec"],
                "priority": 1,
                "risk_level": "low",
                "tool_request": {"command": "printf 'should not run'", "cwd": ".", "timeout_seconds": 5},
            },
        )
        assert flag_intent.status_code == 200
        assert extra_intent.status_code == 200

        run_flag = client.post(f"/api/projects/{project_id}/scheduler/run-next")
        assert run_flag.status_code == 200
        assert run_flag.json()["status"] == "completed"

        candidate = client.get(f"/api/projects/{project_id}/flag-candidates").json()[0]
        accepted = client.post(
            f"/api/projects/{project_id}/flag-candidates/{candidate['id']}/validation",
            json={"accepted": True},
        )
        assert accepted.status_code == 200

        run_again = client.post(f"/api/projects/{project_id}/scheduler/run-next")
        assert run_again.status_code == 200
        assert run_again.json()["status"] == "project_completed"

        intents = client.get(f"/api/projects/{project_id}/intents")
        assert intents.status_code == 200
        statuses = {intent["objective"]: intent["status"] for intent in intents.json()}
        assert statuses["This should be cancelled after completion."] == "CANCELLED"

        events = client.get(f"/api/projects/{project_id}/events")
        assert events.status_code == 200
        completed_events = [event for event in events.json() if event["event_type"] == "project.completed"]
        assert completed_events
        assert extra_intent.json()["id"] in completed_events[0]["payload_json"]["cancelled_intent_ids"]


def test_completed_project_rejects_new_intents_and_hints() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "mutation-guard", "goal": "Complete then reject mutation.", "allowed_hosts": ["127.0.0.1"]},
        )
        assert created.status_code == 200
        project_id = created.json()["id"]

        intent = client.post(
            f"/api/projects/{project_id}/intents",
            json={
                "objective": "Emit a candidate flag.",
                "capability_tags": ["sandbox.exec"],
                "priority": 10,
                "risk_level": "low",
                "tool_request": {"command": "printf 'flag{locked}'", "cwd": ".", "timeout_seconds": 5},
            },
        )
        assert intent.status_code == 200
        assert client.post(f"/api/projects/{project_id}/scheduler/run-next").status_code == 200

        candidate = client.get(f"/api/projects/{project_id}/flag-candidates").json()[0]
        assert client.post(
            f"/api/projects/{project_id}/flag-candidates/{candidate['id']}/validation",
            json={"accepted": True},
        ).status_code == 200

        new_intent = client.post(
            f"/api/projects/{project_id}/intents",
            json={"objective": "Should fail", "capability_tags": ["sandbox.exec"]},
        )
        new_hint = client.post(f"/api/projects/{project_id}/hints", json={"content": "Should fail"})
        assert new_intent.status_code == 409
        assert new_hint.status_code == 409


def test_project_summary_supports_acceptance_review() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "summary", "goal": "Review final summary.", "allowed_hosts": ["127.0.0.1"]},
        )
        assert created.status_code == 200
        project_id = created.json()["id"]

        intent = client.post(
            f"/api/projects/{project_id}/intents",
            json={
                "objective": "Emit a candidate flag for summary.",
                "capability_tags": ["sandbox.exec"],
                "priority": 10,
                "risk_level": "low",
                "tool_request": {"command": "printf 'ctf{summary}'", "cwd": ".", "timeout_seconds": 5},
            },
        )
        assert intent.status_code == 200
        assert client.post(f"/api/projects/{project_id}/scheduler/run-next").status_code == 200

        summary = client.get(f"/api/projects/{project_id}/summary")
        assert summary.status_code == 200
        body = summary.json()
        assert body["project"]["status"] == "FLAG_READY"
        assert body["counts"]["findings"] == 1
        assert body["flag_candidates"][0]["status"] == "LOCAL_VERIFIED"
        assert body["counts"]["events_returned"] >= 1
        assert body["findings"][0]["title"] == "Candidate flag: ctf{summary}"
        assert body["latest_context_snapshot"] is not None


def test_placeholder_flag_from_transcript_cannot_complete_a_project() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "placeholder", "goal": "Reject schema placeholders.", "allowed_hosts": ["127.0.0.1"]},
        )
        project_id = created.json()["id"]

        with Session(engine) as session:
            project = session.get(Project, project_id)
            assert project is not None
            intent = Intent(project_id=project_id, objective="Validate a flag", status="RUNNING")
            worker = Worker(project_id=project_id, intent_id=intent.id, status="RUNNING")
            attempt = Attempt(project_id=project_id, intent_id=intent.id, worker_id=worker.id)
            trace = LLMTrace(
                project_id=project_id,
                worker_id=worker.id,
                intent_id=intent.id,
                context_snapshot_id="ctx_test",
                prompt_hash="test",
            )
            session.add_all([intent, worker, attempt, trace])
            session.commit()

            transcript = ArtifactStore().write_text(
                session,
                project_id=project_id,
                source_attempt_id=attempt.id,
                artifact_type="codex-transcript",
                summary="output schema",
                content='{"candidate_flags": ["flag{...}"]}',
            )
            assert FlagValidator().extract_candidate_flags(session, artifact_refs=[transcript.id]) == []

            ResultProcessor().apply(
                session,
                attempt=attempt,
                llm_trace=trace,
                output={"status": "partial", "candidate_flags": [{"value": "flag{...}", "artifact_ref": transcript.id}]},
            )
            session.refresh(project)
            assert project.status == "ACTIVE"
            assert client.get(f"/api/projects/{project_id}/findings").json() == []


def test_masked_flag_cannot_be_extracted_or_submitted() -> None:
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "masked-flag", "goal": "Reject masked flag output.", "allowed_hosts": ["127.0.0.1"]},
        )
        project_id = created.json()["id"]

        with Session(engine) as session:
            project = session.get(Project, project_id)
            assert project is not None
            intent = Intent(project_id=project_id, objective="Validate a masked flag", status="RUNNING")
            worker = Worker(project_id=project_id, intent_id=intent.id, status="RUNNING")
            attempt = Attempt(project_id=project_id, intent_id=intent.id, worker_id=worker.id)
            trace = LLMTrace(
                project_id=project_id,
                worker_id=worker.id,
                intent_id=intent.id,
                context_snapshot_id="ctx_masked_test",
                prompt_hash="masked-test",
            )
            session.add_all([intent, worker, attempt, trace])
            session.commit()

            artifact = ArtifactStore().write_text(
                session,
                project_id=project_id,
                source_attempt_id=attempt.id,
                artifact_type="tool-output",
                summary="masked tool output",
                content="observed flag{*****}",
            )
            assert FlagValidator().extract_candidate_flags(session, artifact_refs=[artifact.id]) == []

            ResultProcessor().apply(
                session,
                attempt=attempt,
                llm_trace=trace,
                output={"status": "success", "candidate_flags": [{"value": "flag{*****}", "artifact_ref": artifact.id}]},
            )
            session.refresh(project)
            assert project.status == "ACTIVE"
            events = session.exec(select(WorkerEvent).where(WorkerEvent.attempt_id == attempt.id)).all()
            assert any(event.event_type == "finding.flag_candidate_rejected" for event in events)
            assert not any(event.event_type == "finding.flag_candidate" for event in events)
            feedback = session.exec(
                select(Fact).where(Fact.project_id == project_id, Fact.category == "flag_validation_feedback")
            ).all()
            assert len(feedback) == 1
            assert "flag{*****}" in feedback[0].statement
            follow_up = session.exec(
                select(Intent).where(Intent.project_id == project_id, Intent.status == "PENDING")
            ).all()
            assert any("flag{*****}" in item.objective for item in follow_up)
            session.refresh(attempt)
            assert attempt.status == "PARTIAL"
            assert client.get(f"/api/projects/{project_id}/findings").json() == []


def test_evidence_conclusion_runtime_warning_and_rethink(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        "aurora.api.autorun_registry.start",
        lambda **kwargs: SimpleNamespace(project_id=kwargs["project_id"], status="running"),
    )
    client = TestClient(create_app())
    with client:
        created = client.post(
            "/api/projects",
            json={"name": "rethink", "goal": "Reason from retained evidence.", "allowed_hosts": ["127.0.0.1"]},
        )
        project_id = created.json()["id"]

        artifact = client.post(
            f"/api/projects/{project_id}/tools/sandbox.exec/execute",
            json={"request": {"command": "printf 'observed header: X-Test'", "cwd": ".", "timeout_seconds": 5}},
        )
        assert artifact.status_code == 200
        artifact_id = artifact.json()["artifact_refs"][0]

        derived = client.post(
            f"/api/projects/{project_id}/facts",
            json={
                "statement": "The target exposes an X-Test response header.",
                "evidence_items": [{"description": "The response contains the X-Test header.", "artifact_refs": [artifact_id]}],
                "confidence": 0.8,
                "category": "web",
            },
        )
        assert derived.status_code == 200
        assert derived.json()["evidence_refs"] == [artifact_id]
        assert derived.json()["evidence_items"] == [{"description": "The response contains the X-Test header.", "artifact_refs": [artifact_id]}]

        merged = client.post(
            f"/api/projects/{project_id}/facts",
            json={
                "statement": "The target exposes an X-Test response header.",
                "evidence_items": [{"description": "A repeated request returns the same header.", "artifact_refs": [artifact_id]}],
                "confidence": 0.9,
                "category": "web",
            },
        )
        assert merged.status_code == 200
        assert len(merged.json()["evidence_items"]) == 2
        assert merged.json()["confidence"] == 0.9

        legacy = client.post(
            f"/api/projects/{project_id}/facts",
            json={"statement": "Legacy evidence remains supported.", "evidence_refs": [artifact_id], "confidence": 0.6, "category": "compatibility"},
        )
        assert legacy.status_code == 200
        assert legacy.json()["evidence_items"] == []

        missing_evidence = client.post(
            f"/api/projects/{project_id}/facts",
            json={"statement": "Unsupported conclusion.", "confidence": 0.6, "category": "web"},
        )
        assert missing_evidence.status_code == 422

        blank_description = client.post(
            f"/api/projects/{project_id}/facts",
            json={"statement": "Blank evidence.", "evidence_items": [{"description": "   ", "artifact_refs": [artifact_id]}]},
        )
        assert blank_description.status_code == 422

        other_project_id = client.post(
            "/api/projects",
            json={"name": "other-evidence", "goal": "Own another artifact.", "allowed_hosts": ["127.0.0.1"]},
        ).json()["id"]
        other_artifact = client.post(
            f"/api/projects/{other_project_id}/tools/sandbox.exec/execute",
            json={"request": {"command": "printf 'other project'", "cwd": ".", "timeout_seconds": 5}},
        ).json()["artifact_refs"][0]
        cross_project = client.post(
            f"/api/projects/{project_id}/facts",
            json={"statement": "Cross-project evidence.", "evidence_items": [{"description": "Wrong source.", "artifact_refs": [other_artifact]}]},
        )
        assert cross_project.status_code == 400

        with Session(engine) as session:
            warning = WorkerEvent(project_id=project_id, event_type="runtime.error", payload_json={"error": "container exited 1"})
            session.add(warning)
            session.commit()
            session.refresh(warning)
            warning_id = warning.id

        warnings = client.get(f"/api/projects/{project_id}/warnings")
        assert warnings.status_code == 200
        assert [item["id"] for item in warnings.json()] == [warning_id]
        assert client.post(f"/api/projects/{project_id}/warnings/{warning_id}/acknowledge").status_code == 200
        assert client.get(f"/api/projects/{project_id}/warnings").json() == []

        with Session(engine) as session:
            session.add(WorkerEvent(project_id=project_id, event_type="runtime.error", payload_json={"error": "stale worker failure"}))
            session.commit()

        reset = client.post(f"/api/projects/{project_id}/rethink")
        assert reset.status_code == 200
        assert reset.json()["status"] == "working"
        assert reset.json()["autorun"]["status"] == "running"
        board = client.get(f"/api/projects/{project_id}/blackboard").json()
        assert board["project"]["status"] == "WORKING"
        assert board["facts"] == []
        assert len(board["artifacts"]) == 1
        assert board["artifacts"][0]["id"] == artifact_id
        assert board["artifacts"][0]["source_attempt_id"] is None
        assert len(board["intents"]) == 1
        assert board["intents"][0]["status"] == "PENDING"
        assert client.get(f"/api/projects/{project_id}/warnings").json() == []
        event_types = {event["event_type"] for event in client.get(f"/api/projects/{project_id}/events").json()}
        assert "fact.derived_from_evidence" in event_types
        assert "project.rethought" in event_types


def test_rethink_returns_immediately_while_autorun_is_stopping(monkeypatch) -> None:
    from types import SimpleNamespace

    client = TestClient(create_app())
    with client:
        project_id = client.post(
            "/api/projects",
            json={"name": "async-rethink", "goal": "restart active solve", "allowed_hosts": ["127.0.0.1"]},
        ).json()["id"]
        monkeypatch.setattr("aurora.api.autorun_registry.status", lambda _project_id: {"status": "running"})
        monkeypatch.setattr(
            "aurora.api.project_rethink_registry.start",
            lambda **kwargs: SimpleNamespace(project_id=kwargs["project_id"], status="stopping"),
        )

        response = client.post(f"/api/projects/{project_id}/rethink")

        assert response.status_code == 200
        assert response.json()["status"] == "working"
        assert response.json()["phase"] == "stopping"


def test_rethink_stop_catches_container_created_after_stop_request(monkeypatch) -> None:
    from aurora.services.project_rethink_registry import ProjectRethinkRegistry, ProjectRethinkState

    statuses = iter([{"status": "stopping"}, {"status": "stopped"}])
    container_scans = iter([
        {"stopped": [], "errors": []},
        {"stopped": ["late-container"], "errors": []},
    ])
    monkeypatch.setattr("aurora.services.project_rethink_registry.autorun_registry.stop", lambda _project_id: None)
    monkeypatch.setattr("aurora.services.project_rethink_registry.autorun_registry.status", lambda _project_id: next(statuses))
    monkeypatch.setattr("aurora.services.project_rethink_registry.stop_project_containers", lambda _project_id: next(container_scans))
    monkeypatch.setattr("aurora.services.project_rethink_registry.time.sleep", lambda _seconds: None)

    stopped = ProjectRethinkRegistry()._stop_active_autorun("proj_race", ProjectRethinkState(project_id="proj_race"))

    assert stopped == {"stopped": ["late-container"], "errors": []}


def test_discovered_target_is_filtered_and_temporarily_authorized() -> None:
    assert BrowserInteractionService._labeled_target_urls(
        "题目地址：8.8.8.8:18080\nTarget URL: https://ctf.example/challenge/1\n靶机地址: http://1.1.1.1:8080/",
        "ctf.example",
    ) == ["http://8.8.8.8:18080", "http://1.1.1.1:8080/"]
    assert BrowserInteractionService._target_urls(
        [
            "https://ctf.example/challenge/1",
            "http://8.8.8.8:18080/",
            "http://169.254.169.254/latest/meta-data",
            "http://localhost:8080/",
            "https://hm.baidu.com/hm.js?tracking=1",
            "https://blog.csdn.net/example/writeup",
            "http://网址/send就行",
            "https://assets.example.org/app.js",
        ],
        "ctf.example",
    ) == ["http://8.8.8.8:18080/"]

    client = TestClient(create_app())
    with client:
        project = client.post(
            "/api/projects",
            json={"name": "dynamic-target", "goal": "Use a provisioned target.", "allowed_hosts": ["ctf.example"]},
        ).json()
        project_id = project["id"]
        with Session(engine) as session:
            denied = PolicyEngine().check_tool_request(session, project_id=project_id, tool_name="http.request", request={"url": "http://8.8.8.8:18080/"})
            assert denied.allowed is True  # authorization gate removed
            session.add(DiscoveredTarget(project_id=project_id, url="http://8.8.8.8:18080/", host="8.8.8.8"))
            session.commit()
            allowed = PolicyEngine().check_tool_request(session, project_id=project_id, tool_name="http.request", request={"url": "http://8.8.8.8:18080/"})
            assert allowed.allowed is True

        targets = client.get(f"/api/projects/{project_id}/targets")
        assert targets.status_code == 200
        assert targets.json()[0]["host"] == "8.8.8.8"
        session_update = client.post(
            f"/api/projects/{project_id}/browser/session",
            json={"source_url": "https://ctf.example/challenge/1", "cookie": "session=test"},
        )
        assert session_update.status_code == 200
        assert "test" not in session_update.text
        with Session(engine) as session:
            browser_allowed = PolicyEngine().check_tool_request(
                session,
                project_id=project_id,
                tool_name="browser.interact",
                request={"url": "https://ctf.example/challenge/1"},
            )
            assert browser_allowed.allowed is True


def test_false_target_repair_is_idempotent() -> None:
    from aurora.services.target_repair import invalidate_false_targets

    client = TestClient(create_app())
    with client:
        project_id = client.post(
            "/api/projects",
            json={"name": "target-repair", "goal": "Repair false targets.", "allowed_hosts": []},
        ).json()["id"]
        with Session(engine) as session:
            project = session.get(Project, project_id)
            assert project is not None
            project.target_url = "https://hm.baidu.com/hm.js?tracking=1"
            session.add(project)
            session.add(DiscoveredTarget(project_id=project_id, url=project.target_url, host="hm.baidu.com"))
            session.add(Fact(project_id=project_id, statement=f"Browser interaction exposed target: {project.target_url}", category="target", confidence=0.9))
            session.commit()

            first = invalidate_false_targets(session)
            second = invalidate_false_targets(session)
            target = session.exec(select(DiscoveredTarget).where(DiscoveredTarget.project_id == project_id)).first()
            fact = session.exec(select(Fact).where(Fact.project_id == project_id, Fact.category == "target")).first()
            session.refresh(project)

        assert first == {"targets": 1, "facts": 1, "projects": 1}
        assert second == {"targets": 0, "facts": 0, "projects": 0}
        assert target is not None and target.status == "INVALIDATED"
        assert fact is not None and fact.status == "RETRACTED"
        assert project.target_url is None
