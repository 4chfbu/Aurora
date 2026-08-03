import pytest

from aurora.config import get_settings
from tests.runtime_stub import TestWorkerRuntime


@pytest.fixture(autouse=True)
def isolated_runtime_for_tests(monkeypatch):
    monkeypatch.setenv("AURORA_WORKER_RUNTIME", "codex")
    monkeypatch.setenv("AURORA_LLM_API_KEY", "")
    monkeypatch.setenv("AURORA_WORKER_IMAGE", "aurora-test-image-not-present")
    monkeypatch.setenv("AURORA_CONTAINER_NETWORK", "bridge")
    monkeypatch.setattr("aurora.services.demo.get_worker_runtime", lambda: TestWorkerRuntime())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
