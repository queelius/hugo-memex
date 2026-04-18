"""SQLite database layer for hugo-memex. Raw sqlite3 — no ORM."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 3

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
    indexed_at TEXT NOT NULL,
    archived_at TEXT
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

CREATE TABLE IF NOT EXISTS marginalia (
    id TEXT PRIMARY KEY,
    page_path TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source_file TEXT NOT NULL,
    archived_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_marginalia_page ON marginalia(page_path);
CREATE INDEX IF NOT EXISTS idx_marginalia_source ON marginalia(source_file);
CREATE INDEX IF NOT EXISTS idx_pages_archived ON pages(archived_at);
CREATE INDEX IF NOT EXISTS idx_marginalia_archived ON marginalia(archived_at);

CREATE VIRTUAL TABLE IF NOT EXISTS marginalia_fts USING fts5(
    id UNINDEXED,
    body,
    tokenize = 'porter unicode61'
);
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
        skip_prefixes = ("pages_fts_", "marginalia_fts_", "schema_version")
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
--   WHERE t.taxonomy = 'tags' GROUP BY t.term ORDER BY count DESC

-- ══ Marginalia Queries ═════════════════════════════════════════
-- marginalia stores free-form notes attached to pages.
-- marginalia_fts indexes note body with porter stemming.
--
-- All notes for a page:
--   SELECT id, body, created_at FROM marginalia
--   WHERE page_path = 'post/my-post/index.md'
--   ORDER BY created_at
--
-- FTS search across all marginalia:
--   SELECT m.id, m.page_path, m.body
--   FROM marginalia_fts f
--   JOIN marginalia m ON m.id = f.id
--   WHERE marginalia_fts MATCH 'search terms'
--   ORDER BY rank LIMIT 20
--
-- Orphaned marginalia (page deleted but notes survive):
--   SELECT id, page_path, body FROM marginalia
--   WHERE page_path NOT IN (SELECT path FROM pages)"""
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

    # ── Marginalia CRUD ─────────────────────────────────────────

    def save_marginalia(self, note: dict) -> None:
        """Insert or replace a marginalia record and update FTS5.

        Supports optional archived_at field in the note dict.
        """
        self.conn.execute(
            "INSERT OR REPLACE INTO marginalia "
            "(id, page_path, body, created_at, source_file, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                note["id"], note.get("page_path"), note["body"],
                note["created_at"], note["source_file"],
                note.get("archived_at"),
            ),
        )
        self.conn.execute(
            "DELETE FROM marginalia_fts WHERE id = ?", (note["id"],)
        )
        self.conn.execute(
            "INSERT INTO marginalia_fts (id, body) VALUES (?, ?)",
            (note["id"], note["body"]),
        )
        self.conn.commit()

    def get_marginalia(
        self, page_path: str, include_archived: bool = False,
    ) -> list[dict]:
        """Return marginalia for a page, ordered by created_at.

        By default excludes archived notes. Pass include_archived=True
        to return all notes regardless of archive state.
        """
        if include_archived:
            return self.execute_sql(
                "SELECT * FROM marginalia WHERE page_path = ? "
                "ORDER BY created_at",
                (page_path,),
            )
        return self.execute_sql(
            "SELECT * FROM marginalia WHERE page_path = ? "
            "AND archived_at IS NULL ORDER BY created_at",
            (page_path,),
        )

    def delete_marginalia(self, note_id: str) -> bool:
        """Delete a marginalia record and its FTS entry. Returns True if found."""
        try:
            self.conn.execute(
                "DELETE FROM marginalia_fts WHERE id = ?", (note_id,)
            )
            cursor = self.conn.execute(
                "DELETE FROM marginalia WHERE id = ?", (note_id,)
            )
            self.conn.commit()
            return cursor.rowcount > 0
        except Exception:
            self.conn.rollback()
            raise

    def get_all_marginalia_source_files(self) -> set[str]:
        """Return distinct source_file values from marginalia."""
        rows = self.execute_sql(
            "SELECT DISTINCT source_file FROM marginalia"
        )
        return {r["source_file"] for r in rows}

    def delete_marginalia_by_source(self, source_file: str) -> int:
        """Delete all marginalia from a source file. Returns count deleted."""
        try:
            # Get IDs to clean from FTS
            ids = self.execute_sql(
                "SELECT id FROM marginalia WHERE source_file = ?",
                (source_file,),
            )
            for row in ids:
                self.conn.execute(
                    "DELETE FROM marginalia_fts WHERE id = ?", (row["id"],)
                )
            cursor = self.conn.execute(
                "DELETE FROM marginalia WHERE source_file = ?",
                (source_file,),
            )
            self.conn.commit()
            return cursor.rowcount
        except Exception:
            self.conn.rollback()
            raise


    # ── Archive / Restore ───────────────────────────────────────

    def _run_write(self, sql: str, params: tuple):
        """Execute a write statement, temporarily lifting the readonly authorizer
        if necessary. The authorizer blocks raw SQL writes via execute_sql
        (guarding against SQL injection), but direct Python API methods on the
        Database object are expected to work regardless.
        """
        if self.readonly:
            self.conn.set_authorizer(None)
            try:
                cursor = self.conn.execute(sql, params)
                self.conn.commit()
                return cursor
            finally:
                self.conn.set_authorizer(_readonly_authorizer)
        cursor = self.conn.execute(sql, params)
        self.conn.commit()
        return cursor

    def archive_page(self, path: str, timestamp: str) -> bool:
        """Mark a page archived. Idempotent: does not overwrite existing archived_at.

        Returns True if the page exists (whether it was newly archived or already
        archived), False if the page does not exist.
        """
        cursor = self._run_write(
            "UPDATE pages SET archived_at = ? "
            "WHERE path = ? AND archived_at IS NULL",
            (timestamp, path),
        )
        if cursor.rowcount > 0:
            return True
        exists = self.execute_sql(
            "SELECT 1 FROM pages WHERE path = ?", (path,)
        )
        return bool(exists)

    def restore_page(self, path: str) -> bool:
        """Clear archived_at on a page. Returns True if a row was updated."""
        cursor = self._run_write(
            "UPDATE pages SET archived_at = NULL "
            "WHERE path = ? AND archived_at IS NOT NULL",
            (path,),
        )
        return cursor.rowcount > 0

    def archive_marginalia(self, note_id: str, timestamp: str) -> bool:
        """Mark a marginalia note archived. Idempotent."""
        cursor = self._run_write(
            "UPDATE marginalia SET archived_at = ? "
            "WHERE id = ? AND archived_at IS NULL",
            (timestamp, note_id),
        )
        if cursor.rowcount > 0:
            return True
        exists = self.execute_sql(
            "SELECT 1 FROM marginalia WHERE id = ?", (note_id,)
        )
        return bool(exists)

    def restore_marginalia_row(self, note_id: str) -> bool:
        """Clear archived_at on a marginalia note. Returns True if a row was updated.

        Named `restore_marginalia_row` (not `restore_marginalia`) to avoid confusion
        with the MCP-level restore_marginalia tool, which also edits the YAML file.
        """
        cursor = self._run_write(
            "UPDATE marginalia SET archived_at = NULL "
            "WHERE id = ? AND archived_at IS NOT NULL",
            (note_id,),
        )
        return cursor.rowcount > 0

    # ── Purge helpers (used by CLI purge command) ───────────────

    def find_all_archived_pages(self) -> list[str]:
        """Return paths of all archived pages."""
        rows = self.execute_sql(
            "SELECT path FROM pages WHERE archived_at IS NOT NULL ORDER BY path"
        )
        return [r["path"] for r in rows]

    def find_archived_pages_before(self, cutoff: str) -> list[str]:
        """Return paths of pages archived before a given ISO timestamp."""
        rows = self.execute_sql(
            "SELECT path FROM pages "
            "WHERE archived_at IS NOT NULL AND archived_at < ? "
            "ORDER BY path",
            (cutoff,),
        )
        return [r["path"] for r in rows]

    def find_all_archived_marginalia(self) -> list[dict]:
        """Return id + source_file for all archived marginalia notes."""
        return self.execute_sql(
            "SELECT id, source_file FROM marginalia "
            "WHERE archived_at IS NOT NULL ORDER BY id"
        )

    def find_archived_marginalia_before(self, cutoff: str) -> list[dict]:
        """Return id + source_file for marginalia archived before cutoff."""
        return self.execute_sql(
            "SELECT id, source_file FROM marginalia "
            "WHERE archived_at IS NOT NULL AND archived_at < ? "
            "ORDER BY id",
            (cutoff,),
        )


def _migrate_v1_to_v2(conn):
    """Add marginalia tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS marginalia (
            id TEXT PRIMARY KEY,
            page_path TEXT,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            source_file TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_marginalia_page ON marginalia(page_path);
        CREATE INDEX IF NOT EXISTS idx_marginalia_source ON marginalia(source_file);
        CREATE VIRTUAL TABLE IF NOT EXISTS marginalia_fts USING fts5(
            id UNINDEXED,
            body,
            tokenize = 'porter unicode61'
        );
    """)


def _migrate_v2_to_v3(conn):
    """Add archived_at columns and indexes to pages and marginalia."""
    conn.executescript("""
        ALTER TABLE pages ADD COLUMN archived_at TEXT;
        ALTER TABLE marginalia ADD COLUMN archived_at TEXT;
        CREATE INDEX IF NOT EXISTS idx_pages_archived ON pages(archived_at);
        CREATE INDEX IF NOT EXISTS idx_marginalia_archived ON marginalia(archived_at);
    """)


# Migration registry (version_from → migration_fn)
_MIGRATIONS: dict[int, Callable] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
}
