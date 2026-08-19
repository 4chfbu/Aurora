from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from sqlmodel import Session, select

from aurora.config import Settings, get_settings
from aurora.models import Artifact, ChallengeGroup, ChallengeGroupItem, Fact, Project, WorkerEvent, new_id
from aurora.services.demo import create_project_with_bootstrap
from aurora.services.hands_free import HandsFreeService
from aurora.services.slab_match import SlabMatchChallenge, SlabMatchClient


MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
AttachmentDownloader = Callable[[str, int], tuple[bytes, str | None]]


class SlabMatchDirectImporter:
    """Import Agent API challenge data without starting planners or targets."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: SlabMatchClient | None = None,
        downloader: AttachmentDownloader | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or SlabMatchClient(self.settings)
        self.downloader = downloader or getattr(self.client, "download_attachment", None)
        if not callable(self.downloader):
            raise ValueError("Slab Match client does not provide an attachment downloader")

    @staticmethod
    def _requires_environment(challenge: SlabMatchChallenge) -> bool:
        return bool(challenge.is_need_init or challenge.is_need_check or challenge.endpoints)

    @staticmethod
    def _challenge_type(value: str) -> str:
        lowered = value.lower()
        for pattern, challenge_type in (
            (r"\bweb\b", "web"),
            (r"\bpwn\b|binary exploitation", "pwn"),
            (r"crypto", "crypto"),
            (r"reverse|reversing|\bre\b", "reverse"),
            (r"forensic", "forensics"),
            (r"misc|osint|steg", "misc"),
        ):
            if re.search(pattern, lowered):
                return challenge_type
        return "unknown"

    @staticmethod
    def _goal(challenge: SlabMatchChallenge) -> str:
        parts = [challenge.description.strip()]
        if challenge.match_info:
            if challenge.match_info.get("note"):
                parts.append(f"Competition note: {challenge.match_info['note']}")
            if challenge.match_info.get("rule"):
                parts.append(f"Competition rule: {challenge.match_info['rule']}")
        return "\n\n".join(part for part in parts if part) or f"Solve Slab Match exercise {challenge.exercise_id}: {challenge.title}"

    def _download_attachments(
        self,
        session: Session,
        project: Project,
        challenge: SlabMatchChallenge,
        *,
        base_url: str | None = None,
        downloader: AttachmentDownloader | None = None,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        artifact_ids: list[str] = []
        records: list[dict[str, Any]] = []
        attachment_base_url = base_url or self.client.base_url
        fetch = downloader or self.downloader
        for attachment in challenge.attachments:
            url = str(attachment.get("url") or "").strip()
            if not url:
                continue
            record = dict(attachment)
            record["source_url"] = url
            try:
                parsed_attachment = urlparse(url)
                candidates = [urljoin(attachment_base_url.rstrip("/") + "/", url)]
                if not parsed_attachment.scheme and not url.startswith("/"):
                    parsed_base = urlparse(attachment_base_url)
                    origin_url = f"{parsed_base.scheme}://{parsed_base.netloc}/"
                    candidates.append(urljoin(origin_url, url))
                candidates = list(dict.fromkeys(candidates))
                data = None
                mime_type = None
                safe_url = ""
                last_error: Exception | None = None
                for candidate_url in candidates:
                    try:
                        safe_url = HandsFreeService._safe_url(candidate_url)
                        candidate_data, candidate_mime = fetch(safe_url, MAX_ATTACHMENT_BYTES)
                        if len(candidate_data) > MAX_ATTACHMENT_BYTES:
                            raise ValueError("attachment exceeds size limit")
                        if HandsFreeService._attachment_response_is_html(candidate_data, candidate_mime):
                            raise ValueError("attachment endpoint returned HTML instead of a file")
                        data, mime_type = candidate_data, candidate_mime
                        break
                    except Exception as exc:
                        last_error = exc
                if data is None:
                    raise last_error or ValueError("attachment download failed")
                record["url"] = safe_url
                filename = HandsFreeService._safe_filename(
                    str(attachment.get("name") or Path(urlparse(safe_url).path).name or "attachment.bin")
                )
                declared_ext = str(attachment.get("ext") or "").strip().lstrip(".")
                if declared_ext and not Path(filename).suffix:
                    filename = HandsFreeService._safe_filename(f"{filename}.{declared_ext}")
                artifact_id = new_id("artifact")
                suffix = Path(filename).suffix[:20] or ".bin"
                project_dir = self.settings.artifact_dir / project.id
                project_dir.mkdir(parents=True, exist_ok=True)
                path = project_dir / f"{artifact_id}{suffix}"
                path.write_bytes(data)
                artifact = Artifact(
                    id=artifact_id,
                    project_id=project.id,
                    type="imported_attachment",
                    path=str(path),
                    sha256=hashlib.sha256(data).hexdigest(),
                    mime_type=mime_type or mimetypes.guess_type(filename)[0],
                    size=len(data),
                    summary=f"Slab Match attachment: {filename}",
                    origin_kind="challenge_input",
                )
                session.add(artifact)
                session.commit()
                artifact_ids.append(artifact.id)
                record.update({"status": "staged", "artifact_id": artifact.id, "filename": filename})
            except Exception as exc:
                record.update({"status": "download_failed", "reason": str(exc)[:300]})
            records.append(record)
        return artifact_ids, records

    def import_all(self, session: Session) -> dict[str, Any]:
        max_environments = int(self.settings.slab_match_max_concurrent or 1)
        if not 1 <= max_environments <= 10:
            raise ValueError("max_environments must be between 1 and 10")
        challenges = self.client.list_challenges()
        if not challenges:
            raise ValueError("Slab Match did not return any open challenges")

        group = ChallengeGroup(
            name="Slab Match",
            limits={
                "platform": "slab_match",
                "base_url": self.client.base_url,
                "max_dynamic_environments": max_environments,
                "managed_by": "planner",
            },
        )
        session.add(group)
        session.commit()
        session.refresh(group)

        projects: list[dict[str, Any]] = []
        for position, challenge in enumerate(challenges, start=1):
            requires_environment = self._requires_environment(challenge)
            project = create_project_with_bootstrap(
                session,
                name=challenge.title[:240],
                goal=self._goal(challenge),
                challenge_type=self._challenge_type(challenge.challenge_type),
                allowed_hosts=[],
                hint="Analyze imported attachments first when no dynamic target is required.",
            )
            project.target_verification_status = "UNVERIFIED"
            project.target_verification_reason = (
                "Slab Match will allocate a dynamic target when this item is dispatched"
                if requires_environment
                else "This Slab Match item does not require a dynamic target; analyze its statement and attachments directly"
            )
            session.add(project)
            artifact_ids, attachments = self._download_attachments(session, project, challenge)
            metadata = {
                "platform": "slab_match",
                "base_url": self.client.base_url,
                "exercise_id": challenge.exercise_id,
                "difficulty": challenge.difficulty,
                "points": challenge.points,
                "has_solved": challenge.has_solved,
                "is_need_init": challenge.is_need_init,
                "is_need_check": challenge.is_need_check,
                "requires_environment": requires_environment,
                "attachment_only": bool(attachments) and not requires_environment,
                "endpoints": challenge.endpoints,
                "attachments": attachments,
                "match_info": challenge.match_info or {},
                "container_status": "available" if challenge.endpoints and not challenge.is_need_check else ("building" if challenge.is_need_check else "stopped"),
                "container_addr": [],
                "provenance": "slab_match_direct_import",
            }
            session.add(ChallengeGroupItem(group_id=group.id, project_id=project.id, position=position, competition_meta=metadata))
            session.add(Fact(
                project_id=project.id,
                statement=(
                    "Slab Match attachments were downloaded as trusted challenge inputs; begin with local artifact analysis without starting a target."
                    if artifact_ids and not requires_environment
                    else "Challenge metadata was imported directly from the Slab Match Agent API."
                ),
                category="import",
                confidence=1.0,
                evidence_refs=artifact_ids,
            ))
            session.add(WorkerEvent(
                project_id=project.id,
                event_type="slab_match.direct_imported",
                payload_json={
                    "group_id": group.id,
                    "exercise_id": challenge.exercise_id,
                    "requires_environment": requires_environment,
                    "artifact_refs": artifact_ids,
                },
            ))
            session.commit()
            projects.append({
                "project_id": project.id,
                "exercise_id": challenge.exercise_id,
                "name": project.name,
                "requires_environment": requires_environment,
                "artifact_count": len(artifact_ids),
                "attachment_errors": sum(1 for item in attachments if item.get("status") == "download_failed"),
            })

        return {
            "group": group,
            "projects": projects,
            "challenge_count": len(projects),
            "attachment_first_count": sum(1 for project in projects if not project["requires_environment"] and project["artifact_count"]),
            "max_environments": max_environments,
        }

    def repair_group_attachments(self, session: Session, group_id: str) -> dict[str, Any]:
        group = session.get(ChallengeGroup, group_id)
        if group is None:
            raise ValueError("challenge group not found")
        if str((group.limits or {}).get("platform") or "").lower() != "slab_match":
            raise ValueError("challenge group is not a Slab Match import")
        base_url = str((group.limits or {}).get("base_url") or self.client.base_url)
        with_base_url = getattr(self.client, "with_base_url", None)
        client = with_base_url(base_url) if callable(with_base_url) else self.client
        downloader = getattr(client, "download_attachment", None) or self.downloader

        def normalized_url(value: Any) -> str:
            return urljoin(base_url.rstrip("/") + "/", str(value or "").strip())

        items = session.exec(
            select(ChallengeGroupItem)
            .where(ChallengeGroupItem.group_id == group_id)
            .order_by(ChallengeGroupItem.position)
        ).all()
        checked = 0
        repaired = 0
        artifact_count = 0
        failures: list[dict[str, Any]] = []
        for item in items:
            metadata = dict(item.competition_meta or {})
            if str(metadata.get("platform") or "").lower() != "slab_match":
                continue
            exercise_id = metadata.get("exercise_id")
            if exercise_id is None:
                continue
            checked += 1
            challenge = client.get_exercise(exercise_id)
            existing = [entry for entry in metadata.get("attachments", []) if isinstance(entry, dict)]
            staged_urls = {
                normalized_url(entry.get("source_url") or entry.get("url"))
                for entry in existing
                if entry.get("artifact_id") and str(entry.get("source_url") or entry.get("url") or "")
            }
            missing = [entry for entry in challenge.attachments if normalized_url(entry.get("url")) not in staged_urls]
            if not missing:
                continue
            project = session.get(Project, item.project_id)
            if project is None:
                failures.append({"exercise_id": exercise_id, "reason": "project not found"})
                continue
            pending = SlabMatchChallenge(
                exercise_id=challenge.exercise_id,
                title=challenge.title,
                description=challenge.description,
                challenge_type=challenge.challenge_type,
                difficulty=challenge.difficulty,
                points=challenge.points,
                attachments=missing,
                endpoints=challenge.endpoints,
                has_solved=challenge.has_solved,
                is_need_init=challenge.is_need_init,
                is_need_check=challenge.is_need_check,
                raw=challenge.raw,
                match_info=challenge.match_info,
            )
            artifact_ids, downloaded = self._download_attachments(
                session,
                project,
                pending,
                base_url=base_url,
                downloader=downloader,
            )
            merged = [entry for entry in existing if entry.get("artifact_id")]
            merged.extend(downloaded)
            metadata["attachments"] = merged
            metadata["attachment_only"] = bool(merged) and not bool(metadata.get("requires_environment"))
            item.competition_meta = metadata
            session.add(item)
            if artifact_ids:
                repaired += 1
                artifact_count += len(artifact_ids)
                session.add(Fact(
                    project_id=project.id,
                    statement="Missing Slab Match attachments were refreshed from the Agent API.",
                    category="import",
                    confidence=1.0,
                    evidence_refs=artifact_ids,
                ))
                session.add(WorkerEvent(
                    project_id=project.id,
                    event_type="slab_match.attachments_repaired",
                    payload_json={"group_id": group_id, "exercise_id": exercise_id, "artifact_refs": artifact_ids},
                ))
            for entry in downloaded:
                if entry.get("status") == "download_failed":
                    failures.append({"exercise_id": exercise_id, "url": entry.get("url"), "reason": entry.get("reason")})
            session.commit()

        return {
            "group_id": group_id,
            "checked": checked,
            "repaired_projects": repaired,
            "artifact_count": artifact_count,
            "failures": failures,
        }
