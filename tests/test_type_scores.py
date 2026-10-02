"""Every note type the plugin writes has an explicit rerank weight (#376).

``vault_index`` scores a note's type through ``_TYPE_SCORES_BY_CONTEXT``.
A type missing from a context falls back to 0.5. Before #376 that ranked a
health report (``claude-stats``) above standup notes in every context, above
retro notes in every context but ``standup``, and level with decisions in
``debugging``. This test scans
the writers for every ``type: claude-*`` they emit and requires each one to
have a weight in every context.
"""
from __future__ import annotations

import re
from pathlib import Path

import vault_index

REPO = Path(__file__).resolve().parent.parent

# Matches the three shapes writers use:
#   type: claude-x          (YAML in SKILL.md / templates / f-strings)
#   "type": "claude-x"      (Python dict literal)
#   type="claude-x"         (keyword argument)
# It does not match folder names such as ``claude-sessions`` because those
# never follow a ``type`` key. No leading word boundary: f-strings in hooks
# write ``"---\ntype: claude-emerge"``, where ``type`` follows a literal ``n``.
_TYPE_RE = re.compile(r"""["']?type["']?\s*[:=]\s*["']?(claude-[a-z0-9][a-z0-9-]*[a-z0-9])""")

# Written outside this repo (migrated memory notes) but present in live vaults.
_EXTERNAL_TYPES = {"claude-memory"}

_ORIGINAL_TYPES = {
    "claude-session", "claude-insight", "claude-decision",
    "claude-error-fix", "claude-retro", "claude-standup",
}


def _writer_files() -> list[Path]:
    files = sorted((REPO / "hooks").glob("*.py"))
    files += sorted((REPO / "scripts").glob("**/*.py"))
    files += sorted((REPO / "skills").glob("**/*.md"))
    files += sorted((REPO / "templates").glob("*.md"))
    return files


def collect_written_types() -> set[str]:
    found: set[str] = set()
    for path in _writer_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        found.update(_TYPE_RE.findall(text))
    return found | _EXTERNAL_TYPES


def test_scan_finds_the_original_types():
    # Positive control: an empty or broken scan must not pass the coverage test.
    found = collect_written_types()
    assert _ORIGINAL_TYPES <= found, sorted(_ORIGINAL_TYPES - found)


def test_scan_finds_the_newer_writers():
    found = collect_written_types()
    for t in ("claude-snapshot", "claude-stats", "claude-emerge",
              "claude-check-items-report"):
        assert t in found, t


def test_scan_finds_exactly_the_known_types():
    # Widening the scan (scripts/, skills/**) or the character class must not
    # quietly pull in a new type; a real new writer updates this list on purpose.
    assert collect_written_types() == _ORIGINAL_TYPES | {
        "claude-snapshot", "claude-memory", "claude-emerge", "claude-stats",
        "claude-check-items-report",
    }


def test_scan_covers_nested_scripts():
    # scripts/**/*.py must recurse: writers live in scripts/dev-test/ and
    # scripts/vault_doctor_checks/. (skills/**/*.md has no nested files today;
    # the recursive glob is there for future references/ folders.)
    files = {p.relative_to(REPO).as_posix() for p in _writer_files()}
    assert any(f.startswith("scripts/dev-test/") and f.endswith(".py") for f in files)
    assert "skills/vault-ask/SKILL.md" in files


def test_regex_ignores_folder_names():
    assert _TYPE_RE.findall("folder: claude-sessions\nsessions_folder: claude-sessions") == []


def test_regex_matches_all_writer_shapes():
    text = (
        'type: claude-aa\n"type": "claude-bb"\ntype="claude-cc"\n'
        'type: "claude-dd"\n' + r'"---\ntype: claude-ee\ndate: "' + "\ntype: claude-v2-note\n"
    )
    assert _TYPE_RE.findall(text) == [
        "claude-aa", "claude-bb", "claude-cc", "claude-dd", "claude-ee", "claude-v2-note",
    ]


def test_every_written_type_has_a_weight_in_every_context():
    written = collect_written_types()
    missing = {
        ctx: sorted(written - set(scores))
        for ctx, scores in vault_index._TYPE_SCORES_BY_CONTEXT.items()
        if written - set(scores)
    }
    assert not missing, f"types with no weight (would fall back to 0.5): {missing}"


def test_all_contexts_share_one_key_set():
    tables = vault_index._TYPE_SCORES_BY_CONTEXT
    keysets = {ctx: frozenset(scores) for ctx, scores in tables.items()}
    assert len(set(keysets.values())) == 1, keysets


def test_weights_are_in_unit_range():
    for ctx, scores in vault_index._TYPE_SCORES_BY_CONTEXT.items():
        for t, w in scores.items():
            assert 0.0 <= w <= 1.0, (ctx, t, w)


def test_reports_rank_below_curated_notes():
    for ctx, scores in vault_index._TYPE_SCORES_BY_CONTEXT.items():
        curated = min(scores["claude-insight"], scores["claude-decision"],
                      scores["claude-error-fix"])
        for report in ("claude-stats", "claude-check-items-report"):
            assert scores[report] == 0.0, (ctx, report)
            assert scores[report] < curated, (ctx, report)


def test_emerge_does_not_weight_its_own_output():
    assert vault_index._TYPE_SCORES_BY_CONTEXT["emerge"]["claude-emerge"] == 0.0
