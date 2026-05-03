"""Public JSON Feed collector.

Used as a *secondary* source. Micro.blog exposes only a recent slice
(default ~10 items) at the public feed URL, so this is mainly useful for
cross-checking that posts the API knows about are actually rendered to
the public site.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from mb_audit.live.fetcher import Fetcher
from mb_audit.live.inventory import LiveInventory, LiveItem, LiveSource


def fetch_feed_inventory(site_url: str, fetcher: Fetcher) -> LiveInventory:
    base = site_url.rstrip("/")
    feed_url = f"{base}/feed.json"
    raw = fetcher.get_json(feed_url)
    return _parse(raw)


def _parse(raw: Any) -> LiveInventory:
    if not isinstance(raw, dict):
        raise ValueError("live feed: root is not an object")
    home = str(raw.get("home_page_url") or "")
    items_raw = raw.get("items") or []
    if not isinstance(items_raw, list):
        raise ValueError("live feed: 'items' is not an array")
    items = tuple(_item(it) for it in items_raw if isinstance(it, dict))
    # Public feed is almost never the full archive.
    return LiveInventory(
        source=LiveSource.FEED,
        home_page_url=home,
        items=items,
        is_complete=False,
    )


def _item(raw: dict[str, Any]) -> LiveItem:
    date_str = str(raw.get("date_published") or "")
    try:
        dt: datetime | None = datetime.fromisoformat(date_str) if date_str else None
    except ValueError:
        dt = None
    title_raw = raw.get("title")
    title = str(title_raw) if title_raw not in (None, "") else None
    tags_raw = raw.get("tags") or ()
    tags = tuple(str(t) for t in tags_raw) if isinstance(tags_raw, list) else ()
    return LiveItem(
        id=str(raw.get("id") or ""),
        url=str(raw.get("url") or ""),
        title=title,
        date_published=dt,
        content_html=str(raw.get("content_html") or ""),
        content_text=str(raw.get("content_text") or ""),
        tags=tags,
        source=LiveSource.FEED,
    )
