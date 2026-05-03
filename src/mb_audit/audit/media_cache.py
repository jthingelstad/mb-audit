"""On-disk cache for media probe results (URL -> {status, size, final_url})."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from mb_audit.audit.media import MediaProbe

DEFAULT_CACHE_ROOT = Path.home() / ".mb-audit" / "cache" / "media"


def cache_path(site_url: str, *, cache_root: Path = DEFAULT_CACHE_ROOT) -> Path:
    digest = hashlib.sha256(site_url.encode("utf-8")).hexdigest()[:16]
    return cache_root / f"{digest}.json"


def save(
    probes: list[MediaProbe],
    *,
    site_url: str,
    cache_root: Path = DEFAULT_CACHE_ROOT,
) -> Path:
    path = cache_path(site_url, cache_root=cache_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now().astimezone().isoformat(),
        "site_url": site_url,
        "probes": [
            {
                "url": p.url,
                "status": p.status,
                "size": p.size,
                "final_url": p.final_url,
                "error": p.error,
            }
            for p in probes
        ],
    }
    path.write_text(json.dumps(payload))
    return path


def load(
    *,
    site_url: str,
    cache_root: Path = DEFAULT_CACHE_ROOT,
) -> tuple[list[MediaProbe], datetime] | None:
    path = cache_path(site_url, cache_root=cache_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    probes = [
        MediaProbe(
            url=p["url"],
            status=int(p["status"]),
            error=p.get("error"),
            final_url=p.get("final_url") or p["url"],
            size=p.get("size"),
        )
        for p in payload.get("probes") or []
    ]
    fetched_at = datetime.fromisoformat(payload["fetched_at"])
    return probes, fetched_at
