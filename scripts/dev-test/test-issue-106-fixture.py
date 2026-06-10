#!/usr/bin/env python3
"""Fixture-vault dev-test for issue #106 — source-sessions UUID-first taxonomy.

Tracked by #152; validates the #106 taxonomy.

Run when: editing scripts/vault_doctor_checks/source_sessions.py OR when
editing the --json serializer (_issue_row) in scripts/vault_doctor.py.

Usage:
    python3 scripts/dev-test/test-issue-106-fixture.py

This script is self-contained and deterministic: it seeds a temp $HOME with
a synthetic vault + stub JSONLs, runs the real vault_doctor.py dispatcher as a
subprocess, and asserts the full taxonomy invariants.

# Spec-vs-current-code reconciliations (PRs #103 + #104, post-issue-#106):
#
# 1. (#103) --min-confidence flag added. Not tested here (covered by unit
#    tests). The script passes --days=36500 (100 years) to avoid the cutoff
#    window filtering out fixture notes.
#
# 2. (#104) Imported notes are skipped. The fixture does NOT seed imported
#    notes — each fixture note represents one real signal class. The `imported`
#    key is absent from all fixture frontmatter; imported-note skipping is
#    covered by unit tests elsewhere.
#
# 3. date-window-hint reason text: the issue spec says the reason should end
#    with "— content-grep the JSONL before applying". The current code emits
#    " — content-grep the JSONL before applying" (with a space before the
#    em-dash). The assertion uses str.endswith() with that exact suffix so it
#    stays byte-exact to the current code (source_sessions.py lines 929-940).
#    If the reason format ever changes, update the assertion here.
#
# 4. The spec listed two deprecated keys: convergence_warning (false) and
#    convergence_count (0). These are confirmed present as hard-coded defaults
#    in _issue_row (vault_doctor.py lines 329-330) and the assertions verify
#    them on EVERY row.
#
# 5. signal_class is confirmed TOP-LEVEL in the --json row (vault_doctor.py
#    line 322): _issue_row promotes it from Issue.extra to the row dict
#    directly. The row has NO "extra" key. The assertion verifies this for
#    every row — this is the exact class of drift that the CHANGELOG bug
#    4f44033 would have caught.
#
# 6. For uuid-basename-stale: the fixture note has `date: <session-day>` so
#    the JSONL window overlaps the note day, producing confidence=0.99.
#    unresolved==False for this class (no "unresolved" key in extra).
#
# 7. For uuid-day-mismatch: the note `date:` is set to a day that does NOT
#    overlap the JSONL window — the window is two days in the past. This
#    forces overlap_ok=False -> uuid-day-mismatch, conf=0.0, unresolved=True.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# ─── Locate dispatcher (run from repo root OR from scripts/dev-test/) ────

_HERE = Path(__file__).parent
_REPO_ROOT = (_HERE / ".." / "..").resolve()
VAULT_DOCTOR = _REPO_ROOT / "scripts" / "vault_doctor.py"

if not VAULT_DOCTOR.exists():
    sys.exit(f"❌ vault_doctor.py not found at {VAULT_DOCTOR}; run from the repo root.")

# ─── Deterministic SIDs for each signal class ────────────────────────────
# The scanner does NO UUID-format validation — session IDs are opaque
# strings matched by exact filename/frontmatter equality. These fixture SIDs
# deliberately contain non-hex segments ("stale", "mism", ...) so they are
# grep-able and obviously synthetic; fixed values keep the test reproducible.

SID_STALE   = "aaaaaaaa-stale-0000-0000-000000000001"   # uuid-basename-stale
SID_MISMATCH = "bbbbbbbb-mism-0000-0000-000000000002"   # uuid-day-mismatch
SID_MISSING  = "cccccccc-miss-0000-0000-000000000003"   # missing-session-note
# date-window-hint: the hint insight has NO source_session UUID, so Phase 2
# day-overlap matching fires. NOTE: the matcher picks the LARGEST-overlap
# window among session-note-backed JSONLs — that is SID_STALE's JSONL
# (aaaaaaaa, first_ts = start+1000, earliest start → biggest overlap with
# today), NOT this one. SID_HINT_JSONL exists to add a second today-window
# candidate; the hint row's proposed_sid is aaaaaaaa.
SID_HINT_JSONL = "dddddddd-hint-0000-0000-000000000004"
# unresolved: no UUID, no JSONL window at all.

PROJECT = "fixture-project"

# ─── Timestamps ──────────────────────────────────────────────────────────
# All times in UTC. Use a fixed anchor so the test is deterministic.
# The JSONL windows are written around TODAY_STR to ensure they pass --days.

NOW_UTC    = datetime.now(timezone.utc)
TODAY_STR  = NOW_UTC.strftime("%Y-%m-%d")  # e.g. "2026-06-10"
PAST2_STR  = "2026-01-01"  # a day guaranteed not to overlap any TODAY window
_TODAY_NOON_TS   = int(NOW_UTC.replace(hour=12, minute=0, second=0, microsecond=0).timestamp())
_TODAY_START_TS  = int(NOW_UTC.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
_TODAY_END_TS    = _TODAY_START_TS + 86400  # midnight-to-midnight window

# ─── Global cleanup tracking ─────────────────────────────────────────────

_CLEANUP_DIRS: list[Path] = []

def _cleanup():
    for d in _CLEANUP_DIRS:
        shutil.rmtree(d, ignore_errors=True)

atexit.register(_cleanup)
for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, lambda s, f: sys.exit(130))

# ─── Test pass/fail counters ─────────────────────────────────────────────

PASS = 0
FAIL = 0

def pass_(msg: str) -> None:
    global PASS
    print(f"  ✅ {msg}")
    PASS += 1

def fail_(msg: str) -> None:
    global FAIL
    print(f"  ❌ {msg}")
    FAIL += 1

def diff_report(label: str, expected, actual) -> None:
    """Print a helpful diff-style message for assertion failures."""
    print(f"       EXPECTED ({label}): {expected!r}")
    print(f"       ACTUAL   ({label}): {actual!r}")


# ─── Fixture construction helpers ────────────────────────────────────────

def make_jsonl(path: Path, first_ts: int, last_ts: int) -> None:
    """Write a minimal 2-entry JSONL with ISO timestamps spanning first..last."""
    path.parent.mkdir(parents=True, exist_ok=True)
    first_iso = datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    last_iso  = datetime.fromtimestamp(last_ts,  tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    entries = [
        json.dumps({"type": "user",      "message": {"content": "hello"},  "timestamp": first_iso}),
        json.dumps({"type": "assistant", "message": {"content": "world"},  "timestamp": last_iso}),
    ]
    path.write_text("\n".join(entries) + "\n", encoding="utf-8")


def make_insight(path: Path, fm: dict, body: str = "body\n") -> None:
    """Write a markdown note with YAML frontmatter at `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = "\n".join(f"{k}: {v}" for k, v in fm.items())
    path.write_text(f"---\n{fm_lines}\n---\n\n{body}", encoding="utf-8")


def make_session_note(path: Path, sid: str, date_str: str, basename: str) -> None:
    """Write a minimal session note with the required type+session_id fields."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: claude-session\nsession_id: {sid}\ndate: {date_str}\nproject: {PROJECT}\n---\n\nbody\n",
        encoding="utf-8",
    )


# ─── Build fixture HOME ───────────────────────────────────────────────────

FIXTURE_DIR = Path(tempfile.mkdtemp(prefix="ob-issue-106-"))
_CLEANUP_DIRS.append(FIXTURE_DIR)

FAKE_HOME = FIXTURE_DIR / "home"
VAULT     = FIXTURE_DIR / "vault"

(FAKE_HOME / ".claude").mkdir(parents=True)

# Config: points at our synthetic vault.
CONFIG_PATH = FAKE_HOME / ".claude" / "obsidian-brain-config.json"
CONFIG_PATH.write_text(json.dumps({
    "vault_path": str(VAULT),
    "sessions_folder": "claude-sessions",
    "insights_folder": "claude-insights",
}), encoding="utf-8")

(VAULT / "claude-sessions").mkdir(parents=True)
(VAULT / "claude-insights").mkdir(parents=True)

# Project directory for JSONLs: ~/.claude/projects/<slug>/
PROJECTS_ROOT = FAKE_HOME / ".claude" / "projects"
# Use a slug that matches _jsonl_dir_for_project's glob "*<project>" pattern.
# The project name is "fixture-project" so the slug ends with it.
PROJ_SLUG = f"-{PROJECT}"
PROJ_DIR  = PROJECTS_ROOT / PROJ_SLUG
PROJ_DIR.mkdir(parents=True)

# ─── Seed session notes ───────────────────────────────────────────────────
# Session note for SID_STALE: basename is the CURRENT correct name.
# The insight note will have a STALE (wrong) basename in source_session_note.
SESSION_STALE_BASENAME = f"{TODAY_STR}-real-session-for-stale-aaaa"
make_session_note(
    VAULT / "claude-sessions" / f"{SESSION_STALE_BASENAME}.md",
    SID_STALE,
    TODAY_STR,
    SESSION_STALE_BASENAME,
)

# Session note for SID_MISMATCH: exists in vault, but JSONL window is in the past.
SESSION_MISMATCH_BASENAME = f"{PAST2_STR}-session-for-mismatch-bbbb"
make_session_note(
    VAULT / "claude-sessions" / f"{SESSION_MISMATCH_BASENAME}.md",
    SID_MISMATCH,
    PAST2_STR,
    SESSION_MISMATCH_BASENAME,
)

# Session note for SID_HINT_JSONL (date-window-hint case):
# This session note exists in the vault AND has a JSONL that covers TODAY.
# The insight note for this class has NO source_session UUID so it falls into
# Phase 2 date-window matching.
SESSION_HINT_BASENAME = f"{TODAY_STR}-session-for-hint-dddd"
make_session_note(
    VAULT / "claude-sessions" / f"{SESSION_HINT_BASENAME}.md",
    SID_HINT_JSONL,
    TODAY_STR,
    SESSION_HINT_BASENAME,
)

# No session note for SID_MISSING — that's the point of missing-session-note.

# ─── Seed JSONLs ──────────────────────────────────────────────────────────
# SID_STALE JSONL: window covers TODAY so day-overlap check passes → 0.99
make_jsonl(PROJ_DIR / f"{SID_STALE}.jsonl",
           first_ts=_TODAY_START_TS + 1000,
           last_ts=_TODAY_START_TS + 7200)

# SID_MISMATCH JSONL: window must NOT overlap TODAY.
# _jsonl_window() returns (first_entry_ts, file_mtime). The "last_ts" used for
# overlap is the FILE MTIME (not the last JSONL entry). We must set both the
# entry timestamps AND the file mtime to the past, otherwise mtime=NOW would
# make the window span [Jan 2026 .. NOW] and today would be inside it.
_PAST2_START = int(datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())
_mismatch_jsonl = PROJ_DIR / f"{SID_MISMATCH}.jsonl"
make_jsonl(_mismatch_jsonl,
           first_ts=_PAST2_START + 1000,
           last_ts=_PAST2_START + 7200)
# Force the file mtime to Jan 1 2026 so _jsonl_window returns a PAST window.
os.utime(_mismatch_jsonl, (_PAST2_START + 7200, _PAST2_START + 7200))

# SID_MISSING JSONL: exists (triggers missing-session-note, not unresolved)
make_jsonl(PROJ_DIR / f"{SID_MISSING}.jsonl",
           first_ts=_TODAY_START_TS + 2000,
           last_ts=_TODAY_START_TS + 5000)

# SID_HINT_JSONL JSONL: covers TODAY for the date-window-hint match
make_jsonl(PROJ_DIR / f"{SID_HINT_JSONL}.jsonl",
           first_ts=_TODAY_START_TS + 3000,
           last_ts=_TODAY_START_TS + 6000)

# No JSONL for the unresolved case — no match at all.

# ─── Seed insight notes ───────────────────────────────────────────────────

# 1. uuid-basename-stale: UUID resolves, basename in source_session_note is wrong
STALE_NOTE = VAULT / "claude-insights" / f"{TODAY_STR}-insight-uuid-stale-0001.md"
make_insight(STALE_NOTE, {
    "type":                 "claude-insight",
    "date":                 TODAY_STR,
    "project":              PROJECT,
    "source_session":       SID_STALE,
    "source_session_note":  '"[[2025-01-01-wrong-old-basename]]"',
})

# 2. uuid-day-mismatch: UUID resolves to a session note, but JSONL window
#    (Jan 2026) does NOT overlap note's date (TODAY) → 0.0, unresolved=True
MISMATCH_NOTE = VAULT / "claude-insights" / f"{TODAY_STR}-insight-uuid-mismatch-0002.md"
make_insight(MISMATCH_NOTE, {
    "type":                 "claude-insight",
    "date":                 TODAY_STR,
    "project":              PROJECT,
    "source_session":       SID_MISMATCH,
    "source_session_note":  '"[[2025-12-31-wrong-old-basename]]"',
})

# 3. missing-session-note: UUID has a JSONL but NO session note in vault
MISSING_NOTE = VAULT / "claude-insights" / f"{TODAY_STR}-insight-missing-session-0003.md"
make_insight(MISSING_NOTE, {
    "type":            "claude-insight",
    "date":            TODAY_STR,
    "project":         PROJECT,
    "source_session":  SID_MISSING,
    "source_session_note": '"[[2026-01-15-some-session]]"',
})

# 4. date-window-hint: no UUID, date: TODAY, JSONL window overlaps → conf=0.5
HINT_NOTE = VAULT / "claude-insights" / f"{TODAY_STR}-insight-date-hint-0004.md"
make_insight(HINT_NOTE, {
    "type":            "claude-insight",
    "date":            TODAY_STR,
    "project":         PROJECT,
    "source_session":  "",
    "source_session_note": '"[[2025-01-01-placeholder]]"',
})

# 5. unresolved: no UUID, no JSONL match (use a very old date that no JSONL covers)
UNRESOLVED_NOTE = VAULT / "claude-insights" / f"{TODAY_STR}-insight-unresolved-0005.md"
make_insight(UNRESOLVED_NOTE, {
    "type":            "claude-insight",
    "date":            "2020-01-01",
    "project":         PROJECT,
    "source_session":  "",
    "source_session_note": '"[[2020-01-01-no-match]]"',
})

# Capture byte-snapshots of all notes that must NOT be mutated by --apply --yes
# (all except STALE_NOTE which IS the apply target)
_pre_apply_snapshots: dict[Path, bytes] = {}
for note in [MISMATCH_NOTE, MISSING_NOTE, HINT_NOTE, UNRESOLVED_NOTE]:
    _pre_apply_snapshots[note] = note.read_bytes()

# ─── Run vault_doctor --check source-sessions --json ─────────────────────

def run_doctor(*extra_args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(FAKE_HOME)
    return subprocess.run(
        [
            sys.executable,
            str(VAULT_DOCTOR),
            "--check", "source-sessions",
            "--vault", str(VAULT),
            "--sessions-folder", "claude-sessions",
            "--insights-folder", "claude-insights",
            "--days", "36500",   # 100 years — never filters by cutoff
            "--json",
            *extra_args,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

print("═══════════════════════════════════════════════════════════════")
print(f"Issue #106 — source-sessions fixture-vault taxonomy")
print(f"Vault:   {VAULT}")
print(f"Temp home: {FAKE_HOME}")
print("═══════════════════════════════════════════════════════════════")
print()
print("Phase 1 — dry-run --json assertions")
print()

result = run_doctor()
if result.returncode not in (0, 1):
    sys.exit(
        f"❌ vault_doctor exited with unexpected code {result.returncode}\n"
        f"   stderr: {result.stderr[:1000]}\n"
        f"   stdout: {result.stdout[:500]}"
    )

try:
    payload = json.loads(result.stdout)
except json.JSONDecodeError as e:
    sys.exit(
        f"❌ --json output is not valid JSON: {e}\n"
        f"   stdout: {result.stdout[:500]}"
    )

issues = payload.get("issues", [])

# ─── Assertion 1: signal_class is top-level; no "extra" key ──────────────
print("  1. signal_class top-level on every row; no 'extra' key")
all_have_signal_class = True
any_have_extra = False
for row in issues:
    if "signal_class" not in row:
        all_have_signal_class = False
        fail_(f"row missing top-level signal_class: note={Path(row.get('note_path','')).name!r}")
        print(f"       JSON has: {list(row.keys())}")
        break
    if "extra" in row:
        any_have_extra = True
        fail_(f"row has 'extra' key (JSON has extra.signal_class={row['extra'].get('signal_class')!r} "
              f"but expected top-level signal_class): note={Path(row['note_path']).name!r}")
        break
if all_have_signal_class and not any_have_extra:
    pass_(f"all {len(issues)} rows: signal_class top-level, no 'extra' key")
else:
    # Remaining assertions require signal_class to be top-level; skip them
    # rather than crashing with KeyError — the failure is already recorded.
    print()
    print("  ⚠  Skipping assertions 2-7: signal_class not top-level (see assertion 1 failure)")
    print()
    print("═══════════════════════════════════════════════════════════════")
    print(f"PASS={PASS}  FAIL={FAIL}")
    print("═══════════════════════════════════════════════════════════════")
    sys.exit(1)

# ─── Assertion 2: vocabulary subset ──────────────────────────────────────
print("  2. vocabulary subset {uuid-basename-stale, uuid-day-mismatch, missing-session-note, date-window-hint, unresolved}")
EXPECTED_VOCAB = {
    "uuid-basename-stale", "uuid-day-mismatch", "missing-session-note",
    "date-window-hint", "unresolved",
}
found_classes = {r["signal_class"] for r in issues}
unknown = found_classes - EXPECTED_VOCAB
if unknown:
    fail_(f"unexpected signal_class values in output: {unknown}")
    diff_report("expected subset", EXPECTED_VOCAB, found_classes)
else:
    pass_(f"all signal classes are within expected vocabulary: {found_classes}")

# ─── Assertion 3: all 5 classes are present ──────────────────────────────
print("  3. all 5 signal classes are present in output")
missing_classes = EXPECTED_VOCAB - found_classes
if missing_classes:
    fail_(f"missing signal_class(es) from output: {missing_classes}")
    diff_report("found", list(found_classes), list(EXPECTED_VOCAB))
else:
    pass_("all 5 signal classes found")

# Index rows by signal_class for subsequent assertions
by_class: dict[str, list[dict]] = {}
for row in issues:
    by_class.setdefault(row.get("signal_class", ""), []).append(row)

# ─── Assertion 4: per-class confidence invariants ────────────────────────
print("  4. per-class confidence invariants (0.99 / 0.0 / 0.0 / 0.5 / 0.0)")
EXPECTED_CONFIDENCES = {
    "uuid-basename-stale":  0.99,
    "uuid-day-mismatch":    0.0,
    "missing-session-note": 0.0,
    "date-window-hint":     0.5,
    "unresolved":           0.0,
}
conf_ok = True
for cls, exp_conf in EXPECTED_CONFIDENCES.items():
    rows = by_class.get(cls, [])
    if not rows:
        fail_(f"  {cls}: no rows to check confidence")
        conf_ok = False
        continue
    for row in rows:
        got_conf = row.get("confidence")
        if got_conf != exp_conf:
            fail_(f"  {cls}: confidence mismatch")
            diff_report(f"{cls}.confidence", exp_conf, got_conf)
            conf_ok = False
if conf_ok:
    pass_("all per-class confidence values match spec")

# ─── Assertion 5: non-uuid-basename-stale rows have unresolved==true ─────
print("  5. non-uuid-basename-stale rows: unresolved==true")
unresolved_ok = True
for row in issues:
    cls = row.get("signal_class", "")
    is_unresolved = row.get("unresolved")
    if cls == "uuid-basename-stale":
        if is_unresolved is not False:
            fail_(f"uuid-basename-stale should have unresolved=false, got {is_unresolved!r}")
            unresolved_ok = False
    else:
        if is_unresolved is not True:
            fail_(f"{cls}: expected unresolved=true, got {is_unresolved!r}")
            unresolved_ok = False
if unresolved_ok:
    pass_("uuid-basename-stale unresolved=false; all others unresolved=true")

# ─── Assertion 6: date-window-hint reason ends with the content-grep suffix
print("  6. date-window-hint reason ends with '— content-grep the JSONL before applying'")
hint_rows = by_class.get("date-window-hint", [])
if not hint_rows:
    fail_("no date-window-hint rows")
else:
    SUFFIX = "— content-grep the JSONL before applying"
    reason_ok = True
    for row in hint_rows:
        reason = row.get("reason", "")
        if not reason.endswith(SUFFIX):
            fail_(f"date-window-hint reason does not end with expected suffix")
            diff_report("reason", f"...{SUFFIX}", reason[-80:])
            reason_ok = False
    if reason_ok:
        pass_(f"date-window-hint reason ends with expected suffix")

# ─── Assertion 7: deprecated keys convergence_warning/convergence_count ──
print("  7. convergence_warning=false and convergence_count=0 on every row")
deprecated_ok = True
for row in issues:
    cw = row.get("convergence_warning")
    cc = row.get("convergence_count")
    if cw is not False:
        fail_(f"convergence_warning != false on {row.get('signal_class', '?')!r} row: {cw!r}")
        deprecated_ok = False
    if cc != 0:
        fail_(f"convergence_count != 0 on {row.get('signal_class', '?')!r} row: {cc!r}")
        deprecated_ok = False
if deprecated_ok:
    pass_("convergence_warning=false and convergence_count=0 on all rows")

# ─── Phase 2: --apply --yes mutates ONLY the uuid-basename-stale note ─────
print()
print("Phase 2 — --apply --yes: only uuid-basename-stale note changes")
print()

stale_before = STALE_NOTE.read_bytes()
apply_result = run_doctor("--apply", "--yes")
if apply_result.returncode not in (0, 1, 2):
    fail_(f"--apply --yes exited with unexpected code {apply_result.returncode}")
else:
    # All Phase-2 assertions live inside this else: they only make sense
    # when the apply subprocess exited with a sane code.
    stale_after = STALE_NOTE.read_bytes()
    if stale_before == stale_after:
        fail_("uuid-basename-stale note was NOT mutated by --apply (expected it to be repaired)")
    else:
        pass_("uuid-basename-stale note was mutated by --apply")

    # Verify all other notes are byte-identical
    snapshot_ok = True
    for note, snapshot in _pre_apply_snapshots.items():
        after_bytes = note.read_bytes()
        if after_bytes != snapshot:
            fail_(f"--apply mutated a non-stale note: {note.name}")
            diff_report("bytes changed", len(snapshot), len(after_bytes))
            snapshot_ok = False
    if snapshot_ok:
        pass_("all non-stale notes are byte-identical pre/post --apply")

    # Verify the stale note now has the correct basename
    stale_text_after = STALE_NOTE.read_text(encoding="utf-8")
    if SESSION_STALE_BASENAME in stale_text_after:
        pass_(f"repaired note contains correct basename: {SESSION_STALE_BASENAME!r}")
    else:
        fail_(f"repaired note does not contain expected basename {SESSION_STALE_BASENAME!r}")
        print(f"       note content head: {stale_text_after[:300]}")

# ─── Summary ──────────────────────────────────────────────────────────────

print()
print("═══════════════════════════════════════════════════════════════")
print(f"PASS={PASS}  FAIL={FAIL}")
print("═══════════════════════════════════════════════════════════════")
print()
if FAIL == 0:
    print("All assertions passed. Cleanup will run via atexit.")
else:
    print("Some assertions FAILED. See output above for details.")
    print("Note: the fixture dir is deleted at exit (atexit); comment out the")
    print("atexit.register(_cleanup) line to preserve it for inspection.")
print(f"  Fixture: {FIXTURE_DIR}")
print()
print("CI follow-up (not implemented): wire into a job triggered on PRs")
print("  touching scripts/vault_doctor_checks/source_sessions.py")
print()

sys.exit(0 if FAIL == 0 else 1)
