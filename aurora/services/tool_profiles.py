from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from aurora.config import Settings


MANIFEST_PATH = Path(__file__).resolve().parents[1] / "tool_profiles.json"


@lru_cache
def load_tool_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def manifest_sha256() -> str:
    return hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()


def profile_for_challenge(challenge_type: str | None) -> str:
    manifest = load_tool_manifest()
    normalized = (challenge_type or "unknown").strip().lower()
    return str(manifest["routing"].get(normalized, manifest["routing"]["unknown"]))


def effective_challenge_type(challenge_type: str | None, evidence_text: str = "") -> str:
    normalized = (challenge_type or "unknown").strip().lower()
    normalized = {"webapp": "web", "web_app": "web", "web application": "web", "rev": "reverse", "re": "reverse", "forensic": "forensics"}.get(normalized, normalized)
    if normalized in load_tool_manifest()["routing"] and normalized != "unknown":
        return normalized
    text = re.sub(r"https?://\S+", " ", evidence_text.lower())
    heavy_signals = r"\b(?:pwn|rop|heap|elf|binary|firmware|reverse|apk|exe|rsa|aes|crypto|pcap)\b|逆向|溢出|固件|取证|加密"
    if re.search(heavy_signals, text):
        return "unknown"
    if re.search(r"\b(?:web|website|webapp|ssrf|sqli|xss|csrf|flask|django|php|sql injection)\b|网站|网页|注入|前端|后端", text):
        return "web"
    return "unknown"


def profile_capabilities(profile: str) -> dict[str, Any]:
    manifest = load_tool_manifest()
    profiles = manifest["profiles"]
    if profile not in profiles:
        raise ValueError(f"unknown worker tool profile: {profile}")
    selected = dict(profiles[profile])
    parent_name = selected.get("extends")
    if parent_name:
        parent = profile_capabilities(str(parent_name))
        merged = dict(parent)
        merged.update({key: value for key, value in selected.items() if key not in {"commands", "python_modules", "mcp_overrides"}})
        merged["commands"] = sorted(set(parent.get("commands", [])) | set(selected.get("commands", [])))
        merged["python_modules"] = sorted(set(parent.get("python_modules", [])) | set(selected.get("python_modules", [])))
        servers = {name: dict(value) for name, value in parent.get("mcp_servers", {}).items()}
        for name, override in selected.get("mcp_overrides", {}).items():
            servers.setdefault(name, {}).update(override)
        merged["mcp_servers"] = servers
        merged["heavy_only"] = []
        selected = merged
    selected.pop("extends", None)
    selected.pop("mcp_overrides", None)
    return selected


def image_for_profile(settings: Settings, profile: str) -> str:
    if profile == "core":
        return settings.worker_image_core
    if profile == "heavy":
        return settings.worker_image_heavy
    raise ValueError(f"unknown worker tool profile: {profile}")


def tool_environment(settings: Settings, challenge_type: str | None) -> dict[str, Any]:
    profile = profile_for_challenge(challenge_type)
    capabilities = profile_capabilities(profile)
    return {
        "challenge_type": challenge_type or "unknown",
        "profile": profile,
        "image": image_for_profile(settings, profile),
        "manifest_sha256": manifest_sha256(),
        **capabilities,
    }


def worker_preflight(settings: Settings, challenge_type: str | None) -> dict[str, Any]:
    from aurora.services.command_runner import KaliContainerRunner
    environment = tool_environment(settings, challenge_type)
    runner = KaliContainerRunner(image=str(environment["image"]), expected_profile=str(environment["profile"]))
    ready = runner.available()
    return {
        "ready": ready,
        "challenge_type": environment["challenge_type"],
        "profile": environment["profile"],
        "image": environment["image"],
        "manifest_sha256": environment["manifest_sha256"],
        "commands_count": len(environment.get("commands", [])),
        "python_modules_count": len(environment.get("python_modules", [])),
        "mcp_servers": sorted(environment.get("mcp_servers", {})),
        "error": runner.availability_error,
        "build_command": "./scripts/build-kali-codex.sh",
    }
