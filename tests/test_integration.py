"""Integration tests: end-to-end pipeline through MCP tools."""
import json
import shutil
from pathlib import Path

import pytest

from hugo_memex.db import Database
from hugo_memex.indexer import index_content
from hugo_memex.mcp import create_server
from hugo_memex.writer import add_marginalia, delete_marginalia_from_disk


@pytest.fixture
def full_server(hugo_root):
    """Full end-to-end: index content then create MCP server."""
    db = Database(":memory:")
    config = {"hugo_root": str(hugo_root)}
    stats = index_content(str(hugo_root), db)
    assert stats["indexed"] >= 4
    assert stats["errors"] == []
    return create_server(db=db, config=config), stats


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_index_then_query(self, full_server):
        """Index fixture content, then query via MCP tools."""
        server, stats = full_server
        execute = (await server.get_tool("execute_sql")).fn

        # Verify all fixture files indexed
        pages = execute(sql="SELECT path, title, section, kind, draft FROM pages ORDER BY path")
        assert len(pages) >= 4

        # Verify sections
        sections = {p["section"] for p in pages}
        assert "post" in sections
        assert "projects" in sections
        assert "media" in sections

    @pytest.mark.asyncio
    async def test_fts_across_fields(self, full_server):
        """FTS5 searches across title, description, and body."""
        server, _ = full_server
        execute = (await server.get_tool("execute_sql")).fn

        # Search body content
        body_hits = execute(
            sql="SELECT path FROM pages_fts WHERE pages_fts MATCH 'embedded database'"
        )
        assert len(body_hits) >= 1

        # Search title
        title_hits = execute(
            sql="SELECT path FROM pages_fts WHERE pages_fts MATCH 'Test Post'"
        )
        assert len(title_hits) >= 1

    @pytest.mark.asyncio
    async def test_taxonomy_roundtrip(self, full_server):
        """Taxonomies extracted from front matter are queryable."""
        server, _ = full_server
        execute = (await server.get_tool("execute_sql")).fn

        # Query cross-reference: find pages sharing tags with the test post
        cross_ref = execute(
            sql=(
                "SELECT DISTINCT t2.page_path, p2.title "
                "FROM taxonomies t1 "
                "JOIN taxonomies t2 ON t1.taxonomy = t2.taxonomy "
                "  AND t1.term = t2.term "
                "JOIN pages p2 ON t2.page_path = p2.path "
                "WHERE t1.page_path = 'post/test-post/index.md' "
                "  AND t2.page_path != t1.page_path"
            ),
        )
        # The test project shares no tags with the test post,
        # but the book review shares the "books" tag? No, different tags.
        # This just verifies the query runs without error.
        assert isinstance(cross_ref, list)

    @pytest.mark.asyncio
    async def test_json_front_matter_preserved(self, full_server):
        """Complex front matter is preserved losslessly as JSON."""
        server, _ = full_server
        execute = (await server.get_tool("execute_sql")).fn

        # Query nested front matter
        project_pages = execute(
            sql=(
                "SELECT path, front_matter FROM pages "
                "WHERE json_extract(front_matter, '$.project.status') = 'active'"
            ),
        )
        assert len(project_pages) >= 1
        fm = json.loads(project_pages[0]["front_matter"])
        assert fm["project"]["status"] == "active"
        assert fm["project"]["type"] == "library"
        assert "Rust" in fm["tech"]["languages"]

    @pytest.mark.asyncio
    async def test_get_content_matches_index(self, full_server):
        """get_content returns the same content that was indexed."""
        server, _ = full_server
        execute = (await server.get_tool("execute_sql")).fn
        get_content = (await server.get_tool("get_content")).fn

        # Get indexed body
        indexed = execute(
            sql="SELECT body FROM pages WHERE path = 'post/test-post/index.md'"
        )
        assert len(indexed) == 1

        # Get raw content
        raw = get_content(path="post/test-post/index.md")
        assert "Test Post About Python" in raw
        # Body from index should be part of raw content (after front matter)
        assert "SQLite Integration" in indexed[0]["body"]
        assert "SQLite Integration" in raw

    @pytest.mark.asyncio
    async def test_rebuild_after_initial_index(self, full_server):
        """rebuild_index correctly does incremental re-index."""
        server, initial_stats = full_server
        rebuild = (await server.get_tool("rebuild_index")).fn

        # Incremental should skip everything
        result = rebuild()
        assert result["indexed"] == 0
        assert result["unchanged"] == initial_stats["indexed"]

        # Force should re-index everything
        result = rebuild(force=True)
        assert result["indexed"] == initial_stats["indexed"]

    @pytest.mark.asyncio
    async def test_resources_consistent(self, full_server):
        """Resource data is consistent with tool query results."""
        server, _ = full_server
        execute = (await server.get_tool("execute_sql")).fn

        # Get stats from resource
        stats_fn = (await server.get_resource("hugo://stats")).fn
        stats = json.loads(stats_fn())

        # Verify against direct SQL
        total = execute(sql="SELECT COUNT(*) as n FROM pages")[0]["n"]
        assert stats["total_pages"] == total

        # Verify schema resource
        schema_fn = (await server.get_resource("hugo://schema")).fn
        schema = schema_fn()
        assert "pages" in schema
        assert "taxonomies" in schema

    @pytest.mark.asyncio
    async def test_draft_and_published_counts(self, full_server):
        """Draft/published counts are correct."""
        server, _ = full_server
        execute = (await server.get_tool("execute_sql")).fn

        drafts = execute(sql="SELECT COUNT(*) as n FROM pages WHERE draft = 1")
        published = execute(sql="SELECT COUNT(*) as n FROM pages WHERE draft = 0")

        stats_fn = (await server.get_resource("hugo://stats")).fn
        stats = json.loads(stats_fn())

        assert stats["draft_status"]["draft"] == drafts[0]["n"]
        assert stats["draft_status"]["published"] == published[0]["n"]


class TestRealSiteIntegration:
    """Integration tests against the actual metafunctor site."""

    @pytest.fixture
    def real_server(self):
        metafunctor = Path("~/github/repos/metafunctor").expanduser()
        if not (metafunctor / "hugo.toml").exists():
            pytest.skip("metafunctor not available")
        db = Database(":memory:")
        stats = index_content(str(metafunctor), db)
        assert stats["errors"] == []
        config = {"hugo_root": str(metafunctor)}
        return create_server(db=db, config=config), stats

    @pytest.mark.asyncio
    async def test_real_site_comprehensive(self, real_server):
        """Comprehensive verification against metafunctor."""
        server, stats = real_server
        execute = (await server.get_tool("execute_sql")).fn

        # Should have substantial content
        assert stats["indexed"] > 100

        # Verify diverse sections
        sections = execute(
            sql="SELECT section, COUNT(*) as n FROM pages GROUP BY section ORDER BY n DESC"
        )
        section_names = {r["section"] for r in sections}
        assert "post" in section_names
        assert "projects" in section_names

        # Verify taxonomies work
        top_tags = execute(
            sql="SELECT term, COUNT(*) as n FROM taxonomies WHERE taxonomy = 'tags' GROUP BY term ORDER BY n DESC LIMIT 5"
        )
        assert len(top_tags) >= 5

        # Verify FTS works on real content
        fts_results = execute(
            sql=(
                "SELECT p.path, p.title FROM pages_fts f "
                "JOIN pages p ON p.path = f.path "
                "WHERE pages_fts MATCH 'machine learning' "
                "ORDER BY rank LIMIT 5"
            ),
        )
        assert len(fts_results) >= 1

        # Verify JSON front matter queries
        active_projects = execute(
            sql=(
                "SELECT path, title FROM pages "
                "WHERE json_extract(front_matter, '$.project.status') = 'active'"
            ),
        )
        assert len(active_projects) >= 1

        # Verify get_content works
        get_content = (await server.get_tool("get_content")).fn
        if active_projects:
            content = get_content(path=active_projects[0]["path"])
            assert len(content) > 0


class TestMarginaliaLifecycle:
    """Full lifecycle: index, add, re-index, delete, orphan survival, FTS."""

    def test_full_lifecycle(self, fixtures_dir, tmp_path):
        # 1. Copy fixtures to a writable tmp directory
        site = tmp_path / "site"
        shutil.copytree(fixtures_dir, site)

        # 2. Create in-memory DB
        db = Database(":memory:")

        # 3. Initial index: fixture marginalia should be picked up
        stats = index_content(str(site), db)
        assert stats["marginalia_indexed"] >= 2
        assert stats["errors"] == []

        # 4. Query fixture marginalia for the test post
        notes = db.get_marginalia("post/test-post/index.md")
        assert len(notes) >= 2
        original_count = len(notes)

        # 5. Add a new note via the writer
        result = add_marginalia(
            str(site), "post/test-post/index.md", "New lifecycle note",
        )
        assert result["status"] == "created"
        assert result["id"].startswith("mg-")

        # 6. Re-index: the changed YAML file should be re-indexed
        stats2 = index_content(str(site), db)
        assert stats2["marginalia_indexed"] >= 1

        # 7. Query again: count should be original + 1, new note present
        notes_after_add = db.get_marginalia("post/test-post/index.md")
        assert len(notes_after_add) == original_count + 1
        bodies = [n["body"] for n in notes_after_add]
        assert "New lifecycle note" in bodies

        # 8. Delete the new note from disk
        delete_result = delete_marginalia_from_disk(
            str(site), result["source_file"], result["id"],
        )
        assert delete_result["status"] == "deleted"

        # 9. Re-index: count should return to original
        stats3 = index_content(str(site), db)
        notes_after_delete = db.get_marginalia("post/test-post/index.md")
        assert len(notes_after_delete) == original_count

        # 10. Orphan survival: deleting the page should NOT delete marginalia
        db.delete_page("post/test-post/index.md")
        orphan_notes = db.get_marginalia("post/test-post/index.md")
        assert len(orphan_notes) == original_count

        # 11. FTS still works: MATCH 'Python' on marginalia_fts returns results
        fts_hits = db.execute_sql(
            "SELECT id, body FROM marginalia_fts "
            "WHERE marginalia_fts MATCH 'Python'"
        )
        assert len(fts_hits) >= 1

        # 12. Close DB
        db.close()
