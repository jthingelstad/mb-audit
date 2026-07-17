"""Media reconciler — pure functions over BAR + probes + API-known set.

Output is a list of `MediaFinding`s, one per BAR media asset (plus per
external/orphan-referenced URL discovered through posts).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from mb_audit.audit.media import MediaProbe
from mb_audit.audit.severity import (
    MediaClassification,
    Severity,
    media_severity_of,
)
from mb_audit.bar.models import BarInventory, MediaAsset
from mb_audit.live.micropub_media import ApiMediaIndex


@dataclass(frozen=True, slots=True)
class MediaFinding:
    bar_path: str  # uploads/YYYY/...  ("" for finds discovered via post refs)
    expected_url: str
    classification: MediaClassification
    severity: Severity
    bar_size: int | None
    live_status: int | None
    live_size: int | None
    final_url: str | None
    note: str
    evidence: dict[str, Any] = field(default_factory=dict)


def reconcile_media(
    *,
    bar: BarInventory,
    probes: dict[str, MediaProbe],
    api_index: ApiMediaIndex,
    classify_external: bool = True,
) -> list[MediaFinding]:
    findings: list[MediaFinding] = []

    site_host = urlparse(bar.home_page_url).netloc.lower()
    bar_url_prefix = bar.home_page_url.rstrip("/") + "/"

    # Track URLs we've already produced findings for (so we don't double-count
    # post-referenced URLs that are also in bar_paths).
    seen_urls: set[str] = set()

    # --- Pass 1: every file in the BAR's uploads/ ---
    for asset in bar.media:
        expected = bar_url_prefix + asset.path
        seen_urls.add(expected)
        probe = probes.get(expected)
        api_known = expected in api_index.known

        findings.append(_classify_archive(asset, expected, probe, api_known, api_index))

    # --- Pass 2: post-referenced URLs that point at our own host but are
    #            not in the BAR archive (orphan_referenced).            ---
    for post in bar.posts:
        for media_url in post.media_urls:
            if media_url in seen_urls:
                continue
            host = urlparse(media_url).netloc.lower()
            if host == site_host:
                # Internal URL not in BAR uploads → orphan reference
                seen_urls.add(media_url)
                probe = probes.get(media_url)
                findings.append(
                    _make_finding(
                        bar_path="",
                        expected_url=media_url,
                        classification=MediaClassification.ORPHAN_REFERENCED,
                        bar_size=None,
                        probe=probe,
                        note=f"referenced by post {post.url} but not in BAR uploads/",
                        evidence={"referenced_by": post.url},
                    )
                )
            else:
                # External URL — classify on probe status, but only if we
                # actually probed it. Without a probe, stay silent rather
                # than slander third-party hosts.
                seen_urls.add(media_url)
                if not classify_external:
                    continue
                probe = probes.get(media_url)
                if probe is None:
                    continue
                if 200 <= probe.status < 400:
                    cls = MediaClassification.OK
                    note = "external media reachable"
                elif probe.status == 0:
                    cls = MediaClassification.SITE_ERROR
                    note = f"external HEAD errored: {probe.error or '?'}"
                else:
                    cls = MediaClassification.EXTERNAL_BROKEN
                    note = f"external media unreachable (status={probe.status})"
                findings.append(
                    _make_finding(
                        bar_path="",
                        expected_url=media_url,
                        classification=cls,
                        bar_size=None,
                        probe=probe,
                        note=note,
                        evidence={"referenced_by": post.url},
                    )
                )

    return findings


def _classify_archive(
    asset: MediaAsset,
    expected_url: str,
    probe: MediaProbe | None,
    api_known: bool,
    api_index: ApiMediaIndex,
) -> MediaFinding:
    if probe is None:
        # Unprobed — usually means the cache is stale; treat as inconclusive.
        return _make_finding(
            bar_path=asset.path,
            expected_url=expected_url,
            classification=MediaClassification.SITE_ERROR,
            bar_size=asset.size_bytes,
            probe=None,
            note="not probed",
        )

    status = probe.status
    if status == 404:
        if api_known:
            refs = api_index.referencing_posts(expected_url)
            return _make_finding(
                bar_path=asset.path,
                expected_url=expected_url,
                classification=MediaClassification.SITE_MISSING,
                bar_size=asset.size_bytes,
                probe=probe,
                note=(f"site 404 but {len(refs)} live post(s) still reference this media"),
                evidence={"referenced_by": list(refs[:5])},
            )
        return _make_finding(
            bar_path=asset.path,
            expected_url=expected_url,
            classification=MediaClassification.MISSING,
            bar_size=asset.size_bytes,
            probe=probe,
            note="site 404 and no live post references this URL",
        )

    if status == 0 or 500 <= status < 600:
        return _make_finding(
            bar_path=asset.path,
            expected_url=expected_url,
            classification=MediaClassification.SITE_ERROR,
            bar_size=asset.size_bytes,
            probe=probe,
            note=f"transport error / {status}: {probe.error or ''}".strip(),
        )

    if 200 <= status < 400:
        # Present. Size match check, then orphan check.
        if probe.size is not None and asset.size_bytes and probe.size != asset.size_bytes:
            return _make_finding(
                bar_path=asset.path,
                expected_url=expected_url,
                classification=MediaClassification.SIZE_MISMATCH,
                bar_size=asset.size_bytes,
                probe=probe,
                note=f"BAR size={asset.size_bytes} live size={probe.size}",
            )
        if not api_known:
            return _make_finding(
                bar_path=asset.path,
                expected_url=expected_url,
                classification=MediaClassification.ORPHAN_PRESENT,
                bar_size=asset.size_bytes,
                probe=probe,
                note="present on site; not referenced by any live post",
            )
        return _make_finding(
            bar_path=asset.path,
            expected_url=expected_url,
            classification=MediaClassification.OK,
            bar_size=asset.size_bytes,
            probe=probe,
            note="",
        )

    # Catch-all (3xx without redirect resolved, 4xx other than 404, etc.)
    return _make_finding(
        bar_path=asset.path,
        expected_url=expected_url,
        classification=MediaClassification.SITE_ERROR,
        bar_size=asset.size_bytes,
        probe=probe,
        note=f"unexpected status {status}",
    )


def _make_finding(
    *,
    bar_path: str,
    expected_url: str,
    classification: MediaClassification,
    bar_size: int | None,
    probe: MediaProbe | None,
    note: str = "",
    evidence: dict[str, Any] | None = None,
) -> MediaFinding:
    return MediaFinding(
        bar_path=bar_path,
        expected_url=expected_url,
        classification=classification,
        severity=media_severity_of(classification),
        bar_size=bar_size,
        live_status=probe.status if probe else None,
        live_size=probe.size if probe else None,
        final_url=probe.final_url if probe else None,
        note=note,
        evidence=evidence or {},
    )
