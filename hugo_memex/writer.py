"""Content writing tools for Hugo sites.

All writes go to the filesystem (Hugo content files are ground truth).
The SQLite database is updated via rebuild_index after writes.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from hugo_memex.db import Database
from hugo_memex.parser import parse_content


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _dump_front_matter(fm: dict) -> str:
    """Serialize front matter to YAML string between --- delimiters."""
    return yaml.dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True)


# Slug must be filesystem-safe: alphanumerics, dash, underscore, dot only.
# Rejects path separators, `..`, empty, and other surprises.
_VALID_SLUG = re.compile(r"^[A-Za-z0-9._-]+$")

# Section is stricter: one path component, no separators.
_VALID_SECTION = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")


def _resolve_within(root: Path, *parts: str) -> Path:
    """Resolve a path under root and verify it stays within root.

    Follows symlinks during resolution, so a symlink inside root that
    points outside is detected (the resolved target won't be under root).

    Raises ValueError if the resolved path escapes root.
    """
    root_resolved = root.resolve()
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root_resolved):
        raise ValueError(f"Path escapes content/: {'/'.join(parts)}")
    return target


def create_page(
    hugo_root: str,
    section: str,
    slug: str,
    front_matter: dict,
    body: str,
    bundle: bool = True,
) -> dict:
    """Create a new Hugo content page.

    Args:
        hugo_root: Path to Hugo site root.
        section: Content section (e.g. 'post', 'projects').
        slug: URL slug (used as directory name for leaf bundles).
        front_matter: Front matter dict. 'title' is required.
        body: Markdown body content.
        bundle: If True (default), create as leaf bundle (slug/index.md).
                If False, create as standalone file (slug.md).

    Returns:
        Dict with path (relative to content/), absolute_path, status.
    """
    # Validate section and slug before touching the filesystem.
    # These become directory/file names, so we reject anything with
    # path separators, "..", or other surprises.
    if not isinstance(section, str) or not _VALID_SECTION.match(section):
        raise ValueError(
            f"Invalid section {section!r}: must match [A-Za-z0-9._-]+ "
            "and contain no path separators"
        )
    if not isinstance(slug, str) or not _VALID_SLUG.match(slug):
        raise ValueError(
            f"Invalid slug {slug!r}: must match [A-Za-z0-9._-]+ "
            "and contain no path separators"
        )

    content_root = Path(hugo_root) / "content"
    if not content_root.exists():
        raise FileNotFoundError(f"Content directory not found: {content_root}")

    if not front_matter.get("title"):
        raise ValueError("front_matter must include a non-empty 'title'")

    # Set defaults
    fm = dict(front_matter)
    if "date" not in fm:
        fm["date"] = _now_iso()
    if "draft" not in fm:
        fm["draft"] = True

    content_root_resolved = content_root.resolve()

    # Build the target directory and file path.
    # Create the directory, then resolve it (following symlinks) and verify
    # containment. If any parent is a symlink escaping content/, this catches it.
    if bundle:
        page_dir = content_root / section / slug
        file_path = page_dir / "index.md"
    else:
        page_dir = content_root / section
        file_path = page_dir / f"{slug}.md"

    page_dir.mkdir(parents=True, exist_ok=True)

    if not page_dir.resolve().is_relative_to(content_root_resolved):
        raise ValueError(f"Target directory escapes content/: {page_dir}")

    # Refuse to write through a symlink at the target path — even a dangling
    # one, since write_text would create the symlink target.
    if file_path.is_symlink():
        raise ValueError(f"Refusing to write through symlink at {file_path}")

    if file_path.exists():
        raise FileExistsError(
            f"Page already exists: {file_path.relative_to(content_root_resolved)}"
        )

    # Build file content
    fm_text = _dump_front_matter(fm)
    content = f"---\n{fm_text}---\n\n{body}\n"
    file_path.write_text(content, encoding="utf-8")

    rel_path = str(file_path.relative_to(content_root_resolved))
    return {
        "path": rel_path,
        "absolute_path": str(file_path),
        "status": "created",
    }


def update_page(
    hugo_root: str,
    path: str,
    front_matter: dict | None = None,
    body: str | None = None,
) -> dict:
    """Update an existing Hugo content page.

    Args:
        hugo_root: Path to Hugo site root.
        path: Content path relative to content/.
        front_matter: Dict of front matter fields to merge (not replace).
        body: New markdown body (replaces entire body if provided).

    Returns:
        Dict with path, status, changes summary.
    """
    content_root = Path(hugo_root) / "content"
    file_path = (content_root / path).resolve()

    if not file_path.is_relative_to(content_root.resolve()):
        raise ValueError("Path must be within content/ directory")
    if file_path.is_symlink():
        raise ValueError(f"Refusing to write through symlink at {path}")
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    raw = file_path.read_text(encoding="utf-8-sig")

    # Only support YAML front matter in update_page. TOML (+++) and JSON ({)
    # round-trip would require extra deps (tomli_w) and format-specific escaping.
    # Silently converting formats would mangle the user's site.
    stripped = raw.lstrip("\ufeff").lstrip("\n")
    if stripped.startswith("+++"):
        raise ValueError(
            f"TOML front matter not supported by update_page: {path}. "
            "Edit the file directly or convert to YAML front matter."
        )
    if stripped.startswith("{"):
        raise ValueError(
            f"JSON front matter not supported by update_page: {path}. "
            "Edit the file directly or convert to YAML front matter."
        )

    existing_fm, existing_body = parse_content(raw)

    changes = []

    # Merge front matter (update, don't replace)
    if front_matter:
        for key, value in front_matter.items():
            if key not in existing_fm or existing_fm[key] != value:
                changes.append(f"front_matter.{key}")
            existing_fm[key] = value

    # Replace body
    if body is not None and body != existing_body:
        existing_body = body
        changes.append("body")

    if not changes:
        return {"path": path, "status": "unchanged", "changes": []}

    # Rewrite file
    fm_text = _dump_front_matter(existing_fm)
    content = f"---\n{fm_text}---\n\n{existing_body}\n"
    file_path.write_text(content, encoding="utf-8")

    return {"path": path, "status": "updated", "changes": changes}


def get_front_matter_template(db: Database, section: str) -> dict:
    """Derive a front matter template from existing pages in a section.

    Queries the database for the most common front matter keys in the section
    and builds a template with default values. No hardcoded templates.

    Returns:
        Dict with key → {type, frequency, example, default} for each common key.
    """
    rows = db.execute_sql(
        "SELECT front_matter FROM pages WHERE section = ? AND kind = 'page' "
        "ORDER BY date DESC LIMIT 50",
        (section,),
    )
    if not rows:
        return {"_note": f"No pages found in section '{section}'"}

    total = len(rows)
    key_stats: dict[str, dict] = {}

    for row in rows:
        fm = json.loads(row["front_matter"]) if isinstance(row["front_matter"], str) else row["front_matter"]
        for key, value in fm.items():
            if key not in key_stats:
                key_stats[key] = {"count": 0, "examples": [], "types": Counter()}
            key_stats[key]["count"] += 1
            key_stats[key]["types"][type(value).__name__] += 1
            if len(key_stats[key]["examples"]) < 3 and value:
                key_stats[key]["examples"].append(value)

    # Sensible defaults per primary type
    _defaults = {
        "list": lambda: [],
        "dict": lambda: {},
        "bool": lambda: False,
        "str": lambda: "",
    }

    template = {}
    for key, stats in sorted(key_stats.items(), key=lambda x: -x[1]["count"]):
        frequency = stats["count"] / total
        # Pick the most frequent observed type for this key across pages.
        primary_type = stats["types"].most_common(1)[0][0]
        example = stats["examples"][0] if stats["examples"] else None
        default = _defaults.get(primary_type, lambda: None)()

        template[key] = {
            "type": primary_type,
            "frequency": round(frequency, 2),
            "example": example,
            "default": default,
        }

    return template


_FTS_TOKEN = re.compile(r"[A-Za-z0-9_-]+")


def suggest_tags(
    db: Database, text: str, limit: int = 10,
    taxonomy: str = "tags",
) -> list[dict]:
    """Suggest existing tags based on content text.

    Uses FTS5 to find pages similar to the given text, then returns
    the most common tags among those pages, weighted by frequency.
    Also returns the canonical form (most-used casing) for each tag.

    Args:
        taxonomy: Which taxonomy table to search. Defaults to 'tags'.
            For sites with custom taxonomy names, derive from hugo.toml.
    """
    # Tokenize the input into FTS-safe terms: alphanumerics, hyphen, underscore.
    # This strips any characters that would break FTS5 phrase syntax (quotes,
    # AND/OR/NEAR would still be strings, but quoting them as phrases is safe).
    terms = [t for t in _FTS_TOKEN.findall(text) if len(t) > 3]
    if not terms:
        return []

    # Build a ranked OR query. Each term is quoted as an FTS5 phrase,
    # which disables operator interpretation (so "AND", "NEAR" etc. are literal).
    fts_query = " OR ".join(f'"{t}"' for t in terms[:30])
    try:
        similar_pages = db.execute_sql(
            "SELECT path FROM pages_fts WHERE pages_fts MATCH ? "
            "ORDER BY rank LIMIT 50",
            (fts_query,),
        )
    except Exception:
        return []

    if not similar_pages:
        return []

    paths = [r["path"] for r in similar_pages]
    placeholders = ",".join("?" for _ in paths)

    # Get tag frequencies among similar pages
    tag_rows = db.execute_sql(
        f"SELECT term, COUNT(*) as freq FROM taxonomies "
        f"WHERE page_path IN ({placeholders}) AND taxonomy = ? "
        f"GROUP BY term ORDER BY freq DESC LIMIT ?",
        (*paths, taxonomy, limit * 3),
    )
    if not tag_rows:
        return []

    # Resolve canonical (most-used) casing in one batched query.
    lowered = {row["term"].lower() for row in tag_rows}
    lower_placeholders = ",".join("?" for _ in lowered)
    canonical_rows = db.execute_sql(
        f"SELECT term, COUNT(*) as n FROM taxonomies "
        f"WHERE taxonomy = ? AND LOWER(term) IN ({lower_placeholders}) "
        f"GROUP BY term",
        (taxonomy, *lowered),
    )
    canonical: dict[str, tuple[str, int]] = {}
    for cr in canonical_rows:
        lower = cr["term"].lower()
        if lower not in canonical or cr["n"] > canonical[lower][1]:
            canonical[lower] = (cr["term"], cr["n"])

    seen_lower: dict[str, dict] = {}
    for row in tag_rows:
        lower = row["term"].lower()
        if lower not in seen_lower:
            seen_lower[lower] = {
                "tag": canonical.get(lower, (row["term"], 0))[0],
                "relevance": row["freq"],
            }

    return list(seen_lower.values())[:limit]


def validate_page(
    db: Database, hugo_root: str, path: str,
    tag_taxonomy: str = "tags",
) -> dict:
    """Validate a page for completeness and consistency.

    Checks:
    - Required front matter fields present
    - Tag case consistency (flags duplicates)
    - Cross-reference validity (linked_project, related_posts)
    - GPG body hash match (if present)

    Args:
        tag_taxonomy: Which taxonomy the site uses for tags. Defaults to 'tags'.
    """
    content_root = Path(hugo_root) / "content"
    file_path = content_root / path
    if not file_path.exists():
        return {"path": path, "valid": False, "errors": [f"File not found: {path}"]}

    raw = file_path.read_text(encoding="utf-8-sig")
    fm, body = parse_content(raw)

    issues = []

    # Required fields
    for field in ("title", "date", "description"):
        if not fm.get(field):
            issues.append({"severity": "error", "field": field, "message": f"Missing required field: {field}"})

    tags = fm.get(tag_taxonomy)
    if not tags:
        issues.append({"severity": "warning", "field": tag_taxonomy, "message": f"No {tag_taxonomy} defined"})

    # Tag case consistency
    if isinstance(tags, list):
        for tag in tags:
            if not isinstance(tag, str):
                issues.append({
                    "severity": "warning",
                    "field": tag_taxonomy,
                    "message": f"Non-string {tag_taxonomy} entry: {tag!r}",
                })
                continue
            variants = db.execute_sql(
                "SELECT term, COUNT(*) as n FROM taxonomies "
                "WHERE taxonomy = ? AND LOWER(term) = ? "
                "GROUP BY term ORDER BY n DESC",
                (tag_taxonomy, tag.lower()),
            )
            if len(variants) > 1:
                canonical = variants[0]["term"]
                if tag != canonical:
                    issues.append({
                        "severity": "warning",
                        "field": tag_taxonomy,
                        "message": f"Tag '{tag}' has a more common variant: '{canonical}' (used {variants[0]['n']}x vs your form)",
                    })

    # Cross-reference validation
    linked = fm.get("linked_project")
    if linked:
        projects = linked if isinstance(linked, list) else [linked]
        for proj in projects:
            # Check if project exists (look for section pages matching the project name)
            matches = db.execute_sql(
                "SELECT path FROM pages WHERE section = 'projects' "
                "AND (slug = ? OR path LIKE ?)",
                (proj, f"projects/{proj}/%"),
            )
            if not matches:
                issues.append({
                    "severity": "warning",
                    "field": "linked_project",
                    "message": f"Linked project '{proj}' not found in projects section",
                })

    related = fm.get("related_posts")
    if isinstance(related, list):
        for rp in related:
            if isinstance(rp, str):
                matches = db.execute_sql(
                    "SELECT path FROM pages WHERE path = ? OR slug = ?",
                    (rp, rp),
                )
                if not matches:
                    issues.append({
                        "severity": "warning",
                        "field": "related_posts",
                        "message": f"Related post '{rp}' not found",
                    })

    # GPG body hash check
    stored_hash = fm.get("gpg_body_hash")
    if stored_hash:
        computed = _sha256(body)
        if stored_hash != computed:
            issues.append({
                "severity": "info",
                "field": "gpg_body_hash",
                "message": f"Body hash mismatch (content may have changed). Stored: {stored_hash[:30]}..., computed: {computed[:30]}...",
            })

    return {
        "path": path,
        "valid": not any(i["severity"] == "error" for i in issues),
        "issues": issues,
    }
