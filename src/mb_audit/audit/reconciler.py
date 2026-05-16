"""Reconcile BAR inventory against live state, producing Findings."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mb_audit.audit.content import is_modified
from mb_audit.audit.severity import Classification, Severity, severity_of
from mb_audit.bar.models import BarInventory, Post
from mb_audit.live.inventory import LiveInventory
from mb_audit.live.resolver import ResolutionResult, Strategy


@dataclass(frozen=True, slots=True)
class Finding:
    post_id: str
    expected_url: str
    classification: Classification
    severity: Severity
    strategy: Strategy
    found_url: str | None
    note: str
    evidence: dict[str, Any] = field(default_factory=dict)


def reconcile(
    *,
    bar: BarInventory,
    resolutions: list[ResolutionResult],
    inventories: tuple[LiveInventory, ...] = (),
    media_status: dict[str, int] | None = None,
    permalink_status: dict[str, int] | None = None,
) -> list[Finding]:
    """Build Findings from per-post resolutions and optional second-source probes.

    `resolutions`        — 1:1 with `bar.posts`.
    `media_status`       — `{absolute media URL: HTTP status}`.
    `permalink_status`   — `{post.url: HTTP status}` from a parallel HEAD pass
                           against the public site.
    """
    findings: list[Finding] = []
    bar_posts_by_id = {p.id: p for p in bar.posts}
    media_status = media_status or {}
    permalink_status = permalink_status or {}

    for post, res in zip(bar.posts, resolutions):
        finding = _classify_one(post, res, media_status, permalink_status)
        findings.append(finding)

    findings.extend(_extras_in_inventories(bar_posts_by_id, inventories))

    return findings


def _classify_one(
    post: Post,
    res: ResolutionResult,
    media_status: dict[str, int],
    permalink_status: dict[str, int],
) -> Finding:
    api_found = res.found_url is not None
    site_status = permalink_status.get(post.url)
    site_present = site_status is not None and 200 <= site_status < 400
    site_404 = site_status == 404
    site_error = site_status is not None and (
        site_status == 0 or 500 <= site_status < 600
    )

    # Both sources agree it's gone
    if not api_found and site_404:
        return _make(post, res, Classification.MISSING,
                    note=f"absent in MB API and live site (site={site_status})",
                    evidence={
                        "micropub_status": res.micropub_status,
                        "permalink_status": site_status,
                    })

    # API says no, site says yes — API/index issue
    if not api_found and site_present:
        return _make(post, res, Classification.API_MISSING,
                    note=f"live site has it ({site_status}) but MB API does not",
                    evidence={"permalink_status": site_status})

    # API says no, site error/unknown — degrade to MISSING but flag the uncertainty
    if not api_found:
        return _make(post, res, Classification.MISSING,
                    note=_missing_note(res) + (f" site={site_status}" if site_status is not None else ""),
                    evidence={
                        "micropub_status": res.micropub_status,
                        "permalink_status": site_status,
                    })

    found_url = res.found_url
    assert found_url is not None

    # API found it — now check what the site says
    if site_404:
        return _make(post, res, Classification.SITE_MISSING,
                    note="MB API has the post but the public site returns 404",
                    evidence={"permalink_status": site_status})
    if site_error:
        return _make(post, res, Classification.SITE_ERROR,
                    note=f"public site error: status={site_status}",
                    evidence={"permalink_status": site_status})

    # Resolved. Now classify drift.
    if res.strategy == Strategy.FUZZY:
        return _make(post, res, Classification.FUZZY_MATCH,
                    note=res.note or "matched only via fuzzy")

    if _is_relocated(post.url, found_url):
        return _make(post, res, Classification.RELOCATED,
                    note=f"BAR url={post.url} live url={found_url}")

    # Content drift (only when we can compare against live content)
    if res.matched_item is not None and res.matched_item.content_html:
        modified, why = is_modified(post.content_html, res.matched_item.content_html)
        if modified:
            return _make(post, res, Classification.MODIFIED, note=why)

    # Media broken?
    broken = _broken_media(post, media_status)
    if broken:
        return _make(post, res, Classification.MEDIA_BROKEN,
                    note=f"{len(broken)} media URL(s) unreachable",
                    evidence={"broken_urls": broken[:10]})

    # Host drift (BAR url and live url share the same path but the host changed)
    if _has_host_drift(post.url, found_url):
        return _make(post, res, Classification.METADATA_DRIFT,
                    note=f"host drift: BAR={post.url} live={found_url}")

    # Tag drift
    if res.matched_item is not None:
        if tuple(sorted(post.tags)) != tuple(sorted(res.matched_item.tags)):
            return _make(post, res, Classification.METADATA_DRIFT,
                        note=f"tags differ: bar={list(post.tags)} live={list(res.matched_item.tags)}")

    return _make(post, res, Classification.OK, note=res.note)


def _missing_note(res: ResolutionResult) -> str:
    bits = []
    if res.micropub_status is not None:
        bits.append(f"micropub={res.micropub_status}")
    if res.permalink_status is not None:
        bits.append(f"permalink={res.permalink_status}")
    if not bits:
        bits.append("no live source returned a match")
    return " ".join(bits)


def _is_relocated(expected_url: str, found_url: str) -> bool:
    if expected_url == found_url:
        return False
    if expected_url.rstrip("/") == found_url.rstrip("/"):
        return False
    # Same path, different host = host drift (metadata), not a real relocation.
    from urllib.parse import urlparse
    if urlparse(expected_url).path == urlparse(found_url).path:
        return False
    return True


def _has_host_drift(expected_url: str, found_url: str) -> bool:
    if not expected_url or not found_url:
        return False
    from urllib.parse import urlparse
    a, b = urlparse(expected_url), urlparse(found_url)
    return a.path == b.path and a.netloc != b.netloc


def _broken_media(post: Post, media_status: dict[str, int]) -> list[str]:
    out: list[str] = []
    for url in post.media_urls:
        st = media_status.get(url)
        if st is not None and (st == 0 or st >= 400):
            out.append(url)
    return out


def _extras_in_inventories(
    bar_posts_by_id: dict[str, Post],
    inventories: tuple[LiveInventory, ...],
) -> list[Finding]:
    """Live posts whose URL/path is not in the BAR. Expected when the live
    site is newer than the BAR — surfaced for completeness only."""
    from urllib.parse import urlparse

    out: list[Finding] = []
    seen_urls = {p.url for p in bar_posts_by_id.values() if p.url}
    seen_paths = {urlparse(p.url).path for p in bar_posts_by_id.values() if p.url}
    for inv in inventories:
        if not inv.is_complete:
            continue
        for li in inv.items:
            if not li.url:
                continue
            if li.url in seen_urls:
                continue
            if urlparse(li.url).path in seen_paths:
                # Same post the BAR has, just at a different host (legacy URL).
                continue
            out.append(
                Finding(
                    post_id=li.id or li.url,
                    expected_url=li.url,
                    classification=Classification.EXTRA,
                    severity=severity_of(Classification.EXTRA),
                    strategy=Strategy.LIVE_INVENTORY,
                    found_url=li.url,
                    note=f"on live ({inv.source.value}), not in BAR",
                    evidence={},
                )
            )
    return out


def _make(
    post: Post,
    res: ResolutionResult,
    classification: Classification,
    *,
    note: str = "",
    evidence: dict[str, Any] | None = None,
) -> Finding:
    return Finding(
        post_id=post.id,
        expected_url=post.url,
        classification=classification,
        severity=severity_of(classification),
        strategy=res.strategy,
        found_url=res.found_url,
        note=note,
        evidence=evidence or {},
    )
