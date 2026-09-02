"""Security hardening tests for obsidian-brain."""
import ast
import json
import os
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
                if "re.search" in test_src and "pr" in test_src:
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
            assert "_targets_this_project" in defined, f"{path} defines no guard"

            closure, pending = {}, ["_targets_this_project"]
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
        src = Path(".claude/hooks/require-preflight.py").read_text(encoding="utf-8")
        node = [
            n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef) and n.name == "_targets_this_project"
        ][0]
        namespace = {"os": os_module, "re": re}
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


class TestRefspecPushesToProtectedBranchesAreBlocked:
    """#327: a refspec push to a protected branch fell through to allow.

    `targets_protected` was built INCLUDING the refspec spellings, under a
    comment saying so — and the very next `if` re-tested a strictly narrower
    condition (the current branch, or the literal "origin main"/"origin
    develop") that no refspec form can satisfy. Every refspec arm was computed
    and then discarded, so a force-push over `main` was permitted in practice.

    The narrowing `if` is gone, which makes `targets_protected` decide for the
    first time. That is also why the refspec arms had to stop being substring
    tests in the same change: `":main" in command` fires on `foo:mainline` and
    `HEAD:maintenance` too, and those are ordinary branches. Measured before
    the regex went in, both denied — a false deny that only appeared once the
    condition started deciding anything.

    Command strings are assembled from fragments for the reason given in
    TestHookBlockingPathsFire's docstring: this repo's live hooks inspect
    unexecuted command text, so a literal protected push here blocks the
    tooling that reads this file.
    """

    PUSH = "git pu" + "sh"
    MAIN = "ma" + "in"

    # (label, command, expected). The deny rows are the filed defect; the allow
    # rows are what stops the fix from being "deny everything".
    CASES = (
        ("HEAD refspec", f"{PUSH} origin HEAD:{MAIN}", "deny"),
        ("HEAD refspec to develop", f"{PUSH} origin HEAD:develop", "deny"),
        ("branch refspec", f"{PUSH} origin mybranch:{MAIN}", "deny"),
        ("force refspec", f"{PUSH} origin +{MAIN}:{MAIN}", "deny"),
        ("--force with refspec", f"{PUSH} --force origin HEAD:{MAIN}", "deny"),
        ("--force-with-lease", f"{PUSH} --force-with-lease origin HEAD:{MAIN}",
         "deny"),
        ("fully qualified ref", f"{PUSH} origin HEAD:refs/heads/{MAIN}", "deny"),
        # git resolves `heads/main` the same as `refs/heads/main`, and a
        # qualified ref can stand alone with no colon at all — source and
        # destination are then the same branch. Both really push it.
        ("heads/ qualified destination", f"{PUSH} origin HEAD:heads/{MAIN}",
         "deny"),
        ("qualified ref, no colon", f"{PUSH} origin refs/heads/{MAIN}", "deny"),
        ("force, qualified, no colon", f"{PUSH} origin +refs/heads/{MAIN}",
         "deny"),
        # A quote is not whitespace, so a left boundary of `[\\s+]` alone let
        # the quoted spelling through while the bare one denied.
        ('qualified in double quotes', f'{PUSH} origin "refs/heads/{MAIN}"',
         "deny"),
        ("qualified in single quotes",
         f"{PUSH} origin 'refs/heads/{MAIN}'", "deny"),
        ("heads/ in double quotes", f'{PUSH} origin "heads/{MAIN}"', "deny"),
        ("plain protected push", f"{PUSH} origin {MAIN}", "deny"),
        # Ordinary branches whose names merely BEGIN with a protected name.
        # These are the rows a substring test gets wrong.
        ("refspec to a mainline branch", f"{PUSH} origin foo:{MAIN}line",
         "allow"),
        ("refspec to a maintenance branch",
         f"{PUSH} origin HEAD:{MAIN}tenance", "allow"),
        ("feature refspec", f"{PUSH} origin HEAD:feature/x", "allow"),
        ("feature branch", f"{PUSH} origin feature/probe", "allow"),
        ("tag push", f"{PUSH} origin v1.2.3", "allow"),
        # The qualified-ref arm must not fire on an ordinary qualified ref,
        # nor on a branch whose name merely begins with a protected one.
        ("qualified feature ref", f"{PUSH} origin refs/heads/feature/x",
         "allow"),
        ("qualified mainline ref", f"{PUSH} origin heads/{MAIN}line", "allow"),
        ('quoted qualified feature ref',
         f'{PUSH} origin "refs/heads/feature/x"', "allow"),
    )

    @pytest.mark.parametrize(
        "label,command,expected",
        CASES,
        ids=[c[0].replace(" ", "-") for c in CASES],
    )
    def test_refspec_decision(self, tmp_path, label, command, expected):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        got = TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command)
        assert got == expected, f"{label}: expected {expected}, got {got}"

    def test_the_refspec_arms_are_what_block_these(self, tmp_path):
        """The negative control.

        Without it, every deny row above would still pass if the gate simply
        blocked everything — and the allow rows would still pass if the
        refspec arms were deleted, because `prevent-direct-push` on a FEATURE
        branch has no other reason to look at `HEAD:main`. Removing the
        refspec condition must turn the filed rows green-to-red; this asserts
        the condition is the thing carrying them.
        """
        source = Path(".claude/hooks/prevent-direct-push.py").read_text(
            encoding="utf-8")
        assert "_PROTECTED_REFSPEC_RE.search(command)" in source, (
            "the refspec arm of targets_protected is gone; the filed defect "
            "(#327) is reachable again"
        )
        # And it must not have been re-narrowed: the bug was a SECOND, tighter
        # `if` under the first. Anything that re-tests the current branch or a
        # literal remote+ref pair after `if targets_protected:` reintroduces it.
        after = source.split("if targets_protected:", 1)[1]
        assert "current_branch in [" not in after, (
            "a second, narrower condition sits under `if targets_protected:` "
            "again — that is the exact shape of #327"
        )


class TestProtectedBranchDeletionIsBlocked:
    """#333: the `--delete` allowlist stood the gate down on a substring.

    The allowlist was two unanchored `in` tests::

        if "--delete" in command and ("release/" in command or "hotfix/" in command):

    Neither half is tied to an argument, so a protected ref riding ALONGSIDE a
    release ref was permitted: `--delete origin release/x main` deleted `main`.
    A deletion is worse than a push to the same branch — a push can be
    reverted, a deleted ref is gone.

    Falling through to `targets_protected` does not catch it either, which is
    why this needed a deny of its own rather than just a tighter allowlist:
    `"origin main"` is not a substring of `--delete origin release/x main`, the
    refspec arms need a `:` or a `heads/` spelling, and on a feature branch
    `current_branch` is clean. Measured before the fix: allow.

    The allow rows are the other half of the claim. Deleting a `release/` or
    `hotfix/` ref is the case the allowlist exists for, and deleting a
    `feature/` ref is what `git-branch-cleanup` actually runs — it never
    reached the allowlist at all, and must still be permitted.

    Command strings are assembled from fragments for the reason given in
    TestHookBlockingPathsFire's docstring: this repo's live hooks inspect
    unexecuted command text, so a literal protected push here blocks the
    tooling that reads this file.
    """

    PUSH = "git pu" + "sh"
    MAIN = "ma" + "in"

    # (label, command, expected). The deny rows are the filed defect; the allow
    # rows are what stops the fix from being "deny every deletion".
    CASES = (
        ("protected ref beside a release ref",
         f"{PUSH} --delete origin release/x {MAIN}", "deny"),
        ("protected ref beside a hotfix ref",
         f"{PUSH} --delete origin hotfix/x develop", "deny"),
        ("short flag", f"{PUSH} -d origin release/x {MAIN}", "deny"),
        # git's parse-options bundles short flags, so `-fd` is `--force
        # --delete`. A test for `-d` alone does not cover it.
        ("bundled short flag", f"{PUSH} -fd origin release/x {MAIN}", "deny"),
        ("flag after the remote",
         f"{PUSH} origin --delete release/x {MAIN}", "deny"),
        # Control: this one already denied, via the "origin main" substring.
        ("plain protected deletion", f"{PUSH} --delete origin {MAIN}", "deny"),
        # The qualified spellings of the same destination.
        ("qualified protected ref",
         f"{PUSH} --delete origin release/x refs/heads/{MAIN}", "deny"),
        ("heads/ qualified protected ref",
         f"{PUSH} --delete origin release/x heads/{MAIN}", "deny"),
        ("quoted qualified protected ref",
         f'{PUSH} --delete origin release/x "refs/heads/{MAIN}"', "deny"),
        ("protected ref before the release ref",
         f"{PUSH} --delete origin {MAIN} release/x", "deny"),
        # This row is what proves the stand-down asks about EVERY ref rather
        # than any one of them. `HEAD:main` is a ref token the deny below does
        # not recognise (it is a refspec, not a bare ref), so under an "any
        # release ref" stand-down the command is allowed outright and the
        # refspec arms never run. Under "every ref", it falls through to them
        # and `:main` denies.
        ("refspec spelling beside a release ref",
         f"{PUSH} --delete origin release/x HEAD:{MAIN}", "deny"),
        # A second push in the same command is a separate invocation and has to
        # be analysed as one.
        ("protected deletion in a later segment",
         f"{PUSH} --delete origin release/x && {PUSH} --delete origin {MAIN}",
         "deny"),
        # ---- negative controls: the fix must not be "deny every deletion" ----
        ("the intended release cleanup",
         f"{PUSH} --delete origin release/x", "allow"),
        ("the intended hotfix cleanup",
         f"{PUSH} --delete origin hotfix/x", "allow"),
        ("several release refs",
         f"{PUSH} --delete origin release/x hotfix/y", "allow"),
        ("qualified release ref",
         f"{PUSH} --delete origin refs/heads/release/x", "allow"),
        # A branch whose name merely BEGINS with a protected name is ordinary.
        ("a mainline branch beside a release ref",
         f"{PUSH} --delete origin release/x {MAIN}line", "allow"),
        ("a maintenance branch beside a release ref",
         f"{PUSH} --delete origin release/x {MAIN}tenance", "allow"),
        # What git-branch-cleanup actually runs.
        ("feature branch cleanup",
         f"{PUSH} origin --delete feature/x", "allow"),
        ("feature branch cleanup, flag first",
         f"{PUSH} --delete origin feature/x", "allow"),
        # #333 follow-up (fix 1): a backslash-newline is a line continuation,
        # i.e. whitespace, not the segment separator a bare newline is. Before
        # the fix, splitting on it dropped the protected ref into a segment
        # with no push verb, so it was never inspected and the delete was
        # allowed.
        ("protected ref after a line continuation",
         f"{PUSH} --delete origin release/x \\\n    {MAIN}", "deny"),
        # #333 follow-up (fix 2): an unbalanced quote sends `_push_invocations`
        # down the whitespace-split fallback, which does not strip quotes.
        # Before the fix the resulting token kept its leading `"`, `_bare_ref`
        # did not recognise it as protected, and the delete was allowed.
        ("protected ref behind an unbalanced quote",
         f'{PUSH} --delete origin release/x "{MAIN}', "deny"),
        # Negative control for fix 2: a normally single-quoted release ref
        # must still stand down — the quote strip must not turn into "deny
        # every quoted token".
        ("singly-quoted release ref still stands down",
         f"{PUSH} --delete origin 'release/x'", "allow"),
        # `_push_invocations` skips `shlex` for an argument string past
        # `_SHLEX_MAX` (it is quadratic in a single token's length). That
        # shortcut must not become a way to hide a ref: the whitespace split
        # keeps the quotes on, and `_bare_ref` is what takes them off again.
        ("quoted protected ref past the shlex bound",
         f"{PUSH} --delete origin release/x " + "b" * 100_001 + f' "{MAIN}"',
         "deny"),
        # The same shape under the bound, which DOES go through shlex — so the
        # row above is testing the shortcut rather than just the ref matcher.
        ("quoted protected ref under the shlex bound",
         f"{PUSH} --delete origin release/x " + "b" * 10 + f' "{MAIN}"',
         "deny"),
        # A flag whose value is a SEPARATE token consumes that token, so it is
        # neither the remote nor a ref. Skipping it must not open a hole: the
        # protected ref after it still denies.
        ("value flag before a mixed deletion",
         f"{PUSH} -o ci.skip --delete origin release/x {MAIN}", "deny"),
        ("long value flag before a mixed deletion",
         f"{PUSH} --push-option ci.skip --delete origin release/x {MAIN}",
         "deny"),
        # The `=`-joined spelling is one token starting with `-`, so it is
        # already a flag and consumes nothing.
        ("=-joined value flag before a mixed deletion",
         f"{PUSH} --push-option=ci.skip --delete origin release/x {MAIN}",
         "deny"),
        # Verified against real git: `--repo` consumes `origin`, which leaves
        # the single positional as the REPOSITORY and no refspec at all, so
        # git refuses with "--delete doesn't make sense without any refs" and
        # the remote is unchanged. Nothing is deleted, so allow is the verdict
        # that matches git. This row is also the control proving the skip is
        # real: without it, `origin` would be the remote and `main` a ref.
        ("value flag leaves no refspec at all",
         f"{PUSH} --repo origin --delete {MAIN}", "allow"),
        # A `-`-prefixed token is never eaten as a value, so an over-broad
        # entry cannot swallow a real option and hide the ref behind it.
        ("value flag followed by another flag",
         f"{PUSH} -o --force origin --delete {MAIN}", "deny"),
    )

    @pytest.mark.parametrize(
        "label,command,expected",
        CASES,
        ids=[c[0].replace(" ", "-") for c in CASES],
    )
    def test_delete_decision(self, tmp_path, label, command, expected):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        got = TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command)
        assert got == expected, f"{label}: expected {expected}, got {got}"

    # The stand-down is only OBSERVABLE from a protected branch. On a feature
    # branch, "stood down" and "fell through" both end in allow — the hook has
    # no other reason to refuse — so every row above would pass against a
    # stand-down that never fires. From `develop` the two outcomes separate:
    # standing down exits 0 immediately, and anything that falls through hits
    # `current_branch in ["main", "develop"]` and denies. These rows are what
    # keep the allowlist from being quietly deleted.
    DEVELOP_CASES = (
        # The case the allowlist exists for.
        ("release cleanup from develop",
         f"{PUSH} --delete origin release/x", "allow"),
        ("hotfix cleanup from develop",
         f"{PUSH} --delete origin hotfix/x", "allow"),
        # Needs the qualified spellings to be stripped before the release/
        # prefix is tested.
        ("qualified release ref from develop",
         f"{PUSH} --delete origin refs/heads/release/x", "allow"),
        ("heads/ qualified release ref from develop",
         f"{PUSH} --delete origin heads/release/x", "allow"),
        # Needs the quotes stripped: a whitespace split leaves `"release/x"`,
        # which does not start with `release/`.
        ("quoted release ref from develop",
         f'{PUSH} --delete origin "release/x"', "allow"),
        # Needs each push to be analysed on its own arguments: read as one
        # string, the second invocation's `git`, `push` and `origin` become
        # ref tokens of the first and none of them is a release ref.
        ("two release cleanups in one command",
         f"{PUSH} --delete origin release/x && {PUSH} --delete origin hotfix/y",
         "allow"),
        # The false deny this fix removes. `-o` takes a separate value, so
        # before the fix `ci.skip` was read as the remote and `origin` as a
        # ref; `origin` is not a release ref, the stand-down was withheld, and
        # a legitimate release cleanup run from develop was DENIED.
        ("release cleanup behind a value flag",
         f"{PUSH} -o ci.skip --delete origin release/x", "allow"),
        ("release cleanup behind a long value flag",
         f"{PUSH} --push-option ci.skip --delete origin release/x", "allow"),
        # ---- controls: develop must not become an allow-everything branch --
        ("a protected push from develop", f"{PUSH} origin develop", "deny"),
        ("a protected deletion from develop",
         f"{PUSH} --delete origin release/x {MAIN}", "deny"),
        # Pre-existing behaviour, unchanged by #333 and pinned so a later
        # change has to be deliberate: the allowlist only ever covered
        # release/hotfix refs, so deleting a feature branch while standing on
        # develop was refused before this change and still is.
        ("feature cleanup from develop is still refused",
         f"{PUSH} --delete origin feature/x", "deny"),
    )

    @pytest.mark.parametrize(
        "label,command,expected",
        DEVELOP_CASES,
        ids=[c[0].replace(" ", "-") for c in DEVELOP_CASES],
    )
    def test_delete_decision_from_develop(self, tmp_path, label, command,
                                          expected):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b",
                        "develop"], env=env, check=True, capture_output=True)
        got = TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command)
        assert got == expected, f"{label}: expected {expected}, got {got}"

    def test_a_large_command_does_not_stall_the_gate(self, tmp_path):
        """`shlex` is quadratic in the length of a single argument.

        This hook runs before EVERY Bash tool call, and the payload cap
        upstream is 1 MB. Handing `shlex` a ~900 KB argument took 5.6s, against
        0.03s for the same command on `develop` — measured, a ~185x regression
        introduced by the delete gate itself. `_push_invocations` now skips
        `shlex` past `_SHLEX_MAX`, which brought it back to 0.04s.

        The bound is generous on purpose: it sits far below the 5.6s the
        unfixed path took, and far above the ~0.04s the fixed one does, so it
        catches the regression without going flaky on a loaded CI runner. The
        verdict is asserted alongside the timing — a fast ALLOW would be the
        worst possible way to pass this test.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = (f"{self.PUSH} --delete origin release/x "
                   + "b" * 900_000 + f" {self.MAIN}")
        started = time.monotonic()
        got = TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command)
        elapsed = time.monotonic() - started
        assert got == "deny", f"a 900 KB command must still deny, got {got}"
        assert elapsed < 3.0, (
            f"the gate took {elapsed:.2f}s on a 900 KB command; the quadratic "
            f"shlex path is back (it measured 5.6s, the fixed path 0.04s)"
        )

    def test_the_delete_analysis_is_what_carries_these(self, tmp_path):
        """The negative control on the guard itself.

        Without it the deny rows would still pass against a hook that blocked
        everything, and the allow rows would still pass against the ORIGINAL
        substring allowlist for every row that does not mix a protected ref
        with a release one. This asserts the substring form is gone and the
        ref-token analysis is the thing deciding.
        """
        source = Path(".claude/hooks/prevent-direct-push.py").read_text(
            encoding="utf-8")
        assert '"--delete" in command and' not in source, (
            "the unanchored substring allowlist is back; #333 is reachable "
            "again"
        )
        assert "_protected_delete_refs(command)" in source, (
            "the ref-token delete analysis is gone; nothing else in this hook "
            "denies `--delete origin release/x main`"
        )
