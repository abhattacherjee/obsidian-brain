---
name: lessons
description: "Walks the recurring retro lessons from the lessons rollup and lets the user act on each: file a GitHub issue, raise an existing issue's milestone or priority, skip, or snooze, then confirms rule and memory matches in one batch and writes the traces into the retro notes. GitHub changes only on the user's explicit choice. Use when: (1) /lessons or /lessons <N> to act on recurring retro lessons, (2) /standup or plan-week reports that lessons are waiting, (3) the user wants to file an issue for a lesson that keeps coming back, (4) the user wants to raise the priority of an issue that keeps being hit, (5) the user wants to confirm lesson-to-rule matches."
metadata:
  version: 1.0.0
---

# Lessons — Act on Recurring Retro Lessons

Reads the lessons rollup (`retro_rollup.py`, #471) and walks the lessons that keep coming back across sessions. Per lesson the user chooses: **file** an issue, **raise** the tracked issue, **skip**, or **snooze**. Then one batch confirm for rule and memory matches. Every state, note and GitHub-filing write is done by the CLI; this skill only asks questions and passes the answers on.

**Tools needed:** Bash, Read, Write, AskUserQuestion

## Rules (read first)

- **No GitHub change without an explicit choice.** Issues are created only by `backfill --apply --accept FILE`, after the dry run is shown and the user answers yes in Step 6. This skill never runs `gh issue create` and never POSTs to the GitHub API. `gh issue edit` runs only in the Raise step, after the user picked that exact change. If an edit fails, show the error and move on; never retry.
- Every `gh` and `python3` call runs with stdin closed (`< /dev/null`). `gh` calls other than the Raise edits are read-only.
- Retro item text leaves the machine only inside a draft the user confirmed in the File step.
- Titles, bodies, milestones and labels go into the accept file (JSON), never into a shell command. The Raise edits are the only commands that carry a milestone or label name: single-quote each value there, and write a `'` inside a value as `'\''`.
- Argument: an optional number N, the most lessons to walk (default 10).

## Procedure

Follow these steps in order.

### Step 1 — Load config

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys, os
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
sys.path.insert(0, _ob_hooks())
from obsidian_utils import load_config
c = load_config()
if not c.get("vault_path"):
    print("ERROR: vault_path not configured", file=sys.stderr)
    sys.exit(1)
print("HOOKS=" + _ob_hooks())
print("LESSONS_ENABLED=" + ("false" if c.get("lessons_enabled") is False else "true"))
print("PROJECT_REPOS=" + json.dumps(c.get("project_repos") if isinstance(c.get("project_repos"), dict) else {}))
' < /dev/null
```

Parse each output line as KEY=VALUE, splitting on the first `=`. If the command exits non-zero or prints ERROR, tell the user to run `/obsidian-setup` first, and stop.

If `LESSONS_ENABLED` is `false`, say "Lessons are turned off (`lessons_enabled` is false in the config)." and stop.

Every later block starts with the same two lines. Put the `HOOKS` value from this step between the quotes:

```bash
HOOKS='<HOOKS from Step 1>'
test -f "$HOOKS/retro_rollup.py" || { echo "ERROR: retro_rollup.py not found under $HOOKS - verify the obsidian-brain install resolved there is complete, then retry." >&2; exit 1; }
```

### Step 2 — Refresh tracking

```bash
HOOKS='<HOOKS from Step 1>'
test -f "$HOOKS/retro_rollup.py" || { echo "ERROR: retro_rollup.py not found under $HOOKS" >&2; exit 1; }
python3 "$HOOKS/retro_rollup.py" refresh-tracking < /dev/null
```

Exit 1 means some `gh` or memory lookups failed. Show the error lines, say the issue state may be stale, and carry on: the rest of the run still works, but check an issue's state with `gh issue view` in the Raise step before offering a change.

### Step 3 — Read the report

```bash
HOOKS='<HOOKS from Step 1>'
test -f "$HOOKS/retro_rollup.py" || { echo "ERROR: retro_rollup.py not found under $HOOKS" >&2; exit 1; }
python3 "$HOOKS/retro_rollup.py" report --json < /dev/null
```

From `lessons[]`, drop every lesson with `dormant` true, and every lesson with `snoozed_until` on or after today (a snooze that ends today still hides the lesson). Walk the rest in bucket order `raise`, `file`, `didnt_hold`, `rule_not_followed`, keeping the report's order inside a bucket (`quiet` is not walked), up to N lessons. If none are left, go on to Step 5: quiet and `rule_not_followed` matches may still need a batch confirm. Say "No lessons need action." and stop only when Step 5 finds nothing either.

Keep a pending accept list in your head, empty to start. Entries are added in Step 4 and Step 5.

### Step 4 — Per lesson

Print one line: name, bucket, sessions, projects, tracking ref (or "no tracking"), issue milestone and priority. Then ask one AskUserQuestion with these options:

- **File** — only for bucket `file` (the report's `tracking` is `none` or `gone`). The CLI treats gone tracking as none, so File works for both.
- **Raise** — only for a lesson whose tracking is an issue (buckets `raise` and `didnt_hold`).
- **Skip** — do nothing, go to the next lesson.
- **Snooze** — hide the lesson until a date.

For buckets `didnt_hold` (closed issue) and `rule_not_followed`, offer only **Skip** and **Snooze**, plus **Raise** when the tracking is an issue. Re-filing a closed issue is a separate decision and is not offered here.

#### Step 4a — File

1. Read the members:

```bash
HOOKS='<HOOKS from Step 1>'
test -f "$HOOKS/retro_rollup.py" || { echo "ERROR: retro_rollup.py not found under $HOOKS" >&2; exit 1; }
python3 "$HOOKS/retro_rollup.py" show '<LESSON_ID>' --json < /dev/null
```

2. Pick the repo. One project: use the `PROJECT_REPOS` entry for it, else the `origin` remote of `~/dev/claude_workspace/<project>`:

```bash
git -C "$HOME/dev/claude_workspace/<project>" remote get-url origin < /dev/null
```

   Turn the URL into `owner/repo`. Several projects: ask which repo. Show the repo and let the user correct it. The accept entry always carries the confirmed `repo`.
3. Draft a title (under 80 characters, imperative) and a body: the problem in two sentences, the member wordings as quoted bullets (with date and project), and a proposed fix. Show the draft.
4. Ask the milestone (read-only) and the priority label (read-only):

```bash
gh api 'repos/<OWNER/REPO>/milestones?state=open' --jq '.[].title' < /dev/null
gh label list --repo '<OWNER/REPO>' --limit 100 --json name --jq '.[].name' < /dev/null
```

   Offer the milestone titles and the priority labels (names that start with `P` and a digit). "None" is allowed for both. If either lookup fails, say so and draft with no milestone and no labels.
5. AskUserQuestion: **Use this draft**, **Edit the draft**, or **Drop it**. Edit loops back to the draft. On "Use this draft", add `{"lesson_id", "file_issue": true, "repo", "title", "body", "milestone", "labels"}` to the pending accept list (leave out `milestone` when none, and `labels` when none). Nothing is sent to GitHub now.

Limits the CLI enforces (a bad entry refuses the whole file): title 1-256 characters on one line; body at most 20,000 characters; milestone 1-100 characters; at most 10 labels of 1-50 characters, none with a comma or newline.

#### Step 4b — Raise

Show the issue's state, milestone and priority from the report, and refresh them:

```bash
gh issue view '<N>' --repo '<OWNER/REPO>' --json state,milestone,labels --jq '{state, milestone: .milestone.title, labels: [.labels[].name]}' < /dev/null
```

Ask: **Keep**, **Change milestone**, **Change priority**. Only for a change the user picked, show the exact command and run it:

```bash
gh issue edit '<N>' --repo '<OWNER/REPO>' --milestone '<MILESTONE TITLE>' < /dev/null
gh issue edit '<N>' --repo '<OWNER/REPO>' --add-label '<NEW P LABEL>' --remove-label '<OLD P LABEL>' < /dev/null
```

If a `gh issue edit` fails, say so, show the error, and do not add the entry below for that lesson. Otherwise add `{"lesson_id", "tracking_ref"}` (the report's `tracking_ref`, `owner/repo#N`) to the pending accept list, so the lesson's untraced members get the issue trace. Issue traces do not tick items, so no batch review is needed for them.

#### Step 4c — Snooze

Ask for a date (default: today plus 30 days), then:

```bash
HOOKS='<HOOKS from Step 1>'
test -f "$HOOKS/retro_rollup.py" || { echo "ERROR: retro_rollup.py not found under $HOOKS" >&2; exit 1; }
python3 "$HOOKS/retro_rollup.py" snooze '<LESSON_ID>' '<YYYY-MM-DD>' < /dev/null
```

Exit 2 means a bad id or date: show the message and ask again. Skip and Snooze never add to the accept list.

### Step 5 — Batch confirm of rule and memory matches

Lessons tracked by a memory entry or a rule (buckets `rule_not_followed` and `quiet`) can have untraced members. List what a trace would do:

```bash
HOOKS='<HOOKS from Step 1>'
test -f "$HOOKS/retro_rollup.py" || { echo "ERROR: retro_rollup.py not found under $HOOKS" >&2; exit 1; }
python3 "$HOOKS/retro_rollup.py" backfill < /dev/null
```

In the output, `plan[]` has one row per untraced item (`lesson`, `note`, `item`, `trace`). Keep the lessons whose report `tracking` is `memory` or `rule` and whose bucket is `rule_not_followed` or `quiet`. Drop every lesson Step 3 drops (`dormant` true, or `snoozed_until` on or after today) and every lesson snoozed in Step 4c in this run. Print them as one numbered list: `N. lesson name → matched ref (items to trace)`. If there are none, skip to Step 6.

Ask one AskUserQuestion: **Accept all**, **Accept all except some** (the user names the numbers to leave out), or **Skip this batch**. Each accepted lesson adds `{"lesson_id", "tracking_ref"}` to the pending accept list. These traces tick the items, which is why the user sees the matches first.

### Step 6 — Dry run, confirm, apply

If the accept list is empty, say so and stop.

Create the accept file (owner-only, under the state directory), then write the pending list into it as a JSON array with the Write tool:

```bash
ACCEPT=$(python3 -c 'import os, tempfile; d = os.path.expanduser("~/.claude/obsidian-brain"); os.makedirs(d, mode=0o700, exist_ok=True); fd, p = tempfile.mkstemp(dir=d, prefix="lessons-accept-", suffix=".json"); os.close(fd); print(p)' < /dev/null) && echo "ACCEPT=$ACCEPT"
```

Dry run (no `--apply`): it reads the file, calls no `gh`, and writes nothing.

```bash
HOOKS='<HOOKS from Step 1>'
test -f "$HOOKS/retro_rollup.py" || { echo "ERROR: retro_rollup.py not found under $HOOKS" >&2; exit 1; }
python3 "$HOOKS/retro_rollup.py" backfill --accept '<ACCEPT path>' < /dev/null
```

Exit 2: the file is invalid (the message names the field). Fix the entry and write the file again. Otherwise show:

- `planned_filings`: repo, title, milestone, labels, and `body_chars` for each issue to file. A row with reason `pending reconcile` may be filed: the CLI first checks GitHub for an earlier issue and files only if there is none. A row with any other `reason` is not filed (an existing issue is adopted, or no repo was found).
- `skipped_entries`: each entry and its reason.
- `plan`: the number of traces per lesson.

Ask once: "Apply: N issues to file, P pending reconcile (may file), M lessons to trace?" with **Yes** and **No**. N is the number of `planned_filings` without a `reason`; P is the number with reason `pending reconcile`; M is the number of lessons in `plan` plus N.

- **No** — delete the accept file (`rm -f '<ACCEPT path>'`). Nothing changes.
- **Yes** — run the apply, then delete the accept file whatever the result:

```bash
HOOKS='<HOOKS from Step 1>'
test -f "$HOOKS/retro_rollup.py" || { echo "ERROR: retro_rollup.py not found under $HOOKS" >&2; exit 1; }
python3 "$HOOKS/retro_rollup.py" backfill --apply --accept '<ACCEPT path>' < /dev/null
echo "exit=$?"
rm -f '<ACCEPT path>'
```

Show `filed`, `reconciled`, `notes_written`, `skipped_entries`, `untraced_open`, and the exit status. Exit 1 with a `pending_filings` or "filing failed" reason means an issue may exist: tell the user to rerun `/lessons` (the CLI looks for the issue first and never files twice).

## Notes

- The report's `snoozed_until` is read live from the lesson note, so a date the user typed in Obsidian counts.
- A snooze survives rebuilds and carries over when lessons merge or split.
- `didnt_hold` lessons (a closed issue that came back) are shown but not re-filed here.
