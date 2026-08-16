"""Validate python3 -c '...' snippets in all SKILL.md files compile without SyntaxError."""

import ast
import glob
import os
import re
import textwrap
import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


# Single-quoted snippets, possibly spanning lines (the long-standing form).
_SQ_SNIPPET_RE = re.compile(r"python3\s+-c\s+'(.*?)'", re.DOTALL)
# Double-quoted snippets. Deliberately SINGLE-LINE and quote-free
# (``[^"\n]*``): the note_writer.py call sites use a double-quoted one-liner
# so their own inner strings can be single-quoted, and without this pattern
# none of them were linted at all. Multi-line double-quoted snippets (e.g.
# /check-items) embed their own ``"`` characters, so a naive DOTALL match
# would slice them at the wrong quote and "lint" a fragment; requiring the
# closing quote on the same line skips those instead of mis-parsing them.
_DQ_SNIPPET_RE = re.compile(r'python3\s+-c\s+"([^"\n]*)"')


def _extract_python_snippets():
    """Extract python3 -c '...' and single-line python3 -c "..." blocks from
    all SKILL.md files."""
    snippets = []
    for skill_path in sorted(glob.glob(os.path.join(_REPO_ROOT, "skills/*/SKILL.md"))):
        # Extract skill name from path: .../skills/<name>/SKILL.md
        parts = skill_path.replace("\\", "/").split("/")
        skill_name = parts[-2]
        with open(skill_path, encoding="utf-8") as f:
            content = f.read()
        matches = list(_SQ_SNIPPET_RE.finditer(content))
        matches += list(_DQ_SNIPPET_RE.finditer(content))
        for i, match in enumerate(matches):
            # Dedent to handle snippets indented inside bash blocks in SKILL.md
            code = textwrap.dedent(match.group(1))
            snippets.append((f"{skill_name}-{i}", code))
    return snippets


_SNIPPETS = _extract_python_snippets()


@pytest.mark.parametrize("name,code", _SNIPPETS, ids=[s[0] for s in _SNIPPETS])
def test_python_snippet_syntax(name, code):
    """Each python3 -c snippet must be valid Python syntax."""
    compile(code, f"<{name}>", "exec")


def test_at_least_one_snippet_found():
    """Sanity check: we should find at least 10 snippets across all skills."""
    assert len(_SNIPPETS) >= 10, (
        f"Expected at least 10 python3 -c snippets, found {len(_SNIPPETS)}"
    )


def test_no_hardcoded_hooks_path():
    """No snippet should use the old hardcoded sys.path.insert(0, "hooks")."""
    for name, code in _SNIPPETS:
        assert 'sys.path.insert(0, "hooks")' not in code, (
            f"Snippet {name} uses hardcoded hooks path. "
            "Use glob-based cache resolution instead."
        )


def test_snippets_use_cache_glob():
    """Every snippet that imports from hooks should use the cache glob pattern."""
    for name, code in _SNIPPETS:
        if "from obsidian_utils" in code or "from open_item_dedup" in code:
            assert "plugins/cache/" in code, (
                f"Snippet {name} imports from hooks but doesn't use "
                "cache glob pattern for path resolution."
            )


def test_cache_glob_finds_installed_hooks():
    """The glob pattern used in skills should match the actual installed cache."""
    matches = sorted(glob.glob(os.path.expanduser(
        "~/.claude/plugins/cache/*/obsidian-brain/*/hooks"
    )))
    # This test only passes when the plugin is installed
    if not matches:
        pytest.skip("obsidian-brain plugin not installed in cache")
    hooks_dir = matches[-1]
    assert os.path.isfile(os.path.join(hooks_dir, "obsidian_utils.py")), (
        f"Cache hooks dir {hooks_dir} exists but obsidian_utils.py not found"
    )


def test_no_tail_c_in_skills():
    """SKILL.md files must not use 'tail -c' for hash extraction.

    tail -c counts raw bytes including trailing newlines, producing fewer
    visible characters than expected (e.g. 3 hex chars instead of 4).
    Use 'cut -c' instead. Lines containing "Do NOT use" are warnings, not usage.
    """
    _TAIL_C_RE = re.compile(r'tail -c')
    _WARNING_RE = re.compile(r'Do NOT use.*tail -c')
    for skill_path in sorted(glob.glob(os.path.join(_REPO_ROOT, "skills/*/SKILL.md"))):
        skill_name = skill_path.replace("\\", "/").split("/")[-2]
        with open(skill_path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if _TAIL_C_RE.search(line) and not _WARNING_RE.search(line):
                    raise AssertionError(
                        f"Skill {skill_name} line {lineno} uses 'tail -c' which "
                        "miscounts bytes due to trailing newlines. Use 'cut -c' instead."
                    )


def test_hooks_future_annotations():
    """All .py files using PEP 604/585 type hints must have 'from __future__ import annotations'.

    Without this import, `dict | None` and `list[str]` syntax fails on
    Python < 3.10 (macOS system Python is 3.9.6). Scans hooks/ and scripts/.
    """
    pep604_re = re.compile(r':\s*\w+\s*\|\s*\w+|-> \w+\s*\|\s*\w+')
    pep585_re = re.compile(r':\s*(?:list|dict|set|tuple)\[')
    py_files = sorted(
        glob.glob(os.path.join(_REPO_ROOT, "hooks", "*.py"))
        + glob.glob(os.path.join(_REPO_ROOT, "scripts", "**", "*.py"), recursive=True)
    )
    for py_file in py_files:
        with open(py_file, encoding="utf-8") as f:
            content = f.read()
        uses_modern = pep604_re.search(content) or pep585_re.search(content)
        if uses_modern:
            rel_path = os.path.relpath(py_file, _REPO_ROOT)
            assert "from __future__ import annotations" in content, (
                f"{rel_path} uses PEP 604/585 type hints "
                "but is missing 'from __future__ import annotations'. "
                "This breaks on Python < 3.10 (macOS system Python 3.9.6)."
            )


def test_snippets_import_os_before_usage():
    """Snippets using os.* must import os on a PRIOR line.

    The check must not false-pass by matching 'os' in usage lines like
    ``import glob; ... os.path.expanduser(...)``. Only actual import
    statements count: ``import os``, ``import sys, os``, etc.
    """
    # Matches 'import os' as a standalone import or in a comma-separated list
    _IMPORT_OS_RE = re.compile(
        r'^\s*import\s+(?:[\w]+\s*,\s*)*os(?:\s*,|\s*$)'
    )
    _OS_USAGE_RE = re.compile(r'\bos\.')
    for name, code in _SNIPPETS:
        if not _OS_USAGE_RE.search(code):
            continue
        lines = code.strip().split("\n")
        os_imported = False
        for line in lines:
            if _IMPORT_OS_RE.search(line):
                os_imported = True
            if _OS_USAGE_RE.search(line) and not _IMPORT_OS_RE.search(line):
                assert os_imported, (
                    f"Snippet {name} uses os.* before importing os"
                )
                break


def test_snippets_import_glob_before_usage():
    """SNIP_05: Snippets using glob.* must import glob on a PRIOR or SAME line."""
    _IMPORT_GLOB_RE = re.compile(
        r'^\s*import\s+(?:[\w]+\s*,\s*)*glob(?:\s*[,;]|\s*$)'
    )
    _GLOB_USAGE_RE = re.compile(r'\bglob\.')
    for name, code in _SNIPPETS:
        if not _GLOB_USAGE_RE.search(code):
            continue
        lines = code.strip().split("\n")
        glob_imported = False
        for line in lines:
            # Check import first (handles 'import glob; glob.glob(...)' on same line)
            if _IMPORT_GLOB_RE.search(line):
                glob_imported = True
            elif _GLOB_USAGE_RE.search(line):
                assert glob_imported, (
                    f"Snippet {name} uses glob.* before importing glob"
                )
                break


# ---------------------------------------------------------------------------
# Heredoc terminators for note_writer.py must be per-invocation, not fixed.
#
# A quoted delimiter blocks $/backtick expansion but NOT early termination:
# a note body containing a line exactly equal to the terminator ends the
# heredoc there, truncating the note and handing the rest of its text to the
# shell as commands. Notes about this plugin routinely quote these blocks, so
# the terminator carries a `<eof4>` placeholder the model substitutes with
# fresh hex per run. These tests exist so a future edit cannot quietly revert
# to a fixed delimiter.
# ---------------------------------------------------------------------------

_HEREDOC_OPEN_RE = re.compile(r"<<'(OB_[A-Za-z0-9_<>]*)'")


def test_note_writer_heredoc_terminators_are_per_invocation():
    for skill_path in sorted(glob.glob(os.path.join(_REPO_ROOT, "skills/*/SKILL.md"))):
        skill_name = skill_path.replace("\\", "/").split("/")[-2]
        with open(skill_path, encoding="utf-8") as f:
            content = f.read()
        for delim in _HEREDOC_OPEN_RE.findall(content):
            assert delim.endswith("_<eof4>"), (
                f"Skill {skill_name} uses a FIXED heredoc terminator {delim!r}. "
                "Note content containing that exact line ends the heredoc early "
                "and executes the remainder as shell. Use OB_..._EOF_<eof4> and "
                "substitute fresh hex per invocation."
            )


def test_note_writer_heredoc_openers_have_matching_terminator_lines():
    """Every `<<'OB_..._<eof4>'` opener needs its terminator on its own line
    at column 0 — an opener whose terminator was renamed (or indented) would
    swallow the rest of the block."""
    for skill_path in sorted(glob.glob(os.path.join(_REPO_ROOT, "skills/*/SKILL.md"))):
        skill_name = skill_path.replace("\\", "/").split("/")[-2]
        with open(skill_path, encoding="utf-8") as f:
            content = f.read()
        for delim in _HEREDOC_OPEN_RE.findall(content):
            terminators = re.findall(
                rf"^{re.escape(delim)}$", content, re.MULTILINE
            )
            assert terminators, (
                f"Skill {skill_name}: heredoc opener {delim!r} has no matching "
                "terminator at column 0"
            )


def test_note_writer_call_sites_guard_missing_cli():
    """Each note_writer.py block must resolve the plugin cache version-aware
    (lexicographic max() picks 3.9.0 over 3.10.0) and prove the CLI is there,
    so a stale cache fails in the documented `ERROR:` shape rather than as a
    raw Python `can't open file` message.

    The window is bounded by the nearest preceding `HOOKS=` line rather than a
    fixed line count: FORM B (#278) resolves $HOOKS through a multi-line
    `HOOKS=$(python3 -c "...")` block, so a fixed-size window sized for the
    old one-line resolver would clip the `key=lambda` check right out of
    view."""
    for skill_path in sorted(glob.glob(os.path.join(_REPO_ROOT, "skills/*/SKILL.md"))):
        skill_name = skill_path.replace("\\", "/").split("/")[-2]
        with open(skill_path, encoding="utf-8") as f:
            lines = f.read().split("\n")
        for lineno, line in enumerate(lines):
            if 'python3 "$HOOKS/note_writer.py"' not in line:
                continue
            hooks_start = next(
                (j for j in range(lineno - 1, -1, -1) if lines[j].startswith("HOOKS=")),
                None,
            )
            assert hooks_start is not None, (
                f"Skill {skill_name} line {lineno + 1} invokes note_writer.py "
                "with no preceding HOOKS= resolver line"
            )
            window = "\n".join(lines[hooks_start:lineno])
            assert 'test -f "$HOOKS/note_writer.py"' in window, (
                f"Skill {skill_name} line {lineno + 1} invokes note_writer.py "
                "with no preceding `test -f` existence guard"
            )
            assert "key=lambda _p:" in window, (
                f"Skill {skill_name} line {lineno + 1} resolves $HOOKS with a "
                "lexicographic max() — use the version-aware sort key"
            )
            # The line above is a NAMING check, and naming is not behaviour: a
            # site rewritten back to cache-only globbing with the lambda's name
            # left intact satisfied it (proved by mutation during #278 task 2 —
            # all 91 tests still passed). These two are the semantic half.
            # tests/test_hooks_resolver_drift.py enforces the same property
            # over all 71 resolver sites (68 cache-family + #287's 3 repo-root
            # ones); this keeps it enforced at the note_writer call sites
            # specifically, where the window is already in hand.
            assert "known_marketplaces.json" in window, (
                f"Skill {skill_name} line {lineno + 1} resolves $HOOKS from the "
                "plugin cache only — it must consult the marketplace "
                "installLocation first, or a directory-source install keeps "
                "importing the stale released tree (#278)"
            )
            assert window.index("known_marketplaces.json") < window.index(
                "plugins/cache/"
            ), (
                f"Skill {skill_name} line {lineno + 1} consults the plugin cache "
                "before installLocation; cache-first still serves the stale "
                "module whenever one exists (#278)"
            )


def test_only_standup_passes_overwrite():
    """--overwrite is legitimate at exactly one call site (/standup's Step 6.6
    in-place note upgrade). Anywhere else it would let a filename-hash
    collision silently destroy an existing note."""
    for skill_path in sorted(glob.glob(os.path.join(_REPO_ROOT, "skills/*/SKILL.md"))):
        skill_name = skill_path.replace("\\", "/").split("/")[-2]
        with open(skill_path, encoding="utf-8") as f:
            content = f.read()
        for line in content.split("\n"):
            if 'python3 "$HOOKS/note_writer.py"' in line and "--overwrite" in line:
                assert skill_name == "standup", (
                    f"Skill {skill_name} passes --overwrite to note_writer.py; "
                    "only /standup's in-place upgrade may do that"
                )


def test_no_python_3_13_only_apis():
    """No source or test file may use an API newer than CI's Python.

    CI pins 3.12 (.github/workflows/ci.yml) while local interpreters here are
    3.13, so a 3.13-only API passes locally and fails in CI. That already
    happened once: `Path.read_text(newline="")` (added in 3.13) went green
    locally and red in CI. Sibling of test_hooks_future_annotations, which
    guards the same class at the other end (3.9, the macOS system Python the
    hooks actually run under).

    Keep this list short and literal — it is a tripwire for APIs we have
    actually reached for, not an exhaustive compatibility checker.

    This module excludes ITSELF from the scan: the pattern table below
    necessarily contains every literal it searches for, so scanning this file
    would always self-trip. The alternative (splitting each literal across
    string concatenations so it never appears whole) makes both the patterns
    and the failure messages unreadable, which is a worse trade for a
    tripwire.
    """
    forbidden = {
        r"read_text\([^)]*newline": "Path.read_text(newline=...) is 3.13+; use Path.open(newline=...)",
        r"write_text\([^)]*newline": "Path.write_text(newline=...) is 3.13+; use Path.open(newline=...)",
        r"\.full_match\(": "PurePath.full_match() is 3.13+",
        r"re\.PatternError": "re.PatternError is 3.13+; use re.error",
        r"\bcopy\.replace\(": "copy.replace() is 3.13+",
    }
    py_files = sorted(
        glob.glob(os.path.join(_REPO_ROOT, "hooks", "*.py"))
        + glob.glob(os.path.join(_REPO_ROOT, "tests", "*.py"))
        + glob.glob(os.path.join(_REPO_ROOT, "scripts", "**", "*.py"), recursive=True)
    )
    for py_file in py_files:
        rel = os.path.relpath(py_file, _REPO_ROOT)
        if os.path.samefile(py_file, __file__):
            continue  # see docstring: the pattern table would self-trip
        with open(py_file, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                code = line.split("#", 1)[0]  # ignore comments naming the API
                for pattern, why in forbidden.items():
                    assert not re.search(pattern, code), f"{rel}:{lineno} — {why}"


# Plugin-cache resolution must be numeric, not lexicographic: max() over
# strings puts "3.9.0" above "3.10.0", so the moment the plugin reaches a
# two-digit minor with an older cache still present, every one of these lines
# resolves to the WRONG (older) tree — and it fails as an opaque Python
# traceback or a stale-module import, not as any documented ERROR: message.
#
# Scanned as a WINDOW around each occurrence of the cache path, not as a single
# regex over `max(glob.glob(...))`: nine sites bind the glob to a variable
# first (`c=glob.glob(...); print(max(c, key=...))`), which an expression-shaped
# regex misses entirely — it matched 58 of the 67 real sites. The window also
# covers the multi-line form, which the `python3 -c` snippet extractor does not
# parse at all.
_CACHE_GLOB_PATH = "~/.claude/plugins/cache/*/obsidian-brain/*/hooks"
# 4 lines either side: the widest real separation between the glob and its
# max() is the multi-line inline form (2 lines). Deliberately a literal, not
# derived from the files it scans.
_CACHE_WINDOW = 4


def _cache_resolution_sites():
    """[(skill, lineno, window_text)] for every plugin-cache resolution."""
    sites = []
    for skill_path in sorted(glob.glob(os.path.join(_REPO_ROOT, "skills/*/SKILL.md"))):
        skill_name = skill_path.replace("\\", "/").split("/")[-2]
        with open(skill_path, encoding="utf-8") as f:
            lines = f.read().split("\n")
        for i, line in enumerate(lines):
            if _CACHE_GLOB_PATH not in line:
                continue
            window = "\n".join(lines[max(0, i - _CACHE_WINDOW):i + _CACHE_WINDOW + 1])
            if "max(" in window:
                sites.append((skill_name, i + 1, window))
    return sites


def test_no_skill_resolves_the_plugin_cache_lexicographically():
    """Scans EVERY skills/*/SKILL.md — no hardcoded list — so a new skill file
    is covered the moment it is added.

    This is the regression guard for #274: the sweep itself is a one-off, but
    the pattern is easy to copy-paste back in from any older skill or from
    documentation."""
    offenders = [
        f"{skill}/SKILL.md:{lineno}"
        for skill, lineno, window in _cache_resolution_sites()
        if "key=lambda" not in window
    ]
    assert not offenders, (
        "Lexicographic plugin-cache resolution found at: "
        + ", ".join(offenders)
        + ". max() over strings picks 3.9.0 over 3.10.0 — use the numeric key: "
        'key=lambda p: ([int(n) for n in re.findall("[0-9]+", p.split("/")[-2])], p)'
    )


def test_cache_resolution_scan_sees_every_site():
    """Guards the guard: if the scan stops matching, the check above passes
    vacuously. 67 is the measured count of resolution sites today (58 inline +
    9 two-step); a LITERAL, not derived from the scan it validates. A drop
    means the scan went blind; a rise is fine and only needs this number
    raised deliberately."""
    assert len(_cache_resolution_sites()) >= 67, (
        f"only {len(_cache_resolution_sites())} cache-resolution sites matched "
        "the scan — the pattern may have been reformatted past it"
    )


def test_every_version_key_snippet_imports_re():
    """The numeric key uses re.findall, so `re` must be imported on that line
    or a prior one — otherwise the bootstrap dies with NameError at runtime."""
    for name, code in _SNIPPETS:
        if "re.findall" not in code:
            continue
        lines = code.strip().split("\n")
        imported = False
        for line in lines:
            if re.search(r"^\s*import\s[^;]*\bre\b", line):
                imported = True
            if "re.findall" in line:
                assert imported, f"Snippet {name} uses re.findall before importing re"
                break


def test_check_items_skill_captures_head_only_once():
    """#305: skills/check-items/SKILL.md must call `git rev-parse HEAD`
    exactly once (Step 3), not twice. Step 10 used to re-derive HEAD with its
    own `git rev-parse HEAD` call; a commit landing between the two reads
    would stamp the newer HEAD onto verdicts derived at the older one. Step
    10 now reuses the head captured at Step 3 via partition.json's "heads"
    key instead. Anchored on `"HEAD"` specifically so Step 3's unrelated
    `git rev-parse --show-toplevel` call (line ~129) is not counted."""
    path = os.path.join(_REPO_ROOT, "skills", "check-items", "SKILL.md")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    # The pattern must tolerate BOTH the argv-list form this file uses today
    # (`["git", "-C", p, "rev-parse", "HEAD"]`) and the bare shell form
    # (`git rev-parse HEAD`, `head=$(git -C "$r" rev-parse HEAD)`) — SKILL.md
    # is mostly bash, so a re-derivation reintroduced there is the likelier
    # regression, and an argv-only pattern would let it back in silently.
    # `HEAD(?![\w~^])` keeps revision expressions (HEAD~1, HEAD^) out: those
    # name a specific past commit and are not a re-derivation of current HEAD.
    # Step 3's `git rev-parse --show-toplevel` (line ~129) never matches.
    occurrences = re.findall(r'''rev-parse[\s,"']+HEAD(?![\w~^])''', content)
    assert len(occurrences) == 1, (
        f"expected exactly one `rev-parse ... HEAD` call in {path}, "
        f"found {len(occurrences)} — Step 10 must reuse Step 3's captured "
        "head, not re-derive it"
    )


def test_check_items_heads_handoff_key_matches():
    """#305: Step 3 captures each project's HEAD into partition.json and Step
    10 reads it back instead of re-deriving it. Nothing else pins that
    cross-block contract, and breaking it FAILS SILENTLY IN THE WORST
    DIRECTION: Step 10's `heads.get(proj)` returns None for every project, so
    every project hits `continue` and /check-items stops persisting
    classifications entirely — announced only on stderr, which this repo
    documents as ignorable. Verified by mutation: deleting `"heads": heads`
    from Step 3's json.dump leaves the entire 2645-test suite green.

    Both sides are extracted and compared rather than string-matched, so
    reformatting the dict or the call does not break the test — only an
    actual rename or removal on either end does.
    """
    path = os.path.join(_REPO_ROOT, "skills", "check-items", "SKILL.md")
    with open(path, encoding="utf-8") as f:
        content = f.read()

    write = re.search(r'json\.dump\(\{[^}]*"([A-Za-z_]\w*)":\s*heads\b', content)
    assert write, (
        "Step 3 must serialize the captured per-project HEADs into "
        "partition.json (expected a `\"<key>\": heads` entry in its json.dump)"
    )
    read = re.search(r'heads\s*=\s*part\.get\(\s*"([A-Za-z_]\w*)"', content)
    assert read, (
        "Step 10 must read the HEADs Step 3 captured out of partition.json "
        "(expected `heads = part.get(\"<key>\", ...)`)"
    )
    assert write.group(1) == read.group(1), (
        f"partition.json heads-key mismatch: Step 3 writes "
        f"{write.group(1)!r} but Step 10 reads {read.group(1)!r} — every "
        "cache update would be silently skipped"
    )


_PYEOF_HEREDOC_RE = re.compile(r"<< 'PYEOF'\n(.*?)\nPYEOF", re.DOTALL)


def _read_step6_heredoc():
    """Extract the Step 6 `python3 << 'PYEOF' ... PYEOF` heredoc body from
    skills/check-items/SKILL.md — the classifier fallback-chain block
    (agent -> heuristic gap-fill -> cache-merge), distinguished from the
    other 6 same-delimiter PYEOF blocks (see _read_step7_heredoc's
    docstring) by the presence of `classify_groups_with_agent`, which is
    unique to Step 6.
    """
    path = os.path.join(_REPO_ROOT, "skills", "check-items", "SKILL.md")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    blocks = _PYEOF_HEREDOC_RE.findall(content)
    assert blocks, "no `<< 'PYEOF' ... PYEOF` heredoc blocks found in SKILL.md"
    step6_blocks = [b for b in blocks if "classify_groups_with_agent" in b]
    assert len(step6_blocks) == 1, (
        f"expected exactly one PYEOF heredoc block containing "
        f"classify_groups_with_agent, found {len(step6_blocks)} (of "
        f"{len(blocks)} total heredoc blocks)"
    )
    return step6_blocks[0]


def _dict_has_pair(node, key_value, val_value):
    """True if an ast.Dict literal has a Constant `key_value: val_value`
    entry. Zips node.keys/node.values directly (parallel lists, same order,
    same length by construction) rather than building a plain dict from
    them first — several of this dict's other entries are Call nodes
    (`g.get(...)`), not Constants, so a naive `dict(zip(...))` collapse
    would still work here, but zipping keeps the intent explicit: we are
    looking for one specific, unambiguous (key, value) pair, not building a
    general-purpose lookup."""
    for k, v in zip(node.keys, node.values):
        if (
            isinstance(k, ast.Constant) and k.value == key_value
            and isinstance(v, ast.Constant) and v.value == val_value
        ):
            return True
    return False


def _dict_key_names(node):
    """Constant string keys of an ast.Dict literal (skips any non-Constant
    key, though this codebase never uses one)."""
    return {k.value for k in node.keys if isinstance(k, ast.Constant)}


def test_check_items_step6_cache_merge_stamps_note_evidence_only():
    """#318 fix round 4 (F12, CRITICAL): the cache-merge block that replays
    a cached classification for a `known_unchanged` group must stamp
    note_evidence_only onto the constructed dict.

    Without it, Step 7's `item.get("note_evidence_only", False)` silently
    defaults to False on the missing key -- and classifier_source="cache"
    IS a member of _HIGH_TRUST_SOURCES, so a cached DONE citation sharing a
    literal #N with the item text reaches HIGH and gets auto-checked, on
    EVERY re-run of the command after the first (the run that populated the
    cache). This is worse than the original F1/F7 defect: the first run is
    safe, the cached replay silently is not.

    Parsed with `ast`, not a substring search: walks for the ast.Dict
    literal carrying the Constant pair `"classifier_source": "cache"` (the
    one cache-merge dict in this block -- Step 6's other classifications
    all flow through classify_groups_with_agent / classify_groups_heuristic,
    neither of which constructs a literal dict inline here), and asserts
    "note_evidence_only" is among that dict's keys. A regex for the bare
    substring "note_evidence_only" would be satisfied by a comment or an
    unrelated dict elsewhere in the block.
    """
    source = _read_step6_heredoc()
    tree = ast.parse(source)

    cache_dicts = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        and _dict_has_pair(node, "classifier_source", "cache")
    ]
    assert len(cache_dicts) == 1, (
        f"expected exactly one dict literal with "
        f'"classifier_source": "cache" in Step 6\'s heredoc, found '
        f"{len(cache_dicts)}"
    )
    assert "note_evidence_only" in _dict_key_names(cache_dicts[0]), (
        "Step 6's cache-merge dict does not stamp note_evidence_only -- "
        f"found keys {sorted(_dict_key_names(cache_dicts[0]))!r}. Without "
        "it, every cached (classifier_source=\"cache\", a high-trust "
        "source) verdict for a note-only project defaults "
        "note_evidence_only=False at Step 7 and can reach HIGH again "
        "(#318 F12)."
    )


def _read_step5_heredoc():
    """Extract the Step 5 `python3 << 'PYEOF' ... PYEOF` heredoc body from
    skills/check-items/SKILL.md -- the evidence-gathering block, distinguished
    from the other 6 same-delimiter PYEOF blocks by the presence of
    `deep_analysis_pipeline(`, which is unique to Step 5 (Step 5's `from
    open_item_dedup import deep_analysis_pipeline` line contains the bare
    name without a paren, but the call site `status = deep_analysis_pipeline(`
    is the actual invocation and appears nowhere else).
    """
    path = os.path.join(_REPO_ROOT, "skills", "check-items", "SKILL.md")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    blocks = _PYEOF_HEREDOC_RE.findall(content)
    assert blocks, "no `<< 'PYEOF' ... PYEOF` heredoc blocks found in SKILL.md"
    step5_blocks = [b for b in blocks if "deep_analysis_pipeline(" in b]
    assert len(step5_blocks) == 1, (
        f"expected exactly one PYEOF heredoc block containing "
        f"deep_analysis_pipeline(, found {len(step5_blocks)} (of "
        f"{len(blocks)} total heredoc blocks)"
    )
    return step5_blocks[0]


def _is_dict_get_call(node, dict_name, key_value):
    """True if `node` is a Call matching `<dict_name>.get(<key_value>, ...)`."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == dict_name
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == key_value
    )


def test_check_items_step5_extracts_evidence_gaps_from_pipeline_output():
    """#318 Fix round 1 (F14): Step 5 reads `pipeline_data` (deep_analysis_
    pipeline's output_path JSON) and previously extracted only
    `pipeline_data.get("evidence", {})`, dropping `evidence_gaps` on the
    floor. deep_analysis_pipeline already writes evidence_gaps into that
    same JSON (#318 Task 3) -- it is the ONLY record of which projects had
    no local git repo. Step 9's dashboard can only ever render the
    ## Evidence Gaps section (Task 5 Step 5.5) if this extraction exists;
    without it the section and its tests are correct but dead code in a
    real /check-items run.

    Parsed with `ast`, not a substring search: walks for a Call node
    matching `pipeline_data.get("evidence_gaps", ...)` specifically (not
    just any occurrence of the string "evidence_gaps", which would also
    match a comment or the write_check_items_dashboard() call this same
    block does NOT make).
    """
    source = _read_step5_heredoc()
    tree = ast.parse(source)

    gap_extractions = [
        node for node in ast.walk(tree)
        if _is_dict_get_call(node, "pipeline_data", "evidence_gaps")
    ]
    assert gap_extractions, (
        "Step 5's heredoc does not extract "
        'pipeline_data.get("evidence_gaps", ...) -- the dashboard\'s '
        "## Evidence Gaps section can never receive real data from a "
        "live /check-items run (#318 F14)."
    )


def _read_step7_heredoc():
    """Extract the Step 7 `python3 << 'PYEOF' ... PYEOF` heredoc body from
    skills/check-items/SKILL.md.

    Steps 3-10 ALL use `python3 << 'PYEOF' ... PYEOF` (7 occurrences, same
    delimiter every time — unlike note_writer's per-invocation `<eof4>`
    convention) — a naive first-match regex grabs Step 3's block, not
    Step 7's. Scanned for the one block that actually contains
    `assign_tier(`, which is unique to Step 7. `python3 -c '...'` snippets
    elsewhere are already covered by _extract_python_snippets() above via
    _SQ_SNIPPET_RE / _DQ_SNIPPET_RE (neither pattern matches a heredoc, so
    no PYEOF block was ever visible to any existing snippet test). Reads
    the real file on disk, not a fixture, so this test guards the actual
    call site rather than a stand-in for it (#318 fix round 3, F11).
    """
    path = os.path.join(_REPO_ROOT, "skills", "check-items", "SKILL.md")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    blocks = _PYEOF_HEREDOC_RE.findall(content)
    assert blocks, "no `<< 'PYEOF' ... PYEOF` heredoc blocks found in SKILL.md"
    step7_blocks = [b for b in blocks if "assign_tier(" in b]
    assert len(step7_blocks) == 1, (
        f"expected exactly one PYEOF heredoc block containing assign_tier(, "
        f"found {len(step7_blocks)} (of {len(blocks)} total heredoc blocks)"
    )
    return step7_blocks[0]


def test_check_items_step7_threads_note_evidence_only_into_assign_tier():
    """#318 fix round 3 (F11): Step 7's assign_tier(...) call must pass
    note_evidence_only through, or every note-only project's citation
    silently reverts to the pre-F7 HIGH-reachable bypass — announced
    nowhere, since an omitted argument just falls back to
    note_evidence_only's own default (False).

    tests/test_check_items_note_evidence.py's
    test_step7_wiring_threads_note_evidence_only_into_assign_tier pins the
    *semantics* of that call shape (drop the 5th arg -> HIGH instead of
    MED) but only against a Python-level reproduction of it — it cannot
    catch a regression in the real markdown, because nothing executes
    SKILL.md. This test closes that gap by parsing the ACTUAL Step 7
    heredoc off disk.

    Uses `ast`, not a substring/regex search for "note_evidence_only": a
    regex would be satisfied by a comment or a string literal that never
    reaches the call, which is exactly the kind of false-green a future
    "helpful" edit could introduce. Walking the parsed AST for the
    `assign_tier(...)` Call node and inspecting its actual arguments (via
    `ast.unparse`, stdlib since Python 3.9 — no CI-version risk per
    test_no_python_3_13_only_apis's floor) can only pass if the argument is
    genuinely there.
    """
    source = _read_step7_heredoc()
    tree = ast.parse(source)

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "assign_tier"
    ]
    assert len(calls) == 1, (
        f"expected exactly one assign_tier(...) call in Step 7's heredoc, "
        f"found {len(calls)}"
    )
    call = calls[0]

    arg_sources = [ast.unparse(a) for a in call.args]
    kw_names = {kw.arg for kw in call.keywords if kw.arg}

    threaded_as_keyword = "note_evidence_only" in kw_names
    threaded_as_5th_positional = (
        len(arg_sources) >= 5 and "note_evidence_only" in arg_sources[4]
    )
    assert threaded_as_keyword or threaded_as_5th_positional, (
        "Step 7's assign_tier(...) call does not pass note_evidence_only "
        f"through — found {len(arg_sources)} positional arg(s) "
        f"{arg_sources!r} and keyword(s) {sorted(kw_names)!r}. Without it, "
        "every note-only project's citation defaults to "
        "note_evidence_only=False and can reach HIGH again (#318 F7)."
    )


def _read_step8_heredoc():
    """Extract the Step 8 `python3 << 'PYEOF' ... PYEOF` heredoc body from
    skills/check-items/SKILL.md -- the cascade block, distinguished from the
    other 6 same-delimiter PYEOF blocks by the presence of
    `cascade_group_members(`, which is unique to Step 8 (the `from
    open_item_dedup import cascade_group_members` line contains the bare
    name without a paren; the actual call site is the discriminator).
    """
    path = os.path.join(_REPO_ROOT, "skills", "check-items", "SKILL.md")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    blocks = _PYEOF_HEREDOC_RE.findall(content)
    assert blocks, "no `<< 'PYEOF' ... PYEOF` heredoc blocks found in SKILL.md"
    step8_blocks = [b for b in blocks if "cascade_group_members(" in b]
    assert len(step8_blocks) == 1, (
        f"expected exactly one PYEOF heredoc block containing "
        f"cascade_group_members(, found {len(step8_blocks)} (of "
        f"{len(blocks)} total heredoc blocks)"
    )
    return step8_blocks[0]


def test_check_items_step8_stamps_applied_on_flipped_records():
    """#318 Task 6: Step 8's cascade block must stamp `applied=True` onto
    each buckets record whose occurrence was actually Read-Verified-Edited
    by the primary-flip loop (tracked in `source_skips`), and persist that
    stamp back to `buckets_path` -- before Step 9 reads it.

    Without this, check_items_report._body's by-fact rendering (#318 Task
    6, `c.get("applied")`) has nothing to key off of in a real run: every
    classification record loaded from buckets_path would carry no
    `applied` key at all, so the dashboard's checkboxes would silently
    revert to always-unchecked (or, for DONE before this task's fix,
    always-checked) -- correct code shipping dead, exactly like the
    ## Evidence Gaps section before Fix round 1 (F14).

    Parsed with `ast`, not a substring search: walks for an Assign node
    whose target is a Subscript matching `<name>["applied"]` with value
    `True` -- a regex for the bare string "applied" would also match the
    unrelated `applied` count/heading logic living in check_items_report.py
    (a different file, not even loaded here) or a comment in this block.
    """
    source = _read_step8_heredoc()
    tree = ast.parse(source)

    def _is_applied_true_assign(node):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            return False
        target = node.targets[0]
        return (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == "applied"
            and isinstance(node.value, ast.Constant)
            and node.value.value is True
        )

    stamps = [node for node in ast.walk(tree) if _is_applied_true_assign(node)]
    assert stamps, (
        'Step 8\'s cascade block does not contain an `<name>["applied"] = '
        "True` assignment -- the dashboard's applied-by-fact rendering "
        "(#318 Task 6) can never receive real data from a live "
        "/check-items run."
    )

    # The stamp must also be persisted to buckets_path, or the mutation is
    # invisible to the fresh `json.load(open(buckets_path))` Step 9 does.
    # #318 M3: the write is now routed through _atomic_write_json(path, data)
    # (temp+rename, not a plain open("w")) -- checked via ast for a Call to
    # that name with buckets_path/buckets as its two arguments, not a
    # substring search for "json.dump(buckets", which no longer appears
    # literally in this block (the json.dump call now lives inside the
    # helper's generic (path, data) parameters).
    persist_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_atomic_write_json"
        and len(node.args) == 2
        and isinstance(node.args[0], ast.Name) and node.args[0].id == "buckets_path"
        and isinstance(node.args[1], ast.Name) and node.args[1].id == "buckets"
    ]
    assert persist_calls, (
        "Step 8's cascade block stamps applied but never writes buckets "
        "back to buckets_path via _atomic_write_json(buckets_path, buckets) "
        "-- the in-memory mutation is lost when this subprocess exits, "
        "since Step 9 runs as a separate python3 invocation that re-reads "
        "buckets_path from disk."
    )


def test_check_items_step8_atomic_write_helper_uses_temp_and_replace():
    """#318 M3: the buckets_path REWRITE (and the new cascade_summary.json
    write) must go through temp-file-then-rename, not a plain `open(path,
    "w")` -- the repo's atomic-write convention (write_vault_note(),
    check_items_cache.save_cache()). A plain in-place write left the
    previous test's `_atomic_write_json(buckets_path, buckets)` call site
    guard satisfied by a helper that still truncates the file directly, so
    this checks the helper's OWN body for `tempfile.mkstemp` and
    `os.replace`, not just that something named `_atomic_write_json` gets
    called."""
    source = _read_step8_heredoc()
    tree = ast.parse(source)

    funcs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_atomic_write_json"
    ]
    assert len(funcs) == 1, (
        f"expected exactly one _atomic_write_json def in Step 8's heredoc, "
        f"found {len(funcs)}"
    )
    body_src = ast.unparse(funcs[0])
    assert "tempfile.mkstemp" in body_src, (
        "_atomic_write_json does not use tempfile.mkstemp -- it is not "
        "writing to a temp file first."
    )
    assert "os.replace" in body_src, (
        "_atomic_write_json does not use os.replace -- a temp file with no "
        "rename into place is not an atomic write, just a stray temp file."
    )


def _read_step9_heredoc():
    """Extract the Step 9 `python3 << 'PYEOF' ... PYEOF` heredoc body from
    skills/check-items/SKILL.md -- the dashboard-write block, distinguished
    from the other 7 same-delimiter PYEOF blocks by the presence of
    `write_check_items_dashboard(` (the actual call, with its opening
    paren -- the bare `write_check_items_dashboard()` mention in this
    step's own prose, outside any heredoc, is never part of the extracted
    block strings in the first place, so it can't cause a false match here).
    """
    path = os.path.join(_REPO_ROOT, "skills", "check-items", "SKILL.md")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    blocks = _PYEOF_HEREDOC_RE.findall(content)
    assert blocks, "no `<< 'PYEOF' ... PYEOF` heredoc blocks found in SKILL.md"
    step9_blocks = [b for b in blocks if "write_check_items_dashboard(" in b]
    assert len(step9_blocks) == 1, (
        f"expected exactly one PYEOF heredoc block containing "
        f"write_check_items_dashboard(, found {len(step9_blocks)} (of "
        f"{len(blocks)} total heredoc blocks)"
    )
    return step9_blocks[0]


def test_check_items_step9_passes_merges_and_evidence_gaps():
    """#318 I1: Step 9 was prose-only ("call write_check_items_dashboard()
    with the scope, classifications, ...") with no executable block behind
    it -- the same shape as F14 (## Evidence Gaps): code that is written,
    tested, and unreachable unless a model happens to follow the prose.
    `merge_records_from_groups` had ZERO production callers outside that
    prose. Step 9 now has a real heredoc; this guards that its
    write_check_items_dashboard(...) call actually passes `merges=` and
    `evidence_gaps=` as keyword arguments, not just that the function is
    called at all.

    Parsed with `ast`: walks for the Call node whose func is the Name
    `write_check_items_dashboard`, then inspects its keyword arguments by
    name -- a regex for the bare strings "merges" / "evidence_gaps" would
    also match their assignment above the call (`merges = ...`,
    `evidence_gaps = ...`) or a comment, neither of which proves they
    reached the call itself.
    """
    source = _read_step9_heredoc()
    tree = ast.parse(source)

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "write_check_items_dashboard"
    ]
    assert len(calls) == 1, (
        f"expected exactly one write_check_items_dashboard(...) call in "
        f"Step 9's heredoc, found {len(calls)}"
    )
    kw_names = {kw.arg for kw in calls[0].keywords if kw.arg}
    missing = {"merges", "evidence_gaps"} - kw_names
    assert not missing, (
        f"Step 9's write_check_items_dashboard(...) call is missing "
        f"keyword argument(s) {sorted(missing)} -- found keywords "
        f"{sorted(kw_names)!r}. Without merges=, '## Merged Groups' has "
        f"nothing correct to render (#318 bug 3); without evidence_gaps=, "
        f"'## Evidence Gaps' silently never renders (#318 F14)."
    )

    # N6: the other two of I1's three "must be mechanical" items were
    # unguarded -- pinned only by the keyword-argument check above binding
    # `classifications=classifications` and `cascaded=cascaded`, neither of
    # which proves those NAMES were themselves derived from a fresh
    # buckets_path read / cascade_summary_path read, as opposed to (say) a
    # stale variable left over from an earlier, unrelated block.
    def _is_json_load_open_call(node, arg_name):
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "load"
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "json"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Call)
            and isinstance(node.args[0].func, ast.Name) and node.args[0].func.id == "open"
            and node.args[0].args
            and isinstance(node.args[0].args[0], ast.Name)
            and node.args[0].args[0].id == arg_name
        )

    buckets_reads = [n for n in ast.walk(tree) if _is_json_load_open_call(n, "buckets_path")]
    assert buckets_reads, (
        "Step 9's heredoc does not contain json.load(open(buckets_path)) -- "
        "classifications must be reloaded fresh from buckets_path (#318 "
        "Task 6), not a stale copy, or every checkbox silently reverts to "
        "reading from classification instead of Step 8's applied stamps."
    )

    classifications_from_buckets = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "classifications"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "get"
        and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == "buckets"
    ]
    assert classifications_from_buckets, (
        "Step 9's heredoc does not assign classifications = buckets.get(...) "
        "-- json.load(open(buckets_path)) being present doesn't prove "
        "`classifications` itself comes from it, only that SOMETHING reads "
        "the file."
    )

    cascade_summary_reads = [
        n for n in ast.walk(tree) if _is_json_load_open_call(n, "cascade_summary_path")
    ]
    assert cascade_summary_reads, (
        "Step 9's heredoc does not contain "
        "json.load(open(cascade_summary_path)) -- cascaded/skipped would "
        "silently default to 0/0 on every run, not just when Step 8 was "
        "genuinely skipped (#318 I1/N3)."
    )


def test_check_items_step10_uses_locked_cache_not_bare_save():
    """#306: Step 10 must persist cache updates through locked_cache(), the
    context manager that serializes the whole load-mutate-save cycle behind
    a lock, not the old unlocked `load_cache()` ... `save_cache(cache)`
    pair. Skills are advisory prose, not enforced code (memory:
    feedback_skills_advisory_not_enforcement) — a future edit could
    silently drop back to the bare calls without anyone noticing; this pins
    the call shape so that regresses as a failing test instead.

    Extracted from the Step 10 block specifically (bounded by the next
    `## ` header), not the whole file, so Step 3's own unlocked
    load_cache() call — deliberately read-only per the #306 spec — cannot
    false-positive this check.
    """
    path = os.path.join(_REPO_ROOT, "skills", "check-items", "SKILL.md")
    with open(path, encoding="utf-8") as f:
        content = f.read()

    step10_start = content.index("## Step 10")
    rest = content[step10_start + len("## Step 10"):]
    next_header = re.search(r"\n## ", rest)
    step10 = rest[:next_header.start()] if next_header else rest

    assert re.search(r"from check_items_cache import[^\n]*\blocked_cache\b", step10), (
        "Step 10 must import locked_cache from check_items_cache"
    )
    assert re.search(r"\bwith\s+locked_cache\s*\(", step10), (
        "Step 10 must persist cache updates via `with locked_cache(...)`, "
        "not a bare load_cache()/save_cache() pair"
    )
    assert not re.search(r"(?<!\w)save_cache\s*\(\s*cache\s*\)", step10), (
        "Step 10 must not call save_cache(cache) directly — locked_cache() "
        "saves internally as part of its lock-protected cycle"
    )


def _read_step10():
    path = os.path.join(_REPO_ROOT, "skills", "check-items", "SKILL.md")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    step10_start = content.index("## Step 10")
    rest = content[step10_start + len("## Step 10"):]
    next_header = re.search(r"\n## ", rest)
    return content, rest[:next_header.start()] if next_header else rest


def test_check_items_step10_reports_failure_on_stdout_and_exits_nonzero():
    """#323 F3: two paths previously skipped the save entirely (a body
    exception; save_cache() itself raising on a full/read-only disk) and
    left the run looking clean anyway — nothing told the driving agent to
    notice, and the terminal output still showed Step 7's `Cached: N
    reused, M fresh` as if it were the final state. Step 10 must now catch
    both, print `cache NOT updated: <reason>` on STDOUT (mirrors the
    existing `#320 F2` precedent in Step 8: stderr is documented elsewhere
    in this repo, skills/standup/SKILL.md, as ignorable, so the failure
    signal must not live there), and exit non-zero."""
    _, step10 = _read_step10()

    assert re.search(r'except\s+Exception\s+as\s+\w+:', step10), (
        "Step 10 must wrap the locked_cache block in a try/except so a "
        "body exception or a save_cache() failure is caught rather than "
        "left as a bare traceback"
    )
    assert re.search(r'print\(f?"cache NOT updated', step10), (
        "Step 10 must print 'cache NOT updated: <reason>' when the "
        "locked_cache block fails"
    )
    assert re.search(r'print\(f?"cache NOT updated[^\n]*\n\s*sys\.exit\(1\)', step10), (
        "Step 10 must exit non-zero right after reporting the failure "
        "(not merely print it and fall through as if nothing happened)"
    )
    assert 'print("cache updated")' in step10, (
        "the pre-existing success message must still be there for the "
        "happy path"
    )


def test_check_items_step10_does_not_rebind_cache_from_update_cache():
    """#323 F6: `update_cache()` mutates its `cache` argument IN PLACE and
    returns that same object — but `locked_cache()` saves its OWN `cache`
    binding, not whatever a caller inside the `with` block reassigns the
    name to. `cache = update_cache(...)` is safe today only because the
    return value happens to be the same object; the day update_cache()
    returns a copy instead, every run would silently persist the
    pre-mutation snapshot while still printing 'cache updated'. Step 10
    must call update_cache(...) unassigned."""
    _, step10 = _read_step10()

    assert not re.search(r"\bcache\s*=\s*update_cache\s*\(", step10), (
        "Step 10 must not rebind `cache = update_cache(...)` — "
        "locked_cache() saves its own binding, not the rebind (#323 F6)"
    )
    assert re.search(r"\bupdate_cache\(\s*\n", step10), (
        "Step 10 must still call update_cache(...) (unassigned) so the "
        "cache is actually mutated"
    )


def test_check_items_output_format_has_a_cache_line():
    """#323 F3: the Output format block must surface Step 10's outcome via
    a `Cache:` line, unconditionally printed — unlike `Skipped:`, which is
    intentionally omitted when zero, `Cache:` must always appear so a
    dropped update can never be mistaken for silence meaning success."""
    content, _ = _read_step10()
    output_start = content.index("## Output format")
    output_block = content[output_start:output_start + 2000]

    assert "Cache: updated" in output_block, (
        "the example Output format block must show the success case, "
        "`Cache: updated`"
    )
    assert "NOT updated" in output_block, (
        "the Output format section must document the failure wording, "
        "`NOT updated — <reason>`"
    )
