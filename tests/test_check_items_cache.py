import os
import sys
import pytest

HOOKS_DIR = os.path.join(os.path.dirname(__file__), "..", "hooks")
if HOOKS_DIR not in sys.path:
    sys.path.insert(0, HOOKS_DIR)


def test_canonical_hash_stable_across_whitespace():
    """Test 13 - whitespace and markdown punctuation do not change the hash."""
    from check_items_cache import canonical_hash
    a = canonical_hash("Fix #68")
    b = canonical_hash("  Fix  #68  ")
    c = canonical_hash("**Fix** #68")
    d = canonical_hash("Fix #68\n")
    assert a == b == c == d
    assert len(a) == 16
    assert all(ch in "0123456789abcdef" for ch in a)


def test_canonical_hash_changes_on_rename():
    """Test 14 - real content rename produces a different hash."""
    from check_items_cache import canonical_hash
    a = canonical_hash("Fix #68")
    b = canonical_hash("Fix issue 68")
    c = canonical_hash("Fix #69")
    assert a != b
    assert a != c
    assert b != c
