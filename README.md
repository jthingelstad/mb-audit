# mb-audit

Audit a Micro.blog site against one or more BAR backup files. The BAR is the source of truth; the live site is verified to still contain everything the backup says it should.

See `CLAUDE.md` for the design and `docs/bar-format.md` for the BAR file format reference.

## Quick start

```bash
# Install (uv is one option; pip works too)
uv sync --locked

# Inspect a BAR
uv run --locked mb-audit inspect backups/your-backup.bar

# Verify a live site against a BAR
uv run --locked mb-audit verify --bar backups/your-backup.bar --site https://example.com
```
