from __future__ import annotations

from datetime import datetime
from pathlib import Path

from mb_audit.audit.media import MediaProbe
from mb_audit.audit.media_reconciler import reconcile_media
from mb_audit.audit.severity import MediaClassification, Severity
from mb_audit.bar.models import BarInventory, MediaAsset, Post
from mb_audit.live.inventory import LiveInventory, LiveItem, LiveSource
from mb_audit.live.micropub_media import build_api_media_index

HOST = "https://www.thingelstad.com/"


def make_bar(
    *,
    media_files: list[tuple[str, int]] = (),
    posts: list[Post] = (),
) -> BarInventory:
    return BarInventory(
        source_path=Path("/tmp/dummy.bar"),
        host="www.thingelstad.com",
        home_page_url=HOST,
        feed_title="t",
        posts=tuple(posts),
        media=tuple(MediaAsset(path=p, size_bytes=s) for p, s in media_files),
        warnings=(),
    )


def make_post(url: str, html: str = "", text: str = "", media_urls: tuple[str, ...] = ()) -> Post:
    return Post(
        id=url,
        url=url,
        date_published=datetime(2024, 1, 1),
        content_html=html,
        content_text=text,
        media_urls=media_urls,
    )


def make_live(items: list[LiveItem]) -> LiveInventory:
    return LiveInventory(
        source=LiveSource.MICROPUB,
        home_page_url=HOST,
        items=tuple(items),
        is_complete=True,
    )


def make_live_item(url: str, html: str = "") -> LiveItem:
    return LiveItem(
        id=url,
        url=url,
        title=None,
        date_published=None,
        content_html=html,
        content_text="",
        tags=(),
        source=LiveSource.MICROPUB,
    )


# Each test below pins one cell of the 2x2 matrix or one of the side rules.


def test_ok_when_site_200_and_api_references_url() -> None:
    asset_url = HOST + "uploads/2024/x.jpg"
    bar = make_bar(media_files=[("uploads/2024/x.jpg", 1234)])
    api = build_api_media_index(
        make_live(
            [
                make_live_item(
                    "https://www.thingelstad.com/2024/01/02/p.html",
                    f'<img src="{asset_url}">',
                )
            ]
        )
    )
    probes = {
        asset_url: MediaProbe(url=asset_url, status=200, error=None, final_url=asset_url, size=1234)
    }
    findings = reconcile_media(bar=bar, probes=probes, api_index=api)
    assert findings[0].classification == MediaClassification.OK
    assert findings[0].severity == Severity.OK


def test_orphan_present_when_site_200_but_no_api_post_references_it() -> None:
    asset_url = HOST + "uploads/2024/y.jpg"
    bar = make_bar(media_files=[("uploads/2024/y.jpg", 999)])
    api = build_api_media_index(make_live([]))
    probes = {
        asset_url: MediaProbe(url=asset_url, status=200, error=None, final_url=asset_url, size=999)
    }
    findings = reconcile_media(bar=bar, probes=probes, api_index=api)
    assert findings[0].classification == MediaClassification.ORPHAN_PRESENT


def test_site_missing_when_site_404_but_api_post_references_it() -> None:
    asset_url = HOST + "uploads/2024/z.jpg"
    bar = make_bar(media_files=[("uploads/2024/z.jpg", 1000)])
    api = build_api_media_index(
        make_live(
            [
                make_live_item(
                    "https://www.thingelstad.com/2024/01/02/post.html",
                    f'<img src="{asset_url}">',
                )
            ]
        )
    )
    probes = {asset_url: MediaProbe(url=asset_url, status=404, error=None, final_url=asset_url)}
    findings = reconcile_media(bar=bar, probes=probes, api_index=api)
    assert findings[0].classification == MediaClassification.SITE_MISSING
    assert findings[0].severity == Severity.HIGH


def test_missing_when_site_404_and_api_has_no_post_referencing_it() -> None:
    asset_url = HOST + "uploads/2024/q.jpg"
    bar = make_bar(media_files=[("uploads/2024/q.jpg", 100)])
    api = build_api_media_index(make_live([]))
    probes = {asset_url: MediaProbe(url=asset_url, status=404, error=None, final_url=asset_url)}
    findings = reconcile_media(bar=bar, probes=probes, api_index=api)
    assert findings[0].classification == MediaClassification.MISSING
    assert findings[0].severity == Severity.CRITICAL


def test_size_mismatch_when_lengths_differ() -> None:
    asset_url = HOST + "uploads/2024/m.jpg"
    bar = make_bar(media_files=[("uploads/2024/m.jpg", 5000)])
    api = build_api_media_index(
        make_live(
            [
                make_live_item(
                    "https://www.thingelstad.com/p.html",
                    f'<img src="{asset_url}">',
                )
            ]
        )
    )
    probes = {
        asset_url: MediaProbe(url=asset_url, status=200, error=None, final_url=asset_url, size=4999)
    }
    findings = reconcile_media(bar=bar, probes=probes, api_index=api)
    assert findings[0].classification == MediaClassification.SIZE_MISMATCH
    assert "5000" in findings[0].note and "4999" in findings[0].note


def test_size_match_required_only_when_both_known() -> None:
    asset_url = HOST + "uploads/2024/n.jpg"
    bar = make_bar(media_files=[("uploads/2024/n.jpg", 5000)])
    api = build_api_media_index(
        make_live(
            [
                make_live_item(
                    "https://www.thingelstad.com/p.html",
                    f'<img src="{asset_url}">',
                )
            ]
        )
    )
    # No Content-Length on the live response.
    probes = {
        asset_url: MediaProbe(url=asset_url, status=200, error=None, final_url=asset_url, size=None)
    }
    findings = reconcile_media(bar=bar, probes=probes, api_index=api)
    assert findings[0].classification == MediaClassification.OK


def test_orphan_referenced_when_post_points_at_uploads_not_in_bar() -> None:
    referenced = HOST + "uploads/2024/missing.jpg"
    post = make_post(
        "https://www.thingelstad.com/2024/01/02/p.html",
        html=f'<img src="{referenced}">',
        media_urls=(referenced,),
    )
    bar = make_bar(media_files=[], posts=[post])
    api = build_api_media_index(make_live([]))
    findings = reconcile_media(bar=bar, probes={}, api_index=api)
    cls = [f.classification for f in findings]
    assert MediaClassification.ORPHAN_REFERENCED in cls


def test_external_broken_when_external_url_is_404() -> None:
    external = "https://files.example.com/clip.mp4"
    post = make_post(
        "https://www.thingelstad.com/2024/01/02/p.html",
        html=f'<a href="{external}">v</a>',
        media_urls=(external,),
    )
    bar = make_bar(media_files=[], posts=[post])
    api = build_api_media_index(make_live([]))
    probes = {external: MediaProbe(url=external, status=404, error=None, final_url=external)}
    findings = reconcile_media(bar=bar, probes=probes, api_index=api)
    assert any(f.classification == MediaClassification.EXTERNAL_BROKEN for f in findings)


def test_site_error_classifies_5xx_and_transport() -> None:
    asset_url = HOST + "uploads/2024/e.jpg"
    bar = make_bar(media_files=[("uploads/2024/e.jpg", 1)])
    api = build_api_media_index(make_live([]))
    probes = {asset_url: MediaProbe(url=asset_url, status=503, error=None, final_url=asset_url)}
    findings = reconcile_media(bar=bar, probes=probes, api_index=api)
    assert findings[0].classification == MediaClassification.SITE_ERROR

    probes = {asset_url: MediaProbe(url=asset_url, status=0, error="timeout", final_url=asset_url)}
    findings = reconcile_media(bar=bar, probes=probes, api_index=api)
    assert findings[0].classification == MediaClassification.SITE_ERROR
