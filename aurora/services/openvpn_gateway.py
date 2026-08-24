from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
from threading import RLock
import time
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlmodel import Session

from aurora.config import Settings, get_settings
from aurora.models import OpenVPNSetting, now_utc


MAX_OVPN_BYTES = 1024 * 1024
AAD = b"aurora-openvpn-v1"
FORBIDDEN_DIRECTIVES = {
    "up", "down", "route-up", "route-pre-down", "ipchange", "learn-address",
    "client-connect", "client-disconnect", "plugin", "management", "management-client",
    "script-security", "route", "route-ipv6", "redirect-gateway", "redirect-private",
    "pull-filter", "dhcp-option", "config", "cd", "chroot", "setcon", "tls-verify",
    "auth-user-pass-verify", "client-crresponse", "pkcs11-providers", "engine", "capath",
}


class OpenVPNError(RuntimeError):
    pass


class OpenVPNLocked(OpenVPNError):
    pass


class OpenVPNConflict(OpenVPNError):
    pass


class OpenVPNRuntimeError(OpenVPNError):
    pass


def _derive_key(password: str, salt: bytes) -> bytes:
    if len(password) < 10 or len(password) > 1024:
        raise ValueError("VPN vault password must be between 10 and 1024 characters")
    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(password.encode("utf-8"))


def validate_ovpn(content: bytes) -> str:
    if not content or len(content) > MAX_OVPN_BYTES:
        raise ValueError("OVPN file must be between 1 byte and 1 MiB")
    if b"\x00" in content:
        raise ValueError("OVPN file must be UTF-8 text")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("OVPN file must be UTF-8 text") from exc
    inline_tag: str | None = None
    has_remote = False
    has_client = False
    has_tun = False
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if inline_tag:
            if line.lower() == f"</{inline_tag}>":
                inline_tag = None
            continue
        if line.startswith("<") and line.endswith(">") and not line.startswith("</"):
            tag = line[1:-1].strip().lower()
            if tag not in {"ca", "cert", "key", "tls-auth", "tls-crypt", "tls-crypt-v2"}:
                raise ValueError(f"unsupported inline OVPN block <{tag}> on line {number}")
            inline_tag = tag
            continue
        parts = line.split()
        directive = parts[0].lstrip("-").lower()
        if directive in FORBIDDEN_DIRECTIVES:
            raise ValueError(f"unsafe OVPN directive '{directive}' on line {number}")
        if directive in {"ca", "cert", "key", "tls-auth", "tls-crypt", "tls-crypt-v2", "pkcs12", "askpass"}:
            raise ValueError(f"external OVPN file reference '{directive}' is not supported")
        if directive == "auth-user-pass" and len(parts) != 1:
            raise ValueError("auth-user-pass must not reference an external file")
        if directive == "dev":
            if len(parts) != 2 or parts[1].lower() != "tun":
                raise ValueError("the OVPN profile must use exactly 'dev tun'")
            has_tun = True
        has_remote = has_remote or directive == "remote"
        has_client = has_client or directive == "client"
    if inline_tag:
        raise ValueError(f"unterminated inline OVPN block <{inline_tag}>")
    if not has_remote or not has_client or not has_tun:
        raise ValueError("OVPN profile must contain client, dev tun, and remote directives")
    return text


def normalize_routes(values: list[str], *, docker_subnets: list[str] | None = None) -> list[str]:
    blocked = [ipaddress.ip_network(value, strict=False) for value in (docker_subnets or [])]
    results: list[str] = []
    for raw in values:
        value = raw.strip()
        if not value:
            continue
        try:
            network = ipaddress.ip_network(value if "/" in value else f"{value}/32", strict=False)
        except ValueError as exc:
            raise ValueError(f"invalid VPN IPv4 address or CIDR: {value}") from exc
        if network.version != 4:
            raise ValueError("only IPv4 VPN routes are supported")
        if network.prefixlen == 0 or network.is_loopback or network.is_link_local or network.is_multicast or network.is_unspecified:
            raise ValueError(f"unsafe VPN route: {value}")
        if network.overlaps(ipaddress.ip_network("169.254.169.254/32")):
            raise ValueError("cloud metadata addresses cannot be routed through VPN")
        if any(network.overlaps(entry) for entry in blocked):
            raise ValueError(f"VPN route overlaps the Aurora Docker network: {value}")
        rendered = str(network)
        if rendered not in results:
            results.append(rendered)
    if not results:
        raise ValueError("at least one VPN IPv4 address or CIDR is required")
    if len(results) > 256:
        raise ValueError("at most 256 VPN routes are allowed")
    return results


class OpenVPNGatewayRegistry:
    def __init__(self) -> None:
        self._lock = RLock()
        self._payload: dict[str, str] | None = None
        self._routes: list[str] = []
        self._desired_connected = False
        self._runtime_dir: Path | None = None
        self._last_error: str | None = None

    @staticmethod
    def _engine() -> str | None:
        return shutil.which("docker") or shutil.which("podman")

    def _run(self, args: list[str], *, timeout: int = 15) -> subprocess.CompletedProcess[str]:
        engine = self._engine()
        if not engine:
            raise OpenVPNRuntimeError("docker/podman is not available")
        return subprocess.run([engine, *args], text=True, capture_output=True, check=False, timeout=timeout)

    def _container_running(self) -> bool:
        settings = get_settings()
        try:
            result = self._run(["inspect", "--format", "{{.State.Running}}", settings.openvpn_container_name], timeout=5)
        except (OpenVPNRuntimeError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and result.stdout.strip().lower() == "true"

    def _active_workers(self) -> list[str]:
        result = self._run(["ps", "-q", "--filter", "label=aurora.worker_id"], timeout=10)
        if result.returncode != 0:
            raise OpenVPNRuntimeError((result.stderr or result.stdout or "failed to inspect active Workers").strip())
        return [line for line in result.stdout.splitlines() if line.strip()]

    def _assert_mutable(self) -> None:
        if self._active_workers():
            raise OpenVPNConflict("VPN settings cannot change while Solver Workers are running")

    def _docker_subnets(self) -> list[str]:
        network = get_settings().default_container_network
        result = self._run(["network", "inspect", network, "--format", "{{json .IPAM.Config}}"], timeout=10)
        if result.returncode != 0:
            return []
        try:
            return [str(item["Subnet"]) for item in json.loads(result.stdout or "[]") if item.get("Subnet")]
        except (json.JSONDecodeError, TypeError):
            return []

    def public_config(self, session: Session) -> dict[str, Any]:
        setting = session.get(OpenVPNSetting, "global")
        running = self._container_running() if setting else False
        healthy = running and self._healthy(setting.routes)
        state = "unconfigured" if not setting else "locked" if self._payload is None else "connected" if healthy else "error" if self._desired_connected else "unlocked"
        return {
            "configured": setting is not None,
            "locked": setting is not None and self._payload is None,
            "connected": healthy,
            "desired_connected": self._desired_connected,
            "state": state,
            "routes": list(setting.routes) if setting else [],
            "credentials_configured": bool(setting and setting.credentials_configured),
            "last_error": self._last_error,
            "updated_at": setting.updated_at.isoformat() if setting else None,
        }

    def save(self, session: Session, *, ovpn: bytes, vault_password: str, routes: list[str], username: str | None, password: str | None) -> dict[str, Any]:
        with self._lock:
            self._assert_mutable()
            if session.get(OpenVPNSetting, "global") is not None and self._payload is None:
                raise OpenVPNLocked("unlock the existing VPN configuration before replacing it")
            text = validate_ovpn(ovpn)
            normalized = normalize_routes(routes, docker_subnets=self._docker_subnets())
            username = (username or "").strip()
            password = password or ""
            if bool(username) != bool(password):
                raise ValueError("VPN username and password must be provided together")
            requires_credentials = any(
                line.strip().split()[:1] == ["auth-user-pass"]
                for line in text.splitlines()
                if line.strip() and not line.strip().startswith(("#", ";"))
            )
            if requires_credentials and not username:
                raise ValueError("this OVPN profile requires a VPN username and password")
            salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
            key = _derive_key(vault_password, salt)
            payload = {"ovpn": text, "username": username, "password": password}
            encrypted = AESGCM(key).encrypt(nonce, json.dumps(payload).encode("utf-8"), AAD)
            previous = session.get(OpenVPNSetting, "global")
            if previous is not None:
                # A replacement is a real delete-and-create operation rather
                # than an in-place mutation of the old encrypted profile.
                session.delete(previous)
                session.flush()
            setting = OpenVPNSetting(encrypted_payload=encrypted, salt=salt, nonce=nonce)
            setting.encrypted_payload, setting.salt, setting.nonce = encrypted, salt, nonce
            setting.routes = normalized
            setting.credentials_configured = bool(username)
            setting.updated_at = now_utc()
            session.add(setting)
            session.commit()
            self._payload = payload
            self._routes = normalized
            self._desired_connected = False
            self._last_error = None
            self._remove_container()
            self._cleanup_runtime()
            return self.public_config(session)

    def unlock(self, session: Session, vault_password: str) -> dict[str, Any]:
        with self._lock:
            setting = session.get(OpenVPNSetting, "global")
            if setting is None:
                raise OpenVPNConflict("VPN is not configured")
            try:
                key = _derive_key(vault_password, setting.salt)
                payload = json.loads(AESGCM(key).decrypt(setting.nonce, setting.encrypted_payload, AAD))
            except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise OpenVPNLocked("invalid VPN vault password") from exc
            self._payload = payload
            self._routes = list(setting.routes)
            self._last_error = None
            return self.public_config(session)

    def lock(self, session: Session) -> dict[str, Any]:
        with self._lock:
            self._assert_mutable()
            self._desired_connected = False
            self._remove_container()
            self._cleanup_runtime()
            self._payload = None
            self._routes = []
            return self.public_config(session)

    def connect(self, session: Session) -> dict[str, Any]:
        with self._lock:
            self._assert_mutable()
            setting = session.get(OpenVPNSetting, "global")
            if setting is None:
                raise OpenVPNConflict("VPN is not configured")
            if self._payload is None:
                raise OpenVPNLocked("VPN configuration is locked")
            self._desired_connected = True
            self._last_error = None
            self._remove_container()
            self._cleanup_runtime()
            try:
                self._start_container(setting.routes)
                self._wait_until_healthy(setting.routes)
                return self.public_config(session)
            except Exception as exc:
                self._last_error = str(exc)[:1000]
                self._remove_container()
                self._cleanup_runtime()
                if isinstance(exc, OpenVPNError):
                    raise
                raise OpenVPNRuntimeError(str(exc)) from exc

    def disconnect(self, session: Session) -> dict[str, Any]:
        with self._lock:
            self._assert_mutable()
            self._desired_connected = False
            self._remove_container()
            self._cleanup_runtime()
            self._last_error = None
            return self.public_config(session)

    def clear(self, session: Session) -> dict[str, Any]:
        with self._lock:
            self._assert_mutable()
            self._desired_connected = False
            self._remove_container()
            self._cleanup_runtime()
            setting = session.get(OpenVPNSetting, "global")
            if setting:
                session.delete(setting)
                session.commit()
            self._payload = None
            self._routes = []
            self._last_error = None
            return self.public_config(session)

    def initialize(self) -> None:
        with self._lock:
            self._desired_connected = False
            self._payload = None
            self._routes = []
            self._remove_container()
            self._cleanup_runtime()

    def worker_network(self) -> str | None:
        with self._lock:
            if not self._desired_connected:
                return None
            running = self._container_running()
            if not running and self._payload is not None and self._last_error is None:
                # The gateway container may be removed independently from the
                # Aurora process (for example by Docker cleanup or a daemon
                # restart). The decrypted profile is still available in this
                # process, so restore the requested connection before rejecting
                # every subsequent Worker preflight.
                try:
                    self._remove_container()
                    self._cleanup_runtime()
                    self._start_container(self._routes)
                    self._wait_until_healthy(self._routes)
                    running = True
                except Exception as exc:
                    self._last_error = str(exc)[:1000]
                    self._remove_container()
                    self._cleanup_runtime()
                    if not isinstance(exc, OpenVPNError):
                        exc = OpenVPNRuntimeError(str(exc))
                    raise exc
            if not running or not self._healthy(self._routes):
                detail = f": {self._last_error}" if self._last_error else ""
                raise OpenVPNRuntimeError(f"OpenVPN is enabled but the tunnel is not healthy{detail}")
            return f"container:{get_settings().openvpn_container_name}"

    def _wait_until_healthy(self, routes: list[str]) -> None:
        # OpenVPN 2.6 defaults to a 60-second TLS handshake window. Use the
        # same envelope for explicit connects and automatic container recovery.
        connect_timeout = max(60, get_settings().openvpn_connect_timeout_seconds)
        deadline = time.monotonic() + connect_timeout
        while time.monotonic() < deadline:
            if self._healthy(routes):
                return
            if not self._container_running():
                break
            time.sleep(0.5)
        logs = self._run(["logs", "--tail", "60", get_settings().openvpn_container_name], timeout=10)
        raw_logs = (logs.stderr or logs.stdout or "").strip()
        raise OpenVPNRuntimeError(self._connection_failure_detail(raw_logs, connect_timeout))

    def _start_container(self, routes: list[str]) -> None:
        assert self._payload is not None
        base = Path("/dev/shm") if os.access("/dev/shm", os.W_OK) else Path(tempfile.gettempdir())
        runtime = Path(tempfile.mkdtemp(prefix="aurora-vpn-", dir=base))
        runtime.chmod(0o700)
        config_path = runtime / "client.ovpn"
        config_path.write_text(self._payload["ovpn"], encoding="utf-8")
        # The container deliberately drops DAC_OVERRIDE with --cap-drop ALL.
        # Keep the parent directory private (0700), but make the individual
        # read-only bind mount readable by container root.
        config_path.chmod(0o644)
        self._runtime_dir = runtime
        settings = get_settings()
        args = [
            "run", "-d", "--name", settings.openvpn_container_name,
            "--label", "aurora.vpn=true", "--network", settings.default_container_network,
            "--add-host", "host.docker.internal:host-gateway",
            "--cap-drop", "ALL", "--cap-add", "NET_ADMIN", "--device", "/dev/net/tun",
            "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/run", "--tmpfs", "/tmp",
            "-v", f"{config_path}:/vpn/client.ovpn:ro",
        ]
        if self._payload.get("username"):
            auth_path = runtime / "auth.txt"
            auth_path.write_text(f"{self._payload['username']}\n{self._payload['password']}\n", encoding="utf-8")
            auth_path.chmod(0o644)
            args.extend(["-v", f"{auth_path}:/vpn/auth.txt:ro"])
        args.extend([settings.openvpn_image, "--config", "/vpn/client.ovpn", "--route-nopull", "--auth-nocache"])
        if self._payload.get("username"):
            args.extend(["--auth-user-pass", "/vpn/auth.txt"])
        for route in routes:
            network = ipaddress.ip_network(route)
            args.extend(["--route", str(network.network_address), str(network.netmask)])
        result = self._run(args, timeout=30)
        if result.returncode != 0:
            raise OpenVPNRuntimeError((result.stderr or result.stdout or "failed to start OpenVPN container").strip())

    def _healthy(self, routes: list[str] | None) -> bool:
        if not self._container_running():
            return False
        name = get_settings().openvpn_container_name
        tun = self._run(["exec", name, "ip", "link", "show", "dev", "tun0"], timeout=5)
        if tun.returncode != 0 or "UP" not in tun.stdout:
            return False
        for route in routes or []:
            network = ipaddress.ip_network(route)
            probe = self._run(["exec", name, "ip", "route", "get", str(network.network_address)], timeout=5)
            if probe.returncode != 0 or "tun" not in probe.stdout:
                return False
        return True

    @staticmethod
    def _connection_failure_detail(logs: str, timeout_seconds: int) -> str:
        tail = logs[-1600:]
        upper = logs.upper()
        if "AUTH_FAILED" in upper:
            summary = "OpenVPN authentication failed; check the VPN username, password, and client certificate"
        elif "TLS KEY NEGOTIATION FAILED" in upper and "VERIFY OK" not in upper:
            summary = "OpenVPN server did not reply to the UDP TLS handshake; check that the VPN instance is active and UDP reachability to the remote endpoint"
        elif "TLS ERROR" in upper or "TLS KEY NEGOTIATION FAILED" in upper:
            summary = "OpenVPN TLS negotiation failed; check UDP reachability and the client certificate/key"
        elif "VERIFY OK" in upper and "INITIALIZATION SEQUENCE COMPLETED" not in upper:
            summary = f"OpenVPN verified the server certificate but the tunnel handshake did not complete within {max(3, timeout_seconds)} seconds"
        elif "INITIALIZATION SEQUENCE COMPLETED" in upper:
            summary = "OpenVPN connected, but tun0 or one of the configured VPN routes did not become healthy"
        else:
            summary = f"OpenVPN tunnel did not become healthy within {max(3, timeout_seconds)} seconds"
        return f"{summary}\n\nRecent OpenVPN log:\n{tail}" if tail else summary

    def _remove_container(self) -> None:
        try:
            name = get_settings().openvpn_container_name
            owned = self._run(["inspect", "--format", "{{index .Config.Labels \"aurora.vpn\"}}", name], timeout=5)
            if owned.returncode == 0 and owned.stdout.strip().lower() == "true":
                self._run(["rm", "-f", name], timeout=15)
        except Exception:
            pass

    def _cleanup_runtime(self) -> None:
        if self._runtime_dir:
            shutil.rmtree(self._runtime_dir, ignore_errors=True)
            self._runtime_dir = None


openvpn_gateway_registry = OpenVPNGatewayRegistry()
