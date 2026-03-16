"""Configuration for hugo-memex.

Loads from YAML config file with env var overrides.
Also parses Hugo site config (hugo.toml) for taxonomy discovery.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path("~/.config/hugo-memex/config.yaml").expanduser()


def load_config(config_path: str | None = None) -> dict[str, Any]:
    """Load hugo-memex configuration.

    Priority: env vars > YAML file > defaults.

    Config keys:
        hugo_root: Path to Hugo site root (contains hugo.toml + content/)
        database_path: Path to SQLite database file
    """
    config: dict[str, Any] = {
        "hugo_root": None,
        "database_path": str(
            Path("~/.config/hugo-memex/hugo.db").expanduser()
        ),
    }

    # Determine config file path
    path = config_path or os.environ.get("HUGO_MEMEX_CONFIG")
    if path is None and DEFAULT_CONFIG_PATH.exists():
        path = str(DEFAULT_CONFIG_PATH)

    # Load YAML config
    if path and Path(path).exists():
        with open(path) as f:
            loaded = yaml.safe_load(f) or {}
        config.update(loaded)

    # Env var overrides
    if env_root := os.environ.get("HUGO_MEMEX_HUGO_ROOT"):
        config["hugo_root"] = env_root
    if env_db := os.environ.get("HUGO_MEMEX_DATABASE_PATH"):
        config["database_path"] = env_db

    # Expand ~ in paths
    if config["hugo_root"]:
        config["hugo_root"] = str(Path(config["hugo_root"]).expanduser())
    config["database_path"] = str(
        Path(config["database_path"]).expanduser()
    )

    return config


def load_hugo_config(hugo_root: str) -> dict[str, Any]:
    """Parse hugo.toml and return the full config as a dict."""
    config_path = Path(hugo_root) / "hugo.toml"
    if not config_path.exists():
        raise FileNotFoundError(f"Hugo config not found: {config_path}")
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def get_taxonomies(hugo_config: dict[str, Any]) -> dict[str, str]:
    """Extract taxonomy definitions from Hugo config.

    Returns a dict mapping plural form (used in front matter) to
    singular form (Hugo's internal key).

    Example from hugo.toml:
        [taxonomies]
        tag = "tags"           → {"tags": "tag"}
        category = "categories" → {"categories": "category"}
    """
    raw = hugo_config.get("taxonomies", {})
    # Hugo format: singular_key = "plural_value"
    # We want: {plural_value: singular_key}
    return {plural: singular for singular, plural in raw.items()}
