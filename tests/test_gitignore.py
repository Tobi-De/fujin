"""Tests for the .gitignore cache helper."""

from __future__ import annotations

import pytest

from fujin.gitignore import (
    CACHE_PATTERN,
    ensure_fujin_cache_ignored,
    is_fujin_cache_ignored,
)

BLOCK = f"# fujin local build cache\n{CACHE_PATTERN}\n"


def test_creates_gitignore_without_leading_blank_line(tmp_path):
    assert ensure_fujin_cache_ignored(tmp_path) is True

    assert (tmp_path / ".gitignore").read_text() == BLOCK
    assert is_fujin_cache_ignored(tmp_path) is True


def test_appends_after_content_missing_trailing_newline(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.pyc\n.env")

    assert ensure_fujin_cache_ignored(tmp_path) is True

    assert gitignore.read_text() == "*.pyc\n.env\n" + BLOCK


def test_appends_after_content_with_trailing_newline(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.pyc\n")

    assert ensure_fujin_cache_ignored(tmp_path) is True

    assert gitignore.read_text() == "*.pyc\n" + BLOCK


def test_second_call_is_a_noop(tmp_path):
    assert ensure_fujin_cache_ignored(tmp_path) is True
    before = (tmp_path / ".gitignore").read_bytes()

    assert ensure_fujin_cache_ignored(tmp_path) is False
    assert (tmp_path / ".gitignore").read_bytes() == before


@pytest.mark.parametrize(
    "line",
    [
        ".fujin/cache",
        ".fujin/cache/",
        "**/.fujin/cache",
        "**/.fujin/cache/",
        "/.fujin/cache",
        "/.fujin/cache/",
    ],
)
def test_recognises_equivalent_patterns(tmp_path, line):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(f"# existing\n{line}\n")

    assert is_fujin_cache_ignored(tmp_path) is True
    assert ensure_fujin_cache_ignored(tmp_path) is False
    assert gitignore.read_text() == f"# existing\n{line}\n"


def test_ignores_comment_mentions(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("# .fujin/cache is handled elsewhere\n")

    assert is_fujin_cache_ignored(tmp_path) is False
    assert ensure_fujin_cache_ignored(tmp_path) is True
    assert gitignore.read_text() == ("# .fujin/cache is handled elsewhere\n" + BLOCK)


def test_preserves_comments_and_other_lines(tmp_path):
    gitignore = tmp_path / ".gitignore"
    original = "# project ignores\n*.pyc\n\n!keep.pyc\ndist/\n"
    gitignore.write_text(original)

    assert ensure_fujin_cache_ignored(tmp_path) is True

    assert gitignore.read_text() == original + BLOCK
