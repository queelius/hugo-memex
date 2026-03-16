"""Tests for hugo_memex.db."""
import pytest
from hugo_memex.db import Database


class TestDatabaseInit:
    def test_memory_database(self, db):
        assert db.conn is not None
        tables = db.execute_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        table_names = {r["name"] for r in tables}
        assert "pages" in table_names
        assert "taxonomies" in table_names
        assert "sync_state" in table_names
        assert "schema_version" in table_names

    def test_schema_version_set(self, db):
        rows = db.execute_sql("SELECT version FROM schema_version")
        assert rows[0]["version"] == 1

    def test_fts5_table_exists(self, db):
        tables = db.execute_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        table_names = {r["name"] for r in tables}
        assert "pages_fts" in table_names

    def test_context_manager(self):
        with Database(":memory:") as db:
            assert db.conn is not None
        assert db.conn is None

    def test_readonly_mode(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        # Create first
        db1 = Database(db_path)
        db1.close()
        # Open readonly
        db2 = Database(db_path, readonly=True)
        with pytest.raises(Exception):
            db2.execute_sql("INSERT INTO pages (path, title, section, kind, content_hash, indexed_at) VALUES ('x','x','x','x','x','x')")
        db2.close()


class TestPageCRUD:
    def test_save_and_query_page(self, db, sample_page):
        db.save_page(sample_page)
        rows = db.execute_sql("SELECT * FROM pages WHERE path = ?", ("post/test-post/index.md",))
        assert len(rows) == 1
        assert rows[0]["title"] == "Test Post About Python"
        assert rows[0]["section"] == "post"
        assert rows[0]["draft"] == 0

    def test_save_page_replaces_existing(self, db, sample_page):
        db.save_page(sample_page)
        sample_page["title"] = "Updated Title"
        db.save_page(sample_page)
        rows = db.execute_sql("SELECT * FROM pages")
        assert len(rows) == 1
        assert rows[0]["title"] == "Updated Title"

    def test_delete_page(self, db, sample_page):
        db.save_page(sample_page)
        assert db.delete_page("post/test-post/index.md") is True
        rows = db.execute_sql("SELECT * FROM pages")
        assert len(rows) == 0

    def test_delete_nonexistent_page(self, db):
        assert db.delete_page("nonexistent.md") is False

    def test_front_matter_stored_as_json(self, db, sample_page):
        db.save_page(sample_page)
        rows = db.execute_sql(
            "SELECT json_extract(front_matter, '$.tags') as tags FROM pages"
        )
        assert "python" in rows[0]["tags"]


class TestTaxonomies:
    def test_save_and_query_taxonomies(self, db, sample_page, sample_taxonomies):
        db.save_page(sample_page)
        db.save_taxonomies("post/test-post/index.md", sample_taxonomies)
        rows = db.execute_sql(
            "SELECT * FROM taxonomies WHERE page_path = ? ORDER BY taxonomy, term",
            ("post/test-post/index.md",),
        )
        assert len(rows) == 4  # python, sqlite, programming, tutorials
        terms_by_tax = {}
        for r in rows:
            terms_by_tax.setdefault(r["taxonomy"], []).append(r["term"])
        assert terms_by_tax["tags"] == ["python", "sqlite"]
        assert terms_by_tax["categories"] == ["programming"]

    def test_taxonomies_cascade_on_page_delete(self, db, sample_page, sample_taxonomies):
        db.save_page(sample_page)
        db.save_taxonomies("post/test-post/index.md", sample_taxonomies)
        db.delete_page("post/test-post/index.md")
        rows = db.execute_sql("SELECT * FROM taxonomies")
        assert len(rows) == 0

    def test_save_taxonomies_replaces_existing(self, db, sample_page, sample_taxonomies):
        db.save_page(sample_page)
        db.save_taxonomies("post/test-post/index.md", sample_taxonomies)
        db.save_taxonomies("post/test-post/index.md", {"tags": ["new-tag"]})
        rows = db.execute_sql("SELECT * FROM taxonomies WHERE page_path = ?", ("post/test-post/index.md",))
        assert len(rows) == 1
        assert rows[0]["term"] == "new-tag"


class TestFTS5:
    def test_fts_search_by_title(self, db, sample_page):
        db.save_page(sample_page)
        rows = db.execute_sql(
            "SELECT path FROM pages_fts WHERE pages_fts MATCH 'Python'"
        )
        assert len(rows) == 1
        assert rows[0]["path"] == "post/test-post/index.md"

    def test_fts_search_by_body(self, db, sample_page):
        db.save_page(sample_page)
        rows = db.execute_sql(
            "SELECT path FROM pages_fts WHERE pages_fts MATCH 'embedded database'"
        )
        assert len(rows) == 1

    def test_fts_porter_stemming(self, db, sample_page):
        db.save_page(sample_page)
        # "programming" should match via porter stemming of "program"
        rows = db.execute_sql(
            "SELECT path FROM pages_fts WHERE pages_fts MATCH 'program'"
        )
        assert len(rows) == 1

    def test_fts_cleaned_on_page_delete(self, db, sample_page):
        db.save_page(sample_page)
        db.delete_page("post/test-post/index.md")
        rows = db.execute_sql(
            "SELECT * FROM pages_fts WHERE pages_fts MATCH 'Python'"
        )
        assert len(rows) == 0

    def test_fts_join_with_pages(self, db, sample_page):
        db.save_page(sample_page)
        rows = db.execute_sql(
            "SELECT p.path, p.title, p.section FROM pages_fts f "
            "JOIN pages p ON p.path = f.path "
            "WHERE pages_fts MATCH 'SQLite' ORDER BY rank"
        )
        assert len(rows) == 1
        assert rows[0]["section"] == "post"


class TestSyncState:
    def test_save_and_get_sync_state(self, db):
        db.save_sync_state("post/test.md", "hash123", 1234567890.0, "2024-01-01T00:00:00Z")
        state = db.get_sync_state("post/test.md")
        assert state is not None
        assert state["content_hash"] == "hash123"
        assert state["file_mtime"] == 1234567890.0

    def test_get_nonexistent_sync_state(self, db):
        assert db.get_sync_state("nonexistent.md") is None

    def test_delete_sync_state(self, db):
        db.save_sync_state("post/test.md", "hash123", 1234567890.0, "2024-01-01T00:00:00Z")
        db.delete_sync_state("post/test.md")
        assert db.get_sync_state("post/test.md") is None

    def test_get_all_indexed_paths(self, db, sample_page):
        db.save_page(sample_page)
        paths = db.get_all_indexed_paths()
        assert "post/test-post/index.md" in paths


class TestSchema:
    def test_get_schema_includes_ddl(self, db):
        schema = db.get_schema()
        assert "CREATE TABLE" in schema
        assert "pages" in schema
        assert "taxonomies" in schema

    def test_get_schema_excludes_fts_shadow_tables(self, db):
        schema = db.get_schema()
        assert "pages_fts_" not in schema

    def test_get_schema_includes_docs(self, db):
        schema = db.get_schema()
        assert "json_extract" in schema
        assert "MATCH" in schema


class TestStatistics:
    def test_empty_statistics(self, db):
        stats = db.get_statistics()
        assert stats["total_pages"] == 0
        assert stats["total_word_count"] == 0

    def test_statistics_with_data(self, db, sample_page, sample_taxonomies):
        db.save_page(sample_page)
        db.save_taxonomies("post/test-post/index.md", sample_taxonomies)
        stats = db.get_statistics()
        assert stats["total_pages"] == 1
        assert stats["total_word_count"] == 42
        assert stats["pages_by_section"]["post"] == 1
        assert stats["draft_status"]["published"] == 1
        assert "tags" in stats["taxonomies"]
        assert stats["taxonomies"]["tags"]["distinct_terms"] == 2
