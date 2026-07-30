#!/usr/bin/env python3
"""End-to-end dogfood for #192 — production index DB pollution guard.

Builds a synthetic vault + a SENTINEL "production" DB (never the real
~/.claude one), then asserts a 4-cell prevention matrix plus a regression half
proving normal functionality is intact. Exit non-zero on any failure.

Usage:
    python3 scripts/dev-test/test-issue-192-pollution-guard.py [--dev-repo PATH]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path


def _resolve_hooks(dev_repo: str | None) -> str:
    if dev_repo:
        cand = os.path.join(dev_repo, "hooks")
        if os.path.isdir(cand):
            return cand
    here = os.path.dirname(os.path.abspath(__file__))
    repo_hooks = os.path.abspath(os.path.join(here, "..", "..", "hooks"))
    if os.path.isdir(repo_hooks):
        return repo_hooks
    # Canonical obsidian-brain hooks resolver (#278): marketplace-registered
    # install location first, allowlisted-and-version-sorted cache fallback.
    #
    # Kept even though `repo_hooks` above returns first whenever this script
    # runs from its own checkout — which is the normal case, so this block is
    # unreachable there. It is here for the abnormal one: copied out of the
    # repo (a scratch dir, a downloaded gist) with no --dev-repo, where the
    # relative `../../hooks` misses. Before #278 that path fell straight to
    # the cache and silently tested the stale released tree; now it resolves
    # the registered checkout first, same as every other site. Deleting it
    # would make this the one tool that still prefers the cache.
    try:
        for _m in json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json"))).values():
            _s = _m.get("source") if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get("source") == "directory"):
                continue
            _i = _m.get("installLocation") if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            _h = os.path.join(_i, "hooks")
            if os.path.isfile(os.path.join(_h, "obsidian_utils.py")):
                return _h
    except Exception:
        pass
    cache = [_d for _d in glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")) if re.fullmatch("[0-9]+([.][0-9]+)*", _d.split("/")[-2])]
    if cache:
        return max(cache, key=lambda _p: ([int(_n) for _n in _p.split("/")[-2].split(".")], _p))
    raise SystemExit("could not resolve hooks/ dir; pass --dev-repo")


PASS = 0
FAIL = 0


def pass_(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  ✅ {msg}")


def fail_(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  ❌ {msg}")


def _make_vault(root: Path, n: int) -> Path:
    vault = root / "vault"
    sessions = vault / "claude-sessions"
    sessions.mkdir(parents=True)
    for i in range(n):
        (sessions / f"2026-06-03-demo-{i:04d}.md").write_text(
            f"---\ntype: claude-session\nproject: demo\n---\n\n# Demo {i}\n"
            f"alpha bravo charlie note {i}\n",
            encoding="utf-8",
        )
    return vault


def _rowcount(db: str) -> int:
    if not os.path.exists(db):
        return 0
    conn = sqlite3.connect(db)  # noqa: vault-db-connect — independent verification read on a sentinel/tmp DB, not the prod index
    try:
        return conn.execute("SELECT count(*) FROM notes").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev-repo", default=None)
    args = ap.parse_args()

    sys.path.insert(0, _resolve_hooks(args.dev_repo))
    import vault_index  # noqa: E402

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        n_notes = 5
        vault = _make_vault(root, n_notes)
        sentinel_prod = root / "sentinel-prod.db"
        iso = root / "iso.db"

        # --- Cell 1: test ctx + REAL prod path -> guard raises ---
        os.environ["PYTEST_CURRENT_TEST"] = "dogfood::cell1"
        try:
            vault_index._connect(vault_index._REAL_PROD_DB)
            fail_("cell1: _connect did NOT raise on real prod path under test ctx")
        except RuntimeError:
            pass_("cell1: guard blocks real prod DB under test ctx")
        finally:
            os.environ.pop("PYTEST_CURRENT_TEST", None)

        # --- Cell 2: test ctx + isolated path -> writes succeed ---
        os.environ["PYTEST_CURRENT_TEST"] = "dogfood::cell2"
        try:
            db = vault_index.ensure_index(str(vault), ["claude-sessions"], db_path=str(iso))
            if _rowcount(db) == n_notes:
                pass_("cell2: isolated write under test ctx succeeded")
            else:
                fail_(f"cell2: expected {n_notes} rows, got {_rowcount(db)}")
        finally:
            os.environ.pop("PYTEST_CURRENT_TEST", None)

        # --- Cell 3: test ctx + INDIRECT (no db_path) -> lands in OBSIDIAN_BRAIN_DB, sentinel untouched ---
        indirect_db = root / "indirect.db"
        os.environ["PYTEST_CURRENT_TEST"] = "dogfood::cell3"
        os.environ["OBSIDIAN_BRAIN_DB"] = str(indirect_db)
        # Point the sentinel as if it were "prod" and confirm it is never written.
        before = _rowcount(str(sentinel_prod))
        try:
            db = vault_index.ensure_index(str(vault), ["claude-sessions"])  # no db_path
            ok_path = os.path.realpath(db) == os.path.realpath(str(indirect_db))
            ok_sentinel = _rowcount(str(sentinel_prod)) == before
            if ok_path and ok_sentinel and _rowcount(db) == n_notes:
                pass_("cell3: indirect call routed to isolated DB; sentinel untouched")
            else:
                fail_(f"cell3: path_ok={ok_path} sentinel_ok={ok_sentinel} rows={_rowcount(db)}")
        finally:
            os.environ.pop("PYTEST_CURRENT_TEST", None)
            os.environ.pop("OBSIDIAN_BRAIN_DB", None)

        # --- Cell 4: NON-test ctx + default(env) -> writes succeed (production success path) ---
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        os.environ["OBSIDIAN_BRAIN_DB"] = str(sentinel_prod)
        try:
            db = vault_index.ensure_index(str(vault), ["claude-sessions"])  # default -> sentinel
            if os.path.realpath(db) == os.path.realpath(str(sentinel_prod)) and _rowcount(db) == n_notes:
                pass_("cell4: production success path writes normally (no guard in non-test ctx)")
            else:
                fail_(f"cell4: db={db} rows={_rowcount(db)}")
        finally:
            os.environ.pop("OBSIDIAN_BRAIN_DB", None)

        # --- Regression half: functionality intact on isolated DB ---
        # NOTE: search_vault actual signature is search_vault(db_path, query, ..., limit=...)
        # — db_path is the FIRST positional arg, query is the SECOND.
        # The plan's draft had them swapped; corrected here to match vault_index.py line 1451.
        os.environ["OBSIDIAN_BRAIN_DB"] = str(iso)
        try:
            db = vault_index.ensure_index(str(vault), ["claude-sessions"], db_path=str(iso))

            hits = vault_index.search_vault(str(iso), "bravo", limit=10)
            if hits:
                pass_(f"regression: search_vault returned {len(hits)} hit(s)")
            else:
                fail_("regression: search_vault returned no hits for seeded term")

            note0 = str(vault / "claude-sessions" / "2026-06-03-demo-0000.md")
            vault_index.log_access(str(iso), note0, "recall", "demo")
            acts = vault_index.batch_activations(str(iso), [note0])
            if note0 in acts:
                pass_("regression: log_access + batch_activations work via _connect")
            else:
                fail_("regression: batch_activations missing seeded note")

            if _rowcount(str(iso)) == n_notes:
                pass_(f"regression: index note count == {n_notes} (no inflation)")
            else:
                fail_(f"regression: expected {n_notes} rows, got {_rowcount(str(iso))}")
        finally:
            os.environ.pop("OBSIDIAN_BRAIN_DB", None)

    print(f"\n=== dogfood #192: {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
