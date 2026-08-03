from __future__ import annotations

import hashlib
from pathlib import Path

from sqlmodel import Session

from aurora.config import get_settings
from aurora.models import Artifact, new_id


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
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        return artifact

    def read_text(self, artifact: Artifact, max_bytes: int = 64_000) -> str:
        data = Path(artifact.path).read_bytes()[:max_bytes]
        return data.decode("utf-8", errors="replace")
