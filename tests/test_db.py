"""Tests for hugo_memex.db."""
import uuid

import pytest
from hugo_memex.db import Database, SCHEMA_VERSION


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
        assert rows[0]["version"] == SCHEMA_VERSION

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


class TestIndexPage:
    def test_atomic_index_page(self, db, sample_page, sample_taxonomies):
        """index_page saves page + taxonomies + sync state atomically."""
        db.index_page(sample_page, sample_taxonomies, 1234567890.0, "2024-01-01T00:00:00Z")
        pages = db.execute_sql("SELECT * FROM pages")
        assert len(pages) == 1
        taxs = db.execute_sql("SELECT * FROM taxonomies")
        assert len(taxs) == 4
        sync = db.get_sync_state("post/test-post/index.md")
        assert sync is not None
        assert sync["content_hash"] == "abc123"

    def test_atomic_index_page_no_taxonomies(self, db, sample_page):
        """index_page works with empty taxonomies."""
        db.index_page(sample_page, {}, 1234567890.0, "2024-01-01T00:00:00Z")
        pages = db.execute_sql("SELECT * FROM pages")
        assert len(pages) == 1
        taxs = db.execute_sql("SELECT * FROM taxonomies")
        assert len(taxs) == 0


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


# ── Task 1: Marginalia schema ──────────────────────────────────


class TestMarginaliaSchema:
    def test_marginalia_table_exists(self, db):
        tables = db.execute_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        table_names = {r["name"] for r in tables}
        assert "marginalia" in table_names

    def test_marginalia_fts_exists(self, db):
        tables = db.execute_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        table_names = {r["name"] for r in tables}
        assert "marginalia_fts" in table_names

    def test_marginalia_insert_and_query(self, db):
        db.execute_sql(
            "INSERT INTO marginalia (id, page_path, body, created_at, source_file) "
            "VALUES (?, ?, ?, ?, ?)",
            ("note-1", "post/test.md", "Hello world", "2024-01-01T00:00:00Z", "notes/test.md"),
        )
        rows = db.execute_sql(
            "SELECT * FROM marginalia WHERE id = ?", ("note-1",)
        )
        assert len(rows) == 1
        assert rows[0]["body"] == "Hello world"
        assert rows[0]["page_path"] == "post/test.md"

    def test_marginalia_fts_search(self, db):
        db.execute_sql(
            "INSERT INTO marginalia (id, page_path, body, created_at, source_file) "
            "VALUES (?, ?, ?, ?, ?)",
            ("note-1", "post/test.md", "quantum entanglement", "2024-01-01T00:00:00Z", "notes/test.md"),
        )
        db.execute_sql(
            "INSERT INTO marginalia_fts (id, body) VALUES (?, ?)",
            ("note-1", "quantum entanglement"),
        )
        rows = db.execute_sql(
            "SELECT id FROM marginalia_fts WHERE marginalia_fts MATCH 'quantum'"
        )
        assert len(rows) == 1
        assert rows[0]["id"] == "note-1"

    def test_marginalia_no_fk_to_pages(self, db):
        """Marginalia with nonexistent page_path should survive (orphan survival)."""
        db.execute_sql(
            "INSERT INTO marginalia (id, page_path, body, created_at, source_file) "
            "VALUES (?, ?, ?, ?, ?)",
            ("orphan-1", "nonexistent/page.md", "orphan note", "2024-01-01T00:00:00Z", "notes/test.md"),
        )
        rows = db.execute_sql(
            "SELECT * FROM marginalia WHERE id = ?", ("orphan-1",)
        )
        assert len(rows) == 1

    def test_marginalia_null_page_path(self, db):
        """Marginalia with NULL page_path should be allowed (unattached note)."""
        db.execute_sql(
            "INSERT INTO marginalia (id, page_path, body, created_at, source_file) "
            "VALUES (?, ?, ?, ?, ?)",
            ("null-1", None, "floating note", "2024-01-01T00:00:00Z", "notes/test.md"),
        )
        rows = db.execute_sql(
            "SELECT * FROM marginalia WHERE id = ?", ("null-1",)
        )
        assert len(rows) == 1
        assert rows[0]["page_path"] is None


class TestMigrationV1ToV2:
    def test_migration_creates_marginalia_tables(self, tmp_path):
        """Create a v1 database, then reopen it to trigger migration to v2."""
        db_path = str(tmp_path / "migrate.db")
        # Create v1 schema manually: use a v1 database
        conn = __import__("sqlite3").connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # Create the v1 schema (pages, taxonomies, pages_fts, sync_state, schema_version)
        from hugo_memex.db import SCHEMA_SQL
        # Strip the marginalia parts for a v1-like schema
        v1_sql = SCHEMA_SQL.split("CREATE TABLE IF NOT EXISTS marginalia")[0]
        conn.executescript(v1_sql)
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.commit()
        # Verify no marginalia table yet
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "marginalia" not in tables
        conn.close()

        # Reopen via Database class — should trigger migration
        db = Database(db_path)
        tables = db.execute_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        table_names = {r["name"] for r in tables}
        assert "marginalia" in table_names
        assert "marginalia_fts" in table_names

        # Schema version should be updated
        rows = db.execute_sql("SELECT version FROM schema_version")
        assert rows[0]["version"] == 2
        db.close()


# ── Task 2: Marginalia CRUD ────────────────────────────────────


def _make_note(note_id=None, page_path="post/test.md", body="A note",
               created_at="2024-01-01T00:00:00Z", source_file="notes/test.md"):
    return {
        "id": note_id or str(uuid.uuid4()),
        "page_path": page_path,
        "body": body,
        "created_at": created_at,
        "source_file": source_file,
    }


class TestMarginaliaCRUD:
    def test_save_marginalia(self, db):
        note = _make_note(note_id="save-1", body="test body")
        db.save_marginalia(note)
        # Row exists in marginalia table
        rows = db.execute_sql(
            "SELECT * FROM marginalia WHERE id = ?", ("save-1",)
        )
        assert len(rows) == 1
        assert rows[0]["body"] == "test body"
        # FTS populated
        fts_rows = db.execute_sql(
            "SELECT id FROM marginalia_fts WHERE marginalia_fts MATCH 'test'"
        )
        assert len(fts_rows) == 1
        assert fts_rows[0]["id"] == "save-1"

    def test_get_marginalia_for_page(self, db):
        for i in range(3):
            db.save_marginalia(_make_note(
                note_id=f"page-a-{i}",
                page_path="post/alpha.md",
                body=f"Note {i} for alpha",
                created_at=f"2024-01-0{i+1}T00:00:00Z",
            ))
        db.save_marginalia(_make_note(
            note_id="page-b-0",
            page_path="post/beta.md",
            body="Note for beta",
        ))
        results = db.get_marginalia("post/alpha.md")
        assert len(results) == 3
        # Ordered by created_at
        assert results[0]["id"] == "page-a-0"
        assert results[2]["id"] == "page-a-2"
        # Other page not included
        assert all(r["page_path"] == "post/alpha.md" for r in results)

    def test_get_marginalia_empty(self, db):
        results = db.get_marginalia("nonexistent/page.md")
        assert results == []

    def test_delete_marginalia(self, db):
        note = _make_note(note_id="del-1", body="delete me")
        db.save_marginalia(note)
        assert db.delete_marginalia("del-1") is True
        # Gone from marginalia table
        rows = db.execute_sql(
            "SELECT * FROM marginalia WHERE id = ?", ("del-1",)
        )
        assert len(rows) == 0
        # Gone from FTS
        fts_rows = db.execute_sql(
            "SELECT id FROM marginalia_fts WHERE marginalia_fts MATCH 'delete'"
        )
        assert len(fts_rows) == 0

    def test_delete_marginalia_not_found(self, db):
        assert db.delete_marginalia("nonexistent-id") is False

    def test_get_all_marginalia_source_files(self, db):
        db.save_marginalia(_make_note(note_id="sf-1", source_file="notes/a.md"))
        db.save_marginalia(_make_note(note_id="sf-2", source_file="notes/b.md"))
        db.save_marginalia(_make_note(note_id="sf-3", source_file="notes/a.md"))
        sources = db.get_all_marginalia_source_files()
        assert sources == {"notes/a.md", "notes/b.md"}

    def test_delete_marginalia_by_source(self, db):
        db.save_marginalia(_make_note(
            note_id="src-1", source_file="notes/a.md", body="alpha content",
        ))
        db.save_marginalia(_make_note(
            note_id="src-2", source_file="notes/a.md", body="beta content",
        ))
        db.save_marginalia(_make_note(
            note_id="src-3", source_file="notes/b.md", body="gamma content",
        ))
        deleted = db.delete_marginalia_by_source("notes/a.md")
        assert deleted == 2
        # Only the note from b.md remains
        rows = db.execute_sql("SELECT * FROM marginalia")
        assert len(rows) == 1
        assert rows[0]["id"] == "src-3"
        # FTS cleaned for deleted notes
        fts_rows = db.execute_sql(
            "SELECT id FROM marginalia_fts WHERE marginalia_fts MATCH 'alpha OR beta'"
        )
        assert len(fts_rows) == 0
        # FTS still has the surviving note
        fts_rows = db.execute_sql(
            "SELECT id FROM marginalia_fts WHERE marginalia_fts MATCH 'gamma'"
        )
        assert len(fts_rows) == 1
