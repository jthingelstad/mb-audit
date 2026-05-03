"""Concurrent permalink HEAD probes for every BAR post URL.

This is the second-source check against the *rendered* public site —
independent of what the MB API says exists. A post the API knows about
but the public site 404s on indicates a rendering/serving problem.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Iterable

import httpx


@dataclass(frozen=True, slots=True)
class PermalinkProbe:
    url: str
    status: int          # 0 if request errored
    final_url: str       # after redirects
    error: str | None = None


async def _probe(client: httpx.AsyncClient, url: str) -> PermalinkProbe:
    try:
        r = await client.head(url)
    except httpx.HTTPError as e:
        return PermalinkProbe(url=url, status=0, final_url=url, error=str(e))
    if r.status_code in (405, 501):
        # Some hosts disallow HEAD; range-GET 1 byte instead.
        try:
            r = await client.get(url, headers={"Range": "bytes=0-0"})
        except httpx.HTTPError as e:
            return PermalinkProbe(url=url, status=0, final_url=url, error=str(e))
    return PermalinkProbe(url=url, status=r.status_code, final_url=str(r.url))


async def probe_many(
    urls: Iterable[str],
    *,
    concurrency: int = 8,
    timeout: float = 15.0,
    on_progress: callable | None = None,  # type: ignore[type-arg]
) -> list[PermalinkProbe]:
    items = list(urls)
    sem = asyncio.Semaphore(concurrency)
    results: list[PermalinkProbe | None] = [None] * len(items)
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


def probe_all(urls: Iterable[str], **kwargs: object) -> list[PermalinkProbe]:
    return asyncio.run(probe_many(urls, **kwargs))  # type: ignore[arg-type]


def is_present(p: PermalinkProbe) -> bool:
    return 200 <= p.status < 400


def is_missing(p: PermalinkProbe) -> bool:
    return p.status == 404


def is_error(p: PermalinkProbe) -> bool:
    return p.status == 0 or 500 <= p.status < 600
