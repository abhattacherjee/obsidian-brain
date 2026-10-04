"""Pins /vault-ask's wiki instructions (#395)."""
from pathlib import Path
SKILL = (Path(__file__).resolve().parent.parent / "skills/vault-ask/SKILL.md").read_text()


def test_read_only_statement_is_gone():
    assert "this skill is read-only" not in SKILL


def test_wiki_commands_are_used():
    for cmd in ("wiki.py\" lookup", "wiki.py\" stale", "wiki.py\" count", "wiki.py\" rule", "wiki.py\" file", "--caller"):
        assert cmd in SKILL, cmd


def test_payload_goes_through_a_file_not_a_shell_string():
    assert "wiki-payload-" in SKILL and "Never build the JSON in a shell string" in SKILL


def test_candidate_cap_and_index_exclusion():
    assert "at most 3" in SKILL and "claude-wiki-index" in SKILL


def test_reviewed_choices_offered():
    for opt in ("Keep my page", "Refresh and overwrite my edits", "Save the fresh answer as a new page"):
        assert opt in SKILL, opt


def test_file_outcomes_by_exit_code():
    for phrase in ("**Exit 0 with a `warning` key:** tell the user the page was saved",
                   "quote the warning",
                   "**Exit 1:** quote the `ERROR:` line and say nothing was saved",
                   "**Exit 2:** report the failure"):
        assert phrase in SKILL, phrase


def test_rejected_sources_are_dropped_before_filing():
    assert "Drop every rejected name from the `sources` list before filing" in SKILL
    assert "Never file with a rejected name in `sources`" in SKILL


def test_stale_refresh_below_threshold_is_reported():
    assert "tell the user the page could not be refreshed because the fresh answer has fewer than 3 qualifying sources" in SKILL
    assert "the old page stays as is" in SKILL


def test_failed_stale_check_is_not_fresh():
    assert "**`stale` itself fails** (exit 1 or 2, or no JSON): treat the page as not fresh. Do not answer from it." in SKILL


def test_reviewed_flag_is_read_before_update():
    assert "Read the candidate page and check `reviewed:` in its frontmatter" in SKILL


def test_reviewed_page_save_as_new_branch():
    assert ('**Reviewed page, user chose "Save the fresh answer as a new page":** '
            "file a new page, without `update`") in SKILL


def test_payload_dir_created_private():
    assert "run `mkdir -m 700 -p ~/.claude/obsidian-brain`" in SKILL
    assert "mkdir -m 700 -p ~/.claude/obsidian-brain\npython3 '<hooks_dir>/wiki.py'" in SKILL


def test_wiki_folder_comes_from_validated_helper():
    assert "folders = indexed_folders(c, strict=True)" in SKILL
    assert 'print("WIKI=" + wiki)' in SKILL
    assert 'c.get("wiki_folder") or ""' not in SKILL
    assert "WIKI= is printed empty" in SKILL


def test_missing_wiki_py_skips_wiki_steps():
    assert 'test -f "$HOOKS/wiki.py" && echo "WIKI_OK" || echo "WIKI_MISSING"' in SKILL
    assert "On `WIKI_MISSING`, treat `WIKI` as empty" in SKILL


def test_unverifiable_is_a_stale_reason():
    assert "`unverifiable: <page>`" in SKILL
