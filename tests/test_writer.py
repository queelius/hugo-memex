"""Tests for hugo_memex.writer."""
import json
from pathlib import Path

import pytest

from hugo_memex.db import Database
from hugo_memex.indexer import index_content
from hugo_memex.parser import parse_content
from hugo_memex.writer import (
    _marginalia_id,
    add_marginalia,
    create_page,
    get_front_matter_template,
    marginalia_path_for_page,
    page_path_for_marginalia,
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
    # Remove fixture marginalia so writer tests start from a clean slate
    marginalia_dir = site / "data" / "marginalia"
    if marginalia_dir.exists():
        shutil.rmtree(marginalia_dir)
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


class TestMarginaliaPathMapping:
    """Tests for marginalia_path_for_page and page_path_for_marginalia."""

    def test_leaf_bundle(self, writable_site):
        site, _ = writable_site
        result = marginalia_path_for_page(str(site), "post/test-post/index.md")
        expected = Path(str(site)) / "data" / "marginalia" / "post" / "test-post.yaml"
        assert result == expected

    def test_standalone_file(self, writable_site):
        site, _ = writable_site
        result = marginalia_path_for_page(str(site), "media/test-book.md")
        expected = Path(str(site)) / "data" / "marginalia" / "media" / "test-book.yaml"
        assert result == expected

    def test_root_index(self, writable_site):
        site, _ = writable_site
        result = marginalia_path_for_page(str(site), "_index.md")
        expected = Path(str(site)) / "data" / "marginalia" / "_index.yaml"
        assert result == expected

    def test_reverse_mapping_leaf_bundle(self):
        result = page_path_for_marginalia("post/test-post.yaml")
        assert result == "post/test-post/index.md"

    def test_reverse_mapping_standalone(self):
        # Default assumption: non-index yaml maps to leaf bundle
        result = page_path_for_marginalia("media/test-book.yaml")
        assert result == "media/test-book/index.md"

    def test_reverse_mapping_root_index(self):
        result = page_path_for_marginalia("_index.yaml")
        assert result == "_index.md"

    def test_path_traversal_rejected(self, writable_site):
        site, _ = writable_site
        with pytest.raises(ValueError):
            marginalia_path_for_page(str(site), "../../etc/passwd")


class TestAddMarginalia:
    """Tests for add_marginalia disk writer."""

    def test_add_first_note(self, writable_site):
        site, _ = writable_site
        result = add_marginalia(str(site), "post/test-post/index.md", "My first note")
        assert result["status"] == "created"
        assert result["page_path"] == "post/test-post/index.md"
        assert "id" in result
        assert result["id"].startswith("mg-")

        # Verify file was created with correct content
        yaml_path = Path(result["source_file"])
        full_path = Path(str(site)) / yaml_path
        assert full_path.exists()

        import yaml as _yaml
        notes = _yaml.safe_load(full_path.read_text(encoding="utf-8"))
        assert len(notes) == 1
        assert notes[0]["body"] == "My first note"
        assert notes[0]["id"] == result["id"]
        assert "created" in notes[0]

    def test_add_second_note_appends(self, writable_site):
        site, _ = writable_site
        add_marginalia(str(site), "post/test-post/index.md", "Note one")
        add_marginalia(str(site), "post/test-post/index.md", "Note two")

        yaml_path = (
            Path(str(site)) / "data" / "marginalia" / "post" / "test-post.yaml"
        )
        import yaml as _yaml
        notes = _yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert len(notes) == 2
        assert notes[0]["body"] == "Note one"
        assert notes[1]["body"] == "Note two"

    def test_add_creates_directories(self, writable_site):
        site, _ = writable_site
        # The data/marginalia/post/ directory should not exist yet
        marginalia_dir = Path(str(site)) / "data" / "marginalia" / "post"
        assert not marginalia_dir.exists()

        add_marginalia(str(site), "post/test-post/index.md", "A note")
        assert marginalia_dir.exists()

    def test_add_path_traversal_rejected(self, writable_site):
        site, _ = writable_site
        with pytest.raises(ValueError):
            add_marginalia(str(site), "../../etc/passwd", "Evil note")

    def test_deterministic_ids(self):
        """_marginalia_id produces deterministic, well-formed IDs."""
        id1 = _marginalia_id("post/test/index.md", "body text", "2024-01-01T00:00:00Z")
        id2 = _marginalia_id("post/test/index.md", "body text", "2024-01-01T00:00:00Z")
        assert id1 == id2
        assert id1.startswith("mg-")
        assert len(id1) == 15  # "mg-" (3) + 12 hex chars

        # Different inputs produce different IDs
        id3 = _marginalia_id("post/other/index.md", "body text", "2024-01-01T00:00:00Z")
        assert id3 != id1


class TestArchiveMarginaliaOnDisk:
    def test_archive_adds_archived_at_field(self, writable_site):
        site, _ = writable_site
        from hugo_memex.writer import add_marginalia, archive_marginalia_on_disk
        r = add_marginalia(str(site), "post/test-post/index.md", "to archive")
        result = archive_marginalia_on_disk(
            str(site), r["source_file"], r["id"], "2026-04-18T12:00:00Z",
        )
        assert result["status"] == "archived"
        assert result["archived_at"] == "2026-04-18T12:00:00Z"

        import yaml as _yaml
        notes = _yaml.safe_load(
            (Path(site) / r["source_file"]).read_text()
        )
        target = next(n for n in notes if n["id"] == r["id"])
        assert target["archived_at"] == "2026-04-18T12:00:00Z"

    def test_archive_already_archived_is_noop(self, writable_site):
        site, _ = writable_site
        from hugo_memex.writer import add_marginalia, archive_marginalia_on_disk
        r = add_marginalia(str(site), "post/test-post/index.md", "double-archive")
        archive_marginalia_on_disk(
            str(site), r["source_file"], r["id"], "2026-04-18T12:00:00Z",
        )
        result = archive_marginalia_on_disk(
            str(site), r["source_file"], r["id"], "2026-05-01T00:00:00Z",
        )
        assert result["status"] == "already_archived"
        import yaml as _yaml
        notes = _yaml.safe_load(
            (Path(site) / r["source_file"]).read_text()
        )
        target = next(n for n in notes if n["id"] == r["id"])
        assert target["archived_at"] == "2026-04-18T12:00:00Z"

    def test_archive_not_found_raises(self, writable_site):
        site, _ = writable_site
        from hugo_memex.writer import add_marginalia, archive_marginalia_on_disk
        r = add_marginalia(str(site), "post/test-post/index.md", "x")
        with pytest.raises(ValueError, match="not found"):
            archive_marginalia_on_disk(
                str(site), r["source_file"], "mg-missing", "2026-04-18T12:00:00Z",
            )


class TestRestoreMarginaliaOnDisk:
    def test_restore_removes_archived_at(self, writable_site):
        site, _ = writable_site
        from hugo_memex.writer import (
            add_marginalia, archive_marginalia_on_disk, restore_marginalia_on_disk,
        )
        r = add_marginalia(str(site), "post/test-post/index.md", "to restore")
        archive_marginalia_on_disk(
            str(site), r["source_file"], r["id"], "2026-04-18T12:00:00Z",
        )
        result = restore_marginalia_on_disk(str(site), r["source_file"], r["id"])
        assert result["status"] == "restored"
        import yaml as _yaml
        notes = _yaml.safe_load(
            (Path(site) / r["source_file"]).read_text()
        )
        target = next(n for n in notes if n["id"] == r["id"])
        assert "archived_at" not in target

    def test_restore_already_active_is_noop(self, writable_site):
        site, _ = writable_site
        from hugo_memex.writer import add_marginalia, restore_marginalia_on_disk
        r = add_marginalia(str(site), "post/test-post/index.md", "already-active")
        result = restore_marginalia_on_disk(str(site), r["source_file"], r["id"])
        assert result["status"] == "already_active"


class TestPurgeMarginaliaFromDisk:
    def test_purge_removes_note_from_yaml(self, writable_site):
        site, _ = writable_site
        from hugo_memex.writer import add_marginalia, purge_marginalia_from_disk
        r1 = add_marginalia(str(site), "post/test-post/index.md", "keep")
        r2 = add_marginalia(str(site), "post/test-post/index.md", "purge")
        result = purge_marginalia_from_disk(str(site), r2["source_file"], r2["id"])
        assert result["status"] == "purged"
        import yaml as _yaml
        notes = _yaml.safe_load(
            (Path(site) / r1["source_file"]).read_text()
        )
        ids = [n["id"] for n in notes]
        assert r1["id"] in ids
        assert r2["id"] not in ids

    def test_purge_last_note_removes_file(self, writable_site):
        site, _ = writable_site
        from hugo_memex.writer import add_marginalia, purge_marginalia_from_disk
        r = add_marginalia(str(site), "post/test-post/index.md", "only")
        purge_marginalia_from_disk(str(site), r["source_file"], r["id"])
        assert not (Path(site) / r["source_file"]).exists()

    def test_purge_not_found_raises(self, writable_site):
        site, _ = writable_site
        from hugo_memex.writer import add_marginalia, purge_marginalia_from_disk
        r = add_marginalia(str(site), "post/test-post/index.md", "x")
        with pytest.raises(ValueError, match="not found"):
            purge_marginalia_from_disk(str(site), r["source_file"], "mg-missing")
