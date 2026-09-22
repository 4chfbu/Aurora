from datetime import timedelta

from sqlmodel import Session, SQLModel, create_engine

from aurora.models import Artifact, Attempt, AttemptCheckpoint, Intent, Project, now_utc
from aurora.services.artifact_store import ArtifactStore
from aurora.services.context_builder import ContextBuilder
from aurora.services.demo import _select_parent_attempt


def test_native_continuation_keeps_local_capabilities_and_authoritative_parent(monkeypatch) -> None:
    from aurora.config import get_settings
    from aurora.models import ProjectRuntimePolicy
    from aurora.services.round_summary import RoundReflectionService

    monkeypatch.setenv("AURORA_TOOL_CONTRACT", "kali_shell")
    get_settings.cache_clear()
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="native continuation", goal="finish the saved experiment")
        policy = ProjectRuntimePolicy(project_id=project.id, multi_agent_exploration_enabled=True)
        original = Intent(project_id=project.id, objective="original experiment", status="COMPLETED")
        attempt = Attempt(project_id=project.id, intent_id=original.id, worker_id="worker_parent", status="PARTIAL", codex_thread_id="thread_parent", resume_manifest_artifact_id="manifest_parent")
        session.add_all([project, policy, original, attempt])
        session.commit()
        checkpoint = RoundReflectionService().create(session, attempt=attempt, skip_planner=True, budget={"phase": 1}, output={
            "summary": "continue local analysis",
            "suggested_intents": [{"objective": "Verify the saved experiment", "capabilities": ["sandbox.exec", "blackboard.query"], "budget": {"continuation_attempt_id": "foreign_attempt"}}],
        })
        assert len(checkpoint.generated_intent_ids) == 1
        continuation = session.get(Intent, checkpoint.generated_intent_ids[0])
        assert set(continuation.capability_tags) == {"blackboard.query", "sandbox.exec"}
        assert continuation.budget["continuation_attempt_id"] == attempt.id
        assert _select_parent_attempt(session, project_id=project.id, intent=continuation).id == attempt.id


def test_checkpoint_preserves_legacy_failure_shapes() -> None:
    from aurora.services.round_summary import RoundReflectionService

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="failure memory", goal="avoid repeating failed experiments")
        intent = Intent(project_id=project.id, objective="investigate")
        attempt = Attempt(project_id=project.id, intent_id=intent.id, worker_id="worker_memory", environment_id="instance_one", status="PARTIAL")
        session.add_all([project, intent, attempt])
        session.commit()
        checkpoint = RoundReflectionService().create(session, attempt=attempt, skip_planner=True, author_intents=False, budget={}, output={
            "summary": "preserve failures",
            "failed_attempts": [
                {"reason": "command_timed_out", "stderr": "target connection timeout"},
                {"approach": "decode firmware", "result": "non-ASCII output"},
                {"reason": "flag_verification_failed", "summary": "stale evidence", "artifact_refs": ["artifact_failure"]},
                {}, "", "already tried route",
            ],
        })
        assert len(checkpoint.failed_routes) == 4
        assert "command_timed_out" in checkpoint.failed_routes[0]
        assert "target connection timeout" in checkpoint.failed_routes[0]
        assert "decode firmware" in checkpoint.failed_routes[1]
        assert "non-ASCII output" in checkpoint.failed_routes[1]
        assert "stale evidence" in checkpoint.failed_routes[2]
        assert "artifact_failure" in checkpoint.failed_routes[2]


def test_parent_attempt_falls_back_to_latest_resumable_project_state() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(id="project_inherit", name="inherit", goal="continue")
        other_project = Project(id="project_foreign", name="foreign", goal="ignore")
        explicit_parent = Intent(id="intent_parent", project_id=project.id, objective="parent")
        current = Intent(
            id="intent_current",
            project_id=project.id,
            objective="next phase",
            parent_intent_id=explicit_parent.id,
        )
        fallback_intent = Intent(id="intent_fallback", project_id=project.id, objective="fallback")
        session.add_all([project, other_project, explicit_parent, current, fallback_intent])
        base = now_utc()
        fallback = Attempt(
            id="attempt_fallback",
            project_id=project.id,
            intent_id=fallback_intent.id,
            worker_id="worker_fallback",
            status="PARTIAL",
            codex_thread_id="thread_fallback",
            resume_manifest_artifact_id="manifest_fallback",
            started_at=base,
            finished_at=base,
        )
        unusable_parent = Attempt(
            id="attempt_unusable_parent",
            project_id=project.id,
            intent_id=explicit_parent.id,
            worker_id="worker_parent",
            status="FAILED",
            started_at=base + timedelta(seconds=1),
            finished_at=base + timedelta(seconds=1),
        )
        foreign = Attempt(
            id="attempt_foreign",
            project_id=other_project.id,
            intent_id="intent_foreign",
            worker_id="worker_foreign",
            status="SUCCESS",
            codex_thread_id="thread_foreign",
            resume_manifest_artifact_id="manifest_foreign",
            started_at=base + timedelta(seconds=2),
            finished_at=base + timedelta(seconds=2),
        )
        session.add_all([fallback, unusable_parent, foreign])
        session.commit()

        assert _select_parent_attempt(session, project_id=project.id, intent=current).id == fallback.id

        resumable_parent = Attempt(
            id="attempt_resumable_parent",
            project_id=project.id,
            intent_id=explicit_parent.id,
            worker_id="worker_parent",
            status="PARTIAL",
            codex_thread_id="thread_parent",
            resume_manifest_artifact_id="manifest_parent",
            started_at=base - timedelta(seconds=1),
            finished_at=base - timedelta(seconds=1),
        )
        session.add(resumable_parent)
        session.commit()

        assert _select_parent_attempt(session, project_id=project.id, intent=current).id == resumable_parent.id


def test_checkpoint_artifacts_are_pinned_outside_latest_window(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    store = ArtifactStore(tmp_path / "artifacts")
    with Session(engine) as session:
        project = Project(id="project_context", name="context", goal="continue")
        foreign_project = Project(id="project_context_foreign", name="foreign", goal="ignore")
        intent = Intent(id="intent_context", project_id=project.id, objective="next phase")
        session.add_all([project, foreign_project, intent])
        session.commit()
        pinned = store.write_text(
            session,
            project_id=project.id,
            content="critical handoff evidence",
            summary="critical older artifact",
            artifact_type="analysis",
        )
        foreign = store.write_text(
            session,
            project_id=foreign_project.id,
            content="foreign evidence",
            summary="must stay hidden",
            artifact_type="analysis",
        )
        for index in range(11):
            store.write_text(
                session,
                project_id=project.id,
                content=f"new evidence {index}",
                summary=f"new artifact {index}",
                artifact_type="analysis",
            )
        attempt = Attempt(
            id="attempt_context",
            project_id=project.id,
            intent_id=intent.id,
            worker_id="worker_context",
            status="PARTIAL",
        )
        session.add(attempt)
        session.commit()
        session.add(
            AttemptCheckpoint(
                project_id=project.id,
                intent_id=intent.id,
                attempt_id=attempt.id,
                summary="handoff",
                artifact_refs=[pinned.id, foreign.id],
            )
        )
        session.commit()

        snapshot = ContextBuilder().build(session, project_id=project.id, intent_id=intent.id)

        latest_ids = {artifact["id"] for artifact in snapshot.sections_json["artifact_summaries"]}
        handoff_ids = {artifact["id"] for artifact in snapshot.sections_json["handoff_artifacts"]}
        assert pinned.id not in latest_ids
        assert handoff_ids == {pinned.id}


def test_continuation_pins_original_checkpoint_and_dependency_facts(tmp_path) -> None:
    from aurora.models import Fact

    database = create_engine("sqlite://")
    SQLModel.metadata.create_all(database)
    with Session(database) as session:
        project = Project(name="pinned branch", goal="finish file read")
        branch = Intent(project_id=project.id, objective="establish read", status="COMPLETED")
        attempt = Attempt(project_id=project.id, intent_id=branch.id, worker_id="worker_source", status="PARTIAL")
        session.add_all([project, branch, attempt])
        session.commit()
        artifact = ArtifactStore(tmp_path).write_text(session, project_id=project.id, content="file read evidence", summary="source observation", origin_kind="target_observation")
        fact = Fact(project_id=project.id, statement="working file read", source_attempt_id=attempt.id, evidence_refs=[artifact.id])
        checkpoint = AttemptCheckpoint(project_id=project.id, intent_id=branch.id, attempt_id=attempt.id, summary="read confirmed", artifact_refs=[artifact.id])
        continuation = Intent(project_id=project.id, objective="read protected file", parent_intent_id=branch.id, dependency_fact_ids=[fact.id], budget={"continuation_attempt_id": attempt.id})
        session.add_all([fact, checkpoint, continuation])
        session.commit()
        for index in range(30):
            session.add(Fact(project_id=project.id, statement=f"unrelated observation {index}"))
        for index in range(4):
            session.add(AttemptCheckpoint(project_id=project.id, intent_id="peer", attempt_id=f"peer_{index}", summary="other branch"))
        session.commit()
        snapshot = ContextBuilder().build(session, project_id=project.id, intent_id=continuation.id)
        assert snapshot.sections_json["facts"][0]["id"] == fact.id
        assert snapshot.sections_json["recent_checkpoints"][0]["id"] == checkpoint.id
        assert artifact.id in {item["id"] for item in snapshot.sections_json["handoff_artifacts"]}
