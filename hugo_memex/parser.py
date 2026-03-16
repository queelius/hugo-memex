"""Front matter parsing for Hugo content files.

Supports YAML (---), TOML (+++), and JSON ({) delimiters.
"""
from __future__ import annotations

import json
import tomllib

import yaml


def parse_content(raw: str) -> tuple[dict, str]:
    """Parse a Hugo content file into (front_matter_dict, body_markdown).

    Detects the front matter format by the opening delimiter:
      - ``---`` → YAML
      - ``+++`` → TOML
      - ``{``   → JSON

    Returns ({}, body) if no front matter is detected.
    """
    stripped = raw.lstrip("\ufeff")  # strip BOM if present
    stripped = stripped.lstrip("\n")

    if stripped.startswith("---"):
        return _parse_yaml(stripped)
    elif stripped.startswith("+++"):
        return _parse_toml(stripped)
    elif stripped.startswith("{"):
        return _parse_json(stripped)
    else:
        return {}, stripped


def _parse_yaml(text: str) -> tuple[dict, str]:
    """Parse YAML front matter delimited by --- ... ---."""
    # Skip opening ---
    after_open = text[3:]
    # Must have a newline after opening ---
    nl = after_open.find("\n")
    if nl == -1:
        return {}, text
    after_open = after_open[nl + 1:]

    # Find closing ---
    close = after_open.find("\n---")
    if close == -1:
        return {}, text

    fm_text = after_open[:close]
    body = after_open[close + 4:]  # skip \n---
    # Strip leading newline from body
    if body.startswith("\n"):
        body = body[1:]

    parsed = yaml.safe_load(fm_text)
    return (parsed if isinstance(parsed, dict) else {}), body


def _parse_toml(text: str) -> tuple[dict, str]:
    """Parse TOML front matter delimited by +++ ... +++."""
    after_open = text[3:]
    nl = after_open.find("\n")
    if nl == -1:
        return {}, text
    after_open = after_open[nl + 1:]

    close = after_open.find("\n+++")
    if close == -1:
        return {}, text

    fm_text = after_open[:close]
    body = after_open[close + 4:]
    if body.startswith("\n"):
        body = body[1:]

    parsed = tomllib.loads(fm_text)
    return parsed, body


def _parse_json(text: str) -> tuple[dict, str]:
    """Parse JSON front matter — the first {...} block."""
    # Find the matching closing brace
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                fm_text = text[: i + 1]
                body = text[i + 1:]
                if body.startswith("\n"):
                    body = body[1:]
                parsed = json.loads(fm_text)
                return (parsed if isinstance(parsed, dict) else {}), body

    return {}, text
