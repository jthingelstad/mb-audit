"""Media reachability checks via concurrent HEAD requests."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Iterable

import httpx


@dataclass(frozen=True, slots=True)
class MediaProbe:
    url: str
    status: int          # 0 if request errored
    error: str | None
    final_url: str       # after redirects, when known
    size: int | None = None  # Content-Length from HEAD, when present


def _content_length(r: httpx.Response) -> int | None:
    raw = r.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def _probe(client: httpx.AsyncClient, url: str) -> MediaProbe:
    try:
        r = await client.head(url)
    except httpx.HTTPError as e:
        return MediaProbe(url=url, status=0, error=str(e), final_url=url)
    # Some hosts disallow HEAD; fall back to a 1-byte ranged GET.
    if r.status_code in (405, 501):
        try:
            r = await client.get(url, headers={"Range": "bytes=0-0"})
        except httpx.HTTPError as e:
            return MediaProbe(url=url, status=0, error=str(e), final_url=url)
    return MediaProbe(
        url=url,
        status=r.status_code,
        error=None,
        final_url=str(r.url),
        size=_content_length(r),
    )


async def probe_many(
    urls: Iterable[str],
    *,
    concurrency: int = 8,
    timeout: float = 15.0,
    on_progress: callable | None = None,  # type: ignore[type-arg]
) -> list[MediaProbe]:
    items = list(urls)
    sem = asyncio.Semaphore(concurrency)
    results: list[MediaProbe | None] = [None] * len(items)
    completed = 0

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": "mb-audit/0.1"},
    ) as client:
        async def one(i: int, url: str) -> None:
            nonlocal completed
            async with sem:
                results[i] = await _probe(client, url)
                completed += 1
                if on_progress is not None:
                    on_progress(completed, len(items))

        await asyncio.gather(*(one(i, u) for i, u in enumerate(items)))
    return [r for r in results if r is not None]


def probe_all(urls: Iterable[str], **kwargs: object) -> list[MediaProbe]:
    return asyncio.run(probe_many(urls, **kwargs))  # type: ignore[arg-type]


def is_broken(p: MediaProbe) -> bool:
    if p.status == 0:
        return True
    return p.status >= 400
