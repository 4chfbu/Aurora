from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from threading import RLock
from urllib.parse import urlparse
from urllib.request import ProxyHandler

from sqlmodel import Session

from aurora.models import NetworkProxySetting, now_utc


PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
REQUIRED_NO_PROXY = ("127.0.0.1", "localhost", "aurora-cc-switch", "host.docker.internal")


@dataclass(frozen=True)
class NetworkProxyConfig:
    mode: str = "system"
    proxy_url: str | None = None
    no_proxy: str = ",".join(REQUIRED_NO_PROXY)

    def public_dict(self) -> dict[str, str | None]:
        return asdict(self)

    def environment(self) -> dict[str, str]:
        if self.mode == "direct":
            return {key: "" for key in PROXY_ENV_KEYS}
        if self.mode == "system":
            values = {key: os.environ[key] for key in PROXY_ENV_KEYS if os.environ.get(key)}
            inherited_no_proxy = values.get("NO_PROXY") or values.get("no_proxy") or ""
            merged = ",".join(dict.fromkeys([
                *REQUIRED_NO_PROXY,
                *[part.strip() for part in inherited_no_proxy.split(",") if part.strip()],
                *[part.strip() for part in self.no_proxy.split(",") if part.strip()],
            ]))
            values["NO_PROXY"] = merged
            values["no_proxy"] = merged
            return values
        proxy_url = self.proxy_url or ""
        return {
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "NO_PROXY": self.no_proxy,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            "no_proxy": self.no_proxy,
        }

    def container_environment(self) -> dict[str, str]:
        """Render proxy variables from the container's network namespace."""
        values = self.environment()
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            if values.get(key) and _is_loopback_proxy(values[key]):
                # A host proxy bound only to loopback cannot be reached through
                # the Docker bridge gateway.  Direct egress is preferable to
                # injecting a proxy URL that deterministically fails.
                values[key] = ""
        return values

    def urllib_proxy_handler(self) -> ProxyHandler:
        if self.mode == "direct":
            return ProxyHandler({})
        if self.mode == "custom":
            return ProxyHandler({"http": self.proxy_url, "https": self.proxy_url})
        return ProxyHandler()

    def playwright_proxy(self) -> dict[str, str] | None:
        if self.mode == "system":
            server = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
            if not server:
                return None
            bypass = self.environment().get("NO_PROXY", self.no_proxy)
            return {"server": server, **({"bypass": bypass} if bypass else {})}
        if self.mode != "custom" or not self.proxy_url:
            return None
        value = {"server": self.proxy_url}
        if self.no_proxy:
            value["bypass"] = self.no_proxy
        return value


class NetworkProxyRegistry:
    def __init__(self) -> None:
        self._lock = RLock()
        self._config = NetworkProxyConfig()

    def get(self) -> NetworkProxyConfig:
        with self._lock:
            return self._config

    def set(self, *, mode: str, proxy_url: str | None, no_proxy: str | None) -> NetworkProxyConfig:
        config = validate_proxy_config(mode=mode, proxy_url=proxy_url, no_proxy=no_proxy)
        with self._lock:
            self._config = config
        return config


def validate_proxy_config(*, mode: str, proxy_url: str | None, no_proxy: str | None) -> NetworkProxyConfig:
    mode = mode.strip().lower()
    if mode not in {"direct", "system", "custom"}:
        raise ValueError("proxy mode must be direct, system, or custom")
    proxy_url = (proxy_url or "").strip() or None
    if mode == "custom":
        parsed = urlparse(proxy_url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("custom proxy URL must be an absolute HTTP(S) URL")
        if len(proxy_url or "") > 1000 or parsed.fragment:
            raise ValueError("custom proxy URL is invalid")
    else:
        proxy_url = None
    values = [part.strip() for part in (no_proxy or "").split(",") if part.strip()]
    if any(len(part) > 255 or any(char.isspace() for char in part) for part in values):
        raise ValueError("NO_PROXY entries must be comma-separated hosts or domains")
    merged = list(dict.fromkeys([*REQUIRED_NO_PROXY, *values]))
    rendered_no_proxy = ",".join(merged)
    if len(rendered_no_proxy) > 2000:
        raise ValueError("NO_PROXY is too long")
    return NetworkProxyConfig(mode=mode, proxy_url=proxy_url, no_proxy=rendered_no_proxy)


network_proxy_registry = NetworkProxyRegistry()


def _is_loopback_proxy(value: str) -> bool:
    parsed = urlparse(value if "://" in value else f"//{value}")
    return (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}


def load_network_proxy(session: Session) -> NetworkProxyConfig:
    stored = session.get(NetworkProxySetting, "global")
    if stored is None:
        return network_proxy_registry.set(mode="system", proxy_url=None, no_proxy=None)
    return network_proxy_registry.set(mode=stored.mode, proxy_url=stored.proxy_url, no_proxy=stored.no_proxy)


def save_network_proxy(session: Session, *, mode: str, proxy_url: str | None, no_proxy: str | None) -> NetworkProxyConfig:
    config = validate_proxy_config(mode=mode, proxy_url=proxy_url, no_proxy=no_proxy)
    stored = session.get(NetworkProxySetting, "global") or NetworkProxySetting()
    stored.mode = config.mode
    stored.proxy_url = config.proxy_url
    stored.no_proxy = config.no_proxy
    stored.updated_at = now_utc()
    session.add(stored)
    session.commit()
    network_proxy_registry.set(mode=config.mode, proxy_url=config.proxy_url, no_proxy=config.no_proxy)
    return config
