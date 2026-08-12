from __future__ import annotations

import shutil
from dataclasses import dataclass

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.models import (
    Artifact,
    Attempt,
    AuthorizationScope,
    ChallengeGroup,
    ChallengeGroupEvent,
    ChallengeGroupItem,
    ContextSnapshot,
    DiscoveredTarget,
    Fact,
    Finding,
    FlagCandidate,
    Hint,
    ImportCandidate,
    Intent,
    LLMTrace,
    Project,
    ProjectRuntimePolicy,
    ToolTrace,
    Worker,
    WorkerEvent,
)
from aurora.services.autorun_registry import autorun_registry
from aurora.services.browser_sessions import browser_session_registry
from aurora.services.challenge_group_runner import challenge_group_registry
from aurora.services.container_control import remove_project_containers, stop_project_containers


@dataclass
class DeleteProjectResult:
    project_id: str
    removed_group_items: int
    stopped_groups: list[str]
    containers: dict[str, object]


class ProjectDeletionService:
    """Permanently remove projects after their active execution has quiesced."""

    def delete_project(self, session: Session, *, project_id: str) -> DeleteProjectResult:
        if session.get(Project, project_id) is None:
            raise ValueError("project not found")

        group_items = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.project_id == project_id)).all()
        group_ids = sorted({item.group_id for item in group_items})
        for group_id in group_ids:
            challenge_group_registry.stop(group_id)
        autorun_registry.stop(project_id)
        stopped = stop_project_containers(project_id)
        removed = remove_project_containers(project_id)
        containers = {"stopped": stopped.get("stopped", []), "removed": removed.get("removed", []), "errors": [*stopped.get("errors", []), *removed.get("errors", [])]}

        if not autorun_registry.wait_for_stop(project_id, timeout_seconds=3):
            raise RuntimeError("active autorun did not stop; retry after it has stopped")
        for group_id in group_ids:
            if not challenge_group_registry.wait_for_stop(group_id, timeout_seconds=3):
                raise RuntimeError("active challenge group did not stop; retry after it has stopped")

        running_workers = session.exec(select(Worker).where(Worker.project_id == project_id, Worker.status == "RUNNING")).all()
        if running_workers:
            raise RuntimeError("active workers did not stop; retry after they have stopped")

        removed_group_items = self._remove_project_records(session, project_id=project_id)
        return DeleteProjectResult(
            project_id=project_id,
            removed_group_items=removed_group_items,
            stopped_groups=group_ids,
            containers=containers,
        )

    def delete_group(self, session: Session, *, group_id: str) -> dict[str, object]:
        group = session.get(ChallengeGroup, group_id)
        if group is None:
            raise ValueError("challenge group not found")

        challenge_group_registry.stop(group_id)
        if not challenge_group_registry.wait_for_stop(group_id, timeout_seconds=10):
            raise RuntimeError("active challenge group did not stop; retry after it has stopped")

        items = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group_id)).all()
        project_ids = sorted({item.project_id for item in items})
        deleted_projects = [
            self.delete_project(session, project_id=project_id).project_id
            for project_id in project_ids
            if session.get(Project, project_id) is not None
        ]

        for event in session.exec(select(ChallengeGroupEvent).where(ChallengeGroupEvent.group_id == group_id)).all():
            session.delete(event)
        for item in session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group_id)).all():
            session.delete(item)
        session.delete(group)
        session.commit()
        return {"group_id": group_id, "deleted_project_ids": deleted_projects}

    def _remove_project_records(self, session: Session, *, project_id: str) -> int:
        group_items = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.project_id == project_id)).all()
        removed_group_items = len(group_items)
        for item in group_items:
            group = session.get(ChallengeGroup, item.group_id)
            if group and group.current_item_id == item.id:
                group.current_item_id = None
                session.add(group)
            session.delete(item)

        for candidate in session.exec(select(ImportCandidate).where(ImportCandidate.project_id == project_id)).all():
            candidate.project_id = None
            candidate.confirmed = False
            session.add(candidate)

        artifacts = session.exec(select(Artifact).where(Artifact.project_id == project_id)).all()
        for artifact in artifacts:
            session.delete(artifact)
        for model in (
            ProjectRuntimePolicy,
            AuthorizationScope,
            DiscoveredTarget,
            Fact,
            Finding,
            FlagCandidate,
            ToolTrace,
            LLMTrace,
            ContextSnapshot,
            Attempt,
            Worker,
            Intent,
            WorkerEvent,
            Hint,
        ):
            for item in session.exec(select(model).where(model.project_id == project_id)).all():
                session.delete(item)

        project = session.get(Project, project_id)
        if project is not None:
            session.delete(project)
        session.commit()

        settings = get_settings()
        shutil.rmtree(settings.artifact_dir / project_id, ignore_errors=True)
        shutil.rmtree(settings.codex_workspace_dir / project_id, ignore_errors=True)
        browser_session_registry.clear_project_session(project_id)
        return removed_group_items
