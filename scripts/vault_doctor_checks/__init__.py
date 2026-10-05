"""vault_doctor check module registry and shared types.

Each check module in this package must export:
  - NAME: str (kebab-case identifier used on the CLI)
  - DESCRIPTION: str
  - DEFAULT_WINDOW_DAYS: int
  - scan(vault_path, sessions_folder, insights_folder, days, project=None) -> list[Issue]
  - apply(issues, backup_root) -> list[Result]
  - OPT_IN: bool (optional, default False) — True excludes the check from the
    default all-checks sweep; it only runs when named via --check

The registry auto-discovers modules in this package directory on first access.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Issue:
    check: str
    note_path: str
    project: str
    current_source: str
    proposed_source: str
    reason: str
    # Set from literals in check modules (e.g. 0.99, 0.5, 0.0). If a check
    # ever computes confidence arithmetically, round to 2 decimals first —
    # vault_doctor's --min-confidence filter compares with exact >=, and a
    # float artifact like 0.8999999999 would silently miss the threshold.
    confidence: float = 1.0
    extra: dict = field(default_factory=dict)


@dataclass
class Result:
    check: str
    note_path: str
    status: str  # "applied" | "skipped" | "error" | "unresolved"
    backup_path: Optional[str] = None
    error: Optional[str] = None


_CHECKS: dict[str, object] = {}


def _discover() -> None:
    """Import every submodule and register ones that expose the check interface.

    Per-module exceptions (ImportError, SyntaxError, etc.) are logged to
    stderr and the offending module is skipped, so one broken check cannot
    take down the whole dispatcher. This keeps the system pluggable.
    Modules that import cleanly but do not expose the check interface
    (NAME + callable scan/apply) are also warned about on stderr.
    """
    if _CHECKS:
        return
    package = __name__
    import sys as _sys
    for mod_info in pkgutil.iter_modules(__path__):
        try:
            mod = importlib.import_module(f"{package}.{mod_info.name}")
        except Exception as exc:  # noqa: BLE001
            print(
                f"[vault_doctor] failed to load check '{mod_info.name}': "
                f"{type(exc).__name__}: {exc}",
                file=_sys.stderr,
            )
            continue
        name = getattr(mod, "NAME", None)
        if name and callable(getattr(mod, "scan", None)) and callable(getattr(mod, "apply", None)):
            _CHECKS[name] = mod
        else:
            # A module that imports fine but lacks the interface (typo'd
            # NAME/scan/apply, helper accidentally dropped into the package)
            # must not vanish silently — its check would simply never run.
            print(
                f"[vault_doctor] module {mod_info.name} loaded but does not "
                f"expose the check interface; skipped",
                file=_sys.stderr,
            )


def list_checks() -> list[str]:
    _discover()
    return sorted(_CHECKS.keys())


def get_check(name: str):
    _discover()
    if name not in _CHECKS:
        raise KeyError(f"unknown check: {name!r}; available: {list_checks()}")
    return _CHECKS[name]


def all_checks() -> list:
    """All registered checks minus opt-in ones (``OPT_IN = True``).

    Opt-in checks (e.g. one-shot audit tools like audit-historic-repairs)
    only run when explicitly named via get_check() / --check.
    """
    _discover()
    result = []
    for m in _CHECKS.values():
        flag = getattr(m, "OPT_IN", False)
        if not isinstance(flag, bool):
            raise TypeError(
                f"{getattr(m, 'NAME', m)}: OPT_IN must be bool, got {flag!r}"
            )
        if not flag:
            result.append(m)
    return result


# Doctor repairs share the same publication boundary as native hook writers.
import contextlib
import functools
from pathlib import Path

from scripts.doctor_repair_state import REPAIRS as _REPAIRS


def vault_scan(function):
    """Keep the selected vault with every issue, including custom nested folders."""
    @functools.wraps(function)
    def scanned(*args, **kwargs):
        vault = args[0] if args else kwargs["vault_path"]
        import hashlib
        before = {}
        for path in Path(vault).rglob("*.md"):
            try:
                before[str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                pass
        issues = function(*args, **kwargs)
        for issue in issues:
            issue.extra.setdefault("vault_path", str(Path(vault).resolve()))
            revision = before.get(str(Path(issue.note_path).resolve()))
            if revision is not None:
                issue.extra.setdefault("raw_source_revision", revision)
        return issues
    return scanned


def _repair_context(issue=None, path=None):
    from note_transactions import context_for_vault
    from runtime_context import current_runtime_context
    from obsidian_utils import load_config
    native = current_runtime_context()
    declared = issue.extra.get("vault_path") if issue is not None else None
    configured = native.vault_path if native is not None else load_config().get("vault_path")
    # Only old, unconfigured Issue callers need the historical folder fallback.
    vault = declared or configured or Path(path or issue.note_path).resolve().parents[1]
    return context_for_vault(vault)


@contextlib.contextmanager
def repair_scope(issues):
    """Keep original scan intent and trusted output across doctor checks."""
    from note_transactions import ownership_lock, record_raw_read
    if _REPAIRS.get() is not None:
        yield _REPAIRS.get()
        return
    issues = list(issues)
    contexts = {}
    for issue in issues:
        if issue.extra.get("unresolved"):
            continue
        context = _repair_context(issue)
        contexts[str(context.vault_path)] = context
    with contextlib.ExitStack() as stack:
        for key in sorted(contexts):
            stack.enter_context(ownership_lock(contexts[key]))
        revisions = {}
        for issue in issues:
            if issue.extra.get("unresolved"):
                continue
            context = _repair_context(issue)
            path = Path(issue.note_path).resolve()
            if path.exists():
                current = record_raw_read(context, path, path.read_bytes())
                original = issue.extra.get("raw_source_revision", current)
                # Start from approved bytes, not a newly observed manual edit.
                revisions.setdefault(str(path), (context, original, original))
        token = _REPAIRS.set(revisions)
        try:
            yield revisions
        finally:
            _REPAIRS.reset(token)


def repair_batch(function):
    """Own a standalone batch or join the dispatcher's trusted repair scope."""
    @functools.wraps(function)
    def repaired(issues, backup_root):
        from note_transactions import record_raw_read, LockBusy
        issues = list(issues)
        try:
            with repair_scope(issues) as revisions:
                approved, rejected = [], []
                for issue in issues:
                    path = Path(issue.note_path).resolve()
                    saved = revisions.get(str(path))
                    if saved is not None and path.exists():
                        record_raw_read(saved[0], path, path.read_bytes())
                        baseline = issue.extra.get("raw_source_revision", saved[1])
                        if baseline != saved[1]:
                            rejected.append(Result(
                                issue.check, issue.note_path, "error",
                                error="doctor publication conflict: this issue has a different scan baseline",
                            ))
                            continue
                    approved.append(issue)
                return rejected + function(approved, backup_root)
        except (OSError, ValueError, LockBusy) as exc:
            return [Result(issue.check, issue.note_path, "error", error=str(exc)) for issue in issues]
    return repaired


def repair_read(path, vault_path=None):
    """Read exact bytes and register their revision before constructing a fix."""
    from note_transactions import context_for_vault, record_raw_read
    state = _REPAIRS.get()
    saved = state.get(str(Path(path).resolve())) if state is not None else None
    context = (context_for_vault(vault_path) if vault_path else
               saved[0] if saved else _repair_context(path=path))
    raw = Path(path).read_bytes()
    revision = record_raw_read(context, path, raw)
    if state is not None:
        state.setdefault(str(Path(path).resolve()), (context, revision, revision))
    return raw.decode("utf-8")


def repair_write(path, text, *, encoding_repair=False):
    """Publish only against the bytes observed before the repair was computed."""
    from note_transactions import NoteMutation, apply_mutations, record_raw_read
    import hashlib
    resolved = Path(path).resolve()
    state = _REPAIRS.get()
    saved = state.get(str(resolved)) if state is not None else None
    context = saved[0] if saved else _repair_context(path=path)
    revision = saved[2] if saved else record_raw_read(context, path, resolved.read_bytes())
    # Ordinary repairs must not silently bake invalid bytes into replacement chars.
    if not encoding_repair:
        resolved.read_bytes().decode("utf-8")
    operation = "doctor-" + hashlib.sha256(
        (str(resolved) + "\0" + str(revision) + "\0" + text).encode("utf-8")).hexdigest()
    kind = "repair_document" if encoding_repair else "document"
    result = apply_mutations(context, [NoteMutation(resolved, revision, {kind: text}, operation)])
    if result.status not in {"applied", "unchanged"}:
        raise OSError("doctor publication " + result.status + ": " + "; ".join(result.warnings))
    if state is not None:
        state[str(resolved)] = (context, saved[1] if saved else revision, result.revision)
    return result


def repair_move(source, destination, *, expected_revision="tracked"):
    """Move or roll back only against the version observed by this repair."""
    from note_transactions import move_note, record_raw_read
    import hashlib
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    state = _REPAIRS.get()
    saved = state.get(str(source)) if state is not None else None
    context = saved[0] if saved else _repair_context(path=source)
    revision = saved[2] if saved else record_raw_read(context, source, source.read_bytes())
    if expected_revision != "tracked":
        revision = expected_revision
    operation = "doctor-move-" + hashlib.sha256(
        (str(source) + "\0" + str(destination) + "\0" + str(revision)).encode("utf-8")).hexdigest()
    result = move_note(context, source, destination, revision, operation)
    if result.status not in {"applied", "unchanged"}:
        raise OSError("doctor move " + result.status + ": " + "; ".join(result.warnings))
    if state is not None:
        state.pop(str(source), None)
        state[str(destination)] = (context, saved[1] if saved else revision, result.revision)
    return result
