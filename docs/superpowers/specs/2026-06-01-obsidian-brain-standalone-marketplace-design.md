# Design: Standalone-marketplace distribution for obsidian-brain

**Date:** 2026-06-01
**Status:** Approved (brainstorming) — ready for implementation plan
**Repo:** `abhattacherjee/obsidian-brain`

## Overview

obsidian-brain becomes installable directly from its own repo
(`abhattacherjee/obsidian-brain`) as a self-contained Claude Code marketplace.
The `claude-code-skills` monorepo stops being a publish target (full cutover).
Existing monorepo users swap with **zero re-setup** because config and state
already live at install-source-independent paths. A small TTL-based hook guard
prevents duplicate session notes during the overlap window when both the
monorepo plugin and the standalone plugin are briefly installed at once.

**Defining insight:** ~80% of this is already done. The repo already ships a
valid `.claude-plugin/marketplace.json` (`source: "./"`), hooks already use
`${CLAUDE_PLUGIN_ROOT}`, and skills already discover the hooks directory via a
source-agnostic `~/.claude/plugins/cache/*/obsidian-brain/*/hooks` glob. The
only genuinely new code is the hook guard; everything else is plumbing,
tooling, and docs.

### Framing decisions (locked during brainstorming)

- **Distribution:** full cutover to the standalone marketplace.
- **Duplicate prevention:** runtime hook guard (does not depend on users
  following migration steps).
- **In scope:** retool the release/publish flow (+ fix the hardcoded
  `claude-code-skills` reference), README/migration docs.
- **Out of scope:** actively editing the `claude-code-skills` monorepo (no
  deprecation release). Its existing entry simply freezes and stops receiving
  updates.

## Verified current state

| Fact | Source |
|------|--------|
| `.claude-plugin/marketplace.json` → name `obsidian-brain-repo`, plugin `obsidian-brain`, `source: "./"`, version `2.5.2` | repo |
| `.claude-plugin/plugin.json` → name `obsidian-brain`, version `2.5.2` | repo |
| Config path `~/.claude/obsidian-brain-config.json` | `hooks/obsidian_utils.py:905` (`_CONFIG_PATH`) |
| State dir `~/.claude/obsidian-brain/` (0o700) | `hooks/obsidian_utils.py:425` (`_SECURE_DIR`) |
| Hooks resolve via `${CLAUDE_PLUGIN_ROOT}` | `hooks/hooks.json` (all 3 events) |
| Skills discover hooks via wildcard glob (source-agnostic) | every `skills/*/SKILL.md` |
| Only hardcoded `claude-code-skills` reference | `scripts/test-dev-skill.sh:18` |
| Version lockstep enforced | `scripts/bump-version.sh`, `scripts/commit-preflight.sh` |

## Section 1 — Marketplace plumbing (mostly verification)

1. Confirm the marketplace manifest validates and that
   `/plugin marketplace add abhattacherjee/obsidian-brain` resolves it.
2. **Marketplace name:** keep `obsidian-brain-repo` as-is. It is the
   `@<marketplace>` suffix users type
   (`/plugin install obsidian-brain@obsidian-brain-repo`), it disambiguates the
   marketplace from the plugin, and renaming buys nothing.
3. Ensure `templates/`, `dashboards/`, `skills/`, `hooks/` are all reachable
   under `source: "./"`. The old monorepo rsync *excluded* some dev files; a
   `./` install ships the whole repo, so confirm nothing dev-only breaks a fresh
   install (`scripts/` is fine to ship; `.coverage` / `.pytest_cache` are
   gitignored and absent from a clean clone).

**Acceptance:** a clean machine can `marketplace add` + `install` and get
working hooks + skills.

## Section 2 — The hook guard (the only real new code)

New shared helper in `hooks/obsidian_utils.py`:

```python
claim_hook_run(event_type: str, session_id: str, ttl_seconds: int = 15) -> bool
```

- Lock path: `~/.claude/obsidian-brain/locks/<sid>-<event_type>` (under the
  existing `_SECURE_DIR`, 0o700 dir, 0o600 file).
- Logic:
  1. `os.open(path, O_CREAT | O_EXCL | O_WRONLY, 0o600)` → success means **we
     own this run** → write pid + timestamp → return `True`.
  2. On `FileExistsError`: stat the lock.
     - If `now - mtime < ttl_seconds` → a sibling plugin copy is handling this
       exact trigger → return `False` (caller no-ops, exit 0).
     - If stale (`>= ttl_seconds`) → this is a *legitimate later fire* (e.g. a
       second SessionStart). Unlink and re-create with `O_EXCL`; if that race is
       lost, return `False`.
- Opportunistic cleanup: at claim time, glob `locks/*` and unlink entries older
  than ~2 days. Bounds growth; cheap.
- **Fail open:** if the lock directory cannot be created or written (restrictive
  `~/.claude` permissions), proceed as if the claim succeeded rather than
  crashing or silently dropping the hook. Session logging must never be lost to
  a permissions quirk.

### Wiring

Each hook entry point calls `claim_hook_run(...)` immediately after parsing
stdin (so the session id is known) and before any vault write:

| Hook | event_type | If `claim_hook_run` returns `False` |
|------|-----------|-------------------------------------|
| `obsidian_session_log.py` (SessionEnd) | `"SessionEnd"` | exit 0, log `outcome=dedup_skip` to `~/.claude/obsidian-brain-hook.log` |
| `obsidian_session_hint.py` (SessionStart) | `"SessionStart"` | exit 0, emit nothing (no double context hint) |
| `obsidian_context_snapshot.py` (PreCompact) | `"PreCompact"` | exit 0, no second snapshot |

### Why the TTL window is correct

- Duplicate-plugin fires for one trigger land within milliseconds of each other
  → inside the 15s window → the second is blocked.
- Legitimate repeat SessionStarts (`startup` → later `resume` / `compact`) are
  seconds-to-minutes apart → outside the window → allowed through.
- Single-install (the normal case) always wins the `O_EXCL` claim uncontended →
  zero behavior change.

### Testing

Unit tests for `claim_hook_run`:
- (a) first claim succeeds (`True`)
- (b) second claim within TTL fails (`False`)
- (c) claim after TTL succeeds — re-claim (`True`)
- (d) stale-lock cleanup removes >2-day entries
- (e) concurrent-claim race (two threads) → exactly one `True`
- (f) fail-open when the lock dir is unwritable

Hook-simulation test: pipe two near-simultaneous synthetic SessionEnd payloads
through the installed hook and assert **exactly one** note is written. Reuse the
existing hook-simulation pattern.

## Section 3 — Config & state compatibility ("just works")

No new code — verification + an acceptance gate. The design already holds:

- Config: `~/.claude/obsidian-brain-config.json` (fixed path).
- State: `~/.claude/obsidian-brain/` (caches, bootstrap markers, and now
  `locks/`).
- Vault path lives inside that config; a plugin swap never touches it.

**Acceptance scenario (must pass before release):** with an existing populated
config from a monorepo install, install the standalone plugin and confirm:
`/recall` runs, **`/obsidian-setup` is NOT prompted/required**, hooks fire
exactly once, and a new session note is written to the same vault.

## Section 4 — Release / publish retooling

**Old flow (to retire):** checkout tag → rsync into
`~/dev/claude-code-skills/plugins/obsidian-brain/` → bump monorepo
`marketplace.json` → push monorepo → `release-monorepo.sh`.

**New flow:** the standalone repo *is* the marketplace. Release = standard Git
Flow release: `bump-version.sh` (plugin.json + marketplace.json in lockstep) →
CHANGELOG → merge to `main` → tag `vX.Y.Z` → GitHub Release. Users pull updates
via `/plugin marketplace update` against `main` / latest tag.

**Code changes:**

1. **`scripts/test-dev-skill.sh:18`** — replace hardcoded
   `CACHE_BASE=".../claude-code-skills/${PLUGIN_NAME}"` with source-agnostic
   discovery:
   `ls -dt ~/.claude/plugins/cache/*/obsidian-brain 2>/dev/null | head -1`
   (the same wildcard pattern the skills and `vault-doctor` already use). Handle
   the "not found" case cleanly.
2. **`/dev-test` skill** — verify it still installs/restores after the script
   change (it shells out to that script). Keep the stale-`.bak` detection
   working (status-before-install behavior).
3. **Docs of the publish path** — update `CLAUDE.md`'s release / Git-Flow
   section and any `scripts/` README to describe the new tag-the-repo flow and
   explicitly drop the monorepo rsync step.

**Explicitly out of scope:** editing the `claude-code-skills` monorepo itself.
Its existing `obsidian-brain` entry freezes at its current version; no new syncs
land there. Documented so future work does not go looking for the sync step.

## Section 5 — README + migration docs

README install section (replaces any monorepo install instructions):

```
/plugin marketplace add abhattacherjee/obsidian-brain
/plugin install obsidian-brain@obsidian-brain-repo
```

New "Migrating from the claude-code-skills version" subsection:

> Your config and vault are untouched — no `/obsidian-setup` needed.
> 1. `/plugin marketplace add abhattacherjee/obsidian-brain`
> 2. `/plugin install obsidian-brain@obsidian-brain-repo`
> 3. `/plugin uninstall` the old monorepo copy.
> During the brief window where both are installed, a built-in guard ensures
> sessions are logged only once.

Style: **extend existing README sections** rather than adding a "What's New"
block.

## Section 6 — Risks & edge cases

- **Both installed permanently (user never uninstalls old):** guard makes this
  *correct* (single note) but wasteful (double hook execution). Docs nudge
  uninstall; acceptable.
- **`locks/` under a read-only / restrictive `~/.claude`:** guard must fail
  **open** (proceed rather than crash) so a permissions quirk never silently
  drops session logging.
- **Clock skew / non-monotonic mtime:** TTL uses wall-clock mtime; a 15s window
  is generous enough that sub-second skew is irrelevant.
- **Fresh single-install regression:** covered by the "uncontended claim always
  wins" property plus a unit test asserting no behavior change with one plugin.

## Out of scope (YAGNI)

- No deprecation release to the monorepo.
- No marketplace rename.
- No new config keys (guard TTL is a constant; promote to config only if a real
  need appears).
- No cross-plugin version negotiation (which copy is "newer"). The guard governs
  *who logs*, not *which code runs*; both copies run identical-enough logic.

## Change-size summary

- One new helper + 3 small hook call-sites + tests (Section 2).
- One script fix (Section 4).
- Docs (Sections 4–5).
- Everything else is verification of already-correct behavior.

## Post-implementation follow-ups (not code)

- Update project memory `feedback_plugin_update_workflow` /
  `feedback_external_plugin_sync_workflow` — the standalone marketplace is now
  the canonical install path (the prior note recorded it as "uninstalled and
  deleted").
