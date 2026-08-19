from sqlmodel import Session, SQLModel, create_engine

from aurora.config import get_settings
from aurora.models import ChallengeGroup, ChallengeGroupItem, Project
from aurora.services.context_builder import ContextBuilder
from aurora.services.project_rethink import rethink_project


def test_rethink_reopens_failed_group_item_and_preserves_competition_context(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AURORA_CODEX_WORKSPACE_DIR", str(tmp_path / "workspaces"))
    get_settings.cache_clear()
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="解压缩", goal="extract", status="WORKING")
        group = ChallengeGroup(name="Slab Match", status="COMPLETED")
        session.add_all([project, group])
        session.commit()
        item = ChallengeGroupItem(
            group_id=group.id,
            project_id=project.id,
            position=1,
            status="FAILED",
            fused_status="FAILED",
            phase=3,
            phase_attempts={"1": 1, "2": 1, "3": 1},
            failure_history=[{"phase": 1, "outcome": "FAILED"}],
            submission_status="NOT_SUBMITTED",
            stop_reason="no_runnable_work",
            competition_meta={"platform": "slab_match", "exercise_id": 10663},
        )
        session.add(item)
        session.commit()

        intent = rethink_project(session, project_id=project.id)
        session.refresh(item)
        session.refresh(group)
        snapshot = ContextBuilder().build(session, project_id=project.id, intent_id=intent.id)

        assert item.status == "PENDING"
        assert item.fused_status == "PENDING"
        assert item.phase == 1
        assert item.phase_attempts == {}
        assert item.failure_history == []
        assert item.stop_reason is None
        assert group.status == "READY"
        assert snapshot.sections_json["competition_context"]["platform"] == "slab_match"
