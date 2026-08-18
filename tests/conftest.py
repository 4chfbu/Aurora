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
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def pytest_sessionfinish(session, exitstatus):
    from aurora.db import engine

    engine.dispose()
    shutil.rmtree(_TEST_RUNTIME_DIR, ignore_errors=True)
