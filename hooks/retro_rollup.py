"""Retro lessons rollup CLI (#471): group retro Process Improvements items into
recurring lessons, render lesson notes and report, and trace accepted lessons
back into the source retros.

Commands: update, rebuild, refresh-tracking, report, backfill, match. Exit 0 on
success, 1 on a runtime or partial failure (unjudged anchors, gh errors, the
lock held, a write error) with the output still printed, 2 on a usage or
config error.

``match`` (#473) looks for an issue, memory entry or rule that already covers a
lesson that has no tracking of its own. It only reads GitHub, and it never
writes a trace: a match reaches the retro notes only through ``backfill --apply
--accept FILE``.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

import lessons_corpus
import lessons_group
import lessons_match
import lessons_notes
import lessons_verify
import memory_sources
import retro_trace

STATE_NAME = "lessons-state.json"
CACHE_NAME = "lessons-verdicts.db"
LOCK_NAME = "lessons.lock"
LESSONS_SUBFOLDER = "lessons"
RETIRED_NAME = "retired.json"
GH_TIMEOUT_SECONDS = 10
GH_MAX_WORKERS = 8
MAX_CONCURRENCY = 16
_PRIORITY = re.compile(r"^P[0-9]")
_REF = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REMOTE = re.compile(
    r"^(?:https://(?:[^@/\s]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_CREATED = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)$")
GH_CREATE_TIMEOUT_SECONDS = 60
RECENT_LIMIT = 1000  # my issues listed in one `gh issue list` (gh paginates); a full page proves nothing
GIT_TIMEOUT_SECONDS = 10
MATCH_LIST_TIMEOUT_SECONDS = 60
MATCH_ISSUE_FIELDS = "number,title,body,state,stateReason,milestone,labels,createdAt,closedAt"


class UsageError(Exception):
    pass


class LockHeld(Exception):
    pass


class Refused(Exception):
    """The run would drop every lesson because no retro item was found (exit 1)."""


# --- context ---------------------------------------------------------------

def _bounded_int(name: str, value, low: int, high: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        want = f"an integer from {low} to {high}" if high is not None else f"an integer of at least {low}"
        raise UsageError(f"{name} must be {want}, got {value!r}")
    return value


class Ctx:
    def __init__(self, args):
        import obsidian_utils
        config = obsidian_utils.load_config()
        self.vault = args.vault or config.get("vault_path") or ""
        self.insights = args.insights_folder or config.get("insights_folder", "claude-insights")
        if not self.vault or not Path(self.vault).is_dir():
            raise UsageError(f"vault not found: {self.vault!r}")
        insights = Path(self.vault, self.insights).resolve()
        if not insights.is_relative_to(Path(self.vault).resolve()):
            raise UsageError(f"insights folder escapes the vault: {self.insights!r}")
        if not insights.is_dir():
            raise UsageError(f"insights folder not found: {self.insights!r}")
        self.lessons_folder = f"{self.insights}/{LESSONS_SUBFOLDER}"
        self.state_dir = Path(args.state_dir or Path.home() / ".claude" / "obsidian-brain")
        self.recent_days = _bounded_int("lessons_recent_days", config.get("lessons_recent_days", 90), 1)
        flag = getattr(args, "concurrency", None)
        self.concurrency = (_bounded_int("concurrency", flag, 1, MAX_CONCURRENCY) if flag is not None else
                            _bounded_int("lessons_concurrency", config.get("lessons_concurrency", 8),
                                         1, MAX_CONCURRENCY))
        repos = config.get("project_repos")
        self.project_repos = repos if isinstance(repos, dict) else {}
        self.today = date.today().isoformat()

    @property
    def state_path(self) -> Path:
        return self.state_dir / STATE_NAME

    @property
    def lessons_dir(self) -> Path:
        return Path(self.vault) / self.lessons_folder


def _ensure_state_dir(ctx: Ctx) -> None:
    ctx.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)


class _Lock:
    def __init__(self, ctx: Ctx):
        _ensure_state_dir(ctx)
        self.fd = os.open(ctx.state_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            raise LockHeld("another rollup run holds the lock")

    def release(self):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


# --- state -----------------------------------------------------------------

def _empty_state() -> dict:
    return {"version": 1, "generated": None, "lessons": {}, "tracking": {}, "notes": {}, "totals": {},
            "filed": {}}


def load_state(ctx: Ctx) -> dict:
    path = ctx.state_path
    if not path.exists():
        return _empty_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("lessons"), dict):
            raise ValueError("not a state object")
    except (OSError, UnicodeError, ValueError):
        aside = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.rename(aside)
            print(f"retro_rollup: state file unreadable; moved to {aside.name}, rebuilding", file=sys.stderr)
        except OSError:
            pass
        return _empty_state()
    base = _empty_state()
    base.update(state)
    base["filed"] = _clean_filed(base.get("filed"))
    return base


def _marker(lid: str) -> str:
    return f"<!-- retro-lesson:{lid} -->"


_MARKER_RE = re.compile(r"<!-- retro-lesson:[A-Za-z0-9_-]{1,64} -->")


def _reconcile_marker(pending: dict, lid: str) -> str:
    """The marker to look for: the record's own when it has the exact marker shape, else this id's."""
    marker = pending.get("marker")
    return marker if isinstance(marker, str) and _MARKER_RE.fullmatch(marker) else _marker(lid)


def _is_pending(value) -> bool:
    return (isinstance(value, dict) and value.get("pending") is True
            and _valid_repo(value.get("repo")))


def _clean_filed(filed) -> dict:
    """Keep the two valid forms: "owner/repo#N", or a pending record; drop anything else."""
    if not isinstance(filed, dict):
        return {}
    return {lid: v for lid, v in filed.items()
            if (isinstance(v, str) and _REF.match(v)) or _is_pending(v)}


def save_state(ctx: Ctx, state: dict) -> None:
    _ensure_state_dir(ctx)
    fd, tmp = tempfile.mkstemp(dir=ctx.state_dir, prefix=".lessons-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, ctx.state_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_retired(ctx: Ctx) -> dict:
    try:
        data = json.loads((ctx.lessons_dir / RETIRED_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeError, ValueError):
        return {}


# --- grouping ----------------------------------------------------------------

def _tracking_key(tracking: dict | None) -> str | None:
    return f"{tracking['kind']}:{tracking['ref']}" if tracking else None


def _load(ctx: Ctx):
    items, skipped = lessons_corpus.load_items(ctx.vault, ctx.insights)
    by_key = {}
    for item in items:
        by_key.setdefault(item["key"], item)  # identical text twice in one note: keep the first
    return items, by_key, skipped


def _filing_sources(lid: str, lesson: dict, retired: dict) -> list[str]:
    """Ids a lesson's filing may sit under: its own id, its split parent, then merged-in ids."""
    olds = [lid]
    if lesson.get("split_from"):
        olds.append(lesson["split_from"])
    olds += sorted(o for o, r in retired.items() if isinstance(r, dict) and r.get("merged_into") == lid)
    return olds


def _inherited_filing(state: dict, lid: str, lesson: dict, retired: dict):
    """The issue ref a lesson was filed under (see _filing_sources)."""
    for old_id in _filing_sources(lid, lesson, retired):
        ref = state["filed"].get(old_id)
        if isinstance(ref, str) and _REF.match(ref):
            return ref, old_id
    return None, None


def _inherited_pending(state: dict, lid: str, lesson: dict, retired: dict):
    """The first pending filing record under an id this lesson came from, else None."""
    for old_id in _filing_sources(lid, lesson, retired):
        rec = state["filed"].get(old_id)
        if _is_pending(rec):
            return rec
    return None


def regroup(ctx: Ctx, state: dict, *, judge: bool, note: str | None = None, cache=None) -> dict:
    """Judge (optionally), re-form lessons, write changed notes; return a summary."""
    items, by_key, skipped = _load(ctx)
    if not by_key and state["lessons"]:
        # Retro notes missing mid-sync or all unreadable: keep notes and state as they are.
        raise Refused(f"no retro items found in {ctx.insights!r} ({len(skipped)} unreadable); "
                      f"refusing to drop {len(state['lessons'])} lessons")
    unreadable = bool(skipped)  # an unreadable note hides items: never prune on partial data
    texts = {k: v["text"] for k, v in by_key.items()}
    note_of = {k: v["note"] for k, v in by_key.items()}
    _ensure_state_dir(ctx)
    own_cache = cache is None
    cache = cache or lessons_verify.VerdictCache(ctx.state_dir / CACHE_NAME)
    try:
        verify = {"judged": 0, "failed": 0, "skipped": 0}
        if judge:
            only = None
            if note is not None:
                stem = Path(note).stem
                only = {k for k, v in by_key.items() if v["note"] == stem}
            cands = lessons_corpus.top_candidates(list(by_key.values()), only=only)

            def progress(done, total, failed):
                print(f"judged {done}/{total} failed={failed}", file=sys.stderr, flush=True)

            verify = lessons_verify.verify_anchors(texts, cands, cache, concurrency=ctx.concurrency,
                                                   progress=progress)
        elif not unreadable:
            cache.prune(set(by_key))
        live = set(by_key)
        edges = {e for e in cache.edges() if e[0] in live and e[1] in live}
        unjudged = sum(1 for k in by_key if not cache.judged(k))
    finally:
        if own_cache:
            cache.close()

    if unreadable and state["lessons"]:
        # Regrouping hidden items would split or drop lessons: leave notes, state and retired.json alone.
        raise Refused(f"{len(skipped)} unreadable retro notes (first: {skipped[0]['path']}); "
                      f"lessons were left as they were")

    lessons = lessons_group.star_lessons(sorted(by_key), edges, note_of)
    old = {lid: rec["members"] for lid, rec in state["lessons"].items()}
    retired_before = _load_retired(ctx)
    lessons, retired = lessons_group.assign_ids(lessons, old, retired_before)
    new = [l for l in lessons if l["id"] not in state["lessons"]]
    names = lessons_verify.name_lessons(new, texts) if new else {}

    old_notes = dict(state.get("notes", {}))
    snoozes = {lid: lessons_notes.read_snooze(ctx.lessons_dir / fn) for lid, fn in old_notes.items()}
    out_lessons, out_notes, written = {}, {}, 0
    for lesson in lessons:
        lid = lesson["id"]
        lesson["name"] = state["lessons"].get(lid, {}).get("name") or names.get(lid) or lid
        # `since` is sticky for the id's own record; a split child with a new id inherits its parent's.
        prev_rec = state["lessons"].get(lid) or state["lessons"].get(lesson.get("split_from")) or {}
        tracking = lessons_group.lesson_tracking(lesson["members"], by_key, prev_rec.get("tracking"))
        if tracking is not None:
            tracking["source"] = "trace"
        if tracking is None and _is_pending(state["filed"].get(lid)):
            pass  # an issue may exist under this id: the pending record stays for backfill to reconcile
        elif tracking is None:
            # Filed, no trace landed yet: keep the issue, also across a merge or a split.
            filed_ref, source = _inherited_filing(state, lid, lesson, retired)
            if not filed_ref:
                pending = _inherited_pending(state, lid, lesson, retired)
                if pending is not None:
                    state["filed"][lid] = dict(pending)  # keeps its own marker; the old id keeps its record
            if filed_ref:
                since = (state["tracking"].get(f"issue:{filed_ref}") or {}).get("created")
                if not since:
                    old_tracking = (state["lessons"].get(source) or {}).get("tracking") or {}
                    since = old_tracking.get("since") if old_tracking.get("ref") == filed_ref else None
                tracking = {"kind": "issue", "ref": filed_ref, "since": since or ctx.today,
                            "source": "filed"}
                state["filed"][lid] = filed_ref  # a later file_issue accept on this id finds it
        # Last in line: the lesson's own match, keyed by its id (a recurring lesson keeps it).
        own_match = (state["lessons"].get(lid) or {}).get("match")
        if tracking is None and not _is_pending(state["filed"].get(lid)):
            tracking = _match_tracking(own_match)
        info = state["tracking"].get(_tracking_key(tracking)) if tracking else None
        c = lessons_group.classify(lesson, by_key, tracking, info, ctx.today, ctx.recent_days)
        snooze = snoozes.get(lid)
        if snooze is None and lesson.get("split_from"):
            snooze = snoozes.get(lesson["split_from"])
        if snooze is None:
            for old_id, rec in retired.items():
                if rec.get("merged_into") == lid and old_id not in retired_before and snoozes.get(old_id):
                    snooze = snoozes[old_id]
                    break
        filename = old_notes.get(lid) or lessons_notes.note_filename(lesson)
        content = lessons_notes.render_note(lesson, c, tracking, info, by_key, snooze)
        if lessons_notes.write_if_changed(ctx.vault, ctx.lessons_folder, filename, content):
            written += 1
        rec = {"name": lesson["name"], "centre": lesson["centre"], "members": lesson["members"],
               "tracking": tracking, "info": info, "classify": c}
        if lesson.get("split_from"):
            rec["split_from"] = lesson["split_from"]
        if own_match is not None:
            rec["match"] = own_match
        out_lessons[lid] = rec
        out_notes[lid] = filename

    removed = 0
    for lid, filename in old_notes.items():
        if lid not in out_notes:
            path = (ctx.lessons_dir / filename)
            if path.resolve().is_relative_to(ctx.lessons_dir.resolve()):
                try:
                    path.unlink()
                    removed += 1
                except FileNotFoundError:
                    pass
    if retired != retired_before:
        lessons_notes.write_if_changed(ctx.vault, ctx.lessons_folder, RETIRED_NAME,
                                       json.dumps(retired, indent=1, sort_keys=True) + "\n")

    in_lessons = len({k for l in out_lessons.values() for k in l["members"]} & set(by_key))
    state.update({
        "generated": ctx.today, "lessons": out_lessons, "notes": out_notes,
        "totals": {"items": len(by_key), "items_in_lessons": in_lessons,
                   "one_off": len(by_key) - in_lessons, "lessons": len(out_lessons),
                   "largest": max((len(l["members"]) for l in out_lessons.values()), default=0),
                   "unjudged": unjudged, "unreadable_notes": len(skipped)},
    })
    save_state(ctx, state)
    return {"lessons": len(out_lessons), "new": len(new), "items_in_lessons": in_lessons,
            "unjudged": unjudged, "failed": verify["failed"], "notes_written": written,
            "notes_removed": removed, "unreadable": len(skipped)}


def _report_unreadable(summary: dict) -> bool:
    """Say that retro notes were unreadable (only reached with no prior lessons); True when any were."""
    if not summary["unreadable"]:
        return False
    print(f"retro_rollup: {summary['unreadable']} unreadable retro notes; nothing was pruned",
          file=sys.stderr)
    return True


# --- tracking refresh -------------------------------------------------------------

def _run_gh(args: list[str], timeout: int = GH_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=timeout)


def _issue_info(ref: str) -> tuple[dict | None, str | None]:
    match = _REF.match(ref)
    if not match:
        return None, "not an issue ref"
    repo, number = match.groups()
    try:
        result = _run_gh(["issue", "view", number, "--repo", repo, "--json",
                          "state,stateReason,milestone,labels,createdAt,closedAt"])
    except subprocess.TimeoutExpired:
        return None, "gh timed out"
    except OSError as exc:
        return None, str(exc)[:200]
    if result.returncode:
        return None, f"gh exited with code {result.returncode}"
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None, "gh returned invalid JSON"
    if not isinstance(data, dict) or not isinstance(data.get("state"), str):
        return None, "gh JSON has no string state"
    milestone = data.get("milestone")
    labels = [l.get("name", "") for l in data.get("labels") or [] if isinstance(l, dict)]
    priority = next((l for l in labels if _PRIORITY.match(l)), None)
    return {"exists": True, "state": data["state"].upper(),
            "milestone": milestone.get("title") if isinstance(milestone, dict) else None,
            "priority": priority,
            "created": (data.get("createdAt") or "")[:10] or None,
            "closed": (data.get("closedAt") or "")[:10] or None}, None


def refresh_tracking(ctx: Ctx, state: dict) -> list[dict]:
    """Re-read every lesson's tracking; return errors (old fields kept for those)."""
    refs = {}
    for rec in state["lessons"].values():
        t = rec.get("tracking")
        if t:
            refs[_tracking_key(t)] = t
    errors = []
    issues = [k for k, t in refs.items() if t["kind"] == "issue"]
    if issues:
        with ThreadPoolExecutor(max_workers=min(GH_MAX_WORKERS, len(issues))) as pool:
            results = dict(zip(issues, pool.map(lambda k: _issue_info(refs[k]["ref"]), issues)))
        for key, (info, error) in results.items():
            if error:
                errors.append({"ref": refs[key]["ref"], "error": error})
            else:
                state["tracking"][key] = info
    memory_stems = None
    memory_failed = False
    for key, t in refs.items():
        if t["kind"] == "memory":
            if memory_stems is None:
                scan_errors: list = []
                memory_stems = {Path(p).stem for p in
                                memory_sources.memory_sources("claude-code", errors=scan_errors)}
                memory_failed = bool(scan_errors)
                errors.extend({"ref": e.get("path", "memory"), "error": e.get("error", "memory scan failed")}
                              for e in scan_errors)
            # A scan that hit errors cannot say a stem is gone or back: keep what was there.
            if memory_failed:
                state["tracking"].setdefault(key, {"exists": None})
            else:
                state["tracking"][key] = {"exists": t["ref"] in memory_stems}
        elif t["kind"] == "path":
            raw = t["ref"]
            if raw.startswith(("~", "/")):
                state["tracking"][key] = {"exists": Path(os.path.expanduser(raw)).exists()}
            else:
                state["tracking"][key] = {"exists": None}  # repo-relative: cannot check here
    return errors


# --- matching a lesson to tracking that already exists (#473) ---------------------

_MATCH_KIND = {"issue": "issue", "memory": "memory", "rule": "path"}


def _match_tracking(match) -> dict | None:
    """The tracking an accepted match gives a lesson; None for a rejection or a malformed record."""
    if not isinstance(match, dict) or match.get("none"):
        return None
    kind, ref, since = match.get("kind"), match.get("ref"), match.get("since")
    if kind not in ("issue", "memory", "path") or not isinstance(ref, str) or not ref:
        return None
    if kind == "issue" and not _REF.match(ref):
        return None
    if not (isinstance(since, str) and lessons_corpus._iso_date(since)):
        since = None
    return {"kind": kind, "ref": ref, "since": since, "source": "match"}


def _wants_match(state: dict, lid: str, rec: dict) -> bool:
    """A lesson is matched only when nothing else tracks it: no member trace, no filed or pending issue."""
    tracking = rec.get("tracking")
    if tracking and tracking.get("source") != "match":
        return False
    filed = state["filed"].get(lid)
    return not (isinstance(filed, str) or _is_pending(filed))


def _file_day(path) -> str | None:
    try:
        return date.fromtimestamp(Path(path).stat().st_mtime).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _rule_since(ref: str, line) -> str | None:
    """The day a rule was written: the last commit that touched its first 60 characters when its file
    sits in a git repo, else the file's mtime. A path that is not in a repo never raises."""
    path = Path(os.path.expanduser(ref))
    fallback = _file_day(path)
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        return fallback
    try:
        needle = path.read_text(encoding="utf-8").splitlines()[line - 1].strip()
        needle = (needle[2:] if needle.startswith("- ") else needle).strip()[:60]
        if not needle:
            return fallback
        git = ["git", "-C", str(path.parent)]
        top = subprocess.run([*git, "rev-parse", "--show-toplevel"], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS)
        if top.returncode:
            return fallback
        log = subprocess.run([*git, "log", "-1", "--format=%cs", "-S", needle, "--", str(path)],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, UnicodeError, IndexError, subprocess.TimeoutExpired):
        return fallback
    day = log.stdout.strip()
    return day if log.returncode == 0 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else fallback


def _list_repo_issues(repo: str):
    """Every issue in a repo, read-only. Returns (issues, None) or (None, error)."""
    try:
        result = _run_gh(["issue", "list", "--repo", repo, "--state", "all", "--limit", str(RECENT_LIMIT),
                          "--json", MATCH_ISSUE_FIELDS], timeout=MATCH_LIST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None, "gh timed out"
    except OSError as exc:
        return None, str(exc)[:200]
    if result.returncode:
        return None, f"gh exited with code {result.returncode}"
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None, "gh returned invalid JSON"
    if not isinstance(data, list) or not all(isinstance(i, dict) for i in data):
        return None, "gh JSON is not a list of issues"
    return data, None


def _project_repo(ctx: Ctx, project: str, cache: dict) -> str | None:
    """The repo a project's issues live in: project_repos first, then the origin remote."""
    if project not in cache:
        configured = ctx.project_repos.get(project)
        if configured is not None:
            cache[project] = configured if _valid_repo(configured) else None
        else:
            cache[project] = _origin_repo(project)
    return cache[project]


def _home_ref(home: Path, path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def _listed_issue_info(meta: dict) -> dict:
    """Tracking facts for a matched issue, as refresh-tracking would record them."""
    milestone = meta.get("milestone")
    labels = [l.get("name", "") for l in meta.get("labels") or [] if isinstance(l, dict)]
    state = meta.get("state")
    return {"exists": True, "state": state.upper() if isinstance(state, str) and state else "OPEN",
            "milestone": milestone.get("title") if isinstance(milestone, dict) else None,
            "priority": next((l for l in labels if isinstance(l, str) and _PRIORITY.match(l)), None),
            "created": (meta.get("createdAt") or "")[:10] or None,
            "closed": (meta.get("closedAt") or "")[:10] or None}


def match_lessons(ctx: Ctx, state: dict, *, recheck: bool = False) -> dict:
    """Give each untracked lesson the issue, memory entry or rule that already covers it.

    The single highest-cosine candidate across issues, memory and rules is accepted or rejected by one
    Haiku call (spike round 4, #473). A lesson is checked again only when its top candidate changed, or
    with ``recheck``. Anything that cannot be read or answered leaves the lesson unchecked (its old
    match, if any, stands) and is reported in ``errors``; it is never recorded as "no match".
    """
    summary = {"matched": 0, "rejected": 0, "no_proposal": 0, "unchanged": 0, "unchecked": 0, "errors": []}
    errors = summary["errors"]
    _, by_key, skipped = _load(ctx)
    if skipped:
        return summary  # unreadable notes hide items: regroup refuses the run, so write nothing here
    todo = []
    for lid, rec in sorted(state["lessons"].items()):
        if _wants_match(state, lid, rec):
            members = [by_key[k] for k in rec["members"] if k in by_key]
            if members:
                todo.append((lid, rec, members))
    if not todo:
        return summary

    memory_errors: list = []
    paths = memory_sources.memory_sources("claude-code", errors=memory_errors)
    memory = lessons_match.memory_docs([str(p) for p in paths], memory_errors)
    if memory_errors:
        # A partial memory scan could hide the covering entry: check nothing rather than record "no match".
        errors.extend({"ref": e.get("path", "memory"), "error": e.get("error", "memory scan failed")}
                      for e in memory_errors)
        summary["unchecked"] = len(todo)
        return summary

    import obsidian_utils
    home = Path.home()
    roots = [Path(r) for r in obsidian_utils.get_workspace_roots()] or [home / "dev" / "claude_workspace"]
    # Rankings use idf over the whole source: every project's repo and rules, not just the lessons being
    # checked now. A lesson is then limited to its own repos and its own projects' rules, so a rerun with
    # fewer lessons to check ranks the rest the same way.
    projects = sorted({item["project"] for item in by_key.values()})
    rule_errors: list = []
    rules, seen = [], set()
    try:
        for root in roots:
            for doc in lessons_match.rule_docs(home, projects, root, rule_errors):
                if (doc["ref"], doc["line"]) not in seen:
                    seen.add((doc["ref"], doc["line"]))
                    rules.append(doc)
    except OSError as exc:
        # The rules could not be listed: like a memory scan error, check nothing rather than record "no match".
        errors.append({"ref": "rules", "error": str(exc)[:200]})
        summary["unchecked"] = len(todo)
        return summary
    if rule_errors:
        # A rule file or directory that cannot be read could hide the covering rule: like a memory scan
        # error, check nothing rather than record "no match" or erase an earlier accept.
        errors.extend({"ref": e["path"], "error": e["error"]}
                      for e in {e["path"]: e for e in rule_errors}.values())
        summary["unchecked"] = len(todo)
        return summary
    project_rules = {p: {_home_ref(home, root / p / "CLAUDE.md") for root in roots} for p in projects}
    every_project_rule = set().union(*project_rules.values()) if project_rules else set()
    rule_at: dict[str, list[int]] = {}
    for i, doc in enumerate(rules):
        rule_at.setdefault(doc["ref"], []).append(i)
    global_rules = {i for ref, at in rule_at.items() if ref not in every_project_rule for i in at}

    repo_cache: dict = {}
    lesson_projects = {lid: sorted({m["project"] for m in members}) for lid, _, members in todo}
    # A configured project_repos value that is not a repo is a setup error, not "no repo": the project's
    # issues cannot be read, so its lessons stay unchecked. No value and no origin is simply no issue source.
    bad_repo = {p: v for p in projects
                if (v := ctx.project_repos.get(p)) is not None and not _valid_repo(v)}
    for p in sorted({p for lid, ps in lesson_projects.items() for p in ps} & set(bad_repo)):
        errors.append({"ref": p, "error": f"project_repos[{p!r}] is not an owner/name repo: "
                                          f"{str(bad_repo[p])[:80]!r}"})
    lesson_repos = {lid: sorted({r for p in ps if (r := _project_repo(ctx, p, repo_cache))})
                    for lid, ps in lesson_projects.items()}
    repos = sorted({r for p in projects if (r := _project_repo(ctx, p, repo_cache))})
    issue_docs, issue_meta, failed = [], {}, set()
    issue_at: dict[str, list[int]] = {}
    if repos:
        with ThreadPoolExecutor(max_workers=min(GH_MAX_WORKERS, len(repos))) as pool:
            listings = list(pool.map(_list_repo_issues, repos))
        for repo, (data, error) in zip(repos, listings):
            if error:
                failed.add(repo)
                if any(repo in rs for rs in lesson_repos.values()):  # only a repo a pending lesson needs
                    errors.append({"ref": repo, "error": error})
                continue
            if len(data) >= RECENT_LIMIT:
                print(f"retro_rollup: {repo}: the listing is full ({len(data)} issues); "
                      f"older issues are not candidates", file=sys.stderr)
            docs = lessons_match.issue_docs(repo, data)
            issue_at[repo] = list(range(len(issue_docs), len(issue_docs) + len(docs)))
            issue_docs.extend(docs)
            issue_meta.update({f"{repo}#{i['number']}": i for i in data
                               if type(i.get("number")) is int})
    issue_index = lessons_match.Index(issue_docs)
    memory_index = lessons_match.Index(memory)
    rule_index = lessons_match.Index(rules)
    issue_views, rule_views = {}, {}

    jobs, changed = [], False
    for lid, rec, members in todo:
        if failed & set(lesson_repos[lid]) or bad_repo.keys() & set(lesson_projects[lid]):
            summary["unchecked"] += 1
            continue
        repo_key = tuple(lesson_repos[lid])
        if repo_key not in issue_views:
            issue_views[repo_key] = issue_index.view({i for r in repo_key for i in issue_at[r]})
        applicable = frozenset(ref for p in lesson_projects[lid] for ref in project_rules[p])
        if applicable not in rule_views:
            rule_views[applicable] = rule_index.view(
                global_rules | {i for ref in applicable for i in rule_at.get(ref, ())})
        sources = {"issue": issue_views[repo_key], "memory": memory_index, "rule": rule_views[applicable]}
        texts = [m["text"] for m in members]
        best = lessons_match.best_candidate(lessons_match.lesson_text(rec["name"], texts), sources)
        doc = best[0] if best else None
        chash = (lessons_match.short_hash(f"{doc['source']}|{doc['ref']}|{doc['text']}")
                 if doc else None)
        prev = rec.get("match")
        if isinstance(prev, dict) and prev.get("candidate") == chash and not recheck:
            summary["unchanged"] += 1
            if (doc and doc["source"] != "issue" and prev.get("kind") == _MATCH_KIND[doc["source"]]
                    and prev.get("ref") == doc["ref"]):
                # The file was read just now: drop a stale "gone" left by an earlier refresh-tracking.
                key = f"{prev['kind']}:{doc['ref']}"
                if state["tracking"].get(key) != {"exists": True}:
                    state["tracking"][key] = {"exists": True}
                    changed = True
            continue
        if doc is None:
            rec["match"] = {"none": True, "candidate": None}
            summary["no_proposal"] += 1
            changed = True
            continue
        jobs.append((lid, rec, doc, chash, lessons_match.anchor_text(rec["name"], texts)))

    def ask(job):
        return lessons_match.gate(job[4], job[2], lambda prompt: lessons_verify.run_haiku(prompt))

    if jobs:
        with ThreadPoolExecutor(max_workers=min(ctx.concurrency, len(jobs))) as pool:
            verdicts = list(pool.map(ask, jobs))
        for (lid, rec, doc, chash, _), verdict in zip(jobs, verdicts):
            if verdict is None:
                summary["unchecked"] += 1
                errors.append({"ref": lid, "error": "Haiku gave no usable answer"})
            elif verdict:
                if doc["source"] == "issue":
                    since = doc.get("created")
                elif doc["source"] == "memory":
                    since = _file_day(doc["path"])
                else:
                    since = _rule_since(doc["ref"], doc.get("line"))
                rec["match"] = {"kind": _MATCH_KIND[doc["source"]], "ref": doc["ref"],
                                "since": since, "candidate": chash}
                if doc["source"] == "issue":
                    state["tracking"][f"issue:{doc['ref']}"] = _listed_issue_info(issue_meta[doc["ref"]])
                else:
                    # The file was read just now: drop a stale "gone" left by an earlier refresh-tracking.
                    state["tracking"][f"{rec['match']['kind']}:{doc['ref']}"] = {"exists": True}
                summary["matched"] += 1
                changed = True
            else:
                rec["match"] = {"none": True, "candidate": chash}
                summary["rejected"] += 1
                changed = True
    if changed:
        for _, rec, _ in todo:
            # Same result regroup reaches; set now so refresh-tracking sees a new match.
            rec["tracking"] = _match_tracking(rec.get("match"))
        save_state(ctx, state)
    return summary


# --- backfill ---------------------------------------------------------------

def _trace_for(tracking: dict, number: int) -> dict:
    kind = tracking["kind"]
    field = {"issue": "ref", "memory": "stem", "path": "path"}[kind]
    return {"item": number, "kind": kind, field: tracking["ref"]}


def _untraced_report(ctx: Ctx, state: dict, accepted: set[str]) -> dict:
    items, by_key, skipped = _load(ctx)
    lesson_of = {k: lid for lid, rec in state["lessons"].items() for k in rec["members"]}
    cache = lessons_verify.VerdictCache(ctx.state_dir / CACHE_NAME)
    try:
        rows = []
        for item in items:
            if item["checked"] or item["trace"] is not None:
                continue
            lid = lesson_of.get(item["key"])
            if lid is None:
                reason = "one-off" if cache.judged(item["key"]) else "unjudged"
            elif lid in accepted:
                reason = "accepted-not-written"
            elif not state["lessons"][lid].get("tracking"):
                reason = "lesson-untracked"
            elif state["lessons"][lid]["tracking"].get("source") == "match":
                reason = "lesson-matched"
            else:
                reason = "not-accepted"
            rows.append({"note": item["note"], "item": item["item"], "line": item["line"],
                         "lesson": lid, "reason": reason})
    finally:
        cache.close()
    rows += [{"note": Path(s["path"]).stem, "item": None, "line": None, "lesson": None,
              "reason": s["reason"]} for s in skipped]
    return {"untraced_open": len(rows), "items": rows}


def _read_accept(path: str) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise UsageError(f"cannot read accept file: {exc}")
    usage = ('accept file must be a JSON list of {"lesson_id", "tracking_ref"} or '
             '{"lesson_id", "file_issue": true, "repo"?, "title"?, "body"?, "milestone"?, "labels"?}')
    if not isinstance(data, list):
        raise UsageError(usage)
    for e in data:
        if not isinstance(e, dict) or not isinstance(e.get("lesson_id"), str):
            raise UsageError(usage)
        if "file_issue" in e:
            if e["file_issue"] is not True or "tracking_ref" in e:
                raise UsageError(usage)
            if "repo" in e and not _valid_repo(e["repo"]):
                raise UsageError(f"invalid repo in accept file: {e['repo']!r}")
            for field, low, high in (("title", 1, 256), ("body", 0, 20000), ("milestone", 1, 100)):
                if field in e:
                    value = e[field]
                    if (not isinstance(value, str) or not low <= len(value) <= high
                            or (field == "title" and (not value.strip() or "\n" in value or "\r" in value))):
                        raise UsageError(f"invalid {field} in accept file")
            if "labels" in e:
                labels = e["labels"]
                if (not isinstance(labels, list) or len(labels) > 10
                        or any(not isinstance(label, str) or not 1 <= len(label) <= 50
                               or any(c in label for c in ",\n\r") for label in labels)):
                    raise UsageError("invalid labels in accept file")
        elif (not isinstance(e.get("tracking_ref"), str)
              or any(field in e for field in ("repo", "title", "body", "milestone", "labels"))):
            raise UsageError(usage)
    return data


# --- filing an issue for an untracked lesson ----------------------------------------

def _valid_repo(value) -> bool:
    if not isinstance(value, str) or not _REPO.match(value):
        return False
    return not any(part in (".", "..") for part in value.split("/"))


def _repo_from_remote(url: str) -> str | None:
    """owner/repo from a github.com https or ssh remote URL; anything else is None."""
    match = _REMOTE.match(url.strip()) if isinstance(url, str) else None
    if not match:
        return None
    repo = f"{match.group(1)}/{match.group(2)}"
    return repo if _valid_repo(repo) else None


def _git_origin(path: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=GIT_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def _origin_repo(project: str) -> str | None:
    """The origin repo of <workspace root>/<project>, or None."""
    import obsidian_utils
    if not project or project in (".", "..") or "/" in project or "\\" in project:
        return None
    for root in obsidian_utils.get_workspace_roots():
        candidate = Path(root) / project
        if candidate.is_dir() and candidate.resolve().parent == Path(root).resolve():
            repo = _repo_from_remote(_git_origin(str(candidate)) or "")
            if repo:
                return repo
    return None


def _lesson_project(rec: dict, by_key: dict) -> str | None:
    """The project with the most member sessions (retros); ties go to the smaller name."""
    sessions: dict[str, set] = {}
    for key in rec["members"]:
        item = by_key.get(key)
        if item is not None:
            sessions.setdefault(item["project"], set()).add(item["note"])
    return min(sessions, key=lambda p: (-len(sessions[p]), p)) if sessions else None


def _resolve_repo(ctx: Ctx, rec: dict, by_key: dict, override: str | None,
                  cache: dict | None = None) -> tuple[str | None, str | None]:
    """(repo, None) or (None, reason): accept override, then project_repos, then the origin remote."""
    project = _lesson_project(rec, by_key)
    if override is not None:
        return (override, None) if _valid_repo(override) else (None, "invalid repo")
    configured = ctx.project_repos.get(project) if project else None
    if configured is not None:
        return (configured, None) if _valid_repo(configured) else (None, f"invalid repo for project {project}")
    if cache is not None and project in cache:
        repo = cache[project]
    else:
        repo = _origin_repo(project) if project else None
        if cache is not None:
            cache[project] = repo
    return (repo, None) if repo else (None, f"no repo for project {project}")


def _issue_body(lid: str, rec: dict, by_key: dict) -> str:
    """Counts, dates, project names and note#item refs only; never retro item text."""
    members = [by_key[k] for k in rec["members"] if k in by_key]
    dates = sorted(m["date"] for m in members)
    lines = [_marker(lid), f"Recurring lesson `{lid}` from the retro lessons rollup (#471).", "",
             f"- Sessions: {len({m['note'] for m in members})}",
             f"- Projects: {', '.join(sorted({m['project'] for m in members}))}",
             f"- First seen: {dates[0] if dates else 'unknown'}",
             f"- Last seen: {dates[-1] if dates else 'unknown'}",
             "- Retro items (note#item):"]
    lines += [f"  - {m['note']}#{m['item']}" for m in sorted(members, key=lambda m: (m["date"], m["note"], m["item"]))]
    return "\n".join(lines) + "\n"


def _filing_title(lid: str, rec: dict, entry: dict) -> str:
    import obsidian_utils
    if "title" in entry:
        return " ".join(obsidian_utils.scrub_secrets(entry["title"]).split())
    return " ".join(str(rec["name"]).split())[:200] or lid


def _filing_body(lid: str, rec: dict, by_key: dict, entry: dict) -> str:
    import obsidian_utils
    facts = _issue_body(lid, rec, by_key)
    if "body" not in entry:
        return facts
    custom = "".join(line for line in obsidian_utils.scrub_secrets(entry["body"]).splitlines(keepends=True)
                       if not line.strip().startswith("<!-- retro-lesson:"))
    return _marker(lid) + "\n\n" + custom + "\n\n---\n" + facts.split("\n", 1)[1]


def _create_issue(ctx: Ctx, repo: str, title: str, body: str,
                  milestone=None, labels=()) -> tuple[str | None, str | None]:
    """File one issue; return ("owner/repo#N", None) or (None, error)."""
    _ensure_state_dir(ctx)
    fd, tmp = tempfile.mkstemp(dir=ctx.state_dir, prefix=".gh-body-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(tmp, 0o600)
        try:
            args = ["issue", "create", "--repo", repo, "--title", title, "--body-file", tmp]
            if milestone is not None:
                args += ["--milestone", milestone]
            for label in labels:
                args += ["--label", label]
            result = _run_gh(args, timeout=GH_CREATE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return None, "gh timed out; the issue may exist, check before rerunning"
        except OSError as exc:
            return None, str(exc)[:200]
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if result.returncode:
        return None, f"gh exited with code {result.returncode}"
    last = (result.stdout or "").rstrip().splitlines()[-1:] or [""]
    match = _CREATED.match(last[0].strip())
    if not match or f"{match.group(1)}/{match.group(2)}".casefold() != repo.casefold():
        return None, "gh did not print the new issue URL"
    return f"{repo}#{match.group(3)}", None


def _list_my_issues(repo: str):
    """One plain listing of my issues in a repo (no search index, so a just-created issue is there at
    once). Returns (issues, None) or (None, error)."""
    try:
        result = _run_gh(["issue", "list", "--repo", repo, "--state", "all", "--author", "@me",
                          "--json", "number,body,state,createdAt,closedAt,milestone",
                          "--limit", str(RECENT_LIMIT)])
    except subprocess.TimeoutExpired:
        return None, "gh timed out"
    except OSError as exc:
        return None, str(exc)[:200]
    if result.returncode:
        return None, f"gh exited with code {result.returncode}"
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None, "gh returned invalid JSON"
    if not isinstance(data, list) or not all(isinstance(i, dict) for i in data):
        return None, "gh JSON is not a list of issues"
    return data, None


def _match_marker(repo: str, data: list, marker: str):
    """Look for an earlier issue carrying the marker in a listing from _list_my_issues.

    Returns (ref, meta, error): ("owner/repo#N", issue metadata, None) when found, (None, None, None)
    when there is none, or (None, None, error) when the listing cannot say: a full page with no match
    cannot rule out an earlier filing.
    """
    found = sorted((i for i in data
                    if isinstance(i.get("number"), int) and not isinstance(i["number"], bool)
                    and i["number"] > 0 and isinstance(i.get("body"), str)
                    and marker in (line.strip() for line in i["body"].splitlines())),
                   key=lambda i: i["number"])
    if found:
        return f"{repo}#{found[0]['number']}", found[0], None
    if len(data) >= RECENT_LIMIT:
        return None, None, (f"more than {RECENT_LIMIT} of my issues in {repo}; "
                            f"cannot rule out an earlier filing")
    return None, None, None


def _adopted_info(issue: dict, today: str) -> dict:
    """Tracking facts for an adopted issue, as refresh-tracking would record them."""
    def day(value):
        return value[:10] if isinstance(value, str) and len(value) >= 10 else None

    milestone = issue.get("milestone")
    title = milestone.get("title") if isinstance(milestone, dict) else None
    state = issue.get("state")
    return {"exists": True, "state": state if isinstance(state, str) and state else "OPEN",
            "milestone": title if isinstance(title, str) and title else None, "priority": None,
            "created": day(issue.get("createdAt")) or today, "closed": day(issue.get("closedAt"))}


def _planned_filings(ctx: Ctx, state: dict, by_key: dict,
                     entries: list[dict] | None = None) -> list[dict]:
    plans, cache = [], {}
    selected = ({e["lesson_id"]: e for e in entries if e.get("file_issue")}
                if entries is not None else {lid: {} for lid in state["lessons"]})
    for lid, entry in sorted(selected.items()):
        rec = state["lessons"].get(lid)
        if rec is None:
            continue
        filed = state["filed"].get(lid)
        if rec.get("tracking") or isinstance(filed, str):
            continue
        untraced = sum(1 for k in rec["members"] if k in by_key and by_key[k]["trace"] is None)
        if not untraced:
            continue
        if _is_pending(filed) and entries is None:
            repo, reason = filed["repo"], "pending reconcile"
        else:
            repo, reason = _resolve_repo(ctx, rec, by_key, entry.get("repo"), cache)
            if _is_pending(filed) and reason is None:
                reason = "pending reconcile"
        plan = {"lesson": lid, "repo": repo,
                "title": _filing_title(lid, rec, entry) if entries is not None else rec["name"],
                "members": untraced, "milestone": entry.get("milestone"), "labels": entry.get("labels", [])}
        if entries is not None:
            plan["body_chars"] = len(entry.get("body", ""))
        if reason:
            plan["reason"] = reason
        plans.append(plan)
    return plans


def _pending_filings(state: dict) -> dict:
    return {lid: v for lid, v in sorted(state["filed"].items()) if _is_pending(v)}


def _reconcile_pending(ctx: Ctx, state: dict) -> tuple[list[dict], bool, bool]:
    """Settle every pending filing record with one issue listing per repo.

    Found marker: adopt the issue. Listing fine and no match: nothing was created, drop the record.
    Listing failed: keep the record. Returns (reconciled, ok, changed).
    """
    pending = _pending_filings(state)
    by_repo: dict[str, list[str]] = {}
    for lid, rec in pending.items():
        by_repo.setdefault(rec["repo"], []).append(lid)
    reconciled, ok, changed = [], True, False
    for repo, lids in by_repo.items():
        data, list_error = _list_my_issues(repo)
        for lid in lids:
            rec = pending[lid]
            ref, meta, error = (None, None, list_error)
            if list_error is None:
                ref, meta, error = _match_marker(repo, data, _reconcile_marker(rec, lid))
            if error:
                print(f"retro_rollup: {lid}: cannot check {repo} for an earlier filing: {error}",
                      file=sys.stderr)
                reconciled.append({"lesson": lid, "repo": repo, "issue": None, "outcome": "error"})
                ok = False
                continue
            changed = True
            if ref is None:
                del state["filed"][lid]
                reconciled.append({"lesson": lid, "repo": repo, "issue": None, "outcome": "none"})
                continue
            info = _adopted_info(meta, ctx.today)
            state["filed"][lid] = ref
            state["tracking"][f"issue:{ref}"] = info
            lesson = state["lessons"].get(lid)
            if lesson is not None and not lesson.get("tracking"):
                lesson["tracking"] = {"kind": "issue", "ref": ref, "since": info["created"]}
            reconciled.append({"lesson": lid, "repo": repo, "issue": int(ref.rsplit("#", 1)[1]),
                               "outcome": "adopted"})
    return reconciled, ok, changed


def backfill(ctx: Ctx, state: dict, accept: list[dict] | None,
             preview: list[dict] | None = None) -> tuple[dict, bool]:
    """Dry run when accept is None; preview optionally selects entries without applying them."""
    items, by_key, _ = _load(ctx)
    plan, skipped_entries, filed, reconciled = [], [], [], []
    accepted = {}
    ok = True
    can_file = True
    retired = {}
    preview_filings = _planned_filings(ctx, state, by_key, preview) if preview is not None else []
    preview_by_id = {row["lesson"]: row for row in preview_filings}
    if accept is not None:
        retired = _load_retired(ctx)
        reconciled, ok, changed = _reconcile_pending(ctx, state)
        if changed:
            try:
                save_state(ctx, state)
            except OSError as exc:
                print(f"retro_rollup: cannot save the reconcile result: {exc}", file=sys.stderr)
                ok = can_file = False
    for entry in (accept if accept is not None else preview) or []:
        lid = entry["lesson_id"]
        rec = state["lessons"].get(lid)
        if rec is None:
            skipped_entries.append({**entry, "reason": "unknown lesson"})
        elif entry.get("file_issue"):
            if preview is not None:
                row = preview_by_id.get(lid)
                reason = None
                if row is None:
                    reason = ("already tracked" if rec.get("tracking") or isinstance(state["filed"].get(lid), str)
                              else "nothing to trace")
                elif row["repo"] is None:
                    reason = row["reason"]
                    ok = False
                if reason:
                    skipped_entries.append({**entry, "reason": reason})
                continue
            ref = state["filed"].get(lid)
            if not (isinstance(ref, str) and _REF.match(ref)):
                ref = None
            if ref is None and not rec.get("tracking") and not _is_pending(state["filed"].get(lid)):
                # An issue filed under a merged-in or split-from id counts: do not file a second one.
                # An own pending record still here means its reconcile failed: keep it, never overwrite.
                ref, source = _inherited_filing(state, lid, rec, retired)
                if ref is not None:
                    state["filed"][lid] = ref
                    since = ((state["tracking"].get(f"issue:{ref}") or {}).get("created")
                             or ((state["lessons"].get(source) or {}).get("tracking") or {}).get("since")
                             or ctx.today)
                    rec["tracking"] = {"kind": "issue", "ref": ref, "since": since}
                    try:
                        save_state(ctx, state)
                    except OSError as exc:
                        print(f"retro_rollup: cannot save: {exc}", file=sys.stderr)
                        skipped_entries.append({**entry, "reason": f"issue {ref} exists but the state "
                                                f"could not be saved: {exc}; rerun to adopt it"})
                        ok = False
                        continue
            if ref is None:
                if rec.get("tracking"):
                    skipped_entries.append({**entry, "reason": "already tracked"})
                    continue
                if not any(k in by_key and by_key[k]["trace"] is None for k in rec["members"]):
                    skipped_entries.append({**entry, "reason": "nothing to trace"})
                    continue
                if not can_file or _inherited_pending(state, lid, rec, retired) is not None:
                    # A reconcile failed (or could not be saved): an earlier issue may exist.
                    skipped_entries.append({**entry, "reason": "cannot check for an earlier filing"})
                    ok = False
                    continue
                repo, reason = _resolve_repo(ctx, rec, by_key, entry.get("repo"))
                if repo is None:
                    skipped_entries.append({**entry, "reason": reason})
                    ok = False
                    continue
                title = _filing_title(lid, rec, entry)
                # Phase 1: record the intent, so a crash or a gh timeout can be reconciled later.
                before = state["filed"].get(lid)
                state["filed"][lid] = {"pending": True, "repo": repo, "marker": _marker(lid)}
                try:
                    save_state(ctx, state)
                except OSError as exc:
                    if before is None:
                        del state["filed"][lid]
                    else:
                        state["filed"][lid] = before
                    skipped_entries.append({**entry, "reason": f"cannot save the filing record: {exc}"})
                    ok = False
                    continue
                ref, error = _create_issue(ctx, repo, title, _filing_body(lid, rec, by_key, entry),
                                           milestone=entry.get("milestone"), labels=entry.get("labels", []))
                if ref is None:
                    # The pending record stays: the issue may exist (timeout, odd output).
                    skipped_entries.append({**entry, "reason": f"filing failed: {error}"})
                    ok = False
                    continue
                # Phase 2, in one save: the issue ref, and the tracking the new issue gives the lesson,
                # so a dry run, the report and the untraced list see it even if every trace write fails.
                state["filed"][lid] = ref
                # Known facts about the issue, as refresh-tracking would record them, so the sessions
                # from before it existed do not count as recurrences. A new issue is open and created today.
                info = {"exists": True, "state": "OPEN", "milestone": None, "priority": None,
                        "created": ctx.today, "closed": None}
                rec["tracking"] = {"kind": "issue", "ref": ref, "since": info["created"]}
                state["tracking"][f"issue:{ref}"] = info
                number = int(ref.rsplit("#", 1)[1])
                try:
                    save_state(ctx, state)
                except OSError as exc:
                    skipped_entries.append({**entry, "reason": f"issue {ref} exists but the state "
                                            f"could not be saved: {exc}; rerun to adopt it"})
                    ok = False
                    continue
                filed.append({"lesson": lid, "repo": repo, "issue": number})
            elif rec.get("tracking") and rec["tracking"]["ref"] != ref:
                skipped_entries.append({**entry, "reason": "already tracked"})
                continue
            accepted[lid] = {**rec, "tracking": {"kind": "issue", "ref": ref}}
        elif not rec.get("tracking") or rec["tracking"]["ref"] != entry["tracking_ref"]:
            skipped_entries.append({**entry, "reason": "tracking changed"})
        elif ((state["tracking"].get(_tracking_key(rec["tracking"])) or {}).get("exists") is False
              or (rec.get("classify") or {}).get("tracking_gone")):
            skipped_entries.append({**entry, "reason": "tracking gone"})
        else:
            accepted[lid] = rec
    lessons = accepted if accept is not None or preview is not None else {
        lid: rec for lid, rec in state["lessons"].items() if rec.get("tracking")}
    by_note: dict[str, list[tuple[str, dict]]] = {}
    for lid, rec in lessons.items():
        for key in rec["members"]:
            item = by_key.get(key)
            if item is None or item["trace"] is not None:
                continue
            by_note.setdefault(item["path"], []).append((key, rec["tracking"]))
            plan.append({"lesson": lid, "note": item["note"], "item": item["item"],
                         "trace": _trace_for(rec["tracking"], item["item"])})
    written = 0
    if accept is not None:
        for path, wanted in sorted(by_note.items()):
            try:
                with open(path, encoding="utf-8", newline="") as fh:
                    text = fh.read()
            except (OSError, UnicodeError) as exc:
                print(f"retro_rollup: cannot read {path}: {exc}", file=sys.stderr)
                ok = False
                continue
            stem = Path(path).stem
            current = {}
            for it in retro_trace.parse_items(text):
                current.setdefault(lessons_corpus.item_key(stem, it["text"]), it)  # first copy, like _load
            traces, expected = [], {}
            for key, tracking in wanted:
                it = current.get(key)
                if it is not None and it["trace"] is None:
                    traces.append(_trace_for(tracking, it["item"]))
                    expected[it["item"]] = key
            if not traces:
                continue

            def still_the_same(item, stem=stem, expected=expected):
                # Re-checked under the note lock: an edit since the read above must not shift a trace.
                return lessons_corpus.item_key(stem, item["text"]) == expected.get(item["item"])

            try:
                result = retro_trace.apply_traces(ctx.vault, path, traces, partial=True,
                                                  verify=still_the_same)
                written += int(result["written"])
            except (retro_trace.TraceError, OSError) as exc:
                print(f"retro_rollup: {path}: {exc}", file=sys.stderr)
                ok = False
    report = _untraced_report(ctx, state, set(accepted))
    return {"mode": "apply" if accept is not None else "dry-run", "planned": len(plan),
            "notes_written": written, "skipped_entries": skipped_entries,
            "plan": plan if accept is None else [], "filed": filed, "reconciled": reconciled,
            "pending_filings": ([{"lesson": l, "repo": r["repo"]} for l, r in _pending_filings(state).items()]
                                if accept is None else []),
            "planned_filings": (preview_filings if preview is not None else
                                _planned_filings(ctx, state, by_key) if accept is None else []),
            **report}, ok


# --- output -----------------------------------------------------------------

def _print_table(rep: dict) -> None:
    order = ("raise", "file", "didnt_hold", "rule_not_followed", "quiet")
    titles = {"raise": "Raise priority", "file": "File an issue", "didnt_hold": "Fix didn't hold",
              "rule_not_followed": "Rule not followed", "quiet": "Tracked and quiet"}
    for bucket in order:
        rows = [l for l in rep["lessons"] if l["bucket"] == bucket and not l["dormant"]]
        if not rows:
            continue
        print(f"\n## {titles[bucket]} ({len(rows)})")
        for l in rows[:25]:
            track = l["tracking_ref"] or "no tracking"
            if l.get("tracking_source") == "match":
                track += " (match)"
            ms = f" · milestone {l['issue_milestone']}" if l.get("issue_milestone") else ""
            print(f"- {len(l['members'])} items · {l['recent_sessions']}/{l['sessions']} sessions · "
                  f"{len(l['projects'])} projects · {l['first_seen']} to {l['last_seen']} · "
                  f"{track}{ms} · {l['name']}")
    dormant = sum(1 for l in rep["lessons"] if l["dormant"])
    print(f"\n{len(rep['lessons'])} lessons, {dormant} dormant; totals {json.dumps(rep['totals'], sort_keys=True)}")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="retro_rollup.py")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault")
    common.add_argument("--insights-folder")
    common.add_argument("--state-dir")
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update", parents=[common])
    u.add_argument("--note")
    u.add_argument("--concurrency", type=int)
    sub.add_parser("rebuild", parents=[common])
    sub.add_parser("refresh-tracking", parents=[common])
    m = sub.add_parser("match", parents=[common])
    m.add_argument("--concurrency", type=int)
    m.add_argument("--all", action="store_true", help="check every untracked lesson again")
    r = sub.add_parser("report", parents=[common])
    r.add_argument("--json", action="store_true")
    b = sub.add_parser("backfill", parents=[common])
    b.add_argument("--apply", action="store_true")
    b.add_argument("--accept")
    return p


def main(argv=None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        if args.cmd == "backfill" and args.apply and not args.accept:
            raise UsageError("--apply and --accept FILE go together")
        ctx = Ctx(args)
        if args.cmd == "report":
            state = load_state(ctx)
            snoozes = {lid: lessons_notes.read_snooze(ctx.lessons_dir / fn)
                       for lid, fn in state.get("notes", {}).items()}
            rep = lessons_notes.report(state, snoozes)
            if args.json:
                print(json.dumps(rep, indent=1))
            else:
                _print_table(rep)
            return 0
        entries = _read_accept(args.accept) if args.cmd == "backfill" and args.accept else None
        lock = _Lock(ctx)
    except UsageError as exc:
        print(f"retro_rollup: {exc}", file=sys.stderr)
        return 2
    except (LockHeld, OSError) as exc:
        print(f"retro_rollup: {exc}", file=sys.stderr)
        return 1
    try:
        state = load_state(ctx)
        if args.cmd in ("update", "rebuild"):
            summary = regroup(ctx, state, judge=args.cmd == "update", note=getattr(args, "note", None))
            print(f"lessons: {summary['lessons']} (+{summary['new']} new), items in lessons: "
                  f"{summary['items_in_lessons']}, unjudged: {summary['unjudged']}, "
                  f"failed: {summary['failed']}, notes written: {summary['notes_written']}, "
                  f"removed: {summary['notes_removed']}, unreadable notes: {summary['unreadable']}")
            return 1 if _report_unreadable(summary) or summary["failed"] else 0
        if args.cmd == "match":
            found = match_lessons(ctx, state, recheck=args.all)
            summary = regroup(ctx, state, judge=False)
            print(json.dumps({**found, "notes_written": summary["notes_written"]}))
            return 1 if _report_unreadable(summary) or found["errors"] else 0
        if args.cmd == "refresh-tracking":
            found = match_lessons(ctx, state)  # a lesson's match is looked up before tracking is re-read
            errors = found.pop("errors") + refresh_tracking(ctx, state)
            summary = regroup(ctx, state, judge=False)
            print(json.dumps({"errors": errors, "match": found, "notes_written": summary["notes_written"]}))
            return 1 if _report_unreadable(summary) or errors else 0
        result, ok = backfill(ctx, state, entries if args.apply else None,
                              preview=entries if not args.apply else None)
        print(json.dumps(result, indent=1))
        return 0 if ok else 1
    except (OSError, Refused) as exc:
        print(f"retro_rollup: {exc}", file=sys.stderr)
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
