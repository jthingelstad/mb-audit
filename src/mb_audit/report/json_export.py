"""Stable-key JSON export for findings."""

from __future__ import annotations

import json
from datetime import datetime

from mb_audit.audit.reconciler import Finding
from mb_audit.bar.models import BarInventory


def render_json(
    bar: BarInventory,
    site_url: str,
    findings: list[Finding],
    *,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
) -> str:
    payload = {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "bar": {
            "path": str(bar.source_path),
            "host": bar.host,
            "feed_title": bar.feed_title,
            "post_count": bar.post_count,
            "media_count": bar.media_count,
            "warnings": list(bar.warnings),
        },
        "site": {"url": site_url},
        "findings": [
            {
                "post_id": f.post_id,
                "expected_url": f.expected_url,
                "found_url": f.found_url,
                "classification": f.classification.value,
                "severity": f.severity.value,
                "strategy": f.strategy.value,
                "note": f.note,
                "evidence": f.evidence,
            }
            for f in findings
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)
