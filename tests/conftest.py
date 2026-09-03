import os
from pathlib import Path
import shutil
import tempfile

import pytest


_TEST_RUNTIME_DIR = Path(tempfile.mkdtemp(prefix="aurora-pytest-"))
os.environ["AURORA_DB_URL"] = f"sqlite:///{_TEST_RUNTIME_DIR / 'aurora.db'}"
os.environ["AURORA_API_LOCK_DIR"] = str(_TEST_RUNTIME_DIR / "locks")
os.environ["AURORA_CODEX_MODEL_CONTEXT_WINDOW"] = "1000000"
os.environ["AURORA_CODEX_AUTO_COMPACT_TOKEN_LIMIT"] = "800000"
os.environ["AURORA_CODEX_PROXY_BASE_URL"] = "http://test-proxy.invalid/v1"

from aurora.config import get_settings
from aurora.services.tool_profiles import tool_environment
from tests.runtime_stub import TestWorkerRuntime


@pytest.fixture(autouse=True)
def isolated_runtime_for_tests(monkeypatch):
    from aurora.db import engine, init_db
    from sqlmodel import SQLModel

    SQLModel.metadata.drop_all(engine)
    init_db()
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    monkeypatch.setenv("AURORA_LLM_API_KEY", "")
    monkeypatch.setenv("AURORA_WORKER_IMAGE", "aurora-test-image-not-present")
    monkeypatch.setenv("AURORA_CONTAINER_NETWORK", "bridge")
    monkeypatch.setattr("aurora.services.demo.get_worker_runtime", lambda: TestWorkerRuntime())
    # API lifespan startup/shutdown must not discover or remove containers from
    # the developer's real Docker daemon while the isolated test database is in
    # use. OpenVPNGatewayRegistry behavior is covered with FakeGateway instead.
    monkeypatch.setattr("aurora.api.openvpn_gateway_registry.initialize", lambda: None)

    def _ready_test_preflight(settings, challenge_type=None):
        environment = tool_environment(settings, challenge_type)
        return {
            "ready": True,
            "challenge_type": environment["challenge_type"],
            "profile": environment["profile"],
            "image": environment["image"],
            "manifest_sha256": environment["manifest_sha256"],
            "commands_count": len(environment.get("commands", [])),
            "python_modules_count": len(environment.get("python_modules", [])),
            "mcp_servers": sorted(environment.get("mcp_servers", {})),
            "error": None,
            "build_command": "./scripts/build-kali-codex.sh",
        }

    monkeypatch.setattr("aurora.services.demo.worker_preflight", _ready_test_preflight)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def pytest_sessionfinish(session, exitstatus):
    from aurora.db import engine

    engine.dispose()
    shutil.rmtree(_TEST_RUNTIME_DIR, ignore_errors=True)
