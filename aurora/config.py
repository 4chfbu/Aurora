from functools import lru_cache
from pathlib import Path
from pydantic import BaseModel, Field, model_validator
import os


def optional_env_int(name: str) -> int | None:
    value = os.getenv(name)
    return int(value) if value else None


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


class DebugConfig(BaseModel):
    capture_full_context: bool = True
    redact_secrets: bool = True
    max_context_snapshot_bytes: int = 512_000
    capture_tool_stdout: bool = False


class Settings(BaseModel):
    database_url: str = Field(default_factory=lambda: os.getenv("AURORA_DB_URL", "sqlite:///./aurora.db"))
    db_journal_mode: str = Field(default_factory=lambda: os.getenv("AURORA_DB_JOURNAL_MODE", "wal"))
    db_busy_timeout_ms: int = Field(default_factory=lambda: int(os.getenv("AURORA_DB_BUSY_TIMEOUT_MS", "30000")))
    artifact_dir: Path = Field(default_factory=lambda: Path(os.getenv("AURORA_ARTIFACT_DIR", "./artifacts")))
    api_lock_dir: Path | None = Field(default_factory=lambda: Path(value) if (value := os.getenv("AURORA_API_LOCK_DIR")) else None)
    api_host: str = Field(default_factory=lambda: os.getenv("AURORA_API_HOST", "0.0.0.0"))
    api_port: int = Field(default_factory=lambda: int(os.getenv("AURORA_API_PORT", "8000")))
    debug: DebugConfig = Field(default_factory=DebugConfig)
    default_worker_image: str = Field(default_factory=lambda: os.getenv("AURORA_WORKER_IMAGE", "aurora-kali-codex:latest"))
    worker_image_core: str = Field(default_factory=lambda: os.getenv("AURORA_WORKER_IMAGE_CORE") or os.getenv("AURORA_WORKER_IMAGE", "aurora-kali-codex:core"))
    worker_image_heavy: str = Field(default_factory=lambda: os.getenv("AURORA_WORKER_IMAGE_HEAVY", "aurora-kali-codex:heavy"))
    default_container_network: str = Field(default_factory=lambda: os.getenv("AURORA_CONTAINER_NETWORK", "aurora-runtime"))
    worker_runtime: str = Field(default_factory=lambda: os.getenv("AURORA_WORKER_RUNTIME", "codex"))
    tool_contract: str = Field(default_factory=lambda: os.getenv("AURORA_TOOL_CONTRACT", "kali_shell"))
    native_allow_blackboard_query: bool = Field(default_factory=lambda: os.getenv("AURORA_NATIVE_ALLOW_BLACKBOARD_QUERY", "true").lower() in {"1", "true", "yes"})
    codex_command_template: str = Field(default_factory=lambda: os.getenv("AURORA_CODEX_COMMAND_TEMPLATE", "/workspace/runtime/codex-via-cc-switch.sh {prompt_filename} {output_schema_filename} {last_message_filename}"))
    codex_proxy_base_url: str = Field(default_factory=lambda: os.getenv("AURORA_CODEX_PROXY_BASE_URL", "http://aurora-cc-switch:15723/v1"))
    codex_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_CODEX_TIMEOUT_SECONDS", "1800")))
    codex_model_context_window: int = Field(default_factory=lambda: int(os.getenv("AURORA_CODEX_MODEL_CONTEXT_WINDOW", "1000000")))
    codex_auto_compact_token_limit: int = Field(default_factory=lambda: int(os.getenv("AURORA_CODEX_AUTO_COMPACT_TOKEN_LIMIT", "800000")))
    codex_transcript_max_bytes: int = Field(default_factory=lambda: int(os.getenv("AURORA_CODEX_TRANSCRIPT_MAX_BYTES", str(2 * 1024 * 1024))))
    codex_workspace_dir: Path = Field(default_factory=lambda: Path(os.getenv("AURORA_CODEX_WORKSPACE_DIR", "./codex-workspaces")))
    worker_control_base_url: str = Field(default_factory=lambda: os.getenv("AURORA_WORKER_CONTROL_BASE_URL", "http://host.docker.internal:8000"))
    worker_container_cpus: float = Field(default_factory=lambda: float(os.getenv("AURORA_WORKER_CONTAINER_CPUS", "2")))
    worker_container_memory: str = Field(default_factory=lambda: os.getenv("AURORA_WORKER_CONTAINER_MEMORY", "4g"))
    llm_api_key: str | None = Field(default_factory=lambda: os.getenv("AURORA_LLM_API_KEY") or os.getenv("OPENAI_API_KEY"))
    llm_base_url: str = Field(default_factory=lambda: os.getenv("AURORA_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1")
    llm_model: str = Field(default_factory=lambda: os.getenv("AURORA_LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini")
    triage_model: str | None = Field(default_factory=lambda: os.getenv("AURORA_TRIAGE_MODEL"))
    planner_model: str | None = Field(default_factory=lambda: os.getenv("AURORA_PLANNER_MODEL"))
    solver_model: str | None = Field(default_factory=lambda: os.getenv("AURORA_SOLVER_MODEL"))
    reviewer_model: str | None = Field(default_factory=lambda: os.getenv("AURORA_REVIEWER_MODEL"))
    triage_model_context_window: int | None = Field(default_factory=lambda: optional_env_int("AURORA_TRIAGE_MODEL_CONTEXT_WINDOW"))
    triage_auto_compact_token_limit: int | None = Field(default_factory=lambda: optional_env_int("AURORA_TRIAGE_AUTO_COMPACT_TOKEN_LIMIT"))
    solver_model_context_window: int | None = Field(default_factory=lambda: optional_env_int("AURORA_SOLVER_MODEL_CONTEXT_WINDOW"))
    solver_auto_compact_token_limit: int | None = Field(default_factory=lambda: optional_env_int("AURORA_SOLVER_AUTO_COMPACT_TOKEN_LIMIT"))
    reviewer_model_context_window: int | None = Field(default_factory=lambda: optional_env_int("AURORA_REVIEWER_MODEL_CONTEXT_WINDOW"))
    reviewer_auto_compact_token_limit: int | None = Field(default_factory=lambda: optional_env_int("AURORA_REVIEWER_AUTO_COMPACT_TOKEN_LIMIT"))
    llm_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_LLM_TIMEOUT_SECONDS", "120")))
    default_soft_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_DEFAULT_SOFT_TIMEOUT_SECONDS", "300")))
    default_hard_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_DEFAULT_HARD_TIMEOUT_SECONDS", "1800")))
    default_max_tool_calls: int = Field(default_factory=lambda: int(os.getenv("AURORA_DEFAULT_MAX_TOOL_CALLS", "12")))
    default_max_repeat_failures: int = Field(default_factory=lambda: int(os.getenv("AURORA_DEFAULT_MAX_REPEAT_FAILURES", "2")))
    default_max_agent_actions: int = Field(default_factory=lambda: int(os.getenv("AURORA_DEFAULT_MAX_AGENT_ACTIONS", "20")))
    default_max_route_repeats: int = Field(default_factory=lambda: int(os.getenv("AURORA_DEFAULT_MAX_ROUTE_REPEATS", "2")))
    default_max_no_progress_actions: int = Field(default_factory=lambda: int(os.getenv("AURORA_DEFAULT_MAX_NO_PROGRESS_ACTIONS", "5")))
    default_finalize_grace_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_DEFAULT_FINALIZE_GRACE_SECONDS", "60")))
    resume_max_files: int = Field(default_factory=lambda: int(os.getenv("AURORA_RESUME_MAX_FILES", "100")))
    resume_max_bytes: int = Field(default_factory=lambda: int(os.getenv("AURORA_RESUME_MAX_BYTES", str(64 * 1024 * 1024))))
    codex_require_explicit_model_metadata: bool = Field(default_factory=lambda: os.getenv("AURORA_CODEX_REQUIRE_EXPLICIT_MODEL_METADATA", "true").lower() in {"1", "true", "yes"})
    browser_navigation_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_BROWSER_NAVIGATION_TIMEOUT_SECONDS", "15")))
    browser_retry_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_BROWSER_RETRY_TIMEOUT_SECONDS", "5")))
    browser_dom_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_BROWSER_DOM_TIMEOUT_SECONDS", "5")))
    browser_action_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_BROWSER_ACTION_TIMEOUT_SECONDS", "8")))
    worker_reap_interval_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_WORKER_REAP_INTERVAL_SECONDS", "5")))
    cataloger_llm_api_key: str | None = Field(default_factory=lambda: os.getenv("AURORA_CATALOGER_LLM_API_KEY") or os.getenv("AURORA_LLM_API_KEY") or os.getenv("OPENAI_API_KEY"))
    cataloger_llm_base_url: str = Field(default_factory=lambda: os.getenv("AURORA_CATALOGER_LLM_BASE_URL") or os.getenv("AURORA_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1")
    cataloger_llm_model: str = Field(default_factory=lambda: os.getenv("AURORA_CATALOGER_LLM_MODEL") or os.getenv("AURORA_LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini")
    cataloger_llm_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_CATALOGER_LLM_TIMEOUT_SECONDS") or os.getenv("AURORA_LLM_TIMEOUT_SECONDS") or "120"))
    cataloger_agent_enabled: bool = Field(default_factory=lambda: os.getenv("AURORA_CATALOGER_AGENT_ENABLED", "true").lower() in {"1", "true", "yes"})
    cataloger_max_agent_steps: int = Field(default_factory=lambda: int(os.getenv("AURORA_CATALOGER_MAX_AGENT_STEPS", "12")))
    cataloger_max_pages: int = Field(default_factory=lambda: int(os.getenv("AURORA_CATALOGER_MAX_PAGES", "20")))
    cataloger_max_candidates: int = Field(default_factory=lambda: int(os.getenv("AURORA_CATALOGER_MAX_CANDIDATES", "500")))
    cataloger_max_response_bytes: int = Field(default_factory=lambda: int(os.getenv("AURORA_CATALOGER_MAX_RESPONSE_BYTES", str(2 * 1024 * 1024))))
    cataloger_attachment_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_CATALOGER_ATTACHMENT_TIMEOUT_SECONDS", "20")))
    fofa_email: str | None = Field(default_factory=lambda: os.getenv("AURORA_FOFA_EMAIL"))
    fofa_key: str | None = Field(default_factory=lambda: os.getenv("AURORA_FOFA_KEY"))
    fofa_base_url: str = Field(default_factory=lambda: os.getenv("AURORA_FOFA_BASE_URL", "https://api.fofa.info/v1/search/all"))
    fofa_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_FOFA_TIMEOUT_SECONDS", "20")))
    subagents_enabled: bool = Field(default_factory=lambda: os.getenv("AURORA_SUBAGENTS_ENABLED", "false").lower() in {"1", "true", "yes"})
    subagents_max_concurrent: int = Field(default_factory=lambda: int(os.getenv("AURORA_SUBAGENTS_MAX_CONCURRENT", "2")))
    subagents_max_per_worker: int = Field(default_factory=lambda: int(os.getenv("AURORA_SUBAGENTS_MAX_PER_WORKER", "2")))
    max_challenge_group_concurrent: int = Field(default_factory=lambda: int(os.getenv("AURORA_MAX_CHALLENGE_GROUP_CONCURRENT", "2")))
    tsecbench_base_url: str = Field(default_factory=lambda: os.getenv("AURORA_TSECBENCH_BASE_URL") or os.getenv("BENCHMARK_BASE_URL") or "https://tsecbench.zc.tencent.com")
    tsecbench_token: str | None = Field(default_factory=lambda: os.getenv("AURORA_TSECBENCH_TOKEN") or os.getenv("BENCHMARK_TOKEN") or None)
    tsecbench_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_TSECBENCH_TIMEOUT_SECONDS", "20")))
    tsecbench_max_concurrent: int = Field(default_factory=lambda: int(os.getenv("AURORA_TSECBENCH_MAX_CONCURRENT", "3")))
    slab_match_base_url: str = Field(default_factory=lambda: os.getenv("AURORA_SLAB_MATCH_BASE_URL") or os.getenv("SLAB_MATCH_BASE_URL") or "https://example.com/slab-match/api/v1/agent")
    slab_match_access_key: str | None = Field(default_factory=lambda: os.getenv("AURORA_SLAB_MATCH_ACCESS_KEY") or os.getenv("SLAB_MATCH_ACCESS_KEY") or None)
    slab_match_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_SLAB_MATCH_TIMEOUT_SECONDS", "20")))
    slab_match_max_concurrent: int = Field(default_factory=lambda: int(os.getenv("AURORA_SLAB_MATCH_MAX_CONCURRENT", "1")))
    slab_match_notice_poll_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_SLAB_MATCH_NOTICE_POLL_SECONDS", "15")))
    openvpn_image: str = Field(default_factory=lambda: os.getenv("AURORA_OPENVPN_IMAGE", "aurora-openvpn:latest"))
    openvpn_container_name: str = Field(default_factory=lambda: os.getenv("AURORA_OPENVPN_CONTAINER_NAME", "aurora-openvpn"))
    openvpn_connect_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_OPENVPN_CONNECT_TIMEOUT_SECONDS", "75")))

    @model_validator(mode="after")
    def validate_codex_context_budget(self) -> "Settings":
        if not 1 <= self.api_port <= 65535:
            raise ValueError("AURORA_API_PORT must be between 1 and 65535")
        if self.codex_model_context_window <= 0:
            raise ValueError("AURORA_CODEX_MODEL_CONTEXT_WINDOW must be positive")
        if self.codex_auto_compact_token_limit <= 0 or self.codex_auto_compact_token_limit >= self.codex_model_context_window:
            raise ValueError("AURORA_CODEX_AUTO_COMPACT_TOKEN_LIMIT must be positive and smaller than the Codex context window")
        for role in ("triage", "solver", "reviewer"):
            window = getattr(self, f"{role}_model_context_window")
            compact = getattr(self, f"{role}_auto_compact_token_limit")
            if (window is None) != (compact is None):
                raise ValueError(f"{role} model context window and auto compact limit must be configured together")
            if window is not None and (window <= 0 or compact is None or compact <= 0 or compact >= window):
                raise ValueError(f"{role} auto compact limit must be positive and smaller than its context window")
        if self.codex_require_explicit_model_metadata and (
            "AURORA_CODEX_MODEL_CONTEXT_WINDOW" not in os.environ
            or "AURORA_CODEX_AUTO_COMPACT_TOKEN_LIMIT" not in os.environ
        ):
            raise ValueError("production Codex runtime requires explicit model context and compaction metadata")
        if self.default_max_no_progress_actions < 0:
            raise ValueError("AURORA_DEFAULT_MAX_NO_PROGRESS_ACTIONS cannot be negative")
        if self.resume_max_files <= 0 or self.resume_max_bytes <= 0:
            raise ValueError("resume manifest limits must be positive")
        if self.slab_match_notice_poll_seconds <= 0:
            raise ValueError("AURORA_SLAB_MATCH_NOTICE_POLL_SECONDS must be positive")
        return self

    @property
    def fofa_configured(self) -> bool:
        return bool(self.fofa_email and self.fofa_key)

    @property
    def cataloger_configured(self) -> bool:
        return bool(self.cataloger_llm_api_key and self.cataloger_llm_base_url and self.cataloger_llm_model)

    @property
    def tsecbench_configured(self) -> bool:
        return bool(self.tsecbench_base_url and self.tsecbench_token)

    @property
    def slab_match_configured(self) -> bool:
        return bool(self.slab_match_base_url and self.slab_match_access_key)

    def model_for_role(self, role: str) -> str:
        if role == "triage" and self.triage_model:
            return self.triage_model
        if role == "planner" and self.planner_model:
            return self.planner_model
        if role == "solver" and self.solver_model:
            return self.solver_model
        if role == "reviewer" and self.reviewer_model:
            return self.reviewer_model
        return self.llm_model

    def codex_metadata_for_role(self, role: str) -> tuple[int, int]:
        window = getattr(self, f"{role}_model_context_window", None)
        compact = getattr(self, f"{role}_auto_compact_token_limit", None)
        return (
            int(window or self.codex_model_context_window),
            int(compact or self.codex_auto_compact_token_limit),
        )


@lru_cache
def get_settings() -> Settings:
    load_env_file()
    return Settings()
