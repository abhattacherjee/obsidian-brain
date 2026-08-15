"""Security hardening tests for obsidian-brain."""
import ast
import hashlib
import io
import itertools
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import time
import types
from pathlib import Path
from unittest.mock import patch

import pytest


class TestSecureDirectory:
    """C1: All temp/cache files use ~/.claude/obsidian-brain/ instead of /tmp."""

    def test_secure_dir_constant_points_to_claude_dir(self):
        # Read the source to verify the constant is defined as the real path.
        # We check the source rather than the live module attribute because the
        # global _isolate_secure_dir_globally autouse fixture patches _SECURE_DIR
        # to a per-test tmp dir; the invariant we care about is the *definition*
        # in source, not the runtime value under test isolation.
        import inspect
        import obsidian_utils
        src = inspect.getsource(obsidian_utils)
        expected_def = '_SECURE_DIR = os.path.expanduser("~/.claude/obsidian-brain")'
        assert expected_def in src, (
            f"_SECURE_DIR definition not found in source; expected:\n  {expected_def!r}"
        )

    def test_cache_prefix_under_secure_dir(self):
        from obsidian_utils import _CACHE_PREFIX, _SECURE_DIR
        assert _CACHE_PREFIX.startswith(_SECURE_DIR)

    def test_bootstrap_prefix_under_secure_dir(self):
        from obsidian_utils import _BOOTSTRAP_PREFIX, _SECURE_DIR
        assert _BOOTSTRAP_PREFIX.startswith(_SECURE_DIR)

    def test_ensure_secure_dir_creates_0o700(self, tmp_path, monkeypatch):
        test_dir = str(tmp_path / "secure-test")
        monkeypatch.setattr("obsidian_utils._SECURE_DIR", test_dir)
        from obsidian_utils import _ensure_secure_dir
        result = _ensure_secure_dir()
        assert result == test_dir
        mode = stat.S_IMODE(os.stat(test_dir).st_mode)
        assert mode == 0o700

    def test_ensure_secure_dir_fixes_wrong_permissions(self, tmp_path, monkeypatch):
        test_dir = str(tmp_path / "secure-test")
        os.makedirs(test_dir, mode=0o755)
        monkeypatch.setattr("obsidian_utils._SECURE_DIR", test_dir)
        from obsidian_utils import _ensure_secure_dir
        _ensure_secure_dir()
        mode = stat.S_IMODE(os.stat(test_dir).st_mode)
        assert mode == 0o700

    def test_ensure_secure_dir_idempotent(self, tmp_path, monkeypatch):
        test_dir = str(tmp_path / "secure-test")
        monkeypatch.setattr("obsidian_utils._SECURE_DIR", test_dir)
        from obsidian_utils import _ensure_secure_dir
        _ensure_secure_dir()
        _ensure_secure_dir()  # second call should not fail
        mode = stat.S_IMODE(os.stat(test_dir).st_mode)
        assert mode == 0o700


class TestEnvVarOverrideRemoved:
    """C2: OBSIDIAN_BRAIN_BOOTSTRAP_PREFIX env var no longer controls path."""

    def test_bootstrap_prefix_ignores_env_var(self, monkeypatch):
        monkeypatch.setenv("OBSIDIAN_BRAIN_BOOTSTRAP_PREFIX", "/tmp/evil-")
        from obsidian_utils import _bootstrap_prefix, _SECURE_DIR
        prefix = _bootstrap_prefix()
        assert "/tmp/evil-" not in prefix
        assert prefix.startswith(_SECURE_DIR)


class TestPathTraversal:
    """H1: write_vault_note blocks path traversal."""

    def test_write_vault_note_blocks_traversal(self, tmp_path):
        from obsidian_utils import write_vault_note
        result = write_vault_note(str(tmp_path), "../../etc", "evil.md", "payload")
        assert isinstance(result, str)
        assert "traversal" in result.lower() or "outside" in result.lower(), (
            f"Expected traversal error message, got: {result!r}"
        )

    def test_write_vault_note_allows_normal_subfolder(self, tmp_path):
        from obsidian_utils import write_vault_note
        result = write_vault_note(
            str(tmp_path), "claude-sessions", "test.md",
            "---\nstatus: summarized\n---\ntest content\n"
        )
        assert result is None
        assert (tmp_path / "claude-sessions" / "test.md").exists()


class TestTranscriptPathValidation:
    """H3: transcript_path must be inside ~/.claude/projects/."""

    def test_session_log_validates_transcript_path(self):
        import inspect
        import obsidian_session_log
        src = inspect.getsource(obsidian_session_log)
        assert "claude/projects" in src, "transcript_path validation missing"

    def test_context_snapshot_validates_transcript_path(self):
        import inspect
        import obsidian_context_snapshot
        src = inspect.getsource(obsidian_context_snapshot)
        assert "claude/projects" in src, "transcript_path validation missing"


class TestFindTranscriptContainment:
    """M8: find_transcript_jsonl validates returned path stays in projects_dir."""

    def test_find_transcript_checks_containment(self):
        import inspect
        from obsidian_utils import find_transcript_jsonl
        src = inspect.getsource(find_transcript_jsonl)
        assert "realpath" in src or "resolve" in src
        assert "startswith" in src or "is_relative_to" in src


class TestSecretScrubbing:
    """H2: scrub_secrets redacts common secret patterns."""

    def test_scrub_github_token(self):
        from obsidian_utils import scrub_secrets
        text = "my token is ghp_abc123def456ghi789jkl012mno345pqr678stu9"
        result = scrub_secrets(text)
        assert "ghp_" not in result
        assert "REDACTED" in result

    def test_scrub_aws_key(self):
        from obsidian_utils import scrub_secrets
        result = scrub_secrets("key=AKIAIOSFODNN7EXAMPLE")
        assert "AKIA" not in result

    def test_scrub_password(self):
        from obsidian_utils import scrub_secrets
        result = scrub_secrets("password=hunter2")
        assert "hunter2" not in result

    def test_scrub_bearer_token(self):
        from obsidian_utils import scrub_secrets
        result = scrub_secrets("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def")
        assert "eyJhb" not in result

    def test_scrub_pem_header(self):
        from obsidian_utils import scrub_secrets
        result = scrub_secrets("-----BEGIN RSA PRIVATE KEY-----")
        assert "BEGIN RSA" not in result

    def test_scrub_preserves_normal_text(self):
        from obsidian_utils import scrub_secrets
        text = "normal conversation about code review and debugging"
        assert scrub_secrets(text) == text


class TestRawMessageToggle:
    """H2: log_raw_messages config controls conversation logging."""

    def test_build_raw_fallback_scrubs_secrets(self):
        from obsidian_utils import build_raw_fallback
        result = build_raw_fallback(
            ["my password=hunter2 for the DB"],
            {"project": "test", "duration_minutes": 5},
            assistant_msgs=["ok"],
            config={"log_raw_messages": True},
        )
        assert "hunter2" not in result
        assert "## Conversation (raw)" in result

    def test_build_raw_fallback_skips_conversation_when_disabled(self):
        from obsidian_utils import build_raw_fallback
        result = build_raw_fallback(
            ["user message"],
            {"project": "test", "duration_minutes": 5},
            assistant_msgs=["assistant reply"],
            config={"log_raw_messages": False},
        )
        assert "## Conversation (raw)" not in result
        assert "user message" not in result

    def test_build_raw_fallback_includes_conversation_by_default(self):
        from obsidian_utils import build_raw_fallback
        result = build_raw_fallback(
            ["user message"],
            {"project": "test", "duration_minutes": 5},
            assistant_msgs=["assistant reply"],
        )
        assert "## Conversation (raw)" in result


class TestWikilinkEscaping:
    """Bash [[ ]] conditionals must not become Obsidian wikilinks."""

    def test_escape_wikilinks_basic(self):
        from obsidian_utils import escape_wikilinks
        assert escape_wikilinks("if [[ $X == y ]]; then") == r"if \[\[ $X == y ]]; then"

    def test_escape_wikilinks_no_false_positive(self):
        from obsidian_utils import escape_wikilinks
        # Single brackets should not be touched
        assert escape_wikilinks("if [ $X == y ]; then") == "if [ $X == y ]; then"

    def test_escape_wikilinks_empty(self):
        from obsidian_utils import escape_wikilinks
        assert escape_wikilinks("") == ""

    def test_escape_wikilinks_multiple(self):
        from obsidian_utils import escape_wikilinks
        text = "[[ a ]] && [[ b ]]"
        assert text.count("[[") == 2
        result = escape_wikilinks(text)
        assert "[[" not in result
        assert r"\[\[" in result

    def test_build_raw_fallback_escapes_wikilinks_in_user_msgs(self):
        from obsidian_utils import build_raw_fallback
        result = build_raw_fallback(
            ['if [[ $CURRENT_BRANCH == feature/* ]]; then echo yes; fi'],
            {"project": "test", "duration_minutes": 5},
            config={"log_raw_messages": True},
        )
        assert "[[" not in result.split("## Conversation (raw)")[1]
        assert r"\[\[" in result

    def test_build_raw_fallback_escapes_wikilinks_in_assistant_msgs(self):
        from obsidian_utils import build_raw_fallback
        result = build_raw_fallback(
            ["user msg"],
            {"project": "test", "duration_minutes": 5},
            assistant_msgs=['Run: if [[ $VAR ]]; then ...'],
            config={"log_raw_messages": True},
        )
        assert r"\[\[" in result

    def test_build_raw_fallback_escapes_wikilinks_in_tool_usage(self):
        from obsidian_utils import build_raw_fallback
        result = build_raw_fallback(
            ["user msg"],
            {"project": "test", "duration_minutes": 5},
            tool_uses=[{"name": "Bash", "detail": 'if [[ -f foo ]]; then rm foo; fi'}],
            config={"log_raw_messages": True},
        )
        tool_section = result.split("## Tool Usage")[1].split("##")[0]
        assert "[[" not in tool_section
        assert r"\[\[" in tool_section


class TestShellInjectionFix:
    """H4: commit-preflight.sh uses sys.argv, not path interpolation."""

    def test_commit_preflight_uses_sys_argv(self):
        with open("scripts/commit-preflight.sh") as f:
            src = f.read()
        assert "sys.argv[1]" in src, "commit-preflight still interpolates path"
        assert "hashlib.md5('$(realpath" not in src, "old vulnerable pattern present"


class TestStdinCap:
    """M6: EVERY entry point that reads stdin caps the read.

    This used to be a hardcoded list of three hook files checking for the
    literal ``read(1_000_000)``. That is the shape of check that goes quietly
    out of date: when ``hooks/note_writer.py`` was added with its own stdin
    read, nothing here failed — the new entry point was simply not in the
    list, and it reads ``STDIN_CAP_CHARS + 1`` rather than the literal, so
    even adding it to the list would not have matched (#275).

    The version below DISCOVERS stdin reads by parsing the AST of every module
    under hooks/ and scripts/, resolves each read's bound through named
    constants and simple arithmetic, and fails on any read it cannot prove is
    bounded. A new uncapped entry point fails immediately, wherever it lands.

    #278 added an opt-in second walk over the tree the SKILL.md resolvers
    actually land on, to catch a cached ``deep_cli.py`` sitting there with
    #275's cap reverted. That walk is GONE (#289), deliberately, and the
    removal is the fix rather than a regression:

    * It never ran. On a directory-source install the resolver points back at
      the checkout, so the walk short-circuited; on CI nothing resolves at
      all. Both configurations were no-ops, and it was gated behind
      ``OB_SCAN_RESOLVED_INSTALL=1`` on top of that — because ungated it went
      red on contributors' clean checkouts, naming cached files they cannot
      fix from their tree.
    * It could not catch anything new. A cache is populated from a release,
      and a release is cut from this repo, so the walk below is UPSTREAM of
      every byte that can land in a cache. The only uncapped cache trees
      possible are ones predating #275 — unfixable from any checkout by
      construction.
    * Its resolver mirror was a hand-written reimplementation. The canonical
      resolver's behaviour — directory-source beats cache, github-source
      falls through, malformed entries never shadow a later good one — is
      covered against the REAL SKILL.md bytes in
      tests/test_hooks_resolver_drift.py, so nothing was lost with it.

    What the reach was actually missing was in-repo: ``.claude/hooks/``. See
    ``_source_modules``.
    """

    CAP = 1_000_000
    # `read(CAP + 1)` is the documented overflow-detection idiom (note_writer),
    # so the bound may exceed CAP by exactly one.
    MAX_ALLOWED = CAP + 1

    @classmethod
    def _source_modules(cls):
        """Every in-repo Python entry point, across all three hook trees.

        ``.claude/hooks/`` is here because it is where the gap actually was.
        Those five files are Claude Code PreToolUse hooks — stdin entry points
        by definition, and squarely covered by CLAUDE.md's "cap stdin reads"
        pattern — and every one of them read ``json.load(sys.stdin)``
        unbounded while this guard stayed green, because the walk only ever
        looked at ``hooks/`` and ``scripts/``.
        """
        roots = [
            *sorted(Path("hooks").glob("*.py")),
            *sorted(Path("scripts").rglob("*.py")),
            *sorted(Path(".claude/hooks").glob("*.py")),
        ]
        return [p for p in roots if p.is_file()]

    # Attributes that CONSUME the stream. `read`/`read1`/`readline` take a
    # size; `readlines`' argument is a hint, not a bound, so it is always
    # unbounded. Non-consuming attributes (isatty, fileno, encoding) are
    # deliberately absent — they must not be flagged.
    SIZED_READS = ("read", "read1", "readline")
    UNSIZED_READS = ("readlines",)

    @staticmethod
    def _is_stdin_expr(node, aliases):
        """True if ``node`` evaluates to stdin — ``sys.stdin``, a bare
        ``stdin`` imported from sys, ``sys.stdin.buffer``, or a local alias
        bound to one of those."""
        if isinstance(node, ast.Attribute) and node.attr == "buffer":
            node = node.value
        if isinstance(node, ast.Attribute):
            return node.attr == "stdin"
        return isinstance(node, ast.Name) and node.id in aliases

    @classmethod
    def _stdin_aliases(cls, tree):
        """Names bound to stdin: ``stdin`` itself (``from sys import stdin``)
        plus any ``f = sys.stdin``. Without this, ``f = sys.stdin; f.read()``
        walks straight past the guard."""
        aliases = {"stdin"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and cls._is_stdin_expr(node.value, aliases):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases.add(target.id)
        return aliases

    @classmethod
    def _consumption_sites(cls, tree, aliases):
        """Yield ``(node, size_arg_or_None, kind)`` for every construct that
        drains stdin.

        Four shapes, all of which evaded the original `.read`-attribute-only
        check and one of which was LIVE in the tree (``json.load(sys.stdin)``
        at two deep_cli entry points, both reachable from skills):

        1. ``stdin.read(...)`` / ``read1`` / ``readline``  — sized, resolve it
        2. ``stdin.readlines()``                            — hint, not a bound
        3. ``stdin`` passed as an ARGUMENT to any call      — e.g. json.load,
           io.TextIOWrapper: the callee reads to EOF and no size is visible
        4. iteration — ``for line in stdin`` / comprehensions
        """
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and cls._is_stdin_expr(
                    node.func.value, aliases
                ):
                    if node.func.attr in cls.SIZED_READS:
                        yield node, (node.args[0] if node.args else None), node.func.attr
                    elif node.func.attr in cls.UNSIZED_READS:
                        yield node, None, node.func.attr
                    continue
                # stdin handed to something else to drain (json.load(sys.stdin))
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    if cls._is_stdin_expr(arg, aliases):
                        yield node, None, "passed-to-call"
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                if cls._is_stdin_expr(node.iter, aliases):
                    yield node, None, "iteration"
            elif isinstance(node, ast.comprehension):
                if cls._is_stdin_expr(node.iter, aliases):
                    yield node, None, "iteration"

    @staticmethod
    def _int_constants(tree):
        """``{NAME: value}`` for every unambiguous ``NAME = <int>`` binding.

        A name bound more than once is deliberately EXCLUDED rather than
        resolved last-write-wins: this walk flattens every scope in the module
        into one namespace, so a function-local ``CAP = 500`` and a
        module-level ``CAP = 10**9`` would otherwise collapse into whichever
        the walk happened to reach last — and half of those guesses resolve a
        read to a bound it does not actually have. Unresolvable is the safe
        answer: it reports the site as an offender rather than passing it.
        """
        seen = {}
        ambiguous = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, int):
                    for target in node.targets:
                        if not isinstance(target, ast.Name):
                            continue
                        if target.id in seen and seen[target.id] != node.value.value:
                            ambiguous.add(target.id)
                        seen[target.id] = node.value.value
        return {k: v for k, v in seen.items() if k not in ambiguous}

    @classmethod
    def _resolve_bound(cls, arg, consts):
        """Static value of a read()'s size argument, or None if unresolvable."""
        if arg is None:
            return None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
            return arg.value
        if isinstance(arg, ast.Name):
            return consts.get(arg.id)
        if isinstance(arg, ast.BinOp) and isinstance(arg.op, (ast.Add, ast.Sub)):
            left = cls._resolve_bound(arg.left, consts)
            right = cls._resolve_bound(arg.right, consts)
            if left is None or right is None:
                return None
            return left + right if isinstance(arg.op, ast.Add) else left - right
        return None

    @classmethod
    def _all_stdin_reads(cls):
        """``[(path, lineno, bound_or_None, kind)]`` for the whole codebase."""
        found = []
        for path in cls._source_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            consts = cls._int_constants(tree)
            aliases = cls._stdin_aliases(tree)
            for node, size_arg, kind in cls._consumption_sites(tree, aliases):
                bound = cls._resolve_bound(size_arg, consts) if size_arg else None
                found.append((str(path), node.lineno, bound, kind))
        return found

    def test_every_stdin_read_is_capped(self):
        offenders = [
            f"{path}:{lineno} ({kind}, "
            + ("no resolvable bound" if bound is None else f"bound={bound}")
            + ")"
            for path, lineno, bound, kind in self._all_stdin_reads()
            if bound is None or bound > self.MAX_ALLOWED
        ]
        assert not offenders, (
            "Uncapped or over-cap stdin read(s): " + ", ".join(offenders)
            + f". Cap reads at {self.CAP} characters (project CLAUDE.md security "
            "pattern); read(CAP + 1) is allowed for overflow detection."
        )

    def test_the_capped_check_can_actually_fail(self, tmp_path, monkeypatch):
        """Negative control for the guard's ASSERTION half, not its walk.

        ``test_every_stdin_read_is_capped`` asserts over whatever
        ``_all_stdin_reads`` returns; if the resolver or the offender predicate
        ever stops classifying an uncapped read as an offender, that assertion
        goes green over a codebase full of them and nothing notices. #278 got
        this property incidentally, from a test that ended by asserting the
        capped-check raised; #289 removed that test and took the property with
        it. Stated directly here, and without depending on the walk: the module
        list is replaced with one planted offender.

        Measured against two mutants that both leave the rest of the suite
        green: ``_all_stdin_reads``'s ``... if size_arg else None`` -> ``else
        cls.CAP``, and the offender predicate ``bound is None or bound > MAX``
        -> ``bound is not None and bound > MAX``.
        """
        offender = tmp_path / "uncapped_entry_point.py"
        offender.write_text("import sys\npayload = sys.stdin.read()\n", encoding="utf-8")
        monkeypatch.setattr(
            type(self), "_source_modules", classmethod(lambda cls: [offender])
        )
        assert self._all_stdin_reads(), "planted read was not discovered"
        with pytest.raises(AssertionError, match="Uncapped or over-cap stdin read"):
            self.test_every_stdin_read_is_capped()

    def test_discovery_finds_the_known_entry_points(self):
        """Guards the guard: if the AST walk ever stops finding reads, the
        check above would pass vacuously. These are the entry points that read
        stdin today — including the two the old hardcoded list missed and the
        ``.claude/hooks/`` tree the walk did not reach at all (#289).

        The ``.claude/hooks/`` entries are the load-bearing half: those files
        are already capped, so ``test_every_stdin_read_is_capped`` is green
        over them whether or not the walk reaches them. Only naming them here
        makes a walk that quietly stops covering that tree fail."""
        paths = {path for path, _, _, _ in self._all_stdin_reads()}
        for expected in (
            "hooks/obsidian_session_log.py",
            "hooks/obsidian_session_hint.py",
            "hooks/obsidian_context_snapshot.py",
            "hooks/note_writer.py",
            "hooks/check_items_cli.py",
            ".claude/hooks/require-preflight.py",
            ".claude/hooks/enforce-pr-base-branch.py",
            ".claude/hooks/prevent-direct-push.py",
            ".claude/hooks/update-changelog-before-pr.py",
            ".claude/hooks/validate-branch-name.py",
            # The remaining three, previously covered only by the count.
            "hooks/deep_cli.py",
            "hooks/obsidian_retro_gate.py",
            "scripts/vault_doctor.py",
        ):
            assert expected in paths, f"stdin read in {expected} no longer discovered"
        # Exact, not >=. What >= permits is SUBSTITUTION: an existing read
        # reformatted past the AST extractor at the same moment a new entry
        # point lands keeps the total at 13 and the suite green, while the
        # reformatted file's cap silently stops being verified. Every one of
        # the 13 is named above, so a new entry point should fail here and be
        # added deliberately. (tests/test_hooks_resolver_drift.py makes the
        # same call for the same reason.)
        found = self._all_stdin_reads()
        assert len(found) == 13, (
            f"expected exactly 13 stdin read sites, found {len(found)}: "
            + ", ".join(f"{path}:{lineno}" for path, lineno, _, _ in sorted(found)[:5])
            + " ... . A RISE means a new stdin entry point landed — name it in the "
            "list above, deliberately, because a new place the process reads "
            "untrusted input is exactly the thing this module exists to notice. A "
            "DROP means a read was removed, or reformatted past the AST extractor "
            "— the silent-substitution case the equality guard is here to catch, "
            "since that file's cap stops being verified while the suite stays "
            "green."
        )

class TestFilePermissions:
    """M1, M2: Files use 0o600 permissions."""

    def test_write_vault_note_uses_0o600(self):
        import inspect
        from obsidian_utils import write_vault_note
        src = inspect.getsource(write_vault_note)
        assert "0o600" in src
        assert "0o644" not in src

    def test_vault_index_db_uses_0o600(self):
        with open("hooks/vault_index.py") as f:
            src = f.read()
        assert "0o644" not in src, "vault_index still uses 0o644"
        assert "0o600" in src

    def test_load_config_fixes_permissions(self):
        import inspect
        from obsidian_utils import load_config
        src = inspect.getsource(load_config)
        assert "0o077" in src or "0o600" in src, "config permission fix missing"

    def test_vault_note_written_with_0o600(self, tmp_path):
        from obsidian_utils import write_vault_note
        write_vault_note(
            str(tmp_path), "sessions", "test.md",
            "---\nstatus: summarized\n---\ncontent\n"
        )
        note = tmp_path / "sessions" / "test.md"
        mode = stat.S_IMODE(os.stat(note).st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


class TestLikeEscaping:
    """M7: LIKE wildcards in tags are escaped."""

    def test_vault_index_has_like_escape(self):
        with open("hooks/vault_index.py") as f:
            src = f.read()
        assert "ESCAPE" in src, "LIKE ESCAPE clause missing"


class TestFlipNoteStatus:
    """M5: flip_note_status uses atomic write."""

    def test_flip_note_status_atomic(self, tmp_path):
        from obsidian_utils import flip_note_status
        note = tmp_path / "test-note.md"
        note.write_text("---\nstatus: auto-logged\nproject: test\n---\nContent here\n")
        flip_note_status(str(note), "auto-logged", "summarized")
        content = note.read_text()
        assert "status: summarized" in content
        assert "status: auto-logged" not in content
        assert "Content here" in content

    def test_flip_note_status_preserves_other_fields(self, tmp_path):
        from obsidian_utils import flip_note_status
        note = tmp_path / "test-note.md"
        note.write_text("---\nstatus: auto-logged\nproject: my-project\ntags:\n  - claude/session\n---\n# Title\nBody\n")
        flip_note_status(str(note), "auto-logged", "summarized")
        content = note.read_text()
        assert "project: my-project" in content
        assert "claude/session" in content
        assert "# Title" in content

    def test_flip_note_status_only_changes_frontmatter_not_body(self, tmp_path):
        """Status string in body must not be modified."""
        from obsidian_utils import flip_note_status
        note = tmp_path / "test-note.md"
        note.write_text(
            "---\nstatus: auto-logged\nproject: test\n---\n"
            "The old status: auto-logged was changed.\n"
        )
        flip_note_status(str(note), "auto-logged", "summarized")
        content = note.read_text()
        assert "status: summarized" in content.split("---")[1]  # frontmatter
        assert "status: auto-logged was changed" in content  # body preserved

    def test_flip_note_status_ignores_body_when_frontmatter_differs(self, tmp_path):
        """Body containing old status should not be modified if frontmatter status differs."""
        from obsidian_utils import flip_note_status
        note = tmp_path / "test-note.md"
        note.write_text(
            "---\nstatus: summarized\nproject: test\n---\n"
            "Previously it was status: auto-logged\n"
        )
        result = flip_note_status(str(note), "auto-logged", "summarized")
        assert result is False  # not found in frontmatter
        content = note.read_text()
        assert "Previously it was status: auto-logged" in content  # body untouched

    def test_flip_note_status_returns_false_when_absent(self, tmp_path):
        from obsidian_utils import flip_note_status
        note = tmp_path / "test-note.md"
        note.write_text("---\nstatus: summarized\n---\nContent\n")
        result = flip_note_status(str(note), "auto-logged", "summarized")
        assert result is False

    def test_flip_note_status_returns_false_for_missing_file(self, tmp_path):
        from obsidian_utils import flip_note_status
        result = flip_note_status(str(tmp_path / "nonexistent.md"), "auto-logged", "summarized")
        assert result is False


class TestPathTraversalFilename:
    """Additional path traversal tests for filename and symlink vectors."""

    def test_write_vault_note_blocks_filename_traversal(self, tmp_path):
        from obsidian_utils import write_vault_note
        result = write_vault_note(str(tmp_path), "sessions", "../../../etc/passwd", "evil")
        assert isinstance(result, str)
        assert "traversal" in result.lower() or "outside" in result.lower(), (
            f"Expected traversal error message, got: {result!r}"
        )

    def test_write_vault_note_blocks_absolute_filename(self, tmp_path):
        from obsidian_utils import write_vault_note
        result = write_vault_note(str(tmp_path), "sessions", "/etc/passwd", "evil")
        assert isinstance(result, str)
        assert "traversal" in result.lower() or "outside" in result.lower(), (
            f"Expected traversal error message, got: {result!r}"
        )

    def test_write_vault_note_no_dir_created_on_traversal(self, tmp_path):
        """Traversal check must run BEFORE mkdir to prevent side-effect directory creation."""
        from obsidian_utils import write_vault_note
        evil_dir = tmp_path / ".." / ".." / "evil-dir-test"
        write_vault_note(str(tmp_path), "../../evil-dir-test", "test.md", "payload")
        assert not evil_dir.exists()


class TestHookInputFailsClosed:
    """Every PreToolUse gate DENIES when it cannot read its own input.

    Capping the read (#289) is only half the contract. The other half is what
    happens when the payload is unusable — malformed, or larger than the cap.
    Three of these five hooks used to ``sys.exit(1)`` there, which reads as
    refusal but is not: a non-zero exit is a NON-BLOCKING error in the
    PreToolUse contract, so Claude Code surfaces it and lets the tool call
    proceed. A branch-name gate, a protected-branch push gate and a PR-base
    gate all stepped aside on input they could not parse.

    Blocking is ``exit 0`` carrying ``permissionDecision: "deny"``. This test
    pins that, behaviourally, against the real scripts — the AST guard in
    ``TestStdinCap`` can prove a read is bounded but says nothing about where
    control goes when the bound is hit.
    """

    HOOKS = (
        "require-preflight",
        "enforce-pr-base-branch",
        "prevent-direct-push",
        "update-changelog-before-pr",
        "validate-branch-name",
    )
    CAP = 1_000_000

    @staticmethod
    def _run(hook, payload):
        return subprocess.run(
            [sys.executable, str(Path(".claude/hooks") / f"{hook}.py")],
            input=payload,
            capture_output=True,
            text=True,
            timeout=60,
        )

    @staticmethod
    def _decision(proc):
        try:
            return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]
        except (ValueError, KeyError, TypeError):
            return None

    @pytest.mark.parametrize("hook", HOOKS)
    def test_malformed_payload_denies(self, hook):
        proc = self._run(hook, '{"tool_name":')
        assert proc.returncode == 0, (
            f"{hook} exited {proc.returncode} on malformed input; a non-zero exit "
            "is a non-blocking error and the tool call PROCEEDS"
        )
        assert self._decision(proc) == "deny", (
            f"{hook} did not deny on malformed input: {proc.stdout!r} {proc.stderr!r}"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_oversize_payload_denies(self, hook):
        """An oversize payload whose truncation is still VALID JSON.

        The fixture shape is the whole point, and the obvious one does not
        work: padding inside the document means ``read(CAP + 1)`` tears the
        JSON, the parse fails, and the hook denies via the parse branch — so
        the test passes with the overflow check deleted and proves nothing
        about it (measured: deleting the check left this green).

        A complete document followed by whitespace past the cap truncates to
        something ``json.loads`` accepts. Without an explicit overflow branch
        the hook would parse a TRUNCATED view of the payload and proceed —
        the actual fail-open — so this fixture is what makes the check
        load-bearing rather than cosmetic.
        """
        doc = json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "git status"}}
        )
        payload = doc + " " * (self.CAP + 100 - len(doc))
        assert len(payload) > self.CAP
        assert json.loads(payload[: self.CAP + 1])["tool_name"] == "Bash", (
            "fixture no longer truncates to valid JSON; it would exercise the "
            "parse branch instead of the overflow branch"
        )
        proc = self._run(hook, payload)
        assert proc.returncode == 0, (
            f"{hook} exited {proc.returncode} on an oversize payload; a non-zero "
            "exit is a non-blocking error and the tool call PROCEEDS"
        )
        assert self._decision(proc) == "deny", (
            f"{hook} did not deny an oversize payload: {proc.stdout!r} {proc.stderr!r}"
        )

    # (id, raw bytes). Each one was measured exiting non-zero — i.e. fail-OPEN
    # — on all five hooks before the corresponding guard landed. Kept as bytes
    # because one of them is not decodable text.
    UNUSABLE_PAYLOADS = (
        # Valid JSON, not an object: parses, then `.get()` explodes upstream.
        ("json-null", b"null"),
        ("json-list", b"[]"),
        ("json-string", b'"5"'),
        ("json-int", b"5"),
        ("json-bool", b"true"),
        # Valid JSON object, wrong nested types: parses, survives the top-level
        # dict check, then explodes one line later.
        #   'str' object has no attribute 'get'
        ("tool_input-is-a-string", b'{"tool_name":"Bash","tool_input":"x"}'),
        #   argument of type 'int' is not iterable
        ("command-is-an-int", b'{"tool_name":"Bash","tool_input":{"command":5}}'),
        # Undecodable: raises UnicodeDecodeError from the READ, before any
        # parse, so a try around json.loads alone never sees it.
        ("invalid-utf8", b'{"tool_name":"Bash","tool_input":{}}\xff\xfe'),
        # ~400 KB, far under the cap, but json.loads raises RecursionError —
        # which is not a ValueError, so the old except tuple missed it.
        ("deep-nesting", b"[" * 200_000 + b"]" * 200_000),
    )

    @staticmethod
    def _run_raw(hook, payload: bytes):
        """Byte-level runner. ``_run`` is text-mode and cannot express a
        payload that is not valid UTF-8, which is one of the fixtures."""
        return subprocess.run(
            [sys.executable, str(Path(".claude/hooks") / f"{hook}.py")],
            input=payload,
            capture_output=True,
            timeout=60,
        )

    @pytest.mark.parametrize("hook", HOOKS)
    @pytest.mark.parametrize(
        "payload",
        [p for _, p in UNUSABLE_PAYLOADS],
        ids=[i for i, _ in UNUSABLE_PAYLOADS],
    )
    def test_unusable_payload_denies(self, hook, payload):
        """Everything a gate can be handed that it cannot act on must DENY.

        Four distinct failure modes, deliberately in one table because they
        share one consequence: before the fix each raised out of
        ``_read_hook_input`` (or out of the caller one line later), the hook
        exited non-zero, and a non-zero exit is a NON-blocking error under the
        PreToolUse contract — so the gated command RAN. A payload the gate
        cannot parse is not a payload the gate has cleared.

        Both assertions are load-bearing. Exit 0 on its own is exactly what a
        hook that silently allowed the payload would produce, so the deny
        decision has to be asserted alongside it.
        """
        proc = self._run_raw(hook, payload)
        assert proc.returncode == 0, (
            f"{hook} exited {proc.returncode} on an unusable payload; a non-zero "
            f"exit is a non-blocking error and the tool call PROCEEDS: "
            f"{proc.stderr[-400:]!r}"
        )
        assert self._decision(proc) == "deny", (
            f"{hook} did not deny an unusable payload: "
            f"{proc.stdout[:400]!r} {proc.stderr[-400:]!r}"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_closed_stdin_denies(self, hook):
        """fd 0 CLOSED, which is not the same thing as fd 0 empty.

        ``stdin=subprocess.DEVNULL`` gives an open, immediately-EOF stream and
        exercises nothing: the hook reads ``""`` and denies on the parse. With
        the descriptor actually closed, CPython sets ``sys.stdin`` to ``None``,
        so the failure is an ``AttributeError`` on the attribute access and
        ``read()`` is never entered — invisible to a handler that names
        ``(UnicodeDecodeError, OSError)``, and invisible to a ``try`` around
        ``json.loads`` alone. Measured on all five hooks before the fix:
        ``rc=1, AttributeError: 'NoneType' object has no attribute 'read'``,
        i.e. a non-blocking error, i.e. the gated command runs.

        ``<&-`` in a shell wrapper rather than ``preexec_fn``: it closes the
        descriptor in the child without running Python code between fork and
        exec.
        """
        script = str(Path(".claude/hooks") / f"{hook}.py")
        proc = subprocess.run(
            ["/bin/sh", "-c", 'exec "$0" "$1" <&-', sys.executable, script],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, (
            f"{hook} exited {proc.returncode} with stdin closed; a non-zero exit "
            f"is a non-blocking error and the tool call PROCEEDS: "
            f"{proc.stderr[-400:]!r}"
        )
        assert self._decision(proc) == "deny", (
            f"{hook} did not deny with stdin closed: {proc.stdout[:300]!r}"
        )

    @staticmethod
    def _run_with_stdout_closed(hook, payload):
        """Spawn the hook with fd 1 actually CLOSED.

        ``stdout=subprocess.DEVNULL`` exercises nothing here — DEVNULL is an
        open, writable descriptor, so the decision is written and discarded by
        the kernel, which is indistinguishable from success. The failure being
        tested only exists when there is no fd 1 at all: CPython then sets
        ``sys.stdout`` to ``None``, and ``print()`` on ``None`` is a documented
        NO-OP that does not raise. ``1>&-`` in a shell wrapper closes it
        without running Python between fork and exec.
        """
        script = str(Path(".claude/hooks") / f"{hook}.py")
        return subprocess.run(
            ["/bin/sh", "-c", 'printf %s "$2" | exec "$0" "$1" 1>&-',
             sys.executable, script, payload],
            capture_output=True, text=True, timeout=60,
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_a_deny_that_cannot_reach_stdout_is_not_a_silent_allow(self, hook):
        """The worst failure mode in this file: a deny nobody can hear.

        ``exit 0`` blocks only because of what is ON stdout. Exit 0 with an
        EMPTY stdout is an ALLOW. So a gate that decides "deny" and then cannot
        write it does not fail loudly — it turns itself off, with no traceback,
        no stderr and no non-zero exit. Measured before the fix, fd 1 closed
        and a must-deny payload: all five hooks gave rc=0, 0 bytes of stdout,
        0 bytes of stderr. Silent allow, 5/5.

        Note this got *quieter* when the read handler was broadened: a hook
        spawned with no std fds used to die loudly on `sys.stdin` being None.
        Exit 2 is the fix because it is the one blocking signal that does not
        depend on stdout being writable.
        """
        proc = self._run_with_stdout_closed(hook, "null")
        assert proc.returncode == 2, (
            f"{hook} exited {proc.returncode} when its deny could not be "
            f"written; 0 with empty stdout is an ALLOW: {proc.stderr[-400:]!r}"
        )
        assert proc.stdout == "", f"{hook} wrote to a closed fd 1: {proc.stdout!r}"
        assert "BLOCKED:" in proc.stderr, (
            f"{hook} blocked at exit 2 but gave no reason on stderr, which is "
            f"where an exit-2 reason comes from: {proc.stderr!r}"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_a_deny_into_a_broken_pipe_is_not_a_silent_allow(self, hook):
        """Same failure, reached by buffering rather than by ``None``.

        Here fd 1 exists, so ``print()`` succeeds — into the buffer. With no
        reader left, the actual write happens at interpreter shutdown, long
        after ``sys.exit(0)`` has fixed the exit status, and CPython reports it
        as "Exception ignored on flushing sys.stdout". The explicit
        ``sys.stdout.flush()`` inside the try is what drags that failure back
        to a point where the hook can still act on it; without it this test is
        the only thing that notices.
        """
        read_fd, write_fd = os.pipe()
        try:
            proc = subprocess.Popen(
                [sys.executable, str(Path(".claude/hooks") / f"{hook}.py")],
                stdin=subprocess.PIPE, stdout=write_fd, stderr=subprocess.PIPE,
                text=True,
            )
        finally:
            os.close(write_fd)
        # Drop the read end too: the child now holds the only handle on a pipe
        # with no reader, so its first real write gets EPIPE.
        os.close(read_fd)
        _, err = proc.communicate("null", timeout=60)
        assert proc.returncode == 2, (
            f"{hook} exited {proc.returncode} writing a deny into a broken "
            f"pipe; only exit 2 blocks without stdout: {err[-400:]!r}"
        )
        assert "BLOCKED:" in err, f"{hook} gave no reason on stderr: {err!r}"

    @pytest.mark.parametrize("hook", HOOKS)
    def test_a_deny_with_both_streams_broken_is_not_a_silent_allow(self, hook):
        """`2>&1 |` — stdout AND stderr are the same dead pipe.

        Dropping stdout alone is not enough here: stderr is then the only
        remaining broken stream, its finalization flush fails, and CPython
        overrides the exit status with 120 all the same (measured — both the
        `sys.stdout = None` and the `os.dup2(devnull, 1)` idioms exit 120 in
        this scenario, and 120 is non-blocking). stderr is therefore dropped
        too, after `_warn()` has had its chance at it.
        """
        script = str(Path(".claude/hooks") / f"{hook}.py")
        proc = subprocess.run(
            ["/bin/bash", "-c",
             'printf %s "$2" | python3 "$1" 2>&1 | true; exit ${PIPESTATUS[1]}',
             "bash", script, '{"tool_name":'],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 2, (
            f"{hook} exited {proc.returncode} with both streams broken; only "
            f"exit 2 blocks, and 120 is a non-blocking error"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_the_same_payload_still_denies_normally_at_exit_0(self, hook):
        """Negative control for both tests above.

        Without it, a hook hardwired to `raise SystemExit(2)` would satisfy
        them while blocking every command with an unexplained exit-2 error
        instead of a decision. With stdout available the SAME payload must
        take the normal path: exit 0, a deny on stdout, and no ``BLOCKED:``
        marker — that marker is for the degraded channel only.
        """
        proc = self._run(hook, "null")
        assert proc.returncode == 0, proc.stderr[-300:]
        assert self._decision(proc) == "deny"
        assert "BLOCKED:" not in proc.stderr, (
            f"{hook} took the degraded path with stdout available: "
            f"{proc.stderr!r}"
        )

    def test_a_payload_exactly_at_the_cap_is_not_denied(self):
        """Boundary control for ``len(raw) > _STDIN_CAP``, at the threshold.

        The oversize fixture above sits at ``CAP + 100``: a wide gap, which
        pins the deny side but says nothing about where the edge is. Flipping
        ``>`` to ``>=`` left the whole suite green (measured) while denying
        every legitimate payload of exactly 1 MB. These two rows — CAP allowed,
        CAP + 1 denied — are what make the operator itself load-bearing.
        """
        doc = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        payload = (doc + " " * (self.CAP - len(doc))).encode()
        assert len(payload) == self.CAP
        for hook in self.HOOKS:
            proc = self._run_raw(hook, payload)
            assert proc.returncode == 0, f"{hook}: {proc.stderr[-300:]!r}"
            assert self._decision(proc) != "deny", (
                f"{hook} denied a payload of exactly {self.CAP} bytes, which is "
                f"within the cap: {proc.stdout[:300]!r}"
            )

    def test_a_payload_one_byte_over_the_cap_is_denied(self):
        """The other half of the boundary: CAP + 1 exactly, still valid JSON.

        Valid JSON matters — an oversize payload that is also unparseable
        would deny via the parse branch and prove nothing about the length
        check.
        """
        doc = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        payload = (doc + " " * (self.CAP + 1 - len(doc))).encode()
        assert len(payload) == self.CAP + 1
        assert json.loads(payload)["tool_name"] == "Bash"
        for hook in self.HOOKS:
            proc = self._run_raw(hook, payload)
            assert proc.returncode == 0, f"{hook}: {proc.stderr[-300:]!r}"
            assert self._decision(proc) == "deny", (
                f"{hook} did not deny a payload of {self.CAP + 1} bytes: "
                f"{proc.stdout[:300]!r}"
            )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_a_readable_benign_payload_is_not_denied(self, hook):
        """Negative control. Without it, a hook hardcoded to deny everything
        would satisfy every test above while breaking every command.

        The payload must be a **Bash** one. All five hooks are registered with
        ``"matcher": "Bash"`` in .claude/settings.json, so a non-Bash payload
        is never delivered in production — a control built on one exercises a
        path that cannot occur, and a hook that denied every real Bash command
        would still pass it.
        """
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        proc = self._run(hook, payload)
        assert proc.returncode == 0
        assert self._decision(proc) != "deny", (
            f"{hook} denied a benign Bash command: {proc.stdout!r}"
        )

    def test_the_hooks_are_registered_for_bash(self):
        """Pins the premise the control above rests on. If a hook is ever
        re-registered under a different matcher, the control silently stops
        describing production and this fails instead."""
        settings = json.loads(Path(".claude/settings.json").read_text())
        registered = {}
        for entry in settings["hooks"]["PreToolUse"]:
            for h in entry["hooks"]:
                for hook in self.HOOKS:
                    if f"{hook}.py" in h["command"]:
                        registered.setdefault(hook, set()).add(entry["matcher"])
        assert set(registered) == set(self.HOOKS), (
            f"hooks missing from .claude/settings.json: "
            f"{set(self.HOOKS) - set(registered)}"
        )
        assert all(m == {"Bash"} for m in registered.values()), registered


class TestHookBlockingPathsFire:
    """The five gates' BUSINESS deny paths — the ones they exist for.

    Nothing covered these before. `.github/workflows/ci.yml` excludes
    `.claude/hooks/*` from CI's diff scan, and `tests/` never executed a gated
    command through them, so the push gate, the branch-name gate, the PR-base
    gate, the preflight gate and the changelog gate were all held up by manual
    testing alone. #289 then re-pointed every one of them at a shared
    `_read_hook_input()` helper and (in three) swapped an inline output block
    for `_deny()` — i.e. it moved exactly these paths, with no net underneath.

    The fixture is a THROWAWAY repo, not this checkout, and that is not
    fastidiousness: probing in-tree reports `update-changelog-before-pr` as
    ALLOW, because this branch happens to carry entries under `[Unreleased]`.
    The same test would flip to red on a branch that does not. A fixture whose
    verdict depends on the developer's working tree is worse than none.

    Command strings are assembled from fragments on purpose: this repo's live
    PreToolUse hooks inspect unexecuted command text, so a literal
    protected-branch push string in this file blocks the tooling that reads it.
    """

    HOOKS = TestHookInputFailsClosed.HOOKS

    @staticmethod
    def _repo(tmp_path):
        """A hermetic git repo on a feature branch with the hooks copied in."""
        work = tmp_path / "probe"
        shutil.copytree(Path(".claude/hooks"), work / ".claude/hooks")
        env = {
            k: v for k, v in os.environ.items()
            # GIT_DIR/GIT_WORK_TREE in the ambient env override cwd entirely and
            # would silently point every git call back at the real repo.
            if not k.startswith(("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX"))
        }
        env.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
            CLAUDE_PROJECT_DIR=str(work),
        )
        for args in (
            ["init", "-q", "-b", "feature/probe"],
            ["-c", "user.email=t@example.invalid", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-m", "seed"],
        ):
            subprocess.run(["git", "-C", str(work), *args], env=env,
                           check=True, capture_output=True)
        return work, env

    @classmethod
    def _decision_and_reason(cls, work, env, hook, command):
        """(decision, reason) from ONE run.

        Some rows cannot be told apart by the verdict alone: every malformed
        token denies, so a token gate that ignored `expires` entirely and
        denied for the wrong reason would pass a verdict-only assertion (#327).
        One run, because the gate DELETES a token it rejects — a second run
        would see no token at all and block for a different reason.
        """
        proc = subprocess.run(
            [sys.executable, str(work / ".claude/hooks" / f"{hook}.py")],
            input=json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": command}}),
            capture_output=True, text=True, timeout=60, cwd=work, env=env,
        )
        if proc.returncode == 2:
            return "deny", proc.stderr
        try:
            output = json.loads(proc.stdout)["hookSpecificOutput"]
        except (ValueError, KeyError, TypeError):
            return "allow", ""
        decision = output.get("permissionDecision", "allow")
        return decision, output.get("permissionDecisionReason", "")

    @classmethod
    def _decide(cls, work, env, hook, command):
        proc = subprocess.run(
            [sys.executable, str(work / ".claude/hooks" / f"{hook}.py")],
            input=json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": command}}),
            capture_output=True, text=True, timeout=60, cwd=work, env=env,
        )
        assert proc.returncode == 0, (
            f"{hook} exited {proc.returncode}; a non-zero exit is a non-blocking "
            f"error and the command PROCEEDS: {proc.stderr[-400:]!r}"
        )
        try:
            return json.loads(proc.stdout)["hookSpecificOutput"].get(
                "permissionDecision", "allow"
            )
        except (ValueError, KeyError, TypeError):
            return "allow"

    # (hook, command, expected). Fragments, see the class docstring.
    CASES = (
        ("prevent-direct-push", "git pu" + "sh origin ma" + "in", "deny"),
        ("prevent-direct-push", "git pu" + "sh origin feature/probe", "allow"),
        ("validate-branch-name", "git chec" + "kout -b nonsense-branch", "deny"),
        ("validate-branch-name", "git chec" + "kout -b release/1.0.0", "deny"),
        ("validate-branch-name", "git chec" + "kout -b feature/ok", "allow"),
        # feature/probe with no --base defaults to main on GitHub's side.
        ("enforce-pr-base-branch", "gh pr cre" + "ate --title x", "deny"),
        ("enforce-pr-base-branch", "gh pr cre" + "ate --base ma" + "in", "deny"),
        ("enforce-pr-base-branch", "gh pr cre" + "ate --base develop", "allow"),
        # No preflight token exists for a throwaway CLAUDE_PROJECT_DIR, and the
        # real repo's token is neither read nor consumed (the path is a hash of
        # that directory).
        ("require-preflight", "git com" + "mit -m wip", "deny"),
        ("require-preflight", "git status", "allow"),
    )

    @pytest.mark.parametrize("hook,command,expected", CASES)
    def test_gate_decision(self, tmp_path, hook, command, expected):
        work, env = self._repo(tmp_path)
        assert self._decide(work, env, hook, command) == expected

    def test_changelog_gate_blocks_a_pr_with_no_unreleased_entries(self, tmp_path):
        """Split out because its verdict depends on a file, not the command —
        which is exactly why an in-tree probe of it is meaningless."""
        work, env = self._repo(tmp_path)
        cmd = "gh pr cre" + "ate --base develop"
        changelog = work / "CHANGELOG.md"

        changelog.write_text("# Changelog\n\n## [Unreleased]\n\n## [1.0.0]\n\nnothing\n")
        assert self._decide(work, env, "update-changelog-before-pr", cmd) == "deny"

        changelog.unlink()
        assert self._decide(work, env, "update-changelog-before-pr", cmd) == "deny"

        changelog.write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Added\n- a real entry\n"
        )
        assert self._decide(work, env, "update-changelog-before-pr", cmd) == "allow"


class TestScopeGuardCannotBeBypassed:
    """`_targets_this_project()` must not hand an attacker an off switch (#326).

    The guard exists so a gate stands down for a command aimed at a DIFFERENT
    checkout. It found the `cd` with a bare `re.search`, i.e. anywhere in the
    command — including AFTER the dangerous verb, where it cannot possibly
    change where that verb acted. Appending `; cd /nonexistent` to any command
    therefore turned every gate in this repo off: returning False here exits 0
    with nothing on stdout, and in the PreToolUse contract that is an ALLOW.

    Two things the fixture must get right or it proves nothing:

    * `CLAUDE_PROJECT_DIR` MUST be set. Unset, the helper returns its fail-safe
      `True`, every arm denies identically, and the bypass looks absent. This
      is how the finding was initially mis-refuted.
    * The bare verb must be probed alongside the bypass shape in the same test,
      so a hook that denies EVERYTHING is not mistaken for a fixed one — and
      the out-of-scope shape must be probed too, so "always in scope" is not
      mistaken for a fix either (`test_legitimate_descoping_still_allows`).

    Command strings are assembled from fragments on purpose: this repo's live
    PreToolUse hooks inspect unexecuted command text, so a literal
    protected-branch push string in this file blocks the tooling that reads it.
    """

    HOOKS = TestHookInputFailsClosed.HOOKS

    # hook -> a command shape that hook's business logic DENIES in the fixture
    # repo of TestHookBlockingPathsFire._repo (feature/probe, no preflight
    # token, no CHANGELOG.md).
    VERBS = {
        "require-preflight": "git com" + "mit -m wip",
        "prevent-direct-push": "git pu" + "sh origin ma" + "in",
        "validate-branch-name": "git chec" + "kout -b nonsense-branch",
        "enforce-pr-base-branch": "gh pr cre" + "ate --base ma" + "in",
        "update-changelog-before-pr": "gh pr cre" + "ate --base develop",
    }

    @staticmethod
    def _decide(work, env, hook, command):
        """Run one hook and return "deny"/"allow".

        Deliberately NOT TestHookBlockingPathsFire._decide: that one requires
        rc 0, and one of the shapes here (a NUL byte in the `cd` target) used
        to raise ValueError out of os.path.realpath for rc 1. rc 1 is a
        NON-BLOCKING error — the command proceeds — so it must never be read
        as a refusal; rc 2 IS blocking and is accepted as a deny.
        """
        proc = subprocess.run(
            [sys.executable, str(work / ".claude/hooks" / f"{hook}.py")],
            input=json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": command}}),
            capture_output=True, text=True, timeout=60, cwd=work, env=env,
        )
        assert proc.returncode in (0, 2), (
            f"{hook} exited {proc.returncode}; only 0 (with a deny decision) and 2 "
            f"block. Anything else is a non-blocking error and the command "
            f"PROCEEDS: {proc.stderr[-400:]!r}"
        )
        if proc.returncode == 2:
            return "deny"
        try:
            return json.loads(proc.stdout)["hookSpecificOutput"].get(
                "permissionDecision", "allow"
            )
        except (ValueError, KeyError, TypeError):
            return "allow"

    @pytest.fixture
    def probe(self, tmp_path):
        """(work, env, elsewhere) — the gated repo plus a real dir outside it."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        return work, env, str(elsewhere)

    @pytest.mark.parametrize("hook", HOOKS)
    def test_trailing_cd_does_not_disarm_the_gate(self, probe, hook):
        """`<verb> ; cd /nonexistent` must deny — the control denies too."""
        work, env, _ = probe
        verb = self.VERBS[hook]
        assert self._decide(work, env, hook, verb) == "deny", (
            f"{hook} did not deny the bare verb; the fixture cannot discriminate"
        )
        assert self._decide(work, env, hook, verb + " ; cd /nonexistent") == "deny", (
            f"{hook} was disarmed by a trailing cd — a cd AFTER the verb cannot "
            f"change where the verb acted"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_legitimate_descoping_still_allows(self, probe, hook):
        """Negative control: a real `cd` to another checkout BEFORE the verb
        must still stand the gate down. Without this, "always in scope" would
        satisfy every other test in this class."""
        work, env, elsewhere = probe
        command = f"cd {elsewhere} && " + self.VERBS[hook]
        assert self._decide(work, env, hook, command) == "allow", (
            f"{hook} gated a command aimed at {elsewhere}, which is not this project"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_a_trailing_cd_does_not_re_scope_a_legitimately_descoped_command(self, probe, hook):
        """`cd /other && <verb> && cd -` must still ALLOW.

        This is the ONLY fixture in this class that reaches the position check
        — the `start < position` filter that IS the #326 fix. Every other shape
        is short-circuited before it: with the filter deleted, a trailing `cd`
        is admitted as "preceding", but then `cmd[cd_end:position]` is an empty
        slice, so `connectors` is empty, the `&&`-only rule returns "in scope",
        and the verdict is unchanged. Delete `start < position` without this
        test and the suite stays green while the original bypass is back.

        Here the mutant is observably wrong in the other direction: it picks
        the TRAILING cd as the governing one, the empty slice sends it down the
        same fail-closed path, and a command that provably ran in another
        checkout is gated. Asserting ALLOW is what catches that.

        `cd -` is the realistic form — the idiom for returning to the previous
        directory after doing work elsewhere — so this shape is not contrived.
        """
        work, env, elsewhere = probe
        verb = self.VERBS[hook]
        for trailing in ("cd -", f"cd {elsewhere}"):
            command = f"cd {elsewhere} && {verb} && {trailing}"
            assert self._decide(work, env, hook, command) == "allow", (
                f"{hook} gated a command that ran in {elsewhere}; a cd AFTER the "
                f"verb ({trailing!r}) must not decide where the verb ran"
            )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_last_cd_before_the_verb_wins(self, probe, hook):
        """`cd /other && cd <project> && <verb>` runs in <project> — deny."""
        work, env, elsewhere = probe
        command = f"cd {elsewhere} && cd {work} && " + self.VERBS[hook]
        assert self._decide(work, env, hook, command) == "deny", (
            f"{hook} honoured a superseded cd; shell semantics are last-write-wins"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_second_verb_occurrence_back_in_scope_denies(self, probe, hook):
        """`cd /other && <verb> ; <verb>` must be gated.

        Not because `;` resets the cwd — it does not — but because the `cd`
        can FAIL. `&&` then short-circuits the first occurrence while `;` runs
        the second one anyway, in the session cwd, i.e. here. Whether the `cd`
        succeeds is unknowable from the command text, so only an unbroken run
        of `&&` between a `cd` and a verb proves the verb ran elsewhere.
        """
        work, env, elsewhere = probe
        verb = self.VERBS[hook]
        command = f"cd {elsewhere} && {verb} ; {verb}"
        assert self._decide(work, env, hook, command) == "deny", (
            f"{hook} let one out-of-scope occurrence descope the whole command"
        )

    # Prefixes containing a `cd` the shell never executes as a directory
    # change: quoted text, a command substitution, a backtick substitution.
    INERT_CD_PREFIXES = (
        'echo "x; cd /tmp" && ',
        "echo 'x; cd /tmp' && ",
        "X=$(true; cd /tmp) && ",
        "X=`true; cd /tmp` && ",
    )

    @pytest.mark.parametrize("prefix", INERT_CD_PREFIXES)
    @pytest.mark.parametrize("hook", HOOKS)
    def test_a_cd_that_never_executes_does_not_disarm_the_gate(self, probe, hook, prefix):
        """A `cd` inside quotes or a substitution is TEXT, not a cwd change.

        The `cd` regex matches raw command text, so it honoured a `cd` inside
        "..." / '...' / $(...) / `...` — none of which move the shell. The verb
        then ran HERE while the guard concluded it ran elsewhere, and every one
        of these four shapes disarmed all five gates. Pre-existing (develop's
        first-cd-anywhere search allows them too), but the guard's own claim is
        that descoping is PROVABLE, and a `cd` that never ran proves nothing.
        """
        work, env, _ = probe
        command = prefix + self.VERBS[hook]
        assert self._decide(work, env, hook, command) == "deny", (
            f"{hook} was disarmed by a cd the shell would never execute: {prefix!r}"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_a_line_continuation_is_whitespace_not_a_separator(self, probe, hook):
        """`cd /other && \\<newline> <verb>` must still ALLOW.

        A backslash-newline is a line continuation — whitespace — but the bare
        `\\n` reached the `&&`-only connector rule, failed it, and re-scoped a
        command that genuinely ran elsewhere. `cd /other/repo && \\` is routine,
        so this denied real cross-repo work and looked like the other repo's
        fault. A BARE newline is left alone: that one IS a separator, and a
        failed `cd` before it runs the verb here.
        """
        work, env, elsewhere = probe
        command = f"cd {elsewhere} && \\\n  " + self.VERBS[hook]
        assert self._decide(work, env, hook, command) == "allow", (
            f"{hook} gated a line-wrapped command that ran in {elsewhere}"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_an_over_captured_cd_target_leaves_the_command_in_scope(self, probe, hook):
        """`cd /other;<verb>` — no space after the `;` — must DENY.

        The bare-target group is `\\S+`, so it swallows the separator too and
        captures `/other;git`. That puts the end of the `cd` match PAST the
        verb, making the connector slice empty, and the `not connectors` arm of
        the connector rule is the only thing that keeps this gated — nothing
        else in this class reaches it. Deny is also the right answer on its own
        terms: a `;` does not prove the `cd` succeeded.
        """
        work, env, elsewhere = probe
        command = f"cd {elsewhere};" + self.VERBS[hook]
        assert self._decide(work, env, hook, command) == "deny", (
            f"{hook} descoped a command whose cd target was mis-captured"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_subshell_cd_descopes_only_within_that_subshell(self, probe, hook):
        """`(cd /other && <verb>)` allows; the same verb after the `)` denies.

        The user's global CLAUDE.md mandates `(cd dir && cmd)` over a bare
        `cd`, so gating that shape denied a documented-legit way to work in
        another checkout. A subshell's cwd is real — but only until the `)`.
        Admitting a leading `(` without checking the subshell is still open
        would just move the bypass: `(cd /other) && <verb>` runs the verb here.
        """
        work, env, elsewhere = probe
        verb = self.VERBS[hook]
        assert self._decide(work, env, hook, f"(cd {elsewhere} && {verb})") == "allow", (
            f"{hook} gated a subshell command that ran in {elsewhere}"
        )
        for escaped in (
            f"(cd {elsewhere} && {verb}) && {verb}",
            f"(cd {elsewhere} && true) && {verb}",
            f"X=$(cd {elsewhere} && pwd) && {verb}",
        ):
            assert self._decide(work, env, hook, escaped) == "deny", (
                f"{hook} let a closed subshell descope a verb outside it: {escaped!r}"
            )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_a_cd_inside_a_substitution_never_descopes(self, probe, hook):
        """`X=$(cd /other && <verb>)` must DENY — deliberately over-strict.

        Here the verb really does run in /other: it is inside the same
        substitution subshell as the `cd`. The guard refuses to reason about
        that anyway and treats any `cd` inside `$( )` or backticks as no
        evidence at all, because a substitution's interior is where quoting and
        nesting are least reliably modelled by a regex-and-scanner pair, and
        the cost of being wrong in the other direction is a gate that does not
        fire. Gating an exotic shape costs a false denial; trusting it costs
        the gate. Without this assertion the whole `$( )` distinction is
        vacuous — the still-inside-the-subshell check already handles the
        escaping shape `X=$(cd /other) && <verb>`.
        """
        work, env, elsewhere = probe
        verb = self.VERBS[hook]
        for command in (
            f"X=$(cd {elsewhere} && {verb})",
            f"X=`cd {elsewhere} && {verb}`",
        ):
            assert self._decide(work, env, hook, command) == "deny", (
                f"{hook} descoped on a cd inside a substitution: {command!r}"
            )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_nul_byte_in_cd_target_denies_instead_of_crashing(self, probe, hook):
        """A NUL in the `cd` target raised ValueError out of os.path.realpath.

        Uncaught, that is a traceback and rc 1 — a NON-blocking error, so the
        gated command ran. An unresolvable target must read as IN scope.
        _decide() rejects any rc but 0 and 2, which is the assertion that
        matters here.
        """
        work, env, _ = probe
        command = "cd /tmp/\x00x && " + self.VERBS[hook]
        assert self._decide(work, env, hook, command) == "deny", (
            f"{hook} did not deny an unresolvable cd target"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_prefix_sibling_directory_is_out_of_scope(self, probe, hook):
        """DELIBERATE LOOSENING (#326 F3): `<project>-evil` is a different repo.

        A bare `startswith(project_dir)` read a sibling that merely shares a
        path prefix as inside this project, so these commands used to DENY.
        With the separator-anchored compare they ALLOW, which is correct
        scoping — and is the one place this change relaxes a gate rather than
        tightening it.
        """
        work, env, _ = probe
        command = f"cd {work}-evil && " + self.VERBS[hook]
        assert self._decide(work, env, hook, command) == "allow", (
            f"{hook} treated the sibling {work}-evil as part of this project"
        )

    def test_pr_base_gate_scope_checks_only_after_matching_a_verb(self):
        """The PR-base gate's scope check used to sit at module level, ahead of
        every verb guard, so it evaluated on EVERY Bash tool call in this repo
        — which is what gave the NUL-byte crash its widest blast radius. Each
        verb branch now runs its own check, so the guard only executes for a
        command the gate was going to judge anyway."""
        src = Path(".claude/hooks/enforce-pr-base-branch.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        def scope_calls(node):
            return [
                n for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "_targets_this_project"
            ]

        guarded = []
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test_src = ast.get_source_segment(src, node.test) or ""
                # The verb is matched through _verb_occurrences() since #327,
                # so that both the gate and the scope check below it use one
                # matcher and cannot disagree about where the verb is.
                if "_verb_occurrences" in test_src and "_GH_PR_" in test_src:
                    guarded.extend(scope_calls(node))

        assert len(scope_calls(tree)) == len(guarded) == 2, (
            "every _targets_this_project call must sit inside a branch that has "
            "already matched a gh pr verb; found "
            f"{len(scope_calls(tree))} call(s), {len(guarded)} of them guarded"
        )

    def test_all_five_copies_of_the_helper_are_identical(self):
        """The helper is hand-duplicated into all five hooks because
        /harden-repo installs each file standalone into other repos, where no
        shared module exists to import. Duplication is therefore deliberate —
        and drift between the copies is how one gate silently keeps a bug the
        others were fixed for.

        The comparison follows the guard's whole call graph rather than just
        its entry point: `_targets_this_project` delegates the "would the shell
        actually run this cd" question to `_shell_scan`, and drift in one copy
        of THAT is exactly as silent. The closure is derived, not listed, so a
        helper added later is covered without editing this test.
        """
        closures = {}
        for hook in self.HOOKS:
            path = Path(".claude/hooks") / f"{hook}.py"
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
            defined = {
                n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)
            }
            for root in ("_targets_this_project", "_argument_span"):
                assert root in defined, f"{path} defines no {root}()"

            # Two roots, not one: `_argument_span` is hand-duplicated into all
            # five hooks like the rest of this block, but nothing in
            # `_targets_this_project`'s call graph reaches it, so a copy that
            # drifted would change which text one gate reads as a command's
            # arguments — and no test would notice.
            closure, pending = {}, ["_targets_this_project", "_argument_span"]
            while pending:
                name = pending.pop()
                if name in closure:
                    continue
                node = defined[name]
                closure[name] = ast.get_source_segment(src, node)
                pending.extend(
                    call.func.id for call in ast.walk(node)
                    if isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in defined
                )

            # Module-level names the closure READS are part of it: a
            # _TRANSPARENT_TOKENS that drifts in one copy changes where that
            # copy thinks a verb is a command, as silently as a drifted body.
            # Names the closure BINDS are subtracted first — a local called
            # `match` must not drag in a module-level `match` that happens to
            # share its name.
            read, bound = set(), set()
            for name in closure:
                read.update(
                    n.id for n in ast.walk(defined[name])
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                )
                bound.update(
                    n.id for n in ast.walk(defined[name])
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
                )
                bound.update(
                    a.arg for a in ast.walk(defined[name]) if isinstance(a, ast.arg)
                )
            read -= bound
            for stmt in tree.body:
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name) and target.id in read:
                            closure[target.id] = ast.get_source_segment(src, stmt)
            closures[hook] = closure

        reference_hook = self.HOOKS[0]
        reference = closures[reference_hook]
        for hook, closure in closures.items():
            assert set(closure) == set(reference), (
                f"{hook}.py's guard depends on {sorted(set(closure))}, "
                f"{reference_hook}.py's on {sorted(set(reference))}"
            )
            for name, source in closure.items():
                assert source == reference[name], (
                    f"{hook}.py's {name}() has drifted from {reference_hook}.py's"
                )

class TestScopeGuardFailsClosedWhenScopeIsUnknowable:
    """The guard's fail-safes, exercised on the function itself.

    Four of its `return True`s cannot be reached through the hooks as wired
    today — every caller passes a verb pattern that matches at least what it
    matched itself, and CLAUDE_PROJECT_DIR is always set in real operation.
    They exist because the alternative to each is `return False`, and False
    means the hook exits 0 with no decision, which the PreToolUse contract
    reads as ALLOW. Unreachable today is not the same as unreachable after the
    next edit to a caller, so they are pinned here rather than left to a
    mutation that nothing kills.

    The function is loaded by extracting its source, so this exercises the
    shipped text, not a copy that can drift from it.
    """

    @staticmethod
    def _helper(os_module=os):
        """Load `_targets_this_project` and everything it reads, from the file.

        The whole call closure comes along, not just the entry point: the
        guard delegates "where would the shell be" to `_shell_scan` and "where
        would the verb actually run" to `_verb_occurrences`, and a namespace
        missing either raises NameError inside the function under test, which
        is a traceback, rc 1, and non-blocking. Module-level names the closure
        reads travel with it for the same reason. Both sets are derived from
        the AST, so a helper added later needs no edit here.
        """
        src = Path(".claude/hooks/require-preflight.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        defined = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

        closure, pending = {}, ["_targets_this_project"]
        while pending:
            name = pending.pop()
            if name in closure:
                continue
            node = defined[name]
            closure[name] = node
            pending.extend(
                call.func.id for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in defined
            )

        read, bound = set(), set()
        for node in closure.values():
            read.update(
                n.id for n in ast.walk(node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            )
            bound.update(
                n.id for n in ast.walk(node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
            )
            bound.update(a.arg for a in ast.walk(node) if isinstance(a, ast.arg))
        read -= bound
        namespace = {"os": os_module, "re": re}
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in read for t in stmt.targets
            ):
                exec(ast.get_source_segment(src, stmt), namespace)  # noqa: S102
        for node in closure.values():
            exec(ast.get_source_segment(src, node), namespace)  # noqa: S102
        return namespace["_targets_this_project"]

    def test_verb_the_caller_matched_but_this_cannot_find_is_in_scope(self, monkeypatch, tmp_path):
        """Two matchers disagreeing must gate, not wave through.

        With no occurrence to place, there is nothing to prove out of scope —
        and falling through to the "every occurrence is elsewhere" return would
        allow a command on the strength of a verb it could not even locate.
        """
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        helper = self._helper()
        assert helper(f"cd {tmp_path}-elsewhere && git com" + "mit", r"git pu" + "sh") is True

    def test_unusable_verb_pattern_is_in_scope(self, monkeypatch, tmp_path):
        """A malformed verb regex must not escape as an exception: an uncaught
        re.error is a traceback, rc 1, and rc 1 is non-blocking."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        helper = self._helper()
        assert helper(f"cd {tmp_path}-elsewhere && git com" + "mit", "git com" + "mit(") is True

    def test_missing_project_dir_is_in_scope(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        helper = self._helper()
        assert helper(f"cd {tmp_path} && git com" + "mit", r"git com" + "mit") is True

    def test_unresolvable_project_dir_is_in_scope(self, tmp_path):
        """os.path.realpath raises ValueError on an embedded NUL — on the
        PROJECT dir as readily as on the cd target, and this one is evaluated
        for every command the hook sees.

        Injected through an os shim, not monkeypatch.setenv: os.environ itself
        rejects a NUL, so setenv raises before the helper is ever called. That
        makes this branch unreachable in real operation and its except clause
        insurance — but the cost of omitting it is a traceback, rc 1, and a
        non-blocking error on EVERY command the hook sees.
        """
        shim = types.SimpleNamespace(
            environ={"CLAUDE_PROJECT_DIR": f"{tmp_path}/\x00x"},
            path=os.path,
            sep=os.sep,
        )
        helper = self._helper(shim)
        assert helper(f"cd {tmp_path}-elsewhere && git com" + "mit", r"git com" + "mit") is True


class TestVerbMatchingAndRefspecPushes:
    """#327: what makes a command "a push", and what makes it "to main".

    Three defects of one shape — a gate deciding by substring what the shell
    decides by grammar.

    * `prevent-direct-push` built a refspec-aware list of protected targets
      (`":main"`, `":develop"`) under a comment claiming refspec coverage, and
      then guarded it with a strictly NARROWER `if` that no refspec form could
      satisfy. Every refspec push fell through to allow, force ones included.
    * All three git gates matched their verb as a literal substring. Git
      accepts global options between the executable and the subcommand, so
      `git -C . <verb>` contains no such substring and the hook exited 0
      before any other logic ran.
    * `"origin main" in command` is a prefix test, so `origin maintenance`
      denied, as did any command merely QUOTING a push.

    Measured hermetically before the fix, on a feature branch: 7 refspec/force
    forms allowed, 5 global-option shapes allowed, 3 false denies.

    The fixture is TestHookBlockingPathsFire._repo, a throwaway repo on
    `feature/probe`. Probing from `develop` would make
    `current_branch in ["main", "develop"]` deny every shape regardless of its
    form — control and case cannot diverge, and every verdict is then the
    same verdict. Each deny case re-probes an allow control in the SAME repo
    so that a hook which denies everything cannot pass as a fixed one.

    Command strings are assembled from fragments on purpose: this repo's live
    PreToolUse hooks inspect unexecuted command text, so a literal
    protected-branch push string in this file blocks the tooling that reads it.
    """

    PUSH = "git pu" + "sh"
    MAIN = "ma" + "in"
    DEV = "deve" + "lop"
    CONTROL_ALLOW = PUSH + " origin feature/probe"

    # F1 — every one of these pushes a protected branch without ever saying
    # "origin main"; all seven were allowed.
    REFSPEC_DENY = (
        PUSH + " origin " + MAIN,                      # control: this DID deny
        PUSH + " origin HEAD:" + MAIN,
        PUSH + " origin HEAD:" + DEV,
        PUSH + " --force origin HEAD:" + MAIN,
        PUSH + " origin mybranch:" + MAIN,
        PUSH + " origin +" + MAIN + ":" + MAIN,
        PUSH + " --force-with-lease origin HEAD:" + MAIN,
        PUSH + " origin HEAD:refs/heads/" + MAIN,
    )

    # F2 — a global option between `git` and the subcommand, in each of the
    # three gates that matched its verb as a literal.
    GLOBAL_OPTION_DENY = (
        ("prevent-direct-push", "git -C . pu" + "sh origin " + MAIN),
        ("prevent-direct-push", "git -c user.name=x pu" + "sh origin " + MAIN),
        ("prevent-direct-push", "git --no-pager pu" + "sh origin " + MAIN),
        ("prevent-direct-push",
         "git -C . -c user.name=x pu" + "sh origin HEAD:" + MAIN),
        # A long option run, which is where the obvious way to write this
        # pattern collapses. `-{1,2}` splits every long option two ways, so a
        # run that never reaches the subcommand backtracks exponentially
        # (measured: 26 options, 16.7s of CPU inside the gate, on command text
        # written by whoever is being gated). Capping the repetition caps that
        # cost and hands back a bypass — one option past the cap and the gate
        # stops matching. This row is 25 of them, and it must still deny.
        #
        # It is a CORRECTNESS row and nothing more. The blowup needs options
        # that never reach a subcommand, and this row reaches one, so it runs
        # in 0.0000s either way. The cost is asserted by
        # TestTheVerbPatternIsLinear below, which bounds the time instead of
        # the verdict (#327).
        ("prevent-direct-push",
         "git " + "-c user.name=x " * 25 + "pu" + "sh origin " + MAIN),
        ("validate-branch-name", "git -C . chec" + "kout -b bogus"),
        ("require-preflight", "git -C . com" + "mit -m x"),
    )

    # F3/F4 — shapes that are not a push to a protected branch and denied
    # anyway, plus the negative controls that keep the fix from being
    # "match anything containing push".
    NEGATIVE_ALLOW = (
        PUSH + " origin " + MAIN + "tenance",       # F4: whole ref, not prefix
        PUSH + " origin foo:" + MAIN + "line",      # F4: the other side
        PUSH + " origin feature/" + MAIN,           # F4: a ref merely ending in it
        "echo " + PUSH + " origin " + MAIN,         # F3: an argument, not a command
        'echo "' + PUSH + " origin " + MAIN + '"',  # F3: quoted
        "grep -r '" + PUSH + " origin " + MAIN + "' .",
        # A separator INSIDE the quotes. Without this row the quoted-state
        # check is shadowed: for `echo "<push>"` the command-position walk
        # already rejects the occurrence on its own, so that guard could be
        # deleted with the suite still green (#326 shipped two guards like
        # that). Here the `;` makes the position look like a command start,
        # and only "this is inside quotes" saves it.
        'echo "; ' + PUSH + " origin " + MAIN + '"',
        "git pu" + "shd /tmp",                      # not the push verb
        PUSH + " origin v1.2.3",                    # tag push, allowed before too
        PUSH + " origin feature/probe",
    )

    def test_a_leading_plus_is_a_force_push_to_the_same_ref(self, tmp_path):
        """`+main` with no colon.

        Every other row carrying a `+` also carries a `:`, and the
        `rsplit(":", 1)` branch strips the `+` along with everything before the
        colon — so `\\+?` in the ref pattern never decided anything and could be
        deleted with the suite still green (#327). `git push origin +main` is
        the real force-push spelling that reaches it.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        push = "git pu" + "sh origin "
        main = "ma" + "in"
        assert decide(work, env, "prevent-direct-push",
                      push + "+" + main) == "deny"
        assert decide(work, env, "prevent-direct-push",
                      push + "+develop") == "deny"
        # The control: a leading `+` does not make every ref protected.
        assert decide(work, env, "prevent-direct-push",
                      push + "+feature/probe") == "allow"
        assert decide(work, env, "prevent-direct-push",
                      push + "+" + main + "tenance") == "allow"

    @pytest.mark.parametrize("command", REFSPEC_DENY)
    def test_a_refspec_push_to_a_protected_branch_denies(self, tmp_path, command):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        assert decide(work, env, "prevent-direct-push", command) == "deny", (
            f"a protected branch was pushed without the gate objecting: {command!r}"
        )
        assert decide(work, env, "prevent-direct-push", self.CONTROL_ALLOW) == "allow", (
            "the fixture denies a feature-branch push too, so it cannot tell a "
            "fixed gate from one that denies everything"
        )

    @pytest.mark.parametrize("hook,command", GLOBAL_OPTION_DENY)
    def test_a_global_git_option_does_not_evade_the_verb_guard(
        self, tmp_path, hook, command
    ):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        assert decide(work, env, hook, command) == "deny", (
            f"{hook} never saw this command: git takes global options before "
            f"the subcommand, so the literal verb is not in it: {command!r}"
        )
        assert decide(work, env, hook, "git status") == "allow", (
            f"{hook} denies an unrelated command, so this fixture proves nothing"
        )

    @pytest.mark.parametrize("command", NEGATIVE_ALLOW)
    def test_shapes_that_are_not_a_protected_push_allow(self, tmp_path, command):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        assert decide(work, env, "prevent-direct-push", command) == "allow", (
            f"false deny — this is not a push to a protected branch: {command!r}"
        )
        assert decide(work, env, "prevent-direct-push",
                      self.PUSH + " origin " + self.MAIN) == "deny", (
            "the fixture allows a real protected push, so it cannot tell a "
            "fixed gate from a disabled one"
        )

    def test_a_quoted_verb_still_denies_when_it_is_also_a_real_command(self, tmp_path):
        """The quoted-mention allowance must not disarm the command beside it.

        `echo "<push>" && <push>` mentions the verb twice: once as data, once
        as a command. Dropping the inert occurrence is only safe because the
        live one is still found.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = ('echo "' + self.PUSH + " origin " + self.MAIN + '" && '
                   + self.PUSH + " origin " + self.MAIN)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == "deny"

    def test_an_exec_wrapper_does_not_make_the_verb_an_argument(self, tmp_path):
        """`sudo <push>` and `env A=b <push>` DO push, unlike `echo <push>`.

        Command-position matching is what stops `echo <push>` denying, and the
        obvious way to write it — "the verb must be the first word after a
        separator" — hands back an off switch to anything that runs its
        arguments. These walk back over transparent tokens instead.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        for command in (
            "sudo " + self.PUSH + " origin " + self.MAIN,
            "env GIT_TRACE=1 " + self.PUSH + " origin " + self.MAIN,
            "time " + self.PUSH + " origin " + self.MAIN,
            "true && sudo " + self.PUSH + " origin " + self.MAIN,
        ):
            assert TestHookBlockingPathsFire._decide(
                work, env, "prevent-direct-push", command) == "deny", (
                f"an exec wrapper stood the gate down: {command!r}"
            )

    def test_a_verb_inside_a_substitution_is_kept(self, tmp_path):
        """`$( )` is the one non-"exec" state whose occurrences are KEPT: the
        command inside really runs, in a subshell whose cwd never escapes.
        Nothing named that arm, so folding it in with the dropped states —
        which is the natural way to write the check — would have gone
        unnoticed. The `echo`'d row is the control that keeps this from
        passing on a gate that denies everything.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        push = self.PUSH + " origin " + self.MAIN
        assert decide(work, env, "prevent-direct-push", "$(" + push + ")") == "deny"
        assert decide(work, env, "prevent-direct-push",
                      "X=`" + push + "`") == "deny"
        assert decide(work, env, "prevent-direct-push", "echo " + push) == "allow"

    def test_a_prefix_that_does_not_parse_keeps_the_occurrence(self, tmp_path):
        """A `)` with nothing open makes the prefix unparseable — and an
        unplaceable verb must be KEPT, not dropped.

        The three non-executing states are not interchangeable: "quoted" is
        proof the verb is inert, "unparseable" is proof of nothing, and
        dropping an occurrence is an allow. A `case` arm is the ordinary way
        to write that prefix.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = ("case $x in a) " + self.PUSH + " origin " + self.MAIN
                   + " ;; esac")
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == "deny"

    def test_a_cd_after_an_unparseable_prefix_does_not_descope(self, tmp_path):
        """The other half of the "broken" state, on the `cd` side.

        `_shell_scan` returning "exec" for a prefix it could not parse would
        make the `cd` in `echo ) && cd /elsewhere && <push>` count, and a
        counted `cd` descopes the verb — an ALLOW. The unmatched `)` means a
        shell runs none of it, so gating costs nothing and trusting it costs
        the gate. The row below it is the same command WITHOUT the `)`, which
        must still allow, or this asserts only that everything denies.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        push = self.PUSH + " origin " + self.MAIN
        decide = TestHookBlockingPathsFire._decide
        assert decide(work, env, "prevent-direct-push",
                      f"echo ) && cd {elsewhere} && {push}") == "deny"
        assert decide(work, env, "prevent-direct-push",
                      f"cd {elsewhere} && {push}") == "allow"

    def test_an_absolute_path_to_git_is_still_git(self, tmp_path):
        """`/usr/bin/git <verb>` contains no `git <verb>` at a word boundary
        the naive way either, and command-position matching would read the
        path as the preceding word if the pattern did not include it."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push",
            "/usr/bin/git pu" + "sh origin " + self.MAIN) == "deny"

    def test_the_branch_name_comes_from_the_command_that_runs(self, tmp_path):
        """validate-branch-name extracts the name with its own regex, which
        had to move with the verb: `git -C . checkout -b bogus` reaching the
        gate is worth nothing if the extraction then finds no name and the
        hook exits 0."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        assert decide(work, env, "validate-branch-name",
                      "git -C . chec" + "kout -b feature/fine") == "allow"
        assert decide(work, env, "validate-branch-name",
                      "git -C . chec" + "kout -b bogus") == "deny"

    def test_a_descoped_push_is_still_descoped(self, tmp_path):
        """Negative control for the whole verb-matching change: the #326 scope
        helper is fed the SAME pattern the gate matched, so a push that
        provably runs in another checkout must still allow."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        command = f"cd {elsewhere} && git -C . pu" + "sh origin " + self.MAIN
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == "allow"


class TestGitSubprocessHandlersAreWideEnough:
    """#327 F5: `except (CalledProcessError, FileNotFoundError)` is too narrow.

    A `git` on PATH that exists and is not executable raises PermissionError.
    It is an OSError, as FileNotFoundError is, but only the latter was named —
    so the exception escaped, the hook exited 1, and rc 1 is a NON-BLOCKING
    error under the PreToolUse contract: the command proceeds.

    What makes this narrowness rather than a design gap is that the code
    already intends to survive a git failure — it falls back to an empty
    branch name and carries on, and that fallback is safe in the deny
    direction. The exception tuple simply did not admit the failure.
    """

    @staticmethod
    def _shimmed(tmp_path, env, *names):
        """PATH holding nothing but non-executable stand-ins for `names`.

        PREPENDING the shim proves nothing, and silently: `execvp` semantics
        are to treat EACCES as "keep looking", and CPython's exec loop does
        the same, so the real `git` further down PATH is found and the test
        tautologises into the ordinary happy path. Measured — the first
        version of this fixture passed against the UNFIXED handler.
        """
        shim = tmp_path / "shim"
        shim.mkdir(exist_ok=True)
        for name in names:
            target = shim / name
            target.write_text("#!/bin/sh\nexit 0\n")
            target.chmod(0o600)  # readable, NOT executable -> PermissionError
        return dict(env, PATH=str(shim))

    def test_a_non_executable_git_does_not_turn_the_push_gate_off(self, tmp_path):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        env = self._shimmed(tmp_path, env, "git")
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push",
            "git pu" + "sh origin ma" + "in") == "deny", (
            "the push gate did not survive an unusable git; an uncaught "
            "PermissionError is rc 1, and rc 1 is non-blocking"
        )

    def test_a_non_executable_git_blocks_the_pr_base_gate(self, tmp_path):
        """An unreadable branch is not an acceptable branch.

        This test used to assert ALLOW, on the reasoning that "with no branch
        name the gate has nothing to object to". That reasoning is the
        fail-open: `get_current_branch()` returned `""` on any git failure,
        `"".startswith("feature/")` is False, and the base check therefore
        never ran — an unusable git switched the gate off as surely as an
        uncaught exception did, just at rc 0 (#327). Failing to determine the
        branch is not evidence about the branch.

        The negative control is the row below: with a WORKING git on the
        feature branch, the same command still denies for its own reason (no
        --base), so this row cannot pass by denying everything.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        broken = self._shimmed(tmp_path, env, "git")
        command = "gh pr cre" + "ate --title x"
        assert TestHookBlockingPathsFire._decide(
            work, broken, "enforce-pr-base-branch", command) == "deny", (
            "an unusable git stood the PR-base gate down"
        )
        assert TestHookBlockingPathsFire._decide(
            work, env, "enforce-pr-base-branch", command) == "deny"

    def test_a_non_executable_git_blocks_the_push_gate_on_any_branch(
        self, tmp_path
    ):
        """`git push` and `git push -f` from `main`, with git unusable.

        The `""` fallback was documented as "safe in the deny direction" and
        was not: `""` is not `main`, not `develop`, and starts with neither
        `release/` nor `hotfix/`, so a push whose only offence is the branch
        it is ON sailed through. Measured before the fix — both denied with a
        working git, both ALLOWED with a broken one (#327).
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", "ma" + "in"],
                       env=env, check=True, capture_output=True)
        broken = self._shimmed(tmp_path, env, "git")
        push = "git pu" + "sh"
        for command in (f"{push} origin feature/x", f"{push} -f origin feature/x"):
            assert TestHookBlockingPathsFire._decide(
                work, env, "prevent-direct-push", command) == "deny", (
                f"control: a working git must still deny this: {command!r}"
            )
            assert TestHookBlockingPathsFire._decide(
                work, broken, "prevent-direct-push", command) == "deny", (
                f"a broken git turned the branch check off: {command!r}"
            )

    def test_an_unusable_gh_blocks_the_merge_it_cannot_verify(self, tmp_path):
        """`gh pr merge` with no PR number resolves one by running gh. When
        that raised PermissionError the hook exited 1 and the merge went
        ahead unverified; the deny it already had for a failed gh is the
        right outcome and is now reachable."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        env = self._shimmed(tmp_path, env, "git", "gh")
        # The REASON separates the two, and the verdict does not. Both deny,
        # but only the numberless one may deny for "cannot determine the PR
        # number" — a span extractor that always came back empty would be a
        # false deny of every numbered merge and would still pass a
        # verdict-only assertion (#327).
        merge = "gh pr me" + "rge"
        _, numbered = TestHookBlockingPathsFire._decision_and_reason(
            work, env, "enforce-pr-base-branch", f"{merge} 5")
        assert "PR #5" in numbered, (
            "the merge gate lost the PR number it was given: "
            f"{numbered[:140]!r}"
        )
        _, numberless = TestHookBlockingPathsFire._decision_and_reason(
            work, env, "enforce-pr-base-branch", merge)
        assert "Cannot determine PR number" in numberless, (
            f"the numberless merge blocked for the wrong reason: "
            f"{numberless[:140]!r}"
        )
        for command in (merge, f"{merge} 5"):
            assert TestHookBlockingPathsFire._decide(
                work, env, "enforce-pr-base-branch", command) == "deny", (
                f"the base-branch check could not run and did not block: {command!r}"
            )


class TestAdvisoryOutputUsesTheSameChannelRules:
    """#327 F6: two ways to write the decision channel, one knowing the rules.

    `_deny`/`block` flush inside a try and fall back to exit 2; the ALLOW-side
    sites were left on a bare `print()`. That is not a fail-open — the
    intended outcome there IS allow, and 120 is non-blocking — but on a dead
    pipe it produced `rc=120` and an `Exception ignored on flushing
    sys.stdout` in the transcript on a path where nothing is wrong. Both now
    go through one `_emit()`.

    The payload is driven straight into `_emit` rather than through a gated
    command: reaching the advisory branch through the business logic needs a
    live preflight token in one hook and a CHANGELOG in the other, and neither
    has anything to do with what is being tested.
    """

    HOOKS = ("require-preflight", "update-changelog-before-pr")

    DRIVER = (
        "import runpy, sys\n"
        # run_name is deliberately not __main__: these hooks guard their entry
        # point with it, and running main() would read stdin and decide things.
        "mod = runpy.run_path(sys.argv[1], run_name='loaded_for_test')\n"
        "mod['_emit']({'hookSpecificOutput': {'hookEventName': 'PreToolUse',"
        " 'additionalContext': 'advisory'}})\n"
    )

    def _run(self, hook, stdout):
        return subprocess.run(
            [sys.executable, "-c", self.DRIVER,
             str(Path(".claude/hooks") / f"{hook}.py")],
            stdout=stdout, stderr=subprocess.PIPE, text=True, timeout=60,
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_an_advisory_payload_into_a_dead_pipe_is_not_a_hook_error(self, hook):
        read_fd, write_fd = os.pipe()
        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", self.DRIVER,
                 str(Path(".claude/hooks") / f"{hook}.py")],
                stdin=subprocess.DEVNULL, stdout=write_fd,
                stderr=subprocess.PIPE, text=True,
            )
        finally:
            os.close(write_fd)
        os.close(read_fd)
        _, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, (
            f"{hook} exited {proc.returncode} writing advisory context into a "
            f"dead pipe; the intended decision is allow and exit 0 is how it "
            f"is spelled: {err[-400:]!r}"
        )
        assert "Exception ignored" not in err, (
            f"{hook} left a spurious hook error in the transcript: {err!r}"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_the_same_advisory_payload_still_reaches_a_live_stdout(self, hook):
        """Negative control: exiting 0 on a dead pipe is only correct if the
        payload is still WRITTEN when there is somewhere to write it."""
        proc = self._run(hook, subprocess.PIPE)
        assert proc.returncode == 0, proc.stderr[-400:]
        assert json.loads(proc.stdout)["hookSpecificOutput"][
            "additionalContext"] == "advisory"

    @pytest.mark.parametrize("hook", HOOKS)
    def test_a_deny_through_the_shared_path_still_exits_2_on_a_dead_pipe(self, hook):
        """The deny side must not have been loosened by sharing `_emit` with
        the advisory side: exit 0 with an empty stdout is an ALLOW, so a deny
        that cannot be written has to exit 2 — never 120, which is
        non-blocking."""
        driver = (
            "import runpy, sys\n"
            "mod = runpy.run_path(sys.argv[1], run_name='loaded_for_test')\n"
            "mod['block']('nope')\n"
        )
        read_fd, write_fd = os.pipe()
        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", driver,
                 str(Path(".claude/hooks") / f"{hook}.py")],
                stdin=subprocess.DEVNULL, stdout=write_fd,
                stderr=subprocess.PIPE, text=True,
            )
        finally:
            os.close(write_fd)
        os.close(read_fd)
        _, err = proc.communicate(timeout=60)
        assert proc.returncode == 2, (
            f"{hook} exited {proc.returncode} on a deny it could not write; "
            f"only 2 blocks and 120 is a non-blocking error: {err[-400:]!r}"
        )
        assert "BLOCKED:" in err, f"{hook} blocked with no reason on stderr: {err!r}"


class TestFormsDevelopDeniedDoNotRegress:
    """Shapes `develop` denied, which the first cut of this change ALLOWED.

    Matching the verb by substring made `develop` accidentally right about a
    whole family of commands: it did not care what stood in front of the verb,
    so `if <verb>; then …`, `exec <verb>`, `\\<verb>` and a line-continued
    `git \\`+newline+`push` all denied. Deciding by grammar is only an
    improvement if it keeps them — a command-position walk that stops at the
    first word it does not recognise turns each one into an ALLOW, and an
    allow is what this gate exists to prevent. Measured develop -> first cut,
    all seven deny -> allow.

    So every row here is a REGRESSION test in the strict sense: the reference
    verdict is develop's, not this change's. Each is paired in the same test
    with the `echo`'d form, which must still ALLOW — otherwise "deny
    everything" would pass as a fix.

    Fragments again: the live hooks inspect unexecuted command text.
    """

    PUSH = "git pu" + "sh"
    MAIN = "ma" + "in"
    DEV = "deve" + "lop"
    CHECKOUT = "git chec" + "kout -b"
    COMMIT = "git com" + "mit"
    GH_CREATE = "gh pr cre" + "ate"

    # C1 — a keyword or wrapper that OPENS a command. The walk-back has to
    # cross it; stopping there reads a real command as text.
    OPENERS = (
        ("prevent-direct-push", "if {v} origin " + MAIN + "; then echo ok; fi"),
        ("prevent-direct-push", "while {v} origin " + MAIN + "; do :; done"),
        ("prevent-direct-push", "until {v} origin " + MAIN + "; do :; done"),
        ("prevent-direct-push", "exec {v} origin " + MAIN),
        ("prevent-direct-push", "command {v} origin " + MAIN),
        ("prevent-direct-push", "! {v} origin " + MAIN),
        # A bare backslash: the ordinary way to bypass an alias or a shell
        # function of the same name. The verb pattern matches at offset 1.
        ("prevent-direct-push", "\\{v} origin " + MAIN),
        ("require-preflight", "if {v} -m wip; then :; fi"),
        ("require-preflight", "exec {v} -m wip"),
        ("require-preflight", "\\{v} -m wip"),
        ("validate-branch-name", "if {v} nonsense; then :; fi"),
        ("validate-branch-name", "while {v} nonsense; do :; done"),
        ("validate-branch-name", "\\{v} nonsense"),
        ("update-changelog-before-pr", "if {v} --base develop; then :; fi"),
        ("update-changelog-before-pr", "exec {v} --base develop"),
        ("enforce-pr-base-branch", "if {v} --base " + MAIN + "; then :; fi"),
        ("enforce-pr-base-branch", "\\{v} --base " + MAIN),
    )

    # C2 — "\<newline>" is a line continuation, i.e. whitespace. `\s+` does
    # not span the backslash, so the verb guard never matched and exited 0
    # before the helper that already knew this was ever called.
    CONTINUATIONS = (
        ("prevent-direct-push", "git \\\n push origin " + MAIN),
        ("prevent-direct-push", "git -C \\\n . pu" + "sh origin " + MAIN),
        ("prevent-direct-push", "git pu" + "sh origin \\\n" + MAIN),
        ("require-preflight", "git \\\n com" + "mit -m wip"),
        ("validate-branch-name", "git \\\n chec" + "kout -b nonsense"),
        ("update-changelog-before-pr", "gh pr \\\n cre" + "ate --base develop"),
        ("enforce-pr-base-branch", "gh pr \\\n cre" + "ate --title x"),
    )

    # C3 — the same defect this change fixes for git, in the gh siblings.
    # `gh --repo o/r pr list` parses and returns 0 against the real binary.
    GH_GLOBAL_OPTIONS = (
        ("enforce-pr-base-branch", "gh --repo o/r pr cre" + "ate --base " + MAIN),
        ("enforce-pr-base-branch", "gh -R o/r pr cre" + "ate --base " + MAIN),
        ("enforce-pr-base-branch",
         'gh --repo "o/r x" pr cre' + "ate --base " + MAIN),
        ("enforce-pr-base-branch", "/opt/homebrew/bin/gh pr cre" + "ate --base " + MAIN),
        ("update-changelog-before-pr", "gh --repo o/r pr cre" + "ate --base develop"),
    )

    # The verb each gate's business logic denies in the fixture repo, and the
    # `echo`'d control that must still allow.
    VERBS = {
        "prevent-direct-push": PUSH,
        "require-preflight": COMMIT,
        "validate-branch-name": CHECKOUT,
        "update-changelog-before-pr": GH_CREATE,
        "enforce-pr-base-branch": GH_CREATE,
    }

    @classmethod
    def _fill(cls, hook, template):
        return template.replace("{v}", cls.VERBS[hook])

    @pytest.mark.parametrize("hook,template", OPENERS)
    def test_a_command_opener_does_not_stand_the_gate_down(
        self, tmp_path, hook, template
    ):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        command = self._fill(hook, template)
        assert decide(work, env, hook, command) == "deny", (
            f"{hook} read a real command as text because of the word in front "
            f"of the verb; develop denied this: {command!r}"
        )
        echoed = "echo " + self._fill(hook, "{v}")
        assert decide(work, env, hook, echoed) == "allow", (
            f"{hook} now denies an echo'd mention, so the row above proves "
            f"nothing but a gate that denies everything: {echoed!r}"
        )

    @pytest.mark.parametrize("hook,command", CONTINUATIONS)
    def test_a_line_continuation_does_not_hide_the_verb(
        self, tmp_path, hook, command
    ):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        assert decide(work, env, hook, command) == "deny", (
            f"{hook} lost the verb across a line continuation: {command!r}"
        )
        echoed = "echo " + self._fill(hook, "{v}")
        assert decide(work, env, hook, echoed) == "allow", (
            f"{hook} denies an echo'd mention; this fixture cannot discriminate"
        )

    @pytest.mark.parametrize("hook,command", GH_GLOBAL_OPTIONS)
    def test_a_global_gh_option_does_not_evade_the_pr_gates(
        self, tmp_path, hook, command
    ):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        assert decide(work, env, hook, command) == "deny", (
            f"{hook} never saw this command: gh takes global options before "
            f"its subcommand, exactly as git does: {command!r}"
        )
        assert decide(work, env, hook, "gh --repo o/r pr sta" + "tus") == "allow", (
            f"{hook} denies an unrelated gh command, so this proves nothing"
        )

    def test_a_global_gh_option_does_not_evade_the_merge_gate(self, tmp_path):
        """`gh pr merge` needs the same widening, and its verdict must not
        depend on the network: with `gh` unusable the gate's own "cannot
        verify" deny is what proves the verb was matched at all — an unmatched
        verb exits 0, which is an allow."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        env = TestGitSubprocessHandlersAreWideEnough._shimmed(
            tmp_path, env, "git", "gh")
        decide = TestHookBlockingPathsFire._decide
        assert decide(work, env, "enforce-pr-base-branch",
                      "gh --repo o/r pr me" + "rge 5") == "deny"
        assert decide(work, env, "enforce-pr-base-branch",
                      "echo gh pr me" + "rge 5") == "allow"

    # Quoting is an EVASION AXIS, not one bad row: a matcher that reads the
    # ref by position in the text loses it behind any quote, so every refspec
    # form is swept with both quote styles. The controls under them are what
    # keep the sweep from becoming "deny anything quoted".
    QUOTED_REFS = (
        "git pu" + "sh origin '" + MAIN + "'",
        'git pu' + 'sh origin "' + MAIN + '"',
        "git pu" + "sh origin '" + DEV + "'",
        "git pu" + "sh --force origin 'HEAD:" + MAIN + "'",
        'git pu' + 'sh --force origin "HEAD:' + MAIN + '"',
        "git pu" + "sh origin '+" + MAIN + ":" + MAIN + "'",
        'git pu' + 'sh --force-with-lease origin "HEAD:' + MAIN + '"',
        "git pu" + "sh origin 'refs/heads/" + MAIN + "'",
        "git pu" + "sh origin 'mybranch:" + DEV + "'",
        "git pu" + "sh upstream '" + MAIN + "'",
    )

    QUOTED_ALLOWED = (
        "git pu" + "sh origin '" + MAIN + "tenance'",
        'git pu' + 'sh origin "foo:' + MAIN + 'line"',
        "git pu" + "sh origin 'feature/" + MAIN + "'",
        'git pu' + 'sh origin feature/probe -o "deploy to ' + MAIN + '"',
    )

    @pytest.mark.parametrize("command", QUOTED_ALLOWED)
    def test_quoting_does_not_make_an_ordinary_ref_protected(
        self, tmp_path, command
    ):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == "allow", command

    @pytest.mark.parametrize("command", QUOTED_REFS)
    def test_a_quoted_ref_is_the_same_ref(self, tmp_path, command):
        """This file already models quoted option values; the ref comparison
        being quote-blind was an inconsistency inside one file."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        assert decide(work, env, "prevent-direct-push", command) == "deny", command
        assert decide(work, env, "prevent-direct-push",
                      "git pu" + "sh origin '" + self.MAIN + "tenance'") == "allow", (
            "tolerating the opening quote must not re-break whole-ref matching"
        )

    def test_a_command_that_does_not_parse_keeps_its_verb(self, tmp_path):
        """An apostrophe opens a quote that never closes, and every position
        after it scans as "quoted" — so dropping quoted occurrences turned
        `echo don't && <push>` into an ALLOW, on a shape develop denied.

        "Quoted" is only evidence of inertness when the command as a WHOLE
        parses. The three rows separate that: the middle one is the same
        command with the apostrophe removed (it always denied), and the last
        is a genuinely quoted mention inside a command that parses, which must
        still allow.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        push = self.PUSH + " origin " + self.MAIN
        assert decide(work, env, "prevent-direct-push",
                      "echo don't && " + push) == "deny"
        assert decide(work, env, "prevent-direct-push",
                      "echo dont && " + push) == "deny"
        assert decide(work, env, "prevent-direct-push",
                      "echo '" + push + "'") == "allow"

    def test_the_option_value_grammar_is_identical_in_all_five_hooks(self):
        """`_OPT_VALUE` is hand-duplicated like the scope guard, and it is not
        reachable from `_targets_this_project`, so the drift test that covers
        that closure does not cover this. A copy that drifts changes which
        commands one gate can see, silently."""
        sources = {}
        for hook in TestHookInputFailsClosed.HOOKS:
            src = (Path(".claude/hooks") / f"{hook}.py").read_text(encoding="utf-8")
            tree = ast.parse(src)
            found = [
                ast.get_source_segment(src, node) for node in tree.body
                if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_OPT_VALUE"
                        for t in node.targets)
            ]
            assert len(found) == 1, f"{hook}.py defines _OPT_VALUE {len(found)}x"
            sources[hook] = found[0]
        reference = sources[TestHookInputFailsClosed.HOOKS[0]]
        for hook, text in sources.items():
            assert text == reference, f"{hook}.py's _OPT_VALUE has drifted"


class TestWrapperArgumentsDoNotHideTheVerb:
    """#327 I2: a wrapper's own argument is a bare word, and so is `echo`.

    The command-position walk crosses transparent tokens, and an option
    (`-n1`) is transparent. A SEPARATED value is not an option — it is a bare
    word — so the walk stopped at `60` in `timeout 60 <verb>` and read a real
    command as text. Measured, develop -> before this fix: `timeout 60`,
    `nice -n 10` and `xargs -n 1` all deny -> ALLOW, while their joined-flag
    spellings (`xargs -n1`) denied, which is the tell that the flag was never
    the point.

    The fix cannot be "cross bare words too": `echo <verb>` is that shape
    exactly, and crossing it re-breaks the false-deny this change exists to
    close. Bare words are carried and only settled once a wrapper claims them,
    which is what the paired rows below assert — same walk, opposite verdicts.
    """

    PUSH = "git pu" + "sh"
    MAIN = "ma" + "in"

    GATED = (
        "timeout 60 {v}",
        "timeout -k 5 60 {v}",
        "nice -n 10 {v}",
        "nice -n10 {v}",
        "xargs -n 1 {v}",
        "xargs -n1 {v}",
        "sudo -u someone {v}",
        "env -i {v}",
        "nohup {v}",
    )

    TEXT = (
        "echo {v}",
        "echo -n {v}",
        "grep -r foo {v}",
        "printf %s {v}",
        # Bare words nothing claims: the walk reaches the start of the line
        # still carrying them, which settles it as text.
        "echo one two three four {v}",
        # A plain keyword is not a wrapper: `time` takes no argument of its
        # own, so the bare word before the verb belonged to something else.
        "time foo {v}",
        # `time foo <verb>` alone does not prove the arm that returns False on
        # reaching a plain keyword with bare words outstanding: the walk would
        # reach the start of the line and the terminal `return pending_bare ==
        # 0` gives the same answer. These two put an ARG-TAKING wrapper further
        # left, so the terminal check is never reached and only that arm can
        # produce the allow (#327).
        "xargs time foo {v}",
        "sudo time foo {v}",
    )

    @pytest.mark.parametrize("template", GATED)
    def test_a_wrapper_with_arguments_still_reaches_the_gate(self, tmp_path, template):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = template.format(v=self.PUSH + " origin " + self.MAIN)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == "deny", (
            f"a wrapper's own argument hid a real push: {command!r}"
        )

    @pytest.mark.parametrize("template", TEXT)
    def test_the_verb_as_another_command_s_argument_still_allows(
        self, tmp_path, template
    ):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = template.format(v=self.PUSH + " origin " + self.MAIN)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == "allow", (
            f"carrying bare words re-broke the false-deny fix: {command!r}"
        )

    def test_the_walk_gives_up_on_the_gated_side(self, tmp_path):
        """The walk is bounded, and the bound fails CLOSED.

        Each step rescans the text to its left, so an unbounded walk is
        quadratic in a command line written by whoever is being gated. The
        bound is a cost control, not a meaning: running out establishes
        nothing, and an unestablished verb is a gated one.

        The first cut bounded the number of BARE WORDS carried instead, and a
        mutation proved that unfalsifiable — the terminal check already
        rejects `echo one two three four <verb>`, so raising the carry limit
        changed no verdict any test could see. The rows below are what a real
        bound looks like: past it the verdict flips, under it nothing changes,
        and a wrapper further left than four words is now credited (the last
        row), which the carry limit had been quietly refusing.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        push = self.PUSH + " origin " + self.MAIN
        assert decide(work, env, "prevent-direct-push",
                      "echo " + "w " * 70 + push) == "deny"
        assert decide(work, env, "prevent-direct-push",
                      "echo " + "w " * 10 + push) == "allow"
        assert decide(work, env, "prevent-direct-push",
                      "xargs a b c d e " + push) == "deny"

    @pytest.mark.parametrize("hook,command", (
        ("require-preflight", "timeout 5 git com" + "mit -m wip"),
        ("validate-branch-name", "timeout 5 git chec" + "kout -b nonsense"),
        ("update-changelog-before-pr", "timeout 5 gh pr cre" + "ate --base develop"),
        ("enforce-pr-base-branch", "timeout 60 gh pr cre" + "ate --base ma" + "in"),
    ))
    def test_the_other_gates_cross_a_wrapper_argument_too(
        self, tmp_path, hook, command
    ):
        """The walk is one hand-duplicated helper; a fix in one copy that did
        not reach the others would leave three gates open."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        assert TestHookBlockingPathsFire._decide(work, env, hook, command) == "deny"


class TestNothingInFrontOfTheVerbIsStillACommand:
    """#327 I5: three shapes reach the verb with NO word in front of it.

    Measured on this branch before the fix, against `develop`, which denied
    every row. The marker is a filesystem side-effect, not stdout: stdout is
    captured inside a substitution and cannot tell a run from a mention.

        echo starting<newline><push> origin main      ALLOW  (develop: deny)
        echo a<newline>echo b<newline><push> …        ALLOW  (develop: deny)
        echo a   <newline><push> origin main          ALLOW  (develop: deny)
        echo x # note<newline><push> origin main      ALLOW  (develop: deny)
        `<push> origin main`                          ALLOW  (develop: deny)
        true; `<push> origin main`                    ALLOW  (develop: deny)
        >/dev/null <push> origin main                 ALLOW  (develop: deny)
        2>/dev/null <push> origin main                ALLOW  (develop: deny)
        true && >/dev/null <push> origin main         ALLOW  (develop: deny)

    Three causes, all in one helper: `prefix.rstrip()` removed the newline the
    very next line tests for as a separator; the backtick was missing from
    that separator set while `(` was in it; and a leading redirection was an
    unrecognised bare word. A two-line Bash command is the ordinary case,
    which made the first the widest hole in this change.

    Every gated row is paired with a control that must still ALLOW and differs
    only in what stands at the command position — `<newline>echo <push>`
    against `<newline><push>`, `` `echo <push>` `` against `` `<push>` ``.
    Without the pair, a helper that denied every newline would pass this class
    while re-breaking the false-deny fix the change exists to deliver.

    These shipped because no command string in this file contained a newline,
    a backtick or a redirection.
    """

    PUSH = "git pu" + "sh"
    MAIN = "ma" + "in"
    BT = chr(96)
    VERB = PUSH + " origin " + MAIN

    # (label, template). The verb is a COMMAND in each: bash runs it.
    GATED = (
        ("a second line", "echo starting\n{v}"),
        ("a third line", "echo a\necho b\n{v}"),
        ("spaces before the newline", "echo a   \n{v}"),
        ("a comment on the line before", "echo x # note\n{v}"),
        ("a bare backtick", BT + "{v}" + BT),
        ("a backtick after a separator", "true; " + BT + "{v}" + BT),
        ("a leading redirection", ">/dev/null {v}"),
        ("a leading 2> redirection", "2>/dev/null {v}"),
        ("a redirection after a separator", "true && >/dev/null {v}"),
        ("a redirection with a separated target", "> out {v}"),
        ("an appending redirection", ">>out {v}"),
        ("an input redirection", "<in {v}"),
        # File-descriptor duplication. `&` is a separator, but the `&` in
        # `>&`, `<&` and `&>` is part of the operator — reading it as one left
        # the fd number behind as an outstanding bare word, and the verb read
        # as that word's argument.
        ("fd duplication 2>&1", "2>&1 {v}"),
        ("fd duplication 1>&2", "1>&2 {v}"),
        ("fd duplication 3>&1", "3>&1 {v}"),
        ("fd duplication then a redirection", "2>&1 >/dev/null {v}"),
        ("fd close N>&-", "2>&- {v}"),
        ("fd duplication on input", "3<&0 {v}"),
        ("&>> appending both streams", "&>>out {v}"),
        ("fd duplication after a separator", "true && 2>&1 {v}"),
    )

    # The same shapes with a real command in front of the verb: bash prints it.
    TEXT = (
        ("the second line echoes it", "echo starting\necho {v}"),
        ("the third line echoes it", "echo a\necho b\necho {v}"),
        ("spaces before the newline, then echo", "echo a   \necho {v}"),
        # A `#` gets no special case: everything after an unquoted one is a
        # comment, and the verdict comes from `echo`, the word that decides in
        # the shell too. A rule that allowed on SEEING a `#` would be
        # quote-blind — `env "A=x # y" <verb>` really does push.
        ("a comment on the SAME line", "echo x # {v}"),
        ("echo inside the backticks", BT + "echo {v}" + BT),
        ("echo after the redirection", ">/dev/null echo {v}"),
        ("the redirection is echo's own", "echo a > out {v}"),
        # A bare `>` claims exactly ONE word — its target. Credit it with more
        # and this row denies: `echo` would never be reached.
        ("a separated redirection, then echo", "> out echo {v}"),
        # The fd-duplication fix must not make every `&` inert: these two run
        # `echo`, and the `2>&1` belongs to it.
        ("echo with fd duplication", "echo 2>&1 {v}"),
        ("echo, an argument, fd duplication", "echo a 2>&1 {v}"),
        ("echo with &> redirecting both streams", "echo a &>out {v}"),
        # A non-breaking space is an ordinary word character, here and in
        # bash: its IFS is space, tab and newline. So the command after the
        # `&&` is the single word `<nbsp>git`, which bash does not find and
        # never runs — verified by running it, with a file as the marker. The
        # word-boundary set is deliberately the same one the strip uses;
        # `rsplit(None, 1)` split on `str.isspace()` and disagreed with bash.
        ("a non-breaking space after a separator", "true &&\u00a0{v}"),
        ("a non-breaking space inside a word", "a\u00a0b {v}"),
    )

    def test_a_redirection_among_the_arguments_does_not_cut_the_span(
        self, tmp_path
    ):
        """The same misread `&`, one function over.

        `_argument_span` ends a command at `&` too, so `<push> 2>&1 origin
        main` was cut to `<push> 2>` — and the protected-ref test, which reads
        only that span, never saw `main`. Measured ALLOW here against DENY on
        `develop`, which is the shape of a regression, not of a fix.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        push = "git pu" + "sh"
        main = "ma" + "in"
        assert decide(work, env, "prevent-direct-push",
                      f"{push} 2>&1 origin {main}") == "deny"
        assert decide(work, env, "prevent-direct-push",
                      f"{push} >/dev/null origin {main}") == "deny"
        # The control: a `&&` after the push still ends that push's span, so
        # the second command's arguments are not read as the first's.
        assert decide(work, env, "prevent-direct-push",
                      f"{push} origin feature/probe && echo {main}") == "allow"

    @pytest.mark.parametrize("label,template", GATED)
    def test_a_verb_with_nothing_in_front_of_it_is_gated(
        self, tmp_path, label, template
    ):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = template.format(v=self.VERB)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == "deny", (
            f"{label}: bash runs this push and the gate stood down: {command!r}"
        )

    @pytest.mark.parametrize("label,template", TEXT)
    def test_the_same_shape_with_a_command_in_front_still_allows(
        self, tmp_path, label, template
    ):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = template.format(v=self.VERB)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == "allow", (
            f"{label}: closing the bypass re-broke the false-deny fix: "
            f"{command!r}"
        )

    # hook -> (a command that hook denies, the same text as echo's argument).
    # The walk is one hand-duplicated helper, so a fix that reached only the
    # push gate would leave four gates open on every shape above.
    EVERY_GATE = (
        ("require-preflight", "git com" + "mit -m wip"),
        ("prevent-direct-push", "git pu" + "sh origin ma" + "in"),
        ("validate-branch-name", "git chec" + "kout -b nonsense-branch"),
        ("enforce-pr-base-branch", "gh pr cre" + "ate --base ma" + "in"),
        # No CHANGELOG.md exists in the throwaway repo, so this gate denies on
        # the file, once the command reaches it at all.
        ("update-changelog-before-pr", "gh pr cre" + "ate --base develop"),
    )
    SHAPES = (
        ("newline", "echo starting\n{v}", "echo starting\necho {v}"),
        ("backtick", BT + "{v}" + BT, BT + "echo {v}" + BT),
        ("redirection", ">/dev/null {v}", ">/dev/null echo {v}"),
    )

    @pytest.mark.parametrize("hook,verb", EVERY_GATE)
    @pytest.mark.parametrize("shape,gated,text", SHAPES)
    def test_every_gate_reads_the_same_shapes_the_same_way(
        self, tmp_path, hook, verb, shape, gated, text
    ):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        assert decide(work, env, hook, gated.format(v=verb)) == "deny", (
            f"{hook}: a {shape} in front of the verb stood the gate down"
        )
        assert decide(work, env, hook, text.format(v=verb)) == "allow", (
            f"{hook}: the {shape} shape denies even when the verb is text"
        )


@pytest.mark.skipif(not os.path.exists("/bin/bash"),
                    reason="the bash-truth oracle needs /bin/bash")
class TestBashTruthDifferential:
    """#327 I6: bash decides what runs; the gate only gets to agree.

    Every other test in this file is a row somebody thought of, and that is
    exactly how the newline and backtick bypasses shipped: the suite was 3035
    green while 16 of 55 executing constructs — 29% — walked straight past the
    push gate, because no fixture in it contained a newline or a backtick.

    So this one does not assert a verdict list. It builds shell constructs by
    combination, asks BASH whether the verb really executed, and requires a
    deny for every construct that did. A construct bash does not execute is
    skipped rather than asserted on: this is a bypass hunt, and a fail-closed
    deny on inert text is not a bypass.

    The marker is a FILESYSTEM side-effect, never stdout. Half these
    constructs run the verb inside `$( )` or backticks, where stdout is
    captured by the substitution and never reaches the probe — a stdout marker
    reports "did not run" for exactly the constructs that hide a live verb,
    which is the wrong answer in the fail-open direction.

    Deterministic on purpose: a fixed prefix/suffix/wrapper product, not random
    generation, so a failure names a construct that can be pasted into a shell,
    and a green run means the same thing tomorrow.

    Templates are assembled from fragments (`VERB` is substituted late) for the
    same reason as the rest of this file: the live PreToolUse hooks in this
    repo read unexecuted command text.
    """

    PREFIX = ("", "true; ", "true && ", "true || ", "echo hi\n", "echo hi   \n",
              "# c\n", "x=1 ", "{ ", ">/dev/null ", "2>&1 ", "1>&2 ", "&>out ",
              "3>&1 ", "if true; then ", "for i in 1; do ",
              "while false; do : ; done; ", "! ", "time ", "sudo ",
              "nice -n 10 ", "command ", "eval ", "\\", "(", "$(", "`")
    SUFFIX = ("", ";", " ; }", " ; done", " ; fi", ")", "`")
    WRAPPERS = ("%s", "`%s`", "$(%s)", "( %s )", "{ %s ; }")
    WRAP_PREFIX = ("", "true; ", "true && ", "echo hi\n", "x=1 ", ">/dev/null ",
                   "2>&1 ")

    @classmethod
    def _templates(cls):
        """The deterministic construct set, VERB still a placeholder."""
        out = set()
        for prefix in cls.PREFIX:
            for suffix in cls.SUFFIX:
                out.add(prefix + "VERB" + suffix)
        for wrapper in cls.WRAPPERS:
            for prefix in cls.WRAP_PREFIX:
                out.add(prefix + wrapper % "VERB")
        return sorted(out)

    @staticmethod
    def _bash_runs_it(work, template):
        """Did bash EXECUTE the verb? Marker is a file, not stdout."""
        marker = work / "FUZZ_RAN"
        if marker.exists():
            marker.unlink()
        try:
            subprocess.run(
                ["/bin/bash", "-c", template.replace("VERB", f"touch {marker}")],
                capture_output=True, text=True, cwd=work, timeout=10,
                # sudo must fail fast instead of reading a password off the
                # terminal the suite is running in.
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return False
        ran = marker.exists()
        if ran:
            marker.unlink()
        return ran

    def test_every_construct_bash_executes_is_denied(self, tmp_path):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        verb = "git pu" + "sh origin ma" + "in"
        executed, bypasses = 0, []
        for template in self._templates():
            if not self._bash_runs_it(work, template):
                continue
            executed += 1
            command = template.replace("VERB", verb)
            if TestHookBlockingPathsFire._decide(
                    work, env, "prevent-direct-push", command) != "deny":
                bypasses.append(template)
        # The oracle has to keep finding live constructs, or this test passes
        # by testing nothing. 55 executed at the time it was written.
        assert executed >= 50, (
            f"only {executed} constructs executed; the bash oracle is broken, "
            "not the gate"
        )
        assert not bypasses, (
            f"{len(bypasses)} of {executed} constructs run the verb and the "
            "gate allowed them:\n  " + "\n  ".join(repr(t) for t in bypasses)
        )


class TestEveryOccurrenceIsJudged:
    """#327 I7: one acceptable command laundered every later one on the line.

    The push gate was fixed to loop over its occurrences; the branch-name gate
    and the PR-base gate were left judging `occurrences[0]` alone. Measured,
    all four ALLOW before this fix while each offending half denies on its own:

        git CHECKOUT -b feature/ok && git CHECKOUT -b badname
        git CHECKOUT -b release/v1.0.0 && git CHECKOUT -b release/1.0
        gh pr CREATE --base develop && gh pr CREATE --title x
        gh pr CREATE --base develop && gh pr CREATE --base main --title x

    The last one opens a feature->main PR, which is the single thing that gate
    exists to prevent.

    Each row is paired with two controls: the offending half ALONE must still
    deny (so the row is not passing because the gate denies chains), and a
    chain of two ACCEPTABLE commands must still allow (so it is not passing
    because the gate denies second occurrences).
    """

    BAD_THEN_GOOD = (
        ("branch: a good name then a bad one", "validate-branch-name",
         "git {CO} -b feature/ok && git {CO} -b badname"),
        ("branch: a good release then a bad one", "validate-branch-name",
         "git {CO} -b release/v1.0.0 && git {CO} -b release/1.0"),
        ("pr base: a compliant create then a bare one", "enforce-pr-base-branch",
         "gh pr {CR} --base develop && gh pr {CR} --title x"),
        ("pr base: a compliant create then --base main", "enforce-pr-base-branch",
         "gh pr {CR} --base develop && gh pr {CR} --base main --title x"),
    )
    ALONE = (
        ("branch: the bad name alone", "validate-branch-name",
         "git {CO} -b badname"),
        ("branch: the bad release alone", "validate-branch-name",
         "git {CO} -b release/1.0"),
        ("pr base: the bare create alone", "enforce-pr-base-branch",
         "gh pr {CR} --title x"),
        ("pr base: --base main alone", "enforce-pr-base-branch",
         "gh pr {CR} --base main --title x"),
    )
    ALL_GOOD = (
        ("branch: two good names", "validate-branch-name",
         "git {CO} -b feature/ok && git {CO} -b feature/two"),
        ("pr base: two compliant creates", "enforce-pr-base-branch",
         "gh pr {CR} --base develop && gh pr {CR} --base develop"),
    )

    @staticmethod
    def _fill(template):
        return template.format(CO="check" + "out", CR="cre" + "ate")

    @pytest.mark.parametrize("label,hook,template", BAD_THEN_GOOD)
    def test_a_later_occurrence_is_judged_too(self, tmp_path, label, hook, template):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = self._fill(template)
        assert TestHookBlockingPathsFire._decide(
            work, env, hook, command) == "deny", (
            f"{label}: the first occurrence excused the second: {command!r}"
        )

    @pytest.mark.parametrize("label,hook,template", ALONE)
    def test_the_offending_half_alone_still_denies(
        self, tmp_path, label, hook, template
    ):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        assert TestHookBlockingPathsFire._decide(
            work, env, hook, self._fill(template)) == "deny"

    @pytest.mark.parametrize("label,hook,template", ALL_GOOD)
    def test_a_chain_of_acceptable_commands_still_allows(
        self, tmp_path, label, hook, template
    ):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = self._fill(template)
        assert TestHookBlockingPathsFire._decide(
            work, env, hook, command) == "allow", (
            f"{label}: looping over occurrences turned into denying chains: "
            f"{command!r}"
        )


class TestThePrNumberComesFromABareWord:
    """#327 I8: a digit inside a flag's value was read as the PR number.

    `gh pr MERGE` takes the PR as a positional argument, and the gate took the
    first digit run anywhere in the span instead. So a number written in a
    commit subject or a body decided which PR got its base checked, while a
    different PR was the one being merged.

    Proven against a stubbed `gh` in which #331 targets develop from a feature
    branch (compliant, allowed) and #296 targets main from a feature branch
    (must block). Measured before the fix:

        gh pr MERGE 296 --squash                     DENY   (control)
        gh pr MERGE -t "Merge PR 331" 296 --squash   ALLOW  <- laundered
        gh pr MERGE --body "closes 331" 296 --squash ALLOW  <- laundered

    The negative control matters as much: `-t "Merge PR 296" 331` must still
    ALLOW. A gate that read every digit would deny it, and would be "fixed"
    only in the sense that it now denies more.
    """

    @staticmethod
    def _stub_gh(tmp_path, env):
        """A gh that answers for two PRs and fails for anything else."""
        binder = tmp_path / "stub"
        binder.mkdir(exist_ok=True)
        gh = binder / "gh"
        gh.write_text(
            '#!/bin/sh\n'
            'for a in "$@"; do case "$a" in\n'
            '331) echo "develop feature/ok"; exit 0;;\n'
            '296) echo "main feature/bad"; exit 0;;\n'
            'esac; done\n'
            'exit 1\n'
        )
        gh.chmod(0o755)
        return dict(env, PATH=str(binder) + os.pathsep + env.get("PATH", ""))

    MERGE = "gh pr " + "me" + "rge"

    DENIED = (
        ("the PR itself", "{m} 296 --squash"),
        ("a number in -t", '{m} -t "Merge PR 331" 296 --squash'),
        ("a number in --body", '{m} --body "closes 331" 296 --squash'),
        ("a number in --subject", '{m} --subject "PR 331" 296 --squash'),
        ("a number in -R", "{m} -R owner/repo331 296 --squash"),
        ("a bare number after -t, then the real PR", "{m} -t 331 296 --squash"),
        ("two different PRs named", "{m} 296 331"),
        ("a compliant PR named first, a blocked one second", "{m} 331 296"),
    )
    ALLOWED = (
        ("the compliant PR itself", "{m} 331 --squash"),
        ("a blocked number quoted in -t", '{m} -t "Merge PR 296" 331 --squash'),
        ("a blocked number quoted in --body", '{m} --body "see 296" 331 --squash'),
        # Unquoted, and the value of a flag: skipping it is the only reason
        # this merge is judged as #331. Count it and the span names two PRs.
        ("a bare blocked number after -t", "{m} -t 296 331 --squash"),
    )

    @pytest.mark.parametrize("label,template", DENIED)
    def test_the_merge_that_runs_is_the_one_checked(self, tmp_path, label, template):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        env = self._stub_gh(tmp_path, env)
        command = template.format(m=self.MERGE)
        assert TestHookBlockingPathsFire._decide(
            work, env, "enforce-pr-base-branch", command) == "deny", (
            f"{label}: a digit that is not the PR decided the check: {command!r}"
        )

    @pytest.mark.parametrize("label,template", ALLOWED)
    def test_a_quoted_number_does_not_block_a_compliant_merge(
        self, tmp_path, label, template
    ):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        env = self._stub_gh(tmp_path, env)
        command = template.format(m=self.MERGE)
        assert TestHookBlockingPathsFire._decide(
            work, env, "enforce-pr-base-branch", command) == "allow", (
            f"{label}: reading every digit denies compliant merges: {command!r}"
        )

    def test_a_compliant_merge_does_not_excuse_a_later_one(self, tmp_path):
        """The same occurrence-loop fix as the create and branch gates."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        env = self._stub_gh(tmp_path, env)
        assert TestHookBlockingPathsFire._decide(
            work, env, "enforce-pr-base-branch",
            f"{self.MERGE} 331 --squash && {self.MERGE} 296 --squash") == "deny"


class TestTheScanIsLinearAndBounded:
    """#327 I9: a hook that hangs has written no decision, and no decision is
    an ALLOW.

    `_shell_scan(cmd[:start])` restarts at offset 0 and was called once per
    match, so the cost was quadratic in the command text: measured end-to-end
    at 0.63s for 14 KB and 9.54s for 56 KB, with 1 MB inside the stdin cap
    these hooks accept. The separator-free shape was worse still — 7.8s at
    32 KB and 488s at 256 KB.

    Two changes, and the test asserts what each is for. `_shell_states`
    computes every prefix state in one pass, and `_at_command_position` walks
    by index instead of copying and rescanning the text to its left. The
    length cap is the backstop for what is left, not the thing making this
    pass — which is why the timing row runs at a size the cap ALLOWS.
    """

    PUSH = "git " + "pu" + "sh" + " origin " + "ma" + "in"

    def test_a_large_command_is_judged_quickly(self, tmp_path):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        # Just under the cap, and the worst measured shape: no separator, so
        # every step of the walk used to rescan the whole text to its left.
        unit = self.PUSH + " "
        command = (unit * (30 * 1024 // len(unit)))[:30 * 1024]
        started = time.monotonic()
        decision = TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command)
        elapsed = time.monotonic() - started
        assert decision == "deny"
        # Generous against the 7.8s this shape cost before, and against CI
        # jitter; the point is the order of magnitude, not the number.
        assert elapsed < 3.0, (
            f"{len(command)} characters took {elapsed:.1f}s; the scan is "
            "quadratic again and a slow gate is an open one"
        )

    @pytest.mark.parametrize("hook,verb", (
        ("prevent-direct-push", "git " + "pu" + "sh origin ma" + "in"),
        ("require-preflight", "git " + "com" + "mit -m wip"),
        ("validate-branch-name", "git " + "check" + "out -b nonsense"),
        ("enforce-pr-base-branch", "gh pr " + "cre" + "ate --title x"),
        ("update-changelog-before-pr", "gh pr " + "cre" + "ate --base develop"),
    ))
    def test_a_command_over_the_cap_is_blocked_not_waved_through(
        self, tmp_path, hook, verb
    ):
        """Over the cap the answer is a BLOCK, in every gate.

        The control is the same command under the cap: it must reach the
        gate's own verdict, so this row cannot pass by denying everything.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        padding = "echo " + "x" * 100 + "\n"
        # The verb as ECHO'S ARGUMENT: under the cap this is an ALLOW, so the
        # deny above the cap can only be the cap. A fixture whose verb sits at
        # command position denies either way and proves nothing.
        text = "echo " + verb
        long_pad = padding * ((32 * 1024) // len(padding) + 2)
        assert decide(work, env, hook, long_pad + text) == "deny", (
            "a command too long to analyse was allowed"
        )
        assert decide(work, env, hook, padding * 10 + text) == "allow", (
            "control: under the cap this shape is text, and must allow"
        )
        assert decide(work, env, hook, padding * 10 + verb) == "deny", (
            "control: under the cap a real verb must still reach its verdict"
        )


class TestSubprocessCallsCannotHangOrEscape:
    """#327 I10: no subprocess call in any of these hooks had a timeout.

    Including the ones that go to the network. `subprocess.TimeoutExpired` is
    a `SubprocessError`, but it is NOT a `CalledProcessError` and NOT an
    `OSError`, so a handler naming those two lets a timeout escape as an
    uncaught traceback — rc 1, non-blocking, i.e. the command proceeds.

    Structural, because the alternative is a test that really waits: the
    behavioural row below shortens the timeout in a copy of the hook so a
    stubbed `gh` that sleeps is measured in a second rather than thirty.
    """

    HOOKS = TestHookInputFailsClosed.HOOKS

    # The two hooks that make NO subprocess call at all. They are named rather
    # than derived so that adding a call to one of them fails this file
    # instead of silently escaping every timeout rule below. Neither imports
    # `subprocess`; neither carries a timeout constant, and neither should.
    NO_SUBPROCESS = ("require-preflight", "validate-branch-name")

    @staticmethod
    def _subprocess_calls(hook):
        """(node, source) for every subprocess call in one hook."""
        source = (Path(".claude/hooks") / f"{hook}.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "subprocess"
                    and func.attr in ("run", "check_output", "call",
                                      "check_call", "Popen")):
                yield node, source

    def test_the_two_hooks_with_no_subprocess_calls_still_have_none(self):
        """Confirmed, not assumed: neither imports `subprocess`, so neither
        needs a timeout constant and neither has one."""
        for hook in self.NO_SUBPROCESS:
            source = (Path(".claude/hooks") / f"{hook}.py").read_text(
                encoding="utf-8")
            assert not list(self._subprocess_calls(hook)), hook
            assert "import subprocess" not in source, hook
            assert "_GIT_TIMEOUT" not in source and "_GH_TIMEOUT" not in source, (
                f"{hook} carries a timeout constant it does not use"
            )

    def test_every_subprocess_call_passes_a_BOUNDED_timeout(self):
        """`timeout=` is not enough — `timeout=None` is no timeout at all.

        This test used to assert only that the keyword was present, and
        `_GIT_TIMEOUT = None` therefore restored the original unbounded hang
        with the whole suite still green (#327). The VALUE has to be a number:
        either a literal, or a name this file resolves to a positive number in
        the hook's own module.
        """
        problems = []
        for hook in self.HOOKS:
            if hook in self.NO_SUBPROCESS:
                continue
            namespace = TestTheStateVectorAgreesWithTheScanner._hook_namespace(hook)
            for node, _source in self._subprocess_calls(hook):
                keywords = {kw.arg: kw.value for kw in node.keywords}
                if "timeout" not in keywords:
                    problems.append(f"{hook}:{node.lineno} has no timeout")
                    continue
                value = keywords["timeout"]
                if isinstance(value, ast.Constant):
                    resolved = value.value
                elif isinstance(value, ast.Name):
                    resolved = namespace.get(value.id, None)
                else:
                    resolved = None
                if not isinstance(resolved, (int, float)) or resolved <= 0:
                    problems.append(
                        f"{hook}:{node.lineno} timeout resolves to {resolved!r}")
        assert not problems, (
            "a subprocess call that can hang forever; the harness kills the "
            f"hook, no decision is written, and that is an allow: {problems}"
        )

    def test_no_handler_narrows_to_calledprocesserror(self):
        """`except (subprocess.CalledProcessError, ...)` cannot catch a
        timeout. Every one of them is now `subprocess.SubprocessError`, which
        can."""
        narrow = []
        for hook in self.HOOKS:
            path = Path(".claude/hooks") / f"{hook}.py"
            src = path.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.ExceptHandler) or node.type is None:
                    continue
                named = ast.get_source_segment(src, node.type) or ""
                if "CalledProcessError" in named:
                    narrow.append(f"{hook}:{node.lineno}")
        assert not narrow, (
            f"handlers that a TimeoutExpired escapes: {narrow}"
        )

    @staticmethod
    def _slow_binaries(tmp_path, env, *names, seconds=30):
        """Put `names` on PATH as scripts that never answer in time."""
        binder = tmp_path / ("slow-" + "-".join(names))
        binder.mkdir(exist_ok=True)
        for name in names:
            script = binder / name
            script.write_text(f"#!/bin/sh\nsleep {seconds}\n")
            script.chmod(0o755)
        return dict(env, PATH=str(binder) + os.pathsep + env.get("PATH", ""))

    @staticmethod
    def _shorten_bounds(work, hook, git=1, gh=1):
        """Shrink one copied hook's timeouts so the row takes a second.

        The constants are asserted before being replaced, so this fixture goes
        red rather than quietly doing nothing if either is renamed, retuned or
        set to None.
        """
        path = work / ".claude/hooks" / f"{hook}.py"
        source = path.read_text(encoding="utf-8")
        assert "_GIT_TIMEOUT = 10" in source, hook
        source = source.replace("_GIT_TIMEOUT = 10", f"_GIT_TIMEOUT = {git}")
        if "_GH_TIMEOUT" in source:
            assert "_GH_TIMEOUT = 30" in source, hook
            source = source.replace("_GH_TIMEOUT = 30", f"_GH_TIMEOUT = {gh}")
        path.write_text(source, encoding="utf-8")

    def test_a_git_that_hangs_blocks_the_push(self, tmp_path):
        """The bound on `git branch --show-current`, which had no test.

        Setting `_GIT_TIMEOUT = None` restored the original unbounded hang and
        542 tests still passed (#327). This is the row that notices. It also
        pins the REASON: the push must be blocked for not being able to
        determine the branch, not for some later check happening to fire.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        self._shorten_bounds(work, "prevent-direct-push")
        slow = self._slow_binaries(tmp_path, env, "git")
        started = time.monotonic()
        decision, reason = TestHookBlockingPathsFire._decision_and_reason(
            work, slow, "prevent-direct-push", "git pu" + "sh origin feature/x")
        elapsed = time.monotonic() - started
        assert decision == "deny", "a git that never answers let the push through"
        assert "Cannot determine the current branch" in reason, reason[:160]
        assert elapsed < 15, (
            f"the hook took {elapsed:.1f}s; it waited on git rather than "
            "bounding it"
        )

    def test_a_git_that_hangs_blocks_the_pr_base_gate(self, tmp_path):
        """The same bound in the other gate that reads the branch."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        self._shorten_bounds(work, "enforce-pr-base-branch")
        slow = self._slow_binaries(tmp_path, env, "git")
        started = time.monotonic()
        decision, reason = TestHookBlockingPathsFire._decision_and_reason(
            work, slow, "enforce-pr-base-branch", "gh pr cre" + "ate --title x")
        elapsed = time.monotonic() - started
        assert decision == "deny"
        assert "Cannot determine the current branch" in reason, reason[:160]
        assert elapsed < 15, f"the hook took {elapsed:.1f}s"

    def test_a_working_binary_is_not_blocked_by_the_bound(self, tmp_path):
        """The control for both rows above.

        A one-second bound must not turn an ordinary, fast `git` into a block
        — otherwise the rows above would pass against a gate that denied
        everything, which is the failure this whole change is about.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        self._shorten_bounds(work, "prevent-direct-push")
        decide = TestHookBlockingPathsFire._decide
        assert decide(work, env, "prevent-direct-push",
                      "git pu" + "sh origin feature/probe") == "allow"
        assert decide(work, env, "prevent-direct-push",
                      "git pu" + "sh origin ma" + "in") == "deny"

    def test_a_gh_that_hangs_blocks_the_merge(self, tmp_path):
        """A timeout on the merge path must DENY.

        The hook's own `_GH_TIMEOUT` is shortened in the throwaway copy so the
        row takes a second instead of thirty; what it proves is the handler,
        which is the part that was wrong.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        self._shorten_bounds(work, "enforce-pr-base-branch")
        slow = self._slow_binaries(tmp_path, env, "gh")
        started = time.monotonic()
        decision, reason = TestHookBlockingPathsFire._decision_and_reason(
            work, slow, "enforce-pr-base-branch",
            "gh pr " + "me" + "rge 7 --squash")
        elapsed = time.monotonic() - started
        assert decision == "deny", "a gh that never answers let the merge through"
        assert "could not run" in reason, reason[:160]
        assert elapsed < 25, (
            f"the hook took {elapsed:.1f}s; it waited on gh rather than "
            "bounding it"
        )


class TestPreflightTokenCannotBeLaundered:
    """#327 I11: three ways the preflight gate stood itself down.

    * the token READ caught `(ValueError, OSError)`. A token file of 200,000
      `[` characters raises RecursionError out of `json.load`, which is
      neither — an uncaught traceback, rc 1, non-blocking, and the unverified
      commit proceeded. The path is a predictable, world-writable /tmp name,
      so writing that file needs no privilege.
    * `json` accepts `NaN` and `Infinity`, and both passed the "is it a
      number" test. `current_time > NaN` is False, so the token never expired.
    * `_get_token_path()` calls `os.getcwd()` at IMPORT time. With the working
      directory deleted that raises before any code can decide anything —
      again rc 1, again non-blocking.

    Every row is paired with the same fixture holding a VALID token, which
    must allow; without it a gate that blocked unconditionally would pass.
    """

    COMMIT = "git " + "com" + "mit -m wip"

    @staticmethod
    def _token_path(work):
        digest = hashlib.md5(os.path.realpath(str(work)).encode()).hexdigest()[:8]
        return Path(f"/tmp/.preflight-token-{digest}")

    @pytest.fixture
    def preflight(self, tmp_path):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        token = self._token_path(work)
        yield work, env, token
        if token.exists():
            token.unlink()

    def test_a_valid_token_still_allows(self, preflight):
        """The control every row below needs."""
        work, env, token = preflight
        token.write_text(json.dumps(
            {"expires": int(time.time()) + 300, "checks_run": "all",
             "staged_files": 1}))
        assert TestHookBlockingPathsFire._decide(
            work, env, "require-preflight", self.COMMIT) == "allow"

    @pytest.mark.parametrize("label,body", (
        ("nested brackets over the size cap", "[" * 200_000),
        ("nested brackets under the size cap", "[" * 40_000),
    ))
    def test_a_token_that_cannot_be_parsed_blocks(self, preflight, label, body):
        work, env, token = preflight
        token.write_text(body)
        assert TestHookBlockingPathsFire._decide(
            work, env, "require-preflight", self.COMMIT) == "deny", (
            f"{label}: an unparseable token let the commit through"
        )

    def test_a_valid_but_oversized_token_blocks(self, preflight):
        """A token is a small JSON object; 64 KB of one is not a token.

        This row is what makes the size check observable on its own. An
        unparseable giant is caught by the widened handler whether or not the
        size is checked first, but a giant that parses CLEANLY and carries a
        future expiry would otherwise be honoured.
        """
        work, env, token = preflight
        token.write_text(json.dumps(
            {"expires": int(time.time()) + 300, "checks_run": "all",
             "pad": "x" * (80 * 1024)}))
        assert TestHookBlockingPathsFire._decide(
            work, env, "require-preflight", self.COMMIT) == "deny"

    @pytest.mark.parametrize("expires", ("NaN", "Infinity", "-Infinity"))
    def test_a_token_that_never_expires_blocks(self, preflight, expires):
        work, env, token = preflight
        token.write_text('{"expires": %s, "checks_run": "all"}' % expires)
        assert TestHookBlockingPathsFire._decide(
            work, env, "require-preflight", self.COMMIT) == "deny", (
            f"expires={expires} produced a token that never expires"
        )

    def test_an_expired_token_still_blocks(self, preflight):
        """The ordinary expiry path, which the negated comparison must keep."""
        work, env, token = preflight
        token.write_text(json.dumps({"expires": int(time.time()) - 10}))
        assert TestHookBlockingPathsFire._decide(
            work, env, "require-preflight", self.COMMIT) == "deny"

    def test_a_deleted_working_directory_blocks(self, tmp_path):
        """`os.getcwd()` at import time, with no CLAUDE_PROJECT_DIR to use
        instead. Before the guard this was a traceback at rc 1."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        gone = tmp_path / "gone"
        gone.mkdir()
        env = {k: v for k, v in env.items() if k != "CLAUDE_PROJECT_DIR"}
        script = (f'cd {gone} && rmdir {gone} && '
                  f'exec {sys.executable} {work}/.claude/hooks/require-preflight.py')
        proc = subprocess.run(
            ["/bin/sh", "-c", script],
            input=json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": self.COMMIT}}),
            capture_output=True, text=True, timeout=60, cwd=str(gone), env=env,
        )
        assert proc.returncode in (0, 2), (
            f"a deleted cwd exited {proc.returncode}; anything but a decision "
            f"at rc 0, or rc 2, lets the commit through: {proc.stderr[-300:]!r}"
        )
        decision = "deny" if proc.returncode == 2 else None
        if decision is None:
            try:
                decision = json.loads(proc.stdout)["hookSpecificOutput"].get(
                    "permissionDecision", "allow")
            except (ValueError, KeyError, TypeError):
                decision = "allow"
        assert decision == "deny", (
            "a deleted cwd let the unverified commit through"
        )


class TestTheStateVectorAgreesWithTheScanner:
    """`_shell_states(cmd)[k]` must be `_shell_scan(cmd[:k])`, at every k.

    `_shell_scan` is kept as the reference implementation precisely so this
    can be asserted rather than argued: the single-pass version is what every
    caller now uses, and a divergence in it moves a verb between "quoted",
    "subst" and "exec" — which is the difference between a gate standing down
    and firing.

    The corpus is not a handful of nice strings. It is every string of length
    up to three over the alphabet that matters (quotes, backslash, backtick,
    `$`, parens, space, newline), plus a seeded random sample of longer ones,
    plus the shapes earlier defects were found in. Both functions are read out
    of the real hook file, so this cannot drift from what ships.
    """

    @staticmethod
    def _hook_namespace(hook="prevent-direct-push"):
        """Execute a hook module and hand back its globals.

        Its top level reads stdin and exits, so stdin is a payload the gate
        stands down on and SystemExit is expected. Reading the constants out
        of the real module is the point: a copy in this file could drift from
        the pattern that ships.
        """
        path = Path(f".claude/hooks/{hook}.py")
        namespace = {"__name__": "probe"}
        payload = json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": "true"}})
        real_stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"),
                 namespace)
        except SystemExit:
            pass
        finally:
            sys.stdin = real_stdin
        return namespace

    @staticmethod
    def _corpus():
        alphabet = "'\"`$()\\ a\n)"
        corpus = [
            "", "git push origin main", "echo 'x' && git push",
            '$(git push)', "`git push`", "(git push)", ")", "()", "(()",
            "echo don't && git push", 'echo "unclosed', "echo 'unclosed",
            'echo "a\\"b" && git push', "cd /a && (cd /b && git push) && git push",
            'echo "x; cd /tmp" && git push', "2>&1 git push", "x=`a` git push",
            "$($($(a)))", "```", "`a`b`c`", "\\$(a)", "$\\(a)", '"$(a)"',
        ]
        for size in (1, 2, 3):
            for combo in itertools.product(alphabet, repeat=size):
                corpus.append("".join(combo))
        rng = random.Random(20260814)
        for _ in range(1500):
            corpus.append("".join(rng.choice(alphabet + "bc;&|")
                                  for _ in range(rng.randint(4, 24))))
        return corpus

    def test_every_prefix_state_matches_the_reference_scanner(self):
        namespace = self._hook_namespace()
        scan, states = namespace["_shell_scan"], namespace["_shell_states"]
        compared, mismatches = 0, []
        for command in self._corpus():
            vector = states(command)
            assert len(vector) == len(command) + 1, (
                f"the vector is not one entry per prefix: {command!r}"
            )
            for k in range(len(command) + 1):
                compared += 1
                expected = scan(command[:k])
                if vector[k] != expected and len(mismatches) < 10:
                    mismatches.append((command, k, vector[k], expected))
        assert compared > 20_000, (
            f"only {compared} offsets compared; the corpus stopped covering"
        )
        assert not mismatches, (
            "the single-pass scan disagrees with the reference scanner:\n  "
            + "\n  ".join(repr(row) for row in mismatches)
        )


class TestTheVerbPatternIsLinear:
    """#327 I12: the optional path prefix was quadratic, with ZERO matches.

    `(?:[^\\s;&|()<>]*/)?git\\b` made the engine scan forward from every offset
    looking for a `/` that is not there. Measured on `"a" * n`, no match
    anywhere in it:

        4 KB -> 0.029s    16 KB -> 0.451s    64 KB -> 7.205s    256 KB -> 115.7s

    Four times the length, sixteen times the time. End-to-end, a plain `echo
    <64 KB of base64>` cost 7.29s inside the hook and then allowed. The
    trigger is mundane: a base64 blob, a data URI, a minified bundle, a
    `--data` payload.

    This is NOT the quadratic scan the state vector fixed. It fires with no
    matches at all, so `_shell_scan` and `_at_command_position` are never
    reached, and memoizing them leaves it exactly in place. Two independent
    defects, two independent tests.

    Time-bounded rather than verdict-bounded, because the verdict was never
    wrong — and read out of the hook's own module so it cannot drift from what
    ships. A generous bound: the fixed pattern does this in under 0.01s, and
    the broken one needs seven seconds.
    """

    HOOK_PATTERNS = (
        ("prevent-direct-push", "_GIT_PUSH_RE"),
        ("require-preflight", "_GIT_COMMIT_RE"),
        ("validate-branch-name", "_GIT_CHECKOUT_B_RE"),
        ("enforce-pr-base-branch", "_GH_PR_CREATE_RE"),
        ("enforce-pr-base-branch", "_GH_PR_MERGE_RE"),
        ("update-changelog-before-pr", "_GH_PR_CREATE_RE"),
    )

    @classmethod
    def _patterns(cls):
        for hook, name in cls.HOOK_PATTERNS:
            namespace = TestTheStateVectorAgreesWithTheScanner._hook_namespace(hook)
            assert name in namespace, f"{hook} defines no {name}"
            yield f"{hook}:{name}", namespace[name]

    def test_a_long_word_with_no_match_is_searched_in_linear_time(self):
        blob = "a" * 65536
        slow = []
        for label, pattern in self._patterns():
            started = time.monotonic()
            assert re.search(pattern, blob) is None, label
            elapsed = time.monotonic() - started
            if elapsed > 1.0:
                slow.append(f"{label}: {elapsed:.2f}s")
        assert not slow, (
            "the verb pattern is quadratic in the command text; a hook the "
            f"harness kills writes no decision, and that is an allow: {slow}"
        )

    def test_the_same_blob_through_the_real_hook_is_fast(self, tmp_path):
        """End-to-end, and under the length cap so the cap is not what saves it."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = "echo " + "a" * (30 * 1024)
        started = time.monotonic()
        decision = TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command)
        elapsed = time.monotonic() - started
        assert decision == "allow"
        assert elapsed < 3.0, (
            f"a 30 KB blob with no verb in it took {elapsed:.1f}s"
        )


class TestTheGitFlowFinishPathIsReachable:
    """#327 I13: two widened handlers no test could reach.

    The Git Flow finish block only runs when the current branch IS `main` or
    `develop`, and every fixture in this file is on `feature/probe`. So the
    two `except (SubprocessError, OSError)` handlers in it — the one that
    decides "HEAD is not a merge commit" and the one around the recent-log
    lookup — were never executed by anything, widened or not.

    These rows put the probe repo on those branches. They also pin the
    behaviour the block exists for, which nothing else did: a Git Flow finish
    merge on `main` allows, and an ordinary commit on `main` does not.
    """

    PUSH = "git pu" + "sh origin "
    MAIN = "ma" + "in"

    @staticmethod
    def _on(work, env, branch, message=None, merge=False):
        """Move the probe repo onto `branch`, optionally with a merge HEAD."""
        run = lambda *args: subprocess.run(
            ["git", "-C", str(work), *args], env=env, check=True,
            capture_output=True)
        run("checkout", "-q", "-B", branch)
        if merge:
            run("checkout", "-q", "-B", "tmp-side")
            run("-c", "user.email=t@example.invalid", "-c", "user.name=t",
                "commit", "-q", "--allow-empty", "-m", "side")
            run("checkout", "-q", branch)
            run("-c", "user.email=t@example.invalid", "-c", "user.name=t",
                "merge", "-q", "--no-ff", "-m", message or "merge", "tmp-side")
        elif message:
            run("-c", "user.email=t@example.invalid", "-c", "user.name=t",
                "commit", "-q", "--allow-empty", "-m", message)

    def test_a_git_flow_finish_merge_on_main_allows(self, tmp_path):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        self._on(work, env, self.MAIN, "Merge release/v1.2.0 into main",
                 merge=True)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", self.PUSH + self.MAIN) == "allow"

    def test_an_ordinary_commit_on_main_still_denies(self, tmp_path):
        """The control: without the merge, the same push on the same branch
        must be denied, or the row above passes by allowing everything."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        self._on(work, env, self.MAIN, "ordinary work")
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", self.PUSH + self.MAIN) == "deny"

    def test_a_feature_merge_on_main_still_denies(self, tmp_path):
        """A merge commit is not enough — features never merge to main."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        self._on(work, env, self.MAIN, "Merge feature/x into main", merge=True)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", self.PUSH + self.MAIN) == "deny"

    def test_a_version_bump_after_a_release_allows_on_develop(self, tmp_path):
        """The recent-log branch, which is the SECOND unreachable handler: it
        runs only when HEAD is not a merge and the branch is develop."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        self._on(work, env, "develop", "chore: bump after release/v1.2.0")
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", self.PUSH + "develop") == "allow"

    def test_ordinary_work_on_develop_still_denies(self, tmp_path):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        self._on(work, env, "develop", "feat: something unrelated")
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", self.PUSH + "develop") == "deny"

    def test_an_unusable_git_on_main_blocks_instead_of_crashing(self, tmp_path):
        """Both handlers, with `git` unusable.

        This is what widening them to `SubprocessError` is FOR: reaching them
        at all needs the branch to be main or develop, and reaching them with
        a failure that is not a `CalledProcessError` needs a git that cannot
        run. Before, this exited 1 — non-blocking — and the push proceeded.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        self._on(work, env, self.MAIN, "Merge release/v1.2.0 into main",
                 merge=True)
        broken = TestGitSubprocessHandlersAreWideEnough._shimmed(
            tmp_path, env, "git")
        assert TestHookBlockingPathsFire._decide(
            work, broken, "prevent-direct-push",
            self.PUSH + self.MAIN) == "deny", (
            "an unusable git turned the Git Flow finish path into an allow"
        )


class TestASubstitutionInsideDoubleQuotesStillRuns:
    """#327 I14: `_shell_scan` jumped over a `"..."` span in one step.

    Nothing inside the span reached the stack, so a command substitution
    written there read as ordinary quoted text: the occurrence was dropped as
    "quoted", the hook exited 0 with an empty decision, and that is an ALLOW.
    Command substitution IS performed inside double quotes, so these really
    run. Measured, working tree against `develop`:

        echo "$(<push> origin main)"   develop=deny   tree=ALLOW
        echo "`<push> origin main`"    develop=deny   tree=ALLOW
        echo "$(<commit> -m x)"        develop=deny   tree=ALLOW

    The double quote is a toggle now, and the substitution test runs before
    the quote test so that `echo "$(` is "subst" and keeps its verb rather
    than "quoted", which would drop it.

    The controls are the whole point of the fix's shape: a verb that is merely
    QUOTED, with no substitution around it, must still allow — that is the
    false-deny this change exists to deliver, and a fix that made every double
    quote gated would take it back.
    """

    BT = chr(96)
    PUSH = "git pu" + "sh origin ma" + "in"
    COMMIT = "git com" + "mit -m x"

    GATED = (
        ("$( ) inside double quotes", 'echo "$({v})"', "prevent-direct-push"),
        ("backticks inside double quotes",
         'echo "' + BT + '{v}' + BT + '"', "prevent-direct-push"),
        ("nested, unquoted outside", 'x="$({v})"', "prevent-direct-push"),
        ("after other quoted text", 'echo "a b" "$({v})"', "prevent-direct-push"),
        # The unquoted forms, which were already gated: they must stay that way.
        ("$( ) unquoted", "$({v})", "prevent-direct-push"),
        ("backticks unquoted", BT + "{v}" + BT, "prevent-direct-push"),
    )
    TEXT = (
        ("a verb inside plain double quotes", 'echo "{v}"'),
        ("a verb inside single quotes", "echo '{v}'"),
        ("a verb quoted after a real command", 'true && echo "{v}"'),
        ("double quotes around an ordinary word", 'echo "a b" && echo {v}'),
    )

    @pytest.mark.parametrize("label,template,hook", GATED)
    def test_a_substitution_that_runs_is_gated(self, tmp_path, label, template, hook):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = template.format(v=self.PUSH)
        assert TestHookBlockingPathsFire._decide(
            work, env, hook, command) == "deny", (
            f"{label}: a live substitution read as quoted text: {command!r}"
        )

    def test_the_commit_gate_reads_it_the_same_way(self, tmp_path):
        """One scanner, five copies: a fix in one is a fix in none."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        assert decide(work, env, "require-preflight",
                      'echo "$(%s)"' % self.COMMIT) == "deny"
        assert decide(work, env, "require-preflight",
                      'echo "%s"' % self.COMMIT) == "allow"

    @pytest.mark.parametrize("label,template", TEXT)
    def test_a_merely_quoted_verb_still_allows(self, tmp_path, label, template):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = template.format(v=self.PUSH)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == "allow", (
            f"{label}: making the double quote a toggle re-broke the "
            f"false-deny fix: {command!r}"
        )


class TestAHatchCannotExcuseAProtectedRefBesideIt:
    """#327 I15: the escape hatches were substrings one level down.

    Scoping each hatch to the command that carries it was half the fix. Within
    that span the tests were still `"--tags" in span`, so hatch text sitting in
    the SAME push's argument list stood the gate down. All four ALLOW before
    this fix, and all four are real git that really does push main — `--tags`
    pushes tags IN ADDITION to the refspec, it does not replace it:

        <push> origin refs/tags/v1 main
        <push> --tags origin main
        <push> --delete origin release/x main
        <push> --force origin main refs/tags/v1

    And the span does not end at a redirect or a process substitution, neither
    of which is an argument to `git push`, so a hatch could be read out of a
    filename or a subshell:

        <push> origin main > /tmp/refs/tags/log
        <push> origin main 2> refs/tags/err
        <push> origin main <(echo --tags)
        <push> origin main --push-option="see --tags"

    The controls are the Git Flow pushes this gate exists to permit. Two of
    them only reach the hatch at all from `main`, which is the branch the
    hatch is for, so those rows move the probe repo there.
    """

    PUSH = "git pu" + "sh"
    MAIN = "ma" + "in"

    GATED = (
        ("a tag ref beside main", "{p} origin refs/tags/v1 {m}"),
        ("--tags beside main", "{p} --tags origin {m}"),
        ("--delete release beside main", "{p} --delete origin release/x {m}"),
        ("a force push with a tag after it", "{p} --force origin {m} refs/tags/v1"),
        ("a hatch inside a redirect target", "{p} origin {m} > /tmp/refs/tags/log"),
        ("a hatch as a redirect target", "{p} origin {m} 2> refs/tags/err"),
        ("a hatch inside a process substitution", "{p} origin {m} <(echo --tags)"),
        ("a hatch inside a push-option", '{p} origin {m} --push-option="see --tags"'),
        ("a refspec to main beside a tag", "{p} origin HEAD:{m} refs/tags/v1"),
    )
    ALLOWED_ON_FEATURE = (
        ("a version tag push", "{p} origin v1.2.3 --tags"),
        ("a release branch deletion", "{p} --delete origin release/x"),
        ("a hotfix branch deletion", "{p} --delete origin hotfix/x"),
        ("an ordinary feature push", "{p} origin feature/probe"),
    )
    ALLOWED_ON_MAIN = (
        ("a version tag push from main", "{p} origin v1.2.3 --tags"),
        ("a refs/tags push from main", "{p} origin refs/tags/v1.2.3"),
        ("a release deletion from main", "{p} --delete origin release/x"),
    )

    def _fill(self, template):
        return template.format(p=self.PUSH, m=self.MAIN)

    @pytest.mark.parametrize("label,template", GATED)
    def test_a_hatch_word_does_not_excuse_the_protected_ref(
        self, tmp_path, label, template
    ):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = self._fill(template)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == "deny", (
            f"{label}: a hatch excused a push of {self.MAIN}: {command!r}"
        )

    @pytest.mark.parametrize("label,template", ALLOWED_ON_FEATURE)
    def test_the_git_flow_pushes_still_work(self, tmp_path, label, template):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = self._fill(template)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == "allow", (
            f"{label}: word-matching the hatch broke a real Git Flow push: "
            f"{command!r}"
        )

    @pytest.mark.parametrize("label,template", ALLOWED_ON_MAIN)
    def test_the_hatch_still_opens_from_main(self, tmp_path, label, template):
        """From `main` every push is denied unless a hatch opens it, so these
        rows are what proves the hatch still exists at all."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        subprocess.run(["git", "-C", str(work), "checkout", "-q", "-B", self.MAIN],
                       env=env, check=True, capture_output=True)
        command = self._fill(template)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == "allow", (
            f"{label}: the hatch no longer opens from {self.MAIN}: {command!r}"
        )

    # From `main` every push is denied unless a hatch opens it, so this is
    # where a hatch that is too GENEROUS shows up — as a false allow. Each row
    # carries hatch TEXT that is not a hatch: a word that merely contains
    # `--tags`, a `refs/tags/` that is a redirect target rather than a ref, and
    # a `--delete` of something that is not a release or hotfix branch. Without
    # these the substring/word distinction is invisible, because a protected
    # ref is decided before the hatch is consulted at all.
    DENIED_FROM_MAIN = (
        ("--tags inside a push-option",
         '{p} origin feature/x --push-option="see --tags"'),
        ("--tags inside a process substitution",
         "{p} origin feature/x <(echo --tags)"),
        ("refs/tags as a redirect target",
         "{p} origin feature/x 2> refs/tags/err"),
        ("refs/tags inside a redirect path",
         "{p} origin feature/x > /tmp/refs/tags/log"),
        ("deleting something that is not a release branch",
         "{p} --delete origin feature/x"),
        ("the control: an ordinary push", "{p} origin feature/x"),
    )

    @pytest.mark.parametrize("label,template", DENIED_FROM_MAIN)
    def test_hatch_text_that_is_not_a_hatch_does_not_open_from_main(
        self, tmp_path, label, template
    ):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        subprocess.run(["git", "-C", str(work), "checkout", "-q", "-B", self.MAIN],
                       env=env, check=True, capture_output=True)
        command = self._fill(template)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == "deny", (
            f"{label}: hatch text that is not a hatch opened the gate: "
            f"{command!r}"
        )


class TestTheLastBaseWins:
    """#327 I16: `--base` took the first match; `gh` honours the last.

    The same defect as the PR number, inside one command rather than across
    two. `gh pr CREATE --base develop --base main` created a feature->main PR
    and ALLOWED, while the reversed spelling denied — an asymmetry that proves
    it was first-match rather than a rule.

    Two different bases in one create is refused rather than guessed at; the
    same base twice is not. `--base` is read by WORD now, so a `--base`
    written inside a title or a body is a mention and not a base — which also
    fixes a false deny nobody had noticed: `gh pr CREATE --base develop
    --title "not --base main"` used to read `main"` out of the title.
    """

    CREATE = "gh pr " + "cre" + "ate"
    MAIN = "ma" + "in"

    DENIED = (
        ("develop then main", "{c} --base develop --base {m}"),
        ("main then develop", "{c} --base {m} --base develop"),
        ("attached spellings", "{c} --base=develop --base={m}"),
        ("mixed spellings", "{c} --base develop --base={m}"),
        ("only a base in the title", '{c} --title "use --base develop"'),
        # Unquoted, and the VALUE of a flag. `_WORD_RE` keeps a quoted span
        # inside its word, so only this shape can tell whether the value of a
        # value-taking flag is skipped: here `--base` is the title, and
        # `develop` is a positional, so this create has no base at all.
        ("a --base that is a title's value", "{c} --title --base develop"),
    )
    ALLOWED = (
        ("one base", "{c} --base develop"),
        ("attached base", "{c} --base=develop"),
        ("the same base twice", "{c} --base develop --base develop"),
        ("a base plus a quoted mention of another",
         '{c} --base develop --title "not --base {m}"'),
        ("a base plus a body mentioning another",
         '{c} --base develop --body "supersedes --base {m}"'),
    )

    def _fill(self, template):
        return template.format(c=self.CREATE, m=self.MAIN)

    @pytest.mark.parametrize("label,template", DENIED)
    def test_an_ambiguous_or_missing_base_is_blocked(self, tmp_path, label, template):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = self._fill(template)
        assert TestHookBlockingPathsFire._decide(
            work, env, "enforce-pr-base-branch", command) == "deny", (
            f"{label}: {command!r}"
        )

    @pytest.mark.parametrize("label,template", ALLOWED)
    def test_a_single_base_of_develop_still_allows(self, tmp_path, label, template):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = self._fill(template)
        assert TestHookBlockingPathsFire._decide(
            work, env, "enforce-pr-base-branch", command) == "allow", (
            f"{label}: reading --base by word broke a compliant create: "
            f"{command!r}"
        )

    def test_the_occurrence_loop_does_not_key_on_the_connector(self, tmp_path):
        """`;` chains the same way `&&` does, and the fix must not care."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        c, m = self.CREATE, self.MAIN
        assert decide(work, env, "enforce-pr-base-branch",
                      f"{c} --base develop --title a; {c} --base {m} --title b"
                      ) == "deny"
        assert decide(work, env, "enforce-pr-base-branch",
                      f"{c} --base develop --title a; {c} --base develop --title b"
                      ) == "allow"


class TestAllowSideHatchesAreScopedToTheirCommand:
    """#327 I3/I4/S2: the gates were narrow where they blocked and wide where
    they stood down.

    Every deny-side test in this change asks what a shell would RUN. Every
    allow-side escape hatch still asked whether some characters appear
    anywhere in the command text — so each hatch was an off switch for the
    whole line, reachable by writing about it. Measured on develop and on the
    first cuts of this change:

        <push> origin main  # see refs/tags/v1                     ALLOW
        echo 'refs/tags/x' && <push> origin main                   ALLOW
        echo '--tags' && <push> origin main                        ALLOW
        <push> --delete origin release/x && <push> origin main     ALLOW
        <push> origin main && <push> origin v1.2.3                 ALLOW
        <commit> -m "document SKIP_PREFLIGHT=1 escape hatch"       ALLOW
        grep -r 'SKIP_PREFLIGHT=1' . && <commit> -m wip            ALLOW
        echo --base develop && gh pr create --title x              ALLOW

    The commit-message row is the one that needs no adversary: documenting the
    escape hatch in a commit message silently skips preflight.

    Each hatch is now read from the occurrence's own argument span, and each
    row below is paired with the LEGITIMATE use of the same hatch, which must
    still allow — a hatch that never applies is not a fix.
    """

    PUSH = "git pu" + "sh"
    MAIN = "ma" + "in"
    COMMIT = "git com" + "mit"

    HATCH_MUST_NOT_APPLY = (
        "{p} origin " + MAIN + "  # see refs/tags/v1",
        "echo 'refs/tags/x' && {p} origin " + MAIN,
        "echo '--tags' && {p} origin " + MAIN,
        "{p} --delete origin release/x && {p} origin " + MAIN,
        "{p} origin " + MAIN + " && {p} origin v1.2.3",
        "{p} origin v1.2.3 && {p} origin " + MAIN,
    )

    HATCH_STILL_APPLIES = (
        "{p} origin v1.2.3",
        "{p} --tags",
        "{p} origin refs/tags/v1.2.3",
        "{p} --delete origin release/v1.2.3",
        "{p} origin feature/probe",
    )

    @pytest.mark.parametrize("template", HATCH_MUST_NOT_APPLY)
    def test_a_hatch_does_not_excuse_a_different_push(self, tmp_path, template):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = template.format(p=self.PUSH)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == "deny", (
            f"a protected push was excused by text belonging to another "
            f"command: {command!r}"
        )

    @pytest.mark.parametrize("template", HATCH_STILL_APPLIES)
    def test_the_hatch_still_opens_for_its_own_push(self, tmp_path, template):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = template.format(p=self.PUSH)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == "allow", (
            f"Git Flow's own operations must still pass: {command!r}"
        )

    SKIP_MUST_NOT_APPLY = (
        '{c} -m "document SKIP_PREFLIGHT=1 escape hatch"',
        "grep -r 'SKIP_PREFLIGHT=1' . && {c} -m wip",
        'echo "; SKIP_PREFLIGHT=1 " && {c} -m wip',
    )

    @pytest.mark.parametrize("template", SKIP_MUST_NOT_APPLY)
    def test_writing_about_the_skip_flag_does_not_set_it(self, tmp_path, template):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = template.format(c=self.COMMIT)
        assert TestHookBlockingPathsFire._decide(
            work, env, "require-preflight", command) == "deny", (
            f"preflight was skipped by text that sets nothing: {command!r}"
        )

    SKIP_REALLY_SET = (
        "SKIP_PREFLIGHT=1 {c} -m wip",
        "true && SKIP_PREFLIGHT=1 {c} -m wip",
        # `env VAR=value command` is an ordinary spelling of an assignment, and
        # develop allowed it. The first cut of this gate anchored the hatch to
        # a separator, which denied it — a false deny introduced by this
        # change, and the reason the hatch is now judged by the same
        # command-position walk as the verb rather than by its own regex.
        "env SKIP_PREFLIGHT=1 {c} -m wip",
        "sudo SKIP_PREFLIGHT=1 {c} -m wip",
    )

    @pytest.mark.parametrize("template", SKIP_REALLY_SET)
    def test_the_skip_flag_still_works_when_it_is_really_set(
        self, tmp_path, template
    ):
        """The emergency hatch is deliberate and must survive: an assignment
        the shell would really make, at a command position."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        assert TestHookBlockingPathsFire._decide(
            work, env, "require-preflight",
            template.format(c=self.COMMIT)) == "allow", template

    def test_the_skip_flag_is_a_whole_assignment(self, tmp_path):
        """`SKIP_PREFLIGHT=12` is a different value, and `echo SKIP_...` is an
        argument to echo. Neither sets anything, and both must still block —
        the boundary the token check draws, and the walk the `env` row above
        needs, are not the same check."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        for command in ("SKIP_PREFLIGHT=12 " + self.COMMIT + " -m wip",
                        "echo SKIP_PREFLIGHT=1 && " + self.COMMIT + " -m wip"):
            assert decide(work, env, "require-preflight", command) == "deny", command

    def test_an_earlier_base_does_not_satisfy_a_later_create(self, tmp_path):
        """`--base` was searched across the whole command, so any earlier
        mention satisfied a create that had none of its own — and GitHub
        defaults those to main, which is the thing this gate exists to stop."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        create = "gh pr cre" + "ate --title x"
        assert decide(work, env, "enforce-pr-base-branch",
                      "echo --base develop && " + create) == "deny"
        assert decide(work, env, "enforce-pr-base-branch",
                      "gh pr cre" + "ate --base develop") == "allow"

    def test_the_pr_number_comes_from_the_merge_being_run(self, tmp_path):
        """A digit grabbed from anywhere after the verb, so
        `gh pr merge --squash && echo 123` verified PR #123 and merged
        whichever PR the branch points at.

        Both readings deny here (gh is unusable), so the decision cannot tell
        them apart — the REASON can, and it is what names the PR the gate
        thought it was checking.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        env = TestGitSubprocessHandlersAreWideEnough._shimmed(
            tmp_path, env, "git", "gh")
        proc = subprocess.run(
            [sys.executable, str(work / ".claude/hooks/enforce-pr-base-branch.py")],
            input=json.dumps({"tool_name": "Bash", "tool_input": {
                "command": "gh pr me" + "rge --squash && echo 123"}}),
            capture_output=True, text=True, timeout=60, cwd=work, env=env,
        )
        assert proc.returncode == 0, proc.stderr[-300:]
        reason = json.loads(proc.stdout)["hookSpecificOutput"][
            "permissionDecisionReason"]
        assert "123" not in reason, (
            f"the gate read a digit from another command as the PR number: "
            f"{reason!r}"
        )
        assert "Cannot determine PR number" in reason, reason


class TestProtectedRefIsProtectedOnAnyRemote:
    """#327 S1: the rule keyed off the remote NAME, so only `origin` counted.

    `git push upstream main` and `git push git@github.com:o/r.git main` push
    main exactly as hard, and both allowed on develop and on the first cut of
    this change. Once a push verb is established, a WORD among its arguments
    that IS `main`/`develop` is a ref — the remote it goes to does not change
    what is being overwritten.

    This is the one place the change widens what denies rather than narrowing
    it, so the controls matter more than usual: an ordinary branch push to a
    non-`origin` remote, a branch merely CONTAINING the word, and the word
    inside a quoted sentence must all still allow.
    """

    PUSH = "git pu" + "sh"
    MAIN = "ma" + "in"

    @pytest.mark.parametrize("command,expected", (
        ("{p} upstream " + MAIN, "deny"),
        ("{p} fork develop", "deny"),
        ("{p} git@github.com:o/r.git " + MAIN, "deny"),
        ("{p} https://host/o/r.git HEAD:" + MAIN, "deny"),
        ("{p} upstream refs/heads/" + MAIN, "deny"),
        ("{p} upstream " + MAIN + "^{}", "deny"),
        ("{p} upstream " + MAIN + "~1", "deny"),
        # Controls: ordinary work to any remote must still pass.
        ("{p} upstream feature/probe", "allow"),
        ("{p} upstream " + MAIN + "tenance", "allow"),
        ("{p} upstream develop-x", "allow"),
        ("{p} upstream feature/" + MAIN, "allow"),
        # The word inside a quoted SENTENCE is not a ref. This is what the
        # whole-word comparison buys over a substring search, and the row
        # above it (`origin 'main'`, in the quoted-ref test) is what it must
        # not cost.
        ('{p} upstream feature/probe -o "deploy to ' + MAIN + '"', "allow"),
    ))
    def test_protected_ref_on_any_remote(self, tmp_path, command, expected):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        # .replace, not .format: one of the refs below is `main^{}`, and a peel
        # suffix is a format placeholder as far as str.format is concerned.
        command = command.replace("{p}", self.PUSH)
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == expected, command


class TestEveryBranchCreatingFormIsGated:
    """#327 S4: `checkout -b` is one of three spellings.

    `git checkout -B <name>` creates (or resets) a branch and `git switch -c
    <name>` creates one, and a gate that knows only `checkout -b` lets a
    non-conforming name in by either. Both allowed on develop too.
    """

    @pytest.mark.parametrize("command,expected", (
        ("git chec" + "kout -b nonsense", "deny"),
        ("git chec" + "kout -B nonsense", "deny"),
        ("git swi" + "tch -c nonsense", "deny"),
        ("git swi" + "tch -C nonsense", "deny"),
        ("git swi" + "tch --create nonsense", "deny"),
        ("git swi" + "tch --force-create nonsense", "deny"),
        ("git -C . swi" + "tch -c nonsense", "deny"),
        ("git -C . swi" + "tch --force-create nonsense", "deny"),
        ("git chec" + "kout -B feature/ok", "allow"),
        ("git swi" + "tch -c feature/ok", "allow"),
        ("git swi" + "tch -C feature/ok", "allow"),
        ("git swi" + "tch --create feature/ok", "allow"),
        ("git swi" + "tch --force-create feature/ok", "allow"),
        # Switching to a branch that exists creates nothing, and a gate that
        # denied it would block ordinary work.
        ("git swi" + "tch existing-branch", "allow"),
        ("git chec" + "kout existing-branch", "allow"),
        ("git swi" + "tch --detach HEAD~1", "allow"),
        ("echo git swi" + "tch -c nonsense", "allow"),
    ))
    def test_branch_creation_decision(self, tmp_path, command, expected):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        assert TestHookBlockingPathsFire._decide(
            work, env, "validate-branch-name", command) == expected, command


class TestPreflightTokenShapeFailsClosed:
    """#327 S6: a token that PARSES is not yet a token that can be read.

    `json.load` returning `[]`, `5` or `"x"` is not a JSONDecodeError, so the
    handler did not catch it — and `token_data.get(...)` then raised
    AttributeError. An `expires` that is a string raises TypeError at the
    comparison instead. Both are tracebacks, both exit 1, and rc 1 is a
    NON-BLOCKING error under the PreToolUse contract: the unverified commit
    proceeds. Same fail-open class this change closes elsewhere.

    The valid-token row is the control: a gate that rejected every token would
    pass every row above while making the preflight mechanism unusable.
    """

    @staticmethod
    def _token_path(work):
        """The hook's own formula, not a copy of its output."""
        return Path("/tmp") / (
            ".preflight-token-"
            + hashlib.md5(str(Path(work).resolve()).encode()).hexdigest()[:8]
        )

    # (body, expected, reason fragment). The reason matters: EVERY malformed
    # token denies, and `{"expires": true}` denies even with the shape check
    # deleted, because `True` reaches `current_time > True` and reads as long
    # expired. A verdict-only assertion is therefore satisfied by a gate that
    # never validated the shape at all — so each row names the reason it must
    # be blocked FOR (#327).
    @pytest.mark.parametrize("body,expected,reason", (
        ("[]", "deny", "Invalid preflight token"),
        ("5", "deny", "Invalid preflight token"),
        ('"x"', "deny", "Invalid preflight token"),
        ("null", "deny", "Invalid preflight token"),
        ('{"expires": "soon"}', "deny", "Invalid preflight token"),
        ('{"expires": true}', "deny", "Invalid preflight token"),
        ("not json at all", "deny", "Invalid preflight token"),
        # An expiry that IS a number and IS in the past blocks for the other
        # reason, which is what makes the rows above discriminating.
        ('{"expires": 1}', "deny", "token expired"),
        # The control: a well-formed, unexpired token allows, and is what the
        # rows above must be distinguished FROM.
        (None, "allow", ""),
    ))
    def test_a_malformed_token_blocks_instead_of_crashing(
        self, tmp_path, body, expected, reason
    ):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        token = self._token_path(work)
        if body is None:
            body = json.dumps({"expires": int(time.time()) + 300,
                               "checks_run": "test", "staged_files": 1})
        token.write_text(body, encoding="utf-8")
        command = "git com" + "mit -m wip"
        try:
            decision, blocked_with = (
                TestHookBlockingPathsFire._decision_and_reason(
                    work, env, "require-preflight", command))
            assert decision == expected, body
            if reason:
                assert reason.lower() in blocked_with.lower(), (
                    f"{body} was blocked, but not for the reason that makes "
                    f"the row discriminating: {blocked_with[:140]!r}"
                )
        finally:
            if token.exists():
                token.unlink()
