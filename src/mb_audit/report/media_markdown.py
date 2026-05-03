"""Markdown renderer for media-audit findings."""
from __future__ import annotations

from collections import Counter
from datetime import datetime

from mb_audit.audit.media_reconciler import MediaFinding
from mb_audit.audit.severity import MediaClassification, Severity
from mb_audit.bar.models import BarInventory

_SEVERITY_ORDER = [
    Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM,
    Severity.LOW, Severity.INFO, Severity.OK,
]

_CLASS_ORDER = [
    MediaClassification.MISSING,
    MediaClassification.SITE_MISSING,
    MediaClassification.SITE_ERROR,
    MediaClassification.SIZE_MISMATCH,
    MediaClassification.ORPHAN_REFERENCED,
    MediaClassification.EXTERNAL_BROKEN,
    MediaClassification.ORPHAN_PRESENT,
    MediaClassification.OK,
]


def render_media_markdown(
    bar: BarInventory,
    site_url: str,
    findings: list[MediaFinding],
    *,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
) -> str:
    by_class = Counter(f.classification for f in findings)
    by_sev = Counter(f.severity for f in findings)
    total = len(findings)

    parts: list[str] = []
    parts.append(f"# mb-audit media report `{run_id}`")
    parts.append("")
    parts.append(f"- **BAR:** `{bar.source_path.name}`")
    parts.append(f"- **Site:** {site_url or '(none)'}")
    parts.append(f"- **BAR media files:** {bar.media_count}")
    parts.append(f"- **Total media findings:** {total}")
    parts.append(f"- **Run:** {started_at.isoformat(timespec='seconds')} → "
                 f"{finished_at.isoformat(timespec='seconds')} "
                 f"({(finished_at - started_at).total_seconds():.0f}s)")
    parts.append("")
    parts.append("> The BAR's `uploads/` directory is the source of truth. "
                 "**`media_missing`** (BAR has the file, site 404s, no API post references it) "
                 "is the headline finding. **`media_site_missing`** indicates a rendering issue: "
                 "API still references the file, but the public URL 404s.")
    parts.append("")

    parts.append("## Summary")
    parts.append("")
    parts.append("| Severity | Count |")
    parts.append("|---|---:|")
    for sev in _SEVERITY_ORDER:
        parts.append(f"| {sev.value} | {by_sev[sev]} |")
    parts.append(f"| **total** | **{total}** |")
    parts.append("")
    parts.append("| Classification | Count |")
    parts.append("|---|---:|")
    for c in _CLASS_ORDER:
        parts.append(f"| {c.value} | {by_class[c]} |")
    parts.append("")

    for sev in _SEVERITY_ORDER:
        relevant = [f for f in findings
                    if f.severity == sev and f.classification != MediaClassification.OK]
        if not relevant:
            continue
        parts.append(f"## {sev.value.title()} — {len(relevant)} finding(s)")
        parts.append("")
        for f in relevant[:500]:
            parts.append(f"- **{f.classification.value}** [`{f.expected_url}`]({f.expected_url})")
            if f.bar_path:
                parts.append(f"  - bar path: `{f.bar_path}`")
            if f.live_status is not None:
                parts.append(f"  - live status: `{f.live_status}`"
                             + (f", live size: {f.live_size}" if f.live_size is not None else ""))
            if f.bar_size is not None:
                parts.append(f"  - bar size: {f.bar_size}")
            if f.note:
                parts.append(f"  - note: {f.note}")
            for k, v in f.evidence.items():
                parts.append(f"  - {k}: `{v}`")
        if len(relevant) > 500:
            parts.append(f"_…and {len(relevant) - 500} more (see JSON manifest)_")
        parts.append("")

    n_missing = by_class[MediaClassification.MISSING]
    n_site_missing = by_class[MediaClassification.SITE_MISSING]
    n_orphan_ref = by_class[MediaClassification.ORPHAN_REFERENCED]
    if n_missing or n_site_missing or n_orphan_ref:
        parts.append("## Manton Email Summary (media)")
        parts.append("")
        parts.append(f"`mb-audit verify-media` against `{site_url}` using BAR "
                     f"`{bar.source_path.name}` ({bar.media_count} media files):")
        parts.append("")
        parts.append(f"- **{n_missing} media_missing** (BAR has the file, site 404s, "
                     f"no live post references it)")
        parts.append(f"- **{n_site_missing} media_site_missing** (BAR has the file, site 404s, "
                     f"a live post still references it — rendering issue)")
        parts.append(f"- **{n_orphan_ref} media_orphan_referenced** (post URL points at "
                     f"uploads/X but X is absent from the BAR archive)")
        parts.append("")

    return "\n".join(parts)
