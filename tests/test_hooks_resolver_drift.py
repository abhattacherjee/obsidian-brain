"""Drift + behaviour guards for the 71 hand-copied resolvers (#278, #287).

Every ``skills/*/SKILL.md`` bash block is its own shell, so the resolver that
puts ``hooks/`` on ``sys.path`` cannot be hoisted into one shared definition —
it is copied verbatim into 71 sites. This module is what makes that duplication
safe. It has two halves:

**Drift (static).** Each site is extracted from the file, normalised by *its own
leading indent* (the blocks sit at different indentation depths, and comparing
un-normalised text invents spurious families), and compared **byte for byte**
against a canonical form declared here. The site count is asserted by
**equality**: a ``>=`` floor would let a silently deleted site pass.

**Behaviour (executed).** The canonical forms are not just compared as text —
every distinct block observed in the files is ``exec``'d against fixture trees
under ``tmp_path`` with ``$HOME`` redirected, and asserted to (a) prefer a
**directory-source** marketplace ``installLocation`` over any cache, (b) ignore
a github-source entry even when its clone satisfies the sentinel, (c) reject
non-canonical sibling directories, and (d) never raise on a malformed
``known_marketplaces.json``. Executing the *file-derived* text (rather than the
constants below) is deliberate: a site edited to keep the resolver's variable
names while gutting its logic still gets run, and still fails.

(b) is not decoration. obsidian-brain's ``.claude-plugin/marketplace.json``
declares ``"source": "./"``, so the marketplace repo IS the plugin repo and a
github-source clone under ``~/.claude/plugins/marketplaces/<name>`` carries
``hooks/obsidian_utils.py`` at its root. The sentinel alone cannot tell a clone
from a checkout; the ``source.source == "directory"`` discriminator can, and it
is what keeps github-source installs — every external user — resolving the
cache exactly as they did before #278.

Fixtures that mean to exercise the ``installLocation``/``isabs`` guards
therefore carry a valid directory ``source``. Without it the discriminator
stands in front and skips the entry first, and the inner guard becomes
untestable while its test still passes.

That last point is the carried finding from Task 2's review, which proved by
mutation that ``assert "key=lambda _p:" in window`` is a naming proxy: a site
rewritten back to cache-only globbing, with the lambda's name untouched, passed
the entire suite. ``test_every_resolver_site_consults_install_location_before_the_cache``
is the test that now fails for that exact mutation.

**Two resolver FAMILIES, deliberately not merged.** FORM A/A_SINGLE/B/C answer
"where is the *installed* plugin tree?" and therefore fall back to the plugin
cache. FORM D (#287, ``/dev-test``) answers a different question — "where is
the local *checkout* I can copy *from*?" — and per that plan's D3 the cache is
NOT a legal answer: ``scripts/test-dev-skill.sh`` derives its ``REPO_ROOT``
from its own location, so resolving it out of the cache makes ``install`` copy
the cache onto itself and print a full success transcript for a no-op. FORM D
therefore has no cache branch at all and returns ``''`` so its shell caller can
fail loudly. The behavioural parametrisations are split accordingly
(``_BLOCK_PARAMS`` vs ``_REPO_BLOCK_PARAMS``); the split is by ``site.func``,
not by canonical text, so a *drifted* FORM D copy is still executed against the
FORM D assertions rather than escaping both sets.
``test_block_partitions_cover_every_distinct_block`` pins that no block falls
between the two.

Nothing here reads this machine's real ``~/.claude`` — every fixture is built
under ``tmp_path`` with ``$HOME`` monkeypatched, so the suite behaves the same
on CI, where neither the marketplace registry nor the plugin cache exists.
That matters twice as much for FORM D: this machine has a directory-source
registration pointing at *this very checkout*, so the registry route and the
``git rev-parse --show-toplevel`` fallback return the SAME path here. A test
that only asserted "the resolver found the checkout" would pass with either
layer deleted. Every FORM D case below is built so the two routes disagree.
"""

from __future__ import annotations

import collections
import glob
import json
import os
import re
import shutil
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
            _s = _m.get("source") if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get("source") == "directory"):
                continue
            _i = _m.get("installLocation") if isinstance(_m, dict) else None
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
            _s = _m.get('source') if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get('source') == 'directory'):
                continue
            _i = _m.get('installLocation') if isinstance(_m, dict) else None
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
            _s = _m.get('source') if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get('source') == 'directory'):
                continue
            _i = _m.get('installLocation') if isinstance(_m, dict) else None
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
            _s = _m.get('source') if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get('source') == 'directory'):
                continue
            _i = _m.get('installLocation') if isinstance(_m, dict) else None
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

# 3 sites (/dev-test steps 2-4): the REPO-ROOT family (#287). Copied from
# docs/plans/287-dev-test-repo-root.md's canonical FORM_D listing. It differs
# from FORM A/B/C in three deliberate ways, each pinned by a test below:
#   * the sentinel is `scripts/test-dev-skill.sh`, not `hooks/obsidian_utils.py`
#     — it is looking for a checkout to copy FROM, not an installed tree;
#   * it returns the installLocation ROOT, not a subdirectory of it;
#   * it has NO plugin-cache fallback and defaults to `''` (D3 — resolving the
#     cache here would make `/dev-test install` copy the cache onto itself).
# Single-quoted internals: its sites sit inside `python3 -c "..."` (D5).
FORM_D = """\
import json, os
def _ob_repo():
    try:
        for _m in json.load(open(os.path.expanduser('~/.claude/plugins/known_marketplaces.json'))).values():
            _s = _m.get('source') if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get('source') == 'directory'):
                continue
            _i = _m.get('installLocation') if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            if os.path.isfile(os.path.join(_i, 'scripts', 'test-dev-skill.sh')):
                return _i
    except Exception:
        pass
    return ''
print(_ob_repo())"""

# The independently derived tally (see the plan's task-4 brief, and #287's
# Global Constraints). EQUALITY, not a floor: 56 + 2 + 9 + 1 + 3 = 71.
EXPECTED_FORM_COUNTS = {
    FORM_A: 56,
    FORM_A_SINGLE: 2,
    FORM_B: 9,
    FORM_C: 1,
    FORM_D: 3,
}
EXPECTED_SITE_COUNT = 71

#: Every canonical form, mapped to its constant NAME. One table, consulted by
#: both ``_distinct_blocks()`` and ``test_canonical_form_family_counts_are_exact``
#: — they used to carry separate copies, and a form added to one but not the
#: other reports as ``NONCANONICAL`` in exactly one of them.
FORM_NAMES = {
    FORM_A: "FORM_A",
    FORM_A_SINGLE: "FORM_A_SINGLE",
    FORM_B: "FORM_B",
    FORM_C: "FORM_C",
    FORM_D: "FORM_D",
}

#: The resolver function name that marks the repo-root family. ``site.func`` —
#: not canonical text — is what routes a block to the FORM D behavioural
#: assertions, so a DRIFTED dev-test copy is still executed against them.
REPO_FUNC = "_ob_repo"

#: FORM D's sentinel, relative to the installLocation root.
DEV_TEST_SENTINEL = ("scripts", "test-dev-skill.sh")

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

# `_ob_repo` (#287) is here for the reason D4 spells out: a resolver named
# anything this pattern does not list is INVISIBLE to `_SITES`, so the `== 71`
# equality never moves, byte identity has nothing to compare, and hand-copies
# ship unguarded. That is finding C1 from #278's final review.
_DEF_RE = re.compile(r"^(\s*)def (_ob_hooks|_ob_doctor|_ob_repo)\(\):$")
# FORM D needs neither `glob` nor `re` (no cache glob, no version allowlist),
# so its import line is its own alternative rather than a loosened optional.
_IMPORT_RE = re.compile(r"^\s*(?:import glob, json, os, re(?:, sys)?|import json, os)$")
_TAIL_RE = re.compile(
    r"^\s*(sys\.path\.insert\(0, _ob_hooks\(\)\)|print\(_ob_(?:hooks|doctor|repo)\(\)\))$"
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
                f"by a canonical import line (`import glob, json, os, re[, sys]` "
                f"for FORM A/B/C, `import json, os` for FORM D) "
                f"(found {lines[start] if start >= 0 else '<start of file>'!r})"
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
        canonical = FORM_NAMES.get(site.text, f"NONCANONICAL@{site.label}")
        out.append((canonical, site.text, site.func))
    return out


_BLOCKS = _distinct_blocks()
# Split by FUNCTION NAME, not by canonical text. The two families answer
# different questions (see the module docstring) and cannot satisfy each
# other's assertions: FORM A/B/C must fall back to the plugin cache, FORM D
# must never touch it. Keying on `func` means a DRIFTED dev-test block — one
# that is no longer byte-identical to FORM_D — still lands in
# `_REPO_BLOCK_PARAMS` and still has to pass every FORM D behaviour test,
# rather than silently escaping both parametrisations.
_BLOCK_PARAMS = [
    pytest.param(t, f, id=lbl) for lbl, t, f in _BLOCKS if f != REPO_FUNC
]
_REPO_BLOCK_PARAMS = [
    pytest.param(t, f, id=lbl) for lbl, t, f in _BLOCKS if f == REPO_FUNC
]


def test_block_partitions_cover_every_distinct_block():
    """Guard on the split itself.

    An empty ``parametrize`` list does not fail — pytest simply collects no
    cases — so a typo in ``REPO_FUNC``, or a family whose function is renamed,
    would delete a whole behavioural parametrisation while the summary line
    still reads green. Both partitions must be non-empty and must together
    account for every distinct block.
    """
    assert _BLOCK_PARAMS, "no cache-family blocks — the behavioural suite is empty"
    assert _REPO_BLOCK_PARAMS, "no repo-root blocks — FORM D behaviour is untested"
    assert len(_BLOCK_PARAMS) + len(_REPO_BLOCK_PARAMS) == len(_BLOCKS)


# ---------------------------------------------------------------------------
# (a) Drift: byte identity + exact counts
# ---------------------------------------------------------------------------


def test_resolver_site_count_is_exactly_71():
    """EQUALITY, deliberately — 71 is derived independently (56 + 2 + 9 + 1 + 3),
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
    invisible to ``_SITES``, so ``== 71`` never moves and byte identity has
    nothing to compare. It also still contains ``key=lambda p:``, so the older
    lexicographic guard in test_skill_snippets.py is satisfied, and that
    module's site scan is a ``>= 67`` floor that only ever goes up. A 72nd copy
    of the OLD resolver is the same rot arriving by the front door.

    So: every line mentioning the plugin cache must lie INSIDE the line span of
    an extracted canonical block. Sites are dedented but never re-wrapped, so
    ``text.count("\\n")`` is an exact span.

    **Known limit.** This keys on the literal string
    ``~/.claude/plugins/cache/*/obsidian-brain/``. A hand-rolled resolver
    written as
    ``os.path.join(os.path.expanduser("~/.claude/plugins/cache"), ...)``, or
    the ``find "$CACHE_DIR" -maxdepth 2 -name hooks`` shape that this branch
    documents as having evaded an earlier audit, contains no such literal and
    slips straight past. The literal is a good key for the shape the 68
    cache-family copies actually use, not a general-purpose detector. (FORM D
    has no cache glob by design, so it contributes nothing to find here — the
    guard that keeps it cache-free is
    ``test_every_resolver_site_consults_install_location_before_the_cache``.)
    ``test_no_scripts_file_reaches_the_cache_without_the_registry`` covers
    ``scripts/**`` with a deliberately shape-agnostic key (any ``plugins/cache``
    mention) for exactly this reason.
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
        + " — a resolver was added that is not one of the 71 canonical copies. "
        "Use FORM A/B/C verbatim; a hand-rolled or pre-#278 one-liner is "
        "invisible to every other test in this module."
    )


# Files under scripts/ whose ENTIRE job is validating what `/dev-test install`
# wrote into the plugin cache. Resolving the checkout in these would defeat
# them, so they are exempt by design, not by oversight. Each is asserted to
# exist below, so a rename cannot leave a dead exemption behind that silently
# widens as the path is reused.
CACHE_ONLY_SCRIPTS = {
    # Writes the cache; must find it the way Claude Code laid it out.
    "scripts/test-dev-skill.sh",
    # Assert on what the install put in the cache.
    "scripts/dev-test/test-issue-101-manual.sh",
    "scripts/dev-test/test-issue-105-manual.sh",
    "scripts/dev-test/test-snapshots-manual.sh",
    "scripts/dev-test/test-vault-doctor-snapshots-manual.sh",
    # Manual checklists whose verification steps grep the INSTALLED tree.
    "scripts/dev-test/DEV-TEST-ISSUE-105.md",
    "scripts/dev-test/DEV-TEST-ISSUE-125.md",
}


def _scripts_files():
    for dirpath, dirnames, filenames in os.walk(os.path.join(_REPO_ROOT, "scripts")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


def test_cache_only_script_allowlist_has_no_dead_entries():
    """An allowlist that outlives its files stops being an exemption and starts
    being a hole: the next file to land on that path inherits the pass."""
    missing = sorted(
        p for p in CACHE_ONLY_SCRIPTS if not os.path.isfile(os.path.join(_REPO_ROOT, p))
    )
    assert not missing, (
        "CACHE_ONLY_SCRIPTS names files that no longer exist: "
        + ", ".join(missing)
        + " — delete the entry or fix the path."
    )


def test_no_scripts_file_reaches_the_cache_without_the_registry():
    """The 71 SKILL.md copies are pinned byte-for-byte; the ~13 copies under
    ``scripts/`` cannot be, so this is the weaker invariant that still bites.

    Byte identity is impossible there — different defaults, different path
    indices ([-2] vs [-3] vs [-4]), Python and shell hosts — but the property
    that matters survives: a file that reaches into the plugin cache must
    consult the marketplace registry too, and the resolver-shaped references
    must consult it FIRST. Without this, the argument that made 71 hand-copies
    acceptable ("the mitigation is a drift test") simply did not extend to
    them.

    Two keys, deliberately different:

    * **Presence** is keyed on any ``plugins/cache`` mention — shape-agnostic,
      so it catches the ``find``/``ls -dt`` shapes that the literal-glob check
      above cannot see.
    * **Ordering** is keyed on the literal resolver glob
      ``plugins/cache/*/obsidian-brain/``. The wildcard marketplace segment is
      what makes a reference a *resolver* rather than a concrete path typed
      into a manual `ls`, and only resolvers have an ordering to get wrong.

    Ordering is checked **per occurrence, not per file**. The earlier form
    compared every cache glob against one file-wide "first registry mention"
    offset, which meant a file that already carried one compliant resolver
    laundered every later one: a second, cache-only resolver appended to
    ``scripts/test-security.sh`` sat after that offset and passed. Here each
    cache glob must CLAIM its own preceding, not-yet-claimed registry mention.
    The canonical forms carry exactly one of each per block, so the pairing is
    1:1 per resolver and a registry-less resolver anywhere in the file — first,
    last, or wedged between two compliant ones — has nothing left to claim.
    """
    glob_key = "plugins/cache/*/obsidian-brain/"
    registry_key = "known_marketplaces.json"
    missing_registry = []
    cache_first = []
    for path in _scripts_files():
        rel = os.path.relpath(path, _REPO_ROOT).replace("\\", "/")
        if rel in CACHE_ONLY_SCRIPTS:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        if "plugins/cache" not in text:
            continue
        if registry_key not in text:
            missing_registry.append(rel)
            continue
        # Every occurrence of both keys, merged into one offset-ordered walk.
        # `str.find` in a loop rather than `text.index`: several of these files
        # carry the identical glob line three times, and `index` would report
        # the first one's offset for all three.
        events = []
        for kind, needle in ((0, registry_key), (1, glob_key)):
            at = text.find(needle)
            while at >= 0:
                events.append((at, kind))
                at = text.find(needle, at + 1)
        # kind 0 sorts before kind 1 at an equal offset, which cannot happen
        # for these two distinct needles but keeps the walk total-ordered.
        unclaimed = 0
        for offset, kind in sorted(events):
            if kind == 0:
                unclaimed += 1
            elif unclaimed:
                unclaimed -= 1
            else:
                cache_first.append(f"{rel}:{text.count(chr(10), 0, offset) + 1}")
    assert not missing_registry, (
        "scripts/ file(s) resolve the plugin cache with no marketplace lookup "
        "at all: " + ", ".join(sorted(missing_registry)) + " — port FORM A/B/C "
        "into them, or add them to CACHE_ONLY_SCRIPTS with a reason if their "
        "entire job is validating what `/dev-test install` wrote into the cache."
    )
    assert not cache_first, (
        "scripts/ file(s) glob the plugin cache with no preceding, unclaimed "
        "known_marketplaces.json lookup of its own: "
        + ", ".join(sorted(cache_first))
        + " — cache-first keeps serving the stale released tree whenever one "
        "exists, which is the whole of #278. An earlier compliant resolver in "
        "the same file does NOT cover a later cache-only one; each resolver "
        "needs its own registry lookup."
    )


# The shapes S-002 fixed: a shell expansion sitting inside a quoted Python
# string literal, in a file whose path now comes from the registry.
_INTERPOLATION_SHAPES = (
    "sys.path.insert(0, '$",
    'sys.path.insert(0, "$',
    "open('$",
    'open("$',
)


def test_no_converted_script_interpolates_a_resolved_path_into_python():
    """CLAUDE.md: "No path interpolation in ``python3 -c``: always pass paths
    via ``sys.argv``, never as string literals in the source code."

    Scoped to scripts/ files that consult ``known_marketplaces.json``, because
    those are precisely the files whose path variable this PR re-sourced. Before
    #278 the value came from a ``$HOME``-rooted ``find`` and could be argued to
    be well-formed by construction; now it is an arbitrary ``installLocation``
    string read out of a JSON file the plugin does not own. A path containing a
    single quote turns the interpolated program into a ``SyntaxError`` — the
    whole embedded block dies, and in ``scripts/test-security.sh`` that reads as
    a security test FAILING rather than as a broken harness.

    The cache-only scripts are exempt for the same reason they are exempt from
    the registry guard: their path is a cache directory they laid out
    themselves, un-widened by this PR.
    """
    offenders = []
    for path in _scripts_files():
        rel = os.path.relpath(path, _REPO_ROOT).replace("\\", "/")
        if rel in CACHE_ONLY_SCRIPTS:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        if "known_marketplaces.json" not in text:
            continue
        for lineno, line in enumerate(text.split("\n"), 1):
            if any(shape in line for shape in _INTERPOLATION_SHAPES):
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "shell variable interpolated into embedded Python source: "
        + ", ".join(offenders)
        + " — pass the path as an argv entry instead (`sys.argv[1]` in the "
        "block, `\" \"$VAR\"` on the block's closing line). The resolved path "
        "is an installLocation read out of known_marketplaces.json, so a quote "
        "in it is a SyntaxError, not a hypothetical."
    )


def test_every_resolver_site_is_byte_identical_to_a_canonical_form():
    """No 'close enough' copies. 71 hand-maintained duplicates only stay safe
    while they are literally the same bytes."""
    canonical = set(EXPECTED_FORM_COUNTS)
    offenders = [s.label for s in _SITES if s.text not in canonical]
    assert not offenders, (
        "Resolver site(s) drifted from every canonical form: "
        + ", ".join(offenders)
        + ". Compare against FORM_A / FORM_A_SINGLE / FORM_B / FORM_C / FORM_D "
        "in this module (and .superpowers/sdd/278-hooks-resolver/resolver-spec.md, "
        "docs/plans/287-dev-test-repo-root.md for FORM D); the difference is "
        "usually a quote character or a re-wrapped line."
    )


def test_canonical_form_family_counts_are_exact():
    """Each family's population is pinned, so moving a site between families —
    e.g. flipping one site's internal quote character — fails here even though
    the total stays at 71."""
    observed = collections.Counter(s.text for s in _SITES)
    names = FORM_NAMES
    # Aggregated, not a dict comprehension: several drifted families all map to
    # the "NONCANONICAL" key, and a comprehension would keep only the last
    # one's count — reporting `{'NONCANONICAL': 1}` for 71 drifted sites.
    got = collections.Counter()
    for text, n in observed.items():
        got[names.get(text, "NONCANONICAL")] += n
    got = dict(got)
    want = {names[text]: n for text, n in EXPECTED_FORM_COUNTS.items()}
    assert got == want, (
        f"resolver family populations changed: expected {want}, got {got}. "
        "56 = double-quoted sys.path.insert sites, 2 = /check-items' "
        "single-quoted sys.path.insert sites, 9 = FORM B print sites, "
        "1 = /vault-doctor's FORM C dispatcher, 3 = /dev-test's FORM D "
        "repo-root sites."
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


def test_form_d_is_the_repo_root_variant():
    """FORM D shares FORM A/B/C's registry preamble verbatim — the same
    discriminator, the same ``installLocation`` validation — and then diverges
    where #287 says it must.

    The preamble is compared as TEXT rather than restated as prose so that a
    future edit to the shared guards in one family and not the other is a
    failure here, not a silent fork. The divergences (different sentinel,
    returns the root, no cache) are each asserted individually so removing one
    fails for its own reason.
    """
    d = _observed(FORM_D)
    b = _observed(FORM_B)

    # Everything from `def` through the `isabs` guard's `continue` is shared.
    def _preamble(text):
        lines = text.split("\n")
        start = next(i for i, ln in enumerate(lines) if ln.startswith("def "))
        end = next(i for i, ln in enumerate(lines) if "os.path.isabs(_i)" in ln)
        return "\n".join(lines[start + 1 : end + 2])

    assert _preamble(d) == _preamble(b), (
        "FORM D's registry preamble has forked from FORM B's — the "
        "source-discriminator and installLocation guards must stay identical "
        "across families or a fix applied to one silently misses the other"
    )

    assert d.startswith("import json, os\n"), (
        "FORM D needs neither glob nor re; a stray import is drift"
    )
    assert "os.path.join(_i, 'scripts', 'test-dev-skill.sh')" in d, (
        "FORM D's sentinel is the dev-test script, not hooks/obsidian_utils.py: "
        "it is looking for a checkout to copy FROM"
    )
    assert "obsidian_utils.py" not in d
    assert "\n                return _i\n" in d, (
        "FORM D returns the installLocation ROOT — the caller appends "
        "scripts/test-dev-skill.sh itself"
    )
    assert "plugins/cache" not in d, (
        "D3: resolving the cache here makes `/dev-test install` copy the cache "
        "onto itself and print a success transcript for a no-op"
    )
    assert "glob" not in d and "re.fullmatch" not in d, (
        "no cache glob means no version allowlist to carry either"
    )
    assert d.endswith("\n    return ''\nprint(_ob_repo())"), (
        "FORM D must default to the empty string so its shell caller's "
        "`[ -z \"$REPO\" ]` guard fires and the fallback route is reached"
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

    FORM D (``_ob_repo``) is held to a STRICTER rule, not a looser one: it must
    mention no plugin cache at all. Per #287's D3 the cache is not a legal
    answer for ``/dev-test``, because ``scripts/test-dev-skill.sh`` derives its
    ``REPO_ROOT`` from its own location — resolved out of the cache, ``install``
    copies the cache onto itself and reports success for a byte-for-byte no-op.
    Its sentinel differs for the same reason: it is looking for a checkout to
    copy FROM (``scripts/test-dev-skill.sh``), not an installed tree to import
    (``hooks/obsidian_utils.py``).
    """
    offenders = []
    for site in _SITES:
        text = site.text
        repo_family = site.func == REPO_FUNC
        sentinel = "test-dev-skill.sh" if repo_family else "obsidian_utils.py"
        if "known_marketplaces.json" not in text:
            offenders.append(f"{site.label} (no marketplace lookup at all)")
            continue
        if "installLocation" not in text:
            offenders.append(f"{site.label} (no installLocation read)")
            continue
        if sentinel not in text:
            offenders.append(f"{site.label} (no {sentinel} sentinel check)")
            continue
        if repo_family:
            if "plugins/cache" in text:
                offenders.append(
                    f"{site.label} (repo-root resolver reaches the plugin cache; "
                    "D3 forbids it — that is the self-copy no-op)"
                )
            continue
        if "plugins/cache/" not in text:
            offenders.append(f"{site.label} (no plugin-cache fallback at all)")
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


#: What a directory-source (local checkout) marketplace entry carries. The
#: resolver keys on this and ONLY this: a github-source entry's clone root is
#: also a full plugin tree for obsidian-brain (its marketplace.json declares
#: `"source": "./"`, so the marketplace repo IS the plugin repo), which means
#: the sentinel alone cannot tell the two apart. See
#: ``test_a_github_source_install_falls_through_to_the_cache``.
DIRECTORY_SOURCE = {"source": "directory", "path": "/irrelevant"}
GITHUB_SOURCE = {"source": "github", "repo": "abhattacherjee/obsidian-brain"}

#: Sentinel meaning "write no ``source`` key at all" — distinct from ``None``,
#: which writes an explicit JSON ``null``. Both must be skipped, and a test
#: that cannot tell them apart cannot pin either.
OMIT_SOURCE = object()


def _seed_install_location(
    home,
    func,
    *,
    with_doctor=True,
    key="user-chosen-name",
    source=DIRECTORY_SOURCE,
):
    """Register a marketplace whose installLocation holds a real checkout, and
    return the path the resolver should produce for a directory-source entry.

    The registry key is deliberately NOT the plugin name: it is user-chosen
    (``obsidian-brain-repo`` on the author's machine), and a resolver that
    keyed off it would break for everyone else.

    ``source`` defaults to a directory entry because that is the install shape
    #278 exists to fix; pass ``GITHUB_SOURCE`` to build the shape that must be
    REJECTED even though its tree satisfies the sentinel.
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
    entry = {"installLocation": str(checkout)}
    if source is not OMIT_SOURCE:
        entry["source"] = source
    (home / MARKETPLACES).write_text(
        json.dumps({key: entry}), encoding="utf-8"
    )
    return expected


def _default_for(func):
    return "" if func == "_ob_doctor" else "hooks"


# Source-shape hazards, kept separate from MALFORMED_REGISTRIES because each of
# these needs a REAL checkout behind its installLocation: with a dangling path
# the sentinel would reject the entry anyway and the assertion would hold for
# the wrong reason. `source` is third-party-controlled like every other key, so
# a string/list/absent/odd value must `continue`, never raise — a raise inside
# the shared `try` aborts iteration over every remaining entry (the C2 defect
# arriving through a different key).
NON_DIRECTORY_SOURCES = {
    "github": GITHUB_SOURCE,
    "local": {"source": "local"},
    "source-is-a-bare-string": "directory",
    "source-is-a-list": [],
    "source-is-null": None,
    "source-key-absent": OMIT_SOURCE,
    "inner-source-key-absent": {"repo": "abhattacherjee/obsidian-brain"},
    "inner-source-is-a-dict": {"source": {}},
    "inner-source-differs-in-case": {"source": "Directory"},
}


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
    """No registry file at all — the shape on CI, and on a machine with no
    marketplaces installed. The loop must fall through rather than blow up.

    This models "nothing installed", NOT "installed from github": a github
    install DOES have a registry entry, with an absolute installLocation
    pointing at the marketplace clone. That case is
    ``test_a_github_source_install_falls_through_to_the_cache``.
    """
    expected = _seed_cache(fake_home, "3.3.1", func)
    assert not (fake_home / MARKETPLACES).exists()
    assert _resolver(text, func)() == expected


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
def test_a_directory_source_install_beats_the_cache(text, func, fake_home):
    """Half 1 of the discriminator pin: a *directory* entry whose tree carries
    the sentinel wins over a newer cache. This is #278's whole point, and it is
    stated as its own test so the pair below reads as a matched set."""
    _seed_cache(fake_home, "9.9.9", func)
    expected = _seed_install_location(fake_home, func, source=DIRECTORY_SOURCE)
    assert _resolver(text, func)() == expected


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
def test_a_github_source_install_falls_through_to_the_cache(text, func, fake_home):
    """Half 2, and the one the discriminator exists for.

    obsidian-brain's ``.claude-plugin/marketplace.json`` declares
    ``"source": "./"`` — the marketplace repo IS the plugin repo — so a
    github-source install's clone under
    ``~/.claude/plugins/marketplaces/<name>`` carries ``hooks/obsidian_utils.py``
    at its root and satisfies the sentinel just as a local checkout does. The
    fixture below is built that way deliberately: WITHOUT the ``source.source``
    discriminator the resolver returns the clone, and external users — the
    entire github-installed population — would load SKILL.md from the cache
    (the installed version) while ``sys.path`` pointed at the marketplace
    clone's default branch. ``/plugin marketplace update`` refreshes the clone
    without touching the cache, so that skew is the NORMAL update path, not an
    exotic one.

    Cache must win. github installs keep the behaviour they had before #278.
    """
    expected = _seed_cache(fake_home, "3.3.1", func)
    clone = _seed_install_location(fake_home, func, source=GITHUB_SOURCE)
    assert os.path.isfile(
        os.path.join(fake_home / "checkout", "hooks", "obsidian_utils.py")
    ), "fixture must satisfy the sentinel, or it proves nothing"
    assert _resolver(text, func)() == expected != clone


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
@pytest.mark.parametrize("shape", sorted(NON_DIRECTORY_SOURCES))
def test_a_non_directory_source_falls_through_to_the_cache(
    text, func, shape, fake_home
):
    """Every non-directory / malformed ``source`` shape is skipped, and none of
    them raises. Each fixture's installLocation holds a REAL tree carrying the
    sentinel, so falling through proves the discriminator did the work rather
    than the sentinel."""
    expected = _seed_cache(fake_home, "3.3.1", func)
    _seed_install_location(fake_home, func, source=NON_DIRECTORY_SOURCES[shape])
    assert _resolver(text, func)() == expected


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
def test_install_location_without_the_sentinel_falls_through(text, func, fake_home):
    """An installLocation pointing at some OTHER plugin's checkout must not
    hijack resolution: no hooks/obsidian_utils.py, no match.

    The entry carries a valid directory ``source`` so the sentinel check is
    what rejects it — otherwise the discriminator would skip it first and this
    test would pass without ever reaching the guard it names.
    """
    expected = _seed_cache(fake_home, "3.3.1", func)
    stranger = fake_home / "some-other-plugin"
    stranger.mkdir()
    (fake_home / MARKETPLACES).write_text(
        json.dumps(
            {"other": {"source": DIRECTORY_SOURCE, "installLocation": str(stranger)}}
        ),
        encoding="utf-8",
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


_DIR = '"source": {"source": "directory"}'

MALFORMED_REGISTRIES = {
    "not-json": "}{ not json at all",
    "empty-file": "",
    "top-level-list": "[]",
    "null-entry": '{"mp": null}',
    "entry-is-int": '{"mp": 7}',
    "entry-is-list": '{"mp": []}',
    # These three carry a VALID directory source on purpose, so the guard they
    # exercise is the installLocation validation itself. Without the source
    # key the new `source.source` discriminator would skip them first and the
    # inner guard would be shadowed — a test that can no longer fail for the
    # reason its name gives.
    "missing-key": "{" + '"mp": {' + _DIR + "}}",
    "install-location-null": "{" + '"mp": {' + _DIR + ', "installLocation": null}}',
    "install-location-int": "{" + '"mp": {' + _DIR + ', "installLocation": 3}}',
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


# Two entries that both fail to yield a usable ``installLocation``, kept
# separate because they are stopped by DIFFERENT halves of the same guard —
# collapsing them is how the empty-string case went untested for a whole
# review cycle while a docstring credited ``isabs`` for the pass.
UNUSABLE_INSTALL_LOCATIONS = {
    # The empty string is the ONLY shape that reaches `os.path.isabs`:
    # `isinstance("", str)` is True, so `isabs` alone stands between it and
    # `os.path.join("", "hooks") == "hooks"`.
    "empty-string": {"source": DIRECTORY_SOURCE, "installLocation": ""},
    # The key omitted entirely is a different path through the same `if`:
    # `.get` returns None and `isinstance(_i, str)` rejects it before `isabs`
    # is ever evaluated. Deleting `isabs` does NOT make this row fail, which
    # is exactly why it cannot stand in for the row above.
    "key-absent": {"source": DIRECTORY_SOURCE},
}


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
@pytest.mark.parametrize("shape", sorted(UNUSABLE_INSTALL_LOCATIONS))
def test_empty_or_absent_install_location_falls_through_to_the_cache(
    text, func, shape, fake_home, tmp_path, monkeypatch
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

    The ``isabs`` guard in the resolver is what makes the ``empty-string`` row
    pass, and deleting that guard makes that row — and only that row — fail.
    ``key-absent`` is carried alongside it rather than instead of it: it is
    stopped one clause earlier, by ``isinstance(_i, str)``.

    The cwd-on-a-checkout construction below is the whole point of the test:
    with a neutral cwd the assertion holds for the wrong reason (nothing
    matches either way), and the regression walks straight back in. For the
    same reason both entries carry a valid directory ``source``: without it
    the ``source.source`` discriminator would skip the entry first and neither
    inner guard would be reached.
    """
    cwd_checkout = tmp_path / "cwd-checkout"
    (cwd_checkout / "hooks").mkdir(parents=True)
    (cwd_checkout / "hooks" / "obsidian_utils.py").write_text("", encoding="utf-8")
    (cwd_checkout / "scripts").mkdir()
    (cwd_checkout / "scripts" / "vault_doctor.py").write_text("", encoding="utf-8")
    (fake_home / MARKETPLACES).write_text(
        json.dumps({"mp": UNUSABLE_INSTALL_LOCATIONS[shape]}), encoding="utf-8"
    )
    expected = _seed_cache(fake_home, "3.3.1", func)

    monkeypatch.chdir(cwd_checkout)
    assert _resolver(text, func)() == expected


def test_the_empty_install_location_fixture_is_actually_empty():
    """Guard on the guard. The bug this test exists to prevent is not a wrong
    assertion, it is a fixture that quietly stops constructing the condition
    its test is named for — which is precisely what happened here: the
    ``empty-string`` row used to omit the key, so ``isabs`` was never reached
    and the docstring credited a guard the test could not fail without.
    """
    assert UNUSABLE_INSTALL_LOCATIONS["empty-string"]["installLocation"] == ""
    assert "installLocation" not in UNUSABLE_INSTALL_LOCATIONS["key-absent"]


@pytest.mark.parametrize("text,func", _BLOCK_PARAMS)
def test_relative_install_location_falls_through_to_the_cache(
    text, func, fake_home, tmp_path, monkeypatch
):
    """Same hazard, stated the other way: a RELATIVE ``installLocation`` is not
    usable either, because what it names depends on where the skill was
    invoked from.

    Directory-source entry, again so the ``isabs`` guard is the one under
    test rather than the discriminator standing in front of it.
    """
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
        json.dumps(
            {
                "mp": {
                    "source": DIRECTORY_SOURCE,
                    "installLocation": "relative-install",
                }
            }
        ),
        encoding="utf-8",
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
#
# Two classes, and both are needed. The `dir-*` rows carry a valid directory
# `source`, so the guard that must skip them is the installLocation validation
# — those are the rows that keep `isabs`/`continue` honest now that the
# `source.source` discriminator stands in front of it. The remaining rows have
# a non-dict entry or a non-directory `source`, so the discriminator itself is
# what must skip them without aborting the loop.
MULTI_ENTRY_REGISTRIES = {
    "dir-null-location-then-good": {
        "aaa-third-party": {"source": DIRECTORY_SOURCE, "installLocation": None}
    },
    "dir-no-location-then-good": {"aaa-third-party": {"source": DIRECTORY_SOURCE}},
    "dir-relative-location-then-good": {
        "aaa-third-party": {"source": DIRECTORY_SOURCE, "installLocation": "some/where"}
    },
    "int-then-good": {"aaa-third-party": 7},
    "null-then-good": {"aaa-third-party": None},
    "list-then-good": {"aaa-third-party": []},
    "no-key-then-good": {"aaa-third-party": {}},
    "github-then-good": {
        "aaa-third-party": {"source": GITHUB_SOURCE, "installLocation": "/abs/where"}
    },
    "string-source-then-good": {
        "aaa-third-party": {"source": "directory", "installLocation": "/abs/where"}
    },
    "list-source-then-good": {
        "aaa-third-party": {"source": [], "installLocation": "/abs/where"}
    },
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
    AttributeError, ``"github".get("source")`` → AttributeError) aborts
    iteration over every remaining entry and drops straight to the cache —
    cache-first resolution restored silently, by someone else's plugin, with no
    obsidian-brain misconfiguration involved. That is this PR's own thesis
    failing.
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
        json.dumps(
            {
                "user-chosen-name": {
                    "source": DIRECTORY_SOURCE,
                    "installLocation": str(checkout),
                }
            }
        ),
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


# ---------------------------------------------------------------------------
# (e) FORM D — /dev-test's repo-root resolver (#287)
#
# THE TRAP this section is built around: the author's machine registers a
# directory-source marketplace pointing at THIS checkout, so the
# `git rev-parse --show-toplevel` route (layer 1) and the registry route
# (layer 2) return the same path here — the numbering is the shipped one, cwd
# first, matching every test docstring below. "The resolver found the
# checkout" is therefore not evidence of anything — it stays true with either
# layer deleted. Every case below makes the two routes DISAGREE, or removes one
# of them, so the assertion can only hold for one reason.
# ---------------------------------------------------------------------------

_GIT = shutil.which("git")
_BASH = shutil.which("bash")
_PYTHON3 = shutil.which("python3")
_REQUIRES_GIT = pytest.mark.skipif(_GIT is None, reason="git not available")
_REQUIRES_BASH = pytest.mark.skipif(_BASH is None, reason="bash not available")
_REQUIRES_PYTHON3 = pytest.mark.skipif(
    _PYTHON3 is None, reason="python3 not on PATH (the block invokes it by name)"
)


def _seed_checkout(root, *, sentinel=True):
    """Build a plausible obsidian-brain checkout at ``root``.

    ``hooks/obsidian_utils.py`` is written even when ``sentinel=False`` on
    purpose: a tree that is missing EVERYTHING would be rejected by any
    implementation, so the negative fixtures would prove nothing about the
    guard they are named for. What distinguishes the two is exactly the one
    file FORM D keys on.
    """
    (root / "hooks").mkdir(parents=True, exist_ok=True)
    (root / "hooks" / "obsidian_utils.py").write_text("", encoding="utf-8")
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    if sentinel:
        (root / "scripts" / "test-dev-skill.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
        )
    return str(root)


def _has_sentinel(root):
    return os.path.isfile(os.path.join(str(root), *DEV_TEST_SENTINEL))


def _write_registry(home, entries):
    (home / MARKETPLACES).write_text(json.dumps(entries), encoding="utf-8")


def _dir_entry(path):
    return {"source": DIRECTORY_SOURCE, "installLocation": str(path)}


# ---------------------------------------------------------------------------
# FORM D, case 1: the registry beats the caller's location
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,func", _REPO_BLOCK_PARAMS)
def test_repo_resolver_prefers_the_registered_checkout_over_the_cwd(
    text, func, fake_home, tmp_path, monkeypatch
):
    """Both trees carry the sentinel, and the cwd is one of them.

    That is the discriminating construction: a resolver that looked at the
    working directory would return ``other``, and one that read the registry
    returns ``registered``. With only one tree in play the assertion would
    hold either way.

    Note this pins the PYTHON layer, which must stay cwd-independent — the
    cwd is consulted by the shell wrapper around it (and, since the F1
    precedence fix, consulted FIRST; see the shell section at the bottom of
    this module). If this body ever starts answering with the cwd, the two
    layers become indistinguishable and the wrapper's ordering is untestable.
    """
    registered = _seed_checkout(fake_home / "registered-checkout")
    other = _seed_checkout(tmp_path / "some-other-checkout")
    assert _has_sentinel(registered) and _has_sentinel(other)
    _write_registry(fake_home, {"user-chosen-name": _dir_entry(registered)})

    monkeypatch.chdir(other)
    got = _resolver(text, func)()
    assert got == registered, f"expected the registered checkout, got {got!r}"
    assert got != other


# ---------------------------------------------------------------------------
# FORM D, case 2: a bad entry must not shadow a later good one (#278's C2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,func", _REPO_BLOCK_PARAMS)
def test_a_checkout_without_the_sentinel_does_not_shadow_a_later_good_one(
    text, func, fake_home
):
    """``json.load`` preserves insertion order, so the sentinel-less entry is
    iterated FIRST. It must be skipped, not treated as the answer and not
    treated as the end of the search.

    This is the case that gives the sentinel check teeth: delete
    ``os.path.isfile(os.path.join(_i, 'scripts', 'test-dev-skill.sh'))`` and the
    first entry — another plugin's directory-source checkout, which a real
    registry is full of — is returned instead.
    """
    stranger = _seed_checkout(fake_home / "aaa-some-other-plugin", sentinel=False)
    good = _seed_checkout(fake_home / "obsidian-brain")
    assert not _has_sentinel(stranger), "fixture must lack the sentinel"
    assert _has_sentinel(good)
    entries = {"aaa-third-party": _dir_entry(stranger), "zzz-ours": _dir_entry(good)}
    assert list(entries)[0] == "aaa-third-party", "bad entry must be iterated first"
    _write_registry(fake_home, entries)

    assert _resolver(text, func)() == good


# ---------------------------------------------------------------------------
# FORM D, case 3: github-source entries are ignored, sentinel or not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,func", _REPO_BLOCK_PARAMS)
def test_a_github_source_entry_is_ignored_even_with_the_sentinel(
    text, func, fake_home
):
    """Not theoretical. obsidian-brain's ``.claude-plugin/marketplace.json``
    declares ``"source": "./"``, so the marketplace repo IS the plugin repo and
    a github-source clone under ``~/.claude/plugins/marketplaces/<name>`` really
    does carry ``scripts/test-dev-skill.sh`` at its root.

    Copying *from* that clone is wrong for the same reason copying from the
    cache is (#287 D3 / #278): it is a released tree that ``/plugin marketplace
    update`` rewrites behind your back, not the working copy the user is
    editing. ``''`` — and the loud shell failure it triggers — is the correct
    answer.

    The fixture's sentinel is asserted first, so a future refactor that stops
    creating it cannot turn this into a test that passes for the wrong reason.
    """
    clone = _seed_checkout(fake_home / "marketplace-clone")
    assert _has_sentinel(clone), "fixture must satisfy the sentinel, or it proves nothing"
    _write_registry(
        fake_home, {"mp": {"source": GITHUB_SOURCE, "installLocation": clone}}
    )

    assert _resolver(text, func)() == ""


@pytest.mark.parametrize("text,func", _REPO_BLOCK_PARAMS)
@pytest.mark.parametrize("shape", sorted(NON_DIRECTORY_SOURCES))
def test_a_non_directory_source_does_not_shadow_a_later_good_one(
    text, func, shape, fake_home
):
    """Every non-directory / malformed ``source`` shape is skipped WITHOUT
    aborting the loop.

    ``source`` is third-party-controlled like every other key: a bare string
    (``"directory".get`` → AttributeError) or a list is enough to raise inside
    the shared ``try``, and one raise takes out every remaining entry. Each
    fixture's installLocation holds a real sentinel-bearing tree, so falling
    through to the good entry proves the discriminator did the work rather than
    the sentinel.
    """
    decoy = _seed_checkout(fake_home / "aaa-decoy")
    good = _seed_checkout(fake_home / "obsidian-brain")
    assert _has_sentinel(decoy), "decoy must satisfy the sentinel"
    entry = {"installLocation": decoy}
    source = NON_DIRECTORY_SOURCES[shape]
    if source is not OMIT_SOURCE:
        entry["source"] = source
    entries = {"aaa-third-party": entry, "zzz-ours": _dir_entry(good)}
    assert list(entries)[0] == "aaa-third-party", "bad entry must be iterated first"
    _write_registry(fake_home, entries)

    assert _resolver(text, func)() == good


#: Registry VALUES that are not dicts at all — the shape ``NON_DIRECTORY_SOURCES``
#: cannot reach, because every row there builds ``{"installLocation": …}`` and
#: only varies the ``source`` key.
#:
#: These are what keep ``isinstance(_m, dict)`` honest. Measured before this
#: fixture existed: dropping that guard from FORM D's three SKILL.md copies and
#: the ``FORM_D`` constant together failed exactly ONE test — the byte-identity
#: text pin — and **zero** FORM D behaviour tests. The behavioural safety net was
#: transitive only (FORM B's ``int-then-good`` row fails, and the preamble pin
#: forces the two families to stay byte-identical), which is weaker than a family
#: that has its own direct cover.
#:
#: Consequence of the missing guard: ``_m.get('source')`` on a non-dict raises
#: inside the SHARED ``try``, aborting iteration over every REMAINING entry — so
#: one third-party plugin's malformed row silently takes out obsidian-brain's own
#: and ``/dev-test`` stops finding the registered checkout.
NON_DICT_ENTRIES = {
    "entry-is-an-int": 7,
    "entry-is-null": None,
    "entry-is-a-list": [],
    "entry-is-a-bare-string": "directory",
}


def test_the_non_dict_entry_fixtures_are_actually_non_dicts():
    """Guard on the guard. A row that quietly became a dict would be skipped by
    the ``source`` discriminator standing in front of ``isinstance(_m, dict)``,
    so the test would keep passing while covering a different guard entirely —
    the #278 fixture-drift defect in a new place."""
    for shape, entry in NON_DICT_ENTRIES.items():
        assert not isinstance(entry, dict), (
            f"{shape} must NOT be a dict, or `isinstance(_m, dict)` is never "
            "the guard under test"
        )


@pytest.mark.parametrize("text,func", _REPO_BLOCK_PARAMS)
@pytest.mark.parametrize("shape", sorted(NON_DICT_ENTRIES))
def test_a_non_dict_entry_does_not_shadow_a_later_good_one(
    text, func, shape, fake_home
):
    """Bad entry FIRST, good entry second — the only framing that separates
    "skipped it" from "gave up on the whole registry".

    ``json.load`` preserves insertion order, and ``''`` is the answer BOTH a
    correct resolver and a crashed one give for a registry with no usable entry
    — which is why the good entry has to be there, and has to be second.
    """
    good = _seed_checkout(fake_home / "obsidian-brain")
    assert _has_sentinel(good), "the good entry must satisfy the sentinel"
    entries = {"aaa-third-party": NON_DICT_ENTRIES[shape], "zzz-ours": _dir_entry(good)}
    assert list(entries)[0] == "aaa-third-party", "bad entry must be iterated first"
    _write_registry(fake_home, entries)

    assert _resolver(text, func)() == good


# ---------------------------------------------------------------------------
# FORM D, case 4: unusable installLocation values
# ---------------------------------------------------------------------------

#: Each row carries a VALID directory ``source``, so the guard under test is
#: the installLocation validation rather than the discriminator standing in
#: front of it. Note which clause stops which row — collapsing them is how the
#: empty-string case went untested for a whole review cycle in #278:
#:
#:   * ``key-absent`` / ``null`` / ``int``  -> ``isinstance(_i, str)``
#:   * ``empty-string`` / ``relative``      -> ``os.path.isabs(_i)``
REPO_UNUSABLE_INSTALL_LOCATIONS = {
    "key-absent": {"source": DIRECTORY_SOURCE},
    "null": {"source": DIRECTORY_SOURCE, "installLocation": None},
    "int": {"source": DIRECTORY_SOURCE, "installLocation": 3},
    "empty-string": {"source": DIRECTORY_SOURCE, "installLocation": ""},
    "relative": {
        "source": DIRECTORY_SOURCE,
        "installLocation": "relative-checkout",
    },
}


def test_the_repo_unusable_install_location_fixtures_are_actually_unusable():
    """Guard on the guards. The failure mode being prevented is not a wrong
    assertion but a fixture that quietly stops constructing the condition its
    test is named for."""
    assert "installLocation" not in REPO_UNUSABLE_INSTALL_LOCATIONS["key-absent"]
    assert REPO_UNUSABLE_INSTALL_LOCATIONS["null"]["installLocation"] is None
    assert REPO_UNUSABLE_INSTALL_LOCATIONS["empty-string"]["installLocation"] == ""
    assert not os.path.isabs(
        REPO_UNUSABLE_INSTALL_LOCATIONS["relative"]["installLocation"]
    )
    for shape, entry in REPO_UNUSABLE_INSTALL_LOCATIONS.items():
        assert entry["source"] == DIRECTORY_SOURCE, (
            f"{shape} must carry a valid directory source, or the discriminator "
            "skips it first and the installLocation guard is never reached"
        )


@pytest.mark.parametrize("text,func", _REPO_BLOCK_PARAMS)
@pytest.mark.parametrize("shape", sorted(REPO_UNUSABLE_INSTALL_LOCATIONS))
def test_an_unusable_install_location_does_not_shadow_a_later_good_one(
    text, func, shape, fake_home, tmp_path, monkeypatch
):
    """Bad entry first, good entry second — the only framing that can tell
    "skipped it" from "gave up on the whole registry".

    The cwd is a decoy checkout that ALSO carries ``relative-checkout/`` beneath
    it, and that construction is the whole point of the test rather than
    scenery. ``os.path.join("", "scripts", "test-dev-skill.sh")`` is the
    *relative* string ``scripts/test-dev-skill.sh``: with ``isabs`` deleted, the
    ``empty-string`` row's sentinel check is evaluated against the working
    directory, succeeds there, and the resolver returns ``''`` — which the shell
    caller reads as "not found" and which is not the registered checkout either
    way. Same for ``relative``. With a neutral cwd both rows would pass with the
    guard deleted, and #287's whole thesis (resolution must not depend on where
    you invoked from) would walk back in through the front door.
    """
    cwd_decoy = tmp_path / "cwd-decoy"
    _seed_checkout(cwd_decoy)
    _seed_checkout(cwd_decoy / "relative-checkout")
    good = _seed_checkout(fake_home / "obsidian-brain")
    entries = {
        "aaa-third-party": REPO_UNUSABLE_INSTALL_LOCATIONS[shape],
        "zzz-ours": _dir_entry(good),
    }
    assert list(entries)[0] == "aaa-third-party", "bad entry must be iterated first"
    _write_registry(fake_home, entries)

    monkeypatch.chdir(cwd_decoy)
    got = _resolver(text, func)()
    assert got == good, (
        f"{shape}: expected the registered checkout {good!r}, got {got!r} — a "
        "cwd-relative or aborted resolution"
    )


@pytest.mark.parametrize("text,func", _REPO_BLOCK_PARAMS)
@pytest.mark.parametrize("shape", sorted(REPO_UNUSABLE_INSTALL_LOCATIONS))
def test_an_unusable_install_location_alone_yields_the_empty_string(
    text, func, shape, fake_home, tmp_path, monkeypatch
):
    """The same rows with no good entry behind them: the answer is ``''``, never
    a cwd-relative path. FORM D has no cache to fall back to (D3), so ``''`` is
    what makes the shell caller take its fallback route and then fail loudly."""
    cwd_decoy = tmp_path / "cwd-decoy"
    _seed_checkout(cwd_decoy)
    _seed_checkout(cwd_decoy / "relative-checkout")
    _write_registry(
        fake_home, {"mp": REPO_UNUSABLE_INSTALL_LOCATIONS[shape]}
    )

    monkeypatch.chdir(cwd_decoy)
    assert _resolver(text, func)() == ""


# ---------------------------------------------------------------------------
# FORM D, case 5: a malformed registry degrades, it does not raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,func", _REPO_BLOCK_PARAMS)
@pytest.mark.parametrize("shape", sorted(MALFORMED_REGISTRIES))
def test_repo_resolver_never_raises_on_a_malformed_registry(
    text, func, shape, fake_home
):
    """This block runs inside ``REPO="$(python3 -c "…")"``. An exception here
    does not degrade gracefully — it puts a traceback on stderr, leaves ``$REPO``
    empty anyway, and buries the actionable error message the shell wrapper was
    going to print."""
    (fake_home / MARKETPLACES).write_text(
        MALFORMED_REGISTRIES[shape], encoding="utf-8"
    )
    assert _resolver(text, func)() == ""


@pytest.mark.parametrize("text,func", _REPO_BLOCK_PARAMS)
def test_repo_resolver_returns_empty_when_there_is_no_registry_at_all(
    text, func, fake_home
):
    """The shape on CI and on a machine with no marketplaces installed: the
    file does not exist, ``open`` raises, and the answer is ``''``."""
    assert not (fake_home / MARKETPLACES).exists()
    assert _resolver(text, func)() == ""


# ---------------------------------------------------------------------------
# FORM D, cases 6-7: the SHELL wrapper around the block
#
# The Python resolver is only layer 2. Layer 1 (the sentinel-gated git
# toplevel, which the wrapper consults FIRST) and layer 3 (the loud failure)
# live in the bash that wraps it, and nothing above can see them — including
# which of the two layers answered, which is the whole question here.
# These run the fenced block from skills/dev-test/SKILL.md verbatim
# under a real bash, with only its final `bash "$REPO/scripts/test-dev-skill.sh"
# <sub>` line swapped for an echo — asserted to be that line first, so the
# rewrite cannot silently mis-fire on a block it does not understand.
# ---------------------------------------------------------------------------

_DEV_TEST_SKILL = os.path.join(_REPO_ROOT, "skills", "dev-test", "SKILL.md")
_DEV_TEST_INVOCATION_RE = re.compile(
    r'^bash "\$REPO/scripts/test-dev-skill\.sh" (install|restore|status)$'
)
_RESOLVED_MARKER = "OB_RESOLVED_REPO:"


def _dev_test_shell_blocks():
    """``[(lineno, script, subcommand)]`` — every ```bash fence in /dev-test
    that resolves $REPO.

    Parametrised over rather than sampled: the three copies are hand-maintained
    and the whole point of this module is that hand-maintained copies drift.

    The Python body is byte-pinned elsewhere (drift test), but the trailing
    ``bash "$REPO/scripts/test-dev-skill.sh" <sub>`` line is the *only* thing
    the three copies are allowed to differ on — Step 2 is ``install``, Step 3
    ``restore``, Step 4 ``status`` — so the subcommand is extracted here too,
    not just the body, and pinned as an ordered tuple by
    ``test_dev_test_steps_map_to_the_right_subcommands`` below.
    """
    with open(_DEV_TEST_SKILL, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    blocks = []
    opened = None
    for i, line in enumerate(lines):
        if opened is None:
            if line.strip() == "```bash":
                opened = i
            continue
        if line.strip() == "```":
            body = lines[opened + 1 : i]
            if any("test-dev-skill.sh" in ln for ln in body):
                match = _DEV_TEST_INVOCATION_RE.match(body[-1])
                sub = match.group(1) if match else None
                blocks.append((opened + 2, "\n".join(body), sub))
            opened = None
    return blocks


def _prepare_dev_test_block(script):
    """Swap the trailing installer invocation for an echo of the resolved path.

    The precondition is asserted, not assumed. A silent no-op rewrite would
    leave these tests running the REAL ``test-dev-skill.sh``, which writes the
    plugin cache — the assertions would then be measuring a side effect on the
    developer's machine instead of the resolver.
    """
    lines = script.split("\n")
    assert _DEV_TEST_INVOCATION_RE.match(lines[-1]), (
        f"dev-test block does not end in the expected installer invocation "
        f"(found {lines[-1]!r}) — the harness rewrite would be a no-op and "
        f"these tests would run the real script against the real plugin cache"
    )
    lines[-1] = f'echo "{_RESOLVED_MARKER}$REPO"'
    return "\n".join(lines)


_DEV_TEST_BLOCK_PARAMS = [
    pytest.param(_prepare_dev_test_block(text), id=f"dev-test:{lineno}")
    for lineno, text, _sub in _dev_test_shell_blocks()
]


def test_every_dev_test_step_has_an_extracted_shell_block():
    """An empty parametrize list collects zero cases and still reads green, so
    the count is pinned here: /dev-test has three steps that resolve $REPO."""
    assert len(_DEV_TEST_BLOCK_PARAMS) == EXPECTED_FORM_COUNTS[FORM_D] == 3


def test_dev_test_steps_map_to_the_right_subcommands():
    """The Python resolver body is byte-pinned identical across all three
    SKILL.md blocks (drift test above); the trailing subcommand is the ONE
    thing that is allowed — and needed — to differ between them, which makes
    it the one thing that can drift unnoticed. Pin it as an ORDERED tuple,
    not a set: a set would still pass if Step 3 and Step 4 swapped answers.

    Concretely: swapping Step 3 ("Restore original") from `restore` to
    `install` passes the entire rest of this repo's test suite unchanged —
    `/dev-test restore` would silently re-run `install` instead of undoing
    it. This is the regression this test exists to catch.
    """
    subs = [sub for _lineno, _text, sub in _dev_test_shell_blocks()]
    assert subs == ["install", "restore", "status"], (
        f"expected the 3 /dev-test steps (Install/Restore/Status) to invoke "
        f"test-dev-skill.sh with subcommands in that exact order, got {subs!r}"
    )


def _dev_test_report_prose():
    """``{subcommand: prose}`` — what each step tells Claude to SAY afterwards.

    The prose runs from the closing fence of a step's ```bash block to the next
    ``### `` heading (or EOF). That text is the step's deliverable: the script's
    exit status only reaches the user through it.
    """
    with open(_DEV_TEST_SKILL, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    out = {}
    opened = None
    pending = None
    start = None
    for i, line in enumerate(lines):
        if pending is not None and line.startswith("### "):
            out[pending] = "\n".join(lines[start:i])
            pending = None
        if opened is None:
            if line.strip() == "```bash":
                opened = i
            continue
        if line.strip() == "```":
            body = lines[opened + 1 : i]
            match = (
                _DEV_TEST_INVOCATION_RE.match(body[-1])
                if any("test-dev-skill.sh" in ln for ln in body)
                else None
            )
            if match:
                pending, start = match.group(1), i + 1
            opened = None
    if pending is not None:
        out[pending] = "\n".join(lines[start:])
    return out


#: The sentence each step is only allowed to say on ``Exit 0``, per step.
#:
#: The pin that bites is the ORDER: keyword-presence assertions ("exit status",
#: "non-zero") are a proxy for prose semantics and survive the mutation that
#: matters — hoisting the success sentence out of its arm and above the
#: branching instruction, so it reads as the unconditional deliverable again.
#: Measured on this module before ``restore``/``status`` were covered: moving
#: Step 3's "Original version restored" above the branching instruction while
#: leaving the keywords in place passed 262/262. That is this branch's own
#: defect class ("stop the skill narrating over a refused install") surviving
#: in the other two steps, so all three are pinned the same way.
_EXIT_ZERO_ONLY_SENTENCE = {
    "install": "Start a new Claude Code session",
    "restore": "Original version restored",
    # Step 4 has no banner; its exit-0-only claim is that the script's output
    # IS a status report — the thing a non-zero exit means it is not.
    "status": "report the status verbatim",
}


@pytest.mark.parametrize("sub", ["install", "restore", "status"])
def test_dev_test_reporting_branches_on_the_exit_status(sub):
    """`exit 2` was a signal with no receiver.

    ``scripts/test-dev-skill.sh`` exits 2 when the dev version was copied into
    the cache but the security tests failed, printing *"Do not run a live
    session against this install until they pass."* Step 2 then instructed,
    unconditionally, *"Dev version installed. Start a new Claude Code session
    to pick up the changes."* — the direct contradiction of the script's own
    warning, delivered as the last thing the user hears. Two reviewers found it
    independently: the failure is surfaced by the script and then talked over
    by its sole consumer.

    This is the same defect the branch fixes one file over ("the install
    transcript stops asserting work that did not happen"), reintroduced at the
    layer where the scripted line IS the deliverable — so it is pinned here, in
    the prose, not only in the script's exit code.
    """
    prose = _dev_test_report_prose()
    assert set(prose) == {"install", "restore", "status"}, (
        f"expected report prose after all three steps, got {sorted(prose)}"
    )
    text = prose[sub]

    assert "exit status" in text, (
        f"step {sub!r} must tell Claude to branch on the script's exit status"
    )
    assert "non-zero" in text, (
        f"step {sub!r} must say what to do when the script exits non-zero"
    )
    if sub == "install":
        # Exit 2 is install-only: it is the one status that means "the cache
        # WAS written AND the result is unsafe", so it needs its own arm —
        # collapsing it into the generic non-zero arm would tell the user
        # nothing was installed when something was.
        assert "Exit 2" in text
        assert "/dev-test restore" in text
        # Exit 3 is install-only for the same reason, from the other side: a
        # partway install (backup published, cache half-overwritten) used to
        # exit 1 and land in the catch-all arm, which asserted "nothing was
        # installed" — the exact opposite of what the script had just printed.
        # A user told nothing happened does not run `restore`.
        assert "Exit 3" in text, (
            "the partway-install exit code needs its own arm; folded into the "
            "catch-all it becomes a claim the caller cannot know is true"
        )
        assert text.index("Exit 3") < text.index("Any other non-zero"), (
            "the exit-3 arm must be read before the catch-all it is carved out of"
        )
        # And the catch-all must not re-assert the state it no longer knows.
        assert "nothing was installed." not in text, (
            "the catch-all arm covers every non-zero code that is not 2 or 3; "
            "asserting 'nothing was installed' there is what exit 3 exists to fix"
        )
    # The exit-0-only sentence must live UNDER the exit-0 arm, never above the
    # branching instruction — in every step, not just `install`.
    banner = _EXIT_ZERO_ONLY_SENTENCE[sub]
    assert banner in text, f"step {sub!r} lost its exit-0 sentence {banner!r}"
    assert "Exit 0" in text, f"step {sub!r} must name the exit-0 arm"
    assert text.index("Exit 0") < text.index(banner), (
        f"step {sub!r}: {banner!r} must be reachable only on exit 0, not "
        "hoisted above the branching instruction where it reads as the "
        "unconditional deliverable"
    )


def _run_dev_test_block(script, home, cwd):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["HOME"] = str(home)
    env.pop("USERPROFILE", None)
    # GIT_DIR (and friends) would override `git -C`/cwd and make
    # `git rev-parse --show-toplevel` answer for some other repo entirely, at
    # exit 0 — the fallback layer would then be measured against the wrong tree.
    return subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
        timeout=120,
    )


def _resolved(result):
    for line in result.stdout.split("\n"):
        if line.startswith(_RESOLVED_MARKER):
            return line[len(_RESOLVED_MARKER) :]
    return None


def _git_init(path):
    subprocess.run(
        [_GIT, "init", "-q"], cwd=str(path), check=True, capture_output=True, timeout=60
    )


@_REQUIRES_BASH
@_REQUIRES_PYTHON3
@_REQUIRES_GIT
@pytest.mark.parametrize("script", _DEV_TEST_BLOCK_PARAMS)
def test_shell_prefers_the_cwd_checkout_over_the_registered_one(
    script, fake_home, tmp_path
):
    """Layer 1 (cwd, sentinel-gated) beats layer 2 (registry) when they disagree.

    Both trees are git repositories carrying the sentinel, so BOTH routes can
    succeed and the answer says which one ran. On this machine the two routes
    coincide, which is precisely why they are forced apart here.

    This is the git-WORKTREE / second-clone case: standing in a checkout is a
    deliberate context signal, and the registry can only ever name one of
    them. With the original registry-first ordering, ``/dev-test install``
    from a worktree silently installed the REGISTERED checkout's code at
    exit 0 with a full success transcript — the silent-stale class this whole
    change exists to kill, re-entering through layer ordering.
    """
    registered = _seed_checkout(fake_home / "registered-checkout")
    _git_init(registered)
    cwd_repo = _seed_checkout(tmp_path / "cwd-repo")
    _git_init(cwd_repo)
    _write_registry(fake_home, {"user-chosen-name": _dir_entry(registered)})

    result = _run_dev_test_block(script, fake_home, cwd_repo)
    assert result.returncode == 0, result.stderr
    got = _resolved(result)
    # Layer 1 hands back git's physical path for cwd_repo. Layer 2 would have
    # produced the registry's installLocation string verbatim instead.
    assert got == os.path.realpath(cwd_repo), result.stdout
    assert os.path.realpath(got) != os.path.realpath(registered)


@_REQUIRES_BASH
@_REQUIRES_PYTHON3
@_REQUIRES_GIT
@pytest.mark.parametrize("script", _DEV_TEST_BLOCK_PARAMS)
def test_shell_uses_the_registry_when_the_cwd_toplevel_lacks_the_sentinel(
    script, fake_home, tmp_path
):
    """Layer 2, reached because the cwd is a FOREIGN repo — bug #287 itself.

    This is the invocation the issue was filed for: ``/dev-test`` run while
    working on some other project (``control-tower`` in the live report). The
    cwd toplevel is a perfectly good git repo that simply is not
    obsidian-brain, so the layer-1 sentinel rejects it and the registered
    checkout answers instead.

    It is also the test that makes the layer-1 sentinel check load-bearing:
    delete ``[ -f "$_T/scripts/test-dev-skill.sh" ]`` from the wrapper and the
    stranger's toplevel wins outright, the registry is never consulted, and
    this fails naming the wrong tree.
    """
    registered = _seed_checkout(fake_home / "registered-checkout")
    _git_init(registered)
    _write_registry(fake_home, {"user-chosen-name": _dir_entry(registered)})
    stranger = _seed_checkout(tmp_path / "control-tower", sentinel=False)
    _git_init(stranger)

    result = _run_dev_test_block(script, fake_home, stranger)
    assert result.returncode == 0, result.stderr
    got = _resolved(result)
    assert got == registered, result.stdout
    assert os.path.realpath(got) != os.path.realpath(stranger)


@_REQUIRES_BASH
@_REQUIRES_PYTHON3
@_REQUIRES_GIT
@pytest.mark.parametrize("script", _DEV_TEST_BLOCK_PARAMS)
def test_shell_uses_a_cwd_toplevel_that_has_the_sentinel(
    script, fake_home, tmp_path
):
    """Layer 1 with no registry at all — the unregistered local checkout.

    Run from a SUBDIRECTORY of the repo, so a wrapper that used ``$PWD``
    instead of ``git rev-parse --show-toplevel`` would resolve
    ``<repo>/skills/dev-test`` and fail the sentinel check. #287's D2 keeps
    this layer specifically to preserve the invocation that works today — an
    unregistered local checkout with the cwd inside it — so deleting it must
    fail something, and this is it.
    """
    assert not (fake_home / MARKETPLACES).exists()
    cwd_repo = _seed_checkout(tmp_path / "cwd-repo")
    _git_init(cwd_repo)
    subdir = tmp_path / "cwd-repo" / "skills" / "dev-test"
    subdir.mkdir(parents=True)

    result = _run_dev_test_block(script, fake_home, subdir)
    assert result.returncode == 0, result.stderr
    assert _resolved(result) == os.path.realpath(cwd_repo), result.stdout


@_REQUIRES_BASH
@_REQUIRES_PYTHON3
@_REQUIRES_GIT
@pytest.mark.parametrize("script", _DEV_TEST_BLOCK_PARAMS)
def test_shell_announces_the_resolved_source_checkout(script, fake_home, tmp_path):
    """The resolved tree must be PRINTED, not just used.

    Several checkouts of this repo can coexist (worktrees, second clones, the
    registered one) and they resolve to different answers. Without this line
    an install from the wrong tree is byte-identical, on screen, to an install
    from the right one: ``scripts/test-dev-skill.sh`` prints destinations
    (``hooks/*.py -> cache``) and never its source. Ambiguity has to be
    observable rather than inferred, so the echo is pinned here — including
    the resolved path itself, so an echo of a stale or empty variable fails.
    """
    cwd_repo = _seed_checkout(tmp_path / "cwd-repo")
    _git_init(cwd_repo)

    result = _run_dev_test_block(script, fake_home, cwd_repo)
    assert result.returncode == 0, result.stderr
    assert f"Source checkout: {os.path.realpath(cwd_repo)}" in result.stdout, (
        result.stdout
    )


@_REQUIRES_BASH
@_REQUIRES_PYTHON3
@_REQUIRES_GIT
@pytest.mark.parametrize("script", _DEV_TEST_BLOCK_PARAMS)
def test_shell_rejects_a_cwd_toplevel_without_the_sentinel(
    script, fake_home, tmp_path
):
    """Layer 3. The bug #287 fixes is exactly this shape: ``/dev-test`` invoked
    while working on some OTHER project, whose git toplevel is a perfectly
    valid repo that simply is not obsidian-brain.

    The old code ``cd``'d there and ran ``./scripts/test-dev-skill.sh``, which
    does not exist. The new code must exit non-zero and say which two routes it
    tried, so the user can register the checkout instead of guessing.
    """
    assert not (fake_home / MARKETPLACES).exists()
    stranger = _seed_checkout(tmp_path / "control-tower", sentinel=False)
    _git_init(stranger)

    result = _run_dev_test_block(script, fake_home, stranger)
    assert result.returncode != 0, result.stdout
    assert _resolved(result) is None, "must not report a resolved repo"
    assert "known_marketplaces.json" in result.stderr, result.stderr
    assert "scripts/test-dev-skill.sh" in result.stderr, result.stderr


@_REQUIRES_BASH
@_REQUIRES_PYTHON3
@_REQUIRES_GIT
@pytest.mark.parametrize("script", _DEV_TEST_BLOCK_PARAMS)
def test_shell_rejects_a_github_source_registry_with_no_usable_cwd(
    script, fake_home, tmp_path
):
    """Case 3 carried through to the shell: a github-source entry whose clone
    HAS the sentinel is ignored by layer 1, layer 2 finds nothing usable, and
    the command fails loudly instead of copying the marketplace clone.

    Without the ``source.source == 'directory'`` discriminator this returns the
    clone at exit 0 — a green transcript for the wrong tree.
    """
    clone = _seed_checkout(fake_home / "marketplace-clone")
    assert _has_sentinel(clone)
    _write_registry(
        fake_home, {"mp": {"source": GITHUB_SOURCE, "installLocation": clone}}
    )
    stranger = _seed_checkout(tmp_path / "control-tower", sentinel=False)
    _git_init(stranger)

    result = _run_dev_test_block(script, fake_home, stranger)
    assert result.returncode != 0, result.stdout
    assert _resolved(result) is None
    assert "known_marketplaces.json" in result.stderr, result.stderr


@_REQUIRES_BASH
@_REQUIRES_PYTHON3
@pytest.mark.parametrize("script", _DEV_TEST_BLOCK_PARAMS)
def test_shell_fails_cleanly_outside_any_git_repository(
    script, fake_home, tmp_path
):
    """``git rev-parse --show-toplevel`` exits non-zero here. The wrapper runs
    under whatever shell options Claude Code's Bash tool sets, and the ``|| true``
    is what keeps that from taking the whole block down before the error message
    is printed."""
    assert not (fake_home / MARKETPLACES).exists()
    nowhere = tmp_path / "not-a-repo"
    nowhere.mkdir()

    result = _run_dev_test_block(script, fake_home, nowhere)
    assert result.returncode != 0, result.stdout
    assert _resolved(result) is None
    assert "ERROR" in result.stderr, result.stderr
