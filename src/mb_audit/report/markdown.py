"""Markdown report renderer."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable

from mb_audit.audit.reconciler import Finding
from mb_audit.audit.severity import Classification, Severity
from mb_audit.bar.models import BarInventory

_SEVERITY_ORDER = [
    Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM,
    Severity.LOW, Severity.INFO, Severity.OK,
]


def render_markdown(
    bar: BarInventory,
    site_url: str,
    findings: list[Finding],
    *,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
) -> str:
    by_class = Counter(f.classification for f in findings)
    by_sev = Counter(f.severity for f in findings)
    total = len(findings)
    n_missing = by_class[Classification.MISSING]
    n_relocated = by_class[Classification.RELOCATED]
    n_modified = by_class[Classification.MODIFIED]
    n_media = by_class[Classification.MEDIA_BROKEN]
    n_fuzzy = by_class[Classification.FUZZY_MATCH]
    n_drift = by_class[Classification.METADATA_DRIFT]
    n_extra = by_class[Classification.EXTRA]
    n_ok = by_class[Classification.OK]

    parts: list[str] = []
    parts.append(f"# mb-audit report `{run_id}`")
    parts.append("")
    parts.append(f"- **BAR:** `{bar.source_path.name}`")
    parts.append(f"- **Site:** {site_url or '(none)'}")
    parts.append(f"- **BAR posts:** {bar.post_count}")
    parts.append(f"- **BAR media:** {bar.media_count}")
    if bar.warnings:
        parts.append(f"- **BAR warnings:** {len(bar.warnings)}")
        for w in bar.warnings:
            parts.append(f"  - {w}")
    parts.append(f"- **Run:** {started_at.isoformat(timespec='seconds')} → "
                 f"{finished_at.isoformat(timespec='seconds')} "
                 f"({(finished_at - started_at).total_seconds():.0f}s)")
    parts.append("")
    parts.append(
        "> The BAR is the source of truth. "
        "**`missing`** posts (in BAR, not on the live site) are the headline finding. "
        "**`extra`** posts (on the live site, not in this BAR) are expected — "
        "the live site is newer than the BAR."
    )
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
    for c in [
        Classification.MISSING, Classification.SITE_MISSING, Classification.API_MISSING,
        Classification.SITE_ERROR, Classification.RELOCATED, Classification.MEDIA_BROKEN,
        Classification.MODIFIED, Classification.FUZZY_MATCH, Classification.METADATA_DRIFT,
        Classification.EXTRA, Classification.OK,
    ]:
        parts.append(f"| {c.value} | {by_class[c]} |")
    parts.append("")

    # Detail sections, only when non-empty, in severity order.
    for sev in _SEVERITY_ORDER:
        relevant = [f for f in findings if f.severity == sev and f.classification != Classification.OK]
        if not relevant:
            continue
        parts.append(f"## {sev.value.title()} — {len(relevant)} finding(s)")
        parts.append("")
        for f in relevant[:500]:  # cap per-section to keep reports readable
            parts.append(f"- **{f.classification.value}** [`{f.expected_url}`]({f.expected_url})")
            if f.found_url and f.found_url != f.expected_url:
                parts.append(f"  - found at: {f.found_url}")
            if f.strategy.value != "none":
                parts.append(f"  - resolver: `{f.strategy.value}`")
            if f.note:
                parts.append(f"  - note: {f.note}")
            for k, v in f.evidence.items():
                parts.append(f"  - {k}: `{v}`")
        if len(relevant) > 500:
            parts.append(f"_…and {len(relevant) - 500} more (see JSON manifest)_")
        parts.append("")

    n_site_missing = by_class[Classification.SITE_MISSING]
    n_api_missing = by_class[Classification.API_MISSING]
    if n_missing or n_relocated or n_site_missing or n_api_missing:
        parts.append("## Manton Email Summary")
        parts.append("")
        parts.append(_manton_block(
            bar, site_url, findings,
            n_missing=n_missing, n_relocated=n_relocated,
            n_site_missing=n_site_missing, n_api_missing=n_api_missing,
        ))
        parts.append("")

    return "\n".join(parts)


def _manton_block(
    bar: BarInventory,
    site_url: str,
    findings: list[Finding],
    *,
    n_missing: int,
    n_relocated: int,
    n_site_missing: int,
    n_api_missing: int,
) -> str:
    missing = [f for f in findings if f.classification == Classification.MISSING][:25]
    site_missing = [f for f in findings if f.classification == Classification.SITE_MISSING][:25]
    api_missing = [f for f in findings if f.classification == Classification.API_MISSING][:25]
    relocated = [f for f in findings if f.classification == Classification.RELOCATED][:25]

    lines = [
        f"Hi Manton — `mb-audit` against `{site_url}` using BAR "
        f"`{bar.source_path.name}` ({bar.post_count} posts):",
        "",
        f"- **{n_missing} missing** (absent in both API and site — the headline finding)",
        f"- **{n_site_missing} site-missing** (API has it, public URL 404s — likely BAR-export slug truncation)",
        f"- **{n_api_missing} api-missing** (site renders it, API does not return it)",
        f"- **{n_relocated} relocated** (URL changed)",
        "",
    ]
    if missing:
        lines.append("### Missing posts (critical)")
        lines.append("")
        for f in missing:
            ev = _evidence_short(f)
            lines.append(f"- {f.expected_url}{ev}")
        if n_missing > len(missing):
            lines.append(f"- …and {n_missing - len(missing)} more (full list in `report.json`)")
        lines.append("")
    if site_missing:
        lines.append("### Site-missing (URL 404s on site, post exists in API)")
        lines.append("")
        for f in site_missing:
            lines.append(f"- BAR `{f.expected_url}` → API `{f.found_url}`")
        if n_site_missing > len(site_missing):
            lines.append(f"- …and {n_site_missing - len(site_missing)} more")
        lines.append("")
    if api_missing:
        lines.append("### API-missing (site renders it, API hides it)")
        lines.append("")
        for f in api_missing:
            lines.append(f"- {f.expected_url}")
        lines.append("")
    if relocated:
        lines.append("### Relocated (URL changed)")
        lines.append("")
        for f in relocated:
            lines.append(f"- expected `{f.expected_url}`, found `{f.found_url}`")
        lines.append("")
    return "\n".join(lines)


def _evidence_short(f: Finding) -> str:
    bits = []
    if f.evidence.get("micropub_status") is not None:
        bits.append(f"micropub={f.evidence['micropub_status']}")
    if f.evidence.get("permalink_status") is not None:
        bits.append(f"site={f.evidence['permalink_status']}")
    return f" ({', '.join(bits)})" if bits else ""


def write_report(directory: Path, *, markdown: str, json_blob: str) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    md_path = directory / "report.md"
    json_path = directory / "report.json"
    md_path.write_text(markdown)
    json_path.write_text(json_blob)
    return md_path, json_path
