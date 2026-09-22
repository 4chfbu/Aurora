from __future__ import annotations

import hashlib
from pathlib import Path

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.models import Artifact, new_id
from aurora.services.evidence_context import artifact_environment_context


class ArtifactStore:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or get_settings().artifact_dir

    def write_text(
        self,
        session: Session,
        *,
        project_id: str,
        content: str,
        summary: str,
        source_attempt_id: str | None = None,
        artifact_type: str = "text",
        sensitivity: str = "normal",
        origin_kind: str = "unclassified",
        evidence_context: dict | None = None,
    ) -> Artifact:
        artifact_id = new_id("artifact")
        project_dir = self.base_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / f"{artifact_id}.txt"
        data = content.encode("utf-8", errors="replace")
        path.write_bytes(data)
        artifact = Artifact(
            id=artifact_id,
            project_id=project_id,
            source_attempt_id=source_attempt_id,
            type=artifact_type,
            path=str(path),
            sha256=hashlib.sha256(data).hexdigest(),
            mime_type="text/plain",
            size=len(data),
            summary=summary,
            sensitivity=sensitivity,
            origin_kind=origin_kind,
            evidence_context={
                **artifact_environment_context(session, project_id=project_id, source_attempt_id=source_attempt_id, origin_kind=origin_kind),
                **(evidence_context or {}),
            },
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        return artifact

    def write_file(
        self,
        session: Session,
        *,
        project_id: str,
        source: Path,
        summary: str,
        source_attempt_id: str | None = None,
        artifact_type: str = "file",
        sensitivity: str = "normal",
        origin_kind: str = "unclassified",
        deduplicate: bool = False,
    ) -> Artifact:
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        context = {
            **artifact_environment_context(session, project_id=project_id, source_attempt_id=source_attempt_id, origin_kind=origin_kind),
            "original_name": source.name,
        }
        if deduplicate:
            candidates = session.exec(
                select(Artifact)
                .where(
                    Artifact.project_id == project_id,
                    Artifact.sha256 == digest,
                    Artifact.type == artifact_type,
                    Artifact.origin_kind == origin_kind,
                    Artifact.sensitivity == sensitivity,
                )
                .order_by(Artifact.created_at)
            ).all()
            for existing in candidates:
                if existing.evidence_context != context:
                    continue
                try:
                    with Path(existing.path).open("rb") as handle:
                        if hashlib.file_digest(handle, "sha256").hexdigest() == digest:
                            return existing
                except OSError:
                    continue
        artifact_id = new_id("artifact")
        project_dir = self.base_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix[:20] if source.suffix else ".bin"
        path = project_dir / f"{artifact_id}{suffix}"
        path.write_bytes(data)
        artifact = Artifact(
            id=artifact_id,
            project_id=project_id,
            source_attempt_id=source_attempt_id,
            type=artifact_type,
            path=str(path),
            sha256=digest,
            mime_type="application/octet-stream",
            size=len(data),
            summary=summary,
            sensitivity=sensitivity,
            origin_kind=origin_kind,
            evidence_context=context,
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        return artifact

    def read_text(self, artifact: Artifact, max_bytes: int = 64_000) -> str:
        with Path(artifact.path).open("rb") as handle:
            data = handle.read(max(0, max_bytes))
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def original_name(artifact: Artifact) -> str:
        return Path(str((artifact.evidence_context or {}).get("original_name") or artifact.path)).name
