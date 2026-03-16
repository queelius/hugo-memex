"""Shared fixtures for hugo-memex tests."""
from pathlib import Path

import pytest

from hugo_memex.db import Database

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir():
    return FIXTURES_DIR


@pytest.fixture
def hugo_root(fixtures_dir):
    return fixtures_dir


@pytest.fixture
def content_dir(fixtures_dir):
    return fixtures_dir / "content"


@pytest.fixture
def db():
    """In-memory database for testing."""
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def sample_page():
    """A sample page dict ready for db.save_page()."""
    return {
        "path": "post/test-post/index.md",
        "slug": "test-post-python",
        "title": "Test Post About Python",
        "section": "post",
        "kind": "page",
        "bundle_type": "leaf",
        "date": "2024-06-15T10:00:00Z",
        "draft": False,
        "description": "A test post about Python programming and SQLite databases",
        "word_count": 42,
        "body": "This is a test post about Python programming.\n\n"
                "## SQLite Integration\n\n"
                "SQLite is a great embedded database.",
        "front_matter": {
            "title": "Test Post About Python",
            "date": "2024-06-15T10:00:00Z",
            "tags": ["python", "sqlite"],
            "categories": ["programming"],
        },
        "content_hash": "abc123",
        "indexed_at": "2024-06-15T12:00:00Z",
    }


@pytest.fixture
def sample_taxonomies():
    """Sample taxonomies for the sample_page."""
    return {
        "tags": ["python", "sqlite"],
        "categories": ["programming"],
        "series": ["tutorials"],
    }
