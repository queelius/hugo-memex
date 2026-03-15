# Hugo Memex

MCP server that indexes Hugo site content into SQLite with FTS5 full-text search.

## Architecture

- **DB is a read-only cache** — Hugo content files are ground truth
- **Generic schema** — JSON `front_matter` column, no per-content-type tables
- **Taxonomies auto-discovered** from hugo.toml
- Raw `sqlite3` — no ORM. WAL mode, foreign keys, FTS5.

## Project Layout

```
hugo_memex/
    parser.py    # Front matter parsing (YAML/TOML/JSON)
    db.py        # Database (schema, migrations, queries)
    config.py    # YAML config + env var overrides
    indexer.py   # Index pipeline (discovery, parsing, sync)
    mcp.py       # FastMCP server (3 tools + 3 resources)
    cli.py       # argparse CLI
tests/
    fixtures/    # Hugo content fixtures for testing
```

## Commands

```bash
# Run tests
pytest tests/ -v --cov=hugo_memex

# Index content
hugo-memex index

# Start MCP server
hugo-memex mcp

# Run SQL query
hugo-memex sql "SELECT title, section FROM pages LIMIT 10"
```

## Conventions

- No ORM — raw sqlite3 with parameterized queries
- All front matter stored losslessly as JSON in `front_matter` column
- Taxonomies derived from hugo.toml config, not hardcoded
- Incremental sync via content_hash (SHA-256) + file mtime
- FTS5 with porter stemming + unicode61 tokenizer
