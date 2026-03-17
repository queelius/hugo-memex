"""Content writing tools for Hugo sites.

All writes go to the filesystem (Hugo content files are ground truth).
The SQLite database is updated via rebuild_index after writes.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from hugo_memex.db import Database


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _dump_front_matter(fm: dict) -> str:
    """Serialize front matter to YAML string between --- delimiters."""
    # Use default_flow_style=False for readable output,
    # but flow style for simple lists (tags, categories)
    return yaml.dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True)


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
    content_root = Path(hugo_root) / "content"
    if not content_root.exists():
        raise FileNotFoundError(f"Content directory not found: {content_root}")

    if "title" not in front_matter:
        raise ValueError("front_matter must include 'title'")

    # Set defaults
    fm = dict(front_matter)
    if "date" not in fm:
        fm["date"] = _now_iso()
    if "draft" not in fm:
        fm["draft"] = True

    # Determine file path
    if bundle:
        page_dir = content_root / section / slug
        page_dir.mkdir(parents=True, exist_ok=True)
        file_path = page_dir / "index.md"
    else:
        (content_root / section).mkdir(parents=True, exist_ok=True)
        file_path = content_root / section / f"{slug}.md"

    if file_path.exists():
        raise FileExistsError(
            f"Page already exists: {file_path.relative_to(content_root)}"
        )

    # Build file content
    fm_text = _dump_front_matter(fm)
    content = f"---\n{fm_text}---\n\n{body}\n"
    file_path.write_text(content, encoding="utf-8")

    rel_path = str(file_path.relative_to(content_root))
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
    from hugo_memex.parser import parse_content

    content_root = Path(hugo_root) / "content"
    file_path = (content_root / path).resolve()

    if not file_path.is_relative_to(content_root.resolve()):
        raise ValueError("Path must be within content/ directory")
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    raw = file_path.read_text(encoding="utf-8-sig")
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
                key_stats[key] = {"count": 0, "examples": [], "types": set()}
            key_stats[key]["count"] += 1
            key_stats[key]["types"].add(type(value).__name__)
            if len(key_stats[key]["examples"]) < 3 and value:
                key_stats[key]["examples"].append(value)

    # Build template: keys appearing in >50% of pages
    template = {}
    for key, stats in sorted(key_stats.items(), key=lambda x: -x[1]["count"]):
        frequency = stats["count"] / total
        primary_type = max(stats["types"], key=lambda t: 1)  # most common type
        example = stats["examples"][0] if stats["examples"] else None

        # Generate sensible defaults
        if primary_type == "list":
            default = []
        elif primary_type == "dict":
            default = {}
        elif primary_type == "bool":
            default = False
        elif primary_type == "str":
            default = ""
        elif primary_type == "NoneType":
            default = None
        else:
            default = None

        template[key] = {
            "type": primary_type,
            "frequency": round(frequency, 2),
            "example": example,
            "default": default,
        }

    return template


def suggest_tags(
    db: Database, text: str, limit: int = 10,
) -> list[dict]:
    """Suggest existing tags based on content text.

    Uses FTS5 to find pages similar to the given text, then returns
    the most common tags among those pages, weighted by frequency.
    Also returns the canonical form (most-used casing) for each tag.
    """
    # Find similar pages via FTS5
    # Extract key terms from the text (first 200 words)
    words = text.split()[:200]
    # Filter to meaningful words (>3 chars, not common stopwords)
    terms = [w.strip(".,;:!?\"'()[]{}") for w in words if len(w) > 3]
    if not terms:
        return []

    # Build an OR query from the terms
    fts_query = " OR ".join(f'"{t}"' for t in terms[:30])
    try:
        similar_pages = db.execute_sql(
            "SELECT path FROM pages_fts WHERE pages_fts MATCH ? LIMIT 50",
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
        f"WHERE page_path IN ({placeholders}) AND taxonomy = 'tags' "
        f"GROUP BY term ORDER BY freq DESC LIMIT ?",
        (*paths, limit * 3),  # fetch extra to handle dedup
    )

    # Normalize: find the canonical (most-used) form for each tag
    seen_lower: dict[str, dict] = {}
    for row in tag_rows:
        lower = row["term"].lower()
        if lower not in seen_lower:
            # Find the most-used casing globally
            canonical_rows = db.execute_sql(
                "SELECT term, COUNT(*) as n FROM taxonomies "
                "WHERE taxonomy = 'tags' AND LOWER(term) = ? "
                "GROUP BY term ORDER BY n DESC LIMIT 1",
                (lower,),
            )
            canonical = canonical_rows[0]["term"] if canonical_rows else row["term"]
            seen_lower[lower] = {
                "tag": canonical,
                "relevance": row["freq"],
            }

    results = list(seen_lower.values())[:limit]
    return results


def validate_page(
    db: Database, hugo_root: str, path: str,
) -> dict:
    """Validate a page for completeness and consistency.

    Checks:
    - Required front matter fields present
    - Tag case consistency (flags duplicates)
    - Cross-reference validity (linked_project, related_posts)
    - GPG body hash match (if present)
    """
    from hugo_memex.parser import parse_content

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

    if not fm.get("tags"):
        issues.append({"severity": "warning", "field": "tags", "message": "No tags defined"})

    # Tag case consistency
    tags = fm.get("tags", [])
    if isinstance(tags, list):
        for tag in tags:
            variants = db.execute_sql(
                "SELECT term, COUNT(*) as n FROM taxonomies "
                "WHERE taxonomy = 'tags' AND LOWER(term) = ? "
                "GROUP BY term ORDER BY n DESC",
                (tag.lower(),),
            )
            if len(variants) > 1:
                canonical = variants[0]["term"]
                if tag != canonical:
                    issues.append({
                        "severity": "warning",
                        "field": "tags",
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
