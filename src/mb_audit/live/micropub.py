"""Micro.blog Micropub `q=source` collector.

This is the **primary** authoritative source for `mb-audit`: the Micro.blog
API itself, queried with the user's app token. It tells us exactly which
posts MB believes exist and returns the canonical Markdown source per post.

The Micropub spec (https://www.w3.org/TR/micropub/#querying) defines two
shapes for q=source:

1. ``GET .../micropub?q=source`` — list of posts, paginated.
2. ``GET .../micropub?q=source&url=<post-url>`` — a single post by URL.

For an audit, the per-URL form is ideal: we ask the API about exactly
the URLs the BAR claims exist. A 404 / empty response is unambiguous
evidence the post is missing.

Response shape (MF2 JSON):

```json
{
  "type": ["h-entry"],
  "properties": {
    "name":      ["Title"],            // optional
    "content":   ["markdown..."],      // or [{"html": "...", "value": "..."}]
    "published": ["2024-01-02T..."],
    "url":       ["https://example.com/2024/01/02/foo.html"],
    "category":  ["tag1", "tag2"]
  }
}
```

Some servers wrap a single result as `{"items": [<entry>]}`; we accept both.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, cast

import httpx

from mb_audit.live.inventory import LiveInventory, LiveItem, LiveSource

DEFAULT_MICROPUB_ENDPOINT = "https://micro.blog/micropub"
DEFAULT_CONCURRENCY = 6
DEFAULT_PAGE_SIZE = 200
DEFAULT_PAGE_DELAY_SEC = 0.5  # be polite between paginated requests

log = logging.getLogger(__name__)
ProgressCallback = Callable[[int, int], None]


class MicropubError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PerUrlLookup:
    requested_url: str
    item: LiveItem | None
    status: int
    error: str | None = None


# ---------- Single-URL lookup (the workhorse) ----------


async def lookup_post(
    client: httpx.AsyncClient,
    post_url: str,
    *,
    endpoint: str = DEFAULT_MICROPUB_ENDPOINT,
) -> PerUrlLookup:
    """Look up one post by URL via Micropub q=source."""
    try:
        r = await client.get(
            endpoint,
            params={"q": "source", "url": post_url},
        )
    except httpx.HTTPError as e:
        return PerUrlLookup(post_url, None, status=0, error=str(e))

    if r.status_code == 404:
        return PerUrlLookup(post_url, None, status=404)
    if r.status_code == 401 or r.status_code == 403:
        raise MicropubError(f"Micropub auth failed ({r.status_code}). Check MICROBLOG_TOKEN.")
    if r.status_code != 200:
        return PerUrlLookup(
            post_url,
            None,
            status=r.status_code,
            error=f"unexpected status {r.status_code}",
        )

    try:
        payload = r.json()
    except ValueError as e:
        return PerUrlLookup(post_url, None, status=r.status_code, error=f"bad JSON: {e}")

    item = _coerce_to_item(payload, requested_url=post_url)
    if item is None:
        return PerUrlLookup(post_url, None, status=200, error="empty response")
    return PerUrlLookup(post_url, item, status=200)


async def lookup_many(
    post_urls: Iterable[str],
    *,
    token: str,
    endpoint: str = DEFAULT_MICROPUB_ENDPOINT,
    concurrency: int = DEFAULT_CONCURRENCY,
    on_progress: ProgressCallback | None = None,
) -> list[PerUrlLookup]:
    """Concurrent per-URL Micropub lookups."""
    urls = list(post_urls)
    sem = asyncio.Semaphore(concurrency)
    headers = _auth_headers(token)
    results: list[PerUrlLookup | None] = [None] * len(urls)
    completed = 0

    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:

        async def one(i: int, url: str) -> None:
            nonlocal completed
            async with sem:
                results[i] = await lookup_post(client, url, endpoint=endpoint)
                completed += 1
                if on_progress is not None:
                    on_progress(completed, len(urls))

        await asyncio.gather(*(one(i, u) for i, u in enumerate(urls)))
    # mypy: results is fully populated by gather above
    return [r for r in results if r is not None]


# ---------- Optional bulk pagination ----------


def fetch_config(token: str, endpoint: str = DEFAULT_MICROPUB_ENDPOINT) -> dict[str, Any]:
    """GET ?q=config — destinations, post types, media endpoint."""
    with httpx.Client(headers=_auth_headers(token), timeout=30.0) as client:
        r = client.get(endpoint, params={"q": "config"})
        if r.status_code in (401, 403):
            raise MicropubError(f"Micropub auth failed ({r.status_code}). Check MICROBLOG_TOKEN.")
        r.raise_for_status()
        payload = r.json()
        if not isinstance(payload, dict):
            raise MicropubError("Micropub config response is not an object")
        return cast(dict[str, Any], payload)


def pick_destination_uid(config: dict[str, Any], home_page_url: str) -> str | None:
    """Match a BAR home_page_url to a Micropub `destination[].uid`.

    Destinations have `name` like ``www.thingelstad.com`` and `uid` like
    ``https://jthingelstad.micro.blog/``. We compare on host.
    """
    from urllib.parse import urlparse

    bar_host = urlparse(home_page_url).netloc.lower().lstrip("www.")
    for d in config.get("destination") or []:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or "").lower().lstrip("www.")
        if name == bar_host:
            return str(d.get("uid") or "") or None
    return None


def fetch_full_inventory(
    *,
    token: str,
    mp_destination: str | None = None,
    endpoint: str = DEFAULT_MICROPUB_ENDPOINT,
    page_size: int = DEFAULT_PAGE_SIZE,
    page_delay_sec: float = DEFAULT_PAGE_DELAY_SEC,
    max_pages: int = 2000,
    on_page: ProgressCallback | None = None,
) -> LiveInventory:
    """Page through `q=source` to build a complete inventory of MB posts.

    Pass `mp_destination` (the destination's `uid`) to scope to a single
    blog. Without it, MB returns posts across all of the user's blogs.

    A small per-page delay keeps us polite to the MB API.
    """
    import time

    headers = _auth_headers(token)
    items: list[LiveItem] = []
    offset = 0
    home_page_url = ""
    with httpx.Client(headers=headers, timeout=60.0) as client:
        for page_idx in range(max_pages):
            params: dict[str, Any] = {
                "q": "source",
                "offset": offset,
                "limit": page_size,
            }
            if mp_destination:
                params["mp-destination"] = mp_destination
            r = client.get(endpoint, params=params)
            if r.status_code in (401, 403):
                raise MicropubError(
                    f"Micropub auth failed ({r.status_code}). Check MICROBLOG_TOKEN."
                )
            if r.status_code == 429:
                retry = float(r.headers.get("Retry-After") or "5")
                time.sleep(retry)
                continue
            r.raise_for_status()
            payload = r.json()
            page = _coerce_to_items(payload)
            if not page:
                break
            items.extend(page)
            offset += len(page)
            if on_page is not None:
                on_page(len(items), len(page))
            if len(page) < page_size:
                break
            if page_delay_sec > 0:
                time.sleep(page_delay_sec)

    return LiveInventory(
        source=LiveSource.MICROPUB,
        home_page_url=home_page_url,
        items=tuple(items),
        is_complete=True,
    )


# ---------- Helpers ----------


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "mb-audit/0.1",
    }


def _coerce_to_item(payload: Any, *, requested_url: str | None = None) -> LiveItem | None:
    if not isinstance(payload, dict):
        return None
    if "items" in payload:
        items = payload["items"] or []
        if not isinstance(items, list) or not items:
            return None
        return _mf2_to_item(items[0], fallback_url=requested_url)
    if "properties" in payload:
        return _mf2_to_item(payload, fallback_url=requested_url)
    return None


def _coerce_to_items(payload: Any) -> list[LiveItem]:
    if not isinstance(payload, dict):
        return []
    items_raw = payload.get("items") or []
    if not isinstance(items_raw, list):
        return []
    return [it for it in (_mf2_to_item(e) for e in items_raw if isinstance(e, dict)) if it]


def _mf2_to_item(entry: dict[str, Any], *, fallback_url: str | None = None) -> LiveItem | None:
    props = entry.get("properties") or {}
    if not isinstance(props, dict):
        return None

    url = _first_str(props.get("url")) or (fallback_url or "")
    if not url:
        return None

    uid = _first_str(props.get("uid")) or url
    title = _first_str(props.get("name"))
    published_str = _first_str(props.get("published"))
    try:
        published = datetime.fromisoformat(published_str) if published_str else None
    except ValueError:
        published = None

    content_md, content_html = _extract_content(props.get("content"))
    cats_raw = props.get("category") or []
    if isinstance(cats_raw, list):
        tags = tuple(str(c) for c in cats_raw)
    else:
        tags = ()

    return LiveItem(
        id=uid,
        url=url,
        title=title or None,
        date_published=published,
        content_html=content_html,
        content_text=content_md,
        tags=tags,
        source=LiveSource.MICROPUB,
    )


def _first_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        if not value:
            return ""
        v = value[0]
    else:
        v = value
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return str(v.get("value") or v.get("html") or "")
    return str(v)


def _extract_content(value: Any) -> tuple[str, str]:
    """Return (markdown_or_text, html). Either may be empty."""
    if value is None:
        return "", ""
    if isinstance(value, list):
        if not value:
            return "", ""
        v = value[0]
    else:
        v = value
    if isinstance(v, str):
        return v, ""
    if isinstance(v, dict):
        return str(v.get("value") or ""), str(v.get("html") or "")
    return str(v), ""
