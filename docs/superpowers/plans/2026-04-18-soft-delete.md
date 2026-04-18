# Soft Delete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `archived_at`-based soft delete to `pages` and `marginalia` per the `*-memex` workspace contract: default deletes are soft, hard delete is opt-in via CLI purge, and archived records are hidden from default reads but preserved for URI stability.

**Architecture:** Schema v3 adds `archived_at TEXT NULL` columns to both record tables. The indexer switches from hard-delete cleanup to an `archive on missing, restore on return` model with a diff-based sync for marginalia notes. Marginalia YAML files gain a backward-compatible per-note `archived_at` field. MCP write tools archive by default; a new CLI `purge` command is the only path to hard delete.

**Tech Stack:** Python 3.11+, sqlite3, PyYAML, FastMCP v2, pytest + pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-04-18-soft-delete-design.md`

---

### Task 1: Schema v3 migration and archived_at columns

Add `archived_at` columns + indexes, bump schema version, register migration.

**Files:**
- Modify: `hugo_memex/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write failing tests for schema v3**

Add to `tests/test_db.py`:

```python
class TestSchemaV3:
    def test_pages_has_archived_at(self, db):
        cols = {r["name"] for r in db.execute_sql("PRAGMA table_info(pages)")}
        assert "archived_at" in cols

    def test_marginalia_has_archived_at(self, db):
        cols = {r["name"] for r in db.execute_sql("PRAGMA table_info(marginalia)")}
        assert "archived_at" in cols

    def test_archived_at_defaults_null(self, db):
        # Insert a page without specifying archived_at, it should be NULL
        db.conn.execute(
            "INSERT INTO pages (path, title, section, kind, front_matter, content_hash, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("post/t/index.md", "T", "post", "page", "{}", "h", "2026-01-01T00:00:00Z"),
        )
        db.conn.commit()
        rows = db.execute_sql("SELECT archived_at FROM pages WHERE path = ?", ("post/t/index.md",))
        assert rows[0]["archived_at"] is None

    def test_archived_at_indexes_exist(self, db):
        indexes = {r["name"] for r in db.execute_sql(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
        assert "idx_pages_archived" in indexes
        assert "idx_marginalia_archived" in indexes

    def test_schema_version_is_3(self, db):
        rows = db.execute_sql("SELECT version FROM schema_version")
        assert rows[0]["version"] == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py::TestSchemaV3 -v`
Expected: FAIL (column `archived_at` does not exist)

- [ ] **Step 3: Update SCHEMA_SQL and bump SCHEMA_VERSION to 3**

In `hugo_memex/db.py`:

Change `SCHEMA_VERSION = 2` to `SCHEMA_VERSION = 3`.

In `SCHEMA_SQL`, add `archived_at TEXT` to the `pages` CREATE TABLE (after `indexed_at TEXT NOT NULL,`) and to the `marginalia` CREATE TABLE (after `source_file TEXT NOT NULL,`). Add these two index statements before the closing `"""`:

```sql
CREATE INDEX IF NOT EXISTS idx_pages_archived ON pages(archived_at);
CREATE INDEX IF NOT EXISTS idx_marginalia_archived ON marginalia(archived_at);
```

- [ ] **Step 4: Add the v2-to-v3 migration function**

In `hugo_memex/db.py`, near the existing `_migrate_v1_to_v2` function, add:

```python
def _migrate_v2_to_v3(conn):
    """Add archived_at columns and indexes to pages and marginalia."""
    conn.executescript("""
        ALTER TABLE pages ADD COLUMN archived_at TEXT;
        ALTER TABLE marginalia ADD COLUMN archived_at TEXT;
        CREATE INDEX IF NOT EXISTS idx_pages_archived ON pages(archived_at);
        CREATE INDEX IF NOT EXISTS idx_marginalia_archived ON marginalia(archived_at);
    """)
```

Update the `_MIGRATIONS` dict at the bottom of the file:

```python
_MIGRATIONS: dict[int, Callable] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_db.py::TestSchemaV3 -v`
Expected: all 5 tests PASS

- [ ] **Step 6: Write migration test (v2 → v3)**

Add to `tests/test_db.py`:

```python
class TestMigrationV2ToV3:
    def test_v2_db_migrates_to_v3(self, tmp_path):
        """A v2 database on disk upgrades to v3 when opened, preserving data."""
        import sqlite3
        db_path = tmp_path / "v2.db"
        # Create a minimal v2 database manually
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (2);
            CREATE TABLE pages (
                path TEXT PRIMARY KEY, slug TEXT, title TEXT NOT NULL,
                section TEXT NOT NULL, kind TEXT NOT NULL, bundle_type TEXT,
                date TEXT, draft BOOLEAN NOT NULL DEFAULT 0, description TEXT,
                word_count INTEGER, body TEXT, front_matter JSON NOT NULL DEFAULT '{}',
                content_hash TEXT NOT NULL, indexed_at TEXT NOT NULL
            );
            CREATE TABLE marginalia (
                id TEXT PRIMARY KEY, page_path TEXT, body TEXT NOT NULL,
                created_at TEXT NOT NULL, source_file TEXT NOT NULL
            );
            CREATE TABLE taxonomies (
                page_path TEXT NOT NULL, taxonomy TEXT NOT NULL, term TEXT NOT NULL,
                PRIMARY KEY (page_path, taxonomy, term)
            );
            CREATE TABLE sync_state (
                file_path TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
                file_mtime REAL NOT NULL, last_synced TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE pages_fts USING fts5(path UNINDEXED, title, description, body);
            CREATE VIRTUAL TABLE marginalia_fts USING fts5(id UNINDEXED, body);
            INSERT INTO pages (path, title, section, kind, front_matter, content_hash, indexed_at)
            VALUES ('post/existing/index.md', 'Existing', 'post', 'page', '{}', 'h', '2026-01-01T00:00:00Z');
        """)
        conn.commit()
        conn.close()

        # Open via Database class, should migrate to v3
        from hugo_memex.db import Database
        db = Database(str(db_path))
        try:
            version = db.execute_sql("SELECT version FROM schema_version")[0]["version"]
            assert version == 3
            # Verify columns added
            cols = {r["name"] for r in db.execute_sql("PRAGMA table_info(pages)")}
            assert "archived_at" in cols
            # Verify pre-migration data preserved and defaults to NULL
            rows = db.execute_sql("SELECT path, archived_at FROM pages")
            assert len(rows) == 1
            assert rows[0]["path"] == "post/existing/index.md"
            assert rows[0]["archived_at"] is None
        finally:
            db.close()
```

- [ ] **Step 7: Run migration test**

Run: `pytest tests/test_db.py::TestMigrationV2ToV3 -v`
Expected: PASS

- [ ] **Step 8: Run full test suite**

Run: `pytest tests/ -v`
Expected: all tests PASS (existing tests unaffected by additive migration)

- [ ] **Step 9: Commit**

```bash
git add hugo_memex/db.py tests/test_db.py
git commit -m "feat: add schema v3 with archived_at columns on pages and marginalia"
```

---

### Task 2: DB archive/restore methods for pages and marginalia

Add methods to mark records archived and restore them, update existing methods to handle archived_at.

**Files:**
- Modify: `hugo_memex/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write failing tests for archive/restore methods**

Add to `tests/test_db.py`:

```python
class TestArchiveRestoreMethods:
    def _make_page(self, db, path="post/t/index.md"):
        db.conn.execute(
            "INSERT INTO pages (path, title, section, kind, front_matter, content_hash, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (path, "T", "post", "page", "{}", "h", "2026-01-01T00:00:00Z"),
        )
        db.conn.commit()

    def test_archive_page_sets_timestamp(self, db):
        self._make_page(db)
        assert db.archive_page("post/t/index.md", "2026-04-18T12:00:00Z") is True
        row = db.execute_sql("SELECT archived_at FROM pages WHERE path = ?", ("post/t/index.md",))[0]
        assert row["archived_at"] == "2026-04-18T12:00:00Z"

    def test_archive_page_missing_returns_false(self, db):
        assert db.archive_page("post/missing/index.md", "2026-04-18T12:00:00Z") is False

    def test_archive_page_idempotent(self, db):
        """Archiving an already-archived page does not change archived_at."""
        self._make_page(db)
        db.archive_page("post/t/index.md", "2026-04-18T12:00:00Z")
        # Calling archive again with a different timestamp should NOT overwrite
        db.archive_page("post/t/index.md", "2026-05-01T00:00:00Z")
        row = db.execute_sql("SELECT archived_at FROM pages WHERE path = ?", ("post/t/index.md",))[0]
        assert row["archived_at"] == "2026-04-18T12:00:00Z"

    def test_restore_page_clears_timestamp(self, db):
        self._make_page(db)
        db.archive_page("post/t/index.md", "2026-04-18T12:00:00Z")
        assert db.restore_page("post/t/index.md") is True
        row = db.execute_sql("SELECT archived_at FROM pages WHERE path = ?", ("post/t/index.md",))[0]
        assert row["archived_at"] is None

    def test_restore_page_on_active_is_noop(self, db):
        self._make_page(db)
        # Restoring a page that isn't archived returns False (no change)
        assert db.restore_page("post/t/index.md") is False

    def test_archive_marginalia(self, db):
        db.save_marginalia({
            "id": "mg-1", "page_path": "post/t/index.md", "body": "note",
            "created_at": "2026-04-01T00:00:00Z", "source_file": "data/marginalia/post/t.yaml",
        })
        assert db.archive_marginalia("mg-1", "2026-04-18T12:00:00Z") is True
        row = db.execute_sql("SELECT archived_at FROM marginalia WHERE id = ?", ("mg-1",))[0]
        assert row["archived_at"] == "2026-04-18T12:00:00Z"

    def test_archive_marginalia_missing(self, db):
        assert db.archive_marginalia("mg-missing", "2026-04-18T12:00:00Z") is False

    def test_archive_marginalia_idempotent(self, db):
        db.save_marginalia({
            "id": "mg-idem", "page_path": "p", "body": "b",
            "created_at": "2026-04-01T00:00:00Z", "source_file": "f",
        })
        db.archive_marginalia("mg-idem", "2026-04-18T12:00:00Z")
        db.archive_marginalia("mg-idem", "2026-05-01T00:00:00Z")
        row = db.execute_sql("SELECT archived_at FROM marginalia WHERE id = ?", ("mg-idem",))[0]
        assert row["archived_at"] == "2026-04-18T12:00:00Z"

    def test_restore_marginalia(self, db):
        db.save_marginalia({
            "id": "mg-res", "page_path": "p", "body": "b",
            "created_at": "2026-04-01T00:00:00Z", "source_file": "f",
        })
        db.archive_marginalia("mg-res", "2026-04-18T12:00:00Z")
        assert db.restore_marginalia_row("mg-res") is True
        row = db.execute_sql("SELECT archived_at FROM marginalia WHERE id = ?", ("mg-res",))[0]
        assert row["archived_at"] is None


class TestSaveMarginaliaPreservesArchivedAt:
    def test_save_with_archived_at(self, db):
        db.save_marginalia({
            "id": "mg-a", "page_path": "p", "body": "b",
            "created_at": "2026-04-01T00:00:00Z", "source_file": "f",
            "archived_at": "2026-04-18T12:00:00Z",
        })
        row = db.execute_sql("SELECT archived_at FROM marginalia WHERE id = ?", ("mg-a",))[0]
        assert row["archived_at"] == "2026-04-18T12:00:00Z"

    def test_save_without_archived_at(self, db):
        db.save_marginalia({
            "id": "mg-b", "page_path": "p", "body": "b",
            "created_at": "2026-04-01T00:00:00Z", "source_file": "f",
        })
        row = db.execute_sql("SELECT archived_at FROM marginalia WHERE id = ?", ("mg-b",))[0]
        assert row["archived_at"] is None


class TestGetMarginaliaFiltering:
    def test_get_marginalia_excludes_archived_by_default(self, db):
        db.save_marginalia({
            "id": "mg-active", "page_path": "p", "body": "active",
            "created_at": "2026-04-01T00:00:00Z", "source_file": "f",
        })
        db.save_marginalia({
            "id": "mg-arch", "page_path": "p", "body": "archived",
            "created_at": "2026-04-02T00:00:00Z", "source_file": "f",
            "archived_at": "2026-04-18T12:00:00Z",
        })
        rows = db.get_marginalia("p")
        ids = {r["id"] for r in rows}
        assert ids == {"mg-active"}

    def test_get_marginalia_include_archived(self, db):
        db.save_marginalia({
            "id": "mg-active", "page_path": "p", "body": "active",
            "created_at": "2026-04-01T00:00:00Z", "source_file": "f",
        })
        db.save_marginalia({
            "id": "mg-arch", "page_path": "p", "body": "archived",
            "created_at": "2026-04-02T00:00:00Z", "source_file": "f",
            "archived_at": "2026-04-18T12:00:00Z",
        })
        rows = db.get_marginalia("p", include_archived=True)
        ids = {r["id"] for r in rows}
        assert ids == {"mg-active", "mg-arch"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py::TestArchiveRestoreMethods tests/test_db.py::TestSaveMarginaliaPreservesArchivedAt tests/test_db.py::TestGetMarginaliaFiltering -v`
Expected: FAIL (methods don't exist, `archived_at` not in save_marginalia)

- [ ] **Step 3: Update save_marginalia to handle archived_at**

In `hugo_memex/db.py`, replace the `save_marginalia` method with:

```python
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
```

- [ ] **Step 4: Update get_marginalia to support include_archived**

In `hugo_memex/db.py`, replace the `get_marginalia` method with:

```python
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
```

- [ ] **Step 5: Add archive/restore methods**

In `hugo_memex/db.py`, add these methods to the `Database` class (after `delete_marginalia_by_source`):

```python
    # ── Archive / Restore ───────────────────────────────────────

    def archive_page(self, path: str, timestamp: str) -> bool:
        """Mark a page archived. Idempotent: does not overwrite existing archived_at.

        Returns True if the page exists and was archived (or was already archived),
        False if the page does not exist.
        """
        cursor = self.conn.execute(
            "UPDATE pages SET archived_at = ? "
            "WHERE path = ? AND archived_at IS NULL",
            (timestamp, path),
        )
        self.conn.commit()
        if cursor.rowcount > 0:
            return True
        # Check if the page exists at all (already archived or missing)
        exists = self.execute_sql(
            "SELECT 1 FROM pages WHERE path = ?", (path,)
        )
        return bool(exists)

    def restore_page(self, path: str) -> bool:
        """Clear archived_at on a page. Returns True if a row was updated."""
        cursor = self.conn.execute(
            "UPDATE pages SET archived_at = NULL "
            "WHERE path = ? AND archived_at IS NOT NULL",
            (path,),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def archive_marginalia(self, note_id: str, timestamp: str) -> bool:
        """Mark a marginalia note archived. Idempotent."""
        cursor = self.conn.execute(
            "UPDATE marginalia SET archived_at = ? "
            "WHERE id = ? AND archived_at IS NULL",
            (timestamp, note_id),
        )
        self.conn.commit()
        if cursor.rowcount > 0:
            return True
        exists = self.execute_sql(
            "SELECT 1 FROM marginalia WHERE id = ?", (note_id,)
        )
        return bool(exists)

    def restore_marginalia_row(self, note_id: str) -> bool:
        """Clear archived_at on a marginalia note. Returns True if a row was updated.

        Named `restore_marginalia_row` (not `restore_marginalia`) to avoid
        confusion with the MCP-level restore_marginalia tool, which also edits
        the YAML file on disk.
        """
        cursor = self.conn.execute(
            "UPDATE marginalia SET archived_at = NULL "
            "WHERE id = ? AND archived_at IS NOT NULL",
            (note_id,),
        )
        self.conn.commit()
        return cursor.rowcount > 0
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_db.py::TestArchiveRestoreMethods tests/test_db.py::TestSaveMarginaliaPreservesArchivedAt tests/test_db.py::TestGetMarginaliaFiltering -v`
Expected: all tests PASS

- [ ] **Step 7: Run full test suite**

Run: `pytest tests/ -v`
Expected: all tests PASS

- [ ] **Step 8: Commit**

```bash
git add hugo_memex/db.py tests/test_db.py
git commit -m "feat: add archive/restore DB methods with include_archived filter"
```

---

### Task 3: DB purge methods for hard delete

Add methods used by the CLI purge command to hard-delete archived rows.

**Files:**
- Modify: `hugo_memex/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write failing tests for purge methods**

Add to `tests/test_db.py`:

```python
class TestPurgeMethods:
    def test_find_archived_pages_before(self, db):
        db.conn.execute(
            "INSERT INTO pages (path, title, section, kind, front_matter, content_hash, indexed_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("post/old/index.md", "Old", "post", "page", "{}", "h", "2026-01-01T00:00:00Z", "2026-01-15T00:00:00Z"),
        )
        db.conn.execute(
            "INSERT INTO pages (path, title, section, kind, front_matter, content_hash, indexed_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("post/recent/index.md", "Recent", "post", "page", "{}", "h", "2026-01-01T00:00:00Z", "2026-04-10T00:00:00Z"),
        )
        db.conn.execute(
            "INSERT INTO pages (path, title, section, kind, front_matter, content_hash, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("post/active/index.md", "Active", "post", "page", "{}", "h", "2026-01-01T00:00:00Z"),
        )
        db.conn.commit()
        paths = db.find_archived_pages_before("2026-03-01T00:00:00Z")
        assert paths == ["post/old/index.md"]

    def test_find_archived_marginalia_before(self, db):
        db.save_marginalia({
            "id": "mg-old", "page_path": "p", "body": "b",
            "created_at": "2026-01-01T00:00:00Z", "source_file": "f",
            "archived_at": "2026-01-15T00:00:00Z",
        })
        db.save_marginalia({
            "id": "mg-recent", "page_path": "p", "body": "b",
            "created_at": "2026-04-01T00:00:00Z", "source_file": "f",
            "archived_at": "2026-04-10T00:00:00Z",
        })
        db.save_marginalia({
            "id": "mg-active", "page_path": "p", "body": "b",
            "created_at": "2026-04-01T00:00:00Z", "source_file": "f",
        })
        results = db.find_archived_marginalia_before("2026-03-01T00:00:00Z")
        assert len(results) == 1
        assert results[0]["id"] == "mg-old"
        assert results[0]["source_file"] == "f"

    def test_find_all_archived_pages(self, db):
        db.conn.execute(
            "INSERT INTO pages (path, title, section, kind, front_matter, content_hash, indexed_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("post/a/index.md", "A", "post", "page", "{}", "h", "2026-01-01T00:00:00Z", "2026-04-10T00:00:00Z"),
        )
        db.conn.execute(
            "INSERT INTO pages (path, title, section, kind, front_matter, content_hash, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("post/b/index.md", "B", "post", "page", "{}", "h", "2026-01-01T00:00:00Z"),
        )
        db.conn.commit()
        paths = db.find_all_archived_pages()
        assert paths == ["post/a/index.md"]

    def test_find_all_archived_marginalia(self, db):
        db.save_marginalia({
            "id": "mg-a", "page_path": "p", "body": "b",
            "created_at": "2026-04-01T00:00:00Z", "source_file": "f1",
            "archived_at": "2026-04-10T00:00:00Z",
        })
        db.save_marginalia({
            "id": "mg-b", "page_path": "p", "body": "b",
            "created_at": "2026-04-01T00:00:00Z", "source_file": "f2",
        })
        results = db.find_all_archived_marginalia()
        ids = [r["id"] for r in results]
        assert ids == ["mg-a"]

    def test_delete_page_hard(self, db):
        db.conn.execute(
            "INSERT INTO pages (path, title, section, kind, front_matter, content_hash, indexed_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("post/x/index.md", "X", "post", "page", "{}", "h", "2026-01-01T00:00:00Z", "2026-04-10T00:00:00Z"),
        )
        db.conn.commit()
        # delete_page (hard) still works and removes the row
        assert db.delete_page("post/x/index.md") is True
        rows = db.execute_sql("SELECT 1 FROM pages WHERE path = ?", ("post/x/index.md",))
        assert rows == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py::TestPurgeMethods -v`
Expected: FAIL (methods don't exist)

- [ ] **Step 3: Add find methods to the Database class**

In `hugo_memex/db.py`, add after the archive/restore methods:

```python
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
```

Note: `delete_page` and `delete_marginalia` already exist and still perform hard delete. No changes needed to them.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py::TestPurgeMethods -v`
Expected: all 5 tests PASS

- [ ] **Step 5: Run full suite**

Run: `pytest tests/ -v`
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add hugo_memex/db.py tests/test_db.py
git commit -m "feat: add find_archived_* helpers for CLI purge"
```

---

### Task 4: Writer functions for archive/restore/purge on disk

Replace `delete_marginalia_from_disk` with archive/restore/purge operations that edit YAML files in place.

**Files:**
- Modify: `hugo_memex/writer.py`
- Test: `tests/test_writer.py`

- [ ] **Step 1: Write failing tests for the new writer functions**

Add to `tests/test_writer.py`:

```python
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
        # Second call with a later timestamp should not overwrite
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_writer.py::TestArchiveMarginaliaOnDisk tests/test_writer.py::TestRestoreMarginaliaOnDisk tests/test_writer.py::TestPurgeMarginaliaFromDisk -v`
Expected: FAIL (functions don't exist)

- [ ] **Step 3: Add archive/restore/purge writer functions**

In `hugo_memex/writer.py`, remove the existing `delete_marginalia_from_disk` function and replace it with these three new functions (place them where `delete_marginalia_from_disk` was):

```python
def _read_marginalia_notes(yaml_path: Path) -> list[dict]:
    """Read and parse a marginalia YAML file. Returns empty list if missing or malformed."""
    if not yaml_path.exists():
        return []
    raw = yaml_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    return data if isinstance(data, list) else []


def _write_marginalia_notes(yaml_path: Path, notes: list[dict]) -> None:
    """Write a list of notes to a YAML file. Deletes the file if list is empty."""
    if not notes:
        if yaml_path.exists():
            yaml_path.unlink()
        return
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(
        yaml.dump(notes, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def archive_marginalia_on_disk(
    hugo_root: str, source_file: str, note_id: str, timestamp: str,
) -> dict:
    """Mark a marginalia note archived by adding archived_at to its YAML entry.

    Idempotent: if the note already has archived_at set, the existing value
    is preserved and status is "already_archived".

    Raises ValueError if the file or note is not found.
    """
    yaml_path = Path(hugo_root) / source_file
    if not yaml_path.exists():
        raise ValueError(f"Marginalia file not found: {source_file}")
    notes = _read_marginalia_notes(yaml_path)
    target = None
    for n in notes:
        if n.get("id") == note_id:
            target = n
            break
    if target is None:
        raise ValueError(f"Note {note_id} not found in {source_file}")
    if target.get("archived_at"):
        return {
            "id": note_id, "status": "already_archived",
            "archived_at": target["archived_at"],
        }
    target["archived_at"] = timestamp
    _write_marginalia_notes(yaml_path, notes)
    return {"id": note_id, "status": "archived", "archived_at": timestamp}


def restore_marginalia_on_disk(
    hugo_root: str, source_file: str, note_id: str,
) -> dict:
    """Remove the archived_at field from a marginalia note's YAML entry.

    No-op if the note is not currently archived (returns already_active).
    Raises ValueError if the file or note is not found.
    """
    yaml_path = Path(hugo_root) / source_file
    if not yaml_path.exists():
        raise ValueError(f"Marginalia file not found: {source_file}")
    notes = _read_marginalia_notes(yaml_path)
    target = None
    for n in notes:
        if n.get("id") == note_id:
            target = n
            break
    if target is None:
        raise ValueError(f"Note {note_id} not found in {source_file}")
    if not target.get("archived_at"):
        return {"id": note_id, "status": "already_active"}
    del target["archived_at"]
    _write_marginalia_notes(yaml_path, notes)
    return {"id": note_id, "status": "restored"}


def purge_marginalia_from_disk(
    hugo_root: str, source_file: str, note_id: str,
) -> dict:
    """Remove a marginalia note from its YAML file entirely (hard delete on disk).

    If the file becomes empty after removal, the file itself is deleted.
    Raises ValueError if the file or note is not found.
    """
    yaml_path = Path(hugo_root) / source_file
    if not yaml_path.exists():
        raise ValueError(f"Marginalia file not found: {source_file}")
    notes = _read_marginalia_notes(yaml_path)
    remaining = [n for n in notes if n.get("id") != note_id]
    if len(remaining) == len(notes):
        raise ValueError(f"Note {note_id} not found in {source_file}")
    _write_marginalia_notes(yaml_path, remaining)
    return {"id": note_id, "status": "purged"}
```

- [ ] **Step 4: Delete old `delete_marginalia_from_disk` tests**

In `tests/test_writer.py`, remove the `TestDeleteMarginaliaFromDisk` class entirely (its tests will be replaced by the new archive/restore/purge test classes). Also remove any `delete_marginalia_from_disk` import from the top of the test file.

- [ ] **Step 5: Run writer tests**

Run: `pytest tests/test_writer.py -v`
Expected: all tests PASS including the new archive/restore/purge ones

- [ ] **Step 6: Verify imports don't reference the removed function**

Run: `grep -n "delete_marginalia_from_disk" hugo_memex/ tests/ 2>/dev/null` (ignoring any matches in plan/spec markdown).

Expected: only matches are inside `hugo_memex/mcp.py` (which we'll fix in Task 6). No test file references should remain.

If tests reference it, update them now. If `mcp.py` still imports it, that's fine for this task; Task 6 handles the MCP layer.

- [ ] **Step 7: Run full suite**

Run: `pytest tests/ -v`
Expected: `test_writer.py` passes; `test_mcp.py` may fail because `mcp.py` still imports the removed function. That's OK; Task 6 fixes it. Verify writer and db tests pass.

- [ ] **Step 8: Commit**

```bash
git add hugo_memex/writer.py tests/test_writer.py
git commit -m "feat: replace delete_marginalia_from_disk with archive/restore/purge writers"
```

---

### Task 5: Indexer diff-based sync with archive/restore semantics

Rework the indexer cleanup phase so that missing files archive rather than delete, and returning files restore. Also switch marginalia indexing to a diff-based upsert that preserves archived_at state.

**Files:**
- Modify: `hugo_memex/indexer.py`
- Modify: `tests/test_indexer.py`
- Modify: `tests/fixtures/data/marginalia/post/test-post.yaml`

- [ ] **Step 1: Update the fixture to include an archived note**

Edit `tests/fixtures/data/marginalia/post/test-post.yaml` to add one note with `archived_at`:

```yaml
- id: mg-fixture00001
  created: 2026-04-15T10:00:00Z
  body: This fixture note references Python and SQLite integration.
- id: mg-fixture00002
  created: 2026-04-16T09:00:00Z
  body: Related to llm-memex://conversation/test-convo-123
- id: mg-fixture00003
  created: 2026-04-17T08:00:00Z
  archived_at: 2026-04-18T00:00:00Z
  body: An archived fixture note for indexer coverage.
```

- [ ] **Step 2: Write failing tests for indexer archive/restore**

Add to `tests/test_indexer.py`:

```python
class TestIndexerSoftDelete:
    def test_archived_yaml_note_is_archived_in_db(self, hugo_root, db):
        """A note with archived_at in YAML lands archived in the DB."""
        stats = index_content(str(hugo_root), db)
        assert stats["errors"] == []
        rows = db.execute_sql(
            "SELECT id, archived_at FROM marginalia WHERE id = ?",
            ("mg-fixture00003",),
        )
        assert rows[0]["archived_at"] == "2026-04-18T00:00:00Z"

    def test_missing_page_archives_instead_of_deleting(self, tmp_path, fixtures_dir):
        """When a .md file is removed, the page row is archived, not deleted."""
        import shutil
        site = tmp_path / "site"
        shutil.copytree(fixtures_dir, site)
        db = Database(":memory:")
        index_content(str(site), db)
        # Remove the page's file
        target = site / "content" / "post" / "test-post" / "index.md"
        target.unlink()
        target.parent.rmdir()
        # Re-index
        stats = index_content(str(site), db)
        assert stats["archived"] == 1
        # Row should still exist with archived_at set
        rows = db.execute_sql(
            "SELECT path, archived_at FROM pages WHERE path = ?",
            ("post/test-post/index.md",),
        )
        assert len(rows) == 1
        assert rows[0]["archived_at"] is not None
        db.close()

    def test_returning_page_is_restored(self, tmp_path, fixtures_dir):
        """When a previously-missing .md file returns, archived_at is cleared."""
        import shutil
        site = tmp_path / "site"
        shutil.copytree(fixtures_dir, site)
        db = Database(":memory:")
        index_content(str(site), db)
        target = site / "content" / "post" / "test-post" / "index.md"
        saved_content = target.read_text()
        target.unlink()
        target.parent.rmdir()
        index_content(str(site), db)  # archive
        # Put it back
        target.parent.mkdir(parents=True)
        target.write_text(saved_content)
        stats = index_content(str(site), db)
        assert stats["restored"] == 1
        rows = db.execute_sql(
            "SELECT archived_at FROM pages WHERE path = ?",
            ("post/test-post/index.md",),
        )
        assert rows[0]["archived_at"] is None
        db.close()

    def test_archive_is_idempotent(self, tmp_path, fixtures_dir):
        """Re-indexing an already-archived page does not re-archive."""
        import shutil
        site = tmp_path / "site"
        shutil.copytree(fixtures_dir, site)
        db = Database(":memory:")
        index_content(str(site), db)
        target = site / "content" / "post" / "test-post" / "index.md"
        target.unlink()
        target.parent.rmdir()
        s1 = index_content(str(site), db)
        s2 = index_content(str(site), db)
        assert s1["archived"] == 1
        assert s2["archived"] == 0
        db.close()

    def test_missing_marginalia_file_archives_all_its_notes(self, tmp_path, fixtures_dir):
        """When a marginalia YAML is removed, all its DB rows get archived_at set."""
        import shutil
        site = tmp_path / "site"
        shutil.copytree(fixtures_dir, site)
        db = Database(":memory:")
        index_content(str(site), db)
        # Remove the fixture marginalia file
        yaml_file = site / "data" / "marginalia" / "post" / "test-post.yaml"
        yaml_file.unlink()
        stats = index_content(str(site), db)
        assert stats["marginalia_archived"] >= 2  # fixture had 2 active + 1 pre-archived
        rows = db.execute_sql(
            "SELECT id, archived_at FROM marginalia "
            "WHERE id IN ('mg-fixture00001', 'mg-fixture00002')"
        )
        for r in rows:
            assert r["archived_at"] is not None
        db.close()

    def test_returning_marginalia_file_restores_notes(self, tmp_path, fixtures_dir):
        """When a removed marginalia YAML returns, its notes un-archive."""
        import shutil
        site = tmp_path / "site"
        shutil.copytree(fixtures_dir, site)
        db = Database(":memory:")
        index_content(str(site), db)
        yaml_file = site / "data" / "marginalia" / "post" / "test-post.yaml"
        saved = yaml_file.read_text()
        yaml_file.unlink()
        index_content(str(site), db)  # archive
        # Restore the file
        yaml_file.write_text(saved)
        stats = index_content(str(site), db, force=True)
        assert stats["marginalia_restored"] >= 2
        rows = db.execute_sql(
            "SELECT id, archived_at FROM marginalia "
            "WHERE id = 'mg-fixture00001'"
        )
        assert rows[0]["archived_at"] is None
        db.close()

    def test_manual_yaml_archived_at_edit_syncs_to_db(self, tmp_path, fixtures_dir):
        """Editing a YAML to add archived_at to a note marks the DB row archived."""
        import shutil
        import yaml as _yaml
        site = tmp_path / "site"
        shutil.copytree(fixtures_dir, site)
        db = Database(":memory:")
        index_content(str(site), db)
        # Confirm mg-fixture00001 starts active
        rows = db.execute_sql(
            "SELECT archived_at FROM marginalia WHERE id = 'mg-fixture00001'"
        )
        assert rows[0]["archived_at"] is None
        # Edit the YAML to add archived_at to that note
        yaml_file = site / "data" / "marginalia" / "post" / "test-post.yaml"
        notes = _yaml.safe_load(yaml_file.read_text())
        for n in notes:
            if n["id"] == "mg-fixture00001":
                n["archived_at"] = "2026-04-18T15:00:00Z"
        yaml_file.write_text(_yaml.dump(notes, sort_keys=False))
        # Re-index
        stats = index_content(str(site), db)
        assert stats["marginalia_archived"] == 1
        rows = db.execute_sql(
            "SELECT archived_at FROM marginalia WHERE id = 'mg-fixture00001'"
        )
        assert rows[0]["archived_at"] == "2026-04-18T15:00:00Z"
        db.close()

    def test_stats_keys_renamed(self, hugo_root, db):
        """The stats dict uses archived/restored keys, not removed."""
        stats = index_content(str(hugo_root), db)
        assert "archived" in stats
        assert "restored" in stats
        assert "marginalia_archived" in stats
        assert "marginalia_restored" in stats
        assert "removed" not in stats
        assert "marginalia_removed" not in stats
```

- [ ] **Step 3: Update existing indexer tests to match new stats keys**

Search for any existing tests asserting `stats["removed"]` or `stats["marginalia_removed"]` in `tests/test_indexer.py` and update them:
- Replace `stats["removed"]` with `stats["archived"]` where the semantic is "file was deleted".
- Replace `stats["marginalia_removed"]` with `stats["marginalia_archived"]`.

Run `grep -n 'removed\|marginalia_removed' tests/test_indexer.py` to find them, update each to the new key name.

- [ ] **Step 4: Run tests to verify they fail**

Run: `pytest tests/test_indexer.py -v`
Expected: FAIL (new stats keys don't exist, archive-on-missing behavior not implemented)

- [ ] **Step 5: Rewrite the indexer cleanup and marginalia sync**

In `hugo_memex/indexer.py`, replace the `index_content` function body. Below is the full replacement, showing the diff-based marginalia sync:

Locate this block (pages cleanup):

```python
    # Cleanup: remove pages that no longer exist on disk
    if not paths:
        indexed_paths = db.get_all_indexed_paths()
        removed_paths = indexed_paths - seen_paths
        for path in removed_paths:
            db.delete_page(path)
            db.delete_sync_state(path)
            stats["removed"] += 1
```

Replace with:

```python
    # Archive pages whose source file no longer exists on disk.
    # Restore pages whose source file has returned (archived_at cleared
    # when the same path re-indexes above via save_page, which leaves
    # archived_at untouched; we clear it here explicitly for clarity).
    if not paths:
        all_known_paths = db.get_all_indexed_paths()
        missing_paths = all_known_paths - seen_paths
        now_iso = _now_iso()
        for path in missing_paths:
            row = db.execute_sql(
                "SELECT archived_at FROM pages WHERE path = ?", (path,)
            )
            if row and row[0]["archived_at"] is None:
                db.archive_page(path, now_iso)
                stats["archived"] += 1
            # Missing but already archived: no-op (idempotent)
        # Restore pages whose file is back on disk.
        for path in seen_paths:
            row = db.execute_sql(
                "SELECT archived_at FROM pages WHERE path = ?", (path,)
            )
            if row and row[0]["archived_at"] is not None:
                db.restore_page(path)
                stats["restored"] += 1
```

Also initialize the new stats keys at the top of `index_content`. Locate:

```python
    stats: dict[str, Any] = {
        "indexed": 0, "unchanged": 0, "removed": 0, "errors": [],
        "marginalia_indexed": 0, "marginalia_unchanged": 0,
        "marginalia_removed": 0,
    }
```

Replace with:

```python
    stats: dict[str, Any] = {
        "indexed": 0, "unchanged": 0, "archived": 0, "restored": 0,
        "errors": [],
        "marginalia_indexed": 0, "marginalia_unchanged": 0,
        "marginalia_archived": 0, "marginalia_restored": 0,
    }
```

Then locate the marginalia processing loop (starts around `for yaml_file in marginalia_files:`). Replace the per-file body that does `db.delete_marginalia_by_source(rel_file)` followed by insert-all with a diff-based sync:

```python
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
            yaml_by_id = {}
            notes_in_file = 0
            now_iso = _now_iso()
            for note in notes:
                note_id = note.get("id")
                body = note.get("body")
                if not note_id or not body:
                    continue
                yaml_by_id[note_id] = note
                created_at = note.get("created", now_iso)
                db.save_marginalia({
                    "id": note_id,
                    "page_path": page_path,
                    "body": body,
                    "created_at": created_at,
                    "source_file": rel_file,
                    "archived_at": note.get("archived_at"),
                })
                notes_in_file += 1

                prev = existing_by_id.get(note_id)
                if prev is not None:
                    was_archived = prev["archived_at"] is not None
                    now_archived = note.get("archived_at") is not None
                    if not was_archived and now_archived:
                        stats["marginalia_archived"] += 1
                    elif was_archived and not now_archived:
                        stats["marginalia_restored"] += 1

            # Notes in DB but not in YAML: archive them.
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
```

Finally, replace the marginalia cleanup block. Locate:

```python
    if not paths:
        known_sources = db.get_all_marginalia_source_files()
        removed_sources = known_sources - seen_marginalia_sources
        for source in removed_sources:
            db.delete_marginalia_by_source(source)
            db.delete_sync_state(source)
            stats["marginalia_removed"] += 1
```

Replace with:

```python
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
```

- [ ] **Step 6: Run indexer tests**

Run: `pytest tests/test_indexer.py -v`
Expected: all tests PASS, including the new `TestIndexerSoftDelete`

- [ ] **Step 7: Run full suite**

Run: `pytest tests/ -v`
Expected: `test_db.py`, `test_indexer.py`, `test_writer.py` all PASS. `test_mcp.py` may still fail due to stale imports from Task 4; Task 6 fixes those.

- [ ] **Step 8: Commit**

```bash
git add hugo_memex/indexer.py tests/test_indexer.py tests/fixtures/data/marginalia/post/test-post.yaml
git commit -m "feat: indexer archives on missing, restores on return, diff-syncs marginalia"
```

---

### Task 6: MCP tool changes (soft delete default, restore tool, include_archived)

Update `delete_marginalia` to soft-delete by default with `purge=True` opt-in, add `restore_marginalia`, add `include_archived` params to read tools.

**Files:**
- Modify: `hugo_memex/mcp.py`
- Test: `tests/test_mcp.py`

- [ ] **Step 1: Write failing tests for MCP changes**

Add to `tests/test_mcp.py`:

```python
class TestDeleteMarginaliaSoftDefault:
    @pytest.mark.asyncio
    async def test_delete_default_archives(self, writable_mcp_server):
        server, site = writable_mcp_server
        add_fn = await _get_tool_fn(server, "add_marginalia")
        r = add_fn(page_path="post/test-post/index.md", body="to archive via MCP")
        del_fn = await _get_tool_fn(server, "delete_marginalia")
        result = del_fn(id=r["id"])
        assert result["status"] == "archived"
        # YAML should still contain the note, but with archived_at set
        import yaml as _yaml
        notes = _yaml.safe_load(
            (site / r["source_file"]).read_text()
        )
        target = next(n for n in notes if n["id"] == r["id"])
        assert "archived_at" in target
        # DB should also reflect archived_at synchronously (no rebuild required)
        db = server._test_db
        rows = db.execute_sql(
            "SELECT archived_at FROM marginalia WHERE id = ?", (r["id"],)
        )
        assert rows[0]["archived_at"] is not None

    @pytest.mark.asyncio
    async def test_delete_with_purge_removes(self, writable_mcp_server):
        server, site = writable_mcp_server
        add_fn = await _get_tool_fn(server, "add_marginalia")
        r = add_fn(page_path="post/test-post/index.md", body="to purge via MCP")
        del_fn = await _get_tool_fn(server, "delete_marginalia")
        result = del_fn(id=r["id"], purge=True)
        assert result["status"] == "purged"
        # YAML should no longer contain the note
        import yaml as _yaml
        notes = _yaml.safe_load(
            (site / r["source_file"]).read_text()
        )
        ids = [n["id"] for n in notes]
        assert r["id"] not in ids
        # DB row removed
        db = server._test_db
        rows = db.execute_sql(
            "SELECT 1 FROM marginalia WHERE id = ?", (r["id"],)
        )
        assert rows == []

    @pytest.mark.asyncio
    async def test_delete_already_archived_is_noop(self, writable_mcp_server):
        server, site = writable_mcp_server
        add_fn = await _get_tool_fn(server, "add_marginalia")
        r = add_fn(page_path="post/test-post/index.md", body="noop delete")
        del_fn = await _get_tool_fn(server, "delete_marginalia")
        del_fn(id=r["id"])
        result = del_fn(id=r["id"])
        assert result["status"] == "already_archived"


class TestRestoreMarginaliaTool:
    @pytest.mark.asyncio
    async def test_restore_archived_note(self, writable_mcp_server):
        server, site = writable_mcp_server
        add_fn = await _get_tool_fn(server, "add_marginalia")
        r = add_fn(page_path="post/test-post/index.md", body="restore me")
        del_fn = await _get_tool_fn(server, "delete_marginalia")
        del_fn(id=r["id"])
        restore_fn = await _get_tool_fn(server, "restore_marginalia")
        result = restore_fn(id=r["id"])
        assert result["status"] == "restored"
        db = server._test_db
        rows = db.execute_sql(
            "SELECT archived_at FROM marginalia WHERE id = ?", (r["id"],)
        )
        assert rows[0]["archived_at"] is None

    @pytest.mark.asyncio
    async def test_restore_already_active_is_noop(self, writable_mcp_server):
        server, site = writable_mcp_server
        add_fn = await _get_tool_fn(server, "add_marginalia")
        r = add_fn(page_path="post/test-post/index.md", body="active")
        restore_fn = await _get_tool_fn(server, "restore_marginalia")
        result = restore_fn(id=r["id"])
        assert result["status"] == "already_active"

    @pytest.mark.asyncio
    async def test_restore_nonexistent_raises(self, writable_mcp_server):
        server, site = writable_mcp_server
        restore_fn = await _get_tool_fn(server, "restore_marginalia")
        with pytest.raises(Exception):
            restore_fn(id="mg-nonexistent")


class TestGetMarginaliaIncludeArchived:
    @pytest.mark.asyncio
    async def test_default_excludes_archived(self, writable_mcp_server):
        server, site = writable_mcp_server
        add_fn = await _get_tool_fn(server, "add_marginalia")
        r_active = add_fn(page_path="post/test-post/index.md", body="active")
        r_archived = add_fn(page_path="post/test-post/index.md", body="will-archive")
        del_fn = await _get_tool_fn(server, "delete_marginalia")
        del_fn(id=r_archived["id"])
        get_fn = await _get_tool_fn(server, "get_marginalia")
        result = get_fn(page_path="post/test-post/index.md")
        ids = {r["id"] for r in result}
        assert r_active["id"] in ids
        assert r_archived["id"] not in ids

    @pytest.mark.asyncio
    async def test_include_archived_returns_all(self, writable_mcp_server):
        server, site = writable_mcp_server
        add_fn = await _get_tool_fn(server, "add_marginalia")
        r_active = add_fn(page_path="post/test-post/index.md", body="active")
        r_archived = add_fn(page_path="post/test-post/index.md", body="will-archive-2")
        del_fn = await _get_tool_fn(server, "delete_marginalia")
        del_fn(id=r_archived["id"])
        get_fn = await _get_tool_fn(server, "get_marginalia")
        result = get_fn(page_path="post/test-post/index.md", include_archived=True)
        ids = {r["id"] for r in result}
        assert r_active["id"] in ids
        assert r_archived["id"] in ids


class TestGetPagesIncludeArchived:
    @pytest.mark.asyncio
    async def test_default_excludes_archived(self, writable_mcp_server):
        server, site = writable_mcp_server
        db = server._test_db
        db.archive_page("post/test-post/index.md", "2026-04-18T12:00:00Z")
        get_fn = await _get_tool_fn(server, "get_pages")
        result = get_fn(section="post")
        paths = {p["path"] for p in result}
        assert "post/test-post/index.md" not in paths

    @pytest.mark.asyncio
    async def test_include_archived(self, writable_mcp_server):
        server, site = writable_mcp_server
        db = server._test_db
        db.archive_page("post/test-post/index.md", "2026-04-18T12:00:00Z")
        get_fn = await _get_tool_fn(server, "get_pages")
        result = get_fn(section="post", include_archived=True)
        paths = {p["path"] for p in result}
        assert "post/test-post/index.md" in paths


class TestServerRegistration:
    @pytest.mark.asyncio
    async def test_restore_marginalia_registered(self, mcp_server):
        tools = await mcp_server.get_tools()
        tool_names = set(tools.keys()) if isinstance(tools, dict) else {t.name for t in tools}
        assert "restore_marginalia" in tool_names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp.py::TestDeleteMarginaliaSoftDefault tests/test_mcp.py::TestRestoreMarginaliaTool tests/test_mcp.py::TestGetMarginaliaIncludeArchived tests/test_mcp.py::TestGetPagesIncludeArchived tests/test_mcp.py::TestServerRegistration -v`
Expected: FAIL (new tool not registered, old delete hard-deletes, no include_archived param)

- [ ] **Step 3: Rewrite delete_marginalia and add restore_marginalia**

In `hugo_memex/mcp.py`, locate the existing `delete_marginalia` tool definition. Replace it with this version (now accepting a `purge` param and calling the writer's archive or purge function):

```python
    @mcp.tool()
    def delete_marginalia(
        id: Annotated[
            str,
            Field(description="Marginalia note ID (e.g. 'mg-abc123def456')"),
        ],
        purge: Annotated[
            bool,
            Field(description="If True, hard-delete the note (remove from YAML and DB). Default False archives the note instead."),
        ] = False,
        ctx: Context | None = None,
    ) -> dict:
        """Delete a marginalia note.

Default behavior is soft delete (archive): adds archived_at to the YAML entry
and updates the DB row. The note is still on disk and still indexed but
hidden from default get_marginalia calls.

With purge=True, removes the note entirely from YAML and DB. The DB row
and FTS entry are hard-deleted. Use with caution: this breaks URI stability.
"""
        config = _get_config(mcp, ctx)
        hugo_root = config.get("hugo_root")
        if not hugo_root:
            raise ToolError("hugo_root not configured")
        database = _get_db(mcp, ctx)
        rows = database.execute_sql(
            "SELECT source_file, archived_at FROM marginalia WHERE id = ?",
            (id,),
        )
        if not rows:
            raise ToolError(f"Marginalia note not found: {id}")
        source_file = rows[0]["source_file"]

        if purge:
            from hugo_memex.writer import purge_marginalia_from_disk
            try:
                purge_marginalia_from_disk(hugo_root, source_file, id)
            except (ValueError, FileNotFoundError) as e:
                raise ToolError(str(e))
            database.delete_marginalia(id)
            return {"id": id, "status": "purged"}

        # Soft delete (archive)
        if rows[0]["archived_at"] is not None:
            return {"id": id, "status": "already_archived"}
        from hugo_memex.writer import archive_marginalia_on_disk
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            archive_marginalia_on_disk(hugo_root, source_file, id, now_iso)
        except (ValueError, FileNotFoundError) as e:
            raise ToolError(str(e))
        database.archive_marginalia(id, now_iso)
        return {"id": id, "status": "archived", "archived_at": now_iso}
```

Then add the new `restore_marginalia` tool right after `delete_marginalia`:

```python
    @mcp.tool()
    def restore_marginalia(
        id: Annotated[
            str,
            Field(description="Marginalia note ID to restore (e.g. 'mg-abc123def456')"),
        ],
        ctx: Context | None = None,
    ) -> dict:
        """Restore an archived marginalia note.

Removes the archived_at field from the YAML entry and clears the DB row's
archived_at synchronously. No-op (returns already_active) if the note is
not currently archived.
"""
        config = _get_config(mcp, ctx)
        hugo_root = config.get("hugo_root")
        if not hugo_root:
            raise ToolError("hugo_root not configured")
        database = _get_db(mcp, ctx)
        rows = database.execute_sql(
            "SELECT source_file, archived_at FROM marginalia WHERE id = ?",
            (id,),
        )
        if not rows:
            raise ToolError(f"Marginalia note not found: {id}")
        if rows[0]["archived_at"] is None:
            return {"id": id, "status": "already_active"}
        source_file = rows[0]["source_file"]
        from hugo_memex.writer import restore_marginalia_on_disk
        try:
            restore_marginalia_on_disk(hugo_root, source_file, id)
        except (ValueError, FileNotFoundError) as e:
            raise ToolError(str(e))
        database.restore_marginalia_row(id)
        return {"id": id, "status": "restored"}
```

- [ ] **Step 4: Update get_marginalia tool to accept include_archived**

In `hugo_memex/mcp.py`, locate the existing `get_marginalia` tool. Replace its signature and body with:

```python
    @mcp.tool(annotations={"readOnlyHint": True})
    def get_marginalia(
        page_path: Annotated[
            str,
            Field(description="Content path relative to content/"),
        ],
        include_archived: Annotated[
            bool,
            Field(description="If True, include archived notes. Default False returns only active notes."),
        ] = False,
        ctx: Context | None = None,
    ) -> list[dict]:
        """Get all marginalia notes for a page.

By default excludes archived notes. Pass include_archived=True to see
the full history, including soft-deleted notes.
"""
        database = _get_db(mcp, ctx)
        return database.get_marginalia(page_path, include_archived=include_archived)
```

- [ ] **Step 5: Update get_pages to accept include_archived**

In `hugo_memex/mcp.py`, locate the `get_pages` tool. Add an `include_archived` parameter between `include_drafts` and `limit`, and add a WHERE clause for `p.archived_at IS NULL` when the flag is False.

Find the parameter block and add:

```python
        include_archived: Annotated[
            bool,
            Field(description="Include archived pages (default false)"),
        ] = False,
```

Locate the existing line:

```python
        if not include_drafts:
            conds.append("p.draft = 0")
```

Add right after it:

```python
        if not include_archived:
            conds.append("p.archived_at IS NULL")
```

- [ ] **Step 6: Run MCP tests**

Run: `pytest tests/test_mcp.py -v`
Expected: all MCP tests PASS, including the new classes.

- [ ] **Step 7: Run full suite**

Run: `pytest tests/ -v`
Expected: all tests PASS across db, writer, indexer, mcp, integration.

- [ ] **Step 8: Commit**

```bash
git add hugo_memex/mcp.py tests/test_mcp.py
git commit -m "feat: MCP soft delete default, restore_marginalia tool, include_archived on reads"
```

---

### Task 7: Schema resource docs + CLAUDE.md updates

Update the schema resource so LLMs querying `execute_sql` know how to filter archived records, and update CLAUDE.md to reflect v3 schema.

**Files:**
- Modify: `hugo_memex/db.py` (the `get_schema` method's docs string and the `execute_sql` docstring example queries)
- Modify: `hugo_memex/mcp.py` (execute_sql docstring example queries)
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add Archived Records section to get_schema docs**

In `hugo_memex/db.py`, locate the `get_schema` method. At the end of the `docs` string (before the closing `"""`), append:

```
-- ══ Archived Records (soft delete) ═════════════════════════════
-- All record tables use soft delete: archived_at IS NULL means active.
-- Default queries should filter archived rows unless you want history.
--
-- Active pages only:
--   SELECT path, title FROM pages
--   WHERE archived_at IS NULL AND draft = 0
--   ORDER BY date DESC
--
-- Recently archived pages (last 30 days):
--   SELECT path, archived_at FROM pages
--   WHERE archived_at IS NOT NULL
--     AND date(archived_at) > date('now', '-30 days')
--   ORDER BY archived_at DESC
--
-- Archived marginalia for a page (history view):
--   SELECT id, body, created_at, archived_at FROM marginalia
--   WHERE page_path = ? AND archived_at IS NOT NULL
--   ORDER BY archived_at DESC
--
-- Count active vs archived per section:
--   SELECT section,
--          SUM(CASE WHEN archived_at IS NULL THEN 1 ELSE 0 END) as active,
--          SUM(CASE WHEN archived_at IS NOT NULL THEN 1 ELSE 0 END) as archived
--   FROM pages GROUP BY section ORDER BY section
```

- [ ] **Step 2: Update existing query examples in get_schema docs to filter archived**

In `hugo_memex/db.py`'s `get_schema` method, find these lines in the existing docs:

```
-- List recent posts:
--   SELECT path, title, date, section FROM pages
--   WHERE kind = 'page' AND draft = 0
--   ORDER BY date DESC LIMIT 20
```

Update to:

```
-- List recent posts:
--   SELECT path, title, date, section FROM pages
--   WHERE kind = 'page' AND draft = 0 AND archived_at IS NULL
--   ORDER BY date DESC LIMIT 20
```

Find:

```
-- Pages by tag:
--   SELECT p.path, p.title, p.date FROM pages p
--   JOIN taxonomies t ON p.path = t.page_path
--   WHERE t.taxonomy = 'tags' AND t.term = 'python'
```

Update to:

```
-- Pages by tag:
--   SELECT p.path, p.title, p.date FROM pages p
--   JOIN taxonomies t ON p.path = t.page_path
--   WHERE t.taxonomy = 'tags' AND t.term = 'python' AND p.archived_at IS NULL
```

- [ ] **Step 3: Update execute_sql docstring in mcp.py**

In `hugo_memex/mcp.py`, locate the `execute_sql` tool's docstring. Make the same two updates in its "Common queries" section (the "List recent posts" and "Pages by tag" examples).

- [ ] **Step 4: Update CLAUDE.md**

In `CLAUDE.md`, in the "Schema" section, add a line after the existing schema descriptions noting archived_at:

Find:

```
- **marginalia_fts**: FTS5 virtual table over marginalia body text.
```

Add after:

```
- **Soft delete**: `pages.archived_at` and `marginalia.archived_at` are `NULL` for active records; any ISO timestamp means archived. Default MCP reads and query examples filter `archived_at IS NULL`.
```

In the "Conventions" section, add:

```
- Missing source files archive rather than delete. Hard delete is CLI-only via `hugo-memex purge --missing` or `--archived-before`.
```

- [ ] **Step 5: Run full test suite to ensure schema resource tests still pass**

Run: `pytest tests/ -v`
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add hugo_memex/db.py hugo_memex/mcp.py CLAUDE.md
git commit -m "docs: schema resource + CLAUDE.md updated for soft delete"
```

---

### Task 8: CLI purge subcommand

Add `hugo-memex purge --missing | --archived-before <date> [--dry-run]`.

**Files:**
- Modify: `hugo_memex/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests for the purge CLI command**

Add to `tests/test_cli.py`:

```python
import json
import subprocess
import sys
from pathlib import Path


class TestPurgeCLI:
    def _setup_site(self, tmp_path, fixtures_dir, config_path):
        """Copy fixtures to tmp, build a config, return paths."""
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
        assert "filter" in (result.stderr + result.stdout).lower() \
            or "missing" in (result.stderr + result.stdout).lower()

    def test_purge_missing_purges_archived_missing_pages(self, tmp_path, fixtures_dir):
        config_path = tmp_path / "config.yaml"
        site, db_path = self._setup_site(tmp_path, fixtures_dir, config_path)
        # Index, then remove a .md so it archives on next index
        self._run_cli(config_path, "index")
        (site / "content" / "post" / "test-post" / "index.md").unlink()
        (site / "content" / "post" / "test-post").rmdir()
        self._run_cli(config_path, "index")
        # Confirm archive happened
        from hugo_memex.db import Database
        db = Database(str(db_path))
        rows = db.execute_sql(
            "SELECT archived_at FROM pages WHERE path = ?",
            ("post/test-post/index.md",),
        )
        assert rows and rows[0]["archived_at"] is not None
        db.close()
        # Purge missing
        result = self._run_cli(config_path, "purge", "--missing")
        assert result.returncode == 0
        # Verify row is gone
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
        # Directly insert an old archived page via SQL
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
        # Purge anything archived before 2026-01-01
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
        # The archived row should still exist
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
        # Remove the whole marginalia YAML file
        yaml_file = site / "data" / "marginalia" / "post" / "test-post.yaml"
        yaml_file.unlink()
        self._run_cli(config_path, "index")
        # Purge missing
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py::TestPurgeCLI -v`
Expected: FAIL (no purge subcommand)

- [ ] **Step 3: Add the purge subcommand to cli.py**

In `hugo_memex/cli.py`, in the `_make_parser` function, add after the existing `sql` subparser and before the `mcp` subparser:

```python
    # purge
    pg = sub.add_parser(
        "purge",
        help="Hard-delete archived records (pages and marginalia)",
    )
    pg.add_argument(
        "--missing", action="store_true",
        help="Purge archived records whose source file is gone",
    )
    pg.add_argument(
        "--archived-before",
        help="Purge archived records whose archived_at is older than this ISO date",
    )
    pg.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be purged without deleting",
    )
```

Add this function after `cmd_sql`:

```python
def cmd_purge(args):
    from pathlib import Path
    config, db = _load(args.config)

    if not args.missing and not args.archived_before:
        print(
            "Error: purge requires at least one filter "
            "(--missing or --archived-before).",
            file=sys.stderr,
        )
        db.close()
        sys.exit(2)

    hugo_root = Path(config["hugo_root"])

    pages_to_purge: set[str] = set()
    marginalia_to_purge: list[dict] = []

    if args.missing:
        for path in db.find_all_archived_pages():
            content_file = hugo_root / "content" / path
            if not content_file.exists():
                pages_to_purge.add(path)
        for row in db.find_all_archived_marginalia():
            yaml_file = hugo_root / row["source_file"]
            if not yaml_file.exists():
                marginalia_to_purge.append(row)

    if args.archived_before:
        for path in db.find_archived_pages_before(args.archived_before):
            pages_to_purge.add(path)
        seen_ids = {m["id"] for m in marginalia_to_purge}
        for row in db.find_archived_marginalia_before(args.archived_before):
            if row["id"] not in seen_ids:
                marginalia_to_purge.append(row)

    if args.dry_run:
        print(f"Would purge {len(pages_to_purge)} pages:")
        for p in sorted(pages_to_purge):
            print(f"  {p}")
        print(f"Would purge {len(marginalia_to_purge)} marginalia notes:")
        for m in marginalia_to_purge:
            print(f"  {m['id']} (in {m['source_file']})")
        db.close()
        return

    # Perform the purge
    from hugo_memex.writer import purge_marginalia_from_disk

    for path in pages_to_purge:
        db.delete_page(path)
        db.delete_sync_state(path)

    for m in marginalia_to_purge:
        yaml_file = hugo_root / m["source_file"]
        if yaml_file.exists():
            try:
                purge_marginalia_from_disk(
                    str(hugo_root), m["source_file"], m["id"],
                )
            except (ValueError, FileNotFoundError):
                # Already gone from YAML; just clean the DB row
                pass
        db.delete_marginalia(m["id"])

    print(
        f"Purged {len(pages_to_purge)} pages, "
        f"{len(marginalia_to_purge)} marginalia notes."
    )
    db.close()
```

Register the command in the `commands` dict in `main`:

```python
    commands = {
        "index": cmd_index,
        "stats": cmd_stats,
        "search": cmd_search,
        "sql": cmd_sql,
        "purge": cmd_purge,
        "mcp": cmd_mcp,
    }
```

- [ ] **Step 4: Update cmd_index output to show new stats keys**

In `hugo_memex/cli.py`, update the `cmd_index` function's print statement. Replace:

```python
    print(
        f"Indexed: {stats['indexed']}, "
        f"Unchanged: {stats['unchanged']}, "
        f"Removed: {stats['removed']}"
    )
```

With:

```python
    print(
        f"Indexed: {stats['indexed']}, "
        f"Unchanged: {stats['unchanged']}, "
        f"Archived: {stats['archived']}, "
        f"Restored: {stats['restored']}"
    )
    print(
        f"Marginalia: indexed {stats['marginalia_indexed']}, "
        f"unchanged {stats['marginalia_unchanged']}, "
        f"archived {stats['marginalia_archived']}, "
        f"restored {stats['marginalia_restored']}"
    )
```

- [ ] **Step 5: Run CLI tests**

Run: `pytest tests/test_cli.py -v`
Expected: all tests PASS

- [ ] **Step 6: Run full suite**

Run: `pytest tests/ -v`
Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
git add hugo_memex/cli.py tests/test_cli.py
git commit -m "feat: add hugo-memex purge subcommand (--missing, --archived-before, --dry-run)"
```

---

### Task 9: Full lifecycle integration test

End-to-end test covering add → archive → hidden-by-default → restore → archive again → purge → gone.

**Files:**
- Modify: `tests/test_integration.py`

- [ ] **Step 1: Write the integration test**

Add to `tests/test_integration.py`:

```python
class TestSoftDeleteLifecycle:
    def test_full_lifecycle(self, tmp_path, fixtures_dir):
        """End-to-end: add, archive (via MCP), hidden by default, restore, purge."""
        import shutil
        from hugo_memex.db import Database
        from hugo_memex.indexer import index_content
        from hugo_memex.writer import (
            add_marginalia, archive_marginalia_on_disk,
            restore_marginalia_on_disk, purge_marginalia_from_disk,
        )

        site = tmp_path / "site"
        shutil.copytree(fixtures_dir, site)
        db = Database(":memory:")
        index_content(str(site), db)

        # Phase 1: Add a note, verify it's active
        r = add_marginalia(str(site), "post/test-post/index.md", "lifecycle-test-note")
        index_content(str(site), db)
        rows = db.get_marginalia("post/test-post/index.md")
        assert any(n["id"] == r["id"] for n in rows)

        # Phase 2: Archive via writer, verify hidden in default get, visible with include_archived
        archive_marginalia_on_disk(
            str(site), r["source_file"], r["id"], "2026-04-18T12:00:00Z",
        )
        index_content(str(site), db)
        active = db.get_marginalia("post/test-post/index.md")
        assert not any(n["id"] == r["id"] for n in active)
        all_notes = db.get_marginalia("post/test-post/index.md", include_archived=True)
        assert any(n["id"] == r["id"] for n in all_notes)

        # Phase 3: Restore, verify active again
        restore_marginalia_on_disk(str(site), r["source_file"], r["id"])
        index_content(str(site), db, force=True)
        active = db.get_marginalia("post/test-post/index.md")
        assert any(n["id"] == r["id"] for n in active)

        # Phase 4: Archive by removing the source .md file (page-level archive)
        md = site / "content" / "post" / "test-post" / "index.md"
        md.unlink()
        md.parent.rmdir()
        stats = index_content(str(site), db)
        assert stats["archived"] == 1
        rows = db.execute_sql(
            "SELECT archived_at FROM pages WHERE path = ?",
            ("post/test-post/index.md",),
        )
        assert rows[0]["archived_at"] is not None

        # Phase 5: Purge the archived note via writer, verify gone
        archive_marginalia_on_disk(
            str(site), r["source_file"], r["id"], "2026-04-18T13:00:00Z",
        )
        purge_marginalia_from_disk(str(site), r["source_file"], r["id"])
        db.delete_marginalia(r["id"])
        rows = db.execute_sql(
            "SELECT 1 FROM marginalia WHERE id = ?", (r["id"],)
        )
        assert rows == []

        db.close()
```

- [ ] **Step 2: Run the integration test**

Run: `pytest tests/test_integration.py::TestSoftDeleteLifecycle -v`
Expected: PASS

- [ ] **Step 3: Run full suite with coverage**

Run: `pytest tests/ -v --cov=hugo_memex`
Expected: all tests PASS, coverage on new soft-delete code paths is high (>85%).

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: full lifecycle integration test for soft delete"
```

---

## Notes

- `rebuild_index` MCP tool is intentionally unchanged except for its stats keys (updated in Task 5). No `purge` flag is added to the MCP layer; hard delete is CLI-only per the spec.
- The `save_marginalia` signature accepts an optional `archived_at` key in the note dict. All callers that don't care about archived state can omit it (treated as NULL).
- The indexer's diff-based sync for marginalia replaces the prior "delete-then-reinsert" pattern. This preserves archived_at state when the YAML hasn't explicitly changed it.
