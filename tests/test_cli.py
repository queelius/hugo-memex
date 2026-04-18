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


class TestPurgeCLI:
    def _setup_site(self, tmp_path, fixtures_dir, config_path):
        import shutil
        site = tmp_path / "site"
        shutil.copytree(fixtures_dir, site)
        db_path = tmp_path / "hugo.db"
        config_path.write_text(
            f"hugo_root: {site}\ndatabase_path: {db_path}\n"
        )
        return site, db_path

    def _run_cli(self, config_path, *args):
        return subprocess.run(
            [sys.executable, "-m", "hugo_memex", "--config", str(config_path)]
            + list(args),
            capture_output=True,
            text=True,
        )

    def test_purge_requires_filter(self, tmp_path, fixtures_dir):
        config_path = tmp_path / "config.yaml"
        self._setup_site(tmp_path, fixtures_dir, config_path)
        result = self._run_cli(config_path, "purge")
        assert result.returncode != 0
        combined = (result.stderr + result.stdout).lower()
        assert "filter" in combined or "missing" in combined

    def test_purge_missing_purges_archived_missing_pages(self, tmp_path, fixtures_dir):
        config_path = tmp_path / "config.yaml"
        site, db_path = self._setup_site(tmp_path, fixtures_dir, config_path)
        self._run_cli(config_path, "index")
        (site / "content" / "post" / "test-post" / "index.md").unlink()
        (site / "content" / "post" / "test-post").rmdir()
        self._run_cli(config_path, "index")
        from hugo_memex.db import Database
        db = Database(str(db_path))
        rows = db.execute_sql(
            "SELECT archived_at FROM pages WHERE path = ?",
            ("post/test-post/index.md",),
        )
        assert rows and rows[0]["archived_at"] is not None
        db.close()
        result = self._run_cli(config_path, "purge", "--missing")
        assert result.returncode == 0
        db = Database(str(db_path))
        rows = db.execute_sql(
            "SELECT 1 FROM pages WHERE path = ?",
            ("post/test-post/index.md",),
        )
        assert rows == []
        db.close()

    def test_purge_archived_before_filters_by_date(self, tmp_path, fixtures_dir):
        config_path = tmp_path / "config.yaml"
        site, db_path = self._setup_site(tmp_path, fixtures_dir, config_path)
        self._run_cli(config_path, "index")
        from hugo_memex.db import Database
        db = Database(str(db_path))
        db.conn.execute(
            "INSERT INTO pages (path, title, section, kind, front_matter, content_hash, indexed_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("post/old/index.md", "Old", "post", "page", "{}", "h",
             "2025-01-01T00:00:00Z", "2025-06-01T00:00:00Z"),
        )
        db.conn.commit()
        db.close()
        result = self._run_cli(
            config_path, "purge", "--archived-before", "2026-01-01",
        )
        assert result.returncode == 0
        db = Database(str(db_path))
        rows = db.execute_sql(
            "SELECT 1 FROM pages WHERE path = 'post/old/index.md'"
        )
        assert rows == []
        db.close()

    def test_purge_dry_run_does_not_purge(self, tmp_path, fixtures_dir):
        config_path = tmp_path / "config.yaml"
        site, db_path = self._setup_site(tmp_path, fixtures_dir, config_path)
        self._run_cli(config_path, "index")
        (site / "content" / "post" / "test-post" / "index.md").unlink()
        (site / "content" / "post" / "test-post").rmdir()
        self._run_cli(config_path, "index")
        result = self._run_cli(
            config_path, "purge", "--missing", "--dry-run",
        )
        assert result.returncode == 0
        from hugo_memex.db import Database
        db = Database(str(db_path))
        rows = db.execute_sql(
            "SELECT archived_at FROM pages WHERE path = ?",
            ("post/test-post/index.md",),
        )
        assert rows and rows[0]["archived_at"] is not None
        db.close()

    def test_purge_archived_missing_marginalia(self, tmp_path, fixtures_dir):
        config_path = tmp_path / "config.yaml"
        site, db_path = self._setup_site(tmp_path, fixtures_dir, config_path)
        self._run_cli(config_path, "index")
        yaml_file = site / "data" / "marginalia" / "post" / "test-post.yaml"
        yaml_file.unlink()
        self._run_cli(config_path, "index")
        result = self._run_cli(config_path, "purge", "--missing")
        assert result.returncode == 0
        from hugo_memex.db import Database
        db = Database(str(db_path))
        rows = db.execute_sql(
            "SELECT 1 FROM marginalia WHERE source_file = ?",
            ("data/marginalia/post/test-post.yaml",),
        )
        assert rows == []
        db.close()
