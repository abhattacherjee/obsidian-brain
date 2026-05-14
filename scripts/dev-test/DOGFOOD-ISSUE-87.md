# Dogfood test plan — Issue #87 (`/check-items` smarter v2)

Hand this file to a **fresh Claude Code session** (or a human operator) to
exercise PR #158 end-to-end against the real vault. The merge-gate harness
(`test-issue-87-manual.py`) only proves the wiring is intact; this plan proves
the *behavior* is right.

**Prereqs**
- PR #158 branch checked out OR merged to `develop` and `/dev-test install`'d.
  Confirm via `grep '"version"' .claude-plugin/plugin.json` → `2.4.4` or later.
- `~/.claude/obsidian-brain-config.json` exists and points at a real vault.
- `claude` CLI on `$PATH` (the pipeline shells out for Haiku/Sonnet sub-agents).
- At least one project under `<vault>/claude-sessions/` with ≥10 sessions in
  the last 14 days, OR widen the window with `30d` in the relevant phases.
- A clean shell — don't run inside a `claude -p` sub-agent.

**Reset between phases** (optional, only when a phase says to)
```bash
rm -f ~/.claude/obsidian-brain/check-items-classifications.json
rm -f ~/.claude/obsidian-brain/check-items-evidence-*.json
```

**Where artifacts land**
- Dashboard:  `<vault>/claude-dashboards/check-items-<scope>-<YYYY-MM-DD>.md`
- Cache:      `~/.claude/obsidian-brain/check-items-classifications.json`
- Evidence:   `~/.claude/obsidian-brain/check-items-evidence-<hash>.json`
- Pipeline tmp: `~/.claude/obsidian-brain/proto-stage-*.json` (cleaned at end)

---

## Phase 1 — Cold-cache run on current project (golden path)

**Setup:** `cd` into an obsidian-brain-tracked project. Delete the cache (see
Reset above). Run `/check-items`.

**Pass criteria**
- [ ] Skill announces parsing, scope resolves to current project, 14d window.
- [ ] Step 2 reports a non-zero count of open items collected.
- [ ] Step 3 coarse-grouping logs N groups; **all** are routed `needs` with
      `_reason="new"` (cold cache).
- [ ] Step 4 semantic-merge sub-agent (Sonnet) dispatches and returns. If
      payload exceeds the 1 MB stdin cap, the skill logs `oversize fallback`
      and continues with the token-coarse grouping — **not** an error.
- [ ] Step 5 evidence-gather completes; you see a Haiku sub-agent fire.
- [ ] Step 6 classifier returns DONE / NEEDS-ACTION / STALE / ACTIVE buckets.
- [ ] Step 7 prints the review-confirmation block grouped by tier.
- [ ] Step 9 writes the dashboard. **Open the file** and verify:
      - YAML frontmatter parses (no stray quotes, no `..` in `date:` or `scope:`).
      - Filename matches `check-items-<safe_scope>-<YYYY-MM-DD>.md` with no
        `..`, no spaces, no shell-meta in `<safe_scope>`.
      - DONE / NEEDS-ACTION / STALE / ACTIVE sections present (or
        "no items" placeholders).
- [ ] Step 10 writes the cache. `jq '.runs | keys' ~/.claude/obsidian-brain/check-items-classifications.json`
      contains the current project name.

**Stop signal:** any sub-agent dispatch error containing `argv` or `ARG_MAX`
means the stdin-not-argv discipline regressed — file a bug.

---

## Phase 2 — Warm-cache replay (cache hits)

**Setup:** Immediately re-run `/check-items` in the same project. Do NOT delete
the cache.

**Pass criteria**
- [ ] Skill is **noticeably faster** (rough sniff: ≥30% wall-clock reduction vs.
      Phase 1 — sub-agent skip is the dominant savings).
- [ ] Step 3 partition log shows the majority of groups routed to `known` with
      `_cached_classification` / `_cached_confidence` / `_cached_action_required`
      hydrated. Only groups that genuinely changed (mtime, HEAD, TTL) carry a
      `_reason` from {`mtime_changed`, `head_changed`, `ttl_expired`}.
- [ ] No semantic-merge or classifier sub-agent fires for cached groups.
- [ ] Dashboard regenerates with the same classifications. Diff the two
      dashboards: `diff` should be empty except for the timestamp and any
      genuine drift.
- [ ] Cache file is rewritten (mtime newer) but `schema_version: 1` preserved.

**Bug surface:** if the second run reclassifies everything, the
`canonical_hash` between Step 3 and Step 10 has drifted again (the R8 bug).

---

## Phase 3 — `--no-cache` forces full reclassification

**Setup:** Same project as Phase 2. Run `/check-items --no-cache`.

**Pass criteria**
- [ ] Every group routed `needs` with `_reason="force"`.
- [ ] Semantic-merge + classifier fire as in Phase 1.
- [ ] Resulting cache file updates `last_run_ts` and `project_head_at_classify`
      to the current commit.

---

## Phase 4 — Vault-wide `all` mode

**Setup:** `/check-items all`. (You may need to `cd` outside any git repo to
hit the project-iteration path, or stay inside — both should work.)

**Pass criteria**
- [ ] Skill iterates over multiple projects (you see project names in logs).
- [ ] One combined dashboard at `<vault>/claude-dashboards/check-items-all-<YYYY-MM-DD>.md` aggregating items from all projects (single file, not per-project).
- [ ] Cache file gains a `runs.<project>` entry for each project visited.
- [ ] No `_reason="head_unavailable"` groups silently dropped — they should be
      classified (or surfaced as `--no-cache`-equivalent), not lost.

---

## Phase 5 — Widened window `30d` + `--show-all`

**Setup:** Pick a project with sparse activity. Run `/check-items 30d --show-all`.

**Pass criteria**
- [ ] Step 2 collection set is strictly larger than the 14d run (or equal if
      no notes fell in the 14d–30d band).
- [ ] Output table includes STALE-classified items (default 14d run hides STALE). LOW-confidence rows are NOT gated by `--show-all` — they always surface for user review.

---

## Phase 6 — `--dry-run` skips edit-confirm loop

**Setup:** Run `/check-items --dry-run` in a project with at least one
DONE-classified item.

**Pass criteria**
- [ ] Skill writes the dashboard.
- [ ] Skill does **NOT** present an `AskUserQuestion` checkoff confirmation.
- [ ] No session-note files are modified (verify with `git status` on the
      vault if it's a git repo, or `find <vault>/claude-sessions -newer
      <some-marker> -mmin -5`).
- [ ] Cache still updates (dry-run only skips checkoff edits, not persistence).

---

## Phase 7 — Edit-confirm loop applies checkoffs

**Setup:** Run `/check-items` (no flags) in a project with at least one
high-confidence DONE item. Accept the proposed checkoffs.

**Pass criteria**
- [ ] Edit applies `- [x]` to the correct line in the correct file (verify by
      opening the cited file).
- [ ] Cascade flips **every** member of the confirmed group (consumes the
      Step-3/Step-4 grouping output via `cascade_group_members`, not
      text-search re-discovery). Textually-divergent siblings clustered by
      distinctive tokens get flipped, not only literal-text matches.
- [ ] Final summary reports `Applied: N` matching the count you accepted.
- [ ] Next `/check-items` run on the same project does NOT re-surface the
      checked-off items.

---

## Phase 8 — Security guards (manual probe)

**Setup:** Pick or create a session note with an open item whose surrounding
context contains:
- A frontmatter `project:` value with a `/` or `..` segment.
- An item whose text spans multiple lines or contains backticks/YAML markers.

Then run `/check-items <that-project>`.

**Pass criteria**
- [ ] Dashboard filename contains **only** `[A-Za-z0-9_-]+` in the `<scope>`
      slot. No dots (no `..` survival), no slashes, no whitespace.
- [ ] Dashboard YAML frontmatter parses cleanly (use `yq` or `python -c 'import
      yaml; yaml.safe_load(open(...))'`).
- [ ] No file created outside `<vault>/claude-dashboards/` (containment via
      `resolve()` + `is_relative_to()`).
- [ ] Cache JSON contains no shell-meta in keys; structure stays valid.

---

## Phase 9 — Oversized-payload fast path

**Setup:** Synthetic — create a project with many (~100+) coarse groups OR
edit `~/.claude/obsidian-brain-config.json` to point the
`sessions_folder` at a directory with high item density. Run `/check-items`.

**Pass criteria**
- [ ] Skill logs that the semantic-merge payload would exceed the 1 MB stdin
      cap and falls back to coarse grouping.
- [ ] `~/.claude/obsidian-brain/proto-stage-*.json` temp files are cleaned up
      (`ls ~/.claude/obsidian-brain/proto-stage-*.json` is empty after the run).
- [ ] No `subprocess` crash, no `BrokenPipeError`.

---

## Phase 10 — `/recall` no longer surfaces checkoff candidates

**Setup:** Run `/recall` in a project where Phase 7 just checked items off.

**Pass criteria**
- [ ] `/recall` skips the "I noticed these open items may now be done" branch
      entirely. There is **no** AskUserQuestion picker for checkoffs.
- [ ] Context brief is still rendered.
- [ ] SKILL.md does not import `batch_cascade_checkoff` (grep
      `skills/recall/SKILL.md` to confirm — convergence regression check).

---

## Phase 11 — Regression sweep on neighbor skills

Quick smoke that we didn't break anything adjacent. Run each, expect normal
behavior (no exceptions, dashboards/notes write where they should):

- [ ] `/standup` — should not surface already-merged items as open.
- [ ] `/vault-stats` — counts plausible, no unreadable-path explosion.
- [ ] `/vault-doctor` — runs to completion, no false-positive UUID failures.
- [ ] A trivial session boundary: start a fresh CC session, make one edit,
      end the session. Verify a session note lands at
      `<vault>/claude-sessions/<YYYY-MM-DD>-<project>-*.md` with
      `status: auto-logged`.

---

## Failure triage

| Symptom | Likely root cause | First check |
|---|---|---|
| All groups reclassified on warm run | `canonical_hash` mismatch Step 3↔Step 10 | `grep canonical_hash skills/check-items/SKILL.md` — Step 10 must reuse Step 3's hash, not recompute from `canonical_text` |
| Dashboard filename has `..` or `/` | `_safe_filename_component` regression | `hooks/check_items_report.py` — regex must exclude `.` |
| Sub-agent fails with `Argument list too long` | Prompt regressed to argv | `hooks/check_items_cli.py` — `subprocess.run(cmd, input=prompt, ...)` |
| `head_unavailable` groups never classified | Missing `_reason` tag | `skills/check-items/SKILL.md` Step 6 — failed-HEAD groups must carry `_reason` |
| Cache writes empty string | `_evidence_cache_put` poisoned by read failure | `hooks/open_item_dedup.py` — skip put on read failure |
| `/recall` still prompts for checkoffs | SKILL.md not synced or scope cut regression | Diff `~/.claude/skills/obsidian-brain/skills/recall/SKILL.md` vs. repo |

## Reporting

Capture in a single message back:
1. Pass/fail per phase (checkboxes above).
2. For any failure: phase number, observed vs. expected, the exact log/file
   lines.
3. Wall-clock timings for Phase 1 vs. Phase 2 (rough proof of cache savings).
4. Dashboard paths produced.

Ref: PR #158 · issue #87 · milestone v2.5.
