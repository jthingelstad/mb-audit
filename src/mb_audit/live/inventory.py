"""Unified live-side post model.

`LiveItem` and `LiveInventory` are the shapes the resolver consumes.
Both the Micropub q=source collector and the public JSON Feed collector
populate the same types.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from urllib.parse import urlparse


class LiveSource(str, Enum):
    MICROPUB = "micropub"  # authoritative — comes from MB API q=source
    FEED = "feed"  # public JSON feed
    PERMALINK = "permalink"  # fetched via direct HTML GET


@dataclass(frozen=True, slots=True)
class LiveItem:
    id: str
    url: str
    title: str | None
    date_published: datetime | None
    content_html: str
    content_text: str  # markdown source when MICROPUB; rendered text otherwise
    tags: tuple[str, ...]
    source: LiveSource

    @property
    def slug(self) -> str:
        return self.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".html")


class LiveInventory:
    """Live-side post inventory. Indexes are built once and reused.

    Not a frozen dataclass because we lazily memoize derived indexes.
    """

    __slots__ = (
        "source",
        "home_page_url",
        "items",
        "is_complete",
        "_by_id",
        "_by_url",
        "_by_path",
        "_by_slug",
    )

    def __init__(
        self,
        *,
        source: LiveSource,
        home_page_url: str,
        items: tuple[LiveItem, ...],
        is_complete: bool,
    ) -> None:
        self.source = source
        self.home_page_url = home_page_url
        self.items = items
        self.is_complete = is_complete
        self._by_id: dict[str, LiveItem] | None = None
        self._by_url: dict[str, LiveItem] | None = None
        self._by_path: dict[str, list[LiveItem]] | None = None
        self._by_slug: dict[str, list[LiveItem]] | None = None

    def by_id(self) -> dict[str, LiveItem]:
        if self._by_id is None:
            self._by_id = {it.id: it for it in self.items if it.id}
        return self._by_id

    def by_url(self) -> dict[str, LiveItem]:
        if self._by_url is None:
            self._by_url = {it.url: it for it in self.items if it.url}
        return self._by_url

    def by_path(self) -> dict[str, list[LiveItem]]:
        if self._by_path is None:
            out: dict[str, list[LiveItem]] = {}
            for it in self.items:
                if not it.url:
                    continue
                out.setdefault(urlparse(it.url).path, []).append(it)
            self._by_path = out
        return self._by_path

    def by_slug(self) -> dict[str, list[LiveItem]]:
        if self._by_slug is None:
            out: dict[str, list[LiveItem]] = {}
            for it in self.items:
                out.setdefault(it.slug, []).append(it)
            self._by_slug = out
        return self._by_slug
