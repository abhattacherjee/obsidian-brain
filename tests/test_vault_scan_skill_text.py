"""The /vault-ask and /vault-search skills use hooks/vault_scan.py where a
fixed-line Read or a missing Grep tool used to lose data (#312, #375).

Skills are prose the model follows, so these tests pin the prose: each
instruction is checked inside the step it belongs to, not anywhere in the
file, so moving a sentence to the wrong step fails.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILLS = {
    "vault-ask": REPO / "skills" / "vault-ask" / "SKILL.md",
    "vault-search": REPO / "skills" / "vault-search" / "SKILL.md",
}


def _step(skill: str, number: str) -> str:
    text = SKILLS[skill].read_text(encoding="utf-8")
    m = re.search(
        rf"^### Step {re.escape(number)} — .*?(?=^### Step |\Z)", text, re.M | re.S,
    )
    assert m, f"{skill}: Step {number} not found"
    return m.group(0)


@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_step4_has_vault_scan_grep_fallback(skill):
    step = _step(skill, "4")
    assert 'python3 "$HOOKS/vault_scan.py" grep' in step
    assert "Grep tool is not available" in step


@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_step5_reads_metadata_with_vault_scan_meta(skill):
    step = _step(skill, "5")
    assert 'python3 "$HOOKS/vault_scan.py" meta' in step
    assert "Read(" not in step


@pytest.mark.parametrize("skill", sorted(SKILLS))
@pytest.mark.parametrize("phrase", [
    "first 30 lines", "first 40 lines", "limit=40", "exceeding 20 lines",
])
def test_no_fixed_line_frontmatter_reads(skill, phrase):
    assert phrase not in SKILLS[skill].read_text(encoding="utf-8")


def test_vault_ask_rationale_states_real_depth():
    step = _step("vault-ask", "5")
    assert "line 460" in step


def test_vault_search_tag_mode_is_frontmatter_only():
    assert "--frontmatter-only" in _step("vault-search", "2")
    step4 = _step("vault-search", "4")
    tag_part = step4.split("**For tag mode:**", 1)[1].split("**For structured mode:**", 1)[0]
    # The command itself, not just the prose, must carry the flag.
    cmds = [ln for ln in tag_part.splitlines()
            if ln.startswith('python3 "$HOOKS/vault_scan.py" grep ')]
    assert len(cmds) == 1, cmds
    assert cmds[0].split()[-1] == "--frontmatter-only"
    assert "Grep(" not in tag_part


@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_vault_scan_blocks_resolve_hooks_and_check_the_script(skill):
    text = SKILLS[skill].read_text(encoding="utf-8")
    blocks = re.findall(r"^```bash\n(.*?)^```", text, re.M | re.S)
    scan_blocks = [b for b in blocks if "vault_scan.py" in b]
    assert len(scan_blocks) >= 2, skill
    for b in scan_blocks:
        assert b.startswith('cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"\n')
        assert '\nHOOKS=$(python3 -c "\n' in b
        assert 'test -f "$HOOKS/vault_scan.py" ||' in b


@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_vault_scan_calls_single_quote_pasted_values(skill):
    # #386: values pasted from earlier steps go in single quotes, never "$VAR".
    text = SKILLS[skill].read_text(encoding="utf-8")
    calls = [ln for ln in text.splitlines() if ln.startswith('python3 "$HOOKS/vault_scan.py"')]
    assert calls, skill
    for ln in calls:
        rest = ln[len('python3 "$HOOKS/vault_scan.py"'):]
        assert '"' not in rest, ln
        assert "'<vault_path>'" in rest, ln
