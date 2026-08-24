import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from aurora.models import OpenVPNSetting
from aurora.config import get_settings
from aurora.api import create_app
from aurora.services.openvpn_gateway import (
    OpenVPNGatewayRegistry,
    OpenVPNLocked,
    OpenVPNRuntimeError,
    normalize_routes,
    validate_ovpn,
)


PROFILE = b"""client
dev tun
proto udp
remote vpn.example 1194
<ca>
certificate-data
</ca>
"""


class FakeGateway(OpenVPNGatewayRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.commands: list[list[str]] = []
        self.running = False

    def _run(self, args: list[str], *, timeout: int = 15) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        if args[:2] == ["network", "inspect"]:
            return subprocess.CompletedProcess(args, 0, '[{"Subnet":"172.29.0.0/16"}]\n', "")
        if args[:2] == ["ps", "-q"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["rm", "-f"]:
            self.running = False
        if args[:2] == ["run", "-d"]:
            self.running = True
            return subprocess.CompletedProcess(args, 0, "container-id\n", "")
        if args[:2] == ["inspect", "--format"]:
            if "Config.Labels" in args[2]:
                return subprocess.CompletedProcess(args, 0, "true\n" if self.running else "\n", "")
            return subprocess.CompletedProcess(args, 0, "true\n" if self.running else "false\n", "")
        if args[:3] == ["exec", "aurora-openvpn", "ip"]:
            output = "5: tun0: <POINTOPOINT,UP>\n" if "link" in args else "10.20.0.0 via 10.8.0.1 dev tun0\n"
            return subprocess.CompletedProcess(args, 0, output, "")
        return subprocess.CompletedProcess(args, 0, "", "")


def test_ovpn_validation_rejects_host_mutation_and_external_files() -> None:
    assert "remote vpn.example" in validate_ovpn(PROFILE)
    for line in (b"up /tmp/script", b"route 0.0.0.0 0.0.0.0", b"redirect-gateway def1", b"ca /tmp/ca.pem"):
        with pytest.raises(ValueError):
            validate_ovpn(PROFILE + b"\n" + line + b"\n")


def test_route_validation_normalizes_and_blocks_dangerous_ranges() -> None:
    assert normalize_routes(["10.20.1.9", "10.30.0.4/16", "10.20.1.9/32"]) == ["10.20.1.9/32", "10.30.0.0/16"]
    for route in ("0.0.0.0/0", "127.0.0.1", "169.254.169.254", "172.29.3.0/24", "::1"):
        with pytest.raises(ValueError):
            normalize_routes([route], docker_subnets=["172.29.0.0/16"])


def test_encrypted_config_is_locked_after_restart_and_contains_no_secrets() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    gateway = FakeGateway()
    with Session(engine) as session:
        saved = gateway.save(session, ovpn=PROFILE, vault_password="vault-password", routes=["10.20.0.0/16"], username="alice", password="vpn-secret")
        stored = session.get(OpenVPNSetting, "global")
        assert saved["state"] == "unlocked"
        assert stored is not None
        rendered = bytes(stored.encrypted_payload)
        assert b"vpn-secret" not in rendered and b"vpn.example" not in rendered

        restarted = FakeGateway()
        assert restarted.public_config(session)["locked"] is True
        with pytest.raises(OpenVPNLocked):
            restarted.unlock(session, "wrong-password")
        unlocked = restarted.unlock(session, "vault-password")
        assert unlocked["state"] == "unlocked"
        assert unlocked["credentials_configured"] is True


def test_saving_new_profile_deletes_and_replaces_old_encrypted_record() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    gateway = FakeGateway()
    with Session(engine) as session:
        gateway.save(session, ovpn=PROFILE, vault_password="first-password", routes=["10.20.0.0/16"], username=None, password=None)
        first = session.get(OpenVPNSetting, "global")
        assert first is not None
        first_ciphertext = bytes(first.encrypted_payload)

        replacement = PROFILE.replace(b"vpn.example", b"vpn-new.example")
        gateway.save(session, ovpn=replacement, vault_password="second-password", routes=["10.30.0.0/16"], username=None, password=None)
        current = session.get(OpenVPNSetting, "global")

        assert current is not None
        assert bytes(current.encrypted_payload) != first_ciphertext
        assert current.routes == ["10.30.0.0/16"]
        with pytest.raises(OpenVPNLocked):
            OpenVPNGatewayRegistry().unlock(session, "first-password")


def test_openvpn_timeout_explains_incomplete_handshake() -> None:
    detail = OpenVPNGatewayRegistry._connection_failure_detail(
        "VERIFY OK: depth=0, CN=vpn-server",
        30,
    )
    assert "verified the server certificate" in detail
    assert "did not complete within 30 seconds" in detail
    assert "VERIFY OK" in detail


def test_openvpn_timeout_without_server_reply_points_to_endpoint_or_udp() -> None:
    detail = OpenVPNGatewayRegistry._connection_failure_detail(
        "TLS key negotiation failed to occur within 60 seconds (check your network connectivity)",
        75,
    )
    assert "did not reply" in detail
    assert "UDP" in detail


def test_gateway_command_is_isolated_and_routes_only_selected_networks() -> None:
    gateway = FakeGateway()
    gateway._payload = {"ovpn": PROFILE.decode(), "username": "alice", "password": "vpn-secret"}
    try:
        gateway._start_container(["10.20.0.0/16"])
        command = next(item for item in gateway.commands if item[:2] == ["run", "-d"])
        rendered = " ".join(command)
        assert f"--network {get_settings().default_container_network}" in rendered
        assert "--cap-drop ALL --cap-add NET_ADMIN" in rendered
        assert "--device /dev/net/tun" in rendered
        assert "--add-host host.docker.internal:host-gateway" in rendered
        assert "--route-nopull" in rendered
        assert "--route 10.20.0.0 255.255.0.0" in rendered
        assert "--network host" not in rendered
        assert "vpn-secret" not in rendered
        assert gateway._runtime_dir is not None
        assert (gateway._runtime_dir.stat().st_mode & 0o777) == 0o700
        assert ((gateway._runtime_dir / "client.ovpn").stat().st_mode & 0o777) == 0o644
        assert ((gateway._runtime_dir / "auth.txt").stat().st_mode & 0o777) == 0o644
    finally:
        gateway._cleanup_runtime()


def test_worker_network_recovers_when_requested_gateway_container_disappears() -> None:
    gateway = FakeGateway()
    gateway._payload = {"ovpn": PROFILE.decode(), "username": "", "password": ""}
    gateway._routes = ["10.20.0.0/16"]
    gateway._desired_connected = True

    assert gateway.worker_network() == "container:aurora-openvpn"
    assert gateway.running is True
    assert len([command for command in gateway.commands if command[:2] == ["run", "-d"]]) == 1
    gateway._cleanup_runtime()


def test_worker_network_does_not_retry_a_known_connection_failure() -> None:
    gateway = FakeGateway()
    gateway._payload = {"ovpn": PROFILE.decode(), "username": "", "password": ""}
    gateway._routes = ["10.20.0.0/16"]
    gateway._desired_connected = True
    gateway._last_error = "OpenVPN authentication failed"

    with pytest.raises(OpenVPNRuntimeError, match="OpenVPN authentication failed"):
        gateway.worker_network()

    assert not any(command[:2] == ["run", "-d"] for command in gateway.commands)


def test_auth_profile_requires_web_credentials() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    gateway = FakeGateway()
    with Session(engine) as session, pytest.raises(ValueError, match="requires a VPN username"):
        gateway.save(session, ovpn=PROFILE + b"auth-user-pass\n", vault_password="vault-password", routes=["10.20.0.0/16"], username=None, password=None)


def test_openvpn_upload_api_uses_multipart_without_exposing_secrets(monkeypatch) -> None:
    from aurora.services.openvpn_gateway import openvpn_gateway_registry

    captured: dict[str, object] = {}
    public = {"configured": True, "locked": False, "connected": False, "desired_connected": False, "state": "unlocked", "routes": ["10.20.0.0/16"], "credentials_configured": True, "last_error": None, "updated_at": None}

    def save(session, **kwargs):
        captured.update(kwargs)
        return public

    monkeypatch.setattr(openvpn_gateway_registry, "initialize", lambda: None)
    monkeypatch.setattr(openvpn_gateway_registry, "save", save)
    with TestClient(create_app()) as client:
        response = client.put(
            "/api/settings/openvpn",
            files={"ovpn": ("client.ovpn", PROFILE, "text/plain")},
            data={"vault_password": "vault-password", "routes": "10.20.0.0/16", "username": "alice", "password": "vpn-secret"},
        )

    assert response.status_code == 200
    assert response.json() == public
    assert captured["ovpn"] == PROFILE
    assert captured["routes"] == ["10.20.0.0/16"]
    assert "vpn-secret" not in response.text and "vault-password" not in response.text
