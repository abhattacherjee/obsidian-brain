"""Haiku same-lesson judgments and their resumable SQLite cache."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
import subprocess

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
    return [obj.get(str(i+1)) == "same" for i in range(n)]


def run_haiku(prompt: str, timeouts: tuple[int, int] = (120, 240)) -> str | None:
    for t in timeouts:
        try:
            result = subprocess.run(
                ["claude", "-p", "--model", MODEL], input=prompt,
                capture_output=True, text=True, timeout=t,
                env=obsidian_utils.worker_env(),
            )
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            return None
        return result.stdout if result.returncode == 0 else None
    return None


def default_cache_path() -> Path:
    return Path.home() / ".claude" / "obsidian-brain" / "lessons-verdicts.db"


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
        # WAL mode keeps recent rows in side files; they hold the same data.
        for side in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            if side.exists():
                side.chmod(0o600)

    def judged(self, anchor_key: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM anchors WHERE anchor=? AND rubric=? AND model=?",
            (anchor_key, RUBRIC_VERSION, MODEL),
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
            self._conn.executemany(
                "DELETE FROM pairs WHERE a=? AND b=? AND rubric=? AND model=?",
                dead_pairs,
            )
            self._conn.executemany(
                "DELETE FROM anchors WHERE anchor=? AND rubric=? AND model=?",
                dead_anchors,
            )
            return self._conn.total_changes - before

    def close(self):
        self._conn.close()


def verify_anchors(texts: dict[str, str], cands: dict[str, list[str]],
                   cache: VerdictCache, runner=None, concurrency: int = 8,
                   progress=None) -> dict:
    if runner is None:
        runner = run_haiku
    anchors = [a for a, keys in cands.items() if keys and not cache.judged(a)]
    total = len(anchors)
    judged = failed = 0

    def verify(anchor):
        out = runner(build_prompt(texts[anchor], [texts[c] for c in cands[anchor]]))
        return parse_verdicts(out, len(cands[anchor])) if out is not None else None

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(verify, a): a for a in anchors}
        for done, future in enumerate(as_completed(futures), 1):
            verdicts = future.result()
            if verdicts is None:
                failed += 1
            else:
                anchor = futures[future]
                cache.record(anchor, cands[anchor], verdicts)
                judged += 1
            if progress is not None and done % 100 == 0 and done != total:
                progress(done, total, failed)
    if progress is not None:
        progress(total, total, failed)
    return {"judged": judged, "failed": failed, "skipped": len(cands) - total}


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
