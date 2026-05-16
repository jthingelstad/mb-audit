from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from mb_audit.bar import parse_bar

BACKUPS_DIR = Path(__file__).resolve().parent.parent / "backups"


def _make_bar(tmp_path: Path, feed: dict, uploads: dict[str, bytes] | None = None) -> Path:
    bar = tmp_path / "test.bar"
    with zipfile.ZipFile(bar, "w") as zf:
        zf.writestr("index.html", "<html></html>")
        zf.writestr("feed.json", json.dumps(feed))
        for name, content in (uploads or {}).items():
            zf.writestr(name, content)
    return bar


def test_parse_minimal_post(tmp_path: Path) -> None:
    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Test",
        "home_page_url": "https://example.com/",
        "feed_url": "https://example.com/feed.json",
        "items": [
            {
                "id": "http://example.micro.blog/2024/01/02/hello.html",
                "url": "https://example.com/2024/01/02/hello.html",
                "title": "Hello",
                "date_published": "2024-01-02T10:30:00-06:00",
                "content_html": '<p>Hi <img src="https://example.com/uploads/2024/abc.jpg" /></p>',
                "content_text": "Hi",
                "tags": ["greeting"],
            }
        ],
    }
    bar = _make_bar(tmp_path, feed, uploads={"uploads/2024/abc.jpg": b"\xff\xd8\xff"})

    inv = parse_bar(bar)

    assert inv.host == "example.com"
    assert inv.feed_title == "Test"
    assert inv.post_count == 1
    assert inv.media_count == 1
    p = inv.posts[0]
    assert p.title == "Hello"
    assert p.is_long_form
    assert p.slug == "hello"
    assert p.tags == ("greeting",)
    assert "https://example.com/uploads/2024/abc.jpg" in p.media_urls
    assert inv.warnings == ()


def test_untitled_micropost_is_short(tmp_path: Path) -> None:
    feed = {
        "home_page_url": "https://example.com/",
        "items": [
            {
                "id": "x",
                "url": "https://example.com/2024/01/02/short.html",
                "date_published": "2024-01-02T10:30:00-06:00",
                "content_html": "<p>short</p>",
                "content_text": "short",
            }
        ],
    }
    bar = _make_bar(tmp_path, feed)
    inv = parse_bar(bar)
    assert inv.posts[0].title is None
    assert not inv.posts[0].is_long_form


def test_empty_feed_emits_warning(tmp_path: Path) -> None:
    bar = _make_bar(tmp_path, {"home_page_url": "https://example.com/", "items": []})
    inv = parse_bar(bar)
    assert inv.post_count == 0
    assert any("zero items" in w for w in inv.warnings)


def test_missing_items_key_emits_warning(tmp_path: Path) -> None:
    bar = _make_bar(tmp_path, {"home_page_url": "https://example.com/"})
    inv = parse_bar(bar)
    assert any("no 'items'" in w for w in inv.warnings)


def test_media_extraction_skips_non_media_links(tmp_path: Path) -> None:
    feed = {
        "home_page_url": "https://example.com/",
        "items": [
            {
                "id": "x",
                "url": "https://example.com/2024/01/02/p.html",
                "date_published": "2024-01-02T00:00:00Z",
                "content_html": (
                    '<a href="https://wikipedia.org/wiki/Foo">link</a>'
                    '<img src="https://example.com/uploads/2024/a.png">'
                    '<a href="https://files.example.com/clip.mp4">video</a>'
                ),
                "content_text": "",
            }
        ],
    }
    bar = _make_bar(tmp_path, feed)
    p = parse_bar(bar).posts[0]
    assert "https://example.com/uploads/2024/a.png" in p.media_urls
    assert "https://files.example.com/clip.mp4" in p.media_urls
    assert "https://wikipedia.org/wiki/Foo" not in p.media_urls


def test_not_a_bar_raises(tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.bar"
    with zipfile.ZipFile(bogus, "w") as zf:
        zf.writestr("readme.txt", "not a bar")
    with pytest.raises(ValueError, match="no feed.json"):
        parse_bar(bogus)


# ---------- Real-fixture tests (skipped if backups/ is empty) ----------

def _bar_files() -> list[Path]:
    if not BACKUPS_DIR.exists():
        return []
    return sorted(BACKUPS_DIR.glob("*.bar"))


@pytest.mark.parametrize("bar_path", _bar_files(), ids=lambda p: p.name)
def test_real_bar_parses(bar_path: Path) -> None:
    inv = parse_bar(bar_path)
    assert inv.host  # all real BARs have a home_page_url
    # Either we have posts, or we surfaced a warning explaining why we don't.
    assert inv.post_count > 0 or inv.warnings
    # Sanity: every post has the canonical URL shape.
    for p in inv.posts[:50]:
        assert p.url.startswith("https://")
        assert p.url.endswith(".html")
