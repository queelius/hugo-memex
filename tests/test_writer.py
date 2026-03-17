"""Tests for hugo_memex.writer."""
import json
from pathlib import Path

import pytest

from hugo_memex.db import Database
from hugo_memex.indexer import index_content
from hugo_memex.parser import parse_content
from hugo_memex.writer import (
    create_page,
    get_front_matter_template,
    suggest_tags,
    update_page,
    validate_page,
)


@pytest.fixture
def writable_site(tmp_path, fixtures_dir):
    """Create a writable copy of the fixture Hugo site + populated DB."""
    import shutil
    site = tmp_path / "site"
    shutil.copytree(fixtures_dir, site)
    db = Database(":memory:")
    stats = index_content(str(site), db)
    assert stats["errors"] == []
    return site, db


class TestCreatePage:
    def test_create_leaf_bundle(self, writable_site):
        site, db = writable_site
        result = create_page(
            str(site), "post", "my-new-post",
            {"title": "My New Post", "tags": ["python"]},
            "Hello world!",
        )
        assert result["status"] == "created"
        assert result["path"] == "post/my-new-post/index.md"

        # Verify file exists and is parseable
        file_path = site / "content" / "post" / "my-new-post" / "index.md"
        assert file_path.exists()
        fm, body = parse_content(file_path.read_text())
        assert fm["title"] == "My New Post"
        assert fm["tags"] == ["python"]
        assert fm["draft"] is True
        assert "date" in fm
        assert "Hello world!" in body

    def test_create_standalone(self, writable_site):
        site, db = writable_site
        result = create_page(
            str(site), "media", "my-review",
            {"title": "Book Review"},
            "Great book.",
            bundle=False,
        )
        assert result["path"] == "media/my-review.md"
        assert (site / "content" / "media" / "my-review.md").exists()

    def test_create_with_custom_draft_false(self, writable_site):
        site, db = writable_site
        result = create_page(
            str(site), "post", "published-post",
            {"title": "Published", "draft": False},
            "Content.",
        )
        fm, _ = parse_content(
            (site / "content" / "post" / "published-post" / "index.md").read_text()
        )
        assert fm["draft"] is False

    def test_create_rejects_existing(self, writable_site):
        site, db = writable_site
        with pytest.raises(FileExistsError):
            create_page(
                str(site), "post", "test-post",
                {"title": "Duplicate"},
                "Body.",
            )

    def test_create_requires_title(self, writable_site):
        site, db = writable_site
        with pytest.raises(ValueError, match="title"):
            create_page(str(site), "post", "no-title", {}, "Body.")

    def test_create_new_section(self, writable_site):
        site, db = writable_site
        result = create_page(
            str(site), "tutorials", "first-tutorial",
            {"title": "Tutorial 1"},
            "Step 1...",
        )
        assert result["status"] == "created"
        assert (site / "content" / "tutorials" / "first-tutorial" / "index.md").exists()


class TestUpdatePage:
    def test_update_front_matter(self, writable_site):
        site, db = writable_site
        result = update_page(
            str(site), "post/test-post/index.md",
            front_matter={"featured": True, "description": "Updated description"},
        )
        assert result["status"] == "updated"
        assert "front_matter.featured" in result["changes"]

        fm, _ = parse_content(
            (site / "content" / "post" / "test-post" / "index.md").read_text()
        )
        assert fm["featured"] is True
        assert fm["description"] == "Updated description"
        # Original fields preserved
        assert fm["title"] == "Test Post About Python"
        assert fm["tags"] == ["python", "sqlite"]

    def test_update_body(self, writable_site):
        site, db = writable_site
        result = update_page(
            str(site), "post/test-post/index.md",
            body="Completely new body content.",
        )
        assert result["status"] == "updated"
        assert "body" in result["changes"]

        _, body = parse_content(
            (site / "content" / "post" / "test-post" / "index.md").read_text()
        )
        assert "Completely new body content." in body

    def test_update_both(self, writable_site):
        site, db = writable_site
        result = update_page(
            str(site), "post/test-post/index.md",
            front_matter={"draft": True},
            body="New body.",
        )
        assert len(result["changes"]) >= 2

    def test_update_no_changes(self, writable_site):
        site, db = writable_site
        fm_original, body_original = parse_content(
            (site / "content" / "post" / "test-post" / "index.md").read_text()
        )
        result = update_page(
            str(site), "post/test-post/index.md",
            front_matter={"title": fm_original["title"]},
        )
        assert result["status"] == "unchanged"

    def test_update_not_found(self, writable_site):
        site, db = writable_site
        with pytest.raises(FileNotFoundError):
            update_page(str(site), "nonexistent.md")

    def test_update_path_traversal(self, writable_site):
        site, db = writable_site
        with pytest.raises(ValueError, match="within content"):
            update_page(str(site), "../../etc/passwd")


class TestGetFrontMatterTemplate:
    def test_post_template(self, writable_site):
        _, db = writable_site
        template = get_front_matter_template(db, "post")
        assert "title" in template
        assert "tags" in template
        assert "date" in template
        assert template["title"]["frequency"] > 0.5

    def test_project_template(self, writable_site):
        _, db = writable_site
        template = get_front_matter_template(db, "projects")
        assert "title" in template

    def test_empty_section(self, writable_site):
        _, db = writable_site
        template = get_front_matter_template(db, "nonexistent")
        assert "_note" in template


class TestSuggestTags:
    def test_suggest_from_python_text(self, writable_site):
        _, db = writable_site
        suggestions = suggest_tags(
            db, "Python programming with SQLite databases and full-text search",
        )
        assert len(suggestions) >= 1
        tag_names = [s["tag"] for s in suggestions]
        assert any("python" in t.lower() for t in tag_names)

    def test_suggest_returns_canonical_form(self, writable_site):
        _, db = writable_site
        suggestions = suggest_tags(db, "Rust systems programming")
        # Should return tags, each with canonical casing
        for s in suggestions:
            assert "tag" in s
            assert "relevance" in s

    def test_suggest_empty_text(self, writable_site):
        _, db = writable_site
        suggestions = suggest_tags(db, "")
        assert suggestions == []

    def test_suggest_respects_limit(self, writable_site):
        _, db = writable_site
        suggestions = suggest_tags(db, "programming algorithms data structures", limit=3)
        assert len(suggestions) <= 3


class TestValidatePage:
    def test_valid_page(self, writable_site):
        site, db = writable_site
        result = validate_page(db, str(site), "post/test-post/index.md")
        assert result["valid"] is True

    def test_missing_description(self, writable_site):
        site, db = writable_site
        # Create a page without description
        create_page(str(site), "post", "no-desc", {"title": "No Desc"}, "Body.")
        # Index it so the DB knows about it
        index_content(str(site), db, paths=["post/no-desc/index.md"], force=True)
        result = validate_page(db, str(site), "post/no-desc/index.md")
        issues = [i for i in result["issues"] if i["field"] == "description"]
        assert len(issues) >= 1

    def test_not_found(self, writable_site):
        site, db = writable_site
        result = validate_page(db, str(site), "nonexistent.md")
        assert result["valid"] is False

    def test_gpg_hash_mismatch(self, writable_site):
        site, db = writable_site
        # Create a page with a GPG hash then change the body
        create_page(
            str(site), "post", "signed",
            {"title": "Signed", "gpg_body_hash": "sha256:deadbeef"},
            "Original body.",
        )
        result = validate_page(db, str(site), "post/signed/index.md")
        hash_issues = [i for i in result["issues"] if i["field"] == "gpg_body_hash"]
        assert len(hash_issues) == 1
        assert "mismatch" in hash_issues[0]["message"]
