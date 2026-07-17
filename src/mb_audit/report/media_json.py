"""Stable-key JSON export for media findings."""

from __future__ import annotations

import json
from datetime import datetime

from mb_audit.audit.media_reconciler import MediaFinding
from mb_audit.bar.models import BarInventory


def render_media_json(
    bar: BarInventory,
    site_url: str,
    findings: list[MediaFinding],
    *,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
) -> str:
    payload = {
        "run_id": run_id,
        "kind": "media",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "bar": {
            "path": str(bar.source_path),
            "host": bar.host,
            "media_count": bar.media_count,
            "post_count": bar.post_count,
        },
        "site": {"url": site_url},
        "findings": [
            {
                "bar_path": f.bar_path,
                "expected_url": f.expected_url,
                "classification": f.classification.value,
                "severity": f.severity.value,
                "bar_size": f.bar_size,
                "live_status": f.live_status,
                "live_size": f.live_size,
                "final_url": f.final_url,
                "note": f.note,
                "evidence": f.evidence,
            }
            for f in findings
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)
