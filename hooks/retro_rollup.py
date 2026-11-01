"""Retro lessons rollup CLI (#471): group retro Process Improvements items into
recurring lessons, render lesson notes and report, and trace accepted lessons
back into the source retros.

Commands: update, rebuild, refresh-tracking, report, backfill. Exit 0 on
success, 1 on a runtime or partial failure (unjudged anchors, gh errors, the
lock held, a write error) with the output still printed, 2 on a usage or
config error.
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
import lessons_notes
import lessons_verify
import retro_trace

STATE_NAME = "lessons-state.json"
CACHE_NAME = "lessons-verdicts.db"
LOCK_NAME = "lessons.lock"
LESSONS_SUBFOLDER = "lessons"
RETIRED_NAME = "retired.json"
GH_TIMEOUT_SECONDS = 10
GH_MAX_WORKERS = 8
_PRIORITY = re.compile(r"^P[0-9]")
_REF = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)$")


class UsageError(Exception):
    pass


class LockHeld(Exception):
    pass


# --- context ---------------------------------------------------------------

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
        self.lessons_folder = f"{self.insights}/{LESSONS_SUBFOLDER}"
        self.state_dir = Path(args.state_dir or Path.home() / ".claude" / "obsidian-brain")
        self.recent_days = int(config.get("lessons_recent_days", 90))
        self.concurrency = int(getattr(args, "concurrency", None) or config.get("lessons_concurrency", 8))
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
    return {"version": 1, "generated": None, "lessons": {}, "tracking": {}, "notes": {}, "totals": {}}


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
    return base


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


def regroup(ctx: Ctx, state: dict, *, judge: bool, note: str | None = None, cache=None) -> dict:
    """Judge (optionally), re-form lessons, write changed notes; return a summary."""
    items, by_key, skipped = _load(ctx)
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
        else:
            cache.prune(set(by_key))
        live = set(by_key)
        edges = {e for e in cache.edges() if e[0] in live and e[1] in live}
        unjudged = sum(1 for k in by_key if not cache.judged(k))
    finally:
        if own_cache:
            cache.close()

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
        tracking = lessons_group.lesson_tracking(lesson["members"], by_key)
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

    in_lessons = sum(len(l["members"]) for l in out_lessons.values())
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


# --- tracking refresh -------------------------------------------------------------

def _run_gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=GH_TIMEOUT_SECONDS)


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
    for key, t in refs.items():
        if t["kind"] == "memory":
            if memory_stems is None:
                import memory_sources
                memory_stems = {Path(p).stem for p in memory_sources.memory_sources("claude-code")}
            state["tracking"][key] = {"exists": t["ref"] in memory_stems}
        elif t["kind"] == "path":
            raw = t["ref"]
            if raw.startswith(("~", "/")):
                state["tracking"][key] = {"exists": Path(os.path.expanduser(raw)).exists()}
            else:
                state["tracking"][key] = {"exists": None}  # repo-relative: cannot check here
    return errors


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
            elif not state["lessons"][lid].get("tracking"):
                reason = "lesson-untracked"
            elif lid not in accepted:
                reason = "not-accepted"
            else:
                reason = "accepted-not-written"
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
    if not isinstance(data, list) or not all(
            isinstance(e, dict) and isinstance(e.get("lesson_id"), str)
            and isinstance(e.get("tracking_ref"), str) for e in data):
        raise UsageError('accept file must be a JSON list of {"lesson_id", "tracking_ref"}')
    return data


def backfill(ctx: Ctx, state: dict, accept: list[dict] | None) -> tuple[dict, bool]:
    """Dry run when accept is None; else trace accepted lessons. Returns (result, ok)."""
    items, by_key, _ = _load(ctx)
    plan, skipped_entries = [], []
    accepted = {}
    for entry in accept or []:
        rec = state["lessons"].get(entry["lesson_id"])
        if rec is None:
            skipped_entries.append({**entry, "reason": "unknown lesson"})
        elif not rec.get("tracking") or rec["tracking"]["ref"] != entry["tracking_ref"]:
            skipped_entries.append({**entry, "reason": "tracking changed"})
        else:
            accepted[entry["lesson_id"]] = rec
    lessons = accepted if accept is not None else {
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
    ok = True
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
            current = {lessons_corpus.item_key(stem, it["text"]): it for it in retro_trace.parse_items(text)}
            traces = []
            for key, tracking in wanted:
                it = current.get(key)
                if it is not None and it["trace"] is None:
                    traces.append(_trace_for(tracking, it["item"]))
            if not traces:
                continue
            try:
                result = retro_trace.apply_traces(ctx.vault, path, traces, partial=True)
                written += int(result["written"])
            except (retro_trace.TraceError, OSError) as exc:
                print(f"retro_rollup: {path}: {exc}", file=sys.stderr)
                ok = False
    report = _untraced_report(ctx, state, set(accepted))
    return {"mode": "apply" if accept is not None else "dry-run", "planned": len(plan),
            "notes_written": written, "skipped_entries": skipped_entries,
            "plan": plan if accept is None else [], **report}, ok


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
            track = f" · {l['tracking_ref']}" if l["tracking_ref"] else ""
            ms = f" · milestone {l['issue_milestone']}" if l.get("issue_milestone") else ""
            print(f"- {l['recent_sessions']}/{l['sessions']} sessions · {len(l['projects'])} projects"
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
        if args.cmd == "backfill" and args.apply != bool(args.accept):
            raise UsageError("--apply and --accept FILE go together")
        ctx = Ctx(args)
        if args.cmd == "report":
            rep = lessons_notes.report(load_state(ctx))
            if args.json:
                print(json.dumps(rep, indent=1))
            else:
                _print_table(rep)
            return 0
        accept = _read_accept(args.accept) if args.cmd == "backfill" and args.apply else None
        lock = _Lock(ctx)
    except UsageError as exc:
        print(f"retro_rollup: {exc}", file=sys.stderr)
        return 2
    except LockHeld as exc:
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
            return 1 if summary["failed"] else 0
        if args.cmd == "refresh-tracking":
            errors = refresh_tracking(ctx, state)
            summary = regroup(ctx, state, judge=False)
            print(json.dumps({"errors": errors, "notes_written": summary["notes_written"]}))
            return 1 if errors else 0
        result, ok = backfill(ctx, state, accept)
        print(json.dumps(result, indent=1))
        return 0 if ok else 1
    except OSError as exc:
        print(f"retro_rollup: {exc}", file=sys.stderr)
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
