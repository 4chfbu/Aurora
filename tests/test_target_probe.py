from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from aurora.config import get_settings
from aurora.services.command_runner import CommandResult, CommandRunner
from aurora.services.target_probe import TargetProbeService
from aurora.services.target_probe import normalize_target_url


class ProbeRunner(CommandRunner):
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code

    def run(self, *, command: str, cwd: Path, timeout: int | None) -> CommandResult:
        return CommandResult(command, command, str(cwd), "200", "", self.exit_code, "test")


class ReachableOpener:
    def open(self, request, timeout):
        return nullcontext(SimpleNamespace(status=200))


def test_target_probe_requires_both_host_and_worker_network(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AURORA_CODEX_WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("203.0.113.10", 80))])
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr("urllib.request.build_opener", lambda *args, **kwargs: ReachableOpener())

    success = TargetProbeService(command_runner=ProbeRunner(0), sleep=lambda _: None).probe("http://challenge.example", attempts=1)
    worker_failure = TargetProbeService(command_runner=ProbeRunner(7), sleep=lambda _: None).probe("http://challenge.example", attempts=1)

    assert success.success is True
    assert success.code == "REACHABLE"
    assert worker_failure.success is False
    assert worker_failure.code == "WORKER_NETWORK_FAILED"


def test_target_probe_reports_dns_failure(monkeypatch) -> None:
    def failed_dns(*args, **kwargs):
        raise OSError("name not known")

    monkeypatch.setattr("socket.getaddrinfo", failed_dns)
    result = TargetProbeService(command_runner=ProbeRunner(), sleep=lambda _: None).probe("http://missing.example", attempts=1)

    assert result.code == "DNS_FAILED"
    assert result.success is False


def test_manual_target_url_normalization_and_management_rejection() -> None:
    assert normalize_target_url("HTTPS://Example.COM:443/a%20b?x=1") == "https://example.com/a%20b?x=1"
    for url in (
        "http://user:pass@example.com/",
        "http://example.com/#fragment",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data",
        "ftp://example.com/",
    ):
        try:
            normalize_target_url(url)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe URL accepted: {url}")
