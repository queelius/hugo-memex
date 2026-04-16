"""Tests for hugo_memex.indexer."""
from pathlib import Path

import pytest

from hugo_memex.db import Database
from hugo_memex.indexer import (
    classify_page,
    discover_content,
    extract_page_record,
    extract_taxonomies,
    index_content,
)
from hugo_memex.writer import page_path_for_marginalia


class TestDiscoverContent:
    def test_discovers_fixture_files(self, content_dir):
        files = discover_content(content_dir)
        names = [f.name for f in files]
        assert "index.md" in names  # post/test-post/index.md
        assert "_index.md" in names  # root _index.md
        assert "test-book.md" in names  # standalone

    def test_nonexistent_dir(self, tmp_path):
        files = discover_content(tmp_path / "nonexistent")
        assert files == []


class TestClassifyPage:
    def test_root_index(self):
        section, kind, bundle = classify_page("_index.md", "_index.md")
        assert section == ""
        assert kind == "section"
        assert bundle == "branch"

    def test_section_index(self):
        section, kind, bundle = classify_page("post/_index.md", "_index.md")
        assert section == "post"
        assert kind == "section"
        assert bundle == "branch"

    def test_leaf_bundle(self):
        section, kind, bundle = classify_page("post/my-post/index.md", "index.md")
        assert section == "post"
        assert kind == "page"
        assert bundle == "leaf"

    def test_standalone_page(self):
        section, kind, bundle = classify_page("media/test-book.md", "test-book.md")
        assert section == "media"
        assert kind == "page"
        assert bundle is None


class TestExtractPageRecord:
    def test_basic_extraction(self):
        fm = {
            "title": "My Post",
            "date": "2024-01-15",
            "draft": False,
            "description": "A post",
            "slug": "my-post",
        }
        body = "Hello world. This is a test."
        record = extract_page_record("post/my-post/index.md", fm, body, "hash123")
        assert record["title"] == "My Post"
        assert record["section"] == "post"
        assert record["kind"] == "page"
        assert record["bundle_type"] == "leaf"
        assert record["draft"] is False
        assert record["word_count"] == 6

    def test_missing_title_uses_stem(self):
        record = extract_page_record("media/review.md", {}, "body", "hash")
        assert record["title"] == "review"


class TestExtractTaxonomies:
    def test_extracts_defined_taxonomies(self):
        fm = {"tags": ["python", "go"], "categories": ["programming"], "custom": "ignored"}
        defs = {"tags": "tag", "categories": "category"}
        result = extract_taxonomies(fm, defs)
        assert result["tags"] == ["python", "go"]
        assert result["categories"] == ["programming"]
        assert "custom" not in result

    def test_missing_taxonomy_skipped(self):
        fm = {"tags": ["python"]}
        defs = {"tags": "tag", "series": "series"}
        result = extract_taxonomies(fm, defs)
        assert "tags" in result
        assert "series" not in result

    def test_empty_list_skipped(self):
        fm = {"tags": []}
        defs = {"tags": "tag"}
        result = extract_taxonomies(fm, defs)
        assert "tags" not in result


class TestIndexContent:
    def test_index_fixture_content(self, hugo_root, db):
        stats = index_content(str(hugo_root), db)
        assert stats["indexed"] >= 4
        assert stats["errors"] == []

        # Verify pages were inserted
        pages = db.execute_sql("SELECT path, title, section, kind FROM pages ORDER BY path")
        assert len(pages) >= 4
        paths = {p["path"] for p in pages}
        assert "post/test-post/index.md" in paths
        assert "projects/test-project/index.md" in paths
        assert "media/test-book.md" in paths

    def test_taxonomies_indexed(self, hugo_root, db):
        index_content(str(hugo_root), db)
        tags = db.execute_sql(
            "SELECT DISTINCT term FROM taxonomies WHERE taxonomy = 'tags'"
        )
        terms = {r["term"] for r in tags}
        assert "python" in terms
        assert "sqlite" in terms

    def test_incremental_skip(self, hugo_root, db):
        stats1 = index_content(str(hugo_root), db)
        assert stats1["indexed"] >= 4

        stats2 = index_content(str(hugo_root), db)
        assert stats2["indexed"] == 0
        assert stats2["unchanged"] == stats1["indexed"]

    def test_force_reindex(self, hugo_root, db):
        index_content(str(hugo_root), db)
        stats = index_content(str(hugo_root), db, force=True)
        assert stats["indexed"] >= 4
        assert stats["unchanged"] == 0

    def test_specific_paths(self, hugo_root, db):
        stats = index_content(
            str(hugo_root), db,
            paths=["post/test-post/index.md"],
        )
        assert stats["indexed"] == 1
        pages = db.execute_sql("SELECT path FROM pages")
        assert len(pages) == 1

    def test_fts_populated(self, hugo_root, db):
        index_content(str(hugo_root), db)
        rows = db.execute_sql(
            "SELECT path FROM pages_fts WHERE pages_fts MATCH 'Python'"
        )
        assert any(r["path"] == "post/test-post/index.md" for r in rows)

    def test_draft_detection(self, hugo_root, db):
        index_content(str(hugo_root), db)
        drafts = db.execute_sql("SELECT path FROM pages WHERE draft = 1")
        draft_paths = {r["path"] for r in drafts}
        assert "media/test-book.md" in draft_paths

    def test_cleanup_removed_files(self, tmp_path):
        """Test that pages are removed when files are deleted from disk."""
        # Create a minimal Hugo site
        (tmp_path / "hugo.toml").write_text(
            'title = "test"\n[taxonomies]\ntag = "tags"\n'
        )
        content = tmp_path / "content"
        post_dir = content / "post" / "temp"
        post_dir.mkdir(parents=True)
        (post_dir / "index.md").write_text(
            '---\ntitle: "Temp"\n---\nBody.'
        )

        db = Database(":memory:")
        stats1 = index_content(str(tmp_path), db)
        assert stats1["indexed"] == 1

        # Delete the file
        (post_dir / "index.md").unlink()
        post_dir.rmdir()

        stats2 = index_content(str(tmp_path), db)
        assert stats2["removed"] == 1
        assert db.execute_sql("SELECT COUNT(*) as n FROM pages")[0]["n"] == 0
        db.close()

    def test_index_real_site(self):
        """Test against actual metafunctor site if available."""
        metafunctor = Path("~/github/repos/metafunctor").expanduser()
        if not (metafunctor / "hugo.toml").exists():
            pytest.skip("metafunctor not available")

        db = Database(":memory:")
        stats = index_content(str(metafunctor), db)
        assert stats["indexed"] > 10
        assert stats["errors"] == []

        # Verify various content types
        sections = db.execute_sql(
            "SELECT section, COUNT(*) as n FROM pages "
            "GROUP BY section ORDER BY n DESC"
        )
        section_names = {r["section"] for r in sections}
        assert "post" in section_names
        assert "projects" in section_names

        # Verify taxonomies
        tax_stats = db.execute_sql(
            "SELECT taxonomy, COUNT(DISTINCT term) as terms "
            "FROM taxonomies GROUP BY taxonomy"
        )
        assert len(tax_stats) > 0

        db.close()


class TestMarginaliaIndexing:
    def test_index_discovers_marginalia(self, hugo_root, db):
        """index_content discovers and indexes marginalia YAML files."""
        stats = index_content(str(hugo_root), db)
        assert stats["marginalia_indexed"] >= 2

    def test_indexed_marginalia_queryable(self, hugo_root, db):
        """Indexed marginalia are queryable via db.get_marginalia."""
        index_content(str(hugo_root), db)
        notes = db.get_marginalia("post/test-post/index.md")
        ids = {n["id"] for n in notes}
        assert "mg-fixture00001" in ids
        assert "mg-fixture00002" in ids

    def test_marginalia_fts_populated(self, hugo_root, db):
        """Marginalia body text is indexed in FTS5."""
        index_content(str(hugo_root), db)
        rows = db.execute_sql(
            "SELECT id FROM marginalia_fts WHERE marginalia_fts MATCH 'Python'"
        )
        ids = {r["id"] for r in rows}
        assert "mg-fixture00001" in ids

    def test_marginalia_incremental(self, hugo_root, db):
        """Second index run skips unchanged marginalia files."""
        stats1 = index_content(str(hugo_root), db)
        assert stats1["marginalia_indexed"] >= 2

        stats2 = index_content(str(hugo_root), db)
        assert stats2["marginalia_indexed"] == 0
        assert stats2["marginalia_unchanged"] >= 1

    def test_marginalia_force_reindex(self, hugo_root, db):
        """Force reindex re-processes all marginalia files."""
        index_content(str(hugo_root), db)
        stats = index_content(str(hugo_root), db, force=True)
        assert stats["marginalia_indexed"] >= 2

    def test_marginalia_orphan_survives_page_delete(self, hugo_root, db):
        """Marginalia survive when their associated page is deleted."""
        index_content(str(hugo_root), db)
        # Verify marginalia exist
        notes_before = db.get_marginalia("post/test-post/index.md")
        assert len(notes_before) >= 2

        # Delete the page
        db.delete_page("post/test-post/index.md")

        # Marginalia should still be there (orphan survival)
        notes_after = db.get_marginalia("post/test-post/index.md")
        assert len(notes_after) == len(notes_before)
