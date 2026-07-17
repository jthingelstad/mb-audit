"""Multi-strategy post resolution: BAR post -> live URL.

Order:
  1. micropub_lookup — direct Micropub q=source by URL (authoritative)
  2. live_inventory  — match against any LiveInventory we already collected
                       (Micropub bulk paginate, or public feed fallback)
  3. permalink       — HEAD post.url against the public site
  4. slug_date       — same slug ±2 days in any inventory
  5. title           — exact title within an inventory (long-form only)
  6. content_hash    — first ~500 chars of content hashed against inventory
  7. fuzzy           — rapidfuzz over content_text against inventory
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

import httpx
from rapidfuzz import fuzz

from mb_audit.bar.models import Post
from mb_audit.live.inventory import LiveInventory, LiveItem
from mb_audit.live.micropub import PerUrlLookup
from mb_audit.live.permalink import head_permalink


class Strategy(str, Enum):
    MICROPUB_LOOKUP = "micropub_lookup"
    LIVE_INVENTORY = "live_inventory"
    PERMALINK = "permalink"
    SLUG_DATE = "slug_date"
    TITLE = "title"
    CONTENT_HASH = "content_hash"
    FUZZY = "fuzzy"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    post_id: str
    expected_url: str
    found_url: str | None
    final_url: str | None
    strategy: Strategy
    micropub_status: int | None  # status returned by the Micropub lookup, if any
    permalink_status: int | None  # status returned by HEAD, if attempted
    matched_item: LiveItem | None  # the live item we matched (for downstream content/media checks)
    note: str = ""


def resolve(
    post: Post,
    *,
    micropub_lookup: PerUrlLookup | None,
    inventories: tuple[LiveInventory, ...],
    permalink_client: httpx.Client | None,
    fuzzy_threshold: float = 0.85,
) -> ResolutionResult:
    micropub_status = micropub_lookup.status if micropub_lookup else None

    # 1. Micropub direct lookup
    if micropub_lookup and micropub_lookup.item is not None:
        return ResolutionResult(
            post_id=post.id,
            expected_url=post.url,
            found_url=micropub_lookup.item.url,
            final_url=None,
            strategy=Strategy.MICROPUB_LOOKUP,
            micropub_status=micropub_status,
            permalink_status=None,
            matched_item=micropub_lookup.item,
        )

    # 2. Live inventory by id/url (covers MB bulk page + public feed fallback)
    inventory_hit = _lookup_in_inventories(post, inventories)
    if inventory_hit:
        item, inv = inventory_hit
        return ResolutionResult(
            post_id=post.id,
            expected_url=post.url,
            found_url=item.url,
            final_url=None,
            strategy=Strategy.LIVE_INVENTORY,
            micropub_status=micropub_status,
            permalink_status=None,
            matched_item=item,
            note=f"matched in {inv.source.value} inventory",
        )

    # 3. Permalink HEAD as a last "does the URL work?" check
    perma_status: int | None = None
    if permalink_client is not None:
        head = head_permalink(permalink_client, post.url)
        perma_status = head.status
        if 200 <= head.status < 300:
            return ResolutionResult(
                post_id=post.id,
                expected_url=post.url,
                found_url=post.url,
                final_url=head.final_url,
                strategy=Strategy.PERMALINK,
                micropub_status=micropub_status,
                permalink_status=perma_status,
                matched_item=None,
                note="permalink responds 2xx but post not in API",
            )

    # 4-7: degraded matches against any inventory we have
    slug = post.slug
    target_text = (post.content_text or "").strip()
    target_hash = _content_hash(target_text)
    target_prefix = target_text[:1000]

    for inv in inventories:
        # 4. Slug + date range
        for li in inv.by_slug().get(slug, []):
            if li.date_published is None:
                continue
            if abs(li.date_published - post.date_published) <= timedelta(days=2):
                return _hit(
                    post,
                    li,
                    Strategy.SLUG_DATE,
                    micropub_status,
                    perma_status,
                    note=f"slug match in {inv.source.value} ±2d",
                )

        # 5. Title
        if post.is_long_form and post.title:
            wanted = post.title.strip()
            for li in inv.items:
                if li.title and li.title.strip() == wanted:
                    return _hit(
                        post,
                        li,
                        Strategy.TITLE,
                        micropub_status,
                        perma_status,
                        note=f"title match in {inv.source.value}",
                    )

        # 6. Content hash
        if target_hash:
            for li in inv.items:
                if _content_hash(li.content_text) == target_hash:
                    return _hit(
                        post,
                        li,
                        Strategy.CONTENT_HASH,
                        micropub_status,
                        perma_status,
                        note=f"content-hash match in {inv.source.value}",
                    )

        # 7. Fuzzy
        if target_prefix:
            best_score = 0.0
            best_item: LiveItem | None = None
            for li in inv.items:
                if not li.content_text:
                    continue
                score = fuzz.ratio(target_prefix, li.content_text[:1000]) / 100.0
                if score > best_score:
                    best_score = score
                    best_item = li
            if best_item is not None and best_score >= fuzzy_threshold:
                return _hit(
                    post,
                    best_item,
                    Strategy.FUZZY,
                    micropub_status,
                    perma_status,
                    note=f"fuzzy {best_score:.2f} in {inv.source.value}",
                )

    return ResolutionResult(
        post_id=post.id,
        expected_url=post.url,
        found_url=None,
        final_url=None,
        strategy=Strategy.NONE,
        micropub_status=micropub_status,
        permalink_status=perma_status,
        matched_item=None,
    )


def _lookup_in_inventories(
    post: Post, inventories: tuple[LiveInventory, ...]
) -> tuple[LiveItem, LiveInventory] | None:
    from urllib.parse import urlparse

    bar_path = urlparse(post.url).path
    for inv in inventories:
        # exact URL match wins
        hit = inv.by_url().get(post.url) or inv.by_id().get(post.id)
        if hit is not None:
            return hit, inv
        # path-only match — same post, different host (legacy hostname in MB records)
        if bar_path:
            for li in inv.by_path().get(bar_path, []):
                return li, inv
    return None


def _hit(
    post: Post,
    item: LiveItem,
    strategy: Strategy,
    micropub_status: int | None,
    perma_status: int | None,
    *,
    note: str,
) -> ResolutionResult:
    return ResolutionResult(
        post_id=post.id,
        expected_url=post.url,
        found_url=item.url,
        final_url=None,
        strategy=strategy,
        micropub_status=micropub_status,
        permalink_status=perma_status,
        matched_item=item,
        note=note,
    )


def _content_hash(text: str) -> str:
    head = (text or "").strip()[:500]
    if not head:
        return ""
    return hashlib.sha256(head.encode("utf-8")).hexdigest()
