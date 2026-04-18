# Marginalia Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add marginalia (free-form notes attached to Hugo content pages) stored as YAML files under `data/marginalia/`, indexed into SQLite, and accessible via MCP tools.

**Architecture:** Marginalia lives on the filesystem in `data/marginalia/*.yaml`, following Hugo's data directory convention. The indexer discovers and indexes them into a `marginalia` table + FTS5 virtual table. Three new MCP tools (add, get, delete) follow the existing two-phase write pattern: modify files on disk, then rebuild_index to update the DB.

**Tech Stack:** Python 3.11+, sqlite3, PyYAML, FastMCP v2, pytest + pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-04-16-marginalia-design.md`

---

### Task 1: Schema changes (db.py)

Add the marginalia table, FTS5 virtual table, index, and v1-to-v2 migration.

**Files:**
- Modify: `hugo_memex/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write failing test for marginalia table existence**

In `tests/test_db.py`, add:

```python
class TestMarginaliaSchema:
    def test_marginalia_table_exists(self, db):
        tables = {
            r["name"]
            for r in db.execute_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "marginalia" in tables

    def test_marginalia_fts_exists(self, db):
        tables = {
            r["name"]
            for r in db.execute_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "marginalia_fts" in tables

    def test_marginalia_insert_and_query(self, db):
        db.conn.execute(
            "INSERT INTO marginalia (id, page_path, body, created_at, source_file) "
            "VALUES (?, ?, ?, ?, ?)",
            ("mg-test123", "post/test/index.md", "A note", "2026-04-16T12:00:00Z",
             "data/marginalia/post/test.yaml"),
        )
        db.conn.commit()
        rows = db.execute_sql(
            "SELECT * FROM marginalia WHERE id = ?", ("mg-test123",)
        )
        assert len(rows) == 1
        assert rows[0]["body"] == "A note"
        assert rows[0]["page_path"] == "post/test/index.md"

    def test_marginalia_fts_search(self, db):
        db.conn.execute(
            "INSERT INTO marginalia (id, page_path, body, created_at, source_file) "
            "VALUES (?, ?, ?, ?, ?)",
            ("mg-fts1", "post/test/index.md", "Python programming notes",
             "2026-04-16T12:00:00Z", "data/marginalia/post/test.yaml"),
        )
        db.conn.execute(
            "INSERT INTO marginalia_fts (id, body) VALUES (?, ?)",
            ("mg-fts1", "Python programming notes"),
        )
        db.conn.commit()
        rows = db.execute_sql(
            "SELECT id FROM marginalia_fts WHERE marginalia_fts MATCH 'Python'"
        )
        assert len(rows) == 1
        assert rows[0]["id"] == "mg-fts1"

    def test_marginalia_no_fk_to_pages(self, db):
        """Marginalia can exist without a matching page (orphan survival)."""
        db.conn.execute(
            "INSERT INTO marginalia (id, page_path, body, created_at, source_file) "
            "VALUES (?, ?, ?, ?, ?)",
            ("mg-orphan", "nonexistent/page/index.md", "Orphan note",
             "2026-04-16T12:00:00Z", "data/marginalia/nonexistent/page.yaml"),
        )
        db.conn.commit()
        rows = db.execute_sql("SELECT id FROM marginalia WHERE id = 'mg-orphan'")
        assert len(rows) == 1

    def test_marginalia_null_page_path(self, db):
        """page_path can be NULL."""
        db.conn.execute(
            "INSERT INTO marginalia (id, page_path, body, created_at, source_file) "
            "VALUES (?, ?, ?, ?, ?)",
            ("mg-null", None, "Detached note",
             "2026-04-16T12:00:00Z", "data/marginalia/orphan.yaml"),
        )
        db.conn.commit()
        rows = db.execute_sql("SELECT id FROM marginalia WHERE page_path IS NULL")
        assert len(rows) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py::TestMarginaliaSchema -v`
Expected: FAIL (table "marginalia" does not exist)

- [ ] **Step 3: Add marginalia to SCHEMA_SQL and bump version**

In `hugo_memex/db.py`, append to the `SCHEMA_SQL` string (before the closing `"""`), after the existing `CREATE INDEX` statements:

```sql
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
```

Change `SCHEMA_VERSION = 1` to `SCHEMA_VERSION = 2`.

Add the v1-to-v2 migration function and register it:

```python
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


_MIGRATIONS: dict[int, Callable] = {
    1: _migrate_v1_to_v2,
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py::TestMarginaliaSchema -v`
Expected: all 6 tests PASS

- [ ] **Step 5: Write migration test**

Add to `tests/test_db.py`:

```python
class TestMigrationV1ToV2:
    def test_migration_adds_marginalia(self):
        """A v1 database upgrades to v2 with marginalia tables."""
        from hugo_memex.db import Database, _migrate_v1_to_v2
        db = Database(":memory:")
        # Downgrade to v1 by dropping marginalia tables
        db.conn.execute("DROP TABLE IF EXISTS marginalia_fts")
        db.conn.execute("DROP TABLE IF EXISTS marginalia")
        db.conn.execute("UPDATE schema_version SET version = 1")
        db.conn.commit()
        # Re-open triggers migration
        db2 = Database(":memory:")
        # Directly test migration on the downgraded db
        _migrate_v1_to_v2(db.conn)
        db.conn.execute("UPDATE schema_version SET version = 2")
        db.conn.commit()
        tables = {
            r["name"]
            for r in db.execute_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "marginalia" in tables
        assert "marginalia_fts" in tables
        db.close()
        db2.close()
```

- [ ] **Step 6: Run migration test**

Run: `pytest tests/test_db.py::TestMigrationV1ToV2 -v`
Expected: PASS

- [ ] **Step 7: Run full test suite to verify no regressions**

Run: `pytest tests/ -v`
Expected: all existing tests PASS

- [ ] **Step 8: Commit**

```bash
git add hugo_memex/db.py tests/test_db.py
git commit -m "feat: add marginalia schema (v2) with FTS5 and migration"
```

---

### Task 2: DB helper methods for marginalia (db.py)

Add methods for saving, querying, and deleting marginalia records.

**Files:**
- Modify: `hugo_memex/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write failing tests for DB marginalia methods**

Add to `tests/test_db.py`:

```python
class TestMarginaliaCRUD:
    def test_save_marginalia(self, db):
        db.save_marginalia({
            "id": "mg-save1",
            "page_path": "post/test/index.md",
            "body": "Test note",
            "created_at": "2026-04-16T12:00:00Z",
            "source_file": "data/marginalia/post/test.yaml",
        })
        rows = db.execute_sql("SELECT * FROM marginalia WHERE id = 'mg-save1'")
        assert len(rows) == 1
        assert rows[0]["body"] == "Test note"
        # FTS should be populated too
        fts = db.execute_sql(
            "SELECT id FROM marginalia_fts WHERE marginalia_fts MATCH 'Test'"
        )
        assert len(fts) == 1

    def test_get_marginalia_for_page(self, db):
        for i in range(3):
            db.save_marginalia({
                "id": f"mg-get{i}",
                "page_path": "post/test/index.md",
                "body": f"Note {i}",
                "created_at": f"2026-04-1{i}T12:00:00Z",
                "source_file": "data/marginalia/post/test.yaml",
            })
        # Different page
        db.save_marginalia({
            "id": "mg-other",
            "page_path": "post/other/index.md",
            "body": "Other note",
            "created_at": "2026-04-16T12:00:00Z",
            "source_file": "data/marginalia/post/other.yaml",
        })
        rows = db.get_marginalia("post/test/index.md")
        assert len(rows) == 3
        assert all(r["page_path"] == "post/test/index.md" for r in rows)

    def test_get_marginalia_empty(self, db):
        rows = db.get_marginalia("nonexistent/page.md")
        assert rows == []

    def test_delete_marginalia(self, db):
        db.save_marginalia({
            "id": "mg-del1",
            "page_path": "post/test/index.md",
            "body": "To be deleted",
            "created_at": "2026-04-16T12:00:00Z",
            "source_file": "data/marginalia/post/test.yaml",
        })
        assert db.delete_marginalia("mg-del1") is True
        rows = db.execute_sql("SELECT * FROM marginalia WHERE id = 'mg-del1'")
        assert len(rows) == 0
        # FTS should be cleaned up too
        fts = db.execute_sql(
            "SELECT id FROM marginalia_fts WHERE marginalia_fts MATCH 'deleted'"
        )
        assert len(fts) == 0

    def test_delete_marginalia_not_found(self, db):
        assert db.delete_marginalia("mg-nonexistent") is False

    def test_get_all_marginalia_source_files(self, db):
        db.save_marginalia({
            "id": "mg-src1",
            "page_path": "post/a/index.md",
            "body": "Note A",
            "created_at": "2026-04-16T12:00:00Z",
            "source_file": "data/marginalia/post/a.yaml",
        })
        db.save_marginalia({
            "id": "mg-src2",
            "page_path": "post/b/index.md",
            "body": "Note B",
            "created_at": "2026-04-16T12:00:00Z",
            "source_file": "data/marginalia/post/b.yaml",
        })
        files = db.get_all_marginalia_source_files()
        assert files == {"data/marginalia/post/a.yaml", "data/marginalia/post/b.yaml"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py::TestMarginaliaCRUD -v`
Expected: FAIL (AttributeError: 'Database' has no attribute 'save_marginalia')

- [ ] **Step 3: Implement DB methods**

Add to `hugo_memex/db.py` in the `Database` class, after the `get_all_indexed_paths` method:

```python
    # -- Marginalia ---------------------------------------------------

    def save_marginalia(self, note: dict[str, Any]) -> None:
        """Insert or replace a marginalia record and update FTS5."""
        self.conn.execute(
            "INSERT OR REPLACE INTO marginalia "
            "(id, page_path, body, created_at, source_file) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                note["id"], note.get("page_path"), note["body"],
                note["created_at"], note["source_file"],
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

    def get_marginalia(self, page_path: str) -> list[dict[str, Any]]:
        """Return all marginalia for a page, ordered by created_at."""
        return self.execute_sql(
            "SELECT * FROM marginalia WHERE page_path = ? "
            "ORDER BY created_at",
            (page_path,),
        )

    def delete_marginalia(self, note_id: str) -> bool:
        """Delete a marginalia record and its FTS entry. Returns True if found."""
        self.conn.execute(
            "DELETE FROM marginalia_fts WHERE id = ?", (note_id,)
        )
        cursor = self.conn.execute(
            "DELETE FROM marginalia WHERE id = ?", (note_id,)
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_all_marginalia_source_files(self) -> set[str]:
        """Return all distinct source_file paths in the marginalia table."""
        rows = self.execute_sql(
            "SELECT DISTINCT source_file FROM marginalia"
        )
        return {r["source_file"] for r in rows}

    def delete_marginalia_by_source(self, source_file: str) -> int:
        """Delete all marginalia from a given source file. Returns count deleted."""
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py::TestMarginaliaCRUD -v`
Expected: all 6 tests PASS

- [ ] **Step 5: Run full test suite**

Run: `pytest tests/ -v`
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add hugo_memex/db.py tests/test_db.py
git commit -m "feat: add marginalia CRUD methods to Database"
```

---

### Task 3: Path mapping and writer functions (writer.py)

Add the path mapping logic and filesystem write/delete functions for marginalia.

**Files:**
- Modify: `hugo_memex/writer.py`
- Test: `tests/test_writer.py`

- [ ] **Step 1: Write failing tests for path mapping**

Add to `tests/test_writer.py`:

```python
from hugo_memex.writer import (
    marginalia_path_for_page,
    page_path_for_marginalia,
    add_marginalia,
    delete_marginalia_from_disk,
)


class TestMarginaliaPathMapping:
    def test_leaf_bundle(self, writable_site):
        site, _ = writable_site
        result = marginalia_path_for_page(str(site), "post/test-post/index.md")
        expected = Path(site) / "data" / "marginalia" / "post" / "test-post.yaml"
        assert result == expected

    def test_standalone_file(self, writable_site):
        site, _ = writable_site
        result = marginalia_path_for_page(str(site), "media/test-book.md")
        expected = Path(site) / "data" / "marginalia" / "media" / "test-book.yaml"
        assert result == expected

    def test_root_index(self, writable_site):
        site, _ = writable_site
        result = marginalia_path_for_page(str(site), "_index.md")
        expected = Path(site) / "data" / "marginalia" / "_index.yaml"
        assert result == expected

    def test_reverse_mapping_leaf_bundle(self):
        assert page_path_for_marginalia("post/test-post.yaml") == "post/test-post/index.md"

    def test_reverse_mapping_standalone(self):
        # Default: assume leaf bundle
        assert page_path_for_marginalia("media/test-book.yaml") == "media/test-book/index.md"

    def test_reverse_mapping_root_index(self):
        assert page_path_for_marginalia("_index.yaml") == "_index.md"

    def test_path_traversal_rejected(self, writable_site):
        site, _ = writable_site
        with pytest.raises(ValueError, match="within"):
            marginalia_path_for_page(str(site), "../../etc/passwd")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_writer.py::TestMarginaliaPathMapping -v`
Expected: FAIL (ImportError: cannot import name 'marginalia_path_for_page')

- [ ] **Step 3: Implement path mapping functions**

Add to `hugo_memex/writer.py`, after the `_VALID_SECTION` regex:

```python
def _marginalia_id(page_path: str, body: str, created: str) -> str:
    """Generate a deterministic marginalia ID."""
    content = f"{page_path}\n{body}\n{created}"
    digest = hashlib.sha256(content.encode()).hexdigest()[:12]
    return f"mg-{digest}"


def marginalia_path_for_page(hugo_root: str, page_path: str) -> Path:
    """Compute the marginalia YAML file path for a given content page path.

    Mapping: strip index.md (bundles) or .md (standalone), append .yaml,
    place under data/marginalia/.
    """
    root = Path(hugo_root)
    content_root = root / "content"

    # Validate page_path stays within content/
    target = (content_root / page_path).resolve()
    if not target.is_relative_to(content_root.resolve()):
        raise ValueError(f"Path must be within content/: {page_path}")

    p = Path(page_path)
    if p.name == "index.md":
        # Leaf bundle: post/my-post/index.md -> post/my-post.yaml
        rel = p.parent
    elif p.name == "_index.md":
        # Section index at root: _index.md -> _index.yaml
        if len(p.parts) == 1:
            return root / "data" / "marginalia" / "_index.yaml"
        # Nested section: section/_index.md -> section/_index.yaml
        rel = p.parent / "_index"
    else:
        # Standalone: media/book.md -> media/book.yaml
        rel = p.with_suffix("")

    return root / "data" / "marginalia" / rel.with_suffix(".yaml")


def page_path_for_marginalia(marginalia_rel_path: str) -> str:
    """Reverse-map a marginalia file path to the best-guess content page path.

    marginalia_rel_path is relative to data/marginalia/.
    Default assumption: leaf bundle (stem/index.md).
    """
    p = Path(marginalia_rel_path)
    stem = p.with_suffix("")  # strip .yaml

    if stem.name == "_index":
        # Section index
        if len(stem.parts) == 1:
            return "_index.md"
        return str(stem.parent / "_index.md")

    # Default: assume leaf bundle
    return str(stem / "index.md")
```

- [ ] **Step 4: Run path mapping tests**

Run: `pytest tests/test_writer.py::TestMarginaliaPathMapping -v`
Expected: all 7 tests PASS

- [ ] **Step 5: Write failing tests for add/delete marginalia on disk**

Add to `tests/test_writer.py`:

```python
class TestAddMarginalia:
    def test_add_first_note(self, writable_site):
        site, _ = writable_site
        result = add_marginalia(str(site), "post/test-post/index.md", "First note")
        assert result["status"] == "created"
        assert result["id"].startswith("mg-")
        assert result["page_path"] == "post/test-post/index.md"

        # Verify file was written
        yaml_path = Path(site) / "data" / "marginalia" / "post" / "test-post.yaml"
        assert yaml_path.exists()
        import yaml as _yaml
        notes = _yaml.safe_load(yaml_path.read_text())
        assert len(notes) == 1
        assert notes[0]["body"] == "First note"
        assert notes[0]["id"] == result["id"]

    def test_add_second_note_appends(self, writable_site):
        site, _ = writable_site
        add_marginalia(str(site), "post/test-post/index.md", "First")
        add_marginalia(str(site), "post/test-post/index.md", "Second")

        yaml_path = Path(site) / "data" / "marginalia" / "post" / "test-post.yaml"
        import yaml as _yaml
        notes = _yaml.safe_load(yaml_path.read_text())
        assert len(notes) == 2
        assert notes[0]["body"] == "First"
        assert notes[1]["body"] == "Second"

    def test_add_creates_directories(self, writable_site):
        site, _ = writable_site
        result = add_marginalia(str(site), "projects/test-project/index.md", "Project note")
        yaml_path = Path(site) / "data" / "marginalia" / "projects" / "test-project.yaml"
        assert yaml_path.exists()

    def test_add_path_traversal_rejected(self, writable_site):
        site, _ = writable_site
        with pytest.raises(ValueError, match="within"):
            add_marginalia(str(site), "../../etc/passwd", "Evil note")

    def test_deterministic_ids(self, writable_site):
        """Same page_path + body + created produces the same ID."""
        site, _ = writable_site
        r1 = add_marginalia(str(site), "post/test-post/index.md", "Same note")
        # Delete and re-add with same content won't produce same ID because
        # created timestamp differs. But two calls in the same second could.
        # Instead, test the _marginalia_id function directly.
        from hugo_memex.writer import _marginalia_id
        id1 = _marginalia_id("post/x/index.md", "body", "2026-01-01T00:00:00Z")
        id2 = _marginalia_id("post/x/index.md", "body", "2026-01-01T00:00:00Z")
        assert id1 == id2
        assert id1.startswith("mg-")
        assert len(id1) == 15  # "mg-" + 12 hex chars


class TestDeleteMarginaliaFromDisk:
    def test_delete_note(self, writable_site):
        site, _ = writable_site
        r1 = add_marginalia(str(site), "post/test-post/index.md", "Keep me")
        r2 = add_marginalia(str(site), "post/test-post/index.md", "Delete me")

        result = delete_marginalia_from_disk(str(site), r2["source_file"], r2["id"])
        assert result["status"] == "deleted"

        yaml_path = Path(site) / "data" / "marginalia" / "post" / "test-post.yaml"
        import yaml as _yaml
        notes = _yaml.safe_load(yaml_path.read_text())
        assert len(notes) == 1
        assert notes[0]["id"] == r1["id"]

    def test_delete_last_note_removes_file(self, writable_site):
        site, _ = writable_site
        r1 = add_marginalia(str(site), "post/test-post/index.md", "Only note")
        delete_marginalia_from_disk(str(site), r1["source_file"], r1["id"])

        yaml_path = Path(site) / "data" / "marginalia" / "post" / "test-post.yaml"
        assert not yaml_path.exists()

    def test_delete_not_found(self, writable_site):
        site, _ = writable_site
        add_marginalia(str(site), "post/test-post/index.md", "A note")
        with pytest.raises(ValueError, match="not found"):
            delete_marginalia_from_disk(
                str(site),
                "data/marginalia/post/test-post.yaml",
                "mg-nonexistent",
            )
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `pytest tests/test_writer.py::TestAddMarginalia tests/test_writer.py::TestDeleteMarginaliaFromDisk -v`
Expected: FAIL (ImportError)

- [ ] **Step 7: Implement add_marginalia and delete_marginalia_from_disk**

Add to `hugo_memex/writer.py`:

```python
def add_marginalia(hugo_root: str, page_path: str, body: str) -> dict:
    """Create a new marginalia note for a page. Writes to data/marginalia/.

    Returns dict with id, page_path, source_file, status.
    """
    yaml_path = marginalia_path_for_page(hugo_root, page_path)

    created = _now_iso()
    note_id = _marginalia_id(page_path, body, created)

    note = {"id": note_id, "created": created, "body": body}

    # Read existing notes or start fresh
    if yaml_path.exists():
        existing = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or []
    else:
        existing = []

    existing.append(note)

    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(
        yaml.dump(existing, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )

    # source_file relative to hugo_root
    source_file = str(yaml_path.relative_to(Path(hugo_root)))

    return {
        "id": note_id,
        "page_path": page_path,
        "source_file": source_file,
        "status": "created",
    }


def delete_marginalia_from_disk(
    hugo_root: str, source_file: str, note_id: str,
) -> dict:
    """Remove a marginalia note from its YAML file on disk.

    If the file becomes empty, deletes it.
    Raises ValueError if note_id is not found in the file.
    """
    yaml_path = Path(hugo_root) / source_file

    if not yaml_path.exists():
        raise ValueError(f"Marginalia file not found: {source_file}")

    notes = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or []
    original_len = len(notes)
    notes = [n for n in notes if n.get("id") != note_id]

    if len(notes) == original_len:
        raise ValueError(f"Note {note_id} not found in {source_file}")

    if notes:
        yaml_path.write_text(
            yaml.dump(notes, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )
    else:
        yaml_path.unlink()

    return {"id": note_id, "status": "deleted"}
```

- [ ] **Step 8: Run tests**

Run: `pytest tests/test_writer.py::TestAddMarginalia tests/test_writer.py::TestDeleteMarginaliaFromDisk tests/test_writer.py::TestMarginaliaPathMapping -v`
Expected: all tests PASS

- [ ] **Step 9: Run full test suite**

Run: `pytest tests/ -v`
Expected: all tests PASS

- [ ] **Step 10: Commit**

```bash
git add hugo_memex/writer.py tests/test_writer.py
git commit -m "feat: add marginalia path mapping and disk write/delete"
```

---

### Task 4: Indexer support for marginalia (indexer.py)

Add discovery and indexing of `data/marginalia/*.yaml` files.

**Files:**
- Modify: `hugo_memex/indexer.py`
- Create: `tests/fixtures/data/marginalia/post/test-post.yaml`
- Test: `tests/test_indexer.py`

- [ ] **Step 1: Create test fixture marginalia file**

Create `tests/fixtures/data/marginalia/post/test-post.yaml`:

```yaml
- id: "mg-fixture00001"
  created: "2026-04-15T10:00:00Z"
  body: "This fixture note references Python and SQLite integration."
- id: "mg-fixture00002"
  created: "2026-04-16T09:00:00Z"
  body: "Related to llm-memex://conversation/test-convo-123"
```

- [ ] **Step 2: Write failing tests for marginalia indexing**

Add to `tests/test_indexer.py`:

```python
from hugo_memex.writer import page_path_for_marginalia


class TestMarginaliaIndexing:
    def test_index_discovers_marginalia(self, hugo_root, db):
        stats = index_content(str(hugo_root), db)
        assert stats["marginalia_indexed"] >= 2

    def test_indexed_marginalia_queryable(self, hugo_root, db):
        index_content(str(hugo_root), db)
        rows = db.get_marginalia("post/test-post/index.md")
        assert len(rows) >= 2
        ids = {r["id"] for r in rows}
        assert "mg-fixture00001" in ids
        assert "mg-fixture00002" in ids

    def test_marginalia_fts_populated(self, hugo_root, db):
        index_content(str(hugo_root), db)
        rows = db.execute_sql(
            "SELECT id FROM marginalia_fts WHERE marginalia_fts MATCH 'Python'"
        )
        assert any(r["id"] == "mg-fixture00001" for r in rows)

    def test_marginalia_incremental(self, hugo_root, db):
        stats1 = index_content(str(hugo_root), db)
        assert stats1["marginalia_indexed"] >= 2
        stats2 = index_content(str(hugo_root), db)
        assert stats2["marginalia_indexed"] == 0
        assert stats2["marginalia_unchanged"] >= 1

    def test_marginalia_force_reindex(self, hugo_root, db):
        index_content(str(hugo_root), db)
        stats = index_content(str(hugo_root), db, force=True)
        assert stats["marginalia_indexed"] >= 2

    def test_marginalia_orphan_survives_page_delete(self, hugo_root, db):
        index_content(str(hugo_root), db)
        # Delete the page but marginalia should persist
        db.delete_page("post/test-post/index.md")
        rows = db.get_marginalia("post/test-post/index.md")
        assert len(rows) >= 2  # marginalia still there
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_indexer.py::TestMarginaliaIndexing -v`
Expected: FAIL (KeyError: 'marginalia_indexed')

- [ ] **Step 4: Implement marginalia indexing in indexer.py**

Add these functions to `hugo_memex/indexer.py`:

```python
def discover_marginalia(data_dir: Path) -> list[Path]:
    """Walk data/marginalia/ and collect all .yaml files."""
    marginalia_dir = data_dir / "marginalia"
    if not marginalia_dir.exists():
        return []
    return sorted(p for p in marginalia_dir.rglob("*.yaml") if p.is_file())
```

Add this import at the top of `indexer.py`:

```python
import yaml
```

And add this import from writer:

```python
from hugo_memex.writer import page_path_for_marginalia
```

Modify `index_content` to add a marginalia pass after the content pass. After the cleanup block (the `if not paths:` block at the end), add:

```python
    # ── Marginalia pass ──────────────────────────────────────────
    stats["marginalia_indexed"] = 0
    stats["marginalia_unchanged"] = 0
    stats["marginalia_removed"] = 0

    data_dir = hugo_root_path / "data"
    marginalia_files = discover_marginalia(data_dir)
    seen_marginalia_files: set[str] = set()

    for yaml_file in marginalia_files:
        rel_file = str(yaml_file.relative_to(hugo_root_path))
        seen_marginalia_files.add(rel_file)

        try:
            raw_bytes = yaml_file.read_bytes()
            file_hash = _content_hash(raw_bytes)
            file_mtime = yaml_file.stat().st_mtime

            if not force:
                sync = db.get_sync_state(rel_file)
                if sync and sync["content_hash"] == file_hash:
                    if sync["file_mtime"] != file_mtime:
                        db.save_sync_state(rel_file, file_hash, file_mtime, _now_iso())
                    stats["marginalia_unchanged"] += 1
                    continue

            notes = yaml.safe_load(raw_bytes.decode("utf-8-sig")) or []
            if not isinstance(notes, list):
                stats["errors"].append({"path": rel_file, "error": "Expected YAML list"})
                continue

            # Compute page_path from the marginalia file's relative position
            marginalia_rel = str(
                yaml_file.relative_to(data_dir / "marginalia")
            )
            page_path = page_path_for_marginalia(marginalia_rel)

            # Clear old entries from this file before re-indexing
            db.delete_marginalia_by_source(rel_file)

            for note in notes:
                if not isinstance(note, dict):
                    continue
                note_id = note.get("id", "")
                body = note.get("body", "")
                created = note.get("created", "")
                if not note_id or not body:
                    continue
                db.save_marginalia({
                    "id": note_id,
                    "page_path": page_path,
                    "body": body,
                    "created_at": created,
                    "source_file": rel_file,
                })

            db.save_sync_state(rel_file, file_hash, file_mtime, _now_iso())
            stats["marginalia_indexed"] += len(
                [n for n in notes if isinstance(n, dict) and n.get("id") and n.get("body")]
            )

        except Exception as e:
            stats["errors"].append({"path": rel_file, "error": str(e)})

    # Cleanup: remove marginalia whose source files no longer exist
    if not paths:
        indexed_sources = db.get_all_marginalia_source_files()
        removed_sources = indexed_sources - seen_marginalia_files
        for src in removed_sources:
            db.delete_marginalia_by_source(src)
            db.delete_sync_state(src)
            stats["marginalia_removed"] += 1
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_indexer.py::TestMarginaliaIndexing -v`
Expected: all 6 tests PASS

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/ -v`
Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
git add hugo_memex/indexer.py tests/test_indexer.py tests/fixtures/data/marginalia/post/test-post.yaml
git commit -m "feat: index marginalia from data/marginalia/ YAML files"
```

---

### Task 5: MCP tools for marginalia (mcp.py)

Add `add_marginalia`, `get_marginalia`, and `delete_marginalia` tools.

**Files:**
- Modify: `hugo_memex/mcp.py`
- Test: `tests/test_mcp.py`

- [ ] **Step 1: Write failing tests for MCP marginalia tools**

Add to `tests/test_mcp.py`:

```python
class TestGetMarginalia:
    @pytest.mark.asyncio
    async def test_get_marginalia(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "get_marginalia")
        result = fn(page_path="post/test-post/index.md")
        assert len(result) >= 2
        assert all(r["page_path"] == "post/test-post/index.md" for r in result)

    @pytest.mark.asyncio
    async def test_get_marginalia_empty(self, mcp_server):
        fn = await _get_tool_fn(mcp_server, "get_marginalia")
        result = fn(page_path="nonexistent/page.md")
        assert result == []


class TestAddMarginalia:
    @pytest.mark.asyncio
    async def test_add_marginalia(self, writable_mcp_server):
        server, site = writable_mcp_server
        fn = await _get_tool_fn(server, "add_marginalia")
        result = fn(page_path="post/test-post/index.md", body="New MCP note")
        assert result["status"] == "created"
        assert result["id"].startswith("mg-")

        # Verify file was written
        yaml_path = site / "data" / "marginalia" / "post" / "test-post.yaml"
        assert yaml_path.exists()

    @pytest.mark.asyncio
    async def test_add_marginalia_path_traversal(self, writable_mcp_server):
        server, site = writable_mcp_server
        fn = await _get_tool_fn(server, "add_marginalia")
        with pytest.raises(Exception, match="within"):
            fn(page_path="../../etc/passwd", body="Evil")


class TestDeleteMarginalia:
    @pytest.mark.asyncio
    async def test_delete_marginalia(self, writable_mcp_server):
        server, site = writable_mcp_server
        add_fn = await _get_tool_fn(server, "add_marginalia")
        result = add_fn(page_path="post/test-post/index.md", body="To delete via MCP")

        del_fn = await _get_tool_fn(server, "delete_marginalia")
        del_result = del_fn(id=result["id"])
        assert del_result["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_delete_marginalia_not_found(self, writable_mcp_server):
        server, site = writable_mcp_server
        del_fn = await _get_tool_fn(server, "delete_marginalia")
        with pytest.raises(Exception):
            del_fn(id="mg-nonexistent")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp.py::TestGetMarginalia tests/test_mcp.py::TestAddMarginalia tests/test_mcp.py::TestDeleteMarginalia -v`
Expected: FAIL (tool "get_marginalia" not found)

- [ ] **Step 3: Implement MCP tools**

Add to `hugo_memex/mcp.py`, inside `_register_tools`, after the `validate_page` tool:

```python
    # -- Marginalia tools -----------------------------------------

    @mcp.tool(annotations={"readOnlyHint": True})
    def get_marginalia(
        page_path: Annotated[
            str,
            Field(description="Content path relative to content/ (e.g. 'post/my-post/index.md')"),
        ],
        ctx: Context | None = None,
    ) -> list[dict]:
        """Get all marginalia notes for a page, ordered by creation time.

Returns an empty list if no marginalia exists for the given page.
"""
        database = _get_db(mcp, ctx)
        return database.get_marginalia(page_path)

    @mcp.tool()
    def add_marginalia(
        page_path: Annotated[
            str,
            Field(description="Content path relative to content/ (e.g. 'post/my-post/index.md')"),
        ],
        body: Annotated[
            str,
            Field(description="Free-form note text (may contain URIs)"),
        ],
        ctx: Context | None = None,
    ) -> dict:
        """Add a marginalia note to a page. Writes to data/marginalia/.

Call rebuild_index() afterward to update the search index.
"""
        config = _get_config(mcp, ctx)
        hugo_root = config.get("hugo_root")
        if not hugo_root:
            raise ToolError("hugo_root not configured")

        from hugo_memex.writer import add_marginalia as _add

        try:
            return _add(hugo_root, page_path, body)
        except (ValueError, FileNotFoundError) as e:
            raise ToolError(str(e))

    @mcp.tool()
    def delete_marginalia(
        id: Annotated[
            str,
            Field(description="Marginalia note ID (e.g. 'mg-a1b2c3d4e5f6')"),
        ],
        ctx: Context | None = None,
    ) -> dict:
        """Delete a marginalia note by ID. Removes from the YAML file on disk.

Call rebuild_index() afterward to update the search index.
"""
        config = _get_config(mcp, ctx)
        hugo_root = config.get("hugo_root")
        if not hugo_root:
            raise ToolError("hugo_root not configured")

        database = _get_db(mcp, ctx)
        rows = database.execute_sql(
            "SELECT source_file FROM marginalia WHERE id = ?", (id,)
        )
        if not rows:
            raise ToolError(f"Marginalia note not found: {id}")

        from hugo_memex.writer import delete_marginalia_from_disk

        try:
            return delete_marginalia_from_disk(hugo_root, rows[0]["source_file"], id)
        except (ValueError, FileNotFoundError) as e:
            raise ToolError(str(e))
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_mcp.py::TestGetMarginalia tests/test_mcp.py::TestAddMarginalia tests/test_mcp.py::TestDeleteMarginalia -v`
Expected: all 5 tests PASS

- [ ] **Step 5: Verify tools show up in registration**

Add to `tests/test_mcp.py::TestServerSetup::test_tools_registered`, extend the existing assertions:

```python
        assert "get_marginalia" in tool_names
        assert "add_marginalia" in tool_names
        assert "delete_marginalia" in tool_names
```

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/ -v`
Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
git add hugo_memex/mcp.py tests/test_mcp.py
git commit -m "feat: add marginalia MCP tools (add, get, delete)"
```

---

### Task 6: Update schema resource and CLAUDE.md

Update the schema resource docs and CLAUDE.md to reflect marginalia.

**Files:**
- Modify: `hugo_memex/db.py` (get_schema docs string)
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add marginalia query patterns to get_schema docs**

In `hugo_memex/db.py`, in the `get_schema` method, append to the `docs` string (before the closing triple-quote):

```python
        docs = """
...existing docs...

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
```

- [ ] **Step 2: Update CLAUDE.md**

In `CLAUDE.md`, update the schema section to mention marginalia:

In the "Schema (3 tables + 1 virtual)" section, change the heading to "Schema (4 tables + 2 virtual)" and add:

```
- **marginalia**: Free-form notes attached to pages. No FK to `pages` (orphan survival). Source YAML files live in `data/marginalia/`.
- **marginalia_fts**: FTS5 virtual table over marginalia body text.
```

In the "Conventions" section, add:

```
- Marginalia stored in `data/marginalia/` as YAML files, mirroring content path structure
```

- [ ] **Step 3: Run full test suite (including schema resource test)**

Run: `pytest tests/ -v`
Expected: all tests PASS

- [ ] **Step 4: Commit**

```bash
git add hugo_memex/db.py CLAUDE.md
git commit -m "docs: add marginalia to schema resource and CLAUDE.md"
```

---

### Task 7: Integration test for full marginalia lifecycle

End-to-end test covering add, index, query, delete, orphan survival.

**Files:**
- Test: `tests/test_integration.py`

- [ ] **Step 1: Write integration test**

Add to `tests/test_integration.py`:

```python
from hugo_memex.writer import (
    add_marginalia,
    delete_marginalia_from_disk,
)
from hugo_memex.indexer import index_content
from hugo_memex.db import Database


class TestMarginaliaLifecycle:
    def test_full_lifecycle(self, tmp_path, fixtures_dir):
        """Add marginalia, index, query, delete, verify orphan survival."""
        import shutil
        site = tmp_path / "site"
        shutil.copytree(fixtures_dir, site)

        db = Database(":memory:")

        # Step 1: Initial index (includes fixture marginalia)
        stats = index_content(str(site), db)
        assert stats["errors"] == []
        assert stats["marginalia_indexed"] >= 2

        # Step 2: Query fixture marginalia
        notes = db.get_marginalia("post/test-post/index.md")
        assert len(notes) >= 2

        # Step 3: Add new marginalia via writer
        result = add_marginalia(
            str(site), "post/test-post/index.md", "New lifecycle note"
        )
        assert result["status"] == "created"

        # Step 4: Re-index to pick up the new note
        stats2 = index_content(str(site), db)
        assert stats2["marginalia_indexed"] >= 1  # the changed file

        notes2 = db.get_marginalia("post/test-post/index.md")
        assert len(notes2) == len(notes) + 1
        assert any(n["body"] == "New lifecycle note" for n in notes2)

        # Step 5: Delete a note via writer
        delete_marginalia_from_disk(
            str(site), result["source_file"], result["id"]
        )

        # Step 6: Re-index to reflect deletion
        stats3 = index_content(str(site), db)
        notes3 = db.get_marginalia("post/test-post/index.md")
        assert len(notes3) == len(notes)

        # Step 7: Orphan survival - delete the page, marginalia persists
        db.delete_page("post/test-post/index.md")
        orphan_notes = db.get_marginalia("post/test-post/index.md")
        assert len(orphan_notes) == len(notes)  # still there

        # Step 8: FTS still works on marginalia
        fts_rows = db.execute_sql(
            "SELECT id FROM marginalia_fts WHERE marginalia_fts MATCH 'Python'"
        )
        assert len(fts_rows) >= 1

        db.close()
```

- [ ] **Step 2: Run integration test**

Run: `pytest tests/test_integration.py::TestMarginaliaLifecycle -v`
Expected: PASS

- [ ] **Step 3: Run full test suite with coverage**

Run: `pytest tests/ -v --cov=hugo_memex`
Expected: all tests PASS, coverage includes new marginalia code paths

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add marginalia lifecycle integration test"
```
