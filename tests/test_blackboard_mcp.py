import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
import urllib.error
import pytest


class _FastMCP:
    def __init__(self, *_args, **_kwargs):
        pass

    def tool(self):
        return lambda function: function

    def run(self, **_kwargs):
        return None


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _load_blackboard_server(monkeypatch):
    mcp_module = ModuleType("mcp")
    server_module = ModuleType("mcp.server")
    fastmcp_module = ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = _FastMCP
    common_module = ModuleType("common")
    common_module.audited = lambda _server, _tool, _request, operation: operation()
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)
    monkeypatch.setitem(sys.modules, "common", common_module)
    monkeypatch.setenv("AURORA_WORKER_CONTROL_BASE_URL", "http://control.test")
    monkeypatch.setenv("AURORA_WORKER_ID", "worker_test")
    monkeypatch.setenv("AURORA_WORKER_CONTROL_TOKEN", "token_test")
    monkeypatch.setenv("AURORA_WORKER_CONTROL_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("AURORA_WORKER_CONTROL_MAX_ATTEMPTS", "3")
    path = Path("container/kali-codex/mcp/blackboard_server.py")
    spec = importlib.util.spec_from_file_location("test_blackboard_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_blackboard_control_retries_with_same_idempotency_key(monkeypatch) -> None:
    module = _load_blackboard_server(monkeypatch)
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.data, timeout))
        if len(calls) < 3:
            raise urllib.error.URLError("temporarily unavailable")
        return _Response({"status": "saved"})

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    assert module._request("/checkpoint", {"summary": "saved"}) == {"status": "saved"}
    assert len(calls) == 3
    assert calls[0][1] == 1
    assert calls[0][0] == calls[1][0] == calls[2][0]
    assert json.loads(calls[0][0])["request_id"]


def test_blackboard_failure_preserves_evidence_and_marks_it_stale(monkeypatch, tmp_path) -> None:
    module = _load_blackboard_server(monkeypatch)
    monkeypatch.chdir(tmp_path)
    snapshot = {"version": 7, "facts": [{"statement": "RCE reproduced", "evidence_refs": ["artifact_rce"]}], "stale": False}
    monkeypatch.setattr(module, "_request", lambda _path: snapshot)
    assert module.query() == snapshot

    def unavailable(_path):
        raise RuntimeError("control timeout")

    monkeypatch.setattr(module, "_request", unavailable)
    cached = module.query()
    assert cached["version"] == 7
    assert cached["facts"] == snapshot["facts"]
    assert cached["stale"] is True
    assert cached["sync_error"] == "control timeout"
    (tmp_path / "runtime" / "blackboard.json").unlink()
    with pytest.raises(RuntimeError, match="peer state is unknown"):
        module.query()
