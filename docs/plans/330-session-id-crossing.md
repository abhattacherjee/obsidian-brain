# Plan — #330: stop `get_session_context()` crossing ids between concurrent sessions

## Problem

`get_session_context()` resolves the current session by scanning
`~/.claude/projects/<project-dir>/*.jsonl` and taking the newest mtime. Two live
sessions in one repo both append constantly, so whichever transcript was touched
last wins — and each session can get the other's id.

The bootstrap file `~/.claude/obsidian-brain/sid-<project>` does **not** rescue
this. `_try_bootstrap_fast_path()` validates its cached sid against the
newest-mtime JSONL and returns `None` when a different session is strictly newer,
so it falls through to `_try_slow_jsonl_glob()`, which applies the same
newest-mtime rule. Both layers collapse to one tiebreak, and that tiebreak is a
race. (The bootstrap does hold on an exact same-second mtime tie — a narrow
window that does not cover the observed failure.)

Downstream, per the issue: wrong `source_session`, dangling/wrong
`source_session_note` backlinks, and a retro classification gate armed under one
id while the Stop hook checks another.

## Root cause

Newest-mtime over a project directory is not a session key. Two concurrent
sessions in one repo are indistinguishable under it, and the resolver has no
authoritative input to break the tie.

## Key finding that shapes the fix

The harness exports `CLAUDE_CODE_SESSION_ID`, and **nothing in this repo reads
it** (zero hits for any session-id env var across `hooks/`, `scripts/`,
`skills/`). Verified on this machine, Claude Code 2.1.259:

- the value matches the session's own scratchpad path segment
- a subagent inherits the **parent's** value unchanged — so a helper called from
  a dispatched agent still resolves the parent session, which is what we want
- it maps directly to `~/.claude/projects/<slug>/<sid>.jsonl`, which exists

Hooks already receive an authoritative `session_id` in their stdin JSON. The env
var is the same authority made available to the skill/CLI side, which is the side
that currently guesses.

## Design decisions (confirmed with the user)

1. **Env var first; `unknown` on any tie.** Trust `CLAUDE_CODE_SESSION_ID` when
   well-formed. Only when it is absent do we scan, and then two transcripts
   touched inside the window yield `unknown` rather than a guess.
2. **vault-doctor reports crossed only.** Flag `source_session` != the target
   note's `session_id`. Dangling backlinks stay with `snapshot-integrity` and
   #214.

## Spec deviation to reconcile on the issue (acceptance criterion 3)

The issue asks that `source_session_note` be written "only when the target note
exists and its `session_id` matches, otherwise omitted."

Applied literally to **snapshots**, this is a regression.
`obsidian_context_snapshot.py:161` writes `source_session_note: "[[<parent_stem>]]"`
at PreCompact, which normally fires *before* SessionEnd writes the parent note —
the link is a deliberate forward reference. `vault_index.py:993-1007` parses it to
associate snapshots with their parent. Requiring the target to exist would strip
the backlink from nearly every snapshot and break that association.

A live vault audit supports this reading: of 40 dangling `source_session_note`
links, all 40 point at a missing note and 11 carry no `source_session` at all —
the snapshot/PreCompact shape, not the crossing this issue is about.

**Therefore:** the write guard applies to the retro/insight path, where a
resolved `source_session` is stamped and a wrong id causes misattribution. It
does not apply to the snapshot forward reference. Edit the issue body to say so
(do not leave it in a comment).

## Live-data audit (latent-invariant check)

Run against the real vault before any code change:

| condition | count |
|---|---|
| `source_session` != target note's `session_id` (**crossed**) | 1 |
| `source_session_note` naming a note that does not exist (dangling) | 40 |

One genuine crossing exists (`2026-08-27-retro-d205.md`). A code-level guard is
therefore enough for new writes; a bulk migration is not required. The single
crossed note is reported by the new check and left for the user to decide.

## Test-isolation hazard (must land with task 1)

The suite runs inside a Claude Code session, so `CLAUDE_CODE_SESSION_ID` is
**already set in pytest's environment** (verified: pytest sees the live value)
and `tests/conftest.py` has no isolation for it. Adding the env layer without a
fixture would let the real session id leak into all 87
`test_get_session_context.py` tests.

`conftest.py` already uses this exact autouse pattern for `_SECURE_DIR`,
`_BOOTSTRAP_PREFIX` and `OBSIDIAN_BRAIN_DB`; follow it.

Baseline on clean `develop`: **3984 passed, 30 xfailed**.

---

## Tasks

### Task 1 — autouse env isolation + `allow_env` plumbing

- Add `_isolate_harness_session_id_globally` autouse fixture in
  `tests/conftest.py`: `monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)`.
- Add `allow_env: bool = True` to `_resolve_session_id()`, threaded to
  `_get_session_id_fast()`.
- No behaviour change yet — the env layer is added in task 2. This task exists so
  the isolation is in place *before* the layer can contaminate anything.
- **Verify:** full suite still 3984 passed / 30 xfailed.

### Task 2 — env-var layer as resolution layer 0

- In `_resolve_session_id()`, before the project-basename layers: read
  `CLAUDE_CODE_SESSION_ID`. If `allow_env` and the value passes the existing
  `_SID_FILENAME_SAFE.fullmatch()` validation, return it.
- Gate on format only, not on transcript existence: a brand-new session has no
  transcript yet, and the harness's own id is correct regardless. Falling through
  to the mtime scan in that window is precisely the bug.
- Emit a one-time WARN (reuse `_warn_once`) when the env value resolves to no
  transcript under the current project, so a genuinely odd environment is visible
  without being fatal.
- `_slow_path_newest_sid()` keeps `allow_bootstrap=False` and passes
  `allow_env=False`, so `check_hook_status()` stays a real health check rather
  than becoming circular.
- **Tests:** env set + valid → returned without any glob; env set + malformed →
  ignored, falls through; env absent → existing behaviour unchanged; subagent
  inheritance case (parent id used).

### Task 3 — ambiguity yields `unknown`, never a guess

- In `_try_slow_jsonl_glob()`: when two or more viable transcripts have mtime
  within `_CONCURRENT_SESSION_WINDOW_SECONDS` of the newest, return `"unknown"`
  instead of the newest.
- Window: **120 s**. Measured across 3714 transcripts in 40 project directories
  on this machine, no project had more than one transcript inside 120 s during
  normal single-session work; two concurrent sessions append within seconds of
  each other. Re-measure if this proves noisy.
- Apply the same refusal to `_try_bootstrap_fast_path()`'s fall-through so the
  bootstrap cannot re-introduce a guess the scan just refused.
- `unknown` is already a supported outcome — `get_session_context()` refuses to
  cache it and falls back to `canonical_project_name()`.
- **Tests:** two transcripts 1 s apart → `unknown`; two 10 min apart → newest;
  exact-mtime tie → `unknown`; single transcript → unchanged. Include an
  exact-boundary fixture at precisely 120 s (guards the `>=` vs `>` operator).

### Task 4 — retro gate: refuse to arm under an unusable id

- `mark_retro_classification_pending()` returns an explicit error string and
  writes no sentinel when `session_id` is empty or `"unknown"`. Arming under a
  dead key is worse than not arming: the Stop hook checks the harness id, never
  sees it, and enforcement silently vanishes (issue evidence item 3).
- **Test the arm-then-check pair end-to-end**, which nothing currently does: arm
  via `get_session_context()` with the env layer live, then drive
  `hooks/obsidian_retro_gate.py` via subprocess with the harness stdin
  `session_id`, and assert the gate is seen and cleared. Existing
  `tests/test_retro_gate.py` only ever writes the sentinel directly with a
  hardcoded sid, so the cross-source mismatch is currently untested.

### Task 5 — `source_session_note` write guard (retro/insight path only)

- Where a note is written with a resolved `source_session`, emit
  `source_session_note` only if the target note exists **and** its `session_id`
  matches the stamped `source_session`; otherwise omit the field.
- Explicitly **not** applied to `obsidian_context_snapshot.py` — see the spec
  deviation above.
- **Tests:** match → written; target missing → omitted; target exists but
  `session_id` differs → omitted; snapshot path → still writes its forward
  reference (regression guard for the deviation).

### Task 6 — vault-doctor `crossed-source-session` check

- New module `scripts/vault_doctor_checks/crossed_source_session.py`, picked up
  by the existing `pkgutil` auto-discovery (`__init__.py:51-75`).
- Detection only: `source_session` present, `source_session_note` resolves to an
  existing note, and that note's `session_id` != `source_session`. No `apply()`
  repair — deciding which of the two ids is right needs evidence the check does
  not have, and a wrong auto-repair would rewrite real attribution.
- Deliberately does **not** report dangling links (user decision;
  `snapshot-integrity` and #214 own those).
- New module, not an addition to `source_sessions.py`: that file is 1175 lines
  with its own confidence/apply semantics, and #214 already reports 14 standing
  false positives in it.
- **Tests:** crossed pair → one issue; matching pair → none; dangling link →
  none (explicit negative control, since that is the case we chose to exclude);
  missing `session_id` on target → none.

### Task 7 — docs

- `docs/architecture/architecture.json`: update `session-context` (new env layer)
  and add the new vault-doctor check component; keep `lastUpdated` current and
  `version` in sync with `plugin.json`.
- Re-render + smoke-test per `CLAUDE.md`.
- `CHANGELOG.md` under `[Unreleased]`.

## Out of scope

- #214 snapshot-orphan false positives.
- #111 `_resolve_session_note_by_hash` WARN wording.
- Repairing the one existing crossed note (reported, not rewritten).
