from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mb_audit.bar.models import BarInventory, MediaAsset, Post
from mb_audit.bar.zip_reader import BarZipReader

_FEED_MEMBER = "feed.json"
_UPLOADS_PREFIX = "uploads/"

# src="..." or href="..." — single or double quoted.
_MEDIA_ATTR_RE = re.compile(
    r'''(?:src|href)\s*=\s*(?P<q>["'])(?P<url>[^"']+)(?P=q)''',
    re.IGNORECASE,
)

# Heuristic: an attribute value points at media if it has an extension we
# care about. We do not whitelist hosts — external CDNs are valid references.
_MEDIA_EXT_RE = re.compile(
    r'\.(?:jpe?g|png|gif|webp|heic|mp4|m4v|mov|webm|mp3|m4a|wav|pdf)'
    r'(?:\?.*)?$',
    re.IGNORECASE,
)


def parse_bar(path: Path) -> BarInventory:
    """Parse a Micro.blog BAR file.

    A BAR is a ZIP archive containing index.html, feed.json (JSON Feed v1),
    and uploads/YYYY/<hash>.<ext>. This function loads feed.json fully into
    memory and enumerates uploads/ via ZipInfo only (no payload reads).
    """
    if not path.exists():
        raise FileNotFoundError(f"BAR not found: {path}")

    warnings: list[str] = []

    with BarZipReader(path) as zf:
        if not zf.has(_FEED_MEMBER):
            raise ValueError(f"{path}: no feed.json — not a BAR file?")

        try:
            feed = json.loads(zf.read(_FEED_MEMBER))
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}: feed.json is not valid JSON: {e}") from e

        if not isinstance(feed, dict):
            raise ValueError(f"{path}: feed.json root is not an object")

        items = feed.get("items")
        if items is None:
            warnings.append("feed.json has no 'items' key")
            items = []
        elif not isinstance(items, list):
            raise ValueError(f"{path}: feed.json 'items' is not an array")

        if len(items) == 0:
            warnings.append(
                "feed.json has zero items — BAR may be empty or malformed"
            )

        home_page_url = str(feed.get("home_page_url") or "")
        feed_title = str(feed.get("title") or "")
        host = urlparse(home_page_url).netloc if home_page_url else ""

        posts = tuple(_post_from_item(it, warnings) for it in items if isinstance(it, dict))

        media = tuple(
            MediaAsset(path=e.name, size_bytes=e.uncompressed_size)
            for e in zf.iter_prefix(_UPLOADS_PREFIX)
        )

    return BarInventory(
        source_path=path,
        host=host,
        home_page_url=home_page_url,
        feed_title=feed_title,
        posts=posts,
        media=media,
        warnings=tuple(warnings),
    )


def _post_from_item(item: dict[str, Any], warnings: list[str]) -> Post:
    item_id = str(item.get("id") or "")
    url = str(item.get("url") or "")
    date_str = str(item.get("date_published") or "")
    content_html = str(item.get("content_html") or "")
    content_text = str(item.get("content_text") or "")

    title_raw = item.get("title")
    title = str(title_raw) if title_raw not in (None, "") else None

    tags_raw = item.get("tags") or ()
    tags = tuple(str(t) for t in tags_raw) if isinstance(tags_raw, list) else ()

    if not item_id:
        warnings.append(f"item missing 'id' (url={url or '?'})")
    if not url:
        warnings.append(f"item missing 'url' (id={item_id or '?'})")
    if not date_str:
        warnings.append(f"item missing 'date_published' (id={item_id or '?'})")

    try:
        # JSON Feed dates are ISO 8601; Python 3.11+ handles "+HH:MM" offsets.
        date_published = datetime.fromisoformat(date_str) if date_str else datetime.min
    except ValueError:
        warnings.append(f"item has unparseable date '{date_str}' (id={item_id})")
        date_published = datetime.min

    media_urls = _extract_media_urls(content_html)

    return Post(
        id=item_id,
        url=url,
        date_published=date_published,
        content_html=content_html,
        content_text=content_text,
        title=title,
        tags=tags,
        media_urls=media_urls,
    )


def _extract_media_urls(html: str) -> tuple[str, ...]:
    if not html:
        return ()
    seen: dict[str, None] = {}
    for m in _MEDIA_ATTR_RE.finditer(html):
        url = m.group("url")
        if _MEDIA_EXT_RE.search(url):
            seen.setdefault(url, None)
    return tuple(seen)
