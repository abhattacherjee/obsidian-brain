"""Project memory consolidation: audit the index, validate a move plan, apply it."""

from __future__ import annotations

import argparse
import contextlib
import glob
import hashlib
import shutil
import tempfile
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True

import vault_index
from memory_links import line_pointers, line_asides, _link_targets, _md_child_link_targets
from note_writer import _acquire_lock, _atomic_rewrite, _release_lock
from memory_sources import _projects_root
from obsidian_utils import (_cc_encoded_dirnames, _transcript_cwd, canonical_project_name, indexed_folders,
                            load_config, scrub_secrets, slugify, write_vault_note)


class StoreError(Exception):
    """The store cannot be selected or read."""


class PlanError(Exception):
    """The consolidation plan is invalid."""


def resolve_store(store_arg: str | None, cwd: str) -> Path:
    projects = _projects_root()
    try:
        if store_arg is None:
            root = cwd
            try:
                git = subprocess.run(
                    ["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=True,
                )
                root = str(Path(git.stdout.rstrip("\n")).parent)
            except (OSError, subprocess.SubprocessError, UnicodeError):
                pass
            candidates = [projects / name / "memory" for name in sorted(_cc_encoded_dirnames(root))]
            stores = [p for p in candidates if p.is_dir()]
            if not stores:
                raise StoreError(f"no memory store for {root}; pass --store")
            if len(stores) > 1:
                raise StoreError(f"multiple memory stores: {', '.join(map(str, stores))}; pass --store")
            p = stores[0]
        else:
            p = Path(store_arg)
        if os.path.islink(p) or os.path.islink(p.parent):
            raise StoreError(f"symlinked memory store or project: {p}")
        if not p.is_dir():
            raise StoreError(f"not a directory: {p}")
        p = p.resolve()
        if p.name != "memory" or p.parent.parent != projects.resolve():
            raise StoreError(f"expected {projects}/<project>/memory, got {p}")
        return p
    except (OSError, RuntimeError, ValueError) as exc:
        raise StoreError(str(exc)) from exc


def _read_file(path: Path, limit: int = 2_000_000) -> tuple[str, float]:
    def open_regular(name, flags):
        # A replacement symlink or FIFO must not redirect or block the read.
        return os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK)

    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise StoreError(f"expected a regular, non-symlink file: {path}")
        with open(path, encoding="utf-8", newline="", opener=open_regular) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise StoreError(f"expected a regular file: {path}")
            text = stream.read(limit + 1)
        if len(text) > limit:
            raise StoreError(f"{path} exceeds {limit:,} characters")
        return text, info.st_mtime
    except (OSError, UnicodeError) as exc:
        raise StoreError(f"cannot read {path}: {exc}") from exc


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1]:
        if value[0] == '"':
            return value[1:-1].replace(r'\"', '"')
        if value[0] == "'":
            return value[1:-1]
    return value


def _norm_body(text: str) -> str:
    """Drop leading blank lines and trailing whitespace; keep the first line's indentation."""
    return re.sub(r"\A(?:[ \t]*\r?\n)+", "", text).rstrip()


def read_entry(path: Path) -> dict:
    text, mtime = _read_file(path)
    fields = {}
    body = _norm_body(text)
    lines = text.splitlines(keepends=True)
    if lines and lines[0].rstrip("\r\n") == "---":
        metadata = False
        for i, raw in enumerate(lines[1:], 1):
            line = raw.rstrip("\r\n")
            if line == "---":
                body = _norm_body("".join(lines[i + 1:]))
                break
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, sep, value = line.partition(":")
            if line[0].isspace():
                if metadata and sep and key.strip() == "type":
                    fields["nested_type"] = _unquote(value)
            else:
                metadata = key == "metadata" and bool(sep) and not value.strip()
                if sep and key in ("type", "name", "description"):
                    fields[key] = _unquote(value)
        else:
            fields.clear()
    return dict(slug=path.stem, name=fields.get("name", ""), description=fields.get("description", ""),
                memory_type=fields.get("type", fields.get("nested_type", "")),
                body=body, text=text, mtime=mtime)


def _index_references(lines: list[str], names: set[str]) -> tuple[Counter, set[str]]:
    pointers = Counter()
    referenced = set()
    for line in lines:
        ptr = line_pointers(line, names)
        pointers.update(ptr)
        referenced.update(ptr | line_asides(line, names))
    return pointers, referenced


def audit(store: Path) -> dict:
    text, _ = _read_file(store / "MEMORY.md")
    try:
        names = {p.name for p in store.iterdir() if p.name.endswith(".md") and p.name != "MEMORY.md"}
    except OSError as exc:
        raise StoreError(str(exc)) from exc
    lines = text.splitlines()
    pointers, referenced = _index_references(lines, names)
    size = len(text.encode("utf-8"))
    over = []
    if size >= 17000:
        over.append("soft_bytes")
    if size >= 24000:
        over.append("hard_bytes")
    if len(lines) > 200:
        over.append("lines")
    entries = []
    for name in sorted(names, key=lambda name: Path(name).stem):
        entry = read_entry(store / name)
        stamp = datetime.fromtimestamp(entry["mtime"]).astimezone()
        entries.append(dict(slug=entry["slug"], memory_type=entry["memory_type"],
                            body_chars=len(entry["body"]), mtime=stamp.isoformat(timespec="seconds"),
                            indexed=name in referenced))
    orphans = [entry["slug"] for entry in entries if not entry["indexed"]]
    return {
        "store": str(store),
        "index": dict(bytes=size, chars=len(text), lines=len(lines), soft_limit_bytes=17000,
                      hard_limit_bytes=24000, line_limit=200, over=over),
        "entries": entries,
        "orphans": orphans,
        "dangling": sorted(_md_child_link_targets(text) - names),
        "duplicates": sorted(Path(name).stem for name, n in pointers.items() if n > 1),
        "mode": "rescue" if orphans else "shrink" if over else "ok",
    }


def _target_errors(target, vault_root: Path, allow_missing: bool = False) -> list[str]:
    if (not isinstance(target, str) or not target.endswith(".md")
            or "\0" in target or Path(target).is_absolute()):
        return ["target must be a vault-relative .md path"]
    errors = []
    p = vault_root
    try:
        symlink = os.path.islink(p)
        for part in Path(target).parts:
            p = p / part
            symlink |= os.path.islink(p)
        if symlink:
            errors.append("target contains a symlink")
        if not p.resolve().is_relative_to(vault_root.resolve()):
            errors.append("target escapes the vault")
        if not p.is_file() and not allow_missing:
            errors.append("target is not an existing file")
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"invalid target: {exc}")
    return errors


_ACTION_KEYS = {"new": {"type", "topic"}, "append": {"target"}, "retire": {"reason"}}
_TYPES = ("claude-insight", "claude-error-fix", "claude-decision")


def load_plan(path: Path, store: Path, vault_root: Path,
              moved_dir: Path | None = None) -> tuple[list[dict], list[str]]:
    def reject_constant(value):
        raise PlanError(f"invalid JSON constant: {value}")

    try:
        text, _ = _read_file(path)
        plan = json.loads(text, parse_constant=reject_constant)
    except (StoreError, PlanError, ValueError, RecursionError) as exc:
        return [], [str(exc)]
    return validate_plan(plan, store, vault_root, moved_dir)


def validate_plan(plan, store: Path, vault_root: Path,
                  moved_dir: Path | None = None) -> tuple[list[dict], list[str]]:
    """Check a parsed plan. ``moved_dir`` (resume only) lets an entry whose
    original an interrupted run already moved count as present."""
    if not isinstance(plan, dict):
        return [], ["plan must be an object"]
    errors = []
    if set(plan) != {"version", "store", "items"}:
        errors.append("plan keys must be version, store, items")
    if type(plan.get("version")) is not int or plan["version"] != 1:
        errors.append("version must be 1")
    try:
        if Path(plan.get("store")).resolve() != store.resolve():
            errors.append("store mismatch")
    except (TypeError, ValueError, OSError, RuntimeError):
        errors.append("invalid store path")
    items = plan.get("items")
    if not isinstance(items, list) or not items:
        return [], [*errors, "items must be a non-empty list"]

    seen = set()
    normalized = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"item {i} (missing slug): expected an object")
            continue
        slug = item.get("slug")
        issues = []
        valid_slug = isinstance(slug, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,200}", slug)
        if not valid_slug or slug.casefold() in ("memory", "memory.md"):
            issues.append("invalid slug")
        else:
            if slug in seen:
                issues.append("duplicate slug")
            seen.add(slug)
            source = store / (slug + ".md")
            if moved_dir is not None and not os.path.lexists(source):
                source = moved_dir / (slug + ".md")
            if source.is_symlink() or not source.is_file():
                issues.append("missing or non-regular entry file")
        action = item.get("action")
        if not isinstance(action, str) or action not in _ACTION_KEYS:
            issues.append("invalid action")
        else:
            for key in sorted(item.keys() - {"slug", "action"} - _ACTION_KEYS[action]):
                issues.append(f"unknown key {key}")
        if action == "new":
            if item.setdefault("type", "claude-insight") not in _TYPES:
                issues.append("invalid type")
            if "topic" not in item and valid_slug:
                base = re.sub(r"^(feedback|technical|reference|project|user)_", "", slug)
                item["topic"] = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:40].rstrip("-")
            topic = item.get("topic")
            if "topic" in item and (not isinstance(topic, str) or not re.fullmatch(r"[a-z0-9-]{1,40}", topic)):
                issues.append("invalid topic")
        elif action == "append":
            # On resume a deleted target refuses only its own item (read-back), not the run.
            issues.extend(_target_errors(item.get("target"), vault_root, allow_missing=moved_dir is not None))
        elif action == "retire":
            reason = item.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                issues.append("reason must be a non-empty string")
        errors.extend(f"item {i} ({slug!r}): {issue}" for issue in issues)
        normalized.append(item)
    return normalized, errors


def index_edit(
    index_text: str, store_names: set[str], moving: set[str], pointer_line: str
) -> tuple[str, list[str], dict[str, str], dict[str, str]]:
    lines = index_text.split("\n")
    pointers = [line_pointers(line, store_names) for line in lines]
    refused = {}
    eligible = set(moving)
    # Repeat until no new refusal: a slug refused on one mixed line stays, so
    # another line it shares with a moving slug becomes mixed too. One pass
    # kept that second line and left the moving slug's pointer dangling.
    changed = True
    while changed:
        changed = False
        for ptr in pointers:
            staying = ptr - eligible
            if staying and ptr & eligible:
                for name in ptr & eligible:
                    refused[Path(name).stem] = ("index line also points at staying entries: "
                                                + ", ".join(sorted(staying)))
                eligible -= ptr
                changed = True

    kept, dropped, warnings = [], [], {}
    insert_at = None
    for line, ptr in zip(lines, pointers):
        if ptr and ptr <= eligible:
            dropped.append(line)
            # A staying entry named only here loses its last reference.
            for name in line_asides(line, store_names) - moving:
                warnings[Path(name).stem] = "named as an aside on a dropped index line"
            continue
        kept.append(line)
        if ptr and line.startswith("- "):
            insert_at = len(kept)
        for name in line_asides(line, store_names) & eligible:
            warnings[Path(name).stem] = "named as an aside on a kept index line"
    if dropped and pointer_line:
        if insert_at is None:
            insert_at = len(kept) - int(index_text.endswith("\n"))
        kept.insert(insert_at, pointer_line)
    return "\n".join(kept), dropped, refused, warnings


# ---------------------------------------------------------------------------
# apply: write vault notes, back up the store, move originals, edit the index
# ---------------------------------------------------------------------------

_TYPE_TAGS = {"claude-insight": "claude/insight", "claude-error-fix": "claude/error-fix",
              "claude-decision": "claude/decision"}
MOVED_TAG = "claude/topic/moved-from-memory"
# Progress markers only: the steps re-run from the snapshot and read the
# filesystem, so no branch depends on a state except "done".
RUN_STATES = ("backed-up", "notes-written", "moved", "done")
_MANIFEST_LIMIT = 50_000_000
# Every name `verify` emits, in order. The skill contract test checks SKILL.md names each one.
VERIFY_CHECKS = ("state", "index-links-resolve", "no-duplicates", "all-indexed", "removed-equals-moved",
                 "notes-lossless", "backups-present", "sizes")
_BACKUP_PREFIX = "memory-moved-"
# The start marker is the one a rerun searches for (plan "Markers"). The end
# marker bounds the copied body, so the lossless check still works after a
# later append to the same note adds sections below it.
_START = "<!-- migrated_from_memory: {slug} -->"
_END = "<!-- /migrated_from_memory: {slug} -->"
_END_PREFIX = "<!-- /migrated_from_memory:"


class ApplyError(Exception):
    """The run cannot go on (exit 2)."""


class _ItemError(Exception):
    """One item is refused; the rest of the run goes on."""


def _checkpoint(name: str) -> None:
    """Test hook: the stop-point tests raise here to simulate a crash."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sizes(text: str) -> dict:
    return dict(bytes=len(text.encode("utf-8")), chars=len(text), lines=len(text.splitlines()))


def _private_dirs(path: Path) -> None:
    """Create each missing level of ``path`` 0o700 (makedirs' mode is leaf-only)."""
    missing = []
    p = path
    while not os.path.lexists(p):
        missing.append(p)
        p = p.parent
    if os.path.islink(p) or not p.is_dir():
        raise ApplyError(f"not a real directory: {p}")
    for d in reversed(missing):
        os.mkdir(d, 0o700)
        os.chmod(d, 0o700)


def _contained(path: Path, vault_root: Path) -> Path:
    """Refuse a write target that is or passes through a symlink, or leaves the vault."""
    rel = path.relative_to(vault_root)
    p = vault_root
    for part in rel.parts:
        p = p / part
        if os.path.islink(p):
            raise _ItemError(f"symlink in write path: {p}")
    if not path.resolve().is_relative_to(vault_root.resolve()):
        raise _ItemError(f"write path escapes the vault: {path}")
    return path


def _yaml(value: str) -> str:
    # JSON strings are valid YAML double-quoted scalars; quoting keeps a slug
    # such as "1.5" or "true" a string.
    return json.dumps(value, ensure_ascii=False)


def _title(index_text: str, entry: dict) -> str:
    slug = entry["slug"]
    m = re.search(r"\[([^\[\]\n]{1,300})\]\(\s*<?(?:\./)?" + re.escape(slug) + r"\.md[\s>)\"']",
                  index_text)
    link = m.group(1) if m else ""
    named = re.sub(r"[-_]+", " ", re.sub(r"^(feedback|technical|reference|project|user)[-_]", "", entry["name"])).strip()
    words = r"[A-Za-z0-9]+"
    title = link if len(re.findall(words, link)) >= len(re.findall(words, named)) else named
    return " ".join((title or named or entry["description"] or slug).split())


def _summary(entry: dict) -> str:
    return " ".join(entry["description"].split()) or "(no description in the memory file)"


def _provenance(slug: str, date: str, backup_dir: Path) -> str:
    return (f"> Moved from project memory `{slug}` on {date}; "
            f"originals in `{backup_dir}` (`moved/{slug}.md`).")


def _render_new(item, entry, title, project, now, store, backup_dir) -> str:
    slug, date = entry["slug"], now.strftime("%Y-%m-%d")
    tags = [_TYPE_TAGS[item["type"]], f"claude/project/{project}", MOVED_TAG]
    if f"claude/topic/{item['topic']}" not in tags:
        tags.append(f"claude/topic/{item['topic']}")
    # No source_session / source_session_note: gather_session_evidence picks
    # insights by source_session, so the moving session would count a moved
    # fact as its own finding (#495).
    front = [
        "---", f"type: {item['type']}", f"date: {date}",
        f"created_at: {now.isoformat(timespec='seconds')}", f"project: {project}",
        f"migrated_from_memory: {_yaml(slug)}", f"synced_from: {_yaml(str(store / (slug + '.md')))}",
        f"memory_type: {_yaml(entry['memory_type'])}", "status: active", "tags:",
        *(f"  - {tag}" for tag in tags), "---",
    ]
    return "\n".join(front) + (
        f"\n\n# {title}\n\n{_provenance(slug, date, backup_dir)}\n\n## Summary\n\n{_summary(entry)}"
        f"\n\n## Details\n\n{_norm_body(entry['body'])}\n\n{_END.format(slug=slug)}\n")


def _render_section(entry, title, now, backup_dir) -> str:
    slug, date = entry["slug"], now.strftime("%Y-%m-%d")
    return (f"## From project memory: {title} (moved {date})\n{_START.format(slug=slug)}\n\n"
            f"{_provenance(slug, date, backup_dir)}\n\n### Summary\n\n{_summary(entry)}\n\n"
            f"### Details\n\n{_norm_body(entry['body'])}\n\n{_END.format(slug=slug)}\n")


def _details(text: str, slug: str, action: str) -> str | None:
    """The copied body in a written note, or None when the markers are missing."""
    if action == "new":
        start, header = 0, "\n## Details\n"
    else:
        start, header = text.find(_START.format(slug=slug)), "\n### Details\n"
        if start < 0:
            return None
    at = text.find(header, start)
    end = text.find(_END.format(slug=slug), start)
    if at < 0 or end < at:
        return None
    return text[at + len(header):end]


def _summary_text(text: str, slug: str, action: str) -> str | None:
    """The copied summary in a written note, or None when its markers are missing."""
    if action == "new":
        start, summary, details = 0, "\n## Summary\n", "\n## Details\n"
    else:
        start = text.find(_START.format(slug=slug))
        if start < 0:
            return None
        summary, details = "\n### Summary\n", "\n### Details\n"
    at = text.find(summary, start)
    end = text.find(details, start)
    if at < 0 or end < at + len(summary):
        return None
    return text[at + len(summary):end]


def _frontmatter(text: str) -> str:
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return ""
    for i, line in enumerate(lines[1:], 1):
        if line.rstrip("\r") == "---":
            return "\n".join(lines[1:i])
    return ""


def _check_readback(path: Path, entry: dict, action: str, prefix: str | None = None) -> None:
    """Check the copied body and whitespace-normalized summary against the memory."""
    try:
        text, _ = _read_file(path)
    except StoreError as exc:
        raise _ItemError(f"read-back failed: {exc}") from exc
    _check_note_text(path, text, entry, action, prefix)


def _check_note_text(path: Path, text: str, entry: dict, action: str, prefix: str | None = None) -> None:
    details = _details(text, entry["slug"], action)
    if details is None:
        raise _ItemError(f"read-back failed: markers missing in {path}")
    if _norm_body(details) != _norm_body(entry["body"]):
        raise _ItemError(f"read-back failed: copied text differs from the memory body in {path}")
    summary = _summary_text(text, entry["slug"], action)
    if summary is None or " ".join(summary.split()) != _summary(entry):
        raise _ItemError(f"read-back failed: copied summary differs from the memory description in {path}")
    if prefix is not None and not text.startswith(prefix):
        raise _ItemError(f"read-back failed: the rest of {path} changed")
    if action == "new" and re.search(r"^source_session", _frontmatter(text), re.M):
        raise _ItemError(f"read-back failed: {path} carries source_session")


def _scan_markers(folder: Path) -> dict[str, list[Path]]:
    """Map slug -> notes under ``folder`` whose frontmatter has migrated_from_memory."""
    found: dict[str, list[Path]] = {}
    if not folder.is_dir():
        return found
    pattern = re.compile(r'^migrated_from_memory:[ \t]*"?([A-Za-z0-9][A-Za-z0-9_.-]{0,200})"?[ \t]*\r?$', re.M)
    for note in sorted(folder.rglob("*.md")):
        if note.is_symlink() or not note.is_file():
            continue
        try:
            with open(note, encoding="utf-8", errors="replace") as stream:
                head = stream.read(16_384)
        except OSError:
            continue
        for slug in pattern.findall(_frontmatter(head)):
            found.setdefault(slug, []).append(note)
    return found


def _marker_project(note: Path) -> str | None:
    """The project a moved note records: frontmatter ``project:``, else its ``claude/project/`` tag."""
    try:
        with open(note, encoding="utf-8", errors="replace") as stream:
            front = _frontmatter(stream.read(16_384))
    except OSError:
        return None
    m = re.search(r"^project:[ \t]*(.+?)[ \t]*\r?$", front, re.M)
    if m:
        return _unquote(m.group(1))
    m = re.search(r"^[ \t]*-[ \t]*claude/project/(\S+?)[ \t]*\r?$", front, re.M)
    return m.group(1) if m else None


def _write_json(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".run-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_run(backup_dir: Path) -> dict:
    if os.path.islink(backup_dir) or not backup_dir.is_dir():
        raise ApplyError(f"not a backup directory: {backup_dir}")
    try:
        text, _ = _read_file(backup_dir / "run.json")
        run = json.loads(text)
    except (StoreError, ValueError, RecursionError) as exc:
        raise ApplyError(f"cannot read run.json: {exc}") from exc
    keys = ("version", "state", "store", "vault", "insights_folder", "plan", "planned", "refused", "written",
            "retired", "moved")
    if not isinstance(run, dict) or any(k not in run for k in keys):
        raise ApplyError("run.json is missing fields")
    if run["version"] != 1 or run["state"] not in RUN_STATES:
        raise ApplyError(f"run.json has version {run['version']!r}, state {run['state']!r}")
    return run


def _unfinished_runs(backup_root: Path, store: Path) -> list[Path]:
    out = []
    try:
        dirs = sorted(backup_root.glob(_BACKUP_PREFIX + "*"))
    except OSError:
        return out
    for d in dirs:
        if ".tmp-" in d.name:
            continue  # a partial backup that was never renamed into place is not a run
        try:
            run = _read_run(d)
        except ApplyError:
            continue
        if run["store"] == str(store) and run["state"] != "done":
            out.append(d)
    return out


def _leftover_backups(backup_root: Path, store: Path) -> list[Path]:
    try:
        return sorted(backup_root.glob(f"{_BACKUP_PREFIX}*-{glob.escape(store.parent.name)}.tmp-*"))
    except OSError:
        return []


def _move(src: Path, dst: Path) -> None:
    if os.path.lexists(dst):
        raise _ItemError(f"{dst} already exists")
    os.rename(src, dst)  # same filesystem: the store and backups both live under ~/.claude
    os.chmod(dst, 0o600)


def _lock_holder(path: Path) -> tuple[int | None, str]:
    """The pid and time a lock file records, or (None, "") when it is unreadable."""
    try:
        text, _ = _read_file(path, 1000)
        pid, _, when = text.strip().partition(" ")
        return (int(pid) if int(pid) > 0 else None), when
    except (StoreError, ValueError):
        return None, ""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, but belongs to someone else
    return True


@contextlib.contextmanager
def _store_lock(backup_root: Path, store: Path, out):
    """One exclusive lock per store: ``apply --apply`` and ``apply --resume`` hold it, so two
    runs never move the same store's files at once. A lock left by a dead pid is taken over."""
    _private_dirs(backup_root)
    path = backup_root / f".memory-consolidate-{store.parent.name}.lock"
    for attempt in (0, 1):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            break
        except FileExistsError:
            pid, when = _lock_holder(path)
            if attempt or pid is None or _pid_alive(pid):
                who = f"pid {pid} since {when}" if pid else "an unreadable lock file; remove it if no run is active"
                raise ApplyError(f"another run holds the lock {path} ({who})") from None
            out(f"WARN stale lock from pid {pid} ({when}); taking it")
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(f"{os.getpid()} {datetime.now().astimezone().isoformat(timespec='seconds')}\n")
    try:
        yield
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class _Run:
    """One apply run. ``data`` is what run.json holds. Every step re-runs from the backup's
    snapshot and reads the filesystem, so a resume is the same code as a fresh run."""

    def __init__(self, data: dict, backup_dir: Path, out):
        self.data = data
        self.dir = backup_dir
        self.out = out

    @property
    def store(self) -> Path:
        return Path(self.data["store"])

    def refuse(self, slug: str, reason: str) -> None:
        self.data["refused"].append(dict(slug=slug, reason=reason))
        self.out(f"REFUSE {slug}: {reason}")

    def save(self, state: str | None = None) -> None:
        if state:
            self.data["state"] = state
        self.data["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _write_json(self.dir / "run.json", self.data)

    def write_notes(self) -> None:
        """Step 1: write each note from the snapshot, or reuse the one already there, and read it back."""
        d = self.data
        vault = Path(d["vault"])
        now = datetime.fromisoformat(d["created_at"])
        items = {i["slug"]: i for i in d["plan"]["items"]}
        refused = {r["slug"] for r in d["refused"]}
        rows = {w["slug"]: w for w in d["written"]}
        retired = list(d["retired"])
        for row in d["planned"]:
            slug = row["slug"]
            if slug in refused:
                continue
            item = items[slug]
            try:
                entry = read_entry(self.dir / "full-copy" / f"{slug}.md")
                if _sha256(entry["text"]) != row["sha256"]:
                    raise _ItemError("changed during the run: the backup copy differs from the text that was checked")
                if item["action"] == "retire":
                    if slug not in retired:
                        retired.append(slug)
                    self.out(f"RETIRE {slug}: {item['reason']}")
                    continue
                grew = _write_note(row, item, entry, vault, d["project"], now, self.store, self.dir)
            except (_ItemError, StoreError, OSError, ApplyError) as exc:
                # An original that already moved goes back in move_all.
                rows.pop(slug, None)
                if slug in retired:
                    retired.remove(slug)
                refused.add(slug)
                self.refuse(slug, str(exc))
                continue
            rows[slug] = dict(slug=slug, action=item["action"], note=row["note"], sha256=row["sha256"])
            self.out(f"{item['action'].upper()} {slug} -> {row['note']}" + (f" (+{grew} chars)" if grew else ""))
        order = [r["slug"] for r in d["planned"]]
        d["written"] = [rows[s] for s in order if s in rows]
        d["retired"] = [s for s in order if s in retired]

    def move_all(self) -> None:
        """Step 2. Each item's status comes from the filesystem, never from the journal."""
        d = self.data
        moved_dir = self.dir / "moved"
        if not moved_dir.exists():
            os.mkdir(moved_dir, 0o700)
        refused = {r["slug"] for r in d["refused"]}
        wanted = {r["slug"]: r["sha256"] for r in d["planned"]}
        slugs = [i["slug"] for i in d["plan"]["items"]]
        for slug in slugs:
            src, dst = self.store / f"{slug}.md", moved_dir / f"{slug}.md"
            in_store, in_moved = os.path.lexists(src), os.path.lexists(dst)
            if slug in refused:
                if in_moved:
                    # Its note failed (or was never written), so its original goes back.
                    if in_store:
                        raise ApplyError(f"{slug} is refused and its original is in {dst}, but the store "
                                         f"path {src} is taken; move one of them by hand, then resume")
                    _move(dst, src)
                    self.out(f"RESTORED {slug} -> {src}")
                continue
            if slug not in wanted:
                continue
            if in_moved:
                if in_store:
                    raise ApplyError(f"{slug} is in both the store and {dst}; move one of them by hand, then resume")
                continue  # moved before an interruption
            try:
                if not in_store:
                    raise _ItemError("original is missing from the store")
                if _sha256(read_entry(src)["text"]) != wanted[slug]:
                    raise _ItemError("original changed after the backup copy; not moved")
                _move(src, dst)
            except (_ItemError, StoreError, OSError) as exc:
                self.refuse(slug, str(exc))
                refused.add(slug)
                continue
            self.out(f"MOVED {slug} -> {dst}")
            _checkpoint("mid-4")
        d["moved"] = [s for s in slugs if s not in refused and os.path.lexists(moved_dir / f"{s}.md")
                      and not os.path.lexists(self.store / f"{s}.md")]

    def pointer_line(self, moved: list[str]) -> str:
        new = sum(1 for w in self.data["written"] if w["action"] == "new" and w["slug"] in moved)
        appended = sum(1 for w in self.data["written"] if w["action"] == "append" and w["slug"] in moved)
        retired = sum(1 for s in self.data["retired"] if s in moved)
        date = self.data["created_at"][:10]
        backup = self.dir.absolute()
        home = Path.home()
        if backup.is_relative_to(home):
            backup = Path("~") / backup.relative_to(home)
        return (f"- Moved to vault ({date}): {len(moved)} entries ({new} new notes tagged {MOVED_TAG}, "
                f"{appended} appended to existing notes, {retired} retired); find them with /vault-ask; "
                f"originals in {backup}")

    def edit_index(self) -> None:
        """Step 3: re-read the live MEMORY.md, drop moved pointers, write atomically."""
        index = self.store / "MEMORY.md"
        lock, err = _acquire_lock(index)
        if err:
            raise ApplyError(err)
        try:
            for _attempt in range(3):
                st = os.lstat(index)  # stat before read: a change after it fails expect_stat
                text, _ = _read_file(index)
                names = {p.name for p in self.store.iterdir()
                         if p.name.endswith(".md") and p.name != "MEMORY.md"}
                moved = list(self.data["moved"])
                names |= {f"{s}.md" for s in moved}
                new_text, dropped, refused, _w = index_edit(text, names, {f"{s}.md" for s in moved},
                                                            self.pointer_line(moved))
                if refused:
                    # A line written since validation now mixes a moved entry
                    # with a staying one. Put those originals back so the index
                    # never points at a moved file.
                    for slug, reason in sorted(refused.items()):
                        try:
                            _move(self.dir / "moved" / f"{slug}.md", self.store / f"{slug}.md")
                        except (_ItemError, OSError) as exc:
                            raise ApplyError(f"cannot move {slug} back: {exc}") from exc
                        self.data["moved"].remove(slug)
                        self.refuse(slug, reason + "; moved back to the store")
                    self.save()
                    continue
                if new_text != text:
                    err = _atomic_rewrite(index, new_text, expect_stat=(st.st_mtime_ns, st.st_size))
                    if err:
                        self.out(f"index changed while writing, retrying: {err}")
                        continue
                self.data["dropped_lines"] = self.data.get("dropped_lines", []) + dropped
                self.data["sizes"]["after"] = _sizes(new_text)
                break
            else:
                raise ApplyError(f"MEMORY.md kept changing; finish with: apply --resume {self.dir}")
        finally:
            _release_lock(lock)
        _checkpoint("index-written")

    def finish(self) -> int:
        """Notes, moves, index, done: the one path for a fresh run and a resume."""
        d = self.data
        if d["state"] == "done":
            self.out("nothing to do: this run is finished")
            return 0
        self.write_notes()
        self.save("notes-written")
        _checkpoint("notes")
        self.move_all()
        self.save("moved")
        _checkpoint("moved")
        self.edit_index()
        self.save("done")
        for key in ("before", "after"):
            if d["sizes"].get(key):
                self.out("index {}: {bytes} bytes, {chars} chars, {lines} lines".format(key, **d["sizes"][key]))
        self.out(f"backup: {self.dir}")
        if not d["moved"]:
            return 2
        return 1 if d["refused"] else 0


def _backup(data: dict, store: Path, backup_root: Path, backup_dir: Path) -> None:
    """Copy the store into a temp sibling, add manifest.json and run.json, then rename it to
    ``backup_dir``. A stop before the rename leaves only a ``.tmp-`` dir; any backup dir that
    exists is complete."""
    _private_dirs(backup_root)
    tmp = Path(tempfile.mkdtemp(dir=backup_root, prefix=backup_dir.name + ".tmp-"))
    full = tmp / "full-copy"
    shutil.copytree(store, full, symlinks=True)
    rows = []
    for root, dirs, names in os.walk(full):
        os.chmod(root, 0o700)
        for name in sorted(dirs + names):
            path = Path(root) / name
            rel = path.relative_to(full).as_posix()
            if os.path.islink(path):
                rows.append(dict(name=rel, kind="symlink", target=os.readlink(path)))
            elif name in names:
                os.chmod(path, 0o600)
                rows.append(dict(name=rel, kind="file", sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    _write_json(tmp / "manifest.json", dict(version=1, files=sorted(rows, key=lambda r: r["name"])))
    data["state"] = "backed-up"
    data["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_json(tmp / "run.json", data)
    if os.path.lexists(backup_dir):
        raise ApplyError(f"backup dir already exists: {backup_dir}")
    os.rename(tmp, backup_dir)


def _prepare(items, store: Path, vault_root: Path, insights_folder: str, project: str, now,
             backup_root: Path, out):
    """Step 1: validate every entry, choose the backup dir, plan each note."""
    data = dict(version=1, state="planned", store=str(store), vault=str(vault_root),
                insights_folder=insights_folder, project=project,
                created_at=now.isoformat(timespec="seconds"),
                plan=dict(version=1, store=str(store), items=items),
                refused=[], warnings=[], planned=[], written=[], retired=[], moved=[], dropped_lines=[])
    refused, warnings = {}, {}
    entries = {}
    index_text, _ = _read_file(store / "MEMORY.md")
    for item in items:
        slug = item["slug"]
        try:
            entry = read_entry(store / f"{slug}.md")
        except StoreError as exc:
            refused[slug] = str(exc)
            continue
        if scrub_secrets(entry["text"]) != entry["text"]:
            refused[slug] = "text holds a secret-shaped string (scrub_secrets would change it)"
        elif scrub_secrets(_title(index_text, entry)) != _title(index_text, entry):
            # The title can come from the MEMORY.md link label, which the text check never sees.
            refused[slug] = "index label holds a secret-shaped string (scrub_secrets would change it)"
        elif _END_PREFIX in entry["body"]:
            refused[slug] = "body contains the end marker text"
        else:
            entries[slug] = entry
    baseline = audit(store)
    data["baseline"] = dict(indexed=[e["slug"] for e in baseline["entries"] if e["indexed"]],
                            dangling=baseline["dangling"], duplicates=baseline["duplicates"],
                            orphans=baseline["orphans"])
    data["sizes"] = dict(before=_sizes(index_text), after=None)
    # Plan each note. A marker already present means an earlier run wrote it.
    folder = vault_root / insights_folder
    markers = _scan_markers(folder) if any(i["action"] == "new" for i in items) else {}
    plans = []
    for item in items:
        slug = item["slug"]
        if slug not in entries:
            continue
        entry = entries[slug]
        title = _title(index_text, entry)
        try:
            if item["action"] == "new":
                # A same-slug note from another project is not this run's note.
                any_project = markers.get(slug, [])
                hits = [h for h in any_project if _marker_project(h) == project]
                if len(hits) > 1:
                    raise _ItemError("more than one note carries its marker: "
                                     + ", ".join(str(h.relative_to(vault_root)) for h in hits))
                if hits:
                    plans.append(dict(item=item, entry=entry, title=title, dest=hits[0], reuse=True))
                else:
                    name = (f"{now.strftime('%Y-%m-%d')}-{slugify(title, 50)}-"
                            f"{hashlib.sha1(slug.encode()).hexdigest()[:4]}.md")
                    dest = _contained(folder / name, vault_root)
                    if dest in any_project:
                        # Another project's note holds this name: key the name on the project too.
                        name = (f"{now.strftime('%Y-%m-%d')}-{slugify(title, 50)}-"
                                f"{hashlib.sha1(f'{project}/{slug}'.encode()).hexdigest()[:4]}.md")
                        dest = _contained(folder / name, vault_root)
                    if os.path.lexists(dest):
                        raise _ItemError(f"{dest.relative_to(vault_root)} already exists and has no marker")
                    plans.append(dict(item=item, entry=entry, title=title, dest=dest, reuse=False))
            elif item["action"] == "append":
                errs = _target_errors(item["target"], vault_root)
                if errs:
                    raise _ItemError("; ".join(errs))
                dest = _contained(vault_root / item["target"], vault_root)
                text, _ = _read_file(dest)
                count = text.count(_START.format(slug=slug))
                if count > 1:
                    raise _ItemError(f"{item['target']} carries its marker {count} times")
                plans.append(dict(item=item, entry=entry, title=title, dest=dest, reuse=count == 1))
            else:
                plans.append(dict(item=item, entry=entry, title=title, dest=None, reuse=False))
        except (_ItemError, StoreError, ValueError, OSError) as exc:
            refused[slug] = str(exc)
            continue

    # Index rule, repeated until stable: a slug refused above stays, which can
    # make a line it shares with a moving slug mixed.
    names = {e["slug"] + ".md" for e in baseline["entries"]}
    while True:
        moving = {p["item"]["slug"] + ".md" for p in plans}
        _, dropped, idx_refused, idx_warnings = index_edit(index_text, names, moving, "")
        if not idx_refused:
            break
        refused.update(idx_refused)
        plans = [p for p in plans if p["item"]["slug"] not in idx_refused]
    warnings.update(idx_warnings)
    for e in baseline["entries"]:
        if e["slug"] + ".md" in moving:
            continue
        try:
            other, _ = _read_file(store / f"{e['slug']}.md")
        except StoreError:
            continue
        for name in _link_targets(other) & moving:
            warnings.setdefault(Path(name).stem, f"mentioned in {e['slug']}.md")

    # The backup dir name is fixed before any note is written, because each note names it.
    backup_dir = backup_root / f"{_BACKUP_PREFIX}{now.strftime('%Y%m%d-%H%M%S')}-{store.parent.name}"
    data["backup_dir"] = str(backup_dir)
    # What the notes are written from, and what each move must still find: one hash per entry.
    data["planned"] = [dict(slug=p["item"]["slug"], action=p["item"]["action"], title=p["title"],
                            note=str(p["dest"].relative_to(vault_root)) if p["dest"] else None,
                            sha256=_sha256(p["entry"]["text"])) for p in plans]
    data["refused"] = [dict(slug=s, reason=r) for s, r in refused.items()]
    data["warnings"] = [dict(slug=s, reason=r) for s, r in sorted(warnings.items())]
    data["dropped_lines"] = []
    return data, plans, backup_dir, dropped, index_text


def _write_note(row, item, entry, vault_root: Path, project: str, now, store: Path, backup_dir: Path) -> int:
    """Write (or find) one note from the snapshot ``entry`` and read it back; return the chars added.
    Idempotent: a note that is already there is only read back."""
    dest = _contained(vault_root / row["note"], vault_root)
    if item["action"] == "new":
        if not os.path.lexists(dest):
            content = _render_new(item, entry, row["title"], project, now, store, backup_dir)
            _private_dirs(dest.parent)
            err = write_vault_note(str(vault_root), str(dest.parent.relative_to(vault_root)),
                                   dest.name, content)
            if err:
                raise _ItemError(err)
        _check_readback(dest, entry, "new")
        return 0
    try:
        text, _ = _read_file(dest)
    except StoreError as exc:
        raise _ItemError(f"append target is missing or unreadable: {exc}") from exc
    count = text.count(_START.format(slug=entry["slug"]))
    if count > 1:
        raise _ItemError(f"{row['note']} carries its marker {count} times")
    if count == 1:
        _check_readback(dest, entry, "append")
        return 0
    section = _render_section(entry, row["title"], now, backup_dir)
    lock, err = _acquire_lock(dest)
    if err:
        raise _ItemError(err)
    try:
        st = os.lstat(dest)  # stat before read: a change after it fails expect_stat
        text, _ = _read_file(dest)
        if _START.format(slug=entry["slug"]) in text:
            raise _ItemError(f"{dest} gained its marker during the run")
        sep = "" if text.endswith("\n\n") or not text else "\n" if text.endswith("\n") else "\n\n"
        err = _atomic_rewrite(dest, text + sep + section, expect_stat=(st.st_mtime_ns, st.st_size))
        if err:
            raise _ItemError(err)
    finally:
        _release_lock(lock)
    _check_readback(dest, entry, "append", prefix=text)
    return len(sep + section)


def apply(plan_path: Path, store: Path, vault_root: Path, insights_folder: str, project: str,
          backup_root: Path, do_apply: bool, now: datetime, out=print) -> int:
    """Dry run (default) or ``--apply``, following the plan's "Write order"."""
    if os.path.islink(backup_root):
        raise ApplyError(f"backup root is a symlink: {backup_root}")
    args = (plan_path, store, vault_root.resolve(), insights_folder, project, backup_root, do_apply, now, out)
    if not do_apply:
        return _apply(*args)  # a dry run writes nothing, so it takes no lock
    with _store_lock(backup_root, store, out):
        return _apply(*args)


def _apply(plan_path, store, vault_root, insights_folder, project, backup_root, do_apply, now, out) -> int:
    items, errors = load_plan(plan_path, store, vault_root)
    if errors:
        raise ApplyError("bad plan: " + "; ".join(errors))
    for leftover in _leftover_backups(backup_root, store):
        out(f"WARN leftover partial backup {leftover}")
    unfinished = _unfinished_runs(backup_root, store)
    if unfinished:
        raise ApplyError(f"unfinished run for this store; finish it with: apply --resume {unfinished[0]}")
    data, plans, backup_dir, dropped, _ = _prepare(items, store, vault_root, insights_folder,
                                                   project, now, backup_root, out)
    if os.path.lexists(backup_dir):
        raise ApplyError(f"backup dir already exists: {backup_dir}")
    out(f"backup: {backup_dir}")
    for r in data["refused"]:
        out(f"REFUSE {r['slug']}: {r['reason']}")
    for w in data["warnings"]:
        out(f"WARN {w['slug']}: {w['reason']}")
    if not do_apply:
        for plan in plans:
            item, slug = plan["item"], plan["item"]["slug"]
            if item["action"] == "retire":
                out(f"RETIRE {slug}: {item['reason']}")
                continue
            rel = plan["dest"].relative_to(vault_root)
            if plan["reuse"]:
                out(f"{item['action'].upper()} {slug} -> {rel} (already written; read back only)")
            elif item["action"] == "new":
                out(f"NEW {slug} -> {rel}")
            else:
                size = len(_render_section(plan["entry"], plan["title"], now, backup_dir)) + 2
                out(f"APPEND {slug} -> {rel} (+{size} chars)")
        # The preview drops only lines whose entries all still move.
        for line in dropped:
            out(f"DROP {line}")
        out("dry run: nothing written; add --apply to write")
        if not plans:
            return 2
        return 1 if data["refused"] else 0
    if not plans:
        out("nothing to move")
        return 2

    try:
        _backup(data, store, backup_root, backup_dir)
        _checkpoint("backup")
        return _Run(data, backup_dir, out).finish()
    except OSError as exc:
        exc.backup_dir = backup_dir  # lets _main_apply print the resume hint once run.json exists
        raise


def resume(backup_dir: Path, vault_arg: str | None, insights_arg: str | None, out=print) -> int:
    """Run the same steps again from the backup's snapshot; each one is idempotent."""
    backup_dir = Path(backup_dir)
    data = _read_run(backup_dir)
    store = resolve_store(data["store"], os.getcwd())
    out(f"store: {store}")
    if str(store) != data["store"]:
        raise ApplyError(f"run.json store {data['store']} is not {store}")
    vault_root = Path(data["vault"])
    if vault_arg is not None and Path(vault_arg).resolve() != vault_root.resolve():
        raise ApplyError(f"--vault {vault_arg} is not the run's vault {vault_root}")
    if insights_arg is not None and insights_arg != data["insights_folder"]:
        raise ApplyError(f"--insights-folder {insights_arg} is not the run's {data['insights_folder']}")
    with _store_lock(backup_dir.parent, store, out):
        data = _read_run(backup_dir)  # another run may have finished it before we got the lock
        _, errors = validate_plan(data["plan"], store, vault_root, moved_dir=backup_dir / "moved")
        if errors:
            raise ApplyError("bad plan in run.json: " + "; ".join(errors))
        out(f"resuming from state: {data['state']}")
        return _Run(data, backup_dir, out).finish()


def _default_project(store: Path, cwd: str) -> str:
    """The project label for ``store``: the cwd's own when the cwd resolves to this store, else
    the cwd a transcript in the store's project folder recorded. Never the caller's cwd by default."""
    try:
        if resolve_store(None, cwd) == store:
            return canonical_project_name(cwd)
    except StoreError:
        pass
    try:
        logs = sorted(store.parent.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    except OSError:
        logs = []
    for log in logs:
        recorded = _transcript_cwd(str(log))
        if recorded:
            return canonical_project_name(recorded)
    raise ApplyError(f"cannot tell which project {store} belongs to; pass --project")


def _main_apply(args) -> int:
    try:
        if args.resume:
            if args.plan or args.store or args.apply or args.project or args.backup_root:
                raise ApplyError("--resume takes only --vault and --insights-folder")
            return resume(Path(args.resume), args.vault, args.insights_folder)
        store = resolve_store(args.store, os.getcwd())
        print(f"store: {store}")
        if args.vault is None or args.insights_folder is None:
            config = load_config(fresh=True)
        vault = args.vault if args.vault is not None else config.get("vault_path", "")
        folder = args.insights_folder if args.insights_folder is not None else config["insights_folder"]
        if not vault or not Path(vault).is_dir():
            raise ApplyError(f"vault is not a directory: {vault!r}; pass --vault")
        if not folder or Path(folder).is_absolute() or ".." in Path(folder).parts:
            raise ApplyError(f"insights folder must be vault-relative: {folder!r}")
        project = args.project or _default_project(store, os.getcwd())
        backup_root = Path(args.backup_root or Path.home() / ".claude" / "backups")
        return apply(Path(args.plan), store, Path(vault), folder, project, backup_root,
                     args.apply, datetime.now().astimezone())
    except (StoreError, ApplyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        # Disk full, permissions: exit 2, never the "some items refused" exit 1.
        print(f"error: {exc}", file=sys.stderr)
        where = Path(args.resume) if args.resume else getattr(exc, "backup_dir", None)
        if where is not None and (Path(where) / "run.json").is_file():
            print(f"finish with: apply --resume {where}", file=sys.stderr)
        return 2


def verify(backup_dir: Path, store: Path | None = None) -> tuple[bool, list[dict]]:
    return _verify_run(backup_dir, _read_run(backup_dir), store)


def _verify_run(backup_dir: Path, run: dict, store: Path | None) -> tuple[bool, list[dict]]:
    live = Path(run["store"])
    if store is not None and store.resolve() != live.resolve():
        raise ApplyError(f"run.json is for {live}")
    if run["state"] != "done":
        return False, [dict(check=VERIFY_CHECKS[0], ok=False, detail=run["state"])]

    checks = []

    def record(name, issues, detail=None):
        assert name in VERIFY_CHECKS, name
        checks.append(dict(check=name, ok=not issues, detail=detail or ", ".join(sorted(issues)) or "ok"))

    full = backup_dir / "full-copy"
    moved_dir = backup_dir / "moved"
    moved = set(run["moved"])
    baseline = run["baseline"]
    text, _ = _read_file(live / "MEMORY.md")
    names = {p.name for p in live.glob("*.md") if p.name != "MEMORY.md" and p.is_file()}
    moved_names = {f"{slug}.md" for slug in moved}
    pointers, referenced = _index_references(text.splitlines(), names | moved_names)
    dangling = _md_child_link_targets(text) - names - {"MEMORY.md"}
    record("index-links-resolve", dangling.difference(baseline["dangling"]))
    duplicates = {Path(name).stem for name, count in pointers.items() if count > 1}
    record("no-duplicates", duplicates.difference(baseline["duplicates"]))
    orphans = {Path(name).stem for name in names - referenced}
    record("all-indexed", orphans.difference(baseline["orphans"]))

    snapshot = full / "MEMORY.md"
    before = None
    if snapshot.is_file():
        before, _ = _read_file(snapshot)
        old_names = {p.name for p in full.glob("*.md") if p.name != "MEMORY.md" and p.is_file()}
        _, old = _index_references(before.splitlines(), old_names)
        removed = {Path(name).stem for name in old - referenced}
        expected = moved & {Path(name).stem for name in old}
        detail = f"unexpected: {sorted(removed - expected)}; missing: {sorted(expected - removed)}"
        record("removed-equals-moved", [] if removed == expected else ["differs"], detail)
    else:
        record("removed-equals-moved", ["missing full-copy/MEMORY.md"])

    vault = Path(run["vault"])
    issues = []
    for row in run["written"]:
        slug, action = row["slug"], row["action"]
        try:
            note = _contained(vault / row["note"], vault)
            note_text, _ = _read_file(note)
            # The note was written from the snapshot; a kept original may have been edited since.
            original = read_entry(_contained((moved_dir if slug in moved else full) / f"{slug}.md", backup_dir))
            _check_note_text(note, note_text, original, action)
            if action == "new":
                front = _frontmatter(note_text)
                markers = re.findall(r"^migrated_from_memory:([^\n]*)$", front, re.M)
                if slug not in map(_unquote, markers):
                    issues.append(f"{slug}: missing migrated_from_memory in frontmatter")
        except (_ItemError, StoreError, ValueError) as exc:
            issues.append(f"{slug}: {exc}")
    record("notes-lossless", issues)

    missing = [f"missing moved/{name}" for name in moved_names if not (moved_dir / name).is_file()]
    remaining = [f"still in store: {name}" for name in moved_names if os.path.lexists(live / name)]
    if before is None:
        missing.append("missing full-copy/MEMORY.md")
    damaged = []
    try:
        raw, _ = _read_file(backup_dir / "manifest.json", _MANIFEST_LIMIT)
        listed = json.loads(raw)["files"]
    except StoreError as exc:
        if (backup_dir / "manifest.json").exists():
            raise ApplyError(f"cannot read manifest.json: {exc}") from exc
        listed = None
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        raise ApplyError(f"cannot read manifest.json: {exc}") from exc
    if listed is None:
        print("WARN backups-present: manifest.json is missing (written before the manifest existed); "
              "only full-copy/MEMORY.md and moved/ were checked")
    else:
        for row in listed:
            path = full / row["name"]
            try:
                _contained(path.parent, full)
                if row["kind"] == "symlink":
                    if not os.path.lexists(path):
                        raise FileNotFoundError(path)
                    same = os.path.islink(path) and os.readlink(path) == row["target"]
                else:
                    same = hashlib.sha256(_contained(path, full).read_bytes()).hexdigest() == row["sha256"]
            except (OSError, ValueError, _ItemError):
                damaged.append(f"missing full-copy/{row['name']}")
                continue
            if not same:
                damaged.append(f"changed full-copy/{row['name']}")
    record("backups-present", missing + remaining + damaged)

    units = "{bytes} bytes, {chars} chars, {lines} lines"
    before_size = units.format(**_sizes(before)) if before is not None else "unavailable"
    detail = f"before: {before_size}; after: {units.format(**_sizes(text))}"
    record("sizes", [], detail)
    return all(check["ok"] for check in checks), checks


def smoke(vault_root: Path, config: dict, db_path: str, query: str) -> list[dict]:
    vault_index.ensure_index(str(vault_root), indexed_folders(config), db_path)
    return vault_index.search_vault(db_path, query, log_access=False)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"error: {message}\n")


def main(argv=None) -> int:
    parser = _ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    audit_parser = commands.add_parser("audit", help="report memory index limits and drift", allow_abbrev=False)
    audit_parser.add_argument("--store")
    audit_parser.add_argument("--json", action="store_true")
    apply_parser = commands.add_parser("apply", help="dry run (default) or apply a move plan", allow_abbrev=False)
    source = apply_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan")
    source.add_argument("--resume", metavar="DIR")
    apply_parser.add_argument("--store")
    apply_parser.add_argument("--apply", action="store_true")
    apply_parser.add_argument("--vault")
    apply_parser.add_argument("--insights-folder")
    apply_parser.add_argument("--project")
    apply_parser.add_argument("--backup-root")
    verify_parser = commands.add_parser("verify", help="check a finished consolidation run", allow_abbrev=False)
    verify_parser.add_argument("--backup", required=True, type=Path)
    verify_parser.add_argument("--store", type=Path)
    smoke_parser = commands.add_parser("smoke", help="reindex the vault and search for a moved note", allow_abbrev=False)
    smoke_parser.add_argument("--query", required=True)
    smoke_parser.add_argument("--expect", metavar="NOTE", action="append", type=PurePosixPath, default=[],
                              help="require this vault-relative note among the hits (repeatable)")
    smoke_parser.add_argument("--vault")
    smoke_parser.add_argument("--db")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if args.command == "apply":
        return _main_apply(args)
    if args.command == "verify":
        try:
            run = _read_run(args.backup)
            print(f"store: {run['store']}")
            ok, checks = _verify_run(args.backup, run, args.store)
            for check in checks:
                print(f"{'PASS' if check['ok'] else 'FAIL'} {check['check']}: {check['detail']}")
            print("verify: ok" if ok else "verify: FAILED")
            return int(not ok)
        except (ApplyError, StoreError, OSError, KeyError, TypeError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "smoke":
        try:
            config = load_config(fresh=True)
            vault = config.get("vault_path") if args.vault is None else args.vault
            if not vault or not Path(vault).is_dir():
                raise ApplyError(f"vault is not a directory: {vault!r}")
            db = str(Path(args.db).resolve()) if args.db is not None else vault_index._default_db_path()
            hits = smoke(Path(vault), config, db, args.query)
            paths = [PurePosixPath(os.path.relpath(hit["path"], vault)) for hit in hits]
            print(f"smoke: {len(hits)} hit(s) for {args.query}")
            for path in paths[:15]:
                print(f"HIT {path}")
            missing = [path for path in args.expect if path not in paths]
            for path in missing:
                print(f"MISSING {path}")
            return int(not hits or bool(missing))
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        store = resolve_store(args.store, os.getcwd())
        print(f"store: {store}")
        report = audit(store)
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        index = report["index"]
        print("index: {bytes} bytes, {chars} chars, {lines} lines".format(**index))
        print("limits: {soft_limit_bytes} soft bytes, {hard_limit_bytes} hard bytes, "
              "{line_limit} lines".format(**index))
        print("over:", ", ".join(index["over"]) or "none")
        print("mode:", report["mode"])
        # Counts, plus the names that need action; --json keeps every entry.
        entries = report["entries"]
        print(f"entries: {len(entries)} ({sum(e['indexed'] for e in entries)} indexed)")
        for key in ("orphans", "dangling", "duplicates"):
            print(f"{key} ({len(report[key])}): {', '.join(report[key])}")
    return int(report["mode"] != "ok" or bool(report["dangling"] or report["duplicates"]))


if __name__ == "__main__":
    sys.exit(main())
