"""Classification kinds and their severities."""
from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"
    OK = "ok"


class Classification(str, Enum):
    OK = "ok"
    MISSING = "missing"               # API and site both can't find it
    SITE_MISSING = "site_missing"     # API has it, public site 404s
    API_MISSING = "api_missing"       # site renders it, API doesn't return it
    SITE_ERROR = "site_error"         # public site returned 5xx / connection error
    RELOCATED = "relocated"
    MEDIA_BROKEN = "media_broken"
    MODIFIED = "modified"
    METADATA_DRIFT = "metadata_drift"
    EXTRA = "extra"
    FUZZY_MATCH = "fuzzy_match"


_SEVERITY: dict[Classification, Severity] = {
    Classification.OK: Severity.OK,
    Classification.MISSING: Severity.CRITICAL,
    Classification.SITE_MISSING: Severity.HIGH,
    Classification.API_MISSING: Severity.HIGH,
    Classification.SITE_ERROR: Severity.MEDIUM,
    Classification.RELOCATED: Severity.HIGH,
    Classification.MEDIA_BROKEN: Severity.HIGH,
    Classification.MODIFIED: Severity.MEDIUM,
    Classification.METADATA_DRIFT: Severity.LOW,
    Classification.EXTRA: Severity.INFO,
    Classification.FUZZY_MATCH: Severity.MEDIUM,
}


def severity_of(c: Classification) -> Severity:
    return _SEVERITY[c]


class MediaClassification(str, Enum):
    OK = "media_ok"
    MISSING = "media_missing"                       # site 404 + no API post references it
    SITE_MISSING = "media_site_missing"             # site 404, API post still references it
    ORPHAN_PRESENT = "media_orphan_present"         # site 200, no API post references it
    SIZE_MISMATCH = "media_size_mismatch"           # both 200, Content-Length differs
    SITE_ERROR = "media_site_error"                 # 5xx / connection error
    ORPHAN_REFERENCED = "media_orphan_referenced"   # post points at uploads/X but X not in BAR
    EXTERNAL_BROKEN = "media_external_broken"       # external CDN URL 404s


_MEDIA_SEVERITY: dict[MediaClassification, Severity] = {
    MediaClassification.OK: Severity.OK,
    MediaClassification.MISSING: Severity.CRITICAL,
    MediaClassification.SITE_MISSING: Severity.HIGH,
    MediaClassification.ORPHAN_PRESENT: Severity.INFO,
    MediaClassification.SIZE_MISMATCH: Severity.MEDIUM,
    MediaClassification.SITE_ERROR: Severity.MEDIUM,
    MediaClassification.ORPHAN_REFERENCED: Severity.HIGH,
    MediaClassification.EXTERNAL_BROKEN: Severity.MEDIUM,
}


def media_severity_of(c: MediaClassification) -> Severity:
    return _MEDIA_SEVERITY[c]
