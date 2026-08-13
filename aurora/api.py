from __future__ import annotations

from contextlib import asynccontextmanager
import json
from threading import Event, Lock, Thread
import time
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from sqlmodel import Session, select

from pathlib import Path

from aurora.config import get_settings
from aurora.db import engine, get_session, init_db
from aurora.models import (
    Artifact,
    Attempt,
    AttemptCheckpoint,
    ContextSnapshot,
    ChallengeGroup,
    ChallengeGroupEvent,
    ChallengeGroupItem,
    DiscoveredTarget,
    Fact,
    Finding,
    FlagCandidate,
    Hint,
    ImportBatch,
    ImportCandidate,
    Intent,
    LLMTrace,
    Project,
    ToolTrace,
    Worker,
    WorkerEvent,
    ProjectRuntimePolicy,
    now_utc,
)
from aurora.services.demo import create_project_with_bootstrap, run_one_demo_step
from aurora.services.artifact_store import ArtifactStore
from aurora.services.capability_gateway import CapabilityGateway
from aurora.services.scheduler import Scheduler
from aurora.services.blackboard_repository import BlackboardRepository
from aurora.services.observer import ObserverService
from aurora.services.manager import ManagerService
from aurora.services.autorunner import AutoRunLimits, AutoRunnerService
from aurora.services.autorun_registry import autorun_registry
from aurora.services.api_instance_lock import acquire_api_instance_lock
from aurora.services.container_control import get_project_container_logs, stop_project_containers
from aurora.services.hands_free import HandsFreeService
from aurora.services.project_rethink import rethink_project
from aurora.services.project_rethink_registry import project_rethink_registry
from aurora.services.runtime_warnings import acknowledge_runtime_warning, list_active_runtime_warnings
from aurora.services.browser_sessions import browser_session_registry
from aurora.services.challenge_group_runner import ChallengeGroupRunner, challenge_group_registry
from aurora.services.project_deletion import ProjectDeletionService
from aurora.services.project_repair import reopen_project_after_invalid_flag
from aurora.services.target_verification import TargetVerificationService
from aurora.services.target_management import TargetManagementService
from aurora.services.network_proxy import load_network_proxy, network_proxy_registry, save_network_proxy
from aurora.services.worker_control import WorkerControlService


class CreateProjectRequest(BaseModel):
    name: str
    goal: str
    challenge_type: str | None = None
    allowed_hosts: list[str] = Field(default_factory=list)
    hint: str | None = None
    subagents_enabled: bool = False


class SolveProjectRequest(CreateProjectRequest):
    max_iterations: int = 20
    max_minutes: int = 0
    no_progress_limit: int = 2
    stop_on_observer_escalate: bool = True


class CreateIntentRequest(BaseModel):
    objective: str
    capability_tags: list[str] = Field(default_factory=list)
    dependency_fact_ids: list[str] = Field(default_factory=list)
    parent_intent_id: str | None = None
    priority: float = 1.0
    risk_level: str = "low"
    tool_request: dict[str, Any] | None = None


class ExecuteToolRequest(BaseModel):
    request: dict[str, Any] = Field(default_factory=dict)
    worker_id: str | None = None
    intent_id: str | None = None
    attempt_id: str | None = None


class HeartbeatRequest(BaseModel):
    lease_seconds: int = 300


class CreateHintRequest(BaseModel):
    content: str
    source: str = "user"


class EvidenceItemRequest(BaseModel):
    description: str = Field(min_length=1, max_length=2_000)
    artifact_refs: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def description_is_not_blank(self) -> "EvidenceItemRequest":
        if not self.description.strip():
            raise ValueError("evidence description must not be blank")
        return self


class CreateEvidenceFactRequest(BaseModel):
    statement: str = Field(min_length=1, max_length=4_000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    evidence_items: list[EvidenceItemRequest] = Field(default_factory=list, max_length=10)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    category: str = Field(default="analysis", min_length=1, max_length=100)

    @model_validator(mode="after")
    def requires_evidence(self) -> "CreateEvidenceFactRequest":
        if not self.evidence_refs and not self.evidence_items:
            raise ValueError("at least one evidence artifact or structured evidence item is required")
        return self


class BrowserSessionRequest(BaseModel):
    source_url: str = Field(min_length=1, max_length=2_048)
    cookie: str = Field(min_length=1, max_length=16_384)


class AutoRunRequest(BaseModel):
    max_iterations: int = 20
    max_minutes: int = 0
    no_progress_limit: int = 2
    stop_on_observer_escalate: bool = True
    background: bool = False


class HandsFreeImportRequest(BaseModel):
    source_url: str
    cookie: str | None = Field(default=None, max_length=16_384)
    username: str | None = Field(default=None, max_length=512)
    password: str | None = Field(default=None, max_length=4_096)
    login_url: str | None = Field(default=None, max_length=2_048)


class HandsFreeContinueRequest(BaseModel):
    cookie: str | None = Field(default=None, max_length=16_384)
    username: str | None = Field(default=None, max_length=512)
    password: str | None = Field(default=None, max_length=4_096)
    login_url: str | None = Field(default=None, max_length=2_048)


class HandsFreeConfirmRequest(BaseModel):
    candidate_ids: list[str] = Field(default_factory=list)
    name_overrides: dict[str, str] = Field(default_factory=dict)


class ManualTargetRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2_048)
    probe: bool = True


class TargetVerifyRequest(BaseModel):
    confirm_paid: bool = False


class ManualFlagValidationRequest(BaseModel):
    accepted: bool


class NetworkProxyRequest(BaseModel):
    mode: str
    proxy_url: str | None = Field(default=None, max_length=1_000)
    no_proxy: str | None = Field(default=None, max_length=2_000)


class WorkerFactRequest(BaseModel):
    statement: str = Field(min_length=1, max_length=4_000)
    evidence_refs: list[str] = Field(min_length=1, max_length=100)
    category: str = Field(default="analysis", min_length=1, max_length=100)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)


class WorkerCheckpointRequest(BaseModel):
    summary: str = Field(min_length=1, max_length=2_000)
    completed_steps: list[str] = Field(default_factory=list, max_length=20)
    failed_routes: list[str] = Field(default_factory=list, max_length=20)
    next_step: str = Field(default="", max_length=1_000)
    artifact_refs: list[str] = Field(default_factory=list, max_length=100)


class ImportProgressRegistry:
    def __init__(self) -> None:
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._lock = Lock()

    def publish(self, batch_id: str, phase: str, detail: str) -> None:
        with self._lock:
            events = self._events.setdefault(batch_id, [])
            events.append({"id": len(events) + 1, "phase": phase, "detail": detail, "timestamp": time.time()})

    def after(self, batch_id: str, event_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return [event for event in self._events.get(batch_id, []) if event["id"] > event_id]


import_progress_registry = ImportProgressRegistry()


def _run_import_batch(batch_id: str, *, cookie: str | None, username: str | None, password: str | None, login_url: str | None) -> None:
    def progress(phase: str, detail: str) -> None:
        import_progress_registry.publish(batch_id, phase, detail)

    with Session(engine) as session:
        try:
            HandsFreeService().run_batch(session, batch_id, cookie=cookie, username=username, password=password, login_url=login_url, progress=progress)
        except Exception as exc:
            # The service persists the failure state; this fallback covers worker setup failures.
            import_progress_registry.publish(batch_id, "FAILED", str(exc)[:1000])


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        with acquire_api_instance_lock(settings.database_url):
            init_db()
            with Session(engine) as session:
                load_network_proxy(session)
            settings.artifact_dir.mkdir(parents=True, exist_ok=True)
            challenge_group_registry.resume_interrupted_groups()
            stop_reaper = Event()

            def reap_worker_leases() -> None:
                interval = max(1, settings.worker_reap_interval_seconds)
                while not stop_reaper.is_set():
                    try:
                        with Session(engine) as session:
                            Scheduler().reap_all_expired(session)
                    except Exception:
                        # The next pass retries; worker execution must never be
                        # brought down by maintenance failure.
                        pass
                    stop_reaper.wait(interval)

            reaper_thread = Thread(target=reap_worker_leases, name="aurora-worker-reaper", daemon=True)
            reaper_thread.start()
            try:
                yield
            finally:
                stop_reaper.set()
                reaper_thread.join(timeout=max(1, settings.worker_reap_interval_seconds) + 1)

    app = FastAPI(title="Aurora API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route("/favicon.ico", methods=["GET", "HEAD"])
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/api/settings/network-proxy")
    def get_network_proxy() -> dict[str, str | None]:
        return network_proxy_registry.get().public_dict()

    @app.put("/api/settings/network-proxy")
    def update_network_proxy(payload: NetworkProxyRequest, session: Session = Depends(get_session)) -> dict[str, str | None]:
        try:
            return save_network_proxy(session, mode=payload.mode, proxy_url=payload.proxy_url, no_proxy=payload.no_proxy).public_dict()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects")
    def create_project(payload: CreateProjectRequest, session: Session = Depends(get_session)) -> Project:
        return create_project_with_bootstrap(
            session,
            name=payload.name,
            goal=payload.goal,
            challenge_type=payload.challenge_type,
            allowed_hosts=payload.allowed_hosts,
            hint=payload.hint,
            subagents_enabled=payload.subagents_enabled,
        )

    @app.post("/api/hands-free/imports")
    def create_hands_free_import(payload: HandsFreeImportRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
        try:
            batch = HandsFreeService().create_batch(session, payload.source_url, cookie=payload.cookie, username=payload.username, password=payload.password)
            browser_session_registry.register_batch(batch_id=batch.id, source_url=payload.source_url, cookie=payload.cookie)
            import_progress_registry.publish(batch.id, "QUEUED", "导入任务已创建，正在等待执行。")
            Thread(target=_run_import_batch, kwargs={"batch_id": batch.id, "cookie": payload.cookie, "username": payload.username, "password": payload.password, "login_url": payload.login_url}, daemon=True).start()
            return {"batch": batch, "candidates": []}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/hands-free/imports/{batch_id}/continue")
    def continue_hands_free_import(batch_id: str, payload: HandsFreeContinueRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
        try:
            batch = HandsFreeService().prepare_continue(session, batch_id, cookie=payload.cookie, username=payload.username, password=payload.password)
            browser_session_registry.register_batch(batch_id=batch.id, source_url=batch.source_url, cookie=payload.cookie)
            import_progress_registry.publish(batch.id, "QUEUED", "已更新登录会话，正在继续导入。")
            Thread(target=_run_import_batch, kwargs={"batch_id": batch.id, "cookie": payload.cookie, "username": payload.username, "password": payload.password, "login_url": payload.login_url}, daemon=True).start()
            return {"batch": batch, "candidates": []}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/hands-free/imports/{batch_id}/events")
    def stream_hands_free_import_events(batch_id: str, session: Session = Depends(get_session)) -> StreamingResponse:
        if session.get(ImportBatch, batch_id) is None:
            raise HTTPException(status_code=404, detail="import batch not found")

        def stream():
            event_id = 0
            terminal = {"READY", "NEEDS_SESSION", "FAILED"}
            while True:
                events = import_progress_registry.after(batch_id, event_id)
                if events:
                    for event in events:
                        event_id = event["id"]
                        yield f"id: {event_id}\nevent: progress\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if events[-1]["phase"] in terminal:
                        return
                else:
                    yield ": keepalive\n\n"
                time.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/hands-free/imports/{batch_id}")
    def get_hands_free_import(batch_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        try:
            result = HandsFreeService().get_batch(session, batch_id)
            return {"batch": result.batch, "candidates": result.candidates}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/hands-free/imports/{batch_id}/candidates/{candidate_id}/external-attachments/{attachment_index}/download")
    def download_external_attachment(batch_id: str, candidate_id: str, attachment_index: int, session: Session = Depends(get_session)) -> StreamingResponse:
        candidate = session.get(ImportCandidate, candidate_id)
        if candidate is None or candidate.batch_id != batch_id:
            raise HTTPException(status_code=404, detail="import candidate not found")
        if attachment_index < 0 or attachment_index >= len(candidate.external_attachments_json):
            raise HTTPException(status_code=404, detail="external attachment not found")
        item = candidate.external_attachments_json[attachment_index]
        url = item.get("url") if isinstance(item, dict) else None
        if not isinstance(url, str) or not url.strip():
            raise HTTPException(status_code=400, detail="external attachment has no download URL")
        try:
            upstream = HandsFreeService.open_external_attachment(url)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"external attachment download failed: {str(exc)[:300]}") from exc
        filename = HandsFreeService._safe_filename(str(item.get("filename") or HandsFreeService._filename(url)))
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        content_length = upstream.headers.get("Content-Length")
        if content_length and content_length.isdigit():
            headers["Content-Length"] = content_length

        def stream():
            try:
                while chunk := upstream.read(1024 * 1024):
                    yield chunk
            finally:
                upstream.close()

        return StreamingResponse(stream(), media_type=upstream.headers.get_content_type(), headers=headers)

    @app.post("/api/hands-free/imports/{batch_id}/confirm")
    def confirm_hands_free_import(batch_id: str, payload: HandsFreeConfirmRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
        try:
            projects = HandsFreeService().confirm(session, batch_id, payload.candidate_ids, payload.name_overrides)
            browser_session_registry.bind_batch_project_sources(
                batch_id=batch_id,
                project_sources={project["project_id"]: project["challenge_url"] for project in projects if project.get("challenge_url")},
            )
            verifier = TargetVerificationService()
            for project in projects:
                if project["status"] == "created":
                    verification = verifier.verify(session, project_id=project["project_id"], source_url=project.get("challenge_url"), source_metadata=project.get("source_metadata"))
                    project["target_verification_status"] = verification.status
                    project["target_verification_reason"] = verification.reason
                    project["target_url"] = verification.target_url
            group = session.get(ChallengeGroup, projects[0]["group_id"]) if projects else None
            return {"projects": projects, "group": group}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/target/verify")
    def verify_project_target(project_id: str, payload: TargetVerifyRequest | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
        _require_project(session, project_id)
        result = TargetVerificationService().verify(session, project_id=project_id, allow_paid_launch=bool(payload and payload.confirm_paid))
        return {"status": result.status, "reason": result.reason, "target_url": result.target_url}

    @app.post("/api/projects/{project_id}/targets/manual")
    def set_manual_project_target(project_id: str, payload: ManualTargetRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
        _require_project(session, project_id)
        try:
            return TargetManagementService().submit_manual(session, project_id=project_id, url=payload.url, probe=payload.probe).__dict__
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/targets/{target_id}/confirm")
    def confirm_project_target(project_id: str, target_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        _require_project(session, project_id)
        try:
            return TargetManagementService().activate(session, project_id=project_id, target_id=target_id).__dict__
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/solve")
    def solve_project(payload: SolveProjectRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
        project = create_project_with_bootstrap(
            session,
            name=payload.name,
            goal=payload.goal,
            challenge_type=payload.challenge_type,
            allowed_hosts=payload.allowed_hosts,
            hint=payload.hint or payload.goal,
            subagents_enabled=payload.subagents_enabled,
        )
        result = AutoRunnerService().run_until_stop(session, project_id=project.id, limits=_autorun_limits(payload))
        return {"project": project, "autorun": result.__dict__, "summary": get_project_summary(project.id, session)}

    @app.get("/api/projects")
    def list_projects(session: Session = Depends(get_session)) -> list[Project]:
        return session.exec(select(Project).order_by(Project.created_at.desc())).all()

    @app.get("/api/challenge-groups")
    def list_challenge_groups(session: Session = Depends(get_session)) -> list[ChallengeGroup]:
        return session.exec(select(ChallengeGroup).order_by(ChallengeGroup.created_at.desc())).all()

    @app.get("/api/challenge-groups/{group_id}")
    def get_challenge_group(group_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        group = session.get(ChallengeGroup, group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="challenge group not found")
        items = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group_id).order_by(ChallengeGroupItem.position)).all()
        events = session.exec(select(ChallengeGroupEvent).where(ChallengeGroupEvent.group_id == group_id).order_by(ChallengeGroupEvent.created_at.desc()).limit(100)).all()
        projects = {project.id: project for project in session.exec(select(Project).where(Project.id.in_([item.project_id for item in items]))).all()}
        group_candidates = session.exec(
            select(FlagCandidate).where(FlagCandidate.project_id.in_([item.project_id for item in items]))
        ).all() if items else []
        candidate_flags = {
            candidate.project_id: candidate.value
            for candidate in group_candidates
            if candidate.status in {"LOCAL_VERIFIED", "SUBMITTED", "ACCEPTED", "AWAITING_MANUAL_VALIDATION"}
        }
        return {"group": group, "items": items, "projects": projects, "candidate_flags": candidate_flags, "flag_candidates": group_candidates, "events": events, "background": challenge_group_registry.status(group_id)}

    @app.post("/api/challenge-groups/{group_id}/items/{item_id}/flag-validation")
    def validate_group_flag_manually(
        group_id: str,
        item_id: str,
        payload: ManualFlagValidationRequest,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        try:
            item = ChallengeGroupRunner().validate_flag_manually(
                session,
                group_id=group_id,
                item_id=item_id,
                accepted=payload.accepted,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        group = session.get(ChallengeGroup, group_id)
        background = (
            challenge_group_registry.resume_after_manual_validation(group_id)
            if group is not None and group.status == "READY"
            else None
        )
        return {
            "status": "accepted" if payload.accepted else "rejected",
            "item": item,
            "background": background.__dict__ if background else None,
        }

    @app.delete("/api/challenge-groups/{group_id}")
    def delete_challenge_group(group_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        try:
            result = ProjectDeletionService().delete_group(session, group_id=group_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "deleted", **result}

    @app.post("/api/challenge-groups/{group_id}/start")
    def start_challenge_group(group_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        group = session.get(ChallengeGroup, group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="challenge group not found")
        if group.status == "COMPLETED":
            raise HTTPException(status_code=409, detail="challenge group is completed")
        if group.status == "AWAITING_MANUAL_VALIDATION":
            raise HTTPException(status_code=409, detail="challenge group is awaiting manual flag validation")
        state = challenge_group_registry.start(group_id)
        return {"status": "started", "background": state.__dict__}

    @app.post("/api/challenge-groups/{group_id}/stop")
    def stop_challenge_group(group_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        if session.get(ChallengeGroup, group_id) is None:
            raise HTTPException(status_code=404, detail="challenge group not found")
        state = challenge_group_registry.stop(group_id)
        items = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == group_id)).all()
        containers = {item.project_id: stop_project_containers(item.project_id) for item in items}
        return {"status": "stopping" if state else "stopped", "background": state.__dict__ if state else None, "containers": containers}

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str, session: Session = Depends(get_session)) -> Project:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return project

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        try:
            result = ProjectDeletionService().delete_project(session, project_id=project_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "status": "deleted",
            "project_id": result.project_id,
            "removed_group_items": result.removed_group_items,
            "stopped_group_ids": result.stopped_groups,
            "containers": result.containers,
        }

    @app.get("/api/projects/{project_id}/runtime-policy")
    def get_runtime_policy(project_id: str, session: Session = Depends(get_session)) -> ProjectRuntimePolicy:
        _require_project(session, project_id)
        policy = session.exec(select(ProjectRuntimePolicy).where(ProjectRuntimePolicy.project_id == project_id)).first()
        if policy is None:
            raise HTTPException(status_code=404, detail="runtime policy not found")
        return policy

    @app.post("/api/projects/{project_id}/browser/session")
    def set_browser_session(project_id: str, payload: BrowserSessionRequest, session: Session = Depends(get_session)) -> dict[str, str]:
        _require_project(session, project_id)
        try:
            browser_session_registry.set_project_session(project_id=project_id, source_url=payload.source_url, cookie=payload.cookie)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session.add(WorkerEvent(project_id=project_id, event_type="browser.session.updated", payload_json={"source_url": payload.source_url}))
        session.commit()
        return {"status": "ready"}

    @app.get("/internal/workers/{worker_id}/blackboard")
    def worker_blackboard(worker_id: str, authorization: str | None = Header(default=None), session: Session = Depends(get_session)) -> dict[str, Any]:
        service = WorkerControlService()
        try:
            worker, attempt = service.authenticate(session, worker_id=worker_id, token=_bearer_token(authorization))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return service.query(session, worker=worker, attempt=attempt)

    @app.post("/internal/workers/{worker_id}/facts")
    def worker_append_fact(worker_id: str, payload: WorkerFactRequest, authorization: str | None = Header(default=None), session: Session = Depends(get_session)) -> dict[str, Any]:
        service = WorkerControlService()
        try:
            worker, attempt = service.authenticate(session, worker_id=worker_id, token=_bearer_token(authorization))
            return service.append_fact(session, worker=worker, attempt=attempt, **payload.model_dump())
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/internal/workers/{worker_id}/checkpoint")
    def worker_save_checkpoint(worker_id: str, payload: WorkerCheckpointRequest, authorization: str | None = Header(default=None), session: Session = Depends(get_session)) -> dict[str, Any]:
        service = WorkerControlService()
        try:
            worker, attempt = service.authenticate(session, worker_id=worker_id, token=_bearer_token(authorization))
            return service.save_checkpoint(session, worker=worker, attempt=attempt, **payload.model_dump())
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/intents")
    def create_intent(project_id: str, payload: CreateIntentRequest, session: Session = Depends(get_session)) -> Intent:
        _require_mutable_project(session, project_id)
        data = payload.model_dump(exclude={"tool_request"})
        budget = {"tool_request": payload.tool_request} if payload.tool_request else {}
        result = BlackboardRepository().upsert_intent(session, project_id=project_id, budget=budget, **data)
        return result.item

    @app.get("/api/projects/{project_id}/intents")
    def list_intents(project_id: str, session: Session = Depends(get_session)) -> list[Intent]:
        _require_project(session, project_id)
        return session.exec(select(Intent).where(Intent.project_id == project_id).order_by(Intent.created_at)).all()

    @app.post("/api/projects/{project_id}/hints")
    def create_hint(project_id: str, payload: CreateHintRequest, session: Session = Depends(get_session)) -> Hint:
        _require_mutable_project(session, project_id)
        hint = Hint(project_id=project_id, content=payload.content, source=payload.source)
        session.add(hint)
        session.commit()
        session.refresh(hint)
        session.add(
            WorkerEvent(
                project_id=project_id,
                event_type="hint.created",
                payload_json={"hint_id": hint.id, "source": hint.source, "content": hint.content},
            )
        )
        session.commit()
        session.refresh(hint)
        return hint

    @app.post("/api/projects/{project_id}/facts")
    def derive_fact_from_evidence(project_id: str, payload: CreateEvidenceFactRequest, session: Session = Depends(get_session)) -> Fact:
        _require_mutable_project(session, project_id)
        evidence_items = [
            {"description": item.description.strip(), "artifact_refs": list(dict.fromkeys(item.artifact_refs))}
            for item in payload.evidence_items
        ]
        evidence_refs = list(dict.fromkeys([
            *payload.evidence_refs,
            *(artifact_id for item in evidence_items for artifact_id in item["artifact_refs"]),
        ]))
        artifacts = [session.get(Artifact, artifact_id) for artifact_id in evidence_refs]
        if any(artifact is None or artifact.project_id != project_id for artifact in artifacts):
            raise HTTPException(status_code=400, detail="every evidence artifact must belong to this project")
        result = BlackboardRepository().upsert_fact(
            session,
            project_id=project_id,
            statement=payload.statement.strip(),
            category=payload.category.strip(),
            confidence=payload.confidence,
            evidence_refs=evidence_refs,
            evidence_items=evidence_items,
        )
        session.add(
            WorkerEvent(
                project_id=project_id,
                event_type="fact.derived_from_evidence",
                payload_json={"fact_id": result.item.id, "evidence_refs": evidence_refs, "evidence_items": evidence_items, "created": result.created},
            )
        )
        session.commit()
        session.refresh(result.item)
        return result.item

    @app.get("/api/projects/{project_id}/hints")
    def list_hints(project_id: str, session: Session = Depends(get_session)) -> list[Hint]:
        _require_project(session, project_id)
        return session.exec(select(Hint).where(Hint.project_id == project_id).order_by(Hint.created_at.desc())).all()

    @app.post("/api/projects/{project_id}/rethink")
    def rethink(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        _require_project(session, project_id)
        running_workers = session.exec(select(Worker).where(Worker.project_id == project_id, Worker.status == "RUNNING")).all()
        background_autorun = autorun_registry.status(project_id)
        autorun_active = bool(background_autorun and background_autorun.get("status") in {"running", "stopping"})
        if autorun_active or running_workers:
            try:
                state = project_rethink_registry.start(project_id=project_id)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return {"status": "working", "phase": state.status, "rethink": state.__dict__}

        autorun_registry.stop(project_id)
        containers = stop_project_containers(project_id)
        if running_workers:
            raise HTTPException(status_code=409, detail="active workers did not stop; retry after they have stopped")
        try:
            bootstrap_intent = rethink_project(session, project_id=project_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        autorun = autorun_registry.start(project_id=project_id, limits=AutoRunLimits())
        return {"status": "working", "bootstrap_intent": bootstrap_intent, "autorun": autorun.__dict__, "containers": containers}

    @app.post("/api/projects/{project_id}/run-demo")
    def run_demo(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        _require_project(session, project_id)
        return run_one_demo_step(session, project_id=project_id)

    @app.post("/api/projects/{project_id}/scheduler/run-next")
    def run_next_intent(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        _require_project(session, project_id)
        return run_one_demo_step(session, project_id=project_id)

    @app.post("/api/projects/{project_id}/scheduler/reap-expired")
    def reap_expired(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        _require_project(session, project_id)
        reaped = Scheduler().reap_expired(session, project_id=project_id)
        return {"reaped": reaped}

    @app.post("/api/projects/{project_id}/observer/run")
    def run_observer(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        _require_project(session, project_id)
        decision = ObserverService().analyze_project(session, project_id=project_id)
        return {
            "decision": decision.decision,
            "severity": decision.severity,
            "reason": decision.reason,
            "references": decision.references,
        }

    @app.post("/api/projects/{project_id}/autorun/start")
    def autorun_start(project_id: str, payload: AutoRunRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
        _require_project(session, project_id)
        if payload.background:
            state = autorun_registry.start(project_id=project_id, limits=_autorun_limits(payload))
            return {"autorun": state.__dict__, "summary": get_project_summary(project_id, session)}
        result = AutoRunnerService().run_until_stop(session, project_id=project_id, limits=_autorun_limits(payload))
        return {"autorun": result.__dict__, "summary": get_project_summary(project_id, session)}

    @app.post("/api/projects/{project_id}/autorun/step")
    def autorun_step(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        _require_project(session, project_id)
        return AutoRunnerService().step(session, project_id=project_id)

    @app.post("/api/projects/{project_id}/autorun/stop")
    def autorun_stop(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        _require_project(session, project_id)
        state = autorun_registry.stop(project_id)
        containers = stop_project_containers(project_id)
        session.add(WorkerEvent(project_id=project_id, event_type="autorun.stopped", payload_json={"reason": "manual_stop"}))
        session.commit()
        return {
            "status": "stopping" if state else "stopped",
            "reason": "manual_stop",
            "autorun": state.__dict__ if state else None,
            "containers": containers,
        }

    @app.get("/api/projects/{project_id}/autorun/status")
    def autorun_status(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        _require_project(session, project_id)
        status = AutoRunnerService().status(session, project_id=project_id)
        status["background"] = autorun_registry.status(project_id)
        status["rethink"] = project_rethink_registry.status(project_id)
        return status

    @app.post("/api/projects/{project_id}/manager/run")
    def run_manager(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        _require_project(session, project_id)
        decision = ManagerService().run_project(session, project_id=project_id)
        return {
            "status": decision.status,
            "reason": decision.reason,
            "proposed_intents": decision.proposed_intents,
        }

    @app.post("/api/workers/{worker_id}/heartbeat")
    def worker_heartbeat(worker_id: str, payload: HeartbeatRequest, session: Session = Depends(get_session)) -> Worker:
        worker = Scheduler().heartbeat(session, worker_id=worker_id, lease_seconds=payload.lease_seconds)
        if worker is None:
            raise HTTPException(status_code=404, detail="running worker lease not found")
        return worker

    @app.post("/api/projects/{project_id}/tools/{tool_name}/execute")
    def execute_tool(
        project_id: str,
        tool_name: str,
        payload: ExecuteToolRequest,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        _require_project(session, project_id)
        result = CapabilityGateway().execute(
            session,
            project_id=project_id,
            tool_name=tool_name,
            request=payload.request,
            worker_id=payload.worker_id,
            intent_id=payload.intent_id,
            attempt_id=payload.attempt_id,
        )
        return {
            "success": result.success,
            "summary": result.summary,
            "artifact_refs": result.artifact_refs,
            "metrics": result.metrics,
            "warnings": result.warnings,
            "trace_id": result.trace_id,
        }

    @app.get("/api/projects/{project_id}/blackboard")
    def get_blackboard(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        _require_project(session, project_id)
        return {
            "project": session.get(Project, project_id),
            "facts": session.exec(select(Fact).where(Fact.project_id == project_id).order_by(Fact.created_at)).all(),
            "intents": session.exec(select(Intent).where(Intent.project_id == project_id).order_by(Intent.created_at)).all(),
            "attempts": session.exec(select(Attempt).where(Attempt.project_id == project_id).order_by(Attempt.started_at)).all(),
            "artifacts": session.exec(select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.created_at)).all(),
            "findings": session.exec(select(Finding).where(Finding.project_id == project_id).order_by(Finding.created_at)).all(),
            "flag_candidates": session.exec(select(FlagCandidate).where(FlagCandidate.project_id == project_id).order_by(FlagCandidate.created_at)).all(),
            "workers": session.exec(select(Worker).where(Worker.project_id == project_id).order_by(Worker.created_at)).all(),
            "checkpoints": session.exec(select(AttemptCheckpoint).where(AttemptCheckpoint.project_id == project_id).order_by(AttemptCheckpoint.created_at)).all(),
        }

    @app.get("/api/projects/{project_id}/summary")
    def get_project_summary(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        project = _require_project(session, project_id)
        findings = session.exec(select(Finding).where(Finding.project_id == project_id).order_by(Finding.created_at.desc())).all()
        facts = session.exec(select(Fact).where(Fact.project_id == project_id, Fact.status == "ACTIVE").order_by(Fact.created_at.desc())).all()
        intents = session.exec(select(Intent).where(Intent.project_id == project_id).order_by(Intent.created_at.desc())).all()
        attempts = session.exec(select(Attempt).where(Attempt.project_id == project_id).order_by(Attempt.started_at.desc())).all()
        artifacts = session.exec(select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.created_at.desc())).all()
        tool_traces = session.exec(select(ToolTrace).where(ToolTrace.project_id == project_id).order_by(ToolTrace.created_at.desc())).all()
        llm_traces = session.exec(select(LLMTrace).where(LLMTrace.project_id == project_id).order_by(LLMTrace.created_at.desc())).all()
        context_snapshots = session.exec(
            select(ContextSnapshot).where(ContextSnapshot.project_id == project_id).order_by(ContextSnapshot.created_at.desc())
        ).all()
        checkpoints = session.exec(select(AttemptCheckpoint).where(AttemptCheckpoint.project_id == project_id).order_by(AttemptCheckpoint.created_at.desc())).all()
        events = session.exec(select(WorkerEvent).where(WorkerEvent.project_id == project_id).order_by(WorkerEvent.created_at.desc()).limit(200)).all()
        flag_candidates = session.exec(select(FlagCandidate).where(FlagCandidate.project_id == project_id).order_by(FlagCandidate.created_at.desc())).all()
        return {
            "project": project,
            "counts": {
                "findings": len(findings),
                "active_facts": len(facts),
                "intents": len(intents),
                "attempts": len(attempts),
                "artifacts": len(artifacts),
                "tool_traces": len(tool_traces),
                "llm_traces": len(llm_traces),
                "context_snapshots": len(context_snapshots),
                "checkpoints": len(checkpoints),
                "events_returned": len(events),
                "flag_candidates": len(flag_candidates),
            },
            "findings": findings,
            "flag_candidates": flag_candidates,
            "recent_facts": facts[:20],
            "intents_by_status": _count_by_status([intent.status for intent in intents]),
            "attempts_by_status": _count_by_status([attempt.status for attempt in attempts]),
            "recent_artifacts": artifacts[:20],
            "recent_tool_traces": tool_traces[:20],
            "recent_llm_traces": llm_traces[:10],
            "latest_context_snapshot": context_snapshots[0] if context_snapshots else None,
            "recent_checkpoints": checkpoints[:10],
            "recent_events": events,
        }

    @app.get("/api/projects/{project_id}/findings")
    def list_findings(project_id: str, session: Session = Depends(get_session)) -> list[Finding]:
        _require_project(session, project_id)
        return session.exec(select(Finding).where(Finding.project_id == project_id).order_by(Finding.created_at.desc())).all()

    @app.get("/api/projects/{project_id}/flag-candidates")
    def list_flag_candidates(project_id: str, session: Session = Depends(get_session)) -> list[FlagCandidate]:
        _require_project(session, project_id)
        return session.exec(select(FlagCandidate).where(FlagCandidate.project_id == project_id).order_by(FlagCandidate.created_at.desc())).all()

    @app.post("/api/projects/{project_id}/flag-candidates/{candidate_id}/validation")
    def validate_project_flag_manually(
        project_id: str,
        candidate_id: str,
        payload: ManualFlagValidationRequest,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        project = _require_project(session, project_id)
        candidate = session.get(FlagCandidate, candidate_id)
        if candidate is None or candidate.project_id != project_id:
            raise HTTPException(status_code=404, detail="flag candidate not found")
        if candidate.status not in {"LOCAL_VERIFIED", "SUBMITTED", "AWAITING_MANUAL_VALIDATION"}:
            raise HTTPException(status_code=409, detail="flag candidate is not awaiting a final decision")

        awaiting_items = session.exec(
            select(ChallengeGroupItem).where(
                ChallengeGroupItem.project_id == project_id,
                ChallengeGroupItem.submission_status == "AWAITING_MANUAL_VALIDATION",
            )
        ).all()
        if awaiting_items:
            runner = ChallengeGroupRunner()
            affected_group_ids: list[str] = []
            try:
                for item in awaiting_items:
                    runner.validate_flag_manually(
                        session,
                        group_id=item.group_id,
                        item_id=item.id,
                        accepted=payload.accepted,
                        candidate_id=candidate_id,
                    )
                    if item.group_id not in affected_group_ids:
                        affected_group_ids.append(item.group_id)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

            backgrounds: dict[str, Any] = {}
            for group_id in affected_group_ids:
                group = session.get(ChallengeGroup, group_id)
                if group is not None and group.status == "READY":
                    state = challenge_group_registry.resume_after_manual_validation(group_id)
                    backgrounds[group_id] = state.__dict__ if state else None
            session.refresh(candidate)
            return {
                "status": "accepted" if payload.accepted else "rejected",
                "candidate": candidate,
                "groups": affected_group_ids,
                "backgrounds": backgrounds,
            }

        candidate.updated_at = now_utc()
        if payload.accepted:
            candidate.status = "ACCEPTED"
            project.status = "COMPLETED"
            project.updated_at = now_utc()
            pending_intents = session.exec(select(Intent).where(Intent.project_id == project_id, Intent.status == "PENDING")).all()
            for intent in pending_intents:
                intent.status = "CANCELLED"
                intent.updated_at = now_utc()
                session.add(intent)
            session.add(WorkerEvent(project_id=project_id, event_type="project.completed", payload_json={"reason": "flag manually accepted", "candidate_id": candidate.id, "value": candidate.value, "cancelled_intent_ids": [intent.id for intent in pending_intents]}))
            session.add(project)
            session.add(candidate)
            session.commit()
        else:
            candidate.status = "REJECTED"
            candidate.rejection_reason = "manual validation rejected the candidate flag"
            session.add(candidate)
            finding = session.exec(select(Finding).where(Finding.project_id == project_id, Finding.title == f"Candidate flag: {candidate.value}")).first()
            if finding is None:
                raise HTTPException(status_code=409, detail="candidate finding not found")
            reopen_project_after_invalid_flag(session, project_id=project_id, finding_id=finding.id, reason=candidate.rejection_reason)
        session.refresh(candidate)
        return {"status": "accepted" if payload.accepted else "rejected", "candidate": candidate}

    @app.get("/api/projects/{project_id}/checkpoints")
    def list_checkpoints(project_id: str, session: Session = Depends(get_session)) -> list[AttemptCheckpoint]:
        _require_project(session, project_id)
        return session.exec(select(AttemptCheckpoint).where(AttemptCheckpoint.project_id == project_id).order_by(AttemptCheckpoint.created_at.desc())).all()

    @app.get("/api/projects/{project_id}/artifacts")
    def list_artifacts(project_id: str, session: Session = Depends(get_session)) -> list[Artifact]:
        _require_project(session, project_id)
        return session.exec(select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.created_at.desc())).all()

    @app.get("/api/projects/{project_id}/targets")
    def list_discovered_targets(project_id: str, session: Session = Depends(get_session)) -> list[DiscoveredTarget]:
        _require_project(session, project_id)
        return session.exec(select(DiscoveredTarget).where(DiscoveredTarget.project_id == project_id).order_by(DiscoveredTarget.created_at.desc())).all()

    @app.get("/api/projects/{project_id}/warnings")
    def list_runtime_warnings(project_id: str, session: Session = Depends(get_session)) -> list[WorkerEvent]:
        _require_project(session, project_id)
        return list_active_runtime_warnings(session, project_id=project_id)

    @app.post("/api/projects/{project_id}/warnings/{warning_event_id}/acknowledge")
    def acknowledge_warning(project_id: str, warning_event_id: str, session: Session = Depends(get_session)) -> dict[str, str]:
        _require_project(session, project_id)
        if not acknowledge_runtime_warning(session, project_id=project_id, warning_event_id=warning_event_id):
            raise HTTPException(status_code=404, detail="runtime warning not found")
        return {"status": "acknowledged", "warning_event_id": warning_event_id}

    @app.get("/api/artifacts/{artifact_id}/content")
    def get_artifact_content(artifact_id: str, max_bytes: int = 64_000, session: Session = Depends(get_session)) -> dict[str, Any]:
        artifact = session.get(Artifact, artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        return {
            "artifact": artifact,
            "content": ArtifactStore().read_text(artifact, max_bytes=max_bytes),
            "max_bytes": max_bytes,
            "truncated": artifact.size > max_bytes,
        }

    @app.get("/api/projects/{project_id}/attempts")
    def list_attempts(project_id: str, session: Session = Depends(get_session)) -> list[Attempt]:
        _require_project(session, project_id)
        return session.exec(select(Attempt).where(Attempt.project_id == project_id).order_by(Attempt.started_at.desc())).all()

    @app.get("/api/projects/{project_id}/debug/context-snapshots")
    def list_context_snapshots(project_id: str, session: Session = Depends(get_session)) -> list[ContextSnapshot]:
        _require_project(session, project_id)
        return session.exec(
            select(ContextSnapshot).where(ContextSnapshot.project_id == project_id).order_by(ContextSnapshot.created_at.desc())
        ).all()

    @app.get("/api/context-snapshots/{snapshot_id}")
    def get_context_snapshot(snapshot_id: str, session: Session = Depends(get_session)) -> ContextSnapshot:
        snapshot = session.get(ContextSnapshot, snapshot_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="context snapshot not found")
        return snapshot

    @app.get("/api/projects/{project_id}/debug/llm-traces")
    def list_llm_traces(project_id: str, session: Session = Depends(get_session)) -> list[LLMTrace]:
        _require_project(session, project_id)
        return session.exec(select(LLMTrace).where(LLMTrace.project_id == project_id).order_by(LLMTrace.created_at.desc())).all()

    @app.get("/api/projects/{project_id}/debug/tool-traces")
    def list_project_tool_traces(project_id: str, session: Session = Depends(get_session)) -> list[ToolTrace]:
        _require_project(session, project_id)
        return session.exec(select(ToolTrace).where(ToolTrace.project_id == project_id).order_by(ToolTrace.created_at.desc())).all()

    @app.get("/api/projects/{project_id}/events")
    def list_project_events(project_id: str, limit: int = 100, session: Session = Depends(get_session)) -> list[WorkerEvent]:
        _require_project(session, project_id)
        safe_limit = min(max(limit, 1), 500)
        return session.exec(
            select(WorkerEvent)
            .where(WorkerEvent.project_id == project_id)
            .order_by(WorkerEvent.created_at.desc())
            .limit(safe_limit)
        ).all()

    @app.get("/api/projects/{project_id}/events/stream")
    def stream_project_events(
        project_id: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        """Stream project events in chronological order for the live execution UI."""
        with Session(engine) as session:
            _require_project(session, project_id)

        def stream():
            cursor = last_event_id
            sent_initial_batch = False
            while True:
                with Session(engine) as session:
                    events = session.exec(
                        select(WorkerEvent)
                        .where(WorkerEvent.project_id == project_id)
                        .order_by(WorkerEvent.created_at, WorkerEvent.id)
                    ).all()

                if cursor:
                    cursor_index = next((index for index, event in enumerate(events) if event.id == cursor), None)
                    pending = events[cursor_index + 1 :] if cursor_index is not None else events[-100:]
                else:
                    pending = events[-100:] if not sent_initial_batch else []

                if pending:
                    for event in pending:
                        payload = event.model_dump(mode="json")
                        yield f"id: {event.id}\nevent: project-event\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        cursor = event.id
                    sent_initial_batch = True
                else:
                    yield ": keepalive\n\n"
                time.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/projects/{project_id}/runtime/logs")
    def get_runtime_logs(project_id: str, tail: int = 200, session: Session = Depends(get_session)) -> dict[str, Any]:
        _require_project(session, project_id)
        return get_project_container_logs(project_id, tail=tail)

    @app.get("/api/llm-traces/{trace_id}")
    def get_llm_trace(trace_id: str, session: Session = Depends(get_session)) -> LLMTrace:
        trace = session.get(LLMTrace, trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="llm trace not found")
        return trace

    @app.get("/api/workers/{worker_id}/events")
    def list_worker_events(worker_id: str, session: Session = Depends(get_session)) -> list[WorkerEvent]:
        return session.exec(select(WorkerEvent).where(WorkerEvent.worker_id == worker_id).order_by(WorkerEvent.created_at)).all()

    @app.get("/api/attempts/{attempt_id}/tool-traces")
    def list_tool_traces(attempt_id: str, session: Session = Depends(get_session)) -> list[ToolTrace]:
        return session.exec(select(ToolTrace).where(ToolTrace.attempt_id == attempt_id).order_by(ToolTrace.created_at)).all()

    web_dist = Path(__file__).resolve().parent.parent / "apps" / "web" / "dist"
    if (web_dist / "index.html").exists() and (web_dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")

        @app.get("/")
        def serve_index() -> FileResponse:
            return FileResponse(web_dist / "index.html")

    return app


def _require_project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _bearer_token(authorization: str | None) -> str:
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        return ""
    return token.strip()


def _require_mutable_project(session: Session, project_id: str) -> Project:
    project = _require_project(session, project_id)
    if project.status in {"COMPLETED", "CANCELLED", "FAILED", "FLAG_READY", "AWAITING_MANUAL_VALIDATION"}:
        raise HTTPException(status_code=409, detail=f"project is {project.status.lower()}")
    return project


def _count_by_status(statuses: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    return counts


def _autorun_limits(payload: AutoRunRequest | SolveProjectRequest) -> AutoRunLimits:
    return AutoRunLimits(
        max_iterations=payload.max_iterations,
        max_minutes=payload.max_minutes,
        no_progress_limit=payload.no_progress_limit,
        stop_on_observer_escalate=payload.stop_on_observer_escalate,
    )
