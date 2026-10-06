"""
Cache module for /check-items classifier results.

Per spec section Persistence and cache invalidation (lines 410-494).
Python stdlib only.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import uuid
import threading
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from runtime_adapters.claude import legacy_private_directory
CACHE_DIR: Path = legacy_private_directory()
CACHE_PATH: Path = CACHE_DIR / "check-items-classifications.json"

# The following constants are used by Tasks 4-7 (load_cache,
# save_cache, partition, update_cache).  They live here so the module is
# the single authoritative home for cache policy values.

SCHEMA_VERSION = 1

TTL_DONE = 86_400          # 24h
TTL_NEEDS_ACTION = 86_400  # 24h
TTL_STALE = 86_400         # 24h
TTL_ACTIVE = 604_800       # 7d


_MARKDOWN_RE = re.compile(r"[*_`~\[\]()#>]+")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_markdown(text: str) -> str:
    return _MARKDOWN_RE.sub("", text)


def canonical_hash(text: str) -> str:
    """
    Compute a stable canonical hash for a check-item text string.

    Stable across cosmetic whitespace/markdown edits; busts on real content
    rename.  Uses SHA-256 with whitespace and markdown normalization, then
    truncates to 16 hex chars (64-bit prefix) as the cache key.

    Spec: "Canonical hash" (Persistence and cache invalidation, lines 410-494).
    """
    stripped = _strip_markdown(text or "")
    normalized = _WHITESPACE_RE.sub(" ", stripped).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _cache_path() -> Path:
    from runtime_context import current_runtime_context
    context = current_runtime_context()
    if context is not None:
        from session_auxiliary_state import cross_run_directory
        return cross_run_directory(context) / "check-items-classifications.json"
    return CACHE_PATH


def _empty_cache() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "runs": {}}


def load_cache(*, with_status: bool = False):
    """
    Load the cache. On corruption or schema-version mismatch, warn to stderr
    and return an empty cache. Never blocks the pipeline.

    With `with_status=True`, returns `(data, status)` instead of bare `data`.
    `status` is one of:

      None              -- loaded cleanly (including "file does not exist yet").
      "corrupt"         -- json.JSONDecodeError: the file itself IS the
                            garbage, so overwriting it on save is the correct
                            recovery.
      "unreadable"      -- an OSError reading an otherwise-possibly-fine file
                            (EACCES, EIO, EMFILE, ...). The file was not
                            proven bad, only unreadable *right now* --
                            overwriting it would destroy data this call
                            merely failed to read.
      "foreign_schema"  -- valid JSON, but not this schema (wrong type, or a
                            schema_version this code does not recognise --
                            e.g. written by a newer version). Overwriting
                            would be a silent downgrade.

    Existing callers that pass no arguments are unaffected -- they keep
    getting bare `data`, exactly as before. A caller that opts into
    `with_status=True` and gets back anything other than `None`/"corrupt"
    must NOT blindly save over the (empty) result it was handed -- see
    `locked_cache()`, the only caller that currently does (#323 F1).
    """
    path = _cache_path()

    def _ret(data, status):
        return (data, status) if with_status else data

    if not path.exists():
        return _ret(_empty_cache(), None)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        print(f"[check-items-cache] WARNING: cache load failed ({exc}); using empty cache",
              file=sys.stderr)
        return _ret(_empty_cache(), "corrupt")
    except OSError as exc:
        print(f"[check-items-cache] WARNING: cache load failed ({exc}); using empty cache",
              file=sys.stderr)
        return _ret(_empty_cache(), "unreadable")
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        print(f"[check-items-cache] WARNING: cache schema mismatch; using empty cache",
              file=sys.stderr)
        return _ret(_empty_cache(), "foreign_schema")
    data.setdefault("runs", {})
    return _ret(data, None)


def save_cache(data: dict[str, Any]) -> None:
    """Atomically write the cache with 0o600 permissions."""
    path = _cache_path()
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", delete=False, dir=str(directory), suffix=".tmp", encoding="utf-8"
    )
    try:
        json.dump(data, tmp, indent=2, default=str)
        tmp.flush()
        os.fsync(tmp.fileno())
    except Exception:
        # A failed write (disk full mid-json.dump, a bad fsync, ...) never
        # reaches os.replace() below, so the temp file is orphaned unless we
        # clean it up here. Disk-full is exactly the condition likely to
        # produce a whole run of these (#323 F5). Suppress a failure from the
        # unlink itself -- the disk may still be full -- rather than masking
        # the original error by raising that one instead.
        tmp.close()
        with contextlib.suppress(OSError):
            os.unlink(tmp.name)
        raise
    else:
        tmp.close()
    os.replace(tmp.name, str(path))
    os.chmod(str(path), 0o600)


# Cache ownership is independent of vault writer ownership. Retain the lock
# inode; a crashed holder releases its OS lock without an age takeover.
_LOCK_TIMEOUT_SECONDS = 10.0   # max time locked_cache() waits for a contended lock
_LOCK_STALE_SECONDS = 30.0     # legacy age helper only; never grants ownership
_LOCK_POLL_INTERVAL_SECONDS = 0.05
# #323 F7: _lock_payload() formats its timestamp with `.3f` (millisecond
# precision), which ROUNDS -- up to ~0.5ms toward the future. A lock read
# back and aged microseconds later can therefore see a genuinely fresh
# payload as up to ~0.5ms "ahead" of time.time(), a pure formatting
# artifact with no bearing on staleness. Measured empirically: naively
# gating on `0 <= age` made a freshly-written payload fall through to the
# st_mtime fallback in roughly HALF of all checks (956,812 / 2,000,000
# trials of `time.time() - float(f"{time.time():.3f}")` came back
# negative) -- reintroducing the exact single-read mis-pairing hazard the
# docstring above describes as already fixed, since mtime and the payload
# can differ. 10ms is two orders of magnitude past the ~0.5ms formatting
# error and many orders of magnitude short of the minutes-to-hours a real
# clock stepped back by NTP or a resumed VM would show, so it cannot mask
# an actual unusable timestamp.
_LOCK_AGE_ROUNDING_TOLERANCE_SECONDS = 0.01


_OWNED_LOCKS = {}
_OWNERS_GUARD = threading.Lock()


def _lock_payload() -> bytes:
    return f"{os.getpid()} {time.time():.3f} {uuid.uuid4().hex}\n".encode()


def _try_create_lock(lock_path: Path, payload: bytes) -> None:
    """Acquire the persistent inode; age never grants ownership."""
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FileExistsError(str(lock_path)) from exc
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        os.write(fd, payload)
        with _OWNERS_GUARD:
            _OWNED_LOCKS[payload] = (fd, lock_path)
    except BaseException:
        os.close(fd)
        raise


def _lock_age(lock_path: Path, holder: bytes) -> float:
    """Legacy timestamp diagnostic; OS ownership does not use lock age."""
    try:
        age = time.time() - float(holder.split()[1])
        if -_LOCK_AGE_ROUNDING_TOLERANCE_SECONDS <= age:
            # Clamp a rounding-negative age to 0.0 rather than returning it
            # raw: callers (and this function's own contract, "age ... in
            # seconds") reasonably expect a non-negative result, and a
            # sub-millisecond negative value is formatting noise, not a
            # real measurement.
            return max(0.0, age)
    except (IndexError, ValueError):
        pass
    try:
        age = time.time() - lock_path.stat().st_mtime
        if -_LOCK_AGE_ROUNDING_TOLERANCE_SECONDS <= age:
            return max(0.0, age)
    except OSError:
        pass
    # Neither source yielded a usable past time (both unparseable/missing,
    # or both parsed to something NaN/future-dated). Resolve to stale so the
    # lock self-heals on the next acquire attempt instead of wedging.
    print(f"[check-items-cache] WARNING: cache lock {lock_path} carries no "
          f"usable timestamp; treating it as stale", file=sys.stderr)
    return _LOCK_STALE_SECONDS + 1.0


def _acquire_lock(lock_path: Path, timeout: float) -> bytes | None:
    """Wait for OS ownership, bounded by a monotonic deadline."""
    try:
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    while True:
        payload = _lock_payload()
        try:
            _try_create_lock(lock_path, payload)
            return payload
        except FileExistsError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(_LOCK_POLL_INTERVAL_SECONDS, max(0, deadline - time.monotonic())))
        except OSError:
            return None


def _read_lock(lock_path: Path) -> bytes | None:
    try:
        return lock_path.read_bytes()
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            print("[check-items-cache] WARNING: could not read the cache lock", file=sys.stderr)
        return None


def _owns_lock(lock_path: Path, payload: bytes) -> bool:
    with _OWNERS_GUARD:
        held = _OWNED_LOCKS.get(payload)
    if held is None:
        return False
    try:
        fd, _ = held
        actual = lock_path.stat()
        owned = os.fstat(fd)
        return (actual.st_dev, actual.st_ino) == (owned.st_dev, owned.st_ino) and _read_lock(lock_path) == payload
    except OSError:
        return False


def _release_lock(lock_path: Path, payload: bytes) -> None:
    owned = _owns_lock(lock_path, payload)
    with _OWNERS_GUARD:
        held = _OWNED_LOCKS.pop(payload, None)
    if held is None:
        return
    fd, _ = held
    try:
        if owned:
            os.ftruncate(fd, 0)
        else:
            print("[check-items-cache] WARNING: lost cache lock ownership; cache publication skipped", file=sys.stderr)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def locked_cache(timeout: float = _LOCK_TIMEOUT_SECONDS):
    """Yield mutable cache data, publishing only while OS ownership remains.

    A timeout still permits computation, but drops the cache update. Persistent
    lock files are never unlinked or taken from a living holder based on age.
    """
    lock_path = _cache_path().with_suffix(".lock")
    payload = _acquire_lock(lock_path, timeout)
    if payload is None:
        print(
            f"[check-items-cache] WARNING: proceeding without the cache lock "
            f"({lock_path}); cache publication will be skipped",
            file=sys.stderr,
        )
    cache, status = load_cache(with_status=True)
    try:
        yield cache
        if payload is not None and not _owns_lock(lock_path, payload):
            print(
                f"[check-items-cache] WARNING: lost the cache lock mid-run "
                f"({lock_path}); cache publication will be skipped",
                file=sys.stderr,
            )
        if payload is None or not _owns_lock(lock_path, payload):
            print("[check-items-cache] WARNING: refusing to publish without cache ownership",
                  file=sys.stderr)
        elif status in (None, "corrupt"):
            save_cache(cache)
        else:
            print(
                f"[check-items-cache] WARNING: refusing to save -- the "
                f"on-disk cache could not be loaded ({status}); this run's "
                f"cache update is being dropped, the existing on-disk cache "
                f"is left untouched, and every group re-classifies next run",
                file=sys.stderr,
            )
    finally:
        if payload is not None:
            _release_lock(lock_path, payload)


def _ttl_for(classification: str) -> int:
    # REVIEW is a nudge for human judgement (#264 Task 1) — it must
    # re-evaluate on the same cadence as ACTIVE, not the short DONE/
    # NEEDS-ACTION/STALE cycle, so it maps explicitly to TTL_ACTIVE
    # (matches the default fallback, spelled out here for clarity).
    return {
        "DONE": TTL_DONE,
        "NEEDS-ACTION": TTL_NEEDS_ACTION,
        "STALE": TTL_STALE,
        "REVIEW": TTL_ACTIVE,
    }.get(classification, TTL_ACTIVE)


def _mtime_matches(cached_members: list[dict], current_members: list[dict]) -> bool:
    """True iff all current member mtimes match cached within 1s tolerance."""
    cached_by_key = {(m.get("file"), m.get("line")): m.get("mtime") for m in cached_members}
    for cm in current_members:
        cached_mtime = cached_by_key.get((cm.get("file"), cm.get("line")))
        if cached_mtime is None:
            return False
        try:
            if abs(float(cm.get("mtime", 0)) - float(cached_mtime)) > 1.0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _classified_ts_age(cached: dict, now: float) -> float:
    """Seconds between `now` and a cached entry's stored classified_ts.

    A sufficiently large int (e.g. a 400-digit JSON integer) makes float()
    raise OverflowError rather than ValueError -- treat it the same as any
    other unusable value: ancient, so the group re-derives. A NaN stamp
    yields a NaN age, which fails every comparison; callers must test
    `0 <= age`, never `age < 0`.
    """
    try:
        classified_ts = float(cached.get("classified_ts", 0))
    except (TypeError, ValueError, OverflowError):
        classified_ts = 0.0
    return now - classified_ts


def _warn_if_unusable_ts(cached: dict, now: float, h: str, project: str) -> None:
    """Report a stored classified_ts that is not a valid past time.

    A negative or NaN age means clock skew, a hand-edit, or an external
    writer -- an environmental fault worth reporting, distinct from routine
    expiry. Rejecting on read means the replay never forms, so
    _freeze_classification's write-side clamp warning cannot fire for this
    entry; the signal has to live here. Called from BOTH guards that can
    route such an entry to `needs` (#305), which are mutually exclusive.
    """
    if not (0 <= _classified_ts_age(cached, now)):
        print(
            f"[check-items-cache] WARNING: unusable classified_ts "
            f"({cached.get('classified_ts')!r}) for {h} in {project}; "
            f"re-deriving instead of replaying",
            file=sys.stderr,
        )


def _cache_ai_identity(context, requested=None):
    from ai_backend import resolve_ai_selection
    # A prior result's model is evidence about that run, not today's native
    # default. Codex replay must independently resolve its current selection.
    backend, model = resolve_ai_selection(context, "classify_items",
                                        requested if context.host == "claude" else None)
    if backend == "claude":
        # An observed ID from a previous run cannot resolve today's alias.
        explicit = context.config.get("classifier_model")
        model = explicit if isinstance(explicit, str) and explicit.startswith("claude-") else None
    return backend, model



def classifier_cache_replay_status(context):
    """Report whether today's native model selection can authorize replay."""
    from runtime_context import current_runtime_context
    if current_runtime_context() is not context:
        raise ValueError("Classifier cache status requires the invoking context")
    _, model = _cache_ai_identity(context)
    enabled = isinstance(model, str) and bool(model)
    return {"enabled": enabled, "reason": "ready" if enabled else "native_model_unresolved",
            "model": model if enabled else None}


def _classifier_contract(groups):
    from ai_backend import ai_contract_identity
    identifiers = {group.get("group_id", ""): canonical_hash(group.get("representative", ""))
                   for group in groups}
    return ai_contract_identity("classify_items", {
        "expected_ids": [identifiers[group.get("group_id", "")] for group in groups],
        "project_by_id": {identifiers[group.get("group_id", "")]: group.get("project", "") for group in groups},
        "expected_count": len(groups),
    })


def build_classifier_provenance(context, groups, evidence, classifications=None):
    """Build the full namespace before cache replay; never invoke AI."""
    import copy
    from runtime_context import current_runtime_context
    from check_items_cli import (CLASSIFIER_CHUNK_SIZE, CLASSIFIER_PROMPT,
                                 _bridge_project_evidence, _pick_classifier_model, _valid_groups)
    from check_items_prefilter import has_classifiable_evidence, is_prefilter_enabled
    if current_runtime_context() is not context or not _valid_groups(groups) or not isinstance(evidence, dict):
        raise ValueError("Classifier provenance requires bound complete input")
    enabled = is_prefilter_enabled()
    classified = []
    for group in groups:
        normalized = dict(group)
        normalized.setdefault("instances", group.get("members", []))
        if not enabled or has_classifiable_evidence(normalized, _bridge_project_evidence(evidence, group.get("project", ""))):
            classified.append(group)
    selections = {}
    contracts = {}
    for index in range(0, len(classified), CLASSIFIER_CHUNK_SIZE):
        chunk = classified[index:index + CLASSIFIER_CHUNK_SIZE]
        requested = _pick_classifier_model(len(chunk)) if context.host == "claude" else None
        backend, model = _cache_ai_identity(context, requested)
        for group in chunk:
            selections[group["group_id"]] = {"backend": backend, "model": model}
            contracts[group["group_id"]] = _classifier_contract(chunk)
    backend, fallback = _cache_ai_identity(context, "haiku" if context.host == "claude" else None)
    for group in groups:
        selections.setdefault(group["group_id"], {"backend": backend, "model": fallback})
        contracts.setdefault(group["group_id"], _classifier_contract([group]))
    if classifications is not None:
        for record in classifications:
            if record.get("classifier_source") != "agent":
                continue
            expected = selections.get(record.get("group_id"))
            observed = record.get("ai_model")
            if (expected is None or record.get("ai_backend") != expected["backend"]
                    or not isinstance(observed, str) or not observed
                    or (context.host == "claude" and not observed.startswith("claude-"))
                    or (expected["model"] is not None and observed != expected["model"])):
                raise ValueError("Successful classifier metadata differs from approved selection")
            expected["model"] = observed
    return {"complete": True, "evidence": copy.deepcopy(evidence),
            "classifier_model_by_id": {key: value["model"] for key, value in selections.items()},
            "classifier_backend_by_id": {key: value["backend"] for key, value in selections.items()},
            "backend_contract_by_id": contracts,
            "classifier_chunk_size": CLASSIFIER_CHUNK_SIZE, "prefilter": enabled,
            "prompt_sha256": hashlib.sha256(CLASSIFIER_PROMPT.encode("utf-8")).hexdigest(),
            "output_schema": "classifier-exact-ids-v3"}


def _provenance(group, project, extra=None, *, observed_model=None):
    """Fingerprint input, evidence, and the actual selected classifier model."""
    from runtime_context import current_runtime_context
    from check_items_cli import CLASSIFIER_PROMPT
    context = current_runtime_context()
    if context is None:
        return None
    extra = extra or {}
    if extra.get("complete") is not True or "evidence" not in extra:
        return None
    requested = extra.get("classifier_model_by_id", {}).get(group.get("group_id"))
    if requested is None:
        requested = extra.get("classifier_model")
    backend, model = _cache_ai_identity(context, requested)
    if observed_model is not None:
        model = observed_model
    # Unknown native defaults cannot prove replay uses the same model.
    if not isinstance(model, str) or not model:
        return None
    semantic = {key: value for key, value in group.items()
                if not key.startswith("_") and key != "group_id"}
    for field in ("members", "instances"):
        if isinstance(semantic.get(field), list):
            semantic[field] = [{key: value for key, value in member.items() if key != "mtime"}
                               for member in semantic[field]]
    stable_extra = {key: value for key, value in extra.items()
                    if not key.startswith('_') and key not in {
                        'classifier_model_by_id', 'classifier_backend_by_id', 'backend_contract_by_id'}}
    stable_extra["backend_contract"] = extra.get("backend_contract_by_id", {}).get(group.get("group_id"))
    values = {
        "input": semantic,
        "vault": str(context.vault_path.resolve()),
        "project": str(context.canonical_project_root.resolve()),
        "algorithm": "check-items-v3",
        "schema": SCHEMA_VERSION,
        "prompt": hashlib.sha256(CLASSIFIER_PROMPT.encode("utf-8")).hexdigest(),
        "output_schema": "classifier-exact-ids-v3",
        "shared_backend_contract": _classifier_contract([group]),
        "classifier_policy": {"threshold": 30, "chunk_size": extra.get("classifier_chunk_size", 25),
                              "prefilter": extra.get("prefilter", True)},
        "backend": backend,
        "model": model,
        "evidence": stable_extra,
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _agent_metadata_matches(group, fresh, extra=None):
    from runtime_context import current_runtime_context
    from check_items_cli import CLASSIFIER_PROMPT
    context = current_runtime_context()
    if context is None:
        return False
    extra = extra or {}
    requested = extra.get("classifier_model_by_id", {}).get(group.get("group_id"))
    requested = requested or extra.get("classifier_model")
    backend, model = _cache_ai_identity(context, requested)
    observed = fresh.get("ai_model")
    known_observed = isinstance(observed, str) and bool(observed) and (backend != "claude" or observed.startswith("claude-"))
    return (known_observed and fresh.get("ai_backend") == backend
            and (model is None or observed == model)
            and fresh.get("ai_prompt_version") == "check-items-classifier-v3"
            and fresh.get("ai_prompt_sha256") == hashlib.sha256(CLASSIFIER_PROMPT.encode("utf-8")).hexdigest())


def partition(
    groups: list[dict],
    cache: dict,
    project: str,
    head_sha: str,
    force: bool = False,
    now: float | None = None,
    *, provenance: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Apply invalidation rules in spec order (first match wins):
        force -> new -> head_changed -> mtime_changed ->
        ttl_expired (or unusable_ts for a negative/NaN age -- a corrupt,
        hand-edited, or clock-skewed stamp, reported distinctly from routine
        expiry) -> heuristic_cached.
    Returns (known_unchanged, needs_reclassification). Groups routed to
    `needs` carry `_reason` for dashboard visibility. Groups routed to
    `known` carry `_cached_classification` / `_cached_confidence` /
    `_cached_evidence_citation` / `_cached_action_required` (NOT `_reason`).
    """
    if now is None:
        now = time.time()
    run = cache.get("runs", {}).get(project, {})
    cached_groups_by_hash = {
        g["canonical_hash"]: g
        for g in run.get("groups", [])
        if isinstance(g, dict) and isinstance(g.get("canonical_hash"), str)
    }
    cached_head = run.get("project_head_at_classify")

    known: list[dict] = []
    needs: list[dict] = []

    for g in groups:
        h = g.get("canonical_hash")
        if force:
            g["_reason"] = "force"
            needs.append(g)
            continue
        cached = cached_groups_by_hash.get(h)
        if cached is None:
            g["_reason"] = "new"
            needs.append(g)
            continue
        # #305: prefer the per-entry stamp when present. An entry only carries one
        # when update_cache could not confirm it this run, so the fallback is
        # exactly right for every other entry: those DID have a fresh record, and
        # the record was produced at the head the run-level field was then bumped
        # to.
        entry_head = cached.get("head_at_classify", cached_head)
        if entry_head != head_sha:
            # #305: a pinned survivor short-circuits HERE, before the
            # classified_ts range check below that reports a corrupt or
            # clock-skewed stamp. Routing is unaffected (both land in
            # `needs`), but the diagnostic would be silently lost -- and
            # pinned survivors are exactly the population most likely to
            # carry a stale stamp, since by definition nothing re-verified
            # them. Emit the same warning here so reordering the guards does
            # not cost the signal. Mutually exclusive with the call below:
            # each branch `continue`s, so a stamp is never reported twice.
            _warn_if_unusable_ts(cached, now, h, project)
            g["_reason"] = "head_changed"
            needs.append(g)
            continue
        if not _mtime_matches(cached.get("members", []), g.get("members", [])):
            g["_reason"] = "mtime_changed"
            needs.append(g)
            continue
        _age = _classified_ts_age(cached, now)
        # Symmetric range, not a one-sided `> ttl`: an age outside [0, ttl] is
        # unusable, not merely old. A NEGATIVE age means the stamp is ahead of
        # the clock (skew, a hand-edit, a cache synced from a faster machine)
        # and a one-sided check would trust it forever; a NaN age fails every
        # comparison, so `0 <= _age` is False and it lands here too. Both are
        # re-derived instead of replayed. _freeze_classification clamps the
        # stored value on write; this closes the same hole on read, in the
        # SAME run rather than the next one -- which matters because a
        # replayed verdict is high-trust and can preselect for auto-checkoff.
        if not (0 <= _age <= _ttl_for(cached.get("classification", "ACTIVE"))):
            # Separate "unusable" from "merely old". A negative or NaN age means
            # the stored stamp is not a valid past time (clock skew, a hand-edit,
            # an external writer) -- an environmental fault worth reporting, not
            # the routine expiry that a bare "ttl_expired" implies. Rejecting on
            # read means the replay never forms, so _freeze_classification's
            # clamp warning cannot fire for this entry; the signal has to live
            # here.
            if not (0 <= _age):
                _warn_if_unusable_ts(cached, now, h, project)
                g["_reason"] = "unusable_ts"
            else:
                g["_reason"] = "ttl_expired"
            needs.append(g)
            continue
        # #297: a cached verdict that was produced by the token-overlap
        # heuristic must never be replayed as `known`. Entries written before
        # #297 carry no classifier_source at all, so fall back to the
        # citation's shape — classify_groups_heuristic always emits
        # "heuristic: token '<t>' near completion phrase '<p>'". Replaying one
        # would let SKILL.md stamp it "cache" (a trusted source) and
        # preselect it for auto-checkoff, which is the exact defect #297 is
        # about. Routing to `needs` re-classifies it with real evidence.
        if (cached.get("classifier_source") == "heuristic"
                or str(cached.get("evidence_citation") or "").startswith("heuristic:")):
            g["_reason"] = "heuristic_cached"
            needs.append(g)
            continue
        expected_provenance = _provenance(g, project, provenance)
        if expected_provenance is None or cached.get("provenance") != expected_provenance:
            g["_reason"] = "provenance_changed"
            needs.append(g)
            continue
        g["_cached_classification"] = cached.get("classification")
        g["_cached_confidence"] = cached.get("confidence")
        g["_cached_evidence_citation"] = cached.get("evidence_citation")
        g["_cached_action_required"] = cached.get("action_required")
        for field in ("ai_backend", "ai_model", "ai_prompt_version", "ai_prompt_sha256"):
            g["_cached_" + field] = cached.get(field)
        known.append(g)

    return known, needs


def update_cache(
    cache: dict,
    project: str,
    all_groups: list[dict],
    fresh_classifications: list[dict],
    head_sha: str,
    now: float | None = None,
    *, provenance: dict | None = None,
) -> dict:
    """
    Merge fresh classifications into the cache and GC entries whose
    canonical_hash is no longer in the current run.

    1. Build a hash set from all_groups (the current run's groups).
    2. Keep cached groups whose hash is in the current set; evict the rest.
    3. Overwrite by canonical_hash with any fresh classifications.
    4. Bump last_run_ts and project_head_at_classify.
    """
    if now is None:
        now = time.time()

    # #297 defect 5: only known-trusted verdicts may be persisted. partition()
    # replays cached verdicts as `known` until the project's HEAD moves, so a
    # single degraded run would poison every later run with token-co-occurrence
    # citations. Allowlist, not denylist: an un-migrated or misspelled source
    # (absent, "", "Heuristic", a future value nobody has audited yet) must be
    # refused rather than silently persisted — the same allowlist-over-denylist
    # reasoning as assign_tier's _HIGH_TRUST_SOURCES cap (open_item_dedup.py).
    # Enforced here rather than only in SKILL.md because skills are advisory
    # and code is not (memory: feedback_skills_advisory_not_enforcement).
    _TRUSTED_SOURCES = {"agent", "prefilter", "cache"}
    _rejected = [fc for fc in (fresh_classifications or [])
                 if fc.get("classifier_source") not in _TRUSTED_SOURCES]
    if _rejected:
        print(
            f"[check-items-cache] refusing to cache {len(_rejected)} "
            f"verdict(s) with untrusted/unknown classifier_source for "
            f"{project}; they will be re-classified on the next run",
            file=sys.stderr,
        )
    _rejected_hashes = {fc.get("canonical_hash") for fc in _rejected}
    fresh_classifications = [fc for fc in (fresh_classifications or [])
                             if fc.get("classifier_source") in _TRUSTED_SOURCES]

    current_hashes = {g.get("canonical_hash") for g in all_groups}
    fresh_by_hash = {fc.get("canonical_hash"): fc for fc in fresh_classifications}

    cache.setdefault("schema_version", SCHEMA_VERSION)
    cache.setdefault("runs", {})
    run = cache["runs"].setdefault(project, {})
    prior_run_head = run.get("project_head_at_classify")
    existing_groups = run.get("groups", [])

    surviving: list[dict] = []
    seen: set[str] = set()
    for g in existing_groups:
        # Mirrors partition()'s isinstance(g, dict) filter when it builds
        # cached_groups_by_hash: a corrupt cache file (a stray scalar or
        # list inside `groups`) degrades to a skipped entry rather than
        # crashing the whole cache write with AttributeError.
        if not isinstance(g, dict):
            continue
        h = g.get("canonical_hash")
        if h not in current_hashes:
            continue
        if h in _rejected_hashes:
            # This run could not verify the group — do not let an older entry
            # be revalidated by the unconditional project_head_at_classify bump.
            continue
        if h in fresh_by_hash:
            surviving.append(_freeze_classification(fresh_by_hash[h], now, prior=g))
        else:
            # #305: this entry got no fresh classification this run, so it was
            # never verified against `head_sha`. Pin it to the head it WAS
            # verified at, so the unconditional run-level bump below cannot
            # revalidate it. setdefault semantics (not an overwrite): an entry
            # that already dissented in an earlier run must keep its ORIGINAL
            # head, not inherit the newer run-level value it was equally never
            # verified against.
            surviving.append({**g, "head_at_classify": g.get("head_at_classify", prior_run_head)})
        seen.add(h)

    for h, fc in fresh_by_hash.items():
        # `h in _rejected_hashes` must be checked here too, not just in the
        # loop above: a hash rejected in loop 1 (e.g. this run's verdict for
        # it was "heuristic") was deliberately left OUT of `seen` so its
        # prior entry could be evicted. Without this check, if the SAME hash
        # also has a trusted "cache" record in fresh_by_hash (fresh_by_hash
        # is last-write-wins over fresh_classifications, so either ordering
        # of the two records for one hash collapses to one here), this loop
        # would re-insert it immediately -- undoing the eviction loop 1 just
        # performed, in the same function call, and defeating both #297's
        # "do not let an unverified verdict be replayed" guard and #302's
        # inheritance (a fresh insert here restarts the TTL at `now`).
        if h in seen or h in _rejected_hashes or h not in current_hashes:
            continue
        # The common case: a brand-new canonical_hash that partition() routed
        # here with _reason "new", so there is no prior entry and `now` is the
        # right stamp. Two rarer routes also land here: a
        # classifier_source="cache" replay whose backing entry vanished between
        # this run's two load_cache() calls (a concurrent run — warned by
        # _freeze_classification's else-branch below), and a hash whose prior
        # entry was just evicted by the #297 _rejected_hashes branch earlier
        # in this function.
        surviving.append(_freeze_classification(fc, now))

    inputs = {group.get("canonical_hash"): group for group in all_groups}
    previous = {entry.get("canonical_hash"): entry for entry in existing_groups if isinstance(entry, dict)}
    for entry in surviving:
        key = entry.get("canonical_hash")
        fresh = fresh_by_hash.get(key)
        if fresh is not None and fresh.get("classifier_source") != "cache":
            approved = (fresh.get("classifier_source") != "agent"
                        or _agent_metadata_matches(inputs[key], fresh, provenance))
            observed_provenance = provenance
            if (approved and fresh.get("classifier_source") == "agent" and provenance is not None
                    and _provenance(inputs[key], project, provenance) is None):
                import copy
                observed_provenance = copy.deepcopy(provenance)
                observed_provenance.setdefault("classifier_model_by_id", {})[inputs[key].get("group_id")] = fresh["ai_model"]
            entry["provenance"] = (_provenance(inputs[key], project, observed_provenance,
                                               observed_model=fresh.get("ai_model") if fresh.get("classifier_source") == "agent" else None)
                                   if approved else None)
        elif fresh is not None and key in previous and "provenance" in previous[key]:
            entry["provenance"] = previous[key]["provenance"]
    run["groups"] = surviving
    run["last_run_ts"] = int(now)
    run["project_head_at_classify"] = head_sha
    return cache


def _resolve_replay_ts(fc: dict, prior: dict | None, now: float):
    """Decide the classified_ts an entry should be persisted with.

    `fc` is the fresh classification record for this run, `prior` is the
    existing on-disk cache entry for the same canonical_hash (or None if
    there isn't one), and `now` is this run's timestamp. Returns the value
    to store as classified_ts.

    #302: classifier_source == "cache" means this run *replayed* a verdict
    from partition()'s known-hit path without re-deriving it (Step 6 of
    check-items/SKILL.md). SKILL.md Step 10 unconditionally stamps
    classified_ts=int(time.time()) on every record it hands to
    update_cache, including these replays. If we accepted that stamp here,
    a replayed verdict's TTL clock would reset on every run that merely
    *reads* it, so partition()'s ttl_expired check would never fire on a
    repo whose HEAD is static — measuring "last read" instead of "last
    verification". Inherit the prior on-disk classified_ts verbatim
    instead: partition() already tolerates a non-numeric classified_ts by
    treating it as ancient (see
    test_partition_handles_non_numeric_classified_ts), so round-tripping a
    possibly-corrupt prior value is fail-safe, whereas "healing" it by
    stamping `now` would re-introduce exactly this bug. That tolerance
    has one gap: a *numeric* bad value (a millisecond-valued stamp, a
    hand-edited future date) parses fine and is NOT read as ancient, so the
    clamp below handles the future-dated and NaN cases explicitly.
    """
    _is_replay = fc.get("classifier_source") == "cache"
    if not _is_replay:
        return fc.get("classified_ts", int(now))

    if isinstance(prior, dict) and "classified_ts" in prior:
        classified_ts = prior["classified_ts"]
        # Under partition()'s OLD one-sided `now - classified_ts > ttl` check,
        # a future-dated inherited stamp was permanently un-expirable -- that
        # comparison can never be true while the value is ahead of the clock,
        # so a clock-skewed or hand-edited entry would pin the verdict as
        # "known" forever, the very failure #302 exists to prevent.
        # partition() now uses the symmetric `0 <= age <= ttl` range, so a
        # future-dated stamp already fails on read (a negative age) and is
        # rejected there as unusable_ts. This clamp is therefore
        # defense-in-depth, same as the NaN clamp below: it stops a bad value
        # from ever reaching disk, rather than relying solely on the
        # read-time guard to catch it every time a stale entry is read.
        # Clamp to 0 (ancient) rather than to `now`: this run did NOT verify
        # the verdict, so granting it a fresh full TTL would be a bounded
        # restatement of the same "measuring last read, not last
        # verification" bug. 0 forces re-derivation on the next run, which is
        # exactly how partition() already treats a non-numeric stamp.
        # Non-numeric values are deliberately NOT coerced: partition() reads
        # them as ancient already, and comparing a str to a float would raise
        # TypeError and take down the entire cache update.
        try:
            _numeric_ts = float(classified_ts)
        except (TypeError, ValueError, OverflowError):
            _numeric_ts = None
        # Every comparison against NaN is False, so `_numeric_ts > now` would
        # let a NaN prior stamp pass through as a valid inherited value. Under
        # partition()'s OLD one-sided `now - ts > ttl` check, a NaN age would
        # have been permanently un-expirable (every comparison against NaN is
        # False). partition() now uses a symmetric `0 <= age <= ttl` range, so
        # a NaN age already fails on read and is caught there. This clamp is
        # defense-in-depth: it stops a bad value from ever reaching disk,
        # rather than relying solely on the read-time guard to catch it every
        # time. `not (x <= now)` routes both NaN and future values into the
        # clamp below while leaving past and exactly-equal values untouched.
        # With the read-side guard in place, this whole clamp branch is
        # unreachable in the normal single-run flow: partition() would have
        # already routed a future-dated or NaN prior to `needs` as
        # unusable_ts, so classifier_source="cache" (which only partition()'s
        # known-hit path produces) could never carry a bad prior ts here in
        # the first place. It is reachable only via a concurrent writer
        # racing this run's load/save, or a caller that builds
        # classifier_source="cache" records directly without going through
        # partition() first -- so the tests below guard a defense-in-depth
        # branch, not a path this process's own single-run flow can hit.
        if _numeric_ts is not None and not (_numeric_ts <= now):
            print(
                f"[check-items-cache] WARNING: cached classified_ts "
                f"({classified_ts}) for {fc.get('canonical_hash')} is not a "
                f"usable past timestamp; treating it as unverified so it "
                f"re-derives next run",
                file=sys.stderr,
            )
            classified_ts = 0
        return classified_ts

    # A replay whose prior entry cannot supply a timestamp -- either no
    # prior at all, or a prior carrying no classified_ts. Both are the
    # same invariant violation: partition() can only have emitted
    # "cache" by finding a usable entry, so reaching here means one of
    # two things happened: the cache changed between Step 3's
    # load_cache() and Step 10's (they run in separate processes, or a
    # caller bug), OR -- the route update_cache's own comment above
    # names for this same call site -- the #297 _rejected_hashes
    # branch evicted this hash's prior entry earlier in THIS run. This
    # is the one place the #302 fix silently reverts to re-stamping,
    # so it must not be silent.
    print(
        f"[check-items-cache] WARNING: classifier_source='cache' for "
        f"{fc.get('canonical_hash')} but no prior on-disk "
        f"classified_ts to inherit; stamping now, so its TTL restarts "
        f"(cache changed mid-run, or its prior entry was evicted this "
        f"run)",
        file=sys.stderr,
    )
    return fc.get("classified_ts", int(now))


def _freeze_classification(fc: dict, now: float, prior: dict | None = None) -> dict:
    """Normalize a fresh classification dict into the on-disk cache entry shape.

    `prior` is the existing on-disk cache entry for the same canonical_hash
    (or None if there isn't one). Timestamp policy lives in
    _resolve_replay_ts(); this function only shapes the resulting dict.
    """
    return {
        "canonical_hash": fc.get("canonical_hash"),
        "canonical_text": fc.get("canonical_text") or fc.get("representative", ""),
        "members": fc.get("members", []),
        "classification": fc.get("classification"),
        "confidence": fc.get("confidence"),
        "evidence_citation": fc.get("evidence_citation"),
        "action_required": fc.get("action_required"),
        "classified_ts": _resolve_replay_ts(fc, prior, now),
        "classifier_source": fc.get("classifier_source"),
        **{field: fc.get(field) for field in
           ("ai_backend", "ai_model", "ai_prompt_version", "ai_prompt_sha256")},
    }
