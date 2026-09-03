from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import queue
import re
import threading
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sqlmodel import Session, select

from aurora.config import Settings, get_settings
from aurora.models import Artifact, ChallengeGroup, ChallengeGroupItem, Fact, ImportArtifact, ImportBatch, ImportCandidate, WorkerEvent, now_utc, new_id
from aurora.services.demo import create_project_with_bootstrap
from aurora.services.agent_runtime import agent_runtime_settings
from aurora.services.flag_prefix_config import _normalize_prefixes
from aurora.services.prompt_renderer import PromptRenderer
from aurora.services.network_proxy import network_proxy_registry
from aurora.services.tsecbench import TSecBenchClient, TSecBenchNeedsSession, TSecBenchError


MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024
MIN_VERIFIED_CANDIDATE_CONFIDENCE = 0.8
ATTACHMENT_EXTENSIONS = {".7z", ".bin", ".cap", ".gz", ".iso", ".jar", ".pcap", ".pcapng", ".pdf", ".rar", ".tar", ".tgz", ".txt", ".zip"}
FetchText = Callable[[str], tuple[str, str]]
FetchBytes = Callable[[str, int], tuple[bytes, str | None]]
FetchJson = Callable[[str, str | None], dict[str, Any]]
PostJson = Callable[[str, dict[str, Any], str | None], dict[str, Any]]
Cataloger = Callable[[dict[str, Any]], dict[str, Any]]
ProgressCallback = Callable[[str, str], None]
AttachmentCollector = Callable[[list[dict[str, Any]], str | None], None]


class AuthenticatedFetcher(Protocol):
    def fetch_text(self, url: str) -> tuple[str, str]: ...
    def fetch_bytes(self, url: str, max_bytes: int) -> tuple[bytes, str | None]: ...
    def fetch_json(self, url: str) -> dict[str, Any]: ...
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


@dataclass
class CollectionResult:
    inventory: dict[str, Any]
    candidates: list[dict[str, Any]]
    summary: str
    platform: str
    strategy: str
    pages_scanned: int = 1
    diagnostics: list[dict[str, Any]] | None = None


class HandsFreeService:
    """Collects challenge metadata and files without entering the Solver execution path."""

    def __init__(
        self,
        settings: Settings | None = None,
        fetch_text: FetchText | None = None,
        fetch_bytes: FetchBytes | None = None,
        fetch_json: FetchJson | None = None,
        post_json: PostJson | None = None,
        cataloger: Cataloger | None = None,
        authenticated_fetcher_factory: AuthenticatedFetcherFactory | None = None,
        ctfplus_attachment_collector: AttachmentCollector | None = None,
        tsecbench_client: TSecBenchClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.fetch_text = fetch_text or self._fetch_text
        self.fetch_bytes = fetch_bytes or self._fetch_bytes
        self.fetch_json = fetch_json or self._fetch_json
        self.post_json = post_json or self._post_json
        self.cataloger = cataloger
        self.authenticated_fetcher_factory = authenticated_fetcher_factory or self._authenticated_fetcher
        # Kept as a constructor alias for compatibility with existing callers;
        # the collector is now used for every platform, not only CTF+.
        self.attachment_collector = ctfplus_attachment_collector or self._collect_browser_attachments
        if tsecbench_client is not None:
            self.tsecbench_client = tsecbench_client
        elif fetch_json is not None:
            # Preserve the service's established injectable fetch_json seam for
            # deterministic tests and embedders. The production client still
            # uses its own HTTP implementation so it can set BENCHMARK_TOKEN.
            def _injected_tsecbench_request(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None, token: str | None = None) -> Any:
                if method == "GET" and payload is None:
                    return self.fetch_json(url, token)
                return self.post_json(url, payload or {}, token)
            self.tsecbench_client = TSecBenchClient(self.settings, request_json=_injected_tsecbench_request)
        else:
            self.tsecbench_client = TSecBenchClient(self.settings)

    def create_batch(self, session: Session, source_url: str, *, cookie: str | None = None, username: str | None = None, password: str | None = None) -> ImportBatch:
        source_url = self._safe_url(source_url)
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
            fetch_json: Callable[[str], dict[str, Any]] = lambda url: self.fetch_json(url, cookie)
            if self._auth_method(cookie, username, password):
                fetcher = self.authenticated_fetcher_factory(batch.source_url, cookie, username, password, login_url)
                fetch_text = fetcher.fetch_text
                fetch_bytes = fetcher.fetch_bytes
                authenticated_json = getattr(fetcher, "fetch_json", None)
                if callable(authenticated_json):
                    fetch_json = authenticated_json
            self._progress(progress, "DETECTING", "正在识别题目平台和数据来源。")
            tsecbench_url = self._is_tsecbench_url(batch.source_url)
            if tsecbench_url:
                final_url = self._safe_url(batch.source_url)
                inventory = {"page_url": final_url, "title": "TSecBench", "text": "", "links": [], "collected_links": []}
                self._progress(progress, "CATALOGING", "检测到 TSecBench，正在读取题目 API。")
                collection = self._tsecbench_collection(final_url, inventory)
            else:
                html, final_url = fetch_text(batch.source_url)
                final_url = self._safe_url(final_url)
                if self._looks_like_login_page(html, final_url):
                    raise NeedsSessionError("Login is required. Provide a valid Cookie to continue.", urlparse(final_url).hostname)
                inventory = self._inventory(html, final_url)
            if not tsecbench_url and self._is_ctfplus_problem_bank(final_url):
                self._progress(progress, "CATALOGING", "正在通过 CTF+ 题库 API 识别当前页题目。")
                inventory, candidates, model_result = self._ctfplus_problem_bank(final_url, cookie=cookie, inventory=inventory)
                collection = CollectionResult(inventory, candidates, str(model_result.get("summary", "")), "ctfplus", "platform_api", int(model_result.get("pages_scanned", 1)))
            elif not tsecbench_url and self._is_ctfd_page(html, inventory):
                self._progress(progress, "CATALOGING", "检测到 CTFd，正在读取题目 API 和详情。")
                collection = self._ctfd_collection(final_url, inventory, fetch_json)
            elif not tsecbench_url:
                self._progress(progress, "CATALOGING", "正在归集静态页面和浏览器响应。")
                collection = self._generic_collection(final_url, inventory, cookie=cookie, fetcher=fetcher, progress=progress)
            inventory = collection.inventory
            self._progress(progress, "VALIDATING", "正在验证题目身份、来源并去重。")
            candidates = self._deduplicate_candidates(collection.candidates)
            model_result = {"summary": collection.summary}
            if candidates and collection.platform != "tsecbench":
                self._progress(progress, "DETAILS", f"正在检查 {len(candidates)} 个题目详情页中的附件。")
                self._enrich_candidate_attachments(candidates, fetch_text)
            if candidates and collection.platform != "tsecbench":
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
            batch.platform = collection.platform
            batch.extraction_strategy = collection.strategy
            batch.pages_scanned = collection.pages_scanned
            batch.diagnostics_json = collection.diagnostics or ([] if candidates else [{"code": "NO_VERIFIED_CANDIDATES", "message": "未发现具有题目身份依据的候选；导航、静态资源和站点链接已被过滤。"}])
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

    def confirm(
        self,
        session: Session,
        batch_id: str,
        candidate_ids: list[str],
        name_overrides: dict[str, str] | None = None,
        flag_prefixes: list[str] | None = None,
    ) -> list[dict[str, str]]:
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
        normalized_flag_prefixes = _normalize_prefixes(flag_prefixes or []) if flag_prefixes is not None else None
        agent_runtime = agent_runtime_settings(session)
        group = ChallengeGroup(
            import_batch_id=batch_id,
            name=(batch.title or f"Imported batch {batch_id[-8:]}")[:240],
            limits={"max_iterations": 0, "max_minutes": 0, "no_progress_limit": 4, "stop_on_observer_escalate": True},
            flag_prefixes=normalized_flag_prefixes or None,
            # TSecBench targets are allocated lazily when an item is
            # dispatched.  The platform permits a small bounded pool rather
            # than requiring every imported challenge to own a target.
            max_concurrent=(
                max(1, int(self.settings.tsecbench_max_concurrent or 1))
                if batch.platform == "tsecbench"
                else 1
            ),
        )
        session.add(group)
        session.commit()
        session.refresh(group)
        results: list[dict[str, str]] = []
        for position, candidate in enumerate(candidates, start=1):
            if candidate.project_id:
                session.add(ChallengeGroupItem(group_id=group.id, project_id=candidate.project_id, position=position, competition_meta=candidate.source_metadata_json))
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
                multi_agent_exploration_enabled=(
                    batch.platform == "tsecbench" and agent_runtime.multi_agent_exploration_enabled
                ),
                max_parallel_explorers=agent_runtime.default_max_project_workers,
            )
            project.target_verification_status = "UNVERIFIED"
            project.target_verification_reason = "靶机为可选项；可继续分析题目与附件，也可稍后自动识别或人工注入"
            session.add(project)
            artifacts = self._attach_staged_artifacts(session, candidate, project.id)
            session.add(Fact(
                project_id=project.id,
                statement="Challenge source page and attachments were imported as evidence. Continue local analysis without a target; network actions require an explicitly discovered or manually injected target URL.",
                category="import",
                confidence=candidate.confidence,
                evidence_refs=artifacts,
            ))
            session.add(WorkerEvent(
                project_id=project.id,
                event_type="import.confirmed",
                payload_json={"batch_id": batch_id, "candidate_id": candidate.id, "source_url": batch.source_url, "source_metadata": candidate.source_metadata_json, "artifact_refs": artifacts},
            ))
            candidate.project_id = project.id
            candidate.confirmed = True
            candidate.updated_at = now_utc()
            session.add(candidate)
            session.add(ChallengeGroupItem(group_id=group.id, project_id=project.id, position=position, competition_meta=candidate.source_metadata_json))
            session.commit()
            results.append({"candidate_id": candidate.id, "project_id": project.id, "status": "created", "group_id": group.id, "challenge_url": candidate.challenge_url, "source_metadata": candidate.source_metadata_json})
        return results

    @staticmethod
    def _is_ctfplus_problem_bank(url: str) -> bool:
        parsed = urlparse(url)
        return (parsed.hostname or "").lower().rstrip(".") in {"ctfplus.cn", "www.ctfplus.cn"} and parsed.path.rstrip("/") == "/learning/problem/problem-bank"

    def _is_tsecbench_url(self, url: str) -> bool:
        parsed = urlparse(url)
        configured = urlparse(self.settings.tsecbench_base_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        configured_host = (configured.hostname or "").lower().rstrip(".")
        return parsed.path.rstrip("/").endswith("/openapi/v1/challenges") or bool(host and configured_host and host == configured_host)

    def _tsecbench_collection(self, source_url: str, inventory: dict[str, Any]) -> CollectionResult:
        try:
            challenges = self.tsecbench_client.list_challenges()
        except TSecBenchNeedsSession as exc:
            raise NeedsSessionError(str(exc), urlparse(source_url).hostname) from exc
        except TSecBenchError as exc:
            raise ValueError(str(exc)) from exc
        candidates: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        for challenge in challenges[: self.settings.cataloger_max_candidates]:
            metadata = {
                "platform": "tsecbench",
                "unique_code": challenge.unique_code,
                "difficulty": challenge.difficulty,
                "level": challenge.level,
                "points": challenge.points,
                "flag_count": challenge.flag_count,
                "correct_flag_count": challenge.correct_flag_count,
                "is_completed": challenge.is_completed,
                "container_status": challenge.container_status,
                "container_addr": challenge.container_addr,
                "provenance": "platform_api",
            }
            candidates.append({
                "title": challenge.title[:500],
                "description": challenge.description[:5000],
                "challenge_url": f"{self.settings.tsecbench_base_url.rstrip('/')}/openapi/v1/challenges?unique_code={quote(challenge.unique_code, safe='')}",
                "challenge_type": self._challenge_type(
                    f"{challenge.challenge_type or ''} {challenge.unique_code} {challenge.title} {challenge.description}",
                    [],
                ),
                "confidence": 1.0,
                "attachment_urls": [],
                "source_metadata": metadata,
            })
        inventory["title"] = "TSecBench"
        inventory["text"] = f"TSecBench challenge list ({len(candidates)})"
        return CollectionResult(inventory, candidates, f"TSecBench API identified {len(candidates)} challenge(s).", "tsecbench", "platform_api", 1, diagnostics)

    @staticmethod
    def _is_ctfd_page(html: str, inventory: dict[str, Any]) -> bool:
        sample = f"{html[:200_000]} {inventory.get('text', '')}"
        return bool(re.search(r"(?:Powered by CTFd|x-data=[\"']ChallengeBoard|/api/v1/challenges|ctfd\.io)", sample, re.IGNORECASE))

    def _ctfd_collection(
        self,
        source_url: str,
        inventory: dict[str, Any],
        fetch_json: Callable[[str], dict[str, Any]],
    ) -> CollectionResult:
        parsed = urlparse(source_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        list_url = f"{origin}/api/v1/challenges"
        payload = fetch_json(list_url)
        if not payload.get("success") or not isinstance(payload.get("data"), list):
            message = str(payload.get("message") or payload.get("errors") or "CTFd challenge API returned an invalid response")
            if re.search(r"login|auth|permission|unauthorized|forbidden", message, re.IGNORECASE):
                raise NeedsSessionError("CTFd requires a valid login session. Provide a valid Cookie to continue.", parsed.hostname)
            raise ValueError(message)
        candidates: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        for raw in payload["data"][: self.settings.cataloger_max_candidates]:
            if not isinstance(raw, dict) or raw.get("id") is None or not str(raw.get("name") or "").strip():
                continue
            challenge_id = str(raw["id"])
            detail_url = f"{origin}/api/v1/challenges/{challenge_id}"
            detail = raw
            try:
                detail_payload = fetch_json(detail_url)
                if detail_payload.get("success") and isinstance(detail_payload.get("data"), dict):
                    detail = {**raw, **detail_payload["data"]}
                else:
                    diagnostics.append({"code": "DETAIL_UNAVAILABLE", "challenge_id": challenge_id, "message": "CTFd detail API did not return a usable object."})
            except NeedsSessionError:
                raise
            except Exception as exc:
                diagnostics.append({"code": "DETAIL_UNAVAILABLE", "challenge_id": challenge_id, "message": str(exc)[:300]})
            candidate = self._ctfd_candidate(detail, source_url, detail_url)
            if candidate is not None:
                candidates.append(candidate)
        summary = f"CTFd API identified {len(candidates)} challenge(s) visible in the current session."
        return CollectionResult(inventory, candidates, summary, "ctfd", "platform_api", 1, diagnostics)

    @classmethod
    def _ctfd_candidate(cls, item: dict[str, Any], source_url: str, detail_url: str) -> dict[str, Any] | None:
        challenge_id = str(item.get("id") or "").strip()
        title = str(item.get("name") or "").strip()
        if not challenge_id or not title:
            return None
        category = str(item.get("category") or "unknown")
        file_attachments: list[str] = []
        for raw in item.get("files", []) if isinstance(item.get("files"), list) else []:
            value = raw if isinstance(raw, str) else next((raw.get(key) for key in ("url", "location", "path") if isinstance(raw, dict) and raw.get(key)), None)
            if isinstance(value, str) and value.strip():
                try:
                    file_attachments.append(cls._safe_url(urljoin(source_url, value)))
                except ValueError:
                    continue
        description_value = str(item.get("description") or item.get("view") or "")
        description_attachments = cls._ctfd_description_attachment_urls(description_value, source_url)
        attachments = list(dict.fromkeys([*file_attachments, *description_attachments]))
        trusted_external = list(dict.fromkeys([
            *file_attachments,
            *(url for url in description_attachments if cls._is_direct_description_attachment(url)),
        ]))
        tags = [str(tag.get("value") or tag.get("name")) for tag in item.get("tags", []) if isinstance(tag, dict) and (tag.get("value") or tag.get("name"))]
        description = cls._plain_text(description_value)
        details = [description]
        if item.get("value") is not None:
            details.append(f"Points: {item['value']}")
        if tags:
            details.append(f"Tags: {', '.join(tags)}")
        return {
            "title": title[:500],
            "description": "\n".join(part for part in details if part)[:3000],
            "challenge_url": f"{source_url.split('#', 1)[0]}#challenge-{challenge_id}",
            "challenge_type": cls._challenge_type(category, tags),
            "confidence": 1.0,
            "attachment_urls": attachments,
            # CTFd challenge details are authoritative input. Exact external URLs
            # can be fetched without forwarding the platform session to the host.
            "_trusted_external_attachment_urls": trusted_external,
            "source_metadata": {
                "platform": "ctfd",
                "challenge_id": challenge_id,
                "detail_api_url": detail_url,
                "page_url": source_url.split("#", 1)[0],
                "locator": {"selector": f'button.challenge-button[value="{challenge_id}"]'},
                "provenance": "platform_api",
            },
        }

    @classmethod
    def _ctfd_description_attachment_urls(cls, description: str, source_url: str) -> list[str]:
        """Extract links that CTFd challenge authors placed in an Attachments section."""
        if not description:
            return []
        heading_pattern = re.compile(
            r"(?im)(?:^|\s)(?:#{1,6}\s*([^\n<]+)|<h[1-6][^>]*>\s*(.*?)\s*</h[1-6]>)"
        )
        headings = [
            (match.start(), cls._plain_text(match.group(1) or match.group(2) or "").strip().lower())
            for match in heading_pattern.finditer(description)
        ]
        link_pattern = re.compile(
            r"\[([^\]]{1,300})\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+['\"][^)]*['\"])?\s*\)"
            r"|<a\b[^>]*\bhref\s*=\s*(['\"])(.*?)\4[^>]*>(.*?)</a>",
            re.IGNORECASE | re.DOTALL,
        )
        urls: list[str] = []
        for match in link_pattern.finditer(description):
            label = cls._plain_text(match.group(1) or match.group(6) or "")
            raw_url = match.group(2) or match.group(3) or match.group(5) or ""
            preceding = [heading for heading in headings if heading[0] < match.start()]
            section = preceding[-1][1] if preceding else ""
            if not re.search(r"\battachments?\b|附件|下载", section, re.IGNORECASE) and not re.search(
                r"\battachments?\b|download|附件|下载", label, re.IGNORECASE
            ):
                continue
            try:
                urls.append(cls._safe_url(urljoin(source_url, raw_url.strip())))
            except ValueError:
                continue
        return list(dict.fromkeys(urls))

    @staticmethod
    def _is_direct_description_attachment(url: str) -> bool:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        suffix = Path(parsed.path).suffix.lower()
        if suffix and suffix not in {".htm", ".html", ".php", ".asp", ".aspx", ".jsp"}:
            return True
        return hostname == "amazonaws.com" or hostname.endswith(".amazonaws.com")

    def _generic_collection(
        self,
        source_url: str,
        inventory: dict[str, Any],
        *,
        cookie: str | None,
        fetcher: AuthenticatedFetcher | None,
        progress: ProgressCallback | None,
    ) -> CollectionResult:
        diagnostics: list[dict[str, Any]] = []
        if self.cataloger is not None or self.settings.cataloger_configured:
            result = self._catalog(inventory)
            candidates = self._validated_candidates(result, inventory)
            if candidates:
                return CollectionResult(inventory, candidates, str(result.get("summary", "")), "generic", "cataloger_static", 1, diagnostics)
            diagnostics.append({"code": "STATIC_CATALOG_EMPTY", "message": str(result.get("summary") or "Cataloger found no evidence-backed static candidates.")[:500]})
        else:
            diagnostics.append({"code": "CATALOGER_UNAVAILABLE", "message": "Cataloger LLM is not configured; deterministic collection remains available."})

        self._progress(progress, "BROWSING", "正在通过隔离浏览器检查动态数据。")
        collector = getattr(fetcher, "collect_catalog", None) if fetcher is not None else None
        browser_result = collector(self) if callable(collector) else self._browser_collection(source_url, cookie)
        if browser_result is not None:
            browser_result.diagnostics = [*diagnostics, *(browser_result.diagnostics or [])]
            return browser_result
        diagnostics.append({"code": "BROWSER_UNAVAILABLE", "message": "Playwright browser collection was unavailable or failed."})
        return CollectionResult(inventory, [], "No verified challenge candidates were found.", "generic", "deterministic_only", 1, diagnostics)

    def _browser_collection(self, source_url: str, cookie: str | None) -> CollectionResult | None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None
        try:
            with sync_playwright() as playwright:
                proxy = network_proxy_registry.get().playwright_proxy()
                browser = playwright.chromium.launch(headless=True, **({"proxy": proxy} if proxy else {}))
                context = browser.new_context()
                try:
                    if cookie and cookie.strip():
                        context.add_cookies(self._parse_browser_cookie(cookie, urlparse(source_url).hostname or ""))
                    return self._browser_collection_context(context, source_url)
                finally:
                    context.close()
                    browser.close()
        except Exception:
            return None

    def _browser_collection_context(self, context: Any, source_url: str) -> CollectionResult:
        page = context.new_page()
        responses: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        source_host = (urlparse(source_url).hostname or "").lower().rstrip(".")

        def capture(response: Any) -> None:
            try:
                response_host = (urlparse(response.url).hostname or "").lower().rstrip(".")
                content_type = str(response.headers.get("content-type") or "").lower()
                if response_host != source_host or "json" not in content_type:
                    return
                body = response.body()
                if len(body) > self.settings.cataloger_max_response_bytes:
                    diagnostics.append({"code": "RESPONSE_SKIPPED", "url": response.url, "message": "JSON response exceeded collection limit."})
                    return
                parsed = json.loads(body.decode("utf-8", errors="replace"))
                responses.append({"id": f"response_{len(responses)}", "url": response.url, "status": response.status, "body": parsed})
            except Exception:
                return

        page.on("response", capture)
        try:
            page.goto(source_url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1_500)
            inventory = self._inventory(page.content(), self._safe_url(page.url))
            candidates = self._observed_json_candidates(responses, inventory["page_url"])
            if candidates:
                return CollectionResult(inventory, candidates, f"Browser responses identified {len(candidates)} challenge(s).", "generic", "observed_json", 1, diagnostics)
            if not self.settings.cataloger_agent_enabled or (self.cataloger is None and not self.settings.cataloger_configured):
                return CollectionResult(inventory, [], "Browser collection found no verified challenge objects.", "generic", "browser_observation", 1, diagnostics)
            candidates, pages, trace = self._run_cataloger_agent(page, responses, inventory)
            diagnostics.extend(trace)
            return CollectionResult(inventory, candidates, f"Cataloger Agent identified {len(candidates)} verified challenge(s).", "generic", "browser_agent", pages, diagnostics)
        finally:
            page.close()

    def _run_cataloger_agent(self, page: Any, responses: list[dict[str, Any]], inventory: dict[str, Any]) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
        trace: list[dict[str, Any]] = []
        pages = 1
        for step in range(self.settings.cataloger_max_agent_steps):
            elements = self._agent_elements(page)
            observation = {
                **inventory,
                "agent_mode": True,
                "agent_elements": elements,
                "observed_json": [{"id": item["id"], "url": item["url"], "body": item["body"]} for item in responses[-20:]],
                "agent_contract": "Return candidates backed by challenge_url from collected_links, or one action using an element_ref. Never return a raw navigation URL.",
            }
            result = self._catalog(observation)
            candidates = self._validated_candidates(result, inventory)
            if candidates:
                return candidates, pages, trace
            action = result.get("action") if isinstance(result.get("action"), dict) else None
            if not action or action.get("type") == "finish":
                trace.append({"code": "AGENT_FINISHED", "step": step + 1, "message": str(result.get("summary") or "Agent finished without verified candidates.")[:500]})
                break
            element_ref = str(action.get("element_ref") or "")
            element = next((item for item in elements if item["ref"] == element_ref), None)
            if action.get("type") not in {"click", "navigate", "scroll"} or (action.get("type") != "scroll" and not self._agent_control_allowed(element)):
                trace.append({"code": "AGENT_ACTION_DENIED", "step": step + 1, "action": action.get("type"), "element_ref": element_ref})
                break
            before_url = page.url
            if action["type"] == "scroll":
                page.mouse.wheel(0, 1200)
            else:
                controls = page.locator("a, button, [role='button']")
                controls.nth(int(element["index"])).click(timeout=10_000)
            page.wait_for_timeout(1_000)
            inventory = self._inventory(page.content(), self._safe_url(page.url))
            pages += 1 if page.url != before_url or action["type"] == "click" else 0
            trace.append({"code": "AGENT_ACTION", "step": step + 1, "action": action["type"], "element_ref": element_ref})
            candidates = self._observed_json_candidates(responses, inventory["page_url"])
            if candidates:
                return candidates, min(pages, self.settings.cataloger_max_pages), trace
            if pages >= self.settings.cataloger_max_pages:
                trace.append({"code": "PAGE_LIMIT_REACHED", "pages": pages})
                break
        return [], min(pages, self.settings.cataloger_max_pages), trace

    @staticmethod
    def _agent_elements(page: Any) -> list[dict[str, Any]]:
        controls = page.locator("a, button, [role='button']")
        elements: list[dict[str, Any]] = []
        for index in range(min(controls.count(), 200)):
            control = controls.nth(index)
            try:
                text = " ".join((control.inner_text(timeout=1_000) or control.get_attribute("aria-label") or "").split())[:300]
                href = control.get_attribute("href") or ""
                control_type = control.get_attribute("type") or ""
                elements.append({"ref": f"element_{index}", "index": index, "text": text, "href": href[:1000], "type": control_type})
            except Exception:
                continue
        return elements

    @staticmethod
    def _agent_control_allowed(element: dict[str, Any] | None) -> bool:
        if not element:
            return False
        label = f"{element.get('text', '')} {element.get('href', '')}".lower()
        if element.get("type", "").lower() == "submit" or re.search(r"submit|solve|flag|login|register|start|launch|create instance|启动|提交|登录|注册|创建实例|启动靶机", label):
            return False
        return bool(re.search(r"next|previous|page|filter|category|challenge|task|problem|more|detail|下一|上一|分页|筛选|分类|题目|详情", label))

    def _observed_json_candidates(self, responses: list[dict[str, Any]], page_url: str) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for response in responses:
            endpoint_signal = bool(re.search(r"challenge|task|problem", response["url"], re.IGNORECASE))
            for item in self._json_objects(response["body"]):
                challenge_id = item.get("id")
                title = item.get("name") or item.get("title")
                evidence_signal = any(key in item for key in ("category", "description", "value", "points", "solves", "files", "attachments", "tags"))
                if challenge_id is None or not isinstance(title, str) or not title.strip() or not (endpoint_signal or evidence_signal):
                    continue
                attachments = self._json_attachment_urls(item, page_url)
                category = str(item.get("category") or item.get("type") or "unknown")
                candidates.append({
                    "title": title.strip()[:500],
                    "description": self._plain_text(str(item.get("description") or ""))[:3000],
                    "challenge_url": f"{page_url.split('#', 1)[0]}#challenge-{challenge_id}",
                    "challenge_type": self._challenge_type(category, []),
                    "confidence": 0.9,
                    "attachment_urls": attachments,
                    "source_metadata": {"platform": "generic", "challenge_id": str(challenge_id), "response_url": response["url"], "page_url": page_url, "provenance": "observed_json"},
                })
                if len(candidates) >= self.settings.cataloger_max_candidates:
                    return self._deduplicate_candidates(candidates)
        return self._deduplicate_candidates(candidates)

    @classmethod
    def _json_objects(cls, value: Any) -> list[dict[str, Any]]:
        objects: list[dict[str, Any]] = []
        if isinstance(value, dict):
            objects.append(value)
            for child in value.values():
                objects.extend(cls._json_objects(child))
        elif isinstance(value, list):
            for child in value:
                objects.extend(cls._json_objects(child))
        return objects

    @classmethod
    def _json_attachment_urls(cls, item: dict[str, Any], page_url: str) -> list[str]:
        values = item.get("files", item.get("attachments", []))
        if not isinstance(values, list):
            return []
        urls: list[str] = []
        for raw in values:
            value = raw if isinstance(raw, str) else next((raw.get(key) for key in ("url", "downloadUrl", "location", "path") if isinstance(raw, dict) and raw.get(key)), None)
            if isinstance(value, str):
                try:
                    urls.append(cls._safe_url(urljoin(page_url, value)))
                except ValueError:
                    continue
        return list(dict.fromkeys(urls))

    @staticmethod
    def _plain_text(value: str) -> str:
        return " ".join(re.sub(r"<[^>]+>", " ", value).split())

    @staticmethod
    def _challenge_type(category: str, tags: list[str]) -> str:
        text = " ".join([category, *tags]).lower()
        patterns = (
            (r"\bweb\b|网站|网页|登录|sql注入|xss|ssrf|csrf|命令注入|文件上传", "web"),
            (r"\bpwn\b|binary exploitation|栈溢出|堆利用|格式化字符串|\brop\b", "pwn"),
            (r"crypto|密码学|加密|解密|密文|签名算法", "crypto"),
            (r"reverse|reversing|\bre\b|逆向|反编译|固件|恶意代码分析", "reverse"),
            (r"forensic|取证|流量分析|内存分析|日志分析", "forensics"),
            (r"misc|osint|steg|杂项|隐写|开源情报", "misc"),
        )
        for pattern, challenge_type in patterns:
            if re.search(pattern, text):
                return challenge_type
        return "unknown"

    @staticmethod
    def _deduplicate_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        result: list[dict[str, Any]] = []
        for candidate in candidates:
            metadata = candidate.get("source_metadata") if isinstance(candidate.get("source_metadata"), dict) else {}
            identity = (str(metadata.get("platform") or "url"), str(metadata.get("unique_code") or metadata.get("challenge_id") or candidate.get("challenge_url") or ""))
            if not identity[1] or identity in seen:
                continue
            seen.add(identity)
            result.append(candidate)
        return result

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
        origin = f"{parsed.scheme}://{parsed.netloc}"
        candidates: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        pages_scanned = 0
        total = 0
        current_page = page
        while pages_scanned < self.settings.cataloger_max_pages and len(candidates) < self.settings.cataloger_max_candidates:
            request_payload = {**payload, "page": {**payload["page"], "page": current_page}}
            response = self.post_json(endpoint, request_payload, cookie)
            if int(response.get("code", 200)) != 200:
                message = str(response.get("msg") or "CTF+ problem API request failed")
                if re.search(r"login|auth|token|登录|未登录|权限", message, re.IGNORECASE):
                    raise NeedsSessionError("CTF+ requires a valid login session. Provide a valid Cookie to continue.", parsed.hostname)
                raise ValueError(message)
            data = response.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("problems"), list):
                raise ValueError("CTF+ problem API returned an invalid problem list")
            pages_scanned += 1
            total = int(data.get("total", total or len(data["problems"])))
            new_count = 0
            for problem in data["problems"]:
                if not isinstance(problem, dict):
                    continue
                problem_id = str(problem.get("id") or "")
                if not problem_id or problem_id in seen_ids:
                    continue
                candidate = self._ctfplus_candidate(problem, origin)
                if candidate is not None:
                    seen_ids.add(problem_id)
                    candidates.append(candidate)
                    new_count += 1
            if new_count == 0 or (total > 0 and len(candidates) >= total):
                break
            current_page += 1
        tag_label = "、".join(payload["tags"][:3])
        inventory = {**inventory, "title": f"CTF+ 题库{f' · {tag_label}' if tag_label else ''}", "text": f"CTF+ API 识别：从第 {page} 页开始扫描 {pages_scanned} 页，共 {total or len(candidates)} 道匹配题目。", "links": [], "collected_links": []}
        summary = f"CTF+ API identified {len(candidates)} problem(s) across {pages_scanned} page(s) starting at page {page} (page size {size}; total {total or len(candidates)})."
        return inventory, candidates, {"summary": summary, "pages_scanned": pages_scanned}

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
        return {
            "title": title[:500],
            "description": "\n".join(part for part in details if part)[:3000],
            "challenge_url": f"{origin}/learning/problem/problem-detail/{problem_id}/description",
            "challenge_type": cls._ctfplus_problem_type(tags),
            "confidence": 0.98,
            "attachment_urls": cls._ctfplus_attachment_urls(problem.get("attachments"), origin),
            "source_metadata": {"platform": "ctfplus", "challenge_id": problem_id, "provenance": "platform_api"},
        }

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
                proxy = network_proxy_registry.get().playwright_proxy()
                browser = playwright.chromium.launch(headless=True, **({"proxy": proxy} if proxy else {}))
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
            with build_opener(network_proxy_registry.get().urllib_proxy_handler()).open(request, timeout=self.settings.cataloger_llm_timeout_seconds) as response:
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
            if not isinstance(raw, dict) or raw.get("challenge_url") not in valid_links or not self._is_verified_challenge_link(raw["challenge_url"], inventory, links_by_url.get(raw["challenge_url"])):
                continue
            try:
                confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
            except (TypeError, ValueError):
                continue
            if confidence < MIN_VERIFIED_CANDIDATE_CONFIDENCE:
                continue
            attachments = [url for url in raw.get("attachment_urls", []) if isinstance(url, str) and url in valid_links and self._is_attachment_link(links_by_url[url])]
            title = str(raw.get("title") or raw["challenge_url"]).strip()
            if not title:
                continue
            candidates.append({
                "title": title[:500], "description": str(raw.get("description") or "")[:3000],
                "challenge_url": raw["challenge_url"], "challenge_type": str(raw.get("challenge_type") or "unknown")[:80],
                "confidence": confidence, "attachment_urls": attachments,
                "source_metadata": {"platform": "generic", "provenance": "cataloger", "page_url": inventory["page_url"]},
            })
        return candidates

    @classmethod
    def _is_verified_challenge_link(cls, url: str, inventory: dict[str, Any], link: dict[str, Any] | None) -> bool:
        parsed = urlparse(url)
        page = urlparse(inventory["page_url"])
        if not parsed.hostname or not page.hostname or not cls._same_domain(inventory["page_url"], url):
            return False
        if url.split("#", 1)[0].rstrip("/") == inventory["page_url"].split("#", 1)[0].rstrip("/"):
            return False
        path = parsed.path.lower()
        if Path(path).suffix.lower() in {".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".map"}:
            return False
        text = f"{(link or {}).get('text', '')} {path}".lower()
        return bool(re.search(r"challenge|task|problem|题目|赛题|/detail(?:/|$)|/challenges?/[^/]+", text))

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
            proxy = network_proxy_registry.get().playwright_proxy()
            browser = playwright.chromium.launch(headless=True, **({"proxy": proxy} if proxy else {}))
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
        navigation_timeout = max(1, self.settings.browser_navigation_timeout_seconds) * 1_000
        action_timeout = max(1, self.settings.browser_action_timeout_seconds) * 1_000
        attachment_timeout = max(1, self.settings.cataloger_attachment_timeout_seconds) * 1_000
        try:
            response = page.goto(candidate["challenge_url"], wait_until="domcontentloaded", timeout=navigation_timeout)
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
                label = (control.get_attribute("aria-label") or control.inner_text(timeout=action_timeout) or control.get_attribute("download") or "attachment.bin").strip()
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
                    with page.expect_download(timeout=attachment_timeout) as event:
                        control.click(timeout=action_timeout)
                    download = event.value
                    data, mime_type, source_url = self._read_browser_download(context, download)
                    filename = download.suggested_filename or label
                    candidate.setdefault("_downloaded_attachments", []).append({"filename": filename, "data": data, "mime_type": mime_type or mimetypes.guess_type(filename)[0], "source_url": source_url})
                except Exception as exc:
                    candidate.setdefault("_attachment_issues", []).append({"filename": label, "status": "download_failed", "reason": str(exc)[:300]})
        except Exception as exc:
            candidate.setdefault("_attachment_issues", []).append({"status": "download_failed", "reason": f"detail attachment inspection failed: {exc}"[:300]})
        finally:
            page.close()

    def _read_browser_download(self, context: Any, download: Any) -> tuple[bytes, str | None, str]:
        """Read the original browser download with a hard completion deadline."""
        timeout = max(1, self.settings.cataloger_attachment_timeout_seconds)
        deadline = time.monotonic() + timeout
        source_url = str(download.url)
        artifact = getattr(getattr(download, "_impl_obj", None), "_artifact", None)
        absolute_path = getattr(artifact, "absolute_path", None)

        # Chromium writes to a temporary .crdownload and only exposes this
        # final artifact path after completion. Polling it avoids the unbounded
        # wait in download.path() while preserving one-shot, POST and blob URLs.
        if absolute_path:
            path = Path(str(absolute_path))
            while time.monotonic() < deadline:
                if path.is_file():
                    with path.open("rb") as handle:
                        data = handle.read(MAX_ATTACHMENT_BYTES + 1)
                    if len(data) > MAX_ATTACHMENT_BYTES:
                        raise ValueError("attachment exceeds size limit")
                    return data, mimetypes.guess_type(download.suggested_filename or "")[0], source_url
                time.sleep(0.05)
            try:
                download.cancel()
            except Exception:
                pass
            raise TimeoutError(f"browser attachment download timed out after {timeout} seconds")

        # Remote Playwright connections do not expose a local artifact path.
        # Re-fetch there as a compatibility fallback, retaining browser cookies
        # and a Referer when one is available.
        source_url = self._safe_url(source_url)
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1_000))
        page = getattr(download, "page", None)
        referer = str(getattr(page, "url", "") or "")
        headers = {"Accept": "application/octet-stream,*/*"}
        if referer.startswith(("http://", "https://")):
            headers["Referer"] = referer
        try:
            response = context.request.get(source_url, timeout=remaining_ms, max_redirects=5, headers=headers)
            final_url = self._safe_url(str(response.url))
            if not response.ok:
                raise ValueError(f"attachment request failed with HTTP {response.status}")
            content_length = response.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > MAX_ATTACHMENT_BYTES:
                raise ValueError("attachment exceeds size limit")
            data = response.body()
            if len(data) > MAX_ATTACHMENT_BYTES:
                raise ValueError("attachment exceeds size limit")
            return data, response.headers.get("content-type"), final_url
        finally:
            try:
                download.cancel()
            except Exception:
                pass

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
        # Navigation text alone is not proof that a URL represents a challenge.
        # Keep the method for compatibility with older callers, but never emit
        # speculative candidates.
        return []

    def _persist_candidates(self, session: Session, batch: ImportBatch, candidates: list[dict[str, Any]], inventory: dict[str, Any], fetch_bytes: FetchBytes) -> list[ImportCandidate]:
        persisted: list[ImportCandidate] = []
        for item in candidates:
            staged, external = self._stage_attachments(
                session,
                batch,
                inventory["page_url"],
                item["attachment_urls"],
                fetch_bytes,
                trusted_external_urls=item.get("_trusted_external_attachment_urls", []),
            )
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
                external_attachments_json=external, evidence_json=[{"source_url": inventory["page_url"], "title": inventory["title"], "provenance": (item.get("source_metadata") or {}).get("provenance")}],
                source_metadata_json=item.get("source_metadata") if isinstance(item.get("source_metadata"), dict) else {},
            )
            session.add(candidate)
            persisted.append(candidate)
        session.commit()
        for candidate in persisted:
            session.refresh(candidate)
        return persisted

    def _stage_attachments(
        self,
        session: Session,
        batch: ImportBatch,
        page_url: str,
        urls: list[str],
        fetch_bytes: FetchBytes | None = None,
        *,
        trusted_external_urls: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        staged: list[dict[str, Any]] = []
        external: list[dict[str, Any]] = []
        trusted_external = set(trusted_external_urls or [])
        for url in dict.fromkeys(urls):
            same_domain = self._same_domain(page_url, url)
            if not same_domain and url not in trusted_external:
                external.append({"url": url, "status": "external_review_required"})
                continue
            try:
                # Authenticated fetchers carry platform cookies, so external CDN
                # downloads always use the service's unauthenticated downloader.
                downloader = (fetch_bytes or self.fetch_bytes) if same_domain else self.fetch_bytes
                if getattr(downloader, "__self__", None) is self and getattr(downloader, "__func__", None) is HandsFreeService._fetch_bytes:
                    downloader = lambda target, limit: self._fetch_bytes(target, limit, referer=page_url)
                data, mime_type = self._download_with_timeout(downloader, url, MAX_ATTACHMENT_BYTES)
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
                reason = str(exc)[:300]
                status = "external_review_required" if "exceeds size limit" in reason else "download_failed"
                external.append({"url": url, "status": status, "reason": reason})
        return staged, external

    def _download_with_timeout(self, downloader: FetchBytes, url: str, max_bytes: int) -> tuple[bytes, str | None]:
        """Bound the total wall-clock time of an attachment reader."""
        owner = getattr(downloader, "__self__", None)
        if isinstance(owner, _PlaywrightAuthenticatedFetcher):
            # The synchronous Playwright API is thread-affine. Its request gets
            # the same configured timeout in fetch_bytes().
            return downloader(url, max_bytes)

        timeout = max(1, self.settings.cataloger_attachment_timeout_seconds)
        results: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                results.put((True, downloader(url, max_bytes)))
            except Exception as exc:
                results.put((False, exc))

        worker = threading.Thread(target=run, daemon=True, name="cataloger-attachment-download")
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            raise TimeoutError(f"attachment download timed out after {timeout} seconds")
        succeeded, result = results.get_nowait()
        if not succeeded:
            if isinstance(result, Exception):
                raise result
            raise RuntimeError("attachment download failed without an error")
        if not isinstance(result, tuple) or len(result) != 2:
            raise ValueError("attachment downloader returned an invalid response")
        return result

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
            artifact = Artifact(project_id=project_id, type="imported_attachment", path=imported.path, sha256=imported.sha256, mime_type=imported.mime_type, size=imported.size, summary=f"Imported attachment: {imported.filename}", origin_kind="challenge_input")
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

    def _authenticated_fetcher(self, source_url: str, cookie: str | None, username: str | None, password: str | None, login_url: str | None) -> AuthenticatedFetcher:
        return _PlaywrightAuthenticatedFetcher(source_url, cookie, username, password, login_url, self.settings.cataloger_attachment_timeout_seconds)

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
        with build_opener(network_proxy_registry.get().urllib_proxy_handler()).open(request, timeout=20) as response:
            return response.read(2 * 1024 * 1024).decode(response.headers.get_content_charset() or "utf-8", errors="replace"), response.geturl()

    @staticmethod
    def _post_json(url: str, payload: dict[str, Any], cookie: str | None) -> dict[str, Any]:
        headers = {"User-Agent": "Aurora-Cataloger/1.0", "Content-Type": "application/json"}
        if cookie and cookie.strip():
            headers["Cookie"] = cookie.strip().removeprefix("Cookie:").strip()
        request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        try:
            with build_opener(network_proxy_registry.get().urllib_proxy_handler()).open(request, timeout=20) as response:
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
    def _fetch_json(url: str, cookie: str | None) -> dict[str, Any]:
        headers = {"User-Agent": "Aurora-Cataloger/1.0", "Accept": "application/json"}
        if cookie and cookie.strip():
            headers["Cookie"] = cookie.strip().removeprefix("Cookie:").strip()
        request = Request(url, headers=headers)
        try:
            with build_opener(network_proxy_registry.get().urllib_proxy_handler()).open(request, timeout=20) as response:
                body = json.loads(response.read(2 * 1024 * 1024).decode(response.headers.get_content_charset() or "utf-8"))
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise NeedsSessionError("The platform API requires a valid login session.", urlparse(url).hostname) from exc
            raise ValueError(f"platform API request failed with HTTP {exc.code}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ValueError(f"platform API request failed: {exc}") from exc
        if not isinstance(body, dict):
            raise ValueError("platform API returned an invalid response")
        return body

    def _fetch_bytes(self, url: str, max_bytes: int, *, referer: str | None = None) -> tuple[bytes, str | None]:
        class _SafeRedirectHandler(HTTPRedirectHandler):
            def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request | None:
                HandsFreeService._safe_url(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        HandsFreeService._safe_url(url)
        headers = {"User-Agent": "Aurora-Cataloger/1.0", "Accept": "application/octet-stream,*/*"}
        if referer:
            headers["Referer"] = referer
        opener = build_opener(network_proxy_registry.get().urllib_proxy_handler(), _SafeRedirectHandler())
        request = Request(url, headers=headers)
        timeout = max(1, self.settings.cataloger_attachment_timeout_seconds)
        for attempt in range(2):
            try:
                with opener.open(request, timeout=timeout) as response:
                    HandsFreeService._safe_url(response.geturl())
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None and int(content_length) > max_bytes:
                        raise ValueError("attachment exceeds size limit")
                    data = response.read(max_bytes + 1)
                    if len(data) > max_bytes:
                        raise ValueError("attachment exceeds size limit")
                    return data, response.headers.get_content_type()
            except HTTPError as exc:
                if attempt == 0 and (exc.code == 429 or 500 <= exc.code < 600):
                    time.sleep(0.1)
                    continue
                raise
            except (URLError, TimeoutError, OSError):
                if attempt == 0:
                    time.sleep(0.1)
                    continue
                raise
        raise RuntimeError("attachment download failed")

    @staticmethod
    def open_external_attachment(url: str):
        class _SafeRedirectHandler(HTTPRedirectHandler):
            def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request | None:
                HandsFreeService._safe_url(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        url = HandsFreeService._safe_url(url)
        opener = build_opener(network_proxy_registry.get().urllib_proxy_handler(), _SafeRedirectHandler())
        response = opener.open(Request(url, headers={"User-Agent": "Aurora-Attachment-Review/1.0"}), timeout=30)
        HandsFreeService._safe_url(response.geturl())
        return response


class _PlaywrightAuthenticatedFetcher:
    """Ephemeral authenticated browser context. Credentials never leave this object."""

    def __init__(self, source_url: str, cookie: str | None, username: str | None, password: str | None, login_url: str | None, attachment_timeout_seconds: int = 20) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ValueError("authenticated imports require Playwright; install the browser runtime first") from exc
        self.source_url = HandsFreeService._safe_url(source_url)
        self.source_host = urlparse(self.source_url).hostname or ""
        self.attachment_timeout_ms = max(1, attachment_timeout_seconds) * 1_000
        self._playwright = None
        self._browser = None
        self._context = None
        try:
            self._playwright = sync_playwright().start()
            proxy = network_proxy_registry.get().playwright_proxy()
            self._browser = self._playwright.chromium.launch(headless=True, **({"proxy": proxy} if proxy else {}))
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
        response = self._context.request.get(
            url,
            timeout=self.attachment_timeout_ms,
            max_redirects=5,
            headers={"Accept": "application/octet-stream,*/*", "Referer": self.source_url},
        )
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

    def fetch_json(self, url: str) -> dict[str, Any]:
        response = self._context.request.get(url, timeout=30_000, max_redirects=5, headers={"Accept": "application/json"})
        final_url = HandsFreeService._safe_url(response.url)
        if not HandsFreeService._same_domain(self.source_url, final_url):
            raise ValueError("platform API redirected outside the source domain")
        if response.status in {401, 403}:
            raise NeedsSessionError("The login session does not have access to the platform API.", urlparse(final_url).hostname)
        if not response.ok:
            raise ValueError(f"platform API request failed with HTTP {response.status}")
        try:
            body = response.json()
        except Exception as exc:
            raise ValueError("platform API returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise ValueError("platform API returned an invalid response")
        return body

    def collect_attachments(self, candidates: list[dict[str, Any]], collector: Callable[[Any, dict[str, Any]], None]) -> None:
        """Run browser attachment collection in the authenticated import context."""
        for candidate in candidates:
            collector(self._context, candidate)

    def collect_catalog(self, service: HandsFreeService) -> CollectionResult:
        return service._browser_collection_context(self._context, self.source_url)

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
