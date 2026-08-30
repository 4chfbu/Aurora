from datetime import timedelta

from sqlmodel import Session, SQLModel, create_engine

from aurora.models import Artifact, Attempt, AttemptCheckpoint, Intent, Project, now_utc
from aurora.services.artifact_store import ArtifactStore
from aurora.services.context_builder import ContextBuilder
from aurora.services.demo import _select_parent_attempt


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
