from __future__ import annotations

import ipaddress
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlmodel import Session, select

from aurora.models import WorkerEvent
from aurora.config import get_settings
from aurora.services.artifact_store import ArtifactStore
from aurora.services.browser_sessions import browser_session_registry
from aurora.services.network_proxy import network_proxy_registry


URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
LABELED_TARGET_PATTERN = re.compile(
    r"(?im)(?:靶机(?:地址|链接|url)?|题目(?:地址|链接|url)?|target(?:\s*(?:url|address|host))?|challenge(?:\s*(?:url|address|host))?|instance(?:\s*(?:url|address|host))?)\s*(?:[:：=]|为)?\s*"
    r"(https?://[^\s\"'<>]+|(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?:/[^\s\"'<>]*)?|[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?::\d{1,5})?(?:/[^\s\"'<>]*)?)"
)
DENIED_HOSTS = {"169.254.169.254", "metadata.google.internal", "localhost"}
DENIED_TARGET_DOMAINS = {
    "baidu.com",
    "bdstatic.com",
    "csdn.net",
    "csdnimg.cn",
    "google-analytics.com",
    "googletagmanager.com",
}
STATIC_TARGET_SUFFIXES = {
    ".css", ".gif", ".ico", ".jpeg", ".jpg", ".js", ".map", ".png", ".svg", ".webp", ".woff", ".woff2",
}


@dataclass
class BrowserInteractionResult:
    success: bool
    summary: str
    artifact_refs: list[str]
    target_urls: list[str]
    target_candidates: list[dict[str, object]] | None = None
    requires_confirmation: bool = False


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

        responses: list[dict[str, object]] = []
        browser = None
        settings = get_settings()
        navigation_timeout = max(1, min(int(request.get("navigation_timeout_seconds", settings.browser_navigation_timeout_seconds)), 60)) * 1_000
        retry_timeout = max(1, min(settings.browser_retry_timeout_seconds, 15)) * 1_000
        dom_timeout = max(1, min(settings.browser_dom_timeout_seconds, 15)) * 1_000
        action_timeout = max(1, min(settings.browser_action_timeout_seconds, 20)) * 1_000
        total_deadline = time.monotonic() + max(5, min(int(request.get("total_timeout_seconds", 35)), 90))
        try:
            with sync_playwright() as playwright:
                proxy = network_proxy_registry.get().playwright_proxy()
                browser = playwright.chromium.launch(headless=True, **({"proxy": proxy} if proxy else {}))
                context = browser.new_context()
                context.add_cookies(self._parse_cookie(browser_session.cookie, source_host))
                page = context.new_page()
                page.on("dialog", lambda dialog: dialog.accept())
                page.on("response", lambda response: self._capture_response(response, responses))
                navigation_error = None
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=min(navigation_timeout, max(1, int((total_deadline - time.monotonic()) * 1_000))))
                except Exception as exc:
                    navigation_error = exc
                    try:
                        page.goto(url, wait_until="commit", timeout=min(retry_timeout, max(1, int((total_deadline - time.monotonic()) * 1_000))))
                        navigation_error = None
                    except Exception as retry_exc:
                        navigation_error = retry_exc
                before_url = page.url
                try:
                    before_body = page.locator("body").inner_text(timeout=dom_timeout)[:80_000]
                except Exception:
                    before_body = ""
                if navigation_error is not None and not before_body and not responses:
                    browser.close()
                    return self._record_execution_error(session, project_id=project_id, attempt_id=attempt_id, url=url, response_urls=[], page_text=before_body, reason=f"browser navigation timeout: {navigation_error}")
                before_candidates = self._page_target_urls(page, source_host)
                candidates = list(before_candidates)
                clicked = False
                auto_launch_label = ""
                requires_confirmation = self._requires_confirmation(before_body)
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
                            origin_kind="target_observation",
                            summary="Browser inspected challenge page; no labeled target address or launch control found",
                            content=json.dumps({"page_url": url, "page_text": before_body[:12_000], "target_urls": []}, ensure_ascii=False, indent=2),
                        )
                        browser.close()
                        return BrowserInteractionResult(False, "no labeled target address or launch control found", [artifact.id], [])
                responses.clear()
                if not candidates:
                    if requires_confirmation and not bool(request.get("allow_paid_launch")):
                        artifact = ArtifactStore().write_text(
                            session,
                            project_id=project_id,
                            source_attempt_id=attempt_id,
                            artifact_type="browser-inspection",
                            origin_kind="target_observation",
                            summary="Browser found a paid launch confirmation and paused before accepting it",
                            content=json.dumps({"page_url": url, "requires_confirmation": True, "page_text": before_body[:12_000]}, ensure_ascii=False, indent=2),
                        )
                        browser.close()
                        return BrowserInteractionResult(True, "启动操作可能扣除积分或余额，需要用户确认", [artifact.id], [], [], True)
                    control = auto_control if auto_launch_label else (page.get_by_text(text, exact=False).first if text else page.locator(selector).first)
                    if control.count() < 1:
                        browser.close()
                        return BrowserInteractionResult(False, "declared browser control was not found", [], [])
                    control.click(timeout=min(action_timeout, max(1, int((total_deadline - time.monotonic()) * 1_000))))
                    clicked = True
                    self._accept_web_confirmation(page, action_timeout)
                    self._wait_for_provisioning(page, source_host, total_deadline, int(request.get("wait_seconds", 5)))
                final_url = page.url
                try:
                    body = page.locator("body").inner_text(timeout=dom_timeout)[:80_000]
                except Exception:
                    body = before_body
                after_candidates = self._page_target_urls(page, source_host)
                structured_candidates = self._structured_response_urls(responses, source_host)
                navigation_candidates = self._target_urls([before_url, final_url], source_host) if clicked else []
                candidates = list(dict.fromkeys([*candidates, *after_candidates, *structured_candidates, *navigation_candidates]))
                scored_candidates = []
                for candidate in candidates:
                    if candidate in structured_candidates:
                        source, score = "structured_response", 98
                    elif candidate in after_candidates and candidate not in before_candidates:
                        source, score = "new_target_control", 95
                    elif candidate in before_candidates:
                        source, score = "existing_target_control", 85
                    else:
                        source, score = "navigation", 75
                    scored_candidates.append({"url": candidate, "source": source, "score": score})
                artifact = ArtifactStore().write_text(
                    session,
                    project_id=project_id,
                    source_attempt_id=attempt_id,
                    artifact_type="browser-interaction",
                    origin_kind="target_observation",
                    summary=f"Browser {'clicked ' + (auto_launch_label or text or selector) if clicked else 'inspected labeled fields'}; discovered {len(candidates)} target URL(s)",
                    content=json.dumps({"page_url": url, "before_url": before_url, "final_url": final_url, "locator": {"text": text, "selector": selector}, "clicked": clicked, "responses": responses[-100:], "target_candidates": scored_candidates, "page_text": body[:12_000]}, ensure_ascii=False, indent=2),
                )
                for candidate in scored_candidates:
                    candidate["artifact_ref"] = artifact.id
                browser.close()
        except Exception as exc:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            return self._record_execution_error(session, project_id=project_id, attempt_id=attempt_id, url=url, response_urls=[str(item.get("url") or "") for item in responses], page_text="", reason=f"browser interaction failed: {exc}")

        for candidate in scored_candidates:
            session.add(WorkerEvent(project_id=project_id, worker_id=worker_id, intent_id=intent_id, attempt_id=attempt_id, event_type="target.discovered", payload_json={**candidate, "artifact_ref": artifact.id, "status": "CANDIDATE"}))
        session.commit()
        return BrowserInteractionResult(True, f"browser interaction completed; discovered {len(candidates)} target URL(s)", [artifact.id], candidates, scored_candidates, requires_confirmation)

    @classmethod
    def _page_target_urls(cls, page, source_host: str) -> list[str]:
        try:
            values = page.locator("input, textarea, a, button, code, pre, [data-target-url], [data-instance-url]").evaluate_all(
                """elements => elements.filter(element => {
                    const excluded = element.closest('[class*=comment i], [id*=comment i], [class*=writeup i], [id*=writeup i]');
                    const text = `${element.innerText || ''} ${element.value || ''} ${element.href || ''}`;
                    return !excluded && /(target|instance|challenge|靶机|题目).{0,24}(https?:\\/\\/|(?:\\d{1,3}\\.){3}\\d{1,3})/i.test(text);
                }).map(element => `${element.innerText || ''} ${element.value || ''} ${element.href || ''}`)"""
            )
        except Exception:
            return []
        return cls._labeled_target_urls("\n".join(str(value) for value in values), source_host)

    @staticmethod
    def _capture_response(response, responses: list[dict[str, object]]) -> None:
        resource_type = str(response.request.resource_type or "")
        if resource_type not in {"document", "xhr", "fetch"}:
            return
        item: dict[str, object] = {"url": response.url, "resource_type": resource_type, "status": response.status}
        content_type = str(response.headers.get("content-type") or "").lower()
        if "json" in content_type:
            try:
                item["json"] = response.json()
            except Exception:
                pass
        responses.append(item)

    @classmethod
    def _structured_response_urls(cls, responses: list[dict[str, object]], source_host: str) -> list[str]:
        values: list[str] = []

        def visit(value: object, key: str = "") -> None:
            if isinstance(value, dict):
                for child_key, child in value.items():
                    visit(child, str(child_key))
            elif isinstance(value, list):
                for child in value:
                    visit(child, key)
            elif isinstance(value, str) and re.search(r"(?:url|target|instance|host|address|靶机)", key, re.IGNORECASE):
                values.extend(URL_PATTERN.findall(value))

        for response in responses:
            if "json" in response:
                visit(response["json"])
        return cls._target_urls(values, source_host)

    @staticmethod
    def _accept_web_confirmation(page, timeout_ms: int) -> None:
        pattern = re.compile(r"^(?:确认|确定|继续|启动|创建|支付|Confirm|OK|Continue|Launch|Create|Pay)$", re.IGNORECASE)
        for selector in (".swal2-confirm", ".bootbox-accept", ".modal.show .btn-primary"):
            control = page.locator(selector).first
            if control.count() > 0 and control.is_visible():
                control.click(timeout=timeout_ms)
                return
        control = page.get_by_role("button", name=pattern).first
        if control.count() > 0 and control.is_visible():
            control.click(timeout=timeout_ms)

    @classmethod
    def _wait_for_provisioning(cls, page, source_host: str, deadline: float, minimum_wait_seconds: int) -> None:
        end = min(deadline, time.monotonic() + max(5, min(minimum_wait_seconds + 15, 30)))
        while time.monotonic() < end:
            page.wait_for_timeout(min(1000, max(1, int((end - time.monotonic()) * 1000))))
            try:
                page.wait_for_load_state("domcontentloaded", timeout=1000)
            except Exception:
                pass
            if cls._page_target_urls(page, source_host):
                return

    @staticmethod
    def _requires_confirmation(text: str) -> bool:
        return bool(re.search(r"(?:付费|支付|扣除|积分|金币|余额|cost|pay|credit|coin)", text, re.IGNORECASE))

    @staticmethod
    def _record_execution_error(session: Session, *, project_id: str, attempt_id: str | None, url: str, response_urls: list[str], page_text: str, reason: str) -> BrowserInteractionResult:
        artifact = ArtifactStore().write_text(
            session, project_id=project_id, source_attempt_id=attempt_id,
            artifact_type="browser-inspection", origin_kind="target_observation",
            summary="Browser execution error; partial observation retained",
            content=json.dumps({"page_url": url, "page_text": page_text[:12_000], "response_urls": response_urls[-100:], "error": reason}, ensure_ascii=False, indent=2),
        )
        return BrowserInteractionResult(False, reason, [artifact.id], [])

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
                if "." not in host or not host.isascii() or any(not label or not re.fullmatch(r"[a-z0-9-]+", label) for label in host.split(".")):
                    continue
            if any(host == domain or host.endswith(f".{domain}") for domain in DENIED_TARGET_DOMAINS):
                continue
            if any(parsed.path.lower().endswith(suffix) for suffix in STATIC_TARGET_SUFFIXES):
                continue
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
