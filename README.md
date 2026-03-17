# hugo-memex

MCP server that makes any Hugo site's content queryable via SQLite with FTS5 full-text search.

Your Hugo blog is ground truth. hugo-memex indexes it into a SQLite cache with full-text search, taxonomy queries, and JSON front matter extraction : then exposes it via [MCP](https://modelcontextprotocol.io) so AI assistants can query, search, and create content.

## Install

```bash
pip install hugo-memex
```

## Quick Start

```bash
# Configure
mkdir -p ~/.config/hugo-memex
cat > ~/.config/hugo-memex/config.yaml << 'EOF'
hugo_root: ~/path/to/your/hugo-site
database_path: ~/.config/hugo-memex/hugo.db
EOF

# Index your site
hugo-memex index

# Query it
hugo-memex search "machine learning"
hugo-memex sql "SELECT title, section, date FROM pages ORDER BY date DESC LIMIT 10"
hugo-memex stats
```

## MCP Integration

Add to your `.mcp.json` for Claude Code:

```json
{
  "mcpServers": {
    "hugo-memex": {
      "command": "python",
      "args": ["-m", "hugo_memex", "mcp"],
      "env": {
        "HUGO_MEMEX_CONFIG": "/path/to/config.yaml"
      }
    }
  }
}
```

## Tools

| Tool | Purpose |
|------|---------|
| `execute_sql` | Read-only SQL with ~10 exemplar queries in the docstring |
| `get_pages` | Bulk content retrieval : filter by section, tag, FTS search, paths |
| `get_content` | Raw markdown from filesystem for a single file |
| `create_page` | Create new content with proper leaf bundle structure |
| `update_page` | Merge front matter / replace body on existing pages |
| `suggest_tags` | FTS5-based tag suggestions with canonical casing |
| `get_front_matter_template` | Derive section conventions from actual data |
| `validate_page` | Check completeness, tag consistency, cross-references |
| `rebuild_index` | Incremental re-sync after content changes |

## Resources

| Resource | Purpose |
|----------|---------|
| `hugo://schema` | Full DDL + relationship docs + query patterns |
| `hugo://site` | Hugo site config (hugo.toml) as JSON |
| `hugo://stats` | Aggregate stats for quick orientation |

## Architecture

- **DB is a read-only cache** : Hugo content files are ground truth
- **Generic schema** : JSON `front_matter` column, no per-content-type tables
- **Taxonomies auto-discovered** from `hugo.toml`
- **Incremental sync** via SHA-256 content hash + file mtime
- **FTS5** with porter stemming + unicode61 tokenizer
- **SQLite authorizer** enforces read-only (not bypassable via PRAGMA)
- Raw `sqlite3` : no ORM. WAL mode, foreign keys.

## Configuration

```yaml
# ~/.config/hugo-memex/config.yaml
hugo_root: ~/github/repos/my-hugo-site   # contains hugo.toml + content/
database_path: ~/.config/hugo-memex/hugo.db
```

Environment variable overrides: `HUGO_MEMEX_CONFIG`, `HUGO_MEMEX_HUGO_ROOT`, `HUGO_MEMEX_DATABASE_PATH`.

## License

MIT
