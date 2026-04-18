# Soft Delete for hugo-memex

## Problem

The `*-memex` workspace contract requires every record table to carry an `archived_at TIMESTAMP NULL` column. Deletes are soft by default; hard delete is opt-in. This preserves cross-archive URIs so trails and marginalia that reference a record remain meaningful after the record is removed.

hugo-memex currently violates this contract in three places:

1. **Indexer page cleanup**: a missing `.md` file triggers `db.delete_page(path)` (hard delete).
2. **Indexer marginalia cleanup**: a missing YAML file triggers `db.delete_marginalia_by_source(file)` (hard delete).
3. **MCP `delete_marginalia(id)`**: edits the YAML to remove the note, then rebuild drops the row from the DB.

## Decision

**Option C: DB archive + in-place `archived_at` field per marginalia note.**

- Schema v3 adds `archived_at TIMESTAMP NULL` columns to `pages` and `marginalia`.
- Indexer sets `archived_at` when source files disappear; clears it when files return.
- Per-note marginalia soft-delete adds an `archived_at` field inside the YAML note dict; the file stays on disk.
- A new CLI `purge` command performs bulk hard delete of archived records.
- MCP read tools filter archived rows by default; callers opt into including them.

Alternatives rejected:
- **Option A (DB-only archive)**: The indexer would un-archive notes on the next sync because the YAML still has them. Impossible without duplicating the archive state on disk.
- **Option B (separate `_archived/` directory)**: Doubles the data directory surface area and complicates Hugo template access to archived notes. Two sources of truth for a single note's state.

## Schema changes

Version bumps from 2 to 3. Additive migration, no data rewrite.

```sql
ALTER TABLE pages ADD COLUMN archived_at TEXT;
ALTER TABLE marginalia ADD COLUMN archived_at TEXT;

CREATE INDEX IF NOT EXISTS idx_pages_archived ON pages(archived_at);
CREATE INDEX IF NOT EXISTS idx_marginalia_archived ON marginalia(archived_at);
```

Both columns store ISO 8601 UTC strings (same format as existing `created_at`, `indexed_at`). `NULL` means active; any timestamp means archived at that time.

Migration v2→v3 is registered in `_MIGRATIONS[2] = _migrate_v2_to_v3`.

## YAML format change (backward-compatible)

Marginalia YAML files add an optional per-note `archived_at` field:

```yaml
- id: mg-abc
  created: 2026-04-18T09:00:00Z
  body: "Active note"
- id: mg-def
  created: 2026-04-17T10:00:00Z
  archived_at: 2026-04-18T11:00:00Z
  body: "Archived note kept for URI stability"
```

- Presence of `archived_at` means archived.
- Existing files without the field continue to work (treated as active).
- The indexer reads `note.get("archived_at")` and stores it in the DB column.

## Indexer behavior changes

### Content pages

Cleanup logic in `index_content` currently calls `db.delete_page(path)` for missing files. New behavior:

```
for each path previously indexed:
    if path no longer on disk:
        if page row's archived_at is NULL:
            set archived_at = now()
            stats["archived"] += 1
    else if page row's archived_at is NOT NULL and file exists:
        clear archived_at
        stats["restored"] += 1
```

No rows are deleted by the indexer (ever). Hard delete only happens via explicit `purge` command.

### Marginalia

For each YAML file:
- If the file was present but is now gone: mark all rows from that `source_file` as archived.
- If a row is active in DB but the YAML's corresponding note has `archived_at`: mark DB row archived.
- If a row is archived in DB but the YAML's corresponding note has no `archived_at`: un-archive the DB row.

### Stats dict

```python
{
    "indexed": int,          # newly indexed or re-indexed content
    "unchanged": int,        # content unchanged since last sync
    "archived": int,         # NEW: pages newly archived this run
    "restored": int,         # NEW: pages un-archived this run
    "errors": list,

    "marginalia_indexed": int,
    "marginalia_unchanged": int,
    "marginalia_archived": int,      # NEW
    "marginalia_restored": int,      # NEW
}
```

`removed` and `marginalia_removed` keys are dropped. No caller in the codebase depends on them other than tests, which will be updated.

## MCP tool changes

### Read tools filter archived by default

- `get_pages(...)`: gains `include_archived: bool = False` parameter. Default SQL adds `AND archived_at IS NULL`.
- `get_marginalia(page_path, include_archived: bool = False)`: same pattern.
- `execute_sql`: unchanged. User's SQL is their responsibility. Schema resource docs are updated with a "filter archived" idiom.

### Write tools soft-delete by default

- `delete_marginalia(id, purge: bool = False)`:
  - Default (`purge=False`): reads YAML file, finds note by id, adds `archived_at: <now>` field, writes file back. The tool also updates the DB row's `archived_at` synchronously (no need to call `rebuild_index` separately for the archive to take effect).
  - With `purge=True`: removes the note from YAML, hard-deletes the DB row and FTS entry.
  - If the note is already archived and `purge=False`, the tool is a no-op returning `status: "already_archived"`.
- Returns `{id, status: "archived" | "already_archived" | "purged"}`.

### New MCP tools

- `restore_marginalia(id)`: removes the `archived_at` field from the note in its YAML file, clears the DB row's `archived_at` synchronously. Returns `{id, status: "restored"}`. If the note is not currently archived, returns `{id, status: "already_active"}` (no-op, not an error).

Pages intentionally do not get a `delete_page` or `restore_page` MCP tool. Page lifecycle is filesystem-driven; use `rm post/x.md` to archive and replacing the file to restore.

## CLI changes

### New `purge` subcommand

```bash
hugo-memex purge --missing
# Hard-delete all archived rows whose source file is missing on disk.

hugo-memex purge --archived-before 2026-01-01
# Hard-delete archived rows whose archived_at is older than the given date.

hugo-memex purge --dry-run --missing
# Print what would be purged without doing it.
```

Both filters can be combined. Without any filter, `purge` refuses to run (no default bulk delete).

### `index` output

`hugo-memex index` prints the new stats keys:
```
Indexed: 34, Unchanged: 930, Archived: 2, Restored: 0
Marginalia: indexed 3, unchanged 1, archived 0, restored 0
```

## Schema resource docs update

The `get_schema()` response gets a new section:

```
-- ══ Archived Records ══════════════════════════════════════════
-- All record tables use soft delete: archived_at IS NULL means active.
-- Default queries should filter archived rows unless you want history.
--
-- Active pages only:
--   SELECT path, title FROM pages WHERE archived_at IS NULL AND draft = 0
--
-- Recently archived (last 30 days):
--   SELECT path, archived_at FROM pages
--   WHERE archived_at IS NOT NULL
--     AND date(archived_at) > date('now', '-30 days')
--   ORDER BY archived_at DESC
--
-- Archived marginalia for a page (history view):
--   SELECT id, body, created_at, archived_at FROM marginalia
--   WHERE page_path = ? AND archived_at IS NOT NULL
--   ORDER BY archived_at DESC
```

Existing query examples in the same docs are updated to include `AND archived_at IS NULL` where appropriate (e.g., the "list recent posts" and "pages by tag" patterns in `execute_sql` docstring).

## Testing

- **Schema migration**: v2 → v3 creates columns and indexes; existing data unaffected.
- **Indexer archiving**: create content, index, remove file, re-index → row gets `archived_at`; counts show in `archived` stat.
- **Indexer restore**: archive a page, restore the file, re-index → `archived_at` cleared; counts show in `restored`.
- **Indexer idempotency**: running index twice on archived state does not re-set `archived_at` (count stays 0 on second run).
- **Marginalia per-note archive in YAML**: manually add `archived_at` to a note → indexer sets DB column.
- **MCP `delete_marginalia` default**: adds `archived_at` to YAML, DB reflects.
- **MCP `delete_marginalia(purge=True)`**: full hard delete, matches old behavior.
- **MCP `restore_marginalia`**: clears `archived_at` in YAML and DB.
- **MCP `get_marginalia` filter**: default excludes archived; `include_archived=True` returns both.
- **MCP `get_pages` filter**: same pattern.
- **CLI `purge --missing`**: purges only archived rows whose source files are gone.
- **CLI `purge --archived-before`**: date filter works.
- **CLI `purge` with no filter**: exits non-zero, no action.
- **CLI `purge --dry-run`**: reports what would be purged without writing.
- **Integration lifecycle**: add → archive → verify hidden by default → restore → archive again → purge → gone.
- **Fixture addition**: the fixture `data/marginalia/post/test-post.yaml` gains an archived note for indexer test coverage.

## Migration path for existing data

- Databases at schema v2 auto-migrate on first open (SQLite ALTER TABLE is cheap, rows are unaffected).
- Existing marginalia YAML files continue to work; they're all treated as active since no notes have `archived_at`.
- Users who ran the old indexer and had rows hard-deleted: those rows stay gone. No recovery path. Documented.

## Scope boundaries

This spec covers:
- `archived_at` on `pages` and `marginalia`.
- Indexer soft-delete and restore.
- MCP default-soft delete for marginalia, filter-archived on reads.
- CLI `purge` command.

Out of scope:
- Soft delete for other future record tables (add as new tables land).
- Automatic purge policies (user runs purge manually).
- Archive browsing UI in dev mode (could extend the Hugo partial later).
- Cross-archive trail awareness of archived records (belongs to meta-memex).
- `archive_marginalia` as a distinct operation from `delete_marginalia` (the verb "delete" maps to "archive" per the contract; we don't introduce a separate verb).
- Purge via MCP. Hard delete is administrative housekeeping, only exposed through the CLI. This keeps the LLM-facing surface non-destructive by design.
