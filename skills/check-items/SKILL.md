---
name: check-items
description: Triage open `- [ ]` items across your Obsidian vault with evidence-grounded AI classification. Auto-closes items shipped by merged PRs, surfaces items needing external action (e.g. `gh issue close`), hides stale items by default. Replaces the old token-overlap heuristic with a two-pass AI pipeline backed by a persistence cache. Use when: (1) sweeping a project for done work, (2) auditing what's still actionable, (3) recovering from /recall deferral fatigue.
---

# /check-items

## Invocation

```
/check-items                     # current project, 14d window (default)
/check-items <project>           # named project, 14d window
/check-items all                 # every project with open items in window
/check-items 30d                 # current project, widen window
/check-items --show-all          # include LOW-confidence + STALE
/check-items --dry-run           # run pipeline, write report, skip edit-confirm loop
/check-items --no-cache          # force re-classification of every group
```

Arguments are order-independent and combinable: `/check-items all 30d --show-all`.

## Step 1 — Parse arguments and resolve scope

Run this bash block. It parses `$ARGUMENTS` per the invocation contract above (positional project / `all` / `Nd`, plus the three flags). Output goes to a temp directory under `~/.claude/obsidian-brain/`; the printed path is captured in `$scope_path` and passed to every subsequent step as `"$scope_path"`.

Each step below is a bash block; the embedded Python reads its inputs from `$1`, `$2`, … via `sys.argv`. Pass `"$scope_path"` captured here as the first arg, and any previous step's output path as subsequent args.

```bash
ARGUMENTS="${ARGUMENTS:-}"
scope_path=$(python3 -c "
import sys, os, glob, json, tempfile
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
sys.path.insert(0, _ob_hooks())
from check_items_args import parse_scope, _known_projects, _vault_known_projects

argv = sys.argv[1:]
scope_obj = parse_scope(argv)
if scope_obj.unknown_tokens:
    _near = sorted(p for p in (_known_projects() | _vault_known_projects())
                   if any(t.lower() in p.lower() or p.lower() in t.lower()
                          for t in scope_obj.unknown_tokens))[:5]
    print('ERROR: unrecognised argument(s): '
          + ', '.join(repr(t) for t in scope_obj.unknown_tokens), file=sys.stderr)
    if _near:
        print('Did you mean: ' + ', '.join(_near), file=sys.stderr)
    print('Valid forms: <project> | all | Nd | --show-all | --dry-run | --no-cache',
          file=sys.stderr)
    sys.exit(2)
scope = {
    'mode': scope_obj.mode,
    'project': scope_obj.project,
    'window_days': scope_obj.window_days,
    'show_all': scope_obj.show_all,
    'dry_run': scope_obj.dry_run,
    'no_cache': scope_obj.no_cache,
}

workdir = tempfile.mkdtemp(prefix='check-items-', dir=os.path.expanduser('~/.claude/obsidian-brain'))
os.chmod(workdir, 0o700)
scope_path = os.path.join(workdir, 'scope.json')
with open(scope_path, 'w') as f:
    json.dump(scope, f)
os.chmod(scope_path, 0o600)
print(scope_path)
" $ARGUMENTS)

echo "scope_path=$scope_path"
cat "$scope_path"
```

Save the printed `scope.json` path in `$scope_path`; pass it to every subsequent step as the first arg.

Note: `window_days` in scope controls how many sessions' files to pass as `basenames` in Step 5. The `collect_open_items` helper itself scans by `max_sessions` count (not calendar days); to apply a window filter, limit the basenames list to files dated within the window before passing to `deep_analysis_pipeline`.

**Unrecognised arguments are fatal (#318).** `parse_scope` records any token that is not a flag, `all`, an `Nd` window, or a known project on `scope.unknown_tokens`, and the block above exits 2 rather than proceeding. A silently-dropped project name does not degrade the run — it makes the run answer about the *current* project while appearing to answer about the one that was named. Known projects are the union of workspace-root directories and every `project` value in the vault index, so a notes-only project with no git repo is recognised.

If the block exits 2, stop and show the user the stderr verbatim; do not fall through to Step 2.

## Step 2 — Collect open items (Stage 1)

```bash
raw_path=$(python3 -c "
import sys, os, glob, json, itertools
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
sys.path.insert(0, _ob_hooks())
from open_item_dedup import collect_open_items

scope_path = sys.argv[1]
scope = json.load(open(scope_path))
config_path = os.path.expanduser('~/.claude/obsidian-brain-config.json')
try:
    config = json.load(open(config_path))
    vault_path = config.get('vault_path')
    if not vault_path:
        raise ValueError('vault_path missing from config')
except (OSError, json.JSONDecodeError, ValueError) as exc:
    print(f'ERROR: obsidian-brain config not loadable ({exc}); run /obsidian-setup', file=sys.stderr)
    sys.exit(1)
sessions_folder = config.get('sessions_folder', 'claude-sessions')

# Resolve project list from scope
if scope['mode'] == 'vault':
    # All projects: collect from all session notes without project filter.
    # We use a sentinel to indicate vault-wide scan below.
    projects = None
elif scope['mode'] == 'project' and scope['project']:
    projects = [scope['project']]
else:
    # current: derive project name from cwd git repo name
    import subprocess
    res = subprocess.run(
        ['git', 'rev-parse', '--show-toplevel'],
        capture_output=True, text=True
    )
    if res.returncode != 0:
        print('ERROR: /check-items current-project mode requires running inside a git repo.\n'
              'Use /check-items all (vault-wide) or /check-items <project>.', file=sys.stderr)
        sys.exit(1)
    cwd_proj = os.path.basename(res.stdout.strip()) if res.returncode == 0 else None
    projects = [cwd_proj] if cwd_proj else None

raw_items = []
if projects is None:
    # vault-wide: scan all session files, no project filter
    # collect_open_items requires a project arg; use per-project discovery
    sessions_dir = os.path.join(vault_path, sessions_folder)
    if os.path.isdir(sessions_dir):
        seen_projects = set()
        for fname in os.listdir(sessions_dir):
            if not fname.endswith('.md'):
                continue
            fpath = os.path.join(sessions_dir, fname)
            try:
                with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                    for line in itertools.islice(f, 20):
                        if line.strip().startswith('project:'):
                            proj = line.strip().split(':', 1)[1].strip().strip('\"').strip(\"'\")
                            if proj:
                                seen_projects.add(proj)
                            break
            except OSError:
                continue
        for proj in sorted(seen_projects):
            items = collect_open_items(
                vault_path=vault_path,
                sessions_folder=sessions_folder,
                project=proj,
                max_sessions=50,
            )
            for fpath, line_num, item_text in items:
                raw_items.append({
                    'file': os.path.basename(fpath),
                    'path': fpath,
                    'line': line_num,
                    'text': item_text,
                    'project': proj,
                })
else:
    for proj in projects:
        if proj is None:
            continue
        items = collect_open_items(
            vault_path=vault_path,
            sessions_folder=sessions_folder,
            project=proj,
            max_sessions=50,
        )
        for fpath, line_num, item_text in items:
            raw_items.append({
                'file': os.path.basename(fpath),
                'path': fpath,
                'line': line_num,
                'text': item_text,
                'project': proj,
            })

out_path = os.path.join(os.path.dirname(scope_path), 'raw_items.json')
with open(out_path, 'w') as f:
    json.dump(raw_items, f, indent=2)
os.chmod(out_path, 0o600)
print(out_path)
" "$scope_path")

echo "raw_path=$raw_path"
```

## Step 3 — Coarse-group + cache partition (Stage 2a + cache load)

Convention: each step is a `bash` block. Steps 1-2 use `python3 -c "..."` with positional argv tokens (inputs passed as `sys.argv` args, e.g. `python3 -c "..." "$scope_path"`). Steps 3-10 use `python3 << 'PYEOF' ... PYEOF` heredoc with inputs passed via environment variables (e.g. `SCOPE_PATH="$scope_path" python3 << 'PYEOF' ... PYEOF`); the Python reads them via `os.environ["VAR_NAME"]` — never via `sys.argv`. Both patterns produce a runnable shell block — never paste raw `python3` blocks that rely on `sys.argv` without an argv-passing wrapper, and never use env-var heredoc style for Steps 1-2 which expect positional args.

```bash
part_path=$(SCOPE_PATH="$scope_path" RAW_PATH="$raw_path" python3 << 'PYEOF'
import sys, os, glob, json, subprocess
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
from open_item_dedup import find_duplicates, cross_project_dedup
from check_items_cache import canonical_hash, load_cache, partition
from obsidian_utils import get_workspace_roots

scope_path = os.environ["SCOPE_PATH"]
raw_path = os.environ["RAW_PATH"]
scope = json.load(open(scope_path))
raw_items = json.load(open(raw_path))

# Build coarse groups per project using per-candidate find_duplicates loop.
# find_duplicates(candidate_text, existing_items, threshold=5) is per-candidate.
by_project = {}
for it in raw_items:
    by_project.setdefault(it.get("project", "unknown"), []).append(it)

coarse_by_proj = {}
for proj, items in by_project.items():
    # Convert to the (fpath, line_num, item_text) tuples that find_duplicates expects
    tuples = [(it["path"], it["line"], it["text"]) for it in items]
    seen_grouped = set()
    groups = []
    for idx, (fpath, line_num, item_text) in enumerate(tuples):
        if idx in seen_grouped:
            continue
        others = [(f, l, t) for j, (f, l, t) in enumerate(tuples) if j != idx]
        dupes = find_duplicates(item_text, others)
        members = [{"file": os.path.basename(fpath), "line": line_num, "text": item_text,
                     "mtime": os.path.getmtime(fpath) if os.path.exists(fpath) else 0}]
        for df, dl, dt, dc in dupes:
            for j, (f2, l2, t2) in enumerate(tuples):
                if os.path.abspath(f2) == os.path.abspath(df) and l2 == dl:
                    seen_grouped.add(j)
            members.append({"file": os.path.basename(df), "line": dl,
                             "text": dt, "confidence": dc,
                             "mtime": os.path.getmtime(df) if os.path.exists(df) else 0})
        seen_grouped.add(idx)
        import uuid
        g = {
            "group_id": str(uuid.uuid4())[:8],
            "project": proj,
            "representative": item_text,
            "members": members,
            "canonical_hash": canonical_hash(item_text),
        }
        groups.append(g)
    coarse_by_proj[proj] = groups

flat_groups = cross_project_dedup(coarse_by_proj) if scope["mode"] == "vault" else \
              [g for v in coarse_by_proj.values() for g in v]

# Cache partition (per project).
cache = load_cache()
known, needs = [], []
heads = {}
for proj, groups in coarse_by_proj.items():
    repo_path = None
    for _root in get_workspace_roots():
        _candidate = os.path.join(_root, proj)
        if os.path.isdir(os.path.join(_candidate, ".git")):
            repo_path = _candidate
            break
    if not repo_path:
        print(f"[check-items] no repo found for {proj} in {get_workspace_roots()}; forcing reclassify",
              file=sys.stderr)
        for g in groups:
            g["_reason"] = "head_unavailable"  # ensure Step 6 includes these in to_classify
        needs.extend(groups)
        continue
    head_proc = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=10,
    )
    if head_proc.returncode != 0 or not head_proc.stdout.strip():
        print(f"[check-items] no HEAD for {proj} ({repo_path}); forcing reclassify",
              file=sys.stderr)
        for g in groups:
            g["_reason"] = "head_unavailable"  # ensure Step 6 includes these in to_classify
        needs.extend(groups)
        continue
    head = head_proc.stdout.strip()
    heads[proj] = head
    k, n = partition(groups, cache, project=proj, head_sha=head, force=scope["no_cache"])
    known.extend(k)
    needs.extend(n)

out = os.path.join(os.path.dirname(scope_path), "partition.json")
with open(out, "w") as f:
    json.dump({"flat_groups": flat_groups, "known": known, "needs": needs, "heads": heads}, f, indent=2)
os.chmod(out, 0o600)
print(out)
PYEOF
)

echo "part_path=$part_path"
```

## Step 4 — Semantic merge on needs-reclassification set (Stage 2b)

```bash
merged_path=$(SCOPE_PATH="$scope_path" PART_PATH="$part_path" python3 << 'PYEOF'
import sys, os, glob, json
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
from open_item_dedup import merge_groups_semantically, get_last_semantic_merge_mode

scope_path = os.environ["SCOPE_PATH"]
part_path = os.environ["PART_PATH"]
data = json.load(open(part_path))
needs = data["needs"]
known = data["known"]

# Run semantic merge only over needs-reclassification groups so that known
# (already-classified) groups are never absorbed into a canonical group and
# silently lose their _reason flag. Step 6 filters to groups with _reason set;
# if a needs-group were merged into a known canonical, it would be skipped.
needs_by_proj = {}
for g in needs:
    needs_by_proj.setdefault(g["project"], []).append(g)

# merge_groups_semantically accepts dict {project: [groups]}; returns same shape.
merged_needs_by_proj = merge_groups_semantically(needs_by_proj) if needs_by_proj else {}
mode = get_last_semantic_merge_mode()

# Splice known (untouched) back in after merge so Step 6 can iterate all groups.
merged_by_proj = {}
for proj, groups in merged_needs_by_proj.items():
    merged_by_proj.setdefault(proj, []).extend(groups)
for g in known:
    merged_by_proj.setdefault(g["project"], []).append(g)

out = os.path.join(os.path.dirname(scope_path), "merged.json")
with open(out, "w") as f:
    json.dump({"merged_by_proj": merged_by_proj, "mode": mode}, f, indent=2)
os.chmod(out, 0o600)
print(out)
PYEOF
)

echo "merged_path=$merged_path"
```


## Step 5 — Gather evidence (Stage 3)

```bash
evidence_path=$(SCOPE_PATH="$scope_path" MERGED_PATH="$merged_path" python3 << 'PYEOF'
import sys, os, glob, json, datetime
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
from open_item_dedup import deep_analysis_pipeline

scope_path = os.environ["SCOPE_PATH"]
merged_path = os.environ["MERGED_PATH"]
scope = json.load(open(scope_path))
data = json.load(open(merged_path))
_config_path = os.path.expanduser("~/.claude/obsidian-brain-config.json")
try:
    config = json.load(open(_config_path))
    vault_path = config.get("vault_path")
    if not vault_path:
        raise ValueError("vault_path missing from config")
except (OSError, json.JSONDecodeError, ValueError) as exc:
    print(f"ERROR: obsidian-brain config not loadable ({exc}); run /obsidian-setup", file=sys.stderr)
    sys.exit(1)
sessions_folder = config.get("sessions_folder", "claude-sessions")
insights_folder = config.get("insights_folder", "claude-insights")

# Collect session file basenames within window_days to scope evidence gathering.
window_days = scope.get("window_days", 14)
cutoff = (datetime.date.today() - datetime.timedelta(days=window_days)).isoformat()
sessions_dir = os.path.join(vault_path, sessions_folder)
basenames = []
if os.path.isdir(sessions_dir):
    for fname in sorted(os.listdir(sessions_dir), reverse=True):
        if not fname.endswith(".md"):
            continue
        # Filenames are YYYY-MM-DD-* — date prefix determines recency
        if len(fname) < 10 or fname[4] != "-" or fname[7] != "-":
            continue
        date_prefix = fname[:10]
        if date_prefix < cutoff:
            break  # sorted reverse-chronological; once below cutoff we're done
        basenames.append(fname)

projects = list(data["merged_by_proj"].keys()) if isinstance(data["merged_by_proj"], dict) else []
output_path = os.path.join(os.path.dirname(scope_path), "pipeline_evidence.json")

# deep_analysis_pipeline(basenames, projects_json, output_path, vault_path,
#                        sessions_folder, insights_folder, db_path=None)
# Returns "OK:<total>:<groups>:<N>" status string; data is written to output_path.
status = deep_analysis_pipeline(
    basenames=basenames,
    projects_json=json.dumps(projects),
    output_path=output_path,
    vault_path=vault_path,
    sessions_folder=sessions_folder,
    insights_folder=insights_folder,
)
if not status.startswith("OK"):
    print(f"WARNING: deep_analysis_pipeline returned: {status}", file=sys.stderr)

# Read the written evidence from output_path for downstream use.
try:
    pipeline_data = json.load(open(output_path))
    evidence = pipeline_data.get("evidence", {})
except (OSError, json.JSONDecodeError) as e:
    print(f"WARNING: could not read pipeline output: {e}", file=sys.stderr)
    evidence = {}

out = os.path.join(os.path.dirname(scope_path), "evidence.json")
with open(out, "w") as f:
    json.dump(evidence, f, default=str, indent=2)
os.chmod(out, 0o600)
print(out)
PYEOF
)

echo "evidence_path=$evidence_path"
```

## Step 6 — Classify (Stage 4) with fallback chain

```bash
classifications_path=$(SCOPE_PATH="$scope_path" MERGED_PATH="$merged_path" EVIDENCE_PATH="$evidence_path" python3 << 'PYEOF'
import sys, os, glob, json
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
from open_item_dedup import (
    classify_groups_with_agent, classify_groups_heuristic, get_last_classifier_mode
)

scope_path = os.environ["SCOPE_PATH"]
merged_path = os.environ["MERGED_PATH"]
evidence_path = os.environ["EVIDENCE_PATH"]
data = json.load(open(merged_path))
evidence = json.load(open(evidence_path))

# Filter to needs-reclassification only (groups with _reason set).
all_merged = [g for v in data["merged_by_proj"].values() for g in v] \
             if isinstance(data["merged_by_proj"], dict) \
             else data["merged_by_proj"]
to_classify = [g for g in all_merged if g.get("_reason")]

primary = classify_groups_with_agent(to_classify, evidence)
mode = get_last_classifier_mode()

# Provenance travels with each verdict: Step 7 refuses to auto-check a
# heuristic one, Step 10 refuses to cache it.
for c in primary:
    c.setdefault("classifier_source", "agent")

# Gap-fill. classify_groups_with_agent may return a PARTIAL list when only
# some chunks failed (#297). Fill exactly the groups it did not cover rather
# than discarding the agent's good work. On total failure primary == [], so
# this degenerates to the old whole-set heuristic fallback.
_done_ids = {c.get("group_id") for c in primary}
_missing = [g for g in to_classify if g.get("group_id") not in _done_ids]
if _missing:
    for c in classify_groups_heuristic(_missing, evidence):
        c["classifier_source"] = "heuristic"
        primary.append(c)

# Merge cached classifications (known_unchanged) with fresh.
classifications = list(primary)
for g in all_merged:
    if g.get("_cached_classification"):
        classifications.append({
            "group_id": g.get("group_id"),
            "classification": g["_cached_classification"],
            "confidence": g.get("_cached_confidence", "LOW"),
            "canonical_text": g.get("representative", ""),
            "evidence_citation": g.get("_cached_evidence_citation"),
            "action_required": g.get("_cached_action_required"),
            "project": g.get("project"),
            "classifier_source": "cache",
        })

out = os.path.join(os.path.dirname(scope_path), "classifications.json")
with open(out, "w") as f:
    json.dump({"classifications": classifications, "classifier_mode": mode}, f, indent=2)
os.chmod(out, 0o600)
print(out)
PYEOF
)

echo "classifications_path=$classifications_path"
```

## Step 7 — Apply tier rules + present review (Stage 5)

```bash
buckets_path=$(SCOPE_PATH="$scope_path" CLASSIFICATIONS_PATH="$classifications_path" python3 << 'PYEOF'
import sys, os, glob, json
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
from open_item_dedup import assign_tier, partition_for_review

scope_path = os.environ["SCOPE_PATH"]
classifications_path = os.environ["CLASSIFICATIONS_PATH"]
scope = json.load(open(scope_path))
data = json.load(open(classifications_path))
for item in data["classifications"]:
    item["tier"] = assign_tier(item.get("evidence_citation"),
                               item.get("canonical_text"),
                               item.get("classification"),
                               item.get("classifier_source"))

buckets = partition_for_review(data["classifications"], show_all=scope["show_all"])

mode = data.get("classifier_mode", "ok")
if mode != "ok":
    _deg = sum(1 for i in data["classifications"]
               if i.get("classifier_source") == "heuristic")
    print(f"\n!! CLASSIFIER DEGRADED (mode={mode}) — {_deg} of "
          f"{len(data['classifications'])} verdict(s) come from the "
          f"token-overlap heuristic, not evidence.", file=sys.stderr)
    print("   Heuristic citations read 'token X near completion phrase Y'. "
          "That is co-occurrence, NOT proof the item is done.", file=sys.stderr)
    print("   A citation reading 'DONE rejected — ... but <cue> governs it' is "
          "the #299 conditional guard: the co-occurrence was a pending or "
          "forward-looking reference, so the item stayed open.", file=sys.stderr)
    print("   They are capped at tier MED and are never preselected. "
          "Verify each one manually before accepting.", file=sys.stderr)

# Format: HIGH first, MED next, LOW (only if show_all). DONE preselected [x],
# NEEDS-ACTION [ ] with action_required command surfaced. REVIEW always [ ]
# (never auto-checked — assign_tier caps REVIEW at MED, so it never sorts
# into the HIGH+DONE preselected group either). Same cap applies to any
# verdict whose classifier_source is not agent/prefilter/cache (#297) — a
# heuristic verdict can never reach HIGH, so it can never be preselected.
print("\n=== Review ===", file=sys.stderr)
for item in sorted(buckets["review"],
                   key=lambda x: ("HIGH MED LOW".split().index(x.get("tier", "LOW")),
                                  x.get("classification"))):
    mark = "[x]" if item["classification"] == "DONE" and item["tier"] == "HIGH" else "[ ]"
    _marker = " [heuristic]" if item.get("classifier_source") == "heuristic" else ""
    print(f"  {mark} ({item['classification']}/{item['tier']}) {item['canonical_text']}{_marker}", file=sys.stderr)
    print(f"      evidence: {item.get('evidence_citation')}", file=sys.stderr)
    if item.get("action_required"):
        print(f"      action:   {item['action_required']}", file=sys.stderr)

out = os.path.join(os.path.dirname(scope_path), "buckets.json")
with open(out, "w") as f:
    json.dump(buckets, f, indent=2)
os.chmod(out, 0o600)
print(out)
PYEOF
)

echo "buckets_path=$buckets_path"
```

If `scope.dry_run` is true OR the user types `none` at the confirm prompt: skip Step 8 (Edit + cascade) and go straight to Step 9 (dashboard). The dashboard is ALWAYS written.

## Step 8 — Apply confirmed checkoffs (Stage 6) + cascade (Stage 7)

For each item the user kept selected (default-selected for HIGH+DONE, opt-in for everything else):

1. Read the target line via Read tool.
2. Verify the line matches the preview (memory `feedback_open_item_checkoff_verify_before_edit`).
3. If mismatch: surface the diff, ABORT this item (do not flip), continue with the next.
4. If match: use Edit tool to flip `- [ ]` → `- [x]` on that line only. After a successful flip, append `[full_file_path, line_number]` to a `source_skips` tracking list.

After the user-confirmed batch, write the source-skips tracking list to a tempfile and run cascade via `cascade_group_members` (which consumes the Step-3/Step-4 grouping output directly, so textually-divergent siblings clustered by distinctive tokens are also flipped — not just text-search re-discovery matches):

```bash
# Write source_skips JSON — list of [full_path, line_number] pairs for lines
# the SKILL already primary-flipped above.  Replace the contents below with the
# actual list collected in the primary-flip loop.
_skips_file=$(python3 -c "
import tempfile, os, json, sys
d = os.path.expanduser('~/.claude/obsidian-brain')
os.makedirs(d, mode=0o700, exist_ok=True)
fd, path = tempfile.mkstemp(dir=d, prefix='check-items-source-skips-', suffix='.json')
with os.fdopen(fd, 'w') as f:
    json.dump(sys.argv[1:], f)   # placeholder — SKILL replaces with real list
os.chmod(path, 0o600)
print(path)
")
# ^^^ In practice, Claude writes the actual skips list by passing them as
# the JSON-serialised content written to $_skips_file after the primary-flip loop.

SCOPE_PATH="$scope_path" BUCKETS_PATH="$buckets_path" MERGED_PATH="$merged_path" SKIPS_FILE="$_skips_file" python3 << 'PYEOF'
import sys, os, glob, json, re
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
from open_item_dedup import cascade_group_members, parse_cascade_skipped_total

scope_path = os.environ["SCOPE_PATH"]
buckets_path = os.environ["BUCKETS_PATH"]
merged_path = os.environ["MERGED_PATH"]
skips_file = os.environ.get("SKIPS_FILE", "")

_config_path = os.path.expanduser("~/.claude/obsidian-brain-config.json")
try:
    config = json.load(open(_config_path))
    vault_path = config.get("vault_path")
    if not vault_path:
        raise ValueError("vault_path missing from config")
except (OSError, json.JSONDecodeError, ValueError) as exc:
    print(f"ERROR: obsidian-brain config not loadable ({exc}); run /obsidian-setup", file=sys.stderr)
    sys.exit(1)
sessions_folder = config.get("sessions_folder", "claude-sessions")
sessions_dir = os.path.join(vault_path, sessions_folder)

buckets = json.load(open(buckets_path))
scope = json.load(open(scope_path))

# Load merged groups so we can resolve group_id → members with full paths.
# Members in merged.json store basename only; resolve to full path via sessions_dir.
try:
    merged_data = json.load(open(merged_path))
    groups_by_id = {}
    for _gs in merged_data.get("merged_by_proj", {}).values():
        for _g in _gs:
            groups_by_id[_g.get("group_id")] = _g
except (OSError, json.JSONDecodeError) as exc:
    print(
        f"[check-items] FATAL: cannot load merged.json ({exc}) — skipping cascade to avoid corrupt cache",
        file=sys.stderr,
    )
    sys.exit(1)

# Load source_skips: set of (full_path, line_number) pairs already primary-flipped.
source_skips = set()
if skips_file and os.path.exists(skips_file):
    try:
        raw_skips = json.load(open(skips_file))
        for entry in raw_skips or []:
            if isinstance(entry, list) and len(entry) == 2:
                source_skips.add((str(entry[0]), int(entry[1])))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"[check-items] WARNING: source_skips load failed ({exc}); cascade summary may be inaccurate", file=sys.stderr)
        source_skips = set()

# Build groups_to_cascade: DONE items from buckets["review"] that have members
# in merged.json. Resolve member basenames to full paths.
groups_to_cascade = []
for b in buckets["review"]:
    if b.get("classification") != "DONE":
        continue
    gid = b.get("group_id")
    merged_group = groups_by_id.get(gid) if gid else None
    if not merged_group:
        continue
    raw_members = merged_group.get("members", []) or []
    if not raw_members:
        continue
    resolved_members = []
    for m in raw_members:
        basename = m.get("file", "")
        line_num = m.get("line")
        if not basename or line_num is None:
            continue
        full_path = os.path.join(sessions_dir, basename)
        resolved_members.append({"file": full_path, "line": line_num, "text": m.get("text", "")})
    if resolved_members:
        groups_to_cascade.append({"members": resolved_members})

summary = cascade_group_members(groups_to_cascade, source_skips)
print(f"[cascade] {summary}")

# Extract count from summary for downstream use.
m = re.search(r"Cascaded (\d+)", summary)
cascade_total = int(m.group(1)) if m else 0
print(f"cascaded_total={cascade_total}")

# #320 F1/F2: `summary` can carry Skipped/WRITE FAILED lines beyond the bare
# count above -- surface them as distinct fields instead of letting them
# disappear once cascade_total is extracted, and treat a lost write as a
# hard failure rather than a silent success (0 exit code, "nothing to do").
skipped_lines = [
    ln for ln in summary.splitlines()
    if ln.startswith("Skipped ") or ln.startswith("WRITE FAILED")
]
# #320 R1 (Gemini): the doc below (Output format section) promises
# cascade_skipped_total sums every Skipped AND WRITE FAILED line -- the
# inline parsing here previously only summed Skipped lines, silently
# diverging from that doc. parse_cascade_skipped_total() (hooks/
# open_item_dedup.py) is the single source of truth for both this script
# and its unit tests, so the two can no longer drift apart.
cascade_skipped_total = parse_cascade_skipped_total(summary)
for ln in skipped_lines:
    print(f"[cascade-skip] {ln}")
print(f"cascade_skipped_total={cascade_skipped_total}")

if "WRITE FAILED" in summary:
    print(
        "[cascade] FATAL: a verified checkoff flip failed to save to disk "
        "-- do not report this run as fully successful",
        file=sys.stderr,
    )
    sys.exit(1)
PYEOF

# #320 F2: if the block above exited non-zero (WRITE FAILED), STOP here --
# do not report the run as fully successful. Tell the user which file(s)
# lost a verified flip (see the printed [cascade-skip] WRITE FAILED line
# and stderr) and that they should re-run /check-items to retry the
# cascade once the underlying disk/permission issue is resolved. The
# confirmed checkoffs from steps 1-4 above are still on disk; only the
# cascade to sibling copies failed to save.

# Clean up source-skips tempfile.
rm -f "$_skips_file"
```

**Note on primary-flip loop tracking:** After each successful Edit in steps 1–4, append the flipped item's full file path and line number as `[path, line]` to a Python list, then write that list as JSON to `$_skips_file` before running the cascade block. This prevents the cascade from double-flipping lines the SKILL already handled. `batch_cascade_checkoff` is retained for ad-hoc text-search use outside this SKILL flow.

**Reading `cascade_total` and `cascade_skipped_total`:** carry both forward to Step 9's `write_check_items_dashboard()` call (`cascaded=cascade_total`, `skipped=cascade_skipped_total`) and to the terminal Output format's `Cascaded:` and `Skipped:` lines. If the python block above exited non-zero, its `WRITE FAILED` line is the reason — report it to the user verbatim rather than proceeding as if the cascade fully succeeded.

## Step 9 — Write dashboard report (Stage 8) — ALWAYS

(Implemented in Task 22.) Call `write_check_items_dashboard()` with the scope, classifications, applied count, cascade count, semantic-merge mode, and classifier mode, plus `skipped=cascade_skipped_total` (#320 F1 — the count of cascade candidates that were refused or lost, so the dashboard doesn't collapse to only the auto-checked count). Path: `<vault>/<check_items_folder>/check-items-<scope>-<YYYY-MM-DD>.md` (folder configurable, default `claude-check-items`).

## Step 10 — Persist cache updates

```bash
SCOPE_PATH="$scope_path" CLASSIFICATIONS_PATH="$classifications_path" PARTITION_PATH="$part_path" python3 << 'PYEOF'
import sys, os, glob, json, time
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
from check_items_cache import locked_cache, update_cache, canonical_hash

scope_path = os.environ["SCOPE_PATH"]
classifications_path = os.environ["CLASSIFICATIONS_PATH"]
partition_path = os.environ["PARTITION_PATH"]
scope = json.load(open(scope_path))
data = json.load(open(classifications_path))
part = json.load(open(partition_path))

# Re-derive project list for cache update; HEAD shas are reused from Step 3 (#305).
all_groups = part["flat_groups"]

# Build a lookup from merged.json so we can attach members + mtime to each
# fresh classification. members are required by partition() for mtime
# invalidation; empty members causes every group to look mtime_changed next run.
merged_path_for_step10 = os.path.join(os.path.dirname(scope_path), "merged.json")
try:
    merged_data = json.load(open(merged_path_for_step10))
    all_merged = merged_data
except (OSError, json.JSONDecodeError) as exc:
    print(
        f"[check-items] FATAL: cannot load merged.json ({exc}) — skipping cache update to avoid corrupt cache",
        file=sys.stderr,
    )
    sys.exit(1)

groups_by_id = {}
for _proj, _gs in all_merged.get("merged_by_proj", {}).items():
    for _g in _gs:
        groups_by_id[_g.get("group_id")] = _g

fresh_classifications = []
for c in data["classifications"]:
    gid = c.get("group_id")
    group = groups_by_id.get(gid, {})
    fresh_classifications.append({
        "canonical_hash": group.get("canonical_hash") or canonical_hash(c.get("canonical_text", "")),
        "canonical_text": c.get("canonical_text", ""),
        "members": group.get("members", []),  # file/line/mtime preserved for mtime invalidation
        "classification": c.get("classification"),
        "confidence": c.get("confidence"),
        "evidence_citation": c.get("evidence_citation"),
        # #297: a missing classifier_source must not be laundered into a
        # trusted value here — update_cache's allowlist only persists
        # agent/prefilter/cache, so an absent source now correctly falls
        # through to "" and gets refused rather than silently cached as if
        # it were agent-derived.
        "classifier_source": c.get("classifier_source", ""),
        # #302: this stamp is authoritative only for freshly-derived
        # verdicts. For classifier_source == "cache" replays (Step 6),
        # _freeze_classification (called from update_cache) ignores this
        # value and inherits the prior on-disk entry's classified_ts
        # instead — but only when such an entry still exists and carries
        # its own classified_ts. If not (see the "no prior on-disk" warning
        # branch inside _freeze_classification), this stamp IS used as-is
        # and the TTL restarts. In the normal case a replayed verdict's TTL
        # keeps measuring from its last real verification, not its last read.
        "classified_ts": int(time.time()),
        "_group_project": group.get("project", "unknown"),  # carried for fresh_by_proj attribution
    })

# Group by project for per-project update_cache calls.
groups_by_proj = {}
for g in all_groups:
    groups_by_proj.setdefault(g.get("project", "unknown"), []).append(g)
fresh_by_proj = {}
for fc in fresh_classifications:
    # Derive project from the looked-up group (groups_by_id[gid]["project"]), not hash matching,
    # so same canonical text in multiple projects is attributed correctly.
    proj = fc.pop("_group_project", "unknown")
    fresh_by_proj.setdefault(proj, []).append(fc)

# #305: reuse the HEAD captured at Step 3 rather than re-deriving it here. A
# commit landing between Step 3 and Step 10 would otherwise stamp a mid-run
# HEAD onto verdicts that were actually derived against the OLD head —
# laundering them as verified-current when they were never checked against
# this newer commit.
heads = part.get("heads", {})
# #306: locked_cache() serializes the whole load-mutate-save cycle behind a
# lock, so a concurrent run on a different project (same shared
# cross-project cache file) cannot overwrite this run's update with a stale
# snapshot. It loads, yields the cache to mutate below, and saves + releases
# once this block exits.
#
# #323 F3: two paths previously skipped the save entirely and left the run
# looking clean anyway — a body exception, or save_cache() itself raising
# (a full or read-only disk) — with nothing telling the driving agent to
# notice. Both are now caught here and reported explicitly rather than
# left as a bare traceback. #323 F6: `update_cache()` mutates `cache` IN
# PLACE and its return value is deliberately left unassigned below —
# locked_cache() saves its OWN `cache` binding, so reassigning that name to
# update_cache's return value here would be invisible to it the day
# update_cache() stops returning the same object it was given (see
# locked_cache()'s docstring).
try:
    with locked_cache() as cache:
        for proj, proj_groups in groups_by_proj.items():
            head = heads.get(proj)
            if not head:
                print(f"[check-items] no head captured at Step 3 for {proj}; skipping cache update",
                      file=sys.stderr)
                continue
            update_cache(
                cache=cache,
                project=proj,
                all_groups=proj_groups,
                fresh_classifications=fresh_by_proj.get(proj, []),
                head_sha=head,
            )
except Exception as exc:
    # stdout, not stderr: skills/standup/SKILL.md documents stderr as
    # ignorable, and this line must actually be noticed.
    print(f"cache NOT updated: {exc}")
    sys.exit(1)

print("cache updated")
PYEOF
```

**#323 F3 — if the block above printed `cache NOT updated: <reason>` and exited non-zero:** STOP here — do not summarise this run as clean, and do not report `Cached: N reused, M fresh` (from Step 7) as if it were the final state. Report the `cache NOT updated: ...` line to the user verbatim. This run's classifications are lost (fail-safe: every group re-derives from scratch next run, at the cost of re-classification tokens, never incorrect output) — but any checkoffs already applied in Step 8 are on disk and unaffected.

## Output format

```
✓ /check-items obsidian-brain (14d)
  Raw: 225  Groups: 40  Merged: 24
  Mode: semantic+classifier  Cached: 8 reused, 16 fresh
  Result: 3 DONE (auto-checked), 2 NEEDS-ACTION (commands below), 4 REVIEW (needs a look), 19 ACTIVE (silent)
  Report: ~/Obsidian/claude-check-items/check-items-obsidian-brain-2026-05-11.md
  Cascaded: 2 sibling notes
  Skipped: 3 sibling(s) refused or lost — see report for basename:line detail
  Cache: updated
```

`Skipped:` (#320 F1) is populated from `cascade_skipped_total` (Step 8) — the count of cascade candidates that were refused (drift, unverifiable stored text, checkbox already gone) or lost (a failed write), summed across every `Skipped ...`/`WRITE FAILED` line in the cascade summary. Omit the line entirely when `cascade_skipped_total` is 0 — don't print `Skipped: 0`.

`Cache:` (#323 F3) reflects Step 10's outcome and is always printed, never omitted: `updated` on success, or `NOT updated — <reason>; verdicts will be re-derived next run` when Step 10 printed `cache NOT updated: <reason>` and exited non-zero.

## Notes

- All sub-agent prompts live in `hooks/check_items_cli.py` (the semantic-merge and classifier prompt constants). Do NOT inline those prompts in this SKILL.md.
- The cache file is at `~/.claude/obsidian-brain/check-items-classifications.json` (0o600). Safe to delete for a full reset.
- `/recall` no longer surfaces checkoff candidates. If you used to invoke `/recall → "skip"`, just run `/check-items` directly.
