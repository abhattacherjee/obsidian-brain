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
    assert "Drop every rejected name from the `sources` or `memory_sources` list before filing" in SKILL
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


def test_refresh_leaves_the_page_out_of_sources():
    assert "leave the page being refreshed out of `sources`" in SKILL


# --- memory sources (#396) ---------------------------------------------------


def test_memory_search_uses_memgrep_on_every_ask_that_reaches_step_3():
    assert 'wiki.py" memgrep' in SKILL
    assert "The memory search runs on every ask that reaches Step 3" in SKILL
    # Step 3's fast path must not skip it; only a fresh wiki answer does.
    assert "even when Step 3 skipped the Grep searches" in SKILL
    assert "a fresh wiki answer from Step 2b stops before it, by design" in SKILL


def test_memory_citation_is_plain_text():
    assert "memory: <project-dir>/<file>.md" in SKILL
    assert "never as a wikilink" in SKILL


def test_memory_names_go_to_count_and_file():
    assert '"memory_sources": [<every memory file cited in Sources, by its memgrep name>]' in SKILL
    assert '"memory_sources": ["<project-dir>/<file>.md", "..."]' in SKILL


def test_memory_files_stay_out_of_vault_scan_meta():
    assert "Do not pass memory files to `vault_scan.py meta`" in SKILL


def test_skill_has_no_claude_only_memory_path():
    assert "~/.claude/projects" not in SKILL and "/memory/" not in SKILL


# --- review fixes (#399) ------------------------------------------------------


def test_refresh_adds_the_pages_memory_files_from_memory_paths():
    assert "`memory_paths`" in SKILL
    assert "Add each path in `memory_paths` to `CANDIDATE_FILES` as type `claude-memory`" in SKILL


def test_memory_reason_forms_are_listed():
    for form in ("`changed: memory:<project-dir>/<file>.md`", "`missing: memory:<project-dir>/<file>.md`",
                 "`unverifiable: memory:<project-dir>/<file>.md`"):
        assert form in SKILL


def test_memgrep_skipped_and_host_are_reported():
    assert '"host"' in SKILL and '"skipped"' in SKILL
    assert "N memory file(s) could not be read" in SKILL
    assert "this host has no memory files" in SKILL


def test_rejected_names_bullet_covers_memory_sources():
    assert "Never file with a rejected name in `sources` or `memory_sources`." in SKILL
