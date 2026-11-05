"""Haiku same-lesson judgments and their resumable SQLite cache."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading
import time

import obsidian_utils
import vault_index


RUBRIC: str = """You decide whether lessons state the SAME rule. Each lesson was written down after a coding session.
For each candidate, answer "same" if it states the same lesson as the anchor: someone who already follows the anchor's rule would not need the candidate written down separately. Different wording, project or example is fine if the rule is the same. Answer "different" if it is another rule, even on the same broad topic.
Examples:
- Anchor "Write `~/ro.sh` at the first router refusal in a session." / candidate "Offer the `~/ro-<id4>.sh` router override the first time the router blocks a Read." -> same.
- Anchor "Verify every file:line citation in a fix spec before dispatching." / candidate "Mutation-test every new guard." -> different.
Reply with ONLY a JSON object mapping each candidate number to "same" or "different", e.g. {"1": "same", "2": "different"}. No prose."""
RUBRIC_VERSION = "r2-2026-10-31"
MODEL = "haiku"
PARK_DAYS = 30
_spawn_count = 0
_spawn_lock = threading.Lock()


def _now() -> float:
    return time.time()


def spawn_count() -> int:
    """Return the number of Claude subprocess attempts in this process."""
    with _spawn_lock:
        return _spawn_count


def build_prompt(anchor: str, candidates: list[str]) -> str:
    return "\n".join(
        [RUBRIC, "", "ANCHOR: " + anchor, ""]
        + [f"CANDIDATE {i+1}: {c}" for i, c in enumerate(candidates)]
    )


def _parse_object(stdout: str) -> dict | None:
    match = re.search(r"\{.*\}", stdout, re.S)
    if match is None:
        return None
    try:
        obj = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_verdicts(stdout: str, n: int) -> list[bool] | None:
    obj = _parse_object(stdout)
    if obj is None:
        return None
    verdicts = []
    for i in range(n):
        value = obj.get(str(i + 1))
        word = value.strip().lower() if isinstance(value, str) else None
        if word not in ("same", "different"):
            return None  # a missing or odd answer must not cache as "different"
        verdicts.append(word == "same")
    return verdicts


def run_haiku(prompt: str, timeouts: tuple[int, int] = (120, 240)) -> str | None:
    global _spawn_count
    for t in timeouts:
        try:
            # A stripped cwd avoids loading repo CLAUDE.md, memory, and git status.
            cwd = obsidian_utils.worker_cwd()
            with _spawn_lock:
                _spawn_count += 1
            result = subprocess.run(
                # No tools and no MCP: retro text must not reach a tool-capable session.
                ["claude", "-p", "--model", MODEL, "--setting-sources", "project",
                 "--strict-mcp-config", "--tools", "", "--no-session-persistence"],
                input=prompt,
                capture_output=True, text=True, timeout=t,
                env=obsidian_utils.worker_env(),
                cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            return None
        return result.stdout if result.returncode == 0 else None
    return None


def default_cache_path() -> Path:
    return Path.home() / ".claude" / "obsidian-brain" / "lessons-verdicts.db"


def _cands_key(cand_keys: list[str]) -> str:
    return hashlib.sha1("\n".join(cand_keys).encode()).hexdigest()


class VerdictCache:
    """Store unordered pairs; a positive judgment is sticky in either direction."""

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._conn = vault_index._connect(str(path))
        path.chmod(0o600)
        with self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS pairs ("
                "a TEXT, b TEXT, rubric TEXT, model TEXT, same INTEGER, "
                "PRIMARY KEY(a,b,rubric,model))"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS anchors ("
                "anchor TEXT, rubric TEXT, model TEXT, "
                "PRIMARY KEY(anchor,rubric,model))"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS failures ("
                "anchor TEXT, cands TEXT, rubric TEXT, model TEXT, parked_at REAL, "
                "PRIMARY KEY(anchor,cands,rubric,model))"
            )
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(failures)")}
            if "parked_at" not in columns:
                self._conn.execute("ALTER TABLE failures ADD COLUMN parked_at REAL")
        # WAL mode keeps recent rows in side files; they hold the same data.
        for side in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            if side.exists():
                side.chmod(0o600)

    def judged(self, anchor_key: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM anchors WHERE anchor=? AND rubric=? AND model=?",
            (anchor_key, RUBRIC_VERSION, MODEL),
        ).fetchone() is not None

    def park(self, anchor_key: str, cand_keys: list[str]) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO failures VALUES (?, ?, ?, ?, ?)",
                (anchor_key, _cands_key(cand_keys), RUBRIC_VERSION, MODEL, _now()),
            )

    def parked(self, anchor_key: str, cand_keys: list[str]) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM failures WHERE anchor=? AND cands=? AND rubric=? AND model=? "
            "AND parked_at > ?",
            (anchor_key, _cands_key(cand_keys), RUBRIC_VERSION, MODEL,
             _now() - PARK_DAYS * 86400),
        ).fetchone() is not None

    def record(self, anchor_key: str, cand_keys: list[str], verdicts: list[bool]) -> None:
        with self._conn:
            for key, same in zip(cand_keys, verdicts):
                a, b = sorted((anchor_key, key))
                self._conn.execute(
                    "INSERT INTO pairs VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(a,b,rubric,model) DO UPDATE "
                    "SET same=MAX(pairs.same, excluded.same)",
                    (a, b, RUBRIC_VERSION, MODEL, int(same)),
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO anchors VALUES (?, ?, ?)",
                (anchor_key, RUBRIC_VERSION, MODEL),
            )

    def edges(self) -> set[tuple[str, str]]:
        return {(row[0], row[1]) for row in self._conn.execute(
            "SELECT a, b FROM pairs WHERE same=1 AND rubric=? AND model=? AND a<b",
            (RUBRIC_VERSION, MODEL),
        )}

    def prune(self, live_keys: set[str]) -> int:
        # Scan keys rather than building an IN clause with SQLite's parameter cap.
        with self._conn:
            before = self._conn.total_changes
            dead_pairs = [tuple(row) for row in self._conn.execute(
                "SELECT a, b, rubric, model FROM pairs"
            ) if row[0] not in live_keys or row[1] not in live_keys]
            dead_anchors = [tuple(row) for row in self._conn.execute(
                "SELECT anchor, rubric, model FROM anchors"
            ) if row[0] not in live_keys]
            dead_failures = [tuple(row) for row in self._conn.execute(
                "SELECT anchor, cands, rubric, model FROM failures"
            ) if row[0] not in live_keys]
            self._conn.executemany(
                "DELETE FROM pairs WHERE a=? AND b=? AND rubric=? AND model=?",
                dead_pairs,
            )
            self._conn.executemany(
                "DELETE FROM anchors WHERE anchor=? AND rubric=? AND model=?",
                dead_anchors,
            )
            self._conn.executemany(
                "DELETE FROM failures WHERE anchor=? AND cands=? AND rubric=? AND model=?",
                dead_failures,
            )
            return self._conn.total_changes - before

    def close(self):
        self._conn.close()


def verify_anchors(texts: dict[str, str], cands: dict[str, list[str]],
                   cache: VerdictCache, runner=None, concurrency: int = 8,
                   progress=None) -> dict:
    if runner is None:
        runner = run_haiku
    anchors = []
    cache_hits = parked = 0
    for anchor, keys in cands.items():
        if not keys:
            continue
        if cache.judged(anchor):
            cache_hits += 1
        elif cache.parked(anchor, keys):
            parked += 1
        else:
            anchors.append(anchor)
    total = len(anchors)
    judged = failed = unparseable = 0

    def verify(anchor):
        out = runner(build_prompt(texts[anchor], [texts[c] for c in cands[anchor]]))
        # An empty reply is a runner failure, not an unparseable answer: retry it.
        if out is not None and not out.strip():
            out = None
        verdicts = parse_verdicts(out, len(cands[anchor])) if out is not None else None
        return verdicts, out is not None and verdicts is None

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(verify, a): a for a in anchors}
        for done, future in enumerate(as_completed(futures), 1):
            verdicts, invalid = future.result()
            anchor = futures[future]
            if verdicts is None:
                failed += 1
                if invalid:
                    cache.park(anchor, cands[anchor])
                    unparseable += 1
            else:
                cache.record(anchor, cands[anchor], verdicts)
                judged += 1
            if progress is not None and done % 100 == 0 and done != total:
                progress(done, total, failed)
    if progress is not None:
        progress(total, total, failed)
    return {"judged": judged, "failed": failed, "unparseable": unparseable,
            "parked": parked, "cache_hits": cache_hits, "calls": total,
            "skipped": len(cands) - total}


def name_lessons(lessons: list[dict], texts: dict[str, str], runner=None) -> dict[str, str]:
    if runner is None:
        runner = run_haiku
    names = {}
    for start in range(0, len(lessons), 40):
        batch = lessons[start:start + 40]
        lines = [
            'Give each lesson a short imperative rule name (max 80 characters). '
            'Reply with ONLY a JSON object mapping each lesson number to its name, '
            'e.g. {"1": "Verify citations", "2": "Test new guards"}.',
        ]
        for i, lesson in enumerate(batch, 1):
            centre = lesson["centre"]
            others = [key for key in lesson["members"] if key != centre][:4]
            lines.extend(["", f"LESSON {i}:", texts[centre]])
            lines.extend("- " + texts[key] for key in others)
        out = runner("\n".join(lines))
        obj = _parse_object(out) if out is not None else None
        for i, lesson in enumerate(batch, 1):
            name = obj.get(str(i)) if obj is not None else None
            name = name.strip() if isinstance(name, str) else ""
            if not name or len(name) > 80:
                name = texts[lesson["centre"]].split(". ", 1)[0][:80]
            names[lesson["id"]] = name
    return names
