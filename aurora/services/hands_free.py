from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen

from sqlmodel import Session, select

from aurora.config import Settings, get_settings
from aurora.models import Artifact, ChallengeGroup, ChallengeGroupItem, Fact, ImportArtifact, ImportBatch, ImportCandidate, WorkerEvent, now_utc, new_id
from aurora.services.demo import create_project_with_bootstrap
from aurora.services.prompt_renderer import PromptRenderer


MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024
ATTACHMENT_EXTENSIONS = {".7z", ".bin", ".cap", ".gz", ".iso", ".jar", ".pcap", ".pcapng", ".pdf", ".rar", ".tar", ".tgz", ".txt", ".zip"}
FetchText = Callable[[str], tuple[str, str]]
FetchBytes = Callable[[str, int], tuple[bytes, str | None]]
PostJson = Callable[[str, dict[str, Any], str | None], dict[str, Any]]
Cataloger = Callable[[dict[str, Any]], dict[str, Any]]
ProgressCallback = Callable[[str, str], None]
AttachmentCollector = Callable[[list[dict[str, Any]], str | None], None]


class AuthenticatedFetcher(Protocol):
    def fetch_text(self, url: str) -> tuple[str, str]: ...
    def fetch_bytes(self, url: str, max_bytes: int) -> tuple[bytes, str | None]: ...
    def close(self) -> None: ...


AuthenticatedFetcherFactory = Callable[[str, str | None, str | None, str | None, str | None], AuthenticatedFetcher]


class NeedsSessionError(Exception):
    def __init__(self, message: str, login_domain: str | None = None) -> None:
        super().__init__(message)
        self.login_domain = login_domain


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self.text: list[str] = []
        self.links: list[dict[str, str]] = []
        self._active_anchor: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = values.get("href") or values.get("src")
            if href:
                self._active_anchor = {"url": href, "text": values.get("title") or values.get("alt") or ""}
        elif tag in {"link", "script", "img", "source"}:
            href = values.get("href") or values.get("src")
            if href:
                self.links.append({"url": href, "text": values.get("title") or values.get("alt") or ""})

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._active_anchor is not None:
            self.links.append(self._active_anchor)
            self._active_anchor = None

    def handle_data(self, data: str) -> None:
        compact = " ".join(data.split())
        if not compact:
            return
        if self._in_title:
            self.title += compact + " "
        if self._active_anchor is not None:
            self._active_anchor["text"] = f"{self._active_anchor['text']} {compact}".strip()
        self.text.append(compact)


@dataclass
class ScanResult:
    batch: ImportBatch
    candidates: list[ImportCandidate]


class HandsFreeService:
    """Collects challenge metadata and files without entering the Solver execution path."""

    def __init__(
        self,
        settings: Settings | None = None,
        fetch_text: FetchText | None = None,
        fetch_bytes: FetchBytes | None = None,
        post_json: PostJson | None = None,
        cataloger: Cataloger | None = None,
        authenticated_fetcher_factory: AuthenticatedFetcherFactory | None = None,
        ctfplus_attachment_collector: AttachmentCollector | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.fetch_text = fetch_text or self._fetch_text
        self.fetch_bytes = fetch_bytes or self._fetch_bytes
        self.post_json = post_json or self._post_json
        self.cataloger = cataloger
        self.authenticated_fetcher_factory = authenticated_fetcher_factory or self._authenticated_fetcher
        # Kept as a constructor alias for compatibility with existing callers;
        # the collector is now used for every platform, not only CTF+.
        self.attachment_collector = ctfplus_attachment_collector or self._collect_browser_attachments

    def create_batch(self, session: Session, source_url: str, *, cookie: str | None = None, username: str | None = None, password: str | None = None) -> ImportBatch:
        source_url = self._safe_url(source_url)
        if not self.settings.cataloger_configured and self.cataloger is None and not self._is_ctfplus_problem_bank(source_url):
            raise ValueError("cataloger Agent is not configured; set AURORA_CATALOGER_LLM_API_KEY")
        auth_method = self._auth_method(cookie, username, password)
        batch = ImportBatch(source_url=source_url, auth_method=auth_method)
        session.add(batch)
        session.commit()
        session.refresh(batch)
        return batch

    def scan(self, session: Session, source_url: str, *, cookie: str | None = None, username: str | None = None, password: str | None = None, login_url: str | None = None, progress: ProgressCallback | None = None) -> ScanResult:
        batch = self.create_batch(session, source_url, cookie=cookie, username=username, password=password)
        return self._scan_batch(session, batch, cookie=cookie, username=username, password=password, login_url=login_url, progress=progress)

    def run_batch(self, session: Session, batch_id: str, *, cookie: str | None = None, username: str | None = None, password: str | None = None, login_url: str | None = None, progress: ProgressCallback | None = None) -> ScanResult:
        batch = session.get(ImportBatch, batch_id)
        if batch is None:
            raise ValueError("import batch not found")
        if batch.status != "SCANNING":
            raise ValueError("import batch is not ready to run")
        return self._scan_batch(session, batch, cookie=cookie, username=username, password=password, login_url=login_url, progress=progress)

    def continue_scan(self, session: Session, batch_id: str, *, cookie: str | None = None, username: str | None = None, password: str | None = None, login_url: str | None = None, progress: ProgressCallback | None = None) -> ScanResult:
        self.prepare_continue(session, batch_id, cookie=cookie, username=username, password=password)
        return self.run_batch(session, batch_id, cookie=cookie, username=username, password=password, login_url=login_url, progress=progress)

    def prepare_continue(self, session: Session, batch_id: str, *, cookie: str | None = None, username: str | None = None, password: str | None = None) -> ImportBatch:
        batch = session.get(ImportBatch, batch_id)
        if batch is None:
            raise ValueError("import batch not found")
        if batch.status != "NEEDS_SESSION":
            raise ValueError("import batch does not require a login session")
        if self._auth_method(cookie, username, password) is None:
            raise ValueError("provide a Cookie or username and password to continue")
        batch.auth_method = self._auth_method(cookie, username, password)
        batch.auth_message = None
        batch.error = None
        batch.status = "SCANNING"
        batch.updated_at = now_utc()
        session.add(batch)
        session.commit()
        session.refresh(batch)
        return batch

    def _scan_batch(self, session: Session, batch: ImportBatch, *, cookie: str | None, username: str | None, password: str | None, login_url: str | None, progress: ProgressCallback | None = None) -> ScanResult:
        fetcher: AuthenticatedFetcher | None = None
        try:
            self._progress(progress, "FETCHING", "正在访问题目列表页面。")
            fetch_text = self.fetch_text
            fetch_bytes = self.fetch_bytes
            if self._auth_method(cookie, username, password):
                fetcher = self.authenticated_fetcher_factory(batch.source_url, cookie, username, password, login_url)
                fetch_text = fetcher.fetch_text
                fetch_bytes = fetcher.fetch_bytes
            html, final_url = fetch_text(batch.source_url)
            final_url = self._safe_url(final_url)
            if self._looks_like_login_page(html, final_url):
                raise NeedsSessionError("Login is required. Provide a valid Cookie to continue.", urlparse(final_url).hostname)
            inventory = self._inventory(html, final_url)
            if self._is_ctfplus_problem_bank(final_url):
                self._progress(progress, "CATALOGING", "正在通过 CTF+ 题库 API 识别当前页题目。")
                inventory, candidates, model_result = self._ctfplus_problem_bank(final_url, cookie=cookie, inventory=inventory)
            else:
                self._progress(progress, "CATALOGING", "正在归集题目链接并调用 Cataloger Agent。")
                model_result = self._catalog(inventory)
                candidates = self._validated_candidates(model_result, inventory)
                if not candidates and fetcher is None:
                    browser_inventory = self._browser_inventory(final_url)
                    if browser_inventory is not None:
                        inventory = browser_inventory
                        model_result = self._catalog(inventory)
                        candidates = self._validated_candidates(model_result, inventory)
                if not candidates:
                    candidates = self._heuristic_candidates(inventory)
                if candidates:
                    self._progress(progress, "DETAILS", f"正在检查 {len(candidates)} 个题目详情页中的附件。")
                    self._enrich_candidate_attachments(candidates, fetch_text)
            if candidates:
                self._progress(progress, "DETAILS", f"正在从 {len(candidates)} 个题目详情页补全浏览器下载附件。")
                collect_in_session = getattr(fetcher, "collect_attachments", None) if fetcher is not None else None
                if callable(collect_in_session):
                    collect_in_session(candidates, self._collect_candidate_browser_attachments)
                else:
                    self.attachment_collector(candidates, cookie)
                if all(candidate.get("_attachment_needs_session") for candidate in candidates):
                    raise NeedsSessionError("Challenge attachments require a valid login session. Provide a valid Cookie to continue.", urlparse(final_url).hostname)
            self._progress(progress, "STAGING", "正在暂存同域附件。")
            persisted = self._persist_candidates(session, batch, candidates, inventory, fetch_bytes)
            batch.title = inventory["title"] or None
            batch.summary = str(model_result.get("summary", ""))[:1000]
            batch.status = "READY"
            batch.updated_at = now_utc()
            session.add(batch)
            session.commit()
            session.refresh(batch)
            self._progress(progress, "READY", f"识别完成，共 {len(persisted)} 个候选题目。")
            return ScanResult(batch=batch, candidates=persisted)
        except NeedsSessionError as exc:
            batch.status = "NEEDS_SESSION"
            batch.login_domain = exc.login_domain
            batch.auth_message = str(exc)[:1000]
            batch.error = None
            batch.updated_at = now_utc()
            session.add(batch)
            session.commit()
            session.refresh(batch)
            self._progress(progress, "NEEDS_SESSION", batch.auth_message or "需要登录会话才能继续。")
            return ScanResult(batch=batch, candidates=[])
        except Exception as exc:
            batch.status = "FAILED"
            batch.error = str(exc)[:2000]
            batch.updated_at = now_utc()
            session.add(batch)
            session.commit()
            self._progress(progress, "FAILED", str(exc)[:1000])
            if isinstance(exc, ValueError):
                raise
            raise ValueError(f"catalog import failed: {exc}") from exc
        finally:
            if fetcher is not None:
                fetcher.close()

    @staticmethod
    def _progress(callback: ProgressCallback | None, phase: str, detail: str) -> None:
        if callback is not None:
            callback(phase, detail)

    def get_batch(self, session: Session, batch_id: str) -> ScanResult:
        batch = session.get(ImportBatch, batch_id)
        if batch is None:
            raise ValueError("import batch not found")
        candidates = session.exec(select(ImportCandidate).where(ImportCandidate.batch_id == batch_id).order_by(ImportCandidate.created_at)).all()
        return ScanResult(batch=batch, candidates=candidates)

    def confirm(self, session: Session, batch_id: str, candidate_ids: list[str], name_overrides: dict[str, str] | None = None) -> list[dict[str, str]]:
        batch = session.get(ImportBatch, batch_id)
        if batch is None:
            raise ValueError("import batch not found")
        if batch.status != "READY":
            raise ValueError("import batch is not ready for confirmation")
        requested = list(dict.fromkeys(candidate_ids))
        if not requested:
            raise ValueError("select at least one candidate")
        found_candidates = session.exec(
            select(ImportCandidate).where(ImportCandidate.batch_id == batch_id, ImportCandidate.id.in_(requested))
        ).all()
        if len(found_candidates) != len(requested):
            raise ValueError("one or more candidates do not belong to this import batch")
        candidates_by_id = {candidate.id: candidate for candidate in found_candidates}
        candidates = [candidates_by_id[candidate_id] for candidate_id in requested]
        group = ChallengeGroup(
            import_batch_id=batch_id,
            name=(batch.title or f"Imported batch {batch_id[-8:]}")[:240],
            limits={"max_iterations": 20, "max_minutes": 0, "no_progress_limit": 4, "stop_on_observer_escalate": True},
        )
        session.add(group)
        session.commit()
        session.refresh(group)
        results: list[dict[str, str]] = []
        for position, candidate in enumerate(candidates, start=1):
            if candidate.project_id:
                session.add(ChallengeGroupItem(group_id=group.id, project_id=candidate.project_id, position=position))
                session.commit()
                results.append({"candidate_id": candidate.id, "project_id": candidate.project_id, "status": "existing", "group_id": group.id})
                continue
            name = (name_overrides or {}).get(candidate.id, "").strip() or candidate.title
            project = create_project_with_bootstrap(
                session,
                name=name[:240],
                goal=candidate.description or f"Analyze the imported challenge: {candidate.title}",
                challenge_type=candidate.challenge_type,
                # A CTF platform page is a source of challenge material, not an
                # authorized target for Workers.  TargetVerificationService uses
                # the ephemeral browser session to discover an explicit instance
                # URL and only that discovered host becomes tool-authorized.
                allowed_hosts=[],
                hint="Challenge page retained as import evidence; do not treat the training platform as a target.",
            )
            project.target_verification_status = "NEEDS_SESSION"
            project.target_verification_reason = "导入后等待登录会话以验证靶机启动"
            session.add(project)
            artifacts = self._attach_staged_artifacts(session, candidate, project.id)
            session.add(Fact(
                project_id=project.id,
                statement="Challenge source page was imported as evidence; await an explicitly discovered target URL before network actions.",
                category="import",
                confidence=candidate.confidence,
                evidence_refs=artifacts,
            ))
            session.add(WorkerEvent(
                project_id=project.id,
                event_type="import.confirmed",
                payload_json={"batch_id": batch_id, "candidate_id": candidate.id, "source_url": batch.source_url, "artifact_refs": artifacts},
            ))
            candidate.project_id = project.id
            candidate.confirmed = True
            candidate.updated_at = now_utc()
            session.add(candidate)
            session.add(ChallengeGroupItem(group_id=group.id, project_id=project.id, position=position))
            session.commit()
            results.append({"candidate_id": candidate.id, "project_id": project.id, "status": "created", "group_id": group.id, "challenge_url": candidate.challenge_url})
        return results

    @staticmethod
    def _is_ctfplus_problem_bank(url: str) -> bool:
        parsed = urlparse(url)
        return (parsed.hostname or "").lower().rstrip(".") in {"ctfplus.cn", "www.ctfplus.cn"} and parsed.path.rstrip("/") == "/learning/problem/problem-bank"

    def _ctfplus_problem_bank(self, source_url: str, *, cookie: str | None, inventory: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        parsed = urlparse(source_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        page = self._query_int(query, "page", 1, minimum=1)
        size = self._query_int(query, "size", 20, minimum=1, maximum=100)
        payload: dict[str, Any] = {
            "order": self._query_int(query, "order", 3, minimum=1),
            "name": self._query_value(query, "name", ""),
            "tags": [tag for tag in query.get("tags", []) if tag],
            "publicType": self._query_int(query, "publicType", -1),
            "problemType": self._query_int(query, "problemType", -1),
            "problemTypeGroup": self._query_int(query, "problemTypeGroup", -1),
            "isSolved": self._query_int(query, "isSolved", -1),
            "favoriteId": self._query_value(query, "favoriteId", ""),
            "payment": {},
            "page": {"page": page, "size": size},
        }
        if "difficulty" in query:
            payload["difficulty"] = self._query_int(query, "difficulty", -1)
        endpoint = f"{parsed.scheme}://{parsed.netloc}/api/problem/searchPublicProblem"
        response = self.post_json(endpoint, payload, cookie)
        if int(response.get("code", 200)) != 200:
            message = str(response.get("msg") or "CTF+ problem API request failed")
            if re.search(r"login|auth|token|登录|未登录|权限", message, re.IGNORECASE):
                raise NeedsSessionError("CTF+ requires a valid login session. Provide a valid Cookie to continue.", parsed.hostname)
            raise ValueError(message)
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("problems"), list):
            raise ValueError("CTF+ problem API returned an invalid problem list")

        origin = f"{parsed.scheme}://{parsed.netloc}"
        candidates = [self._ctfplus_candidate(problem, origin) for problem in data["problems"] if isinstance(problem, dict)]
        candidates = [candidate for candidate in candidates if candidate is not None]
        tag_label = "、".join(payload["tags"][:3])
        inventory = {**inventory, "title": f"CTF+ 题库{f' · {tag_label}' if tag_label else ''}", "text": f"CTF+ API 识别：第 {page} 页，共 {data.get('total', len(candidates))} 道匹配题目。", "links": [], "collected_links": []}
        summary = f"CTF+ API identified {len(candidates)} problem(s) from page {page} (page size {size}; total {data.get('total', len(candidates))})."
        return inventory, candidates, {"summary": summary}

    @classmethod
    def _ctfplus_candidate(cls, problem: dict[str, Any], origin: str) -> dict[str, Any] | None:
        problem_id = str(problem.get("id") or "").strip()
        title = str(problem.get("name") or "").strip()
        if not problem_id or not title:
            return None
        tags = [str(tag.get("name")) for tag in problem.get("tags", []) if isinstance(tag, dict) and tag.get("name")]
        public_id = str(problem.get("publicId") or "").strip()
        details = [str(problem.get("desc") or "").strip()]
        if tags:
            details.append(f"标签：{'、'.join(tags)}")
        if public_id:
            details.append(f"公开题号：{public_id}")
        if problem.get("difficulty") is not None:
            details.append(f"难度：{problem['difficulty']}")
        return {"title": title[:500], "description": "\n".join(part for part in details if part)[:3000], "challenge_url": f"{origin}/learning/problem/problem-detail/{problem_id}/description", "challenge_type": cls._ctfplus_problem_type(tags), "confidence": 0.98, "attachment_urls": cls._ctfplus_attachment_urls(problem.get("attachments"), origin)}

    @classmethod
    def _ctfplus_attachment_urls(cls, attachments: Any, origin: str) -> list[str]:
        if not isinstance(attachments, list):
            return []
        urls: list[str] = []
        for attachment in attachments:
            raw = attachment if isinstance(attachment, str) else next((attachment.get(key) for key in ("downloadUrl", "url", "fileUrl", "path") if isinstance(attachment, dict) and attachment.get(key)), None)
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                urls.append(cls._safe_url(urljoin(origin, raw)))
            except ValueError:
                continue
        return list(dict.fromkeys(urls))

    @staticmethod
    def _ctfplus_problem_type(tags: list[str]) -> str:
        values = {tag.lower() for tag in tags}
        for token, challenge_type in (("web", "web"), ("reverse", "reverse"), ("re", "reverse"), ("crypto", "crypto"), ("pwn", "pwn"), ("misc", "misc")):
            if token in values:
                return challenge_type
        return "unknown"

    @staticmethod
    def _query_value(query: dict[str, list[str]], name: str, default: str) -> str:
        return (query.get(name) or [default])[0]

    @classmethod
    def _query_int(cls, query: dict[str, list[str]], name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
        try:
            value = int(cls._query_value(query, name, str(default)))
        except ValueError:
            return default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _inventory(self, html: str, page_url: str) -> dict[str, Any]:
        parser = _PageParser()
        parser.feed(html)
        seen: set[str] = set()
        links: list[dict[str, str]] = []
        for link in parser.links:
            absolute = urljoin(page_url, link["url"])
            try:
                absolute = self._safe_url(absolute)
            except ValueError:
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            links.append({"url": absolute, "text": link["text"][:400], "is_attachment": self._is_attachment(absolute)})
        return {
            "page_url": page_url,
            "title": parser.title.strip()[:500],
            "text": " ".join(parser.text)[:12000],
            "links": links[:500],
            "collected_links": [link["url"] for link in links],
        }

    def _browser_inventory(self, url: str) -> dict[str, Any] | None:
        """Use an optional browser only after static extraction produced no candidate."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.goto(url, wait_until="networkidle", timeout=30_000)
                    return self._inventory(page.content(), self._safe_url(page.url))
                finally:
                    browser.close()
        except Exception:
            # Browser support is an optional enrichment, never a required runtime dependency.
            return None

    def _catalog(self, inventory: dict[str, Any]) -> dict[str, Any]:
        if self.cataloger is not None:
            return self.cataloger(inventory)
        prompt = PromptRenderer().read_prompt("cataloger.system.md")
        payload = {
            "model": self.settings.cataloger_llm_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(inventory, ensure_ascii=True)},
            ],
        }
        endpoint = self.settings.cataloger_llm_base_url.rstrip("/") + "/chat/completions"
        request = Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers={"Authorization": f"Bearer {self.settings.cataloger_llm_api_key}", "Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=self.settings.cataloger_llm_timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ValueError(f"cataloger Agent request failed: {exc}") from exc
        content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("cataloger Agent returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ValueError("cataloger Agent returned an invalid response")
        return result

    def _validated_candidates(self, result: dict[str, Any], inventory: dict[str, Any]) -> list[dict[str, Any]]:
        valid_links = set(inventory["collected_links"])
        links_by_url = {link["url"]: link for link in inventory["links"]}
        candidates: list[dict[str, Any]] = []
        for raw in result.get("candidates", []):
            if not isinstance(raw, dict) or raw.get("challenge_url") not in valid_links:
                continue
            attachments = [url for url in raw.get("attachment_urls", []) if isinstance(url, str) and url in valid_links and self._is_attachment_link(links_by_url[url])]
            title = str(raw.get("title") or raw["challenge_url"]).strip()
            if not title:
                continue
            candidates.append({
                "title": title[:500], "description": str(raw.get("description") or "")[:3000],
                "challenge_url": raw["challenge_url"], "challenge_type": str(raw.get("challenge_type") or "unknown")[:80],
                "confidence": max(0.0, min(1.0, float(raw.get("confidence", 0.5)))), "attachment_urls": attachments,
            })
        return candidates

    def _enrich_candidate_attachments(self, candidates: list[dict[str, Any]], fetch_text: FetchText) -> None:
        for candidate in candidates:
            try:
                html, final_url = fetch_text(candidate["challenge_url"])
                details = self._inventory(html, self._safe_url(final_url))
                attachments = [link["url"] for link in details["links"] if self._is_attachment_link(link)]
                candidate["attachment_urls"] = list(dict.fromkeys([*candidate["attachment_urls"], *attachments]))
            except Exception:
                # A single inaccessible challenge detail page must not prevent other imports.
                continue

    def _collect_browser_attachments(self, candidates: list[dict[str, Any]], cookie: str | None) -> None:
        """Collect files from rendered detail pages when static extraction misses them."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            for candidate in candidates:
                candidate.setdefault("_attachment_issues", []).append({"status": "download_failed", "reason": "Playwright Chromium is required to collect browser-managed attachments"})
            return

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            try:
                if cookie and cookie.strip():
                    source_host = (urlparse(candidates[0]["challenge_url"]).hostname or "") if candidates else ""
                    context.add_cookies(self._parse_browser_cookie(cookie, source_host))
                for candidate in candidates:
                    self._collect_candidate_browser_attachments(context, candidate)
            finally:
                context.close()
                browser.close()

    def _collect_candidate_browser_attachments(self, context: Any, candidate: dict[str, Any]) -> None:
        page = context.new_page()
        try:
            response = page.goto(candidate["challenge_url"], wait_until="networkidle", timeout=30_000)
            if response is not None and response.status in {401, 403}:
                candidate["_attachment_needs_session"] = True
                candidate.setdefault("_attachment_issues", []).append({"status": "needs_session", "reason": "attachment requires a logged-in Cookie"})
                return
            if self._looks_like_login_page(page.content(), page.url):
                candidate["_attachment_needs_session"] = True
                candidate.setdefault("_attachment_issues", []).append({"status": "needs_session", "reason": "attachment requires a logged-in Cookie"})
                return
            controls = page.locator('a[download], a[href*="download" i], a[aria-label*="下载" i], a[aria-label*="attachment" i], button[aria-label*="下载" i], button[aria-label*="download" i], button:has-text("下载"), button:has-text("Download")')
            seen: set[str] = set()
            for index in range(controls.count()):
                control = controls.nth(index)
                label = (control.get_attribute("aria-label") or control.inner_text(timeout=5_000) or control.get_attribute("download") or "attachment.bin").strip()
                href = control.get_attribute("href") or ""
                signature = f"{label}|{href}"
                if signature in seen:
                    continue
                seen.add(signature)
                # Ordinary anchor downloads are collected by the static detail-page
                # parser.  Clicking them here makes browsers wait for a download even
                # when a platform opens a login/interstitial page in a new tab.
                if href and not href.strip().lower().startswith(("javascript:", "#")):
                    continue
                if re.search(r"dynamic|动态", label, re.IGNORECASE):
                    candidate.setdefault("_attachment_issues", []).append({"filename": label, "status": "dynamic_attachment", "reason": "dynamic attachment must be obtained from the challenge page"})
                    continue
                try:
                    with page.expect_download(timeout=30_000) as event:
                        control.click(timeout=10_000)
                    download = event.value
                    path = download.path()
                    if path is None:
                        raise ValueError("download did not provide a local file")
                    data = Path(path).read_bytes()
                    if len(data) > MAX_ATTACHMENT_BYTES:
                        raise ValueError("attachment exceeds size limit")
                    candidate.setdefault("_downloaded_attachments", []).append({"filename": download.suggested_filename or label, "data": data, "mime_type": mimetypes.guess_type(download.suggested_filename or label)[0], "source_url": candidate["challenge_url"]})
                except Exception as exc:
                    candidate.setdefault("_attachment_issues", []).append({"filename": label, "status": "download_failed", "reason": str(exc)[:300]})
        except Exception as exc:
            candidate.setdefault("_attachment_issues", []).append({"status": "download_failed", "reason": f"detail attachment inspection failed: {exc}"[:300]})
        finally:
            page.close()

    @staticmethod
    def _parse_browser_cookie(raw_cookie: str, domain: str) -> list[dict[str, str]]:
        value = raw_cookie.strip().removeprefix("Cookie:").strip()
        cookies = []
        for part in value.split(";"):
            name, separator, cookie_value = part.strip().partition("=")
            if separator and name:
                cookies.append({"name": name, "value": cookie_value, "domain": domain, "path": "/"})
        if not cookies:
            raise NeedsSessionError("The supplied Cookie is empty or invalid.", domain)
        return cookies

    def _heuristic_candidates(self, inventory: dict[str, Any]) -> list[dict[str, Any]]:
        candidates = []
        for link in inventory["links"]:
            text = (link["text"] + " " + link["url"]).lower()
            if link["is_attachment"] or not re.search(r"challenge|task|problem|ctf|题目|赛题", text):
                continue
            candidates.append({"title": link["text"] or link["url"], "description": "Detected from page navigation.", "challenge_url": link["url"], "challenge_type": "unknown", "confidence": 0.35, "attachment_urls": []})
        return candidates[:50]

    def _persist_candidates(self, session: Session, batch: ImportBatch, candidates: list[dict[str, Any]], inventory: dict[str, Any], fetch_bytes: FetchBytes) -> list[ImportCandidate]:
        persisted: list[ImportCandidate] = []
        for item in candidates:
            staged, external = self._stage_attachments(session, batch, inventory["page_url"], item["attachment_urls"], fetch_bytes)
            staged_hashes = {str(reference.get("sha256")) for reference in staged if reference.get("sha256")}
            for downloaded in item.get("_downloaded_attachments", []):
                try:
                    digest = hashlib.sha256(downloaded.get("data", b"")).hexdigest() if isinstance(downloaded.get("data"), bytes) else ""
                    if digest and digest in staged_hashes:
                        continue
                    reference = self._stage_attachment_data(session, batch, downloaded)
                    staged.append(reference)
                    staged_hashes.add(str(reference["sha256"]))
                except Exception as exc:
                    external.append({"url": downloaded.get("source_url", item["challenge_url"]), "filename": downloaded.get("filename"), "status": "download_failed", "reason": str(exc)[:300]})
            external.extend(item.get("_attachment_issues", []))
            candidate = ImportCandidate(
                batch_id=batch.id, title=item["title"], description=item["description"], challenge_url=item["challenge_url"],
                challenge_type=item["challenge_type"], confidence=item["confidence"], staged_attachments_json=staged,
                external_attachments_json=external, evidence_json=[{"source_url": inventory["page_url"], "title": inventory["title"]}],
            )
            session.add(candidate)
            persisted.append(candidate)
        session.commit()
        for candidate in persisted:
            session.refresh(candidate)
        return persisted

    def _stage_attachments(self, session: Session, batch: ImportBatch, page_url: str, urls: list[str], fetch_bytes: FetchBytes | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        staged: list[dict[str, Any]] = []
        external: list[dict[str, Any]] = []
        for url in dict.fromkeys(urls):
            if not self._same_domain(page_url, url):
                external.append({"url": url, "status": "external_review_required"})
                continue
            try:
                data, mime_type = (fetch_bytes or self.fetch_bytes)(url, MAX_ATTACHMENT_BYTES)
                if len(data) > MAX_ATTACHMENT_BYTES:
                    raise ValueError("attachment exceeds size limit")
                if self._attachment_response_requires_login(data, mime_type):
                    raise NeedsSessionError("Attachment endpoint requires a valid login session.", urlparse(page_url).hostname)
                if self._attachment_response_is_html(data, mime_type):
                    raise ValueError("attachment endpoint returned an HTML page instead of a file")
                filename = self._filename(url, data, mime_type)
                path = self.settings.artifact_dir / "imports" / batch.id / f"{new_id('file')}_{filename}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                imported = ImportArtifact(batch_id=batch.id, source_url=url, filename=filename, path=str(path), sha256=hashlib.sha256(data).hexdigest(), mime_type=mime_type or mimetypes.guess_type(filename)[0], size=len(data))
                session.add(imported)
                session.commit()
                session.refresh(imported)
                staged.append({"import_artifact_id": imported.id, "filename": filename, "url": url, "size": len(data), "sha256": imported.sha256, "status": "staged"})
            except NeedsSessionError:
                raise
            except Exception as exc:
                external.append({"url": url, "status": "download_failed", "reason": str(exc)[:300]})
        return staged, external

    @staticmethod
    def _attachment_response_requires_login(data: bytes, mime_type: str | None) -> bool:
        """Reject HTML authentication interstitials returned with HTTP 200 as files."""
        content_type = (mime_type or "").lower()
        sample = data[:200_000].lstrip()
        if "html" not in content_type and not sample.startswith((b"<!doctype html", b"<html", b"<HTML")):
            return False
        text = sample.decode("utf-8", errors="ignore").lower()
        return bool(
            re.search(r"(?:请登录|需要登录|登录后|login required|authentication required|sign[ -]?in required)", text)
            or HandsFreeService._looks_like_login_page(text, "")
        )

    @staticmethod
    def _attachment_response_is_html(data: bytes, mime_type: str | None) -> bool:
        content_type = (mime_type or "").lower()
        sample = data[:4_096].lstrip().lower()
        return "html" in content_type or sample.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))

    def _stage_attachment_data(self, session: Session, batch: ImportBatch, attachment: dict[str, Any]) -> dict[str, Any]:
        data = attachment.get("data")
        if not isinstance(data, bytes) or len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment exceeds size limit or has invalid content")
        if self._attachment_response_is_html(data, attachment.get("mime_type")):
            raise ValueError("browser download returned an HTML page instead of a file")
        filename = self._safe_filename(str(attachment.get("filename") or "attachment.bin"))
        detected_suffix = self._attachment_suffix(data, attachment.get("mime_type"))
        if detected_suffix and (Path(filename).suffix.lower() not in ATTACHMENT_EXTENSIONS or Path(filename).suffix.lower() == ".html"):
            filename = self._safe_filename(f"{Path(filename).stem or 'attachment'}{detected_suffix}")
        path = self.settings.artifact_dir / "imports" / batch.id / f"{new_id('file')}_{filename}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        imported = ImportArtifact(batch_id=batch.id, source_url=str(attachment.get("source_url") or ""), filename=filename, path=str(path), sha256=hashlib.sha256(data).hexdigest(), mime_type=attachment.get("mime_type") or mimetypes.guess_type(filename)[0], size=len(data))
        session.add(imported)
        session.commit()
        session.refresh(imported)
        return {"import_artifact_id": imported.id, "filename": filename, "url": attachment.get("source_url"), "size": len(data), "sha256": imported.sha256, "status": "staged"}

    def _attach_staged_artifacts(self, session: Session, candidate: ImportCandidate, project_id: str) -> list[str]:
        ids: list[str] = []
        for reference in candidate.staged_attachments_json:
            imported = session.get(ImportArtifact, reference.get("import_artifact_id"))
            if imported is None or not Path(imported.path).is_file():
                continue
            artifact = Artifact(project_id=project_id, type="imported_attachment", path=imported.path, sha256=imported.sha256, mime_type=imported.mime_type, size=imported.size, summary=f"Imported attachment: {imported.filename}")
            session.add(artifact)
            session.commit()
            session.refresh(artifact)
            ids.append(artifact.id)
        return ids

    @staticmethod
    def _auth_method(cookie: str | None, username: str | None, password: str | None) -> str | None:
        if cookie and cookie.strip():
            return "cookie"
        if username and password:
            return "password"
        if username or password:
            raise ValueError("username and password must be provided together")
        return None

    @staticmethod
    def _looks_like_login_page(html: str, final_url: str) -> bool:
        lowered = (html[:200_000] + " " + final_url).lower()
        return bool(re.search(r"(?:/login|/signin|/auth|登录|sign[ -]?in|log[ -]?in)", lowered) and re.search(r"(?:password|密码|type=[\"']password)", lowered))

    @staticmethod
    def _authenticated_fetcher(source_url: str, cookie: str | None, username: str | None, password: str | None, login_url: str | None) -> AuthenticatedFetcher:
        return _PlaywrightAuthenticatedFetcher(source_url, cookie, username, password, login_url)

    @staticmethod
    def _safe_url(url: str) -> str:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("only absolute HTTP(S) URLs are allowed")
        hostname = parsed.hostname.lower().rstrip(".")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if hostname in {"localhost", "metadata.google.internal"} or hostname.endswith(".localhost") or hostname == "::1" or (address is not None and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved)):
            raise ValueError("metadata and local management endpoints are not allowed")
        return parsed.geturl()

    @staticmethod
    def _same_domain(page_url: str, attachment_url: str) -> bool:
        page_host = (urlparse(page_url).hostname or "").lower().rstrip(".")
        attachment_host = (urlparse(attachment_url).hostname or "").lower().rstrip(".")
        return bool(page_host and attachment_host and (attachment_host == page_host or attachment_host.endswith("." + page_host)))

    @staticmethod
    def _is_attachment(url: str) -> bool:
        return Path(urlparse(url).path).suffix.lower() in ATTACHMENT_EXTENSIONS

    @classmethod
    def _is_attachment_link(cls, link: dict[str, Any]) -> bool:
        if bool(link.get("is_attachment")) or cls._is_attachment(str(link.get("url", ""))):
            return True
        label = f"{link.get('text', '')} {link.get('url', '')}".lower()
        return bool(re.search(r"(?:attachment|download|附件|下载)", label))

    @staticmethod
    def _filename(url: str, data: bytes | None = None, mime_type: str | None = None) -> str:
        """Use payload signatures when a download endpoint hides its real filename."""
        path = Path(urlparse(url).path)
        name = path.name or "attachment.bin"
        suffix = path.suffix.lower()
        detected_suffix = HandsFreeService._attachment_suffix(data or b"", mime_type)
        if detected_suffix and (suffix not in ATTACHMENT_EXTENSIONS or suffix == ".html"):
            name = f"{path.stem or 'attachment'}{detected_suffix}"
        return HandsFreeService._safe_filename(name)

    @staticmethod
    def _attachment_suffix(data: bytes, mime_type: str | None) -> str | None:
        """Infer common CTF attachment formats without trusting endpoint URL paths."""
        content_type = (mime_type or "").split(";", 1)[0].strip().lower()
        signatures = (
            (b"PK\x03\x04", ".zip"), (b"PK\x05\x06", ".zip"), (b"PK\x07\x08", ".zip"),
            (b"Rar!\x1a\x07", ".rar"), (b"7z\xbc\xaf\x27\x1c", ".7z"), (b"\x1f\x8b", ".gz"),
            (b"%PDF-", ".pdf"), (b"\x7fELF", ".elf"), (b"MZ", ".exe"),
            (b"\xd4\xc3\xb2\xa1", ".pcap"), (b"\xa1\xb2\xc3\xd4", ".pcap"), (b"\x0a\x0d\x0d\x0a", ".pcapng"),
        )
        for signature, suffix in signatures:
            if data.startswith(signature):
                return suffix
        return {
            "application/zip": ".zip", "application/x-rar-compressed": ".rar", "application/x-7z-compressed": ".7z",
            "application/gzip": ".gz", "application/pdf": ".pdf", "application/java-archive": ".jar",
            "application/vnd.tcpdump.pcap": ".pcap",
        }.get(content_type)

    @staticmethod
    def _safe_filename(name: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:180] or "attachment.bin"

    @staticmethod
    def _fetch_text(url: str) -> tuple[str, str]:
        request = Request(url, headers={"User-Agent": "Aurora-Cataloger/1.0"})
        with urlopen(request, timeout=20) as response:
            return response.read(2 * 1024 * 1024).decode(response.headers.get_content_charset() or "utf-8", errors="replace"), response.geturl()

    @staticmethod
    def _post_json(url: str, payload: dict[str, Any], cookie: str | None) -> dict[str, Any]:
        headers = {"User-Agent": "Aurora-Cataloger/1.0", "Content-Type": "application/json"}
        if cookie and cookie.strip():
            headers["Cookie"] = cookie.strip().removeprefix("Cookie:").strip()
        request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=20) as response:
                body = json.loads(response.read(2 * 1024 * 1024).decode(response.headers.get_content_charset() or "utf-8"))
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise NeedsSessionError("CTF+ requires a valid login session. Provide a valid Cookie to continue.", urlparse(url).hostname) from exc
            raise ValueError(f"CTF+ problem API request failed with HTTP {exc.code}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ValueError(f"CTF+ problem API request failed: {exc}") from exc
        if not isinstance(body, dict):
            raise ValueError("CTF+ problem API returned an invalid response")
        return body

    @staticmethod
    def _fetch_bytes(url: str, max_bytes: int) -> tuple[bytes, str | None]:
        request = Request(url, headers={"User-Agent": "Aurora-Cataloger/1.0"})
        with urlopen(request, timeout=30) as response:
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("attachment exceeds size limit")
            return data, response.headers.get_content_type()


class _PlaywrightAuthenticatedFetcher:
    """Ephemeral authenticated browser context. Credentials never leave this object."""

    def __init__(self, source_url: str, cookie: str | None, username: str | None, password: str | None, login_url: str | None) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ValueError("authenticated imports require Playwright; install the browser runtime first") from exc
        self.source_url = HandsFreeService._safe_url(source_url)
        self.source_host = urlparse(self.source_url).hostname or ""
        self._playwright = None
        self._browser = None
        self._context = None
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            self._context = self._browser.new_context()
            if cookie and cookie.strip():
                self._context.add_cookies(self._parse_cookie(cookie))
            if username and password:
                self._login(login_url or self.source_url, username, password)
        except (NeedsSessionError, ValueError):
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise ValueError("authenticated imports require a working Playwright Chromium runtime; run `python -m playwright install chromium`") from exc

    def fetch_text(self, url: str) -> tuple[str, str]:
        page = self._context.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            if response is None:
                raise ValueError("page returned no response")
            final_url = HandsFreeService._safe_url(page.url)
            if not HandsFreeService._same_domain(self.source_url, final_url):
                raise ValueError("redirected outside the source domain")
            if response.status in {401, 403}:
                raise NeedsSessionError("The login session does not have access to this page.", urlparse(final_url).hostname)
            return page.content(), final_url
        finally:
            page.close()

    def fetch_bytes(self, url: str, max_bytes: int) -> tuple[bytes, str | None]:
        response = self._context.request.get(url, timeout=30_000, max_redirects=5)
        final_url = HandsFreeService._safe_url(response.url)
        if not HandsFreeService._same_domain(self.source_url, final_url):
            raise ValueError("attachment redirected outside the source domain")
        if not response.ok:
            if response.status in {401, 403}:
                raise NeedsSessionError("The login session no longer has access to this attachment.", urlparse(final_url).hostname)
            raise ValueError(f"attachment request failed with HTTP {response.status}")
        data = response.body()
        if len(data) > max_bytes:
            raise ValueError("attachment exceeds size limit")
        return data, response.headers.get("content-type")

    def collect_attachments(self, candidates: list[dict[str, Any]], collector: Callable[[Any, dict[str, Any]], None]) -> None:
        """Run browser attachment collection in the authenticated import context."""
        for candidate in candidates:
            collector(self._context, candidate)

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def _parse_cookie(self, raw_cookie: str) -> list[dict[str, str]]:
        value = raw_cookie.strip()
        if value.lower().startswith("cookie:"):
            value = value.split(":", 1)[1].strip()
        cookies: list[dict[str, str]] = []
        for part in value.split(";"):
            name, separator, cookie_value = part.strip().partition("=")
            if not separator or not name:
                continue
            cookies.append({"name": name, "value": cookie_value, "domain": self.source_host, "path": "/"})
        if not cookies:
            raise NeedsSessionError("The supplied Cookie is empty or invalid.", self.source_host)
        return cookies

    def _login(self, login_url: str, username: str, password: str) -> None:
        login_url = HandsFreeService._safe_url(login_url)
        if not HandsFreeService._same_domain(self.source_url, login_url):
            raise ValueError("login URL must use the source domain or one of its subdomains")
        page = self._context.new_page()
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)
            body = page.locator("body").inner_text(timeout=5_000).lower()
            if re.search(r"captcha|two.factor|2fa|验证码|二次验证", body):
                raise NeedsSessionError("This platform requires interactive verification. Complete login in your browser and paste its Cookie.", urlparse(page.url).hostname)
            user_fields = page.locator("input[type='email'], input[name*='user' i], input[name*='email' i], input[name*='login' i]")
            password_fields = page.locator("input[type='password']")
            if user_fields.count() < 1 or password_fields.count() < 1:
                raise NeedsSessionError("The platform login form is not supported. Paste an authenticated Cookie to continue.", urlparse(page.url).hostname)
            user_fields.first.fill(username)
            password_fields.first.fill(password)
            submit = page.locator("button[type='submit'], input[type='submit']")
            if submit.count() < 1:
                raise NeedsSessionError("The platform login form is not supported. Paste an authenticated Cookie to continue.", urlparse(page.url).hostname)
            submit.first.click()
            page.wait_for_timeout(1_000)
            body = page.locator("body").inner_text(timeout=5_000).lower()
            if re.search(r"captcha|two.factor|2fa|验证码|二次验证", body) or HandsFreeService._looks_like_login_page(page.content(), page.url):
                raise NeedsSessionError("Login did not complete. Complete login in your browser and paste its Cookie.", urlparse(page.url).hostname)
        finally:
            page.close()
