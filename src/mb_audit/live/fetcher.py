"""Thin httpx wrapper used by the live collectors.

- Sync `httpx.Client` for individual page/feed fetches.
- A small disk cache keyed by URL, honoring ETag/Last-Modified.
- Auth-header redaction in any logged output.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_USER_AGENT = "mb-audit/0.1 (+https://github.com/jthingelstad/mb-audit)"
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
DEFAULT_CACHE_ROOT = Path.home() / ".mb-audit" / "cache"

_AUTH_HEADER_RE = re.compile(r"(?i)(authorization|x-microblog-token):\s*\S+")


def redact(message: str) -> str:
    """Strip credentials out of a string before logging."""
    return _AUTH_HEADER_RE.sub(r"\1: <redacted>", message)


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def install_redactor(logger: logging.Logger) -> None:
    for h in logger.handlers:
        h.setFormatter(_RedactingFormatter(h.formatter._fmt if h.formatter else "%(message)s"))


@dataclass(frozen=True, slots=True)
class CachedResponse:
    status_code: int
    url: str
    headers: dict[str, str]
    content: bytes


class Fetcher:
    """HTTP client + on-disk cache. Caller closes via context manager."""

    def __init__(
        self,
        cache_root: Path = DEFAULT_CACHE_ROOT,
        token: str | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._cache_root = cache_root
        self._cache_root.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": user_agent, "Accept": "*/*"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @property
    def client(self) -> httpx.Client:
        return self._client

    # ----- GETs with cache -----

    def get(self, url: str, *, use_cache: bool = True) -> CachedResponse:
        if not use_cache:
            r = self._client.get(url)
            return CachedResponse(r.status_code, str(r.url), dict(r.headers), r.content)

        cached = self._read_cache(url)
        conditional: dict[str, str] = {}
        if cached:
            etag = cached.headers.get("etag")
            last_mod = cached.headers.get("last-modified")
            if etag:
                conditional["If-None-Match"] = etag
            if last_mod:
                conditional["If-Modified-Since"] = last_mod

        r = self._client.get(url, headers=conditional or None)
        if r.status_code == 304 and cached:
            return cached
        cr = CachedResponse(r.status_code, str(r.url), dict(r.headers), r.content)
        if r.status_code == 200:
            self._write_cache(url, cr)
        return cr

    def get_json(self, url: str) -> Any:
        cr = self.get(url)
        if cr.status_code != 200:
            raise httpx.HTTPStatusError(
                f"GET {url} -> {cr.status_code}", request=None, response=None  # type: ignore[arg-type]
            )
        return json.loads(cr.content)

    # ----- HEAD (no cache) -----

    def head(self, url: str) -> httpx.Response:
        return self._client.head(url)

    # ----- Cache plumbing -----

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self._cache_root / digest[:2] / f"{digest}.json"

    def _read_cache(self, url: str) -> CachedResponse | None:
        path = self._cache_path(url)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        body_path = path.with_suffix(".bin")
        if not body_path.exists():
            return None
        return CachedResponse(
            status_code=payload["status_code"],
            url=payload["url"],
            headers=payload["headers"],
            content=body_path.read_bytes(),
        )

    def _write_cache(self, url: str, cr: CachedResponse) -> None:
        path = self._cache_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "status_code": cr.status_code,
                    "url": cr.url,
                    "headers": dict(cr.headers),
                }
            )
        )
        path.with_suffix(".bin").write_bytes(cr.content)
