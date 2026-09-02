"""Security hardening tests for obsidian-brain."""
import ast
import json
import os
import re
import shlex
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


class TestVerbFormsCannotSkipTheGate:
    """`git`/`gh` accept GLOBAL options between the executable and the
    subcommand: `git -C . push`, `git -c k=v push`, `git --no-pager push`,
    `gh --repo o/r pr create`, `gh -R o/r pr merge`. Every gate's entry test
    and scope guard used to be a literal substring check (`"git push" in
    command`) or a bare verb regex (`\\bgit\\s+push\\b`), so none of those
    forms matched — the gate exited 0 with an empty stdout, an ALLOW under
    the PreToolUse contract, with every check below it skipped (#351, #327
    item 2).

    Every row below is `allow` on develop before this fix, except the
    plain-form controls (no global options at all), which already deny and
    must keep denying, and the "no verb"/out-of-scope negative controls,
    which must stay `allow` so the fix is not "deny every git/gh invocation".

    Command strings are assembled from fragments on purpose: this repo's live
    PreToolUse hooks inspect unexecuted command text, so a literal
    protected-branch push string in this file blocks the tooling that reads
    it.
    """

    # (hook, command, expected).
    CASES = (
        # --- prevent-direct-push: git push ---
        ("prevent-direct-push", "git pu" + "sh origin ma" + "in", "deny"),
        ("prevent-direct-push", "git -C . pu" + "sh origin ma" + "in", "deny"),
        ("prevent-direct-push", "git -c user.name=x pu" + "sh origin ma" + "in", "deny"),
        ("prevent-direct-push", "git --no-pager pu" + "sh origin ma" + "in", "deny"),
        ("prevent-direct-push", "git --no-pager -C . pu" + "sh origin ma" + "in", "deny"),
        ("prevent-direct-push", "git -C . pu" + "sh origin feature/probe", "allow"),
        ("prevent-direct-push", "git -C /nonexistent-elsewhere pu" + "sh origin ma" + "in", "allow"),
        ("prevent-direct-push", "git config --global alias.p pu" + "sh", "allow"),
        ("prevent-direct-push", "git -C . status", "allow"),
        # CRIT-1: a value quoted mid-token (`-c k="v w"`), not fully quoted.
        ("prevent-direct-push",
         'git -c user.name="A B" pu' + "sh origin ma" + "in", "deny"),
        ("prevent-direct-push",
         "git -c user.name='A B' pu" + "sh origin ma" + "in", "deny"),
        # CRIT-2: a line continuation between the executable and the verb.
        ("prevent-direct-push", "git \\\n  pu" + "sh origin ma" + "in", "deny"),
        ("prevent-direct-push", "git -C \\\n  . pu" + "sh origin ma" + "in", "deny"),
        # IMP-1: `_push_invocations` (the #333 deletion gate) with global options.
        ("prevent-direct-push",
         "git -C . pu" + "sh origin --delete ma" + "in", "deny"),
        ("prevent-direct-push",
         "git --no-pager pu" + "sh origin --delete ma" + "in", "deny"),
        ("prevent-direct-push",
         "git -c k=v pu" + "sh origin --delete ma" + "in", "deny"),
        ("prevent-direct-push",
         "git -C . pu" + "sh origin --delete release/1.0.0 ma" + "in", "deny"),
        ("prevent-direct-push",
         "git -C . pu" + "sh origin --delete release/1.0.0", "allow"),
        # NEW-1: an UNBALANCED quote inside a global option (a real git
        # idiom, `-c user.name=O'Brien`) must degrade to matching as an
        # ordinary character, not fail the whole match.
        ("prevent-direct-push",
         "git -c user.name=O'Brien pu" + "sh origin ma" + "in", "deny"),
        ("prevent-direct-push",
         'git -c a.b="cd pu' + "sh origin ma" + "in", "deny"),
        ("prevent-direct-push",
         "git -c a.b=O'B pu" + "sh origin --delete ma" + "in", "deny"),
        # NEW-2: `-C ""` / `-C ''` (documented git behaviour: cwd unchanged)
        # must not crash the hook -- an empty/unresolved -C target is
        # ambiguous, not provably out of scope, so it falls through gated.
        ("prevent-direct-push",
         'git -C "" pu' + "sh origin ma" + "in", "deny"),
        ("prevent-direct-push",
         "git -C '' pu" + "sh origin ma" + "in", "deny"),
        # CRIT-3: a run of unbalanced quotes must still resolve to a verdict
        # (see TestDecisionTimeIsBounded for the actual timing assertion);
        # here we only check it still denies, i.e. still matches the verb.
        ("prevent-direct-push",
         'git -a ' + '"' * 40 + ' pu' + "sh origin ma" + "in", "deny"),
        # CRIT-4: a decoy `-C`/`cd` sitting inside a QUOTED VALUE of an
        # unrelated option must not descope the command -- git reads the
        # whole quoted string back as ONE config value, not a real `-C`.
        ("prevent-direct-push",
         'git -c a.b="x -C /nonexistent-elsewhere" pu' + "sh origin ma" + "in",
         "deny"),
        ("prevent-direct-push",
         'git -c a.b="x -C /nonexistent-elsewhere" pu'
         + "sh origin --delete ma" + "in", "deny"),
        ("prevent-direct-push",
         'git -c a.b="x && cd /nonexistent-elsewhere" pu'
         + "sh origin ma" + "in", "deny"),
        # CRIT-5: a quoted `;` (or `&`/`|`) inside a global-option value
        # defeats a text-blind segment split -- see `_push_invocations`.
        ("prevent-direct-push",
         'git -c a.b="c;d" pu' + "sh origin --delete ma" + "in", "deny"),
        # CRIT-6: two more global-option spellings that reach real git.
        ("prevent-direct-push",
         "git -c user.name=A\\ B pu" + "sh origin ma" + "in", "deny"),
        ("prevent-direct-push",
         'git "-c" k=v pu' + "sh origin ma" + "in", "deny"),
        ("prevent-direct-push",
         "git '-c' 'k=v w' pu" + "sh origin ma" + "in", "deny"),
        ("prevent-direct-push",
         "git -c a.b=c=d\\ e pu" + "sh origin ma" + "in", "deny"),

        # --- validate-branch-name: git checkout -b ---
        ("validate-branch-name", "git chec" + "kout -b nonsense-branch", "deny"),
        ("validate-branch-name", "git -C . chec" + "kout -b nonsense-branch", "deny"),
        ("validate-branch-name", "git -c user.name=x chec" + "kout -b nonsense-branch", "deny"),
        ("validate-branch-name", "git --no-pager chec" + "kout -b nonsense-branch", "deny"),
        ("validate-branch-name", "git --no-pager -C . chec" + "kout -b nonsense-branch", "deny"),
        ("validate-branch-name", "git -C . chec" + "kout -b feature/ok", "allow"),
        ("validate-branch-name", "git -C /nonexistent-elsewhere chec" + "kout -b nonsense-branch", "allow"),
        ("validate-branch-name", "git config --global alias.c chec" + "kout", "allow"),
        ("validate-branch-name", "git -C . status", "allow"),
        # CRIT-1
        ("validate-branch-name",
         'git -c user.name="A B" chec' + "kout -b nonsense-branch", "deny"),
        # CRIT-2
        ("validate-branch-name",
         "git \\\n  chec" + "kout -b nonsense-branch", "deny"),
        # NEW-1
        ("validate-branch-name",
         "git -c user.name=O'Brien chec" + "kout -b nonsense-branch", "deny"),
        # NEW-2
        ("validate-branch-name",
         'git -C "" chec' + "kout -b nonsense-branch", "deny"),
        ("validate-branch-name",
         "git -C '' chec" + "kout -b nonsense-branch", "deny"),
        # CRIT-4
        ("validate-branch-name",
         'git -c a.b="x -C /nonexistent-elsewhere" chec'
         + "kout -b nonsense-branch", "deny"),
        # CRIT-6
        ("validate-branch-name",
         "git -c user.name=A\\ B chec" + "kout -b nonsense-branch", "deny"),
        ("validate-branch-name",
         'git "-c" k=v chec' + "kout -b nonsense-branch", "deny"),

        # --- require-preflight: git commit ---
        ("require-preflight", "git com" + "mit -m wip", "deny"),
        ("require-preflight", "git -C . com" + "mit -m wip", "deny"),
        ("require-preflight", "git -c user.name=x com" + "mit -m wip", "deny"),
        ("require-preflight", "git --no-pager com" + "mit -m wip", "deny"),
        ("require-preflight", "git --no-pager -C . com" + "mit -m wip", "deny"),
        ("require-preflight", "git -C /nonexistent-elsewhere com" + "mit -m wip", "allow"),
        ("require-preflight", "git config --global alias.c com" + "mit", "allow"),
        ("require-preflight", "git -C . status", "allow"),
        # CRIT-1
        ("require-preflight",
         'git -c user.name="A B" com' + "mit -m wip", "deny"),
        # CRIT-2
        ("require-preflight", "git \\\n  com" + "mit -m wip", "deny"),
        # NEW-1
        ("require-preflight",
         "git -c user.name=O'Brien com" + "mit -m wip", "deny"),
        # NEW-2
        ("require-preflight", 'git -C "" com' + "mit -m wip", "deny"),
        ("require-preflight", "git -C '' com" + "mit -m wip", "deny"),
        # CRIT-4
        ("require-preflight",
         'git -c a.b="x -C /nonexistent-elsewhere" com' + "mit -m wip", "deny"),
        # CRIT-6
        ("require-preflight",
         "git -c user.name=A\\ B com" + "mit -m wip", "deny"),
        ("require-preflight", "git '-c' 'k=v w' com" + "mit -m wip", "deny"),

        # --- enforce-pr-base-branch: gh pr create ---
        ("enforce-pr-base-branch", "gh pr cre" + "ate --base ma" + "in", "deny"),
        ("enforce-pr-base-branch", "gh --repo o/r pr cre" + "ate --base ma" + "in", "deny"),
        ("enforce-pr-base-branch", "gh -R o/r pr cre" + "ate --base ma" + "in", "deny"),
        ("enforce-pr-base-branch", "gh --repo o/r pr cre" + "ate --base develop", "allow"),
        ("enforce-pr-base-branch", "gh config get git_protocol", "allow"),
        # CRIT-1
        ("enforce-pr-base-branch",
         'gh -c foo="a b" pr cre' + "ate --base ma" + "in", "deny"),
        # CRIT-2
        ("enforce-pr-base-branch", "gh \\\n  pr cre" + "ate --base ma" + "in", "deny"),
        # NEW-1
        ("enforce-pr-base-branch",
         "gh -c foo=O'Brien pr cre" + "ate --base ma" + "in", "deny"),
        # CRIT-6
        ("enforce-pr-base-branch",
         'gh "-c" foo=x pr cre' + "ate --base ma" + "in", "deny"),

        # --- enforce-pr-base-branch: gh pr merge ---
        ("enforce-pr-base-branch", "gh pr mer" + "ge 5", "deny"),
        ("enforce-pr-base-branch", "gh --repo o/r pr mer" + "ge 5", "deny"),
        ("enforce-pr-base-branch", "gh -R o/r pr mer" + "ge 5", "deny"),
        # CRIT-1
        ("enforce-pr-base-branch", 'gh -c foo="a b" pr mer' + "ge 5", "deny"),
        # CRIT-2
        ("enforce-pr-base-branch", "gh \\\n  pr mer" + "ge 5", "deny"),
        # NEW-1
        ("enforce-pr-base-branch", "gh -c foo=O'Brien pr mer" + "ge 5", "deny"),

        # --- update-changelog-before-pr: gh pr create ---
        ("update-changelog-before-pr", "gh pr cre" + "ate --base develop", "deny"),
        ("update-changelog-before-pr", "gh --repo o/r pr cre" + "ate --base develop", "deny"),
        ("update-changelog-before-pr", "gh -R o/r pr cre" + "ate --base develop", "deny"),
        # CRIT-1
        ("update-changelog-before-pr",
         'gh -c foo="a b" pr cre' + "ate --base develop", "deny"),
        # CRIT-2
        ("update-changelog-before-pr",
         "gh \\\n  pr cre" + "ate --base develop", "deny"),
        # MIN-1: this gate had no `allow` negative control at all.
        ("update-changelog-before-pr", "gh --repo o/r pr view 3", "allow"),
        ("update-changelog-before-pr",
         "gh pr list --search 'pr cre" + "ate'", "allow"),
        # NEW-1
        ("update-changelog-before-pr",
         "gh -c foo=O'Brien pr cre" + "ate --base develop", "deny"),
        # CRIT-6
        ("update-changelog-before-pr",
         "gh -c foo=x\\ y pr cre" + "ate --base develop", "deny"),
    )

    @pytest.mark.parametrize("hook,command,expected", CASES)
    def test_global_option_forms(self, tmp_path, hook, command, expected):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        assert TestHookBlockingPathsFire._decide(work, env, hook, command) == expected, (
            f"{hook} got {command!r} wrong"
        )

    @pytest.mark.parametrize("hook,verb", (
        ("prevent-direct-push", "pu" + "sh origin ma" + "in"),
        ("validate-branch-name", "chec" + "kout -b nonsense-branch"),
        ("require-preflight", "com" + "mit -m wip"),
    ))
    def test_quoted_path_with_a_space_inside_the_project_still_denies(
        self, tmp_path, hook, verb
    ):
        """`-C "<path with a space>"` pointing INSIDE the project must still
        deny. Built from the fixture's own tmp project dir at runtime — never
        a hardcoded literal path — because `_targets_this_project` compares
        the resolved `-C` target against `CLAUDE_PROJECT_DIR`, which the
        harness sets to that tmp repo.

        The quoted alternative in `_GLOBAL_OPTS`'s value group exists so a
        space in the path does not end the option mid-argument and strand the
        verb match — an unquoted `-C /a b push ...` would read `push` as part
        of the `-C` value and the verb pattern would stop matching, the same
        fail-open this whole pattern exists to close.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = f'git -C "{work}/a b" ' + verb
        assert TestHookBlockingPathsFire._decide(work, env, hook, command) == "deny"


class TestGlobalOptionSpellingsMatchThePlainVerdict:
    """The durable regression net for the whole `_GLOBAL_OPTS`/`_Q` class
    (#351), not just the specific cases CRIT-1/CRIT-2/NEW-1/NEW-2/CRIT-3/
    CRIT-4/CRIT-5/CRIT-6 happened to probe. Every one of rounds 1-4 found a
    fail-open or a blowup this file's own hand-picked example list had not
    thought of yet — the pattern of "case N+1 next round" is exactly what a
    hand-picked list cannot close, which is IMP-3's finding: the round-3
    decoy rows were themselves hand-picked, and three of the four turned out
    to be structurally vacuous (see `test_prefix_decoy_matches_plain_verdict`
    below for why, and `TestGeneratedGlobalOptionValuesMatchThePlainVerdict`
    for the mechanically-generated matrix that replaces "pick more examples"
    as the primary net).

    Instead of asserting a hardcoded `allow`/`deny` per spelling, this
    generates a bounded matrix of global-option spellings and asserts each
    one's verdict equals the PLAIN form's verdict (no global options at all)
    for the same verb. The plain form is the control; a spelling that moves
    the verdict away from it is a bug by construction, in EITHER direction —
    a new fail-open, or a spelling that over-matches into a false deny.

    Kept to a small, deliberately chosen set rather than a fuzzer, so it
    stays in the seconds range: separate and `=`-joined values; bare,
    single-quoted, double-quoted, half-quoted, unbalanced-quoted and
    empty-quoted values; a value containing a space, a `-`, or an `=`; a
    single option and two stacked; a tab separator; a `\\<newline>`
    continuation right after the global options; and — CRIT-4's class as a
    property, not an example — a `-C` decoy (both quote styles) sitting
    inside a quoted value of an unrelated option.
    """

    # Text to splice between the executable and the verb, e.g.
    # "git " + SPELLING + "push origin main". Every one of these must be a
    # NO-OP on the verdict relative to the plain form with no global options.
    OPTION_SPELLINGS = (
        "-C . ",
        "-c user.name=x ",
        '-c user.name="A B" ',
        "-c user.name='A B' ",
        "-c user.name=O'Brien ",          # unbalanced quote (NEW-1)
        '-c a.b="only-opens ',             # unbalanced quote, opens & never closes
        "--no-pager ",
        '-C "" ',                          # empty double-quoted value (NEW-2)
        "-C '' ",                          # empty single-quoted value (NEW-2)
        "-c k=v=w ",                       # value containing '='
        "-c k=a-b ",                       # value containing '-'
        "--repo=o/r ",                     # '='-joined long option
        "-C .\t",                          # tab as the trailing separator
        "-C\t. ",                          # tab between flag and value
        "-C . -c user.name=x ",            # two stacked options
        "-c user.name=x -C . ",            # two stacked options, reversed
        "-C . \\\n",                       # line continuation after the options
        # CRIT-6
        r"-c user.name=A\ B ",              # backslash-escaped space
        '"-c" k=v ',                        # the OPTION TOKEN itself quoted
        "'-c' 'k=v w' ",                    # option AND value both quoted
        # CRIT-4: a `-C` decoy sitting inside a QUOTED VALUE of an unrelated
        # option. Real git reads each of these back as ONE config value, not
        # a second `-C`. IMP-3: this is the only decoy shape that is a real
        # guard here (proved by MUT-A in the round-3 re-review) -- a decoy
        # `cd`/verb spliced at this SAME position (between the executable
        # and the verb) can never precede the verb match's own start, so
        # `_targets_this_project`'s `preceding = [... c[0] < position]`
        # filter can never even see it; that is what
        # `test_prefix_decoy_matches_plain_verdict` below exists to cover
        # instead. Both quote styles are tested since only the double-quoted
        # one used to be live.
        '-c a.b="x -C /nonexistent-elsewhere" ',
        "-c a.b='x -C /nonexistent-elsewhere' ",
    )

    # hook -> (executable prefix, bare verb text with its own arguments).
    HOOK_VERBS = {
        "prevent-direct-push": ("git ", "pu" + "sh origin ma" + "in"),
        "validate-branch-name": ("git ", "chec" + "kout -b nonsense-branch"),
        "require-preflight": ("git ", "com" + "mit -m wip"),
        "enforce-pr-base-branch": ("gh ", "pr cre" + "ate --base ma" + "in"),
        "update-changelog-before-pr": ("gh ", "pr cre" + "ate --base develop"),
    }

    @pytest.mark.parametrize("spelling", OPTION_SPELLINGS)
    @pytest.mark.parametrize("hook", sorted(HOOK_VERBS))
    def test_spelling_matches_plain_verdict(self, tmp_path, hook, spelling):
        exe, verb = self.HOOK_VERBS[hook]
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        plain = exe + verb
        control = TestHookBlockingPathsFire._decide(work, env, hook, plain)
        candidate = exe + spelling + verb
        got = TestHookBlockingPathsFire._decide(work, env, hook, candidate)
        assert got == control, (
            f"{hook}: {candidate!r} verdict {got!r} != plain-form {plain!r} "
            f"verdict {control!r}"
        )

    # IMP-3: a decoy spliced BETWEEN the executable and the verb (the
    # OPTION_SPELLINGS axis above) sits at an offset >= the verb match's own
    # start (`position` in `_targets_this_project`), and `preceding = [c for
    # c in cd_matches if c[0] < position]` can never see anything at or after
    # `position` -- so a decoy `cd` there can NEVER exercise the
    # `_shell_scan` vouching on `cd`-detection, no matter how the decoy is
    # spelled. The round-3 re-review proved this by mutation (MUT-B: drop
    # `_shell_scan` from `cd`-detection, re-breaking #326 -- 105 passed,
    # nothing red). A decoy only threatens that code path when it sits
    # BEFORE the verb, which is what this axis does.
    PREFIX_DECOYS = (
        'echo "x && cd /nonexistent-elsewhere" && ',
        "echo 'x && cd /nonexistent-elsewhere' && ",
        'echo "x ; cd /tmp" && ',
        'echo "x -C /nonexistent-elsewhere" && ',
    )

    @pytest.mark.parametrize("prefix", PREFIX_DECOYS)
    @pytest.mark.parametrize("hook", sorted(HOOK_VERBS))
    def test_prefix_decoy_matches_plain_verdict(self, tmp_path, hook, prefix):
        exe, verb = self.HOOK_VERBS[hook]
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        plain = exe + verb
        control = TestHookBlockingPathsFire._decide(work, env, hook, plain)
        candidate = prefix + exe + verb
        got = TestHookBlockingPathsFire._decide(work, env, hook, candidate)
        assert got == control, (
            f"{hook}: {candidate!r} verdict {got!r} != plain-form {plain!r} "
            f"verdict {control!r}"
        )


def _double_quote_escape(value: str) -> str:
    """`value` wrapped in double quotes, backslash-escaping `\\` and `"`."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _backslash_escape(value: str) -> str:
    """`value` with every non-alphanumeric/`-_./` character backslash-escaped.

    Over-escaping an already-safe character (`\\a` for `a`) is harmless in
    POSIX shells -- a backslash before a character with no special meaning
    is just that character -- so this does not need to be precise about
    which characters truly need escaping.
    """
    safe = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./"
    )
    return "".join(("\\" + c) if c not in safe else c for c in value)


def _option_token_quotings(token):
    """Every quoting of `token` a POSIX shell reads back as exactly `token`.

    Derived from the token by rule -- wrap it, wrap its tail, wrap its
    first character, escape every character -- rather than typed out one
    spelling at a time, which is the same discipline `_double_quote_escape`
    and `_backslash_escape` apply to option VALUES.

    The axis exists because varying the value alone turned out to be blind
    to the half of #351 CRIT-6 that round 4 then broke. Reverting CRIT-6's
    escaped-space half turned 5 generated rows red; reverting its
    quoted-option-token half left all 105 green, because every generated
    spelling was `-c a.b=<styled>` -- the value varied, the option token
    was always a bare `-c`.
    """
    return {
        "bare": token,
        "fully-double-quoted": '"' + token + '"',
        "fully-single-quoted": "'" + token + "'",
        "tail-double-quoted": token[0] + '"' + token[1:] + '"',
        "tail-single-quoted": token[0] + "'" + token[1:] + "'",
        "leading-dash-double-quoted": '"' + token[0] + '"' + token[1:],
        "leading-dash-single-quoted": "'" + token[0] + "'" + token[1:],
        "every-character-escaped": "".join("\\" + c for c in token),
    }


_OPTION_TOKEN_QUOTINGS = _option_token_quotings("-c")

# Spellings `_GLOBAL_OPTS` does not match today, so the gate stands down on
# a command git accepts. Measured `allow` where the plain form is `deny`,
# on all five hooks, at `74cf1b1` (before this branch), at `b428ff3` and
# here -- pre-existing, unchanged by #351, NOT a regression.
#
# They are listed as strict xfails rather than left out: an omitted row is
# a gap nobody can see, and `strict=True` means the day one of them starts
# passing this test FAILS until the entry is deleted, so the ledger cannot
# rot. The obvious fix -- letting `_OPT` start with a quote followed by the
# dash -- was measured during round 5 at 221ms for 2000 tokens and rising
# quadratically, i.e. it trades a fail-open for a slow-loris on every Bash
# call, so it was not taken. See `TestPatternDecisionTimeIsBounded`.
_OPTION_TOKEN_KNOWN_GAPS = frozenset({
    "leading-dash-double-quoted",
    "leading-dash-single-quoted",
    "every-character-escaped",
})

# Two values, so the axis is not silently coupled to one value shape.
_OPTION_TOKEN_VALUES = {
    "bare": "a.b=v",
    "shlex-quoted-space": "a.b=" + shlex.quote("x y"),
}

_OPTION_TOKEN_PARAMS = [
    pytest.param(
        quoting, value_name,
        marks=(
            [pytest.mark.xfail(
                strict=True,
                reason=f"known pre-existing gap: `_GLOBAL_OPTS` does not "
                       f"match a {quoting} option token (same at 74cf1b1)",
            )]
            if quoting in _OPTION_TOKEN_KNOWN_GAPS else []
        ),
        id=f"{quoting}-{value_name}",
    )
    for quoting in sorted(_OPTION_TOKEN_QUOTINGS)
    for value_name in sorted(_OPTION_TOKEN_VALUES)
]


class TestGeneratedGlobalOptionValuesMatchThePlainVerdict:
    """IMP-3's "stop hand-picking spellings" fix. Rounds 1-4 each found a
    fail-open the hand-picked `OPTION_SPELLINGS` list above had not thought
    of, because the same mind chose its rows every time (CRIT-1's
    half-quoted value, NEW-1's unbalanced quote, CRIT-6's backslash-escaped
    space and quoted option token -- four different escaping ideas across
    four rounds). This class generates spellings mechanically instead: a
    small set of AWKWARD VALUES crossed with a small set of QUOTING STYLES,
    each built with `shlex.quote` or an explicit escaping function rather
    than typed out by hand, so the matrix does not depend on anyone
    thinking of the next awkward case.

    7 values x 3 styles = 21 combinations, x 5 hooks = 105 rows. Runs in the
    same few seconds per row as the rest of this file's subprocess-based
    tests -- bounded and fast, not a fuzzer.
    """

    VALUES = {
        "space": "a b",
        "single-quote": "a'b",
        "double-quote": 'a"b',
        "semicolon": "a;b",
        "equals": "a=b",
        "backslash": "a\\b",
        "empty": "",
    }

    # Each style takes a raw value and returns a shell-embeddable spelling
    # of it. `shlex` is Python's canonical POSIX-quoting implementation --
    # using it (rather than hand-typing more quoted examples) is the point:
    # it models the same tokenisation the shell does.
    STYLES = {
        "shlex-quote": shlex.quote,
        "double-quoted": _double_quote_escape,
        "backslash-escaped": _backslash_escape,
    }

    HOOK_VERBS = TestGlobalOptionSpellingsMatchThePlainVerdict.HOOK_VERBS

    @pytest.mark.parametrize("value_name", sorted(VALUES))
    @pytest.mark.parametrize("style_name", sorted(STYLES))
    @pytest.mark.parametrize("hook", sorted(HOOK_VERBS))
    def test_generated_value_matches_plain_verdict(
        self, tmp_path, hook, style_name, value_name
    ):
        exe, verb = self.HOOK_VERBS[hook]
        styled = self.STYLES[style_name](self.VALUES[value_name])
        spelling = f"-c a.b={styled} "
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        plain = exe + verb
        control = TestHookBlockingPathsFire._decide(work, env, hook, plain)
        candidate = exe + spelling + verb
        got = TestHookBlockingPathsFire._decide(work, env, hook, candidate)
        assert got == control, (
            f"{hook}: {candidate!r} ({style_name}/{value_name}) verdict "
            f"{got!r} != plain-form {plain!r} verdict {control!r}"
        )

    @pytest.mark.parametrize("quoting,value_name", _OPTION_TOKEN_PARAMS)
    @pytest.mark.parametrize("hook", sorted(HOOK_VERBS))
    def test_generated_option_token_matches_plain_verdict(
        self, tmp_path, hook, quoting, value_name
    ):
        """The same property over the OPTION TOKEN rather than its value.

        `-c`, `"-c"`, `'-c'`, `-"c"`, `-'c'` are one option to any POSIX
        shell, so all five must reach the same verdict as the plain form.
        Three further quotings are strict xfails -- see
        `_OPTION_TOKEN_KNOWN_GAPS`.
        """
        exe, verb = self.HOOK_VERBS[hook]
        token = _OPTION_TOKEN_QUOTINGS[quoting]
        spelling = f"{token} {_OPTION_TOKEN_VALUES[value_name]} "
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        plain = exe + verb
        control = TestHookBlockingPathsFire._decide(work, env, hook, plain)
        candidate = exe + spelling + verb
        got = TestHookBlockingPathsFire._decide(work, env, hook, candidate)
        assert got == control, (
            f"{hook}: {candidate!r} ({quoting}/{value_name}) verdict "
            f"{got!r} != plain-form {plain!r} verdict {control!r}"
        )


# ---------------------------------------------------------------------------
# A GENERATED end-to-end timing matrix, for the reason round 4 of Task 1
# generated the spelling matrix instead of listing it: three separate
# superlinear regressions in this file were each missed by a hand-picked
# list of remembered worst cases, and the third was a QUADRATIC one that the
# list walked straight past. `ADVERSARIAL_INPUTS` below holds inputs like
# `"x" * 900_000` -- 900 KB with no quote and no separator in it, which is
# exactly the shape that CANNOT exercise the span/separator interaction, and
# it measured a comfortable 72 ms while a same-sized command holding both
# took 37.9 SECONDS.
#
# So the axes are enumerated instead of the examples: separator density x
# quote density x total length. A cell is a repeating unit at a size; the
# units cover both ends of both axes and the combinations between them.
_MATRIX_UNITS = {
    # Both axes high at once -- the shape that was quadratic. Every unit
    # contributes one closed quoted span AND two separators, so the span
    # count and the separator count both grow with the input.
    "quoted separators": ("PUSH origin feature/x && ", 'echo "a;b" && '),
    "single-quoted separators": ("PUSH origin feature/x && ", "echo 'a;b' && "),
    # Same, but the separator is a newline -- the path where `_quoted_spans`
    # DROPS a span rather than vouching for it.
    "newline separators": ("PUSH origin feature/x\n", 'echo "a;b"\n'),
    # Separators only.
    "bare separators": ("PUSH origin feature/x && ", "echo a && "),
    "separator run": ("PUSH origin feature/x", ";"),
    # Quotes only, closed and unclosed.
    "quoted words": ("PUSH origin ", '"ab" '),
    "balanced quote run": ("PUSH origin ", '"" '),
    "unbalanced quote run": ("PUSH origin feature/x ", '"'),
    # Neither -- the control, and the shape the old hand-picked list had.
    "no structure": ("PUSH origin ", "x"),
}

# 4 KB is an ordinary long command; 64 KB is where a quadratic term first
# becomes unmistakable; 900 KB sits just under the hooks' 1 MB stdin cap.
_MATRIX_SIZES_KB = (4, 64, 900)

# Sizes for the GROWTH assertion below. Doubling twice, chosen so the WORK
# dominates the noise rather than merely exceeding the fixed ~42 ms of
# interpreter start-up and git calls.
#
# `work = elapsed - baseline` is a difference of two measured numbers, so its
# relative error is worst when the work is small -- and that is a flake, not
# a finding. Measured under 12 CPU burners on 12 cores (2x oversubscription,
# harsher than any CI runner): at 32/64/128 KB the assertion failed on five
# of six runs with ratios of 3.05-5.24 across several shapes, because work
# down at 3-15 ms is the same size as the jitter. At 128/256/512 KB the work
# is 6-111 ms and the worst ratio over three runs was 2.13-2.33 -- BETTER
# than the 2.50 measured idle at the smaller sizes, because the signal grew
# faster than the noise.
#
# So the fix for a noisy ratio is more signal, not a bigger floor: raising
# `_GROWTH_MIN_WORK_MS` would have discarded the cheap shapes entirely,
# where moving up two doublings keeps every shape and measures all of them
# better. A quadratic term is still unmistakable long before an absolute
# ceiling would fire -- at these sizes the pre-fix hook does not finish a
# single affected cell.
_GROWTH_SIZES_KB = (128, 256, 512)

# Best-of-N per cell. `min` picks the least-contended run, which is what makes
# a wall-clock measurement mean something on a machine running several agents
# at once -- the condition under which this suite has actually been observed
# to slow from 4:00 to 6:09.
_GROWTH_REPS = 3

# Below this, a cell's work time is measurement noise rather than signal, and
# a ratio computed from it is meaningless. Measured: on the fixed code the
# smallest cells sit at 0.5-3 ms and produce ratios anywhere from 1.1 to 2.2
# purely from jitter; on the PRE-fix code two shapes with sub-millisecond work
# produced 4.60 and 4.06, which would have been false alarms rather than the
# real quadratic sitting beside them. Cells under the floor are covered by the
# absolute ceiling in `test_generated_shape_within_budget` instead.
_GROWTH_MIN_WORK_MS = 3.0

# Linear work doubles when the input doubles, so an honest ratio sits near
# 2.0; quadratic work quadruples. Measured across all nine shapes on the fixed
# code, worst ratio 2.24. 3.0 sits between the two regimes with ~34% headroom
# over the measured worst case, and the quadratic it exists to catch does not
# arrive at 3.1 -- the three affected shapes do not finish at all at 128 KB.
_GROWTH_MAX_RATIO = 3.0


_MATRIX_LABELS = sorted(
    f"{unit} @ {kb}KB"
    for unit in _MATRIX_UNITS
    for kb in _MATRIX_SIZES_KB
)


def _matrix_command(label):
    """Build one cell, sized by its JSON PAYLOAD rather than its length.

    Sizing on the raw string is a trap that silently empties the cell: a
    quote-heavy shape roughly doubles under JSON escaping, so an "850 KB"
    command became a >1 MB payload, tripped the hooks' own `_STDIN_CAP` and
    was refused WITHOUT being parsed at all. Four of nine cells measured that
    way -- fast, green, and testing nothing. Sizing on the encoded payload
    keeps every cell just under the cap and genuinely through the parser.
    """
    unit_label, _, size = label.rpartition(" @ ")
    target = int(size.removesuffix("KB")) * 1024
    prefix, unit = _MATRIX_UNITS[unit_label]
    prefix = prefix.replace("PUSH", "git pu" + "sh")
    n = max(1, (target - len(prefix)) // len(unit))
    for _ in range(6):
        cmd = prefix + unit * n
        got = len(json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": cmd}}))
        if got <= target:
            return cmd
        n = max(1, int(n * target / got))
    return prefix + unit * n


class TestDecisionTimeIsBounded:
    """A standing wall-clock budget on adversarial input, per hook (#351
    CRIT-3). Round 3's reviewer found the round-2 fix's widened `_Q`
    catch-all made a quote character both a balanced-run opener and an
    ordinary character in the same `(?:...)*` repeat, which is catastrophic
    backtracking: a 43-byte command (34 unbalanced double quotes) took 932ms
    against the shipped 4d67581 pattern, and the reviewer measured over 10s
    at 40. That is what turns "a reviewer happened to think to time it" into
    a standing assertion nothing else in this file would have caught --
    every other test in this class only checks the VERDICT, never how long
    it took to arrive at one.

    Budget: a floor of 500ms, raised to 12x this machine's own measured
    baseline when that is larger -- see CEILING_BASELINE_MULTIPLE. The floor
    was chosen from measurements on this machine (a 2026-era
    Mac): an honest `git status` costs ~22-29ms per hook here --
    interpreter/subprocess startup dominates that number, not the regex
    work -- and the heaviest NON-pathological input measured (a ~900KB
    single argument, close to the 1MB stdin cap, through
    `prevent-direct-push`, which also runs `_push_invocations` and
    `_protected_delete_refs`) cost ~90ms. 500ms is ~5x headroom over that
    measured worst case and ~20x over the honest baseline -- room to
    absorb a slower CI runner -- while staying far under a
    human-noticeable stall (UX guidance generally puts the "user's flow of
    thought stays uninterrupted" threshold around 1s). The CRIT-3 pattern
    blew past 500ms by roughly 2x at just 34 quotes and would have blown
    past it by orders of magnitude at the adversarial sizes used here.

    A PreToolUse hook runs on every Bash tool call in this session, with a
    1MB stdin cap on what it will read — a slow gate here is not merely an
    inconvenience, it stalls every command the user runs.
    """

    BUDGET_MS = 500

    # The ceiling is MACHINE-RELATIVE, because an absolute millisecond number
    # measures the runner as much as the code. CI proved it: the
    # `balanced quote run @ 900KB` row measured 548.4 ms on a shared GitHub
    # runner against ~96 ms locally and failed a flat 500 ms budget, while the
    # growth-ratio assertion below it passed -- a ratio cancels machine speed
    # and an absolute number cannot.
    #
    # Raising 500 to 2000 was the other option and is the wrong one: it picks a
    # number for today's runner, drifts again on tomorrow's, and widens the
    # window in which a genuine slowdown looks acceptable on every machine.
    #
    # `max(BUDGET_MS, MULTIPLE * baseline)` keeps both halves. The floor is
    # what "a human notices a stall" means on a fast machine, where 12x a 25 ms
    # baseline is only 300 ms and nobody would notice. The multiple is what it
    # means on a slow or contended one, where the same honest work legitimately
    # costs proportionally more.
    #
    # 12 comes from measurement, not taste. The honest worst shape across all
    # five hooks and all 27 generated cells is 6.06x its hook's baseline
    # (`prevent-direct-push`, `balanced quote run @ 900KB`: 242.3 ms against a
    # 40.0 ms baseline); the other four hooks peak at 1.54x-2.63x. 12 is ~2x
    # headroom over the worst honest case. Read the other way: once the
    # multiple dominates, this catches a ~2x regression on the worst shape;
    # while the floor dominates it catches a ~3.3x one. Anything smaller is
    # the growth-ratio assertion's job, and that is the primary net -- this is
    # a backstop for pathological ABSOLUTE cost.
    CEILING_BASELINE_MULTIPLE = 12

    # Best-of-2 for the baseline, taken per row rather than once per session:
    # `min` discards a scheduling spike, and re-measuring per row keeps the
    # denominator honest when load varies through a long run.
    CEILING_BASELINE_REPS = 2

    # A per-row subprocess timeout just above the budget, not the shared
    # 60s harness default. Round 3's implementation used the harness's
    # ordinary `_decide`-style 60s cap, so a genuine regression died as
    # `subprocess.TimeoutExpired` — an ERROR whose message says "60
    # seconds", not "over the 500ms budget" — and cost ~10 minutes of wall
    # clock to discover (2 pathological rows x 5 hooks x 60s each) (#351
    # IMP-4). 4x the budget is generous headroom for scheduling jitter
    # (nothing measured here comes within 5x of 500ms honestly) while
    # still failing in low single-digit seconds per row instead of a full
    # minute.
    SUBPROCESS_TIMEOUT_S = 2

    @classmethod
    def _ceiling_ms(cls, baseline_ms):
        """The absolute ceiling for a machine whose baseline is `baseline_ms`.

        See `CEILING_BASELINE_MULTIPLE` for where the 12 comes from.
        """
        return max(cls.BUDGET_MS, cls.CEILING_BASELINE_MULTIPLE * baseline_ms)

    @classmethod
    def _baseline_ms(cls, work, env, hook):
        """Cost of getting in and out of `hook` at all, on this machine now."""
        best = None
        for _ in range(cls.CEILING_BASELINE_REPS):
            start = time.perf_counter()
            proc = subprocess.run(
                [sys.executable, str(work / ".claude/hooks" / f"{hook}.py")],
                input=json.dumps({"tool_name": "Bash",
                                  "tool_input": {"command":
                                                 "git pu" + "sh origin feature/probe"}}),
                capture_output=True, text=True,
                timeout=cls.SUBPROCESS_TIMEOUT_S, cwd=work, env=env,
            )
            assert proc.returncode == 0, (
                f"{hook} baseline exited {proc.returncode}: "
                f"{proc.stderr[-400:]!r}"
            )
            elapsed = (time.perf_counter() - start) * 1000
            best = elapsed if best is None else min(best, elapsed)
        return best

    def test_the_ceiling_scales_with_the_machine(self):
        """The formula's own properties, over synthetic baselines.

        A slow machine cannot be produced on demand, so the two directions
        that matter are asserted directly: the ceiling never shrinks below
        the floor (a suspiciously fast baseline cannot make it fire on
        everything), and it never grows past the multiple (it cannot become
        a ceiling that fires on nothing). The end-to-end proof that a real
        regression still trips it is in the round-7 report: the pre-fix
        quadratic hook reddens these rows at both a 40 ms and a
        load-inflated baseline.
        """
        for baseline in (5.0, 25.0, 40.0, 90.0, 250.0, 1000.0):
            ceiling = self._ceiling_ms(baseline)
            assert ceiling >= self.BUDGET_MS
            assert ceiling <= max(self.BUDGET_MS,
                                  self.CEILING_BASELINE_MULTIPLE * baseline)
            # The honest worst shape, 6.06x its hook's baseline, must pass on
            # every machine -- otherwise this is just a flake generator.
            assert ceiling > 6.06 * baseline

    # label -> command. Chosen to stress the three shapes rounds 1-3 each
    # found a fail-open or a blowup in: unbalanced quotes (CRIT-3), a long
    # run of global-option tokens (more `_GLOBAL_OPTS` iterations), and an
    # argument near the stdin cap (`_push_invocations`'s `_SHLEX_MAX` path).
    ADVERSARIAL_INPUTS = {
        "2000 unbalanced double-quotes":
            "git -a " + '"' * 2000 + " x",
        "2000 unbalanced single-quotes":
            "git -a " + "'" * 2000 + " x",
        "500 stacked global-option tokens":
            "git " + ("-c a=b " * 500) + "pu" + "sh origin ma" + "in",
        "a ~900KB single argument, near the 1MB stdin cap":
            "git pu" + "sh origin " + ("x" * 900_000),
        # #351 round 4 shipped an `_OPT` under which a run of fully-quoted
        # words was 2^n: 141 chars took 1.8s and 165 chars took 33s through
        # this very hook. No row above is a run of fully-quoted words, so
        # nothing here went red. These two are, in both of the spellings
        # that blew up -- a word that is NOT an option token, and one that
        # IS. Each carries the run after BOTH executables, because every
        # other row in this dict says `git ` and is therefore a no-op for
        # the two `gh` gates.
        "2000 fully-quoted words after each executable":
            "git " + ('"a" ' * 2000) + "x gh " + ('"a" ' * 2000) + "x",
        "2000 quoted OPTION words after each executable":
            "git " + ('"-c" ' * 2000) + "x gh " + ('"-c" ' * 2000) + "x",
        # The #351 tag allowlist runs `_is_tag_ref` -- and with it
        # `_VERSION_TAG_RE.fullmatch` -- once per ref TOKEN, so a command
        # with thousands of them is the shape that would expose a
        # superlinear cost there. No row above has more than a handful of
        # ref tokens.
        "5000 version-tag ref tokens":
            "git pu" + "sh --tags origin " + ("v1.2.3 " * 5000),
        # The same count in the shape that makes every one of them FAIL the
        # tag test, so the allowance is refused on the last token rather
        # than short-circuiting on the first.
        "5000 near-miss tag ref tokens":
            "git pu" + "sh origin " + ("v1.2.3x " * 5000),
    }

    @pytest.mark.parametrize("label", sorted(ADVERSARIAL_INPUTS))
    @pytest.mark.parametrize("hook", TestHookInputFailsClosed.HOOKS)
    def test_decision_within_budget(self, tmp_path, hook, label):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        baseline = self._baseline_ms(work, env, hook)
        ceiling = self._ceiling_ms(baseline)
        command = self.ADVERSARIAL_INPUTS[label]
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                [sys.executable, str(work / ".claude/hooks" / f"{hook}.py")],
                input=json.dumps({"tool_name": "Bash",
                                  "tool_input": {"command": command}}),
                capture_output=True, text=True,
                timeout=self.SUBPROCESS_TIMEOUT_S, cwd=work, env=env,
            )
        except subprocess.TimeoutExpired:
            elapsed_ms = (time.perf_counter() - start) * 1000
            pytest.fail(
                f"{hook} took over {elapsed_ms:.0f}ms on {label!r}, well "
                f"past the {ceiling:.0f}ms ceiling (subprocess killed at "
                f"the {self.SUBPROCESS_TIMEOUT_S}s safety cap) -- likely "
                f"catastrophic backtracking or another superlinear blowup"
            )
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert proc.returncode == 0, (
            f"{hook} exited {proc.returncode} on {label!r}: "
            f"{proc.stderr[-400:]!r}"
        )
        assert elapsed_ms < ceiling, (
            f"{hook} took {elapsed_ms:.1f}ms on {label!r}, over the "
            f"{ceiling:.0f}ms ceiling (max of the {self.BUDGET_MS}ms floor "
            f"and {self.CEILING_BASELINE_MULTIPLE}x this machine's "
            f"{baseline:.1f}ms baseline) -- possible catastrophic "
            f"backtracking or other superlinear blowup"
        )

    @pytest.mark.parametrize("label", _MATRIX_LABELS)
    @pytest.mark.parametrize("hook", TestHookInputFailsClosed.HOOKS)
    def test_generated_shape_within_budget(self, tmp_path, hook, label):
        """The same budget, over GENERATED shapes rather than remembered ones.

        Worst cell measured on this machine after the fix: 241 ms
        (`prevent-direct-push`, `balanced quote run @ 900KB`), against the
        500 ms budget -- about 2x headroom, thinner than the hand-picked
        rows enjoy, and deliberately so: these shapes are chosen to be the
        expensive ones. The regression this class exists to catch is
        superlinear, not a constant factor, so it does not arrive at 600 ms
        -- the quadratic term measured 37.9 SECONDS on the 128 KB cell of
        `quoted separators`, and cells here are seven times larger again.
        """
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        baseline = self._baseline_ms(work, env, hook)
        ceiling = self._ceiling_ms(baseline)
        command = _matrix_command(label)
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                [sys.executable, str(work / ".claude/hooks" / f"{hook}.py")],
                input=json.dumps({"tool_name": "Bash",
                                  "tool_input": {"command": command}}),
                capture_output=True, text=True,
                timeout=self.SUBPROCESS_TIMEOUT_S, cwd=work, env=env,
            )
        except subprocess.TimeoutExpired:
            elapsed_ms = (time.perf_counter() - start) * 1000
            pytest.fail(
                f"{hook} took over {elapsed_ms:.0f}ms on the generated shape "
                f"{label!r}, well past the {ceiling:.0f}ms ceiling "
                f"(subprocess killed at the {self.SUBPROCESS_TIMEOUT_S}s "
                f"safety cap) -- a superlinear blowup over separator or "
                f"quote density"
            )
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert proc.returncode == 0, (
            f"{hook} exited {proc.returncode} on {label!r}: "
            f"{proc.stderr[-400:]!r}"
        )
        assert elapsed_ms < ceiling, (
            f"{hook} took {elapsed_ms:.1f}ms on the generated shape "
            f"{label!r}, over the {ceiling:.0f}ms ceiling (max of the "
            f"{self.BUDGET_MS}ms floor and "
            f"{self.CEILING_BASELINE_MULTIPLE}x this machine's "
            f"{baseline:.1f}ms baseline)"
        )

    # ---- the RELATIVE assertion --------------------------------------------
    # Everything above is an absolute millisecond budget, and an absolute
    # budget has two problems that this method exists to answer.
    #
    # It measures the MACHINE as much as the code. This suite has been
    # observed taking 4:00 idle and 6:09 with several agents running, and a
    # loaded machine is exactly when a hook feels slow to a human -- so the
    # budget has to be set loose enough to survive load, which blunts it.
    # Measuring an honest baseline command in the SAME run and subtracting it
    # cancels most of that: interpreter start-up, git calls and scheduler
    # contention move the baseline and the sample together.
    #
    # And it only fires once the input is already big enough to blow it. The
    # quadratic term this class failed to catch was visible as a RATIO at
    # 4 KB -- 70 ms against a flat 127 ms baseline -- long before it was
    # visible as a budget breach at 64 KB. A net that watches the shape of
    # the curve catches such a defect a hundred times smaller than one
    # watching the total.
    #
    # Only `prevent-direct-push` is swept: it is the one hook with the
    # `_push_invocations` span/separator machinery where the growth risk
    # lives. The other four are covered by the absolute matrix above.
    @pytest.mark.parametrize("unit", sorted(_MATRIX_UNITS))
    def test_work_grows_no_faster_than_the_input(self, tmp_path, unit):
        """Doubling the input must not much more than double the work.

        Measured on the fixed code, worst ratio across all nine shapes:
        2.50 idle, and 2.13-2.33 under 12 CPU burners on 12 cores (linear
        work doubles, so ~2.0 is the honest figure). Against the pre-fix
        hook the three shapes carrying both many quoted spans and many
        separators do not finish a single cell -- so this separates the
        regimes rather than merely ranking them.
        """
        work_dir, env = TestHookBlockingPathsFire._repo(tmp_path)

        def measure(command):
            best = None
            for _ in range(_GROWTH_REPS):
                start = time.perf_counter()
                try:
                    proc = subprocess.run(
                        [sys.executable,
                         str(work_dir / ".claude/hooks/prevent-direct-push.py")],
                        input=json.dumps({"tool_name": "Bash",
                                          "tool_input": {"command": command}}),
                        capture_output=True, text=True,
                        timeout=self.SUBPROCESS_TIMEOUT_S, cwd=work_dir,
                        env=env,
                    )
                except subprocess.TimeoutExpired:
                    return None
                assert proc.returncode == 0, (
                    f"prevent-direct-push exited {proc.returncode}: "
                    f"{proc.stderr[-400:]!r}"
                )
                elapsed = (time.perf_counter() - start) * 1000
                best = elapsed if best is None else min(best, elapsed)
            return best

        # The fixed cost of getting in and out of the hook at all, measured
        # here rather than assumed, so load moves it with the samples.
        baseline = measure("git pu" + "sh origin feature/probe")
        assert baseline is not None, "the baseline command itself timed out"

        works = []
        for kb in _GROWTH_SIZES_KB:
            elapsed = measure(_matrix_command(f"{unit} @ {kb}KB"))
            if elapsed is None:
                pytest.fail(
                    f"prevent-direct-push did not finish {unit!r} at {kb}KB "
                    f"within {self.SUBPROCESS_TIMEOUT_S}s -- superlinear "
                    f"growth over separator or quote density (the fixed code "
                    f"costs single-digit milliseconds of work here)"
                )
            works.append((kb, max(elapsed - baseline, 0.0)))

        for (kb_a, work_a), (kb_b, work_b) in zip(works, works[1:]):
            if work_a < _GROWTH_MIN_WORK_MS:
                continue  # noise, not signal -- see _GROWTH_MIN_WORK_MS
            ratio = work_b / work_a
            assert ratio < _GROWTH_MAX_RATIO, (
                f"prevent-direct-push work grew {ratio:.2f}x for {unit!r} "
                f"when the input doubled from {kb_a}KB to {kb_b}KB "
                f"({work_a:.1f}ms -> {work_b:.1f}ms over a {baseline:.1f}ms "
                f"baseline), past the {_GROWTH_MAX_RATIO}x bound. Linear "
                f"work doubles; quadratic work quadruples."
            )


def _hook_regex_constants(hook):
    """Every regex constant a hook builds, folded out of its source.

    The hooks are SCRIPTS -- importing one reads stdin and exits -- so the
    patterns cannot simply be imported. They are, however, plain
    string-concatenation expressions over `r'...'` literals and constants
    defined above them, so this folds the three AST node types that takes
    (a string constant, a name defined earlier in the same file, and `+`)
    by hand. Deliberately NOT `eval`: a file that audits source should not
    execute it, and a hand fold fails loudly on anything it does not model
    instead of quietly running it.
    """
    src = (Path(".claude/hooks") / f"{hook}.py").read_text(encoding="utf-8")
    ns = {}

    def fold(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return ns[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return fold(node.left) + fold(node.right)
        raise TypeError(type(node).__name__)

    for node in ast.parse(src).body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        name = node.targets[0].id
        if not (name in ("_Q", "_OPT", "_GLOBAL_OPTS", "_VERSION_TAG")
                or name.endswith("_VERB")):
            continue
        try:
            ns[name] = fold(node.value)
        except (TypeError, KeyError):
            continue
    return ns


# Times ONE `re.search` and prints the milliseconds. Run as a subprocess so
# a catastrophic backtrack is killed by a timeout instead of wedging pytest
# (Python's `re` does not check for signals while matching, so an in-process
# alarm cannot interrupt it). Pattern and text arrive as JSON on stdin --
# never interpolated into this source, never passed as shell arguments.
_PATTERN_TIMER_SRC = (
    "import json, re, sys, time\n"
    "req = json.load(sys.stdin)\n"
    "rx = re.compile(req['pattern'])\n"
    "start = time.perf_counter()\n"
    "rx.search(req['text'])\n"
    "print((time.perf_counter() - start) * 1000)\n"
)


_PATTERN_REPEATS = 5000

# Repeated between the executable and a trailing non-verb word, so the
# engine must EXHAUST the pattern rather than succeed early -- the worst
# case, and the one a real fail-open command also hits. Module-level
# because a class-body comprehension cannot see other class attributes.
_PATTERN_TOKENS = {
    "a fully-quoted word": '"a" ',
    "a fully-quoted word holding a space": '"a b" ',
    "a single-quoted word": "'a' ",
    "a quoted OPTION word": '"-c" ',
    "a single-quoted OPTION word": "'-c' ",
    "a quoted option and a quoted value": '"-c" "k=v w" ',
    "a quoted option and a bare value": '"-c" k=v ',
    "a quoted option and a quoted dash-value": '"-c" "-x" ',
    "a bare option and a quoted dash-value": '-c "-x" ',
    "a backslash-escaped space in a value": "-c a.b=x\\ y ",
    "a bare backslash escape": "\\x",
    "an unbalanced double quote": '"',
    "an unbalanced single quote": "'",
}

_PATTERN_LABELS = sorted(
    [f"{name} x {_PATTERN_REPEATS}" for name in _PATTERN_TOKENS]
    + ["a ~900KB single argument"]
)


class TestPatternDecisionTimeIsBounded:
    """A timing budget on the PATTERNS themselves, not just end-to-end.

    `TestDecisionTimeIsBounded` measures whole hooks, which is the number
    that matters to a user but a coarse instrument: every row there pays
    ~25ms of interpreter startup, and its four original inputs happened to
    miss the shape that broke round 4. This class times a single
    `re.search` per row against SHAPES GENERATED from a token template, so
    the next spelling is covered by construction rather than by whoever
    thinks of it.

    Why this is a standing assertion rather than a habit: every single
    regex change in `_Q`/`_OPT`/`_GLOBAL_OPTS` so far has been one
    measurement away from a 30-second hook. Round 2 widened `_Q`'s
    catch-all and made a quote both a run-opener and an ordinary character
    (CRIT-3, 932ms at 34 quotes). Round 4 added a fully-quoted word as an
    option token, which made it eligible as the PREVIOUS option's value
    too (BLOCKING-1, 33s at 165 chars). Round 5's first attempt at the fix
    required the `-` inside the quotes, which stops `"a" "a" ...` but not
    `"-c" "-c" ...` -- still 2^n, still only visible by timing it. Each was
    a verdict-preserving change, so no correctness test in this file could
    have caught any of them.

    The rule those three share, written into the pattern's own comment: in
    a `(?:...)*` repeat, any two alternatives that can both match the same
    text hand the engine a choice it will backtrack over. Adding an
    alternative can only ever ADD a gate to the VERDICT; it can multiply
    the COST.
    """

    # 200ms for one `re.search`. The worst honest measurement on this
    # machine across every shape below, at ten times the repeat count used
    # here, is ~27ms (a 600KB run of backslash-escaped values); at the
    # 5000 repeats used here nothing exceeds ~3ms. 200ms is therefore
    # ~60x headroom over the measured worst case while still catching a
    # merely QUADRATIC regression (a candidate measured during round 5 hit
    # 221ms at 2000 repeats), let alone an exponential one, which cannot
    # finish at all at these sizes and dies on the subprocess cap instead.
    BUDGET_MS = 200
    SUBPROCESS_TIMEOUT_S = 5

    REPEATS = _PATTERN_REPEATS
    REPEATED_TOKENS = _PATTERN_TOKENS

    HOOK_VERBS = TestGlobalOptionSpellingsMatchThePlainVerdict.HOOK_VERBS

    @classmethod
    def _texts(cls, exe, verb):
        texts = {
            f"{name} x {cls.REPEATS}": exe + token * cls.REPEATS + " x"
            for name, token in cls.REPEATED_TOKENS.items()
        }
        texts["a ~900KB single argument"] = (
            exe + verb + " " + "x" * 900_000)
        return texts

    @pytest.mark.parametrize("label", _PATTERN_LABELS)
    @pytest.mark.parametrize("hook", sorted(HOOK_VERBS))
    def test_pattern_search_within_budget(self, hook, label):
        exe, verb = self.HOOK_VERBS[hook]
        constants = _hook_regex_constants(hook)
        # Every pattern the hook applies to command text, not just the entry
        # verb. `prevent-direct-push` also carries `_VERSION_TAG`, added with
        # the #351 tag allowlist; a new pattern that is never timed is
        # exactly how rounds 2, 4 and 5 each shipped a 30-second hook.
        patterns = {
            k: v for k, v in constants.items()
            if k.endswith("_VERB") or k == "_VERSION_TAG"
        }
        assert any(k.endswith("_VERB") for k in patterns), (
            f"{hook}.py defines no `*_VERB` constant this test could fold "
            f"-- a rename would otherwise make every row here vacuous"
        )
        text = self._texts(exe, verb)[label]
        for name, pattern in sorted(patterns.items()):
            start = time.perf_counter()
            try:
                proc = subprocess.run(
                    [sys.executable, "-c", _PATTERN_TIMER_SRC],
                    input=json.dumps({"pattern": pattern, "text": text}),
                    capture_output=True, text=True,
                    timeout=self.SUBPROCESS_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired:
                elapsed_ms = (time.perf_counter() - start) * 1000
                pytest.fail(
                    f"{hook}.{name} did not finish one re.search on "
                    f"{label!r} within {elapsed_ms:.0f}ms (killed at the "
                    f"{self.SUBPROCESS_TIMEOUT_S}s cap), against a "
                    f"{self.BUDGET_MS}ms budget -- catastrophic "
                    f"backtracking: two alternatives in a repeat can both "
                    f"match the same text"
                )
            assert proc.returncode == 0, (
                f"{hook}.{name} timer exited {proc.returncode} on "
                f"{label!r}: {proc.stderr[-400:]!r}"
            )
            match_ms = float(proc.stdout.strip())
            assert match_ms < self.BUDGET_MS, (
                f"{hook}.{name} took {match_ms:.1f}ms for one re.search on "
                f"{label!r}, over the {self.BUDGET_MS}ms budget"
            )


class TestUnparseableTextResolvesTowardGating:
    """Every consumer of `_shell_scan` must say which way "cannot tell"
    resolves, and it has to be whichever way ADDS gating FOR THAT CONSUMER.

    `_shell_scan` reports "this position is inside an open quote" and "this
    text does not parse at all" as the same `False`, and it cannot tell
    them apart, because it reads a PREFIX and a prefix that ends mid-quote
    looks exactly like one that never closes. Its two consumers need
    opposite things from that `False`:

    * `_targets_this_project` DROPS a `cd`/`-C` it cannot vouch for. A
      dropped `cd` leaves the command in scope, so the gate still applies.
    * `_push_invocations` must SPLIT on a separator it cannot vouch for. An
      extra segment with no verb is skipped; a MERGED one runs past the ref
      and hands the deletion gate a token like `main;`.

    Round 4 of #351 gave the second consumer the first one's answer --
    reused `_shell_scan` and merged on doubt -- and three command shapes
    that `develop` denies started to ALLOW a delete of a protected branch.
    Measured, all four `_shell_scan` blind spots, on `prevent-direct-push`:
    deny at `74cf1b1`, allow at `b428ff3`.

    The fixtures below are all valid bash (`bash -n` rc 0) whose PREFIX a
    real shell parses fine and `_shell_scan` cannot: an apostrophe in a `#`
    comment, an unbalanced quote in one, a `case` pattern's bare `)`, and a
    heredoc body. Each test asserts the GATING verdict, and pairs it with a
    control proving the row is not trivially gated.
    """

    # label -> (prefix, suffix). The suffix closes the construct the prefix
    # opens, so every fixture is a command bash would actually accept.
    BAILS = {
        "an apostrophe in a `#` comment": ("git status # don't\n", ""),
        "an unbalanced quote in a `#` comment": ('git status # a "b\n', ""),
        "a `case` pattern's bare `)`": ("case a in a) ", " ;; esac"),
        "a heredoc body with an apostrophe":
            ("cat <<'EOF'\ndon't\nEOF\n", ""),
    }

    HOOK_VERBS = TestGlobalOptionSpellingsMatchThePlainVerdict.HOOK_VERBS

    # A `cd` to a directory outside the project stands every gate down --
    # that is `_targets_this_project`'s whole job (#326).
    ELSEWHERE = "cd /nonexistent-elsewhere && "

    @pytest.mark.parametrize("bail", sorted(BAILS))
    @pytest.mark.parametrize("hook", sorted(HOOK_VERBS))
    def test_unvouched_cd_is_dropped_and_the_gate_still_applies(
        self, tmp_path, hook, bail
    ):
        exe, verb = self.HOOK_VERBS[hook]
        prefix, suffix = self.BAILS[bail]
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide

        # Controls, so a `deny` below is not just "this hook denies
        # everything": the bare command is gated, and the SAME `cd` stands
        # it down when the text ahead of it does parse.
        assert decide(work, env, hook, exe + verb) == "deny"
        assert decide(work, env, hook, self.ELSEWHERE + exe + verb) == "allow"

        candidate = prefix + self.ELSEWHERE + exe + verb + suffix
        assert decide(work, env, hook, candidate) == "deny", (
            f"{hook}: {candidate!r} honoured a `cd` that {bail} makes "
            f"unverifiable -- dropping it is the gating direction"
        )

    # `_push_invocations` is `prevent-direct-push`-only, and so is this.
    DELETE = "git pu" + "sh origin --delete "

    # What follows the ref. A shell needs no space around a separator, and
    # the GLUED spellings are the ones only the split can catch: `_bare_ref`
    # strips a TRAILING run of separator characters, so it rescues `main;`
    # from `main; echo hi` but nothing recovers `main` from `main&&echo`.
    # The spaced spelling is kept so both layers stay covered.
    TAILS = ("; echo hi", ";echo hi", "&&echo hi", "|cat")

    @pytest.mark.parametrize("tail", TAILS)
    @pytest.mark.parametrize("bail", sorted(BAILS))
    def test_unvouched_separator_still_splits_the_segment(
        self, tmp_path, bail, tail
    ):
        prefix, suffix = self.BAILS[bail]
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        hook = "prevent-direct-push"

        protected = prefix + self.DELETE + "ma" + "in" + tail + suffix
        assert decide(work, env, hook, protected) == "deny", (
            f"{protected!r} deleted a protected branch: {bail} stopped the "
            f"separator from splitting, so the ref token kept it attached "
            f"and the #333 deletion gate never recognised it"
        )

        # The control: splitting must not have become "deny everything".
        # A release-cleanup delete on the same shape still stands down.
        release = prefix + self.DELETE + "release/1.0.0" + tail + suffix
        assert decide(work, env, hook, release) == "allow", (
            f"{release!r} was denied -- the release-cleanup stand-down has "
            f"to survive the split, or this test proves nothing"
        )

    # A lone quote in a `#` comment or a heredoc body is not a quote to the
    # shell, but `_quoted_spans` cannot see comments or heredocs. Given TWO
    # of them it pairs the first with the second, on a LATER LINE, and
    # vouches for every separator in between -- so the command merged into
    # one segment. The merge alone is harmless; the fail-open needs the
    # separator GLUED to the ref, which makes the token `main&&echo`, past
    # the reach of `_bare_ref`'s trailing-separator strip. Measured `deny`
    # at `74cf1b1`, `allow` at `950609c`. Both quote characters, all three
    # separators, and a heredoc as well as a `#` comment.
    #
    # (prefix, tail) around the `<delete> <ref>` in the middle.
    NEWLINE_SPANNING_QUOTES = {
        "an apostrophe in a `#` comment, then another, glued `&&`":
            ("echo hi # don't\n", "&&echo x # it's"),
        "an apostrophe in a `#` comment, then another, glued `;`":
            ("echo hi # don't\n", ";echo x # it's"),
        "an apostrophe in a `#` comment, then another, glued `|`":
            ("echo hi # don't\n", "|cat # it's"),
        "an apostrophe in a heredoc body, then another, glued `&&`":
            ("cat <<'EOF'\ndon't\nEOF\n", "&&echo x # it's"),
        'a double quote in a `#` comment, then another, glued `&&`':
            ('echo hi # don"t\n', '&&echo x # it"s'),
    }

    @pytest.mark.parametrize("shape", sorted(NEWLINE_SPANNING_QUOTES))
    def test_a_span_across_a_newline_does_not_vouch_for_a_separator(
        self, tmp_path, shape
    ):
        prefix, tail = self.NEWLINE_SPANNING_QUOTES[shape]
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        hook = "prevent-direct-push"

        protected = prefix + self.DELETE + "ma" + "in" + tail
        assert decide(work, env, hook, protected) == "deny", (
            f"{protected!r} deleted a protected branch: {shape} let "
            f"`_quoted_spans` pair two quotes ACROSS a newline and vouch "
            f"for the separator between them, so the segment never split "
            f"and the ref token kept the separator glued to it"
        )

        # Same shape, release ref: the stand-down must still stand down, or
        # this is just "deny everything after a quote".
        release = prefix + self.DELETE + "release/1.0.0" + tail
        assert decide(work, env, hook, release) == "allow", (
            f"{release!r} was denied -- the release-cleanup stand-down has "
            f"to survive the span filter"
        )

    # Isolate the cause: each of these differs from a row above in exactly
    # one respect, and each denies at `74cf1b1` AND at `950609c`, so none
    # of them can be what the rows above are really testing.
    NEWLINE_SPAN_CONTROLS = {
        # the separator is space-separated, so `main` is a clean token and
        # `_bare_ref` never sees the separator at all
        "the separator spaced away from the ref":
            ("echo hi # don't\n", " && echo x # it's"),
        # no second quote, so nothing pairs and no span is built
        "no bracketing quotes at all":
            ("echo hi # dont\n", "&&echo x # its"),
        # no bail, no quotes: the ordinary shape
        "neither a bail nor a quote": ("", "&&echo x"),
    }

    @pytest.mark.parametrize("shape", sorted(NEWLINE_SPAN_CONTROLS))
    def test_newline_span_controls_are_unaffected(self, tmp_path, shape):
        prefix, tail = self.NEWLINE_SPAN_CONTROLS[shape]
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = prefix + self.DELETE + "ma" + "in" + tail
        assert TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command) == "deny", (
            f"{command!r} ({shape}) must deny at every revision -- it is a "
            f"control, not a fixture for the span filter"
        )

    @staticmethod
    def _bare_ref():
        """`_bare_ref` loaded from the shipped source, not a copy of it.

        Same technique as `TestScopeGuardFailsClosedWhenScopeIsUnknowable`.
        """
        src = Path(".claude/hooks/prevent-direct-push.py").read_text(
            encoding="utf-8")
        node = [
            n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef) and n.name == "_bare_ref"
        ][0]
        namespace = {}
        exec(ast.get_source_segment(src, node), namespace)  # noqa: S102
        return namespace["_bare_ref"]

    # (token, the ref git would actually act on)
    GLUED_REF_TOKENS = (
        ("ma" + "in", "ma" + "in"),
        ("ma" + "in;", "ma" + "in"),
        ("ma" + "in&", "ma" + "in"),
        ("ma" + "in|", "ma" + "in"),
        ("ma" + "in;;", "ma" + "in"),
        ('"refs/heads/ma' + 'in"', "ma" + "in"),
        ("+refs/heads/ma" + "in;", "ma" + "in"),
        # NOT stripped: a separator INSIDE the name is part of the ref git
        # receives, and no branch is named `main;x`, so this must stay
        # unprotected or a legitimate command starts being denied.
        ("ma" + "in;x", "ma" + "in;x"),
    )

    @pytest.mark.parametrize("token,expected", GLUED_REF_TOKENS)
    def test_bare_ref_strips_a_glued_trailing_separator(self, token, expected):
        """The second layer, pinned on its own.

        The segment split above is the primary fix, and it makes this
        stripping redundant TODAY -- which is exactly why it needs its own
        test: a defence-in-depth layer that no test exercises is deleted by
        the next person who notices nothing goes red without it. It is here
        because a `;`/`&`/`|` glued to a ref is how round 4 of #351 let a
        delete of a protected branch through, and this is the last place
        that can still be caught.
        """
        assert self._bare_ref()(token) == expected


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


# B1 fixture, generated at module level because a class-body comprehension
# cannot see other class attributes -- same reason `_PATTERN_TOKENS` is here.
# Fragments, for the reason given in TestHookBlockingPathsFire's docstring.
_GLUE_PUSH = "git pu" + "sh"
_GLUE_REFS = ("ma" + "in", "develop")
_GLUE_FORMS = {
    "plain": "{pu} origin {ref}{glue}",
    "-d": "{pu} -d origin {ref}{glue}",
    "--delete": "{pu} --delete origin {ref}{glue}",
    "-f": "{pu} -f origin {ref}{glue}",
}
# Every way bash lets punctuation sit flush against the previous word. Each
# was confirmed valid bash (`bash -n` rc 0) and confirmed to reach git with
# the ref intact.
_GLUE_PUNCTUATION = {
    "trailing backslash": chr(92),
    "redirect to a file": ">f",
    "append to a file": ">>f",
    "clobbering redirect": ">|f",
    "close a descriptor": "<&-",
    "read-write redirect": "<>f",
    "redirect to /dev/null": ">/dev/null",
}
_GLUE_CASES = tuple(
    (f"{ref} {form} {glue_name}",
     template.format(pu=_GLUE_PUSH, ref=ref, glue=glue))
    for ref in _GLUE_REFS
    for form, template in _GLUE_FORMS.items()
    for glue_name, glue in _GLUE_PUNCTUATION.items()
)


class TestAllowlistsCannotShadowTheDenyGates:
    """#351: the tag allowlist ran ABOVE the deletion gate and stood the whole
    hook down on an unanchored substring.

    Three tests decided it, all on the raw command text and all before any
    deny gate had run::

        if "refs/tags/" in command or "--tags" in command:
            sys.exit(0)
        if re.search(r'git push\\s+\\S+\\s+v\\d+\\.\\d+\\.\\d+', command):
            sys.exit(0)

    None is tied to an argument, so ONE tag-shaped thing anywhere on the line
    bought a stand-down for everything else on it. Measured at `1b0d3e5`, all
    ALLOW from a feature branch:

    * `--tags origin --delete main` deleted `main`;
    * `origin refs/tags/v1 --delete main` and `origin v1.2.3 --delete main`
      did the same in the other two spellings;
    * `--tags origin main` pushed `main`.

    Two more got through with no allowlist involved at all, because
    `--mirror` and `--prune` delete remote refs with NO `--delete` flag: the
    deletion gate's `_is_delete()` is false, no ref token is ever examined,
    and nothing else in the hook looks at either flag. `--mirror origin`
    measured ALLOW and removes every remote ref absent locally, `main`
    included.

    The fix is gate ORDER plus ref-token anchoring: both denies run first,
    and the tag allowance now asks whether EVERY push on the line pushes
    nothing but tags — the same correction #333 made to the release-cleanup
    allowlist. The allowance still has to run BEFORE the current-branch
    check, because the release flow really does run `git push origin v3.5.0`
    from `main` (scripts/git-flow-finish.sh, phase 3).

    Command strings are assembled from fragments for the reason given in
    TestHookBlockingPathsFire's docstring: this repo's live hooks inspect
    unexecuted command text, so a literal protected push here blocks the
    tooling that reads this file.
    """

    PUSH = "git pu" + "sh"
    GPUSH = "git -C . pu" + "sh"
    MAIN = "ma" + "in"

    # (label, command, expected) from a FEATURE branch. This is where the
    # deny rows discriminate: on a feature branch the hook has no other
    # reason to refuse, so anything that reaches the end allows.
    CASES = (
        # ---- the filed defect: an allowlist shadowing the deletion gate ----
        ("--tags shadows the deletion gate",
         f"{PUSH} --tags origin --delete {MAIN}", "deny"),
        ("refs/tags/ shadows the deletion gate",
         f"{PUSH} origin refs/tags/v1 --delete {MAIN}", "deny"),
        ("a bare version tag shadows the deletion gate",
         f"{PUSH} origin v1.2.3 --delete {MAIN}", "deny"),
        # The widened suffix must not reopen the shadow it sits next to.
        ("a prerelease tag shadows the deletion gate",
         f"{PUSH} origin v1.2.3-rc1 --delete {MAIN}", "deny"),
        ("a build-metadata tag shadows the deletion gate",
         f"{PUSH} origin v1.2.3+build.5 --delete {MAIN}", "deny"),
        ("--tags shadows a develop deletion",
         f"{PUSH} --tags origin --delete develop", "deny"),
        # ---- the filed defect: an allowlist shadowing a protected PUSH ----
        ("--tags beside a protected branch",
         f"{PUSH} --tags origin {MAIN}", "deny"),
        ("--tags beside a qualified protected ref",
         f"{PUSH} --tags origin refs/heads/{MAIN}", "deny"),
        ("--tags beside a protected refspec",
         f"{PUSH} --tags origin HEAD:{MAIN}", "deny"),
        # ---- a protected ref that is not ADJACENT to the remote ----
        # `targets_protected` tested the literal substring `"origin main"`, so
        # a protected ref one word further along was missed entirely: measured
        # ALLOW at 74cf1b1 from a feature branch, with no tag involved in the
        # `foo` spelling at all. Same substring-vs-ref-token class #333 fixed
        # for deletes, now fixed for pushes by `_protected_push_refs`, which
        # reuses `_ref_tokens` + `_bare_ref` + `_PROTECTED_REF_RE`.
        ("a protected ref one word past the remote",
         f"{PUSH} origin foo {MAIN}", "deny"),
        ("a protected ref past a feature ref",
         f"{PUSH} origin feature/x {MAIN}", "deny"),
        ("develop one word past the remote",
         f"{PUSH} origin foo develop", "deny"),
        ("a protected ref past a tag ref",
         f"{PUSH} origin v1.2.3 {MAIN}", "deny"),
        # Carried by the pre-existing refspec arm, not by the new one — kept
        # as a control that the two agree rather than as proof of either.
        ("a qualified protected ref past another ref",
         f"{PUSH} origin foo refs/heads/{MAIN}", "deny"),
        # Negative controls for the SAME arm: a branch whose name merely begins
        # with a protected one is ordinary. `_PROTECTED_REF_RE.fullmatch` on
        # `_bare_ref(t)` is what buys this; a substring test would deny all of
        # them.
        ("a mainline branch past another ref",
         f"{PUSH} origin foo {MAIN}line", "allow"),
        ("a maintenance branch past another ref",
         f"{PUSH} origin foo {MAIN}tenance", "allow"),
        ("a develop-x branch past another ref",
         f"{PUSH} origin foo develop-x", "allow"),
        ("a qualified mainline branch past another ref",
         f"{PUSH} origin foo heads/{MAIN}line", "allow"),
        # A branch whose LEAF is spelt like a protected one. This is the row
        # that separates a whole-ref `fullmatch` from a `search`: the
        # `mainline`/`maintenance` rows above do not, because
        # `_PROTECTED_REF`'s trailing negative lookahead refuses those under
        # either matcher. Here the lookahead is satisfied (the name ends the
        # token) and only the whole-ref anchoring keeps an ordinary branch
        # pushable.
        ("a branch whose leaf is spelt like a protected one",
         f"{PUSH} origin feature/{MAIN}", "allow"),
        ("a qualified branch whose leaf is spelt like a protected one",
         f"{PUSH} origin refs/heads/feature/{MAIN}", "allow"),
        ("a branch whose leaf is spelt like develop",
         f"{PUSH} origin foo team/develop", "allow"),
        # C5: `"origin main" in command` fires INSIDE `origin maintenance`,
        # so an ordinary branch was refused — measured deny at 74cf1b1 and
        # at fc551f2. `_protected_push_refs` decides the same question on
        # ref tokens, which is both correct and narrower, so the two
        # substring arms are deleted rather than patched.
        ("an ordinary branch the substring arm caught",
         f"{PUSH} origin {MAIN}tenance", "allow"),
        ("an ordinary branch the develop substring arm caught",
         f"{PUSH} origin developer", "allow"),
        # A legitimate MULTI-REF push. Every other negative control for this
        # arm carries a protected-looking name; this one carries none, so it
        # is the row that catches an arm which simply refuses more than one
        # ref rather than reading them. git pushes all three.
        ("several feature refs in one push",
         f"{PUSH} origin feature/a feature/b", "allow"),
        ("several feature refs, one of them qualified",
         f"{PUSH} origin feature/a refs/heads/feature/b feature/c", "allow"),
        # The value-flag handling in `_ref_tokens` exists so a flag's VALUE is
        # never read as a ref. Going through `_ref_tokens` rather than
        # re-deriving tokens is what keeps these right — re-deriving them is
        # how #333's review found a legitimate release cleanup being DENIED.
        ("a value flag whose value is not a ref",
         f"{PUSH} -o ci.skip origin feature/x", "allow"),
        ("a long value flag whose value is not a ref",
         f"{PUSH} --push-option ci.skip origin feature/x", "allow"),
        # The sharpest form of the same claim: a push option whose VALUE is
        # spelt exactly like a protected branch. git sends it to the remote's
        # hook as an opaque string and pushes `feature/x`; nothing touches the
        # protected branch, so this must allow. It denies the moment
        # `_protected_push_refs` stops going through `_ref_tokens`.
        ("a value flag whose value is spelt like a protected branch",
         f"{PUSH} -o {MAIN} origin feature/x", "allow"),
        # ---- --mirror / --prune, which need no --delete to destroy refs ----
        ("mirror push", f"{PUSH} --mirror origin", "deny"),
        ("prune push",
         f"{PUSH} --prune origin +refs/heads/*:refs/heads/*", "deny"),
        ("mirror push the tag allowlist would have shadowed",
         f"{PUSH} --mirror --tags origin", "deny"),
        ("mirror push behind a global option",
         f"{GPUSH} --mirror origin", "deny"),
        # git's parse-options accepts an unambiguous ABBREVIATION of a long
        # option. Verified on git 2.50.1 against a real bare remote: `--mi`,
        # `--mir` and `--mirror` all pushed `refs/heads/main` plus every tag.
        # An exact-token denylist is therefore bypassable by typing less.
        ("abbreviated mirror push", f"{PUSH} --mi origin", "deny"),
        ("half-abbreviated mirror push", f"{PUSH} --mir origin", "deny"),
        ("abbreviated prune push",
         f"{PUSH} --pru origin +refs/heads/*:refs/heads/*", "deny"),
        # ---- C2: the same class as --mirror, one layer out. None of these
        # NAMES a protected ref, and none carries --delete, so every
        # ref-token gate below is blind to them; each was measured ALLOW at
        # 74cf1b1 and at fc551f2, and each puts refs/heads/main AND
        # refs/heads/develop on the remote. `--branches` is the modern
        # spelling of `--all`.
        ("push every branch", f"{PUSH} --all origin", "deny"),
        ("push every branch, modern spelling",
         f"{PUSH} --branches origin", "deny"),
        ("abbreviated push-every-branch", f"{PUSH} --al origin", "deny"),
        ("abbreviated modern spelling", f"{PUSH} --br origin", "deny"),
        # A refspec whose DESTINATION is a glob covering branches does the
        # same job with no flag at all.
        ("a branch-glob refspec",
         f"{PUSH} origin +refs/heads/*:refs/heads/*", "deny"),
        ("a bare glob refspec", f"{PUSH} origin '*:*'", "deny"),
        ("a glob that covers a protected name", f"{PUSH} origin 'ma*'",
         "deny"),
        # ---- C2 negative controls: globs that CANNOT reach a protected ref
        # must stay allowed, or this becomes "deny every wildcard".
        ("a tag glob", f"{PUSH} origin 'refs/tags/*:refs/tags/*'", "allow"),
        ("a feature glob", f"{PUSH} origin 'feature/*'", "allow"),
        ("a qualified feature glob",
         f"{PUSH} origin 'refs/heads/feature/*:refs/heads/feature/*'",
         "allow"),
        # Source side is branches, destination is tags: it creates tags and
        # touches no branch, so it allows. The DESTINATION is what matters.
        ("branches pushed into tags",
         f"{PUSH} origin 'refs/heads/*:refs/tags/*'", "allow"),
        # ---- C3: `_is_tag_ref` reads a refspec's DESTINATION, after the
        # last colon. No row pushed a tag INTO a branch, so flipping that
        # choice to the source side left the class green while this flipped
        # deny->allow — the gate-3 stand-down sits above the refspec arm.
        ("a tag ref pushed into a protected branch",
         f"{PUSH} origin refs/tags/v1:refs/heads/{MAIN}", "deny"),
        ("a tag ref pushed into a protected branch, unqualified",
         f"{PUSH} origin refs/tags/v1:{MAIN}", "deny"),
        # ---- the QUOTED spelling of a flag, on both deny gates ----
        # `_push_invocations` falls back to a whitespace split when
        # `shlex.split` raises on an unbalanced quote, and that split does not
        # strip quoting. `_bare_ref` has taken the quotes off REF tokens since
        # #333; the FLAG tokens kept theirs, so a quoted flag did not start
        # with `-` and every flag test skipped it. Measured at 74cf1b1: all
        # three of these ALLOW, and the `--delete` one deletes a protected
        # branch. Both helpers now strip the quote first.
        ("a quoted mirror flag behind an unbalanced quote",
         f'{PUSH} "--mirror" origin "', "deny"),
        ("a single-quoted mirror flag behind an unbalanced quote",
         f"{PUSH} '--mirror' origin \"", "deny"),
        # This one goes through `shlex`, which strips the quotes itself, so it
        # is the control proving the row above tests the FALLBACK path rather
        # than just the flag matcher.
        ("a quoted mirror flag that shlex can parse",
         f'{PUSH} "--mirror" origin', "deny"),
        # The same bypass on the #333 deletion gate. Kept here rather than in
        # TestProtectedBranchDeletionIsBlocked because the quoting class is
        # what this change fixed; the gate itself is unchanged.
        ("a quoted delete flag beside a protected ref",
         f'{PUSH} "--delete" origin "{MAIN}', "deny"),
        # ---- negative controls: the fix must not be "deny every tag push" --
        ("the release flow's tag push", f"{PUSH} origin v1.2.3", "allow"),
        ("all tags", f"{PUSH} --tags origin", "allow"),
        ("a qualified tag ref", f"{PUSH} origin refs/tags/v1.2.3", "allow"),
        ("a tag push behind a global option", f"{GPUSH} origin v1.2.3",
         "allow"),
        ("a feature branch push", f"{PUSH} origin feature/probe", "allow"),
        ("the #333 release cleanup",
         f"{PUSH} origin --delete release/1.0.0", "allow"),
        ("what git-branch-cleanup runs",
         f"{PUSH} origin --delete feature/x", "allow"),
    )

    @pytest.mark.parametrize(
        "label,command,expected",
        CASES,
        ids=[c[0].replace(" ", "-") for c in CASES],
    )
    def test_allowlist_decision(self, tmp_path, label, command, expected):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        got = TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command)
        assert got == expected, f"{label}: expected {expected}, got {got}"

    # The allowance is only OBSERVABLE from a protected branch, for the reason
    # spelled out on TestProtectedBranchDeletionIsBlocked.DEVELOP_CASES: on a
    # feature branch "stood down" and "fell through" both end in allow. From
    # `main` they separate, and `main` is also where the release flow really
    # pushes its tag from (scripts/git-flow-finish.sh creates the tag on main,
    # then runs `git push origin "$VERSION"`).
    MAIN_CASES = (
        # The case the allowance exists for. Every tag in this repo's history
        # is the bare `vX.Y.Z` spelling.
        ("the release flow's tag push from main",
         f"{PUSH} origin v3.5.0", "allow"),
        ("all tags from main", f"{PUSH} --tags origin", "allow"),
        ("a qualified tag ref from main",
         f"{PUSH} origin refs/tags/v1.2.3", "allow"),
        ("several tags from main",
         f"{PUSH} origin v1.2.3 refs/tags/v1.2.4", "allow"),
        # The old version-tag test was a literal `git push` regex, so a tag
        # push carrying a global option FALSELY DENIED from main — measured
        # at `1b0d3e5`. The replacement reads ref tokens, so it does not care
        # how the verb is spelled.
        ("a tag push behind a global option, from main",
         f"{GPUSH} origin v1.2.3", "allow"),
        # A prerelease or build-metadata suffix is an ordinary thing to cut,
        # and a false deny on the release flow is how a gate gets switched
        # off. The suffix opens nothing: a ref matching this pattern is by
        # construction neither `main` nor `develop`, and a branch that happens
        # to be named `v1.2.3-foo` was always pushable anyway.
        ("a prerelease tag from main", f"{PUSH} origin v4.0.0-rc1", "allow"),
        ("a build-metadata tag from main",
         f"{PUSH} origin v1.2.3+build.5", "allow"),
        ("a prerelease tag behind a global option, from main",
         f"{GPUSH} origin v2.0.0-beta.1", "allow"),
        # ---- controls: main must not become an allow-everything branch ----
        ("--tags beside a protected branch, from main",
         f"{PUSH} --tags origin {MAIN}", "deny"),
        ("--tags shadowing a deletion, from main",
         f"{PUSH} --tags origin --delete {MAIN}", "deny"),
        ("a plain protected push from main", f"{PUSH} origin {MAIN}", "deny"),
        # The row that separates `all` from `any`. Both agree whenever there
        # is exactly ONE ref token, so every other deny row here survives the
        # `any` mutation: `--tags origin main` has one ref, `main`, which is
        # not a tag either way. Only a MIX tells them apart — and it has to be
        # judged from `main`, because on a feature branch a stand-down and a
        # fall-through both end in allow.
        ("a tag ref beside a protected branch ref, from main",
         f"{PUSH} origin refs/tags/v1 {MAIN}", "deny"),
        ("a tag ref beside an ordinary branch ref, from main",
         f"{PUSH} origin v1.2.3 feature/x", "deny"),
        ("a mirror push from main", f"{PUSH} --mirror origin", "deny"),
        # `--follow-tags` is NOT a tags-only push and must not be treated as
        # one. Verified on git 2.50.1 against a real bare remote with
        # push.default=current: `git push --follow-tags origin` put
        # `refs/heads/main` on the remote alongside the tag, where
        # `git push --tags origin` pushed the tag ALONE. Standing the hook
        # down on a bare `--follow-tags` would therefore have opened a fresh
        # route to pushing `main` — the very thing this class closes.
        ("--follow-tags is a branch push, from main",
         f"{PUSH} --follow-tags origin", "deny"),
        # With an explicit tag refspec it adds only reachable tags, so it is
        # a genuine tags-only push and must still be allowed.
        ("--follow-tags with an explicit tag ref, from main",
         f"{PUSH} --follow-tags origin v1.2.3", "allow"),
    )

    @pytest.mark.parametrize(
        "label,command,expected",
        MAIN_CASES,
        ids=[c[0].replace(" ", "-") for c in MAIN_CASES],
    )
    def test_allowlist_decision_from_main(self, tmp_path, label, command,
                                          expected):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b",
                        self.MAIN], env=env, check=True, capture_output=True)
        got = TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command)
        assert got == expected, f"{label}: expected {expected}, got {got}"

    # A release/hotfix branch exits at gate 5 (`is_release_or_hotfix_finish`)
    # before gate 6 runs at all, so gate 6 cannot stand in for the deletion
    # gate here. That makes this the ONLY place the quote strip in
    # `_is_delete` is observable: everywhere else the ref-token arm of
    # `targets_protected` reaches the same verdict by another route, and
    # removing the strip turned no row red until these went in. The branch the
    # release flow actually runs from is a `release/*` one, so this is the
    # live path, not a contrived one.
    RELEASE_CASES = (
        ("a quoted delete flag, from a release branch",
         f'{PUSH} "--delete" origin "{MAIN}', "deny"),
        ("a single-quoted delete flag, from a release branch",
         f"{PUSH} '--delete' origin \"{MAIN}", "deny"),
        ("a quoted short delete flag, from a release branch",
         f'{PUSH} "-d" origin "{MAIN}', "deny"),
        # Control: the bare spelling already denied at 74cf1b1, so the three
        # rows above are testing the QUOTING rather than the deletion gate.
        ("a bare protected deletion, from a release branch",
         f"{PUSH} --delete origin {MAIN}", "deny"),
        # Control: a release branch must stay able to clean up its own refs.
        ("the intended cleanup, from a release branch",
         f"{PUSH} --delete origin release/x", "allow"),
        # Control: gate 5 really does stand this branch down, which is what
        # makes the rows above meaningful rather than incidental.
        ("a release branch may push main during a finish",
         f"{PUSH} origin {MAIN}", "allow"),
    )

    @pytest.mark.parametrize(
        "label,command,expected",
        RELEASE_CASES,
        ids=[c[0].replace(" ", "-") for c in RELEASE_CASES],
    )
    def test_allowlist_decision_from_release_branch(self, tmp_path, label,
                                                    command, expected):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b",
                        "release/1.0.0"], env=env, check=True,
                       capture_output=True)
        got = TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command)
        assert got == expected, f"{label}: expected {expected}, got {got}"

    # C4. Two guards in `_is_tag_push_only` that no row reached. Both are
    # about text where the hook's two matchers DISAGREE, which is why no
    # ordinary command exercises them — and both hand out a stand-down, so
    # the direction that matters is the one where they refuse.
    DISAGREEMENT_CASES = (
        # `_PUSH_VERB`'s `\s+` matches a newline, so the entry test sees one
        # `git push` spanning it; `_push_invocations` splits there and finds
        # none. Valid bash (it runs `git`, then `push`, which is not a
        # command), and the two matchers reach opposite answers. If the
        # `if not invocations: return False` guard returned True instead,
        # gate 3 would stand the hook down on it.
        ("the two matchers disagree across a newline",
         "git \npu" + "sh origin " + MAIN, "deny"),
        # A separator inside a quoted run that closes is NOT a separator, so
        # this arrives as one ref token spelt `refs/tags/v1;<protected>`.
        # It starts with `refs/tags/`, so without the separator refusal in
        # `_is_tag_ref` it reads as a tag and stands the hook down. Judged
        # from `main`, where a stand-down and a fall-through differ.
        ("a separator inside a quoted ref token",
         f'{PUSH} origin "refs/tags/v1;{MAIN}"', "deny"),
    )

    @pytest.mark.parametrize(
        "label,command,expected",
        DISAGREEMENT_CASES,
        ids=[c[0].replace(" ", "-") for c in DISAGREEMENT_CASES],
    )
    def test_matcher_disagreement_does_not_stand_the_hook_down(
            self, tmp_path, label, command, expected):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b",
                        self.MAIN], env=env, check=True, capture_output=True)
        got = TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command)
        assert got == expected, f"{label}: expected {expected}, got {got}"

    # ---- B1: shell punctuation glued to the ref token ----------------------
    # `git push origin <protected>>/dev/null` is one WORD to this hook and two
    # to bash: bash splits the redirection off and really pushes the branch.
    # Same for a trailing backslash, which bash strips. `_bare_ref` normalised
    # neither, so `_protected_push_refs` saw `main>/dev/null` and `main\` and
    # matched neither.
    #
    # This was masked until 5f7f29b by the `"origin main" in command` substring
    # arm -- the arm fired on the raw text regardless of how the token ended.
    # Deleting the arm (C5) was right, and it did expose two other real gaps,
    # but it had no deny row carrying THIS glue, so a passing mutation could
    # not see it. Measured: 0 of 84 rows fail open at 74cf1b1, 84 of 84 at
    # 5f7f29b..95dd663, every one valid bash (`bash -n` rc 0) and every one a
    # real push or a real deletion of a protected branch.
    #
    # Generated rather than listed, for the reason the timing matrix is:
    # a hand-picked glue list is what missed this in the first place.
    GLUE_CASES = _GLUE_CASES

    @pytest.mark.parametrize(
        "label,command",
        GLUE_CASES,
        ids=[c[0].replace(" ", "-") for c in GLUE_CASES],
    )
    def test_glued_punctuation_does_not_hide_a_protected_ref(
            self, tmp_path, label, command):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        got = TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command)
        assert got == "deny", f"{label}: expected deny, got {got}"

    # The other half of the claim. Normalising the glue must not turn into
    # "deny anything with punctuation on it", and in particular must not
    # resurrect the substring false-denies C5 removed: `maintenance`, `main2`,
    # `mainline` and `developer` all DENIED at 74cf1b1 and must stay allowed.
    # `>{protected}` is the sharp one -- it is a redirection INTO a file named
    # like a protected branch, not a push of one, and the split must read it
    # that way round.
    GLUE_CONTROLS = (
        ("a feature branch wearing a redirect",
         f"{PUSH} origin feature/x>/dev/null", "allow"),
        ("a feature branch wearing a backslash",
         f"{PUSH} origin feature/x" + chr(92), "allow"),
        ("maintenance wearing a redirect",
         f"{PUSH} origin {MAIN}tenance>/dev/null", "allow"),
        ("a main2 branch wearing a redirect",
         f"{PUSH} origin {MAIN}2>/dev/null", "allow"),
        ("a mainline branch", f"{PUSH} origin {MAIN}line", "allow"),
        ("a developer branch wearing a redirect",
         f"{PUSH} origin developer>/dev/null", "allow"),
        ("a tag wearing a redirect",
         f"{PUSH} origin refs/tags/v1.2.3>/dev/null", "allow"),
        # A redirection whose TARGET FILE is named like a protected branch.
        # The word before the `>` is what git receives; there is no ref here
        # at all, and the current branch is a feature branch.
        ("a redirect into a file named like a protected branch",
         f"{PUSH} origin >{MAIN}", "allow"),
    )

    @pytest.mark.parametrize(
        "label,command,expected",
        GLUE_CONTROLS,
        ids=[c[0].replace(" ", "-") for c in GLUE_CONTROLS],
    )
    def test_glue_normalisation_does_not_over_deny(
            self, tmp_path, label, command, expected):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        got = TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command)
        assert got == expected, f"{label}: expected {expected}, got {got}"

    def test_the_gate_order_is_what_carries_these(self):
        """The negative control on ORDER, which no verdict row can express.

        Every deny row above would still pass against a hook that blocked
        everything, and — more to the point — the whole defect was an
        allowlist sitting in the wrong PLACE rather than a missing check.

        This assertion is not redundant with the verdict rows, and the
        mutation says so: moving `_is_tag_push_only` back above the deletion
        gate turns NO verdict row red, because the function refuses on
        `_is_delete()` and `_all_ref_flags()` by itself before it ever looks
        at a ref. The two are belt and braces — the position and the internal
        checks each close #351 alone — and this is the only test that can
        catch the position half going. (Deleting the gate-2 denies, by
        contrast, turns seventeen verdict rows red; changing `all` to `any`
        turns the mixed-ref rows red; and dropping the `_protected_push_refs`
        arm turns forty-nine red, because since the two substring arms were
        removed it is the only thing carrying a protected-push deny at all.)
        """
        source = Path(".claude/hooks/prevent-direct-push.py").read_text(
            encoding="utf-8")

        # The three substring tests that were the defect. Matched in their
        # CODE shape, not as bare prose: the replacement's own docstring
        # quotes each of them to say what it replaced, the way
        # `test_the_delete_analysis_is_what_carries_these` matches
        # `'"--delete" in command and'` rather than `'"--delete"'`.
        assert 'if "refs/tags/" in command' not in source, (
            "the unanchored refs/tags/ substring allowlist is back; #351 is "
            "reachable again"
        )
        assert 'or "--tags" in command:' not in source, (
            "the unanchored --tags substring allowlist is back; #351 is "
            "reachable again"
        )
        assert ("git pu" + "sh" + r"\s+\S+\s+v") not in source, (
            "the unanchored version-tag regex is back; it also spelt the "
            "verb literally, so a global-option tag push falsely denied"
        )

        delete_deny = source.index(
            "_deleted_protected = _protected_delete_refs(command)")
        mirror_deny = source.index("_all_ref_pushes = [")
        tag_allow = source.index("if _is_tag_push_only(command):")
        branch_check = source.index('["main", "develop"]')

        assert delete_deny < tag_allow, (
            "the tag allowance runs BEFORE the protected-deletion deny again "
            "— that is exactly #351: one tag ref on the line stands the whole "
            "hook down while `main` is being deleted"
        )
        assert mirror_deny < tag_allow, (
            "the tag allowance runs BEFORE the mirror/prune deny again — "
            "`--mirror --tags origin` is allowed as a tag push"
        )
        assert tag_allow < branch_check, (
            "the tag allowance runs AFTER the current-branch check — the "
            "release flow's `git push origin v3.5.0` from main now denies "
            "(scripts/git-flow-finish.sh phase 3)"
        )

    def test_the_non_adjacent_arm_is_what_blocks_those(self):
        """The negative control on the new `targets_protected` arm.

        Without it the deny rows above would still pass against a hook that
        blocked everything, and the `mainline`/`maintenance` allow rows would
        still pass if the arm were deleted, because a feature branch has no
        other reason to refuse them. Two claims: the arm exists, and it
        decides on a WHOLE ref rather than a substring.
        """
        source = Path(".claude/hooks/prevent-direct-push.py").read_text(
            encoding="utf-8")
        assert "_protected_push_refs(command)" in source, (
            "the non-adjacent protected-push arm is gone; `origin foo "
            "<protected>` is allowed from a feature branch again"
        )
        body = source.split("def _protected_push_refs", 1)[1].split(
            "\ndef ", 1)[0]
        assert "_ref_tokens(tokens)" in body, (
            "_protected_push_refs no longer goes through `_ref_tokens`, so a "
            "value flag's value can be read as a ref — that is the false deny "
            "#333's review found"
        )
        assert "_PROTECTED_REF_RE.fullmatch(_bare_ref(t))" in body, (
            "_protected_push_refs no longer matches a WHOLE ref; `mainline` "
            "and `maintenance` are ordinary branches and must stay allowed"
        )

    def test_the_tag_allowance_asks_about_every_ref(self):
        """`all`, not `any` — the #333 correction, applied here.

        Without it `--tags origin main` is a tag push with a branch riding
        along, which is the shape of every deny row above.
        """
        source = Path(".claude/hooks/prevent-direct-push.py").read_text(
            encoding="utf-8")
        body = source.split("def _is_tag_push_only", 1)[1].split(
            "\n_deleted_protected", 1)[0]
        assert "if not all(_is_tag_ref(r) for r in refs):" in body, (
            "_is_tag_push_only no longer asks about EVERY ref token; one tag "
            "on the line buys a stand-down for whatever else is on it"
        )
