"""On-disk cache for Micropub inventories.

We store the parsed inventory as JSON under ~/.mb-audit/cache/, keyed by a
hash of (endpoint, mp_destination). The token is *not* part of the key —
that would force re-fetches each time the user rotates a token. Instead we
trust the cache file's permissions (user-only by default).

Cached inventories are tagged with a fetched_at timestamp; the caller
decides whether they're fresh enough.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from mb_audit.live.inventory import LiveInventory, LiveItem, LiveSource

DEFAULT_CACHE_ROOT = Path.home() / ".mb-audit" / "cache" / "micropub"


def cache_path(
    *,
    endpoint: str,
    mp_destination: str | None,
    cache_root: Path = DEFAULT_CACHE_ROOT,
) -> Path:
    key = f"{endpoint}\n{mp_destination or ''}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return cache_root / f"inventory-{digest}.json"


def save(
    inventory: LiveInventory,
    *,
    endpoint: str,
    mp_destination: str | None,
    cache_root: Path = DEFAULT_CACHE_ROOT,
) -> Path:
    path = cache_path(endpoint=endpoint, mp_destination=mp_destination, cache_root=cache_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now().astimezone().isoformat(),
        "endpoint": endpoint,
        "mp_destination": mp_destination,
        "home_page_url": inventory.home_page_url,
        "is_complete": inventory.is_complete,
        "items": [
            {
                "id": it.id,
                "url": it.url,
                "title": it.title,
                "date_published": it.date_published.isoformat() if it.date_published else None,
                "content_html": it.content_html,
                "content_text": it.content_text,
                "tags": list(it.tags),
            }
            for it in inventory.items
        ],
    }
    path.write_text(json.dumps(payload))
    return path


def load(
    *,
    endpoint: str,
    mp_destination: str | None,
    cache_root: Path = DEFAULT_CACHE_ROOT,
) -> tuple[LiveInventory, datetime] | None:
    path = cache_path(endpoint=endpoint, mp_destination=mp_destination, cache_root=cache_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    items = []
    for raw in payload.get("items") or []:
        dp = raw.get("date_published")
        try:
            dt = datetime.fromisoformat(dp) if dp else None
        except ValueError:
            dt = None
        items.append(
            LiveItem(
                id=raw.get("id") or "",
                url=raw.get("url") or "",
                title=raw.get("title"),
                date_published=dt,
                content_html=raw.get("content_html") or "",
                content_text=raw.get("content_text") or "",
                tags=tuple(raw.get("tags") or ()),
                source=LiveSource.MICROPUB,
            )
        )
    inv = LiveInventory(
        source=LiveSource.MICROPUB,
        home_page_url=payload.get("home_page_url") or "",
        items=tuple(items),
        is_complete=bool(payload.get("is_complete")),
    )
    fetched_at = datetime.fromisoformat(payload.get("fetched_at"))
    return inv, fetched_at
