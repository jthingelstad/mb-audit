"""Pure helpers used by the CLI command orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

from mb_audit.bar.models import BarInventory, Post


@dataclass(frozen=True, slots=True)
class MediaProbeTargets:
    archive_urls: list[str]
    extra_internal_urls: list[str]
    external_urls: list[str]

    @property
    def all_urls(self) -> list[str]:
        return self.archive_urls + self.extra_internal_urls + self.external_urls


def resolve_site_url(site_arg: str | None, home_page_url: str) -> str:
    """Resolve CLI --site semantics.

    None means "default to the BAR home page"; an explicit empty string disables
    public-site probing.
    """
    if site_arg == "":
        return ""
    if site_arg is not None:
        return site_arg
    return home_page_url


def unique_media_urls(posts: Iterable[Post]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for post in posts:
        for url in post.media_urls:
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
    return urls


def build_media_probe_targets(
    inventory: BarInventory,
    *,
    include_external: bool,
) -> MediaProbeTargets:
    base = inventory.home_page_url.rstrip("/") + "/"
    archive_urls = [base + asset.path for asset in inventory.media]
    archive_set = set(archive_urls)
    site_host = inventory.host.lower()

    external_urls: list[str] = []
    seen_external: set[str] = set()
    extra_internal_urls: list[str] = []
    seen_internal: set[str] = set()

    for post in inventory.posts:
        for url in post.media_urls:
            host = urlparse(url).netloc.lower()
            if host == site_host:
                if url not in archive_set and url not in seen_internal:
                    seen_internal.add(url)
                    extra_internal_urls.append(url)
                continue

            if include_external and url not in seen_external:
                seen_external.add(url)
                external_urls.append(url)

    return MediaProbeTargets(
        archive_urls=archive_urls,
        extra_internal_urls=extra_internal_urls,
        external_urls=external_urls,
    )


def slice_inventory(inventory: BarInventory, n: int) -> BarInventory:
    return BarInventory(
        source_path=inventory.source_path,
        host=inventory.host,
        home_page_url=inventory.home_page_url,
        feed_title=inventory.feed_title,
        posts=inventory.posts[:n],
        media=inventory.media,
        warnings=inventory.warnings,
    )
