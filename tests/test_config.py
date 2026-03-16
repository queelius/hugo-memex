"""Tests for hugo_memex.config."""
import os
from pathlib import Path

import pytest

from hugo_memex.config import load_config, load_hugo_config, get_taxonomies


class TestLoadConfig:
    def test_defaults(self, tmp_path, monkeypatch):
        # Clear env vars
        monkeypatch.delenv("HUGO_MEMEX_CONFIG", raising=False)
        monkeypatch.delenv("HUGO_MEMEX_HUGO_ROOT", raising=False)
        monkeypatch.delenv("HUGO_MEMEX_DATABASE_PATH", raising=False)
        config = load_config(str(tmp_path / "nonexistent.yaml"))
        assert config["hugo_root"] is None
        assert "hugo.db" in config["database_path"]

    def test_yaml_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HUGO_MEMEX_HUGO_ROOT", raising=False)
        monkeypatch.delenv("HUGO_MEMEX_DATABASE_PATH", raising=False)
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "hugo_root: /path/to/hugo\n"
            "database_path: /path/to/db.sqlite\n"
        )
        config = load_config(str(config_file))
        assert config["hugo_root"] == "/path/to/hugo"
        assert config["database_path"] == "/path/to/db.sqlite"

    def test_env_var_overrides(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("hugo_root: /yaml/path\n")
        monkeypatch.setenv("HUGO_MEMEX_HUGO_ROOT", "/env/path")
        config = load_config(str(config_file))
        assert config["hugo_root"] == "/env/path"

    def test_env_var_config_path(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("hugo_root: /from/env/config\n")
        monkeypatch.setenv("HUGO_MEMEX_CONFIG", str(config_file))
        monkeypatch.delenv("HUGO_MEMEX_HUGO_ROOT", raising=False)
        config = load_config()
        assert config["hugo_root"] == "/from/env/config"

    def test_tilde_expansion(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HUGO_MEMEX_HUGO_ROOT", raising=False)
        monkeypatch.delenv("HUGO_MEMEX_DATABASE_PATH", raising=False)
        config_file = tmp_path / "config.yaml"
        config_file.write_text("hugo_root: ~/my-site\n")
        config = load_config(str(config_file))
        assert "~" not in config["hugo_root"]
        assert config["hugo_root"].endswith("my-site")


class TestHugoConfig:
    def test_load_hugo_config(self, fixtures_dir):
        config = load_hugo_config(str(fixtures_dir))
        assert config["title"] == "Test Site"
        assert "taxonomies" in config

    def test_load_hugo_config_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_hugo_config(str(tmp_path))

    def test_get_taxonomies(self, fixtures_dir):
        config = load_hugo_config(str(fixtures_dir))
        taxonomies = get_taxonomies(config)
        # Our fixture has: tag="tags", category="categories", series="series"
        assert taxonomies["tags"] == "tag"
        assert taxonomies["categories"] == "category"
        assert taxonomies["series"] == "series"

    def test_get_taxonomies_from_real_site(self):
        """Test against actual metafunctor hugo.toml if available."""
        metafunctor = Path("~/github/repos/metafunctor").expanduser()
        if not (metafunctor / "hugo.toml").exists():
            pytest.skip("metafunctor not available")
        config = load_hugo_config(str(metafunctor))
        taxonomies = get_taxonomies(config)
        assert "tags" in taxonomies
        assert "categories" in taxonomies
        assert "genres" in taxonomies
        assert "series" in taxonomies
        assert "linked-projects" in taxonomies

    def test_get_taxonomies_empty(self):
        taxonomies = get_taxonomies({})
        assert taxonomies == {}
