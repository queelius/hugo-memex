# Marginalia for hugo-memex

## Problem

The `*-memex` ecosystem contract requires every archive to support marginalia: free-form notes attached to records. hugo-memex currently has no marginalia support.

Hugo sites are filesystem-dominant: content files are ground truth, the SQLite DB is a derived cache. Marginalia is original data (not derivable from content), so it needs a storage approach that respects this invariant.

## Decision

Store marginalia as YAML files under `data/marginalia/`, using Hugo's own convention for auxiliary structured data. The indexer discovers and indexes them into the DB alongside content pages. The DB remains a cache: a full rebuild re-indexes marginalia from the YAML files on disk.

This was chosen over two alternatives:
- **Sidecar files** (`.marginalia.yaml` next to each content file): clutters bundle directories, harder to manage as a collection.
- **DB-only table**: breaks the "DB is a cache" invariant, marginalia would be lost on rebuild.

## File layout

Mirror the content path structure under `data/marginalia/`:

```
data/marginalia/
  post/
    my-post.yaml           # notes on post/my-post/index.md
    standalone-file.yaml   # notes on post/standalone-file.md
  projects/
    cool-project.yaml      # notes on projects/cool-project/index.md
```

### Path mapping rules

| Content path                      | Marginalia file                      |
|-----------------------------------|--------------------------------------|
| `post/my-post/index.md` (bundle)  | `data/marginalia/post/my-post.yaml`  |
| `post/standalone.md` (standalone)  | `data/marginalia/post/standalone.yaml` |
| `_index.md` (root section)        | `data/marginalia/_index.yaml`        |
| `media/book-review.md`            | `data/marginalia/media/book-review.yaml` |

Rule: strip `index.md` from leaf bundles (keep the parent directory name), strip `.md` from standalone files, append `.yaml`.

Reverse mapping (marginalia file to page path): look up `<stem>/index.md` in the `pages` table first (leaf bundle), then `<stem>.md` (standalone). If neither exists in the DB, set `page_path` to `<stem>/index.md` as the best-guess path; the marginalia is still indexed (orphan survival).

## File format

Each file is a YAML list of notes:

```yaml
- id: "mg-a1b2c3d4e5f6"
  created: "2026-04-16T12:00:00Z"
  body: "This needs updating for Python 3.13"
- id: "mg-d4e5f6a1b2c3"
  created: "2026-04-17T09:30:00Z"
  body: "Related to llm-memex://conversation/abc123"
```

### ID generation

`mg-` prefix + first 12 hex chars of SHA-256(`page_path + "\n" + body + "\n" + created`).

This makes IDs:
- Deterministic (same content produces the same ID)
- Durable (survive re-imports)
- Short enough to reference in conversation

### Fields

| Field     | Type   | Required | Description                              |
|-----------|--------|----------|------------------------------------------|
| `id`      | string | yes      | Durable identifier (see generation rule) |
| `created` | string | yes      | ISO 8601 UTC timestamp                   |
| `body`    | string | yes      | Free-form text, may contain URIs         |

No tags, categories, or other metadata on marginalia. The parent page's taxonomies and FTS provide discoverability.

## Schema changes

New tables added to `SCHEMA_SQL` in `db.py`:

```sql
CREATE TABLE IF NOT EXISTS marginalia (
    id TEXT PRIMARY KEY,
    page_path TEXT,              -- NULL if orphaned (page deleted)
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source_file TEXT NOT NULL    -- path relative to hugo root (data/marginalia/...)
);

CREATE INDEX IF NOT EXISTS idx_marginalia_page ON marginalia(page_path);

CREATE VIRTUAL TABLE IF NOT EXISTS marginalia_fts USING fts5(
    id UNINDEXED,
    body,
    tokenize = 'porter unicode61'
);
```

No foreign key from `page_path` to `pages.path`. This ensures:
- Marginalia can be indexed even if its parent page does not exist (orphan survival)
- Deleting a page does not cascade-delete its marginalia
- The marginalia file persists on disk regardless of DB state

Schema version bumps from 1 to 2. Migration: `CREATE TABLE` + `CREATE INDEX` + `CREATE VIRTUAL TABLE` (additive only, no data migration needed).

## Indexer changes

`indexer.py` gains a second discovery pass after content indexing:

1. Walk `data/marginalia/` for `*.yaml` files
2. For each file, parse the YAML list
3. Compute `page_path` via reverse path mapping
4. For each note, insert into `marginalia` + `marginalia_fts`
5. Track sync state (hash + mtime) the same way content files are tracked

The `index_content` function's return stats dict gains a `marginalia_indexed` count.

Incremental sync works identically to content: skip files whose hash is unchanged. On full reindex with cleanup, remove marginalia rows whose `source_file` no longer exists on disk.

## MCP tools

Three new tools in `mcp.py`:

### `add_marginalia(page_path, body) -> dict`

- Computes the marginalia file path from `page_path` using the path mapping
- Reads existing YAML file (if any), appends new entry
- Generates ID, sets `created` to now
- Writes the YAML file
- Returns `{id, page_path, source_file, status: "created"}`
- Caller should call `rebuild_index` afterward (same two-phase pattern as `create_page`)
- Path traversal protection: validates `page_path` resolves within `content/`

### `get_marginalia(page_path) -> list[dict]`

- Read-only, queries the `marginalia` table
- Returns all notes for the given page, ordered by `created_at`
- Returns empty list if no marginalia exists

### `delete_marginalia(id) -> dict`

- Looks up the note in the DB to find `source_file`
- Reads the YAML file, removes the entry with matching ID
- If the file becomes empty, deletes it
- Returns `{id, status: "deleted"}` or raises ToolError if not found
- Caller should call `rebuild_index` afterward

## Writer changes

New functions in `writer.py`:

- `marginalia_path_for_page(hugo_root, page_path) -> Path`: path mapping logic
- `page_path_for_marginalia(marginalia_rel_path) -> str`: reverse mapping
- `add_marginalia(hugo_root, page_path, body) -> dict`: create note on disk
- `delete_marginalia(hugo_root, source_file, note_id) -> dict`: remove note from disk

These follow the same pattern as `create_page` / `update_page`: filesystem writes only, DB update via rebuild_index.

## Hugo template access

Files in `data/marginalia/` are accessible in Hugo templates via `.Site.Data.marginalia`. A template could render marginalia for the current page:

```go-html-template
{{ $slug := .File.ContentBaseName }}
{{ $section := .Section }}
{{ $notes := index .Site.Data.marginalia $section $slug }}
{{ range $notes }}
  <aside class="marginalia">
    <time>{{ .created }}</time>
    <p>{{ .body | markdownify }}</p>
  </aside>
{{ end }}
```

This is a free bonus of the `data/` approach. No template changes are required for the MCP server to work; Hugo rendering is optional.

## Testing

- Unit tests for path mapping (both directions, edge cases: root `_index.md`, nested sections)
- Unit tests for ID generation (deterministic, stable)
- Integration tests: add marginalia via writer, index it, query via DB
- MCP tool tests: `add_marginalia`, `get_marginalia`, `delete_marginalia`
- Orphan survival test: index marginalia, delete parent page, verify marginalia persists
- Fixture additions: add a `data/marginalia/` directory to the test fixture site with sample notes

## URI scheme

Marginalia records are addressable as:

```
hugo-memex://marginalia/<id>
```

This follows the cross-archive URI convention. Individual notes within a marginalia file are first-class records with their own IDs, not fragments of the parent page.

## Scope boundaries

This spec covers marginalia storage, indexing, and MCP access within hugo-memex only. The following are explicitly out of scope:

- Cross-archive marginalia (belongs to meta-memex)
- URI extraction from marginalia body text (belongs to meta-memex graph layer)
- Embedding marginalia for semantic search (belongs to meta-memex embedding layer)
- Hugo template implementation (the data format supports it, but no templates are shipped)
