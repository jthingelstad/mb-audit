"""Direct permalink probes for posts that aren't in the live feed."""
from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class PermalinkResult:
    url: str
    status: int
    final_url: str    # after redirects
    fetched_html: str | None  # only populated when content was needed


def head_permalink(client: httpx.Client, url: str) -> PermalinkResult:
    try:
        r = client.head(url)
    except httpx.HTTPError as e:
        return PermalinkResult(url=url, status=0, final_url=str(e), fetched_html=None)
    return PermalinkResult(
        url=url, status=r.status_code, final_url=str(r.url), fetched_html=None
    )


def get_permalink(client: httpx.Client, url: str) -> PermalinkResult:
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        return PermalinkResult(url=url, status=0, final_url=str(e), fetched_html=None)
    return PermalinkResult(
        url=url,
        status=r.status_code,
        final_url=str(r.url),
        fetched_html=r.text if r.status_code == 200 else None,
    )
