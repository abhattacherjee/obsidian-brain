"""Security hardening tests for obsidian-brain."""
import ast
import json
import os
import shutil
import stat
import subprocess
import sys
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
