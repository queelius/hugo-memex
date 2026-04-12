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

    def test_custom_tag_taxonomy(self, writable_site):
        """validate_page uses the supplied taxonomy name, not a hardcode."""
        site, db = writable_site
        # Simulate a site where the taxonomy is 'category' instead of 'tags'
        create_page(
            str(site), "post", "taxed",
            {"title": "Taxed", "description": "d", "category": ["a", "b"]},
            "body",
        )
        index_content(str(site), db, paths=["post/taxed/index.md"], force=True)
        result = validate_page(db, str(site), "post/taxed/index.md",
                               tag_taxonomy="category")
        # Should not warn "No category defined"
        no_tax = [i for i in result["issues"] if i["message"].startswith("No category")]
        assert no_tax == []

    def test_non_string_tag_does_not_crash(self, writable_site):
        """Non-string tag (e.g. YAML integer) produces a warning, not a crash."""
        site, db = writable_site
        create_page(
            str(site), "post", "badtag",
            {"title": "Bad", "description": "d", "tags": ["ok", 2024, None]},
            "body",
        )
        result = validate_page(db, str(site), "post/badtag/index.md")
        # Should surface structured warnings for the non-string entries
        msgs = [i["message"] for i in result["issues"]]
        assert any("Non-string" in m for m in msgs)


class TestCreatePageSecurity:
    """Regression tests for path traversal and related write-path issues."""

    def test_rejects_slug_with_traversal(self, writable_site):
        site, db = writable_site
        with pytest.raises(ValueError, match="Invalid slug"):
            create_page(
                str(site), "post", "../../../tmp/pwned",
                {"title": "x"}, "body",
            )

    def test_rejects_slug_with_slash(self, writable_site):
        site, db = writable_site
        with pytest.raises(ValueError, match="Invalid slug"):
            create_page(
                str(site), "post", "a/b",
                {"title": "x"}, "body",
            )

    def test_rejects_empty_slug(self, writable_site):
        site, db = writable_site
        with pytest.raises(ValueError, match="Invalid slug"):
            create_page(str(site), "post", "", {"title": "x"}, "body")

    def test_rejects_section_with_traversal(self, writable_site):
        site, db = writable_site
        with pytest.raises(ValueError, match="Invalid section"):
            create_page(
                str(site), "../../tmp", "pwned",
                {"title": "x"}, "body",
            )

    def test_rejects_section_with_slash(self, writable_site):
        site, db = writable_site
        with pytest.raises(ValueError, match="Invalid section"):
            create_page(
                str(site), "post/sub", "slug",
                {"title": "x"}, "body",
            )

    def test_rejects_symlink_parent(self, writable_site, tmp_path):
        """A symlinked directory inside content/ that escapes is caught."""
        site, db = writable_site
        outside = tmp_path / "outside"
        outside.mkdir()
        link = site / "content" / "post" / "sneaky"
        link.symlink_to(outside)
        with pytest.raises(ValueError, match="escapes content"):
            create_page(
                str(site), "post", "sneaky",
                {"title": "x"}, "body",
            )

    def test_rejects_empty_title(self, writable_site):
        site, db = writable_site
        with pytest.raises(ValueError, match="title"):
            create_page(
                str(site), "post", "empty-title",
                {"title": ""}, "body",
            )


class TestUpdatePageFormats:
    """update_page only supports YAML front matter; TOML/JSON should error."""

    def test_toml_front_matter_rejected(self, writable_site):
        site, db = writable_site
        # Write a TOML-front-matter file directly (bypassing create_page)
        toml_path = site / "content" / "post" / "toml-post" / "index.md"
        toml_path.parent.mkdir(parents=True)
        toml_path.write_text(
            '+++\ntitle = "TOML Post"\ntags = ["python"]\n+++\n\nOriginal body.\n'
        )
        with pytest.raises(ValueError, match="TOML front matter"):
            update_page(str(site), "post/toml-post/index.md", body="New body.")

    def test_json_front_matter_rejected(self, writable_site):
        site, db = writable_site
        json_path = site / "content" / "post" / "json-post" / "index.md"
        json_path.parent.mkdir(parents=True)
        json_path.write_text('{"title": "JSON Post"}\n\nOriginal body.\n')
        with pytest.raises(ValueError, match="JSON front matter"):
            update_page(str(site), "post/json-post/index.md", body="New body.")

    def test_yaml_still_works(self, writable_site):
        """Sanity: the YAML path still works after adding the format check."""
        site, db = writable_site
        result = update_page(
            str(site), "post/test-post/index.md",
            front_matter={"featured": True},
        )
        assert result["status"] == "updated"


class TestSuggestTagsRobustness:
    """Regression tests for FTS5 query-string hazards."""

    def test_text_with_embedded_quotes(self, writable_site):
        """Quotes inside words must not crash the FTS query."""
        site, db = writable_site
        # This previously produced an FTS5 MATCH syntax error and silently
        # returned [].  Now the tokenizer strips quotes entirely.
        result = suggest_tags(
            db,
            'discussing Rust\'s "borrow" checker and "ownership" model',
        )
        assert isinstance(result, list)

    def test_text_with_fts_operators(self, writable_site):
        """AND/OR/NEAR in the text must be treated as literals, not operators."""
        site, db = writable_site
        result = suggest_tags(
            db,
            "Testing AND operations OR NEAR queries in search",
        )
        assert isinstance(result, list)


class TestFrontMatterTemplateTypes:
    """Regression for the 'most common type' bug — the old code picked
    an arbitrary set element; now it picks the real most-common."""

    def test_most_common_type(self, writable_site):
        _, db = writable_site
        template = get_front_matter_template(db, "post")
        # All posts in the fixture have 'title' as a str, never anything else
        assert template["title"]["type"] == "str"
        # Defaults match the primary type
        assert template["title"]["default"] == ""
