"""Validate python3 -c '...' snippets in all SKILL.md files compile without SyntaxError."""

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
    raw Python `can't open file` message."""
    for skill_path in sorted(glob.glob(os.path.join(_REPO_ROOT, "skills/*/SKILL.md"))):
        skill_name = skill_path.replace("\\", "/").split("/")[-2]
        with open(skill_path, encoding="utf-8") as f:
            lines = f.read().split("\n")
        for lineno, line in enumerate(lines):
            if 'python3 "$HOOKS/note_writer.py"' not in line:
                continue
            window = "\n".join(lines[max(0, lineno - 6):lineno])
            assert 'test -f "$HOOKS/note_writer.py"' in window, (
                f"Skill {skill_name} line {lineno + 1} invokes note_writer.py "
                "with no preceding `test -f` existence guard"
            )
            assert "key=lambda p:" in window, (
                f"Skill {skill_name} line {lineno + 1} resolves $HOOKS with a "
                "lexicographic max() — use the version-aware sort key"
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


# Skills whose note_writer.py call sites this PR converted. Their sys.path
# bootstrap lines must use the SAME numeric version key as the HOOKS= line, or
# a single skill run can load obsidian_utils from one cached plugin version and
# note_writer.py from another (verified: with 3.9.0 and 3.10.0 both cached, the
# lexicographic form picks 3.9.0 while the numeric one picks 3.10.0).
_VERSION_AWARE_SKILLS = (
    "compress", "decide", "error-log", "retro", "standup",
    "vault-import", "vault-stats",
)
_LEXICOGRAPHIC_MAX = (
    'max(glob.glob(os.path.expanduser('
    '"~/.claude/plugins/cache/*/obsidian-brain/*/hooks")), default="hooks")'
)


def test_converted_skills_resolve_plugin_version_numerically():
    """Scoped deliberately to the skills this PR touches. The remaining
    ~26 lexicographic lines in the other skill files are a tracked follow-up:
    they are uniformly wrong together, which is survivable, whereas MIXING
    resolvers inside one run is the hazard this pins."""
    for skill in _VERSION_AWARE_SKILLS:
        path = os.path.join(_REPO_ROOT, "skills", skill, "SKILL.md")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert _LEXICOGRAPHIC_MAX not in content, (
            f"Skill {skill} still resolves the plugin cache lexicographically; "
            "at 3.10.x with a 3.9.x cached it will load a different plugin "
            "version than the note_writer.py call site in the same run."
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
