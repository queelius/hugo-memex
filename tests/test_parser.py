"""Tests for hugo_memex.parser."""
import pytest
from hugo_memex.parser import parse_content


class TestYAMLFrontMatter:
    def test_basic_yaml(self):
        raw = """---
title: "Hello World"
date: 2024-01-15
draft: false
tags:
  - python
  - testing
---

This is the body.
"""
        fm, body = parse_content(raw)
        assert fm["title"] == "Hello World"
        assert fm["draft"] is False
        assert fm["tags"] == ["python", "testing"]
        assert "This is the body." in body

    def test_yaml_with_nested_data(self):
        raw = """---
title: "Project"
project:
  status: "active"
  type: "library"
tech:
  languages:
    - "Rust"
---

Body here.
"""
        fm, body = parse_content(raw)
        assert fm["project"]["status"] == "active"
        assert fm["tech"]["languages"] == ["Rust"]
        assert "Body here." in body

    def test_yaml_with_bom(self):
        raw = "\ufeff---\ntitle: \"BOM Test\"\n---\n\nBody."
        fm, body = parse_content(raw)
        assert fm["title"] == "BOM Test"

    def test_yaml_empty_front_matter(self):
        raw = "---\n---\n\nJust body."
        fm, body = parse_content(raw)
        assert fm == {}
        assert "Just body." in body


class TestTOMLFrontMatter:
    def test_basic_toml(self):
        raw = """+++
title = "TOML Post"
date = 2024-06-15T10:00:00Z
draft = true
tags = ["go", "hugo"]
+++

TOML body content.
"""
        fm, body = parse_content(raw)
        assert fm["title"] == "TOML Post"
        assert fm["draft"] is True
        assert fm["tags"] == ["go", "hugo"]
        assert "TOML body content." in body

    def test_toml_with_tables(self):
        raw = """+++
title = "Tables"

[params]
author = "Test"

[params.social]
github = "user"
+++

Body.
"""
        fm, body = parse_content(raw)
        assert fm["params"]["author"] == "Test"
        assert fm["params"]["social"]["github"] == "user"


class TestJSONFrontMatter:
    def test_basic_json(self):
        raw = """{
  "title": "JSON Post",
  "date": "2024-01-01",
  "tags": ["json", "test"]
}

JSON body content.
"""
        fm, body = parse_content(raw)
        assert fm["title"] == "JSON Post"
        assert fm["tags"] == ["json", "test"]
        assert "JSON body content." in body

    def test_json_with_nested_braces(self):
        raw = '{"title": "Nested", "meta": {"key": "value"}}\n\nBody.'
        fm, body = parse_content(raw)
        assert fm["title"] == "Nested"
        assert fm["meta"]["key"] == "value"

    def test_json_with_escaped_quotes(self):
        raw = '{"title": "He said \\"hello\\""}\n\nBody.'
        fm, body = parse_content(raw)
        assert fm["title"] == 'He said "hello"'


class TestNoFrontMatter:
    def test_plain_markdown(self):
        raw = "# Just Markdown\n\nNo front matter here."
        fm, body = parse_content(raw)
        assert fm == {}
        assert "Just Markdown" in body

    def test_empty_string(self):
        fm, body = parse_content("")
        assert fm == {}
        assert body == ""

    def test_unclosed_yaml_delimiter(self):
        raw = "---\ntitle: broken\n\nNo closing delimiter."
        fm, body = parse_content(raw)
        assert fm == {}


class TestRealWorldContent:
    """Test against patterns found in actual Hugo sites."""

    def test_date_only_string(self):
        """Hugo allows date as just YYYY-MM-DD."""
        raw = "---\ntitle: \"Post\"\ndate: 2024-01-10\n---\n\nBody."
        fm, body = parse_content(raw)
        # PyYAML parses YYYY-MM-DD as datetime.date
        assert fm["title"] == "Post"
        assert fm["date"] is not None

    def test_multiline_description(self):
        raw = """---
title: "Multi"
description: >
  This is a long
  description that spans
  multiple lines.
---

Body.
"""
        fm, body = parse_content(raw)
        assert "long" in fm["description"]
        assert "multiple lines" in fm["description"]
