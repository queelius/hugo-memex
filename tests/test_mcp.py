"""Tests for hugo_memex.mcp.

Tests the MCP server by directly calling the underlying tool/resource
functions. FastMCP v2 wraps these as async, so we use async fixtures.
"""
import json

import pytest

from hugo_memex.db import Database
from hugo_memex.indexer import index_content
from hugo_memex.mcp import create_server


@pytest.fixture
def indexed_db(hugo_root):
    """Database pre-populated with fixture content."""
    db = Database(":memory:")
    stats = index_content(str(hugo_root), db)
    assert stats["errors"] == []
    return db


@pytest.fixture
def mcp_server(indexed_db, hugo_root):
    """MCP server with test-injected database."""
    config = {"hugo_root": str(hugo_root)}
    return create_server(db=indexed_db, config=config)


@pytest.fixture
def writable_mcp_server(tmp_path, fixtures_dir):
    """MCP server on a writable copy of the fixture site."""
    import shutil
    site = tmp_path / "site"
    shutil.copytree(fixtures_dir, site)
    db = Database(":memory:")
    stats = index_content(str(site), db)
    assert stats["errors"] == []
    config = {"hugo_root": str(site)}
    return create_server(db=db, config=config), site


async def _get_tool_fn(server, name):
    """Get a registered tool's underlying function."""
    tool = await server.get_tool(name)
    return tool.fn


async def _get_resource_fn(server, uri):
    """Get a registered resource's underlying function."""
    resource = await server.get_resource(uri)
    return resource.fn


class TestExecuteSQL:
    @pytest.mark.asyncio
    async def test_select_pages(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "execute_sql")
        result = fn(sql="SELECT path, title FROM pages ORDER BY path")
        assert len(result) >= 4
        paths = {r["path"] for r in result}
        assert "post/test-post/index.md" in paths

    @pytest.mark.asyncio
    async def test_select_with_params(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "execute_sql")
        result = fn(
            sql="SELECT path FROM pages WHERE section = ?",
            params=["post"],
        )
        assert all(r["path"].startswith("post/") for r in result)

    @pytest.mark.asyncio
    async def test_fts_search(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "execute_sql")
        result = fn(
            sql=(
                "SELECT p.path, p.title FROM pages_fts f "
                "JOIN pages p ON p.path = f.path "
                "WHERE pages_fts MATCH 'Python'"
            ),
        )
        assert any("Python" in r["title"] for r in result)

    @pytest.mark.asyncio
    async def test_taxonomy_query(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "execute_sql")
        result = fn(
            sql=(
                "SELECT p.path FROM pages p "
                "JOIN taxonomies t ON p.path = t.page_path "
                "WHERE t.taxonomy = 'tags' AND t.term = ?"
            ),
            params=["python"],
        )
        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_json_extract(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "execute_sql")
        result = fn(
            sql=(
                "SELECT path, json_extract(front_matter, '$.project.status') as status "
                "FROM pages WHERE json_extract(front_matter, '$.project.status') IS NOT NULL"
            ),
        )
        assert any(r["status"] == "active" for r in result)

    @pytest.mark.asyncio
    async def test_write_blocked(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "execute_sql")
        with pytest.raises(Exception):
            fn(sql="DELETE FROM pages")

    @pytest.mark.asyncio
    async def test_pragma_bypass_blocked(self, mcp_server):
        """PRAGMA query_only=OFF must not bypass the authorizer."""
        fn = await _get_tool_fn(mcp_server, "execute_sql")
        with pytest.raises(Exception):
            fn(sql="PRAGMA query_only=OFF")
        # Writes should still be blocked after the attempt
        with pytest.raises(Exception):
            fn(sql="DELETE FROM pages")

    @pytest.mark.asyncio
    async def test_pragma_writable_schema_blocked(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "execute_sql")
        with pytest.raises(Exception):
            fn(sql="PRAGMA writable_schema=ON")

    @pytest.mark.asyncio
    async def test_drop_table_blocked(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "execute_sql")
        with pytest.raises(Exception):
            fn(sql="DROP TABLE pages")

    @pytest.mark.asyncio
    async def test_attach_database_blocked(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "execute_sql")
        with pytest.raises(Exception):
            fn(sql="ATTACH DATABASE ':memory:' AS evil")

    @pytest.mark.asyncio
    async def test_invalid_sql(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "execute_sql")
        with pytest.raises(Exception):
            fn(sql="SELECT * FROM nonexistent_table")


class TestGetContent:
    @pytest.mark.asyncio
    async def test_get_content(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "get_content")
        result = fn(path="post/test-post/index.md")
        assert "Test Post About Python" in result
        assert "SQLite Integration" in result

    @pytest.mark.asyncio
    async def test_get_content_not_found(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "get_content")
        with pytest.raises(Exception, match="not found"):
            fn(path="nonexistent/file.md")

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "get_content")
        with pytest.raises(Exception, match="within content"):
            fn(path="../../etc/passwd")

    @pytest.mark.asyncio
    async def test_path_prefix_collision_blocked(self, mcp_server):
        """Directory names that are prefixes of content/ must be blocked."""
        fn = await _get_tool_fn(mcp_server, "get_content")
        # ../content-evil/file.md would pass a naive startswith check
        with pytest.raises(Exception):
            fn(path="../content-evil/file.md")


class TestGetPages:
    @pytest.mark.asyncio
    async def test_get_pages_by_section(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "get_pages")
        result = fn(section="post")
        assert len(result) >= 1
        assert all(r["section"] == "post" for r in result)
        # Should include body by default
        assert "body" in result[0]
        assert len(result[0]["body"]) > 0
        # Should include taxonomies
        assert "taxonomies" in result[0]
        # front_matter should be a dict, not a JSON string
        assert isinstance(result[0]["front_matter"], dict)

    @pytest.mark.asyncio
    async def test_get_pages_by_tag(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "get_pages")
        result = fn(tag="python")
        assert len(result) >= 1
        for r in result:
            assert "python" in r["taxonomies"].get("tags", [])

    @pytest.mark.asyncio
    async def test_get_pages_fts_search(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "get_pages")
        result = fn(search="SQLite embedded database")
        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_get_pages_by_paths(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "get_pages")
        result = fn(paths=["post/test-post/index.md", "projects/test-project/index.md"])
        assert len(result) == 2
        paths = {r["path"] for r in result}
        assert "post/test-post/index.md" in paths
        assert "projects/test-project/index.md" in paths

    @pytest.mark.asyncio
    async def test_get_pages_no_body(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "get_pages")
        result = fn(section="post", include_body=False)
        assert len(result) >= 1
        assert "body" not in result[0]

    @pytest.mark.asyncio
    async def test_get_pages_with_drafts(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "get_pages")
        # Without drafts
        no_drafts = fn(section="media")
        # With drafts
        with_drafts = fn(section="media", include_drafts=True)
        assert len(with_drafts) >= len(no_drafts)

    @pytest.mark.asyncio
    async def test_get_pages_limit(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "get_pages")
        result = fn(section="post", limit=1)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_get_pages_requires_filter(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "get_pages")
        with pytest.raises(Exception, match="filter"):
            fn()

    @pytest.mark.asyncio
    async def test_get_pages_combined_filters(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "get_pages")
        result = fn(section="post", tag="python")
        assert len(result) >= 1
        for r in result:
            assert r["section"] == "post"
            assert "python" in r["taxonomies"].get("tags", [])


class TestRebuildIndex:
    @pytest.mark.asyncio
    async def test_rebuild_incremental(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "rebuild_index")
        result = fn()
        assert result["unchanged"] >= 4
        assert result["indexed"] == 0

    @pytest.mark.asyncio
    async def test_rebuild_force(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "rebuild_index")
        result = fn(force=True)
        assert result["indexed"] >= 4

    @pytest.mark.asyncio
    async def test_rebuild_specific_paths(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "rebuild_index")
        result = fn(paths=["post/test-post/index.md"], force=True)
        assert result["indexed"] == 1

    @pytest.mark.asyncio
    async def test_readonly_restored_after_rebuild(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "rebuild_index")
        fn(force=True)
        sql_fn = await _get_tool_fn(mcp_server, "execute_sql")
        with pytest.raises(Exception):
            sql_fn(sql="DELETE FROM pages")

    @pytest.mark.asyncio
    async def test_rebuild_path_traversal_blocked(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "rebuild_index")
        with pytest.raises(Exception, match="within content"):
            fn(paths=["../../etc/passwd"])


class TestResources:
    @pytest.mark.asyncio
    async def test_schema_resource(self, mcp_server):
        fn = await _get_resource_fn(mcp_server, "hugo://schema")
        result = fn()
        assert "CREATE TABLE" in result
        assert "pages_fts" in result
        assert "json_extract" in result

    @pytest.mark.asyncio
    async def test_site_resource(self, mcp_server):
        fn = await _get_resource_fn(mcp_server, "hugo://site")
        result = fn()
        data = json.loads(result)
        assert data["title"] == "Test Site"
        assert "taxonomies" in data

    @pytest.mark.asyncio
    async def test_stats_resource(self, mcp_server):
        fn = await _get_resource_fn(mcp_server, "hugo://stats")
        result = fn()
        data = json.loads(result)
        assert data["total_pages"] >= 4
        assert "pages_by_section" in data
        assert "taxonomies" in data


class TestServerSetup:
    @pytest.mark.asyncio
    async def test_tools_registered(self, mcp_server):
        tools = await mcp_server.get_tools()
        # FastMCP v2 returns a dict keyed by name
        if isinstance(tools, dict):
            tool_names = set(tools.keys())
        else:
            tool_names = {t.name for t in tools}
        assert "execute_sql" in tool_names
        assert "get_content" in tool_names
        assert "rebuild_index" in tool_names

    @pytest.mark.asyncio
    async def test_resources_registered(self, mcp_server):
        resources = await mcp_server.get_resources()
        # FastMCP v2 returns a dict keyed by URI string
        if isinstance(resources, dict):
            resource_uris = set(resources.keys())
        else:
            resource_uris = {str(r.uri) for r in resources}
        assert "hugo://schema" in resource_uris
        assert "hugo://site" in resource_uris
        assert "hugo://stats" in resource_uris


class TestCreatePageWrapper:
    @pytest.mark.asyncio
    async def test_extra_front_matter_does_not_clobber_title(self, writable_mcp_server):
        """Explicit title must win over extra_front_matter.title."""
        server, site = writable_mcp_server
        fn = await _get_tool_fn(server, "create_page")
        result = fn(
            section="post", slug="title-check",
            title="Real Title", body="body",
            extra_front_matter={"title": "HIJACKED", "other": 42},
        )
        assert result["status"] == "created"
        # Verify the actual file has the explicit title, not the hijack
        from hugo_memex.parser import parse_content
        raw = (site / "content" / result["path"]).read_text()
        fm, _ = parse_content(raw)
        assert fm["title"] == "Real Title"
        assert fm["other"] == 42

    @pytest.mark.asyncio
    async def test_traversal_via_slug_rejected(self, writable_mcp_server):
        server, site = writable_mcp_server
        fn = await _get_tool_fn(server, "create_page")
        with pytest.raises(Exception, match="Invalid slug"):
            fn(
                section="post", slug="../../../tmp/evil",
                title="x", body="body",
            )
