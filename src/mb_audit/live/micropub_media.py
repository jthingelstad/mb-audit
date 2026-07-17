"""Build the set of media URLs the Micro.blog API "knows about."

Micro.blog does not expose a media-listing endpoint (`q=media`, `q=media-list`,
and `media-endpoint?q=last` all return `{}`), and `q=source` MF2 entries do
not include `photo`/`video`/`audio` properties — only `content`. So the API's
view of "which media exist" is derived from the URLs embedded in post bodies.

This module operates over a `LiveInventory` already collected by the post
audit and makes **zero new API calls**.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mb_audit.live.inventory import LiveInventory

# Same shape as bar/parser.py — keep them in lockstep.
_MEDIA_ATTR_RE = re.compile(
    r"""(?:src|href)\s*=\s*(?P<q>["'])(?P<url>[^"']+)(?P=q)""",
    re.IGNORECASE,
)
_MEDIA_EXT_RE = re.compile(
    r"\.(?:jpe?g|png|gif|webp|heic|mp4|m4v|mov|webm|mp3|m4a|wav|pdf)"
    r"(?:\?.*)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ApiMediaIndex:
    """Reverse index: media URL -> list of post URLs that reference it."""

    refs: dict[str, tuple[str, ...]]

    @property
    def known(self) -> set[str]:
        return set(self.refs.keys())

    def referencing_posts(self, media_url: str) -> tuple[str, ...]:
        return self.refs.get(media_url, ())


def build_api_media_index(inventory: LiveInventory) -> ApiMediaIndex:
    """Walk every API post's body and collect media URLs by post.

    MB's q=source returns the post body as a single string under MF2
    `properties.content[0]`. We map that to `LiveItem.content_text`
    even though it can contain inline HTML — `<img src=...>` tags are the
    common embed style for media. So we scan both fields.
    """
    refs: dict[str, list[str]] = {}
    for item in inventory.items:
        urls: set[str] = set()
        urls |= _extract_media(item.content_html)
        urls |= _extract_media(item.content_text)
        for url in urls:
            refs.setdefault(url, []).append(item.url)
    return ApiMediaIndex(refs={k: tuple(v) for k, v in refs.items()})


def _extract_media(html: str) -> set[str]:
    if not html:
        return set()
    out: set[str] = set()
    for m in _MEDIA_ATTR_RE.finditer(html):
        url = m.group("url")
        if _MEDIA_EXT_RE.search(url):
            out.add(url)
    return out
