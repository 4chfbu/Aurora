from __future__ import annotations

import shlex
import socket
import time
import urllib.error
import urllib.request
import ipaddress
from dataclasses import asdict, dataclass
from urllib.parse import quote, unquote, urlparse, urlunparse

from aurora.config import get_settings
from aurora.services.command_runner import AutoCommandRunner, CommandRunner
from aurora.services.network_proxy import network_proxy_registry


DENIED_HOSTNAMES = {"localhost", "localhost.localdomain", "metadata.google.internal"}


def normalize_target_url(raw_url: str) -> str:
    """Return a stable HTTP(S) URL or raise for unsafe management targets."""
    raw = raw_url.strip()
    if not raw or any(character.isspace() for character in raw):
        raise ValueError("target URL must not be empty or contain whitespace")
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("target URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("target URL must not contain embedded credentials")
    if parsed.fragment:
        raise ValueError("target URL must not contain a fragment")

    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError("target URL contains an invalid host or port") from exc
    _reject_management_host(host)
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("target URL port is out of range")

    default_port = 80 if parsed.scheme.lower() == "http" else 443
    rendered_host = f"[{host}]" if ":" in host else host
    netloc = rendered_host if port in {None, default_port} else f"{rendered_host}:{port}"
    path = quote(unquote(parsed.path or "/"), safe="/%:@!$&'()*+,;=-._~")
    return urlunparse((parsed.scheme.lower(), netloc, path, "", parsed.query, ""))


def _reject_management_host(host: str) -> None:
    if host in DENIED_HOSTNAMES or host.endswith(".localhost"):
        raise ValueError("target URL points to a denied metadata or management host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or any(not label or len(label) > 63 for label in host.split(".")):
            raise ValueError("target URL contains an invalid hostname")
        return
    if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved:
        raise ValueError("target URL points to a denied metadata or management address")


@dataclass(frozen=True)
class TargetProbeResult:
    success: bool
    code: str
    summary: str
    diagnostics: dict[str, object]

    def public_dict(self) -> dict[str, object]:
        return asdict(self)


class TargetProbeService:
    def __init__(self, command_runner: CommandRunner | None = None, sleep=time.sleep) -> None:
        settings = get_settings()
        self.command_runner = command_runner or AutoCommandRunner(
            prefer_kali=True,
            allow_local_fallback=False,
            image=settings.worker_image_core,
            expected_profile="core",
        )
        self.sleep = sleep

    def probe_transport(self, url: str, *, timeout: int = 5) -> TargetProbeResult:
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            port = parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)
            if parsed.scheme not in {"http", "https", "tcp"} or not host or port is None:
                raise ValueError("target must be an HTTP(S) or TCP URL with a port")
            _reject_management_host(host)
            seconds = max(1, min(timeout, 10))
            script = (
                "import socket; "
                f"connection = socket.create_connection(({host!r}, {port}), timeout={seconds}); "
                "connection.close()"
            )
            workspace = (get_settings().codex_workspace_dir / "_target-probe").resolve()
            workspace.mkdir(parents=True, exist_ok=True)
            result = self.command_runner.run(
                command=f"python3 -c {shlex.quote(script)}", cwd=workspace, timeout=seconds + 2,
            )
            return TargetProbeResult(
                result.exit_code == 0, "REACHABLE" if result.exit_code == 0 else "WORKER_NETWORK_FAILED",
                "Worker TCP probe", {"url": url, "exit_code": result.exit_code, "error": result.stderr[-500:]},
            )
        except (ValueError, OSError, RuntimeError) as exc:
            return TargetProbeResult(False, "TRANSPORT_PROBE_FAILED", str(exc)[:500], {"url": url})

    def probe(self, url: str, *, attempts: int = 3) -> TargetProbeResult:
        try:
            url = normalize_target_url(url)
        except ValueError as exc:
            return TargetProbeResult(False, "INVALID_TARGET_URL", str(exc), {"url": url})
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.scheme not in {"http", "https"} or not host:
            return TargetProbeResult(False, "INVALID_TARGET_URL", "靶机地址不是有效 HTTP(S) URL", {"url": url})

        last: TargetProbeResult | None = None
        for index in range(max(1, min(attempts, 5))):
            last = self._probe_once(url=url, host=host, port=port)
            if last.success:
                return last
            if index + 1 < attempts:
                self.sleep(min(2 ** index, 4))
        assert last is not None
        return last

    def _probe_once(self, *, url: str, host: str, port: int) -> TargetProbeResult:
        diagnostics: dict[str, object] = {"url": url, "host": host, "port": port}
        try:
            addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
            diagnostics["dns_addresses"] = addresses
            for value in addresses:
                _reject_management_host(value)
        except ValueError as exc:
            diagnostics["dns_error"] = str(exc)
            return TargetProbeResult(False, "DENIED_RESOLVED_ADDRESS", str(exc), diagnostics)
        except OSError as exc:
            diagnostics["dns_error"] = str(exc)
            return TargetProbeResult(False, "DNS_FAILED", f"靶机域名解析失败: {exc}", diagnostics)

        try:
            with socket.create_connection((host, port), timeout=5):
                diagnostics["host_tcp"] = "reachable"
        except OSError as exc:
            diagnostics["host_tcp"] = "failed"
            diagnostics["host_tcp_error"] = str(exc)

        opener = urllib.request.build_opener(network_proxy_registry.get().urllib_proxy_handler())
        request = urllib.request.Request(url, headers={"User-Agent": "Aurora-Target-Probe/1.0"})
        try:
            with opener.open(request, timeout=10) as response:
                diagnostics["host_http_status"] = response.status
                diagnostics["host_http"] = "reachable"
        except urllib.error.HTTPError as exc:
            diagnostics["host_http_status"] = exc.code
            diagnostics["host_http"] = "reachable"
        except Exception as exc:
            diagnostics["host_http"] = "failed"
            diagnostics["host_http_error"] = str(exc)
        command = (
            "curl --silent --show-error --output /dev/null --write-out '%{http_code}' "
            f"--connect-timeout 5 --max-time 10 {shlex.quote(url)}"
        )
        try:
            probe_workspace = (get_settings().codex_workspace_dir / "_target-probe").resolve()
            probe_workspace.mkdir(parents=True, exist_ok=True)
            worker = self.command_runner.run(command=command, cwd=probe_workspace, timeout=15)
            diagnostics["worker_http_exit_code"] = worker.exit_code
            diagnostics["worker_http_status"] = worker.stdout.strip()[-3:]
            if worker.stderr.strip():
                diagnostics["worker_http_error"] = worker.stderr.strip()[-500:]
        except Exception as exc:
            diagnostics["worker_http_exit_code"] = None
            diagnostics["worker_http_error"] = str(exc)

        host_reachable = diagnostics.get("host_http") == "reachable"
        worker_reachable = diagnostics.get("worker_http_exit_code") == 0
        if host_reachable and worker_reachable:
            return TargetProbeResult(True, "REACHABLE", "宿主机和 Worker 均可访问靶机", diagnostics)
        if not host_reachable and diagnostics.get("host_tcp") == "failed":
            code = "PORT_NOT_READY"
            summary = "靶机地址已生成，但端口尚未就绪"
        elif not worker_reachable:
            code = "WORKER_NETWORK_FAILED"
            summary = "宿主机可见靶机，但 Worker 容器无法访问；请检查代理、Docker 网络或 DNS"
        else:
            code = "HTTP_FAILED"
            summary = "靶机端口可达，但 HTTP 探测失败"
        return TargetProbeResult(False, code, summary, diagnostics)
