from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlmodel import Session, select

from aurora.models import Artifact, DiscoveredTarget, WorkerEvent
from aurora.services.artifact_store import ArtifactStore
from aurora.services.blackboard_repository import BlackboardRepository
from aurora.services.browser_sessions import browser_session_registry


URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
LABELED_TARGET_PATTERN = re.compile(
    r"(?im)(?:靶机(?:地址|链接|url)?|题目(?:地址|链接|url)?|target(?:\s*(?:url|address|host))?|challenge(?:\s*(?:url|address|host))?|instance(?:\s*(?:url|address|host))?)\s*(?:[:：=]|为)?\s*"
    r"(https?://[^\s\"'<>]+|(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?:/[^\s\"'<>]*)?|[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?::\d{1,5})?(?:/[^\s\"'<>]*)?)"
)
DENIED_HOSTS = {"169.254.169.254", "metadata.google.internal", "localhost"}


@dataclass
class BrowserInteractionResult:
    success: bool
    summary: str
    artifact_refs: list[str]
    target_urls: list[str]


class BrowserInteractionService:
    def execute(self, session: Session, *, project_id: str, request: dict, worker_id: str | None, intent_id: str | None, attempt_id: str | None) -> BrowserInteractionResult:
        browser_session = browser_session_registry.get_project_session(project_id)
        if browser_session is None:
            session.add(WorkerEvent(project_id=project_id, worker_id=worker_id, intent_id=intent_id, attempt_id=attempt_id, event_type="browser.session_required", payload_json={"reason": "no project browser session"}))
            session.commit()
            return BrowserInteractionResult(False, "browser session required; paste a Cookie for this project", [], [])

        url = str(request.get("url") or browser_session.source_url).strip()
        source_host = (urlparse(browser_session.source_url).hostname or "").lower().rstrip(".")
        target_host = (urlparse(url).hostname or "").lower().rstrip(".")
        if not target_host or not self._same_domain(source_host, target_host):
            return BrowserInteractionResult(False, "browser interaction must stay on the authenticated challenge domain", [], [])
        locator = request.get("locator") if isinstance(request.get("locator"), dict) else {}
        text = str(locator.get("text", "")).strip()
        selector = str(locator.get("selector", "")).strip()
        if selector and (len(selector) > 300 or "javascript:" in selector.lower()):
            return BrowserInteractionResult(False, "invalid browser selector", [], [])

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return BrowserInteractionResult(False, "Playwright Chromium is not installed", [], [])

        response_urls: list[str] = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context()
                context.add_cookies(self._parse_cookie(browser_session.cookie, source_host))
                page = context.new_page()
                page.on("response", lambda response: response_urls.append(response.url))
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                before_url = page.url
                before_body = page.locator("body").inner_text(timeout=10_000)[:80_000]
                candidates = self._labeled_target_urls(before_body, source_host)
                clicked = False
                auto_launch_label = ""
                if not candidates and not text and not selector:
                    launch_pattern = re.compile(r"(?:启动|开启|创建|获取|Start|Launch|Create|Get)\s*(?:靶机|环境|实例|Instance|Target|Environment)?", re.IGNORECASE)
                    auto_control = page.get_by_role("button", name=launch_pattern).first
                    if auto_control.count() < 1:
                        auto_control = page.get_by_role("link", name=launch_pattern).first
                    if auto_control.count() > 0:
                        text = "自动识别的启动靶机入口"
                        auto_launch_label = text
                    else:
                        artifact = ArtifactStore().write_text(
                            session,
                            project_id=project_id,
                            source_attempt_id=attempt_id,
                            artifact_type="browser-inspection",
                            summary="Browser inspected challenge page; no labeled target address or launch control found",
                            content=json.dumps({"page_url": url, "page_text": before_body[:12_000], "target_urls": []}, ensure_ascii=False, indent=2),
                        )
                        browser.close()
                        return BrowserInteractionResult(False, "no labeled target address or launch control found", [artifact.id], [])
                response_urls.clear()
                if not candidates:
                    control = auto_control if auto_launch_label else (page.get_by_text(text, exact=False).first if text else page.locator(selector).first)
                    if control.count() < 1:
                        browser.close()
                        return BrowserInteractionResult(False, "declared browser control was not found", [], [])
                    control.click(timeout=10_000)
                    clicked = True
                    page.wait_for_timeout(min(max(int(request.get("wait_seconds", 5)), 1), 20) * 1_000)
                    page.wait_for_load_state("domcontentloaded", timeout=10_000)
                final_url = page.url
                body = page.locator("body").inner_text(timeout=10_000)[:80_000]
                candidates = list(dict.fromkeys([*candidates, *self._labeled_target_urls(body, source_host)]))
                if clicked:
                    candidates = list(dict.fromkeys([*candidates, *self._target_urls([before_url, final_url, *response_urls, *URL_PATTERN.findall(body)], source_host)]))
                artifact = ArtifactStore().write_text(
                    session,
                    project_id=project_id,
                    source_attempt_id=attempt_id,
                    artifact_type="browser-interaction",
                    summary=f"Browser {'clicked ' + (auto_launch_label or text or selector) if clicked else 'inspected labeled fields'}; discovered {len(candidates)} target URL(s)",
                    content=json.dumps({"page_url": url, "before_url": before_url, "final_url": final_url, "locator": {"text": text, "selector": selector}, "clicked": clicked, "response_urls": response_urls[-100:], "target_urls": candidates, "page_text": body[:12_000]}, ensure_ascii=False, indent=2),
                )
                browser.close()
        except Exception as exc:
            return BrowserInteractionResult(False, f"browser interaction failed: {exc}", [], [])

        for target_url in candidates:
            host = (urlparse(target_url).hostname or "").lower().rstrip(".")
            existing = session.exec(select(DiscoveredTarget).where(DiscoveredTarget.project_id == project_id, DiscoveredTarget.host == host, DiscoveredTarget.status == "ACTIVE")).first()
            if existing is None:
                session.add(DiscoveredTarget(project_id=project_id, url=target_url, host=host, source_artifact_id=artifact.id))
                session.add(WorkerEvent(project_id=project_id, worker_id=worker_id, intent_id=intent_id, attempt_id=attempt_id, event_type="target.discovered", payload_json={"url": target_url, "host": host, "artifact_ref": artifact.id}))
                BlackboardRepository().upsert_fact(session, project_id=project_id, statement=f"Browser interaction exposed target: {target_url}", category="target", confidence=0.9, evidence_refs=[artifact.id], source_intent_id=intent_id, source_attempt_id=attempt_id)
                BlackboardRepository().upsert_intent(session, project_id=project_id, objective=f"Inspect provisioned target {target_url}", capability_tags=["http.request"], parent_intent_id=intent_id, priority=3.0, risk_level="low", budget={"tool_request": {"url": target_url, "timeout_seconds": 10}})
        session.commit()
        return BrowserInteractionResult(True, f"browser interaction completed; discovered {len(candidates)} target URL(s)", [artifact.id], candidates)

    @staticmethod
    def _same_domain(source_host: str, host: str) -> bool:
        return host == source_host or host.endswith("." + source_host)

    @staticmethod
    def _parse_cookie(raw: str, domain: str) -> list[dict[str, str]]:
        cookies = []
        for item in raw.removeprefix("Cookie:").split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name:
                cookies.append({"name": name, "value": value, "domain": domain, "path": "/"})
        if not cookies:
            raise ValueError("invalid Cookie")
        return cookies

    @staticmethod
    def _target_urls(values: list[str], source_host: str) -> list[str]:
        targets: list[str] = []
        for value in values:
            parsed = urlparse(value.rstrip(".,;)]}"))
            host = (parsed.hostname or "").lower().rstrip(".")
            if parsed.scheme not in {"http", "https"} or not host or host == source_host or host.endswith("." + source_host):
                continue
            if host in DENIED_HOSTS:
                continue
            try:
                address = ipaddress.ip_address(host)
                if address.is_loopback or address.is_link_local or address.is_reserved:
                    continue
            except ValueError:
                pass
            normalized = parsed.geturl()
            if normalized not in targets:
                targets.append(normalized)
        return targets[:10]

    @classmethod
    def _labeled_target_urls(cls, text: str, source_host: str) -> list[str]:
        values = []
        for match in LABELED_TARGET_PATTERN.finditer(text):
            value = match.group(1).rstrip(".,;)]}")
            if not value.startswith(("http://", "https://")):
                value = f"http://{value}"
            values.append(value)
        return cls._target_urls(values, source_host)
