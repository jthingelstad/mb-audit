"""Content drift detection.

A v1-honest implementation: extract visible text from rendered HTML using
selectolax, normalize whitespace, and compare lengths and a normalized
prefix. Anything more sophisticated (per-paragraph diff, image-tag
sensitivity) is layered on later.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

_WS_RE = re.compile(r"\s+")
_LENGTH_TOLERANCE = 0.05  # 5% length delta is "the same"
_PREFIX_LEN = 1500


def normalize_html(html: str) -> str:
    """Render HTML to whitespace-normalized visible text."""
    if not html:
        return ""
    tree = HTMLParser(html)
    text = tree.text(separator=" ", deep=True, strip=True) or ""
    return _WS_RE.sub(" ", text).strip()


def is_modified(bar_html: str, live_html: str) -> tuple[bool, str]:
    """Return (modified?, reason).

    Returns (False, "") if the post is unchanged within tolerance.
    """
    bar = normalize_html(bar_html)
    live = normalize_html(live_html)
    if bar == live:
        return False, ""
    if not bar or not live:
        return True, f"empty content side: bar={len(bar)} live={len(live)}"
    longer = max(len(bar), len(live))
    delta = abs(len(bar) - len(live)) / longer
    if delta > _LENGTH_TOLERANCE:
        return True, f"length differs by {delta:.0%} (bar={len(bar)} live={len(live)})"
    if bar[:_PREFIX_LEN] != live[:_PREFIX_LEN]:
        return True, "leading content differs"
    return False, ""
