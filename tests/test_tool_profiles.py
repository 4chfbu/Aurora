import json

from aurora.config import Settings
from aurora.services.tool_profiles import image_for_profile, load_tool_manifest, profile_capabilities, profile_for_challenge, tool_environment


def test_challenge_types_route_to_expected_profiles() -> None:
    assert profile_for_challenge("web") == "core"
    for challenge_type in ("pwn", "reverse", "crypto", "forensics", "misc", "unknown", None):
        assert profile_for_challenge(challenge_type) == "heavy"


def test_heavy_profile_inherits_core_capabilities() -> None:
    core = profile_capabilities("core")
    heavy = profile_capabilities("heavy")
    assert set(core["commands"]).issubset(heavy["commands"])
    assert heavy["mcp_servers"]["aurora_reverse"]["backend"] == "rizin+ghidra"
    assert heavy["mcp_servers"]["aurora_debug"]["backend"] == "gdb-mi"
    assert heavy["heavy_only"] == []


def test_tool_environment_reports_selected_image(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_WORKER_IMAGE_CORE", "worker:core-test")
    monkeypatch.setenv("AURORA_WORKER_IMAGE_HEAVY", "worker:heavy-test")
    settings = Settings()
    assert image_for_profile(settings, "core") == "worker:core-test"
    environment = tool_environment(settings, "reverse")
    assert environment["image"] == "worker:heavy-test"
    assert environment["profile"] == "heavy"
    assert "ghidra" in environment["commands"]


def test_manifest_is_valid_json() -> None:
    manifest = load_tool_manifest()
    assert manifest["schema_version"] == 1
    json.dumps(manifest)
