"""mb-audit CLI."""
from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from mb_audit.audit import media_cache, permalink_cache
from mb_audit.audit.media import MediaProbe, probe_all, probe_many as media_probe_many
from mb_audit.audit.media_reconciler import reconcile_media
from mb_audit.audit.permalinks import probe_many as permalink_probe_many
from mb_audit.audit.reconciler import reconcile
from mb_audit.audit.severity import Classification, MediaClassification
from mb_audit.bar import parse_bar
from mb_audit.bar.models import BarInventory
from mb_audit.live.feed import fetch_feed_inventory
from mb_audit.live.fetcher import Fetcher
from mb_audit.live.inventory import LiveInventory
from mb_audit.live import inventory_cache
from mb_audit.live.micropub import (
    DEFAULT_MICROPUB_ENDPOINT,
    fetch_config,
    fetch_full_inventory,
    pick_destination_uid,
)
from mb_audit.live.micropub_media import build_api_media_index
from mb_audit.live.resolver import resolve
from mb_audit.report.json_export import render_json
from mb_audit.report.markdown import render_markdown, write_report
from mb_audit.report.media_json import render_media_json
from mb_audit.report.media_markdown import render_media_markdown

app = typer.Typer(help="Audit a Micro.blog site against BAR backup files.")
console = Console()

REPORTS_ROOT = Path.home() / ".mb-audit" / "reports"
TOKEN_ENV = "MICROBLOG_TOKEN"


# ---------------- inspect ----------------

@app.command()
def inspect(
    bar: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                               help="Path to a BAR backup file."),
) -> None:
    """Parse a BAR and print a summary."""
    inv = parse_bar(bar)
    _print_inventory_table(inv)


def _print_inventory_table(inv: BarInventory) -> None:
    t = Table(title=f"BAR: {inv.source_path.name}", show_header=False)
    t.add_column("field", style="bold")
    t.add_column("value")
    t.add_row("host", inv.host or "(unknown)")
    t.add_row("feed title", inv.feed_title or "(none)")
    t.add_row("posts", str(inv.post_count))
    t.add_row("media", str(inv.media_count))
    if inv.date_range:
        a, b = inv.date_range
        t.add_row("date range", f"{a.date()} → {b.date()}")
    else:
        t.add_row("date range", "(no posts)")
    console.print(t)
    if inv.warnings:
        console.print("[yellow]Warnings:[/yellow]")
        for w in inv.warnings:
            console.print(f"  - {w}")


# ---------------- verify ----------------

@app.command()
def verify(
    bar: Path = typer.Option(..., "--bar", exists=True, dir_okay=False, readable=True,
                             help="Path to a BAR backup file."),
    site: Optional[str] = typer.Option(None, "--site",
                                       help="Public site URL for the second-source permalink audit. "
                                            "Defaults to the BAR's home_page_url. Pass '' to disable."),
    site_concurrency: int = typer.Option(8, "--site-concurrency", min=1, max=32,
                                          help="Concurrent HEAD requests against the public site."),
    site_refresh: bool = typer.Option(False, "--site-refresh",
                                       help="Bypass cached permalink probes and re-fetch."),
    micropub_endpoint: str = typer.Option(DEFAULT_MICROPUB_ENDPOINT, "--micropub-endpoint"),
    mp_destination: Optional[str] = typer.Option(
        None, "--mp-destination",
        help="Filter Micropub q=source by destination uid. Auto-detected from BAR if omitted.",
    ),
    page_size: int = typer.Option(200, "--page-size", min=1, max=200,
                                  help="Items per Micropub q=source page."),
    page_delay: float = typer.Option(
        0.5, "--page-delay", min=0.0,
        help="Seconds to wait between Micropub paginated requests (politeness).",
    ),
    refresh: bool = typer.Option(
        False, "--refresh",
        help="Bypass the cached Micropub inventory and re-fetch.",
    ),
    media_check: bool = typer.Option(False, "--media-check/--no-media-check",
                                     help="Probe every media URL referenced in the BAR. Slow."),
    media_concurrency: int = typer.Option(8, "--media-concurrency", min=1, max=64),
    limit: Optional[int] = typer.Option(None, "--limit", min=1,
                                        help="For dev: only audit the first N posts."),
) -> None:
    """Audit a BAR against the Micro.blog API (and optionally the live site)."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        console.print(
            f"[red]Missing {TOKEN_ENV}[/red]. Set your Micro.blog app token in the "
            f"environment. Generate one at https://micro.blog/account/apps."
        )
        raise typer.Exit(code=2)

    started = datetime.now().astimezone()
    console.print(f"[bold]Parsing BAR:[/bold] {bar}")
    inv = parse_bar(bar)
    _print_inventory_table(inv)

    posts = list(inv.posts if limit is None else inv.posts[:limit])
    if not posts:
        console.print("[yellow]No posts in BAR; nothing to verify.[/yellow]")
        raise typer.Exit(code=0)

    # Resolve mp_destination either from the flag or by matching the BAR's host
    # against the Micropub destinations list.
    if mp_destination is None and inv.home_page_url:
        try:
            cfg = fetch_config(token, micropub_endpoint)
        except Exception as e:
            console.print(f"[yellow]Could not fetch Micropub config: {e}[/yellow]")
            cfg = {}
        mp_destination = pick_destination_uid(cfg, inv.home_page_url)
        if mp_destination:
            console.print(f"Resolved mp-destination: [cyan]{mp_destination}[/cyan] "
                          f"(from BAR host {inv.host})")
        else:
            console.print(
                "[yellow]No Micropub destination matched the BAR host — "
                "querying across all blogs (may produce noisy 'relocated' results).[/yellow]"
            )

    cached = None if refresh else inventory_cache.load(
        endpoint=micropub_endpoint, mp_destination=mp_destination
    )
    if cached:
        mb_inventory, fetched_at = cached
        console.print(
            f"[green]Using cached MB inventory[/green] "
            f"({len(mb_inventory.items)} posts, fetched {fetched_at.isoformat(timespec='seconds')}). "
            f"Pass --refresh to re-fetch."
        )
    else:
        console.print(
            f"[bold]Fetching full inventory via Micropub q=source[/bold] "
            f"(endpoint={micropub_endpoint}, page_size={page_size}, "
            f"page_delay={page_delay}s)"
        )
        mb_inventory = _fetch_micropub_inventory(
            token=token,
            endpoint=micropub_endpoint,
            page_size=page_size,
            page_delay=page_delay,
            mp_destination=mp_destination,
        )
        console.print(f"  collected {len(mb_inventory.items)} posts from Micro.blog")
        cache_path = inventory_cache.save(
            mb_inventory, endpoint=micropub_endpoint, mp_destination=mp_destination
        )
        console.print(f"  cached at {cache_path}")

    inventories: list[LiveInventory] = [mb_inventory]
    permalink_client: httpx.Client | None = None

    # Default --site to the BAR's home_page_url unless caller passed empty string.
    effective_site = site if site is not None else inv.home_page_url
    if site == "":
        effective_site = ""

    if effective_site:
        console.print(f"[bold]Fetching public feed:[/bold] {effective_site}")
        try:
            with Fetcher() as f:
                feed_inv = fetch_feed_inventory(effective_site, f)
            inventories.append(feed_inv)
            console.print(f"  recent items in public feed: {len(feed_inv.items)}")
        except Exception as e:
            console.print(f"  [yellow]public feed fetch failed: {e}[/yellow]")
        permalink_client = httpx.Client(
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": "mb-audit/0.1"},
        )

    # Second-source check: HEAD every BAR post URL on the public site.
    permalink_status: dict[str, int] = {}
    if effective_site:
        permalink_status = _gather_permalink_status(
            posts=posts,
            site_url=effective_site,
            concurrency=site_concurrency,
            refresh=site_refresh,
        )

    media_status: dict[str, int] = {}
    if media_check:
        all_media: list[str] = []
        seen: set[str] = set()
        for p in posts:
            for u in p.media_urls:
                if u not in seen:
                    seen.add(u)
                    all_media.append(u)
        console.print(f"[bold]Probing {len(all_media)} media URL(s)[/bold] "
                      f"(concurrency={media_concurrency})")
        probes = probe_all(all_media, concurrency=media_concurrency)
        media_status = {p.url: p.status for p in probes}

    # Resolve each post.
    resolutions = []
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("classifying", total=len(posts))
        for post in posts:
            res = resolve(
                post,
                micropub_lookup=None,
                inventories=tuple(inventories),
                permalink_client=permalink_client,
            )
            resolutions.append(res)
            progress.advance(task)

    if permalink_client is not None:
        permalink_client.close()

    # Build a synthetic limited-bar inventory for the reconciler when --limit was used,
    # so `extras` only considers the same slice.
    bar_for_reconcile = inv if limit is None else _slice_inventory(inv, limit)

    findings = reconcile(
        bar=bar_for_reconcile,
        resolutions=resolutions,
        inventories=tuple(inventories),
        media_status=media_status,
        permalink_status=permalink_status,
    )
    finished = datetime.now().astimezone()

    _print_summary(findings, started, finished)

    run_id = _make_run_id(bar, started)
    md = render_markdown(bar_for_reconcile, effective_site, findings,
                         run_id=run_id, started_at=started, finished_at=finished)
    js = render_json(bar_for_reconcile, effective_site, findings,
                     run_id=run_id, started_at=started, finished_at=finished)
    out_dir = REPORTS_ROOT / run_id
    md_path, js_path = write_report(out_dir, markdown=md, json_blob=js)
    console.print(f"[green]Wrote report:[/green] {md_path}")
    console.print(f"[green]Wrote JSON:  [/green] {js_path}")

    n_missing = sum(1 for f in findings if f.classification.value == "missing")
    if n_missing:
        raise typer.Exit(code=1)


def _fetch_micropub_inventory(
    *,
    token: str,
    endpoint: str,
    page_size: int,
    page_delay: float,
    mp_destination: str | None,
) -> LiveInventory:
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=40),
        TextColumn("{task.completed} posts"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("micropub paginate", total=None)

        def on_page(items_so_far: int, last_size: int) -> None:
            progress.update(task, completed=items_so_far)

        return fetch_full_inventory(
            token=token,
            mp_destination=mp_destination,
            endpoint=endpoint,
            page_size=page_size,
            page_delay_sec=page_delay,
            on_page=on_page,
        )


def _gather_permalink_status(
    *,
    posts: list,
    site_url: str,
    concurrency: int,
    refresh: bool,
) -> dict[str, int]:
    """Probe HEAD against every BAR post URL on the public site.

    Uses an on-disk cache of {url: status}. Only newly-seen URLs are
    actually fetched on subsequent runs.
    """
    cached_pairs: dict[str, int] = {}
    cached_age: datetime | None = None
    if not refresh:
        loaded = permalink_cache.load(site_url=site_url)
        if loaded:
            probes, cached_age = loaded
            cached_pairs = {p.url: p.status for p in probes}

    target_urls = [p.url for p in posts]
    to_fetch = [u for u in target_urls if u not in cached_pairs]

    if cached_pairs and not refresh:
        console.print(
            f"[green]Permalink cache hit:[/green] {len(target_urls) - len(to_fetch)}/"
            f"{len(target_urls)} URLs (cached {cached_age.isoformat(timespec='seconds') if cached_age else '?'})"
        )

    new_probes_by_url: dict[str, int] = {}
    if to_fetch:
        console.print(
            f"[bold]Probing {len(to_fetch)} permalink(s) on {site_url}[/bold] "
            f"(concurrency={concurrency})"
        )
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("permalink HEAD", total=len(to_fetch))

            def on_progress(done: int, total: int) -> None:
                progress.update(task, completed=done)

            new_probes = asyncio.run(
                permalink_probe_many(
                    to_fetch,
                    concurrency=concurrency,
                    on_progress=on_progress,
                )
            )
        new_probes_by_url = {p.url: p.status for p in new_probes}

        # Persist merged cache (keep stale entries for URLs we didn't re-probe).
        merged = []
        from mb_audit.audit.permalinks import PermalinkProbe
        for url, status in cached_pairs.items():
            if url not in new_probes_by_url:
                merged.append(PermalinkProbe(url=url, status=status, final_url=url))
        merged.extend(new_probes)
        permalink_cache.save(merged, site_url=site_url)

    return {**cached_pairs, **new_probes_by_url}


def _print_summary(findings: list, started: datetime, finished: datetime) -> None:
    from collections import Counter
    by_class = Counter(f.classification.value for f in findings)
    t = Table(title="Summary", show_header=True, header_style="bold")
    t.add_column("classification")
    t.add_column("count", justify="right")
    for c in [
        "missing", "site_missing", "api_missing", "site_error",
        "relocated", "media_broken", "modified",
        "fuzzy_match", "metadata_drift", "extra", "ok",
    ]:
        t.add_row(c, str(by_class.get(c, 0)))
    console.print(t)
    console.print(f"[dim]Run took {(finished - started).total_seconds():.0f}s[/dim]")


def _make_run_id(bar: Path, started: datetime) -> str:
    h = hashlib.sha256()
    with bar.open("rb") as f:
        h.update(f.read(1 << 20))     # first 1 MB is enough to fingerprint a BAR
    return f"{started.strftime('%Y%m%dT%H%M%S')}-{h.hexdigest()[:8]}"


def _slice_inventory(inv: BarInventory, n: int) -> BarInventory:
    return BarInventory(
        source_path=inv.source_path,
        host=inv.host,
        home_page_url=inv.home_page_url,
        feed_title=inv.feed_title,
        posts=inv.posts[:n],
        media=inv.media,
        warnings=inv.warnings,
    )


# ---------------- verify-media ----------------

@app.command("verify-media")
def verify_media(
    bar: Path = typer.Option(..., "--bar", exists=True, dir_okay=False, readable=True),
    site: Optional[str] = typer.Option(
        None, "--site",
        help="Public site URL (e.g. https://example.com). Defaults to the BAR's home_page_url. Pass '' to disable site probing.",
    ),
    micropub_endpoint: str = typer.Option(DEFAULT_MICROPUB_ENDPOINT, "--micropub-endpoint"),
    mp_destination: Optional[str] = typer.Option(
        None, "--mp-destination",
        help="Filter MB API by destination uid. Auto-detected from BAR if omitted.",
    ),
    concurrency: int = typer.Option(8, "--concurrency", min=1, max=32,
                                    help="Concurrent HEAD requests against the public site."),
    media_refresh: bool = typer.Option(
        False, "--media-refresh",
        help="Bypass cached media probes and re-fetch.",
    ),
    inventory_refresh: bool = typer.Option(
        False, "--inventory-refresh",
        help="Bypass cached MB API inventory and re-fetch.",
    ),
    include_external: bool = typer.Option(
        False, "--include-external/--no-include-external",
        help="Also probe external (non-site) media URLs referenced from posts.",
    ),
) -> None:
    """Audit BAR media files against the Micro.blog API and the public site."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        console.print(
            f"[red]Missing {TOKEN_ENV}[/red]. Set your Micro.blog app token in the "
            f"environment. Generate one at https://micro.blog/account/apps."
        )
        raise typer.Exit(code=2)

    started = datetime.now().astimezone()
    console.print(f"[bold]Parsing BAR:[/bold] {bar}")
    inv = parse_bar(bar)
    _print_inventory_table(inv)

    if inv.media_count == 0:
        console.print("[yellow]BAR contains no media; nothing to audit.[/yellow]")
        raise typer.Exit(code=0)

    effective_site = site if site is not None else inv.home_page_url
    if site == "":
        effective_site = ""
    if not effective_site:
        console.print("[red]No site URL — cannot probe media.[/red]")
        raise typer.Exit(code=2)

    # ---- 1. MB API inventory (cached from `verify` if available) ----
    if mp_destination is None and inv.home_page_url:
        try:
            cfg = fetch_config(token, micropub_endpoint)
        except Exception as e:
            console.print(f"[yellow]Could not fetch Micropub config: {e}[/yellow]")
            cfg = {}
        mp_destination = pick_destination_uid(cfg, inv.home_page_url)
        if mp_destination:
            console.print(f"Resolved mp-destination: [cyan]{mp_destination}[/cyan]")
        else:
            console.print(
                "[yellow]No Micropub destination matched the BAR host — "
                "API view will include posts from all of the user's blogs.[/yellow]"
            )

    cached = None if inventory_refresh else inventory_cache.load(
        endpoint=micropub_endpoint, mp_destination=mp_destination
    )
    if cached:
        mb_inventory, fetched_at = cached
        console.print(
            f"[green]Using cached MB inventory[/green] "
            f"({len(mb_inventory.items)} posts, fetched {fetched_at.isoformat(timespec='seconds')})"
        )
    else:
        console.print(
            f"[bold]Fetching MB inventory[/bold] (endpoint={micropub_endpoint})"
        )
        mb_inventory = _fetch_micropub_inventory(
            token=token, endpoint=micropub_endpoint, page_size=200,
            page_delay=0.5, mp_destination=mp_destination,
        )
        inventory_cache.save(
            mb_inventory, endpoint=micropub_endpoint, mp_destination=mp_destination
        )

    api_index = build_api_media_index(mb_inventory)
    console.print(
        f"  API references {len(api_index.known)} unique media URL(s) "
        f"across {len(mb_inventory.items)} post(s)"
    )

    # ---- 2. Build the URL list to probe ----
    base = inv.home_page_url.rstrip("/") + "/"
    archive_urls = [base + a.path for a in inv.media]

    # External URLs referenced from BAR posts (optional)
    external_urls: list[str] = []
    if include_external:
        site_host = inv.host.lower()
        seen_ext: set[str] = set()
        for post in inv.posts:
            for u in post.media_urls:
                if u in seen_ext:
                    continue
                from urllib.parse import urlparse as _urlp
                if _urlp(u).netloc.lower() != site_host:
                    seen_ext.add(u)
                    external_urls.append(u)

    # Internal post-referenced URLs that are NOT in archive_urls (orphan_referenced)
    archive_set = set(archive_urls)
    extra_internal: list[str] = []
    seen_int: set[str] = set()
    for post in inv.posts:
        for u in post.media_urls:
            from urllib.parse import urlparse as _urlp
            if _urlp(u).netloc.lower() == inv.host.lower():
                if u not in archive_set and u not in seen_int:
                    seen_int.add(u)
                    extra_internal.append(u)

    all_urls = archive_urls + extra_internal + external_urls
    console.print(
        f"[bold]Probe targets:[/bold] {len(archive_urls)} archive + "
        f"{len(extra_internal)} internal-orphan + {len(external_urls)} external "
        f"= {len(all_urls)} total"
    )

    probes_by_url = _gather_media_status(
        urls=all_urls, site_url=effective_site,
        concurrency=concurrency, refresh=media_refresh,
    )

    # ---- 3. Reconcile ----
    findings = reconcile_media(
        bar=inv, probes=probes_by_url, api_index=api_index,
        classify_external=include_external,
    )
    finished = datetime.now().astimezone()

    _print_media_summary(findings, started, finished)

    # ---- 4. Write report ----
    run_id = _make_run_id(bar, started)
    md = render_media_markdown(inv, effective_site, findings,
                               run_id=run_id, started_at=started, finished_at=finished)
    js = render_media_json(inv, effective_site, findings,
                           run_id=run_id, started_at=started, finished_at=finished)
    out_dir = REPORTS_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "media-report.md"
    js_path = out_dir / "media-report.json"
    md_path.write_text(md)
    js_path.write_text(js)
    console.print(f"[green]Wrote media report:[/green] {md_path}")
    console.print(f"[green]Wrote JSON:        [/green] {js_path}")

    n_missing = sum(
        1 for f in findings if f.classification == MediaClassification.MISSING
    )
    if n_missing:
        raise typer.Exit(code=1)


def _gather_media_status(
    *,
    urls: list[str],
    site_url: str,
    concurrency: int,
    refresh: bool,
) -> dict[str, MediaProbe]:
    """Probe URLs with on-disk cache; only fetch new ones."""
    cached_by_url: dict[str, MediaProbe] = {}
    cached_age = None
    if not refresh:
        loaded = media_cache.load(site_url=site_url)
        if loaded:
            probes, cached_age = loaded
            cached_by_url = {p.url: p for p in probes}

    to_fetch = [u for u in urls if u not in cached_by_url]
    if cached_by_url and not refresh:
        console.print(
            f"[green]Media cache hit:[/green] {len(urls) - len(to_fetch)}/"
            f"{len(urls)} URLs (cached "
            f"{cached_age.isoformat(timespec='seconds') if cached_age else '?'})"
        )

    new_by_url: dict[str, MediaProbe] = {}
    if to_fetch:
        console.print(
            f"[bold]Probing {len(to_fetch)} media URL(s)[/bold] "
            f"(concurrency={concurrency})"
        )
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("media HEAD", total=len(to_fetch))

            def on_progress(done: int, total: int) -> None:
                progress.update(task, completed=done)

            new_probes = asyncio.run(
                media_probe_many(
                    to_fetch, concurrency=concurrency, on_progress=on_progress,
                )
            )
        new_by_url = {p.url: p for p in new_probes}

        # Persist merged cache.
        merged = list(cached_by_url.values()) + list(new_by_url.values())
        # Last-writer-wins on duplicates: rebuild from dict to dedup.
        dedup: dict[str, MediaProbe] = {}
        for p in merged:
            dedup[p.url] = p
        media_cache.save(list(dedup.values()), site_url=site_url)

    return {**cached_by_url, **new_by_url}


def _print_media_summary(findings: list, started: datetime, finished: datetime) -> None:
    from collections import Counter
    by_class = Counter(f.classification.value for f in findings)
    t = Table(title="Media summary", show_header=True, header_style="bold")
    t.add_column("classification")
    t.add_column("count", justify="right")
    for c in [
        "media_missing", "media_site_missing", "media_site_error",
        "media_size_mismatch", "media_orphan_referenced", "media_external_broken",
        "media_orphan_present", "media_ok",
    ]:
        t.add_row(c, str(by_class.get(c, 0)))
    console.print(t)
    console.print(f"[dim]Run took {(finished - started).total_seconds():.0f}s[/dim]")


# ---------------- stubs for v2 commands ----------------

@app.command()
def diff(
    bar1: Path = typer.Argument(..., exists=True),
    bar2: Path = typer.Argument(..., exists=True),
) -> None:
    """Compare two BAR files. (Not yet implemented in v1.)"""
    console.print("[yellow]`diff` is not yet implemented in v1.[/yellow]")
    raise typer.Exit(code=2)


@app.command()
def repair(
    bar: Path = typer.Option(..., "--bar"),
    execute: bool = typer.Option(False, "--execute"),
) -> None:
    """Republish missing posts via Micropub. (Not yet implemented in v1.)"""
    console.print("[yellow]`repair` is not yet implemented in v1.[/yellow]")
    raise typer.Exit(code=2)


@app.command()
def history() -> None:
    """List previous runs. (Not yet implemented in v1.)"""
    console.print("[yellow]`history` is not yet implemented in v1.[/yellow]")
    raise typer.Exit(code=2)


@app.command()
def report(run_id: str = typer.Argument(...)) -> None:
    """Re-render a previous run's report. (Not yet implemented in v1.)"""
    console.print("[yellow]`report` is not yet implemented in v1.[/yellow]")
    raise typer.Exit(code=2)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
