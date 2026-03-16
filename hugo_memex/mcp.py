"""Hugo Memex MCP server — the primary interface."""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from hugo_memex.config import load_config, load_hugo_config
from hugo_memex.db import Database
from hugo_memex.indexer import index_content


@asynccontextmanager
async def lifespan(server):
    """Initialize database and config on server startup."""
    config = load_config()
    if not config.get("hugo_root"):
        raise RuntimeError(
            "hugo_root not configured. Set HUGO_MEMEX_HUGO_ROOT or "
            "configure hugo_root in config.yaml"
        )
    db = Database(config["database_path"])
    # Store on server object so resources (which can't receive ctx) can access them
    server._live_db = db
    server._live_config = config
    try:
        yield {"db": db, "config": config}
    finally:
        db.close()


def create_server(db=None, config=None):
    """Create the MCP server.

    Pass db and config for testing (skips lifespan).
    """
    mcp = FastMCP("hugo-memex", lifespan=lifespan if db is None else None)
    if db is not None:
        if not db.readonly:
            from hugo_memex.db import _readonly_authorizer
            db.conn.set_authorizer(_readonly_authorizer)
            db.readonly = True
        mcp._test_db = db
        mcp._test_config = config or {}
    _register_tools(mcp)
    _register_resources(mcp)
    return mcp


def _get_db(mcp, ctx=None):
    """Get database from lifespan context, server object, or test injection."""
    if ctx is not None:
        try:
            return ctx.request_context.lifespan_context["db"]
        except (AttributeError, TypeError, KeyError):
            pass
    return getattr(mcp, "_live_db", None) or getattr(mcp, "_test_db", None)


def _get_config(mcp, ctx=None):
    """Get config from lifespan context, server object, or test injection."""
    if ctx is not None:
        try:
            return ctx.request_context.lifespan_context["config"]
        except (AttributeError, TypeError, KeyError):
            pass
    return getattr(mcp, "_live_config", None) or getattr(mcp, "_test_config", {})


def _register_tools(mcp: FastMCP):
    """Register all MCP tools."""

    @mcp.tool(annotations={"readOnlyHint": True})
    def execute_sql(
        sql: Annotated[str, Field(description="SQL query to execute")],
        params: Annotated[
            list | None,
            Field(description="Query parameters for ? placeholders"),
        ] = None,
        ctx: Context | None = None,
    ) -> list[dict]:
        """Run a read-only SQL query against the Hugo content index.
Read hugo://schema for full DDL and query patterns.

Common queries:

List recent posts:
  SELECT path, title, date, section FROM pages
  WHERE kind = 'page' AND draft = 0
  ORDER BY date DESC LIMIT 20

Full-text search:
  SELECT p.path, p.title, p.section, p.date
  FROM pages_fts f JOIN pages p ON p.path = f.path
  WHERE pages_fts MATCH 'search terms'
  ORDER BY rank LIMIT 20

Pages by tag:
  SELECT p.path, p.title, p.date FROM pages p
  JOIN taxonomies t ON p.path = t.page_path
  WHERE t.taxonomy = 'tags' AND t.term = 'python'

Tag cloud:
  SELECT term, COUNT(*) as count FROM taxonomies
  WHERE taxonomy = 'tags' GROUP BY term ORDER BY count DESC

Pages by section:
  SELECT path, title, date FROM pages
  WHERE section = 'post' AND draft = 0 ORDER BY date DESC

Draft pages:
  SELECT path, title, section FROM pages WHERE draft = 1

JSON front matter query:
  SELECT path, title, json_extract(front_matter, '$.project.status') as status
  FROM pages WHERE json_extract(front_matter, '$.project.status') IS NOT NULL

Section stats:
  SELECT section, COUNT(*) as pages, SUM(word_count) as words
  FROM pages GROUP BY section ORDER BY pages DESC

Cross-reference (pages sharing a tag):
  SELECT DISTINCT p2.path, p2.title FROM taxonomies t1
  JOIN taxonomies t2 ON t1.taxonomy = t2.taxonomy AND t1.term = t2.term
  JOIN pages p2 ON t2.page_path = p2.path
  WHERE t1.page_path = 'post/my-post/index.md' AND t2.page_path != t1.page_path
"""
        database = _get_db(mcp, ctx)
        try:
            return database.execute_sql(sql, tuple(params) if params else ())
        except sqlite3.OperationalError as e:
            if "attempt to write" in str(e):
                raise ToolError(
                    "SQL writes are disabled. The Hugo content index is read-only."
                )
            raise ToolError(str(e))
        except Exception as e:
            raise ToolError(str(e))

    @mcp.tool(annotations={"readOnlyHint": True})
    def get_content(
        path: Annotated[
            str,
            Field(description="Content path relative to content/ (e.g. 'post/my-post/index.md')"),
        ],
        ctx: Context | None = None,
    ) -> str:
        """Return raw markdown content from the Hugo site filesystem.

Use this to read the actual file content for editing workflows.
The path should be relative to the content/ directory.
"""
        config = _get_config(mcp, ctx)
        hugo_root = config.get("hugo_root")
        if not hugo_root:
            raise ToolError("hugo_root not configured")

        content_root = Path(hugo_root) / "content"
        target = (content_root / path).resolve()

        # Path traversal protection (is_relative_to is immune to prefix collisions)
        if not target.is_relative_to(content_root.resolve()):
            raise ToolError("Path must be within content/ directory")
        if not target.exists():
            raise ToolError(f"File not found: {path}")
        if not target.is_file():
            raise ToolError(f"Not a file: {path}")

        return target.read_text(encoding="utf-8-sig")

    @mcp.tool(annotations={"readOnlyHint": True})
    def get_pages(
        section: Annotated[
            str | None,
            Field(description="Filter by section (e.g. 'post', 'projects')"),
        ] = None,
        tag: Annotated[
            str | None,
            Field(description="Filter by tag"),
        ] = None,
        search: Annotated[
            str | None,
            Field(description="FTS5 full-text search query"),
        ] = None,
        paths: Annotated[
            list[str] | None,
            Field(description="Specific page paths to retrieve"),
        ] = None,
        include_body: Annotated[
            bool,
            Field(description="Include full markdown body (default true)"),
        ] = True,
        include_drafts: Annotated[
            bool,
            Field(description="Include draft pages (default false)"),
        ] = False,
        limit: Annotated[
            int,
            Field(description="Max pages to return (default 20)"),
        ] = 20,
        ctx: Context | None = None,
    ) -> list[dict]:
        """Retrieve multiple pages with full content in a single call.

Returns page metadata, front matter, taxonomy terms, and optionally
the full markdown body. Use this instead of execute_sql + get_content
when you need to read page content.

Filters are combined with AND. At least one filter or paths must be provided.

Examples:
  get_pages(section="post", limit=10)           → 10 most recent posts with body
  get_pages(tag="python", include_body=False)    → python-tagged pages, metadata only
  get_pages(search="Bayesian inference")         → FTS5 search with full content
  get_pages(paths=["post/my-post/index.md"])     → specific pages by path
"""
        database = _get_db(mcp, ctx)

        conds = []
        params = []

        if not any([section, tag, search, paths]):
            raise ToolError("Provide at least one filter: section, tag, search, or paths")

        if paths:
            placeholders = ",".join("?" for _ in paths)
            conds.append(f"p.path IN ({placeholders})")
            params.extend(paths)

        if section:
            conds.append("p.section = ?")
            params.append(section)

        if not include_drafts:
            conds.append("p.draft = 0")

        # Build the base query — join with FTS if searching
        if search:
            base = (
                "SELECT p.path, p.title, p.section, p.date, p.slug, "
                "p.kind, p.bundle_type, p.draft, p.description, "
                "p.word_count, p.front_matter"
                + (", p.body" if include_body else "")
                + " FROM pages_fts f"
                " JOIN pages p ON p.path = f.path"
            )
            conds.append("pages_fts MATCH ?")
            params.append(search)
        else:
            base = (
                "SELECT p.path, p.title, p.section, p.date, p.slug, "
                "p.kind, p.bundle_type, p.draft, p.description, "
                "p.word_count, p.front_matter"
                + (", p.body" if include_body else "")
                + " FROM pages p"
            )

        if tag:
            conds.append(
                "EXISTS(SELECT 1 FROM taxonomies t "
                "WHERE t.page_path = p.path AND t.taxonomy = 'tags' AND t.term = ?)"
            )
            params.append(tag)

        where = " AND ".join(conds) if conds else "1=1"
        order = " ORDER BY rank" if search else " ORDER BY p.date DESC NULLS LAST"
        params.append(limit)

        try:
            rows = database.execute_sql(
                f"{base} WHERE {where}{order} LIMIT ?",
                tuple(params),
            )
        except Exception as e:
            raise ToolError(str(e))

        # Enrich each row with its taxonomy terms
        result = []
        for row in rows:
            page = dict(row)
            # Parse front_matter from JSON string to dict
            if isinstance(page.get("front_matter"), str):
                page["front_matter"] = json.loads(page["front_matter"])
            # Fetch taxonomy terms for this page
            tax_rows = database.execute_sql(
                "SELECT taxonomy, term FROM taxonomies WHERE page_path = ?",
                (page["path"],),
            )
            taxonomies = {}
            for tr in tax_rows:
                taxonomies.setdefault(tr["taxonomy"], []).append(tr["term"])
            page["taxonomies"] = taxonomies
            result.append(page)

        return result

    @mcp.tool()
    def rebuild_index(
        paths: Annotated[
            list[str] | None,
            Field(description="Specific content paths to re-index (relative to content/)"),
        ] = None,
        force: Annotated[
            bool,
            Field(description="Force full re-index, ignoring sync state"),
        ] = False,
        ctx: Context | None = None,
    ) -> dict:
        """Re-index Hugo content into the SQLite database.

By default, only indexes changed files (incremental).
Pass force=True for a full rebuild.
Pass specific paths to re-index only those files.
"""
        config = _get_config(mcp, ctx)
        hugo_root = config.get("hugo_root")
        if not hugo_root:
            raise ToolError("hugo_root not configured")

        # Validate paths stay within content/
        if paths:
            content_root = Path(hugo_root) / "content"
            for p in paths:
                resolved = (content_root / p).resolve()
                if not resolved.is_relative_to(content_root.resolve()):
                    raise ToolError(f"Path must be within content/: {p}")

        database = _get_db(mcp, ctx)
        # Use a separate read-write connection for indexing to avoid
        # disabling the authorizer on the shared read-only connection.
        # For :memory: DBs (tests), we must reuse the same connection.
        if database.db_path == ":memory:":
            database.conn.set_authorizer(None)
            try:
                return index_content(hugo_root, database, paths=paths, force=force)
            finally:
                from hugo_memex.db import _readonly_authorizer
                database.conn.set_authorizer(_readonly_authorizer)
        else:
            write_db = Database(database.db_path)
            try:
                return index_content(hugo_root, write_db, paths=paths, force=force)
            finally:
                write_db.close()


def _register_resources(mcp: FastMCP):
    """Register all MCP resources.

    FastMCP v2 treats parameterless resources as static. We use closures
    over `mcp` to access the DB/config at read time, and register with
    no function parameters (ctx not needed since we use the closure).
    """
    from fastmcp.resources import FunctionResource

    def _schema_fn() -> str:
        db = _get_db(mcp)
        if db is None:
            return "Database not available"
        return db.get_schema()

    def _site_fn() -> str:
        config = _get_config(mcp)
        hugo_root = config.get("hugo_root")
        if not hugo_root:
            return json.dumps({"error": "hugo_root not configured"})
        try:
            hugo_config = load_hugo_config(hugo_root)
            return json.dumps(hugo_config, indent=2, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _stats_fn() -> str:
        db = _get_db(mcp)
        if db is None:
            return json.dumps({"error": "Database not available"})
        return json.dumps(db.get_statistics(), indent=2)

    mcp.add_resource(FunctionResource(
        uri="hugo://schema",
        name="schema",
        description="Database schema: DDL, indexes, relationships, FTS5 docs, JSON query patterns. Read this before writing SQL queries.",
        fn=_schema_fn,
    ))
    mcp.add_resource(FunctionResource(
        uri="hugo://site",
        name="site",
        description="Hugo site configuration (hugo.toml) as JSON. Includes taxonomies, menus, params, baseURL, etc.",
        fn=_site_fn,
    ))
    mcp.add_resource(FunctionResource(
        uri="hugo://stats",
        name="stats",
        description="Aggregate statistics about indexed Hugo content. Pages per section, taxonomy counts, draft/published, date ranges.",
        fn=_stats_fn,
    ))


def main():
    """Entry point for `hugo-memex mcp`."""
    create_server().run()
