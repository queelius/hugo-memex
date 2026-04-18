"""Indexer pipeline for Hugo content.

Walks content/, parses front matter, populates the SQLite database
with incremental sync support.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from hugo_memex.config import load_hugo_config, get_taxonomies
from hugo_memex.db import Database
from hugo_memex.parser import parse_content
from hugo_memex.writer import page_path_for_marginalia


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _word_count(text: str) -> int:
    return len(text.split())


def _normalize_date(value: Any) -> str | None:
    """Normalize various date formats to ISO 8601 string.

    For UTC datetimes, emits the Z-form (``...Z``) rather than ``+00:00``
    to match the conventional RFC 3339 representation used in YAML/JSON.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        s = value.isoformat()
        if s.endswith("+00:00"):
            s = s[:-6] + "Z"
        return s
    if isinstance(value, date):
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


def discover_marginalia(data_dir: Path) -> list[Path]:
    """Walk data/marginalia/ and collect all .yaml files."""
    marginalia_dir = data_dir / "marginalia"
    if not marginalia_dir.exists():
        return []
    return sorted(p for p in marginalia_dir.rglob("*.yaml") if p.is_file())


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
        Stats dict with keys: indexed, unchanged, archived, restored, errors,
        marginalia_indexed, marginalia_unchanged, marginalia_archived,
        marginalia_restored.
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

    stats: dict[str, Any] = {
        "indexed": 0, "unchanged": 0, "archived": 0, "restored": 0,
        "errors": [],
        "marginalia_indexed": 0, "marginalia_unchanged": 0,
        "marginalia_archived": 0, "marginalia_restored": 0,
    }

    # Track which paths we've seen for cleanup
    seen_paths: set[str] = set()

    # Snapshot archived_at state for all known pages before the indexing pass,
    # since INSERT OR REPLACE in index_page clears the column to its default.
    pre_archived_paths: set[str] = set()
    if not paths:
        pre_archived_rows = db.execute_sql(
            "SELECT path FROM pages WHERE archived_at IS NOT NULL"
        )
        pre_archived_paths = {r["path"] for r in pre_archived_rows}

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

    # Archive pages whose source file is missing; restore pages whose file returned.
    if not paths:
        all_known_paths = db.get_all_indexed_paths()
        missing_paths = all_known_paths - seen_paths
        now_iso = _now_iso()
        for path in missing_paths:
            if path not in pre_archived_paths:
                db.archive_page(path, now_iso)
                stats["archived"] += 1
            # Missing but already archived: no-op (idempotent)
        # A page is "restored" if it was archived before this run and its file
        # is now present again. index_page's INSERT OR REPLACE clears archived_at
        # for rows that were re-indexed; we only need to explicitly restore
        # when the "unchanged" fast-path skipped re-indexing.
        restored_paths = pre_archived_paths & seen_paths
        for path in restored_paths:
            db.restore_page(path)  # no-op if already cleared by INSERT OR REPLACE
            stats["restored"] += 1

    # ── Marginalia pass ─────────────────────────────────────────
    data_dir = hugo_root_path / "data"
    marginalia_files = discover_marginalia(data_dir)
    seen_marginalia_sources: set[str] = set()

    for yaml_file in marginalia_files:
        rel_file = str(yaml_file.relative_to(hugo_root_path))
        seen_marginalia_sources.add(rel_file)

        try:
            raw_bytes = yaml_file.read_bytes()
            file_hash = _content_hash(raw_bytes)
            file_mtime = yaml_file.stat().st_mtime

            if not force:
                sync = db.get_sync_state(rel_file)
                if sync and sync["content_hash"] == file_hash:
                    if sync["file_mtime"] != file_mtime:
                        db.save_sync_state(
                            rel_file, file_hash, file_mtime, _now_iso()
                        )
                    stats["marginalia_unchanged"] += 1
                    continue

            notes = yaml.safe_load(raw_bytes.decode("utf-8"))
            if not isinstance(notes, list):
                notes = []

            marginalia_rel = str(
                yaml_file.relative_to(data_dir / "marginalia")
            )
            page_path = page_path_for_marginalia(marginalia_rel)

            # Diff sync: compare YAML notes to existing DB rows for this source.
            existing_rows = db.execute_sql(
                "SELECT id, archived_at FROM marginalia WHERE source_file = ?",
                (rel_file,),
            )
            existing_by_id = {r["id"]: r for r in existing_rows}
            yaml_by_id: dict[str, dict] = {}
            notes_in_file = 0
            now_iso = _now_iso()
            for note in notes:
                note_id = note.get("id")
                body = note.get("body")
                if not note_id or not body:
                    continue
                yaml_by_id[note_id] = note
                created_at = _normalize_date(note.get("created")) or now_iso
                archived_at = _normalize_date(note.get("archived_at"))
                db.save_marginalia({
                    "id": note_id,
                    "page_path": page_path,
                    "body": body,
                    "created_at": created_at,
                    "source_file": rel_file,
                    "archived_at": archived_at,
                })
                notes_in_file += 1

                prev = existing_by_id.get(note_id)
                if prev is not None:
                    was_archived = prev["archived_at"] is not None
                    now_archived = archived_at is not None
                    if not was_archived and now_archived:
                        stats["marginalia_archived"] += 1
                    elif was_archived and not now_archived:
                        stats["marginalia_restored"] += 1

            # Notes in DB but not in YAML anymore: archive them.
            for existing_id, prev in existing_by_id.items():
                if existing_id in yaml_by_id:
                    continue
                if prev["archived_at"] is None:
                    db.archive_marginalia(existing_id, now_iso)
                    stats["marginalia_archived"] += 1

            db.save_sync_state(rel_file, file_hash, file_mtime, _now_iso())
            stats["marginalia_indexed"] += notes_in_file

        except Exception as e:
            stats["errors"].append({"path": rel_file, "error": str(e)})

    # Archive marginalia from YAML files that no longer exist on disk.
    if not paths:
        known_sources = db.get_all_marginalia_source_files()
        missing_sources = known_sources - seen_marginalia_sources
        now_iso = _now_iso()
        for source in missing_sources:
            existing = db.execute_sql(
                "SELECT id, archived_at FROM marginalia WHERE source_file = ?",
                (source,),
            )
            for row in existing:
                if row["archived_at"] is None:
                    db.archive_marginalia(row["id"], now_iso)
                    stats["marginalia_archived"] += 1
            # Leave sync_state intact so we can detect the file returning.

    return stats
