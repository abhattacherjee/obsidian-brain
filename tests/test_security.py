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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

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

    # #379: bare provider tokens (auto-capture #425 writes notes unreviewed).
    @pytest.mark.parametrize("token,label", [
        ("sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0K1l2", "[REDACTED:api-key]"),
        ("sk-proj-" + "a1B2c3D4e5F6g7H8i9J0k1L2", "[REDACTED:api-key]"),
        ("sk-" + "abcdefghijklmnopqrstuvwx", "[REDACTED:api-key]"),
        ("github_pat_" + "11ABCDEFG0123456789abc_xyz", "[REDACTED:github-token]"),
        ("xoxb-" + "1234567890-abcdefghij", "[REDACTED:slack-token]"),
        ("xoxp-" + "1234567890-abcdefghij", "[REDACTED:slack-token]"),
    ])
    def test_scrub_provider_tokens(self, token, label):
        from obsidian_utils import scrub_secrets
        result = scrub_secrets(f"body key {token} end")
        assert token not in result
        assert label in result

    def test_scrub_provider_token_negative_control(self):
        from obsidian_utils import scrub_secrets
        text = "the sk-learn task and a risk-averse plan"
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


def _static_int(node, consts):
    """The int value of *node* when it is built only from int literals,
    names in *consts* and ``+``/``-``/``*``; None otherwise. The vendored
    hooks spell their cap ``_MAX_INPUT_BYTES = 8 * _MAX_COMMAND_BYTES``, so a
    literal-only read would leave it unresolved."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
        left = _static_int(node.left, consts)
        right = _static_int(node.right, consts)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        return left * right
    return None


def _vendored_hook_constant(hook, name):
    """Module-level int constant *name* of a vendored hook, read with ``ast``
    (the hook is never imported, so nothing in it runs)."""
    path = Path(".claude/hooks") / f"{hook}.py"
    consts = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            value = _static_int(node.value, consts)
            if value is not None:
                consts[node.targets[0].id] = value
    assert name in consts, f"{path} defines no int constant {name}"
    return consts[name]


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
    # The five gate hooks in .claude/hooks/ are vendored unchanged from
    # harden-repo (#418). They read up to 16 MiB of bytes and judge the
    # whole payload, refusing anything larger (harden-repo #152, #156 G-1);
    # the command itself is capped at 2 MiB inside. A literal here, not the
    # hooks' own constant, so a later vendor that raises the cap fails this
    # test instead of moving it.
    VENDORED_HOOK_CAP = 16 * 1024 * 1024

    @classmethod
    def _max_allowed(cls, path):
        """The largest bound a read in *path* may have."""
        if Path(path).parts[:2] == (".claude", "hooks"):
            return cls.VENDORED_HOOK_CAP + 1
        return cls.MAX_ALLOWED

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
            if isinstance(node, ast.Assign):
                # Folded against the names bound so far, so `B = 8 * A`
                # resolves once `A = <int>` has been seen.
                value = _static_int(node.value, seen)
                if value is not None:
                    for target in node.targets:
                        if not isinstance(target, ast.Name):
                            continue
                        if target.id in seen and seen[target.id] != value:
                            ambiguous.add(target.id)
                        seen[target.id] = value
        return {k: v for k, v in seen.items() if k not in ambiguous}

    @classmethod
    def _resolve_bound(cls, arg, consts):
        """Static value of a read()'s size argument, or None if unresolvable."""
        if arg is None:
            return None
        return _static_int(arg, consts)

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
            if bound is None or bound > self._max_allowed(path)
        ]
        assert not offenders, (
            "Uncapped or over-cap stdin read(s): " + ", ".join(offenders)
            + f". Cap reads at {self.CAP} characters in hooks/ and scripts/ "
            "(project CLAUDE.md security pattern) and at "
            f"{self.VENDORED_HOOK_CAP} bytes in .claude/hooks/; read(CAP + 1) "
            "is allowed for overflow detection."
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

    @pytest.mark.parametrize("tree,bound,capped", [
        ("hooks", "1_000_001", True),
        ("hooks", "1_000_002", False),
        # The vendored cap is accepted only in .claude/hooks/ ...
        ("hooks", "8 * _MAX_COMMAND_BYTES + 1", False),
        (".claude/hooks", "8 * _MAX_COMMAND_BYTES + 1", True),
        # ... and is not a blank cheque there either.
        (".claude/hooks", "8 * _MAX_COMMAND_BYTES + 2", False),
        ("scripts", "_MAX_INPUT_BYTES + 1", False),
    ])
    def test_each_tree_keeps_its_own_cap(self, tmp_path, monkeypatch, tree,
                                          bound, capped):
        """The 16 MiB allowance for the vendored hooks must not leak into
        hooks/ or scripts/, which keep the 1 MB rule, and the folded
        ``8 * _MAX_COMMAND_BYTES`` must resolve to its real value."""
        monkeypatch.chdir(tmp_path)
        planted = Path(tree) / "planted_entry_point.py"
        planted.parent.mkdir(parents=True)
        planted.write_text(
            "import sys\n_MAX_COMMAND_BYTES = 2 * 1024 * 1024\n"
            "_MAX_INPUT_BYTES = 8 * _MAX_COMMAND_BYTES\n"
            f"raw = sys.stdin.buffer.read({bound})\n", encoding="utf-8")
        monkeypatch.setattr(
            type(self), "_source_modules", classmethod(lambda cls: [planted])
        )
        assert len(self._all_stdin_reads()) == 1
        if capped:
            self.test_every_stdin_read_is_capped()
        else:
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
            # #371: reads the payload for sid/cwd when obsidian_utils fails
            # to import.
            "hooks/hook_bootstrap.py",
            # #395: the wiki CLI reads its JSON payload from stdin.
            "hooks/wiki.py",
        ):
            assert expected in paths, f"stdin read in {expected} no longer discovered"
        # Exact, not >=. What >= permits is SUBSTITUTION: an existing read
        # reformatted past the AST extractor at the same moment a new entry
        # point lands keeps the total at 15 and the suite green, while the
        # reformatted file's cap silently stops being verified. Every one of
        # the 15 is named above, so a new entry point should fail here and be
        # added deliberately. (tests/test_hooks_resolver_drift.py makes the
        # same call for the same reason.)
        found = self._all_stdin_reads()
        assert len(found) == 15, (
            f"expected exactly 15 stdin read sites, found {len(found)}: "
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
    # The vendored hooks read up to `_MAX_INPUT_BYTES` (16 MiB) and judge the
    # whole payload (harden-repo #152, #156 G-1). Padding to our old 1 MB cap
    # left a payload they read in full and correctly allowed, so the
    # oversize rows below take the cap from the hooks themselves.
    CAP = _vendored_hook_constant("require-preflight", "_MAX_INPUT_BYTES")

    def test_every_hook_has_the_same_input_cap(self):
        """CAP is read from one hook; the boundary rows below send every
        hook a payload sized from it, so they must all agree."""
        caps = {hook: _vendored_hook_constant(hook, "_MAX_INPUT_BYTES")
                for hook in self.HOOKS}
        assert set(caps.values()) == {self.CAP}, caps
        assert self.CAP == TestStdinCap.VENDORED_HOOK_CAP

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
        return cls._decide_with_reason(work, env, hook, command)[0]

    @staticmethod
    def _decide_with_reason(work, env, hook, command):
        """Run one hook on Claude Code's payload; return (decision, reason).

        Claude Code always sends ``cwd`` (the shell's directory), and the
        vendored hooks resolve a relative ``git -C`` against it: without it,
        ``git -C . push`` has no absolute base and is refused. A payload
        without ``cwd`` is not one production sends, so it is not the
        default here."""
        proc = subprocess.run(
            [sys.executable, str(work / ".claude/hooks" / f"{hook}.py")],
            input=json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": command},
                              "cwd": str(work)}),
            capture_output=True, text=True, timeout=60, cwd=work, env=env,
        )
        assert proc.returncode == 0, (
            f"{hook} exited {proc.returncode}; a non-zero exit is a non-blocking "
            f"error and the command PROCEEDS: {proc.stderr[-400:]!r}"
        )
        try:
            out = json.loads(proc.stdout)["hookSpecificOutput"]
            return (out.get("permissionDecision", "allow"),
                    out.get("permissionDecisionReason", ""))
        except (ValueError, KeyError, TypeError, AttributeError):
            return "allow", ""

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
        hook = "update-changelog-before-pr"
        changelog = work / "CHANGELOG.md"

        def git(*args):
            subprocess.run(["git", "-C", str(work), "-c", "user.email=t@example.invalid",
                            "-c", "user.name=t", *args],
                           env=env, check=True, capture_output=True)

        def commit_changelog(text, msg):
            changelog.write_text(text)
            git("add", "CHANGELOG.md")
            git("com" + "mit", "-q", "-m", msg)

        # The vendored gate also requires CHANGELOG.md to CHANGE on this
        # branch against origin/develop (harden-repo #156 G-22), so the repo
        # gets a base: origin/develop at a commit whose [Unreleased] already
        # holds an entry from earlier work.
        stale = "# Changelog\n\n## [Unreleased]\n\n### Added\n- an old entry\n"
        commit_changelog(stale, "base")
        git("update-ref", "refs/remotes/origin/develop", "HEAD")

        # A stale entry: present on the base, not changed on this branch.
        decision, reason = self._decide_with_reason(work, env, hook, cmd)
        assert decision == "deny"
        assert "NOT modified on this branch" in reason, reason

        commit_changelog("# Changelog\n\n## [Unreleased]\n\n## [1.0.0]\n\nnothing\n",
                         "empty unreleased")
        assert self._decide(work, env, hook, cmd) == "deny"

        changelog.unlink()
        assert self._decide(work, env, hook, cmd) == "deny"

        # A branch commit that adds the entry.
        commit_changelog(stale + "- a real entry\n", "add entry")
        assert self._decide(work, env, hook, cmd) == "allow"


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
        # harden-repo #156 G-11: a `-C` target only descopes when it EXISTS.
        # A missing one makes git fail, and "cannot go there" is never
        # "somewhere else", so the allow control uses a real outside dir.
        ("prevent-direct-push", "git -C {elsewhere} pu" + "sh origin ma" + "in", "allow"),
        ("prevent-direct-push", "git -C /nonexistent-elsewhere pu" + "sh origin ma" + "in", "deny"),
        # G-9: defining an alias is refused on purpose (a later `git p` is
        # invisible to every hook). See test_an_alias_definition_is_refused.
        ("prevent-direct-push", "git config --global alias.p pu" + "sh", "deny"),
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
        # G-13: git rejects `-a` ("unknown option") before it pushes
        # anything, and the vendored hook reads that and allows it.
        ("prevent-direct-push",
         'git -a ' + '"' * 40 + ' pu' + "sh origin ma" + "in", "allow"),
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
        ("validate-branch-name", "git -C {elsewhere} chec" + "kout -b nonsense-branch", "allow"),
        ("validate-branch-name", "git -C /nonexistent-elsewhere chec" + "kout -b nonsense-branch", "deny"),
        ("validate-branch-name", "git config --global alias.c chec" + "kout", "deny"),
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
        ("require-preflight", "git -C {elsewhere} com" + "mit -m wip", "allow"),
        ("require-preflight", "git -C /nonexistent-elsewhere com" + "mit -m wip", "deny"),
        ("require-preflight", "git config --global alias.c com" + "mit", "deny"),
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
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        command = command.replace("{elsewhere}", str(elsewhere))
        assert TestHookBlockingPathsFire._decide(work, env, hook, command) == expected, (
            f"{hook} got {command!r} wrong"
        )

    @pytest.mark.parametrize("hook,verb", (
        ("prevent-direct-push", "pu" + "sh"),
        ("validate-branch-name", "chec" + "kout"),
        ("require-preflight", "com" + "mit"),
    ))
    def test_an_alias_definition_is_refused(self, tmp_path, hook, verb):
        """harden-repo #156 G-9, kept upstream by decision: a hook cannot see
        what an alias runs, so `git config alias.p push` would let a later
        `git p origin main` through every gate. The deny must say why, so a
        reader is not sent hunting for a shell expansion."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        command = f"git config --global alias.x {verb}"
        decision, reason = TestHookBlockingPathsFire._decide_with_reason(
            work, env, hook, command)
        assert decision == "deny", f"{hook} allowed {command!r}"
        assert "alias" in reason, f"{hook}: {reason!r}"

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

    # `gh` has no `-C`: `gh -C . pr create` exits with "unknown shorthand
    # flag: 'C'" before it does anything, so a spelling that STARTS with
    # `-C` is not a no-op for gh, and the vendored gh hooks allow it
    # (harden-repo #156 G-14). Those rows are pinned separately below.
    GH_HOOKS = ("enforce-pr-base-branch", "update-changelog-before-pr")
    # A lambda, because a comprehension in a class body cannot see the
    # class's other names.
    SPELLING_PARAMS = (lambda verbs, spellings, gh: [
        pytest.param(hook, spelling, id=f"{hook}-{spelling}")
        for hook in sorted(verbs)
        for spelling in spellings
        if not (hook in gh and spelling.startswith("-C"))
    ])(HOOK_VERBS, OPTION_SPELLINGS, GH_HOOKS)
    GH_DASH_C_SPELLINGS = tuple(s for s in OPTION_SPELLINGS if s.startswith("-C"))

    @pytest.mark.parametrize("hook,spelling", SPELLING_PARAMS)
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

    @pytest.mark.parametrize("spelling", GH_DASH_C_SPELLINGS)
    @pytest.mark.parametrize("hook", GH_HOOKS)
    def test_a_leading_gh_dash_c_is_allowed_because_gh_rejects_it(
        self, tmp_path, hook, spelling
    ):
        """The rows left out of the matrix above, with the verdict the
        vendored hooks give (harden-repo #156 G-14, a deliberate ruling).
        `gh -C <x> pr create` never creates a PR: gh exits on the unknown
        flag. The plain form must still deny, or this proves nothing."""
        exe, verb = self.HOOK_VERBS[hook]
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        assert TestHookBlockingPathsFire._decide(
            work, env, hook, exe + verb) == "deny"
        assert TestHookBlockingPathsFire._decide(
            work, env, hook, exe + spelling + verb) == "allow"

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

# Our old hooks' `_GLOBAL_OPTS` regex did not match a leading-dash-double-
# quoted, leading-dash-single-quoted or every-character-escaped option
# token, and those rows were strict xfails. The vendored harden-repo hooks
# match all eight quotings (#418, harden-repo #156 G-29), so every row is
# now a plain assertion.

# Two values, so the axis is not silently coupled to one value shape.
_OPTION_TOKEN_VALUES = {
    "bare": "a.b=v",
    "shlex-quoted-space": "a.b=" + shlex.quote("x y"),
}

_OPTION_TOKEN_PARAMS = [
    pytest.param(quoting, value_name, id=f"{quoting}-{value_name}")
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
        shell, so all five must reach the same verdict as the plain form,
        and so must the three further quotings in `_option_token_quotings`.
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
#
# #413 moved them up once more, to the largest doubling that fits under the
# hooks' 1,000,000-byte stdin cap (900 KB is 921,600 bytes). The noise floor
# below now scales with the baseline, and more work per cell is what keeps a
# small quadratic above it: at 128/256/512 KB a term costing 6/24/96 ms of
# work was skipped against a 60 ms baseline (Codex, PR #415).
_GROWTH_SIZES_KB = (225, 450, 900)

# Best-of-N per cell. `min` picks the least-contended run, which is what makes
# a wall-clock measurement mean something on a machine running several agents
# at once -- the condition under which this suite has actually been observed
# to slow from 4:00 to 6:09.
_GROWTH_REPS = 5

# Below this, a cell's work time is measurement noise rather than signal, and
# a ratio computed from it is meaningless. Measured: on the fixed code the
# smallest cells sit at 0.5-3 ms and produce ratios anywhere from 1.1 to 2.2
# purely from jitter; on the PRE-fix code two shapes with sub-millisecond work
# produced 4.60 and 4.06, which would have been false alarms rather than the
# real quadratic sitting beside them. A pair under the floor is not compared.
# The absolute ceiling in `test_generated_shape_within_budget` is no backstop
# for a mild quadratic: on PR #415 a planted term (command.count run
# len//1000 times) passed all 27 of its prevent-direct-push cells.
_GROWTH_MIN_WORK_MS = 3.0

# The floor must also scale with the baseline it is measured against (#413).
# Under load the baseline (interpreter start-up plus git calls) swung from 56
# to 84 ms between measurements, while the smaller cells did 14-30 ms of work,
# so a fixed 3 ms floor let scheduler noise read as growth: 8 of 10 runs
# failed under 12 CPU burners (load average 66-80), with ratios up to 11.6x on
# shapes that are plainly linear. A pair is compared only when its smaller
# cell's work is at least this fraction of the baseline measured beside it.
# A quadratic term is still caught once the SMALLER cell of a pair clears the
# floor; PR #415 shows planted terms failing the test. Below that, a quadratic
# can go unseen, and more so under load, because the floor rises with the
# baseline. The catastrophic case never finishes a cell at all.
_GROWTH_MIN_WORK_FRACTION = 0.5

# Linear work doubles when the input doubles, so an honest ratio sits near
# 2.0; quadratic work quadruples. Measured across all nine shapes on the fixed
# code, worst ratio 2.24. 3.0 sits between the two regimes with ~34% headroom
# over the measured worst case, and the quadratic it exists to catch does not
# arrive at 3.1 -- the three affected shapes do not finish at all at 128 KB.
_GROWTH_MAX_RATIO = 3.0


# A miss at twice the bound or more is a regression, not queueing noise.
_TIMING_GROSS_FACTOR = 2.0


class _TimingMiss(NamedTuple):
    """A timing check that missed: what to say, and how far over the bound.

    ``factor`` is measured / bound. A cell that never finished is infinite.
    """
    message: str
    factor: float


def _retry_timing_once(check):
    """Run a wall-clock check, and once more if it fails (#437).

    ``check()`` makes one full measurement and returns None when it is in
    bounds, or a ``_TimingMiss``. Under load a single sample can miss: on
    2026-10-21 the growth sweep read 4.40x against its 3.0x bound at a load
    average of ~20, and the stall gate 3.17 s against 3.0 s at ~30. Both
    passed when run alone. A real regression should miss on both runs, so it
    still fails; the bounds themselves do not change. The cost: a mild
    regression whose extra work is near the noise can pass one of two runs
    under heavy load, and one passing run is enough. CI, on a quieter
    runner, is the backstop.

    Returns None when either run passed, else one ``_TimingMiss`` with both
    messages and the smaller of the two factors.
    """
    first = check()
    if first is None:
        return None
    second = check()
    if second is None:
        return None
    return _TimingMiss(
        f"failed twice in a row:\n  1st: {first.message}\n  2nd: {second.message}",
        min(first.factor, second.factor))


def _fail_timing(miss):
    """Fail on a timing check that missed twice -- or skip, saying why, when
    the machine is overloaded, this is not CI, and the miss is marginal
    (#437).

    The retry alone did not hold: in the #437 branch's first preflight both
    runs of the growth sweep missed (5.30x, then 4.02x) at a load average of
    ~93 on 12 CPUs, and the same cell passed 3 of 3 when re-run straight
    after. Two back-to-back samples land in the same load spike. A load
    average above the CPU count means the wall clock is measuring the queue,
    not the hook. CI (GitHub Actions sets CI=true) keeps the strict failure,
    and so does any miss at ``_TIMING_GROSS_FACTOR`` times the bound or more.
    """
    if not os.environ.get("CI") and miss.factor < _TIMING_GROSS_FACTOR:
        try:
            load = os.getloadavg()[0]
        except OSError:
            load = None
        cpus = os.cpu_count()
        if load is not None and cpus and load > cpus:
            pytest.skip(f"timing missed twice at {miss.factor:.2f}x the bound, "
                        f"at load average {load:.1f} on {cpus} CPUs; CI runs "
                        f"this strictly. {miss.message}")
    pytest.fail(miss.message)


def _growth_gate(work_dir, env, unit, timeout_s):
    """The growth sweep for one input shape: measure, retry once, then fail
    (or skip under load, see ``_fail_timing``). Skips when it compared
    nothing. Lives at module level so a test can run it on a planted hook."""
    def measure(command):
        best = None
        for _ in range(_GROWTH_REPS):
            start = time.perf_counter()
            try:
                proc = subprocess.run(
                    [sys.executable,
                     str(work_dir / ".claude/hooks/prevent-direct-push.py")],
                    input=json.dumps({"tool_name": "Bash",
                                      "tool_input": {"command": command},
                                      "cwd": str(work_dir)}),
                    capture_output=True, text=True,
                    timeout=timeout_s, cwd=work_dir,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                # One slow rep under load is not the cell's best (#418): the
                # vendored hook takes ~1 s on some 900 KB shapes, so a rep
                # that waited behind other workers could hit the cap. The
                # cell is "did not finish" only when every rep did not.
                continue
            assert proc.returncode == 0, (
                f"prevent-direct-push exited {proc.returncode}: "
                f"{proc.stderr[-400:]!r}"
            )
            elapsed = (time.perf_counter() - start) * 1000
            best = elapsed if best is None else min(best, elapsed)
        return best

    # The passing run's cells, for the "compared nothing" skip below.
    passed = {}

    def check():
        works = []
        for kb in _GROWTH_SIZES_KB:
            # The fixed cost of getting in and out of the hook at all,
            # measured right before each cell rather than once, so the
            # load the cell sees moves its baseline too (#413).
            baseline = measure("git pu" + "sh origin feature/probe")
            assert baseline is not None, "the baseline command itself timed out"
            elapsed = measure(_matrix_command(f"{unit} @ {kb}KB"))
            if elapsed is None:
                return _TimingMiss(
                    f"prevent-direct-push did not finish {unit!r} at {kb}KB "
                    f"within {timeout_s}s -- superlinear "
                    f"growth over separator or quote density (the fixed "
                    f"code costs single-digit milliseconds of work here)",
                    float("inf"))
            works.append((kb, max(elapsed - baseline, 0.0), baseline))

        compared = 0
        for (kb_a, work_a, base_a), (kb_b, work_b, baseline) in zip(works, works[1:]):
            if work_a < max(_GROWTH_MIN_WORK_MS, _GROWTH_MIN_WORK_FRACTION * base_a):
                continue  # noise, not signal -- see _GROWTH_MIN_WORK_FRACTION
            compared += 1
            ratio = work_b / work_a
            if ratio >= _GROWTH_MAX_RATIO:
                return _TimingMiss(
                    f"prevent-direct-push work grew {ratio:.2f}x for {unit!r} "
                    f"when the input doubled from {kb_a}KB to {kb_b}KB "
                    f"({work_a:.1f}ms -> {work_b:.1f}ms over baselines of "
                    f"{base_a:.1f}ms and {baseline:.1f}ms), past the "
                    f"{_GROWTH_MAX_RATIO}x bound. Linear "
                    f"work doubles; quadratic work quadruples.",
                    ratio / _GROWTH_MAX_RATIO)
        passed.update(works=works, compared=compared)
        return None

    miss = _retry_timing_once(check)
    if miss is not None:
        _fail_timing(miss)
    works, compared = passed["works"], passed["compared"]
    if not compared:
        # Say so rather than pass having compared nothing: the smaller
        # cell of every pair was under the noise floor, so this run
        # measured no growth at all.
        pytest.skip(f"{unit!r}: the smaller cell of every pair was under the "
                    f"noise floor (kb, work ms, baseline ms): "
                    f"{[(kb, round(w, 1), round(bs, 1)) for kb, w, bs in works]}")


class TestRetryTimingOnce:
    """The retry around the two wall-clock tests (#437)."""

    @staticmethod
    def _check(*results):
        calls = []

        def check():
            calls.append(1)
            return results[len(calls) - 1]
        return check, calls

    def test_one_noisy_sample_then_a_clean_one_passes(self):
        check, calls = self._check(_TimingMiss("4.40x past the 3.0x bound", 1.47),
                                   None)
        assert _retry_timing_once(check) is None
        assert len(calls) == 2

    def test_a_clean_first_sample_is_not_measured_again(self):
        check, calls = self._check(None)
        assert _retry_timing_once(check) is None
        assert len(calls) == 1

    def test_a_miss_on_both_runs_fails_and_names_both(self):
        check, calls = self._check(_TimingMiss("slow 1", 3.0),
                                   _TimingMiss("slow 2", 1.5))
        miss = _retry_timing_once(check)
        assert miss is not None
        assert "slow 1" in miss.message and "slow 2" in miss.message
        assert miss.factor == 1.5, "the smaller factor decides the skip"
        assert len(calls) == 2

    @staticmethod
    def _load(monkeypatch, load, ci):
        monkeypatch.setattr(os, "cpu_count", lambda: 12)
        monkeypatch.setattr(os, "getloadavg", lambda: (load, load, load))
        if ci:
            monkeypatch.setenv("CI", "true")
        else:
            monkeypatch.delenv("CI", raising=False)

    @staticmethod
    def _outcome(miss):
        """("skip" | "fail", message). Both are caught here: a skip escaping
        pytest.raises(fail) reports the test as skipped, not failed."""
        try:
            _fail_timing(miss)
        except pytest.skip.Exception as e:
            return "skip", str(e)
        except pytest.fail.Exception as e:
            return "fail", str(e)
        return "returned", ""

    def test_a_marginal_double_miss_under_overload_skips_locally(self, monkeypatch):
        self._load(monkeypatch, 93.0, ci=False)
        kind, msg = self._outcome(_TimingMiss("slow twice", 1.4))
        assert kind == "skip" and "load average 93.0 on 12 CPUs" in msg, (kind, msg)
        assert "1.40x" in msg

    def test_a_gross_double_miss_fails_even_under_overload(self, monkeypatch):
        self._load(monkeypatch, 93.0, ci=False)
        assert self._outcome(_TimingMiss("slow twice", 3.0)) == ("fail", "slow twice")

    def test_a_never_finished_cell_fails_even_under_overload(self, monkeypatch):
        self._load(monkeypatch, 93.0, ci=False)
        assert self._outcome(
            _TimingMiss("slow twice", float("inf"))) == ("fail", "slow twice")

    def test_ci_fails_a_double_miss_even_under_overload(self, monkeypatch):
        self._load(monkeypatch, 93.0, ci=True)
        assert self._outcome(_TimingMiss("slow twice", 1.4)) == ("fail", "slow twice")

    def test_a_double_miss_at_normal_load_fails(self, monkeypatch):
        self._load(monkeypatch, 12.0, ci=False)
        assert self._outcome(_TimingMiss("slow twice", 1.4)) == ("fail", "slow twice")

    def test_an_unreadable_load_average_fails(self, monkeypatch):
        self._load(monkeypatch, 0.0, ci=False)

        def broken():
            raise OSError("no load average")
        monkeypatch.setattr(os, "getloadavg", broken)
        assert self._outcome(_TimingMiss("slow twice", 1.4)) == ("fail", "slow twice")


# Memo for the size-matched baselines, keyed by (hook, size in KB).
_SIZE_MATCHED_BASELINES = {}

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

    Budget: see SIZE_MATCHED_MULTIPLE and STALL_CEILING_MS -- the generated
    matrix is judged against an equally large benign command, and the
    hand-picked rows against a loose stall backstop. The 500ms figure below
    survives only as a floor, and was chosen from measurements on this
    machine (a 2026-era
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

    # Two nets with different jobs, because one number cannot do both.
    #
    # CI killed the obvious model twice. A flat 500 ms failed on a shared
    # runner. Scaling it by 12x a TINY-COMMAND baseline failed too, and the
    # second failure is the instructive one: the runner's baseline was
    # 39.4 ms against 39.4 ms here -- identical -- while the 900 KB cell took
    # 551.6 ms against 235.8 ms. The runner starts an interpreter at the same
    # speed and moves 900 KB far more slowly.
    #
    # The baseline and the cell were in different REGIMES. A tiny command is
    # dominated by interpreter start-up and a couple of git calls, which is
    # nearly machine-independent; a 900 KB command is dominated by memory and
    # I/O throughput, which is exactly where a shared runner is worst. A ratio
    # between two regimes measures the difference between the regimes, not the
    # code, so K measured here could never transfer.
    #
    # The generated matrix therefore compares each cell against the BENIGN
    # CELL OF THE SAME SIZE -- `no structure @ <same size>`, the same bytes
    # through the same hook with the adversarial property removed. The
    # assertion becomes "this shape costs no more than K x an equally large
    # ordinary command", which is a statement about the code. Measured, that
    # ratio is both tighter and stabler than the cross-regime one:
    #
    #                          worst ratio   worst ratio
    #                                 idle   under 12 burners on 12 cores
    #   vs a tiny command             6.06   --      (CI showed 14x)
    #   vs the same-size benign cell  2.89   2.38-2.57
    #
    # Under load the size-matched baseline rose from 81.5 ms to 121-133 ms and
    # the cell from 235.8 ms to 313-317 ms -- together, which is the property
    # the tiny baseline did not have. Four of the five hooks sit at ~1.0x.
    #
    # K = 10 is ~3.5x headroom over the worst measured 2.89. It catches a
    # constant-factor regression of ~3.5x or more in the span/separator
    # machinery -- the failure mode the growth-ratio assertion CANNOT see,
    # since a constant factor scales every size equally. Anything subtler is
    # that assertion's job, and it is the primary detector: it is
    # machine-independent by construction and stayed green through both CI
    # failures.
    SIZE_MATCHED_MULTIPLE = 10

    # A floor, so a cheap cell whose baseline is a few milliseconds cannot
    # produce a ceiling small enough to flake on scheduling noise.
    CEILING_FLOOR_MS = 500

    # The hand-picked ADVERSARIAL_INPUTS have no same-size benign partner --
    # they are arbitrary remembered shapes, not points on a size axis -- so
    # they keep a plain absolute bound, DEMOTED to what it can honestly claim:
    # a catastrophic-stall backstop, not a tuned budget. 2 s is "a human has
    # noticed and is wondering if it hung", not "this machine's fastest".
    # Regressions on these shapes are caught by the growth assertion and by
    # the size-matched matrix; this only catches a hang.
    STALL_CEILING_MS = 2000

    BASELINE_REPS = 3

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
    def _size_matched_baseline_ms(cls, work, env, hook, kb):
        """Cost of the BENIGN cell of the same size, through the same hook.

        Memoised per (hook, size): it is a property of the machine and the
        input size, not of the adversarial shape being judged, and measuring
        it per row would add hundreds of 900 KB runs to the suite.
        """
        key = (hook, kb)
        if key in _SIZE_MATCHED_BASELINES:
            return _SIZE_MATCHED_BASELINES[key]
        command = _matrix_command(f"no structure @ {kb}KB")
        best = None
        for _ in range(cls.BASELINE_REPS):
            start = time.perf_counter()
            proc = subprocess.run(
                [sys.executable, str(work / ".claude/hooks" / f"{hook}.py")],
                input=json.dumps({"tool_name": "Bash",
                                  "tool_input": {"command": command},
                                  "cwd": str(work)}),
                capture_output=True, text=True,
                timeout=cls.SUBPROCESS_TIMEOUT_S, cwd=work, env=env,
            )
            assert proc.returncode == 0, (
                f"{hook} size-matched baseline exited {proc.returncode}: "
                f"{proc.stderr[-400:]!r}"
            )
            elapsed = (time.perf_counter() - start) * 1000
            best = elapsed if best is None else min(best, elapsed)
        _SIZE_MATCHED_BASELINES[key] = best
        return best

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

    # Best of this many runs per measurement (#418). The vendored hooks are
    # several times slower than ours on 900 KB input (harden-repo #156 G-15),
    # so one sample taken while other workers load the machine missed the
    # budget on code that meets it. `min` is the least-contended run; a real
    # regression is slow on all three. The budgets themselves are unchanged.
    MEASURE_REPS = 3

    @classmethod
    def _best_ms(cls, work, env, hook, command, ceiling_ms):
        """Best wall time over up to MEASURE_REPS runs, in ms; None when every
        run hit the subprocess cap. Stops at the first run under
        *ceiling_ms*, since the best can only be lower. Every run that
        finished must exit 0.

        The cap sits one second above the ceiling, as SUBPROCESS_TIMEOUT_S
        says it should ("just above the budget"). The vendored hooks' benign
        900 KB baseline is ~0.3-0.65 s, so the size-matched ceiling is
        3-6.5 s, and a flat 2 s cap killed a 1.3 s shape that had queued
        behind other work at load average 42 on 12 CPUs (#418) before the
        ceiling could judge it. The ceiling, the assertion, is unchanged."""
        timeout_s = max(cls.SUBPROCESS_TIMEOUT_S, ceiling_ms / 1000 + 1)
        best = None
        for _ in range(cls.MEASURE_REPS):
            start = time.perf_counter()
            try:
                proc = subprocess.run(
                    [sys.executable, str(work / ".claude/hooks" / f"{hook}.py")],
                    input=json.dumps({"tool_name": "Bash",
                                      "tool_input": {"command": command},
                                      "cwd": str(work)}),
                    capture_output=True, text=True,
                    timeout=timeout_s, cwd=work, env=env,
                )
            except subprocess.TimeoutExpired:
                continue
            elapsed_ms = (time.perf_counter() - start) * 1000
            assert proc.returncode == 0, (
                f"{hook} exited {proc.returncode}: {proc.stderr[-400:]!r}"
            )
            best = elapsed_ms if best is None else min(best, elapsed_ms)
            if best < ceiling_ms:
                break
        return best

    @pytest.mark.parametrize("label", sorted(ADVERSARIAL_INPUTS))
    @pytest.mark.parametrize("hook", TestHookInputFailsClosed.HOOKS)
    def test_decision_within_budget(self, tmp_path, hook, label):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        ceiling = self.STALL_CEILING_MS
        command = self.ADVERSARIAL_INPUTS[label]
        elapsed_ms = self._best_ms(work, env, hook, command, ceiling)
        if elapsed_ms is None:
            pytest.fail(
                f"{hook} did not finish {label!r} in any of "
                f"{self.MEASURE_REPS} runs, well past the {ceiling:.0f}ms "
                f"ceiling (subprocess killed at the safety cap, 1 s past "
                f"the ceiling) -- likely "
                f"catastrophic backtracking or another superlinear blowup"
            )
        assert elapsed_ms < ceiling, (
            f"{hook} took {elapsed_ms:.1f}ms on {label!r}, over the "
            f"{ceiling:.0f}ms catastrophic-stall backstop -- this bound only "
            f"catches a hang; a smaller regression is the growth assertion's "
            f"job"
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
        kb = int(label.rpartition(" @ ")[2].removesuffix("KB"))
        baseline = self._size_matched_baseline_ms(work, env, hook, kb)
        ceiling = max(self.CEILING_FLOOR_MS,
                      self.SIZE_MATCHED_MULTIPLE * baseline)
        command = _matrix_command(label)
        elapsed_ms = self._best_ms(work, env, hook, command, ceiling)
        if elapsed_ms is None:
            pytest.fail(
                f"{hook} did not finish the generated shape {label!r} in any "
                f"of {self.MEASURE_REPS} runs, well past the {ceiling:.0f}ms "
                f"ceiling ({self.SIZE_MATCHED_MULTIPLE}x the {baseline:.1f}ms "
                f"cost of an equally large benign command; subprocess killed "
                f"at the safety cap, 1 s past the ceiling) -- a "
                f"superlinear blowup over separator or quote density"
            )
        assert elapsed_ms < ceiling, (
            f"{hook} took {elapsed_ms:.1f}ms on the generated shape "
            f"{label!r}, against a {baseline:.1f}ms baseline for an equally "
            f"large BENIGN command through the same hook -- "
            f"{elapsed_ms / baseline:.2f}x, over the "
            f"{self.SIZE_MATCHED_MULTIPLE}x bound (ceiling {ceiling:.0f}ms). "
            f"Both numbers are the same size on the same machine, so this is "
            f"the code, not the runner."
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
        _growth_gate(work_dir, env, unit, self.SUBPROCESS_TIMEOUT_S)

    def test_a_quadratic_hook_still_fails_the_growth_sweep(self, tmp_path,
                                                            monkeypatch):
        """The negative control on the growth sweep (#437): a planted
        quadratic term, run through ``_growth_gate`` as the real test does it,
        must fail. Deleting the failure call in ``_growth_gate`` turns this
        red."""
        work_dir, env = TestHookBlockingPathsFire._repo(tmp_path)
        hook = work_dir / ".claude/hooks/prevent-direct-push.py"
        src = hook.read_text(encoding="utf-8")
        # After `command` is assigned and normalised. PR #415 planted
        # command.count run len//1000 times, but that is only ~10-160 ms of
        # work per cell: at a load average of ~160 on 12 CPUs one of the two
        # runs passed on noise 3 times in 8. This control proves the gate's
        # failure path is wired, so it plants a term that dominates
        # (len//100, ~0.1-1.6 s). A mild quadratic under heavy load can still
        # pass one run, and one passing run is enough: that is the cost of
        # the retry (see _retry_timing_once).
        # The vendored hook's module-level line right after the size check.
        anchor = '_note_command(command, "git pu' + 'sh")'
        assert src.count(anchor) == 1, "the plant's anchor line moved"
        plant = '_ = [command.count("x") for _ in range(len(command) // 100)]'
        hook.write_text(src.replace(anchor, anchor + "\n" + plant),
                        encoding="utf-8")
        monkeypatch.setenv("CI", "true")
        with pytest.raises(pytest.fail.Exception):
            _growth_gate(work_dir, env, "bare separators",
                         self.SUBPROCESS_TIMEOUT_S)


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

    # A `cd` to an EXISTING directory outside the project stands every gate
    # down -- that is `_targets_this_project`'s whole job (#326). It must
    # exist: the vendored hooks treat a missing `cd` target as in scope,
    # since the `cd` fails and `&&` stops (harden-repo #156 G-11), so a
    # `/nonexistent-elsewhere` control could never allow.
    @staticmethod
    def _elsewhere(tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir(exist_ok=True)
        return f"cd {elsewhere} && "

    @pytest.mark.parametrize("bail", sorted(BAILS))
    @pytest.mark.parametrize("hook", sorted(HOOK_VERBS))
    def test_unvouched_cd_is_dropped_and_the_gate_still_applies(
        self, tmp_path, hook, bail
    ):
        exe, verb = self.HOOK_VERBS[hook]
        prefix, suffix = self.BAILS[bail]
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        elsewhere = self._elsewhere(tmp_path)

        # Controls, so a `deny` below is not just "this hook denies
        # everything": the bare command is gated, and the SAME `cd` stands
        # it down when the text ahead of it does parse.
        assert decide(work, env, hook, exe + verb) == "deny"
        assert decide(work, env, hook, elsewhere + exe + verb) == "allow"

        candidate = prefix + elsewhere + exe + verb + suffix
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

    # (token, what bash hands git). `_bare_ref`, the old hook's helper that
    # stripped a glued trailing separator, does not exist in the vendored
    # hook, so the claim is now driven through the hook itself: a separator
    # glued to a protected ref must not hide it (#351 round 4).
    GLUED_REF_TOKENS = (
        "ma" + "in",
        "ma" + "in;",
        "ma" + "in&",
        "ma" + "in|",
        "ma" + "in;;",
        '"refs/heads/ma' + 'in"',
        "+refs/heads/ma" + "in;",
        # Unquoted, bash splits this at the `;`: `main` is deleted, then
        # `x` runs.
        "ma" + "in;x",
    )

    @pytest.mark.parametrize("token", GLUED_REF_TOKENS)
    def test_a_glued_trailing_separator_does_not_hide_the_ref(
        self, tmp_path, token
    ):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        decide = TestHookBlockingPathsFire._decide
        hook = "prevent-direct-push"
        assert decide(work, env, hook, self.DELETE + token) == "deny", (
            f"{self.DELETE + token!r} deleted a protected branch"
        )
        # The control: the same glue on a release ref still stands down.
        release = token.replace("ma" + "in", "release/1.0.0")
        assert decide(work, env, hook, self.DELETE + release) == "allow", (
            f"{self.DELETE + release!r} was denied, so the deny above "
            f"proves nothing"
        )


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
                              "tool_input": {"command": command},
                              "cwd": str(work)}),
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
        command = f"cd {elsewhere} && {verb} && cd -"
        assert self._decide(work, env, hook, command) == "allow", (
            f"{hook} gated a command that ran in {elsewhere}; a cd AFTER the "
            f"verb ('cd -') must not decide where the verb ran"
        )
        # Narrower than our old hooks: the vendored ones accept only a
        # plain `&& cd -` after the verb, and refuse any other trailing
        # `cd` as "cannot tell which directory" (harden-repo #156 G-7 was
        # closed for `cd -` only). Refusing is the safe direction; pinned so
        # a change to it is noticed.
        command = f"cd {elsewhere} && {verb} && cd {elsewhere}"
        assert self._decide(work, env, hook, command) == "deny", (
            f"{hook} now allows a trailing `cd <dir>` after the verb; "
            f"update this row and the #418 notes"
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

        That is 3.10+ behaviour. Python 3.9's realpath does not raise: it
        returns the path with the NUL still in it (#371). Uncaught, the 3.10+
        error is a traceback and rc 1 — a NON-blocking error, so the gated
        command ran. An unresolvable target must read as IN scope.
        _decide() rejects any rc but 0 and 2, which is the assertion that
        matters here.
        """
        work, env, _ = probe
        command = "cd /tmp/\x00x && " + self.VERBS[hook]
        assert self._decide(work, env, hook, command) == "deny", (
            f"{hook} did not deny an unresolvable cd target"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_nul_byte_popped_by_dotdot_still_denies(self, probe, hook):
        """`/x/\\x00/..` must deny on every interpreter (#371).

        On 3.9, realpath returns "/x" for it: the `..` pops the NUL
        component, so a NUL check AFTER realpath never fires and the command
        reads as aimed elsewhere. The check has to run on the raw string.
        `<elsewhere>/\\x00/..` is the sharp case — without the NUL it is the
        exact target that test_legitimate_descoping_still_allows ALLOWS.
        """
        work, env, elsewhere = probe
        for command in (
            "cd /tmp/\x00/.. && " + self.VERBS[hook],
            f"cd {elsewhere}/\x00/.. && " + self.VERBS[hook],
        ):
            assert self._decide(work, env, hook, command) == "deny", (
                f"{hook} allowed a NUL-bearing cd target: {command!r}"
            )

    @staticmethod
    def _with_c(verb, path):
        """`git push ...` -> `git -C <path> push ...` (same for `gh`)."""
        exe, rest = verb.split(" ", 1)
        return f"{exe} -C {path} {rest}"

    GIT_HOOKS = ("require-preflight", "prevent-direct-push",
                 "validate-branch-name")
    GH_HOOKS = ("enforce-pr-base-branch", "update-changelog-before-pr")

    @pytest.mark.parametrize("hook", GIT_HOOKS)
    def test_nul_byte_in_dash_c_target_denies(self, probe, hook):
        """The `git -C <path>` branch needs the same NUL check as `cd` (#371).

        The control proves each shape reaches that branch: `-C <elsewhere>`
        must ALLOW, or the NUL cases would deny for some other reason and
        prove nothing. The gh hooks are in the next test: `gh` has no `-C`.
        """
        work, env, elsewhere = probe
        verb = self.VERBS[hook]
        control = self._with_c(verb, elsewhere)
        assert self._decide(work, env, hook, control) == "allow", (
            f"{hook} did not descope on {control!r}; the NUL cases cannot "
            f"discriminate"
        )
        for path in ("/tmp/\x00x", "/tmp/\x00/..", f"{elsewhere}/\x00/.."):
            command = self._with_c(verb, path)
            assert self._decide(work, env, hook, command) == "deny", (
                f"{hook} allowed a NUL-bearing -C target: {command!r}"
            )

    @pytest.mark.parametrize("hook", GH_HOOKS)
    def test_a_gh_dash_c_is_allowed_because_gh_rejects_it(self, probe, hook):
        """`gh -C <anything> pr create` exits on "unknown shorthand flag:
        'C'" before it does anything, so the vendored gh hooks allow it,
        NUL byte or not (harden-repo #156 G-14, a deliberate ruling; a NUL
        cannot reach gh through bash anyway). Pinned so the ruling is
        visible, with the plain form as the control."""
        work, env, elsewhere = probe
        verb = self.VERBS[hook]
        assert self._decide(work, env, hook, verb) == "deny"
        for path in (elsewhere, "/tmp/\x00x", "/tmp/\x00/..",
                     f"{elsewhere}/\x00/.."):
            command = self._with_c(verb, path)
            assert self._decide(work, env, hook, command) == "allow", (
                f"{hook} now denies {command!r}; update this row"
            )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_a_missing_cd_target_stays_in_scope(self, probe, hook):
        """harden-repo #156 G-11, kept by decision: a `cd` to a directory
        that does not exist FAILS, `&&` stops, and nothing ran elsewhere, so
        the vendored hooks gate it as if it ran here. "Cannot go there" is
        never "somewhere else". Our old hooks allowed it."""
        work, env, _ = probe
        command = "cd /nonexistent-elsewhere && " + self.VERBS[hook]
        assert self._decide(work, env, hook, command) == "deny", (
            f"{hook} treated a missing directory as another project"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_prefix_sibling_directory_is_out_of_scope(self, probe, hook):
        """DELIBERATE LOOSENING (#326 F3): `<project>-evil` is a different repo.

        A bare `startswith(project_dir)` read a sibling that merely shares a
        path prefix as inside this project, so these commands used to DENY.
        With the separator-anchored compare they ALLOW, which is correct
        scoping — and is the one place this change relaxes a gate rather than
        tightening it.

        The sibling is created: the vendored hooks gate a `cd` to a missing
        directory (see test_a_missing_cd_target_stays_in_scope), so a
        never-created sibling tested that rule, not the prefix compare.
        """
        work, env, _ = probe
        Path(f"{work}-evil").mkdir()
        command = f"cd {work}-evil && " + self.VERBS[hook]
        assert self._decide(work, env, hook, command) == "allow", (
            f"{hook} treated the sibling {work}-evil as part of this project"
        )

    # The marked blocks harden-repo keeps byte-identical across every hook
    # that carries them. Each line is `# --- BEGIN <name> (keep in sync ...`
    # / `# --- END <name> ---`.
    SHARED_BLOCKS = ("shared command scanner v1", "shared repo identity v1")

    @staticmethod
    def _shared_block(src, name):
        """The text from the BEGIN marker line of *name* to its END marker
        line, inclusive, or None when either marker is missing or repeated."""
        begin = f"# --- BEGIN {name} (keep in sync across all hook copies) ---\n"
        end = f"# --- END {name} ---\n"
        if src.count(begin) != 1 or src.count(end) != 1:
            return None
        start = src.index(begin)
        stop = src.index(end, start)
        return src[start:stop + len(end)]

    @pytest.mark.parametrize("block", SHARED_BLOCKS)
    def test_the_shared_blocks_are_identical_in_all_five_hooks(self, block):
        """The scanner and the repo-identity code (the scope guard's
        `_command_scope`, `_same_repo` and the rest) are copied into every
        hook, because /harden-repo installs each file standalone. Drift
        between the copies is how one gate keeps a bug the others were fixed
        for. Upstream marks each copy; this replaces our old check of the
        `_targets_this_project` call graph, which the vendored hooks no
        longer share (each hook's guard names its own verb)."""
        copies = {}
        for hook in self.HOOKS:
            src = (Path(".claude/hooks") / f"{hook}.py").read_text(encoding="utf-8")
            text = self._shared_block(src, block)
            assert text is not None, f"{hook}.py lacks one marked {block!r} block"
            # A near-empty block would make "identical" trivially true. The
            # repo-identity block is the smaller, at 4 functions.
            assert text.count("\ndef ") >= 4, f"{hook}.py's {block!r} block is empty"
            copies[hook] = text
        reference = self.HOOKS[0]
        for hook, text in copies.items():
            assert text == copies[reference], (
                f"{hook}.py's {block!r} block has drifted from {reference}.py's"
            )

    def test_the_block_compare_sees_a_one_byte_drift(self):
        """Negative control for the test above: the extractor must return
        the block's whole text, so a change anywhere in it is a difference,
        and a missing marker is reported rather than skipped."""
        src = Path(".claude/hooks/require-preflight.py").read_text(encoding="utf-8")
        for block in self.SHARED_BLOCKS:
            text = self._shared_block(src, block)
            middle = len(text) // 2
            drifted = text[:middle] + "#" + text[middle:]
            assert self._shared_block(src.replace(text, drifted), block) != text
            begin = f"# --- BEGIN {block}"
            assert self._shared_block(src.replace(begin, "# ---", 1), block) is None

class TestScopeGuardFailsClosedWhenScopeIsUnknowable:
    """The guard's fail-safe when it has no project to compare against.

    This class used to exec our old `_targets_this_project` out of the hook
    source and call it with a planted verb pattern, a broken regex and an
    `os` shim holding a NUL. The vendored hooks build that guard on their
    own scanner, so those calls no longer reach the code they named, and
    three of the four inputs cannot occur in real use (#418). The one claim
    a real call can reach is kept, through the hook: with no
    CLAUDE_PROJECT_DIR, a `cd` to another directory proves nothing, and the
    gate applies. False there would be an exit 0 with no decision, an
    ALLOW.
    """

    @pytest.mark.parametrize("hook", TestHookInputFailsClosed.HOOKS)
    def test_missing_project_dir_is_in_scope(self, tmp_path, hook):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        env = {k: v for k, v in env.items() if k != "CLAUDE_PROJECT_DIR"}
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        command = (f"cd {elsewhere} && "
                   + TestScopeGuardCannotBeBypassed.VERBS[hook])
        assert TestHookBlockingPathsFire._decide(
            work, env, hook, command) == "deny", (
            f"{hook} stood down with no project dir to compare against"
        )


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
        # The vendored hook does not model `--repo`'s value yet and DENIES
        # (harden-repo #156 G-10, moved to harden-repo #165). Refusing is
        # the safe direction; pinned so the fix in #165 turns this red and
        # the row goes back to allow.
        ("value flag leaves no refspec at all",
         f"{PUSH} --repo origin --delete {MAIN}", "deny"),
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
        # Our old hook refused every non-release delete while standing on
        # develop. The vendored hook judges only the refs pushed, on purpose
        # (harden-repo #156 G-21): deleting feature/x touches no protected
        # ref, so it is allowed. The two rows above are the deny controls.
        ("feature cleanup from develop is allowed",
         f"{PUSH} --delete origin feature/x", "allow"),
        ("a protected deletion from develop, no release ref",
         f"{PUSH} --delete origin develop", "deny"),
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
        self._stall_gate(work, env)

    def _stall_gate(self, work, env):
        """The stall check with its retry and failure, as the real test runs
        it. Split out so the negative control exercises the same wiring."""
        miss = _retry_timing_once(lambda: self._large_command_stall(work, env))
        if miss is not None:
            _fail_timing(miss)

    def _large_command_stall(self, work, env):
        """One timed run of the 900 KB command: None when it is under the
        3.0 s bound, else why not. The verdict is asserted on every run and
        never retried -- a fast ALLOW is a failure, not noise."""
        command = (f"{self.PUSH} --delete origin release/x "
                   + "b" * 900_000 + f" {self.MAIN}")
        started = time.monotonic()
        got = TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command)
        elapsed = time.monotonic() - started
        assert got == "deny", f"a 900 KB command must still deny, got {got}"
        if elapsed < 3.0:
            return None
        return _TimingMiss(
            f"the gate took {elapsed:.2f}s on a 900 KB command; the quadratic "
            f"shlex path is back (it measured 5.6s, the fixed path 0.04s)",
            elapsed / 3.0)

    def test_a_gate_that_really_stalls_still_fails(self, tmp_path, monkeypatch):
        """The negative control on the retry (#437): a hook with a planted
        3.5 s stall misses the 3.0 s bound on both runs, so the retry cannot
        turn a real regression into a pass. It goes through ``_stall_gate``,
        the same path the real test takes, so deleting the failure call there
        turns this red."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        hook = work / ".claude/hooks/prevent-direct-push.py"
        src = hook.read_text(encoding="utf-8")
        anchor = "input_data, _input_problem = _read_hook_input()"
        assert src.count(anchor) == 1, "the plant's anchor line moved"
        hook.write_text(src.replace(
            anchor, "import time as _t; _t.sleep(3.5)\n" + anchor),
            encoding="utf-8")
        monkeypatch.setenv("CI", "true")
        calls = []
        real = self._large_command_stall

        def counting(work, env):
            calls.append(1)
            return real(work, env)

        monkeypatch.setattr(self, "_large_command_stall", counting)
        with pytest.raises(pytest.fail.Exception, match="failed twice"):
            self._stall_gate(work, env)
        assert len(calls) == 2


# B1 fixture, generated at module level because a class-body comprehension
# cannot see other class attributes.
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
        # protected branch, so this must allow. The vendored hook does not
        # model `-o`'s value yet and DENIES it as a push of `main`
        # (harden-repo #156 G-10, moved to harden-repo #165). Refusing is the
        # safe direction; pinned so the #165 fix turns this red and the row
        # goes back to allow.
        ("a value flag whose value is spelt like a protected branch",
         f"{PUSH} -o {MAIN} origin feature/x", "deny"),
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
        # Our old hook refused any push from main that was not tags-only.
        # The vendored hook judges only the refs pushed (harden-repo #156
        # G-21, by decision): v1.2.3 and feature/x touch no protected ref.
        # The row above (a tag beside `main`) is the deny control that still
        # tells `all` from `any`.
        ("a tag ref beside an ordinary branch ref, from main",
         f"{PUSH} origin v1.2.3 feature/x", "allow"),
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
        # ---- git accepts unambiguous long-option ABBREVIATIONS ------------
        # `_all_ref_flags` was taught this for --mirror/--prune; `_is_delete`
        # was not, and it is the same fact one function up. With the
        # abbreviation `_is_delete` is False, so the deletion gate never
        # fires -- and on a release branch the Git Flow allowance exits 0
        # BEFORE the ref-token arm gets a look, so nothing catches it. On a
        # feature branch the ref-token arm still does, which is why every
        # probe anyone ran came back clean.
        #
        # `release/*` is the branch the release flow actually runs from, and
        # the consequence is an unrecoverable deletion of a protected branch.
        # Measured ALLOW here at 74cf1b1 and at b48f1b1; deny everywhere else.
        # `--del`, `--dele` and `--delet` all reach git's ref stage.
        ("abbreviated delete, flag first",
         f"{PUSH} --del origin {MAIN}", "deny"),
        ("abbreviated delete, longer prefix",
         f"{PUSH} --dele origin {MAIN}", "deny"),
        ("abbreviated delete, longest prefix",
         f"{PUSH} --delet origin {MAIN}", "deny"),
        ("abbreviated delete after the remote",
         f"{PUSH} origin --del {MAIN}", "deny"),
        ("abbreviated delete of develop",
         f"{PUSH} origin --dele develop", "deny"),
        # git rejects `--d` as ambiguous with `--dry-run`, so denying it costs
        # nothing -- and over-matching here only ever adds a deny.
        ("the ambiguous one-letter prefix",
         f"{PUSH} --d origin {MAIN}", "deny"),
        # ---- and the negative controls for the same change ---------------
        # `--dry-run` also begins with `d` and must NOT read as a delete;
        # "delete".startswith("dry-run") is False, which is what keeps it out.
        ("a dry run is not a deletion",
         f"{PUSH} --dry-run origin release/x", "allow"),
        # The case the allowlist exists for, in abbreviated form: the release
        # cleanup must still stand down once `_is_delete` recognises it.
        ("abbreviated release cleanup still stands down",
         f"{PUSH} --del origin release/x", "allow"),
        ("abbreviated hotfix cleanup still stands down",
         f"{PUSH} --delet origin hotfix/y", "allow"),
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
        # gate 3 would stand the hook down on it. The vendored hook reads the
        # newline as bash does: `git` runs, then a `push` command that does
        # not exist, so nothing is pushed and it allows (harden-repo #156
        # G-13). The row below is the deny control.
        ("the two matchers disagree across a newline",
         "git \npu" + "sh origin " + MAIN, "allow"),
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
        # bash drops a trailing backslash at the end of the command and
        # pushes feature/x, but the vendored hook cannot parse it and
        # refuses ("could not parse this command"). Not in harden-repo #156;
        # an over-deny in the safe direction, pinned and reported (#418).
        ("a feature branch wearing a backslash",
         f"{PUSH} origin feature/x" + chr(92), "deny"),
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

    # ---- BH-1: an interior backslash escape ------------------------------
    # `_push_invocations` falls back to `str.split` whenever the tail holds no
    # quote character, and `str.split` does not remove backslash escapes. So
    # `ma\\in` -- which bash hands to git as the protected branch -- reached
    # every ref-token gate spelt with the backslash still in it and matched
    # nothing. One backslash turned off `_protected_push_refs`,
    # `_protected_delete_refs`, `_all_ref_flags` and `_branch_glob_refspecs`
    # at once, i.e. most of what this PR adds. Measured ALLOW at 74cf1b1 and
    # at b159a2b, `bash -n` clean, and
    # `bash -c 'printf "[%s]" origin --delete ma\\in'` really prints
    # `[origin][--delete][main]`.
    #
    # De-escaping happens at the one point the fallback tokens are built, not
    # in each consumer -- that asymmetry (quoting normalised in one place,
    # escaping in none) is what allowed this.
    ESCAPE = chr(92)
    BACKSLASH_CASES = (
        ("an escaped protected ref, deleted",
         f"{PUSH} origin --delete {MAIN[:2]}{ESCAPE}{MAIN[2:]}", "deny"),
        ("an escaped protected ref, pushed",
         f"{PUSH} origin {MAIN[:2]}{ESCAPE}{MAIN[2:]}", "deny"),
        ("an escaped protected ref, qualified",
         f"{PUSH} origin refs/heads/{MAIN[:2]}{ESCAPE}{MAIN[2:]}", "deny"),
        ("an escaped protected ref, as a refspec destination",
         f"{PUSH} origin HEAD:{MAIN[:2]}{ESCAPE}{MAIN[2:]}", "deny"),
        ("an escaped mirror flag",
         f"{PUSH} --mirr{ESCAPE}or origin", "deny"),
        ("an escaped all-branches flag",
         f"{PUSH} --a{ESCAPE}ll origin", "deny"),
        ("an escaped delete flag",
         f"{PUSH} origin --dele{ESCAPE}te {MAIN}", "deny"),
        # The control that located the bug: add ANY quote character and the
        # command routes through `shlex.split`, which de-escapes correctly --
        # so this row denied even before the fix, and its bare twin did not.
        ("the same command with a quote character in it",
         f'{PUSH} origin --delete {MAIN[:2]}{ESCAPE}{MAIN[2:]} ""', "deny"),
        # ---- and the negative controls -----------------------------------
        # De-escaping must not invent a protected ref where there is none.
        ("an escaped mainline branch",
         f"{PUSH} origin {MAIN[:2]}{ESCAPE}{MAIN[2:]}line", "allow"),
        ("an escaped branch whose leaf looks protected",
         f"{PUSH} origin feature/{MAIN[:2]}{ESCAPE}{MAIN[2:]}", "allow"),
        ("an escaped ordinary branch",
         f"{PUSH} origin featu{ESCAPE}re/x", "allow"),
        # A refspec INTO a tag is not a branch push, escaped or not.
        ("an escaped protected ref as the refspec SOURCE",
         f"{PUSH} origin {MAIN[:2]}{ESCAPE}{MAIN[2:]}:refs/tags/v1", "allow"),
    )

    @pytest.mark.parametrize(
        "label,command,expected",
        BACKSLASH_CASES,
        ids=[c[0].replace(" ", "-") for c in BACKSLASH_CASES],
    )
    def test_backslash_escapes_do_not_hide_a_ref_or_flag(
            self, tmp_path, label, command, expected):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        got = TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command)
        assert got == expected, f"{label}: expected {expected}, got {got}"

    # ---- BH-2: the two matchers contradicting each other -----------------
    # The entry test matches a push verb; `_push_invocations` finds none; every
    # gate is then a loop over an empty list and the command sails through.
    # Reachable from valid bash: an apostrophe in a trailing `#` comment never
    # closes, so `_quoted_spans` returns None and the split falls back to
    # every separator -- including a `|` inside a quoted `-c` value, which
    # tears the verb in half. Measured ALLOW at 74cf1b1 and at b159a2b.
    #
    # The gate now denies on the contradiction itself, the same way
    # `_targets_this_project` gates when its verb search disagrees with its
    # caller's.
    PAGER = 'core.pager="less|cat"'
    CONTRADICTION_CASES = (
        ("a protected deletion hidden by a comment apostrophe",
         f"git -c {PAGER} pu" + f"sh origin --delete {MAIN} # it's fine",
         "deny"),
        # Controls isolating the cause: remove EITHER ingredient and the verb
        # match survives, so the command is judged normally.
        ("the same command with no comment",
         f"git -c {PAGER} pu" + f"sh origin --delete {MAIN}", "deny"),
        ("the same comment with no quoted separator",
         f"{PUSH} origin --delete {MAIN} # it's fine", "deny"),
        ("a benign push with a comment apostrophe",
         f"{PUSH} origin feature/x # it's fine", "allow"),
        ("a benign push with a comment and an UNquoted separator",
         f"{PUSH} origin feature/x && echo done # it's fine", "allow"),
        ("a benign push with a quoted separator and no comment",
         f"git -c {PAGER} pu" + "sh origin feature/x", "allow"),
        ("a benign push with a comment and a quoted value holding no separator",
         'git -c core.pager="lesscat" pu' + "sh origin feature/x # it's fine",
         "allow"),
        # Our old hook refused this legitimate feature-branch push because
        # it carried both ingredients at once, a cost pinned here. The
        # vendored hook reads `#` comments (harden-repo #153, #156 G-6), so it
        # pushes feature/x and allows; the first row, the same command
        # deleting a protected branch, still denies.
        ("a legitimate push carrying BOTH ingredients is allowed",
         f"git -c {PAGER} pu" + "sh origin feature/x # it's fine", "allow"),
    )

    @pytest.mark.parametrize(
        "label,command,expected",
        CONTRADICTION_CASES,
        ids=[c[0].replace(" ", "-") for c in CONTRADICTION_CASES],
    )
    def test_matcher_contradiction_denies(self, tmp_path, label, command,
                                          expected):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        got = TestHookBlockingPathsFire._decide(
            work, env, "prevent-direct-push", command)
        assert got == expected, f"{label}: expected {expected}, got {got}"


_BASH = shutil.which("bash")
_REQUIRES_BASH = pytest.mark.skipif(_BASH is None, reason="bash not available")


@_REQUIRES_BASH
class TestBashTruthDifferential:
    """#334: bash decides what runs; the gate only gets to agree.

    Ported from the #327 review branch (PR #331, not merged), where it found
    every root cause in one pass. The hand-written fixture tables in this file
    were 3035 tests green while 16 of 55 constructs that bash really executes
    got past the push gate. The census of all 165 fixtures showed why: none
    contained a bare newline, a redirection, a heredoc, a pipe or a tab. The
    tables and the matcher came from one mental model, so the tests could only
    confirm it.

    So this class does not assert a verdict list. It builds constructs by
    combination, asks BASH whether the verb really executed, and requires a
    deny for every construct that did. A construct bash does not execute is
    skipped rather than asserted on: this is a bypass hunt, and a fail-closed
    deny on inert text is not a bypass. The hand-written classes above stay as
    regression pins for specific past bugs.

    The marker is a FILESYSTEM side-effect, never stdout. Half these
    constructs run the verb inside `$( )` or backticks, where stdout is
    captured by the substitution and never reaches the probe. A stdout marker
    reports "did not run" for exactly the constructs that hide a live verb,
    which is the wrong answer in the fail-open direction.

    Deterministic on purpose: a fixed prefix/suffix/wrapper product, not random
    generation, so a failure names a construct that can be pasted into a shell,
    and a green run means the same thing tomorrow. Adding a construct to the
    tables below is the only maintenance.

    Templates are assembled with `VERB` substituted late, for the same reason
    as the rest of this file: the live PreToolUse hooks read unexecuted
    command text.
    """

    PREFIX = ("", "true; ", "true && ", "true || ", "false || ", "echo hi\n",
              "echo hi   \n", "# c\n", "x=1 ", "{ ", ">/dev/null ", "2>&1 ",
              "1>&2 ", "&>out ", "3>&1 ", "</dev/null ", "if true; then ",
              "for i in 1; do ", "while false; do : ; done; ", "! ", "time ",
              "sudo ", "nice -n 10 ", "command ", "eval ", "\\", "(", "$(", "`",
              "true | ", "echo hi |\n", "true & ", "\t", "true;\t", "\n",
              "echo $'a' && ", "echo ${HOME} && ", "echo `:` && ",
              "# don't\n", "# don't\n# it's\n", '# say "hi"\n',
              "cat <<EOF\ndon't\nEOF\n", "cat <<'EOF'\ndon't\nEOF\n",
              "cat <<-EOF\n\tx\n\tEOF\n", "cat <<<x && ", "A+=1 ", "A[0]=1 ",
              "caffeinate ", "arch ", "arch -arm64 ", "script -q /dev/null ")
    SUFFIX = ("", ";", " ; }", " ; done", " ; fi", ")", "`", " | cat",
              " 2>/dev/null", " >out", " </dev/null", " &", "\t", "\n",
              " ${FORCE}", " `:`", " $(true)", "\n# it's fine",
              " # don't", "\ncat <<EOF\ndon't\nEOF")
    WRAPPERS = ("%s", "`%s`", "$(%s)", "( %s )", "{ %s ; }")
    WRAP_PREFIX = ("", "true; ", "true && ", "echo hi\n", "x=1 ", ">/dev/null ",
                   "2>&1 ", "true | ", "\t")

    # Constructs the executed corpus must keep containing. Each is a shape the
    # #327 census found ZERO fixtures for, plus the two that had one each.
    REQUIRED = {
        "bare newline": lambda t: "\n" in t,
        "redirection": lambda t: bool(re.search(r"(?<![<])[<>](?![<(])", t)),
        "heredoc": lambda t: "<<" in t,
        "single pipe": lambda t: bool(re.search(r"(?<!\|)\|(?!\|)", t)),
        "||": lambda t: "||" in t,
        "background &": lambda t: bool(re.search(r"(?<![&>])&(?![&>])", t)),
        "tab": lambda t: "\t" in t,
        "$'...'": lambda t: "$'" in t,
        "${VAR}": lambda t: "${" in t,
        "backtick": lambda t: "`" in t,
        "$( )": lambda t: "$(" in t,
    }

    @classmethod
    def _templates(cls, prefix=None, suffix=None, wrappers=None, wrap_prefix=None):
        """The deterministic construct set, VERB still a placeholder."""
        out = set()
        for p in cls.PREFIX if prefix is None else prefix:
            for s in cls.SUFFIX if suffix is None else suffix:
                out.add(p + "VERB" + s)
        for w in cls.WRAPPERS if wrappers is None else wrappers:
            for p in cls.WRAP_PREFIX if wrap_prefix is None else wrap_prefix:
                out.add(p + w % "VERB")
        return sorted(out)

    @classmethod
    def _missing_constructs(cls, templates):
        """Names of REQUIRED constructs that no template contains."""
        return sorted(name for name, has in cls.REQUIRED.items()
                      if not any(has(t) for t in templates))

    @staticmethod
    def _bash_runs_it(work, template, marker_name="FUZZ_RAN"):
        """Did bash EXECUTE the verb? Marker is a file, not stdout. Each
        concurrent probe needs its own marker_name."""
        marker = work / marker_name
        if marker.exists():
            marker.unlink()
        # sudo reads its password from /dev/tty, not stdin, so stdin=DEVNULL
        # alone still prompted on a developer's terminal and blocked for the
        # full timeout. A new session has no controlling tty, so sudo fails
        # fast; and on timeout the whole group is killed, because a timed-out
        # `sudo VERB | cat` otherwise left `sudo touch ...` orphaned.
        proc = subprocess.Popen(
            [_BASH, "-c", template.replace("VERB", f"touch {marker}")],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=work, start_new_session=True,
        )
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                pass
            proc.communicate()
            return False
        # A backgrounded verb (`VERB &`) may still be running when bash
        # returns; give it a moment rather than reading "did not run".
        for _ in range(50):
            if marker.exists():
                break
            if "&" not in template:
                break
            time.sleep(0.01)
        ran = marker.exists()
        if ran:
            marker.unlink()
        return ran

    # Probes and hook calls are independent subprocesses, so they run in a
    # pool; serially the product took ~80s. Each bash probe gets its own
    # scratch dir so a `>out` or `&>out` in one cannot race another.
    _WORKERS = 8

    def _executed(self, work):
        templates = self._templates()

        def probe(item):
            i, t = item
            d = work / f"bash-{i}"
            d.mkdir(parents=True, exist_ok=True)
            return t if self._bash_runs_it(d, t) else None

        with ThreadPoolExecutor(self._WORKERS) as pool:
            return [t for t in pool.map(probe, enumerate(templates)) if t]

    # (hook, verb) — one gated verb per command gate. Each gate's verb is
    # denied from a feature branch in a throwaway repo, so any construct that
    # bash executes must come back "deny". Fragments, see the class docstring.
    GATES = (
        ("prevent-direct-push", "git pu" + "sh origin ma" + "in"),
        ("require-preflight", "git com" + "mit -m wip"),
        ("validate-branch-name", "git chec" + "kout -b nonsense-branch"),
        ("enforce-pr-base-branch", "gh pr cre" + "ate --base ma" + "in"),
    )

    @pytest.fixture(scope="class")
    def executed(self, tmp_path_factory):
        """The constructs bash executes. Verb-independent (the probe runs
        `touch`), so it is computed once and shared by every gate."""
        root = tmp_path_factory.mktemp("bash-truth")
        found = self._executed(root / "probe")
        # The oracle has to keep finding live constructs, or every gate test
        # passes by testing nothing. 663 executed when this was written.
        assert len(found) >= 500, (
            f"only {len(found)} constructs executed; the bash oracle is "
            "broken, not the gate"
        )
        assert not self._missing_constructs(found), (
            "the EXECUTED corpus lost constructs: "
            f"{self._missing_constructs(found)}"
        )
        if sys.platform == "darwin":
            for wrapper in ("caffeinate ", "arch ", "script -q /dev/null "):
                assert any(t.startswith(wrapper + "VERB") or
                           t.startswith(wrapper) for t in found), (
                    f"no construct wrapped in {wrapper.strip()!r} executed on "
                    "macOS, where it exists; that wrapper is no longer tested"
                )
        return found

    @pytest.mark.parametrize("hook,verb", GATES, ids=[g for g, _ in GATES])
    def test_every_construct_bash_executes_is_denied(self, tmp_path, executed, hook, verb):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)

        def allowed(t):
            return TestHookBlockingPathsFire._decide(
                work, env, hook, t.replace("VERB", verb)) != "deny"

        with ThreadPoolExecutor(self._WORKERS) as pool:
            verdicts = list(pool.map(allowed, executed))
        bypasses = [t for t, bad in zip(executed, verdicts) if bad]
        assert not bypasses, (
            f"{hook}: {len(bypasses)} of {len(executed)} constructs run the "
            "verb and the gate allowed them:\n  "
            + "\n  ".join(repr(t) for t in bypasses[:40])
        )

    @pytest.mark.parametrize("template", [
        "VERB", "echo hi\nVERB", "\tVERB", "x=1 VERB", "$(VERB)", "`VERB`",
        "{ VERB ; }", "( VERB )", "true | VERB",
    ])
    def test_pr_merge_is_judged_in_every_command_position(self, tmp_path, template):
        """#334: `gh pr merge` shared the PR-create trigger's anchoring, so a
        merge after a newline, a tab, `x=1 ` or inside `$( )` skipped the base
        check. With `gh` off PATH the check cannot verify the PR and must
        deny; before the fix these constructs never reached it and allowed.
        Not in the oracle above because each call would shell out to `gh`."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "git").symlink_to(shutil.which("git"))
        env = dict(env, PATH=str(bin_dir))
        verb = "gh pr mer" + "ge 5 --squash"
        assert TestHookBlockingPathsFire._decide(
            work, env, "enforce-pr-base-branch", template.replace("VERB", verb)
        ) == "deny"

    @staticmethod
    def _fake_gh(tmp_path, bases):
        """A `gh` on PATH that speaks the JSON the vendored hook parses
        (harden-repo #156 G-31): `gh pr view --json number` names PR 1 as the
        current branch's, and `gh pr view N --json baseRefName,headRefName`
        answers from `bases` ({"N": "base head"}), for N or a pull URL
        ending in N. Any other PR fails, which the hook must read as "cannot
        verify"."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "git").symlink_to(shutil.which("git"))
        lines = ["#!/bin/sh", 'case "$3" in',
                 """  --json) echo '{"number": 1}' ;;"""]
        for num, answer in bases.items():
            base, head = answer.split()
            data = json.dumps({"baseRefName": base, "headRefName": head})
            # The hook hands gh a pull URL as it was written; gh accepts it.
            lines.append(f"  {num}|*/pull/{num}) echo '{data}' ;;")
        lines += ["  *) exit 1 ;;", "esac"]
        gh = bin_dir / "gh"
        gh.write_text("\n".join(lines) + "\n")
        gh.chmod(0o755)
        return str(bin_dir)

    CREATE = "gh pr cre" + "ate"
    MERGE = "gh pr mer" + "ge"

    @pytest.mark.parametrize("command,expected", [
        # A mention placed first must not speak for the real occurrence.
        ("echo CREATE --base develop\nCREATE --base main", "deny"),
        ("# CREATE --base develop\nCREATE --base main", "deny"),
        # Every base spelling must be develop: which one gh keeps is not
        # guessed (a value another flag swallows, or a later override).
        ("CREATE --base develop --title x --base main", "deny"),
        ("CREATE --base main --title x --base develop", "deny"),
        ("CREATE --base develop -Bmain", "deny"),
        ("CREATE --base develop -B=main", "deny"),
        ("CREATE -dB main", "deny"),
        ("CREATE -B main -t --base=develop", "deny"),
        ("CREATE -Bdevelop --title x", "allow"),
        # Positive controls: a well-based create stays allowed.
        ("CREATE --base develop", "allow"),
        ("CREATE --base=develop --title x", "allow"),
        # -B is gh's short form of --base.
        ("CREATE -B main", "deny"),
        ("CREATE -B develop", "allow"),
        # A separator inside a quoted --body is body text, not a command end.
        ("CREATE --body 'a; b (c)' --base develop", "allow"),
        ("CREATE --body 'a; b (c)' --base main", "deny"),
        ("echo CREATE --base develop\nCREATE --base develop", "allow"),
    ])
    def test_each_create_occurrence_is_judged_on_its_own_base(
            self, tmp_path, command, expected):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        cmd = command.replace("CREATE", self.CREATE)
        assert TestHookBlockingPathsFire._decide(
            work, env, "enforce-pr-base-branch", cmd) == expected, cmd

    @pytest.mark.parametrize("command,expected", [
        # A create mention before a merge used to exit the gate before the
        # merge was ever judged.
        # (Separators chosen so the old first-match read "develop" cleanly;
        # with `develop;` or `develop'` it denied for an unrelated reason.)
        ("echo CREATE --base develop\nMERGE 2 --squash", "deny"),
        ("MERGE 2 --squash # CREATE --base develop", "deny"),
        # Each merge is checked against its own PR, not the first number.
        ("echo MERGE 1; MERGE 2 --squash", "deny"),
        ("MERGE 1 --squash\nMERGE 2 --squash", "deny"),
        # A quoted `;` or `)` in --subject/--body, a pull URL, or a flag
        # value that looks like a number must not hide the PR being merged
        # and send the check to the current branch's PR (1) instead.
        ("MERGE --squash --subject 'x; y' 2", "deny"),
        ("MERGE --squash --body 'see (a) b' 2", "deny"),
        ("MERGE https://github.com/o/r/pull/2 --squash", "deny"),
        ("MERGE -t 5 2 --squash", "deny"),
        ("MERGE '#2' --squash", "deny"),
        # Stacked short flags: pflag reads -st 5 as -s -t 5, so gh merges 2.
        ("MERGE -st 5 2", "deny"),
        ("MERGE -sb 5 2", "deny"),
        # A `#` inside a word is not a comment; the selector after it counts.
        ("MERGE -t a#b 2", "deny"),
        # A selector computed at run time cannot be checked.
        ("MERGE `echo 2`", "deny"),
        ("MERGE $(echo 2)", "deny"),
        # Unbalanced quotes cannot be parsed: fail closed.
        ("MERGE 1 --body 'oops", "deny"),
        # Positive controls: a feature PR based on develop may merge.
        ("MERGE 1 --squash", "allow"),
        ("MERGE --squash", "allow"),
        ("MERGE -t 'a; b' 1 --squash", "allow"),
        ("MERGE https://github.com/o/r/pull/1", "allow"),
        ("MERGE -st 5 1", "allow"),
        ("MERGE 1 # note", "allow"),
        ("echo CREATE --base develop; MERGE 1 --squash", "allow"),
    ])
    def test_each_merge_occurrence_is_judged_on_its_own_pr(
            self, tmp_path, command, expected):
        """PR 1 (the current branch's) and PR 5 are feature -> develop
        (fine); PR 2 is feature -> main. PR 5 is valid so that `-t 5` reading
        as the PR would ALLOW, not deny for an unrelated reason."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        env = dict(env, PATH=self._fake_gh(tmp_path, {
            "1": "develop feature/a", "2": "main feature/b",
            "5": "develop feature/c"}))
        cmd = command.replace("CREATE", self.CREATE).replace("MERGE", self.MERGE)
        assert TestHookBlockingPathsFire._decide(
            work, env, "enforce-pr-base-branch", cmd) == expected, cmd

    @pytest.mark.parametrize("head,base,expected", [
        # The release PR and the back-merge PR are both Git Flow steps.
        ("release/3.8.0", "main", "allow"),
        ("release/3.8.0", "develop", "allow"),
        ("hotfix/3.8.1", "main", "allow"),
        ("hotfix/3.8.1", "develop", "allow"),
        # Any other base is still wrong.
        ("release/3.8.0", "feature/x", "deny"),
        ("hotfix/3.8.1", "release/3.8.0", "deny"),
        # A feature PR still may not merge to main.
        ("feature/x", "main", "deny"),
    ])
    def test_release_and_hotfix_may_back_merge_to_develop(
            self, tmp_path, head, base, expected):
        """A release or hotfix lands on main and is then merged back into
        develop through a second PR. The gate denied that second PR, so every
        back-merge had to bypass it. Matches git-flow's check-pr-base hook,
        which allows main or develop for these branches."""
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        env = dict(env, PATH=self._fake_gh(tmp_path, {"7": f"{base} {head}"}))
        assert TestHookBlockingPathsFire._decide(
            work, env, "enforce-pr-base-branch", self.MERGE + " 7 --merge"
        ) == expected

    def test_the_census_fails_when_the_corpus_is_narrowed(self):
        """The alphabet guard must catch a corpus narrowed back to the #327
        shape: single-line commands joined only by `;` or `&&`."""
        narrowed = self._templates(prefix=("", "true; ", "true && "),
                                   suffix=("", ";"), wrappers=("%s",),
                                   wrap_prefix=("",))
        missing = self._missing_constructs(narrowed)
        for name in ("bare newline", "redirection", "heredoc", "single pipe",
                     "tab", "background &"):
            assert name in missing, (name, missing)
        assert self._missing_constructs(self._templates()) == []

    def test_the_census_detectors_are_not_fooled_by_lookalikes(self):
        """`||`, `&&`, `&>`, `2>&1` and `<<` must not count as a pipe,
        background, or plain redirection on their own."""
        req = self.REQUIRED
        assert not req["single pipe"]("true || VERB")
        assert not req["background &"]("true && VERB")
        assert not req["background &"]("&>out VERB")
        assert not req["background &"]("VERB 2>&1")
        assert not req["redirection"]("cat <<EOF\nx\nEOF\nVERB")
        assert req["redirection"]("VERB >out")
        assert req["background &"]("VERB &")
        assert req["single pipe"]("VERB | cat")




class TestPreflightTokenSurvivesADeniedCall:
    """#408: the token was deleted the moment require-preflight approved a
    commit. Another PreToolUse hook could still deny the same Bash call, so
    the command never ran but the token was gone.

    The vendored hook (harden-repo #128, #166, vendored in #418) binds the
    token to the HEAD, the staged tree and the checkout preflight ran in.
    A commit the hook can read exactly, in that checkout, keeps the token
    and stamps ``claimed_at``; a second commit within CLAIM_HOLD_SECONDS is
    refused, so two racing calls cannot both use it (#414). Once HEAD or the
    staged tree moves, the token is spent. A call that can change the index
    itself, or runs in another checkout, is refused and the token kept.
    """

    C = "com" + "mit"
    REG = "git " + C + " -m wip"
    AMEND = "git " + C + " --amend --no-edit"
    CLAIM_HOLD = _vendored_hook_constant("require-preflight", "CLAIM_HOLD_SECONDS")

    @staticmethod
    def _token_path(work):
        """`/tmp/.preflight-token-<md5[:8]>`, keyed on the realpath of
        `git rev-parse --path-format=absolute --git-common-dir`, as both
        require-preflight.py's _get_token_path() and commit-preflight.sh
        compute it."""
        import hashlib
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX"))}
        common = subprocess.run(
            ["git", "-C", str(work), "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            env=env, check=True, capture_output=True, text=True).stdout.strip()
        h = hashlib.md5(os.path.realpath(common).encode()).hexdigest()[:8]
        return Path(f"/tmp/.preflight-token-{h}")

    @staticmethod
    def _git(work, env, *args):
        return subprocess.run(
            ["git", "-C", str(work), "-c", "user.email=t@example.invalid",
             "-c", "user.name=t", *args],
            env=env, check=True, capture_output=True, text=True).stdout.strip()

    @pytest.fixture
    def repo(self, tmp_path):
        work, env = TestHookBlockingPathsFire._repo(tmp_path)
        token = self._token_path(work)
        yield work, env, token
        token.unlink(missing_ok=True)
        Path(f"{token}.lock").unlink(missing_ok=True)

    def _write_token(self, work, env, token, bound=True, **override):
        """A token as the vendored preflight writes it: created, expires,
        staged_files, checks_run, plus the binding (head, tree, worktree)
        read from the repo now. ``bound=False`` leaves the binding out (an
        old-format token); ``override`` replaces or adds fields, and a value
        of None removes one."""
        now = int(time.time())
        data = {"created": now, "expires": now + 300, "staged_files": 1,
                "checks_run": "tests"}
        if bound:
            data["head"] = self._git(work, env, "rev-parse", "HEAD")
            data["tree"] = self._git(work, env, "write-tree")
            data["worktree"] = os.path.realpath(
                self._git(work, env, "rev-parse", "--show-toplevel"))
        data.update(override)
        data = {k: v for k, v in data.items() if v is not None}
        token.write_text(json.dumps(data))

    def _release_claim(self, token):
        """Back-date ``claimed_at`` past the hold, as if CLAIM_HOLD_SECONDS
        had gone by, rather than sleeping."""
        data = json.loads(token.read_text())
        if "claimed_at" in data:
            data["claimed_at"] = int(time.time()) - self.CLAIM_HOLD - 1
            token.write_text(json.dumps(data))

    def _decide(self, work, env, command, cwd="project"):
        """The decision only; see _decide_with_reason."""
        return self._decide_with_reason(work, env, command, cwd)[0]

    def _decide_with_reason(self, work, env, command, cwd="project"):
        """Run the hook with Claude Code's payload, including `cwd` (the
        shell's directory). cwd="project" means the project root; None
        leaves the key out. Returns (decision, reason)."""
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        if cwd == "project":
            payload["cwd"] = str(work)
        elif cwd is not None:
            payload["cwd"] = str(cwd)
        proc = subprocess.run(
            [sys.executable, str(work / ".claude/hooks/require-preflight.py")],
            input=json.dumps(payload), capture_output=True, text=True,
            timeout=60, cwd=work, env=env,
        )
        # Exit 1 is a NON-blocking error: the command would run.
        assert proc.returncode == 0, proc.stderr[-400:]
        try:
            out = json.loads(proc.stdout)["hookSpecificOutput"]
            return (out.get("permissionDecision", "allow"),
                    out.get("permissionDecisionReason", ""))
        except (ValueError, KeyError, TypeError, AttributeError):
            return "allow", ""

    def _worktree(self, work, env):
        wt = work / ".claude" / "worktrees" / "wt"
        self._git(work, env, "worktree", "add", "-q", "-b", "feature/wt", str(wt))
        return wt

    # --- the #408 behaviour -------------------------------------------------

    def test_a_call_that_never_ran_leaves_the_token_usable(self, repo):
        """Approved, then denied by another hook: nothing ran, HEAD and the
        staged tree are unchanged, so the token is still good -- after the
        claim hold (harden-repo #156 G-18). Inside the hold a second commit is
        refused, which is what stops two racing calls from both using one
        kept token (#414); the refusal keeps the token."""
        work, env, token = repo
        self._write_token(work, env, token)
        assert self._decide(work, env, self.REG) == "allow"
        assert token.exists()
        assert "claimed_at" in json.loads(token.read_text())

        decision, reason = self._decide_with_reason(work, env, self.REG)
        assert decision == "deny"
        assert "Another call is committing with this preflight token" in reason
        assert token.exists()

        self._release_claim(token)
        assert self._decide(work, env, self.REG) == "allow"
        assert token.exists()

    def test_a_real_commit_spends_the_token(self, repo):
        """The vendored hook keeps the token on approval (harden-repo #156
        G-19, by decision): a sibling hook may still deny the call. The
        binding is what makes it single use: once the commit lands, HEAD has
        moved, the next commit is refused and the token is spent."""
        work, env, token = repo
        self._write_token(work, env, token)
        assert self._decide(work, env, self.REG) == "allow"
        assert token.exists()
        self._git(work, env, self.C, "-q", "--allow-empty", "-m", "real")
        self._release_claim(token)
        decision, reason = self._decide_with_reason(work, env, self.REG)
        assert decision == "deny"
        assert "HEAD has moved since the preflight ran" in reason
        assert not token.exists()

    def test_amend_may_reuse_the_token_but_a_regular_commit_may_not(self, repo):
        work, env, token = repo
        self._git(work, env, self.C, "-q", "--allow-empty", "-m", "base")
        self._write_token(work, env, token)
        self._git(work, env, self.C, "-q", "--amend", "--allow-empty", "-m", "amended")
        assert self._decide(work, env, self.AMEND) == "allow"
        self._release_claim(token)
        assert self._decide(work, env, self.REG) == "deny"

    def test_amend_after_a_regular_commit_is_denied(self, repo):
        work, env, token = repo
        self._write_token(work, env, token)
        self._git(work, env, self.C, "-q", "--allow-empty", "-m", "real")
        assert self._decide(work, env, self.AMEND) == "deny"

    def test_a_second_amend_of_a_root_commit_is_allowed(self, repo):
        """Both commits have no parent, so they are siblings."""
        work, env, token = repo
        self._write_token(work, env, token)  # at the seed, a root commit
        self._git(work, env, self.C, "-q", "--amend", "--allow-empty", "-m", "seed 2")
        assert self._decide(work, env, self.AMEND) == "allow"
        self._release_claim(token)
        assert self._decide(work, env, self.REG) == "deny"

    @pytest.mark.parametrize("msg", ["drop the --amend flag", "--amend"])
    def test_amend_text_inside_a_message_is_not_an_amend(self, repo, msg):
        work, env, token = repo
        self._git(work, env, self.C, "-q", "--allow-empty", "-m", "base")
        self._write_token(work, env, token)
        self._git(work, env, self.C, "-q", "--amend", "--allow-empty", "-m", "amended")
        cmd = f"git {self.C} -m {shlex.quote(msg)}"
        assert self._decide(work, env, cmd) == "deny"

    def test_an_abbreviated_amend_flag_is_an_amend(self, repo):
        work, env, token = repo
        self._git(work, env, self.C, "-q", "--allow-empty", "-m", "base")
        self._write_token(work, env, token)
        self._git(work, env, self.C, "-q", "--amend", "--allow-empty", "-m", "amended")
        assert self._decide(work, env, f"git {self.C} --amen --no-edit") == "allow"

    def test_command_parsing_is_capped_and_fast(self, repo):
        """Our old hook read at most 100,000 characters of the command. The
        vendored one reads up to _MAX_COMMAND_BYTES (2 MiB) and refuses a
        larger command outright, before any scanning, so it cannot run past
        its deadline. A long amend under the cap is still read as an amend,
        and quickly."""
        work, env, token = repo
        cap = _vendored_hook_constant("require-preflight", "_MAX_COMMAND_BYTES")
        self._git(work, env, self.C, "-q", "--allow-empty", "-m", "base")
        self._write_token(work, env, token)
        self._git(work, env, self.C, "-q", "--amend", "--allow-empty", "-m", "amended")
        under = f"git {self.C} --amend -m " + "x" * 99_000
        start = time.perf_counter()
        assert self._decide(work, env, under) == "allow"
        assert time.perf_counter() - start < 2
        self._release_claim(token)
        over = f"git {self.C} --amend -m " + "x" * (cap + 1)
        decision, reason = self._decide_with_reason(work, env, over)
        assert decision == "deny"
        assert "larger than 2 MB" in reason, reason[:200]
        assert token.exists(), "a refused oversize command spent the token"

    def test_resetting_back_to_the_token_head_makes_it_usable_again(self, repo):
        """A stated limit, pinned so a change to it is deliberate: HEAD and
        the staged tree are all the hook sees, so undoing the commit
        restores the token until it expires."""
        work, env, token = repo
        self._write_token(work, env, token)
        start = self._git(work, env, "rev-parse", "HEAD")
        self._git(work, env, self.C, "-q", "--allow-empty", "-m", "real")
        self._git(work, env, "reset", "-q", "--soft", start)
        assert self._decide(work, env, self.REG) == "allow"

    # --- where the commit runs ----------------------------------------------

    @pytest.mark.parametrize("shape", ["git -C", "cd &&", "shell cwd"])
    def test_a_commit_in_a_worktree_is_refused_and_keeps_the_token(self, repo, shape):
        """A worktree commit moves the worktree's HEAD, never the project's,
        so a token kept for it could cover any number of commits. Our old
        hook allowed it and spent the token; the vendored hook binds the
        token to the checkout preflight ran in and refuses any other
        (harden-repo #156 G-20, by decision). Nothing ran, so it is kept."""
        work, env, token = repo
        wt = self._worktree(work, env)
        self._write_token(work, env, token)
        cwd = "project"
        if shape == "git -C":
            cmd = f"git -C {wt} {self.C} -m wip"
        elif shape == "cd &&":
            cmd = f"cd {wt} && git {self.C} -m wip"
        else:
            cmd, cwd = self.REG, wt
        decision, reason = self._decide_with_reason(work, env, cmd, cwd=cwd)
        assert decision == "deny"
        assert "This commit runs in another checkout" in reason
        assert token.exists()
        assert "claimed_at" not in json.loads(token.read_text())

    def test_a_payload_without_cwd_is_judged_in_the_project(self, repo):
        """With no `cwd` the vendored hook judges the commit in
        CLAUDE_PROJECT_DIR, where the token was made, and keeps it (claimed)
        like any other exact read (harden-repo #156 G-19). Our old hook spent
        it, as the checkout was not proven."""
        work, env, token = repo
        self._write_token(work, env, token)
        assert self._decide(work, env, self.REG, cwd=None) == "allow"
        assert "claimed_at" in json.loads(token.read_text())

    def test_a_subdirectory_of_the_project_keeps_the_token(self, repo):
        work, env, token = repo
        (work / "sub").mkdir()
        self._write_token(work, env, token)
        assert self._decide(work, env, self.REG, cwd=work / "sub") == "allow"
        assert token.exists()

    def test_a_nested_repo_is_refused_and_keeps_the_token(self, repo):
        """A repo nested inside the project is another checkout: refused,
        token kept (G-20). Our old hook allowed it and spent the token."""
        work, env, token = repo
        nested = work / "vendor"
        nested.mkdir()
        subprocess.run(["git", "-C", str(nested), "init", "-q"], env=env, check=True)
        self._write_token(work, env, token)
        decision, reason = self._decide_with_reason(work, env, self.REG, cwd=nested)
        assert decision == "deny"
        assert "This commit runs in another checkout" in reason
        assert token.exists()

    def test_the_408_shape_keeps_the_token(self, repo):
        """The case #408 is about: a commit chained with a PR create that the
        changelog hook denies. Nothing ran, so the token must survive."""
        work, env, token = repo
        self._write_token(work, env, token)
        cmd = (f"git {self.C} -q -F msg.txt && "
               "gh pr create --base develop --title 't' --body-file b.md")
        assert self._decide(work, env, cmd) == "allow"
        assert token.exists()

    # Each was measured landing commits on one kept token in our old hook
    # (#410 review). The vendored hook still allows these and spends the
    # token on approval, because it cannot read the commit exactly.
    @pytest.mark.parametrize("cmd", [
        "find .claude/worktrees -execdir git COMMIT -m x ;",
        "GIT_DIR=elsewhere git COMMIT -m x",
        "sh -c 'git COMMIT -m x'",
        "git COMMIT -m x; git COMMIT -m y",
        "git COMMIT -m x | cat",
        "/usr/bin/git COMMIT -m x",
        "git COMMIT -m x\\ y",
        # A redirection does not split commands, so a leading one hides the
        # command name.
        "> out.txt git COMMIT -m wip",
        # A redirection can plant a hook that commits elsewhere, and its
        # target is not an argument (`> --amend` is no amend flag).
        "echo 'git -C .claude/worktrees/wt COMMIT -m x' > .git/hooks/post-COMMIT && git COMMIT -m y",
        "git COMMIT -m x > --amend",
        "git COMMIT -m x 2>/dev/null",
        # push runs a program.
        "git push -q origin HEAD && git COMMIT -m x",
        "git COMMIT -m wip && gh pr checkout 5",
    ])
    def test_a_command_not_proven_harmless_spends_the_token(self, repo, cmd):
        work, env, token = repo
        self._worktree(work, env)
        self._write_token(work, env, token)
        cmd = cmd.replace("COMMIT", self.C).replace("\\n", "\n")
        assert self._decide(work, env, cmd) == "allow"
        assert not token.exists()

    # The rest of the #410 rows, plus two our old hook kept the token for.
    # Our old hook allowed every one (spending the token where unproven). The
    # vendored hook REFUSES each, and keeps the token, because the call may
    # change the index the token covers, may commit in another checkout, or
    # cannot be pinned to one (harden-repo #156 G-20, by decision; G-4 for
    # the submodule row, which our old hook was the only one to see). Each
    # row names the refusal it gets.
    STAGES = "This call may stage files itself"
    ELSEWHERE = "This commit runs in another checkout"
    UNKNOWN = "Cannot tell which checkout this commit runs in"
    UNPARSEABLE = "could not parse this command"
    REFUSED = (
        ("git submodule foreach -q 'git COMMIT -qam s'", STAGES),
        ("c''d .claude/worktrees/wt && git COMMIT -qam w", STAGES),
        ('c""d .claude/worktrees/wt && git COMMIT -qam w', STAGES),
        ('git "-C" .claude/worktrees/wt COMMIT -m w', ELSEWHERE),
        ("git checkout -q -b hop && git COMMIT -qam h && git checkout -q -", STAGES),
        ("git switch -c hop && git COMMIT -qam h && git switch -", STAGES),
        ("git COMMIT --amend --no-edit && git -C .claude/worktrees/wt COMMIT -m x",
         UNKNOWN),
        ("env --chdir=.claude/worktrees/wt git COMMIT -m x", ELSEWHERE),
        ("git -c core.worktree=x COMMIT -m x", STAGES),
        ("git COMMIT -m $(cat msg)", STAGES),
        ("git COMMIT -m 'unbalanced", UNPARSEABLE),
        ("git COMMIT --amend --no-edit && git push --receive-pack='git -C wt COMMIT -m h; false' . HEAD",
         UNKNOWN),
        # diff/log write files.
        ("git diff --output=.git/hooks/post-COMMIT && git COMMIT -m x", STAGES),
        ("git log --output=x && git COMMIT -m x", STAGES),
        ("git COMMIT -m wip\\ncd .claude/worktrees/wt\\ngit COMMIT -qam w", STAGES),
        # Quoted text bash still runs: each hides a cd and a second commit.
        ('git COMMIT -m "$(cd .claude/worktrees/wt && git COMMIT -qam w)"', STAGES),
        ('git COMMIT -m "`cd .claude/worktrees/wt && git COMMIT -qam w`"', STAGES),
        ("git COMMIT -m a \\'; cd .claude/worktrees/wt && git COMMIT -qam w; echo \\'",
         STAGES),
        # Our old hook kept the token for these two. Staging in the same call
        # is the workflow cost #156 names: stage, run preflight, then commit
        # in its own call.
        ("git add -A && git COMMIT -m 'fix: x; y (z) #1 | w'", STAGES),
        ("git COMMIT -m wip # ; cd .claude/worktrees/wt", UNKNOWN),
        # A bare `commit -a` (and a pathspec) stages tracked changes itself.
        ("git COMMIT -a -m w", STAGES),
        ("git COMMIT --all -m w", STAGES),
        ("git COMMIT -qam w", STAGES),
        ("git COMMIT -m w -- f.txt", STAGES),
    )

    @pytest.mark.parametrize("cmd,reason", REFUSED)
    def test_a_call_that_may_stage_or_move_is_refused_and_keeps_the_token(
            self, repo, cmd, reason):
        work, env, token = repo
        self._worktree(work, env)
        self._write_token(work, env, token)
        cmd = cmd.replace("COMMIT", self.C).replace("\\n", "\n")
        decision, got = self._decide_with_reason(work, env, cmd)
        assert decision == "deny", cmd
        assert reason in got, got[:200]
        assert token.exists(), f"{cmd!r} spent the token without running"

    @pytest.mark.parametrize("cmd", [
        "git submodule foreach -q 'git COMMIT -qam s'",
        "git COMMIT -qam w",
        "git add -A && git COMMIT -m x",
    ])
    def test_a_staging_commit_with_no_token_is_still_refused(self, repo, cmd):
        """The control for the rows above: with no token at all these are
        refused as "preflight required", so the staging refusal is not the
        only thing standing between them and a commit. harden-repo #156 G-4
        found the submodule row allowed with no token; v1.3.0 sees it."""
        work, env, token = repo
        assert not token.exists()
        cmd = cmd.replace("COMMIT", self.C)
        decision, reason = self._decide_with_reason(work, env, cmd)
        assert decision == "deny"
        assert "Preflight verification required" in reason, reason[:200]

    @pytest.mark.parametrize("cmd", [
        "(git COMMIT -m wip) && git status --short",
        "echo ok && git COMMIT -m wip",
        "git COMMIT -m wip\\ngh issue comment 1 --body x",
    ])
    def test_a_harmless_command_keeps_the_token(self, repo, cmd):
        work, env, token = repo
        self._write_token(work, env, token)
        cmd = cmd.replace("COMMIT", self.C).replace("\\n", "\n")
        assert self._decide(work, env, cmd) == "allow"
        assert token.exists()

    def test_an_amend_outside_the_project_is_refused_and_keeps_the_token(self, repo):
        """G-20: another checkout. Our old hook allowed it and spent the
        token."""
        work, env, token = repo
        wt = self._worktree(work, env)
        self._write_token(work, env, token)
        decision, reason = self._decide_with_reason(work, env, self.AMEND, cwd=wt)
        assert decision == "deny"
        assert "This commit runs in another checkout" in reason
        assert token.exists()

    def test_a_quoted_pipe_in_the_message_does_not_hide_the_amend(self, repo):
        work, env, token = repo
        self._git(work, env, self.C, "-q", "--allow-empty", "-m", "base")
        self._write_token(work, env, token)
        self._git(work, env, self.C, "-q", "--amend", "--allow-empty", "-m", "amended")
        cmd = f'git {self.C} -m "|" --amend'
        assert self._decide(work, env, cmd) == "allow"

    def test_amend_after_a_double_dash_is_a_path_not_a_flag(self, repo):
        work, env, token = repo
        self._git(work, env, self.C, "-q", "--allow-empty", "-m", "base")
        self._write_token(work, env, token)
        self._git(work, env, self.C, "-q", "--amend", "--allow-empty", "-m", "amended")
        assert self._decide(work, env, f"git {self.C} -m x -- --amend") == "deny"

    # --- tokens that cannot be trusted --------------------------------------

    @pytest.mark.parametrize("missing", ["all", "head", "tree", "worktree"])
    def test_a_token_without_its_binding_is_refused_and_spent(self, repo, missing):
        """Our old hook accepted a token with no `head` once and spent it.
        The vendored hook refuses a token with none of head/tree/worktree as
        an old format, and one missing any of them as invalid, and spends
        either (harden-repo #156 legacy-token decision, #166). Both are
        stricter: the commit is refused, not allowed."""
        work, env, token = repo
        if missing == "all":
            self._write_token(work, env, token, bound=False)
            expected = "This preflight token is in an old format"
        else:
            self._write_token(work, env, token, **{missing: None})
            expected = "Invalid preflight token"
        decision, reason = self._decide_with_reason(work, env, self.REG)
        assert decision == "deny"
        assert expected in reason, reason[:200]
        assert not token.exists()
        assert self._decide(work, env, self.REG) == "deny"

    def test_an_empty_head_from_a_repo_with_no_commits_is_spent_on_approval(self, repo):
        work, env, token = repo
        self._write_token(work, env, token, head="")
        assert self._decide(work, env, self.REG) == "allow"
        assert not token.exists()

    def test_an_expired_token_is_still_denied(self, repo):
        work, env, token = repo
        self._write_token(work, env, token, expires=int(time.time()) - 1)
        assert self._decide(work, env, self.REG) == "deny"

    @pytest.mark.parametrize("raw", [
        "[1, 2]", '"abc"', "null", '{"expires": "9"}', '{"expires": null}',
        '{"expires": NaN}', '{"expires": Infinity}', '{"expires": true}',
        '{"expires": 1.5e99}', b"\xff\xfe\x00",
        # A short id: pytest puts the test id in PYTEST_CURRENT_TEST, and
        # Linux refuses an env string over 128 KB (E2BIG) when git starts.
        pytest.param("[" * 100_000 + "]" * 100_000, id="deep-nesting"),
    ])
    def test_a_malformed_token_is_denied_not_crashed_on(self, repo, raw):
        """A crash exits 1, a non-blocking error, and the commit runs. These
        were each measured crashing (or, for NaN/Infinity, never expiring)
        before the shape check."""
        work, env, token = repo
        if isinstance(raw, bytes):
            token.write_bytes(raw)
        else:
            token.write_text(raw)
        assert self._decide(work, env, self.REG) == "deny"

    @pytest.mark.parametrize("expires", [
        "Infinity", "NaN", "1.5e99", "9999999999",
    ])
    def test_a_bound_token_that_never_expires_is_denied(self, repo, expires):
        """harden-repo #156 G-5: only reachable with upstream's token shape,
        so the rows above (no binding) could not show it. A float, or an int
        more than an hour ahead, is refused."""
        work, env, token = repo
        self._write_token(work, env, token)
        data = token.read_text().replace(
            f'"expires": {json.loads(token.read_text())["expires"]}',
            f'"expires": {expires}')
        assert expires in data
        token.write_text(data)
        decision, reason = self._decide_with_reason(work, env, self.REG)
        assert decision == "deny"
        assert "Corrupt preflight token" in reason, reason[:200]

    @pytest.mark.parametrize("bad", ["--output=/tmp/x", "HEAD", "HEAD~0", "abc", 7,
                                     "A" * 40])
    @pytest.mark.parametrize("amend", [False, True])
    def test_a_malformed_head_is_denied_and_never_reaches_git(self, repo, bad, amend):
        """Only a full lowercase hex SHA is read. Without that check, `HEAD~0`
        names a commit whose parent is always HEAD's parent, so every amend
        passed. A logging `git` shim proves the value is never an argument."""
        work, env, token = repo
        self._git(work, env, self.C, "-q", "--allow-empty", "-m", "base")
        self._write_token(work, env, token, head=bad)
        log = work.parent / "git-argv.log"
        shim = work.parent / "shim"
        shim.mkdir()
        (shim / "git").write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{log}"\n'
            f'exec "{shutil.which("git")}" "$@"\n')
        (shim / "git").chmod(0o755)
        env = dict(env, PATH=f"{shim}{os.pathsep}{env.get('PATH', '')}")
        cmd = self.AMEND if amend else self.REG
        decision, reason = self._decide_with_reason(work, env, cmd)
        assert decision == "deny"
        assert "Invalid preflight token" in reason, reason[:200]
        assert not token.exists()
        calls = log.read_text().splitlines() if log.exists() else []
        # No git argument is, or is built from, the bad value.
        assert not any(arg == str(bad) or arg.startswith(str(bad) + "^")
                       for call in calls for arg in call.split()), calls

    def test_a_git_failure_denies_and_keeps_the_token(self, repo):
        """git failing says nothing about whether the token was used, so the
        commit is refused (fail closed) but the token is not thrown away.

        The shim passes `rev-parse --git-common-dir` through: the hook keys
        the token path on it, and a shim that failed it too would send the
        hook to another path, where it finds no token and denies for that
        reason instead (measured), proving nothing about this one."""
        work, env, token = repo
        self._write_token(work, env, token)
        shim = work.parent / "broken"
        shim.mkdir()
        (shim / "git").write_text(
            "#!/bin/sh\n"
            'case "$*" in *--git-common-dir*) '
            f'exec "{shutil.which("git")}" "$@" ;; esac\n'
            "exit 1\n")
        (shim / "git").chmod(0o755)
        env = dict(env, PATH=f"{shim}{os.pathsep}{env.get('PATH', '')}")
        decision, reason = self._decide_with_reason(work, env, self.REG)
        assert decision == "deny"
        assert "Could not read the checkout or its staged tree" in reason, reason[:200]
        assert token.exists()

    def test_a_sha256_head_is_accepted(self, tmp_path):
        work = tmp_path / "s256"
        shutil.copytree(Path(".claude/hooks"), work / ".claude/hooks")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX"))}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                   CLAUDE_PROJECT_DIR=str(work))
        init = subprocess.run(["git", "-C", str(work), "init", "-q",
                               "--object-format=sha256", "-b", "feature/probe"],
                              env=env, capture_output=True)
        if init.returncode != 0:
            pytest.skip("this git cannot make a SHA-256 repo")
        self._git(work, env, self.C, "-q", "--allow-empty", "-m", "seed")
        token = self._token_path(work)
        try:
            self._write_token(work, env, token)
            data = json.loads(token.read_text())
            assert len(data["head"]) == len(data["tree"]) == 64
            assert self._decide(work, env, self.REG) == "allow"
            assert token.exists()
        finally:
            token.unlink(missing_ok=True)
            Path(f"{token}.lock").unlink(missing_ok=True)

    # --- the producer ---------------------------------------------------------

    def test_the_real_preflight_records_head_and_the_hook_accepts_it(self, repo):
        """End to end through the script's skip-tests path (the full path
        runs this suite): the token it writes, at the path the hook reads,
        names HEAD, the staged tree and this checkout, and the hook keeps it
        for a commit in the project."""
        work, env, token = repo
        (work / "scripts").mkdir()
        shutil.copy("scripts/commit-preflight.sh", work / "scripts")
        (work / "f.txt").write_text("x")
        self._git(work, env, "add", "f.txt")
        proc = subprocess.run(
            ["bash", "scripts/commit-preflight.sh", "--skip-tests", "probe"],
            cwd=work, env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stdout[-400:] + proc.stderr[-400:]
        data = json.loads(token.read_text())
        assert data["head"] == self._git(work, env, "rev-parse", "HEAD")
        assert data["tree"] == self._git(work, env, "write-tree")
        assert data["worktree"] == os.path.realpath(work)
        assert self._decide(work, env, self.REG) == "allow"
        assert token.exists()

    # --- --allow-empty (#116) -------------------------------------------------

    EMPTY = "git " + C + " --allow-empty -m marker"

    def _preflight(self, work, env, *args):
        if not (work / "scripts").exists():
            (work / "scripts").mkdir()
            shutil.copy("scripts/commit-preflight.sh", work / "scripts")
        return subprocess.run(
            ["bash", "scripts/commit-preflight.sh", *args],
            cwd=work, env=env, capture_output=True, text=True, timeout=60)

    def test_allow_empty_mints_a_token_for_an_empty_commit(self, repo):
        work, env, token = repo
        proc = self._preflight(work, env, "--allow-empty")
        assert proc.returncode == 0, proc.stdout[-400:] + proc.stderr[-400:]
        data = json.loads(token.read_text())
        assert data["head"] == self._git(work, env, "rev-parse", "HEAD")
        assert data["staged_files"] == 0
        assert data["skip_reason"] == "empty commit (--allow-empty)"
        assert self._decide(work, env, self.EMPTY) == "allow"

    def test_the_empty_commit_spends_the_token(self, repo):
        """Single use: once the empty commit has landed and real changes
        are staged, the same token does not cover a second commit.

        This passed vacuously before #418: the fixture looked for the token
        at our old path, where the vendored preflight never writes, so "no
        token" was all it saw. The claim is released first, or the deny
        would be the claim hold, which keeps the token."""
        work, env, token = repo
        assert self._preflight(work, env, "--allow-empty").returncode == 0
        assert token.exists()
        assert self._decide(work, env, self.EMPTY) == "allow"
        self._git(work, env, self.C, "-q", "--allow-empty", "-m", "marker")
        (work / "f.txt").write_text("x")
        self._git(work, env, "add", "f.txt")
        self._release_claim(token)
        decision, reason = self._decide_with_reason(work, env, self.REG)
        assert decision == "deny"
        assert "staged files changed since preflight ran" in reason, reason[:200]
        assert not token.exists()

    def test_allow_empty_refuses_when_files_are_staged(self, repo):
        """Otherwise --allow-empty would be a way to skip the tests on real
        changes."""
        work, env, token = repo
        (work / "f.txt").write_text("x")
        self._git(work, env, "add", "f.txt")
        proc = self._preflight(work, env, "--allow-empty")
        assert proc.returncode == 1
        assert "files are staged" in proc.stdout
        assert not token.exists()

    def test_allow_empty_refuses_unstaged_tracked_changes(self, repo):
        """The hook checks only expiry and HEAD, so `git commit -a` or
        `git commit -m x <path>` would commit these with no scan or test."""
        work, env, token = repo
        (work / "f.txt").write_text("x")
        self._git(work, env, "add", "f.txt")
        self._git(work, env, self.C, "-q", "-m", "track f")
        (work / "f.txt").write_text("changed")
        proc = self._preflight(work, env, "--allow-empty")
        assert proc.returncode == 1, proc.stdout[-400:]
        assert "unstaged changes" in proc.stdout
        assert "f.txt" in proc.stdout
        assert not token.exists()

    def test_allow_empty_still_allows_an_untracked_file(self, repo):
        """`git commit -a` never takes untracked files, and a pathspec
        naming one errors in git, so an untracked file cannot slip through."""
        work, env, token = repo
        (work / "new.txt").write_text("x")
        proc = self._preflight(work, env, "--allow-empty")
        assert proc.returncode == 0, proc.stdout[-400:] + proc.stderr[-400:]
        assert token.exists()

    def test_allow_empty_refuses_when_git_cannot_read_the_index(self, repo):
        """The staged list falls back to empty when git fails, so the flag
        asks git itself, and a git error is not "nothing staged"."""
        work, env, token = repo
        self._preflight(work, env, "--not-a-flag")  # copies the script in
        shim = work.parent / "fatal-git"
        shim.mkdir()
        (shim / "git").write_text("#!/bin/sh\nexit 128\n")
        (shim / "git").chmod(0o755)
        env = dict(env, PATH=f"{shim}{os.pathsep}{env.get('PATH', '')}")
        proc = self._preflight(work, env, "--allow-empty")
        assert proc.returncode == 1, proc.stdout[-400:]
        assert "Could not read the git index (git exited 128)" in proc.stdout
        assert not token.exists()

    def test_nothing_staged_without_the_flag_is_still_refused(self, repo):
        work, env, token = repo
        proc = self._preflight(work, env)
        assert proc.returncode == 1
        assert "No staged files" in proc.stdout
        assert "--allow-empty" in proc.stdout
        assert not token.exists()

    def test_the_token_counts_staged_files_exactly(self, repo):
        work, env, token = repo
        for name in ("a.txt", "b.txt"):
            (work / name).write_text("x")
            self._git(work, env, "add", name)
        assert self._preflight(work, env, "--skip-tests", "probe").returncode == 0
        assert json.loads(token.read_text())["staged_files"] == 2
