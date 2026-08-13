from functools import lru_cache
from pathlib import Path
from pydantic import BaseModel, Field
import os


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
    artifact_dir: Path = Field(default_factory=lambda: Path(os.getenv("AURORA_ARTIFACT_DIR", "./artifacts")))
    debug: DebugConfig = Field(default_factory=DebugConfig)
    default_worker_image: str = Field(default_factory=lambda: os.getenv("AURORA_WORKER_IMAGE", "aurora-kali-codex:latest"))
    worker_image_core: str = Field(default_factory=lambda: os.getenv("AURORA_WORKER_IMAGE_CORE") or os.getenv("AURORA_WORKER_IMAGE", "aurora-kali-codex:core"))
    worker_image_heavy: str = Field(default_factory=lambda: os.getenv("AURORA_WORKER_IMAGE_HEAVY", "aurora-kali-codex:heavy"))
    default_container_network: str = Field(default_factory=lambda: os.getenv("AURORA_CONTAINER_NETWORK", "aurora-runtime"))
    worker_runtime: str = Field(default_factory=lambda: os.getenv("AURORA_WORKER_RUNTIME", "codex"))
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
    planner_model: str | None = Field(default_factory=lambda: os.getenv("AURORA_PLANNER_MODEL"))
    solver_model: str | None = Field(default_factory=lambda: os.getenv("AURORA_SOLVER_MODEL"))
    llm_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_LLM_TIMEOUT_SECONDS", "120")))
    default_soft_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_DEFAULT_SOFT_TIMEOUT_SECONDS", "300")))
    default_hard_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_DEFAULT_HARD_TIMEOUT_SECONDS", "1800")))
    default_max_tool_calls: int = Field(default_factory=lambda: int(os.getenv("AURORA_DEFAULT_MAX_TOOL_CALLS", "12")))
    default_max_repeat_failures: int = Field(default_factory=lambda: int(os.getenv("AURORA_DEFAULT_MAX_REPEAT_FAILURES", "2")))
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
    fofa_email: str | None = Field(default_factory=lambda: os.getenv("AURORA_FOFA_EMAIL"))
    fofa_key: str | None = Field(default_factory=lambda: os.getenv("AURORA_FOFA_KEY"))
    fofa_base_url: str = Field(default_factory=lambda: os.getenv("AURORA_FOFA_BASE_URL", "https://api.fofa.info/v1/search/all"))
    fofa_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("AURORA_FOFA_TIMEOUT_SECONDS", "20")))
    subagents_enabled: bool = Field(default_factory=lambda: os.getenv("AURORA_SUBAGENTS_ENABLED", "false").lower() in {"1", "true", "yes"})
    subagents_max_concurrent: int = Field(default_factory=lambda: int(os.getenv("AURORA_SUBAGENTS_MAX_CONCURRENT", "2")))
    subagents_max_per_worker: int = Field(default_factory=lambda: int(os.getenv("AURORA_SUBAGENTS_MAX_PER_WORKER", "4")))

    @property
    def fofa_configured(self) -> bool:
        return bool(self.fofa_email and self.fofa_key)

    @property
    def cataloger_configured(self) -> bool:
        return bool(self.cataloger_llm_api_key and self.cataloger_llm_base_url and self.cataloger_llm_model)

    def model_for_role(self, role: str) -> str:
        if role == "planner" and self.planner_model:
            return self.planner_model
        if role == "solver" and self.solver_model:
            return self.solver_model
        return self.llm_model


@lru_cache
def get_settings() -> Settings:
    load_env_file()
    return Settings()
