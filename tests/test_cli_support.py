from __future__ import annotations

from datetime import datetime
from pathlib import Path

from mb_audit.bar.models import BarInventory, MediaAsset, Post
from mb_audit.cli_support import (
    build_media_probe_targets,
    resolve_site_url,
    slice_inventory,
    unique_media_urls,
)

HOME = "https://example.com/"


def make_post(url: str, media_urls: tuple[str, ...] = ()) -> Post:
    return Post(
        id=url,
        url=url,
        date_published=datetime(2024, 1, 1),
        content_html="",
        content_text="",
        media_urls=media_urls,
    )


def make_inventory(posts: tuple[Post, ...] = ()) -> BarInventory:
    return BarInventory(
        source_path=Path("/tmp/test.bar"),
        host="example.com",
        home_page_url=HOME,
        feed_title="Test",
        posts=posts,
        media=(MediaAsset(path="uploads/2024/a.jpg", size_bytes=100),),
        warnings=("fixture warning",),
    )


def test_resolve_site_url_honors_empty_disable() -> None:
    assert resolve_site_url(None, HOME) == HOME
    assert resolve_site_url("https://override.example/", HOME) == "https://override.example/"
    assert resolve_site_url("", HOME) == ""


def test_unique_media_urls_preserves_first_seen_order() -> None:
    one = "https://example.com/uploads/2024/a.jpg"
    two = "https://cdn.example.com/b.jpg"
    posts = [
        make_post("https://example.com/1.html", (one, two)),
        make_post("https://example.com/2.html", (two, one)),
    ]

    assert unique_media_urls(posts) == [one, two]


def test_build_media_probe_targets_splits_archive_internal_and_external() -> None:
    archived = "https://example.com/uploads/2024/a.jpg"
    orphan = "https://example.com/uploads/2024/missing.jpg"
    external = "https://cdn.example.com/clip.mp4"
    inventory = make_inventory(
        posts=(
            make_post("https://example.com/1.html", (archived, orphan, external)),
            make_post("https://example.com/2.html", (orphan, external)),
        )
    )

    targets = build_media_probe_targets(inventory, include_external=True)

    assert targets.archive_urls == [archived]
    assert targets.extra_internal_urls == [orphan]
    assert targets.external_urls == [external]
    assert targets.all_urls == [archived, orphan, external]


def test_build_media_probe_targets_can_skip_external_urls() -> None:
    external = "https://cdn.example.com/clip.mp4"
    inventory = make_inventory(posts=(make_post("https://example.com/1.html", (external,)),))

    targets = build_media_probe_targets(inventory, include_external=False)

    assert targets.external_urls == []
    assert targets.all_urls == ["https://example.com/uploads/2024/a.jpg"]


def test_slice_inventory_keeps_metadata_and_limits_posts() -> None:
    inventory = make_inventory(
        posts=(
            make_post("https://example.com/1.html"),
            make_post("https://example.com/2.html"),
        )
    )

    sliced = slice_inventory(inventory, 1)

    assert sliced.posts == inventory.posts[:1]
    assert sliced.media == inventory.media
    assert sliced.warnings == inventory.warnings
