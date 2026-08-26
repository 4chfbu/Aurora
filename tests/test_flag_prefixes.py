from fastapi.testclient import TestClient

import pytest

from aurora.api import create_app
from aurora.config import get_settings
from aurora.services.flag_prefix_config import (
    configure_flag_prefixes,
    configure_group_flag_prefixes,
    flag_prefixes_for_project,
    public_flag_prefix_config,
)


def test_public_flag_prefix_config_defaults(monkeypatch) -> None:
    monkeypatch.delenv("AURORA_FLAG_PREFIXES", raising=False)
    get_settings.cache_clear()
    config = public_flag_prefix_config()
    assert config["prefixes"] == ["flag"]
    assert config["case_insensitive"] is True
    assert config["submit_preserves_case"] is True
    assert config["default"] == ["flag"]
    assert config["source"] == "default"
    get_settings.cache_clear()


def test_public_flag_prefix_config_detects_environment(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_FLAG_PREFIXES", "flag, DASCTF")
    get_settings.cache_clear()
    config = public_flag_prefix_config()
    assert config["prefixes"] == ["flag", "dasctf"]
    assert config["source"] == "environment"
    get_settings.cache_clear()


def test_configure_flag_prefixes_updates_runtime(monkeypatch) -> None:
    monkeypatch.delenv("AURORA_FLAG_PREFIXES", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    original = list(settings.flag_prefixes or ["flag"])
    try:
        result = configure_flag_prefixes(["flag", "DASCTF", "flag"])
        assert result["prefixes"] == ["flag", "dasctf"]
        assert result["source"] == "runtime"
        assert settings.flag_prefixes == ["flag", "dasctf"]
    finally:
        settings.flag_prefixes = original


def test_configure_flag_prefixes_rejects_invalid(monkeypatch) -> None:
    monkeypatch.delenv("AURORA_FLAG_PREFIXES", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        configure_flag_prefixes([])
    with pytest.raises(ValueError):
        configure_flag_prefixes(["bad prefix"])
    with pytest.raises(ValueError):
        configure_flag_prefixes(["flag{brace}"])
    get_settings.cache_clear()


def test_flag_prefixes_endpoints_round_trip(monkeypatch) -> None:
    monkeypatch.delenv("AURORA_FLAG_PREFIXES", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    original = list(settings.flag_prefixes or ["flag"])
    client = TestClient(create_app())
    try:
        initial = client.get("/api/settings/flag-prefixes")
        assert initial.status_code == 200
        assert initial.json()["prefixes"] == ["flag"]

        saved = client.put("/api/settings/flag-prefixes", json={"prefixes": ["flag", "DASCTF"]})
        assert saved.status_code == 200
        assert saved.json()["prefixes"] == ["flag", "dasctf"]
        assert saved.json()["source"] == "runtime"
        assert settings.flag_prefixes == ["flag", "dasctf"]

        fetched = client.get("/api/settings/flag-prefixes")
        assert fetched.json()["prefixes"] == ["flag", "dasctf"]

        rejected = client.put("/api/settings/flag-prefixes", json={"prefixes": ["bad prefix"]})
        assert rejected.status_code == 400
    finally:
        settings.flag_prefixes = original
        get_settings.cache_clear()


def _group_session():
    from sqlmodel import Session, SQLModel, create_engine

    from aurora.models import ChallengeGroup, ChallengeGroupItem, Project

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    project = Project(name="p", goal="g")
    group = ChallengeGroup(name="g")
    session.add_all([project, group])
    session.commit()
    session.add(ChallengeGroupItem(group_id=group.id, project_id=project.id, position=1))
    session.commit()
    return session, project, group


def test_flag_prefixes_for_project_inherits_global(monkeypatch) -> None:
    monkeypatch.delenv("AURORA_FLAG_PREFIXES", raising=False)
    get_settings.cache_clear()
    session, project, _group = _group_session()
    try:
        assert flag_prefixes_for_project(session, project.id) == ("flag",)
        assert flag_prefixes_for_project(session, None) == ("flag",)
    finally:
        session.close()


def test_flag_prefixes_for_project_uses_group_override(monkeypatch) -> None:
    monkeypatch.delenv("AURORA_FLAG_PREFIXES", raising=False)
    get_settings.cache_clear()
    session, project, group = _group_session()
    try:
        config = configure_group_flag_prefixes(session, group.id, ["DASCTF", "flag"])
        assert config["overridden"] is True
        assert config["prefixes"] == ["dasctf", "flag"]
        assert flag_prefixes_for_project(session, project.id) == ("dasctf", "flag")

        cleared = configure_group_flag_prefixes(session, group.id, [])
        assert cleared["overridden"] is False
        assert cleared["prefixes"] == ["flag"]
        assert flag_prefixes_for_project(session, project.id) == ("flag",)
    finally:
        session.close()


def test_configure_group_flag_prefixes_rejects_invalid(monkeypatch) -> None:
    monkeypatch.delenv("AURORA_FLAG_PREFIXES", raising=False)
    get_settings.cache_clear()
    session, _project, group = _group_session()
    try:
        with pytest.raises(ValueError):
            configure_group_flag_prefixes(session, group.id, ["bad prefix"])
        with pytest.raises(ValueError):
            configure_group_flag_prefixes(session, "missing_group", ["flag"])
    finally:
        session.close()
