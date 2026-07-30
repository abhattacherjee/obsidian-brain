"""Drift + behaviour guards for the 68 hand-copied hook resolvers (#278).

Every ``skills/*/SKILL.md`` bash block is its own shell, so the resolver that
puts ``hooks/`` on ``sys.path`` cannot be hoisted into one shared definition —
it is copied verbatim into 68 sites. This module is what makes that duplication
safe. It has two halves:

**Drift (static).** Each site is extracted from the file, normalised by *its own
leading indent* (the blocks sit at different indentation depths, and comparing
un-normalised text invents spurious families), and compared **byte for byte**
against a canonical form declared here. The site count is asserted by
**equality**: a ``>=`` floor would let a silently deleted site pass.

**Behaviour (executed).** The canonical forms are not just compared as text —
every distinct block observed in the files is ``exec``'d against fixture trees
under ``tmp_path`` with ``$HOME`` redirected, and asserted to (a) prefer the
marketplace ``installLocation`` over any cache, (b) reject non-canonical
sibling directories, and (c) never raise on a malformed
``known_marketplaces.json``. Executing the *file-derived* text (rather than the
constants below) is deliberate: a site edited to keep the resolver's variable
names while gutting its logic still gets run, and still fails.

That last point is the carried finding from Task 2's review, which proved by
mutation that ``assert "key=lambda _p:" in window`` is a naming proxy: a site
rewritten back to cache-only globbing, with the lambda's name untouched, passed
the entire suite. ``test_every_resolver_site_consults_install_location_before_the_cache``
is the test that now fails for that exact mutation.

Nothing here reads this machine's real ``~/.claude`` — every fixture is built
under ``tmp_path`` with ``$HOME`` monkeypatched, so the suite behaves the same
on CI, where neither the marketplace registry nor the plugin cache exists.
"""

from __future__ import annotations

import collections
import glob
import json
import os
import re
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# The canonical forms. Declared here as INDEPENDENT expected values — copied
# from .superpowers/sdd/278-hooks-resolver/resolver-spec.md, not derived from
# the files under test. FORM A is quoted with `"` (its sites sit inside
# `python3 -c '...'`); FORM B/C with `'` (theirs sit inside `python3 -c "..."`).
# ---------------------------------------------------------------------------

# 56 sites: internal double quotes, tail `sys.path.insert`.
FORM_A = '''\
import glob, json, os, re, sys
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json"))).values():
            _i = (_m or {}).get("installLocation") if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            _h = os.path.join(_i, "hooks")
            if os.path.isfile(os.path.join(_h, "obsidian_utils.py")):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")) if re.fullmatch("[0-9]+([.][0-9]+)*", _d.split("/")[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split("/")[-2].split(".")], _p), default="hooks")
sys.path.insert(0, _ob_hooks())'''

# 2 sites (/check-items): same tail as FORM A, but the OUTER shell quote is
# double there, so the internal quotes must be single.
FORM_A_SINGLE = """\
import glob, json, os, re, sys
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser('~/.claude/plugins/known_marketplaces.json'))).values():
            _i = (_m or {}).get('installLocation') if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            _h = os.path.join(_i, 'hooks')
            if os.path.isfile(os.path.join(_h, 'obsidian_utils.py')):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser('~/.claude/plugins/cache/*/obsidian-brain/*/hooks')) if re.fullmatch('[0-9]+([.][0-9]+)*', _d.split('/')[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split('/')[-2].split('.')], _p), default='hooks')
sys.path.insert(0, _ob_hooks())"""

# 9 sites: `HOOKS=$(python3 -c "...")`, so the resolver PRINTS its answer
# instead of inserting it, and does not need `sys`.
FORM_B = """\
import glob, json, os, re
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser('~/.claude/plugins/known_marketplaces.json'))).values():
            _i = (_m or {}).get('installLocation') if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            _h = os.path.join(_i, 'hooks')
            if os.path.isfile(os.path.join(_h, 'obsidian_utils.py')):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser('~/.claude/plugins/cache/*/obsidian-brain/*/hooks')) if re.fullmatch('[0-9]+([.][0-9]+)*', _d.split('/')[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split('/')[-2].split('.')], _p), default='hooks')
print(_ob_hooks())"""

# 1 site (/vault-doctor's dispatcher): resolves a FILE under scripts/, so the
# version segment is at [-3] and the empty-string default fails the caller's
# `-f` guard rather than pointing at a directory.
FORM_C = """\
import glob, json, os, re
def _ob_doctor():
    try:
        for _m in json.load(open(os.path.expanduser('~/.claude/plugins/known_marketplaces.json'))).values():
            _i = (_m or {}).get('installLocation') if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            _h = os.path.join(_i, 'hooks')
            if os.path.isfile(os.path.join(_h, 'obsidian_utils.py')):
                _v = os.path.join(os.path.dirname(_h), 'scripts', 'vault_doctor.py')
                if os.path.isfile(_v):
                    return _v
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser('~/.claude/plugins/cache/*/obsidian-brain/*/scripts/vault_doctor.py')) if re.fullmatch('[0-9]+([.][0-9]+)*', _d.split('/')[-3])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split('/')[-3].split('.')], _p), default='')
print(_ob_doctor())"""

# The independently derived tally (see the plan's task-4 brief). EQUALITY, not
# a floor: 56 + 2 + 9 + 1 = 68.
EXPECTED_FORM_COUNTS = {
    FORM_A: 56,
    FORM_A_SINGLE: 2,
    FORM_B: 9,
    FORM_C: 1,
}
EXPECTED_SITE_COUNT = 68

# The resolver as it stood BEFORE #278 — cache-only, digit-scraping key, no
# allowlist. Verbatim from `git show 4d3458c:skills/recall/SKILL.md` line 26
# (4d3458c is this branch's merge base). Kept so the fail-first test can show
# the old form failing where the new one succeeds, rather than only asserting
# that the new one works.
PRE_278_RESOLVER = (
    'import glob, re; sys.path.insert(0, max(glob.glob(os.path.expanduser('
    '"~/.claude/plugins/cache/*/obsidian-brain/*/hooks")), key=lambda p: '
    '([int(n) for n in re.findall("[0-9]+", p.split("/")[-2])], p), '
    'default="hooks"))'
)

# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

_DEF_RE = re.compile(r"^(\s*)def (_ob_hooks|_ob_doctor)\(\):$")
_IMPORT_RE = re.compile(r"^\s*import glob, json, os, re(, sys)?$")
_TAIL_RE = re.compile(
    r"^\s*(sys\.path\.insert\(0, _ob_hooks\(\)\)|print\(_ob_(?:hooks|doctor)\(\)\))$"
)
# A resolver block is 12-13 lines; 30 is generous headroom without letting a
# runaway search swallow unrelated file content.
_MAX_BLOCK_LINES = 30


class Site(collections.namedtuple("Site", "skill lineno func text")):
    """One resolver copy, dedented by its own leading indent."""

    @property
    def label(self):
        return f"{self.skill}:{self.lineno}"


def _resolver_sites():
    """Every resolver copy across skills/*/SKILL.md.

    The block runs from the ``import`` line immediately above ``def _ob_*():``
    down to the first ``sys.path.insert``/``print`` tail. Each block is
    dedented by ITS OWN leading indent before being returned: these blocks are
    emitted at column 0 in some skills and indented inside a fenced step in
    others, and comparing raw text splits one family into several phantom ones.
    """
    sites = []
    for skill_path in sorted(glob.glob(os.path.join(_REPO_ROOT, "skills/*/SKILL.md"))):
        skill_name = skill_path.replace("\\", "/").split("/")[-2]
        with open(skill_path, encoding="utf-8") as f:
            lines = f.read().split("\n")
        for i, line in enumerate(lines):
            m = _DEF_RE.match(line)
            if not m:
                continue
            indent, func = m.group(1), m.group(2)
            start = i - 1
            assert start >= 0 and _IMPORT_RE.match(lines[start]), (
                f"{skill_name}/SKILL.md:{i + 1}: `def {func}():` is not preceded "
                f"by the canonical `import glob, json, os, re[, sys]` line "
                f"(found {lines[start - 1] if start >= 0 else '<start of file>'!r})"
            )
            end = next(
                (
                    j
                    for j in range(i, min(i + _MAX_BLOCK_LINES, len(lines)))
                    if _TAIL_RE.match(lines[j])
                ),
                None,
            )
            assert end is not None, (
                f"{skill_name}/SKILL.md:{i + 1}: `def {func}():` has no "
                f"`sys.path.insert(...)`/`print(...)` tail within "
                f"{_MAX_BLOCK_LINES} lines"
            )
            block = lines[start : end + 1]
            text = "\n".join(
                ln[len(indent) :] if ln.startswith(indent) else ln for ln in block
            )
            sites.append(Site(skill_name, start + 1, func, text))
    return sites


_SITES = _resolver_sites()


def _distinct_blocks():
    """``[(label, text, func)]`` — one entry per DISTINCT block text observed.

    The behavioural tests parametrize over this rather than over the canonical
    constants, so a site that was edited (and therefore forms its own family)
    is still executed and still has to satisfy every behavioural assertion.
    """
    out = []
    seen = {}
    for site in _SITES:
        if site.text in seen:
            continue
        seen[site.text] = True
        canonical = {
            FORM_A: "FORM_A",
            FORM_A_SINGLE: "FORM_A_SINGLE",
            FORM_B: "FORM_B",
            FORM_C: "FORM_C",
        }.get(site.text, f"NONCANONICAL@{site.label}")
        out.append((canonical, site.text, site.func))
    return out


_BLOCKS = _distinct_blocks()
_BLOCK_PARAMS = [pytest.param(t, f, id=lbl) for lbl, t, f in _BLOCKS]


# ---------------------------------------------------------------------------
# (a) Drift: byte identity + exact counts
# ---------------------------------------------------------------------------


def test_resolver_site_count_is_exactly_68():
    """EQUALITY, deliberately — 68 is derived independently (56 + 2 + 9 + 1),
    not read back from the scan. A ``>=`` floor here would let a deleted site,
    or a site reformatted past the extractor, pass silently."""
    assert len(_SITES) == EXPECTED_SITE_COUNT, (
        f"expected exactly {EXPECTED_SITE_COUNT} resolver sites, found "
        f"{len(_SITES)}: "
        + ", ".join(s.label for s in _SITES[:5])
        + " ... . A DROP means a site was deleted or edited past the extractor; "
        "a RISE means new sites were added. Either way, update this number "
        "deliberately."
    )


def test_expected_counts_are_self_consistent():
    """The two literals are independent of the scan, but not of each other."""
    assert EXPECTED_SITE_COUNT == sum(EXPECTED_FORM_COUNTS.values())


def test_no_cache_glob_lives_outside_an_extracted_resolver_block():
    """Cross-check the two scans against each other.

    ``_resolver_sites()`` keys on ``def _ob_hooks():`` / ``def _ob_doctor():``.
    A site that does not use that shape — a fresh copy of the PRE-#278
    one-liner, pasted in from an older skill or from documentation — is
    invisible to ``_SITES``, so ``== 68`` never moves and byte identity has
    nothing to compare. It also still contains ``key=lambda p:``, so the older
    lexicographic guard in test_skill_snippets.py is satisfied, and that
    module's site scan is a ``>= 67`` floor that only ever goes up. A 69th copy
    of the OLD resolver is the same rot arriving by the front door.

    So: every line mentioning the plugin cache must lie INSIDE the line span of
    an extracted canonical block. Sites are dedented but never re-wrapped, so
    ``text.count("\\n")`` is an exact span.
    """
    spans = {}
    for site in _SITES:
        spans.setdefault(site.skill, []).append(
            (site.lineno, site.lineno + site.text.count("\n"))
        )
    offenders = []
    for skill_path in sorted(glob.glob(os.path.join(_REPO_ROOT, "skills/*/SKILL.md"))):
        skill_name = skill_path.replace("\\", "/").split("/")[-2]
        with open(skill_path, encoding="utf-8") as f:
            for lineno, line in enumerate(f.read().split("\n"), 1):
                if "~/.claude/plugins/cache/*/obsidian-brain/" not in line:
                    continue
                if not any(a <= lineno <= b for a, b in spans.get(skill_name, [])):
                    offenders.append(f"{skill_name}:{lineno}")
    assert not offenders, (
        "plugin-cache glob outside a canonical resolver block: "
        + ", ".join(offenders)
        + " — a resolver was added that is not one of the 68 canonical copies. "
        "Use FORM A/B/C verbatim; a hand-rolled or pre-#278 one-liner is "
        "invisible to every other test in this module."
    )


def test_every_resolver_site_is_byte_identical_to_a_canonical_form():
    """No 'close enough' copies. 68 hand-maintained duplicates only stay safe
    while they are literally the same bytes."""
    canonical = set(EXPECTED_FORM_COUNTS)
    offenders = [s.label for s in _SITES if s.text not in canonical]
    assert not offenders, (
        "Resolver site(s) drifted from every canonical form: "
        + ", ".join(offenders)
        + ". Compare against FORM_A / FORM_A_SINGLE / FORM_B / FORM_C in this "
        "module (and .superpowers/sdd/278-hooks-resolver/resolver-spec.md); the "
        "difference is usually a quote character or a re-wrapped line."
    )


def test_canonical_form_family_counts_are_exact():
    """Each family's population is pinned, so moving a site between families —
    e.g. flipping one site's internal quote character — fails here even though
    the total stays at 68."""
    observed = collections.Counter(s.text for s in _SITES)
    names = {
        FORM_A: "FORM_A",
        FORM_A_SINGLE: "FORM_A_SINGLE",
        FORM_B: "FORM_B",
        FORM_C: "FORM_C",
    }
    # Aggregated, not a dict comprehension: several drifted families all map to
    # the "NONCANONICAL" key, and a comprehension would keep only the last
    # one's count — reporting `{'NONCANONICAL': 1}` for 68 drifted sites.
    got = collections.Counter()
    for text, n in observed.items():
        got[names.get(text, "NONCANONICAL")] += n
    got = dict(got)
    want = {names[text]: n for text, n in EXPECTED_FORM_COUNTS.items()}
    assert got == want, (
        f"resolver family populations changed: expected {want}, got {got}. "
        "56 = double-quoted sys.path.insert sites, 2 = /check-items' "
        "single-quoted sys.path.insert sites, 9 = FORM B print sites, "
        "1 = /vault-doctor's FORM C dispatcher."
    )


# ---------------------------------------------------------------------------
# (b) The A/B invariant, checked over the FILE-DERIVED text
# ---------------------------------------------------------------------------


def _body(text):
    """The resolver body: import line through ``return max(...)``, i.e.
    everything except the ``sys.path.insert``/``print`` tail."""
    return "\n".join(text.split("\n")[:-1])


def _observed(form):
    """Assert the family is PRESENT in the files, then hand back its text.

    Membership is checked against the scan, so returning the constant is
    equivalent to returning the file text — but only because of that check,
    which is why it is an assertion and not a convenience.

    It FAILS rather than skips when the family is missing. A skip reads as
    green in a summary line, and the tests that depend on this include the
    fail-first probe proof — the most load-bearing test in the module. The
    drift tests fail in the same run and say what actually drifted.
    """
    if form not in {s.text for s in _SITES}:
        pytest.fail("family absent — see the drift tests")
    return form


def test_form_bodies_are_identical_modulo_quote_character():
    """FORM B is FORM A with the quotes swapped and ``sys`` dropped — nothing
    else.

    The constants are compared, which is meaningful because ``_observed()``
    first asserts each family is present in the files byte-identically: a body
    that diverges in one family fails the drift test, and the family it belongs
    to then fails here.

    ``sys`` is the one legitimate asymmetry: FORM A needs it for
    ``sys.path``, FORM B (which prints) does not.
    """
    a = _body(_observed(FORM_A)).replace('"', "'")
    a_single = _body(_observed(FORM_A_SINGLE))
    b = _body(_observed(FORM_B))

    assert a == a_single, (
        "FORM_A and FORM_A_SINGLE bodies differ by more than the quote "
        "character"
    )
    assert a.replace("import glob, json, os, re, sys", "import glob, json, os, re") == b, (
        "FORM_B body is not FORM_A's body modulo quotes + the `sys` import"
    )


def test_form_c_is_the_scripts_variant_of_form_b():
    """FORM C legitimately differs (it resolves a FILE under ``scripts/`` and
    indexes the version at [-3]), but it must keep the two properties the whole
    fix rests on: the marketplace sentinel and the version allowlist."""
    c = _observed(FORM_C)
    assert "known_marketplaces.json" in c
    assert "obsidian_utils.py" in c, "FORM C must keep the hooks/ sentinel"
    assert "re.fullmatch('[0-9]+([.][0-9]+)*'" in c
    assert "[-3]" in c, "FORM C's version segment is at [-3], not [-2]"
    assert "default=''" in c, (
        "FORM C must default to the empty string so the caller's -f guard fires"
    )


# ---------------------------------------------------------------------------
# (d) THE CARRIED FINDING: semantics, not naming
# ---------------------------------------------------------------------------


def test_every_resolver_site_consults_install_location_before_the_cache():
    """Task 2's reviewer proved by mutation that
    ``assert "key=lambda _p:" in window`` (test_skill_snippets.py) is a NAMING
    proxy: a site rewritten back to cache-only globbing, with the lambda's
    variable name kept, passed all 91 tests.

    This test is the semantic one. It fails for exactly that mutation, because
    it requires the marketplace lookup to be PRESENT and to appear BEFORE the
    cache glob — a cache-only rewrite has no marketplace lookup at all, whatever
    its identifiers are named.
    """
    offenders = []
    for site in _SITES:
        text = site.text
        if "known_marketplaces.json" not in text:
            offenders.append(f"{site.label} (no marketplace lookup at all)")
            continue
        if "installLocation" not in text:
            offenders.append(f"{site.label} (no installLocation read)")
            continue
        if "obsidian_utils.py" not in text:
            offenders.append(f"{site.label} (no sentinel check)")
            continue
        if text.index("known_marketplaces.json") > text.index("plugins/cache/"):
            offenders.append(f"{site.label} (cache consulted before installLocation)")
    assert not offenders, (
        "Resolver site(s) do not consult installLocation before the plugin "
        "cache: " + ", ".join(offenders) + ". Cache-first keeps serving the "
        "stale cached module whenever one exists, which is the entire "
        "silent-failure class #278 exists to close."
    )


# ---------------------------------------------------------------------------
# Executable fixtures — everything below runs the resolver for real, against
# trees under tmp_path with $HOME redirected. Nothing touches the machine's
# real ~/.claude/plugins.
# ---------------------------------------------------------------------------

CACHE_GLOB_ROOT = ".claude/plugins/cache/obsidian-brain-repo/obsidian-brain"
MARKETPLACES = ".claude/plugins/known_marketplaces.json"


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """An empty $HOME, and a neutral working directory.

    ``os.path.expanduser("~")`` reads $HOME on POSIX, so every resolver path
    below lands inside tmp_path — no real marketplace registry, no real plugin
    cache, no dependence on this machine.

    The ``chdir`` is not cosmetic. The resolver's sentinel check is
    ``os.path.isfile(os.path.join(installLocation, "hooks", "obsidian_utils.py"))``
    and an absent/empty ``installLocation`` makes that a RELATIVE path — so
    running the suite from inside an obsidian-brain checkout changes what the
    resolver returns (see
    ``test_empty_install_location_makes_the_sentinel_cwd_relative``). Pinning
    the cwd keeps these tests measuring the resolver rather than the directory
    pytest happened to start in.
    """
    home = tmp_path / "home"
    (home / ".claude" / "plugins").mkdir(parents=True)
    neutral = tmp_path / "neutral-cwd"
    neutral.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.chdir(neutral)
    return home


def _resolver(text, func):
    """Compile a block's BODY (tail dropped) and hand back the function.

    The tail is dropped on purpose: FORM A's tail mutates the interpreter's own
    ``sys.path``, which would leak between tests.
    """
    namespace = {}
    exec(compile(_body(text), "<resolver-block>", "exec"), namespace)  # noqa: S102
    return namespace[func]


def _seed_cache(home, version, func):
    """Create a cached install of ``version`` and return the path the resolver
    should produce for it."""
    base = home / CACHE_GLOB_ROOT / version
    if func == "_ob_doctor":
        (base / "scripts").mkdir(parents=True)
        target = base / "scripts" / "vault_doctor.py"
        target.write_text("", encoding="utf-8")
        return str(target)
    (base / "hooks").mkdir(parents=True)
    (base / "hooks" / "obsidian_utils.py").write_text("", encoding="utf-8")
    return str(base / "hooks")


def _seed_install_location(home, func, *, with_doctor=True, key="user-chosen-name"):
    """Register a directory-source marketplace whose installLocation holds a
    real checkout, and return the path the resolver should produce.

    The registry key is deliberately NOT the plugin name: it is user-chosen
    (``obsidian-brain-repo`` on the author's machine), and a resolver that
    keyed off it would break for everyone else.
    """
    checkout = home / "checkout"
    (checkout / "hooks").mkdir(parents=True)
    (checkout / "hooks" / "obsidian_utils.py").write_text("", encoding="utf-8")
    expected = str(checkout / "hooks")
    if func == "_ob_doctor":
        (checkout / "scripts").mkdir(parents=True)
        expected = str(checkout / "scripts" / "vault_doctor.py")
        if with_doctor:
            (checkout / "scripts" / "vault_doctor.py").write_text("", encoding="utf-8")
    (home / MARKETPLACES).write_text(
        json.dumps({key: {"installLocation": str(checkout)}}), encoding="utf-8"
    )
    return expected


def _default_for(func):
    return "" if func == "_ob_doctor" else "hooks"


# The seven sibling names from the plan's D2 table. Six are NOT canonical
# version directories and the allowlist must drop them; `3.3.10` is handled
# separately below because it IS one (see that test's docstring).
NON_CANONICAL_SIBLINGS = [
    "3.3.1.bak",
    "3.3.1-old",
    "3.3.1a",
    "3.3.1~",
    "3.3.1_bak",
    "3.4.0.bak",
]


# ---------------------------------------------------------------------------
# (b) Allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
@pytest.mark.parametrize("sibling", NON_CANONICAL_SIBLINGS)
def test_allowlist_excludes_non_canonical_sibling(text, func, sibling, fake_home):
    """A real 3.3.1 beside one junk sibling must still resolve to 3.3.1.

    Two of these six (``3.3.1.bak``, ``3.3.1-old``) resolved "correctly" under
    the OLD resolver too — but only by ASCII accident, because ``/`` (0x2F)
    sorts above ``.`` (0x2E) and ``-`` (0x2D) in the tiebreak. They are here so
    the accident is not mistaken for a guarantee;
    ``test_allowlist_rejects_a_cache_of_nothing_but_junk`` is the case that
    fails for them when the allowlist is removed.
    """
    real = _seed_cache(fake_home, "3.3.1", func)
    _seed_cache(fake_home, sibling, func)
    assert _resolver(text, func)() == real


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
def test_allowlist_excludes_all_seven_siblings_at_once(text, func, fake_home):
    """The full D2 trap set in one cache. ``3.3.10`` is excluded from this
    fixture and covered by its own test: unlike the other six it is a genuine
    version name."""
    real = _seed_cache(fake_home, "3.3.1", func)
    for sibling in NON_CANONICAL_SIBLINGS:
        _seed_cache(fake_home, sibling, func)
    assert _resolver(text, func)() == real


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
def test_allowlist_rejects_a_cache_of_nothing_but_junk(text, func, fake_home):
    """With no canonical version present at all, the resolver must fall back to
    its default rather than return a junk directory.

    This is the case that gives the two "ASCII accident" siblings teeth: with
    the allowlist deleted, ``3.3.1.bak``/``3.3.1-old`` are the max and get
    returned, and this assertion is what catches it.
    """
    for sibling in NON_CANONICAL_SIBLINGS:
        _seed_cache(fake_home, sibling, func)
    assert _resolver(text, func)() == _default_for(func)


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
def test_allowlist_admits_a_genuine_higher_version(text, func, fake_home):
    """``3.3.10`` — the seventh D2 sibling — DIVERGES from the other six.

    The plan's D2 table lists it as a trap because under the old digit-scraping
    key it outranked ``3.3.1``. But ``3.3.10`` is a well-formed version
    directory: ``[0-9]+([.][0-9]+)*`` admits it, and outranking 3.3.1 is the
    CORRECT answer for a genuinely newer release, not a defect. Excluding it
    would mean banning every two-digit patch. Pinned as a test so the
    divergence from the table is deliberate and visible.
    """
    _seed_cache(fake_home, "3.3.1", func)
    newer = _seed_cache(fake_home, "3.3.10", func)
    assert _resolver(text, func)() == newer


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
def test_version_ordering_is_numeric_not_lexicographic(text, func, fake_home):
    """The #274 regression: ``max()`` over strings puts 3.9.0 above 3.10.0."""
    _seed_cache(fake_home, "3.9.0", func)
    newer = _seed_cache(fake_home, "3.10.0", func)
    assert _resolver(text, func)() == newer


# ---------------------------------------------------------------------------
# installLocation ordering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
def test_install_location_beats_a_newer_cache(text, func, fake_home):
    """The ordering IS the fix. A 9.9.9 cache outranks every real release, so
    if this returns the cache the resolver is cache-first — the exact
    silent-failure mode #278 closes."""
    _seed_cache(fake_home, "9.9.9", func)
    expected = _seed_install_location(fake_home, func)
    assert _resolver(text, func)() == expected


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
def test_cache_is_used_when_no_marketplace_registry_exists(text, func, fake_home):
    """github-source installs have no directory entry, so the marketplace loop
    finds nothing and must fall through rather than blow up."""
    expected = _seed_cache(fake_home, "3.3.1", func)
    assert not (fake_home / MARKETPLACES).exists()
    assert _resolver(text, func)() == expected


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
def test_install_location_without_the_sentinel_falls_through(text, func, fake_home):
    """An installLocation pointing at some OTHER plugin's checkout must not
    hijack resolution: no hooks/obsidian_utils.py, no match."""
    expected = _seed_cache(fake_home, "3.3.1", func)
    stranger = fake_home / "some-other-plugin"
    stranger.mkdir()
    (fake_home / MARKETPLACES).write_text(
        json.dumps({"other": {"installLocation": str(stranger)}}), encoding="utf-8"
    )
    assert _resolver(text, func)() == expected


def test_doctor_install_location_without_vault_doctor_falls_through(fake_home):
    """FORM C only: an install whose hooks/ exists but whose
    scripts/vault_doctor.py does not must fall through to the cache instead of
    returning a path that fails the caller's ``-f`` guard."""
    text = _observed(FORM_C)
    expected = _seed_cache(fake_home, "3.3.1", "_ob_doctor")
    _seed_install_location(fake_home, "_ob_doctor", with_doctor=False)
    assert _resolver(text, "_ob_doctor")() == expected


MALFORMED_REGISTRIES = {
    "not-json": "}{ not json at all",
    "empty-file": "",
    "top-level-list": "[]",
    "null-entry": '{"mp": null}',
    "entry-is-int": '{"mp": 7}',
    "entry-is-list": '{"mp": []}',
    "missing-key": '{"mp": {}}',
    "install-location-null": '{"mp": {"installLocation": null}}',
    "install-location-int": '{"mp": {"installLocation": 3}}',
}


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
@pytest.mark.parametrize("shape", sorted(MALFORMED_REGISTRIES))
def test_resolver_never_raises_on_a_malformed_registry(text, func, shape, fake_home):
    """These blocks run inside a bash `$(...)`/`python3 -c` at the top of a
    skill step. An exception here is not a graceful degradation — it is an
    opaque traceback in place of the skill."""
    expected = _seed_cache(fake_home, "3.3.1", func)
    (fake_home / MARKETPLACES).write_text(
        MALFORMED_REGISTRIES[shape], encoding="utf-8"
    )
    assert _resolver(text, func)() == expected


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
def test_empty_install_location_falls_through_to_the_cache(
    text, func, fake_home, tmp_path, monkeypatch
):
    """An entry with no usable ``installLocation`` must be SKIPPED — resolution
    may never depend on the caller's working directory.

    ``os.path.join("", "hooks")`` is the relative string ``"hooks"``, so a
    sentinel check built on it is evaluated against the cwd. Run from inside
    any obsidian-brain checkout — which is where a developer runs these skills
    — that file exists, so the loop would return a relative path and the cache
    would never be consulted. FORM C is the sharp end: it deliberately deleted
    a ``$(pwd)/scripts/vault_doctor.py`` fallback because cwd-coupling is a
    bug, and this would have let the same coupling back in through the front
    door.

    The ``isabs`` guard in the resolver is what makes this pass. The
    cwd-on-a-checkout construction below is the whole point of the test: with
    a neutral cwd the assertion holds for the wrong reason (nothing matches
    either way), and the regression walks straight back in.
    """
    cwd_checkout = tmp_path / "cwd-checkout"
    (cwd_checkout / "hooks").mkdir(parents=True)
    (cwd_checkout / "hooks" / "obsidian_utils.py").write_text("", encoding="utf-8")
    (cwd_checkout / "scripts").mkdir()
    (cwd_checkout / "scripts" / "vault_doctor.py").write_text("", encoding="utf-8")
    (fake_home / MARKETPLACES).write_text('{"mp": {}}', encoding="utf-8")
    expected = _seed_cache(fake_home, "3.3.1", func)

    monkeypatch.chdir(cwd_checkout)
    assert _resolver(text, func)() == expected


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
def test_relative_install_location_falls_through_to_the_cache(
    text, func, fake_home, tmp_path, monkeypatch
):
    """Same hazard, stated the other way: a RELATIVE ``installLocation`` is not
    usable either, because what it names depends on where the skill was
    invoked from."""
    cwd_checkout = tmp_path / "cwd-checkout"
    (cwd_checkout / "relative-install" / "hooks").mkdir(parents=True)
    (cwd_checkout / "relative-install" / "hooks" / "obsidian_utils.py").write_text(
        "", encoding="utf-8"
    )
    (cwd_checkout / "relative-install" / "scripts").mkdir()
    (cwd_checkout / "relative-install" / "scripts" / "vault_doctor.py").write_text(
        "", encoding="utf-8"
    )
    (fake_home / MARKETPLACES).write_text(
        json.dumps({"mp": {"installLocation": "relative-install"}}), encoding="utf-8"
    )
    expected = _seed_cache(fake_home, "3.3.1", func)

    monkeypatch.chdir(cwd_checkout)
    assert _resolver(text, func)() == expected


# Multi-entry registries. Every shape in MALFORMED_REGISTRIES is single-entry,
# and that is exactly what hid the loop-abort defect: with one entry there is
# no way to tell "skip the bad entry and keep looking" from "give up on the
# whole registry". A real registry has one entry per installed marketplace (9
# on the author's machine), in an order obsidian-brain does not control — so a
# third-party entry sorted first must not be able to shadow ours.
MULTI_ENTRY_REGISTRIES = {
    "bad-then-good": {"aaa-third-party": {"installLocation": None}},
    "int-then-good": {"aaa-third-party": 7},
    "null-then-good": {"aaa-third-party": None},
    "list-then-good": {"aaa-third-party": []},
    "no-key-then-good": {"aaa-third-party": {}},
    "relative-then-good": {"aaa-third-party": {"installLocation": "some/where"}},
}


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
@pytest.mark.parametrize("shape", sorted(MULTI_ENTRY_REGISTRIES))
def test_a_bad_entry_does_not_shadow_a_later_good_one(
    text, func, shape, fake_home
):
    """C2 regression guard. ``json.load`` preserves insertion order, so the bad
    entry below is iterated FIRST.

    With the whole ``for`` loop inside one ``try``, the first entry that raises
    (``os.path.join(None, "hooks")`` → TypeError, ``(7).get`` →
    AttributeError) aborts iteration over every remaining entry and drops
    straight to the cache — cache-first resolution restored silently, by
    someone else's plugin, with no obsidian-brain misconfiguration involved.
    That is this PR's own thesis failing.
    """
    _seed_cache(fake_home, "9.9.9", func)  # would win if the loop aborted
    expected = _seed_install_location(fake_home, func)
    registry = json.loads((fake_home / MARKETPLACES).read_text(encoding="utf-8"))
    ordered = dict(MULTI_ENTRY_REGISTRIES[shape])
    ordered.update(registry)
    assert list(ordered)[0] != "user-chosen-name", "bad entry must be iterated first"
    (fake_home / MARKETPLACES).write_text(json.dumps(ordered), encoding="utf-8")

    assert _resolver(text, func)() == expected


# ---------------------------------------------------------------------------
# (c) Fail-first: the probe module the OLD resolver cannot see
# ---------------------------------------------------------------------------


def _probe_tree(tmp_path):
    """A checkout carrying ``_probe_278.py`` beside a cache that does not.

    This is the shape of the real failure: a module that exists in the working
    tree but has not been released, so a cache-only resolver imports the stale
    tree and never sees it.
    """
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    (checkout / "hooks").mkdir(parents=True)
    (checkout / "hooks" / "obsidian_utils.py").write_text("", encoding="utf-8")
    (checkout / "hooks" / "_probe_278.py").write_text(
        'VALUE = "from-checkout"\n', encoding="utf-8"
    )
    cache = home / CACHE_GLOB_ROOT / "3.3.1" / "hooks"
    cache.mkdir(parents=True)
    (cache / "obsidian_utils.py").write_text("", encoding="utf-8")
    (home / ".claude" / "plugins" / "known_marketplaces.json").write_text(
        json.dumps({"user-chosen-name": {"installLocation": str(checkout)}}),
        encoding="utf-8",
    )
    return home, checkout, cache


def _run_resolver_script(script, home, cwd):
    env = dict(os.environ, HOME=str(home))
    env.pop("USERPROFILE", None)
    # PYTHONPATH would put the real checkout's hooks/ on the child's path and
    # let the OLD resolver import the probe it is supposed to miss.
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
        timeout=60,
    )


def test_pre_278_resolver_cannot_import_the_probe(tmp_path):
    """Fail-first half 1: the OLD (cache-only) resolver resolves to the cache
    and the probe import dies there.

    ``import os, sys`` is prepended because the pre-#278 sites relied on an
    earlier line in their own snippet for those imports; the resolver
    expression itself is verbatim from the merge base.
    """
    home, _checkout, cache = _probe_tree(tmp_path)
    script = f"import os, sys\n{PRE_278_RESOLVER}\nprint(sys.path[0])\nimport _probe_278\n"
    result = _run_resolver_script(script, home, tmp_path)
    assert result.stdout.strip() == str(cache), (
        f"expected the OLD resolver to land in the cache, got {result.stdout!r}"
    )
    assert result.returncode != 0
    assert "_probe_278" in result.stderr and "No module named" in result.stderr


def test_canonical_resolver_imports_the_probe_from_the_checkout(tmp_path):
    """Fail-first half 2: the shipped FORM A block — executed verbatim,
    ``sys.path.insert`` tail and all — resolves the checkout and imports the
    module the cache does not have."""
    home, checkout, _cache = _probe_tree(tmp_path)
    script = f"{_observed(FORM_A)}\nimport _probe_278\nprint(_probe_278.VALUE)\n"
    result = _run_resolver_script(script, home, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "from-checkout", result.stdout
    assert (checkout / "hooks" / "_probe_278.py").is_file()


def test_form_b_prints_the_checkout_hooks_dir(tmp_path):
    """FORM B's contract is its STDOUT: `HOOKS=$(python3 -c "...")` in bash.
    Run it the way the shell runs it."""
    home, checkout, _cache = _probe_tree(tmp_path)
    result = _run_resolver_script(_observed(FORM_B), home, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(checkout / "hooks")


def test_probe_is_not_committed_to_the_repo():
    """The probe is a scratch artifact. If one is ever left behind in hooks/ it
    ships to every user through the plugin cache."""
    assert not os.path.exists(os.path.join(_REPO_ROOT, "hooks", "_probe_278.py"))
