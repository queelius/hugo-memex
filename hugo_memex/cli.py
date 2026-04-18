"""CLI for hugo-memex."""
from __future__ import annotations

import argparse
import json
import sys

from hugo_memex import __version__


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hugo-memex",
        description="Index Hugo site content into SQLite and query via MCP.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--config", help="Path to config YAML file",
    )
    sub = parser.add_subparsers(dest="command")

    # index
    idx = sub.add_parser("index", help="Index Hugo content into SQLite")
    idx.add_argument("--force", action="store_true", help="Force full re-index")
    idx.add_argument(
        "--path", action="append", dest="paths",
        help="Specific content paths to index (repeatable)",
    )

    # stats
    sub.add_parser("stats", help="Show index statistics")

    # search
    srch = sub.add_parser("search", help="Full-text search content")
    srch.add_argument("query", help="Search query")
    srch.add_argument("-n", "--limit", type=int, default=20, help="Max results")

    # sql
    sq = sub.add_parser("sql", help="Run a SQL query")
    sq.add_argument("query", help="SQL query string")

    # mcp
    sub.add_parser("mcp", help="Start MCP server")

    return parser


def _load(config_path=None):
    """Load config + open database."""
    from hugo_memex.config import load_config
    from hugo_memex.db import Database

    config = load_config(config_path)
    if not config.get("hugo_root"):
        print("Error: hugo_root not configured.", file=sys.stderr)
        print(
            "Set HUGO_MEMEX_HUGO_ROOT or add hugo_root to config.yaml",
            file=sys.stderr,
        )
        sys.exit(1)
    db = Database(config["database_path"])
    return config, db


def cmd_index(args):
    config, db = _load(args.config)
    from hugo_memex.indexer import index_content

    stats = index_content(
        config["hugo_root"], db, paths=args.paths, force=args.force,
    )
    print(
        f"Indexed: {stats['indexed']}, "
        f"Unchanged: {stats['unchanged']}, "
        f"Archived: {stats['archived']}, "
        f"Restored: {stats['restored']}"
    )
    if stats["errors"]:
        print(f"Errors: {len(stats['errors'])}", file=sys.stderr)
        for e in stats["errors"]:
            print(f"  {e['path']}: {e['error']}", file=sys.stderr)
    db.close()


def cmd_stats(args):
    _, db = _load(args.config)
    stats = db.get_statistics()
    print(json.dumps(stats, indent=2))
    db.close()


def cmd_search(args):
    _, db = _load(args.config)
    rows = db.execute_sql(
        "SELECT p.path, p.title, p.section, p.date, "
        "snippet(pages_fts, 3, '>>>', '<<<', '...', 32) as snippet "
        "FROM pages_fts f "
        "JOIN pages p ON p.path = f.path "
        "WHERE pages_fts MATCH ? "
        "ORDER BY rank LIMIT ?",
        (args.query, args.limit),
    )
    if not rows:
        print("No results found.")
    else:
        for r in rows:
            print(f"\n{r['title']}")
            print(f"  {r['section']}/{r['path']}  ({r['date'] or 'no date'})")
            if r.get("snippet"):
                print(f"  {r['snippet']}")
    db.close()


def cmd_sql(args):
    _, db = _load(args.config)
    db.conn.execute("PRAGMA query_only=ON")
    try:
        rows = db.execute_sql(args.query)
        print(json.dumps(rows, indent=2, default=str))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


def cmd_mcp(args):
    from hugo_memex.mcp import create_server
    create_server().run()


def main():
    parser = _make_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    commands = {
        "index": cmd_index,
        "stats": cmd_stats,
        "search": cmd_search,
        "sql": cmd_sql,
        "mcp": cmd_mcp,
    }
    commands[args.command](args)
