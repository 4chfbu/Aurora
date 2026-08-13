from sqlmodel import Session, SQLModel, create_engine
import pytest

from aurora.models import NetworkProxySetting
from aurora.services.command_runner import KaliContainerRunner
from aurora.services.network_proxy import (
    load_network_proxy,
    network_proxy_registry,
    save_network_proxy,
    validate_proxy_config,
)


@pytest.fixture(autouse=True)
def reset_proxy_registry():
    network_proxy_registry.set(mode="system", proxy_url=None, no_proxy=None)
    yield
    network_proxy_registry.set(mode="system", proxy_url=None, no_proxy=None)


def test_custom_proxy_validation_and_runtime_shapes() -> None:
    config = validate_proxy_config(
        mode="custom",
        proxy_url="http://proxy.example:8080",
        no_proxy="challenge.local,.internal",
    )

    assert config.environment()["HTTPS_PROXY"] == "http://proxy.example:8080"
    assert config.playwright_proxy() == {
        "server": "http://proxy.example:8080",
        "bypass": "127.0.0.1,localhost,aurora-cc-switch,host.docker.internal,challenge.local,.internal",
    }
    assert "aurora-cc-switch" in config.no_proxy


@pytest.mark.parametrize("url", ["socks5://proxy.example:1080", "proxy.example:8080", "file:///tmp/proxy"])
def test_custom_proxy_rejects_unsupported_urls(url: str) -> None:
    with pytest.raises(ValueError, match=r"HTTP\(S\)"):
        validate_proxy_config(mode="custom", proxy_url=url, no_proxy=None)


def test_proxy_setting_persists_and_loads() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        saved = save_network_proxy(session, mode="custom", proxy_url="https://proxy.example:8443", no_proxy="ctf.local")
        network_proxy_registry.set(mode="direct", proxy_url=None, no_proxy=None)
        loaded = load_network_proxy(session)
        stored = session.get(NetworkProxySetting, "global")

    assert saved == loaded
    assert stored is not None and stored.proxy_url == "https://proxy.example:8443"


def test_worker_container_uses_selected_proxy_and_can_force_direct() -> None:
    network_proxy_registry.set(mode="custom", proxy_url="http://proxy.example:8080", no_proxy="ctf.local")
    custom_args = KaliContainerRunner()._env_args()
    assert "HTTP_PROXY=http://proxy.example:8080" in custom_args
    assert "NO_PROXY=127.0.0.1,localhost,aurora-cc-switch,host.docker.internal,ctf.local" in custom_args

    network_proxy_registry.set(mode="direct", proxy_url=None, no_proxy=None)
    direct_args = KaliContainerRunner()._env_args()
    assert "HTTP_PROXY=" in direct_args
    assert "HTTPS_PROXY=" in direct_args


@pytest.mark.parametrize(
    ("proxy", "expected"),
    [
        ("http://127.0.0.1:10808", ""),
        ("localhost:10808", ""),
        ("http://proxy.example:8080", "http://proxy.example:8080"),
    ],
)
def test_worker_container_disables_only_unreachable_host_loopback_proxy(monkeypatch, proxy: str, expected: str) -> None:
    monkeypatch.setenv("HTTP_PROXY", proxy)
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    network_proxy_registry.set(mode="system", proxy_url=None, no_proxy=None)

    args = KaliContainerRunner()._env_args()

    assert f"HTTP_PROXY={expected}" in args
    assert f"HTTPS_PROXY={expected}" in args
