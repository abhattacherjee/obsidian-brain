"""Security hardening tests for obsidian-brain."""
import ast
import glob
import json
import os
import re
import stat
import tempfile
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

    It also walks the tree the SKILL.md blocks ACTUALLY resolve (#278). The
    repo-only walk has a blind spot with teeth: skills do not import from the
    checkout, they import from whatever ``_ob_hooks()`` returns, and the cached
    ``deep_cli.py`` could sit there with #275's ``_read_stdin_capped`` reverted
    while this guard stayed green against a repo that had the fix.
    """

    CAP = 1_000_000
    # `read(CAP + 1)` is the documented overflow-detection idiom (note_writer),
    # so the bound may exceed CAP by exactly one.
    MAX_ALLOWED = CAP + 1

    @staticmethod
    def _skill_resolved_install_root():
        """The install root the canonical #278 resolver lands on, or None.

        Mirrors the resolver copied into all 68 SKILL.md sites — marketplace
        ``installLocation`` first, allowlist-filtered cache glob as fallback.
        Byte-identity of those copies is enforced by
        tests/test_hooks_resolver_drift.py; what matters here is only WHERE
        they point.

        Returns None when nothing resolves — which is the normal state on CI
        (no registry, no cache), so this extension is a no-op there rather than
        a failure.
        """
        try:
            registry = os.path.expanduser("~/.claude/plugins/known_marketplaces.json")
            with open(registry, encoding="utf-8") as f:
                for entry in json.load(f).values():
                    # Directory-source entries only. obsidian-brain's
                    # marketplace.json declares `"source": "./"`, so a
                    # github-source marketplace CLONE also carries
                    # hooks/obsidian_utils.py and would satisfy the sentinel
                    # below — the discriminator is what keeps github installs
                    # resolving the cache. Shape-tolerant on purpose: a string
                    # or list `source` must `continue`, never raise, or one
                    # third-party entry aborts iteration over the rest.
                    source = entry.get("source") if isinstance(entry, dict) else None
                    if not (
                        isinstance(source, dict) and source.get("source") == "directory"
                    ):
                        continue
                    # `continue`, not a bare read: one malformed third-party
                    # entry ordered ahead of obsidian-brain's must not abort
                    # iteration. `isabs` because a relative location would make
                    # the sentinel cwd-dependent. Kept in lockstep with the
                    # canonical forms in tests/test_hooks_resolver_drift.py.
                    location = (
                        entry.get("installLocation")
                        if isinstance(entry, dict)
                        else None
                    )
                    if not (isinstance(location, str) and os.path.isabs(location)):
                        continue
                    hooks = os.path.join(location, "hooks")
                    if os.path.isfile(os.path.join(hooks, "obsidian_utils.py")):
                        return Path(location)
        except Exception:
            pass
        cached = [
            d
            for d in glob.glob(
                os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")
            )
            if re.fullmatch("[0-9]+([.][0-9]+)*", d.split("/")[-2])
        ]
        best = max(
            cached,
            key=lambda p: ([int(n) for n in p.split("/")[-2].split(".")], p),
            default=None,
        )
        return Path(best).parent if best else None

    @classmethod
    def _source_modules(cls):
        roots = [
            *sorted(Path("hooks").glob("*.py")),
            *sorted(Path("scripts").rglob("*.py")),
        ]
        # OPT-IN, not automatic. Scanning the resolved tree means asserting on
        # code that is not in this checkout: a contributor whose install
        # resolves the released 3.3.0 or 3.2.2 cache gets a RED suite on a
        # clean `develop`, naming three files they cannot fix from their tree
        # (deep_cli.py x2, vault_doctor.py). CI never sees it — no registry, no
        # cache — so the failure lands only on developer machines. Set
        # OB_SCAN_RESOLVED_INSTALL=1 to audit the tree the skills really load.
        resolved = cls._skill_resolved_install_root()
        if (
            os.environ.get("OB_SCAN_RESOLVED_INSTALL") == "1"
            and resolved is not None
            and resolved.is_dir()
        ):
            # Skip when the resolver points back at this checkout (the normal
            # case for a directory-source install) — the repo walk above
            # already covers it, and scanning it twice only doubles offenders.
            if resolved.resolve() != Path(".").resolve():
                roots += sorted((resolved / "hooks").glob("*.py"))
                roots += sorted((resolved / "scripts").rglob("*.py"))
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

    def test_discovery_finds_the_known_entry_points(self):
        """Guards the guard: if the AST walk ever stops finding reads, the
        check above would pass vacuously. These are the entry points that read
        stdin today — including the two the old hardcoded list missed."""
        paths = {path for path, _, _, _ in self._all_stdin_reads()}
        for expected in (
            "hooks/obsidian_session_log.py",
            "hooks/obsidian_session_hint.py",
            "hooks/obsidian_context_snapshot.py",
            "hooks/note_writer.py",
            "hooks/check_items_cli.py",
        ):
            assert expected in paths, f"stdin read in {expected} no longer discovered"
        assert len(self._all_stdin_reads()) >= 8

    def test_scan_reaches_the_tree_the_skills_actually_resolve(
        self, tmp_path, monkeypatch
    ):
        """#278: the walk must follow the resolver, not just the checkout.

        Hermetic — a fake install under tmp_path with $HOME redirected, so this
        behaves identically on CI, where neither a registry nor a cache exists.
        The uncapped ``sys.stdin.read()`` planted below stands in for the real
        hazard: a released ``deep_cli.py`` sitting in the resolved tree with
        #275's cap reverted, invisible to a repo-only scan.

        ``OB_SCAN_RESOLVED_INSTALL`` is set here because the extension is
        opt-in for everyone else (see ``_source_modules``). Setting it inside
        a hermetic fixture keeps this test proving the walking logic works
        without exporting a real machine's cache into the assertion.
        """
        install = tmp_path / "resolved-install"
        (install / "hooks").mkdir(parents=True)
        (install / "hooks" / "obsidian_utils.py").write_text("", encoding="utf-8")
        (install / "hooks" / "stale_cli.py").write_text(
            "import sys\npayload = sys.stdin.read()\n", encoding="utf-8"
        )
        home = tmp_path / "home"
        (home / ".claude" / "plugins").mkdir(parents=True)
        (home / ".claude" / "plugins" / "known_marketplaces.json").write_text(
            json.dumps(
                {
                    "user-chosen-name": {
                        "source": {"source": "directory", "path": str(install)},
                        "installLocation": str(install),
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("USERPROFILE", raising=False)
        monkeypatch.setenv("OB_SCAN_RESOLVED_INSTALL", "1")

        assert self._skill_resolved_install_root() == install
        offenders = [
            path for path, _, _, _ in self._all_stdin_reads() if "stale_cli" in path
        ]
        assert offenders, (
            "the stdin-cap scan did not follow the resolver into the install "
            "tree — a stale released module can revert its cap unnoticed"
        )
        with pytest.raises(AssertionError, match="Uncapped or over-cap stdin read"):
            self.test_every_stdin_read_is_capped()

    # --- parity with the canonical resolver -------------------------------
    #
    # _skill_resolved_install_root is a MIRROR of the resolver copied into the
    # 68 SKILL.md sites, and a mirror that can drift is worth little: a
    # mutation that reverted the entry validation below left the whole security
    # module green. tests/test_hooks_resolver_drift.py pins the 68 copies to
    # each other; these two pin this copy to the same semantics, behaviourally
    # rather than by comparing text.

    #: A directory-source marketplace entry's ``source`` block. The resolver
    #: keys on this: obsidian-brain's marketplace.json says ``"source": "./"``,
    #: so a github clone is also a full plugin tree and the sentinel alone
    #: cannot tell the two apart.
    DIRECTORY_SOURCE = {"source": "directory", "path": "/irrelevant"}

    @staticmethod
    def _registry(home, entries):
        (home / ".claude" / "plugins").mkdir(parents=True, exist_ok=True)
        (home / ".claude" / "plugins" / "known_marketplaces.json").write_text(
            json.dumps(entries), encoding="utf-8"
        )

    @staticmethod
    def _install(root):
        (root / "hooks").mkdir(parents=True)
        (root / "hooks" / "obsidian_utils.py").write_text("", encoding="utf-8")
        return root

    def test_mirror_skips_a_bad_entry_and_keeps_looking(self, tmp_path, monkeypatch):
        """A malformed third-party entry ordered first must not abort the loop
        (json.load preserves insertion order, so ``aaa-`` is iterated first).

        The bad entry carries a valid directory ``source`` so it reaches the
        installLocation guard: without it the discriminator would skip it
        first and this would stop testing what its name says.
        """
        install = self._install(tmp_path / "checkout")
        home = tmp_path / "home"
        self._registry(
            home,
            {
                "aaa-third-party": {
                    "source": self.DIRECTORY_SOURCE,
                    "installLocation": None,
                },
                "user-chosen-name": {
                    "source": self.DIRECTORY_SOURCE,
                    "installLocation": str(install),
                },
            },
        )
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("USERPROFILE", raising=False)
        assert self._skill_resolved_install_root() == install

    def test_mirror_ignores_a_cwd_relative_install_location(
        self, tmp_path, monkeypatch
    ):
        """A relative installLocation must be skipped, not resolved against
        whatever directory pytest happens to be running in."""
        cwd = tmp_path / "cwd"
        self._install(cwd / "relative-install")
        home = tmp_path / "home"
        self._registry(
            home,
            {
                "mp": {
                    "source": self.DIRECTORY_SOURCE,
                    "installLocation": "relative-install",
                }
            },
        )
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("USERPROFILE", raising=False)
        monkeypatch.chdir(cwd)
        assert self._skill_resolved_install_root() is None

    def test_mirror_ignores_a_github_source_marketplace_clone(
        self, tmp_path, monkeypatch
    ):
        """Third parity property, and the one #278's final review added.

        A github-source marketplace clone satisfies the sentinel too (the
        marketplace repo IS the plugin repo), so without the ``source.source``
        discriminator this mirror would drift from the 68 SKILL.md copies the
        moment they gained it — and a mirror that can drift is worth little.
        """
        clone = self._install(tmp_path / "marketplace-clone")
        home = tmp_path / "home"
        self._registry(
            home,
            {
                "mp": {
                    "source": {"source": "github", "repo": "a/obsidian-brain"},
                    "installLocation": str(clone),
                }
            },
        )
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("USERPROFILE", raising=False)
        assert self._skill_resolved_install_root() is None

    def test_mirror_does_not_raise_on_a_non_dict_source(self, tmp_path, monkeypatch):
        """A string/list ``source`` must ``continue``, not raise: the whole
        loop shares one ``try``, so a raise on entry 1 skips entries 2..N."""
        install = self._install(tmp_path / "checkout")
        home = tmp_path / "home"
        self._registry(
            home,
            {
                "aaa-third-party": {
                    "source": "directory",
                    "installLocation": str(tmp_path / "nowhere"),
                },
                "user-chosen-name": {
                    "source": self.DIRECTORY_SOURCE,
                    "installLocation": str(install),
                },
            },
        )
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("USERPROFILE", raising=False)
        assert self._skill_resolved_install_root() == install


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
