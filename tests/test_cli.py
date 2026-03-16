"""Tests for hugo_memex.cli."""
import json
import subprocess
import sys

import pytest

from hugo_memex.db import Database
from hugo_memex.indexer import index_content


def _run_cli(*args, env=None):
    """Run hugo-memex CLI and return result."""
    import os
    cmd_env = os.environ.copy()
    if env:
        cmd_env.update(env)
    result = subprocess.run(
        [sys.executable, "-m", "hugo_memex"] + list(args),
        capture_output=True, text=True, env=cmd_env,
    )
    return result


class TestCLIHelp:
    def test_help(self):
        result = _run_cli("--help")
        assert result.returncode == 0
        assert "hugo-memex" in result.stdout

    def test_version(self):
        result = _run_cli("--version")
        assert result.returncode == 0
        assert "0.1.0" in result.stdout

    def test_no_command_shows_help(self):
        result = _run_cli()
        assert result.returncode == 0
        assert "hugo-memex" in result.stdout


class TestCLIIndex:
    def test_index_fixtures(self, hugo_root, tmp_path):
        db_path = str(tmp_path / "test.db")
        result = _run_cli(
            "index",
            env={
                "HUGO_MEMEX_HUGO_ROOT": str(hugo_root),
                "HUGO_MEMEX_DATABASE_PATH": db_path,
            },
        )
        assert result.returncode == 0
        assert "Indexed:" in result.stdout

        # Verify DB was created with content
        db = Database(db_path, readonly=True)
        pages = db.execute_sql("SELECT COUNT(*) as n FROM pages")
        assert pages[0]["n"] >= 4
        db.close()

    def test_index_force(self, hugo_root, tmp_path):
        db_path = str(tmp_path / "test.db")
        env = {
            "HUGO_MEMEX_HUGO_ROOT": str(hugo_root),
            "HUGO_MEMEX_DATABASE_PATH": db_path,
        }
        # First index
        _run_cli("index", env=env)
        # Force re-index
        result = _run_cli("index", "--force", env=env)
        assert result.returncode == 0
        assert "Indexed:" in result.stdout

    def test_index_no_config(self, tmp_path, monkeypatch):
        # Point to nonexistent config to prevent default config from loading
        result = _run_cli(
            "index",
            env={
                "HUGO_MEMEX_CONFIG": str(tmp_path / "nonexistent.yaml"),
                "HUGO_MEMEX_DATABASE_PATH": str(tmp_path / "test.db"),
            },
        )
        assert result.returncode != 0


class TestCLIStats:
    def test_stats(self, hugo_root, tmp_path):
        db_path = str(tmp_path / "test.db")
        env = {
            "HUGO_MEMEX_HUGO_ROOT": str(hugo_root),
            "HUGO_MEMEX_DATABASE_PATH": db_path,
        }
        _run_cli("index", env=env)
        result = _run_cli("stats", env=env)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["total_pages"] >= 4


class TestCLISearch:
    def test_search(self, hugo_root, tmp_path):
        db_path = str(tmp_path / "test.db")
        env = {
            "HUGO_MEMEX_HUGO_ROOT": str(hugo_root),
            "HUGO_MEMEX_DATABASE_PATH": db_path,
        }
        _run_cli("index", env=env)
        result = _run_cli("search", "Python", env=env)
        assert result.returncode == 0
        assert "Python" in result.stdout

    def test_search_no_results(self, hugo_root, tmp_path):
        db_path = str(tmp_path / "test.db")
        env = {
            "HUGO_MEMEX_HUGO_ROOT": str(hugo_root),
            "HUGO_MEMEX_DATABASE_PATH": db_path,
        }
        _run_cli("index", env=env)
        result = _run_cli("search", "xyznonexistent", env=env)
        assert result.returncode == 0
        assert "No results" in result.stdout


class TestCLISQL:
    def test_sql_query(self, hugo_root, tmp_path):
        db_path = str(tmp_path / "test.db")
        env = {
            "HUGO_MEMEX_HUGO_ROOT": str(hugo_root),
            "HUGO_MEMEX_DATABASE_PATH": db_path,
        }
        _run_cli("index", env=env)
        result = _run_cli(
            "sql", "SELECT path, title FROM pages ORDER BY path",
            env=env,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert len(data) >= 4

    def test_sql_write_blocked(self, hugo_root, tmp_path):
        db_path = str(tmp_path / "test.db")
        env = {
            "HUGO_MEMEX_HUGO_ROOT": str(hugo_root),
            "HUGO_MEMEX_DATABASE_PATH": db_path,
        }
        _run_cli("index", env=env)
        result = _run_cli("sql", "DELETE FROM pages", env=env)
        assert result.returncode != 0
