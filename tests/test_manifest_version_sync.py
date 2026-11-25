"""plugin.json and marketplace.json name the same version (#532).

The preflight checks this in its LINT block, but skip modes (--docs-only,
--skip-tests, --auto) exit before that block. Before #532 the check ran ahead
of the skip-mode exit, outside the harden-repo template; now CI runs it on
every PR instead.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    return json.loads((ROOT / ".claude-plugin" / name).read_text(encoding="utf-8"))


def test_every_marketplace_entry_names_the_plugin_version():
    plugin = _load("plugin.json")
    market = _load("marketplace.json")
    name, version = plugin["name"], plugin["version"]
    assert isinstance(version, str) and version, plugin
    entries = [p for p in market["plugins"] if p.get("name") == name]
    assert entries, f"marketplace.json has no entry for {name!r}"
    assert [e.get("version") for e in entries] == [version] * len(entries), entries
