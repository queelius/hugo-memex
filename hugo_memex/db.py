"""SQLite database layer for hugo-memex. Raw sqlite3 — no ORM."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    path TEXT PRIMARY KEY,
    slug TEXT,
    title TEXT NOT NULL,
    section TEXT NOT NULL,
    kind TEXT NOT NULL,
    bundle_type TEXT,
    date TEXT,
    draft BOOLEAN NOT NULL DEFAULT 0,
    description TEXT,
    word_count INTEGER,
    body TEXT,
    front_matter JSON NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS taxonomies (
    page_path TEXT NOT NULL REFERENCES pages(path) ON DELETE CASCADE,
    taxonomy TEXT NOT NULL,
    term TEXT NOT NULL,
    PRIMARY KEY (page_path, taxonomy, term)
);

CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    path UNINDEXED,
    title,
    description,
    body,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS sync_state (
    file_path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    file_mtime REAL NOT NULL,
    last_synced TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pages_section ON pages(section);
CREATE INDEX IF NOT EXISTS idx_pages_date ON pages(date);
CREATE INDEX IF NOT EXISTS idx_pages_draft ON pages(draft);
CREATE INDEX IF NOT EXISTS idx_pages_section_date ON pages(section, date DESC);
CREATE INDEX IF NOT EXISTS idx_taxonomies_term ON taxonomies(taxonomy, term);
CREATE INDEX IF NOT EXISTS idx_taxonomies_page ON taxonomies(page_path);
"""


def _dict_factory(cursor, row):
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


# Actions the authorizer allows for read-only connections.
# This cannot be bypassed via SQL (unlike PRAGMA query_only).
_READONLY_ALLOWED = {
    sqlite3.SQLITE_SELECT,         # SELECT statements
    sqlite3.SQLITE_READ,           # Column access
    sqlite3.SQLITE_FUNCTION,       # json_extract, rank, etc.
}

# PRAGMAs that must never be allowed (they re-enable writes)
_DENIED_PRAGMAS = {"query_only", "writable_schema"}


def _readonly_authorizer(action_code, arg1, *_args):
    """SQLite authorizer that denies all write operations.

    Allows read-only PRAGMAs (needed by FTS5 internally) but blocks
    PRAGMAs that could re-enable writes.
    """
    if action_code in _READONLY_ALLOWED:
        return sqlite3.SQLITE_OK
    if action_code == sqlite3.SQLITE_PRAGMA:
        # Allow read-only PRAGMAs (e.g. data_version used by FTS5)
        # but block PRAGMAs that could re-enable writes
        if arg1 and arg1.lower() in _DENIED_PRAGMAS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class Database:
    """SQLite database for Hugo content index."""

    def __init__(self, path: str = ":memory:", readonly: bool = False):
        self.db_path = path
        self.readonly = readonly
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = _dict_factory
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()
        if readonly:
            self.conn.set_authorizer(_readonly_authorizer)

    def _ensure_schema(self):
        tables = {
            r["name"]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        has_pages = "pages" in tables
        has_version = "schema_version" in tables

        if not has_pages or not has_version:
            self.conn.executescript(SCHEMA_SQL)
            self.conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self.conn.commit()
        else:
            self._apply_migrations()

    def _apply_migrations(self):
        row = self.conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()
        current = row["version"] if row else 1
        while current < SCHEMA_VERSION:
            migrate_fn = _MIGRATIONS.get(current)
            if migrate_fn is None:
                raise RuntimeError(
                    f"No migration from v{current} to v{current + 1}"
                )
            migrate_fn(self.conn)
            current += 1
            self.conn.execute(
                "UPDATE schema_version SET version=?", (current,)
            )
            self.conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    # ── Query ────────────────────────────────────────────────────

    def execute_sql(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Execute a SQL query and return results as list of dicts."""
        cursor = self.conn.execute(sql, params)
        if cursor.description is None:
            self.conn.commit()
            return []
        return cursor.fetchall()

    # ── Schema info ──────────────────────────────────────────────

    def get_schema(self) -> str:
        """Return DDL + relationship docs for the LLM."""
        skip_prefixes = ("pages_fts_", "schema_version")
        rows = self.execute_sql(
            "SELECT name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL ORDER BY type, name"
        )
        ddl = "\n\n".join(
            r["sql"] for r in rows
            if not any(r["name"].startswith(p) for p in skip_prefixes)
        )
        docs = """

-- ══ Relationships ══════════════════════════════════════════════
-- taxonomies.page_path  → pages.path  (CASCADE delete)
-- pages_fts.path        → pages.path  (manually synced)
-- sync_state.file_path  → filesystem  (tracking table)

-- ══ FTS5 Full-Text Search ══════════════════════════════════════
-- pages_fts indexes page content with porter stemming + unicode61.
-- Columns: path (UNINDEXED), title, description, body
--
-- FTS search query pattern:
--   SELECT p.path, p.title, p.section, p.date
--   FROM pages_fts f
--   JOIN pages p ON p.path = f.path
--   WHERE pages_fts MATCH 'search terms'
--   ORDER BY rank
--   LIMIT 20
--
-- MATCH syntax: 'word1 word2' (implicit AND), 'word1 OR word2',
-- '"exact phrase"', 'word*' (prefix)

-- ══ JSON Front Matter Queries ══════════════════════════════════
-- All front matter is stored losslessly in pages.front_matter (JSON).
-- Use json_extract() for structured queries:
--
--   SELECT path, title, json_extract(front_matter, '$.project.status') as status
--   FROM pages WHERE json_extract(front_matter, '$.project.status') = 'active'
--
--   SELECT path, title FROM pages
--   WHERE json_extract(front_matter, '$.tech.languages') LIKE '%Rust%'

-- ══ Taxonomy Queries ═══════════════════════════════════════════
-- Taxonomies are auto-discovered from hugo.toml and normalized
-- into the taxonomies table. Common patterns:
--
--   SELECT DISTINCT term FROM taxonomies WHERE taxonomy = 'tags'
--   ORDER BY term
--
--   SELECT p.* FROM pages p
--   JOIN taxonomies t ON p.path = t.page_path
--   WHERE t.taxonomy = 'tags' AND t.term = 'python'
--
--   SELECT t.term, COUNT(*) as count FROM taxonomies t
--   WHERE t.taxonomy = 'tags' GROUP BY t.term ORDER BY count DESC"""
        return ddl + docs

    # ── Statistics ───────────────────────────────────────────────

    def get_statistics(self) -> dict[str, Any]:
        """Return aggregate stats for the hugo://stats resource."""
        agg = self.execute_sql(
            "SELECT COUNT(*) as total, "
            "COALESCE(SUM(word_count), 0) as word_count, "
            "SUM(CASE WHEN draft = 1 THEN 1 ELSE 0 END) as draft, "
            "SUM(CASE WHEN draft = 0 THEN 1 ELSE 0 END) as published, "
            "MIN(CASE WHEN date IS NOT NULL THEN date END) as earliest, "
            "MAX(CASE WHEN date IS NOT NULL THEN date END) as latest "
            "FROM pages"
        )[0]
        by_section = {
            r["section"]: r["n"]
            for r in self.execute_sql(
                "SELECT section, COUNT(*) as n FROM pages "
                "GROUP BY section ORDER BY n DESC"
            )
        }
        taxonomy_counts = {}
        for r in self.execute_sql(
            "SELECT taxonomy, COUNT(DISTINCT term) as terms, "
            "COUNT(*) as usages FROM taxonomies GROUP BY taxonomy"
        ):
            taxonomy_counts[r["taxonomy"]] = {
                "distinct_terms": r["terms"],
                "total_usages": r["usages"],
            }
        return {
            "total_pages": agg["total"],
            "total_word_count": agg["word_count"],
            "pages_by_section": by_section,
            "draft_status": {
                "draft": agg["draft"] or 0,
                "published": agg["published"] or 0,
            },
            "taxonomies": taxonomy_counts,
            "date_range": {
                "earliest": agg["earliest"],
                "latest": agg["latest"],
            },
        }

    # ── Page CRUD ────────────────────────────────────────────────

    def save_page(self, page: dict[str, Any]) -> None:
        """Insert or replace a page record and update FTS5."""
        self._write_page(page)
        self.conn.commit()

    def _write_page(self, page: dict[str, Any]) -> None:
        """Write page + FTS without committing (for use in transactions)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO pages "
            "(path, slug, title, section, kind, bundle_type, "
            "date, draft, description, word_count, body, "
            "front_matter, content_hash, indexed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                page["path"], page.get("slug"), page["title"],
                page["section"], page["kind"], page.get("bundle_type"),
                page.get("date"), int(page.get("draft", False)),
                page.get("description"), page.get("word_count"),
                page.get("body"),
                json.dumps(page.get("front_matter", {})),
                page["content_hash"], page["indexed_at"],
            ),
        )
        self.conn.execute(
            "DELETE FROM pages_fts WHERE path = ?", (page["path"],)
        )
        self.conn.execute(
            "INSERT INTO pages_fts (path, title, description, body) "
            "VALUES (?, ?, ?, ?)",
            (
                page["path"], page["title"],
                page.get("description", ""), page.get("body", ""),
            ),
        )

    def save_taxonomies(self, page_path: str, taxonomies: dict[str, list[str]]) -> None:
        """Save taxonomy terms for a page. Replaces existing terms."""
        self._write_taxonomies(page_path, taxonomies)
        self.conn.commit()

    def _write_taxonomies(self, page_path: str, taxonomies: dict[str, list[str]]) -> None:
        """Write taxonomies without committing (for use in transactions)."""
        self.conn.execute(
            "DELETE FROM taxonomies WHERE page_path = ?", (page_path,)
        )
        for taxonomy, terms in taxonomies.items():
            for term in terms:
                self.conn.execute(
                    "INSERT OR IGNORE INTO taxonomies "
                    "(page_path, taxonomy, term) VALUES (?, ?, ?)",
                    (page_path, taxonomy, term),
                )

    def _write_sync_state(
        self, file_path: str, content_hash: str,
        file_mtime: float, last_synced: str,
    ) -> None:
        """Write sync state without committing (for use in transactions)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO sync_state "
            "(file_path, content_hash, file_mtime, last_synced) "
            "VALUES (?, ?, ?, ?)",
            (file_path, content_hash, file_mtime, last_synced),
        )

    def index_page(
        self, page: dict[str, Any],
        taxonomies: dict[str, list[str]],
        file_mtime: float, last_synced: str,
    ) -> None:
        """Atomically save page, taxonomies, and sync state in one transaction."""
        try:
            self._write_page(page)
            if taxonomies:
                self._write_taxonomies(page["path"], taxonomies)
            self._write_sync_state(
                page["path"], page["content_hash"], file_mtime, last_synced,
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def delete_page(self, path: str) -> bool:
        """Delete a page and its taxonomies/FTS. Returns True if found."""
        try:
            self.conn.execute(
                "DELETE FROM pages_fts WHERE path = ?", (path,)
            )
            cursor = self.conn.execute(
                "DELETE FROM pages WHERE path = ?", (path,)
            )
            self.conn.commit()
            return cursor.rowcount > 0
        except Exception:
            self.conn.rollback()
            raise

    # ── Sync state ───────────────────────────────────────────────

    def get_sync_state(self, file_path: str) -> dict[str, Any] | None:
        """Get sync state for a file."""
        rows = self.execute_sql(
            "SELECT * FROM sync_state WHERE file_path = ?", (file_path,)
        )
        return rows[0] if rows else None

    def save_sync_state(
        self, file_path: str, content_hash: str,
        file_mtime: float, last_synced: str,
    ) -> None:
        """Update sync state for a file."""
        self.conn.execute(
            "INSERT OR REPLACE INTO sync_state "
            "(file_path, content_hash, file_mtime, last_synced) "
            "VALUES (?, ?, ?, ?)",
            (file_path, content_hash, file_mtime, last_synced),
        )
        self.conn.commit()

    def delete_sync_state(self, file_path: str) -> None:
        """Remove sync state for a deleted file."""
        self.conn.execute(
            "DELETE FROM sync_state WHERE file_path = ?", (file_path,)
        )
        self.conn.commit()

    def get_all_indexed_paths(self) -> set[str]:
        """Return all page paths currently in the index."""
        rows = self.execute_sql("SELECT path FROM pages")
        return {r["path"] for r in rows}


# Migration registry (version_from → migration_fn)
_MIGRATIONS: dict[int, Callable] = {}
