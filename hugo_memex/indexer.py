"""Indexer pipeline for Hugo content.

Walks content/, parses front matter, populates the SQLite database
with incremental sync support.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from hugo_memex.config import load_hugo_config, get_taxonomies
from hugo_memex.db import Database
from hugo_memex.parser import parse_content


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _word_count(text: str) -> int:
    return len(text.split())


def _normalize_date(value: Any) -> str | None:
    """Normalize various date formats to ISO 8601 string."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    s = str(value)
    return s if s else None


def _make_json_safe(obj: Any) -> Any:
    """Recursively convert non-JSON-serializable values in front matter.

    YAML parses dates as datetime.date/datetime objects, which json.dumps
    can't handle. This converts them to ISO 8601 strings.
    """
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_json_safe(v) for v in obj]
    return obj


def discover_content(content_dir: Path) -> list[Path]:
    """Walk content/ and collect all .md files."""
    if not content_dir.exists():
        return []
    return sorted(p for p in content_dir.rglob("*.md") if p.is_file())


def classify_page(rel_path: str, filename: str) -> tuple[str, str, str | None]:
    """Determine section, kind, and bundle_type from path.

    Returns (section, kind, bundle_type).
    """
    parts = Path(rel_path).parts

    # Top-level _index.md → root section
    if rel_path == "_index.md":
        return "", "section", "branch"

    # Determine section (first directory component)
    section = parts[0] if len(parts) > 1 else ""

    if filename == "_index.md":
        return section, "section", "branch"
    elif filename == "index.md":
        return section, "page", "leaf"
    else:
        # Standalone .md file
        return section, "page", None


def extract_page_record(
    rel_path: str, front_matter: dict, body: str,
    content_hash: str,
) -> dict[str, Any]:
    """Build a page record dict from parsed content."""
    filename = Path(rel_path).name
    section, kind, bundle_type = classify_page(rel_path, filename)

    title = front_matter.get("title", Path(rel_path).stem)
    date = _normalize_date(front_matter.get("date"))
    slug = front_matter.get("slug")
    draft = bool(front_matter.get("draft", False))
    description = front_matter.get("description")

    return {
        "path": rel_path,
        "slug": slug,
        "title": title,
        "section": section,
        "kind": kind,
        "bundle_type": bundle_type,
        "date": date,
        "draft": draft,
        "description": description,
        "word_count": _word_count(body),
        "body": body,
        "front_matter": front_matter,
        "content_hash": content_hash,
        "indexed_at": _now_iso(),
    }


def extract_taxonomies(
    front_matter: dict, taxonomy_defs: dict[str, str],
) -> dict[str, list[str]]:
    """Extract taxonomy terms from front matter.

    taxonomy_defs maps plural form → singular form (from hugo.toml).
    We look up the plural form in front matter (e.g., "tags": [...]).
    """
    result = {}
    for plural in taxonomy_defs:
        terms = front_matter.get(plural, [])
        if isinstance(terms, list) and terms:
            # Normalize to strings
            result[plural] = [str(t) for t in terms]
    return result


def index_content(
    hugo_root: str,
    db: Database,
    paths: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run the indexing pipeline.

    Args:
        hugo_root: Path to Hugo site root.
        db: Database instance.
        paths: Optional list of specific content paths to index (relative to content/).
        force: If True, re-index all files regardless of sync state.

    Returns:
        Stats dict: {indexed, unchanged, removed, errors}.
    """
    hugo_root_path = Path(hugo_root)
    content_dir = hugo_root_path / "content"

    # Load Hugo config for taxonomy definitions
    hugo_config = load_hugo_config(hugo_root)
    taxonomy_defs = get_taxonomies(hugo_config)

    # Discovery
    if paths:
        md_files = [content_dir / p for p in paths if (content_dir / p).exists()]
    else:
        md_files = discover_content(content_dir)

    stats = {"indexed": 0, "unchanged": 0, "removed": 0, "errors": []}

    # Track which paths we've seen for cleanup
    seen_paths: set[str] = set()

    for md_file in md_files:
        rel_path = str(md_file.relative_to(content_dir))
        seen_paths.add(rel_path)

        try:
            raw_bytes = md_file.read_bytes()
            file_hash = _content_hash(raw_bytes)
            file_mtime = md_file.stat().st_mtime

            # Incremental check
            if not force:
                sync = db.get_sync_state(rel_path)
                if sync and sync["content_hash"] == file_hash:
                    # Content unchanged — update mtime if needed
                    if sync["file_mtime"] != file_mtime:
                        db.save_sync_state(
                            rel_path, file_hash, file_mtime, _now_iso()
                        )
                    stats["unchanged"] += 1
                    continue

            # Parse content
            raw = raw_bytes.decode("utf-8-sig")
            front_matter, body = parse_content(raw)

            # Sanitize front matter for JSON storage
            front_matter = _make_json_safe(front_matter)

            # Build page record
            page = extract_page_record(rel_path, front_matter, body, file_hash)

            # Extract taxonomies
            taxonomies = extract_taxonomies(front_matter, taxonomy_defs)

            # Atomically save page + taxonomies + sync state
            db.index_page(page, taxonomies, file_mtime, _now_iso())
            stats["indexed"] += 1

        except Exception as e:
            stats["errors"].append({"path": rel_path, "error": str(e)})

    # Cleanup: remove pages that no longer exist on disk
    if not paths:
        indexed_paths = db.get_all_indexed_paths()
        removed_paths = indexed_paths - seen_paths
        for path in removed_paths:
            db.delete_page(path)
            db.delete_sync_state(path)
            stats["removed"] += 1

    return stats
