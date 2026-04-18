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


def _get_tag_taxonomy(config: dict) -> str:
    """Determine which taxonomy name a Hugo site uses for tags.

    Hugo's convention is `tag = "tags"` (singular key, plural value).
    We look up the plural form whose singular is "tag"; if the site doesn't
    define one (e.g. taxonomies disabled), fall back to the default "tags".
    """
    hugo_root = config.get("hugo_root") if config else None
    if not hugo_root:
        return "tags"
    try:
        hugo_config = load_hugo_config(hugo_root)
        taxonomies = hugo_config.get("taxonomies", {}) or {}
        # taxonomies maps singular → plural. Find the plural whose singular is "tag".
        for singular, plural in taxonomies.items():
            if singular == "tag":
                return plural
    except Exception:
        pass
    return "tags"


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


class _WriteSession:
    """Context manager that yields a writable Database for the given read-only db.

    For :memory: databases (used in tests), lifts the authorizer on the shared
    connection and restores it on exit. For file-backed databases, opens a
    separate read-write connection and closes it on exit. This mirrors the
    pattern used by rebuild_index.
    """

    def __init__(self, db):
        self._base = db
        self._owned = None

    def __enter__(self):
        if self._base.db_path == ":memory:":
            self._base.conn.set_authorizer(None)
            return self._base
        self._owned = Database(self._base.db_path)
        return self._owned

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._owned is not None:
            self._owned.close()
        else:
            from hugo_memex.db import _readonly_authorizer
            self._base.conn.set_authorizer(_readonly_authorizer)
        return False


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
  WHERE kind = 'page' AND draft = 0 AND archived_at IS NULL
  ORDER BY date DESC LIMIT 20

Full-text search:
  SELECT p.path, p.title, p.section, p.date
  FROM pages_fts f JOIN pages p ON p.path = f.path
  WHERE pages_fts MATCH 'search terms'
  ORDER BY rank LIMIT 20

Pages by tag:
  SELECT p.path, p.title, p.date FROM pages p
  JOIN taxonomies t ON p.path = t.page_path
  WHERE t.taxonomy = 'tags' AND t.term = 'python' AND p.archived_at IS NULL

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
        include_archived: Annotated[
            bool,
            Field(description="Include archived pages (default false)"),
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
        if not any([section, tag, search, paths]):
            raise ToolError("Provide at least one filter: section, tag, search, or paths")

        database = _get_db(mcp, ctx)
        conds = []
        params = []

        if paths:
            placeholders = ",".join("?" for _ in paths)
            conds.append(f"p.path IN ({placeholders})")
            params.extend(paths)

        if section:
            conds.append("p.section = ?")
            params.append(section)

        if not include_drafts:
            conds.append("p.draft = 0")

        if not include_archived:
            conds.append("p.archived_at IS NULL")

        # Shared column list; join with FTS only when searching
        columns = (
            "p.path, p.title, p.section, p.date, p.slug, "
            "p.kind, p.bundle_type, p.draft, p.description, "
            "p.word_count, p.front_matter"
        )
        if include_body:
            columns += ", p.body"

        if search:
            base = f"SELECT {columns} FROM pages_fts f JOIN pages p ON p.path = f.path"
            conds.append("pages_fts MATCH ?")
            params.append(search)
        else:
            base = f"SELECT {columns} FROM pages p"

        if tag:
            tag_taxonomy = _get_tag_taxonomy(_get_config(mcp, ctx))
            conds.append(
                "EXISTS(SELECT 1 FROM taxonomies t "
                "WHERE t.page_path = p.path AND t.taxonomy = ? AND t.term = ?)"
            )
            params.extend([tag_taxonomy, tag])

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

        if not rows:
            return []

        # Fetch taxonomies for all returned pages in a single query (avoids N+1).
        row_paths = [row["path"] for row in rows]
        tax_placeholders = ",".join("?" for _ in row_paths)
        tax_rows = database.execute_sql(
            f"SELECT page_path, taxonomy, term FROM taxonomies "
            f"WHERE page_path IN ({tax_placeholders})",
            tuple(row_paths),
        )
        taxonomies_by_path: dict[str, dict[str, list[str]]] = {}
        for tr in tax_rows:
            taxonomies_by_path.setdefault(tr["page_path"], {}).setdefault(
                tr["taxonomy"], []
            ).append(tr["term"])

        result = []
        for row in rows:
            page = dict(row)
            if isinstance(page.get("front_matter"), str):
                page["front_matter"] = json.loads(page["front_matter"])
            page["taxonomies"] = taxonomies_by_path.get(page["path"], {})
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
        with _WriteSession(database) as write_db:
            return index_content(hugo_root, write_db, paths=paths, force=force)


    # ── Writing tools ──────────────────────────────────────────

    @mcp.tool()
    def create_page(
        section: Annotated[str, Field(description="Content section (e.g. 'post', 'projects')")],
        slug: Annotated[str, Field(description="URL slug (becomes directory name)")],
        title: Annotated[str, Field(description="Page title")],
        body: Annotated[str, Field(description="Markdown body content")],
        tags: Annotated[list[str] | None, Field(description="Tags list")] = None,
        categories: Annotated[list[str] | None, Field(description="Categories list")] = None,
        description: Annotated[str | None, Field(description="Short description")] = None,
        extra_front_matter: Annotated[dict | None, Field(description="Additional front matter fields to merge")] = None,
        draft: Annotated[bool, Field(description="Create as draft (default true)")] = True,
        bundle: Annotated[bool, Field(description="Create as leaf bundle with index.md (default true)")] = True,
        ctx: Context | None = None,
    ) -> dict:
        """Create a new Hugo content page with proper directory structure and front matter.

Creates a leaf bundle (section/slug/index.md) by default. Front matter
follows the conventions of existing pages in the section.

After creating, call rebuild_index(paths=[result.path]) to add it to the index.
"""
        config = _get_config(mcp, ctx)
        hugo_root = config.get("hugo_root")
        if not hugo_root:
            raise ToolError("hugo_root not configured")

        from hugo_memex.writer import create_page as _create

        # Merge extra first, then let explicit args overwrite — the explicit
        # parameters are more intentional than a catch-all dict.
        fm: dict = dict(extra_front_matter) if extra_front_matter else {}
        fm["title"] = title
        fm["draft"] = draft
        if description:
            fm["description"] = description
        if tags:
            fm["tags"] = tags
        if categories:
            fm["categories"] = categories

        try:
            return _create(hugo_root, section, slug, fm, body, bundle=bundle)
        except (FileExistsError, FileNotFoundError, ValueError) as e:
            raise ToolError(str(e))

    @mcp.tool()
    def update_page(
        path: Annotated[str, Field(description="Content path relative to content/")],
        front_matter: Annotated[dict | None, Field(description="Front matter fields to merge (not replace)")] = None,
        body: Annotated[str | None, Field(description="New markdown body (replaces entire body)")] = None,
        ctx: Context | None = None,
    ) -> dict:
        """Update an existing Hugo content page's front matter and/or body.

Front matter is merged (only specified keys change). Body is replaced entirely
if provided. Call rebuild_index(paths=[path]) after to update the index.
"""
        config = _get_config(mcp, ctx)
        hugo_root = config.get("hugo_root")
        if not hugo_root:
            raise ToolError("hugo_root not configured")

        from hugo_memex.writer import update_page as _update

        try:
            return _update(hugo_root, path, front_matter=front_matter, body=body)
        except (FileNotFoundError, ValueError) as e:
            raise ToolError(str(e))

    @mcp.tool(annotations={"readOnlyHint": True})
    def suggest_tags(
        text: Annotated[str, Field(description="Content text to analyze for tag suggestions")],
        limit: Annotated[int, Field(description="Max suggestions (default 10)")] = 10,
        ctx: Context | None = None,
    ) -> list[dict]:
        """Suggest existing tags based on content text using FTS5 similarity.

Finds pages similar to the given text and returns their most common tags,
with canonical casing (resolves Python/python, AI/ai duplicates).
Use this when writing new content to pick consistent, relevant tags.
"""
        database = _get_db(mcp, ctx)
        taxonomy = _get_tag_taxonomy(_get_config(mcp, ctx))
        from hugo_memex.writer import suggest_tags as _suggest
        return _suggest(database, text, limit=limit, taxonomy=taxonomy)

    @mcp.tool(annotations={"readOnlyHint": True})
    def get_front_matter_template(
        section: Annotated[str, Field(description="Section to derive template from (e.g. 'post', 'projects')")],
        ctx: Context | None = None,
    ) -> dict:
        """Get the front matter template for a section, derived from existing pages.

Returns each common key with its type, frequency, example value, and default.
No hardcoded templates — derived from the actual data in the index.
Use this before create_page to know what front matter fields the section expects.
"""
        database = _get_db(mcp, ctx)
        from hugo_memex.writer import get_front_matter_template as _template
        return _template(database, section)

    @mcp.tool(annotations={"readOnlyHint": True})
    def validate_page(
        path: Annotated[str, Field(description="Content path relative to content/")],
        ctx: Context | None = None,
    ) -> dict:
        """Validate a page for completeness and consistency.

Checks: required fields (title, date, description, tags), tag case consistency
(flags duplicates like Python/python), cross-reference validity (linked_project,
related_posts), and GPG body hash match.
"""
        config = _get_config(mcp, ctx)
        hugo_root = config.get("hugo_root")
        if not hugo_root:
            raise ToolError("hugo_root not configured")

        database = _get_db(mcp, ctx)
        taxonomy = _get_tag_taxonomy(config)
        from hugo_memex.writer import validate_page as _validate
        return _validate(database, hugo_root, path, tag_taxonomy=taxonomy)

    # -- Marginalia tools -----------------------------------------

    @mcp.tool(annotations={"readOnlyHint": True})
    def get_marginalia(
        page_path: Annotated[
            str,
            Field(description="Content path relative to content/"),
        ],
        include_archived: Annotated[
            bool,
            Field(description="If True, include archived notes. Default False returns only active notes."),
        ] = False,
        ctx: Context | None = None,
    ) -> list[dict]:
        """Get all marginalia notes for a page.

By default excludes archived notes. Pass include_archived=True to see the
full history, including soft-deleted notes.
"""
        database = _get_db(mcp, ctx)
        return database.get_marginalia(page_path, include_archived=include_archived)

    @mcp.tool()
    def add_marginalia(
        page_path: Annotated[str, Field(description="Content path relative to content/ (e.g. 'post/my-post/index.md')")],
        body: Annotated[str, Field(description="Note body text (markdown supported)")],
        ctx: Context | None = None,
    ) -> dict:
        """Add a marginalia note to a content page.

Creates (or appends to) a YAML file under data/marginalia/ and synchronously
indexes the new note in the database. No rebuild_index call is required for
subsequent get_marginalia/delete_marginalia/restore_marginalia to see it.
"""
        config = _get_config(mcp, ctx)
        hugo_root = config.get("hugo_root")
        if not hugo_root:
            raise ToolError("hugo_root not configured")

        from hugo_memex.writer import add_marginalia as _add

        try:
            result = _add(hugo_root, page_path, body)
        except (ValueError, FileNotFoundError) as e:
            raise ToolError(str(e))

        # Synchronously index the new note so callers don't have to run
        # rebuild_index before querying, deleting, or restoring it.
        database = _get_db(mcp, ctx)
        from datetime import datetime, timezone
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with _WriteSession(database) as write_db:
            write_db.save_marginalia({
                "id": result["id"],
                "page_path": page_path,
                "body": body,
                "created_at": created_at,
                "source_file": result["source_file"],
                "archived_at": None,
            })
        return result

    @mcp.tool()
    def delete_marginalia(
        id: Annotated[
            str,
            Field(description="Marginalia note ID (e.g. 'mg-abc123def456')"),
        ],
        purge: Annotated[
            bool,
            Field(description="If True, hard-delete the note (remove from YAML and DB). Default False archives the note instead."),
        ] = False,
        ctx: Context | None = None,
    ) -> dict:
        """Delete a marginalia note.

Default behavior is soft delete (archive): adds archived_at to the YAML entry
and updates the DB row. The note is still on disk and still indexed but hidden
from default get_marginalia calls.

With purge=True, removes the note entirely from YAML and DB. The DB row and
FTS entry are hard-deleted. Use with caution: this breaks URI stability.
"""
        config = _get_config(mcp, ctx)
        hugo_root = config.get("hugo_root")
        if not hugo_root:
            raise ToolError("hugo_root not configured")
        database = _get_db(mcp, ctx)
        rows = database.execute_sql(
            "SELECT source_file, archived_at FROM marginalia WHERE id = ?",
            (id,),
        )
        if not rows:
            raise ToolError(f"Marginalia note not found: {id}")
        source_file = rows[0]["source_file"]

        if purge:
            from hugo_memex.writer import purge_marginalia_from_disk
            try:
                purge_marginalia_from_disk(hugo_root, source_file, id)
            except (ValueError, FileNotFoundError) as e:
                raise ToolError(str(e))
            with _WriteSession(database) as write_db:
                write_db.delete_marginalia(id)
            return {"id": id, "status": "purged"}

        if rows[0]["archived_at"] is not None:
            return {"id": id, "status": "already_archived"}
        from hugo_memex.writer import archive_marginalia_on_disk
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            archive_marginalia_on_disk(hugo_root, source_file, id, now_iso)
        except (ValueError, FileNotFoundError) as e:
            raise ToolError(str(e))
        with _WriteSession(database) as write_db:
            write_db.archive_marginalia(id, now_iso)
        return {"id": id, "status": "archived", "archived_at": now_iso}

    @mcp.tool()
    def restore_marginalia(
        id: Annotated[
            str,
            Field(description="Marginalia note ID to restore (e.g. 'mg-abc123def456')"),
        ],
        ctx: Context | None = None,
    ) -> dict:
        """Restore an archived marginalia note.

Removes the archived_at field from the YAML entry and clears the DB row's
archived_at synchronously. No-op (returns already_active) if the note is
not currently archived.
"""
        config = _get_config(mcp, ctx)
        hugo_root = config.get("hugo_root")
        if not hugo_root:
            raise ToolError("hugo_root not configured")
        database = _get_db(mcp, ctx)
        rows = database.execute_sql(
            "SELECT source_file, archived_at FROM marginalia WHERE id = ?",
            (id,),
        )
        if not rows:
            raise ToolError(f"Marginalia note not found: {id}")
        if rows[0]["archived_at"] is None:
            return {"id": id, "status": "already_active"}
        source_file = rows[0]["source_file"]
        from hugo_memex.writer import restore_marginalia_on_disk
        try:
            restore_marginalia_on_disk(hugo_root, source_file, id)
        except (ValueError, FileNotFoundError) as e:
            raise ToolError(str(e))
        with _WriteSession(database) as write_db:
            write_db.restore_marginalia_row(id)
        return {"id": id, "status": "restored"}


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
