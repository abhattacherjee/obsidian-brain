"""Validate python3 -c '...' snippets in all SKILL.md files compile without SyntaxError."""

import glob
import re
import textwrap
import pytest


def _extract_python_snippets():
    """Extract python3 -c '...' blocks from all SKILL.md files."""
    snippets = []
    for skill_path in sorted(glob.glob("skills/*/SKILL.md")):
        skill_name = skill_path.split("/")[1]
        with open(skill_path, encoding="utf-8") as f:
            content = f.read()
        for i, match in enumerate(re.finditer(r"python3\s+-c\s+'(.*?)'", content, re.DOTALL)):
            # Dedent to handle snippets indented inside bash blocks in SKILL.md
            code = textwrap.dedent(match.group(1))
            snippets.append((f"{skill_name}-{i}", code))
    return snippets


_SNIPPETS = _extract_python_snippets()


@pytest.mark.parametrize("name,code", _SNIPPETS, ids=[s[0] for s in _SNIPPETS])
def test_python_snippet_syntax(name, code):
    """Each python3 -c snippet must be valid Python syntax."""
    compile(code, f"<{name}>", "exec")


def test_at_least_one_snippet_found():
    """Sanity check: we should find at least 10 snippets across all skills."""
    assert len(_SNIPPETS) >= 10, (
        f"Expected at least 10 python3 -c snippets, found {len(_SNIPPETS)}"
    )
