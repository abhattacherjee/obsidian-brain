"""
Report writer for /check-items.

Path: <vault>/<check_items_folder>/check-items-<scope>-<YYYY-MM-DD>.md
where `check_items_folder` is read from
`~/.claude/obsidian-brain-config.json` (default: `claude-check-items`).
The folder is configurable so users can keep /check-items notes
separate from Dataview dashboards.

Always written, even on --dry-run or user cancel.
Idempotent — overwritten on next run with same scope/date.

Atomic temp+rename pattern.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


def _safe_filename_component(s: str) -> str:
    """Restrict to [A-Za-z0-9_-]. Replace anything else (including dots) with '_'.

    Dots are excluded from the allowed set so that path-traversal sequences
    like '../../etc/passwd' cannot produce '..' components in the filename.
    """
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(s))[:120]


def _frontmatter(scope_name, date_str, window_days, raw_count, group_count,
                 classifications, applied, cascaded,
                 semantic_merge_mode, classifier_mode, skipped=0,
                 evidence_gaps=None):
    counts = {"done": 0, "needs_action": 0, "stale": 0, "active": 0, "review": 0}
    for c in classifications:
        kind = c.get("classification", "")
        key = {"DONE": "done", "NEEDS-ACTION": "needs_action",
               "STALE": "stale", "ACTIVE": "active", "REVIEW": "review"}.get(kind)
        if key:
            counts[key] += 1
    # scope_name and date_str are sanitized defensively so this helper is safe
    # even if called directly with raw values containing newlines or colons that
    # could inject extra YAML fields.
    safe_scope = _safe_filename_component(scope_name)
    safe_date = _safe_filename_component(date_str)
    # evidence_gaps is None for every caller that doesn't pass it (a true
    # no-op) vs. a dict (even one with no actual gaps) for a caller that
    # does -- so the frontmatter key itself, not just its value, signals
    # whether this run evaluated evidence-gap coverage at all.
    gap_line = ""
    if evidence_gaps is not None:
        gap_count = len(evidence_gaps.get("projects_without_repo") or [])
        gap_line = f"evidence_gaps: {gap_count}\n"
    return (
        "---\n"
        "type: claude-check-items-report\n"
        f"date: {safe_date}\n"
        f"scope: {safe_scope}\n"
        f"window: {window_days}d\n"
        f"total_raw_items: {raw_count}\n"
        f"groups: {group_count}\n"
        f"semantic_merge_mode: {semantic_merge_mode}\n"
        f"classifier_mode: {classifier_mode}\n"
        "classifications:\n"
        f"  done: {counts['done']}\n"
        f"  needs_action: {counts['needs_action']}\n"
        f"  stale: {counts['stale']}\n"
        f"  active: {counts['active']}\n"
        f"  review: {counts['review']}\n"
        f"applied: {applied}\n"
        f"cascaded: {cascaded}\n"
        f"skipped: {skipped}\n"
        f"{gap_line}"
        "tags:\n"
        "  - claude/check-items\n"
        f"  - claude/project/{safe_scope}\n"
        "---\n"
    )


def _body(scope_name, date_str, window_days, raw_count, group_count,
          classifications, applied, cascaded, merges, skipped=0,
          evidence_gaps=None):
    by_kind = {"DONE": [], "NEEDS-ACTION": [], "STALE": [], "ACTIVE": [], "REVIEW": []}
    for c in classifications:
        by_kind.setdefault(c.get("classification", "ACTIVE"), []).append(c)

    parts = [f"# Check-Items Report — {scope_name} — {date_str}", "",
             "## Summary"]
    parts.append(
        f"Window {window_days}d. {raw_count} raw items collapsed to "
        f"{group_count} groups after dedup. "
        f"{len(by_kind['DONE'])} DONE, {len(by_kind['NEEDS-ACTION'])} NEEDS-ACTION, "
        f"{len(by_kind['STALE'])} STALE, {len(by_kind['ACTIVE'])} ACTIVE, "
        f"{len(by_kind['REVIEW'])} REVIEW. "
        f"{applied} applied, {cascaded} cascaded, {skipped} skipped."
    )
    parts.append("")

    # #318 Task 6: a record renders `- [x]` when c.get("applied") is truthy,
    # by FACT not by classification -- in every checkbox section below. A
    # DONE verdict the user deselected (never reached Step 8's primary-flip
    # loop) must render unchecked; a REVIEW/NEEDS-ACTION item the user
    # opted into checkoff and Step 8 actually flipped must render checked.
    # The vault has always been correct here; only this report used to lie
    # by rendering DONE as always-checked and everything else as
    # always-unchecked regardless of what was actually applied.
    done_applied = sum(1 for c in by_kind["DONE"] if c.get("applied"))
    # #318 I2: derive the "outside DONE" count the SAME way done_applied is
    # derived -- from the per-record `applied` stamps themselves, never by
    # subtracting the caller's `applied` run-total from done_applied. The
    # subtraction form let the report print "N additional flips applied
    # outside DONE" while nothing outside DONE actually rendered checked
    # (an unstamped applied total), or vice versa. `applied` (the run
    # total) and this fact-derived stamped_total are independent inputs
    # that CAN legitimately disagree -- e.g. a cascade-only flip that never
    # reached a per-record stamp (see Task 6 report's known limitation) --
    # and a disagreement is a real diagnostic, not something to paper over.
    stamped_outside_done = sum(
        1 for kind, items in by_kind.items() if kind != "DONE"
        for c in items if c.get("applied")
    )
    stamped_total = done_applied + stamped_outside_done

    parts.append(f"## Done ({done_applied} applied of {len(by_kind['DONE'])} classified)")
    for c in by_kind["DONE"]:
        mark = "x" if c.get("applied") else " "
        parts.append(f"- [{mark}] {c.get('canonical_text', '')}")
        parts.append(f"  - Evidence: {c.get('evidence_citation') or 'n/a'}")
    if stamped_outside_done > 0:
        _plural = "" if stamped_outside_done == 1 else "s"
        parts.append(f"_{stamped_outside_done} additional flip{_plural} applied outside DONE "
                     f"(see Needs-Action/Review sections below)._")
    if applied != stamped_total:
        parts.append(
            f"_Note: this run reported {applied} applied, but only "
            f"{stamped_total} record(s) in this report are stamped applied "
            f"— Step 8 may not have stamped everything it flipped._"
        )
    parts.append("")

    # M4: "surfaced" / "needs a human look" no longer claim "not applied" --
    # a NEEDS-ACTION or REVIEW row CAN render `- [x]` (#318 Task 6), so a
    # heading asserting the opposite would contradict its own rows.
    parts.append(f"## Needs-Action ({len(by_kind['NEEDS-ACTION'])} surfaced)")
    for c in by_kind["NEEDS-ACTION"]:
        mark = "x" if c.get("applied") else " "
        parts.append(f"- [{mark}] {c.get('canonical_text', '')}")
        parts.append(f"  - Evidence: {c.get('evidence_citation') or 'n/a'}")
        if c.get("action_required"):
            parts.append(f"  - Action: `{c['action_required']}`")
    parts.append("")

    # REVIEW: names a shipped-looking component/branch/feature but has no
    # citable anchor — surfaced for human judgement, never AUTO-checked by
    # classification alone (#264 Task 1). It CAN render checked when the
    # user explicitly opted it into checkoff and Step 8 actually flipped it
    # (#318 Task 6, c.get("applied")) -- always rendered here regardless of
    # --show-all; unlike ACTIVE its evidence_citation is never scrubbed
    # upstream.
    parts.append(f"## Review ({len(by_kind['REVIEW'])} needs a human look)")
    for c in by_kind["REVIEW"]:
        mark = "x" if c.get("applied") else " "
        parts.append(f"- [{mark}] {c.get('canonical_text', '')}")
        parts.append(f"  - Weak signal: {c.get('evidence_citation') or 'n/a'}")
    parts.append("")

    parts.append(f"## Stale ({len(by_kind['STALE'])} — hidden without --show-all)")
    for c in by_kind["STALE"]:
        parts.append(f"- {c.get('canonical_text', '')}")
    parts.append("")

    parts.append(f"## Active ({len(by_kind['ACTIVE'])} — not reviewed)")
    for c in by_kind["ACTIVE"][:50]:
        parts.append(f"- {c.get('canonical_text', '')}")
    if len(by_kind["ACTIVE"]) > 50:
        parts.append(f"- ... and {len(by_kind['ACTIVE']) - 50} more (truncated)")
    parts.append("")

    parts.append("## Merged Groups")
    # merges must be a list of merge records (see
    # open_item_dedup.merge_records_from_groups()). A caller that passes
    # anything else -- e.g. a bare count (#318 bug 3) -- must not crash the
    # whole dashboard write, and the mistake must be visible in the
    # artefact rather than silently coerced to "no merges this run".
    merge_warning = None
    if merges is None:
        merges = []
    elif not isinstance(merges, (list, tuple)):
        # M7: the repr of an arbitrary bad-input value is unbounded -- cap
        # it before it lands in a kept artefact (a caller could pass a
        # multi-MB string or a deeply nested structure, not just a bare int).
        _MAX_MERGES_REPR = 200
        _merges_repr = repr(merges)
        if len(_merges_repr) > _MAX_MERGES_REPR:
            _merges_repr = _merges_repr[:_MAX_MERGES_REPR] + "... (truncated)"
        merge_warning = (
            f"could not render merges: expected a list of merge records, got "
            f"{type(merges).__name__} ({_merges_repr}) — see open_item_dedup."
            f"merge_records_from_groups()"
        )
        merges = []
    else:
        merges = [m for m in merges if isinstance(m, dict)]

    if merge_warning:
        parts.append(f"**Merged Groups unavailable** — {merge_warning}")
    if not merges:
        parts.append("_No semantic merges this run._")
    else:
        for m in merges:
            cid = m.get("canonical_group_id", "?")
            absorbed = ", ".join(m.get("absorbed_group_ids", []))
            parts.append(f"- {cid} absorbs [{absorbed}] — {m.get('reasoning', '')}")
    parts.append("")

    # #318 Task 5 Step 5.5 (Preflight R3): the artefact half of the
    # git-evidence-gap warning Task 3 already puts on stderr. Rendered only
    # when there's an actual gap to report -- an unconditional section
    # would say nothing (see test_dashboard_omits_gap_section_when_no_gaps).
    if evidence_gaps and evidence_gaps.get("projects_without_repo"):
        parts.append("## Evidence Gaps")
        if evidence_gaps.get("all_projects_gapped"):
            parts.append(
                "This run could not classify anything as DONE for ANY "
                "project — a precondition failure (no git evidence "
                "available for any scanned project), not a triage result."
            )
        for proj in evidence_gaps["projects_without_repo"]:
            parts.append(f"- {proj}: DONE was unreachable (no git repo "
                         f"evidence available for this project).")
        parts.append("")

    return "\n".join(parts)


def write_check_items_dashboard(
    *,
    vault_path,
    scope_name,
    date_str,
    window_days,
    raw_count,
    group_count,
    classifications,
    applied,
    cascaded,
    merges,
    semantic_merge_mode,
    classifier_mode,
    dry_run,
    skipped=0,
    evidence_gaps=None,
):
    """
    Write the check-items dashboard report and return the path.
    Always writes (dry_run only affects upstream pipeline behavior).

    ``skipped`` (#320 F1) is the count of cascade candidates that were
    refused or lost (drifted, unverifiable, checkbox-gone, or a failed
    write) — optional with a 0 default so existing callers are unaffected.

    ``evidence_gaps`` (#318 Task 5 Step 5.5) is an optional dict with
    ``projects_scanned``, ``projects_with_evidence``,
    ``projects_without_repo`` (list of project names), and
    ``all_projects_gapped`` (bool). None (the default) is a true no-op —
    every existing caller omits it, so it must render nothing and add no
    frontmatter key.
    """
    # Folder name comes from user config. Reject empty / absolute / parent-
    # escape values explicitly, and anchor the containment check on
    # vault_root (NOT target_dir, which is itself derived from the same user
    # input and would make the check tautological).
    from obsidian_utils import load_config
    cfg = load_config()
    folder_name = cfg.get("check_items_folder", "claude-check-items")
    if not folder_name or not isinstance(folder_name, str):
        folder_name = "claude-check-items"
    if folder_name.startswith("/") or ".." in Path(folder_name).parts:
        raise ValueError(
            f"refusing to use absolute or parent-traversing check_items_folder: "
            f"{folder_name!r}"
        )

    vault_root = Path(vault_path).resolve()
    target_dir = (vault_root / folder_name).resolve()
    if not target_dir.is_relative_to(vault_root):
        raise ValueError(
            f"refusing to use check_items_folder outside vault root: {target_dir}"
        )
    target_dir.mkdir(parents=True, exist_ok=True)

    safe_scope = _safe_filename_component(scope_name)
    safe_date = _safe_filename_component(date_str)
    filename = f"check-items-{safe_scope}-{safe_date}.md"
    target = (target_dir / filename).resolve()
    if not target.is_relative_to(vault_root):
        raise ValueError(f"refusing to write outside vault root: {target}")

    # Use safe_scope (computed above for the filename) throughout the note body
    # and frontmatter so that crafted scope values cannot inject YAML fields or
    # markdown headings with newlines/colons.
    content = (_frontmatter(safe_scope, date_str, window_days, raw_count, group_count,
                            classifications, applied, cascaded,
                            semantic_merge_mode, classifier_mode, skipped=skipped,
                            evidence_gaps=evidence_gaps)
               + _body(safe_scope, date_str, window_days, raw_count, group_count,
                       classifications, applied, cascaded, merges, skipped=skipped,
                       evidence_gaps=evidence_gaps))

    tmp = tempfile.NamedTemporaryFile(
        mode="w", delete=False, dir=str(target_dir),
        suffix=".tmp", encoding="utf-8"
    )
    try:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
    finally:
        tmp.close()
    os.replace(tmp.name, str(target))
    os.chmod(str(target), 0o600)
    return str(target)
