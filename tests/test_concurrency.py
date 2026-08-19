from fastapi.testclient import TestClient

from aurora.api import create_app
from aurora.config import get_settings
from aurora.services.concurrency import configure_concurrency, public_concurrency_config


def test_public_concurrency_config_defaults(monkeypatch) -> None:
    monkeypatch.delenv("AURORA_MAX_CHALLENGE_GROUP_CONCURRENT", raising=False)
    get_settings.cache_clear()
    config = public_concurrency_config()
    assert config["max_agents"] == 2
    assert config["min"] == 1
    assert config["max"] == 8
    assert config["source"] == "default"


def test_public_concurrency_config_detects_environment(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_MAX_CHALLENGE_GROUP_CONCURRENT", "5")
    get_settings.cache_clear()
    config = public_concurrency_config()
    assert config["max_agents"] == 5
    assert config["source"] == "environment"
    assert config["env_value"] == 5
    get_settings.cache_clear()


def test_configure_concurrency_updates_runtime(monkeypatch) -> None:
    monkeypatch.delenv("AURORA_MAX_CHALLENGE_GROUP_CONCURRENT", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    original = settings.max_challenge_group_concurrent
    try:
        result = configure_concurrency(max_agents=4)
        assert result["max_agents"] == 4
        assert result["source"] == "runtime"
        assert settings.max_challenge_group_concurrent == 4
    finally:
        settings.max_challenge_group_concurrent = original


def test_configure_concurrency_rejects_out_of_range(monkeypatch) -> None:
    monkeypatch.delenv("AURORA_MAX_CHALLENGE_GROUP_CONCURRENT", raising=False)
    get_settings.cache_clear()
    import pytest

    with pytest.raises(ValueError):
        configure_concurrency(max_agents=0)
    with pytest.raises(ValueError):
        configure_concurrency(max_agents=9)


def test_concurrency_endpoints_round_trip(monkeypatch) -> None:
    monkeypatch.delenv("AURORA_MAX_CHALLENGE_GROUP_CONCURRENT", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    original = settings.max_challenge_group_concurrent
    client = TestClient(create_app())
    try:
        initial = client.get("/api/settings/concurrency")
        assert initial.status_code == 200
        assert initial.json()["max_agents"] == 2

        saved = client.put("/api/settings/concurrency", json={"max_agents": 6})
        assert saved.status_code == 200
        assert saved.json()["max_agents"] == 6
        assert saved.json()["source"] == "runtime"
        assert settings.max_challenge_group_concurrent == 6

        fetched = client.get("/api/settings/concurrency")
        assert fetched.json()["max_agents"] == 6

        rejected = client.put("/api/settings/concurrency", json={"max_agents": 99})
        assert rejected.status_code == 422
    finally:
        settings.max_challenge_group_concurrent = original
        get_settings.cache_clear()
