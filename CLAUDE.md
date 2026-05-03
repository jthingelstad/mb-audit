# CLAUDE.md — mb-audit

Guidance for Claude Code when working in this repository.

## What this project is

`mb-audit` is a Python CLI tool that audits a Micro.blog–hosted site against one or more Micro.blog backup (BAR) files. The BAR file is treated as the trusted historical source of truth; the live site is verified to still contain everything the backup says it should.

The motivating problem: Micro.blog has, on rare occasions, "lost" posts. Without tooling, this is hard to detect and harder to report. A user may have several BAR files spanning years, but those backups are currently only useful for a full migration to another platform. This tool makes them useful as forensic and verification artifacts against the running site.

## Primary use cases

1. **Verify completeness.** Given a BAR file and a live Micro.blog site, confirm every post in the BAR is present on the live site.
2. **Detect drift.** Identify posts that have been modified, relocated (URL changed), or whose media has broken since the backup.
3. **Generate evidence.** Produce a tight, factual report suitable for emailing Micro.blog support (Manton) when posts appear to be missing.
4. **Compare backups.** Diff two BAR files against each other to detect MB-side changes between backup dates.
5. **Repair (opt-in).** Republish missing posts to the live site via Micropub, with explicit per-post confirmation.

## Design principles

- **Audits never mutate.** `verify` is strictly read-only. Only `repair` writes, and only with explicit confirmation per post.
- **BAR is truth, live site is suspect.** All matching strategies start from the BAR and look outward.
- **Failure modes are tiered.** Missing posts are critical; metadata drift is informational. Severity drives report prominence.
- **Resumable and cached.** Runs against the same BAR or same site should reuse prior work. Network is the slow part.
- **Single-user, single-site by default.** No multi-tenancy, no service mode. Local CLI tool, run by the owner.
- **Open standards.** Use Micropub for both reading (`q=source`) and writing. Avoid MB-specific endpoints when an IndieWeb equivalent exists.
- **Minimal dependencies.** Standard library where possible. Each added dependency must justify itself.

## Architecture overview

```
BAR file(s) ─→ Parser ─→ Expected Inventory ─┐
                                             ├─→ Reconciler ─→ Report
Live site  ─→ Collectors ─→ Actual State ────┘
                                             │
                                  Repair (opt-in, separate command)
```

Modules:

- `bar/` — Parse BAR files into normalized `Post` and `Media` objects. Build expected inventories. Diff two BARs.
- `live/` — Probe the live site via Micropub (`q=source`), JSON Feed, and direct permalink fetches. Implement the multi-strategy post resolver.
- `audit/` — Reconcile expected vs. actual. Classify results. Run content and media checks.
- `repair/` — Interactive Micropub-based republishing of missing posts. Strictly opt-in.
- `report/` — Markdown report, JSON manifest, and a tight "Manton email" summary section.
- `store/` — SQLite for run history and caching. Append-only run log.
- `cli.py` — Typer-based command surface.

## Commands

| Command | Purpose | Mutates? |
|---|---|---|
| `mb-audit inspect <bar>` | Parse a BAR and summarize its contents | No |
| `mb-audit verify --bar <bar> --site <url>` | Full audit, generate report | No |
| `mb-audit diff <bar1> <bar2>` | Compare two BAR files | No |
| `mb-audit repair --bar <bar>` | Interactive republish of missing posts | **Yes** (Micropub) |
| `mb-audit report <run-id>` | Re-render a past run's report | No |
| `mb-audit history` | List previous runs | No |

## Authentication

- **Verify mode requires no auth** for public Micro.blog sites. The live site is fetched as any reader would.
- **`q=source` queries via Micropub** require a Micro.blog app token. Without one, the auditor falls back to public feeds and HTML crawling, which is less authoritative.
- **Repair mode requires a Micro.blog app token.** The user generates this at `account.micro.blog` (Account → App Tokens). The token is read from the `MICROBLOG_TOKEN` environment variable, never stored in config files or the runs database.
- The token must never appear in logs, reports, or error output. Add a redactor to logging.

## Post matching strategies

When locating a backup post on the live site, try in order until one succeeds:

1. **Permalink** — construct expected URL from frontmatter date + slug, fetch directly. Fast and authoritative when nothing has changed.
2. **Slug + date range** — same slug within ±2 days of expected date (handles timezone shifts).
3. **Title** — exact title match within a reasonable date window.
4. **Content hash** — first ~500 characters hashed, compared against recent posts. Catches relocated posts.
5. **Fuzzy** — last resort for posts where everything has drifted. Flag explicitly in report.

Record which strategy succeeded for each match. "Had to fall back to fuzzy match" is itself a finding.

## Severity classification

| Severity | Class | Example |
|---|---|---|
| Critical | `missing` | Post in BAR, not findable on live site by any strategy |
| High | `relocated` | Found, but URL differs from BAR's expected URL |
| High | `media_broken` | Post present, but referenced image/asset 404s |
| Medium | `modified` | Post present, but rendered content differs materially from BAR |
| Low | `metadata_drift` | Categories or tags differ |
| Info | `extra` | Live post has no corresponding entry in BAR (likely newer) |

## BAR file format

A BAR is a ZIP archive renamed to `.bar`. Top-level entries: `index.html` (ignored), `feed.json` (JSON Feed v1 with all posts), and `uploads/YYYY/<hash>.<ext>` for media. There is no Markdown-with-frontmatter; posts are JSON Feed items with seven fields: `id`, `url`, `title` (optional, present iff long-form), `date_published`, `content_html`, `content_text`, `tags`. Media is referenced from post bodies as absolute URLs, almost all pointing back to the live site's `/uploads/` paths. URLs follow `https://<host>/YYYY/MM/DD/<slug>.html` with no observed exceptions across ~10K posts.

See [`docs/bar-format.md`](docs/bar-format.md) for the full reference and counts across the available backups.

## Tech stack

| Need | Choice | Notes |
|---|---|---|
| Python version | 3.11+ | For `tomllib` and modern type hints |
| Package management | `uv` | Fast, modern, what Jamie uses |
| CLI framework | `typer` | Type-hinted, ergonomic |
| HTTP | `httpx` | Sync for resolution, async for media HEAD checks |
| HTML parsing | `selectolax` | Used for content normalization in audit/content.py |
| Output / TUI | `rich` | Tables, diffs, progress bars |
| Storage | stdlib `sqlite3` | No ORM needed for this scope |
| Config | stdlib `tomllib` | Read-only TOML config |
| Fuzzy matching | `rapidfuzz` | For the fuzzy resolver strategy |
| Testing | `pytest` | With fixtures per BAR sample |

## Project layout

```
mb-audit/
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── docs/
│   ├── bar-format.md          # Findings from BAR inspection
│   └── design.md              # Longer-form design notes
├── backups/                   # Sample BARs for testing — gitignored
├── src/mb_audit/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── bar/
│   │   ├── __init__.py
│   │   ├── parser.py
│   │   ├── models.py
│   │   └── differ.py
│   ├── live/
│   │   ├── __init__.py
│   │   ├── micropub.py
│   │   ├── fetcher.py
│   │   └── resolver.py
│   ├── audit/
│   │   ├── __init__.py
│   │   ├── reconciler.py
│   │   ├── content.py
│   │   ├── media.py
│   │   └── severity.py
│   ├── repair/
│   │   ├── __init__.py
│   │   ├── interactive.py
│   │   └── micropub_writer.py
│   ├── report/
│   │   ├── __init__.py
│   │   ├── markdown.py
│   │   ├── json_export.py
│   │   └── manton.py
│   └── store/
│       ├── __init__.py
│       ├── schema.sql
│       └── runs.py
└── tests/
    ├── fixtures/
    ├── test_bar_parser.py
    ├── test_resolver.py
    └── test_reconciler.py
```

## Configuration

Default config at `~/.mb-audit/config.toml`. CLI flags override config values.

```toml
[site]
url = "https://example.com"
micropub_endpoint = "https://micro.blog/micropub"

[auth]
token_env = "MICROBLOG_TOKEN"

[audit]
settling_window_hours = 24
content_diff_enabled = true
media_check_enabled = true
external_link_check = false

[matching]
strategies = ["permalink", "slug_date", "title", "content_hash"]
fuzzy_threshold = 0.85

[reporting]
output_dir = "~/Documents/mb-audit-reports"
formats = ["markdown", "json"]
```

## Operational data

All operational data lives under `~/.mb-audit/`:

- `config.toml` — user configuration
- `bars/` — parsed BAR cache, keyed by content hash
- `cache/` — HTTP response cache (live site fetches)
- `runs.sqlite` — run history, append-only
- `reports/` — generated reports per run

Never write user data outside this directory.

## Reporting

Reports are generated in two formats by default:

- **Markdown** — human-readable, scannable in 30 seconds, structured with summary first and details by severity.
- **JSON** — machine-readable manifest of every classification, used by `repair` and external tools.

The Markdown report includes a **"Manton Email Summary"** section: a tight, factual paragraph (URLs, content hashes, BAR provenance, dates) suitable for pasting into a support email.

## Repair safety

Repair mode is the only path that mutates state. It must:

- Run interactively — one post at a time, with explicit confirmation per post.
- Default to dry-run. Require `--execute` to actually call Micropub.
- Display a content preview and proposed Micropub payload before any write.
- Preserve the original `published` date and `mp-slug` so URL and timestamp match the original.
- Log every repair action to the runs database with a timestamp and the exact payload sent.
- Never auto-retry. A failed repair is reported and surfaced to the user.

## Coding conventions

- **Type hints everywhere.** Run `mypy --strict` clean.
- **Dataclasses for models.** No ORM, no Pydantic unless we hit a wall.
- **Pure functions for validators.** Make `audit/` modules side-effect-free where possible — easier to test.
- **No print statements in library code.** Use `rich` or logging.
- **Tests for every parser and resolver strategy.** Use real BAR fixtures.
- **Errors should be loud and specific.** Wrap network errors with the URL that failed.

## Tasks for Claude Code

The intended workflow when working in this repo:

1. **Inspect the BAR files in `bars/`.** Document structure in `docs/bar-format.md`. This is the prerequisite for everything else.
2. **Refine this spec.** Once BAR structure is understood, update CLAUDE.md and `docs/design.md` with concrete details replacing the working assumptions.
3. **Build the BAR parser first.** Test against every BAR file available.
4. **Build the live-site collectors.** Start with Micropub `q=source` and `feed.json`. Add direct permalink fetching.
5. **Build the resolver.** Implement strategies in order; ensure each is independently testable.
6. **Build the reconciler.** Pure functions over collected data.
7. **Build the report generators.** Markdown first, then JSON.
8. **Build the SQLite run store.** Schema in `src/mb_audit/store/schema.sql`.
9. **Wire up the CLI.** `inspect`, `verify`, `diff`, `report`, `history` first. `repair` last and behind a feature flag.

When in doubt, prefer a working v1 over a comprehensive v2. Repair mode is explicitly out of v1 scope unless verify and reporting are already solid.

## Out of scope (for now)

- Hosting or service mode. This is a CLI tool.
- Auditing non-Micro.blog sites. Future, maybe.
- Auto-repair. Always interactive.
- Sending the Manton email directly. The tool generates the text; the user sends it.
- Webmention or comment auditing. Posts and media only in v1.
- Backup creation. The user already has BAR files; this tool only consumes them.

## References

- Micro.blog API docs: https://microblog.dev/llms.txt
- Micropub spec: https://www.w3.org/TR/micropub/
- JSON Feed spec: https://www.jsonfeed.org/version/1.1/
- Micro.blog account / app tokens: https://micro.blog/account/apps