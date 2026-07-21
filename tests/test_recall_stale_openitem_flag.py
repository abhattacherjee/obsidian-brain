"""Fix B (#264 task 3): /recall must FLAG an open `- [ ]` item that a NEWER
session summary in the same brief reports as done, instead of silently
re-listing it forever.

Drives build_context_brief() directly (in-proc, no sub-agent) and inspects
the <<<OB_OPEN_ITEM_CANDIDATES>>> payload. Read-only: these tests never
check anything off — they only assert the JSON candidate payload carries a
`contradicted_by` date when (and only when) a STRICTLY NEWER session's
`## Summary` reports the item done.

Follows the fixture conventions in tests/test_snapshot_recall.py.
"""
import json

from hooks.obsidian_utils import build_context_brief


def _session(path, date, session_id, project="demo", summary="Did some work.",
             open_items=None, extras=""):
    """Write a minimal claude-session note with a ## Summary and, optionally,
    a ## Open Questions / Next Steps section containing `- [ ]` items."""
    body = f"## Summary\n{summary}\n"
    if open_items:
        items_block = "\n".join(f"- [ ] {t}" for t in open_items)
        body += f"\n## Open Questions / Next Steps\n{items_block}\n"
    path.write_text(
        f"---\ntype: claude-session\ndate: {date}\nsession_id: {session_id}\n"
        f"project: {project}\nstatus: summarized\n{extras}---\n\n"
        f"# Session: {project}\n\n{body}",
        encoding="utf-8",
    )


def _candidates(out):
    """Extract the OB_OPEN_ITEM_CANDIDATES payload as (raw, parsed-list)."""
    marker = "<<<OB_OPEN_ITEM_CANDIDATES>>>"
    idx = out.index(marker)
    payload = out[idx + len(marker):].strip()
    if payload in ("NO_CANDIDATES", "NO_ITEMS"):
        return payload, []
    return payload, json.loads(payload)


def test_stale_open_item_flagged_by_newer_session_summary(tmp_path):
    """POSITIVE: earlier session has the open `- [ ]` item; a strictly LATER
    session's ## Summary reports it done -> the candidate is flagged with
    contradicted_by set to the later session's date."""
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    ins = vault / "claude-insights"
    sess.mkdir(parents=True)
    ins.mkdir()
    _session(
        sess / "2026-04-01-demo-aaaa.md", "2026-04-01", "s1",
        summary="Started the pull-to-refresh docs effort.",
        open_items=["Finish pull-to-refresh docs"],
    )
    _session(
        sess / "2026-04-15-demo-bbbb.md", "2026-04-15", "s2",
        summary="Finish work landed today: the pull-to-refresh docs shipped in this session.",
    )

    out = build_context_brief(str(vault), "claude-sessions", "claude-insights", "demo")
    payload, candidates = _candidates(out)

    assert candidates, f"expected a flagged candidate, got payload={payload!r}"
    match = next((c for c in candidates if "pull-to-refresh" in c["text"]), None)
    assert match is not None, f"expected the pull-to-refresh item in candidates, got {candidates}"
    assert match.get("confidence", 0) >= 3
    assert match.get("contradicted_by") == "2026-04-15", (
        f"expected contradicted_by='2026-04-15', got {match.get('contradicted_by')!r}"
    )


def test_open_item_not_flagged_when_completion_language_in_same_note(tmp_path):
    """NEGATIVE/BOUNDARY (a): completion language in the SAME note as the
    open box must NOT flag the item (no strictly-newer session exists)."""
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    ins = vault / "claude-insights"
    sess.mkdir(parents=True)
    ins.mkdir()
    _session(
        sess / "2026-04-01-demo-cccc.md", "2026-04-01", "s3",
        summary="Finish work landed today: the pull-to-refresh docs shipped in this session.",
        open_items=["Finish pull-to-refresh docs"],
    )

    out = build_context_brief(str(vault), "claude-sessions", "claude-insights", "demo")
    payload, candidates = _candidates(out)

    assert not candidates, (
        f"same-note completion language must NOT flag the item, got {candidates}"
    )


def test_open_item_not_flagged_when_newer_session_lacks_completion_language(tmp_path):
    """NEGATIVE (BH-001): a strictly-newer session that merely CO-MENTIONS the
    branch/file an open item names — without reporting it done — must NOT
    flag the item. A single distinctive-token match (the branch name) alone
    reaches confidence >= 3 with no completion phrase required, which is the
    false-positive this test guards against: mentioning work-in-progress on a
    branch is not evidence the item is done."""
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    ins = vault / "claude-insights"
    sess.mkdir(parents=True)
    ins.mkdir()
    _session(
        sess / "2026-04-01-demo-ffff.md", "2026-04-01", "s6",
        summary="Started work on the pull-to-refresh feature.",
        open_items=["Return to feature/pull-to-refresh-v2 worktree and finish it"],
    )
    _session(
        sess / "2026-04-15-demo-gggg.md", "2026-04-15", "s7",
        summary="Spent the afternoon on feature/pull-to-refresh-v2; still investigating the reducer.",
    )

    out = build_context_brief(str(vault), "claude-sessions", "claude-insights", "demo")
    payload, candidates = _candidates(out)

    match = next((c for c in candidates if "pull-to-refresh-v2" in c["text"]), None)
    assert match is None, (
        f"newer session co-mentioning the branch WITHOUT completion language "
        f"must NOT flag the item, got {match!r}"
    )


def test_open_item_not_flagged_by_older_session_summary(tmp_path):
    """NEGATIVE/BOUNDARY (b): completion language in an OLDER session (dated
    before the open item's own source session) must NOT flag the item —
    only STRICTLY-NEWER sessions count as contradicting evidence."""
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    ins = vault / "claude-insights"
    sess.mkdir(parents=True)
    ins.mkdir()
    _session(
        sess / "2026-03-01-demo-dddd.md", "2026-03-01", "s4",
        summary="Finish work landed today: the pull-to-refresh docs shipped in this session.",
    )
    _session(
        sess / "2026-04-10-demo-eeee.md", "2026-04-10", "s5",
        summary="Kicked off the pull-to-refresh docs effort.",
        open_items=["Finish pull-to-refresh docs"],
    )

    out = build_context_brief(str(vault), "claude-sessions", "claude-insights", "demo")
    payload, candidates = _candidates(out)

    assert not candidates, (
        f"older-session completion language must NOT flag the item, got {candidates}"
    )
