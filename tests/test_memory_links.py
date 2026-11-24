"""Tests for shared memory-index link parsing."""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "hooks"))
sys.path.insert(0, str(_ROOT / "scripts"))

import memory_links  # noqa: E402
from vault_doctor_checks import memory_index  # noqa: E402


@pytest.mark.parametrize(
    ("line", "pointers", "asides"),
    [
        ("- [A](a.md) — hook", {"a.md"}, set()),
        ("- Fixtures: [a](a.md) · [b](b.md)", {"a.md", "b.md"}, set()),
        ("- see [[c]]", set(), {"c.md"}),
        ("- [A](a.md) — like [[b]]", {"a.md"}, {"b.md"}),
        ("- notes in a.md", set(), {"a.md"}),
        ("- [Missing](missing.md)", set(), set()),
    ],
)
def test_line_links(line, pointers, asides):
    store_names = {"a.md", "b.md", "c.md"}
    assert memory_links.line_pointers(line, store_names) == pointers
    assert memory_links.line_asides(line, store_names) == asides


def test_url_is_not_a_child_pointer():
    line = "- [x](https://example.com/README.md)"
    assert memory_links.line_pointers(line, {"README.md"}) == set()
    assert memory_links._md_child_link_targets(line) == set()


def test_memory_index_uses_shared_parser():
    assert memory_index._link_targets is memory_links._link_targets
