from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import Column, UniqueConstraint
from sqlalchemy.types import JSON, LargeBinary
from sqlmodel import Field, SQLModel


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class NetworkProxySetting(SQLModel, table=True):
    id: str = Field(default="global", primary_key=True)
    mode: str = "system"
    proxy_url: str | None = None
    no_proxy: str = "127.0.0.1,localhost,aurora-cc-switch"
    updated_at: datetime = Field(default_factory=now_utc)


class OpenVPNSetting(SQLModel, table=True):
    id: str = Field(default="global", primary_key=True)
    encrypted_payload: bytes = Field(sa_column=Column(LargeBinary))
    salt: bytes = Field(sa_column=Column(LargeBinary))
    nonce: bytes = Field(sa_column=Column(LargeBinary))
    routes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    credentials_configured: bool = False
    updated_at: datetime = Field(default_factory=now_utc)


class SchemaVersion(SQLModel, table=True):
    id: str = Field(default="aurora", primary_key=True)
    version: int = 1
    updated_at: datetime = Field(default_factory=now_utc)


class Project(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("proj"), primary_key=True)
    name: str
    goal: str
    challenge_type: str | None = None
    status: str = "ACTIVE"
    authorization_scope_id: str | None = None
    sandbox_id: str | None = None
    token_budget: int | None = None
    time_budget: int | None = None
    tool_call_budget: int | None = None
    target_verification_status: str = Field(default="UNVERIFIED", index=True)
    target_verification_reason: str | None = None
    target_verified_at: datetime | None = None
    target_url: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class ProjectRuntimePolicy(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("runtime"), primary_key=True)
    project_id: str = Field(index=True, unique=True)
    subagents_enabled: bool = False
    max_subagents_per_worker: int = 2
    max_subagents_concurrent: int = 2
    created_at: datetime = Field(default_factory=now_utc)


class AuthorizationScope(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("scope"), primary_key=True)
    project_id: str = Field(index=True)
    allowed_hosts: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    allowed_domains: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    allowed_ports: list[int] = Field(default_factory=list, sa_column=Column(JSON))
    allowed_protocols: list[str] = Field(default_factory=lambda: ["http", "https", "tcp"], sa_column=Column(JSON))
    allowed_actions: list[str] = Field(default_factory=lambda: ["read", "scan", "analyze", "execute_limited"], sa_column=Column(JSON))
    expires_at: datetime | None = None
    max_request_rate: int | None = None
    deny_metadata_and_management_networks: bool = True
    created_at: datetime = Field(default_factory=now_utc)


class DiscoveredTarget(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("target"), primary_key=True)
    project_id: str = Field(index=True)
    url: str
    host: str = Field(index=True)
    source_artifact_id: str | None = Field(default=None, index=True)
    source: str = Field(default="automatic", index=True)
    confidence: float = 0.0
    probe_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = Field(default="ACTIVE", index=True)
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
    invalidated_at: datetime | None = None


class Fact(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("fact"), primary_key=True)
    project_id: str = Field(index=True)
    statement: str
    category: str = "general"
    confidence: float = 0.5
    evidence_refs: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    evidence_items: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    source_intent_id: str | None = Field(default=None, index=True)
    source_attempt_id: str | None = Field(default=None, index=True)
    status: str = "ACTIVE"
    supersedes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)


class Intent(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("intent"), primary_key=True)
    project_id: str = Field(index=True)
    objective: str
    capability_tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    dependency_fact_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    parent_intent_id: str | None = Field(default=None, index=True)
    priority: float = 1.0
    risk_level: str = "low"
    status: str = Field(default="PENDING", index=True)
    lease_owner: str | None = Field(default=None, index=True)
    lease_expires_at: datetime | None = Field(default=None, index=True)
    lease_generation: int = 0
    retry_count: int = 0
    max_retries: int = 1
    budget: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class Attempt(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("attempt"), primary_key=True)
    project_id: str = Field(index=True)
    intent_id: str = Field(index=True)
    worker_id: str = Field(index=True)
    parent_attempt_id: str | None = Field(default=None, index=True)
    codex_thread_id: str | None = None
    codex_turn_id: str | None = None
    codex_control_token_hash: str | None = None
    last_event_at: datetime | None = None
    resume_count: int = 0
    blackboard_version: int = 0
    lease_generation: int = 0
    status: str = "RUNNING"
    finalization_reason: str | None = None
    resume_manifest_artifact_id: str | None = Field(default=None, index=True)
    result_summary: str | None = None
    failure_reason: str | None = None
    artifact_refs: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    token_usage: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    tool_calls: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    started_at: datetime = Field(default_factory=now_utc)
    finished_at: datetime | None = None


class AttemptCheckpoint(SQLModel, table=True):
    """An evidence-linked reflection and handoff from one solver round to the next."""
    id: str = Field(default_factory=lambda: new_id("checkpoint"), primary_key=True)
    project_id: str = Field(index=True)
    intent_id: str = Field(index=True)
    worker_id: str | None = Field(default=None, index=True)
    attempt_id: str = Field(index=True, unique=True)
    parent_checkpoint_id: str | None = Field(default=None, index=True)
    status: str = Field(default="COMPLETED", index=True)
    summary: str
    conclusions: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    hypotheses: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    failed_routes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    next_steps: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    fact_refs: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    artifact_refs: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    generated_intent_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    budget_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    source: str = "planner"
    created_at: datetime = Field(default_factory=now_utc)


class Worker(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("worker"), primary_key=True)
    project_id: str = Field(index=True)
    intent_id: str = Field(index=True)
    parent_worker_id: str | None = Field(default=None, index=True)
    execution_kind: str = "primary"
    agent_profile_id: str = "solver.general"
    sandbox_id: str | None = None
    capability_set: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="STARTING", index=True)
    lease_generation: int = 0
    lease: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    heartbeat: datetime = Field(default_factory=now_utc)
    budgets: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class Artifact(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("artifact"), primary_key=True)
    project_id: str = Field(index=True)
    source_attempt_id: str | None = Field(default=None, index=True)
    type: str = "text"
    path: str
    sha256: str
    mime_type: str | None = None
    size: int = 0
    summary: str | None = None
    sensitivity: str = "normal"
    origin_kind: str = "unclassified"
    created_at: datetime = Field(default_factory=now_utc)


class ImportBatch(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("import"), primary_key=True)
    source_url: str
    status: str = Field(default="SCANNING", index=True)
    title: str | None = None
    summary: str | None = None
    error: str | None = None
    auth_method: str | None = None
    login_domain: str | None = None
    auth_message: str | None = None
    platform: str | None = None
    extraction_strategy: str | None = None
    pages_scanned: int = 0
    diagnostics_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class ImportCandidate(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("candidate"), primary_key=True)
    batch_id: str = Field(index=True)
    title: str
    description: str | None = None
    challenge_url: str
    challenge_type: str | None = None
    confidence: float = 0.5
    staged_attachments_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    external_attachments_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    evidence_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    source_metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    confirmed: bool = False
    project_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class ImportArtifact(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("import_artifact"), primary_key=True)
    batch_id: str = Field(index=True)
    source_url: str
    filename: str
    path: str
    sha256: str
    mime_type: str | None = None
    size: int = 0
    created_at: datetime = Field(default_factory=now_utc)


class ChallengeGroup(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("group"), primary_key=True)
    import_batch_id: str | None = Field(default=None, index=True)
    name: str
    status: str = Field(default="READY", index=True)
    current_item_id: str | None = Field(default=None, index=True)
    limits: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    deadline_at: datetime | None = Field(default=None, index=True)
    max_concurrent: int = 1
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class ChallengeGroupItem(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("group_item"), primary_key=True)
    group_id: str = Field(index=True)
    project_id: str = Field(index=True)
    position: int
    status: str = Field(default="PENDING", index=True)
    fused_status: str = Field(default="PENDING", index=True)
    phase: int = Field(default=1, index=True)
    phase_attempts: dict[str, int] = Field(default_factory=dict, sa_column=Column(JSON))
    failure_history: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    competition_meta: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    hint_taken: bool = False
    hint_content: str | None = None
    submission_status: str = "NOT_SUBMITTED"
    stop_reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class ChallengeGroupEvent(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("group_event"), primary_key=True)
    group_id: str = Field(index=True)
    item_id: str | None = Field(default=None, index=True)
    event_type: str
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)


class EvaluationSuite(SQLModel, table=True):
    """Immutable challenge inventory used for comparable solver evaluations."""
    id: str = Field(default_factory=lambda: new_id("eval_suite"), primary_key=True)
    name: str
    platform: str = Field(default="tsecbench", index=True)
    items_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    content_hash: str = Field(index=True)
    created_at: datetime = Field(default_factory=now_utc)


class EvaluationRun(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("eval_run"), primary_key=True)
    suite_id: str = Field(index=True)
    label: str
    variant: str = Field(default="candidate", index=True)
    status: str = Field(default="READY", index=True)
    group_id: str | None = Field(default=None, index=True)
    config_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=now_utc)


class EvaluationItemResult(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("run_id", "challenge_key", name="uq_evaluationitem_run_challenge"),)
    id: str = Field(default_factory=lambda: new_id("eval_item"), primary_key=True)
    run_id: str = Field(index=True)
    challenge_key: str = Field(index=True)
    project_id: str | None = Field(default=None, index=True)
    status: str = Field(default="PENDING", index=True)
    platform_correct: bool | None = None
    platform_completed: bool = False
    environment_error: bool = False
    elapsed_seconds: float | None = None
    metrics_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class Finding(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("finding"), primary_key=True)
    project_id: str = Field(index=True)
    severity: str = "info"
    title: str
    reproduction: str | None = None
    evidence_refs: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)


class FlagCandidate(SQLModel, table=True):
    """A flag proposal with explicit provenance and submission lifecycle."""
    __table_args__ = (UniqueConstraint("project_id", "value_hash", name="uq_flagcandidate_project_value"),)

    id: str = Field(default_factory=lambda: new_id("flag"), primary_key=True)
    project_id: str = Field(index=True)
    value: str
    value_hash: str = Field(index=True)
    status: str = Field(default="PROPOSED", index=True)
    provenance_kind: str = "UNVERIFIED"
    artifact_refs: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    verification_artifact_ref: str | None = None
    source_attempt_id: str | None = Field(default=None, index=True)
    source_worker_id: str | None = Field(default=None, index=True)
    submission_count: int = 0
    rejection_reason: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class Hint(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("hint"), primary_key=True)
    project_id: str = Field(index=True)
    content: str
    source: str = "user"
    consumed: bool = False
    created_at: datetime = Field(default_factory=now_utc)


class ContextSnapshot(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("ctx"), primary_key=True)
    project_id: str = Field(index=True)
    intent_id: str = Field(index=True)
    worker_id: str | None = Field(default=None, index=True)
    sections_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    section_metrics_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    visible_tools_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    output_schema_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    total_chars: int = 0
    estimated_tokens: int = 0
    truncation_report_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)


class LLMTrace(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("llm"), primary_key=True)
    project_id: str = Field(index=True)
    worker_id: str = Field(index=True)
    intent_id: str = Field(index=True)
    attempt_id: str | None = Field(default=None, index=True)
    turn_id: str | None = None
    context_snapshot_id: str = Field(index=True)
    prompt_hash: str
    model: str = "unknown-runtime"
    input_chars: int = 0
    estimated_input_tokens: int = 0
    output_chars: int = 0
    estimated_output_tokens: int = 0
    provider_usage_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    decision_summary: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    structured_output: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)


class ToolTrace(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("tool"), primary_key=True)
    project_id: str = Field(index=True)
    worker_id: str | None = Field(default=None, index=True)
    intent_id: str | None = Field(default=None, index=True)
    attempt_id: str | None = Field(default=None, index=True)
    tool_name: str
    request_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    policy_decision: str = "allow"
    command: str | None = None
    cwd: str | None = None
    timeout_seconds: int | None = None
    exit_code: int | None = None
    summary: str | None = None
    artifact_refs: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    stderr_summary: str | None = None
    created_at: datetime = Field(default_factory=now_utc)


class WorkerEvent(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("event"), primary_key=True)
    project_id: str = Field(index=True)
    worker_id: str | None = Field(default=None, index=True)
    intent_id: str | None = Field(default=None, index=True)
    attempt_id: str | None = Field(default=None, index=True)
    event_type: str
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)
