"""Hugo Memex MCP server — the primary interface."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastmcp import FastMCP
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
            db.conn.execute("PRAGMA query_only=ON")
            db.readonly = True
        mcp._test_db = db
        mcp._test_config = config or {}
    _register_tools(mcp)
    _register_resources(mcp)
    return mcp


def _get_db(mcp, ctx):
    """Get database from lifespan context or test injection."""
    try:
        return ctx.request_context.lifespan_context["db"]
    except (AttributeError, TypeError, KeyError):
        return mcp._test_db


def _get_config(mcp, ctx):
    """Get config from lifespan context or test injection."""
    try:
        return ctx.request_context.lifespan_context["config"]
    except (AttributeError, TypeError, KeyError):
        return mcp._test_config


def _register_tools(mcp: FastMCP):
    """Register all MCP tools."""

    @mcp.tool(annotations={"readOnlyHint": True})
    def execute_sql(
        sql: Annotated[str, Field(description="SQL query to execute")],
        params: Annotated[
            list | None,
            Field(description="Query parameters for ? placeholders"),
        ] = None,
        ctx=None,
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
        ctx=None,
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

        # Path traversal protection
        if not str(target).startswith(str(content_root.resolve())):
            raise ToolError("Path must be within content/ directory")
        if not target.exists():
            raise ToolError(f"File not found: {path}")
        if not target.is_file():
            raise ToolError(f"Not a file: {path}")

        return target.read_text(encoding="utf-8-sig")

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
        ctx=None,
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

        database = _get_db(mcp, ctx)
        # Temporarily disable query_only for indexing
        was_readonly = database.readonly
        if was_readonly:
            database.conn.execute("PRAGMA query_only=OFF")
            database.readonly = False
        try:
            return index_content(hugo_root, database, paths=paths, force=force)
        finally:
            if was_readonly:
                database.conn.execute("PRAGMA query_only=ON")
                database.readonly = True


def _register_resources(mcp: FastMCP):
    """Register all MCP resources.

    FastMCP v2 treats parameterless resources as static. We use closures
    over `mcp` to access the DB/config at read time, and register with
    no function parameters (ctx not needed since we use the closure).
    """
    from fastmcp.resources import FunctionResource

    def _schema_fn() -> str:
        db = getattr(mcp, "_test_db", None)
        if db is None:
            return "Database not available"
        return db.get_schema()

    def _site_fn() -> str:
        config = getattr(mcp, "_test_config", {})
        hugo_root = config.get("hugo_root")
        if not hugo_root:
            return json.dumps({"error": "hugo_root not configured"})
        try:
            hugo_config = load_hugo_config(hugo_root)
            return json.dumps(hugo_config, indent=2, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _stats_fn() -> str:
        db = getattr(mcp, "_test_db", None)
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
