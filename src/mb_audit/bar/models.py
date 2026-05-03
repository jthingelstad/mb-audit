from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Post:
    id: str
    url: str
    date_published: datetime
    content_html: str
    content_text: str
    title: str | None = None
    tags: tuple[str, ...] = ()
    media_urls: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        # URL pattern: https://host/YYYY/MM/DD/<slug>.html
        tail = self.url.rstrip("/").rsplit("/", 1)[-1]
        return tail.removesuffix(".html")

    @property
    def is_long_form(self) -> bool:
        return self.title is not None and self.title.strip() != ""


@dataclass(frozen=True, slots=True)
class MediaAsset:
    path: str            # path inside the BAR, e.g. "uploads/2023/02872be889.jpg"
    size_bytes: int


@dataclass(frozen=True, slots=True)
class BarInventory:
    source_path: Path
    host: str            # derived from feed home_page_url
    home_page_url: str
    feed_title: str
    posts: tuple[Post, ...]
    media: tuple[MediaAsset, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def post_count(self) -> int:
        return len(self.posts)

    @property
    def media_count(self) -> int:
        return len(self.media)

    @property
    def date_range(self) -> tuple[datetime, datetime] | None:
        if not self.posts:
            return None
        dates = [p.date_published for p in self.posts]
        return (min(dates), max(dates))
