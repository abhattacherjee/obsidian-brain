"""Project memory consolidation: audit the index, validate a move plan, apply it."""

from __future__ import annotations

import argparse
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
from obsidian_utils import (_cc_encoded_dirnames, canonical_project_name, indexed_folders, load_config, scrub_secrets,
                            slugify, write_vault_note)


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


def _read_file(path: Path) -> tuple[str, float]:
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
            text = stream.read(2_000_001)
        if len(text) > 2_000_000:
            raise StoreError(f"{path} exceeds 2,000,000 characters")
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


def read_entry(path: Path) -> dict:
    text, mtime = _read_file(path)
    fields = {}
    body = text.strip()
    lines = text.splitlines(keepends=True)
    if lines and lines[0].rstrip("\r\n") == "---":
        metadata = False
        for i, raw in enumerate(lines[1:], 1):
            line = raw.rstrip("\r\n")
            if line == "---":
                body = "".join(lines[i + 1:]).strip()
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


def _target_errors(target, vault_root: Path) -> list[str]:
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
        if not p.is_file():
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
        if not valid_slug or slug == "MEMORY":
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
            issues.extend(_target_errors(item.get("target"), vault_root))
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
RUN_STATES = ("notes-written", "moved", "done")
_BACKUP_PREFIX = "memory-moved-"
# The start marker is the one a rerun searches for (plan "Markers"). The end
# marker bounds the copied body, so the lossless check still works after a
# later append to the same note adds sections below it.
_START = "<!-- migrated_from_memory: {slug} -->"
_END = "<!-- /migrated_from_memory: {slug} -->"
_END_PREFIX = "<!-- /migrated_from_memory:"
_PROVENANCE_RE = re.compile(r"originals? (?:at|in) `([^`\n]{1,2000})`")


class ApplyError(Exception):
    """The run cannot go on (exit 2)."""


class _ItemError(Exception):
    """One item is refused; the rest of the run goes on."""


def _checkpoint(name: str) -> None:
    """Test hook: the stop-point tests raise here to simulate a crash."""


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
    title = (m.group(1) if m else "") or entry["name"] or entry["description"] or slug
    return " ".join(title.split())


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
        f"\n\n## Details\n\n{entry['body'].strip()}\n\n{_END.format(slug=slug)}\n")


def _render_section(entry, title, now, backup_dir) -> str:
    slug, date = entry["slug"], now.strftime("%Y-%m-%d")
    return (f"## From project memory: {title} (moved {date})\n{_START.format(slug=slug)}\n\n"
            f"{_provenance(slug, date, backup_dir)}\n\n### Summary\n\n{_summary(entry)}\n\n"
            f"### Details\n\n{entry['body'].strip()}\n\n{_END.format(slug=slug)}\n")


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


def _frontmatter(text: str) -> str:
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return ""
    for i, line in enumerate(lines[1:], 1):
        if line.rstrip("\r") == "---":
            return "\n".join(lines[1:i])
    return ""


def _check_readback(path: Path, entry: dict, action: str, prefix: str | None = None) -> None:
    """Lossless check: the copied text must equal the memory body exactly."""
    try:
        text, _ = _read_file(path)
    except StoreError as exc:
        raise _ItemError(f"read-back failed: {exc}") from exc
    _check_note_text(path, text, entry, action, prefix)


def _check_note_text(path: Path, text: str, entry: dict, action: str, prefix: str | None = None) -> None:
    details = _details(text, entry["slug"], action)
    if details is None:
        raise _ItemError(f"read-back failed: markers missing in {path}")
    if details.strip() != entry["body"].strip():
        raise _ItemError(f"read-back failed: copied text differs from the memory body in {path}")
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
    keys = ("version", "state", "store", "vault", "insights_folder", "plan", "refused", "written",
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
        try:
            run = _read_run(d)
        except ApplyError:
            continue
        if run["store"] == str(store) and run["state"] != "done":
            out.append(d)
    return out


def _move(src: Path, dst: Path) -> None:
    if os.path.lexists(dst):
        raise _ItemError(f"{dst} already exists")
    os.rename(src, dst)  # same filesystem: the store and backups both live under ~/.claude
    os.chmod(dst, 0o600)


class _Run:
    """One apply run. ``data`` is what run.json holds."""

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

    def original(self, slug: str) -> dict:
        """The entry as it was at backup time (full-copy/), else in the store."""
        for p in (self.dir / "full-copy" / f"{slug}.md", self.store / f"{slug}.md",
                  self.dir / "moved" / f"{slug}.md"):
            if os.path.lexists(p):
                return read_entry(p)
        raise _ItemError("original not found in full-copy/, the store or moved/")

    def move_all(self) -> None:
        """Step 4. An original that is already in moved/ counts as moved."""
        moved_dir = self.dir / "moved"
        if not moved_dir.exists():
            os.mkdir(moved_dir, 0o700)
        done = set(self.data["moved"])
        slugs = [w["slug"] for w in self.data["written"]] + list(self.data["retired"])
        for slug in slugs:
            if slug in done:
                continue
            src, dst = self.store / f"{slug}.md", moved_dir / f"{slug}.md"
            try:
                if not os.path.lexists(src):
                    if os.path.lexists(dst):
                        pass  # moved before an interruption
                    else:
                        raise _ItemError("original is missing from the store")
                else:
                    snapshot = self.dir / "full-copy" / f"{slug}.md"
                    if read_entry(src)["text"] != read_entry(snapshot)["text"]:
                        raise _ItemError("original changed after the notes were written; not moved")
                    _move(src, dst)
            except (_ItemError, StoreError, OSError) as exc:
                self.refuse(slug, str(exc))
                continue
            self.data["moved"].append(slug)
            done.add(slug)
            self.save()
            self.out(f"MOVED {slug} -> {dst}")
            _checkpoint("mid-4")
        self.save("moved")

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
        """Step 5: re-read the live MEMORY.md, drop moved pointers, write atomically."""
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
        self.save("done")

    def finish(self) -> int:
        if self.data["state"] == "notes-written":
            self.move_all()
            _checkpoint("moved")
        if self.data["state"] == "moved":
            self.edit_index()
        sizes = self.data["sizes"]
        for key in ("before", "after"):
            if sizes.get(key):
                self.out("index {}: {bytes} bytes, {chars} chars, {lines} lines".format(key, **sizes[key]))
        self.out(f"backup: {self.dir}")
        if not self.data["moved"]:
            return 2
        return 1 if self.data["refused"] else 0


def _prepare(items, store: Path, vault_root: Path, insights_folder: str, project: str, now,
             backup_root: Path, out):
    """Step 1: validate every entry, choose the backup dir, plan each note."""
    data = dict(version=1, state="planned", store=str(store), vault=str(vault_root),
                insights_folder=insights_folder, project=project,
                created_at=now.isoformat(timespec="seconds"),
                plan=dict(version=1, store=str(store), items=items),
                refused=[], warnings=[], written=[], retired=[], moved=[], dropped_lines=[])
    refused, warnings = {}, {}
    entries = {}
    for item in items:
        slug = item["slug"]
        try:
            entry = read_entry(store / f"{slug}.md")
        except StoreError as exc:
            refused[slug] = str(exc)
            continue
        if scrub_secrets(entry["text"]) != entry["text"]:
            refused[slug] = "text holds a secret-shaped string (scrub_secrets would change it)"
        elif _END_PREFIX in entry["body"]:
            refused[slug] = "body contains the end marker text"
        else:
            entries[slug] = entry
    index_text, _ = _read_file(store / "MEMORY.md")
    baseline = audit(store)
    data["baseline"] = dict(indexed=[e["slug"] for e in baseline["entries"] if e["indexed"]],
                            dangling=baseline["dangling"], duplicates=baseline["duplicates"],
                            orphans=baseline["orphans"])
    data["sizes"] = dict(before=_sizes(index_text), after=None)
    # Plan each note. A marker already present means an earlier run wrote it.
    folder = vault_root / insights_folder
    markers = _scan_markers(folder) if any(i["action"] == "new" for i in items) else {}
    plans = []
    reused_dirs = set()
    for item in items:
        slug = item["slug"]
        if slug not in entries:
            continue
        entry = entries[slug]
        title = _title(index_text, entry)
        try:
            if item["action"] == "new":
                hits = markers.get(slug, [])
                if len(hits) > 1:
                    raise _ItemError("more than one note carries its marker: "
                                     + ", ".join(str(h.relative_to(vault_root)) for h in hits))
                if hits:
                    plans.append(dict(item=item, entry=entry, title=title, dest=hits[0], reuse=True))
                else:
                    name = (f"{now.strftime('%Y-%m-%d')}-{slugify(title, 50)}-"
                            f"{hashlib.sha1(slug.encode()).hexdigest()[:4]}.md")
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
        if plans[-1]["reuse"]:
            text, _ = _read_file(plans[-1]["dest"])
            m = _PROVENANCE_RE.search(text[text.find(_START.format(slug=slug)):]
                                      if item["action"] == "append" else text)
            if m:
                reused_dirs.add(m.group(1))

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

    # The backup dir name is fixed before any note is written, because each
    # note names it. A rerun after a stop at step 2 reuses the name its notes
    # already carry, when that dir was never created.
    backup_dir = backup_root / f"{_BACKUP_PREFIX}{now.strftime('%Y%m%d-%H%M%S')}-{store.parent.name}"
    if len(reused_dirs) == 1:
        earlier = Path(reused_dirs.pop())
        if earlier.parent == backup_root and earlier.name.startswith(_BACKUP_PREFIX) and not os.path.lexists(earlier):
            backup_dir = earlier
    data["backup_dir"] = str(backup_dir)
    data["refused"] = [dict(slug=s, reason=r) for s, r in refused.items()]
    data["warnings"] = [dict(slug=s, reason=r) for s, r in sorted(warnings.items())]
    data["dropped_lines"] = []
    return data, plans, backup_dir, dropped, index_text


def _write_note(plan, vault_root: Path, insights_folder: str, project: str, now, store: Path,
                backup_dir: Path) -> tuple[str, int]:
    """Step 2 for one item: write (or find) its note and read it back."""
    item, entry, dest = plan["item"], plan["entry"], plan["dest"]
    if item["action"] == "new":
        if not plan["reuse"]:
            content = _render_new(item, entry, plan["title"], project, now, store, backup_dir)
            _private_dirs(dest.parent)
            # write_vault_note overwrites, so refuse an existing file right before it.
            if os.path.lexists(dest):
                raise _ItemError(f"{dest} appeared during the run")
            err = write_vault_note(str(vault_root), str(dest.parent.relative_to(vault_root)),
                                   dest.name, content)
            if err:
                raise _ItemError(err)
        _check_readback(dest, entry, "new")
        return str(dest.relative_to(vault_root)), 0
    _contained(dest, vault_root)
    if plan["reuse"]:
        _check_readback(dest, entry, "append")
        return str(dest.relative_to(vault_root)), 0
    section = _render_section(entry, plan["title"], now, backup_dir)
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
    return str(dest.relative_to(vault_root)), len(sep + section)


def apply(plan_path: Path, store: Path, vault_root: Path, insights_folder: str, project: str,
          backup_root: Path, do_apply: bool, now: datetime, out=print) -> int:
    """Dry run (default) or ``--apply``, following the plan's "Write order"."""
    vault_root = vault_root.resolve()
    if os.path.islink(backup_root):
        raise ApplyError(f"backup root is a symlink: {backup_root}")
    items, errors = load_plan(plan_path, store, vault_root)
    if errors:
        raise ApplyError("bad plan: " + "; ".join(errors))
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

    # Step 2: write every note and read it back. A failed item is refused and
    # its original stays in the store.
    for plan in plans:
        item, slug = plan["item"], plan["item"]["slug"]
        if item["action"] == "retire":
            data["retired"].append(slug)
            out(f"RETIRE {slug}: {item['reason']}")
            continue
        try:
            note, grew = _write_note(plan, vault_root, insights_folder, project, now, store, backup_dir)
        except (_ItemError, StoreError, OSError, ApplyError) as exc:
            data["refused"].append(dict(slug=slug, reason=str(exc)))
            out(f"REFUSE {slug}: {exc}")
            continue
        data["written"].append(dict(slug=slug, action=item["action"], note=note))
        out(f"{item['action'].upper()} {slug} -> {note}" + (f" (+{grew} chars)" if grew else ""))
    _checkpoint("notes")
    if not data["written"] and not data["retired"]:
        out("nothing to move")
        return 2

    # Step 3: back up the whole store, then record the run.
    if os.path.lexists(backup_dir):
        raise ApplyError(f"backup dir already exists: {backup_dir}")
    _private_dirs(backup_dir)
    full = backup_dir / "full-copy"
    shutil.copytree(store, full, symlinks=True)
    for root, dirs, files in os.walk(full):
        os.chmod(root, 0o700)
        for name in files:
            if not os.path.islink(os.path.join(root, name)):
                os.chmod(os.path.join(root, name), 0o600)
    run = _Run(data, backup_dir, out)
    run.save("notes-written")
    _checkpoint("backup")
    return run.finish()


def resume(backup_dir: Path, vault_arg: str | None, insights_arg: str | None, out=print) -> int:
    """Finish an interrupted run from the state its run.json records."""
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
    _, errors = validate_plan(data["plan"], store, vault_root, moved_dir=backup_dir / "moved")
    if errors:
        raise ApplyError("bad plan in run.json: " + "; ".join(errors))
    run = _Run(data, backup_dir, out)
    out(f"resuming from state: {data['state']}")
    if data["state"] == "notes-written":
        # Re-check each note before its original moves.
        for w in list(data["written"]):
            try:
                _check_readback(vault_root / w["note"], run.original(w["slug"]), w["action"])
                _contained(vault_root / w["note"], vault_root)
            except (_ItemError, StoreError, ValueError) as exc:
                data["written"].remove(w)
                run.refuse(w["slug"], str(exc))
        run.save()
    return run.finish()


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
        project = args.project or canonical_project_name(os.getcwd())
        backup_root = Path(args.backup_root or Path.home() / ".claude" / "backups")
        return apply(Path(args.plan), store, Path(vault), folder, project, backup_root,
                     args.apply, datetime.now().astimezone())
    except (StoreError, ApplyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def verify(backup_dir: Path, store: Path | None = None) -> tuple[bool, list[dict]]:
    return _verify_run(backup_dir, _read_run(backup_dir), store)


def _verify_run(backup_dir: Path, run: dict, store: Path | None) -> tuple[bool, list[dict]]:
    live = Path(run["store"])
    if store is not None and store.resolve() != live.resolve():
        raise ApplyError(f"run.json is for {live}")
    if run["state"] != "done":
        return False, [dict(check="state", ok=False, detail=run["state"])]

    checks = []

    def record(name, issues):
        checks.append(dict(check=name, ok=not issues, detail=", ".join(sorted(issues)) or "ok"))

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
        checks.append(dict(check="removed-equals-moved", ok=removed == expected, detail=detail))
    else:
        record("removed-equals-moved", ["missing full-copy/MEMORY.md"])

    vault = Path(run["vault"])
    issues = []
    for row in run["written"]:
        slug, action = row["slug"], row["action"]
        try:
            note = _contained(vault / row["note"], vault)
            note_text, _ = _read_file(note)
            original = read_entry(_contained(moved_dir / f"{slug}.md", backup_dir))
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
    record("backups-present", missing + remaining)

    units = "{bytes} bytes, {chars} chars, {lines} lines"
    before_size = units.format(**_sizes(before)) if before is not None else "unavailable"
    detail = f"before: {before_size}; after: {units.format(**_sizes(text))}"
    checks.append(dict(check="sizes", ok=True, detail=detail))
    return all(check["ok"] for check in checks), checks


def smoke(vault_root: Path, config: dict, db_path: str, query: str) -> list[dict]:
    vault_index.ensure_index(str(vault_root), indexed_folders(config), db_path)
    return vault_index.search_vault(db_path, query, log_access=False)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"error: {message}\n")


def main(argv=None) -> int:
    parser = _ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    audit_parser = commands.add_parser("audit", help="report memory index limits and drift")
    audit_parser.add_argument("--store")
    audit_parser.add_argument("--json", action="store_true")
    apply_parser = commands.add_parser("apply", help="dry run (default) or apply a move plan")
    source = apply_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan")
    source.add_argument("--resume", metavar="DIR")
    apply_parser.add_argument("--store")
    apply_parser.add_argument("--apply", action="store_true")
    apply_parser.add_argument("--vault")
    apply_parser.add_argument("--insights-folder")
    apply_parser.add_argument("--project")
    apply_parser.add_argument("--backup-root")
    verify_parser = commands.add_parser("verify", help="check a finished consolidation run")
    verify_parser.add_argument("--backup", required=True, type=Path)
    verify_parser.add_argument("--store", type=Path)
    smoke_parser = commands.add_parser("smoke", help="reindex the vault and search for a moved note")
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
