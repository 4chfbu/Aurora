from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlmodel import Session, select

from aurora.config import Settings, get_settings
from aurora.models import Artifact, ChallengeGroup, ChallengeGroupItem, Fact, Project, WorkerEvent, new_id
from aurora.services.slab_match import SlabMatchChallenge, SlabMatchClient
from aurora.services.slab_match_import import SlabMatchDirectImporter


class SlabMatchNoticePoller:
    """Persist unseen platform notices into every live Slab Match project.

    Announcements are competition input: they can amend a statement, publish a
    hint, or carry a replacement attachment. Persisting them as challenge
    artifacts also makes them visible to a Solver that is already running via
    the live blackboard.
    """

    def __init__(self, settings: Settings | None = None, client: SlabMatchClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = client or SlabMatchClient(self.settings)

    @staticmethod
    def _is_live_slab_group(group: ChallengeGroup) -> bool:
        return (
            str((group.limits or {}).get("platform") or "").lower() == "slab_match"
            and group.status not in {"COMPLETED", "STOPPED", "FAILED", "CANCELLED"}
        )

    def poll_once(self, session: Session) -> dict[str, Any]:
        groups = [group for group in session.exec(select(ChallengeGroup)).all() if self._is_live_slab_group(group)]
        result = {"groups_checked": len(groups), "notices_added": 0, "projects_updated": 0, "failures": []}
        for group in groups:
            try:
                update = self._poll_group(session, group)
            except Exception as exc:
                result["failures"].append({"group_id": group.id, "reason": str(exc)[:500]})
                continue
            result["notices_added"] += update["notices_added"]
            result["projects_updated"] += update["projects_updated"]
        return result

    def _poll_group(self, session: Session, group: ChallengeGroup) -> dict[str, int]:
        limits = dict(group.limits or {})
        base_url = str(limits.get("base_url") or self.client.base_url)
        with_base_url = getattr(self.client, "with_base_url", None)
        client = with_base_url(base_url) if callable(with_base_url) else self.client
        known_ids = {str(value) for value in limits.get("notice_ids", [])}
        summaries = client.notice_list()
        unseen = [item for item in summaries if str(item.get("id")) not in known_ids]
        if not unseen:
            return {"notices_added": 0, "projects_updated": 0}

        items = session.exec(
            select(ChallengeGroupItem)
            .where(ChallengeGroupItem.group_id == group.id)
            .order_by(ChallengeGroupItem.position)
        ).all()
        projects_updated = 0
        for summary in sorted(unseen, key=lambda item: (int(item.get("createdTime") or 0), str(item.get("id")))):
            notice_id = int(summary["id"])
            try:
                detail = client.notice_detail(notice_id)
            except Exception:
                detail = dict(summary)
            notice = {**summary, **detail}
            notice["id"] = notice_id
            attachments = SlabMatchClient._as_files([notice.get("file"), notice.get("files"), notice.get("url")])
            for item in items:
                project = session.get(Project, item.project_id)
                if project is None:
                    continue
                evidence_refs = [self._write_notice_artifact(session, project, notice)]
                downloaded: list[dict[str, Any]] = []
                if attachments:
                    pending = SlabMatchChallenge(
                        exercise_id=int((item.competition_meta or {}).get("exercise_id") or 1),
                        title=str(notice.get("title") or f"Notice {notice_id}"),
                        description=str(notice.get("content") or ""),
                        challenge_type=project.challenge_type or "unknown",
                        difficulty=None,
                        points=None,
                        attachments=attachments,
                        endpoints=[],
                        has_solved=False,
                        is_need_init=False,
                        is_need_check=False,
                        raw=notice,
                    )
                    importer = SlabMatchDirectImporter(self.settings, client)
                    attachment_refs, downloaded = importer._download_attachments(
                        session,
                        project,
                        pending,
                        base_url=base_url,
                        downloader=client.download_attachment,
                    )
                    evidence_refs.extend(attachment_refs)
                title = str(notice.get("title") or f"Notice {notice_id}").strip()
                content = str(notice.get("content") or "").strip()
                session.add(Fact(
                    project_id=project.id,
                    statement=f"Slab Match announcement #{notice_id} — {title}: {content}"[:20_000],
                    category="competition_notice",
                    confidence=1.0,
                    evidence_refs=evidence_refs,
                ))
                metadata = dict(item.competition_meta or {})
                notices = [entry for entry in metadata.get("notices", []) if isinstance(entry, dict)]
                notices.append({
                    "id": notice_id,
                    "title": title[:500],
                    "content": content[:20_000],
                    "created_time": notice.get("createdTime"),
                    "artifact_refs": evidence_refs,
                    "attachments": downloaded,
                })
                metadata["notices"] = notices[-50:]
                item.competition_meta = metadata
                session.add(item)
                session.add(WorkerEvent(
                    project_id=project.id,
                    event_type="slab_match.notice_received",
                    payload_json={"group_id": group.id, "notice_id": notice_id, "title": title[:500], "artifact_refs": evidence_refs},
                ))
                projects_updated += 1
            known_ids.add(str(notice_id))

        limits["notice_ids"] = sorted(known_ids, key=lambda value: (0, int(value)) if value.isdigit() else (1, value))[-500:]
        group.limits = limits
        session.add(group)
        session.commit()
        return {"notices_added": len(unseen), "projects_updated": projects_updated}

    def _write_notice_artifact(self, session: Session, project: Project, notice: dict[str, Any]) -> str:
        artifact_id = new_id("artifact")
        payload = json.dumps(notice, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        project_dir = self.settings.artifact_dir / project.id
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / f"{artifact_id}.notice.json"
        path.write_bytes(payload)
        artifact = Artifact(
            id=artifact_id,
            project_id=project.id,
            type="slab_match_notice",
            path=str(path),
            sha256=hashlib.sha256(payload).hexdigest(),
            mime_type="application/json",
            size=len(payload),
            summary=f"Slab Match announcement #{notice.get('id')}: {str(notice.get('title') or '')[:300]}",
            origin_kind="challenge_input",
        )
        session.add(artifact)
        session.commit()
        return artifact.id
