"""Lightweight fuzzy matching for pane filtering.

No dependencies — the algorithm is character-sequential matching so it works
like fzf / fuzzyfinder: the needle's characters must appear in the haystack
in order, but not necessarily contiguously.  Multi-word needles split on
whitespace and every word must match independently (AND logic).
"""
from __future__ import annotations


def fuzzy_match(needle: str, haystack: str) -> bool:
    """Return True when every character of *needle* appears in *haystack*
    in order, case-insensitive.

    An empty needle matches everything (clear filter = show all).
    """
    if not needle:
        return True
    needle_lower = needle.lower()
    haystack_lower = haystack.lower()
    for word in needle_lower.split():
        pos = 0
        for ch in word:
            pos = haystack_lower.find(ch, pos)
            if pos == -1:
                return False
            pos += 1
    return True
