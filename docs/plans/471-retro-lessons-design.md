# Retro lessons rollup (#471)

Date: 2026-10-31. Status: approved by the user 2026-11-01.

## Why

The user ranks issues by friction. A lesson that keeps coming back across sessions is friction that has not been fixed. Today the vault holds 3,881 open, untraced Process Improvements items in 786 retro notes across 27 projects (measured on a copy, 2026-10-31). The same lessons are written again each session, and nothing says which ones already have an issue, a memory entry or a rule, or which keep coming back after one was written.

The user's words: the rollup "will inform issue prioritization. Issues causing friction most of the times across sessions will be top priority." An issue that is tracked but not picked up should be flagged so they raise its priority, and its milestone matters. Acting on the data "should be seamless and I don't need to remember. It should automatically work with my workflow."

## Goals

1. Group open retro items into **lessons**: sets of items, from different sessions, that state the same rule.
2. Match each lesson to existing tracking: a GitHub issue, a memory entry, or a skill / CLAUDE.md rule.
3. Show the lessons on an Obsidian Dataview dashboard, ranked by how many sessions they appeared in, in five buckets (below).
4. Keep the data current with no extra command: `/retro` updates it each session.
5. Ask the user to file or re-prioritise issues only in the weekly passes they already run: github-board `plan-week` and `/standup` (weekly).
6. Write traces back into the source retros in the #470 format, so the backlog is traced or explicitly left open with a reason.
7. Publish one read-only data contract (`report --json`) that other skills use for triage and milestone planning.

## Non-goals

- Writing to GitHub from the rollup itself. Only the filing skill writes, and only after the user says yes to that lesson.
- Writing memory files or CLAUDE.md rules. The 2026-04 `/backlog` design routed lessons into memory; that stays out. The rollup reports a lesson's tracking, it does not create memory.
- Grouping session notes or anything other than retro `## Process Improvements` items.
- A generic topic clustering. `/consolidate` themes already do that and stay separate.

## Anti-goals (a failure even if it works)

- Being pushy. `/retro` never asks about lessons. User ruling 2026-10-31: "Let's not get too pushy. Leave that to plan week", "And standup as well".
- Ticking an item as done because of a wrong match. A false match hides a live friction. Precision beats recall in matching.
- Touching the live vault, the live index DB or live GitHub from a test or a measurement (#471 acceptance criterion).

## Spike results (the grouping method is decided)

Both rounds are on #471 with the bars written before each run.

- Round 1, TF-IDF average-linkage themes: same-lesson keep rate 25% (bar 60%). Missed. TF-IDF finds the same topic, not the same rule.
- Round 2, TF-IDF top 10 candidates plus Haiku "same lesson?" verification: 100% keep on 17 confirmed items (both judges, κ 0.95), against 18% for top-5 cosine; recall 82%. Passed.
- Side check: Haiku on the round 1 pairs had 62% precision against the judges. Its misses were same-topic near misses.
- 40% of items have any same-lesson match in their top 10. Most lessons are one-offs.
- Round 3 (scale and drift, all bars on #471 before each run):
  - A. TF-IDF top 10 in pure Python with an inverted index: 1.0 s for all 3,881 items, 0.2 ms per new item. Pass.
  - B. Haiku throughput: 60–63 calls/min at concurrency 8, 0 failures in 3,881 calls. Pass.
  - C. Two-way confirmation: precision 62% → 64%, recall 82% → 47%. Fail; dropped.
  - C2. Sonnet as verifier: 50% precision. Fail. Haiku flips only 6 of 278 verdicts between runs; the asymmetry is between directions.
  - D. Verdict cache: SQLite 1.3 ms per update and read, against 35 ms for rewriting one JSON file. Pass.
  - Full graph: connected components chain into lessons of 562, 263 and 91 items that mix unrelated rules.
  - E1. Star grouping (below): largest lesson 23, coverage 91% of the components' items. Member precision 74% (Opus 78%, Astra 70%) against a bar of 80%: fail. Linked components score the same 74% per member, so 74% is Haiku's per-pair precision on natural candidates; star only removes the blobs.
  - C3. A stricter rubric: 88% precision but keeps 61% of true members (bar 80%). Fail; the round 2 rubric stays.
  - E2. Recency: 4 of the top 20 lessons by total sessions were last seen over 90 days ago; ranking by recent sessions changes 8 of 20.
  - E3. Stability: one week (118 new items) leaves 485 of 508 lessons unchanged; 24 change members, 29 are new, 3 split, 1 merges.

**What the design accepts:** per-pair precision is about 74%, so a lesson's session count can be inflated by about a quarter. That is acceptable for ranking. It is not acceptable for ticking items, so nothing is ticked without the user's batch confirm.

**Fixture (user ruling 2026-10-31: ids and labels only).** The repo is public and the items come from private projects. `tests/fixtures/471-eval/` holds, for rounds 1–3: item hashes, blind ids, keys, the judges' labels (Opus and `gpt-6-astra` for every round, plus the earlier `gpt-6.1-sol` labels for rounds 1–2), and the Haiku verdicts including the full graph. No item text. The text stays in a private copy under `~/.claude/obsidian-brain/471-eval/` (0o700). Two checks:
- CI: a test replays the frozen full-graph verdicts through the shipped star grouping and checks that it reproduces the frozen lessons, the round 2 score (≥ 70% keep) and the E1 star score on the judge labels. It mocks `claude -p`.
- The live verifier ran once in the spike, on a copy; its scores go in the fixture README.

## How it works

### Items

An item is any top-level Process Improvements item, ticked or not, traced or not, read with `retro_trace.parse_items` from `<vault>/<insights_folder>/*-retro-*.md`. Its project and date come from the note's frontmatter. Item identity is the note stem plus a hash of the item's normalised text with any trace removed, so tracing or renumbering an item keeps its verdicts.

All items count, because since #470 `/retro` Step 7.5 traces every new item before the session ends. A lesson that recurs after an issue was filed now arrives already traced (for example `skipped: covered by memory X`). It still counts as a recurrence. (My call, from the builder check.)

A member item's own trace is tracking evidence: an issue trace names the issue, a memory trace names the memory, a path trace names the rule. A `skipped:` trace names nothing.

### Candidates and verification

- TF-IDF (`hooks/tfidf.py`) over all items, with an inverted index over terms (spike A: 1.0 s for 3,881 items). Each item's 10 nearest items from **other** retro notes are its candidates.
- Haiku checks one anchor and its 10 candidates per call, with the round 2 rubric and examples, through `claude -p --model haiku` with `obsidian_utils.worker_env()`. That is the spike's exact shape; batching several anchors in one call is not allowed unless re-measured against the fixture.
- A pair is confirmed when Haiku confirmed it from either side. (Two-way confirmation failed spike C and is not used.)
- Verdicts live in a private SQLite file under `~/.claude/obsidian-brain/` (0o700 dir, 0o600 file), opened through `vault_index._connect`. The key is the unordered pair of item hashes plus the rubric version and the model. Changing the rubric or the model re-asks every pair; a rerun never asks about a pair already judged under the same rubric and model.
- Concurrency 8 (spike B: 60–63 calls/min). A Haiku failure leaves that anchor unjudged, never "different". The run reports the count, and a rerun retries it.

### Lessons (star grouping)

- Repeat: among the items not yet in a lesson, take the one with the most confirmed pairs to other such items (ties by item hash). It and its directly confirmed items form a lesson, and they leave the pool. Stop when no remaining item has a confirmed pair.
- Keep a lesson only if it spans at least 2 retro notes. Every other item is a one-off.
- Every member is confirmed against the lesson's centre, so a single bridge pair cannot join two rules into a blob (spike E1: largest 23, against 562 for connected components). No coherence check is needed.
- Each lesson gets a short imperative name from one batched Haiku naming call, in the style of the existing cluster-naming helper in `obsidian_utils`.
- **Lesson ids across runs.** A new lesson takes the id of the old lesson it shares most members with, if they share at least half of the smaller one. When two old lessons map to one new lesson, the loser is recorded as `merged_into: <id>`. When one old lesson's members end up in several new lessons, the others get new ids with `split_from: <id>`. Retired ids are kept in a small `retired.json` beside the lesson notes, so links and snoozes resolve. A snooze carries over to the lesson that takes the id, and to each `split_from` lesson.

### Matching to tracking

First, the lesson's own member traces: the issue, memory or rule named most often among them is its tracking, and needs no matching. Only a lesson with no such trace goes to matching. For it, candidates come from three sources, and Haiku confirms "does this already cover the lesson?" for the top 5 per source:
- **Issues:** open and closed issues in the repos of the lesson's projects. Project → repo comes from `git remote get-url origin` in `~/dev/claude_workspace/<project>`, with a config map override for projects outside that layout. Fields read: number, title, body, state, stateReason, milestone, labels, createdAt, closedAt.
- **Memory:** files from `memory_sources.memory_sources("claude-code")` (1,625 today), read by name, description and body.
- **Rules:** bullets in `~/.claude/CLAUDE.md`, each project repo's `CLAUDE.md`, and paragraphs of installed `SKILL.md` files.

A lesson takes its strongest confirmed match, in the order issue, then rule, then memory. If nothing is confirmed, its tracking is `none`.

**Matching gets its own spike before it is built (round 3).** A wrong match ticks items, so matching precision is a quality question like grouping. Bar to be written into #471 before the run: blind-judged precision of confirmed matches ≥ 80% on 20 lessons, two judge families, against TF-IDF-only top 1 and random. Matching is its own PR (Delivery). If round 3 misses, grouping still ships, with tracking from member traces only, and matching goes back to design.

**When tracking started:** issue `createdAt` (and `closedAt` for a closed one); for a rule, the date of the commit that added the line (`git log -S`) when the file is in a git repo; for memory, the file's mtime. mtime is a lower bound that moves on each edit, so it can only under-count recurrence. For tracking that comes from a member's memory or path trace, it is the date of the earliest retro carrying that trace. When no date can be found, `sessions_after_tracking` is empty and the lesson is not put in a recurrence bucket.

### Buckets

`sessions_after_tracking` counts the distinct retro notes in the lesson dated after tracking started.
1. **Raise priority:** open issue, `sessions_after_tracking > 0`. Shows milestone (or none) and priority label.
2. **File an issue:** tracking `none`, ranked by sessions.
3. **Fix didn't hold:** closed issue, lesson came back after `closedAt`.
4. **Rule not followed:** memory or rule match, `sessions_after_tracking > 0`.
5. **Tracked and quiet:** everything else with tracking. Folded.

Ranking within a bucket is by distinct sessions in the last 90 days, then total sessions, then distinct projects (spike E2). A lesson with no session in the last 90 days goes to a **dormant** fold under its bucket and shows "last seen N days ago".

### Output

- **Lesson notes:** one per lesson in `<vault>/<insights_folder>/lessons/`, `type: claude-lesson`, tags `claude/lesson` and `claude/project/<p>`. Frontmatter: `lesson_id, sessions, projects, first_seen, last_seen, tracking (issue|memory|rule|none), tracking_ref, tracking_since, issue_state, issue_milestone, issue_priority, sessions_after_tracking, bucket, snoozed_until`. The body lists each member item's wording with a `[[retro note]]` link. Written through the atomic note-writer path. The tool owns these notes, except `snoozed_until`, which it keeps. A note is written only when its content changes (spike E3: about 53 of 538 notes change in a week), which keeps Obsidian Sync quiet.
- **Dashboard:** `dashboards/retro-lessons.md`, installed by `/obsidian-setup` like the other six. Layout as in the approved mockup (artifact "Retro Lessons Dashboard Mockup").
- **`report --json`:** the data contract. One object per lesson with the frontmatter fields plus member item refs, plus run totals (open items, items in lessons, one-offs, unjudged pairs, largest lesson). Read-only, no Haiku calls; it reads the lesson notes. Every other skill uses this and nothing else.

Lesson notes are derived data. They must not count as new knowledge: `vault-ask`, auto-capture and capture selection skip `type: claude-lesson`. (Builder: check each reader that filters by type, as #455 did for `provisional`.)

### Trace write-back

- `retro_trace.apply_traces` gains a partial mode: it traces only the items given and leaves the rest untraced. The full-coverage check stays the default, so `/retro` Step 7.5 behaves as today.
- Per lesson: issue → issue trace (item stays open, ref joins `tracked_issues`); rule → path trace (ticks); memory → memory trace (ticks). Tracking `none` → no trace.
- Only untraced members get a trace. A member that already has one keeps it.
- **Review before ticking (user ruling 2026-10-31):** memory and rule matches are confirmed by the user in a batch in `/lessons` (one list of lesson → matched rule; accept all or untick the wrong ones). Issue traces do not tick, so they are written without that step. Bucket 5 is part of the batch list, so its matches get traced too.
- **Backfill:** `backfill --dry-run` (the default) prints the bucket table and the trace each item would get, and writes nothing. `backfill --apply --accept <file>` writes traces for the lessons in the accept file: JSON `[{"lesson_id", "tracking_ref"}]`, which `/lessons` writes and a test can write by hand. A lesson whose current `tracking_ref` differs from the accepted one is skipped and reported. A rerun changes nothing.
- After apply, the run reports the count of open items with no trace, and lists each with its reason: one-off, lesson with no tracking, unjudged pair, or note unreadable.

### Keeping it current (no command to remember)

- `/retro` starts `retro_rollup.py update <retro note>` detached after Step 7.5 has traced the note, and prints one line ("lesson update queued"). It never waits and never asks. The update checks each new item (one Haiku call per item, about 7 s each) and rewrites the lesson notes that changed. It never calls `gh`.
- **Weekly rebuild.** `plan-week` runs `retro_rollup.py rebuild` before it reads the data. It re-forms every lesson from the cached verdicts (no verdict calls at all: items left unjudged by a failed `update` stay unjudged until the next `update`, so a weekly run can never turn into a full backfill; judging is per anchor, so an anchor is not re-asked when its top 10 later shifts, and the new item's own call covers the new pair; only newly formed lessons get a naming call), so small drift from incremental updates is reset once a week. It drops cache rows whose items no longer exist (edited or deleted retro text).
- **Fresh tracking before any prompt.** `plan-week`, weekly `/standup` and `/lessons` run `retro_rollup.py refresh-tracking` first. It re-reads issue state, milestone, labels and dates with `gh` (read-only, one `gh issue list` per repo) and checks that each memory and rule `tracking_ref` still exists. A missing ref sets tracking to `gone`, which counts as untracked (bucket 2) and says so in the note.
- The one-time backfill is the first `update` over the whole vault: about 3,900 Haiku calls, about 65 minutes at concurrency 8 (spike B and the full-graph run). It runs in the background with resumable progress through the verdict cache, and with a stall watcher.

### Filing skill (`/lessons`)

- It walks buckets 1 and 2 (then 3 and 4), top first, and asks per lesson: **file** an issue, **raise** (show the existing issue with its milestone and priority so the user can change them), **skip**, or **snooze** (sets `snoozed_until`).
- File: picks the repo (single project → that repo; several projects → asks), drafts title and body from the lesson's member items, sets milestone and priority label after the user confirms, creates the issue, then writes issue traces into every member item.
- Raise: never edits GitHub without the user's explicit choice in that prompt.
- Called by github-board `plan-week` and by `/standup` in weekly mode. Both run it only when buckets 1–4 are non-empty, and say how many lessons wait.

### Other skills using the data

- In this repo: `/standup` gets a "Recurring lessons" section (top 5 across buckets 1–4) and calls `/lessons` in weekly mode. `/emerge` lists lessons whose sessions grew in its window.
- Other repos, each a separate issue pointing at `report --json`: `kickoff` (claude-code-config) shows the top lessons for the current project; github-board `triage-issues`, `plan-milestones` and `plan-week` (claude-code-skills) weight priority and milestone by a matched lesson's sessions and `sessions_after_tracking`.

## Constraints

- Python stdlib only (numpy and scipy are not allowed in shipped code). Spike A measured the inverted-index TF-IDF at 1.0 s for all items.
- Every vault write goes through the atomic note-writer path. Every path is contained in the vault with `resolve()` plus `is_relative_to()`.
- Every `gh` and `claude -p` call has stdin closed and a timeout. Tests mock both. Measurements run on a copy of the retro notes and on a saved issue snapshot.
- The architecture page (`docs/architecture/architecture.json`) is updated in each PR that adds a hook, CLI, skill or datastore.

## Setup, configuration and documentation

Each PR updates the docs and setup for what it adds, in the same PR. Nothing is left to a follow-up.

**`/obsidian-setup` needs an update (PR 1).** It writes the six dashboards from text embedded in `skills/obsidian-setup/SKILL.md` (Step 6), not by copying `dashboards/`. So PR 1:
- adds `retro-lessons.md` to Step 6 with the same "write only if missing" rule, so `upgrade` installs it on existing vaults without touching the other six;
- creates `<insights_folder>/lessons/` in Step 5;
- writes the new config keys with their defaults in fresh mode, and adds missing keys in upgrade mode without changing existing ones;
- updates its closing summary and the dashboard list (six becomes seven);
- adds `dashboards/retro-lessons.md` to the repo's `dashboards/` folder, byte-equal to the embedded copy, with a test that checks the two match.

**New config keys** (in `~/.claude/obsidian-brain-config.json`, all optional):
- `lessons_enabled` (default `true`): `false` turns off the `/retro` update and the weekly prompts.
- `lessons_recent_days` (default `90`): the recency window and the dormant cut-off.
- `lessons_concurrency` (default `8`): parallel Haiku calls.
- `project_repos` (default `{}`): project → `owner/repo` for projects outside `~/dev/claude_workspace/<project>`.

`/vault-config` lists and edits these four (PR 1).

**Docs per PR:**
- **README:** the Skills list (`/lessons` in PR 3), Dataview Dashboards (seven, with Retro Lessons described), Vault Structure (`claude-insights/lessons/`), Configuration (the four keys), and a short "Retro lessons" section after Auto-Capture: what it does, the weekly prompts, the one-time backfill and its cost (PR 1, extended in PRs 2–4).
- **Architecture page:** `docs/architecture/architecture.json` gains the `retro_rollup` CLI and module, the verdict-cache datastore, the lesson notes and `retired.json`, the `/retro` → update flow, the `plan-week` → rebuild and refresh flows, and the `/lessons` skill. `lastUpdated` and `version` are kept in sync, the HTML is re-rendered, and the smoke test passes `[main]` and `[sparse]` (every PR that adds one of these).
- **`CLAUDE.md`:** the "Key files" list gains `hooks/retro_rollup.py`, and the tag list gains `claude/lesson` (PR 1).
- **`CHANGELOG.md`:** an `[Unreleased]` entry per PR.
- **Skill docs:** `skills/retro/SKILL.md`, `skills/standup/SKILL.md` and `skills/emerge/SKILL.md` describe their new step or section (PR 4); `skills/lessons/SKILL.md` is new (PR 3), written with `/skill-authoring`.

## Delivery

Stacked PRs on one `/ship` run, each with its own issue:
1. **#471:** grouping engine (items, candidates, verification cache, star lessons, lesson ids and `retired.json`), tracking from member traces, `rebuild` and `refresh-tracking`, `report --json`, lesson notes, dashboard, partial apply, backfill dry run and apply with the accept file, the spike fixture. This meets all of #471's criteria: apply traces a lesson's untraced members from its member-trace tracking.
2. **#473:** matching against issues, memory and rules (round 3 spike first).
3. **#474:** `/lessons` filing skill with the batch confirm.
4. **#475:** `/retro` update step, plus the `/standup` and `/emerge` sections.
5. **Cross-repo issues** (not built here): `kickoff`, claude-code-config#422; `triage-issues`, `plan-milestones` and `plan-week`, claude-code-skills#283.

## Acceptance criteria (from #471, plus the spike)

- On the committed fixture, the shipped star grouping replays the frozen verdicts to the frozen lessons, scores ≥ 70% keep on round 2, and scores no lower than 74% on the E1 star labels.
- A read-only dry run on a copy prints the bucket table and writes nothing.
- Apply on a copy traces the accepted lessons' items using only #470 forms through the atomic path; a rerun changes nothing.
- After apply, the untraced open-item count is printed, and every remaining item is listed with a reason.
- No test or measurement touches the live vault, live DB or live GitHub.

## Left to the builder

- The exact Haiku prompt for matching, provided it has concrete examples like the grouping rubric.
- Lesson note file names (slug of the lesson name plus a short id).
