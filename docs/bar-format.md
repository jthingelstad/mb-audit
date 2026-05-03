# Micro.blog BAR file format

Findings from inspecting six BAR files spanning 2021-10 through 2026-01.

## Container

A BAR file is a **ZIP archive** with the `.bar` extension.

### ZIP64 defect (worth reporting upstream)

For BARs whose total size exceeds 4 GB, Micro.blog's exporter writes the End-of-Central-Directory record with `cd_offset = 0xFFFFFFFF` — the ZIP64 sentinel value — but does **not** emit the corresponding ZIP64 EOCD record (`PK\x06\x06`) or ZIP64 EOCD locator (`PK\x06\x07`) that the spec requires alongside the sentinel. The central directory entries themselves are well-formed (no per-entry ZIP64 sentinels for offsets/sizes inside the available BARs).

System `unzip` is permissive and recovers by computing `cd_offset = file_size - 22 - cd_size`, which lands on a valid `PK\x01\x02` central directory entry. Python's stdlib `zipfile` is strict, treats the sentinel literally, and fails with `BadZipFile: Bad magic number for file header` on every member open. This is why `mb-audit` ships its own reader in `src/mb_audit/bar/zip_reader.py` rather than using `zipfile`.

Worth flagging to Manton: emitting the ZIP64 EOCD record (and locator) for archives over 4 GB would make BARs read cleanly with any spec-conforming ZIP library, not just permissive ones.

## Top-level layout

Every observed BAR contains exactly three top-level entries:

| Entry | Notes |
|---|---|
| `index.html` | ~1.7 KB shell with a minimal `h-feed` skeleton. Not used by `mb-audit`. |
| `feed.json` | The full content payload. JSON Feed v1. |
| `uploads/YYYY/<hash>.<ext>` | Media files, partitioned by year. |

No drafts directory, no pages directory, no per-post Markdown files, no configuration JSON, no categories index. All post content lives in `feed.json`.

## `feed.json`

Conforms to [JSON Feed v1.1](https://www.jsonfeed.org/version/1.1/). The interesting fields:

```json
{
  "version":       "https://jsonfeed.org/version/1.1",
  "title":         "...",
  "home_page_url": "https://www.thingelstad.com/",
  "feed_url":      "https://www.thingelstad.com/feed.json",
  "icon":          "...",
  "items":         [ ... ]
}
```

### Item schema

Across all six BARs, only these seven fields appear on any item:

| Field | Type | Notes |
|---|---|---|
| `id` | string | Looks like `http://<host>.micro.blog/YYYY/MM/DD/<slug>.html` — the Micro.blog syndication identifier. Always present. |
| `url` | string | Canonical URL on the public site, `https://<host>/YYYY/MM/DD/<slug>.html`. Always present. |
| `title` | string | **Optional.** Present iff this is a long-form post; absent for microposts. This is the only signal of post type. |
| `date_published` | ISO 8601 string | With timezone offset. |
| `content_html` | string | Rendered HTML of the post body. |
| `content_text` | string | Markdown source of the post body. |
| `tags` | string[] | Optional. Empty array or absent for untagged posts. |

Notably **absent** in every BAR examined:

- `_microblog` extension fields (no MB-specific metadata in the export)
- `attachments` (media is referenced inline in `content_html` instead)
- `external_url`
- `author`
- `summary`
- `image` / `banner_image`

If a future BAR adds any of these, the parser should preserve them on the `Post` model rather than silently drop them.

## URL structure

Canonical post URLs follow `https://<host>/YYYY/MM/DD/<slug>.html` with **zero observed exceptions** across ~10K posts. This makes permalink-based resolution cheap and authoritative.

### Host drift in MB's records

When a Micro.blog user has changed their site's hostname over time (e.g. moved from a `<sub>.thingelstad.com` original setup to `www.thingelstad.com`), Micro.blog's own database can keep returning the **historical** hostname for older posts via Micropub `q=source` — even though the BAR (regenerated more recently) uses the **current** hostname for the same posts. The post path is identical; only the host differs.

`mb-audit` treats this as low-severity `metadata_drift` ("host drift") rather than a relocation. Without this normalization, an audit would surface thousands of spurious "relocated" findings.

This is worth flagging upstream: MB could rewrite historical post URLs to the current canonical hostname when the site setting changes.

## Media references

Media inside `content_html`:

- ~98% of `<img src="...">` and `<a href="...">` media URLs are **absolute** to the live site host (e.g., `https://www.thingelstad.com/uploads/2023/02872be889.jpg`).
- ~2% point at external CDNs (`cdn.uploads.micro.blog`, `files.thingelstad.com`, etc.).
- Effectively zero use relative paths like `uploads/...`.

The parser should treat media as absolute URLs and verify them with `HEAD` requests against whatever host they name.

## Storage in `uploads/`

- Layout: `uploads/<year>/<8-char-hex>.<ext>`
- Extensions observed: `.jpg`, `.png`, `.gif`. Larger BARs have a wider set; the parser doesn't enumerate by extension.
- Filenames are content-hash-style; there is no slug correspondence between a media file and the post that references it. The mapping is by URL inside `content_html`, not by directory layout.

## Empty / partial BARs

The 2021-10 BAR (`jthingelstad_86bac7-202110.bar`) contains ~7,000 media files but its `feed.json` has `items: []`. This is a real failure mode of Micro.blog's export and the auditor must surface it loudly rather than treat the BAR as authoritative-and-empty (which would falsely classify every live post as "extra").

## Counts across the six observed BARs

| BAR | Posts | Media |
|---|---:|---:|
| 2021-10 | 0 (malformed) | 7,047 |
| 2022-01 | 7,453 | 7,368 |
| 2023-01 | 7,939 | 8,063 |
| 2025-08 | 9,881 | 13,666 |
| 2025-11 | 9,994 | 14,143 |
| 2026-01 | 10,087 | 14,639 |

`feed.json` is small even at 10K posts (~50–100 MB), so the parser loads it whole rather than streaming. The bulk of BAR file size is the media payload.
