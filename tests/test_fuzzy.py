"""Tests for fuzzy_match character-sequential matching."""
import pytest

from ccmgr.fuzzy import fuzzy_match


# ── basic character-sequential matching ─────────────────────────────────

def test_empty_needle_matches_everything():
    assert fuzzy_match("", "anything") is True
    assert fuzzy_match("", "") is True


def test_exact_match():
    assert fuzzy_match("hello", "hello") is True


def test_case_insensitive():
    assert fuzzy_match("HELLO", "hello") is True
    assert fuzzy_match("hello", "HELLO") is True
    assert fuzzy_match("HeLo", "hElO") is True


def test_sequential_characters():
    """Characters must appear in order but not contiguously."""
    assert fuzzy_match("hl", "hello") is True  # h then l
    assert fuzzy_match("hlo", "hello") is True  # h, l, o in order


def test_reverse_order_fails():
    """Characters must appear in order."""
    assert fuzzy_match("lh", "hello") is False  # l before h


def test_missing_character_fails():
    assert fuzzy_match("hz", "hello") is False  # no 'z'


def test_abbreviation():
    """Common abbreviation pattern: first letters of path segments."""
    assert fuzzy_match("cw", "/mnt/c/CatWork") is True  # C then W
    assert fuzzy_match("catwork", "/mnt/c/CatWork") is True
    assert fuzzy_match("mnt", "/mnt/c/CatWork") is True


def test_partial_match():
    """Needle partially matches but last char doesn't."""
    assert fuzzy_match("hellox", "hello") is False


# ── multi-word (space-separated) AND matching ───────────────────────────

def test_two_words_both_match():
    assert fuzzy_match("cat work", "/mnt/c/CatWork") is True


def test_two_words_one_fails():
    assert fuzzy_match("cat xyz", "/mnt/c/CatWork") is False


def test_three_words_all_match():
    assert fuzzy_match("mnt cat work", "/mnt/c/CatWork") is True


def test_two_words_order_independent():
    """Each word matches independently; word order doesn't matter."""
    assert fuzzy_match("work cat", "/mnt/c/CatWork") is True


def test_extra_whitespace():
    assert fuzzy_match("  cat   work  ", "/mnt/c/CatWork") is True


# ── real-world ccmgr filter scenarios ────────────────────────────────────

def test_project_path_filter():
    """Simulates filtering projects by path fragments."""
    assert fuzzy_match("ccmgr", "/home/giovanna/ccmgr") is True
    assert fuzzy_match("gio cc", "/home/giovanna/ccmgr") is True
    assert fuzzy_match("cc test", "/home/giovanna/ccmgr") is False


def test_session_title_filter():
    """Simulates filtering session titles."""
    assert fuzzy_match("fix bug", "fix: login button not working on mobile") is True
    assert fuzzy_match("mobile fix", "fix: login button not working on mobile") is True
    assert fuzzy_match("feat", "fix: login button") is False


def test_chinese_characters():
    """CJK characters should work with character-sequential matching."""
    assert fuzzy_match("无尽夏", "/mnt/c/Users/无尽夏/CatWork") is True
    assert fuzzy_match("无夏", "/mnt/c/Users/无尽夏/CatWork") is True
    assert fuzzy_match("夏无", "/mnt/c/Users/无尽夏/CatWork") is False


def test_single_character():
    assert fuzzy_match("c", "/mnt/c/CatWork") is True
    assert fuzzy_match("z", "/mnt/c/CatWork") is False
