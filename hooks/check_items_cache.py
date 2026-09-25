"""
Cache module for /check-items classifier results.

Per spec section Persistence and cache invalidation (lines 410-494).
Python stdlib only.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

CACHE_DIR: Path = Path.home() / ".claude" / "obsidian-brain"
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
    def _ret(data, status):
        return (data, status) if with_status else data

    if not CACHE_PATH.exists():
        return _ret(_empty_cache(), None)
    try:
        with CACHE_PATH.open("r", encoding="utf-8") as f:
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
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", delete=False, dir=str(CACHE_DIR), suffix=".tmp", encoding="utf-8"
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
    os.replace(tmp.name, str(CACHE_PATH))
    os.chmod(str(CACHE_PATH), 0o600)


# #306: load_cache() -> save_cache() is an unlocked read-modify-write. Nothing
# stops a run on project B from loading a stale snapshot, a run on project A
# completing in between, and B's save overwriting the whole document --
# silently erasing A's entry. locked_cache() below wraps the cycle in mutual
# exclusion, following the shape of claim_hook_run() in obsidian_utils.py
# (O_EXCL create, stale-TTL takeover, fail-open on any filesystem error) but
# NOT importing from it -- this module has no such dependency today and must
# keep none. The two differ in one deliberate way: claim_hook_run() is a
# dedup where a contended loser correctly abandons its own work; this is
# mutual exclusion where a contended caller's write must still land, so
# contention here polls until timeout rather than giving up immediately.

_LOCK_TIMEOUT_SECONDS = 10.0   # max time locked_cache() waits for a contended lock
_LOCK_STALE_SECONDS = 30.0     # age past which a lock is presumed abandoned (crashed holder)
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


def _lock_payload() -> bytes:
    """Identifies the current holder so a release can verify it still owns
    the lock (see _release_lock) before unlinking."""
    return f"{os.getpid()} {time.time():.3f}\n".encode("utf-8")


def _try_create_lock(lock_path: Path, payload: bytes) -> None:
    """O_EXCL create. Raises FileExistsError if already locked, or another
    OSError for e.g. a permission failure -- both are handled by the caller."""
    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def _lock_age(lock_path: Path, holder: bytes) -> float:
    """Age of the lock identified by `holder`, in seconds.

    Read from the payload's OWN timestamp whenever it parses, so that a
    lock's identity and its age come from a SINGLE read. Deriving age from a
    separate stat() lets the file be replaced in between: a fresh holder's
    identity then gets paired with the previous holder's age, the lock reads
    as stale when it is not, and the takeover deletes a live holder's lock.
    That mis-pairing was reproduced deterministically -- the single-read
    invariant is the actual fix, not the re-read confirmation below it.

    Falls back to st_mtime for a payload this version cannot parse (an older
    format, or a hand-created file).

    #323 F7: only a valid PAST time is trusted, from EITHER source. This is
    a regression #306 (9cfc2bf) introduced when it moved age off st_mtime
    and onto the payload's own embedded timestamp -- that made age a parsed
    value from file content, and float() happily accepts "nan"/"inf", while
    a future-dated stamp (a backward clock step: NTP correction, a suspended
    VM resuming) yields a negative age. All three compare `> _LOCK_STALE_SECONDS`
    as False forever, so takeover never fires and nothing ever unlinks the
    lock again -- every later run stalls the full timeout and then writes
    UNLOCKED, permanently, which is strictly worse than the pre-#306
    baseline (that at least never stalled). Verified by execution against
    the pre-fix 9cfc2bf: a future-ts, NaN-ts, or +inf-ts payload each wedged
    (age stayed "not stale" and _acquire_lock took the full timeout every
    time), while a normal stale payload and an unparseable-payload fallback
    both still recovered via takeover.

    This mirrors a convention this module already applies to
    classified_ts (_classified_ts_age / partition()'s `0 <= age` range
    check, guarded by four tests) -- an unusable stamp must never read as
    "fresh forever". Unlike classified_ts, where an unusable value means
    "re-derive it", an unusable LOCK timestamp must resolve to STALE, not
    fresh: "fresh forever" is exactly what wedges the lock, and treating it
    as fresh is the one choice with no self-healing path. A wrongful
    takeover this produces is still detectable after the fact --
    `_release_lock` warns when it finds the lock was taken from under it.

    The acceptance gate is `-_LOCK_AGE_ROUNDING_TOLERANCE_SECONDS <= age`,
    not the bare `0 <= age` classified_ts uses: `_lock_payload()` formats
    its timestamp with millisecond precision (`.3f`), which rounds, so a
    lock read back and aged microseconds after being written can compute
    as up to ~0.5ms "ahead" of `time.time()` -- a pure formatting artifact.
    A bare `0 <= age` gate turned that artifact into a real bug: roughly
    HALF of freshly-written payloads (measured: 956,812 / 2,000,000 trials)
    failed the gate and fell through to the st_mtime fallback, reintroducing
    the exact single-read mis-pairing this function exists to prevent. See
    `_LOCK_AGE_ROUNDING_TOLERANCE_SECONDS`'s own comment for why 10ms is
    safe on both sides.
    """
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
    """Acquire the lock, waiting up to `timeout` seconds under contention.

    Returns the payload written on success, or None if the lock could not be
    acquired -- on a timeout, or on any OSError (e.g. an unwritable lock
    directory) -- so the caller must proceed WITHOUT the lock (see
    locked_cache()'s fail-open handling).
    """
    try:
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[check-items-cache] WARNING: cache lock directory unavailable "
              f"({exc}); proceeding without the cache lock", file=sys.stderr)
        return None

    # Monotonic, not wall clock: an NTP correction mid-wait would otherwise
    # stretch or collapse the timeout arbitrarily. Staleness stays on the
    # wall clock instead -- it compares a stamp WRITTEN BY ANOTHER PROCESS
    # (the payload, else st_mtime) against time.time(), and two processes
    # share no monotonic epoch, so the two clocks cannot be unified. (This
    # comment used to justify the split by st_mtime alone; since the payload
    # became the primary source in 9cfc2bf, the value being trusted is one
    # an external writer controls -- which is what made the #323 F7 guard
    # above necessary.)
    deadline = time.monotonic() + timeout
    while True:
        # #323 F8: built fresh on EVERY attempt, not once before the loop.
        # A payload timestamped before the wait began is already up to
        # `timeout` seconds old the moment a contended winner's create
        # finally succeeds -- silently cutting its effective stale grace
        # period from _LOCK_STALE_SECONDS down to
        # _LOCK_STALE_SECONDS - timeout. Timestamping at the moment of the
        # attempt that actually succeeds keeps the grace period intact.
        payload = _lock_payload()
        try:
            _try_create_lock(lock_path, payload)
            return payload
        except FileExistsError:
            try:
                holder = lock_path.read_bytes()
            except OSError:
                # Lock vanished between the failed create and this read (the
                # holder just released it), or it is unreadable. Fall through
                # to the bounded poll -- never loop straight back to the
                # create, which would spin without honouring `timeout`.
                holder = None
            if holder is not None and _lock_age(lock_path, holder) > _LOCK_STALE_SECONDS:
                # Stale lock: a crashed holder must not wedge the cache
                # permanently unwritable. Takeover is inherently racy --
                # multiple waiters can all observe staleness at once and
                # race to unlink + recreate. Losing that race raises
                # FileExistsError again on the very next loop iteration (a
                # rival's fresh lock, not our own), which routes back into
                # this same branch and falls through to ordinary polling --
                # so the failure direction is always "keep waiting", never
                # "assume ownership and write unprotected while a
                # legitimate new holder is mid-write".
                try:
                    # Re-read immediately before unlinking and require the
                    # bytes to be UNCHANGED. Between measuring staleness and
                    # unlinking, the stale holder can release and a NEW
                    # holder legitimately take the lock; unlinking then
                    # deletes a LIVE holder's lock and puts two writers in
                    # the critical section at once. That is not theoretical
                    # -- it was reproduced deterministically before this
                    # check existed.
                    #
                    # A residual window remains between this read and the
                    # unlink. Closing it completely needs rename-based
                    # claiming, whose failure mode (restoring a lock that a
                    # third process may already have recreated) is worse
                    # than the microsecond window it removes. The common
                    # case -- the lock was replaced while we deliberated --
                    # is caught here.
                    if lock_path.read_bytes() == holder:
                        try:
                            lock_path.unlink()
                        except OSError as exc:
                            # Distinct from the read failure below: we DID
                            # confirm the lock is stale and ours to take, but
                            # could not remove it (a foreign owner, a
                            # read-only volume, a macOS uchg flag, a
                            # directory sitting at the lock path). Silently
                            # swallowing this makes every later run pay the
                            # full timeout and then run unlocked, forever,
                            # with nothing distinguishing it from ordinary
                            # contention (#323 F4).
                            print(
                                f"[check-items-cache] WARNING: could not "
                                f"remove the cache lock {lock_path} ({exc}); "
                                f"later runs will stall {timeout:.0f}s and "
                                f"run unlocked until it is removed",
                                file=sys.stderr,
                            )
                except OSError:
                    # Cannot even READ it to confirm staleness: a foreign
                    # owner, a read-only volume, or a directory sitting at
                    # the lock path. Fall through to the bounded poll rather
                    # than retrying forever.
                    pass
        except OSError as exc:
            print(f"[check-items-cache] WARNING: cache lock acquire failed "
                  f"({exc}); proceeding without the cache lock", file=sys.stderr)
            return None
        # EVERY path that does not return lands here, so `timeout` governs
        # unconditionally. Two branches above used to `continue` straight
        # back to the create, bypassing this check: with a stale lock that
        # could not be unlinked, _acquire_lock never returned and pegged a
        # core -- an unbounded busy loop strictly worse than the fail-safe
        # race #306 set out to fix. Verified by execution before the fix
        # (timeout=1.0 still spinning at 5s) and after (returns in ~1s).
        if time.monotonic() >= deadline:
            return None
        time.sleep(_LOCK_POLL_INTERVAL_SECONDS)


def _read_lock(lock_path: Path) -> bytes | None:
    """Read the lock file's current payload, or None if it cannot be read.

    ENOENT (the lock is simply gone -- released, or never existed) returns
    None silently, the ordinary case. Any OTHER OSError (EACCES, EIO, a
    directory sitting at the path) also returns None but is reported first:
    that is NOT "cleanly released", it is "could not be verified", and
    treating the two identically is exactly the swallow that let an
    un-removable/unreadable lock degrade every future run silently and
    forever (#323 F4). Shared by `_release_lock` and `locked_cache`'s
    pre-save ownership check (#323 F2) so the narrowing lives in one place.
    """
    try:
        with open(lock_path, "rb") as f:
            return f.read()
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            print(
                f"[check-items-cache] WARNING: could not read the cache "
                f"lock {lock_path} ({exc}); treating it as unverifiable",
                file=sys.stderr,
            )
        return None


def _release_lock(lock_path: Path, payload: bytes) -> None:
    """Release a lock this process still owns.

    Re-reads the on-disk payload first: if another process took the lock
    over as stale (see _acquire_lock) while we still believed we held it,
    the on-disk payload no longer matches ours, and unlinking would delete
    THEIR lock, letting a third writer race in unprotected. Tolerates the
    file already being gone. Never raises.
    """
    current = _read_lock(lock_path)
    if current is None:
        return  # already gone, or unreadable -- _read_lock already warned
    if current != payload:
        # Our lock was taken over as stale while we still believed we held
        # it -- meaning another writer was inside the critical section
        # concurrently with us. Do NOT unlink (that would delete THEIR
        # lock), and do not stay silent: this is the one observable trace
        # that mutual exclusion was actually violated, and swallowing it
        # would leave a real concurrent-write window with no evidence at
        # all. Reaching this usually means the body outran
        # _LOCK_STALE_SECONDS, but not always -- the documented residual
        # window in _acquire_lock's takeover branch, between its
        # confirm-read and the unlink, can also land a rival's fresh lock
        # here after a body that ran for microseconds (#323 F9).
        print(
            f"[check-items-cache] WARNING: this run's cache lock was taken "
            f"over by another process while still held ({lock_path}); the "
            f"cache write may have raced a concurrent writer",
            file=sys.stderr,
        )
        return
    try:
        lock_path.unlink()
    except OSError as exc:
        # Confirmed we still own it, but could not remove it -- same failure
        # mode as the stale-takeover unlink in _acquire_lock, and the same
        # reason it must not be silent (#323 F4).
        print(
            f"[check-items-cache] WARNING: could not remove the cache lock "
            f"{lock_path} ({exc}); later runs will stall "
            f"{_LOCK_TIMEOUT_SECONDS:.0f}s and run unlocked until it is "
            f"removed",
            file=sys.stderr,
        )


@contextlib.contextmanager
def locked_cache(timeout: float = _LOCK_TIMEOUT_SECONDS):
    """Context manager for the full cache load-mutate-save cycle, under a lock.

    Acquires an O_EXCL lock file derived from CACHE_PATH, loads the cache,
    yields it for the caller to mutate (typically via update_cache()), and
    saves + releases once the `with` block exits -- so the whole
    read-modify-write is one operation a caller cannot half-perform.

    IMPORTANT: the yielded object must be mutated IN PLACE. `save_cache()`
    below saves THIS function's own `cache` binding -- a caller that does
    `cache = update_cache(cache=cache, ...)` and relies on the rebind is
    saving nothing extra today only because `update_cache()` happens to
    mutate its argument and return that same object; a caller that rebinds
    the local name inside the `with` block would silently persist the
    pre-mutation snapshot the day that stops being true (#323 F6).

    The lock path is derived from CACHE_PATH at call time, not a
    module-level constant: tests monkeypatch CACHE_PATH, and a constant
    computed at import time would leave every test contending on one real
    lock file under the user's actual ~/.claude/obsidian-brain/.

    Fail-open: if the lock cannot be acquired within `timeout` (contention)
    or acquisition hits any OSError, a warning goes to stderr and the body
    proceeds WITHOUT the lock -- the pre-existing unlocked behaviour, which
    is the worst acceptable outcome here. #306's race is fail-safe (a lost
    write just means the entry re-classifies next run), so failing to lock
    must never raise or block the pipeline.

    The lock is released in a `finally`, so a body that raises still
    releases it before the exception propagates. The save itself is NOT in
    that `finally`: it only runs if the body completed without raising,
    matching the pre-existing behaviour where save_cache(cache) was a
    separate statement after the caller's work and was never reached on an
    exception either.

    Two additional guards run just before the save (#323):

    F1 -- `load_cache(with_status=True)` distinguishes WHY the on-disk cache
    came back empty. A `status` of "corrupt" (the file itself is garbage) is
    the one case where overwriting it IS the recovery, so it still saves.
    "unreadable" (a transient OSError -- EACCES, EIO, EMFILE -- reading an
    otherwise possibly-fine file) or "foreign_schema" (valid JSON written by
    a newer/different version) means the on-disk data was never proven bad;
    saving over it would silently erase every other project's cache entries
    under a lock, with no warning that an overwrite was even happening. Both
    refuse to save instead -- fail-safe, since a dropped cache update just
    means everything re-classifies next run.

    F2 -- even when the lock WAS acquired, the body can outrun
    `_LOCK_STALE_SECONDS` and have it taken over as stale mid-run (see
    `_acquire_lock`'s takeover branch and `_release_lock`'s post-save check
    for the existing after-the-fact detection). Checking `_read_lock(lock_path)
    != payload` HERE, before the save, means the warning describes a race
    the user can still reason about -- "this save could overwrite it" --
    rather than reporting a fait accompli after `save_cache()` has already
    run. This still saves regardless (fail-open is deliberate here too), it
    only adds a warning.
    """
    lock_path = CACHE_PATH.with_suffix(".lock")
    payload = _acquire_lock(lock_path, timeout)
    if payload is None:
        print(
            f"[check-items-cache] WARNING: proceeding without the cache lock "
            f"({lock_path}); a concurrent writer to a different project could "
            f"silently drop this run's cache update",
            file=sys.stderr,
        )
    cache, status = load_cache(with_status=True)
    try:
        yield cache
        if payload is not None and _read_lock(lock_path) != payload:
            print(
                f"[check-items-cache] WARNING: lost the cache lock mid-run "
                f"({lock_path}) -- held longer than the stale threshold "
                f"(e.g. the machine slept); another run may have written "
                f"since, and this save could overwrite it",
                file=sys.stderr,
            )
        if status in (None, "corrupt"):
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


def partition(
    groups: list[dict],
    cache: dict,
    project: str,
    head_sha: str,
    force: bool = False,
    now: float | None = None,
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
        g["_cached_classification"] = cached.get("classification")
        g["_cached_confidence"] = cached.get("confidence")
        g["_cached_evidence_citation"] = cached.get("evidence_citation")
        g["_cached_action_required"] = cached.get("action_required")
        known.append(g)

    return known, needs


def update_cache(
    cache: dict,
    project: str,
    all_groups: list[dict],
    fresh_classifications: list[dict],
    head_sha: str,
    now: float | None = None,
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
    }
