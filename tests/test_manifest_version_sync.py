"""plugin.json and marketplace.json name the same version (#532).

The preflight checks this in its LINT block, but skip modes (--docs-only,
--skip-tests, --auto) exit before that block. Before #532 the check ran ahead
of the skip-mode exit, outside the harden-repo template; now CI runs it on
every PR instead.
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    return json.loads((ROOT / ".claude-plugin" / name).read_text(encoding="utf-8"))


def _version_sync_problems(plugin, market):
    """What is wrong with the pair, as a list of strings (empty when in sync)."""
    if not isinstance(plugin, dict) or not isinstance(market, dict):
        return ["a manifest is not a JSON object"]
    name, version = plugin.get("name"), plugin.get("version")
    problems = []
    for field, value in (("name", name), ("version", version)):
        if not isinstance(value, str) or not value:
            problems.append(f"plugin.json {field} is not a non-empty string: {value!r}")
    if problems:
        return problems
    plugins = market.get("plugins")
    if not isinstance(plugins, list):
        return [f"marketplace.json plugins is not a list: {plugins!r}"]
    entries = [p for p in plugins if isinstance(p, dict) and p.get("name") == name]
    if not entries:
        return [f"marketplace.json has no entry for {name!r}"]
    return [f"marketplace entry {e.get('name')!r} has version {e.get('version')!r}, "
            f"plugin.json has {version!r}" for e in entries if e.get("version") != version]


def test_every_marketplace_entry_names_the_plugin_version():
    assert _version_sync_problems(_load("plugin.json"), _load("marketplace.json")) == []


GOOD_PLUGIN = {"name": "p", "version": "1.2.3"}


def test_a_matching_pair_has_no_problems():
    market = {"plugins": [{"name": "other", "version": "0"}, {"name": "p", "version": "1.2.3"}]}
    assert _version_sync_problems(GOOD_PLUGIN, market) == []


@pytest.mark.parametrize("plugin, market", [
    pytest.param({"name": None, "version": "1.2.3"},
                 {"plugins": [{"name": None, "version": "1.2.3"}]}, id="null-name"),
    pytest.param({"name": "p", "version": ""},
                 {"plugins": [{"name": "p", "version": ""}]}, id="empty-version"),
    pytest.param(GOOD_PLUGIN, {"plugins": [{"version": "1.2.3"}]}, id="entry-without-name"),
    pytest.param(GOOD_PLUGIN, {"plugins": [{"name": "p", "version": "1.2.4"}]}, id="mismatch"),
    pytest.param(GOOD_PLUGIN, {"plugins": [{"name": "p", "version": "1.2.3"},
                                           {"name": "p", "version": "0.0.1"}]},
                 id="one-of-two-entries-stale"),
    pytest.param(GOOD_PLUGIN, {}, id="no-plugins-key"),
    pytest.param(GOOD_PLUGIN, {"plugins": {"p": "1.2.3"}}, id="plugins-not-a-list"),
])
def test_a_manifest_pair_that_disagrees_is_caught(plugin, market):
    assert _version_sync_problems(plugin, market) != []
