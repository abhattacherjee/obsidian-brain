"""Read-only project memory audit and consolidation plan validation."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True

from memory_links import line_pointers, line_asides, _md_child_link_targets
from memory_sources import _projects_root
from obsidian_utils import _cc_encoded_dirnames


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


def audit(store: Path) -> dict:
    text, _ = _read_file(store / "MEMORY.md")
    try:
        names = {p.name for p in store.iterdir() if p.name.endswith(".md") and p.name != "MEMORY.md"}
    except OSError as exc:
        raise StoreError(str(exc)) from exc
    lines = text.splitlines()
    pointers = Counter()
    referenced = set()
    for line in lines:
        ptr = line_pointers(line, names)
        pointers.update(ptr)
        referenced.update(ptr | line_asides(line, names))
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


def load_plan(path: Path, store: Path, vault_root: Path) -> tuple[list[dict], list[str]]:
    def reject_constant(value):
        raise PlanError(f"invalid JSON constant: {value}")

    try:
        text, _ = _read_file(path)
        plan = json.loads(text, parse_constant=reject_constant)
    except (StoreError, PlanError, ValueError, RecursionError) as exc:
        return [], [str(exc)]
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
    refused = {}
    for line in lines:
        ptr = line_pointers(line, store_names)
        staying = ptr - moving
        if staying:
            for name in ptr & moving:
                refused[Path(name).stem] = "index line also points at staying entries: " + ", ".join(sorted(staying))

    eligible = {name for name in moving if Path(name).stem not in refused}
    kept, dropped, warnings = [], [], {}
    insert_at = None
    for line in lines:
        ptr = line_pointers(line, store_names)
        if ptr and ptr <= eligible:
            dropped.append(line)
            continue
        kept.append(line)
        if ptr and line.startswith("- "):
            insert_at = len(kept)
        if not (ptr & moving and ptr - moving):
            for name in line_asides(line, store_names) & moving:
                warnings[Path(name).stem] = "named as an aside on a kept index line"
    if dropped and pointer_line:
        if insert_at is None:
            insert_at = len(kept) - int(index_text.endswith("\n"))
        kept.insert(insert_at, pointer_line)
    return "\n".join(kept), dropped, refused, warnings


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
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
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
        for key in ("entries", "orphans", "dangling", "duplicates"):
            names = [e["slug"] for e in report[key]] if key == "entries" else report[key]
            print(f"{key} ({len(names)}): {', '.join(names)}")
    return int(report["mode"] != "ok" or bool(report["dangling"] or report["duplicates"]))


if __name__ == "__main__":
    sys.exit(main())
