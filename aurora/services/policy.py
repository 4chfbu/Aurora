from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlmodel import Session, select

from aurora.models import AuthorizationScope, DiscoveredTarget
from aurora.services.browser_sessions import browser_session_registry


METADATA_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
}

MANAGEMENT_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
]


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str


class PolicyEngine:
    def check_tool_request(self, session: Session, *, project_id: str, tool_name: str, request: dict) -> PolicyDecision:
        scope = session.exec(select(AuthorizationScope).where(AuthorizationScope.project_id == project_id)).first()
        if scope is None:
            return PolicyDecision(False, "missing authorization scope")

        if tool_name == "sandbox.exec":
            return PolicyDecision(True, "sandbox policy applies command-level checks")

        if tool_name == "fofa.search":
            return self._check_fofa_scope(scope, request)

        host = self._extract_host(tool_name, request)
        if host is None:
            return PolicyDecision(True, "tool does not declare a network target")

        if self._is_denied_management_host(host) and scope.deny_metadata_and_management_networks:
            return PolicyDecision(False, f"target is denied metadata/management host: {host}")

        if tool_name == "browser.interact":
            browser_session = browser_session_registry.get_project_session(project_id)
            source_host = (urlparse(browser_session.source_url).hostname or "").lower().rstrip(".") if browser_session else ""
            lowered_host = host.lower().rstrip(".")
            if source_host and (lowered_host == source_host or lowered_host.endswith(f".{source_host}")):
                return PolicyDecision(True, "host belongs to the project's authenticated browser session")

        if scope.allowed_hosts and host in scope.allowed_hosts:
            return PolicyDecision(True, "host explicitly allowed")

        if scope.allowed_domains and any(host == domain or host.endswith(f".{domain}") for domain in scope.allowed_domains):
            return PolicyDecision(True, "domain explicitly allowed")

        discovered = session.exec(
            select(DiscoveredTarget).where(
                DiscoveredTarget.project_id == project_id,
                DiscoveredTarget.host == host,
                DiscoveredTarget.status == "ACTIVE",
            )
        ).first()
        if discovered is not None:
            return PolicyDecision(True, "host was discovered through an authorized browser interaction")

        if not scope.allowed_hosts and not scope.allowed_domains:
            return PolicyDecision(False, "authorization scope has no allowed hosts or domains")

        return PolicyDecision(False, f"target is outside authorization scope: {host}")

    def _check_fofa_scope(self, scope: AuthorizationScope, request: dict) -> PolicyDecision:
        query = str(request.get("query", "")).strip()
        match = re.fullmatch(r'\s*(?:host|domain|ip)\s*=\s*"([^"\\]+)"\s*', query, flags=re.IGNORECASE)
        if match is None:
            return PolicyDecision(False, "FOFA query must be exactly one host/domain/ip equality expression")
        target = match.group(1).lower()
        if self._is_denied_management_host(target) and scope.deny_metadata_and_management_networks:
            return PolicyDecision(False, f"target is denied metadata/management host: {target}")
        if target in {host.lower() for host in scope.allowed_hosts}:
            return PolicyDecision(True, "FOFA target explicitly allowed")
        if any(target == domain.lower() or target.endswith(f".{domain.lower()}") for domain in scope.allowed_domains):
            return PolicyDecision(True, "FOFA domain explicitly allowed")
        return PolicyDecision(False, f"FOFA target is outside authorization scope: {target}")

    def _extract_host(self, tool_name: str, request: dict) -> str | None:
        if tool_name in {"http.request", "web.enumerate", "browser.interact"}:
            parsed = urlparse(str(request.get("url", "")))
            return parsed.hostname
        if tool_name == "network.scan":
            target = str(request.get("target", "")).strip()
            return target or None
        return None

    def _is_denied_management_host(self, host: str) -> bool:
        lowered = host.lower()
        if lowered in METADATA_HOSTS:
            return True
        try:
            ip = ipaddress.ip_address(lowered)
        except ValueError:
            return False
        return any(ip in network for network in MANAGEMENT_NETWORKS)
