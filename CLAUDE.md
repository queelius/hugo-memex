# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

MCP server that indexes Hugo site content into SQLite with FTS5 full-text search. Part of the `*-memex` ecosystem (see `~/github/memex/CLAUDE.md` for the shared contract). Hugo content files are ground truth; the database is a read-only cache rebuilt from them.

## Commands

```bash
# Run all tests with coverage
pytest tests/ -v --cov=hugo_memex

# Run a single test class or method
pytest tests/test_writer.py::TestCreatePage -v
pytest tests/test_mcp.py::TestExecuteSQL::test_fts_search -v

# Index content (requires HUGO_MEMEX_HUGO_ROOT or config.yaml)
hugo-memex index
hugo-memex index --force              # full re-index, ignoring sync state
hugo-memex index --path post/my-post/index.md  # single file

# Other CLI commands
hugo-memex search "query terms"
hugo-memex sql "SELECT path, title FROM pages LIMIT 10"
hugo-memex mcp                        # start MCP server (stdio)
hugo-memex stats                      # JSON aggregate stats

# Install for development
pip install -e ".[dev]"
```

## Architecture

**Core invariant: filesystem writes, database reads.** Content creation/updates write to Hugo's `content/` directory. The SQLite index is a derived cache, rebuilt via `rebuild_index` after writes. MCP `execute_sql` enforces read-only access through a SQLite authorizer (not PRAGMA query_only, which can be disabled via SQL).

### Data flow

```
Hugo content/ files
    | indexer.py (parse > classify > extract > save)
SQLite DB (pages + pages_fts + taxonomies + sync_state)
    | mcp.py (8 tools + 3 resources, read-only authorizer)
LLM via MCP
    | writer tools (create_page, update_page)
Hugo content/ files  <-- loop back to top via rebuild_index
```

### Module responsibilities

- **parser.py**: Detects front matter format (YAML `---`, TOML `+++`, JSON `{`) and splits into `(dict, body)`. Stateless, no DB access.
- **config.py**: Loads `~/.config/hugo-memex/config.yaml` with env var overrides (`HUGO_MEMEX_HUGO_ROOT`, `HUGO_MEMEX_DATABASE_PATH`). Also parses `hugo.toml` for taxonomy discovery.
- **db.py**: Raw `sqlite3`, no ORM. WAL mode, foreign keys, FTS5 with porter stemming. Schema is in `SCHEMA_SQL` constant; migrations via `_MIGRATIONS` dict. The `readonly` flag installs a SQLite authorizer callback that denies all writes at the C level.
- **indexer.py**: Walks `content/`, hashes files (SHA-256), skips unchanged via `sync_state` table, writes page+taxonomies+sync atomically. Removes orphaned pages on full reindex.
- **writer.py**: Creates/updates Hugo content files on disk. Path traversal protection via `is_relative_to()` + slug/section validation regex. `update_page` only supports YAML front matter (TOML/JSON would require `tomli_w` and risk mangling).
- **mcp.py**: FastMCP v2 server. `create_server(db, config)` accepts test injection (skips lifespan). `rebuild_index` temporarily lifts the authorizer or opens a separate write connection. Resources are closures over `mcp` (no ctx parameter).

### Schema (4 tables + 2 virtual)

- **pages**: One row per `.md` file. `front_matter` column stores lossless JSON. PK is `path` (relative to `content/`).
- **taxonomies**: Normalized join table `(page_path, taxonomy, term)`. Taxonomy names are auto-discovered from `hugo.toml`, not hardcoded.
- **sync_state**: Tracks `content_hash` + `file_mtime` per file for incremental indexing.
- **pages_fts**: FTS5 virtual table over `(title, description, body)`. Joined to `pages` via `path`.
- **marginalia**: Free-form notes attached to pages. No FK to `pages` (orphan survival). Source YAML files live in `data/marginalia/`.
- **marginalia_fts**: FTS5 virtual table over marginalia body text.

### Security model

- MCP `execute_sql` uses a SQLite authorizer callback (`_readonly_authorizer`) that allowlists `SQLITE_SELECT`, `SQLITE_READ`, `SQLITE_FUNCTION` and denies everything else. This cannot be bypassed via SQL (unlike PRAGMA query_only). `PRAGMA query_only` and `PRAGMA writable_schema` are explicitly denied.
- All filesystem write paths (create_page, update_page, get_content) use `Path.resolve()` + `is_relative_to()` to prevent path traversal. Symlinks are resolved and checked. Slug/section inputs are validated against `[A-Za-z0-9._-]+` regex.

## Conventions

- No ORM: raw sqlite3 with parameterized queries
- All front matter stored losslessly as JSON in `front_matter` column
- Taxonomies derived from `hugo.toml` config, not hardcoded
- Incremental sync via content_hash (SHA-256) + file mtime
- FTS5 with porter stemming + unicode61 tokenizer
- Two-phase writes: write file to disk, then `rebuild_index(paths=[...])` to update the index
- Marginalia stored in `data/marginalia/` as YAML files, mirroring content path structure

## Testing patterns

Tests use `pytest-asyncio` (`asyncio_mode = "auto"` in pyproject.toml). MCP tool tests get the underlying function via `await server.get_tool(name)` then call it synchronously.

Key fixtures (in `conftest.py`):
- `db`: in-memory Database for unit tests
- `hugo_root` / `fixtures_dir`: points to `tests/fixtures/` (a minimal Hugo site with `hugo.toml` + content files)
- `writable_site` (in `test_writer.py`): `shutil.copytree` of fixtures into `tmp_path` + pre-indexed DB, for tests that modify content
- `writable_mcp_server` (in `test_mcp.py`): same pattern, wrapped in an MCP server

The fixture Hugo site at `tests/fixtures/` has its own `hugo.toml` with taxonomies (tags, categories, series) and content in `post/`, `projects/`, `media/` sections.
